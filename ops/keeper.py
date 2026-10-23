"""The operator's keeper: the calls anyone may make, made on time.

    spike/.venv/bin/python -m ops.keeper --deployment demo --once --dry-run   # what is due
    spike/.venv/bin/python -m ops.keeper --deployment demo --once             # one pass
    spike/.venv/bin/python -m ops.keeper --deployment demo                    # a service loop

Anyone can create a pool for free, so the keeper doesn't serve the factory's pool list. It
follows the factory's ChallengeCreated events instead: a pool needs the keeper from its first
challenge on, and stops needing it once it is idle with no challenge left. A challenge costs
its buyer the price and the platform fee, and the pool real capital, which is what keeps that
list short. Events are read in windows of --log-window blocks (HyperEVM answers at most 50 per
eth_getLogs call), starting where the last run stopped (the --state file) or at the factory's
deployment block. A pass that fails, say on a rate-limited RPC, is logged and the next one
starts from the same block.

The events are not enough on their own. A node that will not serve old logs leaves the scan on
one block for good, and every challenge bought after it goes unseen -- with the exam still
running and nobody to take its day's snapshot or stop it. Measured 16-18 Oct 2026: the keeper's
node went down, the one that replaced it answers reads but times old eth_getLogs out, and the
scan had to be moved to the head by hand. So once every --sweep-every passes the keeper also
reads `pools()` from the factory and asks each pool it is not following for `stage()` and
`challenge()`; one that is not idle, or has a challenge, is followed from then on. That costs
one call plus two per unfollowed pool, and anyone can add pools for free, so a sweep looks at
SWEEP_MAX of them at most and the next sweep carries on from where it stopped. A v3 factory keeps
the pools that have sold a challenge apart (`challengedCount`, `challengedFrom`), and the sweep
reads only those, SWEEP_MAX addresses a call: a pool that has sold none is never asked about,
however many there are (audit, K-1). A scan that has
not moved for SCAN_STUCK_PASSES passes with the head ahead of it says so (`scan_behind`).

For every pool it follows, one pass does what is due:
  challenge Created  → abort at once if the reserved key is spoiled; activate once the capital
                       is there, and on a builder dex once more when the dex shows it (the
                       first call only sends it there); abort after the start window if not
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

from gateway.assets import MAIN_DEX, dex_index_of, order_asset, parse_perp_dexs, weekend_flat_due
from ops import deployments
from ops.contract_errors import refusal
from spike.hlspike import common as c

ZERO = "0x0000000000000000000000000000000000000000"
CANCEL = "(uint32,uint64)[]"
CHECKPOINT_WINDOW = 15 * 60
START_WINDOW = 3600
CHALLENGE_CREATED = "0x" + keccak(text="ChallengeCreated(address,address,address)").hex()
STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"
MAX_LOG_WINDOW = 50  # blocks per eth_getLogs call that HyperEVM accepts
RECUT_AFTER_BLOCKS = 10  # CoreWriter actions land a few seconds after their block
DEFAULT_EVERY_S = 30
# The factory's pool list, read once every this many passes (about ten minutes at the default
# interval, and on the first pass after a start). It finds what the event scan cannot reach.
SWEEP_EVERY = 20
# Unfollowed pools asked about in one sweep, at two reads each. Anyone can create pools for free;
# this caps what that costs a sweep, and the next sweep starts where this one stopped.
SWEEP_MAX = 40
# Passes in a row with the scan on the same block and the head beyond it before `scan_behind`.
SCAN_STUCK_PASSES = 10
# What a refused read costs in time, and what it does NOT tell you.
#
# One refused read makes its pass up to `c.RPC_BACKOFF_TOTAL_S` longer, and the next pass starts
# `DEFAULT_EVERY_S` after this one ENDS -- `main` sleeps between passes, not between accounts. The
# sum of the two is NOT a bound on how late a breach is found, and the first version of this said
# it was: it leaves out the rest of the pass (a breach that starts just after its own account was
# read waits for every account behind it, then for the next pass to come round), the time the node
# takes to answer, and how many reads were refused. The shared budget decides that last one, so
# there is no honest single number here -- only these two, each exact about what it measures.
DELAY_ADDED_BY_A_REFUSED_READ_S = c.RPC_BACKOFF_TOTAL_S  # 63

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
# The perp dexes the keeper reads besides Hyperliquid's own (HIP-3), from --perp-dexes; and each one's
# index, from perpDexs, read once.
PERP_DEXES: tuple[str, ...] = ()
_dex_index: dict[str, int] = {}


def _on_dex(body: dict, dex: str) -> dict:
    return body if dex == MAIN_DEX else {**body, "dex": dex}


def account_dexes(allowed: set[int]) -> tuple[str, ...]:
    """The dexes to read for an account: Hyperliquid's own, the configured ones, and every dex its
    rules list a perp on (AUDIT В4-1/В4-3, 21 Oct 2026). From the rules, so a dex left out of
    COLOSSEUM_PERP_DEXES can't hide an order the stop and the settle steps must cancel: a resting
    order on a dex holds margin there, and with the trader's key cut nobody else can cancel it."""
    wanted = [MAIN_DEX, *PERP_DEXES]
    indexes = sorted({dex_index_of(a) for a in allowed} - {0})
    if indexes:
        if not all(i in _dex_index.values() for i in indexes):
            _dex_index.update(parse_perp_dexs(c.info_post({"type": "perpDexs"})))
        by_index = {i: n for n, i in _dex_index.items()}
        for i in indexes:
            if i not in by_index:
                raise RuntimeError(f"Hyperliquid lists no perp dex {i}, which this account's rules name")
            wanted.append(by_index[i])
    return tuple(dict.fromkeys(wanted))


_names_by_dex: dict[str, dict[str, int]] = {}


def names_on(dexes, coins=()) -> dict[str, int]:
    """The perps of dexes the pass's names don't cover (a dex from an account's rules only). An index
    never changes its name, so a dex's names are kept; they are read again when the account holds or
    rests an order on a coin of that dex the kept names lack -- a perp listed since (CodeRabbit,
    cp#162: otherwise that order would never be on a cancel list)."""
    out: dict[str, int] = {}
    for dex in dexes:
        if dex in (MAIN_DEX, *PERP_DEXES):
            continue
        held = {coin for coin in coins if coin.startswith(dex + ":")}
        if dex not in _names_by_dex or not held <= _names_by_dex[dex].keys():
            index = dex_index(dex)
            _names_by_dex[dex] = {a["name"]: order_asset(index, i)
                                  for i, a in enumerate(c.info_post(_on_dex({"type": "meta"}, dex))["universe"])}
        out.update(_names_by_dex[dex])
    return out


def dex_index(dex: str) -> int:
    if dex == MAIN_DEX:
        return 0
    if dex not in _dex_index:
        _dex_index.update(parse_perp_dexs(c.info_post({"type": "perpDexs"})))
    if dex not in _dex_index:
        raise RuntimeError(f"Hyperliquid lists no perp dex named {dex!r}")
    return _dex_index[dex]


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
        # Every dex's perps by the number an order (and the contract's cancel) uses for them: the
        # plain index on Hyperliquid's own dex, 100000 + dex * 10000 + index on another.
        names = {}
        for dex in (MAIN_DEX, *PERP_DEXES):
            index = dex_index(dex)
            for i, a in enumerate(c.info_post(_on_dex({"type": "meta"}, dex))["universe"]):
                names[a["name"]] = order_asset(index, i)
        _perp_names = names
    except Exception as exc:
        log("meta_read_failed", error=str(exc)[:200], names=len(_perp_names))
    return _perp_names


def read_book(account: str, allowed: set[int] | None = None) -> tuple[dict, list]:
    """An account's positions and open orders, one read of each, for everything a pass decides about it.

    Hyperliquid weighs clearinghouseState 2 and frontendOpenOrders 20, of 1,200 a minute per IP that
    the gateway on the same host shares. The stop check once read both again, which made an active
    account cost 44 a pass instead of 22. frontendOpenOrders lists the same orders as openOrders, in
    the same order, trigger orders included (spike/README.md, question 17), and says which is a stop.

    On every dex the keeper reads (HIP-3): two reads a dex, the positions side by side and the orders
    side by side, in the same shape as one dex's. A coin's name says its dex ("xyz:XYZ100")."""
    states, orders = [], []
    for dex in account_dexes(rules_assets(account) if allowed is None else allowed):
        state = c.info_post(_on_dex({"type": "clearinghouseState", "user": account}, dex))
        dex_orders = c.info_post(_on_dex({"type": "frontendOpenOrders", "user": account}, dex))
        # An answer of the wrong shape is a read that failed, not a dex with nothing on it: taken as
        # empty, it would let the stop check pass the account on the other dexes alone and count the
        # venue as read (CodeRabbit, cp#162). Raised, it is venue_book's None, and the pass stays open.
        if not (isinstance(state, dict) and isinstance(state.get("assetPositions"), list)
                and isinstance(dex_orders, list)):
            raise ValueError(f"dex {dex or 'main'}: clearinghouseState or frontendOpenOrders not of the expected shape")
        states.append(state)
        orders.extend(dex_orders)
    state = {**states[0], "assetPositions": [p for s in states for p in s["assetPositions"]]}
    return state, orders


def stop_inputs(account: str, allowed: set[int], names: dict[str, int],
                book: tuple[dict, list] | None = None, strict: bool = False) -> tuple[list, list]:
    """The cancel list and the forbidden assets, by order number. With `strict`, names of a dex that
    can't be read are an error (the forbidden-asset check is then not done, and the pass stays open);
    without it, the names already kept are used and only a perp never read costs its entries this
    time, so a stop the contract has already found still goes (CodeRabbit, cp#162) and a later settle
    step cancels what is left."""
    state, orders = book or read_book(account, allowed)
    coins = {o["coin"] for o in orders} | {p["position"]["coin"] for p in state.get("assetPositions", [])}
    try:
        names = {**names, **names_on(account_dexes(allowed), coins)}
    except Exception as exc:
        if strict:
            raise
        log("meta_read_failed", account=account, error=str(exc)[:200])
        # A refresh that failed for a perp listed since must not drop the orders of the perps already
        # named: their cancels go with the stop (CodeRabbit, cp#162).
        names = {**names, **{name: asset for kept in _names_by_dex.values() for name, asset in kept.items()}}
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
    precompile over EVM (`RuledAccount.violation`), a path apart from Hyperliquid's info API. The EVM
    node is not the keeper's own: from 18 October it is the public `rpc.hyperliquid-testnet.xyz`, and
    its per-IP budget is shared with the gateway on the same host (audit M-1: accepted on testnet,
    to be separated before mainnet). Before A-14 this read came first and threw, so an account went
    entirely unchecked exactly when the shared budget was tight: the one moment the check is worth
    having.
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


def send(wallet, addr: str, sig: str, types=(), args=(), dry=False, gas: int | None = None) -> None:
    if dry:
        log("would_send", to=addr, call=sig)
        return
    try:
        rcpt = c.transact(wallet, addr, sig, list(types), list(args), **({"gas": gas} if gas is not None else {}))
        log("sent", to=addr, call=sig, tx=rcpt["transactionHash"])
    except Exception as exc:  # one failed call must not stop the pass
        # A start on a builder dex refuses NotOnDex until a later block shows its capital, and a
        # minute on sends it again: that is waiting, not a failed send (audit window 2, В5-3).
        if refusal(exc) == "NotOnDex()":
            log("waiting_for_dex", to=addr, call=sig)
            return
        # A keeper that finds a breach and cannot send it is the one failure that matters, and
        # "send_failed" alone read the same whether the node was busy for a second or the wallet
        # had been empty for days. The balance goes in the line so the two can be told apart --
        # as a fact, not as a verdict on this send.
        gas = gas_balance(wallet)
        log("send_failed", to=addr, call=sig, error=str(exc)[:200], gas_wei=gas,
            unfunded=None if gas is None else unfunded(gas))


# An agent approved with no name lived fourteen days and was gone (a live challenge's, 18 October
# 2026). The contracts approve every agent as `pools` for RuledAccount.AGENT_TERM, and HyperCore shows
# the term (extraAgents). In the last RENEW_WINDOW of it the keeper renews the agent, which gives the
# trader a fresh key (`renewAgent`). These are alarms, never a second approval of a key (the CTO,
# 19 October 2026):
# - a key HyperCore no longer lists before its term ends;
# - any other address it lists for the account;
# - a term that ended unrenewed, which it then renews (audit window 2, A5-2).
# A test holds the contract's two constants against its source.
AGENT_TERM = 179 * 86_400
RENEW_WINDOW = 14 * 86_400
# HyperCore lists an approval a little after the block that sent it: no alarm in the first hour.
AGENT_SETTLE = 3_600


def listed_agents(account: str) -> list[dict] | None:
    """The named agents HyperCore holds for `account` (extraAgents), None if it would not say."""
    try:
        return c.info_post({"type": "extraAgents", "user": account}) or []
    except Exception as exc:
        log("agents_unread", account=account, error=str(exc)[:200])
        return None


def renew_if_due(wallet, account: str, now: int, dry: bool) -> None:
    """Renews the account's agent from the last RENEW_WINDOW of its term on: the term HyperCore shows
    for the key, the one the account asked for when HyperCore would not say; once the term the account
    asked for has ended, whatever HyperCore shows. Raises the alarms above. An
    account deployed before agents were named has no `agentValidUntil`, and is passed over."""
    try:
        asked = view(account, "agentValidUntil()", "uint64")
    except Exception:
        return
    ends = asked // 1000
    key = to_checksum_address(view(account, "agentKey()", "address"))
    term = ends
    listed = listed_agents(account)
    if listed is not None:
        mine = [a for a in listed if str(a.get("address", "")).lower() == key.lower()]
        others = [str(a.get("address", "")) for a in listed if str(a.get("address", "")).lower() != key.lower()]
        if others:
            log("agent_unexpected", account=account, key=key, listed=others)
        if mine:
            term = int(mine[0].get("validUntil") or 0) // 1000
            if term != ends:
                log("agent_term_differs", account=account, key=key, shown_until=term * 1000, asked_until=asked)
        elif ends - AGENT_TERM + AGENT_SETTLE <= now < ends:
            log("agent_missing", account=account, key=key, asked_until=asked)
    if now >= ends:
        log("agent_term_over", account=account, key=key, asked_until=asked)
    if ends - RENEW_WINDOW <= now and (term - RENEW_WINDOW <= now or now >= ends):
        log("renew_agent", account=account, key=key, asked_until=asked)
        send(wallet, account, "renewAgent()", dry=dry)


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


def check_protection(account: str, book: tuple[dict, list] | None = None, now: float | None = None) -> None:
    """Logs each open position that has no stop on the exchange, and each HIP-3 position still open
    when the pool's default says it should be flat over the weekend. A failure is logged too and
    never stops the pass: the rules are still the contract's to enforce. The keeper holds no key and
    the contracts close a position only at a breach or a settle, so it can only say so."""
    try:
        for gap in unprotected(account, book):
            log("protection_missing", account=account, **gap)
        if now is not None and weekend_flat_due(now):
            state, _ = book or read_book(account)
            for entry in state.get("assetPositions", []):
                p = entry["position"]
                if ":" in p["coin"] and Decimal(p.get("szi") or 0) != 0:
                    log("hip3_open_over_weekend", account=account, coin=p["coin"], size=p["szi"])
    except Exception as exc:
        log("protection_check_failed", account=account, error=str(exc)[:200])


def in_checkpoint_window(now: int) -> bool:
    return now % 86400 < CHECKPOINT_WINDOW


def rules_assets(addr: str) -> set[int]:
    # The first four fields: a v3 account appends each asset's cap (assetLeverageX100), and a
    # reader of the four reads an older deployment's rules and a v3 one's alike.
    rules = c.call_view(addr, "rules()", [], [], ["(uint16,uint16,uint32,uint32[])"])[0]
    return set(rules[3])


def recut_if_uncut(wallet, account: str, latest: int | None, dry: bool) -> None:
    if latest is None:
        return  # whether a cut has had its blocks is a question about height, and we have none
    key = to_checksum_address(view(account, "cutKey()", "address"))
    if key == ZERO or latest < view(account, "cutBlock()", "uint64") + RECUT_AFTER_BLOCKS:
        return
    role = c.info_post({"type": "userRole", "user": key})
    agent_of = str((role.get("data") or {}).get("user", "")).lower()
    if role.get("role") == "agent" and agent_of == account.lower():
        log("cut_did_not_land", account=account, key=key)
        send(wallet, account, "recut(bytes32)", ["bytes32"], [os.urandom(32)], dry=dry)


# A small HyperEVM block holds 2,000,000 gas, and the worst stop needs about 3.4 million: sixteen
# assets in the rules, sixteen extra, thirty-two cancels (audit window 2, В6-3; spike/run.py stopgas,
# 22 October 2026). So a stop or a settlement step names as many cancels and extra positions as fit,
# and the next settlement step names the rest (the CTO, 22 October 2026). The contract closes the
# rules' own positions whatever is named: at worst 1.43 million, which fits. Big blocks (30 million,
# once a minute) are the fallback docs/DESIGN.md describes, not switched on.
SMALL_BLOCK_GAS = 2_000_000
# What such a call may be estimated at: a tenth under the block, for the estimate's error.
FIT_GAS = 1_800_000


def estimate(wallet, to: str, sig: str, types, args) -> int | None:
    """The node's gas estimate for the call from the keeper's wallet; None if it would revert or the
    node will not say."""
    try:
        data = c.encode_call(sig, list(types), list(args))
        return int(c.rpc("eth_estimateGas", [{"from": wallet.address, "to": to, "data": "0x" + data.hex()}]), 16)
    except Exception:
        return None


def fit(gas_of, cancels: list, extra: list) -> tuple[list, list, int | None, bool]:
    """The most of `extra`, then of `cancels`, with which a call that `gas_of(cancels, extra)` estimates
    stays at FIT_GAS or under, and that estimate. The extra positions go first: they are exposure now,
    where a resting order is only the chance of one. The lists come back whole when the whole call
    fits, or when neither it nor the smallest call can be estimated (then the send says why). False
    when even the smallest call does not fit."""
    whole = gas_of(cancels, extra)
    if whole is not None and whole <= FIT_GAS:
        return cancels, extra, whole, True
    if whole is None and smallest(gas_of, extra) is None:
        return cancels, extra, None, True
    # A whole call the node will not estimate while the smallest one it does is cut like one over the
    # block, an estimate that does not come counting as over (the CTO, 23 October 2026).

    def most(top: int, gas_at) -> int:
        """The largest n from 0 to top whose call fits, -1 if none does."""
        lo, hi, best = 0, top, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            gas = gas_at(mid)
            if gas is not None and gas <= FIT_GAS:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    k = most(len(extra), lambda n: gas_of([], extra[:n]))
    if k < 0:
        return [], [], None, False
    j = max(most(len(cancels), lambda n: gas_of(cancels[:n], extra[:k])), 0)
    return cancels[:j], extra[:k], gas_of(cancels[:j], extra[:k]), True


def smallest(gas_of, extra: list) -> int | None:
    """The estimate of the smallest call worth sending: neither cancels nor extra, or, when that is
    refused, the first extra position alone. A stop for a forbidden asset only is refused (NoBreach)
    unless an extra position names one."""
    gas = gas_of([], [])
    if gas is None and extra:
        gas = gas_of([], extra[:1])
    return gas


def send_fitted(wallet, addr: str, sig: str, types, head: list, cancels, extra, tail: list, dry: bool) -> None:
    """Sends `sig(*head, cancels, extra, *tail)` with its cancels and extra cut to what a small block
    holds, its gas limit at most the block's."""
    if not cancels and not extra:  # nothing to cut
        return send(wallet, addr, sig, types, [*head, cancels, extra, *tail], dry=dry)

    def gas_of(cn, ex):
        return estimate(wallet, addr, sig, types, [*head, cn, ex, *tail])

    cn, ex, gas, fits = fit(gas_of, list(cancels), list(extra))
    call = sig.split("(")[0]
    if not fits:
        # Nothing to cut leaves it under a small block: an alarm, and no send, since a transaction no
        # small block takes would hold every later one of this wallet behind its nonce.
        log("call_over_small_block", to=addr, call=call, cancels=len(cancels), extra=len(extra))
        return
    if len(cn) < len(cancels) or len(ex) < len(extra):
        log("named_in_part", to=addr, call=call, cancels=f"{len(cn)}/{len(cancels)}", extra=f"{len(ex)}/{len(extra)}")
    send(wallet, addr, sig, types, [*head, cn, ex, *tail], dry=dry,
         gas=None if gas is None else min(gas * 5 // 4, SMALL_BLOCK_GAS))


def stop(wallet, addr: str, fn: str, cancels, extra, dry: bool) -> None:
    send_fitted(wallet, addr, f"{fn}({CANCEL},uint32[],bytes32)", [CANCEL, "uint32[]", "bytes32"],
                [], cancels, extra, [os.urandom(32)], dry)


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
    try:
        _, extra = stop_inputs(account, rules_assets(account), names, book, strict=True)
    except Exception as exc:
        # The names of a dex this account's rules use could not be read: the forbidden-asset rule is
        # not answered, so the venue counts as unread and the round comes back to this account.
        log("venue_read_failed", account=account, error=str(exc)[:200])
        return 0, None
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
        elif view(ch, "capitalArrived()", "bool") or view(ch, "sentToDexAt()", "uint64"):
            # On a builder dex the first activate() sends the capital there and a later one starts
            # the challenge once the dex shows it, or sends it again a minute on (audit window 2,
            # В4-2). Either way spot no longer has it, and that is not a reason to abort.
            send(wallet, ch, "activate()", dry=dry)
        elif now > view(ch, "createdAt()", "uint64") + START_WINDOW:
            send(wallet, ch, "abort()", dry=dry)
        return True
    if status != ACTIVE and status not in STOPPED:
        return True
    if status == ACTIVE:
        # A breach before the day's snapshot: the contract refuses the snapshot while a rule is
        # broken, so that midnight forgives nothing (deep audit, F12).
        reason, book = whats_broken(ch, names)
        if not reason and in_checkpoint_window(now) and view(ch, "day()", "uint32") < now // 86400:
            send(wallet, ch, "checkpoint()", dry=dry)
        if reason:
            log("breach_found", account=ch, reason=reason)
            cancels, extra = stop_args(ch, names, book)
            stop(wallet, ch, "breach", cancels, extra, dry)
        elif now > view(ch, "deadline()", "uint64"):
            cancels, extra = stop_args(ch, names, book)
            stop(wallet, ch, "expire", cancels, extra, dry)
        else:
            renew_if_due(wallet, ch, now, dry)
            if book is not None:
                check_protection(ch, book, now)
        return book is not None
    else:
        recut_if_uncut(wallet, ch, latest, dry)
        # Settling keeps the old behaviour: a refused read raises, the pass logs the pool and
        # comes back to it. Nothing is at stake in the meantime -- the account is already stopped
        # -- so there is no reason to spend a transaction on a settle step with no cancels in it.
        cancels, extra = stop_inputs(ch, rules_assets(ch), names)
        send_fitted(wallet, ch, f"settle({CANCEL},uint32[])", [CANCEL, "uint32[]"], [], cancels, extra, [], dry)
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
            reason, book = whats_broken(pool, names)
            if not reason and in_checkpoint_window(now) and view(pool, "day()", "uint32") < now // 86400:
                send(wallet, pool, "checkpoint()", dry=dry)
            if reason:
                log("breach_found", account=pool, reason=reason)
                cancels, extra = stop_args(pool, names, book)
                stop(wallet, pool, "breach", cancels, extra, dry)
            else:
                renew_if_due(wallet, pool, now, dry)
                if book is not None:
                    check_protection(pool, book, now)
            checked = checked and book is not None
        else:
            recut_if_uncut(wallet, pool, latest, dry)
            cancels, extra = stop_inputs(pool, rules_assets(pool), names)
            send_fitted(wallet, pool, f"settleFunded({CANCEL},uint32[])", [CANCEL, "uint32[]"], [], cancels, extra, [], dry)
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


def factory_pools(factory: str) -> list[str]:
    """Every pool the factory has made, in the order it made them."""
    return [to_checksum_address(p) for p in view(factory, "pools()", "address[]")]


def challenged_count(factory: str) -> int | None:
    """How many pools have sold a challenge, on the factory's own list of them; None from a factory
    deployed before it kept one (audit, K-1). A read that fails for any other reason is raised."""
    try:
        return view(factory, "challengedCount()", "uint256")
    except Exception as exc:
        if "revert" in str(exc).lower():
            return None
        raise


def challenged_page(factory: str, start: int, n: int) -> list[str]:
    """Up to `n` of the pools that have sold a challenge, from the `start`-th, one call."""
    return [to_checksum_address(p) for p in
            view(factory, "challengedFrom(uint256,uint256)", "address[]", ["uint256", "uint256"], [start, n])]


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
                 window: int = MAX_LOG_WINDOW, max_windows: int = 50, sweep_every: int = SWEEP_EVERY):
        if not 1 <= window <= MAX_LOG_WINDOW:
            raise SystemExit(f"--log-window must be 1..{MAX_LOG_WINDOW}: HyperEVM refuses wider eth_getLogs ranges")
        if max_windows < 1:
            raise SystemExit("--max-windows must be at least 1: a pass that reads no logs never finds a challenge")
        if sweep_every < 1:
            raise SystemExit("--sweep-every must be at least 1: a keeper that never reads the pool list "
                             "loses every pool its scan cannot reach")
        self.sweep_every = sweep_every
        # In memory only: a restart sweeps on its first pass, and from the top of the list.
        self.passes = 0
        self.sweep_at = 0
        self.stuck_passes = 0
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

    def sweep(self) -> None:
        """Follows every pool on the factory's list that is not idle or has a challenge, asking
        about SWEEP_MAX unfollowed ones a sweep at most. A refused read ends the sweep, and the next
        one begins after the pool that refused: a pool whose reads always fail would otherwise
        hold the place for good, and nothing behind it would ever be asked about.

        The place is an index into the factory's whole list, not into the unfollowed part of it.
        The factory only ever appends, so an index there names the same pool from one sweep to the
        next; an index into the unfollowed part shifts whenever a sweep follows a pool before it,
        and the next sweep would skip the pool that slid into its place."""
        try:
            total = challenged_count(self.factory)
        except Exception as exc:
            log("sweep_failed", error=str(exc)[:200])
            return
        if total is not None:
            self.sweep_challenged(total)
            return
        try:
            pools = factory_pools(self.factory)
        except Exception as exc:
            log("sweep_failed", error=str(exc)[:200])
            return
        if not pools:
            return
        start = self.sweep_at % len(pools)
        asked = 0
        for n in range(len(pools)):
            i = (start + n) % len(pools)
            pool = pools[i]
            if pool in self.live:
                continue
            if asked == SWEEP_MAX:
                self.sweep_at = i
                return
            asked += 1
            try:
                stage = view(pool, "stage()", "uint8")
                challenge = to_checksum_address(view(pool, "challenge()", "address"))
            except Exception as exc:
                log("sweep_failed", pool=pool, error=str(exc)[:200])
                self.sweep_at = i + 1
                return
            if stage != IDLE or challenge != ZERO:
                log("found_by_sweep", pool=pool, stage=stage, challenge=challenge)
                self.live.add(pool)
        self.sweep_at = start

    def sweep_challenged(self, total: int) -> None:
        """The sweep of a factory that keeps its pools with a challenge apart (audit, K-1): only those,
        SWEEP_MAX addresses a call, and never a pool that has sold none, however many there are. The
        place is an index into that list, which the factory only appends to."""
        if total == 0:
            return
        i = self.sweep_at % total
        asked = seen = 0
        while seen < total:
            try:
                page = challenged_page(self.factory, i, min(SWEEP_MAX, total - i))
            except Exception as exc:
                log("sweep_failed", error=str(exc)[:200])
                self.sweep_at = i
                return
            if not page:
                break
            for pool in page:
                if pool not in self.live:
                    if asked == SWEEP_MAX:
                        self.sweep_at = i
                        return
                    asked += 1
                    try:
                        stage = view(pool, "stage()", "uint8")
                        challenge = to_checksum_address(view(pool, "challenge()", "address"))
                    except Exception as exc:
                        log("sweep_failed", pool=pool, error=str(exc)[:200])
                        self.sweep_at = (i + 1) % total
                        return
                    if stage != IDLE or challenge != ZERO:
                        log("found_by_sweep", pool=pool, stage=stage, challenge=challenge)
                        self.live.add(pool)
                i = (i + 1) % total
                seen += 1
                if seen == total:
                    break
        self.sweep_at = i

    def one_pass(self) -> None:
        try:
            latest = int(c.rpc("eth_blockNumber"), 16)
        except Exception as exc:
            # This read sits outside the per-pool guard, so its refusal used to end the pass
            # before a single account was looked at -- and a pass that ends before it starts
            # never moves the round on, which is how one rate-limited minute became a tail of
            # pools nobody checked. Measured on the host on 1 October 2026: a keeper pass died
            # on `eth_blockNumber: rate limited 6 times in a row`.
            #
            # Without the head there is no scan (a window needs an end) and no recut (a question
            # about height). Every rule the contract holds is still answered: those come off the
            # margin precompile, and an eth_call needs no block number.
            log("head_read_failed", error=str(exc)[:200])
            latest = None
        scan_from = self.next_block
        end = None if latest is None else min(latest, self.next_block + self.window * self.max_windows - 1)
        # Window by window, and the place moves after each window that came back. A refused read
        # ends the scan for this pass and keeps what it read: a pass that threw its whole scan away
        # on the last window started again from the same block every time, so after a gap -- a
        # deployment older than the keeper, or any restart behind the head -- it never caught up.
        # Measured on the demo, 23 Sep 2026: 50 windows a pass against a node that allows 100 calls
        # a minute, every pass refused part way, `next_block` at the deployment block all along.
        while end is not None and self.next_block <= end:
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
        # A scan that cannot move is not watching: a challenge bought after its block goes unseen
        # until the sweep below finds the pool. Counted only while there is a head to reach, and a
        # pass that could not read the head leaves the count alone: it says nothing about whether
        # the scan moved, and on a node that refuses now and then a reset there would keep
        # `scan_behind` from ever being said (CodeRabbit, cp#155).
        if latest is None:
            pass
        elif self.next_block == scan_from and self.next_block <= latest:
            self.stuck_passes += 1
            if self.stuck_passes % SCAN_STUCK_PASSES == 0:
                log("scan_behind", next_block=self.next_block, latest=latest,
                    behind=latest - self.next_block + 1, passes=self.stuck_passes)
        else:
            self.stuck_passes = 0
        if self.passes % self.sweep_every == 0:
            self.sweep()
            self.save()
        self.passes += 1
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


# Where the host keeps the gateway's settings (ops/host/bootstrap.sh).
GATEWAY_ENV = pathlib.Path("/etc/colosseum/gateway.env")
MAINNET_CHAIN_ID = 999


def node_shared_with_gateway(own: str, gateway_env: pathlib.Path) -> bool:
    """Whether the keeper reads the gateway's node, by gateway.env's GATEWAY_RPC_URL. A node's
    budget is per IP: shared, a gateway busy with orders leaves the keeper's stops and snapshots
    waiting, and the other way round (audit, M-1)."""
    try:
        text = gateway_env.read_text()
    except OSError:
        return False
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "GATEWAY_RPC_URL":
            return value.strip().rstrip("/") == own.strip().rstrip("/")
    return False


def check_node(own: str, chain_id: int, gateway_env: pathlib.Path = GATEWAY_ENV) -> None:
    """On testnet a node shared with the gateway is said at the start (accepted there, the CTO,
    20 October 2026); on mainnet the keeper does not start on it."""
    if not node_shared_with_gateway(own, gateway_env):
        return
    if chain_id == MAINNET_CHAIN_ID:
        raise SystemExit("the keeper's node is the gateway's: on mainnet the keeper needs a node of its own "
                         "(audit M-1); set COLOSSEUM_RPC_URL in keeper.env to another")
    log("node_shared_with_gateway", rpc=own.split("?")[0], why="one IP budget for orders and stops (audit M-1)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/")
    p.add_argument("--wallet", default="keeper")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=DEFAULT_EVERY_S, help="seconds between passes")
    p.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    p.add_argument("--state", help="state file (default ops/state/keeper-<deployment>.json)")
    p.add_argument("--log-window", type=int, default=MAX_LOG_WINDOW,
                   help=f"blocks per eth_getLogs call, 1..{MAX_LOG_WINDOW}")
    p.add_argument("--max-windows", type=int, default=50, help="eth_getLogs calls per pass, at most")
    # From keeper.env by default, like the gateway's GATEWAY_PERP_DEXES: a dex goes on and off with the
    # platform's list, and that is a setting and a restart, not a release (CTO, 21 Oct 2026).
    p.add_argument("--perp-dexes", default=os.environ.get("COLOSSEUM_PERP_DEXES", ""),
                   help="the perp dexes read besides Hyperliquid's own (HIP-3), comma-separated, e.g. xyz")
    p.add_argument("--sweep-every", type=int, default=SWEEP_EVERY,
                   help="passes between reads of the factory's pool list (and on the first pass)")
    args = p.parse_args()

    global PERP_DEXES
    PERP_DEXES = tuple(dict.fromkeys(d.strip() for d in args.perp_dexes.split(",") if d.strip()))
    c.assert_testnet()
    check_node(c.RPC_URL, c.CHAIN_ID)
    record = deployments.load(args.deployment)
    state = pathlib.Path(args.state) if args.state else STATE_DIR / f"keeper-{args.deployment}.json"
    wallet = None if args.dry_run else c.account(args.wallet)
    keeper = Keeper(record["PoolFactory"], wallet, args.dry_run, state, deployment_block(record),
                    window=args.log_window, max_windows=args.max_windows, sweep_every=args.sweep_every)
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
