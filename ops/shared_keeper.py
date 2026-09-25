"""The shared pool's keeper: the calls anyone may make on a SharedPool, made when the pool would take them.

    spike/.venv/bin/python -m ops.shared_keeper --deployment shared-run --once --dry-run   # what is due
    spike/.venv/bin/python -m ops.shared_keeper --deployment shared-run --once             # one pass
    spike/.venv/bin/python -m ops.shared_keeper --deployment shared-run                    # a service loop

The seats' challenges and funded stages are ordinary pools of the deployment's factory, and the core
keeper (ops/keeper.py, run with the same deployment) already takes them through their lives:
activate, checkpoint, breach, expire, settle, recut. This one does what only a shared pool has, in
this order on every pass:

  a funded seat       → noteFunded when its stage is new to the pool; endFundedTerm once the stage
                        has run the term the seat was published with
  a short queue       → releaseSeat for an idle seat holding capital, while the queue needs more
                        than the pool has free
  deposits, a queue   → settle, naming the open tickets that hold a deposit; for a queue alone at
                        most once every --queue-every seconds, since its money comes in slowly
  money to spare      → armSeat for an idle seat short of its capital, then prepareAccount on it

Every call is first tried with eth_estimateGas from the keeper's own address and sent only if the
contract would take it. The rules live in SharedPool; the keeper keeps no second copy of them, only
the judgement of when a call is worth its gas. It reads the contract's state and the precompiles
through eth_call and never the event log. Nothing here is privileged: `--wallet` is any funded
testnet key. Testnet only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from spike.hlspike import common as c

ZERO = core.ZERO
CANCEL = core.CANCEL
IDLE, CHALLENGE, FUNDED, CLOSING = core.IDLE, core.CHALLENGE, core.FUNDED, core.CLOSING
# SharedPool's constants the keeper plans with; the tests hold them against the Solidity source.
MAX_PER_POINT = 8
SWEEP_MIN = 100_000_000
# A small HyperEVM block holds 2M gas, and send_tx asks for a quarter more than the estimate.
SMALL_BLOCK_GAS = 2_000_000
GAS_HEADROOM = 1.25
OPEN_READ = 64  # open tickets read per pass; anyone can open them for the price of gas
# An empty ticket is read again only every this many passes. Anyone can open tickets for gas alone,
# and reading each of them on every pass would spend the public RPC's allowance on nothing. A new
# ticket is read at once: a deposit usually follows its ticket within seconds.
RECHECK_EVERY = 10

ERRORS = [
    "NotStarted()", "NotSeat(address)", "SeatBusy(address)", "TooSoon(address)", "AlreadyArmed(address)",
    "NotEnoughFree(uint64,uint64)", "NotQuiet(uint8,address)", "NoValue()", "TooMany()", "QueueWaiting()",
    "NoQueue()", "QueueCovered()", "PaymentsLanding(uint64)", "NotFunded(address)", "TermNotOver(uint64)",
    "NotReady()",
]
ERROR_NAMES = {"0x" + keccak(text=e)[:4].hex(): e.split("(")[0] for e in ERRORS}


def log(event: str, **fields) -> None:
    print(json.dumps({"t": int(time.time()), "event": event, **fields}, default=str), flush=True)


def view(addr: str, sig: str, out: str, types=(), args=()):
    return c.call_view(addr, sig, list(types), list(args), [out])[0]


def refusal(exc: Exception) -> str:
    """The contract's reason for a refused call, by name when it is one of SharedPool's errors."""
    m = re.search(r"0x[0-9a-fA-F]{8}", str(exc))
    return ERROR_NAMES.get(m.group(0).lower(), m.group(0)) if m else str(exc)[:120]


class SharedKeeper:
    def __init__(self, pool: str, wallet, sender: str, dry: bool, arm: bool = True, queue_every: int = 300):
        self.pool = to_checksum_address(pool)
        self.wallet = wallet
        self.sender = to_checksum_address(sender)
        self.dry = dry
        self.arm = arm
        self.queue_every = queue_every
        self.last_queue_point = 0
        self.passes = 0
        self.empty_at: dict[str, int] = {}  # open ticket -> the pass that last found it empty

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
        return self.deliver(to, sig, types, args)

    def deliver(self, to: str, sig: str, types=(), args=()) -> bool:
        """Sends a call already tried; in a dry run only says so."""
        if self.dry:
            log("would_send", to=to, call=sig.split("(")[0])
            return True
        try:
            rcpt = c.transact(self.wallet, to, sig, list(types), list(args))
            log("sent", to=to, call=sig.split("(")[0], tx=rcpt["transactionHash"])
            return True
        except Exception as exc:  # one failed call must not stop the pass
            log("send_failed", to=to, call=sig.split("(")[0], error=str(exc)[:200])
            return False

    # ── reads ────────────────────────────────────────────────────────────────────────

    def spot(self, account: str) -> int:
        return c.core_spot_balance(account, c.USDC_TOKEN)["total"]

    def seats(self) -> list[dict]:
        out = []
        for s in view(self.pool, "seats()", "address[]"):
            s = to_checksum_address(s)
            out.append({
                "address": s,
                "stage": view(s, "stage()", "uint8"),
                "challenge": to_checksum_address(view(s, "challenge()", "address")),
                "need": view(s, "capitalNeeded()", "uint64"),
                "ready": view(s, "accountReady()", "bool"),
                "spot": self.spot(s),
            })
        return out

    def funded_tickets(self, minimum: int) -> list[str]:
        count = view(self.pool, "openTicketCount()", "uint256")
        if not count:
            return []
        opened = [to_checksum_address(t) for t in view(self.pool, "openTickets(uint256,uint256)", "address[]",
                                                        ["uint256", "uint256"], [0, min(count, OPEN_READ)])]
        funded = []
        for t in opened:
            last = self.empty_at.get(t)
            if last is not None and self.passes - last < RECHECK_EVERY:
                continue
            if self.spot(t) >= minimum:
                funded.append(t)
                self.empty_at.pop(t, None)
            else:
                self.empty_at[t] = self.passes
        # Tickets no longer open were counted or never will be; forget them.
        self.empty_at = {t: n for t, n in self.empty_at.items() if t in opened}
        return funded

    def stray_on_closed(self) -> bool:
        """Money on a closed ticket that a point would sweep in: a late transfer, or a sweep of
        the last point that hasn't landed."""
        return any(self.spot(t) >= SWEEP_MIN for t in view(self.pool, "closedTickets()", "address[]"))

    # ── the pass ─────────────────────────────────────────────────────────────────────

    def funded_pass(self, seats: list[dict], names, now: int) -> None:
        for s in seats:
            if s["stage"] != FUNDED:
                continue
            seat = s["address"]
            since = view(self.pool, "fundedSince(address)", "uint64", ["address"], [seat])
            key = to_checksum_address(view(seat, "agentKey()", "address"))
            noted = to_checksum_address(view(self.pool, "fundedKey(address)", "address", ["address"], [seat]))
            if since == 0 or noted != key:
                self.send(self.pool, "noteFunded(address)", ["address"], [seat])
                continue
            if now < since + view(self.pool, "fundedTerm(address)", "uint32", ["address"], [seat]):
                continue
            cancels, extra = core.stop_inputs(seat, core.rules_assets(seat), names)
            log("funded_term_over", seat=seat, since=since)
            self.send(self.pool, f"endFundedTerm(address,{CANCEL},uint32[],bytes32)",
                      ["address", CANCEL, "uint32[]", "bytes32"], [seat, cancels, extra, os.urandom(32)])

    def release_pass(self, seats: list[dict], queued: int) -> None:
        if not queued:
            return
        for s in seats:
            if s["stage"] == IDLE and s["challenge"] == ZERO and s["spot"] > 0:
                self.send(self.pool, "releaseSeat(address)", ["address"], [s["address"]])

    def point(self, now: int) -> None:
        minimum = view(self.pool, "minDeposit()", "uint64")
        tickets = self.funded_tickets(minimum)
        queued = view(self.pool, "queuedShares()", "uint256")
        queue_due = queued > 0 and now >= self.last_queue_point + self.queue_every
        if not tickets and not queue_due and not self.stray_on_closed():
            return
        # The seats and the queue are what they are; the tickets are what the keeper can leave for
        # the next point when a point would not fit a small block.
        named = tickets[:MAX_PER_POINT]
        while True:
            gas = self.gas(self.pool, "settle(address[])", ["address[]"], [named])
            if gas is None:
                return
            if gas * GAS_HEADROOM <= SMALL_BLOCK_GAS or not named:
                break
            log("point_too_big", gas=gas, tickets=len(named))
            named = named[: len(named) // 2]
        if gas * GAS_HEADROOM > SMALL_BLOCK_GAS:
            log("point_needs_big_block", gas=gas,
                hint="enable big blocks for the keeper's wallet (evmUserModify usingBigBlocks)")
            return
        if self.deliver(self.pool, "settle(address[])", ["address[]"], [named]) and queued:
            self.last_queue_point = now

    def arm_pass(self, seats: list[dict]) -> None:
        for s in seats:
            if s["stage"] != IDLE or s["challenge"] != ZERO:
                continue
            if s["spot"] < s["need"]:
                if self.arm:
                    self.send(self.pool, "armSeat(address)", ["address"], [s["address"]])
            elif not s["ready"]:
                self.send(s["address"], "prepareAccount()")

    def one_pass(self) -> None:
        self.passes += 1
        now = int(time.time())
        names = core.perp_index_by_name()
        seats = self.seats()
        self.funded_pass(seats, names, now)
        self.release_pass(seats, view(self.pool, "queuedShares()", "uint256"))
        self.point(now)
        self.arm_pass(self.seats())
        log("pass_done", pool=self.pool, seats=len(seats))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/ with a SharedPool")
    p.add_argument("--wallet", default="keeper")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=30, help="seconds between passes")
    p.add_argument("--queue-every", type=int, default=300,
                   help="seconds between points run only for the queue (a deposit runs one at once)")
    p.add_argument("--no-arm", dest="arm", action="store_false", help="never top a seat up")
    p.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    p.add_argument("--from", dest="sender", help="address the calls are tried from in a dry run")
    args = p.parse_args()

    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "SharedPool" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no shared pool")
    wallet = None if args.dry_run else c.account(args.wallet)
    sender = wallet.address if wallet else (args.sender or c.address_of(args.wallet))
    k = SharedKeeper(record["SharedPool"], wallet, sender, args.dry_run, arm=args.arm, queue_every=args.queue_every)
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
