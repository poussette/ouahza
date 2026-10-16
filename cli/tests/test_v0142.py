import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "core"))

import requests
from providers import ethereum


class TestV0142(unittest.TestCase):
    def test_llamarpc_is_last(self):
        self.assertEqual(ethereum._DEFAULT_RPCS[-1], "https://eth.llamarpc.com")
        self.assertEqual(len(set(ethereum._DEFAULT_RPCS)), len(ethereum._DEFAULT_RPCS))

    def test_beacon_401_gives_clear_warning(self):
        p = ethereum.EthereumProvider()
        resp = mock.Mock(status_code=401)
        err = requests.HTTPError("HTTP 401 from beaconcha.in", response=resp)
        with mock.patch.object(p, "_get_native_balance", return_value=1.0), \
             mock.patch.object(p, "_get_beacon_validators", side_effect=err), \
             mock.patch.dict(os.environ, {"ETHERSCAN_API_KEY": ""}):
            w = p.get_balance("0x" + "1" * 40)
        self.assertIn("requires an API key", w.warning or "")


if __name__ == "__main__":
    unittest.main()
