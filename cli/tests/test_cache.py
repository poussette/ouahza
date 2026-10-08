"""Price cache, net statistics and prefetch."""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import requests

import pricing
from providers import net
from providers.base import TokenBalance, WalletBalance
from test_security import FakeResp, router

net.THROTTLE_ENABLED = False

ETH = "0x" + "ab" * 20
MINT = "So11111111111111111111111111111111111111112"


def counting(routes):
    calls = []
    inner = router(routes)

    def fake(method, url, **kw):
        calls.append(url)
        return inner(method, url, **kw)
    fake.calls = calls
    return fake


class CacheTests(unittest.TestCase):
    def setUp(self):
        pricing.clear_cache()
        net.reset_stats()

    def test_native_second_call_uses_cache(self):
        fake = counting({"simple/price": FakeResp({"ethereum": {"usd": 2000, "eur": 1800}})})
        with mock.patch("requests.request", fake):
            a = pricing._fetch_native_prices({"ethereum"})
            b = pricing._fetch_native_prices({"ethereum"})
        self.assertEqual(a, b)
        self.assertEqual(a["ethereum"]["usd"], 2000)
        self.assertEqual(len(fake.calls), 1)

    def test_native_only_missing_ids_requested(self):
        fake = counting({"simple/price": lambda url, kw: FakeResp(
            {g: {"usd": 1, "eur": 1} for g in kw["params"]["ids"].split(",")})})
        with mock.patch("requests.request", fake):
            pricing._fetch_native_prices({"ethereum"})
            both = pricing._fetch_native_prices({"ethereum", "solana"})
        self.assertEqual(set(both), {"ethereum", "solana"})
        self.assertEqual(len(fake.calls), 2)

    def test_failure_is_not_cached(self):
        def boom(*a, **k):
            raise requests.ConnectionError("down")
        with mock.patch("requests.request", boom), mock.patch.object(net, "RETRY_DELAYS", ()):
            self.assertEqual(pricing._fetch_native_prices({"ethereum"}), {})
        fake = counting({"simple/price": FakeResp({"ethereum": {"usd": 5, "eur": 4}})})
        with mock.patch("requests.request", fake):
            self.assertEqual(pricing._fetch_native_prices({"ethereum"})["ethereum"]["usd"], 5)

    def test_ttl_expiry(self):
        fake = counting({"simple/price": FakeResp({"ethereum": {"usd": 5, "eur": 4}})})
        with mock.patch("requests.request", fake):
            pricing._fetch_native_prices({"ethereum"})
            with mock.patch.object(pricing, "PRICE_TTL", 0.0):
                pricing._fetch_native_prices({"ethereum"})
        self.assertEqual(len(fake.calls), 2)

    def test_unpriced_token_not_asked_again(self):
        fake = counting({"token_price": FakeResp({MINT: {"usd": 150, "eur": 140}})})
        other = "Z" * 44
        with mock.patch("requests.request", fake):
            a = pricing._fetch_token_prices("solana", {MINT, other})
            b = pricing._fetch_token_prices("solana", {MINT, other})
        self.assertIn(MINT, a)
        self.assertNotIn(other, a)
        self.assertEqual(a.keys(), b.keys())
        self.assertEqual(len(fake.calls), 1)

    def test_mex_prices_cached_and_copy_is_safe(self):
        fake = counting({"mex-tokens": FakeResp([{"id": "MEX-455c57", "price": 0.5}])})
        with mock.patch("requests.request", fake):
            a = pricing._fetch_mex_tokens_prices()
            a["POISON"] = 1.0
            b = pricing._fetch_mex_tokens_prices()
        self.assertNotIn("POISON", b)
        self.assertEqual(b["MEX-455c57"], 0.5)
        self.assertEqual(len(fake.calls), 1)

    def test_fx_cached(self):
        fake = counting({"latest": FakeResp({"rates": {"EUR": 0.9}})})
        with mock.patch("requests.request", fake):
            self.assertEqual(pricing._fetch_usd_eur_rate(), 0.9)
            self.assertEqual(pricing._fetch_usd_eur_rate(), 0.9)
        self.assertEqual(len(fake.calls), 1)

    def test_token_info_negative_cached(self):
        fake = counting({"/tokens": FakeResp([{"identifier": "AAA-111111", "price": 2, "decimals": 18}])})
        with mock.patch("requests.request", fake):
            a = pricing._fetch_token_info({"AAA-111111", "BBB-222222"})
            b = pricing._fetch_token_info({"AAA-111111", "BBB-222222"})
        self.assertEqual(set(a), {"AAA-111111"})
        self.assertEqual(a, b)
        self.assertEqual(len(fake.calls), 1)

    def test_prefetch_warms_cache_for_apply_pricing(self):
        fake = counting({
            "simple/price": FakeResp({"ethereum": {"usd": 2000, "eur": 1800}}),
        })
        with mock.patch("requests.request", fake):
            pricing.prefetch({"ethereum"})
            deadline = time.time() + 3
            while not fake.calls and time.time() < deadline:
                time.sleep(0.01)
            w = WalletBalance("ethereum", ETH, "ETH", native_amount=1.0)
            self.assertTrue(pricing.apply_pricing([w]))
        self.assertEqual(w.total_usd, 2000)
        self.assertEqual(len(fake.calls), 1)   # single flight: no duplicate call
        self.assertIn("prix", pricing.LAST_TIMINGS)
        self.assertIn("lp", pricing.LAST_TIMINGS)

    def test_second_apply_pricing_makes_no_price_call(self):
        fake = counting({
            "simple/price": FakeResp({"ethereum": {"usd": 2000, "eur": 1800}}),
            "token_price": FakeResp({("0x" + "cd" * 20): {"usd": 2, "eur": 1.8}}),
        })

        def run():
            w = WalletBalance("ethereum", ETH, "ETH", native_amount=1.0)
            w.tokens.append(TokenBalance("X", "x", 10.0, contract="0x" + "cd" * 20))
            pricing.apply_pricing([w])
            return w
        with mock.patch("requests.request", fake):
            first = run()
            n = len(fake.calls)
            second = run()
        self.assertEqual(len(fake.calls), n)
        self.assertEqual(first.total_usd, second.total_usd)
        self.assertEqual(second.total_usd, 2020)


class StatsTests(unittest.TestCase):
    def setUp(self):
        net.reset_stats()

    def test_counts_and_summary(self):
        fake = router({"a.test": FakeResp({"ok": 1})})
        with mock.patch("requests.request", fake):
            net.request_json("GET", "https://a.test/x")
            net.request_json("GET", "https://a.test/y")
        self.assertEqual(net.STATS["a.test"]["calls"], 2)
        self.assertIn("a.test 2 appel(s)", net.stats_summary())

    def test_429_counted(self):
        seq = iter([FakeResp({}, status=429), FakeResp({"ok": 1})])
        with mock.patch("requests.request", lambda *a, **k: next(seq)), \
                mock.patch.object(net, "_sleep", lambda s: None), \
                mock.patch.object(net, "RETRY_DELAYS", (0.0,)):
            net.request_json("GET", "https://b.test/")
        self.assertEqual(net.STATS["b.test"]["r429"], 1)
        self.assertIn("1×429", net.stats_summary())


if __name__ == "__main__":
    unittest.main()
