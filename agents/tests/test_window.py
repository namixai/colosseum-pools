"""The command line a Claude Code window trades with: limits counted across commands and
days, no option that raises them, no key for reads or a dry run. Offline.

    spike/.venv/bin/python -m unittest discover -s agents/tests -t .
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agents import client as cli
from agents import desk as dk
from agents.tests.fakes import ACCOUNT, FACTORY, NOW, POOL, REGISTRY, FakeChain, FakeGateway, Wallet

DAY = 86400


class Reader:
    def __init__(self, bound=True, key="0x00000000000000000000000000000000000000E1"):
        self.bound, self.key = bound, key

    def trading_key(self, account):
        return self.key

    def is_bound(self, key, account, trader):
        return self.bound and trader == Wallet.address


class Chain(FakeChain):
    RPC_URL = "http://rpc.invalid"

    def __init__(self):
        super().__init__()
        self.keys_loaded = 0

    def account(self, name):
        self.keys_loaded += 1
        return Wallet()

    def address_of(self, name):
        return Wallet.address

    def assert_testnet(self):
        pass


class Window(unittest.TestCase):
    def setUp(self):
        self.chain = Chain()
        self.gateway = FakeGateway()
        self.reader = Reader()
        self.clock = [float(NOW)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (mock.patch.object(dk, "c", self.chain),
                      mock.patch.object(cli, "STATE_DIR", pathlib.Path(tmp.name)),
                      mock.patch.object(cli.time, "time", side_effect=lambda: self.clock[0]),
                      mock.patch("ops.deployments.resolve", return_value=(FACTORY, REGISTRY))):
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv):
        args = cli.parser().parse_args(["--deployment", "demo", *argv])
        return cli.run(args, self.chain, lambda f, r: self.reader, lambda w, url: self.gateway)

    def order(self, *extra):
        return self.run_cli(*extra, "order", ACCOUNT, "ETH", "buy", "0.005", "3000", "--type", "post_only")

    def test_orders_are_counted_across_commands_and_days(self):
        for _ in range(cli.WINDOW_MAX_ORDERS_PER_DAY):
            self.order()
        with self.assertRaises(dk.Refused):
            self.order()
        self.assertEqual(len(self.gateway.orders), cli.WINDOW_MAX_ORDERS_PER_DAY)
        self.assertEqual(self.run_cli("account", ACCOUNT)["session"]["orders_left"], 0)
        self.clock[0] += DAY  # a new UTC day
        self.order()
        self.assertEqual(len(self.gateway.orders), cli.WINDOW_MAX_ORDERS_PER_DAY + 1)

    def test_a_counter_file_can_lower_the_limits_but_not_raise_them(self):
        path = cli.session_path(Wallet.address, dk.to_checksum_address(ACCOUNT), NOW)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"orders_left": 99, "cancels_left": 99, "graduations_left": 99}))
        for _ in range(cli.WINDOW_MAX_ORDERS_PER_DAY):
            self.order()
        with self.assertRaises(dk.Refused):
            self.order()

    def test_the_per_order_cap_is_the_windows(self):
        with self.assertRaises(dk.Refused):
            self.run_cli("order", ACCOUNT, "BTC", "buy", "0.002", "60000")  # 120 USDC
        self.run_cli("order", ACCOUNT, "BTC", "buy", "0.0015", "60000")  # 90 USDC
        self.assertEqual(len(self.gateway.orders), 1)

    def test_no_option_raises_a_limit(self):
        for option in ("--max-notional", "--max-orders", "--max-price", "--budget-usd"):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), mock.patch("sys.stderr"):
                cli.parser().parse_args(["--deployment", "demo", option, "999", "pools"])

    def test_a_wallet_that_is_not_the_accounts_trader_sends_nothing(self):
        self.reader.bound = False
        with self.assertRaises(dk.Refused):
            self.order()
        self.reader.bound, self.reader.key = True, None  # the account isn't trading
        with self.assertRaises(dk.Refused):
            self.order()
        self.assertEqual(self.gateway.orders, [])

    def test_reads_and_dry_runs_load_no_key_and_send_nothing(self):
        self.run_cli("account", ACCOUNT)
        self.run_cli("market", ACCOUNT, "BTC")
        self.run_cli("pools")
        out = self.order("--dry-run")
        self.assertEqual(out["status"], "not_sent")
        self.assertEqual(self.run_cli("--dry-run", "buy", POOL, "20")["status"], "not_sent")
        self.assertEqual((self.chain.keys_loaded, self.gateway.orders, self.chain.sent), (0, [], []))

    def test_one_purchase_a_day(self):
        self.run_cli("buy", POOL, "20")
        self.chain.pools[POOL.lower()]["challenge"] = "0x" + "00" * 20  # the pool is free again
        with self.assertRaises(dk.Refused):
            self.run_cli("buy", POOL, "20")

    def test_main_prints_one_json_object(self):
        out = io.StringIO()
        with mock.patch("spike.hlspike.common", self.chain), \
                mock.patch("gateway.chain.JsonRpcReader", return_value=Reader(bound=False)), \
                mock.patch.object(cli, "GatewayClient", return_value=self.gateway), redirect_stdout(out):
            code = cli.main(["--deployment", "demo", "order", ACCOUNT, "ETH", "buy", "0.005", "3000"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue()), {"ok": False, "refused": "this wallet is not the account's trader"})


if __name__ == "__main__":
    unittest.main()
