import io
import json
import pathlib
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ops import deploy_v3 as d

ROOT = pathlib.Path(__file__).resolve().parents[2]
FAUCET = "0x00000000000000000000000000000000000000Fa"
SAFE = "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB"
PUBLISHER = "0x00000000000000000000000000000000000000AB"
GUARDIAN = "0x00000000000000000000000000000000000000cD"
PCR = "0x" + "aa" * 32
DOC = "0x" + "bb" * 32
BINDER_ARGS = ("--publisher", PUBLISHER, "--pcr-hash", PCR, "--attestation-hash", DOC, "--guardian", GUARDIAN)


class DeployV3(unittest.TestCase):
    """Factory v3 in one command (the pools team): on testnet the demo's token first, then the
    registry, the implementations, the factory on that token, the paper exam and the reader, then
    the settings and the keys; on mainnet a plan and nothing sent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "deployments").mkdir()
        self.deployed: list[tuple[str, list]] = []
        self.linked: dict = {}
        self.sent: list[str] = []
        self.calls: list[tuple[str, str, list]] = []
        self.deployer = mock.Mock(address="0x00000000000000000000000000000000000000De")

        def deploy(acct, contract, types, values, libraries=None):
            addr = "0x" + f"{len(self.deployed) + 1:040x}"
            self.deployed.append((contract, list(values)))
            self.linked[contract] = libraries
            return d.to_checksum_address(addr), {"transactionHash": "0x01", "blockNumber": "0x10"}

        def transact(acct, to, sig, types, values):
            self.sent.append(sig.split("(")[0])
            self.calls.append((to, sig.split("(")[0], list(values)))
            return {"transactionHash": "0x02"}

        patches = [
            mock.patch.object(d, "ROOT", self.root),
            mock.patch.object(d, "git_head", return_value="ab" * 20),
            mock.patch.object(d.subprocess, "run"),
            mock.patch.object(d, "initcode_keccak", side_effect=lambda name: "0x" + "11" * 32),
            mock.patch.object(d, "shares_code", return_value=bytes.fromhex("22" * 32)),
            mock.patch.object(d, "check_assets", return_value=[3, 4, 0]),
            mock.patch.object(d, "big_blocks"),
            mock.patch.object(d.c, "assert_testnet"),
            mock.patch.object(d.c, "account", return_value=self.deployer),
            mock.patch.object(d.c, "core_user_exists", return_value=True),
            mock.patch.object(d.c, "deploy", side_effect=deploy),
            mock.patch.object(d.c, "transact", side_effect=transact),
            mock.patch.object(d.c, "rpc", return_value="0x6001"),
            mock.patch.object(d.c, "record"),
            # The mainnet plan's reads, from public endpoints: never the network in a test.
            mock.patch.object(d, "read_rpc", side_effect=lambda url, method, params:
                              {"eth_getTransactionCount": "0x5", "eth_getBalance": hex(2 * 10**18)}[method]),
            mock.patch.object(d, "perp_numbers", return_value={"BTC": 0, "ETH": 1, "SOL": 5}),
            mock.patch.object(d, "gas_estimate", return_value={
                "read_at_utc": "2026-11-09T21:00:00Z",
                "deployment": {"gas_total": 27_000_000, "transactions": 16, "hype": 0.27, "usd": 9.5,
                               "need_big_block": ["PoolImpl"]},
                "deployment_at_ten_times_the_price": {"hype": 2.7}}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, *argv) -> str:
        out = io.StringIO()
        with mock.patch.object(d.sys, "argv", ["deploy_v3", *argv]), redirect_stdout(out):
            self.assertEqual(d.main(), 0)
        return out.getvalue()

    def test_testnet_deploys_the_demo_token_first_and_the_factory_on_it(self):
        self.run_main("--label", "demo3", "--faucet", FAUCET, "--faucet-daily", "500", *BINDER_ARGS)
        self.assertEqual([name for name, _ in self.deployed],
                         ["DemoUsdc", "KeyBinder", "AccountOps", "Pool", "ChallengeAccount", "PoolFactory",
                          "PaperExam", "MultiPoolReader"])
        binder = d.to_checksum_address("0x" + f"{2:040x}")
        self.assertEqual(dict(self.deployed)["KeyBinder"], [self.deployer.address, 50, 86_400],
                         "the deployer owns it while it is configured; testnet's fuse and the 24-hour delay")
        library = {"AccountOps": d.to_checksum_address("0x" + f"{3:040x}")}
        self.assertEqual((self.linked["Pool"], self.linked["ChallengeAccount"]), (library, library))
        token = d.to_checksum_address("0x" + f"{1:040x}")
        factory_args = dict(self.deployed)["PoolFactory"]
        self.assertEqual(factory_args[-1], token, "the factory on the demo's token")
        self.assertEqual(factory_args[3], self.deployer.address, "the deployer operates the factory")
        self.assertEqual(factory_args[0], binder, "the factory on the binder")
        self.assertEqual(self.sent, ["setAccountSource", "setPublisher", "setGuardian", "setPlatformAssets",
                                     "setFundedAssets", "setExamImpl", "setSharesCode", "setChallengeFee", "setFaucet"])
        calls = {fn: (to, args) for to, fn, args in self.calls}
        self.assertEqual(calls["setAccountSource"], (binder, [factory_args and d.to_checksum_address("0x" + f"{6:040x}")]))
        self.assertEqual(calls["setPublisher"], (binder, [PUBLISHER, bytes.fromhex("aa" * 32), bytes.fromhex("bb" * 32)]))
        self.assertEqual(calls["setGuardian"], (binder, [d.to_checksum_address(GUARDIAN)]))
        record = json.loads((self.root / "deployments" / "testnet-v3-demo3.json").read_text())
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["usdc"], token)
        self.assertEqual(record["faucet"], {"address": d.to_checksum_address(FAUCET), "daily": 500 * 10**6})
        self.assertEqual(set(record["codehash"]), {k for k, _ in d.CONTRACTS})
        self.assertEqual(set(record["initcode_keccak"]), {k for k, _ in d.CONTRACTS})
        self.assertEqual(record["chain_id"], 998)
        self.assertEqual(record["core_writer"], d.c.CORE_WRITER)
        self.assertNotIn("published_keys", record)
        self.assertEqual(record["KeyBinder"], binder)
        self.assertEqual(record["key_binder"]["publisher"], {"address": PUBLISHER, "pcr_hash": PCR,
                                                             "attestation_doc_hash": DOC, "test_key": False})
        self.assertEqual(record["key_binder"]["guardian"], d.to_checksum_address(GUARDIAN))
        self.assertEqual(record["key_binder"]["owner"], {"from": self.deployer.address, "to": None, "state": "the deployer"})
        self.assertFalse((self.root / "deployments" / "registries.json").exists(), "a binder holds no list of keys")

    def test_a_dry_run_checks_everything_and_sends_nothing(self):
        out = self.run_main("--label", "demo3", "--dry-run", *BINDER_ARGS)
        self.assertEqual(self.deployed, [])
        self.assertEqual(self.sent, [])
        self.assertIn("nothing was sent", out)
        self.assertIn("DemoUsdc", out)
        self.assertIn("KeyBinder(deployer, 50 a day, renewal delay 86400 s)", out)
        self.assertIn(f"publisher        {PUBLISHER}", out)
        self.assertIn("safe             the deployer keeps the key binder and the factory", out)
        self.assertFalse((self.root / "deployments" / "testnet-v3-demo3.json").exists())

    def test_mainnet_is_refused_without_a_dry_run_before_anything_is_read(self):
        with mock.patch.object(d.sys, "argv", ["deploy_v3", "--label", "v3", "--network", "mainnet"]):
            with self.assertRaises(SystemExit) as caught:
                d.main()
        self.assertIn("dry run only", str(caught.exception))
        d.git_head.assert_not_called()
        d.c.assert_testnet.assert_not_called()

    def test_mainnet_dry_run_names_the_chains_usdc_and_no_demo_token(self):
        out = self.run_main("--label", "v3", "--network", "mainnet", "--dry-run")
        self.assertIn("0xb88339CB7199b77E23DB6E890353E22632Ba630f", out)
        self.assertIn("999", out)
        self.assertNotIn("DemoUsdc,", out)
        self.assertEqual(self.deployed, [])
        d.c.assert_testnet.assert_not_called()
        self.assertEqual(d.plan("mainnet", d.NETWORKS["mainnet"]["usdc"])[0], "KeyBinder")
        self.assertIn("a daily fuse the owner sets", out)

    def test_the_networks_are_network_pys(self):
        from spike.hlspike import network
        for name, net in network.NETWORKS.items():
            self.assertEqual((d.NETWORKS[name]["chain_id"], d.NETWORKS[name]["rpc"], d.NETWORKS[name]["api"]),
                             (net.chain_id, net.rpc_url, net.api_url))
        self.assertEqual(d.NETWORKS["mainnet"]["usdc"], network.MAINNET.usdc_erc20)
        self.assertIsNone(d.NETWORKS["testnet"]["usdc"], "testnet prices in the demo's own token, deployed first")

    def test_mainnet_plan_names_every_address_the_perps_the_gas_and_the_record_and_sends_nothing(self):
        deployer = "0x00000000000000000000000000000000000000De"
        out = self.run_main("--label", "v3", "--network", "mainnet", "--dry-run", "--deployer", deployer,
                            "--max-binds-per-day", "200", "--safe", SAFE, *BINDER_ARGS)
        deploys = d.plan("mainnet", d.NETWORKS["mainnet"]["usdc"])
        for i, key in enumerate(deploys):
            self.assertIn(f"{key} at {d.created_by(d.to_checksum_address(deployer), 5 + i)}", out)
        self.assertIn("nonce 5, 2.0000 HYPE", out)
        self.assertIn("rpc https://rpc.hyperliquid.xyz/evm", out)
        self.assertIn("BTC 0, ETH 1, SOL 5", out)
        self.assertIn("27,000,000 over 16 transactions", out)
        self.assertIn("measured on demo3's receipts, a deployment with a key registry", out)
        self.assertIn("KeyBinder(deployer, 200 a day, renewal delay 86400 s)", out)
        self.assertNotIn("publish ", out)
        self.assertIn("would write      deployments/mainnet-v3-v3.json", out)
        self.assertEqual((self.deployed, self.sent), ([], []))
        d.c.rpc.assert_not_called()
        d.c.account.assert_not_called()
        self.assertFalse((self.root / "deployments" / "mainnet-v3-v3.json").exists())

    def test_a_mainnet_plan_says_what_it_could_not_read_and_still_sends_nothing(self):
        with mock.patch.object(d, "perp_numbers", side_effect=OSError("no route")), \
                mock.patch.object(d, "gas_estimate", side_effect=RuntimeError("rate limited")):
            out = self.run_main("--label", "v3", "--network", "mainnet", "--dry-run")
        self.assertIn("perp numbers     not read: no route", out)
        self.assertIn("gas              not read: rate limited", out)
        self.assertIn("pass --deployer", out)
        self.assertEqual((self.deployed, self.sent), ([], []))

    def test_a_nonce_that_cannot_be_read_is_said_and_the_plan_goes_on(self):
        with mock.patch.object(d, "read_rpc", side_effect=OSError("rate limited")):
            out = self.run_main("--label", "v3", "--network", "mainnet", "--dry-run", "--deployer", "0x" + "de" * 20)
        self.assertIn("nonce not read (rate limited)", out)
        self.assertIn("(addresses not read)", out)
        self.assertIn("would write      deployments/mainnet-v3-v3.json", out)

    def test_a_delisted_perp_keeps_its_place_and_has_no_number(self):
        from ops import plan_reads
        meta = {"universe": [{"name": "BTC"}, {"name": "ETH", "isDelisted": True}, {"name": "SOL"}]}
        with mock.patch.object(plan_reads, "read_info", return_value=meta) as info:
            self.assertEqual(plan_reads.perp_numbers("https://api.example/", ["BTC", "ETH", "SOL"]), {"BTC": 0, "SOL": 2})
        info.assert_called_once()

    def test_the_address_of_a_contract_is_its_deployers_and_nonces(self):
        # The first contract of 0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0, a known one: nonce 0.
        self.assertEqual(d.created_by("0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0", 0),
                         d.to_checksum_address("0xcd234a471b72ba2f1ccf0a70fcaba648a5eecd8d"))
        self.assertEqual(d.created_by("0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0", 1),
                         d.to_checksum_address("0x343c43a37d37dff08ae8c4a11544c718abb4fcf8"))

    def test_with_owner_the_binder_and_the_factory_are_handed_over_last_and_the_record_says_pending(self):
        self.run_main("--label", "demo3", "--safe", SAFE, *BINDER_ARGS)
        self.assertEqual(self.sent[-2:], ["transferOwnership", "transferOperator"])
        binder, factory = d.to_checksum_address("0x" + f"{2:040x}"), d.to_checksum_address("0x" + f"{6:040x}")
        self.assertEqual(self.calls[-2:], [(binder, "transferOwnership", [SAFE]), (factory, "transferOperator", [SAFE])])
        record = json.loads((self.root / "deployments" / "testnet-v3-demo3.json").read_text())
        pending = {"from": self.deployer.address, "to": SAFE, "state": "pending"}
        self.assertEqual((record["key_binder"]["owner"], record["factory_operator"]), (pending, pending))
        self.assertIn("transferOperator", record["tx"])
        self.assertIn("transferOwnership", record["tx"])

    def test_without_owner_the_deployer_keeps_both_and_the_record_says_so(self):
        self.run_main("--label", "demo3")
        self.assertNotIn("transferOperator", self.sent)
        self.assertNotIn("transferOwnership", self.sent)
        self.assertEqual(self.sent[0], "setAccountSource")
        self.assertNotIn("setPublisher", self.sent)
        record = json.loads((self.root / "deployments" / "testnet-v3-demo3.json").read_text())
        self.assertNotIn("factory_operator", record)
        self.assertIsNone(record["key_binder"]["publisher"])

    def test_bad_binder_settings_are_refused_before_anything(self):
        for argv, said in ((["--safe", SAFE.lower()], "checksummed"), (["--safe", "0x12"], "not an address"),
                           (["--guardian", "0x" + "00" * 20], "zero address"),
                           (["--publisher", PUBLISHER], "--pcr-hash and --attestation-hash"),
                           (["--publisher", PUBLISHER, "--pcr-hash", "0x" + "00" * 32, "--attestation-hash", DOC], "cannot be zero"),
                           (["--publisher", PUBLISHER, "--pcr-hash", "0x12", "--attestation-hash", DOC], "64 hex digits"),
                           (["--pcr-hash", PCR], "give --publisher"),
                           (["--renew-delay", str(7 * 86_400 + 1)], "--renew-delay"),
                           (["--max-binds-per-day", "0"], "at least 1")):
            with mock.patch.object(d.sys, "argv", ["deploy_v3", "--label", "demo3", *argv]), \
                    self.assertRaises(SystemExit, msg=argv) as caught:
                d.main()
            self.assertIn(said, str(caught.exception), argv)
        d.git_head.assert_not_called()

    def test_a_test_publisher_is_refused_on_mainnet(self):
        h = d.kb.TEST_PUBLISHER_HASH
        for pcr, doc in ((h, h), (h, DOC), (PCR, h)):
            with mock.patch.object(d.sys, "argv", ["deploy_v3", "--label", "v3", "--network", "mainnet", "--dry-run",
                                                   "--publisher", PUBLISHER, "--pcr-hash", pcr, "--attestation-hash", doc]), \
                    self.assertRaises(SystemExit, msg=(pcr, doc)) as caught:
                d.main()
            self.assertIn("testnet", str(caught.exception))

    def test_a_test_publisher_on_testnet_is_said_and_recorded_as_one(self):
        h = d.kb.TEST_PUBLISHER_HASH
        out = self.run_main("--label", "demo3", "--dry-run", "--publisher", PUBLISHER, "--pcr-hash", h, "--attestation-hash", h)
        self.assertIn("a key in a file, testnet only", out)
        self.run_main("--label", "demo3", "--publisher", PUBLISHER, "--pcr-hash", h, "--attestation-hash", h)
        record = json.loads((self.root / "deployments" / "testnet-v3-demo3.json").read_text())
        self.assertTrue(record["key_binder"]["publisher"]["test_key"])

    def test_the_mainnet_plan_names_the_handover(self):
        out = self.run_main("--label", "v3", "--network", "mainnet", "--dry-run", "--safe", SAFE)
        self.assertIn(f"the key binder (transferOwnership) and the factory (transferOperator) to {SAFE}", out)

    def test_a_mainnet_run_needs_its_fuse_named(self):
        self.assertRaises(SystemExit, d.kb.constructor, SAFE, d.kb.settings(
            mock.Mock(safe=None, publisher=None, pcr_hash=None, attestation_hash=None, guardian=None,
                      max_binds_per_day=None, renew_delay=86_400), "mainnet"))

    def test_a_faucet_on_mainnet_is_refused(self):
        with mock.patch.object(d.sys, "argv", ["deploy_v3", "--label", "v3", "--network", "mainnet",
                                               "--dry-run", "--faucet", FAUCET]):
            with self.assertRaises(SystemExit):
                d.main()

    def test_a_label_that_exists_is_refused(self):
        (self.root / "deployments" / "testnet-v3-demo3.json").write_text("{}")
        with mock.patch.object(d.sys, "argv", ["deploy_v3", "--label", "demo3", "--dry-run"]):
            with self.assertRaises(SystemExit):
                d.main()

    def test_the_factorys_constructor_is_the_one_the_contract_declares(self):
        text = (ROOT / "src/PoolFactory.sol").read_text()
        params = re.search(r"constructor\((.*?)\) \{", text, re.S).group(1)
        self.assertEqual([p.split()[0] for p in params.split(",")],
                         ["KeyBinder", "address", "address", "address", "IERC20"])
        self.assertIn('"PoolFactory", "PoolFactory",\n                ["address", "address", "address", "address", "address"]',
                      (ROOT / "ops/deploy_v3.py").read_text())

    def test_the_binders_constructor_is_the_one_the_contract_declares(self):
        text = (ROOT / "src/KeyBinder.sol").read_text()
        params = re.search(r"constructor\((.*?)\) EIP712", text, re.S).group(1)
        self.assertEqual([p.split()[0] for p in params.split(",")], ["address", "uint32", "uint64"])
        self.assertEqual(d.kb.constructor(SAFE, d.kb.settings(
            mock.Mock(safe=None, publisher=None, pcr_hash=None, attestation_hash=None, guardian=None,
                      max_binds_per_day=None, renew_delay=86_400), "testnet"))[0], ["address", "uint32", "uint64"])
        self.assertIn(f"MAX_RENEW_DELAY = {d.kb.MAX_RENEW_DELAY // 86_400} days", text)


if __name__ == "__main__":
    unittest.main()
