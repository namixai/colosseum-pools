"""Gateway tests. Offline: the chain, the Signer and Hyperliquid are replaced by fakes.

    spike/.venv/bin/python -m unittest discover -s gateway/tests -t .
"""

from __future__ import annotations

import os
import unittest

from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action

from gateway import auth, hl
from gateway.checks import GatewayError, NonceBook, OrderRequest, check
from gateway.server import Gateway
from gateway.signer import SignResult

# The Hyperliquid Python SDK's own published test key and vector (tests/signing_test.py,
# "test_l1_action_signing_order_matches"). A public test value, not a secret. The action is
# written out here by hand, in the field order Hyperliquid hashes.
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


def order(asset: int = 3) -> dict:
    return {
        "type": "order",
        "orders": [{"a": asset, "b": True, "p": "60000", "s": "0.0002", "r": False, "t": {"limit": {"tif": "Alo"}}}],
        "grouping": "na",
    }


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
        self.calls = 0

    def has_key(self, key):
        return key.lower() == self.key_address.lower()

    def sign(self, key, kind, action, nonce):
        self.calls += 1
        if self.refuse:
            return SignResult(403, None, {"decision": "deny"}, {"error": "policy_denied", "receipt": {"decision": "deny"}})
        sig = sign_l1_action(self.wallet, action, None, nonce, None, False)
        return SignResult(200, sig, {"decision": "allow"}, {"signature": sig})


class Base(unittest.TestCase):
    def setUp(self):
        self.trader = Account.create(os.urandom(32))
        self.enclave_key = Account.create(os.urandom(32))
        self.reader = FakeReader(self.enclave_key.address, self.trader.address)
        self.nonces = NonceBook()

    def request(self, action=None, nonce=NOW, expires=NOW + 30_000, signer=None, account=ACCOUNT) -> OrderRequest:
        action = action or order()
        sig = auth.sign_order(signer or self.trader, account, action, nonce, expires)
        return OrderRequest.from_json({
            "account": account, "kind": action["type"], "action": action,
            "nonce": nonce, "expiresAt": expires, "signature": sig,
        })


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


class Checks(Base):
    def test_happy_path(self):
        cleared = check(self.request(), self.reader, NOW, self.nonces)
        self.assertEqual(cleared.trader, self.trader.address)
        self.assertEqual(cleared.key, self.enclave_key.address)

    def assertRefused(self, code, req, reader=None):
        with self.assertRaises(GatewayError) as ctx:
            check(req, reader or self.reader, NOW, self.nonces)
        self.assertEqual(ctx.exception.code, code)

    def test_expiry_window(self):
        self.assertRefused("expired", self.request(expires=NOW))
        self.assertRefused("expired", self.request(expires=NOW + 60_001))

    def test_changed_action_is_not_the_traders(self):
        req = self.request()
        tampered = OrderRequest(req.account, req.kind, order(asset=4), req.nonce, req.expires_at, req.signature)
        self.assertRefused("not_your_account", tampered)

    def test_someone_elses_signature(self):
        self.assertRefused("not_your_account", self.request(signer=Account.create(os.urandom(32))))

    def test_unknown_account(self):
        other = "0x00000000000000000000000000000000000000B2"
        self.assertRefused("not_an_account", self.request(account=other))

    def test_account_not_trading(self):
        self.reader.trading = False
        self.assertRefused("not_trading", self.request())

    def test_asset_outside_the_rules(self):
        self.assertRefused("asset_not_allowed", self.request(action=order(asset=0)))

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
        mismatched = order()
        mismatched["type"] = "cancel"
        req = OrderRequest.from_json({"account": ACCOUNT, "kind": "order", "action": mismatched, "nonce": 1,
                                      "expiresAt": NOW + 1, "signature": "0x" + "00" * 65})
        self.assertRefused("bad_request", req)
        too_many = order()
        too_many["orders"] = too_many["orders"] * 65
        self.assertRefused("bad_request", self.request(action=too_many))
        smuggled = {"type": "cancel", "cancels": [{"a": 3, "o": 1}], "orders": order()["orders"]}
        self.assertRefused("bad_request", self.request(action=smuggled))
        for body in ({}, {"account": "nope"}, []):
            with self.assertRaises(GatewayError):
                OrderRequest.from_json(body)


class Flow(Base):
    def gateway(self, signer):
        self.submitted = []

        def submit(action, nonce, signature):
            self.submitted.append((action, nonce, signature))
            return {"status": "ok"}

        return Gateway(self.reader, signer, submit=submit, clock=lambda: NOW / 1000)

    def body(self, req):
        return {"account": req.account, "kind": req.kind, "action": req.action, "nonce": req.nonce,
                "expiresAt": req.expires_at, "signature": req.signature}

    def test_submits_what_the_enclave_signed(self):
        gw = self.gateway(FakeSigner(self.enclave_key, self.enclave_key.address))
        status, out = gw.handle_order(self.body(self.request()))
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertEqual(len(self.submitted), 1)
        action, nonce, sig = self.submitted[0]
        self.assertEqual(hl.recover_signer(action, sig, nonce), self.enclave_key.address)

    def test_wrong_signing_key_is_never_submitted(self):
        stranger = Account.create(os.urandom(32))
        gw = self.gateway(FakeSigner(stranger, self.enclave_key.address))
        status, out = gw.handle_order(self.body(self.request()))
        self.assertEqual((status, out["status"]), (502, "signature_mismatch"))
        self.assertEqual(self.submitted, [])

    def test_enclave_refusal_comes_back_with_its_receipt(self):
        signer = FakeSigner(self.enclave_key, self.enclave_key.address, refuse=True)
        gw = self.gateway(signer)
        status, out = gw.handle_order(self.body(self.request()))
        self.assertEqual((status, out["status"]), (403, "refused_by_signer"))
        self.assertEqual(out["receipt"], {"decision": "deny"})
        self.assertEqual(self.submitted, [])

    def test_gateway_refusal_never_reaches_the_enclave(self):
        signer = FakeSigner(self.enclave_key, self.enclave_key.address)
        self.reader.trading = False
        status, out = self.gateway(signer).handle_order(self.body(self.request()))
        self.assertEqual((status, out["code"]), (409, "not_trading"))
        self.assertEqual(signer.calls, 0)

    def test_key_without_a_token(self):
        other = Account.create(os.urandom(32))
        status, out = self.gateway(FakeSigner(other, other.address)).handle_order(self.body(self.request()))
        self.assertEqual((status, out["code"]), (503, "key_not_configured"))


if __name__ == "__main__":
    unittest.main()
