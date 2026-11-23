"""The stops' loop and its watch (ops/stop_filler.py): a stop reached is sent in the step that sees it, nothing waits on
anything else, a busy node costs a look and no block, and a reach with no fill is an alarm. On a node made up here."""
import json
import pathlib
import re
import shutil
import tempfile
import threading
import time
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
SEL = {name: "0x" + keccak(text=name)[:4].hex() for name in ("positionOf(uint32)", "fillTrigger(uint32,bool)", "deadline()",
                                                              "lastFillAt()", "terms()", "result()")}


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
        self.count = 7  # the "pending" count
        self.latest: int | None = None  # the "latest" count; the pending one when None
        self.send_answers: list = []  # what eth_sendRawTransaction raises, one each, before it takes them
        self.status = 1  # PaperExam.Status of every exam: Active
        self.deadline = 0
        self.last_fill = 0
        self.max_idle = 0
        self.senders: list[int] = []  # the thread of each eth_sendRawTransaction
        self.stale = 0  # seconds the head's time is behind the clock of `Base.clock`
        self.balance = 10**18

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
            return hex(self.count if params[1] == "pending" or self.latest is None else self.latest)
        if method == "eth_getBlockByNumber":
            block = self.head if params[0] == "latest" else int(params[0], 16)
            return {"number": hex(block), "timestamp": hex(1_000_000 + block - self.stale)}
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            wanted = {a.lower() for a in q["address"]}
            topics = set(q["topics"][0])
            return [l for l in self.logs if lo <= int(l["blockNumber"], 16) <= hi and l["address"].lower() in wanted
                    and l["topics"][0] in topics]
        if method == "eth_sendRawTransaction":
            self.senders.append(threading.get_ident())
            if self.send_answers:
                answer = self.send_answers.pop(0)
                if isinstance(answer, tuple):  # taken, and the answer lost on the way
                    self.raw.append(params[0])
                    answer = answer[0]
                raise answer
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
            if data == SEL["deadline()"]:
                return "0x" + encode(["uint64"], [self.deadline]).hex()
            if data == SEL["lastFillAt()"]:
                return "0x" + encode(["uint64"], [self.last_fill]).hex()
            if data == SEL["terms()"]:
                return "0x" + encode([sk.EXAM_TERMS], [(0, [], 0, self.max_idle, 0, 0, 0, 0)]).hex()
            if data == SEL["result()"]:
                return "0x" + encode(sf.RESULT, [self.status, FILLER.address, 0, 0]).hex()
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
        self.later = 0.0  # seconds this machine's clock is ahead of the head's block time

    def loop(self, wallet=FILLER, tip=0, **kw) -> sf.StopLoop:
        alarm = sf.Alarm(self.dir / "stops.alarm", "")
        self.alert_seen = threading.Event()
        alarm._send = lambda line, where: (self.alerts.append(line), self.alert_seen.set())
        alarm.command = "cat"  # the channel; _send stands in for it
        sender = sf.Sender(FILLER, self.node) if wallet is not None else None
        if getattr(self, "journal", None) is not None:
            self.journal.close()  # one writer a journal: the loop made before this one lets it go
        self.journal = sf.PriceJournal(self.dir / "prices.jsonl")
        self.addCleanup(self.journal.close)
        kw.setdefault("clock", lambda: 1_000_000 + self.node.head + self.later)
        return sf.StopLoop(self.keeper, self.node, wallet=wallet, sender=sender, tip_wei=tip, journal=self.journal,
                           alarm=alarm, **kw)

    def run_to(self, loop, *heads):
        """Each head as the loop's `run` takes it: its time as polled, then a step to it."""
        out = []
        for h in heads:
            self.node.head = h
            loop.head_t[h] = 1_000_000 + h - self.node.stale  # the fake node's time of a block
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
        self.assertLess(methods.index("eth_sendRawTransaction"), methods.index("eth_getTransactionReceipt"))
        self.assertEqual(methods.count("eth_getTransactionReceipt"), 1, "looked at once, not waited for")
        tx = self.node.sent()[0]
        self.assertEqual((tx["to"], tx["nonce"], tx["tip"], tx["gas"]), (EXAM, 7, 2 * sf.GWEI, sk.FILL_GAS))
        self.assertTrue(tx["data"].startswith(SEL["fillTrigger(uint32,bool)"]))
        self.assertEqual(self.run_to(loop, 103), [[]], "in flight: not again")
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

    def test_a_stop_with_no_receipt_is_not_sent_twice_until_the_chain_counted_past_it(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        self.run_to(loop, *range(102, 130))
        self.assertEqual(len(self.node.raw), 1, "in flight while it has no receipt: one transaction, not two")
        loop.sender.ledger[7]["at"] -= sf.RESEND_AFTER_S
        self.node.latest = 8  # its nonce taken on chain, by no transaction of its
        loop.sender.sweep()
        self.assertFalse(loop.sender.is_gone(7), "once is not enough: its receipt may not be indexed yet")
        loop.sender.ledger[7]["at"] -= sf.RESEND_AFTER_S
        loop.sender.sweep()
        self.run_to(loop, 130)
        self.assertTrue(self.events("trigger_fill_lost"))
        self.assertEqual([t["nonce"] for t in self.node.sent()], [7, 8], "gone: the stop goes again, on the next nonce")

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

    def run_loop(self, loop, until, seconds=3.0):
        stop = threading.Event()
        thread = threading.Thread(target=loop.run, args=(0.01, stop), daemon=True)
        thread.start()
        self.loop_threads = getattr(self, "loop_threads", set()) | {thread.ident}
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            time.sleep(0.01)
        stop.set()
        thread.join(2)

    def test_a_keeper_send_held_in_its_thread_takes_no_nonce_and_holds_no_stop(self):
        """The keeper's thread stuck before its transaction is handed over (its node's ladder, its chain check): no nonce
        is taken for it, so the stop goes out on the next one and lands with nothing in front of it."""
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.node.head = 101
        held = threading.Event()
        keeper = threading.Thread(target=lambda: self.keeper_send(loop, held), daemon=True)
        with mock.patch.object(c, "assert_network", side_effect=lambda: held.wait(5)):
            keeper.start()
            self.run_loop(loop, lambda: self.node.raw)
            self.assertEqual([t["nonce"] for t in self.node.sent()], [7], "the stop, on the first nonce")
            held.set()
            self.run_loop(loop, lambda: len(self.node.raw) == 2)
        keeper.join(5)
        self.assertEqual([t["nonce"] for t in self.node.sent()], [7, 8], "the keeper's after it, no gap")
        self.assertLessEqual(set(self.node.senders), self.loop_threads, "every transaction handed over by the stops' thread")

    def keeper_send(self, loop, held):
        with mock.patch.object(c, "rpc", side_effect=lambda m, p=(): hex(21_000) if m == "eth_estimateGas" else hex(10**8)), \
                mock.patch.object(c, "wait_receipt", return_value={"status": "0x1"}):
            try:
                loop.sender.send_and_wait(POOL, b"\x01")
            except Exception:
                pass

    def test_a_keeper_send_the_node_refused_spends_no_nonce(self):
        """A transaction the node did not take (busy, an error) leaves its nonce to the next one: no gap behind it."""
        loop = self.loop()
        self.node.send_answers = [c.NodeBusy("eth_sendRawTransaction: HTTP 429")]
        loop.sender.requests.put(req := {"to": POOL, "data": b"", "gas": 21_000, "gas_price": 10**8, "state": "queued",
                                          "done": threading.Event(), "result": None, "error": None,
                                          "not_before": 0.0, "delay": 1.0})
        loop.sender.serve()
        self.assertIsInstance(req["error"], sf.Busy)
        self.assertIn(req, loop.sender.later, "kept to be tried again in a moment")
        loop.sender.later.clear()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        self.assertEqual([t["nonce"] for t in self.node.sent()], [7], "the stop on the nonce the refused one left")

    def test_behind_the_stops_reached_at_the_head_go_out_first_and_the_blocks_between_are_read_back(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price.update({101: 949_000, 102: 960_000, 103: 940_000})
        self.node.head = 103
        sent = loop.step(103)
        self.assertEqual(sent, [(EXAM, BTC, False)])
        oracle_blocks = [int(p[1], 16) for m, p in self.node.calls if m == "eth_call" and p[0]["to"] == sf.ORACLE_PX
                         and p[0]["data"].endswith(encode(["uint32"], [BTC]).hex())]
        self.assertEqual(oracle_blocks, [100, 103, 101, 102], "the head's price first, then the blocks between")
        self.assertLess([m for m, _ in self.node.calls].index("eth_sendRawTransaction"),
                        [m for m, _ in self.node.calls].index("eth_getLogs", 2), "sent before the read back")
        short = self.events("stop_reach_too_short")
        self.assertEqual((short[0]["reached_from"], short[0]["reached_to"]), (101, 101),
                         "the reach of block 101, read back, ended in 102 with no fill")

    def test_past_the_read_back_the_blocks_are_said_as_not_seen(self):
        loop = self.loop(backfill=3)
        self.armed_stop(loop)
        self.node.head = 110
        loop.step(110)
        blind = self.events("stop_watch_blind")
        self.assertEqual((blind[0]["from_block"], blind[0]["to"]), (101, 107))
        journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual(sorted(journal.px[3]), [100, 108, 109, 110])
        self.assertIn((101, 107, "the head ran ahead of the loop: their prices were not read back"), journal.gaps)

    def test_a_stop_set_on_an_exam_sold_in_the_same_blocks_is_known_at_once(self):
        new = to_checksum_address("0x" + "66" * 20)
        self.node.positions[(new, BTC)] = 4
        loop = self.loop()
        self.run_to(loop, 99)
        sold = ev(sk.EXAM_SOLD, POOL, 100, b"", word(int(new, 16)))
        self.node.logs += [sold, set_log(100, 950_000, 101, exam=new)]
        self.node.price[100] = 900_000
        # The platform's prices are read in every block, a stop known or not: the stop set in 100, armed for 101 and
        # reached at 100's price, goes out at the end of the step that read it.
        self.assertEqual(self.run_to(loop, 100), [[(new, BTC, False)]], "no slow pass waited for")
        self.assertIn((new, BTC, False), loop.triggers)
        self.assertEqual(self.run_to(loop, 101), [[]], "in flight: not sent twice")

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
        self.assertTrue(self.alert_seen.wait(2), "the channel runs in a thread of its own")
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

    def test_a_reach_of_one_block_ended_by_the_price_is_a_line_not_an_alarm(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 1_000_000, 102: 950_000, 103: 950_001, 104: 949_000, 106: 960_000})
        self.run_to(loop, 101, 102, 103)
        self.assertEqual(self.events("stop_missed"), [])
        self.assertEqual([(m["reached_from"], m["reached_to"]) for m in self.events("stop_reach_too_short")], [(102, 102)])
        self.run_to(loop, 104, 105, 106)
        self.assertEqual([(m["reached_from"], m["reached_to"]) for m in self.events("stop_missed")], [(104, 105)])

    def test_a_reach_after_the_exam_was_over_is_a_line_not_an_alarm(self):
        """Past its deadline or idle limit `fillTrigger` refuses (`TooLate`) whoever sends it: `expire` is due."""
        src = (ROOT / "src/exam/PaperExam.sol").read_text()
        self.assertIn("if (deadline != 0 && block.timestamp > deadline) return true;", src)
        self.assertIn("return _terms.maxIdle != 0 && block.timestamp > uint256(lastFillAt) + _terms.maxIdle;", src)
        for deadline, last_fill, idle in ((1_000_000 + 101, 0, 0), (0, 1_000_000, 50)):
            self.node = Node()
            self.said.clear()
            self.node.deadline, self.node.last_fill, self.node.max_idle = deadline, last_fill, idle
            loop = self.loop(wallet=None)
            self.armed_stop(loop)
            self.node.price.update({101: 900_000, 104: 960_000})
            self.run_to(loop, 101, 102, 103, 104)
            self.assertEqual(self.events("stop_missed"), [])
            self.assertTrue(self.events("stop_reach_after_exam_over")[0]["over"])
        self.node = Node()
        self.said.clear()
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 900_000, 104: 960_000})
        self.run_to(loop, 101, 102, 103)
        with mock.patch.object(self.node, "call", side_effect=lambda m, p=(): (_ for _ in ()).throw(RuntimeError("down"))
                               if m == "eth_getBlockByNumber" else Node.call(self.node, m, p)):
            self.run_to(loop, 104)
        self.assertEqual(self.events("stop_missed")[0]["over"], "not read", "not read: the alarm all the same")

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
            if data == SEL["result()"]:
                return "0x" + encode(sf.RESULT, [sf.ACTIVE, FILLER.address, 0, 0]).hex()
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


class TheSender(Base):
    """Every transaction of the key numbered and handed to the node by the stops' thread alone."""

    def test_an_answer_lost_on_the_way_spends_the_nonce_and_the_same_bytes_go_again(self):
        sender = sf.Sender(FILLER, self.node)
        self.node.send_answers = [(sf.Unsure("eth_sendRawTransaction: ReadTimeout"),)]
        tx, nonce = sender.send(POOL, b"\x01", 21_000, 10**8)
        self.assertEqual((nonce, sender.next), (7, 8), "it may be in: its nonce is spent")
        self.assertEqual(sender.send(POOL, b"\x02", 21_000, 10**8)[1], 8)
        sender.ledger[7]["at"] -= sf.RESEND_AFTER_S
        sender.sweep()
        self.assertEqual(self.node.raw[0], self.node.raw[2], "sent again with the same bytes: the same hash")
        self.assertEqual(len({t["nonce"] for t in self.node.sent()}), 2, "never another transaction under nonce 7")

    def test_one_already_in_a_block_is_confirmed_by_its_receipt_and_not_sent_again(self):
        """The case c.send_tx waited five seconds for: the transaction landed though its answer said otherwise."""
        sender = sf.Sender(FILLER, self.node)
        self.node.send_answers = [(sf.Unsure("eth_sendRawTransaction: ConnectionError"),)]
        tx, nonce = sender.send(POOL, b"\x01", 21_000, 10**8)
        self.node.receipts[tx] = {"status": "0x1", "blockNumber": hex(101)}
        self.node.latest = 8
        sender.ledger[nonce]["at"] -= sf.RESEND_AFTER_S
        sender.sweep()
        self.assertEqual(sender.ledger, {})
        self.assertFalse(sender.is_gone(nonce), "landed, not gone")
        self.assertEqual(len(self.node.raw), 1, "not sent again")

    def test_the_count_is_read_once_and_never_below_a_nonce_handed_out(self):
        sender = sf.Sender(FILLER, self.node)
        sender.send(POOL, b"", 21_000, 10**8)  # 7, its receipt not in
        self.node.count = 3  # a node that lags: it knows nothing of 7
        self.node.latest = 3
        self.node.send_answers = [RuntimeError("eth_sendRawTransaction: nonce too low")]
        _, nonce = sender.send(POOL, b"", 21_000, 10**8)
        self.assertEqual(nonce, 9, "past the one it said was taken, never back to the node's 3")
        counts = [p for m, p in self.node.calls if m == "eth_getTransactionCount"]
        self.assertEqual([p[1] for p in counts], ["pending", "latest"], "once at the start, once when told it is taken")

    def test_the_keepers_sends_go_through_the_one_sender_on_its_node(self):
        """The keeper's own node is asked for the chain, the gas and the receipt; never for a count or a send."""
        loop = self.loop()
        main_calls = []

        def main_rpc(method, params=()):
            main_calls.append(method)
            return hex(21_000) if method == "eth_estimateGas" else hex(10**8)

        done = []
        with mock.patch.object(c, "rpc", side_effect=main_rpc), mock.patch.object(c, "assert_network") as chain, \
                mock.patch.object(c, "wait_receipt", return_value={"status": "0x1"}):
            keeper = threading.Thread(target=lambda: done.append(loop.sender.send_and_wait(POOL, b"\x01")), daemon=True)
            keeper.start()
            for _ in range(300):
                loop.sender.serve()
                if done:
                    break
                time.sleep(0.01)
            keeper.join(2)
        chain.assert_called_once()
        self.assertEqual(sorted(set(main_calls)), ["eth_estimateGas", "eth_gasPrice"])
        self.assertEqual(len(self.node.raw), 1)
        self.assertEqual(loop.sender.ledger, {}, "its receipt in: nothing kept")

    def test_a_keeper_send_not_taken_in_time_is_never_sent(self):
        sender = sf.Sender(FILLER, self.node)
        with self.assertRaises(c.NotSent):
            sender.submit(POOL, b"", 21_000, 10**8, wait=0.05)
        sender.serve()
        self.assertEqual(self.node.raw, [], "cancelled before it was taken")

    def test_a_reverted_send_raises(self):
        sender = sf.Sender(FILLER, self.node)
        with mock.patch.object(c, "rpc", return_value=hex(10**8)), mock.patch.object(c, "assert_network"), \
                mock.patch.object(c, "wait_receipt", return_value={"status": "0x0"}), \
                mock.patch.object(sender, "submit", side_effect=lambda *a, **k: sender.send(*a[:4])):
            with self.assertRaises(c.Reverted):
                sender.send_and_wait(POOL, b"", gas=21_000)
        self.assertEqual([t["tip"] for t in self.node.sent()], [0], "the keeper's own sends tip nothing, as before")


class LostAndFound(Base):
    def test_the_events_of_blocks_past_the_price_read_back_are_read_all_the_same(self):
        loop = self.loop(wallet=None, backfill=3)
        self.armed_stop(loop)
        self.node.price.update({101: 900_000})
        self.node.logs += fill_of_stop(103)
        self.node.positions[(EXAM, BTC)] = 0
        self.node.logs.append(set_log(104, 1_200_000, 105, take=True))
        self.node.positions[(EXAM, BTC)] = 5
        self.run_to(loop, 110)
        self.assertNotIn((EXAM, BTC, False), loop.triggers, "its TriggerFilled of block 103 was read")
        self.assertIn((EXAM, BTC, True), loop.triggers, "the take set in block 104 was read")
        self.assertEqual(self.events("stop_missed") + self.events("stop_overdue"), [])
        self.assertEqual(self.events("stop_watch_blind")[0]["from_block"], 101)

    def test_past_the_catch_up_it_says_it_lost_the_thread_and_forgets_its_reaches(self):
        loop = self.loop(wallet=None, backfill=3, catchup=10)
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["touch"], 101)
        self.node.price[102] = 960_000
        self.run_to(loop, 150)
        lost = self.events("stop_watch_lost")
        self.assertEqual((lost[0]["from_block"], lost[0]["alarm"]), (102, "ALARM"))
        self.assertEqual(self.events("stop_missed"), [], "a reach whose end it did not see is not judged")
        logs = [p[0] for m, p in self.node.calls if m == "eth_getLogs"]
        self.assertTrue(all(int(q["toBlock"], 16) - int(q["fromBlock"], 16) < 50 for q in logs), "windows the node takes")

    def test_a_read_that_drops_or_moves_a_stop_while_reached_is_an_alarm(self):
        for found, reason in (({}, "gone from the exam while reached (read from it)"),
                              ({(EXAM, BTC, False): (800_000, 90)}, "moved while reached (read from the exam)")):
            self.node = Node()
            self.said.clear()
            loop = self.loop(wallet=None)
            self.armed_stop(loop, armed=90)
            self.node.price[101] = 900_000
            self.run_to(loop, 101)
            loop.inbox.put(("state", EXAM, 101, found, {BTC: 1}))  # as the keeper's slow pass hands it over
            self.run_to(loop, 102)
            self.assertEqual(self.events("stop_missed")[0]["reason"], reason)


class TheWatchAlone(Base):
    def test_an_exam_that_ended_is_told_by_its_own_read_and_raises_nothing(self):
        """`_end` clears no stop, take or position: the read of the exam's status is what tells it ended."""
        src = (ROOT / "src/exam/PaperExam.sol").read_text()
        end = src[src.index("function _end("):src.index("function _book(")]
        self.assertNotIn("stopOf", end)
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        loop.apply_reread(EXAM, 100, {(EXAM, BTC, False): (950_000, 90)}, {BTC: 1})
        self.node.status = 3  # Breached
        self.node.price[101] = 900_000
        loop.reread(EXAM, call=self.node.call)
        self.run_to(loop, 101)
        self.assertIn(EXAM, loop.ended)
        self.assertEqual(loop.triggers, {})
        self.assertEqual(self.events("stop_missed"), [])
        self.keeper.done = set()
        with mock.patch.object(c, "rpc", side_effect=self.node.call):
            keeper = mock.Mock(done=set(), accounts={EXAM: {"pool": POOL, "kind": "exam"}})
            keeper.live.return_value = [EXAM]
            sf.watch_pass(keeper, loop)
        self.assertEqual(keeper.done, {EXAM}, "not read again")

    def test_its_files_are_its_own_and_a_second_writer_is_refused(self):
        a = mock.Mock(stop_rpc="", price_journal=None, stop_alarm=None, deployment="sim1", stop_tip_gwei=0,
                      stop_overdue=5, stop_alarm_min_blocks=2)
        with mock.patch.object(sf, "Node"), mock.patch.object(sf, "check_chain"):
            watch = sf.build(a, Keeper(), self.dir / "watch-sim1.json", prefix="watch-")
            self.assertEqual((watch.journal.path.name, watch.alarm.flag.name), ("watch-prices-sim1.jsonl", "watch-stops-sim1.alarm"))
            keeper = sf.build(a, Keeper(), self.dir / "multi-sim1.json")
            self.assertEqual(keeper.journal.path.name, "prices-sim1.jsonl")
        with self.assertRaises(SystemExit):
            sf.PriceJournal(self.dir / "prices-sim1.jsonl")


class Alive(Base):
    def test_a_thread_that_ended_is_an_alarm_and_starts_again(self):
        loop = self.loop(wallet=None)
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        with mock.patch.object(sf, "start", return_value="new") as again:
            self.assertEqual(sf.keep_alive(loop, dead, 0.5), "new")
        again.assert_called_once_with(loop, 0.5)
        self.assertEqual(self.events("stop_loop_dead")[0]["alarm"], "ALARM")

    def test_a_thread_that_stopped_moving_is_an_alarm_once(self):
        loop = self.loop(wallet=None)
        alive = mock.Mock(is_alive=lambda: True)
        loop.beat = time.monotonic() - sf.STALL_S - 1
        sf.keep_alive(loop, alive, 0.5)
        sf.keep_alive(loop, alive, 0.5)
        self.assertEqual(len(self.events("stop_loop_stalled")), 1)
        loop.beat = time.monotonic()
        sf.keep_alive(loop, alive, 0.5)
        self.assertTrue(self.events("stop_loop_moving_again"))

    def test_calls_failing_for_long_are_an_alarm_once(self):
        loop = self.loop(wallet=None)
        loop._failed("stop_loop_failed", RuntimeError("x"))
        self.assertEqual(self.events("stop_loop_failing"), [])
        loop.failing_since -= sf.STALL_S
        loop._failed("stop_loop_failed", RuntimeError("x"))
        loop._failed("stop_loop_failed", RuntimeError("x"))
        self.assertEqual(len(self.events("stop_loop_failing")), 1)

    def test_a_long_blindness_with_a_stop_standing_is_an_alarm(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.unreadable = set(range(101, 101 + sf.BLIND_ALARM_BLOCKS))
        self.run_to(loop, *range(101, 101 + sf.BLIND_ALARM_BLOCKS - 1))
        self.assertEqual(self.events("stop_watch_blind_long"), [])
        self.run_to(loop, 100 + sf.BLIND_ALARM_BLOCKS)
        self.assertEqual(self.events("stop_watch_blind_long")[0]["from_block"], 101)


class NoKeyInTheLog(unittest.TestCase):
    URL = "http://127.0.0.1:9/v2/SECRETKEY?key=SECRETKEY"

    def test_no_error_and_no_line_names_the_nodes_path(self):
        import requests
        node = sf.Node(self.URL)
        for exc in (requests.ConnectionError(f"HTTPConnectionPool: Max retries exceeded with url: {self.URL}"),
                    requests.ReadTimeout(f"read timed out ({self.URL})")):
            with mock.patch.object(node.session, "post", side_effect=exc):
                with self.assertRaises(sf.Unsure) as caught:
                    node.call("eth_blockNumber")
            self.assertNotIn("SECRETKEY", str(caught.exception))
        for status, kind in ((500, sf.Unsure), (403, RuntimeError)):
            resp = mock.Mock(status_code=status, url=self.URL)
            with mock.patch.object(node.session, "post", return_value=resp):
                with self.assertRaises(kind) as caught:
                    node.call("eth_blockNumber")
            self.assertNotIn("SECRETKEY", str(caught.exception))
        self.assertEqual(sf.host_of(self.URL), "http://127.0.0.1:9")


class TheJournal(Base):
    def test_it_is_the_file_payout_review_judges_by_with_time_and_gaps(self):
        j = sf.PriceJournal(self.dir / "p.jsonl", clock=lambda: 7)
        j.add(100, {3: 950_000, 100_007: 12}, t=1_100)
        j.add(101, {}, unread={3})
        j.gap(102, 104, "the loop was not running")
        j.add(105, {3: 949_000})
        day = self.dir / "p-1970-01-01.jsonl"  # one file a day by the loop's clock; p.jsonl itself is the lock
        self.assertEqual((self.dir / "p.jsonl").read_text(), "")
        got = pr.load_prices(day.read_text())
        self.assertEqual(got.px, {3: {100: 950_000, 105: 949_000}, 100_007: {100: 12}})
        self.assertEqual((got.times, got.gaps, got.lines), ({100: 1_100}, [(102, 104, "the loop was not running")], 3))
        header, first, second = [json.loads(x) for x in day.read_text().splitlines()[:3]]
        self.assertEqual(header, {"journal": sf.JOURNAL_SCHEMA})
        self.assertEqual(first, {"b": 100, "t": 1_100, "w": 7, "px": {"3": 950_000, "100007": 12}})
        self.assertEqual(second, {"b": 101, "t": None, "w": 7, "px": {}, "unread": [3]})
        j.close()
        again = sf.PriceJournal(self.dir / "p.jsonl")
        self.assertEqual(again.last, 105, "opened again, it knows its last block")
        again.close()

    def test_a_write_cut_short_is_cut_off_when_it_is_opened_and_nothing_before_it(self):
        path = self.dir / "p-1970-01-01.jsonl"
        whole = "".join(json.dumps({"b": b, "px": {"3": b}}) + "\n" for b in range(1, 5001))
        for cut, last in ((whole + '{"b": 5001, "px": {"3', 5000), ('{"b": 1', None), ("", None),
                          (whole + json.dumps({"gap": [5001, 5009], "why": "x"}) + "\n", 5009)):
            path.write_text(cut)
            j = sf.PriceJournal(self.dir / "p.jsonl", clock=lambda: 7)
            self.assertEqual(j.last, last, cut[-30:])
            j.add(9_000, {3: 1})
            j.close()
            text = path.read_text()
            self.assertTrue(text.endswith("\n"))
            self.assertEqual(len(pr.load_prices(text).px[3]), (5000 if last else 0) + 1)

    def test_the_loop_writes_each_block_it_read_and_its_time(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 949_000, 102: 951_000})
        loop.head_t[102] = 1_000_102
        self.run_to(loop, 101, 102)
        got = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual((got.px[3][101], got.px[3][102], got.times.get(102)), (949_000, 951_000, 1_000_102))

    def test_started_again_the_blocks_it_was_not_running_in_are_a_gap(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.run_to(loop, 101)
        loop = self.loop(wallet=None)  # the keeper started again, the journal opened again
        self.run_to(loop, 120)
        got = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertIn((102, 119, "the loop was not running"), got.gaps)

    def test_a_journal_that_cannot_be_written_is_an_alarm_at_once(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        with mock.patch.object(loop.journal, "add", side_effect=OSError("No space left on device")):
            self.run_to(loop, 101, 102)
        failed = self.events("stop_journal_failed")
        self.assertEqual((len(failed), failed[0]["alarm"]), (1, "ALARM"), "once, at once")


class TheSettings(unittest.TestCase):
    def parse(self, argv, env=None):
        import argparse
        p = argparse.ArgumentParser()
        with mock.patch.dict("os.environ", env or {}, clear=False):
            for name in ("COLOSSEUM_STOPS", "COLOSSEUM_STOP_RPC_URL", "COLOSSEUM_STOP_TIP_GWEI"):  # noqa: the env as given
                if not env or name not in env:
                    import os
                    os.environ.pop(name, None)
            sf.add_args(p)
            p.add_argument("--deployment", default="sim1")
            return p.parse_args(argv)

    def test_by_default_the_loop_on_the_keepers_node_with_no_tip(self):
        a = self.parse([])
        self.assertEqual((a.stops, a.stop_rpc, a.stop_tip_gwei), ("loop", "", 0.0))
        self.assertEqual(self.parse([], {"COLOSSEUM_STOPS": "inline"}).stops, "inline", "the way back, by hand")
        node = mock.Mock()
        with mock.patch.object(sf, "Node", return_value=node) as made, mock.patch.object(sf, "check_chain"):
            loop = sf.build(a, Keeper(), pathlib.Path(tempfile.mkdtemp()) / "multi-sim1.json")
        made.assert_called_once_with(c.RPC_URL)
        self.assertEqual(loop.tip, 0)
        self.assertIsNone(loop.sender, "no wallet, no sender")
        self.assertEqual(loop.journal.path.name, "prices-sim1.jsonl")
        loop.journal.close()
        self.assertEqual(loop.alarm.flag.name, "stops-sim1.alarm")

    def test_the_documents_say_the_default(self):
        hosting = (ROOT / "docs/HOSTING.md").read_text()
        self.assertIn("COLOSSEUM_STOPS=loop", hosting)
        self.assertNotIn("COLOSSEUM_STOPS=inline`, the stops back", hosting)

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


class TheBotsReview(Base):
    """What the code-review bot found in #333, each held here."""

    def test_a_fill_in_the_block_a_reach_turns_overdue_raises_nothing(self):
        loop = self.loop(wallet=None, overdue=3)
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.node.logs += fill_of_stop(103)
        self.run_to(loop, 101, 102, 103)
        self.assertEqual(self.events("stop_overdue"), [])
        self.assertEqual(self.events("stop_caught")[0]["filled"], 103)

    def test_a_stop_newly_reached_goes_out_before_the_receipts_are_read(self):
        self.keeper.accounts[OTHER] = {"pool": POOL, "kind": "exam"}
        self.node.positions[(OTHER, BTC)] = 4
        loop = self.loop()
        self.node.logs += [set_log(100, 950_000, 101), set_log(100, 900_000, 101, exam=OTHER)]
        self.run_to(loop, 100)
        self.node.price[101] = 940_000  # EXAM's stop reached, OTHER's not
        self.run_to(loop, 101)
        self.node.price[102] = 890_000  # now OTHER's too, with EXAM's in flight
        before = len(self.node.calls)
        self.run_to(loop, 102)
        methods = [m for m, _ in self.node.calls[before:]]
        self.assertLess(methods.index("eth_sendRawTransaction"), methods.index("eth_getTransactionReceipt"))

    def test_an_exam_the_keeper_added_meanwhile_still_gets_its_second_read(self):
        new = to_checksum_address("0x" + "67" * 20)
        self.node.positions[(new, BTC)] = 4
        loop = self.loop()
        self.run_to(loop, 99)
        self.node.logs += [ev(sk.EXAM_SOLD, POOL, 100, b"", word(int(new, 16))), set_log(100, 950_000, 101, exam=new)]
        real = self.node.call

        def call(method, params=()):
            out = real(method, params)
            if method == "eth_getLogs":
                self.keeper.accounts[new] = {"pool": POOL, "kind": "exam"}  # the keeper's fast loop, meanwhile
            return out

        with mock.patch.object(self.node, "call", side_effect=call):
            self.run_to(loop, 100)
        self.assertIn((new, BTC, False), loop.triggers)

    def test_one_asset_unread_leaves_the_others_judged_and_sent(self):
        self.keeper.accounts[OTHER] = {"pool": POOL, "kind": "exam"}
        self.node.positions[(OTHER, 4)] = 3
        loop = self.loop()
        self.node.logs += [set_log(100, 950_000, 101), set_log(100, 2_000, 101, asset=4, exam=OTHER)]
        self.run_to(loop, 100)
        self.node.price[101] = 900_000
        real = self.node.call

        def call(method, params=()):
            if method == "eth_call" and params[0]["to"] == sf.ORACLE_PX and params[0]["data"].endswith("04"):
                raise RuntimeError("asset 4 not read")
            return real(method, params)

        with mock.patch.object(self.node, "call", side_effect=call):
            self.assertEqual(self.run_to(loop, 101), [[(EXAM, BTC, False)]])
        self.assertIn("assets [4]", self.events("stop_watch_blind")[0]["why"])

    def test_blocks_not_judged_break_a_reach_and_raise_nothing(self):
        loop = self.loop(wallet=None, overdue=4)
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.node.unreadable = {102, 103, 104}
        self.run_to(loop, 101, 102, 103, 104, 105)
        self.assertEqual(self.events("stop_overdue"), [], "101 and 105 are not a reach of five blocks")
        self.assertEqual(self.events("stop_reach_unclear")[0]["reached_to"], 101)
        self.run_to(loop, 106, 107, 108)
        self.assertEqual(self.events("stop_overdue")[0]["reached_from"], 105)

    def test_a_position_read_that_failed_is_read_again_with_no_event(self):
        loop = self.loop(wallet=None)
        real = self.node.call
        fails = {"n": 1}

        def call(method, params=()):
            if method == "eth_call" and params[0]["data"].startswith(SEL["positionOf(uint32)"]) and fails["n"]:
                fails["n"] -= 1
                raise RuntimeError("eth_call: HTTP 403")
            return real(method, params)

        self.node.logs.append(set_log(100, 950_000, 101))
        with mock.patch.object(self.node, "call", side_effect=call):
            self.run_to(loop, 100)
            self.assertIsNone(loop.signs[(EXAM, BTC)])
            self.run_to(loop, 101)
        self.assertEqual(loop.signs[(EXAM, BTC)], 1, "read again at the next step")

    def test_a_failed_position_read_is_read_again_inside_one_step(self):
        """The read fails after TriggerSet; the reach and its end come in the same step: still judged."""
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        real = self.node.call
        fails = {"n": 1}

        def call(method, params=()):
            if method == "eth_call" and params[0]["data"].startswith(SEL["positionOf(uint32)"]) and fails["n"]:
                fails["n"] -= 1
                raise RuntimeError("eth_call: HTTP 403")
            return real(method, params)

        self.node.logs.append(set_log(100, 950_000, 101))
        self.node.price.update({101: 900_000, 103: 960_000})
        with mock.patch.object(self.node, "call", side_effect=call):
            self.run_to(loop, 103)
        missed = self.events("stop_missed")
        self.assertEqual((missed[0]["reached_from"], missed[0]["reached_to"]), (101, 102))
        reads = [int(p[1], 16) for m, p in self.node.calls if m == "eth_call" and p[0]["data"].startswith(SEL["positionOf(uint32)"])]
        self.assertEqual(reads, [100], "the retry pinned to block 100, the one before 101")

    def test_a_done_exam_sold_meanwhile_gets_no_stop_back_from_its_logs(self):
        new = to_checksum_address("0x" + "68" * 20)
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        loop.extra.add(new)  # sold since the keeper's last look, and done in its thread since
        self.keeper.done.add(new)
        self.node.positions[(new, BTC)] = 4
        self.node.logs.append(set_log(100, 950_000, 101, exam=new))
        self.run_to(loop, 100)
        self.assertEqual(loop.triggers, {})
        loop.ended.add(EXAM)  # and an exam that ended: a TriggerSet among its logs brings nothing back
        loop._events(101, [set_log(101, 950_000, 102)])
        self.assertEqual(loop.triggers, {})

    def test_an_exam_the_keeper_holds_done_is_dropped_and_its_open_reach_said(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101, 102)
        self.keeper.done.add(EXAM)
        self.run_to(loop, 103)
        self.assertEqual(loop.triggers, {})
        self.assertEqual(self.events("stop_missed")[0]["reason"], "the keeper holds the exam done")

    def test_a_write_cut_short_while_running_is_mended_before_the_next(self):
        j = sf.PriceJournal(self.dir / "p.jsonl", clock=lambda: 7)
        j.add(1, {3: 5})
        with open(self.dir / "p-1970-01-01.jsonl", "ab") as fh:  # a write cut short: no end of line
            fh.write(b'{"b":2,"px":{"3"')
        j.broken = True  # the write that failed said so
        j.add(3, {3: 7})
        self.assertEqual(pr.load_prices(pr.journal_text(self.dir / "p.jsonl")).px, {3: {1: 5, 3: 7}})
        j.close()


class TheSecondReview(Base):
    """The second review of #333 (А–Е, the rest of 10, a connect timeout) and the bot's three failed reads in a row."""

    def fails(self, n: int):
        """The node, with the next `n` reads of a position failing."""
        real = self.node.call
        left = {"n": n}

        def call(method, params=()):
            if method == "eth_call" and params[0]["data"].startswith(SEL["positionOf(uint32)"]) and left["n"]:
                left["n"] -= 1
                raise RuntimeError("eth_call: HTTP 403")
            return real(method, params)
        return mock.patch.object(self.node, "call", side_effect=call)

    def test_three_failed_position_reads_in_a_row_still_judge_the_reach(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        self.node.logs.append(set_log(100, 950_000, 101))
        self.node.price.update({101: 900_000, 103: 960_000})
        with self.fails(3):
            self.run_to(loop, 103)
        missed = self.events("stop_missed")
        self.assertEqual((missed[0]["reached_from"], missed[0]["reached_to"]), (101, 102),
                         "judged when the position was read at last, block by block")
        reads = [int(p[1], 16) for m, p in self.node.calls if m == "eth_call" and p[0]["data"].startswith(SEL["positionOf(uint32)"])]
        self.assertIn(100, reads, "each kept block judged against the position before it, read by its block")

    def test_a_kept_block_is_judged_against_the_position_as_it_stood_then(self):
        """Reads fail until after the position was closed in block 103: the kept blocks 101-103 are judged against the
        position before each (long), not against the closed one the late read finds."""
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        self.node.logs += [set_log(100, 950_000, 101), filled_log(103)]
        self.node.price.update({101: 900_000, 103: 960_000})
        real = self.node.call
        left = {"n": 5}

        def call(method, params=()):
            if method == "eth_call" and params[0]["data"].startswith(SEL["positionOf(uint32)"]):
                if left["n"]:
                    left["n"] -= 1
                    raise RuntimeError("eth_call: HTTP 403")
                return "0x" + encode(["int256"], [10 if int(params[1], 16) <= 102 else 0]).hex()
            return real(method, params)

        with mock.patch.object(self.node, "call", side_effect=call):
            self.run_to(loop, 104)
        missed = self.events("stop_missed")
        self.assertEqual((missed[0]["reached_from"], missed[0]["reached_to"], missed[0]["reason"]),
                         (101, 102, "the price went back"))

    def test_alarms_read_whether_the_exam_was_over_only_after_the_stops_went_out(self):
        """Block 103: EXAM's reach (101-102) ends, an alarm; OTHER's stop, armed for 104, is reached and goes out first."""
        self.keeper.accounts[OTHER] = {"pool": POOL, "kind": "exam"}
        self.node.positions[(OTHER, BTC)] = 4
        loop = self.loop()
        self.node.logs += [set_log(100, 950_000, 101), set_log(100, 952_000, 104, exam=OTHER)]
        self.run_to(loop, 100)
        self.node.price.update({101: 949_000, 103: 951_000})
        loop.keeper.filler = False  # EXAM's reach is watched, not carried out
        self.run_to(loop, 101, 102)
        loop.keeper.filler = True
        before = len(self.node.calls)
        self.assertEqual(self.run_to(loop, 103), [[(OTHER, BTC, False)]])
        methods = [m for m, _ in self.node.calls[before:]]
        self.assertEqual(self.events("stop_missed")[0]["exam"], EXAM)
        self.assertLess(methods.index("eth_sendRawTransaction"), methods.index("eth_getBlockByNumber"))

    def test_a_keeper_send_the_node_was_busy_for_goes_out_in_a_moment(self):
        sender = sf.Sender(FILLER, self.node)
        self.node.send_answers = [c.NodeBusy("eth_sendRawTransaction: HTTP 429")]
        got = []
        keeper = threading.Thread(target=lambda: got.append(sender.submit(POOL, b"", 21_000, 10**8, wait=10)), daemon=True)
        keeper.start()
        end = time.monotonic() + 5
        while not got and time.monotonic() < end:
            sender.serve()
            time.sleep(0.05)
        keeper.join(2)
        self.assertEqual(got[0][1], 7, "sent after a second, on the nonce the busy node did not take")
        self.assertEqual(len(self.node.raw), 1)

    def test_a_keeper_send_not_sent_in_time_says_why(self):
        sender = sf.Sender(FILLER, self.node)
        self.node.send_answers = [c.NodeBusy("eth_sendRawTransaction: HTTP 429")] * 3
        done = []

        def keeper():
            try:
                sender.submit(POOL, b"", 21_000, 10**8, wait=0.3)
            except c.NotSent as exc:
                done.append(str(exc))

        queued = threading.Event()
        put = sender.requests.put
        sender.requests.put = lambda req, *a, **k: (put(req, *a, **k), queued.set())  # served only once it is queued
        thread = threading.Thread(target=keeper, daemon=True)
        thread.start()
        self.assertTrue(queued.wait(2))
        sender.serve()
        thread.join(2)
        self.assertIn("HTTP 429", done[0])
        self.assertEqual(sender.later, [], "cancelled: never sent after")

    def test_the_keepers_sends_go_out_between_the_blocks_of_a_long_step(self):
        loop = self.loop()
        self.armed_stop(loop)
        loop.sender.requests.put({"to": POOL, "data": b"", "gas": 21_000, "gas_price": 10**8, "state": "queued",
                                  "done": threading.Event(), "result": None, "error": None, "not_before": 0.0, "delay": 1.0})
        self.node.price[101] = 960_000
        before = len(self.node.calls)
        self.run_to(loop, 106)
        calls = self.node.calls[before:]
        sent_at = [i for i, (m, _) in enumerate(calls) if m == "eth_sendRawTransaction"][0]
        last_price = [i for i, (m, p) in enumerate(calls) if m == "eth_call" and p[0]["to"] == sf.ORACLE_PX][-1]
        self.assertLess(sent_at, last_price, "not held to the end of the step")

    def test_the_oldest_transaction_waiting_long_is_an_alarm_once(self):
        sender = sf.Sender(FILLER, self.node)
        raised = []
        sender.alarm = lambda event, **f: raised.append((event, f))
        sender.send(POOL, b"", 21_000, 10**8)
        sender.send(POOL, b"", 21_000, 10**8)
        for e in sender.ledger.values():
            e["at"] -= sf.RESEND_AFTER_S
            e["since"] -= sf.STUCK_S
        sender.sweep()
        for e in sender.ledger.values():
            e["at"] -= sf.RESEND_AFTER_S
        sender.sweep()
        self.assertEqual([(e, f["nonce"]) for e, f in raised], [("send_stuck", 7)], "the oldest, once")

    def test_a_read_error_of_the_keepers_node_names_no_path(self):
        loop = self.loop(wallet=None)
        url = "https://node.example:8545/v2/SECRETKEY?k=SECRETKEY"
        with self.assertRaises(RuntimeError) as caught:
            loop.reread(EXAM, call=lambda m, p=(): (_ for _ in ()).throw(
                RuntimeError(f"403 Client Error: Forbidden for url: {url}")))
        self.assertNotIn("SECRETKEY", str(caught.exception))
        self.assertIn("node.example", str(caught.exception))

    def test_failing_episodes_raise_one_alarm_an_hour(self):
        loop = self.loop(wallet=None)
        for _ in range(3):  # three episodes of a busy node, each over 30 seconds
            loop._failed("stop_node_busy", c.NodeBusy("x"))
            loop.failing_since -= sf.STALL_S
            loop._failed("stop_node_busy", c.NodeBusy("x"))
            loop.failing_since, loop.failing_said = None, False
        self.assertEqual(len(self.events("stop_loop_failing")), 1)
        self.assertEqual(len(self.events("stop_loop_failing_again")), 2)
        loop.failing_alarm_at -= sf.FAILING_REALARM_S
        loop._failed("stop_node_busy", c.NodeBusy("x"))
        loop.failing_since -= sf.STALL_S
        loop._failed("stop_node_busy", c.NodeBusy("x"))
        self.assertEqual(len(self.events("stop_loop_failing")), 2, "an hour later, again")

    def test_a_gap_past_the_read_back_with_stops_armed_is_an_alarm_at_once(self):
        loop = self.loop(wallet=None, backfill=3)
        self.armed_stop(loop)
        self.run_to(loop, 106)
        gap = self.events("stop_watch_gap")
        self.assertEqual((gap[0]["from_block"], gap[0]["to"], gap[0]["alarm"]), (101, 103, "ALARM"))
        self.node = Node()
        self.said.clear()
        loop = self.loop(wallet=None, backfill=3)
        self.run_to(loop, 100, 106)
        self.assertEqual(self.events("stop_watch_gap"), [], "nothing standing: a line, not an alarm")

    def test_a_connect_timeout_is_not_sent_and_spends_no_nonce(self):
        import requests
        node = sf.Node("http://127.0.0.1:9")
        with mock.patch.object(node.session, "post", side_effect=requests.ConnectTimeout("x")):
            with self.assertRaises(sf.Unreached):
                node.call("eth_blockNumber")
        sender = sf.Sender(FILLER, self.node)
        self.node.send_answers = [sf.Unreached("eth_sendRawTransaction: ConnectTimeout")]
        with self.assertRaises(sf.Busy):
            sender.send(POOL, b"", 21_000, 10**8)
        self.assertEqual((sender.next, sender.ledger), (7, {}), "the nonce is the next one's")
        self.assertEqual(sender.send(POOL, b"", 21_000, 10**8)[1], 7)


class TheThirdReview(Base):
    """The bots' review of #334."""

    def test_the_oldest_of_the_whole_ledger_is_the_one_stuck(self):
        sender = sf.Sender(FILLER, self.node)
        raised = []
        sender.alarm = lambda event, **f: raised.append((event, f))
        sender.send(POOL, b"", 21_000, 10**8)
        sender.send(POOL, b"", 21_000, 10**8)
        sender.ledger[7]["at"] -= 1  # just sent again: not due now, still the oldest
        sender.ledger[8]["at"] -= sf.RESEND_AFTER_S
        sender.ledger[8]["since"] -= sf.STUCK_S
        sender.sweep()
        self.assertEqual(raised, [], "nonce 8 waits behind nonce 7: no alarm for it")

    def test_the_chain_counted_past_twice_running_not_twice_in_all(self):
        sender = sf.Sender(FILLER, self.node)
        sender.send(POOL, b"", 21_000, 10**8)
        for latest in (8, 7, 8):
            self.node.latest = latest
            sender.ledger[7]["at"] -= sf.RESEND_AFTER_S
            sender.sweep()
        self.assertFalse(sender.is_gone(7), "8, 7, 8 is not two sweeps running")
        sender.ledger[7]["at"] -= sf.RESEND_AFTER_S
        sender.sweep()
        self.assertTrue(sender.is_gone(7))

    def test_a_keeper_send_refused_after_its_wait_is_not_sent_late(self):
        sender = sf.Sender(FILLER, self.node)
        self.node.send_answers = [c.NodeBusy("eth_sendRawTransaction: HTTP 429")]
        req = {"to": POOL, "data": b"", "gas": 21_000, "gas_price": 10**8, "state": "queued", "done": threading.Event(),
               "result": None, "error": None, "not_before": 0.0, "delay": 1.0, "until": time.monotonic() - 1}
        sender.handle(req)
        self.assertTrue(req["done"].is_set())
        self.assertIsInstance(req["error"], c.NotSent)
        self.assertEqual(sender.later, [], "not tried again past the keeper's wait")

    def test_scrub_takes_out_a_bad_address_and_a_bare_path(self):
        self.assertNotIn("SECRET", sf.scrub("bad http://SECRET:port/x"))
        said = sf.scrub("Max retries exceeded with url: /v2/SECRET?k=SECRET (Caused by ConnectTimeout)")
        self.assertNotIn("SECRET", said)
        self.assertIn("url: <path>", said)

    def test_a_stop_set_and_armed_inside_the_blocks_not_read_is_a_gap_alarm(self):
        loop = self.loop(wallet=None, backfill=3)
        self.run_to(loop, 100)
        self.node.logs.append(set_log(101, 950_000, 102))
        self.run_to(loop, 106)
        gap = self.events("stop_watch_gap")
        self.assertEqual((gap[0]["from_block"], gap[0]["to"], gap[0]["alarm"]), (101, 103, "ALARM"))

    def test_a_stop_gone_before_its_kept_prices_were_judged_is_an_alarm(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        self.node.logs += [set_log(100, 950_000, 101), cleared_log(102)]
        self.node.price.update({101: 900_000})
        with TheSecondReview.fails(self, 10):
            self.run_to(loop, 102)
        lost = self.events("stop_unjudged")
        self.assertEqual((lost[0]["from_block"], lost[0]["alarm"]), (101, "ALARM"))

    def test_an_alarm_says_the_side_it_was_judged_on(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        t = loop.triggers[(EXAM, BTC, False)]
        loop._judge((EXAM, BTC, False), t, 101, 900_000, 1)
        loop.signs[(EXAM, BTC)] = None
        self.assertEqual(loop._what((EXAM, BTC, False), t, 101)["side"], "long")

    def test_a_failed_read_of_the_past_keeps_the_kept_prices_first(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        self.node.logs.append(set_log(100, 950_000, 101))
        real = self.node.call

        def call(method, params=()):
            if method == "eth_call" and params[0]["data"].startswith(SEL["positionOf(uint32)"]) and int(params[1], 16) < 101:
                raise RuntimeError("eth_call: HTTP 403")
            return real(method, params)

        with TheSecondReview.fails(self, 2):
            self.run_to(loop, 101)  # the position's reads fail: block 101's price is kept
        with mock.patch.object(self.node, "call", side_effect=call):
            self.run_to(loop, 102)  # the read now works, the read of the past does not
        self.assertIn((EXAM, BTC), loop.unread_signs, "read again at the next step")
        self.assertIsNone(loop.signs[(EXAM, BTC)])
        self.assertEqual([b for b, _ in loop.triggers[(EXAM, BTC, False)]["unjudged"]], [101, 102],
                         "the newer block kept behind the older one")

    def test_an_exam_that_ended_keeps_no_moves(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.logs.append(filled_log(101))
        self.run_to(loop, 101)
        self.assertIn((EXAM, BTC), loop.moves)
        loop._ended(EXAM, 101, "the exam ended")
        self.assertNotIn((EXAM, BTC), loop.moves)


class OffTheChainsRecord(Base):
    """What a stop needs when the journal is the record: a node that is not stale, a loop not behind, a key that can
    pay, and a stop's transaction that does not wait ten seconds when it did not land."""

    def test_an_old_head_is_an_alarm_once_and_a_fresh_one_a_line(self):
        loop = self.loop(wallet=None)
        self.later = sf.STALE_HEAD_S + 1
        loop._head_fresh(100, 1_000_100)
        loop._head_fresh(101, 1_000_101)
        stale = self.events("stop_node_stale")
        self.assertEqual((len(stale), stale[0]["alarm"]), (1, "ALARM"))
        self.later = 0
        loop._head_fresh(102, 1_000_102)
        self.assertEqual(len(self.events("stop_node_fresh_again")), 1)

    def test_a_loop_short_of_the_head_is_an_alarm_once(self):
        loop = self.loop(wallet=None)
        loop.block = 100
        loop._behind(100)
        loop.block = 90
        self.later = sf.BEHIND_S - 1
        loop._behind(100)
        self.assertEqual(self.events("stop_loop_behind"), [])
        self.later = sf.BEHIND_S + 1
        loop._behind(100)
        loop._behind(100)
        self.assertEqual([f["alarm"] for f in self.events("stop_loop_behind")], ["ALARM"])

    def test_a_key_that_pays_for_few_stops_is_an_alarm_once(self):
        loop = self.loop()
        self.node.balance = sf.STOP_GAS * (2 * 100_000_000) * (sf.MIN_RUNWAY_FILLS - 1)
        loop._balance()
        loop.balance_at = -sf.BALANCE_EVERY_S
        loop._balance()
        low = self.events("stop_key_low")
        self.assertEqual((len(low), low[0]["fills_left"], low[0]["alarm"]), (1, sf.MIN_RUNWAY_FILLS - 1, "ALARM"))
        self.node.balance = 10**18
        loop.balance_at = -sf.BALANCE_EVERY_S
        loop._balance()
        self.assertEqual(len(self.events("stop_key_topped_up")), 1)

    def test_the_run_reads_the_heads_time_and_checks_it(self):
        loop = self.loop(wallet=None)
        self.node.stale = int(sf.STALE_HEAD_S) + 5
        stop = threading.Event()
        loop._served = lambda until, s: stop.set()
        loop.run(0.01, stop)
        self.assertEqual(loop.head_t, {100: 1_000_100 - self.node.stale})
        self.assertEqual(len(self.events("stop_node_stale")), 1)

    def test_a_stop_that_did_not_land_is_signed_again_with_higher_fees_under_its_nonce(self):
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        first = self.node.sent()[0]
        loop.sender.sweep(time.monotonic() + sf.BUMP_AFTER_S)
        again = self.node.sent()
        self.assertEqual(len(again), 2, "after two seconds, not ten")
        self.assertEqual(again[1]["nonce"], first["nonce"], "the same nonce: only one can land")
        self.assertEqual(again[1]["data"], first["data"])
        self.assertEqual(self.events("send_bumped")[0]["bumps"], 1)
        e = loop.sender.ledger[first["nonce"]]
        self.assertEqual(len(e["hashes"]), 2)
        self.assertGreater(e["max_fee"], 2 * 100_000_000, "the fee cap a quarter higher: a replacement the node takes")
        # The second signing lands: the loop reads its receipt, whichever it was.
        self.node.receipts[e["hashes"][1]] = {"status": "0x1", "blockNumber": hex(103)}
        loop._receipts(103)
        self.assertEqual(loop.pending, {})
        self.assertEqual(len(self.events("trigger_filled")), 1)

    def test_a_keepers_transaction_is_not_bumped(self):
        sender = sf.Sender(FILLER, self.node)
        sender.send(POOL, b"", 21_000, 10**8)
        sender.sweep(time.monotonic() + sf.BUMP_AFTER_S)
        self.assertEqual(len(self.node.raw), 1, "a keeper's waits RESEND_AFTER_S and goes again as it is")


class TheLog(unittest.TestCase):
    def test_a_line_is_written_under_the_lock(self):
        from ops import keeper as core
        held = []
        out = mock.Mock()
        out.write.side_effect = lambda line: held.append(core._LOG_LOCK.locked())
        with mock.patch.object(core.sys, "stdout", out):
            core.log("stop_missed")
        self.assertEqual(held, [True])

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

    def test_its_sends_go_through_the_loops_one_sender(self):
        k = self.keeper()
        k.outbox = mock.Mock()
        k.outbox.send_and_wait.return_value = {"transactionHash": "0x01", "blockNumber": hex(5), "logs": []}
        with mock.patch.object(k, "is_filler", return_value=True), mock.patch.object(sk.c, "send_tx") as old:
            k.fill(EXAM)
        old.assert_not_called()
        self.assertEqual(k.outbox.send_and_wait.call_args[1]["gas"], sk.FILL_GAS)

    def main(self, env, argv=("--deployment", "sim1", "--dry-run", "--from", "0x" + "44" * 20)):
        """The keeper's main, one turn of its loop, with nothing real behind it."""
        class Out(Exception):
            pass
        k = self.keeper()
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(sk.deployments, "load", return_value={"SimPoolFactory": "0x" + "33" * 20, "block": 1}), \
                mock.patch.object(sk.sn, "confirm_service"), mock.patch.object(sk, "SimKeeper", return_value=k), \
                mock.patch.object(k, "slow_pass"), mock.patch.object(k, "fast_step"), \
                mock.patch.object(sk.c, "rpc", return_value="0x1"), mock.patch.object(sk.time, "sleep", side_effect=Out), \
                mock.patch.object(sf, "build") as build, mock.patch.object(sf, "start") as start, \
                mock.patch.object(sf, "keep_alive") as alive:
            with self.assertRaises(Out):
                sk.main(list(argv))
        return k, build, start, alive

    def test_by_default_the_loop_starts_and_inline_is_the_keeper_as_it_was(self):
        import os
        env = {k_: v for k_, v in os.environ.items() if k_ != "COLOSSEUM_STOPS"}
        with mock.patch.dict("os.environ", env, clear=True):
            k, build, start, alive = self.main({})
        build.assert_called_once()
        start.assert_called_once()
        k, build, start, alive = self.main({"COLOSSEUM_STOPS": "inline"})
        build.assert_not_called()
        start.assert_not_called()
        self.assertIsNone(k.stop_loop)
        self.assertIsNone(k.outbox)

    def test_asked_for_the_loop_starts_and_is_kept_alive(self):
        k, build, start, alive = self.main({"COLOSSEUM_STOPS": "loop"})
        build.assert_called_once()
        self.assertIsNone(build.call_args[1]["wallet"], "a dry run hands the loop no key")
        start.assert_called_once()
        alive.assert_called_once()

    def test_without_it_as_before(self):
        k = self.keeper()
        with mock.patch.object(k, "is_filler", return_value=True), \
                mock.patch.object(sk.c, "send_tx", return_value={"transactionHash": "0x01", "blockNumber": hex(5)}) as old:
            k.fill(EXAM)
        old.assert_called_once()


if __name__ == "__main__":
    unittest.main()
