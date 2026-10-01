"""The stop and the take the gateway keeps on Hyperliquid (gateway/protect.py). Offline: the chain
and Hyperliquid are fakes; the Hyperliquid answers are the shapes measured on testnet on
28 Sep 2026 (spike/tpsl_probe.py).

    spike/.venv/bin/python -m unittest gateway.tests.test_protect
"""

from __future__ import annotations

import inspect
import os
import pathlib
import re
import threading
import unittest
from decimal import ROUND_CEILING, Decimal

from eth_account import Account

from gateway import auth, chain, hl, protect, server
from gateway.checks import GatewayError
from gateway.demo_signer import DemoSigner, check_caps
from gateway.protect import (LONG, SHORT, Book, Extra, Market, Protective, Protector, RuleLimits, exposure, lines,
                             order_wire, parse_book, parse_markets, reconcile, valid_px, wire_number)
from gateway.server import Gateway
from gateway.tests.test_gateway import ACCOUNT, NOW, FakeReader

ROOT = pathlib.Path(__file__).resolve().parents[2]
BTC, ETH, SOL = 3, 4, 0
MARKETS = {SOL: Market("SOL", Decimal("150"), 2), BTC: Market("BTC", Decimal("60000"), 5),
           ETH: Market("ETH", Decimal("3000"), 4)}
DAY = 20_833  # the UTC day NOW (in ms) falls on
USDC = 1_000_000
BPS_I = 10_000


def funded(equity_at_start=1000, day_start=1000, day=DAY) -> RuleLimits:
    """A funded pool: 3% a day, 5% drawdown, an 8% target, all measured from 1000 USDC."""
    return RuleLimits(False, 300, 500, equity_at_start * USDC, day, day_start * USDC, 800)


def challenge(capital=Decimal(3)) -> RuleLimits:
    """The live check's challenge: 3 USDC, 3% a day, 6% drawdown, 8% target."""
    c = int(capital * USDC)
    return RuleLimits(True, 300, 600, c, DAY, c, 800)


def long(asset, size) -> dict:
    return {asset: {LONG: Decimal(size), SHORT: Decimal(0)}}


class Lines(unittest.TestCase):
    def test_a_stop_sits_where_equity_reaches_the_nearest_rule(self):
        # Floors: 950 for the drawdown, 970 for the day -> 30 USDC to lose; 0.01 BTC is 600 of
        # notional, so the line is 5% under the mark. The take: 8% of 1000 is 80 -> 13.33% over.
        got = lines(funded(), Decimal(1000), long(BTC, "0.01"), MARKETS)
        self.assertEqual(got, {(BTC, LONG): protect.Line(Decimal("57000"), Decimal("68000"))})
        self.assertEqual(Decimal("0.01") * (60000 - 57000), 30)

    def test_the_drawdown_floor_binds_when_it_is_the_higher_one(self):
        # The day started at 900: its floor is 873, the static one 950 -> 50 to lose, not 27.
        got = lines(funded(day_start=900), Decimal(1000), long(BTC, "0.01"), MARKETS)
        self.assertEqual(got[(BTC, LONG)].stop, Decimal("55000"))
        self.assertEqual(funded(day_start=900).floor(), Decimal(950))
        self.assertEqual(funded(day_start=0).floor(), Decimal(950))
        self.assertEqual(funded(day_start=1010).floor(), Decimal("979.7"))

    def test_a_shorts_stop_and_take_mirror_a_longs(self):
        exp = {BTC: {LONG: Decimal(0), SHORT: Decimal("0.01")}}
        self.assertEqual(lines(funded(), Decimal(1000), exp, MARKETS),
                         {(BTC, SHORT): protect.Line(Decimal("63000"), Decimal("52000"))})

    def test_the_budget_is_shared_in_proportion_to_notional(self):
        exp = {**long(BTC, "0.01"), **long(ETH, "0.1")}  # 600 and 300 of notional, 30 to lose
        got = lines(funded(), Decimal(1000), exp, MARKETS)
        self.assertEqual((got[(BTC, LONG)].stop, got[(ETH, LONG)].stop), (Decimal("58000"), Decimal("2900")))
        loss = Decimal("0.01") * (60000 - got[(BTC, LONG)].stop) + Decimal("0.1") * (3000 - got[(ETH, LONG)].stop)
        self.assertEqual(loss, 30)

    def test_the_challenge_take_clears_the_pass_target_by_what_closing_costs(self):
        # 3 USDC, 8% target -> 3.24; 0.0042 ETH at 2600 is 10.92 of notional. At 3.05 of equity
        # the day's floor 2.91 leaves 0.14 to lose and 0.19 to gain. The room then adds
        # CLOSE_COST_BPS of the notional -- 0.019656 at 18 bps -- because the take is a MARKET
        # trigger and the account has to be at or above the target after paying BOTH fees: the
        # entry's lands after the line is fixed. The stop still rounds towards the mark; the take
        # rounds away from it, so a tick does not eat that allowance.
        markets = {ETH: Market("ETH", Decimal("2600"), 4)}
        got = lines(challenge(), Decimal("3.05"), long(ETH, "0.0042"), markets)[(ETH, LONG)]
        self.assertEqual((got.stop, got.take), (Decimal("2566.7"), Decimal("2650.0")))
        self.assertLessEqual(Decimal("0.0042") * (2600 - got.stop), Decimal("0.14"))
        at_take = Decimal("3.05") + Decimal("0.0042") * (got.take - 2600)
        self.assertGreater(at_take, Decimal("3.24"), "a take at the target would pass nothing")
        allowance = Decimal("10.92") * protect.CLOSE_COST_BPS / Decimal(10_000)
        self.assertLess(at_take - Decimal("3.24") - allowance, protect.tick(Decimal("2650"), 4),
                        "over the target by the allowance and at most a tick more")
        # At the start, flat at 3: 0.09 to lose.
        got = lines(challenge(), Decimal(3), long(ETH, "0.0042"), markets)[(ETH, LONG)]
        self.assertEqual((got.stop, got.take), (Decimal("2578.6"), Decimal("2661.9")))

    def test_the_funded_take_is_one_target_from_the_equity_it_has(self):
        self.assertEqual(funded().gain_room(Decimal(1200)), Decimal(96))
        self.assertEqual(challenge().gain_room(Decimal("3.1")), Decimal("0.14"))

    def test_resting_orders_and_the_order_on_its_way_count_as_filled(self):
        book = Book(Decimal(1000), {BTC: Decimal("0.004")}, {BTC: {LONG: Decimal("0.002"), SHORT: Decimal("0.001")}})
        self.assertEqual(exposure(book), {BTC: {LONG: Decimal("0.006"), SHORT: Decimal("0.001")}})
        self.assertEqual(exposure(book, Extra(BTC, True, Decimal("0.004"))),
                         {BTC: {LONG: Decimal("0.010"), SHORT: Decimal("0.001")}})
        self.assertEqual(exposure(Book(Decimal(1000)), Extra(ETH, False, Decimal("0.1"))),
                         {ETH: {LONG: Decimal(0), SHORT: Decimal("0.1")}})
        short = Book(Decimal(1000), {BTC: Decimal("-0.004")})
        self.assertEqual(exposure(short), {BTC: {LONG: Decimal(0), SHORT: Decimal("0.004")}})
        # The larger side of an asset is what shares the budget: 0.01 of BTC here, not 0.011.
        got = lines(funded(), Decimal(1000), exposure(book, Extra(BTC, True, Decimal("0.004"))), MARKETS)
        self.assertEqual(got[(BTC, LONG)].stop, Decimal("57000"))

    def test_rounding_never_loosens_a_stop_and_costs_a_take_at_most_a_tick(self):
        cases = [(mark, sz) for mark in ("1.23456", "12.3456", "123.456", "1234.56", "12345.6", "123456", "98765.4321")
                 for sz in (0, 2, 4, 5)] + [("0.123456", 0), ("0.123456", 2)]
        for mark, sz_decimals in cases:
            if True:
                for size in ("0.01", "0.37", "3"):
                    m = {BTC: Market("X", Decimal(mark), sz_decimals)}
                    limits, equity = funded(), Decimal(1000)
                    exp = long(BTC, size)
                    notional = Decimal(size) * Decimal(mark)
                    got = lines(limits, equity, exp, m)[(BTC, LONG)]
                    f = min((equity - limits.floor()) / notional, protect.MAX_DISTANCE)
                    g = min(limits.gain_room(equity) / notional, protect.MAX_DISTANCE)
                    self.assertGreaterEqual(got.stop, Decimal(mark) * (1 - f), (mark, sz_decimals, size))
                    # The take now rounds AWAY from the mark, so it may sit one tick beyond its
                    # line -- never nearer, which would cut into the room that pays for closing.
                    self.assertLessEqual(got.take, Decimal(mark) * (1 + g) + protect.tick(Decimal(mark), sz_decimals),
                                         (mark, sz_decimals, size))
                    self.assertGreaterEqual(got.take, Decimal(mark) * (1 + g), (mark, sz_decimals, size))
                    self.assertLess(got.stop, Decimal(mark))
                    self.assertGreater(got.take, Decimal(mark))
                    for px in (got.stop, got.take):
                        self.assertEqual(valid_px(px, sz_decimals, ROUND_CEILING), px, (px, sz_decimals))

    def test_with_nothing_left_the_stop_is_one_tick_from_the_mark(self):
        got = lines(funded(), Decimal(970), long(BTC, "0.01"), MARKETS)[(BTC, LONG)]
        self.assertEqual(got.stop, Decimal("59999"))
        got = lines(funded(), Decimal(900), {BTC: {LONG: Decimal(0), SHORT: Decimal("0.01")}}, MARKETS)[(BTC, SHORT)]
        self.assertEqual(got.stop, Decimal("60001"))

    def test_a_position_too_small_to_use_the_budget_gets_a_stop_half_the_mark_away(self):
        got = lines(funded(), Decimal(1000), long(BTC, "0.0002"), MARKETS)[(BTC, LONG)]
        self.assertEqual((got.stop, got.take), (Decimal("30000"), Decimal("90000")))

    def test_prices_follow_hyperliquids_rules(self):
        self.assertEqual(valid_px(Decimal("2578.5714"), 4, ROUND_CEILING), Decimal("2578.6"))
        self.assertEqual(valid_px(Decimal("123456.7"), 5, ROUND_CEILING), Decimal("123457"))  # whole: always
        self.assertEqual(valid_px(Decimal("0.0123456"), 0, ROUND_CEILING), Decimal("0.012346"))
        self.assertEqual(valid_px(Decimal("150.123"), 2, ROUND_CEILING), Decimal("150.13"))
        self.assertEqual(wire_number(Decimal("57000.0")), "57000")
        self.assertEqual(wire_number(Decimal("0.10")), "0.1")
        self.assertEqual(wire_number(Decimal("1E+2")), "100")


class TakeAndTheTarget(unittest.TestCase):
    """A challenge the take closed has to be able to pass.

    The take is a MARKET trigger. Placed exactly at the target it leaves the account flat a taker
    fee BELOW the target, and `graduate` asks for at least the target with no tolerance
    (`ChallengeAccount.sol:229-230`). Found by the CTO on 1 October 2026 from reading the code and
    measured here: with the demo's pass pool (capital 70, target 25 bps, 99.66 USDC of notional)
    the old line put equity at the take at 70.17493 against a target of 70.175, and the account
    held **70.13** once closed. Retrying did not help either -- the next room was exactly that fee,
    so every attempt landed on `target - fee` again: a fixed point, not a sequence creeping up.

    `gain_room` now reaches past the target by `CLOSE_COST_BPS` of the notional, and the take
    rounds away from the mark so a tick cannot eat it.
    """

    CAPITAL = Decimal(70)
    TARGET_BPS = 25
    MARK = Decimal("83750")
    SIZE = Decimal("0.00119")       # about 99.66 USDC of notional, the client's cap being 100
    TAKER = Decimal("0.00045")      # Hyperliquid's taker fee, a side

    def setUp(self):
        self.limits = RuleLimits(True, 1500, 2000, int(self.CAPITAL * USDC), DAY,
                                 int(self.CAPITAL * USDC), self.TARGET_BPS)
        self.notional = self.SIZE * self.MARK
        self.target = self.CAPITAL * (BPS_I + self.TARGET_BPS) / BPS_I
        self.take = lines(self.limits, self.CAPITAL, long(BTC, self.SIZE),
                          {BTC: Market("BTC", self.MARK, 5)})[(BTC, LONG)].take

    def equity_at(self, px: Decimal) -> Decimal:
        return self.CAPITAL + self.SIZE * (px - self.MARK)

    def flat_after(self, slip_bps: Decimal) -> Decimal:
        """What the account holds, flat, after a taker entry and a take that fired.

        Both fills are `slip_bps` worse than their price: the entry above the mark, the close below
        the trigger. Both fees are taker, and the entry's is in here because the take is placed with
        the order that opens the position -- the fee lands after the line is fixed, and `reconcile`
        does not pull a resting take nearer.
        """
        entry = self.MARK * (1 + slip_bps / BPS_I)
        out = self.take * (1 - slip_bps / BPS_I)
        return (self.CAPITAL + self.SIZE * (out - entry)
                - self.SIZE * entry * self.TAKER - self.SIZE * out * self.TAKER)

    def test_the_take_clears_the_target_by_what_closing_costs(self):
        # Measured against the FEE, not against the constant: the requirement is that the room
        # covers what closing actually costs, and a constant that stopped covering it would make
        # this test red rather than agree with itself.
        over = self.equity_at(self.take) - self.target
        self.assertGreaterEqual(over, self.SIZE * self.take * self.TAKER,
                                f"the room over the target is {over}, the close costs "
                                f"{self.SIZE * self.take * self.TAKER}")

    def test_a_taker_entry_and_a_take_nobody_moved_still_passes(self):
        """The CTO's case, 1 October 2026, and the one the bot's instructions actually walk: a
        limit order into the market (so taker), the take left where the gateway put it, and the
        fill a little worse than the trigger.

        A stop filled 1.1 bps worse than its trigger on 29 September, so that is the number to
        beat; four is twice as bad on both sides at once.
        """
        for slip in (Decimal(0), Decimal("1.1"), Decimal(2), Decimal(4)):
            flat = self.flat_after(slip)
            self.assertGreaterEqual(flat, self.target,
                                    f"{slip} bps of slippage a side leaves {flat} against {self.target}")

    def test_the_allowance_carries_both_sides_not_only_the_close(self):
        # Pinned to its parts, not to 18: if the fee or the slippage is ever read again and found
        # larger, this goes red instead of agreeing with a constant that stopped being true.
        self.assertGreaterEqual(protect.CLOSE_COST_BPS,
                                2 * (protect.TAKER_FEE_BPS + protect.SLIPPAGE_BPS),
                                "the entry's fee is debited after the take is placed, so the room "
                                "has to carry both sides")
        # And the margin left over is a multiple of the slippage, not equal to it.
        margin_bps = (self.flat_after(Decimal(0)) - self.target) / self.notional * BPS_I
        self.assertGreater(margin_bps, 2 * protect.SLIPPAGE_BPS)

    def test_the_line_without_the_allowance_is_the_defect_it_was(self):
        # The arithmetic of the old line, kept so the fix cannot be removed quietly: room was
        # exactly `target - equity`, which puts the take where equity only reaches the target.
        room = self.target - self.CAPITAL
        old_take = self.MARK * (self.notional + room) / self.notional
        flat = self.equity_at(old_take) - self.SIZE * old_take * self.TAKER
        self.assertLess(flat, self.target, "the old line did land under the target")
        self.assertGreater(self.target - flat, Decimal("0.04"))

    def test_ten_basis_points_was_not_enough_either(self):
        # The first fix, measured on 1 October: it covered the close and not the entry, and the
        # margin came to 1.01 bps of the notional -- less than the 1.1 a stop had already slipped.
        room = self.target - self.CAPITAL + self.notional * Decimal(10) / BPS_I
        take10 = protect.valid_px(self.MARK * (self.notional + room) / self.notional, 5, ROUND_CEILING)
        entry_fee, exit_fee = self.notional * self.TAKER, self.SIZE * take10 * self.TAKER
        margin = self.equity_at(take10) - entry_fee - exit_fee - self.target
        self.assertLess(margin / self.notional * BPS_I, Decimal("1.1"),
                        "ten basis points left less margin than one measured bad fill")

    def test_one_attempt_is_enough_now_instead_of_a_fixed_point_below_the_target(self):
        equity, cost = self.CAPITAL, self.SIZE * self.take * self.TAKER
        room = self.limits.gain_room(equity, self.notional)
        self.assertGreaterEqual(equity + room - cost, self.target,
                                "one go reaches the target, so there is nothing to retry")


class Wire(unittest.TestCase):
    def test_orders_are_reduce_only_market_triggers_for_the_whole_position(self):
        self.assertEqual(order_wire(ETH, LONG, "sl", Decimal("2578.6"), 4), {
            "a": 4, "b": False, "p": "2320.7", "s": "0", "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": "2578.6", "tpsl": "sl"}}})
        self.assertEqual(order_wire(ETH, SHORT, "tp", Decimal("2500"), 4)["p"], "2750")  # buying back: up to 10% over
        self.assertEqual(order_wire(ETH, SHORT, "sl", Decimal("2700"), 4)["p"], "2970")
        self.assertIs(order_wire(ETH, SHORT, "sl", Decimal("2700"), 4)["b"], True)
        self.assertEqual(list(order_wire(ETH, LONG, "tp", Decimal("2700"), 4)), ["a", "b", "p", "s", "r", "t"])
        self.assertEqual(list(order_wire(ETH, LONG, "tp", Decimal("2700"), 4)["t"]["trigger"]),
                         ["isMarket", "triggerPx", "tpsl"])
        self.assertEqual(protect.place_action([{}])["grouping"], "positionTpsl")
        self.assertEqual(protect.modify_action(7, {}), {"type": "batchModify", "modifies": [{"oid": 7, "order": {}}]})

    def test_the_book_is_read_from_hyperliquids_answers(self):
        markets = parse_markets([{"universe": [{"name": "SOL", "szDecimals": 2}, {"name": "APT", "szDecimals": 2},
                                                {"name": "ATOM", "szDecimals": 2}, {"name": "BTC", "szDecimals": 5},
                                                {"name": "ETH", "szDecimals": 4}]},
                                 [{"markPx": "150.0"}, {"markPx": None}, {"markPx": "4.1"}, {"markPx": "60000.0"},
                                  {"markPx": "3000.5"}]])
        self.assertEqual(markets[ETH], Market("ETH", Decimal("3000.5"), 4))
        self.assertNotIn(1, markets)
        state = {"marginSummary": {"accountValue": "11.0512"},
                 "assetPositions": [{"position": {"coin": "ETH", "szi": "0.0042"}},
                                    {"position": {"coin": "BTC", "szi": "0.0"}}]}
        orders = [  # as frontendOpenOrders gave them on testnet, 28 Sep 2026
            {"coin": "ETH", "isPositionTpsl": True, "isTrigger": True, "oid": 61270164942, "orderType": "Stop Market",
             "reduceOnly": True, "side": "A", "sz": "0.0", "triggerPx": "2550.1"},
            {"coin": "ETH", "isPositionTpsl": True, "isTrigger": True, "oid": 61270164943,
             "orderType": "Take Profit Market", "reduceOnly": True, "side": "A", "sz": "0.0", "triggerPx": "2707.9"},
            {"coin": "BTC", "isPositionTpsl": False, "isTrigger": False, "oid": 5, "orderType": "Limit",
             "reduceOnly": False, "side": "B", "sz": "0.002", "triggerPx": "0.0"},
            {"coin": "BTC", "isPositionTpsl": False, "isTrigger": False, "oid": 6, "orderType": "Limit",
             "reduceOnly": True, "side": "A", "sz": "0.001", "triggerPx": "0.0"},
            {"coin": "DOGE", "isTrigger": False, "oid": 8, "reduceOnly": False, "side": "B", "sz": "10"},
            # A stop limit may rest unfilled past its trigger: it guards nothing, and nobody can place
            # one through the gateway anyway.
            {"coin": "ETH", "isPositionTpsl": False, "isTrigger": True, "oid": 9, "orderType": "Stop Limit",
             "reduceOnly": True, "side": "A", "sz": "0.0042", "triggerPx": "2560"},
        ]
        book = parse_book(state, orders, markets)
        self.assertEqual(book.equity, Decimal("11.0512"))
        self.assertEqual(book.positions, {ETH: Decimal("0.0042")})
        self.assertEqual(book.opening, {BTC: {LONG: Decimal("0.002"), SHORT: Decimal(0)}})
        self.assertEqual(book.protective, [Protective(61270164942, ETH, LONG, "sl", Decimal("2550.1")),
                                           Protective(61270164943, ETH, LONG, "tp", Decimal("2707.9"))])

    def test_hyperliquids_answer_to_a_stop_or_take_confirms_it(self):
        placed = {"status": "ok", "response": {"type": "order", "data": {"statuses": ["waitingForTrigger",
                                                                                      "waitingForTrigger"]}}}
        moved = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 9}}]}}}
        self.assertEqual(hl.venue_outcome(placed), (None, True))
        self.assertEqual(hl.venue_outcome(moved), (None, True))
        half = {"status": "ok", "response": {"type": "order", "data": {"statuses": ["waitingForTrigger", {}]}}}
        self.assertEqual(hl.venue_outcome(half), (None, False))


def stop_at(oid, asset, side, px):
    return Protective(oid, asset, side, "sl", Decimal(px))


def take_at(oid, asset, side, px):
    return Protective(oid, asset, side, "tp", Decimal(px))


class Reconcile(unittest.TestCase):
    WANT = {(BTC, LONG): protect.Line(Decimal("57000"), Decimal("68000"))}

    def plan(self, positions, protective):
        return reconcile(Book(Decimal(1000), positions, {}, protective), self.WANT, MARKETS)

    def test_a_missing_stop_and_take_are_placed_in_one_action(self):
        plan = self.plan({BTC: Decimal("0.01")}, [])
        self.assertEqual(plan.actions, [protect.place_action([order_wire(BTC, LONG, "sl", Decimal("57000"), 5),
                                                              order_wire(BTC, LONG, "tp", Decimal("68000"), 5)])])
        self.assertEqual(plan.report, [{"asset": BTC, "coin": "BTC", "side": LONG, "stop": "57000", "stopWas": "placed",
                                        "take": "68000", "takeWas": "placed"}])

    def test_a_stop_guarding_a_position_is_never_moved_away_from_the_market(self):
        tight = self.plan({BTC: Decimal("0.01")}, [stop_at(1, BTC, LONG, "58000"), take_at(2, BTC, LONG, "68000")])
        self.assertEqual(tight.actions, [])
        loose = self.plan({BTC: Decimal("0.01")}, [stop_at(1, BTC, LONG, "56000"), take_at(2, BTC, LONG, "68000")])
        self.assertEqual(loose.actions, [protect.modify_action(1, order_wire(BTC, LONG, "sl", Decimal("57000"), 5))])
        # With two, the tighter one is the one that counts.
        two = self.plan({BTC: Decimal("0.01")}, [stop_at(1, BTC, LONG, "50000"), stop_at(3, BTC, LONG, "57500"),
                                                  take_at(2, BTC, LONG, "68000")])
        self.assertEqual(two.actions, [])
        short = reconcile(Book(Decimal(1000), {BTC: Decimal("-0.01")}, {}, [stop_at(1, BTC, SHORT, "62000"),
                                                                           take_at(2, BTC, SHORT, "52000")]),
                          {(BTC, SHORT): protect.Line(Decimal("63000"), Decimal("52000"))}, MARKETS)
        self.assertEqual(short.actions, [])

    def test_a_stop_guarding_nothing_yet_follows_the_line_both_ways(self):
        plan = self.plan({}, [stop_at(1, BTC, LONG, "58000"), take_at(2, BTC, LONG, "65000")])
        self.assertEqual(plan.actions, [protect.modify_action(1, order_wire(BTC, LONG, "sl", Decimal("57000"), 5)),
                                        protect.modify_action(2, order_wire(BTC, LONG, "tp", Decimal("68000"), 5))])
        self.assertEqual(self.plan({}, [stop_at(1, BTC, LONG, "57000"), take_at(2, BTC, LONG, "68000")]).actions, [])

    def test_a_take_within_the_target_stays_where_it_is(self):
        near = self.plan({BTC: Decimal("0.01")}, [stop_at(1, BTC, LONG, "57000"), take_at(2, BTC, LONG, "61000")])
        self.assertEqual(near.actions, [])
        far = self.plan({BTC: Decimal("0.01")}, [stop_at(1, BTC, LONG, "57000"), take_at(2, BTC, LONG, "69000")])
        self.assertEqual(far.actions, [protect.modify_action(2, order_wire(BTC, LONG, "tp", Decimal("68000"), 5))])

    def test_each_direction_goes_in_an_action_of_its_own(self):
        # Hyperliquid refuses, as a whole, a position TP/SL action that mixes the sides (28 Sep 2026).
        want = {(BTC, LONG): protect.Line(Decimal("57000"), Decimal("68000")),
                (BTC, SHORT): protect.Line(Decimal("63000"), Decimal("52000"))}
        plan = reconcile(Book(Decimal(1000), {BTC: Decimal("0.01")}, {}, []), want, MARKETS)
        self.assertEqual(len(plan.actions), 2)
        for action in plan.actions:
            self.assertEqual(len({(o["a"], o["b"]) for o in action["orders"]}), 1, action)
            check_caps("protect", action, lambda a: Decimal(1))

    def test_orders_for_the_other_direction_are_not_this_ones(self):
        plan = self.plan({BTC: Decimal("0.01")}, [stop_at(1, BTC, SHORT, "58000"), take_at(2, BTC, SHORT, "59000")])
        self.assertEqual(plan.report[0]["stopWas"], "placed")


# ── the gateway with it ──────────────────────────────────────────────────────────────────

class Exchange:
    """Hyperliquid for one account: its book, and what happens to the actions sent to it."""

    def __init__(self, equity="1000", positions=None, markets=None):
        self.equity = Decimal(equity)
        self.positions = dict(positions or {})
        self.markets_now = dict(markets or MARKETS)
        self.orders: list[dict] = []  # frontendOpenOrders
        self.sent: list[dict] = []
        self.refuse = None  # an answer for the gateway's own actions
        self.next_oid = 1000
        self.market_reads = 0
        self.rest_trader_orders = False  # a trader's order joins the book when it is submitted
        self.during_trader_submit = None  # runs while a trader's order is on its way

    # Venue
    def markets(self):
        self.market_reads += 1
        return dict(self.markets_now)

    def book(self, account, markets):
        coin = {i: m.coin for i, m in markets.items()}
        state = {"marginSummary": {"accountValue": str(self.equity)},
                 "assetPositions": [{"position": {"coin": coin[a], "szi": str(q)}} for a, q in self.positions.items()]}
        return parse_book(state, self.orders, markets)

    def _oid(self):
        self.next_oid += 1
        return self.next_oid

    def _protective(self, wire, oid):
        trig = wire["t"]["trigger"]
        return {"coin": self.markets_now[wire["a"]].coin, "isPositionTpsl": True, "isTrigger": True, "oid": oid,
                "orderType": "Stop Market" if trig["tpsl"] == "sl" else "Take Profit Market", "reduceOnly": True,
                "side": "B" if wire["b"] else "A", "sz": "0.0", "triggerPx": trig["triggerPx"]}

    # submit
    def submit(self, action, nonce, signature):
        self.sent.append(action)
        own = action.get("grouping") == "positionTpsl" or action["type"] == "batchModify"
        if own and self.refuse is not None:
            return self.refuse
        if action["type"] == "batchModify":
            m = action["modifies"][0]
            self.orders = [o for o in self.orders if o["oid"] != m["oid"]]
            oid = self._oid()
            self.orders.append(self._protective(m["order"], oid))
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}
        if action["type"] == "cancel":
            oid = action["cancels"][0]["o"]
            self.orders = [o for o in self.orders if o["oid"] != oid]
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        if own:
            for wire in action["orders"]:
                self.orders.append(self._protective(wire, self._oid()))
            return {"status": "ok", "response": {"type": "order",
                                                 "data": {"statuses": ["waitingForTrigger"] * len(action["orders"])}}}
        if self.during_trader_submit is not None:
            self.during_trader_submit()
        oid = self._oid()
        if self.rest_trader_orders:
            o = action["orders"][0]
            self.orders.append(resting(self.markets_now[o["a"]].coin, "B" if o["b"] else "A", o["s"], oid))
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def own_actions(self):
        return [a for a in self.sent if a.get("grouping") == "positionTpsl" or a["type"] == "batchModify"]

    def trader_actions(self):
        return [a for a in self.sent if a not in self.own_actions()]


def resting(coin, side, sz, oid):
    """A trader's limit order resting on the book, as frontendOpenOrders lists it."""
    return {"coin": coin, "isPositionTpsl": False, "isTrigger": False, "oid": oid, "orderType": "Limit",
            "reduceOnly": False, "side": side, "sz": sz, "triggerPx": "0.0"}


class Reader(FakeReader):
    def __init__(self, key, trader, limits):
        super().__init__(key, trader, assets={SOL, BTC, ETH})
        self.limits, self.full_reads, self.snapshot_reads = limits, 0, 0

    def rule_limits(self, account):
        self.full_reads += 1
        return self.limits

    def day_snapshot(self, account):
        self.snapshot_reads += 1
        return self.limits.day, self.limits.day_start_equity


class FlowBase(unittest.TestCase):
    def setUp(self):
        self.trader = Account.create(os.urandom(32))
        self.key = Account.create(os.urandom(32))
        self.reader = Reader(self.key.address, self.trader.address, funded())
        self.x = Exchange()
        self.now = NOW / 1000
        self.gw = Gateway(self.reader, DemoSigner([self.key], mid=lambda a: MARKETS[a].mark), submit=self.x.submit,
                          clock=lambda: self.now, venue=self.x)

    def send(self, kind="order", **over):
        base = {"account": ACCOUNT, "asset": BTC, "nonce": NOW, "expiresAt": NOW + 30_000}
        if kind == "order":
            base.update(isBuy=True, limitPx="60000", size="0.005", reduceOnly=False, tif="Gtc")
        elif kind == "cancel":
            base.update(oid=42)
        else:
            base.update(triggerPx="58000")
        base.update(over)
        fields = {k: base[k] for k in [f["name"] for f in auth.TYPES[kind][1]]}
        return self.gw.handle_order({"kind": kind, kind: fields, "signature": auth.sign(self.trader, kind, fields)})


class Flow(FlowBase):
    def test_the_stop_and_take_are_on_the_book_before_the_order_goes(self):
        status, out = self.send()
        self.assertEqual((status, out["status"]), (200, "submitted"))
        # 0.005 BTC is 300 of notional against 30 to lose: 10% under the mark; 80 to gain: 26.67% over.
        self.assertEqual(self.x.sent, [
            protect.place_action([order_wire(BTC, LONG, "sl", Decimal("54000"), 5),
                                  order_wire(BTC, LONG, "tp", Decimal("76000"), 5)]),
            {"type": "order", "grouping": "na",
             "orders": [{"a": BTC, "b": True, "p": "60000", "s": "0.005", "r": False, "t": {"limit": {"tif": "Gtc"}}}]}])
        self.assertEqual(out["protection"], [{"asset": BTC, "coin": "BTC", "side": LONG, "stop": "54000",
                                              "stopWas": "placed", "take": "76000", "takeWas": "placed"}])
        self.assertIn(ACCOUNT, self.gw.protector.watching())

    def test_an_order_is_not_sent_without_its_stop_and_can_be_sent_again(self):
        # Refused, or answered with nothing that confirms it: either way the order stays home.
        for answer in ({"status": "err", "response": "Insufficient margin"}, {"status": "ok"}):
            self.x.refuse = answer
            status, out = self.send()
            self.assertEqual((status, out["status"], out["code"]), (502, "refused_by_gateway", "protection_failed"))
            self.assertEqual(self.x.trader_actions(), [])
        self.x.refuse = None
        status, out = self.send()  # the same signed request: its nonce came back
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertEqual(len(self.x.trader_actions()), 1)

    def test_an_order_is_not_sent_when_the_signer_refuses_its_stop(self):
        self.x.markets_now[7] = Market("DOGE", Decimal("0.2"), 0)
        self.x.positions[7] = Decimal("100")  # an asset off the platform list: the demo signer won't touch it
        status, out = self.send()
        self.assertEqual((status, out["code"]), (502, "protection_failed"))
        self.assertIn("policy", out["detail"])
        self.assertEqual(self.x.trader_actions(), [])

    def test_a_stop_signed_by_another_key_is_never_sent(self):
        stranger = Account.create(os.urandom(32))
        demo = self.gw.signer

        class Swapping:
            """Signs the trader's orders with the account's key and the gateway's own with another."""

            def has_key(self, key):
                return demo.has_key(key)

            def sign(self, key, kind, action, nonce):
                if kind == "protect":
                    return DemoSigner([stranger]).sign(stranger.address, kind, action, nonce)
                return demo.sign(key, kind, action, nonce)

        self.gw.signer = Swapping()
        status, out = self.send()
        self.assertEqual((status, out["code"]), (502, "protection_failed"))
        self.assertIn("another key", out["detail"])
        self.assertEqual(self.x.sent, [])

    def test_nothing_opens_at_the_rule_line(self):
        self.x.equity = Decimal(970)
        status, out = self.send()
        self.assertEqual((status, out["code"]), (409, "at_rule_line"))
        self.assertEqual(self.x.sent, [])

    def test_a_challenge_that_met_its_target_opens_nothing_more(self):
        self.reader.limits = challenge()
        self.x.equity = Decimal("3.24")
        status, out = self.send(size="0.0002")
        self.assertEqual((status, out["code"]), (409, "target_met"))
        self.assertEqual(self.x.sent, [])

    def test_reduce_only_orders_need_no_stop_of_their_own(self):
        self.x.positions[BTC] = Decimal("0.005")
        status, out = self.send(isBuy=False, reduceOnly=True, tif="Ioc", limitPx="59000")
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertEqual(self.x.own_actions(), [])
        self.assertNotIn("protection", out)

    def test_the_trader_can_not_cancel_the_stop_or_the_take(self):
        self.send()
        for o in self.x.orders:
            status, out = self.send("cancel", oid=o["oid"], nonce=NOW + o["oid"])
            self.assertEqual((status, out["code"]), (403, "protective_order"))
        self.assertEqual([a["type"] for a in self.x.sent].count("cancel"), 0)
        status, out = self.send("cancel", oid=42, nonce=NOW + 1)  # an ordinary order
        self.assertEqual((status, out["status"]), (200, "submitted"))

    def open_long(self):
        self.send()
        self.x.positions[BTC] = Decimal("0.005")

    def test_a_stop_moves_only_nearer_the_mark(self):
        self.open_long()  # stop 54000, take 76000
        status, out = self.send("stop", triggerPx="53000", nonce=NOW + 1)
        self.assertEqual((status, out["code"]), (403, "stop_looser"))
        status, out = self.send("stop", triggerPx="60000", nonce=NOW + 2)
        self.assertEqual((status, out["code"]), (403, "stop_past_mark"))
        status, out = self.send("stop", triggerPx="58000", nonce=NOW + 3)
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertEqual(self.x.sent[-1]["type"], "batchModify")
        self.assertEqual(self.x.sent[-1]["modifies"][0]["order"], order_wire(BTC, LONG, "sl", Decimal("58000"), 5))
        stops = [o["triggerPx"] for o in self.x.orders if o["orderType"] == "Stop Market"]
        self.assertEqual(stops, ["58000"])
        status, out = self.send("stop", triggerPx="57000", nonce=NOW + 4)  # looser than where it now is
        self.assertEqual((status, out["code"]), (403, "stop_looser"))

    def test_a_take_moves_anywhere_within_the_target(self):
        self.open_long()
        for px, nonce in (("70000", 1), ("75000", 2), ("76000", 3)):
            status, out = self.send("take", triggerPx=px, nonce=NOW + nonce)
            self.assertEqual((status, out["status"]), (200, "submitted"), px)
        status, out = self.send("take", triggerPx="76001", nonce=NOW + 4)
        self.assertEqual((status, out["code"]), (403, "take_beyond_target"))
        status, out = self.send("take", triggerPx="59000", nonce=NOW + 5)
        self.assertEqual((status, out["code"]), (403, "take_past_mark"))
        self.assertEqual([o["triggerPx"] for o in self.x.orders if o["orderType"].startswith("Take")], ["76000"])

    def test_moving_needs_a_position(self):
        status, out = self.send("stop", triggerPx="58000")
        self.assertEqual((status, out["code"]), (409, "no_position"))
        status, out = self.send("take", triggerPx="61000", nonce=NOW + 1)
        self.assertEqual((status, out["code"]), (409, "no_position"))

    def test_a_stop_or_take_signature_is_not_the_other_one(self):
        fields = {"account": ACCOUNT, "asset": BTC, "triggerPx": "58000", "nonce": NOW, "expiresAt": NOW + 30_000}
        signed = auth.sign(self.trader, "take", fields)
        self.assertNotEqual(auth.recover_trader("stop", fields, signed).lower(), self.trader.address.lower())

    def test_the_gateways_own_nonces_never_repeat_and_skip_the_traders(self):
        first = self.gw._next_nonce(None)
        self.assertEqual(first, NOW)
        self.assertEqual(self.gw._next_nonce(NOW + 1), NOW + 2)
        self.assertEqual(self.gw._next_nonce(None), NOW + 3)

    def test_the_protective_actions_use_their_own_nonce(self):
        seen = []
        submit = self.x.submit
        self.gw.submit = lambda action, nonce, sig: (seen.append(nonce), submit(action, nonce, sig))[1]
        self.send()
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(seen[1], NOW)  # the trader's order goes with the trader's nonce


class OneAtATime(FlowBase):
    """From the stop and take placed for an order to Hyperliquid's answer, the book doesn't show the
    order; nothing else may act on the account in between."""

    def stops(self):
        return [o["triggerPx"] for o in self.x.orders if o["orderType"] == "Stop Market"]

    def test_a_sweep_waits_until_the_order_has_reached_hyperliquid(self):
        # 0.002 BTC already rests; the order on its way adds 0.003, so the stop is placed for 0.005.
        self.x.orders.append(resting("BTC", "B", "0.002", 555))
        self.x.rest_trader_orders = True
        self.gw.protector.watch(ACCOUNT, self.key.address)
        seen = {}

        def meanwhile():
            sweep = threading.Thread(target=self.gw.protector.sweep)
            sweep.start()
            sweep.join(0.3)
            seen.update(blocked=sweep.is_alive(), sweep=sweep)

        self.x.during_trader_submit = meanwhile
        status, out = self.send(size="0.003")
        seen["sweep"].join(5)
        self.assertEqual(status, 200)
        self.assertTrue(seen["blocked"])
        # The line for 0.005; a sweep let in early would have seen only the 0.002 and moved the stop
        # out to that looser line (45000).
        self.assertEqual(self.stops(), ["54000"])

    def test_two_orders_for_one_account_are_protected_one_after_the_other(self):
        self.x.rest_trader_orders = True
        second = {}

        def meanwhile():
            if "thread" in second:
                return
            before = len(self.x.sent)
            thread = threading.Thread(target=lambda: second.update(answer=self.send(nonce=NOW + 1)))
            second["thread"] = thread
            thread.start()
            thread.join(0.3)
            second["blocked"] = thread.is_alive() and len(self.x.sent) == before

        self.x.during_trader_submit = meanwhile
        status, _ = self.send()
        second["thread"].join(5)
        self.assertEqual((status, second["answer"][0]), (200, 200))
        self.assertTrue(second["blocked"])
        # Both 0.005 buys: 0.01 BTC is 600 of notional against 30 to lose, a stop 5% under the mark.
        self.assertEqual(self.stops(), ["57000"])


class Sweep(FlowBase):
    def test_a_position_that_opened_later_gets_its_stop(self):
        self.send()
        self.x.orders.clear()  # the stop fired and Hyperliquid took both away...
        self.x.positions[BTC] = Decimal("0.005")  # ...then a resting order filled
        self.gw.protector.sweep()
        self.assertEqual(self.x.own_actions()[-1], protect.place_action(
            [order_wire(BTC, LONG, "sl", Decimal("54000"), 5), order_wire(BTC, LONG, "tp", Decimal("76000"), 5)]))

    def test_nothing_is_sent_while_all_is_in_place(self):
        self.send()
        before = len(self.x.sent)
        self.x.positions[BTC] = Decimal("0.005")
        self.gw.protector.sweep()
        self.assertEqual(len(self.x.sent), before)

    def test_an_account_the_key_no_longer_trades_is_let_go(self):
        self.send()
        self.x.orders.clear()
        self.x.positions[BTC] = Decimal("0.005")
        self.reader.trading = False  # stopped: the key was cut
        self.gw.protector.sweep()
        self.assertEqual(len(self.x.own_actions()), 1)
        self.assertNotIn(ACCOUNT, self.gw.protector.watching())

    def test_an_account_with_nothing_open_is_let_go(self):
        self.send()
        self.x.orders.clear()
        self.gw.protector.sweep()
        self.assertNotIn(ACCOUNT, self.gw.protector.watching())

    def test_a_new_days_snapshot_is_read_and_tightens_the_stop(self):
        self.send()  # stop at 54000: 30 to lose from 1000
        self.x.positions[BTC] = Decimal("0.005")
        # The next UTC day was snapshotted at 1020 and equity is back at 1000: the day's floor is
        # 989.4 now, so 10.6 is left to lose, and the stop moves up to meet it.
        self.now = (DAY + 1) * 86400 + 120
        self.reader.limits = funded(day_start=1020, day=DAY + 1)
        self.gw.protector.sweep()
        self.assertEqual(self.reader.snapshot_reads, 1)
        stops = [o["triggerPx"] for o in self.x.orders if o["orderType"] == "Stop Market"]
        self.assertEqual(stops, ["57880"])  # 60000 * (1 - 10.6 / 300)
        self.gw.protector.sweep()
        self.assertEqual(self.reader.snapshot_reads, 1)  # today's snapshot is in hand

    def test_the_rules_are_read_once_per_key_and_an_old_snapshot_at_most_once_a_minute(self):
        self.reader.limits = funded(day=DAY - 1)
        self.send()
        self.send(nonce=NOW + 1)
        self.assertEqual((self.reader.full_reads, self.reader.snapshot_reads), (1, 0))
        self.now += 61
        self.send(nonce=NOW + 2, expiresAt=NOW + 90_000)
        self.assertEqual((self.reader.full_reads, self.reader.snapshot_reads), (1, 1))

    def test_one_account_failing_does_not_stop_the_sweep(self):
        other = "0x00000000000000000000000000000000000000B7"
        self.gw.protector.watch(other, self.key.address)
        self.send()
        self.x.orders.clear()
        self.x.positions[BTC] = Decimal("0.005")
        book = self.x.book

        def only_ours(account, markets):
            if account == other:
                raise ConnectionError("info API unreachable")
            return book(account, markets)

        self.x.book = only_ours
        self.gw.protector.sweep()
        self.assertEqual(len(self.x.own_actions()), 2)


class DemoSignerPolicy(unittest.TestCase):
    SL = order_wire(BTC, LONG, "sl", Decimal("54000"), 5)
    TP = order_wire(BTC, LONG, "tp", Decimal("76000"), 5)

    def refused(self, action):
        with self.assertRaises(GatewayError) as ctx:
            check_caps("protect", action, lambda a: Decimal(1))
        return ctx.exception.code

    def test_the_demo_signer_signs_the_stop_and_take_and_nothing_wider(self):
        check_caps("protect", protect.place_action([self.SL, self.TP]), lambda a: Decimal(1))
        check_caps("protect", protect.modify_action(5, self.SL), lambda a: Decimal(1))
        grown = dict(self.SL, s="0.1")
        opening = dict(self.SL, r=False)
        limit = dict(self.SL, t={"limit": {"tif": "Gtc"}})
        limit_trigger = dict(self.SL, t={"trigger": {"isMarket": False, "triggerPx": "54000", "tpsl": "sl"}})
        off_list = dict(self.SL, a=7)
        extra = dict(self.SL, c="0x" + "00" * 16)
        for action in (
            {"type": "order", "orders": [self.SL], "grouping": "na"},
            {"type": "order", "orders": [self.SL, self.TP, self.SL], "grouping": "positionTpsl"},
            {"type": "order", "orders": [], "grouping": "positionTpsl"},
            protect.place_action([grown]), protect.place_action([opening]), protect.place_action([limit]),
            protect.place_action([limit_trigger]), protect.place_action([off_list]), protect.place_action([extra]),
            {"type": "batchModify", "modifies": [{"oid": 5, "order": self.SL}, {"oid": 6, "order": self.TP}]},
            {"type": "batchModify", "modifies": [{"oid": True, "order": self.SL}]},
            protect.modify_action(5, grown),
            {"type": "cancel", "cancels": [{"a": BTC, "o": 5}]},
            {"type": "order", "orders": [self.SL], "grouping": "positionTpsl", "builder": {"b": "0x0", "f": 1}},
            protect.place_action([self.SL, order_wire(BTC, SHORT, "sl", Decimal("66000"), 5)]),
            protect.place_action([self.SL, order_wire(ETH, LONG, "tp", Decimal("3300"), 4)]),
        ):
            self.assertEqual(self.refused(action), "policy", action)

    def test_a_trader_kind_is_not_a_way_in(self):
        with self.assertRaises(GatewayError):
            check_caps("order", protect.place_action([self.SL]), lambda a: Decimal(1))
        with self.assertRaises(GatewayError):
            check_caps("stop", protect.place_action([self.SL]), lambda a: Decimal(1))


class Chain(unittest.TestCase):
    def test_the_rule_and_term_tuples_are_the_contracts(self):
        source = (ROOT / "src" / "Types.sol").read_text()

        def fields(struct):
            body = re.search(rf"struct {struct} \{{(.*?)\}}", source, re.S).group(1)
            return [m.group(1) for m in re.finditer(r"^\s*(u?int\d+(?:\[\])?) \w+;", body, re.M)]

        self.assertEqual(chain.RULES, "(" + ",".join(fields("Rules")) + ")")
        self.assertEqual(chain.TERMS, "(" + ",".join(fields("Terms")) + ")")
        self.assertEqual(fields("Terms")[2], "uint16")  # targetBps, the third

    def test_rule_limits_reads_the_contract(self):
        r = chain.JsonRpcReader("http://rpc.invalid", "0x" + "f1" * 20, "0x" + "f2" * 20)
        answers = {"isChallenge(address)": (True,), "rules()": ((300, 600, 500, [0, 3, 4]),),
                   "drawdownBase()": (3_000_000,), "day()": (DAY,), "dayStartEquity()": (3_050_000,),
                   "terms()": ((1_500_000, 3_000_000, 800, 86400, 0, 8000, 30_000_000),)}
        r._call = lambda to, sig, types, args, out: answers[sig]
        self.assertEqual(r.rule_limits(ACCOUNT), RuleLimits(True, 300, 600, 3_000_000, DAY, 3_050_000, 800))
        self.assertEqual(r.day_snapshot(ACCOUNT), (DAY, 3_050_000))


class HostMinute(FlowBase):
    """What the gateway spends of Hyperliquid's 1,200 a minute per IP, which the keeper on the same host
    shares, measured on the fake venue with Hyperliquid's weights (rate-limits-and-user-limits:
    clearinghouseState and allMids 2, an exchange action 1 per 40 orders in it, every other info read 20)."""

    def setUp(self):
        super().setUp()
        self.spent = 0
        x = self.x
        markets, book, submit = x.markets, x.book, x.submit

        def weighed_markets():
            self.spent += 20  # metaAndAssetCtxs
            return markets()

        def weighed_book(account, m):
            self.spent += 2 + 20  # clearinghouseState, frontendOpenOrders
            return book(account, m)

        def weighed_submit(action, nonce, signature):
            self.spent += 1 + len(action.get("orders", action.get("modifies", action.get("cancels", [])))) // 40
            return submit(action, nonce, signature)

        def weighed_mid(asset):
            self.spent += 2  # the demo signer's allMids
            return MARKETS[asset].mark

        x.markets, x.book, x.submit = weighed_markets, weighed_book, weighed_submit
        self.gw = Gateway(self.reader, DemoSigner([self.key], mid=weighed_mid), submit=weighed_submit,
                          clock=lambda: self.now, venue=x)

    def test_at_the_default_two_accounts_trading_at_nginxs_rate_fit_in_the_hosts_minute(self):
        # A sell costs the most: the demo signer reads the mids only to cap a sell (gateway/demo_signer.py).
        status, _ = self.send()
        self.assertEqual(status, 200)
        buy, self.spent = self.spent, 0
        status, _ = self.send(isBuy=False, limitPx="50000", nonce=NOW + 1)
        self.assertEqual(status, 200)
        per_order, self.spent = self.spent, 0
        self.assertEqual((buy, per_order), (44, 46))
        # A sweep with the one account the orders went to, once it has nothing left to place; then one more.
        self.x.positions[BTC] = Decimal("0.005")
        self.gw.protector.sweep()
        self.spent = 0
        self.gw.protector.sweep()
        one, self.spent = self.spent, 0
        self.gw.protector.watch("0x" + "01" * 20, self.key.address)
        self.gw.protector.sweep()
        per_account = self.spent - one
        self.assertEqual((one - per_account, per_account), (20, 22))

        template = (pathlib.Path(__file__).resolve().parents[2] / "ops" / "host" / "nginx-pools-api.conf.in").read_text()
        orders = int(re.search(r"zone=pools_all:\S+ rate=(\d+)r/m;", template).group(1))
        self.assertEqual(orders, 15)

        def minute(n, every=server.PROTECT_EVERY_S, with_orders=True):
            keeper = 2 * (20 + 22 * n)  # ops/keeper.py: a pass every 30 seconds, one read of each account
            return keeper + 60 / every * (20 + 22 * n) + (orders * per_order if with_orders else 0)

        self.assertEqual(minute(2), 1074)
        self.assertGreater(minute(3), 1200)
        self.assertEqual(minute(2, every=10), 1202)
        self.assertEqual(max(n for n in range(1, 50) if minute(n, with_orders=False) <= 1200), 8)
        self.assertEqual(max(n for n in range(1, 50) if minute(n, every=10, with_orders=False) <= 1200), 5)
        doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "GATEWAY.md").read_text()
        self.assertIn("| two accounts | 1,074 | 1,202 |", doc)
        self.assertIn("| most accounts under 1,200, with orders | 2 (three are 1,206) | 1 |", doc)
        self.assertIn("| most accounts under 1,200, no orders | 8 (nine are 1,308) | 5 (six are 1,216) |", doc)
        self.assertIn("(default 15)", doc)


class Wiring(unittest.TestCase):
    def test_the_gateway_protects_on_the_real_venue_unless_told_otherwise(self):
        gw = Gateway(FakeReader("0x" + "00" * 20, "0x" + "00" * 20), object())
        self.assertIsInstance(gw.protector, Protector)
        self.assertIsInstance(gw.protector.venue, hl.Venue)

    def test_main_starts_the_sweep(self):
        body = inspect.getsource(server.main)
        self.assertIn("target=gateway.protector.run", body)
        self.assertIn("make_handler(gateway,", body)
        self.assertEqual(server.PROTECT_EVERY_S, 15.0)


if __name__ == "__main__":
    unittest.main()
