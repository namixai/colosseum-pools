"""One step at a time on a pool of many traders, on testnet (chain 998), by hand: there is no
keeper for it yet, so every call somebody has to make is a command here.

    spike/.venv/bin/python ops/multi_run.py --label multi-1 status
    spike/.venv/bin/python ops/multi_run.py --label multi-1 begin --who multi-operator
    spike/.venv/bin/python ops/multi_run.py --label multi-1 stopterm --who multi-operator [--undo]
    spike/.venv/bin/python ops/multi_run.py --label multi-1 fund --who multi-operator --usdc 100
    spike/.venv/bin/python ops/multi_run.py --label multi-1 buy --who multi-trader --line 0
    spike/.venv/bin/python ops/multi_run.py --label multi-1 invite --who multi-operator --trader 0x... --line 0 --nonce 1 --days 7
    spike/.venv/bin/python ops/multi_run.py --label multi-1 free --who multi-trader --invite '<the line invite printed>'
    spike/.venv/bin/python ops/multi_run.py --label multi-1 withdrawinvite --who multi-operator --nonce 1
    spike/.venv/bin/python ops/multi_run.py --label multi-1 notetrade --who multi-keeper --seat 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 price --asset BTC
    spike/.venv/bin/python ops/multi_run.py --label multi-1 place --who multi-trader --exam 0x... --asset BTC --side buy --size 0.0002
    spike/.venv/bin/python ops/multi_run.py --label multi-1 fill --who multi-trader --exam 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 exam --exam 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 autoclose --who multi-trader --exam 0x... --asset BTC [--cushion 0.001] [--minutes 60]
    spike/.venv/bin/python ops/multi_run.py --label multi-1 graduate --who multi-trader --exam 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 expire --who multi-trader --exam 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 claim --who multi-trader --exam 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 open --who multi-trader --seat 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 stop --who multi-trader --seat 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 settle --who multi-trader --seat 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 finish --who multi-trader --seat 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 reserve --who multi-operator --usdc 10
    spike/.venv/bin/python ops/multi_run.py --label multi-1 extend --who multi-operator --term-days 0.125 --usdc 10
    spike/.venv/bin/python ops/multi_run.py --label multi-1 end --who multi-trader --seat 0x...
    spike/.venv/bin/python ops/multi_run.py --label multi-1 withdraw --who multi-operator --usdc 50

`status`, `price` and `exam` read and send nothing, and neither does `invite`: it signs a free
exam for one trader with the inviter's key and prints the invitation as one line of JSON, to be
handed over wherever the creator likes; `free` is the trader's call that takes it. Every other step sends one transaction signed
by the key `--who` names -- `buy` an approval and then the purchase, `settle` one step after
another until the seat is idle -- and then says what it cost in gas and what the chain shows. `--dry-run` before the step's name makes it
say what it would send and stop there.

A refusal by a contract is printed as one line with the error's name (`refused: Ending()`), and
the command exits with 1.

`autoclose` is the watcher an exam with a small target needs: its orders fill at the mark of a
later block, so a position in profit cannot be closed at the moment it is. It reads the exam's
equity every few seconds, and once it is at the step's target with the closing fill's cost on top
(and `--cushion` more, in USDC), it places the order that closes the position in `--asset`, fills
it at the first block it may, and passes the step. If the mark moved back between the order and
the fill, it says so and stops, flat: the trader opens again and runs it again. It fills only the
order it placed itself, and touches nothing when another asset is open as well.

Three of the steps measure something the simulator could not show: `fund` and `claim` count the
seconds a transfer between HyperCore accounts takes to show in a balance, and `price` puts the
mark a contract reads next to the exchange's own.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

from hlspike import common as c  # noqa: E402
from ops import deployments  # noqa: E402
from ops.deploy_multi import PLAN_T  # noqa: E402
from ops.contract_errors import refusal  # noqa: E402
from ops.deploy_testnet import PLATFORM_ASSETS  # noqa: E402

USDC_1E6 = 1_000_000
USDC_1E8 = 100_000_000
EXAM_SOLD = "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64)").hex()
CLAIMED = "0x" + keccak(text="Claimed(address,address,address)").hex()
EXAM_STATUS = ("None", "Active", "Passed", "Breached", "Expired")
SEAT_STATE = ("None", "Free", "Armed", "Funded", "Waiting")
STAGE = ("Idle", "Challenge", "Funded", "Closing", "PassedAwaitingKey")
NO_SALT = b"\x00" * 32
STOP_TYPES = ["(uint32,uint64)[]", "uint32[]", "bytes32"]
SETTLE_TYPES = ["(uint32,uint64)[]", "uint32[]"]
# MultiPool.SeatInfo, as `seatInfo(address)` returns it. A test holds it against the source.
SEAT_INFO = ["uint8", "uint8", "bool", "address", "uint64", "uint64", "uint64", "uint16"]
# PaperExam's ExamTerms, as `terms()` returns it. A test holds it against the source.
EXAM_TERMS = ["(uint64,uint16[],uint32,uint32,uint16,uint32,uint16,uint16)"]
PENDING = ["uint32", "bool", "uint64", "uint64"]
# MultiPool.ARM_WAIT: how long a payment out counts as still leaving. A test holds it against the source.
ARM_WAIT = 60


def view(to: str, sig: str, out: list, types: list = (), args: list = ()) -> tuple:
    return c.call_view(to, sig, list(types), list(args), list(out))


def topic_address(topic: str) -> str:
    return to_checksum_address("0x" + topic[-40:])


def indexed(receipt: dict, topic0: str, position: int) -> str:
    """The address in indexed slot `position` (1 to 3) of the receipt's one log with `topic0`."""
    found = [log for log in receipt["logs"] if log["topics"][0] == topic0]
    if len(found) != 1:
        raise SystemExit(f"expected one such event in {receipt['transactionHash']}, found {len(found)}")
    return topic_address(found[0]["topics"][position])


def size_units(size: float, sz_decimals: int) -> int:
    """A size in coins as the exam counts it: whole units of the asset's smallest step."""
    units = round(size * 10**sz_decimals)
    if units <= 0 or abs(units / 10**sz_decimals - size) > 1e-12:
        raise SystemExit(f"{size} is not a whole number of steps of 1e-{sz_decimals}")
    return units


def seconds(days: float) -> int:
    """A time given in days, as the contracts count it."""
    return round(days * 86_400)


def mark_usd(raw_px: int, sz_decimals: int) -> float:
    """A mark as the price precompile returns it, in dollars: it carries 6 - szDecimals decimals."""
    return raw_px / 10 ** (6 - sz_decimals)


def gap_bps(contract_usd: float, exchange_usd: float) -> float:
    return (contract_usd - exchange_usd) / exchange_usd * 10_000


def target_equity(capital: int, target_bps: int) -> int:
    """The equity a step passes at (1e6 = 1 USDC), worked out as `graduate` does."""
    return capital + capital * target_bps // 10_000


def close_cost(notional: int, cost_ppm: int) -> int:
    """What the fill that closes a position of this notional costs, rounded up as `fill` does."""
    return (notional * cost_ppm + 999_999) // 1_000_000


def autoclose_move(equity: int, target: int, position: int, asset_ntl: int, total_ntl: int, cost_ppm: int,
                   cushion: int) -> tuple:
    """What the watcher does next, from what the exam shows (1e6 = 1 USDC; `position` in the
    asset's units, signed): ("wait",), ("close", is_buy, size), ("graduate",), or ("stop", why)."""
    if total_ntl > asset_ntl:
        return ("stop", "another asset is open as well; autoclose closes one asset, and the step passes flat")
    if position == 0:
        if equity >= target:
            return ("graduate",)
        return ("stop", f"flat at {equity / USDC_1E6:.6f}, under the target {target / USDC_1E6:.6f}: the mark "
                        "moved back between the order and its fill; open again and run autoclose again")
    if equity >= target + close_cost(asset_ntl, cost_ppm) + cushion:
        return ("close", position < 0, abs(position))
    return ("wait",)


def status_notes(value: int, held: int, in_work: int, in_work_bps: int, out_flight: int, last_out: int,
                 now: int) -> list[str]:
    """What `status` says beside the numbers, so that two readings of the first run do not mislead
    (1e8 = 1 USDC for value and out_flight, 1e6 for in_work)."""
    notes = []
    if out_flight and now <= last_out + ARM_WAIT:
        until = time.strftime("%H:%M:%S UTC", time.gmtime(last_out + ARM_WAIT))
        notes.append(f"{out_flight / USDC_1E8:g} paid out counts as still leaving until {until}: once it has landed, "
                     "value reads low by up to that much until then (the money is not lost)")
    if value == 0 and held == 0:
        notes.append("the pool is empty: there is nothing to withdraw, and a withdraw answers BufferNeeded")
    elif value:
        # What the share in work leaves free: `_keepShare` refuses past it.
        kept = -(-in_work * 100 * 10_000 // in_work_bps) if in_work else 0
        notes.append(f"a withdrawal, once the pool is over, may take up to {max(value - kept, 0) / USDC_1E8:.6f}")
    return notes


def universe() -> dict:
    """name -> (index, szDecimals, the exchange's mark) for the assets this deployment lists."""
    meta, ctxs = c.info_post({"type": "metaAndAssetCtxs"})
    return {u["name"]: (i, u["szDecimals"], float(ctxs[i]["markPx"]))
            for i, u in enumerate(meta["universe"]) if u["name"] in PLATFORM_ASSETS}


INVITE_TYPEHASH = keccak(text="Invite(address trader,uint8 line,uint64 nonce,uint64 expiresAt)")
DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")


def invite_digest(desk: str, trader: str, line: int, nonce: int, expires_at: int, chain_id: int = c.CHAIN_ID) -> bytes:
    """What the pool's inviter signs for one free exam (`FreeExams.take`): EIP-712 over the pool's
    `FreeExams` on this chain, the trader, the line, a number used once and the second it lapses."""
    domain = keccak(abi_encode(["bytes32", "bytes32", "bytes32", "uint256", "address"],
                               [DOMAIN_TYPEHASH, keccak(text="FreeExams"), keccak(text="1"), chain_id, desk]))
    invite = keccak(abi_encode(["bytes32", "address", "uint8", "uint64", "uint64"],
                               [INVITE_TYPEHASH, trader, line, nonce, expires_at]))
    return keccak(b"\x19\x01" + domain + invite)


def plan_of_pool(pool: str) -> tuple:
    """The plan a pool holds, read from the contract it keeps it in (`MultiPool.planData`)."""
    holder = to_checksum_address(view(pool, "planData()", ["address"])[0])
    return decode_plan(bytes.fromhex(c.rpc("eth_getCode", [holder, "latest"])[2:]))


def decode_plan(code: bytes) -> tuple:
    """The plan out of the code that keeps it: STOP, then the plan ABI-encoded."""
    if not code or code[0] != 0:
        raise SystemExit("that is not where a pool keeps its plan")
    return abi_decode([PLAN_T], code[1:])[0]


def sent(what: str, receipt: dict) -> None:
    print(f"  {what}: tx {receipt['transactionHash']}, block {int(receipt['blockNumber'], 16)}, "
          f"gas {int(receipt['gasUsed'], 16):,}")


def wait_spot(account: str, at_least: int, timeout_s: int = 180) -> float | None:
    """Seconds until `account` shows `at_least` (1e8) of spot USDC to a contract; None if it never did."""
    started = time.time()
    while time.time() - started < timeout_s:
        if c.core_spot_balance(account, c.USDC_TOKEN)["total"] >= at_least:
            return time.time() - started
        time.sleep(1)
    return None


class Run:
    def __init__(self, record: dict, dry: bool):
        self.pool = record["MultiPool"]
        # The pool's views that only read are the reader's (CTO, 18 October 2026); a record made
        # before it has none, and `status` says so.
        self.reader = record.get("MultiPoolReader")
        self.usdc = to_checksum_address(view(record["PoolFactory"], "usdc()", ["address"])[0])
        self.factory = record["PoolFactory"]
        self.dry = dry

    def send(self, what: str, who: str, to: str, sig: str, types: list = (), args: list = ()) -> dict | None:
        if self.dry:
            print(f"  --dry-run: {who} would call {sig} on {to} with {list(args)}")
            return None
        receipt = c.transact(c.account(who), to, sig, list(types), list(args))
        sent(what, receipt)
        return receipt

    # ── reading ──────────────────────────────────────────────

    def status(self, _a) -> None:
        p = self.pool
        value, in_work, held = (view(p, f"{n}()", ["uint256"])[0] for n in ("value", "inWork", "held"))
        ends = view(p, "endsAt()", ["uint64"])[0]
        print(f"pool {p}")
        print(f"  value {value / USDC_1E8:.6f}, in work {in_work / USDC_1E6:g} over {held} size(s), "
              f"promised to running exams {view(p, 'promised()', ['uint256'])[0] / USDC_1E6:g}")
        print(f"  on HyperCore {c.core_spot_balance(p, c.USDC_TOKEN)['total'] / USDC_1E8:.6f}, "
              f"on HyperEVM {c.erc20_balance(self.usdc, p) / USDC_1E6:.6f}")
        print(f"  ends {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(ends))}, "
              f"closing {view(p, 'closing()', ['bool'])[0]}; sells until "
              f"{time.strftime('%H:%M:%S UTC', time.gmtime(ends - view(p, 'saleLead()', ['uint64'])[0]))}; "
              f"now {time.strftime('%H:%M:%S UTC', time.gmtime())}")
        plan = plan_of_pool(p)
        for note in status_notes(value, held, in_work, plan[7], view(p, "outFlight()", ["uint64"])[0],
                                 view(p, "lastOutAt()", ["uint64"])[0], int(time.time())):
            print(f"  {note}")
        menu = plan[0]
        for i, (size, price) in enumerate(menu):
            room = ("? (no reader in the record)" if self.reader is None else
                    view(self.reader, "room(address,uint256)", ["uint256"], ["address", "uint256"], [p, i])[0])
            print(f"  line {i}: size {size / USDC_1E6:g}, price {price / USDC_1E6:g}, room for {room} more")
        for seat in view(p, "seats()", ["address[]"])[0]:
            seat = to_checksum_address(seat)
            line, state, _hit, trader, *_ = view(p, "seatInfo(address)", SEAT_INFO, ["address"], [seat])
            stage = view(seat, "stage()", ["uint8"])[0]
            spot = c.core_spot_balance(seat, c.USDC_TOKEN)["total"] / USDC_1E8
            equity = c.core_margin_summary(seat)["accountValue"] / USDC_1E6 if c.core_user_exists(seat) else 0.0
            print(f"  seat {seat} line {line}: {SEAT_STATE[state]}, pool stage {STAGE[stage]}, "
                  f"trader {trader}, spot {spot:.6f}, perp {equity:.6f}")

    def exam(self, a) -> None:
        e = a.exam
        status, trader, capital, passed_at = view(e, "result()", ["uint8", "address", "uint64", "uint64"])
        asset, is_buy, sz, placed = view(e, "pending()", ["uint32", "bool", "uint64", "uint64"])
        print(f"exam {e}: {EXAM_STATUS[status]}, trader {trader}, size {capital / USDC_1E6:g}, step {view(e, 'step()', ['uint8'])[0]}")
        print(f"  balance {view(e, 'cash()', ['int256'])[0] / USDC_1E6:.6f}, "
              f"equity {view(e, 'equity()', ['int256'])[0] / USDC_1E6:.6f}, "
              f"deadline {view(e, 'deadline()', ['uint64'])[0]}, passed at {passed_at}")
        print(f"  waiting order: {'none' if sz == 0 else ('buy' if is_buy else 'sell') + f' {sz} units of asset {asset}, placed in block {placed}'}"
              f"; this block {int(c.rpc('eth_blockNumber'), 16)}")

    def price(self, a) -> None:
        index, decimals, exchange = universe()[a.asset]
        raw = c.precompile("markPx", ["uint32"], [index], ["uint64"])[0]
        contract = mark_usd(raw, decimals)
        print(f"{a.asset}: a contract reads {contract:.6f} (raw {raw}), the exchange says {exchange:.6f}: "
              f"{gap_bps(contract, exchange):+.2f} bps apart")

    # ── sending ──────────────────────────────────────────────

    def fund(self, a) -> None:
        amount = round(a.usdc * USDC_1E8)
        before = c.core_spot_balance(self.pool, c.USDC_TOKEN)["total"]
        if self.dry:
            print(f"  --dry-run: {a.who} would send {a.usdc} USDC on HyperCore to {self.pool}")
            return
        resp = c.exchange(c.account(a.who)).spot_transfer(a.usdc, self.pool, c.spot_token_wire("USDC"))
        print(f"  transfer: {resp}")
        waited = wait_spot(self.pool, before + amount)
        print("  it never showed in the pool's balance" if waited is None
              else f"  the pool's balance showed it after {waited:.0f} s")

    def buy(self, a) -> None:
        menu = plan_of_pool(self.pool)[0]
        price = menu[a.line][1]  # the platform's 0.5 % is out of it (item 28)
        self.send("approve", a.who, self.usdc, "approve(address,uint256)", ["address", "uint256"], [self.pool, price])
        receipt = self.send("buy", a.who, self.pool, "buy(uint256)", ["uint256"], [a.line])
        if receipt:
            print(f"  exam {indexed(receipt, EXAM_SOLD, 1)}")

    def place(self, a) -> None:
        index, decimals, _ = universe()[a.asset]
        self.send("place", a.who, a.exam, "place(uint32,bool,uint64)", ["uint32", "bool", "uint64"],
                  [index, a.side == "buy", size_units(a.size, decimals)])

    def fill(self, a) -> None:
        self.send("fill", a.who, a.exam, "fill()")

    def graduate(self, a) -> None:
        self.send("graduate", a.who, a.exam, "graduate()")

    def expire(self, a) -> None:
        """Ends an exam past its deadline, or past its idle limit: `release` takes only an exam
        that has ended."""
        if self.send("expire", a.who, a.exam, "expire()"):
            print(f"  exam {EXAM_STATUS[view(a.exam, 'result()', ['uint8', 'address', 'uint64', 'uint64'])[0]]}")

    def autoclose(self, a) -> None:
        e = a.exam
        index, _, _ = universe()[a.asset]
        capital, targets, *_, cost_ppm, delay, window = view(e, "terms()", EXAM_TERMS)[0]
        cushion = round(a.cushion * USDC_1E6)
        ours = None  # the block our closing order was placed in
        foreign = None  # an order the trader left, told once
        until = time.time() + a.minutes * 60
        print(f"  watching {e} on {a.asset} for {a.minutes:g} min, every {a.every:g} s")
        while time.time() < until:
            status = view(e, "result()", ["uint8", "address", "uint64", "uint64"])[0]
            if EXAM_STATUS[status] != "Active":
                print(f"  the exam is {EXAM_STATUS[status]}")
                return
            _, _, sz, placed = view(e, "pending()", PENDING)
            block = int(c.rpc("eth_blockNumber"), 16)
            if sz and placed == ours and block < placed + delay + window:
                if block >= placed + delay:
                    self.send("fill", a.who, e, "fill()")
                    ours = None
                else:
                    time.sleep(1)
                continue
            if sz and placed != ours and block < placed + delay + window:
                if foreign != placed:
                    print(f"  an order placed in block {placed} waits, not ours; waiting until it is filled or lapses")
                    foreign = placed
                time.sleep(a.every)
                continue
            target = target_equity(capital, targets[view(e, "step()", ["uint8"])[0]])
            equity = view(e, "equity()", ["int256"])[0]
            position = view(e, "positionOf(uint32)", ["int256"], ["uint32"], [index])[0]
            if position != 0 and equity < target:
                time.sleep(a.every)
                continue
            px = c.precompile("markPx", ["uint32"], [index], ["uint64"])[0]
            move = autoclose_move(equity, target, position, abs(position) * px, view(e, "notional()", ["uint256"])[0],
                                  cost_ppm, cushion)
            if move[0] == "wait":
                time.sleep(a.every)
            elif move[0] == "close":
                print(f"  equity {equity / USDC_1E6:.6f} against the target {target / USDC_1E6:.6f}: closing")
                receipt = self.send("place", a.who, e, "place(uint32,bool,uint64)", ["uint32", "bool", "uint64"],
                                    [index, move[1], move[2]])
                if not receipt:
                    return
                ours = int(receipt["blockNumber"], 16)
            elif move[0] == "graduate":
                print(f"  flat at {equity / USDC_1E6:.6f}, the target {target / USDC_1E6:.6f}: passing the step")
                self.send("graduate", a.who, e, "graduate()")
                return
            else:
                print(f"  {move[1]}")
                return
        print("  time is up and the exam is still open")

    def release(self, a) -> None:
        self.send("release", a.who, self.pool, "release(address)", ["address"], [a.exam])

    def claim(self, a) -> None:
        receipt = self.send("claim", a.who, self.pool, "claim(address)", ["address"], [a.exam])
        if not receipt:
            return
        seat = indexed(receipt, CLAIMED, 3)
        size = view(a.exam, "result()", ["uint8", "address", "uint64", "uint64"])[2] * 100
        waited = wait_spot(seat, size)
        print(f"  seat {seat}")
        print("  the money never showed on the seat; `rearm` after a minute" if waited is None
              else f"  the seat's balance showed its {size / USDC_1E8:g} after {waited:.0f} s")

    def open(self, a) -> None:
        if self.send("open", a.who, self.pool, "open(address)", ["address"], [a.seat]):
            print(f"  seat stage {STAGE[view(a.seat, 'stage()', ['uint8'])[0]]}, "
                  f"trader {view(a.seat, 'fundedTrader()', ['address'])[0]}, "
                  f"agent key {view(a.seat, 'agentKey()', ['address'])[0]}, "
                  f"started on {view(a.seat, 'fundedStart()', ['int64'])[0] / USDC_1E6:g}")

    def rearm(self, a) -> None:
        self.send("rearm", a.who, self.pool, "rearm(address)", ["address"], [a.seat])

    def stop(self, a) -> None:
        self.send("stopFunded", a.who, a.seat, "stopFunded((uint32,uint64)[],uint32[],bytes32)", STOP_TYPES,
                  [[], [], NO_SALT])

    def target(self, a) -> None:
        self.send("hitTarget", a.who, self.pool, "hitTarget(address,(uint32,uint64)[],uint32[],bytes32)",
                  ["address"] + STOP_TYPES, [a.seat, [], [], NO_SALT])

    def settle(self, a) -> None:
        for step in range(1, 9):
            if view(a.seat, "stage()", ["uint8"])[0] == 0:
                print("  the seat is idle")
                return
            if not self.send(f"settleFunded, step {step}", a.who, a.seat,
                             "settleFunded((uint32,uint64)[],uint32[])", SETTLE_TYPES, [[], []]):
                return
            c.wait_blocks(2)
        print("  eight steps and the seat is not idle yet; look at it with `status`")

    def finish(self, a) -> None:
        self.send("finish", a.who, self.pool, "finish(address)", ["address"], [a.seat])

    def sweep(self, a) -> None:
        self.send("sweep", a.who, self.pool, "sweep(address)", ["address"], [a.seat])

    def note(self, a) -> None:
        self.send("noteActivity", a.who, self.pool, "noteActivity(address)", ["address"], [a.seat])
        since = view(self.pool, "activeSince(address)", ["uint64"], ["address"], [a.seat])[0]
        print(f"  last trade seen {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(since))}")

    def idle(self, a) -> None:
        self.send("closeIdle", a.who, self.pool, "closeIdle(address,(uint32,uint64)[],uint32[],bytes32)",
                  ["address"] + STOP_TYPES, [a.seat, [], [], NO_SALT])

    def end(self, a) -> None:
        self.send("endSeat", a.who, self.pool, "endSeat(address,(uint32,uint64)[],uint32[],bytes32)",
                  ["address"] + STOP_TYPES, [a.seat, [], [], NO_SALT])

    def extend(self, a) -> None:
        before = view(self.pool, "endsAt()", ["uint64"])[0]
        if self.send("extend", a.who, self.pool, "extend(uint32,uint64,uint256)", ["uint32", "uint64", "uint256"],
                     [seconds(a.term_days), round(a.usdc * USDC_1E8), round(a.usdc_evm * USDC_1E6)]):
            after = view(self.pool, "endsAt()", ["uint64"])[0]
            print(f"  the end moved by {(after - before) / 3600:g} h, to "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(after))}")

    def invite(self, a) -> None:
        """Signs a free exam for one trader and prints the invitation. Nothing is sent, and the key
        has to be the one the pool's plan names as its inviter."""
        acct = c.account(a.who)
        desk = self.desk()
        inviter = view(desk, "inviter()", ["address"])[0]
        if inviter.lower() != acct.address.lower():
            raise SystemExit(f"{a.who} is not this pool's inviter, {inviter}")
        trader = to_checksum_address(a.trader)
        expires = int(time.time() + a.days * 86_400)
        sig = acct.unsafe_sign_hash(invite_digest(desk, trader, a.line, a.nonce, expires))
        print(json.dumps({"pool": self.pool, "desk": desk, "trader": trader, "line": a.line, "nonce": a.nonce,
                          "expiresAt": expires, "v": sig.v, "r": f"0x{sig.r:064x}", "s": f"0x{sig.s:064x}"}))

    def desk(self) -> str:
        desk = to_checksum_address(view(self.pool, "freeExams()", ["address"])[0])
        if int(desk, 16) == 0:
            raise SystemExit("this pool's plan has no free exams")
        return desk

    def free(self, a) -> None:
        inv = json.loads(a.invite)
        if inv["pool"].lower() != self.pool.lower():
            raise SystemExit(f"this invitation is for the pool {inv['pool']}")
        self.send("free exam", a.who, self.desk(), "take(uint8,uint64,uint64,uint8,bytes32,bytes32)",
                  ["uint8", "uint64", "uint64", "uint8", "bytes32", "bytes32"],
                  [inv["line"], inv["nonce"], inv["expiresAt"], inv["v"], bytes.fromhex(inv["r"][2:]),
                   bytes.fromhex(inv["s"][2:])])

    def withdrawinvite(self, a) -> None:
        self.send("withdraw invitation", a.who, self.desk(), "withdraw(uint64)", ["uint64"], [a.nonce])

    def begin(self, a) -> None:
        """The owner starts the term, accepting the pool's terms by their hash."""
        h = plan_of_pool(self.pool)[16]
        self.send("begun", a.who, self.pool, "begin(bytes32)", ["bytes32"], [h])

    def stopterm(self, a) -> None:
        """The owner says the pool stops at the end of its term, or takes that back."""
        self.send("stop" if not a.undo else "stop taken back", a.who, self.pool, "setStop(bool)", ["bool"], [not a.undo])

    def notetrade(self, a) -> None:
        self.send("trade noted", a.who, self.pool, "noteTrade(address)", ["address"], [a.seat])

    def withdraw(self, a) -> None:
        self.send("withdraw", a.who, self.pool, "withdraw(uint64)", ["uint64"], [round(a.usdc * USDC_1E8)])

    def withdrawevm(self, a) -> None:
        self.send("withdrawEvm", a.who, self.pool, "withdrawEvm(uint256)", ["uint256"], [round(a.usdc * USDC_1E6)])

    def reserve(self, a) -> None:
        # What is to leave at the end of the term: `extend` takes it out once that end has passed,
        # never in the term's last days (DECISIONS 103).
        self.send("setReserve", a.who, self.pool, "setReserve(uint256)", ["uint256"], [round(a.usdc * USDC_1E8)])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True)
    p.add_argument("--dry-run", action="store_true", help="say what the step would send, and send nothing")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("exam").add_argument("--exam", required=True)
    sub.add_parser("price").add_argument("--asset", required=True, choices=sorted(PLATFORM_ASSETS))

    def step(name: str, *fields: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name)
        s.add_argument("--who", required=True, help="the named key that signs")
        for f in fields:
            s.add_argument(f"--{f}", required=True)
        return s

    step("fund").add_argument("--usdc", type=float, required=True)
    step("buy").add_argument("--line", type=int, required=True)
    pl = step("place", "exam")
    pl.add_argument("--asset", required=True, choices=sorted(PLATFORM_ASSETS))
    pl.add_argument("--side", required=True, choices=("buy", "sell"))
    pl.add_argument("--size", type=float, required=True, help="in coins, e.g. 0.0002")
    for name in ("fill", "graduate", "release", "claim", "expire"):
        step(name, "exam")
    ac = step("autoclose", "exam")
    ac.add_argument("--asset", required=True, choices=sorted(PLATFORM_ASSETS))
    ac.add_argument("--cushion", type=float, default=0.0, help="USDC over the target and the closing cost")
    ac.add_argument("--minutes", type=float, default=60.0, help="how long to watch")
    ac.add_argument("--every", type=float, default=3.0, help="seconds between readings")
    for name in ("open", "rearm", "stop", "target", "settle", "finish", "sweep", "end", "note", "idle", "notetrade"):
        step(name, "seat")
    ex = step("extend")
    ex.add_argument("--term-days", type=float, required=True)
    ex.add_argument("--usdc", type=float, default=0.0,
                    help="to take out of the HyperCore balance with it, once the end a reserve was set for has passed")
    ex.add_argument("--usdc-evm", type=float, default=0.0, help="to take out of the USDC on HyperEVM with it")
    for name in ("withdraw", "withdrawevm", "reserve"):
        step(name).add_argument("--usdc", type=float, required=True)
    inv = step("invite", "trader")
    inv.add_argument("--line", type=int, required=True)
    inv.add_argument("--nonce", type=int, required=True, help="the inviter's own numbering; each serves once")
    inv.add_argument("--days", type=float, required=True, help="until the invitation lapses")
    step("free", "invite")
    step("withdrawinvite").add_argument("--nonce", type=int, required=True)
    step("begin")
    step("stopterm").add_argument("--undo", action="store_true", help="take the stop back")
    return p


def main() -> int:
    args = build_parser().parse_args()
    c.assert_testnet()
    record = deployments.load(args.label)
    if "MultiPool" not in record:
        raise SystemExit(f"deployment {args.label} has no MultiPool")
    try:
        getattr(Run(record, args.dry_run), args.cmd)(args)
    except c.Reverted as exc:
        # Mined and refused: the receipt carries no reason.
        print(f"  reverted on chain: {exc}")
        return 1
    except RuntimeError as exc:
        print(f"  refused: {refusal(exc)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
