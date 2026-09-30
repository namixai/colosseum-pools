#!/usr/bin/env python3
"""The cost of closing BTC, ETH and SOL positions at market on Hyperliquid, from a series of l2Book snapshots.

    python analyze_l2.py SERIES_DIR [--out results.json]        (SERIES_DIR: what collect_l2.py wrote)

For every snapshot and every size in SIZES_USD it walks the book (book.py: the book's own 20 levels, stitched to
coarser views only when those are not enough) and records the cost against the mid in basis points, for a long
closed by selling into the bids and a short closed by buying from the asks. Then it reports the distribution over
the whole series, over each UTC hour, the worst hour, the worst snapshots, and how much can be closed within 10, 100
and 500 basis points of the mid (500 is the stop's own slippage limit, CLOSE_SLIPPAGE_BPS in the pool contracts).

A snapshot is not an execution. The fee is not included. Nothing here is measured in a cascade.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone

import book as B

SIZES_USD = (10_000, 100_000, 1_000_000)
BANDS_BPS = (10, 100, 500)
COINS = ("BTC", "ETH", "SOL")
SIDES = (("sell", "bid"), ("buy", "ask"))          # (how the close trades, the side of the book it eats)
INF = float("inf")


def pct(sorted_vals: list, p: float) -> float:
    """Nearest-rank percentile of an ascending list; INF entries (unfilled) sort last."""
    if not sorted_vals:
        return math.nan
    return sorted_vals[max(0, math.ceil(p / 100 * len(sorted_vals)) - 1)]


def num(x, nd=3):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(x, nd)


def levels(view: dict | None, key: str) -> list:
    return [(float(px), float(sz)) for px, sz in view[key]] if view else []


def valid(bids: list, asks: list) -> bool:
    """A book that could be real: sides not crossed, bids falling, asks rising, sizes positive."""
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return False
    if any(b[0] >= a[0] for a, b in zip(bids, bids[1:])) or any(b[0] <= a[0] for a, b in zip(asks, asks[1:])):
        return False
    return all(sz > 0 for _, sz in bids + asks)


def snapshot_rows(record: dict) -> dict | None:
    """Everything one record says: mid, spread, cost per (side, size), depth per (side, band)."""
    v = record["v"]
    ex_b, ex_a = levels(v.get("5"), "b"), levels(v.get("5"), "a")
    if not valid(ex_b, ex_a):
        return None
    mid, spread = B.mid_and_spread(ex_b, ex_a)
    coarse = {"bid": [(k, levels(v.get(str(k)), "b")) for k in (4, 3, 2) if v.get(str(k))],
              "ask": [(k, levels(v.get(str(k)), "a")) for k in (4, 3, 2) if v.get(str(k))]}
    exact = {"bid": ex_b, "ask": ex_a}
    stitched = {s: B.stitch(exact[s], coarse[s], s) for s in ("bid", "ask")}
    row = {"ts": v["5"]["time"], "mid": mid, "spread_bps": spread, "cost": {}, "depth": {}}
    for how, side in SIDES:
        for size in SIZES_USD:
            c = B.close_cost_bps(exact[side], mid, size, how)
            if c is not None:
                row["cost"][(how, size)] = (c, "exact")
                continue
            c = B.close_cost_bps(stitched[side], mid, size, how)
            row["cost"][(how, size)] = (c, "stitched") if c is not None else (INF, "unfilled")
        for bps in BANDS_BPS:
            row["depth"][(side, bps)] = B.depth_within(stitched[side], mid, bps, side)
    return row


def read_series(directory: str):
    files = sorted(glob.glob(os.path.join(directory, "l2_*.jsonl.gz")))
    for f in files:
        with gzip.open(f, "rt") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue                                    # the last line of a file being written


def volatility_split(rs: list, window_s: float = 300.0, top_share: float = 5.0) -> dict | None:
    """Cost and depth in the most violent five-minute stretches of the series against the rest.

    A move is |mid now / mid five minutes ago| in bps. The top `top_share` percent of moves are the volatile set. This is not
    a cascade: it shows how far the book thins when the price merely moves fast inside the window.
    """
    moves, j = [], 0
    for i, r in enumerate(rs):
        while j < i and rs[j + 1]["ts"] <= r["ts"] - window_s * 1000:
            j += 1
        if rs[j]["ts"] <= r["ts"] - window_s * 1000:
            moves.append((i, abs(math.log(r["mid"] / rs[j]["mid"])) * 1e4))
    if len(moves) < 40:
        return None
    vals = sorted(m for _, m in moves)
    thr = pct(vals, 100 - top_share)
    hot = {i for i, m in moves if m >= thr}
    calm = {i for i, _ in moves} - hot

    def med(idx, f):
        v = sorted(f(rs[i]) for i in idx)
        return pct(v, 50)

    out = {"window_s": window_s, "top_share_percent": top_share, "threshold_move_bps": num(thr, 2),
           "n_volatile": len(hot), "n_calm": len(calm), "cost_bps_median": {}, "depth_10bps_median_usd": {}}
    for how, _ in SIDES:
        out["cost_bps_median"][how] = {str(s): {"volatile": num(med(hot, lambda r, k=(how, s): r["cost"][k][0])),
                                                "calm": num(med(calm, lambda r, k=(how, s): r["cost"][k][0]))} for s in SIZES_USD}
    for _, side in SIDES:
        out["depth_10bps_median_usd"][side] = {"volatile": num(med(hot, lambda r, s=side: r["depth"][(s, 10)][0]), 0),
                                               "calm": num(med(calm, lambda r, s=side: r["depth"][(s, 10)][0]), 0)}
    return out


def summarize(rows: dict, expected_step_s: float) -> dict:
    """rows: {coin: [row, ...]} sorted by time. The whole report as plain data."""
    out = {"sizes_usd": list(SIZES_USD), "bands_bps": list(BANDS_BPS), "coins": {}}
    for coin, rs in rows.items():
        if not rs:
            continue
        rs.sort(key=lambda r: r["ts"])
        gaps = [(b["ts"] - a["ts"]) / 1000 for a, b in zip(rs, rs[1:])]
        span_s = (rs[-1]["ts"] - rs[0]["ts"]) / 1000
        c: dict = {"snapshots": len(rs), "first_utc": iso(rs[0]["ts"]), "last_utc": iso(rs[-1]["ts"]),
                   "share_of_expected": num(len(rs) / (span_s / expected_step_s + 1), 4) if span_s else 1.0,
                   "largest_gap_s": num(max(gaps), 1) if gaps else 0, "gaps_over_2min": sum(1 for g in gaps if g > 120)}
        steps = sorted(abs(math.log(b["mid"] / a["mid"])) * 1e4 for a, b in zip(rs, rs[1:]) if 15 <= (b["ts"] - a["ts"]) / 1000 <= 30)
        spreads = sorted(r["spread_bps"] for r in rs)
        c["spread_bps"] = {"median": num(pct(spreads, 50), 4), "p95": num(pct(spreads, 95), 4), "max": num(spreads[-1], 4)}
        c["mid_usd"] = {"first": num(rs[0]["mid"], 4), "last": num(rs[-1]["mid"], 4)}
        c["mid_move_per_snapshot_bps"] = ({"n": len(steps), "median": num(pct(steps, 50), 3), "p99": num(pct(steps, 99), 2), "max": num(steps[-1], 2)}
                                          if steps else None)
        c["cost_bps"] = {}
        for how, side in SIDES:
            c["cost_bps"][how] = {}
            for size in SIZES_USD:
                key = (how, size)
                allv = sorted(r["cost"][key][0] for r in rs)
                status = [r["cost"][key][1] for r in rs]
                by_hour: dict = {}
                by_date: dict = {}
                for r in rs:
                    by_hour.setdefault(hour_of(r["ts"]), []).append(r["cost"][key][0])
                    by_date.setdefault(date_of(r["ts"]), []).append(r["cost"][key][0])
                hours = {}
                for h, vals in sorted(by_hour.items()):
                    vals.sort()
                    hours[h] = {"n": len(vals), "median": num(pct(vals, 50)), "p95": num(pct(vals, 95)), "max": num(vals[-1])}
                worst_h = max(hours, key=lambda h: (hours[h]["p95"] if hours[h]["p95"] is not None else INF, hours[h]["max"] or 0))
                best_h = min(hours, key=lambda h: (hours[h]["median"] if hours[h]["median"] is not None else INF))
                worst_snaps = sorted(rs, key=lambda r: -r["cost"][key][0])[:3]
                c["cost_bps"][how][str(size)] = {
                    "median": num(pct(allv, 50)), "p90": num(pct(allv, 90)), "p99": num(pct(allv, 99)),
                    "max": num(allv[-1]), "unfilled": status.count("unfilled"),
                    "needed_coarser_views": status.count("stitched"),
                    "by_hour_utc": hours, "worst_hour_utc": worst_h, "best_hour_utc": best_h,
                    "by_date_utc": {d: {"n": len(v), "median": num(pct(sorted(v), 50)), "p95": num(pct(sorted(v), 95))}
                                    for d, v in sorted(by_date.items())},
                    "worst_snapshots": [{"utc": iso(r["ts"]), "cost_bps": num(r["cost"][key][0]), "spread_bps": num(r["spread_bps"], 3),
                                         "mid_usd": num(r["mid"], 4)} for r in worst_snaps]}
        c["depth_usd"] = {}
        for side in ("bid", "ask"):
            c["depth_usd"][side] = {}
            for bps in BANDS_BPS:
                vals = sorted(r["depth"][(side, bps)][0] for r in rs)
                covered = sum(1 for r in rs if r["depth"][(side, bps)][1])
                by_hour: dict = {}
                for r in rs:
                    by_hour.setdefault(hour_of(r["ts"]), []).append(r["depth"][(side, bps)][0])
                hmin = {h: min(v) for h, v in by_hour.items()}
                low_h = min(hmin, key=hmin.get)
                c["depth_usd"][side][str(bps)] = {
                    "median": num(pct(vals, 50), 0), "p5": num(pct(vals, 5), 0), "min": num(vals[0], 0),
                    "band_inside_covered_range": f"{covered}/{len(rs)}", "lowest_hour_utc": low_h,
                    "lowest_hour_min": num(hmin[low_h], 0),
                    "median_by_hour_utc": {h: num(pct(sorted(v), 50), 0) for h, v in sorted(by_hour.items())}}
        c["fast_price_moves"] = volatility_split(rs)
        out["coins"][coin] = c
    return out


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H")


def date_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def manifest(directory: str) -> dict:
    files = sorted(glob.glob(os.path.join(directory, "l2_*.jsonl.gz")))
    return {"files": len(files), "bytes": sum(os.path.getsize(f) for f in files),
            "sha256_of_names_and_sizes": hashlib.sha256("|".join(f"{os.path.basename(f)}:{os.path.getsize(f)}" for f in files).encode()).hexdigest()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("series_dir")
    ap.add_argument("--out")
    a = ap.parse_args()
    rows = {c: [] for c in COINS}
    bad = 0
    for rec in read_series(a.series_dir):
        r = snapshot_rows(rec) if rec.get("coin") in rows else None
        if r is None:
            bad += 1
            continue
        rows[rec["coin"]].append(r)
    res = summarize(rows, 20.0)
    res["meta"] = {"source": "https://api.hyperliquid.xyz/info, type l2Book, mainnet, public, read only",
                   "views": ["own 5-figure grid, 20 levels", "nSigFigs 4", "nSigFigs 3", "nSigFigs 2"],
                   "cost_definition": "vwap of a market close of the dollar size (at the mid) against the book, minus the mid, in bps of the mid; "
                                      "long closed by selling into bids, short closed by buying from asks; fee not included",
                   "records_dropped": bad, "series": manifest(a.series_dir),
                   "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    text = json.dumps(res, indent=1, ensure_ascii=False)
    if a.out:
        open(a.out, "w").write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
