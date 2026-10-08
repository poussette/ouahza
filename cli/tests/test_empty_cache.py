"""MultiversX: legacy delegation / validator stake answered "nothing" are remembered for 24 h."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import requests

from providers import multiversx as mvx
from providers import net
from test_security import FakeResp

net.THROTTLE_ENABLED = False
net.RETRY_DELAYS = ()
REAL_TIME = time.time
ADDR = "erd1" + "q" * 58
OTHER = "erd1" + "w" * 58


def api(legacy=None, stake=None, fail=()):
    """Fake api.multiversx.com; records which paths were asked."""
    calls = []

    def fake(method, url, **kw):
        path = url.split("api.multiversx.com", 1)[1]
        calls.append(path)
        for key in fail:
            if path.endswith(key):
                raise requests.ConnectionError("down")
        if path.endswith("/delegation-legacy"):
            return FakeResp(legacy if legacy is not None else {})
        if path.endswith("/stake"):
            return FakeResp(stake if stake is not None else {})
        if path.endswith("/delegation") or path.endswith("/tokens") or path.endswith("/nfts") or "/tokens?" in path:
            return FakeResp([])
        return FakeResp({"balance": str(10**18)})
    fake.calls = calls
    return fake


def asked(fake, suffix):
    return sum(1 for c in fake.calls if c.split("?")[0].endswith(suffix))


class EmptyCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "mvx_empty.json")
        mvx.set_empty_cache_path(None)
        mvx.set_empty_cache_path(self.path)

    def tearDown(self):
        mvx.set_empty_cache_path(None)
        self.tmp.cleanup()

    def balance(self, fake, addr=ADDR):
        with mock.patch("requests.request", fake):
            return mvx.MultiversXProvider().get_balance(addr)

    def test_second_read_skips_both_lookups(self):
        f1, f2 = api(), api()
        self.balance(f1)
        self.assertEqual((asked(f1, "/delegation-legacy"), asked(f1, "/stake")), (1, 1))
        self.balance(f2)
        self.assertEqual((asked(f2, "/delegation-legacy"), asked(f2, "/stake")), (0, 0))
        self.assertEqual(asked(f2, "/delegation"), 1)      # staking pools: always read (rewards grow)

    def test_unconfigured_cache_always_asks(self):
        mvx.set_empty_cache_path(None)
        f1, f2 = api(), api()
        self.balance(f1)
        self.balance(f2)
        self.assertEqual(asked(f2, "/delegation-legacy"), 1)

    def test_position_found_is_never_marked_and_stays_visible(self):
        legacy = {"userStake": str(5 * 10**18)}
        stake = {"totalStaked": str(2500 * 10**18)}
        for _ in range(2):
            f = api(legacy=legacy, stake=stake)
            w = self.balance(f)
            self.assertEqual(asked(f, "/delegation-legacy"), 1)   # asked every time
            self.assertEqual(asked(f, "/stake"), 1)
            self.assertEqual({t.asset_type for t in w.tokens}, {"delegation-legacy", "validator-stake"})

    def test_position_appearing_later_replaces_the_empty_mark(self):
        self.balance(api())
        with mock.patch.object(mvx.time, "time", lambda: REAL_TIME() + mvx.EMPTY_TTL + 10):
            f = api(legacy={"userStake": str(10**18)})
            w = self.balance(f)
        self.assertEqual(asked(f, "/delegation-legacy"), 1)
        self.assertIn("delegation-legacy", {t.asset_type for t in w.tokens})
        f = api(legacy={"userStake": str(10**18)})
        self.balance(f)
        self.assertEqual(asked(f, "/delegation-legacy"), 1)       # no empty mark remains

    def test_errors_are_not_remembered(self):
        f = api(fail=("/delegation-legacy", "/stake"))
        w = self.balance(f)
        self.assertIn("legacy delegation", w.warning)
        f2 = api()
        self.balance(f2)
        self.assertEqual((asked(f2, "/delegation-legacy"), asked(f2, "/stake")), (1, 1))

    def test_other_wallet_not_affected(self):
        self.balance(api())
        f = api()
        self.balance(f, OTHER)
        self.assertEqual(asked(f, "/delegation-legacy"), 1)

    def test_persisted_and_expiring(self):
        self.balance(api())
        mvx.save_empty_cache()
        doc = json.load(open(self.path))
        self.assertEqual(set(doc["empty"][ADDR]), {"legacy", "stake"})
        mvx.set_empty_cache_path(None)
        mvx.set_empty_cache_path(self.path)                      # app restarted
        f = api()
        self.balance(f)
        self.assertEqual(asked(f, "/delegation-legacy"), 0)
        with mock.patch.object(mvx.time, "time", lambda: REAL_TIME() + mvx.EMPTY_TTL + 10):
            f = api()
            self.balance(f)
        self.assertEqual(asked(f, "/delegation-legacy"), 1)

    def test_hostile_file_ignored(self):
        now = time.time()
        for blob in (
            "not json", "[]", json.dumps({"v": 2, "empty": {ADDR: {"legacy": now}}}),
            json.dumps({"v": 1, "empty": {"evil": {"legacy": now}}}),
            json.dumps({"v": 1, "empty": {ADDR: {"legacy": "x", "stake": True, "other": now}}}),
            json.dumps({"v": 1, "empty": {ADDR: {"legacy": now - mvx.EMPTY_TTL - 5}}}),
            json.dumps({"v": 1, "empty": {ADDR: {"legacy": now + 10**9}}}),
        ):
            with open(self.path, "w") as f:
                f.write(blob)
            mvx.set_empty_cache_path(None)
            mvx.set_empty_cache_path(self.path)
            self.assertFalse(mvx._EMPTY.is_empty(ADDR, "legacy"), blob)
            self.assertFalse(mvx._EMPTY.is_empty(ADDR, "stake"), blob)

    def test_same_path_keeps_live_cache(self):
        self.balance(api())
        mvx.set_empty_cache_path(self.path)                      # called again by the app each refresh
        f = api()
        self.balance(f)
        self.assertEqual(asked(f, "/delegation-legacy"), 0)


if __name__ == "__main__":
    unittest.main()
