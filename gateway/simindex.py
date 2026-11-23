"""The events of a SimPoolFactory's pools, read once on the host and handed to the site's panels.

    SIM_INDEX_RECORD=deployments/testnet-rehearsal998.json SIM_INDEX_DIR=/var/lib/colosseum-sim-index \\
        spike/.venv/bin/python -m gateway.simindex

Why it exists. A pool of exams keeps no list of its codes or of its stops: the owner's "Codes & shares",
the operator's stops and the partner's cabinet found them in the pool's events, read from the browser
through the public node a window of blocks at a time. A pool runs 30 days and more, a block is about a
second, so its history is millions of blocks and thousands of `eth_getLogs`: the panels showed an empty
list, a node's refusal, or a cabinet still reading after a minute (site deploy, 23 Nov 2026).

What it is. One process per factory. It reads the factory's `PoolCreated` and its pools' `CodeSet`,
`SharePaid`, `PayoutStopped`, `StopLifted` and `StopFinal` from the factory's first block to the newest
block less `SIM_INDEX_LAG`, a window at a time, keeps them in one JSON file under `SIM_INDEX_DIR`, and
answers three GET routes:

    GET /v1/simindex/health          how far it has read, how far behind the chain, its last error
    GET /v1/simindex/pool?pool=0x…   one pool's events and the blocks they were read from
    GET /v1/simindex/reviews?pool=0x…&ids=3,4
                                     the payout reviews the review service wrote for those payouts of that pool
                                     (SIM_INDEX_REVIEWS, its folder), as they are, for the operator's page (`Reviews`)

It holds no key and takes nothing from outside: no POST, no route that writes, no request that changes
what it reads. The site does not take its word either: each code, stop and send it lists is a candidate,
which the page reads from the chain (`codeOf`, `shareDue`, `payout`, the send's receipt) before it shows
it, and the blocks after the index's last one the page reads itself (app/lib/simindex.js).

Restart and lost connection. The file is written after each window that found something and at least
every `SAVE_EVERY_S` while it reads, by a write to a file beside it and a rename, so a restart finds the
last whole file and reads on from its last block; an event read twice is kept once (block and log index).
A node that fails or throttles is asked again after 1 s, then 2, 4… up to 60 s, from the same block. A
node that refuses a window as too wide gets one half as wide, down to one block.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import requests
from eth_utils import keccak, to_checksum_address

from . import NETWORK
from .chain import RATE_LIMITED, Throttled

FILE_VERSION = 1
DEFAULT_DIR = "/var/lib/colosseum-sim-index"
DEFAULT_BIND = "127.0.0.1:8789"
# The public node answers a range of 1 000 blocks (measured from the site, app/views/simcodes.js); one that
# wants less is met by halving (`_logs`), and the width found is kept for the windows after.
DEFAULT_WINDOW = 1000
# A pause between two reads while behind: the public node allows about 100 calls a minute per address, and the
# gateway and the keepers on the same host share that count (docs/GATEWAY.md, "Secrets and where it runs").
# 1.5 s is 40 reads a minute: 30 days of blocks, 2 600 windows of 1 000, in about 65 minutes, once.
DEFAULT_PAUSE_S = 1.5
# Once caught up, a pass this often: the newest block and, when there are new blocks, one read of their logs.
# The page reads the few blocks after the index's last one itself.
DEFAULT_EVERY_S = 15.0
# Blocks short of the newest that are left for the next pass: HyperEVM's blocks are final when made, and the
# two are a margin for a node that answers a block before its logs.
DEFAULT_LAG = 2
SAVE_EVERY_S = 10.0
RETRY_MAX_S = 60.0
RPC_ATTEMPTS = 5
# A window this many reads in a row went through at a width below the start's is tried twice as wide again.
GROW_AFTER = 50
USER_AGENT = "colosseum-pools-sim-index"
REQUEST_TIMEOUT_S = 10.0

SIGNATURES = {
    "PoolCreated": "PoolCreated(address,address,uint256)",
    "CodeSet": "CodeSet(bytes32,address,uint16,address,uint16)",
    "SharePaid": "SharePaid(bytes32,address,uint256,bool)",
    "PayoutStopped": "PayoutStopped(uint256,address,uint64,uint8,address,bytes32[],bytes32[],uint256,uint256,uint256)",
    "StopLifted": "StopLifted(uint256,uint256,uint256,uint256)",
    "StopFinal": "StopFinal(uint256,uint64,uint256)",
}
TOPICS = {name: "0x" + keccak(text=sig).hex() for name, sig in SIGNATURES.items()}
NAMES = {topic: name for name, topic in TOPICS.items()}
POOL_TOPICS = [TOPICS[n] for n in ("CodeSet", "SharePaid", "PayoutStopped", "StopLifted", "StopFinal")]

# `\Z` and not `$`: `$` also matches before a trailing newline, which would let "0x…\n" through.
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
WORD = re.compile(r"0x[0-9a-fA-F]{64}\Z")


def log_line(**fields) -> None:
    print(json.dumps({"t": int(time.time()), "part": "sim-index", **fields}, default=str), flush=True)


class WindowRefused(RuntimeError):
    """The node answered the logs of a window with an error that is not a throttle: too wide, too many logs,
    or a fault of its own. A narrower window is the one thing worth trying."""


class Rpc:
    """JSON-RPC over HTTP with the gateway's patience for a throttled node (gateway/chain.py)."""

    def __init__(self, url: str, session: Any = None, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = REQUEST_TIMEOUT_S):
        self.url = url
        self._sleep = sleep
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()
        if session is None:
            self._session.headers.update({"User-Agent": USER_AGENT, "Content-Type": "application/json"})

    def call(self, method: str, params: list) -> Any:
        delay = 0.25
        for attempt in range(RPC_ATTEMPTS):
            resp = self._session.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                      timeout=self.timeout)
            if resp.status_code != 429:
                resp.raise_for_status()
                body = resp.json()
                error = body.get("error") if isinstance(body, dict) else {"message": "not a JSON-RPC answer"}
                if error is None:
                    return body["result"]
                if not (isinstance(error, dict) and error.get("code") == RATE_LIMITED):
                    if method == "eth_getLogs":
                        raise WindowRefused(f"{method}: {str(error)[:200]}")
                    raise RuntimeError(f"{method}: {str(error)[:200]}")
            if attempt + 1 < RPC_ATTEMPTS:
                self._sleep(delay)
                delay *= 2
        raise Throttled(f"{method}: rate limited {RPC_ATTEMPTS} times in a row")

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def logs(self, addresses: list[str], topics: list[str], start: int, end: int) -> list[dict]:
        out = self.call("eth_getLogs", [{"address": addresses, "topics": [topics], "fromBlock": hex(start),
                                         "toBlock": hex(end)}])
        if not isinstance(out, list):
            raise WindowRefused("eth_getLogs: not a list")
        return out


def _int(hex_or_int: Any) -> int:
    return hex_or_int if isinstance(hex_or_int, int) else int(str(hex_or_int), 16)


def _address(word: str) -> str:
    return to_checksum_address("0x" + word[-40:])


def _data_words(data: str) -> list[int]:
    raw = data[2:] if data.startswith("0x") else data
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw) - len(raw) % 64, 64)]


def decode(log: dict) -> dict | None:
    """One log as the index keeps it: its name, block, index, transaction and the fields the panels look up.
    A log of another shape (an odd answer from the node) is None, never a guess."""
    try:
        topics = [str(t).lower() for t in log["topics"]]
        name = NAMES.get(topics[0]) if topics else None
        if name is None:
            return None
        base = {"name": name, "address": to_checksum_address(log["address"]), "block": _int(log["blockNumber"]),
                "index": _int(log["logIndex"]), "tx": str(log["transactionHash"]).lower()}
        words = _data_words(str(log.get("data") or "0x"))
        if name == "PoolCreated":
            return {**base, "pool": _address(topics[1]), "creator": _address(topics[2])}
        if name == "CodeSet":
            return {**base, "code": topics[1], "partner": _address(topics[2]), "author": _address(topics[3]),
                    "bps": words[0], "authorBps": words[1]}
        if name == "SharePaid":
            return {**base, "code": topics[1], "to": _address(topics[2]), "amount": str(words[0]),
                    "sent": bool(words[1])}
        if name == "PayoutStopped":
            return {**base, "id": int(topics[1], 16), "trader": _address(topics[2])}
        return {**base, "id": int(topics[1], 16)}  # StopLifted, StopFinal
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class SimIndex:
    """What has been read of one factory's pools, the reading, and the file it is kept in."""

    def __init__(self, rpc: Rpc, chain_id: int, factory: str, from_block: int, path: pathlib.Path,
                 window: int = DEFAULT_WINDOW, lag: int = DEFAULT_LAG, pause_s: float = DEFAULT_PAUSE_S,
                 every_s: float = DEFAULT_EVERY_S, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        if window < 1:
            raise ValueError("the window is at least one block")
        self.rpc = rpc
        self.chain_id = chain_id
        self.factory = to_checksum_address(factory)
        self.from_block = from_block
        self.path = pathlib.Path(path)
        self.max_window = window
        self.window = window
        self.lag = lag
        self.pause_s = pause_s
        self.every_s = every_s
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.scanned_to = from_block - 1
        self.pools: dict[str, dict] = {}   # checksummed pool -> {"created", "creator"}
        self.events: dict[str, dict[str, dict]] = {}  # pool -> "block:index" -> event
        self.head: int | None = None
        self.last_error: str | None = None
        self.errors = 0
        self.started = clock()
        self.updated_at: float | None = None
        self._saved_at = 0.0
        self._dirty = False
        self._good = 0

    # ── the file ─────────────────────────────────────────────────────────────────────

    def load(self) -> str:
        """Takes up the file, if it is this factory's: "loaded", "none" (no file), "other" (another chain,
        factory or first block: set aside, read afresh) or "bad" (not readable: set aside, read afresh)."""
        if not self.path.exists():
            return "none"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            same = (data.get("version") == FILE_VERSION and data.get("chain_id") == self.chain_id
                    and str(data.get("factory", "")).lower() == self.factory.lower()
                    and data.get("from_block") == self.from_block)
            if not same:
                self._set_aside("other")
                return "other"
            pools = {to_checksum_address(p): {"created": int(v["created"]), "creator": to_checksum_address(v["creator"])}
                     for p, v in data["pools"].items()}
            events = {to_checksum_address(p): {f"{e['block']}:{e['index']}": e for e in evs}
                      for p, evs in data["events"].items()}
            scanned_to = int(data["scanned_to"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self._set_aside("bad")
            return "bad"
        with self.lock:
            self.pools, self.events, self.scanned_to = pools, events, max(scanned_to, self.from_block - 1)
            self.updated_at = data.get("updated_at")
        return "loaded"

    def _set_aside(self, why: str) -> None:
        aside = self.path.with_name(f"{self.path.name}.{why}-{int(self.clock())}")
        try:
            os.replace(self.path, aside)
        except OSError:
            pass
        log_line(event="file_set_aside", why=why, to=str(aside))

    def save(self) -> None:
        with self.lock:
            data = {"version": FILE_VERSION, "chain_id": self.chain_id, "factory": self.factory,
                    "from_block": self.from_block, "scanned_to": self.scanned_to, "updated_at": self.updated_at,
                    "pools": self.pools,
                    "events": {p: sorted(evs.values(), key=lambda e: (e["block"], e["index"]))
                               for p, evs in self.events.items()}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._saved_at = self.clock()
        self._dirty = False

    # ── reading ──────────────────────────────────────────────────────────────────────

    def _logs(self, addresses: list[str], topics: list[str], start: int, end: int) -> tuple[list[dict], int]:
        """The logs of [start, end], or of a narrower window from `start` where the node refuses this one.
        Returns the logs and the last block they cover."""
        while True:
            last = min(end, start + self.window - 1)
            try:
                logs = self.rpc.logs(addresses, topics, start, last)
            except Throttled:
                # Hyperliquid's public node answers a window it finds too heavy as "rate limited" (23 Nov 2026: 1 000
                # and 100 blocks both, at a time it answered eth_blockNumber): the next try, after the wait, is narrower.
                if self.window > 1:
                    self.window = max(1, self.window // 2)
                    self._good = 0
                    log_line(event="window_narrowed", window=self.window, why="throttled")
                raise
            except WindowRefused:
                if self.window == 1:
                    raise
                self.window = max(1, self.window // 2)
                self._good = 0
                log_line(event="window_narrowed", window=self.window)
                continue
            self._good += 1
            if self.window < self.max_window and self._good >= GROW_AFTER:
                self.window = min(self.max_window, self.window * 2)
                self._good = 0
            return logs, last

    def _keep(self, ev: dict) -> bool:
        key = f"{ev['block']}:{ev['index']}"
        bucket = self.events.setdefault(ev["address"], {})
        if key in bucket:
            return False
        bucket[key] = {k: v for k, v in ev.items() if k != "address"}
        return True

    def step(self) -> int:
        """Reads the next window up to the newest block less the lag. Returns the blocks read (0: nothing new)."""
        start = self.scanned_to + 1
        # The newest block is asked for only when the reading has reached the last one known: while behind, one
        # read a window.
        if self.head is None or start > self.head - self.lag:
            head = self.rpc.block_number()
            with self.lock:
                self.head = head
        top = self.head - self.lag
        if start > top:
            return 0
        addresses = [self.factory, *self.pools]
        logs, last = self._logs(addresses, [TOPICS["PoolCreated"], *POOL_TOPICS], start, top)
        found = [e for e in (decode(log) for log in logs) if e is not None]
        # A pool made in this window has its own events after its creation in the same window, and its address
        # was not in the request: the window from its creation is read again for it alone.
        new = {e["pool"]: e for e in found if e["name"] == "PoolCreated" and e["address"] == self.factory
               and e["pool"] not in self.pools}
        if new:
            first = min(e["block"] for e in new.values())
            more, _ = self._logs_exact(list(new), POOL_TOPICS, first, last)
            found += [e for e in (decode(log) for log in more) if e is not None]
        changed = False
        with self.lock:
            for pool, e in new.items():
                self.pools[pool] = {"created": e["block"], "creator": e["creator"]}
                changed = True
            for e in found:
                if e["name"] == "PoolCreated":
                    continue
                if e["address"] in self.pools:
                    changed = self._keep(e) or changed
            self.scanned_to = last
            self.updated_at = self.clock()
            self._dirty = True
        if changed or self.clock() - self._saved_at >= SAVE_EVERY_S:
            self.save()
        return last - start + 1

    def _logs_exact(self, addresses: list[str], topics: list[str], start: int, end: int) -> tuple[list[dict], int]:
        """The logs of all of [start, end], in as many windows as the node's width takes."""
        out: list[dict] = []
        at = start
        while at <= end:
            logs, last = self._logs(addresses, topics, at, end)
            out += logs
            at = last + 1
        return out, end

    def run(self, stop: threading.Event | None = None) -> None:
        """Reads on for as long as the process lives: windows back to back while behind, a pass every few
        seconds once caught up, and a failure waited out with a growing pause from the same block."""
        stop = stop or threading.Event()
        delay = 1.0
        while not stop.is_set():
            try:
                read = self.step()
                with self.lock:
                    self.last_error = None
                delay = 1.0
                stop.wait(self.pause_s if read else self.every_s)
            except Exception as exc:  # the node, the network, the disk: said, waited out, read again
                with self.lock:
                    self.errors += 1
                    self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                log_line(event="read_failed", error=self.last_error, wait_s=delay, at=self.scanned_to + 1)
                if self._dirty:
                    try:
                        self.save()
                    except OSError as disk:
                        log_line(event="save_failed", error=str(disk)[:200])
                stop.wait(delay)
                delay = min(delay * 2, RETRY_MAX_S)

    # ── answers ──────────────────────────────────────────────────────────────────────

    def health(self) -> dict:
        with self.lock:
            behind = None if self.head is None else max(0, self.head - self.scanned_to)
            return {"ok": True, "chain_id": self.chain_id, "factory": self.factory, "from_block": self.from_block,
                    "scanned_to": self.scanned_to, "head": self.head, "behind_blocks": behind,
                    "pools": len(self.pools), "window": self.window, "updated_at": self.updated_at,
                    "last_error": self.last_error, "errors": self.errors}

    def pool(self, pool: str) -> tuple[int, dict]:
        if not isinstance(pool, str) or not ADDRESS.match(pool):
            return 400, {"status": "bad_request", "code": "bad_pool", "detail": "pool must be a 0x address"}
        at = to_checksum_address(pool)
        with self.lock:
            meta = self.pools.get(at)
            common = {"chain_id": self.chain_id, "factory": self.factory, "scanned_to": self.scanned_to,
                      "head": self.head}
            if meta is None:
                return 404, {"status": "not_found", "code": "unknown_pool", **common,
                             "detail": "this index has not seen the pool made by its factory up to block "
                                       f"{self.scanned_to}"}
            events = sorted(self.events.get(at, {}).values(), key=lambda e: (e["block"], e["index"]))
            return 200, {"status": "ok", "pool": at, "created": meta["created"], "creator": meta["creator"],
                         **common, "events": [dict(e) for e in events]}


class Reviews:
    """The payout reviews the review service wrote (ops/review_service.py, `<pool>-<id>.json` under its --out), handed
    to the operator's page as they are, read only. The page checks each one itself, by the rules `deploy_sim approve`
    checks it with (app/lib/operator.js, `reviewFor`): this only finds the file. A file's name is built here from a pool
    this index knows (its factory made it) and whole numbers; nothing of the request is a path. No listing, no other
    file of the folder (the service's `said.json`, its `ALARM`), nothing written."""

    def __init__(self, directory: pathlib.Path | None):
        self.directory = directory

    def answer(self, index: SimIndex, pool: str, ids: str) -> tuple[int, dict]:
        if self.directory is None:
            return 404, {"status": "not_found", "code": "reviews_off",
                         "detail": "this index is given no folder of payout reviews (SIM_INDEX_REVIEWS)"}
        if not isinstance(pool, str) or not ADDRESS.match(pool):
            return 400, {"status": "bad_request", "code": "bad_pool", "detail": "pool must be a 0x address"}
        if not isinstance(ids, str) or not IDS.match(ids):
            return 400, {"status": "bad_request", "code": "bad_ids",
                         "detail": f"ids must be up to {REVIEWS_MAX_IDS} payout numbers, separated by commas"}
        numbers = sorted({int(x) for x in ids.split(",")})
        if len(numbers) > REVIEWS_MAX_IDS:
            return 400, {"status": "bad_request", "code": "bad_ids",
                         "detail": f"ids must be up to {REVIEWS_MAX_IDS} payout numbers, separated by commas"}
        at = to_checksum_address(pool)
        with index.lock:
            known = at in index.pools
            chain_id = index.chain_id
        if not known:
            return 404, {"status": "not_found", "code": "unknown_pool", "chain_id": chain_id,
                         "detail": "this index has not seen the pool made by its factory"}
        reviews, missing, unreadable = {}, [], {}
        for n in numbers:
            got = self._read(at, n)
            if got is None:
                missing.append(n)
            elif isinstance(got, str):
                unreadable[str(n)] = got
            else:
                reviews[str(n)] = got
        return 200, {"status": "ok", "chain_id": chain_id, "pool": at, "reviews": reviews, "missing": missing,
                     "unreadable": unreadable}

    def _read(self, pool: str, n: int):
        """The document of payout `n`, None for no file, or why it cannot be handed over."""
        for name in (f"{pool}-{n}.json", f"{pool.lower()}-{n}.json"):
            path = self.directory / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size > REVIEW_MAX_BYTES:
                    return "the review file is larger than a review"
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                return "the review file could not be read as JSON"
            if not isinstance(doc, dict):
                return "the review file is not a JSON object"
            return doc
        return None


# Payout numbers: whole, at most 20 digits each, at most REVIEWS_MAX_IDS of them; a review is a few kilobytes.
REVIEWS_MAX_IDS = 100
IDS = re.compile(r"\d{1,20}(,\d{1,20}){0,99}\Z")
REVIEW_MAX_BYTES = 512 * 1024


def make_handler(index: SimIndex, allow_origin: str = "", request_timeout: float = REQUEST_TIMEOUT_S,
                 reviews: Reviews | None = None):
    reviews = reviews or Reviews(None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "colosseum-pools-sim-index"
        timeout = request_timeout

        def _send(self, status: int, payload: dict, cache: str = "public, max-age=5") -> None:
            data = json.dumps(payload, default=str).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                # What it answers changes with each window read: a CDN keeps it a few seconds at most.
                self.send_header("Cache-Control", cache)
                if allow_origin:
                    self.send_header("Access-Control-Allow-Origin", allow_origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_GET(self):  # noqa: N802
            url = urlsplit(self.path)
            if url.path == "/v1/simindex/health":
                self._send(200, index.health())
                return
            if url.path == "/v1/simindex/pool":
                self._send(*index.pool(parse_qs(url.query).get("pool", [""])[0]))
                return
            if url.path == "/v1/simindex/reviews":
                q = parse_qs(url.query)
                # A review's age is what an approval rests on: no cache keeps an old one.
                self._send(*reviews.answer(index, q.get("pool", [""])[0], q.get("ids", [""])[0]), cache="no-store")
                return
            self._send(404, {"status": "not_found"})

        def _refuse(self):
            # Nothing is written here: every method but GET is refused, without reading a body.
            self.close_connection = True
            self._send(405, {"status": "refused", "code": "read_only", "detail": "this index only answers GET"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815

        def do_OPTIONS(self):  # noqa: N802  (a simple GET needs no preflight; one that comes gets the same headers)
            self.send_response(204)
            if allow_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    return Handler


def settings(env, root: pathlib.Path) -> dict:
    """The factory and its first block, from SIM_INDEX_RECORD (a deployment record, deployments/*.json:
    `SimPoolFactory` and `block`) or from SIM_INDEX_FACTORY and SIM_INDEX_FROM_BLOCK; and the rest."""
    record = env.get("SIM_INDEX_RECORD", "")
    if record:
        path = pathlib.Path(record)
        if not path.is_absolute():
            path = root / path
        data = json.loads(path.read_text(encoding="utf-8"))
        factory, from_block, chain = data.get("SimPoolFactory"), data.get("block"), data.get("chain_id")
        if chain is not None and int(chain) != NETWORK.chain_id:
            raise SystemExit(f"refusing to start: {record} is of chain {chain}, and the network is {NETWORK.name}")
    else:
        factory, from_block = env.get("SIM_INDEX_FACTORY"), env.get("SIM_INDEX_FROM_BLOCK")
    if not (isinstance(factory, str) and ADDRESS.match(factory)):
        raise SystemExit("refusing to start: no SimPoolFactory; set SIM_INDEX_RECORD, or SIM_INDEX_FACTORY "
                         "and SIM_INDEX_FROM_BLOCK")
    try:
        from_block = int(from_block)
    except (TypeError, ValueError):
        raise SystemExit("refusing to start: the factory's first block (record `block`, or SIM_INDEX_FROM_BLOCK) "
                         "is not a number") from None
    window = int(env.get("SIM_INDEX_WINDOW", DEFAULT_WINDOW))
    lag = int(env.get("SIM_INDEX_LAG", DEFAULT_LAG))
    pause = float(env.get("SIM_INDEX_PAUSE", DEFAULT_PAUSE_S))
    every = float(env.get("SIM_INDEX_EVERY", DEFAULT_EVERY_S))
    if from_block < 0 or window < 1 or lag < 0 or pause < 0 or not every > 0:
        raise SystemExit("refusing to start: SIM_INDEX_FROM_BLOCK, SIM_INDEX_LAG and SIM_INDEX_PAUSE must not be "
                         "negative, SIM_INDEX_EVERY must be more than 0 and SIM_INDEX_WINDOW is at least 1")
    factory = to_checksum_address(factory)
    directory = pathlib.Path(env.get("SIM_INDEX_DIR", DEFAULT_DIR))
    return {"factory": factory, "from_block": from_block, "window": window, "lag": lag, "pause_s": pause, "every_s": every,
            "path": directory / f"sim-index-{NETWORK.chain_id}-{factory.lower()}.json",
            "rpc": env.get("SIM_INDEX_RPC_URL") or NETWORK.rpc_url,
            "bind": env.get("SIM_INDEX_BIND", DEFAULT_BIND), "allow_origin": env.get("SIM_INDEX_ALLOW_ORIGIN", ""),
            # The review service's folder (ops/host/colosseum-payout-review@.service --out); unset: no reviews route.
            "reviews": pathlib.Path(env["SIM_INDEX_REVIEWS"]) if env.get("SIM_INDEX_REVIEWS") else None}


def main() -> int:
    from .server import BoundedServer  # the gateway's server: a bounded number of connections, a 503 past it

    cfg = settings(os.environ, pathlib.Path(__file__).resolve().parents[1])
    rpc = Rpc(cfg["rpc"])
    chain = rpc.chain_id()
    if chain != NETWORK.chain_id:
        raise SystemExit(f"refusing to start: RPC chain id is {chain}, expected {NETWORK.chain_id}")
    index = SimIndex(rpc, chain, cfg["factory"], cfg["from_block"], cfg["path"], window=cfg["window"],
                     lag=cfg["lag"], pause_s=cfg["pause_s"], every_s=cfg["every_s"])
    took = index.load()
    log_line(event="start", factory=cfg["factory"], from_block=cfg["from_block"], file=str(cfg["path"]), file_was=took,
             scanned_to=index.scanned_to, pools=len(index.pools))
    host, _, port = cfg["bind"].partition(":")
    server = BoundedServer((host, int(port)), make_handler(index, cfg["allow_origin"],
                                                                  reviews=Reviews(cfg["reviews"])))
    threading.Thread(target=index.run, name="sim-index-read", daemon=True).start()
    print(f"sim index listening on {host}:{port}, factory {cfg['factory']} from block {cfg['from_block']}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
