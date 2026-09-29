#!/usr/bin/env python3
"""A series of Hyperliquid mainnet l2Book snapshots for BTC, ETH and SOL. Public info API, read only, no keys.

    python collect_l2.py --out DIR [--interval 20] [--hours 50]

Every cycle asks each coin for four views of the same book: the book's own grid (20 levels) and the nSigFigs 4, 3 and 2
aggregations (20 buckets each), which reach further out at a coarser price. book.py stitches them. Twelve requests
of weight 2 per cycle, 72 a minute at the default 20 s, against a limit of 1200 a minute per IP.

One gzip JSON line per coin per cycle, one file per UTC hour: {"coin", "t" (local ms at the cycle start),
"v": {"5": {"time" (server ms), "b": [[px, sz], ...], "a": [...]}, "4": ..., "3": ..., "2": ...}}. Prices and sizes stay
strings as the API sends them. A view that failed is missing from "v". Writes DONE in the output directory when the run
ends on its own; collector.log gets a line every five minutes.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import signal
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

URL = "https://api.hyperliquid.xyz/info"
COINS = ("BTC", "ETH", "SOL")
VIEWS = (("5", None), ("4", 4), ("3", 3), ("2", 2))     # label, nSigFigs (None: the book's own five-figure grid)
STOP = False


def fetch(coin: str, nsf) -> dict | None:
    body = {"type": "l2Book", "coin": coin}
    if nsf:
        body["nSigFigs"] = nsf
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        bids, asks = d["levels"]
        return {"time": d["time"], "b": [[l["px"], l["sz"]] for l in bids], "a": [[l["px"], l["sz"]] for l in asks]}
    except Exception:
        return None


def log(out: str, msg: str) -> None:
    with open(os.path.join(out, "collector.log"), "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")


def on_signal(*_):
    global STOP
    STOP = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--hours", type=float, default=50.0)
    ap.add_argument("--cycles", type=int, default=0, help="stop after this many cycles (self-test)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    start = time.time()
    end = start + a.hours * 3600
    log(a.out, f"start interval={a.interval}s hours={a.hours} coins={','.join(COINS)} views={[v[0] for v in VIEWS]}")
    pool = ThreadPoolExecutor(max_workers=len(COINS) * len(VIEWS))
    n = errs = 0
    next_t = start
    while not STOP and time.time() < end and not (a.cycles and n >= a.cycles):
        wait = next_t - time.time()
        if wait > 0:
            time.sleep(min(wait, 1.0))
            continue
        t0 = time.time()
        jobs = {(c, lab): pool.submit(fetch, c, nsf) for c in COINS for lab, nsf in VIEWS}
        views = {k: f.result() for k, f in jobs.items()}
        lat = time.time() - t0
        hour_file = os.path.join(a.out, datetime.fromtimestamp(t0, timezone.utc).strftime("l2_%Y%m%d_%H.jsonl.gz"))
        with gzip.open(hour_file, "at") as f:
            for c in COINS:
                v = {lab: views[(c, lab)] for lab, _ in VIEWS if views[(c, lab)]}
                errs += len(VIEWS) - len(v)
                if "5" in v:
                    f.write(json.dumps({"coin": c, "t": int(t0 * 1000), "v": v}, separators=(",", ":")) + "\n")
        n += 1
        next_t += a.interval
        if next_t < time.time():                         # slept through cycles (laptop asleep): skip, do not burst
            next_t = time.time() + a.interval - ((time.time() - start) % a.interval)
        if n % max(1, int(300 / a.interval)) == 0:
            log(a.out, f"cycles={n} request_errors={errs} last_cycle_s={lat:.2f}")
    log(a.out, f"stop cycles={n} request_errors={errs} reason={'signal' if STOP else 'end'}")
    if not STOP:
        open(os.path.join(a.out, "DONE"), "w").write(datetime.now(timezone.utc).isoformat() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
