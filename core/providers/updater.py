"""Look for a newer release of the app on GitHub.

Only the *check* lives here: it asks the public GitHub API for the latest
release of the repository and returns the link of the APK to download. Nothing
is ever downloaded or installed automatically -- the app just opens that link
and Android's own installer takes over (it refuses any APK that is not signed
with the same key as the installed app, so a tampered file cannot replace it,
and an update over the installed app keeps its private data/settings).
"""

from __future__ import annotations

__version__ = "0.1.0"

import re

from .net import request_json
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


def _pick_asset(assets, repo: str) -> str | None:
    """Link of the APK to offer: the universal build first (it keeps the same
    version-code scheme as an installed universal APK), else any .apk. Only
    links on this repository's github.com release pages are accepted."""
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
        if "universal" in name:
            return url
    return apks[0][1] if apks else None


def check(current: str, repo: str = DEFAULT_REPO) -> dict | None:
    """Return {"version", "url", "page", "notes"} when a release newer than
    `current` exists, else None. Raises net.HttpError on network problems."""
    cur = parse_version(current)
    if cur is None or not valid_repo(repo):
        return None
    data = request_json(
        "GET", f"https://api.github.com/repos/{repo}/releases/latest", max_bytes=500_000,
    )
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    latest = parse_version(data.get("tag_name"))
    if latest is None or latest <= cur:
        return None
    page = f"https://github.com/{repo}/releases/tag/{data['tag_name'].strip()}"
    url = _pick_asset(data.get("assets"), repo) or page
    return {
        "version": ".".join(map(str, latest)),
        "url": url,
        "page": page,
        "notes": clean_text(data.get("body") or "", 400),
    }
