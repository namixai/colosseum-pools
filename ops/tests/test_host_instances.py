"""A further instance on the pools host (the CTO, 23 Oct 2026): demo3's gateway and keeper beside demo2's,
whose live challenge keeps its own. What ops/host_instances.py reads of /etc/colosseum, the nginx it
writes, what install.sh does with both, the units, and what bootstrap.sh writes for an instance.
Offline: a temporary /etc, no systemd, no nginx."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ops import host_instances as hi

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOST = ROOT / "ops" / "host"


def etc_with(files: dict[str, str]) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    for name, text in files.items():
        (d / name).write_text(text)
    return d


DEMO3 = {"deployment": "demo2\n", "deployment.env": "COLOSSEUM_DEPLOYMENT=demo2\n",
         "deployment-demo3": "v3-demo3\n", "deployment-demo3.env": "COLOSSEUM_DEPLOYMENT=v3-demo3\n",
         "gateway-demo3.env": "GATEWAY_SIGNER=demo\nGATEWAY_BIND=127.0.0.1:8788\n"}


class Instances(unittest.TestCase):
    def test_an_instance_is_its_file_its_label_and_its_gateways_port(self):
        self.assertEqual(hi.instances(etc_with(DEMO3)), [hi.Instance("demo3", "v3-demo3", 8788)])
        self.assertEqual(hi.instances(etc_with({"deployment": "demo2\n"})), [], "the first deployment alone: none")

    def test_a_name_label_or_port_out_of_its_rules_stops_the_whole_list(self):
        bad = [
            {"deployment-v1": "x\n", "gateway-v1.env": "GATEWAY_BIND=127.0.0.1:8788\n"},           # the first's paths
            {"deployment-Demo3": "x\n", "gateway-Demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n"},
            {"deployment-demo-3": "x\n", "gateway-demo-3.env": "GATEWAY_BIND=127.0.0.1:8788\n"},
            {"deployment-demo3": "v3 demo3\n", "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n"},
            {"deployment-demo3": "v3-demo3\n"},                                                       # no settings
            {"deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_SIGNER=demo\n"},         # no port
            {"deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8787\n"},  # the first's
            {"deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_BIND=0.0.0.0:8788\n"},    # outside
            {"deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:9000\n"},
            {"deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n",
             "deployment-demo4": "v3-demo4\n", "gateway-demo4.env": "GATEWAY_BIND=127.0.0.1:8788\n"},  # one port
            {"deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n",
             "deployment-demo4": "v3-demo3\n", "gateway-demo4.env": "GATEWAY_BIND=127.0.0.1:8789\n"},  # one deployment
            {"deployment": "v3-demo3\n",
             "deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n"},  # the first's
            {"deployment": "v3-de mo3\n",  # served as v3-demo3: install.sh takes every space out
             "deployment-demo3": "v3-demo3\n", "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n"},
        ]
        for files in bad:
            with self.assertRaises(SystemExit, msg=str(files)):
                hi.instances(etc_with(files))

    def test_the_first_label_is_the_file_with_every_space_out(self):
        self.assertEqual(hi.first_label(etc_with({"deployment": "\tde mo2 \r\n"})), "demo2")
        self.assertEqual(hi.first_label(etc_with({})), "")
        out = subprocess.run([sys.executable, "-m", "ops.host_instances", "first-label",
                              str(etc_with({"deployment": "de mo2\n"}))], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (0, "demo2\n"), out.stderr)

    def test_the_deployment_env_is_the_records_addresses(self):
        record = {"PoolFactory": "0x" + "f1" * 20, "KeyRegistry": "0x" + "f2" * 20}
        with mock.patch.object(hi.deployments, "load", return_value=record) as load:
            text = hi.deployment_env("v3-demo3")
        load.assert_called_once_with("v3-demo3")
        self.assertEqual(text, f"COLOSSEUM_DEPLOYMENT=v3-demo3\nGATEWAY_FACTORY={'0x' + 'f1' * 20}\n"
                               f"GATEWAY_REGISTRY={'0x' + 'f2' * 20}\n")
        with self.assertRaises(SystemExit):
            hi.deployment_env("v3 demo3")

    def test_a_deployment_on_a_key_binder_reads_its_bindings_from_the_binder(self):
        binder = {"PoolFactory": "0x" + "f1" * 20, "KeyBinder": "0x" + "b1" * 20}
        with mock.patch.object(hi.deployments, "load", return_value=binder):
            self.assertIn(f"GATEWAY_REGISTRY={'0x' + 'b1' * 20}\n", hi.deployment_env("keybinder-1"))
        for bad in ({"PoolFactory": "0x" + "f1" * 20},
                    {**binder, "KeyRegistry": "0x" + "f2" * 20},
                    {**binder, "KeyBinder": ""}):
            with self.subTest(sorted(bad)), mock.patch.object(hi.deployments, "load", return_value=bad):
                with self.assertRaises(SystemExit):
                    hi.deployment_env("keybinder-1")

    def test_the_gateway_reads_a_binder_as_it_reads_a_registry(self):
        # The two reads the gateway makes of GATEWAY_REGISTRY (gateway/chain.py) are the same calls on both
        # contracts, with the same answer: a binder in its place is read without a change.
        chain = (ROOT / "gateway" / "chain.py").read_text()
        reads = sorted(set(re.findall(r'self\.registry, "(\w+\([\w,]*\))"', chain)))
        self.assertEqual(reads, ["bindingOf(address)", "isBound(address,address,address)"])
        for sol in ("KeyRegistry.sol", "KeyBinder.sol"):
            src = (ROOT / "src" / sol).read_text()
            self.assertIn("function bindingOf(address key) external view returns (Binding memory)", src, sol)
            self.assertIn("function isBound(address key, address account, address trader) external view returns (bool)",
                          src, sol)
            self.assertRegex(src, r"struct Binding \{\s*State state;\s*address account;\s*address trader;\s*\}", sol)

    def test_the_command_line_says_each_instance_on_a_line(self):
        etc = etc_with(DEMO3)
        out = subprocess.run([sys.executable, "-m", "ops.host_instances", "list", str(etc)],
                             cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (0, "demo3 v3-demo3 8788\n"), out.stderr)


DEMO3_ENV = (
    "COLOSSEUM_DEPLOYMENT=v3-demo3\n"
    "GATEWAY_FACTORY=0x070D0c7dd79D41e4329B2d231b308BA88089d81a\n"
    "GATEWAY_REGISTRY=0x8e2E527aed19a0969aEa49e16aE1Abbc7B84Cc85\n"
    'GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4", "750000": "0.015", "750003": "0.1"}\n'
    'GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5", "750000": "8", "750003": "20"}\n'
    "GATEWAY_PERP_DEXES=xyz\n"
    "COLOSSEUM_PERP_DEXES=xyz\n")
# demo2's gateway.env on the host, read on 11 Nov 2026 (13:44Z): BTC, ETH, SOL at the platform's sizes and caps, no
# builder dex. Its record says the same, in the same order, so the rendered lines are the host's byte for byte; the
# empty dex lines are what the gateway and the keeper already take as none.
DEMO2_ENV = (
    "COLOSSEUM_DEPLOYMENT=demo2\n"
    "GATEWAY_FACTORY=0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222\n"
    "GATEWAY_REGISTRY=0x53AF27F65Dd7473c890f633aC0025b261307779e\n"
    'GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4"}\n'
    'GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5"}\n'
    "GATEWAY_PERP_DEXES=\n"
    "COLOSSEUM_PERP_DEXES=\n")
# The keeper's nodes, demo2's and demo3's alike (11 Nov 2026): Hyperliquid's, 50 blocks a scan call, and dRPC's public
# node for a window Hyperliquid's refuses, 100 (it answers 101 at most). thirdweb's was the second until it answered the
# host's address 429 to every call.
KEEPER_ENV = (
    "COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm\n"
    "COLOSSEUM_LOG_WINDOW=50\n"
    "COLOSSEUM_RPC_BACKUP_URL=https://hyperliquid-testnet.drpc.org\n"
    "COLOSSEUM_BACKUP_LOG_WINDOW=100\n")


class GatewayLimits(unittest.TestCase):
    """The gateway's limits ride with the release, from the record (`gateway_limits`), instead of a line by hand
    in gateway-<name>.env: install.sh writes deployment-<name>.env from `deployment-env` at every deploy, and
    systemd reads it after the hand-kept file, so the record wins (10 Nov 2026)."""

    def limits(self, *perps, dexes=("xyz",)):
        return {"PoolFactory": "0x" + "f1" * 20, "KeyRegistry": "0x" + "f2" * 20,
                "gateway_limits": {"perp_dexes": list(dexes), "perps": list(perps)}}

    def test_demo3s_record_says_what_its_host_has_and_gold(self):
        # On the host, by hand, until now (10 Nov 2026): BTC, ETH, SOL and xyz:XYZ100 at these sizes and caps,
        # xyz read. xyz:GOLD comes with the record: 0.1 an order (about 400 USDC near 4,000) and 20x.
        self.assertEqual(hi.deployment_env("v3-demo3"), DEMO3_ENV + KEEPER_ENV)

    def test_demo2s_record_says_what_its_host_has(self):
        # On the host, by hand, until now (11 Nov 2026): BTC, ETH and SOL at these sizes and caps, no builder dex.
        # The record takes them over unchanged, so the gateway sees no difference after the deploy.
        self.assertEqual(hi.deployment_env("demo2"), DEMO2_ENV + KEEPER_ENV)

    def test_demo2s_lines_are_what_the_gateway_reads(self):
        from gateway.demo_signer import size_caps
        from gateway.protect import leverage_caps
        env = dict(l.split("=", 1) for l in hi.deployment_env("demo2").splitlines())
        sizes, caps = size_caps(env["GATEWAY_SIZE_CAPS"]), leverage_caps(env["GATEWAY_LEVERAGE_CAPS"])
        self.assertEqual(sorted(sizes), [0, 3, 4])
        self.assertEqual(sorted(caps), sorted(sizes))
        self.assertEqual((env["GATEWAY_PERP_DEXES"], env["COLOSSEUM_PERP_DEXES"]), ("", ""))

    def test_the_lines_are_what_the_gateway_reads(self):
        from gateway.demo_signer import size_caps
        from gateway.protect import leverage_caps
        env = dict(l.split("=", 1) for l in hi.deployment_env("v3-demo3").splitlines())
        sizes, caps = size_caps(env["GATEWAY_SIZE_CAPS"]), leverage_caps(env["GATEWAY_LEVERAGE_CAPS"])
        self.assertEqual(sorted(sizes), [0, 3, 4, 750000, 750003])
        # The gateway with the demo signer will not start while a perp has a size and no leverage cap.
        self.assertEqual(sorted(caps), sorted(sizes))

    def test_each_cap_is_the_platforms_default_for_its_market(self):
        # One number in two places: the gateway's ceiling for a market and the default ops/leverage.py gives a
        # pool's rules for it. A market the table withdraws (None) has no cap here either.
        from decimal import Decimal
        from ops.leverage import ASSET_LEVERAGE_X100
        for file in ("testnet-demo2.json", "testnet-v3-demo3.json"):
            record = json.loads((ROOT / "deployments" / file).read_text())
            for perp in record["gateway_limits"]["perps"]:
                coin = perp["name"].split(":")[-1]
                with self.subTest(file=file, perp=perp["name"]):
                    self.assertIsNotNone(ASSET_LEVERAGE_X100.get(coin), "a market with no default has no gateway cap")
                    self.assertEqual(Decimal(perp["leverage"]) * 100, ASSET_LEVERAGE_X100[coin])

    def test_a_perp_the_record_lists_is_numbered_as_the_record_numbers_it(self):
        for file in ("testnet-demo2.json", "testnet-v3-demo3.json"):
            record = json.loads((ROOT / "deployments" / file).read_text())
            listed = record["platform_assets"]
            for perp in record["gateway_limits"]["perps"]:
                if perp["name"] in listed:
                    self.assertEqual(perp["asset"], listed[perp["name"]], f"{file}: {perp['name']}")

    def test_limits_that_the_gateway_would_refuse_are_refused_here(self):
        btc = {"name": "BTC", "asset": 3, "size": "0.005", "leverage": "10"}
        gold = {"name": "xyz:GOLD", "asset": 750003, "size": "0.1", "leverage": "20"}
        bad = {
            "a builder dex not read": self.limits(btc, gold, dexes=()),
            "two perps on one number": self.limits(btc, {**btc, "name": "ETH"}),
            "one name twice": self.limits(btc, {**btc, "asset": 4}),
            "a leverage under 1x": self.limits({**btc, "leverage": "0.5"}),
            "a size of nothing": self.limits({**btc, "size": "0"}),
            "a number as JSON, not a string": self.limits({**btc, "size": 0.005}),
            "a builder dex's market on the first dex's numbers": self.limits({**gold, "asset": 10003}),
            "a first-dex market on a builder dex's numbers": self.limits({**btc, "asset": 750000}),
            "a dex name that is not one": self.limits(btc, dexes=("XYZ",)),
            "no perps": self.limits(),
            "a perp that is not an object": self.limits(btc, None),
            "limits that are not an object": {**self.limits(btc), "gateway_limits": ["xyz"]},
            "a size with a space around it": self.limits({**btc, "size": " 0.005"}),
            "a size with a tab in it": self.limits({**btc, "size": "0.005\t"}),
            "a leverage with an underscore": self.limits({**btc, "leverage": "1_0"}),
            "a leverage with an exponent": self.limits({**btc, "leverage": "1e1"}),
            "a size with no digit before the point": self.limits({**btc, "size": ".5"}),
        }
        for why, record in bad.items():
            with self.subTest(why), mock.patch.object(hi.deployments, "load", return_value=record):
                with self.assertRaises(SystemExit):
                    hi.deployment_env("v3-demo3")
        with mock.patch.object(hi.deployments, "load", return_value=self.limits(btc, gold)):
            self.assertIn('GATEWAY_LEVERAGE_CAPS={"3": "10", "750003": "20"}', hi.deployment_env("v3-demo3"))

    def test_the_records_file_is_read_last_so_it_wins_over_a_line_by_hand(self):
        for unit, own in (("colosseum-gateway.service", "gateway.env"), ("colosseum-keeper.service", "keeper.env"),
                          ("colosseum-gateway@.service", "gateway-%i.env"), ("colosseum-keeper@.service", "keeper-%i.env")):
            envs = [l for l in (HOST / unit).read_text().splitlines() if l.startswith("EnvironmentFile=")]
            record = "deployment-%i.env" if "@" in unit else "deployment.env"
            with self.subTest(unit):
                self.assertEqual(envs, [f"EnvironmentFile=/etc/colosseum/{own}", f"EnvironmentFile=/etc/colosseum/{record}"])


class KeeperNodes(unittest.TestCase):
    """The keeper's nodes ride with the release, from the record (`keeper_nodes`), as the gateway's limits do: the
    node it reads, its scan's window, and the second node a refused window goes to (11 Nov 2026). keeper.env's
    COLOSSEUM_RPC_URL by hand is overridden: systemd reads deployment-*.env after it."""

    def record(self, nodes):
        return {"PoolFactory": "0x" + "f1" * 20, "KeyRegistry": "0x" + "f2" * 20, "keeper_nodes": nodes}

    def env(self, nodes):
        with mock.patch.object(hi.deployments, "load", return_value=self.record(nodes)):
            return hi.deployment_env("v3-demo3")

    def test_demo2_and_demo3_name_hyperliquids_node_and_drpcs(self):
        for label in ("demo2", "v3-demo3"):
            with self.subTest(label):
                self.assertTrue(hi.deployment_env(label).endswith(KEEPER_ENV))

    def test_a_record_without_them_leaves_keeper_env_as_it_is(self):
        with mock.patch.object(hi.deployments, "load", return_value={"PoolFactory": "0x" + "f1" * 20,
                                                                     "KeyRegistry": "0x" + "f2" * 20}):
            self.assertNotIn("COLOSSEUM_RPC", hi.deployment_env("v3-demo3"))

    def test_a_primary_alone_says_no_second_node(self):
        hl = {"url": "https://rpc.hyperliquid-testnet.xyz/evm", "log_window": 50}
        for nodes in ({"primary": hl}, {"primary": hl, "backup": None}):
            text = self.env(nodes)
            self.assertIn("COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm\n", text)
            self.assertNotIn("BACKUP", text)

    def test_nodes_the_keeper_could_not_take_are_refused_before_anything_is_written(self):
        hl = {"url": "https://rpc.hyperliquid-testnet.xyz/evm", "log_window": 50}
        tw = {"url": "https://998.rpc.thirdweb.com", "log_window": 1000}
        bad = {
            "no primary": {"backup": tw},
            "a primary of null": {"primary": None},
            "a primary of null beside a backup": {"primary": None, "backup": tw},
            "a third node": {"primary": hl, "backup": tw, "spare": tw},
            "not an object": ["https://998.rpc.thirdweb.com"],
            "a key in the address": {"primary": hl, "backup": {**tw, "url": "https://node.example/evm?key=abc"}},
            "a key after a hash": {"primary": {**hl, "url": "https://node.example/evm#key"}},
            "a port that is no number": {"primary": {**hl, "url": "https://node.example:key/evm"}},
            "plain http": {"primary": {**hl, "url": "http://rpc.hyperliquid-testnet.xyz/evm"}},
            "a space": {"primary": {**hl, "url": "https://rpc.hyperliquid-testnet.xyz/evm x"}},
            "a quote": {"primary": {**hl, "url": 'https://rpc.hyperliquid-testnet.xyz/"evm'}},
            "a window over Hyperliquid's 50": {"primary": {**hl, "log_window": 51}},
            "a second window over thirdweb's 1000": {"primary": hl, "backup": {**tw, "log_window": 1001}},
            "a window of nothing": {"primary": {**hl, "log_window": 0}},
            "a window as a string": {"primary": {**hl, "log_window": "50"}},
            "a window as true": {"primary": {**hl, "log_window": True}},
            "a field of its own": {"primary": {**hl, "timeout": 5}},
            "the backup is the primary": {"primary": hl, "backup": {**tw, "url": hl["url"] + "/"}},
        }
        for what, nodes in bad.items():
            with self.subTest(what), self.assertRaises(SystemExit):
                self.env(nodes)

    def test_the_lines_are_what_the_keeper_reads(self):
        keeper_py = (ROOT / "ops" / "keeper.py").read_text()
        for name in ("COLOSSEUM_LOG_WINDOW", "COLOSSEUM_RPC_BACKUP_URL", "COLOSSEUM_BACKUP_LOG_WINDOW"):
            self.assertIn(f'os.environ.get("{name}"', keeper_py)
        self.assertIn('os.environ.get("COLOSSEUM_RPC_URL"', (ROOT / "spike" / "hlspike" / "common.py").read_text())


class Nginx(unittest.TestCase):
    """The locations install.sh puts in the site, by the same awk install.sh runs."""

    def render(self, locations: str, upstreams: str = "") -> str:
        install = (HOST / "install.sh").read_text()
        program = re.search(r"""awk '\n(.*?)' "\$rel/ops/host/nginx-pools-api\.conf\.in" > "\$site\"""", install, re.S).group(1)
        env = {**os.environ, "REAL_IP": "    set_real_ip_from CLOUDFLARE-RANGE;", "LOCATIONS": locations,
               "UPSTREAMS": upstreams}
        return subprocess.run(["awk", program, str(HOST / "nginx-pools-api.conf.in")], env=env,
                              capture_output=True, text=True, check=True).stdout

    def test_an_instance_is_reached_at_its_name_on_its_own_gateways_port(self):
        found = [hi.Instance("demo3", "v3-demo3", 8788)]
        site = self.render(hi.nginx_locations(found), hi.nginx_upstreams(found))
        # Its upstream: its own gateway first, every time, and nginx's "restarting" answer behind it.
        self.assertIn("upstream pools_gateway_demo3 {\n    server 127.0.0.1:8788 max_fails=0;\n"
                      f"    server {hi.RESTARTING} backup;\n}}", site[:site.index("server {")])
        server = site[site.index("server_name pools-api.usenami.io;"):]
        for path, upstream in (("/demo3/v1/order", "http://pools_gateway_demo3/v1/order"),
                               ("/demo3/v1/health", "http://pools_gateway_demo3/v1/health"),
                               ("/demo3/v1/faucet", "http://pools_gateway_demo3/v1/faucet"),
                               ("/demo3/v1/contacts", "http://pools_gateway_demo3/v1/contacts")):
            at = server.index(f"location = {path} {{")
            self.assertIn(f"proxy_pass {upstream};", server[at:server.index("}", at)])
            self.assertLess(at, server.index("location / {"), "inside the API's server, before its 404")
        # The first gateway's paths stay its own, and the zones are the ones the first uses: shared.
        self.assertIn("location = /v1/order {\n        limit_req zone=pools_ip burst=10 nodelay;", server)
        order = server[server.index("location = /demo3/v1/order"):]
        self.assertIn("limit_req zone=pools_ip burst=10 nodelay;\n        limit_req zone=pools_all burst=10 nodelay;", order)
        self.assertNotIn("@INSTANCE_LOCATIONS@", site)
        self.assertNotIn("@CLOUDFLARE_REAL_IP_FROM@", site)
        self.assertIn("set_real_ip_from CLOUDFLARE-RANGE;", site)

    def test_with_no_instance_the_site_is_the_first_gateways_alone(self):
        site = self.render(hi.nginx_locations([]), hi.nginx_upstreams([]))
        self.assertEqual(site.count("location ="), 4)
        self.assertEqual(len(re.findall(r"^upstream \S+ \{", site, re.M)), 1)
        self.assertNotIn("@INSTANCE_LOCATIONS@", site)
        self.assertNotIn("@INSTANCE_UPSTREAMS@", site)

    def test_past_a_limit_nginx_answers_in_json_that_nothing_was_sent(self):
        """nginx's own 429 was an HTML page: agents/desk.py could not read it and counted the order as spent."""
        found = [hi.Instance("demo3", "v3-demo3", 8788)]
        site = self.render(hi.nginx_locations(found), hi.nginx_upstreams(found))
        directives = "\n".join(l for l in site.splitlines() if not l.lstrip().startswith("#"))
        server = directives[directives.index("server_name pools-api.usenami.io;"):]
        self.assertIn("limit_req_status 429;", directives)
        self.assertIn("\n    error_page 429 @limited;\n", server, "for the whole API server, not one location")
        self.assertEqual(directives.count("error_page"), 1, "no location answers a limit its own way")
        # A gateway's own 429 (restarting, busy) passes through: nginx replaces only its own answers.
        self.assertNotIn("proxy_intercept_errors", directives)
        at = server.index("location @limited {")
        limited = server[at:server.index("\n    }", at)]
        self.assertIn("default_type application/json;", limited)
        self.assertIn("add_header Retry-After 60 always;", limited)
        # The site calls from its own origin: without the gateway's CORS headers the browser hides the body.
        boot = (HOST / "bootstrap.sh").read_text()
        origins = set(re.findall(r"^GATEWAY_ALLOW_ORIGIN=(\S+)$", boot, re.M))
        self.assertEqual(len(origins), 1, origins)
        origin = origins.pop()
        self.assertIn(f"add_header Access-Control-Allow-Origin {origin} always;", limited)
        self.assertIn("add_header Vary Origin always;", limited)
        restart = directives[directives.index(f"listen {hi.RESTARTING};"):]
        restart = restart[:restart.index("\n}") + 2]
        self.assertIn(f"add_header Access-Control-Allow-Origin {origin} always;", restart,
                      "the restart's answer is read by the same page")
        answer = re.search(r"return 429 '(\{.*\})\\n';", limited)
        self.assertIsNotNone(answer, "429, whose body the CDN passes; a 5xx body it replaces")
        body = json.loads(answer.group(1))
        # What agents/desk.py gives back on: `sent` False, or the status busy.
        self.assertEqual((body["status"], body["code"], body["sent"], body["retry_after_s"]),
                         ("busy", "rate_limited", False, 60))
        self.assertIn("nothing was sent", body["detail"])
        desk = (ROOT / "agents" / "desk.py").read_text()
        self.assertIn('answer.get("sent") is False or answer.get("status") == "busy"', desk)
        # Every path with a limit is in that server.
        for path in ("/v1/order", "/v1/health", "/v1/faucet", "/v1/contacts", "/demo3/v1/order"):
            loc = server.index(f"location = {path} {{")
            self.assertIn("limit_req zone=", server[loc:server.index("}", loc)])

    def test_while_a_gateway_restarts_nginx_answers_that_nothing_was_sent(self):
        """26 Oct 2026: a trader's order met nginx's 502 in the 17 s the gateway took to listen again."""
        site = self.render("", "")
        self.assertIn("upstream pools_gateway {\n    server 127.0.0.1:8787 max_fails=0;\n"
                      f"    server {hi.RESTARTING} backup;\n}}", site)
        server = site[site.index("server_name pools-api.usenami.io;"):]
        for path in ("/v1/order", "/v1/health"):
            at = server.index(f"location = {path} {{")
            self.assertIn("proxy_pass http://pools_gateway;", server[at:server.index("}", at)])
        # Never a POST passed on once a gateway has it: that one may be on Hyperliquid.
        directives = "\n".join(l for l in site.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("non_idempotent", directives)
        restart = site[site.index(f"listen {hi.RESTARTING};"):]
        restart = restart[:restart.index("\n}") + 2]
        self.assertIn("add_header Retry-After 30 always;", restart)
        answer = re.search(r"return 429 '(\{.*\})\\n';", restart)
        self.assertIsNotNone(answer, "429, whose body the CDN passes; a 5xx body it replaces")
        body = json.loads(answer.group(1))
        self.assertEqual((body["status"], body["code"], body["sent"], body["retry_after_s"]),
                         ("busy", "gateway_restarting", False, 30))
        # The check after a deploy says so, rather than that the gateway names no commit.
        saved = pathlib.Path(tempfile.mkdtemp()) / "health.json"
        saved.write_text(answer.group(1) + "\n")
        out = subprocess.run(["bash", str(ROOT / "scripts" / "verify-gateway-deploy.sh"), saved.as_uri()],
                             env={**os.environ, "PYTHON": sys.executable}, capture_output=True, text=True)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("FAIL the gateway is restarting", out.stdout)


class Install(unittest.TestCase):
    def test_install_routes_and_restarts_each_instance_and_stops_on_a_bad_list(self):
        install = (HOST / "install.sh").read_text()
        for unit in ("colosseum-gateway@.service", "colosseum-keeper@.service"):
            self.assertIn(f'install -m 0644 -o root -g root "$rel/ops/host/{unit}" /etc/systemd/system/', install)
        listing = install.index('-m ops.host_instances list /etc/colosseum')
        self.assertLess(listing, install.index('> "$site"'), "the list is read before nginx is touched")
        self.assertIn('|| fail "the instances in /etc/colosseum are not as ops/host_instances.py reads them', install)
        self.assertIn('deployment_env "$label" "/etc/colosseum/deployment-$name.env"', install)
        self.assertIn('units+=("colosseum-gateway@$name" "colosseum-keeper@$name")', install)
        subprocess.run(["bash", "-n", str(HOST / "install.sh")], check=True)

    def test_every_deployments_record_is_checked_before_nginx_routes_to_it(self):
        install = (HOST / "install.sh").read_text()
        check = install.index("for label in $first_label $(awk '{print $2}' <<< \"$instances\"); do")
        self.assertLess(install.index('-m ops.host_instances list /etc/colosseum'), check)
        self.assertLess(check, install.index('> "$site"'), "a label with no record stops the release before nginx")
        loop = install[check:install.index("done", check)]
        self.assertIn('-m ops.host_instances deployment-env "$label") > /dev/null', loop)
        self.assertIn('|| fail "deployment $label: no complete testnet record in this release; nothing switched"', loop)
        self.assertIn('deployment_env "$first_label" /etc/colosseum/deployment.env', install)
        # The label served is the one the instances were checked against: one reader, ops/host_instances.py.
        read = install.index('first_label="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances first-label /etc/colosseum)"')
        self.assertLess(read, check)
        self.assertNotIn("< /etc/colosseum/deployment", install)


class Units(unittest.TestCase):
    @staticmethod
    def directives(path: pathlib.Path) -> list[str]:
        text = path.read_text().replace("\\\n", " ")
        return [l for l in text.splitlines() if l and not l.startswith("#")]

    def test_an_instances_units_are_the_firsts_with_their_own_files_and_keys(self):
        for kind, env, keys_first, keys_instances in (
                ("gateway", "gateway", "-/var/lib/colosseum-gw/keys", "-/var/lib/colosseum-gw/instances"),
                ("keeper", "keeper", "-/var/lib/colosseum-keeper/secrets", "-/var/lib/colosseum-keeper/instances")):
            first = self.directives(HOST / f"colosseum-{kind}.service")
            at = self.directives(HOST / f"colosseum-{kind}@.service")
            differ = sorted(set(first) ^ set(at))
            # Description, the two env files, InaccessiblePaths: each side; and the instance's own four below.
            # A gateway's has its own state directories besides (gateway/faucet.py, gateway/contacts.py).
            # A keeper's has, besides, where its test publisher makes keys, when the record names one.
            self.assertEqual(len(differ), 14, differ)
            self.assertIn("ProtectProc=invisible", at)
            self.assertIn("ProcSubset=pid", at)
            # Every instance runs as one user, so each sees its own directory alone under instances/.
            self.assertIn(f"TemporaryFileSystem={keys_instances[1:]}:ro", at)
            self.assertIn(f"BindReadOnlyPaths={keys_instances[1:]}/%i", at)
            self.assertIn(f"EnvironmentFile=/etc/colosseum/deployment-%i.env", at)
            self.assertIn(f"EnvironmentFile=/etc/colosseum/{env}-%i.env", at)
            inaccessible = [l for l in at if l.startswith("InaccessiblePaths=")]
            self.assertEqual(len(inaccessible), 1)
            self.assertIn(keys_first, inaccessible[0], "an instance does not read the first's keys")
            first_inaccessible = [l for l in first if l.startswith("InaccessiblePaths=")][0]
            self.assertIn(keys_instances, first_inaccessible, "the first does not read an instance's")
        keeper = [l for l in self.directives(HOST / "colosseum-keeper@.service") if l.startswith("ExecStart=")]
        self.assertEqual(keeper, [l for l in self.directives(HOST / "colosseum-keeper.service") if l.startswith("ExecStart=")],
                         "the same keeper, its state by its deployment's label")


class Faucet(unittest.TestCase):
    """The faucet's path (gateway/faucet.py): a network's address goes to the gateway, which limits grants by
    it, and the instance's gateway may write the day's grants and nothing else."""

    render = Nginx.render

    def test_the_faucet_is_limited_harder_than_orders_and_told_the_client(self):
        found = [hi.Instance("demo3", "v3-demo3", 8788)]
        site = self.render(hi.nginx_locations(found), hi.nginx_upstreams(found))
        self.assertIn("limit_req_zone $binary_remote_addr zone=pools_faucet_ip:1m rate=1r/m;", site)
        self.assertIn("limit_req_zone $server_name zone=pools_faucet_all:1m rate=2r/m;", site)
        for path, upstream in (("/v1/faucet", "http://pools_gateway"), ("/demo3/v1/faucet", "http://pools_gateway_demo3/v1/faucet")):
            at = site.index(f"location = {path} {{")
            block = site[at:site.index("}", at)]
            self.assertIn(f"proxy_pass {upstream};", block)
            self.assertIn("limit_req zone=pools_faucet_ip burst=1 nodelay;\n        limit_req zone=pools_faucet_all burst=2 nodelay;", block)
            self.assertIn("proxy_set_header X-Real-IP $remote_addr;", block)
            self.assertIn("proxy_read_timeout 60s;", block)
        # Orders never pass a client address the gateway would trust.
        order = site[site.index("location = /v1/order {"):]
        self.assertNotIn("X-Real-IP", order[:order.index("}")])

    def test_each_gateway_writes_in_its_own_state_directories_alone(self):
        at = Units.directives(HOST / "colosseum-gateway@.service")
        # The faucet's first: gateway/faucet.py takes the first of $STATE_DIRECTORY that is not the contacts'.
        self.assertIn("StateDirectory=colosseum-gw/faucet-%i colosseum-gw/contacts-%i", at)
        self.assertIn("StateDirectoryMode=0700", at)
        self.assertIn("ProtectSystem=strict", at)
        # The first deployment's gateway keeps its pools' contacts and nothing else (the CTO, 28 Oct 2026):
        # the site reads it, and its faucet, if it had one, would not write there.
        first = Units.directives(HOST / "colosseum-gateway.service")
        self.assertEqual([l for l in first if l.startswith(("StateDirectory", "ReadWritePaths"))],
                         ["StateDirectory=colosseum-gw/contacts", "StateDirectoryMode=0700"])
        self.assertIn("ProtectSystem=strict", first)


class Contacts(unittest.TestCase):
    """Pool owners' contacts (gateway/contacts.py): a page's read is cheap and limited by a network, an
    owner's write is limited apart and harder, and no client address goes to the gateway."""

    render = Nginx.render

    def test_reads_and_writes_are_limited_apart(self):
        found = [hi.Instance("demo3", "v3-demo3", 8788)]
        site = self.render(hi.nginx_locations(found), hi.nginx_upstreams(found))
        self.assertIn("map $request_method $pools_contacts_writer {\n    POST    $binary_remote_addr;\n"
                      '    default "";\n}', site)
        self.assertIn("limit_req_zone $binary_remote_addr zone=pools_contacts_ip:1m rate=60r/m;", site)
        self.assertIn("limit_req_zone $pools_contacts_writer zone=pools_contacts_write:1m rate=2r/m;", site)
        # Everyone's writes together too: each may cost the node a read out of the orders' budget.
        self.assertIn('map $request_method $pools_contacts_any_writer {\n    POST    contacts;\n'
                      '    default "";\n}', site)
        self.assertIn("limit_req_zone $pools_contacts_any_writer zone=pools_contacts_write_all:1m rate=6r/m;", site)
        for path, upstream in (("/v1/contacts", "http://pools_gateway"),
                               ("/demo3/v1/contacts", "http://pools_gateway_demo3/v1/contacts")):
            at = site.index(f"location = {path} {{")
            block = site[at:site.index("}", at)]
            self.assertIn(f"proxy_pass {upstream};", block)
            self.assertIn("limit_req zone=pools_contacts_ip burst=20 nodelay;\n"
                          "        limit_req zone=pools_contacts_write burst=2 nodelay;\n"
                          "        limit_req zone=pools_contacts_write_all burst=3 nodelay;", block)
            self.assertNotIn("X-Real-IP", block)
            self.assertNotIn("pools_all", block, "a page's reads do not spend the orders' shared budget")


class TestPublisher(unittest.TestCase):
    """A deployment on a key binder with a test publisher, a key in a file before the enclave (ops/key_relay.py):
    what its record makes deployment-env say, and what install.sh, the keeper's unit and bootstrap.sh do with it."""

    ADDR = "0x" + "a1" * 20

    def record(self, publisher=None, binder=True) -> dict:
        r = {"PoolFactory": "0x" + "f1" * 20}
        if binder:
            r["KeyBinder"] = "0x" + "b1" * 20
        else:
            r["KeyRegistry"] = "0x" + "f2" * 20
        r["key_binder"] = {"publisher": publisher}
        return r

    def env(self, record: dict) -> str:
        with mock.patch.object(hi.deployments, "load", return_value=record):
            return hi.deployment_env("kb1")

    def test_a_test_publisher_has_the_keeper_answer_and_the_gateway_start_empty(self):
        r = self.record({"address": self.ADDR, "test_key": True})
        self.assertEqual(hi.publisher(r), self.ADDR)
        self.assertTrue(self.env(r).endswith("GATEWAY_KEYS_BOUND_LATER=1\nCOLOSSEUM_PUBLISHER_WALLET=publisher\n"))

    def test_an_enclave_publisher_or_none_leaves_the_keeper_without_a_wallet(self):
        for pub in ({"address": self.ADDR, "test_key": False}, None):
            with self.subTest(pub=pub):
                r = self.record(pub)
                self.assertEqual(hi.publisher(r), "")
                self.assertTrue(self.env(r).endswith("GATEWAY_KEYS_BOUND_LATER=1\n"))

    def test_a_deployment_before_the_binder_says_neither(self):
        r = self.record({"address": self.ADDR, "test_key": True}, binder=False)
        self.assertEqual(hi.publisher(r), "")
        text = self.env(r)
        self.assertNotIn("GATEWAY_KEYS_BOUND_LATER", text)
        self.assertNotIn("COLOSSEUM_PUBLISHER_WALLET", text)
        for label in ("demo2", "v3-demo3"):
            text = hi.deployment_env(label)
            self.assertNotIn("GATEWAY_KEYS_BOUND_LATER", text, label)
            self.assertNotIn("COLOSSEUM_PUBLISHER_WALLET", text, label)

    def test_the_command_says_the_publisher_or_an_empty_line(self):
        out = subprocess.run([sys.executable, "-m", "ops.host_instances", "publisher", "v3-demo3"],
                             capture_output=True, text=True, cwd=ROOT, check=True)
        self.assertEqual(out.stdout, "\n")
        bad = subprocess.run([sys.executable, "-m", "ops.host_instances", "publisher", "v3 demo3"],
                             capture_output=True, text=True, cwd=ROOT)
        self.assertNotEqual(bad.returncode, 0)

    def test_the_lines_are_what_the_keeper_and_the_gateway_read(self):
        keeper = (ROOT / "ops" / "keeper.py").read_text()
        self.assertIn('os.environ.get("COLOSSEUM_PUBLISHER_WALLET", "")', keeper)
        self.assertIn('os.environ.get("GATEWAY_KEYS_DIR", "")', keeper)
        self.assertIn('env.get("GATEWAY_KEYS_BOUND_LATER") == "1"', (ROOT / "gateway" / "server.py").read_text())

    def test_install_checks_the_publishers_wallet_before_it_switches_and_watches_its_keys_after(self):
        install = (HOST / "install.sh").read_text()
        for unit in ("colosseum-key-copy@.service", "colosseum-key-copy@.path"):
            self.assertIn(f'install -m 0644 -o root -g root "$rel/ops/host/{unit}" /etc/systemd/system/', install)
        check = install.index('-m ops.host_instances publisher "$label"')
        switch = install.index('ln -sfn "releases/$sha"')
        enable = install.index('systemctl enable --quiet --now "colosseum-key-copy@$name.path"')
        restart = install.index('for unit in "${units[@]}"; do')
        self.assertLess(check, install.index("site=/etc/nginx/conf.d/pools-api.conf"), "before nginx or a unit")
        self.assertLess(switch, enable)
        self.assertLess(enable, restart, "watched before the keepers restart and make keys")
        self.assertIn('systemctl start "colosseum-key-copy@$name.service"', install)
        guard = install[check:install.index('publishers+="$name "')]
        for need in ('"$own/secrets/publisher.key"', '"$own/made-keys"', '"$own/secrets/publisher.addr"',
                     '[ "$(lower <<< "$had")" = "$(lower <<< "$publisher")" ]', "nothing switched"):
            self.assertIn(need, guard)
        first = install[install.index('-m ops.host_instances publisher "$first_label"'):check]
        self.assertIn("serve it as an instance", first)
        self.assertIn(f"secrets/{hi.PUBLISHER_WALLET}.key", install)

    def test_the_keeper_makes_keys_where_the_copier_reads_them(self):
        sys.path.insert(0, str(HOST))
        import key_copy
        d = Units.directives(HOST / "colosseum-keeper@.service")
        self.assertIn(f"BindPaths=-{key_copy.made_keys('%i')}", d)
        self.assertIn(f"Environment=GATEWAY_KEYS_DIR={key_copy.made_keys('%i')}", d)
        boot = (HOST / "bootstrap.sh").read_text()
        self.assertIn('"/var/lib/colosseum-keeper/instances/$instance/made-keys"', boot)
        self.assertEqual(key_copy.made_keys("$instance"), "/var/lib/colosseum-keeper/instances/$instance/made-keys")


class MultiPoolKeeper(unittest.TestCase):
    """A deployment whose record names a MultiPool: its keeper's unit, what install.sh checks and enables, and the
    line its watchdog reads."""

    POOL = "0x" + "c1" * 20

    def test_the_record_names_the_pool_for_the_watchdog_and_the_command(self):
        record = {"PoolFactory": "0x" + "f1" * 20, "KeyRegistry": "0x" + "f2" * 20, "MultiPool": self.POOL}
        with mock.patch.object(hi.deployments, "load", return_value=record):
            self.assertTrue(hi.deployment_env("multi-1").endswith(f"COLOSSEUM_MULTI_POOL={self.POOL}\n"))
        self.assertEqual(hi.multi_pool(record), self.POOL)
        for label in ("demo2", "v3-demo3"):
            self.assertNotIn("COLOSSEUM_MULTI_POOL", hi.deployment_env(label), label)
            self.assertEqual(hi.multi_pool(hi.deployments.load(label)), "", label)
        out = subprocess.run([sys.executable, "-m", "ops.host_instances", "multi", "v3-demo3"],
                             capture_output=True, text=True, cwd=ROOT, check=True)
        self.assertEqual(out.stdout, "\n")
        self.assertNotEqual(subprocess.run([sys.executable, "-m", "ops.host_instances", "multi", "v3 demo3"],
                                           capture_output=True, cwd=ROOT).returncode, 0)

    def test_its_unit_is_the_instance_keepers_with_its_own_program_wallet_and_state(self):
        keeper = Units.directives(HOST / "colosseum-keeper@.service")
        multi = Units.directives(HOST / "colosseum-multi-keeper@.service")
        differ = sorted(set(keeper) ^ set(multi))
        self.assertEqual([l.split("=", 1)[0] for l in differ], ["Description", "Description", "ExecStart", "ExecStart"],
                         differ)
        start = next(l for l in multi if l.startswith("ExecStart="))
        self.assertIn("-m ops.multi_keeper --deployment ${COLOSSEUM_DEPLOYMENT}", start)
        self.assertIn(f"--wallet {hi.MULTI_WALLET} ", start)
        self.assertIn("--state /var/lib/colosseum-keeper/multi-${COLOSSEUM_DEPLOYMENT}.json", start)
        self.assertNotIn("--wallet keeper", start, "never the instance keeper's key: one key, two nonces")

    def test_install_checks_its_wallet_before_it_switches_and_enables_it_after(self):
        install = (HOST / "install.sh").read_text()
        self.assertIn('install -m 0644 -o root -g root "$rel/ops/host/colosseum-multi-keeper@.service" /etc/systemd/system/',
                      install)
        check = install.index('-m ops.host_instances multi "$label"')
        guard = install[check:install.index('multis+="$name "')]
        self.assertIn(f'"$own/secrets/{hi.MULTI_WALLET}.key"', guard)
        self.assertIn("nothing switched", guard)
        first = install[install.index('-m ops.host_instances multi "$first_label"'):check]
        self.assertIn("serve it as an instance", first)
        self.assertLess(check, install.index("site=/etc/nginx/conf.d/pools-api.conf"))
        enable = install.index('systemctl enable --quiet "colosseum-multi-keeper@$name"')
        self.assertLess(install.index('ln -sfn "releases/$sha"'), enable)
        self.assertLess(enable, install.index('for unit in "${units[@]}"; do'))
        self.assertIn('for name in $multis; do units+=("colosseum-multi-keeper@$name"); done', install)
        self.assertLess(install.index('multis+="$name "'), install.index('watch_multis="$multis"'))
        self.assertLess(install.index('watch_multis="$multis"'), install.index('watchdogs_install "$rel"'))

    def test_the_multipool_keeper_reads_the_records_node_window(self):
        src = (ROOT / "ops" / "multi_keeper.py").read_text()
        self.assertIn('default=os.environ.get("COLOSSEUM_LOG_WINDOW", str(MAX_LOG_WINDOW))', src)
        self.assertIn("COLOSSEUM_LOG_WINDOW", hi.KEEPER_NODE_ENV["primary"])


class Bootstrap(unittest.TestCase):
    def env_of(self, name: str) -> list[str]:
        boot = (HOST / "bootstrap.sh").read_text()
        return re.search(rf'"/etc/colosseum/{name}-\$instance\.env" <<EOF\n(.*?)\nEOF', boot, re.S).group(1).splitlines()

    def test_an_instances_gateway_has_its_keys_its_port_and_xyz100(self):
        env = self.env_of("gateway")
        self.assertIn("GATEWAY_KEYS_DIR=/var/lib/colosseum-gw/instances/$instance/keys", env)
        self.assertIn("GATEWAY_BIND=127.0.0.1:$instance_port", env)
        self.assertIn("GATEWAY_PERP_DEXES=xyz", env)
        # The CTO's for demo3: 0.015 of xyz:XYZ100 (750000 on testnet) an order, 8x.
        self.assertIn('GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4", "750000": "0.015"}', env)
        self.assertIn('GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5", "750000": "8"}', env)

    def test_an_instances_keeper_sends_with_its_own_key(self):
        env = self.env_of("keeper")
        self.assertIn("COLOSSEUM_KEY_DIR=/var/lib/colosseum-keeper/instances/$instance/secrets", env)
        self.assertIn("COLOSSEUM_PERP_DEXES=xyz", env)
        self.assertIn("COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm", env)

    def test_an_instance_out_of_the_rules_is_refused_before_anything_is_done(self):
        for args in (["--instance", "v1", "v3-demo3", "8788"], ["--instance", "Demo3", "v3-demo3", "8788"],
                     ["--instance", "demo3", "v3 demo3", "8788"], ["--instance", "demo3", "v3-demo3", "8787"],
                     ["--instance", "demo3", "v3-demo3", "9000"], ["--other"]):
            out = subprocess.run(["bash", str(HOST / "bootstrap.sh"), *args], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2, (args, out.stderr))
            self.assertNotIn("bootstrap: python3.12", out.stdout, "nothing was installed or checked first")


if __name__ == "__main__":
    unittest.main()
