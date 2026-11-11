#!/usr/bin/env python3
"""Watchdog for what the keeper of a Colosseum factory is bound to do, and on time. Run on the pools host.

It was written after a keeper had followed zero pools for days and fallen 175 thousand blocks
behind with nobody noticing. The service was "active" and its logs went on, so the liveness of the
process said nothing about the work being done. This checks the RESULT, from outside the keeper's
own files: public chain reads only, no access to its state or its key.

Three rules, all over LIVE accounts:

  1. `violation() != 0` and the account is not stopped: "the keeper did not stop it".
     The chain records no time for a violation (`violation()` is computed from HyperCore's live
     state), so how long it has stood cannot be proved. It is reported on every run that sees it;
     the flag clears at the first run that no longer does.

  2. A challenge `Active` past its deadline plus 15 minutes: "the keeper did not close it on time".
     The age IS on the chain (`deadline()`).

  3. A challenge `Created` for longer than 15 minutes: "the keeper did not activate it". The age
     is on the chain too (`createdAt()`). The text says whether the capital arrived
     (`capitalArrived()`): with no capital the keeper must not activate but abort after the hour
     of START_WINDOW, and those are different jobs for the operator.

LIVE ACCOUNTS ONLY. On a stopped account `violation()` reads the empty account as a drawdown;
checking it would raise a false alarm on every run. Live = a challenge in status `Active`, or a
pool in stage `Funded`.

A FAILURE OF THE NODE IS NOT A VIOLATION. A node that did not answer, an address that is not
there, an answer that did not parse: exit 3, "could not check". An alarm about the keeper is never
raised for a failure of the watchdog's own tool.

The factory comes from the deployment's env file (`GATEWAY_FACTORY`, written by ops/host/install.sh
from the deployment record and read by the unit), or from `--factory`. There is no default.

Exit codes: 0 quiet · 1 findings · 2 bad call · 3 could not check.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eth_utils import keccak  # noqa: E402  (after the path line: the venv's, as the keeper's)
import watch_backoff as backoff  # noqa: E402  (one retry ladder for both watchdogs)

# The gateway's node, not the keeper's own: a watchdog must not share a budget with what it
# watches, or it goes blind exactly when the keeper is busiest. This one needs only `eth_call` and
# `eth_getBlockByNumber`, so a refusal that keeps the keeper on another node (its `eth_getLogs` from
# this host) does not touch it.
RPC_URL = os.environ.get("POOLS_WATCH_RPC", "https://rpc.hyperliquid-testnet.xyz/evm")
# The fallback is the keeper's backup node, as the deployment record names it
# (`keeper_nodes`; install.sh renders it as COLOSSEUM_RPC_BACKUP_URL into the deployment's env
# file, which the unit reads). POOLS_WATCH_RPC_FALLBACK overrides it. There is no node written
# in here: a record that names none leaves the watchdog on its first node alone.
# Why it matters: the public node answers this host's address with "-32005 More than 3000
# archived blocks queried in one day" to every eth_getBlockByNumber and eth_getLogs, while
# eth_blockNumber still passes. The allowance is per day and shared by everything on the host that
# reads the node; a minute's pause does not lift it. A node that is broken or out of allowance is
# left for the fallback until the run ends; a node that is merely busy gets the ladder, and if it
# is still busy after it, the fallback gets one try (11 Nov 2026).
RPC_FALLBACK_URL = os.environ.get("POOLS_WATCH_RPC_FALLBACK") or os.environ.get("COLOSSEUM_RPC_BACKUP_URL", "")
USER_AGENT = "colosseum-pools-host-watch/1.0 (+https://github.com/namixai/colosseum-pools)"
GRACE_S = 15 * 60
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")

# ChallengeAccount.Status and Pool.Stage, as in ops/keeper.py and the Solidity source.
CREATED, ACTIVE, BREACHED, EXPIRED, FORFEITED, PASSED, ABORTED, SETTLED = range(1, 9)
IDLE, CHALLENGE, FUNDED, CLOSING = range(4)
BREACH_NAMES = {0: "none", 1: "Drawdown", 2: "DailyLoss", 3: "Leverage", 4: "ForbiddenAsset"}


class Unreachable(Exception):
    """Could not check: node, network, factory address, an answer that did not parse."""


def _hex(raw, what: str) -> int:
    """A hexadecimal number from the node, or Unreachable. An answer that is not one is a node that cannot be
    read: it ends the read the way a refused request does, so that a finding already made is not lost to a
    ValueError on its way up. Bugs of the tool itself are not turned into this; they stay exit 3 in main()."""
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        raise Unreachable(f"{what} is not a hexadecimal number: {raw!r}"[:240]) from None


# -- the chain ----------------------------------------------------------------------------------

def _post(url: str, payload: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _rate_limited(err: str) -> bool:
    """The node is busy (a pause helps), not broken (only another node helps). The daily allowance
    on archived blocks comes as -32005 too, but a pause of a minute does not lift it: for this
    run the node is broken, and there is nothing to wait for."""
    if "in one day" in err:
        return False
    return "-32005" in err or "HTTP Error 429" in err


class Chain:
    """The chain reads, from `rpc` and, if that node is broken, from `fallback`."""

    def __init__(self, rpc: str = RPC_URL, fallback: str = RPC_FALLBACK_URL, real: bool = True):
        self.rpc, self.calls = rpc, 0
        self.nodes = [rpc] + ([fallback] if fallback and fallback != rpc else [])
        self.node = 0
        self.switched = ""   # why it left the first node; empty if it did not
        self.ladder = backoff.Ladder(real=real)

    def _switch(self, why: str) -> bool:
        """Move to the next node for the rest of the run. False: there is none to move to."""
        if self.node + 1 >= len(self.nodes):
            return False
        self.node += 1
        self.rpc = self.nodes[self.node]
        self.switched = why
        return True

    def _rpc(self, method: str, params: list):
        last = ""
        made = 0
        attempt = 0
        # A while, not a for over range: a move to the fallback does NOT spend a try. There is one
        # move, so the loop ends. A move after the ladder (busy all the way) leaves `attempt` at the
        # ladder's last, so the fallback gets exactly one try there (11 Nov 2026).
        while attempt < backoff.ATTEMPTS:
            made = attempt + 1
            try:
                j = _post(self.rpc, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                    json.JSONDecodeError) as e:
                last = f"{type(e).__name__}: {e}"
                # A broken node goes straight to the fallback, with no pause: there is nothing to wait for.
                if not _rate_limited(last) and self._switch(last):
                    continue
                if not self.ladder.again(attempt):
                    # Busy past the ladder (or the run's budget): one try on the fallback, since `attempt`
                    # stays where it is and the loop has one turn left; then the rest of the run reads it.
                    if _rate_limited(last) and self._switch(f"busy past the ladder: {last}"):
                        continue
                    break
                attempt += 1
                continue
            if not isinstance(j, dict) or "error" in j:
                # A rate limit is "could not read", not "a violation". So is an answer that is no
                # JSON-RPC envelope at all.
                last = f"the node answered with an error: {j.get('error') if isinstance(j, dict) else j}"
                if not _rate_limited(last) and self._switch(last):
                    continue
                if not self.ladder.again(attempt):
                    # Busy past the ladder (or the run's budget): one try on the fallback, since `attempt`
                    # stays where it is and the loop has one turn left; then the rest of the run reads it.
                    if _rate_limited(last) and self._switch(f"busy past the ladder: {last}"):
                        continue
                    break
                attempt += 1
                continue
            return j.get("result")
        raise Unreachable(f"{method}: {last} - {self.ladder.gave_up(made)}")

    def call(self, to: str, sig: str, data_suffix: str = "") -> str:
        """An eth_call by signature: the selector is computed from it, not written in as a number."""
        self.calls += 1
        selector = "0x" + keccak(sig.encode()).hex()[:8]
        res = self._rpc("eth_call", [{"to": to, "data": selector + data_suffix}, "latest"])
        if not isinstance(res, str) or not res.startswith("0x"):
            raise Unreachable(f"the answer to {sig} at {to} was not parsed: {res!r}")
        return res

    def now(self) -> int:
        """The chain's time, not the machine's: comparing a contract's deadline with a clock is an error."""
        blk = self._rpc("eth_getBlockByNumber", ["latest", False])
        if not isinstance(blk, dict) or "timestamp" not in blk:
            raise Unreachable(f"the chain's head was not read: {blk!r}")
        return _hex(blk["timestamp"], "the head's timestamp")


def _word(raw: str, i: int) -> int:
    body = raw[2:]
    if len(body) < 64 * (i + 1):
        raise Unreachable(f"the answer has no word {i}: {raw[:80]}")
    return _hex(body[64 * i:64 * (i + 1)], f"word {i} of the answer")


def _addr(raw: str, i: int = 0) -> str:
    return "0x" + f"{_word(raw, i):040x}"


def _addr_array(raw: str) -> list[str]:
    n = _word(raw, 1)                      # [0] is the offset, [1] the length
    return ["0x" + f"{_word(raw, 2 + k):040x}" for k in range(n)]


def read_pools(ch: Chain, factory: str) -> list[str]:
    return _addr_array(ch.call(factory, "pools()"))


def violation(ch: Chain, account: str) -> int:
    # An empty uint32[]: offset 0x20 and length 0. The watchdog does not know the extra assets,
    # the pool's owner does, and it must not make any up.
    suffix = f"{32:064x}{0:064x}"
    return _word(ch.call(account, "violation(uint32[])", suffix), 0)


# -- the rules ----------------------------------------------------------------------------------

def examine_pool(ch: Chain, pool: str, now: int, findings: list[dict], seen: dict) -> None:
    """One pool and its challenge account against the three rules. Findings are added to `findings` as they
    are established, so that a read that fails further on does not take them with it."""
    stage = _word(ch.call(pool, "stage()"), 0)
    challenge = _addr(ch.call(pool, "challenge()"))

    if challenge != "0x" + "0" * 40:
        status = _word(ch.call(challenge, "status()"), 0)
        if status == ACTIVE:
            seen["live accounts"] += 1
            deadline = _word(ch.call(challenge, "deadline()"), 0)
            if deadline and now > deadline + GRACE_S:
                findings.append({
                    "id": f"expire|{challenge}",
                    "rule": "the keeper did not close it on time",
                    "text": (f"challenge {challenge} of pool {pool} is Active, its deadline "
                             f"{deadline} passed {(now - deadline) // 60} minutes ago "
                             f"(limit {GRACE_S // 60}). `expire` is due."),
                })
            b = violation(ch, challenge)
            if b != 0 and not _word(ch.call(challenge, "isStopped()"), 0):
                findings.append({
                    "id": f"stop|{challenge}|{b}",
                    "rule": "the keeper did not stop it",
                    "text": (f"challenge {challenge} of pool {pool}: violation() = "
                             f"{BREACH_NAMES.get(b, b)}, the account is NOT stopped. `breach` is due. "
                             f"The chain records no time for a violation, so it is reported on every "
                             f"run that sees it."),
                })
        elif status == CREATED:
            seen["live accounts"] += 1
            created = _word(ch.call(challenge, "createdAt()"), 0)
            if created and now > created + GRACE_S:
                arrived = bool(_word(ch.call(challenge, "capitalArrived()"), 0))
                findings.append({
                    "id": f"activate|{challenge}",
                    "rule": "the keeper did not activate it",
                    "text": (f"challenge {challenge} of pool {pool} was bought {(now - created) // 60} "
                             f"minutes ago and is still Created; the capital "
                             f"{'arrived: `activate` is due' if arrived else 'has NOT arrived: after the hour `abort` is due, not activation'}."),
                })
        else:
            # A stopped account's violation() reads the empty account as a drawdown. Such an
            # account is not touched at all: it would be an alarm on every run.
            seen["closed accounts skipped"] += 1

    if stage == FUNDED:
        seen["live accounts"] += 1
        b = violation(ch, pool)
        if b != 0 and not _word(ch.call(pool, "isStopped()"), 0):
            findings.append({
                "id": f"stop|{pool}|{b}",
                "rule": "the keeper did not stop it",
                "text": (f"pool {pool} is in stage Funded: violation() = {BREACH_NAMES.get(b, b)}, "
                         f"the account is NOT stopped. `breach` is due. The chain records no time "
                         f"for a violation, so it is reported on every run that sees it."),
            })


def examine(ch: Chain, factory: str) -> tuple[list[dict], dict, str]:
    """The findings, a summary of what was covered and, if the run stopped early, why.

    A read that fails before any finding is Unreachable: nothing can be said. A read that fails AFTER a finding
    does not take the finding with it: the finding is real, so it is reported, with the run marked incomplete and
    the pools it did not reach counted. Otherwise a node that went away half-way, or a pool that cannot be read
    on every run, would hide a breach the watchdog had already seen. The run stops at the first failed read: a
    node that has failed once will fail the rest of the run's budget too."""
    now = ch.now()
    pools = read_pools(ch, factory)
    findings, seen = [], {"pools": len(pools), "live accounts": 0, "closed accounts skipped": 0}
    for n, pool in enumerate(pools):
        try:
            examine_pool(ch, pool, now, findings, seen)
        except Unreachable as e:
            if not findings:
                raise
            return findings, seen, f"stopped at pool {pool}, {len(pools) - n} of {len(pools)} pools not fully read: {e}"
    return findings, seen, ""


def factory_address(explicit: str) -> str:
    """The factory to watch: `--factory`, else GATEWAY_FACTORY from the deployment's env file."""
    factory = explicit or os.environ.get("GATEWAY_FACTORY", "")
    if not factory:
        raise Unreachable("no factory is named: GATEWAY_FACTORY is not set (the unit reads it from the "
                          "deployment's env file) and there is no --factory; there is no default on purpose")
    if not ADDRESS.fullmatch(factory):
        raise Unreachable(f"the factory is not an address: {factory!r}")
    return factory


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--rpc", default=RPC_URL)
    ap.add_argument("--rpc-fallback", default=RPC_FALLBACK_URL,
                    help="the node to use if the first is broken, out of allowance, or still busy after the "
                         "ladder; empty: none")
    ap.add_argument("--factory", default="", help="the factory's address, instead of GATEWAY_FACTORY")
    a = ap.parse_args(argv)

    ch = Chain(a.rpc, a.rpc_fallback)
    try:
        factory = factory_address(a.factory)
        findings, seen, incomplete = examine(ch, factory)
    except Unreachable as e:
        print(f"COULD NOT CHECK: {e} - this is NOT a finding about the keeper", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 - see below
        # Python exits 1 on an uncaught exception, and 1 is "a finding" to the wrapper: a crash of
        # the tool would raise an alarm about the keeper. It is a failure of the tool, exit 3.
        print(f"COULD NOT CHECK: unexpected {type(e).__name__}: {e} - this is NOT a finding about the keeper",
              file=sys.stderr)
        return 3

    label = os.environ.get("COLOSSEUM_DEPLOYMENT", "")
    print(f"{'deployment ' + label + ' | ' if label else ''}factory {factory} | "
          + " | ".join(f"{k}: {v}" for k, v in seen.items()) + f" | node calls: {ch.calls}")
    if ch.switched:
        # Degraded mode: the watchdog read the fallback node, and the output says so rather than keep quiet.
        print(f"READ THE FALLBACK NODE {ch.rpc}: the first, {ch.nodes[0]}, did not answer ({ch.switched[:120]})")

    if not findings:
        print("QUIET: for the live accounts, everything the keeper is bound to do is done")
        return 0

    for f in findings:
        print(f"FINDING: {f['id']}")
        print(f"{f['rule']}: {f['text']}")
    if incomplete:
        print(f"INCOMPLETE: the run {incomplete}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
