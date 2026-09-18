"""The demo signer: the platform's caps before anything is signed, keys only from owner-only
files, and the gateway's flow with it. Offline.

    spike/.venv/bin/python -m unittest discover -s gateway/tests -t .
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

from eth_account import Account

from gateway import demo_signer, hl
from gateway.checks import GatewayError
from gateway.demo_signer import COINS, MAX_NOTIONAL, MAX_SIZE, DemoSigner, check_caps, market_mid
from gateway.server import Gateway, signer_from_env
from gateway.signer import SignerClient
from gateway.tests.test_gateway import NOW, Base

BTC, ETH, SOL = 3, 4, 0


def order(asset, size, px, tif="Gtc", **over):
    o = {"a": asset, "b": True, "p": px, "s": size, "r": False, "t": {"limit": {"tif": tif}}}
    o.update(over)
    return {"type": "order", "orders": [o], "grouping": "na"}


def sell(asset, size, px, **over):
    return order(asset, size, px, **{"b": False, **over})


def cancel(asset, oid=7):
    return {"type": "cancel", "cancels": [{"a": asset, "o": oid}]}


class Market:
    """Mids by asset, and which assets were asked for."""

    def __init__(self, **mids):
        self.mids = {{"BTC": BTC, "ETH": ETH, "SOL": SOL}[k]: Decimal(v) for k, v in mids.items()}
        self.asked = []

    def __call__(self, asset):
        self.asked.append(asset)
        return self.mids[asset]


def unreadable(asset):
    raise AssertionError("the market was read")


class Caps(unittest.TestCase):
    def refused(self, code, kind, action, mid=unreadable):
        with self.assertRaises(GatewayError) as ctx:
            check_caps(kind, action, mid)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (403, code), action)

    def test_the_caps_are_the_platforms(self):
        self.assertEqual({k: str(v) for k, v in MAX_SIZE.items()}, {BTC: "0.005", ETH: "0.15", SOL: "4"})
        self.assertEqual(str(MAX_NOTIONAL), "400")
        self.assertEqual(COINS, {BTC: "BTC", ETH: "ETH", SOL: "SOL"})  # testnet meta, 18 Sep 2026

    def test_each_asset_has_its_size_cap(self):
        for asset, ok, over, px in ((BTC, "0.005", "0.0051", "60000"), (ETH, "0.15", "0.1501", "2000"),
                                    (SOL, "4", "4.01", "90")):
            check_caps("order", order(asset, ok, px), unreadable)
            self.refused("over_cap", "order", order(asset, over, px))
            self.refused("over_cap", "order", sell(asset, over, px))  # before the market is read

    def test_notional_cap(self):
        check_caps("order", order(ETH, "0.15", "2666"), unreadable)  # 399.9
        check_caps("order", order(BTC, "0.005", "80000"), unreadable)  # exactly 400
        self.refused("over_cap", "order", order(ETH, "0.15", "2667"))  # 400.05
        self.refused("over_cap", "order", order(BTC, "0.005", "80000.2"))  # 400.001

    def test_a_sell_under_the_market_counts_at_the_mid(self):
        # 18 Sep 2026: SOL's mid 105.805, and 4 SOL offered at 90 fills at the market.
        self.refused("over_cap", "order", sell(SOL, "4", "90"), Market(SOL="105.805"))  # 423.22
        market = Market(BTC="80000")
        check_caps("order", sell(BTC, "0.005", "60000"), market)  # exactly 400 at the mid
        self.assertEqual(market.asked, [BTC])
        self.refused("over_cap", "order", sell(BTC, "0.005", "60000"), Market(BTC="80000.2"))  # 400.001
        self.refused("over_cap", "order", sell(ETH, "0.15", "2000", tif="Ioc"), Market(ETH="2667"))  # 400.05

    def test_a_sell_over_the_market_counts_at_its_limit(self):
        check_caps("order", sell(BTC, "0.005", "80000"), Market(BTC="70000"))  # exactly 400
        self.refused("over_cap", "order", sell(BTC, "0.005", "80000.2"), Market(BTC="70000"))  # 400.001

    def test_a_buy_counts_at_its_limit_and_never_reads_the_market(self):
        check_caps("order", order(BTC, "0.005", "79000"), unreadable)  # 395, whatever the market
        check_caps("order", order(BTC, "0.005", "80000", tif="Ioc"), unreadable)
        self.refused("over_cap", "order", order(BTC, "0.005", "80000.2", tif="Ioc"))
        check_caps("cancel", cancel(SOL), unreadable)

    def test_only_a_literal_true_is_a_buy(self):
        for side in (1, "true", None):
            self.refused("over_cap", "order", order(BTC, "0.005", "60000", b=side), Market(BTC="80000.2"))

    def test_the_mid_is_hyperliquids(self):
        with mock.patch.object(hl, "mids", return_value={"SOL": "105.805", "@1035": "12.1"}):
            self.assertEqual(market_mid(SOL), Decimal("105.805"))
        for answer in ({}, {"SOL": "0"}, {"SOL": "-1"}, {"SOL": "NaN"}, {"SOL": "abc"}, {"SOL": 105.8}, ["SOL"]):
            with mock.patch.object(hl, "mids", return_value=answer), self.assertRaises(GatewayError) as ctx:
                market_mid(SOL)
            self.assertEqual((ctx.exception.status, ctx.exception.code), (502, "no_market_price"), answer)
        self.assertIs(DemoSigner([])._mid, demo_signer.market_mid)

    def test_mids_come_from_the_testnet_info_api(self):
        calls = []

        class Answer:
            def raise_for_status(self):
                return None

            def json(self):
                return {"BTC": "77633.5"}

        def post(url, json=None, timeout=None, headers=None):
            calls.append((url, json, headers["User-Agent"]))
            return Answer()

        with mock.patch.object(hl.requests, "post", post):
            self.assertEqual(hl.mids(), {"BTC": "77633.5"})
        self.assertEqual(calls, [("https://api.hyperliquid-testnet.xyz/info", {"type": "allMids"},
                                  "colosseum-pools-gateway")])

    def test_only_listed_assets(self):
        self.refused("policy", "order", order(5, "0.001", "10"))
        self.refused("policy", "order", sell(5, "0.001", "10"))
        self.refused("policy", "cancel", cancel(5))
        check_caps("cancel", cancel(BTC), unreadable)

    def test_one_limit_order_or_one_cancel_only(self):
        self.refused("policy", "order", order(BTC, "0.001", "60000", t={"trigger": {"isMarket": True}}))
        self.refused("policy", "order", order(BTC, "0.001", "60000", tif="FrontendMarket"))
        self.refused("policy", "order", order(BTC, "0.001", "60000",
                                              t={"limit": {"tif": "Gtc"}, "trigger": {"isMarket": False}}))
        self.refused("policy", "order", {**order(BTC, "0.001", "60000"), "grouping": "normalTpsl"})
        two = order(BTC, "0.001", "60000")
        two["orders"] = two["orders"] * 2
        self.refused("policy", "order", two)
        both = cancel(BTC)
        both["cancels"] = both["cancels"] * 2
        self.refused("policy", "cancel", both)
        self.refused("policy", "cancel", order(BTC, "0.001", "60000"))
        self.refused("policy", "order", cancel(BTC))
        for bad in ("0", "-1", "abc", "NaN", "Infinity", 1):
            self.refused("policy", "order", order(BTC, bad, "60000"))
        self.refused("policy", "order", "not a dict")


class Signing(unittest.TestCase):
    def test_the_signature_is_the_keys(self):
        key = Account.create(os.urandom(32))
        signer = DemoSigner([key])
        action = order(BTC, "0.001", "60000")
        result = signer.sign(key.address.upper().replace("0X", "0x"), "order", action, NOW)
        self.assertEqual((result.http_status, result.receipt), (200, None))
        self.assertEqual(hl.recover_signer(action, result.signature, NOW), key.address)
        self.assertTrue(signer.has_key(key.address.lower()))
        self.assertFalse(signer.has_key(Account.create(os.urandom(32)).address))
        self.assertEqual(len(signer), 1)

    def test_nothing_over_the_caps_is_signed(self):
        key = Account.create(os.urandom(32))
        with self.assertRaises(GatewayError):
            DemoSigner([key]).sign(key.address, "order", order(BTC, "0.006", "60000"), NOW)


class KeyFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name) / "keys"
        self.dir.mkdir()
        os.chmod(self.dir, 0o700)
        self.key = Account.create(os.urandom(32))

    def write(self, name, text, mode=0o600):
        path = self.dir / name
        path.write_text(text)
        os.chmod(path, mode)
        return path

    def test_keys_come_from_owner_only_files_and_the_address_from_the_key(self):
        self.write("demo-agent-01.key", "0x" + bytes(self.key.key).hex() + "\n")
        self.write("addresses.txt", "0x" + "00" * 20 + "\n", 0o644)  # not a key file; ignored
        keys = DemoSigner.load_keys(str(self.dir))
        self.assertEqual([k.address for k in keys], [self.key.address])

    def test_loose_permissions_or_no_keys_refuse_to_start(self):
        with self.assertRaises(SystemExit):
            DemoSigner.load_keys(str(self.dir))  # no keys
        loose = self.write("demo-agent-01.key", "0x" + bytes(self.key.key).hex(), 0o640)
        with self.assertRaises(SystemExit):
            DemoSigner.load_keys(str(self.dir))
        os.chmod(loose, 0o600)
        os.chmod(self.dir, 0o750)
        with self.assertRaises(SystemExit):
            DemoSigner.load_keys(str(self.dir))
        os.chmod(self.dir, 0o700)
        self.assertEqual(len(DemoSigner.load_keys(str(self.dir))), 1)

    def test_the_mode_is_chosen_explicitly(self):
        self.write("demo-agent-01.key", "0x" + bytes(self.key.key).hex())
        mode, signer = signer_from_env({"GATEWAY_SIGNER": "demo", "GATEWAY_KEYS_DIR": str(self.dir)})
        self.assertEqual((mode, type(signer), len(signer)), ("demo", DemoSigner, 1))
        tokens = self.write("tokens.json", json.dumps({self.key.address: "t"}))
        mode, signer = signer_from_env({"GATEWAY_SIGNER": "signer", "SIGNER_URL": "https://signer.invalid",
                                        "SIGNER_TOKENS_FILE": str(tokens)})
        self.assertEqual((mode, type(signer)), ("signer", SignerClient))
        for env in ({}, {"GATEWAY_SIGNER": ""}, {"GATEWAY_SIGNER": "enclave"}):
            with self.assertRaises(SystemExit, msg=env):
                signer_from_env(env)


class DemoFlow(Base):
    def gateway(self, mid=unreadable):
        self.submitted = []

        def submit(action, nonce, signature):
            self.submitted.append((action, nonce, signature))
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        return Gateway(self.reader, DemoSigner([self.enclave_key], mid), submit=submit, clock=lambda: NOW / 1000)

    def test_a_sell_under_the_market_over_the_cap_is_refused_before_signing(self):
        market = Market(BTC="80000.2")
        gw = self.gateway(market)
        body = self.body(isBuy=False, size="0.005", limitPx="60000", tif="Ioc")  # 300 at its limit
        status, out = gw.handle_order(body)
        self.assertEqual((status, out["status"], out["code"]), (403, "refused_by_gateway", "over_cap"))
        self.assertEqual(self.submitted, [])
        market.mids[BTC] = Decimal("79000")  # 395: the same request, nonce given back
        status, out = gw.handle_order(body)
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertEqual(market.asked, [BTC, BTC])

    def test_a_sell_is_not_signed_without_a_mid(self):
        def no_mid(asset):
            raise GatewayError(502, "no_market_price", "BTC")

        def offline(asset):
            raise ConnectionError("info API unreachable")

        for mid, code in ((no_mid, "no_market_price"), (offline, "upstream_failed")):
            gw = self.gateway(mid)
            status, out = gw.handle_order(self.body(isBuy=False, size="0.001", limitPx="60000"))
            self.assertEqual((status, out["code"]), (502, code))
            self.assertEqual(self.submitted, [])
            status, out = gw.handle_order(self.body(size="0.001", limitPx="60000"))  # a buy asks no mid
            self.assertEqual((status, out["status"]), (200, "submitted"))

    def test_over_the_cap_is_refused_before_signing_and_the_nonce_comes_back(self):
        gw = self.gateway()
        status, out = gw.handle_order(self.body(size="0.006"))  # 360 USDC, but over the BTC cap of 0.005
        self.assertEqual((status, out["status"], out["code"]), (403, "refused_by_gateway", "over_cap"))
        self.assertEqual(self.submitted, [])
        status, out = gw.handle_order(self.body(size="0.002"))  # the same nonce, within the cap
        self.assertEqual((status, out["status"]), (200, "submitted"))
        action, nonce, signature = self.submitted[0]
        self.assertEqual(hl.recover_signer(action, signature, nonce), self.enclave_key.address)
        self.assertIsNone(out["receipt"])


if __name__ == "__main__":
    unittest.main()
