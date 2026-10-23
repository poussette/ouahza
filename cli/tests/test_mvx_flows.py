import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "core"))

import requests  # noqa: E402
from providers import multiversx, mvx_flows  # noqa: E402

ME = "erd160p9vp5lnjf7dvur0hal6mggdl25vw0qr3fpn8edsew30q7dhu5q442fwm"
LOCKER = "erd1qqqqqqqqqqqqqpgq05whpg29ggrrm9ww3ufsf9ud23f66msv6avs5s5xxy"
ROUTER = "erd1qqqqqqqqqqqqqpgqa0fsfshnff4n76jhcye6k7uvd7qacsq42jpsp6shh2"
OTHER = "erd1" + "a" * 58
JEX = "JEX-9040ca"
E18 = 10 ** 18
NOW = 1_800_000_000     # far after every fixture timestamp
SOON = 400_000          # shortly after the small fixture histories (keeps the walk to 'now' short)


def tr(token, value, decimals=18, kind="FungibleESDT", ticker=None):
    return {"type": kind, "ticker": ticker or token.split("-")[0], "token": token,
            "decimals": decimals, "value": str(value)}


def out(txh, ts, dest, *transfers, status="success", sender=ME):
    return {"txHash": txh, "timestamp": ts, "sender": sender, "receiver": dest, "status": status,
            "type": "Transaction", "receiverAssets": {"name": "JEXchange: Locker"} if dest == LOCKER else {},
            "action": {"arguments": {"transfers": list(transfers), "receiver": dest}}}


def back(txh, ts, orig, src, *transfers):
    return {"txHash": txh, "timestamp": ts, "originalTxHash": orig, "sender": src, "receiver": ME,
            "status": "success", "type": "SmartContractResult",
            "senderAssets": {"name": "JEXchange: Locker"} if src == LOCKER else {},
            "action": {"arguments": {"transfers": list(transfers), "receiver": ME}}}


def noise(txh, ts):
    return out(txh, ts, OTHER, tr("USDC-c76f1f", 1, 6))


class FakeAPI:
    """Behaves like /accounts/{addr}/transfers: date filters (inclusive or not),
    order, size, count -- and, like the real one, UNRELIABLE offset pages."""

    def __init__(self, items, after_incl=True, before_incl=True, scramble_offsets=False,
                 ignore_filters=False, reject_fields=False):
        self.items = sorted(items, key=lambda i: i["timestamp"])
        self.after_incl, self.before_incl = after_incl, before_incl
        self.scramble, self.ignore_filters, self.reject_fields = scramble_offsets, ignore_filters, reject_fields
        self.calls = []

    def __call__(self, suffix, params):
        self.calls.append((suffix, dict(params)))
        if suffix == "/transfers/count":
            return len(self.items)
        if self.reject_fields and "fields" in params:
            raise requests.HTTPError("HTTP 400")
        rows = list(self.items)
        if not self.ignore_filters:
            if "after" in params:
                a = params["after"]
                rows = [r for r in rows if (r["timestamp"] >= a if self.after_incl else r["timestamp"] > a)]
            if "before" in params:
                b = params["before"]
                rows = [r for r in rows if (r["timestamp"] <= b if self.before_incl else r["timestamp"] < b)]
        off, size = params.get("from", 0), params.get("size", 50)
        if off and self.scramble:
            rows = rows[::-1][1:]                     # what the real API does at some offsets
        return rows[off:off + size]

    def window_calls(self):
        return [p for s, p in self.calls if s == "/transfers"]


class Base(unittest.TestCase):
    def setUp(self):
        mvx_flows.set_cache_path(None)
        mvx_flows.clear_cache()
        mvx_flows._use_fields = True
        self._patches = []

    def cfg(self, size, budget):
        for name, val in (("FLOW_PAGE_SIZE", size), ("FLOW_MAX_PAGES", budget)):
            p = mock.patch.object(mvx_flows, name, val)
            p.start()
            self.addCleanup(p.stop)

    def read_all(self, api, now=NOW, limit=400, restart=False):
        res = None
        for n in range(1, limit + 1):
            res = mvx_flows.engaged_positions(ME, api, now=now)
            if res.done or res.error:
                return res, n
            if restart:   # a restart between refreshes: the state goes through its JSON form
                mvx_flows._states[ME] = mvx_flows.State.from_json(
                    json.loads(json.dumps(mvx_flows._states[ME].to_json())))
        self.fail("never finished")


class TestLedger(unittest.TestCase):
    def test_real_lock_is_reported(self):
        pos = mvx_flows.compute_engaged(ME, [out("ea46", 1792241920, LOCKER, tr(JEX, 1000 * E18))])
        self.assertEqual((pos[0].contract, pos[0].token, pos[0].ticker), (LOCKER, JEX, "JEX"))
        self.assertEqual(pos[0].contract_name, "JEXchange: Locker")
        self.assertAlmostEqual(pos[0].amount, 1000.0)

    def test_real_history_nets_out(self):
        items = [
            out("ea46", 1792241920, LOCKER, tr(JEX, 1000 * E18)),
            back("c1", 1764753024, "3b5e", LOCKER, tr(JEX, 1000000003557600503767317)),
            out("old", 1748843178, LOCKER, tr(JEX, 1000000 * E18)),
        ]
        pos = mvx_flows.compute_engaged(ME, items)
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(pos[0].amount, 999.9964424, places=6)

    def test_never_negative(self):
        items = [out("a", 1, LOCKER, tr(JEX, 10 * E18)), back("b", 2, "u", LOCKER, tr(JEX, 12 * E18))]
        self.assertEqual(mvx_flows.compute_engaged(ME, items), [])
        self.assertEqual(mvx_flows.compute_engaged(ME, [back("b", 2, "u", LOCKER, tr(JEX, 5 * E18))]), [])

    def test_swap_and_nft_receipt_are_not_deposits(self):
        swap = [out("s", 1, ROUTER, tr("USDC-c76f1f", 5_000_000, 6)), back("r", 2, "s", ROUTER, tr("WEGLD-bd4d79", E18))]
        farm = [out("f", 1, ROUTER, tr(JEX, E18)), back("r", 2, "f", ROUTER, tr("FARM-abcdef-01", 1, 0, "NonFungibleESDT"))]
        self.assertEqual(mvx_flows.compute_engaged(ME, swap), [])
        self.assertEqual(mvx_flows.compute_engaged(ME, farm), [])

    def test_ignores_failed_eoa_nft_malformed_and_foreign(self):
        items = [
            out("f", 1, LOCKER, tr(JEX, E18), status="fail"),
            out("e", 1, OTHER, tr(JEX, E18)),
            out("n", 1, LOCKER, tr("COL-abcdef-01", 1, 0, "NonFungibleESDT")),
            out("m1", 1, LOCKER, tr("not a token", E18)),
            out("m2", 1, LOCKER, tr(JEX, "abc")),
            out("m3", 1, LOCKER, tr(JEX, -5)),
            out("m4", 1, LOCKER, tr(JEX, E18, decimals=999)),
            out("m5", 1, LOCKER, tr(JEX, 10 ** 70)),
            out("x", 1, LOCKER, tr(JEX, E18), sender=OTHER),
            "junk", None, {"action": "x"}, {"action": {"arguments": {"transfers": "x"}}},
        ]
        self.assertEqual(mvx_flows.compute_engaged(ME, items), [])


    def test_withdrawal_through_another_contract_is_charged_to_the_contract_called(self):
        # deposit into LOCKER; later the wallet calls LOCKER.unstake (nothing sent) and a DIFFERENT
        # contract pays out: the position held by LOCKER must go back to zero
        call = out("u", 3000, LOCKER)
        call["function"] = "unstake"
        items = [out("d", 1000, LOCKER, tr(JEX, 10 * E18)), call, back("p", 3012, "u", ROUTER, tr(JEX, 10 * E18))]
        self.assertEqual(mvx_flows.compute_engaged(ME, items), [])
        partial = [out("d", 1000, LOCKER, tr(JEX, 10 * E18)), call, back("p", 3012, "u", ROUTER, tr(JEX, 4 * E18))]
        pos = mvx_flows.compute_engaged(ME, partial)
        self.assertEqual((pos[0].contract, round(pos[0].amount, 6)), (LOCKER, 6.0))

    def test_swap_output_never_reduces_a_position(self):
        # 10 JEX sit in LOCKER; a swap whose output is JEX paid by LOCKER's own contract must not eat it
        items = [out("d", 1000, LOCKER, tr(JEX, 10 * E18)),
                 out("sw", 2000, LOCKER, tr("USDC-c76f1f", 5_000_000, 6)),
                 back("o", 2012, "sw", LOCKER, tr(JEX, 3 * E18))]
        pos = mvx_flows.compute_engaged(ME, items)
        self.assertEqual([(p.token, round(p.amount, 6)) for p in pos], [(JEX, 10.0)])

    def test_unknown_origin_answer_is_charged_to_its_sender(self):
        items = [out("d", 1000, LOCKER, tr(JEX, 10 * E18)), back("r", 2000, "someone-else", LOCKER, tr(JEX, 4 * E18))]
        self.assertAlmostEqual(mvx_flows.compute_engaged(ME, items)[0].amount, 6.0)

    def test_describe_explains_each_position(self):
        call = out("d", 1000, LOCKER, tr(JEX, 10 * E18))
        call["function"] = "lock"
        st = mvx_flows.State()
        st.integrate(ME, [call], 10 ** 9)
        lines = st.describe("jex")
        self.assertEqual(len(lines), 1)
        for part in (LOCKER, "JEXchange: Locker", "net=10.0000", "deposits=1", "functions=lock"):
            self.assertIn(part, lines[0])
        self.assertEqual(st.describe("nothing-like-this"), [])

    def test_caps_positions(self):
        items = [out(f"t{i}", 1, "erd1qqqqqqqqqqqqqpgq" + f"{i:04d}".ljust(42, "q"), tr(f"TK{i:04d}-abcdef", E18)) for i in range(mvx_flows.MAX_POSITIONS + 10)]
        self.assertEqual(len(mvx_flows.compute_engaged(ME, items)), mvx_flows.MAX_POSITIONS)


class TestWindowReading(Base):
    def history(self):
        # real shape: 1M locked long ago, unlocked (answer stamped +12 s), then 1000 locked; noise in between
        items = [out("old", 1000, LOCKER, tr(JEX, 1000000 * E18)),
                 noise("n1", 1500), noise("n2", 1600),
                 out("unl", 2000, LOCKER),
                 back("ret", 2012, "unl", LOCKER, tr(JEX, 1000000003557600503767317)),
                 noise("n3", 2500), noise("n4", 2600), noise("n5", 2700),
                 out("new", 3000, LOCKER, tr(JEX, 1000 * E18)),
                 noise("n6", 3500)]
        items[3]["action"] = {"arguments": {"transfers": [], "receiver": LOCKER}}
        return items

    def test_matches_a_single_pass_whatever_the_slicing(self):
        want = mvx_flows.compute_engaged(ME, self.history())[0].amount
        self.assertAlmostEqual(want, 999.9964424, places=6)
        for size, budget in ((3, 6), (3, 8), (5, 4), (100, 12)):
            mvx_flows.clear_cache()
            with mock.patch.object(mvx_flows, "FLOW_PAGE_SIZE", size), mock.patch.object(mvx_flows, "FLOW_MAX_PAGES", budget):
                res, _ = self.read_all(FakeAPI(self.history()), now=SOON, restart=True)
            self.assertEqual(len(res.positions), 1, (size, budget))
            self.assertAlmostEqual(res.positions[0].amount, want, places=6)

    def test_never_uses_offsets_when_a_window_fits(self):
        # the real API reorders/skips entries at offsets: a correct read must not depend on them
        self.cfg(4, 6)
        api = FakeAPI(self.history(), scramble_offsets=True)
        res, _ = self.read_all(api, now=SOON)
        self.assertAlmostEqual(res.positions[0].amount, 999.9964424, places=6)
        self.assertTrue(all(p.get("from", 0) == 0 for p in api.window_calls()))

    def test_starts_at_the_first_transfer_not_at_year_zero(self):
        items = [out("a", 1601589360, LOCKER, tr(JEX, 5 * E18))] + [noise(f"n{i}", 1792241920 + i) for i in range(3)]
        api = FakeAPI(items)
        res, runs = self.read_all(api, now=1792300000 + 5000)
        self.assertEqual(runs, 1)
        self.assertLess(len(api.window_calls()), 14)
        self.assertAlmostEqual(res.positions[0].amount, 5.0)

    def test_dense_period_shrinks_the_window(self):
        self.cfg(10, 12)
        items = [noise(f"d{i}", 5000 + i // 3) for i in range(90)] + [out("dep", 6000, LOCKER, tr(JEX, 2 * E18))]
        api = FakeAPI(items)
        res, _ = self.read_all(api, now=6000 + 5000)
        self.assertAlmostEqual(res.positions[0].amount, 2.0)

    def test_provisional_while_reading_and_percent(self):
        self.cfg(3, 3)
        first = mvx_flows.engaged_positions(ME, FakeAPI(self.history()), now=NOW)
        self.assertFalse(first.done)
        self.assertIsInstance(first.percent, int)
        self.assertTrue(0 <= first.percent <= 99)

    def test_later_runs_only_read_what_is_new(self):
        api = FakeAPI(self.history())
        self.read_all(api, now=SOON)
        api.calls.clear()
        res = mvx_flows.engaged_positions(ME, api, now=SOON + 10_000)
        self.assertLessEqual(len(api.window_calls()), 2)
        self.assertEqual(len(res.positions), 1)
        self.assertAlmostEqual(res.positions[0].amount, 999.9964424, places=6)

    def test_recent_transfers_wait_for_the_lag_margin(self):
        items = [out("new", NOW - 60, LOCKER, tr(JEX, 7 * E18))]
        api = FakeAPI(items)
        self.assertEqual(mvx_flows.engaged_positions(ME, api, now=NOW).positions, [])
        wait = mvx_flows.LAG_SECONDS + mvx_flows.SETTLE_SECONDS + 5      # old enough, and no answer in sight
        later = mvx_flows.engaged_positions(ME, api, now=NOW + wait)
        self.assertEqual(len(later.positions), 1)
        self.assertAlmostEqual(later.positions[0].amount, 7.0)
        again = mvx_flows.engaged_positions(ME, api, now=NOW + wait + 10)
        self.assertAlmostEqual(again.positions[0].amount, 7.0)        # never counted twice

    def test_answer_in_another_window_still_cancels_a_swap(self):
        self.cfg(2, 3)
        items = [noise("a", 4000), out("s", 5000, ROUTER, tr("USDC-c76f1f", 5_000_000, 6)),
                 back("r", 5012, "s", ROUTER, tr("WEGLD-bd4d79", E18)), noise("c", 6000)]
        seen = []
        api = FakeAPI(items)
        for _ in range(80):
            res = mvx_flows.engaged_positions(ME, api, now=SOON)
            seen.append(res.positions)
            if res.done:
                break
        self.assertTrue(res.done)
        self.assertTrue(all(p == [] for p in seen))

    def test_burst_bigger_than_a_page_in_one_second(self):
        self.cfg(3, 12)
        items = [out(f"b{i}", 1000, LOCKER, tr(JEX, E18)) for i in range(8)]
        res, _ = self.read_all(FakeAPI(items), now=SOON)
        self.assertAlmostEqual(res.positions[0].amount, 8.0)

    def test_api_ignoring_the_date_filters_disables_the_estimate(self):
        spread = [out("a", 1000, LOCKER, tr(JEX, E18)), out("b", 1000 + 200 * 86400, LOCKER, tr(JEX, E18))]
        res = mvx_flows.engaged_positions(ME, FakeAPI(spread, ignore_filters=True), now=NOW)
        self.assertTrue(res.error)
        self.assertEqual(res.positions, [])

    def test_fields_rejected_falls_back_to_plain_requests(self):
        res, _ = self.read_all(FakeAPI(self.history(), reject_fields=True), now=SOON)
        self.assertTrue(res.done)
        self.assertFalse(mvx_flows._use_fields)
        self.assertEqual(len(res.positions), 1)

    def test_empty_wallet_is_done_at_once(self):
        res = mvx_flows.engaged_positions(ME, FakeAPI([]), now=NOW)
        self.assertTrue(res.done)
        self.assertEqual(res.positions, [])

    def test_garbage_answer(self):
        res = mvx_flows.engaged_positions(ME, lambda s, p: {"error": 1} if s == "/transfers" else 3, now=NOW)
        self.assertTrue(res.error)

    def test_network_error_propagates(self):
        def boom(s, p):
            raise requests.ConnectionError("down")
        with self.assertRaises(requests.ConnectionError):
            mvx_flows.engaged_positions(ME, boom, now=NOW)


class TestRandomHistories(Base):
    def test_windows_always_match_a_single_pass(self):
        import random
        rnd = random.Random(7)
        semantics = ((True, True), (False, False), (True, False), (False, True))
        for trial in range(100):
            items, t = [], 1000
            for k in range(rnd.randint(1, 40)):
                t += rnd.choice([0, 1, 5, 30, 200, 900, 5000])
                kind = rnd.choice(["dep", "ret", "swap", "noise", "burst", "call"])
                tok = rnd.choice([JEX, "USDC-c76f1f"])
                c = rnd.choice([LOCKER, ROUTER])
                amt = rnd.randint(1, 9) * E18
                if kind == "dep":
                    items.append(out(f"h{trial}-{k}", t, c, tr(tok, amt)))
                elif kind == "ret":
                    items.append(back(f"r{trial}-{k}", t + rnd.choice([0, 12]), f"x{k}", c, tr(tok, amt)))
                elif kind == "swap":
                    items.append(out(f"s{trial}-{k}", t, c, tr(tok, amt)))
                    items.append(back(f"q{trial}-{k}", t + rnd.choice([0, 12, 40]), f"s{trial}-{k}", c, tr("WEGLD-bd4d79", amt)))
                elif kind == "call":     # the wallet calls a contract (nothing sent), ANOTHER one pays out
                    items.append(out(f"u{trial}-{k}", t, c))
                    items[-1]["action"] = {"arguments": {"transfers": [], "receiver": c}}
                    items.append(back(f"p{trial}-{k}", t + rnd.choice([0, 12, 60]), f"u{trial}-{k}",
                                      LOCKER if c == ROUTER else ROUTER, tr(tok, amt)))
                elif kind == "burst":
                    items += [noise(f"b{trial}-{k}-{j}", t) for j in range(rnd.randint(1, 5))]
                else:
                    items.append(noise(f"n{trial}-{k}", t))
            want = {(p.contract, p.token): p.amount for p in mvx_flows.compute_engaged(ME, items)}
            for size, budget in ((3, 8), (4, 6), (7, 4), (100, 12)):
                after_incl, before_incl = semantics[(trial + size) % 4]
                mvx_flows.clear_cache()
                with mock.patch.object(mvx_flows, "FLOW_PAGE_SIZE", size), mock.patch.object(mvx_flows, "FLOW_MAX_PAGES", budget):
                    res, _ = self.read_all(FakeAPI(items, after_incl, before_incl), now=t + 20_000,
                                           limit=3000, restart=True)
                got = {(p.contract, p.token): p.amount for p in res.positions}
                self.assertEqual(got.keys(), want.keys(), (trial, size, budget, after_incl, before_incl))
                for key in want:
                    self.assertAlmostEqual(got[key], want[key], places=6)


class TestPersistence(Base):
    def test_state_survives_a_restart_and_resumes(self):
        items = TestWindowReading.history(self)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mvx_flows.json")
            mvx_flows.set_cache_path(path)
            self.cfg(5, 14)
            api = FakeAPI(items)
            first = mvx_flows.engaged_positions(ME, api, now=SOON)
            self.assertFalse(first.done)
            saved_cursor = mvx_flows._states[ME].cursor
            self.assertGreater(saved_cursor, 1000)           # some history was read
            mvx_flows.save_cache()
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            # "restart": memory forgotten, file reloaded
            mvx_flows.clear_cache()
            mvx_flows._path = None
            mvx_flows.set_cache_path(path)
            api2 = FakeAPI(items)
            res, _ = self.read_all(api2, now=SOON)
            self.assertAlmostEqual(res.positions[0].amount, 999.9964424, places=6)
            first_call = api2.window_calls()[0]
            self.assertEqual(first_call.get("after"), saved_cursor - 1)   # resumed where it stopped

    def test_old_cache_format_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump({"v": 1, "wallets": {ME: {"cursor": 5000, "done": True, "net": {LOCKER + "|" + JEX: str(10 * E18)}}}}, f)
            mvx_flows.set_cache_path(path)
            self.assertNotIn(ME, mvx_flows._states)

    def test_corrupt_or_hostile_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            for blob in ("not json", "[]", json.dumps({"v": 2}), json.dumps({"v": 1, "wallets": {ME: {"cursor": "x"}}}),
                         json.dumps({"v": 1, "wallets": {ME: {"cursor": 5, "net": {"bad": "1", LOCKER + "|" + JEX: "oops"}}}}),
                         json.dumps({"v": 1, "wallets": {"junk": {}}})):
                path = os.path.join(d, "c.json")
                with open(path, "w") as f:
                    f.write(blob)
                mvx_flows._path = None
                mvx_flows.set_cache_path(path)
                res, _ = self.read_all(FakeAPI([out("a", 1000, LOCKER, tr(JEX, 2 * E18))]))
                self.assertAlmostEqual(res.positions[0].amount, 2.0)
                mvx_flows.clear_cache()

    def test_contract_name_lookup_is_cached_and_tolerant(self):
        calls = []
        def acc(c):
            calls.append(c)
            return {"assets": {"name": "JEXchange: Locker"}}
        self.assertEqual(mvx_flows.contract_name(LOCKER, acc), "JEXchange: Locker")
        self.assertEqual(mvx_flows.contract_name(LOCKER, acc), "JEXchange: Locker")
        self.assertEqual(len(calls), 1)
        mvx_flows.clear_cache()
        self.assertEqual(mvx_flows.contract_name(LOCKER, lambda c: 1 / 0), "")


class TestProvider(unittest.TestCase):
    def setUp(self):
        mvx_flows.set_cache_path(None)
        mvx_flows.clear_cache()
        mvx_flows._use_fields = True

    def _run(self, transfers_answer, env=None):
        def fake(method, url, **kw):
            if url.endswith("/transfers/count"):
                return 1
            if url.endswith("/transfers"):
                if isinstance(transfers_answer, Exception):
                    raise transfers_answer
                return FakeAPI(transfers_answer)("/transfers", kw.get("params", {}))
            if url.endswith(f"/accounts/{LOCKER}"):
                return {"assets": {"name": "JEXchange: Locker"}}
            if url.endswith(f"/accounts/{ME}"):
                return {"balance": "0"}
            return []
        with mock.patch.object(multiversx, "request_json", side_effect=fake), \
             mock.patch.dict(os.environ, env or {}):
            return multiversx.MultiversXProvider().get_balance(ME)

    def test_locked_position_becomes_priced_token_entry(self):
        w = self._run([out("ea46", 1_700_000_000, LOCKER, tr(JEX, 1000 * E18))])
        locked = [t for t in w.tokens if t.asset_type == "locked-estimate"]
        self.assertEqual(len(locked), 1)
        self.assertEqual((locked[0].contract, locked[0].symbol), (JEX, "JEX"))
        self.assertAlmostEqual(locked[0].amount, 1000.0)
        self.assertIn("estimated", locked[0].name)
        self.assertNotIn("still being read", w.warning or "")
        from pricing import CONTRACT_PRICED_TYPES
        self.assertIn("locked-estimate", CONTRACT_PRICED_TYPES)

    def test_provisional_warning_while_history_is_incomplete(self):
        many = [out(f"t{i}", 1_600_000_000 + i * 90_000, LOCKER, tr(JEX, E18)) for i in range(40)]
        with mock.patch.object(mvx_flows, "FLOW_MAX_PAGES", 2):
            w = self._run(many)       # not enough calls to reach the present: more history may follow
        self.assertIn("still being read", w.warning or "")

    def test_api_failure_only_adds_a_warning(self):
        w = self._run(requests.ConnectionError("boom"))
        self.assertFalse([t for t in w.tokens if t.asset_type == "locked-estimate"])
        self.assertIn("could not estimate locked tokens", w.warning or "")

    def test_can_be_disabled(self):
        w = self._run([out("ea46", 1_700_000_000, LOCKER, tr(JEX, 1000 * E18))], env={"MVX_FLOWS": "0"})
        self.assertFalse([t for t in w.tokens if t.asset_type == "locked-estimate"])

    def test_cache_path_follows_the_empty_cache_path(self):
        with tempfile.TemporaryDirectory() as d:
            multiversx.set_empty_cache_path(os.path.join(d, "mvx_empty.json"))
            self.assertEqual(mvx_flows._path, os.path.join(d, "mvx_flows.json"))
            multiversx.set_empty_cache_path(None)
            self.assertIsNone(mvx_flows._path)


if __name__ == "__main__":
    unittest.main()


class TestRouterFilter(unittest.TestCase):
    def _state(self, n_tokens):
        from providers import mvx_flows as m
        st = m.State()
        for i in range(n_tokens):
            k = f"erd1qqqqqqqqqqqqqpgqrouter0000000000000000000000000000000000|TK{i}-abcdef"
            st.net[k] = 10 ** 18
            st.meta[k] = ["", f"TK{i}", 18]
        return st

    def test_router_excluded_lock_kept(self):
        self.assertEqual(len(self._state(2).positions()), 2)
        self.assertEqual(self._state(6).positions(), [])

    def test_trust_and_ignore(self):
        import os
        os.environ["MVX_FLOWS_TRUST"] = "erd1qqqqqqqqqqqqqpgqrouter0000000000000000000000000000000000"
        try:
            self.assertEqual(len(self._state(6).positions()), 6)
            os.environ["MVX_FLOWS_IGNORE"] = "erd1qqqqqqqqqqqqqpgqrouter"
            self.assertEqual(len(self._state(2).positions()), 0)
        finally:
            os.environ.pop("MVX_FLOWS_TRUST", None)
            os.environ.pop("MVX_FLOWS_IGNORE", None)


class TestAggregatorFilter(unittest.TestCase):
    def test_negative_balances_count_as_breadth(self):
        from providers import mvx_flows as m
        st = m.State()
        c = "erd1qqqqqqqqqqqqqpgqagg0000000000000000000000000000000000"
        for i, v in enumerate([10 ** 18] + [-10 ** 18] * 5):
            k = f"{c}|T{i}-abcdef"
            st.net[k] = v
            st.meta[k] = ["", f"T{i}", 18]
        self.assertEqual(st.positions(), [])


class TestContractName(unittest.TestCase):
    def test_name_from_transfers_when_account_has_none(self):
        c = "erd1qqqqqqqqqqqqqpgq05whpg29ggrrm9ww3ufsf9ud23f66msv6avs5s5xxy"
        mvx_flows._names.pop(c, None)
        rows = [{"sender": ME, "receiver": c, "receiverAssets": {"name": "JEXchange: Locker"}}]
        got = mvx_flows.contract_name(c, lambda _c: {"address": c}, lambda _c: rows)
        self.assertEqual(got, "JEXchange: Locker")
        self.assertEqual(mvx_flows.contract_name(c, lambda _c: 1 / 0), "JEXchange: Locker")   # cached
        mvx_flows._names.pop(c, None)


class TestHints(unittest.TestCase):
    def test_deposit_and_exit_functions(self):
        C = "erd1qqqqqqqqqqqqqpgq05whpg29ggrrm9ww3ufsf9ud23f66msv6avs5s5xxy"
        mvx_flows._hints.clear()
        h = "ab" * 32
        mine = [{"sender": ME, "receiver": C, "txHash": h, "timestamp": 5, "function": "lock", "type": "Transaction",
                 "action": {"arguments": {"receiver": C, "transfers": [{"type": "FungibleESDT", "token": JEX, "value": "1", "decimals": 18}]}}}]
        contract = [{"receiver": C, "function": "unlock", "type": "Transaction"},
                    {"receiver": C, "function": "unlock", "type": "Transaction"},
                    {"receiver": C, "function": "extendLockPeriod", "type": "Transaction"},
                    {"receiver": C, "function": "lock", "type": "Transaction"},
                    {"receiver": C, "function": "x", "type": "Transaction",
                     "action": {"arguments": {"transfers": [{"type": "FungibleESDT", "token": JEX, "value": "1", "decimals": 18}]}}}]
        pos = mvx_flows.Position(C, "", JEX, "JEX", 1.0)
        budget = [1]
        deps, exits = mvx_flows.position_hints(ME, pos, lambda c: mine, lambda c: contract, budget)
        self.assertEqual(deps, [[h, "lock", 5]])
        self.assertEqual(exits, ["unlock", "extendLockPeriod"])
        self.assertEqual(budget, [0])
        mvx_flows.position_hints(ME, pos, lambda c: 1 / 0, lambda c: 1 / 0, budget)   # cached: no read
        self.assertTrue(any(h in l for l in mvx_flows.describe_hints(ME)))
        mvx_flows._hints.clear()
