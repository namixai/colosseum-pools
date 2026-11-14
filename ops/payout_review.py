"""The review of a simulated pool's payout before anyone signs it through or stops it (docs/SIM-POOL.md).

    spike/.venv/bin/python -m ops.payout_review --deployment sim1                  # every payout still waiting
    spike/.venv/bin/python -m ops.payout_review --deployment sim1 --pool P --id 3  # one of them
    spike/.venv/bin/python -m ops.payout_review --deployment sim1 --json           # the same, as JSON
    spike/.venv/bin/python -m ops.payout_review --deployment sim1 --out reviews/   # one JSON file a payout

It reads the chain and nothing else, sends nothing and holds no key. For each payout claimed
(`PayoutDue`) and still waiting it says:

  who and what     → the trader, the stage, the exam it came from, the amount, when it is due;
  pairs            → every fill of the stage and of the exam against every fill of every other account
                     of the factory's pools: the same asset, the other side, close in time and size
                     (`ops/sim_check.py`'s rule, the same parameters), with both fills' transactions;
  linked wallets   → senders of USDC to this trader who sent USDC to another buyer of the factory too,
                     and USDC sent between the two. The pools themselves pay every trader and are not
                     counted; a sender that funded more than --wide buyers is named but not counted,
                     as a wallet an exchange pays out of funds thousands of strangers;
  late fills       → how many blocks late the filler put this trader's fills (`late` of `Filled`)
                     against everyone else's;
  the group's code → whose code the exam was bought under, its sales, from how many wallets, the
                     longest run of sales in a row under it each from a new wallet, and how many of its
                     buyers never traded or stand in a pair;
  what is running  → exams and stages still able to pass, the payouts they would be if all passed, and
                     whether the pool's free money covers them;
  the verdict      → "clean", or "suspicious" with its reasons, and the call to sign, as text only: a
                     stop (`stopPayout`, reason 1 or 2, only with a pair) or the approval
                     (`approvePayouts`). A stop of reason 3, a result disputed without a pair, needs a
                     written analysis this script does not make.

What it does not see, by its nature: a pair on another factory or another venue, accounts the
trader holds under wallets nothing links, and USDC that came from before the deployment's block.
Testnet only.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from ops import sim_check as chk
from ops import sim_keeper as sk
from spike.hlspike import common as c

PAYOUT_DUE = "0x" + keccak(text="PayoutDue(uint256,address,address,uint64,uint64)").hex()
# What a payout's life is written as (SimPool): each event's first topic is the payout's id. The tests hold each
# signature to the source.
PAYOUT_APPROVED = "0x" + keccak(text="PayoutApproved(uint256,address)").hex()
PAYOUT_PAID = "0x" + keccak(text="PayoutPaid(uint256,address,uint64,uint256,uint256)").hex()
PAYOUT_STOPPED = "0x" + keccak(text="PayoutStopped(uint256,address,uint64,uint8,address,bytes32[],bytes32[],uint256,uint256,uint256)").hex()
PAYOUT_HELD = "0x" + keccak(text="PayoutHeld(uint256,address,uint64)").hex()
STOP_LIFTED = "0x" + keccak(text="StopLifted(uint256,uint256,uint256,uint256)").hex()
STOP_FINAL = "0x" + keccak(text="StopFinal(uint256,uint64,uint256)").hex()
# `pay` writes this when a payout may be paid and the pool's daily limit has no room: it keeps its place, from `nextAt` on.
PAYOUT_WAITS_LIMIT = "0x" + keccak(text="PayoutWaitsLimit(uint256,uint64)").hex()
PAYOUT_TRAIL = {PAYOUT_DUE: "PayoutDue", PAYOUT_APPROVED: "PayoutApproved", PAYOUT_STOPPED: "PayoutStopped",
                STOP_LIFTED: "StopLifted", STOP_FINAL: "StopFinal", PAYOUT_PAID: "PayoutPaid", PAYOUT_HELD: "PayoutHeld"}
TRANSFER = chk.TRANSFER
PAYOUT_BPS = 800
FLOOR_PAYOUTS = 3  # SimPoolFactory.FLOOR_PAYOUTS
BPS = 10_000
# The pool's call that lets payouts through once their 72 hours are over, from its approver (SimPool,
# `approvePayouts`); a test holds the name, the arguments and the rules around it to the source.
APPROVE: str | None = "approvePayouts(uint256[])"
APPROVE_TYPES: list[str] = ["uint256[]"]
PAYOUT_DELAY = 72 * 3600
# Past this from its claim a payout is paid unapproved, unless it was stopped (SimPoolFactory, `PAYOUT_HARD`).
PAYOUT_HARD = 7 * 24 * 3600
PAYOUT_BEFORE_APPROVALS = chk.PAYOUT_BEFORE_APPROVALS
ZERO_ADDRESS = "0x" + "00" * 20
NO_APPROVER = ("the pool names no approver: the payout goes out unapproved from its ceiling on, unless it is stopped; "
               "there is nobody to sign an approval")
APPROVED = "approved already: it is paid from its due time, unless it is stopped; nothing more to sign"


# ── what the numbers say, from what was read ─────────────────────────────────────────

def quantile(values: list[int], q: float) -> int:
    """The value at or below which a share `q` of `values` lie (nearest rank); 0 for none."""
    if not values:
        return 0
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def late_outlier(mine: list[int], others: list[int], min_fills: int) -> bool:
    """Whether this trader's fills came systematically later than everyone else's: enough of each, and
    this trader's median above the others' 90th percentile."""
    if len(mine) < min_fills or len(others) < min_fills:
        return False
    return quantile(mine, 0.5) > quantile(others, 0.9)


def longest_new_wallet_run(sales: list[dict], code: str) -> int:
    """The longest run of the pool's sales in a row that all name `code`, each from a wallet not yet in
    the run: what a code's owner buying exams under many wallets at once leaves behind."""
    best = start = i = 0
    last: dict[str, int] = {}  # each wallet's latest place in the current stretch of this code's sales
    for s in sales:
        if s["code"] != code:
            start, i, last = 0, 0, {}
            continue
        t = s["trader"]
        if last.get(t, -1) >= start:
            start = last[t] + 1  # the run goes on from just after the wallet's earlier sale (review of #283)
        last[t] = i
        i += 1
        best = max(best, i - start)
    return best


def payout_of(size: int) -> int:
    return size * PAYOUT_BPS // BPS


def settle_order(pending: list[tuple[int, int]], slots: list[int], day: int, now: int) -> dict[int, int]:
    """When each waiting payout is settled, as `SimPool.pay` settles them, if a `pay` call is sent as soon as it can
    settle one: `pending` is (id, the time it is ready: its `dueAt` once approved, its ceiling if not) for every payout
    still due; `slots` are the times of the pool's last settlements, oldest first, one for each of the day's places
    (0: never used). A call looks at the payouts in the order of their ids, passes over one that is not ready, and stops
    at the first ready one the limit holds (it keeps its place, and so does every payout after it): a place is free when
    its settlement is a `day` old, and a settlement takes the oldest place. The pool's money is not counted (`PayoutQueued`
    is a different wait), nor a payout stopped in the meantime."""
    slots = list(slots)
    todo = sorted(pending)
    out: dict[int, int] = {}
    t = now
    while todo:
        ready = [(i, r) for i, r in todo if r <= t]
        if not ready:
            t = min(r for _, r in todo)
            continue
        i = ready[0][0]
        free = slots[0] + day if slots[0] else 0
        if free <= t:
            out[i] = t
            slots = slots[1:] + [t]
            todo = [x for x in todo if x[0] != i]
        else:
            # Held by the limit: nothing settles until the place frees (a payout getting ready meanwhile is held too, and
            # then goes in the order of its id).
            t = free
    return out


def payout_cap(chain: dict, pool: str, pay: dict, approvals: bool | None, delay: int | None, hard: int | None) -> dict | None:
    """Where the pool's daily limit on payouts leaves one payout, from what the contract answers (`PAYOUTS_PER_DAY()` and
    `PAYOUT_DAY()` of the factory, `payRoom()` and `nextPayAt()` of the pool) and the settlements it wrote (`PayoutPaid` and
    `PayoutHeld`, each a place used for a day), never a constant here: the limit is whatever was read. None when the chain
    was read without the cap's reads (an older chain: nothing is said); otherwise a document with a `status`:

      none         the factory has no daily limit (its `PAYOUTS_PER_DAY()` reverts): the payout is paid when it is due;
      unread       the limit, the pool's room, the payout's approval or its time was not read, or the events and the
                   pool's answers do not agree on the day's places: no time is said;
      not-waiting  the payout is paid, stopped, held or refused: no limit applies to it any more;
      free         ready and a place is free: the next `pay` settles it (the pool's money allowing);
      waits-limit  ready, and every place is used: it waits, in its place, for `settleAt`;
      not-ready    waiting for its approval or its 72 hours: `readyAt`, and the `settleAt` the limit allows after it.

    `settleAt` is when `settle_order` puts it, an estimate that stands while the payouts ahead of it stay as they are;
    `ahead` is how many payouts settle before it. `recorded` is the latest `PayoutWaitsLimit` written for it."""
    cap = (chain.get("payCap") or {}).get(pool, "absent")
    if cap == "absent":
        return None
    if cap is False:
        return {"status": "none", "limit": None, "dayS": None, "room": None, "nextAt": None, "readyAt": None,
                "settleAt": None, "ahead": None, "recorded": None, "problem": None}
    base = {"limit": cap["limit"], "dayS": cap["day"], "room": cap["room"], "nextAt": cap["nextAt"], "readyAt": None,
            "settleAt": None, "ahead": None, "recorded": (chain.get("waitsLimit") or {}).get((pool, pay["id"])), "problem": None}

    def unread(why: str) -> dict:
        return {**base, "status": "unread", "problem": why}
    if pay["state"] != "Due":
        return {**base, "status": "not-waiting"}
    if approvals is not True or delay is None or hard is None:
        return unread("whether the pool takes approvals, or the factory's delay and ceiling, was not read")
    now, limit, day = chain["now"], cap["limit"], cap["day"]
    done = (chain.get("settled") or {}).get(pool)
    if done is None:
        return unread("the pool's settlements were not read")
    slots = ([0] * limit + done)[-limit:]
    room = sum(1 for t in slots if t == 0 or now >= t + day)
    next_at = slots[0] + day if slots[0] else 0
    if (room, next_at) != (cap["room"], cap["nextAt"]):
        return unread(f"the settlements in the events give room {room} and next {next_at}, the pool says room "
                      f"{cap['room']} and next {cap['nextAt']}")
    pending = []
    for x in chain["payouts"]:
        if x["pool"] == pool and x["state"] == "Due":
            approved = x.get("approved", False)
            if approved is None:
                return unread(f"whether payout {x['id']} is approved was not read")
            pending.append((x["id"], x["dueAt"] if approved else x["dueAt"] - delay + hard))
    ready = next(r for i, r in pending if i == pay["id"])
    when = settle_order(pending, slots, day, now)
    mine = when[pay["id"]]
    ahead = sum(1 for i, t in when.items() if i != pay["id"] and (t, i) < (mine, pay["id"]))
    status = "not-ready" if ready > now else "waits-limit" if mine > now else "free"
    return {**base, "status": status, "readyAt": ready, "settleAt": mine, "ahead": ahead}


def payout_trail(names: list[str]) -> dict:
    """Where a payout stands, from its own recorded events in the order the chain wrote them, with no clock and no
    reading of the pool's storage: `PayoutDue` (the claim), `PayoutApproved`, `PayoutStopped` and `StopLifted`,
    `StopFinal`, `PayoutPaid` and `PayoutHeld`. It waits for approval while it is due (claimed, not stopped, not
    paid) and no `PayoutApproved` was written for it: an approval is kept through a stop and its lifting, as the
    contract keeps `approved`. `problems` are events the contract could not have written in that order (an approval
    of a payout not waiting or approved already, a stop of one stopped before, a payment of one that is not due,
    anything before the claim): a trail with problems says nothing."""
    state: str | None = None
    approved = was_stopped = False
    problems: list[str] = []
    for n in names:
        if n == "PayoutDue":
            if state is not None:
                problems.append("claimed twice")
            state = "Due"
        elif state is None:
            problems.append(f"{n} before the claim")
        elif n == "PayoutApproved":
            if state != "Due" or approved:
                problems.append("an approval of a payout not waiting, or approved already")
            approved = True
        elif n == "PayoutStopped":
            if state != "Due" or was_stopped:
                problems.append("a stop of a payout not waiting, or stopped before")
            state, was_stopped = "Stopped", True
        elif n in ("StopLifted", "StopFinal"):
            if state != "Stopped":
                problems.append(f"{n} of a payout not stopped")
            state = "Due" if n == "StopLifted" else "Refused"
        elif n in ("PayoutPaid", "PayoutHeld"):
            if state != "Due":
                problems.append(f"{n} of a payout not due")
            state = "Paid" if n == "PayoutPaid" else "Held"
    return {"state": state, "approved": approved, "wasStopped": was_stopped, "problems": problems,
            "waitingApproval": state == "Due" and not approved and not problems, "events": list(names)}


# ── mirrored accounts: positions against positions, not fills against fills ──────────

def mirror(mine: list[dict], theirs: list[dict], until: int, ratio: float, their_end: int | None = None) -> dict:
    """How much of the time this account held a position the other account held the opposite of.

    Each account's net position per asset is a step in time, changed by its fills. A moment counts as
    mirrored when, on some asset, the two positions have opposite signs and the smaller is at least
    `ratio` of the larger. `share` is the mirrored time over the time this account held anything (its
    first fill to `until`, counted only while it holds a position). No
    window between fills and no tolerance between sizes: a pair built in pieces, an hour apart, or at a
    fraction of the other's size is still the same two positions against each other.

    `examples` are fills that opened each mirrored stretch: this account's last fill that grew its
    position and the other's last fill that grew theirs, the same asset and opposite sides. `their_end`
    is when the other account ended (breached, expired or passed), if it did: its positions count no
    more from then."""
    out = {"share": 0.0, "mirrored": 0, "exposed": 0, "assets": [], "examples": []}
    if not mine:
        return out
    events = [(f["t"], 0, f) for f in mine] + [(f["t"], 1, f) for f in theirs]
    if their_end is not None:
        events.append((their_end, 2, None))
    events.sort(key=lambda x: (x[0], x[1]))
    pos = ({}, {})
    grew: tuple[dict, dict] = ({}, {})
    assets: dict[int, int] = {}
    examples: list[list[str]] = []
    i = 0
    while i < len(events):
        t = events[i][0]
        while i < len(events) and events[i][0] == t:
            _, side, f = events[i]
            i += 1
            if f is None:
                pos[1].clear()
                continue
            before = pos[side].get(f["asset"], 0)
            after = before + (f["sz"] if f["buy"] else -f["sz"])
            pos[side][f["asset"]] = after
            # A fill that opens, adds to, or turns a position over (a sell of 100 then a buy of 150 opens a long of 50)
            # is the fill that opened this stretch; one that only makes it smaller is not (review of #290).
            if abs(after) > abs(before) or before * after < 0:
                grew[side][f["asset"]] = f
        # To the next fill of either, or, after the last, to `until`: a position still open runs on.
        stop = events[i][0] if i < len(events) else max(until, t)
        span = stop - t
        if span <= 0:
            continue
        if any(v != 0 for v in pos[0].values()):
            out["exposed"] += span
            hit = False
            for a, x in pos[0].items():
                y = pos[1].get(a, 0)
                if x * y < 0 and min(abs(x), abs(y)) >= ratio * max(abs(x), abs(y)):
                    assets[a] = assets.get(a, 0) + span
                    hit = True
                    g, h = grew[0].get(a), grew[1].get(a)
                    if g and h and g["buy"] != h["buy"] and [g["tx"], h["tx"]] not in examples:
                        examples.append([g["tx"], h["tx"]])
            if hit:
                out["mirrored"] += span
    out["share"] = out["mirrored"] / out["exposed"] if out["exposed"] else 0.0
    out["assets"] = sorted(assets)
    out["examples"] = examples[:3]
    return out


def flat_at(fills: list[dict], end: int | None) -> list[dict]:
    """An account's fills, with what it held when it ended closed at that time: an exam that ended holds nothing."""
    if end is None:
        return fills
    pos: dict[int, int] = {}
    for f in fills:
        if f["t"] <= end:
            pos[f["asset"]] = pos.get(f["asset"], 0) + (f["sz"] if f["buy"] else -f["sz"])
    kept = [f for f in fills if f["t"] <= end]
    return kept + [{"account": fills[0]["account"] if fills else None, "asset": a, "buy": q < 0, "sz": abs(q), "t": end,
                    "tx": None} for a, q in sorted(pos.items()) if q]


def opposing_sum(account: str, chain: dict, who: dict[str, str], p: "Params") -> dict | None:
    """The opposite side spread over many small accounts: positions of every other account of the factory, summed,
    against this one's.

    A sum over everyone would catch a busy market, where some account is always on the other side by chance. So
    only accounts given to it count: an account that spent at least `p.sum_dedication` of its own time with a
    position against this one (any size), an hour at least. A trader in the market trades at other hours and
    other assets too, and is mostly not against this account; an account kept for the losing side is little else.
    A moment counts when those accounts' positions opposite this one's, on the same asset, add up to at least
    `p.mirror_ratio` of it. Found when that holds for `p.mirror_share` of this account's time, an hour at least,
    with at least two accounts in it. Each account's part is its average size against this one while it counted.
    Never a stop on its own: a sum is no pair the pool's rule names."""
    ended = chain.get("ended") or {}
    mine = [f for f in chain["fills"] if f["account"] == account]
    if not mine:
        return None
    others: dict[str, list[dict]] = {}
    for f in chain["fills"]:
        if f["account"] != account:
            others.setdefault(f["account"], []).append(f)
    given = []
    for other, fills in sorted(others.items()):
        theirs = flat_at(fills, ended.get(other))
        m = mirror(theirs, mine, chain["now"], 0.0, ended.get(account))
        if m["share"] >= p.sum_dedication and m["mirrored"] >= p.mirror_min_s:
            given.append((other, theirs))
    if len(given) < 2:
        return None
    events = [(f["t"], 0, account, f) for f in mine]
    for other, theirs in given:
        events += [(f["t"], 1, other, f) for f in theirs]
    events.sort(key=lambda x: (x[0], x[1]))
    pos: dict[str, dict[int, int]] = {}
    parts: dict[str, float] = {}
    exposed = mirrored = 0
    i = 0
    while i < len(events):
        t = events[i][0]
        while i < len(events) and events[i][0] == t:
            _, _, acc, f = events[i]
            book = pos.setdefault(acc, {})
            book[f["asset"]] = book.get(f["asset"], 0) + (f["sz"] if f["buy"] else -f["sz"])
            i += 1
        stop = events[i][0] if i < len(events) else max(chain["now"], t)
        span = stop - t
        mine_now = {a: q for a, q in pos.get(account, {}).items() if q}
        if span <= 0 or not mine_now:
            continue
        exposed += span
        for a, x in mine_now.items():
            against = {o: abs(pos[o].get(a, 0)) for o, _ in given if o in pos and pos[o].get(a, 0) * x < 0}
            if sum(against.values()) >= p.mirror_ratio * abs(x) and len(against) >= 2:
                mirrored += span
                for o, q in against.items():
                    parts[o] = parts.get(o, 0.0) + span * q / abs(x)
                break
    share = mirrored / exposed if exposed else 0.0
    if share < p.mirror_share or mirrored < p.mirror_min_s:
        return None
    accounts = [{"account": o, "trader": who.get(o), "part": round(parts[o] / mirrored, 4)}
                for o in sorted(parts, key=lambda o: (-parts[o], o))]
    traders = {who.get(o) for o in parts}
    senders: dict[str, set] = {}
    for t_ in chain["transfers"]:
        if t_["to"] in traders and t_["from"] not in chain["pools"]:
            senders.setdefault(t_["from"], set()).add(t_["to"])
    shared = sorted(s_ for s_, got in senders.items() if len(got) >= 2)
    return {"share": round(share, 4), "mirrored": mirrored, "exposed": exposed, "accounts": accounts,
            "sharedSenders": shared}


def mirrors_of(account: str, chain: dict, who: dict[str, str], p: "Params") -> list[dict]:
    """Every other account of the factory whose positions mirrored this one's for at least `p.mirror_share`
    of its time and `p.mirror_min_s` in all, the most mirrored first."""
    mine = [f for f in chain["fills"] if f["account"] == account]
    others: dict[str, list[dict]] = {}
    for f in chain["fills"]:
        if f["account"] != account:
            others.setdefault(f["account"], []).append(f)
    out = []
    for other, fills in sorted(others.items()):
        m = mirror(mine, fills, chain["now"], p.mirror_ratio, (chain.get("ended") or {}).get(other))
        if m["share"] >= p.mirror_share and m["mirrored"] >= p.mirror_min_s:
            out.append({"account": other, "trader": who.get(other), "share": round(m["share"], 4),
                        "mirrored": m["mirrored"], "exposed": m["exposed"], "assets": m["assets"],
                        "examples": m["examples"]})
    return sorted(out, key=lambda x: (-x["share"], x["account"]))


# ── the result counted again, outside the contract ───────────────────────────────────

PPM = 1_000_000
ANALYSIS = "<keccak256 of the published analysis>"
SOURCE_ORACLE, SOURCE_BOOK, SOURCE_FALLBACK = 0, 1, 2
# An account's `rules()` (src/Types.sol, `Rules`): the day's allowed loss in bps comes first, then the drawdown's.
EXAM_RULES = "(uint16,uint16,uint32,uint32[],uint32[])"
# How an exam's drawdown line is drawn (PaperExam.drawdownKind): from the starting balance, or trailing the highest
# day's snapshot of the step to the starting balance. `Breach.Drawdown` is 1 (src/Types.sol).
FROM_START, TRAILING_TO_START = 0, 1
DRAWDOWN_KINDS = ("from-start", "trailing-to-start")
BREACH_DRAWDOWN = 1


def drawdown_line_bps(kind: int, capital: int, max_drawdown_bps: int, peak: int) -> int:
    """The drawdown line times BPS, as `PaperExam._drawdownLineBps` keeps it, exact: the starting balance less
    `max_drawdown_bps` of it; for the trailing kind the highest day's snapshot of the step less that same amount,
    never above the starting balance."""
    base = capital * BPS - capital * max_drawdown_bps
    if kind == FROM_START:
        return base
    if kind != TRAILING_TO_START:
        raise ValueError(f"{kind} is not a drawdown kind")
    return min(peak * BPS - capital * max_drawdown_bps, capital * BPS)


def drawdown_line(kind: int, capital: int, max_drawdown_bps: int, peak: int) -> int:
    """`PaperExam.drawdownLine()`: the lowest equity that keeps the drawdown rule, rounded up as the contract rounds it
    (equity under it, as an integer, is `equity * BPS` under the exact line)."""
    n = drawdown_line_bps(kind, capital, max_drawdown_bps, peak) + BPS - 1
    return n // BPS if n >= 0 else -(-n // BPS)  # Solidity's division truncates toward zero


def oracle_key(asset: int, block: int) -> str:
    return f"{asset}:{block}"


def coin_flip(trips: list[dict], daily_bps: int | None, p: "Params") -> dict | None:
    """The shape of a pass that needs no view of the market: one position at a small leverage held to the target,
    its stop just over the day's loss line, and after a stop nothing until the next UTC day, when the line is
    counted from the smaller equity. The rules allow it and an honest trader may trade so; it is said as a
    profile, never as a reason.

    Seen from the fills alone, a step at a time: a few round trips (flat to flat), each opened at `flip_leverage`
    times the account at most; every one but the last closed with `flip_near` of the day's allowed loss gone or
    more, and no round trip opened again that day; the last one is the pass. One round trip alone is the same
    game won at once. Round trips under `flip_dust` of the account are not counted either way: they are what
    fills a trading day, not a position."""
    total = stops = 0
    leverage = 0.0
    for step in sorted({t["step"] for t in trips}):
        moves = [t for t in trips if t["step"] == step and t["open"] > 0 and t["ntl"] >= p.flip_dust * t["open"]]
        if not moves or len(moves) > p.flip_trips:
            return None
        if any(t["ntl"] > p.flip_leverage * t["open"] for t in moves):
            return None
        if len(moves) > 1 and not daily_bps:
            return None  # the day's line was not read: a loss cannot be held to it
        for t, nxt in zip(moves, moves[1:]):
            if (t["dayEq"] - t["close"]) * BPS < p.flip_near * t["dayEq"] * daily_bps:
                return None
            if nxt["t0"] // 86_400 <= t["t1"] // 86_400:
                return None
        total += len(moves)
        stops += len(moves) - 1
        leverage = max([leverage] + [t["ntl"] / t["open"] for t in moves])
    if not total:
        return None
    if stops:
        text = (f"passed in {total} round trips at {leverage:.2f}x at most, every one but the last of a step closed with "
                f"{round(p.flip_near * 100)} % or more of the day's allowed loss gone and none opened again that day: the "
                "shape of a coin flip with a stop over the daily line, which the rules allow; not a sign of cheating")
    else:
        text = (f"passed in one round trip a step at {leverage:.2f}x at most: the shape of a coin flip held to the "
                "target, which the rules allow; not a sign of cheating")
    return {"trips": total, "stops": stops, "leverage": round(leverage, 2), "text": text}


def trigger_fills(evs: list[dict]) -> dict[int, str]:
    """The fills among an account's events that a stop or a take made, by their place in the list: the index of a
    `Filled` to "stop" or "take". `fillTrigger` writes `Filled` and, right after it in the same transaction,
    `TriggerFilled` with the same size, price, cost and source (src/exam/PaperExam.sol); a `Filled` that is not
    followed by its own `TriggerFilled` is an order's. The close of a stop is a trade like any fill, so the fills
    list keeps it; it was not timed by the trader or the filler, so the timing tests leave it out."""
    out: dict[int, str] = {}
    for i, e in enumerate(evs[:-1]):
        n = evs[i + 1]
        if (e["name"] == "Filled" and n["name"] == "TriggerFilled" and n["tx"] == e["tx"] and n["asset"] == e["asset"]
                and (n["sz"], n["px"], n["cost"], n["source"]) == (e["sz"], e["px"], e["cost"], e["source"])):
            out[i] = "take" if n["take"] else "stop"
    return out


def trigger_hit(take: bool, position: int, oracle: int, px: int) -> bool:
    """Whether the oracle price has reached a stop or a take (`PaperExam._triggered`): a take on a long and a stop on
    a short at the price or above it, the other two at it or below."""
    return oracle >= px if take == (position > 0) else oracle <= px


def recount(account: str, chain: dict, p: "Params", size: int | None = None, largest: int | None = None) -> dict:
    """The exam's or stage's result counted again from its own events, as PaperExam counts it, and each fill
    against the chain's oracle price.

    Cash starts each step at the capital (`Started`); a fill takes `delta * px + cost` from it. A step passes
    only flat, so `StepPassed` and `Passed` must carry exactly that cash; a day's snapshot carries cash plus
    each open position at the oracle price of its block (checked where those prices were read). Each fill is
    held to the oracle price of its block: a fill at the oracle (`source` 0 or 2) at it exactly, a fill at
    the book never better for the trader. The oracle price of a fill's block is held to the blocks around it
    (one price far from both neighbours, which agree, is an outlier) and, given a file of the venue's prices,
    to the venue's price of that minute. A pass in very few fills or days on the largest size of the menu is
    said as a profile, not as a suspicion; so is a pass in the shape of a coin flip (`coin_flip`), as `flip`.

    The drawdown line moves with the step's highest day's snapshot in an exam of the trailing kind: each
    snapshot is held to the line as it stood before it (the contract takes none while a rule is broken), and an
    end by the drawdown to the line at that time. Where the account's kind and drawdown were not read
    (`chain["drawdownKind"]`, `chain["maxDrawdownBps"]`) nothing is said of the line.

    A stop or a take closes a position at the price a fill takes in the block it is carried out in, and that
    close is a fill like any: it counts in the cash and the position. Each `TriggerFilled` is held to the stop
    or take that stood (set, not yet taken off), to its block (not before `armedAt`), to the oracle having
    reached its price where that price was read, and to what it closes (the smaller position, never more than is
    open: the size named, or all). A stop or take left standing on a position closed or turned round, a trigger
    set on no position and a `TriggerFilled` with no `Filled` of its own are flags too."""
    ev = (chain.get("exam_events") or {}).get(account)
    out = {"read": ev is not None, "capital": None, "fills": 0, "days": 0, "passed": False,
           "checked": {"results": 0, "snapshots": 0, "unchecked": 0, "fills": 0}, "flags": [], "moves": [],
           "profile": None, "flip": None, "drawdown": None, "triggers": {"set": 0, "filled": 0}}
    if ev is None:
        return out
    oracle = chain.get("oracle") or {}
    venue = chain.get("venue") or {}
    cost_ppm = (chain.get("costPpm") or {}).get(account)
    kind = (chain.get("drawdownKind") or {}).get(account)
    dd_bps = (chain.get("maxDrawdownBps") or {}).get(account)
    line_read = kind is not None and dd_bps is not None
    flags = out["flags"]
    by_trigger = trigger_fills(ev)
    capital = cash = 0
    pos: dict[int, int] = {}
    placed: int | None = None
    days: set[int] = set()
    # Round trips, flat to flat, for `coin_flip`: the cash each opened on, its largest notional at the prices of
    # its own fills, the cash it closed on, and the equity the day's line was counted from at its close (the
    # latest snapshot; the capital before any).
    trips: list[dict] = []
    trip: dict | None = None
    last_px: dict[int, int] = {}
    day_eq = step = 0
    peak = 0  # the step's highest day's snapshot, the starting balance first
    standing: dict[tuple[int, bool], dict] = {}  # the stops and takes now set, by asset and kind
    taken_off: dict[tuple[str, int, bool], dict | None] = {}  # what a transaction's `TriggerCleared` removed
    shape: tuple[int, int, int] | None = None  # the latest fill's index, and the position before and after it
    if line_read:
        out["drawdown"] = {"kind": DRAWDOWN_KINDS[kind], "bps": dd_bps, "line": None, "peak": None, "checked": 0}

    def px(asset: int, block: int) -> int | None:
        return oracle.get(oracle_key(asset, block))

    def flag(what: str, e: dict, detail: str) -> None:
        flags.append({"what": what, "tx": e["tx"], "block": e["block"], "detail": detail})

    def result(name: str, e: dict) -> None:
        out["checked"]["results"] += 1
        if any(pos.values()) or e["equity"] != cash:
            flags.append({"what": "result", "tx": e["tx"], "block": e["block"],
                          "detail": f"{name} wrote {e['equity']}, the fills give {cash}" + (" and a position open" if any(pos.values()) else "")})

    def line() -> int | None:
        return drawdown_line(kind, capital, dd_bps, peak) if line_read else None

    for i, e in enumerate(ev):
        name = e["name"]
        if name == "Started":
            capital = cash = day_eq = peak = e["capital"]
            out["capital"] = capital
        elif name == "Placed":
            placed = e["block"]
        elif name == "Filled":
            delta = e["sz"] if e["buy"] else -e["sz"]
            before = pos.get(e["asset"], 0)
            if trip is None:
                trip = {"step": step, "open": cash, "ntl": 0, "t0": e["t"]}
            cash -= delta * e["px"] + e["cost"]
            pos[e["asset"]] = before + delta
            shape = (i, before, before + delta)
            last_px[e["asset"]] = e["px"]
            trip["ntl"] = max(trip["ntl"], sum(abs(q) * last_px[a] for a, q in pos.items()))
            if not any(pos.values()):
                trips.append({**trip, "close": cash, "t1": e["t"], "dayEq": day_eq})
                trip = None
            out["fills"] += 1
            days.add(e["t"] // 86_400)
            if before != 0 and (before + delta == 0 or before * (before + delta) < 0):
                # A position closed or turned round loses its stop and its take, before this fill is written.
                for (asset, take), t in standing.items():
                    if asset == e["asset"]:
                        flag("trigger-kept", e, f"a {'take' if take else 'stop'} at {t['px']} stands on asset {asset} after "
                             "the position was closed or turned round")
            ntl = e["sz"] * e["px"]
            if cost_ppm is not None and e["cost"] != (ntl * cost_ppm + PPM - 1) // PPM:
                flag("cost", e, f"cost {e['cost']}, the terms give {(ntl * cost_ppm + PPM - 1) // PPM}")
            o = px(e["asset"], e["block"])
            if o is not None:
                out["checked"]["fills"] += 1
                better = e["px"] < o if e["buy"] else e["px"] > o
                if better:
                    flag("better-than-oracle", e, f"{'bought' if e['buy'] else 'sold'} at {e['px']}, the oracle {o}")
                elif e["source"] != SOURCE_BOOK and e["px"] != o:
                    flag("not-the-oracle", e, f"filled at {e['px']} as the oracle's, the oracle {o}")
                prev, nxt = px(e["asset"], e["block"] - 1), px(e["asset"], e["block"] + 1)
                if prev is not None and nxt is not None:
                    mid = (prev + nxt) / 2
                    if abs(o - mid) * BPS > p.outlier_bps * mid and abs(prev - nxt) * BPS <= p.outlier_bps * mid:
                        flag("oracle-outlier", e, f"oracle {o} against {prev} and {nxt} in the blocks around it")
                v = venue.get(str(e["asset"]), {}).get(str(e["t"] // 60 * 60))
                if v is not None and abs(o - v) * BPS > p.venue_bps * v:
                    flag("oracle-off-venue", e, f"oracle {o}, the venue {v} that minute")
                at_order = px(e["asset"], placed) if placed is not None and i not in by_trigger else None
                if at_order:
                    # In the trader's favour when the price moved toward their side while the order waited.
                    moved = (at_order - o if e["buy"] else o - at_order) * BPS / at_order
                    out["moves"].append({"tx": e["tx"], "bps": round(moved, 1)})
            if i not in by_trigger:
                placed = None  # a stop's close was no order: the order waiting, if any, is still waiting
        elif name == "TriggerSet":
            out["triggers"]["set"] += 1
            if pos.get(e["asset"], 0) == 0:
                flag("trigger-no-position", e, f"a {'take' if e['take'] else 'stop'} set on asset {e['asset']} with no position")
            standing[(e["asset"], e["take"])] = {"px": e["px"], "sz": e["sz"], "armedAt": e["armedAt"], "seenAt": None}
        elif name == "TriggerCleared":
            taken_off[(e["tx"], e["asset"], e["take"])] = standing.pop((e["asset"], e["take"]), None)
        elif name == "TriggerSeen":
            t = standing.get((e["asset"], e["take"]))
            if t is None:
                flag("trigger-unset", e, f"a {'take' if e['take'] else 'stop'} on asset {e['asset']} was seen to trigger with none set")
            elif t["seenAt"] is None:
                t["seenAt"] = e["seenAt"]
        elif name == "TriggerFilled":
            out["triggers"]["filled"] += 1
            t = taken_off.get((e["tx"], e["asset"], e["take"]))
            if shape is None or shape[0] != i - 1 or (i - 1) not in by_trigger:
                flag("trigger-unmatched", e, "a trigger carried out with no fill of its own right before it")
                continue
            _, before, after = shape
            word = "take" if e["take"] else "stop"
            if t is None:
                flag("trigger-unset", e, f"a {word} carried out on asset {e['asset']} with none standing")
                continue
            if e["block"] < t["armedAt"]:
                flag("trigger-early", e, f"the {word} acts from block {t['armedAt']}, carried out in {e['block']}")
            if e["trigger_px"] != t["px"]:
                flag("trigger-price", e, f"the {word} stood at {t['px']}, the event says {e['trigger_px']}")
            o = px(e["asset"], e["block"])
            if o is not None and before != 0 and not trigger_hit(e["take"], before, o, t["px"]):
                flag("trigger-not-reached", e, f"the {word} at {t['px']} carried out with the oracle at {o}")
            open_ = abs(before)
            want = open_ if t["sz"] == 0 or t["sz"] > open_ else t["sz"]
            filled = ev[i - 1]
            if filled["buy"] != (before < 0) or abs(after) >= open_ or e["sz"] != want:
                flag("trigger-size", e, f"the {word} closes {want} of {open_} on the side that makes the position smaller; "
                     f"it filled {'bought' if filled['buy'] else 'sold'} {e['sz']}")
        elif name == "DaySnapshot":
            day_eq = e["equity"]
            if line_read:
                # A snapshot is taken only while no rule is broken: its equity is on or over the line as it stood
                # before this snapshot could raise it.
                before_line = line()
                out["drawdown"]["checked"] += 1
                if e["equity"] < before_line:
                    flag("snapshot-under-line", e, f"day {e['day']} wrote {e['equity']}, under the drawdown line {before_line}")
                peak = max(peak, e["equity"])
                out["drawdown"]["line"], out["drawdown"]["peak"] = line(), peak
            open_ = {a: q for a, q in pos.items() if q}
            marks = {a: px(a, e["block"]) for a in open_}
            if any(m is None for m in marks.values()):
                out["checked"]["unchecked"] += 1
                continue
            out["checked"]["snapshots"] += 1
            want = cash + sum(q * marks[a] for a, q in open_.items())
            if e["equity"] != want:
                flag("snapshot", e, f"day {e['day']} wrote {e['equity']}, the fills and the oracle give {want}")
        elif name == "StepPassed":
            result("StepPassed", e)
            cash, pos = capital, {}  # `days` stays the whole account's, as `fills` does (review of #290)
            trip, day_eq, step, peak, standing = None, capital, step + 1, capital, {}
        elif name == "Passed":
            result("Passed", e)
            out["passed"] = True
        elif name == "Stopped" and line_read and e.get("reason") is not None:
            # Ended by a rule: by the drawdown if the equity is under the line, which `_judge` asks first.
            under = e["equity"] < line()
            if (e["reason"] == BREACH_DRAWDOWN) != under and e["status"] == 3:
                flag("breach-line", e, f"ended with reason {e['reason']} at {e['equity']}, the drawdown line {line()}")
    out["days"] = len(days)
    if line_read and capital:
        out["drawdown"]["line"], out["drawdown"]["peak"] = line(), peak
    if out["passed"]:
        out["flip"] = coin_flip(trips, (chain.get("dailyLossBps") or {}).get(account), p)
    if out["passed"] and size is not None and size == largest and (out["fills"] <= p.few_fills or out["days"] <= p.few_days):
        out["profile"] = (f"passed in {out['fills']} fills over {out['days']} days on the largest size: a pass that "
                          "could be luck, not a sign of cheating")
    return out


# ── how often the pool's exams pass, against the model ──────────────────────────────

# The CFO's thresholds for the first pool (13 November; to be looked at again after its first 100 exams), from a
# file with --thresholds. Passes among the pool's last 100 finished exams, payouts recorded among its last 100
# exams sold, and payouts over the net income from prices over 30 days; each yellow or red from its line. Shares
# count from 30 finished exams on; before that the pool is red if the payouts recorded since it opened are more
# than its net income from prices since then.
DEFAULT_THRESHOLDS = {"passes": {"window": 100, "yellow": 24, "red": 28},
                      "payouts": {"window": 100, "yellow": 6, "red": 8},
                      "payoutsToIncome": {"days": 30, "yellow": 0.5, "red": 0.75},
                      "minFinished": 30}
LEVELS = ("few", "ok", "yellow", "red")


def binom_tail(k: int, n: int, q: float) -> float:
    """P(X >= k) for X ~ Binomial(n, q), summed exactly in logs."""
    if k <= 0:
        return 1.0
    if k > n or q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    lq, l1 = math.log(q), math.log1p(-q)
    base = math.lgamma(n + 1)
    return min(1.0, sum(math.exp(base - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lq + (n - i) * l1)
                        for i in range(k, n + 1)))


def per_hundred(hits: int, n: int, spec: dict) -> dict:
    """`hits` among the last `n` (at most the window), as a level: counted per hundred where fewer than a hundred."""
    per = hits * 100 / n if n else 0.0
    level = "red" if per >= spec["red"] else "yellow" if per >= spec["yellow"] else "ok"
    return {"hits": hits, "of": n, "perHundred": round(per, 1), "yellow": spec["yellow"], "red": spec["red"], "level": level}


def pass_rates(chain: dict, pool: str, p: "Params", th: dict | None = None) -> dict | None:
    """The pool's passes against the CFO's thresholds, and the same counts split by the code each exam was bought
    under, by the asset it traded, by the hour and weekday of the pass, and the passes in very few fills. None
    when the accounts' events were not read. A rule of the exam that is wrong still counts up on chain; it shows
    as more passing than the model said.

    `coinFlipPasses` is how many of the passes in the same window as the share of passes (the last 100 exams
    ended) have the shape of a coin flip (`coin_flip`): a number said beside the others, with no threshold of its
    own yet and no part in the level."""
    th = {**DEFAULT_THRESHOLDS, **(th or {})}
    ev = chain.get("exam_events")
    if ev is None:
        return None
    stage_of = {f["exam"]: f["stage"] for f in chain["funded"]}
    paid_stages = {x["account"] for x in chain["payouts"]}
    rows = []
    for s_ in chain["sales"]:
        if s_["pool"] != pool:
            continue
        # Every sale of the pool counts for income and payouts; passes need the exam's own events.
        evs = ev.get(s_["exam"]) or []
        started = next((e["t"] for e in evs if e["name"] == "Started"), None)
        end = next((e for e in evs if e["name"] in ("Passed", "Stopped")), None)
        fills = [e for e in evs if e["name"] == "Filled"]
        passed = end is not None and end["name"] == "Passed"
        rows.append({"exam": s_["exam"], "code": s_["code"], "block": s_["block"], "t": started, "endedAt": end["t"] if end else None,
                     "passed": passed, "payout": stage_of.get(s_["exam"]) in paid_stages,
                     "net": s_["price"] - s_.get("fee", 0), "assets": sorted({e["asset"] for e in fills}),
                     "few": passed and (len(fills) <= p.few_fills or len({e["t"] // 86_400 for e in fills}) <= p.few_days)})
    finished = sorted((r for r in rows if r["endedAt"] is not None), key=lambda r: r["endedAt"])
    sold = sorted(rows, key=lambda r: r["block"])
    pays = [x for x in chain["payouts"] if x["pool"] == pool]
    since = chain["now"] - th["payoutsToIncome"]["days"] * 86_400
    out = {"thresholds": th, "finished": len(finished), "sold": len(rows)}
    if len(finished) < th["minFinished"]:
        paid, net = sum(x["amount"] for x in pays), sum(r["net"] for r in rows)
        out["early"] = {"payouts": paid, "net": net, "level": "red" if paid > net else "few"}
        out["level"] = out["early"]["level"]
    else:
        last = finished[-th["passes"]["window"]:]
        out["passes"] = per_hundred(sum(r["passed"] for r in last), len(last), th["passes"])
        lastsold = sold[-th["payouts"]["window"]:]
        out["payouts"] = per_hundred(sum(r["payout"] for r in lastsold), len(lastsold), th["payouts"])
        # A payout is claimed `delay` before it is due: the factory's own, where it was read (review of #290).
        delay = (chain.get("times") or {}).get("delay") or PAYOUT_DELAY
        paid = sum(x["amount"] for x in pays if x["dueAt"] - delay >= since)
        net = sum(r["net"] for r in rows if r["t"] is not None and r["t"] >= since)
        ratio = paid / net if net else (float("inf") if paid else 0.0)
        spec = th["payoutsToIncome"]
        out["payoutsToIncome"] = {"payouts": paid, "net": net, "ratio": None if ratio == float("inf") else round(ratio, 4),
                                  "yellow": spec["yellow"], "red": spec["red"],
                                  "level": "red" if ratio >= spec["red"] else "yellow" if ratio >= spec["yellow"] else "ok"}
        out["level"] = max((out[k]["level"] for k in ("passes", "payouts", "payoutsToIncome")), key=LEVELS.index)
    count = lambda rs: {"sold": len(rs), "passed": sum(r["passed"] for r in rs), "payouts": sum(r["payout"] for r in rs)}
    out["byCode"] = {c_: count([r for r in rows if r["code"] == c_]) for c_ in sorted({r["code"] for r in rows})}
    out["byAsset"] = {str(a): count([r for r in rows if a in r["assets"]]) for a in sorted({a for r in rows for a in r["assets"]})}
    out["byHour"], out["byWeekday"] = {}, {}
    for r in finished:
        if r["passed"]:
            h, wd = str(r["endedAt"] // 3_600 % 24), str((r["endedAt"] // 86_400 + 3) % 7)  # 1970-01-01 was a Thursday
            out["byHour"][h] = out["byHour"].get(h, 0) + 1
            out["byWeekday"][wd] = out["byWeekday"].get(wd, 0) + 1
    out["fewFillPasses"] = sum(r["few"] for r in rows)
    window = [r for r in finished[-th["passes"]["window"]:] if r["passed"]]
    flips = [recount(r["exam"], chain, p)["flip"] for r in window]
    out["coinFlipPasses"] = {"hits": sum(f is not None for f in flips), "withStops": sum(bool(f and f["stops"]) for f in flips),
                             "passes": len(window), "share": round(sum(f is not None for f in flips) / len(window), 3) if window else None}
    # Red: no approving in a batch; each payout is approved on its own (the CFO, 13 November).
    out["batch"] = out["level"] != "red"
    return out


# ── how the fills were timed: judged, not only shown ─────────────────────────────────

def timing(trader: str, chain: dict, who: dict[str, str], p: "Params") -> dict:
    """Two questions about each fill of this trader's exams and stages, from the oracle by block.

    The filler's choice: an order can be filled in any block of its window (from `late` 0, the first possible block,
    to the window's width). Was the block it was filled in the best of the window for the trader, the lowest price
    for a buy, the highest for a sell? A fair filler fills at once or at random; one working for the trader picks
    the best block. By chance a fill lands on a best block about as often as best blocks are in the window (ties
    counted), so the number that did is tested against that: flagged when chance would give as many or more with
    probability under `p.timing_alpha`, over `p.timing_min` fills at least, and the trader gained by it.

    The lead on the feed: did the oracle move the trader's way in the block right after each fill? A trader who sees
    the price before the oracle does buys just before it rises. Over the fills where it moved at all, the moves the
    trader's way are tested against a coin: flagged on the same terms."""
    ev = chain.get("exam_events") or {}
    oracle = chain.get("oracle") or {}
    width = chain.get("fillWindow") or {}
    accounts = [a for a, t in who.items() if t == trader and a in ev]
    picked = hits = 0
    chance = 0.0
    gains: list[float] = []
    moved = ahead = 0
    lates: list[int] = []
    for a in accounts:
        w = width.get(a)
        closed_by_trigger = trigger_fills(ev[a])
        for k, e in enumerate(ev[a]):
            # A stop's or a take's close has no order and no window: neither the filler's choice of a block nor the
            # trader's timing is in it (it fires when the oracle reaches a price the trader set earlier).
            if e["name"] != "Filled" or k in closed_by_trigger:
                continue
            lates.append(e["late"])
            o = oracle.get(oracle_key(e["asset"], e["block"]))
            if o is None:
                continue
            side = 1 if e["buy"] else -1  # a buy gains from a lower price, a sell from a higher
            first = e["block"] - e["late"]
            if w and w > 1:
                window = [oracle.get(oracle_key(e["asset"], first + k)) for k in range(w)]
                # A zero price (a feed that was not live, or one a --prices file gave) is no price: not judged, not divided by.
                if all(x is not None and x > 0 for x in window):
                    best = min(window) if e["buy"] else max(window)
                    picked += 1
                    hits += o == best
                    chance += sum(x == best for x in window) / w
                    gains.append((window[0] - o) * side * BPS / window[0])
            nxt = oracle.get(oracle_key(e["asset"], e["block"] + 1))
            if nxt is not None and nxt != o and nxt > 0 and o > 0:  # no move is read off a price that is not one
                moved += 1
                ahead += (nxt - o) * side > 0
    out = {"lateMedian": quantile(lates, 0.5), "fills": len(lates),
           "filler": {"fills": picked, "atBest": hits, "byChance": round(chance, 2),
                      "gainBps": round(sum(gains) / len(gains), 1) if gains else 0.0, "flag": False},
           "lead": {"fills": moved, "ahead": ahead, "flag": False}}
    if picked >= p.timing_min and gains and sum(gains) > 0:
        out["filler"]["p"] = binom_tail(hits, picked, chance / picked)
        out["filler"]["flag"] = out["filler"]["p"] < p.timing_alpha
    if moved >= p.timing_min:
        out["lead"]["p"] = binom_tail(ahead, moved, 0.5)
        out["lead"]["flag"] = out["lead"]["p"] < p.timing_alpha
    return out


# ── the review of one payout ─────────────────────────────────────────────────────────

class Params:
    def __init__(self, window_s: int = 600, tolerance: float = 0.2, wide: int = 5, code_run: int = 10,
                 idle_share: float = 0.5, min_fills: int = 5, mirror_ratio: float = 0.5, mirror_share: float = 0.6,
                 mirror_min_s: int = 3_600, sum_dedication: float = 0.6, timing_min: int = 8,
                 timing_alpha: float = 0.01, outlier_bps: int = 100, venue_bps: int = 100, few_fills: int = 3,
                 few_days: int = 2, flip_leverage: float = 3.0, flip_near: float = 0.8, flip_trips: int = 4,
                 flip_dust: float = 0.05):
        self.window_s = window_s
        self.tolerance = tolerance
        self.wide = wide
        self.code_run = code_run
        self.idle_share = idle_share
        self.min_fills = min_fills
        # Mirrored accounts: the smaller position at least half the larger, for 60 % of the account's time with a
        # position and an hour at least.
        self.mirror_ratio = mirror_ratio
        self.mirror_share = mirror_share
        self.mirror_min_s = mirror_min_s
        # The opposite side over many accounts: only accounts that spent 60 % of their own time against this one.
        self.sum_dedication = sum_dedication
        # Timing: judged over 8 fills at least, flagged when chance gives as much with probability under 1 %.
        self.timing_min = timing_min
        self.timing_alpha = timing_alpha
        # The recount: an oracle price 1 % off both neighbours that agree within 1 % is an outlier, as is one 1 %
        # off the venue's; a pass in 3 fills or 2 days at most on the largest size is said as a profile.
        self.outlier_bps = outlier_bps
        self.venue_bps = venue_bps
        self.few_fills = few_fills
        self.few_days = few_days
        # The coin flip (`coin_flip`): round trips opened at 3 times the account at most, 4 a step at most, each
        # loss with 80 % of the day's allowance gone; a round trip under 5 % of the account is not counted.
        self.flip_leverage = flip_leverage
        self.flip_near = flip_near
        self.flip_trips = flip_trips
        self.flip_dust = flip_dust


def owners(chain: dict) -> dict[str, str]:
    """Each exam and stage of the factory, to its trader."""
    out = {s["exam"]: s["trader"] for s in chain["sales"]}
    out.update({f["stage"]: f["trader"] for f in chain["funded"]})
    return out


def pairs_of(account: str, chain: dict, who: dict[str, str], p: Params) -> list[dict]:
    mine = [f for f in chain["fills"] if f["account"] == account]
    return [{"mine": a["tx"], "other": b["tx"], "account": b["account"], "trader": who.get(b["account"]),
             "asset": a["asset"], "buy": a["buy"], "sz": [a["sz"], b["sz"]], "t": [a["t"], b["t"]]}
            for a, b in chk.pairs(mine, chain["fills"], p.window_s, p.tolerance)]


def linked(trader: str, chain: dict, p: Params) -> list[dict]:
    """Every other buyer of the factory this trader shares a sender of USDC with, or exchanged USDC with."""
    pools = set(chain["pools"])
    buyers = sorted({s["trader"] for s in chain["sales"]})
    transfers = [t for t in chain["transfers"] if t["from"] not in pools]
    # One pass over the transfers, indexed by sender and pair (review of #283): the first of each sender to each
    # buyer, the buyers each sender funded, and the transfers between this trader and each other wallet.
    first: dict[tuple[str, str], str] = {}
    funded: dict[str, set[str]] = {}
    direct: dict[str, list[str]] = {}
    for t in transfers:
        if t["to"] in buyers:
            first.setdefault((t["from"], t["to"]), t["tx"])
            funded.setdefault(t["from"], set()).add(t["to"])
        if trader in (t["from"], t["to"]) and t["from"] != t["to"]:
            direct.setdefault(t["to"] if t["from"] == trader else t["from"], []).append(t["tx"])
    mine = sorted(s for s, got in funded.items() if trader in got and s != trader)
    out = []
    for other in buyers:
        if other == trader:
            continue
        for sender in mine:
            if sender != other and other in funded[sender]:
                out.append({"trader": other, "sender": sender, "txs": [first[(sender, trader)], first[(sender, other)]],
                            "wide": len(funded[sender]) > p.wide})
        if direct.get(other):
            out.append({"trader": other, "sender": None, "txs": direct[other], "wide": False})
    return out


def link_txs(trader: str, other: str | None, chain: dict, p: Params | None = None) -> list[str]:
    """The links a stop of reason 2 writes between the trader and the paired account's trader: the pools' own
    payouts left out (they pay every trader), and a sender that funded more than `p.wide` buyers left out as
    `linked` leaves it out, so a stop never names a link the review itself does not count (review of #283).
    Transfers between the two traders stay: they need no sender's count."""
    if other is None:
        return []
    p = p or Params()
    pools = set(chain["pools"])
    buyers = {s["trader"] for s in chain["sales"]}
    funded: dict[str, set[str]] = {}
    for t in chain["transfers"]:
        if t["to"] in buyers and t["from"] not in pools:
            funded.setdefault(t["from"], set()).add(t["to"])
    kept = [t for t in chain["transfers"] if t["from"] not in pools
            and (t["from"] in (trader, other) or len(funded.get(t["from"], ())) <= p.wide)]
    return chk.links(kept, trader, other)


def code_review(pool: str, code: str, chain: dict, who: dict[str, str], p: Params) -> dict:
    sales = [s for s in chain["sales"] if s["pool"] == pool]
    mine = [s for s in sales if s["code"] == code]
    partner = mine[0]["partner"] if mine else None
    buyers = sorted({s["trader"] for s in mine})
    traded = {who.get(f["account"]) for f in chain["fills"]}
    idle = [b for b in buyers if b not in traded]
    accounts = [a for a, t in who.items() if t in buyers]
    paired = sorted({who[a] for a in accounts if pairs_of(a, chain, who, p)})
    run = longest_new_wallet_run(sales, code)
    idle_share = len(idle) / len(buyers) if buyers else 0.0
    flag = run >= p.code_run and (idle_share >= p.idle_share or len(paired) * 2 >= len(buyers))
    return {"code": code, "partner": partner, "sales": len(mine), "wallets": len(buyers), "run": run,
            "idle": len(idle), "paired": len(paired), "partnerBuys": partner in buyers, "flag": flag}


def running(pool: str, chain: dict) -> dict:
    """What the pool's exams and stages still able to pass would be paid if all passed, against the
    money the pool holds free of what it owes, holds back and keeps for stops."""
    lines = {s["exam"]: s["size"] for s in chain["sales"] if s["pool"] == pool}
    for f in chain["funded"]:
        if f["pool"] == pool:
            lines[f["stage"]] = lines.get(f["exam"], 0)
    exams = stages = worst = stages_worst = 0
    funded_exams = {f["exam"] for f in chain["funded"]}
    stage_accounts = {f["stage"] for f in chain["funded"]}
    # An account not read may be running: its payout is not in `worst`, so the total is not complete (review of #283).
    unread = sorted(a for a in lines if a in set(chain.get("unread") or []))
    for a, size in lines.items():
        # Running, or passed and not yet funded or claimed; an exam funded runs on as its stage.
        if chain["done"].get(a) or a in funded_exams or chain["status"].get(a) not in ("Active", "Passed"):
            continue
        if a in stage_accounts:
            stages += 1
            stages_worst += payout_of(size)
        else:
            exams += 1
        worst += payout_of(size)
    book = chain["books"][pool]
    free = book["balance"] - book["owed"] - book["held"] - book["reserved"]
    # What the pool itself keeps above that (SimPool.withdrawable): the larger of FLOOR_PAYOUTS of the largest
    # payout and `staged`, the payouts of its running stages. `worst` here is stricter: it counts the running exams
    # too, each as if it passed. Where the pool answers `staged`, the stages counted here are held to it.
    staged = book.get("staged")
    largest = book.get("largestPayout")
    out = {"exams": exams, "stages": stages, "worst": worst, "free": free, "enough": free >= worst and not unread,
           "unread": unread, "staged": staged,
           "floor": max(largest * FLOOR_PAYOUTS, staged) if staged is not None and largest is not None else None,
           "stagesWorst": stages_worst}
    out["stagedMatches"] = None if staged is None else staged == stages_worst
    return out


def stop_call(pool: str, pid: int, reason: int, paired: str, trades: list[str], links: list[str]) -> dict:
    args = [pid, reason, paired, [bytes.fromhex(t[2:]) for t in trades], [bytes.fromhex(t[2:]) for t in links]]
    return {"kind": "stop", "to": pool, "call": chk.STOP, "args": [pid, reason, paired, trades, links],
            "data": "0x" + c.encode_call(chk.STOP, chk.STOP_TYPES, args).hex()}


NO_APPROVAL = "in this pool a payout goes by itself 72 hours after its claim; there is no approval to sign"


def approve_call(pool: str, pid: int, approvals: bool = True, approver: str | None = None, approved: bool = False,
                 state: str = "Due") -> dict:
    """The approval, only where the pool would take it (review of #283). A payout that is not waiting (paid,
    stopped, held, refused) takes none: `approvePayouts` reverts it (`BadPayout`), and the review says its state.
    A pool without approvals pays at 72 hours by itself, and an approval sent to it would revert. A payout
    approved already reverts a second approval (`BadPayout`), and a pool whose approver is the zero address has
    nobody to send one (`NotApprover`)."""
    none = {"kind": "none", "to": pool, "call": None, "args": [], "data": None}
    if state != "Due":
        return {**none, "note": f"the payout is {state.lower()}, not waiting: nothing to approve and nothing to stop"}
    if not approvals:
        return {**none, "note": NO_APPROVAL}
    if approved:
        return {**none, "note": APPROVED}
    if approver is not None and int(approver, 16) == 0:
        return {**none, "note": NO_APPROVER}
    if APPROVE is None:
        return {"kind": "approve", "to": pool, "call": None, "args": [pid], "data": None,
                "note": "the pool has no approval call yet"}
    return {"kind": "approve", "to": pool, "call": APPROVE, "args": [[pid]],
            "data": "0x" + c.encode_call(APPROVE, APPROVE_TYPES, [[pid]]).hex()}


def trail_disagreement(trail: dict | None, pay: dict, approvals: bool | None) -> str | None:
    """Why a payout's recorded events (`payout_trail`) and the state the pool answered cannot both be right, or None.
    Without a trail (a chain read before it was kept) there is nothing to compare. The approval is compared only where
    the pool takes approvals and the payout's `approved` was read."""
    if trail is None:
        return None
    if trail["problems"]:
        return "the payout's recorded events are not a payout's life (" + "; ".join(trail["problems"]) + ")"
    if trail["state"] != pay["state"]:
        return f"the payout's recorded events say {trail['state']}, the pool says {pay['state']}"
    approved = pay.get("approved")  # compared only as the storage answered it: None is taken from the trail, not against it
    if approvals is True and approved is not None and trail["approved"] != bool(approved):
        return f"the payout's recorded events say {'approved' if trail['approved'] else 'not approved'}, the pool says the other"
    return None


def review(chain: dict, pool: str, pid: int, p: Params | None = None, thresholds: dict | None = None) -> dict:
    p = p or Params()
    pay = next(x for x in chain["payouts"] if x["pool"] == pool and x["id"] == pid)
    approvals = chain["books"][pool].get("approvals", True)  # True, False, or None: not read
    # The delay and the ceiling are the factory's own (`payout_times`), never this file's (review of #283); a
    # chain made up without them stands on the design's. Not read, a pool with approvals is not read either:
    # its ceiling would be a guess.
    times = chain.get("times") or {"delay": PAYOUT_DELAY, "hard": PAYOUT_HARD}
    delay, hard = times.get("delay"), times.get("hard")
    times_unread = approvals is True and (delay is None or hard is None)
    # Who approves (the zero address: nobody) and whether this payout is approved already. On a pool with
    # approvals a payout whose `approved` was not read is not read: an approval offered twice would revert.
    approver, approved = chain["books"][pool].get("approver"), pay.get("approved", False)
    trail = pay.get("trail")
    # A storage answer that has no `approved` (the eighth field) leaves it to the payout's recorded events; where those
    # are no payout's life or end elsewhere than the storage says, `trail_disagreement` leaves the pool not read.
    if approved is None and trail is not None:
        approved = trail["approved"]
    approved_unread = approvals is True and approved is None
    # The payout's recorded life (its own events, in order) and the pool's storage must tell the same story: where
    # they do not (a node that served a partial log, a reorg) neither is taken for the truth.
    disagree = trail_disagreement(trail, pay, approvals)
    if times_unread or approved_unread or disagree:
        approvals = None
    who = owners(chain)
    trader, stage = pay["trader"], pay["account"]
    exam = next((f["exam"] for f in chain["funded"] if f["stage"] == stage), None)
    sale = next((s for s in chain["sales"] if s["exam"] == exam), None)
    out = {"pool": pool, "id": pid, "trader": trader, "stage": stage, "exam": exam, "amount": pay["amount"],
           "claimedAt": pay["dueAt"] - delay if delay is not None else None, "dueAt": pay["dueAt"],
           "hardAt": pay["dueAt"] - delay + hard if approvals is True else None, "approvals": approvals,
           "approver": approver if approvals is True else None, "approved": bool(approved) if approvals is True else None,
           "state": pay["state"], "trail": trail["events"] if trail else None,
           "size": sale["size"] if sale else None,
           "fills": {"stage": sum(f["account"] == stage for f in chain["fills"]),
                     "exam": sum(f["account"] == exam for f in chain["fills"])}}
    # Waiting for approval: due, never approved, on a pool that takes approvals; by the payout's recorded events
    # where they were kept, by its storage otherwise. Never by the clock: a payout past its 72 hours with no approval
    # written waits for one, and one approved before them does not.
    out["waitingApproval"] = bool(approvals is True and (trail["waitingApproval"] if trail else pay["state"] == "Due" and not approved))
    pairs = {"stage": pairs_of(stage, chain, who, p), "exam": pairs_of(exam, chain, who, p) if exam else []}
    out["pairs"] = pairs

    # The stop, if there is a pair: the stage's first, the exam's otherwise, with sim_check's choice of
    # the other account and its order of hashes.
    stop = None
    for part in ("stage", "exam"):
        if pairs[part]:
            account = stage if part == "stage" else exam
            mine = [f for f in chain["fills"] if f["account"] == account]
            found = chk.pairs(mine, chain["fills"], p.window_s, p.tolerance)
            paired = chk.finding(found, [])[1]
            links = link_txs(trader, who.get(paired), chain, p)
            reason, paired, trades, links = chk.finding(found, links)
            stop = {"on": part, "by": "pair", "reason": reason, "paired": paired, "pairedTrader": who.get(paired),
                    "trades": trades, "links": links}
            break

    # Mirrored accounts: positions held against positions for most of the time, whatever the fills' timing and
    # sizes. With no pair, a mirror whose examples carry two opposite fills is the stop: reason 1, "opposite
    # positions with `paired`", or 2 with the wallets linked.
    mirrors = {"stage": mirrors_of(stage, chain, who, p), "exam": mirrors_of(exam, chain, who, p) if exam else []}
    out["mirrors"] = mirrors
    out["opposingSum"] = {part: opposing_sum(account, chain, who, p) if account else None
                          for part, account in (("stage", stage), ("exam", exam))}
    for part in ("stage", "exam"):
        if stop:
            break
        for m in mirrors[part]:
            if m["examples"]:
                trades = list(dict.fromkeys(tx for ex in m["examples"] for tx in ex))
                links = link_txs(trader, m["trader"], chain, p)
                stop = {"on": part, "by": "mirror", "reason": 2 if links else 1, "paired": m["account"],
                        "pairedTrader": m["trader"], "trades": trades, "links": links}
                break
    out["stop"] = stop

    # The result counted again, for the stage and for the exam it came from.
    sizes = (chain["books"].get(pool) or {}).get("sizes") or [x["size"] for x in chain["sales"] if x["pool"] == pool]
    largest = max(sizes, default=None)
    out["recount"] = {part: recount(account, chain, p, out["size"], largest) if account else None
                      for part, account in (("stage", stage), ("exam", exam))}

    links = linked(trader, chain, p)
    out["links"] = links
    # The lateness of a fill is the filler's: a stop's or a take's close has none (`via`), and is left out of both.
    mine_late = [f["late"] for f in chain["fills"] if who.get(f["account"]) == trader and not f.get("via")]
    others_late = [f["late"] for f in chain["fills"] if who.get(f["account"]) not in (trader, None) and not f.get("via")]
    out["late"] = {"trader": {"n": len(mine_late), "median": quantile(mine_late, 0.5), "max": max(mine_late, default=0)},
                   "others": {"n": len(others_late), "median": quantile(others_late, 0.5),
                              "p90": quantile(others_late, 0.9)},
                   "outlier": late_outlier(mine_late, others_late, p.min_fills)}
    out["code"] = code_review(pool, sale["code"], chain, who, p) if sale else None
    out["running"] = running(pool, chain)

    found: list[tuple[str, str]] = []  # (kind, text), in the order a page lists them
    # The pool's passes against the model first: on every payout of the pool while it holds. The trader is not
    # to blame, so the approval stays; the choice is the signer's.
    out["passRates"] = pr = pass_rates(chain, pool, p, thresholds)
    if pr and pr["level"] in ("yellow", "red"):
        said = [f"{k} {pr[k]['hits']} of {pr[k]['of']} ({pr[k]['level']})" for k in ("passes", "payouts") if k in pr
                and pr[k]["level"] != "ok"]
        if "payoutsToIncome" in pr and pr["payoutsToIncome"]["level"] != "ok":
            said.append(f"payouts {pr['payoutsToIncome']['ratio']} of net income ({pr['payoutsToIncome']['level']})")
        if "early" in pr:
            said.append(f"payouts {pr['early']['payouts']} above net income {pr['early']['net']} before 30 exams ended")
        found.append(("pass-rate", f"the pool passes more often than the model ({pr['level']}): " + "; ".join(said)))
    if stop and stop["by"] == "pair":
        found.append(("pair", f"pair on the {stop['on']} with {stop['paired']}"
                      + (" and linked wallets" if stop["reason"] == 2 else "")))
    for part in ("stage", "exam"):
        for m in mirrors[part]:
            if stop and stop["by"] == "pair" and m["account"] == stop["paired"]:
                continue  # the same two accounts, said once
            linked_too = stop and stop["by"] == "mirror" and m["account"] == stop["paired"] and stop["reason"] == 2
            found.append(("mirror", f"mirrored positions on the {part} with {m['account']} for "
                          f"{round(m['share'] * 100)} % of its time" + (" and linked wallets" if linked_too else "")))
    for part in ("stage", "exam"):
        o = out["opposingSum"][part]
        if o:
            found.append(("mirror-sum", f"the opposite side of the {part} spread over {len(o['accounts'])} accounts for "
                          f"{round(o['share'] * 100)} % of its time"
                          + (f", {len(o['sharedSenders'])} sender(s) funding several of them" if o["sharedSenders"] else "")))
    out["timing"] = timing(trader, chain, who, p)
    tf, tl = out["timing"]["filler"], out["timing"]["lead"]
    if tf["flag"]:
        found.append(("timing", f"the filler put {tf['atBest']} of {tf['fills']} fills in the window's best block for "
                      f"the trader (chance gives about {tf['byChance']}), {tf['gainBps']} bps each"))
    if tl["flag"]:
        found.append(("timing", f"the oracle moved the trader's way right after {tl['ahead']} of {tl['fills']} fills"))
    counted = [l for l in links if not l["wide"]]
    if counted:
        found.append(("link", "wallet linked to " + ", ".join(sorted({l["trader"] for l in counted}))))
    if out["late"]["outlier"]:
        found.append(("late", "fills later than everyone else's"))
    if out["code"] and out["code"]["flag"]:
        found.append(("code", f"code {out['code']['code'][:10]}: {out['code']['run']} sales in a row from new wallets"))
    for part in ("stage", "exam"):
        rc = out["recount"][part]
        if rc and rc["flags"]:
            found.append(("recount", f"recount on the {part}: " + ", ".join(sorted({f['what'] for f in rc['flags']}))))
    read = chain.get("exam_events")
    unread = [a for a in (stage, exam) if a and (a in (chain.get("unread") or []) or (read is not None and a not in read))]
    if times_unread:
        unread.append("the factory's delay and ceiling")
    elif disagree:
        unread.append(disagree)
    elif approved_unread:
        unread.append("whether the payout is approved")
    elif approvals is None:
        unread.append("whether the pool takes approvals")
    if unread:
        found.append(("unread", "not read: " + ", ".join(unread)))
    # The daily limit on payouts: where it leaves this payout. Said beside the verdict, never a reason in it: a payout the
    # limit holds is waiting its turn, not suspicious.
    out["dailyCap"] = payout_cap(chain, pool, {**pay, "id": pid}, approvals, delay, hard)
    out["verdict"] = "suspicious" if found else "clean"
    out["reasons"] = [t for _, t in found]
    out["kinds"] = [k for k, _ in found]
    # A stop only for a payout the pool would still stop (review of #283): waiting and never stopped, and in time
    # by the pool's own rule. A pool with approvals stops until it is paid; one without, until 5 minutes before
    # `dueAt`. The rule is the checker's own (`stop_due`): the review offers what the checker would send.
    out["stoppable"] = chk.stop_due(pay["state"], bool(pay.get("wasStopped")), pay["dueAt"], chain["now"], approvals is True)
    # What whoever signs is offered (review of #287): a stop where there is one the pool would take; nothing to
    # approve where the result did not count up, only the draft of a reason-3 stop, until the analysis is written
    # or the ceiling comes; the approval as a choice where something else is suspicious; the approval where all is
    # clean. A pool without approvals is offered none: it pays at 72 hours by itself (`none`).
    if approvals is None:
        # Not read whether the pool takes approvals: no call and no word on when it pays, either way it could be wrong.
        out["stoppable"] = None
        out["action"] = "none"
        out["tx"] = {"kind": "none", "to": pool, "call": None, "args": [], "data": None,
                     "note": ("not read the factory's delay and ceiling" if times_unread else
                              disagree if disagree else
                              "not read whether the payout is approved" if approved_unread else
                              "not read whether the pool takes approvals") + ": nothing to sign until it is"}
    elif stop and not out["stoppable"]:
        why = (f"it is {pay['state'].lower()}" if pay["state"] != "Due" else
               "it was stopped once already" if pay.get("wasStopped") else "its time to stop is over")
        out["action"] = "none"
        out["tx"] = {"kind": "none", "to": pool, "call": None, "args": [], "data": None,
                     "note": f"the pool would refuse a stop now: {why}"}
    elif stop:
        out["action"] = "stop"
        out["tx"] = stop_call(pool, pid, stop["reason"], stop["paired"], stop["trades"], stop["links"])
    elif "recount" in out["kinds"] and out["waitingApproval"]:
        # Held back from approval: only an unapproved payout of a pool with approvals has one to be held back. An
        # approved payout is payable from its due time whatever the recount says, a pool without approvals pays by
        # itself, and a payout no longer waiting has nothing to hold back (review of #290); the stop draft stays.
        out["action"] = "hold"
        out["tx"] = None
    else:
        out["tx"] = approve_call(pool, pid, approvals, approver, approved, pay["state"])
        # A payout not waiting, a pool without approvals, a payout approved already, or a pool nobody approves on:
        # nothing to sign,
        # whatever else was found.
        out["action"] = "none" if out["tx"]["kind"] == "none" else "decide" if found else "approve"
        if found and out["tx"]["kind"] == "approve":
            out["tx"]["note"] = "no pair, so the pool takes no stop: approve, or wait for the ceiling"
    # A result that does not count up is a stop of reason 3, a result disputed, only with a published analysis:
    # the draft names the fills and leaves the analysis's hash to whoever writes it.
    flagged = [f["tx"] for part in ("stage", "exam") if out["recount"][part] for f in out["recount"][part]["flags"]]
    out["stopDraft"] = None
    if not stop and flagged:
        out["stopDraft"] = {"kind": "stop-draft", "to": pool, "call": chk.STOP, "reason": 3, "paired": ZERO_ADDRESS,
                            "trades": list(dict.fromkeys(flagged)), "links": [ANALYSIS], "data": None,
                            "note": "reason 3 needs the keccak256 of a published analysis: a person writes it, then the "
                                    "calldata can be built"}
    return out


SCHEMA = "payout-review/3"


def card(r: dict, chain: dict) -> dict:
    """The review as one JSON document a page reads (`app/lib/operator.js`, `reviewFor`). The schema is
    fixed: fields are added under a new SCHEMA, never renamed. /2 added `action`, `evidence.mirrors`,
    `evidence.opposingSum`, `evidence.recount` and `stopDraft`, and the reason kinds `mirror`, `mirror-sum`
    and `recount`; /3, `evidence.passRates` and `evidence.timing`, the kinds `pass-rate` and `timing`, and
    `evidence.recount.<part>.flip` (a profile, shown grey: it is in no reason and changes no colour). Amounts
    are strings of USDC units (6 decimals), since a JSON number loses digits past 2**53; times are seconds on
    the chain's clock."""
    tx = r["tx"] or {}
    g = r["running"]
    return {
        "schema": SCHEMA,
        "pool": r["pool"], "id": r["id"], "trader": r["trader"], "stage": r["stage"], "exam": r["exam"],
        "amount": str(r["amount"]),
        "claimedAt": r["claimedAt"], "dueAt": r["dueAt"], "hardAt": r["hardAt"],
        "verdict": r["verdict"], "action": r["action"],
        "reasons": [{"kind": k, "text": t} for k, t in zip(r["kinds"], r["reasons"])],
        "evidence": {
            "pairs": r["pairs"]["stage"] + r["pairs"]["exam"],
            "stop": r["stop"],
            "mirrors": r["mirrors"],
            "opposingSum": r["opposingSum"],
            "recount": r["recount"],
            "passRates": r["passRates"],
            "timing": r["timing"],
            "links": r["links"],
            "late": r["late"],
            "code": r["code"],
            "running": {"exams": g["exams"], "stages": g["stages"], "worst": str(g["worst"]), "free": str(g["free"]),
                        "enough": g["enough"]},
        },
        "call": None if not tx else {"kind": tx["kind"], "to": tx["to"],
                                     "method": tx["call"].split("(")[0] if tx["call"] else None, "signature": tx["call"],
                                     "args": tx["args"], "data": tx["data"], "note": tx.get("note")},
        "stopDraft": r["stopDraft"],
        "batchApproval": bool(r["passRates"]["batch"]) if r["passRates"] else True,
        # The contract's daily limit on payouts, as read (`payout_cap`): its number, the pool's room, and the time the payout
        # may be settled from; null for a chain read without it.
        "dailyCap": r["dailyCap"],
        "readAt": {"block": chain["latest"], "time": chain["now"]},
    }


def summary(r: dict) -> str:
    """The review in a few lines of text."""
    # Three answers here too (review of #283): a ceiling, no approval at all, or not read, which says no time.
    # "From", never "at": a payout is paid by a call, in the order of the claims and ten a call at most, so its time
    # is the earliest it can be paid, as the pool's terms say.
    when = (", approved: paid from then" if r["approvals"] is True and r["approved"] else
            f", paid unapproved from {r['hardAt']}" if r["approvals"] is True else
            ", paid by itself then" if r["approvals"] is False else ", not read when it is paid")
    lines = [f"payout {r['id']} of pool {r['pool']}: {r['amount'] / 1e6:,.2f} USDC to {r['trader']}, due {r['dueAt']}" + when,
             f"  stage {r['stage']} ({r['fills']['stage']} fills), exam {r['exam']} ({r['fills']['exam']} fills)"]
    if r["waitingApproval"]:
        lines.append("  waiting for approval: " + ("its claim is recorded and no approval after it" if r["trail"] else "due and not approved"))
    for part in ("stage", "exam"):
        for x in r["pairs"][part]:
            lines.append(f"  pair on the {part}: {x['mine']} against {x['other']} of {x['account']} (trader {x['trader']})")
    for part in ("stage", "exam"):
        for m in r["mirrors"][part]:
            lines.append(f"  mirrored on the {part}: {m['account']} (trader {m['trader']}) for {round(m['share'] * 100)} % "
                         f"of {m['exposed']} s, assets {m['assets']}")
        o = r["opposingSum"][part]
        if o:
            lines.append(f"  opposite side of the {part} over {len(o['accounts'])} accounts for {round(o['share'] * 100)} %: "
                         + ", ".join(f"{a['account']} {round(a['part'] * 100)} %" for a in o["accounts"]))
        rc = r["recount"][part]
        if rc and rc["read"]:
            c_ = rc["checked"]
            lines.append(f"  recount of the {part}: {c_['results']} results, {c_['snapshots']} snapshots, {c_['fills']} fills "
                         f"checked, {c_['unchecked']} snapshots without prices; "
                         + ("; ".join(f"{f['what']} at {f['tx']}" for f in rc["flags"]) or "all count up"))
            if rc["profile"]:
                lines.append(f"  profile: {rc['profile']}")
            if rc["flip"]:
                lines.append(f"  profile of the {part}: {rc['flip']['text']}")
    for l in r["links"]:
        lines.append(f"  linked to {l['trader']}" + (f" through {l['sender']}" if l["sender"] else " directly")
                     + (" (wide sender, not counted)" if l["wide"] else "") + ": " + ", ".join(l["txs"]))
    pr = r["passRates"]
    if pr:
        if "early" in pr:
            lines.append(f"  pool: {pr['finished']} exams ended, under 30; payouts {pr['early']['payouts']} against net income "
                         f"{pr['early']['net']}: {pr['level']}")
        else:
            lines.append(f"  pool ({pr['level']}): passes {pr['passes']['hits']} of the last {pr['passes']['of']} ended, payouts "
                         f"{pr['payouts']['hits']} of the last {pr['payouts']['of']} sold, payouts / net income "
                         f"{pr['payoutsToIncome']['ratio']}" + ("" if pr["batch"] else "; red: approve one by one"))
        cf = pr["coinFlipPasses"]
        if cf["passes"]:
            lines.append(f"  pool, for reference: {cf['hits']} of the last {cf['passes']} passes in the shape of a coin flip, "
                         f"{cf['withStops']} of them with a stop at the day's line")
    tm = r["timing"]
    lines.append(f"  timing: best block {tm['filler']['atBest']} of {tm['filler']['fills']} (chance {tm['filler']['byChance']}), "
                 f"oracle the trader's way next block {tm['lead']['ahead']} of {tm['lead']['fills']}")
    cap = r["dailyCap"]
    if cap and cap["status"] not in ("none", "not-waiting"):
        lines.append(f"  daily limit ({cap['limit']} a day, room {cap['room']}): " + (
            f"not read: {cap['problem']}" if cap["status"] == "unread" else
            "a place is free: settled by the next pay" if cap["status"] == "free" else
            f"{'ready, ' if cap['status'] == 'waits-limit' else 'ready from ' + str(cap['readyAt']) + ', '}settled from {cap['settleAt']} "
            f"({cap['ahead']} ahead of it)"))
    lt = r["late"]
    lines.append(f"  late: median {lt['trader']['median']} of {lt['trader']['n']} fills; others median "
                 f"{lt['others']['median']}, p90 {lt['others']['p90']} of {lt['others']['n']}")
    if r["code"]:
        k = r["code"]
        lines.append(f"  code {k['code']} of {k['partner']}: {k['sales']} sales, {k['wallets']} wallets, run {k['run']},"
                     f" {k['idle']} never traded, {k['paired']} in pairs" + (", its owner buys" if k["partnerBuys"] else ""))
    g = r["running"]
    lines.append(f"  running: {g['exams']} exams, {g['stages']} stages, {g['worst'] / 1e6:,.2f} USDC if all pass;"
                 f" free {g['free'] / 1e6:,.2f}: " + ("covered" if g["enough"] else "NOT covered"))
    lines.append("  verdict: " + ("clean" if r["verdict"] == "clean" else "suspicious: " + "; ".join(r["reasons"])))
    if r["stopDraft"]:
        lines.append(f"  draft of a stop, reason 3, on {', '.join(r['stopDraft']['trades'])}: {r['stopDraft']['note']}")
    tx = r["tx"]
    if r["action"] == "hold":
        lines.append(f"  do not sign: the result did not count up; it waits for an analysis, at most until {r['hardAt']}")
    else:
        lines.append(f"  to sign ({tx['kind']}): " + (f"{tx['to']} {tx['call']} {tx['data']}" if tx["data"] else tx.get("note", "")))
    return "\n".join(lines)


# ── reading the chain ────────────────────────────────────────────────────────────────

def sale_of(l: dict) -> dict:
    line, size, price, fee, _, code = abi_decode(["uint256", "uint64", "uint64", "uint256", "bool", "bytes32"],
                                               bytes.fromhex(l["data"][2:]))
    return {"pool": to_checksum_address(l["address"]), "exam": _addr(l["topics"][1]), "trader": _addr(l["topics"][2]),
            "partner": _addr(l["topics"][3]), "line": int(line), "size": int(size), "price": int(price), "fee": int(fee),
            "code": "0x" + code.hex(), "block": int(l["blockNumber"], 16), "tx": l["transactionHash"]}


def funded_of(l: dict) -> dict:
    return {"pool": to_checksum_address(l["address"]), "stage": _addr(l["topics"][1]), "exam": _addr(l["topics"][2]),
            "trader": _addr(l["topics"][3]), "block": int(l["blockNumber"], 16)}


def due_of(l: dict) -> dict:
    amount, due_at = abi_decode(["uint64", "uint64"], bytes.fromhex(l["data"][2:]))
    return {"pool": to_checksum_address(l["address"]), "id": int(l["topics"][1], 16), "account": _addr(l["topics"][2]),
            "trader": _addr(l["topics"][3]), "amount": int(amount), "dueAt": int(due_at), "block": int(l["blockNumber"], 16)}


def filled_of(l: dict, t: int) -> dict:
    f = chk.fill_of(l, t)
    f["late"] = sk.filled_of(l)[0]
    return f


# PaperExam's events (src/exam/PaperExam.sol): the second pass reads every account's own logs.
STARTED = "0x" + keccak(text="Started(address,address,uint64)").hex()
DAY_SNAPSHOT = "0x" + keccak(text="DaySnapshot(uint32,int256)").hex()
STEP_PASSED = "0x" + keccak(text="StepPassed(uint8,int256)").hex()
PASSED = "0x" + keccak(text="Passed(address,int256)").hex()
STOPPED = "0x" + keccak(text="Stopped(uint8,uint8,int256)").hex()
TRIGGER_SEEN = "0x" + keccak(text="TriggerSeen(uint32,bool,uint64,uint256)").hex()
ORACLE_PX = c.PRECOMPILE["oraclePx"]
HIP3_BASE = 100_000  # CoreOps.HIP3_BASE: an asset's index among the oracle's prices is pxIndex(asset)


def exam_event_of(l: dict, t: int) -> dict | None:
    """One PaperExam log as the recount reads it, or None for one it does not."""
    if not l.get("topics"):
        return None  # an anonymous log is none of PaperExam's (review of #287)
    topic, data = l["topics"][0], bytes.fromhex(l["data"][2:])
    e = {"block": int(l["blockNumber"], 16), "index": int(l.get("logIndex", "0x0"), 16), "t": t, "tx": l["transactionHash"]}
    if topic == STARTED:
        return {**e, "name": "Started", "capital": abi_decode(["uint64"], data)[0]}
    if topic == sk.PLACED:
        return {**e, "name": "Placed", "asset": int(l["topics"][1], 16)}
    if topic == sk.FILLED:
        buy, sz, px, cost, late, source = abi_decode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"], data)
        return {**e, "name": "Filled", "asset": int(l["topics"][1], 16), "buy": bool(buy), "sz": int(sz), "px": int(px),
                "cost": int(cost), "late": int(late), "source": int(source)}
    if topic == DAY_SNAPSHOT:
        return {**e, "name": "DaySnapshot", "day": int(l["topics"][1], 16), "equity": abi_decode(["int256"], data)[0]}
    if topic == STEP_PASSED:
        return {**e, "name": "StepPassed", "equity": abi_decode(["int256"], data)[0]}
    if topic == PASSED:
        return {**e, "name": "Passed", "equity": abi_decode(["int256"], data)[0]}
    if topic == STOPPED:
        return {**e, "name": "Stopped", "status": int(l["topics"][1], 16),
                "reason": int(l["topics"][2], 16) if len(l["topics"]) > 2 else None,  # both are indexed; a short log is read as before
                "equity": abi_decode(["int256"], data)[0]}
    # A stop or a take (`setTrigger`, `clearTrigger`, `seeTrigger`, `fillTrigger`): the asset is indexed, `isTake` is the
    # first word of the data.
    if topic == sk.TRIGGER_SET:
        take, tpx, sz, armed = abi_decode(["bool", "uint64", "uint64", "uint256"], data)
        return {**e, "name": "TriggerSet", "asset": int(l["topics"][1], 16), "take": bool(take), "px": int(tpx),
                "sz": int(sz), "armedAt": int(armed)}
    if topic == sk.TRIGGER_CLEARED:
        return {**e, "name": "TriggerCleared", "asset": int(l["topics"][1], 16), "take": bool(abi_decode(["bool"], data)[0])}
    if topic == TRIGGER_SEEN:
        take, oracle_px, seen = abi_decode(["bool", "uint64", "uint256"], data)
        return {**e, "name": "TriggerSeen", "asset": int(l["topics"][1], 16), "take": bool(take), "oracle": int(oracle_px),
                "seenAt": int(seen)}
    if topic == sk.TRIGGER_FILLED:
        take, sz, tpx, cost, trigger_px, source = abi_decode(["bool", "uint64", "uint64", "uint256", "uint64", "uint8"], data)
        return {**e, "name": "TriggerFilled", "asset": int(l["topics"][1], 16), "take": bool(take), "sz": int(sz),
                "px": int(tpx), "cost": int(cost), "trigger_px": int(trigger_px), "source": int(source)}
    return None


def passed_accounts(exam_events: dict[str, list[dict]]) -> list[str]:
    """The exams and stages that passed, by their own events."""
    return [a for a, evs in exam_events.items() if any(e["name"] == "Passed" for e in evs)]


def read_daily(account: str, into: dict[str, int]) -> None:
    """The day's allowed loss of an exam or a stage, in bps: the first of its `rules()`. Left out where not read."""
    try:
        into[account] = int(c.call_view(account, "rules()", [], [], [EXAM_RULES])[0][0])
    except Exception as exc:
        core.log("rules_unread", account=account, error=str(exc)[:200])


def read_drawdown(account: str, kinds: dict[str, int], bps: dict[str, int]) -> None:
    """How an exam's drawdown line is drawn (`drawdownKind()`) and its allowed drawdown in bps (the second of `rules()`).
    An account from before the trailing kind has no getter and its line was from the start; one not read at all
    (the node, the rate) is left out, never taken for the start's."""
    try:
        bps[account] = int(c.call_view(account, "rules()", [], [], [EXAM_RULES])[0][1])
    except Exception as exc:
        core.log("rules_unread", account=account, error=str(exc)[:200])
        return
    try:
        kinds[account] = int(c.call_view(account, "drawdownKind()", [], [], ["uint8"])[0])
    except Exception as exc:
        if "revert" in str(exc).lower():
            kinds[account] = FROM_START
        else:
            core.log("not_read", address=account, call="drawdownKind()", error=str(exc)[:120])
            bps.pop(account, None)


def oracle_at(asset: int, block: int) -> int | None:
    """The chain's oracle price of `asset` as it stood in `block` (the precompile, called at that block), or None
    when the node keeps no state that far back."""
    index = asset - HIP3_BASE if asset >= HIP3_BASE else asset
    try:
        raw = c.rpc("eth_call", [{"to": ORACLE_PX, "data": "0x" + abi_encode(["uint32"], [index]).hex()}, hex(block)])
        px = abi_decode(["uint64"], bytes.fromhex(raw[2:]))[0]
        return int(px) or None
    except Exception:
        return None


def _addr(topic: str) -> str:
    return to_checksum_address("0x" + topic[-40:])


def logs(address, topics: list, lo: int, hi: int) -> list[dict]:
    out = []
    while lo <= hi:
        top = min(lo + core.MAX_LOG_WINDOW - 1, hi)
        out += c.rpc("eth_getLogs", [{"address": address, "topics": topics, "fromBlock": hex(lo), "toBlock": hex(top)}])
        lo = top + 1
    return out


def optional_view(pool: str, sig: str) -> int | None:
    """A uint256 getter a newer pool has and an older one does not: None where it reverts."""
    try:
        return int(core.view(pool, sig, "uint256"))
    except Exception:
        return None


def answers(addr: str, sig: str, out: list[str]) -> bool | None:
    """True if the getter answers, False if the call reverts (the contract has no such function), None if it could
    not be read at all (the node, the rate): a failed read is never taken for "no"."""
    try:
        c.call_view(addr, sig, [], [], out)
        return True
    except Exception as exc:
        if "revert" in str(exc).lower():
            return False
        core.log("not_read", address=addr, call=sig, error=str(exc)[:120])
        return None


def has_approvals(factory: str, pool: str) -> bool | None:
    """Whether the pool takes approvals, with a ceiling: its factory answers `PAYOUT_HARD()` (the ceiling, a constant
    of the factory whose clones all pools are) and the pool answers `approver()`. False only when the factory's call
    reverts; None when either was not read, or the two disagree (review of #283)."""
    hard = answers(factory, "PAYOUT_HARD()", ["uint32"])
    if hard is False:
        return False
    if hard is None:
        return None
    return True if answers(pool, "approver()", ["address"]) is True else None


def read_pay_cap(factory: str, pool: str) -> dict | bool | None:
    """The pool's daily limit on payouts as the contract answers it: the number of payouts (`PAYOUTS_PER_DAY()`) and the
    day (`PAYOUT_DAY()`) of the factory, and the pool's `payRoom()` and `nextPayAt()`. False where the factory has no such
    limit (its call reverts); None where a read failed for any other reason (the node, the rate), never taken for "no".
    This is the one place the limit is read: it is what the contract says, not a number kept here."""
    def read(to: str, sig: str, kind: str):
        try:
            return int(c.call_view(to, sig, [], [], [kind])[0])
        except Exception as exc:
            if "revert" in str(exc).lower():
                return False
            core.log("not_read", address=to, call=sig, error=str(exc)[:120])
            return None
    limit = read(factory, "PAYOUTS_PER_DAY()", "uint8")
    if limit is False or limit is None:
        return limit
    got = [read(factory, "PAYOUT_DAY()", "uint32"), read(pool, "payRoom()", "uint256"), read(pool, "nextPayAt()", "uint64")]
    if any(g is False or g is None for g in got):
        return None  # a factory that has the limit and a pool that does not answer it is not read, not "no limit"
    return {"limit": limit, "day": got[0], "room": got[1], "nextAt": got[2]}


def payout_times(factory: str) -> dict:
    """The factory's own times in seconds: `delay`, from a payout's claim to its `dueAt` (`PAYOUT_DELAY()`), and
    `hard`, the ceiling from its claim (`PAYOUT_HARD()`). None where one was not read or the factory has none: a
    review prints the contract's times or none (review of #283)."""
    out = {}
    for key, sig in (("delay", "PAYOUT_DELAY()"), ("hard", "PAYOUT_HARD()")):
        try:
            out[key] = int(c.call_view(factory, sig, [], [], ["uint32"])[0])
        except Exception as exc:
            core.log("not_read", address=factory, call=sig, error=str(exc)[:120])
            out[key] = None
    return out


def payout_struct(pool: str, pid: int) -> tuple:
    """`payout(id)` as the pool holds it: eight fields, the last `approved`; or the seven of a pool from before
    approvals, whose answer is a word shorter, with None for the eighth."""
    try:
        p = tuple(core.view(pool, "payout(uint256)", sk.PAYOUT, ["uint256"], [pid]))
        return p[:7] + (bool(p[7]),)
    except Exception:
        return tuple(core.view(pool, "payout(uint256)", PAYOUT_BEFORE_APPROVALS, ["uint256"], [pid])) + (None,)


def approver_of(pool: str) -> str | None:
    """The pool's approver, the zero address where none is named; None where it was not read."""
    try:
        return to_checksum_address(c.call_view(pool, "approver()", [], [], ["address"])[0])
    except Exception as exc:
        core.log("not_read", address=pool, call="approver()", error=str(exc)[:120])
        return None


def with_approver(takes: bool | None, pool: str) -> tuple[bool | None, str | None]:
    """Whether the pool takes approvals, and who approves on one that does. The approver not read, the pool is
    not read either: an approval nobody can send must not be offered."""
    approver = approver_of(pool) if takes is True else None
    return (None, None) if takes is True and approver is None else (takes, approver)


def read_chain(record: dict, prices: dict | None = None, venue: dict | None = None) -> dict:
    """Everything the review reads, from the deployment's block to the latest one. The oracle's prices around the
    fills of each payout still waiting are read from the chain at their blocks; `prices` ({"asset:block": px})
    stands in for those the node cannot give, and `venue` ({asset: {minute: px}}) is the venue's own prices,
    from a file, never fetched here."""
    factory, usdc, start = record["SimPoolFactory"], record["usdc"], int(record["block"])
    latest = int(c.rpc("eth_blockNumber"), 16)
    now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    pools = [to_checksum_address(p) for p in core.view(factory, "pools()", "address[]")]
    events = logs(pools, [[sk.EXAM_SOLD, sk.FUNDED, *PAYOUT_TRAIL, PAYOUT_WAITS_LIMIT]], start, latest) if pools else []
    sales = [sale_of(l) for l in events if l["topics"][0] == sk.EXAM_SOLD]
    funded = [funded_of(l) for l in events if l["topics"][0] == sk.FUNDED]
    payouts = [due_of(l) for l in events if l["topics"][0] == PAYOUT_DUE]
    # Each payout's life as the chain wrote it, in the order of the logs: what the review reports as its status.
    life: dict[tuple[str, int], list[str]] = {}
    for l in sorted((l for l in events if l["topics"][0] in PAYOUT_TRAIL),
                    key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16))):
        life.setdefault((to_checksum_address(l["address"]), int(l["topics"][1], 16)), []).append(PAYOUT_TRAIL[l["topics"][0]])
    for x in payouts:
        p = payout_struct(x["pool"], x["id"])
        x["state"], x["wasStopped"], x["approved"] = sk.PAYOUT_STATE[p[4]], bool(p[5]), p[7]
        x["trail"] = payout_trail(life.get((x["pool"], x["id"]), []))
    books = {pool: {"balance": c.call_view(usdc, "balanceOf(address)", ["address"], [pool], ["uint256"])[0],
                    **{k: core.view(pool, f"{k}()", "uint256") for k in ("owed", "held", "reserved")},
                    "sizes": [int(m[0]) for m in c.call_view(pool, "menu()", [], [], ["(uint64,uint64)[]"])[0]],
                    "approvals": has_approvals(factory, pool), "largestPayout": core.view(pool, "largestPayout()", "uint64"),
                    "staged": optional_view(pool, "staged()")}
             for pool in pools}
    for pool, b in books.items():
        b["approvals"], b["approver"] = with_approver(b["approvals"], pool)
    pool_of = {s["exam"]: s["pool"] for s in sales} | {f["stage"]: f["pool"] for f in funded}
    accounts = list(pool_of)
    status, done, unread = {}, {}, []
    for a, pool in pool_of.items():
        try:
            status[a] = sk.EXAM_STATUS[c.call_view(a, "result()", [], [], ["uint8", "address", "uint64", "uint64"])[0]]
            done[a] = bool(c.call_view(pool, "accountOf(address)", ["address"], [a], ["uint8", "uint8", "bool"])[2])
        except Exception as exc:
            core.log("account_unread", account=a, error=str(exc)[:200])
            unread.append(a)
    times: dict[int, int] = {}

    def time_of(block: int) -> int:
        if block not in times:
            times[block] = int(c.rpc("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)
        return times[block]

    # The daily limit: what the contract answers, the times of the pool's settlements (a payout paid or set aside uses a
    # place for a day) and the waits it wrote, for `payout_cap`.
    pay_cap = {pool: read_pay_cap(factory, pool) for pool in pools}
    in_order = sorted(events, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
    settled: dict[str, list[int]] = {pool: [] for pool in pools}
    waits_limit: dict[tuple[str, int], dict] = {}
    for l in in_order:
        pool_ = to_checksum_address(l["address"])
        if l["topics"][0] in (PAYOUT_PAID, PAYOUT_HELD):
            settled[pool_].append(time_of(int(l["blockNumber"], 16)))
        elif l["topics"][0] == PAYOUT_WAITS_LIMIT:
            waits_limit[(pool_, int(l["topics"][1], 16))] = {
                "nextAt": int(abi_decode(["uint64"], bytes.fromhex(l["data"][2:]))[0]), "time": time_of(int(l["blockNumber"], 16))}

    # Every account's own events, in one pass: the fills, and what the recount and the mirror need.
    exam_events: dict[str, list[dict]] = {a: [] for a in accounts}
    for i in range(0, len(accounts), chk.ADDRESS_BATCH):
        for l in logs(accounts[i:i + chk.ADDRESS_BATCH], [], start, latest):
            e = exam_event_of(l, time_of(int(l["blockNumber"], 16)))
            if e is not None:
                exam_events[to_checksum_address(l["address"])].append(e)
    fills, ended = [], {}
    for a, evs in exam_events.items():
        evs.sort(key=lambda e: (e["block"], e["index"]))
        closes = trigger_fills(evs)
        for k, e in enumerate(evs):
            if e["name"] == "Filled":
                fills.append({"account": a, "asset": e["asset"], "buy": e["buy"], "sz": e["sz"], "t": e["t"],
                              "tx": e["tx"], "late": e["late"], "via": closes.get(k)})
            elif e["name"] in ("Passed", "Stopped"):
                ended[a] = e["t"]
    # The oracle around each fill of the payouts still waiting (its block, the ones on each side, its order's) and
    # at each day's snapshot, and each such account's cost.
    oracle: dict[str, int] = dict(prices or {})
    cost_ppm: dict[str, int] = {}
    fill_window: dict[str, int] = {}
    daily_bps: dict[str, int] = {}
    drawdown_kind: dict[str, int] = {}
    drawdown_bps: dict[str, int] = {}
    stage_exam = {f["stage"]: f["exam"] for f in funded}
    for x in payouts:
        if x["state"] != "Due":
            continue
        for a in (x["account"], stage_exam.get(x["account"])):
            if not a or a not in exam_events:
                continue
            try:
                terms = c.call_view(a, "terms()", [], [], [sk.EXAM_TERMS])[0]
                cost_ppm[a], fill_window[a] = int(terms[5]), int(terms[7])
            except Exception as exc:
                core.log("terms_unread", account=a, error=str(exc)[:200])
            read_daily(a, daily_bps)
            read_drawdown(a, drawdown_kind, drawdown_bps)
            assets = {e["asset"] for e in exam_events[a] if e["name"] == "Filled"}
            wanted = set()
            for e in exam_events[a]:
                if e["name"] in ("Filled", "Placed"):
                    wanted |= {(e["asset"], e["block"] + d) for d in (-1, 0, 1)}
                if e["name"] == "Filled" and fill_window.get(a):
                    # Every block of the order's window, for the filler's choice (at most 60 blocks).
                    first = e["block"] - e["late"]
                    wanted |= {(e["asset"], first + k) for k in range(min(fill_window[a], 60))}
                elif e["name"] == "DaySnapshot":
                    wanted |= {(asset, e["block"]) for asset in assets}
            for asset, block in sorted(wanted):
                key = oracle_key(asset, block)
                if key not in oracle:
                    got = oracle_at(asset, block)
                    if got is not None:
                        oracle[key] = got
    # The day's allowed loss of every exam that passed too: the pool's count of passes in the shape of a coin flip.
    for a in passed_accounts(exam_events):
        if a not in daily_bps:
            read_daily(a, daily_bps)
    transfers = []
    for t in sorted({s["trader"] for s in sales}):
        word = "0x" + "00" * 12 + t[2:].lower()
        for l in logs(usdc, [TRANSFER, None, word], start, latest):
            transfers.append({"from": _addr(l["topics"][1]), "to": _addr(l["topics"][2]), "tx": l["transactionHash"],
                              "block": int(l["blockNumber"], 16)})
    transfers.sort(key=lambda t: t["block"])
    return {"now": now, "latest": latest, "pools": pools, "books": books, "sales": sales, "funded": funded,
            "payouts": payouts, "status": status, "done": done, "fills": fills, "transfers": transfers, "unread": unread,
            "exam_events": exam_events, "ended": ended, "oracle": oracle, "costPpm": cost_ppm, "venue": venue or {},
            "fillWindow": fill_window, "dailyLossBps": daily_bps, "drawdownKind": drawdown_kind,
            "maxDrawdownBps": drawdown_bps, "times": payout_times(factory),
            "payCap": {pool: cap for pool, cap in pay_cap.items() if cap is not None}, "settled": settled,
            "waitsLimit": waits_limit}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--pool", help="one pool; with --id, one payout")
    ap.add_argument("--id", type=int)
    ap.add_argument("--window", type=int, default=600, help="seconds two opposite fills may be apart")
    ap.add_argument("--size-tolerance", type=float, default=0.2)
    ap.add_argument("--wide", type=int, default=5, help="a sender that funded more buyers than this is not a link")
    ap.add_argument("--code-run", type=int, default=10, help="sales in a row from new wallets that make a code suspicious")
    ap.add_argument("--prices", help='a JSON file of oracle prices the node cannot give, {"asset:block": px}')
    ap.add_argument("--venue-prices", help="a JSON file of the venue's prices, {asset: {minute: px}}: Hyperliquid's "
                    "candleSnapshot (info API, interval 1m), each minute's close; read from the file, never fetched")
    ap.add_argument("--thresholds", help="a JSON file of the pass-rate thresholds: {passes: {window, yellow, red}, payouts: "
                    "{...}, payoutsToIncome: {days, yellow, red}, minFinished}; without it, the CFO's of 13 November")
    ap.add_argument("--json", action="store_true", help=f"one {SCHEMA} document a payout, as a list")
    ap.add_argument("--out", help=f"a directory to write each payout's {SCHEMA} document to, as <pool>-<id>.json")
    args = ap.parse_args(argv)
    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    load = lambda path: json.loads(pathlib.Path(path).read_text()) if path else None
    chain = read_chain(record, load(args.prices), load(args.venue_prices))
    p = Params(args.window, args.size_tolerance, args.wide, args.code_run)
    pool = to_checksum_address(args.pool) if args.pool else None
    todo = [x for x in chain["payouts"]
            if (pool is None or x["pool"] == pool) and (args.id is None or x["id"] == args.id)
            and (args.id is not None or x["state"] == "Due")]
    th = load(args.thresholds)
    reviews = [review(chain, x["pool"], x["id"], p, th) for x in todo]
    cards = [card(r, chain) for r in reviews]
    if args.out:
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for k in cards:
            (out / f"{k['pool']}-{k['id']}.json").write_text(json.dumps(k, indent=2) + "\n")
    if args.json:
        print(json.dumps(cards, indent=2))
    else:
        print("\n\n".join(summary(r) for r in reviews) or "no payout waiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
