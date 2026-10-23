"""Optional data source for MultiversX: the Ecompass data-api (data-api.mvx.fr).

Only used when a "token data-api Ecompass" is configured (app: Config API;
CLI: env WALLET_DATA_API_TOKEN). Without a token this module does nothing and
no request is ever made to that host. The address of the service is fixed
below on purpose: it is not a setting.

With a token, a MultiversX wallet is read with ONE call,
`GET /portfolios/{address}`, instead of the public api.multiversx.com
(account, token pages, prices, LP contracts). The API answers from its own
index: wallet tokens with a USD price, LP tokens backed by a pool, Hatom
deposits/borrows and xExchange farms. If it is not reachable (or answers
nonsense) the caller falls back to the public API. A token the data-api does
not know at all is also read from the public API (see `known_ids`).

What this module does NOT take from data-api, on purpose:
  - staking (delegation, rewards, unbonding, legacy, validator): read from
    the MultiversX API exactly as before, data-api only gives the active stake;
  - NFTs / SFTs: not listed by data-api;
  - the native EGLD price: still CoinGecko, like the other chains.

Valuation policy (everything the API says is untrusted, like any network
data -- see safe.py). A line is valued only if:
  - plain token: it has a price source, at least WALLET_DATA_API_MIN_LIQUIDITY
    USD of liquidity (default 1000) and a price refreshed less than
    WALLET_DATA_API_MAX_AGE seconds ago (default 600). USDC, the quote asset
    of every price, carries no timestamp and is exempt from the age check;
  - LP token: a pool backs it (the API lists it as a DeFi position), its
    price is fresh, the holding does not exceed the whole supply and the
    underlying assets add up to the same value;
  - farm / Hatom line: same rules on the farmed / underlying token.
Anything else is returned unpriced (`price_locked`, so the public prices are
not asked for it either), except an LP the API cannot vouch for: that one is
left to the local pool reader (lp.py), as before.
"""

from __future__ import annotations

__version__ = "0.1.44"

import os
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from .base import TokenBalance
from .net import HttpError, _sleep, request_json
from .safe import MAX_PRICE, clean_text, safe_amount, safe_decimals

#: fixed address of the service (never shown in the app's settings).
DATA_API_URL = "https://data-api.mvx.fr"
ENV_TOKEN = "WALLET_DATA_API_TOKEN"
ENV_MIN_LIQUIDITY = "WALLET_DATA_API_MIN_LIQUIDITY"
ENV_MAX_AGE = "WALLET_DATA_API_MAX_AGE"
MAX_TOKEN_CHARS = 300

DEFAULT_MIN_LIQUIDITY_USD = 1000.0
DEFAULT_MAX_AGE_SECONDS = 600.0
#: a portfolio with hundreds of tokens is a few hundred KB.
MAX_BYTES = 20_000_000
#: price source of USDC itself (it is the quote of every other price).
SELF_QUOTE_SOURCE = ":USDC/USDC"
#: tolerated clock difference between the API host and this device.
FUTURE_SLACK_SECONDS = 300.0
#: holding more than the whole LP supply means the metadata is lying
#: (same 0.1 % margin as lp.py).
SUPPLY_SLACK_NUM, SUPPLY_SLACK_DEN = 1001, 1000
#: LP value from its price vs. from its underlying assets: relative band ...
CROSS_CHECK_RATIO = 0.05
#: ... and the absolute slack (USD) for dust positions.
CROSS_CHECK_DUST = 0.01
MAX_RAW_DIGITS = 80
LP_MAX_UNIT_PRICE = 1e18


# ----------------------------------------------------------------- configuration

def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if low <= value <= high else default


def token() -> str | None:
    """The configured token: printable ASCII without spaces, else None
    (anything else could inject a header)."""
    value = (os.environ.get(ENV_TOKEN) or "").strip()
    if not value or len(value) > MAX_TOKEN_CHARS or not all(33 <= ord(c) < 127 for c in value):
        return None
    return value


def config_error() -> str | None:
    """Why a configured token is ignored."""
    if (os.environ.get(ENV_TOKEN) or "").strip() and token() is None:
        return "token data-api Ecompass ignoré (caractères non valides)"
    return None


def enabled() -> bool:
    """False without a token: then no request is ever sent to the data-api."""
    return token() is not None


@dataclass(frozen=True)
class Policy:
    min_liquidity_usd: float
    max_age_seconds: float

    @classmethod
    def from_env(cls) -> "Policy":
        return cls(
            min_liquidity_usd=_env_float(ENV_MIN_LIQUIDITY, DEFAULT_MIN_LIQUIDITY_USD, 0.0, 1e12),
            max_age_seconds=_env_float(ENV_MAX_AGE, DEFAULT_MAX_AGE_SECONDS, 1.0, 30 * 86400.0),
        )


@dataclass
class DataApiResult:
    native_amount: float
    tokens: list[TokenBalance] = field(default_factory=list)
    #: base identifiers ("TICKER-abcdef") of the wallet holdings the data-api
    #: accounts for, priced or not. The caller reads the public token list
    #: and adds whatever is NOT in here (tokens the data-api does not know).
    known_ids: set[str] = field(default_factory=set)


def base_id(identifier: str) -> str:
    """"EGLDUSDCFL-1a2b3c-0f" -> "EGLDUSDCFL-1a2b3c" (meta-ESDT nonce dropped)."""
    return "-".join(str(identifier).split("-")[:2])


# --------------------------------------------------------------------- fetching

def fetch(address: str, now: float | None = None) -> DataApiResult:
    """Read one wallet. Raises HttpError (a requests.RequestException) on any
    network or format problem, so callers can fall back to the public API."""
    secret = token()
    if secret is None:
        raise HttpError("data-api token not configured")
    url = f"{DATA_API_URL}/portfolios/{quote(address, safe='')}"
    doc = None
    for attempt in range(2):
        doc = request_json(
            "GET", url, headers={"X-Api-Key": secret}, max_bytes=MAX_BYTES
        )
        if doc is not None:
            break
        # The API answers `null` while another request is filling its cache
        # for this very address: one short wait, then ask again.
        if attempt == 0:
            _sleep(1.0)
    return build(doc, time.time() if now is None else now)


# --------------------------------------------------------------------- helpers

def _dicts(value) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _int(value) -> int | None:
    """A (possibly negative) integer sent as a decimal string, else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text or len(text) > MAX_RAW_DIGITS:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _token(entry) -> dict | None:
    """The TokenOut of a PortfolioTokenOut ({"balance", "token", "usd_value"})."""
    inner = entry.get("token") if isinstance(entry, dict) else None
    return inner if isinstance(inner, dict) else None


def _ident(token: dict | None) -> tuple[str, int | None]:
    """(identifier, decimals) of a TokenOut, ("", None) when unusable."""
    inner = token.get("token") if isinstance(token, dict) else None
    if not isinstance(inner, dict):
        return "", None
    ident = clean_text(inner.get("id"), 80)
    return ident, safe_decimals(inner.get("decimals"))


def _units(raw: int, decimals: int) -> float | None:
    """raw / 10**decimals, exact integer division then one float conversion."""
    try:
        return safe_amount(abs(raw) / (10**decimals))
    except (OverflowError, ZeroDivisionError):
        return None


def _symbol(ident: str) -> str:
    return clean_text(ident.split("-", 1)[0], 40) or "?"


def _label(token: dict, ident: str) -> str:
    return clean_text(token.get("custom_name"), 120) or _symbol(ident)


def _price(token: dict, ident: str, policy: Policy, now: float, *, is_lp: bool) -> float | None:
    """The unit USD price if the policy allows valuing this token, else None."""
    # An LP unit costs a lot more than any token (a 18-decimals LP with a raw
    # supply of 10**12 is worth 10**11 USD per unit): looser ceiling, and
    # pricing.py still refuses any single position above MAX_POSITION_USD.
    price = safe_amount(token.get("usd_price"), LP_MAX_UNIT_PRICE if is_lp else MAX_PRICE)
    if not price:
        return None
    source = clean_text(token.get("usdc_tv_symbol"), 80)
    if not source:
        return None            # no market behind it: leftover price of a dead pool
    if source != SELF_QUOTE_SOURCE:
        stamp = token.get("timestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return None
        if stamp <= 0 or now - stamp > policy.max_age_seconds or stamp - now > FUTURE_SLACK_SECONDS:
            return None
    if not is_lp and ident != "EGLD":       # LP tokens report no liquidity of their own
        liquidity = safe_amount(token.get("usd_liquidity"))
        if liquidity is None or liquidity < policy.min_liquidity_usd:
            return None
    return price


def _lp_consistent(usd: float, position: dict) -> bool:
    """The LP's own price and its pool's reserves must tell the same story."""
    underlying = sum(safe_amount(u.get("usd_value")) or 0.0 for u in _dicts(position.get("underlying_assets")))
    return abs(usd - underlying) <= max(CROSS_CHECK_RATIO * max(usd, underlying), CROSS_CHECK_DUST)


def _value_lp(
    tb: TokenBalance, token: dict, ident: str, raw: int, position: dict | None,
    policy: Policy, now: float, via: str,
) -> bool:
    """Fill tb.usd_value from the LP price if every guard passes."""
    price = _price(token, ident, policy, now, is_lp=True)
    if price is None or position is None or not _dicts(position.get("underlying_assets")):
        return False
    supply = _int(token.get("supply"))
    if supply is not None and supply > 0 and raw * SUPPLY_SLACK_DEN > supply * SUPPLY_SLACK_NUM:
        return False
    usd = tb.amount * price
    if not _lp_consistent(usd, position):
        return False
    tb.usd_value = usd
    tb.name = clean_text(f"{tb.name} · LP valorisé via data-api ({via})", 300)
    return True


# ----------------------------------------------------------------------- build

def build(doc, now: float | None = None, policy: Policy | None = None) -> DataApiResult:
    """Turn a /portfolios document into the provider's TokenBalance lines."""
    now = time.time() if now is None else now
    policy = policy or Policy.from_env()
    if not isinstance(doc, dict) or not isinstance(doc.get("tokens"), list):
        raise HttpError("Unexpected data-api response")
    try:
        return _build(doc, now, policy)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise HttpError("Unexpected data-api response") from None


def _build(doc: dict, now: float, policy: Policy) -> DataApiResult:
    positions = _dicts(doc.get("defi_positions"))
    plain_lp: dict[str, dict] = {}        # LP token id -> its pool-backed position
    for pos in positions:
        if pos.get("is_farming") or pos.get("is_lending") or pos.get("is_borrow") or pos.get("is_collateral"):
            continue
        lp_token = _token(pos.get("lp_token"))
        lp_id, _dec = _ident(lp_token)
        if lp_id:
            plain_lp[lp_id] = pos

    result = DataApiResult(native_amount=0.0)

    # What the data-api accounts for in the wallet (see DataApiResult.known_ids).
    # Underlying and reward tokens are NOT listed: a pool's reserve is not a
    # holding, so a wallet token that only appears there is still "unknown".
    for entry in _dicts(doc.get("tokens")):
        ident, _dec = _ident(_token(entry))
        if ident:
            result.known_ids.add(base_id(ident))
    for pos in positions:
        if pos.get("is_farming"):
            meta = pos.get("farming_meta_esdt")
            meta_id = clean_text(meta.get("id"), 80) if isinstance(meta, dict) else ""
            if meta_id:
                result.known_ids.add(base_id(meta_id))
        else:                       # LP / Hatom receipt token held in the wallet
            ident, _dec = _ident(_token(pos.get("lp_token")))
            if ident:
                result.known_ids.add(base_id(ident))

    # -- wallet: native coin and plain ESDT tokens ------------------------------
    for entry in _dicts(doc.get("tokens")):
        token = _token(entry)
        ident, decimals = _ident(token)
        raw = _int(entry.get("balance"))
        if token is None or not ident or decimals is None or raw is None or raw <= 0:
            continue
        amount = _units(raw, decimals)
        if not amount or amount <= 0:
            continue
        if ident == "EGLD":
            result.native_amount = amount
            continue
        tb = TokenBalance(
            symbol=_symbol(ident), name=_label(token, ident), amount=amount,
            contract=ident, asset_type="esdt",
        )
        if token.get("is_lp_token") is True:
            pos = plain_lp.get(ident)
            via = clean_text((pos or {}).get("protocol"), 30) or "pool"
            # unvouched LP: left unlocked, the local pool reader may still value it
            tb.price_locked = _value_lp(tb, token, ident, raw, pos, policy, now, via)
        else:
            price = _price(token, ident, policy, now, is_lp=False)
            tb.price_locked = True
            if price is not None:
                tb.usd_value = amount * price
        result.tokens.append(tb)

    # -- Hatom deposits and borrows ------------------------------------------------
    for pos in positions:
        if not (pos.get("is_lending") or pos.get("is_borrow")):
            continue
        underlying = _dicts(pos.get("underlying_assets"))
        receipt = _token(pos.get("lp_token"))
        receipt_id, _d = _ident(receipt)
        if len(underlying) != 1 or not receipt_id:
            continue
        u_token = _token(underlying[0])
        u_id, u_dec = _ident(u_token)
        raw = _int(underlying[0].get("balance"))
        if u_token is None or not u_id or u_dec is None or not raw:
            continue
        amount = _units(raw, u_dec)
        if not amount or amount <= 0:
            continue
        borrow = bool(pos.get("is_borrow"))
        tb = TokenBalance(
            symbol=_symbol(u_id),
            name=clean_text(f"{_label(receipt, receipt_id)} · Hatom {'borrow' if borrow else 'deposit'}", 300),
            amount=amount, contract=receipt_id,
            asset_type="borrow" if borrow else "lending",
        )
        tb.price_locked = True
        price = _price(u_token, u_id, policy, now, is_lp=u_token.get("is_lp_token") is True)
        if price is not None:
            tb.usd_value = -amount * price if borrow else amount * price
        result.tokens.append(tb)

    # -- xExchange farms (standard LP farms and metastaking) and their rewards --------
    for pos in positions:
        if not pos.get("is_farming"):
            continue
        f_token = _token(pos.get("lp_token"))
        f_id, f_dec = _ident(f_token)
        raw = _int((pos.get("lp_token") or {}).get("balance"))
        meta = pos.get("farming_meta_esdt")
        meta_id = clean_text(meta.get("id"), 80) if isinstance(meta, dict) else ""
        if f_token is None or not f_id or f_dec is None or not raw or raw <= 0:
            continue
        amount = _units(raw, f_dec)
        if not amount or amount <= 0:
            continue
        kind = clean_text(pos.get("farming_type"), 30) or "farm"
        tb = TokenBalance(
            symbol=_symbol(f_id),
            name=clean_text(f"{_label(f_token, f_id)} · xExchange farm ({kind})", 300),
            amount=amount, contract=meta_id or f_id, asset_type="farm",
        )
        tb.price_locked = True
        if f_token.get("is_lp_token") is True:
            _value_lp(tb, f_token, f_id, raw, pos, policy, now, "xexchange")
        else:
            price = _price(f_token, f_id, policy, now, is_lp=False)
            if price is not None:
                tb.usd_value = amount * price
        result.tokens.append(tb)

        for reward in _dicts(pos.get("pending_rewards")):
            r_token = _token(reward)
            r_id, r_dec = _ident(r_token)
            r_raw = _int(reward.get("balance"))
            if r_token is None or not r_id or r_dec is None or not r_raw or r_raw <= 0:
                continue
            r_amount = _units(r_raw, r_dec)
            if not r_amount or r_amount <= 0:
                continue
            rb = TokenBalance(
                symbol=_symbol(r_id),
                name=clean_text(f"{_label(r_token, r_id)} · pending farm rewards", 300),
                amount=r_amount, contract=r_id, asset_type="farm-rewards",
            )
            rb.price_locked = True
            price = _price(r_token, r_id, policy, now, is_lp=r_token.get("is_lp_token") is True)
            if price is not None:
                rb.usd_value = r_amount * price
            result.tokens.append(rb)

    return result
