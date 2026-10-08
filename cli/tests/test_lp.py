"""LP-token valuation tests (offline: requests.request mocked)."""
import base64
import json
import os
import stat
import tempfile
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import pricing
from providers import lp, net
from providers.base import TokenBalance, WalletBalance
from test_security import FakeResp

net.THROTTLE_ENABLED = False
net.RETRY_DELAYS = ()

LPID = "ALP-2975f6"
POOL = "erd1qqqqqqqqqqqqqpgq" + "p" * 42
ROUTER = "erd1qqqqqqqqqqqqqpgq" + "r" * 42
WEGLD, USDC, OTHER, USH = "WEGLD-bd4d79", "USDC-c76f1f", "ZZZ-abcdef", "USH-111e09"
USDT = "USDT-f8c08c"
E18 = 10**18


def b64(x):
    if isinstance(x, int):
        x = x.to_bytes(max(1, (x.bit_length() + 7) // 8), "big")
    if isinstance(x, str):
        x = x.encode()
    return base64.b64encode(x).decode()


def vm(*values, code="ok"):
    return FakeResp({"data": {"data": {"returnData": [b64(v) for v in values], "returnCode": code}}})


def blob(*ids):
    return b"".join(len(i).to_bytes(4, "big") + i.encode() for i in ids)


class Chain:
    """Tiny fake MultiversX API + gateway. `views` answers for every contract."""

    def __init__(self, holdings, views, supply=2000 * E18, owner=ROUTER, roles=(POOL,), prices=None,
                 code_hash="PT7gHWG9n6lGmLjk2NpHNsaAX/n9P/cL+xPjljMCvXk="):
        self.code_hash = code_hash
        self.holdings, self.views, self.supply, self.owner = holdings, views, supply, owner
        self.roles = roles
        self.prices = prices or {WEGLD: 4.0, USDC: 1.0, USH: 1.0, USDT: 1.0}
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json")))
        if "/vm-values/query" in url:
            body = kw["json"]
            res = self.views.get((body["funcName"], tuple(body["args"])))
            return res if res is not None else vm(code="function not found")
        if url.endswith("/roles"):
            if self.roles is None:
                return FakeResp(status=404, url=url)
            return FakeResp([{"role": "x", "roles": ["ESDTRoleBurnForAll"]}] + [
                {"address": a, "roles": ["ESDTRoleLocalMint", "ESDTRoleLocalBurn"]} for a in self.roles])
        if "/address/" in url and "/esdt/" in url:
            tok = url.rsplit("/", 1)[1]
            return FakeResp({"data": {"tokenData": {"tokenIdentifier": tok, "balance": str(self.holdings.get(tok, 0))}}})
        if "/accounts/" in url:
            return FakeResp({"codeHash": self.code_hash})
        if url.endswith("/tokens"):  # list endpoint (prices/decimals)
            ids = kw["params"]["identifiers"].split(",")
            return FakeResp([
                {"identifier": i, "decimals": 6 if i in (USDC, USDT) else 18, "price": self.prices.get(i)}
                for i in ids
            ])
        if "/tokens/" in url:
            return FakeResp({"owner": self.owner, "minted": str(self.supply), "burnt": "0", "decimals": 18})
        raise AssertionError(url)


def pair_views(r1=10 * E18, r2=40 * E18, sup=2000 * E18, lp=LPID):
    return {
        ("getLpTokenIdentifier", ()): vm(lp),
        ("getFirstTokenId", ()): vm(WEGLD),
        ("getSecondTokenId", ()): vm(OTHER),
        ("getReservesAndTotalSupply", ()): vm(r1, r2, sup),
    }


def run(chain, ids=(LPID,)):
    lp.clear_runtime_caches()   # each run() is a fresh process: no 15-minute price reuse
    with mock.patch("requests.request", chain):
        return lp.price_lp_tokens(list(ids), pricing._fetch_token_info)


def many_tokens():
    return {f"AAA{i}-00000{i}"[:10]: E18 for i in range(1, 10)}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        lp.set_cache_path(None)

    def test_pool_found_through_roles_not_issuer(self):
        # the issuer (router) answers nothing; the role holder is the pair
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        out = run(ch)
        # 10 WEGLD * $4 = $40, x2 (50/50 pool) = $80 over 2000 LP
        self.assertAlmostEqual(out[LPID]["usd"], 80 / 2000)
        self.assertTrue(out[LPID]["adapter"].startswith("xexchange-pair"))
        self.assertIn("estimation", out[LPID]["adapter"])

    def test_unknown_code_never_doubled_and_flagged(self):
        # fake/unknown contract answering like a pair: one side unpriced -> no value
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(), code_hash="evil")
        self.assertEqual(run(ch), {})
        views = pair_views()
        views[("getSecondTokenId", ())] = vm(USDC)
        views[("getReservesAndTotalSupply", ())] = vm(10 * E18, 40 * 10**6, 2000 * E18)
        out = run(Chain({WEGLD: 12 * E18, USDC: 50 * 10**6}, views, code_hash="evil"))
        self.assertIn("contrat non vérifié", out[LPID]["adapter"])
        out = run(Chain({WEGLD: 12 * E18, USDC: 50 * 10**6}, views))
        self.assertNotIn("non vérifié", out[LPID]["adapter"])

    def test_user_approved_hash_and_permissive_mode(self):
        h = "evil" + "A" * 39 + "="           # an unknown (43 chars + '=') code hash
        views = pair_views()
        mk = lambda: Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, views_one_side(), code_hash=h)
        views_one_side = lambda: pair_views()
        self.assertEqual(run(mk()), {})                     # unknown code, one side unpriced
        lp.set_cache_path(None)
        self.assertTrue(lp.get_cache().add_trusted(h, "xexchange-pair"))
        out = run(mk())
        self.assertIn("hash approuvé", out[LPID]["adapter"])
        self.assertIn("estimation", out[LPID]["adapter"])
        lp.set_cache_path(None)                             # approval was not carried over
        with mock.patch.dict(os.environ, {"WALLET_LP_TRUST_ALL": "1"}):
            out = run(mk())
        self.assertIn("mode permissif", out[LPID]["adapter"])

    def test_approval_persists_and_is_validated(self):
        path = self._cache_file()
        lp.set_cache_path(path)
        h = "B" * 43 + "="
        self.assertTrue(lp.get_cache().add_trusted(h, "jex-pair"))
        self.assertFalse(lp.get_cache().add_trusted("not a hash", "jex-pair"))
        lp.get_cache().save()
        c = lp.LPCache(path)
        self.assertTrue(c.is_trusted(h, "jex-pair"))
        self.assertFalse(c.is_trusted(h, "onedex"))        # approval is per adapter
        doc = json.load(open(path))
        doc["trusted"]["zz"] = "jex-pair"
        doc["trusted"]["C" * 43 + "="] = "nope"
        json.dump(doc, open(path, "w"))
        self.assertEqual(list(lp.LPCache(path).trusted), [h])

    def test_hostile_decimal_overflow_does_not_raise(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        orig = ch.__call__

        def fake(method, url, **kw):
            if url.endswith("/tokens/" + LPID):
                return FakeResp({"owner": ROUTER, "minted": "1", "burnt": "0", "supply": "1E999999999", "decimals": 18})
            return orig(method, url, **kw)
        with mock.patch("requests.request", fake):
            self.assertEqual(lp.price_lp_tokens([LPID], pricing._fetch_token_info), {})

    def test_token_id_with_newline_rejected(self):
        self.assertIsNone(lp.as_token_id(b"ABC-123456\n"))

    def test_roles_unavailable_issuer_still_tried(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(), roles=None)
        self.assertIn(LPID, run(ch))

    def test_both_sides_priced_summed(self):
        views = pair_views()
        views[("getSecondTokenId", ())] = vm(USDC)
        views[("getReservesAndTotalSupply", ())] = vm(10 * E18, 40 * 10**6, 2000 * E18)
        ch = Chain({WEGLD: 12 * E18, USDC: 50 * 10**6}, views)
        out = run(ch)
        self.assertAlmostEqual(out[LPID]["usd"], (40 + 40) / 2000)
        self.assertNotIn("estimation", out[LPID]["adapter"])

    def test_reserve_above_real_balance_rejected(self):
        self.assertEqual(run(Chain({WEGLD: 5 * E18, OTHER: 50 * E18}, pair_views(r1=10 * E18))), {})

    def test_token_not_held_rejected(self):
        self.assertEqual(run(Chain({WEGLD: 12 * E18}, pair_views())), {})

    def test_view_names_other_lp_rejected(self):
        self.assertEqual(run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(lp="OTH-123456"))), {})

    def test_pool_that_does_not_name_lp_rejected(self):
        views = pair_views()
        del views[("getLpTokenIdentifier", ())]
        self.assertEqual(run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, views)), {})

    def test_supply_mismatch_rejected(self):
        self.assertEqual(run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(sup=500 * E18))), {})

    def test_jex_named_views(self):
        views = {
            ("getLpToken", ()): vm(LPID), ("getFirstToken", ()): vm(WEGLD), ("getSecondToken", ()): vm(USDC),
            ("getFirstTokenReserve", ()): vm(10 * E18), ("getSecondTokenReserve", ()): vm(40 * 10**6),
            ("getLpTokenSupply", ()): vm(2000 * E18),
        }
        out = run(Chain({WEGLD: 10 * E18, USDC: 40 * 10**6}, views, owner=POOL))
        self.assertAlmostEqual(out[LPID]["usd"], 80 / 2000)
        self.assertTrue(out[LPID]["adapter"].startswith("jex-pair"))

    def _onedex(self, pair_lp=LPID, mapped=True):
        key = [b"\x10"[:1].hex()]  # pair id 16 -> "10"
        mapping = [b"AAA-111111", 3, LPID.encode() if mapped else b"BBB-222222", 16]
        return {
            ("getLpTokenPairIdMap", ()): vm(*mapping),
            ("getPairLpTokenId", tuple(key)): vm(pair_lp),
            ("getPairFirstTokenId", tuple(key)): vm(WEGLD),
            ("getPairSecondTokenId", tuple(key)): vm(USDC),
            ("getPairFirstTokenReserve", tuple(key)): vm(10 * E18),
            ("getPairSecondTokenReserve", tuple(key)): vm(40 * 10**6),
            ("getPairLpTokenTotalSupply", tuple(key)): vm(2000 * E18),
        }

    def test_onedex_shared_contract_keyed_by_pair_id(self):
        h = many_tokens()
        h.update({WEGLD: 12 * E18, USDC: 50 * 10**6})
        out = run(Chain(h, self._onedex(), owner=POOL, roles=(POOL,)))
        self.assertAlmostEqual(out[LPID]["usd"], 80 / 2000)
        self.assertTrue(out[LPID]["adapter"].startswith("onedex"))

    def test_onedex_lp_not_in_map_or_wrong_id(self):
        h = {WEGLD: 12 * E18, USDC: 50 * 10**6}
        self.assertEqual(run(Chain(h, self._onedex(mapped=False), owner=POOL)), {})
        self.assertEqual(run(Chain(h, self._onedex(pair_lp="OTH-123456"), owner=POOL)), {})

    def test_ashswap_blob_tokens_balances_fallback(self):
        # getTokens = one blob without count; getBalances absent: use balances
        views = {
            ("getLpTokenIdentifier", ()): vm(LPID),
            ("getTokens", ()): vm(blob(USH, USDC)),
            ("getTotalSupply", ()): vm(2000 * E18),
        }
        out = run(Chain({USH: 1500 * E18, USDC: 500 * 10**6}, views))
        self.assertAlmostEqual(out[LPID]["usd"], 2000 / 2000)
        self.assertIn("soldes du contrat", out[LPID]["adapter"])

    def test_lists_with_reserve_view(self):
        views = {
            ("getLpTokenIdentifier", ()): vm(LPID), ("getTokens", ()): vm(USH, USDC),
            ("getBalances", ()): vm(1500 * E18, 500 * 10**6), ("getTotalSupply", ()): vm(2000 * E18),
        }
        out = run(Chain({USH: 1500 * E18, USDC: 500 * 10**6}, views))
        self.assertAlmostEqual(out[LPID]["usd"], 1.0)

    def test_lists_need_supply_and_all_priced(self):
        views = {("getLpTokenIdentifier", ()): vm(LPID), ("getTokens", ()): vm(USH, USDC)}
        self.assertEqual(run(Chain({USH: E18, USDC: 10**6}, views)), {})
        views[("getTotalSupply", ())] = vm(2000 * E18)
        ch = Chain({USH: 1500 * E18, USDC: 500 * 10**6}, views, prices={USH: None, USDC: 1.0})
        self.assertEqual(run(ch), {})  # stable/multi: no 50/50 guess

    def test_lists_supply_must_match(self):
        views = {("getLpTokenIdentifier", ()): vm(LPID), ("getTokens", ()): vm(USH, USDC),
                 ("getTotalSupply", ()): vm(5 * E18)}
        self.assertEqual(run(Chain({USH: 1500 * E18, USDC: 500 * 10**6}, views)), {})

    def test_initial_minted_counts_in_supply(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(), supply=1000 * E18)
        orig = ch.__call__

        def fake(method, url, **kw):
            if url.endswith("/tokens/" + LPID):
                return FakeResp({"owner": ROUTER, "minted": str(1000 * E18), "burnt": "0",
                                 "initialMinted": str(1000 * E18), "supply": "2000", "decimals": 18})
            return orig(method, url, **kw)
        with mock.patch("requests.request", fake):
            out = lp.price_lp_tokens([LPID], pricing._fetch_token_info)
        self.assertAlmostEqual(out[LPID]["usd"], 80 / 2000)

    def test_api_supply_disagreement_rejected(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        orig = ch.__call__

        def fake(method, url, **kw):
            if url.endswith("/tokens/" + LPID):
                return FakeResp({"owner": ROUTER, "minted": str(2000 * E18), "burnt": "0", "supply": "5", "decimals": 18})
            return orig(method, url, **kw)
        with mock.patch("requests.request", fake):
            self.assertEqual(lp.price_lp_tokens([LPID], pricing._fetch_token_info), {})

    def test_huge_supply_rejected_no_exception(self):
        self.assertEqual(run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views(), supply=10**400)), {})

    def test_stale_api_balance_tolerated(self):
        self.assertIn(LPID, run(Chain({WEGLD: int(9.8 * E18), OTHER: 50 * E18}, pair_views())))

    def test_http_error_on_view_means_no_such_view(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        orig = ch.__call__

        def fake(method, url, **kw):
            if "/vm-values/query" in url and kw["json"]["funcName"] == "getLpToken":
                return FakeResp(status=400, url=url)
            return orig(method, url, **kw)
        with mock.patch("requests.request", fake):
            out = lp.price_lp_tokens([LPID], pricing._fetch_token_info)
        self.assertIn(LPID, out)

    def test_unknown_contract_stays_cheap(self):
        ch = Chain(many_tokens() | {WEGLD: E18, OTHER: E18}, {})
        run(ch)
        self.assertLessEqual(len([c for c in ch.calls if "vm-values" in c[1]]), 16)

    def test_dead_contract_not_retried_for_other_lps(self):
        ch = Chain({WEGLD: E18}, {})
        run(ch, ids=(LPID, "ALP-111111", "ALP-222222"))
        first = len([c for c in ch.calls if "vm-values" in c[1]])
        self.assertLessEqual(first, 16)

    def test_not_a_contract_and_no_roles_ignored(self):
        ch = Chain({WEGLD: 10 * E18}, {}, owner="erd1" + "a" * 58, roles=())
        self.assertEqual(run(ch), {})

    def test_hostile_gateway_answers(self):
        views = pair_views()
        views[("getReservesAndTotalSupply", ())] = FakeResp({"data": {"data": {"returnData": ["!!notb64", None], "returnCode": "ok"}}})
        self.assertEqual(run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, views)), {})

    def test_oversized_answer_list_ignored(self):
        big = FakeResp({"data": {"data": {"returnData": [b64(1)] * (lp.MAX_RETURN_ITEMS + 1), "returnCode": "ok"}}})
        views = pair_views()
        views[("getLpTokenIdentifier", ())] = big
        self.assertEqual(run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, views)), {})

    def test_budget_bounds_calls(self):
        ch = Chain(many_tokens() | {WEGLD: E18}, {})
        run(ch, ids=[f"ALP-{i:06x}" for i in range(60)])
        self.assertLessEqual(len([c for c in ch.calls if "vm-values" in c[1]]), lp.MAX_VM_CALLS)

    def test_deadline_reports_stopped(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        with mock.patch.object(lp, "DEADLINE_SECONDS", -1):
            self.assertEqual(run(ch, ids=(LPID, "ALP-111111")), {})
        self.assertTrue(lp.LAST_STATS["stopped"])
        run(Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views()))
        self.assertFalse(lp.LAST_STATS["stopped"])
        self.assertEqual(lp.LAST_STATS["valued"], 1)

    def test_stats_not_stopped_when_all_examined(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        run(ch, ids=(LPID, "not-an-id", "ALP-111111"))
        st = lp.LAST_STATS
        self.assertFalse(st["stopped"])
        self.assertEqual((st["candidates"], st["remaining"]), (2, 0))

    def test_balance_read_from_gateway(self):
        ch = Chain({WEGLD: 12 * E18}, {})
        with mock.patch("requests.request", ch):
            self.assertEqual(lp._balance(POOL, WEGLD), 12 * E18)
            self.assertEqual(lp._balance(POOL, OTHER), 0)
        self.assertTrue(any("/address/" in c[1] and c[1].startswith(lp.DEFAULT_GATEWAY) for c in ch.calls))

    def _cache_file(self):
        d = tempfile.mkdtemp()
        return os.path.join(d, "sub", "lp_cache.json")

    def test_cache_persists_and_speeds_up(self):
        path = self._cache_file()
        lp.set_cache_path(path)
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        self.assertIn(LPID, run(ch))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = json.load(open(path))
        self.assertEqual(doc["pools"][LPID]["sc"], POOL)
        self.assertNotIn("usd", json.dumps(doc))      # no price/reserve is stored
        lp.set_cache_path(path)                         # "next launch"
        ch2 = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        out = run(ch2)
        self.assertAlmostEqual(out[LPID]["usd"], 80 / 2000)
        self.assertFalse(any(c[1].endswith("/roles") for c in ch2.calls))
        self.assertLess(len(ch2.calls), len(ch.calls))

    def test_stale_cache_entry_rediscovered(self):
        lp.set_cache_path(self._cache_file())
        lp.get_cache().put_good(LPID, ROUTER, "xexchange-pair")   # wrong contract cached
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        # the router answers like the pool here, so make it contradict the chain
        out = run(ch)
        self.assertIn(LPID, out)

    def test_unreadable_lp_skipped_next_time(self):
        lp.set_cache_path(self._cache_file())
        ch = Chain({WEGLD: E18}, {})
        run(ch)
        self.assertTrue(lp.get_cache().is_failed(LPID))
        ch2 = Chain({WEGLD: E18}, {})
        run(ch2)
        self.assertEqual([c for c in ch2.calls if "vm-values" in c[1]], [])
        # expired verdict is retried
        lp.get_cache().fail[LPID] = time.time() - lp.FAIL_TTL - 5
        ch3 = Chain({WEGLD: E18}, {})
        run(ch3)
        self.assertTrue([c for c in ch3.calls if "vm-values" in c[1]])

    def test_hostile_cache_file_ignored(self):
        path = self._cache_file()
        os.makedirs(os.path.dirname(path))
        for blob in ("not json", "[]", json.dumps({"v": 1, "pools": {"x": 1, LPID: {"sc": "evil", "spec": "xexchange-pair"},
                     "BBB-123456": {"sc": POOL, "spec": "nope"}}, "fail": {LPID: "now", "CCC-123456": True}})):
            open(path, "w").write(blob)
            c = lp.LPCache(path)
            self.assertEqual((c.pools, c.fail), ({}, {}))
        open(path, "w").write("x" * (lp.MAX_CACHE_BYTES + 1))
        self.assertEqual(lp.LPCache(path).pools, {})

    def test_second_pass_after_transient_failure(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        state = {"n": 0}

        def fake(method, url, **kw):
            if "/vm-values/query" in url and state["n"] == 0:
                state["n"] = 1
                return FakeResp(status=429, url=url)
            return ch(method, url, **kw)
        seen = []
        with mock.patch("requests.request", fake):
            out = lp.price_lp_tokens([LPID], pricing._fetch_token_info, on_progress=lambda *a: seen.append(a))
        self.assertIn(LPID, out)
        self.assertFalse(lp.LAST_STATS["stopped"])
        self.assertTrue(seen)

    def _stable_views(self, vp=1_106_494_468_076_081_469, with_lp_view=False):
        status = b"\x00\x00\x00\x01\x00\x00\x00\x00\x02" + blob(USDC, USDT, LPID)  # LP id included, as on chain
        v = {("getVirtualPrice", ()): vm(vp), ("getStatus", ()): vm(status)}
        if with_lp_view:
            v[("getLptoken", ())] = vm(LPID)
        return v

    def test_jex_stable_balances_and_virtual_price(self):
        # 1851.57 USDC + 1374.21 USDT over 2915.19 LP = 1.1065 (= virtual price)
        h = {USDC: 1851566591, USDT: 1374210046, "EVLD-43f56f": 10**7}
        ch = Chain(h, self._stable_views(), supply=2915185100000000000000, owner=POOL)
        out = run(ch)
        self.assertAlmostEqual(out[LPID]["usd"], 1.10655, places=3)
        self.assertIn("jex-stable", out[LPID]["adapter"])

    def test_jex_stable_virtual_price_mismatch_rejected(self):
        h = {USDC: 1851566591, USDT: 1374210046}
        ch = Chain(h, self._stable_views(vp=2 * 10**18), supply=2915185100000000000000, owner=POOL)
        self.assertEqual(run(ch), {})

    def test_jex_stable_needs_lp_binding(self):
        h = {USDC: 1851566591, USDT: 1374210046}
        # contract is only the *issuer*, names no LP, holds no mint role: not tied to the LP
        ch = Chain(h, self._stable_views(), supply=2915185100000000000000, owner=POOL, roles=())
        self.assertEqual(run(ch), {})
        # ... but if it names the LP it is accepted
        lp.set_cache_path(None)
        ch = Chain(h, self._stable_views(with_lp_view=True), supply=2915185100000000000000, owner=POOL, roles=())
        self.assertIn(LPID, run(ch))

    def test_blob_decoders(self):
        self.assertEqual(lp._token_list([blob(USH, USDC)]), [USH, USDC])
        self.assertIsNone(lp._token_list([blob(USH, USDC) + b"\x00"]))
        self.assertIsNone(lp._token_list([USH.encode()]))

    def test_gateway_setting(self):
        with mock.patch.dict(os.environ, {"MULTIVERSX_GATEWAY_URL": "http://evil.example"}):
            self.assertEqual(lp.gateway_url(), lp.DEFAULT_GATEWAY)
        with mock.patch.dict(os.environ, {"MULTIVERSX_GATEWAY_URL": "https://my.node/KEYSECRET1/"}):
            self.assertEqual(lp.gateway_url(), "https://my.node/KEYSECRET1")
            from providers.safe import redact
            self.assertNotIn("KEYSECRET1", redact("error url: /KEYSECRET1/vm-values/query"))

    def test_custom_gateway_is_used(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())
        with mock.patch.dict(os.environ, {"MULTIVERSX_GATEWAY_URL": "https://my.node"}):
            run(ch)
        self.assertTrue(any(c[1].startswith("https://my.node/vm-values/query") for c in ch.calls))

    def test_looks_like_lp(self):
        self.assertTrue(lp.looks_like_lp("JEXWEGLD", "OneDex JEX-WEGLD LP"))
        self.assertTrue(lp.looks_like_lp("X", "LiquidityPool Token"))
        self.assertFalse(lp.looks_like_lp("MEME", "meme"))


class PricingIntegration(unittest.TestCase):
    def setUp(self):
        pricing.clear_cache()
        lp.set_cache_path(None)

    def test_apply_pricing_values_lp_token(self):
        ch = Chain({WEGLD: 12 * E18, OTHER: 50 * E18}, pair_views())

        def fake(method, url, **kw):
            if url.endswith("/mex-tokens"):
                return FakeResp([])
            if "frankfurter" in url:
                return FakeResp({"rates": {"EUR": 0.9}})
            if "coingecko" in url:
                return FakeResp({"elrond-erd-2": {"usd": 4, "eur": 3.6}})
            return ch(method, url, **kw)

        w = WalletBalance("multiversx", "erd1" + "q" * 58, "EGLD", native_amount=1.0)
        w.tokens.append(TokenBalance("ALP-2975f6", "AshswapLPUsdcUsh", 100.0, contract=LPID, asset_type="esdt"))
        w.tokens.append(TokenBalance("MEME", "meme", 5.0, contract="MEME-123456", asset_type="esdt"))
        with mock.patch("requests.request", fake):
            ok = pricing.apply_pricing([w])
        self.assertTrue(ok)
        self.assertAlmostEqual(w.tokens[0].usd_value, 100 * 80 / 2000)
        self.assertAlmostEqual(w.tokens[0].eur_value, 100 * 80 / 2000 * 0.9)
        self.assertIn("LP valorisé", w.tokens[0].name)
        self.assertIsNone(w.tokens[1].usd_value)   # not LP-like: untouched

    def test_amount_above_supply_not_valued(self):
        tok = TokenBalance("ALP", "x LP", 5000.0, contract=LPID, asset_type="esdt")
        pricing._price_lp(tok, {"usd": 0.04, "adapter": "x", "supply": 2000.0}, 0.9)
        self.assertIsNone(tok.usd_value)
        tok.amount = 100.0
        pricing._price_lp(tok, {"usd": 0.04, "adapter": "x", "supply": 2000.0}, 0.9)
        self.assertAlmostEqual(tok.usd_value, 4.0)

    def test_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"WALLET_LP_PRICING": "0"}):
            self.assertFalse(pricing.lp_pricing_enabled())


if __name__ == "__main__":
    unittest.main()
