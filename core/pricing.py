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

__version__ = "0.1.5"






import os

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
    try:
        data = request_json(
            "GET",
            f"{COINGECKO_API}/simple/price",
            params={"ids": ",".join(sorted(gecko_ids)), "vs_currencies": "usd,eur"},
        )
        if not isinstance(data, dict):
            return {}
    except requests.RequestException:
        return {}

    result: dict[str, dict[str, float]] = {}
    for chain_id, gecko_id in NATIVE_COINGECKO_IDS.items():
        if gecko_id in data:
            result[chain_id] = _clean_price_map(data[gecko_id])
    return result


def _fetch_token_prices(
    chain_id: str, contracts: set[str]
) -> dict[str, dict[str, float]]:
    """Return {contract_address_lowercase: {"usd": ..., "eur": ...}}."""
    platform = TOKEN_PLATFORM_IDS.get(chain_id)
    if not platform or not contracts:
        return {}

    prices: dict[str, dict[str, float]] = {}
    contracts_list = list(contracts)
    chunk_size = 50  # keep query strings/URLs reasonably sized
    for i in range(0, len(contracts_list), chunk_size):
        chunk = contracts_list[i : i + chunk_size]
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
                clean = _clean_price_map(vals)
                # Solana mints are case-sensitive base58: keep the exact key
                # too, in addition to the lowercase one used for EVM.
                prices[addr] = clean
                prices[addr.lower()] = clean
    return prices


def _fetch_usd_eur_rate() -> float | None:
    """USD -> EUR conversion rate, used to convert xExchange's USD-only
    token prices into EUR (MultiversX's /mex-tokens only gives USD)."""
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
    ids_list = sorted(identifiers)
    chunk_size = 50
    for i in range(0, len(ids_list), chunk_size):
        chunk = ids_list[i : i + chunk_size]
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
        for tok in batch:
            if not isinstance(tok, dict):
                continue
            identifier = tok.get("identifier")
            if isinstance(identifier, str):
                info[identifier] = {
                    "price": safe_price(tok.get("price")),
                    "decimals": safe_decimals(tok.get("decimals")),
                }
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


def apply_pricing(results: list[WalletBalance], on_progress=None) -> bool:
    """Mutate `results` in place, filling in usd_value/eur_value on every
    native balance and token/staking entry, plus each wallet's totals.

    Returns False when no native price could be fetched at all (CoinGecko
    down / rate-limited): callers must then NOT hide "unpriced" lines, they
    are simply not priced yet."""
    LAST_NOTES.clear()
    chain_ids = {w.chain for w in results if not w.error}
    native_prices = _fetch_native_prices(chain_ids)

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

    token_prices_by_chain = {
        chain: _fetch_token_prices(chain, contracts)
        for chain, contracts in contracts_by_chain.items()
    }

    # MultiversX ESDT/MetaESDT tokens: priced via xExchange (through
    # MultiversX's own API). First a broad/cheap pass over the MEX-economics
    # set, then a targeted pass for whatever's still missing a price.
    xexchange_prices: dict[str, float] = {}
    usd_eur_rate: float | None = None
    lp_prices: dict[str, dict] = {}
    if multiversx_identifiers:
        xexchange_prices = _fetch_mex_tokens_prices()
        still_unpriced = multiversx_identifiers - xexchange_prices.keys()
        if still_unpriced:
            xexchange_prices.update(_fetch_token_prices_by_identifier(still_unpriced))
        usd_eur_rate = _fetch_usd_eur_rate()

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
