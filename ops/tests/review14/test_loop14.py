"""Independent review of PR 338: the stops' loop and its journal. Each test states the CORRECT behaviour; red = defect."""
import json
import unittest

from ops import payout_review as pr
from ops.tests import test_stop_filler as S


class Restart(S.Base):
    def rows(self):
        return [json.loads(x) for x in pr.journal_text(self.dir / "prices.jsonl").splitlines() if x.strip()]

    def test_a_stop_standing_across_a_restart_has_a_price_in_every_block_once_the_loop_runs_again(self):
        """After a restart the loop knows no stop until the keeper's slow pass rereads the exams (`reread`). Its first
        steps journal `px: {}`: every payout of an exam whose stop stood across any restart or deploy has a hole in
        the journal that never heals, and is held for good. The prices a stop would need are the platform's assets',
        known from the start."""
        first = self.loop()
        self.armed_stop(first)                       # stop set in 100, armed from 101
        self.run_to(first, 101, 102)
        again = self.loop()                          # the keeper restarts (a deploy)
        self.node.head = 110
        again.step(110)                              # its first step, before the slow pass has read the exams
        again.apply_reread(S.EXAM, 110, {(S.EXAM, S.BTC, False): (950_000, 101)}, {S.BTC: 1})
        self.run_to(again, 111, 112)
        j = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        holes = [b for b in range(110, 113) if b not in j.px.get(S.BTC, {})]
        self.assertEqual(holes, [], "blocks 110.. of a standing stop with no price in the journal: the payout is held for good")

    def test_a_step_after_the_head_ran_ahead_leaves_a_gap_line_for_each_block_with_no_row(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.run_to(loop, 101)
        self.node.head = 140
        loop.step(140)
        j = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        missing = [b for b in range(101, 141) if b not in j.px.get(S.BTC, {}) and not j.gaps_in(b, b)]
        self.assertEqual(missing, [], "a block with neither a row nor a gap is a silent hole")


if __name__ == "__main__":
    unittest.main()
