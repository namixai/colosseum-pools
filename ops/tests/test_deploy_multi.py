"""ops/deploy_multi.py: the plan file as the contracts will take it, or a refusal before any gas."""
import copy
import json
import pathlib
import re
import unittest

from ops import deploy_multi as d

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAN = json.loads((ROOT / "ops" / "plans" / "multi-testnet-run.json").read_text())
# The bounds the run's pool is deployed with: an hour, a day, and half an hour of notice.
RUN_BOUNDS = d.bounds_of(0.0416666667, 1, 0.0208333333)

SOL_TYPES = {"Rules": None, "ExamTerms": None, "Size": None, "Plan": None, "AfterTarget": "uint8"}
SOURCES = ("src/Types.sol", "src/exam/PaperExam.sol", "src/multi/MultiPool.sol")


def struct_fields(name: str) -> list[str]:
    """The types of a struct's fields, in order, read from the Solidity source."""
    for rel in SOURCES:
        text = (ROOT / rel).read_text()
        m = re.search(r"^struct " + name + r" \{(.*?)^\}", text, re.S | re.M)
        if m:
            live = [l.split("//")[0].strip() for l in m.group(1).split("\n") if not l.strip().startswith("///")]
            return [l.rsplit(" ", 1)[0] for l in live if l.endswith(";")]
    raise AssertionError(f"no struct {name} in {SOURCES}")


def abi_type(sol: str) -> str:
    """A Solidity field type as the call encoder names it."""
    array = sol.endswith("[]")
    base = sol[:-2] if array else sol
    if base in SOL_TYPES:
        base = SOL_TYPES[base] or "(" + ",".join(abi_type(f) for f in struct_fields(base)) + ")"
    return base + ("[]" if array else "")


class PlanType(unittest.TestCase):
    def test_the_plan_type_is_the_struct_the_contract_declares(self):
        self.assertEqual(d.PLAN_T, abi_type("Plan"))
        self.assertEqual(d.RULES_T, abi_type("Rules"))
        self.assertEqual(d.EXAM_T, abi_type("ExamTerms"))

    def test_after_target_names_follow_the_enum(self):
        text = (ROOT / "src/multi/MultiPool.sol").read_text()
        body = re.search(r"enum AfterTarget \{(.*?)\}", text, re.S).group(1)
        names = [l.strip().rstrip(",") for l in body.split("\n") if l.strip() and not l.strip().startswith("//")]
        self.assertEqual(names, ["TraderStops", "AccountCloses", "AccountStays"])
        self.assertEqual(d.AFTER_TARGET, {"trader_stops": 0, "closes": 1, "stays": 2})


class PlanOf(unittest.TestCase):
    def plan(self, **changes):
        raw = copy.deepcopy(PLAN)
        for path, value in changes.items():
            node = raw
            *parents, leaf = path.split("__")
            for key in parents:
                node = node[key]
            node[leaf] = value
        return raw

    def refused(self, **changes):
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(**changes), RUN_BOUNDS)

    def taken(self, **changes):
        return d.plan_of(self.plan(**changes), RUN_BOUNDS)

    def test_the_run_plan_encodes_in_the_contracts_units(self):
        plan = d.plan_of(PLAN, RUN_BOUNDS)
        self.assertEqual(plan[0], [(5_000_000, 50_000), (10_000_000, 100_000), (25_000_000, 250_000)])
        self.assertEqual(plan[1], (300, 600, 500, [3, 4, 0]))
        self.assertEqual(plan[2], (0, [10], 3_600, 0, 0, 100, 2, 300))
        self.assertEqual(plan[3], (300, 600, 500, [3, 4, 0]))
        self.assertEqual(plan[4:], (0, 8000, 0, 8000, 8, 3_600, 6 * 3_600, 0, 86_400))
        self.assertEqual(len(plan), 13)

    def test_the_funded_idle_limit(self):
        self.assertEqual(self.taken(funded__idle_days=0)[12], 0)
        self.assertEqual(d.plan_of(self.plan(funded__idle_days=30, pool__term_days=180))[12], 30 * 86_400)
        self.refused(funded__idle_days=-1)

    def test_the_owner_is_given_checksummed_or_not_at_all(self):
        good = "0x962Cf63A030A6caB62DEa2BdfBcAD11e4CFC188F"
        self.assertEqual(d.checked_owner(good), good)
        self.assertIsNone(d.checked_owner(None))
        for bad in (good.lower(), good.replace("Cf63", "cf63"), "0x1234", "nobody"):
            with self.assertRaises(SystemExit):
                d.checked_owner(bad)

    def test_the_signer_is_not_the_shared_pools_wallet(self):
        self.assertEqual(d.DEFAULT_SIGNER, "multi-operator")

    def test_the_run_plan_is_not_one_the_products_bounds_take(self):
        with self.assertRaises(d.BadPlan):
            d.plan_of(copy.deepcopy(PLAN))

    def test_the_deployments_bounds(self):
        self.assertEqual(d.bounds_of(180, 730, 14), (180 * 86_400, 730 * 86_400, 14 * 86_400))
        self.assertEqual(RUN_BOUNDS, (3_600, 86_400, 1_800))
        for bad in ((0, 1, 1), (1, 1, 0), (2, 1, 1)):
            with self.assertRaises(d.BadPlan):
                d.bounds_of(*bad)
        d.bounds_of(1, 1, 1)
        # The term keeps to them, on both sides.
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(pool__term_days=1.01), RUN_BOUNDS)
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(pool__term_days=0.25), d.bounds_of(0.26, 1, 0.02))
        d.plan_of(self.plan(pool__term_days=1), RUN_BOUNDS)

    def test_the_constructor_types_are_the_ones_the_contract_declares(self):
        text = (ROOT / "src/multi/MultiPool.sol").read_text()
        params = re.search(r"constructor\((.*?)\) \{", text, re.S).group(1)
        sol = [" ".join(part.split()).rsplit(" ", 1)[0].replace(" memory", "") for part in params.split(",")]
        self.assertEqual(sol, ["address", "PoolFactory", "address", "Plan", "uint32", "uint32", "uint32"])
        self.assertEqual(d.CTOR_T, ["address", "address", "address", d.PLAN_T, "uint32", "uint32", "uint32"])

    def test_the_menu(self):
        self.refused(menu=[])
        self.refused(menu=[{"size_usdc": i + 1, "price_usdc": 1} for i in range(9)])
        self.refused(menu=[{"size_usdc": 5, "price_usdc": 0}])
        self.refused(menu=[{"size_usdc": 0, "price_usdc": 1}])
        self.refused(menu=[{"size_usdc": 5, "price_usdc": 1}, {"size_usdc": 5, "price_usdc": 1}])
        self.taken(menu=[{"size_usdc": i + 1, "price_usdc": 1} for i in range(8)])

    def test_rules(self):
        for which in ("exam_rules", "funded_rules"):
            self.refused(**{f"{which}__max_leverage_x100": 99})
            self.refused(**{f"{which}__max_leverage_x100": 1001})
            self.refused(**{f"{which}__daily_loss_bps": 0})
            self.refused(**{f"{which}__daily_loss_bps": 10_000})
            self.refused(**{f"{which}__max_drawdown_bps": 0})
            self.refused(**{f"{which}__max_drawdown_bps": 10_000})
            self.refused(**{f"{which}__assets": []})
            self.refused(**{f"{which}__assets": ["BTC", "BTC"]})
            self.refused(**{f"{which}__assets": ["DOGE"]})
            self.taken(**{f"{which}__max_leverage_x100": 100})
            self.taken(**{f"{which}__max_leverage_x100": 1000})

    def test_the_exam(self):
        self.refused(exam__targets_bps=[])
        self.refused(exam__targets_bps=[100] * 5)
        self.refused(exam__targets_bps=[0])
        self.refused(exam__targets_bps=[10_001])
        self.refused(exam__step_days=0)
        self.refused(exam__max_idle_days=-1)
        self.refused(exam__min_trading_days=2)
        self.refused(exam__cost_ppm=100_001)
        self.refused(exam__fill_delay_blocks=0)
        self.refused(exam__fill_delay_blocks=601)
        self.refused(exam__fill_window_blocks=0)
        self.refused(exam__fill_window_blocks=601)
        plan = d.plan_of(self.plan(exam__targets_bps=[800, 500], exam__step_days=30, exam__max_idle_days=14,
                                   exam__min_trading_days=31, exam__cost_ppm=950, pool__term_days=180))
        self.assertEqual(plan[2], (0, [800, 500], 30 * 86_400, 14 * 86_400, 31, 950, 2, 300))

    def test_the_funded_account(self):
        self.refused(funded__after_target="whatever")
        self.refused(funded__target_bps=1000)  # the run plan has no target, so none may be named
        self.refused(funded__after_target="closes")  # ...and with a target mode, one has to be
        self.refused(funded__after_target="closes", funded__target_bps=10_001)
        self.refused(funded__trader_share_bps=10_001)
        self.refused(funded__max_retakes=1)
        self.refused(funded__after_target="closes", funded__target_bps=1000, funded__max_retakes=1)
        plan = self.taken(funded__after_target="stays", funded__target_bps=1000, funded__max_retakes=2)
        self.assertEqual((plan[4], plan[6], plan[11]), (1000, 2, 2))
        self.assertEqual(self.taken(funded__after_target="closes", funded__target_bps=10_000)[6], 1)

    def test_the_pool(self):
        self.refused(pool__in_work_bps=0)
        self.refused(pool__in_work_bps=10_001)
        self.refused(pool__max_seats=0)
        self.refused(pool__max_seats=17)
        self.refused(pool__claim_window_days=0.04)  # under an hour
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(pool__claim_window_days=31, pool__term_days=365))
        for days in (179, 731):  # the product's bounds, when the deployment names no others
            with self.assertRaises(d.BadPlan):
                d.plan_of(self.plan(pool__term_days=days))
        plan = d.plan_of(self.plan(pool__in_work_bps=10_000, pool__max_seats=16, pool__claim_window_days=30,
                                   pool__term_days=730))
        self.assertEqual(plan[7:11], (10_000, 16, 30 * 86_400, 730 * 86_400))

    def test_an_exam_has_to_fit_the_term(self):
        # Four steps of 44 days and 4 days to take the account are the whole of a 180-day term:
        # nothing could ever be sold. With 3 days to take it, one day is left for selling.
        long_exam = dict(exam__targets_bps=[500] * 4, exam__step_days=44, pool__term_days=180)
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(pool__claim_window_days=4, **long_exam))
        d.plan_of(self.plan(pool__claim_window_days=3, **long_exam))
        # The notice is the least the last stretch can be, however short the exam.
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(pool__term_days=0.25), d.bounds_of(0.0416666667, 1, 0.25))


if __name__ == "__main__":
    unittest.main()
