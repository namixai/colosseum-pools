"""Controls for test_review20 (independent review 20 of #342): the same set-ups with the node behaving as measured (or a
dead asset failing alone) must pass on the PR's head, so the failures there are the findings and not the set-ups.
Run: COLOSSEUM_KEY_DIR=<an empty dir> COLOSSEUM_RPC_URL=http://127.0.0.1:9 python -m unittest ops.tests.review20.test_controls20 -v"""
import unittest
from unittest import mock

from ops import payout_review as pr
from ops import stop_filler as sf
from ops.tests import test_stop_filler as S
from ops.tests import test_sim_keeper as T
from ops.tests.review20 import test_review20 as R


class Controls(S.Base):
    def test_no_switch_take_known(self):
        self.node = R.ClampedNode()
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.run_to(loop, 101)
        self.node.logs.append(S.set_log(103, 1_100_000, 104, take=True))
        self.run_to(loop, 102, 103, 104)
        self.assertLessEqual(loop.block, self.node.head)
        self.assertIn((S.EXAM, S.BTC, True), loop.triggers)

    def test_no_switch_order_handed(self):
        self.node = R.ClampedNode()
        loop = self.loop(wallet=None)
        feed = loop.share()
        self.run_to(loop, 100, 101)
        placed = T.placed_log(S.EXAM, 105, 109, 103)
        self.node.logs.append(placed)
        self.run_to(loop, 102, 103, 104)
        handed = []
        while not feed.empty():
            handed += feed.get_nowait()[2]
        self.assertIn(placed, handed)

    def test_no_switch_journal_right(self):
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        self.run_to(loop, 100, 101)
        self.node.price.update({102: 222, 103: 333})
        self.run_to(loop, 102, 103)
        j = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual({b: px for b, px in j.px[S.BTC].items() if px != self.node.oracle(b)}, {})

    def test_right_label_keeps_the_take(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.run_to(loop, 101, 102)
        loop.inbox.put(("state", S.EXAM, 102, {(S.EXAM, S.BTC, False): (950_000, 101)}, {S.BTC: 1}))
        self.node.logs.append(S.set_log(103, 1_100_000, 104, take=True))
        self.run_to(loop, 103)
        self.assertIn((S.EXAM, S.BTC, True), loop.triggers)

    def test_dead_asset_failing_alone_live_one_sent(self):
        self.node.unread_assets = {7}
        loop = self.loop()
        self.node.positions.update({(S.EXAM, 7): 10, (S.EXAM, 9): 10})
        self.node.logs += [S.set_log(100, 950_000, 101, asset=7), S.set_log(100, 950_000, 101, asset=9, index=1)]
        self.run_to(loop, 100, 101)
        self.node.price[102] = 900_000
        self.run_to(loop, 102)
        fill9 = S.SEL["fillTrigger(uint32,bool)"] + sf.abi_encode(["uint32", "bool"], [9, False]).hex()
        self.assertIn(fill9, [d["data"] for d in self.node.sent()])

    def test_dead_asset_burning_old_way_live_one_sent(self):
        self.node = R.BurningNode()
        self.MULTICALL = False
        loop = self.loop()
        self.node.positions.update({(S.EXAM, 7): 10, (S.EXAM, 9): 10})
        self.node.logs += [S.set_log(100, 950_000, 101, asset=7), S.set_log(100, 950_000, 101, asset=9, index=1)]
        self.run_to(loop, 100, 101)
        self.node.price[102] = 900_000
        self.run_to(loop, 102)
        fill9 = S.SEL["fillTrigger(uint32,bool)"] + sf.abi_encode(["uint32", "bool"], [9, False]).hex()
        self.assertIn(fill9, [d["data"] for d in self.node.sent()])
        blind = self.events("stop_watch_blind")
        self.assertTrue(blind)


class KeeperControl(unittest.TestCase):
    keeper = T.TheBatchedPass.keeper
    passes = T.TheBatchedPass.passes

    def test_measured_right_label_is_the_head(self):
        chain = T.Chain(2, ahead=1)
        loop = mock.Mock()
        sk = self.keeper(chain, loop=loop)
        self.passes(sk, chain)
        loop.reset_mock()
        head = chain.head
        self.passes(sk, chain)
        labels = {c.args[0]: c.kwargs["batch"].block_of[c.args[0]] for c in loop.reread.call_args_list if "batch" in c.kwargs}
        self.assertTrue(labels)
        self.assertEqual({e: b for e, b in labels.items() if b > head}, {})


if __name__ == "__main__":
    unittest.main()
