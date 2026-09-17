"""The operator's keeper: the calls anyone may make, made on time.

    spike/.venv/bin/python -m ops.keeper --deployment demo --once --dry-run   # what is due
    spike/.venv/bin/python -m ops.keeper --deployment demo --once             # one pass
    spike/.venv/bin/python -m ops.keeper --deployment demo                    # a service loop

Anyone can create a pool for free, so the keeper doesn't walk the factory's pool list. It
follows the factory's ChallengeCreated events instead: a pool needs the keeper from its first
challenge on, and stops needing it once it is idle with no challenge left. A challenge costs
its buyer the price and the platform fee, and the pool real capital, which is what keeps that
list short. Events are read in windows of --log-window blocks (HyperEVM answers at most 50 per
eth_getLogs call), starting where the last run stopped (the --state file) or at the factory's
deployment block. A pass that fails, say on a rate-limited RPC, is logged and the next one
starts from the same block.

For every pool it follows, one pass does what is due:
  challenge Created  → abort at once if the reserved key is spoiled; activate once the capital
                       is there; abort after the start window if not
  challenge Active   → checkpoint in the first minutes of a UTC day; breach if a rule is broken;
                       expire after the deadline
  challenge stopped  → recut if the cut key still trades; settle one step
  pool Funded        → checkpoint; breach if a rule is broken
  pool Closing       → recut if the cut key still trades; settleFunded one step

HyperCore ignores a replacement agent that already has an account (spike question 8, 17 Sep
2026), and then the key a stop cut keeps trading. So on a stopped account the keeper asks the
info API whether `cutKey()` is still that account's agent, once `cutBlock()` is at least
RECUT_AFTER_BLOCKS behind, and calls `recut` with a fresh salt if it is.

Nothing here is privileged: the keeper uses its own testnet wallet (--wallet, default "keeper")
and only calls functions that are open to everyone. It never graduates a challenge: passing
early cuts the trader's profit short, so that call is the trader's. Testnet only.

Each pass makes about a dozen RPC reads per followed pool, and the public testnet RPC is rate
limited, so keep the interval at tens of seconds unless the keeper has its own RPC.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

from eth_utils import keccak, to_checksum_address

from ops import deployments
from spike.hlspike import common as c

ZERO = "0x0000000000000000000000000000000000000000"
CANCEL = "(uint32,uint64)[]"
CHECKPOINT_WINDOW = 15 * 60
START_WINDOW = 3600
CHALLENGE_CREATED = "0x" + keccak(text="ChallengeCreated(address,address,address)").hex()
STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"
MAX_LOG_WINDOW = 50  # blocks per eth_getLogs call that HyperEVM accepts
RECUT_AFTER_BLOCKS = 10  # CoreWriter actions land a few seconds after their block

# ChallengeAccount.Status and Pool.Stage. The tests hold these against the Solidity source.
CREATED, ACTIVE, BREACHED, EXPIRED, FORFEITED, PASSED, ABORTED, SETTLED = range(1, 9)
STOPPED = (BREACHED, EXPIRED, FORFEITED, PASSED, ABORTED)
IDLE, CHALLENGE, FUNDED, CLOSING = range(4)


def log(event: str, **fields) -> None:
    print(json.dumps({"t": int(time.time()), "event": event, **fields}, default=str), flush=True)


def view(addr: str, sig: str, out: str, types=(), args=()):
    return c.call_view(addr, sig, list(types), list(args), [out])[0]


def perp_index_by_name() -> dict[str, int]:
    return {a["name"]: i for i, a in enumerate(c.info_post({"type": "meta"})["universe"])}


def stop_inputs(account: str, allowed: set[int], names: dict[str, int]) -> tuple[list, list]:
    orders = c.info_post({"type": "openOrders", "user": account})
    cancels = [(names[o["coin"]], int(o["oid"])) for o in orders if o["coin"] in names][:32]
    state = c.info_post({"type": "clearinghouseState", "user": account})
    extra = sorted({names[p["position"]["coin"]] for p in state.get("assetPositions", [])
                    if p["position"]["coin"] in names and names[p["position"]["coin"]] not in allowed})[:16]
    return cancels, extra


def send(wallet, addr: str, sig: str, types=(), args=(), dry=False) -> None:
    if dry:
        log("would_send", to=addr, call=sig)
        return
    try:
        rcpt = c.transact(wallet, addr, sig, list(types), list(args))
        log("sent", to=addr, call=sig, tx=rcpt["transactionHash"])
    except Exception as exc:  # one failed call must not stop the pass
        log("send_failed", to=addr, call=sig, error=str(exc)[:200])


def in_checkpoint_window(now: int) -> bool:
    return now % 86400 < CHECKPOINT_WINDOW


def rules_assets(addr: str) -> set[int]:
    rules = c.call_view(addr, "rules()", [], [], ["(uint16,uint16,uint32,uint32[])"])[0]
    return set(rules[3])


def recut_if_uncut(wallet, account: str, latest: int, dry: bool) -> None:
    key = to_checksum_address(view(account, "cutKey()", "address"))
    if key == ZERO or latest < view(account, "cutBlock()", "uint64") + RECUT_AFTER_BLOCKS:
        return
    role = c.info_post({"type": "userRole", "user": key})
    agent_of = str((role.get("data") or {}).get("user", "")).lower()
    if role.get("role") == "agent" and agent_of == account.lower():
        log("cut_did_not_land", account=account, key=key)
        send(wallet, account, "recut(bytes32)", ["bytes32"], [os.urandom(32)], dry=dry)


def stop(wallet, addr: str, fn: str, cancels, extra, dry: bool) -> None:
    send(wallet, addr, f"{fn}({CANCEL},uint32[],bytes32)", [CANCEL, "uint32[]", "bytes32"],
         [cancels, extra, os.urandom(32)], dry=dry)


def challenge_pass(wallet, ch: str, names, now: int, latest: int, dry: bool) -> None:
    status = view(ch, "status()", "uint8")
    if status == CREATED:
        if view(ch, "keySpoiled()", "bool"):
            log("key_spoiled", account=ch)
            send(wallet, ch, "abort()", dry=dry)
        elif view(ch, "capitalArrived()", "bool"):
            send(wallet, ch, "activate()", dry=dry)
        elif now > view(ch, "createdAt()", "uint64") + START_WINDOW:
            send(wallet, ch, "abort()", dry=dry)
        return
    if status != ACTIVE and status not in STOPPED:
        return
    cancels, extra = stop_inputs(ch, rules_assets(ch), names)
    if status == ACTIVE:
        if in_checkpoint_window(now) and view(ch, "day()", "uint32") < now // 86400:
            send(wallet, ch, "checkpoint()", dry=dry)
        reason = view(ch, "violation(uint32[])", "uint8", ["uint32[]"], [extra])
        if reason:
            log("breach_found", account=ch, reason=reason)
            stop(wallet, ch, "breach", cancels, extra, dry)
        elif now > view(ch, "deadline()", "uint64"):
            stop(wallet, ch, "expire", cancels, extra, dry)
    else:
        recut_if_uncut(wallet, ch, latest, dry)
        send(wallet, ch, f"settle({CANCEL},uint32[])", [CANCEL, "uint32[]"], [cancels, extra], dry=dry)


def pool_pass(wallet, pool: str, names, now: int, latest: int, dry: bool) -> bool:
    """One pass over a pool and its challenge. False once the pool has nothing left to do."""
    stage = view(pool, "stage()", "uint8")
    challenge = to_checksum_address(view(pool, "challenge()", "address"))
    if challenge != ZERO:
        challenge_pass(wallet, challenge, names, now, latest, dry)
    if stage in (FUNDED, CLOSING):
        cancels, extra = stop_inputs(pool, rules_assets(pool), names)
        if stage == FUNDED:
            if in_checkpoint_window(now) and view(pool, "day()", "uint32") < now // 86400:
                send(wallet, pool, "checkpoint()", dry=dry)
            reason = view(pool, "violation(uint32[])", "uint8", ["uint32[]"], [extra])
            if reason:
                log("breach_found", account=pool, reason=reason)
                stop(wallet, pool, "breach", cancels, extra, dry)
        else:
            recut_if_uncut(wallet, pool, latest, dry)
            send(wallet, pool, f"settleFunded({CANCEL},uint32[])", [CANCEL, "uint32[]"], [cancels, extra], dry=dry)
    return not (stage == IDLE and challenge == ZERO)


def pools_with_challenges(factory: str, start: int, end: int, window: int) -> set[str]:
    """Pools named by the factory's ChallengeCreated events in blocks [start, end]."""
    found: set[str] = set()
    lo = start
    while lo <= end:
        hi = min(lo + window - 1, end)
        logs = c.rpc("eth_getLogs", [{
            "address": factory, "topics": [CHALLENGE_CREATED], "fromBlock": hex(lo), "toBlock": hex(hi),
        }])
        for entry in logs:
            # Only the factory's own events count; an RPC that ignored the filter changes nothing.
            if entry["address"].lower() != factory.lower() or entry["topics"][0] != CHALLENGE_CREATED:
                continue
            found.add(to_checksum_address("0x" + entry["topics"][2][-40:]))
        lo = hi + 1
    return found


class Keeper:
    def __init__(self, factory: str, wallet, dry: bool, state_path: pathlib.Path, start_block: int,
                 window: int = MAX_LOG_WINDOW, max_windows: int = 50):
        if not 1 <= window <= MAX_LOG_WINDOW:
            raise SystemExit(f"--log-window must be 1..{MAX_LOG_WINDOW}: HyperEVM refuses wider eth_getLogs ranges")
        if max_windows < 1:
            raise SystemExit("--max-windows must be at least 1: a pass that reads no logs never finds a challenge")
        self.factory = to_checksum_address(factory)
        self.wallet = wallet
        self.dry = dry
        self.state_path = state_path
        self.window = window
        self.max_windows = max_windows
        self.next_block = start_block
        self.live: set[str] = set()
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if to_checksum_address(state["factory"]) != self.factory:
                raise SystemExit(f"{state_path} belongs to another factory")
            self.next_block = int(state["next_block"])
            self.live = {to_checksum_address(p) for p in state["live"]}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"factory": self.factory, "next_block": self.next_block,
                                   "live": sorted(self.live)}, indent=2) + "\n")
        tmp.replace(self.state_path)

    def one_pass(self) -> None:
        latest = int(c.rpc("eth_blockNumber"), 16)
        end = min(latest, self.next_block + self.window * self.max_windows - 1)
        if end >= self.next_block:
            self.live |= pools_with_challenges(self.factory, self.next_block, end, self.window)
            self.next_block = end + 1
        names = perp_index_by_name()
        now = int(time.time())
        for pool in sorted(self.live):
            try:
                if not pool_pass(self.wallet, pool, names, now, latest, self.dry):
                    self.live.discard(pool)
            except Exception as exc:
                log("pool_failed", pool=pool, error=str(exc)[:200])
        self.save()
        log("pass_done", following=len(self.live), next_block=self.next_block, latest=latest)


def deployment_block(record: dict) -> int:
    if "block" in record:
        return int(record["block"])
    rcpt = c.rpc("eth_getTransactionReceipt", [record["tx"]["PoolFactory"]])
    return int(rcpt["blockNumber"], 16)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/")
    p.add_argument("--wallet", default="keeper")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=30, help="seconds between passes")
    p.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    p.add_argument("--state", help="state file (default ops/state/keeper-<deployment>.json)")
    p.add_argument("--log-window", type=int, default=MAX_LOG_WINDOW,
                   help=f"blocks per eth_getLogs call, 1..{MAX_LOG_WINDOW}")
    p.add_argument("--max-windows", type=int, default=50, help="eth_getLogs calls per pass, at most")
    args = p.parse_args()

    c.assert_testnet()
    record = deployments.load(args.deployment)
    state = pathlib.Path(args.state) if args.state else STATE_DIR / f"keeper-{args.deployment}.json"
    wallet = None if args.dry_run else c.account(args.wallet)
    keeper = Keeper(record["PoolFactory"], wallet, args.dry_run, state, deployment_block(record),
                    window=args.log_window, max_windows=args.max_windows)
    while True:
        try:
            keeper.one_pass()
        except Exception as exc:  # the RPC or the info API failed; the next pass retries
            log("pass_failed", error=str(exc)[:200], next_block=keeper.next_block)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
