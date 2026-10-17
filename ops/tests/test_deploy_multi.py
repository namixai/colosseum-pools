"""ops/deploy_multi.py: the plan file as the contracts will take it, or a refusal before any gas."""
import copy
import json
import pathlib
import re
import unittest

from ops import deploy_multi as d

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAN = json.loads((ROOT / "ops" / "plans" / "multi-testnet-run.json").read_text())
# The bounds the run's pool is deployed with: three hours, a day, and fifteen minutes of notice.
RUN_BOUNDS = d.bounds_of(0.125, 1, 0.0104166667)

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
        self.assertEqual(plan[3], (300, 600, 500, [3, 4]), "a funded account on BTC and ETH only (deep audit N6)")
        self.assertEqual(plan[4:], (0, 8000, 0, 8000, 8, 3_600, 6 * 3_600, 0, 86_400, 0, d.ZERO_ADDRESS, d.ZERO_ADDRESS,
                                    d.ZERO32, d.ZERO_ADDRESS))
        self.assertEqual(len(plan), 18)

    def test_free_exams(self):
        inviter = "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB"
        self.assertEqual(self.taken()[13:15], (0, d.ZERO_ADDRESS))
        bare = copy.deepcopy(PLAN)
        del bare["free"]
        self.assertEqual(d.plan_of(bare, RUN_BOUNDS)[13:15], (0, d.ZERO_ADDRESS), "no section is no free exams")
        self.assertEqual(self.taken(free__per_month=3, free__inviter=inviter)[13:15], (3, inviter))
        # A number needs who invites, a whole one; an address only in checksummed form.
        for over in ({"free__per_month": 3}, {"free__per_month": 1.5, "free__inviter": inviter},
                     {"free__per_month": -1, "free__inviter": inviter}, {"free__per_month": True, "free__inviter": inviter},
                     {"free__per_month": 2**16, "free__inviter": inviter},
                     {"free__per_month": 3, "free__inviter": inviter.lower()},
                     {"free__per_month": 3, "free__inviter": "0x123"},
                     {"free__per_month": 3, "free__inviter": inviter, "pool__max_seats": 3}):
            self.refused(**over)
        self.assertEqual(self.taken(free__per_month=3, free__inviter=inviter, pool__max_seats=4)[13], 3)

    def test_the_noter(self):
        noter = "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB"
        self.assertEqual(self.taken()[15], d.ZERO_ADDRESS, "absent is nobody")
        self.assertEqual(self.taken(funded__noter=noter)[15], noter)
        for bad in (noter.lower(), "0x123", 7):
            self.refused(funded__noter=bad)

    def test_a_renewal_keeps_the_products_share(self):
        # Fourteen days of notice need ninety of renewal; fifteen minutes need 96 minutes 26 seconds.
        d.bounds_of(180, 730, 14, 90)
        with self.assertRaises(d.BadPlan):
            d.bounds_of(180, 730, 14, 89.99)
        d.bounds_of(0.125, 1, 0.0104166667, 97 / 1440)
        with self.assertRaises(d.BadPlan):
            d.bounds_of(0.125, 1, 0.0104166667, 96 / 1440)

    def test_the_plan_carries_the_terms_it_was_given(self):
        h, book = b"\x11" * 32, "0x" + "22" * 20
        self.assertEqual(d.plan_of(PLAN, RUN_BOUNDS, (h, book))[16:], (h, book))

    def test_the_funded_idle_limit(self):
        self.assertEqual(self.taken(funded__idle_days=1)[12], 86_400)
        self.assertEqual(d.plan_of(self.plan(funded__idle_days=90, pool__term_days=180))[12], 90 * 86_400)
        for days in (0, 0.99, 90.01, -1):
            self.refused(funded__idle_days=days)

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
        self.assertEqual(d.bounds_of(180, 730, 14), (180 * 86_400, 730 * 86_400, 14 * 86_400, 90 * 86_400))
        self.assertEqual(RUN_BOUNDS, (10_800, 86_400, 900, 90 * 86_400))
        # The notice: above zero, fourteen days at most and a twelfth of the shortest term at most.
        # A long renewal does not stretch the fourteen days either.
        with self.assertRaises(d.BadPlan):
            d.bounds_of(180, 730, 14.01, 200)
        for bad in ((0, 1, 0.01), (1, 1, 0), (2, 1, 0.01), (180, 730, 14.01), (1, 1, 1),
                    (0.0416666667, 730, 14), (167.99, 730, 14), (0.125, 1, 0.0105)):
            with self.assertRaises(d.BadPlan):
                d.bounds_of(*bad)
        d.bounds_of(1, 1, 1 / 12)
        d.bounds_of(168, 730, 14)
        d.bounds_of(180, 730, 14)
        # The term keeps to them, on both sides.
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(pool__term_days=1.01), RUN_BOUNDS)
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(pool__term_days=0.25), d.bounds_of(0.26, 1, 0.02))
        d.plan_of(self.plan(pool__term_days=1), RUN_BOUNDS)

    def test_the_factory_is_told_the_pool_is_a_multipool_after_it_is_deployed(self):
        # Without it the pool sells exams and opens no account (audit K-3): Pool.openFundedFor
        # asks the factory, and the operator is the one who answers.
        src = (ROOT / "ops" / "deploy_multi.py").read_text()
        deploy = src.index('"MultiPool", "MultiPool",')
        step = src.index('"setMultiPool(address,bool)", ["address", "bool"]')
        self.assertLess(deploy, step)
        self.assertIn("[pool, True]", src[step:step + 200])
        self.assertIn("function setMultiPool(address pool, bool recognized) external onlyOperator",
                      (ROOT / "src/PoolFactory.sol").read_text())

    def test_deep_N6_a_funded_account_trades_only_a_book_deep_enough(self):
        thin = copy.deepcopy(PLAN)
        thin["funded_rules"]["assets"] = ["BTC", "SOL"]
        with self.assertRaises(d.BadPlan):
            d.plan_of(thin, RUN_BOUNDS)
        exam_on_sol = copy.deepcopy(PLAN)
        self.assertIn("SOL", exam_on_sol["exam_rules"]["assets"], "the paper exam may: it fills at the oracle")
        d.plan_of(exam_on_sol, RUN_BOUNDS)
        src = (ROOT / "ops" / "deploy_multi.py").read_text()
        self.assertLess(src.index('"setFundedAssets(uint32[],bool)"'), src.index('"MultiPool", "MultiPool",'))
        self.assertEqual(d.FUNDED_ASSETS, ("BTC", "ETH"))

    def test_the_constructor_types_are_the_ones_the_contract_declares(self):
        text = (ROOT / "src/multi/MultiPool.sol").read_text()
        params = re.search(r"constructor\((.*?)\) \{", text, re.S).group(1)
        sol = [" ".join(part.split()).rsplit(" ", 1)[0].replace(" memory", "") for part in params.split(",")]
        self.assertEqual(sol, ["address", "PoolFactory", "address", "Plan", "uint32", "uint32", "uint32", "uint32"])
        self.assertEqual(d.CTOR_T, ["address", "address", "address", d.PLAN_T, "uint32", "uint32", "uint32", "uint32"])

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
        # Every step counts, not the first alone.
        with self.assertRaises(d.BadPlan):
            d.plan_of(self.plan(exam__targets_bps=[500] * 4, exam__step_days=45, pool__term_days=180))


if __name__ == "__main__":
    unittest.main()
