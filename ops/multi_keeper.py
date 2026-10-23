"""The keeper of a pool of many traders (MultiPool): every call the pool leaves to anyone, made when
the pool would take it, and the noter's call, made from the accounts' fills.

    spike/.venv/bin/python -m ops.multi_keeper --deployment multi-1 --once --dry-run   # what is due
    spike/.venv/bin/python -m ops.multi_keeper --deployment multi-1 --once             # one pass
    spike/.venv/bin/python -m ops.multi_keeper --deployment multi-1                    # a service loop

On every pass, in this order:

  exams sold      → found from the pool's ExamSold events, read in windows of --log-window blocks
                    from where the last pass stopped (the --state file). An exam stays on the list
                    until its size is given back or its account taken.
  an exam running → checkpoint in the first minutes of a UTC day; breach if a rule is broken now;
                    expire once past its deadline or its idle limit
  an order        → fill the order an exam has waiting, as soon as it would take it: the trader may
                    not fill their own (deep audit N6)
  an exam over    → release once it failed or lapsed, once its pass went untaken past the claim
                    window, or once the pool is over
  a free seat     → sweep what reached it after it was freed
  an armed seat   → rearm if the money has not shown a minute after it was sent; open once the
                    seat has rested, purging the registry's spoiled keys first if they refuse it;
                    finish once the pool is over or the claim window has passed unopened (the
                    trader gets the price back)
  a funded seat   → what any funded account gets from the core keeper (ops/keeper.py: checkpoint
                    and breach, and once stopped recut and settleFunded); then endSeat once the
                    pool is over, hitTarget if the plan ends a stage at its target and the account
                    is flat past it, closeIdle once it has gone the plan's idle limit untraded
  a settled seat  → finish: the seat is freed, or held for its trader after a target
  the keys        → keys_short on every pass while the registry has no free key beyond one for
                    every seat spoken for: nothing is sold until the operator publishes more

The windows it judges (a checkpoint's, an exam's deadline, a round's end) are the chain's: the time
of the latest block, the host's clock only when the node will not say (the cloud audit, N15). An
exam whose price does not answer is still expired once past its time (N12). A state file from before
the holders were kept is scanned again from the first block (N13).

On a pool owned by PoolShares, its holders' calls too (holders found from its DepositRecognized
and Seeded events, read with the exams'):

  tickets         → settle, naming the open tickets that hold a deposit (or, once a raise that
                    fell short has taken its price, anything to hand back), and whenever closed
                    tickets are still listed, so that what they hold is swept
  the raise       → launch once it has its target within its thirty days; once it has fallen
                    short, reclaim what the pool holds for the creator, refund every holder, and
                    payFee for the platform
  the round       → renew once the end those who asked to leave asked for has passed, never in
                    the term's last days (DECISIONS 103; audit M-2); closeRound once the pool has
                    stopped and ended
  what is owed    → pay every holder something is owed to

The noter. A trade that opens and closes between two looks leaves the account flat at both, and
to the chain that is no trade at all: closeIdle would end an account whose trader is trading. The
plan names one address that may record such a trade (`noteTrade`), and this keeper is it when its
wallet is. It reads a funded seat's fills on Hyperliquid (userFillsByTime) since the pool last saw
the seat active, every --note-every seconds and always before it would close the seat as idle. A
fill since then is noted, and the seat is not closed; if the fills cannot be read, nothing is
closed on that pass. A keeper that is not the noter still reads them before closeIdle, and leaves
alone a seat that traded.

Every call is first tried with eth_estimateGas from the keeper's own address and sent only if the
contract would take it; a refusal is logged by its error's name (ops/contract_errors.py). The rules
live in the contracts and the keeper keeps no second copy of them, only the judgement of when a
call is worth its gas. It holds no trader's key and makes no call only a trader or the owner may:
`--wallet` is any funded testnet key, and the plan's noter for noteTrade. Testnet only.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from ops.contract_errors import refusal
from ops.deploy_multi import PLAN_T, RULES_T
from spike.hlspike import common as c

ZERO = core.ZERO
CANCEL = core.CANCEL
STOP = f"(address,{CANCEL},uint32[],bytes32)"
STATE_DIR = core.STATE_DIR
MAX_LOG_WINDOW = core.MAX_LOG_WINDOW
EXAM_SOLD = "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64)").hex()
DEPOSIT_RECOGNIZED = "0x" + keccak(text="DepositRecognized(address,address,uint256,uint256)").hex()
SEEDED = "0x" + keccak(text="Seeded(address,uint256)").hex()
# PoolShares' constants the keeper plans with; the tests hold them against the source.
SWEEP_MIN = 100_000_000
MAX_PER_POINT = 8
PAGE = 256
# A ticket costs its opener only gas, so the keeper's reads of them are bounded per pass (deep
# audit F15): a few pages of the open list, the tickets it has not read yet first, then the empty
# ones again, the longest-unread first, each no sooner than RECHECK_EVERY passes after the last.
PAGES_PER_PASS = 4
NEW_PER_PASS = 32
RECHECK_PER_PASS = 32
RECHECK_EVERY = 10

# MultiPool's and PaperExam's names and layouts as the keeper reads them; the tests hold each
# against the Solidity source.
ARM_WAIT = 60
# Spoiled keys one purgeSpoiled call retires. Each cost whoever spoiled it 1 USDC; until they are
# retired, more than sixteen of them on top of the registry's list refuse every opening.
PURGE_PER_CALL = 64
EXAM_STATUS = ("None", "Active", "Passed", "Breached", "Expired")
SEAT_STATE = ("None", "Free", "Armed", "Funded", "Waiting")
EXAM_STATE = ("None", "Sold", "Claimed", "Released")
SEAT_INFO = ["uint8", "uint8", "bool", "address", "uint64", "uint64", "uint64", "uint16"]
EXAM_INFO = ["uint8", "uint8", "address", "bool"]
EXAM_TERMS = "(uint64,uint16[],uint32,uint32,uint16,uint32,uint16,uint16)"
PLAN_FIELDS = ("menu", "exam_rules", "exam", "funded_rules", "funded_target_bps", "trader_share_bps",
               "after_target", "in_work_bps", "max_seats", "claim_window", "term", "max_retakes", "funded_idle",
               "free_per_month", "inviter", "noter", "terms", "terms_book")
TRADER_STOPS = 0  # AfterTarget.TraderStops: the plan has no target, so hitTarget is never due
IDLE, FUNDED, CLOSING = core.IDLE, core.FUNDED, core.CLOSING
BPS = 10_000

log = core.log
view = core.view


# ── what is due, from what was read ──────────────────────────────────────────────────

def exam_calls(state: str, status: str, now: int, over: bool, *, passed_at: int = 0, claim_window: int = 0,
               broken: bool = False, deadline: int = 0, last_fill: int = 0, max_idle: int = 0,
               day: int = 0, pending: bool = False) -> list[str]:
    """The calls worth trying on one exam, in order. `state` is the pool's (ExamState), `status`
    the exam's own; an exam the pool no longer holds as sold needs nothing ever again."""
    if state != "Sold":
        return []
    if status == "Active":
        if over:
            return ["release"]
        # A broken rule first: the day's snapshot is refused while one stands (deep audit, F12).
        if broken:
            return ["breach"]
        # An order waiting is filled as soon as the exam takes it: the trader may not fill their own
        # order, so nobody who placed it picks its block (deep audit, N6).
        calls = ["fill"] if pending else []
        if core.in_checkpoint_window(now) and day < now // 86_400:
            calls.append("checkpoint")
        if (deadline and now > deadline) or (max_idle and now > last_fill + max_idle):
            return calls + ["expire"]
        return calls
    if status in ("Breached", "Expired") or over:
        return ["release"]
    if status == "Passed" and now > passed_at + claim_window:
        return ["release"]
    return []


def seat_calls(state: str, stage: int, now: int, over: bool, *, spot: int = 0, size: int = 0, armed_at: int = 0,
               rest_until: int = 0, wait_until: int = 0, claim_window: int = 0) -> list[str]:
    """The pool's calls worth trying on one seat, in order, from its state and its stage. A funded
    stage's own calls (the core keeper's, and those that need the account read) come separately."""
    if state == "Free":
        return ["sweep"] if spot > 0 else []
    if state == "Armed":
        if over:
            return ["finish"]
        # An account that could not open within the claim window (no key, say) frees its seat: the
        # size comes back to the pool and the price to the trader (deep audit F18, N2).
        late = claim_window != 0 and now > armed_at + claim_window
        if spot < size:
            if late:
                return ["finish"]
            return ["rearm"] if now > armed_at + ARM_WAIT else []
        calls = ["open"] if now > rest_until else []
        return calls + ["finish"] if late else calls
    if state in ("Funded", "Waiting") and stage == IDLE:
        if state == "Waiting" and not over and now <= wait_until:
            return []
        return ["finish"]
    return []


def funded_calls(over: bool, *, has_target: bool, flat: bool, equity: int, start: int, target_bps: int,
                 idle_due: bool) -> list[str]:
    """The pool's calls on a seat whose stage is funded, after the core keeper's turn on it."""
    if over:
        return ["endSeat"]
    if has_target and flat and equity >= start + start * target_bps // BPS:
        return ["hitTarget"]
    if idle_due:
        return ["closeIdle"]
    return []


def traded_since(fills: list, since: int) -> bool:
    """Whether any fill came after `since` (seconds); Hyperliquid stamps fills in milliseconds."""
    return any(int(f["time"]) > since * 1000 for f in fills)


def raise_calls(launched: bool, now: int, raise_ends: int, value: int, target: int, pool_value: int,
                fee_owed: int) -> list[str]:
    """PoolShares' calls on its raise, in order; `refund` stands for one call for each holder."""
    if launched:
        return []
    if now <= raise_ends:
        return ["launch"] if value >= target else []
    return (["reclaim"] if pool_value else []) + ["refund"] + (["payFee"] if fee_owed else [])


def round_calls(over: bool, exit_shares: int, late: bool) -> list[str]:
    """The call that closes the round of those who asked to leave, if one is due: once the end they
    asked for has passed, never in the term's last days (the terms, D5 and D6; DECISIONS 103)."""
    if exit_shares == 0:
        return []
    if over:
        return ["closeRound"]
    return ["renew"] if late else []


def is_late(now: int, exit_shares: int, reserve_for: int, ends: int) -> bool:
    """A round asked out for an end that has passed, the term having renewed by itself since:
    PoolShares' `_late()`, which lets `renew` close it at any time (audit M-2)."""
    return exit_shares != 0 and now > reserve_for and reserve_for < ends


def tickets_due(balances: dict, threshold: int) -> list[str]:
    """The open tickets a settlement point should name, at most MAX_PER_POINT of them."""
    return [t for t, held in balances.items() if held >= threshold][:MAX_PER_POINT]


def found_in_logs(pool: str, shares: str | None, lo: int, hi: int) -> tuple[list[str], list[str]]:
    """The exams the pool's ExamSold events name in blocks [lo, hi], and the holders PoolShares'
    DepositRecognized and Seeded events name, in order, one call."""
    addresses = [pool] + ([shares] if shares else [])
    logs = c.rpc("eth_getLogs", [{"address": addresses, "topics": [[EXAM_SOLD, DEPOSIT_RECOGNIZED, SEEDED]],
                                  "fromBlock": hex(lo), "toBlock": hex(hi)}])
    exams, holders = [], []
    for entry in logs:
        topic, at = entry["topics"][0], to_checksum_address(entry["address"])
        named = to_checksum_address("0x" + entry["topics"][1][-40:])
        if topic == EXAM_SOLD and at == pool:
            exams.append(named)
        elif topic in (DEPOSIT_RECOGNIZED, SEEDED) and at == shares:
            holders.append(named)
    return exams, holders


def reverted(exc: Exception) -> bool:
    """Whether the node said the call reverts, as opposed to not answering."""
    text = str(exc)
    return "execution reverted" in text or "'code': 3" in text


def shares_of(pool: str) -> str | None:
    """The PoolShares contract that owns the pool, if one does: an owner with code that names this
    pool as its own. A person's address, or a contract with no such answer, is None. A node that does
    not answer raises: that is not knowing, and the keeper asks again (deep audit F14)."""
    owner = to_checksum_address(view(pool, "owner()", "address"))
    if c.rpc("eth_getCode", [owner, "latest"]) in ("0x", ""):
        return None
    try:
        named = view(owner, "pool()", "address")
    except RuntimeError as exc:
        if reverted(exc):
            return None
        raise
    return owner if to_checksum_address(named) == pool else None


# A plan's Rules before v3 had four fields: each asset's own cap came with v3 (the CTO, 21 October
# 2026). A plan kept by a pool of either is read by its own type (audit window 2, В6-4).
RULES_T_V2 = "(uint16,uint16,uint32,uint32[])"
PLAN_T_V2 = PLAN_T.replace(RULES_T, RULES_T_V2)
PLAN_HEAD_WORDS = 18  # the plan's head: four offsets (menu, two rules, exam) and fourteen fields


def plan_type(blob: bytes) -> str:
    """The plan's type, told by its encoding: how many words its head has, and where the exam rules
    put their assets (after a head of four words or five). Anything else is a plan of another
    version, read by the keeper of its own deployment's head."""
    def word(at: int) -> int:
        return int.from_bytes(blob[at:at + 32], "big")

    plan = word(0)
    head = word(plan) // 32
    if head != PLAN_HEAD_WORDS:
        raise SystemExit(f"a plan of another version ({head} words in its head, this keeper reads "
                         f"{PLAN_HEAD_WORDS}): run the keeper of its deployment's head")
    rules = plan + word(plan + 32)
    fields = word(rules + 96) // 32
    if fields not in (4, 5):
        raise SystemExit(f"a plan whose rules have {fields} fields: not one this keeper reads")
    return PLAN_T if fields == 5 else PLAN_T_V2


def plan_of(pool: str) -> dict:
    """The plan the pool holds, by field, read from the contract it keeps it in. A plan from before
    v3 reads with no cap of its own for any asset."""
    holder = to_checksum_address(view(pool, "planData()", "address"))
    code = bytes.fromhex(c.rpc("eth_getCode", [holder, "latest"])[2:])
    if not code or code[0] != 0:
        raise SystemExit("that is not where a pool keeps its plan")
    plan = dict(zip(PLAN_FIELDS, abi_decode([plan_type(code[1:])], code[1:])[0]))
    for key in ("exam_rules", "funded_rules"):
        if len(plan[key]) == 4:
            plan[key] = (*plan[key], ())
    return plan


class MultiKeeper:
    def __init__(self, pool: str, wallet, sender: str, dry: bool, state_path: pathlib.Path, start_block: int,
                 window: int = MAX_LOG_WINDOW, max_windows: int = 20, note_every: int = 3_600, reader: str | None = None):
        if not 1 <= window <= MAX_LOG_WINDOW:
            raise SystemExit(f"--log-window must be 1..{MAX_LOG_WINDOW}: HyperEVM refuses wider eth_getLogs ranges")
        self.pool = to_checksum_address(pool)
        # The pool's and its shares contract's views that only read (MultiPoolReader, CTO 18 October).
        self.reader = to_checksum_address(reader) if reader else None
        self.wallet = wallet
        self.sender = to_checksum_address(sender)
        self.dry = dry
        self.state_path = state_path
        self.shares: str | None = None
        self.window = window
        self.max_windows = max_windows
        self.note_every = note_every
        self.next_block = start_block
        self.exams: list[str] = []
        self.holders: list[str] = []
        self.fills_read_at: dict[str, int] = {}  # funded seat -> when its fills were last read
        self.plan: dict | None = None
        self.owner_known = False  # whether shares_of has answered for sure
        self.last_refusal: str | None = None  # the contract's reason for the last call gas() found refused
        self._registry: str | None = None
        self.passes = 0
        self.ticket_read: dict[str, int | None] = {}  # open ticket -> the pass that last read it empty
        self.page_at = 0  # where the next pass goes on reading the open list
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if to_checksum_address(state["pool"]) != self.pool:
                raise SystemExit(f"{state_path} belongs to another pool")
            self.next_block = int(state["next_block"])
            self.exams = [to_checksum_address(e) for e in state["exams"]]
            self.holders = [to_checksum_address(h) for h in state.get("holders", [])]
            if "holders" not in state:
                # A state file from before the holders were kept: their events lie behind its place,
                # so the scan starts again from the first block (the cloud audit, N13).
                self.next_block = start_block
                log("holders_rebuilt", state=str(state_path), from_block=start_block)
            self.fills_read_at = {to_checksum_address(k): int(v) for k, v in state.get("fills_read_at", {}).items()}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pool": self.pool, "next_block": self.next_block, "exams": self.exams,
                                   "holders": self.holders, "fills_read_at": self.fills_read_at}, indent=2) + "\n")
        tmp.replace(self.state_path)

    # ── calls ────────────────────────────────────────────────────────────────────────

    def gas(self, to: str, sig: str, types=(), args=()) -> int | None:
        """What the call would cost, or None if the contract would refuse it now."""
        data = c.encode_call(sig, list(types), list(args))
        self.last_refusal = None
        try:
            return int(c.rpc("eth_estimateGas", [{"from": self.sender, "to": to, "data": "0x" + data.hex()}]), 16)
        except RuntimeError as exc:
            self.last_refusal = refusal(exc)
            log("not_due", to=to, call=sig.split("(")[0], reason=self.last_refusal)
            return None

    def due_of(self, sh: str, h: str) -> int:
        """What `h` is owed by the shares contract as their next call would see it: the reader's
        positionOf, or, for a record from before the reader, the same from the contract's getters."""
        if self.reader:
            return c.call_view(self.reader, "positionOf(address,address)", ["address", "address"], [sh, h],
                               ["uint256", "uint256", "uint256"])[2]
        due = view(sh, "owedOf(address)", "uint256", ["address"], [h])
        leaving = view(sh, "exitOf(address)", "uint256", ["address"], [h])
        r = view(sh, "exitRound(address)", "uint256", ["address"], [h])
        if leaving and r < view(sh, "round()", "uint256"):
            due += leaving * view(sh, "roundValue(uint256)", "uint256", ["uint256"], [r]) // \
                view(sh, "roundShares(uint256)", "uint256", ["uint256"], [r])
        return due

    def chain_now(self) -> int:
        """The latest block's time: the windows the keeper judges are the chain's, not the host's
        clock's (the cloud audit, N15). The host's only if the node will not say."""
        try:
            return int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
        except Exception as exc:
            log("block_time_unread", error=str(exc)[:200])
            return int(time.time())

    def registry(self) -> str:
        """The factory's registry of agent keys, read once."""
        if self._registry is None:
            self._registry = to_checksum_address(view(view(self.pool, "factory()", "address"), "registry()", "address"))
        return self._registry

    def send(self, to: str, sig: str, types=(), args=(), gas: int | None = None) -> bool:
        """Sends the call if the contract would take it now. True if it went out (or would have)."""
        if self.gas(to, sig, types, args) is None:
            return False
        if self.dry:
            log("would_send", to=to, call=sig.split("(")[0])
            return True
        try:
            rcpt = c.transact(self.wallet, to, sig, list(types), list(args), **({"gas": gas} if gas is not None else {}))
            log("sent", to=to, call=sig.split("(")[0], tx=rcpt["transactionHash"])
            return True
        except Exception as exc:  # one failed call must not stop the pass
            gas = core.gas_balance(self.wallet)
            log("send_failed", to=to, call=sig.split("(")[0], error=refusal(exc), gas_wei=gas,
                unfunded=None if gas is None else core.unfunded(gas))
            return False

    def stop_call(self, fn: str, seat: str, names) -> bool:
        """A seat's stop, its cancels and extra positions cut to what a small block holds, the rest left
        to the seat's settlement steps (audit window 2, В6-3; keeper.fit)."""
        cancels, extra = core.stop_args(seat, names, None)
        salt = os.urandom(32)
        types = ["address", CANCEL, "uint32[]", "bytes32"]
        if not cancels and not extra:  # nothing to cut
            return self.send(self.pool, fn + STOP, types, [seat, cancels, extra, salt])
        cn, ex, gas, fits = core.fit(lambda a, b: self.gas(self.pool, fn + STOP, types, [seat, a, b, salt]), cancels, extra)
        if not fits:
            log("call_over_small_block", to=self.pool, call=fn, seat=seat, cancels=len(cancels), extra=len(extra))
            return False
        if len(cn) < len(cancels) or len(ex) < len(extra):
            log("named_in_part", to=self.pool, call=fn, seat=seat, cancels=f"{len(cn)}/{len(cancels)}",
                extra=f"{len(ex)}/{len(extra)}")
        return self.send(self.pool, fn + STOP, types, [seat, cn, ex, salt],
                         gas=None if gas is None else min(gas * 5 // 4, core.SMALL_BLOCK_GAS))

    # ── exams ────────────────────────────────────────────────────────────────────────

    def scan(self, latest: int | None) -> None:
        end = None if latest is None else min(latest, self.next_block + self.window * self.max_windows - 1)
        while end is not None and self.next_block <= end:
            hi = min(self.next_block + self.window - 1, end)
            try:
                exams, holders = found_in_logs(self.pool, self.shares, self.next_block, hi)
            except Exception as exc:
                log("scan_stopped", at_block=self.next_block, error=str(exc)[:200])
                break
            self.exams += [e for e in dict.fromkeys(exams) if e not in self.exams]
            self.holders += [h for h in dict.fromkeys(holders) if h not in self.holders]
            self.next_block = hi + 1
            self.save()

    def exam_pass(self, exam: str, now: int, over: bool) -> bool:
        """One exam's turn. False once the pool holds it as sold no longer."""
        _, state, _, _ = c.call_view(self.pool, "examInfo(address)", ["address"], [exam], EXAM_INFO)
        if EXAM_STATE[state] != "Sold":
            return False
        status, _, _, passed_at = c.call_view(exam, "result()", [], [], ["uint8", "address", "uint64", "uint64"])
        status = EXAM_STATUS[status]
        reads = {}
        if status == "Active" and not over:
            terms = view(exam, "terms()", EXAM_TERMS)
            try:
                broken = view(exam, "violation()", "uint8") != 0
            except Exception as exc:
                # A held asset with no price makes violation() revert, and expire() reads none: an exam
                # past its time is still expired (the cloud audit, N12).
                log("violation_unread", exam=exam, error=str(exc)[:200])
                broken = False
            reads = {"broken": broken, "deadline": view(exam, "deadline()", "uint64"),
                     "last_fill": view(exam, "lastFillAt()", "uint64"), "max_idle": terms[3],
                     "pending": c.call_view(exam, "pending()", [], [], ["uint32", "bool", "uint64", "uint64"])[2] != 0}
            if core.in_checkpoint_window(now):
                reads["day"] = view(exam, "day()", "uint32")
            else:
                reads["day"] = now // 86_400
        for call in exam_calls("Sold", status, now, over, passed_at=passed_at,
                               claim_window=self.plan["claim_window"], **reads):
            if call == "release":
                self.send(self.pool, "release(address)", ["address"], [exam])
            else:
                self.send(exam, f"{call}()")
        return True

    # ── seats ────────────────────────────────────────────────────────────────────────

    def fills_traded(self, seat: str, since: int, now: int) -> bool | None:
        """Whether the seat's account filled anything after `since`; None if Hyperliquid would not say."""
        try:
            fills = c.info_post({"type": "userFillsByTime", "user": seat, "startTime": since * 1000 + 1})
        except Exception as exc:
            log("fills_unread", seat=seat, error=str(exc)[:200])
            return None
        self.fills_read_at[seat] = now
        return traded_since(fills, since)

    def note_if_traded(self, seat: str, since: int, now: int) -> bool | None:
        """Reads the seat's fills and, if it traded, says so to the pool. True if it traded, None if
        that could not be read."""
        traded = self.fills_traded(seat, since, now)
        if traded:
            if self.sender == to_checksum_address(self.plan["noter"]):
                self.send(self.pool, "noteTrade(address)", ["address"], [seat])
            else:
                log("traded_not_noted", seat=seat, noter=self.plan["noter"],
                    why="this keeper's wallet is not the plan's noter")
        return traded

    def funded_pass(self, seat: str, now: int, over: bool, names, latest) -> None:
        try:
            core.pool_pass(self.wallet, seat, names, now, latest, self.dry)
        except core.NotFullyChecked:
            log("venue_incomplete", seat=seat)
        if view(seat, "stage()", "uint8") != FUNDED:
            return
        since = view(self.pool, "activeSince(address)", "uint64", ["address"], [seat])
        idle_due = now > since + self.plan["funded_idle"]
        if not over and not idle_due and now >= self.fills_read_at.get(seat, 0) + self.note_every:
            self.note_if_traded(seat, since, now)
            since = view(self.pool, "activeSince(address)", "uint64", ["address"], [seat])
        margin = c.core_margin_summary(seat)
        calls = funded_calls(over, has_target=self.plan["after_target"] != TRADER_STOPS, flat=margin["ntlPos"] == 0,
                             equity=margin["accountValue"], start=view(seat, "fundedStart()", "int64"),
                             target_bps=self.plan["funded_target_bps"], idle_due=idle_due)
        for call in calls:
            if call == "closeIdle":
                # Never on an account whose fills could not be read, or that traded since: the
                # chain cannot see a round trip from flat to flat, and this is what can.
                if self.note_if_traded(seat, since, now) is not False:
                    continue
            self.stop_call(call, seat, names)

    def seat_pass(self, seat: str, now: int, over: bool, names, latest) -> None:
        line, state, _, _, armed_at, rest_until, wait_until, _ = c.call_view(
            self.pool, "seatInfo(address)", ["address"], [seat], SEAT_INFO)
        state = SEAT_STATE[state]
        stage = view(seat, "stage()", "uint8")
        if state != "Funded":
            self.fills_read_at.pop(seat, None)
        if state in ("Funded", "Waiting") and stage in (FUNDED, CLOSING):
            if state == "Funded" and stage == FUNDED:
                self.funded_pass(seat, now, over, names, latest)
            else:
                try:
                    core.pool_pass(self.wallet, seat, names, now, latest, self.dry)
                except core.NotFullyChecked:
                    log("venue_incomplete", seat=seat)
            return
        spot = c.core_spot_balance(seat, c.USDC_TOKEN)["total"] if state in ("Free", "Armed") else 0
        size = self.plan["menu"][line][0] * 100  # perp units (1e6) to spot's (1e8)
        for call in seat_calls(state, stage, now, over, spot=spot, size=size, armed_at=armed_at,
                               rest_until=rest_until, wait_until=wait_until, claim_window=self.plan["claim_window"]):
            if self.send(self.pool, f"{call}(address)", ["address"], [seat]) or call != "open":
                continue
            # More spoiled keys on top of the registry's list than one assign skips refuse every
            # opening on the factory until somebody retires them, which anyone may (deep audit N2).
            if self.last_refusal == "TooManySpoiledKeys()" and self.send(
                    self.registry(), "purgeSpoiled(uint256)", ["uint256"], [PURGE_PER_CALL]):
                self.send(self.pool, "open(address)", ["address"], [seat])

    def keys_pass(self) -> None:
        """Says so on every pass while the registry holds no key beyond one for every seat the pool
        has spoken for: the pool sells nothing until the operator publishes more (deep audit N3)."""
        free = view(self.registry(), "freeCount()", "uint256")
        held = view(self.pool, "held()", "uint256")
        if free <= held:
            log("keys_short", registry=self.registry(), free=free, held=held,
                why="no exam is sold until the operator publishes more keys; spoiled ones count until purged")

    # ── the round ────────────────────────────────────────────────────────────────────

    def round_pass(self, now: int, ends: int, over: bool) -> None:
        exit_shares = view(self.shares, "exitShares()", "uint256")
        # With nobody asking to leave, the round has nothing to do: renew would refuse.
        if exit_shares == 0:
            return
        late = is_late(now, exit_shares, view(self.pool, "reserveFor()", "uint64"), ends)
        for call in round_calls(over, exit_shares, late):
            # The keeper closes this round (the CTO's decision, 15 October 2026), as soon as the end
            # it was asked for has passed; anyone may as well, so a missed pass delays nobody long.
            if not self.send(self.shares, f"{call}()"):
                log("round_not_closed", shares=self.shares, ends=ends, left_s=ends - now, late=late)

    def tickets_pass(self, refunding: bool) -> None:
        """A settlement point when open tickets hold a deposit, or something to hand back once a
        raise that fell short has taken its price, or when closed tickets are still listed. What it
        reads is bounded per pass, however many tickets anyone opened (deep audit F15)."""
        count = view(self.shares, "openTicketCount()", "uint256")
        start = self.page_at if self.page_at < count else 0
        for _ in range(PAGES_PER_PASS):
            if start >= count:
                break
            page = view(self.shares, "openTickets(uint256,uint256)", "address[]", ["uint256", "uint256"], [start, PAGE])
            for t in page:
                self.ticket_read.setdefault(to_checksum_address(t), None)
            start += PAGE
        self.page_at = start if start < count else 0
        new = [t for t, at in self.ticket_read.items() if at is None][:NEW_PER_PASS]
        due = sorted((t for t, at in self.ticket_read.items() if at is not None and self.passes - at >= RECHECK_EVERY),
                     key=lambda t: self.ticket_read[t])[:RECHECK_PER_PASS]
        threshold = SWEEP_MIN if refunding else view(self.shares, "minDeposit()", "uint64")
        held = {t: c.core_spot_balance(t, c.USDC_TOKEN)["total"] for t in new + due}
        named = tickets_due(held, threshold)
        for t in held:
            if t in named:
                self.ticket_read.pop(t, None)
            else:
                self.ticket_read[t] = self.passes
        if named or view(self.shares, "closedTickets()", "address[]"):
            self.send(self.shares, "settle(address[])", ["address[]"], [named])

    def shares_pass(self, now: int, ends: int, over: bool) -> None:
        sh = self.shares
        launched = view(sh, "launched()", "bool")
        refunding = view(sh, "refundShares()", "uint256") != 0
        calls = raise_calls(launched, now, view(sh, "raiseEnds()", "uint64"),
                            view(sh, "value()", "uint256") if not launched else 0, view(sh, "target()", "uint256"),
                            view(self.pool, "value()", "uint256") if not launched else 0,
                            view(sh, "feeOwed()", "uint256"))
        for call in calls:
            if call == "refund":
                for h in self.holders:
                    if view(sh, "sharesOf(address)", "uint256", ["address"], [h]):
                        self.send(sh, "refund(address)", ["address"], [h])
            else:
                self.send(sh, f"{call}()")
        if launched:
            self.round_pass(now, ends, over)
        creator = to_checksum_address(view(sh, "creator()", "address"))
        for h in dict.fromkeys(self.holders + [creator]):
            if self.due_of(sh, h):
                self.send(sh, "pay(address)", ["address"], [h])
        # The tickets last: whatever anyone opened, the raise, the round and what is owed have
        # already had their turn (deep audit F15).
        try:
            self.tickets_pass(refunding)
        except Exception as exc:
            log("tickets_failed", shares=sh, error=str(exc)[:200])

    # ── the pass ─────────────────────────────────────────────────────────────────────

    def one_pass(self) -> None:
        try:
            latest = int(c.rpc("eth_blockNumber"), 16)
        except Exception as exc:
            log("head_read_failed", error=str(exc)[:200])
            latest = None
        self.passes += 1
        if self.plan is None:
            self.plan = plan_of(self.pool)
            if to_checksum_address(self.plan["noter"]) != self.sender:
                log("not_the_noter", noter=self.plan["noter"], keeper=self.sender,
                    why="a seat that traded is left open, but noteTrade is not this keeper's to send")
        if not self.owner_known:
            # Asked until the node answers for sure: a refused read once turned every PoolShares
            # duty off for good and said nothing (deep audit F14).
            try:
                self.shares = shares_of(self.pool)
                self.owner_known = True
                log("owner_known", shares=self.shares,
                    serves="PoolShares too" if self.shares else "the pool alone: its owner is no PoolShares")
            except Exception as exc:
                log("owner_unknown", error=str(exc)[:200], why="the PoolShares duties wait; asked again next pass")
        # Not before the owner is known: a scan without the shares contract's address moves on past
        # its holders' events and never reads them again (the audit, on F14).
        if self.owner_known:
            self.scan(latest)
        now = self.chain_now()
        ends = view(self.pool, "endsAt()", "uint64")
        over = ends != 0 and now > ends
        names = core.perp_index_by_name()
        kept = []
        for exam in self.exams:
            try:
                if self.exam_pass(exam, now, over):
                    kept.append(exam)
            except Exception as exc:
                kept.append(exam)
                log("exam_failed", exam=exam, error=str(exc)[:200])
        self.exams = kept
        seats = [to_checksum_address(s) for s in view(self.pool, "seats()", "address[]")]
        for seat in seats:
            try:
                self.seat_pass(seat, now, over, names, latest)
            except Exception as exc:
                log("seat_failed", seat=seat, error=str(exc)[:200])
        try:
            self.keys_pass()
        except Exception as exc:
            log("keys_failed", error=str(exc)[:200])
        if self.shares is not None:
            try:
                self.shares_pass(now, ends, over)
            except Exception as exc:
                log("shares_failed", shares=self.shares, error=str(exc)[:200])
        self.save()
        log("pass_done", pool=self.pool, exams=len(self.exams), seats=len(seats), next_block=self.next_block,
            latest=latest)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/ with a MultiPool")
    p.add_argument("--wallet", default="keeper", help="any funded testnet key; the plan's noter for noteTrade")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=30, help="seconds between passes")
    p.add_argument("--note-every", type=int, default=3_600,
                   help="seconds between reads of a funded seat's fills (always read before closeIdle)")
    p.add_argument("--log-window", type=int, default=MAX_LOG_WINDOW)
    p.add_argument("--max-windows", type=int, default=20, help="log windows a pass reads at most")
    p.add_argument("--state", help="the state file (default ops/state/multi-<deployment>.json)")
    p.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    p.add_argument("--from", dest="sender", help="address the calls are tried from in a dry run")
    args = p.parse_args()

    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "MultiPool" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no MultiPool")
    wallet = None if args.dry_run else c.account(args.wallet)
    sender = wallet.address if wallet else (args.sender or c.address_of(args.wallet))
    state = pathlib.Path(args.state) if args.state else STATE_DIR / f"multi-{args.deployment}.json"
    k = MultiKeeper(record["MultiPool"], wallet, sender, args.dry_run, state, int(record["block"]),
                    window=args.log_window, max_windows=args.max_windows, note_every=args.note_every,
                    reader=record.get("MultiPoolReader"))
    while True:
        try:
            k.one_pass()
        except Exception as exc:  # the RPC or the info API failed; the next pass retries
            log("pass_failed", error=str(exc)[:200])
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
