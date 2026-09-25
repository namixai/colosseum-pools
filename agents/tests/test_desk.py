"""The desk's limits: what an agent may read and send for one account, and buying a
challenge. Offline: the chain, Hyperliquid and the gateway are fakes.

    spike/.venv/bin/python -m unittest discover -s agents/tests -t .
"""

from __future__ import annotations

import unittest
from unittest import mock

from agents import desk as dk
from agents.desk import Refused
from agents.tests.fakes import (ACCOUNT, FACTORY, NEW_CHALLENGE, NOW, POOL, USDC, FakeChain,  # noqa: F401
                                FakeGateway, Wallet)


class WithChain(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain()
        patcher = mock.patch.object(dk, "c", self.chain)
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = mock.patch.object(dk.time, "time", return_value=float(NOW))
        clock.start()
        self.addCleanup(clock.stop)
        self.gateway = FakeGateway()

    def desk(self, send=True, **limits):
        return dk.Desk(FACTORY, ACCOUNT, dk.Limits(**limits), self.gateway if send else None, Wallet(), send)


class BusyDoesNotCost(WithChain):
    """A session gets four orders. Two of them went on `busy` in a live session on 25 Sep 2026 --
    the gateway's own chain reads were rate limited, nothing reached Hyperliquid, and the agent
    paid for our node's bad minute out of its own budget."""

    def ok_order(self, desk):
        return desk.place_order("BTC", "buy", 0.0002, 60000, "limit", False)   # 12 USDC

    def test_a_busy_refusal_gives_the_attempt_back(self):
        self.gateway = FakeGateway(busy_for=2)
        desk = self.desk()
        self.assertEqual(self.ok_order(desk)["answer"]["status"], "busy")
        self.assertEqual(desk.orders_left, 4, "a refusal of ours must not cost the agent an order")
        self.assertEqual(self.ok_order(desk)["answer"]["status"], "busy")
        self.assertEqual(desk.orders_left, 4)
        # And the budget really is still whole: four orders still go through afterwards.
        for _ in range(4):
            self.assertEqual(self.ok_order(desk)["answer"]["status"], "submitted")
        self.assertEqual(desk.orders_left, 0)
        with self.assertRaises(Refused):
            self.ok_order(desk)

    def test_an_order_that_was_sent_still_costs_one(self):
        desk = self.desk()
        self.assertEqual(self.ok_order(desk)["answer"]["status"], "submitted")
        self.assertEqual(desk.orders_left, 3)

    def test_the_venues_own_refusal_costs_one(self):
        # It reached Hyperliquid and Hyperliquid said no. That was an attempt.
        self.gateway.order = lambda *a, **k: {"http": 422, "status": "refused_by_venue",
                                              "reason": "Order price cannot be more than 80% away"}
        desk = self.desk()
        self.ok_order(desk)
        self.assertEqual(desk.orders_left, 3)

    def test_an_answer_that_settles_nothing_costs_one(self):
        # "may or may not have reached Hyperliquid": giving this one back is how an order gets
        # sent twice.
        self.gateway.order = lambda *a, **k: {"http": 502, "status": "uncertain", "code": "send_failed",
                                              "detail": "the order may or may not have reached Hyperliquid"}
        desk = self.desk()
        self.ok_order(desk)
        self.assertEqual(desk.orders_left, 3)

    def test_cancels_are_counted_the_same_way(self):
        self.gateway = FakeGateway(busy_for=1)
        desk = self.desk()
        before = desk.cancels_left
        self.assertEqual(desk.cancel_order("BTC", 1)["answer"]["status"], "busy")
        self.assertEqual(desk.cancels_left, before)
        self.assertEqual(desk.cancel_order("BTC", 1)["answer"]["status"], "submitted")
        self.assertEqual(desk.cancels_left, before - 1)


class DeskLimits(WithChain):
    def test_only_perps_on_the_accounts_list(self):
        with self.assertRaises(Refused):
            self.desk().place_order("SOL", "buy", 1, 150, "limit", False)
        with self.assertRaises(Refused):
            self.desk().market_view("SOL")
        self.assertEqual(self.gateway.orders, [])

    def test_minimum_and_per_order_cap(self):
        desk = self.desk(max_notional=100)
        with self.assertRaises(Refused):
            desk.place_order("BTC", "buy", 0.0001, 60000, "limit", False)  # 6 USDC
        with self.assertRaises(Refused):
            desk.place_order("BTC", "buy", 0.002, 60000, "limit", False)  # 120 USDC
        desk.place_order("BTC", "buy", 0.0015, 60000, "limit", False)  # 90 USDC
        self.assertEqual(self.gateway.orders, [(dk.to_checksum_address(ACCOUNT), 3, True, "60000", "0.0015", "Gtc", False)])

    def test_headroom_under_the_leverage_rule(self):
        self.chain.equity, self.chain.notional = 100.0, 200.0  # rule 3x: 300; headroom 0.8: 240
        desk = self.desk(max_notional=100)
        with self.assertRaises(Refused):
            desk.place_order("ETH", "buy", 0.02, 3000, "ioc", False)  # +60 -> 260
        desk.place_order("ETH", "buy", 0.013, 3000, "ioc", False)  # +39 -> 239
        desk.place_order("ETH", "sell", 0.1, 3000, "ioc", True)  # reduce-only: 300 USDC, no cap
        self.assertEqual([o[5:] for o in self.gateway.orders], [("Ioc", False), ("Ioc", True)])

    def test_a_sell_under_the_market_counts_at_the_mid(self):
        desk = self.desk(max_notional=100)  # the fake's mids: BTC 60000, ETH 3000
        with self.assertRaises(Refused):
            desk.place_order("BTC", "sell", 0.0017, 50000, "ioc", False)  # 85 at its limit, 102 at the mid
        with self.assertRaises(Refused):
            desk.place_order("BTC", "sell", 0.0016, 64000, "limit", False)  # 102.4 at its limit, over the mid
        desk.place_order("BTC", "sell", 0.0016, 50000, "ioc", False)  # 96 at the mid
        desk.place_order("BTC", "sell", 0.0016, 62000, "limit", False)  # 99.2 at its limit
        self.assertEqual([o[2:5] for o in self.gateway.orders], [(False, "50000", "0.0016"), (False, "62000", "0.0016")])

    def test_a_sell_without_a_usable_mid_is_refused(self):
        desk = self.desk(max_notional=100)
        self.chain.positions = [{"coin": "BTC", "szi": "0.0031", "entryPx": "59000", "unrealizedPnl": "3.1"}]
        for mids in ({}, {"BTC": "NaN"}, {"BTC": "inf"}, {"BTC": "0"}, {"BTC": "-1"}, {"BTC": "abc"},
                     {"BTC": None}, ["BTC"]):
            self.chain.mids = mids
            with self.assertRaises(Refused, msg=mids):
                desk.place_order("BTC", "sell", 0.0017, 50000, "ioc", False)  # 85 at its limit
            with self.assertRaises(Refused, msg=mids):
                desk.close_position("BTC")
        self.chain.mids = {"BTC": "60000"}
        desk.place_order("BTC", "buy", 0.0015, 60000, "limit", False)  # a buy asks no mid
        self.assertEqual(len(self.gateway.orders), 1)

    def test_headroom_counts_a_sell_at_the_mid(self):
        self.chain.equity, self.chain.notional = 100.0, 200.0  # rule 3x: 300; headroom 0.8: 240
        desk = self.desk(max_notional=100)
        with self.assertRaises(Refused):
            desk.place_order("ETH", "sell", 0.014, 2500, "ioc", False)  # 35 at its limit, 42 at the mid -> 242
        desk.place_order("ETH", "sell", 0.013, 2500, "ioc", False)  # 39 at the mid -> 239
        self.assertEqual(len(self.gateway.orders), 1)

    def test_orders_per_session(self):
        desk = self.desk(max_orders=2)
        desk.place_order("ETH", "buy", 0.005, 3000, "post_only", False)
        with self.assertRaises(Refused):
            desk.place_order("ETH", "buy", 0.001, 3000, "post_only", False)  # refused, and not counted
        desk.place_order("ETH", "buy", 0.005, 3000, "post_only", False)
        with self.assertRaises(Refused):
            desk.place_order("ETH", "buy", 0.005, 3000, "post_only", False)
        self.assertEqual(len(self.gateway.orders), 2)

    def test_no_orders_mode_sends_nothing(self):
        desk = self.desk(send=False)
        out = desk.place_order("BTC", "sell", 0.001, 61000.4, "limit", False)
        self.assertEqual(out, {"status": "not_sent", "order": {"coin": "BTC", "side": "sell", "size": "0.001",
                                                                "limit_price": "61000", "tif": "Gtc",
                                                                "reduce_only": False}})
        self.assertEqual(desk.cancel_order("BTC", 9)["status"], "not_sent")
        self.assertEqual(desk.graduate()["status"], "not_sent")
        self.assertEqual((self.gateway.orders, self.gateway.cancels, self.chain.sent), ([], [], []))

    def test_close_position_crosses_the_book_reduce_only(self):
        desk = self.desk()
        with self.assertRaises(Refused):
            desk.close_position("BTC")
        self.chain.positions = [{"coin": "BTC", "szi": "0.0031", "entryPx": "59000", "unrealizedPnl": "3.1"}]
        desk.close_position("BTC")
        self.assertEqual(self.gateway.orders[-1][2:], (False, "58800", "0.0031", "Ioc", True))

    def test_graduation_once_with_a_readable_refusal(self):
        desk = self.desk()
        selector = "0x" + dk.keccak(text="NotFlat()")[:4].hex()
        self.chain.revert = selector
        self.assertEqual(desk.graduate(), {"status": "refused_by_contract", "reason": "NotFlat"})
        with self.assertRaises(Refused):
            desk.graduate()

    def test_account_view_shows_the_floors(self):
        view = self.desk().account_view()
        self.assertEqual(view["limits_now"], {"equity_floor_drawdown_usdc": 900.0, "equity_floor_today_usdc": 940.5,
                                              "max_open_notional_by_rule_usdc": 3000.0})
        self.assertEqual(view["challenge"]["target_equity_usdc"], 1100.0)
        self.assertEqual(view["challenge"]["hours_left"], 36.0)
        self.assertEqual(view["contract_verdict_now"], "inside the rules")
        self.chain.verdict = 3
        self.assertEqual(self.desk().account_view()["contract_verdict_now"], "Leverage")

    def test_account_view_keeps_the_two_shares_apart(self):
        # The fake's terms pay 0% for passing the challenge and 80% on the funded account.
        # Reading one field where the other is meant swaps these two numbers.
        challenge = self.desk().account_view()["challenge"]
        self.assertEqual(challenge["trader_share_of_challenge_profit_pct"], 0.0)
        self.assertEqual(challenge["trader_share_of_funded_profit_pct"], 80.0)

    def test_a_stranger_account_is_refused(self):
        self.chain.challenge = False
        self.chain.call_view = lambda to, sig, *a: (False,) if to.lower() == FACTORY.lower() else (None,)
        with self.assertRaises(SystemExit):
            self.desk()


class ShopLimits(WithChain):
    def shop(self, send=True, max_price=25.0):
        return dk.Shop(FACTORY, max_price, Wallet(), send)

    def test_listing_shows_only_pools_that_can_sell_now(self):
        offers = self.shop().listing()
        self.assertEqual([o["pool"] for o in offers], [dk.to_checksum_address(POOL)])
        self.assertEqual(offers[0]["price_usdc"], 20.0)
        self.assertEqual(offers[0]["rules"]["perps"], ["BTC", "ETH"])
        self.assertNotIn("_price_units", offers[0])
        # What a buyer is told they get, per stage, and the two are not the same number.
        self.assertEqual(offers[0]["trader_share_of_challenge_profit_pct"], 0.0)
        self.assertEqual(offers[0]["trader_share_of_funded_profit_pct"], 80.0)
        self.assertEqual(offers[0]["funded_capital_after_passing_usdc"], 5000.0)

    def test_buy_needs_the_listing_price_and_the_cap(self):
        shop = self.shop(max_price=15.0)
        with self.assertRaises(Refused):
            shop.buy(POOL, 20.0)  # not listed yet
        shop.listing()
        with self.assertRaises(Refused):
            shop.buy(POOL, 19.0)  # not the listed price
        with self.assertRaises(Refused):
            shop.buy(POOL, 20.0)  # over the cap
        self.assertEqual(self.chain.sent, [])

    def test_one_purchase_approving_the_exact_price(self):
        shop = self.shop()
        shop.listing()
        out = shop.buy(POOL, 20.0)
        self.assertEqual(out["challenge"], dk.to_checksum_address(NEW_CHALLENGE))
        self.assertEqual(self.chain.sent, [(USDC.lower(), "approve", [dk.to_checksum_address(POOL), 20_000_000]),
                                           (POOL.lower(), "buyChallenge", [])])
        with self.assertRaises(Refused):
            shop.buy(POOL, 20.0)

    def test_the_platform_fee_counts_toward_the_cap_and_the_approval(self):
        self.chain.fee = 3_000_000
        shop = self.shop(max_price=22.0)
        offers = shop.listing()
        self.assertEqual((offers[0]["platform_fee_usdc"], offers[0]["total_to_pay_usdc"]), (3.0, 23.0))
        with self.assertRaises(Refused):
            shop.buy(POOL, 20.0)  # 20 fits the cap, 23 doesn't
        shop = self.shop(max_price=23.0)
        shop.listing()
        shop.buy(POOL, 20.0)
        self.assertEqual(self.chain.sent[0], (USDC.lower(), "approve", [dk.to_checksum_address(POOL), 23_000_000]))

    def test_dry_shop_buys_nothing(self):
        shop = self.shop(send=False)
        shop.listing()
        self.assertEqual(shop.buy(POOL, 20.0)["status"], "not_sent")
        self.assertEqual(self.chain.sent, [])


if __name__ == "__main__":
    unittest.main()
