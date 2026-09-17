"""The deployment's record of where its bytecode came from. Offline: git is a fake.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import json
import pathlib
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
        self.assertEqual(self.check(["# enclave keys", "", KEY_A, f"  {KEY_B}  "]),
                         [deploy.to_checksum_address(KEY_A), deploy.to_checksum_address(KEY_B)])
        for bad in (["0x" + "00" * 20], [KEY_A, KEY_A.upper().replace("0X", "0x")], ["not-an-address"]):
            with self.assertRaises(SystemExit, msg=bad):
                self.check(bad)
        with self.assertRaises(SystemExit):
            self.check([KEY_A, KEY_B], on_core={KEY_B})


class Deployer:
    address = "0x00000000000000000000000000000000000000d0"


class HalfWay(unittest.TestCase):
    """The publish transaction fails after four deployments and two settings went through."""

    def test_a_late_failure_leaves_the_addresses_on_disk(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        keys_file = root / "keys.txt"
        keys_file.write_text(KEY_A + "\n")
        addresses = iter(f"0x{i:040x}" for i in range(0xE1, 0xE5))

        def deployed(acct, name, types, values):
            return next(addresses), {"transactionHash": f"0x{name}", "blockNumber": "0x10"}

        def transact(acct, to, sig, types, values):
            if sig.startswith("publish"):
                raise RuntimeError("execution reverted")
            return {"transactionHash": f"0x{sig.split('(')[0]}"}

        with mock.patch.object(deploy, "ROOT", root), \
                mock.patch.object(deploy.subprocess, "run", side_effect=fake_git()), \
                mock.patch.object(deploy, "check_assets", return_value=[3, 4, 0]), \
                mock.patch.object(deploy, "big_blocks"), \
                mock.patch.object(deploy.c, "assert_testnet"), \
                mock.patch.object(deploy.c, "account", return_value=Deployer()), \
                mock.patch.object(deploy.c, "core_user_exists", side_effect=lambda a: a == Deployer.address), \
                mock.patch.object(deploy.c, "deploy", side_effect=deployed), \
                mock.patch.object(deploy.c, "transact", side_effect=transact), \
                mock.patch.object(deploy.sys, "argv", ["deploy", "--label", "t", "--keys-file", str(keys_file)]):
            with self.assertRaises(RuntimeError):
                deploy.main()

        record = json.loads((root / "deployments" / "testnet-t.json").read_text())
        self.assertEqual(record["status"], "incomplete")
        self.assertIn("execution reverted", record["error"])
        self.assertEqual(record["PoolFactory"], "0x" + f"{0xE4:040x}")
        self.assertEqual(set(record["tx"]), {"KeyRegistry", "PoolImpl", "ChallengeAccountImpl", "PoolFactory",
                                             "setAccountSource", "setPlatformAssets"})
        # Nothing downstream will use it.
        with mock.patch.object(deployments, "ROOT", root), self.assertRaises(SystemExit):
            deployments.load("t")


if __name__ == "__main__":
    unittest.main()
