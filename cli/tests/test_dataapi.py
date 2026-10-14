"""Optional Ecompass data-api source (offline: requests.request mocked)."""
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))

import pricing
from providers import dataapi, lp, multiversx, net
from providers.base import TokenBalance, WalletBalance
from providers.net import HttpError
from test_security import FakeResp

net.THROTTLE_ENABLED = False
net.RETRY_DELAYS = ()

FIX = Path(__file__).resolve().parent / "fixtures"
ADDR = "erd1" + "a" * 58
ROOT = dataapi.DATA_API_URL
TOKEN = "tok-123_ABC"
NOW = 1791977075 + 60          # a minute after the freshest token of the fixture
POLICY = dataapi.Policy(min_liquidity_usd=1000.0, max_age_seconds=600.0)


def load(name):
    return json.loads((FIX / name).read_text())


def tok(ident="ZZZ-111111", *, decimals=18, price=2.0, source="XEXCHANGE:ZZZ/USDC",
        liquidity=50_000.0, stamp=NOW - 30, lp=False, supply=None):
    return {
        "token": {"id": ident, "decimals": decimals},
        "timestamp": stamp,
        "usd_price": price,
        "usdc_tv_symbol": source,
        "usd_liquidity": liquidity,
        "is_lp_token": lp,
        "supply": supply,
        "custom_name": None,
    }


def entry(token, balance):
    return {"balance": str(balance), "token": token, "usd_value": 0}


def doc(tokens=(), positions=()):
    return {"address": ADDR, "tokens": list(tokens), "defi_positions": list(positions),
            "staking_delegations": [], "all_assets": []}


def by_contract(result):
    return {t.contract: t for t in result.tokens}


def lp_position(lp_token, raw, underlying_usd, protocol="xexchange"):
    return {
        "lp_token": {"balance": str(raw), "token": lp_token},
        "underlying_assets": [{"balance": "1", "token": tok("AAA-000000"), "usd_value": underlying_usd}],
        "protocol": protocol, "is_farming": False, "is_lending": False, "is_borrow": False,
        "is_collateral": False,
    }


class RealSamples(unittest.TestCase):
    def setUp(self):
        self.defi = dataapi.build(load("dataapi_wallet_defi.json"), NOW, POLICY)
        self.staking = dataapi.build(load("dataapi_wallet_staking.json"), NOW, POLICY)

    def test_native_amount(self):
        self.assertAlmostEqual(self.defi.native_amount, 7.810761692246062, places=9)
        self.assertGreater(self.staking.native_amount, 0)

    def test_plain_tokens_priced_and_illiquid_locked_unpriced(self):
        t = by_contract(self.defi)
        self.assertAlmostEqual(t["USDC-c76f1f"].usd_value, 45.8, delta=0.1)   # timestamp 0, exempt
        self.assertAlmostEqual(t["WETH-b4ca29"].usd_value, 992.3, delta=1)
        for ident in ("SPORE-d48768", "XINU-6cc912"):                        # thin / no source
            self.assertIsNone(t[ident].usd_value, ident)
            self.assertTrue(t[ident].price_locked, ident)

    def test_vouched_lps_valued_and_locked(self):
        t = by_contract(self.defi)
        for ident in ("EGLDUSDC-594e5e", "LPETHBTC-071be8", "LPJEX3USD-25e943", "ALP-2975f6"):
            self.assertIsNotNone(t[ident].usd_value, ident)
            self.assertTrue(t[ident].price_locked, ident)
        self.assertAlmostEqual(t["LPETHBTC-071be8"].usd_value, 2138.7, delta=2)
        self.assertIn("data-api", t["LPETHBTC-071be8"].name)

    def test_hatom_and_farms(self):
        kinds = {}
        for line in self.defi.tokens:
            kinds.setdefault(line.asset_type, []).append(line)
        self.assertEqual(len(kinds["lending"]), 1)
        self.assertGreater(kinds["lending"][0].usd_value, 700)
        self.assertEqual(len(kinds["farm"]), 4)
        self.assertGreaterEqual(len(kinds["farm-rewards"]), 1)

    def test_no_duplicate_lines(self):
        keys = [(t.asset_type, t.contract) for t in self.defi.tokens]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_line_is_locked_or_a_unvouched_lp(self):
        for line in self.defi.tokens + self.staking.tokens:
            if not line.price_locked:
                self.assertIsNone(line.usd_value)

    def test_price_locked_is_not_exported(self):
        from dataclasses import asdict
        self.assertNotIn("price_locked", asdict(self.defi.tokens[0]))


class PricePolicy(unittest.TestCase):
    def one(self, token, balance=10**18, positions=(), policy=POLICY):
        res = dataapi.build(doc([entry(token, balance)], positions), NOW, policy)
        return res.tokens[0]

    def test_basic_price(self):
        line = self.one(tok())
        self.assertEqual(line.usd_value, 2.0)
        self.assertTrue(line.price_locked)

    def test_thin_liquidity_unpriced(self):
        self.assertIsNone(self.one(tok(liquidity=999.0)).usd_value)

    def test_min_liquidity_env(self):
        with mock.patch.dict(os.environ, {dataapi.ENV_MIN_LIQUIDITY: "10"}):
            line = self.one(tok(liquidity=999.0), policy=dataapi.Policy.from_env())
        self.assertEqual(line.usd_value, 2.0)

    def test_bad_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {dataapi.ENV_MIN_LIQUIDITY: "abc", dataapi.ENV_MAX_AGE: "-4"}):
            p = dataapi.Policy.from_env()
        self.assertEqual(p.min_liquidity_usd, 1000.0)
        self.assertEqual(p.max_age_seconds, 600.0)

    def test_no_source_unpriced(self):
        self.assertIsNone(self.one(tok(source="")).usd_value)

    def test_stale_and_future_and_missing_timestamp(self):
        self.assertIsNone(self.one(tok(stamp=NOW - 601)).usd_value)
        self.assertIsNone(self.one(tok(stamp=NOW + 10_000)).usd_value)
        self.assertIsNone(self.one(tok(stamp=0)).usd_value)
        self.assertIsNone(self.one(tok(stamp=None)).usd_value)
        self.assertIsNone(self.one(tok(stamp=True)).usd_value)
        self.assertEqual(self.one(tok(stamp=NOW + 100)).usd_value, 2.0)    # small clock skew

    def test_usdc_exempt_from_age(self):
        usdc = tok("USDC-c76f1f", decimals=6, price=1.0, source=dataapi.SELF_QUOTE_SOURCE, stamp=0)
        self.assertEqual(self.one(usdc, balance=5_000_000).usd_value, 5.0)

    def test_garbage_prices(self):
        for bad in (None, 0, -1, "x", float("inf"), float("nan"), 1e30):
            self.assertIsNone(self.one(tok(price=bad)).usd_value, bad)

    def test_zero_and_negative_balances_skipped(self):
        res = dataapi.build(doc([entry(tok(), 0), entry(tok("YYY-222222"), -5), entry(tok("XXX-333333"), "abc")]), NOW, POLICY)
        self.assertEqual(res.tokens, [])

    def test_egld_goes_to_native(self):
        egld = tok("EGLD", source="XEXCHANGE:WEGLD/USDC", liquidity=None)
        res = dataapi.build(doc([entry(egld, 3 * 10**18)]), NOW, POLICY)
        self.assertEqual(res.native_amount, 3.0)
        self.assertEqual(res.tokens, [])


class LpPolicy(unittest.TestCase):
    LP = "AAABBB-123456"

    def build(self, *, balance=10**18, price=10.0, underlying=10.0, supply=10**20, stamp=NOW - 10,
              positioned=True):
        lp = tok(self.LP, price=price, source="XEXCHANGE:AAABBB/USDC", liquidity=0.0, lp=True,
                 supply=str(supply), stamp=stamp)
        pos = [lp_position(lp, balance, underlying)] if positioned else []
        return dataapi.build(doc([entry(lp, balance)], pos), NOW, POLICY).tokens[0]

    def test_backed_lp_valued(self):
        line = self.build()
        self.assertAlmostEqual(line.usd_value, 10.0)
        self.assertTrue(line.price_locked)

    def test_orphan_lp_left_to_local_reader(self):
        line = self.build(positioned=False)
        self.assertIsNone(line.usd_value)
        self.assertFalse(line.price_locked)

    def test_stale_lp_left_to_local_reader(self):
        line = self.build(stamp=NOW - 5000)
        self.assertIsNone(line.usd_value)
        self.assertFalse(line.price_locked)

    def test_holding_above_supply_refused(self):
        line = self.build(balance=10**18, supply=10**17)
        self.assertIsNone(line.usd_value)
        self.assertFalse(line.price_locked)

    def test_inconsistent_underlying_refused(self):
        line = self.build(price=10.0, underlying=1.0)
        self.assertIsNone(line.usd_value)
        self.assertFalse(line.price_locked)

    def test_small_difference_tolerated(self):
        self.assertIsNotNone(self.build(price=10.0, underlying=10.3).usd_value)

    def test_huge_unit_price_allowed_for_lp_only(self):
        line = self.build(balance=4_152_000, price=3.26e11, underlying=1.354, supply=10**25)
        self.assertAlmostEqual(line.usd_value, 1.3536, places=3)
        plain = dataapi.build(doc([entry(tok(price=3e12), 10**18)]), NOW, POLICY).tokens[0]
        self.assertIsNone(plain.usd_value)


class HatomAndFarms(unittest.TestCase):
    def hatom(self, borrow, price=1.0, stamp=NOW - 5):
        under = tok("USDC-c76f1f", decimals=6, price=price, source=dataapi.SELF_QUOTE_SOURCE, stamp=stamp)
        pos = {
            "lp_token": {"balance": "100", "token": tok("HUSDC-d80042", decimals=8, price=0.0)},
            "underlying_assets": [{"balance": "2500000", "token": under, "usd_value": 2.5}],
            "protocol": "hatom", "is_farming": False,
            "is_lending": not borrow, "is_borrow": borrow, "is_collateral": False,
        }
        return dataapi.build(doc([], [pos]), NOW, POLICY).tokens

    def test_deposit_positive(self):
        (line,) = self.hatom(False)
        self.assertEqual(line.asset_type, "lending")
        self.assertAlmostEqual(line.usd_value, 2.5)
        self.assertEqual(line.contract, "HUSDC-d80042")

    def test_borrow_negative(self):
        (line,) = self.hatom(True)
        self.assertEqual(line.asset_type, "borrow")
        self.assertAlmostEqual(line.usd_value, -2.5)

    def test_hatom_without_usable_price(self):
        (line,) = self.hatom(False, price=None)
        self.assertIsNone(line.usd_value)
        self.assertTrue(line.price_locked)

    def test_farm_with_rewards(self):
        farmed = tok("FARM-aaaaaa", price=3.0, source="XEXCHANGE:FARM/USDC")
        reward = tok("RWD-bbbbbb", price=0.5, source="XEXCHANGE:RWD/USDC")
        pos = {
            "lp_token": {"balance": str(2 * 10**18), "token": farmed},
            "underlying_assets": [], "protocol": "xexchange", "is_farming": True,
            "farming_type": "metastaking",
            "farming_meta_esdt": {"id": "METAFARM-cccccc-01", "balance": "1"},
            "pending_rewards": [{"balance": str(4 * 10**18), "token": reward}],
        }
        farm, rew = dataapi.build(doc([], [pos]), NOW, POLICY).tokens
        self.assertEqual((farm.asset_type, farm.contract), ("farm", "METAFARM-cccccc-01"))
        self.assertAlmostEqual(farm.usd_value, 6.0)
        self.assertEqual(rew.asset_type, "farm-rewards")
        self.assertAlmostEqual(rew.usd_value, 2.0)


class Hostile(unittest.TestCase):
    def test_text_is_sanitised(self):
        evil = tok("EV‮IL-123456")
        evil["custom_name"] = "<script>\x1b[31m" + "N" * 5000
        (line,) = dataapi.build(doc([entry(evil, 10**18)]), NOW, POLICY).tokens
        for text in (line.symbol, line.name, line.contract):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("‮", text)
        self.assertLessEqual(len(line.name), 300)

    def test_malformed_documents(self):
        for bad in (None, [], "x", 3, {}, {"tokens": "x"}, {"tokens": None}):
            with self.assertRaises(HttpError, msg=repr(bad)):
                dataapi.build(bad, NOW, POLICY)

    def test_junk_entries_ignored(self):
        junk = doc([None, 3, "x", {"token": "x"}, {"token": {"token": {"id": 5}}}, entry(tok(), 10**18)],
                   [None, {"lp_token": 3}, {"is_farming": True}, {"is_lending": True, "underlying_assets": 3}])
        res = dataapi.build(junk, NOW, POLICY)
        self.assertEqual([t.contract for t in res.tokens], ["ZZZ-111111"])


class Fetch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {dataapi.ENV_TOKEN: TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(dataapi, "_sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_url_and_header(self):
        with mock.patch("requests.request", return_value=FakeResp(doc())) as req:
            res = dataapi.fetch(ADDR, NOW)
        self.assertEqual(res.tokens, [])
        args, kwargs = req.call_args
        self.assertEqual(args[1], f"https://data-api.mvx.fr/portfolios/{ADDR}")
        self.assertEqual(kwargs["headers"], {"X-Api-Key": TOKEN})

    def test_address_is_quoted(self):
        with mock.patch("requests.request", return_value=FakeResp(doc())) as req:
            dataapi.fetch("a/../b?x", NOW)
        self.assertEqual(req.call_args.args[1], f"{ROOT}/portfolios/a%2F..%2Fb%3Fx")

    def test_null_then_document(self):
        with mock.patch("requests.request", side_effect=[FakeResp(None), FakeResp(doc())]) as req:
            dataapi.fetch(ADDR, NOW)
        self.assertEqual(req.call_count, 2)
        self.sleep.assert_called_once_with(1.0)

    def test_null_twice_is_an_error(self):
        with mock.patch("requests.request", side_effect=[FakeResp(None), FakeResp(None)]):
            with self.assertRaises(HttpError):
                dataapi.fetch(ADDR, NOW)

    def test_server_error_and_wrong_token(self):
        for status in (500, 401, 403):
            with mock.patch("requests.request", return_value=FakeResp(status=status)):
                with self.assertRaises(HttpError):
                    dataapi.fetch(ADDR, NOW)

    def test_no_token_no_request(self):
        for value in ("", "   ", "abc\r\nX-Evil: 1", "a b", "x" * 400, "é"):
            with mock.patch.dict(os.environ, {dataapi.ENV_TOKEN: value}):
                self.assertFalse(dataapi.enabled(), repr(value))
                with mock.patch("requests.request") as req:
                    with self.assertRaises(HttpError):
                        dataapi.fetch(ADDR, NOW)
                req.assert_not_called()

    def test_config_error_only_for_an_unusable_token(self):
        with mock.patch.dict(os.environ, {dataapi.ENV_TOKEN: "a b"}):
            self.assertIn("token", dataapi.config_error())
        with mock.patch.dict(os.environ, {dataapi.ENV_TOKEN: ""}):
            self.assertIsNone(dataapi.config_error())
        self.assertIsNone(dataapi.config_error())

    def test_token_never_follows_a_redirect_to_another_host(self):
        seen = []

        def fake(method, url, **kw):
            seen.append((url, kw.get("headers")))
            if "data-api" in url:
                return FakeResp(status=302, url=url, headers={"Location": "https://other.example.com/x"})
            return FakeResp(doc(), url=url)

        with mock.patch("requests.request", fake):
            dataapi.fetch(ADDR, NOW)
        self.assertEqual(seen[0][1], {"X-Api-Key": TOKEN})
        self.assertIsNone(seen[1][1])


class StatsLabel(unittest.TestCase):
    def test_host_shown_as_data_api_ecompass(self):
        net.reset_stats()
        net._count("data-api.mvx.fr", "calls", 3)
        net._count("api.multiversx.com", "calls", 1)
        text = net.stats_summary()
        self.assertIn("data-api-ecompass 3 appel(s)", text)
        self.assertNotIn("mvx.fr", text)
        self.assertIn("api.multiversx.com 1 appel(s)", text)
        net.reset_stats()


class KnownIds(unittest.TestCase):
    def test_known_ids_of_the_sample(self):
        res = dataapi.build(load("dataapi_wallet_defi.json"), NOW, POLICY)
        for ident in ("USDC-c76f1f", "SPORE-d48768", "XINU-6cc912", "EGLD", "EGLDUSDC-594e5e"):
            self.assertIn(ident, res.known_ids)

    def test_base_id_drops_the_nonce(self):
        self.assertEqual(dataapi.base_id("EGLDUSDCFL-1a2b3c-0f"), "EGLDUSDCFL-1a2b3c")
        self.assertEqual(dataapi.base_id("EGLD"), "EGLD")

    def test_underlying_and_rewards_do_not_count_as_held(self):
        pos = lp_position(tok("LPX-111111", lp=True), 10**18, 1.0)
        pos["underlying_assets"][0]["token"] = tok("ONLYUNDER-999999")
        res = dataapi.build(doc([], [pos]), NOW, POLICY)
        self.assertNotIn("ONLYUNDER-999999", res.known_ids)
        self.assertIn("LPX-111111", res.known_ids)

    def test_farm_known_by_its_meta_token(self):
        farmed = tok("FARM-aaaaaa", price=3.0)
        pos = {
            "lp_token": {"balance": str(10**18), "token": farmed}, "underlying_assets": [],
            "protocol": "xexchange", "is_farming": True,
            "farming_meta_esdt": {"id": "METAFARM-cccccc-01", "balance": "1"}, "pending_rewards": [],
        }
        res = dataapi.build(doc([], [pos]), NOW, POLICY)
        self.assertIn("METAFARM-cccccc", res.known_ids)
        self.assertNotIn("FARM-aaaaaa", res.known_ids)    # staked in the farm, not held


class Provider(unittest.TestCase):
    PUBLIC_TOKENS = [
        {"identifier": "PUB-111111", "ticker": "PUB", "name": "Pub", "balance": str(10**18),
         "decimals": 18, "type": "FungibleESDT"},
        {"identifier": "USDC-c76f1f", "ticker": "USDC", "name": "USDC", "balance": "45828692",
         "decimals": 6, "type": "FungibleESDT"},
        {"identifier": "SPORE-d48768", "ticker": "SPORE", "name": "Spore", "balance": str(10**18),
         "decimals": 18, "type": "FungibleESDT"},
        {"identifier": "EGLDUSDCFL-45109b-01d64a", "ticker": "EGLDUSDCFL", "name": "Farm", "balance": "45813163",
         "decimals": 18, "type": "MetaESDT"},
        {"identifier": "HUSDC-d80042", "ticker": "HUSDC", "name": "Hatom USDC", "balance": "100",
         "decimals": 8, "type": "FungibleESDT"},
    ]

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {dataapi.ENV_TOKEN: TOKEN}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(dataapi, "_sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.calls = []
        multiversx.set_empty_cache_path(None)

    def gateway_doc(self, tokens):
        """What /address/<addr>/esdt answers for these public token entries."""
        return {"data": {"esdts": {
            t["identifier"]: {"tokenIdentifier": t["identifier"], "balance": t["balance"]} for t in tokens
        }}}

    def fake(self, answer, tokens=None, tokens_error=False, gateway=None):
        tokens = self.PUBLIC_TOKENS if tokens is None else tokens
        gateway = self.gateway_doc(tokens) if gateway is None else gateway

        def request(method, url, **kw):
            self.calls.append(url)
            if "/esdt" in url and url.startswith(lp.gateway_url()):
                if isinstance(gateway, Exception):
                    raise gateway
                return FakeResp(gateway)
            if url.startswith(ROOT):
                if isinstance(answer, Exception):
                    raise answer
                return FakeResp(answer)
            path = url.split("api.multiversx.com", 1)[-1]
            if path.endswith("/tokens"):
                if tokens_error:
                    return FakeResp(status=500)
                return FakeResp(tokens if kw.get("params", {}).get("from", 0) == 0 else [])
            if path.startswith("/accounts/") and path.count("/") == 2:
                return FakeResp({"balance": str(10**18)})
            return FakeResp([])
        return request

    def balance(self, answer, **kw):
        with mock.patch("requests.request", side_effect=self.fake(answer, **kw)):
            return multiversx.MultiversXProvider().get_balance(ADDR)

    def contracts(self, w):
        return [t.contract for t in w.tokens if t.asset_type in ("esdt", "meta-esdt")]

    def public_calls(self):
        return [u for u in self.calls if "api.multiversx.com" in u]

    def test_account_call_replaced_and_public_list_read_when_a_token_is_unknown(self):
        w = self.balance(load("dataapi_wallet_defi.json"))      # PUB-111111 is not in the sample
        self.assertIsNone(w.error)
        self.assertAlmostEqual(w.native_amount, 7.810761692246062, places=9)
        public = self.public_calls()
        self.assertFalse([u for u in public if u.endswith(f"/accounts/{ADDR}")], public)
        self.assertTrue(any(u.endswith("/tokens") for u in public))
        self.assertTrue(any("/delegation" in u for u in public))
        self.assertFalse(w.warning)

    def test_unknown_token_is_added_unlocked_and_known_ones_are_not_duplicated(self):
        w = self.balance(load("dataapi_wallet_defi.json"))
        added = [t for t in w.tokens if t.contract == "PUB-111111"]
        self.assertEqual(len(added), 1)
        self.assertFalse(added[0].price_locked)          # the public pricing will look at it
        for ident in ("USDC-c76f1f", "SPORE-d48768"):
            self.assertEqual([t.contract for t in w.tokens].count(ident), 1, ident)
        spore = next(t for t in w.tokens if t.contract == "SPORE-d48768")
        self.assertTrue(spore.price_locked)              # known + illiquid: stays unpriced
        listed = self.contracts(w)
        self.assertNotIn("EGLDUSDCFL-45109b-01d64a", listed)   # the data-api's farm position
        self.assertNotIn("HUSDC-d80042", listed)               # the data-api's Hatom position

    def test_everything_known_means_no_public_token_list(self):
        known = self.PUBLIC_TOKENS[1:]          # USDC, SPORE, farm meta, Hatom receipt: all known
        w = self.balance(load("dataapi_wallet_defi.json"), tokens=known)
        self.assertIsNone(w.error)
        public = self.public_calls()
        self.assertFalse([u for u in public if u.endswith("/tokens")], public)
        self.assertFalse([u for u in public if u.endswith(f"/accounts/{ADDR}")])
        keys = [(t.asset_type, t.contract) for t in w.tokens]
        self.assertEqual(len(keys), len(set(keys)))

    def test_nfts_and_meta_tokens_do_not_trigger_the_public_list(self):
        gateway = self.gateway_doc(self.PUBLIC_TOKENS[1:3])
        gateway["data"]["esdts"]["COLL-abcdef-01"] = {"tokenIdentifier": "COLL-abcdef-01", "balance": "1"}
        gateway["data"]["esdts"]["OTHERMETA-123456-0a"] = {"tokenIdentifier": "OTHERMETA-123456-0a", "balance": "5"}
        self.balance(load("dataapi_wallet_defi.json"), gateway=gateway)
        self.assertFalse([u for u in self.public_calls() if u.endswith("/tokens")])

    def test_zero_or_junk_gateway_entries_ignored(self):
        gateway = self.gateway_doc(self.PUBLIC_TOKENS[1:3])
        gateway["data"]["esdts"]["ZERO-111111"] = {"tokenIdentifier": "ZERO-111111", "balance": "0"}
        gateway["data"]["esdts"]["JUNK"] = {"tokenIdentifier": 5, "balance": "x"}
        self.balance(load("dataapi_wallet_defi.json"), gateway=gateway)
        self.assertFalse([u for u in self.public_calls() if u.endswith("/tokens")])

    def test_gateway_unavailable_falls_back_to_the_public_list(self):
        w = self.balance(load("dataapi_wallet_defi.json"), gateway=HttpError("down"))
        self.assertTrue(any(u.endswith("/tokens") for u in self.public_calls()))
        self.assertIn("PUB-111111", self.contracts(w))

    def test_gateway_garbage_falls_back_to_the_public_list(self):
        self.balance(load("dataapi_wallet_defi.json"), gateway={"data": "x"})
        self.assertTrue(any(u.endswith("/tokens") for u in self.public_calls()))

    def test_private_gateway_is_used_for_the_check(self):
        with mock.patch.dict(os.environ, {"MULTIVERSX_GATEWAY_URL": "https://gw.example.org"}):
            self.balance(load("dataapi_wallet_defi.json"))
        self.assertTrue(any(u.startswith("https://gw.example.org/address/") for u in self.calls))
        self.assertFalse(any(u.startswith("https://gateway.multiversx.com") for u in self.calls))

    def test_public_list_failure_keeps_data_api_lines_and_warns(self):
        w = self.balance(load("dataapi_wallet_defi.json"), tokens_error=True)
        self.assertIsNone(w.error)
        self.assertTrue(any(t.asset_type == "farm" for t in w.tokens))
        self.assertIn("unknown to the data-api", w.warning)

    def test_no_token_means_no_request_to_the_data_api(self):
        with mock.patch.dict(os.environ, {dataapi.ENV_TOKEN: ""}):
            w = self.balance(load("dataapi_wallet_defi.json"))
        self.assertFalse([u for u in self.calls if u.startswith(ROOT)])
        self.assertAlmostEqual(w.native_amount, 1.0)
        self.assertIn("PUB-111111", self.contracts(w))

    def test_fallback_when_data_api_is_down(self):
        w = self.balance(HttpError("boom"))
        self.assertIsNone(w.error)
        self.assertAlmostEqual(w.native_amount, 1.0)
        self.assertIn("PUB-111111", self.contracts(w))
        self.assertIn("data-api unavailable", w.warning)

    def test_fallback_on_nonsense(self):
        w = self.balance({"unexpected": True})
        self.assertAlmostEqual(w.native_amount, 1.0)
        self.assertIn("data-api unavailable", w.warning)

    def test_invalid_token_ignored_with_warning(self):
        with mock.patch.dict(os.environ, {dataapi.ENV_TOKEN: "bad token"}):
            w = self.balance({})
        self.assertAlmostEqual(w.native_amount, 1.0)
        self.assertIn("token data-api", w.warning)
        self.assertFalse([u for u in self.calls if u.startswith(ROOT)])

    def test_secret_never_in_warning(self):
        w = self.balance(HttpError(f"fail {TOKEN}"))
        self.assertNotIn(TOKEN, w.warning or "")


class PricingIntegration(unittest.TestCase):
    def wallet(self, lines):
        return WalletBalance(chain="multiversx", address=ADDR, native_symbol="EGLD", native_amount=0.0, tokens=lines)

    def run_pricing(self, wallets, mex=None):
        with mock.patch.object(pricing, "_fetch_native_prices", return_value={"multiversx": {"usd": 20.0, "eur": 18.0}}), \
             mock.patch.object(pricing, "_fetch_usd_eur_rate", return_value=0.9), \
             mock.patch.object(pricing, "_fetch_mex_tokens_prices", return_value=mex or {}) as mex_call, \
             mock.patch.object(pricing, "_fetch_token_prices_by_identifier", return_value={}) as by_id:
            pricing.clear_cache()
            pricing.apply_pricing(wallets)
        return mex_call, by_id

    def test_locked_lines_get_eur_and_are_not_repriced(self):
        priced = TokenBalance("A", "A", 1.0, contract="A-111111", asset_type="esdt", usd_value=10.0)
        priced.price_locked = True
        unpriced = TokenBalance("B", "B", 1.0, contract="B-222222", asset_type="esdt")
        unpriced.price_locked = True
        w = self.wallet([priced, unpriced])
        mex_call, by_id = self.run_pricing([w], mex={"A-111111": 99.0, "B-222222": 99.0})
        mex_call.assert_not_called()
        by_id.assert_not_called()
        self.assertEqual(priced.usd_value, 10.0)
        self.assertAlmostEqual(priced.eur_value, 9.0)
        self.assertIsNone(unpriced.usd_value)
        self.assertAlmostEqual(w.total_usd, 10.0)

    def test_unlocked_tokens_still_use_public_prices(self):
        line = TokenBalance("C", "C", 2.0, contract="C-333333", asset_type="esdt")
        self.run_pricing([self.wallet([line])], mex={"C-333333": 3.0})
        self.assertAlmostEqual(line.usd_value, 6.0)

    def test_unlocked_lp_goes_to_local_reader(self):
        line = TokenBalance("ALP", "Some LP", 2.0, contract="ALP-2975f6", asset_type="esdt")
        with mock.patch.object(pricing.lp_module, "price_lp_tokens", return_value={}) as reader, \
             mock.patch.object(pricing, "lp_pricing_enabled", return_value=True):
            self.run_pricing([self.wallet([line])])
        reader.assert_called_once()
        self.assertEqual(reader.call_args.args[0], ["ALP-2975f6"])

    def test_locked_lp_not_sent_to_local_reader(self):
        line = TokenBalance("ALP", "Some LP", 2.0, contract="ALP-2975f6", asset_type="esdt", usd_value=5.0)
        line.price_locked = True
        with mock.patch.object(pricing.lp_module, "price_lp_tokens", return_value={}) as reader, \
             mock.patch.object(pricing, "lp_pricing_enabled", return_value=True):
            self.run_pricing([self.wallet([line])])
        reader.assert_not_called()

    def test_position_cap_applies_both_signs(self):
        for value in (pricing.MAX_POSITION_USD * 2, -pricing.MAX_POSITION_USD * 2):
            line = TokenBalance("X", "X", 1.0, contract="X-111111", asset_type="esdt", usd_value=value)
            line.price_locked = True
            self.run_pricing([self.wallet([line])])
            self.assertIsNone(line.usd_value)

    def test_borrow_reduces_total(self):
        dep = TokenBalance("U", "dep", 1.0, contract="HU-111111", asset_type="lending", usd_value=100.0)
        bor = TokenBalance("U", "bor", 1.0, contract="HU-222222", asset_type="borrow", usd_value=-40.0)
        dep.price_locked = bor.price_locked = True
        w = self.wallet([dep, bor])
        self.run_pricing([w])
        self.assertAlmostEqual(w.total_usd, 60.0)
        self.assertAlmostEqual(bor.eur_value, -36.0)

    def test_full_sample_end_to_end(self):
        res = dataapi.build(load("dataapi_wallet_defi.json"), NOW, POLICY)
        w = self.wallet(res.tokens)
        w.native_amount = res.native_amount
        with mock.patch.object(pricing, "lp_pricing_enabled", return_value=False):
            self.run_pricing([w])
        expected = 20.0 * res.native_amount + sum(t.usd_value for t in res.tokens if t.usd_value is not None)
        self.assertAlmostEqual(w.total_usd, expected, places=6)
        self.assertGreater(w.total_usd, 4000)


class DustFilter(unittest.TestCase):
    def test_negative_lines_survive_dust_filter(self):
        import main as cli_main
        bor = TokenBalance("U", "bor", 1.0, contract="HU-1", asset_type="borrow", usd_value=-40.0)
        dust = TokenBalance("D", "dust", 1.0, contract="D-1", asset_type="esdt", usd_value=0.001)
        w = WalletBalance(chain="multiversx", address=ADDR, native_symbol="EGLD", tokens=[bor, dust])
        cli_main.filter_dust([w], 0.01)
        self.assertIn(bor, w.tokens)
        self.assertNotIn(dust, w.tokens)


if __name__ == "__main__":
    unittest.main()
