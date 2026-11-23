"""The keeper's side of a factory's stop book (src/sim/StopBook.sol, docs/SIM-POOL.md, "The stops"): the book's journal
checked in every block, and each exam's touched stops and takes carried out.

A factory deployed with a stop book carries out its exams' stops and takes itself: `poke()`, open to anyone, logs the
oracle price of every asset with a stop or a take on it, once a block, and `settle(exam)`, open to anyone too, carries
out each touched stop or take at the first logged price that reached it, at that price and in that block's name,
however late the settle comes. A block nobody checked is not in the journal: the next check says so (`Gap`), and a stop
the price reached only in such a block is not carried out. Every exam function that changes anything checks the block
and settles first, so a trader who acts writes the journal themselves; one who sends nothing would leave the blocks
unchecked. This loop is what checks them:

  every block      → `poke()` while the book watches any asset (`watched()`) or an exam holds an entry, signed and sent
                     at once with a fixed gas (never an estimate: an estimate is a block late), no wait for its receipt.
                     The checks go out from a thread of their own (`Poker`), on a node of its own when one is given: it
                     looks at the head every `--poke-poll` seconds and sends the check first, for each new head and once
                     a second in any case (a head that went back, stopped or looks old is an alarm, not a reason to stop
                     checking); it reads nothing before it but the head (and the book's `watched()` while it watches
                     nothing, so the first check goes in the very block it is wanted). A send the node refused is tried
                     again at the next look; the check is priced on the last gas price or the newest block's base fee,
                     whichever is higher. Nothing else the loop reads (logs,
                     receipts, `touches`, the gaps) is in its way: a node refusing those costs them, never a check;
  a check that     → the checks go only from keys of their own (`--poke-wallet`), tried in turn, never the keeper's (on
  is late            mainnet the loop does not start without one); one with no block after `POKE_RESEND_S` is signed
                     again under its nonce with a quarter more fee, and the next check goes through the next key;
  the watch of     → on every new head the thread reads `lastPokeBlock()` and `watched()`: blocks going by unchecked while
  the checks         the book watches an asset are an alarm (`book_poke_lagging`, again every `ALARM_EVERY_S` while they
                     last), and so are a node whose newest block is old (`book_node_stale`, the other node then used; both
                     old: `book_head_stale`), a head that cannot be read, checks that cannot be sent (`book_poke_failing`),
                     checks waiting for a block (`book_poke_stuck`, `book_pokes_queued`) and a key whose money pays fewer
                     hours of checks than `--poke-runway-hours` (`book_key_low`), the book unreadable
                     (`book_views_unread`), an asset logged with no price (`book_no_price`), and the book's own watch
                     silent (`book_watch_silent`). `ops/book_watch.py` is the same watch
                     as a process of its own, on another machine and node, that checks the blocks itself when this one is
                     late;
  a touch          → `settle(exam)` for an exam whose `touches(exam)` is not empty, and for one with a change of a stop
                     (taken back or moved) that has come into force, so the book takes the entry out and stops logging
                     its asset. With a fixed gas too: the book's settle does nothing for want of gas, never reverts, so
                     an estimate would find the gas of doing nothing. A long settle stops between two steps and keeps
                     what it found; the loop sends the next one in a later block while `touches` is not empty. A fill
                     that ran out of the gas the call left it (`Fired` with `STARVED`) is sent again with the most a
                     small block holds. A touch that stands in `touches` longer than `TOUCH_ALARM_BLOCKS`, whatever the
                     cause, is an alarm (`book_touch_standing`), and so is an exam whose touches cannot be read as long;
                     a settle reverted in its block `SETTLE_REVERTS` times in a row is an alarm and waits
                     `FAIL_BACKOFF_BLOCKS`;
  a gap            → each `Gap` the book writes while it watched an asset is the loop's own failure: a line, a count,
                     and an alarm. The prices of those blocks are read back from the node where it keeps them, written
                     to the journal of prices, and judged against the book as it stood in the gap (`watched()` and the
                     entries at the block before it and at its last block, and now); a stop or a take they reached is
                     an alarm of its own. `ops/payout_review.py` does not pass such an exam's payout as clean: a stop
                     reached in a gap, gap blocks whose price it could not read, or blocks since `lastPokeBlock()` not
                     yet closed by a `Gap` make its verdict "suspicious" with the draft of a stop of reason 3, and a
                     payout waiting for approval is held ("hold");
  its state        → the last block read and the gaps still to read back, in a file of its own (`--book-state`): after a
                     restart the loop reads the book's events from there, so a `Gap` written while it was down is said;
  the key          → every transaction of the keeper's key goes out through one sender (`stop_filler.Sender`): its own
                     nonce, a transaction late by `POKE_RESEND_S` signed again with more fee, and the keeper's own sends
                     queued to it;
  the nodes        → `--stop-rpc` (the keeper's node when not set) and a backup (`--stop-backup-rpc`, or the keeper's
                     COLOSSEUM_RPC_BACKUP_URL): a call the first refuses, times out on, answers with HTTP 401/403/404 or a
                     limit (-32005, -32007) goes to the second; a node that failed so is asked second for
                     `FAILOVER_BACK_S` seconds, not first every time.

What is left of `--stops` (COLOSSEUM_STOPS) with a stop book: nothing sends `fillTrigger` (an exam with a book refuses it,
`NotBook`), and the watch of reaches by the price is this loop's gaps. `--stops` keeps its meaning for a factory with no
book (the 998 rehearsal), on the same code as before.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import queue
import threading
import time

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from ops import keeper as core
from ops import sim_keeper as sk
from ops import stop_filler as sf
from spike.hlspike import common as c

log = core.log

# StopBook's events; the tests hold each against the Solidity source.
GAP = "0x" + keccak(text="Gap(uint256,uint256)").hex()
FIRED = "0x" + keccak(text="Fired(address,uint32,bool,uint64,uint256,uint8)").hex()
NO_PRICE = "0x" + keccak(text="NoPrice(uint32)").hex()
READ_GAS_SET = "0x" + keccak(text="ReadGasSet(uint256,uint256)").hex()
BOOK_TOPICS = [GAP, FIRED, NO_PRICE, READ_GAS_SET]
POOL_TOPICS = [sk.EXAM_SOLD, sk.FUNDED]
# `Fired.result` (StopBook.FAILED .. STARVED).
FAILED, FILLED, CLOSED, ENDED, STARVED = range(5)
RESULT_NAME = ("failed", "filled", "closed", "ended", "starved")
# StopBook.Touch and StopBook.Entry, as `touches` and `entryOf` return them.
TOUCH = "(uint32,bool,uint64,uint64,uint32,uint256,uint256,uint256)"
ENTRY = "(uint32,uint40,uint40,uint40,bool,bool,uint8,uint32,uint40)"

# The gas of a transaction that must fit a small HyperEVM block (2 000 000) with room for the block's own: the most this
# loop ever gives one.
MAX_TX_GAS = 1_950_000
# A check: the book's own work, and for each asset its two precompile reads (each gets its whole allowance, or the check
# reverts `PokeGas`) and the words it writes, a run of its tree closed on many levels at the worst (test_gas_poke_atTheCap:
# 1 260 830 for 20 assets with 50 000 for each read).
POKE_GAS_BASE = 100_000
POKE_GAS_PER_ASSET = 40_000
PRECOMPILE_GAS = 50_000  # StopBook.MAX_READ_GAS: what each read may be given at most, when the book does not say
# StopBook.NoProgress(uint256)'s selector, as a node's revert data carries it: a settle that did not make one step.
NO_PROGRESS = "0x" + keccak(text="NoProgress(uint256)")[:4].hex()
# A settle: a month of 32 entries with no touch is 1 291 215 in one call, every one touched 2 538 806 in one call or
# three under 1 360 000 (the book's measures). One under this does a part, keeps it, and the next one goes on.
SETTLE_GAS = 1_500_000
# How many of a key's pokes may wait for a block at once: past this the key is stuck and its next check goes through
# another key; with none left it is an alarm (`book_pokes_queued`), never a silence.
MAX_POKES_IN_FLIGHT = 6
# Seconds a poke may wait for a block (one and a half small blocks) before it is signed again under its nonce with a
# quarter more fee, and before the next check goes through the next key.
POKE_RESEND_S = 1.5
# Seconds between two reads of a landed check's receipt for its result (a revert): the count says it landed.
POKE_STATUS_EVERY_S = 5.0
# Seconds a key's oldest poke may wait for a block before it is an alarm (`book_poke_stuck`).
POKE_STUCK_S = 10.0
# Seconds between two checks when no new head is seen (a head that went back, stopped, or looks old): a check is cheap,
# a block left unchecked is a stop not carried out.
POKE_EVERY_S = 1.0
# Seconds a look may spend after its check on the rest (receipts, the book's views, the sweep, the price, the money):
# what is left goes to the next look.
LOOK_BUDGET_S = 0.5
# Calls a second one checks' thread makes of its node on a healthy chain (a look every quarter second): the head each
# look, the check and the key's count each block, the book's views each head, the price every ten blocks. The keeper's
# loop adds its own (logs, touches, receipts); a public node's limit is about 100 a minute: give the keeper a node of
# its own (docs/SIM-POOL.md, "The stops").
CALLS_PER_S_BUDGET = 9.0
# Blocks between two reads of the book's NoPrice events by a checks' thread, and blocks in a row of NoPrice of one asset
# before it is an alarm (`book_no_price`): a check that logs a zero price for an asset carries out none of its stops.
NO_PRICE_EVERY_BLOCKS = 5
NO_PRICE_ALARM_BLOCKS = 3
# Seconds with no read of the book's watched() and lastPokeBlock() before it is an alarm.
VIEWS_BLIND_S = 10.0
# The book's own watch sends at least once in HEARTBEAT_S; the keeper reads its key's count every WATCH_READ_EVERY_S, and
# a count unmoved for WATCH_SILENT_S is an alarm.
HEARTBEAT_S = 3_600.0
WATCH_READ_EVERY_S = 600.0
WATCH_SILENT_S = 2 * HEARTBEAT_S + WATCH_READ_EVERY_S
# Blocks in a row with no check sent before it is an alarm; a key refused for want of money is one at once.
POKE_FAIL_BLOCKS = 3
# Blocks between `lastPokeBlock()` and the head, while the book watches an asset, past which it is an alarm.
LAG_ALARM_BLOCKS = 3
# Seconds the head may be unreadable before it is an alarm; a head older than the node's own clock is one at once.
BLIND_S = 5.0
# Seconds between two of the same alarm while its cause lasts.
ALARM_EVERY_S = 30.0
# Seconds between two reads of a key's money, and between two alarms of a key short of it.
BALANCE_EVERY_S = 60.0
LOW_ALARM_EVERY_S = 3_600.0
# A HyperEVM small block, in seconds: the runway of a key in hours from its runway in blocks.
BLOCK_S = 1.0
RUNWAY_HOURS = 72.0
# Seconds a node whose newest block is older than this has stopped (the default of --stale-s).
STALE_S = 10.0
# Seconds a node that failed is asked second, before it is tried first again.
FAILOVER_BACK_S = 60.0
# Blocks every check goes with the most gas after one reverted, before the book's read gas is used again.
FLOOR_BLOCKS = 300
# Blocks a touch may stand in `touches`, or an exam's touches stay unread, before it is an alarm.
TOUCH_ALARM_BLOCKS = 10
# Settles of an exam reverted in their block in a row before it is an alarm and waits `FAIL_BACKOFF_BLOCKS`.
SETTLE_REVERTS = 3
# Seconds the key's oldest transaction may wait for a block before the queue is an alarm.
STUCK_S = 60.0
# Blocks of a gap whose prices are read back, the latest first: each costs a read an asset.
GAP_READ_BLOCKS = 60
# Blocks of gaps read back in one step, after its check went out: a long read never holds up the next check.
GAP_BLOCKS_PER_STEP = 5
# Blocks an exam whose fill failed (or starved with the most gas) waits before its next settle: the cause is the exam's,
# and a settle a block would only spend gas.
FAIL_BACKOFF_BLOCKS = 30
# The keeper's exams whose entries are read in one step after a start (`BookLoop._seed`).
SEED_PER_STEP = 10
# Blocks back the loop reads in any case, as the stops' loop (stop_filler.CATCHUP).
CATCHUP = sf.CATCHUP
POKE_DATA = c.encode_call("poke()")


def poke_gas(assets: int, precompile_gas: int = 2 * PRECOMPILE_GAS) -> int:
    """The gas a check of `assets` assets is sent with: never short of what the book asks for its reads."""
    return min(MAX_TX_GAS, POKE_GAS_BASE + assets * (precompile_gas + POKE_GAS_PER_ASSET))


def reached(below: bool, level: int, px: int) -> bool:
    """`StopBook._hits`: a price at or under the level (`below`), or at or over it; no price reaches nothing."""
    return px != 0 and (px <= level if below else px >= level)


def phase(entry: tuple, block: int) -> tuple[int, int]:
    """`StopBook._phase`: the level and the armedAt of an entry in `block`, its change's from `nextAt` on."""
    level, armed, _, _, _, _, _, next_level, next_at = entry
    if next_at and block >= next_at:
        return int(next_level), int(next_at)
    return int(level), int(armed)


def book_of(record: dict, factory: str, call=None) -> str | None:
    """The factory's stop book: the record's, or the factory's own `stopBook()`. None for a factory made before the book,
    which has no such function (the call reverts); any other failure of the call is raised, never read as no book."""
    if record.get("StopBook"):
        return to_checksum_address(record["StopBook"])
    call = call or c.rpc
    try:
        raw = call("eth_call", [{"to": factory, "data": "0x" + c.selector("stopBook()").hex()}, "latest"])
    except RuntimeError as exc:
        if "revert" in str(exc).lower():
            return None
        raise
    if not raw or raw == "0x":
        return None
    book = abi_decode(["address"], bytes.fromhex(raw[2:]))[0]
    return None if int(book, 16) == 0 else to_checksum_address(book)


class NodeStale(RuntimeError):
    """A node answers, and its newest block is older than it may be: it has stopped following the chain, or this
    machine's clock is off. `number`: the newest block seen all the same."""

    def __init__(self, message: str, number: int | None = None):
        super().__init__(message)
        self.number = number


# What in an error says the node failed, not the call: its refusal of the caller (401, 403, 404, 408), a limit, a timeout.
FAILOVER_MARKS = ("http 401", "http 403", "http 404", "http 408", "-32005", "-32007", "request limit", "rate limit",
                  "quota", "timeout", "timed out")


def failover_worthy(exc: Exception) -> bool:
    """Whether a failed call is the node's failure and goes to the other node; a revert or `nonce too low` is an answer."""
    if isinstance(exc, (sf.Unsure, c.NodeBusy, NodeStale)):
        return True
    text = str(exc).lower()
    return any(mark in text for mark in FAILOVER_MARKS)


NAMES = ("first", "backup")


def spawn(fn) -> None:
    threading.Thread(target=fn, name="node-probe", daemon=True).start()


class Nodes:
    """A node and its backup as one: a call the first fails on (busy, a timeout, HTTP 401/403/404, a limit) goes to the
    second. The node that failed is then asked second; after `FAILOVER_BACK_S` seconds it is tried again in the
    background (`spawn`), never in the way of a call, and asked first once more only when it answers a fresh block. An
    answer, an error in it included (a revert, `nonce too low`), is the answer: it never goes on. `head()` is the newest
    block of a node whose newest block is fresh (`stale_s`); `basefee` the last block's base fee seen."""

    def __init__(self, first: sf.Node, backup: sf.Node | None = None, *, stale_s: float = STALE_S, standing=None,
                 spawn=spawn):
        self.first = first
        self.backup = backup
        self.nodes = [first] + ([backup] if backup is not None else [])
        self.url = first.url
        self.stale_s = stale_s
        self.prefer = 0
        self.prefer_since = 0.0
        self.probing = False
        self.spawn = spawn
        self.basefee = 0
        self.standing = standing  # a Standing, for a node found stale while the other one serves

    def _order(self) -> list[int]:
        if self.prefer and not self.probing and time.monotonic() - self.prefer_since >= FAILOVER_BACK_S:
            self.probing = True
            self.spawn(self._probe)
        return [self.prefer] + [i for i in range(len(self.nodes)) if i != self.prefer]

    def _probe(self) -> None:
        """The node that failed, tried again: a fresh block, and it is asked first again; anything else, a minute more."""
        try:
            _number, age, _fee = block_age(self.nodes[0])
            if age <= self.stale_s:
                self.prefer = 0
                log("book_node_back", node=NAMES[0])
            else:
                self.prefer_since = time.monotonic()
        except Exception:
            self.prefer_since = time.monotonic()
        finally:
            self.probing = False

    def _failed(self, i: int) -> None:
        if i == self.prefer and len(self.nodes) > 1:
            self.prefer = 1 - i
            self.prefer_since = time.monotonic()

    def call(self, method: str, params=()):
        order = self._order()
        for k, i in enumerate(order):
            try:
                return self.nodes[i].call(method, params)
            except Exception as exc:
                if k + 1 >= len(order) or not failover_worthy(exc):
                    raise
                log("book_node_backup", method=method, failed=NAMES[i], why=str(exc)[:120])
                self._failed(i)

    def head(self) -> int:
        """The newest block, from the first node in turn whose newest block is no older than `stale_s` seconds. A node
        that answers an old block is failed over like one that does not answer, and said (`book_node_stale`); every node
        old raises `NodeStale` carrying the newest block seen."""
        order = self._order()
        stale: list[tuple[str, str]] = []
        newest: int | None = None
        last: Exception | None = None
        for i in order:
            try:
                number, age, fee = block_age(self.nodes[i])
            except Exception as exc:
                last = exc
                self._failed(i)
                continue
            self.basefee = fee or self.basefee
            if age > self.stale_s:
                stale.append((NAMES[i], f"block {number}, {int(age)} s old"))
                newest = number if newest is None else max(newest, number)
                self._failed(i)
                continue
            if self.standing is not None:
                for name, seen in stale:
                    self.standing.raise_(f"node_stale:{name}", "book_node_stale", node=name, seen=seen,
                                         why="the node answers an old block: the other one is used")
                self.standing.clear(f"node_stale:{NAMES[i]}", "book_node_fresh_again", node=NAMES[i])
            return number
        if stale:
            raise NodeStale("every node's newest block is old: " + "; ".join(f"{n} {s}" for n, s in stale), newest)
        raise last if last is not None else RuntimeError("no node")


def block_age(node) -> tuple[int, float, int]:
    """A node's newest block, how many seconds old it is by this machine's clock, and its base fee (0: none said)."""
    b = node.call("eth_getBlockByNumber", ["latest", False])
    return int(b["number"], 16), time.time() - int(b["timestamp"], 16), int(b.get("baseFeePerGas") or "0x0", 16)


def fresh_head(node, stale_s: float = STALE_S) -> int:
    """The newest block of `node` (a `Nodes`, or one node), raised `NodeStale` when it is older than `stale_s`."""
    if callable(getattr(node, "head", None)):
        return node.head()
    number, age, _fee = block_age(node)
    if age > stale_s:
        raise NodeStale(f"the node's newest block is {int(age)} s old at block {number}", number)
    return number


class Standing:
    """Alarms that stand while their cause lasts: raised at once, raised again every `every` seconds while it lasts,
    and a line once when it is over. `alarm` is the loop's (`stop_filler.Alarm`)."""

    def __init__(self, alarm, every: float = ALARM_EVERY_S):
        self.alarm = alarm
        self.every = every
        self.at: dict[str, float] = {}
        self.lock = threading.Lock()

    def raise_(self, key: str, event: str, every: float | None = None, **fields) -> bool:
        now = time.monotonic()
        with self.lock:
            last = self.at.get(key)
            if last is not None and now - last < (self.every if every is None else every):
                return False
            self.at[key] = now
        self.alarm(event, **fields)
        return True

    def clear(self, key: str, event: str, **fields) -> None:
        with self.lock:
            was = self.at.pop(key, None)
        if was is not None:
            log(event, **fields)

    def on(self, key: str) -> bool:
        return key in self.at


def poke_sender(wallet, node) -> sf.Sender:
    """A sender for a stop book's key: every transaction raised in fee when late, a lean sweep (`stop_filler.Sender`)."""
    s = sf.Sender(wallet, node)
    s.bump_all, s.resend_after, s.lean = True, POKE_RESEND_S, True
    return s


class Lane:
    """One key that sends the book's checks: its sender and the checks it has waiting for a block."""

    def __init__(self, name: str, sender: sf.Sender):
        self.name = name
        self.sender = sender
        self.in_flight: list[tuple[int, int, float, str]] = []  # (nonce, head it was sent after, time sent, first hash)
        self.counted: int | None = None  # the chain's count of the key at this look
        self.status_at = -1e18
        self.counted_for = 0
        self.balance_at = -1e18
        self.runway: dict | None = None


class Poker:
    """The book's checks, sent from a thread of their own (see the module's text), and the watch of them.

    `lanes`: the keys that send, tried in turn (none: it watches and sends nothing); never the key of the keeper's other
    transactions. `gas(watched)`: the gas of a check. `wants()`: a reason to check besides what `watched()` said (an exam
    holding an entry). A check goes out for each new head and in any case every `POKE_EVERY_S` seconds: a head that went
    back (another node), stopped or looks old (this machine's clock) is an alarm, never a reason to stop checking.
    `takeover`: with 0 it checks every block; with N it checks only while the book is N blocks behind or more, then
    every block for `hold` blocks after the last time it was (the second sender, `ops/book_watch.py`), and once every
    `HEARTBEAT_S` in any case, so that the keeper sees it is alive (`watch_address`). `on_revert(head)`: a check
    reverted."""

    def __init__(self, book: str, nodes, lanes, alarm, *, gas=None, wants=None, takeover: int = 0,
                 hold: int = 600, lag_alarm: int = LAG_ALARM_BLOCKS, tip_wei: int = 0,
                 runway_hours: float = RUNWAY_HOURS, on_revert=None, role: str = "keeper",
                 watch_address: str | None = None):
        self.book = to_checksum_address(book)
        self.nodes = nodes
        self.lanes = list(lanes)
        self.alarm = alarm
        self.standing = Standing(alarm)
        if isinstance(nodes, Nodes) and nodes.standing is None:
            nodes.standing = self.standing
        self.gas = gas or (lambda n: poke_gas(max(n, 1)))
        self.wants = wants or (lambda: False)
        self.takeover = max(0, takeover)
        self.hold = hold
        self.lag_alarm = lag_alarm
        self.tip = tip_wei
        self.runway_hours = runway_hours
        self.on_revert = on_revert
        self.role = role
        self.watch_address = to_checksum_address(watch_address) if watch_address else None
        self.watch_nonce: int | None = None
        self.watch_seen_at = time.monotonic()
        self.watch_read_at = -1e18
        self.no_price_to = 0  # the last block whose NoPrice events were read
        self.no_price_runs: dict[int, tuple[int, int]] = {}  # asset -> (first, last) block of its run of NoPrice
        self.threaded = False
        self.head = 0
        self.poked_for = 0
        self.sent_at = -1e18
        self.watched_n = 0
        self.last_poke: int | None = None
        self.read_for = 0
        self.views_ok_at = time.monotonic()
        self.taken_until = 0
        self.price = 0
        self.price_at = -10**9
        self.gas_used = 0
        self.fail_from: int | None = None
        self.blind_since: float | None = None
        self.beat = time.monotonic()
        self.quiet_until: dict[str, float] = {}
        for lane in self.lanes:
            if lane.sender.price_hint is None:
                lane.sender.price_hint = self.price_now

    def price_now(self) -> int:
        """The gas price as last read, or the newest block's base fee when that is higher (a basefee that jumped prices
        the next check over it at once, not ten blocks on), or that alone when no price was ever read."""
        return max(self.price, int(getattr(self.nodes, "basefee", 0) or 0))

    # ── a look ───────────────────────────────────────────────────────────────────────

    def tick(self, head: int | None = None) -> bool:
        """One look: the head (an old one is an alarm and still used), the check sent if one is due, then the rest, each
        on its own and within `LOOK_BUDGET_S`. Whether a check went out."""
        start = time.monotonic()
        self.beat = start
        if head is None:
            try:
                head = fresh_head(self.nodes)
                self._sees(head)
            except NodeStale as exc:
                self._blind(exc)
                head = exc.number
            except Exception as exc:
                self._blind(exc)
        else:
            self._sees(head)
        if head is not None:
            self.head = max(self.head, head)
        sent = self._due(head, start) and self._send(head if head is not None else self.head)
        for step in (self._landed, self._liveness, self._sweep, self._no_price, self._price_now, self._balance,
                     self._watch_alive):
            if step is not self._landed and time.monotonic() - start > LOOK_BUDGET_S:
                break  # the next look goes on; the check is never late for these
            self._guard(step, head if head is not None else self.head)
        return sent

    def _due(self, head: int | None, now: float) -> bool:
        if head is not None and self.read_for < head and (self.takeover or not (self.watched_n or self.wants())):
            # Nothing known to watch: the book read first, so the first check goes in the block it is wanted. The second
            # sender reads `lastPokeBlock` first: it checks only behind the first.
            self._guard(self._liveness, head)
        blind = now - self.views_ok_at >= VIEWS_BLIND_S  # the book cannot be read: checked all the same
        if self.takeover and self.lanes and now - self.sent_at >= HEARTBEAT_S:
            return True  # the watch's sign of life
        if not (self.watched_n or self.wants() or blind):
            return False
        if self.takeover and not self._behind(head if head is not None else self.head, blind):
            return False
        return (head is not None and head > self.poked_for) or now - self.sent_at >= POKE_EVERY_S

    def _behind(self, head: int, blind: bool) -> bool:
        behind = blind or self.last_poke is None or head - self.last_poke >= self.takeover
        if behind:
            if head > self.taken_until:
                self.alarm("book_poke_taken_over", head=head, last_poke=self.last_poke, role=self.role,
                           why=f"the book is {self.takeover} blocks or more behind, or cannot be read: this watch checks "
                               f"every block for {self.hold} blocks")
            self.taken_until = head + self.hold
        return head <= self.taken_until

    def _guard(self, step, head: int) -> None:
        try:
            step(head)
        except Exception as exc:
            self._say(f"book_poker_{step.__name__.strip('_')}_failed", error=str(exc)[:160])

    def _say(self, event: str, every: float = 30.0, **fields) -> None:
        now = time.monotonic()
        if now >= self.quiet_until.get(event, 0.0):
            self.quiet_until[event] = now + every
            log(event, **fields)

    def _sees(self, head: int) -> None:
        if self.blind_since is not None:
            log("book_head_seen_again", head=head, blind_for=int(time.monotonic() - self.blind_since))
            self.blind_since = None
        self.standing.clear("blind", "book_head_fresh_again", head=head)

    def _blind(self, exc: Exception) -> None:
        now = time.monotonic()
        if self.blind_since is None:
            self.blind_since = now
        stale = isinstance(exc, NodeStale)
        self._say("book_head_unread", 5.0, error=str(exc)[:160])
        if stale or now - self.blind_since >= BLIND_S:
            self.standing.raise_("blind", "book_head_stale" if stale else "book_head_unread",
                                 seconds=int(now - self.blind_since), error=str(exc)[:160], role=self.role,
                                 why="no fresh block is seen (a node stopped, or this machine's clock is off): checks "
                                     "still go out every second, the book's lag may not be known")

    # ── the check ────────────────────────────────────────────────────────────────────

    def _late(self, lane: Lane, now: float) -> bool:
        return bool(lane.in_flight) and now - lane.in_flight[0][2] >= POKE_RESEND_S

    def _send(self, head: int) -> bool:
        errors: list[str] = []
        now = time.monotonic()
        price = self.price_now()
        if not price:
            try:
                price = self._read_price(head)
            except Exception as exc:
                errors.append(f"gas price: {str(exc)[:120]}")
        if price:
            gas = self.gas(self.watched_n)
            # A key whose oldest check waits longer than POKE_RESEND_S leaves the check to the next key at once; with
            # every key late, the one with room goes all the same.
            order = [l for l in self.lanes if not self._late(l, now)] + [l for l in self.lanes if self._late(l, now)]
            for lane in order:
                if len(lane.in_flight) >= MAX_POKES_IN_FLIGHT:
                    errors.append(f"{lane.name}: {len(lane.in_flight)} checks wait for a block")
                    continue
                try:
                    tx, nonce = lane.sender.send(self.book, POKE_DATA, gas, price, self.tip, bump=True,
                                                 resend_after=POKE_RESEND_S)
                except Exception as exc:
                    errors.append(f"{lane.name}: {str(exc)[:160]}")
                    continue
                lane.in_flight.append((nonce, head, now, tx))
                self.poked_for = max(self.poked_for, head)
                self.sent_at = now
                if self.fail_from is not None:
                    log("book_poke_works_again", head=head, failed_from=self.fail_from, lane=lane.name)
                    self.fail_from = None
                self.standing.clear("failing", "book_poke_sent_again", head=head)
                return True
        if not self.lanes:
            return False
        if self.fail_from is None:
            self.fail_from = head
        self._say("book_poke_failed", 5.0, head=head, errors=errors, then="tried again at the next look")
        queued = bool(errors) and all("wait for a block" in e for e in errors)
        funds = any("insufficient funds" in e.lower() for e in errors)
        if queued or funds or head - self.fail_from + 1 >= POKE_FAIL_BLOCKS:
            self.standing.raise_("failing", "book_pokes_queued" if queued else "book_poke_failing", head=head,
                                 since=self.fail_from, blocks=head - self.fail_from + 1, errors=errors, role=self.role,
                                 why="no check goes out: a stop reached in these blocks is not carried out")
        return False

    def _read_price(self, head: int) -> int:
        self.price = int(self.nodes.call("eth_gasPrice"), 16)
        self.price_at = head
        return self.price

    def _price_now(self, head: int) -> None:
        """The gas price, read again every 10 blocks after the check; a failed read keeps the last one (or the block's
        base fee)."""
        if head - self.price_at >= 10:
            try:
                self._read_price(head)
            except Exception as exc:
                self.price_at = head  # tried again ten blocks on; the last price stands meanwhile
                self._say("book_gas_price_unread", error=str(exc)[:120], kept=self.price_now())

    # ── after the check ──────────────────────────────────────────────────────────────

    def _landed(self, head: int) -> None:
        """Each lane's checks in a block let go by the chain's count of the key, one call a lane; the newest one let go is
        read for its result. A reverted one is an alarm. A check waiting long is one too."""
        now = time.monotonic()
        for lane in self.lanes:
            lane.counted = None
            if not lane.in_flight or lane.counted_for >= head:
                continue  # once a head
            lane.counted_for = head
            try:
                lane.counted = lane.sender.count("latest")
            except Exception as exc:
                self._say("book_poke_count_unread", error=str(exc)[:120])
                continue
            done = [f for f in lane.in_flight if f[0] < lane.counted]
            left = [f for f in lane.in_flight if f[0] >= lane.counted and not lane.sender.is_gone(f[0])]
            if done:
                nonce, at, _sent, tx = done[-1]
                r = None
                if now - lane.status_at >= POKE_STATUS_EVERY_S or self.standing.on("reverted"):
                    lane.status_at = now
                    try:
                        r = lane.sender.receipt(nonce, [tx])
                    except Exception as exc:
                        self._say("book_poke_receipt_unread", error=str(exc)[:120])
                for f in done:
                    lane.sender.confirm(f[0])
                if r and int(r["status"], 16) != 1:
                    self.standing.raise_("reverted", "book_poke_reverted", lane=lane.name, tx=r.get("transactionHash"),
                                         seen=at, block=int(r["blockNumber"], 16), role=self.role,
                                         then="the next checks go with a quarter more gas, the book's read gas read again")
                    if self.on_revert is not None:
                        self.on_revert(head)
                elif r:
                    self.gas_used = int(r.get("gasUsed", "0x0"), 16) or self.gas_used
                    self.standing.clear("reverted", "book_poke_lands_again", lane=lane.name)
            lane.in_flight = left
            key = f"stuck:{lane.name}"
            if left and now - left[0][2] >= POKE_STUCK_S:
                self.standing.raise_(key, "book_poke_stuck", lane=lane.name, nonce=left[0][0], seen=left[0][1],
                                     waiting=len(left), seconds=int(now - left[0][2]), role=self.role,
                                     why="the key's check is not in a block: it is signed again with more fee, and the "
                                         "next checks go through the next key")
            else:
                self.standing.clear(key, "book_poke_unstuck", lane=lane.name)

    def _view(self, sig: str, out: list[str]):
        raw = self.nodes.call("eth_call", [{"to": self.book, "data": "0x" + c.selector(sig).hex()}, "latest"])
        return abi_decode(out, bytes.fromhex(raw[2:]))

    def _liveness(self, head: int) -> None:
        """Once a head: `watched()` and `lastPokeBlock()`. Blocks going by unchecked while the book watches an asset are
        an alarm, again every `ALARM_EVERY_S` while they last; so is no read of either for `VIEWS_BLIND_S` seconds."""
        if self.read_for >= head:
            return
        try:
            watched = len(self._view("watched()", ["uint32[]"])[0])
            last = int(self._view("lastPokeBlock()", ["uint64"])[0])
        except Exception as exc:
            blind_for = time.monotonic() - self.views_ok_at
            if blind_for >= VIEWS_BLIND_S:
                if self.takeover:
                    self.last_poke = None  # not known: the watch takes it as behind
                self.standing.raise_("views", "book_views_unread", seconds=int(blind_for), error=str(exc)[:160],
                                     role=self.role,
                                     why="the book's watched() and lastPokeBlock() cannot be read: its lag is not known; "
                                         "checks go out all the same")
            raise
        self.views_ok_at = time.monotonic()
        self.standing.clear("views", "book_views_read_again", head=head)
        self.watched_n, self.last_poke, self.read_for = watched, last, head
        lag = head - last
        if self.watched_n and lag > self.lag_alarm:
            self.standing.raise_("lag", "book_poke_lagging", head=head, last_poke=last, lag=lag,
                                 watched=self.watched_n, role=self.role,
                                 why="blocks go by and the book is not checked: a stop reached in them is not carried out")
        else:
            self.standing.clear("lag", "book_poke_caught_up", head=head, last_poke=last)

    def _no_price(self, head: int) -> None:
        """The book's `NoPrice` events every `NO_PRICE_EVERY_BLOCKS` blocks: a check that could not read an asset's oracle
        logs a zero, and no stop or take of that asset is touched in that block. That is no `Gap`, the checks go on and
        the lag is nil: so an asset with `NO_PRICE_ALARM_BLOCKS` blocks of it in a row is an alarm of its own, again while
        it lasts (the read gas too low for the precompile, most likely: `ops/deploy_sim.py read-gas`)."""
        if head - self.no_price_to < NO_PRICE_EVERY_BLOCKS and self.no_price_to:
            return
        lo = self.no_price_to + 1 if self.no_price_to else max(1, head - NO_PRICE_EVERY_BLOCKS + 1)
        logs = self.nodes.call("eth_getLogs", [{"address": [self.book], "topics": [[NO_PRICE]], "fromBlock": hex(lo),
                                                "toBlock": hex(head)}])
        self.no_price_to = head
        seen: dict[int, list[int]] = {}
        for l in logs:
            seen.setdefault(int(l["topics"][1], 16), []).append(int(l["blockNumber"], 16))
        for asset in list(self.no_price_runs) + [a for a in seen if a not in self.no_price_runs]:
            blocks = sorted(set(seen.get(asset, [])))
            first, last = self.no_price_runs.get(asset, (None, None))
            for b in blocks:
                first, last = (first, b) if last is not None and b <= last + 1 else (b, b)
            # A run ends with a later check that logged a price: `lastPokeBlock()` past its last NoPrice.
            if last is None or (self.last_poke is not None and self.last_poke > last):
                if asset in self.no_price_runs:
                    del self.no_price_runs[asset]
                    self.standing.clear(f"no_price:{asset}", "book_price_read_again", asset=asset, head=head)
                continue
            self.no_price_runs[asset] = (first, last)
            if last - first + 1 >= NO_PRICE_ALARM_BLOCKS:
                self.standing.raise_(f"no_price:{asset}", "book_no_price", asset=asset, blocks=last - first + 1,
                                     first=first, last=last, role=self.role,
                                     why="the book's check logs no price for this asset: its stops and takes are not "
                                         "touched in these blocks; check the book's read gas (ops/deploy_sim.py read-gas)")

    def _sweep(self, head: int) -> None:
        """Each lane's late checks signed again; the oldest at once when its fee cap is under the newest block's base
        fee (it cannot land, and every later check of the key waits behind it)."""
        basefee = int(getattr(self.nodes, "basefee", 0) or 0)
        for lane in self.lanes:
            if basefee and lane.in_flight:
                with lane.sender.lock:
                    e = lane.sender.ledger.get(lane.in_flight[0][0])
                    if e is not None and e.get("tx") and int(e["tx"]["maxFeePerGas"]) < basefee:
                        e["at"] = -1e18
            lane.sender.sweep(counted=lane.counted)

    def _balance(self, head: int) -> None:
        """Each key's money every `BALANCE_EVERY_S`, and how many blocks and hours of checks it pays at the last price."""
        for lane in self.lanes:
            now = time.monotonic()
            if now - lane.balance_at < BALANCE_EVERY_S:
                continue
            lane.balance_at = now
            balance = int(self.nodes.call("eth_getBalance", [lane.sender.address, "latest"]), 16)
            per = max(self.gas_used or self.gas(self.watched_n), 1) * max(self.price_now(), 1)
            blocks = balance // per
            hours = round(blocks * BLOCK_S / 3600, 1)
            lane.runway = {"balance_wei": balance, "blocks": blocks, "hours": hours}
            key = f"low:{lane.name}"
            if hours < self.runway_hours:
                self.standing.raise_(key, "book_key_low", every=LOW_ALARM_EVERY_S, lane=lane.name,
                                     address=lane.sender.address, **lane.runway, role=self.role,
                                     why=f"the key pays fewer than {self.runway_hours:g} hours of checks: add money")
            else:
                self.standing.clear(key, "book_key_funded", lane=lane.name, **lane.runway)

    def _watch_alive(self, head: int) -> None:
        """The book's own watch, when one is named (`watch_address`): its key sends at least once every `HEARTBEAT_S`, so
        a count of its key that has not moved for `WATCH_SILENT_S` is an alarm (`book_watch_silent`)."""
        if self.watch_address is None:
            return
        now = time.monotonic()
        if now - self.watch_read_at < WATCH_READ_EVERY_S:
            return
        self.watch_read_at = now
        n = int(self.nodes.call("eth_getTransactionCount", [self.watch_address, "latest"]), 16)
        if n != self.watch_nonce:
            if self.watch_nonce is not None:
                self.standing.clear("watch_silent", "book_watch_alive_again", address=self.watch_address)
            self.watch_nonce, self.watch_seen_at = n, now
        elif now - self.watch_seen_at >= WATCH_SILENT_S:
            self.standing.raise_("watch_silent", "book_watch_silent", every=LOW_ALARM_EVERY_S,
                                 address=self.watch_address, seconds=int(now - self.watch_seen_at),
                                 why="the book's own watch has sent nothing for two of its heartbeats: it may be down")

    # ── running ──────────────────────────────────────────────────────────────────────

    def in_flight(self) -> int:
        return sum(len(l.in_flight) for l in self.lanes)

    def fields(self) -> dict:
        return {"head": self.head, "last_poke": self.last_poke, "watched": self.watched_n,
                "pokes_in_flight": self.in_flight(), "runway": {l.name: l.runway for l in self.lanes}}

    def run(self, poll: float, stop: threading.Event | None = None) -> None:
        """Forever (or until `stop`): a look every `poll` seconds."""
        stop = stop or threading.Event()
        self.threaded = True
        log("book_poker_started", role=self.role, node=sf.host_of(self.nodes.url), lanes=[l.name for l in self.lanes],
            takeover=self.takeover)
        while not stop.is_set():
            until = time.monotonic() + poll
            try:
                self.tick()
            except Exception as exc:
                self._say("book_poker_failed", error=str(exc)[:200])
            stop.wait(max(0.0, until - time.monotonic()))


class BookLoop(sf.StopLoop):
    """The stop book of the keeper's factory, every block (see the module's text). `keeper` is the SimKeeper whose pools
    and accounts are watched; `wallet` and `sender` are None for a watch that sends nothing. The keeper's thread, the
    alarm, the journal of prices and the key's sender are the stops' loop's (`stop_filler.StopLoop`)."""

    def __init__(self, keeper, node, book: str, *, reader=None, settle_gas: int = SETTLE_GAS, poke_node=None,
                 lanes: list[Lane] | None = None, state_path: pathlib.Path | None = None,
                 runway_hours: float = RUNWAY_HOURS, watch_address: str | None = None, **kw):
        super().__init__(keeper, node, **kw)
        self.book = to_checksum_address(book)
        self.reader = reader or node  # the keeper's thread reads through its own
        self.settle_gas_default = settle_gas
        self.holders: set[str] = set()  # exams with an entry in the book, or one set since the last read
        self.ever_held: set[str] = set()  # exams that held one, until they end: the ones a gap is judged on
        self.changes: dict[tuple[str, int, bool], int] = {}  # a change asked of an entry -> the block it is in force
        self.watched_n = 0  # how many assets the book watched at the last read
        self.watched_at: dict[int, int] = {}  # block -> how many it watched then, the last CATCHUP blocks
        self.poke_floor = 0  # the gas of every poke after one reverted, up to block `poke_floor_until`
        self.poke_floor_until = 0
        self.settles: dict[str, tuple[str, int, int, int]] = {}  # exam -> (tx, nonce, head, gas)
        self.settle_gas: dict[str, int] = {}  # exam -> the gas its next settle goes with
        self.settle_refused: dict[str, int] = {}  # exam -> the head its settle was last refused at
        self.settle_reverts: dict[str, int] = {}  # exam -> its settles reverted in their block in a row
        self.touch_from: dict[str, int] = {}  # exam -> the block of its first touch not carried out, as last read
        self.unread_from: dict[str, int] = {}  # exam -> the head its touches have failed to read since
        self.precompile_gas = 2 * PRECOMPILE_GAS
        self.gaps = 0
        self.gap_blocks = 0
        self.gap_jobs: list[dict] = []  # gaps whose blocks are still to be read back (`_judge_gaps`)
        self.backoff: dict[str, int] = {}  # exam -> the block before which no settle goes (`FAIL_BACKOFF_BLOCKS`)
        self.failed_said: set[str] = set()  # exams whose failed fill was raised, until one of theirs fills
        self.stuck_said: int | None = None
        self.standing = Standing(self.alarm)
        # The checks go only from keys of their own: the keeper's key carries its settles and fills, and one of those
        # lost or under a basefee would hold every check behind it in the same nonces.
        self.poker = Poker(self.book, poke_node or node, lanes or [], self.alarm, gas=self._poke_gas,
                           wants=lambda: bool(self.holders), tip_wei=self.tip, runway_hours=runway_hours,
                           on_revert=self._poke_reverted, watch_address=watch_address)
        self.poker_thread: threading.Thread | None = None
        self.probed: set[str] = set()  # the keeper's exams whose entries were read once since the start (`_seed`)
        self.state_path = state_path
        self.saved: dict | None = None
        self.state_lock = None
        self._load()

    # ── its state on disk ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """The last block read and the gaps still to read back, as the loop left them."""
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_lock = open(self.state_path.with_name(self.state_path.name + ".lock"), "a")
        try:
            fcntl.flock(self.state_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.state_lock.close()
            raise SystemExit(f"{self.state_path} is kept by another process: give this one its own --book-state") from None
        if not self.state_path.exists():
            return
        try:
            saved = json.loads(self.state_path.read_text())
            if to_checksum_address(saved["book"]) != self.book:
                log("book_state_other_book", path=str(self.state_path), why="kept for another book; started afresh")
                return
            self.block = int(saved["block"])
            self.gaps, self.gap_blocks = int(saved.get("gaps", 0)), int(saved.get("gap_blocks", 0))
            self.gap_jobs = [{"from": int(j["from"]), "to": int(j["to"]), "next": int(j["next"]), "entries": None,
                              "assets": None} for j in saved.get("gap_jobs", [])]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.alarm("book_state_unread", path=str(self.state_path), error=str(exc)[:200],
                       why="the loop starts at the head: a gap written while it was down is not read")
            return
        log("book_state_loaded", block=self.block, gap_jobs=len(self.gap_jobs))

    def close(self) -> None:
        """Lets go of the state's lock (a process that ends lets go of it by itself)."""
        if self.state_lock is not None:
            self.state_lock.close()
            self.state_lock = None

    def _save(self) -> None:
        if self.state_path is None:
            return
        state = {"book": self.book, "block": self.block, "gaps": self.gaps, "gap_blocks": self.gap_blocks,
                 "gap_jobs": [{"from": j["from"], "to": j["to"], "next": j["next"]} for j in self.gap_jobs]}
        if state == self.saved:
            return
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(state))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
            dirfd = os.open(self.state_path.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            self.saved = state
        except OSError as exc:
            self._say("book_state_unwritten", error=str(exc)[:200])

    # ── the checks' gas ──────────────────────────────────────────────────────────────

    def _poke_gas(self, watched: int) -> int:
        if self.poke_floor and self.poker.head > self.poke_floor_until:
            log("book_poke_floor_lifted", head=self.poker.head, then="the book's read gas again")
            self.poke_floor = 0
        return max(self.poke_floor, poke_gas(max(watched, 1), self.precompile_gas))

    def _poke_reverted(self, head: int) -> None:
        """A check reverted: the next go with a quarter more gas than the one before (never the most a small block holds
        at once: two checks of two senders must fit one small block), for `FLOOR_BLOCKS` blocks, and the book's read gas
        is read again."""
        needed = poke_gas(max(self.poker.watched_n, 1), self.precompile_gas)
        self.poke_floor = min(MAX_TX_GAS, max(self.poke_floor, needed) * 5 // 4)
        self.poke_floor_until = head + FLOOR_BLOCKS
        try:
            self.precompile_gas = precompile_gas_of(self.poker.nodes, self.book)
        except Exception as exc:
            log("book_read_gas_unread", error=str(exc)[:120])

    # ── what the keeper hands in ─────────────────────────────────────────────────────

    def reread(self, exam: str, call=None) -> None:
        """An exam's entries as the book holds them, read at one block: whether it has any, and each change asked of one
        with the block it comes into force. Called by the keeper's slow pass in its thread, through its own node."""
        call = call or self.reader.call
        at = int(call("eth_blockNumber"), 16)
        tag = hex(at)

        def read(to, sig, types, args, out):
            raw = call("eth_call", [{"to": to, "data": "0x" + c.encode_call(sig, types, args).hex()}, tag])
            return abi_decode(out, bytes.fromhex(raw[2:]))

        if read(exam, "result()", [], [], sf.RESULT)[0] != sf.ACTIVE:
            self.inbox.put(("ended", exam, at))
            return
        n = int(read(self.book, "entries(address)", ["address"], [exam], ["uint256"])[0])
        changes: dict[tuple[str, int, bool], int] = {}
        if n:
            for asset in read(exam, "rules()", [], [], [sk.RULES])[0][3]:
                for take in (False, True):
                    e = read(self.book, "entryOf(address,uint32,bool)", ["address", "uint32", "bool"],
                             [exam, int(asset), take], [ENTRY])[0]
                    if e[0] and e[8]:
                        changes[(exam, int(asset), take)] = int(e[8])
        self.inbox.put(("book", exam, at, n, changes))

    def apply(self, message: tuple) -> None:
        if message[0] == "ended":
            self._exam_over(message[1])
            return
        _, exam, at, n, changes = message
        if exam in self.ended:
            return
        if n:
            self.holders.add(exam)
            self.ever_held.add(exam)
        elif self.event_at.get(exam, 0) <= at:
            self.holders.discard(exam)
        for key in [k for k in self.changes if k[0] == exam and k not in changes and self.event_at.get(k, 0) <= at]:
            del self.changes[key]
        for key, at_block in changes.items():
            if self.event_at.get(key, 0) <= at:
                self.changes[key] = at_block

    def _live_exams(self) -> set[str]:
        """The keeper's exams not known to have ended: after a restart `holders` is empty until the keeper's slow pass,
        and a gap of that time is judged on these."""
        try:
            accounts = list(self.keeper.accounts)
        except Exception:
            return set()
        done = set(self.keeper.done)
        return {to_checksum_address(a) for a in accounts if a not in done and a not in self.ended}

    def _seed(self) -> None:
        """The keeper's exams whose entries were not read since the start, `SEED_PER_STEP` a step: one with an entry is
        a holder at once, so its touches are settled without waiting for the keeper's slow pass."""
        for exam in sorted(self._live_exams() - self.probed - self.holders)[:SEED_PER_STEP]:
            try:
                n = int(self._call(self.book, "entries(address)", ["address"], [exam], ["uint256"])[0])
            except c.NodeBusy:
                raise
            except Exception as exc:
                self._say("book_entries_unread", exam=exam, error=str(exc)[:120])
                continue
            self.probed.add(exam)
            if n:
                self.holders.add(exam)
                self.ever_held.add(exam)

    def _exam_over(self, exam: str) -> None:
        self.ended.add(exam)
        self.holders.discard(exam)
        self.ever_held.discard(exam)
        for kept in (self.settle_gas, self.settle_refused, self.backoff, self.settle_reverts, self.touch_from,
                     self.unread_from):
            kept.pop(exam, None)
        self.failed_said.discard(exam)
        for key in [k for k in self.changes if k[0] == exam]:
            del self.changes[key]

    # ── a step ───────────────────────────────────────────────────────────────────────

    def step(self, head: int) -> list[str]:
        """Everything up to block `head`: the logs, the gaps, the receipts, and the settles due. The check of the next
        block is the checks' thread's (`Poker`); with no such thread (a test, a step by hand) it is sent here, first.
        What it sent: "poke", and the exams it sent a settle for."""
        sent: list[str] = []
        if not self.poker.threaded and self.poker.tick(head):
            sent.append("poke")
        while True:
            try:
                message = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.apply(message)
        if head <= self.block:
            return sent
        if not self.block:
            self._read_watched(head)
        lo = head if not self.block else self.block + 1
        if head - lo + 1 > self.catchup:
            self.alarm("book_watch_lost", from_block=lo, to=head - self.catchup,
                       why=f"behind by more than {self.catchup} blocks: the book's gaps and the exams' events of these "
                           "blocks are not read; the exams are read again at the next pass")
            lo = head - self.catchup + 1
        accounts, pools = self.watched()
        for exam in set(self.keeper.done) & self.holders:
            self._exam_over(exam)
        self._read_watched(head)
        self._seed()
        for l in self._book_logs(lo, head):
            self._book_event(l, head)
        if accounts or pools:
            for block, logs in sorted(self._logs(accounts, pools, lo, head).items()):
                for l in logs:
                    self._exam_event(block, l)
        self.block = head
        self._save()
        self._receipts(head)
        sent += self._settle_due(head)
        self._check_stuck()
        self._judge_gaps()
        self._save()
        return sent

    def _call(self, to: str, sig: str, types, args, out, tag="latest"):
        raw = self.node.call("eth_call", [{"to": to, "data": "0x" + c.encode_call(sig, types, args).hex()}, tag])
        return abi_decode(out, bytes.fromhex(raw[2:]))

    def _read_watched(self, head: int) -> None:
        """How many assets the book watches now; kept a while by block, for the gaps."""
        self.watched_n = len(self._call(self.book, "watched()", [], [], ["uint32[]"])[0])
        self.watched_at[head] = self.watched_n
        for b in [b for b in self.watched_at if b < head - self.catchup]:
            del self.watched_at[b]

    def _gas_price(self, head: int) -> int:
        """The settles' gas price: read every 10 blocks, the last one kept when a read fails."""
        try:
            return super()._gas_price(head)
        except Exception as exc:
            if not self.gas_price:
                raise
            self.gas_read_at = head
            self._say("book_gas_price_unread", error=str(exc)[:120], kept=self.gas_price)
            return self.gas_price

    def _book_logs(self, lo: int, hi: int) -> list[dict]:
        logs = self._get_logs([self.book], BOOK_TOPICS, lo, hi)
        return sorted(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))

    def _book_event(self, l: dict, head: int) -> None:
        topic = l["topics"][0]
        block = int(l["blockNumber"], 16)
        if topic == GAP:
            frm, to = abi_decode(["uint256", "uint256"], bytes.fromhex(l["data"][2:]))
            self._gap(int(frm), int(to), block)
        elif topic == NO_PRICE:
            self._say("book_no_price", asset=int(l["topics"][1], 16), block=block,
                      note="nothing on this asset is touched in this block")
        elif topic == READ_GAS_SET:
            oracle, bbo = abi_decode(["uint256", "uint256"], bytes.fromhex(l["data"][2:]))
            was, self.precompile_gas = self.precompile_gas, int(oracle) + int(bbo)
            log("book_read_gas_set", block=block, oracle=int(oracle), bbo=int(bbo), was=was,
                then="the next checks go with the gas of the new reads")
        elif topic == FIRED:
            exam = to_checksum_address("0x" + l["topics"][1][-40:])
            asset = int(l["topics"][2], 16)
            take, px, touch, result = abi_decode(["bool", "uint64", "uint256", "uint8"], bytes.fromhex(l["data"][2:]))
            fields = {"exam": exam, "asset": asset, "kind": "take" if take else "stop", "px": int(px),
                      "touch_block": int(touch), "settled": block, "late": block - int(touch), "tx": l["transactionHash"]}
            if result == STARVED:
                was = self.settle_gas.get(exam, self.settle_gas_default)
                self.settle_gas[exam] = MAX_TX_GAS
                if was >= MAX_TX_GAS:
                    self._failed_fill(exam, block, "book_settle_starved", fields, gas=was,
                                      why="a fill ran out of gas with the most a small block holds; the stop waits")
                else:
                    log("book_settle_starved", **fields, gas=was, then=f"the next settle goes with {MAX_TX_GAS}")
            elif result == FAILED:
                self._failed_fill(exam, block, "book_fill_failed", fields,
                                  why="the exam refused the fill; the entry stays and the exam can do nothing until it fills")
            else:
                self.backoff.pop(exam, None)
                self.failed_said.discard(exam)
                log("book_fired", **fields, result=RESULT_NAME[result])

    def _failed_fill(self, exam: str, block: int, event: str, fields: dict, **more) -> None:
        """A fill that will fail again if sent again as it was: an alarm once for the exam, a line after, and its next
        settle `FAIL_BACKOFF_BLOCKS` later, not in every block."""
        self.backoff[exam] = block + FAIL_BACKOFF_BLOCKS
        if exam in self.failed_said:
            log(event, **fields, **more, note="its alarm was raised already", next_from=self.backoff[exam])
            return
        self.failed_said.add(exam)
        self.alarm(event, **fields, **more, next_from=self.backoff[exam])

    def _gap(self, frm: int, to: int, block: int) -> None:
        """A run of blocks the book did not check. One while it watched nothing is no loss; one while it watched an
        asset is this loop's failure, and a stop the price reached in it is not carried out."""
        watched = self._watched_before(frm)
        if watched == 0:
            log("book_gap_idle", from_block=frm, to=to, why="the book watched no asset")
            return
        self.gaps += 1
        self.gap_blocks += to - frm + 1
        self.alarm("book_gap", from_block=frm, to=to, blocks=to - frm + 1, seen=block, gaps=self.gaps,
                   gap_blocks=self.gap_blocks, watched=watched if watched is not None else "not read",
                   why="blocks the book did not check: a stop reached only in them is not carried out")
        lo = max(frm, to - GAP_READ_BLOCKS + 1)
        if lo > frm:
            log("book_gap_unread", from_block=frm, to=lo - 1, why=f"only the last {GAP_READ_BLOCKS} blocks are read back")
        # Read back later in the step, a few blocks a step (`_judge_gaps`): this one is counted and said once, here.
        self.gap_jobs.append({"from": frm, "to": to, "next": lo, "entries": None, "assets": None})

    def _watched_before(self, frm: int) -> int | None:
        """How many assets the book watched after block `frm - 1`: read at that block where the node keeps it, else as
        this loop last saw it. None: not known (taken as some)."""
        try:
            return len(self._call(self.book, "watched()", [], [], ["uint32[]"], hex(frm - 1))[0])
        except Exception:  # a busy node too: the gap is said now, as some, not read again in a later step
            seen = [b for b in self.watched_at if b <= frm - 1]
            return self.watched_at[max(seen)] if seen else None

    def _judge_gaps(self) -> None:
        """The gaps said and not yet read back, `GAP_BLOCKS_PER_STEP` blocks a step, after the step's check went out:
        each block's oracle prices to the journal, and every entry of the exams with one (read once a gap) judged
        against them; a touch there is an alarm. A busy node leaves the rest for the next step."""
        budget = GAP_BLOCKS_PER_STEP
        while self.gap_jobs and budget > 0:
            job = self.gap_jobs[0]
            try:
                if job["entries"] is None:
                    job["assets"], job["entries"] = self._gap_entries(job["from"], job["to"])
                while job["next"] <= job["to"] and budget > 0:
                    self._gap_block(job, job["next"])
                    job["next"] += 1
                    budget -= 1
            except c.NodeBusy as exc:
                self._say("book_gap_read_later", from_block=job["from"], to=job["to"], at=job["next"], error=str(exc)[:120])
                return
            except Exception as exc:
                self.alarm("book_gap_unjudged", from_block=job["from"], to=job["to"], error=str(exc)[:200],
                           why="the entries could not be read: payout_review judges the gap from the journal of prices")
                self.gap_jobs.pop(0)
                continue
            if job["next"] > job["to"]:
                self.gap_jobs.pop(0)

    def _gap_entries(self, frm: int, to: int) -> tuple[list[int], dict]:
        """The book as it stood in the gap: the assets it watched and every entry of the exams that held one, at the
        block before the gap and at its last block (where the node keeps them), and now. The transaction that wrote the
        `Gap` may have closed the last position of an asset, so what is left now alone would judge nothing. Each entry
        keeps every form it was found in (a change set inside the gap): a block is touched if one of them reached."""
        tags = [hex(frm - 1), hex(to), "latest"]
        assets: set[int] = set()
        unread: list[str] = []
        for tag in tags:
            try:
                assets |= {int(a) for a in self._call(self.book, "watched()", [], [], ["uint32[]"], tag)[0]}
            except c.NodeBusy:
                raise
            except Exception as exc:
                if tag == "latest":
                    raise
                unread.append(f"watched at {int(tag, 16)}: {str(exc)[:80]}")
        entries: dict[tuple[str, int, bool], list[tuple]] = {}
        for exam in sorted(self.holders | self.ever_held | self._live_exams()):
            for tag in tags:
                try:
                    if not int(self._call(self.book, "entries(address)", ["address"], [exam], ["uint256"], tag)[0]):
                        continue
                    for asset in sorted(assets):
                        for take in (False, True):
                            e = tuple(self._call(self.book, "entryOf(address,uint32,bool)", ["address", "uint32", "bool"],
                                                 [exam, asset, take], [ENTRY], tag)[0])
                            if e[0] and e not in entries.get((exam, asset, take), []):
                                entries.setdefault((exam, asset, take), []).append(e)
                except c.NodeBusy:
                    raise
                except Exception as exc:
                    if tag == "latest":
                        raise
                    unread.append(f"{exam} at {int(tag, 16)}: {str(exc)[:80]}")
        if unread:
            log("book_gap_state_unread", from_block=frm, to=to, unread=unread[:5],
                why="the node keeps no state of these blocks: judged on what was read")
        return sorted(assets), entries

    def _gap_block(self, job: dict, b: int) -> None:
        prices: dict[int, int] = {}
        for asset in job["assets"]:
            try:
                raw = self.node.call("eth_call", [{"to": sf.ORACLE_PX, "data": "0x" + abi_encode(
                    ["uint32"], [sf.px_index(asset)]).hex()}, hex(b)])
                prices[asset] = int(abi_decode(["uint64"], bytes.fromhex(raw[2:]))[0])
            except c.NodeBusy:
                raise
            except Exception as exc:
                self._say("book_gap_price_unread", block=b, asset=asset, error=str(exc)[:120])
        if self.journal is not None and prices:
            try:
                self.journal.add(b, prices)
            except OSError as exc:
                self._say("stop_journal_failed", error=str(exc)[:200])
        for (exam, asset, take), forms in list(job["entries"].items()):
            for e in forms:
                level, armed = phase(e, b)
                if level and b >= armed and asset in prices and reached(bool(e[4]), level, prices[asset]):
                    self.alarm("book_gap_touch", exam=exam, asset=asset, kind="take" if take else "stop", level=level,
                               block=b, oracle=prices[asset],
                               why="reached in a block the book did not check: not carried out; the payout review "
                                   "does not pass this exam as clean")
                    del job["entries"][(exam, asset, take)]  # said once: its first touch is the one that counts
                    break

    def _exam_event(self, block: int, l: dict) -> None:
        topic = l["topics"][0]
        exam = to_checksum_address(l["address"])
        if topic in (sf.STOPPED, sf.PASSED):
            self._exam_over(exam)
            return
        if exam in self.ended or topic not in (sk.TRIGGER_SET, sk.TRIGGER_CLEARED, *sk.TRIGGER_FILLS):
            return
        self.event_at[exam] = block
        if topic != sk.TRIGGER_SET:
            return
        asset = int(l["topics"][1], 16)
        take, _px, _sz, armed = abi_decode(["bool", "uint64", "uint64", "uint256"], bytes.fromhex(l["data"][2:]))
        key = (exam, asset, bool(take))
        self.event_at[key] = block
        self.holders.add(exam)
        self.ever_held.add(exam)
        # A first stop or a change of one: told apart by the book once it is in force (`_settle_due`).
        self.changes[key] = int(armed)

    # ── settles ──────────────────────────────────────────────────────────────────────

    def _settle_due(self, head: int) -> list[str]:
        """The settles due at `head`: each exam with a touch not yet carried out, and each with a change in force."""
        due: dict[str, str] = {}
        for key, at_block in list(self.changes.items()):
            if at_block > head:
                continue
            exam, asset, take = key
            try:
                e = self._call(self.book, "entryOf(address,uint32,bool)", ["address", "uint32", "bool"],
                               [exam, asset, take], [ENTRY])[0]
            except c.NodeBusy:
                raise
            except Exception as exc:
                self._say("book_entry_unread", exam=exam, error=str(exc)[:120])
                continue
            next_at = int(e[8])
            if not e[0] or not next_at:
                del self.changes[key]  # a first stop, gone, or already in force
            elif next_at > head:
                self.changes[key] = next_at  # replaced by a later change
            else:
                due.setdefault(exam, "change")
        for exam in sorted(self.holders):
            if exam in due:
                continue
            try:
                touches = self._call(self.book, "touches(address)", ["address"], [exam], [f"{TOUCH}[]"])[0]
            except c.NodeBusy:
                raise
            except Exception as exc:
                since = self.unread_from.setdefault(exam, head)
                self._say(f"book_touches_unread:{exam}", exam=exam, error=str(exc)[:120])
                if head - since >= TOUCH_ALARM_BLOCKS:
                    self.standing.raise_(f"unread:{exam}", "book_touches_unread", exam=exam, since=since,
                                         blocks=head - since, error=str(exc)[:160],
                                         why="the exam's touches cannot be read: a touch of it is not settled")
                continue
            if self.unread_from.pop(exam, None) is not None:
                self.standing.clear(f"unread:{exam}", "book_touches_read_again", exam=exam)
            self._touch_standing(exam, touches, head)
            if touches and exam not in self.settles:
                due[exam] = f"{len(touches)} touched, the first in block {int(touches[0][5])}"
        sent = []
        for exam, why in sorted(due.items()):
            if exam in self.settles or exam in self.keeper.done or self.backoff.get(exam, 0) > head:
                continue
            if self._settle(exam, head, why):
                sent.append(exam)
        return sent

    def _touch_standing(self, exam: str, touches, head: int) -> None:
        """A touch not carried out `TOUCH_ALARM_BLOCKS` blocks after its block is an alarm, whatever holds it (a settle
        refused, reverting, starved, waiting, not sent): again every `ALARM_EVERY_S` while it stands."""
        key = f"touch:{exam}"
        if not touches:
            if self.touch_from.pop(exam, None) is not None:
                self.standing.clear(key, "book_touch_carried_out", exam=exam, head=head)
            return
        first = int(touches[0][5])
        self.touch_from[exam] = first
        if head - first > TOUCH_ALARM_BLOCKS:
            self.standing.raise_(key, "book_touch_standing", exam=exam, first_touch_block=first, blocks=head - first,
                                 touches=len(touches), in_flight=exam in self.settles,
                                 backoff_to=self.backoff.get(exam),
                                 why="a touched stop or take is not carried out: the exam's state is not what it owes")

    def _settle(self, exam: str, head: int, why: str) -> bool:
        gas = self.settle_gas.get(exam, self.settle_gas_default)
        data = c.encode_call("settle(address)", ["address"], [exam])
        if self.wallet is None or self.sender is None:
            self._say(f"would_settle:{exam}", exam=exam, why=why, block=head)
            return False
        try:
            # A settle the book would refuse (one with nothing to carry out, once the book refuses those) costs gas and
            # does nothing: tried first, with the gas it will be sent with.
            self.node.call("eth_call", [{"from": self.sender.address, "to": self.book, "data": "0x" + data.hex(),
                                         "gas": hex(gas)}, "latest"])
        except c.NodeBusy:
            raise
        except Exception as exc:
            if NO_PROGRESS in str(exc).lower() and gas < MAX_TX_GAS:
                # Less than one step's gas (`NoProgress`): the next try goes with the most a small block holds.
                self.settle_gas[exam] = MAX_TX_GAS
                log("book_settle_short", exam=exam, gas=gas, then=f"sent again with {MAX_TX_GAS}")
                return False
            if self.settle_refused.get(exam) != head:
                self.settle_refused[exam] = head
                self._say(f"book_settle_refused:{exam}", exam=exam, error=str(exc)[:160], why=why)
            return False
        try:
            tx, nonce = self.sender.send(self.book, data, gas, self._gas_price(head), self.tip, bump=True,
                                         resend_after=POKE_RESEND_S)
        except Exception as exc:
            log("book_settle_failed", exam=exam, error=str(exc)[:200], then="sent again after the next block")
            return False
        self.settles[exam] = (tx, nonce, head, gas)
        log("book_settle_sent", exam=exam, tx=tx, nonce=nonce, gas=gas, why=why, seen=head)
        return True

    # ── receipts ─────────────────────────────────────────────────────────────────────

    def _receipt(self, tx: str, nonce: int):
        """The receipt of one of the loop's transactions (under any of its hashes): a dict, None for none yet, or "gone"
        (never to land)."""
        rcpt = self.sender.receipt(nonce, [tx])
        if rcpt:
            self.sender.confirm(nonce)
            return rcpt
        return "gone" if self.sender.is_gone(nonce) else None

    def _receipts(self, head: int) -> None:
        if self.sender is None:
            return
        try:
            for exam, (tx, nonce, at, gas) in list(self.settles.items()):
                r = self._receipt(tx, nonce)
                if r is None:
                    continue
                del self.settles[exam]
                if r == "gone":
                    log("book_settle_lost", exam=exam, tx=tx, seen=at)
                elif int(r["status"], 16) != 1:
                    # Reverted in the block though it ran in the trial: most likely `NoProgress`, the block's state
                    # dearer than the trial's. The next one goes with the most a small block holds; one reverting
                    # again and again is an alarm and waits, not sent every block.
                    self.settle_gas[exam] = MAX_TX_GAS
                    n = self.settle_reverts[exam] = self.settle_reverts.get(exam, 0) + 1
                    fields = {"exam": exam, "tx": tx, "seen": at, "gas": gas, "reverts": n}
                    if n >= SETTLE_REVERTS:
                        self._failed_fill(exam, int(r["blockNumber"], 16), "book_settle_reverting", fields,
                                          why=f"{n} settles in a row reverted in their block; the touch waits")
                    else:
                        log("book_settle_reverted", **fields, then=f"tried again with {MAX_TX_GAS}")
                else:
                    self.settle_reverts.pop(exam, None)
                    log("book_settled", exam=exam, tx=tx, seen=at, block=int(r["blockNumber"], 16),
                        gas_used=int(r.get("gasUsed", "0x0"), 16))
        except c.NodeBusy:
            raise
        except Exception as exc:
            self._say("book_receipt_unread", error=str(exc)[:120])

    def _check_stuck(self) -> None:
        """The key's oldest transaction, from its whole ledger, waiting for a block longer than `STUCK_S`: every later one
        waits behind it, the checks too. Said once for each oldest one."""
        if self.sender is None:
            return
        with self.sender.lock:
            if not self.sender.ledger:
                return
            nonce = min(self.sender.ledger)
            e = dict(self.sender.ledger[nonce])
        waited = time.monotonic() - e.get("since", e["at"])
        if waited >= STUCK_S and self.stuck_said != nonce:
            self.stuck_said = nonce
            self.alarm("send_stuck", nonce=nonce, tx=e["hash"], seconds=int(waited),
                       why="the key's oldest transaction is not in a block; every later one, the checks too, waits behind it")

    def alive_fields(self) -> dict:
        return {"holders": len(self.holders), "watched": self.watched_n, **self.poker.fields(),
                "settles_in_flight": len(self.settles), "gaps": self.gaps, "gap_blocks": self.gap_blocks}

    # ── running ──────────────────────────────────────────────────────────────────────

    def start_poker(self, poll: float, stop: threading.Event | None = None) -> threading.Thread:
        self.poker.threaded = True
        self.poker_poll = poll
        t = threading.Thread(target=self.poker.run, args=(poll, stop), name="book-checks", daemon=True)
        t.start()
        self.poker_thread = t
        return t

    def keep_poker(self, stop: threading.Event | None = None) -> None:
        """The checks' thread is there and moving: one that ended is an alarm and is started again; one that has not
        looked for `STALL_S` seconds is an alarm."""
        t = self.poker_thread
        if t is None:
            return
        if not t.is_alive():
            self.alarm("book_poker_dead", head=self.poker.head, why="the checks' thread ended; it is started again")
            self.start_poker(self.poker_poll, stop)
            return
        stalled = time.monotonic() - self.poker.beat
        if stalled >= sf.STALL_S:
            self.standing.raise_("poker_stalled", "book_poker_stalled", seconds=int(stalled), head=self.poker.head,
                                 why="the checks' thread has not looked at the head: no check goes out")
        else:
            self.standing.clear("poker_stalled", "book_poker_moving_again", head=self.poker.head)

    def run(self, poll: float, stop: threading.Event | None = None, poke_poll: float = 0.25) -> None:
        """The checks' thread started beside this one, then, forever (or until `stop`): the head every `poll` seconds,
        fresh by its block's time, and a step for each new one; between them, the keeper's transactions. A head whose
        block is old keeps no heartbeat alive (`beat`)."""
        stop = stop or threading.Event()
        if self.poker_thread is None:
            self.start_poker(getattr(self, "poke_poll", poke_poll), stop)
        log("stop_loop_started", node=sf.host_of(self.node.url), tip_wei=self.tip, sends=self.sender is not None,
            journal=str(self.journal.path) if self.journal else None,
            flag=str(self.alarm.flag) if self.alarm.flag else None, book=self.book)
        while not stop.is_set():
            until = time.monotonic() + poll
            try:
                head = fresh_head(self.node)  # a head that stopped moving grows old and raises: no beat from it
                self.beat = time.monotonic()
                if head > self.block:
                    self.step(head)
                if self.failing_since is not None:
                    log("stop_loop_works_again", failed_for=int(time.monotonic() - self.failing_since))
                self.failing_since, self.failing_said = None, False
            except c.NodeBusy as exc:
                self._failed("stop_node_busy", exc)
            except Exception as exc:
                self._failed("stop_loop_failed", exc)
            self.keep_poker(stop)
            if time.monotonic() - self.alive_at >= sf.ALIVE_EVERY_S:
                self.alive_at = time.monotonic()
                log("stop_loop_alive", block=self.block, **self.alive_fields())
            self._served(until, stop)


def build(args, keeper, state, book: str, wallet=None, prefix: str = "") -> BookLoop:
    """The loop from the stops' settings (stop_filler.add_args), its nodes checked against the network."""
    url = args.stop_rpc or c.RPC_URL
    backup_url = getattr(args, "stop_backup_rpc", "") or os.environ.get("COLOSSEUM_RPC_BACKUP_URL", "")
    stale_s = float(getattr(args, "stale_s", STALE_S) or STALE_S)
    first = checked_node(url)
    backup = None
    if backup_url and backup_url.rstrip("/") != url.rstrip("/"):
        backup = checked_node(backup_url)
    else:
        backup_url = ""
    node = Nodes(first, backup, stale_s=stale_s)
    reader = Nodes(sf.Node(url), sf.Node(backup_url) if backup_url else None, stale_s=stale_s)
    # The checks' thread: its own sessions, on its own node when one is given, with the same backup.
    poke_url = getattr(args, "poke_rpc", "") or url
    poke_node = Nodes(checked_node(poke_url, POKE_TIMEOUT_S) if poke_url != url else sf.Node(url, POKE_TIMEOUT_S),
                      sf.Node(backup_url, POKE_TIMEOUT_S) if backup_url and backup_url != poke_url else None,
                      stale_s=stale_s)
    journal_path = (state.parent / f"{prefix}prices-{args.deployment}.json") if args.price_journal is None else (
        pathlib.Path(args.price_journal) if args.price_journal else None)
    flag = (state.parent / f"{prefix}stops-{args.deployment}.alarm") if args.stop_alarm is None else (
        pathlib.Path(args.stop_alarm) if args.stop_alarm else None)
    book_state = getattr(args, "book_state", None)
    state_path = (state.parent / f"{prefix}book-{args.deployment}.json") if book_state is None else (
        pathlib.Path(book_state) if book_state else None)
    if args.stop_tip_gwei < 0:
        raise SystemExit("--stop-tip-gwei must not be negative")
    # The keeper's key: its settles and the keeper's fills, raised in fee when late as the checks are. The checks go
    # only from keys of their own (`--poke-wallet`): never the keeper's, whose other transactions would hold them.
    sender = poke_sender(wallet, node) if wallet is not None else None
    lanes = []
    names = [n.strip() for n in (getattr(args, "poke_wallet", "") or "").split(",") if n.strip()]
    if wallet is not None:
        for name in names:
            key = c.account(name)
            if to_checksum_address(key.address) == to_checksum_address(wallet.address):
                raise SystemExit(f"--poke-wallet {name} is the keeper's own key: the checks need keys of their own")
            lanes.append(Lane(name, poke_sender(key, poke_node)))
        if not lanes:
            if c.NETWORK.name == "mainnet":
                raise SystemExit("refusing to run on mainnet with a stop book and no key of the checks' own: name one "
                                 "or more in --poke-wallet (COLOSSEUM_POKE_WALLETS)")
            log("book_poke_no_key", alarm="WARNING",
                why="no --poke-wallet: this keeper sends no check; the book's blocks are left to anyone else's poke")
    loop = BookLoop(keeper, node, book, reader=reader, wallet=wallet, sender=sender, poke_node=poke_node, lanes=lanes,
                    state_path=state_path, runway_hours=float(getattr(args, "poke_runway_hours", RUNWAY_HOURS)),
                    watch_address=getattr(args, "book_watch_address", "") or None,
                    tip_wei=int(round(args.stop_tip_gwei * sf.GWEI)),
                    journal=sf.PriceJournal(journal_path) if journal_path else None, alarm=sf.Alarm(flag))
    if wallet is not None and not lanes:
        loop.alarm("book_poke_no_key", why="no key of the checks' own (--poke-wallet): this keeper sends no check")
    loop.poke_poll = float(getattr(args, "poke_poll", 0.25) or 0.25)
    loop.precompile_gas = precompile_gas_of(node, book)
    return loop


# Seconds a call of the checks' thread waits for its node: a check late by more is a block lost anyway.
POKE_TIMEOUT_S = 2.0


def checked_node(url: str, timeout: float = 5.0) -> sf.Node:
    """A node on the network's chain (and, on mainnet, the keeper's check of it)."""
    node = sf.Node(url, timeout)
    if c.NETWORK.name == "mainnet":
        core.check_node(url, c.CHAIN_ID)
    sf.check_chain(node)
    return node


def precompile_gas_of(node, book: str) -> int:
    """What the book gives its two precompile reads of an asset (`oracleGas`, `bboGas`, the operator's to change); the
    most it may give (`MAX_READ_GAS`) when the book does not answer. Read at the start; a check that reverted for want
    of gas raises every later one to the most (`poke_floor`)."""
    total = 0
    for name in ("oracleGas()", "bboGas()"):
        try:
            raw = node.call("eth_call", [{"to": book, "data": "0x" + c.selector(name).hex()}, "latest"])
            total += int(abi_decode(["uint256"], bytes.fromhex(raw[2:]))[0])
        except Exception:
            total += PRECOMPILE_GAS
    return total
