"""Look for a newer release of the app on GitHub.

Only the *check* lives here: it asks the public GitHub API for the latest
release of the repository and returns the link of the APK to download. Nothing
is ever downloaded or installed automatically -- the app just opens that link
and Android's own installer takes over (it refuses any APK that is not signed
with the same key as the installed app, so a tampered file cannot replace it,
and an update over the installed app keeps its private data/settings).
"""

from __future__ import annotations

__version__ = "0.1.26"

import re

import requests

from .net import HttpError, request_json
from .safe import clean_text

DEFAULT_REPO = "poussette/ouahza"

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}")
_VER_RE = re.compile(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})")


def parse_version(text) -> tuple[int, int, int] | None:
    """'v0.9.8' / '0.9.8' -> (0, 9, 8); anything else -> None."""
    if not isinstance(text, str):
        return None
    m = _VER_RE.fullmatch(text.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def valid_repo(repo) -> bool:
    return isinstance(repo, str) and bool(_REPO_RE.fullmatch(repo)) and ".." not in repo


VARIANTS = ("universal", "arm64")


def variant_from_apk_entries(names) -> str | None:
    """Which build is installed, from the entry names inside its APK: the arm64
    build only ships lib/arm64-v8a/, the universal one also ships lib/armeabi-v7a/.
    None when it cannot be told."""
    names = list(names)
    has64 = any(n.startswith("lib/arm64-v8a/") for n in names)
    has32 = any(n.startswith("lib/armeabi-v7a/") for n in names)
    if has64 and not has32:
        return "arm64"
    if has32:
        return "universal"
    return None


def _pick_asset(assets, repo: str, variant: str = "universal") -> str | None:
    """Link of the APK of the *same variant* as the installed app (an arm64 APK
    must be updated by an arm64 APK: the two builds use different version-code
    schemes). Only links on this repository's github.com release pages are
    accepted; None when the release has no APK of that variant."""
    if variant not in VARIANTS:
        variant = "universal"
    prefix = f"https://github.com/{repo}/releases/download/"
    apks = []
    for a in assets if isinstance(assets, list) else []:
        if not isinstance(a, dict):
            continue
        name, url = a.get("name"), a.get("browser_download_url")
        if (isinstance(name, str) and isinstance(url, str)
                and name.lower().endswith(".apk") and url.startswith(prefix)):
            apks.append((name.lower(), url))
    for name, url in apks:
        if name.endswith(f"-{variant}.apk"):
            return url
    return None


def check(current: str, repo: str = DEFAULT_REPO, variant: str = "universal") -> dict | None:
    """Return {"version", "url", "page", "notes"} when a release newer than
    `current` exists, else None. Raises net.HttpError on network problems."""
    cur = parse_version(current)
    if cur is None or not valid_repo(repo):
        return None
    try:
        data = request_json(
            "GET", f"https://api.github.com/repos/{repo}/releases/latest", max_bytes=500_000,
        )
    except HttpError as exc:
        # The anonymous GitHub API allows ~60 requests an hour per IP address, and a
        # phone on a shared mobile network often exhausts it: use the public
        # "releases/latest" page instead (no such limit).
        if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
            return _check_via_redirect(cur, repo, variant)
        raise
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    latest = parse_version(data.get("tag_name"))
    if latest is None or latest <= cur:
        return None
    page = f"https://github.com/{repo}/releases/tag/{data['tag_name'].strip()}"
    url = _pick_asset(data.get("assets"), repo, variant) or page
    return {
        "version": ".".join(map(str, latest)),
        "url": url,
        "page": page,
        "notes": clean_text(data.get("body") or "", 400),
    }


def _check_via_redirect(cur: tuple[int, int, int], repo: str, variant: str) -> dict | None:
    """Same answer without the API: github.com/<repo>/releases/latest redirects to
    .../releases/tag/<tag>. The APK link follows the build's naming scheme
    (Ouahza-<version>-<variant>.apk); if it were wrong the release page opens."""
    if variant not in VARIANTS:
        variant = "universal"
    try:
        resp = requests.get(
            f"https://github.com/{repo}/releases/latest", allow_redirects=False, timeout=(6, 15),
        )
    except requests.RequestException as exc:
        raise HttpError("GitHub injoignable") from exc
    try:
        location = resp.headers.get("Location", "") if 300 <= resp.status_code < 400 else ""
    finally:
        resp.close()
    m = re.fullmatch(
        rf"(?:https://github\.com)?/{re.escape(repo)}/releases/tag/(v?\d{{1,4}}\.\d{{1,4}}\.\d{{1,4}})", location,
    )
    if not m:
        raise HttpError("HTTP 403 from api.github.com")
    tag = m.group(1)
    latest = parse_version(tag)
    if latest is None or latest <= cur:
        return None
    version = ".".join(map(str, latest))
    page = f"https://github.com/{repo}/releases/tag/{tag}"
    return {
        "version": version,
        "url": f"https://github.com/{repo}/releases/download/{tag}/Ouahza-{version}-{variant}.apk",
        "page": page,
        "notes": "",
    }
