"""ops/terms.py: every clause written from a setting of the contract, named beside its value."""
import copy
import json
import pathlib
import unittest

from ops import deploy_multi as d
from ops import terms as t

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAN = json.loads((ROOT / "ops" / "plans" / "multi-testnet-run.json").read_text())
BOUNDS = d.bounds_of(180, 730, 14)


class Terms(unittest.TestCase):
    def test_every_setting_is_named_with_its_value(self):
        text = t.render(PLAN, BOUNDS)
        for key, value in (("plan.pool.term_days", PLAN["pool"]["term_days"]), ("bounds.renewal", 90 * 86_400),
                           ("bounds.notice", 14 * 86_400), ("plan.pool.in_work_bps", PLAN["pool"]["in_work_bps"]),
                           ("plan.funded.idle_days", PLAN["funded"]["idle_days"]),
                           ("plan.free.per_month", PLAN["free"]["per_month"]),
                           ("plan.exam.cost_ppm", PLAN["exam"]["cost_ppm"])):
            self.assertIn(f"{key} = {value}", text)
        self.assertTrue(text.startswith(f"Terms of a pool of many traders on Hyperliquid, template {t.TEMPLATE_VERSION}."))

    def test_a_different_setting_is_different_terms(self):
        base = t.terms_hash(t.render(PLAN, BOUNDS))
        for path, value in (("pool.term_days", 0.5), ("pool.in_work_bps", 7000), ("funded.trader_share_bps", 7000),
                            ("exam.cost_ppm", 950), ("free.per_month", 3)):
            raw = copy.deepcopy(PLAN)
            node = raw
            *parents, leaf = path.split(".")
            for k in parents:
                node = node[k]
            node[leaf] = value
            self.assertNotEqual(t.terms_hash(t.render(raw, BOUNDS)), base, path)
        self.assertNotEqual(t.terms_hash(t.render(PLAN, d.bounds_of(180, 730, 14, 120))), base, "the renewal")

    def test_the_cfos_clauses_are_marked_pending_until_given(self):
        text = t.render(PLAN, BOUNDS)
        self.assertTrue(t.pending(text))
        given = t.render(PLAN, BOUNDS, cfo=["A. The investor's money can be lost."])
        self.assertFalse(t.pending(given))
        self.assertIn("A. The investor's money can be lost.", given)

    def test_free_exams_are_stated_with_their_limits(self):
        raw = copy.deepcopy(PLAN)
        raw["free"] = {"per_month": 3, "inviter": "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB"}
        raw["pool"]["max_seats"] = 8
        text = t.render(raw, BOUNDS)
        self.assertIn("3 exams a month may be given away free", text)
        self.assertIn("no more than a quarter of the seats (2) running free at once", text)

    def test_a_shared_pool_states_its_raise(self):
        text = t.render(PLAN, BOUNDS, shares={"target_usdc": 100_000})
        self.assertIn("shares.target_usdc = 100000", text)
        self.assertIn("less the platform's 0.5 %", text)
        self.assertNotIn("shares.target_usdc", t.render(PLAN, BOUNDS))


if __name__ == "__main__":
    unittest.main()
