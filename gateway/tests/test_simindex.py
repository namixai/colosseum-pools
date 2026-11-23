"""The sim pools' event index (gateway/simindex.py). Offline: the node is a fake that holds a few logs.

    spike/.venv/bin/python -m unittest gateway.tests.test_simindex
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from gateway import simindex
from gateway.chain import Throttled
from gateway.server import BoundedServer

FACTORY = "0x00000000000000000000000000000000000000F1"
POOL = "0x00000000000000000000000000000000000000A1"
OTHER = "0x00000000000000000000000000000000000000B2"
PARTNER = "0x00000000000000000000000000000000000000C3"
AUTHOR = "0x00000000000000000000000000000000000000D4"
CODE = "0x" + "ab" * 32


def word(v) -> str:
    if isinstance(v, str):
        return "0x" + v[2:].lower().rjust(64, "0")
    return "0x" + format(v, "064x")


def raw(address, name, topics, data_words=(), block=0, index=0):
    return {"address": address, "topics": [simindex.TOPICS[name], *[word(t) for t in topics]],
            "data": "0x" + "".join(word(w)[2:] for w in data_words), "blockNumber": hex(block),
            "logIndex": hex(index), "transactionHash": "0x" + format(block * 1000 + index, "064x")}


class Node:
    """A node with logs, a newest block and a widest window; `fail` raises once per entry before a read."""

    def __init__(self, logs, head, widest=10_000):
        self.all, self.head, self.widest = logs, head, widest
        self.fail: list[Exception] = []
        self.asked: list[tuple] = []

    def block_number(self):
        if self.fail:
            raise self.fail.pop(0)
        return self.head

    def logs(self, addresses, topics, start, end):
        self.asked.append((tuple(addresses), start, end))
        if end - start + 1 > self.widest:
            raise simindex.WindowRefused("eth_getLogs: block range too large")
        lower = {a.lower() for a in addresses}
        return [log for log in self.all if log["address"].lower() in lower and log["topics"][0] in topics
                and start <= int(log["blockNumber"], 16) <= end]


def history():
    return [
        raw(FACTORY, "PoolCreated", [POOL, PARTNER], [5], block=105),
        raw(POOL, "CodeSet", [CODE, PARTNER, AUTHOR], [4000, 1000], block=107, index=1),
        raw(OTHER, "CodeSet", [CODE, PARTNER, AUTHOR], [4000, 0], block=108),  # not a pool of this factory
        raw(POOL, "SharePaid", [CODE, PARTNER], [123456, 1], block=2500),
        raw(POOL, "PayoutStopped", [7, PARTNER], [1, 2, 3, 4, 5, 6, 7, 8], block=2600),
        raw(POOL, "StopLifted", [7], [1, 1, 0], block=2700),
    ]


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def make(node, tmp, **kw):
    return simindex.SimIndex(node, 998, FACTORY, 100, pathlib.Path(tmp) / "idx.json", lag=0, pause_s=0,
                             clock=kw.pop("clock", Clock()), **kw)


def read_all(index):
    while index.step():
        pass


class Decode(unittest.TestCase):
    def test_each_event_is_kept_with_the_fields_the_panels_look_up(self):
        logs = [simindex.decode(log) for log in history()]
        self.assertEqual(logs[0]["pool"], POOL)
        self.assertEqual((logs[1]["code"], logs[1]["partner"], logs[1]["author"], logs[1]["bps"], logs[1]["authorBps"]),
                         (CODE, PARTNER, AUTHOR, 4000, 1000))
        self.assertEqual((logs[3]["to"], logs[3]["amount"], logs[3]["sent"]), (PARTNER, "123456", True))
        self.assertEqual((logs[4]["id"], logs[4]["trader"]), (7, PARTNER))
        self.assertEqual(logs[5]["id"], 7)

    def test_a_log_of_another_shape_is_dropped_not_guessed(self):
        self.assertIsNone(simindex.decode({"topics": ["0x" + "11" * 32]}))
        self.assertIsNone(simindex.decode({**history()[1], "topics": [simindex.TOPICS["CodeSet"]]}))
        self.assertIsNone(simindex.decode({"address": POOL}))

    def test_topics_are_the_contract_events(self):
        # The signatures are the events of src/sim/SimPool.sol and SimPoolFactory.sol, as the app's ABI has them.
        src = (pathlib.Path(__file__).resolve().parents[2] / "src/sim/SimPool.sol").read_text()
        for name in ("CodeSet", "SharePaid", "PayoutStopped", "StopLifted", "StopFinal"):
            self.assertIn(f"event {name}(", src)
        self.assertEqual(simindex.TOPICS["CodeSet"],
                         "0x" + simindex.keccak(text="CodeSet(bytes32,address,uint16,address,uint16)").hex())


class Reading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_pool_made_in_a_window_has_its_events_of_that_window(self):
        node = Node(history(), head=3000)
        index = make(node, self.tmp)
        read_all(index)
        status, body = index.pool(POOL)
        self.assertEqual(status, 200)
        self.assertEqual([e["name"] for e in body["events"]], ["CodeSet", "SharePaid", "PayoutStopped", "StopLifted"])
        self.assertEqual(body["created"], 105)
        self.assertEqual(body["scanned_to"], 3000)
        # The address that is not the factory's pool is not kept.
        self.assertEqual(index.pool(OTHER)[0], 404)

    def test_a_window_the_node_refuses_is_halved_and_the_reading_goes_on(self):
        node = Node(history(), head=3000, widest=300)
        index = make(node, self.tmp)
        read_all(index)
        self.assertEqual(index.window, 250)
        self.assertEqual(len(index.pool(POOL)[1]["events"]), 4)
        self.assertTrue(all(end - start + 1 <= 1000 for _, start, end in node.asked))

    def test_the_window_grows_back_after_a_run_of_good_reads(self):
        node = Node([], head=200_000, widest=500)
        index = make(node, self.tmp)
        index.step()
        self.assertEqual(index.window, 500)
        node.widest = 10_000
        for _ in range(simindex.GROW_AFTER):
            index.step()
        self.assertEqual(index.window, 1000)

    def test_a_window_refused_at_one_block_is_an_error_and_nothing_is_skipped(self):
        node = Node(history(), head=3000, widest=0)
        index = make(node, self.tmp)
        with self.assertRaises(simindex.WindowRefused):
            index.step()
        self.assertEqual(index.scanned_to, 99)

    def test_a_throttled_window_is_waited_out_and_asked_for_narrower(self):
        node = Node(history(), head=3000)
        index = make(node, self.tmp)
        calls = {"n": 0}
        logs = node.logs

        def throttled_once(*a):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Throttled("eth_getLogs: rate limited")
            return logs(*a)

        node.logs = throttled_once
        with self.assertRaises(Throttled):
            index.step()
        self.assertEqual((index.window, index.scanned_to), (500, 99))
        read_all(index)
        self.assertEqual(len(index.pool(POOL)[1]["events"]), 4)

    def test_the_lag_leaves_the_newest_blocks_for_the_next_pass(self):
        node = Node(history(), head=3000)
        index = make(node, self.tmp)
        index.lag = 5
        read_all(index)
        self.assertEqual(index.scanned_to, 2995)

    def test_a_restart_reads_on_from_the_file_and_keeps_each_event_once(self):
        node = Node(history(), head=2550)
        clock = Clock()
        first = make(node, self.tmp, clock=clock)
        read_all(first)
        first.save()
        node.head = 3000
        second = make(node, self.tmp, clock=clock)
        self.assertEqual(second.load(), "loaded")
        self.assertEqual(second.scanned_to, 2550)
        node.asked.clear()
        read_all(second)
        self.assertEqual(node.asked[0][1], 2551)
        # The same window read again (a restart before the save) keeps each event once.
        second.scanned_to = 100
        read_all(second)
        self.assertEqual([e["name"] for e in second.pool(POOL)[1]["events"]],
                         ["CodeSet", "SharePaid", "PayoutStopped", "StopLifted"])

    def test_a_file_of_another_factory_or_a_broken_one_is_set_aside(self):
        path = pathlib.Path(self.tmp) / "idx.json"
        path.write_text(json.dumps({"version": 1, "chain_id": 998, "factory": OTHER, "from_block": 100,
                                    "scanned_to": 5, "pools": {}, "events": {}}))
        index = make(Node([], head=10), self.tmp)
        self.assertEqual(index.load(), "other")
        self.assertEqual(index.scanned_to, 99)
        path.write_text("{not json")
        self.assertEqual(make(Node([], head=10), self.tmp).load(), "bad")
        self.assertFalse(path.exists())
        self.assertEqual(len(list(pathlib.Path(self.tmp).iterdir())), 2)

    def test_the_file_is_whole_after_each_save(self):
        node = Node(history(), head=3000)
        index = make(node, self.tmp)
        read_all(index)
        data = json.loads((pathlib.Path(self.tmp) / "idx.json").read_text())
        self.assertEqual(data["scanned_to"], 3000)
        self.assertEqual(list(data["pools"]), [POOL])
        self.assertFalse((pathlib.Path(self.tmp) / "idx.json.tmp").exists())

    def test_a_lost_node_is_waited_out_and_the_reading_goes_on_from_the_same_block(self):
        node = Node(history(), head=3000)
        node.fail = [ConnectionError("down"), Throttled("busy")]
        index = make(node, self.tmp)
        stop = threading.Event()
        waits = []

        def wait(s):
            waits.append(s)
            if index.scanned_to >= 3000:
                stop.set()
            return stop.is_set()

        stop.wait = wait
        index.run(stop)
        self.assertEqual(waits[:2], [1.0, 2.0])
        self.assertEqual(index.errors, 2)
        self.assertIsNone(index.last_error)
        self.assertEqual(index.health()["behind_blocks"], 0)


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.index = make(Node(history(), head=3000), cls.tmp)
        read_all(cls.index)
        cls.server = BoundedServer(("127.0.0.1", 0), simindex.make_handler(cls.index, "https://site.example"))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, method="GET", body=None):
        req = urllib.request.Request(self.base + path, method=method, data=body)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, dict(err.headers), json.loads(err.read())

    def test_health_says_how_far_it_has_read(self):
        status, headers, body = self.get("/v1/simindex/health")
        self.assertEqual(status, 200)
        self.assertEqual((body["scanned_to"], body["head"], body["behind_blocks"], body["pools"]), (3000, 3000, 0, 1))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://site.example")

    def test_a_pool_is_answered_with_its_events(self):
        status, _, body = self.get(f"/v1/simindex/pool?pool={POOL.lower()}")
        self.assertEqual(status, 200)
        self.assertEqual(body["pool"], POOL)
        self.assertEqual(body["events"][0]["code"], CODE)

    def test_bad_and_unknown_pools(self):
        self.assertEqual(self.get("/v1/simindex/pool?pool=nope")[0], 400)
        # `$` would let a trailing newline through; the address is matched whole.
        self.assertEqual(self.get(f"/v1/simindex/pool?pool={POOL}%0A")[0], 400)
        self.assertEqual(self.index.pool(POOL + "\n")[0], 400)
        status, _, body = self.get(f"/v1/simindex/pool?pool={OTHER}")
        self.assertEqual((status, body["code"]), (404, "unknown_pool"))
        self.assertEqual(self.get("/v1/simindex/other")[0], 404)

    def test_nothing_is_taken_but_get(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, _, body = self.get("/v1/simindex/pool", method=method, body=b"{}")
            self.assertEqual((status, body["code"]), (405, "read_only"), method)


class ReviewRoute(unittest.TestCase):
    """GET /v1/simindex/reviews: the review service's documents, found by a pool this index knows and whole numbers."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.index = make(Node(history(), head=3000), cls.tmp)
        read_all(cls.index)
        cls.folder = pathlib.Path(tempfile.mkdtemp())
        (cls.folder / f"{POOL}-3.json").write_text(json.dumps({"schema": "payout-review/4", "id": 3}))
        (cls.folder / f"{POOL.lower()}-4.json").write_text(json.dumps({"schema": "payout-review/4", "id": 4}))
        (cls.folder / f"{POOL}-5.json").write_text("{not json")
        (cls.folder / f"{POOL}-6.json").write_text("[1]")
        (cls.folder / "said.json").write_text(json.dumps({"secret": True}))
        outside = pathlib.Path(tempfile.mkdtemp()) / "x.json"
        outside.write_text(json.dumps({"outside": True}))
        (cls.folder / f"{POOL}-7.json").symlink_to(outside)
        cls.server = BoundedServer(("127.0.0.1", 0), simindex.make_handler(
            cls.index, "https://site.example", reviews=simindex.Reviews(cls.folder)))
        cls.bare = BoundedServer(("127.0.0.1", 0), simindex.make_handler(cls.index, "https://site.example"))
        for s in (cls.server, cls.bare):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.bare_base = f"http://127.0.0.1:{cls.bare.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.server, cls.bare):
            s.shutdown()
            s.server_close()

    get = Routes.get

    def test_the_documents_of_the_ids_asked_as_written(self):
        status, headers, body = self.get(f"/v1/simindex/reviews?pool={POOL.lower()}&ids=3,4,8")
        self.assertEqual(status, 200)
        self.assertEqual((body["pool"], body["chain_id"]), (POOL, self.index.chain_id))
        self.assertEqual(body["reviews"], {"3": {"schema": "payout-review/4", "id": 3, "reasons": [], "call": None},
                                           "4": {"schema": "payout-review/4", "id": 4, "reasons": [], "call": None}})
        self.assertEqual(body["missing"], [8])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_outside_the_host_only_what_the_page_decides_by_no_evidence_no_draft_no_other_trader(self):
        other, wallet = "0x" + "ab" * 20, "0x" + "cd" * 20
        doc = {"schema": "payout-review/4", "pool": POOL, "id": 9, "trader": "0x" + "11" * 20, "amount": "5",
               "claimedAt": 1, "dueAt": 2, "hardAt": 3, "verdict": "suspicious", "action": "stop", "chainId": 998,
               "readAt": {"block": 5, "time": 6}, "batchApproval": False,
               "reasons": [{"kind": "pair", "text": f"pair on the stage with {other}"},
                           {"kind": "link", "text": f"wallet linked to {wallet}"}],
               "evidence": {"pairs": [{"account": other}], "links": [{"trader": wallet}],
                            "passRates": {"passes": {"yellow": 24}}},
               "stopDraft": {"reason": 2, "paired": other},
               "call": {"kind": "stop", "to": POOL, "method": "stopPayout", "signature": "stopPayout(...)",
                        "args": [9, 2, other, [], []], "data": "0x" + other[2:]}}
        got = simindex.outward(doc)
        text = json.dumps(got)
        self.assertNotIn(other[2:], text)
        self.assertNotIn(wallet[2:], text)
        for gone in ("evidence", "stopDraft", "passRates", "yellow"):
            self.assertNotIn(gone, text)
        self.assertEqual(got["reasons"], [{"kind": "pair", "text": simindex.REASON_WORDS["pair"]},
                                          {"kind": "link", "text": simindex.REASON_WORDS["link"]}])
        self.assertEqual(got["call"], {"kind": "stop", "to": POOL, "method": "stopPayout", "signature": "stopPayout(...)"})
        for k in ("pool", "id", "trader", "amount", "claimedAt", "dueAt", "hardAt", "verdict", "action", "readAt", "chainId"):
            self.assertEqual(got[k], doc[k], k)
        approve = {**doc, "action": "approve", "call": {"kind": "approve", "to": POOL, "method": "approvePayouts",
                                                         "signature": "approvePayouts(uint256[])", "args": [[9]], "data": "0x01"}}
        self.assertEqual(simindex.outward(approve)["call"]["args"], [[9]], "an approval's own call, the payout's number")

    def test_a_broken_file_or_a_link_out_of_the_folder_is_not_handed_over(self):
        _, _, body = self.get(f"/v1/simindex/reviews?pool={POOL}&ids=5,6,7")
        self.assertEqual(body["reviews"], {})
        self.assertEqual(sorted(body["unreadable"]), ["5", "6"])
        self.assertEqual(body["missing"], [7], "a symbolic link is no review")

    def test_only_a_known_pool_and_whole_numbers(self):
        for q in ("pool=nope&ids=1", f"pool={POOL}&ids=", f"pool={POOL}&ids=../said", f"pool={POOL}&ids=1%0A",
                  f"pool={POOL}&ids=-1", f"pool={POOL}&ids=" + ",".join(str(i) for i in range(101))):
            self.assertEqual(self.get(f"/v1/simindex/reviews?{q}")[0], 400, q)
        status, _, body = self.get(f"/v1/simindex/reviews?pool={OTHER}&ids=3")
        self.assertEqual((status, body["code"]), (404, "unknown_pool"))

    def test_without_a_folder_there_is_no_route_and_nothing_is_written(self):
        self.assertEqual(self.get(f"/v1/simindex/reviews?pool={POOL}&ids=3")[0], 200)
        req = urllib.request.Request(f"{self.bare_base}/v1/simindex/reviews?pool={POOL}&ids=3")
        with self.assertRaises(urllib.error.HTTPError) as got:
            urllib.request.urlopen(req, timeout=5)
        with got.exception as err:
            self.assertEqual((err.code, json.loads(err.read())["code"]), (404, "reviews_off"))
        status, _, body = self.get("/v1/simindex/reviews", method="POST", body=b"{}")
        self.assertEqual((status, body["code"]), (405, "read_only"))

    def test_the_folder_comes_from_the_settings(self):
        base = {"SIM_INDEX_FACTORY": FACTORY, "SIM_INDEX_FROM_BLOCK": "1"}
        self.assertIsNone(simindex.settings(base, pathlib.Path("."))["reviews"])
        cfg = simindex.settings({**base, "SIM_INDEX_REVIEWS": "/var/lib/colosseum-keeper/reviews-sim1"}, pathlib.Path("."))
        self.assertEqual(cfg["reviews"], pathlib.Path("/var/lib/colosseum-keeper/reviews-sim1"))


class Settings(unittest.TestCase):
    def test_the_record_names_the_factory_and_its_first_block(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        cfg = simindex.settings({"SIM_INDEX_RECORD": "deployments/testnet-rehearsal998.json",
                                 "SIM_INDEX_DIR": "/tmp/x"}, root)
        record = json.loads((root / "deployments/testnet-rehearsal998.json").read_text())
        self.assertEqual(cfg["factory"], record["SimPoolFactory"])
        self.assertEqual(cfg["from_block"], record["block"])
        self.assertEqual(cfg["bind"], "127.0.0.1:8789")
        self.assertTrue(str(cfg["path"]).startswith("/tmp/x/sim-index-998-"))

    def test_no_factory_or_a_bad_number_stops_the_start(self):
        with self.assertRaises(SystemExit):
            simindex.settings({}, pathlib.Path("."))
        with self.assertRaises(SystemExit):
            simindex.settings({"SIM_INDEX_FACTORY": FACTORY, "SIM_INDEX_FROM_BLOCK": "x"}, pathlib.Path("."))
        with self.assertRaises(SystemExit):
            simindex.settings({"SIM_INDEX_FACTORY": FACTORY, "SIM_INDEX_FROM_BLOCK": "1", "SIM_INDEX_WINDOW": "0"},
                              pathlib.Path("."))


class RpcAnswers(unittest.TestCase):
    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self):
            return self._body

    class Session:
        def __init__(self, replies):
            self.replies = list(replies)

        def post(self, url, json=None, timeout=None):
            return self.replies.pop(0)

    def test_a_logs_error_is_a_refused_window_and_a_throttle_is_waited_out(self):
        err = self.Reply(200, {"error": {"code": -32000, "message": "query exceeds max block range"}})
        rpc = simindex.Rpc("http://x", session=self.Session([err]), sleep=lambda s: None)
        with self.assertRaises(simindex.WindowRefused):
            rpc.logs([POOL], [], 1, 2)
        busy = self.Reply(429, None)
        ok = self.Reply(200, {"result": "0x10"})
        rpc = simindex.Rpc("http://x", session=self.Session([busy, ok]), sleep=lambda s: None)
        self.assertEqual(rpc.block_number(), 16)


if __name__ == "__main__":
    unittest.main()
