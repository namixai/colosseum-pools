#!/usr/bin/env python3
"""Checks for book.py, the cost of closing at market against an l2Book snapshot.

    python -m unittest test_book -v          (from this directory; no network)

The properties that matter are the ones that make a number safe to quote: the stitched book must never give a
lower cost or a deeper band than the real book would, and must agree with it wherever the finest view is enough.
The real API is imitated by a synthetic book cut the way the API cuts it (bids floor, asks ceil to n significant
figures, sizes summed, 20 buckets), checked against the live API on 25.09.2026 away from decade boundaries.
"""
from __future__ import annotations

import random
import unittest
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import book as B


def api_view(levels, k, side, n=20):
    """levels: [(Decimal price, float size)] best first. The nSigFigs=k view of them, floats, best first."""
    out, seen = [], {}
    for px, sz in levels:
        w = Decimal(10) ** (px.adjusted() - k + 1)
        rounding = ROUND_FLOOR if side == "bid" else ROUND_CEILING
        b = (px / w).to_integral_value(rounding=rounding) * w
        if b in seen:
            out[seen[b]][1] += sz
        else:
            seen[b] = len(out)
            out.append([b, sz])
    return [(float(p), s) for p, s in out[:n]]


def synthetic(mid: str, tick: str, depth: int, seed: int):
    """A fine-grid book: (bids, asks) as [(Decimal, size)] best first, sizes growing away from the touch."""
    rng = random.Random(seed)
    m, t = Decimal(mid), Decimal(tick)
    bids = [(m - t / 2 - i * t, rng.uniform(0.2, 2.0) * (1 + i / 40)) for i in range(depth)]
    asks = [(m + t / 2 + i * t, rng.uniform(0.2, 2.0) * (1 + i / 40)) for i in range(depth)]
    return bids, asks


def as_floats(levels):
    return [(float(p), s) for p, s in levels]


COINS = {"BTC-like": ("84708.50", "1", 30000), "ETH-like": ("2713.85", "0.1", 6000), "SOL-like": ("119.165", "0.01", 2500),
         "across a decade": ("100.05", "0.01", 4000)}


def stitched(levels, side):
    exact = as_floats(levels[:20])
    coarse = [(k, api_view(levels, k, side)) for k in (4, 3, 2)]
    return B.stitch(exact, coarse, side)


class Walk(unittest.TestCase):
    def test_vwap_of_a_close_that_crosses_two_levels(self):
        bids = [(100.0, 1.0), (99.0, 2.0), (98.0, 10.0)]
        vwap, got, deepest = B.walk(bids, 2.0)
        self.assertAlmostEqual(vwap, (100 * 1 + 99 * 1) / 2)
        self.assertEqual((got, deepest), (2.0, 99.0))

    def test_a_book_too_thin_for_the_size_reports_what_it_could_fill(self):
        vwap, got, _ = B.walk([(100.0, 1.0), (99.0, 1.0)], 5.0)
        self.assertAlmostEqual(got, 2.0)
        self.assertIsNone(B.close_cost_bps([(100.0, 1.0), (99.0, 1.0)], 100.0, 500.0, "sell"))

    def test_average_price_for_a_dollar_notional(self):
        bids = [(100.0, 1.0), (99.0, 2.0)]
        self.assertAlmostEqual(B.vwap_for_notional(bids, 100.0), 100.0)                    # the whole first level
        self.assertAlmostEqual(B.vwap_for_notional(bids, 199.0), 199.0 / (1 + 99.0 / 99.0))  # first level plus $99 of the second
        self.assertIsNone(B.vwap_for_notional(bids, 10_000.0))

    def test_a_tiny_close_costs_the_half_spread(self):
        bids, asks = [(99.9, 100.0)], [(100.1, 100.0)]
        mid, spread = B.mid_and_spread(bids, asks)
        self.assertAlmostEqual(mid, 100.0)
        self.assertAlmostEqual(B.close_cost_bps(bids, mid, 1.0, "sell"), spread / 2)
        self.assertAlmostEqual(B.close_cost_bps(asks, mid, 1.0, "buy"), spread / 2)

    def test_buying_back_a_short_is_priced_against_the_asks_upward(self):
        asks = [(101.0, 1.0), (102.0, 1.0)]
        self.assertAlmostEqual(B.close_cost_bps(asks, 100.0, 200.0, "buy"), (101.5 - 100.0) / 100.0 * 1e4)


class Widths(unittest.TestCase):
    def test_widths_the_api_uses(self):
        self.assertAlmostEqual(B.bucket_width(84700.0, 4, "bid"), 10.0)
        self.assertAlmostEqual(B.bucket_width(84710.0, 4, "ask"), 10.0)
        self.assertAlmostEqual(B.bucket_width(2714.0, 3, "ask"), 10.0)
        self.assertAlmostEqual(B.bucket_width(119.0, 3, "bid"), 1.0)

    def test_a_bucket_on_a_power_of_ten_is_measured_on_the_side_it_holds(self):
        self.assertAlmostEqual(B.bucket_width(100.0, 3, "ask"), 0.1)      # holds (99.9, 100.0]
        self.assertAlmostEqual(B.bucket_width(100.0, 3, "bid"), 1.0)      # holds [100, 101)


class Stitching(unittest.TestCase):
    def test_a_bucket_over_the_edge_is_left_out_and_one_beyond_it_is_taken(self):
        exact_bids = [(100.0, 1.0), (99.0, 1.0)]
        book = B.stitch(exact_bids, [(3, [(99.0, 5.0), (98.0, 7.0)])], "bid")
        self.assertEqual([(p, s) for p, s, _ in book], [(100.0, 1.0), (99.0, 1.0), (98.0, 7.0)])
        exact_asks = [(101.0, 1.0), (102.0, 1.0)]
        book = B.stitch(exact_asks, [(3, [(102.0, 5.0), (103.0, 7.0)])], "ask")
        self.assertEqual([(p, s) for p, s, _ in book], [(101.0, 1.0), (102.0, 1.0), (103.0, 7.0)])

    def test_sources_are_named(self):
        book = B.stitch([(100.0, 1.0), (99.0, 1.0)], [(2, [(98.0, 7.0)])], "bid")
        self.assertEqual([s for _, _, s in book], ["exact", "exact", "k2"])


class NeverOptimistic(unittest.TestCase):
    """Across four synthetic books and every size that fits, the stitched book is never cheaper than the real one."""

    def cases(self):
        for name, (mid, tick, depth) in COINS.items():
            bids, asks = synthetic(mid, tick, depth, seed=len(name))
            yield name, bids, asks

    def test_cost_never_below_the_real_cost_and_equal_when_the_finest_view_is_enough(self):
        for name, bids, asks in self.cases():
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2
            for side, levels, tag in (("bid", bids, "sell"), ("ask", asks, "buy")):
                real, st = as_floats(levels), stitched(levels, side)
                exact_qty = sum(s for _, s in real[:20])
                for notional in (1e3, 1e4, 1e5, 1e6, 1e7, 3e7):
                    c_real = B.close_cost_bps(real, mid, notional, tag)
                    c_st = B.close_cost_bps(st, mid, notional, tag)
                    if c_st is None:
                        continue
                    self.assertIsNotNone(c_real, (name, tag, notional))
                    self.assertGreaterEqual(c_st, c_real - 1e-9, (name, tag, notional))
                    if notional / mid <= exact_qty:
                        self.assertAlmostEqual(c_st, c_real, places=9, msg=(name, tag, notional))

    def test_the_stitched_book_reaches_further_than_twenty_levels(self):
        for name, bids, asks in self.cases():
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2
            real, st = as_floats(bids), stitched(bids, "bid")
            big = 0.9 * sum(p * s for p, s in real[:20]) * 6
            self.assertIsNone(B.close_cost_bps(real[:20], mid, big, "sell"), name)
            self.assertIsNotNone(B.close_cost_bps(st, mid, big, "sell"), name)

    def test_depth_is_a_lower_bound(self):
        for name, bids, asks in self.cases():
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2
            for side, levels in (("bid", bids), ("ask", asks)):
                real, st = as_floats(levels), stitched(levels, side)
                for bps in (5, 20, 100, 500):
                    d_real, _ = B.depth_within([(p, s, "x") for p, s in real], mid, bps, side)
                    d_st, _ = B.depth_within(st, mid, bps, side)
                    self.assertLessEqual(d_st, d_real + 1e-6, (name, side, bps))

    def test_a_band_beyond_the_book_says_so(self):
        _, covered = B.depth_within([(100.0, 1.0, "exact"), (99.9, 1.0, "exact")], 100.0, 500, "bid")
        self.assertFalse(covered)
        _, covered = B.depth_within([(100.0, 1.0, "exact"), (90.0, 1.0, "exact")], 100.0, 500, "bid")
        self.assertTrue(covered)


if __name__ == "__main__":
    unittest.main()
