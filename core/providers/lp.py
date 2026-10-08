"""Valuation of MultiversX liquidity-pool (LP) tokens by reading the pool's
smart contract (`vm-values/query` on a MultiversX gateway).

Why contracts and not a DEX API: no public price exists for most LP tokens
(AshSwap, JEX, OneDex...), but the *pool* knows its reserves and the LP total
supply, so   LP price = value of the reserves / LP supply.

Finding the pool (real-world findings, see README): the account that *issued*
an LP token is often a router / factory, not the pool. The contract that really
mints and burns the LP token is, however, the pool, and the public endpoint
`/tokens/<LP>/roles` lists it (holder of ESDTRoleLocalMint / LocalBurn). So the
candidates for an LP token are: the holders of those roles + the issuer.

Reading the pool: a contract publishes no ABI, so each *adapter* below is data
(candidate view names for one family of contracts). A result is accepted only if
it is **consistent with independent on-chain facts**:

  * the pool itself names the LP token we hold (mandatory, no exception);
  * every reserve token is really held by the contract and the reserve does
    not exceed the contract's actual balance of that token;
  * the LP supply given by the pool agrees with minted - burnt from the API.

A wrong guess therefore yields "no value", never a wrong value.

Adding a DEX that is not recognised: run `python lp_probe.py <LP-identifier>`
(shows the candidate contracts and which views answer), then add a small dict to
ADAPTERS below. See README, section "LP tokens".
"""

from __future__ import annotations

__version__ = "0.1.7"






import base64
import binascii
import json
import math
import os
import re
import time
from decimal import Decimal
from urllib.parse import quote

import requests

from .net import HttpError, request_json
from .safe import MAX_POSITION_USD, safe_decimals, validate_rpc_url

DEFAULT_GATEWAY = "https://gateway.multiversx.com"
MVX_API = "https://api.multiversx.com"

#: hard budgets per run, so a hostile/unknown contract cannot cost minutes.
MAX_LP_TOKENS = 80
MAX_PASSES = 3                # a new budget per pass, same run
FAIL_TTL = 86400              # seconds an unrecognised LP is skipped
MAX_CACHE_ENTRIES = 2000
MAX_CACHE_BYTES = 500_000
MAX_VM_CALLS = 1000
MAX_OWNER_CALLS = 200         # per candidate contract (unknown contracts stay cheap)
MAX_CANDIDATES = 4            # candidate pool contracts per LP token
MAX_RETURN_ITEMS = 5000       # OneDex returns one item per pair (~1500 today)
MAX_RESPONSE_BYTES = 4_000_000
DEADLINE_SECONDS = 120        # wall clock for the whole LP step
MAX_SUPPLY_RAW = 10**60       # anything above is hostile metadata
BALANCE_TOLERANCE = 1.05      # indexed API balances can lag the live gateway
LP_MAX_POSITION_USD = 5e7     # a personal LP position above this is not credible

_TOKEN_ID_RE = re.compile(r"[A-Z0-9]{3,10}-[0-9a-f]{6}")  # use fullmatch
_SC_PREFIX = "erd1qqqqqqqq"  # smart-contract addresses start with 8 zero bytes
_LP_WORDS = ("lp", "liquidity", "pool")


#: filled by price_lp_tokens: how far the last run got (shown to the user).
LAST_STATS: dict = {"candidates": 0, "examined": 0, "valued": 0, "stopped": False}


def gateway_url() -> str:
    """Custom node (env MULTIVERSX_GATEWAY_URL, https only) or the public one."""
    url = validate_rpc_url(os.environ.get("MULTIVERSX_GATEWAY_URL")) or DEFAULT_GATEWAY
    return url.rstrip("/")


# --------------------------------------------------------------------- adapters
# Pure data: lists of candidate view names, tried in order. "curve" says how an
# unpriced side may be treated: constant_product (2 tokens, 50/50 value) or
# multi (stable / n-token pools: every token must be priced).

ADAPTERS: list[dict] = [
    {   # xExchange pair contract (open source: mx-exchange-sc) and its forks.
        "name": "xexchange-pair", "kind": "pair3", "curve": "constant_product",
        "code_hashes": ["PT7gHWG9n6lGmLjk2NpHNsaAX/n9P/cL+xPjljMCvXk="],
        "lp": ["getLpTokenIdentifier"],
        "first": ["getFirstTokenId"],
        "second": ["getSecondTokenId"],
        "reserves_and_supply": ["getReservesAndTotalSupply"],
    },
    {   # JEX: one dedicated contract per pair, separate views.
        "name": "jex-pair", "kind": "named", "curve": "constant_product",
        "code_hashes": ["kDh8hR9vyceELMUuy6JdAg0X90+ZaLeyVQS6tPbY82s=",
                        "Xjk15W4/HVIx+gPFvNqKxdyu9sQhki2AoeEQplOTxuU="],  # JEX/SPORE pair
        "lp": ["getLpToken"],
        "first": ["getFirstToken"],
        "second": ["getSecondToken"],
        "first_reserve": ["getFirstTokenReserve"],
        "second_reserve": ["getSecondTokenReserve"],
        "supply": ["getLpTokenSupply"],
    },
    {   # OneDex: ONE contract for ~1000 pairs, views keyed by a numeric pair id,
        # the id being found in an LP-token -> id map.
        "name": "onedex", "kind": "keyed", "curve": "constant_product",
        "code_hashes": ["3ujBhmT9t7oZ4SbXL6agDT8fFMIzl5QEgr1arlVtvFE="],
        "id_map": ["getLpTokenPairIdMap"],
        "lp": ["getPairLpTokenId"],
        "first": ["getPairFirstTokenId"],
        "second": ["getPairSecondTokenId"],
        "first_reserve": ["getPairFirstTokenReserve"],
        "second_reserve": ["getPairSecondTokenReserve"],
        "supply": ["getPairLpTokenTotalSupply"],
    },
    {   # AshSwap and other pools exposing a token list (+ optional reserve list).
        # Stable pools: when no reserve view answers, the contract's own balances
        # of the listed tokens are the reserves (the pool still has to name the
        # LP token and give a supply that matches the chain).
        "name": "list-pool", "kind": "lists", "curve": "multi",
        "code_hashes": ["tc+lTL/W5PLTIgVVjK/qbL0UNQ9bdHFFdVAFHGME7xA="],
        "lp": ["getLpTokenIdentifier", "getLpTokenId", "getLpToken"],
        "tokens": ["getTokens", "getPoolTokens", "getTokenIds", "getUnderlyingTokens"],
        "reserves": ["getBalances", "getReserves", "getPoolReserves", "getTokenReserves"],
        "supply": ["getTotalSupply", "getLpTotalSupply", "getLpSupply", "getTotalLpSupply"],
    },
    {   # JEX stable pools (3USD, USDC/USDT...), Curve-like.
        "name": "jex-stable", "kind": "stable", "curve": "multi",
        "code_hashes": ["1rVgfuWwjKwNuBM7c56RaeXS3raiTunJ2xQBQKYwug4=",   # USDC/USDT
                        "KV//LSzx57BCMaH9s7UGQomf5OazdF9yffd+/c90HKo="],  # 3USD
        "lp": ["getLptoken", "getLpToken", "getLpTokenIdentifier"],
        "tokens": ["getTokens"],
        "status": ["getStatus"],
        "supply": ["getLpTokenSupply", "getTotalSupply"],
        "virtual_price": ["getVirtualPrice"],
    },
]

# "code_hashes": contract code hashes observed on the real pools of that DEX. A
# self-consistent pool with another code is still valued, but flagged "contrat
# non vérifié" and never with the 50/50 doubling: anyone can deploy a contract
# that answers these views with invented numbers (see README, "Limites").

# (JEX stable pools, "kind": "stable": no list/reserve views, only getStatus (a
#  blob that contains the token ids) and getVirtualPrice. Reserves = the
#  contract's live balances of those tokens, cross-checked against the virtual
#  price, which is the pool's own LP-value-in-peg-units figure.)

#: view names tried by lp_probe.py to help writing a new adapter.
PROBE_NAMES = [
    "getReservesAndTotalSupply", "getReserves", "getReserve", "getFirstTokenId",
    "getSecondTokenId", "getLpTokenIdentifier", "getLpTokenId", "getLpToken",
    "getTotalSupply", "getLpTotalSupply", "getLpTokenSupply", "getTokens", "getTokenIds",
    "getPoolTokens", "getUnderlyingTokens", "getPoolReserves", "getTokenReserves",
    "getBalances", "getPairs", "viewPair", "viewPairs", "getState", "getPoolInfo",
    "getAmplificationFactor", "getAmpFactor", "getVirtualPrice", "getTotalFeePercent",
    "getSwapFeePercent", "getFirstToken", "getSecondToken", "getFirstTokenReserve",
    "getSecondTokenReserve", "getLpTokenPairIdMap", "getLastPairId",
]


# ------------------------------------------------------------------- low level

class Budget:
    """Call budget (optionally nested under a parent) plus a wall-clock deadline."""

    def __init__(self, calls: int | None = None, parent: "Budget | None" = None,
                 deadline: float | None = None):
        self.calls = MAX_VM_CALLS if calls is None else calls
        self.parent = parent
        self.deadline = deadline if deadline is not None else time.monotonic() + DEADLINE_SECONDS

    def spend(self) -> bool:
        if self.calls <= 0 or time.monotonic() > self.deadline:
            return False
        if self.parent is not None and not self.parent.spend():
            return False
        self.calls -= 1
        return True


def _find_return(doc, depth: int = 0):
    if isinstance(doc, dict):
        if "returnData" in doc:
            return doc
        if depth < 3:
            for v in doc.values():
                found = _find_return(v, depth + 1)
                if found is not None:
                    return found
    return None


def vm_query(sc: str, func: str, args: list[str] | None = None, budget: Budget | None = None):
    """Call a view. Returns a list of raw byte strings, or None if the contract
    has no such view / it failed (so adapters can try the next name). Network
    errors propagate (requests.RequestException)."""
    if budget is not None and not budget.spend():
        raise requests.RequestException("LP query budget exhausted")
    try:
        doc = request_json(
            "POST", f"{gateway_url()}/vm-values/query",
            json_body={"scAddress": sc, "funcName": func, "args": list(args or [])},
            max_bytes=MAX_RESPONSE_BYTES, allow_loopback_http=True,
        )
    except HttpError as exc:
        # some gateways answer "unknown function" with a 4xx/500: that only
        # means "no such view"; rate limits / outages must still propagate.
        if re.match(r"HTTP (4\d\d|500) ", str(exc)) and not str(exc).startswith("HTTP 429"):
            return None
        raise
    ret = _find_return(doc)
    if ret is None or str(ret.get("returnCode", "")).lower() != "ok":
        return None
    data = ret.get("returnData") or []
    if not isinstance(data, list) or len(data) > MAX_RETURN_ITEMS:
        return None
    out: list[bytes] = []
    for item in data:
        try:
            out.append(base64.b64decode(item, validate=True) if isinstance(item, str) else b"")
        except (binascii.Error, ValueError):
            raise ValueError("malformed contract answer") from None
    return out


def as_uint(raw: bytes) -> int | None:
    return int.from_bytes(raw, "big") if len(raw) <= 32 else None


def as_token_id(raw: bytes) -> str | None:
    try:
        s = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return s if _TOKEN_ID_RE.fullmatch(s) else None


def _blob_items(raw: bytes, limit: int = 8) -> list[bytes] | None:
    """A Vec encoded as one item: repeated (u32 length + bytes), nothing left over."""
    items: list[bytes] = []
    p = 0
    while p < len(raw):
        if len(items) >= limit or p + 4 > len(raw):
            return None
        n = int.from_bytes(raw[p:p + 4], "big")
        if n == 0 or n > 64 or p + 4 + n > len(raw):
            return None
        items.append(raw[p + 4:p + 4 + n])
        p += 4 + n
    return items or None


def _token_list(ret: list[bytes]) -> list[str] | None:
    """Token ids from either several items or one length-prefixed blob."""
    if not ret:
        return None
    if len(ret) == 1:
        parts = _blob_items(ret[0])
        if parts is None:
            return None
    else:
        parts = ret
    ids = [as_token_id(x) for x in parts]
    if len(ids) < 2 or None in ids or len(set(ids)) != len(ids):
        return None
    return ids  # type: ignore[return-value]


def _uint_list(ret: list[bytes], n: int) -> list[int] | None:
    """n integers from either n items or one length-prefixed blob."""
    if len(ret) == 1 and n > 1:
        parts = _blob_items(ret[0], limit=n)
        if parts is None:
            return None
    else:
        parts = ret
    if len(parts) != n:
        return None
    vals = [as_uint(x) for x in parts]
    return None if None in vals else vals  # type: ignore[return-value]


def _arg_uint(n: int) -> str:
    h = format(n, "x")
    return h if len(h) % 2 == 0 else "0" + h


def _first_answer(sc, names, args, budget):
    """(name, returnData) of the first view in `names` that answers."""
    for name in names:
        res = vm_query(sc, name, args, budget)
        if res:
            return name, res
    return None, None


# ------------------------------------------------------------------ chain facts

def _token_facts(lp: str):
    """{owner, supply_raw, decimals} for the LP token, or None."""
    d = request_json("GET", f"{MVX_API}/tokens/{quote(lp, safe='')}")
    if not isinstance(d, dict):
        return None
    decimals = safe_decimals(d.get("decimals"))
    try:
        supply = int(d.get("initialMinted") or 0) + int(d["minted"]) - int(d["burnt"])
        if decimals is not None and d.get("supply") is not None:
            # second source: the API's own (display-unit) supply must agree
            shown = Decimal(str(d["supply"])) * (10 ** decimals)
            if abs(shown - supply) > max(supply // 50, 10 ** decimals):
                return None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    owner = d.get("owner")
    if not 0 < supply <= MAX_SUPPLY_RAW or decimals is None:
        return None
    return {
        "owner": owner if isinstance(owner, str) and owner.startswith(_SC_PREFIX) else None,
        "supply_raw": supply, "decimals": decimals,
    }


def candidate_contracts(lp: str, facts: dict) -> list[str]:
    """Smart contracts that may be the pool: holders of the LP mint/burn roles,
    then the issuer. (The issuer is often a router/factory, the role holder the pool.)"""
    out: list[str] = []
    try:
        roles = request_json("GET", f"{MVX_API}/tokens/{quote(lp, safe='')}/roles")
    except HttpError:
        roles = None  # endpoint absent for this token: the issuer is still tried
    for r in roles if isinstance(roles, list) else []:
        if not isinstance(r, dict):
            continue
        addr, rl = r.get("address"), r.get("roles")
        if (
            isinstance(addr, str) and addr.startswith(_SC_PREFIX) and isinstance(rl, list)
            and any(x in ("ESDTRoleLocalMint", "ESDTRoleLocalBurn") for x in rl)
            and addr not in out
        ):
            out.append(addr)
    facts["role_holders"] = set(out)
    if facts["owner"] and facts["owner"] not in out:
        out.append(facts["owner"])
    return out[:MAX_CANDIDATES]


def _balance(sc: str, token: str) -> int:
    """The contract's live balance of one fungible token, read from the node
    (gateway: faster than the indexed API, and the same node as the views)."""
    try:
        d = request_json(
            "GET", f"{gateway_url()}/address/{quote(sc, safe='')}/esdt/{quote(token, safe='')}",
            max_bytes=100_000, allow_loopback_http=True,
        )
    except HttpError as exc:
        if re.match(r"HTTP 4\d\d ", str(exc)) and not str(exc).startswith("HTTP 429"):
            return 0  # the node answers an error for "holds none of it"
        raise
    try:
        data = d["data"]["tokenData"]["balance"] if isinstance(d, dict) else 0
        return max(0, int(data))
    except (KeyError, TypeError, ValueError):
        return 0


class PoolState:
    def __init__(self, adapter: str, reserves: dict[str, int], curve: str,
                 verified: bool = False, from_balances: bool = False,
                 virtual_price: int | None = None):
        self.adapter = adapter
        self.reserves = reserves
        self.curve = curve  # "constant_product" (2 tokens) or "multi"
        self.verified = verified          # contract code hash is a known one
        self.from_balances = from_balances  # reserves = contract balances, not a view
        self.virtual_price = virtual_price  # stable pools: LP value in peg units, 1e18 scale
        self.trust: str | None = None        # "known" | "approved" | "permissive" | None
        self.code_hash: str | None = None


class Inconsistent(Exception):
    """The pool answered, but its answer contradicts on-chain facts."""


def _supply_ok(a: int, b: int) -> bool:
    return abs(a - b) <= max(b // 50, 1000)


def _within(reserve: int, balance: int) -> bool:
    """reserve <= balance, with slack: balances come from the indexed API,
    reserves live from the gateway."""
    return reserve <= int(balance * BALANCE_TOLERANCE) + 1


def _code_hash(sc: str, budget: Budget) -> str | None:
    if not budget.spend():
        raise requests.RequestException("LP query budget exhausted")
    d = request_json("GET", f"{MVX_API}/accounts/{quote(sc, safe='')}", params={"fields": "codeHash"})
    h = d.get("codeHash") if isinstance(d, dict) else None
    return h if isinstance(h, str) and len(h) <= 64 else None


def _verify(spec, sc, facts, reserves: dict[str, int], supply: int, budget: Budget,
            from_balances: bool = False, virtual_price: int | None = None) -> PoolState:
    if len(reserves) < 2 or any(v <= 0 for v in reserves.values()):
        raise Inconsistent("unreadable reserves")
    if not _supply_ok(supply, facts["supply_raw"]):
        raise Inconsistent("pool supply contradicts the token supply")
    for tok, res in reserves.items():
        if not budget.spend():
            raise requests.RequestException("LP query budget exhausted")
        if not _within(res, _balance(sc, tok)):
            raise Inconsistent("reserve exceeds the contract's balance")
    h = _code_hash(sc, budget)
    trust = "known" if h in spec.get("code_hashes", ()) else (
        "approved" if _CACHE.is_trusted(h, spec["name"]) else (
            "permissive" if trust_all() else None))
    st = PoolState(spec["name"], dict(reserves), spec["curve"], trust is not None, from_balances, virtual_price)
    st.trust, st.code_hash = trust, h
    return st


def _names_lp(spec, sc, lp, budget, args) -> bool | None:
    """True: the pool names this LP. None: no answer. Inconsistent: names another."""
    _, ans = _first_answer(sc, spec["lp"], args, budget)
    if not ans:
        return None
    if as_token_id(ans[0]) != lp:
        raise Inconsistent("pool names another LP token")
    return True


# ------------------------------------------------------------------------ cache
# Remembers, per LP token, WHICH contract is the pool and WHICH adapter reads it
# (so later runs skip the discovery), and which LP tokens nobody could read (so
# spam does not eat the budget on every run). It never stores a price or a
# reserve: values are recomputed and re-verified against the chain every run.
# Content is public chain data only (token ids, contract addresses).

_ADDR_RE = re.compile(r"erd1[a-z0-9]{58}")
_HASH_RE = re.compile(r"[A-Za-z0-9+/]{43}=")  # base64 of a sha256


def _adapter_signature() -> str:
    return "|".join(sorted(f"{a['name']}:{','.join(a.get('code_hashes', ()))}" for a in ADAPTERS))


class LPCache:
    def __init__(self, path: str | None = None):
        self.path = path
        self.pools: dict[str, dict] = {}
        self.fail: dict[str, float] = {}
        self.trusted: dict[str, str] = {}   # code hash -> adapter, approved by the user
        self.dirty = False
        if path:
            self._load()

    def _load(self) -> None:
        try:
            if os.path.getsize(self.path) > MAX_CACHE_BYTES:
                return
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(doc, dict) or doc.get("v") != 1:
            return
        names = {a["name"] for a in ADAPTERS}
        pools = doc.get("pools")
        for lp, e in (pools.items() if isinstance(pools, dict) else ()):
            if (
                len(self.pools) < MAX_CACHE_ENTRIES and isinstance(lp, str)
                and _TOKEN_ID_RE.fullmatch(lp) and isinstance(e, dict)
                and isinstance(e.get("sc"), str) and _ADDR_RE.fullmatch(e["sc"])
                and e.get("spec") in names
            ):
                self.pools[lp] = {"sc": e["sc"], "spec": e["spec"]}
        trusted = doc.get("trusted")
        for h, spec in (trusted.items() if isinstance(trusted, dict) else ()):
            if len(self.trusted) < 200 and _HASH_RE.fullmatch(str(h)) and spec in names:
                self.trusted[h] = spec
        # "unreadable" verdicts only hold for the adapter set that produced them
        fails = doc.get("fail")
        if doc.get("sig") == _adapter_signature() and isinstance(fails, dict):
            now = time.time()
            for lp, ts in fails.items():
                if (
                    len(self.fail) < MAX_CACHE_ENTRIES and isinstance(lp, str)
                    and _TOKEN_ID_RE.fullmatch(lp) and isinstance(ts, (int, float))
                    and not isinstance(ts, bool) and 0 < now - ts < FAIL_TTL
                ):
                    self.fail[lp] = float(ts)

    def good(self, lp: str):
        e = self.pools.get(lp)
        return (e["sc"], e["spec"]) if e else None

    def put_good(self, lp: str, sc: str, spec: str) -> None:
        if len(self.pools) < MAX_CACHE_ENTRIES or lp in self.pools:
            if self.pools.get(lp) != {"sc": sc, "spec": spec}:
                self.pools[lp] = {"sc": sc, "spec": spec}
                self.dirty = True
        if self.fail.pop(lp, None) is not None:
            self.dirty = True

    def drop_good(self, lp: str) -> None:
        if self.pools.pop(lp, None) is not None:
            self.dirty = True

    def is_trusted(self, code_hash: str | None, spec: str) -> bool:
        return bool(code_hash) and self.trusted.get(code_hash) == spec

    def add_trusted(self, code_hash: str, spec: str) -> bool:
        """Remember a code hash the USER approved (lp_probe.py --trust)."""
        if not _HASH_RE.fullmatch(code_hash or "") or len(self.trusted) >= 200:
            return False
        self.trusted[code_hash] = spec
        self.dirty = True
        return True

    def is_failed(self, lp: str) -> bool:
        ts = self.fail.get(lp)
        return ts is not None and 0 < time.time() - ts < FAIL_TTL

    def put_fail(self, lp: str) -> None:
        if len(self.fail) < MAX_CACHE_ENTRIES or lp in self.fail:
            self.fail[lp] = time.time()
            self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        data = json.dumps({"v": 1, "sig": _adapter_signature(), "pools": self.pools,
                           "fail": self.fail, "trusted": self.trusted})
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass  # a cache that cannot be written is only a missed optimisation


_CACHE = LPCache()


def set_cache_path(path: str | None) -> None:
    """Persist the discovery cache at `path` (None = memory only)."""
    global _CACHE
    _CACHE = LPCache(path)


def default_cache_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ouahza", "lp_cache.json")


def trust_all() -> bool:
    """Permissive mode (user setting): a contract that passes every consistency
    check counts as verified even if its code hash is unknown."""
    return os.environ.get("WALLET_LP_TRUST_ALL") == "1"


def get_cache() -> LPCache:
    return _CACHE


# --------------------------------------------------------------------- strategies

def _try_pair3(spec, lp, facts, sc, ctx, budget):
    if not _names_lp(spec, sc, lp, budget, []):
        return None
    _, a = _first_answer(sc, spec["first"], [], budget)
    _, b = _first_answer(sc, spec["second"], [], budget)
    if not a or not b:
        return None
    t1, t2 = as_token_id(a[0]), as_token_id(b[0])
    if not t1 or not t2 or t1 == t2:
        raise Inconsistent("bad pool tokens")
    _, rs = _first_answer(sc, spec["reserves_and_supply"], [], budget)
    if not rs or len(rs) < 3:
        return None
    r1, r2, sup = as_uint(rs[0]), as_uint(rs[1]), as_uint(rs[2])
    if None in (r1, r2, sup):
        raise Inconsistent("unreadable reserves")
    return _verify(spec, sc, facts, {t1: r1, t2: r2}, sup, budget)


def _try_named(spec, lp, facts, sc, ctx, budget):
    if not _names_lp(spec, sc, lp, budget, []):
        return None
    got = {}
    for key in ("first", "second", "first_reserve", "second_reserve", "supply"):
        _, ans = _first_answer(sc, spec[key], [], budget)
        if not ans:
            return None
        got[key] = ans[0]
    t1, t2 = as_token_id(got["first"]), as_token_id(got["second"])
    r1, r2, sup = as_uint(got["first_reserve"]), as_uint(got["second_reserve"]), as_uint(got["supply"])
    if not t1 or not t2 or t1 == t2 or None in (r1, r2, sup):
        raise Inconsistent("unreadable pool answers")
    return _verify(spec, sc, facts, {t1: r1, t2: r2}, sup, budget)


def _try_keyed(spec, lp, facts, sc, ctx, budget):
    if "id_map" not in ctx:
        _, m = _first_answer(sc, spec["id_map"], [], budget)
        mapping: dict[str, int] = {}
        for i in range(0, len(m or []) - 1, 2):
            tid, num = as_token_id(m[i]), as_uint(m[i + 1])
            if tid and num is not None:
                mapping[tid] = num
        ctx["id_map"] = mapping
    pid = ctx["id_map"].get(lp)
    if pid is None:
        return None
    args = [_arg_uint(pid)]
    if not _names_lp(spec, sc, lp, budget, args):
        raise Inconsistent("pair id does not lead to this LP")
    got = {}
    for key in ("first", "second", "first_reserve", "second_reserve", "supply"):
        _, ans = _first_answer(sc, spec[key], args, budget)
        if not ans:
            return None
        got[key] = ans[0]
    t1, t2 = as_token_id(got["first"]), as_token_id(got["second"])
    r1, r2, sup = as_uint(got["first_reserve"]), as_uint(got["second_reserve"]), as_uint(got["supply"])
    if not t1 or not t2 or t1 == t2 or None in (r1, r2, sup):
        raise Inconsistent("unreadable pool answers")
    return _verify(spec, sc, facts, {t1: r1, t2: r2}, sup, budget)


def _try_lists(spec, lp, facts, sc, ctx, budget):
    if not _names_lp(spec, sc, lp, budget, []):
        return None
    _, tk = _first_answer(sc, spec["tokens"], [], budget)
    ids = _token_list(tk or [])
    if not ids:
        return None
    _, sp = _first_answer(sc, spec["supply"], [], budget)
    sup = as_uint(sp[0]) if sp else None
    if sup is None:
        return None  # without a supply the pool says nothing we can cross-check
    _, rs = _first_answer(sc, spec["reserves"], [], budget)
    vals = _uint_list(rs, len(ids)) if rs else None
    from_balances = vals is None
    if vals is None:
        # no usable reserve view: the contract's own balances are the reserves
        vals = []
        for t in ids:
            if not budget.spend():
                raise requests.RequestException("LP query budget exhausted")
            vals.append(_balance(sc, t))
    vp = _virtual_price(sc, ["getVirtualPrice"], budget)
    return _verify(spec, sc, facts, dict(zip(ids, vals)), sup, budget, from_balances, vp)


def _virtual_price(sc, names, budget) -> int | None:
    _, ans = _first_answer(sc, names, [], budget)
    vp = as_uint(ans[0]) if ans else None
    return vp if vp and 0 < vp < 10**30 else None


def _scan_token_ids(blob: bytes) -> list[str]:
    """Token ids found in an opaque answer, in order of appearance."""
    found = re.findall(rb"[A-Z0-9]{3,10}-[0-9a-f]{6}", blob)
    return list(dict.fromkeys(x.decode("ascii") for x in found))[:8]


def _try_stable(spec, lp, facts, sc, ctx, budget):
    vp = _virtual_price(sc, spec["virtual_price"], budget)
    if vp is None:
        return None  # not a stable pool
    named = _names_lp(spec, sc, lp, budget, [])  # optional here: raises if another LP
    if not named and sc not in facts.get("role_holders", ()):
        return None  # nothing ties this contract to this LP token
    _, tk = _first_answer(sc, spec["tokens"], [], budget)
    ids = _token_list(tk or [])
    if not ids:
        _, st = _first_answer(sc, spec["status"], [], budget)
        # the status blob also carries the LP token's own id, which the pool does not hold
        ids = [t for t in _scan_token_ids(b"".join(st or [])) if t != lp]
    if len(ids) < 2:
        return None
    _, sp = _first_answer(sc, spec["supply"], [], budget)
    sup = as_uint(sp[0]) if sp else facts["supply_raw"]
    vals = []
    for t in ids:
        if not budget.spend():
            raise requests.RequestException("LP query budget exhausted")
        vals.append(_balance(sc, t))
    return _verify(spec, sc, facts, dict(zip(ids, vals)), sup, budget, True, vp)


_STRATEGY = {"pair3": _try_pair3, "named": _try_named, "keyed": _try_keyed, "lists": _try_lists, "stable": _try_stable}


def _ctx(cache: dict, sc: str, budget: Budget) -> dict:
    return cache.setdefault(sc, {
        "dead": False, "spec": None,
        "budget": Budget(MAX_OWNER_CALLS, parent=budget, deadline=budget.deadline),
    })


def discover_pool(lp: str, budget: Budget, cache: dict) -> tuple[dict, PoolState] | None:
    """(facts, PoolState) or None when the LP cannot be read. Network problems
    and budget exhaustion raise requests.RequestException (= "try again later")."""
    facts = _token_facts(lp)
    if not facts:
        return None
    lc = _CACHE
    hit = lc.good(lp)
    if hit:  # fast path: the pool and its dialect are known, only verify
        sc, spec_name = hit
        spec = next((x for x in ADAPTERS if x["name"] == spec_name), None)
        if spec:
            ctx = _ctx(cache, sc, budget)
            try:
                state = _STRATEGY[spec["kind"]](spec, lp, facts, sc, ctx, ctx["budget"])
            except Inconsistent:
                state = None
            if state:
                return facts, state
        lc.drop_good(lp)  # stale (pool migrated / contract upgraded): rediscover
    for sc in candidate_contracts(lp, facts):
        ctx = _ctx(cache, sc, budget)
        if ctx["dead"]:
            continue
        specs = ([ctx["spec"]] if ctx["spec"] else []) + [x for x in ADAPTERS if x is not ctx["spec"]]
        answered = False
        for spec in specs:
            try:
                state = _STRATEGY[spec["kind"]](spec, lp, facts, sc, ctx, ctx["budget"])
            except Inconsistent:
                answered = True
                break  # answered but contradicted by the chain: distrust this contract
            if state:
                ctx["spec"] = spec
                lc.put_good(lp, sc, spec["name"])
                return facts, state
        if not answered and ctx["spec"] is None and not ctx.get("id_map"):
            ctx["dead"] = True  # speaks none of the known dialects: skip for other LPs
    return None


# ---------------------------------------------------------------------- pricing

def pool_value_detail(state: PoolState, infos: dict) -> tuple[float, bool] | None:
    """(USD value of all reserves, estimated?) from `infos` =
    {token_id: {"price", "decimals"}}. `estimated` = one side unpriced, doubled."""
    parts: list[float] = []
    unpriced = 0
    for tid, raw in state.reserves.items():
        info = infos.get(tid) or {}
        dec = safe_decimals(info.get("decimals"))
        if dec is None:
            return None
        try:
            amount = raw / (10 ** dec)
        except OverflowError:
            return None
        price = info.get("price")
        if price:
            parts.append(amount * price)
        else:
            unpriced += 1
    if not parts:
        return None
    estimated = False
    if unpriced == 0:
        value = sum(parts)
    elif (state.verified and state.curve == "constant_product"
          and len(state.reserves) == 2 and unpriced == 1):
        value = 2 * parts[0]  # a 50/50 constant-product pool: both sides worth the same
        estimated = True
    else:
        return None
    if math.isfinite(value) and 0 < value <= MAX_POSITION_USD:
        return value, estimated
    return None


def pool_value_usd(state: PoolState, infos: dict) -> float | None:
    res = pool_value_detail(state, infos)
    return res[0] if res else None


def looks_like_lp(symbol: str, name: str) -> bool:
    text = f"{symbol} {name}".lower()
    return any(w in text for w in _LP_WORDS)


def _vp_mismatch(st: PoolState, infos: dict, price: float) -> bool:
    """Stable pools publish a virtual price (LP value in units of the pegged
    asset). The balances-based price must agree with it, within a loose band."""
    if not st.virtual_price:
        return False
    prices = [(infos.get(t) or {}).get("price") for t in st.reserves]
    if not prices or not all(prices):
        return True
    expected = st.virtual_price / 1e18 * (sum(prices) / len(prices))
    return not (expected > 0 and 0.88 <= price / expected <= 1.12)


def _global_exhausted(budget: Budget) -> bool:
    return budget.calls <= 0 or time.monotonic() > budget.deadline


def price_lp_tokens(candidates: list[str], fetch_infos, on_progress=None) -> dict[str, dict]:
    # `candidates` should be ordered most-valuable-first (the per-run limits
    # then spare the real positions, not the alphabetically early spam).
    """{lp_identifier: {"usd": unit price, "adapter": label, "supply": n}} for the
    LP tokens among `candidates` that could be valued. `fetch_infos(ids)` must
    return {token_id: {"price": usd|None, "decimals": int}} (pricing.py
    provides it). `on_progress(done, total, valued)` is optional.
    Never raises: any failure just leaves that LP unvalued. When a pass runs out
    of budget, up to MAX_PASSES passes continue with the LP tokens left over."""
    lc = _CACHE
    valid = list(dict.fromkeys(c for c in candidates if _TOKEN_ID_RE.fullmatch(str(c))))
    ordered = valid[:MAX_LP_TOKENS]
    unreadable = [lp for lp in ordered if lc.is_failed(lp)]
    # known pools first (cheap, certain), then the unexamined ones, in given order
    todo = sorted((lp for lp in ordered if not lc.is_failed(lp)), key=lambda lp: 0 if lc.good(lp) else 1)
    stats = LAST_STATS
    stats.update(candidates=len(valid), examined=0, valued=0, stopped=False,
                 unreadable=len(unreadable), remaining=0)
    found: list[tuple[str, dict, PoolState]] = []
    total = len(todo)
    for _ in range(MAX_PASSES):
        if not todo:
            break
        budget = Budget()
        cache: dict = {}
        remaining: list[str] = []
        for i, lp in enumerate(todo):
            if _global_exhausted(budget):
                remaining = todo[i:]
                break
            transient = False
            res = None
            try:
                res = discover_pool(lp, budget, cache)
            except Exception:  # noqa: BLE001 - hostile data must never abort the run
                transient = True
            if transient:
                if _global_exhausted(budget):
                    remaining = todo[i:]  # this LP was cut off: redo it first next pass
                    break
                remaining.append(lp)      # outage / rate limit: try again next pass
            else:
                stats["examined"] += 1
                if res:
                    found.append((lp, res[0], res[1]))
                else:
                    lc.put_fail(lp)
            if on_progress:
                try:
                    on_progress(total - len(todo) + i + 1 - len(remaining), total, len(found))
                except Exception:  # noqa: BLE001
                    pass
        lc.save()
        todo = remaining
    stats["remaining"] = len(todo)
    stats["remaining"] += max(0, len(valid) - len(ordered))
    stats["stopped"] = stats["remaining"] > 0
    lc.save()
    if not found:
        return {}
    ids = {t for _, _, st in found for t in st.reserves}
    try:
        infos = fetch_infos(ids)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for lp, facts, st in found:
        detail = pool_value_detail(st, infos)
        if detail is None:
            continue
        value, estimated = detail
        try:
            supply = facts["supply_raw"] / (10 ** facts["decimals"])
            price = value / supply if supply > 0 else None
        except (OverflowError, ZeroDivisionError):
            continue
        if price and math.isfinite(price) and price > 0 and not _vp_mismatch(st, infos, price):
            label = st.adapter
            if estimated:
                label += ", estimation 50/50"
            if st.from_balances:
                label += ", soldes du contrat"
            if st.trust == "approved":
                label += ", hash approuvé"
            elif st.trust == "permissive":
                label += ", contrat non vérifié (mode permissif)"
            elif not st.verified:
                label += ", contrat non vérifié"
            out[lp] = {"usd": price, "adapter": label, "supply": supply}
    stats["valued"] = len(out)
    return out
