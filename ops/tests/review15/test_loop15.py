"""Review 15: the stops' loop and its journal after the fixes. Each test states the CORRECT behaviour."""
import json
import unittest

from ops import payout_review as pr
from ops.tests import test_stop_filler as S


class Restart(S.Base):
    def journal_of(self):
        j = pr.read_journals([self.dir / "prices.jsonl"])
        return j if j is not None else pr.Journal()

    def test_a_stop_standing_across_a_restart_has_a_price_in_every_block(self):
        first = self.loop()
        self.armed_stop(first)
        self.run_to(first, 101, 102)
        again = self.loop()
        self.node.head = 110
        again.step(110)
        again.apply_reread(S.EXAM, 110, {(S.EXAM, S.BTC, False): (950_000, 101)}, {S.BTC: 1})
        self.run_to(again, 111, 112)
        j = self.journal_of()
        holes = [b for b in range(103, 113) if b not in j.px.get(S.BTC, {}) and not j.gaps_in(b, b)]
        self.assertEqual(holes, [], "a block with neither a row nor a gap")
        in_gap = [b for b in range(103, 110) if b not in j.px.get(S.BTC, {})]
        self.assertEqual(sorted(set(in_gap)), list(range(103, 110)), "the blocks the loop was down are a gap, held by the review")
        for b in range(110, 113):
            self.assertIn(b, j.px.get(S.BTC, {}), f"block {b} after the restart has no price of the stop's asset")

    def test_a_block_written_twice_by_a_step_that_failed_and_ran_again_gives_one_price(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.run_to(loop, 101, 102, 103)
        loop.block = 101  # a step that failed half way is run again from the block after
        loop.step(103)
        j = self.journal_of()
        self.assertEqual(j.conflicts, {}, "a replayed block must not make the journal disagree with itself")

    def test_every_row_of_a_loop_that_follows_the_head_has_the_blocks_time(self):
        """The candle screen takes the minutes of blocks that have a time: a step over several blocks writes t=null."""
        loop = self.loop()
        self.armed_stop(loop)
        self.node.head = 130
        loop.head_t[130] = 1_800_000_130
        loop.step(130)
        rows = [json.loads(x) for f in sorted(self.dir.glob("prices-*.jsonl")) for x in f.read_text().splitlines()[1:] if x.strip()]
        nulls = [r["b"] for r in rows if "b" in r and r["t"] is None]
        self.assertEqual(nulls, [], "rows with no time are outside the candle screen, and nothing says so")


if __name__ == "__main__":
    unittest.main()
