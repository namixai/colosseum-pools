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


KEY_A = "0x00000000000000000000000000000000000000a1"
KEY_B = "0x00000000000000000000000000000000000000b2"


class Keys(unittest.TestCase):
    def check(self, lines, on_core=()):
        with mock.patch.object(deploy.c, "core_user_exists", side_effect=lambda a: a.lower() in on_core):
            return deploy.check_keys(lines)

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

    def run_main(self, deploy_one, transact=None, big_blocks=None):
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
                mock.patch.object(deploy.c, "deploy", side_effect=deploy_one), \
                mock.patch.object(deploy.c, "transact", side_effect=record_tx), \
                mock.patch.object(deploy.sys, "argv", ["deploy", "--label", "t", "--keys-file", str(self.keys_file)]):
            return deploy.main()

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
        # The demo's platform fee: 2 test USDC to the operator, on the new factory.
        factory = "0x" + f"{0xE4:040x}"
        fee = [(to, values) for to, sig, values in self.transactions if sig == "setChallengeFee(uint256,address)"]
        self.assertEqual(fee, [(factory, [2_000_000, Deployer.address])])
        self.assertEqual((record["challenge_fee"], "setChallengeFee" in record["tx"]), (2_000_000, True))
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
