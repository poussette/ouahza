"""Token prices: DefiLlama first (many tokens per request), CoinGecko second (its keyless
API takes ONE contract per request: HTTP 400 / error_code 10012 beyond that, and 10-30
calls a minute). Every pass is bounded."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

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


def addr(i):
    return "0x" + f"{i:040x}"


def wallet(named, unnamed=0):
    w = WalletBalance("ethereum", "0x" + "e" * 40, "ETH", native_amount=1.0)
    for i in range(named):
        w.tokens.append(TokenBalance(f"T{i}", f"Token {i}", 1.0, contract=addr(i)))
    for i in range(unnamed):
        w.tokens.append(TokenBalance("x", UNKNOWN_TOKEN_NAME, 1.0, contract=addr(10_000 + i)))
    return w


class Net:
    """Fake DefiLlama + CoinGecko + FX. `llama(i) -> record | None` prices token i (default $2, confidence .99);
    `gecko(contract) -> FakeResp` answers one CoinGecko request (default: not priced). CoinGecko enforces
    its real rule: more than one contract per request is a 400."""
    def __init__(self, llama=None, gecko=None, llama_down=False, llama_raw=None):
        self.llama_calls, self.gecko_calls = [], []
        self.llama = llama if llama else (lambda i: {"price": 2.0, "confidence": 0.99})
        self.gecko = gecko
        self.llama_down, self.llama_raw = llama_down, llama_raw

    def __call__(self, method, url, **kw):
        if "coins.llama.fi" in url:
            ids = unquote(url.split("/prices/current/", 1)[1]).split(",")
            self.llama_calls.append(ids)
            if self.llama_down:
                raise requests.ConnectionError("down")
            if self.llama_raw is not None:
                return FakeResp(self.llama_raw)
            coins = {}
            for key in ids:
                rec = self.llama(int(key.split(":")[1], 16))
                if rec is not None:
                    coins[key] = rec
            return FakeResp({"coins": coins})
        if "simple/price" in url:
            return FakeResp({"ethereum": {"usd": 100, "eur": 90}})
        if "token_price" in url:
            batch = kw["params"]["contract_addresses"].split(",")
            self.gecko_calls.append(batch)
            if len(batch) > 1:
                return FakeResp({"error_code": 10012}, status=400)
            return self.gecko(batch[0]) if self.gecko else FakeResp({})
        if "frankfurter" in url:
            return FakeResp({"rates": {"EUR": 0.9}})
        raise requests.ConnectionError("no route " + url)


class TokenPricingTests(unittest.TestCase):
    def setUp(self):
        pricing.clear_cache()
        net.reset_stats()

    def price(self, w, fake):
        with mock.patch("requests.request", fake):
            return pricing.apply_pricing([w])

    def test_defillama_prices_everything_in_few_requests_and_adds_eur(self):
        fake, w = Net(), wallet(100)
        self.price(w, fake)
        self.assertEqual([len(c) for c in fake.llama_calls], [40, 40, 20])
        self.assertEqual(fake.gecko_calls, [])
        self.assertTrue(all(t.usd_value == 2.0 and abs(t.eur_value - 1.8) < 1e-9 for t in w.tokens))
        self.assertEqual(pricing.LAST_NOTES, [])

    def test_ids_use_the_chain_prefix(self):
        fake = Net()
        self.price(wallet(2), fake)
        self.assertEqual(fake.llama_calls[0], [f"ethereum:{addr(0)}", f"ethereum:{addr(1)}"])

    def test_unnamed_tokens_are_never_sent(self):
        fake = Net()
        self.price(wallet(2, unnamed=500), fake)
        asked = {k for call in fake.llama_calls for k in call}
        self.assertEqual(asked, {f"ethereum:{addr(0)}", f"ethereum:{addr(1)}"})

    def test_low_confidence_or_missing_prices_go_to_the_fallback_one_by_one(self):
        def llama(i):
            return {"price": 2.0, "confidence": 0.5} if i == 0 else (None if i == 1 else {"price": 3.0, "confidence": 0.99})
        fake = Net(llama, gecko=lambda c: FakeResp({c: {"usd": 7.0, "eur": 6.0}}))
        w = wallet(3)
        self.price(w, fake)
        self.assertEqual(sorted(sum(fake.gecko_calls, [])), sorted([addr(0), addr(1)]))
        self.assertTrue(all(len(c) == 1 for c in fake.gecko_calls))
        self.assertEqual([t.usd_value for t in w.tokens], [7.0, 7.0, 3.0])

    def test_fallback_is_capped_and_silent_when_the_main_source_is_up(self):
        fake = Net(llama=lambda i: None)               # DefiLlama knows none of them
        self.price(wallet(60), fake)
        self.assertEqual(len(fake.gecko_calls), pricing.FALLBACK_CONTRACTS)
        self.assertEqual(pricing.LAST_NOTES, [])

    def test_defillama_down_falls_back_and_says_so(self):
        fake = Net(llama_down=True, gecko=lambda c: FakeResp({c: {"usd": 2.0, "eur": 1.8}}))
        w = wallet(30)
        self.price(w, fake)
        self.assertEqual(len(fake.gecko_calls), pricing.FALLBACK_CONTRACTS)
        self.assertTrue(any("25" in n and "non valorisé" in n for n in pricing.LAST_NOTES), pricing.LAST_NOTES)

    def test_hostile_defillama_answers_do_not_crash_or_price(self):
        for raw in ([], "x", {"coins": None}, {"coins": []}, {"coins": {"ethereum:" + addr(0): "x"}},
                    {"coins": {"ethereum:" + addr(0): {"price": "x", "confidence": 0.99}}},
                    {"coins": {"ethereum:" + addr(0): {"price": float("nan"), "confidence": 0.99}}},
                    {"coins": {"ethereum:" + addr(0): {"price": -3, "confidence": 0.99}}},
                    {"coins": {"ethereum:" + addr(0): {"price": 2.0, "confidence": "high"}}},
                    {"coins": {"ethereum:" + addr(0): {"price": 2.0, "confidence": True}}},
                    {"coins": {"ethereum:" + addr(0): {"price": 2.0}}},
                    {"coins": {7: {"price": 2.0, "confidence": 0.99}}}):
            pricing.clear_cache()
            w = wallet(1)
            self.price(w, Net(llama_raw=raw))
            self.assertIsNone(w.tokens[0].usd_value, raw)

    def test_number_examined_is_bounded(self):
        fake = Net()
        self.price(wallet(500), fake)
        self.assertEqual(sum(len(c) for c in fake.llama_calls), pricing.MAX_CONTRACTS_PER_CHAIN)
        self.assertTrue(any("300" in n and "ethereum" in n for n in pricing.LAST_NOTES), pricing.LAST_NOTES)

    def test_coingecko_pass_stops_after_repeated_refusals(self):
        def refuse(c):
            raise requests.ConnectionError("429")
        fake = Net(llama=lambda i: None, gecko=refuse)
        with mock.patch.object(pricing, "FALLBACK_CONTRACTS", 50):
            self.price(wallet(50), fake)
        self.assertEqual(len(fake.gecko_calls), pricing.TOKEN_FAIL_LIMIT)

    def test_time_budget_stops_new_requests(self):
        fake = Net()
        with mock.patch.object(pricing, "TOKEN_PRICE_BUDGET", -1.0):
            self.price(wallet(5), fake)
        self.assertEqual((fake.llama_calls, fake.gecko_calls), ([], []))

    def test_notes_do_not_leak_into_the_next_run(self):
        self.price(wallet(500), Net())
        self.assertTrue(pricing.LAST_NOTES)
        pricing.clear_cache()
        self.price(wallet(2), Net())
        self.assertEqual(pricing.LAST_NOTES, [])

    def test_second_refresh_is_served_from_the_cache(self):
        fake = Net()
        self.price(wallet(5), fake)
        n = len(fake.llama_calls)
        self.price(wallet(5), fake)
        self.assertEqual(len(fake.llama_calls), n)

    def test_coingecko_one_contract_rule_is_documented_in_the_fake(self):
        self.assertEqual(pricing.COINGECKO_BATCH, 1)
        r = Net()("GET", "https://api.coingecko.com/api/v3/simple/token_price/ethereum",
                  params={"contract_addresses": "a,b"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
