"""The shared pool's run script: the parts that decide something before a transaction goes out.

    spike/.venv/bin/python -m unittest ops.tests.test_shared_run
"""

from __future__ import annotations

import argparse
import sys
import unittest
from unittest import mock

from ops import deploy_shared
from ops import shared_run as run

DEP_A = "0x00000000000000000000000000000000000000A1"
DEP_B = "0x00000000000000000000000000000000000000B1"
POOL = "0x00000000000000000000000000000000000000C1"


def seat_args(**over) -> argparse.Namespace:
    base = {"price": None, "capital": None, "funded": None, "term": run.FUNDED_TERM, "duration": None,
            "target_bps": None, "daily_bps": None, "drawdown_bps": None}
    base.update(over)
    return argparse.Namespace(**base)


class SeatTerms(unittest.TestCase):
    def test_the_runs_terms_unless_the_command_line_changes_them(self):
        self.assertEqual(run.seat_terms(seat_args()), run.TERMS)
        t = run.seat_terms(seat_args(price=1.5, capital=1, funded=10))
        self.assertEqual((t["price"], t["capital"], t["fundedCapital"]), (1_500_000, 1_000_000, 10_000_000))
        self.assertEqual(t["targetBps"], run.TERMS["targetBps"])

    def test_a_seat_on_the_demos_factory_keeps_a_tenth(self):
        record = {"SharedPool": POOL, "factory_from": "demo", "platform_assets": {"BTC": 3}}
        with mock.patch.object(run, "c") as c:
            with self.assertRaisesRegex(SystemExit, "a tenth of the funded capital"):
                run.cmd_seat(record, seat_args())  # 2 and 8: the first run's seat
            c.transact.assert_not_called()
            c.call_view.return_value = ([],)
            run.cmd_seat(record, seat_args(capital=1, funded=10, target_bps=800))
            self.assertEqual(c.transact.call_count, 1)

    def test_the_rules_and_duration_from_the_command_line(self):
        self.assertEqual(run.seat_rules(seat_args()), run.RULES)
        self.assertEqual(run.seat_rules(seat_args(daily_bps=500, drawdown_bps=1000)), (500, 1000, run.RULES[2]))
        self.assertEqual(run.seat_terms(seat_args(duration=86400))["duration"], 86400)
        self.assertEqual(run.seat_terms(seat_args(target_bps=800))["targetBps"], 800)

    def test_a_seat_on_the_demos_factory_keeps_to_the_models_grid(self):
        record = {"SharedPool": POOL, "factory_from": "demo", "platform_assets": {"BTC": 3}}
        with mock.patch.object(run, "c") as c:
            c.call_view.return_value = ([],)
            for off in ({"drawdown_bps": 700, "target_bps": 800}, {"target_bps": 100}):  # 7%, and a 1% target
                with self.assertRaisesRegex(SystemExit, "Economics page's model has a figure"):
                    run.cmd_seat(record, seat_args(capital=3, funded=30, **off))
            c.transact.assert_not_called()
            run.cmd_seat(record, seat_args(capital=3, funded=30, daily_bps=500, drawdown_bps=1000, target_bps=800))
            sent_rules = c.transact.call_args.args[4][0]
            self.assertEqual(sent_rules[:3], (500, 1000, run.RULES[2]))

    def test_a_seat_on_a_factory_of_its_own_may_differ(self):
        record = {"SharedPool": POOL, "platform_assets": {"BTC": 3}}
        with mock.patch.object(run, "c") as c:
            c.call_view.return_value = ([],)
            run.cmd_seat(record, seat_args())
            self.assertEqual(c.transact.call_count, 1)


class Request(unittest.TestCase):
    def call(self, now, last_deposit=1_000_000, lock=600):
        record = {"SharedPool": POOL}
        args = argparse.Namespace(who="shared-dep-a", shares="all")
        answers = {"lastDeposit": last_deposit, "lock": lock, "sharesOf": 5 * 10**8, "queuedOf": 0}
        with mock.patch.object(run, "c") as c, mock.patch.object(run.time, "time", return_value=now):
            c.account.return_value.address = DEP_A
            c.call_view.side_effect = lambda to, sig, types, args_, out: (answers[sig.split("(")[0]],)
            c.transact.return_value = {"transactionHash": "0x" + "ab" * 32}
            try:
                run.cmd_request(record, args)
            finally:
                self.sent = c.transact.call_count

    def test_a_request_inside_the_lock_says_how_long_and_sends_nothing(self):
        with self.assertRaisesRegex(SystemExit, "locked until 1000600; 100 s to go"):
            self.call(now=1_000_500)
        self.assertEqual(self.sent, 0)

    def test_a_request_after_the_lock_goes_out(self):
        self.call(now=1_000_600)
        self.assertEqual(self.sent, 1)


class Wallets(unittest.TestCase):
    def test_depositors_are_topped_up_to_the_deposit_and_the_tickets_fee(self):
        balances = {DEP_A.lower(): 1_807_142_800, DEP_B.lower(): 0}
        args = argparse.Namespace(only=["shared-dep-a", "shared-dep-b"], trader=False, deposit=20.0, hype=0.004,
                                  trader_evm=1.2)
        with mock.patch.object(run, "c") as c:
            c.address_of.side_effect = {"shared-dep-a": DEP_A, "shared-dep-b": DEP_B}.get
            c.core_spot_balance.side_effect = lambda who, token: {"total": balances[who.lower()]}
            ex = c.exchange.return_value
            ex.spot_transfer.return_value = {"status": "ok"}
            c.send_tx.return_value = {"transactionHash": "0x" + "ab" * 32}
            run.cmd_wallets({}, args)
        sent = {call.args[1]: call.args[0] for call in ex.spot_transfer.call_args_list}
        self.assertAlmostEqual(sent[DEP_A], 2.928572, places=8)  # 21 less the 18.071428 it holds
        self.assertEqual(sent[DEP_B], 21.0)

    def test_a_depositor_holding_enough_gets_nothing(self):
        args = argparse.Namespace(only=["shared-dep-a"], trader=False, deposit=20.0, hype=0.0, trader_evm=1.2)
        with mock.patch.object(run, "c") as c:
            c.address_of.return_value = DEP_A
            c.core_spot_balance.return_value = {"total": 2_100_000_000}
            c.send_tx.return_value = {"transactionHash": "0x" + "ab" * 32}
            run.cmd_wallets({}, args)
            c.exchange.return_value.spot_transfer.assert_not_called()


class DeployArguments(unittest.TestCase):
    def refused(self, *argv):
        with mock.patch.object(sys, "argv", ["deploy_shared.py", "--label", "x", *argv]), \
                mock.patch.object(deploy_shared, "c") as c:
            with self.assertRaisesRegex(SystemExit, "either --keys-file"):
                deploy_shared.main()
            self.assertEqual(c.mock_calls, [], "nothing reached the chain")

    def test_an_empty_keys_file_is_still_a_keys_file(self):
        self.refused("--on-demo-factory", "--keys-file", "")

    def test_one_of_the_two_is_needed(self):
        self.refused()

    def test_a_named_deployment_and_a_keys_file_are_still_two(self):
        self.refused("--on-factory-of", "demo2", "--keys-file", "keys.txt")

    def test_an_empty_label_is_refused_and_never_becomes_the_first_deployment(self):
        for label in ("", "   "):
            with mock.patch.object(sys, "argv", ["deploy_shared.py", "--label", "x", "--on-factory-of", label]), \
                    mock.patch.object(deploy_shared, "c") as c, \
                    mock.patch.object(deploy_shared.deployments, "load") as load:
                with self.assertRaisesRegex(SystemExit, "--on-factory-of needs the label of a deployment"):
                    deploy_shared.main()
                self.assertEqual(c.mock_calls, [], "nothing reached the chain")
                load.assert_not_called()

    def test_the_demo_flag_names_one_deployment_and_cannot_be_given_another(self):
        with mock.patch.object(sys, "argv", ["deploy_shared.py", "--label", "x", "--on-demo-factory",
                                             "--on-factory-of", "demo2"]), \
                mock.patch.object(deploy_shared, "c") as c:
            with self.assertRaisesRegex(SystemExit, "name one deployment"):
                deploy_shared.main()
            self.assertEqual(c.mock_calls, [])


BASE = {"label": "demo2", "commit": "e" * 40, "PoolFactory": "0x" + "f2" * 20, "KeyRegistry": "0x" + "a1" * 20,
        "PoolImpl": "0x" + "b1" * 20, "ChallengeAccountImpl": "0x" + "c1" * 20}
OPERATOR = "0x" + "0e" * 20


class OnAnotherDeploymentsFactory(unittest.TestCase):
    """--on-factory-of: the SharedPool alone, on the factory of the deployment named, and a dry run of it."""

    def run_main(self, *argv, gas_wei=7 * 10**15, listed=True, exists=False):
        self.loaded, self.deployed, self.saved, self.blocks = [], [], [], []
        out = mock.MagicMock()
        out.exists.return_value = exists
        out.relative_to.return_value = "deployments/testnet-x.json"

        def call_view(to, sig, types, values, outs):
            return [listed] if sig.startswith("isPlatformAsset") else [700000]

        def deploy_contract(op, path, record, key, contract, types, values):
            self.deployed.append((contract, values, dict(record)))
            return "0x" + "5a" * 20, {"blockNumber": hex(123)}

        # unsafe: the module calls c.assert_testnet(), a name a plain mock takes for a misspelt assertion.
        with mock.patch.object(sys, "argv", ["deploy_shared.py", "--label", "x", *argv]), \
                mock.patch.object(deploy_shared, "c", new_callable=lambda: mock.MagicMock(unsafe=True)) as c, \
                mock.patch.object(deploy_shared.deployments, "load", side_effect=lambda l: self.loaded.append(l) or BASE), \
                mock.patch.object(deploy_shared, "git", return_value=""), \
                mock.patch.object(deploy_shared, "git_head", return_value="h" * 40), \
                mock.patch.object(deploy_shared.subprocess, "run"), \
                mock.patch.object(deploy_shared, "check_assets", return_value=[3, 4, 0]), \
                mock.patch.object(deploy_shared, "big_blocks", side_effect=lambda op, on: self.blocks.append(on)), \
                mock.patch.object(deploy_shared, "deploy_contract", side_effect=deploy_contract), \
                mock.patch.object(deploy_shared, "save", side_effect=lambda path, rec: self.saved.append(dict(rec))), \
                mock.patch.object(deploy_shared, "ROOT") as root, \
                mock.patch("builtins.print") as said:
            self.said = said
            root.__truediv__.return_value.__truediv__.return_value = out
            c.CHAIN_ID = 998
            c.account.return_value.address = OPERATOR
            c.core_user_exists.return_value = True
            c.call_view.side_effect = call_view
            c.rpc.return_value = hex(gas_wei)
            self.c = c
            return deploy_shared.main()

    def test_a_dry_run_makes_every_check_and_sends_nothing(self):
        self.assertEqual(self.run_main("--on-factory-of", "demo2", "--dry-run"), 0)
        self.assertEqual(self.loaded, ["demo2"], "the checks are against the deployment named")
        self.assertEqual((self.deployed, self.saved, self.blocks), ([], [], []), "a dry run sent or wrote something")
        self.c.transact.assert_not_called()
        self.c.record.assert_not_called()
        asked = [call.args[1] for call in self.c.call_view.call_args_list]
        self.assertEqual(asked.count("isPlatformAsset(uint32)"), 3)
        text = "\n".join(str(call.args[0]) for call in self.said.call_args_list)
        self.assertIn("nothing was sent", text)
        self.assertIn(f"of demo2: PoolFactory {BASE['PoolFactory']}, KeyRegistry {BASE['KeyRegistry']}", text)
        self.assertIn("min deposit 2000000000, lock 600 s, fee 1000 bps", text)

    def test_the_real_run_deploys_the_shared_pool_on_that_factory_and_says_so_in_the_record(self):
        self.assertEqual(self.run_main("--on-factory-of", "demo2"), 0)
        self.assertEqual(self.loaded, ["demo2"], "the factory and the implementations are the named deployment's")
        self.assertEqual(self.blocks, [True, False], "big blocks on for the deployment, then off")
        [(contract, values, record)] = self.deployed
        self.assertEqual(contract, "SharedPool")
        self.assertEqual(values, [BASE["PoolFactory"], OPERATOR, OPERATOR, 2_000_000_000, 600, 1000])
        self.assertEqual((record["factory_from"], record["PoolFactory"], record["KeyRegistry"]),
                         ("demo2", BASE["PoolFactory"], BASE["KeyRegistry"]))
        self.assertEqual(record["implementations_from"], BASE["commit"])
        self.assertEqual((self.saved[-1]["status"], self.saved[-1]["block"]), ("complete", 123))

    def test_the_demo_flag_is_the_first_deployment(self):
        self.run_main("--on-demo-factory", "--dry-run")
        self.assertEqual(self.loaded, ["demo"])

    def test_an_operator_without_gas_is_refused_before_anything_is_sent(self):
        # 0.0005 HYPE is what one SharedPool cost; the floor is four times that, and far under the
        # four-contract floor of ops/deploy_testnet.py, which refused a wallet holding fourteen times enough.
        self.assertEqual(deploy_shared.SHARED_GAS_FLOOR_WEI, 2 * 10**15)
        with self.assertRaisesRegex(SystemExit, "below the 0.002 this deployment needs"):
            self.run_main("--on-factory-of", "demo2", gas_wei=10**15)
        self.assertEqual((self.deployed, self.blocks), ([], []))
        self.assertEqual(self.run_main("--on-factory-of", "demo2", "--dry-run", gas_wei=7 * 10**15), 0)

    def test_a_factory_that_does_not_list_an_asset_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "the factory of demo2 doesn't list asset 3"):
            self.run_main("--on-factory-of", "demo2", "--dry-run", listed=False)

    def test_a_dry_run_is_only_for_a_named_factory(self):
        with mock.patch.object(sys, "argv", ["deploy_shared.py", "--label", "x", "--keys-file", "k.txt", "--dry-run"]), \
                mock.patch.object(deploy_shared, "c") as c:
            with self.assertRaisesRegex(SystemExit, "--dry-run is for --on-factory-of"):
                deploy_shared.main()
            self.assertEqual(c.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
