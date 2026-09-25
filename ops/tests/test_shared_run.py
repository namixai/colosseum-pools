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


if __name__ == "__main__":
    unittest.main()
