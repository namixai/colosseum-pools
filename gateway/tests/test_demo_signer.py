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
from gateway.demo_signer import MAX_NOTIONAL, DemoSigner, check_caps, market_mid, size_caps
from gateway.server import Gateway, signer_from_env
from gateway.signer import SignerClient
from gateway.tests.test_gateway import NOW, Base, QuietVenue, TESTNET_CAPS

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
            check_caps(kind, action, mid, TESTNET_CAPS)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (403, code), action)

    def test_the_gateways_close_is_one_reduce_only_ioc_order_on_the_list(self):
        close = {"type": "order", "orders": [{"a": BTC, "b": False, "p": "54000", "s": "0.5", "r": True,
                                              "t": {"limit": {"tif": "Ioc"}}}], "grouping": "na"}
        check_caps("close", close, unreadable, TESTNET_CAPS)  # over the size cap: it can only close
        bad = [
            {**close, "grouping": "positionTpsl"},
            {**close, "orders": close["orders"] * 2},
            {**close, "orders": [{**close["orders"][0], "r": False}]},
            {**close, "orders": [{**close["orders"][0], "t": {"limit": {"tif": "Gtc"}}}]},
            {**close, "orders": [{**close["orders"][0], "a": 999}]},
            {**close, "orders": [{**close["orders"][0], "s": "0"}]},
        ]
        for action in bad:
            with self.assertRaises(GatewayError, msg=str(action)):
                check_caps("close", action, unreadable, TESTNET_CAPS)
        # Nothing else is signed as a close, and a close is nothing else.
        with self.assertRaises(GatewayError):
            check_caps("protect", close, unreadable, TESTNET_CAPS)

    def test_the_caps_are_the_platforms_setting(self):
        # The list and the caps come from GATEWAY_SIZE_CAPS (CTO, 21 Oct 2026); the code holds no perp.
        self.assertEqual(size_caps('{"3": "0.005", "4": "0.15", "0": "4"}'), TESTNET_CAPS)
        self.assertEqual(size_caps(""), {})
        for bad in ('["3"]', '{"BTC": "0.005"}', '{"3": "0"}', '{"3": 0.005}', '{"3": "-1"}', '{"3": "0.005"'):
            with self.assertRaises(SystemExit, msg=bad):
                size_caps(bad)
        self.assertEqual(str(MAX_NOTIONAL), "400")
        self.assertEqual(DemoSigner([]).caps, {}, "no setting, nothing on the list: nothing is signed")
        source = pathlib.Path(demo_signer.__file__).read_text()
        self.assertNotIn("Decimal(\"0.005\")", source)

    def test_the_gateway_reads_the_caps_from_its_setting(self):
        env = {"GATEWAY_SIGNER": "demo", "GATEWAY_KEYS_DIR": "x", "GATEWAY_SIZE_CAPS": '{"750003": "0.1"}'}
        with mock.patch.object(DemoSigner, "load_keys", return_value=[]):
            _, signer = signer_from_env(env)
            self.assertEqual(signer.caps, {750003: Decimal("0.1")})
            for empty in ({}, {"GATEWAY_SIZE_CAPS": ""}, {"GATEWAY_SIZE_CAPS": "{}"}):
                with self.assertRaises(SystemExit) as ctx:
                    signer_from_env({"GATEWAY_SIGNER": "demo", "GATEWAY_KEYS_DIR": "x", **empty})
                self.assertIn("GATEWAY_SIZE_CAPS is empty", str(ctx.exception))
        self.assertIn('GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4"}',
                      (pathlib.Path(demo_signer.__file__).parents[1] / "ops/host/bootstrap.sh").read_text())

    def test_each_asset_has_its_size_cap(self):
        for asset, ok, over, px in ((BTC, "0.005", "0.0051", "60000"), (ETH, "0.15", "0.1501", "2000"),
                                    (SOL, "4", "4.01", "90")):
            check_caps("order", order(asset, ok, px), unreadable, TESTNET_CAPS)
            self.refused("over_cap", "order", order(asset, over, px))
            self.refused("over_cap", "order", sell(asset, over, px))  # before the market is read

    def test_notional_cap(self):
        check_caps("order", order(ETH, "0.15", "2666"), unreadable, TESTNET_CAPS)  # 399.9
        check_caps("order", order(BTC, "0.005", "80000"), unreadable, TESTNET_CAPS)  # exactly 400
        self.refused("over_cap", "order", order(ETH, "0.15", "2667"))  # 400.05
        self.refused("over_cap", "order", order(BTC, "0.005", "80000.2"))  # 400.001

    def test_a_sell_under_the_market_counts_at_the_mid(self):
        # 18 Sep 2026: SOL's mid 105.805, and 4 SOL offered at 90 fills at the market.
        self.refused("over_cap", "order", sell(SOL, "4", "90"), Market(SOL="105.805"))  # 423.22
        market = Market(BTC="80000")
        check_caps("order", sell(BTC, "0.005", "60000"), market, TESTNET_CAPS)  # exactly 400 at the mid
        self.assertEqual(market.asked, [BTC])
        self.refused("over_cap", "order", sell(BTC, "0.005", "60000"), Market(BTC="80000.2"))  # 400.001
        self.refused("over_cap", "order", sell(ETH, "0.15", "2000", tif="Ioc"), Market(ETH="2667"))  # 400.05

    def test_a_sell_over_the_market_counts_at_its_limit(self):
        check_caps("order", sell(BTC, "0.005", "80000"), Market(BTC="70000"), TESTNET_CAPS)  # exactly 400
        self.refused("over_cap", "order", sell(BTC, "0.005", "80000.2"), Market(BTC="70000"))  # 400.001

    def test_a_buy_counts_at_its_limit_and_never_reads_the_market(self):
        check_caps("order", order(BTC, "0.005", "79000"), unreadable, TESTNET_CAPS)  # 395, whatever the market
        check_caps("order", order(BTC, "0.005", "80000", tif="Ioc"), unreadable, TESTNET_CAPS)
        self.refused("over_cap", "order", order(BTC, "0.005", "80000.2", tif="Ioc"))
        check_caps("cancel", cancel(SOL), unreadable, TESTNET_CAPS)

    def test_only_a_literal_true_is_a_buy(self):
        for side in (1, "true", None):
            self.refused("over_cap", "order", order(BTC, "0.005", "60000", b=side), Market(BTC="80000.2"))

    def test_reduce_only_passes_the_notional_cap_and_nothing_else(self):
        # 18 Sep 2026: a 4 SOL long closed at a mid of 105.805 is 423 USDC, over the cap.
        check_caps("order", sell(SOL, "4", "90", r=True), unreadable, TESTNET_CAPS)  # no market read either
        self.refused("over_cap", "order", sell(SOL, "4", "90"), Market(SOL="105.805"))  # not reduce-only
        check_caps("order", order(BTC, "0.005", "80000.2", r=True), unreadable, TESTNET_CAPS)  # closing a short: 400.001
        self.refused("over_cap", "order", order(BTC, "0.005", "80000.2"))
        self.refused("over_cap", "order", sell(SOL, "4.01", "90", r=True))  # the size cap still binds
        self.refused("policy", "order", sell(5, "0.001", "10", r=True))  # so does the platform list
        self.refused("policy", "order", sell(BTC, "0.001", "60000", r=True, t={"trigger": {"isMarket": True}}))
        for flag in (1, "true", None):  # only a literal true is reduce-only
            self.refused("over_cap", "order", order(BTC, "0.005", "80000.2", r=flag))

    def test_the_mid_is_hyperliquids(self):
        named = mock.patch.object(hl, "coin_of", lambda asset, timeout=5.0: {SOL: "SOL"}[asset])
        named.start()
        self.addCleanup(named.stop)
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
        check_caps("cancel", cancel(BTC), unreadable, TESTNET_CAPS)

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
        signer = DemoSigner([key], caps=TESTNET_CAPS)
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
            DemoSigner([key], caps=TESTNET_CAPS).sign(key.address, "order", order(BTC, "0.006", "60000"), NOW)


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

    def test_a_key_bound_since_the_start_is_read_from_the_directory(self):
        # A deployment with a key binder writes each key just before it is bound (ops/key_relay.py).
        self.write("demo-agent-01.key", "0x" + bytes(self.key.key).hex())
        now = [100.0]
        signer = DemoSigner(DemoSigner.load_keys(str(self.dir)), caps=TESTNET_CAPS, directory=str(self.dir),
                            clock=lambda: now[0])
        bound = Account.create(os.urandom(32))
        self.write(f"agent-{bound.address}.key", "0x" + bytes(bound.key).hex())
        self.assertFalse(signer.has_key(bound.address), "not before RELOAD_EVERY_S has passed since the last read")
        now[0] += DemoSigner.RELOAD_EVERY_S
        self.assertTrue(signer.has_key(bound.address))
        self.assertTrue(signer.has_key(self.key.address), "the keys it had stay")
        self.assertEqual(len(signer), 2)
        stranger = Account.create(os.urandom(32)).address
        os.chmod(self.dir, 0o750)  # a directory it may not read now leaves the keys it had
        now[0] += DemoSigner.RELOAD_EVERY_S
        self.assertFalse(signer.has_key(stranger))
        self.assertEqual(len(signer), 2)
        os.chmod(self.dir, 0o700)

    def test_a_signer_given_no_directory_never_reads_one(self):
        self.write("demo-agent-01.key", "0x" + bytes(self.key.key).hex())
        now = [0.0]
        signer = DemoSigner([], caps=TESTNET_CAPS, clock=lambda: now[0])
        now[0] = 10.0**9
        self.assertFalse(signer.has_key(self.key.address))

    def test_the_mode_is_chosen_explicitly(self):
        self.write("demo-agent-01.key", "0x" + bytes(self.key.key).hex())
        mode, signer = signer_from_env({"GATEWAY_SIGNER": "demo", "GATEWAY_KEYS_DIR": str(self.dir),
                                        "GATEWAY_SIZE_CAPS": '{"3": "0.005"}'})
        self.assertEqual((mode, type(signer), len(signer)), ("demo", DemoSigner, 1))
        self.assertEqual(signer._directory, str(self.dir), "the directory is read again for a key bound later")
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

        return Gateway(self.reader, DemoSigner([self.enclave_key], mid, caps=TESTNET_CAPS), submit=submit, clock=lambda: NOW / 1000,
                       venue=QuietVenue())

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

    def test_reduce_only_over_the_notional_is_signed_the_same_order_without_it_is_not(self):
        market = Market(BTC="90000")
        gw = self.gateway(market)
        closing = self.body(isBuy=False, size="0.005", limitPx="60000", tif="Ioc", reduceOnly=True)  # 450 at the mid
        status, out = gw.handle_order(closing)
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertIs(self.submitted[0][0]["orders"][0]["r"], True)
        opening = self.body(isBuy=False, size="0.005", limitPx="60000", tif="Ioc", nonce=NOW + 1)
        status, out = gw.handle_order(opening)
        self.assertEqual((status, out["status"], out["code"]), (403, "refused_by_gateway", "over_cap"))
        self.assertEqual((len(self.submitted), market.asked), (1, [BTC]))

    def test_a_sell_is_not_signed_without_a_mid(self):
        def no_mid(asset):
            raise GatewayError(502, "no_market_price", "BTC")

        def offline(asset):
            raise ConnectionError("info API unreachable")

        for mid, code in ((no_mid, "no_market_price"), (offline, "upstream_failed")):
            gw = self.gateway(mid)
            status, out = gw.handle_order(self.body(isBuy=False, size="0.001", limitPx="60000"))
            self.assertEqual((status, out["code"], out["sent"]), (424, code, False))
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
