#!/usr/bin/env python3
"""Checks for analyze_l2.py, which turns a series of l2Book snapshots into the cost of a market close.

    python -m unittest test_analyze -v          (from this directory; no network)

The series are synthetic books cut the way the API cuts them (test_book.api_view), so what the analyzer reports can be
compared with what was put in: a thin hour must be the worst hour, a book too thin for the size must count as unfilled
and never as cheap, and a jump in the price must show up in the volatile split.
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import analyze_l2 as A
import test_book as T


def ms(y, mo, d, h, mi=0, s=0) -> int:
    return int((datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() + s) * 1000)


def record(coin: str, t: int, mid: str, scale: float, seed: int) -> dict:
    """A SOL-like book at `mid`, sizes multiplied by `scale`, as the collector would have written it."""
    bids, asks = T.synthetic(mid, "0.01", 2500, seed)
    bids = [(p, s * scale * 1000) for p, s in bids]
    asks = [(p, s * scale * 1000) for p, s in asks]
    v = {"5": {"time": t, "b": [[str(p), str(s)] for p, s in bids[:20]], "a": [[str(p), str(s)] for p, s in asks[:20]]}}
    for k in (4, 3, 2):
        v[str(k)] = {"time": t, "b": [[str(p), str(s)] for p, s in T.api_view(bids, k, "bid")],
                     "a": [[str(p), str(s)] for p, s in T.api_view(asks, k, "ask")]}
    return {"coin": coin, "t": t, "v": v}


def write(directory: str, records: list, name="l2_20260925_10.jsonl.gz", tail=""):
    with gzip.open(os.path.join(directory, name), "wt") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        f.write(tail)


def summary_of(records: list) -> dict:
    with tempfile.TemporaryDirectory() as d:
        write(d, records)
        rows = {c: [] for c in A.COINS}
        for rec in A.read_series(d):
            r = A.snapshot_rows(rec)
            if r:
                rows[rec["coin"]].append(r)
        return A.summarize(rows, 20.0)


class Basics(unittest.TestCase):
    def test_nearest_rank_percentiles_and_unfilled_last(self):
        v = [float(i) for i in range(1, 11)]
        self.assertEqual((A.pct(v, 50), A.pct(v, 90), A.pct(v, 95), A.pct(v, 100)), (5.0, 9.0, 10.0, 10.0))
        self.assertEqual(A.pct(sorted([1.0, 2.0, A.INF]), 100), A.INF)
        self.assertIsNone(A.num(A.INF))

    def test_a_book_that_could_not_be_real_is_refused(self):
        self.assertTrue(A.valid([(99.0, 1.0), (98.0, 1.0)], [(100.0, 1.0), (101.0, 1.0)]))
        self.assertFalse(A.valid([(101.0, 1.0)], [(100.0, 1.0)]))                            # crossed
        self.assertFalse(A.valid([(98.0, 1.0), (99.0, 1.0)], [(100.0, 1.0)]))                # bids rising
        self.assertFalse(A.valid([(99.0, 1.0)], [(101.0, 1.0), (100.5, 1.0)]))               # asks falling
        self.assertFalse(A.valid([(99.0, 0.0)], [(100.0, 1.0)]))

    def test_a_torn_last_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, [record("SOL", ms(2026, 9, 25, 10), "119.165", 1.0, 1)], tail='{"coin": "SOL", "t": 17')
            self.assertEqual(len(list(A.read_series(d))), 1)


class Series(unittest.TestCase):
    def test_a_thin_hour_is_the_worst_hour_and_hours_are_bucketed_in_utc(self):
        recs = []
        for h, scale in ((10, 1.0), (11, 0.02), (12, 1.0)):
            for i in range(30):
                recs.append(record("SOL", ms(2026, 9, 25, h, 0, 20 * i), "119.165", scale, 100 * h + i))
        s = summary_of(recs)["coins"]["SOL"]
        c = s["cost_bps"]["sell"]["100000"]
        self.assertEqual(sorted(c["by_hour_utc"]), ["10", "11", "12"])
        self.assertEqual(c["worst_hour_utc"], "11")
        self.assertGreater(c["by_hour_utc"]["11"]["median"], 5 * c["by_hour_utc"]["10"]["median"])
        self.assertEqual(s["snapshots"], 90)
        self.assertEqual(s["depth_usd"]["bid"]["10"]["lowest_hour_utc"], "11")

    def test_a_book_too_thin_for_the_size_is_counted_unfilled_not_cheap(self):
        recs = [record("SOL", ms(2026, 9, 25, 10, 0, 20 * i), "119.165", 1e-7, i) for i in range(10)]
        c = summary_of(recs)["coins"]["SOL"]["cost_bps"]["sell"]["1000000"]
        self.assertEqual(c["unfilled"], 10)
        self.assertIsNone(c["median"])                                                 # unfilled sorts last, so the median is not a number

    def test_a_close_that_fits_the_finest_view_needs_no_coarser_one(self):
        recs = [record("SOL", ms(2026, 9, 25, 10, 0, 20 * i), "119.165", 1.0, i) for i in range(10)]
        c = summary_of(recs)["coins"]["SOL"]["cost_bps"]["sell"]["10000"]
        self.assertEqual((c["unfilled"], c["needed_coarser_views"]), (0, 0))

    def test_the_size_that_outruns_twenty_levels_is_stitched(self):
        recs = [record("SOL", ms(2026, 9, 25, 10, 0, 20 * i), "119.165", 0.05, i) for i in range(10)]
        c = summary_of(recs)["coins"]["SOL"]["cost_bps"]["sell"]["1000000"]
        self.assertGreater(c["needed_coarser_views"], 0)
        self.assertEqual(c["unfilled"], 0)

    def test_the_move_between_neighbouring_snapshots_is_measured(self):
        recs = [record("SOL", ms(2026, 9, 25, 10, 0, 20 * i), "119.165", 1.0, i) for i in range(5)]
        recs.append(record("SOL", ms(2026, 9, 25, 10, 1, 40), "120.365", 1.0, 5))       # +1 % in 20 s
        m = summary_of(recs)["coins"]["SOL"]["mid_move_per_snapshot_bps"]
        self.assertEqual(m["n"], 5)
        self.assertAlmostEqual(m["max"], 100.0, delta=1.0)
        self.assertEqual(m["median"], 0.0)

    def test_gaps_are_reported(self):
        recs = [record("SOL", ms(2026, 9, 25, 10, 0, 20 * i), "119.165", 1.0, i) for i in range(5)]
        recs += [record("SOL", ms(2026, 9, 25, 10, 30, 0), "119.165", 1.0, 9)]
        s = summary_of(recs)["coins"]["SOL"]
        self.assertEqual(s["gaps_over_2min"], 1)
        self.assertGreater(s["largest_gap_s"], 1000)


class Volatility(unittest.TestCase):
    def test_the_book_is_thinner_when_the_price_moves_fast(self):
        recs = []
        for i in range(70):
            jump = i >= 60                                                             # the last ten snapshots: price +0.6 %, book thin
            mid = "119.880" if jump else "119.165"
            recs.append(record("SOL", ms(2026, 9, 25, 10, 0, 20 * i), mid, 0.03 if jump else 1.0, i))
        f = summary_of(recs)["coins"]["SOL"]["fast_price_moves"]
        self.assertIsNotNone(f)
        self.assertGreater(f["threshold_move_bps"], 30)
        self.assertGreater(f["cost_bps_median"]["sell"]["100000"]["volatile"], f["cost_bps_median"]["sell"]["100000"]["calm"])
        self.assertLess(f["depth_10bps_median_usd"]["bid"]["volatile"], f["depth_10bps_median_usd"]["bid"]["calm"])

    def test_too_short_a_series_gives_no_split(self):
        recs = [record("SOL", ms(2026, 9, 25, 10, 0, 20 * i), "119.165", 1.0, i) for i in range(20)]
        self.assertIsNone(summary_of(recs)["coins"]["SOL"]["fast_price_moves"])


if __name__ == "__main__":
    unittest.main()
