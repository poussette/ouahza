"""Security helpers shared by every provider, the pricing module and the UIs.

Threat model in one paragraph: everything that comes back from the network
(token names/symbols, RPC error strings, decimals, amounts, prices, page
counts...) is *untrusted* -- anybody can airdrop a token to your address
with a hostile name or absurd metadata, and a public RPC/API can misbehave.
The config text you type is trusted-ish but may be pasted from elsewhere.
The helpers below make sure such data cannot (a) leak your API keys, (b) hang
or exhaust the app, (c) inject terminal escapes / spreadsheet formulas /
UI markup, or (d) poison totals with NaN/inf/absurd numbers.
"""

from __future__ import annotations

__version__ = "0.1.1"






import math
import os
import re
import unicodedata
from urllib.parse import urlsplit

#: max length of a displayed text field (names get a bit more room because
#: some of them embed several base58 addresses, e.g. Solana stake accounts).
MAX_TEXT = 300
MAX_SYMBOL = 40
#: ERC-20/ESDT decimals are at most 18 in practice; anything above this is
#: hostile metadata (10**decimals would otherwise be unbounded work).
MAX_DECIMALS = 36
#: sanity ceilings: a personal wallet never holds more than this many units of
#: one asset, nor sees a unit price above MAX_PRICE.
MAX_AMOUNT = 1e24
MAX_PRICE = 1e12
#: a single position valued above this (USD) is treated as a pricing glitch
#: / manipulated illiquid pool, not real money, and left unpriced.
MAX_POSITION_USD = 1e9
#: input limits for the address list.
MAX_ENTRIES = 500
MAX_LINE = 512

_DROPPED_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


# --------------------------------------------------------------------- text

def clean_text(value, max_len: int = MAX_TEXT) -> str:
    """Return `value` as printable single-line text.

    Removes control characters (terminal escape sequences start with ESC),
    bidirectional overrides and zero-width characters (used to visually
    spoof text), turns line breaks into spaces and truncates.
    """
    if value is None:
        return ""
    s = str(value)[: max_len * 4]  # bound the work on hostile giant strings
    out = []
    for ch in s:
        if ch in "\n\r\t":
            out.append(" ")
        elif unicodedata.category(ch) in _DROPPED_CATEGORIES:
            continue
        else:
            out.append(ch)
    s = re.sub(r" {2,}", " ", "".join(out)).strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


# ------------------------------------------------------------------ secrets

#: environment variables whose *values* must never appear in any output.
_SENSITIVE_ENV = (
    "ETHERSCAN_API_KEY",
    "BEACONCHAIN_API_KEY",
    "ETH_RPC_URL",
    "SOLANA_RPC_URL",
    "MULTIVERSX_GATEWAY_URL",
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)((?:api[-_]?key|apikey|access[-_]?token|token|secret|auth|key)=)[^&\s\"'<>)]+"
)
_USERINFO_RE = re.compile(r"(?i)(https?://)[^/\s@]+@")


def _env_secret_fragments() -> list[str]:
    """Secret-looking pieces of the configured custom endpoints. Alchemy /
    Infura / Helius embed the key as a path segment, and `requests` errors
    often print only the path, so the full URL never appears to be replaced."""
    frags: list[str] = []
    for name in _SENSITIVE_ENV:
        value = os.environ.get(name, "")
        if len(value) < 4:
            continue
        frags.append(value)
        try:
            parts = urlsplit(value)
            frags.extend(seg for seg in parts.path.split("/") if len(seg) >= 8)
            frags.extend(
                v for pair in parts.query.split("&") for v in [pair.partition("=")[2]] if len(v) >= 4
            )
            if parts.password:
                frags.append(parts.password)
            if parts.username and len(parts.username) >= 8:
                frags.append(parts.username)
        except ValueError:
            pass
    # longest first so a full URL is replaced before its own fragments
    return sorted(set(frags), key=len, reverse=True)


def redact(text) -> str:
    """Strip credentials from a string before it is shown, copied or logged.

    `requests` exceptions embed the full request URL (or just its path), which
    for Etherscan / beaconcha.in carries the API key in the query string and
    for custom RPC endpoints (Alchemy, Infura, Helius...) carries it in the
    path.
    """
    s = str(text)
    for frag in _env_secret_fragments():
        s = s.replace(frag, "<secret>")
    s = _USERINFO_RE.sub(r"\1***@", s)
    return _QUERY_SECRET_RE.sub(r"\1***", s)


def safe_error(exc) -> str:
    """One-line, credential-free, printable description of an exception."""
    return clean_text(redact(exc), MAX_TEXT)


def validate_rpc_url(url: str | None) -> str | None:
    """Accept only https URLs (or http to a loopback node); else None."""
    if not url:
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return url
    return None


# ------------------------------------------------------------------ numbers

def safe_amount(value, ceiling: float = MAX_AMOUNT) -> float | None:
    """A finite amount in [0, ceiling], else None (NaN/inf/negative/absurd)."""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(v) or v < 0 or v > ceiling:
        return None
    return v


def safe_price(value) -> float | None:
    """A finite, strictly positive unit price below MAX_PRICE, else None."""
    v = safe_amount(value, MAX_PRICE)
    return v if v else None


def safe_decimals(value, default: int | None = None) -> int | None:
    """An int in [0, MAX_DECIMALS], else `default`."""
    if isinstance(value, bool):
        return default
    try:
        d = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return d if 0 <= d <= MAX_DECIMALS else default


# ---------------------------------------------------------------------- csv

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_text(value) -> str:
    """Neutralise spreadsheet formula injection for an untrusted text cell.

    Excel/LibreOffice/Sheets evaluate cells starting with = + - @ as
    formulas (e.g. =HYPERLINK(...) used to exfiltrate data); prefixing a
    single quote makes them plain text. Use for text columns only -- numeric
    columns must stay numbers.
    """
    if value is None:
        return ""
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIXES) else s


# ------------------------------------------------------------------ wallets

def sanitize_wallet(w) -> None:
    """Clean a WalletBalance in place right after a provider returned it:
    printable bounded text everywhere, credential-free error/warning text,
    finite sane amounts (tokens with invalid amounts are dropped)."""
    w.label = clean_text(w.label, 100) or None if w.label else w.label
    w.chain = clean_text(w.chain, MAX_SYMBOL)
    w.address = clean_text(w.address, 200)
    w.native_symbol = clean_text(w.native_symbol, MAX_SYMBOL)
    if w.error:
        w.error = clean_text(redact(w.error), 400)
    if w.warning:
        w.warning = clean_text(redact(w.warning), 600)

    if w.native_amount is not None:
        amount = safe_amount(w.native_amount)
        if amount is None:
            w.native_amount = None
            w.error = w.error or "Montant natif invalide renvoyé par l'API (ignoré)."
        else:
            w.native_amount = amount

    kept = []
    dropped = 0
    for t in w.tokens:
        amount = safe_amount(t.amount)
        if amount is None:
            dropped += 1
            continue
        t.amount = amount
        t.symbol = clean_text(t.symbol, MAX_SYMBOL) or "?"
        t.name = clean_text(t.name, MAX_TEXT) or "?"
        t.contract = clean_text(t.contract, 200) if t.contract else t.contract
        t.asset_type = clean_text(t.asset_type, MAX_SYMBOL) or "token"
        kept.append(t)
    w.tokens = kept
    if dropped:
        note = f"{dropped} position(s) ignorée(s) (montant invalide renvoyé par l'API)"
        w.warning = f"{w.warning}; {note}" if w.warning else note
