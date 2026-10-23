"""Android: a normal refresh never reads the transfer history (MVX_FLOWS=cache)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "core"))
sys.path.insert(0, os.path.dirname(__file__))

from providers import mvx_flows
from test_dataapi import Provider as _P, load
from test_mvx_catchup import _api, _history, ME


class CacheOnly(unittest.TestCase):
    def setUp(self):
        self.h = _P("test_account_call_replaced_and_public_list_read_when_a_token_is_unknown")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        mvx_flows.clear_cache()
        mvx_flows.set_cache_path(None)
        p = mock.patch.dict(os.environ, {"MVX_FLOWS": "cache"})
        p.start()
        self.addCleanup(p.stop)

    def _balance(self):
        from providers import multiversx
        with mock.patch("requests.request", side_effect=self.h.fake(load("dataapi_wallet_defi.json"))):
            return multiversx.MultiversXProvider().get_balance(_P_ADDR)

    def test_never_analysed_reads_nothing_and_says_so(self):
        with mock.patch.object(mvx_flows, "engaged_positions", side_effect=AssertionError("history read")):
            w = self._balance()
        self.assertFalse([t for t in w.tokens if t.asset_type == "locked-estimate"])
        self.assertIn("not analysed", w.warning or "")
        self.assertFalse([u for u in self.h.public_calls() if "/transfers" in u], self.h.public_calls())

    def test_after_deep_analysis_positions_show_without_any_read(self):
        items = _history(300)
        mvx_flows.catch_up(_P_ADDR, _api(items))
        with mock.patch.object(mvx_flows, "engaged_positions", side_effect=AssertionError("history read")):
            w = self._balance()
        locked = [t for t in w.tokens if t.asset_type == "locked-estimate"]
        self.assertEqual(len(locked), 1)
        self.assertAlmostEqual(locked[0].amount, len(items))
        self.assertNotIn("not analysed", w.warning or "")
        self.assertFalse([u for u in self.h.public_calls() if "/transfers" in u], self.h.public_calls())


from test_dataapi import ADDR as _P_ADDR  # noqa: E402

if __name__ == "__main__":
    unittest.main()
