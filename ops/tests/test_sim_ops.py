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
        self.assertEqual(int(constant("MIN_PARTNER_BPS")), d.MIN_PARTNER_BPS)
        self.assertEqual(int(constant("MAX_PARTNER_BPS")), d.MAX_PARTNER_BPS)
        self.assertEqual(int(constant("MAX_CODES")), d.MAX_CODES)

    def test_the_plan_tuple_is_simplans_layout(self):
        body = re.search(r"struct SimPlan \{(.*?)\n\}", POOL, re.S).group(1)
        types = [l.split()[0] for l in body.splitlines() if l.strip() and not l.strip().startswith("//")]
        self.assertEqual(types, ["SimSize[]", "Rules", "ExamTerms", "Rules", "ExamTerms", "uint32", "bool", "uint16",
                                 "bytes32"])
        self.assertTrue(d.PLAN_T.startswith("((uint64,uint64)[]," + d.RULES_T + "," + d.TERMS_T))
        self.assertTrue(d.PLAN_T.endswith(",uint32,bool,uint16,bytes32)"))

    def test_the_scripts_calls_are_the_pools(self):
        self.assertIn("function setCode(bytes32 code, address partner, uint16 bps)", POOL)
        self.assertEqual(d.SET_CODE, "setCode(bytes32,address,uint16)")
        body = re.search(r"struct Code \{(.*?)\n    \}", POOL, re.S).group(1)
        types = [l.split()[0] for l in body.splitlines() if l.strip() and not l.strip().startswith("//")]
        self.assertEqual(types, d.CODE_OF, "codeOf's answer, field by field")
        self.assertIn("function buy(uint256 line, bytes32 code)", POOL)
        self.assertIn('"buy(uint256,bytes32)"', (ROOT / "ops/deploy_sim.py").read_text(encoding="utf-8"))
        self.assertIn('keccak(text="ExamSold(address,address,uint256,uint64,uint64,uint256,bool,bytes32,address)")',
                      (ROOT / "ops/deploy_sim.py").read_text(encoding="utf-8"))
        self.assertEqual("0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64,uint256,bool,bytes32,address)").hex(),
                         declared("src/sim/SimPool.sol", "ExamSold"))
        self.assertIn("event PoolCreated(address indexed pool, address indexed creator, uint256 locked);", FACTORY)
        self.assertIn('keccak(text="PoolCreated(address,address,uint256)")', (ROOT / "ops/deploy_sim.py").read_text(encoding="utf-8"))

    def test_the_relayers_call_and_event_are_the_pools(self):
        self.assertIn("function buyWithAuthorization(\n        address trader,\n        uint256 line,\n        bytes32 code,\n"
                      "        bytes32 salt,\n        uint256 validAfter,\n        uint256 validBefore,\n        uint8 v,", POOL)
        self.assertIn("keccak256(abi.encode(code, salt))", POOL)
        self.assertEqual(r.SIG.split("(")[0], "buyWithAuthorization")
        self.assertEqual(r.EXAM_SOLD, declared("src/sim/SimPool.sol", "ExamSold"))


class TheRecord(unittest.TestCase):
    def test_a_finished_deploy_writes_a_record_the_loader_takes(self):
        """`deploy` used to end on "deployed", which `deployments.load` refuses: `pool`, `buy`, the keeper and the
        checker would all have stopped at the record."""
        import tempfile
        from unittest import mock
        from ops import deployments
        from spike.hlspike import network as networks
        net = networks.select()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "testnet-x.json"
            path.write_text(json.dumps({"chain_id": net.chain_id, "status": d.DONE, "SimPoolFactory": "0x" + "33" * 20}), encoding="utf-8")
            with mock.patch.object(deployments, "record_path", return_value=path):
                self.assertEqual(deployments.load("x", net)["status"], "complete")
        self.assertIn('record["status"] = DONE', (ROOT / "ops/deploy_sim.py").read_text(encoding="utf-8"))


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
        self.assertEqual(plan[4], plan[2], "the stage has the exam's terms")
        self.assertEqual(plan[5:], (604800, True, 150, h))

    def test_its_codes_wait_for_the_cfo_and_alex_and_its_terms_say_how_codes_share(self):
        self.assertEqual(FIRST["codes"], [], "an empty list: the codes and their shares come before the pool sells")
        self.assertNotIn("partner", FIRST)
        for name in ("sim-first-pool", "sim-measure-pool"):
            text = json.loads((ROOT / f"deployments/plans/{name}.json").read_text(encoding="utf-8"))["terms_text"]
            self.assertIn("Every exam is bought with a trading group's code", text)
            self.assertIn("once a sale names a code, neither changes", text)
            self.assertIn("in the part of every price the pool took that its code's sales brought", text)

    def test_the_cfos_numbers_of_12_november(self):
        """Thirty days a stage, no idle rule that could bind, no minimum of days, 0.045 % a fill, seven days
        to start the stage; the measuring pool fills at the same cost."""
        measure = json.loads((ROOT / "deployments/plans/sim-measure-pool.json").read_text(encoding="utf-8"))
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
            self.assertIn("72 hours after the payout is claimed", plan["terms_text"])
            self.assertNotIn("up to 72 hours", plan["terms_text"], "the contract pays no earlier, and later if queued")

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
        self.assertEqual(d.codes_of(m, "0x" + "ab" * 20),
                         [("MEASURE", keccak(text="MEASURE"), d.to_checksum_address("0x" + "ab" * 20), 0)],
                         "one code, the creator's own, with no share: a sale needs one")


class TheCodes(unittest.TestCase):
    CREATOR = "0x" + "ab" * 20

    def test_a_plans_codes_are_checked_as_the_pool_checks_them(self):
        ok = {"codes": [{"code": "GROUP-ONE", "partner": "0x" + "11" * 20, "bps": 5000},
                        {"code": "PLATFORM", "partner": "creator", "bps": 0}]}
        got = d.codes_of(ok, self.CREATOR)
        self.assertEqual([(t, h, b) for t, h, _, b in got], [("GROUP-ONE", keccak(text="GROUP-ONE"), 5000),
                                                             ("PLATFORM", keccak(text="PLATFORM"), 0)])
        self.assertEqual(got[1][2], d.to_checksum_address(self.CREATOR))
        bad = [[{"code": "A", "partner": "0x" + "11" * 20, "bps": 3999}],
               [{"code": "A", "partner": "0x" + "11" * 20, "bps": 6001}],
               [{"code": "A", "partner": "0x" + "00" * 20, "bps": 0}],
               [{"code": "", "partner": "0x" + "11" * 20, "bps": 0}],
               [{"code": "A", "partner": "0x" + "11" * 20, "bps": 0}, {"code": "A", "partner": "0x" + "22" * 20, "bps": 0}],
               [{"code": str(i), "partner": "0x" + "11" * 20, "bps": 0} for i in range(d.MAX_CODES + 1)]]
        for codes in bad:
            with self.assertRaises(SystemExit):
                d.codes_of({"codes": codes}, self.CREATOR)
        self.assertEqual(d.codes_of({}, self.CREATOR), [])

    def test_a_code_a_sale_named_is_skipped_if_it_matches_and_stops_everything_if_not(self):
        p1, p2 = d.to_checksum_address("0x" + "11" * 20), d.to_checksum_address("0x" + "22" * 20)
        codes = [("A", b"a" * 32, p1, 5000), ("B", b"b" * 32, p2, 4000), ("C", b"c" * 32, p1, 0)]
        chain = {b"a" * 32: (p1, 5000, True), b"b" * 32: (p1, 4000, False), b"c" * 32: (d.ZERO, 0, False)}
        self.assertEqual([t for t, *_ in d.to_set(codes, chain.__getitem__)], ["B", "C"], "A is there already")
        chain[b"a" * 32] = (p1, 6000, True)
        with self.assertRaises(SystemExit):
            d.to_set(codes, chain.__getitem__)


GOOD = {"pool": "0x" + "11" * 20, "trader": "0x" + "22" * 20, "line": 1, "code": "0x" + "66" * 32, "salt": "0x" + "33" * 32,
        "valid_after": 0, "valid_before": 2_000, "v": 27, "r": "0x" + "44" * 32, "s": "0x" + "55" * 32}


class TheRelayer(unittest.TestCase):
    def test_a_malformed_purchase_is_refused(self):
        for k, v in (("pool", "0x12"), ("salt", "0x" + "33" * 31), ("code", "0x" + "66" * 33), ("v", 29), ("line", "x")):
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
        with self.assertRaises(r.Refused):
            r.check(p, 1_000, pools, 2, False, code_set=False)

    def test_the_signed_nonce_is_the_code_and_the_salt(self):
        """The trader signs keccak256(abi.encode(code, salt)): the relayer cannot send it with another code."""
        from eth_abi import encode
        p = r.parsed(GOOD)
        self.assertEqual(p["nonce"], keccak(encode(["bytes32", "bytes32"], [b"\x66" * 32, b"\x33" * 32])))
        self.assertNotEqual(r.parsed({**GOOD, "code": "0x" + "77" * 32})["nonce"], p["nonce"])

    def test_the_call_takes_the_signed_fields_in_order(self):
        p = r.parsed(GOOD)
        self.assertEqual(r.args_of(p), [p["trader"], 1, bytes.fromhex("66" * 32), bytes.fromhex("33" * 32), 0, 2_000, 27,
                                        bytes.fromhex("44" * 32), bytes.fromhex("55" * 32)])
        self.assertEqual(len(r.TYPES), len(r.args_of(p)))


if __name__ == "__main__":
    unittest.main()
