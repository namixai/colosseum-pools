"""Independent review 19 of PR 340 (head c66f0ce), its test of answer A: can a host or a service act on the wrong network?
Offline; records are stubbed. The reviewer's probes (host.env read by bash and python alike, a node's address in no text)
were run once and are not kept; the findings M3 and L1 are tested in test_sim_levers.py and test_mainnet_host.py.
Run: COLOSSEUM_KEY_DIR= COLOSSEUM_RPC_URL=http://127.0.0.1:9 python -m unittest ops.tests.review19.test_network_gate -v"""
import os, pathlib, tempfile, unittest
from unittest import mock
from ops import host_instances as hi

SIM = {"SimPoolFactory": "0x" + "99" * 20, "filler": "0x" + "11" * 20, "checker": "0x" + "22" * 20}

class Stand(unittest.TestCase):
    def setUp(self):
        self.etc = pathlib.Path(tempfile.mkdtemp())
        self.rec = self.etc / "exists.json"; self.rec.write_text("{}")
        self.p = [mock.patch.object(hi.deployments, "load", return_value=SIM),
                  mock.patch.object(hi.deployments, "record_path", return_value=self.rec)]
        for p in self.p: p.start()
        self.addCleanup(lambda: [p.stop() for p in self.p])
    def host(self, text): (self.etc / "host.env").write_text(text)
    def inst(self, name="sim1", label="sim1", keeper="COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=sim1\n", index=None):
        (self.etc / f"deployment-{name}").write_text(label + "\n")
        if keeper is not None: (self.etc / f"keeper-{name}.env").write_text(keeper)
        if index is not None: (self.etc / f"sim-index-{name}.env").write_text(index)
    def ok(self, net):
        with mock.patch.dict(os.environ, {"COLOSSEUM_NETWORK": net}):
            return hi.instances(self.etc)
    def refused(self, net="mainnet"):
        with self.assertRaises(SystemExit) as s: self.ok(net)
        return str(s.exception)

class Gate(Stand):
    def test_missing_host_env_with_mainnet_instance_file_is_refused(self):
        self.inst(); self.assertIn("names another network", self.refused("testnet"))
    def test_mainnet_needs_confirm_exactly(self):
        self.host("COLOSSEUM_NETWORK=mainnet\n")
        for k in ("COLOSSEUM_NETWORK=mainnet\n", "COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=\n",
                  "COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=other\n",
                  "COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=sim1 # c\n",
                  "COLOSSEUM_NETWORK=mainnet\n#COLOSSEUM_CONFIRM_MAINNET=sim1\n",
                  "COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=sim1\nCOLOSSEUM_CONFIRM_MAINNET=sim1\n",
                  "COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_NETWORK=testnet\nCOLOSSEUM_CONFIRM_MAINNET=sim1\n",
                  "COLOSSEUM_CONFIRM_MAINNET=sim1\n"):
            with self.subTest(k):
                self.inst(keeper=k); self.refused()
    def test_accepted_spellings(self):
        self.host("COLOSSEUM_NETWORK=mainnet\n")
        for k in ("COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=sim1\n",
                  "COLOSSEUM_NETWORK=\"mainnet\"\nCOLOSSEUM_CONFIRM_MAINNET='sim1'\n",
                  "# x\n  COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=sim1  \n"):
            with self.subTest(k):
                self.inst(keeper=k); self.assertEqual([i.name for i in self.ok("mainnet")], ["sim1"])
    def test_env_naming_the_other_network_than_the_hosts(self):
        self.host("COLOSSEUM_NETWORK=mainnet\n"); self.inst()
        self.assertIn("name the same in both", self.refused("testnet"))
    def test_index_file_testnet_or_without_network_line(self):
        self.host("COLOSSEUM_NETWORK=mainnet\n")
        for i in ("COLOSSEUM_NETWORK=testnet\n", "SIM_INDEX_RECORD=x\n", ""):
            with self.subTest(i): self.inst(index=i); self.refused()
    def test_second_instance_and_first_deployment_refused_on_mainnet(self):
        self.host("COLOSSEUM_NETWORK=mainnet\n"); self.inst(); self.inst("sim2", "sim2", "COLOSSEUM_NETWORK=mainnet\nCOLOSSEUM_CONFIRM_MAINNET=sim2\n")
        self.refused()
    def test_first_deployment_file_refused_on_mainnet(self):
        self.host("COLOSSEUM_NETWORK=mainnet\n"); self.inst(); (self.etc / "deployment").write_text("demo2\n"); self.refused()
    def test_a_stray_testnet_env_file_beside(self):
        # watch.env / gateway.env naming testnet explicitly
        self.host("COLOSSEUM_NETWORK=mainnet\n"); self.inst(); (self.etc / "watch.env").write_text("COLOSSEUM_NETWORK=testnet\n"); self.refused()
    def test_label_with_only_a_testnet_record(self):
        self.host("COLOSSEUM_NETWORK=mainnet\n"); self.inst()
        def rp(label, net=None):
            net = net or hi.networks.select(); return self.rec if net.name == "testnet" else self.etc / "nope.json"
        with mock.patch.object(hi.deployments, "record_path", side_effect=rp):
            self.assertIn("one host serves one network", self.refused())
    def test_testnet_host_without_file_is_as_before(self):
        self.inst("demo", "demo", keeper=None)
        (self.etc / "gateway-demo.env").write_text("GATEWAY_BIND=127.0.0.1:8788\n")
        with mock.patch.object(hi.deployments, "load", return_value={"PoolFactory": "0x1"}):
            self.assertEqual([(i.name, i.port) for i in self.ok("testnet")], [("demo", 8788)])
            # testnet host + COLOSSEUM_NETWORK=mainnet in the caller's shell is refused, not followed
            with self.assertRaises(SystemExit): self.ok("mainnet")

if __name__ == "__main__":
    unittest.main()
