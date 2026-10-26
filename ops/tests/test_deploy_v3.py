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
KEY = "0x00000000000000000000000000000000000000a1"


class DeployV3(unittest.TestCase):
    """Factory v3 in one command (the CTO's 1/8/1): on testnet the demo's token first, then the
    registry, the implementations, the factory on that token, the paper exam and the reader, then
    the settings and the keys; on mainnet a plan and nothing sent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "deployments").mkdir()
        self.keys = self.root / "keys.txt"
        self.keys.write_text(KEY + "\n")
        self.deployed: list[tuple[str, list]] = []
        self.linked: dict = {}
        self.sent: list[str] = []
        self.deployer = mock.Mock(address="0x00000000000000000000000000000000000000De")

        def deploy(acct, contract, types, values, libraries=None):
            addr = "0x" + f"{len(self.deployed) + 1:040x}"
            self.deployed.append((contract, list(values)))
            self.linked[contract] = libraries
            return d.to_checksum_address(addr), {"transactionHash": "0x01", "blockNumber": "0x10"}

        def transact(acct, to, sig, types, values):
            self.sent.append(sig.split("(")[0])
            return {"transactionHash": "0x02"}

        patches = [
            mock.patch.object(d, "ROOT", self.root),
            mock.patch.object(d, "git_head", return_value="ab" * 20),
            mock.patch.object(d.subprocess, "run"),
            mock.patch.object(d, "initcode_keccak", side_effect=lambda name: "0x" + "11" * 32),
            mock.patch.object(d, "shares_code", return_value=bytes.fromhex("22" * 32)),
            mock.patch.object(d, "check_assets", return_value=[3, 4, 0]),
            mock.patch.object(d, "check_keys", side_effect=lambda lines: [d.to_checksum_address(KEY)]),
            mock.patch.object(d, "registries_in_records", return_value={}),
            mock.patch.object(d, "big_blocks"),
            mock.patch.object(d.c, "assert_testnet"),
            mock.patch.object(d.c, "account", return_value=self.deployer),
            mock.patch.object(d.c, "core_user_exists", return_value=True),
            mock.patch.object(d.c, "deploy", side_effect=deploy),
            mock.patch.object(d.c, "transact", side_effect=transact),
            mock.patch.object(d.c, "rpc", return_value="0x6001"),
            mock.patch.object(d.c, "record"),
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
        self.run_main("--label", "demo3", "--keys-file", str(self.keys), "--faucet", FAUCET, "--faucet-daily", "500")
        self.assertEqual([name for name, _ in self.deployed],
                         ["DemoUsdc", "KeyRegistry", "AccountOps", "Pool", "ChallengeAccount", "PoolFactory",
                          "PaperExam", "MultiPoolReader"])
        library = {"AccountOps": d.to_checksum_address("0x" + f"{3:040x}")}
        self.assertEqual((self.linked["Pool"], self.linked["ChallengeAccount"]), (library, library))
        token = d.to_checksum_address("0x" + f"{1:040x}")
        factory_args = dict(self.deployed)["PoolFactory"]
        self.assertEqual(factory_args[-1], token, "the factory on the demo's token")
        self.assertEqual(factory_args[3], self.deployer.address, "the deployer operates the factory")
        self.assertEqual(self.sent, ["setAccountSource", "setPlatformAssets", "setFundedAssets", "setExamImpl",
                                     "setSharesCode", "setChallengeFee", "setFaucet", "publish"])
        record = json.loads((self.root / "deployments" / "testnet-v3-demo3.json").read_text())
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["usdc"], token)
        self.assertEqual(record["faucet"], {"address": d.to_checksum_address(FAUCET), "daily": 500 * 10**6})
        self.assertEqual(set(record["codehash"]), {k for k, _ in d.CONTRACTS})
        self.assertEqual(set(record["initcode_keccak"]), {k for k, _ in d.CONTRACTS})
        self.assertEqual(record["chain_id"], 998)
        self.assertEqual(record["core_writer"], d.c.CORE_WRITER)
        self.assertEqual(record["published_keys"], [d.to_checksum_address(KEY)])
        journal = json.loads((self.root / "deployments" / "registries.json").read_text())
        self.assertEqual([e["address"] for e in journal], [record["KeyRegistry"]], "A5-3: the registry is journaled")

    def test_a_dry_run_checks_everything_and_sends_nothing(self):
        out = self.run_main("--label", "demo3", "--keys-file", str(self.keys), "--dry-run")
        self.assertEqual(self.deployed, [])
        self.assertEqual(self.sent, [])
        self.assertIn("nothing was sent", out)
        self.assertIn("DemoUsdc", out)
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
        self.assertEqual(d.plan("mainnet", d.NETWORKS["mainnet"]["usdc"])[0], "KeyRegistry")

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
                         ["KeyRegistry", "address", "address", "address", "IERC20"])
        self.assertIn('"PoolFactory", "PoolFactory",\n                ["address", "address", "address", "address", "address"]',
                      (ROOT / "ops/deploy_v3.py").read_text())


if __name__ == "__main__":
    unittest.main()
