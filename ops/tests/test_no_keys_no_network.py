"""The guard of the ops tests (ops/tests/__init__.py): no test reads a real key or reaches a live node, and every
run of scripts/mutations.py gets an empty key directory. Each probe here is harmless if the guard were gone: a file
under ~/secure that does not exist, and an address of IPv6's documentation range (2001:db8::/32) that routes nowhere."""
import importlib.util
import os
import pathlib
import socket
import unittest
from unittest import mock

import ops.tests as guard
from spike.hlspike import network as networks

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROBE = os.path.expanduser("~/secure/colosseum-testnet/no-such-key-the-guard-probe.key")
NOWHERE = "2001:db8::1"


class TheKeys(unittest.TestCase):
    def test_the_key_directory_is_an_empty_one_of_the_tests(self):
        self.assertEqual(os.environ["COLOSSEUM_KEY_DIR"], guard.KEY_DIR)
        self.assertIsNone(guard.under_secure(guard.KEY_DIR))
        self.assertEqual(os.listdir(guard.KEY_DIR), [])
        from spike.hlspike import common as c
        self.assertEqual(str(c.KEY_DIR), guard.KEY_DIR, "common.py reads its keys from there")

    def test_nothing_under_secure_is_opened_or_listed(self):
        for read in (lambda: open(PROBE), lambda: pathlib.Path(PROBE).read_text(),
                     lambda: os.listdir(os.path.dirname(PROBE)), lambda: list(os.scandir(os.path.dirname(PROBE)))):
            with self.assertRaises(guard.GuardTripped) as caught:
                read()
            self.assertIn("the tests never read a real key", str(caught.exception))
        self.assertIsNotNone(guard.under_secure(pathlib.Path("~/secure").expanduser()))
        self.assertIsNone(guard.under_secure(os.path.expanduser("~/secure-not")))
        self.assertIsNone(guard.under_secure(3), "a file descriptor is no path")

    def test_what_trips_it_is_no_exception_the_code_could_swallow(self):
        self.assertFalse(issubclass(guard.GuardTripped, Exception))


class TheNetwork(unittest.TestCase):
    def test_no_live_host_is_this_machine(self):
        for net in (networks.TESTNET, networks.MAINNET):
            for url in (net.rpc_url, net.api_url):
                self.assertFalse(guard.local(url.split("/")[2].split(":")[0]), url)
        for host in ("localhost", "127.0.0.1", "127.1.2.3", "::1", "[::1]", b"localhost", None, ""):
            self.assertTrue(guard.local(host), host)
        for host in (NOWHERE, "[2001:db8::1]", "0.0.0.0", "rpc.hyperliquid-testnet.xyz", b"api.hyperliquid.xyz"):
            self.assertFalse(guard.local(host), host)

    def test_a_lookup_or_a_connection_elsewhere_fails_the_test(self):
        with self.assertRaises(guard.GuardTripped) as caught:
            socket.getaddrinfo(NOWHERE, 443)
        self.assertIn("never a live node or Hyperliquid's API", str(caught.exception))
        s = socket.socket(socket.AF_INET6)
        s.settimeout(0.2)
        try:
            with self.assertRaises(guard.GuardTripped):
                s.connect((NOWHERE, 9))
        finally:
            s.close()

    def test_the_rpc_and_the_api_of_the_scripts_fail_the_test(self):
        from spike.hlspike import common as c
        with mock.patch.object(c, "RPC_URL", f"http://[{NOWHERE}]:8545/evm"), self.assertRaises(guard.GuardTripped):
            c.rpc("eth_chainId", [])
        with mock.patch.object(c, "API_URL", f"https://[{NOWHERE}]"), self.assertRaises(guard.GuardTripped):
            c.info_post({"type": "meta"})

    def test_a_local_node_is_reached(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=1):
                pass


class TheMutationRun(unittest.TestCase):
    def test_each_run_gets_an_empty_key_directory(self):
        spec = importlib.util.spec_from_file_location("mutations_under_test", ROOT / "scripts" / "mutations.py")
        stand = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stand)
        seen = []

        def run(cmd, **kw):
            keys = kw["env"]["COLOSSEUM_KEY_DIR"]
            seen.append((keys, os.listdir(keys), guard.under_secure(keys)))
        with mock.patch.object(stand.subprocess, "run", side_effect=run):
            stand.run_suite(["true"])
        self.assertEqual(len(seen), 1)
        keys, listed, secure = seen[0]
        self.assertEqual((listed, secure), ([], None))
        self.assertFalse(os.path.exists(keys), "gone with the run")


if __name__ == "__main__":
    unittest.main()
