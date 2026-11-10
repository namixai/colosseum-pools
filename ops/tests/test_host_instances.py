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
        self.assertEqual(hi.deployment_env("v3-demo3"), DEMO3_ENV)

    def test_a_record_without_limits_says_its_addresses_alone(self):
        # demo2's record has none: its gateway.env still says them, as it did.
        text = hi.deployment_env("demo2")
        self.assertEqual([l.split("=")[0] for l in text.splitlines()],
                         ["COLOSSEUM_DEPLOYMENT", "GATEWAY_FACTORY", "GATEWAY_REGISTRY"])

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
        record = json.loads((ROOT / "deployments" / "testnet-v3-demo3.json").read_text())
        for perp in record["gateway_limits"]["perps"]:
            coin = perp["name"].split(":")[-1]
            with self.subTest(perp["name"]):
                self.assertIsNotNone(ASSET_LEVERAGE_X100.get(coin), "a market with no default has no gateway cap")
                self.assertEqual(Decimal(perp["leverage"]) * 100, ASSET_LEVERAGE_X100[coin])

    def test_a_perp_the_record_lists_is_numbered_as_the_record_numbers_it(self):
        record = json.loads((ROOT / "deployments" / "testnet-v3-demo3.json").read_text())
        listed = record["platform_assets"]
        for perp in record["gateway_limits"]["perps"]:
            if perp["name"] in listed:
                self.assertEqual(perp["asset"], listed[perp["name"]], perp["name"])

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
            self.assertEqual(len(differ), 14 if kind == "gateway" else 12, differ)
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
