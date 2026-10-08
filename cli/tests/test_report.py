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
