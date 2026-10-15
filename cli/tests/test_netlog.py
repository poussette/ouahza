"""The per-host request log shown by the app's "Logs" button (offline)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import requests

from providers import net
from providers.net import HttpError
from test_security import FakeResp

net.THROTTLE_ENABLED = False


class NetLogTests(unittest.TestCase):
    def setUp(self):
        net.reset_stats()

    def test_empty(self):
        self.assertEqual(net.stats_report(), "Aucune requête.")

    def test_calls_time_and_hosts_all_listed(self):
        with mock.patch("requests.request", lambda *a, **k: FakeResp({"ok": 1})):
            for host in ("a.test", "b.test", "c.test", "d.test", "e.test", "e.test"):
                net.request_json("GET", f"https://{host}/x")
        out = net.stats_report()
        for host in ("a.test", "b.test", "c.test", "d.test", "e.test"):
            self.assertIn(host, out)          # more than the 4 hosts of the one-line summary
        self.assertIn("6 appel(s)", out)
        self.assertIn("aucune erreur", out)
        self.assertIn("2 appel(s) ·", out)    # e.test, listed first
        self.assertLess(out.index("e.test"), out.index("a.test"))
        self.assertGreaterEqual(net.STATS["a.test"]["time"], 0.0)

    def test_retries_waits_and_errors_are_reported(self):
        seq = iter([FakeResp({}, status=429), FakeResp({}, status=503), FakeResp({"ok": 1})])
        with mock.patch("requests.request", lambda *a, **k: next(seq)), \
                mock.patch.object(net, "_sleep", lambda s: None), \
                mock.patch.object(net, "RETRY_DELAYS", (2.0, 3.0)):
            net.request_json("GET", "https://r.test/")
        out = net.stats_report()
        self.assertIn("3 appel(s)", out)
        self.assertIn("2 nouvel(les) essai(s) (pause 5 s)", out)
        self.assertIn("1× HTTP 429", out)
        self.assertIn("1× HTTP 503", out)
        self.assertIn("2 erreur(s)", out)

    def test_transport_errors_and_final_http_error(self):
        def boom(*a, **k):
            raise requests.ConnectionError("secret-url https://u.test/KEY123")
        with mock.patch("requests.request", boom), mock.patch.object(net, "RETRY_DELAYS", ()):
            with self.assertRaises(HttpError):
                net.request_json("GET", "https://down.test/")
        with mock.patch("requests.request", lambda *a, **k: FakeResp({}, status=404)), \
                mock.patch.object(net, "RETRY_DELAYS", ()):
            with self.assertRaises(HttpError):
                net.request_json("GET", "https://nf.test/")
        out = net.stats_report()
        self.assertIn("1× ConnectionError", out)
        self.assertIn("1× HTTP 404", out)
        self.assertNotIn("KEY123", out)       # only fixed labels, never the server's/URL's text

    def test_throttle_wait_shown_and_reset_clears_everything(self):
        net.STATS["w.test"] = {"calls": 3, "wait": 12.4, "r429": 0, "time": 1.0}
        self.assertIn("attente 12 s", net.stats_report())
        net._note_error("w.test", "HTTP 500")
        net.reset_stats()
        self.assertEqual((net.STATS, net.ERRORS), ({}, {}))

    def test_label_variety_is_bounded(self):
        for i in range(100):
            net._note_error("x.test", f"E{i}")
        self.assertLessEqual(len(net.ERRORS["x.test"]), 12)

    def test_data_api_host_keeps_its_friendly_name(self):
        with mock.patch("requests.request", lambda *a, **k: FakeResp({"ok": 1})):
            net.request_json("GET", "https://data-api.mvx.fr/x")
        self.assertIn("data-api-ecompass", net.stats_report())


if __name__ == "__main__":
    unittest.main()
