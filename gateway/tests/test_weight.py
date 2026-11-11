"""What the gateway spends of Hyperliquid's minute, and how it spends less: the meter (gateway/weight.py),
the venue that reads the open orders of an account holding a position every time and those of one
holding none only when they may have changed (hl.Venue), and the sweep that comes back every 15 seconds
only to the accounts where a position can stand without a stop.

    spike/.venv/bin/python -m unittest gateway.tests.test_weight
"""

from __future__ import annotations

import http.client
import inspect
import threading
import unittest
from decimal import Decimal
from unittest import mock

from gateway import hl, server, weight
from gateway.protect import LONG, Protective
from gateway.tests.test_gateway import ACCOUNT, NOW
from gateway.tests.test_protect import BTC, Exchange, FlowBase, resting


class Meter(unittest.TestCase):
    def test_each_request_weighs_what_hyperliquid_publishes(self):
        self.assertEqual([weight.info_weight({"type": t}) for t in
                          ("clearinghouseState", "allMids", "l2Book", "orderStatus", "spotClearinghouseState",
                           "exchangeStatus", "userRole", "frontendOpenOrders", "metaAndAssetCtxs", "meta",
                           "perpDexs", "extraAgents")],
                         [2, 2, 2, 2, 2, 2, 60, 20, 20, 20, 20, 20])
        self.assertEqual(weight.exchange_weight({"type": "order", "orders": [{}] * 39}), 1)
        self.assertEqual(weight.exchange_weight({"type": "order", "orders": [{}] * 40}), 2)
        self.assertEqual(weight.exchange_weight({"type": "cancel", "cancels": [{}] * 79}), 2)
        self.assertEqual(weight.exchange_weight({"type": "batchModify", "modifies": [{}]}), 1)

    def test_the_minute_is_the_last_sixty_seconds_by_type(self):
        now = [1000.0]
        m = weight.Meter(clock=lambda: now[0])
        m.info({"type": "clearinghouseState"})
        m.info({"type": "frontendOpenOrders"})
        now[0] += 30
        m.info({"type": "clearinghouseState"})
        m.exchange({"type": "order", "orders": [{}]})
        self.assertEqual(m.per_minute(), {"total": 25, "by_type": {"clearinghouseState": 4, "exchange": 1,
                                                                    "frontendOpenOrders": 20}})
        now[0] += 30  # the first two are a minute old now
        self.assertEqual(m.per_minute(), {"total": 3, "by_type": {"clearinghouseState": 2, "exchange": 1}})
        # A meter nobody asks keeps one minute alone: what is older goes as the next request is added.
        now[0] += 120
        m.info({"type": "allMids"})
        self.assertEqual(len(m._sent), 1)

    def test_every_read_and_every_action_of_the_gateway_is_counted_as_sent(self):
        m = weight.Meter()

        class Answer:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                pass

            def json(self):
                return self.body

        with mock.patch.object(hl, "METER", m), \
                mock.patch.object(hl.requests, "post", side_effect=lambda *a, **k: Answer({"status": "ok"})):
            hl._info({"type": "clearinghouseState", "user": ACCOUNT}, 1)
            hl._info({"type": "metaAndAssetCtxs"}, 1)
            hl.submit({"type": "order", "orders": [{}]}, 1, {})
        self.assertEqual(m.per_minute()["by_type"], {"clearinghouseState": 2, "exchange": 1, "metaAndAssetCtxs": 20})
        # A refused read is counted too: Hyperliquid counts it either way.
        with mock.patch.object(hl, "METER", m), mock.patch.object(hl.requests, "post", side_effect=OSError("refused")):
            with self.assertRaises(OSError):
                hl._info({"type": "frontendOpenOrders", "user": ACCOUNT}, 1)
        self.assertEqual(m.per_minute()["by_type"]["frontendOpenOrders"], 20)


def state(szi: str) -> dict:
    positions = [] if szi == "0" else [{"position": {"coin": "BTC", "szi": szi, "positionValue": "300"}}]
    return {"marginSummary": {"accountValue": "1000", "totalNtlPos": "300"}, "assetPositions": positions}


STOP = {"coin": "BTC", "side": "A", "isTrigger": True, "reduceOnly": True, "orderType": "Stop Market",
        "oid": 7, "triggerPx": "58000", "sz": "0.005"}


class OrdersReadOnChange(unittest.TestCase):
    """hl.Venue: the positions every read; the orders every read where the account holds a position, and
    where it holds none only when they may have changed."""

    def setUp(self):
        self.now = 0.0
        self.venue = hl.Venue(clock=lambda: self.now)
        # No position, an order resting that may open one: the account whose orders list may be kept.
        self.szi = "0"
        self.orders = [resting("BTC", "B", "0.005", 8)]
        self.bad = None  # an orders answer that cannot be read, in place of the list
        self.asked: list[str] = []
        markets = {BTC: hl.Market("BTC", Decimal("60000"), 5)}
        self.markets = markets

        def info(body, timeout):
            self.asked.append(body["type"])
            if body["type"] == "clearinghouseState":
                return state(self.szi)
            if isinstance(self.bad, Exception):
                raise self.bad
            return list(self.orders) if self.bad is None else self.bad

        patcher = mock.patch.object(hl, "_info", side_effect=info)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, reuse=True):
        self.asked.clear()
        book = self.venue.book(ACCOUNT, self.markets, reuse_orders=reuse)
        return list(self.asked), book

    def test_with_no_position_and_nothing_changed_the_last_orders_stand_and_the_read_costs_2(self):
        self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"])
        asked, book = self.read()
        self.assertEqual(asked, ["clearinghouseState"])
        self.assertEqual(book.resting, [(BTC, 8)], "the last list, whole")

    def test_an_account_holding_a_position_has_its_orders_read_every_time(self):
        self.szi, self.orders = "0.005", [STOP]
        for _ in range(3):
            asked, book = self.read()
            self.assertEqual(asked, ["clearinghouseState", "frontendOpenOrders"])
        self.assertEqual(book.protective, [Protective(7, BTC, LONG, "sl", Decimal("58000"))])

    def test_a_stop_gone_with_the_position_as_it_was_is_seen_at_the_next_read(self):
        # A crash: the stop-market fired, nothing filled it, and the exchange took it off. The position
        # is the same size it was, so only the orders read now say there is no stop.
        self.szi, self.orders = "0.005", [STOP]
        self.assertEqual(len(self.read()[1].protective), 1)
        self.orders = []
        asked, book = self.read()
        self.assertEqual((asked, book.protective), (["clearinghouseState", "frontendOpenOrders"], []))

    def test_a_position_that_changed_has_its_orders_read_again(self):
        self.read()
        self.szi = "0.010"  # the order that rested filled
        self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"])
        self.szi = "0"  # its stop fired: no position again, and the list read with one is not kept
        self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"])
        self.assertEqual(self.read()[0], ["clearinghouseState"])

    def test_orders_a_minute_old_are_read_again(self):
        self.read()
        self.now += hl.ORDERS_MAX_AGE_S - 1
        self.assertEqual(self.read()[0], ["clearinghouseState"])
        self.now += 1
        self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"])

    def test_after_anything_sent_for_the_account_its_orders_are_read_again(self):
        self.read()
        self.venue.forget(ACCOUNT.upper().replace("0X", "0x"))  # any case of the address
        self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"])

    def test_an_answer_that_cannot_be_read_whole_has_the_orders_read_every_time(self):
        for unreadable in ("not a size", None):
            self.szi = unreadable
            self.venue.forget(ACCOUNT)
            for _ in range(2):
                with mock.patch.object(hl, "parse_book", return_value=hl.Book(equity=Decimal(0))):
                    self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"], unreadable)
        self.assertIsNot(hl.positions_of({"assetPositions": ["?"]}), hl.positions_of({"assetPositions": ["?"]}))
        self.assertNotEqual(hl.positions_of({"assetPositions": ["?"]}), hl.positions_of({"assetPositions": ["?"]}))
        self.assertEqual(hl.positions_of(state("0")), ())

    def test_an_orders_answer_that_cannot_be_read_is_not_kept_and_drops_the_list_before_it(self):
        self.read()  # a list kept: no position, an order resting
        # Not a list, a list the book can't be read from, a read that failed.
        for bad in ({}, "busy", [{"coin": "BTC"}], RuntimeError("timed out")):
            self.bad = bad
            with self.assertRaises(Exception, msg=repr(bad)):
                self.read(reuse=False)  # an order's own read, which never takes the kept list
            self.bad = None
            self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"],
                             f"{bad!r}: the list kept before it is gone, and it was not kept")
            self.assertEqual(self.read()[0], ["clearinghouseState"], "a list read whole is kept again")

    def test_a_read_not_allowed_to_reuse_always_reads_them(self):
        self.read()
        self.assertEqual(self.read(reuse=False)[0], ["clearinghouseState", "frontendOpenOrders"])
        self.assertEqual(self.read(reuse=False)[0], ["clearinghouseState", "frontendOpenOrders"])

    def test_each_dex_keeps_its_own_list(self):
        self.venue = hl.Venue(clock=lambda: self.now, dexes=(hl.MAIN_DEX, "xyz"))
        with mock.patch.object(self.venue, "dex_index", return_value=1):
            self.assertEqual(self.read()[0], ["clearinghouseState", "frontendOpenOrders"] * 2)
            self.assertEqual(self.read()[0], ["clearinghouseState"] * 2)


class Recording(Exchange):
    """The test exchange, as a venue that may keep an orders list: it says which reads may."""

    reuses_orders = True

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads: list[tuple[str, bool]] = []
        self.forgotten: list[str] = []

    def book(self, account, markets, reuse_orders=False):
        self.reads.append((account, reuse_orders))
        return super().book(account, markets)

    def forget(self, account):
        self.forgotten.append(account)


def answer_of(x: Exchange) -> dict:
    """The test exchange's account as Hyperliquid's `clearinghouseState` gives it."""
    notional = sum(abs(q) * x.markets_now[a].mark for a, q in x.positions.items())
    return {"marginSummary": {"accountValue": str(x.equity), "totalNtlPos": str(notional)},
            "assetPositions": [{"position": {"coin": x.markets_now[a].coin, "szi": str(q),
                                             "positionValue": str(abs(q) * x.markets_now[a].mark)}}
                               for a, q in x.positions.items() if q != 0]}


class LiveCache(FlowBase):
    """The sweep on hl.Venue's own book and its own kept lists, not on a double of them: only Hyperliquid's
    two answers come from the test exchange."""

    def setUp(self):
        super().setUp()
        x = self.x
        self.asked: list[str] = []

        def info(body, timeout):
            self.asked.append(body["type"])
            if body["type"] == "clearinghouseState":
                return answer_of(x)
            if body["type"] == "frontendOpenOrders":
                return [dict(o) for o in x.orders]
            raise AssertionError(body)

        patcher = mock.patch.object(hl, "_info", side_effect=info)
        patcher.start()
        self.addCleanup(patcher.stop)

        class Venue(hl.Venue):
            def markets(self):
                return x.markets()

        self.venue = Venue(clock=lambda: self.now)
        # Every action that reaches the exchange, the trader's and the gateway's own: what each found
        # kept of the account's orders as it went. Nothing may go while a list is kept.
        self.kept_at_send: list[str] = []
        self.lose_answer = False  # the gateway's next own action lands, and its answer is lost

        def submit(action, nonce, signature):
            if any(k[0] == ACCOUNT.lower() for k in self.venue._orders):
                self.kept_at_send.append(action["type"])
            out = x.submit(action, nonce, signature)
            if self.lose_answer and (action.get("grouping") == "positionTpsl" or action["type"] == "batchModify"):
                self.lose_answer = False
                raise TimeoutError("the answer was lost; the action is on the book")
            return out

        self.gw = server.Gateway(self.reader, self.gw.signer, submit=submit, clock=lambda: self.now,
                                 venue=self.venue)
        self.p = self.gw.protector

    def tearDown(self):
        self.assertEqual(self.kept_at_send, [], "an action went while the account's orders list was kept")

    def resting_and_kept(self):
        """An account the gateway watches with an order resting and no position, its list kept."""
        self.x.rest_trader_orders = True
        status, _ = self.send()
        self.assertEqual(status, 200)
        self.sweep()
        self.assertEqual(self.sweep(), ["clearinghouseState"], "the list is kept")

    def sweep(self, full=True) -> list[str]:
        self.asked.clear()
        self.p.sweep(full=full)
        return list(self.asked)

    def stops(self) -> list[dict]:
        return [o for o in self.x.orders if o.get("orderType") == "Stop Market"]

    def test_a_stop_the_exchange_took_off_is_put_back_at_the_next_sweep_with_the_position_as_it_was(self):
        status, _ = self.send()  # its stop and take on the book
        self.assertEqual(status, 200)
        self.x.positions[BTC] = Decimal("0.005")  # the order filled
        self.sweep()
        self.sweep()
        self.assertNotIn(ACCOUNT, self.p.hot(), "a position with its stop, nothing resting, nothing changed")
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"], "a position: read every sweep")
        # A crash: the stop fired into a book that did not fill it, and the exchange took it off.
        # The position is as it was.
        self.x.orders = [o for o in self.x.orders if o.get("orderType") != "Stop Market"]
        sent = len(self.x.own_actions())
        self.assertIn("frontendOpenOrders", self.sweep())
        self.assertEqual(len(self.stops()), 1, "the next sweep, the longer period's, put the stop back")
        self.assertGreater(len(self.x.own_actions()), sent)
        self.assertIn(ACCOUNT, self.p.hot())

    def test_an_account_holding_no_position_keeps_its_list_until_something_changes(self):
        self.x.rest_trader_orders = True
        status, _ = self.send()  # an order resting that may open, no position
        self.assertEqual(status, 200)
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"], "after the order: afresh")
        self.assertEqual(self.sweep(), ["clearinghouseState"])
        self.assertEqual(self.sweep(full=False), ["clearinghouseState"], "hot, and still the kept list")
        # It filled: the positions moved, the orders are read, and from then on at every sweep.
        self.x.positions[BTC] = Decimal("0.005")
        self.x.orders = [o for o in self.x.orders if o.get("orderType") != "Limit"]
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"])
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"])

    def test_a_stop_and_take_that_landed_with_their_answer_lost_are_read_at_the_next_sweep(self):
        self.resting_and_kept()
        before = [o["triggerPx"] for o in self.stops()]
        # A second order: its stop moves on the book, the answer does not come back, and the order is
        # refused. The list the order's own read kept shows the stop where it was.
        self.lose_answer = True
        status, out = self.send(nonce=NOW + 1)
        self.assertEqual((out.get("code"), out.get("sent")), ("protection_failed", False), out)
        self.assertNotEqual([o["triggerPx"] for o in self.stops()], before, "the stop moved")
        self.assertIn(ACCOUNT, self.p.hot())
        self.assertEqual(self.sweep(full=False), ["clearinghouseState", "frontendOpenOrders"])

    def test_a_request_refused_after_the_account_was_read_keeps_no_list(self):
        self.resting_and_kept()
        status, out = self.send(nonce=NOW + 1, size="1")  # past the leverage rule, found from a fresh read
        self.assertEqual(status // 100, 4, out)
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"])

    def test_a_trader_action_goes_with_no_list_kept(self):
        self.resting_and_kept()
        self.x.orders = [o for o in self.x.orders if o.get("orderType") != "Limit"]
        status, out = self.send("cancel", nonce=NOW + 1)
        self.assertEqual(status, 200, out)
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"])

    def test_a_pair_left_by_an_order_that_went_is_taken_off_with_no_list_kept(self):
        self.resting_and_kept()
        # The order went without filling, outside the gateway: its stop and take stand for nothing.
        self.x.orders = [o for o in self.x.orders if o.get("orderType") != "Limit"]
        self.p.forget_orders(ACCOUNT)  # the list kept with the order in it is not what is tested here
        sent = len(self.x.sent)
        self.sweep()
        self.sweep()
        self.assertEqual([a["type"] for a in self.x.sent[sent:]], ["cancel", "cancel"])
        self.assertEqual(self.x.orders, [])

    def test_a_sweep_that_broke_off_keeps_no_list(self):
        self.resting_and_kept()
        with mock.patch.object(self.p, "_stray_again", side_effect=RuntimeError("the chain would not answer")):
            self.sweep()
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"])

    def test_a_read_after_an_entry_that_broke_off_keeps_no_list(self):
        self.resting_and_kept()
        with mock.patch.object(self.p, "_stray_again", side_effect=RuntimeError("the chain would not answer")):
            self.p.after_entry(ACCOUNT, self.key.address)
        self.assertEqual(self.sweep(), ["clearinghouseState", "frontendOpenOrders"])

    def test_every_action_of_the_gateways_own_goes_out_by_one_door(self):
        from gateway import protect
        source = inspect.getsource(protect.Protector)
        self.assertEqual(source.count("self.act("), 1, "the stop, the take, a cancel and a close all go by _send")
        door = inspect.getsource(protect.Protector._send)
        self.assertLess(door.index("self.note_sent(account)"), door.index("self.act("))
        trader = inspect.getsource(server.Gateway._handle_held)
        self.assertLess(trader.index("self.protector.note_sent(req.account)"),
                        trader.index("self.submit(action, req.nonce, signature)"))
        self.resting_and_kept()
        self.assertTrue(any(k[0] == ACCOUNT.lower() for k in self.venue._orders))
        self.p._send(ACCOUNT, self.key.address, {"type": "cancel", "cancels": [{"a": BTC, "o": 1}]})
        self.assertFalse(any(k[0] == ACCOUNT.lower() for k in self.venue._orders))


class HotAndCalm(FlowBase):
    def setUp(self):
        super().setUp()
        self.x = Recording()
        self.gw = server.Gateway(self.reader, self.gw.signer, submit=self.x.submit, clock=lambda: self.now, venue=self.x)
        self.p = self.gw.protector

    def test_only_the_sweep_may_take_the_last_orders_list(self):
        status, _ = self.send()
        self.assertEqual(status, 200)
        self.assertTrue(self.x.reads and not any(reuse for _, reuse in self.x.reads), "an order's own reads read them")
        self.assertIn(ACCOUNT, self.x.forgotten, "the order went: the next read takes them afresh")
        self.x.reads.clear()
        self.p.sweep()
        self.assertEqual(self.x.reads, [(ACCOUNT, True)])
        self.x.reads.clear()
        self.p.after_entry(ACCOUNT, self.key.address)
        self.assertEqual(self.x.reads, [(ACCOUNT, False)], "the read after an entry is an order's own")

    def test_a_trader_action_of_any_kind_makes_the_account_hot_and_its_orders_read_again(self):
        self.send()
        self.x.forgotten.clear()
        with self.p._guard:
            self.p._hot.clear()
        status, out = self.send("cancel", nonce=NOW + 1)
        self.assertEqual(status, 200, out)
        self.assertIn(ACCOUNT, self.x.forgotten)
        self.assertIn(ACCOUNT, self.p.hot())

    def test_an_account_is_hot_while_a_position_can_stand_without_a_stop_and_calm_otherwise(self):
        self.send()  # its stop and take on the book
        self.x.positions[BTC] = Decimal("0.005")  # and the order filled
        self.p.sweep()
        self.p.sweep()
        self.assertNotIn(ACCOUNT, self.p.hot(), "a position with its stop, nothing resting, nothing changed")
        self.assertEqual(self.p.accounts_with_position(), 1)
        # The hot-only pass reads nobody, not even the marks.
        self.x.reads.clear()
        reads = self.x.market_reads
        self.p.sweep(full=False)
        self.assertEqual((self.x.reads, self.x.market_reads), ([], reads))
        # The stop is gone (fired, or taken off with a halted market): hot at the next pass, read at the next tick.
        self.x.orders = [o for o in self.x.orders if not (o.get("isTrigger") and o.get("orderType") == "Stop Market")]
        self.p.sweep()
        self.assertIn(ACCOUNT, self.p.hot())
        self.x.reads.clear()
        self.p.sweep(full=False)
        self.assertEqual(self.x.reads, [(ACCOUNT, True)])

    def test_positions_that_moved_since_the_last_pass_make_the_account_hot(self):
        self.send()
        self.x.positions[BTC] = Decimal("0.005")
        self.p.sweep()
        self.p.sweep()
        self.assertNotIn(ACCOUNT, self.p.hot())
        # The position grew and its stop and take stood for it as they were, a fill in parts: the sweep
        # comes back at the shorter period, then calm again once nothing moves.
        self.x.positions[BTC] = Decimal("0.003")
        self.p.sweep()
        self.assertIn(ACCOUNT, self.p.hot())
        self.p.sweep()
        self.assertNotIn(ACCOUNT, self.p.hot())

    def test_an_order_resting_that_may_open_keeps_the_account_hot(self):
        self.x.rest_trader_orders = True
        self.send()
        self.p.sweep()
        self.p.sweep()
        self.assertIn(ACCOUNT, self.p.hot())

    def test_an_account_let_go_is_neither_hot_nor_counted(self):
        self.send()
        self.x.positions[BTC] = Decimal("0.005")
        self.p.sweep()
        self.assertEqual(self.p.accounts_with_position(), 1)
        self.p._unwatch(ACCOUNT)
        self.assertEqual((self.p.hot(), self.p.accounts_with_position()), (set(), 0))


class TwoPeriods(unittest.TestCase):
    def run_ticks(self, every, hot_every, ticks=6):
        from gateway.protect import Protector
        p = Protector(object(), object(), lambda *a: (None, True, {}))
        fulls: list[bool] = []
        p.sweep = lambda full=True: fulls.append(full)

        class Ticks:
            def __init__(self, n):
                self.n = n
                self.waited: list[float] = []

            def wait(self, t):
                self.waited.append(t)
                self.n -= 1
                return self.n < 0

        stop = Ticks(ticks)
        p.run(every, stop, hot_every)
        return fulls, set(stop.waited)

    def test_every_account_at_the_longer_period_and_the_hot_ones_between(self):
        self.assertEqual(self.run_ticks(30, 15), ([False, True] * 3, {15}))

    def test_without_a_shorter_period_every_sweep_is_whole(self):
        self.assertEqual(self.run_ticks(30, None), ([True] * 6, {30}))
        self.assertEqual(self.run_ticks(15, 30), ([True] * 6, {15}), "a 'shorter' period that isn't changes nothing")


class PeriodsMustBePositive(unittest.TestCase):
    def test_a_period_of_zero_or_less_is_refused_before_a_single_sweep(self):
        from gateway.protect import Protector
        p = Protector(object(), object(), lambda *a: (None, True, {}))
        swept: list[bool] = []
        p.sweep = lambda full=True: swept.append(full)

        class Once:  # one tick at most, so a run that wrongly starts cannot spin
            def __init__(self):
                self.n = 0

            def wait(self, t):
                self.n += 1
                return self.n > 1

        for every, hot in ((0, None), (-1, None), (30, 0), (30, -15)):
            with self.assertRaises(ValueError, msg=(every, hot)):
                p.run(every, Once(), hot)
        self.assertEqual(swept, [])
        import inspect
        body = inspect.getsource(server.main)
        self.assertIn("if not every > 0 or not hot_every > 0:", body)
        # The sweep starts in `take_on_at_start`, which main starts after the check.
        self.assertLess(body.index("if not every > 0 or not hot_every > 0:"), body.index("target=take_on_at_start"))
        self.assertIn("target=gateway.protector.run", inspect.getsource(server.take_on_at_start))


class Health(unittest.TestCase):
    def test_health_says_the_minutes_weight_and_the_accounts_at_each_request(self):
        from gateway.server import BoundedServer, make_handler

        class Stub:
            def handle_order(self, body):
                return 200, {}

        calls = []

        def live():
            calls.append(1)
            return {"weight_per_min": {"total": 42, "by_type": {"clearinghouseState": 42}}, "accounts_with_position": 3}

        srv = BoundedServer(("127.0.0.1", 0), make_handler(Stub(), "", health={"signer": "demo"}, live=live))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for _ in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                conn.request("GET", "/v1/health")
                import json
                said = json.loads(conn.getresponse().read())
                conn.close()
            self.assertEqual((said["signer"], said["weight_per_min"]["total"], said["accounts_with_position"]),
                             ("demo", 42, 3))
            self.assertEqual(len(calls), 2, "read at each request, not once at the start")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_the_live_summary_names_the_weight_and_the_accounts(self):
        from gateway.protect import Protector
        p = Protector(object(), object(), lambda *a: (None, True, {}))
        said = server.live_summary(p)
        self.assertEqual(set(said), {"weight_per_min", "accounts_watched", "accounts_with_position", "accounts_hot",
                                     "keys_unread"})
        self.assertEqual(said["keys_unread"], 0)
        self.assertEqual(set(said["weight_per_min"]), {"total", "by_type"})

    def test_the_periods_are_in_health(self):
        from gateway.demo_signer import DemoSigner
        said = server.health_summary("demo", DemoSigner([]), 30.0, protect_hot_every=15.0)
        self.assertEqual((said["protect_every"], said["protect_hot_every"]), (30.0, 15.0))


if __name__ == "__main__":
    unittest.main()
