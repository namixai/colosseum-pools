"""ops/multi_run.py: the parts that turn what a person types into what a contract takes, and back."""
import pathlib
import re
import unittest

from eth_utils import keccak

from ops import multi_run as r

ROOT = pathlib.Path(__file__).resolve().parents[2]
MULTI = (ROOT / "src/multi/MultiPool.sol").read_text()
EXAM = (ROOT / "src/exam/PaperExam.sol").read_text()
POOL = (ROOT / "src/Pool.sol").read_text()


def enum_names(text: str, name: str) -> tuple:
    body = re.search(r"enum " + name + r" \{(.*?)\}", text, re.S).group(1)
    lines = [l.strip().rstrip(",") for l in body.split("\n")]
    return tuple(l for l in lines if l and not l.startswith("//"))


def struct_types(text: str, name: str, enums: tuple) -> list:
    body = re.search(r"struct " + name + r" \{(.*?)\n    \}", text, re.S).group(1)
    fields = [l.strip() for l in body.split("\n") if l.strip().endswith(";")]
    return ["uint8" if f.split(" ")[0] in enums else f.split(" ")[0] for f in fields]


class Copies(unittest.TestCase):
    def test_names_follow_the_contracts_enums(self):
        self.assertEqual(r.EXAM_STATUS, enum_names(EXAM, "Status"))
        self.assertEqual(r.SEAT_STATE, enum_names(MULTI, "SeatState"))
        self.assertEqual(r.STAGE, enum_names(POOL, "Stage"))

    def test_seat_info_is_the_struct_the_contract_declares(self):
        self.assertEqual(r.SEAT_INFO, struct_types(MULTI, "SeatInfo", ("SeatState",)))

    def test_event_topics_are_the_events_the_contract_emits(self):
        self.assertIn("event ExamSold(address indexed exam, address indexed trader, uint256 line, uint64 size, uint64 price);", MULTI)
        self.assertIn("event Claimed(address indexed exam, address indexed trader, address indexed seat);", MULTI)
        self.assertEqual(r.EXAM_SOLD, "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64)").hex())
        self.assertEqual(r.CLAIMED, "0x" + keccak(text="Claimed(address,address,address)").hex())

    def test_the_calls_it_sends_exist(self):
        for sig in ("function buy(uint256 line)", "function claim(address exam)", "function open(address seat)",
                    "function rearm(address seat)", "function finish(address seat)", "function sweep(address seat)",
                    "function release(address exam)", "function withdraw(uint64 amount1e8)",
                    "function noteActivity(address seat)",
                    "function closeIdle(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)",
                    "function withdrawEvm(uint256 amount)", "function extend(uint32 term, uint64 out1e8, uint256 outEvm)",
                    "function endSeat(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)",
                    "function hitTarget(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)"):
            self.assertIn(sig, MULTI)
        for sig in ("function place(uint32 asset, bool isBuy, uint64 sz)", "function fill()", "function graduate()"):
            self.assertIn(sig, EXAM)
        self.assertIn("function stopFunded(Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)", POOL)
        self.assertIn("function settleFunded(Cancel[] calldata cancels, uint32[] calldata extraAssets)", POOL)


class Numbers(unittest.TestCase):
    def test_a_size_is_whole_steps_of_the_asset(self):
        self.assertEqual(r.size_units(0.0002, 5), 20)
        self.assertEqual(r.size_units(0.04, 4), 400)
        self.assertEqual(r.size_units(1, 0), 1)
        for size, decimals in ((0.000001, 5), (0.00025, 4), (0, 5), (-0.001, 5)):
            with self.assertRaises(SystemExit):
                r.size_units(size, decimals)

    def test_a_mark_carries_six_minus_the_size_decimals(self):
        self.assertEqual(r.mark_usd(764000, 5), 76400.0)
        self.assertEqual(r.mark_usd(241370, 4), 2413.70)
        self.assertEqual(r.mark_usd(150_123_400, 0), 150.1234)

    def test_days_become_the_contracts_seconds(self):
        self.assertEqual(r.seconds(0.0416666667), 3_600)
        self.assertEqual(r.seconds(180), 15_552_000)

    def test_the_gap_is_the_contracts_mark_over_the_exchanges(self):
        self.assertAlmostEqual(r.gap_bps(100.5, 100.0), 50.0)
        self.assertAlmostEqual(r.gap_bps(99.0, 100.0), -100.0)
        self.assertEqual(r.gap_bps(100.0, 100.0), 0.0)


class Receipts(unittest.TestCase):
    EXAM_ADDR = "0x00000000000000000000000000000000000000Aa"
    SEAT_ADDR = "0x00000000000000000000000000000000000000bB"

    def topic(self, addr: str) -> str:
        return "0x" + "0" * 24 + addr[2:].lower()

    def test_the_exam_and_the_seat_are_read_from_the_event(self):
        receipt = {"transactionHash": "0x1", "logs": [
            {"topics": ["0xfeed", self.topic(self.SEAT_ADDR)]},
            {"topics": [r.EXAM_SOLD, self.topic(self.EXAM_ADDR), self.topic(self.SEAT_ADDR)]},
            {"topics": [r.CLAIMED, self.topic(self.EXAM_ADDR), self.topic(self.EXAM_ADDR), self.topic(self.SEAT_ADDR)]},
        ]}
        self.assertEqual(r.indexed(receipt, r.EXAM_SOLD, 1).lower(), self.EXAM_ADDR.lower())
        self.assertEqual(r.indexed(receipt, r.CLAIMED, 3).lower(), self.SEAT_ADDR.lower())

    def test_no_event_or_two_is_a_refusal_not_a_guess(self):
        one = {"topics": [r.EXAM_SOLD, self.topic(self.EXAM_ADDR), self.topic(self.SEAT_ADDR)]}
        for logs in ([], [one, one]):
            with self.assertRaises(SystemExit):
                r.indexed({"transactionHash": "0x1", "logs": logs}, r.EXAM_SOLD, 1)


class Parser(unittest.TestCase):
    def test_every_step_names_who_signs_and_what_it_acts_on(self):
        p = r.build_parser()
        a = p.parse_args(["--label", "multi-1", "place", "--who", "trader", "--exam", "0xE", "--asset", "BTC",
                          "--side", "sell", "--size", "0.0002"])
        self.assertEqual((a.cmd, a.who, a.exam, a.asset, a.side, a.size, a.dry_run), ("place", "trader", "0xE", "BTC", "sell", 0.0002, False))
        a = p.parse_args(["--label", "multi-1", "--dry-run", "finish", "--who", "trader", "--seat", "0xS"])
        self.assertEqual((a.cmd, a.seat, a.dry_run), ("finish", "0xS", True))
        a = p.parse_args(["--label", "multi-1", "extend", "--who", "op", "--term-days", "0.5", "--usdc", "10"])
        self.assertEqual((a.cmd, a.term_days, a.usdc, a.usdc_evm), ("extend", 0.5, 10.0, 0.0))
        for bad in (["--label", "x", "buy", "--line", "0"],  # nobody signs
                    ["--label", "x", "open", "--who", "t"],  # no seat
                    ["--label", "x", "place", "--who", "t", "--exam", "0xE", "--asset", "DOGE", "--side", "buy", "--size", "1"],
                    ["status"]):  # no deployment named
            with self.assertRaises(SystemExit):
                p.parse_args(bad)

    def test_every_step_the_parser_takes_is_a_step_the_run_has(self):
        p = r.build_parser()
        steps = next(a for a in p._actions if a.dest == "cmd").choices
        for name in steps:
            self.assertTrue(callable(getattr(r.Run, name, None)), name)


if __name__ == "__main__":
    unittest.main()
