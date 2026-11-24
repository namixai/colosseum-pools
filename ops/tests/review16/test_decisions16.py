"""What was decided on the findings of review 16 of PR 338, beside the reviewer's own tests (test_review16.py)."""
import json
import unittest
from unittest import mock

from ops import payout_review as pr
from ops.tests import test_payout_review as T
from ops.tests.review15.test_review15 import ScanWorld


class EveryBuyersFunding(ScanWorld):
    """Two buyers funded by one wallet are a pair by the funding of both, and the second has no payout of its own: once
    a payout waits, the funding of every buyer of the factory is read back to the deployment, and until it is, each
    payout waiting is held. With no payout waiting, none of it is read."""

    def buyer(self, who, block, exam):
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(exam), self.word(who), self.word(T.addr(0xB0))],
                           "blockNumber": block})

    def counted(self):
        calls, real = [], self.fake()

        def logs(address, topics, lo, hi):
            calls.append((lo, hi, len(topics[2]) if len(topics) > 2 and topics[2] else 0))
            return real(address, topics, lo, hi)
        return calls, logs

    def test_no_payout_waiting_reads_no_funding_at_all(self):
        self.buyer(T.addr(0x101), 5_000, T.addr(0x201))
        s = self.scan(5_010, part=5_010)
        calls, logs = self.counted()
        with mock.patch.object(pr, "logs", logs):
            self.assertEqual(s.read_links([], self.USDC), [])
        self.assertEqual(calls, [], "a buyer with no payout waiting anywhere costs the node nothing")
        self.assertFalse(s.links_read(T.addr(0x101)))

    def test_a_payout_waits_until_every_buyers_funding_is_read_not_only_its_own_traders(self):
        a, b, funder = T.addr(0x101), T.addr(0x102), T.addr(0xF0)
        self.buyer(a, 2_000, T.addr(0x201))
        self.buyer(b, 5_000, T.addr(0x202))
        self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(b)], "blockNumber": 4_500})
        s = self.scan(5_010, part=5_010)
        clock = iter([0.0, 0.0, 99.0, 99.0])  # two chunks, and the time is out: a's blocks read, b's not
        with mock.patch.object(pr, "logs", self.fake()), mock.patch.object(pr.time, "monotonic", lambda: next(clock)):
            self.assertEqual(s.read_links([a], self.USDC, until=1.0), [a], "a's payout is held while b is not read")
        self.assertEqual((s.links_read(a), s.links_read(b)), (True, False))
        self.assertEqual(self.links(s, [a]), [])
        self.assertTrue(s.links_read(b))
        self.assertIn((funder, b, 4_500), {(t["from"], t["to"], t["block"]) for t in s.d["transfers"]})

    def test_a_buyer_first_seen_after_the_others_were_read_is_read_from_the_deployment_too(self):
        a, c, funder = T.addr(0x101), T.addr(0x103), T.addr(0xF0)
        self.buyer(a, 2_000, T.addr(0x201))
        s = self.scan(2_010, part=2_010)
        self.assertEqual(self.links(s, [a]), [])
        self.buyer(c, 6_000, T.addr(0x203))
        self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(c)], "blockNumber": 15})
        with mock.patch.object(pr, "logs", self.fake()), \
                mock.patch.object(pr.c, "rpc", side_effect=lambda m, p=(): {"timestamp": hex(1_000 * int(p[0], 16))}):
            s.read(self.POOLS, self.USDC, 6_010, part=6_010)
        self.assertFalse(s.links_read(c))
        calls, logs = self.counted()
        with mock.patch.object(pr, "logs", logs):
            self.assertEqual(s.read_links([a], self.USDC), [])
        to = 5_510  # the scan's step that first saw the buyer began at 5 511: from there on its transfers are the scan's
        self.assertEqual(s.d["links"][c]["to"], to)
        self.assertEqual([(lo, hi, n) for lo, hi, n in calls], [(1 + 1_000 * k, min(1_000 * (k + 1), to), 1) for k in range(6)],
                         "the new buyer alone, from the deployment's block, a chunk a call")
        self.assertIn((funder, c, 15), {(t["from"], t["to"], t["block"]) for t in s.d["transfers"]})

    def test_buyers_go_a_hundred_to_a_call(self):
        for k in range(250):  # each in a block of its own, all first seen in the scan's step from block 10 001
            self.buyer(T.addr(0x1000 + k), 10_001 + k, T.addr(0x5000 + k))
        s = self.scan(10_300, part=10_300)
        calls, logs = self.counted()
        with mock.patch.object(pr, "logs", logs):
            self.assertEqual(s.read_links([T.addr(0x1000)], self.USDC), [])
        self.assertEqual(len(calls), 30, "10 chunks of 1 000 blocks, 250 buyers in three calls each")
        self.assertEqual(max(n for _, _, n in calls), pr.chk.ADDRESS_BATCH)


class EachAlarmAlone(unittest.TestCase):
    """An alarm that raises is that alarm's failure: the ones after it are said, and it is said again at the next pass."""

    def test_an_alarm_that_raised_is_not_remembered_and_the_others_are(self):
        import pathlib
        import tempfile
        from ops.tests.review16.test_review16 import Alarms16
        d = pathlib.Path(tempfile.mkdtemp())
        seen, said, broken = [], {}, [True]

        def alarm(event, **fields):
            seen.append(fields["id"])
            if fields["id"] == 0 and broken:
                raise RuntimeError("the channel's own fault")
            return True
        Alarms16().run_pass(d, alarm, said)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(sorted(k for k, v in said.items() if isinstance(v, dict)), ["0xP-1", "0xP-2"],
                         "the one that raised is not remembered as said")
        self.assertEqual(sorted(p.name for p in d.glob("0xP-*.json")), ["0xP-0.json", "0xP-1.json", "0xP-2.json"])
        broken.clear()
        del seen[:]
        Alarms16().run_pass(d, alarm, said)
        self.assertEqual(seen, [0], "said again at the next pass, the others not twice")


def row(b, px, asset=3):
    return json.dumps({"b": b, "t": 1_000 * b, "w": 0, "px": {str(asset): px}})


class BadRows(unittest.TestCase):
    """A line that is no line of a journal is a bad line: its block is not in that journal, and the pass goes on."""

    def test_a_line_too_long_nested_too_deep_or_with_a_constant_for_a_number_is_a_bad_line(self):
        bads = ['{"b":5,"px":{"3":NaN}}', '{"b":5,"w":Infinity,"px":{"3":7}}', '{"b":5,"t":-Infinity,"px":{"3":7}}',
                '{"b":5,"px":{"3":7},"x":"' + "y" * pr.LINE_MAX + '"}']
        for bad in bads:
            for want in (None, {3: [(1, 10)]}):
                j = pr.Journal(want)
                j.add_lines([row(4, 7) + "\n", bad + "\n", row(6, 7) + "\n"])
                self.assertEqual((sorted(j.px[3]), j.bad), ([4, 6], 1), (bad[:30], want))
        # Brackets nested past the parser's depth: how deep that is goes with the interpreter (RecursionError at about
        # a thousand on some, a plain syntax error on others), so the parser's answer is given here.
        real = json.loads

        def loads(text, **kw):
            if '"deep"' in text:
                raise RecursionError("maximum recursion depth exceeded")
            return real(text, **kw)
        with mock.patch.object(pr.json, "loads", loads):
            j = pr.Journal()
            j.add_lines([row(4, 7) + "\n", '{"b":5,"deep":1,"px":{"3":7}}\n', row(6, 7) + "\n"])
        self.assertEqual((sorted(j.px[3]), j.bad), ([4, 6], 1))
        for nested in ("[" * 50_000, '{"b":5,"px":{"3":' + "[" * 5_000 + "}}"):
            j = pr.Journal()
            j.add_lines([row(4, 7) + "\n", nested + "\n", row(6, 7) + "\n"])
            self.assertEqual(sorted(j.px[3]), [4, 6], "whichever way the parser refuses it, the lines around it are read")
        fits = '{"b":5,"px":{"3":7},"x":"' + "y" * (pr.LINE_MAX - 100) + '"}'
        j = pr.Journal()
        j.add_lines([fits + "\n"])
        self.assertEqual((sorted(j.px[3]), j.bad), ([5], 0), "a line within the limit is read")

    def test_a_bad_line_of_the_second_journal_leaves_its_block_on_one_journal(self):
        j = pr.Journal()
        j.add_text("\n".join(row(b, 7) for b in (4, 5, 6)) + "\n")
        j.add_text(row(4, 7) + "\n" + '{"b":5,"t":1,"w":0,"px":{"3":Infinity}}' + "\n" + row(6, 7) + "\n", None, "second")
        self.assertEqual((j.single_in(3, 4, 6), j.bad), ([5], 1), "held as resting on one journal, never clean")


class WhatAJournalKeeps(unittest.TestCase):
    """Beside its prices a journal keeps the gaps that touch a stop's time and its first notes; the rest are counted."""

    @staticmethod
    def gap(a, b, why="x"):
        return json.dumps({"gap": [a, b], "why": why}) + "\n"

    def test_a_gap_is_kept_where_it_touches_a_stops_time_and_counted_where_it_does_not(self):
        j = pr.Journal({3: [(1_000, 1_400)], 4: [(3_000, 3_010)]})
        j.add_lines([self.gap(900, 999), self.gap(990, 1_000), self.gap(1_100, 1_101), self.gap(1_400, 1_500),
                     self.gap(1_401, 2_999), self.gap(2_000, 3_000), self.gap(3_011, 9_000), self.gap(1, 9_000, "y" * 500)])
        self.assertEqual([(a, b) for a, b, _ in j.gaps], [(990, 1_000), (1_100, 1_101), (1_400, 1_500), (2_000, 3_000), (1, 9_000)])
        self.assertEqual(j.gaps_more, 3)
        self.assertEqual(j.gaps_in(1_000, 1_400)[:3], ["990..1000 (x)", "1100..1101 (x)", "1400..1500 (x)"])
        self.assertEqual(len(j.gaps[-1][2]), pr.WHY_MAX, "a gap's reason is cut")
        by_hand = pr.Journal()
        by_hand.add_lines(self.gap(10 * k, 10 * k + 1) for k in range(pr.GAPS_KEPT + 5))
        self.assertEqual((len(by_hand.gaps), by_hand.gaps_more), (pr.GAPS_KEPT, 5), "read by hand: every gap, to the limit")

    def test_the_notes_are_the_first_hundred_and_a_count(self):
        import pathlib
        import shutil
        import tempfile
        from ops.tests.test_price_journals import HEAD
        j = pr.Journal()
        j.add_lines(('{"journal":"x%d"}\n' % k for k in range(250)), {"label": "a"}, "second")
        self.assertEqual((len(j.notes), j.notes_more), (pr.NOTES_KEPT, 150))
        j.note("the one that matters", first=True)
        self.assertEqual((j.notes[0], len(j.notes), j.notes_more), ("the one that matters", pr.NOTES_KEPT, 151))
        # The files of the second folder that are not day files are said the same way: the first hundred, and a count.
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "second").mkdir()
        (d / "prices-sim1-1970-01-01.jsonl").write_text(json.dumps(HEAD) + "\n" + row(4, 7) + "\n")
        for k in range(150):
            (d / "second" / f"watch-prices-sim1-abc{k // 100}-ef-{k % 100:02d}.jsonl").write_text("x\n")
        got = pr.read_journals([d / "prices-sim1.jsonl", d / "second" / "watch-prices-sim1.jsonl"], HEAD)
        self.assertEqual((len(got.notes), got.notes_more, sorted(got.px[3])), (pr.NOTES_KEPT, 50, [4]))


class SecondJournalNotRead(unittest.TestCase):
    """Whatever goes wrong reading the second journal is that journal not read: its blocks rest on one journal (held),
    the service says it, and the pass goes on to write its documents."""

    def folder(self):
        import pathlib
        import shutil
        import tempfile
        from ops.tests.test_price_journals import HEAD
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "second").mkdir()
        text = json.dumps(HEAD) + "\n" + "\n".join(row(b, 7) for b in (4, 5, 6)) + "\n"
        (d / "prices-sim1-1970-01-01.jsonl").write_text(text)
        (d / "second" / "watch-prices-sim1-1970-01-01.jsonl").write_text(text)
        return d / "prices-sim1.jsonl", d / "second" / "watch-prices-sim1.jsonl", HEAD

    def test_its_reading_raised_and_the_blocks_rest_on_the_keepers_journal_alone(self):
        from ops import review_service as rs
        keeper, second, head = self.folder()
        real = pr.file_lines

        def lines(files):
            if files and files[0].parent.name == "second":
                raise RuntimeError("anything at all")
            return real(files)
        with mock.patch.object(pr, "file_lines", lines):
            got = pr.read_journals([keeper, second], head)
            unread = []
            with mock.patch.object(rs.core, "log"):
                self.assertIsInstance(rs.read_prices(keeper, head, second, None, None, None, unread), pr.Journal)
        self.assertEqual((sorted(got.px[3]), got.single_in(3, 4, 6)), ([4, 5, 6], [4, 5, 6]))
        self.assertEqual(got.unread, ["watch-prices-sim1.jsonl: RuntimeError"])
        self.assertIn("was not read to its end (RuntimeError)", got.notes[0])
        self.assertEqual(unread, got.unread, "the service is told")
        both = pr.read_journals([keeper, second], head)
        self.assertEqual((both.unread, both.single_in(3, 4, 6)), ([], []))
        # The keeper's own journal is the one the review cannot do without: its failure is the pass's, as before.
        with mock.patch.object(pr, "file_lines", side_effect=RuntimeError("the keeper's")):
            with self.assertRaises(RuntimeError):
                pr.read_journals([keeper, second], head)

    def test_the_service_says_it_once_every_six_hours_while_it_lasts(self):
        from ops import review_service as rs
        raised, said = [], {}
        alarm = lambda event, **f: raised.append((event, f["errors"])) or True  # noqa: E731
        rs.second_unread([], 1_000.0, said, alarm)
        self.assertEqual((raised, said), ([], {}))
        rs.second_unread(["w.jsonl: RuntimeError", "w.jsonl: RuntimeError"], 1_000.0, said, alarm)
        rs.second_unread(["w.jsonl: RuntimeError"], 1_000.0 + rs.REPEAT_S - 1, said, alarm)
        self.assertEqual(raised, [("second_journal_unread", ["w.jsonl: RuntimeError"])])
        rs.second_unread(["w.jsonl: RuntimeError"], 1_000.0 + rs.REPEAT_S, said, alarm)
        self.assertEqual(len(raised), 2)
        rs.second_unread([], 2_000.0 + rs.REPEAT_S, said, alarm)
        self.assertNotIn("_second_unread", said, "read again: forgotten, and said anew if it comes back")
        rs.second_unread(["x"], 5.0, said, lambda event, **f: False)
        self.assertNotIn("_second_unread", said, "an alarm the channel did not take is not remembered")


if __name__ == "__main__":
    unittest.main()
