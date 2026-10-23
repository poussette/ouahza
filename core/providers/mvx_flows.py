"""Generic estimate of fungible ESDT tokens engaged in smart contracts.

Staking, locking, vesting or lending contracts often take tokens and give
nothing back (no receipt token), so the position is invisible in the wallet.
Instead of coding each protocol, this module keeps a ledger per
(contract, token) from the wallet's transfer history:

    engaged = tokens sent to the contract - tokens received from it

A positive net is reported as an estimate and valued like any ESDT token.

A deposit only counts when the same transaction brought NOTHING back from a
contract (any token, NFT or SFT): swaps, liquidity adds, farm entries and
receipt-token deposits are therefore left to the existing mechanisms. The net
is never negative (rewards can exceed the principal). Interest and penalties
are not modelled, so this stays an estimate.

The WHOLE history is read, oldest first, by date windows (the API's offset
pagination is not reliable), with at most FLOW_MAX_PAGES calls per refresh; the
ledger and a cursor are kept (on disk in the app), so later refreshes only read
what is new. While the history is not fully read the
amounts are provisional and the caller is told so (`done` is False).

Safety margins: a contract's answer can be stamped some seconds after the call
(seen: +12 s), so a deposit only counts after SETTLE_SECONDS of data without an
answer (the wait may span several slices and restarts), and nothing newer than
LAG_SECONDS is integrated at all (indexer lag).
"""
from __future__ import annotations

__version__ = "0.1.45"

import json
import os
import re
import threading
import time
from typing import Callable, NamedTuple

from .safe import clean_text, safe_decimals, write_private

FLOW_PAGE_SIZE = 100
_pages_env = os.environ.get("MVX_FLOWS_PAGES", "")
FLOW_MAX_PAGES = int(_pages_env) if _pages_env.isdigit() and 3 <= int(_pages_env) <= 60 else 12   # 12 calls x up to 100 transfers
MAX_POSITIONS = 120
# A real staking/lock contract holds one or two tokens of the wallet. A contract
# that swallowed many different tokens (router, limit-order book, bridge, game)
# is an exchange whose answers we cannot see (e.g. native EGLD out): not a lock.
MAX_TOKENS_PER_CONTRACT = 4   # distinct tokens with a non-zero balance (stake + a few rewards)


def _addr_list(env: str) -> list[str]:
    return [a.strip().lower() for a in os.environ.get(env, "").replace(";", ",").split(",") if a.strip()]


def _ignored(address: str, contract: str) -> bool:
    """MVX_FLOWS_IGNORE entries: `CONTRACT` (every wallet) or `WALLET:CONTRACT` (that wallet only);
    each side is a full address or 8+ characters of its start or end."""
    for item in _addr_list("MVX_FLOWS_IGNORE"):
        wallet, sep, target = item.partition(":")
        if sep:
            if _matches(address, [wallet]) and _matches(contract, [target]):
                return True
        elif _matches(contract, [item]):
            return True
    return False


def _matches(contract: str, items: list[str]) -> bool:
    c = contract.lower()
    return any(c == i or (len(i) >= 8 and (c.startswith(i) or c.endswith(i))) for i in items)
SETTLE_SECONDS = 120
LAG_SECONDS = 900
MAX_RAW = 10 ** 60
MAX_NET_ENTRIES = 500
MAX_ADDRESSES = 300
MAX_CNAMES = 1000
GROUP_CLOSE_RATIO = 3      # a protocol-wide outflow up to 3x the position still reads as 'it was withdrawn'
MAX_CACHE_BYTES = 1_500_000
START_WINDOW = 30 * 86400
MAX_WINDOW = 365 * 86400
_FIELDS = "txHash,originalTxHash,sender,receiver,status,timestamp,type,function,action,senderAssets,receiverAssets,value,nonce"

_CONTRACT_PREFIX = "erd1qqqqqqqqqqqq"   # smart-contract addresses start with 8 zero bytes
_TOKEN_RE = re.compile(r"[A-Z0-9]{3,10}-[0-9a-f]{6}")
_ADDR_RE = re.compile(r"erd1[a-z0-9]{58}")
_FUNGIBLE = "FungibleESDT"
_FUNC_RE = re.compile(r"[A-Za-z0-9_]{1,40}")


class Position(NamedTuple):
    contract: str        # smart-contract address holding the tokens
    contract_name: str   # public name when known, else ""
    token: str           # token identifier, e.g. JEX-9040ca
    ticker: str
    amount: float


class Result(NamedTuple):
    positions: list
    done: bool           # False: history still being read, amounts are provisional
    percent: int | None  # progress while not done, when the total is known
    error: str           # non-empty: estimate unavailable for this wallet


def is_contract(address) -> bool:
    return isinstance(address, str) and address.startswith(_CONTRACT_PREFIX) and len(address) == 62


# ------------------------------------------------------------------ ledger

def _transfers(item: dict) -> tuple[list[dict], str]:
    """(token transfers, final receiver) of one API transfer entry."""
    action = item.get("action")
    args = action.get("arguments") if isinstance(action, dict) else None
    if not isinstance(args, dict):
        return [], str(item.get("receiver") or "")
    raw = args.get("transfers")
    dest = args.get("receiver")
    if not isinstance(dest, str):
        dest = str(item.get("receiver") or "")
    return [t for t in raw if isinstance(t, dict)] if isinstance(raw, list) else [], dest


def _raw_amount(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if 0 < n <= MAX_RAW else None


def _native_back(item: dict) -> bool:
    """A contract result paying native EGLD to the wallet (not a gas refund).

    Seen on a real unwrap: /transfers lists the payback WITHOUT a `nonce` (the API leaves
    out a zero), and does not list the gas refund at all; a refund that were listed carries
    the nonce of the user's transaction + 1. So a missing or zero nonce is a payback, any
    other nonce is not."""
    n = item.get("nonce", 0)
    if isinstance(n, bool) or not isinstance(n, int) or n != 0:
        return False
    try:
        return int(item.get("value", 0)) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _ts(item: dict) -> int | None:
    v = item.get("timestamp")
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return None
    return v


def _uid(item: dict) -> str:
    return "|".join(str(item.get(k, "")) for k in ("txHash", "originalTxHash", "sender", "receiver", "timestamp")) + \
        "|" + json.dumps(_transfers(item)[0], sort_keys=True, default=str)[:300]


def _group(i: dict) -> str:
    return str(i.get("originalTxHash") or i.get("txHash") or "")


def _order(i: dict) -> tuple:
    """Oldest first; within one second a transaction before the answers it triggers."""
    return (_ts(i) or 0, 1 if i.get("type") == "SmartContractResult" else 0)


KEEP_GROUPS = 3600          # how long the origin of a transaction is remembered (answers are async)


class State:
    """Ledger of one wallet. `net`, `meta` and `stats` are keyed 'contract|token'.

    Every user transaction is a GROUP with the answers it triggers (same
    originalTxHash), and the contract the USER CALLED holds the position, not
    whichever contract finally pays (proxies, routers, forwarding):
      - tokens sent and something comes back in the group -> an exchange or a
        receipt (swap, liquidity, farm, lending): ignored on both sides;
      - tokens sent and nothing comes back -> a deposit into the called contract;
      - nothing sent, tokens come back -> a withdrawal / claim from the called contract.
    Answers whose originating transaction is not the wallet's own are charged
    to the contract that sent them.
    """

    def __init__(self):
        self.cursor = 0                       # everything stamped <= cursor is integrated
        self.edge: list[str] = []             # integrated items stamped exactly `cursor`
        self.net: dict[str, int] = {}
        self.meta: dict[str, list] = {}       # [contract name, ticker, decimals]
        self.stats: dict[str, list] = {}      # [deposits, withdrawals, last deposit ts, [functions]]
        self.done = False
        self.seen = 0
        self.total: int | None = None
        self.win = START_WINDOW               # current time-window length (adapts to the density)
        # Deposits wait SETTLE_SECONDS of data for an answer before they count;
        # the wait may span several windows and restarts.
        self.pending: dict[str, list] = {}    # group -> [ts, [[contract, token, raw, ticker, decimals, name, func], ...]]
        self.calls: dict[str, list] = {}      # group -> [ts, called contract, had tokens out, contract name]
        self.recent_returned: dict[str, int] = {}   # group -> ts of an answer with nothing to charge
        self.cnames: dict[str, str] = {}      # contract -> public name (kept even when a balance returns to zero)
        self.address = ""                     # the wallet (not saved: set when the ledger is used)

    # ---------------------------------------------------------- persistence
    def to_json(self) -> dict:
        net = {k: str(v) for k, v in self.net.items() if v != 0}
        if len(net) > MAX_NET_ENTRIES:
            net = dict(list(net.items())[:MAX_NET_ENTRIES])
        pend = {g: [v[0], [[c, t, str(r), tk, d, n, f] for c, t, r, tk, d, n, f in v[1]]]
                for g, v in list(self.pending.items())[:200]}
        return {"cursor": self.cursor, "edge": self.edge[:200], "net": net,
                "meta": {k: self.meta[k] for k in net if k in self.meta},
                "stats": {k: self.stats[k] for k in net if k in self.stats},
                "pending": pend, "calls": dict(list(self.calls.items())[-400:]),
                "returned": dict(list(self.recent_returned.items())[-500:]),
                "cnames": dict(list(self.cnames.items())[-MAX_CNAMES:]),
                "done": self.done, "seen": self.seen, "total": self.total, "win": self.win}

    @classmethod
    def from_json(cls, doc) -> "State | None":
        if not isinstance(doc, dict):
            return None
        st = cls()
        try:
            cur = doc.get("cursor", 0)
            if isinstance(cur, bool) or not isinstance(cur, int) or cur < 0:
                return None
            st.cursor = cur
            edge = doc.get("edge", [])
            st.edge = [e for e in edge if isinstance(e, str)][:200] if isinstance(edge, list) else []
            for k, v in (doc.get("net") or {}).items():
                contract, _, token = str(k).partition("|")
                if is_contract(contract) and _TOKEN_RE.fullmatch(token):
                    st.net[k] = int(v)
            for k, m in (doc.get("meta") or {}).items():
                if k in st.net and isinstance(m, list) and len(m) == 3 and safe_decimals(m[2]) is not None:
                    st.meta[k] = [clean_text(m[0], 60), clean_text(m[1], 20), int(m[2])]
            for k, m in (doc.get("stats") or {}).items():
                if k in st.net and isinstance(m, list) and len(m) == 4 and isinstance(m[3], list):
                    st.stats[k] = [int(m[0]), int(m[1]), int(m[2]), [clean_text(f, 40) for f in m[3][:3]]]
            for g, v in (doc.get("pending") or {}).items():
                ts, deps = v
                clean = []
                for c, t, r, tk, d, n, f in deps:
                    if is_contract(c) and _TOKEN_RE.fullmatch(t) and safe_decimals(d) is not None:
                        clean.append((c, t, int(r), clean_text(tk, 20), int(d), clean_text(n, 60), clean_text(f, 40)))
                if isinstance(g, str) and isinstance(ts, int) and clean:
                    st.pending[g] = [ts, clean]
            for g, v in (doc.get("calls") or {}).items():
                ts, contract, had_out, name = v
                if isinstance(g, str) and isinstance(ts, int) and is_contract(contract):
                    st.calls[g] = [ts, contract, bool(had_out), clean_text(name, 60)]
            for g, ts in (doc.get("returned") or {}).items():
                if isinstance(g, str) and isinstance(ts, int) and not isinstance(ts, bool):
                    st.recent_returned[g] = ts
            if "cnames" not in doc:                  # ledger saved before contract names were kept
                for k, m in st.meta.items():
                    if m[0] and k.partition("|")[0] not in st.cnames:
                        st.cnames[k.partition("|")[0]] = m[0]
            for c, n in (doc.get("cnames") or {}).items():
                if is_contract(c) and isinstance(n, str) and n and len(st.cnames) < MAX_CNAMES:
                    st.cnames[c] = clean_text(n, 60)
            win = doc.get("win", START_WINDOW)
            st.win = win if isinstance(win, int) and not isinstance(win, bool) and 1 <= win <= MAX_WINDOW else START_WINDOW
            st.done = bool(doc.get("done"))
            seen = doc.get("seen", 0)
            st.seen = seen if isinstance(seen, int) and not isinstance(seen, bool) and seen >= 0 else 0
            total = doc.get("total")
            st.total = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None
        except (TypeError, ValueError, OverflowError, AttributeError):
            return None
        return st

    # --------------------------------------------------------------- ledger
    @staticmethod
    def _valid(toks: list[dict]) -> list[tuple]:
        out = []
        for t in toks:
            if t.get("type") != _FUNGIBLE:
                continue
            token = t.get("token")
            amount = _raw_amount(t.get("value"))
            decimals = safe_decimals(t.get("decimals"))
            if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token) or amount is None or decimals is None:
                continue
            out.append((token, amount, decimals, clean_text(t.get("ticker"), 20) or token.split("-")[0]))
        return out

    def integrate(self, address: str, items: list[dict], watermark: int) -> None:
        """Add `items` (oldest first) to the ledger. `watermark` is how far in time
        the data read so far is complete: deposits older than watermark - SETTLE
        are final, younger ones keep waiting for a possible answer."""
        for i in items:
            self.seen += 1
            toks, dest = _transfers(i)
            sender, grp, ts = i.get("sender"), _group(i), _ts(i) or 0
            valid = self._valid(toks)
            if sender == address:
                if is_contract(dest):                    # the wallet called / paid a contract
                    a = i.get("receiverAssets")
                    name = clean_text(a.get("name"), 60) if isinstance(a, dict) else ""
                    if name:
                        self.cnames[dest] = name
                    self.calls[grp] = [ts, dest, bool(valid), name]
                    if valid and grp not in self.recent_returned:
                        func = clean_text(i.get("function"), 40)
                        entry = self.pending.setdefault(grp, [ts, []])
                        for token, amount, decimals, ticker in valid:
                            entry[1].append((dest, token, amount, ticker, decimals, name, func))
                continue
            if not toks and is_contract(sender) and i.get("receiver") == address and _native_back(i):
                # native EGLD paid back by a contract (unwrap, swap to EGLD...): an exchange
                info = self.calls.get(grp)
                if info and info[2]:
                    self.recent_returned[grp] = ts
                    self.pending.pop(grp, None)
                continue
            if not (toks and dest == address and is_contract(sender)):
                continue
            info = self.calls.get(grp)
            if info and info[2]:                         # tokens went out in this group: exchange / receipt
                self.recent_returned[grp] = ts
                self.pending.pop(grp, None)
                continue
            if not info:                                 # not the wallet's own call
                self.recent_returned[grp] = ts
                self.pending.pop(grp, None)
            holder = info[1] if info else sender
            a = i.get("senderAssets")
            payer = clean_text(a.get("name"), 60) if isinstance(a, dict) else ""
            if payer:
                self.cnames[sender] = payer
            # the line belongs to `holder` (the contract the wallet called): the payer's name is
            # only its own when the payer IS the holder
            name = (info[3] or self.cnames.get(holder, "")) if info else payer
            for token, amount, decimals, ticker in valid:
                self._add(holder, token, -amount, ticker, decimals, name)
                self.stats.setdefault(f"{holder}|{token}", [0, 0, 0, []])[1] += 1
        self.settle(watermark)

    def _add(self, contract, token, raw, ticker, decimals, name) -> None:
        key = f"{contract}|{token}"
        self.net[key] = self.net.get(key, 0) + raw
        old = self.meta.get(key)
        self.meta[key] = [name or (old[0] if old else ""), ticker, decimals]

    def settle(self, watermark: int) -> None:
        for grp in [g for g, (ts, _) in self.pending.items() if ts <= watermark - SETTLE_SECONDS]:
            ts, deps = self.pending.pop(grp)
            for contract, token, raw, ticker, decimals, name, func in deps:
                self._add(contract, token, raw, ticker, decimals, name)
                st = self.stats.setdefault(f"{contract}|{token}", [0, 0, 0, []])
                st[0] += 1
                st[2] = max(st[2], ts)
                if func and func not in st[3] and len(st[3]) < 3:
                    st[3].append(func)
        for book in (self.recent_returned,):
            for grp in [g for g, ts in book.items() if ts < watermark - KEEP_GROUPS]:
                del book[grp]
        for grp in [g for g, v in self.calls.items() if v[0] < watermark - KEEP_GROUPS]:
            del self.calls[grp]

    def describe(self, token_filter: str | None = None) -> list[str]:
        """One line per (contract, token) of the ledger, for diagnosis."""
        rows = []
        groups = self.group_totals()
        gpos: dict[tuple[str, str], int] = {}
        for k2, r2 in self.net.items():
            p2 = self._protocol(k2)
            if p2 and r2 > 0:
                gpos[(p2, k2.partition("|")[2])] = gpos.get((p2, k2.partition("|")[2]), 0) + r2
        for key, raw in self.net.items():
            contract, _, token = key.partition("|")
            name, ticker, decimals = self.meta.get(key, ["", token.split("-")[0], 0])
            if token_filter and token_filter.lower() not in f"{ticker}{token}".lower():
                continue
            nd, nr, last, funcs = self.stats.get(key, [0, 0, 0, []])
            when = time.strftime("%Y-%m-%d", time.gmtime(last)) if last else "-"
            if _ignored(self.address, contract):
                tag = " [ignored]"
            elif raw > 0 and sum(1 for k, r in self.net.items() if r != 0 and k.startswith(contract + "|")) > MAX_TOKENS_PER_CONTRACT \
                    and not _matches(contract, _addr_list("MVX_FLOWS_TRUST")):
                tag = " [router-like: excluded]"
            else:
                tag = ""
            proto = self._protocol(key)
            if proto and raw > 0 and self._closed(proto, token, groups, gpos):
                tag = (tag + " [protocol net <= 0: withdrawn elsewhere]").strip()
                tag = " " + tag
            rows.append((abs(raw / 10 ** decimals), f"{tag.strip() + ' ' if tag else ''}{contract}  {name or '-'}  {ticker}  net={raw / 10 ** decimals:,.4f}"
                         f"  deposits={nd} withdrawals={nr} last_deposit={when}  functions={','.join(funcs) or '-'}"))
        rows.sort(key=lambda r: -r[0])
        pend = sum(len(v[1]) for v in self.pending.values())
        lines = [r[1] for r in rows]
        if pend:
            lines.append(f"({pend} deposit(s) still waiting for a possible answer)")
        return lines

    def _protocol(self, key: str) -> str:
        """Protocol a contract belongs to: the public name before ':' ("Hatom: Controller" ->
        "hatom"), '' when the contract has no such name."""
        contract = key.partition("|")[0]
        name = self.cnames.get(contract) or _names.get(contract, "")
        return name.split(":", 1)[0].strip().lower() if ":" in name else ""

    def group_totals(self) -> dict[tuple[str, str], int]:
        """Net amount per (protocol, token) over all the contracts of the protocol."""
        out: dict[tuple[str, str], int] = {}
        if os.environ.get("MVX_FLOWS_GROUP", "1") == "0":
            return out
        for key, raw in self.net.items():
            proto = self._protocol(key)
            if proto:
                tk = (proto, key.partition("|")[2])
                out[tk] = out.get(tk, 0) + raw
        return out

    def _closed(self, proto: str, token: str, totals: dict, positives: dict) -> bool:
        """The protocol holds nothing more of this token, and what left is of the SAME SIZE as what
        went in (a market in, its controller out). A protocol whose other modules moved far more
        than the position (rewards, swap outputs: a lock next to a staking module) proves nothing."""
        total = totals.get((proto, token))
        if total is None or total > 0:
            return False
        return -total <= GROUP_CLOSE_RATIO * positives.get((proto, token), 0)

    def positions(self) -> list[Position]:
        """A deposit and its withdrawal often go through different contracts of ONE protocol
        (e.g. a lending market in, its controller out). When the net over the whole protocol
        is zero or negative for a token, nothing is left there and no position is reported.
        A positive protocol net never REDUCES a position: another module paying rewards in the
        same token (e.g. a staking module next to a locker) is not a withdrawal of the lock."""
        out = []
        trust = _addr_list("MVX_FLOWS_TRUST")
        breadth: dict[str, int] = {}
        for key, raw in self.net.items():
            if raw != 0:
                c = key.partition("|")[0]
                breadth[c] = breadth.get(c, 0) + 1
        totals = self.group_totals()
        positives: dict[tuple[str, str], int] = {}
        for key, raw in self.net.items():
            proto = self._protocol(key)
            if proto and raw > 0:
                tk = (proto, key.partition("|")[2])
                positives[tk] = positives.get(tk, 0) + raw
        for key, raw in sorted(self.net.items(), key=lambda kv: (-kv[1], kv[0])):   # ties: fixed order, so the result never depends on the order the history was read
            if raw <= 0 or key not in self.meta:
                continue
            contract, _, token = key.partition("|")
            if _ignored(self.address, contract):
                continue
            if breadth.get(contract, 0) > MAX_TOKENS_PER_CONTRACT and not _matches(contract, trust):
                continue
            proto = self._protocol(key)
            if proto and self._closed(proto, token, totals, positives):
                continue          # withdrawn through another module of the same protocol
            name, ticker, decimals = self.meta[key]
            out.append(Position(contract, name, token, ticker, raw / 10 ** decimals))
        out.sort(key=lambda p: -p.amount)
        return out[:MAX_POSITIONS]


def compute_engaged(address: str, items: list) -> list[Position]:
    """Pure ledger over a complete list of API transfer entries (any order)."""
    entries = [i for i in items if isinstance(i, dict) and _ts(i) is not None and i.get("status", "success") == "success"]
    entries.sort(key=_order)
    st = State()
    st.address = address
    st.integrate(address, entries, 2 ** 62)
    return st.positions()


# ------------------------------------------------------------- persistence

_states: dict[str, State] = {}
_names: dict[str, str] = {}
_path: str | None = None
_dirty = False
_lock = threading.Lock()
_use_fields = True


def set_cache_path(path: str | None) -> None:
    """Keep the ledgers in `path` (None = memory only)."""
    global _path, _dirty
    with _lock:
        if path == _path:
            return
        _path = path
        _states.clear()
        _dirty = False
        if path:
            try:
                if os.path.getsize(path) <= MAX_CACHE_BYTES:
                    with open(path, encoding="utf-8") as f:
                        doc = json.load(f)
                    # v4: native EGLD paybacks are understood (a missing nonce counts as 0); older ledgers are ignored (re-read), names/hints stay valid
                    wallets = doc.get("wallets") if isinstance(doc, dict) and doc.get("v") == 4 else None
                    for k, h in (doc.get("hints") or {}).items() if isinstance(doc, dict) and isinstance(doc.get("hints"), dict) else ():
                        try:
                            if len(_hints) < 400 and isinstance(k, str) and len(k) < 200 and len(h) == 2:
                                _hints[k] = [[[str(d[0])[:64], clean_text(d[1], 40), int(d[2])] for d in h[0][:3]],
                                             [clean_text(f, 40) for f in h[1][:4]]]
                        except (TypeError, ValueError, IndexError):
                            pass
                    saved = doc.get("names") if isinstance(doc, dict) else None
                    for c, n in (saved.items() if isinstance(saved, dict) else ()):
                        if len(_names) < 500 and is_contract(c) and isinstance(n, str) and n:
                            _names[c] = clean_text(n, 60)
                    for addr, d in (wallets.items() if isinstance(wallets, dict) else ()):
                        if len(_states) < MAX_ADDRESSES and isinstance(addr, str) and _ADDR_RE.fullmatch(addr):
                            st = State.from_json(d)
                            if st:
                                _states[addr] = st
            except (OSError, ValueError, RecursionError):
                pass


def save_cache() -> None:
    global _dirty
    with _lock:
        if not _path or not _dirty:
            return
        blob = json.dumps({"v": 4, "wallets": {a: s.to_json() for a, s in _states.items()},
                           "names": {c: n for c, n in _names.items() if n},
                           "hints": dict(_hints)})
        path, _dirty = _path, False
    if len(blob) > MAX_CACHE_BYTES:
        return
    try:
        write_private(path, blob)
    except OSError:
        pass


def clear_cache() -> None:
    global _dirty
    with _lock:
        _states.clear()
        _names.clear()
        _hints.clear()
        _dirty = False


# ----------------------------------------------------------------- reading

def _fetch(get: Callable, params: dict):
    """One /transfers call; asks for fewer fields when the API supports it."""
    global _use_fields
    if _use_fields:
        try:
            return get("/transfers", dict(params, fields=_FIELDS))
        except Exception as exc:
            if _is_busy(exc):
                raise                 # overloaded API: not a reason to drop `fields`, the caller waits
            page = get("/transfers", params)
            _use_fields = False       # the plain call works, the filtered one did not
            return page
    return get("/transfers", params)


def _window(get: Callable, after: int, before: int, size: int):
    """Transfers stamped in [after, before] (never an offset: the API's offset
    pagination can skip or reorder entries, date filters are reliable)."""
    return _fetch(get, {"order": "asc", "size": size, "from": 0, "status": "success",
                        "after": after - 1, "before": before + 1})


def engaged_positions(address: str, get: Callable[[str, dict], object], now: float | None = None) -> Result:
    """Read the next slice of the wallet's history and return the current estimate.

    The history is read by DATE WINDOWS: a window is accepted only when the API
    returned fewer than a full page (so nothing was cut), otherwise it is halved.
    At most FLOW_MAX_PAGES calls per refresh. `get(suffix, params)` returns the
    decoded JSON of `/accounts/{address}{suffix}`. Network errors propagate.
    """
    global _dirty
    now_s = int(now if now is not None else time.time())
    horizon = now_s - LAG_SECONDS
    with _lock:
        st = _states.get(address)
        if st is None:
            st = _states[address] = State()
        st.address = address
    if not st.done:
        try:
            total = get("/transfers/count", {})
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                st.total = total
        except Exception:
            pass

    calls = 0
    error = ""
    if st.cursor == 0:
        # where the history starts (one call), instead of walking empty years
        first = get("/transfers", {"order": "asc", "size": 1, "from": 0, "status": "success"})
        calls += 1
        if not isinstance(first, list):
            return Result(st.positions(), st.done, None, "unexpected answer from the transfers API")
        starts = [_ts(r) for r in first if isinstance(r, dict) and _ts(r) is not None]
        with _lock:
            if starts:
                st.cursor = max(1, min(min(starts) - 1, horizon))
            else:
                st.cursor, st.done = max(1, horizon), True      # nothing at all
            _dirty = True

    while calls < FLOW_MAX_PAGES and st.cursor < horizon:
        end = min(st.cursor + st.win, horizon)
        rows = _window(get, st.cursor, end, FLOW_PAGE_SIZE)
        calls += 1
        if not isinstance(rows, list):
            return Result(st.positions(), st.done, None, "unexpected answer from the transfers API")
        batch = [r for r in rows if isinstance(r, dict) and _ts(r) is not None]
        stamps = [_ts(r) for r in batch]
        if stamps and (min(stamps) < st.cursor - 1 or max(stamps) > end + 1):
            error = "transfers API ignores the date filters: estimate disabled"
            break
        full = len(rows) >= FLOW_PAGE_SIZE
        if full and end - st.cursor > 1:
            st.win = max(1, (end - st.cursor) // 2)        # too dense: a smaller window
            continue
        if full:
            # one second holds more than a page: only here read by offset (rare)
            offset = FLOW_PAGE_SIZE
            while calls < FLOW_MAX_PAGES:
                more = _fetch(get, {"order": "asc", "size": FLOW_PAGE_SIZE, "from": offset, "status": "success",
                                    "after": st.cursor - 1, "before": end + 1})
                calls += 1
                if not isinstance(more, list):
                    break
                batch += [r for r in more if isinstance(r, dict) and _ts(r) is not None]
                if len(more) < FLOW_PAGE_SIZE:
                    break
                offset += FLOW_PAGE_SIZE
            else:
                break                                      # out of calls: retry next refresh
        edge = set(st.edge)
        window_items, seen_uids = [], set()
        for r in sorted(batch, key=_order):
            t = _ts(r)
            if t < st.cursor or t > end or r.get("status", "success") != "success":
                continue
            uid = _uid(r)
            if uid in seen_uids or (t == st.cursor and uid in edge):
                continue
            seen_uids.add(uid)
            window_items.append(r)
        with _lock:
            st.integrate(address, window_items, end)
            st.edge = [_uid(r) for r in window_items if _ts(r) == end][:200] if end > st.cursor else \
                list(edge | {_uid(r) for r in window_items if _ts(r) == end})[:200]
            st.cursor = max(st.cursor, end)
            n = len(batch)
            if n < FLOW_PAGE_SIZE // 4:
                st.win = min(MAX_WINDOW, st.win * 4)
            elif n < FLOW_PAGE_SIZE // 2:
                st.win = min(MAX_WINDOW, st.win * 2)
            elif n > FLOW_PAGE_SIZE * 2 // 3:
                st.win = max(1, st.win // 2)
            _dirty = True

    with _lock:
        if not error and st.cursor >= horizon:
            st.done = True
        _dirty = True
    percent = None
    if not st.done and st.total:
        percent = max(0, min(99, int(100 * st.seen / st.total)))
    return Result(st.positions(), st.done, percent, error)


def progress(address: str) -> tuple[int, int | None, bool]:
    """(transfers integrated so far, total transfers when known, history fully read)."""
    with _lock:
        st = _states.get(address)
        return (st.seen, st.total, st.done) if st else (0, None, False)


def _is_busy(exc: Exception) -> bool:
    """API overloaded or rate-limiting (HTTP 429 / 5xx / timeouts): worth waiting for."""
    return bool(re.search(r"HTTP (429|5\d\d)\b|timed out|timeout|Connection", str(exc), re.I))


def catch_up(address: str, get: Callable[[str, dict], object],
             on_progress: Callable[[Result], None] | None = None,
             max_rounds: int = 2000, now: float | None = None,
             on_wait: Callable[[float, Exception], None] | None = None,
             sleep: Callable[[float], None] = time.sleep) -> Result:
    """Read the WHOLE remaining history in one go (CLI): engaged_positions() in a loop
    until the history is complete. The ledger is saved after every round, so an
    interruption (Ctrl+C, network error) loses nothing.

    When the API pushes back (HTTP 429 / 5xx / timeout) the loop waits (5 s, doubling,
    at most 60 s) and tries again, and keeps a small pause between rounds afterwards;
    after 8 failures in a row the error propagates. Other errors propagate at once.
    Stops on a result error, when the API stops advancing (6 rounds without
    progress) or after `max_rounds` rounds."""
    res = None
    stuck = failures = 0
    pause = 0.0
    for _ in range(max_rounds):
        with _lock:
            st = _states.get(address)
            before = st.cursor if st else 0
        try:
            res = engaged_positions(address, get, now)
        except Exception as exc:
            save_cache()
            if not _is_busy(exc) or failures >= 8:
                raise
            failures += 1
            delay = min(60.0, 5.0 * 2 ** (failures - 1))
            pause = min(5.0, max(pause, 1.0) * 2)
            if on_wait:
                on_wait(delay, exc)
            sleep(delay)
            continue
        failures = 0
        pause = max(0.0, pause - 0.5)
        save_cache()
        with _lock:
            after = _states[address].cursor if address in _states else 0
        if on_progress:
            on_progress(res)
        if res.done or res.error:
            break
        stuck = stuck + 1 if after <= before else 0
        if stuck >= 6:
            break
        if pause:
            sleep(pause)
    return res


def contract_name(contract: str, get_account: Callable[[str], object],
                  get_transfers: Callable[[str], object] | None = None) -> str:
    """Public name of a contract (cached, saved with the ledger); '' when unknown.

    The account route usually has no name for smart contracts; the transfers that
    involve the contract do carry it (senderAssets / receiverAssets), so one
    transfer of the contract is read as a fallback (`get_transfers(contract)`).
    """
    global _dirty
    if contract in _names:
        return _names[contract]
    name = ""
    try:
        try:
            doc = get_account(contract)
        except Exception:
            doc = None
        assets = doc.get("assets") if isinstance(doc, dict) else None
        name = clean_text(assets.get("name"), 60) if isinstance(assets, dict) else ""
        if not name and get_transfers is not None:
            rows = get_transfers(contract)
            for r in (rows if isinstance(rows, list) else ()):
                if not isinstance(r, dict):
                    continue
                for who, key in (("sender", "senderAssets"), ("receiver", "receiverAssets")):
                    a = r.get(key)
                    if r.get(who) == contract and isinstance(a, dict) and a.get("name"):
                        name = clean_text(a.get("name"), 60)
                        break
                if name:
                    break
    except Exception:
        return ""
    if len(_names) > 500:
        _names.clear()
    _names[contract] = name
    if name:
        _dirty = True
    return name


_hints: dict[str, list] = {}      # "wallet|contract|token" -> [[[txhash, function, ts], ...], [exit functions]]
HINTS_PER_REFRESH = 20


def position_hints(address: str, pos: "Position", get_mine: Callable[[str], object],
                   get_contract: Callable[[str], object], budget: list | None = None) -> list:
    """How the engaged tokens were sent and which functions of the contract exist to get
    them back: [[[txhash, function, ts], ...] (the wallet's latest deposits of this token),
    [function names called on the contract without sending tokens (unlock, withdraw...)]].

    Two API reads per position, then kept with the ledger. `budget` is a one-item list
    counting the reads still allowed in this refresh (hints already known cost nothing).
    """
    global _dirty
    key = f"{address}|{pos.contract}|{pos.token}"
    if key in _hints:
        return _hints[key]
    if budget is not None and budget[0] <= 0:
        return [[], []]
    if budget is not None:
        budget[0] -= 1
    deposits, exits = [], []
    try:
        rows = get_mine(pos.contract)
        for r in (rows if isinstance(rows, list) else ()):
            if not isinstance(r, dict) or r.get("sender") != address or r.get("type") == "SmartContractResult":
                continue
            toks, dest = _transfers(r)
            if dest == pos.contract and any(t.get("token") == pos.token for t in toks):
                h = r.get("txHash") or r.get("originalTxHash")
                if isinstance(h, str) and re.fullmatch(r"[0-9a-fA-F]{64}", h) and len(deposits) < 3:
                    deposits.append([h, clean_text(r.get("function"), 40), _ts(r) or 0])
        rows = get_contract(pos.contract)
        counts: dict[str, int] = {}
        mine = {d[1] for d in deposits}
        for r in (rows if isinstance(rows, list) else ()):
            if not isinstance(r, dict) or r.get("type") == "SmartContractResult" or r.get("receiver") != pos.contract:
                continue
            f = clean_text(r.get("function"), 40)
            if f and f not in mine and not _transfers(r)[0] and _FUNC_RE.fullmatch(f):
                counts[f] = counts.get(f, 0) + 1
        exits = [f for f, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:4]]
    except Exception:
        return [[], []]
    with _lock:
        if len(_hints) > 400:
            _hints.clear()
        _hints[key] = [deposits, exits]
        _dirty = True
    return _hints[key]


def describe_hints(address: str) -> list[str]:
    """Full deposit transactions and exit functions known for the wallet's positions (diagnosis)."""
    out = []
    for key, (deps, exits) in list(_hints.items()):
        if not key.startswith(address + "|"):
            continue
        _, contract, token = key.split("|", 2)
        out.append(f"[{token}] {contract}")
        for h, f, ts in deps:
            out.append(f"    deposit {time.strftime('%Y-%m-%d', time.gmtime(ts))}  {f or '?'}  https://explorer.multiversx.com/transactions/{h}")
        out.append(f"    functions called on the contract without sending tokens: {', '.join(exits) or '-'}")
    return out


def describe(address: str, token_filter: str | None = None) -> list[str]:
    """Ledger lines of a wallet (diagnosis); empty when it was never read."""
    with _lock:
        st = _states.get(address)
    return st.describe(token_filter) if st else []
