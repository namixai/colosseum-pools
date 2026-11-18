"""The simulated pool's deploy script and relayer: their numbers and layouts against the contracts,
the first pool's plan, and what the relayer refuses before it spends gas."""
import io
import json
import pathlib
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from eth_utils import keccak

from ops import deploy_sim as d
from ops import sim_keeper as keeper
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
        # The fee is the factory's, set at its deploy and changed by its operator, above zero with no cap: the script
        # holds each network's fee at the deploy, and asks mainnet to confirm one above 10 HYPE.
        self.assertIn("uint256 public creationFee;", FACTORY)
        self.assertIsNone(re.search(r"CREATION_FEE", FACTORY), "no constant, immutable or cap of the fee is left")
        self.assertIn("function setCreationFee(uint256 fee) external onlyOperator", FACTORY)
        self.assertIn("if (fee == 0) revert BadFee();", FACTORY)
        self.assertEqual(d.LARGE_FEE, 10 * 10**18)
        self.assertEqual(d.CREATION_FEES, {"mainnet": 10**18, "testnet": 10**17}, "1 HYPE on mainnet, 0.1 on testnet")
        self.assertIn("uint256 creationFee_) {", FACTORY)
        self.assertEqual(int(constant("PAYOUT_BPS")), d.PAYOUT_BPS)
        # The factory takes it from SimPool.sol, whose pool counts two payouts of a line free before it sells.
        self.assertEqual(constant("LOCKED_PAYOUTS"), "uint8(COVER_PAYOUTS)")
        self.assertEqual(int(re.search(r"constant COVER_PAYOUTS = (\d+);", POOL).group(1)), d.LOCKED_PAYOUTS)
        self.assertEqual(int(constant("MIN_PARTNER_BPS")), d.MIN_PARTNER_BPS)
        self.assertEqual(int(constant("MAX_PARTNER_BPS")), d.MAX_PARTNER_BPS)
        self.assertNotIn("MAX_CODES", FACTORY, "a pool has no limit on its codes")
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
                                 "bytes32", "SimTier[]", "uint8", "uint256", "uint32"])
        self.assertTrue(d.PLAN_T.startswith("((uint64,uint64)[]," + d.RULES_T + "," + d.TERMS_T))
        self.assertTrue(d.PLAN_T.endswith(",uint32,bool,uint16,bytes32," + d.TIER_T + "[],uint8,uint256,uint32)"))
        tier = re.search(r"struct SimTier \{(.*?)\n\}", POOL, re.S).group(1)
        self.assertEqual([l.split()[0] for l in tier.splitlines() if l.strip() and not l.strip().startswith("//")],
                         ["uint16", "uint16", "uint8"])
        self.assertEqual(d.TIER_T, "(uint16,uint16,uint8)")

    def test_the_scripts_calls_are_the_pools(self):
        self.assertIn("function setSplitCode(bytes32 code, address partner, uint16 bps, address author, uint16 authorBps)",
                      re.sub(r"\s+", " ", POOL))
        self.assertEqual(d.SET_CODE, "setSplitCode(bytes32,address,uint16,address,uint16)")
        self.assertEqual(d.SET_CODE_TYPES, ["bytes32", "address", "uint16", "address", "uint16"])
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
        self.assertIn("keccak256(abi.encode(code, line, salt))", POOL)
        self.assertEqual(r.SIG.split("(")[0], "buyWithAuthorization")
        self.assertEqual(r.EXAM_SOLD, declared("src/sim/SimPool.sol", "ExamSold"))


class TheHandOver(unittest.TestCase):
    """`deploy` hands the factory to our Safe last: setOperator, read before and after; needed on mainnet."""
    F, S, D = "0x" + "fa" * 20, "0x" + "5a" * 20, "0x" + "de" * 20
    KEYS = ["--filler", "0x" + "11" * 20, "--checker", "0x" + "22" * 20, "--closer", "0x" + "33" * 20]

    def argv(self, *extra):
        return ["deploy", "--label", "never-written", *self.KEYS, *extra]

    OWNERS = ["0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20]

    def send(self, safe, operators=None, code="0x6080", reads_of_safe=None, fee_read=10**17, extra=()):
        """A testnet deploy with --send, every chain call mocked: the transactions it sends, in order, and the record.
        `reads_of_safe` overrides what the Safe answers to getThreshold() and getOwners(): a tuple, or an exception
        to raise."""
        answers = {"getThreshold()": (2,), "getOwners()": (self.OWNERS,), **(reads_of_safe or {})}

        def view(to, sig, *rest):
            if sig in answers:
                if isinstance(answers[sig], Exception):
                    raise answers[sig]
                return answers[sig]
            return (next(reads),)

        from types import SimpleNamespace
        from unittest import mock
        sent, records = [], []
        reads = iter(operators or [self.D, safe])
        deployed = iter(["0x" + f"{i:040x}" for i in range(1, 8)])
        op = SimpleNamespace(address=d.to_checksum_address(self.D))
        transact = lambda acct, to, sig, types, values: sent.append((to, sig, values)) or {"transactionHash": f"0x{len(sent):x}"}
        argv = self.argv("--send", *(["--safe", safe] if safe else []), *extra)

        def rpc(m, p=()):
            if m == "eth_chainId":
                return hex(d.c.CHAIN_ID)
            if m == "eth_call":
                self.assertEqual(p[0]["data"], "0x" + keccak(text="creationFee()")[:4].hex())
                fee_reads.append(p[0]["to"])
                return "0x%064x" % fee_read
            return code
        fee_reads = []
        self.fee_reads = fee_reads
        with mock.patch.object(d.sn, "confirm_send"), mock.patch.object(d.sn, "check_assets"), \
                mock.patch.object(d.c, "account", return_value=op), mock.patch.object(d, "big_blocks") as big, \
                mock.patch.object(d, "deploy_contract", side_effect=lambda *a, **k: (next(deployed), {"blockNumber": "0x10"})) as dc, \
                mock.patch.object(d.c, "transact", side_effect=transact), \
                mock.patch.object(d.c, "call_view", side_effect=view), \
                mock.patch.object(d.c, "rpc", side_effect=rpc), \
                mock.patch.object(d, "git_head", return_value="ab" * 20), \
                mock.patch.object(d, "save", side_effect=lambda out, rec: records.append(json.loads(json.dumps(rec)))), \
                mock.patch.object(d, "mark_incomplete") as incomplete, \
                mock.patch.object(d.deployments, "load", return_value={"keeper_nodes": {}}), mock.patch("builtins.print"):
            try:
                result = d.main(argv)
            except SystemExit as exc:
                result = exc
        return result, sent, records, dc, big, incomplete

    def test_the_plan_says_the_hand_over_or_that_the_key_stays(self):
        from unittest import mock
        with mock.patch("builtins.print") as out:
            self.assertEqual(d.main(self.argv("--safe", self.S)), 0)
        text = "\n".join(str(x.args[0]) for x in out.call_args_list)
        self.assertIn(f"would hand over  setOperator {d.to_checksum_address(self.S)} last", text)
        with mock.patch("builtins.print") as out:
            self.assertEqual(d.main(self.argv()), 0)
        self.assertIn("stays the deployer's key (no --safe; testnet only)", "\n".join(str(x.args[0]) for x in out.call_args_list))

    def test_setoperator_to_the_safe_is_the_last_call_and_read_before_and_after(self):
        result, sent, records, _, _, incomplete = self.send(self.S)
        self.assertEqual(result, 0)
        factory = sent[0][0]
        self.assertEqual(sent[-1], (factory, "setOperator(address)", [d.to_checksum_address(self.S)]))
        self.assertEqual([t[1] for t in sent].count("setOperator(address)"), 1)
        self.assertEqual(records[-1]["status"], d.DONE)
        self.assertEqual(records[-1]["operator"], d.to_checksum_address(self.S))
        self.assertEqual(records[-1]["deployer"], d.to_checksum_address(self.D))
        self.assertIn("setOperator", records[-1]["tx"])
        # Until the read after, the record does not name the Safe as the operator, nor say it is complete.
        before_done = [r for r in records if "setOperator" in r["tx"]][0]
        self.assertEqual(before_done["operator"], d.to_checksum_address(self.D))
        self.assertNotEqual(before_done["status"], d.DONE)
        incomplete.assert_not_called()

    def test_without_a_safe_on_testnet_the_key_stays_and_nothing_is_handed_over(self):
        result, sent, records, *_ = self.send(None)
        self.assertEqual(result, 0)
        self.assertNotIn("setOperator(address)", [t[1] for t in sent])
        self.assertEqual(records[-1]["operator"], d.to_checksum_address(self.D))

    def test_a_safe_that_is_zero_or_a_platform_key_is_refused_before_anything_is_sent(self):
        for safe, said in (("0x" + "00" * 20, "--safe is 0x0"), (self.KEYS[1], "the filler, the checker or the closer"),
                           (self.KEYS[5], "the filler, the checker or the closer")):
            result, sent, _, dc, big, _ = self.send(safe)
            self.assertIsInstance(result, SystemExit)
            self.assertIn(said, str(result))
            self.assertEqual(sent, [])
            dc.assert_not_called(), big.assert_not_called()

    def test_the_deployers_own_key_or_an_address_without_code_is_refused_before_anything_is_sent(self):
        no_code = ((self.S, c, "has no code on testnet") for c in ("0x", "0x0", "0X", "0X0", "", None))
        for safe, code, said in ((self.D, "0x6080", "the deployer's own key"), *no_code):
            result, sent, _, dc, big, _ = self.send(safe, code=code)
            self.assertIsInstance(result, SystemExit)
            self.assertIn(said, str(result))
            self.assertEqual(sent, [])
            dc.assert_not_called(), big.assert_not_called()

    def test_an_address_that_does_not_answer_as_a_safe_is_refused_before_anything_is_sent(self):
        for reads, said in (({"getThreshold()": RuntimeError("revert")}, "is not a Safe: getThreshold() or getOwners()"),
                            ({"getOwners()": ValueError("empty")}, "is not a Safe: getThreshold() or getOwners()"),
                            ({"getThreshold()": (0,)}, "threshold 0 with 3 owners"),
                            ({"getThreshold()": (4,)}, "threshold 4 with 3 owners"),
                            ({"getOwners()": ([],)}, "with 0 owners")):
            result, sent, _, dc, big, _ = self.send(self.S, reads_of_safe=reads)
            self.assertIsInstance(result, SystemExit, reads)
            self.assertIn(said, str(result))
            self.assertEqual(sent, [])
            dc.assert_not_called(), big.assert_not_called()

    def test_the_person_confirming_sees_the_safes_threshold_and_owners(self):
        from unittest import mock
        texts = []

        def stop_at_the_phrase(args, what, c):
            texts.append(what())      # the confirmation's text is made once the person is at a terminal
            raise SystemExit("stop at the confirmation")

        with mock.patch.object(d.sn, "confirm_send", side_effect=stop_at_the_phrase), \
                mock.patch.object(d.c, "rpc", side_effect=lambda m, p=(): hex(d.c.CHAIN_ID) if m == "eth_chainId" else "0x6080"), \
                mock.patch.object(d.c, "call_view", side_effect=lambda to, sig, *r: {
                    "getThreshold()": (2,), "getOwners()": (self.OWNERS,)}[sig]), \
                mock.patch("builtins.print") as out:
            with self.assertRaises(SystemExit):
                d.main(self.argv("--send", "--safe", self.S))
        printed = "\n".join(str(x.args[0]) for x in out.call_args_list)
        for text in (texts[0], printed):
            self.assertIn(d.to_checksum_address(self.S), text)
            self.assertIn("2 of its 3 owners must sign", text)
            for o in self.OWNERS:
                self.assertIn(d.to_checksum_address(o), text)

    def test_an_operator_that_is_not_the_deployer_before_sends_nothing_and_leaves_the_deploy_incomplete(self):
        result, sent, records, _, _, incomplete = self.send(self.S, operators=["0x" + "99" * 20])
        self.assertIsInstance(result, SystemExit)
        self.assertIn("not the deployer's key: setOperator not sent", str(result))
        self.assertNotIn("setOperator(address)", [t[1] for t in sent])
        self.assertNotEqual(records[-1]["status"], d.DONE)
        incomplete.assert_called_once()

    def test_an_operator_that_is_not_the_safe_after_leaves_the_deploy_incomplete(self):
        result, sent, records, _, _, incomplete = self.send(self.S, operators=[self.D, self.D])
        self.assertIsInstance(result, SystemExit)
        self.assertIn("not the Safe", str(result))
        self.assertNotEqual(records[-1]["status"], d.DONE)
        self.assertEqual(records[-1]["operator"], d.to_checksum_address(self.D))
        incomplete.assert_called_once()

    def test_the_factory_is_deployed_with_the_networks_fee_read_back_and_recorded(self):
        result, sent, records, dc, _, incomplete = self.send(None)
        self.assertEqual(result, 0)
        factory_call = next(call for call in dc.call_args_list if call.args[3] == "SimPoolFactory")
        types, values = factory_call.args[5], factory_call.args[6]
        self.assertEqual((types[-1], values[-1]), ("uint256", 10**17), "testnet's 0.1 HYPE, the constructor's last")
        self.assertEqual(len(types), 5)
        self.assertEqual(self.fee_reads, [sent[0][0]], "read back from the factory once, before it is configured")
        self.assertEqual((records[-1]["creation_fee_at_deploy_wei"], records[-1]["creation_fee_at_deploy_hype"]),
                         (str(10**17), "0.1"))
        incomplete.assert_not_called()
        # A fee named for testnet is the one deployed and recorded.
        result, _, records, dc, _, _ = self.send(None, fee_read=25 * 10**16, extra=("--creation-fee", "0.25"))
        self.assertEqual(result, 0)
        factory_call = next(call for call in dc.call_args_list if call.args[3] == "SimPoolFactory")
        self.assertEqual(factory_call.args[6][-1], 25 * 10**16)
        self.assertEqual(records[-1]["creation_fee_at_deploy_hype"], "0.25")

    def test_a_factory_that_reads_back_another_fee_leaves_the_deploy_incomplete(self):
        result, sent, records, _, _, incomplete = self.send(None, fee_read=10**18)
        self.assertIsInstance(result, SystemExit)
        self.assertIn(f"takes {10**18} wei to open a pool, not the {10**17} deployed", str(result))
        self.assertEqual(sent, [], "nothing configured on a factory of another fee")
        self.assertEqual(records, [], "no record saved as configuring")
        self.assertEqual(len(self.fee_reads), 1, "the fee that failed is the one said, read once")
        incomplete.assert_called_once()
        self.assertNotEqual(incomplete.call_args.args[1]["status"], d.DONE)

    def test_the_call_is_the_factorys(self):
        self.assertIn("function setOperator(address operator_) external onlyOperator", FACTORY)
        self.assertIn("address public operator;", FACTORY)


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


class TheCreationFee(unittest.TestCase):
    """The fee for opening a pool is set in the factory at its deploy: 0.1 HYPE on testnet unless named, 1 on mainnet
    and nothing else there; its operator may change it after (`fee`), so every later command reads it from the
    factory."""
    KEYS = ["--filler", "0x" + "11" * 20, "--checker", "0x" + "22" * 20, "--closer", "0x" + "33" * 20]

    def fee(self, net, given=None, confirm=False):
        from types import SimpleNamespace
        from spike.hlspike import network as networks
        with mock.patch.object(d.c, "NETWORK", {"testnet": networks.TESTNET, "mainnet": networks.MAINNET}[net]):
            return d.creation_fee_of(SimpleNamespace(creation_fee=given, confirm_large_fee=confirm))

    def test_each_network_has_its_own_and_mainnet_only_its_own(self):
        self.assertEqual(self.fee("testnet"), 10**17)
        self.assertEqual(self.fee("mainnet"), 10**18)
        self.assertEqual(self.fee("mainnet", "1"), 10**18)
        self.assertEqual(self.fee("mainnet", "1.000"), 10**18)
        self.assertEqual(self.fee("testnet", "0.25"), 25 * 10**16)
        self.assertEqual(self.fee("mainnet", "0.5"), 5 * 10**17, "mainnet takes another fee when named")
        self.assertEqual(self.fee("mainnet", "10"), 10 * 10**18, "10 HYPE needs no confirmation")
        self.assertEqual(self.fee("mainnet", "10.5", confirm=True), 105 * 10**17, "above it, with the confirmation")
        self.assertEqual(self.fee("testnet", "1000"), 1000 * 10**18, "testnet: no cap and no confirmation")
        self.assertEqual(self.fee("testnet", "0.000000000000000001"), 1)
        self.assertEqual(self.fee("testnet", "1e3"), 1000 * 10**18)
        self.assertEqual(self.fee("testnet", "1.2e40"), 12 * 10**57, "converted from its digits, not rounded")
        self.assertEqual(self.fee("testnet", "123456789012345678901234567890.123456789012345678"),
                         123456789012345678901234567890123456789012345678, "every digit kept")
        for net, given, said in (("mainnet", "10.000000000000000001", "more than 10 HYPE on mainnet: add "
                                                                       "--confirm-large-fee"),
                                 ("mainnet", "100", "nothing was sent"),
                                 ("testnet", "0", "above 0: the factory refuses a fee of none"),
                                 ("mainnet", "0", "above 0: the factory refuses a fee of none"),
                                 ("testnet", "-0.1", "above 0"),
                                 ("testnet", "1.2e59", "does not fit a uint256 of wei"),
                                 ("testnet", "1e999999", "does not fit a uint256 of wei"),
                                 ("testnet", "0.0000000000000000001", "at most 18 decimals"),
                                 ("testnet", "10.0000000000000000000000000001", "at most 18 decimals"),
                                 ("testnet", "0.10000000000000000000000000001", "at most 18 decimals"),
                                 ("testnet", "Infinity", "not an amount of HYPE"),
                                 ("testnet", "-Infinity", "not an amount of HYPE"),
                                 ("testnet", "NaN", "not an amount of HYPE"),
                                 ("testnet", "sNaN", "not an amount of HYPE"),
                                 ("testnet", "one", "not an amount of HYPE")):
            with self.assertRaises(SystemExit) as caught:
                self.fee(net, given)
            self.assertIn(said, str(caught.exception), (net, given))

    def test_the_plan_says_the_fee_and_the_network_and_sends_nothing(self):
        for extra, said in (([], "creation fee     0.1 HYPE to open a pool on testnet (100000000000000000 wei), set in "
                                 "the factory; its operator may change it after (`fee`)"),
                            (["--creation-fee", "0.5"], "creation fee     0.5 HYPE to open a pool on testnet")):
            out = io.StringIO()
            with mock.patch.object(d.c, "rpc") as rpc, mock.patch.object(d, "deploy_contract") as dc, redirect_stdout(out):
                self.assertEqual(d.main(["deploy", "--label", "never-written", *self.KEYS, *extra]), 0)
            rpc.assert_not_called(), dc.assert_not_called()
            self.assertIn(said, out.getvalue())
            self.assertIn("SimPoolFactory(usdc, exam, pool, operator, fee)", out.getvalue())
        # A fee above the bound is refused before anything is read or sent. Every way out is stubbed: should the
        # refusal ever go, the test fails at the confirmation, and no key is read and nothing reaches a network.
        reached = AssertionError("deploy went past the fee's check")
        with self.assertRaises(SystemExit) as caught, mock.patch.object(d, "deploy_contract") as dc, \
                mock.patch.object(d.sn, "confirm_send", side_effect=reached), \
                mock.patch.object(d.sn, "check_assets", side_effect=reached), \
                mock.patch.object(d.c, "account", side_effect=reached) as key, \
                mock.patch.object(d, "big_blocks", side_effect=reached) as big, \
                mock.patch.object(d.c, "rpc", side_effect=reached), mock.patch.object(d, "save"), \
                mock.patch.object(d, "mark_incomplete"):
            d.main(["deploy", "--label", "never-written", *self.KEYS, "--creation-fee", "0", "--send"])
        self.assertIn("the factory refuses a fee of none", str(caught.exception))
        dc.assert_not_called(), key.assert_not_called(), big.assert_not_called()

    def test_the_fee_is_read_from_the_factory(self):
        factory = "0x" + "f1" * 20
        with mock.patch.object(d.c, "rpc", return_value="0x%064x" % 10**17) as rpc:
            self.assertEqual(d.factory_fee(factory), 10**17)
        method, params = rpc.call_args.args
        self.assertEqual(method, "eth_call")
        self.assertEqual(params, [{"to": factory, "data": "0x" + keccak(text="creationFee()")[:4].hex()}, "latest"])
        state = {factory: {"code": "0x60"}}
        with mock.patch.object(d.c, "rpc", return_value="0x%064x" % 10**18) as rpc:
            self.assertEqual(d.factory_fee(factory, state), 10**18)
        self.assertEqual(rpc.call_args.args[1][2], state, "a factory put in the call by a state override")
        for answer in ("0x", "0x0", "", None, "0x" + "00" * 32):
            with mock.patch.object(d.c, "rpc", return_value=answer), self.assertRaises(SystemExit) as caught:
                d.factory_fee(factory)
            self.assertIn("gave no fee for opening a pool on testnet", str(caught.exception))

    def test_hype_is_written_as_people_write_it(self):
        self.assertEqual([d.hype(w) for w in (10**18, 10**17, 25 * 10**16, 1)], ["1", "0.1", "0.25", "0.000000000000000001"])


class TheFeeCommand(unittest.TestCase):
    """`fee`: setCreationFee on a deployment's factory, from its operator, within the factory's bound; read first,
    sent only with --send, read back after; for our Safe, the call printed."""
    FACTORY, OPERATOR, OTHER = "0x" + "f1" * 20, "0x" + "0a" * 20, "0x" + "0b" * 20

    def run_fee(self, argv, now=10**17, operator=None, after=None, key_address=None, net="testnet"):
        from spike.hlspike import network as networks
        reads = iter([now, after if after is not None else now])
        out = io.StringIO()
        sent = []

        def rpc(m, p=()):
            self.assertEqual(p[0], {"to": self.FACTORY, "data": "0x" + keccak(text="creationFee()")[:4].hex()})
            return "0x%064x" % next(reads)
        transact = lambda acct, to, sig, types, values: sent.append((to, sig, values)) or {"transactionHash": "0x01"}
        with mock.patch.object(d.deployments, "load", return_value={"SimPoolFactory": self.FACTORY}), \
                mock.patch.object(d.c, "rpc", side_effect=rpc), \
                mock.patch.object(d.c, "call_view", return_value=(operator or self.OPERATOR,)), \
                mock.patch.object(d.c, "address_of", return_value=d.to_checksum_address(key_address or self.OPERATOR)), \
                mock.patch.object(d.c, "account") as key, mock.patch.object(d.sn, "confirm_send") as confirm, \
                mock.patch.object(d.c, "transact", side_effect=transact), \
                mock.patch.object(d.c, "NETWORK", {"testnet": networks.TESTNET, "mainnet": networks.MAINNET}[net]), \
                mock.patch.object(d.c, "assert_network"), redirect_stdout(out):
            try:
                code = d.main(["fee", "--deployment", "x", "--network", net, *argv])
            except SystemExit as exc:
                code = exc
        return code, sent, out.getvalue(), key, confirm

    def test_without_send_it_says_what_it_would_send_and_sends_nothing(self):
        code, sent, said, key, confirm = self.run_fee(["--amount", "0.5", "--key", "deployer"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        confirm.assert_not_called()
        self.assertIn("a pool opens for 0.1 HYPE now; setCreationFee(500000000000000000) from deployer", said)
        self.assertIn("makes it 0.5 HYPE (500000000000000000 wei) for every pool opened after; the pools open already are not touched", said)
        self.assertIn("nothing was sent", said)

    def test_with_send_it_sends_from_the_operator_and_reads_the_fee_back(self):
        code, sent, said, key, confirm = self.run_fee(["--amount", "0.5", "--key", "deployer", "--send"],
                                                      after=5 * 10**17)
        self.assertEqual(code, 0)
        self.assertEqual(sent, [(self.FACTORY, "setCreationFee(uint256)", [5 * 10**17])])
        confirm.assert_called_once()
        self.assertIn("the fee to open a pool is 0.5 HYPE, tx 0x01", said)
        # A fee that does not read back as sent is said, with the transaction.
        code, sent, said, *_ = self.run_fee(["--amount", "0.5", "--key", "deployer", "--send"], after=10**17)
        self.assertIsInstance(code, SystemExit)
        self.assertIn("reads 0.1 HYPE, not 0.5: tx 0x01", str(code))

    def test_only_the_operator_within_the_bound_and_not_the_same_fee(self):
        for argv, kw, said in ((["--amount", "0.5", "--key", "other"], {"key_address": self.OTHER},
                                f"NotOperator; its operator is {d.to_checksum_address(self.OPERATOR)}"),
                               (["--amount", "0.1", "--key", "deployer"], {}, "is 0.1 HYPE already"),
                               (["--amount", "0", "--key", "deployer"], {}, "the factory refuses a fee of none"),
                               (["--amount", "-1", "--key", "deployer"], {}, "above 0")):
            code, sent, _, key, confirm = self.run_fee(argv + ["--send"], **kw)
            self.assertIsInstance(code, SystemExit, argv)
            self.assertIn(said, str(code))
            self.assertEqual(sent, [])
            confirm.assert_not_called()
        code, *_ = self.run_fee(["--amount", "0.5"])
        self.assertIn("--key or --signer", str(code))

    def test_on_mainnet_a_fee_above_10_hype_wants_its_confirmation_and_testnet_none(self):
        safe = d.to_checksum_address(self.OPERATOR)
        code, sent, said, key, confirm = self.run_fee(["--amount", "20", "--signer", safe], now=10**18, net="mainnet")
        self.assertIsInstance(code, SystemExit)
        self.assertIn("--amount is 20 HYPE, more than 10 HYPE on mainnet: add --confirm-large-fee", str(code))
        self.assertEqual(said, "", "refused before anything is read or printed")
        code, sent, said, *_ = self.run_fee(["--amount", "20", "--signer", safe, "--confirm-large-fee"], now=10**18,
                                            net="mainnet")
        self.assertEqual(code, 0)
        self.assertIn("makes it 20 HYPE (20000000000000000000 wei)", said)
        code, sent, said, *_ = self.run_fee(["--amount", "20", "--key", "deployer"])
        self.assertEqual(code, 0, "testnet asks no confirmation")

    def test_for_a_safe_the_call_is_printed_and_nothing_is_sent(self):
        safe = d.to_checksum_address(self.OPERATOR)
        code, sent, said, key, confirm = self.run_fee(["--amount", "2", "--signer", safe, "--send"], now=10**18)
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        key.assert_not_called(), confirm.assert_not_called()
        self.assertIn(f"from the signer ({safe})", said)
        tx = json.loads(said[said.index("{"):])["transactions"]
        self.assertEqual(len(tx), 1)
        self.assertEqual((tx[0]["to"], tx[0]["value"]), (d.to_checksum_address(self.FACTORY), "0"))
        self.assertEqual(tx[0]["data"], "0x" + d.c.encode_call("setCreationFee(uint256)", ["uint256"], [2 * 10**18]).hex())
        # A Safe that is not the operator is refused before anything is printed.
        code, *_ = self.run_fee(["--amount", "2", "--signer", d.to_checksum_address(self.OTHER)], now=10**18)
        self.assertIn("NotOperator", str(code))

    def test_the_call_is_the_factorys(self):
        self.assertIn("function setCreationFee(uint256 fee) external onlyOperator", FACTORY)
        self.assertIn("event CreationFeeSet(uint256 previous, uint256 fee);", FACTORY)


class TheLibraries(unittest.TestCase):
    """SimPool links to three libraries since its code outgrew a block's limit; `deploy` puts them first and names
    them, and the pool's built code asks for exactly these three."""

    def test_deploy_links_the_pool_to_both_libraries(self):
        src = (ROOT / "ops/deploy_sim.py").read_text(encoding="utf-8")
        stops = src.index('deploy_contract(op, out, record, "SimPoolStops", "SimPoolStops", [], [])')
        setup = src.index('deploy_contract(op, out, record, "SimPoolSetup", "SimPoolSetup", [], [])')
        codes = src.index('deploy_contract(op, out, record, "SimPoolCodes", "SimPoolCodes", [], [])')
        pool = src.index('{"SimPoolStops": stops, "SimPoolSetup": setup, "SimPoolCodes": codes_lib})')
        self.assertLess(max(stops, setup, codes), pool, "the libraries before the pool that links to them")

    def test_the_pools_code_links_to_these_three_and_no_other(self):
        art = ROOT / "out/SimPool.sol/SimPool.json"
        if not art.exists():
            self.skipTest("no forge build here")
        refs = json.loads(art.read_text())["bytecode"].get("linkReferences") or {}
        names = sorted(name for libs in refs.values() for name in libs)
        self.assertEqual(names, ["SimPoolCodes", "SimPoolSetup", "SimPoolStops"])


class TheFirstPool(unittest.TestCase):
    # The CFO's prices of 16 November (25 000 and up: its section on large accounts): for each size, the price
    # on the tiers 3 / 9, 4 / 10, 5 / 10 and 6 / 10.
    SIZES = (5_000, 10_000, 25_000, 50_000, 100_000, 250_000)
    TIERS = ((300, 900), (400, 1000), (500, 1000), (600, 1000))
    PRICES = {5_000: (20, 30, 40, 50), 10_000: (40, 60, 79, 95), 25_000: (100, 150, 198, 238),
              50_000: (200, 300, 395, 475), 100_000: (400, 600, 790, 950), 250_000: (1_000, 1_500, 1_975, 2_375)}

    def test_it_is_the_cfos_menu_of_24_lines_and_locks_4000(self):
        want = [(size, self.PRICES[size][k]) for size in self.SIZES for k in range(4)]
        self.assertEqual([(m["size_usdc"], m["price_usdc"]) for m in FIRST["menu"]], want)
        self.assertEqual(d.locked_of(FIRST), 4_000 * 10**6, "the creator's lock of 16 November")
        self.assertEqual(d.on_of(FIRST), 0xfff, "5 000, 10 000 and 25 000 on; 50 000 and up off")
        self.assertEqual([m["on"] for m in FIRST["menu"]], [m["size_usdc"] <= 25_000 for m in FIRST["menu"]])
        # The same menu the contract tests open a pool on and measure the gas of (`_firstPlan`).
        sol = (ROOT / "test/sim/SimPool.t.sol").read_text()
        body = sol[sol.index("function _firstPlan()"):]
        body = body[:body.index("\n    }\n")]
        self.assertIn("p.locked = 4_000e6;", body)
        self.assertIn("p.on = 0xfff;", body)
        self.assertIn("uint16[4] memory dd = [uint16(300), 400, 500, 600];", body)
        self.assertIn("uint16[4] memory target = [uint16(900), 1000, 1000, 1000];", body)
        self.assertIn("uint64[12] memory price = [uint64(20e6), 30e6, 40e6, 50e6, 40e6, 60e6, 79e6, 95e6, 100e6, 150e6, "
                      "198e6, 238e6];", body)

    def test_its_tuple(self):
        h = keccak(text=FIRST["terms_text"])
        plan = d.plan_of(FIRST, h)
        self.assertEqual(plan[0], [(size * 10**6, self.PRICES[size][k] * 10**6) for size in self.SIZES for k in range(4)])
        self.assertEqual(plan[1], (300, 600, 1000, [3, 4, 0], [1000, 1000, 500]))
        self.assertEqual(plan[2], (0, [1000], 2592000, 2592000, 0, 450, 4, 3))
        self.assertEqual(plan[4], plan[2], "the stage has the exam's terms")
        self.assertEqual(plan[5:9], (604800, True, 150, h))
        # Each line its tier, trailing to the start, and two payouts a day.
        self.assertEqual(plan[9:11], ([(dd, t, 1) for _ in self.SIZES for dd, t in self.TIERS], 2))
        self.assertEqual(plan[11:], (4_000 * 10**6, 0xfff))
        # The payout is 8 % of the size: what the terms say each line needs free.
        self.assertEqual(d.PAYOUT_BPS, 800)

    def test_its_terms_name_the_24_lines_the_large_accounts_and_the_four_reasons(self):
        text = FIRST["terms_text"]
        for k, (dd, t) in enumerate(self.TIERS):
            line = f"{dd // 100} % and {t // 100} %: " + ", ".join(
                f"{size:,} for {self.PRICES[size][k]:,} USDC".replace(",", " ") for size in self.SIZES)
            self.assertIn(line, text)
        self.assertIn("The pool opens with the lines of 5 000, 10 000 and 25 000 switched on; the lines of 50 000, "
                      "100 000 and 250 000 are there and switched off.", text)
        self.assertIn("Only the pool's creator switches a line on, and only when the pool holds free money for two "
                      "payouts of that line: 8 000 for 50 000, 16 000 for 100 000, 40 000 for 250 000. Until the pool "
                      "holds that money, the line cannot be switched on.", text)
        for size in (50_000, 100_000, 250_000):
            self.assertIn(f"{2 * size * d.PAYOUT_BPS // 10_000:,} for {size:,}".replace(",", " "), text)
        self.assertIn("and the payouts of every trader who has passed an exam or runs a stage", text)
        self.assertIn("the payout of a trader who has passed the exam stays in the pool from the pass, and for as long "
                      "as the stage runs", text)
        self.assertIn("before then its creator can neither close it nor take back what it put in", text)
        self.assertIn("is paid a fixed 8 % of the size, once, which is not a share of any profit", text)
        self.assertIn("nothing here promises a profit", text)
        self.assertIn("may stop it, once, with one of four reasons written on chain", text)
        # The CFO's words for reason 4 (14 November): a person's decision on a written analysis, with the 14 days.
        self.assertIn("4, prohibited play: a series of exams bought to trade each at the edge of its risk, or one "
                      "account hedged by another, shown by transactions and a published analysis, with 14 days for "
                      "the trader to object.", text)
        self.assertNotIn("testnet", json.dumps(FIRST).lower(), "one plan for both networks")
        self.assertLessEqual(len(text.encode("utf-8")), d.TERMS_MAX_BYTES)

    def test_a_line_names_its_own_tier_and_a_plan_its_payouts_a_day(self):
        raw = json.loads(json.dumps(FIRST))
        # Two lines, neither with a tier of its own, no lock and no switches: the plan as every pool had it before.
        raw["menu"] = [{"size_usdc": 5000, "price_usdc": 45}, {"size_usdc": 10000, "price_usdc": 85}]
        del raw["locked_usdc"]
        self.assertEqual(d.plan_of(raw, b"\0" * 32)[9:], ([(600, 1000, 0), (600, 1000, 0)], 2, 1_600 * 10**6, 0b11))
        raw["menu"][0].update(max_drawdown_bps=300, target_bps=900, drawdown_kind=1)
        raw["payouts_per_day"] = 3
        plan = d.plan_of(raw, b"\0" * 32)
        self.assertEqual(plan[9:11], ([(300, 900, 1), (600, 1000, 0)], 3))
        raw["funded"]["targets_bps"] = [800]
        with self.assertRaises(SystemExit):
            d.plan_of(raw, b"\0" * 32)  # line 1 has no tier, and the stages' targets differ

    def test_its_codes_wait_for_the_cfo_and_alex_and_its_terms_say_how_codes_share(self):
        self.assertEqual(FIRST["codes"], [], "an empty list: the codes and their shares come before the pool sells")
        self.assertNotIn("partner", FIRST)
        for name in ("sim-first-pool", "sim-measure-pool"):
            text = json.loads((ROOT / f"deployments/plans/{name}.json").read_text(encoding="utf-8"))["terms_text"]
            self.assertIn("Every exam is bought with a trading group's code", text)
            self.assertIn("no sooner than 72 hours after the payout is claimed, and then once the pool's approver has "
                          "approved it, or 7 days after it is claimed without approval", text)
            self.assertIn("The pool's creator may change its approver at any time; whoever approves, no payout waits for "
                          "approval longer than 7 days after it is claimed", text)
            self.assertIn("Until it is paid, the platform's checker may stop it", text)
            self.assertIn("Once a trader has used a code, its owner, its author and their split never change and its "
                          "share can be raised, never lowered", text)
            self.assertIn("a pool has as many codes as its creator sets", text)
            # A withdrawal credits the codes; their money waits in the pool until anyone sends it on (16 November).
            self.assertIn("What a code is credited stays in the pool, out of the creator's reach and out of payouts, "
                          "until anyone sends it on, at any time: to the code's owner, and the author's part to the "
                          "author.", text)
            self.assertNotIn("each code's owner is sent its share", text)
            self.assertIn("in the part of every price the pool took that its code's sales brought", text)
            self.assertIn("The pool runs at least 6 months (184 days) from its opening", text)
            self.assertIn("the platform may cancel a withdrawal request; it never moves the money", text)
            if name == "sim-measure-pool":
                self.assertIn("a funded trader's payout stays in the pool for as long as the stage runs; the creator "
                              "takes profit only above it", text)
            self.assertIn("A share credited to a code is never taken back", text)
            # What `_profit` and `_withdraw` do, not more: only the profit a withdrawal takes is shared (review of #286).
            self.assertIn("USDC sent to the pool other than by the creator's addToPot is not counted as put in by the "
                          "creator: it covers payouts as any money of the pool does, and what of it is profit when the "
                          "creator takes money out (above the payouts owed, held and kept, and above what the creator "
                          "put in) is shared with the codes' owners as any profit taken.", text)
            self.assertNotIn("counts as profit and is shared", text)
            # `pay` is a call of its own, walks in the order of the claims, and passes a payout waiting for its
            # approval only with the money for it kept (review of #286).
            self.assertIn("a payout that may be paid is paid by a call anyone may make, which the keeper makes pass "
                          "after pass. Each pool pays at most two payouts a day; any more are paid in order on the "
                          "following days. The two are counted over any 24 hours and count every payout, approved or "
                          "not: a payout whose 72 hours or 7 days have passed still waits for a following day once the "
                          "day's two are paid.", text)
            # The 7 days are when a payout may be paid without approval, not a promise of payment by then: it can
            # also wait for money, or be stopped (review of #293).
            self.assertNotIn("is paid no later than 7 days", text)
            self.assertNotIn("in its next pass", text, "a payout past the day's two waits for a later day")
            self.assertNotIn("ten payouts a call", text)
            self.assertEqual(keeper.PAY_PER_CALL, 2, "the two of the terms")
            self.assertEqual(constant("PAYOUT_DAY"), "24 hours", "the day of the terms")
            self.assertEqual(constant("MAX_PAYOUTS_PER_DAY"), "uint8(MAX_PAYOUTS_A_DAY)")
            self.assertEqual(d.plan_of(json.loads((ROOT / f"deployments/plans/{name}.json").read_text()), b"\0" * 32)[10],
                             2, "the two of the terms: the plan's payouts a day")
            self.assertIn("Payouts are paid in the order they were claimed, out of the money the pool holds: one still "
                          "waiting for its approval keeps its place, and a later one is paid past it only if the money "
                          "also covers every payout so passed over; one the pool cannot cover waits, and every payout "
                          "after it, until money comes in.", text)
            self.assertNotIn("came due", text)
            self.assertNotIn("if the pool does not hold enough then", text)
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

    def test_the_cfos_fill_numbers_of_13_november(self):
        """An order waits four blocks in both stages of both plans: a trader who sees the price a block ahead of the
        oracle gains nothing from it. The first pool's window is three blocks until the filler's lateness is
        measured; the measuring pool keeps the widest. The terms say both numbers."""
        measure = json.loads((ROOT / "deployments/plans/sim-measure-pool.json").read_text(encoding="utf-8"))
        for plan, window in ((FIRST, 3), (measure, int(constant("MAX_FILL_WINDOW")))):
            for stage in ("exam", "funded"):
                self.assertEqual(plan[stage]["fill_delay"], 4)
                self.assertEqual(plan[stage]["fill_window"], window)
            self.assertIn("An order is filled by the platform's filler no sooner than the fourth block after the one it "
                          "is placed in, at the price of the block that fills it, and lapses at no cost if it is not "
                          f"filled within {window} blocks from then.", plan["terms_text"])
        # The placeholder is the window rule of docs/SIM-POOL.md at a largest lateness of none.
        max_late = 0
        self.assertEqual(FIRST["exam"]["fill_window"], max_late + 1 + max(2, -(-(max_late + 1) // 2)))
        self.assertIn("fill_window: 3 blocks, a placeholder", FIRST["about"])

    def test_a_plan_sets_its_lock_and_its_lines_on(self):
        menu = [{"size_usdc": s, "price_usdc": 1} for s in (5000, 10000, 25000, 50000)]
        old = {"menu": menu}
        self.assertEqual(d.locked_of(old), 8_000 * 10**6, "no locked_usdc: two payouts of the largest, as before")
        self.assertEqual(d.on_of(old), 0b1111, "and every line on")
        plan = {"menu": menu, "locked_usdc": 4000}
        self.assertEqual(d.locked_of(plan), 4_000 * 10**6)
        self.assertEqual(d.on_of(plan), 0b0111, "a line without `on`: on if the lock covers two of its payouts")
        plan["menu"] = [dict(m, on=False) if m["size_usdc"] == 10000 else m for m in menu]
        self.assertEqual(d.on_of(plan), 0b0101)
        plan["menu"] = [dict(m, on=True) for m in menu]
        with self.assertRaises(SystemExit) as caught:
            d.on_of(plan)
        self.assertIn("line 3 (50000) is on", str(caught.exception))
        with self.assertRaises(SystemExit):
            d.locked_of({"menu": menu, "locked_usdc": 799.99})
        h = keccak(text="t")
        two = dict(FIRST, menu=[{"size_usdc": 5000, "price_usdc": 45}, {"size_usdc": 10000, "price_usdc": 85}])
        self.assertEqual(d.plan_of(dict(two, locked_usdc=1600), h)[-2:], (1_600 * 10**6, 0b11))

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
                         [("MEASURE", keccak(text="MEASURE"), d.to_checksum_address("0x" + "ab" * 20), 0, d.ZERO, 0)],
                         "one code, the creator's own, with no share: a sale needs one")


class TheCheck(unittest.TestCase):
    """`check`: the plan against SimPoolFactory.check by eth_call, with this checkout's code put in the call, and the
    gas of `create` against the network's small block."""
    PLAN = str(ROOT / "deployments/plans/sim-first-pool.json")
    LARGEST = "0x" + (250_000 * 10**6).to_bytes(32, "big").hex()

    FEE_SEL = "0x" + keccak(text="creationFee()")[:4].hex()

    def run_check(self, argv, answer=None, record=None, create_needs=1_700_000, limits=(30_000_000, 3_000_000),
                  ignores_gas=False, head=100, fee=10**17):
        calls = []
        self.built = built = []
        answer = self.LARGEST if answer is None else answer

        def rpc(method, params):
            calls.append((method, params))
            if method == "eth_blockNumber":
                return hex(head)
            if method == "eth_getBlockByNumber":
                n = int(params[0], 16)
                if n < 0:
                    raise RuntimeError("eth_getBlockByNumber: invalid block number")
                return {"gasLimit": hex(limits[n % len(limits)])}
            if params[0]["data"] == self.FEE_SEL:
                return "0x%064x" % fee
            if params[0]["data"].startswith("0x" + keccak(text=f"create({d.PLAN_T})")[:4].hex()):
                if ignores_gas or int(params[0]["gas"], 16) >= create_needs:
                    return "0x" + "00" * 32
                raise RuntimeError("eth_call: out of gas")
            if isinstance(answer, Exception):
                raise answer
            return answer
        out = io.StringIO()
        with mock.patch.object(d.c, "rpc", side_effect=rpc), \
                mock.patch.object(d, "runtime_of", side_effect=lambda n, t, v, libs=None: built.append((n, t, v)) or f"0x{n}"), \
                mock.patch.object(d.deployments, "load", return_value=record or {}), \
                mock.patch.object(d, "git_head", return_value="ab" * 20), mock.patch.object(d.c, "account") as key, \
                mock.patch.object(d.c, "send_tx") as sent, mock.patch.object(d.c, "transact") as tx, redirect_stdout(out):
            code = d.main(["check", "--plan", self.PLAN] + argv)
        key.assert_not_called(), sent.assert_not_called(), tx.assert_not_called()
        return code, calls, out.getvalue()

    def test_without_a_deployment_this_checkouts_factory_is_put_in_the_call(self):
        code, calls, said = self.run_check([])
        self.assertEqual(code, 0)
        method, (call, block, state) = calls[0]
        self.assertEqual((method, block), ("eth_call", "latest"))
        self.assertEqual(call["to"], d.CHECK_AT["factory"])
        plan = d.plan_of(FIRST, keccak(text=FIRST["terms_text"]))
        self.assertEqual(call["data"], "0x" + d.c.encode_call(f"check({d.PLAN_T})", [d.PLAN_T], [plan]).hex())
        self.assertEqual(state[d.CHECK_AT["exam"]], {"code": "0xPaperExam"})
        self.assertEqual(state[d.CHECK_AT["pool"]], {"code": "0xSimPool"})
        self.assertEqual(state[d.CHECK_AT["setup"]], {"code": "0xSimPoolSetup"})
        self.assertEqual(state[d.CHECK_AT["stops"]], {"code": "0xSimPoolStops"})
        self.assertEqual(state[d.CHECK_AT["codes"]], {"code": "0xSimPoolCodes"})
        f = state[d.CHECK_AT["factory"]]
        self.assertEqual(f["code"], "0xSimPoolFactory")
        # This checkout's factory, built with testnet's fee as `deploy` builds it there.
        name, types, values = next(b for b in self.built if b[0] == "SimPoolFactory")
        self.assertEqual((types[-1], values[-1], len(values)), ("uint256", 10**17, 5))
        word = lambda a: "0x" + int(a, 16).to_bytes(32, "big").hex()
        self.assertEqual(f["stateDiff"]["0x" + "00" * 31 + "05"], word(d.CHECK_AT["book"]))
        for role, slot in (("filler", 1), ("checker", 2), ("closer", 3), ("fee", 4)):
            self.assertEqual(f["stateDiff"]["0x%064x" % slot], word(d.CHECK_AT[role]), role)
        # The assets as `deploy` lists them: testnet's BTC 3, ETH 4, SOL 0, with ops/leverage.py's caps.
        for name, idx, cap in d.asset_list(["BTC", "ETH", "SOL"]):
            slot = "0x" + keccak(idx.to_bytes(32, "big") + (6).to_bytes(32, "big")).hex()
            self.assertEqual(f["stateDiff"][slot], "0x" + cap.to_bytes(32, "big").hex(), name)
        # The fee is storage: a constructor's storage is not in the code, so the call is given testnet's fee.
        self.assertEqual(f["stateDiff"]["0x%064x" % 9], "0x" + (10**17).to_bytes(32, "big").hex())
        self.assertEqual(len(f["stateDiff"]), 9, "the terms book, the fee, four roles and three assets, nothing else")
        b = state[d.CHECK_AT["book"]]
        text_slot = "0x" + keccak(keccak(text=FIRST["terms_text"]) + (0).to_bytes(32, "big")).hex()
        self.assertEqual(b["stateDiff"], {text_slot: word(d.CHECK_AT["text"])}, "the terms, as `pool` publishes them")
        self.assertIn("the factory accepts the plan; largest size 250000 USDC.", said)
        self.assertIn("250000 for 2375 USDC: loss limit 6 %, target 10 %, trailing to the start", said)
        self.assertIn("locks 4000 USDC", said)

    def test_the_gas_of_create_is_measured_by_eth_call_against_the_small_block(self):
        code, calls, said = self.run_check([], create_needs=1_673_901)
        self.assertEqual(code, 0)
        creates = [p for m, p in calls if m == "eth_call" and p[0]["data"].startswith(
            "0x" + keccak(text=f"create({d.PLAN_T})")[:4].hex())]
        call, block, state = creates[0]
        # The fee is the factory's, read from it in the call it is measured in: the one sent with create.
        read = [p for m, p in calls if m == "eth_call" and p[0]["data"] == self.FEE_SEL]
        self.assertEqual(len(read), 1)
        self.assertEqual(read[0][0]["to"], d.CHECK_AT["factory"])
        self.assertEqual(read[0][2][d.CHECK_AT["factory"]], state[d.CHECK_AT["factory"]], "this checkout's factory")
        self.assertEqual(call["value"], hex(10**17))
        # The fee and every probe at one block, the head read once: a fee changed meanwhile cannot pass as gas.
        self.assertEqual(read[0][1], hex(100))
        self.assertEqual({p[1] for p in creates}, {hex(100)})
        self.assertEqual(call["from"], d.CHECK_AT["creator"])
        self.assertEqual(int(creates[0][0]["gas"], 16), d.GAS_FLOOR, "first: does the node keep to a call's gas")
        self.assertEqual(state[d.c.USDC_ERC20], {"code": d.TOKEN_STUB}, "USDC answered by the stand-in")
        self.assertEqual(state[d.CHECK_AT["creator"]], {"balance": hex(2 * 10**17)})
        self.assertEqual(state[d.CHECK_AT["pool"]], {"code": "0xSimPool"}, "this checkout's pool, as for check")
        # The small block is the least limit of the latest blocks, not the big block's.
        self.assertEqual(sum(1 for m, _ in calls if m == "eth_getBlockByNumber"), d.BLOCKS_READ)
        measured = int(re.search(r"create takes (\d+) gas", said).group(1))
        self.assertTrue(1_673_901 <= measured < 1_673_901 + d.GAS_STEP, measured)
        self.assertIn(f"of 3000000 in a small block of testnet: the pool opens by an ordinary transaction", said)
        self.assertIn("Nothing was sent.", said)

    def test_a_young_chain_is_read_down_to_its_first_block_and_no_further(self):
        code, calls, said = self.run_check([], head=2)
        self.assertEqual(code, 0)
        read = [int(p[0], 16) for m, p in calls if m == "eth_getBlockByNumber"]
        self.assertEqual(read, [2, 1, 0], "three blocks, none below 0")
        self.assertIn("of 3000000 in a small block", said)

    def test_a_create_above_the_small_block_is_red(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_check([], create_needs=3_100_000)
        self.assertIn("create does not run on 3000000 gas", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.run_check([], create_needs=2_950_000)
        self.assertIn(f"and USDC's transfer up to {d.TOKEN_GAS} more, above 3000000", str(caught.exception))
        self.assertIn("would not open by an ordinary transaction", str(caught.exception))

    def test_a_node_that_ignores_the_calls_gas_measures_nothing(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_check([], ignores_gas=True)
        self.assertIn("ignores a call's gas", str(caught.exception))

    def test_with_a_deployment_its_factory_is_asked_as_it_stands(self):
        factory = "0x" + "77" * 20
        code, calls, said = self.run_check(["--deployment", "x"], record={"SimPoolFactory": factory})
        self.assertEqual(code, 0)
        method, params = calls[0]
        self.assertEqual(len(params), 2, "no state override: the factory, its assets and its terms book as they are")
        self.assertEqual(params[0]["to"], factory)
        create = next(p for m, p in calls if m == "eth_call" and p[0]["to"] == factory and len(p) == 3)
        self.assertEqual(set(create[2]), {d.c.USDC_ERC20, d.CHECK_AT["creator"]}, "only the token and the HYPE")
        # The deployed factory's own fee, read from it as it stands, is the one sent: here mainnet's 1 HYPE.
        code, calls, said = self.run_check(["--deployment", "x"], record={"SimPoolFactory": factory}, fee=10**18)
        read = next(p for m, p in calls if m == "eth_call" and p[0]["data"] == self.FEE_SEL)
        self.assertEqual((read[0]["to"], len(read)), (factory, 2), "no state override")
        create = next(p for m, p in calls if m == "eth_call" and p[0]["to"] == factory and len(p) == 3)
        self.assertEqual(create[0]["value"], hex(10**18))

    def test_a_refusal_names_the_factorys_error(self):
        for sig in ("BadPlan()", "Unpublished()"):
            err = RuntimeError("eth_call: {'code': 3, 'message': 'execution reverted', 'data': '0x%s'}"
                               % keccak(text=sig)[:4].hex())
            with self.assertRaises(SystemExit) as caught:
                self.run_check([], answer=err)
            self.assertIn(f"the factory refuses the plan: {sig[:-2]}", str(caught.exception))
        # Revert data in capitals, as some nodes write it.
        err = RuntimeError("eth_call: execution reverted, data 0x%s" % keccak(text="BadPlan()")[:4].hex().upper())
        with self.assertRaises(SystemExit) as caught:
            self.run_check([], answer=err)
        self.assertIn("the factory refuses the plan: BadPlan", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.run_check([], answer=RuntimeError("eth_call: node down"))
        self.assertIn("node down", str(caught.exception))

    def test_no_answer_is_no_factory(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_check(["--deployment", "x"], answer="0x", record={"SimPoolFactory": "0x" + "77" * 20})
        self.assertIn("gave no answer on testnet: no factory there", str(caught.exception))

    def test_an_answer_other_than_the_largest_size_is_no_pass(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_check([], answer="0x" + (5_000 * 10**6).to_bytes(32, "big").hex())
        self.assertIn("not the plan's largest size", str(caught.exception))

    def test_terms_the_book_would_refuse_are_refused_first(self):
        for text in ("", "x" * (d.TERMS_MAX_BYTES + 1)):
            with tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "plan.json"
                path.write_text(json.dumps(dict(FIRST, terms_text=text)))
                with self.assertRaises(SystemExit) as caught, mock.patch.object(d.c, "rpc") as rpc:
                    d.main(["check", "--plan", str(path)])
            rpc.assert_not_called()
            self.assertIn(f"TermsBook.publish takes 1 to {d.TERMS_MAX_BYTES}", str(caught.exception))

    def test_the_slots_are_the_ones_the_contract_test_holds(self):
        sol = (ROOT / "test/sim/SimPool.t.sol").read_text()
        for name in ("FACTORY_TERMS_BOOK_SLOT", "FACTORY_ASSET_CAP_SLOT", "BOOK_TEXT_AT_SLOT"):
            self.assertEqual(int(re.search(rf"constant {name} = (\d+);", sol).group(1)), getattr(d, name), name)
        self.assertEqual(int(re.search(r"constant FACTORY_CREATION_FEE_SLOT = (\d+);", sol).group(1)),
                         d.FACTORY_CREATION_FEE_SLOT)
        for role in ("FILLER", "CHECKER", "CLOSER", "FEE"):
            self.assertEqual(int(re.search(rf"constant FACTORY_{role}_SLOT = (\d+);", sol).group(1)),
                             d.FACTORY_ROLE_SLOTS[role.lower()], role)
        self.assertEqual(d.map_slot(3, 6), "0x" + keccak((3).to_bytes(32, "big") + (6).to_bytes(32, "big")).hex())
        self.assertEqual(d.SMALL_BLOCK_GAS, int(re.search(r"constant SMALL_BLOCK_GAS = ([\d_]+);", sol).group(1)
                                                 .replace("_", "")))
        self.assertEqual(int(re.search(r"MAX_LENGTH = ([\d_]+);", (ROOT / "src/lib/CodeStore.sol").read_text())
                             .group(1).replace("_", "")), d.TERMS_MAX_BYTES)

    def test_the_check_addresses_are_no_ones_and_distinct(self):
        self.assertEqual(len(set(d.CHECK_AT.values())), len(d.CHECK_AT))
        self.assertTrue(all(int(a, 16) < 2**16 for a in d.CHECK_AT.values()), "not a key's address")


class TheCodes(unittest.TestCase):
    CREATOR = "0x" + "ab" * 20

    def test_a_plans_codes_are_checked_as_the_pool_checks_them(self):
        ok = {"codes": [{"code": "GROUP-ONE", "partner": "0x" + "11" * 20, "bps": 5000},
                        {"code": "PLATFORM", "partner": "creator", "bps": 0}]}
        got = d.codes_of(ok, self.CREATOR)
        self.assertEqual([(t, h, b) for t, h, _, b, *_ in got], [("GROUP-ONE", keccak(text="GROUP-ONE"), 5000),
                                                                 ("PLATFORM", keccak(text="PLATFORM"), 0)])
        self.assertEqual(got[1][2], d.to_checksum_address(self.CREATOR))
        self.assertEqual([g[4:] for g in got], [(d.ZERO, 0), (d.ZERO, 0)], "codes for one")
        two = d.codes_of({"codes": [{"code": "BLOGGER-1", "partner": "0x" + "11" * 20, "bps": 6000,
                                     "author": "0x" + "33" * 20, "author_bps": 5000}]}, self.CREATOR)
        self.assertEqual(two[0][3:], (6000, d.to_checksum_address("0x" + "33" * 20), 5000), "a code for two")
        # As many codes as the creator sets: a network of a thousand authors, a code each.
        many = [{"code": f"A{i}", "partner": "0x" + "11" * 20, "bps": 6000, "author": "0x" + "33" * 20,
                 "author_bps": 5000} for i in range(1_000)]
        self.assertEqual(len(d.codes_of({"codes": many}, self.CREATOR)), 1_000)
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
               [{"code": "A", "partner": "0x" + "11" * 20, "bps": 6000, "author": "0x" + "33" * 20}],
               [{"code": "A", "partner": "0x" + "11" * 20, "bps": 6000, "author": "0x" + "00" * 20, "author_bps": 5000}],
               [{"code": "A", "partner": "0x" + "11" * 20, "bps": 6000, "author": "0x" + "33" * 20, "author_bps": 0}],
               [{"code": "A", "partner": "0x" + "11" * 20, "bps": 6000, "author": "0x" + "33" * 20, "author_bps": 10_000}]]
        for codes in bad:
            with self.assertRaises(SystemExit):
                d.codes_of({"codes": codes}, self.CREATOR)
        self.assertEqual(d.codes_of({}, self.CREATOR), [])

    def test_a_code_a_sale_named_is_skipped_if_it_matches_and_stops_everything_if_not(self):
        p1, p2 = d.to_checksum_address("0x" + "11" * 20), d.to_checksum_address("0x" + "22" * 20)
        z = d.ZERO
        codes = [("A", b"a" * 32, p1, 5000, z, 0), ("B", b"b" * 32, p2, 4000, z, 0), ("C", b"c" * 32, p1, 0, z, 0)]
        chain = {b"a" * 32: (p1, 5000, True, 0, z), b"b" * 32: (p1, 4000, False, 0, z), b"c" * 32: (z, 0, False, 0, z)}
        self.assertEqual([t for t, *_ in d.to_set(codes, chain.__getitem__)], ["B", "C"], "A is there already")
        chain[b"a" * 32] = (p1, 6000, True, 0, z)
        with self.assertRaises(SystemExit) as caught:
            d.to_set(codes, chain.__getitem__)
        self.assertIn("its share can only rise, for the same partner, author and split", str(caught.exception))
        chain[b"a" * 32] = (p1, 4000, True, 0, z)
        self.assertEqual([t for t, *_ in d.to_set(codes, chain.__getitem__)], ["A", "B", "C"], "A raised from 40 to 50 %")
        chain[b"a" * 32] = (p2, 4000, True, 0, z)
        with self.assertRaises(SystemExit):
            d.to_set(codes, chain.__getitem__)  # another partner, even with a higher share
        # A code for two: its author and split stay as a sale fixed them.
        p3 = d.to_checksum_address("0x" + "33" * 20)
        two = [("T", b"t" * 32, p1, 6000, p3, 5000)]
        self.assertEqual(d.to_set(two, {b"t" * 32: (p1, 6000, True, 5000, p3)}.__getitem__), [], "as on chain")
        self.assertEqual(len(d.to_set(two, {b"t" * 32: (p1, 5000, True, 5000, p3)}.__getitem__)), 1, "a raise")
        for fixed in [(p1, 4000, True, 4000, p3), (p1, 4000, True, 5000, p2), (p1, 4000, True, 0, z)]:
            with self.assertRaises(SystemExit):
                d.to_set(two, {b"t" * 32: fixed}.__getitem__)


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

    def test_an_approval_and_a_new_approver_from_a_signer_elsewhere_say_the_signer_not_none(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        ledger = d.to_checksum_address(self.CLOSER)
        safe = d.to_checksum_address(self.CREATOR)
        waiting = ("0x" + "11" * 20, "0x" + "77" * 20, 800_000_000, 1_000, 1, False, 0, False)
        answers = {"payoutCount()": [5], "payout(uint256)": [waiting], "factory()": ["0x" + "99" * 20],
                   "PAYOUT_HARD()": [7 * self.DAY], "PAYOUT_DELAY()": [3 * self.DAY], "approver()": [ledger],
                   "creator()": [safe]}
        for argv, who in ((["approve", "--pool", self.OTHER, "--signer", ledger, "--ids", "3"], ledger),
                          (["approver", "--pool", self.OTHER, "--signer", safe, "--address", self.CLOSER], safe)):
            out = io.StringIO()
            with mock.patch.object(d.c, "call_view", side_effect=lambda to, sig, *a: answers[sig]), \
                    mock.patch.object(d.c, "account") as key, mock.patch.object(d.c, "transact") as sent, \
                    redirect_stdout(out):
                self.assertEqual(d.main(argv), 0)
            sent.assert_not_called()
            key.assert_not_called()
            self.assertIn(f"from the signer ({who})", out.getvalue())
            self.assertNotIn("None", out.getvalue())

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
            # No key was named: the line says the signer and its address, not "from None" (review of #291).
            self.assertIn(f"from the signer ({safe})", said)
            self.assertNotIn("None", said)
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
                mock.patch.object(d.c, "rpc", return_value="0x%064x" % 10**17) as rpc, \
                mock.patch.object(d.c, "account") as key, mock.patch.object(d.c, "send_tx") as sent, redirect_stdout(out):
            self.assertEqual(d.main(["pool", "--deployment", "x", "--plan", "deployments/plans/sim-first-pool.json",
                                     "--signer", safe, "--send"]), 0)
        key.assert_not_called()
        sent.assert_not_called()
        self.assertEqual(rpc.call_args.args[1][0], {"to": record["SimPoolFactory"],
                                                    "data": "0x" + keccak(text="creationFee()")[:4].hex()})
        said = out.getvalue()
        self.assertIn("pays 0.1 HYPE (the factory's fee)", said)
        txs = json.loads(said[said.index("{"):])["transactions"]
        self.assertEqual([t["to"].lower() for t in txs], [record["TermsBook"], record["usdc"], record["SimPoolFactory"]])
        self.assertEqual([t["value"] for t in txs], ["0", "0", str(10**17)], "the factory's fee with create")
        self.assertTrue(txs[2]["data"].startswith("0x" + keccak(text=f"create({d.PLAN_T})")[:4].hex()))
        # Its codes: one setCode each still to set, read from the pool as the Safe.
        p1 = d.to_checksum_address("0x" + "11" * 20)
        plan = {"codes": [{"code": "A", "partner": p1, "bps": 5000}, {"code": "B", "partner": "creator", "bps": 0}]}
        out = io.StringIO()
        with mock.patch.object(d.pathlib.Path, "read_text", return_value=json.dumps(plan)), \
                mock.patch.object(d.c, "call_view", return_value=(d.ZERO, 0, False, 0, d.ZERO)), \
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

    def test_the_signed_nonce_is_the_code_the_line_and_the_salt(self):
        """The trader signs keccak256(abi.encode(code, line, salt)): the relayer can send it with neither another code
        nor another line, two lines of a menu may share a price."""
        from eth_abi import encode
        p = r.parsed(GOOD)
        self.assertEqual(p["nonce"], keccak(encode(["bytes32", "uint256", "bytes32"], [b"\x66" * 32, 1, b"\x33" * 32])))
        self.assertNotEqual(r.parsed({**GOOD, "code": "0x" + "77" * 32})["nonce"], p["nonce"])
        self.assertNotEqual(r.parsed({**GOOD, "line": 0})["nonce"], p["nonce"])
        for line in (-1, 2**256):
            with self.assertRaises(r.Refused):
                r.parsed({**GOOD, "line": line})  # malformed, a 400: not an encoder's error, a 502 (review of #302)

    def test_the_call_takes_the_signed_fields_in_order(self):
        p = r.parsed(GOOD)
        self.assertEqual(r.args_of(p), [p["trader"], 1, bytes.fromhex("66" * 32), bytes.fromhex("33" * 32), 0, 2_000, 27,
                                        bytes.fromhex("44" * 32), bytes.fromhex("55" * 32)])
        self.assertEqual(len(r.TYPES), len(r.args_of(p)))


if __name__ == "__main__":
    unittest.main()
