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
                     looks at the head every `--poke-poll` seconds, sends the check first and reads nothing before it but
                     the head (and the book's `watched()` while it watches nothing, so the first check goes in the very
                     block it is wanted). A send the node refused is tried again at the next look, inside the block; the
                     last gas price read stands in for one that cannot be read. Nothing else the loop reads (logs,
                     receipts, `touches`, the gaps) is in its way: a node refusing those costs them, never a check;
  a check that     → each check has the keeper's key or keys of its own (`--poke-wallet`), tried in turn; one with no
  is late            block after `POKE_RESEND_S` is signed again under its nonce with a quarter more fee;
  the watch of     → on every new head the thread reads `lastPokeBlock()` and `watched()`: blocks going by unchecked while
  the checks         the book watches an asset are an alarm (`book_poke_lagging`, again every `ALARM_EVERY_S` while they
                     last), and so are a node whose newest block is old (`book_node_stale`, the other node then used; both
                     old: `book_head_stale`), a head that cannot be read, checks that cannot be sent (`book_poke_failing`),
                     checks waiting for a block (`book_poke_stuck`, `book_pokes_queued`) and a key whose money pays fewer
                     hours of checks than `--poke-runway-hours` (`book_key_low`). `ops/book_watch.py` is the same watch
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
                     an alarm of its own. `ops/payout_review.py` does not pass such an exam's payout as clean: a touch
                     in a gap, gap blocks it could not read, or blocks since the last check not yet closed by a `Gap`
                     make its verdict not clean, with a stop of the payout drafted for the checker (reason 3);
  its state        → the last block read and the gaps still to read back, in a file of its own (`--book-state`): after a
                     restart the loop reads the book's events from there, so a `Gap` written while it was down is said;
  the key          → every transaction of the keeper's key goes out through one sender (`stop_filler.Sender`), from this
                     thread and the checks' thread: its own nonce, a transaction lost by the node sent again, and the
                     keeper's own sends queued to it;
  the nodes        → `--stop-rpc` (the keeper's node when not set) and a backup (`--stop-backup-rpc`, or the keeper's
                     COLOSSEUM_RPC_BACKUP_URL): a call the first refuses, times out on, answers with HTTP 401/403/404 or a
                     limit (-32005, -32007) goes to the second; a node that failed so is asked second for
                     `FAILOVER_BACK_S` seconds, not first every time.

What is left of `--stops` (COLOSSEUM_STOPS) with a stop book: nothing sends `fillTrigger` (an exam with a book refuses it,
`NotBook`), and the watch of reaches by the price is this loop's gaps. `--stops` keeps its meaning for a factory with no
book (the 998 rehearsal), on the same code as before.
"""

from __future__ import annotations

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
# Seconds a poke may wait for a block before it is signed again under its nonce with a quarter more fee.
POKE_RESEND_S = 2.5
# Seconds a key's oldest poke may wait for a block before it is an alarm (`book_poke_stuck`).
POKE_STUCK_S = 10.0
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
    """A node answers, and its newest block is older than it may be: it has stopped following the chain."""


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


class Nodes:
    """A node and its backup as one: a call the first fails on (busy, a timeout, HTTP 401/403/404, a limit) goes to the
    second. The node that failed is then asked second for `FAILOVER_BACK_S` seconds, so a dead first node does not cost
    every call its timeout. An answer, an error in it included (a revert, `nonce too low`), is the answer: it never
    goes on. `head()` is the newest block of a node whose newest block is fresh (`stale_s`)."""

    def __init__(self, first: sf.Node, backup: sf.Node | None = None, *, stale_s: float = STALE_S, standing=None):
        self.first = first
        self.backup = backup
        self.nodes = [first] + ([backup] if backup is not None else [])
        self.url = first.url
        self.stale_s = stale_s
        self.prefer = 0
        self.prefer_since = 0.0
        self.standing = standing  # a Standing, for a node found stale while the other one serves

    def _order(self) -> list[int]:
        if self.prefer and time.monotonic() - self.prefer_since >= FAILOVER_BACK_S:
            self.prefer = 0
        return [self.prefer] + [i for i in range(len(self.nodes)) if i != self.prefer]

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
        that answers an old block is failed over like one that does not answer, and said (`book_node_stale`)."""
        order = self._order()
        stale: list[tuple[str, str]] = []
        last: Exception | None = None
        for i in order:
            try:
                number, age = block_age(self.nodes[i])
            except Exception as exc:
                last = exc
                self._failed(i)
                continue
            if age > self.stale_s:
                stale.append((NAMES[i], f"block {number}, {int(age)} s old"))
                self._failed(i)
                continue
            if self.standing is not None:
                for name, seen in stale:
                    self.standing.raise_(f"node_stale:{name}", "book_node_stale", node=name, seen=seen,
                                         why="the node answers an old block: the other one is used")
                self.standing.clear(f"node_stale:{NAMES[i]}", "book_node_fresh_again", node=NAMES[i])
            return number
        if stale:
            raise NodeStale("every node's newest block is old: " + "; ".join(f"{n} {s}" for n, s in stale))
        raise last if last is not None else RuntimeError("no node")


def block_age(node) -> tuple[int, float]:
    """A node's newest block and how many seconds old it is by this machine's clock."""
    b = node.call("eth_getBlockByNumber", ["latest", False])
    return int(b["number"], 16), time.time() - int(b["timestamp"], 16)


def fresh_head(node, stale_s: float = STALE_S) -> int:
    """The newest block of `node` (a `Nodes`, or one node), raised `NodeStale` when it is older than `stale_s`."""
    if callable(getattr(node, "head", None)):
        return node.head()
    number, age = block_age(node)
    if age > stale_s:
        raise NodeStale(f"the node's newest block is {int(age)} s old at block {number}")
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


class Lane:
    """One key that sends the book's checks: its sender and the checks it has waiting for a block."""

    def __init__(self, name: str, sender: sf.Sender):
        self.name = name
        self.sender = sender
        self.in_flight: list[tuple[int, int, float]] = []  # (nonce, head it was sent after, monotonic time sent)
        self.balance_at = -1e18
        self.runway: dict | None = None


class Poker:
    """The book's checks, sent from a thread of their own (see the module's text), and the watch of them.

    `lanes`: the keys that send, tried in turn (none: it watches and sends nothing). `gas(watched)`: the gas of a check.
    `wants()`: a reason to check besides what `watched()` said (an exam holding an entry). `takeover`: with 0 it checks
    every block; with N it checks only while the book is N blocks behind or more, and then every block for `hold`
    blocks after the last time it was (the second sender, `ops/book_watch.py`). `on_revert(head)`: a check reverted."""

    def __init__(self, book: str, nodes, lanes, alarm, *, gas=None, wants=None, takeover: int = 0,
                 hold: int = 600, lag_alarm: int = LAG_ALARM_BLOCKS, tip_wei: int = 0,
                 runway_hours: float = RUNWAY_HOURS, on_revert=None, role: str = "keeper"):
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
        self.threaded = False
        self.head = 0
        self.poked_for = 0
        self.watched_n = 0
        self.last_poke: int | None = None
        self.read_for = 0
        self.taken_until = 0
        self.price = 0
        self.price_at = -10**9
        self.gas_used = 0
        self.fail_from: int | None = None
        self.blind_since: float | None = None
        self.beat = time.monotonic()
        self.quiet_until: dict[str, float] = {}

    # ── a look ───────────────────────────────────────────────────────────────────────

    def tick(self, head: int | None = None) -> bool:
        """One look: the head (fresh), the check of the next block sent if one is due, then the rest, each on its own.
        Whether a check went out."""
        self.beat = time.monotonic()
        if head is None:
            try:
                head = fresh_head(self.nodes)
            except Exception as exc:
                self._blind(exc)
                return False
        self._sees(head)
        sent = False
        if head > self.poked_for:
            if self.takeover or (self.watched_n == 0 and not self.wants()):
                # Nothing known to watch: the book read first, so the first check goes in the block it is wanted. The
                # second sender reads `lastPokeBlock` first: it checks only behind the first.
                self._guard(self._liveness, head)
            if self._due(head):
                sent = self._send(head)
        for step in (self._landed, self._liveness, self._sweep, self._price_now, self._balance):
            self._guard(step, head)
        return sent

    def _due(self, head: int) -> bool:
        if not (self.watched_n or self.wants()):
            return False
        if not self.takeover:
            return True
        behind = self.last_poke is None or head - self.last_poke >= self.takeover
        if behind:
            if head > self.taken_until:
                self.alarm("book_poke_taken_over", head=head, last_poke=self.last_poke, role=self.role,
                           why=f"the book is {self.takeover} blocks or more behind: this watch checks every block for "
                               f"{self.hold} blocks")
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
        self.head = max(self.head, head)

    def _blind(self, exc: Exception) -> None:
        now = time.monotonic()
        if self.blind_since is None:
            self.blind_since = now
        stale = isinstance(exc, NodeStale)
        self._say("book_head_unread", 5.0, error=str(exc)[:160])
        if stale or now - self.blind_since >= BLIND_S:
            self.standing.raise_("blind", "book_head_stale" if stale else "book_head_unread",
                                 seconds=int(now - self.blind_since), error=str(exc)[:160], role=self.role,
                                 why="no fresh block is seen: no check goes out and the book's lag is not known")

    # ── the check ────────────────────────────────────────────────────────────────────

    def _send(self, head: int) -> bool:
        errors: list[str] = []
        try:
            price = self.price or self._read_price(head)
        except Exception as exc:
            price = 0
            errors.append(f"gas price: {str(exc)[:120]}")
        if price:
            gas = self.gas(self.watched_n)
            for lane in self.lanes:
                if len(lane.in_flight) >= MAX_POKES_IN_FLIGHT:
                    errors.append(f"{lane.name}: {len(lane.in_flight)} checks wait for a block")
                    continue
                try:
                    _tx, nonce = lane.sender.send(self.book, POKE_DATA, gas, price, self.tip, bump=True,
                                                  resend_after=POKE_RESEND_S)
                except Exception as exc:
                    errors.append(f"{lane.name}: {str(exc)[:160]}")
                    continue
                lane.in_flight.append((nonce, head, time.monotonic()))
                self.poked_for = head
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
        """The gas price, read again every 10 blocks after the check; a failed read keeps the last one."""
        if head - self.price_at >= 10:
            try:
                self._read_price(head)
            except Exception as exc:
                self.price_at = head  # tried again ten blocks on; the last price stands meanwhile
                self._say("book_gas_price_unread", error=str(exc)[:120], kept=self.price)

    # ── after the check ──────────────────────────────────────────────────────────────

    def _landed(self, head: int) -> None:
        """Each lane's checks in a block let go: by their receipt, or, when receipts cannot be read, by the chain's count
        of the key past their nonce. A reverted one is an alarm. A check waiting long is one too."""
        for lane in self.lanes:
            if not lane.in_flight:
                continue
            count: int | None = None
            left = []
            for nonce, at, sent_at in lane.in_flight:
                try:
                    r = lane.sender.receipt(nonce)
                except Exception as exc:
                    self._say("book_poke_receipt_unread", error=str(exc)[:120])
                    if count is None:
                        try:
                            count = lane.sender.count("latest")
                        except Exception:
                            count = -1
                    if 0 <= count and nonce < count:
                        lane.sender.confirm(nonce)  # in a block; its receipt is not read
                    else:
                        left.append((nonce, at, sent_at))
                    continue
                if r is None:
                    if lane.sender.is_gone(nonce):
                        log("book_poke_lost", lane=lane.name, nonce=nonce, seen=at)
                    else:
                        left.append((nonce, at, sent_at))
                    continue
                lane.sender.confirm(nonce)
                if int(r["status"], 16) != 1:
                    self.alarm("book_poke_reverted", lane=lane.name, tx=r.get("transactionHash"), seen=at,
                               block=int(r["blockNumber"], 16), role=self.role,
                               then=f"the next checks go with {MAX_TX_GAS} for {FLOOR_BLOCKS} blocks")
                    if self.on_revert is not None:
                        self.on_revert(head)
                else:
                    self.gas_used = int(r.get("gasUsed", "0x0"), 16) or self.gas_used
            lane.in_flight = left
            key = f"stuck:{lane.name}"
            if left and time.monotonic() - left[0][2] >= POKE_STUCK_S:
                self.standing.raise_(key, "book_poke_stuck", lane=lane.name, nonce=left[0][0], seen=left[0][1],
                                     waiting=len(left), seconds=int(time.monotonic() - left[0][2]), role=self.role,
                                     why="the key's check is not in a block: it is signed again with more fee")
            else:
                self.standing.clear(key, "book_poke_unstuck", lane=lane.name)

    def _view(self, sig: str, out: list[str]):
        raw = self.nodes.call("eth_call", [{"to": self.book, "data": "0x" + c.selector(sig).hex()}, "latest"])
        return abi_decode(out, bytes.fromhex(raw[2:]))

    def _liveness(self, head: int) -> None:
        """Once a head: `watched()` and `lastPokeBlock()`. Blocks going by unchecked while the book watches an asset are
        an alarm, again every `ALARM_EVERY_S` while they last."""
        if self.read_for >= head:
            return
        self.watched_n = len(self._view("watched()", ["uint32[]"])[0])
        self.last_poke = int(self._view("lastPokeBlock()", ["uint64"])[0])
        self.read_for = head
        lag = head - self.last_poke
        if self.watched_n and lag > self.lag_alarm:
            self.standing.raise_("lag", "book_poke_lagging", head=head, last_poke=self.last_poke, lag=lag,
                                 watched=self.watched_n, role=self.role,
                                 why="blocks go by and the book is not checked: a stop reached in them is not carried out")
        else:
            self.standing.clear("lag", "book_poke_caught_up", head=head, last_poke=self.last_poke)

    def _sweep(self, head: int) -> None:
        for lane in self.lanes:
            lane.sender.sweep()

    def _balance(self, head: int) -> None:
        """Each key's money every `BALANCE_EVERY_S`, and how many blocks and hours of checks it pays at the last price."""
        for lane in self.lanes:
            now = time.monotonic()
            if now - lane.balance_at < BALANCE_EVERY_S:
                continue
            lane.balance_at = now
            balance = int(self.nodes.call("eth_getBalance", [lane.sender.address, "latest"]), 16)
            per = max(self.gas_used or self.gas(self.watched_n), 1) * max(self.price, 1)
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
                 runway_hours: float = RUNWAY_HOURS, **kw):
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
        if lanes is None:
            lanes = [Lane("keeper", self.sender)] if self.sender is not None and self.wallet is not None else []
        self.poker = Poker(self.book, poke_node or node, lanes, self.alarm, gas=self._poke_gas,
                           wants=lambda: bool(self.holders), tip_wei=self.tip, runway_hours=runway_hours,
                           on_revert=self._poke_reverted)
        self.poker_thread: threading.Thread | None = None
        self.state_path = state_path
        self.saved: dict | None = None
        self._load()

    # ── its state on disk ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """The last block read and the gaps still to read back, as the loop left them."""
        if self.state_path is None or not self.state_path.exists():
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
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self.state_path)
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
        """A check reverted: the next go with the most a small block holds, for `FLOOR_BLOCKS` blocks, and the book's read
        gas is read again."""
        self.poke_floor = MAX_TX_GAS
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
        for exam in sorted(self.holders | self.ever_held):
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
            tx, nonce = self.sender.send(self.book, data, gas, self._gas_price(head), self.tip)
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
    # The keeper's key sends through the checks' node too: a check sent on it never waits out the keeper's timeout.
    sender = sf.Sender(wallet, poke_node) if wallet is not None else None
    lanes = []
    if wallet is not None:
        for name in [n.strip() for n in (getattr(args, "poke_wallet", "") or "").split(",") if n.strip()]:
            key = c.account(name)
            if to_checksum_address(key.address) == to_checksum_address(wallet.address):
                continue  # the keeper's own key is the last lane
            lanes.append(Lane(name, sf.Sender(key, poke_node)))
        lanes.append(Lane("keeper", sender))
    loop = BookLoop(keeper, node, book, reader=reader, wallet=wallet, sender=sender, poke_node=poke_node, lanes=lanes,
                    state_path=state_path, runway_hours=float(getattr(args, "poke_runway_hours", RUNWAY_HOURS)),
                    tip_wei=int(round(args.stop_tip_gwei * sf.GWEI)),
                    journal=sf.PriceJournal(journal_path) if journal_path else None, alarm=sf.Alarm(flag))
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
