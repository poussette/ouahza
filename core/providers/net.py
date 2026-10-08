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

__version__ = "0.1.5"






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
RETRY_DELAYS = (1.0, 3.0)
_RETRY_STATUS = {429, 502, 503, 504}
MAX_RETRY_AFTER = 10.0

#: minimum spacing (seconds) between two request *starts* to the same host,
#: shared by all worker threads: stays under the free-tier rate limits
#: (api.multiversx.com answers 429 when 24 wallets fire ~7 calls each at once).
THROTTLE_ENABLED = True
MIN_INTERVAL = {
    "api.multiversx.com": 0.4,
    "gateway.multiversx.com": 0.15,
    "api.coingecko.com": 1.5,
}
DEFAULT_INTERVAL = 0.05
_throttle_lock = threading.Lock()
_next_slot: dict[str, float] = {}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _throttle(url: str) -> None:
    if not THROTTLE_ENABLED:
        return
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return
    interval = MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
    with _throttle_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + interval
    if slot > now:
        _sleep(slot - now)


def _short_http_error(resp) -> str:
    """HTTP error text without the full URL (it may carry keys/addresses)."""
    try:
        host = urlsplit(resp.url).hostname or "API"
    except ValueError:
        host = "API"
    return f"HTTP {resp.status_code} from {host}"


def _send(method, url, params, json_body, timeout, allow_loopback_http):
    """One request, following redirects by hand: the scheme of every hop is
    checked *before* the next request is sent, so a hijacked endpoint
    answering `302 -> http://...` never receives the request (and its API key)
    in clear text."""
    for _hop in range(MAX_REDIRECTS + 1):
        _throttle(url)
        try:
            resp = requests.request(
                method, url, params=params, json=json_body, timeout=timeout,
                stream=True, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HttpError(redact(str(exc))) from None
        location = resp.headers.get("Location") if 300 <= resp.status_code < 400 else None
        if not location:
            return resp
        resp.close()
        url = urljoin(url, location)
        if not _https_or_loopback(url, allow_loopback_http):
            raise HttpError("Refusing redirect to a non-HTTPS URL")
        if resp.status_code in (301, 302, 303):
            method, json_body = "GET", None
        params = None  # the redirect target already carries its own query
    raise HttpError("Too many redirects")


def request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body=None,
    timeout=DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_loopback_http: bool = False,
):
    """Perform the request and return the decoded JSON document. Rate limits
    (429/502/503/504), timeouts and connection errors are retried with a
    short back-off (honouring Retry-After)."""
    if not _https_or_loopback(url, allow_loopback_http):
        raise HttpError("Refusing non-HTTPS URL")
    delays = list(RETRY_DELAYS)
    while True:
        wait = None
        try:
            resp = _send(method, url, params, json_body, timeout, allow_loopback_http)
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
        _sleep(wait)
    try:
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            raise HttpError(_short_http_error(resp)) from None
        body = _read_capped(resp, max_bytes)
    finally:
        resp.close()
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise HttpError("Invalid JSON in API response") from None
