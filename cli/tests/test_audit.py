"""Regression tests for the 0.1.34 security audit (one test per finding).

Everything here is offline: network calls are mocked. Each test names the
defect it pins down, so a failure points at the weakness that came back.
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "core"))
sys.path.insert(0, str(HERE.parent.parent / "android"))

import report  # noqa: E402
from providers import bitcoin, ethereum, lp, multiversx, net, safe, solana, updater  # noqa: E402
from providers.base import TokenBalance, WalletBalance  # noqa: E402
from test_security import FakeResp  # noqa: E402

net.THROTTLE_ENABLED = False
net.RETRY_DELAYS = ()

ADDR = "erd1" + "a" * 58


# ------------------------------------------------------------------ net.py
class RedirectHardening(unittest.TestCase):
    """A public server must not be able to steer a request somewhere private."""

    def run_chain(self, first_url, hops, *, method="GET", body=None, allow_loopback_http=False):
        calls = []
        seq = iter(hops)

        def fake(m, url, **kw):
            calls.append((m, url, kw.get("json"), kw.get("headers")))
            return next(seq)

        with mock.patch("requests.request", fake):
            try:
                out = net.request_json(method, first_url, json_body=body, headers={"X-Api-Key": "K"},
                                       allow_loopback_http=allow_loopback_http)
            except net.HttpError as exc:
                return calls, exc
        return calls, out

    def test_public_server_cannot_redirect_to_a_loopback_http_service(self):
        calls, err = self.run_chain(
            "https://node.example.org/x",
            [FakeResp(status=302, headers={"Location": "http://127.0.0.1:8080/admin"})],
            allow_loopback_http=True,          # the app allows a *local node*, not a bounce to it
        )
        self.assertIsInstance(err, net.HttpError)
        self.assertEqual(len(calls), 1)

    def test_redirect_between_loopback_nodes_still_works(self):
        calls, out = self.run_chain(
            "http://localhost:8545/",
            [FakeResp(status=307, headers={"Location": "http://localhost:8546/"}), FakeResp({"ok": 1})],
            allow_loopback_http=True,
        )
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(len(calls), 2)

    def test_public_server_cannot_redirect_to_a_private_ip(self):
        for target in ("https://169.254.169.254/latest/meta-data", "https://10.0.0.5/", "https://192.168.1.1/",
                       "https://[::1]/", "https://[fd00::1]/"):
            calls, err = self.run_chain("https://api.example.org/x",
                                        [FakeResp(status=302, headers={"Location": target})])
            self.assertIsInstance(err, net.HttpError, target)
            self.assertEqual(len(calls), 1, target)

    def test_a_private_node_may_redirect_inside_its_own_network(self):
        calls, out = self.run_chain(
            "https://192.168.1.10/x",
            [FakeResp(status=302, headers={"Location": "https://192.168.1.11/x"}), FakeResp({"ok": 1})],
        )
        self.assertEqual(out, {"ok": 1})

    def test_request_body_is_never_replayed_to_another_host(self):
        calls, err = self.run_chain(
            "https://rpc.example.org/", [FakeResp(status=307, headers={"Location": "https://other.example.net/"})],
            method="POST", body={"jsonrpc": "2.0", "method": "eth_getBalance"},
        )
        self.assertIsInstance(err, net.HttpError)
        self.assertEqual(len(calls), 1)               # other.example.net never contacted

    def test_same_host_307_keeps_method_and_body(self):
        calls, out = self.run_chain(
            "https://rpc.example.org/a",
            [FakeResp(status=307, headers={"Location": "/b"}), FakeResp({"ok": 1})],
            method="POST", body={"x": 1},
        )
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(calls[1][0], "POST")
        self.assertEqual(calls[1][2], {"x": 1})

    def test_get_may_follow_a_cross_host_https_redirect_without_credentials(self):
        calls, out = self.run_chain(
            "https://a.example.org/x",
            [FakeResp(status=302, headers={"Location": "https://b.example.org/y"}), FakeResp({"ok": 1})],
        )
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(calls[0][3], {"X-Api-Key": "K"})
        self.assertIsNone(calls[1][3])

    def test_malformed_redirect_target_is_refused_not_crashing(self):
        calls, err = self.run_chain("https://a.example.org/x",
                                    [FakeResp(status=302, headers={"Location": "https://[bad/"})])
        self.assertIsInstance(err, net.HttpError)


# ------------------------------------------------------------------ safe.py
class UrlAndValueHygiene(unittest.TestCase):
    def test_rpc_url_with_stray_characters_is_rejected(self):
        for bad in ("https://node.example.org/\nX-Evil: 1", "https://node.example.org/ path", "https://nodé.example.org/",
                    "https://node.example.org/\x00", "https://node.example.org/‮"):
            self.assertIsNone(safe.validate_rpc_url(bad), repr(bad))
        self.assertEqual(safe.validate_rpc_url("  https://node.example.org/v1/key  "), "https://node.example.org/v1/key")
        self.assertEqual(safe.validate_rpc_url("http://127.0.0.1:8545"), "http://127.0.0.1:8545")

    def test_safe_value(self):
        self.assertEqual(safe.safe_value(-12.5), -12.5)         # a borrow is negative
        self.assertEqual(safe.safe_value(3), 3.0)
        for bad in ("1", None, True, float("nan"), float("inf"), 1e30, [], {}):
            self.assertIsNone(safe.safe_value(bad), repr(bad))

    def test_sanitize_wallet_drops_non_numeric_values(self):
        w = WalletBalance("multiversx", ADDR, "EGLD", native_amount=1.0, native_usd_value="9", total_usd=float("nan"),
                          total_eur=5.0)
        w.tokens = [TokenBalance("A", "a", 1.0, usd_value="x", eur_value=float("inf")),
                    TokenBalance("B", "b", 1.0, usd_value=-4.0, eur_value=-3.6)]
        safe.sanitize_wallet(w)
        self.assertIsNone(w.native_usd_value)
        self.assertIsNone(w.total_usd)
        self.assertEqual(w.total_eur, 5.0)
        self.assertIsNone(w.tokens[0].usd_value)
        self.assertIsNone(w.tokens[0].eur_value)
        self.assertEqual(w.tokens[1].usd_value, -4.0)


class PrivateWrite(unittest.TestCase):
    def test_file_is_0600_atomic_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "f.json")
            safe.write_private(p, "one")
            safe.write_private(p, "two")
            self.assertEqual(Path(p).read_text(), "two")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode), 0o700)
            self.assertEqual(os.listdir(os.path.dirname(p)), ["f.json"])

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "symlink"), "needs O_NOFOLLOW")
    def test_a_symlink_planted_at_the_temp_name_is_not_followed(self):
        with tempfile.TemporaryDirectory() as d:
            victim = os.path.join(d, "victim.txt")
            Path(victim).write_text("keep me")
            p = os.path.join(d, "f.json")
            os.symlink(victim, p + ".tmp")
            with self.assertRaises(OSError):
                safe.write_private(p, "attacker-controlled path")
            self.assertEqual(Path(victim).read_text(), "keep me")

    def test_caches_use_it(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mvx_empty.json")
            cache = multiversx.EmptyCache(p)
            cache.mark(ADDR, "legacy", True)
            cache.save()
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)


# --------------------------------------------------------------- updater.py
class UpdaterUrls(unittest.TestCase):
    REPO = "poussette/ouahza"
    GOOD = "https://github.com/poussette/ouahza/releases/download/v9.9.9/Ouahza-9.9.9-universal.apk"

    def release(self, url):
        return {"tag_name": "v9.9.9", "draft": False, "prerelease": False, "body": "n",
                "assets": [{"name": "Ouahza-9.9.9-universal.apk", "browser_download_url": url}]}

    def check(self, doc):
        with mock.patch.object(updater, "request_json", return_value=doc):
            return updater.check("0.1.0", self.REPO, "universal")

    def test_release_url_accepts_only_plain_github_release_links(self):
        self.assertTrue(updater.is_release_url(self.GOOD))
        self.assertTrue(updater.is_release_url("https://github.com/poussette/ouahza/releases/tag/v1.2.3"))
        for bad in (self.GOOD + "\n", self.GOOD + "\x00", self.GOOD + " x", self.GOOD + "?a=b", self.GOOD + "#f",
                    "http://github.com/poussette/ouahza/releases/download/v1/a.apk",
                    "https://github.com@evil.example/poussette/ouahza/releases/download/v1/a.apk",
                    "https://github.com/poussette/ouahza/releases/download/../../x.apk",
                    "https://github.com/poussette/ouahza//releases/download/v1/a.apk",
                    "https://evil.example/https://github.com/a/b/releases/tag/v1",
                    "https://github.com/a/b/issues/1", "javascript:alert(1)", None, 5, ["x"]):
            self.assertFalse(updater.is_release_url(bad), repr(bad))

    def test_asset_url_with_control_characters_is_not_offered(self):
        for tail in ("\n", "\x00", " ", "\r\n"):
            info = self.check(self.release(self.GOOD + tail))
            self.assertIsNotNone(info)
            self.assertNotIn(tail, info["url"])
            self.assertTrue(updater.is_release_url(info["url"]))     # falls back to the release page

    def test_a_good_asset_is_still_picked(self):
        self.assertEqual(self.check(self.release(self.GOOD))["url"], self.GOOD)

    def test_fallback_check_reads_headers_only(self):
        seen = {}

        def fake_get(url, **kw):
            seen.update(kw)
            return FakeResp(status=302, headers={"Location": "/poussette/ouahza/releases/tag/v9.9.9"})

        with mock.patch("requests.get", fake_get):
            info = updater._check_via_redirect((0, 1, 0), self.REPO, "universal")
        self.assertTrue(seen.get("stream"))
        self.assertTrue(updater.is_release_url(info["url"]))


# ------------------------------------------- update integrity (SHA-256)
class UpdateIntegrity(unittest.TestCase):
    REPO = "poussette/ouahza"
    BASE = "https://github.com/poussette/ouahza/releases/download/v9.9.9/"
    APK = "Ouahza-9.9.9-universal.apk"
    DATA = b"pretend apk bytes" * 1000

    def digest(self, data=None):
        import hashlib
        return hashlib.sha256(self.DATA if data is None else data).hexdigest()

    def info(self, **kw):
        d = {"version": "9.9.9", "url": self.BASE + self.APK, "page": "https://github.com/x/y/releases/tag/v9.9.9",
             "notes": "", "sums": self.BASE + "SHA256SUMS.txt"}
        d.update(kw)
        return d

    def test_parse_real_sha256sum_output(self):
        import shutil
        import subprocess
        if not shutil.which("sha256sum"):
            self.skipTest("no sha256sum")
        with tempfile.TemporaryDirectory() as d:
            for name, data in ((self.APK, self.DATA), ("Ouahza-9.9.9-arm64.apk", b"other")):
                Path(d, name).write_bytes(data)
            out = subprocess.run("sha256sum *.apk", shell=True, cwd=d, capture_output=True, text=True, check=True).stdout
        parsed = updater.parse_sha256sums(out)
        self.assertEqual(parsed[self.APK], self.digest())
        self.assertEqual(parsed["Ouahza-9.9.9-arm64.apk"], self.digest(b"other"))

    def test_parse_ignores_junk_and_keeps_the_first_entry(self):
        good, evil = "a" * 64, "b" * 64
        text = f"{good}  one.apk\n{evil}  one.apk\nnot a line\n{good[:-1]}  short.apk\n{good} *star.apk\n{good}  ../x.apk\n"
        parsed = updater.parse_sha256sums(text)
        self.assertEqual(parsed, {"one.apk": good, "star.apk": good})
        self.assertEqual(updater.parse_sha256sums(None), {})

    def test_sha256_of_and_comparison(self):
        import io
        self.assertEqual(updater.sha256_of(io.BytesIO(self.DATA)), self.digest())
        with self.assertRaises(ValueError):
            updater.sha256_of(io.BytesIO(b"x" * 100), max_bytes=10)
        self.assertTrue(updater.same_digest(self.digest(), self.digest().upper()))
        for bad in (None, "", "abc", self.digest()[:-1], 5, self.digest() + "0"):
            self.assertFalse(updater.same_digest(self.digest(), bad), repr(bad))
        self.assertFalse(updater.same_digest(self.digest(), "0" * 64))

    def test_expected_sha256_reads_the_published_listing(self):
        listing = f"{self.digest()}  {self.APK}\n{'c' * 64}  Ouahza-9.9.9-arm64.apk\n"
        seen = {}

        def fake(method, url, **kw):
            seen["url"], seen["max"] = url, kw.get("max_bytes")
            return listing

        with mock.patch.object(updater, "request_text", fake):
            self.assertEqual(updater.expected_sha256(self.info()), self.digest())
            self.assertIsNone(updater.expected_sha256(self.info(url=self.BASE + "Other-9.9.9-universal.apk")))
        self.assertEqual(seen["url"], self.BASE + "SHA256SUMS.txt")
        self.assertLessEqual(seen["max"], 100_000)

    def test_no_listing_no_expectation_and_no_request(self):
        with mock.patch.object(updater, "request_text", side_effect=AssertionError("must not fetch")):
            for info in (None, {}, self.info(sums=None), self.info(sums="http://github.com/x/y/releases/download/v1/S"),
                         self.info(sums="https://evil.example/SHA256SUMS.txt"), self.info(url="https://evil.example/a.apk"),
                         self.info(url=self.BASE + "page")):
                self.assertIsNone(updater.expected_sha256(info), repr(info))

    def test_listing_that_cannot_be_fetched_raises_httperror(self):
        with mock.patch.object(updater, "request_text", side_effect=net.HttpError("HTTP 404 from github.com")):
            with self.assertRaises(net.HttpError):
                updater.expected_sha256(self.info())

    def test_check_offers_the_listing_of_the_release(self):
        release = {"tag_name": "v9.9.9", "draft": False, "prerelease": False, "body": "n", "assets": [
            {"name": self.APK, "browser_download_url": self.BASE + self.APK},
            {"name": "SHA256SUMS.txt", "browser_download_url": self.BASE + "SHA256SUMS.txt"}]}
        with mock.patch.object(updater, "request_json", return_value=release):
            self.assertEqual(updater.check("0.1.0", self.REPO, "universal")["sums"], self.BASE + "SHA256SUMS.txt")
        release["assets"][1]["browser_download_url"] = "https://evil.example/SHA256SUMS.txt"
        with mock.patch.object(updater, "request_json", return_value=release):
            self.assertIsNone(updater.check("0.1.0", self.REPO, "universal")["sums"])
        release["assets"].pop(1)          # a release published before this feature
        with mock.patch.object(updater, "request_json", return_value=release):
            self.assertIsNone(updater.check("0.1.0", self.REPO, "universal")["sums"])

    def test_fallback_check_points_at_the_listing_too(self):
        def fake_get(url, **kw):
            return FakeResp(status=302, headers={"Location": "/poussette/ouahza/releases/tag/v9.9.9"})

        with mock.patch("requests.get", fake_get):
            info = updater._check_via_redirect((0, 1, 0), self.REPO, "universal")
        self.assertEqual(info["sums"], self.BASE + "SHA256SUMS.txt")
        self.assertTrue(updater.is_release_url(info["sums"]))

    def test_request_text_is_capped_https_only_and_decoded(self):
        with mock.patch("requests.request", lambda *a, **k: FakeResp(body="héllo\n".encode())):
            self.assertEqual(net.request_text("GET", "https://ok.test/"), "héllo\n")
        with mock.patch("requests.request", lambda *a, **k: FakeResp(body=b"x" * 5000)):
            with self.assertRaises(net.HttpError):
                net.request_text("GET", "https://ok.test/", max_bytes=1000)
        with self.assertRaises(net.HttpError):
            net.request_text("GET", "http://ok.test/")

    def test_workflow_publishes_the_listing_with_the_apks(self):
        text = (HERE.parent.parent / ".github" / "workflows" / "build-apk.yml").read_text(encoding="utf-8")
        self.assertIn("sha256sum *.apk > SHA256SUMS.txt", text)
        self.assertEqual(text.count("dist/SHA256SUMS.txt"), 2)        # release create AND upload
        try:
            import yaml
        except ImportError:
            return
        self.assertIn("release", yaml.safe_load(text)["jobs"])

    def test_app_checks_the_download_before_opening_the_installer(self):
        import ast
        src = (HERE.parent.parent / "android" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_on_download_done")
        body = ast.get_source_segment(src, fn)
        self.assertLess(body.index("_verify_download("), body.index("startActivity("))
        self.assertIn('"mismatch"', body)
        verify = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_verify_download")
        vsrc = ast.get_source_segment(src, verify)
        for needle in ("expected_sha256", "sha256_of", "same_digest", "detachFd"):
            self.assertIn(needle, vsrc)


class AppVerifyGlue(unittest.TestCase):
    """Runs the app's real `_verify_download` (extracted from android/main.py, which
    needs Kivy) against a fake Android activity backed by a real file descriptor."""

    @classmethod
    def setUpClass(cls):
        import ast
        import textwrap
        src = (HERE.parent.parent / "android" / "main.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_verify_download")
        ns = {"os": os}
        exec(textwrap.dedent(ast.get_source_segment(src, fn)), ns)
        cls.verify = staticmethod(ns["_verify_download"])

    class Pfd:
        def __init__(self, path):
            self.fd = os.open(path, os.O_RDONLY)

        def detachFd(self):
            fd, self.fd = self.fd, None
            return fd

    class Activity:
        def __init__(self, path):
            self.path = path

        def getContentResolver(self):
            outer = self

            class CR:
                def openFileDescriptor(self, uri, mode):
                    assert mode == "r"
                    return AppVerifyGlue.Pfd(outer.path)
            return CR()

    class Me:
        pass

    def check(self, data, info, listing):
        with tempfile.TemporaryDirectory() as d:
            apk = os.path.join(d, "a.apk")
            Path(apk).write_bytes(data)
            me = self.Me()
            me._update_info = info
            with mock.patch.object(updater, "request_text", return_value=listing):
                return self.verify(me, self.Activity(apk), "content://x")

    def test_ok_mismatch_and_unknown(self):
        good = UpdateIntegrity.DATA
        digest = UpdateIntegrity().digest()
        info = UpdateIntegrity().info()
        listing = f"{digest}  {UpdateIntegrity.APK}\n"
        self.assertEqual(self.check(good, info, listing), "ok")
        self.assertEqual(self.check(good + b"tampered", info, listing), "mismatch")
        self.assertEqual(self.check(good, info, "no listing here\n"), "unknown")       # no entry for this APK
        self.assertEqual(self.check(good, UpdateIntegrity().info(sums=None), listing), "unknown")
        self.assertEqual(self.check(good, None, listing), "unknown")

    def test_a_failure_to_check_never_blocks_the_update(self):
        me = self.Me()
        me._update_info = UpdateIntegrity().info()
        with mock.patch.object(updater, "request_text", side_effect=net.HttpError("down")):
            self.assertEqual(self.verify(me, self.Activity("/nonexistent"), "content://x"), "unknown")


# ------------------------------------------------- untrusted-data robustness
class HostileResponses(unittest.TestCase):
    def test_token_type_that_is_not_text_does_not_crash(self):
        prov = multiversx.MultiversXProvider()
        for weird in ({}, [], [[]], 5, None):
            tok = {"identifier": "ABC-123456", "ticker": "ABC", "name": "n", "balance": "5", "decimals": 0, "type": weird}
            got = prov._to_token_balance(tok)
            self.assertIsNotNone(got, repr(weird))
            self.assertEqual(got.asset_type, "esdt")

    def test_trailing_newline_is_not_a_valid_address_or_hex(self):
        self.assertIsNone(multiversx._MVX_RE.fullmatch(ADDR + "\n"))
        self.assertIsNone(ethereum._ETH_RE.fullmatch("0x" + "a" * 40 + "\n"))
        self.assertIsNone(ethereum._HEX_RE.fullmatch("0x1f\n"))
        self.assertIsNone(solana._SOL_RE.fullmatch("A" * 44 + "\n"))
        self.assertIsNone(bitcoin._BTC_RE.fullmatch("bc1q" + "a" * 38 + "\n"))

    def test_etherscan_contract_with_newline_is_skipped(self):
        doc = {"status": "1", "result": [
            {"contractAddress": "0x" + "a" * 40 + "\n", "tokenSymbol": "BAD", "tokenName": "n", "tokenDecimal": "18"},
            {"contractAddress": "0x" + "b" * 40, "tokenSymbol": "OK", "tokenName": "n", "tokenDecimal": "18"}]}
        with mock.patch.object(ethereum, "request_json", return_value=doc):
            found = ethereum.EthereumProvider()._discover_token_contracts("0x" + "c" * 40)
        self.assertEqual(list(found), ["0x" + "b" * 40])

    def test_solana_mint_with_newline_is_skipped(self):
        def rpc(method, url, **kw):
            m = (kw.get("json_body") or {}).get("method")
            if m == "getBalance":
                return {"result": {"value": 10**9}}
            if m == "getTokenAccountsByOwner":
                good = "So11111111111111111111111111111111111111112"
                return {"result": {"value": [
                    {"account": {"data": {"parsed": {"info": {"mint": good + "\n", "tokenAmount": {"uiAmountString": "1"}}}}}},
                    {"account": {"data": {"parsed": {"info": {"mint": good, "tokenAmount": {"uiAmountString": "2"}}}}}}]}}
            return {"result": []}

        solana._token_list_cache = {}
        with mock.patch.object(solana, "request_json", side_effect=rpc):
            w = solana.SolanaProvider().get_balance("A" * 44)
        self.assertEqual([t.amount for t in w.tokens], [2.0])

    def test_bitcoin_infinite_or_absurd_numbers_become_an_error_line(self):
        for stats in ({"funded_txo_sum": 1e999}, {"funded_txo_sum": 10**40}):
            doc = {"chain_stats": stats, "mempool_stats": {}}
            with mock.patch.object(bitcoin, "request_json", return_value=doc):
                w = bitcoin.BitcoinProvider().get_balance("bc1q" + "a" * 38)
            self.assertTrue(w.error, stats)
            self.assertIsNone(w.native_amount)

    def test_solana_result_of_the_wrong_shape_does_not_crash(self):
        def rpc(method, url, **kw):
            m = (kw.get("json_body") or {}).get("method")
            if m == "getBalance":
                return {"result": {"value": 10**9}}
            if m == "getTokenAccountsByOwner":
                return {"result": ["not", "a", "dict"]}
            if m == "getProgramAccounts":
                return {"result": [{"pubkey": "S" * 40, "account": {"lamports": 5 * 10**9, "data": {"parsed": {"info": {
                    "meta": ["not a dict"], "stake": {"delegation": {"voter": "V", "activationEpoch": "1"}}}}}}}]}
            return {"result": {"epoch": 100}}

        solana._token_list_cache = {}
        with mock.patch.object(solana, "request_json", side_effect=rpc):
            w = solana.SolanaProvider().get_balance("A" * 44)
        self.assertIsNone(w.error)
        self.assertEqual(len(w.tokens), 1)

    def test_solana_infinite_lamports_become_an_error_line(self):
        with mock.patch.object(solana, "request_json", return_value={"result": {"value": 1e999}}):
            w = solana.SolanaProvider().get_balance("A" * 44)
        self.assertTrue(w.error)

    def test_beacon_balance_infinity_is_skipped(self):
        prov = ethereum.EthereumProvider()
        with mock.patch.object(prov, "_get_native_balance", return_value=1.0), \
             mock.patch.object(prov, "_discover_token_contracts", return_value={}), \
             mock.patch.object(prov, "_get_beacon_validators", return_value=[{"validatorindex": 7}]), \
             mock.patch.object(prov, "_get_beacon_balances", return_value=[{"validatorindex": 7, "balance": 1e999}]):
            w = prov.get_balance("0x" + "c" * 40)
        self.assertEqual([t for t in w.tokens if t.asset_type == "staked"], [])


class DamagedLocalFiles(unittest.TestCase):
    def test_lp_cache_with_unhashable_values_loads_empty(self):
        doc = {"v": 1,
               "pools": {"LP-abcdef": {"sc": ADDR, "spec": {"x": 1}}},
               "trusted": {"A" * 43 + "=": ["not", "a", "name"]}}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            Path(p).write_text(json.dumps(doc))
            cache = lp.LPCache(p)
        self.assertEqual(cache.pools, {})
        self.assertEqual(cache.trusted, {})

    def test_tampered_report_does_not_break_the_app_after_loading(self):
        w = WalletBalance("multiversx", ADDR, "EGLD", native_amount=1.0, native_usd_value=5.0, native_eur_value=4.0,
                          total_usd=10.0, total_eur=9.0, label="L")
        w.tokens = [TokenBalance("X", "x", 2.0, contract="X-123456", usd_value=5.0, eur_value=4.0, asset_type="esdt")]
        doc = json.loads(report.dump_results([w], True, time.time()))
        doc["wallets"][0]["total_eur"] = "a lot"
        doc["wallets"][0]["native_usd_value"] = [1]
        doc["wallets"][0]["tokens"][0]["usd_value"] = "x" * 5
        doc["wallets"][0]["tokens"][0]["eur_value"] = float("inf")
        out = report.load_results(json.dumps(doc))
        self.assertIsNotNone(out)
        results = out[0]
        fresh = [WalletBalance("multiversx", ADDR, "EGLD", native_amount=2.0, label="L")]
        report.carry_prices(fresh, results)
        report.compute_label_totals(results)
        report.format_table(results)
        report.build_json(results)
        report.build_csv(results)

    def test_label_totals_with_a_missing_eur_total(self):
        w = WalletBalance("multiversx", ADDR, "EGLD", total_usd=10.0, total_eur=None, label="L")
        totals = report.compute_label_totals([w])
        self.assertEqual(totals["L"]["usd"], 10.0)
        self.assertEqual(totals["L"]["eur"], 0.0)


if __name__ == "__main__":
    unittest.main()
