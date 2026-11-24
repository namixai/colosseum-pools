"""A host for mainnet's simulated pools (24 Nov 2026): one host serves one network, named in /etc/colosseum/host.env;
a mainnet host serves one simulated pool's instance and nothing of testnet's. What ops/host_instances.py reads of the
host for that, what install.sh and bootstrap.sh do with it. Offline: a temporary /etc and a temporary folder of
records, no systemd, no nginx, no node."""

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

from ops import deployments
from ops import host_instances as hi

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOST = ROOT / "ops" / "host"

FILLER, CHECKER = "0x" + "f1" * 20, "0x" + "c1" * 20
# A simulated pool's record on mainnet, as ops/deploy_sim.py writes one: the node in it is the network's public one.
SIM = {"chain_id": 999, "network": "mainnet", "label": "sim1", "status": "complete", "block": 1,
       "SimPoolFactory": "0x" + "ab" * 20, "filler": FILLER, "checker": CHECKER,
       "keeper_nodes": {"primary": {"url": "https://rpc.hyperliquid.xyz/evm", "log_window": 50}}}
KEEPER_ENV = ("COLOSSEUM_KEY_DIR=/var/lib/colosseum-keeper/instances/sim1/secrets\n"
              "COLOSSEUM_NETWORK=mainnet\n"
              "COLOSSEUM_CONFIRM_MAINNET=sim1\n")
MAINNET_HOST = {"host.env": "COLOSSEUM_NETWORK=mainnet\n", "deployment-sim1": "sim1\n", "keeper-sim1.env": KEEPER_ENV}


def etc_with(files: dict[str, str]) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    for name, text in files.items():
        (d / name).write_text(text)
    return d


class Records:
    """A folder of records of the test's own in place of the repository's, and the network the environment names."""

    def __init__(self, network: str, records: dict[str, dict]):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "deployments").mkdir()
        for name, record in records.items():
            (self.root / "deployments" / f"{name}.json").write_text(json.dumps(record))
        self.patches = [mock.patch.object(deployments, "ROOT", self.root),
                        mock.patch.dict(os.environ, {"COLOSSEUM_NETWORK": network})]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def mainnet(**more) -> Records:
    return Records("mainnet", {"mainnet-sim1": SIM, **more})


class TheHostsNetwork(unittest.TestCase):
    """One place names the network a host serves: /etc/colosseum/host.env. No file is testnet, as every host before."""

    GOOD = {"COLOSSEUM_NETWORK=mainnet\n": "mainnet", "COLOSSEUM_NETWORK=testnet\n": "testnet",
            "# the one network this host serves\nCOLOSSEUM_NETWORK=mainnet\n\n": "mainnet",
            "COLOSSEUM_NETWORK=mainnet": "mainnet"}
    BAD = ["COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_NETWORK=testnet\n", "COLOSSEUM_NETWORK=main\n", "COLOSSEUM_NETWORK=\n",
           "COLOSSEUM_NETWORK=\"mainnet\"\n", "COLOSSEUM_NETWORK = mainnet\n", " COLOSSEUM_NETWORK=mainnet\n",
           "COLOSSEUM_NETWORK=mainnet # the host\n", "COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_RPC_URL=https://x\n",
           "NETWORK=mainnet\n", "", "# nothing\n", "COLOSSEUM_NETWORK=MAINNET\n"]

    def test_a_host_without_the_file_is_a_testnet_host(self):
        self.assertEqual(hi.host_network(etc_with({})).name, "testnet")
        self.assertEqual(hi.host_network(etc_with({"deployment": "demo2\n", "keeper.env": "COLOSSEUM_RPC_URL=x\n"})).name,
                         "testnet")

    def test_the_file_names_it_on_one_line_and_anything_else_stops(self):
        for text, name in self.GOOD.items():
            self.assertEqual(hi.host_network(etc_with({"host.env": text})).name, name, text)
        for text in self.BAD:
            with self.subTest(text), self.assertRaises(SystemExit) as stop:
                hi.host_network(etc_with({"host.env": text}))
            self.assertIn("names the host's network on one line of its own", str(stop.exception))

    def bootstrap_reads(self, env: pathlib.Path) -> tuple[int, str]:
        """bootstrap.sh's own reader, the function as the script has it, on a file of the test's."""
        boot = (HOST / "bootstrap.sh").read_text()
        function = re.search(r"^host_network\(\) \{\n.*?^\}\n", boot, re.S | re.M).group(0)
        out = subprocess.run(["bash", "-c", f'set -euo pipefail\nhost_env="$1"\n{function}host_network', "bash", str(env)],
                             capture_output=True, text=True)
        return out.returncode, out.stdout.strip()

    def test_bootstrap_reads_the_file_as_install_does(self):
        # Two readers of one file, the shell's before there is a python on the host: held to the same answers.
        self.assertEqual(self.bootstrap_reads(etc_with({}) / "host.env"), (0, "testnet"))
        for text, name in self.GOOD.items():
            self.assertEqual(self.bootstrap_reads(etc_with({"host.env": text}) / "host.env"), (0, name), text)
        for text in self.BAD:
            code, said = self.bootstrap_reads(etc_with({"host.env": text}) / "host.env")
            self.assertEqual((code != 0, said), (True, ""), text)

    def test_the_command_says_it_for_install(self):
        for files, name in (({}, "testnet"), ({"host.env": "COLOSSEUM_NETWORK=mainnet\n"}, "mainnet")):
            out = subprocess.run([sys.executable, "-m", "ops.host_instances", "network", str(etc_with(files))],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual((out.returncode, out.stdout), (0, name + "\n"), out.stderr)
        bad = subprocess.run([sys.executable, "-m", "ops.host_instances", "network",
                              str(etc_with({"host.env": "COLOSSEUM_NETWORK=both\n"}))], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, "")

    def test_the_environment_names_the_same_network_or_nothing_is_read(self):
        # install.sh exports the host's network before it reads a record; a run by hand that names the other is refused.
        with mainnet():
            with self.assertRaisesRegex(SystemExit, "this host serves testnet .* COLOSSEUM_NETWORK selects mainnet"):
                hi.instances(etc_with({"deployment-sim1": "sim1\n"}))
        with Records("testnet", {"mainnet-sim1": SIM}):
            with self.assertRaisesRegex(SystemExit, "this host serves mainnet .* COLOSSEUM_NETWORK selects testnet"):
                hi.instances(etc_with(MAINNET_HOST))

    def test_a_list_asked_by_hand_takes_the_hosts_network(self):
        env = {k: v for k, v in os.environ.items() if k != "COLOSSEUM_NETWORK"}
        etc = etc_with({"host.env": "COLOSSEUM_NETWORK=mainnet\n"})
        out = subprocess.run([sys.executable, "-m", "ops.host_instances", "list", str(etc)], cwd=ROOT, env=env,
                             capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (0, ""), out.stderr)
        other = subprocess.run([sys.executable, "-m", "ops.host_instances", "list", str(etc)], cwd=ROOT,
                               env={**env, "COLOSSEUM_NETWORK": "testnet"}, capture_output=True, text=True)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("one host serves one network", other.stderr)


class AMainnetHost(unittest.TestCase):
    """A mainnet record is taken on a mainnet host: one simulated pool's instance, with its own file's two lines."""

    def test_a_mainnet_record_is_served_on_a_mainnet_host(self):
        with mainnet():
            etc = etc_with(MAINNET_HOST)
            self.assertEqual(hi.instances(etc), [hi.Instance("sim1", "sim1", None)])
            self.assertEqual(hi.deployment_env("sim1"),
                             "COLOSSEUM_DEPLOYMENT=sim1\nCOLOSSEUM_RPC_URL=https://rpc.hyperliquid.xyz/evm\n"
                             "COLOSSEUM_LOG_WINDOW=50\nCOLOSSEUM_MULTI_WALLET=filler\n")
            self.assertEqual(hi.sim_wallets(deployments.load("sim1")), [("filler", FILLER), ("checker", CHECKER)])
            self.assertEqual(hi.keeper_target(deployments.load("sim1")), SIM["SimPoolFactory"])
            self.assertEqual(hi.nginx_locations(hi.instances(etc)) + hi.nginx_upstreams(hi.instances(etc)), "")

    def test_a_fresh_mainnet_host_with_no_instance_yet_takes_a_release(self):
        # The first release goes out before `bootstrap.sh --sim-instance`, which is run from it.
        with mainnet():
            self.assertEqual(hi.instances(etc_with({"host.env": "COLOSSEUM_NETWORK=mainnet\n"})), [])

    def test_testnets_record_of_the_same_label_is_not_taken_for_mainnets(self):
        # The record is read for the host's network alone: testnet's of that label is another file.
        with Records("mainnet", {"testnet-sim1": {**SIM, "chain_id": 998, "network": "testnet"}}):
            with self.assertRaisesRegex(SystemExit, "names sim1, a testnet deployment, and this host serves mainnet"):
                hi.instances(etc_with(MAINNET_HOST))
        with mainnet():
            with self.assertRaisesRegex(SystemExit, "no deployment named 'other' on mainnet"):
                hi.instances(etc_with({**MAINNET_HOST, "deployment-sim1": "other\n"}))

    def refused(self, files: dict[str, str], **records) -> str:
        with mainnet(**records):
            with self.assertRaises(SystemExit) as stop:
                hi.instances(etc_with(files))
        return str(stop.exception)

    def test_it_serves_one_simulated_pools_instance_and_nothing_else(self):
        demo3 = json.loads((ROOT / "deployments" / "testnet-v3-demo3.json").read_text())
        second = {**MAINNET_HOST, "deployment-sim2": "sim2\n", "keeper-sim2.env": KEEPER_ENV.replace("sim1", "sim2")}
        self.assertIn("it serves one simulated pool's instance", self.refused(second, **{"mainnet-sim2": SIM}))
        self.assertIn("sim1, sim2", self.refused(second, **{"mainnet-sim2": SIM}))
        # An exchange's instance, were its record mainnet's: a gateway, which a mainnet host has none of.
        exchange = {"host.env": "COLOSSEUM_NETWORK=mainnet\n", "deployment-demo3": "v3-demo3\n",
                    "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n"}
        self.assertIn("it serves one simulated pool's instance",
                      self.refused(exchange, **{"mainnet-v3-demo3": {**demo3, "chain_id": 999, "network": "mainnet"}}))
        for name in ("deployment", "deployment.env"):
            with self.subTest(name):
                self.assertIn("a first deployment has a gateway",
                              self.refused({**MAINNET_HOST, name: "sim9\n"}, **{"mainnet-sim9": SIM}))
        self.assertIn("the shared pool's keeper is testnet's",
                      self.refused({**MAINNET_HOST, "shared-keeper.env": "COLOSSEUM_SHARED_DEPLOYMENT=shared-demo2b\n"}))

    def test_its_own_file_names_the_network_and_the_confirmation_before_anything_switches(self):
        key = "https://node.example/evm/A-KEY-NOBODY-PRINTS"
        for what, text in {
                "no confirmation": KEEPER_ENV.replace("COLOSSEUM_CONFIRM_MAINNET=sim1\n", ""),
                "another label's": KEEPER_ENV.replace("=sim1\n", "=sim2\n"),
                "commented out": KEEPER_ENV.replace("COLOSSEUM_CONFIRM_MAINNET", "#   COLOSSEUM_CONFIRM_MAINNET"),
                "said twice": KEEPER_ENV + "COLOSSEUM_CONFIRM_MAINNET=sim1\n",
                "no network": KEEPER_ENV.replace("COLOSSEUM_NETWORK=mainnet\n", ""),
                "an empty one": KEEPER_ENV.replace("CONFIRM_MAINNET=sim1", "CONFIRM_MAINNET="),
        }.items():
            with self.subTest(what):
                said = self.refused({**MAINNET_HOST, "keeper-sim1.env": text + f"COLOSSEUM_HOST_RPC_URL={key}\n"})
                self.assertIn("COLOSSEUM_NETWORK=mainnet and, written by hand, COLOSSEUM_CONFIRM_MAINNET=sim1", said)
                self.assertNotIn("A-KEY", said, "the file's other lines hold a node's key: none is said")
        self.assertIn("keeper-sim1.env", self.refused({k: v for k, v in MAINNET_HOST.items() if k != "keeper-sim1.env"}))
        # systemd drops a value's quotes, and so does the reading.
        with mainnet():
            quoted = KEEPER_ENV.replace("=sim1\n", "='sim1'\n").replace("=mainnet\n", '="mainnet"\n')
            self.assertEqual(len(hi.instances(etc_with({**MAINNET_HOST, "keeper-sim1.env": quoted}))), 1)


class OneHostOneNetwork(unittest.TestCase):
    """Whatever on a host belongs to the other network stops the release, in those words."""

    def test_a_testnet_host_takes_no_mainnet_record(self):
        with Records("testnet", {"mainnet-sim1": SIM}):
            with self.assertRaisesRegex(SystemExit, "names sim1, a mainnet deployment, and this host serves testnet"):
                hi.instances(etc_with({"deployment-sim1": "sim1\n"}))

    def test_a_mainnet_host_takes_no_testnet_deployment(self):
        # demo3 and demo2 as they are named on testnet's host, on a host whose file says mainnet.
        files = {"host.env": "COLOSSEUM_NETWORK=mainnet\n", "deployment-demo3": "v3-demo3\n",
                 "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n"}
        with mock.patch.dict(os.environ, {"COLOSSEUM_NETWORK": "mainnet"}):
            with self.assertRaisesRegex(SystemExit, "deployment-demo3 names v3-demo3, a testnet deployment, and this "
                                                    "host serves mainnet.*one host serves one network"):
                hi.instances(etc_with(files))
            with self.assertRaisesRegex(SystemExit, "deployment names demo2, a testnet deployment"):
                hi.instances(etc_with({"host.env": "COLOSSEUM_NETWORK=mainnet\n", "deployment": "demo2\n"}))

    def test_no_settings_file_names_the_other_network(self):
        # A unit takes COLOSSEUM_NETWORK from its own file: one naming mainnet on a testnet host would start there.
        for name in ("keeper-demo3.env", "sim-index-sim1.env", "keeper.env", "gateway-demo3.env"):
            files = {"deployment": "demo2\n", "deployment-demo3": "v3-demo3\n",
                     "gateway-demo3.env": "GATEWAY_BIND=127.0.0.1:8788\n"}
            files[name] = files.get(name, "") + "COLOSSEUM_NETWORK=mainnet\n"
            with self.subTest(name), self.assertRaisesRegex(SystemExit, f"{name} names another network than this host's, testnet"):
                hi.instances(etc_with(files))
        with mainnet():
            with self.assertRaisesRegex(SystemExit, "sim-index-sim1.env names another network than this host's, mainnet"):
                hi.instances(etc_with({**MAINNET_HOST, "sim-index-sim1.env": "COLOSSEUM_NETWORK=\nSIM_INDEX_RECORD=x\n"}))
        # Naming the host's own, or none, is as before.
        same = {"deployment": "demo2\n", "keeper.env": "COLOSSEUM_NETWORK=testnet\n", "gateway.env": "COLOSSEUM_NETWORK=\n"}
        self.assertEqual(hi.instances(etc_with(same)), [])


class Install(unittest.TestCase):
    def setUp(self):
        self.install = (HOST / "install.sh").read_text()

    def test_install_reads_the_hosts_network_once_and_exports_it_before_any_record(self):
        read = self.install.index('network="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances network /etc/colosseum)"')
        export = self.install.index('export COLOSSEUM_NETWORK="$network"')
        listing = self.install.index("-m ops.host_instances list /etc/colosseum")
        self.assertLess(read, export)
        self.assertLess(export, listing, "every record after this is read for the host's network")
        self.assertIn("nothing switched", self.install[read:export])
        self.assertEqual(self.install.count("COLOSSEUM_NETWORK="), 1, "named in one place: the host's file")
        self.assertNotIn("Testnet only", self.install)
        for script in ("deploy.sh", "deploy-ssm.sh", "bootstrap.sh"):
            self.assertNotIn("Testnet only", (HOST / script).read_text(), script)
        self.assertNotIn("Testnet only", (ROOT / "ops" / "host_instances.py").read_text())

    def test_a_record_that_is_missing_is_said_for_the_hosts_network(self):
        self.assertIn('fail "deployment $label: no complete $network record in this release; nothing switched"', self.install)
        self.assertIn('fail "deployment $1: no complete $network record in this release"', self.install)

    def test_a_mainnet_host_with_testnets_services_enabled_switches_to_nothing(self):
        at = self.install.index('if [ "$network" = mainnet ]; then\n  for unit in colosseum-gateway colosseum-keeper colosseum-shared-keeper; do')
        block = self.install[at:self.install.index("\nfi\n", at)]
        self.assertIn('! systemctl is-enabled --quiet "$unit" 2>/dev/null \\\n      || fail "this is a mainnet host and $unit is enabled', block)
        self.assertIn("nothing switched", block)
        self.assertLess(self.install.index("-m ops.host_instances list /etc/colosseum"), at)
        self.assertLess(at, self.install.index("site=/etc/nginx/conf.d/pools-api.conf"), "before nginx or a unit")
        self.assertLess(at, self.install.index('ln -sfn "releases/$sha"'))
        # The block as install.sh has it, with a systemctl that says which units are enabled.
        for enabled, code in (("", 0), ("colosseum-keeper", 1), ("colosseum-shared-keeper", 1)):
            for network, expect in (("mainnet", code), ("testnet", 0)):
                script = ('fail() { printf "install: %s\\n" "$*" >&2; exit 1; }\n'
                          'systemctl() { [ "$1" = is-enabled ] && [ "$3" = "$ENABLED" ]; }\n'
                          f'network={network}\n{block}\nfi\n')
                out = subprocess.run(["bash", "-c", script], env={**os.environ, "ENABLED": enabled},
                                     capture_output=True, text=True)
                self.assertEqual(out.returncode, expect, (enabled, network, out.stderr))
                if expect:
                    self.assertIn(f"this is a mainnet host and {enabled} is enabled", out.stderr)


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.boot = (HOST / "bootstrap.sh").read_text()

    def test_a_network_out_of_the_two_is_refused_before_anything_is_done(self):
        for args in (["--network"], ["--network", "main"], ["--network", "mainnet", "sim1"], ["--network", ""]):
            out = subprocess.run(["bash", str(HOST / "bootstrap.sh"), *args], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2, (args, out.stderr))
            self.assertIn("usage: bootstrap.sh --network <testnet|mainnet>", out.stderr)
            self.assertNotIn("bootstrap: python3.12", out.stdout, "nothing was installed or checked first")

    def test_the_network_is_settled_before_a_package_a_user_or_a_file(self):
        settled = self.boot.index('network="$(host_network)"')
        self.assertLess(settled, self.boot.index("for pkg in python3.12 nginx; do"))
        guard = self.boot[settled:self.boot.index("for pkg in python3.12 nginx; do")]
        # The file once written is never changed; a host with testnet's deployments is no mainnet host; a mainnet host
        # takes no exchange's instance and no second one.
        for need in ("the file is never changed here", "this host serves testnet's deployments. Take them off first",
                     "it serves one simulated pool's instance (--sim-instance), never an exchange's",
                     "names an instance already: it serves one, as one user"):
            self.assertIn(need, guard)
        self.assertEqual(guard.count("exit 2"), 5)
        write = self.boot.index('"COLOSSEUM_NETWORK=$network" | install -m 0644 -o root -g root /dev/stdin "$host_env"')
        self.assertIn('if [ -n "$want_network" ] && [ ! -e "$host_env" ]; then', self.boot[settled:write])

    def test_a_mainnet_host_gets_none_of_testnets_settings(self):
        start = self.boot.index('if [ "$network" = testnet ]; then\n')
        end = self.boot.index("fi  # testnet's three settings files\n")
        inside = self.boot[start:end]
        for name in ("gateway.env", "keeper.env", "shared-keeper.env"):
            self.assertIn(f"if [ ! -f /etc/colosseum/{name} ]; then", inside)
            self.assertEqual(self.boot.count(f"if [ ! -f /etc/colosseum/{name} ]; then"), 1)

    def mainnet_block(self) -> str:
        tail = self.boot[self.boot.index('[ -n "$instance" ] || exit 0'):]
        at = tail.index('if [ "$network" = mainnet ]; then\n')
        return tail[at:tail.index("\nfi\n", at)]

    def test_a_mainnet_instances_settings_name_the_network_and_leave_the_confirmation_to_a_person(self):
        block = self.mainnet_block()
        self.assertEqual(block.count('"COLOSSEUM_NETWORK=mainnet" \\\n'), 2, "the keeper's and the index's: the host's network")
        self.assertIn('printf \'%s\\n\' "COLOSSEUM_KEY_DIR=$key_dir" \\\n      "COLOSSEUM_NETWORK=mainnet" \\\n', block)
        self.assertIn('"SIM_INDEX_RECORD=deployments/mainnet-$instance_label.json"', block)
        # The confirmation, the host's nodes and the site's origin are named in comments: a person writes each.
        for by_hand in ("COLOSSEUM_CONFIRM_MAINNET=$instance_label", "COLOSSEUM_HOST_RPC_URL=", "COLOSSEUM_STOP_RPC_URL=",
                        "SIM_INDEX_ALLOW_ORIGIN="):
            line = next(l for l in block.splitlines() if by_hand in l and "say " not in l)
            self.assertTrue(line.strip().startswith('"#   '), line)
        # Root's alone: the file will hold a node's key.
        self.assertEqual(block.count("install -m 0600 -o root -g root /dev/stdin"), 2)
        self.assertNotIn("0644", block)
        self.assertIn("chmod 0710 /var/lib/colosseum-keeper", block)
        self.assertTrue(block.rstrip().endswith("exit 0"), "testnet's settings below are never written for it")
        # What a mainnet host's file is checked for at a release is what this names.
        self.assertIn(f"{hi.CONFIRM}=$instance_label", block)

    def test_what_bootstrap_writes_and_a_person_completes_passes_installs_check(self):
        block = self.mainnet_block()
        script = block[block.index("    printf '%s\\n' \"COLOSSEUM_KEY_DIR"):]
        script = script[:script.index("| install -m 0600")]
        made = subprocess.run(["bash", "-c", f'key_dir=/k; instance_label=sim1\n{script}'], capture_output=True, text=True,
                              check=True).stdout
        self.assertEqual(made.splitlines()[:2], ["COLOSSEUM_KEY_DIR=/k", "COLOSSEUM_NETWORK=mainnet"])
        with mainnet():
            with self.assertRaisesRegex(SystemExit, "written by hand, COLOSSEUM_CONFIRM_MAINNET=sim1"):
                hi.instances(etc_with({**MAINNET_HOST, "keeper-sim1.env": made}))
            by_hand = made + "COLOSSEUM_CONFIRM_MAINNET=sim1\nCOLOSSEUM_HOST_RPC_URL=https://node.example/evm/key\n"
            self.assertEqual(hi.instances(etc_with({**MAINNET_HOST, "keeper-sim1.env": by_hand})),
                             [hi.Instance("sim1", "sim1", None)])

    def test_the_keepers_directory_keeps_the_groups_pass_once_it_has_it(self):
        self.assertIn("keeper_mode=0700\n[ \"$(stat -c '%a' /var/lib/colosseum-keeper 2>/dev/null || true)\" != 710 ] "
                      "|| keeper_mode=0710\n"
                      'install -d -m "$keeper_mode" -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper\n',
                      self.boot)
        subprocess.run(["bash", "-n", str(HOST / "bootstrap.sh")], check=True)


class TheIndexOnNginx(unittest.TestCase):
    """The index of a simulated pool is reached through nginx: the second machine's watch reads the host's pulse there,
    and the operator's page the reviews. install.sh writes the site anew from the template at every release, so the
    route is the template's own."""

    def site(self) -> str:
        from ops.tests.test_host_instances import Nginx
        return Nginx.render(self, hi.nginx_locations([]), hi.nginx_upstreams([]))

    def block(self, site: str) -> str:
        at = site.index("    location ~ ^/v1/simindex/")
        return site[at:site.index("\n    }\n", at)]

    def test_the_three_paths_the_index_answers_go_to_it_and_no_other(self):
        from gateway import simindex
        site = self.site()
        server = site[site.index("server_name pools-api.usenami.io;"):]
        block = self.block(server)
        self.assertLess(server.index(block), server.index("    location / {\n        return 404;"),
                        "inside the API's server, before its 404")
        answered = re.findall(r'url\.path == "/v1/simindex/(\w+)"', (ROOT / "gateway" / "simindex.py").read_text())
        self.assertEqual(sorted(answered), ["health", "pool", "reviews"])
        named = re.match(r"    location ~ \^/v1/simindex/\(([a-z|]+)\)\$ \{\n", block)
        self.assertIsNotNone(named, block)
        self.assertEqual(sorted(named.group(1).split("|")), sorted(answered), "the paths the service answers, each whole")
        self.assertIn(f"        proxy_pass http://{simindex.DEFAULT_BIND};\n", block, "the index's own port")
        self.assertNotIn("pools_gateway", block, "never a gateway's upstream")
        # The watch on the second machine asks for exactly this path (its unit's COLOSSEUM_HOST_HEALTH_URL).
        self.assertIn("/v1/simindex/health", (HOST / "second-writer" / "colosseum-host-watch@.service").read_text())

    def test_get_alone_under_a_limit_and_nothing_keeps_an_answer(self):
        site = self.site()
        block = self.block(site)
        self.assertIn("        limit_except GET { deny all; }\n", block)
        self.assertIn("        limit_req zone=pools_simindex_ip burst=20 nodelay;\n", block)
        self.assertIn("limit_req_zone $binary_remote_addr zone=pools_simindex_ip:1m rate=60r/m;", site[:site.index("server {")])
        self.assertIn("        proxy_hide_header Cache-Control;\n        add_header Cache-Control no-store always;", block)
        directives = "\n".join(l for l in site.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("proxy_cache", directives)
        self.assertNotIn("X-Real-IP", block, "the index limits by nothing a client says")

    def test_the_route_is_the_templates_so_a_release_keeps_it_and_a_host_without_an_index_keeps_its_site(self):
        template = (HOST / "nginx-pools-api.conf.in").read_text()
        self.assertEqual(template.count("location ~ ^/v1/simindex/(health|pool|reviews)$ {"), 1)
        # No marker of its own and no word of it in install.sh: nothing to fill in, nothing to leave out by hand.
        self.assertNotIn("simindex", (HOST / "install.sh").read_text().split("# 2. Units")[1].split("# 3.")[0].replace(
            "colosseum-sim-index@.service", ""))
        # With no instance at all the first gateway's paths are as they were: four exact locations and one upstream.
        site = self.site()
        self.assertEqual(site.count("location ="), 4)
        self.assertEqual(len(re.findall(r"^upstream \S+ \{", site, re.M)), 1)
        self.assertNotIn("@", "".join(l for l in site.splitlines() if l.startswith("@")))


class TheJournalThePulseLooksAt(unittest.TestCase):
    """The host's pulse says when the stops' loop last wrote its journal of prices. The loop names the journal by the
    deployment's label; the index, given the record's file, looked for prices-mainnet-sim1-<day>.jsonl, which nobody
    writes. One name on both sides, held here: the writer's own default against the index's."""

    def written(self, label: str, folder: pathlib.Path) -> tuple[pathlib.Path, str]:
        """The journal the keeper's stops' loop writes for `label` with its unit's --state, by its own default."""
        from ops import stop_filler as sf
        unit = (HOST / "colosseum-multi-keeper@.service").read_text()
        state = pathlib.PurePosixPath(re.search(r"--state (\S+)", unit).group(1).replace("${COLOSSEUM_DEPLOYMENT}", label))
        self.assertNotIn("--price-journal", unit)
        self.assertEqual(str(state.parent), "/var/lib/colosseum-keeper")
        args = mock.Mock(stop_rpc="", price_journal=None, stop_alarm=None, deployment=label, stop_tip_gwei=0,
                         stop_overdue=5, stop_alarm_min_blocks=2)
        with mock.patch.object(sf, "Node"), mock.patch.object(sf, "check_chain"):
            loop = sf.build(args, mock.Mock(factory=None), folder / state.name)
        try:
            return loop.journal.path, loop.journal.file_of(1_800_000_000).name
        finally:
            loop.journal.close()

    def test_the_index_looks_for_the_journal_its_writer_writes(self):
        from gateway import simindex
        from spike.hlspike import network as networks
        unit = (HOST / "colosseum-sim-index@.service").read_text()
        self.assertIn("Environment=SIM_INDEX_KEEPER_DIR=/var/lib/colosseum-keeper", unit)
        for net in (networks.MAINNET, networks.TESTNET):
            for label in ("sim1", "rehearsal998", "mainnet-first"):
                with self.subTest(net=net.name, label=label), tempfile.TemporaryDirectory() as d, \
                        mock.patch.object(simindex, "NETWORK", net):
                    folder = pathlib.Path(d)
                    journal, day_file = self.written(label, folder)
                    record = str(deployments.record_path(label, net).relative_to(deployments.ROOT))
                    self.assertEqual(record, f"deployments/{net.name}-{label}.json")
                    looked = simindex.prices_path({"SIM_INDEX_KEEPER_DIR": str(folder)}, record)
                    self.assertEqual(looked, journal)
                    # And the day file the pulse takes the time of is the one the writer makes for that day.
                    (folder / day_file).write_text("{}\n")
                    os.utime(folder / day_file, (1_800_000_000 - 7, 1_800_000_000 - 7))
                    pulse = simindex.Pulse(looked, None, clock=lambda: 1_800_000_000)
                    self.assertEqual(pulse.prices_at(), 1_800_000_000 - 7)

    def test_the_setting_that_names_the_journal_outright_still_wins(self):
        from gateway import simindex
        env = {"SIM_INDEX_PRICES": "/elsewhere/prices-x.jsonl", "SIM_INDEX_KEEPER_DIR": "/k"}
        self.assertEqual(simindex.prices_path(env, "deployments/mainnet-sim1.json"), pathlib.Path("/elsewhere/prices-x.jsonl"))
        self.assertIsNone(simindex.prices_path({}, "deployments/mainnet-sim1.json"))
        self.assertIsNone(simindex.prices_path({"SIM_INDEX_KEEPER_DIR": "/k"}, ""))
        # A record named without its network is the label as it stands, as before.
        self.assertEqual(simindex.prices_path({"SIM_INDEX_KEEPER_DIR": "/k"}, "deployments/sim1.json"),
                         pathlib.Path("/k/prices-sim1.jsonl"))


OWN = "https://KEY-IN-THE-HOST.node.example/evm/KEY-IN-THE-PATH?k=KEY-IN-THE-QUERY"
SECRETS = ("KEY-IN-THE-HOST", "KEY-IN-THE-PATH", "KEY-IN-THE-QUERY", "node.example")


class TheHostsOwnNode(unittest.TestCase):
    """The record is public, so the node it names is one without a key. The node the host pays for is named on the host
    alone, COLOSSEUM_HOST_RPC_URL in the instance's keeper-<name>.env, and it is the keeper's node whatever the record
    says; its address is in no error's text and in no line for a person."""

    def start(self, env: dict[str, str], code: str) -> str:
        """`code` in a process started with `env`, as a unit starts the keeper with its files' lines."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("COLOSSEUM_")}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                             env={**clean, "COLOSSEUM_KEY_DIR": "", **env})
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def unit_env(self, unit: str, files: dict[str, str]) -> dict[str, str]:
        """What a unit's process is given: its EnvironmentFile= lines in order, a later file over an earlier one."""
        env: dict[str, str] = {}
        for line in (HOST / unit).read_text().splitlines():
            if line.startswith("EnvironmentFile="):
                name = line.split("=", 1)[1].lstrip("-").rsplit("/", 1)[1].replace("%i", "sim1")
                for setting in files.get(name, "").splitlines():
                    if "=" in setting and not setting.startswith("#"):
                        key, value = setting.split("=", 1)
                        env[key] = value
        return env

    def test_the_hosts_own_node_wins_over_the_records_for_the_keeper_and_the_stops(self):
        with mainnet():
            record_file = hi.deployment_env("sim1")
        self.assertIn("COLOSSEUM_RPC_URL=https://rpc.hyperliquid.xyz/evm\n", record_file, "the record's: a public node")
        by_hand = KEEPER_ENV + f"COLOSSEUM_RPC_URL=https://by-hand.example/evm\nCOLOSSEUM_HOST_RPC_URL={OWN}\n"
        files = {"keeper-sim1.env": by_hand, "deployment-sim1.env": record_file}
        said = "from spike.hlspike import common as c; import os; print(c.RPC_URL); print(os.environ.get('COLOSSEUM_STOP_RPC_URL', '') or c.RPC_URL)"
        for unit in ("colosseum-multi-keeper@.service", "colosseum-sim-checker@.service", "colosseum-payout-review@.service"):
            env = self.unit_env(unit, files)
            # The record's file is read last, as before: its COLOSSEUM_RPC_URL replaces the line by hand...
            self.assertEqual(env["COLOSSEUM_RPC_URL"], "https://rpc.hyperliquid.xyz/evm", unit)
            # ...and the host's own node is the node all the same, the stops' loop's too while it names none of its own.
            self.assertEqual(self.start(env, said).splitlines(), [OWN, OWN], unit)
        env = self.unit_env("colosseum-multi-keeper@.service", {**files, "keeper-sim1.env": by_hand + "COLOSSEUM_STOP_RPC_URL=https://stops.example/evm\n"})
        self.assertEqual(self.start(env, said).splitlines(), [OWN, "https://stops.example/evm"])
        # Without it everything is as it was: the record's node, and with no record's line the one by hand.
        no_own = {"keeper-sim1.env": KEEPER_ENV + "COLOSSEUM_RPC_URL=https://by-hand.example/evm\n", "deployment-sim1.env": record_file}
        self.assertEqual(self.start(self.unit_env("colosseum-multi-keeper@.service", no_own), said).splitlines()[0],
                         "https://rpc.hyperliquid.xyz/evm")
        self.assertEqual(self.start({"COLOSSEUM_RPC_URL": "https://by-hand.example/evm", "COLOSSEUM_HOST_RPC_URL": " "},
                                    said).splitlines()[0], "https://by-hand.example/evm", "one of spaces alone is none")

    def test_the_stops_loop_reads_the_hosts_own_node(self):
        from ops import stop_filler as sf
        from spike.hlspike import common as c
        args = mock.Mock(stop_rpc="", price_journal="", stop_alarm="", deployment="sim1", stop_tip_gwei=0,
                         stop_overdue=5, stop_alarm_min_blocks=2)
        with mock.patch.object(c, "RPC_URL", OWN), mock.patch.object(c, "OWN_NODE", OWN), \
                mock.patch.object(sf, "Node") as node, mock.patch.object(sf, "check_chain"):
            sf.build(args, mock.Mock(factory=None), pathlib.Path(tempfile.mkdtemp()) / "sim-sim1.json")
        node.assert_called_once_with(OWN)

    def failing(self, error: Exception):
        """c's session, whose every call fails with `error`, raised as the HTTP library raises: on top of its own."""
        def post(*args, **kwargs):
            try:
                raise OSError(f"under it: {OWN}")
            except OSError:
                raise error
        return mock.Mock(post=post)

    ERRORS = {
        "refused": lambda r: r.ConnectionError(
            "HTTPSConnectionPool(host='KEY-IN-THE-HOST.node.example', port=443): Max retries exceeded with url: "
            "/evm/KEY-IN-THE-PATH?k=KEY-IN-THE-QUERY (Caused by NewConnectionError('Failed to establish a new connection'))"),
        "a status": lambda r: r.HTTPError(f"503 Server Error: Service Unavailable for url: {OWN}"),
        "a name not found": lambda r: r.ConnectionError("Failed to resolve 'key-in-the-host.NODE.example' ([Errno 8] nodename)"),
        "an address not taken": lambda r: r.exceptions.InvalidURL(f"Invalid URL {OWN!r}: No host supplied"),
        "a timeout": lambda r: r.ReadTimeout(f"HTTPSConnectionPool(host='KEY-IN-THE-HOST.node.example', port=443): Read timed out. (read timeout=30)"),
    }

    def test_no_failure_of_a_call_says_the_nodes_address_while_the_hosts_own_is_named(self):
        import traceback

        import requests
        from spike.hlspike import common as c
        for what, make in self.ERRORS.items():
            for call in (lambda: c.rpc("eth_blockNumber"), lambda: c.rpc_once(OWN, "eth_getLogs", [{}])):
                with self.subTest(what), mock.patch.object(c, "OWN_NODE", OWN), mock.patch.object(c, "RPC_URL", OWN), \
                        mock.patch.object(c, "_session", self.failing(make(requests))):
                    with self.assertRaises(requests.RequestException) as caught:
                        call()
                    exc = caught.exception
                    self.assertIs(type(exc), type(make(requests)), "the same error, by its type, for whoever tells them apart")
                    # What a keeper logs (`error=str(exc)[:200]`), an alarm carries and a start's traceback prints.
                    for said in (str(exc), repr(exc), "".join(traceback.format_exception(exc))):
                        for secret in SECRETS:
                            self.assertNotIn(secret.lower(), said.lower(), (what, said))
                    self.assertTrue(str(exc), "it still says what failed")

    def test_without_it_an_error_is_as_it_was(self):
        import requests
        from spike.hlspike import common as c
        error = self.ERRORS["refused"](requests)
        with mock.patch.object(c, "OWN_NODE", ""), mock.patch.object(c, "_session", self.failing(error)):
            with self.assertRaises(requests.ConnectionError) as caught:
                c.rpc("eth_blockNumber")
        self.assertIs(caught.exception, error)
        self.assertIn("/evm/KEY-IN-THE-PATH", str(caught.exception))
        self.assertIsNotNone(caught.exception.__context__)
        self.assertFalse(caught.exception.__suppress_context__)

    def test_an_error_of_the_node_itself_is_said_whole(self):
        # The node's own answer ("execution reverted", a refused range) is what the keepers decide by: never touched.
        from spike.hlspike import common as c
        answer = mock.Mock(status_code=200, json=lambda: {"error": {"code": 3, "message": "execution reverted: NotFiller()"}})
        with mock.patch.object(c, "OWN_NODE", OWN), mock.patch.object(c, "_session", mock.Mock(post=lambda *a, **k: answer)):
            with self.assertRaisesRegex(RuntimeError, r"eth_call: \{'code': 3, 'message': 'execution reverted: NotFiller\(\)'\}"):
                c.rpc("eth_call", [])

    def test_hush_takes_out_every_address_and_the_own_nodes_name(self):
        from ops import sim_check
        from spike.hlspike import common as c
        with mock.patch.object(c, "OWN_NODE", OWN), mock.patch.object(c, "RPC_URL", OWN):
            self.assertEqual(c.hush(f"for url: {OWN} and {OWN[:40]}"), "for url: <node> and <node>")
            self.assertEqual(c.hush(f"for url: {OWN}/and/on " + "x" * 300), "for url: <node> " + "x" * 300, "not cut here")
            self.assertEqual(c.hush("Max retries exceeded with url: /evm/KEY-IN-THE-PATH (Caused by x)"),
                             "Max retries exceeded with url: <path> (Caused by x)")
            self.assertEqual(c.hush("host='KEY-IN-THE-HOST.node.example', port=443"), "host='<host>', port=443")
            self.assertEqual(c.hush("Failed to resolve 'Key-In-The-Host.node.example'"), "Failed to resolve '<host>'")
            self.assertEqual(c.hush("execution reverted: NotFiller()"), "execution reverted: NotFiller()")
        with mock.patch.object(c, "OWN_NODE", ""):
            self.assertEqual(c.hush("see https://other.example/x?k=1 now"), "see <node> now")
        # One set of rules: the checker's own, which already hid its lines' addresses (ops/sim_check.py, `quiet`).
        with mock.patch.object(sim_check, "quiet", return_value="the checker's rules") as rules:
            self.assertEqual(c.hush("anything"), "the checker's rules")
        rules.assert_called_once_with("anything", None)
        self.assertNotIn("re.compile", (ROOT / "spike" / "hlspike" / "common.py").read_text(), "no rules of its own")
        # A configured address that does not parse: nothing of the text is said, rather than all of it.
        with mock.patch.object(c, "RPC_URL", "https://[not-an-address/evm"):
            self.assertNotIn("KEY", c.hush("failed at https://x.example/KEY and [not-an-address"))

    def test_a_person_is_shown_which_node_and_never_the_hosts_own_address(self):
        import contextlib
        import io

        from ops import sim_network as sn
        from spike.hlspike import common as c
        with mock.patch.object(c, "OWN_NODE", OWN), mock.patch.object(c, "RPC_URL", OWN):
            self.assertEqual(c.node_shown(), "the host's own node (its address is not shown)")
            self.assertEqual(c.node_shown(OWN + "/"), "the host's own node (its address is not shown)")
            self.assertEqual(c.node_shown("https://public.example/evm?x=1"), "https://public.example/evm")
            # The line a person confirms a send on mainnet by (ops/sim_network.py).
            import types

            from spike.hlspike import network as networks
            args = mock.Mock(network="mainnet")
            fake = types.SimpleNamespace(NETWORK=networks.MAINNET, RPC_URL=OWN, node_shown=c.node_shown,
                                         assert_network=lambda: None)
            out = io.StringIO()
            with mock.patch.object(sn, "key_dir_ok"), contextlib.redirect_stdout(out):
                sn.confirm_send(args, "a send", fake, ask=lambda q: sn.CONFIRM_PHRASE, tty=True)
            self.assertIn("RPC the host's own node (its address is not shown): a send", out.getvalue())
            for secret in SECRETS:
                self.assertNotIn(secret, out.getvalue())
        with mock.patch.object(c, "OWN_NODE", ""), mock.patch.object(c, "RPC_URL", "https://public.example/evm?x=1"):
            self.assertEqual(c.node_shown(), "https://public.example/evm")

    def test_no_line_of_the_journal_names_a_node_while_the_hosts_own_is_named(self):
        import contextlib
        import io

        from ops import keeper as core
        from ops import sim_keeper, stop_filler
        from spike.hlspike import common as c
        # The stops' loop says its node's scheme and host at its start, through the keepers' one `log`.
        self.assertIn('log("stop_loop_started", node=host_of(self.node.url)', (ROOT / "ops" / "stop_filler.py").read_text())
        self.assertIs(stop_filler.log, core.log)
        self.assertIs(sim_keeper.log, core.log)
        started = dict(node=stop_filler.host_of(OWN), tip_wei=0, sends=True, block=7)
        out = io.StringIO()
        with mock.patch.object(c, "OWN_NODE", OWN), mock.patch.object(c, "RPC_URL", OWN), contextlib.redirect_stdout(out):
            core.log("stop_loop_started", **started)
            core.log("loop_failed", error=f"HTTPSConnectionPool(host='KEY-IN-THE-HOST.node.example'): url: /evm/KEY-IN-THE-PATH")
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual((lines[0]["event"], lines[0]["node"], lines[0]["block"], lines[0]["sends"]),
                         ("stop_loop_started", "<node>", 7, True))
        for secret in SECRETS:
            self.assertNotIn(secret, out.getvalue())
        out = io.StringIO()
        with mock.patch.object(c, "OWN_NODE", ""), contextlib.redirect_stdout(out):
            core.log("stop_loop_started", **started)
        self.assertEqual(json.loads(out.getvalue())["node"], "https://key-in-the-host.node.example", "as before without it")

    def test_the_record_keeps_a_public_node_and_the_host_is_told_where_its_own_goes(self):
        # The record refuses an address with a key in it, as before; the host's file is where bootstrap says it goes.
        with self.assertRaises(SystemExit):
            hi.keeper_nodes({"keeper_nodes": {"primary": {"url": OWN, "log_window": 50}}})
        self.assertIn("COLOSSEUM_HOST_RPC_URL=<this host's own node>", (HOST / "bootstrap.sh").read_text())
        from spike.hlspike import common as c
        self.assertEqual(c.OWN_NODE_ENV, "COLOSSEUM_HOST_RPC_URL")
        self.assertIn(c.OWN_NODE_ENV, (HOST / "colosseum-multi-keeper@.service").read_text())


if __name__ == "__main__":
    unittest.main()
