"""The stops' loop and its watch (ops/stop_filler.py): a stop reached is sent in the step that sees it, nothing waits on
anything else, a busy node costs a look and no block, and a reach with no fill is an alarm. On a node made up here.

The node made up here has Multicall3: a look is one eth_call that says the block's number, its time and every price.
Every test of the loop runs a second time on a node without it (`the_old_way`, at the end), where the loop is as it
was: the head, then a call a price."""
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
                                                              "lastFillAt()", "terms()", "result()", "rules()",
                                                              "stopOf(uint32)", "takeOf(uint32)", "getBlockNumber()",
                                                              "getCurrentBlockTimestamp()",
                                                              "aggregate3((address,bool,bytes)[])")}
ORACLE_OF = {a: "0x" + encode(["uint32"], [a]).hex() for a in (0, 3, 4)}  # the data of an oracle read, by asset
# The requests of one measure of the node's numbering (`sk.numbering`): twice, each a call between two reads of the
# head with the head's time asked for by its number, taken when the two agree (review 20 of #342).
MEASURE = ["eth_blockNumber", "eth_call", "eth_blockNumber", "eth_getBlockByNumber"] * 2


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
        self.multicall = True  # Multicall3 stands on this chain
        self.ahead = 0  # 1: a call at `latest` says the next block's number (HyperEVM's own nodes), with the head's time
        self.ignores_tag = False  # a batch asked for a block by its number is answered at the head all the same
        self.no_past = False  # a batch asked for a block that is no longer the head is not answered
        self.unread_assets: set[int] = set()  # assets whose oracle read fails, in any block
        self.batch_fails = 0  # the next batches at `latest` that fail whole
        self.batch_unread: set[tuple[str, str]] = set()  # (to, selector) of reads that fail inside a batch, not alone
        self.stops: dict[tuple[str, int, bool], tuple[int, int]] = {}  # (exam, asset, take) -> (px, armed), as the exam holds them
        self.exam_assets = [BTC]
        # Every read of the chain's state, alone or inside a batch: (to, data, the block it was answered at).
        self.reads: list[tuple[str, str, int]] = []
        self.counts: dict[str, int] = {}

    def oracle(self, block: int) -> int:
        past = [b for b in self.price if b <= block]
        return self.price[max(past)] if past else 1_000_000

    def taken(self) -> dict[str, int]:
        counts, self.counts = self.counts, {}
        return counts

    def price_reads(self, asset: int = BTC) -> list[int]:
        """The blocks the oracle price of `asset` was read at, in order, alone or inside a batch."""
        return [block for to, data, block in self.reads if to.lower() == sf.ORACLE_PX.lower() and data == ORACLE_OF[asset]]

    def batches(self) -> list[tuple[str, int]]:
        """The batches asked for: (the tag, how many reads besides the block's two)."""
        return [(p[1], len(decode(["(address,bool,bytes)[]"], bytes.fromhex(p[0]["data"][10:]))[0]) - 2)
                for m, p in self.calls if m == "eth_call" and p[0]["to"].lower() == sk.MULTICALL3.lower()]

    def time_of(self, block: int) -> int:
        return 1_000_000 + block - self.stale

    def batch(self, data: str, tag: str) -> str:
        """Multicall3's aggregate3, as a node answers it: every read at one block, each allowed to fail alone."""
        if tag == "latest" and self.batch_fails:
            self.batch_fails -= 1
            raise RuntimeError("eth_call: {'code': -32000, 'message': 'out of gas'}")
        if tag != "latest" and self.no_past and int(tag, 16) != self.head:
            raise RuntimeError("eth_call: {'code': -32000, 'message': 'header not found'}")
        block = self.head if tag == "latest" or self.ignores_tag else int(tag, 16)
        out = []
        for to, may_fail, inner in decode(["(address,bool,bytes)[]"], bytes.fromhex(data[10:]))[0]:
            inner = "0x" + inner.hex()
            try:
                if to.lower() == sk.MULTICALL3.lower():
                    answer = encode(["uint256"], [block + self.ahead if inner == SEL["getBlockNumber()"] else self.time_of(block)])
                elif (to_checksum_address(to), inner[:10]) in self.batch_unread:
                    raise RuntimeError("execution reverted")
                else:
                    answer = bytes.fromhex(self.read(to_checksum_address(to), inner, block)[2:])
                out.append((True, answer))
            except c.NodeBusy:
                raise
            except Exception:
                if not may_fail:
                    raise RuntimeError("eth_call: execution reverted: Multicall3: call failed") from None
                out.append((False, b""))
        return "0x" + encode(["(bool,bytes)[]"], [out]).hex()

    def read(self, to: str, data: str, block: int, sender=None) -> str:
        """One read of the chain's state at `block`: the same answer alone and inside a batch."""
        self.reads.append((to, data, block))
        if to.lower() == sf.ORACLE_PX.lower():
            if block in self.unreadable or decode(["uint32"], bytes.fromhex(data[2:]))[0] in self.unread_assets:
                raise RuntimeError("header not found")
            return "0x" + encode(["uint64"], [self.oracle(block)]).hex()
        if data.startswith(SEL["positionOf(uint32)"]):
            asset = decode(["uint32"], bytes.fromhex(data[10:]))[0]
            return "0x" + encode(["int256"], [self.positions.get((to_checksum_address(to), asset), 0)]).hex()
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
        if data == SEL["rules()"]:
            return "0x" + encode([sk.RULES], [(300, 600, 1000, self.exam_assets, [])]).hex()
        for take, getter in ((False, "stopOf(uint32)"), (True, "takeOf(uint32)")):
            if data.startswith(SEL[getter]):
                asset = decode(["uint32"], bytes.fromhex(data[10:]))[0]
                px, armed = self.stops.get((to_checksum_address(to), asset, take), (0, 0))
                return "0x" + encode(["uint64", "uint64", "uint64", "uint64"], [px, 0, armed, 0]).hex()
        raise AssertionError(f"the loop read {data} of {to}")

    def call(self, method, params=()):
        self.calls.append((method, params))
        self.counts[method] = self.counts.get(method, 0) + 1
        if method in self.busy:
            raise c.NodeBusy(f"{method}: HTTP 429")
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_chainId":
            return hex(c.CHAIN_ID)
        if method == "eth_getCode":
            return "0x" + "60" * 3808 if self.multicall and params[0].lower() == sk.MULTICALL3.lower() else "0x"
        if method == "eth_gasPrice":
            return hex(100_000_000)
        if method == "eth_getTransactionCount":
            return hex(self.count if params[1] == "pending" or self.latest is None else self.latest)
        if method == "eth_getBlockByNumber":
            block = self.head if params[0] == "latest" else int(params[0], 16)
            return {"number": hex(block), "timestamp": hex(self.time_of(block))}
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
            if q["to"].lower() == sk.MULTICALL3.lower():
                if not self.multicall:
                    raise AssertionError("the loop asked a node with no Multicall3 for a batch")
                return self.batch(q["data"], tag)
            return self.read(q["to"], q["data"], self.head if tag == "latest" else int(tag, 16))
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
    # Whether the node made up for the test has Multicall3. Each class of tests runs again without (`the_old_way`).
    MULTICALL = True

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
        self.node.multicall = self.MULTICALL
        made = sf.StopLoop(self.keeper, self.node, wallet=wallet, sender=sender, tip_wei=tip, journal=self.journal,
                           alarm=alarm, multicall=self.MULTICALL, **kw)
        # The node's numbering as `run` measures it before its first look (TheNodesNumbering measures it for real).
        made.ahead, made.ahead_at = self.node.ahead, time.monotonic()
        return made

    def run_to(self, loop, *heads):
        """Each head as the loop's `run` takes it: one look at it, then a step to it with what the look read. Without
        Multicall3 the look is the head and its time alone, and the step reads the prices."""
        out = []
        for h in heads:
            self.node.head = h
            if loop.multicall:
                head, head_t, look = loop._head()
                loop.head_t[head] = head_t
                out.append(loop.step(head, look))
            else:
                loop.head_t[h] = self.node.time_of(h)  # the fake node's time of a block
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
        # The blocks BTC's price was read at, alone (a call a price) or in the block's one call: the same order.
        self.assertEqual(self.node.price_reads(), [100, 103, 101, 102], "the head's price first, then the blocks between")
        methods = [m for m, _ in self.node.calls]
        self.assertLess(methods.index("eth_sendRawTransaction"), methods.index("eth_getLogs", methods.index("eth_getLogs") + 1),
                        "sent before the read back")
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
        # With Multicall3 the look at the head is its one call, the platform's prices in it, and the first look of a start
        # held against the node's record of its block (review 20 of #342, L1); nothing else either way.
        self.assertEqual([m for m, _ in self.node.calls], ["eth_call", "eth_getBlockByNumber", "eth_call"] if self.MULTICALL else [])
        self.assertEqual([tag for tag, _ in self.node.batches()], ["latest", "latest"] if self.MULTICALL else [])
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
        self.node.unread_assets = {4}  # its read fails, alone or inside the block's one call
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
        last_price = [i for i, (m, p) in enumerate(calls) if m == "eth_call" and p[0]["to"].lower() in (
            sf.ORACLE_PX.lower(), sk.MULTICALL3.lower())][-1]  # a price read alone, or a block's one call
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

    def test_a_stop_replaced_before_its_kept_prices_were_judged_is_an_alarm_too(self):
        """setTrigger sets a stop over the one standing with no TriggerCleared, and a reread of the exam can find it
        moved. Either way the stop replaced is gone: prices kept for it while its position was not read are said
        (`stop_unjudged`), as for one taken off, never lost with it."""
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        self.node.logs += [set_log(100, 950_000, 101), set_log(102, 940_000, 103)]
        self.node.price.update({101: 900_000})
        with TheSecondReview.fails(self, 10):
            self.run_to(loop, 102)
        lost = self.events("stop_unjudged")
        self.assertEqual([(e["from_block"], e["px"], e["alarm"]) for e in lost], [(101, 950_000, "ALARM")])
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["px"], 940_000, "the new stop stands")
        self.assertFalse(loop.triggers[(EXAM, BTC, False)].get("unjudged"))

    def test_a_stop_a_reread_finds_moved_before_its_kept_prices_were_judged_is_an_alarm_too(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 99)
        self.node.logs.append(set_log(100, 950_000, 101))
        self.node.price.update({101: 900_000})
        with TheSecondReview.fails(self, 10):
            self.run_to(loop, 101)
        self.assertEqual([b for b, _ in loop.triggers[(EXAM, BTC, False)]["unjudged"]], [101])
        loop.apply_reread(EXAM, 101, {(EXAM, BTC, False): (940_000, 103)}, {})
        lost = self.events("stop_unjudged")
        self.assertEqual([(e["from_block"], e["px"], e["alarm"]) for e in lost], [(101, 950_000, "ALARM")])
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["px"], 940_000)

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
                mock.patch.object(k, "slow_pass"), mock.patch.object(k, "fast_step") as fast, \
                mock.patch.object(sk.c, "rpc", return_value="0x1"), mock.patch.object(sk.time, "sleep", side_effect=Out), \
                mock.patch.object(k, "fed_step", side_effect=Out) as fed, \
                mock.patch.object(sf, "build") as build, mock.patch.object(sf, "start") as start, \
                mock.patch.object(sf, "keep_alive") as alive:
            # One turn ends where the keeper waits: for the stops' loop's next step with the loop, in its sleep without.
            with self.assertRaises(Out):
                sk.main(list(argv))
        self.fed, self.fast = fed, fast
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
        # One look at the head and one read of the logs for both loops: the loop is told to hand its steps on, and the
        # keeper waits for them; it makes no fast step of its own look.
        build.return_value.share.assert_called_once_with()
        self.fed.assert_called_once()
        self.fast.assert_not_called()
        k, build, start, alive = self.main({"COLOSSEUM_STOPS": "inline"})
        self.fed.assert_not_called()
        self.fast.assert_called_once()

    def test_without_it_as_before(self):
        k = self.keeper()
        with mock.patch.object(k, "is_filler", return_value=True), \
                mock.patch.object(sk.c, "send_tx", return_value={"transactionHash": "0x01", "blockNumber": hex(5)}) as old:
            k.fill(EXAM)
        old.assert_called_once()


class TheOneCall(Base):
    """With Multicall3 a look is one eth_call: the block's number, its time and every price, all of one state. The line
    of the journal takes its number and its time from that call, and a block the node does not answer for by its
    number is a gap, never a line with another block's price."""
    ONE_CALL_ONLY = True

    def rows(self) -> list[dict]:
        return [json.loads(x) for f in sorted(self.dir.glob("prices-*.jsonl")) for x in f.read_text().splitlines()[1:] if x.strip()]

    def test_the_numbers_of_multicall3_are_its_own(self):
        """Multicall3 (mds1/multicall3) is at one address on every chain; its three functions by their selectors."""
        self.assertEqual(sk.MULTICALL3, "0xcA11bde05977b3631167028862bE2a173976CA11")
        self.assertEqual(SEL["aggregate3((address,bool,bytes)[])"], "0x82ad56cb")
        self.assertEqual(SEL["getBlockNumber()"], "0x42cbb15c")
        self.assertEqual(SEL["getCurrentBlockTimestamp()"], "0x0f28c97d")
        self.assertEqual("0x" + sk.c.encode_call(sk.AGGREGATE3)[:4].hex(), "0x82ad56cb")
        self.assertEqual(["0x" + d.hex() for d in sk.BLOCK_READS], ["0x42cbb15c", "0x0f28c97d"])

    def test_a_look_is_one_call_and_the_line_takes_its_number_and_its_time_from_it(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price[101] = 960_000
        before = len(self.node.calls)
        self.run_to(loop, 101)
        asked = self.node.calls[before:]
        self.assertEqual([m for m, _ in asked], ["eth_call", "eth_getLogs"], "one call for the head, its time and its "
                         "prices, and the block's logs: no eth_blockNumber, no eth_getBlockByNumber, no call a price")
        self.assertEqual(self.node.batches()[-1], ("latest", 3), "at `latest`, the platform's three prices in it")
        row = self.rows()[-1]
        self.assertEqual((row["b"], row["t"], row["px"]["3"]), (101, self.node.time_of(101), 960_000))
        # The number and the time are the call's own: a node that says another time for the block says it in the line.
        with mock.patch.object(self.node, "time_of", side_effect=lambda b: 5_000_000 + b):
            self.run_to(loop, 102)
        self.assertEqual((self.rows()[-1]["b"], self.rows()[-1]["t"]), (102, 5_000_102))

    def test_a_look_at_a_head_already_looked_at_is_its_one_call_and_nothing_more(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        before = len(self.node.calls)
        self.run_to(loop, 100, 100)
        self.assertEqual([m for m, _ in self.node.calls[before:]], ["eth_call", "eth_call"])

    def test_a_node_that_answers_a_number_with_another_block_gives_a_gap_and_never_a_line(self):
        """Two blocks went by between two looks. Asked for each by its number, an honest node answers for it; a node
        that answers with its head (the public ones do, read on 24 November 2026), or with nothing for a block that
        is no longer its last, gives no line: the price of block 103 is never written under 101 or 102."""
        for how, lines in (("honest", {100: 1_000_000, 101: 111, 102: 222, 103: 333}),
                           ("ignores_tag", {100: 1_000_000, 103: 333}), ("no_past", {100: 1_000_000, 103: 333})):
            self.node = Node()
            self.said.clear()
            for f in self.dir.glob("prices*"):
                f.unlink()
            self.journal = None
            if how != "honest":
                setattr(self.node, how, True)
            loop = self.loop(wallet=None)
            self.run_to(loop, 100)
            self.node.price.update({101: 111, 102: 222, 103: 333})
            self.run_to(loop, 103)
            journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
            self.assertEqual(journal.px[BTC], lines, how)
            self.assertEqual(journal.times, {b: self.node.time_of(b) for b in lines}, "each line's time by its own call")
            self.assertEqual({m for m, _ in self.node.calls} - {"eth_getBlockByNumber"}, {"eth_call", "eth_getLogs"})
            self.assertEqual([p[0] for m, p in self.node.calls if m == "eth_getBlockByNumber"], [hex(100), hex(103)],
                             "no block asked for apart but the heads', by their numbers: the first look of a start, and "
                             "a look that skips blocks, are held against the node's record of their block (review 20 "
                             "of #342, and its L1)")
            if how == "honest":
                # The first block read back is taken once the node's answers by number hold against the look at 100.
                self.assertEqual(self.node.batches()[-4:], [("latest", 3), (hex(101), 3), (hex(100), 3), (hex(102), 3)])
            else:
                self.assertEqual(self.node.batches()[-3:], [("latest", 3), (hex(101), 3), (hex(102), 3)], how)
            gaps = [(lo, hi) for lo, hi, _ in journal.gaps]
            self.assertEqual(gaps, [] if how == "honest" else [(101, 101), (102, 102)], how)
            if how != "honest":
                self.assertTrue(all("not read back" in why for _, _, why in journal.gaps))
                self.assertEqual(self.events("stop_watch_blind")[0]["from_block"], 101)
                self.assertEqual(self.events("stop_read_back_gap"), [], "no stop standing: a line and a gap, no alarm")

    def test_a_block_not_read_back_with_a_stop_standing_is_an_alarm_once_an_hour(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.ignores_tag = True
        self.run_to(loop, 103)
        alarms = self.events("stop_read_back_gap")
        self.assertEqual([(a["block"], a["blocks"], a["alarm"]) for a in alarms], [(101, 1, "ALARM")], "once, at once")
        self.assertIn("answered for block 103", alarms[0]["reason"])
        self.run_to(loop, 106)
        self.assertEqual(len(self.events("stop_read_back_gap")), 1, "the next ones inside the hour are counted, not said")
        loop.read_back_said_at -= sf.READ_BACK_REALARM_S
        self.run_to(loop, 109)
        self.assertEqual([(a["block"], a["blocks"]) for a in self.events("stop_read_back_gap")], [(101, 1), (107, 4)])
        # The reach is judged across the blocks not read as across any not judged: nothing is raised for them.
        self.assertEqual(self.events("stop_missed") + self.events("stop_overdue"), [])

    def test_the_read_backs_of_a_step_are_no_more_than_its_read_back(self):
        loop = self.loop(wallet=None, backfill=4)
        self.run_to(loop, 100)
        self.run_to(loop, 130)
        tags = [tag for tag, _ in self.node.batches()]
        self.assertEqual(tags, ["latest", "latest", hex(127), hex(128), hex(129)], "three read back, the rest a gap")
        journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertIn((101, 126, "the head ran ahead of the loop: their prices were not read back"), journal.gaps)

    def test_one_asset_that_fails_leaves_the_line_the_others_prices(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.unread_assets = {4}
        self.node.price[101] = 123
        before = len(self.node.calls)
        self.run_to(loop, 101, 102)
        row = self.rows()[-2]
        self.assertEqual((row["b"], row["px"], row["unread"]), (101, {"0": 123, "3": 123}, [4]))
        self.assertEqual([m for m, _ in self.node.calls[before:]], ["eth_call", "eth_getLogs"] * 2,
                         "the one that failed is not asked for again: a call and the logs a block, as before")

    def test_the_platforms_assets_go_first_in_the_call_and_a_stops_asset_after_them(self):
        """A read of the oracle for an asset it does not have burns the gas left to the call: the platform's three
        are read before any other, and nothing but the platform's and the assets of known stops is asked for."""
        other = 2  # an asset below the platform's in number, with a stop standing
        ORACLE_OF[other] = "0x" + encode(["uint32"], [other]).hex()
        self.node.positions[(EXAM, other)] = 5
        loop = self.loop(wallet=None)
        self.node.logs.append(set_log(100, 950_000, 101, asset=other))
        self.run_to(loop, 100)
        self.node.reads.clear()
        self.run_to(loop, 101)
        order = [decode(["uint32"], bytes.fromhex(data[2:]))[0] for to, data, _ in self.node.reads if to.lower() == sf.ORACLE_PX.lower()]
        self.assertEqual(order, [0, 3, 4, other])

    def test_a_stop_set_on_another_asset_since_the_look_has_its_price_read_at_the_blocks_number(self):
        """A stop on an asset outside the platform's, set in the block before the head and found in this step: the look
        at the head did not ask for its price. It is read by the head's number in one more call, once, and taken
        only if the node answers for that very block; if not, it is unread in the line, never another block's."""
        other = 2
        ORACLE_OF[other] = "0x" + encode(["uint32"], [other]).hex()
        for moved_on in (False, True):
            self.node = Node()
            for f in self.dir.glob("prices*"):
                f.unlink()
            self.node.positions[(EXAM, other)] = 5
            loop = self.loop(wallet=None)
            self.run_to(loop, 100)
            self.node.logs.append(set_log(101, 950_000, 102, asset=other))
            self.node.price[101] = 777
            real = self.node.call

            def call(method, params=()):
                out = real(method, params)
                if method == "eth_getLogs" and moved_on:
                    self.node.head, self.node.ignores_tag = 103, True  # the node went on, and answers a number with its head
                return out

            with mock.patch.object(self.node, "call", side_effect=call):
                self.run_to(loop, 102)
            row = [r for r in self.rows() if r.get("b") == 102][-1]
            # The look, block 101 read back (taken once the node's answers by number hold against the look at 100,
            # review 20 of #342), and the one price the look did not ask for.
            if moved_on:
                self.assertEqual(self.node.batches()[-3:], [("latest", 3), (hex(101), 3), (hex(102), 1)])
            else:
                self.assertEqual(self.node.batches()[-4:], [("latest", 3), (hex(101), 3), (hex(100), 3), (hex(102), 1)])
            if moved_on:
                self.assertEqual((row["px"], row["unread"]), ({"0": 777, "3": 777, "4": 777}, [other]))
            else:
                self.assertEqual(row["px"], {"0": 777, "2": 777, "3": 777, "4": 777})
            before = len(self.node.calls)
            self.node.ignores_tag = False
            self.run_to(loop, 103)
            self.assertEqual(self.node.batches()[-1], ("latest", 4), "from the next look on it is in the look")
            self.assertEqual([m for m, _ in self.node.calls[before:]].count("eth_call"), 1)

    def test_the_call_failing_whole_makes_this_look_the_old_way_and_the_next_one_again_one_call(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.batch_fails = 1
        self.node.price[101] = 555
        before = len(self.node.calls)
        self.run_to(loop, 101)
        asked = [m for m, _ in self.node.calls[before:]]
        self.assertEqual(asked, ["eth_call", "eth_getBlockByNumber", "eth_call", "eth_call", "eth_call", "eth_getLogs"],
                         "the call that failed, then the head and a call a price")
        self.assertEqual(self.node.price_reads()[-1], 101)
        self.assertEqual(self.rows()[-1]["px"], {"0": 555, "3": 555, "4": 555})
        self.assertEqual(len(self.events("stop_look_failed")), 1)
        before = len(self.node.calls)
        self.run_to(loop, 102)
        self.assertEqual([m for m, _ in self.node.calls[before:]], ["eth_call", "eth_getLogs"])
        self.node.batch_fails = 1
        before = len(self.node.calls)
        self.run_to(loop, 102)
        self.assertEqual([m for m, _ in self.node.calls[before:]], ["eth_call", "eth_getBlockByNumber"],
                         "a head already looked at: no price is asked for")

    def test_a_block_not_read_back_counts_as_a_block_with_no_price(self):
        """A node that answers no block by its number, and looks that miss every other block: half the blocks are
        gaps. That is an alarm too, with no stop standing: the journal has no price for them."""
        loop = self.loop(wallet=None)
        self.node.ignores_tag = True
        self.run_to(loop, *range(100, 100 + 2 * sf.UNPRICED_MIN, 2))
        said = self.events("stop_prices_unread")
        self.assertEqual([(a["blocks"], a["without"]) for a in said], [(sf.UNPRICED_MIN, sf.UNPRICED_MIN // 2 - 1 + 1)])
        self.assertEqual(self.events("stop_read_back_gap"), [])

    def test_a_busy_node_is_not_asked_the_old_way(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.busy = {"eth_call"}
        self.node.head = 101
        before = len(self.node.calls)
        with self.assertRaises(c.NodeBusy):
            loop._head()
        self.assertEqual([m for m, _ in self.node.calls[before:]], ["eth_call"], "it costs the look, not four more calls")


class TheNodesNumbering(Base):
    """HyperEVM's own nodes run a call at `latest` as the block to come: the call says the next block's number with the
    head's time (read on both networks on 24 November 2026). Others run it as the head itself. The loop measures
    which, between two reads of the head that agree, and counts the call's number back: the head is a block that
    exists, its logs are there to read, and the line is the head's."""
    ONE_CALL_ONLY = True

    def test_the_head_is_the_calls_number_less_the_nodes_one(self):
        for ahead in (0, 1):
            self.node = Node()
            self.node.ahead = ahead
            for f in self.dir.glob("prices*"):
                f.unlink()
            loop = self.loop(wallet=None)
            loop.ahead = None  # as at the start: measured before the first look
            self.node.price[100] = 4_242
            stop = threading.Event()
            loop._served = lambda until, s: stop.set()
            loop.run(0.01, stop)
            self.assertEqual(loop.ahead, ahead)
            self.assertEqual([m for m, _ in self.node.calls], MEASURE + ["eth_call", "eth_getBlockByNumber", "eth_getLogs"],
                             "measured once, then the look, held against the node's record of its block as the first "
                             "of a start (review 20 of #342, L1), and its logs")
            logs = [p[0] for m, p in self.node.calls if m == "eth_getLogs"][0]
            self.assertEqual((logs["fromBlock"], logs["toBlock"]), (hex(100), hex(100)), "the head, not the block to come")
            journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
            self.assertEqual((journal.px[BTC], journal.times), ({100: 4_242}, {100: self.node.time_of(100)}))
            self.assertEqual(loop.block, 100)

    def test_a_block_asked_for_by_its_number_is_taken_by_the_same_count(self):
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.price.update({101: 111, 102: 222})
        self.run_to(loop, 102)
        self.assertEqual(pr.load_prices(pr.journal_text(self.dir / "prices.jsonl")).px[BTC], {100: 1_000_000, 101: 111, 102: 222})

    def test_a_block_that_comes_between_the_two_reads_is_measured_again(self):
        loop = self.loop(wallet=None)
        loop.ahead = None
        real = self.node.call
        moves = {"left": 1}

        def call(method, params=()):
            out = real(method, params)
            if method == "eth_call" and moves["left"]:
                moves["left"] -= 1
                self.node.head += 1  # a block between the two reads of the head
            return out

        with mock.patch.object(self.node, "call", side_effect=call):
            loop._numbering()
        self.assertEqual(loop.ahead, 0, "the first measure said 1 with the head moved under it: not taken")
        self.assertEqual([m for m, _ in self.node.calls], ["eth_blockNumber", "eth_call", "eth_blockNumber"] + MEASURE)

    def test_a_number_that_is_neither_the_heads_nor_the_next_ends_the_one_call_with_an_alarm(self):
        self.node.ahead = 2
        loop = self.loop(wallet=None)
        loop.ahead = None
        self.run_to(loop, 100, 101)
        self.assertFalse(loop.multicall)
        off = self.events("stop_one_call_off")
        self.assertEqual((len(off), off[0]["alarm"]), (1, "ALARM"))
        self.assertEqual(self.node.batches(), [("latest", 0)], "the measure's own call, and none after")
        self.assertEqual(sorted(pr.load_prices(pr.journal_text(self.dir / "prices.jsonl")).px[BTC]), [100, 101],
                         "the loop goes on as it was without Multicall3")

    def test_with_the_numbering_not_measured_the_look_is_made_the_old_way_and_measured_again_at_the_next(self):
        """The measure cannot be made (its call fails, or the head moves at each try). The loop does not stand still
        for it, nor the keeper's fast loop behind it: the look is the head and a call a price, a block missed
        meanwhile is a gap, and the measure is tried again once NUMBERING_RETRY_S have gone by, not at every look
        (review 20 of #342, L3: up to 24 requests a look, 577 then 1 141 a minute on testnet on 25 November 2026)."""
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        loop.ahead = None
        feed = loop.share()
        self.node.price.update({100: 4_242, 102: 4_343})
        with mock.patch.object(sk, "numbering", side_effect=RuntimeError("eth_call: {'code': -32000, 'message': 'x'}")) as measure:
            self.run_to(loop, 100)
            self.assertEqual([m for m, _ in self.node.calls], ["eth_getBlockByNumber", "eth_call", "eth_call", "eth_call",
                                                               "eth_getLogs"], "the head, a call a price, the logs")
            self.assertEqual(self.node.batches(), [])
            self.run_to(loop, 102)
            self.assertEqual(measure.call_count, 1, "not measured again at the next look, a moment later")
        journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual(journal.px[BTC], {100: 4_242, 102: 4_343}, "the head's own number, not the call's next one")
        self.assertEqual([(lo, hi) for lo, hi, _ in journal.gaps], [(101, 101)])
        self.assertIn("not measured", journal.gaps[0][2])
        self.assertEqual([feed.get_nowait()[:2], feed.get_nowait()[:2]], [(100, 100), (101, 102)], "the steps go on")
        self.assertEqual(len(self.events("stop_numbering_unread")), 1, "said once in a while, not at every look")
        self.assertIsNone(loop.ahead)
        loop.measure_failed_at -= sf.NUMBERING_RETRY_S
        before = len(self.node.calls)
        self.run_to(loop, 103)
        self.assertEqual(loop.ahead, 1, f"measured at the first look {sf.NUMBERING_RETRY_S:g} seconds on")
        self.assertEqual([m for m, _ in self.node.calls[before:]], MEASURE + ["eth_call", "eth_getBlockByNumber", "eth_getLogs"],
                         "the first look by the measure held against the node's record of its block (L1)")

    def test_a_node_that_cannot_be_measured_is_measured_once_in_a_while_and_not_at_every_look(self):
        """A node whose servers stand at different heights answers a block it does not have yet by its number with
        -32603 "invalid block height" (testnet, 25 November 2026): the measure fails. Tried again at every look it cost
        up to 24 requests a look, and the loop went from about 191 requests a minute to 577, then 1 141."""
        loop = self.loop(wallet=None)
        loop.ahead = None
        real = self.node.call

        def call(method, params=()):
            if method == "eth_getBlockByNumber" and params[0] != "latest":
                self.node.calls.append((method, params))
                raise RuntimeError("eth_getBlockByNumber: {'code': -32603, 'message': 'invalid block height'}")
            return real(method, params)

        with mock.patch.object(self.node, "call", side_effect=call):
            self.run_to(loop, *range(100, 120))
            measured = lambda: sum(1 for m, _ in self.node.calls if m == "eth_blockNumber") // 2  # noqa: E731
            self.assertEqual(measured(), 1, "one measure at the first look, none at the next nineteen")
            loop.measure_failed_at -= sf.NUMBERING_RETRY_S
            self.run_to(loop, 120)
            self.assertEqual(measured(), 2, f"again {sf.NUMBERING_RETRY_S:g} seconds on")
        self.assertIsNone(loop.ahead)
        journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual(sorted(journal.px[BTC]), list(range(100, 121)), "every look the old way meanwhile, a line a block")

    def test_a_busy_node_at_the_measure_costs_the_look_and_is_not_asked_the_old_way(self):
        loop = self.loop(wallet=None)
        loop.ahead = None
        self.node.busy = {"eth_call"}
        with self.assertRaises(c.NodeBusy):
            loop._head()
        self.assertEqual([m for m, _ in self.node.calls], ["eth_blockNumber", "eth_call"])

    def test_it_is_measured_again_and_a_change_is_an_alarm(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.assertEqual(self.node.calls[0][0], "eth_call", "measured a moment ago: not again")
        loop.ahead_at -= sf.NUMBERING_EVERY_S
        self.node.ahead = 1
        before = len(self.node.calls)
        self.run_to(loop, 101)
        self.assertEqual([m for m, _ in self.node.calls[before:]], MEASURE + ["eth_call", "eth_getLogs"],
                         "ten minutes on, measured again before the look, which then counts by the new measure")
        changed = self.events("stop_numbering_changed")
        self.assertEqual([(a["was"], a["now"], a["alarm"]) for a in changed], [(0, 1, "ALARM")])
        self.assertEqual((loop.ahead, loop.block), (1, 101))


class AMinuteOfRequests(Base):
    """What a node paid by the request is paid for: the loop's own `run` over a minute of blocks, a block a second."""

    def minute(self, loop, blocks=60, looks_a_block=2) -> dict:
        stop = threading.Event()
        looks = {"n": 0}

        def served(until, s):
            looks["n"] += 1
            if looks["n"] >= blocks * looks_a_block:
                s.set()
            elif looks["n"] % looks_a_block == 0:
                self.node.head += 1

        loop._served = served
        self.node.counts = {}
        loop.run(1.0 / looks_a_block, stop)
        return self.node.taken()

    def test_two_looks_a_second_with_a_stop_standing(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.head = 101
        got = self.minute(loop)
        if self.MULTICALL:
            self.assertEqual(got, {"eth_call": 120, "eth_getLogs": 60}, "a call a look, the logs a block: 180 a minute")
        else:
            self.assertEqual(got, {"eth_getBlockByNumber": 120, "eth_call": 180, "eth_getLogs": 60},
                             "the head a look, a call a price a block, the logs a block: 360 a minute")
        self.assertEqual(loop.block, 160)
        self.assertEqual(sorted(pr.load_prices(pr.journal_text(self.dir / "prices.jsonl")).px[BTC]), list(range(100, 161)))

    def test_one_look_a_second(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.head = 101
        got = self.minute(loop, looks_a_block=1)
        self.assertEqual(sum(got.values()), 120 if self.MULTICALL else 300)
        self.assertEqual(self.events("stop_loop_started")[0]["looks_a_minute"], 60)

    def test_the_line_that_says_it_is_alive_says_the_requests_by_method_and_no_address(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.head = 101
        with mock.patch.object(sf, "ALIVE_EVERY_S", 0.0):
            self.minute(loop, blocks=2)
        alive = self.events("stop_loop_alive")
        self.assertEqual(len(alive), 4, "one a look here; one a minute on a host")
        total: dict = {}
        for line in alive:
            for method, n in line["requests"].items():
                total[method] = total.get(method, 0) + n
        self.assertEqual(total, {"eth_call": 4, "eth_getLogs": 2} if self.MULTICALL else
                         {"eth_getBlockByNumber": 4, "eth_call": 6, "eth_getLogs": 2})
        self.assertNotIn("127.0.0.1", json.dumps(alive))

    def test_the_nodes_own_count_is_by_method_and_starts_again_once_taken(self):
        node = sf.Node("http://127.0.0.1:9/v2/SECRETKEY")
        answer = mock.Mock(status_code=200)
        answer.json.return_value = {"result": "0x1"}
        with mock.patch.object(node.session, "post", return_value=answer):
            node.call("eth_blockNumber")
            node.call("eth_call", [{}, "latest"])
            node.call("eth_call", [{}, "latest"])
        with mock.patch.object(node.session, "post", side_effect=sf.requests.ConnectTimeout("x")):
            with self.assertRaises(sf.Unreached):
                node.call("eth_getLogs", [{}])
        self.assertEqual(node.taken(), {"eth_blockNumber": 1, "eth_call": 2, "eth_getLogs": 1}, "one that failed is paid for too")
        self.assertEqual(node.taken(), {})


class LinesWithNoPrice(Base):
    """A journal of lines with no price checks nothing, and said nothing: `stop_watch_blind` is raised only for an
    asset with a stop standing. More than a tenth of the last ten minutes' blocks without a price is an alarm, a
    stop standing or not, said again at most once an hour."""

    def test_more_than_a_tenth_of_ten_minutes_with_no_price_is_an_alarm_with_no_stop_standing(self):
        loop = self.loop(wallet=None)
        self.node.unread_assets = {0, 3, 4}
        self.run_to(loop, *range(100, 100 + sf.UNPRICED_MIN - 1))
        self.assertEqual(self.events("stop_prices_unread"), [], "too few blocks to judge by")
        self.run_to(loop, 100 + sf.UNPRICED_MIN - 1)
        said = self.events("stop_prices_unread")
        self.assertEqual([(a["blocks"], a["without"], a["alarm"]) for a in said], [(sf.UNPRICED_MIN, sf.UNPRICED_MIN, "ALARM")])
        self.assertEqual(loop.triggers, {}, "no stop stands")
        self.run_to(loop, *range(130, 200))
        self.assertEqual(len(self.events("stop_prices_unread")), 1, "once while it lasts, inside the hour")
        self.later = sf.UNPRICED_REALARM_S
        self.run_to(loop, *range(200, 240))
        self.assertEqual(len(self.events("stop_prices_unread")), 2, "an hour later, again")

    def test_a_tenth_or_less_is_no_alarm_and_old_lines_are_forgotten(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, *range(100, 190))
        self.node.unread_assets = {0, 3, 4}
        self.run_to(loop, *range(190, 200))
        self.assertEqual(self.events("stop_prices_unread"), [], "ten of a hundred: a tenth, not more")
        self.run_to(loop, 200)
        self.assertEqual(self.events("stop_prices_unread")[0]["without"], 11)
        # Ten minutes on, the lines of before are out of the count, the good ones too: twenty with a price and ten
        # without are a third of the last ten minutes, whatever the two hundred before them were.
        self.node = Node()
        self.said.clear()
        loop = self.loop(wallet=None)
        self.run_to(loop, *range(100, 300))
        self.later = sf.UNPRICED_WINDOW_S + 300
        self.run_to(loop, *range(300, 320))
        self.node.unread_assets = {0, 3, 4}
        self.run_to(loop, *range(320, 330))
        self.assertEqual([(a["blocks"], a["without"]) for a in self.events("stop_prices_unread")], [(30, 10)])

    def test_blocks_the_read_back_skipped_are_blocks_with_no_price_and_the_ones_it_was_not_running_in_are_not(self):
        loop = self.loop(wallet=None, backfill=3)
        self.run_to(loop, *range(100, 130))
        self.run_to(loop, 170)  # the head ran ahead: blocks 130 to 167 are a gap, the last three are read back
        said = self.events("stop_prices_unread")
        self.assertEqual([(a["blocks"], a["without"]) for a in said], [(68, 38)], "said as the gap is written")
        # Started again after 200 blocks: the gap of the blocks it was not running in is no line of this count.
        self.said.clear()
        loop = self.loop(wallet=None, backfill=3)
        self.run_to(loop, *range(370, 410))
        self.assertEqual(self.events("stop_prices_unread"), [])

    def test_one_asset_read_is_a_line_with_a_price(self):
        loop = self.loop(wallet=None)
        self.node.unread_assets = {0, 4}
        self.run_to(loop, *range(100, 160))
        self.assertEqual(self.events("stop_prices_unread"), [])


class HeldForItsBlock(Base):
    """The keeper's batch reads an exam at the node's head, which may be a block this loop has not looked at yet."""

    def test_a_read_of_a_block_the_loop_has_not_reached_waits_for_that_blocks_events(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["touch"], 101)
        # Block 102 fills the stop; the keeper's batch ran at 102 and found no stop, before this loop read 102.
        self.node.logs += fill_of_stop(102)
        self.node.positions[(EXAM, BTC)] = 0
        loop.inbox.put(("state", EXAM, 102, {}, {}))
        self.run_to(loop, 102)
        self.assertEqual(self.events("stop_missed"), [], "applied before 102's events it would be a stop gone while reached")
        self.assertEqual(self.events("stop_caught")[0]["filled"], 102)
        self.assertEqual(loop.held, [])

    def test_a_stop_set_in_the_block_of_the_read_is_not_dropped_by_it(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.logs.append(set_log(102, 950_000, 103))
        loop.inbox.put(("state", EXAM, 101, {}, {}))  # read at 101: no stop yet
        self.run_to(loop, 102)
        self.assertIn((EXAM, BTC, False), loop.triggers, "the event of 102 is newer than the read of 101")
        loop.inbox.put(("state", EXAM, 104, {(EXAM, BTC, False): (940_000, 104)}, {BTC: 1}))
        self.run_to(loop, 103)
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["px"], 950_000, "held: block 104 is not looked at yet")
        self.run_to(loop, 104)
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["px"], 940_000, "taken once 104's events are in")

    def test_before_the_first_step_a_read_is_taken_at_once(self):
        loop = self.loop(wallet=None)
        loop.inbox.put(("state", EXAM, 100, {(EXAM, BTC, False): (950_000, 90)}, {BTC: 1}))
        self.node.price[100] = 900_000
        self.run_to(loop, 100)
        self.assertEqual(loop.triggers[(EXAM, BTC, False)]["touch"], 100, "known from the first block looked at")

    def test_at_the_first_step_a_read_of_a_later_block_waits_for_it_too(self):
        """Two reads wait before the loop's first step, of block 100: the exam's stop as it stood at 100, and the exam
        ended at 101. The second is of a block the step does not read: taken at once, it would drop the stop a block
        before the chain did."""
        loop = self.loop(wallet=None)
        loop.inbox.put(("state", EXAM, 100, {(EXAM, BTC, False): (950_000, 90)}, {BTC: 1}))
        loop.inbox.put(("ended", EXAM, 101))
        self.run_to(loop, 100)
        self.assertIn((EXAM, BTC, False), loop.triggers)
        self.assertNotIn(EXAM, loop.ended)
        self.run_to(loop, 101)
        self.assertIn(EXAM, loop.ended, "taken once block 101's events are in")


class TheKeepersBatch(Base):
    """The slow pass reads an exam's status, stops, takes and positions in one call with its other reads (sk.Reads):
    handed to the loop as it is, at the block that call ran in."""
    ONE_CALL_ONLY = True

    def batch(self, exams, ahead=0):
        reads = sk.Reads()
        groups = [(e, [(e, sk.c.encode_call("result()"))] + [(e, sk.c.encode_call(sig, ["uint32"], [BTC]))
                       for sig in ("stopOf(uint32)", "takeOf(uint32)", "positionOf(uint32)")], []) for e in exams]
        reads.load(groups, self.node.call, ahead)
        reads.assets = {e: [BTC] for e in exams}
        return reads

    def test_the_stops_of_a_batch_are_taken_at_the_block_its_call_ran_in(self):
        for ahead in (0, 1):
            self.node = Node()
            self.node.ahead = ahead
            self.said.clear()
            loop = self.loop()
            self.run_to(loop, 100)
            self.node.stops[(EXAM, BTC, False)] = (950_000, 95)
            self.node.head = 101  # the node's head when the keeper's batch runs: a block ahead of the loop
            before = len(self.node.calls)
            loop.reread(EXAM, batch=self.batch([EXAM], ahead))
            self.assertEqual([m for m, _ in self.node.calls[before:]], ["eth_call"], "one call, and none by the reread itself")
            self.assertEqual(loop.inbox.queue[0][:3], ("state", EXAM, 101), "the block the call ran in, by the node's numbering")
            self.node.price[101] = 900_000
            self.assertEqual(self.run_to(loop, 101), [[(EXAM, BTC, False)]], "known and reached at 101: sent in that step")
            self.assertEqual(self.events("stop_found_by_read")[0]["at"], 101)

    def test_an_exam_that_ended_is_told_by_the_batch(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        loop.apply_reread(EXAM, 100, {(EXAM, BTC, False): (950_000, 90)}, {BTC: 1})
        self.node.status = 3
        loop.reread(EXAM, batch=self.batch([EXAM]))
        self.run_to(loop, 101)
        self.assertIn(EXAM, loop.ended)

    def test_a_read_that_failed_in_the_batch_is_said_and_nothing_is_handed_over(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        batch = self.batch([EXAM])
        del batch.got[(EXAM.lower(), sk.c.encode_call("stopOf(uint32)", ["uint32"], [BTC]))]
        with self.assertRaises(RuntimeError) as caught:
            loop.reread(EXAM, batch=batch)
        self.assertIn("stopOf", str(caught.exception))
        self.assertTrue(loop.inbox.empty())

class TheWatchAlonesBatch(Base):
    def test_the_watch_alone_reads_all_its_exams_in_one_call(self):
        exams = [to_checksum_address("0x" + f"{i:040x}") for i in range(0x7001, 0x7001 + 9)]
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        keeper = sk.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, True, self.dir / "watch.json", 1)
        keeper.multicall = self.MULTICALL
        keeper.ahead, keeper.ahead_at = 0, time.monotonic()
        for e in exams:
            self.node.stops[(e, BTC, False)] = (950_000, 95)
        with mock.patch.object(c, "rpc", side_effect=self.node.call):
            for turn in (1, 2):
                before = len(self.node.calls)
                loop.reread_all(exams, keeper)
                asked = [m for m, _ in self.node.calls[before:]]
                if not self.MULTICALL:
                    self.assertEqual(len(asked), 9 * 5, "a call a read, as before: status, rules, stop, take, position")
                elif turn == 1:
                    # The rules of each, then the stops of each, then the first call's block held against the node's
                    # record of it (review 20 of #342).
                    self.assertEqual(asked, ["eth_call", "eth_call", "eth_getBlockByNumber"])
                else:
                    self.assertEqual(asked, ["eth_call", "eth_getBlockByNumber"], "the rules are written once")
        self.run_to(loop, 101)
        self.assertEqual(sorted(k[0] for k in loop.triggers), sorted(exams))

    def test_an_exam_with_a_read_missing_in_the_batch_is_read_again_alone(self):
        exams = [to_checksum_address("0x" + f"{i:040x}") for i in range(0x7101, 0x7104)]
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        keeper = sk.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, True, self.dir / "watch.json", 1)
        keeper.multicall = self.MULTICALL
        keeper.ahead, keeper.ahead_at = 0, time.monotonic()
        keeper.rule_assets = {e: [BTC] for e in exams}
        for e in exams:
            self.node.stops[(e, BTC, False)] = (950_000, 95)
        self.node.batch_unread = {(exams[1], SEL["stopOf(uint32)"])}
        said = []
        with mock.patch.object(c, "rpc", side_effect=self.node.call), \
                mock.patch.object(sf, "log", side_effect=lambda e, **f: said.append(e)):
            loop.reread_all(exams, keeper)
        alone = [to for to, data, _ in self.node.reads if data == SEL["rules()"]]
        self.assertEqual(alone, [exams[1]] if self.MULTICALL else exams, "only the one the batch did not read whole")
        self.assertNotIn("stops_unread", said)
        self.run_to(loop, 101)
        self.assertEqual(sorted(k[0] for k in loop.triggers), sorted(exams), "its stop is known all the same")

    def test_the_watchs_own_pass_asks_its_own_node_for_multicall3(self):
        """The loop reads through its node (--stop-rpc), the pass through the plumbing's: each is asked for itself."""
        class Out(Exception):
            pass
        made = mock.Mock(multicall=True)
        seen = {}

        def a_pass(keeper, loop):
            seen["multicall"] = keeper.multicall

        with mock.patch.object(sk.deployments, "load", return_value={"SimPoolFactory": "0x" + "33" * 20, "block": 1}), \
                mock.patch.object(sk.sn, "confirm_service"), mock.patch.object(sf, "build", return_value=made), \
                mock.patch.object(sf, "start"), mock.patch.object(sf, "keep_alive"), \
                mock.patch.object(sf, "watch_pass", side_effect=a_pass), \
                mock.patch.object(sk, "multicall_or_not", return_value=False) as asked, \
                mock.patch.object(c, "rpc", return_value="0x1"), mock.patch.object(sf.time, "sleep", side_effect=Out):
            with self.assertRaises(Out):
                sf.main(["--deployment", "sim1", "--state", str(self.dir / "watch-sim1.json")])
            asked.assert_called_once()
            self.assertIs(asked.call_args.args[0], c.rpc)
        self.assertEqual(seen, {"multicall": False}, "the pass's node has none, whatever the loop's has")


class OneReadOfTheLogs(Base):
    """With the keeper's fast loop fed from here, the orders placed are read in the step's own eth_getLogs and handed
    on with the step's blocks; the watch alone asks for no orders."""

    def placed(self, block, exam=EXAM):
        return ev(sk.PLACED, exam, block, encode(["bool", "uint64", "uint256", "uint256"], [True, 100, block + 1, block + 5]),
                  word(BTC))

    def test_shared_the_orders_are_read_with_the_stops_and_handed_on_with_the_blocks(self):
        loop = self.loop(wallet=None)
        feed = loop.share()
        self.node.logs += [set_log(100, 950_000, 101), self.placed(100)]
        self.run_to(loop, 100)
        asked = [p[0] for m, p in self.node.calls if m == "eth_getLogs"]
        self.assertEqual(len(asked), 1, "one read of the block's logs for both loops")
        self.assertIn(sk.PLACED, asked[0]["topics"][0])
        lo, hi, logs = feed.get_nowait()
        self.assertEqual((lo, hi, [l["topics"][0] for l in logs]), (100, 100, [sk.PLACED]), "the stops' own events stay here")
        self.assertIn((EXAM, BTC, False), loop.triggers)
        self.run_to(loop, 103)
        self.assertEqual(feed.get_nowait()[:2], (101, 103), "a step over three blocks is handed on as it is")

    def test_an_account_sold_in_the_same_blocks_has_its_orders_handed_on_too(self):
        new = to_checksum_address("0x" + "69" * 20)
        loop = self.loop(wallet=None)
        feed = loop.share()
        self.run_to(loop, 99)
        feed.get_nowait()
        sold = ev(sk.EXAM_SOLD, POOL, 100, b"", word(int(new, 16)))
        self.node.logs += [sold, self.placed(100, exam=new)]
        self.run_to(loop, 100)
        _, _, logs = feed.get_nowait()
        self.assertEqual(sorted((l["topics"][0], l["address"]) for l in logs), sorted([(sk.EXAM_SOLD, POOL), (sk.PLACED, new)]))

    def test_with_nothing_watched_the_head_is_handed_on_all_the_same(self):
        self.keeper.accounts, self.keeper.pools = {}, []
        loop = self.loop(wallet=None)
        feed = loop.share()
        self.run_to(loop, 100, 101)
        self.assertEqual([feed.get_nowait(), feed.get_nowait()], [(100, 100, []), (101, 101, [])])

    def test_not_shared_no_order_is_asked_for(self):
        loop = self.loop(wallet=None)
        self.node.logs.append(self.placed(100))
        self.run_to(loop, 100)
        asked = [p[0] for m, p in self.node.calls if m == "eth_getLogs"]
        self.assertNotIn(sk.PLACED, asked[0]["topics"][0])
        self.assertIsNone(loop.feed)


class TheStart(Base):
    """What `build` looks at once at the start, and what the first line of the loop says."""
    ONE_CALL_ONLY = True

    def args(self, **kw):
        base = dict(stop_rpc="", price_journal=None, stop_alarm=None, deployment="sim1", stop_tip_gwei=0, stop_overdue=5,
                    stop_alarm_min_blocks=2, stop_poll=None, review_status="")
        return mock.Mock(**{**base, **kw})

    def test_with_multicall3_the_loop_looks_in_one_call_and_without_it_is_as_it_was_and_says_so_once(self):
        lines = []
        for there in (True, False):
            self.node = Node()
            self.node.multicall = there
            with mock.patch.object(sf, "Node", return_value=self.node), \
                    mock.patch.object(sk, "log", side_effect=lambda e, **f: lines.append((e, f))):
                loop = sf.build(self.args(), Keeper(), self.dir / f"k{there}" / "multi-sim1.json")
            loop.journal.close()
            self.assertEqual(loop.multicall, there)
            self.assertEqual([m for m, _ in self.node.calls], ["eth_chainId", "eth_getCode"])
        self.assertEqual([(e, f["who"]) for e, f in lines], [("no_multicall", "the stops' loop")], "one line, without it only")

    def test_on_mainnet_a_node_with_no_multicall3_does_not_start(self):
        from spike.hlspike import network as networks
        self.node.multicall = False
        with mock.patch.object(sf, "Node", return_value=self.node), mock.patch.object(sf, "check_chain"), \
                mock.patch.object(sf.core, "check_node"), mock.patch.object(c, "NETWORK", networks.MAINNET), \
                mock.patch.dict("os.environ", {"COLOSSEUM_ALERT_CMD": "cat"}):
            with self.assertRaises(SystemExit) as caught:
                sf.build(self.args(), Keeper(), self.dir / "multi-sim1.json")
        self.assertIn("no Multicall3", str(caught.exception))

    def test_the_looks_a_second_are_a_setting_and_the_first_line_says_them(self):
        import argparse
        import os

        def parse(argv, env):
            p = argparse.ArgumentParser()
            with mock.patch.dict("os.environ", env):
                if "COLOSSEUM_STOP_POLL" not in env:
                    os.environ.pop("COLOSSEUM_STOP_POLL", None)
                sf.add_args(p)
                return p.parse_args(argv)

        self.assertIsNone(parse([], {}).stop_poll, "not set: the keeper's --poll, twice a second")
        self.assertIsNone(parse([], {"COLOSSEUM_STOP_POLL": " "}).stop_poll, "empty is not set")
        self.assertEqual(parse([], {"COLOSSEUM_STOP_POLL": "1"}).stop_poll, 1.0)
        zero = parse([], {"COLOSSEUM_STOP_POLL": "0"})
        self.assertEqual(zero.stop_poll, 0.0, "a zero in the variable is a zero, refused as the flag's is")
        with mock.patch.object(sf, "Node", return_value=self.node):
            with self.assertRaises(SystemExit):
                sf.build(self.args(stop_poll=zero.stop_poll), Keeper(), self.dir / "multi-sim1.json")
        self.assertEqual(parse(["--stop-poll", "0.25"], {"COLOSSEUM_STOP_POLL": "1"}).stop_poll, 0.25, "the flag over the variable")
        with mock.patch.object(sf, "Node", return_value=self.node):
            with self.assertRaises(SystemExit):
                sf.build(self.args(stop_poll=0.0), Keeper(), self.dir / "multi-sim1.json")
        loop = self.loop(wallet=None)
        stop = threading.Event()
        loop._served = lambda until, s: stop.set()
        # The keeper's own node, named by its scheme and host; a node of the stops' own is not named (review 19 of #340,
        # L1: test_mainnet_host.py), so the line is read here with the loop's node as the keeper's, whatever the
        # environment of the run names.
        with mock.patch.object(c, "RPC_URL", self.node.url):
            loop.run(0.5, stop)
        started = self.events("stop_loop_started")[0]
        self.assertEqual((started["poll"], started["looks_a_minute"], started["one_call"]), (0.5, 120, True))
        self.assertEqual(started["node"], "http://127.0.0.1:9")

    def test_the_widest_read_of_logs_is_a_setting_and_not_needed(self):
        import argparse
        import os

        def parse(argv, env):
            p = argparse.ArgumentParser()
            with mock.patch.dict("os.environ", env):
                if "COLOSSEUM_STOP_LOG_WINDOW" not in env:
                    os.environ.pop("COLOSSEUM_STOP_LOG_WINDOW", None)
                sf.add_args(p)
                return p.parse_args(argv)

        self.assertEqual(parse([], {}).stop_log_window, sf.LOG_WINDOW)
        self.assertEqual(parse([], {"COLOSSEUM_STOP_LOG_WINDOW": "10"}).stop_log_window, 10)
        self.assertEqual(parse(["--stop-log-window", "1000"], {"COLOSSEUM_STOP_LOG_WINDOW": "10"}).stop_log_window, 1000)
        with mock.patch.object(sf, "Node", return_value=self.node):
            loop = sf.build(self.args(stop_log_window=10), Keeper(), self.dir / "a" / "multi-sim1.json")
            loop.journal.close()
            self.assertEqual((loop.logs.full, loop.logs.width), (10, 10))
            loop = sf.build(self.args(), Keeper(), self.dir / "b" / "multi-sim1.json")
            loop.journal.close()
            self.assertEqual(loop.logs.width, sf.LOG_WINDOW, "not a setting of this caller's: the default")
            with self.assertRaises(SystemExit):
                sf.build(self.args(stop_log_window=0), Keeper(), self.dir / "c" / "multi-sim1.json")


class LimitedNode(Node):
    """A node of narrower ways than HyperEVM's own, as Alchemy's free plan was on testnet on 25 November 2026: its
    eth_getLogs takes at most `limit` blocks and refuses a wider range with HTTP 400, naming the width it takes or not
    (`named`), as the loop's node says such an answer (`sf.Node._said`); and its logs may stand `lag` blocks behind the
    head its calls answer at, a range past them refused with -32602."""

    def __init__(self, limit: int = 10, lag: int = 0, named: bool = True):
        super().__init__()
        self.limit, self.lag, self.named = limit, lag, named
        self.log_reads: list[tuple[int, int, str]] = []  # each eth_getLogs: its first and last block, and its answer

    def call(self, method, params=()):
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            if hi > self.head - self.lag:
                self.refused(method, params, lo, hi, "past its head",
                             "eth_getLogs: {'code': -32602, 'message': 'block range extends beyond current head block'}")
            if hi - lo + 1 > self.limit:
                answer = mock.Mock(status_code=400)
                answer.json.return_value = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": (
                    f"Under the Free tier plan, you can make eth_getLogs requests with up to a {self.limit} block range. "
                    f"Based on your parameters, this block range should work: [{hex(lo)}, {hex(lo + self.limit - 1)}]"
                    if self.named else "block range too large")}}
                self.refused(method, params, lo, hi, "too wide", f"eth_getLogs: HTTP 400{sf.Node._said(answer)}")
            self.log_reads.append((lo, hi, "read"))
        return super().call(method, params)

    def refused(self, method, params, lo: int, hi: int, why: str, error: str):
        self.calls.append((method, params))
        self.counts[method] = self.counts.get(method, 0) + 1
        self.log_reads.append((lo, hi, why))
        raise RuntimeError(error)

    def read_spans(self) -> list[tuple[int, int]]:
        return [(lo, hi) for lo, hi, how in self.log_reads if how == "read"]

    def blocks_read(self) -> list[int]:
        """Every block of every eth_getLogs it answered, as many times as it answered for it."""
        return sorted(b for lo, hi in self.read_spans() for b in range(lo, hi + 1))


class TheNodesLimits(Base):
    """A node narrower than HyperEVM's own: eth_getLogs of ten blocks at most, its logs a block or two behind the head
    its calls answer at (Alchemy's free plan, testnet, 25 November 2026). The loop, more than ten blocks behind, asked
    for fifty, was refused, and stood on that range for good (14:05:10, and 14:28:31 after a restart), the keeper's
    fast loop with it. It reads in the width the node takes, as far as the node's logs reach, and gives no block up."""

    def fresh(self, node: Node) -> None:
        self.node = node
        self.said.clear()
        for f in self.dir.glob("prices*"):
            f.unlink()
        self.journal = None

    def test_a_node_of_any_width_is_read_in_the_width_it_takes_and_the_width_found_is_kept(self):
        for limit, named in ((10, True), (10, False), (3, False), (50, True), (1_000, True)):
            with self.subTest(limit=limit, named=named):
                self.fresh(LimitedNode(limit, named=named))
                loop = self.loop(wallet=None)
                self.run_to(loop, 100)
                self.node.logs.append(set_log(160, 950_000, 161))
                self.run_to(loop, 220)  # 120 blocks behind
                self.assertEqual(loop.block, 220)
                self.assertIn((EXAM, BTC, False), loop.triggers, "the stop set in block 160 is read")
                self.assertEqual(self.node.blocks_read(), list(range(100, 221)), "every block's events read, each once")
                self.assertTrue(all(hi - lo < limit for lo, hi in self.node.read_spans()))
                width = loop.logs.width
                if limit >= sf.LOG_WINDOW:
                    self.assertEqual((width, self.events("logs_window_narrowed")), (sf.LOG_WINDOW, []), "never refused")
                else:
                    self.assertEqual(width, limit if named else sf.LOG_WINDOW >> len(self.events("logs_window_narrowed")),
                                     "the width the refusal names, or half again till it is taken")
                    self.assertLessEqual(width, limit)
                    self.assertEqual(self.events("logs_window_narrowed")[-1]["now"], width)
                refused = sum(1 for *_, how in self.node.log_reads if how == "too wide")
                self.node.logs.append(set_log(280, 960_000, 281, take=True))
                self.run_to(loop, 300)
                self.assertEqual(sum(1 for *_, how in self.node.log_reads if how == "too wide"), refused,
                                 "the width found is kept: refused no more")
                self.assertIn((EXAM, BTC, True), loop.triggers)
                self.assertEqual(self.events("stop_watch_lost"), [])

    def test_a_node_whose_logs_stand_behind_its_head_is_read_as_far_as_they_reach_and_the_rest_next_time(self):
        self.node = LimitedNode(limit=50, lag=1)
        loop = self.loop(wallet=None)
        feed = loop.share()
        self.node.price.update({100: 990_000, 101: 980_000, 102: 970_000})
        self.run_to(loop, 100)  # its logs end at 99: nothing is read, and the loop starts at block 100 all the same
        self.assertEqual(loop.block, 99)
        self.node.logs.append(set_log(101, 950_000, 102))
        self.run_to(loop, 101)  # its logs end at 100
        self.assertEqual(loop.block, 100)
        self.assertNotIn((EXAM, BTC, False), loop.triggers, "block 101's events are not there yet")
        self.node.lag = 0
        self.run_to(loop, 102)
        self.assertEqual(loop.block, 102)
        self.assertIn((EXAM, BTC, False), loop.triggers, "the stop set in block 101 is read at the next step")
        self.assertEqual([feed.get_nowait()[:2] for _ in range(2)], [(100, 100), (101, 102)],
                         "handed on as read: no block twice, none left out")
        self.assertTrue(feed.empty())
        journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual((journal.px[BTC], journal.gaps), ({100: 990_000, 101: 980_000, 102: 970_000}, []))
        if self.MULTICALL:
            self.assertEqual(self.node.price_reads(), [100, 101, 102], "each block's price read once, by its own look")
        self.assertEqual(loop.logs.taken_short(), 2)
        self.assertEqual(loop.logs.width, 50, "a node behind is no node of a narrower width")

    def test_a_loop_far_behind_on_such_a_node_catches_up_with_no_failure_and_hands_every_block_on(self):
        """As on testnet on 25 November 2026: 600 blocks behind, on a node of ten blocks whose logs stand a block or
        two behind at times. Through the loop's own `run`: no look fails, no step is a failure, and every block's
        orders are handed on to the keeper's fast loop, in order."""
        self.node = LimitedNode(limit=10, lag=0)
        loop = self.loop(wallet=None)
        feed = loop.share()
        self.run_to(loop, 100)
        placed = ev(sk.PLACED, EXAM, 450, encode(["bool", "uint64", "uint256", "uint256"], [True, 100, 451, 455]), word(BTC))
        self.node.logs += [placed, set_log(650, 950_000, 651)]
        self.node.head, self.node.lag = 700, 1
        lags = iter([0, 2, 1, 0, 1, 2, 0, 0, 1, 0] * 3)
        looks = {"n": 0}

        def served(until, stop):
            looks["n"] += 1
            self.node.head += 1
            self.node.lag = next(lags)
            if looks["n"] >= 20:
                stop.set()

        loop._served = served
        with mock.patch.object(sf, "ALIVE_EVERY_S", 0.0):
            loop.run(0.5, threading.Event())
        self.assertGreaterEqual(loop.block, self.node.head - 3)
        self.assertEqual(self.node.blocks_read(), list(range(100, loop.block + 1)), "every block's events, each once")
        steps = []
        while not feed.empty():
            steps.append(feed.get_nowait())
        self.assertEqual(steps[0][:2], (100, 100))
        self.assertTrue(all(b[0] == a[1] + 1 for a, b in zip(steps, steps[1:])), "each step from the block after the last")
        self.assertEqual(steps[-1][1], loop.block)
        self.assertEqual([int(l["blockNumber"], 16) for _, _, logs in steps for l in logs], [450], "the order handed on")
        self.assertIn((EXAM, BTC, False), loop.triggers)
        self.assertEqual([e for e, _ in self.said if e in ("stop_loop_failed", "stop_node_busy", "stop_watch_lost")
                          or e.startswith("stop_loop_failing")], [])
        alive = self.events("stop_loop_alive")
        self.assertEqual({a["log_window"] for a in alive}, {10})
        self.assertGreater(sum(a["logs_short"] for a in alive), 0, "the steps the node's logs ended short in, said")

    def test_a_fill_landed_in_a_block_whose_logs_are_not_there_yet_is_not_sent_again(self):
        self.node = LimitedNode(limit=50)
        loop = self.loop()
        self.armed_stop(loop)
        self.node.price[101] = 900_000
        self.run_to(loop, 101)
        self.assertEqual(len(self.node.raw), 1, "reached at 101: sent")
        tx = loop.pending[(EXAM, BTC, False)][0]
        # It lands in block 102, whose events the node's logs do not have yet.
        self.node.receipts[tx] = {"status": "0x1", "blockNumber": hex(102)}
        self.node.logs += fill_of_stop(102)
        self.node.lag = 1
        self.run_to(loop, 102)
        self.assertEqual((loop.block, len(self.node.raw)), (101, 1), "not sent a second time while its fill is not read")
        self.assertEqual(self.events("trigger_filled")[0]["block"], 102)
        self.node.lag = 0
        self.run_to(loop, 103)
        self.assertNotIn((EXAM, BTC, False), loop.triggers, "its TriggerFilled read")
        self.assertEqual((len(self.node.raw), loop.landed), (1, {}))

    def test_prices_a_look_read_of_blocks_behind_the_read_back_are_their_lines_not_a_gap(self):
        """The node's logs stood further behind than the read-back of prices: the blocks the step then reaches are behind
        it too, but each was looked at as the head, and its look's prices are its line (review of #349)."""
        if not self.MULTICALL:
            return  # the old way's looks read no prices
        self.node = LimitedNode(limit=50)
        loop = self.loop(wallet=None, backfill=3)
        self.run_to(loop, 100)
        self.node.lag = 5
        for h in range(101, 111):
            self.node.price[h] = 900_000 + h
            self.run_to(loop, h)
        self.node.lag = 0
        self.run_to(loop, 111)
        journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual(sorted(journal.px[BTC]), list(range(100, 112)), "a line for every block looked at")
        self.assertEqual({b: px for b, px in journal.px[BTC].items() if b > 100}, {b: self.node.oracle(b) for b in range(101, 112)})
        self.assertEqual(journal.gaps, [])
        self.assertEqual(self.events("stop_watch_blind"), [])

    def test_prices_kept_for_a_block_ahead_are_dropped_when_a_look_does_not_hold(self):
        """A look that does not hold says the numbering may have moved: the prices kept for blocks the steps have not
        reached may be another block's, and are read again by number (review of #349)."""
        if not self.MULTICALL:
            return
        self.node = LimitedNode(limit=50, lag=2)
        loop = self.loop(wallet=None)
        self.node.lag = 0
        self.run_to(loop, 100)
        self.node.lag = 2
        self.run_to(loop, 101, 102)
        self.assertEqual(sorted(loop.unstepped), [101, 102])
        loop.head_t[103] = self.node.time_of(104)  # kept by a look whose step failed before it kept its prices
        loop._checked(sf.Look(103, self.node.time_of(102), {}, frozenset()))  # the time of another block
        self.assertEqual(loop.unstepped, {})
        self.assertFalse({101, 102} & set(loop.asked))
        self.assertFalse({101, 102, 103} & set(loop.head_t), "nor their time, which a line read again would keep")
        self.assertEqual(loop.head_t[100], self.node.time_of(100), "a block stepped keeps its line's time")

    def test_a_node_failing_its_logs_is_asked_once_a_step_not_in_ever_narrower_windows(self):
        """A server's -32603, an HTTP 5xx, a timeout: each narrowed down to one block, 606 eth_getLogs a minute against
        120 before, and a step of timeouts waited half a minute (review of #349, M1). Raised as it comes, as before."""
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        real = self.node.call
        for error in (RuntimeError("eth_getLogs: {'code': -32603, 'message': 'internal error'}"),
                      sf.Unsure("eth_getLogs: HTTP 503"), sf.Unsure("eth_getLogs: ReadTimeout")):
            with self.subTest(error=str(error)):
                def call(method, params=(), error=error):
                    if method == "eth_getLogs":
                        self.node.calls.append((method, params))
                        raise error
                    return real(method, params)

                before = len(self.node.calls)
                with mock.patch.object(self.node, "call", side_effect=call), self.assertRaises(type(error)):
                    self.run_to(loop, 140)
                self.assertEqual(sum(1 for m, _ in self.node.calls[before:] if m == "eth_getLogs"), 1)
                self.assertEqual((loop.block, loop.logs.width), (100, sf.LOG_WINDOW))
        self.assertEqual(self.events("logs_window_narrowed"), [])

    def test_the_windows_read_before_a_block_the_node_fails_on_are_kept_and_the_step_goes_as_far(self):
        """Ten blocks a window, 30 behind, and the node fails on block 125: blocks 101 to 120 read, the step to 120, and
        the next one meets the error at block 121 (review of #349, M1: the blocks before it were read and dropped)."""
        self.node = LimitedNode(limit=10)
        loop = self.loop(wallet=None)
        loop.logs.width = 10
        self.run_to(loop, 100)
        real = self.node.call

        def call(method, params=()):
            if method == "eth_getLogs" and int(params[0]["fromBlock"], 16) <= 125 <= int(params[0]["toBlock"], 16):
                self.node.calls.append((method, params))
                raise RuntimeError("eth_getLogs: {'code': -32603, 'message': 'internal error'}")
            return real(method, params)

        with mock.patch.object(self.node, "call", side_effect=call):
            self.run_to(loop, 130)
            self.assertEqual(loop.block, 120)
            with self.assertRaises(RuntimeError):
                self.run_to(loop, 131)
        self.assertEqual(loop.block, 120)
        self.assertEqual(self.node.blocks_read(), list(range(100, 121)), "each block once")
        self.assertEqual([e["read_to"] for e in self.events("logs_read_stopped")], [120])
        self.run_to(loop, 132)
        self.assertEqual(loop.block, 132)
        self.assertEqual(self.node.blocks_read(), list(range(100, 133)))

    def test_after_a_step_the_nodes_logs_ended_short_in_the_next_look_is_no_skip(self):
        """The node's logs a block behind its head: each step one block short of its look. The next look, one block on
        from the last, was taken for blocks skipped and held against the node's record, and when that answer failed,
        the prices kept were dropped and the journal had gaps (review of #349, M3). Blocks past the last look are."""
        if not self.MULTICALL:
            return
        self.node = LimitedNode(limit=50)
        loop = self.loop(wallet=None)
        self.run_to(loop, 100)
        self.node.lag = 1
        before = len(self.node.calls)
        self.run_to(loop, 101, 102, 103)
        self.assertEqual(loop.block, 102)
        self.assertEqual([p for m, p in self.node.calls[before:] if m == "eth_getBlockByNumber"], [], "none doubted")
        self.node.lag = 0
        self.run_to(loop, 105)
        self.assertEqual([p for m, p in self.node.calls[before:] if m == "eth_getBlockByNumber"], [[hex(105), False]],
                         "two blocks past the last look: held")
        self.assertEqual(self.events("stop_look_unconfirmed"), [])
        journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
        self.assertEqual((sorted(journal.px[BTC]), journal.gaps), (list(range(100, 106)), []))

    def test_a_refusal_of_http_4xx_says_the_width_it_names_and_no_other_word_of_the_nodes(self):
        """The node's words went into the error, and from there to lines, the flag file and the alarm's channel: a
        provider's key echoed in a 401, or the host name of a --stop-rpc whose host is the key, with them (review of
        #349, L4). Only what a refusal of the range says of it is said, in the loop's own words."""
        node = sf.Node("http://SECRETHOST.example.invalid:9/v2/SECRETKEY")
        answer = mock.Mock(status_code=400)

        def said(status: int, message) -> str:
            answer.status_code = status
            answer.json.return_value = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": message}}
            with mock.patch.object(node.session, "post", return_value=answer), self.assertRaises(RuntimeError) as caught:
                node.call("eth_getLogs", [{}])
            return str(caught.exception)

        width = said(400, "Under the Free tier plan, you can make eth_getLogs requests with up to a 10 block range. See "
                          "http://SECRETHOST.example.invalid:9/v2/SECRETKEY/docs, SECRETHOST, SECRETKEY")
        self.assertEqual(width, "eth_getLogs: HTTP 400: a block range refused, 10 blocks at most")
        self.assertEqual((sk.said_width(width), sk.width_refusal(RuntimeError(width), 50)), (10, (10, True)))
        self.assertEqual(said(400, "block range too large SECRETKEY"), "eth_getLogs: HTTP 400: a block range refused")
        behind = said(400, "block range extends beyond current head block")
        self.assertEqual(behind, "eth_getLogs: HTTP 400: a block range past the node's head")
        self.assertTrue(sk.beyond_head(behind))
        for status, message in ((401, "Invalid API key SECRETKEY for SECRETHOST.example.invalid"),
                                (403, "SECRETKEY: not allowed"), (408, "request limit"), (400, "limit SECRETHOST")):
            self.assertEqual(said(status, message), f"eth_getLogs: HTTP {status}", "no word of the node's")
        answer.json.side_effect = ValueError("not JSON")
        with mock.patch.object(node.session, "post", return_value=answer), self.assertRaises(RuntimeError) as caught:
            node.call("eth_getLogs", [{}])
        self.assertEqual(str(caught.exception), "eth_getLogs: HTTP 400")


class TheAlarmsThatComeAgain(Base):
    """An alarm of the loop's health that its cause raises at every step is raised once in ALARM_AGAIN_S at most, the
    ones between counted. On testnet on 25 November 2026, past its catch-up with its step failing at every look, the
    loop wrote stop_watch_lost every second, 62 lines a minute."""

    def failing_logs(self):
        real = self.node.call

        def call(method, params=()):
            if method == "eth_getLogs":
                self.node.calls.append((method, params))
                raise RuntimeError("eth_getLogs: {'code': -32603, 'message': 'internal error'}")
            return real(method, params)

        return mock.patch.object(self.node, "call", side_effect=call)

    def test_a_step_past_the_catch_up_that_keeps_failing_says_it_lost_the_thread_once_in_a_while(self):
        loop = self.loop(wallet=None, backfill=3, catchup=10)
        self.run_to(loop, 100)
        with self.failing_logs():
            for h in range(150, 180):
                with self.assertRaises(RuntimeError):
                    self.run_to(loop, h)
        lost = self.events("stop_watch_lost")
        self.assertEqual([a.get("alarm") for a in lost if "held_back" not in a], ["ALARM"], "one alarm for thirty steps")
        held = [a for a in lost if "held_back" in a]
        self.assertEqual(len(held), 29, "each one held back is a line of its own (review of #349, L1)")
        self.assertTrue(all("alarm" not in a and a["from_block"] > 100 for a in held), "a line, not an alarm, saying all")
        flagged = [l for l in (self.dir / "stops.alarm").read_text().splitlines() if "stop_watch_lost" in l]
        self.assertEqual(len(flagged), 1)
        at, unraised = loop.again["stop_watch_lost"]
        self.assertEqual(unraised, 29)
        loop.again["stop_watch_lost"] = (at - sf.ALARM_AGAIN_S, unraised)
        with self.failing_logs(), self.assertRaises(RuntimeError):
            self.run_to(loop, 180)
        raised = [a for a in self.events("stop_watch_lost") if a.get("alarm")]
        self.assertEqual((len(raised), raised[1]["not_raised"]), (2, 29), "the next one says how many were not raised")
        self.assertEqual(loop.logs.width, sf.LOG_WINDOW, "an error that says nothing of the width teaches none")

    def test_a_stops_own_alarms_are_each_raised_however_close_together(self):
        loop = self.loop(wallet=None)
        self.armed_stop(loop)
        self.node.price.update({101: 900_000, 103: 1_000_000, 104: 900_000, 106: 1_000_000})
        self.run_to(loop, 101, 102, 103, 104, 105, 106)
        self.assertEqual([a["alarm"] for a in self.events("stop_missed")], ["ALARM", "ALARM"])

    def test_a_thread_that_keeps_ending_is_an_alarm_once_in_a_while_and_started_again_each_time(self):
        loop = self.loop(wallet=None)
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        with mock.patch.object(sf, "start", return_value=dead) as again:
            for _ in range(5):
                sf.keep_alive(loop, dead, 0.5)
        self.assertEqual(again.call_count, 5)
        dead = self.events("stop_loop_dead")
        self.assertEqual(([a.get("alarm") for a in dead], sum("held_back" in a for a in dead)), (["ALARM"] + [None] * 4, 4))


class TheFirstLookAndTheSide(Base):
    """Review 20 of #342, its L1 and L2."""

    def test_the_first_look_of_a_start_is_held_against_the_nodes_record_and_the_next_a_moment_later_not(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100, 101)
        asked = [p[0] for m, p in self.node.calls if m == "eth_getBlockByNumber"]
        self.assertEqual(asked, [hex(100)] if self.MULTICALL else [])

    def test_a_check_the_node_does_not_answer_makes_that_look_the_old_way_and_is_asked_again_a_moment_later(self):
        """The first look's check met a node that did not answer (busy, a server's error, not reached): raised, the
        check stayed due and every look after failed the same way, not one step in 400 blocks on a node that did not
        answer a block by its number (review of #349, M2). That look is made the old way, so is every one till the
        check is asked again NUMBERING_RETRY_S on, and the first that holds ends it; no block is lost."""
        if not self.MULTICALL:
            return
        for error in (c.NodeBusy("eth_getBlockByNumber: HTTP 429"), sf.Unsure("eth_getBlockByNumber: HTTP 500"),
                      sf.Unreached("eth_getBlockByNumber: ConnectTimeout")):
            with self.subTest(error=type(error).__name__):
                for f in self.dir.glob("prices*"):
                    f.unlink()
                self.journal = None
                self.node = Node()
                loop = self.loop(wallet=None)
                real = self.node.call

                def call(method, params=(), real=real, error=error):
                    if method == "eth_getBlockByNumber" and params[0] != "latest":
                        self.node.calls.append((method, params))
                        raise error
                    return real(method, params)

                with mock.patch.object(self.node, "call", side_effect=call):
                    self.run_to(loop, 100, 101, 102)
                self.assertEqual(loop.block, 102, "each look stepped")
                by_number = [p[0] for m, p in self.node.calls if m == "eth_getBlockByNumber" and p[0] != "latest"]
                self.assertEqual(by_number, [hex(100)], f"asked once, then not for {sf.NUMBERING_RETRY_S:g} seconds")
                self.assertEqual(sum(1 for m, p in self.node.calls if m == "eth_getBlockByNumber" and p[0] == "latest"), 3,
                                 "each look the old way")
                self.assertEqual(len(self.node.batches()), 1, "no one call wasted on a look made the old way")
                loop.check_failed_at -= sf.NUMBERING_RETRY_S
                before = len(self.node.calls)
                self.run_to(loop, 103, 104)
                self.assertEqual([p for m, p in self.node.calls[before:] if m == "eth_getBlockByNumber"],
                                 [[hex(103), False]], "asked again, held, and not again within the minute")
                self.assertIsNone(loop.check_failed_at)
                journal = pr.load_prices(pr.journal_text(self.dir / "prices.jsonl"))
                self.assertEqual((sorted(journal.px[BTC]), journal.gaps), (list(range(100, 105)), []))

    def test_a_node_that_never_answers_a_block_by_its_number_stands_no_step_still(self):
        """Through the loop's own run: two looks a block for two minutes, every eth_getBlockByNumber of a number an HTTP
        500. Every block stepped, the number asked for once in NUMBERING_RETRY_S (review of #349, M2: 0 of 400)."""
        if not self.MULTICALL:
            return
        loop = self.loop(wallet=None)
        real = self.node.call

        def call(method, params=()):
            if method == "eth_getBlockByNumber" and params[0] != "latest":
                self.node.calls.append((method, params))
                raise sf.Unsure("eth_getBlockByNumber: HTTP 500")
            return real(method, params)

        looks, now = {"n": 0}, [10_000.0]

        def served(until, stop):
            looks["n"] += 1
            now[0] += 0.5
            if looks["n"] % 2 == 0:
                self.node.head += 1
            if looks["n"] >= 240:
                stop.set()

        loop._served = served
        with mock.patch.object(self.node, "call", side_effect=call), \
                mock.patch.object(sf.time, "monotonic", side_effect=lambda: now[0]):
            loop.run(0.5, threading.Event())
        self.assertEqual((loop.block, self.node.head), (219, 220))
        by_number = sum(1 for m, p in self.node.calls if m == "eth_getBlockByNumber" and p[0] != "latest")
        self.assertLessEqual(by_number, 120 / sf.NUMBERING_RETRY_S + 1)
        self.assertEqual([e for e, f in self.said if f.get("alarm") or e in ("stop_loop_failed", "stop_node_busy")], [])

    def test_a_wrong_measure_at_the_start_loses_no_event(self):
        """L1: measured 0 where the node numbers a call as the next block (a measure that met the coincidences it asks
        for), the node's logs cut at its head. Unchecked for the first minute, the loop took the call's next block for
        the head and lost the stop set in block 102."""
        if not self.MULTICALL:
            return  # the old way has no numbering to measure

        class Clamped(Node):
            def call(self, method, params=()):
                out = super().call(method, params)
                return [l for l in out if int(l["blockNumber"], 16) <= self.head] if method == "eth_getLogs" else out

        self.node = Clamped()
        self.node.ahead = 1
        loop = self.loop(wallet=None)
        loop.ahead = 0
        self.run_to(loop, 100, 101)
        self.node.logs.append(set_log(102, 950_000, 103))
        self.run_to(loop, 102, 103, 104)
        self.assertIn((EXAM, BTC, False), loop.triggers, "the stop set in block 102 is known")

    def test_a_side_the_loop_could_not_read_in_the_reads_block_is_taken_from_the_read(self):
        """L2: an event of block 101 moved the position, the loop's read of its side failed; the exam's read of block
        101 says it."""
        loop = self.loop(wallet=None)
        self.node.logs.append(set_log(100, 950_000, 101))
        self.run_to(loop, 100)
        with mock.patch.object(self.node, "read", side_effect=RuntimeError("busy")):
            loop._read_sign(EXAM, BTC, 101)
        self.assertIsNone(loop.signs[(EXAM, BTC)])
        loop.apply_reread(EXAM, 100, {(EXAM, BTC, False): (950_000, 101)}, {BTC: -1})
        self.assertIsNone(loop.signs[(EXAM, BTC)], "a read of an earlier block says nothing of it")
        loop.apply_reread(EXAM, 101, {(EXAM, BTC, False): (950_000, 101)}, {BTC: 1})
        self.assertEqual(loop.signs[(EXAM, BTC)], 1)
        self.assertNotIn((EXAM, BTC), loop.unread_signs)


def the_old_way(module: dict) -> None:
    """Every class of tests of the loop in `module` (its globals), again on a node with no Multicall3: the loop as it
    was, the head and then a call a price, which is what it falls back to. A class with ONE_CALL_ONLY set tests what
    only the one call has."""
    for name, cls in list(module.items()):
        if (isinstance(cls, type) and issubclass(cls, Base) and cls.__module__ == module["__name__"]
                and cls.MULTICALL and not getattr(cls, "ONE_CALL_ONLY", False) and cls is not Base):
            module[name + "TheOldWay"] = type(name + "TheOldWay", (cls,), {"MULTICALL": False, "__module__": cls.__module__})


the_old_way(globals())


if __name__ == "__main__":
    unittest.main()
