"""Private gateway: X-Api-Key header, never leaked to the public gateway, other hosts or error text."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

from providers import lp, net, safe  # noqa: E402
from test_lp import FakeResp  # noqa: E402

KEY = "cac8ae0d-2250-4c21-afbb-e297c73b503e"


class GatewayKeyTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        net.THROTTLE_ENABLED = False

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_header_for_private_gateway(self):
        os.environ["MULTIVERSX_GATEWAY_URL"] = "https://data-api.mvx.fr"
        os.environ["MULTIVERSX_GATEWAY_KEY"] = KEY
        self.assertEqual(lp.gateway_headers(), {"X-Api-Key": KEY})

    def test_never_sent_to_the_public_gateway(self):
        os.environ.pop("MULTIVERSX_GATEWAY_URL", None)
        os.environ["MULTIVERSX_GATEWAY_KEY"] = KEY
        self.assertIsNone(lp.gateway_headers())

    def test_no_key_no_header_and_bad_key_ignored(self):
        os.environ["MULTIVERSX_GATEWAY_URL"] = "https://data-api.mvx.fr"
        os.environ.pop("MULTIVERSX_GATEWAY_KEY", None)
        self.assertIsNone(lp.gateway_headers())
        os.environ["MULTIVERSX_GATEWAY_KEY"] = "abc\r\nX-Evil: 1"
        self.assertIsNone(lp.gateway_headers())

    def test_header_reaches_the_request_and_stops_at_other_hosts(self):
        seen = []

        def fake(method, url, **kw):
            seen.append((url, kw.get("headers")))
            if "data-api" in url:
                return FakeResp(status=302, url=url, headers={"Location": "https://other.example.com/x"})
            return FakeResp({"ok": 1}, url=url)

        with mock.patch("requests.request", fake):
            net.request_json("GET", "https://data-api.mvx.fr/a", headers={"X-Api-Key": KEY})
        self.assertEqual(seen[0][1], {"X-Api-Key": KEY})
        self.assertIsNone(seen[1][1])               # redirect to another host: key dropped

    def test_key_is_redacted_from_messages(self):
        os.environ["MULTIVERSX_GATEWAY_KEY"] = KEY
        self.assertNotIn(KEY, safe.redact(f"boom {KEY} boom"))


if __name__ == "__main__":
    unittest.main()
