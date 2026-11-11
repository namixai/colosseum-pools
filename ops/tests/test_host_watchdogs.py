"""The host's watchdogs (ops/host/watchdogs): the two checks, the wrapper that sorts their outcomes, the units and
timers, and how ops/host/install.sh puts them in place from a release. Offline: the chain is a model in this
file, systemd is a pair of scripts that write down what they were asked, and the "host" is a temporary directory.

    spike/.venv/bin/python -m unittest ops.tests.test_host_watchdogs
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

from eth_utils import keccak

from ops import deployments, host_instances

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOST = ROOT / "ops" / "host"
DOGS = HOST / "watchdogs"
RUNNER = DOGS / "watchdog-run.sh"
LIB = DOGS / "install-lib.sh"

SERVICES = ["colosseum-watch-keys", "colosseum-watch-keeper",
            "colosseum-watch-keys-demo3", "colosseum-watch-keeper-demo3"]
FIRST_SET, DEMO3_SET = SERVICES[:2], SERVICES[2:]
RUNTIME = ["watchdog-run.sh", "keys_watch.py", "keeper_watch.py", "watch_backoff.py"]
SHA = "a" * 40


def load(name: str, env: dict[str, str] | None = None):
    """A fresh copy of one of the scripts. They read their node settings when imported, so a test that
    changes the environment has to import again."""
    wanted = dict(env or {})
    # The scripts import their ladder from their own directory: no __pycache__ may be left there, or the test of
    # what the directory holds (and what install.sh would copy) reads it as a file of the release.
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        with mock.patch.dict(os.environ, wanted):
            for var in ("POOLS_WATCH_RPC", "POOLS_WATCH_RPC_FALLBACK", "COLOSSEUM_RPC_BACKUP_URL", "COLOSSEUM_RPC_URL",
                        "POOLS_WATCH_KEYS_RPC", "POOLS_WATCH_KEYS_RPC_FALLBACK"):
                if var not in wanted:
                    os.environ.pop(var, None)
            spec = importlib.util.spec_from_file_location(name, DOGS / f"{name}.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = dont_write
    return mod


def run_main(mod, argv: list[str], env: dict[str, str] | None = None):
    """main() with the ladder's pauses counted and not slept, and its output captured: (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    real_ladder = mod.backoff.Ladder
    with mock.patch.dict(os.environ, env or {}), \
            mock.patch.object(mod.backoff, "Ladder", lambda real=True: real_ladder(real=False)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def word(n: int) -> str:
    return f"{n:064x}"


def addr_word(a: str) -> str:
    return a[2:].lower().rjust(64, "0")


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


# -- a registry and a factory, as the chain would answer ---------------------------------------------

REGISTRY = addr(0x1111)
FACTORY = addr(0x2222)
PRIMARY, BACKUP = "https://primary.example/evm", "https://backup.example/evm"


class Node:
    """A JSON-RPC node that answers from a model. `behaviour` says how each URL fails, if it does:
    "quota", "busy", "http500", "garbage"."""

    def __init__(self, mod, **model):
        self.mod = mod
        self.model = model
        self.behaviour: dict[str, str] = {}
        self.asked: list[tuple[str, str]] = []
        self.fail_when = lambda method, params: False   # a read that fails, whichever node is asked

    def __call__(self, url, payload, timeout=0):
        method = payload["method"]
        self.asked.append((url, method))
        if self.fail_when(method, payload["params"]):
            return {"error": {"code": -32000, "message": "execution reverted"}}
        how = self.behaviour.get(url)
        if how == "http500":
            err = urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)
            raise self.mod._Transient(f"HTTPError: {err}") if hasattr(self.mod, "_Transient") else err
        if how == "quota":
            return {"error": {"code": -32005, "message": "More than 3000 archived blocks queried in one day"}}
        if how == "busy":
            return {"error": {"code": -32005, "message": "rate limited"}}
        if how == "garbage":
            return ["not", "an", "envelope"]
        return {"result": self.answer(method, payload["params"])}

    def answer(self, method, params):
        raise NotImplementedError


class RegistryNode(Node):
    """KeyRegistry: `free` keys in the array at slot 5, each in state `state_of`, `spoiled` have a HyperCore account."""

    def __init__(self, mod, free=17, spoiled=(), slot_length=None, element=None, state=1, exists_answer=None,
                 exists_raw=None, head_raw=None):
        super().__init__(mod)
        self.exists_raw = exists_raw    # the precompile's answer, written as it is (a malformed one, say)
        self.head_raw = head_raw
        self.free = free
        self.keys = [addr(0x9000 + i) for i in range(free)]
        self.spoiled = set(addr(0x9000 + i) for i in spoiled)
        self.slot_length = free if slot_length is None else slot_length
        self.element = element
        self.state = state
        self.exists_answer = exists_answer
        self.exists_calls = 0
        self.base = int(keccak((5).to_bytes(32, "big")).hex(), 16)

    def answer(self, method, params):
        if method == "eth_blockNumber":
            return self.head_raw or hex(123456)
        if method == "eth_getStorageAt":
            slot = int(params[1], 16)
            if slot == 5:
                return "0x" + word(self.slot_length)
            i = slot - self.base
            if self.element is not None:
                return self.element
            return "0x" + word(int(self.keys[i], 16))
        if method == "eth_call":
            call = params[0]
            if call["to"] == self.mod.CORE_USER_EXISTS:
                self.exists_calls += 1
                if self.exists_raw is not None:
                    return self.exists_raw
                if self.exists_answer is not None:
                    return "0x" + word(self.exists_answer)
                return "0x" + word(1 if "0x" + call["data"][-40:] in self.spoiled else 0)
            if call["data"].startswith(self.mod.SEL_FREE_COUNT):
                return "0x" + word(self.free)
            if call["data"].startswith(self.mod.SEL_BINDING_OF):
                return "0x" + word(self.state) + word(0) + word(0)
        raise AssertionError(f"the model does not know {method} {params}")


class FactoryNode(Node):
    """A PoolFactory with pools, each with an optional challenge account, on a chain whose time is `now`."""

    def __init__(self, mod, now=1_000_000):
        super().__init__(mod)
        self.now = now
        self.pools: list[dict] = []
        self.accounts: dict[str, dict] = {}
        self.malformed: dict[str, str] = {}   # address (lower case) -> the answer every call to it gets, as written
        self.sel = {}

    def add_pool(self, stage=0, challenge=None, **account):
        pool = {"address": addr(0x5000 + len(self.pools)), "stage": stage, "challenge": challenge,
                "violation": account.pop("pool_violation", 0), "stopped": account.pop("pool_stopped", 0)}
        self.pools.append(pool)
        if challenge is not None:
            self.accounts[challenge] = {"status": 2, "deadline": 0, "createdAt": 0, "capitalArrived": 1,
                                        "violation": 0, "stopped": 0, **account}
        return pool

    def selector(self, sig: str) -> str:
        return "0x" + keccak(sig.encode()).hex()[:8]

    def answer(self, method, params):
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(self.now)}
        call = params[0]
        to, data = call["to"].lower(), call["data"]
        sel = data[:10]
        if to in self.malformed:
            return self.malformed[to]
        if to == FACTORY.lower() and sel == self.selector("pools()"):
            return "0x" + word(32) + word(len(self.pools)) + "".join(addr_word(p["address"]) for p in self.pools)
        for pool in self.pools:
            if to == pool["address"].lower():
                if sel == self.selector("stage()"):
                    return "0x" + word(pool["stage"])
                if sel == self.selector("challenge()"):
                    return "0x" + addr_word(pool["challenge"] or addr(0))
                if sel == self.selector("violation(uint32[])"):
                    return "0x" + word(pool["violation"])
                if sel == self.selector("isStopped()"):
                    return "0x" + word(pool["stopped"])
        acct = self.accounts.get(call["to"])
        if acct is not None:
            for key, sig in (("status", "status()"), ("deadline", "deadline()"), ("createdAt", "createdAt()"),
                             ("capitalArrived", "capitalArrived()"), ("violation", "violation(uint32[])"),
                             ("stopped", "isStopped()")):
                if sel == self.selector(sig):
                    return "0x" + word(acct[key])
        raise AssertionError(f"the model does not know {to} {sel}")


class KeysWatch(unittest.TestCase):
    def setUp(self):
        self.mod = load("keys_watch")

    def run_with(self, node, *argv):
        with mock.patch.object(self.mod, "_post", node):
            return run_main(self.mod, ["--registry", REGISTRY, "--rpc", PRIMARY, "--rpc-fallback", BACKUP, *argv])

    def test_the_selectors_are_the_ones_of_the_calls_they_name(self):
        self.assertEqual(self.mod.SEL_FREE_COUNT, "0x" + keccak(b"freeCount()").hex()[:8])
        self.assertEqual(self.mod.SEL_BINDING_OF, "0x" + keccak(b"bindingOf(address)").hex()[:8])
        # the array's elements start at keccak256 of the slot, as the model of the registry lays them out
        self.assertEqual(self.mod.FREE_ARRAY_SLOT, 5)

    def test_enough_free_keys_and_none_spoiled_is_quiet(self):
        code, out, err = self.run_with(RegistryNode(self.mod, free=17))
        self.assertEqual(code, 0, err)
        self.assertIn("free keys: 17", out)
        self.assertIn("checked for spoilage: 17 | spoiled: 0", out)
        self.assertTrue(out.rstrip().endswith("QUIET: not fewer keys than the threshold and none spoiled"))

    def test_the_threshold_is_strict_sixteen_is_quiet_and_fifteen_is_a_finding(self):
        # "Threshold 16" is an alarm on the SECOND key lost: 17 laid out, 16 left is still quiet.
        self.assertEqual(self.run_with(RegistryNode(self.mod, free=16), "--threshold", "16")[0], 0)
        code, out, _ = self.run_with(RegistryNode(self.mod, free=15), "--threshold", "16")
        self.assertEqual(code, 1)
        self.assertIn("FINDING: 15 free agent keys, fewer than the threshold 16", out)
        self.assertIn("SEVERITY: critical", out)

    def test_a_spoiled_key_is_a_finding_whatever_the_count(self):
        code, out, _ = self.run_with(RegistryNode(self.mod, free=17, spoiled=(3,)), "--threshold", "16")
        self.assertEqual(code, 1)
        self.assertIn("FINDING: 1 of 17 free keys are spoiled", out)
        self.assertIn(f"spoiled: {addr(0x9003)}", out)

    def test_a_storage_layout_that_does_not_hold_is_could_not_check_never_none_spoiled(self):
        for why, node in (
                ("the slot's length is not freeCount()", RegistryNode(self.mod, free=17, slot_length=12)),
                ("an element is not an address", RegistryNode(self.mod, free=17, element="0x" + "ff" * 32)),
                ("an element is not in the state Free", RegistryNode(self.mod, free=17, state=2)),
                ("the precompile answers 2", RegistryNode(self.mod, free=17, exists_answer=2))):
            code, out, err = self.run_with(node)
            self.assertEqual((code, out.count("QUIET")), (3, 0), why)
            self.assertIn("COULD NOT CHECK", err, why)

    def test_a_finding_already_made_survives_a_spoilage_read_that_fails_and_without_one_it_is_exit_3(self):
        def precompile_dies_after(node, n):
            node.fail_when = lambda method, params: (
                method == "eth_call" and params[0]["to"] == self.mod.CORE_USER_EXISTS and node.exists_calls >= n)
            return node

        # the count is already under the threshold: reported, the spoilage check marked incomplete
        low = precompile_dies_after(RegistryNode(self.mod, free=15), 0)
        code, out, _ = self.run_with(low, "--threshold", "16")
        self.assertEqual(code, 1)
        self.assertIn("FINDING: 15 free agent keys, fewer than the threshold 16", out)
        self.assertIn("INCOMPLETE: the spoilage check stopped after 0 of 15 keys", out)
        # a spoiled key found before the read failed is kept
        part = precompile_dies_after(RegistryNode(self.mod, free=17, spoiled=(1,)), 3)
        code, out, _ = self.run_with(part, "--threshold", "16")
        self.assertEqual(code, 1)
        self.assertIn(f"spoiled: {addr(0x9001)}", out)
        self.assertIn("INCOMPLETE: the spoilage check stopped after 3 of 17 keys", out)
        # nothing found and the read failed: nothing can be said
        none = precompile_dies_after(RegistryNode(self.mod, free=17), 3)
        code, out, err = self.run_with(none, "--threshold", "16")
        self.assertEqual((code, out.count("QUIET")), (3, 0))
        self.assertIn("COULD NOT CHECK", err)

    def test_a_malformed_answer_is_an_unreadable_node_and_does_not_take_a_finding_with_it(self):
        malformed = "0x" + "zz" * 32
        # before any finding: could not check, and the reason is the answer, not a bug of the tool
        code, out, err = self.run_with(RegistryNode(self.mod, head_raw="0xzz"))
        self.assertEqual((code, out.count("QUIET")), (3, 0))
        self.assertIn("the chain's head is not a hexadecimal number", err)
        self.assertNotIn("unexpected", err)
        # with the count already under the threshold: reported, the spoilage check marked incomplete
        code, out, _ = self.run_with(RegistryNode(self.mod, free=15, exists_raw=malformed), "--threshold", "16")
        self.assertEqual(code, 1)
        self.assertIn("FINDING: 15 free agent keys, fewer than the threshold 16", out)
        self.assertIn("INCOMPLETE: the spoilage check stopped after 0 of 15 keys", out)
        self.assertIn("is not a hexadecimal number", out)
        # with the count fine and nothing found, the same answer is exit 3
        code, out, err = self.run_with(RegistryNode(self.mod, free=17, exists_raw=malformed), "--threshold", "16")
        self.assertEqual((code, out.count("QUIET")), (3, 0))
        self.assertIn("is not a hexadecimal number", err)

    def test_a_registry_that_is_not_named_is_could_not_check_there_is_no_default(self):
        with mock.patch.object(self.mod, "_post", RegistryNode(self.mod)), mock.patch.dict(os.environ, {}):
            os.environ.pop("GATEWAY_REGISTRY", None)
            code, _, err = run_main(self.mod, ["--rpc", PRIMARY])
        self.assertEqual(code, 3)
        self.assertIn("no registry is named: GATEWAY_REGISTRY is not set", err)
        for bad in ("0x1234", "not-an-address"):
            with mock.patch.object(self.mod, "_post", RegistryNode(self.mod)):
                self.assertEqual(run_main(self.mod, ["--registry", bad])[0], 3, bad)

    def test_the_registry_is_read_from_the_deployments_env_file_variable(self):
        node = RegistryNode(self.mod)
        with mock.patch.object(self.mod, "_post", node):
            code, out, _ = run_main(self.mod, ["--rpc", PRIMARY, "--rpc-fallback", ""],
                                    {"GATEWAY_REGISTRY": REGISTRY, "COLOSSEUM_DEPLOYMENT": "demo2"})
        self.assertEqual(code, 0)
        self.assertIn(f"deployment demo2 | registry {REGISTRY}", out)

    def test_a_broken_node_goes_to_the_fallback_at_once_and_the_output_says_so(self):
        node = RegistryNode(self.mod)
        node.behaviour[PRIMARY] = "http500"
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 0)
        self.assertIn(f"READ THE FALLBACK NODE {BACKUP}", out)
        self.assertNotIn("(retries:", out, "a broken node is not waited for")

    def test_a_busy_node_is_waited_out_on_the_same_node_and_then_the_fallback_gets_one_try(self):
        # The whole ladder on the first node, then one try on the fallback, which then reads the rest of the run.
        # From the host, 11 Nov 2026: thirdweb's node answered 429 to every call, and the run was exit 3 every time.
        node = RegistryNode(self.mod)
        node.behaviour[PRIMARY] = "busy"
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 0)
        self.assertIn(f"READ THE FALLBACK NODE {BACKUP}", out)
        self.assertIn("busy past the ladder", out)
        asked = [u for u, _ in node.asked]
        self.assertEqual(asked[:8], [PRIMARY] * 7 + [BACKUP], "seven tries on the first node, then the fallback")
        self.assertEqual(set(asked[7:]), {BACKUP}, "the rest of the run reads the fallback")

    def test_busy_on_both_nodes_is_could_not_check_after_one_try_on_the_fallback(self):
        node = RegistryNode(self.mod)
        node.behaviour[PRIMARY] = node.behaviour[BACKUP] = "busy"
        code, _, err = self.run_with(node)
        self.assertEqual(code, 3)
        self.assertIn("7 failures in a row, 63 s of pauses", err)
        self.assertEqual([u for u, _ in node.asked], [PRIMARY] * 7 + [BACKUP])

    def test_a_busy_node_with_no_fallback_is_could_not_check_after_the_ladder(self):
        node = RegistryNode(self.mod)
        node.behaviour[PRIMARY] = "busy"
        code, _, err = self.run_with(node, "--rpc-fallback", "")
        self.assertEqual(code, 3)
        self.assertIn("7 failures in a row, 63 s of pauses", err)
        self.assertEqual({u for u, _ in node.asked}, {PRIMARY})

    def test_the_nodes_are_the_deployments_record_and_watch_env_names_one_only_where_it_names_none(self):
        hl, backup = "https://record.example/evm", "https://record-backup.example/evm"
        rec = load("keys_watch", {"COLOSSEUM_RPC_URL": hl, "COLOSSEUM_RPC_BACKUP_URL": backup,
                                  "POOLS_WATCH_KEYS_RPC": "https://hand.example", "POOLS_WATCH_KEYS_RPC_FALLBACK": "https://hand2.example"})
        self.assertEqual((rec.RPC_URL, rec.RPC_FALLBACK_URL), (hl, backup), "the record wins")
        hand = load("keys_watch", {"POOLS_WATCH_KEYS_RPC": "https://hand.example", "POOLS_WATCH_KEYS_RPC_FALLBACK": "https://hand2.example"})
        self.assertEqual((hand.RPC_URL, hand.RPC_FALLBACK_URL), ("https://hand.example", "https://hand2.example"))
        bare = load("keys_watch")
        self.assertEqual((bare.RPC_URL, bare.RPC_FALLBACK_URL), ("https://rpc.hyperliquid-testnet.xyz/evm", ""))
        self.assertNotIn("thirdweb.com", (DOGS / "keys_watch.py").read_text())

    def test_a_broken_node_with_no_fallback_is_could_not_check(self):
        node = RegistryNode(self.mod)
        node.behaviour[PRIMARY] = "http500"
        code, _, err = self.run_with(node, "--rpc-fallback", "")
        self.assertEqual(code, 3)
        self.assertIn("7 failures in a row", err)

    def test_an_answer_that_is_no_envelope_is_could_not_check(self):
        node = RegistryNode(self.mod)
        node.behaviour[PRIMARY] = "garbage"
        node.behaviour[BACKUP] = "garbage"
        self.assertEqual(self.run_with(node)[0], 3)

    def test_a_crash_of_the_tool_is_could_not_check_and_never_a_finding(self):
        # Python exits 1 on an uncaught exception, and 1 is "a finding" to the wrapper. A bug of the tool is not
        # a node's answer: it stays "unexpected", where a malformed answer is an unreadable node (below).
        node = RegistryNode(self.mod)

        def bug(method, params):
            raise RuntimeError("a bug of the tool")
        node.answer = bug
        code, _, err = self.run_with(node)
        self.assertEqual(code, 3)
        self.assertIn("COULD NOT CHECK: unexpected RuntimeError", err)


class KeeperWatch(unittest.TestCase):
    def setUp(self):
        self.mod = load("keeper_watch")

    def run_with(self, node, *argv, env=None):
        with mock.patch.object(self.mod, "_post", node):
            return run_main(self.mod, ["--factory", FACTORY, "--rpc", PRIMARY, "--rpc-fallback", BACKUP, *argv],
                            env)

    def chain(self, now=1_000_000):
        return FactoryNode(self.mod, now=now)

    def test_nothing_to_do_is_quiet(self):
        node = self.chain()
        node.add_pool(stage=self.mod.IDLE)
        code, out, err = self.run_with(node)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.rstrip().endswith("QUIET: for the live accounts, everything the keeper is bound to do is done"))
        self.assertIn("pools: 1 | live accounts: 0", out)

    def test_an_active_challenge_past_its_deadline_plus_fifteen_minutes_is_a_finding_and_not_before(self):
        grace = self.mod.GRACE_S
        for late, expected in ((grace, 0), (grace + 1, 1)):  # strictly more than 15 minutes
            node = self.chain(now=1_000_000)
            node.add_pool(challenge=addr(0xC1), status=self.mod.ACTIVE, deadline=1_000_000 - late)
            code, out, _ = self.run_with(node)
            self.assertEqual(code, expected, f"{late} s late")
        self.assertIn(f"FINDING: expire|{addr(0xC1)}", out)
        self.assertIn("the keeper did not close it on time", out)

    def test_a_created_challenge_older_than_fifteen_minutes_is_a_finding_and_says_if_the_capital_came(self):
        for arrived, words in ((1, "arrived: `activate` is due"), (0, "has NOT arrived")):
            node = self.chain()
            node.add_pool(challenge=addr(0xC2), status=self.mod.CREATED, createdAt=1_000_000 - 3600,
                          capitalArrived=arrived)
            code, out, _ = self.run_with(node)
            self.assertEqual(code, 1)
            self.assertIn("the keeper did not activate it", out)
            self.assertIn(words, out)
        node = self.chain()
        node.add_pool(challenge=addr(0xC2), status=self.mod.CREATED, createdAt=1_000_000 - 60)
        self.assertEqual(self.run_with(node)[0], 0, "a minute old is not late")

    def test_a_violation_on_a_live_account_that_is_not_stopped_is_a_finding_and_a_stopped_one_is_not(self):
        node = self.chain()
        node.add_pool(challenge=addr(0xC3), status=self.mod.ACTIVE, deadline=2_000_000, violation=1, stopped=0)
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 1)
        self.assertIn("the keeper did not stop it", out)
        self.assertIn("violation() = Drawdown", out)
        stopped = self.chain()
        stopped.add_pool(challenge=addr(0xC3), status=self.mod.ACTIVE, deadline=2_000_000, violation=1, stopped=1)
        self.assertEqual(self.run_with(stopped)[0], 0)

    def test_a_finding_already_made_survives_a_later_read_that_fails_and_without_one_it_is_exit_3(self):
        def broken_pool(node, pool):
            node.fail_when = lambda method, params: (
                method == "eth_call" and params[0]["to"].lower() == pool["address"].lower())

        node = self.chain()
        node.add_pool(challenge=addr(0xC5), status=self.mod.ACTIVE, deadline=1_000_000 - 3600)
        second = node.add_pool(stage=self.mod.IDLE)
        broken_pool(node, second)
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 1, "the breach already seen is not hidden by a pool that cannot be read")
        self.assertIn(f"FINDING: expire|{addr(0xC5)}", out)
        self.assertIn(f"INCOMPLETE: the run stopped at pool {second['address']}, 1 of 2 pools not fully read", out)
        quiet = self.chain()
        quiet.add_pool(stage=self.mod.IDLE)
        second = quiet.add_pool(stage=self.mod.IDLE)
        broken_pool(quiet, second)
        code, out, err = self.run_with(quiet)
        self.assertEqual((code, out.count("QUIET")), (3, 0), "no finding, and a read failed: nothing can be said")
        self.assertIn("this is NOT a finding about the keeper", err)

    def test_a_malformed_answer_after_a_finding_does_not_take_it_with_it_and_without_one_it_is_exit_3(self):
        malformed = "0x" + "zz" * 32
        node = self.chain()
        node.add_pool(challenge=addr(0xC6), status=self.mod.ACTIVE, deadline=1_000_000 - 3600)
        second = node.add_pool(stage=self.mod.IDLE)
        node.malformed[second["address"].lower()] = malformed
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 1)
        self.assertIn(f"FINDING: expire|{addr(0xC6)}", out)
        self.assertIn(f"INCOMPLETE: the run stopped at pool {second['address']}", out)
        self.assertIn("is not a hexadecimal number", out)
        quiet = self.chain()
        quiet.add_pool(stage=self.mod.IDLE)
        second = quiet.add_pool(stage=self.mod.IDLE)
        quiet.malformed[second["address"].lower()] = malformed
        code, out, err = self.run_with(quiet)
        self.assertEqual((code, out.count("QUIET")), (3, 0))
        self.assertIn("is not a hexadecimal number", err)
        self.assertNotIn("unexpected", err)

    def test_a_funded_pool_that_breaches_is_a_finding(self):
        node = self.chain()
        node.add_pool(stage=self.mod.FUNDED, pool_violation=2, pool_stopped=0)
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 1)
        self.assertIn("is in stage Funded: violation() = DailyLoss", out)

    def test_a_closed_account_is_not_looked_at_at_all(self):
        # On a stopped account violation() reads the empty account as a drawdown: an alarm on every run.
        node = self.chain()
        node.add_pool(challenge=addr(0xC4), status=self.mod.SETTLED, violation=1)
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 0)
        self.assertIn("closed accounts skipped: 1", out)

    def test_a_node_that_does_not_answer_is_could_not_check_never_a_finding_about_the_keeper(self):
        node = self.chain()
        node.add_pool(challenge=addr(0xC3), status=self.mod.ACTIVE, deadline=2_000_000, violation=1)
        node.behaviour[PRIMARY] = "http500"
        node.behaviour[BACKUP] = "http500"
        code, out, err = self.run_with(node)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("this is NOT a finding about the keeper", err)

    def test_an_exhausted_daily_allowance_goes_to_the_fallback_without_a_pause(self):
        # "More than 3000 archived blocks queried in one day": a minute's pause does not lift it.
        node = self.chain()
        node.add_pool(stage=self.mod.IDLE)
        node.behaviour[PRIMARY] = "quota"
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 0)
        self.assertIn(f"READ THE FALLBACK NODE {BACKUP}", out)
        self.assertEqual([u for u, _ in node.asked].count(PRIMARY), 1, "left the first node at the first refusal")

    def test_a_busy_node_is_waited_out_and_then_the_fallback_gets_one_try(self):
        node = self.chain()
        node.add_pool(stage=self.mod.IDLE)
        node.behaviour[PRIMARY] = "busy"
        code, out, _ = self.run_with(node)
        self.assertEqual(code, 0)
        self.assertIn(f"READ THE FALLBACK NODE {BACKUP}", out)
        self.assertEqual([u for u, _ in node.asked][:8], [PRIMARY] * 7 + [BACKUP])

    def test_busy_on_both_nodes_is_could_not_check_after_one_try_on_the_fallback(self):
        node = self.chain()
        node.behaviour[PRIMARY] = node.behaviour[BACKUP] = "busy"
        code, _, err = self.run_with(node)
        self.assertEqual(code, 3)
        self.assertIn("7 failures in a row, 63 s of pauses", err)
        self.assertEqual([u for u, _ in node.asked], [PRIMARY] * 7 + [BACKUP])

    def test_an_exhausted_allowance_with_no_fallback_is_could_not_check(self):
        node = self.chain()
        node.behaviour[PRIMARY] = "quota"
        code, _, err = self.run_with(node, "--rpc-fallback", "")
        self.assertEqual(code, 3)
        self.assertIn("in one day", err)

    def test_the_factory_is_read_from_the_deployments_env_file_variable_and_has_no_default(self):
        node = self.chain()
        node.add_pool(stage=self.mod.IDLE)
        with mock.patch.object(self.mod, "_post", node):
            code, out, _ = run_main(self.mod, ["--rpc", PRIMARY, "--rpc-fallback", ""],
                                    {"GATEWAY_FACTORY": FACTORY, "COLOSSEUM_DEPLOYMENT": "v3-demo3"})
        self.assertEqual(code, 0)
        self.assertIn(f"deployment v3-demo3 | factory {FACTORY}", out)
        with mock.patch.object(self.mod, "_post", node), mock.patch.dict(os.environ, {}):
            os.environ.pop("GATEWAY_FACTORY", None)
            code, _, err = run_main(self.mod, ["--rpc", PRIMARY])
        self.assertEqual(code, 3)
        self.assertIn("no factory is named: GATEWAY_FACTORY is not set", err)

    def test_a_crash_of_the_tool_is_could_not_check_and_never_a_finding(self):
        node = self.chain()
        node.add_pool(stage=self.mod.IDLE)

        def bug(method, params):
            raise RuntimeError("a bug of the tool")
        node.answer = bug
        code, _, err = self.run_with(node)
        self.assertEqual(code, 3)
        self.assertIn("COULD NOT CHECK: unexpected RuntimeError", err)

    def test_the_fallback_node_is_the_one_the_deployments_record_names_and_none_is_written_in(self):
        backup = "https://record.example/evm"
        self.assertEqual(load("keeper_watch", {"COLOSSEUM_RPC_BACKUP_URL": backup}).RPC_FALLBACK_URL, backup)
        # an override by hand wins over the record's line
        both = load("keeper_watch", {"COLOSSEUM_RPC_BACKUP_URL": backup, "POOLS_WATCH_RPC_FALLBACK": "https://hand.example"})
        self.assertEqual(both.RPC_FALLBACK_URL, "https://hand.example")
        # a record that names none leaves the watchdog on its first node alone
        self.assertEqual(load("keeper_watch").RPC_FALLBACK_URL, "")
        self.assertNotIn("thirdweb", (DOGS / "keeper_watch.py").read_text())

    def test_the_variables_that_move_the_two_checks_by_hand_have_names_of_their_own(self):
        # Both checks read Hyperliquid's node first since 11 Nov 2026 (the keys check took the record's node when
        # thirdweb's refused the host every call), and the record's second node after it. A line in watch.env still
        # moves one check and not the other.
        self.assertNotEqual("POOLS_WATCH_KEYS_RPC", "POOLS_WATCH_RPC")
        self.assertIn('"POOLS_WATCH_KEYS_RPC"', (DOGS / "keys_watch.py").read_text())
        self.assertIn('"POOLS_WATCH_RPC"', (DOGS / "keeper_watch.py").read_text())


class Ladder(unittest.TestCase):
    def test_the_ladder_outlasts_the_nodes_rate_window_and_there_is_one_for_both_checks(self):
        from importlib import import_module  # noqa: F401  (the module is loaded by path below)
        mod = load("keys_watch").backoff
        self.assertEqual((mod.ATTEMPTS, mod.PAUSES_S, mod.LADDER_S), (7, (1, 2, 4, 8, 16, 32), 63))
        self.assertGreater(mod.LADDER_S, mod.RATE_WINDOW_S)
        self.assertIs(load("keys_watch").backoff, load("keeper_watch").backoff)
        for script in ("keys_watch.py", "keeper_watch.py"):
            text = (DOGS / script).read_text()
            self.assertNotRegex(text, r"time\.sleep|PAUSES_S\s*=", script + " has no ladder of its own")

    def test_a_run_never_starts_a_pause_that_would_end_past_its_budget(self):
        backoff = load("keys_watch").backoff
        now = [0.0]
        ladder = backoff.Ladder(real=False, clock=lambda: now[0])
        now[0] = backoff.RUN_BUDGET_S - 3
        self.assertTrue(ladder.again(0))      # a pause of 1 s still fits
        self.assertTrue(ladder.again(1))      # 2 s: exactly on the budget
        self.assertFalse(ladder.again(2))     # 4 s would pass it
        self.assertTrue(ladder.out_of_budget)
        self.assertIn("budget of 240 s is used up", ladder.gave_up(3))


class Wrapper(unittest.TestCase):
    """watchdog-run.sh sorts the outcomes: quiet clears the flag, a finding raises it, "could not check" touches it
    in neither direction."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = pathlib.Path(tmp.name)
        self.flag = self.state / "probe.alarm"

    def wrap(self, code: int, text: str = "the check's last line", env: dict[str, str] | None = None):
        out = subprocess.run(["bash", str(RUNNER), "probe", "bash", "-c", f"printf '%s\\n' {text!r}; exit {code}"],
                             capture_output=True, text=True,
                             env={**os.environ, "COLOSSEUM_WATCH_DIR": str(self.state), **(env or {})})
        return out

    def test_quiet_clears_a_flag_that_was_up_and_says_so(self):
        self.flag.write_text("old alarm\n")
        out = self.wrap(0)
        self.assertEqual(out.returncode, 0)
        self.assertFalse(self.flag.exists())
        self.assertIn("probe: RECOVERED", out.stdout)
        self.assertIn("probe: quiet: the check's last line", out.stdout)

    def test_a_finding_raises_the_flag_with_the_finding_in_it_and_does_not_redden_the_unit(self):
        out = self.wrap(1, "FINDING: x")
        self.assertEqual(out.returncode, 0, "a finding is a successful check")
        self.assertIn("FINDING: x", self.flag.read_text())
        self.assertRegex(self.flag.read_text().splitlines()[0], r"^at: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("probe: ALARM: finding", out.stdout)
        self.assertIn("no alert channel on the host (COLOSSEUM_ALERT_CMD is not set)", out.stdout)

    def test_a_flag_that_cannot_be_written_turns_the_unit_red_and_still_says_and_sends_the_finding(self):
        # The state directory is a file here: mktemp cannot make anything under it.
        broken = self.state / "not-a-directory"
        broken.write_text("")
        sink = self.state / "sink.txt"
        out = subprocess.run(["bash", str(RUNNER), "probe", "bash", "-c", "echo 'FINDING: z'; exit 1"],
                             capture_output=True, text=True,
                             env={**os.environ, "COLOSSEUM_WATCH_DIR": str(broken), "COLOSSEUM_ALERT_CMD": f"cat > {sink}"})
        self.assertEqual(out.returncode, 4, "a unit that cannot keep its alarm is broken, not successful")
        self.assertIn("probe: ALARM: finding", out.stdout)
        self.assertIn("probe:   FINDING: z", out.stdout)
        self.assertIn("ERROR: the flag", out.stdout)
        self.assertIn("could not be written", out.stdout)
        self.assertIn("the channel took the alarm", out.stdout)
        self.assertIn("FINDING: z", sink.read_text(), "the channel is still told")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes where it is told not to")
    def test_a_flag_that_stood_is_left_as_it_was_when_the_new_one_cannot_be_written(self):
        self.flag.write_text("the alarm that stood\n")
        self.state.chmod(0o500)
        self.addCleanup(self.state.chmod, 0o700)
        out = self.wrap(1, "FINDING: z")
        self.assertEqual(out.returncode, 4)
        self.assertEqual(self.flag.read_text(), "the alarm that stood\n")
        self.assertEqual([p.name for p in self.state.iterdir()], ["probe.alarm"], "no temporary file left behind")

    def fake_bin(self, **scripts) -> dict[str, str]:
        """An environment whose PATH starts with a directory of `scripts`: commands that do as the test says."""
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for name, body in scripts.items():
            f = d / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)
        return {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"}

    def test_a_write_that_fails_after_the_temporary_file_is_made_is_the_same_failure(self):
        # a mktemp that hands back a directory: the write cannot open it
        self.flag.write_text("the alarm that stood\n")
        out = self.wrap(1, "FINDING: z", self.fake_bin(mktemp=f'echo "{self.state}"'))
        self.assertEqual(out.returncode, 4)
        self.assertEqual(self.flag.read_text(), "the alarm that stood\n")
        self.assertIn("ERROR: the flag", out.stdout)
        self.assertIn("the alarm is in the journal only", out.stdout)
        self.assertNotIn("the flag and the journal", out.stdout, "it must not claim a flag it could not write")

    def test_a_move_that_fails_leaves_the_flag_that_stood_and_no_temporary_file(self):
        self.flag.write_text("the alarm that stood\n")
        out = self.wrap(1, "FINDING: z", self.fake_bin(mv="exit 1"))
        self.assertEqual(out.returncode, 4)
        self.assertEqual(self.flag.read_text(), "the alarm that stood\n")
        self.assertEqual([p.name for p in self.state.iterdir()], ["probe.alarm"], "no temporary file left behind")
        self.assertIn("the alarm is in the journal only", out.stdout)

    def test_the_flag_is_written_by_one_write_so_that_its_status_is_the_writes_own(self):
        # Two printf's in a { } group report the status of the second: the first could fail and the flag still be
        # moved into place, short. One printf has one status.
        code = "\n".join(l for l in RUNNER.read_text().splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\{\s*printf[^\n]*;\s*printf")
        self.assertIn("the alarm is in the flag and the journal only", self.wrap(1, "FINDING: y").stdout)

    def test_code_3_touches_the_flag_in_neither_direction(self):
        for was_up in (True, False):
            self.flag.unlink(missing_ok=True)
            if was_up:
                self.flag.write_text("a live alarm\n")
            out = self.wrap(3, "COULD NOT CHECK: node")
            self.assertEqual(out.returncode, 3)
            self.assertEqual(self.flag.exists(), was_up, f"flag was up: {was_up}")
            if was_up:
                self.assertEqual(self.flag.read_text(), "a live alarm\n")
            self.assertIn("COULD NOT CHECK (code 3)", out.stdout)
            self.assertIn("the flag is untouched", out.stdout)

    def test_an_unknown_code_is_loud_and_leaves_the_flag_alone(self):
        self.flag.write_text("a live alarm\n")
        out = self.wrap(7)
        self.assertEqual(out.returncode, 7)
        self.assertEqual(self.flag.read_text(), "a live alarm\n")
        self.assertIn("UNKNOWN CODE 7", out.stdout)

    def test_an_alert_channel_gets_the_finding_on_stdin_and_a_channel_that_refuses_is_not_called_sent(self):
        sink = self.state / "sink.txt"
        out = self.wrap(1, "FINDING: y", {"COLOSSEUM_ALERT_CMD": f"cat > {sink}"})
        self.assertIn("FINDING: y", sink.read_text())
        self.assertIn("the channel took the alarm", out.stdout)
        out = self.wrap(1, "FINDING: y", {"COLOSSEUM_ALERT_CMD": "exit 5"})
        self.assertIn("WARNING: the channel did not take the alarm", out.stdout)
        self.assertNotIn("the channel took the alarm", out.stdout)
        self.assertTrue(self.flag.exists(), "the flag is there whatever the channel did")

    def test_it_needs_a_name_and_a_command(self):
        out = subprocess.run(["bash", str(RUNNER), "only-a-name"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 64)


def directives(path: pathlib.Path) -> list[str]:
    text = path.read_text().replace("\\\n", " ")
    return [" ".join(l.split()) for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def exec_start(path: pathlib.Path) -> str:
    return [l for l in directives(path) if l.startswith("ExecStart=")][0]


class Units(unittest.TestCase):
    def test_the_directory_holds_what_install_ships_and_nothing_else(self):
        names = {p.name for p in DOGS.iterdir() if p.is_file()}
        expected = set(RUNTIME) | {"install-lib.sh"} | {f"{u}{ext}" for u in SERVICES for ext in (".service", ".timer")}
        self.assertEqual(names, expected, "a file nobody installs, or one that is missing")

    def test_it_is_public_text_in_english_with_no_internal_path_in_it(self):
        for path in (p for p in DOGS.iterdir() if p.is_file()):
            text = path.read_text()
            self.assertIsNone(re.search("[\u0400-\u04FF]", text), f"{path.name}: not English")
            for private in ("_hu" + "b/", "usenami-" + "platform"):  # written split: this file is public too
                self.assertNotIn(private, text, path.name)

    def test_the_wrapper_and_the_checks_are_executable_where_a_unit_runs_them(self):
        self.assertTrue(os.access(RUNNER, os.X_OK), "ExecStart runs the wrapper directly")
        self.assertTrue(os.access(DOGS / "keys_watch.py", os.R_OK))
        self.assertTrue(os.access(DOGS / "keeper_watch.py", os.R_OK))
        self.assertTrue(stat.S_IMODE(RUNNER.stat().st_mode) & stat.S_IXUSR)

    def test_every_unit_runs_a_file_the_release_ships_from_the_watchdogs_own_directory(self):
        for unit in SERVICES:
            path = DOGS / f"{unit}.service"
            lines = directives(path)
            start = exec_start(path)
            self.assertIn("ExecStart=/opt/colosseum-watch/current/watchdog-run.sh ", start, unit)
            self.assertIn("/opt/colosseum-pools/venv/bin/python /opt/colosseum-watch/current/", start, unit)
            for token in start.split():
                if token.startswith("/opt/colosseum-watch/current/"):
                    self.assertTrue((DOGS / token.rsplit("/", 1)[1]).is_file(), f"{unit}: {token} is not shipped")
            self.assertIn("WorkingDirectory=/opt/colosseum-watch", lines, unit)
            # Not in the pools release: a rollback goes to one that does not carry the watchdogs.
            self.assertEqual([l for l in lines if "/opt/colosseum-pools/current" in l], [], unit)

    def test_the_wrapper_names_are_the_flags_the_hand_made_install_kept(self):
        names = {"colosseum-watch-keys": "keys", "colosseum-watch-keeper": "keeper",
                 "colosseum-watch-keys-demo3": "keys-demo3", "colosseum-watch-keeper-demo3": "keeper-demo3"}
        for unit, flag in names.items():
            self.assertIn(f"watchdog-run.sh {flag} ", exec_start(DOGS / f"{unit}.service"), unit)

    def test_the_two_checks_of_a_set_run_one_script_each_and_the_threshold_is_written_as_a_number(self):
        for suffix in ("", "-demo3"):
            self.assertIn("keys_watch.py --threshold 16", exec_start(DOGS / f"colosseum-watch-keys{suffix}.service"))
            self.assertTrue(exec_start(DOGS / f"colosseum-watch-keeper{suffix}.service").endswith("keeper_watch.py"))

    def test_each_set_reads_its_deployments_env_file_last_and_without_a_dash(self):
        for unit in FIRST_SET:
            envs = [l for l in directives(DOGS / f"{unit}.service") if l.startswith("EnvironmentFile=")]
            self.assertEqual(envs, ["EnvironmentFile=-/etc/colosseum/watch.env",
                                    "EnvironmentFile=/etc/colosseum/deployment.env"], unit)
        for unit in DEMO3_SET:
            envs = [l for l in directives(DOGS / f"{unit}.service") if l.startswith("EnvironmentFile=")]
            self.assertEqual(envs, ["EnvironmentFile=-/etc/colosseum/watch.env",
                                    "EnvironmentFile=/etc/colosseum/deployment-demo3.env"], unit)

    def test_they_are_held_as_tightly_as_the_keeper_and_see_neither_its_keys_nor_the_gateways(self):
        own = ("Description=", "EnvironmentFile=", "ExecStart=", "ReadWritePaths=", "InaccessiblePaths=", "RestartSec=",
               "WorkingDirectory=", "User=", "Group=", "Restart=", "WantedBy=", "Environment=", "SuccessExitStatus=",
               "TimeoutStartSec=", "Type=")
        keeper = [l for l in directives(HOST / "colosseum-keeper.service")
                  if not l.startswith(own) and l not in ("[Install]",)]
        for unit in SERVICES:
            lines = set(directives(DOGS / f"{unit}.service"))
            self.assertEqual([l for l in keeper if l not in lines], [], unit)
            self.assertIn("User=colosseum-watch", lines)
            self.assertIn("ReadWritePaths=/var/lib/colosseum-watch", lines)
            self.assertEqual([l for l in lines if l.startswith("ReadWritePaths=")], ["ReadWritePaths=/var/lib/colosseum-watch"])
            self.assertIn("InaccessiblePaths=/var/lib/colosseum-keeper /var/lib/colosseum-gw", lines)
            self.assertIn("Type=oneshot", lines)
            # a finding (code 1) is a successful check: only a broken tool reddens the unit
            self.assertIn("SuccessExitStatus=1", lines)

    def test_a_unit_may_run_longer_than_the_runs_budget_by_a_request(self):
        budget = load("keys_watch").backoff.RUN_BUDGET_S
        for unit in SERVICES:
            timeout = [l for l in directives(DOGS / f"{unit}.service") if l.startswith("TimeoutStartSec=")]
            self.assertEqual(len(timeout), 1, unit)
            self.assertGreaterEqual(int(timeout[0].split("=")[1]), budget + 25 + 5, unit)

    def test_the_timers_run_every_quarter_hour_at_minutes_of_their_own_and_catch_up_once(self):
        minutes = {}
        for unit in SERVICES:
            lines = directives(DOGS / f"{unit}.timer")
            cal = [l for l in lines if l.startswith("OnCalendar=")]
            self.assertEqual(len(cal), 1, unit)
            minutes[unit] = cal[0]
            self.assertIn("Persistent=true", lines, unit)
            self.assertIn(f"Unit={unit}.service", lines, unit)
            self.assertIn("WantedBy=timers.target", lines, unit)
        # 00, 03, 07 and 11 of each quarter hour: no two watchdogs hit one public node in the same second
        self.assertEqual(minutes, {"colosseum-watch-keys": "OnCalendar=*:00/15",
                                   "colosseum-watch-keeper": "OnCalendar=*:07/15",
                                   "colosseum-watch-keys-demo3": "OnCalendar=*:03/15",
                                   "colosseum-watch-keeper-demo3": "OnCalendar=*:11/15"})


class Record(unittest.TestCase):
    """What the units read is what install.sh writes from the deployment's record."""

    def test_the_variables_the_checks_read_are_lines_of_the_file_the_record_renders(self):
        for label in ("demo2", "v3-demo3"):
            rendered = dict(l.split("=", 1) for l in host_instances.deployment_env(label).splitlines())
            record = deployments.load(label)
            self.assertEqual(rendered["GATEWAY_REGISTRY"], record["KeyRegistry"], label)
            self.assertEqual(rendered["GATEWAY_FACTORY"], record["PoolFactory"], label)
            self.assertEqual(rendered["COLOSSEUM_DEPLOYMENT"], label)
            # The record names the keepers' nodes and the file carries the backup one under the name the keeper
            # check reads its second node from: the wire between the two is this one variable.
            self.assertIn("COLOSSEUM_RPC_BACKUP_URL", rendered, label)
            wired = load("keeper_watch", {"COLOSSEUM_RPC_BACKUP_URL": rendered["COLOSSEUM_RPC_BACKUP_URL"]})
            self.assertEqual(wired.RPC_FALLBACK_URL, rendered["COLOSSEUM_RPC_BACKUP_URL"], label)
        self.assertIn('"GATEWAY_REGISTRY"', (DOGS / "keys_watch.py").read_text())
        self.assertIn('"GATEWAY_FACTORY"', (DOGS / "keeper_watch.py").read_text())

    def test_the_files_the_units_read_are_the_ones_install_writes(self):
        install = (HOST / "install.sh").read_text()
        self.assertIn('deployment_env "$first_label" /etc/colosseum/deployment.env', install)
        self.assertIn('deployment_env "$label" "/etc/colosseum/deployment-$name.env"', install)


# -- install.sh puts them in place --------------------------------------------------------------------

class Install(unittest.TestCase):
    """The library install.sh sources, run on a directory standing for the host, with a `systemctl` that writes down
    what it was asked and answers as the test says."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.rel = self.tmp / "release"
        shutil.copytree(DOGS, self.rel / "ops" / "host" / "watchdogs")
        self.host = {name: self.tmp / name for name in ("opt", "state", "units", "etc", "bin")}
        for path in self.host.values():
            path.mkdir()
        (self.host["etc"] / "deployment.env").write_text("COLOSSEUM_DEPLOYMENT=demo2\n")
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        self.fakes()

    def fakes(self):
        bin_dir = self.host["bin"]
        scripts = {
            "systemctl": '''#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
case "$1" in
  is-active) echo "${FAKE_ACTIVE:-inactive}" ;;
  show) case "$3" in
          Result) case "$5" in "$FAKE_BAD_UNIT") echo exit-code ;; *) echo success ;; esac ;;
          ExecMainStatus) case "$5" in "$FAKE_BAD_UNIT") echo 3 ;; *) echo 0 ;; esac ;;
        esac ;;
esac
exit 0
''',
            "id": '#!/bin/sh\necho "id $*" >> "$FAKE_LOG"\n[ -z "$FAKE_NO_USER" ]\n',
            "useradd": '#!/bin/sh\necho "useradd $*" >> "$FAKE_LOG"\n',
            "journalctl": '#!/bin/sh\necho "journalctl $*" >> "$FAKE_LOG"\necho "the watchdog said its last line"\n',
            "sleep": "#!/bin/sh\nexit 0\n",
        }
        for name, body in scripts.items():
            f = bin_dir / name
            f.write_text(body)
            f.chmod(0o755)

    def run_install(self, instances="", sha=SHA, smoke=False, env=None, extra="", wait=0):
        script = f'''set -euo pipefail
say() {{ printf 'install: %s\\n' "$*"; }}
fail() {{ printf 'install: %s\\n' "$*" >&2; exit 1; }}
. "{LIB}"
watch_base="{self.host['opt']}"; watch_state="{self.host['state']}"; unit_dir="{self.host['units']}"
etc_dir="{self.host['etc']}"; root_own=""; watch_own=""; watch_smoke_wait={wait}
{extra}
watchdogs_install "{self.rel}" "{sha}" "{instances}"
{'watchdogs_smoke' if smoke else ''}
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{self.host['bin']}{os.pathsep}{os.environ['PATH']}",
                                   "FAKE_LOG": str(self.log), **(env or {})})

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def units_installed(self) -> set[str]:
        return {p.name for p in self.host["units"].iterdir()}

    def test_the_first_deployments_pair_is_installed_from_the_release_and_started_in_order(self):
        out = self.run_install()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.units_installed(), {f"{u}{e}" for u in FIRST_SET for e in (".service", ".timer")})
        tree = self.host["opt"] / "releases" / SHA
        self.assertEqual(subprocess.run(["diff", "-r", str(DOGS), str(tree)]).returncode, 0, "the release's own files")
        current = self.host["opt"] / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(os.readlink(current), f"releases/{SHA}")
        self.assertTrue(os.access(tree / "watchdog-run.sh", os.X_OK))
        self.assertTrue((current / "keys_watch.py").is_file(), "the path the units name")
        calls = self.calls()
        reload_at = calls.index("systemctl daemon-reload")
        enable_at = next(i for i, c in enumerate(calls) if c.startswith("systemctl enable"))
        restart_at = next(i for i, c in enumerate(calls) if c.startswith("systemctl restart"))
        self.assertLess(reload_at, enable_at)
        self.assertLess(enable_at, restart_at)
        self.assertEqual(calls[enable_at], "systemctl enable --quiet colosseum-watch-keys.timer colosseum-watch-keeper.timer")
        self.assertEqual(calls[restart_at], "systemctl restart colosseum-watch-keys.timer colosseum-watch-keeper.timer")
        self.assertIn("install: watchdogs: current -> releases/" + SHA, out.stdout)

    def test_an_instance_gets_its_pair_when_its_env_file_is_here_and_the_release_ships_units_for_it(self):
        (self.host["etc"] / "deployment-demo3.env").write_text("COLOSSEUM_DEPLOYMENT=v3-demo3\n")
        out = self.run_install(instances="demo3 v3-demo3 8788\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.units_installed(), {f"{u}{e}" for u in SERVICES for e in (".service", ".timer")})
        enable = next(c for c in self.calls() if c.startswith("systemctl enable"))
        for unit in SERVICES:
            self.assertIn(f"{unit}.timer", enable)

    def test_an_instance_without_its_env_file_or_without_units_in_the_release_is_left_out_and_said_so(self):
        out = self.run_install(instances="demo3 v3-demo3 8788\ndemo9 some-label 8789\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.units_installed(), {f"{u}{e}" for u in FIRST_SET for e in (".service", ".timer")})
        self.assertIn("instance demo3: no ", out.stdout)
        self.assertIn("deployment-demo3.env, so its watchdogs are not installed", out.stdout)
        self.assertIn("instance demo9: the release ships no watchdogs for it", out.stdout)

    def test_without_the_first_deployments_env_file_its_pair_is_not_installed(self):
        (self.host["etc"] / "deployment.env").unlink()
        out = self.run_install()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.units_installed(), set())
        self.assertIn("deployment.env: the first deployment's watchdogs are not installed", out.stdout)
        self.assertNotIn("systemctl enable", "\n".join(self.calls()), "no timer is enabled for nothing")

    def nothing_was_switched(self):
        self.assertEqual(self.units_installed(), set())
        self.assertFalse((self.host["opt"] / "current").exists())
        self.assertNotIn("systemctl daemon-reload", self.calls())
        self.assertFalse(any(c.startswith("systemctl enable") or c.startswith("systemctl restart") for c in self.calls()))

    def test_a_release_without_the_watchdogs_stops_the_install_before_anything_is_touched(self):
        shutil.rmtree(self.rel / "ops" / "host" / "watchdogs")
        out = self.run_install()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(f"release {SHA} carries no ops/host/watchdogs; nothing switched", out.stderr)
        self.nothing_was_switched()

    def test_a_release_that_lacks_a_file_a_unit_runs_or_a_unit_of_a_pair_stops_it_too(self):
        (self.rel / "ops" / "host" / "watchdogs" / "keeper_watch.py").unlink()
        out = self.run_install()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("carries no ops/host/watchdogs/keeper_watch.py; nothing switched", out.stderr)
        self.nothing_was_switched()
        (self.rel / "ops" / "host" / "watchdogs" / "keeper_watch.py").write_text("")
        (self.rel / "ops" / "host" / "watchdogs" / "colosseum-watch-keeper.timer").unlink()
        out = self.run_install()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("carries no complete unit colosseum-watch-keeper (.service and .timer); nothing switched", out.stderr)
        self.nothing_was_switched()

    def test_a_copy_that_is_not_the_releases_own_stops_the_install_and_leaves_no_half_tree(self):
        # A `cp` that cuts a file short, as a full disk would.
        cp = self.host["bin"] / "cp"
        cp.write_text('#!/bin/sh\n/bin/cp "$@" || exit $?\nfor last; do :; done\n: > "$last/keys_watch.py"\n')
        cp.chmod(0o755)
        out = self.run_install()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is not the release's own; nothing switched", out.stderr)
        self.nothing_was_switched()
        self.assertEqual(list((self.host["opt"] / "releases").glob("*")), [], "no .partial left behind")

    def test_a_host_without_diff_installs_just_the_same_the_copy_is_compared_by_checksums(self):
        for tool in ("diff", "cmp"):
            fake = self.host["bin"] / tool
            fake.write_text("#!/bin/sh\nexit 127\n")
            fake.chmod(0o755)
        out = self.run_install()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((self.host["opt"] / "current" / "keys_watch.py").is_file())

    def test_the_user_is_made_when_it_is_missing_and_only_then(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.assertFalse(any(c.startswith("useradd") for c in self.calls()))
        self.log.write_text("")
        out = self.run_install(env={"FAKE_NO_USER": "1"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("useradd --system --no-create-home --home-dir /nonexistent --shell /sbin/nologin colosseum-watch",
                      self.calls())
        self.assertIn("install: user colosseum-watch created", out.stdout)

    def test_what_a_hand_made_install_left_is_removed_and_the_flags_of_the_state_directory_stay(self):
        old = self.host["opt"]
        (old / "watchdogs").mkdir()
        for name in ("watchdog-run.sh", "watchdogs/hl_take.py", "watchdogs/pools-keys-watch.py"):
            (old / name).write_text("old\n")
        (self.host["state"] / "keys.alarm").write_text("a live alarm\n")
        out = self.run_install()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse((old / "watchdog-run.sh").exists())
        self.assertFalse((old / "watchdogs").exists())
        self.assertIn("removed 3 file(s) an earlier hand-made install left", out.stdout)
        self.assertEqual((self.host["state"] / "keys.alarm").read_text(), "a live alarm\n", "a flag is a fact; not touched")

    def test_installing_the_same_release_twice_is_the_same_install_and_old_trees_are_pruned(self):
        releases = self.host["opt"] / "releases"
        releases.mkdir()
        for i in range(8):
            d = releases / (f"{i:x}" * 40)
            d.mkdir()
            os.utime(d, (1_000_000 + i, 1_000_000 + i))
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.run_install().returncode, 0)
        kept = sorted(p.name for p in releases.iterdir())
        self.assertEqual(len(kept), 5)
        self.assertIn(SHA, kept, "the one `current` names is always kept")
        self.assertEqual(os.readlink(self.host["opt"] / "current"), f"releases/{SHA}")

    def test_the_first_run_of_each_watchdog_is_started_waited_for_and_reported(self):
        out = self.run_install(smoke=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = self.calls()
        for unit in FIRST_SET:
            self.assertIn(f"systemctl start --no-block {unit}.service", calls)
            self.assertIn(f"install: {unit}: first run ok: the watchdog said its last line", out.stdout)
        self.assertLess(calls.index("systemctl start --no-block colosseum-watch-keys.service"),
                        max(i for i, c in enumerate(calls) if c.startswith("systemctl show")))

    def test_a_watchdog_that_could_not_check_is_a_warning_and_never_fails_the_install(self):
        out = self.run_install(smoke=True, env={"FAKE_BAD_UNIT": "colosseum-watch-keeper.service"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("install: WARNING colosseum-watch-keeper: first run ended as exit-code (exit 3)", out.stdout)
        self.assertIn("read: journalctl -u colosseum-watch-keeper", out.stdout)
        self.assertIn("install: colosseum-watch-keys: first run ok", out.stdout)

    def test_a_watchdog_still_running_when_the_wait_is_over_is_said_to_be_still_running(self):
        out = self.run_install(smoke=True, env={"FAKE_ACTIVE": "activating"}, wait=0)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("WARNING colosseum-watch-keys: still running after 0 s", out.stdout)


class InstallScript(unittest.TestCase):
    """install.sh calls the library where it has to: before anything switches, and the first runs after the restarts."""

    def test_the_watchdogs_are_checked_and_put_in_place_before_the_switch_and_run_after_the_restarts(self):
        install = (HOST / "install.sh").read_text()
        source = install.index('. "$rel/ops/host/watchdogs/install-lib.sh"')
        put = install.index('watchdogs_install "$rel" "$sha" "$instances"')
        envs = install.index('deployment_env "$label" "/etc/colosseum/deployment-$name.env"')
        switch = install.index('mv -T "$base/current.next" "$base/current"')
        restart = install.index('systemctl restart "$unit"')
        smoke = install.index("watchdogs_smoke || true")
        prune = install.index("find \"$base/releases\"")
        self.assertLess(source, put)
        self.assertLess(envs, put, "the deployment env files the units read are written first")
        self.assertLess(put, switch, "a release that cannot carry its watchdogs does not switch")
        self.assertLess(restart, smoke)
        self.assertLess(smoke, prune)

    def test_the_shell_is_well_formed(self):
        for path in (HOST / "install.sh", LIB, RUNNER):
            subprocess.run(["bash", "-n", str(path)], check=True)

    def test_nothing_is_left_to_be_put_on_the_host_first(self):
        # deploy.sh sends a commit and runs install.sh, and has no word of the watchdogs: the host needs no file
        # written beforehand (the old install refused to run without /etc/colosseum/watch.env).
        code = "\n".join(l for l in LIB.read_text().splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("watch.env", code)
        self.assertNotIn("watch-demo3.env", code)
        self.assertNotIn("watch", (HOST / "deploy.sh").read_text())
        for unit in SERVICES:
            envs = [l for l in directives(DOGS / f"{unit}.service") if l.startswith("EnvironmentFile=")]
            self.assertEqual([e for e in envs if not e.startswith("EnvironmentFile=-") and "deployment" not in e], [], unit)


if __name__ == "__main__":
    unittest.main()
