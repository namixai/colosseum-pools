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

    def test_six_refusals_in_a_row_are_waited_out_instead_of_lost(self):
        # The case that happened on the host on 1 October 2026: `eth_blockNumber: rate limited
        # 6 times in a row`, and a keeper pass died on it. The old ladder stopped at 31 seconds
        # and a rate limit is counted over a MINUTE, so every attempt landed in the same spent
        # minute. Six refusals and a seventh answer is now a slow call, not a lost one.
        limited = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}
        self.answers(*[(200, limited)] * 6, (200, {"jsonrpc": "2.0", "id": 1, "result": "0x3e6"}))
        self.assertEqual(self.common.rpc("eth_blockNumber"), "0x3e6")
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0, 8.0, 16.0, 32.0])

    def test_the_ladder_outlasts_the_minute_the_limit_is_counted_over(self):
        # The number, not the shape: a ladder that adds up to less than a minute cannot outlast a
        # limit measured per minute, however many steps it has.
        self.assertGreater(self.common.RPC_BACKOFF_TOTAL_S, 60)
        self.assertEqual(self.common.RPC_BACKOFF_TOTAL_S,
                         sum(2 ** i for i in range(self.common.RPC_ATTEMPTS - 1)),
                         "the constant and the ladder have to be the same number")

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


class NothingReachedTheNode(unittest.TestCase):
    """`send_tx` has to say which failures happened before the broadcast.

    A caller that spends a budget on an attempt -- the agent desk's one graduation a day, one stop a
    session -- gives the attempt back for `NotSent` and only for it. The desk's own tests use a fake
    that raises `c.NotSent`, so without this class the fake and `send_tx` could drift apart and
    every one of those tests would stay green while production never raised it.
    """

    def run_send(self, fail_at: str, receipt_status: str = "0x1"):
        from unittest import mock

        from eth_account import Account

        from spike.hlspike import common

        answers = {"eth_chainId": "0x3e6", "eth_getTransactionCount": "0x0", "eth_gasPrice": "0x5f5e100",
                   "eth_estimateGas": "0x5208", "eth_sendRawTransaction": "0x" + "ee" * 32}

        def rpc(method, params=()):
            if method == fail_at:
                raise RuntimeError(f"{method}: {{'code': 3, 'message': 'execution reverted', 'data': '0xdeadbeef'}}")
            return answers[method]

        with mock.patch.object(common, "rpc", side_effect=rpc), \
                mock.patch.object(common, "wait_receipt", side_effect=lambda h: {"status": receipt_status}):
            common.send_tx(Account.create(), "0x" + "11" * 20, b"", 0)

    def test_a_revert_at_the_gas_estimate_never_left(self):
        from spike.hlspike import common
        with self.assertRaises(common.NotSent) as caught:
            self.run_send("eth_estimateGas")
        self.assertIn("0xdeadbeef", str(caught.exception),
                      "the revert data survives, so a caller can still name the error")

    def test_a_read_before_the_estimate_never_left_either(self):
        from spike.hlspike import common
        for method in ("eth_getTransactionCount", "eth_gasPrice"):
            with self.subTest(method), self.assertRaises(common.NotSent):
                self.run_send(method)

    def test_a_transaction_that_was_broadcast_and_reverted_is_not_NotSent(self):
        from spike.hlspike import common
        with self.assertRaises(RuntimeError) as caught:
            self.run_send("nothing", receipt_status="0x0")
        self.assertNotIsInstance(caught.exception, common.NotSent,
                                 "it reached the chain, so the attempt may not come back")

    def test_a_send_error_that_is_not_a_duplicate_is_not_NotSent(self):
        # The node may have taken it and failed to answer. `NotSent` here would let a second
        # transaction land on top of one already in the mempool.
        from spike.hlspike import common
        with self.assertRaises(RuntimeError) as caught:
            self.run_send("eth_sendRawTransaction")
        self.assertNotIsInstance(caught.exception, common.NotSent)
