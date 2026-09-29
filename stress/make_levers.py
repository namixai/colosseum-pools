#!/usr/bin/env python3
"""The three levers an investor sets, run through this package on the same eight crash days.

A pool's worst day depends less on its rules than on three choices around them: where the stop
sits (on the exchange, at the line; or with a keeper that closes a minute later), how much
leverage the seats may take, and which coins they may trade. This runs `pool_stress.py` once for
every combination and writes `results/levers-<date>.json`, one cell per combination, from the
snapshot in `data/`. Nothing in the other result files changes.

    python3 make_levers.py                  # writes results/levers-2026-09-29.json
    python3 make_levers.py --out /tmp/l     # writes elsewhere, e.g. to diff against results/

The long side only, as in the headline of README.md. A stop at the line has no waiting window, so
only the open is run for it; a keeper's minute has two ends, the open of the next minute and the
worst price inside it, and both are run. Each cell gives the median over the eight days of the
day's median loss, and the worst entry of the worst day, as a share of the seats' capital.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA, RESULTS = os.path.join(HERE, "data"), os.path.join(HERE, "results")
STAMP = "2026-09-29"
DAYS = ("2025-10-10", "2026-02-05", "2026-01-31", "2025-11-03",
        "2026-06-05", "2025-11-04", "2025-11-21", "2025-12-01")
# The two lists of README.md: the wide one (every coin in data/, the seats on the first five by
# name) and the default one, with the big seats on the calm coins.
LISTS = (("wide", None, None, "one seat on each of the first five coins by name: ADA, AVAX, BNB, BTC, DOGE"),
         ("default", "BTCUSDT,ETHUSDT,SOLUSDT", "BTCUSDT,ETHUSDT,SOLUSDT,SOLUSDT,SOLUSDT",
          "BTC, ETH and SOL, the big seats on the calm coins: BTC 50k, ETH 25k, SOL 3 x 5k"))
STOPS = ((0, "exchange", ("open",)), (1, "keeper", ("open", "worst")))
LEVERAGE = (3.0, 5.0)


def run(lag: int, lev: float, symbols: str | None, seats: str | None, mode: str, out: str) -> dict:
    cmd = [sys.executable, os.path.join(HERE, "pool_stress.py"), "--data-dir", DATA, "--days", ",".join(DAYS),
           "--entries", "minute", "--seats-report", "--lag", str(lag), "--lev", str(lev), "--exec", mode,
           "--json", out]
    if symbols:
        cmd += ["--symbols", symbols, "--seat-coins", seats]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pool_stress.py failed ({r.returncode}): {r.stderr[-400:]}")
    with open(out, encoding="utf-8") as f:
        rows = [x for x in json.load(f)["пул"] if x["сторона"] == "лонг"]
    if sorted(x["сутки"] for x in rows) != sorted(DAYS):
        raise SystemExit(f"expected the eight days, got {[x['сутки'] for x in rows]}")
    worst = max(rows, key=lambda x: x["худший_убыток_%_капитала_мест"])
    return {
        "median_of_days_pct": round(statistics.median(x["убыток_пула_медиана_%"] for x in rows), 2),
        "worst_day": worst["сутки"],
        "worst_entry": worst["худший_вход"],
        "worst_pct": worst["худший_убыток_%_капитала_мест"],
        "worst_usd": worst["худший_убыток_usd"],
        "seats_hit_on_worst": worst["мест_пробито_в_худшем"],
        "seats_liquidated_on_worst": worst["мест_ликвидировано_в_худшем"],
        "days_with_liquidations": sum(1 for x in rows if x["мест_ликвидировано_в_худшем"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=RESULTS)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cells = []
    with tempfile.TemporaryDirectory() as tmp:
        for (lag, stop, modes), lev, (name, symbols, seats, _) in itertools.product(STOPS, LEVERAGE, LISTS):
            for mode in modes:
                out = os.path.join(tmp, f"{stop}-{int(lev)}x-{name}-{mode}.json")
                cells.append({"stop": stop, "lag": lag, "leverage": lev, "list": name, "exec": mode,
                              **run(lag, lev, symbols, seats, mode, out)})
                c = cells[-1]
                print(f"{stop:8} {lev:.0f}x {name:7} {mode:5} median {c['median_of_days_pct']:>5}%  "
                      f"worst {c['worst_pct']:>5}%  liquidated {c['seats_liquidated_on_worst']}")
    payload = {
        "produced": STAMP,
        "note": ("The same snapshot, rules and pool as the other result files: 3% a day, 6% from the start, "
                 "a 4.5 bps taker fee, maintenance margin 2%, five seats of $50k / $25k / $5k / $5k / $5k all "
                 "entering the same minute, long side. A stop on the exchange closes at the line itself: that is "
                 "the line, not a fill in a real book, and the book in a cascade has not been measured."),
        "days": list(DAYS),
        "lists": {name: desc for name, _, _, desc in LISTS},
        "cells": cells,
    }
    path = os.path.join(a.out, f"levers-{STAMP}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"{len(cells)} cells: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
