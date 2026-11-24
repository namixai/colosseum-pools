"""Independent review 16 of PR 338 (head 1585034f). Each test states the CORRECT behaviour; red = a defect.
Run: COLOSSEUM_KEY_DIR= COLOSSEUM_RPC_URL=http://127.0.0.1:9 python -m unittest ops.tests.review16.test_review16 -v"""
import json
import os
import pathlib
import random
import shutil
import tempfile
import tracemalloc
import unittest
from unittest import mock

from ops import payout_review as pr
from ops import review_service as rs
from ops import stop_filler as sf
from ops.tests import test_payout_review as T
from ops.tests.review15.test_review15 import ScanWorld, world, truth, MT
from ops.tests.test_price_journals import HEAD

DAY = 86_400


def tmp(case):
    d = pathlib.Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


class PoisonedRows(unittest.TestCase):
    """The second machine's folder is filled by whoever holds its rsync key. A row that is not a whole price must be a
    hole (`bad`), never the end of the pass: the review is down for every payout while the file lies there."""

    def read(self, line):
        j = pr.Journal()
        j.add_lines([line + "\n"], None, "second")
        return j

    def test_an_infinite_price_is_a_hole_not_a_crash(self):
        for line in ('{"b":5,"t":1,"w":0,"px":{"3":Infinity}}', '{"b":5,"t":1,"w":0,"px":{"3":1e999}}'):
            self.assertEqual(self.read(line).bad, 1, line)

    def test_an_infinite_time_is_a_hole_not_a_crash(self):
        self.assertEqual(self.read('{"b":5,"t":1e999,"w":0,"px":{"3":7}}').bad, 1)

    def test_an_infinite_gap_is_not_a_crash(self):
        self.read('{"gap":[1e999,3],"why":"x"}')

    def test_a_deeply_nested_line_is_not_a_crash(self):
        self.read("[" * 200_000)

    def test_the_whole_pass_survives_a_poisoned_second_journal_file(self):
        """End to end: read_prices (the service's reader) over a second-writer folder with one poisoned day file."""
        d = tmp(self)
        keeper = d / "prices-sim1.jsonl"
        sec = d / "second"
        sec.mkdir()
        (sec / "watch-prices-sim1-2026-11-24.jsonl").write_text(
            json.dumps(HEAD) + "\n" + '{"b":1100,"t":1,"w":0,"px":{"3":Infinity}}\n')
        got = rs.read_prices(keeper, None, sec / "watch-prices-sim1.jsonl", None, None, {3: [(1000, 1400)]})
        self.assertTrue(got is None or isinstance(got, pr.Journal))


class UnboundedRows(unittest.TestCase):
    """What a journal keeps must go with what it is asked for, not with the bytes of a file (64 MB each, one a day of the
    window): a 1 GB unit is killed by three of them, and the review is down for everyone."""

    @staticmethod
    def retained(lines, expect=None):
        size = sum(len(x) + 1 for x in lines)
        tracemalloc.start()
        j = pr.Journal(want={3: [(1_000, 1_400)]})
        j.add_lines(iter(lines), expect, "second")
        cur, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return cur / size, j

    def test_gap_rows_outside_every_stop_are_not_kept(self):
        lines = ['{"gap":[%d,%d],"why":"x"}' % (5_000 + 2 * i, 5_001 + 2 * i) for i in range(100_000)]
        ratio, j = self.retained(lines)
        self.assertLess(ratio, 0.5, f"{ratio:.1f} bytes kept per byte of gap rows ({len(j.gaps)} gaps): 64 MB is "
                                    f"{64 * ratio:.0f} MB, and three day files are the unit's whole gigabyte")

    def test_header_rows_refused_do_not_grow_the_notes(self):
        lines = ['{"journal":"x%d"}' % i for i in range(100_000)]
        ratio, j = self.retained(lines, {"label": "a"})
        self.assertLess(len(j.notes), 1_000, f"{len(j.notes)} notes kept, {ratio:.1f} bytes per byte of the file")


class Windows16(unittest.TestCase):
    """H-B: the blocks kept are those of the stops' time, both ends included; 4-byte block numbers and times."""

    def keep(self, rows, want, **kw):
        j = pr.Journal(want=want)
        j.add_lines((json.dumps(r) + "\n" for r in rows), None, "k")
        return j

    def test_first_and_last_block_of_a_stops_life_are_kept_and_the_ones_beside_them_are_not(self):
        rows = [{"b": b, "t": 100 + b, "w": 0, "px": {"3": 7}} for b in range(1_000, 1_020)]
        j = self.keep(rows, {3: [(1_005, 1_010)]})
        self.assertEqual(sorted(j.px[3].b), list(range(1_005, 1_011)))
        self.assertEqual(sorted(j.times.b), list(range(1_005, 1_011)))

    def test_two_stops_in_one_asset_with_a_hole_between_them_keep_only_their_own_blocks(self):
        rows = [{"b": b, "t": 1, "w": 0, "px": {"3": 7}} for b in range(0, 30)]
        j = self.keep(rows, {3: [(2, 4), (5, 6), (20, 21)]})
        self.assertEqual(sorted(j.px[3].b), [2, 3, 4, 5, 6, 20, 21])

    def test_blocks_at_the_edge_of_four_bytes(self):
        top = (1 << 32) - 1
        rows = [{"b": top, "t": 5, "w": 0, "px": {"3": 7}}, {"b": top + 1, "t": 5, "w": 0, "px": {"3": 7}},
                {"b": -1, "t": 5, "w": 0, "px": {"3": 7}}]
        j = self.keep(rows, {3: [(top - 5, top + 5)]})
        self.assertEqual(sorted(j.px[3].b), [top])
        self.assertEqual(j.bad, 2)

    def test_a_time_past_four_bytes_or_negative_is_no_time_and_not_a_crash(self):
        rows = [{"b": 10, "t": 1 << 32, "w": 0, "px": {"3": 7}}, {"b": 11, "t": -5, "w": 0, "px": {"3": 7}},
                {"b": 12, "t": (1 << 32) - 1, "w": 0, "px": {"3": 7}}]
        j = self.keep(rows, {3: [(0, 100)]})
        self.assertEqual(sorted(j.times.b), [12])
        self.assertEqual(sorted(j.px[3].b), [10, 11, 12], "the prices stay; the missing times are said by the candle screen")

    def test_a_price_past_eight_bytes_is_a_hole(self):
        j = self.keep([{"b": 10, "t": 1, "w": 0, "px": {"3": 1 << 63}}], {3: [(0, 100)]})
        self.assertEqual(j.bad, 1)

    def test_against_a_plain_model_with_rows_out_of_order_twice_over_and_in_two_journals(self):
        rng = random.Random(16)
        for case in range(300):
            want = {3: [(10, 30)], 5: [(25, 45)]} if case % 2 else None
            sources = []
            for s in range(2):
                rows = []
                for _ in range(rng.randrange(0, 60)):
                    b = rng.randrange(0, 60)
                    asset = rng.choice((3, 5))
                    px = rng.choice((7, 7, 7, 8)) if rng.random() < 0.9 else rng.choice((0, 9))
                    rows.append((asset, b, px))
                if rng.random() < 0.5:
                    rng.shuffle(rows)
                else:
                    rows.sort(key=lambda r: r[1])
                    if rows and rng.random() < 0.5:
                        rows += rows[:3]  # a replay of its own lines
                sources.append(rows)
            j = pr.Journal(want=want)
            for s, rows in enumerate(sources):
                j.add_lines((json.dumps({"b": b, "t": b + 1000, "w": 0, "px": {str(a): px}}) + "\n" for a, b, px in rows),
                            None, f"s{s}")
            model, seen = {}, {}
            for s, rows in enumerate(sources):
                for a, b, px in rows:
                    if px <= 0 or (want is not None and not any(lo <= b <= hi for lo, hi in want.get(a, []))):
                        continue
                    model.setdefault((a, b), set()).add(px)
                    seen.setdefault((a, b), set()).add(s)
            for a in (3, 5):
                got = j.px.get(a)
                have = sorted(b for (x, b) in model if x == a)
                self.assertEqual([] if got is None else list(got.b), have, (case, a))
                clash = sorted(b for (x, b), v in model.items() if x == a and len(v) > 1)
                self.assertEqual(sorted(set(j._clash.get(a, ()))), clash, (case, a, "clash"))
                single = [b for (x, b) in sorted(seen, key=lambda k: k[1]) if x == a and len(seen[(x, b)]) == 1]
                self.assertEqual(j.single_in(a, 0, 99), single, (case, a, "single"))
                self.assertEqual(j.single_count(a, 0, 99), len(single))


class Files16(unittest.TestCase):
    def test_nothing_but_a_plain_day_file_of_the_label_is_read_from_the_second_folder(self):
        d = tmp(self)
        path = d / "watch-prices-sim1.jsonl"
        good = d / "watch-prices-sim1-2026-11-24.jsonl"
        good.write_text("x\n")
        (d / "outside.txt").write_text("secret\n")
        (d / "watch-prices-sim1-2026-11-23.jsonl").symlink_to(d / "outside.txt")
        (d / "watch-prices-sim1-2026-11-22.jsonl").mkdir()
        os.mkfifo(d / "watch-prices-sim1-2026-11-21.jsonl")
        (d / "watch-prices-sim1-abcd-ef-gh.jsonl").write_text("x\n")
        (d / "watch-prices-sim1-2026-11-24.jsonl.part").write_text("x\n")
        (d / "watch-prices-sim10-2026-11-24.jsonl").write_text("x\n")
        (d / "watch-prices-sim1-2026-11-20.jsonl ").write_text("x\n")
        notes = []
        got = pr.journal_files(path, None, None, strict=True, notes=notes)
        self.assertEqual([f.name for f in got], [good.name], notes)

    def test_a_file_past_the_limit_is_left_out_and_said(self):
        d = tmp(self)
        f = d / "watch-prices-sim1-2026-11-24.jsonl"
        f.write_text("x" * 200)
        with mock.patch.object(pr, "JOURNAL_FILE_MAX", 100):
            notes = []
            self.assertEqual(pr.journal_files(d / "watch-prices-sim1.jsonl", strict=True, notes=notes), [])
        self.assertTrue(notes)

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_what_is_read_of_a_file_is_capped_at_the_limit_whatever_it_grew_to_after_the_size_was_looked_at(self):
        """The size is looked at when the file is listed; `file_lines` then reads to its end. A file the key's holder keeps
        growing (or swaps for another) between the two is read as long as it is."""
        d = tmp(self)
        f = d / "watch-prices-sim1-2026-11-24.jsonl"
        f.write_text("\n".join('{"b":%d}' % i for i in range(20_000)) + "\n")
        with mock.patch.object(pr, "JOURNAL_FILE_MAX", 1_000):
            n = sum(1 for _ in pr.file_lines([f]))
        self.assertLess(n, 1_000, f"{n} lines read of a file past the {1_000} byte limit")

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_one_long_line_is_not_read_whole_either(self):
        d = tmp(self)
        f = d / "watch-prices-sim1-2026-11-24.jsonl"
        f.write_text("x" * 5_000_000)
        with mock.patch.object(pr, "JOURNAL_FILE_MAX", 100_000):
            got = [len(x) for x in pr.file_lines([f])]
        self.assertTrue(not got or max(got) <= 100_000, f"a line of {max(got)} bytes in memory")

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_the_same_journal_given_twice_is_not_two_journals(self):
        """--prices2 (COLOSSEUM_PRICES2) pointed at the keeper's own journal: every block is 'in both'."""
        d = tmp(self)
        f = d / "prices-sim1-2026-11-24.jsonl"
        f.write_text(json.dumps(HEAD) + "\n" + "\n".join(json.dumps({"b": b, "t": 1, "w": 0, "px": {"3": 7}}) for b in range(10, 20)) + "\n")
        j = pr.read_journals([d / "prices-sim1.jsonl", d / "prices-sim1.jsonl"], None)
        self.assertEqual(j.single_count(3, 0, 100), 10, "the keeper's own file read as the second writer's: no cross-check, said as two")


class Seams16(unittest.TestCase):
    def test_a_stop_across_midnight_reads_both_day_files_and_a_day_each_side(self):
        d = tmp(self)
        for day in ("2026-11-21", "2026-11-22", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"):
            (d / f"prices-sim1-{day}.jsonl").write_text("x\n")
        import calendar
        t = calendar.timegm((2026, 11, 23, 0, 0, 5))
        got = [f.name for f in pr.journal_files(d / "prices-sim1.jsonl", t, t + 3 * 3_600)]
        self.assertEqual(got, [f"prices-sim1-{x}.jsonl" for x in ("2026-11-22", "2026-11-23", "2026-11-24")])

    def test_a_foreign_header_in_the_second_file_leaves_its_blocks_single_and_the_payout_held(self):
        t = truth()
        j = pr.Journal()
        j.add_text("\n".join(json.dumps({"b": k, "t": 60 * k, "w": 0, "px": {"3": v}}) for k, v in sorted(t.items()) if 1_002 <= k <= 1_400) + "\n",
                   None, "keeper")
        j.add_text(json.dumps({**HEAD, "label": "other"}) + "\n" + "\n".join(json.dumps({"b": k, "t": 60 * k, "w": 0, "px": {"3": v}})
                   for k, v in sorted(t.items()) if 1_002 <= k <= 1_400) + "\n", {"label": "sim1"}, "second")
        self.assertTrue(j.notes)
        self.assertEqual(j.single_count(3, 1_003, 1_305), 303)

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_second_stale_does_not_crash_the_pass_on_a_file_that_is_not_there_by_the_time_it_is_looked_at(self):
        d = tmp(self)
        (d / "watch-prices-sim1-2026-11-24.jsonl").symlink_to(d / "nowhere")
        said = {}
        got = []
        rs.second_stale(d / "watch-prices-sim1.jsonl", 1_800_000_000, said, lambda *a, **k: got.append(k) or True)
        self.assertTrue(got, "a second journal with nothing readable in it is the stale alarm")


class Route16(unittest.TestCase):
    def index(self):
        from gateway import simindex as si
        import threading
        idx = si.SimIndex.__new__(si.SimIndex)
        idx.lock = threading.Lock()
        idx.pools = {"0x" + "ab" * 20: {}}
        idx.chain_id = 998
        return idx

    def test_ids_and_pool_are_numbers_and_an_address_and_nothing_names_a_path(self):
        from gateway import simindex as si
        d = tmp(self)
        (d / "secret.json").write_text('{"x":1}')
        r = si.Reviews(d)
        idx = self.index()
        from eth_utils import to_checksum_address
        pool = to_checksum_address("0x" + "ab" * 20)
        idx.pools = {pool: {}}
        for ids in ("../secret", "1,../secret", "1/../../x", "1\n2", "", "1,,2", "-1", "1e3", "0x10", " 1", "1 ", ",".join(["1"] * 101)):
            code, body = r.answer(idx, pool, ids)
            self.assertEqual(code, 400, ids)
        for pl in ("../x", pool + "/..", pool[:-1], "0x" + "zz" * 20, pool + "00", ""):
            self.assertEqual(r.answer(idx, pl, "1")[0], 400, pl)

    def test_a_review_file_that_is_a_symlink_or_a_directory_or_huge_is_not_handed_out(self):
        from gateway import simindex as si
        from eth_utils import to_checksum_address
        d = tmp(self)
        pool = to_checksum_address("0x" + "ab" * 20)
        (d / "o.json").write_text('{"schema":"x","reasons":[{"kind":"pair","text":"names 0xTRADER"}]}')
        (d / f"{pool}-1.json").symlink_to(d / "o.json")
        (d / f"{pool}-2.json").mkdir()
        (d / f"{pool}-3.json").write_text(" " * (si.REVIEW_MAX_BYTES + 1))
        (d / f"{pool}-4.json").write_text(json.dumps({"schema": "x", "reasons": [{"kind": "pair", "text": "names 0xTRADER"}],
                                                      "evidence": {"stop": 1}, "stopDraft": {"a": 1}, "secret": 5}))
        idx = self.index()
        idx.pools = {pool: {}}
        code, body = si.Reviews(d).answer(idx, pool, "1,2,3,4")
        self.assertEqual((code, body["missing"]), (200, [1, 2]))
        self.assertEqual(list(body["unreadable"]), ["3"])
        got = json.dumps(body["reviews"]["4"])
        for word in ("evidence", "stopDraft", "secret", "0xTRADER"):
            self.assertNotIn(word, got)


class OneJournalBranches(unittest.TestCase):
    """M1: 'clean' only where each block of each stop is in both journals at one price, or a calibrated node bore it out."""

    def chain_with(self, a, b, node, reads=200):
        mt = MT()
        ch, _ = mt.world(set(range(1_300, 1_306)))
        j = pr.Journal()
        for name, rows in (("keeper", a), ("second", b)):
            if rows is not None:
                j.add_text("\n".join(json.dumps({"b": k, "t": 60 * k, "w": 0, "px": {"3": v}})
                                     for k, v in sorted(rows.items()) if 1_002 <= k <= 1_400) + "\n", None, name)
        ch.d["journal"] = j
        ch.d["journalCheck"] = pr.check_journal(list(ch.d["exam_events"]), ch.d["exam_events"], j, 2_000, node, reads)
        return pr.review(ch.d, T.POOL, 0), j

    def node_of(self, table, latest_px=777, mapping=None):
        def node(asset, block):
            if block == 2_000:
                return latest_px
            return table.get(block) if mapping is None else mapping(block)
        return node

    def test_a_calibrated_node_that_bears_every_single_block_out_makes_it_clean(self):
        t = truth()
        got, _ = self.chain_with(t, None, self.node_of(t), reads=400)
        self.assertEqual((got["verdict"], got["action"], got["journals"]), ("clean", "approve", 2), got["reasons"])

    def test_the_default_200_reads_leave_a_303_block_stop_on_one_journal_held(self):
        t = truth()
        got, _ = self.chain_with(t, None, self.node_of(t))
        self.assertEqual((got["action"], got["journals"]), ("hold", 1))

    def test_a_node_that_answers_nothing_holds(self):
        t = truth()
        got, _ = self.chain_with(t, None, self.node_of(t, mapping=lambda b: None))
        self.assertNotEqual(got["verdict"], "clean")

    def test_no_node_holds(self):
        t = truth()
        got, _ = self.chain_with(t, None, None)
        self.assertEqual((got["action"], got["journals"]), ("hold", 1))

    def test_a_node_that_gives_todays_price_everywhere_holds(self):
        t = truth()
        got, _ = self.chain_with(t, None, self.node_of(t, mapping=lambda b: 777))
        self.assertNotEqual(got["verdict"], "clean")

    def test_a_node_that_knows_only_some_blocks_holds_the_rest(self):
        t = truth()
        got, _ = self.chain_with(t, None, self.node_of(t, mapping=lambda b: t.get(b) if b % 2 else None))
        self.assertNotEqual(got["verdict"], "clean")

    def test_a_node_that_agrees_with_one_journal_does_not_clear_a_disagreement_between_them(self):
        t = truth()
        mt = MT()
        other = {**t, 1_150: mt.LOW}
        got, _ = self.chain_with(t, other, self.node_of(t))
        self.assertNotEqual(got["verdict"], "clean", "the node bore the keeper's price out; the second journal says LOW")

    def test_more_single_blocks_than_the_node_is_asked_for_hold(self):
        t = truth()
        got, _ = self.chain_with(t, None, self.node_of(t), reads=60)
        self.assertEqual((got["action"], got["journals"]), ("hold", 1), got["reasons"])

    def test_a_node_that_lies_at_one_block_holds(self):
        t = truth()
        mt = MT()
        got, _ = self.chain_with(t, None, self.node_of(t, mapping=lambda b: mt.LOW if b == 1_150 else t.get(b)))
        self.assertNotEqual(got["verdict"], "clean")

    def test_two_journals_that_agree_are_clean_with_two(self):
        t = truth()
        got, _ = self.chain_with(t, t, None)
        self.assertEqual((got["verdict"], got["action"], got["journals"]), ("clean", "approve", 2), got["reasons"])

    def test_a_block_one_journal_has_and_the_other_has_not_holds_even_where_the_one_is_right(self):
        t = truth()
        part = {b: v for b, v in t.items() if b != 1_150}
        got, _ = self.chain_with(t, part, None)
        self.assertEqual((got["action"], got["journals"]), ("hold", 1))

    def test_a_zero_price_in_the_second_journal_is_a_hole_in_it_and_holds(self):
        t = truth()
        zero = {**t, 1_150: 0}
        got, _ = self.chain_with(t, zero, None)
        self.assertEqual((got["action"], got["journals"]), ("hold", 1))

    def test_a_time_missing_on_a_row_holds_on_the_candle_screen(self):
        t = truth()
        c = {3: {60 * b: (p, p, p) for b, p in t.items()}}
        got, _ = world(t, t, times=False, candles=c)
        self.assertEqual(got["action"], "hold")

    def test_a_row_with_a_time_in_one_journal_only_is_timed(self):
        t = truth()
        c = {3: {60 * b: (p, p, p) for b, p in t.items()}}
        got, _ = world(t, t, times=True, candles=c)
        self.assertEqual(got["action"], "approve", got["reasons"])


class Links16(ScanWorld):
    def test_two_buyers_funded_by_one_wallet_long_before_they_bought_are_linked_when_only_one_has_a_payout(self):
        """read_links reads only a trader with a payout waiting. The other buyer of the pair (the losing wallet, which
        never has a payout) is read from its own first purchase on, so the funder of both is seen sending to one."""
        a, b, funder = T.addr(0x101), T.addr(0x102), T.addr(0xF0)
        far = 300_000
        for k, who in enumerate((a, b)):
            self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(who)], "blockNumber": 10 + k})
            self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(T.addr(0x201 + k)), self.word(who), self.word(T.addr(0xB0))],
                               "blockNumber": far + k})
        s = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        s.d["scannedTo"] = far - 1
        s.d["pools"] = list(self.POOLS)
        with mock.patch.object(pr, "logs", self.fake()), \
                mock.patch.object(pr.c, "rpc", side_effect=lambda m, p=(): {"timestamp": hex(1_000 * int(p[0], 16))}):
            s.read(self.POOLS, self.USDC, far + 5, part=100)
        self.assertEqual(self.links(s, [a]), [])
        chain = {"pools": list(self.POOLS), "transfers": sorted(s.d["transfers"], key=lambda t: t["block"]),
                 "sales": [{"trader": a}, {"trader": b}]}
        found = pr.linked(a, chain, pr.Params())
        self.assertEqual([l["trader"] for l in found], [b], "the funder of both wallets is not seen sending to the second")

    def test_control_both_traders_read_back_are_linked(self):
        a, b, funder = T.addr(0x101), T.addr(0x102), T.addr(0xF0)
        far = 300_000
        for k, who in enumerate((a, b)):
            self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(who)], "blockNumber": 10 + k})
            self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(T.addr(0x201 + k)), self.word(who), self.word(T.addr(0xB0))],
                               "blockNumber": far + k})
        s = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        s.d["scannedTo"] = far - 1
        s.d["pools"] = list(self.POOLS)
        with mock.patch.object(pr, "logs", self.fake()), \
                mock.patch.object(pr.c, "rpc", side_effect=lambda m, p=(): {"timestamp": hex(1_000 * int(p[0], 16))}):
            s.read(self.POOLS, self.USDC, far + 5, part=100)
        self.assertEqual(self.links(s, [a, b]), [])
        chain = {"pools": list(self.POOLS), "transfers": sorted(s.d["transfers"], key=lambda t: t["block"]),
                 "sales": [{"trader": a}, {"trader": b}]}
        self.assertEqual([l["trader"] for l in pr.linked(a, chain, pr.Params())], [b])

    def test_a_scan_file_of_the_old_schema_is_not_used(self):
        s = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        s.d["scannedTo"] = 77
        s.save()
        old = json.loads((self.dir / "scan.json").read_text())
        old["schema"] = "payout-review-scan/1"
        (self.dir / "scan.json").write_text(json.dumps(old))
        again = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        self.assertEqual(again.scanned_to, 0)

    def test_a_scan_of_another_deployment_start_or_chain_is_not_used(self):
        s = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        s.d["scannedTo"] = 77
        s.save()
        for args in ((T.addr(0xFB), 998, 1), (T.addr(0xFA), 999, 1), (T.addr(0xFA), 998, 2)):
            self.assertEqual(pr.LogScan(self.dir / "scan.json", *args).scanned_to, args[2] - 1)

    def test_a_chunk_whose_read_failed_is_not_marked_read(self):
        trader, funder = T.addr(0x101), T.addr(0xF0)
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(T.addr(0x201)), self.word(trader), self.word(T.addr(0xB0))],
                           "blockNumber": 5_000})
        self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(trader)], "blockNumber": 2_500})
        s = self.scan(5_010, part=5_010)
        real = self.fake()
        calls = [0]

        def flaky(address, topics, lo, hi):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("node error")
            return real(address, topics, lo, hi)
        with mock.patch.object(pr, "logs", flaky):
            with self.assertRaises(RuntimeError):
                s.read_links([trader], self.USDC)
        again = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        self.assertEqual(again.d["links"][trader]["at"], 1 + pr.LINK_CHUNK, "only the chunk before the failure is kept")
        self.assertFalse(again.links_read(trader))
        self.assertEqual(self.links(again, [trader]), [])
        self.assertEqual([t["block"] for t in again.d["transfers"]], [2_500])

    def test_a_trader_whose_links_are_partly_read_is_reported_left(self):
        trader = T.addr(0x101)
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(T.addr(0x201)), self.word(trader), self.word(T.addr(0xB0))],
                           "blockNumber": 5_000})
        s = self.scan(5_010, part=5_010)
        clock = iter([0.0, 0.0, 99.0, 99.0, 99.0])
        with mock.patch.object(pr, "logs", self.fake()), mock.patch.object(pr.time, "monotonic", lambda: next(clock)):
            self.assertEqual(s.read_links([trader], self.USDC, until=1.0), [trader])
        self.assertFalse(s.links_read(trader))

    def test_a_new_buyer_without_a_payout_costs_no_link_calls(self):
        trader = T.addr(0x101)
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(T.addr(0x201)), self.word(trader), self.word(T.addr(0xB0))],
                           "blockNumber": 1_000_300})
        calls = []
        real = self.fake()

        def counting(address, topics, lo, hi):
            calls.append((lo, hi))
            return real(address, topics, lo, hi)
        s = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        s.d["scannedTo"] = 1_000_000
        s.d["pools"] = list(self.POOLS)
        with mock.patch.object(pr, "logs", counting), \
                mock.patch.object(pr.c, "rpc", side_effect=lambda m, p=(): {"timestamp": hex(5)}):
            s.read(self.POOLS, self.USDC, 1_000_600)
        self.assertLess(len(calls), 20, calls[:5])

    def test_a_late_found_pool_log_does_not_unsort_what_the_review_reads_in_order(self):
        exam, trader = T.addr(0x201), T.addr(0x101)
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(exam), self.word(trader), self.word(T.addr(0xB0))],
                           "blockNumber": 38})
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(T.addr(0x202)), self.word(T.addr(0x102)), self.word(T.addr(0xB0))],
                           "blockNumber": 39})
        self.blind = (38, 38)
        self.scan(40)
        self.blind = None
        s = self.scan(80)
        self.assertEqual(sorted(s.accounts()), sorted([exam, T.addr(0x202)]))


class Watch16(unittest.TestCase):
    NOW = 1_800_000_000

    def body(self, **kw):
        return {"now": self.NOW, "prices_at": self.NOW - 1, "review_at": self.NOW - 60, **kw}

    def test_a_fresh_body_on_a_close_clock_is_fine(self):
        self.assertIsNone(rs.host_problem(self.body(), self.NOW + 3))

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_a_second_machines_clock_far_off_does_not_say_the_host_is_down_but_says_something(self):
        got = rs.host_problem(self.body(), self.NOW + 6 * 3_600)
        self.assertIsNotNone(got, "an answer the host stamped six hours before this machine's clock: a cached or replayed answer, or a broken clock")

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_an_answer_replayed_from_a_cache_a_day_later_is_not_taken_as_alive(self):
        self.assertIsNotNone(rs.host_problem(self.body(), self.NOW + DAY))

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_a_host_stamp_in_the_future_is_not_taken_as_alive_either(self):
        self.assertIsNotNone(rs.host_problem(self.body(now=self.NOW + 10 * DAY, prices_at=self.NOW + 10 * DAY - 1,
                                                      review_at=self.NOW + 10 * DAY - 1), self.NOW))

    def test_old_journal_old_review_missing_numbers(self):
        self.assertEqual(rs.host_problem(self.body(prices_at=self.NOW - 181), self.NOW)[0], "prices")
        self.assertIsNone(rs.host_problem(self.body(prices_at=self.NOW - 180), self.NOW))
        self.assertEqual(rs.host_problem(self.body(review_at=self.NOW - 1_801), self.NOW)[0], "review")
        self.assertEqual(rs.host_problem(self.body(prices_at=None), self.NOW)[0], "prices")
        self.assertEqual(rs.host_problem(self.body(review_at=None), self.NOW)[0], "review")
        self.assertEqual(rs.host_problem(self.body(prices_at=True), self.NOW)[0], "prices")
        self.assertEqual(rs.host_problem(self.body(prices_at="1799999999"), self.NOW)[0], "prices")
        self.assertEqual(rs.host_problem(self.body(prices_at=float(self.NOW)), self.NOW)[0], "prices")

    def test_an_answer_that_is_not_json_or_not_an_object_or_an_error_is_an_alarm(self):
        for bad in (ValueError("x"), OSError("down"), TimeoutError("slow")):
            said, state = [], {}

            def fetch(url, bad=bad):
                raise bad
            rs.watch_host("http://h/x", state, lambda *a, **k: said.append(k) or True, self.NOW, fetch=fetch)
            self.assertEqual(said[0]["kind"], "silent")
        # a JSON list through the real fetch
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s, n): return b"[1,2]"
        with mock.patch("urllib.request.urlopen", return_value=R()):
            with self.assertRaises(ValueError):
                rs.fetch_health("http://h/x")

    def test_a_channel_that_did_not_take_the_alarm_is_tried_again_next_minute(self):
        said, state = [], {}
        rs.watch_host("http://h/x", state, lambda *a, **k: (said.append(1), False)[1], self.NOW,
                      fetch=lambda u: self.body(prices_at=self.NOW - 999))
        self.assertEqual(state, {})
        rs.watch_host("http://h/x", state, lambda *a, **k: said.append(1) or True, self.NOW + 60,
                      fetch=lambda u: self.body(prices_at=self.NOW - 999))
        self.assertEqual(len(said), 2)

    def test_the_url_is_not_in_the_alarm(self):
        seen = []
        rs.watch_host("https://secret.example/KEY", {}, lambda e, **k: seen.append(json.dumps(k)) or True, self.NOW,
                      fetch=lambda u: (_ for _ in ()).throw(OSError("https://secret.example/KEY")))
        self.assertNotIn("secret.example", seen[0])

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_health_says_nothing_of_the_provider_url_when_a_read_failed(self):
        """`last_error` is in the public health route and is `str(exc)[:200]` of the read that failed; requests' own
        HTTPError names the URL, which for a provider is the key."""
        import requests
        from gateway import simindex as si
        resp = requests.Response()
        resp.status_code = 500
        resp.url = "https://rpc.example.com/v2/SECRETKEY123"
        err = None
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            err = exc
        idx = si.SimIndex.__new__(si.SimIndex)
        import threading
        idx.lock = threading.Lock()
        idx.last_error = f"{type(err).__name__}: {str(err)[:200]}"
        idx.head = idx.scanned_to = 0
        idx.chain_id = 998
        idx.factory = "0x"
        idx.from_block = 0
        idx.pools = {}
        idx.window = 1
        idx.updated_at = 0
        idx.errors = 1
        self.assertNotIn("SECRETKEY123", json.dumps(idx.health()))


class Pulse16(unittest.TestCase):
    def test_the_pulse_has_numbers_only_and_reads_nothing_else(self):
        from gateway import simindex as si
        d = tmp(self)
        (d / "reviews").mkdir()
        (d / "reviews" / "status.json").write_text(json.dumps({"at": 1_800_000_000, "secret": "x"}))
        (d / "prices-sim1-2026-11-24.jsonl").write_text("x")
        p = si.Pulse(d / "prices-sim1.jsonl", d / "reviews", clock=lambda: 1_795_000_000 + 0)
        ans = p.answer()
        self.assertEqual(set(ans), {"now", "prices_at", "review_at"})
        self.assertTrue(all(v is None or isinstance(v, int) for v in ans.values()))

    @unittest.expectedFailure  # found by review 16: put right in a later commit of this round
    def test_status_json_as_a_symlink_or_too_big_or_not_json_is_null_not_a_crash(self):
        from gateway import simindex as si
        d = tmp(self)
        (d / "o").write_text(json.dumps({"at": 5}))
        (d / "status.json").symlink_to(d / "o")
        self.assertIsNone(si.Pulse(None, d).review_at())
        (d / "status.json").unlink()
        (d / "status.json").write_text("x" * 5_000)
        self.assertIsNone(si.Pulse(None, d).review_at())
        (d / "status.json").write_text("[1]")
        self.assertIsNone(si.Pulse(None, d).review_at())
        (d / "status.json").write_text('{"at": true}')
        self.assertIsNone(si.Pulse(None, d).review_at())
        (d / "status.json").write_text('{"at": 1e999}')
        self.assertIsNone(si.Pulse(None, d).review_at(), "an infinite time is no time, and not a crash of the route")


class Alarms16(unittest.TestCase):
    def reviews(self, n=3):
        r = lambda i: {"pool": "0xP", "id": i, "verdict": "suspicious", "action": "hold", "approvals": True, "approved": False,
                       "trader": "0xT", "kinds": ["stop-unchecked"], "reasons": ["x"], "dueAt": 10, "hardAt": 99_999_999}
        chain = {"payouts": [{"state": "Due", "pool": "0xP", "id": i} for i in range(n)], "now": 100, "latest": 5}
        return r, chain

    def run_pass(self, d, alarm, said, n=3):
        r, chain = self.reviews(n)
        with mock.patch.object(rs.pr, "read_chain", return_value=chain), \
                mock.patch.object(rs.pr, "review", side_effect=lambda c, p, i: r(i)), \
                mock.patch.object(rs.pr, "card", side_effect=lambda rv, c: {"pool": rv["pool"], "id": rv["id"]}), \
                mock.patch.object(rs.core, "log"):
            return rs.one_pass({}, d, None, said, alarm)

    def test_one_alarm_that_raises_does_not_keep_the_others_from_being_said(self):
        d = tmp(self)
        seen, said = [], {}

        def alarm(event, **fields):
            seen.append(fields["id"])
            if fields["id"] == 0:
                raise RuntimeError("the channel's own bug")
            return True
        try:
            self.run_pass(d, alarm, said)
        except RuntimeError:
            pass
        self.assertEqual(seen, [0, 1, 2], "the second and third held payouts were never said")

    def test_an_alarm_the_channel_refused_is_said_again_next_pass_and_the_others_still_go(self):
        d = tmp(self)
        seen, said = [], {}
        answers = {0: False, 1: True, 2: True}

        def alarm(event, **fields):
            seen.append(fields["id"])
            return answers[fields["id"]]
        self.run_pass(d, alarm, said)
        self.assertEqual((seen, sorted(k for k, v in said.items() if v)), ([0, 1, 2], ["0xP-1", "0xP-2"]))
        answers[0] = True
        self.run_pass(d, alarm, said)
        self.assertEqual(seen, [0, 1, 2, 0])

    def test_an_alarm_with_no_channel_returns_false_and_is_not_remembered(self):
        d = tmp(self)
        with mock.patch.dict("os.environ", {"COLOSSEUM_ALERT_CMD": ""}):
            a = sf.Alarm(d / "ALARM")
            self.assertIs(a("x", wait=True), False)
        self.assertIsNone(sf.Alarm(None, "")("x"))

    def test_a_blank_channel_is_not_a_channel_on_mainnet(self):
        net = mock.Mock()
        net.name = "mainnet"
        for cmd in ("", " ", "\t\n", "   \n  "):
            with mock.patch.object(sf.c, "NETWORK", net), mock.patch.dict("os.environ", {"COLOSSEUM_ALERT_CMD": cmd}):
                with self.assertRaises(SystemExit):
                    sf.require_channel("x")

    def test_a_channel_that_does_nothing_is_not_caught_at_start_on_mainnet(self):
        """Not a defect to fix in code (no program can tell a channel that reaches a person from one that does not), but the
        start takes `:` and `true` as channels and the alarm they 'send' is taken: said here so it is a decision."""
        net = mock.Mock()
        net.name = "mainnet"
        for cmd in (":", "true", "cat >/dev/null"):
            with mock.patch.object(sf.c, "NETWORK", net), mock.patch.dict("os.environ", {"COLOSSEUM_ALERT_CMD": cmd}):
                sf.require_channel("x")
                d = tmp(self)
                self.assertTrue(sf.Alarm(d / "A")("x", wait=True))

    def test_the_mainnet_test_is_the_networks_name_not_a_variable_a_service_can_blank(self):
        from spike.hlspike import network as nw
        for bad in ("Mainnet", " mainnet", "mainnet ", "main"):
            with self.assertRaises(SystemExit):
                nw.select({"COLOSSEUM_NETWORK": bad})
        self.assertEqual(nw.select({"COLOSSEUM_NETWORK": ""}).name, "testnet")
        self.assertEqual(nw.select({}).name, "testnet")


if __name__ == "__main__":
    unittest.main()
