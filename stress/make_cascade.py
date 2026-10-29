#!/usr/bin/env python3
"""The cascade on the default list at the leverage rule v1.2: how the seats sit on BTC, ETH and SOL, on 10 Oct 2025.

The Economics page shows what a cascade like 10 Oct 2025 does to a pool, for three ways of spreading its five seats
over the default list: the big seats on the calm coins, the big seat on the riskiest coin, and every seat on one
coin. Each seat takes the leverage the rule allows its coin (BTC and ETH 10x, SOL 5x; make_levers.py has the rule),
both sides are run, and the keeper closes a minute late, at the open and at the worst price of the window. This
writes `results/cascade-v1.2-<date>.json`: the tool's rows for every layout, and the cases the page reads, each the
worse of the two sides as a share of the seats' capital.

    python3 make_cascade.py                               # writes results/cascade-v1.2-2026-10-29.json
    python3 make_cascade.py --out /tmp/c                  # writes elsewhere, e.g. to diff against results/
    python3 make_cascade.py --lev 5 --in-work-bps 9000 --out /tmp/c   # every seat at 5x, the seats at 90%,
                                                          # as the layout file of 25 Sep 2026 was run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

from make_levers import DATA, IN_WORK_BPS, POOL_USD, RESULTS, RULE, RULE_LEVERAGE, RULE_NOTE, seats_usd

HERE = os.path.dirname(os.path.abspath(__file__))
STAMP = "2026-10-29"
DAY = "2025-10-10"
DEFAULT_LIST = "BTCUSDT,ETHUSDT,SOLUSDT"
# The layouts of make_results.py, and every seat on each coin of the list in turn: at the rule's leverage the
# riskiest coin is not known in advance, so the single-coin case is the worst of the three.
LAYOUTS = (("calm-seats-big", "BTCUSDT,ETHUSDT,SOLUSDT,SOLUSDT,SOLUSDT"),
           ("risky-seat-big", "SOLUSDT,ETHUSDT,BTCUSDT,BTCUSDT,BTCUSDT"),
           ("all-on-btc", "BTCUSDT,BTCUSDT,BTCUSDT,BTCUSDT,BTCUSDT"),
           ("all-on-eth", "ETHUSDT,ETHUSDT,ETHUSDT,ETHUSDT,ETHUSDT"),
           ("all-on-sol", "SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT"))
CASES = (("spread_calm", "seats spread over the list, the big ones on the calm coins", ("calm-seats-big",)),
         ("spread_risky", "seats spread over the list, the big one on the riskiest coin", ("risky-seat-big",)),
         ("single_coin", "every seat on one coin of the list, the worst of the three", ("all-on-btc", "all-on-eth", "all-on-sol")))
SIDES = {"лонг": "long", "шорт": "short"}


def k(usd: float) -> str:
    return f"{usd / 1000:.1f}".removesuffix(".0") + "k"


def human(coins: list[str], levs: list[float], capital: list[float]) -> str:
    """The layout in words: each seat's coin, size and leverage, the repeated ones once."""
    seats, out = list(zip(coins, capital, levs, strict=True)), []
    for seat in dict.fromkeys(seats):
        n = seats.count(seat)
        coin, usd, lev = seat
        out.append(f"{coin[:-4]} {f'{n} x ' if n > 1 else ''}{k(usd)} at {lev:g}x")
    return ", ".join(out)


def run(coins: str, levs: list[float], capital: list[float], mode: str, out: str) -> list[dict]:
    cmd = [sys.executable, os.path.join(HERE, "pool_stress.py"), "--data-dir", DATA, "--days", DAY,
           "--symbols", DEFAULT_LIST, "--seat-coins", coins, "--entries", "minute", "--seats-report", "--lag", "1",
           "--exec", mode, "--lev", repr(levs[0]), "--seat-lev", ",".join(map(repr, levs)),
           "--seats-usd", ",".join(map(repr, capital)), "--pool-usd", str(POOL_USD), "--json", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pool_stress.py failed ({r.returncode}): {r.stderr[-400:]}")
    with open(out, encoding="utf-8") as f:
        rows = json.load(f)["пул"]
    if sorted(x["сторона"] for x in rows) != sorted(SIDES):
        raise SystemExit(f"expected a long and a short row on {DAY}, got {[x['сторона'] for x in rows]}")
    return rows


def case(runs: list[dict], ids: tuple[str, ...], label: str, total: float) -> dict:
    """The worse side of the worse layout, at each end of the keeper's minute, as a share of the seats' capital."""
    worse = {}
    for mode in ("open", "worst"):
        rows = [(r, x) for r in runs if r["layout"] in ids and r["exec"] == mode for x in r["rows"]]
        worse[mode] = max(rows, key=lambda rx: rx[1]["худший_убыток_usd"])

    def at(r, x):
        return SIDES[x["сторона"]] if len(ids) == 1 else f"{r['seat_coins'][0][:-4]} {SIDES[x['сторона']]}"

    return {"label": label,
            **{mode: x["худший_убыток_usd"] / total for mode, (r, x) in worse.items()},
            **{f"{mode}_at": at(r, x) for mode, (r, x) in worse.items()},
            "leverage": {mode: r["leverage"] for mode, (r, x) in worse.items()},
            "layout": {mode: r["human"] for mode, (r, x) in worse.items()},
            "seats_liquidated": {mode: x["мест_ликвидировано_в_худшем"] for mode, (r, x) in worse.items()}}


def leverage_words(coins: list[str], levs: list[float]) -> str:
    by: dict[float, list[str]] = {}
    seat = dict(zip(coins, levs))
    for coin in DEFAULT_LIST.split(","):                 # the list's own order: BTC, ETH, SOL
        if coin in seat:
            by.setdefault(seat[coin], []).append(coin[:-4])
    return ", ".join(f"{' and '.join(c)} {lev:g}x" for lev, c in sorted(by.items(), key=lambda kv: -kv[0]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=RESULTS)
    ap.add_argument("--in-work-bps", type=int, default=IN_WORK_BPS)
    ap.add_argument("--lev", type=float, default=0.0, help="one leverage for every seat instead of the rule's")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    capital = seats_usd(a.in_work_bps)
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for layout, coins in LAYOUTS:
            seat_coins = coins.split(",")
            levs = [a.lev] * len(seat_coins) if a.lev else [RULE_LEVERAGE[c] for c in seat_coins]
            for mode in ("open", "worst"):
                rows = run(coins, levs, capital, mode, os.path.join(tmp, f"{layout}-{mode}.json"))
                runs.append({"layout": layout, "seat_coins": seat_coins, "seat_leverage": levs,
                             "leverage": leverage_words(seat_coins, levs), "human": human(seat_coins, levs, capital),
                             "exec": mode, "rows": rows})
                worse = max(rows, key=lambda x: x["худший_убыток_usd"])
                print(f"{layout:15} {mode:5} worse side {SIDES[worse['сторона']]:5} "
                      f"{worse['худший_убыток_%_капитала_мест']:>5}% of the seats, "
                      f"{worse['худший_убыток_%_пула']:>5}% of the pool, liquidated {worse['мест_ликвидировано_в_худшем']}")
    total = sum(capital)
    payload = {
        "produced": STAMP,
        "note": ("The snapshot and rules of the other result files (3% a day, 6% from the start, a 4.5 bps taker fee, "
                 "maintenance margin 2%) on the cascade day, the default list BTC, ETH and SOL, five seats in the proportions "
                 "50 : 25 : 5 : 5 : 5 entering the same minute, both sides, the keeper a minute late: at the open of the next "
                 "minute and at the worst price inside it."),
        "day": DAY,
        "rule": {"leverage": RULE if not a.lev else None, "note": RULE_NOTE if not a.lev else f"every seat at {a.lev:g}x"},
        "pool": {"usd": POOL_USD, "in_work_bps": a.in_work_bps, "seats_usd": [round(x, 2) for x in capital]},
        "runs": runs,
        "cases": {name: case(runs, ids, label, total) for name, label, ids in CASES},
    }
    path = os.path.join(a.out, f"cascade-v1.2-{STAMP}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"{len(runs)} runs: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
