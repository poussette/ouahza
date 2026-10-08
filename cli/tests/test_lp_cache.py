"""LP price reuse (15 min), known-pool fast path and batched token facts."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import pricing
from providers import lp
from test_lp import (E18, LPID, OTHER, POOL, WEGLD, Chain, FakeResp, pair_views, vm)

fetch = pricing._fetch_token_info


def price(chain, ids=(LPID,)):
    with mock.patch("requests.request", chain):
        return lp.price_lp_tokens(list(ids), fetch)


def vm_calls(chain):
    return [c for c in chain.calls if "/vm-values/query" in c[1]]


class LPCacheTests(unittest.TestCase):
    def setUp(self):
        lp.set_cache_path(None)
        pricing.clear_cache()

    def chain(self, **kw):
        return Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(), **kw)

    def test_price_reused_within_ttl(self):
        c = self.chain()
        first = price(c)
        n = len(c.calls)
        second = price(c)
        self.assertEqual(len(c.calls), n)            # not a single request
        self.assertEqual(first[LPID]["usd"], second[LPID]["usd"])
        self.assertEqual(lp.LAST_STATS["valued"], 1)
        self.assertEqual(lp.LAST_STATS["cached"], 1)

    def test_price_recomputed_after_ttl(self):
        c = self.chain()
        price(c)
        n = len(c.calls)
        with mock.patch.object(lp, "LP_PRICE_TTL", 0.0):
            out = price(c)
        self.assertGreater(len(c.calls), n)
        self.assertIn(LPID, out)

    def test_cached_result_is_a_copy(self):
        c = self.chain()
        a = price(c)
        a[LPID]["usd"] = 999
        self.assertNotEqual(price(c)[LPID]["usd"], 999)

    def test_known_pool_skips_static_views_but_still_verifies(self):
        c = self.chain()
        price(c)
        first_vm = len(vm_calls(c))
        self.assertIn(("getFirstTokenId" in str(vm_calls(c))), (True,))
        with mock.patch.object(lp, "LP_PRICE_TTL", 0.0):
            c.calls.clear()
            out = price(c)
        names = [x[2]["funcName"] for x in vm_calls(c)]
        self.assertNotIn("getFirstTokenId", names)
        self.assertNotIn("getLpTokenIdentifier", names)
        self.assertIn("getReservesAndTotalSupply", names)
        self.assertLess(len(names), first_vm)
        self.assertIn(LPID, out)
        # the balance cross-check still ran
        self.assertTrue(any("/esdt/" in x[1] for x in c.calls))

    def test_known_pool_still_rejects_inflated_reserves(self):
        c = self.chain()
        price(c)
        liar = Chain({WEGLD: 1 * E18, OTHER: 1 * E18}, pair_views(), roles=(POOL,))
        with mock.patch.object(lp, "LP_PRICE_TTL", 0.0):
            out = price(liar)
        self.assertEqual(out, {})        # balances far below the claimed reserves

    def test_batch_facts_one_call_when_list_has_the_fields(self):
        c = self.chain()
        inner = c.__call__

        def with_facts(method, url, **kw):
            if url.endswith("/tokens") and kw.get("params", {}).get("identifiers") == LPID:
                c.calls.append((method, url, None))
                return FakeResp([{"identifier": LPID, "owner": c.owner, "minted": str(c.supply),
                                  "burnt": "0", "decimals": 18}])
            return inner(method, url, **kw)
        with_facts.calls = c.calls
        out = price(with_facts)
        self.assertIn(LPID, out)
        single = [x for x in c.calls if x[1].endswith("/tokens/" + LPID)]
        self.assertEqual(single, [])     # no individual /tokens/<LP> call

    def test_repeated_set_cache_path_keeps_prices(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lp_cache.json")
            lp.set_cache_path(path)
            c = self.chain()
            price(c)
            n = len(c.calls)
            lp.set_cache_path(path)          # what the app does at every refresh
            price(c)
            self.assertEqual(len(c.calls), n)
            lp.set_cache_path(None)          # a different cache: start clean
            self.assertFalse(lp._PRICES)

    def test_pool_tokens_survive_a_relaunch(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lp_cache.json")
            lp.set_cache_path(path)
            price(self.chain())
            doc = json.load(open(path))
            self.assertEqual(doc["pools"][LPID]["tokens"], [WEGLD, OTHER])
            lp.set_cache_path(None)          # the app is closed...
            lp.set_cache_path(path)          # ...and started again: cold memory, warm file
            c = self.chain()
            out = price(c)
            names = [x[2]["funcName"] for x in vm_calls(c)]
            self.assertIn(LPID, out)
            self.assertNotIn("getFirstTokenId", names)
            self.assertIn("getReservesAndTotalSupply", names)
            self.assertTrue(any("/esdt/" in x[1] for x in c.calls))   # balances still cross-checked
            self.assertEqual(json.load(open(path))["pools"][LPID]["tokens"], [WEGLD, OTHER])

    def test_bad_persisted_tokens_ignored(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lp_cache.json")
            for bad in (["x", "y"], [WEGLD, WEGLD], [WEGLD], "nope", [1, 2]):
                json.dump({"v": 1, "pools": {LPID: {"sc": POOL, "spec": "xexchange-pair", "tokens": bad}}},
                          open(path, "w"))
                lp.set_cache_path(None)
                lp.set_cache_path(path)
                self.assertNotIn("tokens", lp.get_cache().pools[LPID])

    def test_wrong_persisted_tokens_are_rejected_by_verification(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lp_cache.json")
            json.dump({"v": 1, "pools": {LPID: {"sc": POOL, "spec": "xexchange-pair",
                                                "tokens": [WEGLD, "FAKE-123456"]}}}, open(path, "w"))
            lp.set_cache_path(None)
            lp.set_cache_path(path)
            out = price(self.chain())        # reserves belong to WEGLD/OTHER, not FAKE
            # the stale entry is dropped and the pool is rediscovered correctly
            self.assertIn(LPID, out)
            self.assertEqual(lp.get_cache().pools[LPID]["tokens"], [WEGLD, OTHER])

    def test_trust_change_drops_cached_prices(self):
        c = self.chain()
        price(c)
        self.assertTrue(lp._PRICES)
        lp.get_cache().add_trusted("A" * 43 + "=", "xexchange-pair")
        self.assertFalse(lp._PRICES)


if __name__ == "__main__":
    unittest.main()
