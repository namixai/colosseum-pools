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
                       expire after the deadline; otherwise say so if a position has no stop
  challenge stopped  → recut if the cut key still trades; settle one step
  pool Funded        → checkpoint; breach if a rule is broken; otherwise the same stop check
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

The reads of Hyperliquid's own API are limited too, and that budget is the host's rather than the
keeper's: the public gateway draws on the same 1200 a minute. So a pass is not guaranteed to
finish, and the pools it does not reach go unserved until the next one. It takes them in sorted
order but begins where the last pass got to, not at the top every time -- under a fixed order the
same accounts are last in every round, and where an address sorts is something its owner chooses
(audit A-14). The place is `resume_at` in the state file, and an account whose venue read was
refused does not count as a turn taken.

For the same reason the contract is asked first and asked with an empty list. Drawdown, daily
loss and leverage come off the margin precompile over EVM, which the keeper reads through its own
node: those three are answered whatever Hyperliquid is doing, and a breach found that way is sent
with no cancel list rather than not sent. Only the forbidden-asset rule needs the venue, because
the contract holds the allowed set but not the account's open positions.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import pathlib
import sys
import time
from decimal import Decimal

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
IDLE, CHALLENGE, FUNDED, CLOSING, PASSED_AWAITING_KEY = range(5)


# A stop cost 194818 gas at 0.1 gwei on 25 Sep 2026 -- 0.0000195 HYPE -- and a settle step
# 0.0000115. This floor is about fifty stops: a RESERVE line, not the price of one transaction.
# A wallet just under it can still pay for a good many stops, so `unfunded` says the reserve is
# running out and wants topping up; it never claims that this particular send could not have
# gone through. The raw balance goes in the same line for that, and `gas_wei: 0` is the one
# reading that leaves no doubt. Measured after a keeper found a real breach and could not send
# it: the node answered 500, which reads exactly like a rate limit, and the cause was a wallet
# that had never been funded and had sent nothing in its life.
GAS_FLOOR_WEI = 10**15


def unfunded(balance_wei: int, floor_wei: int = GAS_FLOOR_WEI) -> bool:
    """Whether the keeper's gas reserve has run below the floor and wants topping up. Not a claim
    that a given send was impossible: a balance just under the floor still pays for many."""
    return int(balance_wei) < int(floor_wei)


def gas_balance(wallet) -> int | None:
    """The keeper's own gas balance, or None if it can't be read. Swallows its own failure: this
    is called while reporting someone else's, and must not replace it with a second one."""
    try:
        return c.evm_balance(wallet.address)
    except Exception:
        return None


def log(event: str, **fields) -> None:
    print(json.dumps({"t": int(time.time()), "event": event, **fields}, default=str), flush=True)


def view(addr: str, sig: str, out: str, types=(), args=()):
    return c.call_view(addr, sig, list(types), list(args), [out])[0]


# The perp list, kept from the last pass that could read it. Hyperliquid lists new perps from
# time to time and never renumbers the old ones, so a list one pass out of date is right about
# every asset it names.
_perp_names: dict[str, int] = {}


def perp_index_by_name() -> dict[str, int]:
    """The perp index by name, or the last one we had if Hyperliquid will not answer.

    This read sits outside the per-pool guard, so a refusal here used to end the whole pass before
    a single account was looked at -- and a pass that ends before it starts never moves the round
    on, which makes one refused read the start of the tail A-14 is about. On the very first pass
    there is nothing to fall back to and the names go empty: that costs the cancel list and the
    forbidden-asset list, and leaves the contract's own rules, which need no names at all.
    """
    global _perp_names
    try:
        _perp_names = {a["name"]: i for i, a in enumerate(c.info_post({"type": "meta"})["universe"])}
    except Exception as exc:
        log("meta_read_failed", error=str(exc)[:200], names=len(_perp_names))
    return _perp_names


def read_book(account: str) -> tuple[dict, list]:
    """An account's positions and open orders, one read of each, for everything a pass decides about it.

    Hyperliquid weighs clearinghouseState 2 and frontendOpenOrders 20, of 1,200 a minute per IP that
    the gateway on the same host shares. The stop check once read both again, which made an active
    account cost 44 a pass instead of 22. frontendOpenOrders lists the same orders as openOrders, in
    the same order, trigger orders included (spike/README.md, question 17), and says which is a stop."""
    return (c.info_post({"type": "clearinghouseState", "user": account}),
            c.info_post({"type": "frontendOpenOrders", "user": account}))


def stop_inputs(account: str, allowed: set[int], names: dict[str, int],
                book: tuple[dict, list] | None = None) -> tuple[list, list]:
    state, orders = book or read_book(account)
    cancels = [(names[o["coin"]], int(o["oid"])) for o in orders if o["coin"] in names][:32]
    extra = sorted({names[p["position"]["coin"]] for p in state.get("assetPositions", [])
                    if p["position"]["coin"] in names and names[p["position"]["coin"]] not in allowed})[:16]
    return cancels, extra


class NotFullyChecked(Exception):
    """The contract's rules were answered for this account; Hyperliquid's were not.

    Raised at the end of a pool's turn, after everything the contract alone could decide has been
    done and any breach it found has already been sent. It exists so the round does not count this
    account as served: the forbidden-asset rule and the stop check both need the venue, and a
    round that counted a refused read as done would leave the same accounts missing them pass
    after pass -- which is the tail A-14 is about, arriving by a second door.
    """


def venue_book(account: str) -> tuple[dict, list] | None:
    """`read_book`, or None when Hyperliquid would not answer.

    A refused venue read must not decide anything about an account. What it costs is the list of
    resting orders to cancel and the list of assets to name to the contract; what it does not cost
    is the contract's own rules -- drawdown, daily loss and leverage all come off the margin
    precompile over EVM (`RuledAccount.violation`), and the keeper reads EVM through its own node
    with its own budget. Before A-14 this read came first and threw, so an account went entirely
    unchecked exactly when the shared budget was tight: the one moment the check is worth having.
    """
    try:
        return read_book(account)
    except Exception as exc:
        log("venue_read_failed", account=account, error=str(exc)[:200])
        return None


def stop_args(account: str, names: dict[str, int],
              book: tuple[dict, list] | None) -> tuple[list, list]:
    """The cancel list and the forbidden assets for a stop, empty when the venue will not answer.

    A stop with no cancels leaves the resting orders for the next pass. A stop not sent leaves the
    rule broken, which is the thing money is lost to.
    """
    if book is None:
        book = venue_book(account)
    if book is None:
        return [], []
    return stop_inputs(account, rules_assets(account), names, book)


def send(wallet, addr: str, sig: str, types=(), args=(), dry=False) -> None:
    if dry:
        log("would_send", to=addr, call=sig)
        return
    try:
        rcpt = c.transact(wallet, addr, sig, list(types), list(args))
        log("sent", to=addr, call=sig, tx=rcpt["transactionHash"])
    except Exception as exc:  # one failed call must not stop the pass
        # A keeper that finds a breach and cannot send it is the one failure that matters, and
        # "send_failed" alone read the same whether the node was busy for a second or the wallet
        # had been empty for days. The balance goes in the line so the two can be told apart --
        # as a fact, not as a verdict on this send.
        gas = gas_balance(wallet)
        log("send_failed", to=addr, call=sig, error=str(exc)[:200], gas_wei=gas,
            unfunded=None if gas is None else unfunded(gas))


def unprotected(account: str, book: tuple[dict, list] | None = None) -> list[dict]:
    """Open positions with no stop on Hyperliquid itself: no reduce-only stop market order on the side
    that would close them, for the whole position. The gateway puts one there before every order that may
    open a position, and sweeps for the rest (gateway/protect.py); the keeper holds no key to place
    one, so all it can do is say so."""
    state, orders = book or read_book(account)
    positions = state.get("assetPositions", [])
    gaps = []
    for entry in positions:
        p = entry["position"]
        size = Decimal(p["szi"])
        if size == 0:
            continue
        closing = "A" if size > 0 else "B"
        if not any(o.get("coin") == p["coin"] and o.get("isTrigger") and o.get("reduceOnly") and o.get("side") == closing
                   and o.get("orderType") == "Stop Market"  # a stop limit may rest unfilled past its trigger
                   and (o.get("isPositionTpsl") or Decimal(o.get("sz") or "0") >= abs(size))
                   for o in orders):
            gaps.append({"coin": p["coin"], "size": p["szi"]})
    return gaps


def check_protection(account: str, book: tuple[dict, list] | None = None) -> None:
    """Logs each open position that has no stop on the exchange. A failure is logged too and never
    stops the pass: the rules are still the contract's to enforce."""
    try:
        for gap in unprotected(account, book):
            log("protection_missing", account=account, **gap)
    except Exception as exc:
        log("protection_check_failed", account=account, error=str(exc)[:200])


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


def whats_broken(account: str, names: dict[str, int]) -> tuple[int, tuple[dict, list] | None]:
    """The contract's verdict on an account, and the venue reads if they were needed and answered.

    The contract goes first and is asked with an empty list, because drawdown, daily loss and
    leverage need nothing but the margin precompile. Only if it says nothing is broken do we ask
    Hyperliquid, and then for one reason: the forbidden-asset rule is the one rule the contract
    cannot check on its own -- it holds the allowed set, but the account's open positions are not
    in its storage, so the candidate assets have to be named to it. It still verifies each one
    against the chain before calling it a breach (`RuledAccount.violation`), so a wrong candidate
    list cannot manufacture a stop; a missing one can only fail to find this rule, and leaves the
    three money rules answered either way.

    The account's book is read ONCE here and handed back, because everything else this pass does
    with the account wants the same reading: the cancel list for a stop, and the stop check that
    runs when nothing is broken. Reading it twice is 44 of Hyperliquid's weight an account instead
    of 22, on a budget the gateway on this host shares.
    """
    reason = view(account, "violation(uint32[])", "uint8", ["uint32[]"], [[]])
    if reason:
        return reason, None
    book = venue_book(account)
    if book is None:
        return 0, None
    _, extra = stop_inputs(account, rules_assets(account), names, book)
    if extra:
        return view(account, "violation(uint32[])", "uint8", ["uint32[]"], [extra]), book
    return 0, book


def challenge_pass(wallet, ch: str, names, now: int, latest: int, dry: bool) -> bool:
    """One pass over a challenge. False when Hyperliquid refused a read this account needed."""
    status = view(ch, "status()", "uint8")
    if status == CREATED:
        if view(ch, "keySpoiled()", "bool"):
            log("key_spoiled", account=ch)
            send(wallet, ch, "abort()", dry=dry)
        elif view(ch, "capitalArrived()", "bool"):
            send(wallet, ch, "activate()", dry=dry)
        elif now > view(ch, "createdAt()", "uint64") + START_WINDOW:
            send(wallet, ch, "abort()", dry=dry)
        return True
    if status != ACTIVE and status not in STOPPED:
        return True
    if status == ACTIVE:
        if in_checkpoint_window(now) and view(ch, "day()", "uint32") < now // 86400:
            send(wallet, ch, "checkpoint()", dry=dry)
        reason, book = whats_broken(ch, names)
        if reason:
            log("breach_found", account=ch, reason=reason)
            cancels, extra = stop_args(ch, names, book)
            stop(wallet, ch, "breach", cancels, extra, dry)
        elif now > view(ch, "deadline()", "uint64"):
            cancels, extra = stop_args(ch, names, book)
            stop(wallet, ch, "expire", cancels, extra, dry)
        else:
            check_protection(ch, book)
        return book is not None
    else:
        recut_if_uncut(wallet, ch, latest, dry)
        # Settling keeps the old behaviour: a refused read raises, the pass logs the pool and
        # comes back to it. Nothing is at stake in the meantime -- the account is already stopped
        # -- so there is no reason to spend a transaction on a settle step with no cancels in it.
        cancels, extra = stop_inputs(ch, rules_assets(ch), names)
        send(wallet, ch, f"settle({CANCEL},uint32[])", [CANCEL, "uint32[]"], [cancels, extra], dry=dry)
    return True


def pool_pass(wallet, pool: str, names, now: int, latest: int, dry: bool) -> bool:
    """One pass over a pool and its challenge. False once the pool has nothing left to do."""
    stage = view(pool, "stage()", "uint8")
    challenge = to_checksum_address(view(pool, "challenge()", "address"))
    checked = True
    if challenge != ZERO:
        checked = challenge_pass(wallet, challenge, names, now, latest, dry)
    if stage == PASSED_AWAITING_KEY:
        # The trader met the target and the pass is already recorded; the stage only needs a
        # live key. The call is open to anyone and reverts NoFreeKey when there is none, so it
        # is worth trying every pass: the pool holds the investor's capital until it succeeds.
        send(wallet, pool, "openFundedStage()", dry=dry)
    if stage in (FUNDED, CLOSING):
        if stage == FUNDED:
            if in_checkpoint_window(now) and view(pool, "day()", "uint32") < now // 86400:
                send(wallet, pool, "checkpoint()", dry=dry)
            reason, book = whats_broken(pool, names)
            if reason:
                log("breach_found", account=pool, reason=reason)
                cancels, extra = stop_args(pool, names, book)
                stop(wallet, pool, "breach", cancels, extra, dry)
            else:
                check_protection(pool, book)
            checked = checked and book is not None
        else:
            recut_if_uncut(wallet, pool, latest, dry)
            cancels, extra = stop_inputs(pool, rules_assets(pool), names)
            send(wallet, pool, f"settleFunded({CANCEL},uint32[])", [CANCEL, "uint32[]"], [cancels, extra], dry=dry)
    if not checked:
        # After the work, not instead of it: a breach the contract found is already sent, and the
        # pool stays in `live` because this says "not finished", not "gone".
        raise NotFullyChecked(pool)
    # A pool waiting for a key is not finished with, even when its challenge has settled.
    return not (stage == IDLE and challenge == ZERO)


def pools_with_challenges(factory: str, lo: int, hi: int) -> set[str]:
    """Pools named by the factory's ChallengeCreated events in blocks [lo, hi], one call."""
    found: set[str] = set()
    logs = c.rpc("eth_getLogs", [{
        "address": factory, "topics": [CHALLENGE_CREATED], "fromBlock": hex(lo), "toBlock": hex(hi),
    }])
    for entry in logs:
        # Only the factory's own events count; an RPC that ignored the filter changes nothing.
        if entry["address"].lower() != factory.lower() or entry["topics"][0] != CHALLENGE_CREATED:
            continue
        found.add(to_checksum_address("0x" + entry["topics"][2][-40:]))
    return found


def ring(order: list[str], resume_at: str | None) -> list[str]:
    """The pools of one pass, in the order this pass takes them: sorted, but begun where the last
    pass broke off instead of at the top every time.

    A pass is not guaranteed to finish. Hyperliquid's REST budget belongs to the host, not to the
    keeper -- the public gateway draws on the same 1200 a minute -- and a pass that runs out of it
    fails the pools it has not reached. Under a fixed order those are the same pools every pass:
    the end of the sorted list is never served, and where an address sorts is something its owner
    chooses. Rotating the start makes the tail a place in the round rather than a set of accounts.
    """
    if not order:
        return []
    start = bisect.bisect_left(order, resume_at) % len(order) if resume_at else 0
    return order[start:] + order[:start]


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
        # Where the next pass begins its round. Read with a default, because a keeper started on
        # a state file written before this existed -- the demo's, and the second keeper's, which
        # was seeded from it -- must come up rather than die on a missing key.
        self.resume_at: str | None = None
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if to_checksum_address(state["factory"]) != self.factory:
                raise SystemExit(f"{state_path} belongs to another factory")
            self.next_block = int(state["next_block"])
            self.live = {to_checksum_address(p) for p in state["live"]}
            at = state.get("resume_at")
            self.resume_at = to_checksum_address(at) if at else None

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"factory": self.factory, "next_block": self.next_block,
                                   "live": sorted(self.live), "resume_at": self.resume_at},
                                  indent=2) + "\n")
        tmp.replace(self.state_path)

    def one_pass(self) -> None:
        latest = int(c.rpc("eth_blockNumber"), 16)
        end = min(latest, self.next_block + self.window * self.max_windows - 1)
        # Window by window, and the place moves after each window that came back. A refused read
        # ends the scan for this pass and keeps what it read: a pass that threw its whole scan away
        # on the last window started again from the same block every time, so after a gap -- a
        # deployment older than the keeper, or any restart behind the head -- it never caught up.
        # Measured on the demo, 23 Sep 2026: 50 windows a pass against a node that allows 100 calls
        # a minute, every pass refused part way, `next_block` at the deployment block all along.
        while self.next_block <= end:
            hi = min(self.next_block + self.window - 1, end)
            try:
                self.live |= pools_with_challenges(self.factory, self.next_block, hi)
            except Exception as exc:
                log("scan_stopped", at_block=self.next_block, error=str(exc)[:200])
                break
            self.next_block = hi + 1
            # On disk window by window. A refused read is handled above, but a process killed
            # mid-scan -- a redeploy restarts this service -- would otherwise start the next run
            # at the block this pass began at, which is the rescan the loop exists to avoid. It
            # also keeps what was read from the venue reads below, which can be refused too.
            self.save()
        names = perp_index_by_name()
        now = int(time.time())
        todo = ring(sorted(self.live), self.resume_at)
        served = None
        for i, pool in enumerate(todo):
            try:
                if not pool_pass(self.wallet, pool, names, now, latest, self.dry):
                    self.live.discard(pool)
                served = i
            except NotFullyChecked:
                # Not an error and not a turn taken: the contract's rules were answered, the
                # venue's were not, and the round has to come back to this one before it comes
                # back to the accounts it did get through.
                log("venue_incomplete", pool=pool)
            except Exception as exc:
                log("pool_failed", pool=pool, error=str(exc)[:200])
        if served is not None:
            # Next pass begins after the last pool this one actually got through, so a round cut
            # short by the shared budget carries on instead of starting over.
            #
            # After the last SUCCESS, not at the first failure: a pool that throws for its own
            # reasons -- a reverting view, an address the node dislikes -- would otherwise hold
            # the place at its own position for good, and everything behind it would go unserved.
            # That is the same starvation as the fixed order with a new cause. Success is what
            # moves the round, so nothing that only fails can own a place in it.
            #
            # A round that gets through everything wraps back to its own top, and that is right:
            # the order only has to move when a pass does not finish, which is the case this
            # exists for. When nothing at all got through there is nothing to carry on from, and
            # the next pass starts where this one did.
            self.resume_at = todo[(served + 1) % len(todo)]
        self.save()
        log("pass_done", following=len(self.live), next_block=self.next_block, latest=latest,
            resume_at=self.resume_at)


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
