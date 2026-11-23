"""The journals of prices as the review reads them (ops/payout_review.py `Journal`, `read_journals`): one file a day with
a header, a line of the wrong shape a hole and never the end of the review, a block written twice with two prices a
doubt, and two journals from two machines: a block held where either has it, and the same price where both have it."""
import json
import pathlib
import shutil
import tempfile
import unittest

from ops import payout_review as pr
from ops import stop_filler as sf
from ops.tests import test_payout_review as T

HEAD = {"journal": sf.JOURNAL_SCHEMA, "chain": 998, "factory": "0x" + "fa" * 20, "label": "sim1"}
DAY = 86_400


def row(b, px, t=None):
    return json.dumps({"b": b, "t": t, "w": 0, "px": {"3": px}})


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_the_review_and_the_loop_name_the_journal_kind_alike(self):
        self.assertEqual(pr.JOURNAL_SCHEMA, sf.JOURNAL_SCHEMA)

    def test_one_file_a_day_each_with_its_header_and_only_the_days_wanted_are_read(self):
        now = [10 * DAY + 5]
        j = sf.PriceJournal(self.dir / "prices-sim1.jsonl", clock=lambda: now[0], header=HEAD)
        j.add(1, {3: 7})
        now[0] += DAY
        j.add(2, {3: 8})
        now[0] += 5 * DAY
        j.add(3, {3: 9})
        j.close()
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(names, ["prices-sim1-1970-01-11.jsonl", "prices-sim1-1970-01-12.jsonl",
                                 "prices-sim1-1970-01-17.jsonl", "prices-sim1.jsonl"])
        for name in names[:3]:
            self.assertEqual(json.loads((self.dir / name).read_text().splitlines()[0])["chain"], 998)
        got = pr.read_journals([self.dir / "prices-sim1.jsonl"], HEAD, lo_t=10 * DAY, hi_t=10 * DAY + 10)
        self.assertEqual(got.px, {3: {1: 7, 2: 8}}, "the day before and after the stop's time, not the week after")
        again = sf.PriceJournal(self.dir / "prices-sim1.jsonl", clock=lambda: now[0], header=HEAD)
        self.assertEqual(again.last, 3, "opened again it knows its last block from the newest day file")
        again.close()

    def test_a_journal_of_another_chain_or_factory_is_refused(self):
        for other in ({"chain": 999}, {"factory": "0x" + "fb" * 20}, {"label": "sim2"}, {"journal": "something/1"}):
            text = json.dumps({**HEAD, **other}) + "\n" + row(5, 7) + "\n"
            got = pr.Journal.parse(text, HEAD)
            self.assertEqual(got.px, {}, other)
            self.assertTrue(got.notes and "not read" in got.notes[0], other)
        self.assertEqual(pr.Journal.parse(json.dumps(HEAD) + "\n" + row(5, 7) + "\n", HEAD).px, {3: {5: 7}})

    def test_a_missing_second_journal_holds_nothing_on_its_own(self):
        (self.dir / "a.jsonl").write_text(row(5, 7) + "\n")
        got = pr.read_journals([self.dir / "a.jsonl", self.dir / "b.jsonl"])
        self.assertEqual(got.px, {3: {5: 7}})
        self.assertIn("b.jsonl", got.notes[0])
        self.assertIsNone(pr.read_journals([self.dir / "b.jsonl"]))


class Shapes(unittest.TestCase):
    def test_a_line_of_the_wrong_shape_is_a_hole_and_is_counted(self):
        got = pr.load_prices(row(4, 7) + '\n{"b": 5, "px": {"3": "x"}}\n{"b": true, "px": {}}\n' + row(6, 7) + "\n")
        self.assertEqual((sorted(got.px[3]), got.bad), ([4, 6], 2))

    def test_a_block_written_twice_alike_is_one_block_and_with_two_prices_a_conflict(self):
        got = pr.load_prices(row(4, 7) + "\n" + row(4, 7) + "\n")
        self.assertEqual(got.conflicts, {})
        got = pr.load_prices(row(4, 7) + "\n" + row(4, 8) + "\n")
        self.assertIn(4, got.conflicts[3])


class TwoJournals(unittest.TestCase):
    """The MissedTouches world: a long, a stop at 950 000 armed at 1 003, carried out at 1 305."""

    def judged(self, a: dict, b: dict | None):
        mt = T.MissedTouches()
        low = set(range(1_300, 1_306))
        ch, _ = mt.world(low)
        j = pr.Journal()
        for name, rows in (("keeper", a), ("second", b)):
            if rows is not None:
                j.add_text("\n".join(row(k, v) for k, v in sorted(rows.items()) if 1_002 <= k <= 1_400) + "\n", None, name)
        ch.d["journal"] = j
        ch.d["journalCheck"] = pr.check_journal(list(ch.d["exam_events"]), ch.d["exam_events"], j, 2_000, None)
        return pr.review(ch.d, T.POOL, 0), mt, low

    def truth(self):
        mt = T.MissedTouches()
        return mt.prices(set(range(1_300, 1_306)), until=2_001)

    def test_each_journal_whole_and_alike_is_clean(self):
        got, _, _ = self.judged(self.truth(), self.truth())
        self.assertEqual((got["verdict"], got["action"]), ("clean", "approve"))

    def test_a_restart_of_one_writer_is_covered_by_the_other(self):
        truth = self.truth()
        a = {b: px for b, px in truth.items() if not 1_100 <= b <= 1_150}   # the keeper's deploy
        b = {b: px for b, px in truth.items() if not 1_200 <= b <= 1_220}   # the second machine's reboot
        got, _, _ = self.judged(a, b)
        self.assertEqual((got["verdict"], got["action"]), ("clean", "approve"))

    def test_a_block_neither_has_is_a_hole_and_holds(self):
        truth = self.truth()
        a = {b: px for b, px in truth.items() if b != 1_150}
        got, _, _ = self.judged(a, dict(a))
        self.assertEqual(got["action"], "hold")

    def test_one_falsified_block_is_caught_by_the_other_journal(self):
        """The review's own M1 case: a block where one journal hides the touch the oracle had. Without an archive node a
        sample cannot find it; the second writer's own read of the same block can."""
        mt = T.MissedTouches()
        truth = {**self.truth(), 1_150: mt.LOW}
        lie = {**truth, 1_150: mt.MARK}
        for a, b in ((lie, truth), (truth, lie)):
            got, _, _ = self.judged(a, b)
            self.assertEqual(got["action"], "hold")
            self.assertIn("disagree", json.dumps(got["reasons"]))

    def test_a_touch_in_either_journal_alone_is_a_touch(self):
        mt = T.MissedTouches()
        truth = self.truth()
        a = {**{b: px for b, px in truth.items() if b != 1_150}}
        b = {**{k: v for k, v in truth.items() if k != 1_149}, 1_150: mt.LOW}
        got, _, _ = self.judged(a, b)
        self.assertEqual(got["action"], "hold")


if __name__ == "__main__":
    unittest.main()
