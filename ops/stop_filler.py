"""The stops and takes of the paper exams, looked at in every block with nothing in the way, and the watch that says at
once when one was reached and not carried out (docs/SIM-POOL.md, "The stops").

    spike/.venv/bin/python -m ops.sim_keeper --deployment sim1                     # the keeper with this loop inside it
    spike/.venv/bin/python -m ops.stop_filler --deployment sim1                     # the watch alone: no key, sends nothing

On by default, on testnet and mainnet alike: the keeper's `--stops loop` (COLOSSEUM_STOPS). `--stops inline`
(COLOSSEUM_STOPS=inline) is the way back to the stops tried in the keeper's one loop, with no thread and no journal: the
payout review then holds every payout with a stop, since none was checked in every block.

A stop exists for the chain only in a block whose transaction carries it out: `fillTrigger` takes it while the oracle
price of that very block is at the stop or past it, and inside its window only from the factory's filler. A reach that
no transaction of the filler's lands in leaves no trace. So the loop that carries stops out must look at every block and
must not wait for anything else. In the keeper's one loop it did, in turn: a slow pass every 30 seconds with no look at
the stops while it ran; each fill and each stop waiting for its own receipt, the others queued behind it; a node's rate
limit holding the whole process for up to 63 seconds; and a stop set while the keeper was behind its logs unknown until
the slow pass. This loop runs in a thread of its own, beside the keeper's:

  every block      → one `eth_getLogs` of the blocks since its last look (the stops and takes set, cleared and filled,
                     the fills that move a position, the exams sold and ended), one read of the oracle price for each
                     asset with a stop or a take standing, and the stops whose price has reached them compared here, not
                     tried one by one with an `eth_call`;
  a stop reached   → `fillTrigger` signed and sent at once, with a fixed gas, no reads before it and no wait for its
                     receipt: the receipt is looked at in the next blocks. A stop the exam refused once is tried with an
                     `eth_call` first the next time (the book past the ceiling, say), as before;
  one sender       → the filler's one key sends from two threads, and a nonce taken by one and not yet handed to the node
                     would hold every later transaction of the other. So every transaction of the key, the keeper's too,
                     is numbered and handed to the node by this thread alone (`Sender`), one call each, never retried
                     behind a ladder. The keeper's thread puts its transaction in a queue, this thread sends it between
                     two looks at the head, and the keeper waits for its receipt in its own thread. A nonce is spent only
                     once the node took the transaction; one whose answer was lost is sent again with the same bytes,
                     never with other ones under the same nonce;
  its own node     → `--stop-rpc` (COLOSSEUM_STOP_RPC_URL), the keeper's node when not set. Every call is tried once:
                     a busy node costs the loop that look, not a minute; the blocks it missed are read back the next
                     time, their events always (`CATCHUP` blocks at most) and their prices for the last `BACKFILL`;
  a tip            → `--stop-tip-gwei` (COLOSSEUM_STOP_TIP_GWEI), zero when not set, as every send of the keeper's.

The watch runs on the same prices. For each stop or take standing, armed and with a position open, a stretch of blocks
in which the oracle stood at it or past it is a reach. A reach that ends, the price back, the stop taken off or moved by
the trader, the position closed or the exam over, with no `TriggerFilled` inside it, is an alarm (`stop_missed`); a
reach that has lasted `--stop-overdue` blocks and is not filled yet is one too (`stop_overdue`). Two are not alarms, and
are said as lines: a reach of one block that ended by the price (no transaction sent on seeing it can land inside it,
`--stop-alarm-min-blocks`), and a reach after the exam's deadline or idle limit, which no `fillTrigger` can carry out
(`TooLate`) and which the keeper's `expire` ends. An alarm is written as the host's watchdogs write theirs
(ops/host/watchdogs/watchdog-run.sh): an `ALARM` line in the journal, the line added to a flag file (`--stop-alarm`),
and the line given to COLOSSEUM_ALERT_CMD when the host has a channel. A miss does not mend itself, so its flag stays
until a person removes it. So does a loop that cannot see: a thread that ended or stopped moving, calls failing for
`STALL_S` seconds, prices unread for `BLIND_ALARM_BLOCKS` blocks, or a gap past `CATCHUP` are alarms too.

Every block it looks at goes to a journal (`--price-journal`, prices-<deployment>.jsonl beside the state), one JSON
line a block: its number, its time, this loop's clock, and the oracle price of each asset with a stop or a take
standing (`PriceJournal`); the blocks it did not look at are written as gaps with why (not running, the head ahead of
its read-back, behind past `CATCHUP`). It survives a restart (a last line cut short is cut off) and has one writer.
It is the record `ops/payout_review.py --prices` judges every block of each stop's time by, and the keeper's own:
the review believes it only where the chain's own prices and a calibrated node bear it out.

Nothing is sent on a timer: the only transaction of this loop is a stop's `fillTrigger`, in the block that saw it
reached. One that has not landed `BUMP_AFTER_S` later is signed again under its nonce with fees a quarter higher (only
one of them can land), up to `MAX_BUMPS` times. Alarms besides the misses: the node's head older than `STALE_HEAD_S`
by this machine's clock (`stop_node_stale`), the loop short of the head for `BEHIND_S` (`stop_loop_behind`), the key
able to pay for fewer than `MIN_RUNWAY_FILLS` stops (`stop_key_low`), and the journal not written
(`stop_journal_failed`). No key and no node address goes to a line or an alarm (`scrub`).

What this does not do: carry out a stop whose reach is shorter than the way from a block seen to a transaction landed
(about one or two blocks); carry out a stop the exam refuses (its book too far from the oracle); stop a trader taking a
stop off while it is reached; or go on while the filler's one key and
its one machine are down. A stop also waits behind any earlier transaction of the same key in the node. Those need the
contract (a key of its own for the stops, or anyone allowed to carry a stop out) or a second machine (the design note of
20 November 2026).
"""

from __future__ import annotations

import argparse
import bisect
import fcntl
import json
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from ops import keeper as core
from ops import sim_keeper as sk
from spike.hlspike import common as c

log = core.log

# PaperExam's events this loop reads besides the keeper's; the tests hold each against the Solidity source.
STOPPED = "0x" + keccak(text="Stopped(uint8,uint8,int256)").hex()
PASSED = "0x" + keccak(text="Passed(address,int256)").hex()
ACCOUNT_TOPICS = [sk.TRIGGER_SET, sk.TRIGGER_CLEARED, sk.TRIGGER_FILLED, sk.FILLED, STOPPED, PASSED]
POOL_TOPICS = [sk.EXAM_SOLD, sk.FUNDED]
ORACLE_PX = c.PRECOMPILE["oraclePx"]
HIP3_BASE = 100_000  # CoreOps.HIP3_BASE: the oracle's index of an asset is pxIndex(asset)
ACTIVE = 1  # PaperExam.Status.Active
RESULT = ["uint8", "address", "uint64", "uint64"]
STOP_GAS = sk.FILL_GAS
# Blocks back whose prices the loop reads when the head ran ahead of it. Each costs a price read a standing asset.
BACKFILL = 20
# Blocks back whose events the loop reads in any case: past these it has lost the thread, says so and starts afresh.
CATCHUP = 1_000
# Blocks a reach may last unfilled before it is an alarm while it still lasts.
OVERDUE = 5
# A reach ended by the price shorter than this is a line, not an alarm: a transaction sent on seeing it lands after it.
MIN_ALARM_BLOCKS = 2
# Seconds a transaction of the key may go without a receipt before its same bytes are sent again.
RESEND_AFTER_S = 10.0
# Seconds the keeper's thread waits for this thread to take its transaction.
REQUEST_WAIT_S = 60.0
# Seconds of failing calls, or of no step at all, before the loop is an alarm.
STALL_S = 30.0
# A loop failing again and again raises its alarm at most once in this many seconds; the episodes between are lines.
FAILING_REALARM_S = 3_600.0
# Seconds the oldest transaction of the key may wait for its block before it is an alarm (the gas price left it behind,
# say): every later one of the key waits behind it.
STUCK_S = 120.0
# Blocks a stop whose position could not be read is kept, price by price, to be judged once the position is read.
UNJUDGED_MAX = 200
BLIND_ALARM_BLOCKS = 30
ALIVE_EVERY_S = 600.0
GWEI = 10 ** 9
# A stop's transaction with no receipt this long is signed again under its nonce with fees a quarter higher (a
# replacement: only one of them can land), up to MAX_BUMPS times; a transaction of the keeper's waits RESEND_AFTER_S.
BUMP_AFTER_S = 2.0
MAX_BUMPS = 4
# Seconds the node's head block may be older than this machine's clock before it is an alarm: a node behind or stuck.
STALE_HEAD_S = 60.0
# Seconds the loop may stay short of the node's head before it is an alarm.
BEHIND_S = 30.0
# How often the key's balance is read, and the stops' fills it must still pay for (at twice the gas price, the most a
# stop's transaction may pay) before it is an alarm.
BALANCE_EVERY_S = 300.0
MIN_RUNWAY_FILLS = 200


def px_index(asset: int) -> int:
    """`CoreOps.pxIndex`: the oracle's index of an exam's asset."""
    return asset - HIP3_BASE if asset >= HIP3_BASE else asset


def hit(take: bool, sign: int, oracle: int, px: int) -> bool:
    """Whether the oracle price has reached a stop or a take of a position of `sign` (`PaperExam._triggered`): a take on
    a long and a stop on a short at the price or above it, the other two at it or below. No position, no reach."""
    if sign == 0 or oracle <= 0:
        return False
    return oracle >= px if take == (sign > 0) else oracle <= px


def sign_of(position: int) -> int:
    return (position > 0) - (position < 0)


def host_of(url: str) -> str:
    """A node's address as a log may show it: the scheme and the host, never the path or the query a key rides in."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}" + (f":{parts.port}" if parts.port else "")


def scrub(text: str) -> str:
    """`text` with every address in it cut to its scheme and host, and a bare request path (urllib3's "url: /v2/…")
    taken out: an error of the keeper's node names its URL, and a key may ride in its path. An address that does not
    parse (a port that is no number) is taken out whole."""
    def host(url: str) -> str:
        try:
            return host_of(url)
        except ValueError:
            return "<node>"
    text = re.sub(r"[a-z][a-z0-9+.-]*://[^\s'\")]+", lambda m: host(m.group(0)), text)
    return re.sub(r"(url: ?)/[^\s'\"<>()]*", r"\1<path>", text)


# ── a node, tried once ───────────────────────────────────────────────────────────────

class Unsure(RuntimeError):
    """The call may have reached the node or not: a timeout, a connection broken, a server error. For a read it is a
    failure like any; for a transaction it means the transaction may be in."""


class Unreached(RuntimeError):
    """The connection was never made (a connect timeout): nothing reached the node, a transaction was not sent."""


class Busy(c.NotSent):
    """The node did not take a transaction for a reason that passes: busy, or not reached. A transaction of the keeper's
    is tried again in a moment; a stop goes with the next block."""


class Node:
    """One JSON-RPC node, its own HTTP session (the keeper's thread has the other), every call tried once: HTTP 429 and
    the public node's -32005 raise c.NodeBusy, an answer that may or may not have been acted on raises Unsure, an error
    in the answer RuntimeError. No error text carries the node's address: a key may ride in its path."""

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": c.USER_AGENT, "Content-Type": "application/json"})

    def call(self, method: str, params=()):
        try:
            resp = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
                                     timeout=self.timeout)
        except requests.ConnectTimeout:
            raise Unreached(f"{method}: ConnectTimeout") from None
        except requests.RequestException as exc:
            raise Unsure(f"{method}: {type(exc).__name__}") from None
        if resp.status_code == 429:
            raise c.NodeBusy(f"{method}: HTTP 429")
        if resp.status_code >= 500:
            raise Unsure(f"{method}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"{method}: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise Unsure(f"{method}: an answer that is not JSON") from None
        error = body.get("error")
        if error is None:
            return body["result"]
        if isinstance(error, dict) and error.get("code") == c.RATE_LIMITED:
            raise c.NodeBusy(f"{method}: rate limited")
        raise RuntimeError(f"{method}: {error}")


# ── the key's one sender ─────────────────────────────────────────────────────────────

class Sender:
    """Every transaction of the filler's key, numbered and handed to the node by the stops' thread alone.

    A nonce is spent only once the node has taken the transaction (an answer, or one lost on the way: `Unsure`). A node
    that refused it (busy, an error) leaves the nonce for the next transaction, so no other thread is ever left behind a
    gap. What was spent is kept, with its bytes, until its receipt: one with no receipt after `RESEND_AFTER_S` is sent
    again as it was, and one whose nonce the chain has counted past without it is said and let go. The count is read
    from the node once at the start and again only when the node says a nonce is taken by something else, and the
    next nonce never goes below one handed out. The keeper's thread hands its transactions over through `submit`."""

    def __init__(self, wallet, node: Node):
        self.wallet = wallet
        self.node = node
        self.address = to_checksum_address(wallet.address)
        self.next: int | None = None
        self.lock = threading.Lock()  # the ledger and the requests' states, between the two threads
        self.ledger: dict[int, dict] = {}  # nonce -> {"hash", "raw", "at", "since", "past", "said"}
        self.gone: set[int] = set()
        self.requests: queue.Queue = queue.Queue()
        self.later: list[dict] = []  # the keeper's transactions the node was busy for, each with its `not_before`
        self.alarm = lambda event, **fields: log(event, alarm="ALARM", **fields)  # the loop's, once it has one

    def _count(self, tag: str) -> int:
        return int(self.node.call("eth_getTransactionCount", [self.address, tag]), 16)

    def send(self, to: str, data: bytes, gas: int, gas_price: int, tip: int = 0, bump: bool = False) -> tuple[str, int]:
        """The stops' thread only. Signs and hands one transaction to the node; its hash and nonce. c.NotSent when the
        node did not take it: its nonce is not spent. `bump`: a stop's, signed again with higher fees when it does not
        land at once (`sweep`)."""
        try:
            if self.next is None:
                self.next = self._count("pending")
        except Exception as exc:
            raise c.NotSent(str(exc)) from None
        for attempt in (0, 1):
            nonce = self.next
            tx = {"chainId": c.CHAIN_ID, "nonce": nonce, "value": 0, "data": data, "type": 2, "to": to_checksum_address(to),
                  "maxPriorityFeePerGas": tip, "maxFeePerGas": gas_price * 2 + tip, "gas": gas}
            try:
                signed = self.wallet.sign_transaction(tx)
            except Exception as exc:
                raise c.NotSent(str(exc)) from None
            raw = "0x" + signed.raw_transaction.hex().removeprefix("0x")
            tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
            try:
                self.node.call("eth_sendRawTransaction", [raw])
            except Unsure as exc:
                log("send_unsure", nonce=nonce, tx=tx_hash, error=str(exc)[:120], then="sent again as it is if no receipt")
            except (c.NodeBusy, Unreached) as exc:
                raise Busy(str(exc)) from None
            except RuntimeError as exc:
                message = str(exc)
                if "already known" not in message:
                    if "nonce too low" in message.lower() and attempt == 0:
                        # Taken by something else (a key used elsewhere): never by a transaction of ours, whose nonces
                        # are kept. Up to the chain's count, never below what was handed out; signed again, once.
                        try:
                            counted = self._count("latest")
                        except Exception as exc2:
                            raise c.NotSent(str(exc2)) from None
                        self.next = max(self.next + 1, counted)
                        log("nonce_taken_elsewhere", nonce=nonce, next=self.next)
                        continue
                    raise c.NotSent(message) from None
            now = time.monotonic()
            with self.lock:
                self.ledger[nonce] = {"hash": tx_hash, "raw": raw, "at": now, "since": now, "past": 0, "said": False,
                                      "max_fee": gas_price * 2 + tip, "hashes": [tx_hash], "bump": bump, "bumps": 0,
                                      "tx": tx}
            self.next = nonce + 1
            return tx_hash, nonce
        raise c.NotSent("the nonce was taken twice")

    def confirm(self, nonce: int) -> None:
        """Its receipt is in: nothing more to keep."""
        with self.lock:
            self.ledger.pop(nonce, None)

    def receipt(self, nonce: int, tx_hash: str):
        """The receipt of the transaction under `nonce`, whichever of its signings landed; None while none has."""
        with self.lock:
            hashes = list((self.ledger.get(nonce) or {}).get("hashes") or [tx_hash])
        for h in reversed(hashes):
            rcpt = self.node.call("eth_getTransactionReceipt", [h])
            if rcpt:
                return rcpt
        return None

    def _bumped(self, nonce: int, e: dict, now: float) -> bool:
        """A stop's transaction that has not landed, signed again under its nonce with fees a quarter higher and sent:
        whether it was. Only one of its signings can land. Past MAX_BUMPS it is sent again as it is."""
        if not e.get("bump") or e["bumps"] >= MAX_BUMPS:
            return False
        tx = dict(e["tx"])
        tx["maxPriorityFeePerGas"] = tx["maxPriorityFeePerGas"] * 5 // 4 + 1
        tx["maxFeePerGas"] = tx["maxFeePerGas"] * 5 // 4 + 1
        try:
            signed = self.wallet.sign_transaction(tx)
        except Exception:
            return False
        raw = "0x" + signed.raw_transaction.hex().removeprefix("0x")
        tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
        try:
            self.node.call("eth_sendRawTransaction", [raw])
        except Unsure:
            pass  # may be in: kept as one of its signings
        except Exception as exc:
            log("send_bump_failed", nonce=nonce, tx=tx_hash, error=scrub(str(exc))[:120])
            return False
        with self.lock:
            if nonce in self.ledger:
                self.ledger[nonce].update(hash=tx_hash, raw=raw, at=now, tx=tx, bumps=e["bumps"] + 1,
                                          max_fee=tx["maxFeePerGas"], hashes=e["hashes"] + [tx_hash])
        log("send_bumped", nonce=nonce, tx=tx_hash, max_fee=tx["maxFeePerGas"], tip=tx["maxPriorityFeePerGas"],
            bumps=e["bumps"] + 1)
        return True

    def is_gone(self, nonce: int) -> bool:
        with self.lock:
            return nonce in self.gone

    def sweep(self, now: float | None = None) -> None:
        """The stops' thread: a transaction of the key with no receipt after `RESEND_AFTER_S` is sent again with the same
        bytes (the same hash: nothing new can land); one whose nonce the chain counted past without it, at two sweeps
        running (a receipt the node had not indexed yet is found at the second), is let go. The oldest one still waiting
        after `STUCK_S` is an alarm, once: every later transaction of the key waits behind it."""
        now = time.monotonic() if now is None else now
        with self.lock:
            old = sorted((n, dict(e)) for n, e in self.ledger.items()
                         if now - e["at"] >= (BUMP_AFTER_S if e.get("bump") and e["bumps"] < MAX_BUMPS else RESEND_AFTER_S))
            oldest = min(self.ledger, default=None)  # of the whole ledger: one just sent again is still the oldest
        if not old:
            return
        counted = self._count("latest")
        for nonce, e in old:
            if self.receipt(nonce, e["hash"]):
                self.confirm(nonce)
                continue
            if counted > nonce:
                with self.lock:
                    if nonce in self.ledger:
                        self.ledger[nonce]["past"] += 1
                        self.ledger[nonce]["at"] = now
                    gone = e["past"] + 1 >= 2
                    if gone:
                        self.ledger.pop(nonce, None)
                        self.gone.add(nonce)
                if gone:
                    log("send_gone", nonce=nonce, tx=e["hash"], why="the chain counted past its nonce without it, twice")
                continue
            with self.lock:  # two sweeps running, not two in all: a count that fell back starts it again
                if nonce in self.ledger:
                    self.ledger[nonce]["past"] = 0
            if nonce == oldest and now - e["since"] >= STUCK_S and not e["said"]:
                with self.lock:
                    if nonce in self.ledger:
                        self.ledger[nonce]["said"] = True
                self.alarm("send_stuck", nonce=nonce, tx=e["hash"], seconds=int(now - e["since"]), max_fee=e["max_fee"],
                           why="the key's oldest transaction is not in a block; every later one waits behind it")
            if self._bumped(nonce, e, now):
                continue
            try:
                self.node.call("eth_sendRawTransaction", [e["raw"]])
                log("send_again", nonce=nonce, tx=e["hash"])
            except Exception as exc:
                if "already known" not in str(exc):
                    log("send_again_failed", nonce=nonce, tx=e["hash"], error=str(exc)[:120])
            with self.lock:
                if nonce in self.ledger:
                    self.ledger[nonce]["at"] = now

    # ── the keeper's thread ──────────────────────────────────────────────────────────

    def submit(self, to: str, data: bytes, gas: int, gas_price: int, wait: float = REQUEST_WAIT_S) -> tuple[str, int]:
        """The keeper's thread: its transaction to the stops' thread, which sends it at its next look; its hash and nonce.
        Not taken in `wait` seconds: c.NotSent, and it will not be sent."""
        req = {"to": to, "data": data, "gas": gas, "gas_price": gas_price, "state": "queued", "done": threading.Event(),
               "result": None, "error": None, "not_before": 0.0, "delay": 1.0, "until": time.monotonic() + wait}
        self.requests.put(req)
        if not req["done"].wait(wait):
            with self.lock:
                if req["state"] == "queued":
                    req["state"] = "cancelled"
                    if req in self.later:
                        self.later.remove(req)
                    last = f" (last: {req['error']})" if req["error"] is not None else ""
                    raise c.NotSent(f"the transaction was not sent in {wait:.0f} seconds; it will not be{last}")
            req["done"].wait()  # taken: its one call is under way
        if req["error"] is not None:
            raise req["error"]
        return req["result"]

    def handle(self, req: dict) -> None:
        """One of the keeper's transactions sent. The node busy or not reached: tried again after 1, 2, 4, 8 and 16 seconds
        as long as the keeper waits for it (`submit`), as c.rpc's ladder did, but with no thread held meanwhile."""
        with self.lock:
            if req["state"] != "queued":
                return
            if req["not_before"] > time.monotonic():
                self.later.append(req)
                return
            req["state"] = "taken"
        try:
            req["result"] = self.send(req["to"], req["data"], req["gas"], req["gas_price"])
        except Busy as exc:
            with self.lock:
                # Tried again only inside the keeper's wait: one refused after it is not sent, never sent late.
                if req["state"] == "taken" and time.monotonic() < req.get("until", float("inf")):
                    req["state"] = "queued"
                    req["not_before"] = time.monotonic() + req["delay"]
                    req["delay"] = min(req["delay"] * 2, 16.0)
                    req["error"] = exc
                    self.later.append(req)
                    return
            req["error"] = exc
        except c.NotSent as exc:
            req["error"] = exc
        except Exception as exc:
            req["error"] = c.NotSent(str(exc))
        req["error"] = None if req["result"] is not None else req["error"]
        req["done"].set()

    def serve(self) -> None:
        """The keeper's transactions due now: the ones waiting for a busy node whose time has come, and the new ones."""
        now = time.monotonic()
        with self.lock:
            due = [r for r in self.later if r["not_before"] <= now]
            self.later = [r for r in self.later if r["not_before"] > now]
        for req in due:
            self.handle(req)
        while True:
            try:
                req = self.requests.get_nowait()
            except queue.Empty:
                return
            self.handle(req)

    def wait_for(self, left: float) -> float:
        """How long to wait for a new transaction of the keeper's before one waiting for a busy node is due."""
        with self.lock:
            soonest = min((r["not_before"] for r in self.later), default=None)
        return left if soonest is None else max(0.0, min(left, soonest - time.monotonic()))

    def send_and_wait(self, to: str, data: bytes, gas: int | None = None) -> dict:
        """The keeper's own sends (a fill, a slow call): the chain checked, the gas estimated with a quarter more when not
        given, handed to the stops' thread, and the receipt waited for here, as c.send_tx does. A reverted one raises
        c.Reverted; one with no receipt in time stays with the sender, which sends its same bytes again."""
        try:
            c.assert_network()
            if gas is None:
                probe = {"from": self.address, "to": to_checksum_address(to), "data": "0x" + data.hex()}
                gas = int(int(c.rpc("eth_estimateGas", [probe]), 16) * 1.25)
            gas_price = int(c.rpc("eth_gasPrice"), 16)
        except Exception as exc:  # a chain that is not the network's stops the program (SystemExit), as c.send_tx's
            raise c.NotSent(str(exc)) from None
        tx_hash, nonce = self.submit(to, data, gas, gas_price)
        rcpt = c.wait_receipt(tx_hash)
        self.confirm(nonce)
        if int(rcpt["status"], 16) != 1:
            raise c.Reverted(f"transaction {tx_hash} reverted")
        return rcpt


# ── the journal of prices ────────────────────────────────────────────────────────────

class PriceJournal:
    """The oracle prices this loop read, one JSON line a block it looked at, in block order: the file
    `ops/payout_review.py --prices` judges every block of each stop's time by.

        {"b": block, "t": the block's time (null where not read), "w": this loop's clock, "px": {asset: price}}
        {"b": ..., "px": {...}, "unread": [asset]}      an asset whose price could not be read in that block
        {"gap": [from, to], "why": ...}                 blocks the loop did not look at, and why

    Every block it judged has its line, with every asset that had a stop or a take standing then (none standing: an
    empty "px"), so a block missing is a gap of its own; the blocks it was not running in are written as a gap when it
    starts again, from the last line it wrote. Lines are appended and flushed one by one; a last line cut short (the
    machine stopped mid write) is cut off when the journal is opened, and nothing before it. One process writes it: a
    second one is refused at the start. It is the keeper's own record: the review holds it against the chain's own
    prices and a node before it believes it."""

    def __init__(self, path: pathlib.Path, clock=time.time):
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        self._held = open(path, "rb")
        try:
            fcntl.flock(self._held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._held.close()
            raise SystemExit(f"{path} is written by another process: give this one its own --price-journal") from None
        self.broken = False
        self.last = self._mend()

    def _mend(self) -> int | None:
        """The file cut back to its last whole line; the last block it holds a line or a gap for, None for none."""
        with open(self.path, "r+b") as fh:
            size = fh.seek(0, os.SEEK_END)
            back = min(size, 1 << 16)
            fh.seek(size - back)
            tail = fh.read(back)
            if tail and not tail.endswith(b"\n"):
                cut = tail.rfind(b"\n")
                fh.truncate(size - back + cut + 1 if cut >= 0 else 0)
                tail = tail[:cut + 1] if cut >= 0 else b""
        for line in reversed(tail.splitlines()):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "b" in row:
                return int(row["b"])
            if isinstance(row, dict) and "gap" in row:
                return int(row["gap"][1])
        return None

    def close(self) -> None:
        self._held.close()

    def _write(self, row: dict) -> None:
        """One line, appended. A write that failed (a full disk) may have left half a line: cut off before the next."""
        if self.broken:
            self._mend()
            self.broken = False
        try:
            with open(self.path, "ab") as fh:
                fh.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
                fh.flush()
        except OSError:
            self.broken = True
            raise

    def add(self, block: int, prices: dict[int, int], t: int | None = None, unread=()) -> None:
        """Block `block` looked at: its prices, its time where known, the assets not read in it."""
        row: dict = {"b": block, "t": t, "w": int(self.clock()), "px": {str(a): int(px) for a, px in sorted(prices.items())}}
        if unread:
            row["unread"] = sorted(unread)
        self._write(row)
        self.last = block

    def gap(self, lo: int, hi: int, why: str) -> None:
        """Blocks `lo` to `hi` not looked at: a gap, with why."""
        if hi < lo:
            return
        self._write({"gap": [lo, hi], "why": why, "w": int(self.clock())})
        self.last = max(self.last or hi, hi)


# ── the alarm ────────────────────────────────────────────────────────────────────────

class Alarm:
    """What the host's watchdogs do with a finding (ops/host/watchdogs/watchdog-run.sh), done from a running loop: the
    line in the journal marked ALARM, added to a flag file, and handed to COLOSSEUM_ALERT_CMD when there is one. The
    channel runs in a thread of its own, so a slow one does not stop the loop. No other channel is opened here."""

    def __init__(self, flag: pathlib.Path | None, command: str | None = None):
        self.flag = flag
        self.command = os.environ.get("COLOSSEUM_ALERT_CMD", "") if command is None else command
        self.said_no_channel = False
        self.lock = threading.Lock()  # both threads may raise one

    def __call__(self, event: str, **fields) -> None:
        line = json.dumps({"t": int(time.time()), "event": event, **fields}, default=str)
        log(event, alarm="ALARM", **fields)
        where = "the journal"
        with self.lock:
            if self.flag is not None:
                try:
                    self.flag.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.flag, "a", encoding="utf-8") as fh:
                        fh.write(f"at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n{line}\n")
                    where = "the flag and the journal"
                except OSError as exc:
                    log("stop_alarm_flag_failed", flag=str(self.flag), error=str(exc)[:200])
            no_channel = not self.command and not self.said_no_channel
            if no_channel:
                self.said_no_channel = True
        if self.command:
            threading.Thread(target=self._send, args=(line, where), daemon=True).start()
        elif no_channel:
            log("stop_alarm_no_channel", why="COLOSSEUM_ALERT_CMD is not set", where=where)

    def _send(self, line: str, where: str) -> None:
        try:
            subprocess.run(["sh", "-c", self.command], input=line + "\n", text=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            log("stop_alarm_sent")
        except Exception as exc:
            log("stop_alarm_not_sent", error=str(exc)[:200], where=where)


# ── the loop ─────────────────────────────────────────────────────────────────────────

class StopLoop:
    """The stops and takes of the keeper's exams, every block (see the module's text). `keeper` is the SimKeeper whose
    pools and accounts are watched; `wallet` and `sender` are None for a watch that sends nothing."""

    def __init__(self, keeper, node: Node, *, wallet=None, sender: Sender | None = None, tip_wei: int = 0,
                 journal: PriceJournal | None = None, alarm: Alarm | None = None, backfill: int = BACKFILL,
                 overdue: int = OVERDUE, min_alarm_blocks: int = MIN_ALARM_BLOCKS, catchup: int = CATCHUP,
                 clock=time.time, min_runway: int = MIN_RUNWAY_FILLS):
        self.keeper = keeper
        self.clock = clock  # the wall clock the head's time and the loop's lag are held against
        self.min_runway = min_runway
        self.head_t: dict[int, int] = {}  # a head block -> its time, as the node gave it
        self.stale_said = False
        self.behind_said = False
        self.caught_up_at: float | None = None
        self.balance_at = -BALANCE_EVERY_S
        self.low_said = False
        self.journal_said = False
        self.node = node
        self.wallet = wallet
        self.sender = sender
        self.tip = tip_wei
        self.journal = journal
        self.alarm = alarm or Alarm(None, "")
        self.backfill = max(1, backfill)
        self.catchup = max(self.backfill, catchup)
        self.overdue = max(1, overdue)
        self.min_alarm_blocks = max(1, min_alarm_blocks)
        self.block = 0  # the last block whose events (and prices, where read) are in
        self.triggers: dict[tuple[str, int, bool], dict] = {}  # key -> {"px", "armed", "touch", "said", "first"}
        self.signs: dict[tuple[str, int], int | None] = {}  # (exam, asset) -> the position's side; None: not read
        self.event_at: dict[tuple, int] = {}  # a trigger's or a position's last block of an event or a read
        self.extra: set[str] = set()  # accounts sold since the keeper's last look
        self.ended: set[str] = set()
        self.pending: dict[tuple[str, int, bool], tuple[str, int, int]] = {}  # key -> (tx, nonce, block sent at)
        self.probe: set[tuple[str, int, bool]] = set()  # refused once: an eth_call before the next send
        self.blind_from: int | None = None
        self.blind_said = False
        self.unread_signs: set[tuple[str, int]] = set()
        self.inbox: queue.Queue = queue.Queue()
        self.gas_price = 0
        self.gas_read_at = -10**9
        self.quiet_until: dict[str, float] = {}
        self.beat = time.monotonic()  # the last time the loop heard the node's head
        self.failing_since: float | None = None
        self.failing_said = False
        self.stall_said = False
        self.failing_alarm_at = -FAILING_REALARM_S
        self.alive_at = 0.0
        self.alarms_due: list[tuple] = []  # (event, what, reason): raised once the step's stops are out
        self.moves: dict[tuple[str, int], list[int]] = {}  # (exam, asset) -> the blocks its position moved in
        if sender is not None:
            sender.alarm = self.alarm

    # ── what the keeper hands in ─────────────────────────────────────────────────────

    def watched(self) -> tuple[list[str], list[str]]:
        """The accounts and the pools whose logs are read: the keeper's, as its thread left them, and the ones sold since.
        Each is copied whole first (`list` and `set` of a dict or a set copy it in one step)."""
        done = set(self.keeper.done)
        accounts = [a for a in list(self.keeper.accounts) if a not in done]
        accounts = [a for a in dict.fromkeys(accounts + sorted(self.extra)) if a not in self.ended and a not in done]
        return accounts, list(self.keeper.pools)

    def reread(self, exam: str, call=None) -> None:
        """An exam's state as the contract holds it, read at one block, and handed to the loop: ended, or its stops and
        takes and its positions. Called by the keeper's slow pass in its thread, and by the watch alone: what was set
        while nothing read the logs (a keeper down, or behind) is known at the next pass. The loop keeps what its own
        events say after that block."""
        try:
            self._reread(exam, call or c.rpc)
        except Exception as exc:  # the keeper's node answers with errors that name its address: never in a log
            raise RuntimeError(scrub(str(exc))) from None

    def _reread(self, exam: str, call) -> None:
        at = self.block or int(call("eth_blockNumber"), 16)
        tag = hex(at)

        def read(sig, types, args, out):
            raw = call("eth_call", [{"to": exam, "data": "0x" + c.encode_call(sig, types, args).hex()}, tag])
            return abi_decode(out, bytes.fromhex(raw[2:]))

        if read("result()", [], [], RESULT)[0] != ACTIVE:
            self.inbox.put(("ended", exam, at))
            return
        found: dict[tuple[str, int, bool], tuple[int, int]] = {}
        signs: dict[int, int] = {}
        for asset in read("rules()", [], [], [sk.RULES])[0][3]:
            asset = int(asset)
            for is_take, getter in ((False, "stopOf(uint32)"), (True, "takeOf(uint32)")):
                px, _, armed, _ = read(getter, ["uint32"], [asset], [sk.TRIGGER])[0]
                if px:
                    found[(exam, asset, is_take)] = (int(px), int(armed))
            if any(k[1] == asset for k in found):
                signs[asset] = sign_of(read("positionOf(uint32)", ["uint32"], [asset], ["int256"])[0])
        self.inbox.put(("state", exam, at, found, signs))

    def apply(self, message: tuple) -> None:
        if message[0] == "ended":
            _, exam, at = message
            self._ended(exam, at, "the exam ended (read from it)")
            return
        _, exam, at, found, signs = message
        self.apply_reread(exam, at, found, signs)

    def apply_reread(self, exam: str, at: int, found: dict, signs: dict) -> None:
        if exam in self.ended:
            return
        for key in [k for k in self.triggers if k[0] == exam]:
            if key not in found and self.event_at.get(key, 0) <= at:
                t = self.triggers[key]
                if t["touch"] is not None:
                    self._missed(key, t, at, "gone from the exam while reached (read from it)")
                log("stop_dropped_by_read", exam=exam, asset=key[1], take=key[2], at=at)
                self._drop(key)
        for key, (px, armed) in found.items():
            if self.event_at.get(key, 0) > at:
                continue
            t = self.triggers.get(key)
            if t is None:
                log("stop_found_by_read", exam=exam, asset=key[1], take=key[2], px=px, armed=armed, at=at)
                self.triggers[key] = self._fresh(px, armed)
            elif (t["px"], t["armed"]) != (px, armed):
                if t["touch"] is not None:
                    self._missed(key, t, at, "moved while reached (read from the exam)")
                self.triggers[key] = self._fresh(px, armed)
        for asset, s in signs.items():
            if self.event_at.get((exam, asset), 0) <= at:
                self.signs[(exam, asset)] = s

    @staticmethod
    def _fresh(px: int, armed: int) -> dict:
        return {"px": px, "armed": armed, "touch": None, "said": False, "first": None}

    # ── a step ───────────────────────────────────────────────────────────────────────

    def step(self, head: int) -> list[tuple[str, int, bool]]:
        """Everything up to block `head`: the keeper's reads, the logs, each block's prices and its watch, the receipts,
        then the stops reached at `head` sent for the next block. The keys it sent for."""
        while True:
            try:
                self.apply(self.inbox.get_nowait())
            except queue.Empty:
                break
        if head <= self.block:
            return []
        lo = head if not self.block else self.block + 1
        if not self.block and self.journal is not None and self.journal.last is not None and self.journal.last < head - 1:
            # Started again: the blocks since the journal's last line were looked at by nobody.
            self._gap(self.journal.last + 1, head - 1, "the loop was not running")
        if head - lo + 1 > self.catchup:
            self._gap(lo, head - self.catchup, f"behind by more than {self.catchup} blocks: not read at all")
            self.alarm("stop_watch_lost", from_block=lo, to=head - self.catchup,
                       why=f"behind by more than {self.catchup} blocks: their events are not read; every reach is "
                           "forgotten and the stops are read again from the exams at the next pass")
            for t in self.triggers.values():
                t.update(touch=None, said=False, first=None)
            lo = head - self.catchup + 1
            self.block = lo - 1
        price_lo = max(lo, head - self.backfill + 1)
        if price_lo > lo:
            self._blind(lo, price_lo - 1, "the head ran ahead of the loop: the events of these blocks are read, not "
                                          "their prices")
            self._gap(lo, price_lo - 1, "the head ran ahead of the loop: their prices were not read back")
            standing = [k for k, t in self.triggers.items() if t["armed"] < price_lo and self.signs.get(k[:2]) != 0]
            gap_said = bool(standing)
            if standing:
                # A reach inside these blocks would not be seen, however short: said at once, not after 30 blocks.
                self.alarm("stop_watch_gap", from_block=lo, to=price_lo - 1, standing=len(standing),
                           why="the head ran ahead of the loop by more than its read-back of prices, with stops armed")
        accounts, pools = self.watched()
        for exam in {k[0] for k in self.triggers} & set(self.keeper.done) - self.ended:
            # Done in the keeper's thread (released, claimed) before this loop read its last blocks: it has ended.
            self._ended(exam, self.block, "the keeper holds the exam done")
        if not accounts and not pools:
            self.block = head
            return []
        cache: dict[int, dict[int, int]] = {}
        sent: list[tuple[str, int, bool]] = []
        if lo < head and self.triggers:
            # Behind: the stops reached at the head go out first, and the blocks before it are read after.
            cache[head] = self._prices(head)
            sent = self._act(head, cache[head])
        by_block = self._logs(accounts, pools, lo, head)
        for b in range(lo, head + 1):
            if self.unread_signs:  # read again at the block before, inside the step too: no block judged without them
                self._read_signs_again(b - 1)
            due: list = []
            if b >= price_lo:
                prices = cache.get(b)
                if prices is None or set(prices) != {k[1] for k in self.triggers}:
                    prices = self._prices(b)  # a busy node ends the step here; the next one starts at this block
                cache[b] = prices
                due = self._watch(b, prices)
                if self.journal is not None:
                    try:
                        self.journal.add(b, prices, self.head_t.get(b), {k[1] for k in self.triggers} - set(prices))
                        if self.journal_said:
                            self.journal_said = False
                            log("stop_journal_works_again", block=b)
                    except OSError as exc:
                        self._journal_failed(exc)
            self._events(b, by_block.get(b, []))
            for key in due:  # after the block's own events: a stop filled in it is not overdue
                t = self.triggers.get(key)
                if t is not None and t["touch"] is not None and not t["said"]:
                    t["said"] = True
                    self._raise("stop_overdue", key, t, b, "reached and not carried out yet")
            if b < price_lo and not gap_said and any(t["armed"] < price_lo and self.signs.get(k[:2]) != 0
                                                     for k, t in self.triggers.items()):
                # A stop set inside the blocks not read, armed in them: the same gap, said as soon as it is known.
                gap_said = True
                self.alarm("stop_watch_gap", from_block=lo, to=price_lo - 1, standing=len(self.triggers),
                           why="a stop was set and armed inside blocks whose prices the loop did not read")
            self.block = b
            if self.sender is not None and b < head:
                self.sender.serve()  # the keeper's transactions between two blocks, not after the whole step
        sent += self._act(head, cache.get(head) or {}, skip=set(sent))  # the new ones first, then the receipts
        self._receipts(head)
        sent += self._act(head, cache.get(head) or {}, skip=set(sent))  # and the ones a receipt let go
        self._raise_due()  # its reads (was the exam over?) after the stops went out, never before them
        return sent

    def _get_logs(self, addresses: list[str], topics: list[str], lo: int, hi: int) -> list[dict]:
        out: list[dict] = []
        for start in range(lo, hi + 1, core.MAX_LOG_WINDOW):
            out += self.node.call("eth_getLogs", [{"address": addresses, "topics": [topics], "fromBlock": hex(start),
                                                   "toBlock": hex(min(hi, start + core.MAX_LOG_WINDOW - 1))}])
        return out

    def _logs(self, accounts: list[str], pools: list[str], lo: int, hi: int) -> dict[int, list[dict]]:
        found = self._get_logs(accounts + pools, ACCOUNT_TOPICS + POOL_TOPICS, lo, hi)
        known = set(accounts)
        done = set(self.keeper.done)
        pool_set = {p.lower() for p in pools}
        new = []
        for l in found:
            if l["topics"][0] in POOL_TOPICS and l["address"].lower() in pool_set:
                a = to_checksum_address("0x" + l["topics"][1][-40:])
                if a not in known and a not in done and a not in self.ended:
                    self.extra.add(a)
                    new.append(a)
                    known.add(a)
        if new:  # their stops in the same blocks, which the read above could not name
            found = found + self._get_logs(new, ACCOUNT_TOPICS, lo, hi)
        by_block: dict[int, list[dict]] = {}
        for l in found:
            if l["topics"][0] in ACCOUNT_TOPICS and to_checksum_address(l["address"]) in known:
                by_block.setdefault(int(l["blockNumber"], 16), []).append(l)
        for logs in by_block.values():
            logs.sort(key=lambda l: int(l.get("logIndex", "0x0"), 16))
        return by_block

    def _prices(self, block: int) -> dict[int, int]:
        """The oracle price, at `block`, of each asset with a stop or a take standing that was read: one that was not
        leaves its stops unjudged in this block and the others as they are."""
        assets = sorted({k[1] for k in self.triggers})
        prices: dict[int, int] = {}
        unread: list[int] = []
        why = ""
        for asset in assets:
            try:
                raw = self.node.call("eth_call", [{"to": ORACLE_PX, "data": "0x" + abi_encode(["uint32"], [px_index(asset)]).hex()},
                                                  hex(block)])
                prices[asset] = int(abi_decode(["uint64"], bytes.fromhex(raw[2:]))[0])
            except c.NodeBusy:
                raise
            except Exception as exc:  # a block the node keeps no state of, an answer that did not parse
                unread.append(asset)
                why = str(exc)[:120]
        if unread:
            self._blind(block, block, f"assets {unread}: {why}")
            return prices
        if self.blind_from is not None and assets:
            log("stop_watch_sees_again", blind_from=self.blind_from, to=block - 1)
            self.blind_from, self.blind_said = None, False
        return prices

    def _blind(self, lo: int, hi: int, why: str) -> None:
        """Blocks whose prices the loop has not: nothing is judged in them. Said once for a run of them, and an alarm once
        the run is `BLIND_ALARM_BLOCKS` long with a stop standing."""
        if self.blind_from is None:
            self.blind_from = lo
            log("stop_watch_blind", from_block=lo, to=hi, why=why,
                standing=len(self.triggers), note="a reach inside these blocks is not seen; the journal has a gap")
        if not self.blind_said and self.triggers and hi - self.blind_from + 1 >= BLIND_ALARM_BLOCKS:
            self.blind_said = True
            self.alarm("stop_watch_blind_long", from_block=self.blind_from, to=hi, standing=len(self.triggers), why=why)

    def _journal_failed(self, exc: Exception) -> None:
        """The journal could not be written (a full disk, say): an alarm at once, since the payout review holds every
        stop whose blocks it lacks; once, until a line is written again."""
        if not self.journal_said:
            self.journal_said = True
            self.alarm("stop_journal_failed", block=self.block, error=str(exc)[:200],
                       why="the journal of prices is not written: every stop standing meanwhile holds its payout")

    def _gap(self, lo: int, hi: int, why: str) -> None:
        """Blocks no price was read in, written to the journal as a gap: the review holds every stop that stood in them."""
        if self.journal is None or hi < lo:
            return
        try:
            self.journal.gap(lo, hi, why)
        except OSError as exc:
            self._journal_failed(exc)

    # ── the watch ────────────────────────────────────────────────────────────────────

    def _watch(self, block: int, prices: dict[int, int]) -> list:
        """Each standing stop judged in `block`, by the price read. One whose position is not read yet keeps the price,
        to be judged once it is (`_replay`). The keys overdue now, decided by the caller once the block's own events
        are in."""
        due = []
        for key, t in list(self.triggers.items()):
            exam, asset, take = key
            s = self.signs.get((exam, asset))
            if asset not in prices or t["armed"] > block:
                continue
            if s is None:
                kept = t.setdefault("unjudged", [])
                kept.append((block, prices[asset]))
                if len(kept) > UNJUDGED_MAX:
                    del kept[0]
                    self._say("stop_unjudged_dropped", exam=exam, asset=asset, why=f"position unread {UNJUDGED_MAX} blocks")
                continue
            if self._judge(key, t, block, prices[asset], s):
                due.append(key)
        return due

    def _judge(self, key, t: dict, block: int, px: int, s: int) -> bool:
        """One stop in one block: its reach goes on, starts or ends. A block not judged breaks a reach: one reached on
        both sides of it starts its count again, one that ended across it is a miss (events are read in every block, so
        no fill came). Whether it is overdue now."""
        exam, asset, take = key
        t["side"] = s  # the side it is judged on: an alarm says this one, not the one read since
        seen, t["seen"] = t.get("seen"), block
        gap = seen is not None and block > seen + 1
        if hit(take, s, px, t["px"]):
            if gap and t["touch"] is not None:
                # Reached on both sides of blocks not judged: whether it held between is not known. Its count starts
                # again here; nothing is raised for the gap itself.
                log("stop_reach_unclear", **self._what(key, t, seen), why=f"blocks {seen + 1}..{block - 1} not judged")
                t.update(touch=None, said=False, first=None)
            if t["touch"] is None:
                t["touch"], t["first"] = block, px
            return not t["said"] and block - t["touch"] + 1 >= self.overdue
        if t["touch"] is not None:
            if not gap and seen - t["touch"] + 1 < self.min_alarm_blocks:
                log("stop_reach_too_short", **self._what(key, t, seen),
                    why="a transaction sent on seeing it lands after it")
                t.update(touch=None, said=False, first=None)
            else:  # reached, back, and no fill read in between (events are read in every block): a miss
                self._missed(key, t, seen, "the price went back" + (" in blocks not judged" if gap else ""))
        return False

    def _replay(self, exam: str, asset: int) -> None:
        """The prices kept while the position of `exam`'s `asset` could not be read, judged now, each block against the
        position as it stood before it: read at the block before, once for each stretch the position did not move in.
        A read that fails again keeps the rest for the next time."""
        moves = self.moves.get((exam, asset), [])
        signs: dict[int, int] = {}
        for take in (False, True):
            key = (exam, asset, take)
            t = self.triggers.get(key)
            if t is None or not t.get("unjudged"):
                continue
            while t["unjudged"]:
                block, px = t["unjudged"][0]
                stretch = bisect.bisect_left(moves, block)  # moves are added in block order
                if stretch not in signs:
                    try:
                        raw = self.node.call("eth_call", [{"to": exam, "data": "0x" + c.encode_call(
                            "positionOf(uint32)", ["uint32"], [asset]).hex()}, hex(block - 1)])
                        signs[stretch] = sign_of(abi_decode(["int256"], bytes.fromhex(raw[2:]))[0])
                    except Exception as exc:
                        self._say("stop_position_unread", exam=exam, asset=asset, error=str(exc)[:120])
                        return
                t["unjudged"].pop(0)
                if self._judge(key, t, block, px, signs[stretch]) and not t["said"]:
                    t["said"] = True
                    self._raise("stop_overdue", key, t, block, "reached and not carried out yet")
                if key not in self.triggers:
                    break

    def _missed(self, key, t: dict, last: int, reason: str) -> None:
        if not t["said"]:
            self._raise("stop_missed", key, t, last, reason)
        else:
            log("stop_missed", **self._what(key, t, last), reason=reason, note="its alarm was raised while it lasted")
        t.update(touch=None, said=False, first=None)

    def _raise(self, event: str, key, t: dict, last: int, reason: str) -> None:
        """An alarm for a reach, raised once the step's stops are out (`_raise_due`): its reads must not hold a stop."""
        self.alarms_due.append((event, self._what(key, t, last), reason))

    def _raise_due(self) -> None:
        """The step's alarms for reaches, each unless the exam was over by the first block a fill could have landed in:
        past its deadline or its idle limit `fillTrigger` refuses (`TooLate`) whoever sends it, and it is the keeper's
        `expire` that is due."""
        due, self.alarms_due = self.alarms_due, []
        for event, what, reason in due:
            over = self._over_at(what["exam"], what["reached_from"] + 1)
            if over:
                log("stop_reach_after_exam_over", **what, reason=reason, over=over)
                continue
            self.alarm(event, **what, reason=reason, **({} if over is not None else {"over": "not read"}))

    def _over_at(self, exam: str, block: int) -> str | None:
        """Why the exam was over at `block` (`PaperExam._over`), "" when it was not, None when it could not be read."""
        tag = hex(block)
        try:
            def read(sig, out):
                raw = self.node.call("eth_call", [{"to": exam, "data": "0x" + c.encode_call(sig).hex()}, tag])
                return abi_decode(out, bytes.fromhex(raw[2:]))
            now = int(self.node.call("eth_getBlockByNumber", [tag, False])["timestamp"], 16)
            deadline = read("deadline()", ["uint64"])[0]
            last_fill = read("lastFillAt()", ["uint64"])[0]
            max_idle = read("terms()", [sk.EXAM_TERMS])[0][3]
        except Exception:
            return None
        if deadline and now > deadline:
            return f"past its deadline {deadline}"
        if max_idle and now > last_fill + max_idle:
            return f"past its idle limit, last fill {last_fill} + {max_idle}"
        return ""

    def _what(self, key, t: dict, last: int) -> dict:
        exam, asset, take = key
        return {"exam": exam, "asset": asset, "kind": "take" if take else "stop", "px": t["px"],
                "reached_from": t["touch"], "reached_to": last, "oracle_first": t["first"],
                "side": "long" if (t.get("side") if t.get("side") is not None else self.signs.get((exam, asset)) or 0) > 0
                else "short"}

    def _ended(self, exam: str, block: int, reason: str) -> None:
        for key in [k for k in self.triggers if k[0] == exam]:
            if self.triggers[key]["touch"] is not None:
                self._missed(key, self.triggers[key], block, reason)
            self._drop(key)
        for k in [k for k in self.moves if k[0] == exam]:
            del self.moves[k]
        self.ended.add(exam)

    def _events(self, block: int, logs: list[dict]) -> None:
        filled_in_tx = {(l["transactionHash"], to_checksum_address(l["address"]), int(l["topics"][1], 16),
                         bool(int(l["data"][2:66], 16))) for l in logs if l["topics"][0] == sk.TRIGGER_FILLED}
        moved: set[tuple[str, int]] = set()
        for l in logs:
            topic = l["topics"][0]
            exam = to_checksum_address(l["address"])
            if topic in (STOPPED, PASSED):
                self._ended(exam, block, "the exam ended")
                continue
            if exam in self.ended:
                continue  # an exam that ended keeps no stop, whatever its later logs say
            asset = int(l["topics"][1], 16)
            if topic == sk.FILLED:
                moved.add((exam, asset))
                self.moves.setdefault((exam, asset), []).append(block)
                continue
            take = bool(int(l["data"][2:66], 16))
            key = (exam, asset, take)
            self.event_at[key] = block
            t = self.triggers.get(key)
            if topic == sk.TRIGGER_FILLED:
                if t is not None and t["touch"] is not None:
                    log("stop_caught", exam=exam, asset=asset, kind="take" if take else "stop", px=t["px"],
                        reached_from=t["touch"], filled=block, late=block - t["touch"], tx=l["transactionHash"])
                else:
                    log("stop_filled", exam=exam, asset=asset, kind="take" if take else "stop", filled=block,
                        tx=l["transactionHash"])
                self._drop(key)
            elif topic == sk.TRIGGER_CLEARED:
                if (l["transactionHash"], exam, asset, take) in filled_in_tx:
                    continue  # the fill's own clearing, read with its TriggerFilled
                if t is not None and t["touch"] is not None:
                    self._missed(key, t, block, "taken off by the trader while reached")
                self._drop(key)
            elif topic == sk.TRIGGER_SET:
                _, px, _, armed = abi_decode(["bool", "uint64", "uint64", "uint256"], bytes.fromhex(l["data"][2:]))
                if t is not None and t["touch"] is not None:
                    self._missed(key, t, block, "moved by the trader while reached")
                self.triggers[key] = self._fresh(int(px), int(armed))
                self.probe.discard(key)
                moved.add((exam, asset))
        for exam, asset in moved:
            if any(k[0] == exam and k[1] == asset for k in self.triggers):
                self._read_sign(exam, asset, block)

    def _read_sign(self, exam: str, asset: int, block: int) -> None:
        """The side of a position after the events of `block`, read at it; None until it is read."""
        self.event_at[(exam, asset)] = block
        try:
            raw = self.node.call("eth_call", [{"to": exam, "data": "0x" + c.encode_call(
                "positionOf(uint32)", ["uint32"], [asset]).hex()}, hex(block)])
            s = sign_of(abi_decode(["int256"], bytes.fromhex(raw[2:]))[0])
        except Exception as exc:
            self._say("stop_position_unread", exam=exam, asset=asset, error=str(exc)[:120])
            self.signs[(exam, asset)] = None
            self.unread_signs.add((exam, asset))  # read again at the next step, with no event needed
            return
        self.unread_signs.discard((exam, asset))
        if any(t.get("unjudged") for k, t in self.triggers.items() if k[0] == exam and k[1] == asset):
            self._replay(exam, asset)
            if any(t.get("unjudged") for k, t in self.triggers.items() if k[0] == exam and k[1] == asset):
                # A read of the past failed: the kept prices are judged first, at the next step, and the newer ones are
                # kept behind them meanwhile, never judged ahead of them.
                self.signs[(exam, asset)] = None
                self.unread_signs.add((exam, asset))
                return
        if s == 0:
            for take in (False, True):
                t = self.triggers.get((exam, asset, take))
                if t is not None and t["touch"] is not None:
                    self._missed((exam, asset, take), t, block, "the position was closed")
        self.signs[(exam, asset)] = s

    def _read_signs_again(self, block: int) -> None:
        """The positions a read failed for, read again at `block`, the last one whose events are in."""
        for exam, asset in sorted(self.unread_signs):
            if not any(k[0] == exam and k[1] == asset for k in self.triggers):
                self.unread_signs.discard((exam, asset))
            elif block > 0:
                self._read_sign(exam, asset, block)

    def _drop(self, key) -> None:
        t = self.triggers.pop(key, None)
        self.probe.discard(key)
        if t is not None and t.get("unjudged"):
            # Prices kept while its position could not be read, and the stop gone before they were judged: this loop
            # cannot say whether it was reached in them. Said as an alarm, never dropped in silence; the payout review
            # judges those blocks by the journal, which holds them.
            blocks = [b for b, _ in t["unjudged"]]
            self.alarm("stop_unjudged", exam=key[0], asset=key[1], kind="take" if key[2] else "stop", px=t["px"],
                       from_block=blocks[0], to=blocks[-1], blocks=len(blocks),
                       why="the stop ended before its kept prices were judged (its position was not read)")

    # ── carrying out ─────────────────────────────────────────────────────────────────

    def _act(self, head: int, prices: dict[int, int], skip: set | frozenset = frozenset()) -> list[tuple[str, int, bool]]:
        """The stops reached at `head`, armed for the next block and not in flight, sent at once. One whose position is
        not read, or that the exam refused once, is tried with an eth_call first."""
        sent = []
        for key, t in list(self.triggers.items()):
            exam, asset, take = key
            if key in skip or t["armed"] > head + 1 or key in self.pending or exam in self.keeper.done:
                continue
            s = self.signs.get((exam, asset))
            if s == 0:
                continue
            data = c.encode_call("fillTrigger(uint32,bool)", ["uint32", "bool"], [asset, take])
            if s is None or key in self.probe:
                try:
                    self.node.call("eth_call", [{"from": self.keeper.sender, "to": exam, "data": "0x" + data.hex()}, "latest"])
                except Exception:
                    continue
            elif asset not in prices or not hit(take, s, prices[asset], t["px"]):
                continue
            sent.append(key)
            if self.wallet is None or self.sender is None or not self.keeper.is_filler():
                if t.get("would") != t["touch"]:
                    t["would"] = t["touch"]
                    log("would_fill_trigger", exam=exam, asset=asset, take=take, block=head)
                continue
            try:
                tx, nonce = self.sender.send(exam, data, STOP_GAS, self._gas_price(head), self.tip, bump=True)
            except Exception as exc:
                log("trigger_fill_failed", exam=exam, asset=asset, take=take, error=str(exc)[:200])
                continue
            self.pending[key] = (tx, nonce, head)
            self.probe.discard(key)
            log("trigger_sent", exam=exam, asset=asset, take=take, tx=tx, nonce=nonce, seen=head)
        return sent

    def _gas_price(self, head: int) -> int:
        if head - self.gas_read_at >= 10 or not self.gas_price:
            self.gas_price = int(self.node.call("eth_gasPrice"), 16)
            self.gas_read_at = head
        return self.gas_price

    def _receipts(self, head: int) -> None:
        for key, (tx, nonce, at) in list(self.pending.items()):
            try:
                rcpt = self.sender.receipt(nonce, tx)
            except Exception as exc:
                self._say("stop_receipt_unread", error=str(exc)[:120])
                return
            exam, asset, take = key
            if rcpt:
                del self.pending[key]
                self.sender.confirm(nonce)
                if int(rcpt["status"], 16) == 1:
                    log("trigger_filled", exam=exam, asset=asset, take=take, tx=tx, block=int(rcpt["blockNumber"], 16),
                        seen=at)
                else:
                    self.probe.add(key)
                    log("trigger_fill_refused", exam=exam, asset=asset, take=take, tx=tx,
                        block=int(rcpt["blockNumber"], 16), seen=at)
            elif self.sender.is_gone(nonce):
                # Its nonce is counted on chain without it: it will never land, and the stop may be sent again.
                del self.pending[key]
                log("trigger_fill_lost", exam=exam, asset=asset, take=take, tx=tx, seen=at)

    # ── running ──────────────────────────────────────────────────────────────────────

    def _say(self, event: str, every: float = 30.0, **fields) -> None:
        """A line that would repeat each block, said once in `every` seconds."""
        now = time.monotonic()
        if now >= self.quiet_until.get(event, 0.0):
            self.quiet_until[event] = now + every
            log(event, **fields)

    def _failed(self, event: str, exc: Exception) -> None:
        now = time.monotonic()
        if self.failing_since is None:
            self.failing_since = now
        self._say(event, error=str(exc)[:200])
        if not self.failing_said and now - self.failing_since >= STALL_S:
            self.failing_said = True
            if now - self.failing_alarm_at >= FAILING_REALARM_S:
                self.failing_alarm_at = now
                self.alarm("stop_loop_failing", seconds=int(now - self.failing_since), block=self.block,
                           error=str(exc)[:200])
            else:  # one alarm an hour at most: the flag file does not fill with the same episode
                log("stop_loop_failing_again", seconds=int(now - self.failing_since), block=self.block,
                    error=str(exc)[:200], note="its alarm was raised within the hour")

    def _served(self, until: float, stop: threading.Event) -> None:
        """Until `until`: the keeper's transactions sent as they come, the key's ledger swept."""
        if self.sender is not None:
            self.sender.serve()
            try:
                self.sender.sweep()
            except Exception as exc:
                self._say("send_sweep_failed", error=str(exc)[:120])
        while not stop.is_set():
            left = until - time.monotonic()
            if left <= 0:
                return
            if self.sender is None:
                stop.wait(left)
                return
            try:
                self.sender.handle(self.sender.requests.get(timeout=self.sender.wait_for(left)))
            except queue.Empty:
                self.sender.serve()  # one the node was busy for may be due now

    def _head_fresh(self, head: int, head_t: int) -> None:
        """The node's head held against this machine's clock: older than STALE_HEAD_S, the node is behind or stuck, and
        a stop reached now is not seen. An alarm once, a line when it is fresh again."""
        age = self.clock() - head_t
        if age > STALE_HEAD_S and not self.stale_said:
            self.stale_said = True
            self.alarm("stop_node_stale", head=head, seconds=int(age),
                       why="the node's head block is old by this machine's clock: the node is behind or stuck")
        elif age <= STALE_HEAD_S and self.stale_said:
            self.stale_said = False
            log("stop_node_fresh_again", head=head)

    def _behind(self, head: int) -> None:
        """The loop held against the node's head: short of it for BEHIND_S seconds is an alarm once."""
        now = self.clock()
        if self.block >= head:
            self.caught_up_at = now
            if self.behind_said:
                self.behind_said = False
                log("stop_loop_caught_up", block=self.block)
            return
        if self.caught_up_at is None:
            self.caught_up_at = now
        if now - self.caught_up_at >= BEHIND_S and not self.behind_said:
            self.behind_said = True
            self.alarm("stop_loop_behind", block=self.block, head=head, seconds=int(now - self.caught_up_at),
                       why="the loop is short of the node's head: the blocks between are not judged yet")

    def _balance(self) -> None:
        """The key's balance every BALANCE_EVERY_S, as stops' fills it still pays for at twice the gas price: fewer than
        `min_runway` is an alarm once, a line when it is back. A key with nothing left carries out no stop."""
        if self.sender is None or self.clock() - self.balance_at < BALANCE_EVERY_S:
            return
        self.balance_at = self.clock()
        try:
            balance = int(self.node.call("eth_getBalance", [self.sender.address, "latest"]), 16)
            price = self.gas_price or int(self.node.call("eth_gasPrice"), 16)
        except Exception as exc:
            self._say("stop_balance_unread", error=scrub(str(exc))[:120])
            return
        fills = balance // max(STOP_GAS * (2 * price + self.tip), 1)
        if fills < self.min_runway and not self.low_said:
            self.low_said = True
            self.alarm("stop_key_low", balance_wei=balance, fills_left=fills, gas_price=price,
                       why=f"the filler's key pays for fewer than {self.min_runway} stops' fills: top it up")
        elif fills >= self.min_runway and self.low_said:
            self.low_said = False
            log("stop_key_topped_up", balance_wei=balance, fills_left=fills)

    def run(self, poll: float, stop: threading.Event | None = None) -> None:
        """Forever (or until `stop`): the head every `poll` seconds, and a step for each new one; between them, the keeper's
        transactions. Nothing here waits on the keeper's thread; a busy node costs the look it refused."""
        stop = stop or threading.Event()
        log("stop_loop_started", node=host_of(self.node.url), tip_wei=self.tip, sends=self.sender is not None,
            journal=str(self.journal.path) if self.journal else None,
            flag=str(self.alarm.flag) if self.alarm.flag else None)
        while not stop.is_set():
            until = time.monotonic() + poll
            try:
                got = self.node.call("eth_getBlockByNumber", ["latest", False])
                head, head_t = int(got["number"], 16), int(got["timestamp"], 16)
                self.beat = time.monotonic()
                self.head_t[head] = head_t
                for b in [b for b in self.head_t if b < head - CATCHUP]:
                    del self.head_t[b]
                self._head_fresh(head, head_t)
                if head > self.block:
                    self.step(head)
                self._behind(head)
                self._balance()
                if self.failing_since is not None:
                    log("stop_loop_works_again", failed_for=int(time.monotonic() - self.failing_since))
                self.failing_since, self.failing_said = None, False
            except c.NodeBusy as exc:
                self._failed("stop_node_busy", exc)
            except Exception as exc:
                self._failed("stop_loop_failed", exc)
            if time.monotonic() - self.alive_at >= ALIVE_EVERY_S:
                self.alive_at = time.monotonic()
                log("stop_loop_alive", block=self.block, standing=len(self.triggers), in_flight=len(self.pending),
                    blind_from=self.blind_from)
            self._served(until, stop)


def keep_alive(loop: StopLoop, thread: threading.Thread, poll: float) -> threading.Thread:
    """From the keeper's loop: the stops' thread is there and moving. One that ended is an alarm and is started again;
    one that has not heard the node's head for `STALL_S` seconds, with no failure it said itself, is an alarm."""
    if not thread.is_alive():
        loop.alarm("stop_loop_dead", block=loop.block, why="the stops' thread ended; it is started again")
        return start(loop, poll)
    stalled = time.monotonic() - loop.beat
    if stalled >= STALL_S and not loop.stall_said and not loop.failing_said:
        loop.stall_said = True
        loop.alarm("stop_loop_stalled", seconds=int(stalled), block=loop.block)
    elif stalled < STALL_S and loop.stall_said:
        loop.stall_said = False
        log("stop_loop_moving_again", block=loop.block)
    return thread


def check_chain(node: Node) -> None:
    """The stops' node on the network the keeper's is on: a stop sent to another chain is no stop."""
    chain = int(node.call("eth_chainId"), 16)
    if chain != c.CHAIN_ID:
        raise SystemExit(f"refusing to run: the stops' node is on chain {chain}, expected {c.CHAIN_ID}")


def add_args(p: argparse.ArgumentParser) -> None:
    """The settings of the stops' loop. Without `--stops loop` the keeper is as it was, and none of the rest is read."""
    p.add_argument("--stops", choices=("inline", "loop"), default=os.environ.get("COLOSSEUM_STOPS", "") or "loop",
                   help="loop (the default): the stops in a thread of their own, every block, with the journal of prices "
                        "and the watch of missed stops (ops/stop_filler.py); inline: tried in the keeper's one loop, "
                        "with no journal (every payout with a stop is then held by the review)")
    p.add_argument("--stop-rpc", default=os.environ.get("COLOSSEUM_STOP_RPC_URL", ""),
                   help="with --stops loop: the node the stops are read from and the key's transactions sent to; the "
                        "keeper's (COLOSSEUM_RPC_URL) when empty")
    p.add_argument("--stop-tip-gwei", type=float, default=float(os.environ.get("COLOSSEUM_STOP_TIP_GWEI", "0") or 0),
                   help="with --stops loop: the priority fee of a stop's transaction, in gwei; zero, as every send of the "
                        "keeper's")
    p.add_argument("--stop-poll", type=float, default=None, help="seconds between looks at the head (default --poll)")
    p.add_argument("--stop-overdue", type=int, default=OVERDUE, help="blocks a reach may last unfilled before its alarm")
    p.add_argument("--stop-alarm-min-blocks", type=int, default=MIN_ALARM_BLOCKS,
                   help="a reach the price ended in fewer blocks is a line, not an alarm (1: every one an alarm)")
    p.add_argument("--price-journal", default=None,
                   help="the journal of oracle prices (default prices-<deployment>.jsonl next to the state); '' for none")
    p.add_argument("--stop-alarm", default=None,
                   help="the flag file of missed stops (default stops-<deployment>.alarm next to the state); '' for none")


def build(args, keeper, state: pathlib.Path, wallet=None, prefix: str = "") -> StopLoop:
    """The loop from the settings, its node checked against the network; a sender for the key when there is a wallet.
    `prefix` keeps the default files of a watch alone apart from the keeper's."""
    url = args.stop_rpc or c.RPC_URL
    node = Node(url)
    if c.NETWORK.name == "mainnet":
        core.check_node(url, c.CHAIN_ID)
    check_chain(node)
    journal_path = (state.parent / f"{prefix}prices-{args.deployment}.jsonl") if args.price_journal is None else (
        pathlib.Path(args.price_journal) if args.price_journal else None)
    flag = (state.parent / f"{prefix}stops-{args.deployment}.alarm") if args.stop_alarm is None else (
        pathlib.Path(args.stop_alarm) if args.stop_alarm else None)
    if args.stop_tip_gwei < 0:
        raise SystemExit("--stop-tip-gwei must not be negative")
    return StopLoop(keeper, node, wallet=wallet, sender=Sender(wallet, node) if wallet is not None else None,
                    tip_wei=int(round(args.stop_tip_gwei * GWEI)),
                    journal=PriceJournal(journal_path) if journal_path else None, alarm=Alarm(flag),
                    overdue=args.stop_overdue, min_alarm_blocks=args.stop_alarm_min_blocks)


def start(loop: StopLoop, poll: float) -> threading.Thread:
    thread = threading.Thread(target=loop.run, args=(poll,), name="stops", daemon=True)
    thread.start()
    return thread


def watch_pass(keeper, loop: StopLoop) -> None:
    """The watch alone, every --every seconds: new pools and accounts, and each one not known to have ended read again
    (an exam that ended is told to the loop by its own read)."""
    keeper.scan(int(c.rpc("eth_blockNumber"), 16))
    for a in keeper.live():
        if a in loop.ended:
            keeper.done.add(a)
            continue
        try:
            loop.reread(a)
        except Exception as exc:
            log("stops_unread", account=a, error=str(exc)[:200])


def main(argv: list[str] | None = None) -> int:
    """The watch alone: the factory's accounts found as the keeper finds them, their stops reread every --every seconds,
    and the loop with no key. It sends nothing; run it on another machine and node to see what the keeper missed even
    when the keeper itself is down. Its files are its own (watch-prices-…, watch-stops-…)."""
    from ops import deployments
    from ops import sim_network as sn

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/ with a SimPoolFactory")
    p.add_argument("--every", type=int, default=300, help="seconds between reads of the pools, accounts and stops")
    p.add_argument("--poll", type=float, default=0.5)
    p.add_argument("--state", help="the watch's own state file (default ops/state/watch-<deployment>.json)")
    add_args(p)
    sn.add_args(p)
    args = p.parse_args(argv)
    sn.selected(args, c.NETWORK)
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    state = pathlib.Path(args.state) if args.state else sk.STATE_DIR / f"watch-{args.deployment}.json"
    keeper = sk.SimKeeper(record["SimPoolFactory"], None, "0x" + "00" * 20, True, state, int(record["block"]))
    loop = build(args, keeper, state, prefix="watch-")
    poll = args.stop_poll or args.poll
    thread = start(loop, poll)
    while True:
        try:
            watch_pass(keeper, loop)
        except Exception as exc:
            log("watch_pass_failed", error=str(exc)[:200])
        deadline = time.monotonic() + args.every
        while time.monotonic() < deadline:
            thread = keep_alive(loop, thread, poll)
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    sys.exit(main())
