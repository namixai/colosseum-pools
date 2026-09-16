"""The operator's keeper: the calls anyone may make, made on time.

    spike/.venv/bin/python -m ops.keeper --deployment demo --once --dry-run   # what is due
    spike/.venv/bin/python -m ops.keeper --deployment demo --once             # one pass
    spike/.venv/bin/python -m ops.keeper --deployment demo                    # a service loop

For every pool and every challenge it can see, one pass does what is due:
  challenge Created  → activate once the capital is there; abort after the start window if not
  challenge Active   → checkpoint in the first minutes of a UTC day; breach if a rule is broken;
                       expire after the deadline
  challenge stopped  → settle one step
  pool Funded        → checkpoint; breach if a rule is broken
  pool Closing       → settleFunded one step

Nothing here is privileged: the keeper uses its own testnet wallet (--wallet, default "keeper")
and only calls functions that are open to everyone. It never graduates a challenge: passing
early cuts the trader's profit short, so that call is the trader's. Testnet only.

Each pass makes about a dozen RPC reads per pool, and the public testnet RPC is rate limited,
so keep the interval at tens of seconds unless the keeper has its own RPC.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from eth_utils import to_checksum_address

from ops import deployments
from spike.hlspike import common as c

ZERO = "0x0000000000000000000000000000000000000000"
CANCEL = "(uint32,uint64)[]"
CHECKPOINT_WINDOW = 15 * 60
START_WINDOW = 3600


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


def challenge_pass(wallet, ch: str, names, now: int, dry: bool) -> None:
    status = view(ch, "status()", "uint8")
    if status == 1:  # Created
        if view(ch, "capitalArrived()", "bool"):
            send(wallet, ch, "activate()", dry=dry)
        elif now > view(ch, "createdAt()", "uint64") + START_WINDOW:
            send(wallet, ch, "abort()", dry=dry)
        return
    allowed = rules_assets(ch)
    cancels, extra = stop_inputs(ch, allowed, names)
    if status == 2:  # Active
        if in_checkpoint_window(now) and view(ch, "day()", "uint32") < now // 86400:
            send(wallet, ch, "checkpoint()", dry=dry)
        reason = view(ch, "violation(uint32[])", "uint8", ["uint32[]"], [extra])
        if reason:
            log("breach_found", account=ch, reason=reason)
            send(wallet, ch, f"breach({CANCEL},uint32[],bytes32)", [CANCEL, "uint32[]", "bytes32"],
                 [cancels, extra, os.urandom(32)], dry=dry)
        elif now > view(ch, "deadline()", "uint64"):
            send(wallet, ch, f"expire({CANCEL},uint32[],bytes32)", [CANCEL, "uint32[]", "bytes32"],
                 [cancels, extra, os.urandom(32)], dry=dry)
    elif status in (3, 4, 5, 6, 7):  # stopped, not yet settled
        send(wallet, ch, f"settle({CANCEL},uint32[])", [CANCEL, "uint32[]"], [cancels, extra], dry=dry)


def pool_pass(wallet, pool: str, names, now: int, dry: bool) -> None:
    stage = view(pool, "stage()", "uint8")
    challenge = to_checksum_address(view(pool, "challenge()", "address"))
    if challenge != ZERO:
        challenge_pass(wallet, challenge, names, now, dry)
    if stage not in (2, 3):
        return
    allowed = rules_assets(pool)
    cancels, extra = stop_inputs(pool, allowed, names)
    if stage == 2:  # Funded
        if in_checkpoint_window(now) and view(pool, "day()", "uint32") < now // 86400:
            send(wallet, pool, "checkpoint()", dry=dry)
        reason = view(pool, "violation(uint32[])", "uint8", ["uint32[]"], [extra])
        if reason:
            log("breach_found", account=pool, reason=reason)
            send(wallet, pool, f"breach({CANCEL},uint32[],bytes32)", [CANCEL, "uint32[]", "bytes32"],
                 [cancels, extra, os.urandom(32)], dry=dry)
    else:  # Closing
        send(wallet, pool, f"settleFunded({CANCEL},uint32[])", [CANCEL, "uint32[]"], [cancels, extra], dry=dry)


def one_pass(wallet, factory: str, dry: bool) -> None:
    names = perp_index_by_name()
    pools = view(factory, "pools()", "address[]")
    now = int(time.time())
    for pool in pools:
        try:
            pool_pass(wallet, to_checksum_address(pool), names, now, dry)
        except Exception as exc:
            log("pool_failed", pool=pool, error=str(exc)[:200])
    log("pass_done", pools=len(pools))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", help="label of a record in deployments/")
    p.add_argument("--factory", help="PoolFactory address, instead of --deployment")
    p.add_argument("--wallet", default="keeper")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=30, help="seconds between passes")
    p.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    args = p.parse_args()

    c.assert_testnet()
    wallet = None if args.dry_run else c.account(args.wallet)
    if args.factory:
        factory = to_checksum_address(args.factory)
    elif args.deployment:
        factory = to_checksum_address(deployments.load(args.deployment)["PoolFactory"])
    else:
        raise SystemExit("pass --deployment <label> or --factory <address>")
    while True:
        one_pass(wallet, factory, args.dry_run)
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
