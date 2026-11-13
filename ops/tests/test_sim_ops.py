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
        self.assertEqual(constant("MIN_TERM"), "184 days")

    def test_deploy_names_the_closer_a_fourth_key(self):
        """The closer alone closes a pool before its term; the operator opens our pools, and a pool's creator is
        never the closer, so the four keys are distinct, checked before anything is sent."""
        src = (ROOT / "ops/deploy_sim.py").read_text(encoding="utf-8")
        self.assertIn('("setCloser", "setCloser(address)", ["address"], [closer])', src)
        self.assertIn("function setCloser(address closer_) external onlyOperator", FACTORY)
        a, b = "0x" + "11" * 20, "0x" + "22" * 20
        for keys in ((a, b, a), (a, b, b), (a, a, b)):
            with self.assertRaises(SystemExit) as caught:
                d.main(["deploy", "--label", "never-written", "--filler", keys[0], "--checker", keys[1], "--closer", keys[2]])
            self.assertIn("three keys", str(caught.exception))
        # A zero key is refused before anything is deployed, even with --send (review of #282).
        from unittest import mock
        zero = "0x" + "00" * 20
        for keys in ((a, b, zero), (zero, a, b), (a, zero, b)):
            with mock.patch.object(d, "deploy_contract") as deployed, self.assertRaises(SystemExit) as caught:
                d.main(["deploy", "--label", "never-written", "--filler", keys[0], "--checker", keys[1], "--closer", keys[2],
                        "--send"])
            self.assertIn("none of them 0x0", str(caught.exception))
            deployed.assert_not_called()

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
            self.assertIn("72 hours after the payout is claimed if the pool's approver has approved it, and 7 days after "
                          "it is claimed if not", text)
            self.assertIn("Until it is paid, the platform's checker may stop it", text)
            self.assertIn("once a sale names a code, neither changes", text)
            self.assertIn("in the part of every price the pool took that its code's sales brought", text)
            self.assertIn("The pool runs at least 6 months (184 days) from its opening", text)
            self.assertIn("the platform may cancel a withdrawal request; it never moves the money", text)
            self.assertIn("a funded trader's payout stays in the pool for as long as the stage runs; the creator takes "
                          "profit only above it", text)
            self.assertIn("A share paid to a code's owner is never taken back", text)
            self.assertIn("USDC sent to the pool other than by the creator's addToPot counts as profit", text)
            self.assertIn("A stopped payout stays in the pool for at least 14 days while the trader can object, unless the "
                          "checker lifts the stop within them and it is paid as any payout; after them anyone may make "
                          "the stop final, and only from then is the money the pool's",
                          text)
            self.assertIn("only the platform can close it sooner, with the money going where it would anyway", text)
            self.assertIn("The creator withdraws by a request: the money leaves no sooner than 7 days after it, within "
                          "3 days after that, only what the pool can spare then, and never more than was asked; the "
                          "platform may cancel a withdrawal request; it never moves the money", text)

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
               [{"code": " GROUP-ONE", "partner": "0x" + "11" * 20, "bps": 0}],
               [{"code": "GROUP-ONE", "partner": "0x" + "11" * 20}],
               ["GROUP-ONE"],
               [{"code": "GROUP-ONE\n", "partner": "0x" + "11" * 20, "bps": 0}],
               [{"code": "GROUP-ONE\u00a0", "partner": "0x" + "11" * 20, "bps": 0}],
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


class TheClosing(unittest.TestCase):
    """`close` and `finish` say, before anything is sent, as whom the pool would take the call or why it would
    refuse; the contract decides the same (test/sim/SimPool.t.sol)."""
    CREATOR, CLOSER, OTHER = ("0x" + x * 20 for x in ("Aa", "Cc", "Ee"))
    DAY = 86_400
    WEEK = 7 * DAY

    def state(self, **kw):
        st = {"creator": d.to_checksum_address(self.CREATOR), "closer": d.to_checksum_address(self.CLOSER),
              "closed": False, "finished": False, "locked_until": 184 * self.DAY, "live": 0, "owed": 0, "reserved": 0,
              "fund_window": 7 * self.DAY, "closed_at": 0, "requested": 0, "ready_at": 0, "withdrawable": 0,
              "delay": 7 * self.DAY, "window": 3 * self.DAY}
        st.update(kw)
        return st

    def test_before_the_term_only_the_closer_closes(self):
        creator, closer, other = (d.to_checksum_address(a) for a in (self.CREATOR, self.CLOSER, self.OTHER))
        before, at = 184 * self.DAY - 1, 184 * self.DAY
        self.assertIn("before the term", d.close_check(self.state(), before, closer))
        with self.assertRaises(SystemExit) as caught:
            d.close_check(self.state(), before, creator)
        self.assertIn("Locked", str(caught.exception))
        self.assertEqual(d.close_check(self.state(), at, creator), "the creator", "from the term on")
        self.assertEqual(d.close_check(self.state(), at, closer), "the factory's closer")
        for st, who in ((self.state(), other), (self.state(closed=True), closer)):
            with self.assertRaises(SystemExit):
                d.close_check(st, at, who)

    def test_finish_names_what_keeps_the_pool_from_finishing(self):
        creator, closer, other = (d.to_checksum_address(a) for a in (self.CREATOR, self.CLOSER, self.OTHER))
        with self.assertRaises(SystemExit) as caught:
            d.finish_check(self.state(), self.WEEK, creator)
        self.assertIn("not closed yet", str(caught.exception))
        self.assertIn("only the closer closes it", str(caught.exception))
        closed = self.state(closed=True)
        self.assertEqual(d.finish_check(closed, self.WEEK, creator), "the creator")
        self.assertEqual(d.finish_check(closed, self.WEEK, closer), "the factory's closer")
        with self.assertRaises(SystemExit):
            d.finish_check(closed, self.WEEK, other)
        with self.assertRaises(SystemExit) as caught:
            d.finish_check(self.state(closed=True, live=1, owed=800_000_000, reserved=400_000_000), self.WEEK, creator,
                           "0xEXAM runs until 2026-12-12 10:00 UTC at the latest")
        said = str(caught.exception)
        for part in ("NotOver", "1 exam(s) or stage(s) still run: 0xEXAM runs until", "800 USDC of payouts still owed",
                     "400 USDC kept for stops"):
            self.assertIn(part, said)
        with self.assertRaises(SystemExit) as caught:
            d.finish_check(self.state(closed=True, live=1), self.WEEK, creator)
        self.assertIn("--exam ADDRESS says until when", str(caught.exception))
        with self.assertRaises(SystemExit):
            d.finish_check(self.state(closed=True, finished=True), self.WEEK, creator)

    def test_finish_a_week_after_the_close(self):
        creator = d.to_checksum_address(self.CREATOR)
        closed = self.state(closed=True, closed_at=100)
        with self.assertRaises(SystemExit) as caught:
            d.finish_check(closed, 100 + self.WEEK - 1, creator)
        self.assertIn("NotReady: finish from " + d.utc(100 + self.WEEK), str(caught.exception))
        self.assertEqual(d.finish_check(closed, 100 + self.WEEK, creator), "the creator")
        with self.assertRaises(SystemExit) as caught:
            d.finish_check({**closed, "owed": 800_000_000}, 100, creator)
        self.assertIn("and not before " + d.utc(100 + self.WEEK), str(caught.exception))

    def test_request_at_most_what_the_pool_lets_go(self):
        creator, closer = (d.to_checksum_address(a) for a in (self.CREATOR, self.CLOSER))
        st = self.state(withdrawable=500_000_000)
        said = d.request_check(st, 1_000, creator, 500_000_000)
        self.assertIn("asks for 500 USDC, to be carried out from " + d.utc(1_000 + self.WEEK), said)
        self.assertIn("until " + d.utc(1_000 + self.WEEK + 3 * self.DAY), said)
        self.assertIn("replaces the standing request for 100 USDC",
                      d.request_check({**st, "requested": 100_000_000}, 1_000, creator, 1))
        for bad, word in (((st, 1_000, creator, 500_000_001), "TooMuch: the pool lets go at most 500 USDC"),
                          ((st, 1_000, closer, 1), "NotCreator"),
                          (({**st, "finished": True}, 1_000, creator, 1), "Done")):
            with self.assertRaises(SystemExit) as caught:
                d.request_check(*bad)
            self.assertIn(word, str(caught.exception))

    def test_an_amount_is_usdc_of_at_most_six_decimals(self):
        self.assertEqual(d.amount_units("84.575"), 84_575_000)
        self.assertEqual(d.amount_units(" 250 "), 250_000_000)
        for bad in ("0", "-1", "1.0000001", "abc", "nan", "inf"):
            with self.assertRaises(SystemExit):
                d.amount_units(bad)

    def test_withdraw_a_week_on_for_three_days_the_less_of_asked_and_free(self):
        creator, closer = (d.to_checksum_address(a) for a in (self.CREATOR, self.CLOSER))
        st = self.state(requested=500_000_000, ready_at=1_000, withdrawable=300_000_000)
        self.assertEqual(d.withdraw_check(st, 1_000, creator), 300_000_000, "what is free then")
        self.assertEqual(d.withdraw_check({**st, "withdrawable": 900_000_000}, 1_000, creator), 500_000_000, "what was asked")
        lapses = 1_000 + 3 * self.DAY
        self.assertEqual(d.withdraw_check(st, lapses - 1, creator), 300_000_000)
        for bad, word in (((st, 999, creator), "NotReady: the request for 500 USDC can be carried out from " + d.utc(1_000)),
                          ((st, lapses, creator), "RequestLapsed: the request lapsed at " + d.utc(lapses)),
                          (({**st, "withdrawable": 0}, 1_000, creator), "TooMuch: the pool lets nothing go now"),
                          (({**st, "requested": 0}, 1_000, creator), "NoRequest"),
                          ((st, 1_000, closer), "NotCreator"),
                          (({**st, "finished": True}, 1_000, creator), "Done")):
            with self.assertRaises(SystemExit) as caught:
                d.withdraw_check(*bad)
            self.assertIn(word, str(caught.exception))

    def test_cancel_the_creator_or_the_closer(self):
        creator, closer, other = (d.to_checksum_address(a) for a in (self.CREATOR, self.CLOSER, self.OTHER))
        st = self.state(requested=1)
        self.assertEqual(d.cancel_check(st, creator), "the creator")
        self.assertEqual(d.cancel_check(st, closer), "the factory's closer")
        for bad, word in (((st, other), "NotCreator"), ((self.state(), closer), "NoRequest")):
            with self.assertRaises(SystemExit) as caught:
                d.cancel_check(*bad)
            self.assertIn(word, str(caught.exception))

    def test_the_withdrawal_sends_only_with_send(self):
        from unittest import mock
        st = self.state(requested=500_000_000, ready_at=1_000, withdrawable=900_000_000)
        for argv, sig in ((["request", "--amount", "250"], "requestWithdraw(uint256)"), (["withdraw"], "withdraw()"),
                          (["cancel"], "cancelWithdraw()")):
            with mock.patch.object(d, "pool_state", return_value=st), mock.patch.object(d, "chain_now", return_value=1_000), \
                    mock.patch.object(d.c, "address_of", return_value=d.to_checksum_address(self.CREATOR)), \
                    mock.patch.object(d.c, "assert_testnet"), mock.patch.object(d.c, "account") as key, \
                    mock.patch.object(d.c, "transact", return_value={"transactionHash": "0x01"}) as sent:
                self.assertEqual(d.main([argv[0], "--pool", self.OTHER, "--key", "creator", *argv[1:]]), 0)
                sent.assert_not_called()
                key.assert_not_called()
                self.assertEqual(d.main([argv[0], "--pool", self.OTHER, "--key", "creator", *argv[1:], "--send"]), 0)
                self.assertEqual(sent.call_args[0][2], sig)
                if argv[0] == "request":
                    self.assertEqual(sent.call_args[0][4], [250_000_000])

    def test_approve_only_the_approver_only_waiting_payouts(self):
        approver, other = (d.to_checksum_address(a) for a in (self.CLOSER, self.OTHER))
        hard = 7 * self.DAY - 3 * self.DAY
        waiting = ("0x" + "11" * 20, "0x" + "77" * 20, 800_000_000, 1_000, 1, False, 0, False)
        lines = d.approve_check(approver, approver, {3: waiting}, [3], hard)
        self.assertIn("payout 3: 800 USDC to 0x7777", lines[0])
        self.assertIn("goes out from " + d.utc(1_000) + " approved; " + d.utc(1_000 + hard) + " without", lines[0])
        for args, word in (((approver, other, {3: waiting}, [3], hard), "NotApprover"),
                           (("0x" + "00" * 20, approver, {3: waiting}, [3], hard), "the pool has no approver"),
                           ((approver, approver, {}, [3], hard), "payout 3 does not exist"),
                           ((approver, approver, {3: waiting[:4] + (2,) + waiting[5:]}, [3], hard), "payout 3 is Stopped"),
                           ((approver, approver, {3: waiting[:7] + (True,)}, [3], hard), "approved already")):
            with self.assertRaises(SystemExit) as caught:
                d.approve_check(*args)
            self.assertIn(word, str(caught.exception))
        self.assertEqual(d.ids_of("3, 4,7"), [3, 4, 7])
        for bad in ("", "3,3", "a", "-1"):
            with self.assertRaises(SystemExit):
                d.ids_of(bad)

    def test_approve_sends_only_with_send(self):
        from unittest import mock
        approver = d.to_checksum_address(self.CLOSER)
        waiting = ("0x" + "11" * 20, "0x" + "77" * 20, 800_000_000, 1_000, 1, False, 0, False)
        answers = {"payoutCount()": [5], "payout(uint256)": [waiting], "factory()": ["0x" + "99" * 20],
                   "PAYOUT_HARD()": [7 * self.DAY], "PAYOUT_DELAY()": [3 * self.DAY], "approver()": [approver]}
        with mock.patch.object(d.c, "call_view", side_effect=lambda to, sig, *a: answers[sig]), \
                mock.patch.object(d.c, "address_of", return_value=approver), mock.patch.object(d.c, "assert_testnet"), \
                mock.patch.object(d.c, "account") as key, \
                mock.patch.object(d.c, "transact", return_value={"transactionHash": "0x01"}) as sent:
            self.assertEqual(d.main(["approve", "--pool", self.OTHER, "--key", "approver", "--ids", "3,4"]), 0)
            sent.assert_not_called()
            key.assert_not_called()
            self.assertEqual(d.main(["approve", "--pool", self.OTHER, "--key", "approver", "--ids", "3,4", "--send"]), 0)
            self.assertEqual(sent.call_args[0][2:], ("approvePayouts(uint256[])", ["uint256[]"], [[3, 4]]))

    def test_the_approver_is_named_by_the_creator(self):
        from unittest import mock
        creator = d.to_checksum_address(self.CREATOR)
        with mock.patch.object(d.c, "call_view", return_value=[creator]), \
                mock.patch.object(d.c, "address_of", return_value=d.to_checksum_address(self.OTHER)):
            with self.assertRaises(SystemExit) as caught:
                d.main(["approver", "--pool", self.OTHER, "--key", "x", "--address", self.CLOSER])
            self.assertIn("NotCreator", str(caught.exception))
        with mock.patch.object(d.c, "call_view", return_value=[creator]), \
                mock.patch.object(d.c, "address_of", return_value=creator), mock.patch.object(d.c, "assert_testnet"), \
                mock.patch.object(d.c, "account"), \
                mock.patch.object(d.c, "transact", return_value={"transactionHash": "0x01"}) as sent:
            self.assertEqual(d.main(["approver", "--pool", self.OTHER, "--key", "creator", "--address", self.CLOSER]), 0)
            sent.assert_not_called()
            self.assertEqual(d.main(["approver", "--pool", self.OTHER, "--key", "creator", "--address", self.CLOSER,
                                     "--send"]), 0)
            self.assertEqual(sent.call_args[0][2], "setApprover(address)")

    def test_until_when_an_exam_of_several_steps_runs_and_a_passed_stage(self):
        x = {"status": 1, "started": 1_000, "duration": 30 * self.DAY, "passed_at": 0, "done": False, "kind": 1,
             "step": 0, "steps": 2}
        said = d.exam_text("E", x, 2_000, 7 * self.DAY)
        self.assertIn("step 1 of 2: this step runs until " + d.utc(1_000 + 30 * self.DAY), said)
        self.assertIn("1 more step(s) of up to 30 day(s) each follow, so until " + d.utc(1_000 + 60 * self.DAY), said)
        self.assertIn("runs until " + d.utc(1_000 + 30 * self.DAY) + " at the latest",
                      d.exam_text("E", {**x, "step": 1}, 2_000, 7 * self.DAY), "the last step: its own deadline")
        stage = {**x, "status": 2, "kind": 2, "passed_at": 5_000, "steps": 1}
        self.assertIn("passed its stage: the keeper claims its payout", d.exam_text("E", stage, 6_000, 7 * self.DAY))

    def test_a_signer_elsewhere_gets_the_calls_printed_and_nothing_is_sent(self):
        """Our creator is a Safe and our approver a Ledger: --signer prints the calls for them, reads no key, sends
        nothing; the checks are made as that address."""
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        safe = d.to_checksum_address(self.CREATOR)
        st = self.state(requested=500_000_000, ready_at=1_000, withdrawable=900_000_000)
        for argv, sig in ((["request", "--amount", "250"], "requestWithdraw(uint256)"), (["withdraw"], "withdraw()"),
                          (["cancel"], "cancelWithdraw()")):
            out = io.StringIO()
            with mock.patch.object(d, "pool_state", return_value=st), mock.patch.object(d, "chain_now", return_value=1_000), \
                    mock.patch.object(d.c, "address_of") as key_address, mock.patch.object(d.c, "account") as key, \
                    mock.patch.object(d.c, "transact") as sent, redirect_stdout(out):
                self.assertEqual(d.main([argv[0], "--pool", self.OTHER, "--signer", safe, *argv[1:], "--send"]), 0)
            sent.assert_not_called()
            key.assert_not_called()
            key_address.assert_not_called()
            said = out.getvalue()
            batch = json.loads(said[said.index("{"):])
            self.assertEqual(len(batch["transactions"]), 1)
            tx = batch["transactions"][0]
            self.assertEqual((tx["to"], tx["value"]), (d.to_checksum_address(self.OTHER), "0"))
            self.assertTrue(tx["data"].startswith("0x" + keccak(text=sig)[:4].hex()), sig)
        # The closer's close, for a signer elsewhere too.
        out = io.StringIO()
        with mock.patch.object(d, "pool_state", return_value=st), mock.patch.object(d, "chain_now", return_value=1_000), \
                mock.patch.object(d.c, "transact") as sent, redirect_stdout(out):
            self.assertEqual(d.main(["close", "--pool", self.OTHER, "--signer", d.to_checksum_address(self.CLOSER)]), 0)
        sent.assert_not_called()
        said = out.getvalue()
        self.assertTrue(json.loads(said[said.index("{"):])["transactions"][0]["data"].startswith(
            "0x" + keccak(text="close()")[:4].hex()))
        # The checks are the signer's: a Safe that is not the creator is refused, before anything is printed.
        other = d.to_checksum_address(self.OTHER)
        with mock.patch.object(d, "pool_state", return_value=st), mock.patch.object(d, "chain_now", return_value=1_000):
            with self.assertRaises(SystemExit) as caught:
                d.main(["withdraw", "--pool", self.OTHER, "--signer", other])
        self.assertIn("NotCreator", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            d.main(["withdraw", "--pool", self.OTHER])
        self.assertIn("--key or --signer", str(caught.exception))

    def test_a_pool_for_a_safe_is_three_calls_in_order_and_its_codes_a_batch(self):
        from unittest import mock
        import io
        from contextlib import redirect_stdout
        safe = d.to_checksum_address(self.CREATOR)
        record = {"SimPoolFactory": "0x" + "f1" * 20, "TermsBook": "0x" + "b0" * 20, "usdc": "0x" + "c0" * 20}
        out = io.StringIO()
        with mock.patch.object(d.deployments, "load", return_value=record), \
                mock.patch.object(d.c, "account") as key, mock.patch.object(d.c, "send_tx") as sent, redirect_stdout(out):
            self.assertEqual(d.main(["pool", "--deployment", "x", "--plan", "deployments/plans/sim-first-pool.json",
                                     "--signer", safe, "--send"]), 0)
        key.assert_not_called()
        sent.assert_not_called()
        said = out.getvalue()
        txs = json.loads(said[said.index("{"):])["transactions"]
        self.assertEqual([t["to"].lower() for t in txs], [record["TermsBook"], record["usdc"], record["SimPoolFactory"]])
        self.assertEqual([t["value"] for t in txs], ["0", "0", str(d.CREATION_FEE)], "1 HYPE with create")
        self.assertTrue(txs[2]["data"].startswith("0x" + keccak(text=f"create({d.PLAN_T})")[:4].hex()))
        # Its codes: one setCode each still to set, read from the pool as the Safe.
        p1 = d.to_checksum_address("0x" + "11" * 20)
        plan = {"codes": [{"code": "A", "partner": p1, "bps": 5000}, {"code": "B", "partner": "creator", "bps": 0}]}
        out = io.StringIO()
        with mock.patch.object(d.pathlib.Path, "read_text", return_value=json.dumps(plan)), \
                mock.patch.object(d.c, "call_view", return_value=(d.ZERO, 0, False)), \
                mock.patch.object(d.c, "account") as key, mock.patch.object(d.c, "transact") as sent, redirect_stdout(out):
            self.assertEqual(d.main(["codes", "--pool", self.OTHER, "--plan", "p.json", "--signer", safe]), 0)
        key.assert_not_called()
        sent.assert_not_called()
        said = out.getvalue()
        txs = json.loads(said[said.index("{"):])["transactions"]
        self.assertEqual(len(txs), 2)
        self.assertTrue(all(t["data"].startswith("0x" + keccak(text=d.SET_CODE)[:4].hex()) for t in txs))
        self.assertIn(safe[2:].lower(), txs[1]["data"].lower(), "the creator's own code names the Safe")

    def test_until_when_an_exam_runs(self):
        x = {"status": 1, "started": 1_000, "duration": 30 * self.DAY, "passed_at": 0, "done": False}
        self.assertIn("runs until " + d.utc(1_000 + 30 * self.DAY), d.exam_text("E", x, 2_000, 7 * self.DAY))
        self.assertIn("past its deadline", d.exam_text("E", x, 1_000 + 30 * self.DAY, 7 * self.DAY))
        passed = {**x, "status": 2, "passed_at": 5_000}
        self.assertIn("can be started until " + d.utc(5_000 + 7 * self.DAY), d.exam_text("E", passed, 6_000, 7 * self.DAY))
        self.assertIn("was not funded", d.exam_text("E", passed, 5_001 + 7 * self.DAY, 7 * self.DAY))
        self.assertIn("has ended (Breached)", d.exam_text("E", {**x, "status": 3}, 2_000, 7 * self.DAY))
        self.assertIn("is done here", d.exam_text("E", {**x, "done": True}, 2_000, 7 * self.DAY))

    def test_the_status_list_is_paper_exams(self):
        body = re.search(r"enum Status \{(.*?)\}", (ROOT / "src/exam/PaperExam.sol").read_text(encoding="utf-8"), re.S).group(1)
        self.assertEqual([w.strip() for w in body.split(",") if w.strip()], d.STATUS)

    def test_they_send_only_with_send(self):
        from unittest import mock
        st = self.state(closed=True)
        with mock.patch.object(d, "pool_state", return_value=self.state()), mock.patch.object(d, "chain_now", return_value=10), \
                mock.patch.object(d.c, "address_of", return_value=d.to_checksum_address(self.CLOSER)), \
                mock.patch.object(d.c, "transact") as sent, mock.patch.object(d.c, "account") as key:
            self.assertEqual(d.main(["close", "--pool", self.OTHER, "--key", "closer"]), 0)
            sent.assert_not_called()
            key.assert_not_called()
        with mock.patch.object(d, "pool_state", return_value=st), mock.patch.object(d, "chain_now", return_value=self.WEEK), \
                mock.patch.object(d.c, "address_of", return_value=d.to_checksum_address(self.CREATOR)), \
                mock.patch.object(d.c, "assert_testnet"), mock.patch.object(d.c, "account"), \
                mock.patch.object(d.c, "transact", return_value={"transactionHash": "0x01"}) as sent:
            self.assertEqual(d.main(["finish", "--pool", self.OTHER, "--key", "deployer", "--send"]), 0)
            self.assertEqual(sent.call_args[0][2], "finish()")


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
