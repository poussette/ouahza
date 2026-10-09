"""USD / EUR valuation for wallet balances.

Native coins and ERC-20/SPL tokens are priced via the free CoinGecko API
(no key required). MultiversX ESDT/MetaESDT tokens aren't reliably indexed
by CoinGecko (they use identifiers like "MEX-455c57", not EVM-style
contract addresses), so they're priced instead through MultiversX's own
public API (the same api.multiversx.com host the MultiversX provider
already talks to), which prices tokens from xExchange DEX liquidity:

  1. `/mex-tokens` : a broad, cheap first pass covering the "MEX economics"
     tokens (those with a direct MEX/WEGLD pair).
  2. `/tokens?identifiers=...` : a second, targeted pass for any token the
     wallets actually hold that step 1 didn't price -- this endpoint prices
     any token with known DEX liquidity, not just the MEX-economics set, so
     it catches tokens like a wrapped asset (e.g. WTAO) that trades on
     xExchange but isn't part of that narrower set.

A token with no xExchange trading pair at all (e.g. an obscure or illiquid
ESDT) simply stays unpriced, same as for the other chains.

To add pricing support for a new chain, add its native coin to
NATIVE_COINGECKO_IDS and, if CoinGecko tracks its tokens by contract
address, its platform id to TOKEN_PLATFORM_IDS.
"""

from __future__ import annotations

__version__ = "0.1.26"






import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from providers.base import TokenBalance, WalletBalance
from providers.net import request_json
from providers import lp as lp_module
from providers.safe import MAX_POSITION_USD, safe_decimals, safe_price

COINGECKO_API = "https://api.coingecko.com/api/v3"
MULTIVERSX_API = "https://api.multiversx.com"
FX_API = "https://api.frankfurter.app"  # free, no key, ECB-sourced FX rates

#: hard stop for paginated endpoints (hostile/buggy API repeating full pages).
MAX_PAGES = 20
#: a USD->EUR rate outside this range is an API glitch, not a real FX rate.
FX_MIN, FX_MAX = 0.05, 20.0

# ------------------------------------------------------------------ price cache
# Prices barely move within a couple of minutes, so a second refresh right
# after the first one reuses them instead of calling the (rate-limited)
# public APIs again. Only successful answers are cached ("not priced" answers
# too, so spam tokens are not asked again and again); a failed request is
# never cached. Each kind of request is single-flight: a background prefetch
# and the main run never fire the same call twice.
PRICE_TTL = 120.0        # CoinGecko native / token prices
MEX_TTL = 180.0          # xExchange prices (/mex-tokens, /tokens)
FX_TTL = 12 * 3600.0     # USD -> EUR (ECB rate, published once a day)

_MISS = object()
_cache: dict = {}
_cache_lock = threading.Lock()
_kind_locks: dict[str, threading.Lock] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_get(key, ttl: float):
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    return _MISS


def _cache_put(key, value) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


def _kind_lock(kind: str) -> threading.Lock:
    with _cache_lock:
        return _kind_locks.setdefault(kind, threading.Lock())


#: seconds spent per stage in the last apply_pricing run (shown by the app).
LAST_TIMINGS: dict[str, float] = {}


def _clean_price_map(vals) -> dict[str, float]:
    """{"usd": x, "eur": y} keeping only finite, positive, sane prices."""
    out: dict[str, float] = {}
    if isinstance(vals, dict):
        for cur in ("usd", "eur"):
            p = safe_price(vals.get(cur))
            if p is not None:
                out[cur] = p
    return out


# chain_id (as used by our providers) -> CoinGecko coin id, for the native coin.
NATIVE_COINGECKO_IDS: dict[str, str] = {
    "bitcoin": "bitcoin",
    "ethereum": "ethereum",
    "solana": "solana",
    "multiversx": "elrond-erd-2",  # CoinGecko kept the old "elrond" id for EGLD
}

# chain_id -> CoinGecko "asset platform" id, for looking up token prices by
# contract address. Chains not listed here just don't get token pricing
# (their tokens keep usd_value/eur_value = None).
TOKEN_PLATFORM_IDS: dict[str, str] = {
    "ethereum": "ethereum",
    "solana": "solana",
    # MultiversX ESDT tokens aren't reliably indexed by CoinGecko's public
    # contract-lookup endpoint (they use identifiers like "WEGLD-bd4d79",
    # not EVM-style contract addresses), so they're left unpriced for now.
}

# Asset types whose amount is denominated in the chain's native coin (i.e.
# staking positions), so they're priced with the native coin price rather
# than a per-contract token price.
NATIVE_DENOMINATED_TYPES = {
    "staked",
    "stake-withdrawable",
    "beacon-stake",
    "delegation",
    "delegation-rewards",
    "delegation-legacy",
    "delegation-unbondable",
    "delegation-unbonding",
    "delegation-legacy-unbonding",
    "validator-stake",
}

# Asset types that are individually priced by contract address.
CONTRACT_PRICED_TYPES = {"token", "esdt", "meta-esdt"}


def _fetch_native_prices(chain_ids: set[str]) -> dict[str, dict[str, float]]:
    """Return {chain_id: {"usd": ..., "eur": ...}} for the given chains."""
    gecko_ids = {
        NATIVE_COINGECKO_IDS[c] for c in chain_ids if c in NATIVE_COINGECKO_IDS
    }
    if not gecko_ids:
        return {}
    fresh: dict[str, dict[str, float]] = {}
    with _kind_lock("native"):
        missing = {g for g in gecko_ids if _cache_get(("native", g), PRICE_TTL) is _MISS}
        if missing:
            try:
                data = request_json(
                    "GET",
                    f"{COINGECKO_API}/simple/price",
                    params={"ids": ",".join(sorted(missing)), "vs_currencies": "usd,eur"},
                )
                if isinstance(data, dict):
                    for g in missing:
                        if g in data:
                            fresh[g] = _clean_price_map(data[g])
                            if fresh[g]:
                                _cache_put(("native", g), fresh[g])
            except requests.RequestException:
                pass
        result: dict[str, dict[str, float]] = {}
        for chain_id, gecko_id in NATIVE_COINGECKO_IDS.items():
            if gecko_id not in gecko_ids:
                continue
            if gecko_id in fresh:
                result[chain_id] = fresh[gecko_id]
            else:
                hit = _cache_get(("native", gecko_id), PRICE_TTL)
                if hit is not _MISS:
                    result[chain_id] = hit
        return result


def _fetch_token_prices(
    chain_id: str, contracts: set[str]
) -> dict[str, dict[str, float]]:
    """Return {contract_address_lowercase: {"usd": ..., "eur": ...}}."""
    platform = TOKEN_PLATFORM_IDS.get(chain_id)
    if not platform or not contracts:
        return {}

    prices: dict[str, dict[str, float]] = {}

    def _store(addr: str, clean: dict) -> None:
        # Solana mints are case-sensitive base58: keep the exact key
        # too, in addition to the lowercase one used for EVM.
        prices[addr] = clean
        prices[addr.lower()] = clean

    with _kind_lock("token:" + chain_id):
        todo = []
        for c in contracts:
            hit = _cache_get(("tok", chain_id, c), PRICE_TTL)
            if hit is _MISS:
                todo.append(c)
            elif hit:
                _store(c, hit)
        chunk_size = 50  # keep query strings/URLs reasonably sized
        for i in range(0, len(todo), chunk_size):
            chunk = todo[i : i + chunk_size]
            try:
                data = request_json(
                    "GET",
                    f"{COINGECKO_API}/simple/token_price/{platform}",
                    params={
                        "contract_addresses": ",".join(chunk),
                        "vs_currencies": "usd,eur",
                    },
                )
                if not isinstance(data, dict):
                    continue
            except requests.RequestException:
                continue
            for addr, vals in data.items():
                if isinstance(addr, str):
                    _store(addr, _clean_price_map(vals))
            for c in chunk:  # also remember "not priced", so it is not re-asked
                got = data.get(c) if c in data else data.get(c.lower())
                _cache_put(("tok", chain_id, c), _clean_price_map(got) if got is not None else {})
    return prices


def _fetch_usd_eur_rate() -> float | None:
    """USD -> EUR conversion rate, used to convert xExchange's USD-only
    token prices into EUR (MultiversX's /mex-tokens only gives USD)."""
    with _kind_lock("fx"):
        hit = _cache_get(("fx",), FX_TTL)
        if hit is not _MISS:
            return hit
        rate = _fetch_usd_eur_rate_raw()
        if rate is not None:
            _cache_put(("fx",), rate)
        return rate


def _fetch_usd_eur_rate_raw() -> float | None:
    try:
        data = request_json("GET", f"{FX_API}/latest", params={"from": "USD", "to": "EUR"})
        rate = safe_price(data["rates"]["EUR"])
    except (requests.RequestException, KeyError, TypeError):
        return None
    return rate if rate is not None and FX_MIN <= rate <= FX_MAX else None


def _fetch_mex_tokens_prices() -> dict[str, float]:
    """Return {esdt_identifier: usd_price} for the "MEX economics" token
    set (those with a direct MEX/WEGLD pair), via MultiversX's /mex-tokens.
    Broad and cheap, but doesn't cover every token traded on xExchange."""
    with _kind_lock("mex"):
        hit = _cache_get(("mex",), MEX_TTL)
        if hit is not _MISS:
            return dict(hit)
        prices = _fetch_mex_tokens_prices_raw()
        if prices:
            _cache_put(("mex",), dict(prices))
        return prices


def _fetch_mex_tokens_prices_raw() -> dict[str, float]:
    prices: dict[str, float] = {}
    page_size = 100
    for page in range(MAX_PAGES):
        try:
            batch = request_json(
                "GET",
                f"{MULTIVERSX_API}/mex-tokens",
                params={"from": page * page_size, "size": page_size},
            )
        except requests.RequestException:
            break
        if not isinstance(batch, list) or not batch:
            break
        for tok in batch:
            if not isinstance(tok, dict):
                continue
            identifier = tok.get("id") or tok.get("identifier")
            price = safe_price(tok.get("price"))
            if isinstance(identifier, str) and price is not None:
                prices[identifier] = price
        if len(batch) < page_size:
            break
    return prices


def _fetch_token_info(identifiers: set[str]) -> dict[str, dict]:
    """{esdt_identifier: {"price": usd|None, "decimals": int|None}} via
    MultiversX's general /tokens endpoint (prices any token with known DEX
    liquidity, not just the narrower "MEX economics" set)."""
    if not identifiers:
        return {}
    info: dict[str, dict] = {}
    with _kind_lock("tokinfo"):
        todo = []
        for ident in sorted(identifiers):
            hit = _cache_get(("tokinfo", ident), MEX_TTL)
            if hit is _MISS:
                todo.append(ident)
            elif hit is not None:      # None = "the API does not know it"
                info[ident] = dict(hit)
        chunk_size = 50
        for i in range(0, len(todo), chunk_size):
            chunk = todo[i : i + chunk_size]
            try:
                batch = request_json(
                    "GET",
                    f"{MULTIVERSX_API}/tokens",
                    params={"identifiers": ",".join(chunk), "size": len(chunk)},
                )
                if not isinstance(batch, list):
                    continue
            except requests.RequestException:
                continue
            got: dict[str, dict] = {}
            for tok in batch:
                if not isinstance(tok, dict):
                    continue
                identifier = tok.get("identifier")
                if isinstance(identifier, str):
                    got[identifier] = {
                        "price": safe_price(tok.get("price")),
                        "decimals": safe_decimals(tok.get("decimals")),
                    }
            info.update(got)
            for ident in chunk:
                _cache_put(("tokinfo", ident), dict(got[ident]) if ident in got else None)
    return info


def _fetch_token_prices_by_identifier(identifiers: set[str]) -> dict[str, float]:
    """{esdt_identifier: usd_price} for exactly the given identifiers."""
    return {
        k: v["price"] for k, v in _fetch_token_info(identifiers).items() if v["price"] is not None
    }


def _price_token(
    tok: TokenBalance,
    chain: str,
    native_price: dict,
    token_prices: dict[str, dict],
    xexchange_prices: dict[str, float],
    usd_eur_rate: float | None,
) -> None:
    if tok.asset_type in NATIVE_DENOMINATED_TYPES:
        usd = native_price.get("usd")
        eur = native_price.get("eur")
    elif chain == "multiversx" and tok.asset_type in CONTRACT_PRICED_TYPES and tok.contract:
        # MultiversX identifiers aren't lowercase-normalized like EVM/Solana
        # addresses, so look them up as-is.
        usd = xexchange_prices.get(tok.contract)
        eur = usd * usd_eur_rate if usd is not None and usd_eur_rate is not None else None
    elif tok.asset_type in CONTRACT_PRICED_TYPES and tok.contract:
        tp = token_prices.get(tok.contract) or token_prices.get(tok.contract.lower())
        usd = tp.get("usd") if tp else None
        eur = tp.get("eur") if tp else None
    else:
        # NFTs/SFTs etc.: no reliable floor-price source here.
        return

    # A single position worth more than MAX_POSITION_USD is a pricing glitch
    # or a manipulated illiquid pool, not real money: leave it unpriced
    # instead of poisoning every total.
    if usd is not None and tok.amount * usd <= MAX_POSITION_USD:
        tok.usd_value = tok.amount * usd
        if eur is not None and tok.amount * eur <= MAX_POSITION_USD * FX_MAX:
            tok.eur_value = tok.amount * eur


#: human-readable notes about the last apply_pricing run (shown by CLI / app).
LAST_NOTES: list[str] = []


def lp_pricing_enabled() -> bool:
    return os.environ.get("WALLET_LP_PRICING", "1") != "0"


def _price_lp(tok: TokenBalance, lp: dict, usd_eur_rate: float | None) -> None:
    usd = tok.amount * lp["usd"]
    if usd > lp_module.LP_MAX_POSITION_USD:
        return
    if lp.get("supply") and tok.amount > lp["supply"] * 1.001:
        return  # holding more than the whole supply: the metadata is lying
    tok.usd_value = usd
    if usd_eur_rate is not None:
        tok.eur_value = usd * usd_eur_rate
    # make the origin visible: it is derived from pool reserves, not a market price
    tok.name = f"{tok.name} · LP valorisé via {lp['adapter']}"[:300]


#: seconds the xExchange price list waits before being prefetched: the first
#: wave of wallet requests goes to the same host and gets the room first.
#: apply_pricing fetches it itself if it needs it sooner (single-flight).
MEX_PREFETCH_DELAY = 6.0


def prefetch(chain_ids: set[str]) -> None:
    """Warm the price cache in the background (fire and forget) while the
    wallets are still being read: native prices, the FX rate and, when a
    MultiversX wallet is involved, xExchange's price list. apply_pricing then
    finds them ready. Never raises."""
    chain_ids = set(chain_ids)

    def _safe(fn, *args):
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - purely an optimisation
            pass

    def _delayed(delay, fn):
        if delay > 0:
            time.sleep(delay)
        fn()

    pool = ThreadPoolExecutor(max_workers=3)
    pool.submit(_safe, _fetch_native_prices, chain_ids)
    if "multiversx" in chain_ids:
        pool.submit(_safe, _fetch_usd_eur_rate)
        pool.submit(_safe, _delayed, MEX_PREFETCH_DELAY, _fetch_mex_tokens_prices)
    pool.shutdown(wait=False)


def apply_pricing(results: list[WalletBalance], on_progress=None) -> bool:
    """Mutate `results` in place, filling in usd_value/eur_value on every
    native balance and token/staking entry, plus each wallet's totals.

    Returns False when no native price could be fetched at all (CoinGecko
    down / rate-limited): callers must then NOT hide "unpriced" lines, they
    are simply not priced yet."""
    LAST_NOTES.clear()
    LAST_TIMINGS.clear()
    t_start = time.monotonic()
    chain_ids = {w.chain for w in results if not w.error}

    contracts_by_chain: dict[str, set[str]] = {}
    multiversx_identifiers: set[str] = set()
    for w in results:
        for tok in w.tokens:
            if tok.asset_type in CONTRACT_PRICED_TYPES and tok.contract:
                if w.chain == "multiversx":
                    multiversx_identifiers.add(tok.contract)
                else:
                    contracts_by_chain.setdefault(w.chain, set()).add(
                        tok.contract.lower() if w.chain == "ethereum" else tok.contract
                    )

    # The three groups below talk to different hosts (CoinGecko, MultiversX's
    # API, the FX service), each throttled on its own: they run side by side.
    # Whatever prefetch() already fetched is served from the cache.
    def _coingecko():
        native = _fetch_native_prices(chain_ids)
        by_chain = {
            chain: _fetch_token_prices(chain, contracts)
            for chain, contracts in contracts_by_chain.items()
        }
        return native, by_chain

    def _xexchange():
        # MultiversX ESDT/MetaESDT tokens: priced via xExchange (through
        # MultiversX's own API). First a broad/cheap pass over the
        # MEX-economics set, then a targeted pass for whatever's still
        # missing a price.
        prices = _fetch_mex_tokens_prices()
        still_unpriced = multiversx_identifiers - prices.keys()
        if still_unpriced:
            prices.update(_fetch_token_prices_by_identifier(still_unpriced))
        return prices

    xexchange_prices: dict[str, float] = {}
    usd_eur_rate: float | None = None
    lp_prices: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_cg = pool.submit(_coingecko)
        f_mvx = pool.submit(_xexchange) if multiversx_identifiers else None
        f_fx = pool.submit(_fetch_usd_eur_rate) if multiversx_identifiers else None
        native_prices, token_prices_by_chain = f_cg.result()
        if f_mvx is not None:
            xexchange_prices = f_mvx.result()
            usd_eur_rate = f_fx.result()
    LAST_TIMINGS["prix"] = time.monotonic() - t_start
    t_lp = time.monotonic()

    # LP tokens of other DEXs (AshSwap, JEX, OneDex...) have no public price:
    # value them from their pool's reserves, read straight from the contract.
    if multiversx_identifiers and lp_pricing_enabled():
        # most held first, so the per-run limits spare real positions over spam
        held: dict[str, float] = {}
        for w in results:
            for tok in w.tokens:
                if (
                    w.chain == "multiversx"
                    and tok.asset_type in CONTRACT_PRICED_TYPES
                    and tok.contract
                    and tok.contract not in xexchange_prices
                    and lp_module.looks_like_lp(tok.symbol, tok.name)
                ):
                    held[tok.contract] = held.get(tok.contract, 0.0) + tok.amount
        lp_candidates = sorted(held, key=lambda k: -held[k])
        if lp_candidates:
            lp_prices = lp_module.price_lp_tokens(lp_candidates, _fetch_token_info, on_progress)
            st = lp_module.LAST_STATS
            if st.get("valued", 0) < st.get("candidates", 0):
                left = st.get("remaining", 0)
                unknown = st["candidates"] - st.get("valued", 0) - left
                LAST_NOTES.append(
                    f"LP : {st.get('valued', 0)} valorisé(s) sur {st.get('candidates', 0)} détecté(s)"
                    + (f" · {left} à traiter, relancez pour continuer" if left else "")
                    + (f" · {unknown} non reconnu(s)" if unknown > 0 else "")
                )

    LAST_TIMINGS["lp"] = time.monotonic() - t_lp

    for w in results:
        if w.error:
            continue

        native_price = native_prices.get(w.chain, {})
        total_usd = 0.0
        total_eur = 0.0
        has_any_price = False

        if w.native_amount is not None:
            usd = native_price.get("usd")
            eur = native_price.get("eur")
            if usd is not None and w.native_amount * usd <= MAX_POSITION_USD:
                w.native_usd_value = w.native_amount * usd
                total_usd += w.native_usd_value
                has_any_price = True
            if eur is not None and w.native_usd_value is not None:
                w.native_eur_value = w.native_amount * eur
                total_eur += w.native_eur_value

        token_prices = token_prices_by_chain.get(w.chain, {})
        for tok in w.tokens:
            _price_token(
                tok, w.chain, native_price, token_prices, xexchange_prices, usd_eur_rate
            )
            if tok.usd_value is None and w.chain == "multiversx" and tok.contract in lp_prices:
                _price_lp(tok, lp_prices[tok.contract], usd_eur_rate)
            if tok.usd_value is not None:
                total_usd += tok.usd_value
                has_any_price = True
            if tok.eur_value is not None:
                total_eur += tok.eur_value

        if has_any_price:
            w.total_usd = total_usd
            w.total_eur = total_eur

    return not chain_ids or bool(native_prices)
