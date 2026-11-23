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

    def test_a_restart_of_one_writer_leaves_its_blocks_to_one_journal_and_holds(self):
        """Review 15 of #338 (M1): a block only one journal holds is no cross-check. The keeper's deploy and the second
        machine's reboot leave no hole, but their blocks rest on one journal: held, said as such, never clean."""
        truth = self.truth()
        a = {b: px for b, px in truth.items() if not 1_100 <= b <= 1_150}   # the keeper's deploy
        b = {b: px for b, px in truth.items() if not 1_200 <= b <= 1_220}   # the second machine's reboot
        got, _, _ = self.judged(a, b)
        self.assertEqual((got["verdict"], got["action"], got["kinds"], got["journals"]),
                         ("suspicious", "hold", ["stop-one-journal"], 1))
        self.assertIn("72 of the 303 blocks 1003..1305 of the stop at 950000 on asset 3 are in one journal only",
                      got["reasons"][0])

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


class Candles(unittest.TestCase):
    """The venue's minute candles (ops/venue_candles.py) and the screen the journal is held against
    (`payout_review.candle_doubts`). The samples are the info API's own answers, saved as files."""

    FIX = pathlib.Path(__file__).resolve().parent / "fixtures"

    def post(self, net):
        meta = json.loads((self.FIX / f"hl-meta-{net}.json").read_text())
        candles = json.loads((self.FIX / f"hl-candles-btc-1m-{net}.json").read_text())

        def post(body):
            if body["type"] == "meta":
                return meta
            q = body["req"]
            return [k for k in candles if k["s"] == q["coin"] and q["startTime"] <= k["t"] <= q["endTime"]]
        return post, candles

    def test_the_info_apis_candles_in_the_oracles_units_whole_minutes_only(self):
        from ops import venue_candles as vc
        post, candles = self.post("mainnet")
        self.assertEqual(vc.decimals({0: "BTC"}, post), {0: 5})
        until = candles[-1]["t"] // 1000  # the last candle's minute is still running
        rows = vc.rows_of(0, 5, candles, until)
        self.assertEqual(len(rows), len(candles) - 1)
        self.assertEqual(rows[0]["l"], vc.units(candles[0]["l"], 5))
        self.assertEqual(vc.units("61391.0", 5), 613_910, "6 - szDecimals decimals, as the price precompile gives it")
        with self.assertRaises(RuntimeError):
            vc.decimals({0: "BTC"}, self.post("testnet")[0])  # testnet's index 0 is SOL

    def test_kept_on_disk_and_brought_up_to_date_from_the_last_minute_held(self):
        from ops import venue_candles as vc
        post, candles = self.post("mainnet")
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        store = vc.Store(d, "mainnet")
        now = candles[-1]["t"] // 1000 + 30
        half = candles[len(candles) // 2]["t"] // 1000
        self.assertEqual(vc.update(store, {0: "BTC"}, half, post), len(candles) // 2)
        self.assertEqual(vc.update(store, {0: "BTC"}, now, post), len(candles) - 1 - len(candles) // 2, "only what is new")
        got = store.load()
        self.assertEqual(sorted(got[0]), [k["t"] // 1000 for k in candles[:-1]])
        self.assertEqual(vc.to_venue(got)["0"][str(candles[0]["t"] // 1000)], vc.units(candles[0]["c"], 5))

    def judged(self, journal_px: dict, candles, bps=100):
        mt = T.MissedTouches()
        ch, _ = mt.world(set(range(1_300, 1_306)))
        text = "\n".join(json.dumps({"b": b, "t": 60 * b, "w": 0, "px": {"3": px}})
                         for b, px in sorted(journal_px.items()) if 1_002 <= b <= 1_400) + "\n"
        j = pr.Journal.parse(text, None, "keeper")
        j.add_text(text, None, "second")
        ch.d.update(journal=j, journalCheck={}, candles=candles, candleBps=bps)
        return pr.review(ch.d, T.POOL, 0)

    def candles_of(self, px: dict, wick=0):
        """A candle a minute (a block a minute here) around the journal's price; `wick` bps more each way."""
        return {3: {60 * b: (p * (10_000 - wick) // 10_000, p * (10_000 + wick) // 10_000, p) for b, p in px.items()}}

    def test_candles_that_agree_with_the_journal_are_clean(self):
        truth = T.MissedTouches().prices(set(range(1_300, 1_306)), until=2_001)
        got = self.judged(truth, self.candles_of(truth, wick=30))
        self.assertEqual((got["verdict"], got["action"]), ("clean", "approve"), got["reasons"])

    def test_a_candle_through_the_stop_where_the_journal_shows_no_reach_holds(self):
        mt = T.MissedTouches()
        truth = mt.prices(set(range(1_300, 1_306)), until=2_001)
        candles = self.candles_of(truth)
        candles[3][60 * 1_150] = (930_000, 1_000_000, 990_000)  # 2 % past the stop at 950 000
        got = self.judged(truth, candles)
        self.assertEqual(got["action"], "hold")
        self.assertIn("past the stop at 950000", json.dumps(got["reasons"]))

    def test_a_reach_in_the_journal_the_candles_never_came_near_holds(self):
        mt = T.MissedTouches()
        truth = mt.prices(set(range(1_300, 1_306)), until=2_001)
        candles = self.candles_of(truth)
        for b in range(1_300, 1_306):
            candles[3][60 * b] = (990_000, 1_000_000, 995_000)  # the venue 4 % above the stop all those minutes
        got = self.judged(truth, candles)
        self.assertEqual(got["action"], "hold")
        self.assertIn("short of it", json.dumps(got["reasons"]))

    def test_a_wick_inside_the_allowance_is_no_doubt(self):
        mt = T.MissedTouches()
        truth = mt.prices(set(range(1_300, 1_306)), until=2_001)
        candles = self.candles_of(truth)
        candles[3][60 * 1_150] = (945_000, 1_000_000, 990_000)  # past the stop by about 0.5 %: inside 1 %
        self.assertEqual(self.judged(truth, candles)["action"], "approve")

    def test_no_candles_or_too_few_hold(self):
        truth = T.MissedTouches().prices(set(range(1_300, 1_306)), until=2_001)
        self.assertEqual(self.judged(truth, None)["action"], "hold")
        few = {3: {m: k for m, k in self.candles_of(truth)[3].items() if m % 120 == 0}}
        got = self.judged(truth, few)
        self.assertEqual(got["action"], "hold")
        self.assertIn("candles cover", json.dumps(got["reasons"]))


class LostEvent(unittest.TestCase):
    """A TriggerSet the node's eth_getLogs left out: the account's own state at the block read to shows the stop."""

    def node(self, stop_px, position=100):
        from eth_abi import encode
        from eth_utils import keccak
        sel = lambda s: "0x" + keccak(text=s)[:4].hex()  # noqa: E731

        def rpc(method, params=()):
            assert method == "eth_call" and params[1] == hex(2_000), params
            data = params[0]["data"]
            if data.startswith(sel("rules()")):
                return "0x" + encode([pr.EXAM_RULES], [(0, 0, 0, [3], [])]).hex()
            if data.startswith(sel("positionOf(uint32)")):
                return "0x" + encode(["int256"], [position]).hex()
            if data.startswith(sel("stopOf(uint32)")):
                return "0x" + encode([pr.sk.TRIGGER], [(stop_px, 0, 0, 0)]).hex()
            if data.startswith(sel("takeOf(uint32)")):
                return "0x" + encode([pr.sk.TRIGGER], [(0, 0, 0, 0)]).hex()
            raise AssertionError(data)
        return rpc

    def test_a_stop_the_account_holds_and_its_events_do_not_show_holds(self):
        from unittest import mock
        mt = T.MissedTouches()
        evs = mt.events(end="none")  # a stop at 950 000 standing, by the events
        for held, doubt in ((950_000, False), (900_000, True), (0, True)):
            with mock.patch.object(pr.c, "rpc", side_effect=self.node(held)):
                got = pr.state_doubts(["0xA"], {"0xA": evs}, {"0xA": "Active"}, 2_000)
            self.assertEqual(bool(got), doubt, held)
        lost = [e for e in evs if e["name"] != "TriggerSet"]  # the event lost: the account still holds the stop
        with mock.patch.object(pr.c, "rpc", side_effect=self.node(950_000)):
            got = pr.state_doubts(["0xA"], {"0xA": lost}, {"0xA": "Active"}, 2_000)
        self.assertIn("may be missing from the node's logs", got["0xA"][0])
        with mock.patch.object(pr.c, "rpc", side_effect=self.node(950_000)):
            self.assertEqual(pr.state_doubts(["0xA"], {"0xA": lost}, {"0xA": "Passed"}, 2_000), {}, "an exam over holds none")


class StopsTimeOnly(unittest.TestCase):
    """Review 15 of #338 (H-B): the journals are read line by line, and only the blocks of the stops' time (`want`)
    are kept, in arrays: the memory of a pass goes with the time the stops stood, not with the oldest stop's age."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_only_the_blocks_of_each_assets_windows_are_kept(self):
        lines = [json.dumps({"b": b, "t": 10 * b, "w": 0, "px": {"3": 1_000 + b, "5": 2_000 + b}}) for b in range(1, 201)]
        (self.dir / "a-1970-01-01.jsonl").write_text("\n".join(lines) + "\n")
        (self.dir / "b-1970-01-01.jsonl").write_text("\n".join(lines) + "\n")
        got = pr.read_journals([self.dir / "a.jsonl", self.dir / "b.jsonl"], want={3: [(50, 60), (55, 70), (150, 150)]})
        self.assertEqual(sorted(got.px), [3], "an asset with no stop keeps nothing")
        self.assertEqual(list(got.px[3]), list(range(50, 71)) + [150])
        self.assertEqual((got.px[3][60], got.times[60], got.single_in(3, 1, 200), got.count_in(3, 1, 200)), (1_060, 600, [], 22))
        self.assertNotIn(100, got.times)

    def test_blocks_out_of_order_twice_and_a_hole_filled_by_the_other_are_put_in_place(self):
        a = [row(1, 10), row(2, 20), row(5, 50), row(3, 30), row(3, 31), row(6, 60)]
        b = [row(4, 40), row(5, 50), row(1, 10), row(7, 70)]
        j = pr.Journal()
        j.add_text("\n".join(a) + "\n", None, "keeper")
        j.add_text("\n".join(b) + "\n", None, "second")
        self.assertEqual(dict(j.px[3]), {1: 10, 2: 20, 3: 30, 4: 40, 5: 50, 6: 60, 7: 70})
        self.assertEqual(list(j.px[3].b), sorted(j.px[3].b))
        self.assertEqual(j.single_in(3, 1, 7), [2, 3, 4, 6, 7])
        self.assertEqual((j.single_count(3, 1, 7), j.single_in(3, 1, 7, 2)), (5, [2, 3]))
        self.assertEqual(j.conflicts_in(3, 1, 7), ["keeper gives two prices at block 3: 30 and 31"])

    def test_a_journal_that_disagrees_everywhere_keeps_every_block_but_few_words(self):
        j = pr.Journal()
        j.add_text("\n".join(row(b, 10) for b in range(1, 1_001)) + "\n", None, "keeper")
        j.add_text("\n".join(row(b, 11) for b in range(1, 1_001)) + "\n", None, "second")
        n, words = j.clash_in(3, 900, 1_000)
        self.assertEqual((n, len(words), len(j.conflicts[3])), (101, 3, pr.CONFLICT_TEXTS))
        self.assertEqual(words[0], "the journals disagree at block 900")

    def test_the_windows_are_each_stops_time_and_the_chains_own_prices(self):
        mt = T.MissedTouches()
        ch, s1 = mt.world(set(range(1_300, 1_306)))
        evs = ch.d["exam_events"]
        got = pr.journal_windows([s1], evs, 2_000)
        spans = [(x["armedAt"], x["end"]) for x in pr.trigger_spans(evs[s1], 2_000)]
        self.assertEqual(sorted(got), [3])
        self.assertTrue(set(spans) <= set(got[3]) and spans, got)
        self.assertTrue({(b, b) for b in pr.chain_prices(evs)[3]} <= set(got[3]))
        self.assertEqual(pr.journal_windows([], evs, 2_000), {}, "no account waiting, no block kept")


class SecondFolder(unittest.TestCase):
    """Review 15 of #338 (M5): the second writer's folder is filled from another machine. Only its day files by name are
    read, none past JOURNAL_FILE_MAX: a file too large or of another name is not read (its blocks rest on one journal,
    held), never the end of the pass."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.keeper, self.second = self.dir / "prices-sim1.jsonl", self.dir / "watch-prices-sim1.jsonl"
        text = json.dumps(HEAD) + "\n" + "\n".join(row(b, 10) for b in range(1, 11)) + "\n"
        (self.dir / "prices-sim1-1970-01-01.jsonl").write_text(text)
        (self.dir / "watch-prices-sim1-1970-01-01.jsonl").write_text(text)

    def read(self):
        return pr.read_journals([self.keeper, self.second], HEAD)

    def test_two_whole_journals_are_read(self):
        self.assertEqual(self.read().single_in(3, 1, 10), [])

    def test_a_file_past_the_limit_is_not_read_and_its_blocks_rest_on_one_journal(self):
        from unittest import mock
        big = self.dir / "watch-prices-sim1-1970-01-01.jsonl"
        big.write_text(big.read_text() + json.dumps({"gap": [0, 0], "why": "x" * 500}) + "\n")
        limit = (self.dir / "prices-sim1-1970-01-01.jsonl").stat().st_size
        with mock.patch.object(pr, "JOURNAL_FILE_MAX", limit):
            got = self.read()
        self.assertEqual(got.single_in(3, 1, 10), list(range(1, 11)))
        self.assertTrue(any(f"past the {limit} a journal file may be" in n for n in got.notes), got.notes)

    def test_files_of_other_names_and_links_in_the_second_folder_are_not_read(self):
        (self.dir / "watch-prices-sim1-1970-01-01.jsonl").unlink()
        bad = json.dumps(HEAD) + "\n" + row(5, 99) + "\n"
        (self.dir / "watch-prices-sim1-19x0-01-01.jsonl").write_text(bad)
        (self.dir / "watch-prices-sim1.jsonl").write_text(bad)
        (self.dir / "elsewhere.jsonl").write_text(bad)
        (self.dir / "watch-prices-sim1-1970-01-02.jsonl").symlink_to(self.dir / "elsewhere.jsonl")
        got = self.read()
        self.assertEqual((got.conflicts, got.px[3][5]), ({}, 10), "nothing of those files is taken")
        self.assertEqual(len(got.single_in(3, 1, 10)), 10)
        self.assertEqual(sum("is not a day file" in n for n in got.notes), 2, got.notes)
