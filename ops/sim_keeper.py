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
  the slow pass  → every --every seconds: new pools (the factory's `pools()`), their exams and
                   stages (`ExamSold`, `Funded`, read in windows of --log-window blocks); for each
                   live one, `checkpoint` in the first minutes of a UTC day, `breach` when a rule is
                   broken now, `expire` past its time, `release` once it can do nothing more,
                   `claimPayout` for a passed stage; for each pool, `pay` while a payout is due.

The day's snapshot is what the daily loss is measured from: without it the base stays yesterday's,
and a trader who leaves it after a good day loosens their own limit. The keeper takes it.

--measure EXAM places --orders orders on an exam of the trader's own (`--trader-wallet`), one at a
time, small and alternating buy and sell, and waits for each to be filled or to lapse; it writes
each one's `late` (blocks after the first possible one) and `source` to --measure-out and prints p50
and p99. That is the number the filler's window is set from. It sends transactions: testnet only,
and run by hand.

Every slow call is tried with eth_estimateGas first and sent only if the contract would take it.
The keeper holds no trader's key except, in --measure, the one it is given. Testnet only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
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
EXAM_STATUS = ("None", "Active", "Passed", "Breached", "Expired")
PAYOUT_STATE = ("None", "Due", "Stopped", "Paid", "Held", "Refused")
EXAM_TERMS = "(uint64,uint16[],uint32,uint32,uint16,uint32,uint16,uint16)"
PAYOUT = "(address,address,uint64,uint64,uint8,bool,uint64)"
# A fill's gas, fixed: an estimate first would cost a block. PaperExam's fill, with sixteen assets
# open, is under it (forge test --gas-report).
FILL_GAS = 400_000
PAY_PER_CALL = 16


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


def pay_due(head: int, count: int, state: str, due_at: int, now: int) -> bool:
    """Whether `pay` would pay something: the first unsettled payout is due and its time has come, or
    it is settled and later ones may be (pay walks past it)."""
    if head >= count:
        return False
    return state != "Due" or due_at <= now


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
        self.filler: str | None = None
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if to_checksum_address(state["factory"]) != self.factory:
                raise SystemExit(f"{state_path} belongs to another factory")
            self.next_block = int(state["next_block"])
            self.pools = [to_checksum_address(p) for p in state["pools"]]
            self.accounts = {to_checksum_address(a): v for a, v in state["accounts"].items()}
            self.done = {to_checksum_address(a) for a in state.get("done", [])}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"factory": self.factory, "next_block": self.next_block, "pools": self.pools,
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
            log("not_due", to=to, call=sig.split("(")[0], reason=refusal(exc))
            return False
        if self.dry:
            log("would_send", to=to, call=sig.split("(")[0])
            return True
        try:
            rcpt = c.transact(self.wallet, to, sig, list(types), list(args))
            log("sent", to=to, call=sig.split("(")[0], tx=rcpt["transactionHash"])
            return True
        except Exception as exc:  # one failed call must not stop the pass
            log("send_failed", to=to, call=sig.split("(")[0], error=refusal(exc))
            return False

    def fill(self, exam: str) -> dict | None:
        """The fast loop's fill: sent at once with a fixed gas, no estimate. The receipt, or None."""
        if self.dry or not self.is_filler():
            log("would_fill", exam=exam)
            return None
        try:
            rcpt = c.send_tx(self.wallet, exam, c.encode_call("fill()"), gas=FILL_GAS)
        except Exception as exc:
            log("fill_failed", exam=exam, error=refusal(exc))
            return None
        late = [filled_of(l) for l in rcpt.get("logs", []) if l["topics"] and l["topics"][0] == FILLED]
        log("filled", exam=exam, tx=rcpt["transactionHash"], block=int(rcpt["blockNumber"], 16),
            late=late[0][0] if late else None, source=late[0][1] if late else None)
        return rcpt

    # ── the slow pass ────────────────────────────────────────────────────────────────

    def scan(self, latest: int) -> None:
        for p in view(self.factory, "pools()", "address[]"):
            p = to_checksum_address(p)
            if p not in self.pools:
                self.pools.append(p)
                log("pool_found", pool=p)
        end = min(latest, self.next_block + self.window * self.max_windows - 1)
        while self.pools and self.next_block <= end:
            hi = min(self.next_block + self.window - 1, end)
            try:
                logs = c.rpc("eth_getLogs", [{"address": self.pools, "topics": [[EXAM_SOLD, FUNDED]],
                                              "fromBlock": hex(self.next_block), "toBlock": hex(hi)}])
            except Exception as exc:
                log("scan_stopped", at_block=self.next_block, error=str(exc)[:200])
                break
            for l in logs:
                kind = "exam" if l["topics"][0] == EXAM_SOLD else "stage"
                a = to_checksum_address("0x" + l["topics"][1][-40:])
                self.accounts.setdefault(a, {"pool": to_checksum_address(l["address"]), "kind": kind})
            self.next_block = hi + 1
            self.save()

    def account_pass(self, a: str, now: int) -> None:
        info = self.accounts[a]
        if c.call_view(info["pool"], "accountOf(address)", ["address"], [a], ["uint8", "uint8", "bool"])[2]:
            self.done.add(a)  # released, funded or claimed: the pool holds it done
            return
        status, _, _, passed_at = c.call_view(a, "result()", [], [], ["uint8", "address", "uint64", "uint64"])
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
        fund_window = view(info["pool"], "fundWindow()", "uint32") if info["kind"] == "exam" else 0
        for call in account_calls(info["kind"], status, now, passed_at=passed_at, fund_window=fund_window,
                                  broken=bool(broken), over=bool(over), snapshot_due=bool(snapshot_due)):
            if call in ("checkpoint", "breach", "expire"):
                self.send(a, f"{call}()")
            elif self.send(info["pool"], f"{call}(address)", ["address"], [a]) and not self.dry:
                self.done.add(a)

    def pool_pass(self, pool: str, now: int) -> None:
        head = view(pool, "head()", "uint256")
        count = view(pool, "payoutCount()", "uint256")
        if head >= count:
            return
        p = view(pool, "payout(uint256)", PAYOUT, ["uint256"], [head])
        if pay_due(head, count, PAYOUT_STATE[p[4]], p[3], now):
            self.send(pool, "pay(uint256)", ["uint256"], [PAY_PER_CALL])

    def slow_pass(self) -> None:
        latest = int(c.rpc("eth_blockNumber"), 16)
        now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
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
        self.save()
        log("pass_done", pools=len(self.pools), live=len(self.live()), next_block=self.next_block)

    # ── the fast loop ────────────────────────────────────────────────────────────────

    def fast_step(self, latest: int) -> list[str]:
        """Reads the `Placed` events since the last look, and the pools' new exams and stages with them, and
        sends the fills that are due for the next block. The exams it sent a fill for.

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
            for l in c.rpc("eth_getLogs", [{"address": watched, "topics": [[PLACED, EXAM_SOLD, FUNDED]],
                                            "fromBlock": hex(lo), "toBlock": hex(latest)}]):
                if l["topics"][0] == PLACED:
                    self._order(l)
                    continue
                a = to_checksum_address("0x" + l["topics"][1][-40:])
                if a not in self.accounts:
                    kind = "exam" if l["topics"][0] == EXAM_SOLD else "stage"
                    self.accounts[a] = {"pool": to_checksum_address(l["address"]), "kind": kind}
                    new.append(a)
            if new:  # their orders in the same blocks, which the read above could not name
                for l in c.rpc("eth_getLogs", [{"address": new, "topics": [PLACED],
                                                "fromBlock": hex(lo), "toBlock": hex(latest)}]):
                    self._order(l)
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
        return sent

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
        started = time.time()
        while time.time() - started < timeout_s:
            logs = c.rpc("eth_getLogs", [{"address": exam, "topics": [[FILLED, LAPSED]],
                                          "fromBlock": hex(placed_block), "toBlock": "latest"}])
            filled = [l for l in logs if l["topics"][0] == FILLED and int(l["blockNumber"], 16) > placed_block]
            if filled:
                row["late"], row["source"] = filled_of(filled[0])
                row["filled_block"] = int(filled[0]["blockNumber"], 16)
                late.append(row["late"])
                break
            if c.call_view(exam, "pending()", [], [], ["uint32", "bool", "uint64", "uint64"])[2] == 0:
                break
            time.sleep(1)
        else:
            row["lapsed"] = True
        rows.append(row)
        log("measured", **row)
    summary = {"exam": exam, "orders": orders, "filled": len(late), **quantiles(late), "rows": rows}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/ with a SimPoolFactory")
    p.add_argument("--wallet", default="keeper", help="the factory's filler, or any funded testnet key")
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
    p.add_argument("--asset", type=int, default=3, help="the asset --measure trades (BTC, 3)")
    p.add_argument("--size", type=int, default=12, help="size units per --measure order (BTC: 0.00012, over 10 USDC)")
    p.add_argument("--orders", type=int, default=20)
    p.add_argument("--measure-out", default="ops/state/sim-measure.json")
    args = p.parse_args(argv)

    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    if args.measure:
        if not args.trader_wallet:
            raise SystemExit("--measure needs --trader-wallet, the exam's trader")
        s = measure(to_checksum_address(args.measure), c.account(args.trader_wallet), args.asset, args.size,
                    args.orders, pathlib.Path(args.measure_out))
        print(json.dumps({k: s[k] for k in ("orders", "filled", "p50", "p99", "max") if k in s}))
        return 0
    wallet = None if args.dry_run else c.account(args.wallet)
    sender = wallet.address if wallet else (args.sender or c.address_of(args.wallet))
    state = pathlib.Path(args.state) if args.state else STATE_DIR / f"sim-{args.deployment}.json"
    k = SimKeeper(record["SimPoolFactory"], wallet, sender, args.dry_run, state, int(record["block"]),
                  window=args.log_window, max_windows=args.max_windows)
    last_slow = 0.0
    while True:
        try:
            if time.time() - last_slow >= args.every:
                k.slow_pass()
                last_slow = time.time()
            if args.once:
                return 0
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
