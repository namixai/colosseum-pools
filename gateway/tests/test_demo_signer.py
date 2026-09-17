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

from eth_account import Account

from gateway import hl
from gateway.checks import GatewayError
from gateway.demo_signer import MAX_NOTIONAL, MAX_SIZE, DemoSigner, check_caps
from gateway.server import Gateway, signer_from_env
from gateway.signer import SignerClient
from gateway.tests.test_gateway import NOW, Base

BTC, ETH, SOL = 3, 4, 0


def order(asset, size, px, tif="Gtc", **over):
    o = {"a": asset, "b": True, "p": px, "s": size, "r": False, "t": {"limit": {"tif": tif}}}
    o.update(over)
    return {"type": "order", "orders": [o], "grouping": "na"}


def cancel(asset, oid=7):
    return {"type": "cancel", "cancels": [{"a": asset, "o": oid}]}


class Caps(unittest.TestCase):
    def refused(self, code, kind, action):
        with self.assertRaises(GatewayError) as ctx:
            check_caps(kind, action)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (403, code), action)

    def test_the_caps_are_the_platforms(self):
        self.assertEqual({k: str(v) for k, v in MAX_SIZE.items()}, {BTC: "0.005", ETH: "0.15", SOL: "4"})
        self.assertEqual(str(MAX_NOTIONAL), "400")

    def test_each_asset_has_its_size_cap(self):
        for asset, ok, over, px in ((BTC, "0.005", "0.0051", "60000"), (ETH, "0.15", "0.1501", "2000"),
                                    (SOL, "4", "4.01", "90")):
            check_caps("order", order(asset, ok, px))
            self.refused("over_cap", "order", order(asset, over, px))

    def test_notional_cap(self):
        check_caps("order", order(ETH, "0.15", "2666"))  # 399.9
        check_caps("order", order(BTC, "0.005", "80000"))  # exactly 400
        self.refused("over_cap", "order", order(ETH, "0.15", "2667"))  # 400.05
        self.refused("over_cap", "order", order(BTC, "0.005", "80000.2"))  # 400.001

    def test_only_listed_assets(self):
        self.refused("policy", "order", order(5, "0.001", "10"))
        self.refused("policy", "cancel", cancel(5))
        check_caps("cancel", cancel(BTC))

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
    def gateway(self):
        self.submitted = []

        def submit(action, nonce, signature):
            self.submitted.append((action, nonce, signature))
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        return Gateway(self.reader, DemoSigner([self.enclave_key]), submit=submit, clock=lambda: NOW / 1000)

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
