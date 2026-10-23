"""Deposit and withdrawal through different contracts of one protocol (Hatom market in, controller out)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "core"))

from providers import mvx_flows

E18 = 10 ** 18
MARKET = "erd1qqqqqqqqqqqqqpgqta0tv8d5pjzmwzshrtw62n4nww9kxtl278ssspxpxu"
CONTROLLER = "erd1qqqqqqqqqqqqqpgqxp28qpnv7rfcmk6qrgxgw5uf2fnp84ar78ssqdk6hr"
LOCKER = "erd1qqqqqqqqqqqqqpgq05whpg29ggrrm9ww3ufsf9ud23f66msv6avs5s5xxy"
UTK = "UTK-2f80e9"


def state(rows):
    st = mvx_flows.State()
    for contract, name, token, units in rows:
        k = f"{contract}|{token}"
        st.net[k] = int(units * E18)
        st.meta[k] = [name, token.split("-")[0], 18]
        if name:
            st.cnames[contract] = name
    return st


class TestProtocolNet(unittest.TestCase):
    def test_withdrawn_through_another_module_is_not_a_position(self):
        st = state([(MARKET, "Hatom: UTK Money Market", UTK, 483986), (CONTROLLER, "Hatom: Controller", UTK, -484014)])
        self.assertEqual(st.positions(), [])

    def test_rewards_from_another_module_do_not_shrink_a_lock(self):
        """JEXchange: Staking Module pays JEX rewards, the Locker still holds the locked JEX."""
        staking = "erd1qqqqqqqqqqqqqpgqwkqnf30j7hj4r797kahr0p5t5nsksc8a73eqd732jd"
        st = state([(LOCKER, "JEXchange: Locker", "JEX-9040ca", 55154), (staking, "JEXchange: Staking Module", "JEX-9040ca", -33249)])
        pos = st.positions()
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(pos[0].amount, 55154.0)

    def test_a_lock_is_not_hidden_by_huge_flows_of_other_modules(self):
        """Seen on a real wallet: 1,000 JEX locked, JEXchange modules paid out millions of JEX."""
        mods = [("erd1qqqqqqqqqqqqqpgqwkqnf30j7hj4r797kahr0p5t5nsksc8a73eqd732jd", "JEXchange: Staking Module", -1105483),
                ("erd1qqqqqqqqqqqqqpgq360nakqgsp5zkmguptucpjy6n4n3du7e5snsd2swzq", "JEXchange: Aggregator v2", -1190540)]
        st = state([(LOCKER, "JEXchange: Locker", "JEX-9040ca", 1000)] + [(c, n, "JEX-9040ca", v) for c, n, v in mods])
        self.assertEqual([p.amount for p in st.positions()], [1000.0])
        self.assertFalse(any("withdrawn elsewhere" in l for l in st.describe()))

    def test_seen_on_a_real_wallet_sEGLD(self):
        """Market +9,671, controller -12,050, liquid staking -20,214 (mints): same order of size -> closed."""
        rows = [(MARKET, "Hatom: sEGLD Money Market", "SEGLD-3ad2d0", 9671), (CONTROLLER, "Hatom: Controller", "SEGLD-3ad2d0", -12050),
                ("erd1qqqqqqqqqqqqqpgq4gzfcw7kmkjy8zsf04ce6dl0auhtzjx078sslvrf4e", "Hatom: Liquid Staking", "SEGLD-3ad2d0", -20214)]
        self.assertEqual(state(rows).positions(), [])

    def test_other_protocols_and_unnamed_contracts_are_not_netted(self):
        st = state([(LOCKER, "JEXchange: Locker", "JEX-9040ca", 1000), (CONTROLLER, "Hatom: Controller", "JEX-9040ca", -5000)])
        self.assertEqual([p.amount for p in st.positions()], [1000.0])
        st = state([(MARKET, "", UTK, 10), (CONTROLLER, "", UTK, -50)])
        self.assertEqual([p.amount for p in st.positions()], [10.0])

    def test_result_does_not_depend_on_insertion_order(self):
        other = "erd1qqqqqqqqqqqqqpgqz9pvuz22qvqxfqpk6r3rluj0u2can55c78ssgcqs00"
        rows = [(MARKET, "Hatom: A", UTK, 3), (other, "Hatom: B", UTK, 3), (CONTROLLER, "Hatom: Controller", UTK, -2)]
        a = {p.contract: p.amount for p in state(rows).positions()}
        b = {p.contract: p.amount for p in state(list(reversed(rows))).positions()}
        self.assertEqual(a, b)

    def test_names_survive_a_restart_even_at_zero_balance(self):
        st = state([(MARKET, "", UTK, 100), (CONTROLLER, "", UTK, -100)])
        st.cnames = {MARKET: "Hatom: UTK Money Market", CONTROLLER: "Hatom: Controller"}
        back = mvx_flows.State.from_json(st.to_json())
        self.assertEqual(back.cnames, st.cnames)
        back.net = dict(st.net)
        back.meta = dict(st.meta)
        self.assertEqual(back.positions(), [])

    def test_old_cache_without_names_is_seeded_from_the_ledger(self):
        st = state([(MARKET, "Hatom: UTK Money Market", UTK, 100), (CONTROLLER, "Hatom: Controller", UTK, -100)])
        doc = st.to_json()
        del doc["cnames"]
        back = mvx_flows.State.from_json(doc)
        self.assertEqual(back.positions(), [])

    def test_payer_name_is_not_written_on_the_called_contract(self):
        me, other = "erd1" + "a" * 58, "erd1qqqqqqqqqqqqqpgqa0fsfshnff4n76jhcye6k7uvd7qacsq42jpsp6shh2"
        call = {"txHash": "u1", "originalTxHash": "u1", "sender": me, "receiver": other, "status": "success", "timestamp": 10,
                "type": "Transaction", "action": {"arguments": {"transfers": [], "receiver": other}}}
        pay = {"txHash": "p1", "originalTxHash": "u1", "sender": LOCKER, "receiver": me, "status": "success", "timestamp": 10,
               "type": "SmartContractResult", "senderAssets": {"name": "JEXchange: Locker"},
               "action": {"arguments": {"receiver": me, "transfers": [
                   {"type": "FungibleESDT", "token": UTK, "ticker": "UTK", "decimals": 18, "value": str(E18)}]}}}
        st = mvx_flows.State()
        st.integrate(me, [call, pay], 10 ** 9)
        self.assertEqual(st.meta[f"{other}|{UTK}"][0], "")
        self.assertEqual(st.cnames, {LOCKER: "JEXchange: Locker"})

    def test_ignore_one_wallet_only(self):
        w1, w2 = "erd1efe954adel2jchnl98xt2gnkrtz0gwpap5ap2mzwvuez0d5344qs2llhcv", "erd1" + "c" * 58
        os.environ["MVX_FLOWS_IGNORE"] = f"{w1}:{LOCKER}"
        try:
            for wallet, expected in ((w1, 0), (w2, 1)):
                st = state([(LOCKER, "JEXchange: Locker", "JEX-9040ca", 5)])
                st.address = wallet
                self.assertEqual(len(st.positions()), expected, wallet)
            os.environ["MVX_FLOWS_IGNORE"] = f"{w1[:10]}:{LOCKER[-10:]}"     # 8+ chars are enough
            st = state([(LOCKER, "JEXchange: Locker", "JEX-9040ca", 5)])
            st.address = w1
            self.assertEqual(st.positions(), [])
            os.environ["MVX_FLOWS_IGNORE"] = LOCKER                        # every wallet
            st.address = w2
            self.assertEqual(st.positions(), [])
        finally:
            os.environ.pop("MVX_FLOWS_IGNORE", None)

    def test_can_be_disabled(self):
        st = state([(MARKET, "Hatom: UTK Money Market", UTK, 100), (CONTROLLER, "Hatom: Controller", UTK, -100)])
        os.environ["MVX_FLOWS_GROUP"] = "0"
        try:
            self.assertEqual(len(st.positions()), 1)
        finally:
            os.environ.pop("MVX_FLOWS_GROUP", None)

    def test_describe_flags_it(self):
        st = state([(MARKET, "Hatom: UTK Money Market", UTK, 100), (CONTROLLER, "Hatom: Controller", UTK, -100)])
        self.assertTrue(any("withdrawn elsewhere" in l for l in st.describe()))


if __name__ == "__main__":
    unittest.main()
