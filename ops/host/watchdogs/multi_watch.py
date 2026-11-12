#!/usr/bin/env python3
"""Watchdog for what the keeper of a MultiPool is bound to do (ops/multi_keeper.py), and on time. Run on the pools
host, for an instance whose record names a MultiPool. Public chain reads only, as keeper_watch.py: no access to the
keeper's state or its key.

The rule it was written for: a running paper exam's daily loss limit is measured from its snapshot of the day,
`checkpoint()`, which anyone may call in the first fifteen minutes of a UTC day and nobody after. A keeper that
misses it leaves the snapshot to whoever it suits that day, the trader included. So, over LIVE accounts:

  1. A paper exam `Active` (and held by the pool as sold) whose `day()` is not today once the day's window is
     over: "the keeper did not take the day's snapshot". The window cannot be had back that day; the finding
     stands until the next window, and the flag clears at the first run that no longer sees it.

  2. A paper exam `Active` past its deadline plus 15 minutes, or past its idle limit (`terms().maxIdle` after
     `lastFillAt()`) plus 15 minutes: "the keeper did not expire it".

  3. A paper exam `Active` whose `violation()` is not none: "the keeper did not breach it". The chain records
     no time for a violation, so it is reported on every run that sees it.

  4. A funded seat (the pool's `seatInfo`, state Funded) whose account is not stopped and whose `violation()` is
     not none, or whose `day()` is not today once the window is over: the same two, for the seat's account.

THE EXAMS ARE FOUND WITHOUT LOGS. The pool keeps no list of its exams, and their ExamSold events would have to be
read from the pool's first block, which this host's node allowance does not cover. Each exam is a clone the pool
made with CREATE, so its address follows from the pool's address and its nonce: every address the pool made is
computed from `eth_getTransactionCount` of the pool, and the pool's `examInfo` says which of them are its exams and
which are still sold. A seat the pool made is not an exam, and examInfo says so.

A FAILURE OF THE NODE IS NOT A VIOLATION: exit 3, "could not check", as keeper_watch.py.

The pool comes from the deployment's env file (`COLOSSEUM_MULTI_POOL`, written by ops/host/install.sh from the
deployment record and read by the unit), or from `--pool`. There is no default.

Exit codes: 0 quiet · 1 findings · 2 bad call · 3 could not check.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eth_utils import keccak  # noqa: E402  (after the path line: the venv's, as the keeper's)
import keeper_watch as kw  # noqa: E402
from keeper_watch import (  # noqa: E402  (one chain reader, one node policy, one ladder for the watchdogs)
    ADDRESS,
    GRACE_S,
    RPC_FALLBACK_URL,
    RPC_URL,
    BREACH_NAMES,
    Chain,
    Unreachable,
    _addr_array,
    _hex,
    _word,
    violation,
)

backoff = kw.backoff  # the ladder the chain reader climbs, kw's own
CHECKPOINT_WINDOW = 15 * 60  # PaperExam.CHECKPOINT_WINDOW, RuledAccount's too
# PaperExam.Status, MultiPool.ExamState and MultiPool.SeatState, as in ops/multi_keeper.py.
EXAM_ACTIVE = 1
EXAM_SOLD = 1
SEAT_FUNDED = 3
MAX_MADE = 2_000  # contracts a pool made that one run reads at most; more is "could not check", not a guess


def created_address(maker: str, nonce: int) -> str:
    """The address CREATE gives the `nonce`th contract `maker` makes: keccak(rlp([maker, nonce]))[12:]."""
    if nonce == 0:
        n = b"\x80"
    elif nonce < 0x80:
        n = bytes([nonce])
    else:
        raw = nonce.to_bytes((nonce.bit_length() + 7) // 8, "big")
        n = bytes([0x80 + len(raw)]) + raw
    payload = b"\x94" + bytes.fromhex(maker[2:]) + n
    return "0x" + keccak(bytes([0xC0 + len(payload)]) + payload)[12:].hex()


def made_by(ch: Chain, pool: str) -> list[str]:
    """Every contract `pool` made: a contract's nonce starts at 1 and counts its creations (EIP-161)."""
    n = _hex(ch._rpc("eth_getTransactionCount", [pool, "latest"]), "the pool's nonce")
    if n - 1 > MAX_MADE:
        raise Unreachable(f"the pool made {n - 1} contracts, more than the {MAX_MADE} a run reads")
    return [created_address(pool, k) for k in range(1, n)]


def _arg(address: str) -> str:
    return f"{int(address, 16):064x}"


def missed_snapshot(day: int, now: int) -> bool:
    """The day's snapshot is missing once the window it can be taken in is over."""
    return now % 86_400 >= CHECKPOINT_WINDOW and day < now // 86_400


# -- the rules ----------------------------------------------------------------------------------

def examine_exam(ch: Chain, pool: str, exam: str, now: int, findings: list[dict], seen: dict) -> None:
    if _word(ch.call(pool, "examInfo(address)", _arg(exam)), 1) != EXAM_SOLD:
        return
    seen["exams sold"] += 1
    if _word(ch.call(exam, "result()"), 0) != EXAM_ACTIVE:
        return
    seen["exams running"] += 1
    day = _word(ch.call(exam, "day()"), 0)
    if missed_snapshot(day, now):
        findings.append({
            "id": f"snapshot|{exam}|{now // 86_400}",
            "rule": "the keeper did not take the day's snapshot",
            "text": (f"paper exam {exam} of pool {pool} is Active and its last snapshot is of day {day}, not today "
                     f"({now // 86_400}); the window, the first {CHECKPOINT_WINDOW // 60} minutes of the UTC day, is "
                     f"over, so today's daily loss limit stands on an old snapshot."),
        })
    deadline = _word(ch.call(exam, "deadline()"), 0)
    # terms() is a struct with an array in it: word 0 is its offset, then capital, the array's offset, stepDuration,
    # maxIdle (src/exam/PaperExam.sol, ExamTerms).
    max_idle = _word(ch.call(exam, "terms()"), 4)
    last_fill = _word(ch.call(exam, "lastFillAt()"), 0) if max_idle else 0
    if deadline and now > deadline + GRACE_S:
        findings.append({
            "id": f"expire|{exam}",
            "rule": "the keeper did not expire it",
            "text": (f"paper exam {exam} of pool {pool} is Active, its deadline {deadline} passed "
                     f"{(now - deadline) // 60} minutes ago (limit {GRACE_S // 60}). `expire` is due."),
        })
    elif max_idle and now > last_fill + max_idle + GRACE_S:
        findings.append({
            "id": f"expire|{exam}",
            "rule": "the keeper did not expire it",
            "text": (f"paper exam {exam} of pool {pool} is Active and has gone {(now - last_fill) // 60} minutes "
                     f"without a fill, past its idle limit of {max_idle // 60} (and {GRACE_S // 60} more). `expire` "
                     f"is due."),
        })
    b = _word(ch.call(exam, "violation()"), 0)
    if b != 0:
        findings.append({
            "id": f"breach|{exam}|{b}",
            "rule": "the keeper did not breach it",
            "text": (f"paper exam {exam} of pool {pool}: violation() = {BREACH_NAMES.get(b, b)} and it is still "
                     f"Active. `breach` is due. The chain records no time for a violation, so it is reported on "
                     f"every run that sees it."),
        })


def examine_seat(ch: Chain, pool: str, seat: str, now: int, findings: list[dict], seen: dict) -> None:
    if _word(ch.call(pool, "seatInfo(address)", _arg(seat)), 1) != SEAT_FUNDED:
        return
    seen["seats funded"] += 1
    if _word(ch.call(seat, "isStopped()"), 0):
        return
    b = violation(ch, seat)
    if b != 0:
        findings.append({
            "id": f"stop|{seat}|{b}",
            "rule": "the keeper did not stop it",
            "text": (f"seat {seat} of pool {pool} is funded: violation() = {BREACH_NAMES.get(b, b)}, the account is "
                     f"NOT stopped. `breach` is due."),
        })
    day = _word(ch.call(seat, "day()"), 0)
    if missed_snapshot(day, now):
        findings.append({
            "id": f"snapshot|{seat}|{now // 86_400}",
            "rule": "the keeper did not take the day's snapshot",
            "text": (f"seat {seat} of pool {pool} is funded and its last snapshot is of day {day}, not today "
                     f"({now // 86_400}); the day's window is over."),
        })


def examine(ch: Chain, pool: str) -> tuple[list[dict], dict, str]:
    """The findings, what was covered and, if the run stopped early, why; a read that fails after a finding keeps
    the finding, as in keeper_watch.py."""
    now = ch.now()
    seats = _addr_array(ch.call(pool, "seats()"))
    made = made_by(ch, pool)
    findings: list[dict] = []
    seen = {"contracts made": len(made), "exams sold": 0, "exams running": 0, "seats": len(seats), "seats funded": 0}
    seat_set = {s.lower() for s in seats}
    # The newest contracts first: an exam sold lately is the one most likely running, and a run that stops at its
    # budget then leaves out the old ones, released long ago, not the live ones.
    work = [("seat", s) for s in seats] + [("exam", e) for e in reversed(made) if e.lower() not in seat_set]
    for n, (kind, account) in enumerate(work):
        try:
            (examine_seat if kind == "seat" else examine_exam)(ch, pool, account, now, findings, seen)
        except Unreachable as e:
            if not findings:
                raise
            return findings, seen, f"stopped at {kind} {account}, {len(work) - n} of {len(work)} accounts not read: {e}"
    return findings, seen, ""


def pool_address(explicit: str) -> str:
    pool = explicit or os.environ.get("COLOSSEUM_MULTI_POOL", "")
    if not pool:
        raise Unreachable("no pool is named: COLOSSEUM_MULTI_POOL is not set (the unit reads it from the "
                          "deployment's env file) and there is no --pool; there is no default on purpose")
    if not ADDRESS.fullmatch(pool):
        raise Unreachable(f"the pool is not an address: {pool!r}")
    return pool


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--rpc", default=RPC_URL)
    ap.add_argument("--rpc-fallback", default=RPC_FALLBACK_URL,
                    help="the node to use if the first is broken, out of allowance, or still busy; empty: none")
    ap.add_argument("--pool", default="", help="the MultiPool's address, instead of COLOSSEUM_MULTI_POOL")
    a = ap.parse_args(argv)

    ch = Chain(a.rpc, a.rpc_fallback)
    try:
        pool = pool_address(a.pool)
        findings, seen, incomplete = examine(ch, pool)
    except Unreachable as e:
        print(f"COULD NOT CHECK: {e} - this is NOT a finding about the keeper", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 - a crash of the tool is not a finding about the keeper (keeper_watch.py)
        print(f"COULD NOT CHECK: unexpected {type(e).__name__}: {e} - this is NOT a finding about the keeper",
              file=sys.stderr)
        return 3

    label = os.environ.get("COLOSSEUM_DEPLOYMENT", "")
    print(f"{'deployment ' + label + ' | ' if label else ''}pool {pool} | "
          + " | ".join(f"{k}: {v}" for k, v in seen.items()) + f" | node calls: {ch.calls}")
    if ch.switched:
        print(f"READ THE FALLBACK NODE {ch.rpc}: the first, {ch.nodes[0]}, did not answer ({ch.switched[:120]})")
    if not findings:
        print("QUIET: for the live exams and seats, everything the keeper is bound to do is done")
        return 0
    for f in findings:
        print(f"FINDING: {f['id']}")
        print(f"{f['rule']}: {f['text']}")
    if incomplete:
        print(f"INCOMPLETE: the run {incomplete}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
