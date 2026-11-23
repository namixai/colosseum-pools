"""The stop book's loop made fit for stops on real money (ops/book_keeper.py, ops/book_watch.py): the checks go out from a
thread nothing else holds up, a check that is not landing is an alarm, a node that stopped is failed over, a late check
is signed again with more fee, a gap is judged on the book as it stood in it, and the loop's state outlives a restart.
On the node made up in test_book_keeper.py."""
import argparse
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from eth_abi import encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from ops import book_keeper as bk
from ops import stop_filler as sf
from ops.tests.test_book_keeper import BOOK, BTC, EXAM, KEY, Base, Keeper, Node, entry, ev, sel
from spike.hlspike import common as c

OTHER = Account.from_key("0x" + "43" * 32)  # a second key, made up here


def fee_of(raw: str) -> tuple[int, int, int]:
    t = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    return t["nonce"], t["maxFeePerGas"], t["maxPriorityFeePerGas"]


def tx_hash(raw: str) -> str:
    return "0x" + keccak(hexstr=raw).hex()


def run_threads(loop, seconds: float, poll=0.02, poke_poll=0.01, heads=None, every=0.1):
    """The loop's two threads for `seconds`; `heads`: a head a step every `every` seconds."""
    stop = threading.Event()
    loop.poke_poll = poke_poll
    t = threading.Thread(target=loop.run, args=(poll, stop), daemon=True)
    t.start()
    end = time.monotonic() + seconds
    if heads is not None:
        for h in heads:
            loop.node.head = h
            time.sleep(every)
    while time.monotonic() < end:
        time.sleep(0.01)
    stop.set()
    t.join(3)
    if loop.poker_thread is not None:
        loop.poker_thread.join(3)


# ── a check that is not landing ──────────────────────────────────────────────────────

class TheChecksLiveness(Base):
    def test_blocks_going_by_unchecked_while_the_book_watches_are_an_alarm_again_while_they_last(self):
        loop = self.loop(wallet=None)
        loop.poker.standing.every = 0  # each look raises it again, as every 30 seconds would
        self.node.last_poke = 96
        self.run_to(loop, 100, 101, 102)
        lag = self.alarm_of("book_poke_lagging")
        self.assertEqual([(a["head"], a["last_poke"], a["lag"]) for a in lag], [(100, 96, 4), (101, 96, 5), (102, 96, 6)])
        self.node.last_poke = None  # checked again
        self.run_to(loop, 103)
        self.assertTrue(self.events("book_poke_caught_up"))
        self.assertEqual(len(self.alarm_of("book_poke_lagging")), 3)

    def test_a_lag_of_three_blocks_or_a_book_watching_nothing_is_no_alarm(self):
        loop = self.loop(wallet=None)
        self.node.last_poke = 97
        self.run_to(loop, 100)
        self.node.watched, self.node.last_poke = [], 10
        self.run_to(loop, 101, 102)
        self.assertEqual(self.alarm_of("book_poke_lagging"), [])

    def test_a_key_with_no_money_is_an_alarm_at_once_and_again_while_it_lasts(self):
        class Broke(Node):
            def call(s, method, params=()):
                if method == "eth_sendRawTransaction":
                    raise RuntimeError("eth_sendRawTransaction: {'code': -32000, 'message': 'insufficient funds for gas "
                                       "* price + value'}")
                return super().call(method, params)
        self.node = Broke()
        loop = self.loop()
        self.run_to(loop, 100)
        failing = self.alarm_of("book_poke_failing")
        self.assertEqual([a["head"] for a in failing], [100], "at once: a key refused for want of money")
        self.assertIn("insufficient funds", failing[0]["errors"][0])
        loop.poker.standing.every = 0
        self.run_to(loop, *range(101, 160))
        self.assertEqual(len(self.alarm_of("book_poke_failing")), 60, "again while it lasts")
        self.assertEqual(self.node.raw, [])

    def test_a_check_refused_for_any_cause_three_blocks_in_a_row_is_an_alarm(self):
        class Refuses(Node):
            def call(s, method, params=()):
                if method == "eth_sendRawTransaction":
                    raise RuntimeError("eth_sendRawTransaction: {'code': -32000, 'message': 'txpool is full'}")
                return super().call(method, params)
        self.node = Refuses()
        loop = self.loop()
        self.run_to(loop, 100, 101)
        self.assertEqual(self.alarm_of("book_poke_failing"), [])
        self.run_to(loop, 102)
        self.assertEqual([a["blocks"] for a in self.alarm_of("book_poke_failing")], [bk.POKE_FAIL_BLOCKS])

    def test_a_key_whose_money_pays_few_hours_is_an_alarm_before_it_runs_out(self):
        loop = self.loop()
        self.node.balance = 10 ** 18
        self.run_to(loop, 100)
        low = self.alarm_of("book_key_low")
        self.assertEqual(len(low), 1)
        per = bk.poke_gas(1) * 100_000_000
        self.assertEqual((low[0]["lane"], low[0]["blocks"]), ("keeper", 10 ** 18 // per))
        self.assertLess(low[0]["hours"], bk.RUNWAY_HOURS)
        self.node.balance = 10 ** 21
        loop.poker.lanes[0].balance_at = -1e18
        self.run_to(loop, 101)
        self.assertTrue(self.events("book_key_funded"))
        self.assertEqual(len(self.alarm_of("book_key_low")), 1)

    def test_checks_waiting_long_for_a_block_are_an_alarm(self):
        loop = self.loop()
        self.node.pokes_land = False
        self.run_to(loop, 100)
        lane = loop.poker.lanes[0]
        lane.in_flight = [(n, h, t - bk.POKE_STUCK_S - 1) for n, h, t in lane.in_flight]
        self.run_to(loop, 101)
        self.assertEqual([a["nonce"] for a in self.alarm_of("book_poke_stuck")], [7])


class AFrozenHead(Base):
    def test_a_node_answering_an_old_block_is_an_alarm_no_check_and_no_heartbeat(self):
        self.node.time = time.time() - 1_000
        loop = self.loop()
        loop.beat = 0.0
        run_threads(loop, 0.4)
        self.assertEqual(self.node.raw, [], "no check on a head nobody knows is the chain's")
        self.assertTrue(self.alarm_of("book_head_stale"))
        self.assertEqual(loop.beat, 0.0, "an old head keeps no heartbeat alive")
        thread = mock.Mock()
        thread.is_alive.return_value = True
        sf.keep_alive(loop, thread, 0.5)
        self.assertTrue(self.alarm_of("stop_loop_stalled"))

    def test_an_old_first_node_is_failed_over_to_a_fresh_backup_and_said(self):
        frozen, fresh = Node(), Node()
        frozen.head, frozen.time = 90, time.time() - 1_000
        fresh.head = 120
        alarms = []
        nodes = bk.Nodes(frozen, fresh, standing=bk.Standing(lambda e, **f: alarms.append((e, f))))
        with mock.patch.object(bk, "log"):
            self.assertEqual(nodes.head(), 120)
            self.assertEqual(nodes.call("eth_blockNumber"), hex(120), "the backup is asked first now")
        self.assertEqual([(e, f["node"]) for e, f in alarms], [("book_node_stale", "first")])
        old = Node()
        old.time = time.time() - 1_000
        with self.assertRaises(bk.NodeStale):
            bk.fresh_head(old)

    def test_a_head_that_cannot_be_read_is_an_alarm_after_a_few_seconds(self):
        class Dead:
            url = "http://127.0.0.1:9"

            def call(s, m, p=()):
                raise sf.Unsure(f"{m}: ConnectionError")
        p = bk.Poker(BOOK, Dead(), [], mock.Mock(side_effect=lambda e, **f: self.alarms.append((e, f))))
        p.tick()
        self.assertEqual(self.alarm_of("book_head_unread"), [])
        p.blind_since = time.monotonic() - bk.BLIND_S - 1
        p.tick()
        self.assertEqual(len(self.alarm_of("book_head_unread")), 1)


# ── the checks, held up by nothing ───────────────────────────────────────────────────

class TheChecksFirst(Base):
    def test_the_first_check_goes_in_the_step_that_sees_the_book_watch(self):
        loop = self.loop()
        self.node.watched = []
        self.assertEqual(self.run_to(loop, 100, 101), [[], []])
        self.node.watched = [BTC]  # a stop set in block 102 (the exam's own settle checked 102 itself)
        self.assertEqual(self.run_to(loop, 102, 103), [["poke"], ["poke"]])

    def test_an_exam_holding_an_entry_is_checked_for_before_the_book_is_read_again(self):
        loop = self.loop()
        self.node.watched = []
        self.run_to(loop, 100)
        loop.holders.add(EXAM)
        self.assertEqual(self.run_to(loop, 101), [["poke"]])

    def test_a_node_that_refuses_getlogs_costs_the_logs_and_no_check(self):
        class NoLogs(Node):
            def call(s, method, params=()):
                if method == "eth_getLogs":
                    raise c.NodeBusy("eth_getLogs: HTTP 429")
                return super().call(method, params)
        self.node = NoLogs()
        loop = self.loop()
        for h in range(100, 130):
            self.node.head = h
            with self.assertRaises(c.NodeBusy):
                loop.step(h)
        self.assertEqual(len(self.node.pokes()), 30, "a check for every block of the thirty")

    def test_a_failed_gas_price_read_keeps_the_last_price_and_the_checks(self):
        class NoGas(Node):
            fail = False

            def call(s, method, params=()):
                if method == "eth_gasPrice" and s.fail:
                    raise c.NodeBusy("eth_gasPrice: HTTP 429")
                return super().call(method, params)
        self.node = NoGas()
        loop = self.loop()
        self.run_to(loop, 100)
        self.node.fail = True
        steps = self.run_to(loop, *range(101, 125))
        self.assertEqual(len([s for s in steps if "poke" in s]), 24)
        self.assertEqual({fee_of(r)[1] for r in self.node.raw}, {2 * 100_000_000})

    def test_a_check_the_node_refused_is_sent_again_inside_the_block(self):
        class BusyOnce(Node):
            busy = True

            def call(s, method, params=()):
                if method == "eth_sendRawTransaction" and s.busy:
                    s.busy = False
                    raise c.NodeBusy("eth_sendRawTransaction: HTTP 429")
                return super().call(method, params)
        self.node = BusyOnce()
        loop = self.loop()
        self.node.head = 100
        loop.step(100)
        loop.step(100)  # the next look, a quarter of a second later, the same head
        loop.step(100)
        self.assertEqual(len(self.node.pokes()), 1)


class TheChecksThread(Base):
    def test_a_slow_read_of_every_holders_touches_holds_up_no_check(self):
        class Slow(Node):
            def call(s, method, params=()):
                if method == "eth_call" and params[0]["data"].startswith(sel("touches(address)")):
                    time.sleep(0.02)
                return super().call(method, params)
        self.node = Slow()
        loop = self.loop()
        for i in range(60):
            loop.holders.add("0x" + f"{0x1000 + i:040x}")  # a step reads 60 touches: over a second
        run_threads(loop, 1.6, heads=range(100, 110), every=0.15)
        self.assertGreaterEqual(len(self.node.pokes()), 9, "a check for nearly every head of ten, a step taking 1.2 s")

    def test_a_main_loop_failing_on_every_step_holds_up_no_check(self):
        class NoLogs(Node):
            def call(s, method, params=()):
                if method == "eth_getLogs":
                    raise c.NodeBusy("eth_getLogs: HTTP 429")
                return super().call(method, params)
        self.node = NoLogs()
        loop = self.loop()
        run_threads(loop, 1.2, heads=range(100, 110), every=0.1)
        self.assertEqual(loop.block, 0, "the loop never got past its logs")
        self.assertGreaterEqual(len(self.node.pokes()), 9)

    def test_a_checks_thread_that_ended_is_an_alarm_and_starts_again(self):
        loop = self.loop()
        dead = mock.Mock()
        dead.is_alive.return_value = False
        loop.poker_thread, loop.poker_poll = dead, 0.01
        stop = threading.Event()
        with mock.patch.object(loop.poker, "run", side_effect=lambda poll, s: s.wait()):
            loop.keep_poker(stop)
            self.assertTrue(self.alarm_of("book_poker_dead"))
            self.assertIsNot(loop.poker_thread, dead)
            self.assertTrue(loop.poker_thread.is_alive())
            stop.set()
        loop.poker.beat = time.monotonic() - sf.STALL_S - 1
        alive = mock.Mock()
        alive.is_alive.return_value = True
        loop.poker_thread = alive
        loop.keep_poker(stop)
        self.assertTrue(self.alarm_of("book_poker_stalled"))


# ── a late check, a second key ───────────────────────────────────────────────────────

class ALateCheck(Base):
    def test_a_check_with_no_block_is_signed_again_with_a_quarter_more_fee_under_its_nonce(self):
        loop = self.loop()
        self.node.pokes_land = False
        self.run_to(loop, 100)
        first = self.node.raw[0]
        nonce = next(iter(loop.sender.ledger))
        self.assertEqual(loop.sender.ledger[nonce]["after"], bk.POKE_RESEND_S)
        loop.sender.ledger[nonce]["at"] = -1e9
        loop.sender.sweep()
        again = self.node.raw[-1]
        self.assertNotEqual(again, first)
        (n1, fee1, tip1), (n2, fee2, tip2) = fee_of(first), fee_of(again)
        self.assertEqual(n2, n1)
        self.assertGreaterEqual(fee2, fee1 * 5 // 4)
        self.assertGreater(tip2, tip1)
        self.node.receipts[tx_hash(again)] = {"status": "0x1", "blockNumber": hex(101)}
        self.run_to(loop, 101)
        self.assertNotIn(n1, [n for n, _, _ in loop.poker.lanes[0].in_flight], "landed under its new hash")

    def test_a_check_is_not_signed_again_past_the_most_bumps_but_sent_as_it_is(self):
        loop = self.loop()
        self.node.pokes_land = False
        self.run_to(loop, 100)
        nonce = next(iter(loop.sender.ledger))
        for _ in range(sf.MAX_BUMPS + 2):
            loop.sender.ledger[nonce]["at"] = -1e9
            loop.sender.sweep()
        fees = [fee_of(r)[1] for r in self.node.raw]
        self.assertEqual(len(set(fees)), sf.MAX_BUMPS + 1)
        self.assertEqual(self.node.raw[-1], self.node.raw[-2])

    def test_the_keepers_own_sends_are_never_signed_again(self):
        loop = self.loop()
        tx, nonce = loop.sender.send(BOOK, b"\x00", 21_000, 100_000_000)
        loop.sender.ledger[nonce]["at"] = -1e9
        loop.sender.sweep()
        self.assertEqual(self.node.raw[-1], self.node.raw[0], "the keeper waits for this very hash")

    def test_a_key_refused_leaves_the_check_to_the_next_key_in_the_same_look(self):
        class OneBroke(Node):
            def call(s, method, params=()):
                if method == "eth_sendRawTransaction" and Account.recover_transaction(params[0]) == KEY.address:
                    raise RuntimeError("eth_sendRawTransaction: insufficient funds for gas * price + value")
                return super().call(method, params)
        self.node = OneBroke()
        alarm = mock.Mock(side_effect=lambda e, **f: self.alarms.append((e, f)))
        alarm.flag = None
        own = bk.Lane("own", sf.Sender(KEY, self.node))
        second = bk.Lane("second", sf.Sender(OTHER, self.node))
        loop = bk.BookLoop(Keeper(), self.node, BOOK, wallet=KEY, sender=own.sender, lanes=[own, second], alarm=alarm)
        self.assertEqual(self.run_to(loop, 100, 101), [["poke"], ["poke"]])
        self.assertEqual({Account.recover_transaction(r) for r in self.node.raw}, {OTHER.address})
        self.assertEqual(self.alarm_of("book_poke_failing"), [], "the check went out")

    def test_the_keeper_builds_a_lane_for_each_key_of_its_own_and_its_key_last(self):
        args = argparse.Namespace(stop_rpc="http://127.0.0.1:9", stop_backup_rpc="", poke_rpc="", poke_wallet="a, b",
                                  stale_s=10.0, poke_runway_hours=72.0, poke_poll=0.25, book_state="", price_journal="",
                                  stop_alarm="", stop_tip_gwei=0.0, deployment="sim1")
        keys = {"a": OTHER, "b": Account.from_key("0x" + "44" * 32)}
        with mock.patch.object(bk, "checked_node", side_effect=lambda url, t=5.0: Node()), \
                mock.patch.object(bk.c, "account", side_effect=lambda n: keys[n]), \
                mock.patch.object(bk, "precompile_gas_of", return_value=30_000):
            loop = bk.build(args, Keeper(), pathlib.Path("/nonexistent/state.json"), BOOK, wallet=KEY)
        self.assertEqual([l.name for l in loop.poker.lanes], ["a", "b", "keeper"])
        self.assertIs(loop.poker.lanes[-1].sender, loop.sender)


# ── the nodes ────────────────────────────────────────────────────────────────────────

class TheNodesFailover(unittest.TestCase):
    class Dead:
        url = "http://x"

        def __init__(s, exc):
            s.exc, s.n = exc, 0

        def call(s, m, p=()):
            s.n += 1
            raise s.exc

    class Good:
        url = "http://y"

        def __init__(s):
            s.n = 0

        def call(s, m, p=()):
            s.n += 1
            return "0x1"

    def setUp(self):
        p = mock.patch.object(bk, "log")
        p.start()
        self.addCleanup(p.stop)

    def test_a_403_a_limit_or_a_timeout_goes_to_the_backup(self):
        for exc in (RuntimeError("eth_blockNumber: HTTP 403"),
                    RuntimeError("eth_call: {'code': -32007, 'message': 'request limit reached'}"),
                    sf.Unsure("eth_call: ReadTimeout"), c.NodeBusy("eth_call: HTTP 429")):
            good = self.Good()
            self.assertEqual(bk.Nodes(self.Dead(exc), good).call("eth_blockNumber"), "0x1")
            self.assertEqual(good.n, 1)

    def test_an_answer_is_never_failed_over(self):
        for exc in (RuntimeError("eth_call: execution reverted"), RuntimeError("eth_sendRawTransaction: nonce too low")):
            good = self.Good()
            with self.assertRaises(RuntimeError):
                bk.Nodes(self.Dead(exc), good).call("eth_call")
            self.assertEqual(good.n, 0)

    def test_a_dead_first_node_is_asked_second_and_first_again_a_minute_on(self):
        dead, good = self.Dead(sf.Unsure("eth_call: Timeout")), self.Good()
        nodes = bk.Nodes(dead, good)
        for _ in range(20):
            nodes.call("eth_blockNumber")
        self.assertEqual((dead.n, good.n), (1, 20), "one timeout paid, not twenty")
        nodes.prefer_since -= bk.FAILOVER_BACK_S + 1
        nodes.call("eth_blockNumber")
        self.assertEqual(dead.n, 2, "tried first again after a minute")


# ── the settles ──────────────────────────────────────────────────────────────────────

class ATouchNotCarriedOut(Base):
    def touch(self, blk=101):
        return (BTC, False, 950_000, 949_000, 0xFFFFFFFF, blk, 1_000_000 + blk, 1)

    def test_a_settle_that_keeps_reverting_is_an_alarm_and_waits(self):
        class Reverts(Node):
            def call(s, method, params=()):
                if method == "eth_getTransactionReceipt":
                    for raw in s.raw:
                        if tx_hash(raw) == params[0] and s.decoded_one(raw)[2] == sel("settle(address)"):
                            return {"status": "0x0", "blockNumber": hex(s.head)}
                return super().call(method, params)
        self.node = Reverts()
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [self.touch()]
        self.run_to(loop, *range(100, 200))
        self.assertLess(len(self.node.settles()), 20, "not a settle every block")
        self.assertEqual(len(self.alarm_of("book_settle_reverting")), 1)
        standing = self.alarm_of("book_touch_standing")
        self.assertTrue(standing)
        self.assertEqual(standing[0]["first_touch_block"], 101)

    def test_touches_that_cannot_be_read_are_an_alarm(self):
        class Bad(Node):
            def call(s, method, params=()):
                if method == "eth_call" and params[0]["data"].startswith(sel("touches(address)")):
                    raise RuntimeError("eth_call: out of gas")
                return super().call(method, params)
        self.node = Bad()
        loop = self.loop()
        loop.holders.add(EXAM)
        self.run_to(loop, *range(100, 100 + bk.TOUCH_ALARM_BLOCKS))
        self.assertEqual(self.alarm_of("book_touches_unread"), [])
        self.run_to(loop, 100 + bk.TOUCH_ALARM_BLOCKS)
        self.assertEqual([a["exam"] for a in self.alarm_of("book_touches_unread")], [EXAM])

    def test_a_touch_standing_for_any_cause_is_an_alarm_and_its_end_a_line(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [self.touch(101)]
        self.node.refuse_settle = True  # the trial refuses it: nothing is sent, and nothing else would say so
        self.run_to(loop, *range(101, 101 + bk.TOUCH_ALARM_BLOCKS + 1))
        self.assertEqual(self.alarm_of("book_touch_standing"), [])
        self.run_to(loop, 102 + bk.TOUCH_ALARM_BLOCKS)
        self.assertEqual(len(self.alarm_of("book_touch_standing")), 1)
        self.node.touches[EXAM] = []
        self.run_to(loop, 103 + bk.TOUCH_ALARM_BLOCKS)
        self.assertTrue(self.events("book_touch_carried_out"))


# ── the gaps ─────────────────────────────────────────────────────────────────────────

class TheGapAsItStood(Base):
    def test_a_gap_closed_by_the_tx_that_ended_the_last_stop_is_judged_on_the_book_before_it(self):
        loop = self.loop()
        loop.journal = mock.Mock()
        loop.holders.add(EXAM)
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True)
        self.run_to(loop, 100)
        # Blocks 101..103 unchecked; the price reached the stop in them. In block 104 the trader's own transaction (its
        # settle writes the Gap) closed the position: the exam took the entry out, and the asset left `watched()`.
        self.node.price.update({101: 960_000, 102: 949_000, 103: 949_000, 104: 990_000})
        self.node.watched_at[100] = [BTC]
        self.node.entries_at[100] = dict(self.node.entries)
        self.node.watched = []
        self.node.entries.clear()
        self.node.logs.append(ev(bk.GAP, BOOK, 104, encode(["uint256", "uint256"], [101, 103])))
        self.run_to(loop, 104, 105, 106)
        self.assertTrue(self.alarm_of("book_gap"))
        self.assertEqual([(t["exam"], t["block"]) for t in self.alarm_of("book_gap_touch")], [(EXAM, 102)])
        self.assertEqual([call.args[0] for call in loop.journal.add.call_args_list], [101, 102, 103])

    def test_a_change_set_inside_the_gap_is_judged_too(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.entries_at[100] = {}
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 102, True)  # set in block 101, armed from 102
        self.run_to(loop, 100)
        self.node.price.update({101: 940_000, 102: 960_000, 103: 949_000})
        self.node.logs.append(ev(bk.GAP, BOOK, 104, encode(["uint256", "uint256"], [101, 103])))
        self.run_to(loop, 104)
        self.assertEqual([t["block"] for t in self.alarm_of("book_gap_touch")], [103], "not 101: not armed yet")


# ── a restart ────────────────────────────────────────────────────────────────────────

class ARestart(Base):
    def setUp(self):
        super().setUp()
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "book-sim1.json"

    def tearDown(self):
        for p in self.dir.iterdir():
            p.unlink()
        self.dir.rmdir()

    def loop_at(self):
        alarm = mock.Mock(side_effect=lambda e, **f: self.alarms.append((e, f)))
        alarm.flag = None
        return bk.BookLoop(Keeper(), self.node, BOOK, wallet=KEY, sender=sf.Sender(KEY, self.node), alarm=alarm,
                           state_path=self.path)

    def test_a_gap_written_while_the_loop_was_down_is_read_from_its_last_block(self):
        loop = self.loop_at()
        self.run_to(loop, 100)
        self.assertEqual(json.loads(self.path.read_text())["block"], 100)
        self.node.logs.append(ev(bk.GAP, BOOK, 103, encode(["uint256", "uint256"], [101, 102])))  # while it was down
        again = self.loop_at()
        self.assertEqual(again.block, 100)
        self.run_to(again, 110)
        self.assertEqual([(a["from_block"], a["to"]) for a in self.alarm_of("book_gap")], [(101, 102)])

    def test_the_gaps_still_to_read_back_outlive_a_restart(self):
        loop = self.loop_at()
        loop.holders.add(EXAM)
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True)
        self.run_to(loop, 100)
        self.node.logs.append(ev(bk.GAP, BOOK, 121, encode(["uint256", "uint256"], [101, 120])))
        self.node.price[118] = 949_000
        self.run_to(loop, 121)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["gap_jobs"], [{"from": 101, "to": 120, "next": 101 + bk.GAP_BLOCKS_PER_STEP}])
        again = self.loop_at()
        again.holders.add(EXAM)
        self.run_to(again, 122, 123, 124)
        self.assertEqual([t["block"] for t in self.alarm_of("book_gap_touch")], [118])
        self.assertEqual(len(self.alarm_of("book_gap")), 1, "said once, before the restart")

    def test_a_state_that_does_not_read_is_an_alarm_and_the_loop_starts_at_the_head(self):
        self.path.write_text("{not json")
        loop = self.loop_at()
        self.assertEqual(loop.block, 0)
        self.assertTrue(self.alarm_of("book_state_unread"))


# ── the checks' gas ──────────────────────────────────────────────────────────────────

class TheChecksGas(Base):
    def test_after_a_reverted_check_the_most_gas_for_a_while_then_the_books_again(self):
        loop = self.loop()
        self.node.pokes_land = False
        self.run_to(loop, 100)
        self.node.receipts[tx_hash(self.node.raw[0])] = {"status": "0x0", "blockNumber": hex(101)}
        self.run_to(loop, 101, 102)
        self.assertEqual(self.node.decoded()[-1][1], bk.MAX_TX_GAS)
        self.node.pokes_land = True
        self.run_to(loop, 102 + bk.FLOOR_BLOCKS, 103 + bk.FLOOR_BLOCKS)
        self.assertEqual(self.node.decoded()[-1][1], bk.poke_gas(1, loop.precompile_gas))

    def test_a_change_of_the_read_gas_is_the_next_checks_gas(self):
        loop = self.loop()
        self.run_to(loop, 100)
        self.node.logs.append(ev(bk.READ_GAS_SET, BOOK, 101, encode(["uint256", "uint256"], [15_000, 20_000])))
        self.run_to(loop, 101, 102)
        self.assertEqual(loop.precompile_gas, 35_000)
        self.assertEqual(self.node.decoded()[-1][1], bk.poke_gas(1, 35_000))

    def test_the_event_is_the_books(self):
        src = (pathlib.Path(__file__).resolve().parents[2] / "src/sim/StopBook.sol").read_text()
        self.assertIn("event ReadGasSet(uint256 oracleGas, uint256 bboGas);", src)
        self.assertIn("uint64 public lastPokeBlock;", src)


if __name__ == "__main__":
    unittest.main()
