"""The deployment's record of where its bytecode came from. Offline: git is a fake.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from ops import deploy_testnet as deploy

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def fake_git(status="", head=COMMIT + "\n", fail=None):
    def run(cmd, cwd, capture_output, text):
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


if __name__ == "__main__":
    unittest.main()
