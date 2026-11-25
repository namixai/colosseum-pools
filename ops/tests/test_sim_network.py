"""The simulated pool's network switch (ops/sim_network.py): testnet unless mainnet is named twice, each network's
asset indices from one table, and every sending path refusing mainnet without its confirmation. Both paths, with
nothing read from a network and no key."""
import argparse
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ops import deploy_sim as d
from ops import sim_check as chk
from ops import sim_keeper as sk
from ops import sim_network as sn
from ops import sim_relay as relay
from spike.hlspike import common as c
from spike.hlspike import network as networks

ROOT = pathlib.Path(__file__).resolve().parents[2]
A, B, C3 = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
MAINNET_KEYS = {"COLOSSEUM_KEY_DIR": "/nonexistent/colosseum-mainnet"}


def on(net):
    """The plumbing as if COLOSSEUM_NETWORK named `net`: its network, chain id and USDC, and both RPC checks mocked."""
    return mock.patch.multiple(c, NETWORK=net, CHAIN_ID=net.chain_id, USDC_ERC20=net.usdc_erc20,
                               assert_testnet=mock.DEFAULT, assert_network=mock.DEFAULT)


def fake_c(net):
    return SimpleNamespace(NETWORK=net, RPC_URL=net.rpc_url, assert_testnet=mock.Mock(), assert_network=mock.Mock())


class TheTable(unittest.TestCase):
    def test_each_networks_indices(self):
        """Read from each network's info API meta on 14 November 2026 (ops/sim_probe.py reads them again)."""
        self.assertEqual(sn.SIM["testnet"].assets, {"BTC": 3, "ETH": 4, "SOL": 0})
        self.assertEqual(sn.SIM["mainnet"].assets, {"BTC": 0, "ETH": 1, "SOL": 5})
        self.assertEqual(set(sn.SIM), set(networks.NETWORKS))

    def test_the_same_assets_on_both_and_no_hype(self):
        self.assertEqual(set(sn.SIM["testnet"].assets), set(sn.SIM["mainnet"].assets))
        self.assertNotIn("HYPE", sn.SIM["mainnet"].assets)

    def test_the_precompiles_are_the_plumbings(self):
        self.assertEqual(sn.ORACLE_PX.lower(), c.PRECOMPILE["oraclePx"].lower())
        self.assertTrue(sn.BBO.lower().endswith("080e"))
        self.assertEqual(sn.PERP_ASSET_INFO.lower(), c.PRECOMPILE["perpAssetInfo"].lower())

    def test_an_unknown_asset_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            sn.SIM["mainnet"].index("HYPE")
        self.assertIn("not a mainnet platform asset", str(caught.exception))

    def test_check_assets_reads_the_meta_and_stops_on_a_difference(self):
        universe = [{"name": "BTC"}, {"name": "ETH"}, {}, {}, {}, {"name": "SOL"}]
        sn.check_assets(["BTC", "ETH", "SOL"], networks.MAINNET, lambda body: {"universe": universe})
        moved = [{"name": "BTC"}, {"name": "ETH"}, {}, {}, {}, {"name": "AVAX"}]
        with self.assertRaises(SystemExit) as caught:
            sn.check_assets(["SOL"], networks.MAINNET, lambda body: {"universe": moved})
        self.assertIn("index 5 is AVAX", str(caught.exception))
        with self.assertRaises(SystemExit):
            sn.check_assets(["BTC"], networks.MAINNET, lambda body: {"universe": [None]})
        delisted = [{"name": "BTC", "isDelisted": True}]
        with self.assertRaises(SystemExit):
            sn.check_assets(["BTC"], networks.MAINNET, lambda body: {"universe": delisted})


class TheSwitch(unittest.TestCase):
    def args(self, network=None, **kw):
        return SimpleNamespace(network=network, **kw)

    def test_testnet_by_default(self):
        self.assertIs(sn.selected(self.args(), networks.TESTNET), networks.TESTNET)
        p = argparse.ArgumentParser()
        sn.add_args(p)
        self.assertEqual(p.parse_args([]).network, "testnet")

    def test_the_flag_and_the_env_must_agree(self):
        with self.assertRaises(SystemExit) as caught:
            sn.selected(self.args("mainnet"), networks.TESTNET)
        self.assertIn("--network mainnet, but COLOSSEUM_NETWORK selects testnet", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            sn.selected(self.args(), networks.MAINNET)  # the env alone does not choose mainnet
        self.assertIn("--network testnet, but COLOSSEUM_NETWORK selects mainnet", str(caught.exception))
        self.assertIs(sn.selected(self.args("mainnet"), networks.MAINNET), networks.MAINNET)

    def test_a_one_off_send_on_testnet_is_the_plumbings_check(self):
        fc = fake_c(networks.TESTNET)
        sn.confirm_send(self.args(), "x", fc, ask=mock.Mock(), tty=False)
        fc.assert_testnet.assert_called_once()
        fc.assert_network.assert_not_called()

    def test_a_one_off_send_on_mainnet_needs_a_mainnet_key_dir_a_terminal_and_the_phrase(self):
        fc = fake_c(networks.MAINNET)
        args = self.args("mainnet")
        ask = mock.Mock(return_value=sn.CONFIRM_PHRASE)
        for env in ({}, {"COLOSSEUM_KEY_DIR": ""}, {"COLOSSEUM_KEY_DIR": "~/secure/colosseum-testnet"}):
            with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit) as caught:
                sn.confirm_send(args, "x", fc, ask=ask, tty=True)
            self.assertIn("COLOSSEUM_KEY_DIR", str(caught.exception))
        with mock.patch.dict(os.environ, MAINNET_KEYS, clear=True):
            with self.assertRaises(SystemExit) as caught:
                sn.confirm_send(args, "x", fc, ask=ask, tty=False)
            self.assertIn("without a terminal", str(caught.exception))
            for typed in ("", "yes", "send on testnet", "SEND ON MAINNET"):
                with mock.patch("builtins.print"), self.assertRaises(SystemExit) as caught:
                    sn.confirm_send(args, "x", fc, ask=mock.Mock(return_value=typed), tty=True)
                self.assertIn("not confirmed", str(caught.exception))
            fc.assert_network.assert_not_called()
            with mock.patch("builtins.print"):
                sn.confirm_send(args, "x", fc, ask=ask, tty=True)
        fc.assert_network.assert_called_once()
        fc.assert_testnet.assert_not_called()

    def test_a_service_on_mainnet_sends_only_with_its_label(self):
        fc = fake_c(networks.MAINNET)
        with mock.patch.dict(os.environ, MAINNET_KEYS, clear=True):
            for confirm in (None, "sim2", ""):
                with self.assertRaises(SystemExit) as caught:
                    sn.confirm_service(self.args("mainnet", confirm_mainnet=confirm), "sim1", fc, sends=True)
                self.assertIn("--confirm-mainnet sim1", str(caught.exception))
            fc.assert_network.assert_not_called()
            sn.confirm_service(self.args("mainnet", confirm_mainnet="sim1"), "sim1", fc, sends=True)
        fc.assert_network.assert_called_once()
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            sn.confirm_service(self.args("mainnet", confirm_mainnet="sim1"), "sim1", fc, sends=True)

    def test_a_service_that_only_reads_needs_no_confirmation(self):
        fc = fake_c(networks.MAINNET)
        with mock.patch.dict(os.environ, {}, clear=True):
            sn.confirm_service(self.args("mainnet"), "sim1", fc, sends=False)
        fc.assert_network.assert_called_once()
        ft = fake_c(networks.TESTNET)
        sn.confirm_service(self.args(), "sim1", ft, sends=True)
        ft.assert_testnet.assert_called_once()


class TheDeployScript(unittest.TestCase):
    def deploy_argv(self, *extra):
        return ["deploy", "--label", "never-written", "--filler", A, "--checker", B, "--closer", C3, *extra]

    def nodes_file(self, tmp):
        path = pathlib.Path(tmp) / "nodes.json"
        path.write_text(json.dumps({"primary": {"url": "https://node.example/evm", "log_window": 50}}))
        return str(path)

    def test_testnet_stays_as_it_was(self):
        with on(networks.TESTNET), mock.patch("builtins.print") as out:
            self.assertEqual(d.main(self.deploy_argv()), 0)
        text = "\n".join(str(x.args[0]) for x in out.call_args_list)
        self.assertIn(networks.TESTNET.usdc_erc20, text)
        self.assertIn("BTC (3)", text)
        self.assertIn("deployments/testnet-never-written.json", text)

    def test_mainnet_is_refused_unless_the_env_names_it_too(self):
        with on(networks.TESTNET), self.assertRaises(SystemExit) as caught:
            d.main(self.deploy_argv("--network", "mainnet"))
        self.assertIn("COLOSSEUM_NETWORK selects testnet", str(caught.exception))
        with on(networks.MAINNET), self.assertRaises(SystemExit) as caught:
            d.main(self.deploy_argv())
        self.assertIn("COLOSSEUM_NETWORK selects mainnet", str(caught.exception))

    def test_mainnets_plan_names_its_usdc_its_indices_and_its_own_keeper_node(self):
        with on(networks.MAINNET), self.assertRaises(SystemExit) as caught:
            d.main(self.deploy_argv("--network", "mainnet"))
        self.assertIn("--keeper-nodes is needed on mainnet", str(caught.exception))
        with tempfile.TemporaryDirectory() as tmp, on(networks.MAINNET), self.assertRaises(SystemExit) as caught:
            d.main(self.deploy_argv("--network", "mainnet", "--keeper-nodes", self.nodes_file(tmp)))
        self.assertIn("--safe is needed on mainnet", str(caught.exception))
        safe = "0x" + "5a" * 20
        with tempfile.TemporaryDirectory() as tmp, on(networks.MAINNET), mock.patch("builtins.print") as out:
            self.assertEqual(d.main(self.deploy_argv("--network", "mainnet", "--keeper-nodes", self.nodes_file(tmp),
                                                     "--safe", safe)), 0)
        text = "\n".join(str(x.args[0]) for x in out.call_args_list)
        self.assertIn(f"would hand over  setOperator {d.to_checksum_address(safe)} last", text)
        self.assertIn(networks.MAINNET.usdc_erc20, text)
        self.assertIn("mainnet (chain 999)", text)
        self.assertIn("BTC (0)", text)
        self.assertIn("ETH (1)", text)
        self.assertIn("SOL (5)", text)
        self.assertIn("deployments/mainnet-never-written.json", text)
        self.assertNotIn(networks.TESTNET.usdc_erc20, text)

    def test_a_bad_keeper_node_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, on(networks.MAINNET):
            path = pathlib.Path(tmp) / "nodes.json"
            path.write_text(json.dumps({"primary": {"url": "http://plain.example", "log_window": 50}}))
            with self.assertRaises(SystemExit) as caught:
                d.main(self.deploy_argv("--network", "mainnet", "--keeper-nodes", str(path)))
        self.assertIn("not an https address", str(caught.exception))

    def test_a_mainnet_send_without_a_terminal_sends_nothing_and_reads_no_key(self):
        with tempfile.TemporaryDirectory() as tmp, on(networks.MAINNET), mock.patch.dict(os.environ, MAINNET_KEYS), \
                mock.patch("sys.stdin.isatty", return_value=False), mock.patch("builtins.print"), \
                mock.patch.object(d, "deploy_contract") as deployed, mock.patch.object(d.c, "account") as key, \
                mock.patch.object(d, "big_blocks") as big:
            with self.assertRaises(SystemExit) as caught:
                d.main(self.deploy_argv("--network", "mainnet", "--keeper-nodes", self.nodes_file(tmp), "--safe",
                                        "0x" + "5a" * 20, "--send"))
        self.assertIn("without a terminal", str(caught.exception))
        deployed.assert_not_called()
        key.assert_not_called()
        big.assert_not_called()

    def test_a_confirmed_mainnet_send_reads_the_meta_before_any_key(self):
        """After the phrase, the meta is read and a moved index stops everything before a key is opened."""
        moved = {"universe": [{"name": "BTC"}, {"name": "ETH"}, {}, {}, {}, {"name": "AVAX"}]}
        with tempfile.TemporaryDirectory() as tmp, on(networks.MAINNET), mock.patch.dict(os.environ, MAINNET_KEYS), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value=sn.CONFIRM_PHRASE), \
                mock.patch("builtins.print") as shown, mock.patch.object(d.c, "info_post", return_value=moved), \
                mock.patch.object(d, "safe_facts", return_value=(2, [d.to_checksum_address("0x" + x * 20) for x in ("a1", "a2", "a3")])), \
                mock.patch.object(d.c, "account") as key, mock.patch.object(d, "deploy_contract") as deployed, \
                mock.patch.object(d, "big_blocks"), mock.patch.object(d, "save"), mock.patch.object(d, "mark_incomplete"):
            with self.assertRaises(SystemExit) as caught:
                d.main(self.deploy_argv("--network", "mainnet", "--keeper-nodes", self.nodes_file(tmp), "--safe",
                                        "0x" + "5a" * 20, "--send"))
        self.assertIn("mainnet meta changed", str(caught.exception))
        # What the person was shown before typing the phrase: to whom the factory goes, and the Safe's threshold.
        prompt = [str(x.args[0]) for x in shown.call_args_list if str(x.args[0]).startswith("MAINNET")]
        self.assertEqual(len(prompt), 1)
        self.assertIn("2 of its 3 owners must sign", prompt[0])
        self.assertIn(d.to_checksum_address("0x" + "a2" * 20), prompt[0])
        key.assert_not_called()
        deployed.assert_not_called()

    def test_the_rules_take_the_networks_indices(self):
        rules = {"daily_loss_bps": 300, "max_drawdown_bps": 600, "max_leverage_x100": 1000,
                 "assets": ["BTC", "ETH", "SOL"], "asset_leverage_x100": [1000, 1000, 500]}
        with on(networks.TESTNET):
            self.assertEqual(d._rules(rules)[3], [3, 4, 0])
        with on(networks.MAINNET):
            self.assertEqual(d._rules(rules)[3], [0, 1, 5])

    def test_mainnet_refuses_a_plan_written_for_testnet(self):
        # The measuring pool's plan of testnet says testnet; mainnet's is sim-measure-pool-999.json (24 November); the
        # first pool's is one plan for both networks (16 November).
        plan = str(ROOT / "deployments/plans/sim-measure-pool.json")
        with on(networks.MAINNET), mock.patch.object(d.deployments, "load", return_value={"SimPoolFactory": A}):
            with self.assertRaises(SystemExit) as caught:
                d.main(["pool", "--deployment", "x", "--plan", plan, "--signer", B, "--network", "mainnet"])
        self.assertIn("written for testnet", str(caught.exception))

    def test_mainnet_opens_the_measuring_pool_of_its_own_plan_and_testnet_refuses_it(self):
        """`pool --signer` with mainnet's measuring plan prints the Safe's three calls: 32 locked, the factory's fee,
        a window of 60 blocks, mainnet's BTC, ETH and SOL; nothing is sent and no key is read. Testnet refuses it."""
        import io
        from contextlib import redirect_stdout
        plan = str(ROOT / "deployments/plans/sim-measure-pool-999.json")
        record = {"SimPoolFactory": A, "TermsBook": C3, "usdc": networks.MAINNET.usdc_erc20}
        out = io.StringIO()
        with on(networks.MAINNET), mock.patch.object(d.deployments, "load", return_value=record), \
                mock.patch.object(d.c, "rpc", return_value="0x%064x" % 10**18), \
                mock.patch.object(d.c, "account") as key, mock.patch.object(d.c, "send_tx") as sent, \
                mock.patch.object(d.c, "transact") as tx, redirect_stdout(out):
            self.assertEqual(d.main(["pool", "--deployment", "x", "--plan", plan, "--signer", B,
                                     "--network", "mainnet"]), 0)
            plan_tuple = d.plan_of(json.loads(pathlib.Path(plan).read_text()), b"\0" * 32)
        key.assert_not_called(), sent.assert_not_called(), tx.assert_not_called()
        said = out.getvalue()
        self.assertIn("1 sizes 200 for 2 USDC", said)
        self.assertIn("locks            32 USDC, pays 1 HYPE (the factory's fee); lines on at the start: 0 ", said)
        self.assertIn("book             at the book, ceiling 150 bp; fill window 60 blocks", said)
        self.assertIn("code             'MEASURE' 0x", said)
        self.assertIn(f"-> {d.to_checksum_address(B)} 0%", said, "the creator's own code, no share")
        txs = json.loads(said[said.index("{"):])["transactions"]
        self.assertEqual([t["to"].lower() for t in txs], [C3, networks.MAINNET.usdc_erc20.lower(), A])
        self.assertEqual([t["value"] for t in txs], ["0", "0", str(10**18)])
        self.assertTrue(txs[1]["data"].endswith("%064x" % (32 * 10**6)), "the Safe approves the 32 it locks")
        self.assertEqual((plan_tuple[1][3], plan_tuple[3][3]), ([0, 1, 5], [0, 1, 5]), "mainnet's BTC, ETH and SOL")
        with on(networks.TESTNET), mock.patch.object(d.deployments, "load", return_value=record):
            with self.assertRaises(SystemExit) as caught:
                d.main(["pool", "--deployment", "x", "--plan", plan, "--signer", B, "--network", "testnet"])
        self.assertIn("written for mainnet", str(caught.exception))

    def test_send_or_say_on_mainnet_without_the_phrase_sends_nothing(self):
        args = SimpleNamespace(network="mainnet", signer=None, send=True, key="closer")
        with on(networks.MAINNET), mock.patch.dict(os.environ, MAINNET_KEYS), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="no"), \
                mock.patch("builtins.print"), mock.patch.object(d.c, "transact") as sent, \
                mock.patch.object(d.c, "account") as key:
            with self.assertRaises(SystemExit) as caught:
                d.send_or_say(args, A, "close()")
        self.assertIn("not confirmed", str(caught.exception))
        sent.assert_not_called()
        key.assert_not_called()

    def test_signer_batches_on_mainnet_need_no_phrase(self):
        """The Safe and the Ledger sign elsewhere: --signer prints the calls and asks nothing."""
        args = SimpleNamespace(network="mainnet", signer=B, send=False, key=None)
        with on(networks.MAINNET), mock.patch("builtins.input") as asked, mock.patch("builtins.print"):
            self.assertEqual(d.send_or_say(args, A, "close()"), 0)
        asked.assert_not_called()


class TheServices(unittest.TestCase):
    class Stop(Exception):
        pass

    def test_the_keeper_on_mainnet_sends_only_with_its_label(self):
        with on(networks.MAINNET), mock.patch.dict(os.environ, MAINNET_KEYS), \
                mock.patch.object(sk.deployments, "load", side_effect=self.Stop) as load:
            with self.assertRaises(SystemExit) as caught:
                sk.main(["--deployment", "sim1", "--network", "mainnet", "--once"])
            self.assertIn("--confirm-mainnet sim1", str(caught.exception))
            load.assert_not_called()
            with mock.patch.object(sk.core, "check_node") as node, self.assertRaises(self.Stop):
                sk.main(["--deployment", "sim1", "--network", "mainnet", "--once", "--confirm-mainnet", "sim1"])
            node.assert_called_once()
            with mock.patch.object(sk.core, "check_node"), self.assertRaises(self.Stop):
                sk.main(["--deployment", "sim1", "--network", "mainnet", "--once", "--dry-run"])

    def test_the_keepers_measure_trades_btc_at_the_networks_index(self):
        for net, idx in ((networks.TESTNET, 3), (networks.MAINNET, 0)):
            argv = ["--deployment", "sim1", "--measure", A, "--trader-wallet", "t1", "--network", net.name]
            with on(net), mock.patch.object(sk.sn, "confirm_send") as confirm, \
                    mock.patch.object(sk.deployments, "load", return_value={"SimPoolFactory": B, "block": 1}), \
                    mock.patch.object(sk.c, "account"), mock.patch("builtins.print"), \
                    mock.patch.object(sk, "measure", return_value={"orders": 0, "filled": 0}) as measured:
                self.assertEqual(sk.main(argv), 0)
            confirm.assert_called_once()
            self.assertEqual(measured.call_args.args[2], idx)

    def test_the_relayer_and_the_checker_on_mainnet(self):
        for main, argv in ((relay.main, ["--deployment", "sim1", "--wallet", "relayer"]),
                           (chk.main, ["--deployment", "sim1", "--wallet", "checker", "--once"])):
            with on(networks.MAINNET), mock.patch.dict(os.environ, MAINNET_KEYS), \
                    mock.patch.object(relay.deployments, "load", side_effect=self.Stop) as load:
                with self.assertRaises(SystemExit) as caught:
                    main(argv + ["--network", "mainnet"])
                self.assertIn("--confirm-mainnet sim1", str(caught.exception))
                load.assert_not_called()
                with self.assertRaises(self.Stop):
                    main(argv + ["--network", "mainnet", "--confirm-mainnet", "sim1"])
                with self.assertRaises(self.Stop):
                    main(argv + ["--network", "mainnet", "--dry-run"])
            with on(networks.TESTNET), mock.patch.object(relay.deployments, "load", side_effect=self.Stop), \
                    self.assertRaises(self.Stop):
                main(argv)  # testnet as before: no flag, no label

    def test_the_hosts_unit_hands_mainnet_only_a_simulated_record(self):
        from ops import multi_keeper
        argv = ["multi_keeper", "--deployment", "sim1", "--once", "--network", "mainnet", "--confirm-mainnet", "sim1"]
        with on(networks.MAINNET), mock.patch("sys.argv", argv), \
                mock.patch.object(multi_keeper.deployments, "load", return_value={"MultiPool": A, "block": 1}):
            with self.assertRaises(SystemExit) as caught:
                multi_keeper.main()
        self.assertIn("MultiPool keeper is testnet only", str(caught.exception))
        with on(networks.MAINNET), mock.patch("sys.argv", argv), \
                mock.patch.object(multi_keeper.deployments, "load", return_value={"SimPoolFactory": A, "block": 1}), \
                mock.patch.object(sk, "main", return_value=0) as sim:
            self.assertEqual(multi_keeper.main(), 0)
        sim.assert_called_once_with(argv[1:])


class TheProbe(unittest.TestCase):
    """ops/sim_probe.py on canned answers: it reads, decides and never sends."""

    class Reader:
        def __init__(self, net, coin_at_5="SOL", oracle_scale=1.0):
            import eth_abi
            self.net, self.abi, self.coin_at_5, self.scale = net, eth_abi, coin_at_5, oracle_scale
            self.methods = []

        def rpc(self, method, params):
            self.methods.append(method)
            return {"eth_chainId": hex(self.net.chain_id),
                    "eth_getBlockByNumber": {"number": "0x10", "timestamp": hex(1_794_677_885)}}[method]

        def info(self, body):
            universe = [{"name": "BTC"}, {"name": "ETH"}, {}, {}, {}, {"name": "SOL"}]
            ctxs = [{"oraclePx": "65739.1"}, {"oraclePx": "1844.8"}, {}, {}, {}, {"oraclePx": "73.087"}]
            return [{"universe": universe}, ctxs]

        def call(self, to, data, block):
            from ops.sim_probe import selector
            enc = self.abi.encode
            names = {0: ("BTC", 5, 657350), 1: ("ETH", 4, 184470), 5: (self.coin_at_5, 2, 730850)}
            if to == sn.PERP_ASSET_INFO:
                coin, dec, _ = names[int.from_bytes(data[-4:], "big")]
                return enc(["(string,uint32,uint8,uint8,bool)"], [(coin, 0, dec, 20, False)])
            if to == sn.ORACLE_PX:
                return enc(["uint64"], [int(names[int.from_bytes(data[-4:], "big")][2] * self.scale)])
            if to == sn.BBO:
                px = names[int.from_bytes(data[-8:], "big")][2]
                return enc(["uint64", "uint64"], [px - 1, px + 1])
            if data[:4] == selector("decimals()"):
                return enc(["uint8"], [6])
            if data[:4] == selector("authorizationState(address,bytes32)"):
                return enc(["bool"], [False])
            return enc(["string"], ["USDC"])

    def test_every_check_holds_on_consistent_answers(self):
        from ops import sim_probe
        r = self.Reader(networks.MAINNET)
        out = sim_probe.probe(r)
        self.assertTrue(out["ok"], [x for x in out["checks"] if not x["ok"]])
        self.assertEqual(out["assets"]["SOL"]["index"], 5)
        self.assertEqual(out["assets"]["BTC"]["oracle_px"], 65735.0)
        self.assertNotIn("eth_sendRawTransaction", r.methods)

    def test_a_wrong_coin_or_a_far_oracle_fails(self):
        from ops import sim_probe
        self.assertFalse(sim_probe.probe(self.Reader(networks.MAINNET, coin_at_5="AVAX"))["ok"])
        self.assertFalse(sim_probe.probe(self.Reader(networks.MAINNET, oracle_scale=1.01))["ok"])

    def test_a_missing_api_price_fails_the_check_instead_of_crashing(self):
        from ops import sim_probe
        r = self.Reader(networks.MAINNET)
        info = r.info
        r.info = lambda body: [info(body)[0], info(body)[1][:2]]  # no context for SOL at 5
        out = sim_probe.probe(r)
        self.assertFalse(out["ok"])
        self.assertFalse([x for x in out["checks"] if x["check"] == "SOL: oraclePx 0x…0807"][0]["ok"])
        self.assertIsNone(out["assets"]["SOL"]["gap_bps"])
        json.dumps(out, allow_nan=False, default=str)  # strict JSON: no Infinity in a failed report


class TheStateIsTheChains(unittest.TestCase):
    """A keeper's or checker's state file names its chain; one of another chain is refused (review of #300)."""

    def keeper(self, path):
        return sk.SimKeeper(A, None, B, True, path, 1)

    def write(self, path, **extra):
        path.write_text(json.dumps({"factory": A, "next_block": 5, "pools": [], "accounts": {}, **extra}))

    def test_save_writes_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp, on(networks.TESTNET):
            path = pathlib.Path(tmp) / "s.json"
            self.keeper(path).save()
            self.assertEqual(json.loads(path.read_text())["chain_id"], 998)

    def test_another_chains_state_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s.json"
            self.write(path, chain_id=998)
            with on(networks.MAINNET), self.assertRaises(SystemExit) as caught:
                self.keeper(path)
            self.assertIn("not a state of chain 999", str(caught.exception))
            self.write(path, chain_id=999)
            with on(networks.TESTNET), self.assertRaises(SystemExit):
                self.keeper(path)

    def test_a_state_from_before_the_chain_was_written_is_testnets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s.json"
            self.write(path)
            with on(networks.TESTNET):
                self.assertEqual(self.keeper(path).next_block, 5)
            with on(networks.MAINNET), self.assertRaises(SystemExit):
                self.keeper(path)


class TheReadsOnMainnet(unittest.TestCase):
    def test_a_mainnet_command_checks_the_rpcs_chain_before_it_reads(self):
        with on(networks.MAINNET) as m, mock.patch.object(d, "close", return_value=0) as close:
            self.assertEqual(d.main(["close", "--pool", A, "--signer", B, "--network", "mainnet"]), 0)
        m["assert_network"].assert_called_once()
        close.assert_called_once()
        with on(networks.TESTNET) as t, mock.patch.object(d, "close", return_value=0):
            d.main(["close", "--pool", A, "--signer", B])
        t["assert_network"].assert_not_called()  # testnet as before: its sends check, its reads do not


if __name__ == "__main__":
    unittest.main()
