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
  an exam over    → release once it failed or lapsed, once its pass went untaken past the claim
                    window, or once the pool is over
  a free seat     → sweep what reached it after it was freed
  an armed seat   → rearm if the money has not shown a minute after it was sent; open once the
                    seat has rested; finish once the pool is over
  a funded seat   → what any funded account gets from the core keeper (ops/keeper.py: checkpoint
                    and breach, and once stopped recut and settleFunded); then endSeat once the
                    pool is over, hitTarget if the plan ends a stage at its target and the account
                    is flat past it, closeIdle once it has gone the plan's idle limit untraded
  a settled seat  → finish: the seat is freed, or held for its trader after a target
  the round       → renew, on a pool owned by PoolShares, in the last `notice` of the term

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
from ops.deploy_multi import PLAN_T
from spike.hlspike import common as c

ZERO = core.ZERO
CANCEL = core.CANCEL
STOP = f"(address,{CANCEL},uint32[],bytes32)"
STATE_DIR = core.STATE_DIR
MAX_LOG_WINDOW = core.MAX_LOG_WINDOW
EXAM_SOLD = "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64)").hex()

# MultiPool's and PaperExam's names and layouts as the keeper reads them; the tests hold each
# against the Solidity source.
ARM_WAIT = 60
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
               day: int = 0) -> list[str]:
    """The calls worth trying on one exam, in order. `state` is the pool's (ExamState), `status`
    the exam's own; an exam the pool no longer holds as sold needs nothing ever again."""
    if state != "Sold":
        return []
    if status == "Active":
        if over:
            return ["release"]
        calls = []
        if core.in_checkpoint_window(now) and day < now // 86_400:
            calls.append("checkpoint")
        if broken:
            return calls + ["breach"]
        if (deadline and now > deadline) or (max_idle and now > last_fill + max_idle):
            return calls + ["expire"]
        return calls
    if status in ("Breached", "Expired") or over:
        return ["release"]
    if status == "Passed" and now > passed_at + claim_window:
        return ["release"]
    return []


def seat_calls(state: str, stage: int, now: int, over: bool, *, spot: int = 0, size: int = 0, armed_at: int = 0,
               rest_until: int = 0, wait_until: int = 0) -> list[str]:
    """The pool's calls worth trying on one seat, in order, from its state and its stage. A funded
    stage's own calls (the core keeper's, and those that need the account read) come separately."""
    if state == "Free":
        return ["sweep"] if spot > 0 else []
    if state == "Armed":
        if over:
            return ["finish"]
        if spot < size:
            return ["rearm"] if now > armed_at + ARM_WAIT else []
        return ["open"] if now > rest_until else []
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


def in_renew_window(now: int, ends: int, notice: int) -> bool:
    """The last `notice` of the term, as PoolShares counts it: from `ends - notice`, to the end."""
    return ends != 0 and now + notice > ends


def exams_sold(pool: str, lo: int, hi: int) -> list[str]:
    """The exams the pool's ExamSold events name in blocks [lo, hi], in order, one call."""
    logs = c.rpc("eth_getLogs", [{"address": pool, "topics": [EXAM_SOLD], "fromBlock": hex(lo), "toBlock": hex(hi)}])
    return [to_checksum_address("0x" + entry["topics"][1][-40:]) for entry in logs]


def shares_of(pool: str) -> str | None:
    """The PoolShares contract that owns the pool, if one does: an owner with code that names this
    pool as its own. A person's address, or any other contract, is None."""
    owner = to_checksum_address(view(pool, "owner()", "address"))
    if c.rpc("eth_getCode", [owner, "latest"]) in ("0x", ""):
        return None
    try:
        return owner if to_checksum_address(view(owner, "pool()", "address")) == pool else None
    except RuntimeError:
        return None


def plan_of(pool: str) -> dict:
    """The plan the pool holds, by field, read from the contract it keeps it in."""
    holder = to_checksum_address(view(pool, "planData()", "address"))
    code = bytes.fromhex(c.rpc("eth_getCode", [holder, "latest"])[2:])
    if not code or code[0] != 0:
        raise SystemExit("that is not where a pool keeps its plan")
    return dict(zip(PLAN_FIELDS, abi_decode([PLAN_T], code[1:])[0]))


class MultiKeeper:
    def __init__(self, pool: str, wallet, sender: str, dry: bool, state_path: pathlib.Path, start_block: int,
                 window: int = MAX_LOG_WINDOW, max_windows: int = 20, note_every: int = 3_600):
        if not 1 <= window <= MAX_LOG_WINDOW:
            raise SystemExit(f"--log-window must be 1..{MAX_LOG_WINDOW}: HyperEVM refuses wider eth_getLogs ranges")
        self.pool = to_checksum_address(pool)
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
        self.fills_read_at: dict[str, int] = {}  # funded seat -> when its fills were last read
        self.plan: dict | None = None
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if to_checksum_address(state["pool"]) != self.pool:
                raise SystemExit(f"{state_path} belongs to another pool")
            self.next_block = int(state["next_block"])
            self.exams = [to_checksum_address(e) for e in state["exams"]]
            self.fills_read_at = {to_checksum_address(k): int(v) for k, v in state.get("fills_read_at", {}).items()}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pool": self.pool, "next_block": self.next_block, "exams": self.exams,
                                   "fills_read_at": self.fills_read_at}, indent=2) + "\n")
        tmp.replace(self.state_path)

    # ── calls ────────────────────────────────────────────────────────────────────────

    def gas(self, to: str, sig: str, types=(), args=()) -> int | None:
        """What the call would cost, or None if the contract would refuse it now."""
        data = c.encode_call(sig, list(types), list(args))
        try:
            return int(c.rpc("eth_estimateGas", [{"from": self.sender, "to": to, "data": "0x" + data.hex()}]), 16)
        except RuntimeError as exc:
            log("not_due", to=to, call=sig.split("(")[0], reason=refusal(exc))
            return None

    def send(self, to: str, sig: str, types=(), args=()) -> bool:
        """Sends the call if the contract would take it now. True if it went out (or would have)."""
        if self.gas(to, sig, types, args) is None:
            return False
        if self.dry:
            log("would_send", to=to, call=sig.split("(")[0])
            return True
        try:
            rcpt = c.transact(self.wallet, to, sig, list(types), list(args))
            log("sent", to=to, call=sig.split("(")[0], tx=rcpt["transactionHash"])
            return True
        except Exception as exc:  # one failed call must not stop the pass
            gas = core.gas_balance(self.wallet)
            log("send_failed", to=to, call=sig.split("(")[0], error=refusal(exc), gas_wei=gas,
                unfunded=None if gas is None else core.unfunded(gas))
            return False

    def stop_call(self, fn: str, seat: str, names) -> bool:
        cancels, extra = core.stop_args(seat, names, None)
        return self.send(self.pool, fn + STOP, ["address", CANCEL, "uint32[]", "bytes32"],
                         [seat, cancels, extra, os.urandom(32)])

    # ── exams ────────────────────────────────────────────────────────────────────────

    def scan(self, latest: int | None) -> None:
        end = None if latest is None else min(latest, self.next_block + self.window * self.max_windows - 1)
        while end is not None and self.next_block <= end:
            hi = min(self.next_block + self.window - 1, end)
            try:
                found = exams_sold(self.pool, self.next_block, hi)
            except Exception as exc:
                log("scan_stopped", at_block=self.next_block, error=str(exc)[:200])
                break
            self.exams += [e for e in found if e not in self.exams]
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
            reads = {"broken": view(exam, "violation()", "uint8") != 0, "deadline": view(exam, "deadline()", "uint64"),
                     "last_fill": view(exam, "lastFillAt()", "uint64"), "max_idle": terms[3]}
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
                               rest_until=rest_until, wait_until=wait_until):
            self.send(self.pool, f"{call}(address)", ["address"], [seat])

    # ── the round ────────────────────────────────────────────────────────────────────

    def renew_pass(self, now: int, ends: int) -> None:
        if self.shares is None or not in_renew_window(now, ends, view(self.pool, "notice()", "uint64")):
            return
        # With nobody asking to leave, the round has nothing to do: renew would refuse.
        if view(self.shares, "exitShares()", "uint256") == 0:
            return
        # The keeper has to close this round in these days (the CTO's decision, 15 October 2026):
        # past the end, the next end is the renewal's, and those leaving would wait for it.
        if not self.send(self.shares, "renew()"):
            log("round_not_closed", shares=self.shares, ends=ends, left_s=ends - now)

    # ── the pass ─────────────────────────────────────────────────────────────────────

    def one_pass(self) -> None:
        try:
            latest = int(c.rpc("eth_blockNumber"), 16)
        except Exception as exc:
            log("head_read_failed", error=str(exc)[:200])
            latest = None
        if self.plan is None:
            self.plan = plan_of(self.pool)
            self.shares = shares_of(self.pool)
            if to_checksum_address(self.plan["noter"]) != self.sender:
                log("not_the_noter", noter=self.plan["noter"], keeper=self.sender,
                    why="a seat that traded is left open, but noteTrade is not this keeper's to send")
        self.scan(latest)
        now = int(time.time())
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
        self.renew_pass(now, ends)
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
                    window=args.log_window, max_windows=args.max_windows, note_every=args.note_every)
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
