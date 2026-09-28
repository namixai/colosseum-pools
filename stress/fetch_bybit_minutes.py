#!/usr/bin/env python3
"""Download the exact minute bars the pool stress test runs on, from Bybit's public API.

No key, no account, stdlib only. The bars this writes are the ones in `data/` — the snapshot
shipped with this package — so anyone can re-download them and check, byte for byte, that the
numbers in README.md come from data the exchange still serves:

    python3 fetch_bybit_minutes.py --out /tmp/fresh
    shasum -a 256 -c data/SHA256SUMS            # run from /tmp/fresh

Why a snapshot at all, when this script can fetch the data? Because an exchange may restate a
candle, delist a symbol or change an endpoint, and a number nobody can reproduce is not a
measurement any more. The snapshot pins what was measured; this script says where it came from.

Eight days, ten symbols. The days are the eight worst since 2025-10-10 by average intraday
drawdown (ranked over the full local history in the original run, then fixed here); the symbols
are the ten the original run used. Both are listed below, not computed, so this file alone tells
you what the package contains.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.bybit.com"
USER_AGENT = "usenami-pool-stress-fetch/1.0"
DAY = 86_400
MINUTE = 60
# The eight worst days since 2025-10-10 (average intraday drawdown across the ten symbols).
DAYS = ("2025-10-10", "2025-11-03", "2025-11-04", "2025-11-21",
        "2025-12-01", "2026-01-31", "2026-02-05", "2026-06-05")
# Ten USDT perpetuals. The stress test's pool takes the first five in alphabetical order
# (ADA, AVAX, BNB, BTC, DOGE) — that is the "wide list with alts"; BTC/ETH/SOL is the other one.
SYMBOLS = ("ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
           "ETHUSDT", "LINKUSDT", "SOLUSDT", "SUIUSDT", "XRPUSDT")
FIELDS = ("ts", "open", "high", "low", "close")


def day_ts(d: str) -> int:
    return calendar.timegm(time.strptime(d, "%Y-%m-%d"))


def get(api: str, symbol: str, start_ms: int, limit: int = 1000, tries: int = 4) -> list[list[str]]:
    q = urllib.parse.urlencode({"category": "linear", "symbol": symbol, "interval": "1",
                                "start": start_ms, "limit": limit})
    url = f"{api}/v5/market/kline?{q}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt == tries - 1:
                raise SystemExit(f"Bybit unreachable for {symbol}: {e}")
            time.sleep(1.5 * (attempt + 1))
            continue
        if d.get("retCode") != 0:
            # Bybit answers a healthy request with "10016 svc error" now and then (seen on
            # SOLUSDT, 25 Sep). Retrying is right; giving up on the first one would leave a
            # half-downloaded snapshot that still looks complete.
            if attempt == tries - 1:
                raise SystemExit(f"Bybit refused {symbol} {tries} times: "
                                 f"retCode={d.get('retCode')} {d.get('retMsg')}")
            time.sleep(1.5 * (attempt + 1))
            continue
        return d.get("result", {}).get("list", [])
    return []


def fetch_symbol(api: str, symbol: str, days: list[str]) -> dict[int, tuple[str, str, str, str]]:
    bars: dict[int, tuple[str, str, str, str]] = {}
    for d in days:
        t0 = day_ts(d)
        # 1440 minutes a day, 1000 rows a call: two calls cover a day with room to spare.
        for start in (t0, t0 + 1000 * MINUTE):
            for row in get(api, symbol, start * 1000):
                ts = int(row[0]) // 1000
                if t0 <= ts < t0 + DAY:
                    # Prices are stored as the exchange's own strings, with no reformatting:
                    # "%g" keeps six significant digits and turns a BTC price of 119238.1 into
                    # 119238. Measured on 25 Sep: seven result rows drifted in the last digit,
                    # and the formatting was to blame, not the exchange.
                    bars[ts] = (row[1], row[2], row[3], row[4])
    return bars


def write_csv(path: str, bars: dict[int, tuple[str, str, str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(FIELDS)
        for ts in sorted(bars):
            w.writerow([ts, *bars[ts]])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--days", default=",".join(DAYS))
    ap.add_argument("--api", default=API)
    a = ap.parse_args()
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]
    days = [d.strip() for d in a.days.split(",") if d.strip()]
    os.makedirs(a.out, exist_ok=True)

    gaps, lines = 0, []
    for s in symbols:
        bars = fetch_symbol(a.api, s, days)
        path = os.path.join(a.out, f"{s}.csv")
        write_csv(path, bars)
        per_day = [(d, sum(1 for ts in bars if day_ts(d) <= ts < day_ts(d) + DAY)) for d in days]
        short = [f"{d}:{n}" for d, n in per_day if n != 1440]
        gaps += len(short)
        # A missing minute is not a rounding detail here: the test walks bar by bar. Say it.
        print(f"{s}: {len(bars)} bars" + (f"  MISSING MINUTES -> {', '.join(short)}" if short else ""))
        with open(path, "rb") as f:
            lines.append(f"{hashlib.sha256(f.read()).hexdigest()}  {s}.csv")
    with open(os.path.join(a.out, "SHA256SUMS"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n{len(symbols)} symbols x {len(days)} days -> {a.out}")
    print("SHA256SUMS written. Verify with:  shasum -a 256 -c SHA256SUMS")
    if gaps:
        print(f"WARNING: {gaps} symbol-days are not 1440 bars. The exchange has holes there; "
              f"the numbers below were produced with the snapshot in this package, not with a re-download.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
