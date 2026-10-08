#!/usr/bin/env python3
"""Change the release number everywhere in one go.

    python3 tools/set_version.py 0.1.1

Updates: core/providers/version.py (VERSION), the `__version__` stamp of every
source file, APP_VERSION in android/main.py and `version =` in
android/buildozer.spec. The Android build, the GitHub Release tag and the
in-app update check all derive from that number, so run this before each
release (the new number must be higher than the previous one).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sub(path: Path, pattern: str, repl: str, count_expected: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n < count_expected:
        raise SystemExit(f"pattern not found in {path.relative_to(ROOT)}: {pattern}")
    path.write_text(new, encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", sys.argv[1]):
        print(__doc__)
        return 1
    v = sys.argv[1]
    sub(ROOT / "core/providers/version.py", r'^VERSION = ".*"$', f'VERSION = "{v}"')
    stamped = [p for p in sorted((ROOT / "core/providers").glob("*.py")) if p.name != "version.py"] + [
        ROOT / "core/pricing.py", ROOT / "cli/main.py", ROOT / "cli/lp_probe.py",
        ROOT / "android/main.py", ROOT / "android/report.py",
    ]
    for f in stamped:
        sub(f, r'^__version__ = ".*"$', f'__version__ = "{v}"')
    sub(ROOT / "android/main.py", r'^APP_VERSION = ".*"$', f'APP_VERSION = "{v}"')
    sub(ROOT / "android/buildozer.spec", r"^version = .*$", f"version = {v}")
    print(f"version -> {v} ({len(stamped)} fichiers + version.py + APP_VERSION + buildozer.spec)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
