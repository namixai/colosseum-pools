"""The simulated pool's deploy script and relayer: their numbers and layouts against the contracts,
the first pool's plan, and what the relayer refuses before it spends gas."""
import json
import pathlib
import re
import unittest

from eth_utils import keccak

from ops import deploy_sim as d
from ops import sim_relay as r
from ops.tests.test_sim_keeper import declared

ROOT = pathlib.Path(__file__).resolve().parents[2]
FACTORY = (ROOT / "src/sim/SimPoolFactory.sol").read_text()
POOL = (ROOT / "src/sim/SimPool.sol").read_text()
FIRST = json.loads((ROOT / "deployments/plans/sim-first-pool.json").read_text())


def constant(name: str) -> str:
    return re.search(rf"constant {name} = ([^;]+);", FACTORY).group(1)


class TheNumbers(unittest.TestCase):
    def test_the_scripts_constants_are_the_factorys(self):
        self.assertEqual(constant("CREATION_FEE"), "1 ether")
        self.assertEqual(d.CREATION_FEE, 10**18)
        self.assertEqual(int(constant("PAYOUT_BPS")), d.PAYOUT_BPS)
        self.assertEqual(int(constant("LOCKED_PAYOUTS")), d.LOCKED_PAYOUTS)

    def test_the_plan_tuple_is_simplans_layout(self):
        body = re.search(r"struct SimPlan \{(.*?)\n\}", POOL, re.S).group(1)
        types = [l.split()[0] for l in body.splitlines() if l.strip() and not l.strip().startswith("//")]
        self.assertEqual(types, ["SimSize[]", "Rules", "ExamTerms", "Rules", "ExamTerms", "uint32", "bool", "uint16",
                                 "address", "uint16", "bytes32"])
        self.assertTrue(d.PLAN_T.startswith("((uint64,uint64)[]," + d.RULES_T + "," + d.TERMS_T))
        self.assertTrue(d.PLAN_T.endswith(",uint32,bool,uint16,address,uint16,bytes32)"))

    def test_the_relayers_call_and_event_are_the_pools(self):
        self.assertIn("function buyWithAuthorization(\n        address trader,\n        uint256 line,", POOL)
        self.assertEqual(r.SIG.split("(")[0], "buyWithAuthorization")
        self.assertEqual(r.EXAM_SOLD, declared("src/sim/SimPool.sol", "ExamSold"))


class TheFirstPool(unittest.TestCase):
    def test_it_is_alexs_menu_and_locks_1600(self):
        self.assertEqual([(m["size_usdc"], m["price_usdc"]) for m in FIRST["menu"]], [(5000, 45), (10000, 85)])
        self.assertEqual(d.locked_of(FIRST), 1_600 * 10**6)

    def test_its_tuple(self):
        h = keccak(text=FIRST["terms_text"])
        plan = d.plan_of(FIRST, h)
        self.assertEqual(plan[0], [(5_000 * 10**6, 45 * 10**6), (10_000 * 10**6, 85 * 10**6)])
        self.assertEqual(plan[1], (300, 600, 1000, [3, 4, 0], [1000, 1000, 500]))
        self.assertEqual(plan[2], (0, [1000], 2592000, 2592000, 0, 450, 1, 10))
        self.assertEqual(d.plan_of(FIRST, h)[4], plan[2], "the stage has the exam's terms")
        self.assertEqual(plan[5:], (604800, True, 150, "0x" + "00" * 20, 0, h))

    def test_the_cfos_numbers_of_12_november(self):
        """Thirty days a stage, no idle rule that could bind, no minimum of days, 0.045 % a fill, seven days
        to start the stage; the measuring pool fills at the same cost."""
        measure = json.loads((ROOT / "deployments/plans/sim-measure-pool.json").read_text())
        for plan in (FIRST, measure):
            for stage in ("exam", "funded"):
                t = plan[stage]
                self.assertEqual(t["step_duration_s"], 30 * 86_400)
                self.assertEqual(t["max_idle_s"], t["step_duration_s"], "never ends a stage before its deadline")
                self.assertEqual(t["min_trading_days"], 0)
                self.assertEqual(t["cost_ppm"], 450)
            self.assertEqual(plan["fund_window_s"], 7 * 86_400)
            self.assertIn("pays 0.045 % of its notional", plan["terms_text"])
            self.assertNotIn("without a fill", plan["terms_text"])

    def test_dollars_round_to_usdc_units(self):
        self.assertEqual(d.usdc_units(2.01), 2_010_000, "int(2.01 * 10**6) is 2 009 999")
        self.assertEqual(d.usdc_units(45), 45_000_000)
        self.assertEqual(d.locked_of({"menu": [{"size_usdc": 0.29, "price_usdc": 1}]}), 46_400)

    def test_its_ceiling_is_over_testnets_books_and_within_the_bound(self):
        self.assertGreater(FIRST["max_worse_bps"], 118)
        self.assertLessEqual(FIRST["max_worse_bps"], int(constant("MAX_WORSE_BPS")))

    def test_the_assets_are_listed_with_their_caps_and_hype_is_not(self):
        self.assertEqual(d.asset_list(["BTC", "SOL"]), [("BTC", 3, 1000), ("SOL", 0, 500)])
        with self.assertRaises(SystemExit):
            d.asset_list(["HYPE"])


class TheMeasurePool(unittest.TestCase):
    def test_it_measures_with_the_widest_window_and_locks_little(self):
        m = json.loads((ROOT / "deployments/plans/sim-measure-pool.json").read_text())
        self.assertEqual(m["exam"]["fill_window"], int(constant("MAX_FILL_WINDOW")))
        self.assertEqual(m["funded"]["fill_window"], int(constant("MAX_FILL_WINDOW")))
        self.assertEqual(d.locked_of(m), 160 * 10**6)
        self.assertEqual({k: v for k, v in m["exam_rules"].items()}, FIRST["exam_rules"], "the same rules as the first pool")


GOOD = {"pool": "0x" + "11" * 20, "trader": "0x" + "22" * 20, "line": 1, "valid_after": 0,
        "valid_before": 2_000, "nonce": "0x" + "33" * 32, "v": 27, "r": "0x" + "44" * 32, "s": "0x" + "55" * 32}


class TheRelayer(unittest.TestCase):
    def test_a_malformed_purchase_is_refused(self):
        for k, v in (("pool", "0x12"), ("nonce", "0x" + "33" * 31), ("v", 29), ("line", "x")):
            with self.assertRaises(r.Refused):
                r.parsed({**GOOD, k: v})
        with self.assertRaises(r.Refused):
            r.parsed({k: v for k, v in GOOD.items() if k != "r"})

    def test_what_is_not_worth_the_gas_is_refused(self):
        p = r.parsed(GOOD)
        pools = [p["pool"]]
        r.check(p, 1_000, pools, 2, False)
        cases = [(1_000, [], 2, False), (1_000, pools, 1, False), (2_000 - r.MIN_LEFT_S + 1, pools, 2, False),
                 (1_000, pools, 2, True)]
        for now, ps, n, used in cases:
            with self.assertRaises(r.Refused):
                r.check(p, now, ps, n, used)

    def test_the_call_takes_the_signed_fields_in_order(self):
        p = r.parsed(GOOD)
        self.assertEqual(r.args_of(p), [p["trader"], 1, 0, 2_000, bytes.fromhex("33" * 32), 27,
                                        bytes.fromhex("44" * 32), bytes.fromhex("55" * 32)])
        self.assertEqual(len(r.TYPES), len(r.args_of(p)))


if __name__ == "__main__":
    unittest.main()
