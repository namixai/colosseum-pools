"""The stop book's checks on a made-up chain with a mempool, nonces, the replacement rule, a basefee and a clock under the
test's hand (ops/tests/book_chain.py): a key stuck, a transaction lost, a basefee that jumps, a node behind, dead or old,
the book unreadable, a clock off, checks reverting. Each test counts the blocks no check landed in."""
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from eth_abi import encode

from ops import book_keeper as bk
from ops import book_watch as bw
from ops import stop_filler as sf
from ops.tests.book_chain import BOOK, KEY_A, KEY_B, Chain, Clock, alarms, sel
from ops.tests import test_book_keeper as tbk


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clock.patch(self)
        for mod in (bk, sf, bw):
            p = mock.patch.object(mod, "log", lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        self.alarm, self.out = alarms()

    def poker(self, chain, keys=(KEY_A,), nodes=None, **kw):
        lanes = [bk.Lane(f"k{i}", bk.poke_sender(k, chain)) for i, k in enumerate(keys)]
        return bk.Poker(BOOK, nodes or bk.Nodes(chain, spawn=lambda f: None), lanes, self.alarm,
                        gas=lambda n: 200_000, **kw)

    def run_blocks(self, chain, poker, n, ticks_per_block=4, on_block=None, also=()):
        for _ in range(n):
            chain.mine()
            if on_block:
                on_block(chain)
            for _ in range(ticks_per_block):
                poker.tick()
                for f in also:
                    f()
                self.clock.advance(1.0 / ticks_per_block)

    def run_seconds(self, chain, poker, seconds, poll=0.25):
        t0 = self.clock.t
        while self.clock.t - t0 < seconds:  # as Poker.run: the next look a `poll` after the last began, or at once
            began = self.clock.t
            poker.tick()
            self.clock.advance(max(0.0, poll - (self.clock.t - began)))
            chain.catch_up()

    def names(self):
        return [e for e, _ in self.out]

    def unpoked(self, chain, lo, hi):
        return [b for b in range(lo, hi + 1) if b not in chain.poked]


def settle_data() -> bytes:
    return bytes.fromhex(sel("settle(address)")[2:]) + b"\0" * 32


class TheKeepersOtherTransactions(Base):
    """The checks go only from keys of their own: a settle of the keeper's key, lost or priced under a basefee that
    jumped, holds no check; and it is itself sent again within seconds, raised in fee."""

    def test_a_lost_settle_holds_no_check_and_goes_again_in_seconds(self):
        chain = Chain(self.clock)
        p = self.poker(chain, keys=(KEY_B,))
        keeper = bk.poke_sender(KEY_A, chain)
        self.run_blocks(chain, p, 5)
        chain.lose = lambda addr, nonce, raw, n: addr == KEY_A.address and len([1 for s in chain.sent if s[1] == addr]) == 1
        keeper.send(BOOK, settle_data(), 500_000, chain.basefee)
        start = chain.head
        self.run_blocks(chain, p, 30, also=(keeper.sweep,))
        self.assertEqual(self.unpoked(chain, start + 1, start + 30), [])
        self.assertEqual(chain.nonce.get(KEY_A.address), 1, "the settle landed")

    def test_a_settle_under_a_basefee_that_jumped_is_raised_over_it_and_holds_no_check(self):
        chain = Chain(self.clock)
        p = self.poker(chain, keys=(KEY_B,))
        keeper = bk.poke_sender(KEY_A, chain)
        keeper.price_hint = p.price_now
        self.run_blocks(chain, p, 5)
        keeper.send(BOOK, settle_data(), 500_000, chain.basefee)  # 2x the basefee as it was
        chain.basefee *= 3
        start = chain.head
        chain.mine()
        p.tick()
        self.assertGreaterEqual(p.price_now(), chain.basefee, "the next check is priced on the new base fee at once")
        self.run_blocks(chain, p, 60, also=(keeper.sweep,))
        gap = self.unpoked(chain, start + 1, start + 60)
        self.assertLessEqual(len(gap), 1, "a check priced on the newest block's base fee lands in the next")
        self.assertEqual(chain.nonce.get(KEY_A.address), 1, "the settle was raised over the new basefee")


class TheSecondKey(Base):
    def test_the_second_key_sends_as_soon_as_the_firsts_oldest_check_is_late(self):
        chain = Chain(self.clock)
        p = self.poker(chain, keys=(KEY_A, KEY_B))
        self.run_blocks(chain, p, 5)
        chain.lose = lambda addr, nonce, raw, n: addr == KEY_A.address
        start = chain.head
        self.run_blocks(chain, p, 20)
        self.assertLessEqual(len(self.unpoked(chain, start + 1, start + 20)), 3, "within POKE_RESEND_S, not six checks")


class TheTimer(Base):
    def test_a_head_that_went_back_on_another_node_stops_no_check(self):
        a = Chain(self.clock)
        b = Chain(self.clock)
        b.__dict__ = a.__dict__.copy()
        nodes = bk.Nodes(a, b, spawn=lambda f: None)
        p = bk.Poker(BOOK, nodes, [bk.Lane("k", bk.poke_sender(KEY_A, a))], self.alarm, gas=lambda n: 200_000)
        self.run_blocks(a, p, 10)
        a.lag = 6
        start = a.head
        self.run_blocks(a, p, 12)
        self.assertLessEqual(len(self.unpoked(a, start + 1, start + 12)), 1)

    def test_a_clock_off_by_half_a_minute_stops_no_check_and_is_an_alarm(self):
        chain = Chain(self.clock)
        chain.skew = 30.0
        p = self.poker(chain)
        start = chain.head
        self.run_blocks(chain, p, 20)
        self.assertLessEqual(len(self.unpoked(chain, start + 1, start + 20)), 1)
        self.assertIn("book_head_stale", self.names())

    def test_a_dead_first_node_is_tried_again_out_of_the_way_of_the_checks(self):
        a = Chain(self.clock)
        b = Chain(self.clock)
        b.__dict__ = a.__dict__.copy()
        a.dead, a.latency, b.latency = True, 2.0, 0.0
        probes, dead = [], []
        call = a.call
        a.call = lambda m, p=(): (dead.append(m), call(m, p))[1]
        nodes = bk.Nodes(a, b, spawn=probes.append)
        p = bk.Poker(BOOK, nodes, [bk.Lane("k", bk.poke_sender(KEY_A, b))], self.alarm, gas=lambda n: 200_000)
        b.auto, b.next_block = True, self.clock.t + 1.0
        self.run_seconds(b, p, 150)
        self.assertLessEqual(len(self.unpoked(b, 105, 250)), 1)
        self.assertTrue(probes, "the first node is tried again, in the background")
        self.assertLessEqual(len(dead), 1, "and never in the way: one timeout paid at the start")
        a.dead = False
        probes[0]()
        self.assertEqual(nodes.prefer, 0, "back to the first node once it answers a fresh block")

    def test_with_no_gas_price_read_at_the_start_the_blocks_base_fee_prices_the_check(self):
        chain = Chain(self.clock)
        chain.failing["eth_gasPrice"] = RuntimeError("eth_gasPrice: HTTP 403")
        p = self.poker(chain)
        start = chain.head
        self.run_blocks(chain, p, 10)
        self.assertLessEqual(len(self.unpoked(chain, start + 1, start + 10)), 1)


class TheBookUnread(Base):
    def test_a_watch_that_cannot_read_the_book_says_so_and_checks(self):
        chain = Chain(self.clock)
        chain.failing["eth_call"] = RuntimeError("eth_call: {'code': -32603, 'message': 'internal error'}")
        chain.last_poke = 0
        p = self.poker(chain, takeover=2, role="watch")
        self.run_blocks(chain, p, 60)
        self.assertIn("book_views_unread", self.names())
        self.assertGreaterEqual(len(chain.poked), 40, "behind unknown is behind: the watch checks")

    def test_a_keeper_that_cannot_read_the_book_says_so_and_checks(self):
        chain = Chain(self.clock)
        chain.failing["eth_call"] = RuntimeError("eth_call: {'code': -32000, 'message': 'execution timeout'}")
        p = self.poker(chain)
        self.run_blocks(chain, p, 40)
        self.assertIn("book_views_unread", self.names())
        self.assertGreaterEqual(len(chain.poked), 25)


class TheAlarms(Base):
    def test_checks_reverting_every_block_are_one_standing_alarm(self):
        chain = Chain(self.clock)
        chain.revert_pokes = True
        p = self.poker(chain)
        self.run_blocks(chain, p, 60)
        self.assertLessEqual(self.names().count("book_poke_reverted"), 3)
        self.assertGreaterEqual(self.names().count("book_poke_reverted"), 1)

    def test_a_silent_watch_is_an_alarm_for_the_keeper(self):
        chain = Chain(self.clock)
        p = self.poker(chain, watch_address=KEY_B.address)
        self.run_seconds(chain, p, 60)
        self.assertNotIn("book_watch_silent", self.names())
        self.clock.advance(bk.WATCH_SILENT_S)
        self.run_seconds(chain, p, 1)
        self.assertEqual(self.names().count("book_watch_silent"), 1)
        chain.nonce[KEY_B.address] = 5  # the watch sent its sign of life
        self.clock.advance(bk.WATCH_READ_EVERY_S)
        self.run_seconds(chain, p, 1)
        p2 = [e for e in self.names() if e == "book_watch_silent"]
        self.assertEqual(len(p2), 1)


class AnAssetWithNoPrice(Base):
    """A check that cannot read an asset's oracle (its read gas too low for the precompile) logs a zero and `NoPrice`:
    no `Gap`, no lag, and no stop of that asset touched."""

    def test_blocks_in_a_row_of_no_price_are_an_alarm_with_the_asset_and_their_count(self):
        chain = Chain(self.clock)
        p = self.poker(chain)
        self.run_blocks(chain, p, 10)
        before = self.names().count("book_poke_lagging")
        chain.no_price_assets = {3}
        self.run_blocks(chain, p, 12)
        alarm = [f for e, f in self.out if e == "book_no_price"]
        self.assertTrue(alarm)
        self.assertEqual(alarm[0]["asset"], 3)
        self.assertGreaterEqual(alarm[0]["blocks"], bk.NO_PRICE_ALARM_BLOCKS)
        self.assertIn("read-gas", alarm[0]["why"])
        self.assertEqual(self.names().count("book_poke_lagging"), before, "the checks went on: no lag")
        chain.no_price_assets = set()
        p.standing.every = 0
        n = len(alarm)
        self.run_blocks(chain, p, 12)
        self.assertEqual(len([1 for e, _ in self.out if e == "book_no_price"]), n, "over once a price is logged again")
        self.assertFalse(p.no_price_runs)

    def test_watch_raises_it_too(self):
        chain = Chain(self.clock)
        p = self.poker(chain, takeover=2, role="watch")
        p.sent_at = self.clock.t

        def keeper_pokes_with_no_price(ch):  # the keeper checks every block; asset 3 has no price in them
            ch.last_poke = ch.head
            ch.no_price.append((ch.head, 3))
        self.run_blocks(chain, p, 15, on_block=keeper_pokes_with_no_price)
        self.assertIn("book_no_price", self.names())
        self.assertEqual(chain.sent, [], "the watch sends nothing: the keeper is not behind")


class TheCostOfALook(Base):
    def probe(self, latency: float) -> tuple[int, int]:
        self.clock.t = 1_000_000.0
        chain = Chain(self.clock, latency=latency)
        chain.auto, chain.next_block = True, self.clock.t + 1
        p = self.poker(chain, keys=(KEY_A, KEY_B))
        self.run_seconds(chain, p, 10)
        chain.lose = lambda addr, nonce, raw, n: addr == KEY_A.address
        start, t0 = chain.head, self.clock.t
        self.run_seconds(chain, p, 90)
        return chain.head - start, len(self.unpoked(chain, start + 1, chain.head))

    def test_a_stuck_first_key_on_a_slow_node_leaves_at_most_two_blocks_unchecked_in_ninety_seconds(self):
        for latency in (0.0, 0.03, 0.06, 0.1):
            blocks, gap = self.probe(latency)
            self.assertLessEqual(gap, 2, f"{int(latency * 1000)} ms a call: {gap} of {blocks} blocks unchecked")

    def test_the_calls_a_second_of_one_checks_thread(self):
        chain = Chain(self.clock)
        p = self.poker(chain)
        self.run_blocks(chain, p, 60)
        self.assertLessEqual(len(chain.calls) / 60, bk.CALLS_PER_S_BUDGET)


class TheAlarmChannel(unittest.TestCase):
    def test_alarms_wait_for_a_few_commands_not_one_process_each(self):
        a = sf.Alarm(None, "sleep 0.05")
        with mock.patch.object(sf, "log", lambda *x, **k: None):
            before = threading.active_count()
            for i in range(25):
                a("book_poke_reverted", i=i)
            self.assertLessEqual(threading.active_count() - before, sf.ALARM_WORKERS)
            a.drain(10.0)
        self.assertEqual(a.queue.unfinished_tasks, 0, "every one went out")

    def test_a_failing_channel_is_tried_again(self):
        seen = []
        a = sf.Alarm(None, "exit 1")
        a.retry_s = (0.0, 0.0)
        with mock.patch.object(sf, "log", lambda e, **k: seen.append(e)):
            a("book_gap")
            a.drain(10.0)
        self.assertEqual(seen.count("stop_alarm_not_sent"), 3)

    def test_no_address_path_in_the_line_of_a_node_shared_with_the_gateway(self):
        from ops import keeper as core
        d = pathlib.Path(tempfile.mkdtemp())
        env = d / "gateway.env"
        url = "https://rpc.example.org/v2/SECRETTOKEN123"
        env.write_text("GATEWAY_RPC_URL=" + url + "\n")
        out = []
        with mock.patch.object(core, "log", lambda e, **k: out.append((e, k))):
            core.check_node(url, 998, env)
        env.unlink()
        d.rmdir()
        self.assertEqual(out[0][1]["rpc"], "https://rpc.example.org")
        self.assertEqual(sf.host_free("x https://h.example/k/SECRET?y=1 z"), "x https://h.example z")


class AWatchThatCannotStart(unittest.TestCase):
    def test_it_raises_an_alarm_before_it_ends_once_in_five_minutes(self):
        d = pathlib.Path(tempfile.mkdtemp())
        flag = d / "w.alarm"
        raised = []
        alarm = mock.Mock(side_effect=lambda e, **f: raised.append((e, f)))
        exc = sf.Unsure("eth_chainId: ConnectionError for https://n.example/KEY123")
        with mock.patch.object(bw, "log"):
            bw.start_failed(alarm, flag, exc)
            bw.start_failed(alarm, flag, exc)
        self.assertEqual([e for e, _ in raised], ["book_watch_cannot_start"])
        self.assertNotIn("KEY123", raised[0][1]["error"])
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


class ARestartWithNoHolders(tbk.Base):
    """After a restart `holders` is empty until the keeper's slow pass: a gap of that time is judged on the keeper's
    exams all the same, and an exam with an entry is a holder at once."""

    def setUp(self):
        super().setUp()
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "book.json"

    def tearDown(self):
        for f in self.dir.iterdir():
            f.unlink()
        self.dir.rmdir()

    def loop_at(self):
        alarm = mock.Mock(side_effect=lambda e, **f: self.alarms.append((e, f)))
        alarm.flag = None
        return bk.BookLoop(tbk.Keeper(), self.node, BOOK, wallet=tbk.KEY, sender=sf.Sender(tbk.KEY, self.node),
                           alarm=alarm, state_path=self.path, lanes=[bk.Lane("poke", sf.Sender(tbk.POKER, self.node))])

    def test_a_touch_in_a_gap_written_while_the_loop_was_down_is_judged(self):
        loop = self.loop_at()
        self.node.entries[(tbk.EXAM, tbk.BTC, False)] = tbk.entry(950_000, 90, True)
        self.run_to(loop, 100)
        loop.close()
        self.node.price.update({101: 949_000, 102: 949_000})
        self.node.logs.append(tbk.ev(bk.GAP, BOOK, 103, encode(["uint256", "uint256"], [101, 102])))
        again = self.loop_at()
        self.assertEqual(again.holders, set())
        self.run_to(again, 110)
        self.assertEqual([(t["exam"], t["block"]) for t in self.alarm_of("book_gap_touch")], [(tbk.EXAM, 101)])
        self.assertIn(tbk.EXAM, again.holders, "a holder at once, its touches settled without the slow pass")
        again.close()


if __name__ == "__main__":
    unittest.main()
