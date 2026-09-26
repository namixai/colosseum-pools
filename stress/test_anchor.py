#!/usr/bin/env python3
"""The anchor: this package must reproduce the numbers it publishes, or go red.

`test_pool_stress.py` checks the rules on synthetic data. This file checks something else and
more brittle: that running `pool_stress.py` over the snapshot in `data/` still produces, to the
last digit and in every section, the result files in `results/` (written by `make_results.py`
on 25 Sep 2026, after the review of that day), and the headline numbers quoted in README.md.

It is meant to break. If the exchange restates a candle and someone re-downloads the snapshot,
or a refactor moves a number by 0.01, or a sentence in the README drifts away from the tables,
this test says so instead of letting a stale claim live on.

    python3 test_anchor.py            # needs numpy (pool_stress.py does)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA, RESULTS = os.path.join(HERE, "data"), os.path.join(HERE, "results")
STAMP = "2026-09-25"
DAYS = ("2025-10-10", "2026-02-05", "2026-01-31", "2025-11-03",
        "2026-06-05", "2025-11-04", "2025-11-21", "2025-12-01")
WIDE = ("ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT")   # the pool's five seats
RUNS = (("lag0", 0, "open"), ("lag1", 1, "open"), ("lag2", 2, "open"), ("lag1-worst", 1, "worst"))
TOP = ("правила", "сутки", "монет", "входы")                          # everything that is not a table

PASS, FAIL = [], []


def ok(what: str) -> None:
    PASS.append(what)
    print(f"  ok    {what}")


def bad(what: str, why: str) -> None:
    FAIL.append(what)
    print(f"  FAIL  {what}\n     {why}")


def run(args: list[str], out: str) -> dict:
    cmd = [sys.executable, os.path.join(HERE, "pool_stress.py"), "--data-dir", DATA,
           "--entries", "minute", "--seats-report", "--json", out] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pool_stress.py failed ({r.returncode}): {r.stderr[-400:]}")
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def key_of(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if k in ("сутки", "монета", "сторона", "сводка")},
                      ensure_ascii=False, sort_keys=True)


def diff_rows(published: list, fresh: list, section: str) -> list[str]:
    """Every field of every row, and the set of fields itself: a key that appears or disappears is a
    difference too — that is how the rule block once drifted unnoticed."""
    a = {key_of(r): r for r in published}
    b = {key_of(r): r for r in fresh}
    diffs = []
    if set(a) != set(b):
        diffs.append(f"{section}: {len(a)} rows published, {len(b)} now")
    for k in sorted(set(a) & set(b)):
        if set(a[k]) != set(b[k]):
            diffs.append(f"{section} {k}: fields {sorted(set(a[k]) ^ set(b[k]))} differ")
        diffs += [f"{section} {k} {f}: {v} -> {b[k].get(f)}" for f, v in a[k].items() if b[k].get(f) != v]
    return diffs


def pool_row(res: dict, day: str, side: str = "лонг") -> dict:
    return next(r for r in res["пул"] if r["сутки"] == day and r["сторона"] == side)


def coin_row(res: dict, day: str, coin: str, side: str = "лонг") -> dict:
    return next(r for r in res["строки"] if r["сутки"] == day and r["монета"] == coin and r["сторона"] == side)


def readme_recipe(readme: str) -> list[str]:
    """The bash lines of the "Reproduce it" block, the first fenced block after that heading."""
    m = re.search(r"## Reproduce it.*?```bash\n(.*?)```", readme, re.S)
    return m.group(1).splitlines() if m else []


def main() -> int:
    print("1. the snapshot is the one the numbers were measured on:")
    sums = os.path.join(DATA, "SHA256SUMS")
    if not os.path.exists(sums):
        bad("data/SHA256SUMS exists", "no checksum file — the snapshot cannot vouch for itself")
    else:
        bad_files = []
        for line in open(sums, encoding="utf-8"):
            want, name = line.split()[0], line.split()[-1]
            path = os.path.join(DATA, name)
            if not os.path.exists(path):
                bad_files.append(f"{name}: missing")
                continue
            with open(path, "rb") as f:
                got = hashlib.sha256(f.read()).hexdigest()
            if got != want:
                bad_files.append(f"{name}: {got[:12]}… != {want[:12]}…")
        (ok("every CSV in data/ matches SHA256SUMS") if not bad_files
         else bad("data/ matches SHA256SUMS", "; ".join(bad_files)))

    print("2. the four published runs reproduce, every section, every field:")
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for tag, lag, mode in RUNS:
            fresh = run(["--days", ",".join(DAYS), "--lag", str(lag), "--exec", mode], os.path.join(tmp, f"{tag}.json"))
            results[tag] = fresh
            with open(os.path.join(RESULTS, f"pool-stress-{STAMP}-{tag}.json"), encoding="utf-8") as f:
                published = json.load(f)
            diffs = [f"{k}: {published.get(k)} -> {fresh.get(k)}" for k in TOP if published.get(k) != fresh.get(k)]
            if set(published) != set(fresh):
                diffs.append(f"top-level sections differ: {sorted(set(published) ^ set(fresh))}")
            for section in ("строки", "сводки", "пул"):
                diffs += diff_rows(published[section], fresh[section], section)
            (ok(f"{tag}: rules, days and {len(published['строки'])} rows identical to the published file")
             if not diffs else bad(f"{tag} reproduces", f"{len(diffs)} differences, first: {diffs[0][:160]}"))

    print("3. the headline numbers, by name:")
    r_open, r_worst = results["lag1"], results["lag1-worst"]
    cascade_open, cascade_worst = pool_row(r_open, "2025-10-10"), pool_row(r_worst, "2025-10-10")
    checks = [
        ("wide list, 10 Oct cascade, execution at the open: 83.7% of seat capital ($75 299)",
         (cascade_open["худший_убыток_%_капитала_мест"], cascade_open["худший_убыток_usd"]), (83.7, 75299)),
        ("wide list, same cascade, worst price with liquidated seats losing everything: 94.1% ($84 712)",
         (cascade_worst["худший_убыток_%_капитала_мест"], cascade_worst["худший_убыток_usd"]), (94.1, 84712)),
        ("all seats on SOL, long, at the open: 45.24% (single_coin.open = 0.4524)",
         coin_row(r_open, "2025-10-10", "SOLUSDT")["убыток_макс_%"], 45.24),
        ("all seats on SOL, short, worst price: 100.0% — five liquidated shorts, the margin gone",
         coin_row(r_worst, "2025-10-10", "SOLUSDT", "шорт")["убыток_макс_%"], 100.0),
        ("the same SOL short at the open: 34.07% — the bounce liquidates only at the worst price",
         coin_row(r_open, "2025-10-10", "SOLUSDT", "шорт")["убыток_макс_%"], 34.07),
    ]
    for what, got, want in checks:
        ok(what) if got == want else bad(what, f"got {got}, README says {want}")
    (ok("the cascade's worst entry is 21:18 and the exchange liquidates 3 of 5 seats at both ends")
     if cascade_open["худший_вход"] == "21:18" and cascade_open["мест_ликвидировано_в_худшем"] == 3
     and cascade_worst["мест_ликвидировано_в_худшем"] == 3
     else bad("21:18, 3 of 5 liquidated", f"{cascade_open['худший_вход']}, {cascade_open['мест_ликвидировано_в_худшем']} "
              f"/ {cascade_worst['мест_ликвидировано_в_худшем']} liquidated"))
    if cascade_open["монеты"] != list(WIDE):
        bad("the pool's five seats are the wide list", f"{cascade_open['монеты']}")
    else:
        ok("the pool's five seats are ADA, AVAX, BNB, BTC, DOGE — the wide list, named")
    long_w, short_w = [s for s in r_worst["сводки"] if s["сводка"].startswith("лонг")][0], \
                      [s for s in r_worst["сводки"] if s["сводка"].startswith("шорт")][0]
    (ok("short-side liquidations are flagged (0.02% of entries at the worst price, as for longs)")
     if short_w["ликвидаций_%"] == 0.02 and long_w["ликвидаций_%"] == 0.02
     else bad("short liquidations flagged", f"long {long_w['ликвидаций_%']}%, short {short_w['ликвидаций_%']}%"))
    (ok("overshoot maximum: 89 pp at the open, 97 pp at the worst price")
     if [s for s in r_open["сводки"] if s["сводка"].startswith("лонг")][0]["перелёт_макс_пп"] == 89.06
     and long_w["перелёт_макс_пп"] == 97.0
     else bad("overshoot maxima 89 / 97 pp", f"{long_w['перелёт_макс_пп']}"))

    print("4. an ordinary crash day is held, and the cascade is not the only day over the cap:")
    medians = [pool_row(r_open, d)["убыток_пула_медиана_%"] for d in DAYS]
    (ok(f"median pool loss per day stays in 3.0-3.3% (measured {min(medians)}-{max(medians)})")
     if 2.95 <= min(medians) and max(medians) <= 3.35
     else bad("median 3.0-3.3%", f"measured {min(medians)}-{max(medians)}"))
    worst = [pool_row(r_open, d)["худший_убыток_%_капитала_мест"] for d in DAYS]
    over6, over20 = sum(w > 6 for w in worst), sum(w > 20 for w in worst)
    (ok(f"worst entry beats the 6% cap on {over6} of 8 days, and 20% on {over20}")
     if (over6, over20) == (7, 3) else bad("7 of 8 over the cap, 3 over 20%", f"{over6} and {over20}"))

    print("5. the default list BTC/ETH/SOL, seat by seat — the layout table, and the layout file:")
    layouts = (
        ("calm-seats-big", "BTCUSDT,ETHUSDT,SOLUSDT,SOLUSDT,SOLUSDT", "big seats on the calm coins", 23.9, 40.0, 3),
        ("risky-seat-big", "SOLUSDT,ETHUSDT,BTCUSDT,BTCUSDT,BTCUSDT", "the big seat on the risky coin", 31.7, 70.0, 1),
        ("all-on-sol", "SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT", "all five seats on SOL", 45.2, 100.0, 5),
    )
    with open(os.path.join(RESULTS, f"layouts-default-list-{STAMP}.json"), encoding="utf-8") as f:
        published_layouts = json.load(f)
    pub = {(r["layout"], r["exec"]): r for r in published_layouts["runs"]}
    with tempfile.TemporaryDirectory() as tmp:
        for layout, coins, human, lo, hi, liq_short in layouts:
            got = {}
            for mode in ("open", "worst"):
                res = run(["--days", "2025-10-10", "--symbols", "BTCUSDT,ETHUSDT,SOLUSDT", "--seat-coins", coins,
                           "--lag", "1", "--exec", mode], os.path.join(tmp, f"{layout}-{mode}.json"))
                got[mode] = res["пул"]
                p = pub.get((layout, mode))
                diffs = diff_rows(p["rows"], res["пул"], "пул") if p else ["no such run in the layout file"]
                if p and p["seat_coins"] != coins.split(","):
                    diffs.append(f"seat_coins {p['seat_coins']}")
                (ok(f"{human}, {mode}: identical to the layout file")
                 if not diffs else bad(f"{human}, {mode} matches the layout file", diffs[0][:160]))
            # Lower bound: the long side at the open (that is what the README table quotes). Upper:
            # worst price over both sides — for all three layouts it is the SHORT caught by the 21:21
            # bounce, and at that bounce the exchange liquidates the SOL seats.
            low_l = min(r["худший_убыток_%_капитала_мест"] for r in got["open"] if r["сторона"] == "лонг")
            high = max(r["худший_убыток_%_капитала_мест"] for r in got["worst"])
            high_side = max(got["worst"], key=lambda r: r["худший_убыток_%_капитала_мест"])["сторона"]
            (ok(f"{human}: {low_l}-{high}% of seat capital, README says {lo}-{hi}%")
             if abs(low_l - lo) <= 0.05 and abs(high - hi) <= 0.05
             else bad(f"{human} {lo}-{hi}%", f"measured {low_l}-{high}%"))
            liq_open = sum(r["мест_ликвидировано_в_худшем"] for r in got["open"])
            liq_w_long = sum(r["мест_ликвидировано_в_худшем"] for r in got["worst"] if r["сторона"] == "лонг")
            liq_w_short = sum(r["мест_ликвидировано_в_худшем"] for r in got["worst"] if r["сторона"] == "шорт")
            (ok(f"{human}: none liquidated at the open, none on the long side; {liq_short} SOL seats on the short side at the worst price")
             if (liq_open, liq_w_long, liq_w_short, high_side) == (0, 0, liq_short, "шорт")
             else bad(f"{human}: liquidation pattern 0 / 0 / {liq_short}, upper end short",
                      f"open {liq_open}, worst long {liq_w_long}, worst short {liq_w_short}, upper side {high_side}"))

    print("6. README.md still says what the results say:")
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    wrong = []
    for day in DAYS:
        row = next((ln for ln in readme.splitlines() if ln.startswith(f"| {day} ")), None)
        if row is None:
            wrong.append(f"{day}: no row in README")
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        a, b = pool_row(r_open, day), pool_row(r_worst, day)
        want = [f"{a['убыток_пула_медиана_%']} %", f"{a['убыток_пула_p99_%']} %", f"{b['убыток_пула_p99_%']} %"]
        for i, w in enumerate(want, start=1):
            got = cells[i].replace("**", "").replace(" ", " ")
            if float(got.split()[0]) != float(w.split()[0]):
                wrong.append(f"{day} column {i}: README {got}, results {w}")
        band = cells[4].replace("**", "")
        lo, hi = a["худший_убыток_%_капитала_мест"], b["худший_убыток_%_капитала_мест"]
        if f"{lo}–{hi} %" not in band.replace(" ", " "):
            wrong.append(f"{day} worst entry: README '{band}', results {lo}–{hi} %")
        if a["худший_вход"] not in band:
            wrong.append(f"{day} worst minute: README '{band}', results {a['худший_вход']}")
        if str(a["мест_ликвидировано_в_худшем"]) != cells[6].replace("**", ""):
            wrong.append(f"{day} liquidated: README {cells[6]}, results {a['мест_ликвидировано_в_худшем']}")
    for coin, side_ru, side_col in (("BTCUSDT", "лонг", 1), ("ETHUSDT", "лонг", 1), ("SOLUSDT", "лонг", 1),
                                    ("BTCUSDT", "шорт", 2), ("ETHUSDT", "шорт", 2), ("SOLUSDT", "шорт", 2)):
        row = next((ln for ln in readme.splitlines() if ln.startswith(f"| {coin[:3]} |")), None)
        if row is None:
            wrong.append(f"{coin}: no per-coin row in README")
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        want = (f"{coin_row(r_open, '2025-10-10', coin, side_ru)['убыток_макс_%']} → "
                f"{coin_row(r_worst, '2025-10-10', coin, side_ru)['убыток_макс_%']} %")
        if cells[side_col] != want:
            wrong.append(f"{coin} {side_ru}: README '{cells[side_col]}', results '{want}'")
    dollars = f"${cascade_open['худший_убыток_usd']:,} to ${cascade_worst['худший_убыток_usd']:,} of $90 000".replace(",", " ")
    if dollars not in readme:
        wrong.append(f"dollar figure: README lacks '{dollars}'")
    for layout, coins, human, lo, hi, _ in layouts:
        band = f"{lo}–{hi:.1f} %" if hi != 100.0 else f"{lo}–100 %"      # as the README table writes them
        if f"| {band} |" not in readme:
            wrong.append(f"layout table: README lacks '{band}' for {human}")
    (ok("every number in the README's tables matches the results files")
     if not wrong else bad("README matches results", "; ".join(wrong[:4])))

    print("7. README.md says what the tool does (the sentences the review caught):")
    bracket = ("the open of the next minute (`--lag 1 --exec open`, a\nliquidated seat keeps its maintenance margin) "
               "to the worst price inside that minute's delay window\n(`--lag 1 --exec worst`, a liquidated seat loses everything)")
    (ok("the bracket sentence names lag 1 open → lag 1 worst, the modes the tables use")
     if bracket in readme and "honest bracket is\n`--lag 0`" not in readme
     else bad("bracket sentence", "README does not define the bracket as lag 1 open → lag 1 worst"))
    (ok("liquidation is described as a bracket, with Hyperliquid's words, not as 'the seat loses everything' alone")
     if "any remaining collateral" in readme and "backstop liquidation" in readme and "loses everything |" not in readme
     else bad("liquidation sentence", "README still states one residual for a liquidated seat"))
    (ok("the default list is no longer claimed liquidation-free: the short side is named")
     if "No seat is liquidated in any variant" not in readme and "on the short side, at the worst price, every SOL seat is" in readme
     else bad("default-list liquidation sentence", "README still claims no seat is liquidated in any variant"))
    recipe = readme_recipe(readme)
    cwd_moves = [ln for ln in recipe if re.match(r"\s*cd\s", ln)]                 # a bare cd changes every later line
    anchor_lines = [ln for ln in recipe if "test_anchor.py" in ln]
    sums_ok = any("data/SHA256SUMS" in ln and ("$PKG" in ln or "$OLDPWD" in ln) for ln in recipe)
    (ok("the recipe runs test_anchor.py from the package directory and checks the fresh bars against data/SHA256SUMS")
     if recipe and not cwd_moves and anchor_lines and anchor_lines[0].strip().startswith("python3 test_anchor.py") and sums_ok
     else bad("reproduce recipe", f"cd lines {cwd_moves}, anchor lines {anchor_lines}, sums path anchored {sums_ok}"))
    (ok("make_results.py is named as the origin of results/")
     if "make_results.py" in readme and os.path.exists(os.path.join(HERE, "make_results.py"))
     else bad("results provenance", "README does not name make_results.py, or the file is missing"))

    print()
    print(f"PASSED {len(PASS)} of {len(PASS) + len(FAIL)}" if not FAIL
          else f"FAILED {len(FAIL)} of {len(PASS) + len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
