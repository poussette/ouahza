"""A wallet full of spam tokens must not keep the price pass running for minutes."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import requests

import pricing
from providers import net
from providers.base import TokenBalance, WalletBalance
from providers.solana import UNKNOWN_TOKEN_NAME
from test_security import FakeResp

net.THROTTLE_ENABLED = False
net.RETRY_DELAYS = ()


def mint(i):
    return "A" * 30 + f"{i:014d}"          # 44 characters, one per index


def wallet(named, unnamed):
    w = WalletBalance("solana", "S" * 44, "SOL", native_amount=1.0)
    for i in range(named):
        w.tokens.append(TokenBalance(f"T{i}", f"Token {i}", 1.0, contract=mint(i)))
    for i in range(unnamed):
        w.tokens.append(TokenBalance("x…", UNKNOWN_TOKEN_NAME, 1.0, contract=mint(10_000 + i)))
    return w


class Net:
    """Fake CoinGecko: records every token-price request."""
    def __init__(self, token_answer=None):
        self.token_calls = []
        self.token_answer = token_answer

    def __call__(self, method, url, **kw):
        if "simple/price" in url:
            return FakeResp({"solana": {"usd": 100, "eur": 90}})
        if "token_price" in url:
            self.token_calls.append(kw["params"]["contract_addresses"].split(","))
            if callable(self.token_answer):
                return self.token_answer(self.token_calls[-1])
            return FakeResp({})
        raise requests.ConnectionError("no route")


class PricingLimitTests(unittest.TestCase):
    def setUp(self):
        pricing.clear_cache()
        net.reset_stats()

    def price(self, w, fake):
        with mock.patch("requests.request", fake):
            return pricing.apply_pricing([w])

    def test_number_of_requests_is_bounded(self):
        fake = Net()
        self.price(wallet(0, 1977), fake)
        self.assertEqual(len(fake.token_calls), pricing.MAX_CONTRACTS_PER_CHAIN // 50)
        self.assertTrue(any("1727" in n and "solana" in n for n in pricing.LAST_NOTES), pricing.LAST_NOTES)

    def test_named_tokens_are_priced_before_unnamed_ones(self):
        fake = Net()
        self.price(wallet(60, 800), fake)
        asked = {c for call in fake.token_calls for c in call}
        self.assertTrue(all(mint(i) in asked for i in range(60)))
        self.assertEqual(len(asked), pricing.MAX_CONTRACTS_PER_CHAIN)

    def test_stops_after_two_refused_requests(self):
        def refuse(batch):
            raise requests.ConnectionError("429")
        fake = Net(refuse)
        self.price(wallet(0, 1000), fake)
        self.assertEqual(len(fake.token_calls), pricing.TOKEN_FAIL_LIMIT)
        self.assertTrue(any("non valorisé" in n for n in pricing.LAST_NOTES))

    def test_a_good_answer_resets_the_failure_count(self):
        answers = iter(["bad", "ok", "bad", "ok", "ok"])

        def mixed(batch):
            if next(answers) == "bad":
                raise requests.ConnectionError("boom")
            return FakeResp({})
        fake = Net(mixed)
        self.price(wallet(0, 1000), fake)
        self.assertEqual(len(fake.token_calls), 5)        # never two failures in a row

    def test_time_budget_stops_new_requests(self):
        fake = Net()
        with mock.patch.object(pricing, "TOKEN_PRICE_BUDGET", -1.0):
            self.price(wallet(0, 500), fake)
        self.assertEqual(fake.token_calls, [])
        self.assertTrue(any("non valorisé" in n for n in pricing.LAST_NOTES))

    def test_small_wallet_is_untouched(self):
        fake = Net(lambda batch: FakeResp({batch[0]: {"usd": 2.0, "eur": 1.8}}))
        w = wallet(3, 0)
        self.price(w, fake)
        self.assertEqual(len(fake.token_calls), 1)
        self.assertEqual(pricing.LAST_NOTES, [])
        self.assertEqual(w.tokens[0].usd_value, 2.0)

    def test_notes_do_not_leak_into_the_next_run(self):
        self.price(wallet(0, 600), Net())
        self.assertTrue(pricing.LAST_NOTES)
        pricing.clear_cache()
        self.price(wallet(2, 0), Net())
        self.assertEqual(pricing.LAST_NOTES, [])


if __name__ == "__main__":
    unittest.main()
