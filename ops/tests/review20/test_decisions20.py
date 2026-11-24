"""What was decided on the findings of review 20 of PR 342, beside the reviewer's own tests (test_review20.py,
test_controls20.py): a look's number held to the block it ran in, the measure taken twice, the keeper's batch held to
the head it read, the node's answers by number held to the loop's own looks, the reads a burnt call lost read again
alone, and the events of a read's own block over the read. Fakes are those of test_stop_filler and test_sim_keeper;
no key, no live node.
Run: COLOSSEUM_KEY_DIR=<an empty dir> COLOSSEUM_RPC_URL=http://127.0.0.1:9 python -m unittest ops.tests.review20.test_decisions20 -v"""
import time
import unittest
from unittest import mock

from eth_abi import decode
from eth_utils import to_checksum_address

from ops import payout_review as pr
from ops import sim_keeper as sk
from ops import stop_filler as sf
from ops.tests import test_sim_keeper as T
from ops.tests import test_stop_filler as S
from ops.tests.review20 import test_review20 as R
from spike.hlspike import common as c


def journal(case):
    """The journal of prices the case's loop wrote, as the payout review reads it."""
    return pr.load_prices(pr.journal_text(case.dir / "prices.jsonl"))


class TheLooksNumber(S.Base):
    """A look's number is the call's, counted back by the node's numbering. It is held to the node's own record of
    that block (one eth_getBlockByNumber, by the number) when the look gives a reason to doubt it, and at least once a
    minute whatever it says; one that does not hold makes no step by its number."""

    def test_a_look_with_nothing_to_doubt_asks_for_no_block_and_once_a_minute_for_one(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        before = len(self.node.calls)
        self.run_to(loop, 101, 101)
        self.assertEqual([m for m, _ in self.node.calls[before:]], ["eth_call", "eth_getLogs", "eth_call"],
                         "a new block and the same one again: the call and the logs, nothing more")
        loop.confirmed_at -= sf.CONFIRM_EVERY_S
        before = len(self.node.calls)
        self.run_to(loop, 102)
        self.assertEqual([(m, p[0] if m == "eth_getBlockByNumber" else None) for m, p in self.node.calls[before:]],
                         [("eth_call", None), ("eth_getBlockByNumber", hex(102)), ("eth_getLogs", None)],
                         "a minute on: the look's block asked for by its number, once")
        self.assertEqual(self.events("stop_look_unconfirmed"), [])
        self.run_to(loop, 103)
        self.assertNotIn("eth_getBlockByNumber", [m for m, _ in self.node.calls[before + 3:]], "and not at the next look")

    def test_a_numbering_moved_between_two_looks_at_one_head_is_caught_by_the_time(self):
        """Measured 0; the next look at the same head says the next block's number with the head's time. It would step
        past the node's head and read the logs of a block not there yet."""
        self.node = R.ClampedNode()
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.run_to(loop, 101)
        self.node.ahead = 1
        self.node.logs.append(S.set_log(102, 1_100_000, 103, take=True))
        self.run_to(loop, 101)
        self.assertEqual(loop.block, 101, "no step past the node's head")
        self.assertEqual([(a["was"], a["now"], a["alarm"]) for a in self.events("stop_numbering_changed")], [(0, 1, "ALARM")],
                         "measured again at once")
        self.assertEqual(self.events("stop_look_wrong"), [], "the numbering moved: that one alarm, not two")
        self.assertEqual(loop.ahead, 1)
        before, batches = len(self.node.calls), len(self.node.batches())
        self.run_to(loop, 102, 103)
        self.assertIn((S.EXAM, S.BTC, True), loop.triggers, "the take set in block 102 is known")
        self.assertEqual([t for t, _ in self.node.batches()[batches:]], ["latest", "latest"], "one call a look again")
        self.assertNotIn("eth_getBlockByNumber", [m for m, _ in self.node.calls[before:]])

    def test_a_numbering_moved_that_no_look_betrays_is_caught_within_a_minute(self):
        """Measured 1; the node numbers a call as the head from now on, and the next look comes two blocks on: block
        103's call says 103, counted 102, a block after the last. Nothing the looks say betrays it; the look held to
        the node's record once a minute does."""
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        self.run_to(loop, 100, 101)
        self.node.ahead = 0
        self.node.price.update({102: 222, 103: 333, 104: 444})
        self.run_to(loop, 103)
        self.assertEqual(loop.block, 102, "a block counted one short, for now")
        loop.confirmed_at -= sf.CONFIRM_EVERY_S
        self.run_to(loop, 104)
        self.assertEqual([(a["was"], a["now"]) for a in self.events("stop_numbering_changed")], [(1, 0)])
        self.assertEqual((loop.ahead, loop.block), (0, 104))
        lines = journal(self).px[S.BTC]
        self.assertEqual({b: lines.get(b) for b in (103, 104)}, {103: 333, 104: 444}, "the lines after it are right")
        # The looks kept since the last that held may be of other blocks than their numbers (block 102's of 103): none
        # is kept to hold the node's answers by number against, or an honest node would turn reading back off.
        self.node.price[105] = 555
        loop.read_back_checked_at = None  # ten minutes on: the check of the node's answers by number is due again
        self.run_to(loop, 106)
        self.assertEqual((loop.read_back_off, self.events("stop_read_back_off")), (False, []))
        self.assertEqual(journal(self).px[S.BTC].get(105), 555)

    def out_of_step(self, answer):
        """The node's call, with its next answers by number to eth_getBlockByNumber made by `answer` (a server out of
        step with the one that answered the look); `left` of them."""
        real = self.node.call
        left = {"n": 0}

        def call(method, params=()):
            got = real(method, params)
            if method == "eth_getBlockByNumber" and params[0] != "latest" and left["n"]:
                left["n"] -= 1
                return answer(got)
            return got

        return call, left

    def test_a_look_that_does_not_hold_with_the_numbering_measured_as_before_is_an_alarm_once_an_hour(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        call, left = self.out_of_step(lambda got: {**got, "timestamp": hex(int(got["timestamp"], 16) + 7)})
        with mock.patch.object(self.node, "call", side_effect=call):
            left["n"] = 1
            self.run_to(loop, 102)
            wrong = self.events("stop_look_wrong")
            self.assertEqual([(a["block"], a["recorded"] - a["said"], a["alarm"]) for a in wrong], [(102, 7, "ALARM")])
            self.assertEqual(self.events("stop_numbering_changed"), [])
            self.assertEqual((loop.ahead, loop.block), (0, 102), "measured as before; the step by the head the node says")
            left["n"] = 1
            self.run_to(loop, 104)
            self.assertEqual(len(self.events("stop_look_wrong")), 1, "inside the hour: a line, not said again")
            self.assertEqual(len(self.events("stop_look_unconfirmed")), 2)
            loop.look_wrong_said_at -= sf.LOOK_WRONG_REALARM_S
            left["n"] = 1
            self.run_to(loop, 106)
            self.assertEqual(len(self.events("stop_look_wrong")), 2, "an hour on, said again")

    def test_a_block_the_node_does_not_have_yet_is_a_line_when_measured_as_before(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        call, left = self.out_of_step(lambda got: None)
        with mock.patch.object(self.node, "call", side_effect=call):
            left["n"] = 1
            self.run_to(loop, 102)
        self.assertEqual([a["recorded"] for a in self.events("stop_look_unconfirmed")], [None])
        self.assertEqual(self.events("stop_look_wrong") + self.events("stop_numbering_changed"), [])
        self.assertEqual((loop.ahead, loop.block), (0, 102))


class TheMeasure(unittest.TestCase):
    """The node's numbering is measured twice and taken when the two agree, each measure with the call's time the
    head's own (asked of the node by the head's number)."""

    def test_two_measures_that_do_not_agree_are_not_taken_and_two_that_agree_are(self):
        node = S.Node()
        node.head = 500
        real = node.call
        calls = {"n": 0}

        def call(method, params=()):
            if method == "eth_call":
                calls["n"] += 1
                node.ahead = calls["n"] % 2  # behind one address, a server of each kind in turn
            return real(method, params)

        self.assertIsNone(sk.numbering(call, lambda: int(call("eth_blockNumber"), 16)))
        node.calls.clear()
        node.ahead = 1
        self.assertEqual(sk.numbering(node.call, lambda: int(node.call("eth_blockNumber"), 16)), 1)
        self.assertEqual([m for m, _ in node.calls], S.MEASURE)
        self.assertEqual([p[0] for m, p in node.calls if m == "eth_getBlockByNumber"], [hex(500)] * 2, "the head's, by its number")

    def test_the_time_of_a_block_is_the_one_the_node_records_for_that_number(self):
        def every_number_the_head(method, params=()):
            return {"number": hex(600), "timestamp": hex(1_000_600)}

        self.assertIsNone(sk.block_time(every_number_the_head, 599), "an answer that is not the block asked for")
        self.assertEqual(sk.block_time(every_number_the_head, 600), 1_000_600)
        self.assertIsNone(sk.block_time(lambda method, params=(): None, 601), "a block it does not have")


class TheKeepersBatchHeld(unittest.TestCase):
    """The keeper's pass reads its head with its time (one request, as before), and its batch's first call is held
    against it; only a block that came between the two costs one more request, the node's record of the call's
    block by its number."""

    keeper = T.TheBatchedPass.keeper
    passes = T.TheBatchedPass.passes

    def test_a_block_between_the_head_and_the_call_costs_one_request_and_a_numbering_moved_hands_nothing_over(self):
        for measured, handed in ((1, True), (0, False)):
            chain = T.Chain(2, ahead=1)
            loop = mock.Mock()
            keeper = self.keeper(chain, loop=loop)
            self.passes(keeper, chain)
            keeper.ahead = measured
            real = chain.rpc
            moved = {"left": 1}

            def rpc(method, params=(), real=real, chain=chain, moved=moved):
                got = real(method, params)
                if method == "eth_getBlockByNumber" and params[0] == "latest" and moved["left"]:
                    moved["left"] -= 1
                    chain.head += 1  # a block comes between the pass's head and its first call
                return got

            chain.rpc = rpc
            loop.reset_mock()
            chain.calls.clear()
            _, said = self.passes(keeper, chain)
            by_number = [p[0] for m, p in chain.calls if m == "eth_getBlockByNumber" and p[0] != "latest"]
            self.assertEqual(len(by_number), 1, "one request for the block that came between")
            self.assertEqual({"batch" in call.kwargs for call in loop.reread.call_args_list}, {handed}, measured)
            self.assertEqual([e for e, _ in said if e == "numbering_changed"], [] if handed else ["numbering_changed"])
            self.assertEqual(keeper.ahead, 1 if handed else None)

    def test_with_no_block_between_and_the_numbering_as_measured_nothing_more_is_asked(self):
        chain = T.Chain(2, ahead=1)
        loop = mock.Mock()
        keeper = self.keeper(chain, loop=loop)
        self.passes(keeper, chain)
        chain.calls.clear()
        loop.reset_mock()
        self.passes(keeper, chain)
        self.assertEqual([p[0] for m, p in chain.calls if m == "eth_getBlockByNumber"], ["latest"])
        self.assertTrue(all("batch" in call.kwargs for call in loop.reread.call_args_list))


class ShortPast(S.Node):
    """A node that answers a block by its number only while it is the head or the one just behind it; an older one
    with its head (`older` = "its_head") or not at all ("nothing")."""
    older = "its_head"

    def batch(self, data, tag):
        if tag != "latest" and int(tag, 16) < self.head - 1:
            if self.older == "nothing":
                raise RuntimeError("eth_call: {'code': -32000, 'message': 'missing trie node'}")
            return super().batch(data, "latest")
        return super().batch(data, tag)


class ReadBackHeld(S.Base):
    """A block read back by its number is taken from a node whose answers by number were held against the loop's own
    looks within ten minutes: a block a look read at the head, asked for again by its number once it is behind."""

    def test_a_node_whose_answers_by_number_hold_is_checked_once_in_ten_minutes(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.price.update({101: 111, 102: 222, 103: 333, 104: 444, 105: 555})
        self.run_to(loop, 102)
        self.run_to(loop, 104)
        self.assertEqual([t for t, _ in self.node.batches()], ["latest", "latest", hex(101), hex(100), "latest", hex(103)],
                         "block 101 taken once block 100 held; block 103 taken with no check: a moment ago")
        loop.read_back_checked_at -= sf.READ_BACK_CHECK_EVERY_S
        self.run_to(loop, 106)
        self.assertEqual([t for t, _ in self.node.batches()][6:], ["latest", hex(105), hex(104)],
                         "ten minutes on, again: the newest block kept whose prices differ from the head's")
        self.assertEqual({b: px for b, px in journal(self).px[S.BTC].items() if px != self.node.oracle(b)}, {})
        self.assertFalse(loop.read_back_off)

    def test_reading_back_off_is_an_alarm_once_and_after_it_a_block_missed_is_a_gap_and_asks_nothing(self):
        self.node = R.TodaysPriceUnderAnOldNumber()
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.price.update({101: 111, 102: 222, 103: 333, 104: 444})
        self.run_to(loop, 102)
        self.assertEqual([(a["block"], a["alarm"]) for a in self.events("stop_read_back_off")], [(100, "ALARM")])
        before = len(self.node.batches())
        self.run_to(loop, 104)
        self.assertEqual([t for t, _ in self.node.batches()[before:]], ["latest"], "off: block 103 is not asked for")
        self.assertEqual(len(self.events("stop_read_back_off")), 1)
        j = journal(self)
        self.assertEqual(sorted(j.px[S.BTC]), [100, 102, 104])
        self.assertEqual([(lo, hi) for lo, hi, _ in j.gaps], [(101, 101), (103, 103)])
        self.assertIn("reading back is off", j.gaps[-1][2])

    def test_a_check_the_node_does_not_answer_for_its_block_takes_nothing_back_and_turns_nothing_off(self):
        """A node that answers the block just behind its head by its number, and an older one with its head (or with
        nothing): the check cannot be made. The block read back is a gap; reading back stays on, and the check is
        tried again at the next block read back."""
        for how in ("its_head", "nothing"):
            self.node = ShortPast()
            self.node.older = how
            self.said.clear()
            for f in self.dir.glob("prices*"):
                f.unlink()
            self.journal = None
            loop = self.loop(wallet=None)
            self.run_to(loop, 100)
            self.node.price.update({101: 111, 102: 222})
            self.run_to(loop, 102)
            self.assertEqual([(lo, hi) for lo, hi, _ in journal(self).gaps], [(101, 101)], how)
            self.assertEqual((loop.read_back_off, loop.read_back_checked_at), (False, None), how)
            self.assertEqual(self.events("stop_read_back_off"), [], how)
            self.assertEqual(len(self.events("stop_read_back_unchecked")), 1, how)

    def test_with_no_kept_block_that_can_tell_a_block_read_back_is_taken_with_no_check(self):
        """The price has not moved since the looks kept: a node with no past would say the same price, the true
        one. Nothing is asked for the check, and it waits for a block that can tell."""
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.run_to(loop, 102)
        self.assertEqual([t for t, _ in self.node.batches()], ["latest", "latest", hex(101)])
        self.assertIsNone(loop.read_back_checked_at)
        self.assertEqual(sorted(journal(self).px[S.BTC]), [100, 101, 102])


class TheBurntCall(S.Base):
    """A read of the oracle that fails may burn the gas left to the call: the reads after it fail with it. They, not
    the one that burned it, are read again one by one in the same step, at the head and in a block read back."""

    def oracle_alone(self) -> list[int]:
        return [decode(["uint32"], bytes.fromhex(p[0]["data"][2:]))[0] for m, p in self.node.calls
                if m == "eth_call" and p[0]["to"].lower() == sf.ORACLE_PX.lower()]

    def test_only_the_assets_after_the_first_that_failed_are_read_again_alone(self):
        self.node = R.BurningNode()
        loop = self.loop(wallet=None)
        self.node.positions.update({(S.EXAM, 7): 10, (S.EXAM, 9): 10})
        self.node.logs += [S.set_log(100, 950_000, 101, asset=7), S.set_log(100, 950_000, 101, asset=9, index=1)]
        self.run_to(loop, 100, 101)
        before = len(self.oracle_alone())
        self.run_to(loop, 102)
        self.assertEqual(self.oracle_alone()[before:], [9], "after the one that burned the call, not that one")
        self.node.price[104] = 900_000
        before = len(self.oracle_alone())
        self.run_to(loop, 104)
        self.assertEqual(self.oracle_alone()[before:], [9, 9], "at the head, and in block 103 read back")
        lines = journal(self)
        self.assertEqual({b: lines.px.get(9, {}).get(b) for b in (103, 104)}, {103: 1_000_000, 104: 900_000})
        self.assertNotIn(7, lines.px, "the one that burned the call stays unread")


class TheReadsOwnBlock(S.Base):
    """The events of the block a read is labelled with win over the read: a read of that block holds them all and
    agrees; one that does not was of an earlier block under a later number."""

    def late_read(self, loop):
        loop.inbox.put(("state", S.EXAM, 103, {(S.EXAM, S.BTC, False): (950_000, 101)}, {S.BTC: 1}))

    def test_a_stop_moved_in_the_block_of_a_read_keeps_its_move(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.run_to(loop, 101, 102)
        self.late_read(loop)
        self.node.logs.append(S.set_log(103, 940_000, 104))
        self.run_to(loop, 103)
        self.assertEqual(loop.triggers[(S.EXAM, S.BTC, False)]["px"], 940_000)

    def test_a_position_closed_in_the_block_of_a_read_keeps_its_side(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.run_to(loop, 101, 102)
        self.late_read(loop)
        self.node.logs.append(S.filled_log(103))
        self.node.positions[(S.EXAM, S.BTC)] = 0
        self.run_to(loop, 103)
        self.assertEqual(loop.signs[(S.EXAM, S.BTC)], 0)


class TheWatchAlonesBatchHeld(S.Base):
    def test_a_batch_whose_number_is_not_its_block_hands_nothing_over_and_is_measured_again(self):
        exams = [to_checksum_address("0x" + f"{i:040x}") for i in range(0x7201, 0x7204)]
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        keeper = sk.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, True, self.dir / "watch.json", 1)
        keeper.multicall = True
        keeper.ahead, keeper.ahead_at = 0, time.monotonic()
        keeper.rule_assets = {e: [S.BTC] for e in exams}
        for e in exams:
            self.node.stops[(e, S.BTC, False)] = (950_000, 95)
        self.node.ahead = 1  # since it was measured, the node numbers a call as the block to come
        with mock.patch.object(c, "rpc", side_effect=self.node.call):
            loop.reread_all(exams, keeper)
        self.assertIsNone(keeper.ahead, "measured again at the next pass")
        self.assertEqual(len(self.events("stops_batch_unread")), 1)
        held = list(loop.inbox.queue)
        self.assertEqual(sorted(m[1] for m in held), sorted(exams))
        self.assertEqual({m[2] for m in held}, {100}, "each read alone, at the loop's own block, not at 101")


if __name__ == "__main__":
    unittest.main()
