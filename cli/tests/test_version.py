"""Every component file must carry the release number (see providers/version.py)."""
import ast
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent   # racine du dépôt
ROOT = REPO / "cli"
CORE = REPO / "core"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CORE))

from providers import version  # noqa: E402


def literal_version(path: Path):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__version__" for t in node.targets):
            return ast.literal_eval(node.value) if isinstance(node.value, ast.Constant) else None
    return None


class VersionTests(unittest.TestCase):
    def test_release_number_shape(self):
        self.assertRegex(version.VERSION, r"^\d+\.\d+\.\d+$")

    def test_every_source_file_is_stamped(self):
        files = list((CORE / "providers").glob("*.py")) + [
            CORE / "pricing.py", ROOT / "main.py", ROOT / "lp_probe.py",
            REPO / "android" / "main.py", REPO / "android" / "report.py",
        ]
        for f in files:
            if f.name == "version.py":
                continue
            with self.subTest(file=f.name):
                self.assertEqual(literal_version(f), version.VERSION)

    def test_android_app_and_buildozer_follow_the_release(self):
        main_src = (REPO / "android" / "main.py").read_text(encoding="utf-8")
        self.assertIn(f'APP_VERSION = "{version.VERSION}"', main_src)
        spec = (REPO / "android" / "buildozer.spec").read_text(encoding="utf-8")
        self.assertRegex(spec, rf"(?m)^version = {re.escape(version.VERSION)}$")

    def test_all_components_match(self):
        self.assertEqual(version.mismatches(), [])
        self.assertIn("MISMATCH", "MISMATCH" if version.mismatches({"main": "0.0.1"}) else "")

    def test_stale_file_is_reported(self):
        import providers.lp as lp
        with mock.patch.object(lp, "__version__", "0.7"):
            self.assertEqual(version.mismatches(), ["providers.lp (0.7)"])
            self.assertIn("MISMATCH", version.summary())
        with mock.patch.object(lp, "__version__", None, create=True):
            self.assertEqual(version.mismatches(), ["providers.lp (?)"])


if __name__ == "__main__":
    unittest.main()
