#!/usr/bin/env python3
"""The anchor: this package must reproduce the numbers it publishes, or go red.

`test_pool_stress.py` checks the rules on synthetic data. This file checks something else and
more brittle: that running `pool_stress.py` over the snapshot in `data/` still produces, to the
last digit, the four result files in `results/` — the ones the decision of 20 Sep 2026 was made
on — and the headline numbers quoted in README.md.

It is meant to break. If the exchange restates a candle and someone re-downloads the snapshot,
or a refactor moves a number by 0.01, this test says so instead of letting a stale claim live on
in the README.

    python3 test_anchor.py            # needs numpy (pool_stress.py does)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA, RESULTS = os.path.join(HERE, "data"), os.path.join(HERE, "results")
DAYS = ("2025-10-10", "2026-02-05", "2026-01-31", "2025-11-03",
        "2026-06-05", "2025-11-04", "2025-11-21", "2025-12-01")
WIDE = ("ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT")   # the pool's five seats
RUNS = (("lag0", 0, "open"), ("lag1", 1, "open"), ("lag2", 2, "open"), ("lag1-worst", 1, "worst"))

PASS, FAIL = [], []


def ok(what: str) -> None:
    PASS.append(what)
    print(f"  ok    {what}")


def bad(what: str, why: str) -> None:
    FAIL.append(what)
    print(f"  FAIL  {what}\n     {why}")


def run(lag: int, exec_mode: str, out: str) -> dict:
    cmd = [sys.executable, os.path.join(HERE, "pool_stress.py"),
           "--data-dir", DATA, "--days", ",".join(DAYS), "--entries", "minute",
           "--seats-report", "--lag", str(lag), "--exec", exec_mode, "--json", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pool_stress.py failed ({r.returncode}): {r.stderr[-400:]}")
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def key_of(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if k in ("сутки", "монета", "сторона", "сводка")},
                      ensure_ascii=False, sort_keys=True)


def pool_row(res: dict, day: str, side: str = "лонг") -> dict:
    return next(r for r in res["пул"] if r["сутки"] == day and r["сторона"] == side)


def coin_row(res: dict, day: str, coin: str, side: str = "лонг") -> dict:
    return next(r for r in res["строки"] if r["сутки"] == day and r["монета"] == coin and r["сторона"] == side)


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

    print("2. the four published runs reproduce, field by field:")
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for tag, lag, mode in RUNS:
            fresh = run(lag, mode, os.path.join(tmp, f"{tag}.json"))
            results[tag] = fresh
            with open(os.path.join(RESULTS, f"pool-stress-2026-09-20-{tag}.json"), encoding="utf-8") as f:
                published = json.load(f)
            diffs = []
            for section in ("строки", "сводки", "пул"):
                a = {key_of(r): r for r in published[section]}
                b = {key_of(r): r for r in fresh[section]}
                if set(a) != set(b):
                    diffs.append(f"{section}: {len(a)} rows published, {len(b)} now")
                for k in sorted(set(a) & set(b)):
                    diffs += [f"{section} {k} {f}: {v} -> {b[k].get(f)}"
                              for f, v in a[k].items() if b[k].get(f) != v]
            (ok(f"{tag}: {len(published['строки'])} rows identical to the published file")
             if not diffs else bad(f"{tag} reproduces", f"{len(diffs)} differences, first: {diffs[0][:160]}"))

    print("3. the headline numbers of the 20 Sep decision, by name:")
    r_open, r_worst = results["lag1"], results["lag1-worst"]
    cascade_open, cascade_worst = pool_row(r_open, "2025-10-10"), pool_row(r_worst, "2025-10-10")
    checks = [
        ("wide list, 10 Oct cascade, execution at the open: 83.7% of seat capital",
         cascade_open["худший_убыток_%_капитала_мест"], 83.7),
        ("wide list, same cascade, worst price in the delay window: 87.1%",
         cascade_worst["худший_убыток_%_капитала_мест"], 87.1),
        ("all seats on SOL, long, at the open: 45.24% (single_coin.open = 0.4524)",
         coin_row(r_open, "2025-10-10", "SOLUSDT")["убыток_макс_%"], 45.24),
        ("all seats on SOL, short, worst price: 88.46% — the upper end of 45.2-88.5%",
         coin_row(r_worst, "2025-10-10", "SOLUSDT", "шорт")["убыток_макс_%"], 88.46),
    ]
    for what, got, want in checks:
        ok(what) if got == want else bad(what, f"got {got}, decision says {want}")
    (ok("the cascade's worst entry is 21:18 and the exchange liquidates 3 of 5 seats")
     if cascade_open["худший_вход"] == "21:18" and cascade_open["мест_ликвидировано_в_худшем"] == 3
     else bad("21:18, 3 of 5 liquidated",
              f"{cascade_open['худший_вход']}, {cascade_open['мест_ликвидировано_в_худшем']} liquidated"))
    if cascade_open["монеты"] != list(WIDE):
        bad("the pool's five seats are the wide list", f"{cascade_open['монеты']}")
    else:
        ok("the pool's five seats are ADA, AVAX, BNB, BTC, DOGE — the wide list, named")

    print("4. an ordinary crash day is held, and the cascade is not the only day over the cap:")
    medians = [pool_row(r_open, d)["убыток_пула_медиана_%"] for d in DAYS]
    (ok(f"median pool loss per day stays in 3.0-3.3% (measured {min(medians)}-{max(medians)})")
     if 2.95 <= min(medians) and max(medians) <= 3.35
     else bad("median 3.0-3.3%", f"measured {min(medians)}-{max(medians)}"))
    worst = [pool_row(r_open, d)["худший_убыток_%_капитала_мест"] for d in DAYS]
    over6, over20 = sum(w > 6 for w in worst), sum(w > 20 for w in worst)
    (ok(f"worst entry beats the 6% cap on {over6} of 8 days, and 20% on {over20}")
     if (over6, over20) == (7, 3) else bad("7 of 8 over the cap, 3 over 20%", f"{over6} and {over20}"))

    print("5. the default list BTC/ETH/SOL, seat by seat — the layout table of the decision:")
    layouts = (
        ("BTCUSDT,ETHUSDT,SOLUSDT,SOLUSDT,SOLUSDT", "big seats on the calm coins", 23.9, 39.3),
        ("SOLUSDT,ETHUSDT,BTCUSDT,BTCUSDT,BTCUSDT", "the big seat on the risky coin", 31.7, 63.6),
        ("SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT", "all five seats on SOL", 45.2, 88.5),
    )
    with tempfile.TemporaryDirectory() as tmp:
        for coins, human, lo, hi in layouts:
            got = {}
            for mode in ("open", "worst"):
                out = os.path.join(tmp, f"{human[:6]}-{mode}.json")
                cmd = [sys.executable, os.path.join(HERE, "pool_stress.py"), "--data-dir", DATA,
                       "--days", "2025-10-10", "--symbols", "BTCUSDT,ETHUSDT,SOLUSDT",
                       "--entries", "minute", "--seats-report", "--seat-coins", coins,
                       "--lag", "1", "--exec", mode, "--json", out]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    raise SystemExit(f"layout run failed: {r.stderr[-300:]}")
                with open(out, encoding="utf-8") as f:
                    res = json.load(f)
                got[mode] = res["пул"]
            # Lower bound: execution at the open. Upper: worst price in the delay window, taking
            # whichever side is worse — for "all on SOL" the upper end is the SHORT caught by the
            # 21:21 bounce, not the long.
            low = min(r["худший_убыток_%_капитала_мест"] for r in got["open"])
            high = max(r["худший_убыток_%_капитала_мест"] for r in got["worst"])
            low_l = min(r["худший_убыток_%_капитала_мест"] for r in got["open"] if r["сторона"] == "лонг")
            liq = sum(r["мест_ликвидировано_в_худшем"] for r in got["open"] + got["worst"])
            band_ok = abs(low_l - lo) <= 0.05 and abs(high - hi) <= 0.05
            (ok(f"{human}: {low_l}-{high}% of seat capital, decision says {lo}-{hi}%")
             if band_ok else bad(f"{human} {lo}-{hi}%", f"measured {low_l}-{high}% (low over both sides {low})"))
            (ok(f"{human}: no seat is liquidated in any variant, as the decision says")
             if liq == 0 else bad(f"{human}: no liquidations", f"{liq} seats liquidated"))

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
            got = cells[i].replace("**", "").replace("\u00a0", " ")
            if float(got.split()[0]) != float(w.split()[0]):
                wrong.append(f"{day} column {i}: README {got}, results {w}")
        band = cells[4].replace("**", "")
        lo, hi = a["худший_убыток_%_капитала_мест"], b["худший_убыток_%_капитала_мест"]
        if f"{lo}–{hi} %" not in band.replace("\u00a0", " "):
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
    (ok("every number in the README's tables matches the results files")
     if not wrong else bad("README matches results", "; ".join(wrong[:4])))

    print()
    print(f"PASSED {len(PASS)} of {len(PASS) + len(FAIL)}" if not FAIL
          else f"FAILED {len(FAIL)} of {len(PASS) + len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
