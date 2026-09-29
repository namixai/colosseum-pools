#!/usr/bin/env python3
"""What it costs to close a position at market against a Hyperliquid l2Book snapshot.

The info API's l2Book gives 20 levels a side. On the book's own grid that is 2-16 basis points of depth,
so for a large close the snapshot is stitched from coarser views of the same book (nSigFigs 4, 3, 2).
A coarser view rounds every level against the taker (bids down, asks up) and sums the sizes of the levels
it swallows, so a coarse bucket is priced at its worst edge, and a bucket that overlaps a finer view is
skipped rather than counted twice. Both make the stitched book at least as thin as the real one:
a cost read from it is never lower than the cost the real book would have given.

A snapshot is not an execution: the book moves while an order is on its way, and in a cascade it is thinner.
"""
from __future__ import annotations

import math

Level = tuple  # (price, size) as floats; sizes are in the coin's own units


def bucket_width(price: float, k: int, side: str) -> float:
    """Width of the price bucket that an nSigFigs=k view reports at `price` (bid: floor, ask: ceil).

    An ask bucket priced exactly on a power of ten holds the interval below it, so it is measured with a
    hair taken off; a bid bucket on a power of ten holds the interval above it.
    """
    ref = price * (1 - 1e-12) if side == "ask" else price * (1 + 1e-12)
    return 10.0 ** (math.floor(math.log10(ref)) - k + 1)


def stitch(exact: list, coarse: list, side: str) -> list:
    """One book, best price first, from a fine view and coarser ones (`coarse`: [(k, levels)], fine to coarse).

    Returns [(price, size, source)] where source is 'exact' or 'k<n>'. A coarse bucket joins only if it lies
    wholly beyond what the previous view already covered.
    """
    out = [(px, sz, "exact") for px, sz in exact]
    if not exact:
        return out
    edge = exact[-1][0]
    for k, levels in coarse:
        if not levels:
            continue
        for px, sz in levels:
            w = bucket_width(px, k, side)
            beyond = (px + w <= edge) if side == "bid" else (px - w >= edge)
            if beyond:
                out.append((px, sz, f"k{k}"))
        edge = levels[-1][0]
    return out


def walk(book: list, qty: float) -> tuple:
    """Sell into bids or buy from asks, best first. Returns (vwap, filled_qty, deepest_price)."""
    got = cost = 0.0
    deepest = book[0][0] if book else float("nan")
    for level in book:
        px, sz = level[0], level[1]
        take = min(sz, qty - got)
        if take <= 0:
            break
        got += take
        cost += take * px
        deepest = px
        if got >= qty - 1e-12:
            break
    return (cost / got if got else float("nan")), got, deepest


def vwap_for_notional(book: list, notional: float) -> float | None:
    """Average price of trading `notional` dollars against `book`, best first (how Hyperliquid states its impact prices)."""
    left = notional
    got = spent = 0.0
    for level in book:
        px, sz = level[0], level[1]
        take = min(px * sz, left)
        got += take / px
        spent += take
        left -= take
        if left <= 1e-9:
            return spent / got
    return None


def close_cost_bps(book: list, mid: float, notional: float, side: str) -> float | None:
    """Cost of closing `notional` dollars (at mid) against `book`, in bps of notional, against the mid.

    side 'sell' closes a long against the bids, 'buy' closes a short against the asks.
    None when the book cannot absorb the size.
    """
    qty = notional / mid
    vwap, got, _ = walk(book, qty)
    if got < qty * (1 - 1e-9):
        return None
    return ((mid - vwap) if side == "sell" else (vwap - mid)) / mid * 1e4


def depth_within(book: list, mid: float, bps: float, side: str) -> tuple:
    """Notional (dollars) on `book` within `bps` of mid, and whether the band lies inside what the book covers.

    The figure is a lower bound: a coarse bucket counts at its worst edge, and one that straddles the band
    is left out.
    """
    limit = mid * (1 - bps / 1e4) if side == "bid" else mid * (1 + bps / 1e4)
    total = 0.0
    for px, sz, *_ in book:
        if (side == "bid" and px >= limit) or (side == "ask" and px <= limit):
            total += px * sz
    deepest = book[-1][0] if book else mid
    covered = (deepest <= limit) if side == "bid" else (deepest >= limit)
    return total, covered


def mid_and_spread(bids: list, asks: list) -> tuple:
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2
    return mid, (ba - bb) / mid * 1e4
