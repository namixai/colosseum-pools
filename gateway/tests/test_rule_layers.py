"""Each of a pool's rules against the gateway: what it refuses before it signs, what it leaves to
the stop and the take on the exchange, and what it leaves to the contract and the keeper alone.
docs/GATEWAY.md, "The rules, layer by layer", is the table these tests hold.

Leverage is refused before the signature by this commit's gateway: an order after which the
account could break the pool's leverage rule is not signed (`403 over_leverage`). The pools host
ran release 4900bcb1 when this was written, which does not read the rule; the check holds there
only once a release with it is deployed and scripts/verify-gateway-deploy.sh prints PASS.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal
from unittest import mock

from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from gateway import hl
from gateway.demo_signer import DemoSigner
from gateway.protect import LONG, Market, RuleLimits, lines
from gateway.server import Gateway
from gateway.tests.test_gateway import ACCOUNT, NOW
from gateway.tests.test_protect import MARKETS, Exchange, FlowBase, Reader, challenge

BTC = 3


class LeverageIsRefusedBeforeTheSignature(FlowBase):
    """The record of deployment 2 (docs/EVIDENCE.md, "Deployment 2, and the first run where the
    trader was not us"): 0.00297 BTC at 84076 on a challenge of 70 USDC, 249.71 of notional,
    3.57x against a 3x rule. The gateway the host ran then signed it, and the contract stopped the
    challenge for leverage afterwards. Replayed here against the same rule, it is refused before
    anything is signed."""

    def setUp(self):
        super().setUp()
        self.markets = dict(MARKETS)
        self.markets[BTC] = Market("BTC", Decimal("84076"), 5)
        self.rules = replace(challenge(capital=Decimal(70)), max_leverage_x100=300)
        self.reader = Reader(self.key.address, self.trader.address, self.rules)
        self.x = Exchange(equity="70", markets=self.markets)
        self.gw = Gateway(self.reader, DemoSigner([self.key], mid=lambda a: self.markets[a].mark),
                          submit=self.x.submit, clock=lambda: self.now, venue=self.x)

    def test_the_gateway_reads_the_leverage_rule(self):
        # What the gateway knows of an account's rules: the floors, the target, and the leverage.
        self.assertEqual(list(RuleLimits.__dataclass_fields__),
                         ["challenge", "daily_loss_bps", "max_drawdown_bps", "drawdown_base", "day",
                          "day_start_equity", "target_bps", "max_leverage_x100"])
        self.assertEqual(self.rules.max_leverage_x100, 300)

    def test_an_order_past_the_leverage_rule_is_refused_before_anything_is_signed(self):
        status, out = self.send(limitPx="84076", size="0.00297")
        self.assertGreater(Decimal("0.00297") * 84076 / 70, 3)  # 3.57x against a 3x rule
        self.assertEqual((status, out["status"], out["code"]), (403, "refused_by_gateway", "over_leverage"))
        # Neither the stop and the take nor the order: nothing was signed or sent.
        self.assertEqual(self.x.sent, [])

    def test_an_order_inside_the_leverage_rule_is_signed_and_sent_with_its_stop_first(self):
        status, out = self.send(limitPx="84076", size="0.0024")
        self.assertLess(Decimal("0.0024") * 84076 / 70, 3)  # 2.88x
        self.assertEqual((status, out["status"]), (200, "submitted"))
        protective, order = self.x.sent
        self.assertEqual(protective["grouping"], "positionTpsl")
        self.assertEqual(order["orders"][0]["s"], "0.0024")
        line = lines(self.rules, Decimal(70), {BTC: {LONG: Decimal("0.0024")}}, self.markets)
        self.assertEqual(out["protection"][0]["stop"], str(line[(BTC, LONG)].stop))

    def test_an_account_already_past_its_leverage_opens_nothing_more_while_it_is_above_its_floor(self):
        # 0.02 BTC held on 70 of equity is 24x: above the floors, and still no order that adds goes.
        self.x.positions = {BTC: Decimal("0.02")}
        status, out = self.send(limitPx="84076", size="0.001")
        self.assertEqual((status, out["status"], out["code"]), (403, "refused_by_gateway", "over_leverage"))
        self.assertEqual(self.x.sent, [])


class TheVenueIsHyperliquidsTestnet(unittest.TestCase):
    """The one venue the deployed code knows: Hyperliquid's testnet, for submitting and for reading."""

    def test_orders_are_submitted_to_the_testnet_exchange_and_nowhere_else(self):
        sent = []

        class Answer:
            status_code = 200

            def json(self):
                return {"status": "ok"}

        with mock.patch.object(hl.requests, "post", lambda url, **kw: sent.append(url) or Answer()):
            hl.submit({"type": "noop"}, NOW, {"r": "0x0", "s": "0x0", "v": 27})
        self.assertEqual(sent, ["https://api.hyperliquid-testnet.xyz/exchange"])
        self.assertEqual(hl.TESTNET_INFO_URL, "https://api.hyperliquid-testnet.xyz/info")
        urls = [v for v in vars(hl).values() if isinstance(v, str) and v.startswith("https://")]
        self.assertTrue(urls and all("hyperliquid-testnet.xyz" in u for u in urls), urls)

    def test_a_signature_is_good_on_testnet_only(self):
        # Hyperliquid's phantom agent carries the network: source "b" on testnet, "a" on mainnet.
        # A signature the gateway's signer makes recovers to its key on testnet and to some other
        # address on mainnet, so the same action can't be replayed there.
        key = Account.create()
        action = {"type": "order", "orders": [{"a": BTC, "b": True, "p": "60000", "s": "0.001", "r": False,
                                                "t": {"limit": {"tif": "Gtc"}}}], "grouping": "na"}
        signed = DemoSigner([key], mid=lambda a: Decimal(60000)).sign(key.address, "order", action, NOW)
        sig = signed.signature
        self.assertEqual(hl.recover_signer(action, sig, NOW).lower(), key.address.lower())
        on_mainnet = recover_agent_or_user_from_l1_action(action, sig, None, NOW, None, True)
        self.assertNotEqual(on_mainnet.lower(), key.address.lower())


if __name__ == "__main__":
    unittest.main()
