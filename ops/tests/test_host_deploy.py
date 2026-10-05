"""ops/host/deploy.sh sends the host only what is on origin/main. Offline: a repository made for
the test with an origin beside it, and an `ssh` that writes down what it was asked and sends nothing.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "host" / "deploy.sh"
TARGET = "pools-host"  # an ssh destination; the fake ssh below never resolves it


class Deploy(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "deploy-test", "GIT_AUTHOR_EMAIL": "none",
                    "GIT_COMMITTER_NAME": "deploy-test", "GIT_COMMITTER_EMAIL": "none",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        self.env.pop("COLOSSEUM_SSH_KEY", None)

        origin = self.tmp / "origin.git"
        self.git("init", "--quiet", "--bare", "--initial-branch=main", str(origin), cwd=self.tmp)
        self.repo = self.tmp / "repo"
        self.git("init", "--quiet", "--initial-branch=main", str(self.repo), cwd=self.tmp)
        (self.repo / "ops" / "host").mkdir(parents=True)
        shutil.copy(SCRIPT, self.repo / "ops" / "host" / "deploy.sh")
        (self.repo / "ops" / "host" / "install.sh").write_text("#!/usr/bin/env bash\n")
        self.commit("on the default branch")
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "--quiet", "origin", "main")
        self.merged = self.git("rev-parse", "HEAD")
        (self.repo / "later.txt").write_text("not pushed\n")
        self.commit("not on the default branch yet")
        self.unmerged = self.git("rev-parse", "HEAD")

        # An ssh that sends nothing: it writes down its arguments and swallows what it is piped.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.asked = self.tmp / "ssh-asked.txt"
        fake = bin_dir / "ssh"
        fake.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{self.asked}"\ncat > /dev/null\n')
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env['PATH']}"

    def git(self, *args, cwd=None):
        out = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)

    def deploy(self, sha):
        return subprocess.run(["bash", "ops/host/deploy.sh", sha, TARGET], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def sent(self):
        return self.asked.read_text().splitlines() if self.asked.exists() else []

    def test_a_commit_that_is_not_on_origin_main_is_not_sent(self):
        # It used to say "is NOT on origin/main" and send the commit all the same.
        out = self.deploy(self.unmerged)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(f"deploy: {self.unmerged} is NOT on origin/main", out.stderr)
        self.assertEqual(self.sent(), [])  # ssh was never called: nothing left this machine

    def test_a_commit_on_origin_main_is_sent(self):
        out = self.deploy(self.merged)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"deploy: {self.merged} is on origin/main", out.stdout)
        upload, install = self.sent()
        self.assertIn(f"/opt/colosseum-pools/releases/.{self.merged}.upload", upload)
        self.assertIn(f"ops/host/install.sh {self.merged}", install)
        self.assertTrue(upload.startswith(f"-o BatchMode=yes {TARGET} "))

    def test_an_earlier_commit_of_origin_main_is_sent_too(self):
        # A rollback: the release before this one is still on the branch.
        self.git("push", "--quiet", "origin", "main")
        self.assertEqual(self.deploy(self.merged).returncode, 0)
        self.assertEqual(len(self.sent()), 2)

    def test_once_it_is_merged_and_fetched_the_same_commit_goes(self):
        self.assertNotEqual(self.deploy(self.unmerged).returncode, 0)
        self.git("push", "--quiet", "origin", "main")  # the fast-forward, and this clone knows of it
        self.assertEqual(self.deploy(self.unmerged).returncode, 0)
        self.assertEqual(len(self.sent()), 2)


if __name__ == "__main__":
    unittest.main()
