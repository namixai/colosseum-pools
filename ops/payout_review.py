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
from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from ops import sim_check as chk
from ops import sim_keeper as sk
from spike.hlspike import common as c

PAYOUT_DUE = "0x" + keccak(text="PayoutDue(uint256,address,address,uint64,uint64)").hex()
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
# `payout(id)` of a pool from before approvals: the same fields without the eighth, `approved`.
PAYOUT_BEFORE_APPROVALS = "(address,address,uint64,uint64,uint8,bool,uint64)"
ZERO_ADDRESS = "0x" + "00" * 20
NO_APPROVER = ("the pool names no approver: the payout goes out unapproved at its ceiling, unless it is stopped; "
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


# ── the review of one payout ─────────────────────────────────────────────────────────

class Params:
    def __init__(self, window_s: int = 600, tolerance: float = 0.2, wide: int = 5, code_run: int = 10,
                 idle_share: float = 0.5, min_fills: int = 5):
        self.window_s = window_s
        self.tolerance = tolerance
        self.wide = wide
        self.code_run = code_run
        self.idle_share = idle_share
        self.min_fills = min_fills


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


def approve_call(pool: str, pid: int, approvals: bool = True, approver: str | None = None, approved: bool = False) -> dict:
    """The approval, only where the pool would take it (review of #283). A pool without approvals pays at 72 hours
    by itself, and an approval sent to it would revert. A payout approved already reverts a second approval
    (`BadPayout`), and a pool whose approver is the zero address has nobody to send one (`NotApprover`)."""
    none = {"kind": "none", "to": pool, "call": None, "args": [], "data": None}
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


def review(chain: dict, pool: str, pid: int, p: Params | None = None) -> dict:
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
    approved_unread = approvals is True and approved is None
    if times_unread or approved_unread:
        approvals = None
    who = owners(chain)
    trader, stage = pay["trader"], pay["account"]
    exam = next((f["exam"] for f in chain["funded"] if f["stage"] == stage), None)
    sale = next((s for s in chain["sales"] if s["exam"] == exam), None)
    out = {"pool": pool, "id": pid, "trader": trader, "stage": stage, "exam": exam, "amount": pay["amount"],
           "claimedAt": pay["dueAt"] - delay if delay is not None else None, "dueAt": pay["dueAt"],
           "hardAt": pay["dueAt"] - delay + hard if approvals is True else None, "approvals": approvals,
           "approver": approver if approvals is True else None, "approved": bool(approved) if approvals is True else None,
           "state": pay["state"], "size": sale["size"] if sale else None,
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
            links = link_txs(trader, who.get(paired), chain, p)
            reason, paired, trades, links = chk.finding(found, links)
            stop = {"on": part, "reason": reason, "paired": paired, "pairedTrader": who.get(paired),
                    "trades": trades, "links": links}
            break
    out["stop"] = stop

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

    reasons = []
    if stop:
        reasons.append(f"pair on the {stop['on']} with {stop['paired']}" + (" and linked wallets" if stop["reason"] == 2 else ""))
    counted = [l for l in links if not l["wide"]]
    if counted:
        reasons.append("wallet linked to " + ", ".join(sorted({l["trader"] for l in counted})))
    if out["late"]["outlier"]:
        reasons.append("fills later than everyone else's")
    if out["code"] and out["code"]["flag"]:
        reasons.append(f"code {out['code']['code'][:10]}: {out['code']['run']} sales in a row from new wallets")
    unread = [a for a in (stage, exam) if a and a in chain.get("unread", [])]
    if times_unread:
        unread.append("the factory's delay and ceiling")
    elif approved_unread:
        unread.append("whether the payout is approved")
    elif approvals is None:
        unread.append("whether the pool takes approvals")
    if unread:
        reasons.append("not read: " + ", ".join(unread))
    out["verdict"] = "suspicious" if reasons else "clean"
    out["reasons"] = reasons
    # A stop only for a payout the pool would still stop (review of #283): waiting and never stopped, and in time
    # by the pool's own rule. A pool with approvals stops until it is paid; one without, until 5 minutes before
    # `dueAt`, as the checker sends it.
    waiting = pay["state"] == "Due" and not pay.get("wasStopped")
    in_time = approvals is True or chain["now"] + chk.MARGIN_S < pay["dueAt"]
    out["stoppable"] = waiting and in_time
    if approvals is None:
        # Not read whether the pool takes approvals: no call and no word on when it pays, either way it could be wrong.
        out["stoppable"] = None
        out["tx"] = {"kind": "none", "to": pool, "call": None, "args": [], "data": None,
                     "note": ("not read the factory's delay and ceiling" if times_unread else
                              "not read whether the payout is approved" if approved_unread else
                              "not read whether the pool takes approvals") + ": nothing to sign until it is"}
    elif stop and not out["stoppable"]:
        why = (f"it is {pay['state'].lower()}" if pay["state"] != "Due" else
               "it was stopped once already" if pay.get("wasStopped") else "its time to stop is over")
        out["tx"] = {"kind": "none", "to": pool, "call": None, "args": [], "data": None,
                     "note": f"the pool would refuse a stop now: {why}"}
    elif stop:
        out["tx"] = stop_call(pool, pid, stop["reason"], stop["paired"], stop["trades"], stop["links"])
    else:
        out["tx"] = approve_call(pool, pid, approvals, approver, approved)
        if reasons and out["tx"]["kind"] == "approve":
            out["tx"]["note"] = "no pair, so the pool takes no stop: whoever signs decides"
    return out


SCHEMA = "payout-review/1"


def card(r: dict, chain: dict) -> dict:
    """The review as one JSON document a page reads (`app/lib/operator.js`, `reviewFor`). The schema is
    fixed: fields are added under a new SCHEMA, never renamed. Amounts are strings of USDC units (6
    decimals), since a JSON number loses digits past 2**53; times are seconds on the chain's clock."""
    kinds = []
    if r["stop"]:
        kinds.append("pair")
    if any(not l["wide"] for l in r["links"]):
        kinds.append("link")
    if r["late"]["outlier"]:
        kinds.append("late")
    if r["code"] and r["code"]["flag"]:
        kinds.append("code")
    if any(x.startswith("not read") for x in r["reasons"]):
        kinds.append("unread")
    tx = r["tx"]
    g = r["running"]
    return {
        "schema": SCHEMA,
        "pool": r["pool"], "id": r["id"], "trader": r["trader"], "stage": r["stage"], "exam": r["exam"],
        "amount": str(r["amount"]),
        "claimedAt": r["claimedAt"], "dueAt": r["dueAt"], "hardAt": r["hardAt"],
        "verdict": r["verdict"],
        "reasons": [{"kind": k, "text": t} for k, t in zip(kinds, r["reasons"])],
        "evidence": {
            "pairs": r["pairs"]["stage"] + r["pairs"]["exam"],
            "stop": r["stop"],
            "links": r["links"],
            "late": r["late"],
            "code": r["code"],
            "running": {"exams": g["exams"], "stages": g["stages"], "worst": str(g["worst"]), "free": str(g["free"]),
                        "enough": g["enough"]},
        },
        "call": {"kind": tx["kind"], "to": tx["to"], "method": tx["call"].split("(")[0] if tx["call"] else None,
                 "signature": tx["call"], "args": tx["args"], "data": tx["data"], "note": tx.get("note")},
        "readAt": {"block": chain["latest"], "time": chain["now"]},
    }


def summary(r: dict) -> str:
    """The review in a few lines of text."""
    # Three answers here too (review of #283): a ceiling, no approval at all, or not read, which says no time.
    when = (", approved: paid from then" if r["approvals"] is True and r["approved"] else
            f", paid unapproved at {r['hardAt']}" if r["approvals"] is True else
            ", paid by itself then" if r["approvals"] is False else ", not read when it is paid")
    lines = [f"payout {r['id']} of pool {r['pool']}: {r['amount'] / 1e6:,.2f} USDC to {r['trader']}, due {r['dueAt']}" + when,
             f"  stage {r['stage']} ({r['fills']['stage']} fills), exam {r['exam']} ({r['fills']['exam']} fills)"]
    for part in ("stage", "exam"):
        for x in r["pairs"][part]:
            lines.append(f"  pair on the {part}: {x['mine']} against {x['other']} of {x['account']} (trader {x['trader']})")
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
    tx = r["tx"]
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


def read_chain(record: dict) -> dict:
    """Everything the review reads, from the deployment's block to the latest one."""
    factory, usdc, start = record["SimPoolFactory"], record["usdc"], int(record["block"])
    latest = int(c.rpc("eth_blockNumber"), 16)
    now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    pools = [to_checksum_address(p) for p in core.view(factory, "pools()", "address[]")]
    events = logs(pools, [[sk.EXAM_SOLD, sk.FUNDED, PAYOUT_DUE]], start, latest) if pools else []
    sales = [sale_of(l) for l in events if l["topics"][0] == sk.EXAM_SOLD]
    funded = [funded_of(l) for l in events if l["topics"][0] == sk.FUNDED]
    payouts = [due_of(l) for l in events if l["topics"][0] == PAYOUT_DUE]
    for x in payouts:
        p = payout_struct(x["pool"], x["id"])
        x["state"], x["wasStopped"], x["approved"] = sk.PAYOUT_STATE[p[4]], bool(p[5]), p[7]
    books = {pool: {"balance": c.call_view(usdc, "balanceOf(address)", ["address"], [pool], ["uint256"])[0],
                    **{k: core.view(pool, f"{k}()", "uint256") for k in ("owed", "held", "reserved")},
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

    fills = []
    for i in range(0, len(accounts), chk.ADDRESS_BATCH):
        for l in logs(accounts[i:i + chk.ADDRESS_BATCH], [sk.FILLED], start, latest):
            fills.append(filled_of(l, time_of(int(l["blockNumber"], 16))))
    transfers = []
    for t in sorted({s["trader"] for s in sales}):
        word = "0x" + "00" * 12 + t[2:].lower()
        for l in logs(usdc, [TRANSFER, None, word], start, latest):
            transfers.append({"from": _addr(l["topics"][1]), "to": _addr(l["topics"][2]), "tx": l["transactionHash"],
                              "block": int(l["blockNumber"], 16)})
    transfers.sort(key=lambda t: t["block"])
    return {"now": now, "latest": latest, "pools": pools, "books": books, "sales": sales, "funded": funded,
            "payouts": payouts, "status": status, "done": done, "fills": fills, "transfers": transfers, "unread": unread,
            "times": payout_times(factory)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--pool", help="one pool; with --id, one payout")
    ap.add_argument("--id", type=int)
    ap.add_argument("--window", type=int, default=600, help="seconds two opposite fills may be apart")
    ap.add_argument("--size-tolerance", type=float, default=0.2)
    ap.add_argument("--wide", type=int, default=5, help="a sender that funded more buyers than this is not a link")
    ap.add_argument("--code-run", type=int, default=10, help="sales in a row from new wallets that make a code suspicious")
    ap.add_argument("--json", action="store_true", help=f"one {SCHEMA} document a payout, as a list")
    ap.add_argument("--out", help=f"a directory to write each payout's {SCHEMA} document to, as <pool>-<id>.json")
    args = ap.parse_args(argv)
    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    chain = read_chain(record)
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
