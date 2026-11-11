#!/usr/bin/env python3
"""Watchdog for the free agent keys in a Colosseum key registry, run on the pools host.

Free keys are few (seventeen are laid out), and spoiling one costs about one test USDC. With no
free key both a challenge pass and the sale of new challenges stop, on the whole factory at once.
So breaking the demo costs a few USDC, and it has to be noticed BEFORE it happens, not from a
complaint. Two signals, and the second matters more:

  1. The count. The watchdog reads `freeCount()` of the live registry and compares it with the
     threshold (`--threshold`, 16 in the unit). The comparison is STRICT: the alarm is `free <
     threshold`. With seventeen laid out: 17 is quiet, 16 is quiet (one key has gone, the
     watchdog is still silent), 15 is critical (the second has gone). "Threshold 16" is therefore
     an alarm on the SECOND key lost; an alarm on the first is threshold 17.

     The count does not tell spoilage from sale: every sold challenge takes a free key, and only
     `publish([...])` by the operator gives keys back. It is "how many are left", not "someone is
     breaking them".

  2. The spoilage. For EVERY free key the watchdog asks whether its address has gained an account
     on HyperCore. If it has, the key is already spoiled and will be refused at issue
     (`CoreOps.exists` / `KeySpoiled` in the contract). One spoiled key is critical whatever the
     count.

The free keys are listed from storage: `_free` has no getter, only `freeCount()`. The array's slot
is 5 (measured: the slot holds the length, equal to `freeCount()`) and its elements start at
`keccak256(5)`. The guess is checked on every run: the length must equal `freeCount()` and each
element must be an address in the state `Free`. If not, the run exits 3 with a loud line rather
than report "no spoiled keys" from the wrong memory.

The registry's address is not written in: it comes from the deployment's env file
(`GATEWAY_REGISTRY`, which ops/host/install.sh writes from the deployment record and the unit
reads), or from `--registry`. There is no default: a watchdog that does not know what it watches
says so.

Exit codes: 0 enough keys and none spoiled (quiet) · 1 a finding (spoiled keys, or fewer than the
threshold) · 3 COULD NOT CHECK (node, network, registry address, storage layout): a failure of the
tool, not a statement about the registry.
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

# Not the keeper's node and not the keeper watchdog's: this one watches the registry, so a budget
# shared with the keeper must not blind it when it is needed, and with the two watchdogs on two
# nodes the loss of one node does not take both. The name of the variable is this watchdog's own:
# one file (/etc/colosseum/watch.env) feeds every watchdog unit, and a shared name would put them
# back on one node. Every method it uses (eth_blockNumber, eth_getStorageAt, eth_call, and the
# HyperCore precompile at 0x...0810) answers on both nodes.
RPC_URL = os.environ.get("POOLS_WATCH_KEYS_RPC", "https://998.rpc.thirdweb.com")
# The fallback is the gateway's node. A node that is BROKEN (HTTP 500, "upstreams exhausted")
# is not helped by the ladder, which is for a node that is BUSY: rate limits (-32005, HTTP 429) are
# still waited out on the same node, and a broken node is left for the fallback until the run ends.
# On the fallback this watchdog shares its node with the keeper watchdog; that is a degraded mode,
# and the output says so.
RPC_FALLBACK_URL = os.environ.get("POOLS_WATCH_KEYS_RPC_FALLBACK", "https://rpc.hyperliquid-testnet.xyz/evm")
# Some nodes refuse a bare urllib client; a User-Agent is required.
USER_AGENT = "colosseum-pools-host-watch/1.0 (+https://github.com/namixai/colosseum-pools)"
# keccak256("freeCount()")[:4]; recompute with: cast sig 'freeCount()'.
SEL_FREE_COUNT = "0xa6f48c90"
# keccak256("bindingOf(address)")[:4]; the struct is Binding{State state; address account; address trader}.
SEL_BINDING_OF = "0x8cae3151"
# The HyperCore precompile "does this address have an account": the address is
# CORE_USER_EXISTS_PRECOMPILE_ADDRESS of hyper-evm-lib's HLConstants; the argument is an address,
# the answer 0 or 1.
CORE_USER_EXISTS = "0x0000000000000000000000000000000000000810"
# The slot of the `_free` array in KeyRegistry. Measured, not guessed: slot 5 holds a length equal
# to `freeCount()` and the elements from keccak256(5) read as keys in the state Free.
FREE_ARRAY_SLOT = 5
STATE_FREE = 1               # enum State { Unknown, Free, Bound, Retired }: Free is the second
FREE_KEYS_FLOOR = 16         # what the number means is in the header, together with the comparison
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
# There is no number of tries or pauses here on purpose: the ladder is one for both watchdogs, in
# watch_backoff.py (seven tries, 63 s, more than the node's one-minute rate window).


class Unreachable(Exception):
    """Could not read. A failure of the tool, not a statement about the registry."""


class _Transient(Exception):
    """A failure worth retrying: network, timeout, node error, rate limit."""


def _hex(raw, what: str) -> int:
    """A hexadecimal number from the node, or Unreachable. An answer that is not one is a node that cannot be
    read: it ends the read the way a refused request does, so that a finding already made is not lost to a
    ValueError on its way up. Bugs of the tool itself are not turned into this; they stay exit 3 in main()."""
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        raise Unreachable(f"{what} is not a hexadecimal number: {raw!r}"[:240]) from None


def _post(url: str, payload: dict, timeout: float = 25) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _Transient(f"{type(e).__name__}: {e}") from e


def _rate_limited(err: str) -> bool:
    """The node is busy (a pause helps), not broken (only another node helps)."""
    return "-32005" in err or "HTTP Error 429" in err


class Chain:
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

    @property
    def retries(self) -> int:
        return self.ladder.retries

    def _rpc(self, method: str, params: list):
        self.calls += 1
        last = ""
        made = 0
        attempt = 0
        # A while, not a for over range: a move to the fallback does NOT spend a try. Otherwise a
        # move on the seventh try (six "busy", then "broken") would leave the loop without ever asking
        # the healthy fallback. There is one move, so the loop ends.
        while attempt < backoff.ATTEMPTS:
            made = attempt + 1
            try:
                j = _post(self.rpc, {"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params})
            except _Transient as e:
                last = str(e)
                # A broken node goes straight to the fallback, with no pause: there is nothing to wait for.
                if not _rate_limited(last) and self._switch(last):
                    continue
                if not self.ladder.again(attempt):
                    break
                attempt += 1
                continue
            if not isinstance(j, dict) or "error" in j:
                # A rate limit and any node error mean "did not read", not "no keys".
                last = f"the node answered with an error: {j.get('error') if isinstance(j, dict) else j}"
                if not _rate_limited(last) and self._switch(last):
                    continue
                if not self.ladder.again(attempt):
                    break
                attempt += 1
                continue
            return j.get("result")
        raise Unreachable(f"{method}: {last} - {self.ladder.gave_up(made)}")

    def head(self) -> int:
        raw = self._rpc("eth_blockNumber", [])
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise Unreachable(f"the chain's head was not read: {raw!r}")
        return _hex(raw, "the chain's head")

    def storage_at(self, addr: str, slot: int) -> str:
        raw = self._rpc("eth_getStorageAt", [addr, hex(slot), "latest"])
        if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) != 66:
            raise Unreachable(f"slot {slot} of {addr} was not read: {raw!r}")
        return raw

    def core_user_exists(self, who: str) -> bool:
        """Whether the address has an account on HyperCore. That is what spoilage is."""
        raw = self._rpc("eth_call",
                        [{"to": CORE_USER_EXISTS, "data": "0x" + who[2:].lower().rjust(64, "0")},
                         "latest"])
        if not isinstance(raw, str) or len(raw) != 66:
            raise Unreachable(f"the \"has an account\" precompile did not answer with a word: {raw!r}")
        v = _hex(raw, "the precompile's answer")
        if v not in (0, 1):
            # An answer that is not yes or no is not a "no". Counting it as zero would report
            # "none spoiled" on an answer nobody understands.
            raise Unreachable(f"the precompile returned {v}, not 0 or 1: cannot judge by that")
        return v == 1

    def free_count(self, registry: str) -> int:
        raw = self._rpc("eth_call", [{"to": registry, "data": SEL_FREE_COUNT}, "latest"])
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise Unreachable(f"the registry's answer was not parsed: {raw!r}")
        body = raw[2:]
        if len(body) != 64:
            # An empty answer (`0x`) means "no such method at this address", for instance an address
            # that is no registry at all. Counting it as zero would be an alarm that never ends.
            raise Unreachable(f"the registry's answer is not 32 bytes but {len(body) // 2}: {raw!r}")
        return _hex(body, "the registry's answer")


def free_keys(chain: Chain, registry: str, free: int, slot: int) -> list[str]:
    """The addresses of the free keys from the registry's storage, and a check of the guess itself.

    `_free` has no getter, so the array is read by slot. The guess about the layout is checked on
    EVERY run, not once when this was written: the length in the slot must equal `freeCount()` and
    each element must be an address in the state `Free`. Otherwise exit 3: after a registry move,
    quietly reading someone else's memory and reporting "no spoiled keys" is the worst a watchdog
    can do.
    """
    length = _hex(chain.storage_at(registry, slot), f"slot {slot}")
    if length != free:
        raise Unreachable(
            f"the storage layout did not hold: slot {slot} has length {length} but "
            f"freeCount() = {free}; the keys cannot be listed from this slot")
    base = int(keccak(slot.to_bytes(32, "big")).hex(), 16)
    keys = []
    for i in range(length):
        word = chain.storage_at(registry, base + i)
        if _hex(word, f"element {i} of slot {slot}") >> 160:
            raise Unreachable(f"element {i} of slot {slot} is not an address ({word}): wrong layout")
        key = "0x" + word[-40:]
        raw = chain._rpc("eth_call",
                         [{"to": registry, "data": SEL_BINDING_OF + key[2:].rjust(64, "0")},
                          "latest"])
        if not isinstance(raw, str) or len(raw) < 66:
            raise Unreachable(f"the binding of key {key} was not read: {raw!r}")
        state = _hex(raw[2:66], f"the binding of key {key}")
        if state != STATE_FREE:
            raise Unreachable(
                f"key {key} from the free array is in state {state}, not Free: "
                f"the storage layout is wrong, the listing cannot be trusted")
        keys.append(key)
    return keys


def registry_address(explicit: str) -> str:
    """The registry to watch: `--registry`, else GATEWAY_REGISTRY from the deployment's env file."""
    reg = explicit or os.environ.get("GATEWAY_REGISTRY", "")
    if not reg:
        raise Unreachable("no registry is named: GATEWAY_REGISTRY is not set (the unit reads it from the "
                          "deployment's env file) and there is no --registry; there is no default on purpose")
    if not ADDRESS.fullmatch(reg):
        raise Unreachable(f"the registry is not an address: {reg!r}")
    return reg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Watchdog for the free keys of a Colosseum key registry")
    ap.add_argument("--rpc", default=RPC_URL)
    ap.add_argument("--rpc-fallback", default=RPC_FALLBACK_URL,
                    help="the node to use if the first is broken (not busy); empty: no fallback")
    ap.add_argument("--registry", default="", help="the registry's address, instead of GATEWAY_REGISTRY")
    ap.add_argument("--threshold", type=int, default=FREE_KEYS_FLOOR,
                    help=f"fewer free keys than this is a finding (default {FREE_KEYS_FLOOR})")
    ap.add_argument("--slot", type=int, default=FREE_ARRAY_SLOT,
                    help=f"the slot of the free-key array in the registry (measured: {FREE_ARRAY_SLOT})")
    ap.add_argument("--no-spoilage", action="store_true",
                    help="the threshold only, with no HyperCore query for each key")
    a = ap.parse_args(argv)

    chain = Chain(a.rpc, a.rpc_fallback)
    try:
        registry = registry_address(a.registry)
        # The head is read BEFORE the count: this family of nodes ignores the block number in
        # eth_call and answers from the current state (measured), so "read at block N" here means
        # "N was the head when it was read", not "the state is pinned at N".
        block = chain.head()
        free = chain.free_count(registry)
        spoiled: list[str] = []
        checked = 0
        incomplete = ""
        if not a.no_spoilage:
            try:
                keys = free_keys(chain, registry, free, a.slot)
                for k in keys:
                    if chain.core_user_exists(k):
                        spoiled.append(k)
                    checked += 1
            except Unreachable as e:
                # A count already under the threshold, or a spoiled key already found, is a finding whatever the
                # rest of the reading comes to: it is reported, with the check marked incomplete. With neither,
                # nothing can be said, and this is exit 3.
                if free >= a.threshold and not spoiled:
                    raise
                incomplete = str(e)
    except Unreachable as e:
        print(f"COULD NOT CHECK: {e} - a failure of the tool, NOT a statement about the registry",
              file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 - see below
        # Python exits 1 on an uncaught exception, and 1 is "a finding" to the wrapper: a crash of
        # the tool would raise an alarm about the registry. It is a failure of the tool, exit 3.
        print(f"COULD NOT CHECK: unexpected {type(e).__name__}: {e} - a failure of the tool, "
              "NOT a statement about the registry", file=sys.stderr)
        return 3

    label = os.environ.get("COLOSSEUM_DEPLOYMENT", "")
    tail = f" (retries: {chain.retries})" if chain.retries else ""
    if chain.switched:
        tail += (f" | READ THE FALLBACK NODE {chain.rpc}: the first, {chain.nodes[0]}, is broken "
                 f"({chain.switched[:160]})")
    probe = (f" | checked for spoilage: {checked} | spoiled: {len(spoiled)}"
             if not a.no_spoilage else " | spoilage not checked (--no-spoilage)")
    print(f"{'deployment ' + label + ' | ' if label else ''}registry {registry} | free keys: {free} | "
          f"threshold: {a.threshold}{probe} | chain head at the read: block {block} | calls: {chain.calls}{tail}")

    found = False
    if spoiled:
        # Spoilage outranks the threshold: a key whose address already has an account will be
        # refused at issue, and the count of free keys does not show it at all.
        found = True
        print(f"FINDING: {len(spoiled)} of {checked} free keys are spoiled: their addresses already have "
              f"an account on HyperCore and will be refused at issue")
        for k in spoiled:
            print(f"  spoiled: {k}")
    if free < a.threshold:
        found = True
        print(f"FINDING: {free} free agent keys, fewer than the threshold {a.threshold}")
    if found:
        if incomplete:
            print(f"INCOMPLETE: the spoilage check stopped after {checked} of {free} keys: {incomplete}")
        print("SEVERITY: critical")
        print("With no usable free key both a challenge pass and the sale of new ones stop, on the whole "
              f"factory. Refill: `publish([...])` on the registry {registry} by the operator; the spoiled "
              "ones leave by themselves, the registry sets them aside at the next issue.")
        return 1
    print("QUIET: not fewer keys than the threshold and none spoiled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
