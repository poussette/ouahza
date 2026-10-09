"""LP price reuse (5 min), known-pool fast path and batched token facts."""
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
            real = lp.time.time              # ...and restarted > 5 min later: cold prices, warm pools
            with mock.patch.object(lp.time, "time", lambda: real() + lp.LP_PRICE_TTL + 5):
                lp.set_cache_path(path)
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


class ParallelTests(unittest.TestCase):
    def setUp(self):
        lp.set_cache_path(None)

    def test_budget_is_thread_safe(self):
        import threading
        b = lp.Budget(1000)
        got = []

        def spend():
            n = 0
            while b.spend():
                n += 1
            got.append(n)
        threads = [threading.Thread(target=spend) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(got), 1000)          # never over- or under-spent

    def test_two_lp_tokens_are_examined_side_by_side(self):
        import threading
        import time as _t
        ids = ["AAA-111111", "BBB-222222", "CCC-333333", "DDD-444444"]
        live = [0]
        peak = [0]
        guard = threading.Lock()

        def fake_discover(lpid, budget, cache):
            with guard:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            _t.sleep(0.05)
            with guard:
                live[0] -= 1
            return None                           # "unreadable": cached as failed, no value
        with mock.patch.object(lp, "discover_pool", fake_discover), \
                mock.patch.object(lp, "prefetch_token_facts", lambda l: None):
            out = lp.price_lp_tokens(ids, fetch)
        self.assertEqual(out, {})
        self.assertEqual(peak[0], lp.LP_WORKERS)
        self.assertEqual(lp.LAST_STATS["examined"], 4)

    def test_failed_ones_are_retried_in_next_pass_and_order_kept(self):
        ids = ["AAA-111111", "BBB-222222", "CCC-333333"]
        seen = []

        def flaky(lpid, budget, cache):
            seen.append(lpid)
            if lpid == "BBB-222222" and seen.count(lpid) == 1:
                raise RuntimeError("rate limited")
            return None
        with mock.patch.object(lp, "discover_pool", flaky), \
                mock.patch.object(lp, "prefetch_token_facts", lambda l: None):
            lp.price_lp_tokens(ids, fetch)
        self.assertEqual(seen.count("BBB-222222"), 2)
        self.assertEqual(lp.LAST_STATS["examined"], 3)
        self.assertEqual(lp.LAST_STATS["remaining"], 0)

    def test_progress_reports_only_finished_ones(self):
        ids = ["AAA-111111", "BBB-222222"]
        seen = []
        with mock.patch.object(lp, "discover_pool", lambda l, b, c: None), \
                mock.patch.object(lp, "prefetch_token_facts", lambda l: None):
            lp.price_lp_tokens(ids, fetch, on_progress=lambda *a: seen.append(a))
        self.assertEqual(sorted(x[0] for x in seen), [1, 2])
        self.assertTrue(all(x[1] == 2 for x in seen))


class PersistedPriceTests(unittest.TestCase):
    """A stop/relaunch within 5 minutes reuses the LP prices saved on disk."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "lp_cache.json")
        pricing.clear_cache()

    def tearDown(self):
        lp.set_cache_path(None)

    def chain(self):
        return Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())

    def relaunch(self):
        lp.set_cache_path(None)            # new process: nothing in memory
        lp.set_cache_path(self.path)

    def test_relaunch_reuses_fresh_prices(self):
        lp.set_cache_path(self.path)
        first = price(self.chain())
        self.assertIn(LPID, first)
        self.relaunch()
        c = self.chain()
        second = price(c)
        self.assertEqual(len(c.calls), 0)
        self.assertEqual(second[LPID]["usd"], first[LPID]["usd"])

    def test_relaunch_after_ttl_recomputes(self):
        lp.set_cache_path(self.path)
        price(self.chain())
        real = lp.time.time
        with mock.patch.object(lp.time, "time", lambda: real() + lp.LP_PRICE_TTL + 5):
            self.relaunch()
        c = self.chain()
        price(c)
        self.assertTrue(len(c.calls) > 0)

    def test_revocation_drops_saved_prices(self):
        lp.set_cache_path(self.path)
        price(self.chain())
        lp._drop_prices()
        lp.get_cache().save()
        self.relaunch()
        self.assertEqual(lp.get_cache().prices, {})

    def test_garbage_prices_ignored(self):
        import json
        with open(self.path, "w") as f:
            json.dump({"v": 1, "prices": {LPID: {"ts": "x", "usd": -1, "adapter": 3}}}, f)
        lp.set_cache_path(self.path)
        self.assertEqual(lp.get_cache().prices, {})


if __name__ == "__main__":
    unittest.main()
