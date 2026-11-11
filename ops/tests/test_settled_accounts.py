"""The reading of settled challenge accounts (scripts/settled_accounts.py): what counts as money left behind, the
exit code that turns the weekly watch red, and the watch itself (.github/workflows/settled-accounts-watch.yml).
Offline: the reading kept in docs/ and copies of it with money put back on an account.

    spike/.venv/bin/python -m unittest ops.tests.test_settled_accounts
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import unittest
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("settled_accounts", ROOT / "scripts" / "settled_accounts.py")
settled = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(settled)
WORKFLOW = ROOT / ".github" / "workflows" / "settled-accounts-watch.yml"
ONE = Decimal("1")


def reading() -> dict:
    return json.loads((ROOT / "docs" / "settled-accounts.json").read_text())


def first_settled(result: dict) -> dict:
    for deployment in result["deployments"]:
        for c in deployment["challenges"]:
            if c["status"] == "Settled":
                return c
    raise AssertionError("the reading kept in docs/ has no settled account")


class LeftBehind(unittest.TestCase):
    def test_the_reading_kept_in_docs_has_nothing_left_on_any_settled_account(self):
        self.assertEqual(settled.left_behind(reading(), ONE), [])

    def test_spot_perp_and_evm_usdc_are_added_up_and_the_threshold_is_met_at_equality(self):
        result = reading()
        c = first_settled(result)
        c["spot_usdc"], c["evm_usdc"] = "0.5", "250000"  # 0.25 USDC at six decimals
        c["perp"]["main"]["withdrawable"] = "0.25"
        self.assertEqual(settled.usdc_left(c), ONE)
        found = settled.left_behind(result, ONE)
        self.assertEqual([(f["deployment"], f["challenge"], f["pool"], f["usdc"]) for f in found],
                         [(result["deployments"][0]["label"], c["challenge"], c["pool"], ONE)])
        c["evm_usdc"] = "249999"
        self.assertEqual(settled.left_behind(result, ONE), [], "a millionth under the threshold is not red")

    def test_money_on_an_account_that_has_not_settled_is_not_left_behind(self):
        result = reading()
        c = first_settled(result)
        c["spot_usdc"] = "70"
        for status in ("Created", "Active", "Breached", "Expired", "Forfeited", "Passed", "Aborted"):
            c["status"] = status
            self.assertEqual(settled.left_behind(result, ONE), [], status)
        c["status"] = "Settled"
        self.assertEqual(len(settled.left_behind(result, ONE)), 1)

    def test_a_perp_account_on_a_builder_dex_counts_too(self):
        result = reading()
        c = first_settled(result)
        c["perp"]["xyz"] = {"account_value": "2.5", "withdrawable": "2.5", "positions": 0}
        self.assertEqual(settled.usdc_left(c), Decimal("2.5"))
        self.assertEqual(len(settled.left_behind(result, ONE)), 1)


class Verdict(unittest.TestCase):
    def test_red_only_with_a_threshold_and_money_left_and_each_account_named(self):
        result = reading()
        self.assertEqual(settled.verdict(result, None), 0)
        self.assertEqual(settled.verdict(result, ONE, io.StringIO()), 0)
        c = first_settled(result)
        c["spot_usdc"] = "3"
        err = io.StringIO()
        self.assertEqual(settled.verdict(result, ONE, err), 2)
        self.assertIn(c["challenge"], err.getvalue())
        self.assertIn("3 USDC", err.getvalue())
        self.assertEqual(settled.verdict(result, None), 0, "no threshold, never red")


class Watch(unittest.TestCase):
    def test_it_reads_once_a_week_or_on_a_dispatch_and_goes_red_at_one_usdc(self):
        text = WORKFLOW.read_text()
        self.assertRegex(text, r"\n  schedule:\n    - cron: '\d+ \d+ \* \* \d'\n", "once a week, on one weekday")
        self.assertIn("\n  workflow_dispatch:\n", text)
        self.assertIn("python scripts/settled_accounts.py --out settled-accounts.json --md settled-accounts.md "
                      "--red-above 1\n", text)

    def test_it_only_reads(self):
        text = WORKFLOW.read_text()
        self.assertEqual(re.findall(r"^permissions:\n((?:  .*\n)+)", text, re.M), ["  contents: read\n"])
        self.assertIn("persist-credentials: false", text)
        for word in ("secrets.", "git push", "COLOSSEUM_KEY_DIR", "--wallet"):
            self.assertNotIn(word, text)

    def test_every_action_is_pinned_by_commit(self):
        uses = re.findall(r"uses: (\S+)", WORKFLOW.read_text())
        self.assertTrue(uses)
        for line in uses:
            self.assertRegex(line, r"@[0-9a-f]{40}$", line)


class WhatIsRead(unittest.TestCase):
    """The network and the deployments come from network.py and deployments/LIVE.json, not from constants here."""

    def test_the_network_is_testnet_unless_named_and_its_rpc_and_api_are_network_pys(self):
        from spike.hlspike import network
        self.assertEqual(settled.NET, network.TESTNET)
        self.assertEqual((settled.RPC, settled.INFO), (network.TESTNET.rpc_url, network.TESTNET.api_url + "/info"))

    def test_a_network_reads_its_live_and_testers_entries_and_marks_the_seats_of_the_sites_shared_pool(self):
        from spike.hlspike import network
        live = {"deployments": [
            {"network": "testnet", "role": "live", "record": "live.json", "factory": "0x1"},
            {"network": "testnet", "role": "archive", "record": "archive.json", "factory": "0x5"},
            {"network": "testnet", "role": "testers", "record": "testers.json", "factory": "0x2"},
            {"network": "testnet", "role": "shared", "record": "shared-elsewhere.json", "factory": "0x2"},
            {"network": "testnet", "role": "shared", "record": "shared.json", "factory": "0x1"},
            {"network": "testnet", "role": "staged", "record": "staged.json", "factory": "0x3"},
            {"network": "mainnet", "role": "live", "record": "mainnet.json", "factory": "0x1"},
        ]}
        self.assertEqual(settled.watched(network.TESTNET, live), (["live.json", "testers.json"], "shared.json"))

    def test_by_default_it_reads_the_committed_list(self):
        from spike.hlspike import network
        committed = json.loads(settled.LIVE.read_text())
        self.assertEqual(settled.watched(network.TESTNET), settled.watched(network.TESTNET, committed))
        records, shared = settled.watched(network.TESTNET)
        self.assertTrue(records and shared, "testnet's list has a live entry and a shared pool on its factory")

    def test_mainnet_reads_its_own_entries_and_none_of_testnets(self):
        from spike.hlspike import network
        live = json.loads((ROOT / "deployments" / "LIVE.json").read_text())
        # Testnet's entries only: a mainnet entry added to the list one day must not change what this shows.
        live["deployments"] = [e for e in live["deployments"] if e["network"] != "mainnet"]
        main = {"network": "mainnet", "role": "live", "record": "deployments/mainnet-v3-main.json", "factory": "0xF1"}
        with self.assertRaises(SystemExit) as caught:
            settled.watched(network.MAINNET, live)
        self.assertIn("nothing to read on mainnet", str(caught.exception))
        with_main = {"deployments": live["deployments"] + [main]}
        self.assertEqual(settled.watched(network.MAINNET, with_main), (["deployments/mainnet-v3-main.json"], None))
        shared = {**main, "role": "shared", "record": "deployments/mainnet-shared-main.json"}
        elsewhere = {**shared, "record": "deployments/mainnet-shared-other.json", "factory": "0xF2"}
        self.assertEqual(settled.watched(network.MAINNET, {"deployments": with_main["deployments"] + [elsewhere, shared]}),
                         (["deployments/mainnet-v3-main.json"], "deployments/mainnet-shared-main.json"))

    def test_a_v3_record_is_read_on_its_pools_dex_by_what_it_deployed_not_by_its_label(self):
        from unittest import mock
        factory = "0x" + "f1" * 20
        first = settled.created_by(factory, 1)
        word = lambda v: "0x" + format(v, "064x")  # noqa: E731

        def call(to, signature, args=(), block="latest"):
            return {"usdc()": "0x" + "0" * 24 + "ab" * 20, "pool()": "0x" + "0" * 24 + "cd" * 20,
                    "dex()": word(1), "status()": word(8)}.get(signature, word(0))

        def rpc(method, params):
            return {"eth_getTransactionCount": "0x1", "eth_getBalance": "0x0",
                    "eth_getCode": "0x60" if params[0] == first else "0x"}[method]

        for record, dexes in [({"PoolFactory": factory, "label": "v3-main", "PaperExamImpl": "0x1"}, ["", "xyz"]),
                              ({"PoolFactory": factory, "label": "demo3"}, [""])]:
            with self.subTest(label=record["label"]), mock.patch.object(settled, "call", side_effect=call), \
                    mock.patch.object(settled, "rpc", side_effect=rpc), \
                    mock.patch.object(settled, "core", return_value={}) as core:
                got = settled.read("0x10", ["", "xyz"], set(), record)
                self.assertEqual([c["challenge"] for c in got["challenges"]], [first])
                core.assert_called_once_with(first, dexes)

    def test_the_watch_reads_who_operates_the_registry_beside_what_the_record_says(self):
        from unittest import mock
        factory, registry = "0x" + "f1" * 20, "0x" + "a1" * 20
        given = "0x" + "cc" * 20
        handover = {"from": "0x" + "d1" * 20, "to": given, "state": "pending"}
        word = lambda v: "0x" + format(v, "064x")  # noqa: E731

        def call(to, signature, args=(), block="latest"):
            return {"usdc()": "0x" + "0" * 24 + "ab" * 20, "operator()": "0x" + "0" * 24 + "d1" * 20,
                    "pendingOperator()": "0x" + "0" * 24 + "cc" * 20}.get(signature, word(0))

        record = {"PoolFactory": factory, "KeyRegistry": registry, "label": "x", "registry_operator": handover}
        with mock.patch.object(settled, "call", side_effect=call), \
                mock.patch.object(settled, "rpc", side_effect=lambda m, p: "0x1" if m == "eth_getTransactionCount" else "0x"):
            got = settled.read("0x10", [""], set(), record)
        self.assertEqual(got["registry"], {"address": settled.to_checksum_address(registry),
                                           "operator": settled.to_checksum_address("0x" + "d1" * 20),
                                           "pending_operator": settled.to_checksum_address(given),
                                           "record_says": handover})

    def test_a_registry_whose_operator_is_not_the_records_goes_red_with_code_1(self):
        import io
        deployer, given, other = "0x" + "d1" * 20, "0x" + "cc" * 20, "0x" + "ee" * 20
        zero = settled.ZERO

        def result(operator, pending, record_says):
            return {"deployments": [{"label": "x", "challenges": [], "pools": [], "registry": {
                "address": "0x" + "a1" * 20, "operator": operator, "pending_operator": pending,
                "record_says": record_says}}]}
        handover = {"from": deployer, "to": given, "state": "pending"}
        green = [result(deployer, zero, {"operator": deployer}), result(deployer, given, handover),
                 result(given, zero, {**handover, "state": "accepted"})]
        for r in green:
            self.assertEqual(settled.exit_code(r, None, io.StringIO()), 0)
        for r in [result(other, zero, {"operator": deployer}),        # another operator than the record's
                  result(deployer, other, handover),                  # another pending one
                  result(deployer, zero, handover),                   # the handover the record says is not there
                  result(deployer, given, {**handover, "state": "accepted"})]:
            err = io.StringIO()
            self.assertEqual(settled.exit_code(r, None, err), 1)
            self.assertIn("registry operator does not match the record: x", err.getvalue())
        # A handover the record cannot say: another state, or no addresses. Red, never read as pending.
        for broken in [{**handover, "state": "sent"}, {"from": deployer, "state": "pending"}, {"state": "accepted", "to": None}]:
            err = io.StringIO()
            self.assertEqual(settled.exit_code(result(deployer, given, broken), None, err), 1)
            self.assertIn("is not a pending or accepted handover", err.getvalue())
        # Money left behind is still the louder: 2.
        money = reading()
        c = first_settled(money)
        c["spot_usdc"] = "5"
        money["deployments"][0]["registry"] = result(other, zero, {"operator": deployer})["deployments"][0]["registry"]
        self.assertEqual(settled.exit_code(money, ONE, io.StringIO()), 2)
        # A reading from before the registry was read has nothing to compare.
        self.assertEqual(settled.registry_mismatches(reading()), [])

    def test_the_watch_reads_the_key_binder_and_the_factorys_operator_beside_what_the_record_says(self):
        from unittest import mock
        factory, binder = "0x" + "f1" * 20, "0x" + "b1" * 20
        word = lambda v: "0x" + format(v, "064x")  # noqa: E731
        addr = lambda a: "0x" + "0" * 24 + a[2:]  # noqa: E731
        deployer, safe, pub, guard = ("0x" + x * 20 for x in ("d1", "5a", "ab", "cd"))
        answers = {(binder, "owner()"): addr(deployer), (binder, "pendingOwner()"): addr(safe),
                   (binder, "publisher()"): addr(pub), (binder, "guardian()"): addr(guard), (binder, "paused()"): word(1),
                   (factory, "operator()"): addr(deployer), (factory, "pendingOperator()"): addr(safe)}

        def call(to, signature, args=(), block="latest"):
            if signature == "usdc()":
                return addr("0x" + "ab" * 20)
            return answers.get((to.lower(), signature), word(0))

        kb = {"owner": {"from": deployer, "to": safe, "state": "pending"}, "publisher": {"address": pub}, "guardian": guard}
        record = {"PoolFactory": factory, "KeyBinder": binder, "label": "x", "deployer": deployer, "key_binder": kb,
                  "factory_operator": {"from": deployer, "to": safe, "state": "pending"}}
        with mock.patch.object(settled, "call", side_effect=call), \
                mock.patch.object(settled, "rpc", side_effect=lambda m, p: "0x1" if m == "eth_getTransactionCount" else "0x"):
            got = settled.read("0x10", [""], set(), record)
        c = settled.to_checksum_address
        self.assertEqual({k: got["binder"][k] for k in ("owner", "pending_owner", "publisher", "guardian", "paused",
                                                         "factory_operator", "factory_pending_operator")},
                         {"owner": c(deployer), "pending_owner": c(safe), "publisher": c(pub), "guardian": c(guard),
                          "paused": True, "factory_operator": c(deployer), "factory_pending_operator": c(safe)})
        self.assertIsNone(got["registry"])
        self.assertEqual(settled.binder_mismatches({"deployments": [{**got, "label": "x"}]}),
                         [f"key binder is paused: x {c(binder)}: no key is bound until the owner lifts it"])

    def test_a_key_binder_that_is_not_the_records_goes_red_with_code_1(self):
        import io
        deployer, safe, pub, guard, other = ("0x" + x * 20 for x in ("d1", "5a", "ab", "cd", "ee"))
        zero = settled.ZERO

        def result(owner=deployer, pending=zero, publisher=pub, guardian=guard, paused=False, op=deployer, op_pending=zero,
                   kb_owner=None, factory_operator=None):
            return {"deployments": [{"label": "x", "challenges": [], "pools": [], "binder": {
                "address": "0x" + "b1" * 20, "owner": owner, "pending_owner": pending, "publisher": publisher,
                "guardian": guardian, "paused": paused, "factory_operator": op, "factory_pending_operator": op_pending,
                "record_says": {"deployer": deployer, "factory_operator": factory_operator, "key_binder": {
                    "owner": kb_owner or {"from": deployer, "to": None, "state": "the deployer"},
                    "publisher": {"address": pub}, "guardian": guard}}}}]}
        pending = {"from": deployer, "to": safe, "state": "pending"}
        accepted = {**pending, "state": "accepted"}
        green = [result(), result(pending=safe, op_pending=safe, kb_owner=pending, factory_operator=pending),
                 result(owner=safe, op=safe, kb_owner=accepted, factory_operator=accepted),
                 result(kb_owner={"from": deployer, "to": safe, "state": "named, not sent"})]
        for r in green:
            self.assertEqual(settled.exit_code(r, None, io.StringIO()), 0, r)
        for r, said in [(result(owner=other), "key binder owner"), (result(pending=other), "key binder owner"),
                        (result(kb_owner=pending), "key binder owner"),
                        (result(op=other), "factory operator"), (result(factory_operator=accepted), "factory operator"),
                        (result(publisher=other), "key binder publisher"), (result(guardian=zero), "key binder guardian"),
                        (result(paused=True), "key binder is paused"),
                        (result(kb_owner={**pending, "state": "sent"}), "is not a handover")]:
            err = io.StringIO()
            self.assertEqual(settled.exit_code(r, None, err), 1, said)
            self.assertIn(said, err.getvalue())
        self.assertEqual(settled.binder_mismatches(reading()), [], "a reading before binders has nothing to compare")

    def test_a_reading_without_a_shared_pool_still_makes_a_table(self):
        result = reading()
        result["shared_pool"] = None
        self.assertIn("| Deployment |", settled.table(result))


if __name__ == "__main__":
    unittest.main()
