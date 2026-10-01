"""The deployment's record of where its bytecode came from. Offline: git is a fake.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from ops import deploy_testnet as deploy
from ops import deployments

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def fake_git(status="", head=COMMIT + "\n", fail=None):
    def run(cmd, **kwargs):
        if cmd[0] == "forge":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        verb = cmd[1]
        if verb == fail:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        return subprocess.CompletedProcess(cmd, 0, status if verb == "status" else head, "")
    return run


class Provenance(unittest.TestCase):
    def head(self, **kw):
        with mock.patch.object(deploy.subprocess, "run", side_effect=fake_git(**kw)) as run:
            return deploy.git_head(), run

    def test_a_clean_tree_gives_its_commit_and_checks_every_build_input(self):
        commit, run = self.head()
        self.assertEqual(commit, COMMIT)
        status = run.call_args_list[0].args[0]
        self.assertEqual(status[:4], ["git", "status", "--porcelain", "--"])
        self.assertEqual(set(status[4:]), {"src", "lib", "foundry.toml", "foundry.lock", "remappings.txt"})

    def test_git_failing_stops_the_deployment(self):
        for verb in ("status", "rev-parse"):
            with self.assertRaises(SystemExit, msg=verb):
                self.head(fail=verb)

    def test_no_commit_or_uncommitted_inputs_stop_it(self):
        with self.assertRaises(SystemExit):
            self.head(head="")
        with self.assertRaises(SystemExit):
            self.head(head="HEAD\n")
        with self.assertRaises(SystemExit):
            self.head(status=" M lib/hyper-evm-lib\n")


ZERO = "0x" + "00" * 20
KEY_A = "0x00000000000000000000000000000000000000a1"
KEY_B = "0x00000000000000000000000000000000000000b2"


class Keys(unittest.TestCase):
    REGISTRY = "0x00000000000000000000000000000000000000e0"

    def setUp(self):
        # An empty records directory by default, so these tests say nothing about whatever
        # deployments this checkout happens to carry.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records = pathlib.Path(self.tmp.name)
        self.asked: list[tuple[str, str]] = []

    def record(self, label: str, **fields) -> None:
        (self.records / f"testnet-{label}.json").write_text(json.dumps(fields))

    def check(self, lines, on_core=(), known=(), records=None, registry_fails=False, only=None):
        held = {deploy.to_checksum_address(k) for k in known}
        only_at = deploy.to_checksum_address(only) if only else None

        def call_view(to, sig, types, args, out):
            self.asked.append((to, args[0]))
            if registry_fails:
                raise RuntimeError("429 Too Many Requests")
            hit = deploy.to_checksum_address(args[0]) in held and only_at in (None, to)
            return ((2 if hit else 0, ZERO, ZERO),)

        with mock.patch.object(deploy.c, "core_user_exists", side_effect=lambda a: a.lower() in on_core), \
             mock.patch.object(deploy.c, "call_view", side_effect=call_view):
            return deploy.check_keys(lines, self.records if records is None else records)

    def test_a_key_a_live_registry_already_holds_is_refused(self):
        # The case no other check here can see. A spare key has no HyperCore account -- that is
        # what makes it spare -- so it passes every rule `publish` has. Publishing it again leaves
        # one address in two registries, which is one key two accounts can each take as an agent.
        self.record("demo", KeyRegistry=self.REGISTRY)
        self.assertEqual(self.check([KEY_B]), [deploy.to_checksum_address(KEY_B)])
        with self.assertRaises(SystemExit) as caught:
            self.check([KEY_B, KEY_A], known={KEY_A})
        self.assertIn(self.REGISTRY, str(caught.exception).lower())
        self.assertIn("testnet-demo.json", str(caught.exception))

    def test_the_records_supply_the_registries_and_the_chain_supplies_the_answer(self):
        # Keys reach a live registry long after the deployment that made it -- by hand, from the
        # host -- and no record names them. Reading `published_keys` would have passed exactly
        # the keys this exists to catch.
        self.record("demo", KeyRegistry=self.REGISTRY, published_keys=[])
        with self.assertRaises(SystemExit):
            self.check([KEY_A], known={KEY_A})
        self.assertEqual(self.asked, [(deploy.to_checksum_address(self.REGISTRY),
                                       deploy.to_checksum_address(KEY_A))])

    def test_a_registry_that_will_not_answer_is_not_an_all_clear(self):
        self.record("demo", KeyRegistry=self.REGISTRY)
        with self.assertRaises(SystemExit) as caught:
            self.check([KEY_A], registry_fails=True)
        self.assertIn("could not ask", str(caught.exception))

    def test_a_key_is_accepted_only_after_every_registry_has_been_asked(self):
        # A hit stops the run at the registry that has it, so the thing worth pinning is the other
        # way round: a key that came back clean came back clean from all of them.
        other = "0x00000000000000000000000000000000000000e1"
        self.record("demo", KeyRegistry=self.REGISTRY)
        self.record("rehearsal", KeyRegistry=other)

        self.assertEqual(self.check([KEY_A]), [deploy.to_checksum_address(KEY_A)])
        self.assertEqual(sorted(to for to, _ in self.asked),
                         sorted(deploy.to_checksum_address(r) for r in (self.REGISTRY, other)))

        with self.assertRaises(SystemExit) as caught:  # known only to the second one
            self.check([KEY_A], known={KEY_A}, only=other)
        self.assertIn("testnet-rehearsal.json", str(caught.exception))

    def test_records_it_cannot_read_stop_it_instead_of_being_skipped(self):
        (self.records / "testnet-broken.json").write_text("{not json")
        with self.assertRaises(SystemExit):
            self.check([KEY_B])

    def test_a_missing_records_directory_reads_as_no_records(self):
        # It cannot be told from an empty one, and neither can be told from a wrong path, so this
        # does not pretend to: what the run prints is how many registries it consulted, and nobody
        # is protected by a refusal that fires on the first deployment there has ever been.
        self.assertEqual(self.check([KEY_B], records=self.records / "not-there"),
                         [deploy.to_checksum_address(KEY_B)])

    def test_the_registry_rules_are_checked_before_anything_is_deployed(self):
        self.assertEqual(self.check(["# demo agent keys", "", KEY_A, f"  {KEY_B}  "]),
                         [deploy.to_checksum_address(KEY_A), deploy.to_checksum_address(KEY_B)])
        for bad in (["0x" + "00" * 20], [KEY_A, KEY_A.upper().replace("0X", "0x")], ["not-an-address"]):
            with self.assertRaises(SystemExit, msg=bad):
                self.check(bad)
        with self.assertRaises(SystemExit):
            self.check([KEY_A, KEY_B], on_core={KEY_B})


class Deployer:
    address = "0x00000000000000000000000000000000000000d0"


class Record(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "deployments" / "testnet-t.json"
        deploy.save(self.path, {"status": "configuring", "PoolFactory": "0xe4"})

    def test_a_write_that_fails_leaves_the_previous_record(self):
        def disk_error(fd):
            raise OSError(5, "Input/output error")

        with mock.patch.object(deploy.os, "fsync", disk_error), self.assertRaises(OSError):
            deploy.save(self.path, {"status": "complete", "PoolFactory": "0xe4"})
        self.assertEqual(json.loads(self.path.read_text()), {"status": "configuring", "PoolFactory": "0xe4"})
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["testnet-t.json"])

    def test_the_new_record_is_on_disk_before_it_replaces_the_old_one(self):
        events = []
        real_fsync, real_replace = os.fsync, pathlib.Path.replace

        def fsync(fd):
            events.append(("fsync", "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"))
            real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", pathlib.Path(dst).name))
            return real_replace(src, dst)

        with mock.patch.object(deploy.os, "fsync", fsync), mock.patch.object(pathlib.Path, "replace", replace):
            deploy.save(self.path, {"status": "complete", "PoolFactory": "0xe4"})
        self.assertEqual(events, [("fsync", "file"), ("replace", "testnet-t.json"), ("fsync", "directory")])
        self.assertEqual(json.loads(self.path.read_text())["status"], "complete")


class Deployment(unittest.TestCase):
    """main() against fake chains: four deployments, then the settings."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.keys_file = self.root / "keys.txt"
        self.keys_file.write_text(KEY_A + "\n")
        self.record_path = self.root / "deployments" / "testnet-t.json"
        self.on_disk = []  # the record as it was on disk when each deployment was sent
        self.big_blocks = []

    def deployed(self, fail_on=None):
        addresses = iter(f"0x{i:040x}" for i in range(0xE1, 0xE5))

        def deploy_one(acct, name, types, values):
            self.on_disk.append(json.loads(self.record_path.read_text()) if self.record_path.exists() else None)
            if name == fail_on:
                raise RuntimeError("replacement transaction underpriced")
            return next(addresses), {"transactionHash": f"0x{name}", "blockNumber": "0x10"}
        return deploy_one

    def run_main(self, deploy_one, transact=None, big_blocks=None, balance=10**18):
        self.transactions = []

        def record_tx(acct, to, sig, types, values):
            self.transactions.append((to, sig, values))
            return (transact or (lambda *a: {"transactionHash": "0x" + a[2].split("(")[0]}))(acct, to, sig, types, values)

        def switch(acct, enable):
            self.big_blocks.append(enable)
            if big_blocks is not None:
                big_blocks(enable)

        with mock.patch.object(deploy, "ROOT", self.root), \
                mock.patch.object(deploy.subprocess, "run", side_effect=fake_git()), \
                mock.patch.object(deploy, "check_assets", return_value=[3, 4, 0]), \
                mock.patch.object(deploy, "big_blocks", side_effect=switch), \
                mock.patch.object(deploy.c, "assert_testnet"), \
                mock.patch.object(deploy.c, "record"), \
                mock.patch.object(deploy.c, "account", return_value=Deployer()), \
                mock.patch.object(deploy.c, "core_user_exists", side_effect=lambda a: a == Deployer.address), \
                mock.patch.object(deploy.c, "rpc", return_value=hex(balance)), \
                mock.patch.object(deploy.c, "deploy", side_effect=deploy_one), \
                mock.patch.object(deploy.c, "transact", side_effect=record_tx), \
                mock.patch.object(deploy.sys, "argv", ["deploy", "--label", "t", "--keys-file", str(self.keys_file)]):
            return deploy.main()

    def test_a_deployer_without_gas_is_refused_before_anything_is_sent(self):
        """Running out half way leaves a record marked incomplete and a live contract nothing
        points at, and an empty wallet reads like a node problem until somebody looks."""
        with self.assertRaises(SystemExit) as caught:
            self.run_main(self.deployed(), balance=deploy.GAS_FLOOR_WEI - 1)
        self.assertIn("below", str(caught.exception))
        self.assertEqual(self.big_blocks, [], "big blocks were never switched on")
        self.assertFalse(self.record_path.exists(), "and nothing was written")

    def record(self):
        return json.loads(self.record_path.read_text())

    def assert_not_loadable(self):
        with mock.patch.object(deployments, "ROOT", self.root), self.assertRaises(SystemExit):
            deployments.load("t")

    def test_every_contract_is_on_disk_before_the_next_is_sent(self):
        with mock.patch.object(deploy, "print"):
            self.assertEqual(self.run_main(self.deployed()), 0)
        self.assertEqual(self.big_blocks, [True, False])
        self.assertIsNone(self.on_disk[0])
        self.assertEqual([sorted(r["tx"]) for r in self.on_disk[1:]],
                         [["KeyRegistry"], ["KeyRegistry", "PoolImpl"],
                          ["ChallengeAccountImpl", "KeyRegistry", "PoolImpl"]])
        self.assertEqual([r["PoolImpl"] for r in self.on_disk[2:]], ["0x" + f"{0xE2:040x}"] * 2)
        record = self.record()
        self.assertEqual((record["status"], record["block"], record["published_keys"]),
                         ("complete", 0x10, [deploy.to_checksum_address(KEY_A)]))
        # The demo's platform fee: 0.7 test USDC to the operator, on the new factory.
        factory = "0x" + f"{0xE4:040x}"
        fee = [(to, values) for to, sig, values in self.transactions if sig == "setChallengeFee(uint256,address)"]
        self.assertEqual(fee, [(factory, [700_000, Deployer.address])])
        self.assertEqual((record["challenge_fee"], "setChallengeFee" in record["tx"]), (700_000, True))
        with mock.patch.object(deployments, "ROOT", self.root):
            self.assertEqual(deployments.load("t")["PoolFactory"], "0x" + f"{0xE4:040x}")

    def test_a_failed_deployment_keeps_the_earlier_ones_and_goes_back_to_small_blocks(self):
        with self.assertRaises(RuntimeError):
            self.run_main(self.deployed(fail_on="ChallengeAccount"))
        record = self.record()
        self.assertEqual(record["status"], "incomplete")
        self.assertIn("underpriced", record["error"])
        self.assertEqual(sorted(record["tx"]), ["KeyRegistry", "PoolImpl"])
        self.assertEqual(record["PoolImpl"], "0x" + f"{0xE2:040x}")
        self.assertNotIn("ChallengeAccountImpl", record)
        self.assertEqual(self.big_blocks, [True, False])
        self.assert_not_loadable()

    def test_failing_to_leave_big_blocks_marks_the_record(self):
        def stuck(enable):
            if not enable:
                raise SystemExit("could not switch big blocks to False: {'status': 'err'}")

        with self.assertRaises(SystemExit):
            self.run_main(self.deployed(), big_blocks=stuck)
        record = self.record()
        self.assertEqual(record["status"], "incomplete")
        self.assertIn("could not switch big blocks", record["error"])
        self.assertEqual(record["PoolFactory"], "0x" + f"{0xE4:040x}")
        self.assertEqual(len(record["tx"]), 4)
        self.assert_not_loadable()

    def test_a_late_failure_leaves_the_addresses_on_disk(self):
        """The publish transaction fails after four deployments and two settings went through."""
        def transact(acct, to, sig, types, values):
            if sig.startswith("publish"):
                raise RuntimeError("execution reverted")
            return {"transactionHash": f"0x{sig.split('(')[0]}"}

        with self.assertRaises(RuntimeError):
            self.run_main(self.deployed(), transact=transact)
        record = self.record()
        self.assertEqual(record["status"], "incomplete")
        self.assertIn("execution reverted", record["error"])
        self.assertEqual(record["PoolFactory"], "0x" + f"{0xE4:040x}")
        self.assertEqual(set(record["tx"]), {"KeyRegistry", "PoolImpl", "ChallengeAccountImpl", "PoolFactory",
                                             "setAccountSource", "setPlatformAssets", "setChallengeFee"})
        self.assert_not_loadable()


if __name__ == "__main__":
    unittest.main()
