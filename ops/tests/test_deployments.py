"""ops/deployments.py reads the record of the selected network: deployments/<network>-<label>.json, whose chain id
and `network` field must be that network's. The records here are written into a directory of their own."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from ops import deployments
from spike.hlspike import network

MAINNET_RECORD = {"label": "v3-main", "network": "mainnet", "chain_id": 999, "status": "complete",
                  "PoolFactory": "0x" + "f1" * 20, "KeyRegistry": "0x" + "a1" * 20}
TESTNET_RECORD = {"label": "t", "chain_id": 998, "status": "complete",
                  "PoolFactory": "0x" + "f2" * 20, "KeyRegistry": "0x" + "a2" * 20}


class RecordsByNetwork(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "deployments").mkdir()
        patch = mock.patch.object(deployments, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name: str, record: dict) -> None:
        (self.root / "deployments" / name).write_text(json.dumps(record))

    def test_a_mainnet_record_is_read_from_its_own_file_on_mainnet(self):
        self.write("mainnet-v3-main.json", MAINNET_RECORD)
        self.assertEqual(deployments.load("v3-main", network.MAINNET)["PoolFactory"], MAINNET_RECORD["PoolFactory"])
        with self.assertRaises(SystemExit) as caught:
            deployments.load("v3-main", network.TESTNET)
        self.assertIn("no deployment named 'v3-main' on testnet", str(caught.exception))

    def test_the_selected_network_is_the_one_colosseum_network_names(self):
        self.write("mainnet-v3-main.json", MAINNET_RECORD)
        self.write("testnet-t.json", TESTNET_RECORD)
        with mock.patch.dict("os.environ", {network.ENV: "mainnet"}):
            self.assertEqual(deployments.load("v3-main")["label"], "v3-main")
            self.assertEqual(deployments.record_path("t").name, "mainnet-t.json")
            self.assertEqual([p.name for p in deployments.records()], ["mainnet-v3-main.json"])
        with mock.patch.dict("os.environ", {network.ENV: ""}):
            self.assertEqual(deployments.load("t")["label"], "t")
            self.assertEqual([p.name for p in deployments.records()], ["testnet-t.json"])

    def test_a_record_of_another_chain_or_network_under_this_networks_name_is_refused(self):
        self.write("mainnet-wrong-chain.json", {**MAINNET_RECORD, "chain_id": 998})
        self.write("mainnet-wrong-name.json", {**MAINNET_RECORD, "network": "testnet"})
        self.write("testnet-wrong-chain.json", {**TESTNET_RECORD, "chain_id": 999})
        for label, net, why in [("wrong-chain", network.MAINNET, "not a mainnet deployment: chain_id 998, not 999"),
                                ("wrong-name", network.MAINNET, "says it is on testnet, not mainnet"),
                                ("wrong-chain", network.TESTNET, "not a testnet deployment: chain_id 999, not 998")]:
            with self.subTest(label=label, net=net.name), self.assertRaises(SystemExit) as caught:
                deployments.load(label, net)
            self.assertIn(why, str(caught.exception))

    def test_a_record_that_is_not_json_is_refused_by_name(self):
        (self.root / "deployments" / "mainnet-broken.json").write_text("{not json")
        with self.assertRaises(SystemExit) as caught:
            deployments.load("broken", network.MAINNET)
        self.assertIn("deployments/mainnet-broken.json cannot be read", str(caught.exception))

    def test_an_incomplete_mainnet_record_is_refused_as_a_testnet_one_is(self):
        self.write("mainnet-half.json", {**MAINNET_RECORD, "status": "incomplete", "error": "underpriced"})
        with self.assertRaises(SystemExit) as caught:
            deployments.load("half", network.MAINNET)
        self.assertIn("incomplete, not complete: underpriced", str(caught.exception))


class TheRecordsInTheRepository(unittest.TestCase):
    def test_every_testnet_record_loads_on_testnet_and_none_on_mainnet(self):
        names = [p.name for p in deployments.records(network.TESTNET)]
        self.assertGreaterEqual(len(names), 10)
        for name in names:
            label = name.removeprefix("testnet-").removesuffix(".json")
            with self.subTest(label=label):
                self.assertEqual(deployments.load(label, network.TESTNET)["chain_id"], 998)
        self.assertEqual(deployments.records(network.MAINNET), [])


class Resolve(unittest.TestCase):
    def test_a_record_with_a_key_binder_gives_its_address_for_the_keys(self):
        from unittest import mock
        binder = {"PoolFactory": "0x" + "f1" * 20, "KeyBinder": "0x" + "b1" * 20}
        registry = {"PoolFactory": "0x" + "f1" * 20, "KeyRegistry": "0x" + "a1" * 20}
        for record, keys in ((binder, "0x" + "b1" * 20), (registry, "0x" + "a1" * 20)):
            with mock.patch.object(deployments, "load", return_value=record):
                self.assertEqual(deployments.resolve("x"), ("0x" + "f1" * 20, keys))
        with mock.patch.object(deployments, "load", return_value={"PoolFactory": "0x" + "f1" * 20}), \
                self.assertRaises(SystemExit):
            deployments.resolve("x")


class RegistryOperator(unittest.TestCase):
    def test_the_deployer_unless_handed_over_pending_until_accepted(self):
        deployer, given = "0x" + "d1" * 20, "0x" + "cc" * 20
        self.assertEqual(deployments.registry_operator({"deployer": deployer}), {"operator": deployer, "pending": None})
        pending = {"deployer": deployer, "registry_operator": {"from": deployer, "to": given, "state": "pending"}}
        self.assertEqual(deployments.registry_operator(pending), {"operator": deployer, "pending": given})
        accepted = {**pending, "registry_operator": {**pending["registry_operator"], "state": "accepted"}}
        self.assertEqual(deployments.registry_operator(accepted), {"operator": given, "pending": None})
        sent = {**pending, "registry_operator": {**pending["registry_operator"], "state": "sent"}}
        with self.assertRaises(SystemExit) as caught:
            deployments.registry_operator(sent)
        self.assertIn("'sent': pending or accepted", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
