"""The keeper and the agents can use a dedicated RPC instead of the public one.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PUBLIC = "https://rpc.hyperliquid-testnet.xyz/evm"


def rpc_url(env: dict) -> str:
    code = "from spike.hlspike import common as c; print(c.RPC_URL)"
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    return run.stdout.strip() or run.stderr.strip()


class RpcFromTheEnvironment(unittest.TestCase):
    def test_a_dedicated_rpc_replaces_the_public_one(self):
        env = {k: v for k, v in os.environ.items() if k != "COLOSSEUM_RPC_URL"}
        self.assertEqual(rpc_url(env), PUBLIC)
        self.assertEqual(rpc_url({**env, "COLOSSEUM_RPC_URL": "https://rpc.example.test/evm"}),
                         "https://rpc.example.test/evm")


if __name__ == "__main__":
    unittest.main()


class Throttling(unittest.TestCase):
    """The public RPC throttles; a throttled call is retried, anything else is not."""

    def setUp(self):
        from unittest import mock

        from spike.hlspike import common

        self.common = common
        self.sleeps = []
        patcher = mock.patch.object(common.time, "sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def answers(self, *replies):
        from unittest import mock

        class Reply:
            def __init__(self, status, body):
                self.status_code, self._body = status, body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"http {self.status_code}")

            def json(self):
                return self._body

        post = mock.patch.object(self.common._session, "post",
                                 side_effect=[Reply(status, body) for status, body in replies])
        self.post = post.start()
        self.addCleanup(post.stop)

    def test_a_throttled_call_is_retried(self):
        limited = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}
        self.answers((429, None), (200, limited), (200, {"jsonrpc": "2.0", "id": 1, "result": "0x3e6"}))
        self.assertEqual(self.common.rpc("eth_chainId"), "0x3e6")
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_other_errors_are_not(self):
        self.answers((200, {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}}))
        with self.assertRaises(RuntimeError):
            self.common.rpc("eth_estimateGas")
        self.assertEqual((self.post.call_count, self.sleeps), (1, []))

    def test_it_gives_up_in_the_end(self):
        limited = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}
        self.answers(*[(200, limited)] * self.common.RPC_ATTEMPTS)
        with self.assertRaises(RuntimeError):
            self.common.rpc("eth_blockNumber")
        self.assertEqual(len(self.sleeps), self.common.RPC_ATTEMPTS - 1)


class DuplicateSend(unittest.TestCase):
    """A send that reached the node before it was throttled comes back as a duplicate."""

    def send(self, send_error):
        from unittest import mock

        from eth_account import Account

        from spike.hlspike import common

        answers = {"eth_chainId": "0x3e6", "eth_getTransactionCount": "0x0", "eth_gasPrice": "0x5f5e100",
                   "eth_estimateGas": "0x5208"}

        def rpc(method, params=()):
            if method == "eth_sendRawTransaction":
                raise RuntimeError(f"eth_sendRawTransaction: {send_error}")
            return answers[method]

        waited = []
        with mock.patch.object(common, "rpc", side_effect=rpc), \
                mock.patch.object(common, "wait_receipt", side_effect=lambda h: waited.append(h) or {"status": "0x1"}):
            receipt = common.send_tx(Account.create(), "0x" + "11" * 20, b"", 0)
        return receipt, waited

    def test_already_known_waits_for_the_receipt(self):
        receipt, waited = self.send("{'code': -32000, 'message': 'already known'}")
        self.assertEqual(receipt, {"status": "0x1"})
        self.assertEqual(len(waited), 1)
        self.assertEqual(len(waited[0]), 66)

    def test_another_send_error_is_raised(self):
        with self.assertRaises(RuntimeError):
            self.send("{'code': -32000, 'message': 'insufficient funds'}")
