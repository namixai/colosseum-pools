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
