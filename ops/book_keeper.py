"""The keeper's side of a factory's stop book (src/sim/StopBook.sol, docs/SIM-POOL.md, "The stops"): the book's journal
checked in every block, and each exam's touched stops and takes carried out.

A factory deployed with a stop book carries out its exams' stops and takes itself: `poke()`, open to anyone, logs the
oracle price of every asset with a stop or a take on it, once a block, and `settle(exam)`, open to anyone too, carries
out each touched stop or take at the first logged price that reached it, at that price and in that block's name,
however late the settle comes. A block nobody checked is not in the journal: the next check says so (`Gap`), and a stop
the price reached only in such a block is not carried out. Every exam function that changes anything checks the block
and settles first, so a trader who acts writes the journal themselves; one who sends nothing would leave the blocks
unchecked. This loop is what checks them:

  every block      → `poke()` while the book watches any asset (`watched()`), signed and sent at once with a fixed gas
                     (never an estimate: an estimate is a block late), no wait for its receipt;
  a touch          → `settle(exam)` for an exam whose `touches(exam)` is not empty, and for one with a change of a stop
                     (taken back or moved) that has come into force, so the book takes the entry out and stops logging
                     its asset. With a fixed gas too: the book's settle does nothing for want of gas, never reverts, so
                     an estimate would find the gas of doing nothing. A long settle stops between two steps and keeps
                     what it found; the loop sends the next one in a later block while `touches` is not empty. A fill
                     that ran out of the gas the call left it (`Fired` with `STARVED`) is sent again with the most a
                     small block holds;
  a gap            → each `Gap` the book writes while it watched an asset is the loop's own failure: a line, a count,
                     and an alarm. The prices of those blocks are read back from the node where it keeps them, written
                     to the journal of prices, and a stop or a take they reached is an alarm of its own (the payout of
                     that exam is held by `ops/payout_review.py`);
  the key          → every transaction of the keeper's key goes out through this thread's one sender
                     (`stop_filler.Sender`): its own nonce, a transaction lost by the node sent again with the same bytes,
                     and the keeper's own sends queued to it;
  the nodes        → `--stop-rpc` (the keeper's node when not set) and a backup (`--stop-backup-rpc`, or the keeper's
                     COLOSSEUM_RPC_BACKUP_URL): a call the first refuses or times out on goes to the second, once.

What is left of `--stops` (COLOSSEUM_STOPS) with a stop book: nothing sends `fillTrigger` (an exam with a book refuses it,
`NotBook`), and the watch of reaches by the price is this loop's gaps. `--stops` keeps its meaning for a factory with no
book (the 998 rehearsal), on the same code as before.
"""

from __future__ import annotations

import os
import pathlib
import queue
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
BOOK_TOPICS = [GAP, FIRED, NO_PRICE]
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
# How many of the key's pokes may wait for a block at once: past this the queue is stuck and sending more only deepens it.
MAX_POKES_IN_FLIGHT = 3
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


class Nodes:
    """A node and its backup as one: a call the first refuses (busy) or may not have taken (`Unsure`) goes to the second,
    once. An answer, an error in it included (a revert, `nonce too low`), is the answer: it never goes on."""

    def __init__(self, first: sf.Node, backup: sf.Node | None = None):
        self.first = first
        self.backup = backup
        self.url = first.url

    def call(self, method: str, params=()):
        try:
            return self.first.call(method, params)
        except (sf.Unsure, c.NodeBusy) as exc:
            if self.backup is None:
                raise
            log("book_node_backup", method=method, why=str(exc)[:120])
            return self.backup.call(method, params)


class BookLoop(sf.StopLoop):
    """The stop book of the keeper's factory, every block (see the module's text). `keeper` is the SimKeeper whose pools
    and accounts are watched; `wallet` and `sender` are None for a watch that sends nothing. The keeper's thread, the
    alarm, the journal of prices and the key's sender are the stops' loop's (`stop_filler.StopLoop`)."""

    def __init__(self, keeper, node, book: str, *, reader=None, settle_gas: int = SETTLE_GAS, **kw):
        super().__init__(keeper, node, **kw)
        self.book = to_checksum_address(book)
        self.reader = reader or node  # the keeper's thread reads through its own
        self.settle_gas_default = settle_gas
        self.holders: set[str] = set()  # exams with an entry in the book, or one set since the last read
        self.changes: dict[tuple[str, int, bool], int] = {}  # a change asked of an entry -> the block it is in force
        self.watched_n = 0  # how many assets the book watched at the last read
        self.watched_at: dict[int, int] = {}  # block -> how many it watched then, the last CATCHUP blocks
        self.pokes: list[tuple[str, int, int]] = []  # (tx, nonce, head) of the pokes not yet in a block
        self.poked_for = 0  # the head the last poke was sent after
        self.poke_floor = 0  # the gas of every poke after one reverted
        self.settles: dict[str, tuple[str, int, int, int]] = {}  # exam -> (tx, nonce, head, gas)
        self.settle_gas: dict[str, int] = {}  # exam -> the gas its next settle goes with
        self.settle_refused: dict[str, int] = {}  # exam -> the head its settle was last refused at
        self.precompile_gas = 2 * PRECOMPILE_GAS
        self.gaps = 0
        self.gap_blocks = 0
        self.gap_jobs: list[dict] = []  # gaps whose blocks are still to be read back (`_judge_gaps`)
        self.backoff: dict[str, int] = {}  # exam -> the block before which no settle goes (`FAIL_BACKOFF_BLOCKS`)
        self.failed_said: set[str] = set()  # exams whose failed fill was raised, until one of theirs fills
        self.stuck_said: int | None = None

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
        for kept in (self.settle_gas, self.settle_refused, self.backoff):
            kept.pop(exam, None)
        self.failed_said.discard(exam)
        for key in [k for k in self.changes if k[0] == exam]:
            del self.changes[key]

    # ── a step ───────────────────────────────────────────────────────────────────────

    def step(self, head: int) -> list[str]:
        """Everything up to block `head`: the check of the next block sent first, then the logs, the gaps, the receipts,
        and the settles due. What it sent: "poke", and the exams it sent a settle for."""
        while True:
            try:
                message = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.apply(message)
        if head <= self.block:
            return []
        sent: list[str] = []
        if not self.block:
            self._read_watched(head)
        if self._poke(head):
            sent.append("poke")
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
        self._receipts(head)
        sent += self._settle_due(head)
        self._check_stuck()
        self._judge_gaps()
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

    def _poke(self, head: int) -> bool:
        """The check of block `head + 1`, sent once for each head while the book watches an asset (as last read): sent
        after seeing `head`, it lands in `head + 1` at the soonest."""
        if self.wallet is None or self.sender is None or self.poked_for >= head or not self.watched_n:
            return False
        self.poked_for = head
        if len(self.pokes) >= MAX_POKES_IN_FLIGHT:
            self._say("book_pokes_queued", in_flight=len(self.pokes), why="earlier checks wait for a block; no more sent")
            return False
        gas = max(self.poke_floor, poke_gas(max(self.watched_n, 1), self.precompile_gas))
        try:
            tx, nonce = self.sender.send(self.book, c.encode_call("poke()"), gas, self._gas_price(head), self.tip)
        except Exception as exc:
            log("book_poke_failed", head=head, error=str(exc)[:200], then="sent again after the next block")
            return False
        self.pokes.append((tx, nonce, head))
        return True

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
                    job["assets"], job["entries"] = self._gap_entries()
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

    def _gap_entries(self) -> tuple[list[int], dict]:
        """The assets the book watches and every entry of the exams with one, as they stand now."""
        assets = sorted(int(a) for a in self._call(self.book, "watched()", [], [], ["uint32[]"])[0])
        entries: dict[tuple[str, int, bool], tuple] = {}
        for exam in sorted(self.holders):
            for asset in assets:
                for take in (False, True):
                    e = self._call(self.book, "entryOf(address,uint32,bool)", ["address", "uint32", "bool"],
                                   [exam, asset, take], [ENTRY])[0]
                    if e[0]:
                        entries[(exam, asset, take)] = e
        return assets, entries

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
        for (exam, asset, take), e in list(job["entries"].items()):
            level, armed = phase(e, b)
            if level and b >= armed and asset in prices and reached(bool(e[4]), level, prices[asset]):
                self.alarm("book_gap_touch", exam=exam, asset=asset, kind="take" if take else "stop", level=level,
                           block=b, oracle=prices[asset],
                           why="reached in a block the book did not check: not carried out; the payout is held")
                del job["entries"][(exam, asset, take)]  # said once: its first touch is the one that counts

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
            if exam in due or exam in self.settles:
                continue
            try:
                touches = self._call(self.book, "touches(address)", ["address"], [exam], [f"{TOUCH}[]"])[0]
            except c.NodeBusy:
                raise
            except Exception as exc:
                self._say("book_touches_unread", exam=exam, error=str(exc)[:120])
                continue
            if touches:
                due[exam] = f"{len(touches)} touched, the first in block {int(touches[0][5])}"
        sent = []
        for exam, why in sorted(due.items()):
            if exam in self.settles or exam in self.keeper.done or self.backoff.get(exam, 0) > head:
                continue
            if self._settle(exam, head, why):
                sent.append(exam)
        return sent

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
        """The receipt of one of the loop's transactions: a dict, None for none yet, or "gone" (never to land)."""
        rcpt = self.node.call("eth_getTransactionReceipt", [tx])
        if rcpt:
            self.sender.confirm(nonce)
            return rcpt
        return "gone" if self.sender.is_gone(nonce) else None

    def _receipts(self, head: int) -> None:
        if self.sender is None:
            return
        try:
            left = []
            for tx, nonce, at in self.pokes:
                r = self._receipt(tx, nonce)
                if r is None:
                    left.append((tx, nonce, at))
                elif r == "gone":
                    log("book_poke_lost", tx=tx, seen=at)
                elif int(r["status"], 16) != 1:
                    self.poke_floor = MAX_TX_GAS
                    self.alarm("book_poke_reverted", tx=tx, seen=at, block=int(r["blockNumber"], 16),
                               then=f"every next check goes with {MAX_TX_GAS}")
            self.pokes = left
            for exam, (tx, nonce, at, gas) in list(self.settles.items()):
                r = self._receipt(tx, nonce)
                if r is None:
                    continue
                del self.settles[exam]
                if r == "gone":
                    log("book_settle_lost", exam=exam, tx=tx, seen=at)
                elif int(r["status"], 16) != 1:
                    # Reverted in the block though it ran in the trial: most likely `NoProgress`, the block's state
                    # dearer than the trial's. The next one goes with the most a small block holds.
                    self.settle_gas[exam] = MAX_TX_GAS
                    log("book_settle_reverted", exam=exam, tx=tx, seen=at, gas=gas, then=f"tried again with {MAX_TX_GAS}")
                else:
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
        return {"holders": len(self.holders), "watched": self.watched_n, "pokes_in_flight": len(self.pokes),
                "settles_in_flight": len(self.settles), "gaps": self.gaps, "gap_blocks": self.gap_blocks}


def build(args, keeper, state, book: str, wallet=None, prefix: str = "") -> BookLoop:
    """The loop from the stops' settings (stop_filler.add_args), its nodes checked against the network."""
    url = args.stop_rpc or c.RPC_URL
    backup_url = getattr(args, "stop_backup_rpc", "") or os.environ.get("COLOSSEUM_RPC_BACKUP_URL", "")
    first = sf.Node(url)
    if c.NETWORK.name == "mainnet":
        core.check_node(url, c.CHAIN_ID)
    sf.check_chain(first)
    backup = None
    if backup_url and backup_url.rstrip("/") != url.rstrip("/"):
        backup = sf.Node(backup_url)
        if c.NETWORK.name == "mainnet":
            core.check_node(backup_url, c.CHAIN_ID)
        sf.check_chain(backup)
    node = Nodes(first, backup)
    reader = Nodes(sf.Node(url), sf.Node(backup_url) if backup is not None else None)
    journal_path = (state.parent / f"{prefix}prices-{args.deployment}.json") if args.price_journal is None else (
        pathlib.Path(args.price_journal) if args.price_journal else None)
    flag = (state.parent / f"{prefix}stops-{args.deployment}.alarm") if args.stop_alarm is None else (
        pathlib.Path(args.stop_alarm) if args.stop_alarm else None)
    if args.stop_tip_gwei < 0:
        raise SystemExit("--stop-tip-gwei must not be negative")
    loop = BookLoop(keeper, node, book, reader=reader,
                    wallet=wallet, sender=sf.Sender(wallet, node) if wallet is not None else None,
                    tip_wei=int(round(args.stop_tip_gwei * sf.GWEI)),
                    journal=sf.PriceJournal(journal_path) if journal_path else None, alarm=sf.Alarm(flag))
    loop.precompile_gas = precompile_gas_of(node, book)
    return loop


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
