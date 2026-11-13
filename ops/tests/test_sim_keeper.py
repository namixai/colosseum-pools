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
        self.assertEqual(types, ["address", "address", "uint64", "uint64", "PayoutState", "bool", "uint64", "bool"])
        self.assertEqual(k.PAYOUT, "(address,address,uint64,uint64,uint8,bool,uint64,bool)")
        self.assertEqual(k.PAYOUT_STATE, ("None", "Due", "Stopped", "Paid", "Held", "Refused"))
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

    def test_pay_is_due_for_an_approved_payout_past_its_72_hours_or_any_past_its_7_days(self):
        due, hard = 1_000, 1_000 - k.PAYOUT_DELAY + k.PAYOUT_HARD
        self.assertFalse(k.pay_due([], 2_000), "nothing unsettled")
        self.assertFalse(k.pay_due([("Due", due, True)], due - 1))
        self.assertTrue(k.pay_due([("Due", due, True)], due))
        self.assertFalse(k.pay_due([("Due", due, False)], due), "not approved: it waits")
        self.assertFalse(k.pay_due([("Due", due, False)], hard - 1))
        self.assertTrue(k.pay_due([("Due", due, False)], hard), "7 days: it goes")
        self.assertTrue(k.pay_due([("Due", due, False), ("Due", due + 5, True)], due + 5), "pay walks past one waiting")
        self.assertTrue(k.pay_due([("Stopped", 999, False)], 100), "pay walks past a settled head")
        self.assertFalse(k.pay_due([("Due", due, False), ("Stopped", 0, False)], due), "a settled one behind changes nothing")

    def test_the_payout_times_are_the_factorys(self):
        src = (ROOT / "src/sim/SimPoolFactory.sol").read_text()
        self.assertIn("PAYOUT_DELAY = 72 hours;", src)
        self.assertIn("PAYOUT_HARD = 7 days;", src)
        self.assertEqual((k.PAYOUT_DELAY, k.PAYOUT_HARD), (72 * 3600, 7 * 86_400))

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
                mock.patch.object(sk, "fill", side_effect=lambda e: filled.append(e) or {"transactionHash": "0x01"}):
            self.assertEqual(sk.fast_step(8), [], "the window opens at 10; the next block is 9")
            self.assertEqual(rpc.call_args[0][1][0]["topics"], [[k.PLACED, k.EXAM_SOLD, k.FUNDED]])
            self.assertNotIn(k.to_checksum_address(self.OTHER), sk.orders, "its window ended at 5")
            rpc.return_value = []
            self.assertEqual(sk.fast_step(9), [k.to_checksum_address(self.EXAM)])
        self.assertEqual(filled, [k.to_checksum_address(self.EXAM)])
        self.assertEqual(sk.orders, {}, "sent once")
        self.assertEqual(sk.fast_block, 9)

    def test_a_refused_fill_is_sent_again_next_block_inside_the_window_once_a_block(self):
        sk = self.keeper()
        exam = k.to_checksum_address(self.EXAM)
        answers = [None, None, {"transactionHash": "0x01"}]
        sent = []
        with mock.patch.object(k.c, "rpc", return_value=[placed_log(self.EXAM, 10, 12, 9)]) as rpc, \
                mock.patch.object(sk, "is_filler", return_value=True), \
                mock.patch.object(sk, "fill", side_effect=lambda e: sent.append(e) or answers.pop(0)):
            self.assertEqual(sk.fast_step(9), [exam], "refused: the book past the ceiling, say")
            rpc.return_value = []
            self.assertEqual(sk.fast_step(9), [], "not twice for one block")
            self.assertEqual(sk.fast_step(10), [exam], "again for the next block")
            self.assertEqual(sk.fast_step(11), [exam], "and filled")
            self.assertEqual(sk.fast_step(12), [])
        self.assertEqual(len(sent), 3)
        self.assertEqual(sk.orders, {})

    def test_a_refused_fill_stops_with_its_window(self):
        sk = self.keeper()
        with mock.patch.object(k.c, "rpc", return_value=[placed_log(self.EXAM, 10, 11, 9)]) as rpc, \
                mock.patch.object(sk, "is_filler", return_value=True), \
                mock.patch.object(sk, "fill", return_value=None) as fill:
            sk.fast_step(9)
            rpc.return_value = []
            sk.fast_step(10)
            self.assertEqual(sk.fast_step(11), [], "the window ended at 11")
        self.assertEqual(fill.call_count, 2)
        self.assertEqual(sk.orders, {})

    def test_an_exam_bought_and_ordered_before_the_slow_pass_is_filled(self):
        sk = self.keeper()
        sk.done = set(sk.accounts)
        pool = k.to_checksum_address("0x" + "55" * 20)
        sk.pools = [pool]
        new = "0x" + "66" * 20
        sold = {"address": pool, "topics": [k.EXAM_SOLD, "0x" + "00" * 12 + "66" * 20], "data": "0x", "blockNumber": hex(8)}
        reads = [[sold], [placed_log(new, 9, 13, 8)]]
        filled = []
        with mock.patch.object(k.c, "rpc", side_effect=lambda m, a: reads.pop(0) if reads else []) as rpc, \
                mock.patch.object(sk, "fill", side_effect=lambda e: filled.append(e) or {"transactionHash": "0x01"}):
            self.assertEqual(sk.fast_step(8), [k.to_checksum_address(new)], "its window opens at the next block")
            self.assertEqual(rpc.call_args_list[0][0][1][0]["address"], [pool], "the pools are watched with nothing live")
            self.assertEqual(rpc.call_args_list[1][0][1][0]["address"], [k.to_checksum_address(new)])
        self.assertEqual(sk.accounts[k.to_checksum_address(new)], {"pool": pool, "kind": "exam"})
        self.assertEqual(filled, [k.to_checksum_address(new)])

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


class ThePayoutsClock(unittest.TestCase):
    """The payout is due PAYOUT_DELAY after `claimPayout`, so "up to 72 hours" holds only if the claim goes out in the
    pass that sees the stage passed, and `pay` in the first pass after the due time."""
    STAGE = k.to_checksum_address("0x" + "11" * 20)
    POOL = k.to_checksum_address("0x" + "55" * 20)

    def keeper(self) -> k.SimKeeper:
        import shutil
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        sk = k.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, False, pathlib.Path(tmp_dir) / "s.json", 1)
        sk.accounts = {self.STAGE: {"pool": self.POOL, "kind": "stage"}}
        return sk

    def test_a_passed_stage_is_claimed_in_the_pass_that_sees_it(self):
        sk = self.keeper()
        answers = {"accountOf(address)": (2, 1, False), "result()": (2, "0x" + "77" * 20, 10_000, 990)}
        with mock.patch.object(k.c, "call_view", side_effect=lambda to, sig, *a: answers[sig]), \
                mock.patch.object(sk, "send", return_value=True) as send:
            sk.account_pass(self.STAGE, 1_000)
        send.assert_called_once_with(self.POOL, "claimPayout(address)", ["address"], [self.STAGE])

    def test_pay_goes_out_in_the_first_pass_at_the_due_time(self):
        """Approved: at its 72 hours. Not approved: at its 7 days from the claim, and not before."""
        due = 1_000 + 72 * 3600
        for approved, at in ((True, due), (False, 1_000 + 7 * 86_400)):
            sk = self.keeper()
            payout = ("0x" + "11" * 20, "0x" + "77" * 20, 800, due, 1, False, 0, approved)

            def view(to, sig, out="uint256", *a):
                return {"head()": 0, "payoutCount()": 1}.get(sig, payout)

            with mock.patch.object(k, "view", side_effect=view), mock.patch.object(sk, "send", return_value=True) as send:
                sk.pool_pass(self.POOL, at - 1)
                send.assert_not_called()
                sk.pool_pass(self.POOL, at)
            send.assert_called_once_with(self.POOL, "pay(uint256)", ["uint256"], [k.PAY_PER_CALL])


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
