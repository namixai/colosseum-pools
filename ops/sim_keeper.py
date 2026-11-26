"""The keeper of simulated pools (SimPoolFactory, docs/SIM-POOL.md): every call a pool or its paper
exams leave to anyone, made when it is due, and the fills of the platform's filler.

    spike/.venv/bin/python -m ops.sim_keeper --deployment sim1 --once --dry-run   # what is due
    spike/.venv/bin/python -m ops.sim_keeper --deployment sim1                    # a service
    spike/.venv/bin/python -m ops.sim_keeper --deployment sim1 --measure EXAM \\
        --trader-wallet t1 --orders 20                                                   # how late fills land

`ops/multi_keeper.py` hands a record with a `SimPoolFactory` and no `MultiPool` to this keeper, so
the host's one unit serves either.

Two loops in one process:

  the fast loop  → every new block, while any exam or stage is live: the `Placed` events of the
                   blocks since the last look (one eth_getLogs a block, about 3 600 an hour), and a
                   `fill` sent for each order of ours as soon as its first block (`placedBlock +
                   fillDelay`) is next. The fill goes out with a fixed gas and no estimate first: an
                   estimate is a block late. Only the filler's wallet fills; any other only logs.
                   With the stops' loop (below) it asks the node for neither: that loop hands it
                   each step's head and logs, the orders among them (`fed_step`).
  the slow pass  → every --every seconds: new pools (the factory's `pools()`), their exams and
                   stages (`ExamSold`, `Funded`, read in windows of --log-window blocks); for each
                   live one, `checkpoint` in the first minutes of a UTC day, `breach` when a rule is
                   broken now, `expire` past its time, `release` once it can do nothing more,
                   `claimPayout` for a passed stage; for each pool, `pay` while a payout is due: approved
                   and past its 72 hours, or past its 7 days unapproved. Its reads go in batches of at
                   most a hundred in one eth_call (Multicall3, `Reads`), each allowed to fail alone and
                   then asked alone, an account never cut in two: a pass costs requests by the hundred
                   reads, not by the account. Once a minute the keeper says the requests it made, by
                   method (`keeper_alive`).

By default (`--stops loop`, COLOSSEUM_STOPS; `inline` is the fast loop's as above), the stops are a third
loop's, in a thread of its own (ops/stop_filler.py): every block, the stops and takes of the live exams against the
oracle price, a stop reached sent at once, and an alarm for a reach that went by with no fill. Nothing in the two loops
above holds it up: the slow pass, a receipt waited for and the node's rate limit stop only the thread they are in.
Every transaction of the filler key then goes out through that thread's one sender, the keeper's own handed to it and
their receipts waited for here, so the two threads never number a transaction each. The keeper's loop checks the
thread is there and moving. Its settings (`--stop-rpc`, `--stop-tip-gwei`, the journal of prices, the flag of missed
stops) are that module's.

The day's snapshot is what the daily loss is measured from: without it the base stays yesterday's,
and a trader who leaves it after a good day loosens their own limit. The keeper takes it.

--measure EXAM places --orders orders on an exam of the trader's own (`--trader-wallet`), one at a
time, small and alternating buy and sell, and waits for each to be filled or to lapse; it writes
each one's `late` (blocks after the first possible one) and `source` to --measure-out and prints p50
and p99. That is the number the filler's window is set from. It sends transactions, and is run by hand:
on mainnet it asks for the confirmation phrase at the terminal (ops/sim_network.py).

Every slow call is tried with eth_estimateGas first and sent only if the contract would take it.
The keeper holds no trader's key except, in --measure, the one it is given.

Testnet unless both COLOSSEUM_NETWORK=mainnet and --network mainnet name mainnet (ops/sim_network.py). On
mainnet a keeper that sends also needs --confirm-mainnet with its deployment's label and a mainnet key directory,
and it does not start on the gateway's node (audit M-1); --dry-run needs neither.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import queue
import re
import sys
import time

import requests
from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from ops import sim_network as sn
from ops.contract_errors import refusal
from spike.hlspike import common as c

STATE_DIR = core.STATE_DIR
MAX_LOG_WINDOW = core.MAX_LOG_WINDOW
log = core.log
view = core.view

# The events as the contracts declare them; the tests hold each against the Solidity source.
EXAM_SOLD = "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64,uint256,bool,bytes32,address)").hex()
FUNDED = "0x" + keccak(text="Funded(address,address,address)").hex()
PLACED = "0x" + keccak(text="Placed(uint32,bool,uint64,uint256,uint256)").hex()
FILLED = "0x" + keccak(text="Filled(uint32,bool,uint64,uint64,uint256,uint256,uint8)").hex()
LAPSED = "0x" + keccak(text="Lapsed(uint32,bool,uint64)").hex()
TRIGGER_SET = "0x" + keccak(text="TriggerSet(uint32,bool,uint64,uint64,uint256)").hex()
TRIGGER_CLEARED = "0x" + keccak(text="TriggerCleared(uint32,bool)").hex()
TRIGGER_FILLED = "0x" + keccak(text="TriggerFilled(uint32,bool,uint64,uint64,uint256,uint64,uint8)").hex()
TRIGGER_TOPICS = [TRIGGER_SET, TRIGGER_CLEARED, TRIGGER_FILLED]
EXAM_STATUS = ("None", "Active", "Passed", "Breached", "Expired")
PAYOUT_STATE = ("None", "Due", "Stopped", "Paid", "Held", "Refused")
EXAM_TERMS = "(uint64,uint16[],uint32,uint32,uint16,uint32,uint16,uint16)"
RULES = "(uint16,uint16,uint32,uint32[],uint32[])"
TRIGGER = "(uint64,uint64,uint64,uint64)"
PAYOUT = "(address,address,uint64,uint64,uint8,bool,uint64,bool)"
# SimPoolFactory's PAYOUT_DELAY and PAYOUT_HARD: a payout is due 72 hours after its claim once approved,
# and 7 days after it whether approved or not (the test holds them to the source).
PAYOUT_DELAY = 72 * 3600
PAYOUT_HARD = 7 * 86_400
# A fill's gas, fixed: an estimate first would cost a block. PaperExam's fill, with sixteen assets
# open, is under it (forge test --gas-report).
FILL_GAS = 400_000
# How many payouts one `pay` call settles at most: the gas of a call was measured with two (test/sim/SimPool.t.sol,
# KEEPER_PAYS). A pool settles at most its own `payoutsPerDay` in any 24 hours, one to eight, set when it is made, and
# `payRoom()` says how many are left now: a pool of more than two a day is paid two a call, pass after pass.
PAY_PER_CALL = 2
# How far past the head the keeper reads payouts to find one to pay: behind a head that waits for its approval,
# later approved payouts are paid out of order, and `pay` must be told to look that far (review of #286).
# One call fits a small block (2 000 000 gas, ops/keeper.py), measured from cold storage and not estimated
# (test/sim/SimPool.t.sol, test_pay_theKeepersFullBatch_fitsASmallBlock): PAY_PER_CALL payouts paid past 94
# waiting, `pay(96)`, take 730 903 gas, each further payout looked at 5 390, and `pay(192)` 1 248 428: under two
# thirds of the block, the rest left for a token whose transfer costs more than the test's. This is the
# keeper's limit, kept to what was measured, not the block's: a longer look may still fit, by the queue and the
# token. `pay` counts from the head whatever it is told, so a payout further than this past a waiting head is
# not paid by the keeper until the head moves, which the approval of the payouts before it does, or their 7 days.
SCAN_PAYOUTS = 192


# ── what is due, from what was read ──────────────────────────────────────────────────

def account_calls(kind: str, status: str, now: int, *, passed_at: int = 0, fund_window: int = 0,
                  broken: bool = False, over: bool = False, snapshot_due: bool = False) -> list[str]:
    """The slow calls an exam or a stage of a simulated pool is due, from what was read. `kind` is
    "exam" or "stage"; `over` is past its deadline or its idle limit; `snapshot_due` is inside the day's
    first minutes with today's snapshot not yet taken. Fills are the fast loop's."""
    if status == "Active":
        if broken:
            return ["breach"]
        calls = ["checkpoint"] if snapshot_due else []
        return calls + (["expire"] if over else [])
    if status in ("Breached", "Expired"):
        return ["release"]
    if status == "Passed":
        if kind == "stage":
            return ["claimPayout"]
        if now > passed_at + fund_window:
            return ["release"]
    return []


def payable(state: str, due_at: int, approved: bool, now: int) -> bool:
    """Whether `pay` pays this payout now: waiting, and past its 72 hours once approved or past its 7
    days from its claim whether approved or not."""
    if state != "Due" or now < due_at:
        return False
    return approved or now >= due_at - PAYOUT_DELAY + PAYOUT_HARD


def pay_reach(rows: list[tuple[str, int, bool]], now: int, room: int = PAY_PER_CALL) -> int:
    """How many payouts from the head `pay` should look at (its `max`), from the rows read the head first: up to
    the last payable one the daily limit has room for (`room`, the pool's `payRoom()`), or, with none, past the
    settled ones at the head; zero for nothing to do. `pay` counts every payout it looks at, settled ones too
    (review of #286), so the keeper names how far to go. A payout waiting for its approval keeps the head, and
    the keeper does not call `pay` for it before its 7 days, nor for one the daily limit holds."""
    last = 0
    paid = 0
    most = min(PAY_PER_CALL, room)
    for k, (state, due_at, approved) in enumerate(rows):
        if paid == most:
            break  # all the day has room for; the rest when the limit lets it
        if payable(state, due_at, approved, now):
            last = k + 1
            paid += 1
    if last:
        return last
    settled = 0
    while settled < len(rows) and rows[settled][0] != "Due":
        settled += 1
    return settled


def pay_due(rows: list[tuple[str, int, bool]], now: int) -> bool:
    """Whether `pay` would pay something or move the head (pay_reach)."""
    return pay_reach(rows, now) > 0


def fill_now(fill_from: int, fill_until: int, latest: int) -> bool:
    """Whether an order's fill should go out now, to land in the next block: that block is inside its
    window [fill_from, fill_until]."""
    return fill_from <= latest + 1 <= fill_until


def quantiles(values: list[int]) -> dict:
    """p50, p99 and the largest of a list of blocks late."""
    if not values:
        return {"n": 0}
    s = sorted(values)
    at = lambda p: s[min(len(s) - 1, int(p * len(s)))]
    return {"n": len(s), "p50": at(0.5), "p99": at(0.99), "max": s[-1]}


def placed_of(log_entry: dict) -> tuple[str, int, int]:
    """An order's exam and its window, from a `Placed` log."""
    _, _, fill_from, fill_until = abi_decode(["bool", "uint64", "uint256", "uint256"], bytes.fromhex(log_entry["data"][2:]))
    return to_checksum_address(log_entry["address"]), int(fill_from), int(fill_until)


def filled_of(log_entry: dict) -> tuple[int, int]:
    """`late` and `source` of a `Filled` log."""
    _, _, _, _, late, source = abi_decode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"],
                                          bytes.fromhex(log_entry["data"][2:]))
    return int(late), int(source)


def refused(exc) -> str:
    """A call the chain refused, or one that failed on the way, as one line for the log with no node address in it:
    the node's URL may hold its key, and requests writes the whole of it into its errors. The address goes out of the
    whole text first (ops/sim_check.py, `quiet`), then `refusal` names the contract's error or cuts the node's words
    to 200: cut first, a host name that is the key stands there in part, known to no rule."""
    from ops import sim_check  # it imports this module, so here and not at the top
    return refusal(Exception(sim_check.quiet(exc, None)))


# ── many reads in one call ───────────────────────────────────────────────────────────

# Multicall3 stands at one address on every chain that has it, HyperEVM's two among them (3 808 bytes of code on
# each, read on 24 November 2026). One eth_call of its `aggregate3` carries many reads, each allowed to fail by
# itself, and the number and the time of the block the call ran in with them: all from one state, and the number is
# the call's own, not the tag of the request. A node paid by the request is paid once for all of them.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGGREGATE3 = "aggregate3((address,bool,bytes)[])"
BLOCK_READS = (c.encode_call("getBlockNumber()"), c.encode_call("getCurrentBlockTimestamp()"))
# The most reads one eth_call carries, the two of the block among them.
BATCH = 100
# How often the keeper measures again how its node numbers the block of a call (`numbering`).
NUMBERING_EVERY_S = 600.0
# How often a process says it is alive, with the requests it made since by method (`keeper_alive`).
ALIVE_EVERY_S = 60.0
# Seconds the fast loop may wait with no step handed in by the stops' loop before it is an alarm (`fed_step`): it
# fills the orders from those steps alone, so while none comes no order is filled.
UNFED_S = 30.0


def has_multicall(call) -> bool:
    """Whether Multicall3 stands on the chain of the node `call` asks (c.rpc, or a node's own call)."""
    code = call("eth_getCode", [MULTICALL3, "latest"])
    return isinstance(code, str) and len(code) > 2


def aggregate(call, reads, tag: str = "latest") -> tuple[int, int, list[bytes | None]]:
    """One eth_call of `aggregate3` at `tag`: the number and the time of the block the call says it ran in, and the
    answer of each of `reads` ((to, data) each), None for one that failed. The number is as the node gives it: a node
    that runs a call at `latest` as the next block's says one more than its head (`numbering`). The call itself
    failing (the node, a revert of the whole, an answer of another shape) raises, as any call does."""
    calls = [(MULTICALL3, False, data) for data in BLOCK_READS]
    calls += [(to_checksum_address(to), True, data) for to, data in reads]
    raw = call("eth_call", [{"to": MULTICALL3, "data": "0x" + c.encode_call(AGGREGATE3, ["(address,bool,bytes)[]"],
                                                                         [calls]).hex()}, tag])
    out = abi_decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))[0]
    if len(out) != len(calls) or len(out[0][1]) != 32 or len(out[1][1]) != 32:
        raise RuntimeError(f"aggregate3: {len(out)} answers to {len(calls)} reads")
    return (int.from_bytes(out[0][1], "big"), int.from_bytes(out[1][1], "big"),
            [bytes(data) if ok else None for ok, data in out[2:]])


class BadNumbering(RuntimeError):
    """The number a call says it runs in is neither the head's nor the next block's: nothing here can read it."""


def numbering(call, head_of) -> int | None:
    """How many blocks past its head the node numbers a call at `latest`: 0 for a node that runs it as the head block
    itself, 1 for one that runs it as the block to come (HyperEVM's own nodes, read on 24 November 2026: the number
    of the next block with the time of the head). Measured twice, and taken only when the two agree (review 20 of
    #342): one address may be several servers, and one measure may meet a server a block behind the others. None
    when they do not, or when either could not be made: to be measured again. Anything else raises: the call's
    number says nothing this code knows how to read."""
    first = _measured(call, head_of)
    if first is None:
        return None
    return first if _measured(call, head_of) == first else None


def _measured(call, head_of) -> int | None:
    """One measure: a call between two reads of the head (`head_of`) that agree, and the call's time the head's own,
    as the node records the head by its number. Both kinds of node say the head's time in a call, whatever number
    they say; a call that says another time ran on another block (a server behind the one that read the head), and
    its number says nothing of the head's. None for either: a block came between, or the call met another block."""
    before = head_of()
    number, t, _ = aggregate(call, [])
    if head_of() != before or block_time(call, before) != t:
        return None
    if number - before not in (0, 1):
        raise BadNumbering(f"a call at the head {before} says it runs in block {number}")
    return number - before


def head(call) -> tuple[int, int]:
    """The head block's number and its time, in one request."""
    got = call("eth_getBlockByNumber", ["latest", False])
    return int(got["number"], 16), int(got["timestamp"], 16)


def block_time(call, block: int) -> int | None:
    """The time of block `block` as the node records it, asked for by its number; None for a block it does not have
    (a number past its head) or an answer that is not that block."""
    got = call("eth_getBlockByNumber", [hex(block), False])
    try:
        number, t = int(got["number"], 16), int(got["timestamp"], 16)
    except (TypeError, KeyError, ValueError):
        return None
    return t if number == block else None


def ran_in(call, block: int, t: int | None, latest: tuple[int, int | None] | None = None) -> bool:
    """Whether a batch whose call says it ran in `block` at `t` (its number counted back by the node's numbering) ran
    in that block, held against the head read just before it (`latest`: its number and time) when there is one
    (review 20 of #342). Both kinds of node say the head's time in a call: the head's time under another number, or
    the head's number with another time, is a numbering that moved since it was measured. Any other block (one that
    came between the head and the call, a server a block behind, or no head to hold it against) costs a request: the
    node's own record of `block` by its number, whose time must be the call's. Anything that does not hold, or cannot
    be asked, is no."""
    if latest is not None:
        number, number_t = latest
        if number_t is not None and (block == number or t == number_t):
            return block == number and t == number_t
    try:
        return t is not None and block_time(call, block) == t
    except Exception:
        return False


class Counted:
    """A node's call (`c.rpc`) with its requests counted by method, for the line a process writes once a minute
    (`keeper_alive`, the stops' loop's `stop_loop_alive`): what a node paid by the request is paid for. A call the
    node throttled and c.rpc tried again is counted once. No address of the node is kept."""

    def __init__(self, call) -> None:
        self.call = call
        self.counts: dict[str, int] = {}

    def __call__(self, method: str, params=()):
        self.counts[method] = self.counts.get(method, 0) + 1  # one thread: the keeper's own
        return self.call(method, params)

    def taken(self) -> dict[str, int]:
        """The counts since they were last taken, by method."""
        counts, self.counts = self.counts, {}
        return dict(sorted(counts.items()))


def count_requests() -> Counted:
    """c.rpc counted from here on, for this process: every call of the plumbing goes through it."""
    if not isinstance(c.rpc, Counted):
        c.rpc = Counted(c.rpc)
    return c.rpc


def multicall_or_not(call, who: str) -> bool:
    """Whether Multicall3 is there, looked at once at the start. Without it the process works as before, a read a
    request, and says so in one line; on mainnet it does not start: it is there, and a node that says otherwise is
    not the chain's."""
    if has_multicall(call):
        return True
    if c.NETWORK.name == "mainnet":
        raise SystemExit(f"refusing to run {who} on mainnet: no Multicall3 at {MULTICALL3} on this node")
    log("no_multicall", who=who, at=MULTICALL3, then="each read is its own request, as before")
    return False


class Reads:
    """What one pass reads of the chain, asked in batches (`aggregate`) and answered from them: `view` and
    `call_view` as the keeper's own, the same answers decoded the same way. A read that was not in a batch, or that
    failed in it, or whose answer does not decode, is asked alone as before, so a failure is said as it was.

    The reads of one account are never cut in two: they are of one block, the block of the call that carried them
    (`at`), counted back by the node's numbering. Reads that may burn the call's gas when they fail (`violation()`
    on an asset with no price) go last in their call; a call that fails whole is asked once more without them."""

    def __init__(self) -> None:
        self.got: dict[tuple[str, bytes], bytes] = {}
        self.block_of: dict[str, int] = {}   # an account -> the block its reads are of
        self.assets: dict[str, list[int]] = {}  # an exam -> the assets whose stops were asked for
        self.block: int | None = None        # the block and the time of the first call
        self.time: int | None = None
        self.calls = 0

    def load(self, groups, call, ahead: int = 0, size: int = BATCH) -> None:
        """`groups`: (owner, reads, last) each; `last` are the reads that go at the end of the call."""
        room = size - len(BLOCK_READS)
        chunk: list = []
        for group in groups:
            if chunk and sum(len(g[1]) + len(g[2]) for g in chunk) + len(group[1]) + len(group[2]) > room:
                self._ask(chunk, call, ahead, room)
                chunk = []
            chunk.append(group)
        if chunk:
            self._ask(chunk, call, ahead, room)

    def _ask(self, chunk, call, ahead: int, room: int) -> None:
        first = [r for _, reads, _ in chunk for r in reads]
        last = [r for _, _, tail in chunk for r in tail]
        whole = len(first) + len(last) <= room  # a group larger than a call is cut: its reads are of no one block
        if not whole:
            # Nor of the block an earlier call gave it (review 20 of #342: a new exam's rules, in the pass's first
            # call, and its stops cut across two more): what is not of one block is not handed over as one.
            for owner, _, _ in chunk:
                self.block_of.pop(owner, None)
        for reads in ([first + last] if whole else [(first + last)[i:i + room] for i in range(0, len(first + last), room)]):
            try:
                self.calls += 1
                number, t, answers = aggregate(call, reads)
            except Exception as exc:
                if not (whole and last):
                    log("batch_failed", reads=len(reads), error=refused(exc), then="each asked alone")
                    continue
                try:  # a read that burns the gas when it fails takes the whole call with it: once more without them
                    self.calls += 1
                    reads = first
                    number, t, answers = aggregate(call, reads)
                except Exception as again:
                    log("batch_failed", reads=len(reads), error=refused(again), then="each asked alone")
                    continue
            if self.block is None:
                self.block, self.time = number - ahead, t
            for (to, data), answer in zip(reads, answers):
                if answer is not None:
                    self.got[(to.lower(), data)] = answer
                else:
                    # Asked for again here and not answered: an answer an earlier call gave is of another block.
                    self.got.pop((to.lower(), data), None)
            if whole:
                for owner, _, _ in chunk:
                    self.block_of[owner] = number - ahead

    def raw(self, to: str, sig: str, types=(), args=()) -> bytes | None:
        return self.got.get((to.lower(), c.encode_call(sig, list(types), list(args))))

    def whole(self, exam: str, assets) -> bool:
        """Whether the exam's status and the stop, the take and the position of each of `assets` were all answered, in
        the one call its block is of: what the stops' loop is handed. With one missing, nothing of it is handed over:
        the exam is read again alone, as before."""
        return exam in self.block_of and self.raw(exam, "result()") is not None and all(
            self.raw(exam, sig, ["uint32"], [asset]) is not None
            for asset in assets for sig in ("stopOf(uint32)", "takeOf(uint32)", "positionOf(uint32)"))

    def read(self, to: str, sig: str, types, args, out) -> tuple:
        """A read of the batch, decoded; one that is not in it raises: nothing is asked in its place."""
        raw = self.raw(to, sig, types, args)
        if raw is None:
            raise RuntimeError(f"{sig.split('(')[0]}: not read in the pass's call")
        return abi_decode(list(out), raw)

    def view(self, addr: str, sig: str, out: str, types=(), args=()):
        raw = self.raw(addr, sig, types, args)
        if raw is not None:
            try:
                return abi_decode([out], raw)[0]
            except Exception:
                pass  # an answer of another shape (an address with no code answers with nothing): asked alone
        return view(addr, sig, out, types, args)

    def call_view(self, to: str, sig: str, types, args, out) -> tuple:
        raw = self.raw(to, sig, types, args)
        if raw is not None:
            try:
                return abi_decode(list(out), raw)
            except Exception:
                pass
        return c.call_view(to, sig, types, args, out)


# ── eth_getLogs in the windows a node takes ──────────────────────────────────────────

# A window narrowed by a node's refusal is tried at its full width again this long after: a node whose plan changed,
# or a refusal that was not about the width after all, costs one slower catch-up an hour at most.
LOG_WINDOW_RETRY_S = 3_600.0


def beyond_head(text: str) -> bool:
    """Whether a node's refusal of an eth_getLogs says the range reaches past the blocks its logs have yet: one address
    may be several servers, and the one that answers the logs may stand a block or two behind the one that answered
    the head (Alchemy's -32602 "block range extends beyond current head block", on testnet on 25 November 2026, on
    about one read in seven). Not a failure, and no reason to give any block up: the blocks before it are read, and
    the rest at the next step."""
    t = text.lower()
    return "head" in t and any(w in t for w in ("beyond", "ahead of", "past the", "greater than"))


# A refusal that speaks of the width (a range, a limit, too many blocks or results), or an HTTP 4xx with no words
# (Alchemy's free plan answers 400): the narrower window that the node took after it is the node's, and is kept.
# Any other error (a server's -32603, say) narrows that one read and teaches nothing.
WIDTH_WORDS = re.compile(r"range|limit|exceed|too (?:many|large|big|wide)|more than|max|http 4\d\d|client error",
                         re.IGNORECASE)


def named_width(text: str, refused: int) -> int | None:
    """The width of blocks a refusal names, when it names one narrower than the one refused (Alchemy's free plan: "up
    to a 10 block range", 25 November 2026); None when it names none."""
    for m in re.finditer(r"(\d[\d,]*)[\s-]*blocks?\b", text, re.IGNORECASE):
        n = int(m.group(1).replace(",", ""))
        if 1 <= n < refused:
            return n
    return None


class LogWindow:
    """eth_getLogs of a span of blocks, in windows of a width the node takes, learned from its refusals: no node's
    limit is written into the code (HyperEVM's own nodes take 50 blocks, Alchemy's free plan 10, others 1 000).

    A window the node refuses is narrowed, to the width the refusal names or else to half, down to one block: no answer
    of the node's holds the read on one range for good. A refusal that speaks of the width (`WIDTH_WORDS`) teaches it:
    the width found is kept for the next reads (`width`) and tried at full again `LOG_WINDOW_RETRY_S` later; any other
    narrows that one read only. A refusal of a single block is no refusal of a width: it is raised as it came, and
    the width this read learned on the way down to it is put back. A range
    the node says reaches past its head (`beyond_head`) is read up to the block before: the read returns how far it
    got, and the caller reads the rest next time; nothing is skipped. `passes`: the errors raised as they come (a busy
    node, one never reached), which a narrower window would not mend; `unsure`: the ones that teach nothing whatever
    they say (a timeout, a server's error), as they may pass by themselves."""

    def __init__(self, width: int = MAX_LOG_WINDOW, say=None, clock=time.monotonic):
        self.full = max(1, int(width))
        self.width = self.full
        self.narrowed_at: float | None = None
        self.short = 0  # reads the node's logs ended in before their last block, since last taken (`taken_short`)
        self.say = say or log
        self.clock = clock

    def taken_short(self) -> int:
        short, self.short = self.short, 0
        return short

    def read(self, call, query: dict, lo: int, hi: int, passes: tuple = (), unsure: tuple = ()) -> tuple[list[dict], int]:
        """The logs `query` (its addresses and topics) asks for in blocks `lo` to `hi`, and the last block they were
        read to: `hi`, or less when the node's logs end before it."""
        if self.narrowed_at is not None and self.clock() - self.narrowed_at >= LOG_WINDOW_RETRY_S:
            self.width, self.narrowed_at = self.full, None
        out: list[dict] = []
        start, top, cap = lo, hi, self.width
        learned = (self.width, self.narrowed_at)  # put back if one block is refused all the same: not the width, then
        while start <= top:
            end = min(top, start + cap - 1)
            try:
                got = call("eth_getLogs", [{**query, "fromBlock": hex(start), "toBlock": hex(end)}])
            except passes:
                raise
            except Exception as exc:
                text = str(exc)
                if beyond_head(text):
                    self.short += 1
                    top, cap = end - 1, max(1, (end - start + 1) // 2)
                    continue
                if end == start or "rate limit" in text.lower():
                    if end == start:
                        self.width, self.narrowed_at = learned
                    raise
                cap = named_width(text, end - start + 1) or (end - start + 1) // 2
                if cap < self.width and not isinstance(exc, unsure) and WIDTH_WORDS.search(text):
                    self.say("logs_window_narrowed", was=self.width, now=cap, error=refused(exc),
                             then=f"kept, and {int(self.full)} tried again in an hour")
                    self.width, self.narrowed_at = cap, self.clock()
                continue
            out += got
            start, cap = end + 1, self.width
        return out, start - 1


# ── the keeper ───────────────────────────────────────────────────────────────────────

class SimKeeper:
    def __init__(self, factory: str, wallet, sender: str, dry: bool, state_path: pathlib.Path, start_block: int,
                 window: int = MAX_LOG_WINDOW, max_windows: int = 20):
        if not 1 <= window <= MAX_LOG_WINDOW:
            raise SystemExit(f"--log-window must be 1..{MAX_LOG_WINDOW}: HyperEVM refuses wider eth_getLogs ranges")
        self.factory = to_checksum_address(factory)
        self.wallet = wallet
        self.sender = to_checksum_address(sender)
        self.dry = dry
        self.state_path = state_path
        self.window = window
        self.max_windows = max_windows
        self.next_block = start_block
        self.fast_block = 0  # the last block the fast loop read `Placed` in
        self.pools: list[str] = []
        self.accounts: dict[str, dict] = {}  # exam or stage -> {"pool", "kind"}
        self.done: set[str] = set()
        self.orders: dict[str, tuple[int, int]] = {}  # exam -> the window of its waiting order
        self.tried: dict[str, int] = {}  # exam -> the block its last fill was sent for
        self.limit_said: dict[str, int] = {}  # pool -> the `nextPayAt` its waiting for the daily limit was logged for
        self.triggers: dict[tuple[str, int, bool], int] = {}  # (exam, asset, is_take) -> the block it acts from
        self.trigger_tried: dict[tuple[str, int, bool], int] = {}  # -> the block it was last looked at for
        self.filler: str | None = None
        # With the stops' loop (ops/stop_filler.py, --stops loop) the stops and takes are its, in a thread of its own, and
        # every transaction of the key goes out through its one sender (`outbox`). Without it, as before: the fast loop
        # tries them and each send is c.send_tx's.
        self.stop_loop = None
        self.outbox = None
        # The fast loop's own eth_getLogs (the blocks before the first step the stops' loop hands in, those it skipped,
        # and an account sold in them), in windows the keeper's node takes (`LogWindow`).
        self.logs = LogWindow(MAX_LOG_WINDOW)
        # The slow pass's own (`scan`), `--log-window` at the widest.
        self.scan_logs = LogWindow(window)
        self.fed_at: float | None = None  # when the stops' loop last handed a step in (`fed_step`)
        self.unfed_said = False
        # With Multicall3 on the chain (`main` looks at the start) the slow pass asks its reads in batches (`Reads`);
        # without, and until the node's numbering is measured, each is asked alone as before.
        self.multicall = False
        self.ahead: int | None = None  # how the node numbers the block of a call (`numbering`); None: not measured
        self.ahead_at = 0.0
        self.reads = Reads()  # the pass's batch; empty outside a pass, and every read is then asked alone
        self.rule_assets: dict[str, list[int]] = {}  # an account -> the assets of its rules, written once when it is made
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if to_checksum_address(state["factory"]) != self.factory:
                raise SystemExit(f"{state_path} belongs to another factory")
            # A state is the chain's it was written on. One from before the chain id was written is testnet's: a
            # mainnet run never takes it, nor any state of another chain (review of #300).
            chain = state.get("chain_id")
            if (chain is not None and chain != c.CHAIN_ID) or (chain is None and c.NETWORK.name == "mainnet"):
                raise SystemExit(f"{state_path} is not a state of chain {c.CHAIN_ID}; move it away to start over")
            self.next_block = int(state["next_block"])
            self.pools = [to_checksum_address(p) for p in state["pools"]]
            self.accounts = {to_checksum_address(a): v for a, v in state["accounts"].items()}
            self.done = {to_checksum_address(a) for a in state.get("done", [])}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"factory": self.factory, "chain_id": c.CHAIN_ID, "next_block": self.next_block,
                                   "pools": self.pools,
                                   "accounts": self.accounts, "done": sorted(self.done)}, indent=2) + "\n")
        tmp.replace(self.state_path)

    def live(self) -> list[str]:
        return [a for a in self.accounts if a not in self.done]

    def is_filler(self) -> bool:
        if self.filler is None:
            self.filler = to_checksum_address(view(self.factory, "filler()", "address"))
        return self.filler == self.sender

    # ── calls ────────────────────────────────────────────────────────────────────────

    def send(self, to: str, sig: str, types=(), args=()) -> bool:
        """Sends a slow call if the contract would take it now. True if it went out (or would have)."""
        data = c.encode_call(sig, list(types), list(args))
        try:
            c.rpc("eth_estimateGas", [{"from": self.sender, "to": to, "data": "0x" + data.hex()}])
        except RuntimeError as exc:
            log("not_due", to=to, call=sig.split("(")[0], reason=refused(exc))
            return False
        if self.dry:
            log("would_send", to=to, call=sig.split("(")[0])
            return True
        try:
            rcpt = self.send_tx(to, data)
            log("sent", to=to, call=sig.split("(")[0], tx=rcpt["transactionHash"])
            return True
        except Exception as exc:  # one failed call must not stop the pass
            log("send_failed", to=to, call=sig.split("(")[0], error=refused(exc))
            return False

    def fill(self, exam: str) -> dict | None:
        """The fast loop's fill: sent at once with a fixed gas, no estimate. The receipt, or None."""
        if self.dry or not self.is_filler():
            log("would_fill", exam=exam)
            return None
        try:
            rcpt = self.send_tx(exam, c.encode_call("fill()"), gas=FILL_GAS)
        except Exception as exc:
            log("fill_failed", exam=exam, error=refused(exc))
            return None
        late = [filled_of(l) for l in rcpt.get("logs", []) if l["topics"] and l["topics"][0] == FILLED]
        log("filled", exam=exam, tx=rcpt["transactionHash"], block=int(rcpt["blockNumber"], 16),
            late=late[0][0] if late else None, source=late[0][1] if late else None)
        return rcpt

    def send_tx(self, to: str, data: bytes, gas: int | None = None) -> dict:
        """One transaction from the keeper's wallet, its receipt waited for: handed to the stops' loop's one sender when
        that runs, as c.send_tx otherwise."""
        if self.outbox is None:
            return c.send_tx(self.wallet, to, data, gas=gas)
        return self.outbox.send_and_wait(to, data, gas=gas)

    # ── the slow pass ────────────────────────────────────────────────────────────────

    def scan(self, latest: int) -> None:
        for p in self.reads.view(self.factory, "pools()", "address[]"):
            p = to_checksum_address(p)
            if p not in self.pools:
                self.pools.append(p)
                log("pool_found", pool=p)
        end = min(latest, self.next_block + self.window * self.max_windows - 1)
        while self.pools and self.next_block <= end:
            hi = min(self.next_block + self.window - 1, end)
            try:
                # In windows the node takes (`LogWindow`, `--log-window` the widest): a node of a narrower limit
                # (Alchemy's free plan: 10 blocks) is read in smaller steps instead of stopping the pass.
                logs, top = self.scan_logs.read(
                    c.rpc, {"address": self.pools, "topics": [[EXAM_SOLD, FUNDED]]}, self.next_block, hi,
                    passes=(requests.ConnectionError, c.NodeBusy), unsure=(requests.Timeout,))
            except Exception as exc:
                log("scan_stopped", at_block=self.next_block, error=str(exc)[:200])
                break
            for l in logs:
                kind = "exam" if l["topics"][0] == EXAM_SOLD else "stage"
                a = to_checksum_address("0x" + l["topics"][1][-40:])
                self.accounts.setdefault(a, {"pool": to_checksum_address(l["address"]), "kind": kind})
            self.next_block = top + 1
            self.save()
            if top < hi:
                break  # the node's logs end before `hi`: the rest is read at the next pass

    def account_pass(self, a: str, now: int) -> None:
        info = self.accounts[a]
        view, call_view = self.reads.view, self.reads.call_view  # the pass's batch; a read not in it is asked alone
        if call_view(info["pool"], "accountOf(address)", ["address"], [a], ["uint8", "uint8", "bool"])[2]:
            self.done.add(a)  # released, funded or claimed: the pool holds it done
            return
        status, _, _, passed_at = call_view(a, "result()", [], [], ["uint8", "address", "uint64", "uint64"])
        status = EXAM_STATUS[status]
        broken = over = snapshot_due = False
        if status == "Active":
            try:
                broken = view(a, "violation()", "uint8") != 0
            except Exception as exc:  # a held asset with no price; expire reads none
                log("violation_unread", account=a, error=str(exc)[:200])
            deadline = view(a, "deadline()", "uint64")
            last_fill = view(a, "lastFillAt()", "uint64")
            max_idle = view(a, "terms()", EXAM_TERMS)[3]
            over = (deadline and now > deadline) or (max_idle and now > last_fill + max_idle)
            snapshot_due = core.in_checkpoint_window(now) and view(a, "day()", "uint32") < now // 86_400
            try:
                self.refresh_triggers(a)
            except Exception as exc:
                log("triggers_unread", account=a, error=str(exc)[:200])
        fund_window = view(info["pool"], "fundWindow()", "uint32") if info["kind"] == "exam" else 0
        for call in account_calls(info["kind"], status, now, passed_at=passed_at, fund_window=fund_window,
                                  broken=bool(broken), over=bool(over), snapshot_due=bool(snapshot_due)):
            if call in ("checkpoint", "breach", "expire"):
                self.send(a, f"{call}()")
            elif self.send(info["pool"], f"{call}(address)", ["address"], [a]) and not self.dry:
                self.done.add(a)

    def pool_pass(self, pool: str, now: int) -> None:
        view = self.reads.view  # the pass's batch; a read not in it (a payout, say) is asked alone
        head = view(pool, "head()", "uint256")
        count = view(pool, "payoutCount()", "uint256")
        if head >= count:
            return
        # What the daily limit leaves: with no room the keeper sends no `pay` for a payable payout.
        room = min(PAY_PER_CALL, view(pool, "payRoom()", "uint256"))
        rows = []
        end = min(count, head + SCAN_PAYOUTS)
        found = 0
        early = False
        for pid in range(head, end):
            p = view(pool, "payout(uint256)", PAYOUT, ["uint256"], [pid])
            rows.append((PAYOUT_STATE[p[4]], p[3], bool(p[7])))
            # `pay` stops at a waiting payout whose 72 hours are not over: nothing after it is read.
            if rows[-1][0] == "Due" and now < rows[-1][1]:
                early = True
                break
            if payable(*rows[-1], now):
                found += 1
                if found > room:
                    break  # one more than the day has room for: enough to know that one waits
        reach = pay_reach(rows, now, room)
        if room == 0 and found:
            # A payout may be paid and the day's two are settled: it waits, in its place, for `nextPayAt`. Said only
            # with no room left: while there is room, `nextPayAt` is not yet the time this payout waits for.
            next_at = view(pool, "nextPayAt()", "uint64")
            if self.limit_said.get(pool) != next_at:
                self.limit_said[pool] = next_at
                log("pay_waits_limit", pool=pool, room=room, next_at=next_at)
        if reach:
            self.send(pool, "pay(uint256)", ["uint256"], [reach])
        elif not early and found == 0 and count > end:
            # Every payout within the keeper's reach waits for its approval, and there are more behind them.
            # They are paid once the head moves: the approver approves the ones in front, or their 7 days come.
            log("pay_out_of_reach", pool=pool, head=head, looked=len(rows), beyond=count - end)

    def slow_pass(self) -> None:
        # The head with its time, in one request: the batch's call is held against both (`read_all`).
        latest, latest_t = head(c.rpc)
        self.reads = self.read_all(latest, latest_t)
        try:
            # The time of the head: the batch's own (the call that read the accounts says it), or the head's.
            now = self.reads.time if self.reads.time is not None else latest_t
            self.scan(latest)
            for a in self.live():
                try:
                    self.account_pass(a, now)
                except Exception as exc:
                    log("account_failed", account=a, error=str(exc)[:200])
            for p in self.pools:
                try:
                    self.pool_pass(p, now)
                except Exception as exc:
                    log("pool_failed", pool=p, error=str(exc)[:200])
        finally:
            calls, self.reads = self.reads.calls, Reads()  # the batch is the pass's: nothing later is answered from it
        self.save()
        log("pass_done", pools=len(self.pools), live=len(self.live()), next_block=self.next_block, batches=calls)

    # ── the pass's reads, in batches ─────────────────────────────────────────────────

    def measure_numbering(self, tries: int = 3) -> int | None:
        """How the keeper's node numbers the block of a call at `latest` (`numbering`), measured at the first pass and
        every NUMBERING_EVERY_S after: the block a batch's reads are of is its number less this. None while it is not
        measured (a block came between the reads each time): the pass then asks each read alone. A node whose number
        cannot be read at all ends the batches for this run, said once."""
        if self.ahead is not None and time.monotonic() - self.ahead_at < NUMBERING_EVERY_S:
            return self.ahead
        for _ in range(tries):
            try:
                got = numbering(c.rpc, lambda: int(c.rpc("eth_blockNumber"), 16))
            except BadNumbering as exc:
                self.multicall = False
                log("batches_off", why=str(exc)[:200], then="every read is asked alone, as before")
                return None
            except Exception as exc:
                log("numbering_unread", error=refused(exc))
                return self.ahead
            if got is not None:
                if self.ahead is not None and got != self.ahead:
                    log("numbering_changed", was=self.ahead, now=got)
                self.ahead, self.ahead_at = got, time.monotonic()
                return got
        return self.ahead

    def account_reads(self, a: str, day: bool) -> tuple[str, list, list]:
        """The reads `account_pass` makes of an account, for the pass's batch: its own, and its stops' (the rules while
        its assets are not known; of each asset its stop, its take and its position once they are). `violation()`
        goes last in the call: it reads prices, and a read of a price that fails may burn the call's gas."""
        pool = self.accounts[a]["pool"]
        reads = [(pool, c.encode_call("accountOf(address)", ["address"], [a])), (a, c.encode_call("result()")),
                 (a, c.encode_call("deadline()")), (a, c.encode_call("lastFillAt()")), (a, c.encode_call("terms()"))]
        if day:
            reads.append((a, c.encode_call("day()")))
        return a, reads + self.stop_reads(a), [(a, c.encode_call("violation()"))]

    def stop_reads(self, a: str) -> list:
        assets = self.rule_assets.get(a)
        if assets is None:
            return [(a, c.encode_call("rules()"))]
        return [(a, c.encode_call(sig, ["uint32"], [asset])) for asset in assets
                for sig in ("stopOf(uint32)", "takeOf(uint32)", "positionOf(uint32)")]

    def read_all(self, latest: int, latest_t: int | None = None) -> Reads:
        """The reads of this pass, asked in batches of at most BATCH: the factory's pools, each pool's queue of payouts,
        and each live account's own reads with its stops', an account never cut in two. An account met for the first
        time has its rules read in the first call and its stops in a second: an exam's assets are written once, when
        it is made, and are kept from then on. Without Multicall3, or with the node's numbering not measured, the
        batch is empty and every read is asked alone, as before. `latest`, `latest_t`: the head read just before,
        with its time, which the batch's first call is held against (`ran_in`)."""
        reads = Reads()
        if not self.multicall:
            return reads
        ahead = self.measure_numbering()
        if ahead is None:
            return reads
        # `day()` is read in the day's first minutes only; the chain's clock is not known before the call, so by this
        # machine's with five minutes either side (a read left out is asked alone).
        day = any(core.in_checkpoint_window(int(time.time()) + d) for d in (-300, 0, 300))
        live = self.live()
        groups = [(self.factory, [(self.factory, c.encode_call("pools()"))], [])]
        groups += [(p, [(p, c.encode_call(sig)) for sig in ("head()", "payoutCount()", "payRoom()", "fundWindow()")], [])
                   for p in self.pools]
        groups += [self.account_reads(a, day) for a in live]
        reads.load(groups, c.rpc, ahead)
        new = []
        for a in live:
            if a not in self.rule_assets:
                try:
                    self.rule_assets[a] = [int(x) for x in reads.read(a, "rules()", [], [], [RULES])[0][3]]
                except Exception:
                    continue  # not read: its stops are read the old way in this pass
                new.append((a, [(a, c.encode_call("result()"))] + self.stop_reads(a), []))
        if new:
            reads.load(new, c.rpc, ahead)
        if reads.block is not None and not ran_in(c.rpc, reads.block, reads.time, (latest, latest_t)):
            # The call says another block than it ran in: the head's own time under another number, the head's number
            # with another time, or a time the node does not record for that number. The node numbers its calls another
            # way now; handed over, a batch labelled a block late would be taken as read after that block's events.
            log("numbering_changed", head=latest, call=reads.block, then="measured again; this pass's stops read alone")
            self.ahead = None
            reads.block_of.clear()
        reads.assets = {a: self.rule_assets[a] for a in live
                        if a in self.rule_assets and reads.whole(a, self.rule_assets[a])}
        for a in [a for a in self.rule_assets if a in self.done]:
            del self.rule_assets[a]
        return reads

    # ── the fast loop ────────────────────────────────────────────────────────────────

    def fast_step(self, latest: int, fed: tuple[int, list[dict]] | None = None) -> list[str]:
        """Reads the `Placed` events since the last look, and the pools' new exams and stages with them, and
        sends the fills that are due for the next block. The exams it sent a fill for.

        `fed`: with the stops' loop, the logs it read of the blocks from `fed[0]` to `latest` (`fed_step`): they are
        taken as they are and nothing is asked of the node, except for blocks before them that this loop has not
        read (the first look after a start), which it reads itself as before.

        A new account is read here, not only in the slow pass: an order placed within seconds of the purchase
        would otherwise fall before the account was known and lapse (review of #270). A fill the exam refuses
        (the book past the ceiling, say) is sent again for the next block while the window lasts, as the terms
        say an order waits for a later block of its window; once a block."""
        watched = self.live() + self.pools
        if not watched:
            self.fast_block = latest
            return []
        lo = max(self.fast_block + 1, latest - MAX_LOG_WINDOW + 1)
        if lo <= latest:
            new: list[str] = []
            # Handed in from block `fed[0]` on: only the blocks before it are asked for here, and none when there are none.
            own = latest if fed is None else min(latest, fed[0] - 1)
            for l in (self._own_logs({"address": watched, "topics": [[PLACED, EXAM_SOLD, FUNDED, *TRIGGER_TOPICS]]}, lo, own)
                      if lo <= own else []):
                if l["topics"][0] == PLACED:
                    self._order(l)
                    continue
                if l["topics"][0] in TRIGGER_TOPICS:
                    self._trigger(l)
                    continue
                a = to_checksum_address("0x" + l["topics"][1][-40:])
                if a not in self.accounts:
                    kind = "exam" if l["topics"][0] == EXAM_SOLD else "stage"
                    self.accounts[a] = {"pool": to_checksum_address(l["address"]), "kind": kind}
                    new.append(a)
            if new:  # their orders in the same blocks, which the read above could not name
                # Up to the head, the blocks handed in too: the stops' loop read those without knowing the account.
                for l in self._own_logs({"address": new, "topics": [[PLACED, *TRIGGER_TOPICS]]}, lo, latest):
                    self._trigger(l) if l["topics"][0] in TRIGGER_TOPICS else self._order(l)
            for l in sorted((l for l in (fed[1] if fed is not None else []) if lo <= int(l["blockNumber"], 16) <= latest),
                            key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16))):
                # The stops' loop read these, the orders of an account sold in the same blocks among them.
                if l["topics"][0] == PLACED:
                    self._order(l)
                elif l["topics"][0] in (EXAM_SOLD, FUNDED):
                    a = to_checksum_address("0x" + l["topics"][1][-40:])
                    if a not in self.accounts:
                        kind = "exam" if l["topics"][0] == EXAM_SOLD else "stage"
                        self.accounts[a] = {"pool": to_checksum_address(l["address"]), "kind": kind}
            self.fast_block = latest
        sent = []
        for exam, (start, until) in list(self.orders.items()):
            if latest + 1 > until:
                del self.orders[exam]  # lapsed; the next Placed replaces it
                self.tried.pop(exam, None)
            elif fill_now(start, until, latest) and self.tried.get(exam) != latest:
                self.tried[exam] = latest
                sent.append(exam)
                if self.fill(exam) is not None or self.dry or not self.is_filler():
                    del self.orders[exam]
                    self.tried.pop(exam, None)
        return sent + (self.trigger_step(latest) if self.stop_loop is None else [])  # with the loop, the stops are its

    def fed_step(self, wait: float) -> list[str]:
        """With the stops' loop: its looks at the head and its one read of each block's logs serve this loop too. Waits
        up to `wait` seconds for its next step, takes every step handed in since, and makes one fast step to the last
        head with their logs: no look at the head and no eth_getLogs of this loop's own. None handed in for `UNFED_S`
        is an alarm (`_unfed`): this loop stands with that one."""
        feed = self.stop_loop.feed
        if self.fed_at is None:
            self.fed_at = time.monotonic()
        try:
            steps = [feed.get(timeout=wait)]
        except queue.Empty:
            self._unfed()
            return []
        while True:
            try:
                steps.append(feed.get_nowait())
            except queue.Empty:
                break
        self.fed_at = time.monotonic()
        if self.unfed_said:
            self.unfed_said = False
            log("keeper_fed_again", block=self.fast_block, head=steps[-1][1])
        lo, head, logs = steps[-1][0], steps[-1][1], list(steps[-1][2])
        if head <= self.fast_block:
            return []
        for first, last, more in reversed(steps[:-1]):
            if last + 1 < lo:
                # The loop skipped blocks here (it was far behind). The steps before are left out, their logs too: this
                # loop reads those blocks itself, and an older order taken after them would replace a newer one.
                break
            lo, logs = min(lo, first), list(more) + logs
        return self.fast_step(head, fed=(lo, logs))

    def _unfed(self) -> None:
        """No step handed in for `UNFED_S`: an alarm once, through the stops' loop's, until one comes. The fast loop then
        stands with it, and an order placed meanwhile waits unfilled: the stops' loop's own alarms say why it stands
        (its node failing, behind), this one says the orders stand too, never in silence."""
        waited = time.monotonic() - self.fed_at
        if self.unfed_said or waited < UNFED_S:
            return
        self.unfed_said = True
        self.stop_loop.alarm("keeper_unfed", seconds=int(waited), block=self.fast_block, orders=len(self.orders),
                             why="the fast loop takes its blocks from the stops' loop, and none came: no order placed "
                                 "meanwhile is sent a fill until one does (see the stops' loop's own alarms)")

    def _own_logs(self, query: dict, lo: int, hi: int) -> list[dict]:
        """The fast loop's own read of blocks `lo` to `hi`, in windows the keeper's node takes (`LogWindow`): the
        blocks before the stops' loop's first step after a start are fifty, which a node of a narrower limit refuses
        in one read, and the fast loop would stand on them for good. A read the node's logs ended in before `hi`
        raises, and the next step reads it again from `lo`: the fast loop's block moves only with every block read."""
        # A busy node is raised as it comes: a narrower window would only ask it more (c.rpc's own ladder of HTTP 429
        # ends in "rate limited", raised as it comes too).
        got, top = self.logs.read(c.rpc, query, lo, hi, passes=(requests.ConnectionError, c.NodeBusy),
                                  unsure=(requests.Timeout,))
        if top < hi:
            raise RuntimeError(f"eth_getLogs: the node's logs end at block {top}, before {hi}; read again next time")
        return got

    def trigger_step(self, latest: int) -> list[str]:
        """The stops and takes armed for the next block: each is tried, once a block, by a call that changes
        nothing, and carried out if the exam would take it (the oracle has reached it). The exams it sent one for."""
        sent = []
        for key, armed_at in list(self.triggers.items()):
            if armed_at > latest + 1 or self.trigger_tried.get(key) == latest:
                continue
            self.trigger_tried[key] = latest
            exam, asset, is_take = key
            if self.account_done(exam):
                self.triggers.pop(key, None)
                continue
            data = c.encode_call("fillTrigger(uint32,bool)", ["uint32", "bool"], [asset, is_take])
            try:
                c.rpc("eth_call", [{"from": self.sender, "to": exam, "data": "0x" + data.hex()}, "latest"])
            except RuntimeError:
                continue  # not reached, or not this keeper's to carry out yet
            sent.append(exam)
            if self.dry or not self.is_filler():
                log("would_fill_trigger", exam=exam, asset=asset, take=is_take)
                continue
            try:
                rcpt = c.send_tx(self.wallet, exam, data, gas=FILL_GAS)
                log("trigger_filled", exam=exam, asset=asset, take=is_take, tx=rcpt["transactionHash"],
                    block=int(rcpt["blockNumber"], 16))
            except Exception as exc:  # the next block's price may not hold it; tried again next block
                log("trigger_fill_failed", exam=exam, asset=asset, take=is_take, error=refused(exc))
        return sent

    def refresh_triggers(self, exam: str) -> None:
        """The exam's stops and takes as the contract holds them, read in every slow pass: a keeper started again
        knows the ones set while it was down, which the fast loop's window of logs no longer reaches (review of
        #297). With the stops' loop, read at one block and handed to it."""
        if self.stop_loop is not None:
            if exam in self.reads.assets:  # read in the pass's batch, at one block: handed over as it is
                self.stop_loop.reread(exam, batch=self.reads)
            else:
                self.stop_loop.reread(exam)
            return
        view = self.reads.view
        for asset in self.rule_assets.get(exam) or view(exam, "rules()", RULES)[3]:
            for is_take, getter in ((False, "stopOf(uint32)"), (True, "takeOf(uint32)")):
                px, _, armed_at, _ = view(exam, getter, TRIGGER, ["uint32"], [asset])
                key = (exam, int(asset), is_take)
                if px:
                    self.triggers[key] = int(armed_at)
                else:
                    self.triggers.pop(key, None)

    def account_done(self, exam: str) -> bool:
        return exam in self.done

    def _trigger(self, log_entry: dict) -> None:
        exam = to_checksum_address(log_entry["address"])
        asset = int(log_entry["topics"][1], 16)
        is_take = bool(int(log_entry["data"][2:66], 16))
        key = (exam, asset, is_take)
        if log_entry["topics"][0] == TRIGGER_SET:
            armed_at = int(log_entry["data"][2 + 64 * 3:2 + 64 * 4], 16)
            self.triggers[key] = armed_at
        else:
            self.triggers.pop(key, None)
        self.trigger_tried.pop(key, None)

    def _order(self, log_entry: dict) -> None:
        exam, start, until = placed_of(log_entry)
        self.orders[exam] = (start, until)
        self.tried.pop(exam, None)


# ── the measure ──────────────────────────────────────────────────────────────────────

def measure(exam: str, trader, asset: int, size: int, orders: int, out: pathlib.Path, timeout_s: int = 120) -> dict:
    """Places `orders` orders on the trader's own exam, one at a time, alternating buy and sell, and
    writes how late each one's fill landed (another process, the filler's keeper, fills them)."""
    late: list[int] = []
    rows = []
    for i in range(orders):
        is_buy = i % 2 == 0
        rcpt = c.transact(trader, exam, "place(uint32,bool,uint64)", ["uint32", "bool", "uint64"], [asset, is_buy, size])
        placed_block = int(rcpt["blockNumber"], 16)
        row = {"order": i, "buy": is_buy, "placed_block": placed_block, "late": None, "source": None, "lapsed": False}

        def ended() -> bool:
            """The order's end as the chain's events after its block say it, written to its row: its own fill, or its
            lapse. A fill of a stop or a take (a `Filled` beside a `TriggerFilled` in one transaction) is not the
            order's, nor is anything of another asset."""
            logs = c.rpc("eth_getLogs", [{"address": exam, "topics": [[FILLED, LAPSED, TRIGGER_FILLED]],
                                          "fromBlock": hex(placed_block), "toBlock": "latest"}])
            after = [l for l in logs if int(l["blockNumber"], 16) > placed_block and int(l["topics"][1], 16) == asset]
            stops = {l.get("transactionHash") for l in after if l["topics"][0] == TRIGGER_FILLED}
            filled = [l for l in after if l["topics"][0] == FILLED and l.get("transactionHash") not in stops]
            if filled:
                row["late"], row["source"] = filled_of(filled[0])
                row["filled_block"] = int(filled[0]["blockNumber"], 16)
                late.append(row["late"])
                return True
            if any(l["topics"][0] == LAPSED for l in after):
                row["lapsed"] = True
                return True
            return False

        started = time.time()
        while not ended():
            # The order is judged by the chain's events, never by `pending()` alone: a fill that lands between the
            # read of the events above and this read leaves no order waiting, and is in the events read next, at
            # once and whatever the time left. With neither a fill nor a lapse in them it is written as neither.
            if c.call_view(exam, "pending()", [], [], ["uint32", "bool", "uint64", "uint64"])[2] == 0:
                ended()
                break
            if time.time() - started >= timeout_s:
                row["lapsed"] = True  # still waiting when the measure's time ran out, the events just read
                break
            time.sleep(1)
        rows.append(row)
        log("measured", **row)
    summary = {"exam": exam, "orders": orders, "filled": len(late), **quantiles(late), "rows": rows}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    from ops import stop_filler  # it reads this module's events and numbers, so after them

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/ with a SimPoolFactory")
    p.add_argument("--wallet", default="keeper", help="the factory's filler, or any funded key on the network")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=30, help="seconds between slow passes")
    p.add_argument("--poll", type=float, default=0.5, help="seconds between looks at the head, in the fast loop")
    p.add_argument("--note-every", type=int, default=3_600, help="(multi_keeper's; not used here)")
    p.add_argument("--log-window", type=int, default=MAX_LOG_WINDOW)
    p.add_argument("--max-windows", type=int, default=20)
    p.add_argument("--state", help="the state file (default ops/state/sim-<deployment>.json)")
    p.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    p.add_argument("--from", dest="sender", help="address the calls are tried from in a dry run")
    p.add_argument("--measure", metavar="EXAM", help="place orders on this exam of the trader's and time the fills")
    p.add_argument("--trader-wallet", help="the exam's trader, for --measure")
    p.add_argument("--asset", type=int, help="the perp index --measure trades (default BTC's on the network: "
                   "testnet 3, mainnet 0)")
    p.add_argument("--size", type=int, default=12, help="size units per --measure order (BTC: 0.00012, over 10 USDC)")
    p.add_argument("--orders", type=int, default=20)
    p.add_argument("--measure-out", default="ops/state/sim-measure.json")
    stop_filler.add_args(p)
    sn.add_args(p, service=True)
    args = p.parse_args(argv)

    net = sn.selected(args, c.NETWORK)
    if args.measure:
        if not args.trader_wallet:
            raise SystemExit("--measure needs --trader-wallet, the exam's trader")
        sn.confirm_send(args, f"place {args.orders} orders on the exam {args.measure} from the key {args.trader_wallet!r}", c)
    else:
        sn.confirm_service(args, args.deployment, c, sends=not args.dry_run)
        if net.name == "mainnet":
            core.check_node(c.RPC_URL, c.CHAIN_ID)
    if args.asset is None:
        args.asset = sn.sim(net).index(sn.sim(net).measure_asset)
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    if args.measure:
        s = measure(to_checksum_address(args.measure), c.account(args.trader_wallet), args.asset, args.size,
                    args.orders, pathlib.Path(args.measure_out))
        print(json.dumps({k: s[k] for k in ("orders", "filled", "p50", "p99", "max") if k in s}))
        return 0
    wallet = None if args.dry_run else c.account(args.wallet)
    sender = wallet.address if wallet else (args.sender or c.address_of(args.wallet))
    state = pathlib.Path(args.state) if args.state else STATE_DIR / f"sim-{args.deployment}.json"
    k = SimKeeper(record["SimPoolFactory"], wallet, sender, args.dry_run, state, int(record["block"]),
                  window=args.log_window, max_windows=args.max_windows)
    counted = count_requests()
    k.multicall = multicall_or_not(c.rpc, "the keeper")
    thread = None
    poll = args.stop_poll or args.poll
    if args.stops == "loop" and not args.once:
        # The filler is known before the loop starts: its thread never waits on the keeper's node. A key that is not the
        # filler sends nothing of the stops, and its own sends stay c.send_tx's.
        filler = wallet is not None and k.is_filler()
        k.stop_loop = stop_filler.build(args, k, state, wallet=wallet if filler else None)
        if wallet is not None and not filler:
            # Said at the start, not after the first stop reached and not carried out: this key carries out no stop.
            k.stop_loop.alarm("stop_key_not_filler", key=wallet.address, filler=k.filler,
                              why="the keeper's key is not the factory's filler: it sends no stop, every stop reached "
                                  "goes uncarried until the filler's key runs a keeper")
        k.outbox = k.stop_loop.sender
        # One look at the head and one read of each block's logs for both loops: the stops' loop hands its own on.
        k.stop_loop.share()
        thread = stop_filler.start(k.stop_loop, poll)
    last_slow = 0.0
    alive_at = time.monotonic()
    while True:
        try:
            if time.time() - last_slow >= args.every:
                k.slow_pass()
                last_slow = time.time()
            if args.once:
                return 0
            if time.monotonic() - alive_at >= ALIVE_EVERY_S:
                seconds, alive_at = time.monotonic() - alive_at, time.monotonic()
                log("keeper_alive", block=k.fast_block, live=len(k.live()), orders=len(k.orders),
                    seconds=round(seconds), requests=counted.taken())
            if thread is not None:
                thread = stop_filler.keep_alive(k.stop_loop, thread, poll)
                k.fed_step(args.poll)  # waits for the stops' loop's next step: no look at the head of its own
                continue
            latest = int(c.rpc("eth_blockNumber"), 16)
            if latest > k.fast_block:
                k.fast_step(latest)
        except Exception as exc:  # the RPC failed; the next look retries
            log("loop_failed", error=str(exc)[:200])
            if args.once:
                return 1
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
