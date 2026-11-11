"""The gateway listens before it reads the accounts its keys trade, and says so until it has.

On 10 Nov 2026 the host's demo2 gateway answered nothing for 22 and then 42 seconds after a restart: it read its
92 keys through the throttled public node before it bound its port, so even /v1/health was silent. It now binds
first; /v1/health answers at once with `ready: false`, an order is refused `429 busy` (`gateway_starting`) with
`sent: false`, and the reading runs in its own thread, tried again while it fails as a whole.
"""

from __future__ import annotations

import http.client
import inspect
import io
import json
import threading
import unittest
from contextlib import redirect_stdout

from gateway import server
from gateway.protect import Protector
from gateway.server import BoundedServer, Starting, make_handler, take_on_at_start


class Stub:
    """A gateway whose order handler only counts."""

    def __init__(self):
        self.orders = []

    def handle_order(self, body):
        self.orders.append(body)
        return 200, {"status": "submitted"}


class Served:
    def __init__(self, gw, starting):
        self.srv = BoundedServer(("127.0.0.1", 0), make_handler(gw, "", health={"signer": "demo", "keys": 92},
                                                                starting=starting))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def ask(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        data = None if body is None else json.dumps(body).encode()
        conn.request(method, path, body=data, headers={} if data is None else {"Content-Type": "application/json"})
        resp = conn.getresponse()
        out = resp.status, json.loads(resp.read() or b"null"), dict(resp.getheaders())
        conn.close()
        return out

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class WhileStarting(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.starting = Starting(clock=self.clock)
        self.gw = Stub()
        self.served = Served(self.gw, self.starting)
        self.addCleanup(self.served.close)

    def test_health_answers_at_once_and_says_the_keys_are_not_read_yet(self):
        self.clock.t += 12.34
        status, said, _ = self.served.ask("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual((said["ok"], said["ready"], said["starting_for_s"]), (True, False, 12.3))
        self.assertEqual(said["starting"], "reading the accounts the signer's keys trade")
        self.assertEqual((said["signer"], said["keys"]), ("demo", 92), "the configuration is there from the start")
        self.assertNotIn("start_failed_attempts", said)

    def test_an_order_is_refused_with_a_reason_and_sent_false_and_never_reaches_the_gateway(self):
        status, said, headers = self.served.ask("POST", "/v1/order", {"kind": "order"})
        self.assertEqual(status, 429)
        self.assertEqual((said["status"], said["code"], said["sent"]), ("busy", "gateway_starting", False))
        self.assertIn("still reading the accounts its keys trade", said["detail"])
        self.assertIn("send it again in 5 seconds", said["detail"])
        self.assertEqual(headers.get("Retry-After"), str(server.STARTING_RETRY_AFTER_S))
        self.assertEqual(self.gw.orders, [])

    def test_a_reading_that_failed_is_said_in_health(self):
        self.starting.failed(RuntimeError("eth_call: rate limited 7 times in a row"))
        _, said, _ = self.served.ask("GET", "/v1/health")
        self.assertEqual((said["ready"], said["start_failed_attempts"]), (False, 1))
        self.assertIn("rate limited", said["start_last_error"])

    def test_only_the_order_waits_for_the_keys(self):
        # No contacts and no faucet here: each answers as it would at any time, not `gateway_starting`.
        self.assertEqual(self.served.ask("GET", "/v1/contacts?pool=0x" + "ab" * 20)[1]["code"], "no_contacts_here")
        self.assertEqual(self.served.ask("POST", "/v1/faucet", {"address": "0x" + "ab" * 20})[1]["code"], "no_faucet")

    def test_once_ready_health_says_so_and_orders_go_to_the_gateway(self):
        self.starting.done()
        status, said, _ = self.served.ask("GET", "/v1/health")
        self.assertEqual((status, said["ready"]), (200, True))
        self.assertNotIn("starting", said)
        self.assertNotIn("starting_for_s", said)
        status, said, _ = self.served.ask("POST", "/v1/order", {"kind": "order"})
        self.assertEqual((status, said["status"]), (200, "submitted"))
        self.assertEqual(self.gw.orders, [{"kind": "order"}])


class FakeProtector:
    def __init__(self, starting=None, fail_names=False):
        self.starting = starting
        self.fail_names = fail_names
        self.events = []
        self.swept = threading.Event()

    def take_on(self, keys):
        self.events.append(("take_on", list(keys), self.starting.ready() if self.starting else None))
        return ["0x" + "cd" * 20]

    def check_names(self, assets):
        if self.fail_names:
            raise RuntimeError("info: 429")
        self.events.append(("check_names", sorted(assets)))
        return {a: "ok" for a in assets}

    def run(self, every, stop, hot_every):
        self.events.append(("run", every, stop, hot_every))
        self.swept.set()


class FakeGateway:
    def __init__(self, protector):
        self.protector = protector


class Reader:
    def allowed_assets(self, account):
        return {3, 4}


class Signer:
    """A signer whose list of keys fails `fails` times before it answers."""

    def __init__(self, fails=0):
        self.fails = fails

    def addresses(self):
        if self.fails:
            self.fails -= 1
            raise RuntimeError("signer: list refused")
        return ["0x" + "11" * 20, "0x" + "22" * 20]


def quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(*args, **kwargs)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]


class TheReadingAfterTheStart(unittest.TestCase):
    def test_the_keys_are_read_before_the_sweep_starts_and_ready_comes_after_both(self):
        starting = Starting()
        p = FakeProtector(starting)
        logged = quiet(take_on_at_start, FakeGateway(p), Reader(), Signer(), starting, 30.0, 15.0)
        self.assertTrue(p.swept.wait(5))
        self.assertEqual(p.events[0], ("take_on", ["0x" + "11" * 20, "0x" + "22" * 20], False),
                         "not ready while the keys are read")
        self.assertEqual(p.events[1], ("check_names", [3, 4]))
        self.assertEqual(p.events[2], ("run", 30.0, None, 15.0))
        self.assertTrue(starting.ready())
        self.assertEqual([e["event"] for e in logged], ["protect_took_on", "market_names", "ready"])

    def test_a_reading_that_fails_as_a_whole_is_tried_again_1_2_4_seconds_apart_up_to_the_cap(self):
        starting = Starting()
        p = FakeProtector(starting)
        waited = []
        logged = quiet(take_on_at_start, FakeGateway(p), Reader(), Signer(fails=5), starting, 30.0, 15.0,
                       wait=waited.append, retry_max_s=4.0)
        self.assertEqual(waited, [1.0, 2.0, 4.0, 4.0, 4.0])
        self.assertEqual(starting.failed_attempts, 5)
        self.assertEqual([e["event"] for e in logged].count("protect_take_on_retry"), 5)
        self.assertTrue(p.swept.wait(5))
        self.assertTrue(starting.ready())
        self.assertEqual(starting.summary(), {"ready": True})

    def test_no_sweep_and_no_ready_while_the_reading_fails(self):
        starting = Starting()
        p = FakeProtector(starting)

        class Stop(Exception):
            pass

        def wait(s):
            raise Stop  # the third failure ends the test, not the loop

        signer = Signer(fails=10)
        with self.assertRaises(Stop):
            quiet(take_on_at_start, FakeGateway(p), Reader(), signer, starting, 30.0, 15.0, wait=wait)
        self.assertEqual(p.events, [])
        self.assertFalse(starting.ready())

    def test_names_that_cannot_be_read_do_not_hold_the_start(self):
        starting = Starting()
        p = FakeProtector(starting, fail_names=True)
        logged = quiet(take_on_at_start, FakeGateway(p), Reader(), Signer(), starting, 30.0, 15.0)
        self.assertTrue(p.swept.wait(5))
        self.assertTrue(starting.ready())
        self.assertIn("market_names_failed", [e["event"] for e in logged])

    def test_a_signer_with_no_list_of_keys_takes_nothing_on_and_is_ready(self):
        starting = Starting()
        p = FakeProtector(starting)
        logged = quiet(take_on_at_start, FakeGateway(p), Reader(), object(), starting, 30.0, 15.0)
        self.assertTrue(p.swept.wait(5))
        self.assertEqual([e[0] for e in p.events], ["check_names", "run"], "no take_on without keys")
        self.assertEqual(logged[0], {**logged[0], "event": "protect_took_on", "accounts": [], "keys_listed": False})
        self.assertTrue(starting.ready())


class EndToEnd(unittest.TestCase):
    def test_health_answers_while_the_keys_are_being_read_and_says_ready_after(self):
        release = threading.Event()
        starting = Starting()

        class Slow(FakeProtector):
            def take_on(self, keys):
                release.wait(10)
                return super().take_on(keys)

        p = Slow(starting)
        served = Served(Stub(), starting)
        self.addCleanup(served.close)
        out = io.StringIO()
        with redirect_stdout(out):
            t = threading.Thread(target=take_on_at_start, args=(FakeGateway(p), Reader(), Signer(), starting, 30.0, 15.0))
            t.start()
            self.assertEqual(served.ask("GET", "/v1/health")[1]["ready"], False)
            self.assertEqual(served.ask("POST", "/v1/order", {"kind": "order"})[1]["code"], "gateway_starting")
            release.set()
            t.join(10)
        self.assertEqual(served.ask("GET", "/v1/health")[1]["ready"], True)


class UnreadKeys(unittest.TestCase):
    def test_a_key_the_node_would_not_read_is_counted_until_a_sweep_reads_it(self):
        class Refusing:
            def account_of(self, key):
                raise RuntimeError("eth_call: rate limited 3 times in a row")

        p = Protector(Refusing(), object(), lambda *a: (None, True, {}))
        quiet(p.take_on, ["0x" + "11" * 20, "0x" + "22" * 20])
        self.assertEqual(p.unread_keys(), 2)
        self.assertEqual(server.live_summary(p)["keys_unread"], 2)


class TheCheckAfterADeploy(unittest.TestCase):
    def test_a_gateway_still_reading_its_keys_fails_the_check_after_a_deploy(self):
        import os
        import pathlib
        import subprocess
        import sys
        import tempfile
        root = pathlib.Path(server.__file__).resolve().parents[1]
        saved = pathlib.Path(tempfile.mkdtemp()) / "health.json"
        saved.write_text(json.dumps({"ok": True, "chain_id": 998, "commit": "ab" * 20, "ready": False,
                                     "starting_for_s": 3.2}) + "\n")
        out = subprocess.run(["bash", str(root / "scripts" / "verify-gateway-deploy.sh"), saved.as_uri()],
                             env={**os.environ, "PYTHON": sys.executable}, capture_output=True, text=True)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("FAIL the gateway has just started and still reads its keys' accounts", out.stdout)


class Wiring(unittest.TestCase):
    def test_main_binds_the_server_before_it_reads_a_key(self):
        body = inspect.getsource(server.main)
        self.assertIn("starting = Starting()", body)
        self.assertIn("starting=starting))", body)
        self.assertLess(body.index("server = BoundedServer("), body.index("target=take_on_at_start"))
        self.assertLess(body.index("target=take_on_at_start"), body.index("server.serve_forever()"))
        self.assertNotIn("check_names(", body)


if __name__ == "__main__":
    unittest.main()
