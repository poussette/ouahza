"""Single source of truth for the release number, and a check that every
component file really belongs to it.

Each source file carries its own literal `__version__`. After copying a patch
over an older tree, a file that was not replaced still reports its old number,
so `python main.py --version` (CLI) or the status line (app) shows it at once.
"""

from __future__ import annotations

import importlib

VERSION = "0.1.22"
__version__ = VERSION

#: importable components (the entry script is checked by its caller)
COMPONENTS = [
    "providers", "providers.base", "providers.bitcoin", "providers.ethereum",
    "providers.solana", "providers.multiversx", "providers.net", "providers.safe",
    "providers.lp", "providers.updater", "pricing", "report",
]


def collect(extra: dict[str, str] | None = None) -> dict[str, str | None]:
    """{component: its __version__, or None if missing/unreadable}. A component
    that does not exist in this tree (e.g. `report` in the CLI) is skipped."""
    out: dict[str, str | None] = {"providers.version": VERSION}
    for name in COMPONENTS:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue  # not part of this tree
            out[name] = None
            continue
        except Exception:  # noqa: BLE001 - a broken file must be reported, not crash
            out[name] = None
            continue
        v = getattr(mod, "__version__", None)
        out[name] = v if isinstance(v, str) else None
    out.update(extra or {})
    return out


def mismatches(extra: dict[str, str] | None = None) -> list[str]:
    """Components whose version differs from VERSION ("lp.py (0.7)")."""
    return [f"{name} ({v or '?'})" for name, v in collect(extra).items() if v != VERSION]


def summary(extra: dict[str, str] | None = None) -> str:
    bad = mismatches(extra)
    if not bad:
        return f"v{VERSION}: all components match"
    return f"v{VERSION}: MISMATCH -> " + ", ".join(bad)
