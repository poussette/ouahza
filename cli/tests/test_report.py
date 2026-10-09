"""Last-report persistence and price carry-over (android/report.py)."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "android"))

import report  # noqa: E402
from providers.base import TokenBalance, WalletBalance  # noqa: E402

A = "erd1" + "a" * 58


def old_wallet():
    w = WalletBalance("multiversx", A, "EGLD", native_amount=2.0, native_usd_value=80.0,
                      native_eur_value=72.0, total_usd=180.0, total_eur=162.0, label="L")
    w.tokens = [TokenBalance("X", "x", 10.0, contract="X-1", usd_value=100.0, eur_value=90.0, asset_type="esdt")]
    return w


class ReportTests(unittest.TestCase):
    def test_roundtrip(self):
        out = report.load_results(report.dump_results([old_wallet()], True, 5.0))
        self.assertIsNotNone(out)
        res, ok, ts = out
        self.assertTrue(ok)
        self.assertEqual(ts, 5.0)
        self.assertEqual(res[0].tokens[0].usd_value, 100.0)
        self.assertEqual(res[0].label, "L")

    def test_bad_input(self):
        for bad in ("", "garbage", "[]", '{"format": 99}', '{"format": 1}', '{"format": 1, "wallets": [1]}'):
            self.assertIsNone(report.load_results(bad))

    def test_unknown_fields_ignored(self):
        import json
        doc = json.loads(report.dump_results([old_wallet()], True, 1.0))
        doc["wallets"][0]["future_field"] = 1
        doc["wallets"][0]["tokens"][0]["other"] = 2
        self.assertIsNotNone(report.load_results(json.dumps(doc)))

    def test_carry_prices_scales_with_amount(self):
        new = WalletBalance("multiversx", A, "EGLD", native_amount=3.0)
        new.tokens = [TokenBalance("X", "x", 20.0, contract="X-1", asset_type="esdt"),
                      TokenBalance("Y", "y", 5.0, contract="Y-1", asset_type="esdt")]
        self.assertTrue(report.carry_prices([new], [old_wallet()]))
        self.assertAlmostEqual(new.native_usd_value, 120.0)
        self.assertAlmostEqual(new.tokens[0].eur_value, 180.0)
        self.assertIsNone(new.tokens[1].usd_value)       # unknown before: stays unpriced
        self.assertAlmostEqual(new.total_eur, 108.0 + 180.0)

    def test_carry_prices_unknown_wallet(self):
        other = WalletBalance("multiversx", "erd1" + "b" * 58, "EGLD", native_amount=1.0)
        self.assertFalse(report.carry_prices([other], [old_wallet()]))
        self.assertIsNone(other.total_usd)


if __name__ == "__main__":
    unittest.main()


class RetryTransientTests(unittest.TestCase):
    def test_only_passing_errors_are_retried(self):
        self.assertTrue(report._is_transient("HTTP 429 from api.multiversx.com"))
        self.assertTrue(report._is_transient("HTTP 503 from x"))
        self.assertTrue(report._is_transient("Read timed out"))
        self.assertFalse(report._is_transient("Invalid MultiversX address format."))
        self.assertFalse(report._is_transient("HTTP 404 from x"))
        self.assertFalse(report._is_transient(None))

    def test_failed_wallet_is_retried_once_and_bad_address_is_not(self):
        from unittest import mock
        calls = []

        def fake(label, chain, addr):
            calls.append(addr)
            if addr == "flaky" and calls.count("flaky") == 1:
                return WalletBalance("multiversx", addr, "EGLD", error="HTTP 429 from api.multiversx.com")
            if addr == "bad":
                return WalletBalance("multiversx", addr, "EGLD", error="Invalid MultiversX address format.")
            return WalletBalance("multiversx", addr, "EGLD", native_amount=1.0)

        entries = [(None, None, "ok"), (None, None, "flaky"), (None, None, "bad")]
        with mock.patch.object(report, "resolve_wallet", fake), \
                mock.patch.object(report, "RETRY_PASS_DELAYS", (0, 0)):
            res = report.fetch_all(entries, workers=2)
        self.assertEqual([bool(w.error) for w in res], [False, False, True])
        self.assertEqual(calls.count("flaky"), 2)
        self.assertEqual(calls.count("bad"), 1)


class IncompleteWalletTests(unittest.TestCase):
    def wallet(self, warning=None, error=None, n=0):
        w = WalletBalance("multiversx", A, "EGLD", native_amount=1.0, warning=warning, error=error)
        w.tokens = [TokenBalance("X", "x", 1.0, contract="X-1", asset_type="esdt")] * n
        return w

    def test_what_counts_as_incomplete(self):
        self.assertTrue(report.is_incomplete(self.wallet("could not fetch ESDT/MetaESDT tokens: HTTP 429 from api.multiversx.com")))
        self.assertTrue(report.is_incomplete(self.wallet("Etherscan error, tokens skipped: timed out")))
        self.assertFalse(report.is_incomplete(self.wallet("asset list truncated at 3000 entries")))
        self.assertFalse(report.is_incomplete(self.wallet("could not fetch NFTs/SFTs: HTTP 404 from x")))   # permanent
        self.assertFalse(report.is_incomplete(self.wallet(error="HTTP 429")))                              # an error, not partial
        self.assertFalse(report.is_incomplete(self.wallet()))

    def test_partial_wallet_is_read_again_and_replaced_when_complete(self):
        from unittest import mock
        bad = "could not fetch ESDT/MetaESDT tokens: HTTP 429 from api.multiversx.com"
        calls = []

        def fake(label, chain, addr):
            calls.append(addr)
            if addr == "lp" and calls.count("lp") == 1:
                return self.wallet(bad, n=0)
            return self.wallet(None, n=3)

        entries = [(None, None, "lp"), (None, None, "fine")]
        with mock.patch.object(report, "resolve_wallet", fake), \
                mock.patch.object(report, "RETRY_PASS_DELAYS", (0, 0)):
            res = report.fetch_all(entries, workers=2)
        self.assertEqual(len(res[0].tokens), 3)
        self.assertIsNone(res[0].warning)
        self.assertEqual(calls.count("lp"), 2)
        self.assertEqual(calls.count("fine"), 1)
        self.assertEqual(report.count_incomplete(res), 0)

    def test_still_partial_after_two_retries_is_counted(self):
        from unittest import mock
        bad = "could not fetch ESDT/MetaESDT tokens: HTTP 429 from api.multiversx.com"
        calls = []

        def fake(label, chain, addr):
            calls.append(addr)
            return self.wallet(bad)

        with mock.patch.object(report, "resolve_wallet", fake), \
                mock.patch.object(report, "RETRY_PASS_DELAYS", (0, 0)):
            res = report.fetch_all([(None, None, "lp")], workers=1)
        self.assertEqual(calls.count("lp"), 3)             # first read + two retries
        self.assertEqual(report.count_incomplete(res), 1)


class WorkersTests(unittest.TestCase):
    def test_app_reads_two_wallets_at_a_time(self):
        from unittest import mock
        import threading
        self.assertEqual(report.APP_WORKERS, 2)
        live, peak, lock = [0], [0], threading.Lock()

        def fake(label, chain, addr):
            import time
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            with lock:
                live[0] -= 1
            return WalletBalance("multiversx", A, "EGLD", native_amount=1.0)

        with mock.patch.object(report, "resolve_wallet", fake):
            report.fetch_all([(None, None, str(i)) for i in range(8)], workers=report.APP_WORKERS)
        self.assertEqual(peak[0], 2)
