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
TRANSFER = chk.TRANSFER
PAYOUT_BPS = 800
BPS = 10_000
# The pool's call that lets payouts through once their 72 hours are over, from its approver. Not on main yet: the
# name and the arguments are the design's (approval with a ceiling, 13 November), and a test holds them to the
# source once the pool has it.
APPROVE: str | None = "approvePayouts(uint256[])"
APPROVE_TYPES: list[str] = ["uint256[]"]
PAYOUT_DELAY = 72 * 3600
# Past this from its claim a payout is paid unapproved, unless it was stopped (the same design's ceiling).
PAYOUT_HARD = 7 * 24 * 3600


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
            if abs(after) > abs(before):
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
        m = mirror(mine, fills, chain["now"], p.mirror_ratio, chain.get("ended", {}).get(other))
        if m["share"] >= p.mirror_share and m["mirrored"] >= p.mirror_min_s:
            out.append({"account": other, "trader": who.get(other), "share": round(m["share"], 4),
                        "mirrored": m["mirrored"], "exposed": m["exposed"], "assets": m["assets"],
                        "examples": m["examples"]})
    return sorted(out, key=lambda x: (-x["share"], x["account"]))


# ── the result counted again, outside the contract ───────────────────────────────────

ZERO_ADDRESS = "0x" + "00" * 20
PPM = 1_000_000
ANALYSIS = "<keccak256 of the published analysis>"
SOURCE_ORACLE, SOURCE_BOOK, SOURCE_FALLBACK = 0, 1, 2


def oracle_key(asset: int, block: int) -> str:
    return f"{asset}:{block}"


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
    said as a profile, not as a suspicion."""
    ev = chain.get("exam_events", {}).get(account)
    out = {"read": ev is not None, "capital": None, "fills": 0, "days": 0, "passed": False,
           "checked": {"results": 0, "snapshots": 0, "unchecked": 0, "fills": 0}, "flags": [], "moves": [],
           "profile": None}
    if ev is None:
        return out
    oracle = chain.get("oracle", {})
    venue = chain.get("venue", {})
    cost_ppm = chain.get("costPpm", {}).get(account)
    flags = out["flags"]
    capital = cash = 0
    pos: dict[int, int] = {}
    placed: int | None = None
    days: set[int] = set()

    def px(asset: int, block: int) -> int | None:
        return oracle.get(oracle_key(asset, block))

    def result(name: str, e: dict) -> None:
        out["checked"]["results"] += 1
        if any(pos.values()) or e["equity"] != cash:
            flags.append({"what": "result", "tx": e["tx"], "block": e["block"],
                          "detail": f"{name} wrote {e['equity']}, the fills give {cash}" + (" and a position open" if any(pos.values()) else "")})

    for e in ev:
        name = e["name"]
        if name == "Started":
            capital = cash = e["capital"]
            out["capital"] = capital
        elif name == "Placed":
            placed = e["block"]
        elif name == "Filled":
            delta = e["sz"] if e["buy"] else -e["sz"]
            cash -= delta * e["px"] + e["cost"]
            pos[e["asset"]] = pos.get(e["asset"], 0) + delta
            out["fills"] += 1
            days.add(e["t"] // 86_400)
            ntl = e["sz"] * e["px"]
            if cost_ppm is not None and e["cost"] != (ntl * cost_ppm + PPM - 1) // PPM:
                flags.append({"what": "cost", "tx": e["tx"], "block": e["block"],
                              "detail": f"cost {e['cost']}, the terms give {(ntl * cost_ppm + PPM - 1) // PPM}"})
            o = px(e["asset"], e["block"])
            if o is not None:
                out["checked"]["fills"] += 1
                better = e["px"] < o if e["buy"] else e["px"] > o
                if better:
                    flags.append({"what": "better-than-oracle", "tx": e["tx"], "block": e["block"],
                                  "detail": f"{'bought' if e['buy'] else 'sold'} at {e['px']}, the oracle {o}"})
                elif e["source"] != SOURCE_BOOK and e["px"] != o:
                    flags.append({"what": "not-the-oracle", "tx": e["tx"], "block": e["block"],
                                  "detail": f"filled at {e['px']} as the oracle's, the oracle {o}"})
                prev, nxt = px(e["asset"], e["block"] - 1), px(e["asset"], e["block"] + 1)
                if prev is not None and nxt is not None:
                    mid = (prev + nxt) / 2
                    if abs(o - mid) * BPS > p.outlier_bps * mid and abs(prev - nxt) * BPS <= p.outlier_bps * mid:
                        flags.append({"what": "oracle-outlier", "tx": e["tx"], "block": e["block"],
                                      "detail": f"oracle {o} against {prev} and {nxt} in the blocks around it"})
                v = venue.get(str(e["asset"]), {}).get(str(e["t"] // 60 * 60))
                if v is not None and abs(o - v) * BPS > p.venue_bps * v:
                    flags.append({"what": "oracle-off-venue", "tx": e["tx"], "block": e["block"],
                                  "detail": f"oracle {o}, the venue {v} that minute"})
                at_order = px(e["asset"], placed) if placed is not None else None
                if at_order:
                    # In the trader's favour when the price moved toward their side while the order waited.
                    moved = (at_order - o if e["buy"] else o - at_order) * BPS / at_order
                    out["moves"].append({"tx": e["tx"], "bps": round(moved, 1)})
            placed = None
        elif name == "DaySnapshot":
            open_ = {a: q for a, q in pos.items() if q}
            marks = {a: px(a, e["block"]) for a in open_}
            if any(m is None for m in marks.values()):
                out["checked"]["unchecked"] += 1
                continue
            out["checked"]["snapshots"] += 1
            want = cash + sum(q * marks[a] for a, q in open_.items())
            if e["equity"] != want:
                flags.append({"what": "snapshot", "tx": e["tx"], "block": e["block"],
                              "detail": f"day {e['day']} wrote {e['equity']}, the fills and the oracle give {want}"})
        elif name == "StepPassed":
            result("StepPassed", e)
            cash, pos, days = capital, {}, set()
        elif name == "Passed":
            result("Passed", e)
            out["passed"] = True
    out["days"] = len(days)
    if out["passed"] and size is not None and size == largest and (out["fills"] <= p.few_fills or out["days"] <= p.few_days):
        out["profile"] = (f"passed in {out['fills']} fills over {out['days']} days on the largest size: a pass that "
                          "could be luck, not a sign of cheating")
    return out


# ── the review of one payout ─────────────────────────────────────────────────────────

class Params:
    def __init__(self, window_s: int = 600, tolerance: float = 0.2, wide: int = 5, code_run: int = 10,
                 idle_share: float = 0.5, min_fills: int = 5, mirror_ratio: float = 0.5, mirror_share: float = 0.6,
                 mirror_min_s: int = 3_600, outlier_bps: int = 100, venue_bps: int = 100, few_fills: int = 3,
                 few_days: int = 2):
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
        # The recount: an oracle price 1 % off both neighbours that agree within 1 % is an outlier, as is one 1 %
        # off the venue's; a pass in 3 fills or 2 days at most on the largest size is said as a profile.
        self.outlier_bps = outlier_bps
        self.venue_bps = venue_bps
        self.few_fills = few_fills
        self.few_days = few_days


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


def link_txs(trader: str, other: str | None, chain: dict) -> list[str]:
    """The links a stop of reason 2 writes between the trader and the paired account's trader, the
    pools' own payouts left out (they pay every trader)."""
    if other is None:
        return []
    pools = set(chain["pools"])
    return chk.links([t for t in chain["transfers"] if t["from"] not in pools], trader, other)


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
    exams = stages = worst = 0
    funded_exams = {f["exam"] for f in chain["funded"]}
    stage_accounts = {f["stage"] for f in chain["funded"]}
    for a, size in lines.items():
        # Running, or passed and not yet funded or claimed; an exam funded runs on as its stage.
        if chain["done"].get(a) or a in funded_exams or chain["status"].get(a) not in ("Active", "Passed"):
            continue
        if a in stage_accounts:
            stages += 1
        else:
            exams += 1
        worst += payout_of(size)
    book = chain["books"][pool]
    free = book["balance"] - book["owed"] - book["held"] - book["reserved"]
    return {"exams": exams, "stages": stages, "worst": worst, "free": free, "enough": free >= worst}


def stop_call(pool: str, pid: int, reason: int, paired: str, trades: list[str], links: list[str]) -> dict:
    args = [pid, reason, paired, [bytes.fromhex(t[2:]) for t in trades], [bytes.fromhex(t[2:]) for t in links]]
    return {"kind": "stop", "to": pool, "call": chk.STOP, "args": [pid, reason, paired, trades, links],
            "data": "0x" + c.encode_call(chk.STOP, chk.STOP_TYPES, args).hex()}


def approve_call(pool: str, pid: int) -> dict:
    if APPROVE is None:
        return {"kind": "approve", "to": pool, "call": None, "args": [pid], "data": None,
                "note": "the pool has no approval call yet"}
    return {"kind": "approve", "to": pool, "call": APPROVE, "args": [[pid]],
            "data": "0x" + c.encode_call(APPROVE, APPROVE_TYPES, [[pid]]).hex()}


def review(chain: dict, pool: str, pid: int, p: Params | None = None) -> dict:
    p = p or Params()
    pay = next(x for x in chain["payouts"] if x["pool"] == pool and x["id"] == pid)
    who = owners(chain)
    trader, stage = pay["trader"], pay["account"]
    exam = next((f["exam"] for f in chain["funded"] if f["stage"] == stage), None)
    sale = next((s for s in chain["sales"] if s["exam"] == exam), None)
    out = {"pool": pool, "id": pid, "trader": trader, "stage": stage, "exam": exam, "amount": pay["amount"],
           "claimedAt": pay["dueAt"] - PAYOUT_DELAY, "dueAt": pay["dueAt"],
           "hardAt": pay["dueAt"] - PAYOUT_DELAY + PAYOUT_HARD, "state": pay["state"], "size": sale["size"] if sale else None,
           "fills": {"stage": sum(f["account"] == stage for f in chain["fills"]),
                     "exam": sum(f["account"] == exam for f in chain["fills"])}}
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
            links = link_txs(trader, who.get(paired), chain)
            reason, paired, trades, links = chk.finding(found, links)
            stop = {"on": part, "by": "pair", "reason": reason, "paired": paired, "pairedTrader": who.get(paired),
                    "trades": trades, "links": links}
            break

    # Mirrored accounts: positions held against positions for most of the time, whatever the fills' timing and
    # sizes. With no pair, a mirror whose examples carry two opposite fills is the stop: reason 1, "opposite
    # positions with `paired`", or 2 with the wallets linked.
    mirrors = {"stage": mirrors_of(stage, chain, who, p), "exam": mirrors_of(exam, chain, who, p) if exam else []}
    out["mirrors"] = mirrors
    for part in ("stage", "exam"):
        if stop:
            break
        for m in mirrors[part]:
            if m["examples"]:
                trades = list(dict.fromkeys(tx for ex in m["examples"] for tx in ex))
                links = link_txs(trader, m["trader"], chain)
                stop = {"on": part, "by": "mirror", "reason": 2 if links else 1, "paired": m["account"],
                        "pairedTrader": m["trader"], "trades": trades, "links": links}
                break
    out["stop"] = stop

    # The result counted again, for the stage and for the exam it came from.
    sizes = chain["books"].get(pool, {}).get("sizes") or [x["size"] for x in chain["sales"] if x["pool"] == pool]
    largest = max(sizes, default=None)
    out["recount"] = {part: recount(account, chain, p, out["size"], largest) if account else None
                      for part, account in (("stage", stage), ("exam", exam))}

    links = linked(trader, chain, p)
    out["links"] = links
    mine_late = [f["late"] for f in chain["fills"] if who.get(f["account"]) == trader]
    others_late = [f["late"] for f in chain["fills"] if who.get(f["account"]) not in (trader, None)]
    out["late"] = {"trader": {"n": len(mine_late), "median": quantile(mine_late, 0.5), "max": max(mine_late, default=0)},
                   "others": {"n": len(others_late), "median": quantile(others_late, 0.5),
                              "p90": quantile(others_late, 0.9)},
                   "outlier": late_outlier(mine_late, others_late, p.min_fills)}
    out["code"] = code_review(pool, sale["code"], chain, who, p) if sale else None
    out["running"] = running(pool, chain)

    found: list[tuple[str, str]] = []  # (kind, text), in the order a page lists them
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
    unread = [a for a in (stage, exam) if a and (a in chain.get("unread", [])
                                                 or ("exam_events" in chain and a not in chain["exam_events"]))]
    if unread:
        found.append(("unread", "not read: " + ", ".join(unread)))
    out["verdict"] = "suspicious" if found else "clean"
    out["reasons"] = [t for _, t in found]
    out["kinds"] = [k for k, _ in found]
    # What whoever signs is offered (review of #287): a stop where there is one; nothing to approve where the
    # result did not count up, only the draft of a reason-3 stop, until the analysis is written or the ceiling
    # comes; the approval as a choice where something else is suspicious; the approval where all is clean.
    if stop:
        out["action"] = "stop"
        out["tx"] = stop_call(pool, pid, stop["reason"], stop["paired"], stop["trades"], stop["links"])
    elif "recount" in out["kinds"]:
        out["action"] = "hold"
        out["tx"] = None
    else:
        out["action"] = "decide" if found else "approve"
        out["tx"] = approve_call(pool, pid)
        if found:
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


SCHEMA = "payout-review/2"


def card(r: dict, chain: dict) -> dict:
    """The review as one JSON document a page reads (`app/lib/operator.js`, `reviewFor`). The schema is
    fixed: fields are added under a new SCHEMA, never renamed. /2 added `evidence.mirrors`,
    `evidence.recount` and `stopDraft`, and the reason kinds `mirror` and `recount`. Amounts are strings of USDC units (6
    decimals), since a JSON number loses digits past 2**53; times are seconds on the chain's clock."""
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
            "recount": r["recount"],
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
        "readAt": {"block": chain["latest"], "time": chain["now"]},
    }


def summary(r: dict) -> str:
    """The review in a few lines of text."""
    lines = [f"payout {r['id']} of pool {r['pool']}: {r['amount'] / 1e6:,.2f} USDC to {r['trader']}, due {r['dueAt']}, paid unapproved at {r['hardAt']}",
             f"  stage {r['stage']} ({r['fills']['stage']} fills), exam {r['exam']} ({r['fills']['exam']} fills)"]
    for part in ("stage", "exam"):
        for x in r["pairs"][part]:
            lines.append(f"  pair on the {part}: {x['mine']} against {x['other']} of {x['account']} (trader {x['trader']})")
    for part in ("stage", "exam"):
        for m in r["mirrors"][part]:
            lines.append(f"  mirrored on the {part}: {m['account']} (trader {m['trader']}) for {round(m['share'] * 100)} % "
                         f"of {m['exposed']} s, assets {m['assets']}")
        rc = r["recount"][part]
        if rc and rc["read"]:
            c_ = rc["checked"]
            lines.append(f"  recount of the {part}: {c_['results']} results, {c_['snapshots']} snapshots, {c_['fills']} fills "
                         f"checked, {c_['unchecked']} snapshots without prices; "
                         + ("; ".join(f"{f['what']} at {f['tx']}" for f in rc["flags"]) or "all count up"))
            if rc["profile"]:
                lines.append(f"  profile: {rc['profile']}")
    for l in r["links"]:
        lines.append(f"  linked to {l['trader']}" + (f" through {l['sender']}" if l["sender"] else " directly")
                     + (" (wide sender, not counted)" if l["wide"] else "") + ": " + ", ".join(l["txs"]))
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
    line, size, price, _, _, code = abi_decode(["uint256", "uint64", "uint64", "uint256", "bool", "bytes32"],
                                               bytes.fromhex(l["data"][2:]))
    return {"pool": to_checksum_address(l["address"]), "exam": _addr(l["topics"][1]), "trader": _addr(l["topics"][2]),
            "partner": _addr(l["topics"][3]), "line": int(line), "size": int(size), "price": int(price),
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
ORACLE_PX = c.PRECOMPILE["oraclePx"]
HIP3_BASE = 100_000  # CoreOps.HIP3_BASE: an asset's index among the oracle's prices is pxIndex(asset)


def exam_event_of(l: dict, t: int) -> dict | None:
    """One PaperExam log as the recount reads it, or None for one it does not."""
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
        return {**e, "name": "Stopped", "status": int(l["topics"][1], 16), "equity": abi_decode(["int256"], data)[0]}
    return None


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


def read_chain(record: dict, prices: dict | None = None, venue: dict | None = None) -> dict:
    """Everything the review reads, from the deployment's block to the latest one. The oracle's prices around the
    fills of each payout still waiting are read from the chain at their blocks; `prices` ({"asset:block": px})
    stands in for those the node cannot give, and `venue` ({asset: {minute: px}}) is the venue's own prices,
    from a file, never fetched here."""
    factory, usdc, start = record["SimPoolFactory"], record["usdc"], int(record["block"])
    latest = int(c.rpc("eth_blockNumber"), 16)
    now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    pools = [to_checksum_address(p) for p in core.view(factory, "pools()", "address[]")]
    events = logs(pools, [[sk.EXAM_SOLD, sk.FUNDED, PAYOUT_DUE]], start, latest) if pools else []
    sales = [sale_of(l) for l in events if l["topics"][0] == sk.EXAM_SOLD]
    funded = [funded_of(l) for l in events if l["topics"][0] == sk.FUNDED]
    payouts = [due_of(l) for l in events if l["topics"][0] == PAYOUT_DUE]
    for x in payouts:
        p = core.view(x["pool"], "payout(uint256)", sk.PAYOUT, ["uint256"], [x["id"]])
        x["state"], x["wasStopped"] = sk.PAYOUT_STATE[p[4]], bool(p[5])
    books = {pool: {"balance": c.call_view(usdc, "balanceOf(address)", ["address"], [pool], ["uint256"])[0],
                    **{k: core.view(pool, f"{k}()", "uint256") for k in ("owed", "held", "reserved")},
                    "sizes": [int(m[0]) for m in c.call_view(pool, "menu()", [], [], ["(uint64,uint64)[]"])[0]]}
             for pool in pools}
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
        for e in evs:
            if e["name"] == "Filled":
                fills.append({"account": a, "asset": e["asset"], "buy": e["buy"], "sz": e["sz"], "t": e["t"],
                              "tx": e["tx"], "late": e["late"]})
            elif e["name"] in ("Passed", "Stopped"):
                ended[a] = e["t"]
    # The oracle around each fill of the payouts still waiting (its block, the ones on each side, its order's) and
    # at each day's snapshot, and each such account's cost.
    oracle: dict[str, int] = dict(prices or {})
    cost_ppm: dict[str, int] = {}
    stage_exam = {f["stage"]: f["exam"] for f in funded}
    for x in payouts:
        if x["state"] != "Due":
            continue
        for a in (x["account"], stage_exam.get(x["account"])):
            if not a or a not in exam_events:
                continue
            try:
                cost_ppm[a] = int(c.call_view(a, "terms()", [], [], [sk.EXAM_TERMS])[0][5])
            except Exception as exc:
                core.log("terms_unread", account=a, error=str(exc)[:200])
            assets = {e["asset"] for e in exam_events[a] if e["name"] == "Filled"}
            wanted = set()
            for e in exam_events[a]:
                if e["name"] in ("Filled", "Placed"):
                    wanted |= {(e["asset"], e["block"] + d) for d in (-1, 0, 1)}
                elif e["name"] == "DaySnapshot":
                    wanted |= {(asset, e["block"]) for asset in assets}
            for asset, block in sorted(wanted):
                key = oracle_key(asset, block)
                if key not in oracle:
                    got = oracle_at(asset, block)
                    if got is not None:
                        oracle[key] = got
    transfers = []
    for t in sorted({s["trader"] for s in sales}):
        word = "0x" + "00" * 12 + t[2:].lower()
        for l in logs(usdc, [TRANSFER, None, word], start, latest):
            transfers.append({"from": _addr(l["topics"][1]), "to": _addr(l["topics"][2]), "tx": l["transactionHash"],
                              "block": int(l["blockNumber"], 16)})
    transfers.sort(key=lambda t: t["block"])
    return {"now": now, "latest": latest, "pools": pools, "books": books, "sales": sales, "funded": funded,
            "payouts": payouts, "status": status, "done": done, "fills": fills, "transfers": transfers, "unread": unread,
            "exam_events": exam_events, "ended": ended, "oracle": oracle, "costPpm": cost_ppm, "venue": venue or {}}


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
    reviews = [review(chain, x["pool"], x["id"], p) for x in todo]
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
