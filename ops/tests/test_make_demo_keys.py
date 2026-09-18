"""The demo's agent keys: owner-only files in a new directory, addresses only on the screen.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import io
import os
import pathlib
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from eth_account import Account

from ops import make_demo_keys as mk
from spike.hlspike import common as c


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


class WalletKey(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name) / "secrets"
        self.dir.mkdir()
        os.chmod(self.dir, 0o700)

    def run_main(self, *argv):
        printed = io.StringIO()
        with redirect_stdout(printed):
            self.assertEqual(mk.main(list(argv)), 0)
        return printed.getvalue()

    def test_one_owner_only_key_the_spike_tools_read(self):
        printed = self.run_main("--wallet", "keeper", "--dir", str(self.dir))
        key = self.dir / "keeper.key"
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertNotIn(key.read_text().strip()[2:], printed, "a key reached the screen")
        with mock.patch.object(c, "KEY_DIR", self.dir):
            self.assertEqual(c.account("keeper").address, printed.strip())
        self.assertEqual(c.to_checksum_address((self.dir / "keeper.addr").read_text().strip()), printed.strip())

    def test_a_wallet_key_is_never_replaced(self):
        self.run_main("--wallet", "keeper", "--dir", str(self.dir))
        before = (self.dir / "keeper.key").read_bytes()
        with self.assertRaises(SystemExit):
            self.run_main("--wallet", "keeper", "--dir", str(self.dir))
        self.assertEqual((self.dir / "keeper.key").read_bytes(), before)

    def test_an_open_directory_or_an_odd_name_is_refused(self):
        os.chmod(self.dir, 0o750)
        with self.assertRaises(SystemExit):
            self.run_main("--wallet", "keeper", "--dir", str(self.dir))
        os.chmod(self.dir, 0o700)
        for name in ("../keeper", "Keeper", "keeper.key", ""):
            with self.assertRaises(SystemExit, msg=name):
                self.run_main("--wallet", name, "--dir", str(self.dir))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_the_directory_must_exist_and_be_one(self):
        with self.assertRaises(SystemExit):
            self.run_main("--wallet", "keeper", "--dir", str(self.dir / "missing"))
        plain = self.dir / "plain"
        plain.write_text("")
        os.chmod(plain, 0o600)  # owner-only, so the mode check alone would let it through
        with self.assertRaises(SystemExit):
            self.run_main("--wallet", "keeper", "--dir", str(plain))
        self.assertEqual(([q.name for q in self.dir.iterdir()], plain.read_text()), (["plain"], ""))

    def test_the_two_modes_do_not_mix(self):
        for argv in (["--wallet", "keeper"], ["--wallet", "keeper", "--dir", str(self.dir), "--out", "x"],
                     ["--dir", str(self.dir)], []):
            with self.assertRaises(SystemExit, msg=argv), redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()):
                mk.main(argv)
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
