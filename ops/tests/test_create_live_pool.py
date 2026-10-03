"""The live-run pool is created by one script, and its refusals are the whole safety.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .

Step one withdraws capital out of a finished stand. If step two then reverts, the money has moved
and no pool exists -- so every reason to stop has to be found BEFORE anything is sent, and each one
has to be seen failing at least once. That is what these are.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from ops.create_live_pool import NEW_ACCOUNT_FEE, RULES, SPOT_PER_PERP, TERMS, preconditions

ROOT = pathlib.Path(__file__).resolve().parents[2]

DEPLOYER = "0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4"
STAND = "0x237afA2D58B1612e19D47152FfB2E771c05Fe96D"
NEED = (70 * 10**6 + 700 * 10**6) * SPOT_PER_PERP + NEW_ACCOUNT_FEE  # 771 USDC in spot units


def stand(stage: int = 0, owner: str = DEPLOYER, spot: int = 14_077_230_000) -> dict:
    return {"address": STAND, "stage": stage, "owner": owner, "spot": spot}


class WhatStopsIt(unittest.TestCase):
    def ask(self, **over):
        kw = {"assets": [3, 4, 0], "listed": {3: True, 4: True, 0: True},
              "spot": 69_674_940_000, "need": NEED, "deployer": DEPLOYER, "source": stand()}
        kw.update(over)
        return preconditions(**kw)

    def test_the_live_run_as_planned_passes(self):
        # 696.75 on spot plus 140.77 coming back is 837.52 against 771.
        self.assertEqual(self.ask(), [])

    def test_an_asset_the_factory_does_not_list(self):
        said = self.ask(assets=[3, 1], listed={3: True, 1: False})
        self.assertEqual(len(said), 1)
        self.assertIn("актив 1", said[0])

    def test_an_asset_missing_from_the_reads_is_not_a_pass(self):
        # `listed` comes from a loop of chain reads; a key that never arrived must not read as
        # True through a dict default.
        self.assertTrue(self.ask(assets=[3, 9], listed={3: True}))

    def test_an_empty_asset_list(self):
        # `_checkRules` rejects it too, but at step two -- with the withdrawal already done.
        said = self.ask(assets=[], listed={})
        self.assertEqual(len(said), 1)
        self.assertIn("пуст", said[0])

    def test_duplicate_assets(self):
        said = self.ask(assets=[3, 3], listed={3: True})
        self.assertEqual(len(said), 1)
        self.assertIn("дубликаты", said[0])

    def test_a_source_pool_that_is_not_idle(self):
        said = self.ask(source=stand(stage=1))
        self.assertTrue(any("не Idle" in s for s in said), said)

    def test_a_source_pool_somebody_else_owns(self):
        said = self.ask(source=stand(owner="0x000000000000000000000000000000000000dEaD"))
        self.assertTrue(any("принадлежит" in s for s in said), said)

    def test_the_owner_check_ignores_case(self):
        self.assertEqual(self.ask(source=stand(owner=DEPLOYER.lower())), [])

    def test_not_enough_even_with_the_withdrawal(self):
        said = self.ask(spot=0)
        self.assertEqual(len(said), 1)
        self.assertIn("не хватает", said[0])

    def test_the_withdrawal_is_counted_towards_the_capital(self):
        # 760 on spot is under 771 on its own and over it with the stand's 140.77: the point of
        # step one. Without counting `coming` this would refuse the run we mean to make.
        self.assertEqual(self.ask(spot=76_000_000_000), [])
        self.assertTrue(self.ask(spot=76_000_000_000, source=None))

    def test_without_a_source_only_the_deployers_own_spot_counts(self):
        self.assertEqual(self.ask(spot=NEED, source=None), [])

    def test_every_reason_is_reported_at_once(self):
        # One run, one list: a reader fixing the first problem should not discover the second on
        # the next attempt, with money already moved.
        said = self.ask(assets=[9], listed={}, spot=0, source=stand(stage=2, owner="0x00"))
        self.assertEqual(len(said), 4, said)


class WhatItReadsBack(unittest.TestCase):
    """After the pool is made the script reads `rules()` and `terms()` back and compares them with
    what was asked. It read `rules()` as (uint32,uint32,uint16,uint32[]) while the struct is
    (uint16,uint16,uint32,uint32[]): every field is a full word on the wire, so the planned numbers
    decoded all the same and the check was honest for them -- by luck, not by the type."""

    def test_the_types_are_the_structs_in_the_source(self):
        source = (ROOT / "src" / "Types.sol").read_text()
        for name, const in (("Rules", RULES), ("Terms", TERMS)):
            body = re.search(r"struct %s \{(.*?)\n\}" % name, source, re.S).group(1)
            fields = re.findall(r"^\s*(u?int\d+(?:\[\])?|address|bool)\s+\w+;", body, re.M)
            self.assertEqual("(" + ",".join(fields) + ")", const, name)

    def test_both_are_read_back_with_those_types(self):
        script = (ROOT / "ops" / "create_live_pool.py").read_text()
        self.assertIn('c.call_view(pool, "terms()", [], [], [TERMS])', script)
        self.assertIn('c.call_view(pool, "rules()", [], [], [RULES])', script)
