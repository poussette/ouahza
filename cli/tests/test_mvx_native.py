"""A contract that pays native EGLD back (unwrap, swap to EGLD) makes the deposit an exchange."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "core"))

from providers import mvx_flows

ME = "erd1" + "a" * 58
WEGLD = "erd1qqqqqqqqqqqqqpgqhe8t5jewej70zupmh44jurgn29psua5l2jps3ntjj3"
E18 = 10 ** 18


def call(h, ts, contract, token="WEGLD-bd4d79", func="unwrapEgld", value=5 * E18):
    return {"txHash": h, "originalTxHash": h, "sender": ME, "receiver": contract, "status": "success",
            "timestamp": ts, "type": "Transaction", "function": func,
            "action": {"arguments": {"receiver": contract, "transfers": [
                {"type": "FungibleESDT", "token": token, "ticker": token.split("-")[0], "decimals": 18, "value": str(value)}]}}}


def scr(h, ts, contract, value, nonce, n=0):
    return {"txHash": f"{h[:-1]}{n}", "originalTxHash": h, "sender": contract, "receiver": ME, "status": "success",
            "timestamp": ts, "type": "SmartContractResult", "function": "transfer", "value": str(value), "nonce": nonce}


class TestNative(unittest.TestCase):
    def run_ledger(self, items):
        return mvx_flows.compute_engaged(ME, items)

    def test_unwrap_is_not_a_lock(self):
        h = "a" * 64
        items = [call(h, 100, WEGLD),
                 scr(h, 100, WEGLD, 5 * E18, 0, 1),                  # the EGLD coming back
                 scr(h, 100, WEGLD, 19328440000000, 31156, 2)]       # gas refund
        self.assertEqual(self.run_ledger(items), [])

    def test_gas_refund_alone_does_not_cancel_a_lock(self):
        h = "b" * 64
        items = [call(h, 100, WEGLD, func="lock"), scr(h, 100, WEGLD, 19328440000000, 31156, 2)]
        pos = self.run_ledger(items)
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(pos[0].amount, 5.0)

    def test_payback_listed_without_nonce(self):
        """What /transfers really returns: the payback has no `nonce` key, the refund is not listed."""
        h = "c" * 64
        payback = scr(h, 100, WEGLD, 5 * E18, 0, 1)
        del payback["nonce"]
        self.assertEqual(self.run_ledger([call(h, 100, WEGLD), payback]), [])

    def test_native_back_helper(self):
        self.assertTrue(mvx_flows._native_back({"nonce": 0, "value": "1"}))
        self.assertFalse(mvx_flows._native_back({"nonce": 7, "value": "1"}))
        self.assertFalse(mvx_flows._native_back({"nonce": 0, "value": "0"}))
        self.assertFalse(mvx_flows._native_back({"nonce": True, "value": "1"}))
        self.assertTrue(mvx_flows._native_back({"value": "1"}))
        self.assertFalse(mvx_flows._native_back({"value": "0"}))
        self.assertFalse(mvx_flows._native_back({"nonce": 0, "value": "x"}))


if __name__ == "__main__":
    unittest.main()
