"""The venue's minute candles of the platform's assets, kept on disk for the payout review: the coarse screen the journal of
oracle prices is held against over every minute of a stop's time (`payout_review.candle_doubts`).

    spike/.venv/bin/python -m ops.venue_candles --out ops/state/reviews        # brings the files up to the last minute

Hyperliquid's info API (`candleSnapshot`, interval 1m, no key) gives the last 5 000 candles of a coin at most, about three
and a half days: so the review service brings these files up to date on each of its passes, and what was fetched stays.
One file a day (UTC), candles-<network>-YYYY-MM-DD.jsonl, one line a candle, in the oracle's own units (an integer with
6 - szDecimals decimals, as the price precompile gives it):

    {"a": asset, "m": the minute's first second, "l": low, "h": high, "c": close}

Only whole minutes are kept (never the one still running). A candle is the price of trades on the perp, not the oracle:
the review allows `payout_review.CANDLE_BPS` between them. It reads; it sends nothing and holds no key.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from decimal import Decimal

from ops import keeper as core
from ops import sim_network as sn
from spike.hlspike import common as c

MINUTE = 60
MAX_CANDLES = 5_000  # the info API's own limit on one coin's candles
DAY = 86_400


def units(text, sz_decimals: int) -> int:
    """A price the info API writes in dollars, in the oracle's units: 6 - szDecimals decimals."""
    return int((Decimal(str(text)) * (Decimal(10) ** (6 - sz_decimals))).to_integral_value())


def platform(net=None) -> dict[int, str]:
    """asset index -> its coin name, the platform's on this network (ops/sim_network.py)."""
    return {int(i): name for name, i in sn.sim(net or c.NETWORK).assets.items()}


def decimals(assets: dict[int, str], post=None) -> dict[int, int]:
    """Each asset's szDecimals, from the info API's `meta`, the name at its index checked against ours."""
    universe = (post or c.info_post)({"type": "meta"})["universe"]
    out = {}
    for i, name in assets.items():
        if i >= len(universe) or universe[i]["name"] != name:
            raise RuntimeError(f"the venue's asset {i} is not {name}: the candles are not read")
        out[i] = int(universe[i]["szDecimals"])
    return out


def rows_of(asset: int, sz_decimals: int, candles: list[dict], until: int) -> list[dict]:
    """The info API's candles as lines of the file: the whole minutes before `until` only."""
    out = []
    for k in candles:
        m = int(k["t"]) // 1000
        if m % MINUTE or m + MINUTE > until:
            continue
        out.append({"a": asset, "m": m, "l": units(k["l"], sz_decimals), "h": units(k["h"], sz_decimals),
                    "c": units(k["c"], sz_decimals)})
    return out


class Store:
    """The day files of one network's candles in `out`."""

    def __init__(self, out: pathlib.Path, network: str):
        self.out = out
        self.network = network

    def file_of(self, t: int) -> pathlib.Path:
        return self.out / f"candles-{self.network}-{time.strftime('%Y-%m-%d', time.gmtime(t))}.jsonl"

    def files(self) -> list[pathlib.Path]:
        return sorted(self.out.glob(f"candles-{self.network}-????-??-??.jsonl"))

    def last(self, asset: int) -> int | None:
        """The last minute held of `asset`, from the newest day files back."""
        for f in reversed(self.files()):
            got = [r["m"] for r in self._rows(f) if r["a"] == asset]
            if got:
                return max(got)
        return None

    @staticmethod
    def _rows(f: pathlib.Path) -> list[dict]:
        out = []
        for line in f.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
                out.append({"a": int(r["a"]), "m": int(r["m"]), "l": int(r["l"]), "h": int(r["h"]), "c": int(r["c"])})
            except (ValueError, TypeError, KeyError):
                continue  # a line cut short or of another shape: that minute has no candle
        return out

    def add(self, rows: list[dict]) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        by_file: dict[pathlib.Path, list[str]] = {}
        for r in rows:
            by_file.setdefault(self.file_of(r["m"]), []).append(json.dumps(r, separators=(",", ":")))
        for f, lines in by_file.items():
            with open(f, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    def load(self, lo_t: int | None = None, hi_t: int | None = None) -> dict[int, dict[int, tuple[int, int, int]]]:
        """{asset: {minute: (low, high, close)}} of the days from `lo_t` to `hi_t` (all of them when not given)."""
        lo = time.strftime("%Y-%m-%d", time.gmtime(lo_t)) if lo_t is not None else ""
        hi = time.strftime("%Y-%m-%d", time.gmtime(hi_t)) if hi_t is not None else "9999"
        out: dict[int, dict[int, tuple[int, int, int]]] = {}
        for f in self.files():
            day = f.name[len(f"candles-{self.network}-"):][:10]
            if lo <= day <= hi:
                for r in self._rows(f):
                    out.setdefault(r["a"], {})[r["m"]] = (r["l"], r["h"], r["c"])
        return out


def update(store: Store, assets: dict[int, str], now: int, post=None) -> int:
    """Each asset's candles from the minute after its last one held (the last MAX_CANDLES minutes for a new one) to the
    last whole minute before `now`, fetched and added. Returns how many were added."""
    post = post or c.info_post
    sz = decimals(assets, post)
    until = now // MINUTE * MINUTE
    added = 0
    for asset, name in sorted(assets.items()):
        last = store.last(asset)
        start = max(until - MAX_CANDLES * MINUTE, (last + MINUTE) if last is not None else 0)
        while start < until:
            end = min(until, start + MAX_CANDLES * MINUTE)
            got = post({"type": "candleSnapshot", "req": {"coin": name, "interval": "1m", "startTime": start * 1000,
                                                           "endTime": end * 1000 - 1}})
            rows = [r for r in rows_of(asset, sz[asset], got or [], until) if r["m"] >= start]
            store.add(rows)
            added += len(rows)
            start = end
    return added


def to_venue(candles: dict) -> dict:
    """The candles' closes as the review's --venue-prices map, {asset: {minute: close}} with string keys."""
    return {str(a): {str(m): k[2] for m, k in by.items()} for a, by in candles.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="the directory of the day files")
    sn.add_args(ap)
    args = ap.parse_args(argv)
    sn.selected(args, c.NETWORK)
    n = update(Store(pathlib.Path(args.out), c.NETWORK.name), platform(), int(time.time()))
    core.log("candles_added", added=n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
