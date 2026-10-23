"""CLI: the whole transfer history is read in one run, with a visible progress."""
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers import mvx_flows

ME = "erd1" + "a" * 58
C = "erd1qqqqqqqqqqqqqpgq" + "q" * 42


def _history(n=1200, step=40000):
    now = int(time.time())
    t0 = now - n * step
    return [{
        "txHash": f"{i:064x}", "originalTxHash": f"{i:064x}", "sender": ME, "receiver": C,
        "status": "success", "timestamp": t0 + i * step, "type": "Transaction", "function": "stake",
        "action": {"arguments": {"receiver": C, "transfers": [
            {"type": "FungibleESDT", "token": "JEX-9040ca", "ticker": "JEX", "decimals": 18, "value": str(10 ** 18)}]}},
    } for i in range(n) if t0 + i * step < now - 3000]


def _api(items, fail_after=None):
    calls = [0]

    def get(suffix, params):
        calls[0] += 1
        if fail_after is not None and calls[0] > fail_after:
            raise OSError("network down")
        if suffix == "/transfers/count":
            return len(items)
        rows = [r for r in items if params.get("after", 0) < r["timestamp"] < params.get("before", 10 ** 12)]
        f = params.get("from", 0)
        return rows[f:f + params["size"]]
    return get


class TestCatchUp(unittest.TestCase):
    def setUp(self):
        mvx_flows.clear_cache()
        mvx_flows.set_cache_path(None)

    def test_reads_everything_and_reports_progress(self):
        items = _history()
        seen = []
        res = mvx_flows.catch_up(ME, _api(items), lambda r: seen.append(r.percent))
        self.assertTrue(res.done)
        self.assertGreater(len(seen), 1)                      # several rounds, not one
        self.assertEqual(seen[-1], None)                      # last round: complete
        self.assertTrue(all(p is None or 0 <= p <= 99 for p in seen))
        self.assertEqual(sorted(p for p in seen if p is not None), [p for p in seen if p is not None])
        self.assertAlmostEqual(res.positions[0].amount, len(items))
        self.assertEqual(mvx_flows.progress(ME), (len(items), len(items), True))

    def test_stops_on_error_and_resumes(self):
        items = _history()
        with self.assertRaises(OSError):
            mvx_flows.catch_up(ME, _api(items, fail_after=20))
        seen, total, done = mvx_flows.progress(ME)
        self.assertFalse(done)
        self.assertGreater(seen, 0)
        res = mvx_flows.catch_up(ME, _api(items))
        self.assertTrue(res.done)
        self.assertAlmostEqual(res.positions[0].amount, len(items))

    def test_waits_and_continues_on_rate_limit(self):
        items = _history()
        base = _api(items)
        n = [0]

        def flaky(suffix, params):
            if suffix == "/transfers/count":
                return base(suffix, params)
            n[0] += 1
            if n[0] in (3, 4, 20):
                raise RuntimeError("HTTP 429 from api.multiversx.com")
            return base(suffix, params)
        waits = []
        res = mvx_flows.catch_up(ME, flaky, on_wait=lambda d, e: waits.append(d), sleep=lambda s: None)
        self.assertTrue(res.done)
        self.assertEqual(waits, [5.0, 10.0, 5.0])          # backoff doubles, resets after a success
        self.assertAlmostEqual(res.positions[0].amount, len(items))

    def test_gives_up_after_repeated_429(self):
        def always(suffix, params):
            raise RuntimeError("HTTP 429 from api.multiversx.com")
        with self.assertRaises(RuntimeError):
            mvx_flows.catch_up(ME, always, sleep=lambda s: None)

    def test_other_errors_are_not_retried(self):
        waits = []
        with self.assertRaises(OSError):
            mvx_flows.catch_up(ME, _api(_history(), fail_after=2), on_wait=lambda d, e: waits.append(d), sleep=lambda s: None)
        self.assertEqual(waits, [])

    def test_cli_shows_percent(self):
        import main as cli
        from providers import net
        items = _history()
        err = io.StringIO()
        with mock.patch.object(net, "request_json", lambda m, url, params=None, **k: _api(items)(url[url.index(ME) + len(ME):], params or {})), \
                redirect_stderr(err):
            cli.catch_up_flows([(None, None, ME)])
        text = err.getvalue().replace("\r", "\n")
        self.assertIn("locked-estimate", text)
        self.assertIn("100 %", text)
        self.assertTrue(mvx_flows.progress(ME)[2])


if __name__ == "__main__":
    unittest.main()
