"""Solana token names come from Jupiter's search API: one shared lookup, never a crash."""
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import requests

from providers import net, solana
from providers.net import HttpError

net.THROTTLE_ENABLED = False
net.RETRY_DELAYS = ()

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
ODD = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def acct(mint, amount="1"):
    return {"account": {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"uiAmountString": amount}}}}}}


def make(mints, jupiter):
    """Fake network: RPC answers for a wallet holding `mints`; `jupiter(url)` answers the lookup."""
    calls = []

    def fake(method, url, **kw):
        if "jup.ag" in url:
            calls.append(url)
            return jupiter(url)
        m = (kw.get("json_body") or {}).get("method")
        if m == "getBalance":
            return {"result": {"value": 10**9}}
        if m == "getTokenAccountsByOwner":
            return {"result": {"value": [acct(x) for x in mints]}}
        return {"result": []}
    fake.calls = calls
    return fake


def good(url):
    wanted = url.split("query=", 1)[1].split(",")
    names = {USDC: ("USDC", "USD Coin"), USDT: ("USDT", "USDT")}
    return [{"id": m, "symbol": names[m][0], "name": names[m][1]} for m in wanted if m in names]


class TokenMetaTests(unittest.TestCase):
    def setUp(self):
        solana._token_meta_cache.clear()
        solana._token_meta_failed_at = None

    def read(self, fake, addr="A" * 44):
        with mock.patch.object(solana, "request_json", side_effect=fake):
            return solana.SolanaProvider().get_balance(addr)

    def test_names_come_from_the_search_api_in_one_request(self):
        fake = make([USDC, USDT], good)
        w = self.read(fake)
        self.assertEqual({t.contract: (t.symbol, t.name) for t in w.tokens},
                         {USDC: ("USDC", "USD Coin"), USDT: ("USDT", "USDT")})
        self.assertEqual(len(fake.calls), 1)
        self.assertIn(f"query={USDC},{USDT}", fake.calls[0])

    def test_second_wallet_reuses_the_answer(self):
        fake = make([USDC], good)
        self.read(fake)
        w = self.read(fake, "B" * 44)
        self.assertEqual(w.tokens[0].symbol, "USDC")
        self.assertEqual(len(fake.calls), 1)

    def test_unknown_mint_is_asked_once_and_stays_short(self):
        fake = make([ODD], lambda url: [])
        w = self.read(fake)
        self.assertEqual((w.tokens[0].symbol, w.tokens[0].name), (ODD[:6] + "…", "Unknown SPL token"))
        self.read(fake, "B" * 44)
        self.assertEqual(len(fake.calls), 1)

    def test_failure_keeps_short_form_and_is_not_retried_at_once(self):
        def down(url):
            raise HttpError("down")
        fake = make([USDC], down)
        w = self.read(fake)
        self.assertIsNone(w.error)
        self.assertEqual(w.tokens[0].symbol, USDC[:6] + "…")
        self.read(fake, "B" * 44)
        self.assertEqual(len(fake.calls), 1)          # not hammered
        solana._token_meta_failed_at -= 120           # a minute later: tries again
        fake2 = make([USDC], good)
        self.assertEqual(self.read(fake2, "C" * 44).tokens[0].symbol, "USDC")

    def test_wrong_shaped_answers_do_not_crash(self):
        for bad in ({"error": "x"}, "text", None, [None, 5, "x", {"id": 7}, {"id": USDC, "symbol": ["no"]}]):
            solana._token_meta_cache.clear()
            solana._token_meta_failed_at = None
            w = self.read(make([USDC], lambda url, b=bad: b))
            self.assertIsNone(w.error, bad)
            self.assertEqual(len(w.tokens), 1, bad)

    def test_answer_for_a_mint_nobody_asked_is_ignored(self):
        w = self.read(make([USDC], lambda url: [{"id": USDT, "symbol": "EVIL", "name": "x"}]))
        self.assertEqual(w.tokens[0].symbol, USDC[:6] + "…")
        self.assertNotIn(USDT, solana._token_meta_cache)

    def test_many_mints_are_chunked_and_capped(self):
        mints = ["1" * 31 + chr(c1) + chr(c2) for c1 in range(ord("a"), ord("z") + 1) for c2 in range(ord("a"), ord("z") + 1)][:300]
        fake = make(mints, lambda url: [])
        self.read(fake)
        self.assertEqual(len(fake.calls), solana._META_MAX_MINTS // solana._META_CHUNK)
        self.assertTrue(all(len(u.split("query=")[1].split(",")) <= solana._META_CHUNK for u in fake.calls))

    def test_wallets_read_in_parallel_share_a_single_lookup(self):
        gate = threading.Event()

        def slow(url):
            gate.wait(2)
            return good(url)
        fake = make([USDC], slow)
        out = []
        with mock.patch.object(solana, "request_json", side_effect=fake):
            ts = [threading.Thread(target=lambda a=a: out.append(solana.SolanaProvider().get_balance(a * 44)))
                  for a in "ABC"]
            for t in ts:
                t.start()
            gate.set()
            for t in ts:
                t.join()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([w.tokens[0].symbol for w in out], ["USDC"] * 3)

    def test_wallet_without_tokens_makes_no_lookup(self):
        fake = make([], good)
        self.read(fake)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
