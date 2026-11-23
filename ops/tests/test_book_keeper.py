"""The stop book's loop (ops/book_keeper.py): a check sent for every block while the book watches an asset, a settle for
each exam with a touch or a change in force, every gap said and judged, fixed gas everywhere. On a node made up here."""
import pathlib
import re
import shutil
import tempfile
import unittest
from unittest import mock

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from ops import book_keeper as bk
from ops import payout_review as pr
from ops import sim_keeper as sk
from ops import stop_filler as sf
from spike.hlspike import common as c

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOK = to_checksum_address("0x" + "b0" * 20)
EXAM = to_checksum_address("0x" + "11" * 20)
POOL = to_checksum_address("0x" + "55" * 20)
KEY = Account.from_key("0x" + "42" * 32)  # made up here; no key directory is read
POKER = Account.from_key("0x" + "45" * 32)  # the checks' own key, made up here
BTC = 3


def sel(sig: str) -> str:
    return "0x" + keccak(text=sig)[:4].hex()


def word(n: int) -> str:
    return "0x" + n.to_bytes(32, "big").hex()


def ev(topic, address, block, data, *topics, tx=None, index=0):
    return {"address": address, "topics": [topic, *topics], "data": "0x" + data.hex(), "blockNumber": hex(block),
            "transactionHash": tx or f"0x{block:064x}", "logIndex": hex(index)}


def entry(level=950_000, armed=90, below=True, next_level=0, next_at=0):
    return (level, armed, 1, 0, below, True, 0, next_level, next_at)


class Node:
    """A chain as the loop reads it: a head, the book's watched assets, each exam's touches and entries, logs,
    receipts and the sends."""

    def __init__(self):
        self.url = "http://127.0.0.1:9"
        self.head = 100
        self.watched = [BTC]
        self.watched_at: dict[int, list[int]] = {}
        self.touches: dict[str, list] = {}
        self.entries: dict[tuple[str, int, bool], tuple] = {}
        self.logs: list[dict] = []
        self.receipts: dict[str, dict] = {}
        self.raw: list[str] = []
        self.price: dict[int, int] = {}
        self.refuse_settle = False
        self.pokes_land = True  # every check gets its receipt at the next look, unless a test says otherwise
        self.calls: list = []
        self.entries_at: dict[int, dict] = {}  # block -> the entries then, where a test says
        self.last_poke: int | None = None  # lastPokeBlock(); the head when not set
        self.time: float | None = None  # the newest block's time; now when not set
        self.balance = 10 ** 21

    _senders: dict = {}

    @classmethod
    def sender_of(cls, raw: str) -> str:
        if raw not in cls._senders:
            cls._senders[raw] = Account.recover_transaction(raw)
        return cls._senders[raw]

    @staticmethod
    def nonce_of(raw: str) -> int:
        from eth_account.typed_transactions import TypedTransaction
        return TypedTransaction.from_bytes(HexBytes(raw)).as_dict()["nonce"]

    @staticmethod
    def decoded_one(raw: str) -> tuple[str, int, str]:
        from eth_account.typed_transactions import TypedTransaction
        t = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        return to_checksum_address(t["to"]), t["gas"], "0x" + bytes(t["data"])[:4].hex()

    def decoded(self) -> list[tuple[str, int, str]]:
        """Each transaction sent: (to, gas, the call's selector)."""
        return [self.decoded_one(raw) for raw in self.raw]

    def settles(self) -> list[tuple[str, int, str]]:
        return [d for d in self.decoded() if d[2] == sel("settle(address)")]

    def pokes(self) -> list[tuple[str, int, str]]:
        return [d for d in self.decoded() if d[2] == sel("poke()")]

    def entries_now(self, tag) -> dict:
        if tag != "latest" and int(tag, 16) in self.entries_at:
            return self.entries_at[int(tag, 16)]
        return self.entries

    def call(self, method, params=()):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            import time as _t
            return {"number": hex(self.head), "timestamp": hex(int(_t.time() if self.time is None else self.time))}
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_chainId":
            return hex(c.CHAIN_ID)
        if method == "eth_gasPrice":
            return hex(100_000_000)
        if method == "eth_getTransactionCount":
            if params[1] != "latest":
                return hex(7)
            # Past every nonce of the key's transactions that landed: a receipt, or a check while checks land.
            addr = to_checksum_address(params[0])
            landed = 0
            for raw in self.raw:
                h = "0x" + keccak(hexstr=raw).hex()
                if self.sender_of(raw) != addr:
                    continue
                if h in self.receipts or (self.pokes_land and self.decoded_one(raw)[2] == sel("poke()")):
                    landed = max(landed, self.nonce_of(raw) + 1 - 7)
            return hex(7 + landed)
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
            if params[0] not in self.receipts and self.pokes_land:
                for raw in self.raw:
                    if "0x" + keccak(hexstr=raw).hex() == params[0] and self.decoded_one(raw)[2] == sel("poke()"):
                        return {"status": "0x1", "blockNumber": hex(self.head)}
            return self.receipts.get(params[0])
        if method == "eth_call":
            q, tag = params
            data, to = q["data"], to_checksum_address(q["to"])
            if to == to_checksum_address(sf.ORACLE_PX):
                return "0x" + encode(["uint64"], [self.price.get(int(tag, 16), 1_000_000)]).hex()
            if data.startswith(sel("watched()")):
                w = self.watched_at.get(int(tag, 16), self.watched) if tag != "latest" else self.watched
                return "0x" + encode(["uint32[]"], [w]).hex()
            if data.startswith(sel("touches(address)")):
                exam = to_checksum_address(decode(["address"], bytes.fromhex(data[10:]))[0])
                return "0x" + encode([f"{bk.TOUCH}[]"], [self.touches.get(exam, [])]).hex()
            if data.startswith(sel("lastPokeBlock()")):
                return word(self.head if self.last_poke is None else self.last_poke)
            if data.startswith(sel("entryOf(address,uint32,bool)")):
                exam, asset, take = decode(["address", "uint32", "bool"], bytes.fromhex(data[10:]))
                e = self.entries_now(tag).get((to_checksum_address(exam), asset, take), (0, 0, 0, 0, False, False, 0, 0, 0))
                return "0x" + encode([bk.ENTRY], [e]).hex()
            if data.startswith(sel("entries(address)")):
                exam = to_checksum_address(decode(["address"], bytes.fromhex(data[10:]))[0])
                return word(len([k for k in self.entries_now(tag) if k[0] == exam]))
            if data.startswith(sel("settle(address)")):
                if self.refuse_settle:
                    raise RuntimeError("eth_call: execution reverted")
                return word(0)
            raise RuntimeError(f"unexpected call {data[:10]}")
        raise RuntimeError(f"unexpected method {method}")


class Keeper:
    def __init__(self):
        self.accounts = {EXAM: {"pool": POOL, "kind": "exam"}}
        self.pools = [POOL]
        self.done: set[str] = set()
        self.sender = KEY.address

    def is_filler(self):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.node = Node()
        self.said: list[tuple[str, dict]] = []
        self.alarms: list[tuple[str, dict]] = []
        p = mock.patch.object(bk, "log", side_effect=lambda e, **f: self.said.append((e, f)))
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(sf, "log", side_effect=lambda e, **f: self.said.append((e, f)))
        q.start()
        self.addCleanup(q.stop)

    def loop(self, wallet=KEY):
        alarm = mock.Mock(side_effect=lambda e, **f: self.alarms.append((e, f)))
        alarm.flag = None
        return bk.BookLoop(Keeper(), self.node, BOOK, wallet=wallet,
                           sender=sf.Sender(wallet, self.node) if wallet else None, alarm=alarm,
                           lanes=[bk.Lane("poke", sf.Sender(POKER, self.node))] if wallet else [])

    def run_to(self, loop, *heads):
        out = []
        for h in heads:
            self.node.head = h
            out.append(loop.step(h))
        return out

    def events(self, name):
        return [f for e, f in self.said if e == name]

    def alarm_of(self, name):
        return [f for e, f in self.alarms if e == name]


class TheContract(unittest.TestCase):
    def test_the_events_and_the_structs_are_the_books(self):
        src = re.sub(r"\s+", " ", (ROOT / "src/sim/StopBook.sol").read_text())
        self.assertIn("event Gap(uint256 from, uint256 to);", src)
        self.assertIn("event NoPrice(uint32 indexed asset);", src)
        self.assertIn("event Fired(address indexed exam, uint32 indexed asset, bool isTake, uint64 px, uint256 touchBlock, "
                      "uint8 result);", src)
        self.assertEqual(bk.GAP, "0x" + keccak(text="Gap(uint256,uint256)").hex())
        self.assertEqual(bk.FIRED, "0x" + keccak(text="Fired(address,uint32,bool,uint64,uint256,uint8)").hex())
        self.assertIn("struct Entry { uint32 level; uint40 armedAt; uint40 from; uint40 cursor; bool below; bool listed; "
                      "uint8 at; uint32 nextLevel; uint40 nextAt; }", src)
        self.assertEqual(bk.ENTRY, "(uint32,uint40,uint40,uint40,bool,bool,uint8,uint32,uint40)")
        self.assertIn("struct Touch { uint32 asset; bool isTake; uint64 level; uint64 px; uint32 book; uint256 blk; "
                      "uint256 time; uint256 pos; }", src)
        self.assertEqual(bk.TOUCH, "(uint32,bool,uint64,uint64,uint32,uint256,uint256,uint256)")
        for name, value in (("FAILED", 0), ("FILLED", 1), ("CLOSED", 2), ("ENDED", 3), ("STARVED", 4)):
            self.assertIn(f"uint8 public constant {name} = {value};", src)
            self.assertEqual(getattr(bk, name), value)
        for sig in ("function poke() external", "function settle(address exam) external returns (uint256)",
                    "function watched() external view returns (uint32[] memory)",
                    "function touches(address exam) external view returns (Touch[] memory list)",
                    "function entries(address exam) external view returns (uint256 n)",
                    "function entryOf(address exam, uint32 asset, bool isTake) external view returns (Entry memory)"):
            self.assertIn(sig, src)
        self.assertIn("StopBook public immutable stopBook;", (ROOT / "src/sim/SimPoolFactory.sol").read_text())

    def test_the_phase_and_the_reach_are_the_books(self):
        src = (ROOT / "src/sim/StopBook.sol").read_text()
        self.assertIn("if (e.nextAt != 0 && blk >= e.nextAt) return (e.nextLevel, e.nextAt);", src)
        self.assertIn("return e.below ? x != 0 && x <= e.level : x >= e.level;", src)
        self.assertEqual(bk.phase(entry(950_000, 90, True, 960_000, 120), 119), (950_000, 90))
        self.assertEqual(bk.phase(entry(950_000, 90, True, 960_000, 120), 120), (960_000, 120))
        self.assertEqual(bk.phase(entry(950_000, 90, True, 0, 120), 121), (0, 120), "taken back from block 120")
        self.assertTrue(bk.reached(True, 950_000, 950_000))
        self.assertFalse(bk.reached(True, 950_000, 0), "no price reaches nothing")
        self.assertTrue(bk.reached(False, 1_050_000, 1_050_001))

    def test_the_gas_is_fixed_and_fits_a_small_block(self):
        self.assertLessEqual(bk.MAX_TX_GAS, 2_000_000)
        # The book's measure: 20 assets with 50 000 for each read, at their dearest, 1 260 830 (+25 000 a transaction).
        self.assertGreaterEqual(bk.poke_gas(20), 1_260_830 + 25_000)
        self.assertGreaterEqual(bk.poke_gas(16, 2 * 15_000), 985_207 + 25_000)
        self.assertEqual(bk.poke_gas(40), bk.MAX_TX_GAS)
        self.assertGreaterEqual(bk.SETTLE_GAS, 1_355_902 + 25_000, "the largest part of a split month in one call")


class TheCheck(Base):
    def test_a_check_is_sent_once_for_each_block_with_a_fixed_gas_while_the_book_watches(self):
        loop = self.loop()
        self.assertEqual(self.run_to(loop, 100, 100, 101), [["poke"], [], ["poke"]])
        sent = self.node.decoded()
        self.assertEqual([(to, s) for to, _, s in sent], [(BOOK, sel("poke()"))] * 2)
        self.assertEqual({g for _, g, _ in sent}, {bk.poke_gas(1)})
        self.assertFalse(any(m == "eth_estimateGas" for m, _ in self.node.calls), "never an estimate")
        self.node.watched = []
        self.assertEqual(self.run_to(loop, 102, 103, 104), [["poke"], [], []],
                         "nothing watched: no check from the block after the read that saw it")

    def test_a_watch_with_no_key_sends_nothing(self):
        loop = self.loop(wallet=None)
        self.run_to(loop, 100, 101)
        self.assertEqual(self.node.raw, [])

    def test_checks_waiting_for_a_block_are_not_piled_up(self):
        loop = self.loop()
        self.node.pokes_land = False
        self.run_to(loop, *range(100, 110))
        self.assertEqual(len(self.node.raw), bk.MAX_POKES_IN_FLIGHT)
        self.assertTrue(self.alarm_of("book_pokes_queued"), "a key whose checks all wait is an alarm, never a silence")

    def test_a_check_that_reverted_raises_the_gas_of_the_next_and_is_an_alarm(self):
        loop = self.loop()
        self.node.pokes_land = False
        self.run_to(loop, 100)
        tx = "0x" + keccak(hexstr=self.node.raw[0]).hex()
        self.node.receipts[tx] = {"status": "0x0", "blockNumber": hex(101)}
        self.run_to(loop, 101, 102)
        self.assertTrue(self.alarm_of("book_poke_reverted"))
        self.assertEqual(self.node.decoded()[-1][1], bk.poke_gas(1) * 5 // 4, "a quarter more, not the whole block")


class TheGaps(Base):
    def test_a_gap_while_the_book_watched_is_an_alarm_and_its_touches_another(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True)
        self.run_to(loop, 100)
        self.node.price.update({101: 960_000, 102: 949_000})
        self.node.logs.append(ev(bk.GAP, BOOK, 104, encode(["uint256", "uint256"], [101, 103])))
        self.run_to(loop, 104)
        gap = self.alarm_of("book_gap")
        self.assertEqual((gap[0]["from_block"], gap[0]["to"], gap[0]["blocks"], loop.gaps, loop.gap_blocks),
                         (101, 103, 3, 1, 3))
        touch = self.alarm_of("book_gap_touch")
        self.assertEqual([(t["exam"], t["block"], t["oracle"]) for t in touch], [(EXAM, 102, 949_000)],
                         "said once, at its first touch")

    def test_a_gap_while_the_book_watched_nothing_is_a_line(self):
        loop = self.loop()
        self.node.watched_at[100] = []
        self.run_to(loop, 100)
        self.node.logs.append(ev(bk.GAP, BOOK, 104, encode(["uint256", "uint256"], [101, 103])))
        self.run_to(loop, 104)
        self.assertEqual(self.alarm_of("book_gap"), [])
        self.assertTrue(self.events("book_gap_idle"))

    def test_a_change_in_force_reads_its_new_level_in_the_gap(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True, 0, 102)  # taken back from block 102
        self.run_to(loop, 100)
        self.node.price.update({101: 960_000, 102: 900_000, 103: 900_000})
        self.node.logs.append(ev(bk.GAP, BOOK, 104, encode(["uint256", "uint256"], [101, 103])))
        self.run_to(loop, 104)
        self.assertEqual(self.alarm_of("book_gap_touch"), [], "from block 102 there was no stop")


class TheSettles(Base):
    def touch(self, blk=101):
        return (BTC, False, 950_000, 949_000, 0xFFFFFFFF, blk, 1_000_000 + blk, 1)

    def test_an_exam_with_a_touch_is_settled_with_a_fixed_gas_once_while_in_flight(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [self.touch()]
        self.assertEqual(self.run_to(loop, 101), [["poke", EXAM]])
        self.assertEqual(self.run_to(loop, 102), [["poke"]], "its settle has no receipt yet")
        to, gas, s = self.node.decoded()[1]
        self.assertEqual((to, gas, s), (BOOK, bk.SETTLE_GAS, sel("settle(address)")))
        self.assertLess(self.node.raw.index(self.node.raw[0]), 1, "the check goes out before the settle")
        tx = "0x" + keccak(hexstr=self.node.raw[1]).hex()
        self.node.receipts[tx] = {"status": "0x1", "blockNumber": hex(102), "gasUsed": hex(900_000)}
        self.assertEqual(self.run_to(loop, 103), [["poke", EXAM]], "still touched: the next part")
        self.node.touches[EXAM] = []
        tx = "0x" + keccak(hexstr=self.node.raw[-1]).hex()
        self.node.receipts[tx] = {"status": "0x1", "blockNumber": hex(104)}
        self.assertEqual(self.run_to(loop, 104, 105), [["poke"], ["poke"]])

    def test_a_starved_fill_is_sent_again_with_the_most_a_small_block_holds(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [self.touch()]
        self.run_to(loop, 101)
        tx = "0x" + keccak(hexstr=self.node.raw[1]).hex()
        self.node.receipts[tx] = {"status": "0x1", "blockNumber": hex(102)}
        self.node.logs.append(ev(bk.FIRED, BOOK, 102, encode(["bool", "uint64", "uint256", "uint8"],
                                                             [False, 949_000, 101, bk.STARVED]), word(int(EXAM, 16)), word(BTC)))
        self.run_to(loop, 102, 103)
        self.assertEqual(self.node.settles()[-1][1], bk.MAX_TX_GAS)
        self.assertTrue(self.events("book_settle_starved"))

    def test_a_fill_the_exam_refused_is_an_alarm(self):
        loop = self.loop()
        self.run_to(loop, 101)
        self.node.logs.append(ev(bk.FIRED, BOOK, 102, encode(["bool", "uint64", "uint256", "uint8"],
                                                             [False, 949_000, 101, bk.FAILED]), word(int(EXAM, 16)), word(BTC)))
        self.run_to(loop, 102)
        self.assertTrue(self.alarm_of("book_fill_failed"))

    def test_a_settle_the_book_would_refuse_is_not_sent(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [self.touch()]
        self.node.refuse_settle = True
        self.assertEqual(self.run_to(loop, 101), [["poke"]])

    def test_a_change_in_force_is_settled_and_a_first_stop_is_not(self):
        loop = self.loop()
        set_log = ev(sk.TRIGGER_SET, EXAM, 101, encode(["bool", "uint64", "uint64", "uint256"], [False, 0, 0, 103]),
                     word(BTC))
        self.node.logs.append(set_log)
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True, 0, 103)
        self.assertEqual(self.run_to(loop, 101, 102), [["poke"], ["poke"]], "not in force yet")
        self.assertEqual(self.run_to(loop, 103), [["poke", EXAM]])
        self.node.logs.append(ev(sk.TRIGGER_SET, EXAM, 104, encode(["bool", "uint64", "uint64", "uint256"],
                                                                   [True, 1_050_000, 0, 106]), word(BTC)))
        self.node.entries[(EXAM, BTC, True)] = entry(1_050_000, 106, False)  # a first take: nothing to settle
        self.run_to(loop, 104, 105, 106, 107)
        self.assertNotIn((EXAM, BTC, True), loop.changes)

    def test_an_exam_that_ended_is_not_settled(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [self.touch()]
        loop.keeper.done.add(EXAM)
        self.assertEqual(self.run_to(loop, 101), [["poke"]])


class TheReadOfAnExam(Base):
    def test_a_read_finds_the_entries_and_the_changes(self):
        loop = self.loop()
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True, 0, 130)

        def call(method, params=()):
            if method == "eth_call" and params[0]["data"].startswith(sel("result()")):
                return "0x" + encode(["uint8", "address", "uint64", "uint64"], [1, EXAM, 0, 0]).hex()
            if method == "eth_call" and params[0]["data"].startswith(sel("entries(address)")):
                return word(1)
            if method == "eth_call" and params[0]["data"].startswith(sel("rules()")):
                return "0x" + encode([sk.RULES], [(300, 600, 1000, [BTC], [])]).hex()
            return self.node.call(method, params)

        loop.reread(EXAM, call=call)
        self.run_to(loop, 100)
        self.assertIn(EXAM, loop.holders)
        self.assertEqual(loop.changes, {(EXAM, BTC, False): 130})


class TheNodes(unittest.TestCase):
    def test_a_busy_or_lost_call_goes_to_the_backup_and_an_answer_does_not(self):
        first, backup = mock.Mock(url="a"), mock.Mock(url="b")
        first.call.side_effect = sf.Unsure("eth_call: ReadTimeout")
        backup.call.return_value = "0x1"
        self.assertEqual(bk.Nodes(first, backup).call("eth_call", []), "0x1")
        first.call.side_effect = RuntimeError("eth_call: execution reverted")
        with self.assertRaises(RuntimeError):
            bk.Nodes(first, backup).call("eth_call", [])
        first.call.side_effect = c.NodeBusy("eth_call: HTTP 429")
        with self.assertRaises(c.NodeBusy):
            bk.Nodes(first, None).call("eth_call", [])

    def test_a_factory_before_the_book_has_none_and_a_failed_read_is_not_none(self):
        self.assertEqual(bk.book_of({"StopBook": BOOK}, "0x" + "33" * 20), BOOK)
        with mock.patch.object(c, "rpc", side_effect=RuntimeError("eth_call: {'message': 'execution reverted'}")):
            self.assertIsNone(bk.book_of({}, "0x" + "33" * 20))
        with mock.patch.object(c, "rpc", side_effect=RuntimeError("eth_call: header not found")):
            with self.assertRaises(RuntimeError):
                bk.book_of({}, "0x" + "33" * 20)
        with mock.patch.object(c, "rpc", return_value=word(int(BOOK, 16))):
            self.assertEqual(bk.book_of({}, "0x" + "33" * 20), BOOK)


class TheKey(unittest.TestCase):
    def test_a_nonce_is_let_go_only_at_two_counts_past_it_in_a_row(self):
        node = mock.Mock()
        counts = iter([8, 7, 8, 8])
        node.call.side_effect = lambda m, p=(): {"eth_getTransactionCount": lambda: hex(next(counts)),
                                                 "eth_getTransactionReceipt": lambda: None,
                                                 "eth_sendRawTransaction": lambda: "0x"}[m]()
        s = sf.Sender(KEY, node)
        s.ledger[7] = {"hash": "0x" + "ab" * 32, "raw": "0x00", "at": 0.0, "since": 0.0, "past": 0}
        for _ in range(3):
            s.ledger[7]["at"] = -1e9
            s.sweep()
        self.assertEqual(s.gone, set(), "past, back, past: not twice in a row")
        s.ledger[7]["at"] = -1e9
        s.sweep()
        self.assertEqual(s.gone, {7})


class PayoutReviewWithABook(unittest.TestCase):
    """A touch is missed with a book only in a block its journal does not hold (`gap_touches`)."""

    def e(self, name, block, tx=None, **kw):
        return {"name": name, "block": block, "index": 0, "t": 1_000_000 + block, "tx": tx or f"0x{block:064x}", **kw}

    def test_a_stop_reached_in_a_gap_is_missed_and_one_reached_in_a_checked_block_is_not(self):
        evs = [self.e("Filled", 10, asset=BTC, buy=True, sz=100, px=1_000_000, cost=0, late=0, source=0),
               self.e("TriggerSet", 11, asset=BTC, take=False, px=950_000, sz=0, armedAt=13)]
        prices = {BTC: {20: 949_000, 21: 949_000, 30: 940_000}}
        out = pr.gap_touches(evs, prices, 40, [(20, 22)])
        self.assertEqual([(m["first"], m["last"], m["then"], m["gap"]) for m in out["missed"]], [(20, 21, "standing", True)])
        self.assertEqual(out["unread"], 1, "block 22 of the gap was not read")
        self.assertEqual(pr.gap_touches(evs, prices, 40, [])["missed"], [], "block 30 was checked: the book carries it out")
        self.assertFalse(pr.gap_touches(evs, prices, 40, None)["read"])

    def test_a_stop_taken_back_stands_up_to_its_change_and_a_fill_up_to_its_touch(self):
        evs = [self.e("Filled", 10, asset=BTC, buy=True, sz=100, px=1_000_000, cost=0, late=0, source=0),
               self.e("TriggerSet", 11, asset=BTC, take=False, px=950_000, sz=0, armedAt=13),
               self.e("TriggerSet", 20, asset=BTC, take=False, px=0, sz=0, armedAt=23)]
        spans = pr.book_spans(evs, 40)
        self.assertEqual([(s["armedAt"], s["end"], s["then"]) for s in spans], [(13, 22, "taken off")])
        fill_tx = "0x" + "ff" * 32
        evs = evs[:2] + [self.e("TriggerCleared", 30, tx=fill_tx, asset=BTC, take=False),
                         self.e("Filled", 30, tx=fill_tx, asset=BTC, buy=False, sz=100, px=949_000, cost=0, late=12, source=0),
                         self.e("TriggerFilled", 30, tx=fill_tx, asset=BTC, take=False, sz=100, px=949_000, cost=0,
                                trigger_px=950_000, source=0, touch_block=25, touch_px=949_000)]
        spans = pr.book_spans(evs, 40)
        self.assertEqual([(s["armedAt"], s["end"], s["then"]) for s in spans], [(13, 24, "carried out")])

    def test_a_moved_stop_carried_out_at_its_new_level_is_the_change(self):
        fill_tx = "0x" + "ff" * 32
        evs = [self.e("Filled", 10, asset=BTC, buy=True, sz=100, px=1_000_000, cost=0, late=0, source=0),
               self.e("TriggerSet", 11, asset=BTC, take=False, px=950_000, sz=0, armedAt=13),
               self.e("TriggerSet", 20, asset=BTC, take=False, px=960_000, sz=0, armedAt=23),
               self.e("TriggerCleared", 30, tx=fill_tx, asset=BTC, take=False),
               self.e("Filled", 30, tx=fill_tx, asset=BTC, buy=False, sz=100, px=959_000, cost=0, late=2, source=0),
               self.e("TriggerFilled", 30, tx=fill_tx, asset=BTC, take=False, sz=100, px=959_000, cost=0,
                      trigger_px=960_000, source=0, touch_block=25, touch_px=959_000)]
        spans = pr.book_spans(evs, 40)
        self.assertEqual([(s["px"], s["armedAt"], s["end"], s["then"]) for s in spans],
                         [(950_000, 13, 22, "replaced"), (960_000, 23, 24, "carried out")])
        closes = pr.trigger_closes(evs, book=True)
        self.assertEqual([(t["block"], t["px"], t["armedAt"]) for t in closes], [(25, 960_000, 23)])
        chain = {"exam_events": {EXAM: evs}, "oracle": {pr.oracle_key(BTC, 25): 959_000}, "bookExams": [EXAM]}
        r = pr.recount(EXAM, chain, pr.Params())
        self.assertEqual([f["what"] for f in r["flags"]], [], "the change, touched at its level, is what was carried out")
        chain["oracle"][pr.oracle_key(BTC, 25)] = 961_000
        r = pr.recount(EXAM, chain, pr.Params())
        self.assertIn("trigger-not-reached", [f["what"] for f in r["flags"]])
        self.assertIn("trigger-touch-price", [f["what"] for f in r["flags"]])


if __name__ == "__main__":
    unittest.main()


class TheContractForward(Base):
    def test_a_settle_short_of_one_step_goes_again_with_the_most(self):
        src = (ROOT / "src/sim/StopBook.sol").read_text()
        self.assertIn("error NoProgress(uint256 left);", src)
        self.assertIn("uint32 public oracleGas;", src)
        self.assertEqual(bk.NO_PROGRESS, sel("NoProgress(uint256)"))
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [(BTC, False, 950_000, 949_000, 0xFFFFFFFF, 101, 1_000_101, 1)]
        orig = self.node.call

        def short(method, params=()):
            if method == "eth_call" and params[0]["data"].startswith(sel("settle(address)")) and int(params[0]["gas"], 16) < bk.MAX_TX_GAS:
                raise RuntimeError(f"eth_call: {{'message': 'execution reverted', 'data': '{bk.NO_PROGRESS}{'0' * 63}1'}}")
            return orig(method, params)

        self.node.call = short
        self.assertEqual(self.run_to(loop, 101), [["poke"]])
        self.assertEqual(self.run_to(loop, 102), [["poke", EXAM]])
        self.assertEqual(self.node.settles()[-1][1], bk.MAX_TX_GAS)

    def test_the_read_gas_is_the_books(self):
        node = mock.Mock()
        node.call.side_effect = lambda m, p=(): word(15_000)
        self.assertEqual(bk.precompile_gas_of(node, BOOK), 30_000)
        node.call.side_effect = RuntimeError("eth_call: execution reverted")
        self.assertEqual(bk.precompile_gas_of(node, BOOK), 2 * bk.PRECOMPILE_GAS)


class TheReviewOfTheLoop(Base):
    """The bot review of cp#336: an ended exam keeps nothing, a busy node never says a gap twice, a gap's read-back
    never holds up the next check, and a fill that keeps failing is not sent again every block."""

    def gap(self, frm, to, at):
        self.node.logs.append(ev(bk.GAP, BOOK, at, encode(["uint256", "uint256"], [frm, to])))

    def test_an_ended_exam_leaves_nothing_behind(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        loop.settle_gas[EXAM] = bk.MAX_TX_GAS
        loop.settle_refused[EXAM] = 100
        loop.backoff[EXAM] = 130
        loop.failed_said.add(EXAM)
        loop._exam_over(EXAM)
        self.assertEqual((loop.settle_gas, loop.settle_refused, loop.backoff, loop.failed_said),
                         ({}, {}, {}, set()))

    def test_a_long_gap_is_read_back_a_few_blocks_a_step_after_the_check_and_said_once(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True)
        self.run_to(loop, 100)
        self.gap(101, 120, 121)
        self.node.price[118] = 949_000
        oracle = lambda: len([p for m, p in self.node.calls if m == "eth_call" and p[0]["to"] == to_checksum_address(sf.ORACLE_PX)])
        steps = self.run_to(loop, 121)
        self.assertEqual(oracle(), bk.GAP_BLOCKS_PER_STEP, "a few blocks a step, so the next check is not held up")
        steps += self.run_to(loop, 122, 123, 124)
        self.assertTrue(all(s[:1] == ["poke"] for s in steps), "every step's check goes out first")
        self.assertEqual(len(self.alarm_of("book_gap")), 1)
        self.assertEqual([t["block"] for t in self.alarm_of("book_gap_touch")], [118], "read by the fourth step")
        oracle_reads = [p for m, p in self.node.calls if m == "eth_call" and p[0]["to"] == to_checksum_address(sf.ORACLE_PX)]
        self.assertEqual(len(oracle_reads), 20)

    def test_a_busy_node_in_the_read_back_leaves_it_for_the_next_step_and_says_the_gap_once(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.entries[(EXAM, BTC, False)] = entry(950_000, 90, True)
        self.run_to(loop, 100)
        self.gap(101, 103, 104)
        self.node.price[102] = 949_000
        orig = self.node.call
        busy = {"on": True}

        def call(method, params=()):
            if busy["on"] and method == "eth_call" and params[0]["data"].startswith(sel("entryOf(address,uint32,bool)")):
                raise c.NodeBusy("eth_call: HTTP 429")
            return orig(method, params)

        self.node.call = call
        self.run_to(loop, 104)
        self.assertEqual((len(self.alarm_of("book_gap")), self.alarm_of("book_gap_unjudged"), loop.gaps), (1, [], 1))
        busy["on"] = False
        self.run_to(loop, 105)
        self.assertEqual(len(self.alarm_of("book_gap")), 1, "the gap is not said again")
        self.assertEqual([t["block"] for t in self.alarm_of("book_gap_touch")], [102])

    def test_a_fill_that_fails_is_raised_once_and_settled_again_only_after_a_wait(self):
        loop = self.loop()
        loop.holders.add(EXAM)
        self.node.touches[EXAM] = [(BTC, False, 950_000, 949_000, 0xFFFFFFFF, 101, 1_000_101, 1)]
        self.run_to(loop, 101)
        tx = "0x" + keccak(hexstr=self.node.raw[1]).hex()
        self.node.receipts[tx] = {"status": "0x1", "blockNumber": hex(102)}
        failed = encode(["bool", "uint64", "uint256", "uint8"], [False, 949_000, 101, bk.FAILED])
        self.node.logs.append(ev(bk.FIRED, BOOK, 102, failed, word(int(EXAM, 16)), word(BTC)))
        steps = self.run_to(loop, 102, *range(103, 102 + bk.FAIL_BACKOFF_BLOCKS))
        self.assertTrue(all(EXAM not in s for s in steps), "no settle while it waits")
        self.assertEqual(self.run_to(loop, 102 + bk.FAIL_BACKOFF_BLOCKS), [["poke", EXAM]])
        tx = "0x" + keccak(hexstr=self.node.raw[-1]).hex()
        self.node.receipts[tx] = {"status": "0x1", "blockNumber": hex(133)}
        self.node.logs.append(ev(bk.FIRED, BOOK, 133, failed, word(int(EXAM, 16)), word(BTC)))
        self.run_to(loop, 133)
        self.assertEqual(len(self.alarm_of("book_fill_failed")), 1, "raised once for the exam")


class PayoutReviewTheReview(PayoutReviewWithABook):
    """The bot review of cp#336 on the payout review with a book."""

    def closed_by_the_stop(self):
        fill_tx = "0x" + "ff" * 32
        return [self.e("Filled", 10, asset=BTC, buy=True, sz=100, px=1_000_000, cost=0, late=0, source=0),
                self.e("TriggerSet", 11, asset=BTC, take=False, px=950_000, sz=0, armedAt=13),
                self.e("TriggerSet", 11, asset=BTC, take=True, px=1_100_000, sz=0, armedAt=13),
                self.e("TriggerCleared", 120, tx=fill_tx, asset=BTC, take=False),
                self.e("TriggerCleared", 120, tx=fill_tx, asset=BTC, take=True),
                self.e("Filled", 120, tx=fill_tx, asset=BTC, buy=False, sz=100, px=949_000, cost=0, late=92, source=0),
                self.e("TriggerFilled", 120, tx=fill_tx, asset=BTC, take=False, sz=100, px=949_000, cost=0,
                       trigger_px=950_000, source=0, touch_block=105, touch_px=949_000, touch_t=999_900)]

    def test_the_take_a_stop_closed_with_the_position_stood_up_to_the_touch_not_the_settle(self):
        spans = {s["take"]: s for s in pr.book_spans(self.closed_by_the_stop(), 200)}
        self.assertEqual((spans[True]["end"], spans[True]["then"]), (104, "closed"))
        self.assertEqual(pr.gap_touches(self.closed_by_the_stop(), {BTC: {110: 1_200_000}}, 200, [(110, 110)])["missed"], [],
                         "a gap after the touch is no missed take: the position was closed")

    def test_a_stops_close_is_held_to_the_venue_minute_of_its_touch(self):
        evs = self.closed_by_the_stop()
        chain = {"exam_events": {EXAM: evs}, "oracle": {pr.oracle_key(BTC, 105): 949_000}, "bookExams": [EXAM],
                 "venue": {str(BTC): {str(999_900 // 60 * 60): 949_100, str(1_000_120 // 60 * 60): 990_000}}}
        flags = [f["what"] for f in pr.recount(EXAM, chain, pr.Params())["flags"]]
        self.assertNotIn("oracle-off-venue", flags)

    def test_an_account_whose_book_read_fails_keeps_the_factorys_book(self):
        from unittest import mock as m
        book = BOOK
        with m.patch.object(bk, "book_of", return_value=book), \
                m.patch.object(pr.c, "call_view", side_effect=RuntimeError("eth_call: header not found")), \
                m.patch.object(pr, "logs", return_value=[]):
            got, exams, gaps = pr.read_book({}, "0x" + "33" * 20, [EXAM], 1, 2)
        self.assertEqual((got, exams, gaps), (book, {EXAM}, []))
        other = to_checksum_address("0x" + "99" * 20)
        with m.patch.object(bk, "book_of", return_value=book), \
                m.patch.object(pr.c, "call_view", return_value=(other,)), m.patch.object(pr, "logs", return_value=[]):
            self.assertEqual(pr.read_book({}, "0x" + "33" * 20, [EXAM], 1, 2)[1], set(), "another book: not this one's")
