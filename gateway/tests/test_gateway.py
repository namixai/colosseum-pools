"""Gateway tests. Offline: the chain, the Signer and Hyperliquid are replaced by fakes.

    spike/.venv/bin/python -m unittest discover -s gateway/tests -t .
"""

from __future__ import annotations

import os
import unittest

from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action

from gateway import auth, hl
from gateway.checks import GatewayError, NonceBook, Request, check
from gateway.server import Gateway
from gateway.signer import SignResult

# The Hyperliquid Python SDK's own published test key and vectors (tests/signing_test.py). A
# public test value, not a secret. Actions are written out here by hand, in the field order
# Hyperliquid hashes.
SDK_TEST_KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"
SDK_ORDER_ACTION = {
    "type": "order",
    "orders": [{"a": 1, "b": True, "p": "100", "s": "100", "r": False, "t": {"limit": {"tif": "Gtc"}}}],
    "grouping": "na",
}
SDK_TESTNET_SIG = {
    "r": "0x82b2ba28e76b3d761093aaded1b1cdad4960b3af30212b343fb2e6cdfa4e3d54",
    "s": "0x6b53878fc99d26047f4d7e8c90eb98955a109f44209163f52d8dc4278cbbd9f5",
    "v": 27,
}

ACCOUNT = "0x00000000000000000000000000000000000000A1"
NOW = 1_800_000_000_000


def order_fields(**over) -> dict:
    fields = {
        "account": ACCOUNT, "asset": 3, "isBuy": True, "limitPx": "60000", "size": "0.0002",
        "reduceOnly": False, "tif": "Alo", "nonce": NOW, "expiresAt": NOW + 30_000,
    }
    fields.update(over)
    return fields


def cancel_fields(**over) -> dict:
    fields = {"account": ACCOUNT, "asset": 3, "oid": 42, "nonce": NOW, "expiresAt": NOW + 30_000}
    fields.update(over)
    return fields


class FakeReader:
    def __init__(self, key: str, trader: str, assets=frozenset({3, 4})):
        self.key, self.trader, self.assets = key, trader, set(assets)
        self.accounts = {ACCOUNT.lower()}
        self.trading = True

    def is_account(self, account):
        return account.lower() in self.accounts

    def trading_key(self, account):
        return self.key if self.trading else None

    def is_bound(self, key, account, trader):
        return key == self.key and account.lower() == ACCOUNT.lower() and trader.lower() == self.trader.lower()

    def allowed_assets(self, account):
        return self.assets


class FakeSigner:
    """Signs with a local key the way the enclave would, or refuses."""

    def __init__(self, wallet, key_address: str, refuse: bool = False):
        self.wallet, self.key_address, self.refuse = wallet, key_address, refuse
        self.calls = []

    def has_key(self, key):
        return key.lower() == self.key_address.lower()

    def sign(self, key, kind, action, nonce):
        self.calls.append((kind, action, nonce))
        if self.refuse:
            return SignResult(403, None, {"decision": "deny"}, {"error": "policy_denied", "receipt": {"decision": "deny"}})
        sig = sign_l1_action(self.wallet, action, None, nonce, None, False)
        return SignResult(200, sig, {"decision": "allow"}, {"signature": sig})


class HyperliquidVectors(unittest.TestCase):
    def test_recover_matches_the_sdk_published_vector(self):
        expected = Account.from_key(SDK_TEST_KEY).address
        self.assertEqual(hl.recover_signer(SDK_ORDER_ACTION, SDK_TESTNET_SIG, 0), expected)

    def test_action_hash_matches_the_sdk_production_vector(self):
        # SDK tests/signing_test.py, "test_phantom_agent_creation_matches_production".
        action = {
            "type": "order",
            "orders": [{"a": 4, "b": True, "p": "1670.1", "s": "0.0147", "r": False, "t": {"limit": {"tif": "Ioc"}}}],
            "grouping": "na",
        }
        self.assertEqual(
            "0x" + hl.hash_action(action, 1677777606040).hex(),
            "0x0fcbeda5ae3c4950a548021552a4fea2226858c4453571bf3f24ba017eac2908",
        )

    def test_field_order_is_part_of_the_hash(self):
        reordered = {"orders": SDK_ORDER_ACTION["orders"], "type": "order", "grouping": "na"}
        self.assertNotEqual(hl.hash_action(SDK_ORDER_ACTION, 0), hl.hash_action(reordered, 0))
        self.assertNotEqual(hl.recover_signer(reordered, SDK_TESTNET_SIG, 0), Account.from_key(SDK_TEST_KEY).address)

    def test_built_action_is_the_sdk_vector(self):
        # The gateway's own action builder, fed the SDK vector's fields, gives the SDK's bytes.
        req = Request("order", {
            "account": ACCOUNT, "asset": 1, "isBuy": True, "limitPx": "100", "size": "100",
            "reduceOnly": False, "tif": "Gtc", "nonce": 1, "expiresAt": 2,
        }, "0x" + "00" * 65)
        self.assertEqual(req.action(), SDK_ORDER_ACTION)
        self.assertEqual(list(req.action()["orders"][0]), ["a", "b", "p", "s", "r", "t"])
        self.assertEqual(hl.recover_signer(req.action(), SDK_TESTNET_SIG, 0), Account.from_key(SDK_TEST_KEY).address)


class Base(unittest.TestCase):
    def setUp(self):
        self.trader = Account.create(os.urandom(32))
        self.enclave_key = Account.create(os.urandom(32))
        self.reader = FakeReader(self.enclave_key.address, self.trader.address)
        self.nonces = NonceBook()

    def body(self, kind="order", signer=None, **over) -> dict:
        fields = order_fields(**over) if kind == "order" else cancel_fields(**over)
        sig = auth.sign(signer or self.trader, kind, fields)
        return {"kind": kind, kind: fields, "signature": sig}

    def request(self, **kw) -> Request:
        return Request.from_json(self.body(**kw))

    @staticmethod
    def unsigned(kind="order", **over) -> dict:
        """A body with a placeholder signature: shape checks run before any signature check,
        and some malformed values can't be EIP-712 encoded at all."""
        fields = order_fields(**over) if kind == "order" else cancel_fields(**over)
        return {"kind": kind, kind: fields, "signature": "0x" + "11" * 65}


class Checks(Base):
    def assertRefused(self, code, req, reader=None):
        with self.assertRaises(GatewayError) as ctx:
            check(req, reader or self.reader, NOW, self.nonces)
        self.assertEqual(ctx.exception.code, code)

    def test_happy_path_order_and_cancel(self):
        cleared = check(self.request(), self.reader, NOW, self.nonces)
        self.assertEqual((cleared.trader, cleared.key), (self.trader.address, self.enclave_key.address))
        cleared = check(self.request(kind="cancel", nonce=NOW + 1), self.reader, NOW, self.nonces)
        self.assertEqual(cleared.trader, self.trader.address)

    def test_expiry_window(self):
        self.assertRefused("expired", self.request(expiresAt=NOW))
        self.assertRefused("expired", self.request(expiresAt=NOW + 60_001))

    def test_changed_field_is_not_the_traders(self):
        body = self.body()
        body["order"]["size"] = "0.0003"
        self.assertRefused("not_your_account", Request.from_json(body))
        body = self.body()
        body["order"]["isBuy"] = False
        self.assertRefused("not_your_account", Request.from_json(body))

    def test_order_signature_is_not_a_cancel_signature(self):
        fields = cancel_fields()
        sig = auth.sign(self.trader, "order", {**order_fields(), **{k: fields[k] for k in ("nonce", "expiresAt")}})
        self.assertRefused("not_your_account", Request.from_json({"kind": "cancel", "cancel": fields, "signature": sig}))

    def test_someone_elses_signature(self):
        self.assertRefused("not_your_account", self.request(signer=Account.create(os.urandom(32))))

    def test_unknown_account(self):
        self.assertRefused("not_an_account", self.request(account="0x00000000000000000000000000000000000000B2"))

    def test_account_not_trading(self):
        self.reader.trading = False
        self.assertRefused("not_trading", self.request())

    def test_asset_outside_the_rules(self):
        self.assertRefused("asset_not_allowed", self.request(asset=0))
        self.assertRefused("asset_not_allowed", self.request(kind="cancel", asset=0))

    def test_replay(self):
        req = self.request()
        check(req, self.reader, NOW, self.nonces)
        self.assertRefused("replayed", req)

    def test_refused_request_does_not_burn_the_nonce(self):
        self.reader.trading = False
        self.assertRefused("not_trading", self.request())
        self.reader.trading = True
        check(self.request(), self.reader, NOW, self.nonces)

    def test_shape(self):
        bad_bodies = [
            {}, [], {"kind": "modify"},
            {"kind": "order", "order": order_fields(), "signature": "0x12"},
            self.unsigned(limitPx="60000.0"),      # not canonical: Hyperliquid would verify other bytes
            self.unsigned(size="0.00020"),
            self.unsigned(size="0"),
            self.unsigned(limitPx="1e5"),
            self.unsigned(tif="Fok"),
            self.unsigned(isBuy=1),
            self.unsigned(asset=-1),
            self.unsigned(asset=True),
            self.unsigned(kind="cancel", oid=0),
            self.unsigned(account="nope"),
        ]
        extra = self.unsigned()
        extra["order"]["builder"] = {"b": ACCOUNT, "f": 10}
        bad_bodies.append(extra)
        missing = self.unsigned()
        del missing["order"]["reduceOnly"]
        bad_bodies.append(missing)
        # and the placeholder itself is well formed, so each case fails for its own reason
        Request.from_json(self.unsigned())
        for body in bad_bodies:
            with self.subTest(body=body), self.assertRaises(GatewayError) as ctx:
                Request.from_json(body)
            self.assertEqual(ctx.exception.code, "bad_request")

    def test_actions(self):
        order = self.request(isBuy=False, reduceOnly=True, tif="Ioc").action()
        self.assertEqual(order, {
            "type": "order",
            "orders": [{"a": 3, "b": False, "p": "60000", "s": "0.0002", "r": True, "t": {"limit": {"tif": "Ioc"}}}],
            "grouping": "na",
        })
        self.assertEqual(self.request(kind="cancel").action(), {"type": "cancel", "cancels": [{"a": 3, "o": 42}]})


class Flow(Base):
    def gateway(self, signer):
        self.submitted = []

        def submit(action, nonce, signature):
            self.submitted.append((action, nonce, signature))
            return {"status": "ok"}

        return Gateway(self.reader, signer, submit=submit, clock=lambda: NOW / 1000)

    def test_submits_what_the_trader_signed_and_the_enclave_signed(self):
        signer = FakeSigner(self.enclave_key, self.enclave_key.address)
        status, out = self.gateway(signer).handle_order(self.body())
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertEqual(len(self.submitted), 1)
        action, nonce, sig = self.submitted[0]
        self.assertEqual(action["orders"][0]["s"], "0.0002")
        self.assertEqual(nonce, NOW)
        self.assertEqual(signer.calls[0], ("order", action, NOW))
        self.assertEqual(hl.recover_signer(action, sig, nonce), self.enclave_key.address)

    def test_wrong_signing_key_is_never_submitted(self):
        stranger = Account.create(os.urandom(32))
        status, out = self.gateway(FakeSigner(stranger, self.enclave_key.address)).handle_order(self.body())
        self.assertEqual((status, out["status"]), (502, "signature_mismatch"))
        self.assertEqual(self.submitted, [])

    def test_enclave_refusal_comes_back_with_its_receipt(self):
        signer = FakeSigner(self.enclave_key, self.enclave_key.address, refuse=True)
        status, out = self.gateway(signer).handle_order(self.body())
        self.assertEqual((status, out["status"]), (403, "refused_by_signer"))
        self.assertEqual(out["receipt"], {"decision": "deny"})
        self.assertEqual(self.submitted, [])

    def test_gateway_refusal_never_reaches_the_enclave(self):
        signer = FakeSigner(self.enclave_key, self.enclave_key.address)
        self.reader.trading = False
        status, out = self.gateway(signer).handle_order(self.body())
        self.assertEqual((status, out["code"]), (409, "not_trading"))
        self.assertEqual(signer.calls, [])

    def test_key_without_a_token(self):
        other = Account.create(os.urandom(32))
        status, out = self.gateway(FakeSigner(other, other.address)).handle_order(self.body())
        self.assertEqual((status, out["code"]), (503, "key_not_configured"))


if __name__ == "__main__":
    unittest.main()


class Http(unittest.TestCase):
    """The HTTP layer on a real socket, with the gateway logic replaced."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from gateway.server import make_handler

        class Stub:
            def handle_order(self, body):
                return 200, {"status": "echo", "kind": body.get("kind")}

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Stub(), "https://app.example"))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, headers=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def test_order_roundtrip_and_cors(self):
        status, headers, data = self.call("POST", "/v1/order", b'{"kind": "order"}', {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertIn(b'"echo"', data)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://app.example")

    def test_preflight(self):
        status, headers, _ = self.call("OPTIONS", "/v1/order")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://app.example")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))

    def test_refusals(self):
        self.assertEqual(self.call("POST", "/v1/order", b"{nope", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/order", b"")[0], 413)
        self.assertEqual(self.call("POST", "/v1/order", b"x" * (64 * 1024 + 1))[0], 413)
        self.assertEqual(self.call("POST", "/elsewhere", b"{}")[0], 404)
        self.assertEqual(self.call("GET", "/v1/health")[0], 200)


class AppAgreesWithGateway(unittest.TestCase):
    """The browser app signs with its own copy of the EIP-712 types. A field renamed or
    reordered on one side only would make every signature from the app fail to verify."""

    def test_types_and_domain_match(self):
        import pathlib
        import re

        js = (pathlib.Path(__file__).resolve().parents[2] / "app" / "lib" / "gateway.js").read_text()
        for name, py_fields in (("Order", auth.ORDER_TYPE), ("Cancel", auth.CANCEL_TYPE)):
            block = re.search(rf"{name}: \[(.*?)\]", js, re.S).group(1)
            js_fields = re.findall(r'\{ name: "(\w+)", type: "(\w+)" \}', block)
            self.assertEqual(js_fields, [(f["name"], f["type"]) for f in py_fields], name)
        domain = re.search(r"const DOMAIN = \{ name: \"([^\"]+)\", version: \"(\d+)\"", js)
        self.assertEqual((domain.group(1), domain.group(2)), (auth.DOMAIN["name"], auth.DOMAIN["version"]))
