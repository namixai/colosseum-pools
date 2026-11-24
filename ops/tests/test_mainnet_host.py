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


if __name__ == "__main__":
    unittest.main()
