#!/usr/bin/env python3
"""Rebuild everything in `results/` from the snapshot in `data/` with one command.

The four published runs (lag 0 / 1 / 2 at the open, lag 1 at the worst price) and the layout run
on the default list are produced here and nowhere else, so a reader can see exactly which command
made each file. `test_anchor.py` then checks that the tool still reproduces them field by field.

    python3 make_results.py                 # writes into results/
    python3 make_results.py --out /tmp/r    # writes elsewhere, e.g. to diff against results/
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA, RESULTS = os.path.join(HERE, "data"), os.path.join(HERE, "results")
STAMP = "2026-09-25"                       # the day the tool was last changed (review of 25 Sep 2026)
DAYS = ("2025-10-10", "2026-02-05", "2026-01-31", "2025-11-03",
        "2026-06-05", "2025-11-04", "2025-11-21", "2025-12-01")
RUNS = (("lag0", 0, "open"), ("lag1", 1, "open"), ("lag2", 2, "open"), ("lag1-worst", 1, "worst"))
DEFAULT_LIST = "BTCUSDT,ETHUSDT,SOLUSDT"
LAYOUTS = (("calm-seats-big", "BTCUSDT,ETHUSDT,SOLUSDT,SOLUSDT,SOLUSDT", "BTC 50k, ETH 25k, SOL 3x5k"),
           ("risky-seat-big", "SOLUSDT,ETHUSDT,BTCUSDT,BTCUSDT,BTCUSDT", "SOL 50k, ETH 25k, BTC 3x5k"),
           ("all-on-sol", "SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT", "SOL 5x"))
SEATS_USD = [50000, 25000, 5000, 5000, 5000]


def tool(args: list[str], out: str) -> dict:
    cmd = [sys.executable, os.path.join(HERE, "pool_stress.py"), "--data-dir", DATA, "--entries", "minute",
           "--seats-report", "--json", out] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pool_stress.py failed ({r.returncode}): {r.stderr[-400:]}")
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=RESULTS)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for tag, lag, mode in RUNS:
        path = os.path.join(a.out, f"pool-stress-{STAMP}-{tag}.json")
        res = tool(["--days", ",".join(DAYS), "--lag", str(lag), "--exec", mode], path)
        print(f"  {os.path.basename(path)}: {len(res['строки'])} rows, {len(res['пул'])} pool rows")
    layouts = {"produced": STAMP,
               "note": ("Same snapshot as the four run files; the default list BTC/ETH/SOL, five seats mapped "
                        "by --seat-coins, lag 1, execution at the open and at the worst price of the window."),
               "seats_usd": SEATS_USD, "day": "2025-10-10", "runs": []}
    tmp = os.path.join(a.out, "_layout.json")
    for layout, coins, human in LAYOUTS:
        for mode in ("open", "worst"):
            res = tool(["--days", "2025-10-10", "--symbols", DEFAULT_LIST, "--seat-coins", coins,
                        "--lag", "1", "--exec", mode], tmp)
            layouts["runs"].append({"layout": layout, "seat_coins": coins.split(","), "human": human,
                                    "exec": mode, "rows": res["пул"]})
    os.remove(tmp)
    path = os.path.join(a.out, f"layouts-default-list-{STAMP}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(layouts, ensure_ascii=False, indent=1))
    print(f"  {os.path.basename(path)}: {len(layouts['runs'])} layout runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
