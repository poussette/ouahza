"""Hardened HTTP helper used by every provider and by the pricing module.

Compared with calling `requests.get(...).json()` directly it:
  * only talks HTTPS (plain http is accepted solely for a loopback node);
  * refuses redirects that leave HTTPS;
  * caps the *decoded* response size and the total download time, so a
    hostile/buggy endpoint (or a gzip bomb) cannot exhaust memory or hang;
  * turns every network/HTTP/JSON problem into `HttpError`, a
    `requests.RequestException` subclass whose message has already been
    stripped of API keys (requests exceptions embed the full URL, which for
    Etherscan/beaconcha.in contains the key) -- so existing
    `except requests.RequestException` handlers keep working.
TLS certificate verification is never disabled.
"""

from __future__ import annotations

__version__ = "0.1.37"






import ipaddress
import json
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

from .safe import redact

#: (connect, read) timeouts in seconds.
DEFAULT_TIMEOUT = (6, 20)
DEFAULT_MAX_BYTES = 10_000_000
#: hard wall-clock limit for one download (a slow-drip response would
#: otherwise stay under the per-read timeout forever).
DEADLINE_SECONDS = 60

_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _is_private_literal(host: str | None) -> bool:
    """True for an IP *literal* in a private, loopback, link-local or otherwise
    non-public range (names are not resolved here)."""
    try:
        ip = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    return not ip.is_global


class HttpError(requests.RequestException):
    """Network/HTTP/format error; the message is already credential-free."""


def _https_or_loopback(url: str, allow_loopback_http: bool) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme == "https" and host:
        return True
    return bool(allow_loopback_http and parts.scheme == "http" and host in _LOOPBACK)


def _read_capped(resp, max_bytes: int) -> bytes:
    deadline = time.monotonic() + DEADLINE_SECONDS
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise HttpError(f"API response too large (> {max_bytes // 1_000_000} MB)")
            if time.monotonic() > deadline:
                raise HttpError("API response too slow")
            chunks.append(chunk)
    except requests.RequestException as exc:
        if isinstance(exc, HttpError):
            raise
        raise HttpError(redact(str(exc))) from None
    return b"".join(chunks)


MAX_REDIRECTS = 3

#: transient failures are retried (public APIs rate-limit and flake a lot):
#: delays in seconds between attempts; () disables retrying.
RETRY_DELAYS = (1.0, 3.0, 6.0)
_RETRY_STATUS = {429, 502, 503, 504}
MAX_RETRY_AFTER = 10.0

#: minimum spacing (seconds) between two request *starts* to the same host,
#: shared by all worker threads: stays under the free-tier rate limits
#: (api.multiversx.com answers 429 when 24 wallets fire ~7 calls each at once).
THROTTLE_ENABLED = True
MIN_INTERVAL = {
    "api.multiversx.com": 0.3,
    "gateway.multiversx.com": 0.15,
    "api.coingecko.com": 1.5,
}
DEFAULT_INTERVAL = 0.05
_throttle_lock = threading.Lock()
_next_slot: dict[str, float] = {}


#: after an HTTP 429 the host is asked less often: its interval is multiplied
#: (x1.5 per 429, at most x3) and then eases back by one step (/1.5) for every
#: PENALTY_RECOVERY seconds without a 429, so a slow-down never lingers into
#: the next refresh for longer than needed.
PENALTY_STEP = 1.5
PENALTY_MAX = 3.0
PENALTY_RECOVERY = 20.0
_penalty: dict[str, tuple[float, float]] = {}   # host -> (factor at last 429, time of it)


def _factor(host: str) -> float:
    entry = _penalty.get(host)
    if not entry:
        return 1.0
    factor, since = entry
    calm = max(0.0, time.monotonic() - since)
    return max(1.0, factor / (PENALTY_STEP ** (calm / PENALTY_RECOVERY)))


def _note_429(host: str) -> None:
    with _throttle_lock:
        _penalty[host] = (min(_factor(host) * PENALTY_STEP, PENALTY_MAX), time.monotonic())


#: right after the app starts the free APIs see our first burst (prices,
#: wallets, empty-answer cache still cold): begin gently, the extra caution
#: eases away by itself like after a 429.
def warm_start(hosts=("api.multiversx.com",), factor: float = 1.5) -> None:
    with _throttle_lock:
        now = time.monotonic()
        for host in hosts:
            entry = _penalty.get(host)
            current = _factor(host) if entry else 1.0
            if current < factor:
                _penalty[host] = (factor, now)


def reset_penalties() -> None:
    with _throttle_lock:
        _penalty.clear()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ------------------------------------------------------------------ statistics
# Per-host counters for the "where does the time go" line shown by the app:
# number of requests, seconds spent waiting for the throttle slot, HTTP 429s.
STATS: dict[str, dict[str, float]] = {}
_stats_lock = threading.Lock()


#: host -> {error label -> count}; labels are fixed short words ("HTTP 429", "Timeout"),
#: never the server's text or a URL.
ERRORS: dict[str, dict[str, int]] = {}


def reset_stats() -> None:
    with _stats_lock:
        STATS.clear()
        ERRORS.clear()


def _count(host: str, field: str, amount: float = 1) -> None:
    with _stats_lock:
        row = STATS.setdefault(host or "?", {"calls": 0, "wait": 0.0, "r429": 0})
        row[field] = row.get(field, 0) + amount


def _note_error(host: str, label: str) -> None:
    with _stats_lock:
        row = ERRORS.setdefault(host or "?", {})
        if label in row or len(row) < 12:
            row[label] = row.get(label, 0) + 1


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


#: host names shown differently in the statistics line.
HOST_LABELS = {"data-api.mvx.fr": "data-api-ecompass"}


def stats_summary(max_hosts: int = 4) -> str:
    """"api.multiversx.com 120 appels (attente 48 s, 3×429) · ..." (most used first)."""
    with _stats_lock:
        rows = sorted(STATS.items(), key=lambda kv: -kv[1]["calls"])[:max_hosts]
        parts = []
        for host, row in rows:
            extra = []
            if row["wait"] >= 0.5:
                extra.append(f"attente {row['wait']:.0f} s")
            if row["r429"]:
                extra.append(f"{int(row['r429'])}×429")
            parts.append(f"{HOST_LABELS.get(host, host)} {int(row['calls'])} appel(s)" + (f" ({', '.join(extra)})" if extra else ""))
    return " · ".join(parts)


def stats_report() -> str:
    """Per-host log of the last refresh, for the app's "Logs" popup: number of
    requests, cumulated network seconds, throttle wait, retries and errors
    (every host, most used first). Requests run in parallel, so the cumulated
    seconds can exceed the wall-clock time."""
    with _stats_lock:
        rows = sorted(STATS.items(), key=lambda kv: -kv[1]["calls"])
        errors = {h: dict(e) for h, e in ERRORS.items()}
        total_calls = sum(int(r["calls"]) for _, r in rows)
        total_time = sum(r.get("time", 0.0) for _, r in rows)
        total_err = sum(sum(e.values()) for e in errors.values())
        total_retry = sum(int(r.get("retry", 0)) for _, r in rows)
        blocks = []
        for host, row in rows:
            lines = [HOST_LABELS.get(host, host),
                     f"  {int(row['calls'])} appel(s) · {row.get('time', 0.0):.1f} s"]
            extra = []
            if row["wait"] >= 0.5:
                extra.append(f"attente {row['wait']:.0f} s")
            if row.get("retry"):
                extra.append(f"{int(row['retry'])} nouvel(les) essai(s) (pause {row.get('retry_wait', 0.0):.0f} s)")
            if extra:
                lines.append("  " + " · ".join(extra))
            if errors.get(host):
                lines.append("  erreurs : " + ", ".join(
                    f"{n}× {label}" for label, n in sorted(errors[host].items(), key=lambda kv: -kv[1])))
            blocks.append("\n".join(lines))
    if not blocks:
        return "Aucune requête."
    head = f"{total_calls} appel(s) · {total_time:.0f} s cumulées (en parallèle)\n"
    head += f"{total_retry} nouvel(les) essai(s) · " if total_retry else ""
    head += f"{total_err} erreur(s)" if total_err else "aucune erreur"
    return head + "\n\n" + "\n\n".join(blocks)


def _throttle(url: str) -> None:
    if not THROTTLE_ENABLED:
        return
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return
    with _throttle_lock:
        interval = MIN_INTERVAL.get(host, DEFAULT_INTERVAL) * _factor(host)
        now = time.monotonic()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + interval
    if slot > now:
        _count(host, "wait", slot - now)
        _sleep(slot - now)


def _short_http_error(resp) -> str:
    """HTTP error text without the full URL (it may carry keys/addresses)."""
    try:
        host = urlsplit(resp.url).hostname or "API"
    except ValueError:
        host = "API"
    return f"HTTP {resp.status_code} from {host}"


def _send(method, url, params, json_body, timeout, allow_loopback_http, headers=None):
    """One request, following redirects by hand: the scheme of every hop is
    checked *before* the next request is sent, so a hijacked endpoint
    answering `302 -> http://...` never receives the request (and its API key)
    in clear text."""
    for _hop in range(MAX_REDIRECTS + 1):
        _throttle(url)
        started = time.monotonic()
        try:
            resp = requests.request(
                method, url, params=params, json=json_body, timeout=timeout,
                stream=True, allow_redirects=False, headers=headers,
            )
        except requests.RequestException as exc:
            _count(_host_of(url), "calls")
            _count(_host_of(url), "time", time.monotonic() - started)
            _note_error(_host_of(url), type(exc).__name__)
            raise HttpError(redact(str(exc))) from None
        _count(urlsplit(url).hostname or "", "calls")
        _count(_host_of(url), "time", time.monotonic() - started)
        if resp.status_code >= 400:
            _note_error(_host_of(url), f"HTTP {resp.status_code}")
        if resp.status_code == 429:
            _count(urlsplit(url).hostname or "", "r429")
            _note_429(urlsplit(url).hostname or "")
        location = resp.headers.get("Location") if 300 <= resp.status_code < 400 else None
        if not location:
            return resp
        resp.close()
        previous_host = urlsplit(url).hostname
        try:
            url = urljoin(url, location)
            new_host = urlsplit(url).hostname
        except ValueError:
            raise HttpError("Refusing redirect to an invalid URL") from None
        cross_host = new_host != previous_host
        if headers and cross_host:
            headers = None   # a credential header never follows a redirect to another host
        # Plain http is only ever accepted towards a loopback node, and only when
        # the request was already talking to loopback: a public server must not be
        # able to bounce the request onto a service listening on this machine.
        if not _https_or_loopback(url, allow_loopback_http and previous_host in _LOOPBACK):
            raise HttpError("Refusing redirect to a non-HTTPS URL")
        if cross_host and _is_private_literal(new_host) and not _is_private_literal(previous_host):
            raise HttpError("Refusing redirect to a private network address")
        if resp.status_code in (301, 302, 303):
            method, json_body = "GET", None
        if cross_host and json_body is not None:
            raise HttpError("Refusing cross-host redirect for a request with a body")
        params = None  # the redirect target already carries its own query
    raise HttpError("Too many redirects")


def request_bytes(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body=None,
    timeout=DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_loopback_http: bool = False,
    headers: dict | None = None,
) -> bytes:
    """Perform the request and return the (size-capped) response body. Rate
    limits (429/502/503/504), timeouts and connection errors are retried with
    a short back-off (honouring Retry-After)."""
    if not _https_or_loopback(url, allow_loopback_http):
        raise HttpError("Refusing non-HTTPS URL")
    delays = list(RETRY_DELAYS)
    while True:
        wait = None
        try:
            resp = _send(method, url, params, json_body, timeout, allow_loopback_http, headers)
        except HttpError as exc:
            transient = "redirect" not in str(exc) and "non-HTTPS" not in str(exc)
            if not (transient and delays):
                raise
            wait = delays.pop(0)
        else:
            if resp.status_code in _RETRY_STATUS and delays:
                wait = delays.pop(0)
                try:
                    wait = max(wait, min(float(resp.headers.get("Retry-After", 0)), MAX_RETRY_AFTER))
                except (TypeError, ValueError):
                    pass
                resp.close()
            else:
                break
        _count(_host_of(url), "retry")
        _count(_host_of(url), "retry_wait", wait)
        _sleep(wait)
    try:
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            raise HttpError(_short_http_error(resp)) from None
        read_started = time.monotonic()
        try:
            body = _read_capped(resp, max_bytes)
        finally:
            _count(_host_of(url), "time", time.monotonic() - read_started)
    finally:
        resp.close()
    return body


def request_json(method: str, url: str, **kwargs):
    """`request_bytes` decoded as JSON (same arguments); every problem is an HttpError."""
    body = request_bytes(method, url, **kwargs)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise HttpError("Invalid JSON in API response") from None


def request_text(method: str, url: str, **kwargs) -> str:
    """`request_bytes` decoded as UTF-8 text (invalid bytes replaced)."""
    return request_bytes(method, url, **kwargs).decode("utf-8", "replace")
