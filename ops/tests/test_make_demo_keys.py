"""The demo's agent keys: owner-only files in a new directory, addresses only on the screen.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import io
import pathlib
import stat
import tempfile
import unittest
from contextlib import redirect_stdout

from eth_account import Account

from ops import make_demo_keys as mk


class DemoKeys(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = pathlib.Path(tmp.name) / "demo-agents"

    def run_main(self, *argv):
        printed = io.StringIO()
        with redirect_stdout(printed):
            self.assertEqual(mk.main(["--out", str(self.out), *argv]), 0)
        return printed.getvalue()

    def test_keys_are_owner_only_and_only_addresses_are_printed(self):
        printed = self.run_main("--count", "3")
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        files = sorted(self.out.glob("*.key"))
        self.assertEqual([f.name for f in files], ["demo-agent-01.key", "demo-agent-02.key", "demo-agent-03.key"])
        derived = []
        for f in files:
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f.name)
            secret = f.read_text().strip()
            self.assertNotIn(secret[2:], printed, "a key reached the screen")
            derived.append(Account.from_key(secret).address)
        self.assertEqual(printed.split(), derived)
        self.assertEqual((self.out / "addresses.txt").read_text().split(), derived)
        self.assertEqual(len(set(derived)), 3)

    def test_keys_never_go_into_a_directory_that_exists(self):
        self.out.mkdir()
        with self.assertRaises(SystemExit):
            self.run_main("--count", "1")
        self.assertEqual(list(self.out.iterdir()), [])

    def test_the_count_is_bounded(self):
        for bad in ("0", "101"):
            with self.assertRaises(SystemExit):
                self.run_main("--count", bad)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
