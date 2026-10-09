"""Pools with an unknown contract code are offered for approval, once."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import pricing
from providers import lp
from test_lp import E18, LPID, OTHER, WEGLD, Chain, pair_views, run

H = "evil" + "A" * 39 + "="


def unknown_pool(code_hash=H):
    return Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(), code_hash=code_hash)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        lp.set_cache_path(None)
        pricing.clear_cache()

    def test_unknown_code_is_offered_not_valued(self):
        self.assertEqual(run(unknown_pool()), {})
        pend = lp.pending_approvals()
        self.assertEqual([p["lp"] for p in pend], [LPID])
        self.assertEqual(pend[0]["code_hash"], H)
        self.assertEqual(pend[0]["adapter"], "xexchange-pair")
        self.assertAlmostEqual(pend[0]["unit_usd"], 80 / 2000)   # what it would be worth

    def test_approval_values_the_pool_and_is_remembered(self):
        run(unknown_pool())
        self.assertTrue(lp.approve_pending(LPID))
        self.assertEqual(lp.pending_approvals(), [])
        out = run(unknown_pool())
        self.assertIn("hash approuvé", out[LPID]["adapter"])
        self.assertAlmostEqual(out[LPID]["usd"], 80 / 2000)
        self.assertEqual(lp.pending_approvals(), [])

    def test_approval_survives_a_restart(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "lp_cache.json")
        lp.set_cache_path(path)
        run(unknown_pool())
        self.assertTrue(lp.approve_pending(LPID))
        self.assertEqual(json.load(open(path))["trusted"], {H: "xexchange-pair"})
        lp.set_cache_path(None)
        lp.set_cache_path(path)                    # new session reads the file
        self.assertIn(LPID, run(unknown_pool()))

    def test_revocation_removes_it_for_good(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "lp_cache.json")
        lp.set_cache_path(path)
        run(unknown_pool())
        lp.approve_pending(LPID)
        self.assertEqual(lp.approved_contracts(), [{"code_hash": H, "adapter": "xexchange-pair"}])
        self.assertTrue(lp.revoke_approval(H))
        self.assertFalse(lp.revoke_approval(H))
        self.assertEqual(lp.approved_contracts(), [])
        self.assertEqual(json.load(open(path))["trusted"], {})
        self.assertEqual(run(unknown_pool()), {})                 # offered again, not valued
        self.assertEqual([p["lp"] for p in lp.pending_approvals()], [LPID])
        lp.set_cache_path(None)
        lp.set_cache_path(path)
        self.assertEqual(lp.approved_contracts(), [])

    def test_nothing_to_approve_for_a_known_or_priced_pool(self):
        run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views()))       # known code
        self.assertEqual(lp.pending_approvals(), [])
        self.assertFalse(lp.approve_pending(LPID))
        self.assertFalse(lp.approve_pending("NOPE-123456"))

    def test_fully_priced_unknown_pool_is_valued_without_asking(self):
        from test_lp import USDC
        views = pair_views()
        views[("getSecondTokenId", ())] = lp_vm(USDC)
        views[("getReservesAndTotalSupply", ())] = lp_vm(10 * E18, 40 * 10**6, 2000 * E18)
        out = run(Chain({WEGLD: 12 * E18, USDC: 50 * 10**6}, views, code_hash=H))
        self.assertIn("contrat non vérifié", out[LPID]["adapter"])
        self.assertEqual(lp.pending_approvals(), [])


def lp_vm(*a):
    from test_lp import vm
    return vm(*a)


if __name__ == "__main__":
    unittest.main()


class OutageTests(unittest.TestCase):
    """A rate limit or an outage must never get an LP marked unreadable."""

    def setUp(self):
        lp.set_cache_path(None)
        pricing.clear_cache()

    def test_rate_limited_roles_lookup_is_retried_not_remembered_as_failure(self):
        from providers import net
        from test_security import FakeResp
        storm = {"on": True}
        base = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())

        def chain(method, url, **kw):
            if storm["on"] and url.endswith("/roles"):
                return FakeResp(status=429, url=url)
            return base(method, url, **kw)

        with mock.patch.object(net, "_sleep", lambda s: None), mock.patch("requests.request", chain):
            self.assertEqual(lp.price_lp_tokens([LPID], pricing._fetch_token_info), {})
            self.assertFalse(lp.get_cache().is_failed(LPID))        # not "unreadable"
            storm["on"] = False
            out = lp.price_lp_tokens([LPID], pricing._fetch_token_info)
        self.assertIn(LPID, out)

    def test_absent_roles_endpoint_still_falls_back_to_the_issuer(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(), roles=None)   # 404
        self.assertIn(LPID, run(ch))

    def test_old_unreadable_verdicts_are_forgotten_once(self):
        import time
        d = tempfile.mkdtemp()
        path = os.path.join(d, "lp_cache.json")
        with open(path, "w") as f:
            json.dump({"v": 1, "sig": lp._adapter_signature(), "pools": {}, "fail": {LPID: time.time()}, "trusted": {}}, f)
        self.assertFalse(lp.LPCache(path).is_failed(LPID))          # written by an older version
        c = lp.LPCache(path)
        c.put_fail(LPID)
        c.save()
        self.assertTrue(lp.LPCache(path).is_failed(LPID))           # current format: kept
