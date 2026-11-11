"""ops/host/key_copy.py: the keys an instance's keeper makes, copied into its gateway's key directory, and the
two units that run it."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

from eth_account import Account

HOST = pathlib.Path(__file__).resolve().parents[1] / "host"
sys.path.insert(0, str(HOST))
import key_copy as kc  # noqa: E402

UID, GID = os.getuid(), os.getgid()


def key_file(directory: pathlib.Path, acct=None, mode=0o600, name=None, text=None) -> tuple[str, str]:
    acct = acct or Account.create()
    name = name or f"agent-{acct.address}.key"
    f = directory / name
    f.write_text((text if text is not None else acct.key.hex()) + "\n")
    f.chmod(mode)
    return name, acct.key.hex()


class Copy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.src, self.dst = root / "made-keys", root / "keys"
        self.src.mkdir(mode=0o700)
        self.dst.mkdir(mode=0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def run_copy(self, keeper_uid=UID, gateway_uid=UID) -> tuple[int, list[dict]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = kc.copy(str(self.src), str(self.dst), keeper_uid, gateway_uid, GID)
        return n, [json.loads(l) for l in out.getvalue().splitlines()]

    def test_a_new_key_is_copied_whole_owner_only_and_named_by_its_address(self):
        name, key = key_file(self.src)
        n, said = self.run_copy()
        self.assertEqual(n, 1)
        f = self.dst / name
        self.assertEqual(f.read_text().strip(), key.strip())
        st = f.stat()
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual((st.st_uid, st.st_gid), (UID, GID))
        self.assertEqual(said, [{"event": "key_copied", "address": name[6:-4]}])
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), [name], "no temporary file is left")
        self.assertTrue((self.src / name).exists(), "the keeper keeps its copy, for a renewal it reveals later")

    def test_the_copy_is_handed_to_the_gateways_user(self):
        key_file(self.src)
        with mock.patch.object(kc.os, "fchown", wraps=os.fchown) as fchown:
            self.run_copy(gateway_uid=UID)
        self.assertEqual([c.args[1:] for c in fchown.call_args_list], [(UID, GID)])

    def test_a_second_run_copies_nothing_and_says_nothing(self):
        key_file(self.src)
        self.run_copy()
        self.assertEqual(self.run_copy(), (0, []))

    def test_a_key_the_gateway_holds_another_way_is_never_overwritten(self):
        name, _ = key_file(self.src)
        (self.dst / name).write_text("something else\n")
        (self.dst / name).chmod(0o600)
        n, said = self.run_copy()
        self.assertEqual(n, 0)
        self.assertEqual((self.dst / name).read_text(), "something else\n")
        self.assertEqual([s["event"] for s in said], ["key_differs"])

    def test_a_link_in_the_gateways_directory_is_left_as_it_is(self):
        name, _ = key_file(self.src)
        elsewhere = pathlib.Path(self.tmp.name) / "elsewhere"
        elsewhere.write_text("x")
        (self.dst / name).symlink_to(elsewhere)
        n, said = self.run_copy()
        self.assertEqual(n, 0)
        self.assertEqual(elsewhere.read_text(), "x")
        self.assertEqual([s["event"] for s in said], ["key_differs"])

    def test_what_is_not_a_key_of_the_keepers_own_is_passed_over_and_never_stops_the_next(self):
        other = Account.create()
        bad = [
            key_file(self.src, mode=0o640)[0],                                   # open to its group
            key_file(self.src, text="not a key")[0],                             # not a key
            key_file(self.src, name=f"agent-{other.address}.key")[0],            # another address's key
            key_file(self.src, acct=(big := Account.create()), text=big.key.hex() + " " * 300)[0],  # too big
        ]
        target = pathlib.Path(self.tmp.name) / "target.key"
        target.write_text(Account.create().key.hex())
        target.chmod(0o600)
        link = f"agent-{Account.from_key(target.read_text()).address}.key"
        (self.src / link).symlink_to(target)                                     # a link
        (self.src / f"agent-{Account.create().address}.key").mkdir(mode=0o700)  # a directory
        key_file(self.src, name="keeper.key")                                    # not an agent key: unnamed
        (self.src / f".agent-{other.address}.tmp").write_text(other.key.hex())  # still being written
        good, _ = key_file(self.src)
        n, said = self.run_copy()
        self.assertEqual(n, 1)
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), [good])
        skipped = {s["file"] for s in said if s["event"] == "key_skipped"}
        self.assertEqual(skipped, set(bad) | {link, next(p.name for p in self.src.iterdir() if p.is_dir())})
        self.assertNotIn(other.key.hex()[2:], json.dumps(said), "no key is ever said")

    def test_a_file_of_another_user_is_passed_over(self):
        name, _ = key_file(self.src)
        fd = os.open(self.src, os.O_RDONLY)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(kc.read_key(fd, name, UID + 1))
            self.assertIsNotNone(kc.read_key(fd, name, UID))
        finally:
            os.close(fd)

    def test_directories_must_be_their_users_alone(self):
        for d in (self.src, self.dst):
            with self.subTest(d.name):
                d.chmod(0o750)
                with self.assertRaises(SystemExit):
                    self.run_copy()
                d.chmod(0o700)
        with self.assertRaises(SystemExit):
            self.run_copy(keeper_uid=UID + 1)
        with self.assertRaises(SystemExit):
            self.run_copy(gateway_uid=UID + 1)

    def test_a_directory_that_is_a_link_is_refused(self):
        real = pathlib.Path(self.tmp.name) / "real"
        self.src.rename(real)
        self.src.symlink_to(real)
        with self.assertRaises(OSError):
            self.run_copy()

    def test_the_command_takes_one_instance_name(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for argv in ([], ["demo3", "x"], ["Demo3"], ["../x"], ["a" * 17]):
                self.assertEqual(kc.main(argv), 2, argv)
        self.assertEqual(kc.made_keys("kb1"), "/var/lib/colosseum-keeper/instances/kb1/made-keys")
        self.assertEqual(kc.gateway_keys("kb1"), "/var/lib/colosseum-gw/instances/kb1/keys")


class Units(unittest.TestCase):
    @staticmethod
    def directives(path: pathlib.Path) -> list[str]:
        return [l for l in path.read_text().splitlines() if l and not l.startswith("#")]

    def test_the_copier_sees_the_two_directories_alone_and_no_network(self):
        d = self.directives(HOST / "colosseum-key-copy@.service")
        self.assertIn("User=root", d)
        self.assertIn("Type=oneshot", d)
        self.assertIn("ExecStart=/opt/colosseum-pools/venv/bin/python -I /opt/colosseum-pools/current/ops/host/key_copy.py %i", d)
        self.assertIn("TemporaryFileSystem=/var/lib/colosseum-keeper:ro /var/lib/colosseum-gw:ro", d)
        self.assertIn("BindReadOnlyPaths=/var/lib/colosseum-keeper/instances/%i/made-keys", d)
        self.assertIn("BindPaths=/var/lib/colosseum-gw/instances/%i/keys", d)
        self.assertEqual([l for l in d if l.startswith(("BindPaths=", "BindReadOnlyPaths=", "ReadWritePaths="))],
                         ["BindReadOnlyPaths=/var/lib/colosseum-keeper/instances/%i/made-keys",
                          "BindPaths=/var/lib/colosseum-gw/instances/%i/keys"])
        self.assertIn("PrivateNetwork=true", d)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", d)
        self.assertIn("ProtectSystem=strict", d)
        self.assertIn("NoNewPrivileges=true", d)
        caps = next(l for l in d if l.startswith("CapabilityBoundingSet="))
        self.assertEqual(set(caps.split("=", 1)[1].split()),
                         {"CAP_DAC_OVERRIDE", "CAP_DAC_READ_SEARCH", "CAP_CHOWN", "CAP_FOWNER"})

    def test_the_path_unit_watches_the_keepers_directory_and_starts_the_copier(self):
        d = self.directives(HOST / "colosseum-key-copy@.path")
        self.assertIn("PathChanged=/var/lib/colosseum-keeper/instances/%i/made-keys", d)
        self.assertIn("Unit=colosseum-key-copy@%i.service", d)
        self.assertIn("WantedBy=paths.target", d)
        self.assertEqual(kc.made_keys("%i"), "/var/lib/colosseum-keeper/instances/%i/made-keys")


if __name__ == "__main__":
    unittest.main()
