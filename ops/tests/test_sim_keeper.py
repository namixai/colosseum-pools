"""The keeper of simulated pools: what it judges due, the events it reads, and the fast loop's timing."""
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from eth_abi import encode
from eth_utils import keccak

from ops import sim_keeper as k

ROOT = pathlib.Path(__file__).resolve().parents[2]


def declared(path: str, name: str) -> str:
    """The topic of event `name` as `path` declares it, an enum read as a uint8."""
    text = (ROOT / path).read_text()
    m = re.search(rf"event {name}\(([^;]*?)\);", text, re.S)
    types = []
    for part in m.group(1).split(","):
        t = part.split()[0]
        types.append("uint8" if t[0].isupper() and t != "address" else t)
    return "0x" + keccak(text=f"{name}({','.join(types)})").hex()


class TheEvents(unittest.TestCase):
    def test_each_topic_is_the_contracts_own(self):
        self.assertEqual(k.EXAM_SOLD, declared("src/sim/SimPool.sol", "ExamSold"))
        self.assertEqual(k.FUNDED, declared("src/sim/SimPool.sol", "Funded"))
        self.assertEqual(k.PLACED, declared("src/exam/PaperExam.sol", "Placed"))
        self.assertEqual(k.FILLED, declared("src/exam/PaperExam.sol", "Filled"))
        self.assertEqual(k.LAPSED, declared("src/exam/PaperExam.sol", "Lapsed"))

    def test_the_payout_and_the_terms_read_as_the_contracts_lay_them_out(self):
        pool = (ROOT / "src/sim/SimPool.sol").read_text()
        fields = re.search(r"struct Payout \{(.*?)\}", pool, re.S).group(1)
        types = [l.split()[0] for l in fields.splitlines() if l.strip() and not l.strip().startswith("//")]
        self.assertEqual(types, ["address", "address", "uint64", "uint64", "PayoutState", "bool"])
        self.assertEqual(k.PAYOUT, "(address,address,uint64,uint64,uint8,bool)")
        self.assertEqual(k.PAYOUT_STATE, ("None", "Due", "Stopped", "Paid", "Held"))
        states = re.search(r"enum PayoutState \{(.*?)\}", pool, re.S).group(1)
        self.assertEqual(tuple(w for w in re.findall(r"^\s*(\w+),?$", states, re.M)), k.PAYOUT_STATE)


class WhatIsDue(unittest.TestCase):
    def test_a_running_account(self):
        self.assertEqual(k.account_calls("exam", "Active", 100), [])
        self.assertEqual(k.account_calls("exam", "Active", 100, broken=True, snapshot_due=True), ["breach"])
        self.assertEqual(k.account_calls("stage", "Active", 100, snapshot_due=True), ["checkpoint"])
        self.assertEqual(k.account_calls("exam", "Active", 100, over=True), ["expire"])
        self.assertEqual(k.account_calls("exam", "Active", 100, over=True, snapshot_due=True), ["checkpoint", "expire"])

    def test_an_account_that_ended(self):
        for kind in ("exam", "stage"):
            for status in ("Breached", "Expired"):
                self.assertEqual(k.account_calls(kind, status, 100), ["release"])

    def test_a_pass(self):
        self.assertEqual(k.account_calls("stage", "Passed", 100), ["claimPayout"])
        # An exam's pass waits for its trader's fund through the window, then is released.
        self.assertEqual(k.account_calls("exam", "Passed", 60, passed_at=10, fund_window=50), [])
        self.assertEqual(k.account_calls("exam", "Passed", 61, passed_at=10, fund_window=50), ["release"])

    def test_pay_is_due_once_the_first_unsettled_payouts_time_has_come(self):
        self.assertFalse(k.pay_due(3, 3, "Due", 0, 100), "nothing unsettled")
        self.assertFalse(k.pay_due(0, 1, "Due", 101, 100))
        self.assertTrue(k.pay_due(0, 1, "Due", 100, 100))
        self.assertTrue(k.pay_due(0, 2, "Stopped", 999, 100), "pay walks past a settled one")

    def test_a_fill_goes_out_for_the_next_block_inside_the_window(self):
        self.assertFalse(k.fill_now(10, 14, 8), "the next block is 9, before the window")
        self.assertTrue(k.fill_now(10, 14, 9))
        self.assertTrue(k.fill_now(10, 14, 13))
        self.assertFalse(k.fill_now(10, 14, 14), "the next block is 15, past it")

    def test_quantiles(self):
        self.assertEqual(k.quantiles([]), {"n": 0})
        self.assertEqual(k.quantiles([3, 1, 2, 2]), {"n": 4, "p50": 2, "p99": 3, "max": 3})
        self.assertEqual(k.quantiles(list(range(100)))["p99"], 99)


def placed_log(exam: str, start: int, until: int, block: int) -> dict:
    data = encode(["bool", "uint64", "uint256", "uint256"], [True, 100, start, until])
    return {"address": exam, "topics": [k.PLACED, "0x" + "00" * 32], "data": "0x" + data.hex(), "blockNumber": hex(block)}


class TheLogs(unittest.TestCase):
    def test_placed_and_filled_decode(self):
        exam = "0x" + "ab" * 20
        self.assertEqual(k.placed_of(placed_log(exam, 10, 14, 9))[1:], (10, 14))
        data = encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"], [False, 5, 7, 0, 3, 1])
        self.assertEqual(k.filled_of({"data": "0x" + data.hex()}), (3, 1))


class TheFastLoop(unittest.TestCase):
    EXAM = "0x" + "11" * 20
    OTHER = "0x" + "22" * 20

    def keeper(self) -> k.SimKeeper:
        tmp = pathlib.Path(tempfile.mkdtemp()) / "s.json"
        sk = k.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, False, tmp, 1)
        sk.accounts = {k.to_checksum_address(self.EXAM): {"pool": "0x" + "55" * 20, "kind": "exam"},
                       k.to_checksum_address(self.OTHER): {"pool": "0x" + "55" * 20, "kind": "exam"}}
        return sk

    def test_an_order_is_filled_for_the_block_its_window_opens_and_a_lapsed_one_is_dropped(self):
        sk = self.keeper()
        logs = [placed_log(self.EXAM, 10, 14, 9), placed_log(self.OTHER, 3, 5, 2)]
        filled = []
        with mock.patch.object(k.c, "rpc", return_value=logs) as rpc, \
                mock.patch.object(sk, "fill", side_effect=lambda e: filled.append(e)):
            self.assertEqual(sk.fast_step(8), [], "the window opens at 10; the next block is 9")
            self.assertEqual(rpc.call_args[0][1][0]["topics"], [k.PLACED])
            self.assertNotIn(k.to_checksum_address(self.OTHER), sk.orders, "its window ended at 5")
            rpc.return_value = []
            self.assertEqual(sk.fast_step(9), [k.to_checksum_address(self.EXAM)])
        self.assertEqual(filled, [k.to_checksum_address(self.EXAM)])
        self.assertEqual(sk.orders, {}, "sent once")
        self.assertEqual(sk.fast_block, 9)

    def test_nothing_live_reads_nothing(self):
        sk = self.keeper()
        sk.done = set(sk.accounts)
        with mock.patch.object(k.c, "rpc") as rpc:
            self.assertEqual(sk.fast_step(50), [])
        rpc.assert_not_called()
        self.assertEqual(sk.fast_block, 50)

    def test_a_keeper_that_is_not_the_filler_sends_no_fill(self):
        sk = self.keeper()
        with mock.patch.object(sk, "is_filler", return_value=False), mock.patch.object(k.c, "send_tx") as send:
            self.assertIsNone(sk.fill(self.EXAM))
        send.assert_not_called()

    def test_the_state_comes_back(self):
        sk = self.keeper()
        sk.pools = [k.to_checksum_address("0x" + "55" * 20)]
        sk.done = {k.to_checksum_address(self.OTHER)}
        sk.next_block = 77
        sk.save()
        back = k.SimKeeper(sk.factory, None, sk.sender, False, sk.state_path, 1)
        self.assertEqual((back.next_block, back.pools, back.accounts, back.done),
                         (77, sk.pools, sk.accounts, sk.done))


class TheHostsOneUnit(unittest.TestCase):
    def test_multi_keeper_hands_a_simulated_record_to_this_keeper(self):
        from ops import multi_keeper
        argv = ["multi_keeper", "--deployment", "testnet-sim1", "--once", "--dry-run"]
        with mock.patch.object(multi_keeper.c, "assert_testnet"), \
                mock.patch.object(multi_keeper.deployments, "load", return_value={"SimPoolFactory": "0x" + "33" * 20, "block": 1}), \
                mock.patch.object(k, "main", return_value=0) as sim, mock.patch("sys.argv", argv):
            self.assertEqual(multi_keeper.main(), 0)
        sim.assert_called_once_with(argv[1:])


if __name__ == "__main__":
    unittest.main()
