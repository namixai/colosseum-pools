"""Independent review 20 of #342 (one call a look, reads by the hundred; head 05cbdc3d). Each test states the CORRECT
behaviour; a test that fails on the PR's head shows the finding it is named after. Fakes are the PR's own
(test_stop_filler.Node, test_sim_keeper.Chain); no key, no live node. What was decided on them: test_decisions20.py.
Run: COLOSSEUM_KEY_DIR=<an empty dir> COLOSSEUM_RPC_URL=http://127.0.0.1:9 python -m unittest ops.tests.review20.test_review20 -v"""
import unittest
from unittest import mock

from eth_abi import decode

from ops import payout_review as pr
from ops import stop_filler as sf
from ops.tests import test_stop_filler as S
from ops.tests import test_sim_keeper as T


class ClampedNode(S.Node):
    """eth_getLogs answers only up to the node's own head, as a node does for a range that runs past it (it cuts the
    range at its head and says nothing)."""

    def call(self, method, params=()):
        out = super().call(method, params)
        if method == "eth_getLogs":
            out = [l for l in out if int(l["blockNumber"], 16) <= self.head]
        return out


class StrictNode(S.Node):
    """eth_getLogs refuses a range that runs past the node's head."""

    def call(self, method, params=()):
        if method == "eth_getLogs" and int(params[0]["toBlock"], 16) > self.head:
            self.calls.append((method, params))
            raise RuntimeError("eth_getLogs: {'code': -32000, 'message': 'block range extends beyond current head'}")
        return super().call(method, params)


class NumberingChangesBetweenMeasures(S.Base):
    """F1. The node's numbering is measured at the start and every 10 minutes; between two measures every look is
    counted back by the last one. A node (or the backend behind the same URL) that numbers a call differently since
    the measure, or a measure that met a backend one block behind, shifts every head of the loop by one."""

    def test_measured_0_node_now_numbers_as_next_block_no_event_is_lost(self):
        self.node = ClampedNode()
        self.node.ahead = 0
        loop = self.loop(wallet=None)  # measured: 0
        self.armed_stop(loop)          # a stop on BTC set in block 100
        self.run_to(loop, 101)
        self.node.ahead = 1            # since the measure: a call at latest says the next block's number
        self.node.logs.append(S.set_log(103, 1_100_000, 104, take=True))  # a take set in block 103
        self.run_to(loop, 102, 103, 104)
        self.assertLessEqual(loop.block, self.node.head, "the loop's last block is past the node's head")
        self.assertIn((S.EXAM, S.BTC, True), loop.triggers, "the take set in block 103 is unknown to the loop")

    def test_measured_0_node_now_numbers_as_next_block_orders_are_handed_to_the_keeper(self):
        self.node = ClampedNode()
        self.node.ahead = 0
        loop = self.loop(wallet=None)
        feed = loop.share()
        self.run_to(loop, 100, 101)
        self.node.ahead = 1
        placed = T.placed_log(S.EXAM, 105, 109, 103)
        self.node.logs.append(placed)
        self.run_to(loop, 102, 103, 104)
        handed = []
        while not feed.empty():
            handed += feed.get_nowait()[2]
        self.assertIn(placed, handed, "the order placed in block 103 never reaches the keeper's fast loop: it lapses")

    def test_measured_0_node_now_numbers_as_next_block_a_node_that_refuses_keeps_the_loop_going(self):
        self.node = StrictNode()
        self.node.ahead = 0
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.run_to(loop, 101)
        self.node.ahead = 1
        for h in range(102, 106):
            try:
                self.run_to(loop, h)
            except RuntimeError:
                pass
        self.assertGreaterEqual(loop.block, 104, "every step fails until the next measure: no event, no reread applied")

    def test_measured_1_node_now_numbers_as_head_no_price_under_another_blocks_number(self):
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        self.run_to(loop, 100, 101)
        self.node.ahead = 0            # since the measure: a call at latest says the head's own number
        self.node.price.update({102: 222, 103: 333})
        self.run_to(loop, 102, 103)
        j = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        wrong = {b: px for b, px in j.px[S.BTC].items() if px != self.node.oracle(b)}
        self.assertEqual(wrong, {}, "a price of another block written under a block's number")


class KeeperBatchPastTheHead(unittest.TestCase):
    """F1 on the keeper's side: `read_all` notices a call that says a block BEFORE the head read just before it, never
    one past it. A batch labelled one block late is held by the stops' loop until that block's events are in, then
    applied as if read after them."""

    keeper = T.TheBatchedPass.keeper
    passes = T.TheBatchedPass.passes

    def test_a_call_that_says_a_block_past_the_head_hands_no_stops_over(self):
        chain = T.Chain(2, ahead=1)
        loop = mock.Mock()
        sk = self.keeper(chain, loop=loop)
        self.passes(sk, chain)
        sk.ahead = 0  # measured on a backend one block behind; the node numbers a call as the block to come
        loop.reset_mock()
        head = chain.head
        self.passes(sk, chain)
        labels = {c.args[0]: c.kwargs["batch"].block_of[c.args[0]] for c in loop.reread.call_args_list if "batch" in c.kwargs}
        self.assertEqual({e: b for e, b in labels.items() if b > head}, {},
                         f"handed over as read in a block after the head {head}")

    def test_what_a_late_label_does_in_the_loop_a_take_set_in_that_block_is_dropped(self):
        """The consequence, in the stops' loop: the exam read at block 102 (no take yet) handed in labelled 103; the
        take set in block 103 is dropped by that read once block 103's events are in."""
        case = S.Base("run")
        case.setUp()
        try:
            loop = case.loop(wallet=None)
            case.armed_stop(loop)
            case.run_to(loop, 101, 102)
            loop.inbox.put(("state", S.EXAM, 103, {(S.EXAM, S.BTC, False): (950_000, 101)}, {S.BTC: 1}))
            case.node.logs.append(S.set_log(103, 1_100_000, 104, take=True))
            case.run_to(loop, 103)
            self.assertIn((S.EXAM, S.BTC, True), loop.triggers, "the take set in block 103 dropped by a read of block 102")
        finally:
            case.doCleanups()


class AMeasureOnABackendBehind(unittest.TestCase):
    """F1, how the measure itself goes wrong behind one URL served by several backends (a public or paid node): the
    two reads of the head on backends at the head, the call between them on one a block behind. Every backend numbers
    a call as the next block (1); the measure takes 0."""

    def test_the_measure_is_not_taken_from_a_call_one_block_behind(self):
        from ops import sim_keeper as sk
        node = S.Node()
        node.head, node.ahead = 500, 1
        real = node.call

        def call(method, params=()):
            if method == "eth_call":
                node.head = 499           # the backend that answered the call is one block behind
                try:
                    return real(method, params)
                finally:
                    node.head = 500
            return real(method, params)

        got = sk.numbering(call, lambda: int(call("eth_blockNumber"), 16))
        self.assertIn(got, (None, 1), "every backend numbers a call as the next block; the measure says 0")


class TodaysPriceUnderAnOldNumber(S.Node):
    """Q5: a node that answers a call at an old block's number with that block's number and time, and the state (the
    oracle) of its head: a node with no past of the oracle's state."""

    def batch(self, data, tag):
        if tag == "latest":
            return super().batch(data, tag)
        asked = int(tag, 16)
        saved, self.ignores_tag = self.ignores_tag, True       # every read at the head ...
        try:
            out = decode(["(bool,bytes)[]"], bytes.fromhex(super().batch(data, tag)[2:]))[0]
        finally:
            self.ignores_tag = saved
        from eth_abi import encode
        out = [(True, encode(["uint256"], [asked + self.ahead])), (True, encode(["uint256"], [self.time_of(asked)]))] + list(out[2:])
        return "0x" + encode(["(bool,bytes)[]"], [out]).hex()  # ... under the old block's number and time


class Q5ReadBack(S.Base):
    def test_a_block_read_back_from_a_node_with_no_past_is_a_gap_not_todays_price(self):
        self.node = TodaysPriceUnderAnOldNumber()
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.price.update({101: 111, 102: 222})
        self.run_to(loop, 102)  # block 101 not looked at: read back by its number
        j = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertNotEqual(j.px[S.BTC].get(101), 222, "block 101's line carries block 102's price")


class ANewExamWithManyAssets(unittest.TestCase):
    """F4 (Q3): an exam met for the first time whose stops' reads do not fit one call (33 assets: 1 + 3 * 33 = 100 reads
    > 98) has them cut across calls of different blocks; its label stays the block of the first call (its rules)."""

    keeper = T.TheBatchedPass.keeper
    passes = T.TheBatchedPass.passes

    def test_its_stops_read_across_calls_are_not_handed_over_as_one_block(self):
        chain = T.Chain(1, assets=list(range(1, 34)))
        chain.moves = True                       # a block between two calls
        loop = mock.Mock()
        sk = self.keeper(chain, loop=loop)
        self.passes(sk, chain)
        exam = list(chain.exams)[0]
        handed = [c for c in loop.reread.call_args_list if c.args[0] == exam and "batch" in c.kwargs]
        calls_with_its_stops = [i for i, b in enumerate(chain.batches) if any(to == exam and s != T.sel("rules()")[:10]
                                                                              and s in (T.sel("stopOf(uint32)")[:10],)
                                                                              for to, s in b)]
        self.assertEqual(handed, [], f"handed over at one block, its stops read in calls {calls_with_its_stops}")


class BurningNode(S.Node):
    """A read of the oracle for an asset it has no price of burns the gas left to the call: in a batch, every read after
    it fails too (as the PR's own comments on `_in_order` and `violation()` say); alone, it fails alone."""
    burns = {7}

    def batch(self, data, tag):
        self._in_batch, self._burnt = True, False
        try:
            return super().batch(data, tag)
        finally:
            self._in_batch = False

    def read(self, to, data, block, sender=None):
        if getattr(self, "_in_batch", False) and self._burnt:
            raise RuntimeError("out of gas")
        if to.lower() == sf.ORACLE_PX.lower() and decode(["uint32"], bytes.fromhex(data[2:]))[0] in self.burns:
            if getattr(self, "_in_batch", False):
                self._burnt = True
            raise RuntimeError("precompile: no such asset")
        return super().read(to, data, block, sender)


class OneDeadAssetInTheLook(S.Base):
    """F3. Every exam's stops share one look. A stop on an asset with no oracle price that sorts before another
    exam's stop on a live asset takes that asset's price out of every look; it is not asked again alone."""

    def test_a_stop_on_a_live_asset_after_a_dead_one_is_still_sent(self):
        self.node = BurningNode()
        loop = self.loop()
        self.node.positions.update({(S.EXAM, 7): 10, (S.EXAM, 9): 10})
        self.node.logs += [S.set_log(100, 950_000, 101, asset=7), S.set_log(100, 950_000, 101, asset=9, index=1)]
        self.run_to(loop, 100, 101)
        self.node.price[102] = 900_000   # every live asset's oracle under the stops
        self.run_to(loop, 102)
        sent = [d["data"] for d in self.node.sent()]
        fill9 = S.SEL["fillTrigger(uint32,bool)"] + sf.abi_encode(["uint32", "bool"], [9, False]).hex()
        self.assertIn(fill9, sent, "the stop on asset 9 reached and not sent: its price is never read")


if __name__ == "__main__":
    unittest.main()
