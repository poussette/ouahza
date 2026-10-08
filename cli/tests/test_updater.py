import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

from providers import updater  # noqa: E402

REPO = "poussette/ouahza"
BASE = f"https://github.com/{REPO}/releases/download/v0.9.8/"


def release(tag="v0.9.8", assets=None, **kw):
    d = {
        "tag_name": tag, "draft": False, "prerelease": False, "body": "Nouveautés\n- a\n- b",
        "assets": assets if assets is not None else [
            {"name": "Ouahza-0.9.8-arm64.apk", "browser_download_url": BASE + "Ouahza-0.9.8-arm64.apk"},
            {"name": "Ouahza-0.9.8-universal.apk", "browser_download_url": BASE + "Ouahza-0.9.8-universal.apk"},
        ],
    }
    d.update(kw)
    return d


def run(data, current="0.9.7", repo=REPO, variant="universal"):
    with mock.patch.object(updater, "request_json", return_value=data) as m:
        return updater.check(current, repo, variant), m


class TestUpdater(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v0.9.8"), (0, 9, 8))
        self.assertEqual(updater.parse_version(" 1.10.0 "), (1, 10, 0))
        for bad in ("0.9", "v1.2.3-beta", "latest", "", None, 5, "1.2.3\n; rm"):
            self.assertIsNone(updater.parse_version(bad))

    def test_newer_offers_universal_apk(self):
        info, m = run(release())
        self.assertEqual(info["version"], "0.9.8")
        self.assertTrue(info["url"].endswith("universal.apk"))
        self.assertIn("/releases/tag/v0.9.8", info["page"])
        self.assertNotIn("\n", info["notes"])
        self.assertEqual(m.call_args[0][1], f"https://api.github.com/repos/{REPO}/releases/latest")

    def test_variant_follows_the_installed_build(self):
        info, _ = run(release(), variant="arm64")
        self.assertTrue(info["url"].endswith("-arm64.apk"))
        info, _ = run(release(), variant="universal")
        self.assertTrue(info["url"].endswith("-universal.apk"))
        info, _ = run(release(), variant="bogus")   # unknown -> universal
        self.assertTrue(info["url"].endswith("-universal.apk"))

    def test_variant_detected_from_apk_entries(self):
        v = updater.variant_from_apk_entries
        self.assertEqual(v(["lib/arm64-v8a/libpython.so", "classes.dex"]), "arm64")
        self.assertEqual(v(["lib/arm64-v8a/a.so", "lib/armeabi-v7a/a.so"]), "universal")
        self.assertEqual(v(["lib/armeabi-v7a/a.so"]), "universal")
        self.assertIsNone(v(["classes.dex"]))

    def test_missing_variant_falls_back_to_release_page(self):
        only_universal = [{"name": "Ouahza-0.9.8-universal.apk", "browser_download_url": BASE + "Ouahza-0.9.8-universal.apk"}]
        info, _ = run(release(assets=only_universal), variant="arm64")
        self.assertEqual(info["url"], info["page"])

    def test_numeric_not_lexicographic(self):
        info, _ = run(release("v0.10.0"), current="0.9.9")
        self.assertEqual(info["version"], "0.10.0")

    def test_same_or_older_is_none(self):
        self.assertIsNone(run(release("v0.9.7"))[0])
        self.assertIsNone(run(release("v0.9.6"))[0])

    def test_draft_prerelease_and_junk(self):
        self.assertIsNone(run(release(draft=True))[0])
        self.assertIsNone(run(release(prerelease=True))[0])
        self.assertIsNone(run(release("nightly"))[0])
        self.assertIsNone(run(["not", "a", "dict"])[0])

    def test_foreign_asset_links_are_ignored(self):
        evil = [{"name": "x-universal.apk", "browser_download_url": "https://evil.example/x-universal.apk"},
                {"name": "y.apk", "browser_download_url": "http://github.com/%s/releases/download/v0.9.8/y.apk" % REPO}]
        info, _ = run(release(assets=evil))
        self.assertEqual(info["url"], info["page"])  # falls back to the release page

    def test_no_assets_falls_back_to_page(self):
        info, _ = run(release(assets=[]))
        self.assertEqual(info["url"], info["page"])

    def test_bad_repo_or_current_never_calls_network(self):
        for repo in ("../../etc/passwd", "a/b/c", "x", "a b/c", "a/..", ""):
            with mock.patch.object(updater, "request_json") as m:
                self.assertIsNone(updater.check("0.9.7", repo))
                m.assert_not_called()
        with mock.patch.object(updater, "request_json") as m:
            self.assertIsNone(updater.check("dev", REPO))
            m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
