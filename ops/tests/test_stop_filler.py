"""The stops' loop and its watch (ops/stop_filler.py): a stop reached is sent in the step that sees it, nothing waits on
anything else, a busy node costs a look and no block, and a reach with no fill is an alarm. On a node made up here."""
import json
import pathlib
import re
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from eth_abi import decode, encode
from eth_account import Account
from hexbytes import HexBytes
from eth_utils import keccak, to_checksum_address

from ops import payout_review as pr
from ops import sim_keeper as sk
from ops import stop_filler as sf
from spike.hlspike import common as c

ROOT = pathlib.Path(__file__).resolve().parents[2]
EXAM = to_checksum_address("0x" + "11" * 20)
OTHER = to_checksum_address("0x" + "12" * 20)
POOL = to_checksum_address("0x" + "55" * 20)
FILLER = Account.from_key("0x" + "42" * 32)  # made up here; no key directory is read
BTC = 3
SEL = {name: "0x" + keccak(text=name)[:4].hex() for name in ("positionOf(uint32)", "fillTrigger(uint32,bool)")}


def word(n: int) -> str:
    return "0x" + n.to_bytes(32, "big").hex()


def ev(topic: str, address: str, block: int, data: bytes, *topics: str, tx: str | None = None, index: int = 0) -> dict:
    return {"address": address, "topics": [topic, *topics], "data": "0x" + data.hex(), "blockNumber": hex(block),
            "transactionHash": tx or f"0x{block:064x}", "logIndex": hex(index)}


def set_log(block, px, armed, asset=BTC, take=False, exam=EXAM, tx=None, index=0):
    return ev(sk.TRIGGER_SET, exam, block, encode(["bool", "uint64", "uint64", "uint256"], [take, px, 0, armed]),
              word(asset), tx=tx, index=index)


def cleared_log(block, asset=BTC, take=False, exam=EXAM, tx=None, index=0):
    return ev(sk.TRIGGER_CLEARED, exam, block, encode(["bool"], [take]), word(asset), tx=tx, index=index)


def filled_log(block, asset=BTC, exam=EXAM, tx=None, index=0, buy=False):
    return ev(sk.FILLED, exam, block, encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"],
                                             [buy, 100, 940_000, 0, 0, 0]), word(asset), tx=tx, index=index)


def trigger_filled_log(block, asset=BTC, take=False, exam=EXAM, tx=None, index=0):
    return ev(sk.TRIGGER_FILLED, exam, block, encode(["bool", "uint64", "uint64", "uint256", "uint64", "uint8"],
                                                     [take, 100, 940_000, 0, 950_000, 0]), word(asset), tx=tx, index=index)


def fill_of_stop(block, asset=BTC, take=False, exam=EXAM):
    """What `fillTrigger` writes, in its order: the clearing, the fill, and TriggerFilled, in one transaction."""
    tx = f"0x{block:062x}ff"
    return [cleared_log(block, asset, take, exam, tx, 0), filled_log(block, asset, exam, tx, 1),
            trigger_filled_log(block, asset, take, exam, tx, 2)]


class Node:
    """A chain as the loop reads it: a head, the oracle price of each block, positions, logs, receipts and sends."""

    def __init__(self):
        self.url = "http://127.0.0.1:9"
        self.head = 100
        self.price: dict[int, int] = {}  # block -> BTC's oracle price; a block not here reads the last one before it
        self.positions: dict[tuple[str, int], int] = {(EXAM, BTC): 10}
        self.logs: list[dict] = []
        self.receipts: dict[str, dict] = {}
        self.raw: list[str] = []
        self.calls: list[tuple[str, object]] = []
        self.busy: set[str] = set()
        self.refuse_fill = False
        self.unreadable: set[int] = set()
        self.count = 7

    def oracle(self, block: int) -> int:
        past = [b for b in self.price if b <= block]
        return self.price[max(past)] if past else 1_000_000

    def call(self, method, params=()):
        self.calls.append((method, params))
        if method in self.busy:
            raise c.NodeBusy(f"{method}: HTTP 429")
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_chainId":
            return hex(c.CHAIN_ID)
        if method == "eth_gasPrice":
            return hex(100_000_000)
        if method == "eth_getTransactionCount":
            return hex(self.count)
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            wanted = {a.lower() for a in q["address"]}
            topics = set(q["topics"][0])
            return [l for l in self.logs if lo <= int(l["blockNumber"], 16) <= hi and l["address"].lower() in wanted
                    and l["topics"][0] in topics]
        if method == "eth_sendRawTransaction":
            self.raw.append(params[0])
            return "0x" + keccak(hexstr=params[0]).hex()
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_call":
            q, tag = params
            data = q["data"]
            if q["to"].lower() == sf.ORACLE_PX.lower():
                block = int(tag, 16)
                if block in self.unreadable:
                    raise RuntimeError("header not found")
                return "0x" + encode(["uint64"], [self.oracle(block)]).hex()
            if data.startswith(SEL["positionOf(uint32)"]):
                asset = decode(["uint32"], bytes.fromhex(data[10:]))[0]
                return "0x" + encode(["int256"], [self.positions.get((to_checksum_address(q["to"]), asset), 0)]).hex()
            if data.startswith(SEL["fillTrigger(uint32,bool)"]):
                if self.refuse_fill:
                    raise RuntimeError("execution reverted: TooFarFromOracle")
                return "0x"
        raise AssertionError(f"the loop asked the node for {method} {params}")

    def sent(self) -> list[dict]:
        """The transactions sent, decoded: to, nonce, tip and the call."""
        from eth_account.typed_transactions import TypedTransaction
        out = []
        for raw in self.raw:
            d = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
            out.append({"to": to_checksum_address(d["to"]), "nonce": d["nonce"], "tip": d["maxPriorityFeePerGas"],
                        "gas": d["gas"], "data": "0x" + bytes(d["data"]).hex()})
        return out


class Keeper:
    """What the loop reads of the keeper's: its accounts, pools, the done ones, the sender, whether it is the filler."""

    def __init__(self):
        self.accounts = {EXAM: {"pool": POOL, "kind": "exam"}}
        self.pools = [POOL]
        self.done: set[str] = set()
        self.sender = FILLER.address
        self.filler = True

    def is_filler(self):
        return self.filler


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.node = Node()
        self.keeper = Keeper()
        self.said: list[tuple[str, dict]] = []
        patcher = mock.patch.object(sf, "log", side_effect=lambda e, **f: self.said.append((e, f)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alerts: list[str] = []

    def loop(self, wallet=FILLER, tip=0, **kw) -> sf.StopLoop:
        alarm = sf.Alarm(self.dir / "stops.alarm", "")
        alarm._send = lambda line, where: self.alerts.append(line)
        alarm.command = "cat"  # the channel; _send stands in for it
        nonces = sf.Nonces(FILLER.address) if wallet is not None else None
        loop = sf.StopLoop(self.keeper, self.node, wallet=wallet, nonces=nonces, tip_wei=tip,
                           journal=sf.PriceJournal(self.dir / "prices.json"), alarm=alarm, **kw)
        return loop

    def run_to(self, loop, *heads):
        out = []
        for h in heads:
            self.node.head = h
            out.append(loop.step(h))
        return out

    def events(self, name):
        return [f for e, f in self.said if e == name]

    def armed_stop(self, loop, px=950_000, armed=101, at=100):
        """A stop on the long BTC position, set in block `at`, as the loop's own logs see it."""
        self.node.logs.append(set_log(at, px, armed))
        self.run_to(loop, at)
        self.assertIn((EXAM, BTC, False), loop.triggers)


class TheEvents(unittest.TestCase):
    def test_the_topics_are_the_contracts_own(self):
        src = (ROOT / "src/exam/PaperExam.sol").read_text()
        self.assertIn("event Passed(address indexed trader, int256 equity);", src)
        self.assertIn("event Stopped(Status indexed status, Breach indexed reason, int256 equity);", src)
        self.assertEqual(sf.PASSED, "0x" + keccak(text="Passed(address,int256)").hex())
        self.assertEqual(sf.STOPPED, "0x" + keccak(text="Stopped(uint8,uint8,int256)").hex())
        self.assertIn("mapping(uint32 asset => int256) public positionOf;", src)

    def test_a_reach_is_the_contracts(self):
        src = (ROOT / "src/exam/PaperExam.sol").read_text()
        self.assertIn("bool hit = isTake == (pos > 0) ? oracle >= t.px : oracle <= t.px;", src)
        self.assertTrue(sf.hit(False, 1, 950, 950), "a stop on a long at its price")
        self.assertFalse(sf.hit(False, 1, 951, 950))
        self.assertTrue(sf.hit(True, 1, 1_100, 1_100), "a take on a long at its price")
        self.assertFalse(sf.hit(True, 1, 1_099, 1_100))
        self.assertTrue(sf.hit(False, -1, 1_050, 1_050), "a stop on a short at its price")
        self.assertFalse(sf.hit(False, -1, 1_049, 1_050))
        self.assertTrue(sf.hit(True, -1, 900, 900), "a take on a short at its price")
        self.assertFalse(sf.hit(False, 0, 1, 950), "no position, no reach")
        self.assertFalse(sf.hit(False, 1, 0, 950), "no price, no reach")

    def test_the_oracles_index_is_the_contracts(self):
        src = (ROOT / "src/lib/CoreOps.sol").read_text()
        self.assertIn("return asset >= HIP3_BASE ? asset - HIP3_BASE : asset;", src)
        self.assertEqual((sf.px_index(3), sf.px_index(sf.HIP3_BASE + 7)), (3, 7))
        self.assertEqual(sf.STOP_GAS, sk.FILL_GAS)


class CarryingOut(Base):
    def test_a_stop_reached_is_sent_in_the_step_that_sees_it_with_no_call_before_and_no_wait_after(self):
        loop = self.loop(tip=2 * sf.GWEI)
        self.armed_stop(loop)
        self.node.price[101] = 960_000
        self.assertEqual(self.run_to(loop, 101), [[]], "armed, not reached")
        self.node.price[102] = 949_000
        before = len(self.node.calls)
        self.assertEqual(self.run_to(loop, 102), [[(EXAM, BTC, False)]])
        methods = [m for m, _ in self.node.calls[before:]]
        self.assertNotIn("eth_estimateGas", methods)
        self.assertFalse([p for m, p in self.node.calls[before:] if m == "eth_call" and p[0]["data"].startswith(
            SEL["fillTrigger(uint32,bool)"])], "no eth_call of fillTrigger: the price is compared here")
        self.assertEqual(methods.count("eth_sendRawTransaction"), 1)
        self.assertNotIn("eth_getTransactionReceipt", methods, "its receipt is not waited for")
        tx = self.node.sent()[0]
        self.assertEqual((tx["to"], tx["nonce"], tx["tip"], tx["gas"]), (EXAM, 7, 2 * sf.GWEI, sk.FILL_GAS))
        self.assertTrue(tx["data"].startswith(SEL["fillTrigger(uint32,bool)"]))
        before = len(self.node.calls)
        self.assertEqual(self.run_to(loop, 103), [[]], "in flight: not again")
        self.assertIn("eth_getTransactionReceipt", [m for m, _ in self.node.calls[before:]], "looked at in the next block")
        self.assertEqual(len(self.node.raw), 1)

    def test_one_not_yet_armed_waits_for_its_block(self):
        loop = self.loop()
        self.armed_stop(loop, armed=105)
        self.node.price[101] = 900_000
        self.assertEqual(self.run_to(loop, 101, 102, 103), [[], [], []])
        self.assertEqual(self.run_to(loop, 104), [[(EXAM, BTC, False)]], "armed for block 105, the next one")

    def test_two_stops_in_one_block_go_out_one_after_the_other_on_consecutive_nonces(self):
        self.keeper.accounts[OTHER] = {"pool": POOL, "kind": "exam"}
        self.node.positions[(OTHER, BTC)] = -5
        loop = self.loop()
        self.node.logs += [set_log(100, 950_000, 101), set_log(100, 940_000, 101, exam=OTHER, take=True)]
        self.run_to(loop, 100)
        self.node.price[101] = 930_000
        self.assertEqual(sorted(self.run_to(loop, 101)[0]), sorted([(EXAM, BTC, False), (OTHER, BTC, True)]))
        self.assertEqual(sorted(t["nonce"] for t in self.node.sent()), [7, 8])

    def test_a_refused_stop_is_tried_with_a_call_first_and_a_landed_one_is_said(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        tx = "0x" + keccak(hexstr=self.node.raw[0]).hex()
        self.node.receipts[tx] = {"status": "0x0", "blockNumber": hex(102)}
        self.node.refuse_fill = True
        self.assertEqual(self.run_to(loop, 102, 103), [[], []], "the exam would refuse it: not sent")
        self.assertEqual(self.events("trigger_fill_refused")[0]["tx"], tx)
        self.assertEqual(len(self.node.raw), 1)
        self.node.refuse_fill = False
        self.assertEqual(self.run_to(loop, 104), [[(EXAM, BTC, False)]])
        tx2 = "0x" + keccak(hexstr=self.node.raw[1]).hex()
        self.node.receipts[tx2] = {"status": "0x1", "blockNumber": hex(105)}
        self.node.logs += fill_of_stop(105)
        self.run_to(loop, 105)
        self.assertEqual(self.events("trigger_filled")[0]["tx"], tx2)
        self.assertNotIn((EXAM, BTC, False), loop.triggers)

    def test_a_send_with_no_receipt_is_given_up_and_the_nonce_read_again(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        self.run_to(loop, *range(102, 102 + sf.RECEIPT_GIVE_UP))
        self.assertEqual(len(self.node.raw), 1, "waits for its receipt")
        self.node.count = 9
        self.run_to(loop, 102 + sf.RECEIPT_GIVE_UP)
        self.assertTrue(self.events("trigger_fill_lost"))
        self.assertEqual([t["nonce"] for t in self.node.sent()], [7, 9], "the node's count, read again")

    def test_a_watch_with_no_key_and_a_keeper_that_is_not_the_filler_send_nothing(self):
        for wallet, filler in ((None, True), (FILLER, False)):
            self.node = Node()
            self.keeper.filler = filler
            self.said.clear()
            loop = self.loop(wallet=wallet)
            self.armed_stop(loop)
            self.node.price[101] = 900_000
            self.run_to(loop, 101, 102)
            self.assertEqual(self.node.raw, [])
            self.assertEqual(len(self.events("would_fill_trigger")), 1, "said once for the reach")

    def test_an_exam_the_keeper_holds_done_is_not_sent_for(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.keeper.done.add(EXAM)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        self.assertEqual(self.node.raw, [])


class NoBlindSpots(Base):
    def test_a_busy_node_costs_the_look_and_no_block(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.node.busy = {"eth_call"}
        self.node.head = 101
        with self.assertRaises(c.NodeBusy):
            loop.step(101)
        self.assertEqual(loop.block, 100, "block 101 is read again at the next look")
        self.node.busy = set()
        self.node.price[101] = 900_000
        self.assertEqual(self.run_to(loop, 101), [[(EXAM, BTC, False)]])

    def test_the_loop_runs_with_the_keepers_thread_held(self):
        """The keeper's thread stuck (a receipt, a rate limit): the loop goes on, in a thread of its own."""
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.node.head = 101
        stop = threading.Event()
        held = threading.Lock()
        held.acquire()  # what the keeper's thread would be waiting on
        thread = threading.Thread(target=loop.run, args=(0.01, stop), daemon=True)
        thread.start()
        for _ in range(200):
            if self.node.raw:
                break
            threading.Event().wait(0.01)
        stop.set()
        thread.join(2)
        held.release()
        self.assertEqual(len(self.node.raw), 1)

    def test_behind_the_stops_reached_at_the_head_go_out_first_and_the_blocks_between_are_read_back(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price.update({101: 949_000, 102: 960_000, 103: 940_000})
        self.node.head = 103
        sent = loop.step(103)
        self.assertEqual(sent, [(EXAM, BTC, False)])
        oracle_blocks = [int(p[1], 16) for m, p in self.node.calls if m == "eth_call" and p[0]["to"] == sf.ORACLE_PX]
        self.assertEqual(oracle_blocks, [103, 101, 102], "the head's price first, then the blocks between")
        self.assertLess([m for m, _ in self.node.calls].index("eth_sendRawTransaction"),
                        [m for m, _ in self.node.calls].index("eth_getLogs", 2), "sent before the read back")
        missed = self.events("stop_missed")
        self.assertEqual((missed[0]["reached_from"], missed[0]["reached_to"]), (101, 101),
                         "the reach of block 101, read back, ended in 102 with no fill")

    def test_past_the_read_back_the_blocks_are_said_as_not_seen(self):
        loop = self.loop(backfill=3)
        self.armed_stop(loop)
        self.node.head = 110
        loop.step(110)
        blind = self.events("stop_watch_blind")
        self.assertEqual((blind[0]["from_block"], blind[0]["to"]), (101, 107))
        journal = json.loads((self.dir / "prices.json").read_text())
        self.assertEqual(sorted(int(k.split(":")[1]) for k in journal), [108, 109, 110])

    def test_a_stop_set_on_an_exam_sold_in_the_same_blocks_is_known_at_once(self):
        new = to_checksum_address("0x" + "66" * 20)
        self.node.positions[(new, BTC)] = 4
        loop = self.loop()
        self.run_to(loop, 99)
        sold = ev(sk.EXAM_SOLD, POOL, 100, b"", word(int(new, 16)))
        self.node.logs += [sold, set_log(100, 950_000, 101, exam=new)]
        self.node.price[100] = 900_000
        self.run_to(loop, 100)
        self.assertIn((new, BTC, False), loop.triggers, "no slow pass waited for")
        self.assertEqual(self.run_to(loop, 101), [[(new, BTC, False)]])

    def test_nothing_watched_reads_nothing_but_the_head(self):
        self.keeper.accounts, self.keeper.pools = {}, []
        loop = self.loop()
        self.run_to(loop, 100, 101)
        self.assertEqual({m for m, _ in self.node.calls}, set())
        self.assertEqual(loop.block, 101)


class TheWatch(Base):
    def test_a_reach_that_ends_with_no_fill_is_an_alarm_in_the_journal_the_flag_and_the_channel(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 949_000, 102: 948_000, 103: 951_000})
        self.run_to(loop, 101, 102, 103)
        missed = self.events("stop_missed")
        self.assertEqual(len(missed), 1)
        m = missed[0]
        self.assertEqual((m["exam"], m["asset"], m["kind"], m["px"], m["reached_from"], m["reached_to"], m["oracle_first"],
                          m["side"], m["alarm"]), (EXAM, BTC, "stop", 950_000, 101, 102, 949_000, "long", "ALARM"))
        flag = (self.dir / "stops.alarm").read_text()
        self.assertIn('"event": "stop_missed"', flag)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(json.loads(self.alerts[0])["reached_to"], 102)

    def test_a_reach_filled_inside_it_is_said_caught_and_raises_nothing(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 949_000, 103: 951_000})
        self.node.logs += fill_of_stop(102)
        self.node.positions[(EXAM, BTC)] = 0
        self.run_to(loop, 101, 102, 103)
        self.assertEqual(self.events("stop_missed"), [])
        self.assertEqual(self.events("stop_caught")[0]["late"], 1)
        self.assertFalse((self.dir / "stops.alarm").exists())

    def test_a_long_reach_not_yet_filled_is_an_alarm_while_it_lasts_once(self):
        loop = self.loop(wallet=None, overdue=3)
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101, 102)
        self.assertEqual(self.events("stop_overdue"), [])
        self.run_to(loop, 103)
        self.assertEqual(self.events("stop_overdue")[0]["reached_to"], 103, "the third block of the reach")
        self.run_to(loop, 104, 105)
        self.assertEqual(len(self.events("stop_overdue")), 1, "once")

    def test_a_stop_taken_off_or_moved_by_the_trader_while_reached_is_an_alarm(self):
        for log_of, reason in ((lambda b: cleared_log(b), "taken off by the trader while reached"),
                               (lambda b: set_log(b, 800_000, b + 1), "moved by the trader while reached")):
            self.node = Node()
            self.said.clear()
            loop = self.loop(wallet=None)
            self.armed_stop(loop)
            self.node.price[101] = 900_000
            self.node.logs.append(log_of(102))
            self.run_to(loop, 101, 102)
            self.assertEqual(self.events("stop_missed")[0]["reason"], reason)

    def test_a_position_closed_or_an_exam_ended_while_reached_is_an_alarm(self):
        for logs, reason in (([filled_log(102, buy=False)], "the position was closed"),
                             ([ev(sf.STOPPED, EXAM, 102, encode(["int256"], [0]), word(3), word(2))], "the exam ended")):
            self.node = Node()
            self.said.clear()
            loop = self.loop(wallet=None)
            self.armed_stop(loop)
            self.node.price[101] = 900_000
            self.node.logs += logs
            self.node.head = 101
            loop.step(101)
            self.node.positions[(EXAM, BTC)] = 0
            self.run_to(loop, 102)
            self.assertEqual(self.events("stop_missed")[0]["reason"], reason)

    def test_a_block_whose_price_was_not_read_judges_nothing(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 949_000, 103: 951_000})
        self.node.unreadable = {102, 103}
        self.run_to(loop, 101, 102, 103)
        self.assertEqual(self.events("stop_missed"), [], "the end of the reach was not seen")
        self.assertEqual(self.events("stop_watch_blind")[0]["from_block"], 102)
        self.node.unreadable = set()
        self.run_to(loop, 104)
        self.assertEqual(self.events("stop_missed")[0]["reached_from"], 101)
        self.assertTrue(self.events("stop_watch_sees_again"))

    def test_a_short_one(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 1_000_000, 102: 950_000, 103: 950_001})
        self.run_to(loop, 101, 102, 103)
        self.assertEqual([(m["reached_from"], m["reached_to"]) for m in self.events("stop_missed")], [(102, 102)])


class TheKeepersRead(Base):
    def test_a_stop_found_by_the_slow_pass_is_taken_and_a_newer_event_wins(self):
        loop = self.loop()
        self.run_to(loop, 100)
        loop.apply_reread(EXAM, 100, {(EXAM, BTC, False): (950_000, 90), (EXAM, BTC, True): (1_200_000, 90)}, {BTC: 1})
        self.assertEqual(self.events("stop_found_by_read")[0]["px"], 950_000)
        self.node.logs.append(cleared_log(101, take=True))
        self.run_to(loop, 101)
        loop.apply_reread(EXAM, 100, {(EXAM, BTC, False): (950_000, 90), (EXAM, BTC, True): (1_200_000, 90)}, {BTC: 1})
        self.assertNotIn((EXAM, BTC, True), loop.triggers, "the clearing of block 101 is newer than the read at 100")
        loop.apply_reread(EXAM, 101, {}, {})
        self.assertEqual(loop.triggers, {}, "gone from the chain at 101: dropped")

    def test_the_read_is_pinned_to_one_block_and_handed_over(self):
        loop = self.loop()
        self.run_to(loop, 100)
        reads = []

        def call(method, params):
            reads.append(params[1])
            data = params[0]["data"]
            if data.startswith("0x" + keccak(text="rules()")[:4].hex()):
                return "0x" + encode([sk.RULES], [(300, 600, 1000, [BTC], [])]).hex()
            if data.startswith("0x" + keccak(text="stopOf(uint32)")[:4].hex()):
                return "0x" + encode(["uint64", "uint64", "uint64", "uint64"], [950_000, 0, 95, 0]).hex()
            if data.startswith("0x" + keccak(text="takeOf(uint32)")[:4].hex()):
                return "0x" + encode(["uint64", "uint64", "uint64", "uint64"], [0, 0, 0, 0]).hex()
            return "0x" + encode(["int256"], [-3]).hex()

        loop.reread(EXAM, call=call)
        self.assertEqual(set(reads), {hex(100)})
        self.run_to(loop, 101)
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["px"], 950_000)
        self.assertEqual(loop.signs[(EXAM, BTC)], -1)


class TheNonces(unittest.TestCase):
    def test_two_threads_never_take_one_nonce_and_a_failure_reads_the_node_again(self):
        n = sf.Nonces(FILLER.address)
        counted = {"n": 5}
        call = lambda m, p: hex(counted["n"])
        got = []
        threads = [threading.Thread(target=lambda: got.extend(n.take(call) for _ in range(50))) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(got), list(range(5, 205)))
        for nonce in got:
            n.done(nonce)
        held = n.take(call)  # 205, in flight while the count is read again
        n.failed()
        counted["n"] = 100
        self.assertEqual(n.take(call), held + 1, "not below a nonce in flight")
        n.done(held)
        n.done(held + 1)
        n.failed()
        self.assertEqual(n.take(call), 100, "the node's count, once nothing is in flight")

    def test_a_nonce_in_flight_is_not_handed_out_again_when_the_node_counts_behind(self):
        n = sf.Nonces(FILLER.address)
        call = lambda m, p: hex(5)
        first = n.take(call)
        n.failed()  # the other thread's send failed while `first` is in flight
        self.assertEqual(n.take(call), first + 1)

    def test_the_keepers_send_waits_for_its_receipt_on_the_shared_nonces(self):
        n = sf.Nonces(FILLER.address)
        node = Node()
        with mock.patch.object(c, "rpc", side_effect=node.call), \
                mock.patch.object(c, "wait_receipt", return_value={"status": "0x1", "transactionHash": "0x01"}) as wait:
            rcpt = n.send_and_wait(FILLER, EXAM, b"\x01\x02", gas=1000)
            self.assertEqual(rcpt["status"], "0x1")
            wait.return_value = {"status": "0x0"}
            with self.assertRaises(c.Reverted):
                n.send_and_wait(FILLER, EXAM, b"\x01\x02", gas=1000)
        self.assertEqual([t["nonce"] for t in node.sent()], [7, 8])
        self.assertEqual([t["tip"] for t in node.sent()], [0, 0], "the keeper's own sends tip nothing, as before")

    def test_a_taken_nonce_is_read_again_and_sent_once_more(self):
        n = sf.Nonces(FILLER.address)
        node = Node()
        answers = [RuntimeError("eth_sendRawTransaction: nonce too low")]
        real = node.call

        def call(method, params=()):
            if method == "eth_sendRawTransaction" and answers:
                raise answers.pop()
            return real(method, params)

        node.count = 3
        tx, nonce = n.broadcast(FILLER, call, EXAM, b"", 1000, 100)
        self.assertEqual(nonce, 3, "read again from the node")
        self.assertEqual(len(node.raw), 1)


class TheJournal(Base):
    def test_it_is_the_file_payout_review_takes_as_prices_at_every_moment(self):
        j = sf.PriceJournal(self.dir / "p.json")
        self.assertEqual(json.loads((self.dir / "p.json").read_text()), {})
        j.add(100, {3: 950_000, 100_007: 12})
        j.add(101, {})
        j.add(101, {3: 949_000})
        got = json.loads((self.dir / "p.json").read_text())
        self.assertEqual(got, {pr.oracle_key(3, 100): 950_000, pr.oracle_key(100_007, 100): 12, pr.oracle_key(3, 101): 949_000})
        again = sf.PriceJournal(self.dir / "p.json")
        again.add(102, {3: 1})
        self.assertEqual(len(json.loads((self.dir / "p.json").read_text())), 4, "opened again, it goes on")

    def test_payout_review_counts_a_stops_lateness_from_it(self):
        j = sf.PriceJournal(self.dir / "p.json")
        for b, px in ((98, 960_000), (99, 949_000), (100, 948_000), (101, 947_000)):
            j.add(b, {3: px})
        oracle = json.loads((self.dir / "p.json").read_text())
        close = {"block": 101, "asset": 3, "take": False, "side": 1, "px": 950_000, "armedAt": 90}
        self.assertEqual(pr.trigger_reached(close, lambda b: oracle.get(pr.oracle_key(3, b)), pr.TRIGGER_BACK), (2, False))

    def test_a_write_cut_short_is_mended_when_it_is_opened(self):
        path = self.dir / "p.json"
        for cut, want in (('{"3:1": 5, "3:2": 6, "3:3', {"3:1": 5, "3:2": 6}), ('{"3:1": 5', {}), ("{", {}),
                          ('{"3:1": 5}\n', {"3:1": 5})):
            path.write_text(cut)
            sf.PriceJournal(path).add(9, {3: 1})
            self.assertEqual(json.loads(path.read_text()), {**want, "3:9": 1}, cut)

    def test_the_loop_writes_each_block_it_read(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 949_000, 102: 951_000})
        self.run_to(loop, 101, 102)
        got = json.loads((self.dir / "prices.json").read_text())
        self.assertEqual((got["3:101"], got["3:102"]), (949_000, 951_000))


class TheSettings(unittest.TestCase):
    def parse(self, argv, env=None):
        import argparse
        p = argparse.ArgumentParser()
        with mock.patch.dict("os.environ", env or {}, clear=False):
            for name in ("COLOSSEUM_STOPS", "COLOSSEUM_STOP_RPC_URL", "COLOSSEUM_STOP_TIP_GWEI"):
                if not env or name not in env:
                    import os
                    os.environ.pop(name, None)
            sf.add_args(p)
            p.add_argument("--deployment", default="sim1")
            return p.parse_args(argv)

    def test_by_default_the_keepers_node_and_no_tip(self):
        a = self.parse([])
        self.assertEqual((a.stops, a.stop_rpc, a.stop_tip_gwei), ("loop", "", 0.0))
        node = mock.Mock()
        with mock.patch.object(sf, "Node", return_value=node) as made, mock.patch.object(sf, "check_chain"):
            loop = sf.build(a, Keeper(), pathlib.Path(tempfile.mkdtemp()) / "multi-sim1.json")
        made.assert_called_once_with(c.RPC_URL)
        self.assertEqual(loop.tip, 0)
        self.assertEqual(loop.journal.path.name, "prices-sim1.json")
        self.assertEqual(loop.alarm.flag.name, "stops-sim1.alarm")

    def test_a_node_of_its_own_and_a_tip_are_settings(self):
        a = self.parse([], {"COLOSSEUM_STOP_RPC_URL": "http://127.0.0.1:8545", "COLOSSEUM_STOP_TIP_GWEI": "0.5"})
        with mock.patch.object(sf, "Node") as made, mock.patch.object(sf, "check_chain"):
            loop = sf.build(a, Keeper(), pathlib.Path(tempfile.mkdtemp()) / "s.json")
        made.assert_called_once_with("http://127.0.0.1:8545")
        self.assertEqual(loop.tip, sf.GWEI // 2)
        self.assertEqual(self.parse(["--stops", "inline"]).stops, "inline")
        with mock.patch.object(sf, "Node"), mock.patch.object(sf, "check_chain"):
            none = sf.build(self.parse(["--price-journal", "", "--stop-alarm", ""]), Keeper(), pathlib.Path("/nonexistent/s.json"))
        self.assertIsNone(none.journal)
        self.assertIsNone(none.alarm.flag)

    def test_a_node_on_another_chain_is_refused(self):
        node = mock.Mock()
        node.call.return_value = hex(c.CHAIN_ID + 1)
        with self.assertRaises(SystemExit):
            sf.check_chain(node)


class TheLog(unittest.TestCase):
    def test_a_line_is_written_whole_in_one_write(self):
        """Two threads log: a line in two writes (print's text, then its end) could take the other's line between."""
        from ops import keeper as core
        out = mock.Mock()
        with mock.patch.object(core.sys, "stdout", out):
            core.log("stop_missed", exam=EXAM)
        self.assertEqual(out.write.call_count, 1)
        line = out.write.call_args[0][0]
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(json.loads(line)["event"], "stop_missed")


class TheKeeperWithTheLoop(unittest.TestCase):
    def keeper(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        k = sk.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, False, tmp / "s.json", 1)
        k.accounts = {EXAM: {"pool": POOL, "kind": "exam"}}
        return k

    def test_the_fast_loop_leaves_the_stops_to_the_loop_and_the_slow_pass_hands_its_read_over(self):
        k = self.keeper()
        k.stop_loop = mock.Mock()
        k.triggers[(EXAM, BTC, False)] = 1
        with mock.patch.object(sk.c, "rpc", return_value=[]), mock.patch.object(k, "trigger_step") as inline:
            self.assertEqual(k.fast_step(10), [])
        inline.assert_not_called()
        k.refresh_triggers(EXAM)
        k.stop_loop.reread.assert_called_once_with(EXAM)

    def test_its_sends_take_their_nonces_from_the_shared_book(self):
        k = self.keeper()
        k.nonces = mock.Mock()
        k.nonces.send_and_wait.return_value = {"transactionHash": "0x01", "blockNumber": hex(5), "logs": []}
        with mock.patch.object(k, "is_filler", return_value=True), mock.patch.object(sk.c, "send_tx") as old:
            k.fill(EXAM)
        old.assert_not_called()
        self.assertEqual(k.nonces.send_and_wait.call_args[1]["gas"], sk.FILL_GAS)

    def test_without_it_as_before(self):
        k = self.keeper()
        with mock.patch.object(k, "is_filler", return_value=True), \
                mock.patch.object(sk.c, "send_tx", return_value={"transactionHash": "0x01", "blockNumber": hex(5)}) as old:
            k.fill(EXAM)
        old.assert_called_once()


if __name__ == "__main__":
    unittest.main()
