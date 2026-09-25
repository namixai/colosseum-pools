"""The shared pool's run script: the parts that decide something before a transaction goes out.

    spike/.venv/bin/python -m unittest ops.tests.test_shared_run
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

from ops import shared_run as run

DEP_A = "0x00000000000000000000000000000000000000A1"
DEP_B = "0x00000000000000000000000000000000000000B1"
POOL = "0x00000000000000000000000000000000000000C1"


def seat_args(**over) -> argparse.Namespace:
    base = {"price": None, "capital": None, "funded": None, "term": run.FUNDED_TERM}
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
            run.cmd_seat(record, seat_args(capital=1, funded=10))
            self.assertEqual(c.transact.call_count, 1)

    def test_a_seat_on_a_factory_of_its_own_may_differ(self):
        record = {"SharedPool": POOL, "platform_assets": {"BTC": 3}}
        with mock.patch.object(run, "c") as c:
            c.call_view.return_value = ([],)
            run.cmd_seat(record, seat_args())
            self.assertEqual(c.transact.call_count, 1)


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


if __name__ == "__main__":
    unittest.main()
