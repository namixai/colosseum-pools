"""The keeper of simulated pools: what it judges due, the events it reads, and the fast loop's timing."""
import pathlib
import queue
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

from eth_abi import decode, encode
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
        # How far pay is told to look: to the last payable one, past settled ones paid out of order (review of #286).
        rows = [("Due", due, False)] + [("Paid", due, True)] * 20 + [("Due", due + 5, True)]
        self.assertEqual(k.pay_reach(rows, due + 5), 22, "past twenty payouts paid out of order")
        self.assertEqual(k.pay_reach([("Paid", 0, True), ("Held", 0, True), ("Due", due, False)], due), 2,
                         "a settled head: past the settled ones")
        self.assertEqual(k.pay_reach([("Due", due, False)], due), 0)
        # At most PAY_PER_CALL paid in one call: the day's two (review of #286, and the daily limit).
        many = [("Due", due, True)] * (k.PAY_PER_CALL + 5)
        self.assertEqual(k.pay_reach(many, due), k.PAY_PER_CALL)
        # What the daily limit leaves room for, and no further: one, or none.
        self.assertEqual(k.pay_reach(many, due, 1), 1)
        self.assertEqual(k.pay_reach(many, due, 0), 0, "no room today: no call")
        self.assertEqual(k.pay_reach([("Paid", 0, True), ("Due", due, True)], due, 0), 1,
                         "no room, but a settled head is still passed")
        self.assertEqual(k.pay_reach([("Due", due, False), ("Due", due, True), ("Due", due, True)], due, 1), 2,
                         "past the waiting head to the one payout the day has room for")

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
            self.assertEqual(rpc.call_args[0][1][0]["topics"], [[k.PLACED, k.EXAM_SOLD, k.FUNDED, *k.TRIGGER_TOPICS]])
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
                return {"head()": 0, "payoutCount()": 1, "payRoom()": 2}.get(sig, payout)

            with mock.patch.object(k, "view", side_effect=view), mock.patch.object(sk, "send", return_value=True) as send:
                sk.pool_pass(self.POOL, at - 1)
                send.assert_not_called()
                sk.pool_pass(self.POOL, at)
            send.assert_called_once_with(self.POOL, "pay(uint256)", ["uint256"], [1])


class TheLongQueue(unittest.TestCase):
    """More payouts wait for their approval in front of an approved one than the keeper used to read (review of
    #286), and the daily limit. The pool here is a model of `SimPool.pay` with money for everything: the head,
    `max` counting every payout looked at, a payout waiting for its approval passed over and keeping the head,
    and at most two payouts settled in any 24 hours."""
    DAY = 86_400
    POOL = k.to_checksum_address("0x" + "55" * 20)
    CLAIMED = 1_000
    DUE = CLAIMED + k.PAYOUT_DELAY
    HARD = CLAIMED + k.PAYOUT_HARD

    def setUp(self):
        import shutil
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        self.sk = k.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, False, pathlib.Path(tmp_dir) / "s.json", 1)
        self.head = 0
        self.payouts: list[list] = []  # [state, due_at, approved]
        self.now = self.DUE
        self.calls: list[tuple[int, int]] = []  # (max, paid) of each `pay`
        self.reads = 0
        self.settled: list[int] = [0, 0]  # when the last two payouts were settled, the oldest first
        self.paid_at: list[int] = []

    def queue(self, waiting: int, approved_after: int = 1) -> None:
        self.payouts = [["Due", self.DUE, False] for _ in range(waiting)]
        self.payouts += [["Due", self.DUE, True] for _ in range(approved_after)]

    def view(self, to, sig, out="uint256", types=(), args=()):
        if sig == "head()":
            return self.head
        if sig == "payoutCount()":
            return len(self.payouts)
        if sig == "payRoom()":
            return sum(1 for t in self.settled if self.now >= t + self.DAY)
        if sig == "nextPayAt()":
            return self.settled[0] + self.DAY if self.settled[0] else 0
        self.reads += 1
        state, due_at, approved = self.payouts[args[0]]
        return ("0x" + "11" * 20, "0x" + "77" * 20, 800, due_at, k.PAYOUT_STATE.index(state), False, 0, approved)

    def pay(self, to, sig, types, args) -> bool:
        """SimPool.pay, with the pool holding enough for every payout."""
        self.assertEqual(sig, "pay(uint256)")
        i, seen, paid, waiting = self.head, 0, 0, None
        while i < len(self.payouts) and seen < args[0]:
            p = self.payouts[i]
            seen += 1
            if p[0] == "Due":
                if self.now < p[1]:
                    break
                if not p[2] and self.now < p[1] - k.PAYOUT_DELAY + k.PAYOUT_HARD:
                    waiting = i if waiting is None else waiting
                    i += 1
                    continue
                if self.now < self.settled[0] + self.DAY:
                    break  # PayoutWaitsLimit
                self.settled = [self.settled[1], self.now]
                self.paid_at.append(self.now)
                p[0] = "Paid"
                paid += 1
            i += 1
        self.head = waiting if waiting is not None and waiting < i else i
        self.calls.append((args[0], paid))
        return True

    def passes(self, n: int = 1) -> list:
        logged = []
        with mock.patch.object(k, "view", side_effect=self.view), mock.patch.object(self.sk, "send", side_effect=self.pay), \
                mock.patch.object(k, "log", side_effect=lambda what, **kw: logged.append((what, kw))):
            for _ in range(n):
                self.sk.pool_pass(self.POOL, self.now)
        return logged

    def test_the_keepers_numbers_are_the_ones_the_gas_was_measured_with(self):
        src = (ROOT / "test/sim/SimPool.t.sol").read_text()
        self.assertIn(f"uint256 constant KEEPER_PAYS = {k.PAY_PER_CALL};", src)
        self.assertIn(f"uint256 constant KEEPER_SCAN = {k.SCAN_PAYOUTS};", src)
        self.assertIn("function test_pay_theKeepersFullBatch_fitsASmallBlock()", src)
        self.assertIn('assertLt(worst, 1_300_000, "pay(192) with the day\'s two payments");', src)
        # One call pays at most PAY_PER_CALL, and never more than the pool's room under its own daily limit.
        pool = (ROOT / "src/sim/SimPool.sol").read_text()
        self.assertIn("uint8 public payoutsPerDay;", pool)
        self.assertIn("for (uint256 k = 0; k < payoutsPerDay; ++k) {", pool)
        self.assertIn('room = min(PAY_PER_CALL, view(pool, "payRoom()", "uint256"))',
                      (ROOT / "ops/sim_keeper.py").read_text())
        self.assertIn("event PayoutWaitsLimit(uint256 indexed id, uint64 nextAt);", pool)

    def test_an_approved_payout_behind_96_waiting_is_paid_in_the_first_pass(self):
        self.queue(96)
        self.assertEqual(self.passes(), [])
        self.assertEqual(self.calls, [(97, 1)], "pay is told to look past the 96")
        self.assertEqual(self.payouts[96][0], "Paid")
        self.assertEqual(self.head, 0, "the head still waits for its approval")

    def test_reading_stops_one_past_the_payouts_the_day_has_room_for(self):
        self.queue(3, approved_after=40)
        logged = self.passes()
        self.assertEqual(self.reads, 3 + k.PAY_PER_CALL + 1, "the third payable one says that one waits")
        self.assertEqual(self.calls, [(3 + k.PAY_PER_CALL, k.PAY_PER_CALL)])
        self.assertEqual(logged, [], "there was room: the wait is said in the pass that finds none")

    def test_a_wave_of_five_goes_out_two_two_one_and_no_pay_is_sent_while_the_limit_holds(self):
        self.queue(0, approved_after=5)
        t0 = self.now
        logged = self.passes(3)  # three passes in the same half minute
        self.assertEqual(self.calls, [(2, 2)], "one call, the day's two; then none while the limit holds")
        self.assertEqual([(what, kw["room"], kw["next_at"]) for what, kw in logged],
                         [("pay_waits_limit", 0, t0 + self.DAY)],
                         "said once, with the time the third waits for, not every pass and not while there was room")
        self.now = t0 + self.DAY - 1
        self.passes()
        self.assertEqual(len(self.calls), 1, "a second before the day is over: no call")
        self.now = t0 + self.DAY
        self.passes(2)
        self.now = t0 + 2 * self.DAY
        self.passes(2)
        self.assertEqual(self.calls, [(2, 2), (2, 2), (1, 1)])
        self.assertEqual(self.paid_at, [t0, t0, t0 + self.DAY, t0 + self.DAY, t0 + 2 * self.DAY])
        self.assertTrue(all(p[0] == "Paid" for p in self.payouts))

    def test_with_room_for_one_the_keeper_asks_for_one(self):
        self.queue(0, approved_after=3)
        self.settled = [self.now - self.DAY, self.now - 3_600]  # one of the day's two went out an hour ago
        self.passes()
        self.assertEqual(self.calls, [(1, 1)])

    def test_with_no_room_a_settled_head_is_still_passed(self):
        self.queue(0, approved_after=2)
        self.payouts.insert(0, ["Stopped", self.DUE, False])
        self.settled = [self.now - 60, self.now - 30]
        self.passes()
        self.assertEqual((self.calls, self.head), ([(1, 0)], 1), "the head moves; nothing is paid")

    def test_one_behind_more_than_a_call_can_look_past_is_named_and_is_paid_once_the_ones_in_front_are_approved(self):
        self.queue(k.SCAN_PAYOUTS)
        far = k.SCAN_PAYOUTS
        logged = self.passes()
        self.assertEqual(self.calls, [], "no call: nothing in reach may be paid")
        self.assertEqual(logged, [("pay_out_of_reach", {"pool": self.POOL, "head": 0, "looked": k.SCAN_PAYOUTS, "beyond": 1})])
        # The approver approves the first of the queue: it is paid, the head moves, the far one is in reach.
        self.payouts[0][2] = True
        self.assertEqual(self.passes(), [])
        self.assertEqual((self.calls, self.head), ([(1, 1)], 1))
        self.passes()
        self.assertEqual(self.calls[-1], (k.SCAN_PAYOUTS, 1))
        self.assertEqual(self.payouts[far][0], "Paid")

    def test_with_no_approval_at_all_it_is_paid_in_its_turn_once_the_7_days_of_the_ones_in_front_come_two_a_day(self):
        self.queue(k.SCAN_PAYOUTS)
        far = k.SCAN_PAYOUTS
        self.now = self.HARD - 1
        self.assertEqual([what for what, _ in self.passes()], ["pay_out_of_reach"])
        self.now = self.HARD
        needed = -(-(far + 1) // k.PAY_PER_CALL)  # the days a queue of 193 takes, two a day
        for _ in range(needed - 1):
            self.passes()
            self.now += self.DAY
        self.assertEqual(self.payouts[far][0], "Due")
        self.passes()
        self.assertEqual(self.payouts[far][0], "Paid", "on the day its turn comes, the 97th for 193 payouts")
        self.assertEqual(len(self.calls), needed)
        self.assertTrue(all(looked <= k.SCAN_PAYOUTS and paid <= k.PAY_PER_CALL for looked, paid in self.calls),
                        "every call within what was measured to fit a small block")

    def test_a_queue_whose_72_hours_are_not_over_is_not_out_of_reach(self):
        self.queue(k.SCAN_PAYOUTS + 5)
        self.now = self.DUE - 1
        self.assertEqual(self.passes(), [])
        self.assertEqual(self.reads, 1, "nothing behind a payout not yet due is read")


def trigger_log(exam: str, topic: str, asset: int, is_take: bool, armed_at: int = 0) -> dict:
    if topic == k.TRIGGER_SET:
        data = encode(["bool", "uint64", "uint64", "uint256"], [is_take, 950_000, 0, armed_at])
    elif topic == k.TRIGGER_CLEARED:
        data = encode(["bool"], [is_take])
    else:
        data = encode(["bool", "uint64", "uint64", "uint256", "uint64", "uint8"], [is_take, 100, 940_000, 0, 950_000, 0])
    return {"address": exam, "topics": [topic, "0x" + asset.to_bytes(32, "big").hex()], "data": "0x" + data.hex(),
            "blockNumber": hex(1)}


class TheTriggers(unittest.TestCase):
    """The keeper carries out a stop or a take once the exam would take it: armed, and the oracle reached."""
    EXAM = k.to_checksum_address("0x" + "11" * 20)

    def keeper(self) -> k.SimKeeper:
        import shutil
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        sk = k.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, False, pathlib.Path(tmp_dir) / "s.json", 1)
        sk.accounts = {self.EXAM: {"pool": "0x" + "55" * 20, "kind": "exam"}}
        return sk

    def test_the_topics_are_the_contracts_events(self):
        src = (ROOT / "src/exam/PaperExam.sol").read_text()
        self.assertIn("event TriggerSet(uint32 indexed asset, bool isTake, uint64 px, uint64 sz, uint256 armedAt);", src)
        self.assertIn("event TriggerCleared(uint32 indexed asset, bool isTake);", src)
        self.assertIn("uint32 indexed asset, bool isTake, uint64 sz, uint64 px, uint256 cost, uint64 triggerPx, uint8 source", src)

    def test_set_then_cleared_or_filled_is_forgotten(self):
        sk = self.keeper()
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_SET, 3, False, armed_at=12))
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_SET, 3, True, armed_at=14))
        self.assertEqual(sk.triggers, {(self.EXAM, 3, False): 12, (self.EXAM, 3, True): 14})
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_CLEARED, 3, False))
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_FILLED, 3, True))
        self.assertEqual(sk.triggers, {})

    def test_one_armed_and_reached_is_sent_once_a_block_and_one_not_reached_is_not(self):
        sk = self.keeper()
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_SET, 3, False, armed_at=12))
        reached = {"yes": False}

        def rpc(method, args):
            self.assertEqual(method, "eth_call")
            if not reached["yes"]:
                raise RuntimeError("NotTriggered")
            return "0x"

        with mock.patch.object(k.c, "rpc", side_effect=rpc) as call, mock.patch.object(sk, "is_filler", return_value=True), \
                mock.patch.object(k.c, "send_tx", return_value={"transactionHash": "0x01", "blockNumber": hex(12)}) as send:
            self.assertEqual(sk.trigger_step(10), [], "not armed for block 11")
            call.assert_not_called()
            self.assertEqual(sk.trigger_step(11), [], "armed, not reached")
            reached["yes"] = True
            self.assertEqual(sk.trigger_step(11), [], "once a block")
            self.assertEqual(sk.trigger_step(12), [self.EXAM])
        self.assertEqual(send.call_args[0][1], self.EXAM)
        self.assertTrue(send.call_args[0][2].startswith(k.c.encode_call("fillTrigger(uint32,bool)", ["uint32", "bool"], [3, False])[:4]))

    def test_a_keeper_that_is_not_the_filler_sends_nothing(self):
        sk = self.keeper()
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_SET, 3, True, armed_at=1))
        with mock.patch.object(k.c, "rpc", return_value="0x"), mock.patch.object(sk, "is_filler", return_value=False), \
                mock.patch.object(k.c, "send_tx") as send:
            self.assertEqual(sk.trigger_step(5), [self.EXAM])
        send.assert_not_called()

    def test_a_keeper_started_again_reads_the_triggers_from_the_exam(self):
        sk = self.keeper()  # nothing from the logs: it was down when they were set
        state = {("stopOf(uint32)", 3): (950_000, 0, 12, 0), ("takeOf(uint32)", 3): (0, 0, 0, 0),
                 ("stopOf(uint32)", 4): (0, 0, 0, 0), ("takeOf(uint32)", 4): (2_600_000, 10, 15, 0)}

        def view(to, sig, out="uint256", types=(), args=()):
            self.assertEqual(to, self.EXAM)
            if sig == "rules()":
                return (300, 600, 1000, [3, 4], [])
            return state[(sig, args[0])]

        with mock.patch.object(k, "view", side_effect=view):
            sk.refresh_triggers(self.EXAM)
        self.assertEqual(sk.triggers, {(self.EXAM, 3, False): 12, (self.EXAM, 4, True): 15})
        state[("stopOf(uint32)", 3)] = (0, 0, 0, 0)  # carried out by someone else meanwhile
        with mock.patch.object(k, "view", side_effect=view):
            sk.refresh_triggers(self.EXAM)
        self.assertEqual(sk.triggers, {(self.EXAM, 4, True): 15})

    def test_a_running_exams_triggers_are_read_in_the_slow_pass(self):
        sk = self.keeper()
        answers = {"accountOf(address)": (1, 0, False), "result()": (1, "0x" + "77" * 20, 10_000, 0)}
        reads = {"violation()": 0, "deadline()": 0, "lastFillAt()": 0, "day()": 10**9, "fundWindow()": 604_800}

        def view(to, sig, out="uint256", *a):
            return (0, [], 0, 0, 0, 0, 1, 1) if sig == "terms()" else reads[sig]

        with mock.patch.object(k.c, "call_view", side_effect=lambda to, sig, *a: answers[sig]), \
                mock.patch.object(k, "view", side_effect=view), mock.patch.object(sk, "refresh_triggers") as refresh, \
                mock.patch.object(sk, "send", return_value=True):
            sk.account_pass(self.EXAM, 1_000)
        refresh.assert_called_once_with(self.EXAM)

    def test_the_getters_read_are_the_contracts(self):
        src = (ROOT / "src/exam/PaperExam.sol").read_text()
        self.assertIn("mapping(uint32 asset => Trigger) public stopOf;", src)
        self.assertIn("mapping(uint32 asset => Trigger) public takeOf;", src)
        self.assertIn("struct Trigger {\n        uint64 px;\n        uint64 sz;\n        uint64 armedAt;\n        uint64 seenAt;\n    }", src)

    def test_an_exam_that_is_done_drops_its_triggers(self):
        sk = self.keeper()
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_SET, 3, True, armed_at=1))
        sk.done.add(self.EXAM)
        with mock.patch.object(k.c, "rpc") as call:
            self.assertEqual(sk.trigger_step(5), [])
        call.assert_not_called()
        self.assertEqual(sk.triggers, {})


class NoKeyOfTheNodeInALine(unittest.TestCase):
    """A node's address may hold its key, and requests writes the whole address into its errors: the keeper's lines of
    a call the chain refused or that failed on the way carry none of it (ops/sim_check.py, `quiet`)."""
    EXAM = k.to_checksum_address("0x" + "11" * 20)

    def keeper(self) -> k.SimKeeper:
        import shutil
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        sk = k.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, False, pathlib.Path(tmp_dir) / "s.json", 1)
        sk.accounts = {self.EXAM: {"pool": "0x" + "55" * 20, "kind": "exam"}}
        return sk

    def test_a_call_refused_or_failed_is_said_without_the_nodes_address(self):
        """The four lines: a slow call not due, a slow call that failed, a fill that failed, a stop's fill that failed."""
        import json
        failed = RuntimeError("403 Client Error: Forbidden for url: https://rpc.example.org/v2/SECRETKEY123")
        sk = self.keeper()
        sk._trigger(trigger_log(self.EXAM, k.TRIGGER_SET, 3, False, armed_at=1))
        lines = []
        with mock.patch.object(k, "log", side_effect=lambda event, **f: lines.append({"event": event, **f})), \
                mock.patch.object(sk, "is_filler", return_value=True):
            with mock.patch.object(k.c, "rpc", side_effect=failed):
                self.assertFalse(sk.send(self.EXAM, "release()"))
            with mock.patch.object(k.c, "rpc", return_value="0x5208"), mock.patch.object(k.c, "send_tx", side_effect=failed):
                self.assertFalse(sk.send(self.EXAM, "release()"))
                self.assertIsNone(sk.fill(self.EXAM))
                self.assertEqual(sk.trigger_step(5), [self.EXAM])
        self.assertEqual([l["event"] for l in lines], ["not_due", "send_failed", "fill_failed", "trigger_fill_failed"])
        for line in lines:
            said = line.get("error") or line.get("reason")
            self.assertEqual(said, "403 Client Error: Forbidden for url: <node>", line["event"])
        self.assertNotIn("SECRETKEY123", json.dumps(lines))
        self.assertNotIn("rpc.example.org", json.dumps(lines))

    def test_an_address_that_goes_on_past_the_configured_one_is_not_left_in_part(self):
        import os
        base = "https://rpc.example.org/v2"
        with mock.patch.object(k.c, "RPC_URL", base), mock.patch.dict(os.environ, {"COLOSSEUM_RPC_URL": base}):
            said = k.refused(RuntimeError(f"403 Client Error: Forbidden for url: {base}/SECRETKEY123"))
        self.assertEqual(said, "403 Client Error: Forbidden for url: <node>")

    def test_the_address_goes_out_of_the_whole_text_before_it_is_cut_and_a_refusal_keeps_its_name(self):
        """`refusal` cuts the node's words to 200. Cut first, a host name that is the node's key stands there in part,
        `host='KEY.prov`, with no end for any rule to know it by. And what the contract itself refused with is still
        said by its name, whatever address stands around it."""
        import json
        import os
        from ops import contract_errors as ce
        url = "https://SECRETKEY123.provider.example/v2"
        sk = self.keeper()
        lines = []
        with mock.patch.object(k.c, "RPC_URL", url), mock.patch.dict(os.environ, {"COLOSSEUM_RPC_URL": url}), \
                mock.patch.object(k, "log", side_effect=lambda event, **f: lines.append({"event": event, **f})), \
                mock.patch.object(sk, "is_filler", return_value=True):
            for lead in range(150, 200):
                text = "x" * lead + " HTTPSConnectionPool(host='SECRETKEY123.provider.example', port=443): Max retries exceeded"
                with mock.patch.object(k.c, "send_tx", side_effect=RuntimeError(text)):
                    self.assertIsNone(sk.fill(self.EXAM))
            self.assertEqual(len(lines), 50)
            self.assertNotIn("SECRETKEY123", json.dumps(lines))
            self.assertTrue(all(len(l["error"]) <= 200 for l in lines))
            sel, sig = next((sel, sig) for sel, sig in ce.BY_SELECTOR.items() if sig.endswith("()"))
            said = RuntimeError(f"{{'code': 3, 'message': 'execution reverted', 'data': '{sel}'}} for url: {url}/SECRETKEY123")
            with mock.patch.object(k.c, "rpc", side_effect=said):
                self.assertFalse(sk.send(self.EXAM, "release()"))
        self.assertEqual((lines[-1]["event"], lines[-1]["reason"]), ("not_due", sig))


def sel(sig: str) -> str:
    return "0x" + keccak(text=sig)[:4].hex()


class Chain:
    """A chain as the keeper's slow pass reads it through c.rpc: Multicall3, a factory with one pool, and exams."""
    FACTORY = k.to_checksum_address("0x" + "33" * 20)
    POOL = k.to_checksum_address("0x" + "55" * 20)

    def __init__(self, n: int, assets=(3, 4, 0), ahead: int = 0, multicall: bool = True):
        self.head = 500
        self.ahead = ahead
        self.multicall = multicall
        self.assets = list(assets)
        self.exams = {k.to_checksum_address("0x" + f"{0xA000 + i:040x}"): {
            "status": 1, "violation": 0, "deadline": 0, "last_fill": 0, "max_idle": 0, "day": 10**9, "passed_at": 0,
            "done": False, "stops": {}, "positions": {}} for i in range(n)}
        self.calls: list[tuple[str, object]] = []
        self.alone: list[tuple[str, str]] = []   # (to, selector) of each read asked alone
        self.batches: list[list[tuple[str, str]]] = []  # the reads of each batch, the block's two left out
        self.fails: set[tuple[str, str]] = set()  # (exam, signature) whose read fails, alone or in a batch
        self.fails_in_batch: dict[tuple[str, str], int] = {}  # (exam, signature) -> the batch read it fails from (1: the first)
        self.asked_in_batch: dict[tuple[str, str], int] = {}
        self.burns = False  # a failing read takes the whole batch with it (it burned the call's gas)
        self.moves = False  # the head goes on by one after each batch
        self.logs: list[dict] = []

    def now(self, block=None) -> int:
        return 1_000_000 + (self.head if block is None else block)

    def read(self, to: str, data: str) -> str:
        to = k.to_checksum_address(to)
        head = data[:10]
        if to == self.FACTORY and head == sel("pools()"):
            return "0x" + encode(["address[]"], [[self.POOL]]).hex()
        if to == self.POOL:
            if head == sel("accountOf(address)"):
                a = k.to_checksum_address(decode(["address"], bytes.fromhex(data[10:]))[0])
                return "0x" + encode(["uint8", "uint8", "bool"], [1, 0, self.exams[a]["done"]]).hex()
            answers = {sel("fundWindow()"): ("uint32", 3_600), sel("head()"): ("uint256", 0),
                       sel("payoutCount()"): ("uint256", 0), sel("payRoom()"): ("uint256", 2)}
            kind, value = answers[head]
            return "0x" + encode([kind], [value]).hex()
        e = self.exams[to]
        for sig in ("result()", "violation()", "deadline()", "lastFillAt()", "terms()", "day()", "rules()",
                    "stopOf(uint32)", "takeOf(uint32)", "positionOf(uint32)"):
            if head == sel(sig) and (to, sig) in self.fails:
                raise RuntimeError("eth_call: execution reverted")
        if head == sel("result()"):
            return "0x" + encode(["uint8", "address", "uint64", "uint64"], [e["status"], "0x" + "77" * 20, 10_000, e["passed_at"]]).hex()
        if head == sel("violation()"):
            return "0x" + encode(["uint8"], [e["violation"]]).hex()
        if head == sel("deadline()"):
            return "0x" + encode(["uint64"], [e["deadline"]]).hex()
        if head == sel("lastFillAt()"):
            return "0x" + encode(["uint64"], [e["last_fill"]]).hex()
        if head == sel("terms()"):
            return "0x" + encode([k.EXAM_TERMS], [(0, [], 0, e["max_idle"], 0, 0, 1, 1)]).hex()
        if head == sel("day()"):
            return "0x" + encode(["uint32"], [e["day"]]).hex()
        if head == sel("rules()"):
            return "0x" + encode([k.RULES], [(300, 600, 1000, self.assets, [])]).hex()
        asset = decode(["uint32"], bytes.fromhex(data[10:]))[0]
        if head == sel("positionOf(uint32)"):
            return "0x" + encode(["int256"], [e["positions"].get(asset, 0)]).hex()
        px, armed = e["stops"].get((asset, head == sel("takeOf(uint32)")), (0, 0))
        return "0x" + encode(["uint64", "uint64", "uint64", "uint64"], [px, 0, armed, 0]).hex()

    def rpc(self, method, params=()):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            block = self.head if params[0] == "latest" else int(params[0], 16)
            return {"number": hex(block), "timestamp": hex(self.now(block))}
        if method == "eth_getLogs":
            q = params[0]
            return [l for l in self.logs if int(q["fromBlock"], 16) <= int(l["blockNumber"], 16) <= int(q["toBlock"], 16)]
        if method == "eth_getCode":
            return "0x" + "60" * 3808 if self.multicall else "0x"
        if method != "eth_call":
            raise AssertionError(f"the keeper asked the node for {method}")
        q, tag = params
        if q["to"].lower() != k.MULTICALL3.lower():
            self.alone.append((k.to_checksum_address(q["to"]), q["data"][:10]))
            return self.read(q["to"], q["data"])
        if not self.multicall or tag != "latest":
            raise AssertionError("a batch on a chain with no Multicall3, or not at the head")
        inner = decode(["(address,bool,bytes)[]"], bytes.fromhex(q["data"][10:]))[0]
        self.batches.append([(k.to_checksum_address(to), "0x" + data.hex()[:8]) for to, _, data in inner[2:]])
        out = [(True, encode(["uint256"], [self.head + self.ahead])), (True, encode(["uint256"], [self.now()]))]
        for to, may_fail, data in inner[2:]:
            try:
                for (exam, sig), nth in self.fails_in_batch.items():
                    if k.to_checksum_address(to) == exam and "0x" + data.hex()[:8] == sel(sig):
                        self.asked_in_batch[(exam, sig)] = self.asked_in_batch.get((exam, sig), 0) + 1
                        if self.asked_in_batch[(exam, sig)] >= nth:
                            raise RuntimeError("eth_call: execution reverted")
                out.append((True, bytes.fromhex(self.read(to, "0x" + data.hex())[2:])))
            except RuntimeError:
                if self.burns:
                    raise RuntimeError("eth_call: {'code': -32000, 'message': 'out of gas'}") from None
                out.append((False, b""))
        if self.moves and len(inner) > 2:
            self.head += 1
        return "0x" + encode(["(bool,bytes)[]"], [out]).hex()


class TheBatchedPass(unittest.TestCase):
    """The slow pass asks its reads in batches of at most a hundred (Multicall3): the requests of a pass go with the
    reads by the hundred, not with the accounts by seventeen. The answers are the same and are read the same way."""

    def keeper(self, chain: Chain, loop=None) -> k.SimKeeper:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sk = k.SimKeeper(Chain.FACTORY, None, "0x" + "44" * 20, False, tmp / "s.json", chain.head)
        sk.pools = [Chain.POOL]
        sk.accounts = {a: {"pool": Chain.POOL, "kind": "exam"} for a in chain.exams}
        sk.multicall = chain.multicall
        sk.stop_loop = loop
        return sk

    def passes(self, sk: k.SimKeeper, chain: Chain, n: int = 1):
        """`n` slow passes on `chain`; what was sent (to, call) and what was logged."""
        sent, said = [], []
        with mock.patch.object(k.c, "rpc", side_effect=chain.rpc), \
                mock.patch.object(sk, "send", side_effect=lambda to, sig, *a: sent.append((to, sig.split("(")[0])) or True), \
                mock.patch.object(k, "log", side_effect=lambda event, **f: said.append((event, f))), \
                mock.patch.object(k.time, "time", return_value=float(chain.now())):
            for _ in range(n):
                sk.slow_pass()
                chain.head += 30  # a pass every 30 seconds, a block a second
        return sent, said

    def test_the_requests_of_a_pass_go_by_the_hundred_reads_not_by_the_account(self):
        counts = {}
        for n in (3, 100):
            chain = Chain(n)
            sk = self.keeper(chain, loop=mock.Mock())
            self.passes(sk, chain)  # the first pass measures the node's numbering and reads each exam's rules
            chain.calls.clear(), chain.batches.clear()
            self.passes(sk, chain)
            self.assertEqual(chain.alone, [], "no read asked alone")
            reads = sum(len(b) for b in chain.batches)
            self.assertEqual(reads, 1 + 4 + n * (5 + 3 * 3 + 1), "the factory's pools, the pool's four, fifteen an account")
            self.assertTrue(all(len(b) + 2 <= k.BATCH for b in chain.batches), "at most a hundred a call")
            self.assertEqual([m for m, _ in chain.calls], ["eth_getBlockByNumber"] + ["eth_call"] * len(chain.batches) + ["eth_getLogs"])
            counts[n] = len(chain.calls)
            self.assertEqual(sk.stop_loop.reread.call_count, 2 * n, "each exam's stops handed to the loop, each pass")
            self.assertEqual(sk.reads.got, {}, "the batch is the pass's: nothing later is answered from it")
        self.assertEqual(k.BATCH, 100)
        self.assertEqual(counts, {3: 3, 100: 19}, "three accounts in one call; a hundred in seventeen: six a call")
        # A pass every 30 seconds: a hundred more accounts are 32 more requests a minute, where they were 3 300.
        self.assertLessEqual(2 * (counts[100] - counts[3]), 40)

    def test_asked_one_by_one_a_hundred_accounts_were_sixteen_hundred_requests(self):
        chain = Chain(100, multicall=False)
        sk = self.keeper(chain)
        self.passes(sk, chain)
        self.assertEqual(chain.batches, [])
        self.assertEqual(len(chain.calls), 1 + 1 + 1 + 100 * (6 + 7 + 1) + 2,
                         "the head with its time, the pools, the scan; of each account six reads of its own, its rules "
                         "and a stop and a take an asset, its pool's window; the pool's queue")

    def test_an_account_is_never_cut_in_two_and_its_block_is_the_block_of_its_call(self):
        for ahead in (0, 1):
            chain = Chain(20, ahead=ahead)
            chain.moves = True
            loop = mock.Mock()
            sk = self.keeper(chain, loop=loop)
            self.passes(sk, chain)
            loop.reset_mock()
            chain.batches.clear()
            start = chain.head
            self.passes(sk, chain)
            where = {}
            for i, batch in enumerate(chain.batches):
                for to, _ in batch:
                    if to in chain.exams:
                        where.setdefault(to, set()).add(i)
            self.assertTrue(all(len(calls) == 1 for calls in where.values()), "every read of an account in one call")
            self.assertEqual(len(where), 20)
            for call in loop.reread.call_args_list:
                exam, batch = call.args[0], call.kwargs["batch"]
                self.assertEqual(batch.block_of[exam], start + next(iter(where[exam])), "the head as that call ran, whatever "
                                 "the node's numbering")
                self.assertEqual(batch.assets[exam], [3, 4, 0])

    def test_the_pass_does_what_it_did_when_each_read_was_asked_alone(self):
        def chain_of(multicall):
            chain = Chain(8, multicall=multicall)
            exams = list(chain.exams)
            chain.head = 86_400 * 20_000 - 1_000_000 + 120  # two minutes into a UTC day by the chain's clock
            day = chain.now() // 86_400
            chain.exams[exams[0]].update(violation=2, day=day)             # a rule broken now
            chain.exams[exams[1]].update(deadline=chain.now() - 1, day=day)  # past its deadline
            chain.exams[exams[2]].update(day=day - 1)                       # today's snapshot not taken
            chain.exams[exams[3]].update(status=3)                          # breached: released
            chain.exams[exams[4]].update(status=2, passed_at=chain.now() - 3_601)  # passed, the window to fund it over
            chain.exams[exams[5]].update(done=True)                         # the pool holds it done
            chain.exams[exams[6]].update(max_idle=50, last_fill=chain.now() - 51, day=day)  # idle past its limit
            chain.exams[exams[7]].update(day=day)                           # nothing due
            return chain, exams

        outcomes = {}
        for multicall in (True, False):
            chain, exams = chain_of(multicall)
            sk = self.keeper(chain)
            sent, said = self.passes(sk, chain)
            outcomes[multicall] = (sent, sorted(sk.done), [e for e, _ in said if e != "pass_done"])
            self.assertEqual(bool(chain.batches), multicall)
            if multicall:  # the day's first minutes: `day()` is in the batch with the rest
                self.assertEqual([s for _, s in chain.alone if s != sel("rules()")], [], "no read asked alone")
        self.assertEqual(outcomes[True], outcomes[False])
        sent, done, _ = outcomes[True]
        self.assertEqual(sent, [(exams[0], "breach"), (exams[1], "expire"), (exams[2], "checkpoint"), (Chain.POOL, "release"),
                                (Chain.POOL, "release"), (exams[6], "expire")])
        self.assertEqual(done, sorted([exams[3], exams[4], exams[5]]))

    def test_a_read_that_fails_in_a_batch_is_asked_alone_and_said_as_it_was(self):
        chain = Chain(3)
        bad = list(chain.exams)[1]
        chain.fails = {(bad, "violation()")}
        sk = self.keeper(chain)
        self.passes(sk, chain)
        chain.alone.clear()
        sent, said = self.passes(sk, chain)
        self.assertEqual(chain.alone, [(bad, sel("violation()"))], "that one read, and no other")
        unread = [f for e, f in said if e == "violation_unread"]
        self.assertEqual([f["account"] for f in unread], [bad])
        self.assertEqual([e for e, _ in said if e == "account_failed"], [], "the account's pass goes on, as before")

    def test_a_batch_that_fails_whole_is_asked_once_more_without_the_reads_that_burn_its_gas(self):
        """`violation()` reads prices, and a read of a price that fails may burn the gas left to the whole call. Those
        reads are the last of their call; when the call fails whole it is asked again without them, and they alone."""
        chain = Chain(3)
        bad = list(chain.exams)[1]
        chain.fails, chain.burns = {(bad, "violation()")}, True
        sk = self.keeper(chain)
        self.passes(sk, chain)
        chain.alone.clear(), chain.batches.clear(), chain.calls.clear()
        sent, said = self.passes(sk, chain)
        self.assertEqual([len(b) for b in chain.batches], [1 + 4 + 3 * 15, 1 + 4 + 3 * 14], "again, the three violation() left out")
        self.assertTrue(all(b[-1][1] == sel("violation()")[:10] for b in chain.batches[:1]), "they were the last of the call")
        self.assertEqual(sorted(chain.alone), sorted((e, sel("violation()")) for e in chain.exams))
        self.assertEqual([f["account"] for e, f in said if e == "violation_unread"], [bad])
        self.assertEqual([e for e, _ in said if e == "batch_failed"], [], "the second call answered: nothing more is said")

    def test_an_exam_with_a_read_missing_in_the_batch_is_read_again_alone_and_the_others_from_the_batch(self):
        chain = Chain(3)
        bad = list(chain.exams)[1]
        loop = mock.Mock()
        sk = self.keeper(chain, loop=loop)
        self.passes(sk, chain)
        loop.reset_mock()
        chain.fails_in_batch = {(bad, "stopOf(uint32)"): 2}  # the stop of its second asset, inside a batch only
        self.passes(sk, chain)
        how = {call.args[0]: "batch" in call.kwargs for call in loop.reread.call_args_list}
        self.assertEqual(how, {e: e != bad for e in chain.exams}, "the one with a read missing alone, as before; the "
                         "others from the batch")

    def test_a_new_exam_whose_status_fails_in_its_second_call_is_not_handed_over_from_two_blocks(self):
        """An exam met for the first time is read in two calls: its rules with its status, then its stops with its
        status again. If the status fails in the second, the first call's answer is of another block: it is not
        kept beside the second call's stops, and the exam is read again alone."""
        chain = Chain(2)
        chain.moves = True
        bad = list(chain.exams)[0]
        chain.fails_in_batch = {(bad, "result()"): 2}
        loop = mock.Mock()
        sk = self.keeper(chain, loop=loop)
        self.passes(sk, chain)
        how = {call.args[0]: call.kwargs.get("batch") for call in loop.reread.call_args_list}
        self.assertIsNone(how[bad], "read again alone: nothing of two blocks is handed over as one")
        other = list(chain.exams)[1]
        self.assertTrue(how[other].whole(other, [3, 4, 0]))
        self.assertFalse(how[other].whole(bad, [3, 4, 0]))
        self.assertIsNone(how[other].raw(bad, "result()"), "the first call's status is not kept: it is of an older block")

    def test_the_nodes_numbering_is_measured_once_and_again_after_ten_minutes(self):
        chain = Chain(2, ahead=1)
        sk = self.keeper(chain)
        self.passes(sk, chain)
        measure = ["eth_blockNumber", "eth_call", "eth_blockNumber", "eth_getBlockByNumber"] * 2
        self.assertEqual([m for m, _ in chain.calls][:9], ["eth_getBlockByNumber"] + measure,
                         "the pass's head with its time, then two measures that agree, each between two reads of the "
                         "head and with the head's time")
        self.assertEqual(sk.ahead, 1)
        chain.calls.clear()
        self.passes(sk, chain, 3)
        methods = [m for m, _ in chain.calls]
        self.assertEqual((methods.count("eth_blockNumber"), methods.count("eth_getBlockByNumber")), (0, 3),
                         "not measured again: the pass's own head, each pass")
        sk.ahead_at -= k.NUMBERING_EVERY_S
        chain.calls.clear()
        self.passes(sk, chain)
        self.assertEqual([m for m, _ in chain.calls].count("eth_blockNumber"), 4)

    def test_a_numbering_that_cannot_be_read_ends_the_batches_and_says_so(self):
        chain = Chain(2, ahead=3)
        sk = self.keeper(chain)
        sent, said = self.passes(sk, chain, 2)
        self.assertFalse(sk.multicall)
        self.assertEqual([e for e, _ in said if e == "batches_off"], ["batches_off"], "once")
        self.assertEqual(len(chain.batches), 1, "the measure's own call, and none after")
        self.assertTrue(chain.alone, "every read asked alone, as before")

    def test_a_call_that_says_an_older_block_than_the_head_hands_no_stops_over_and_is_measured_again(self):
        chain = Chain(2, ahead=0)
        loop = mock.Mock()
        sk = self.keeper(chain, loop=loop)
        self.passes(sk, chain)
        sk.ahead = 1  # measured on a node that numbers a call as the block to come; the node now says the head's
        loop.reset_mock()
        sent, said = self.passes(sk, chain)
        self.assertEqual([e for e, _ in said if e == "numbering_changed"], ["numbering_changed"])
        self.assertTrue(all("batch" not in call.kwargs for call in loop.reread.call_args_list), "read the old way this pass")
        self.assertEqual(loop.reread.call_count, 2)
        self.assertIsNone(sk.ahead)

    def test_an_exams_assets_are_written_once(self):
        """The assets of an exam's rules are kept after the first read of them: they are written when the exam is made
        and nowhere else."""
        src = (ROOT / "src/exam/PaperExam.sol").read_text()
        self.assertEqual(src.count("_rules.assets.push("), 1)
        init = src[src.index("function _initialize("):src.index("function rules()")]
        self.assertIn("_rules.assets.push(rules_.assets[i]);", init)
        self.assertEqual(src.count("delete _rules"), 0)
        chain = Chain(2)
        sk = self.keeper(chain)
        self.passes(sk, chain, 3)
        asked = [b for batch in chain.batches for b in batch if b[1] == sel("rules()")[:10]]
        self.assertEqual(len(asked), 2, "once an exam")
        sk.done.add(list(chain.exams)[0])
        self.passes(sk, chain)
        self.assertEqual(list(sk.rule_assets), [list(chain.exams)[1]], "an account that is done is forgotten")

    def test_at_the_start_multicall3_is_looked_for_once(self):
        from spike.hlspike import network as networks
        said = []
        with mock.patch.object(k, "log", side_effect=lambda event, **f: said.append((event, f))):
            self.assertTrue(k.multicall_or_not(Chain(0).rpc, "the keeper"))
            self.assertFalse(k.multicall_or_not(Chain(0, multicall=False).rpc, "the keeper"))
            with mock.patch.object(k.c, "NETWORK", networks.MAINNET), self.assertRaises(SystemExit) as caught:
                k.multicall_or_not(Chain(0, multicall=False).rpc, "the keeper")
        self.assertIn("no Multicall3", str(caught.exception))
        self.assertEqual([(e, f["who"]) for e, f in said], [("no_multicall", "the keeper")], "one line, on testnet")


class OneLookForBothLoops(unittest.TestCase):
    """With the stops' loop the fast loop takes its head and its logs from it: no look at the head and no eth_getLogs
    of its own."""
    EXAM = k.to_checksum_address("0x" + "11" * 20)
    POOL = k.to_checksum_address("0x" + "55" * 20)

    def keeper(self) -> k.SimKeeper:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sk = k.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, False, tmp / "s.json", 1)
        sk.accounts = {self.EXAM: {"pool": self.POOL, "kind": "exam"}}
        sk.pools = [self.POOL]
        sk.stop_loop = mock.Mock(feed=queue.Queue())
        return sk

    def test_a_step_handed_in_is_taken_with_no_request_and_its_order_filled_for_the_next_block(self):
        sk = self.keeper()
        sk.fast_block = 8
        filled = []
        sk.stop_loop.feed.put((9, 9, [placed_log(self.EXAM, 10, 14, 9)]))
        with mock.patch.object(k.c, "rpc") as rpc, \
                mock.patch.object(sk, "fill", side_effect=lambda e: filled.append(e) or {"transactionHash": "0x01"}):
            self.assertEqual(sk.fed_step(0.01), [self.EXAM], "its window opens at 10, the next block")
            self.assertEqual(sk.fed_step(0.01), [], "nothing handed in: nothing done, and no look at the head")
        rpc.assert_not_called()
        self.assertEqual((filled, sk.fast_block), ([self.EXAM], 9))

    def test_steps_that_waited_are_taken_together_and_a_block_handed_in_twice_is_taken_once(self):
        sk = self.keeper()
        sk.fast_block = 8
        feed = sk.stop_loop.feed
        feed.put((9, 9, [placed_log(self.EXAM, 12, 16, 9)]))
        feed.put((10, 11, []))
        with mock.patch.object(k.c, "rpc") as rpc, mock.patch.object(sk, "fill", return_value={"transactionHash": "0x01"}) as fill:
            self.assertEqual(sk.fed_step(0.01), [self.EXAM], "the order of block 9, due for block 12, at the head 11")
            # The loop's step failed half way and ran again from block 11: block 11 comes a second time.
            feed.put((11, 12, [placed_log(self.EXAM, 12, 16, 9) | {"blockNumber": hex(11)}]))
            self.assertEqual(sk.fed_step(0.01), [], "not placed again by a block already read")
        rpc.assert_not_called()
        self.assertEqual((fill.call_count, sk.fast_block, sk.orders), (1, 12, {}))

    def test_two_orders_of_one_exam_are_taken_in_the_chains_order(self):
        sk = self.keeper()
        sk.fast_block = 8
        first = placed_log(self.EXAM, 10, 14, 9) | {"logIndex": "0x2"}
        second = placed_log(self.EXAM, 20, 24, 9) | {"logIndex": "0x7"}
        sk.stop_loop.feed.put((9, 9, [second, first]))  # as two reads of the node gave them, not in the block's order
        with mock.patch.object(k.c, "rpc") as rpc:
            sk.fed_step(0.01)
        rpc.assert_not_called()
        self.assertEqual(sk.orders, {self.EXAM: (20, 24)}, "the later order of the block is the one that waits")

    def test_an_account_sold_in_a_step_is_known_from_it(self):
        sk = self.keeper()
        sk.fast_block = 8
        new = k.to_checksum_address("0x" + "66" * 20)
        sold = {"address": self.POOL, "topics": [k.EXAM_SOLD, "0x" + "00" * 12 + "66" * 20], "data": "0x", "blockNumber": hex(9),
                "logIndex": "0x0"}
        order = placed_log(new, 10, 14, 9) | {"logIndex": "0x1"}
        sk.stop_loop.feed.put((9, 9, [order, sold]))
        with mock.patch.object(k.c, "rpc") as rpc, mock.patch.object(sk, "fill", return_value={"transactionHash": "0x01"}):
            self.assertEqual(sk.fed_step(0.01), [new])
        rpc.assert_not_called()
        self.assertEqual(sk.accounts[new], {"pool": self.POOL, "kind": "exam"})

    def test_an_account_sold_in_the_blocks_read_here_has_its_orders_read_up_to_the_head(self):
        """Just started: the stops' loop hands in its head alone, block 500, read without the exam sold in block 499,
        which this loop finds in the blocks it reads itself. The exam's order of block 500 is asked for too."""
        sk = self.keeper()
        new = k.to_checksum_address("0x" + "66" * 20)
        sold = {"address": self.POOL, "topics": [k.EXAM_SOLD, "0x" + "00" * 12 + "66" * 20], "data": "0x", "blockNumber": hex(499)}
        sk.stop_loop.feed.put((500, 500, []))
        asked = []

        def rpc(method, params):
            q = params[0]
            asked.append((q["address"], q["fromBlock"], q["toBlock"]))
            return [sold] if q["address"] != [new] else [placed_log(new, 501, 505, 500)]

        with mock.patch.object(k.c, "rpc", side_effect=rpc), mock.patch.object(sk, "fill", return_value={"transactionHash": "0x01"}):
            self.assertEqual(sk.fed_step(0.01), [new], "its order of block 500, due for 501")
        self.assertEqual(asked[1], ([new], hex(500 - k.MAX_LOG_WINDOW + 1), hex(500)), "to the head, not only to block 499")

    def test_steps_that_do_not_follow_one_another_leave_the_older_ones_logs_out(self):
        """The loop skipped blocks 102 and 103. This loop reads them itself, with the block before them; the older
        step's order of block 101 is not taken after the newer one of block 103."""
        sk = self.keeper()
        sk.fast_block = 100
        feed = sk.stop_loop.feed
        feed.put((101, 101, [placed_log(self.EXAM, 102, 103, 101)]))
        feed.put((104, 104, []))
        asked = []

        def rpc(method, params):
            asked.append((params[0]["fromBlock"], params[0]["toBlock"]))
            return [placed_log(self.EXAM, 102, 103, 101), placed_log(self.EXAM, 106, 110, 103)]

        with mock.patch.object(k.c, "rpc", side_effect=rpc):
            sk.fed_step(0.01)
        self.assertEqual(asked, [(hex(101), hex(103))])
        self.assertEqual(sk.orders, {self.EXAM: (106, 110)}, "the order of block 103 waits, not the one of 101 again")

    def test_the_blocks_before_the_first_step_handed_in_are_read_here_once(self):
        sk = self.keeper()  # just started: the loop hands in its head alone, and an order may wait from before
        sk.stop_loop.feed.put((500, 500, []))
        asked = []

        def rpc(method, params):
            asked.append((method, params[0]["fromBlock"], params[0]["toBlock"]))
            return [placed_log(self.EXAM, 501, 505, 499)]

        with mock.patch.object(k.c, "rpc", side_effect=rpc), mock.patch.object(sk, "fill", return_value={"transactionHash": "0x01"}):
            self.assertEqual(sk.fed_step(0.01), [self.EXAM])
            sk.stop_loop.feed.put((501, 501, []))
            sk.fed_step(0.01)
        self.assertEqual(asked, [("eth_getLogs", hex(500 - k.MAX_LOG_WINDOW + 1), hex(499))], "its own read of the blocks "
                         "before, once")

    def test_blocks_the_loop_skipped_are_read_here(self):
        sk = self.keeper()
        sk.fast_block = 100
        feed = sk.stop_loop.feed
        feed.put((101, 101, []))
        feed.put((1_100, 1_100, []))  # the loop was far behind and went on from here
        asked = []
        with mock.patch.object(k.c, "rpc", side_effect=lambda m, p: asked.append((p[0]["fromBlock"], p[0]["toBlock"])) or []):
            sk.fed_step(0.01)
        self.assertEqual(asked, [(hex(1_100 - k.MAX_LOG_WINDOW + 1), hex(1_099))])
        self.assertEqual(sk.fast_block, 1_100)


class TheMeasure(unittest.TestCase):
    """--measure judges each order by the chain's events. A fill that lands between the read of the events and the
    read of `pending()` leaves no order waiting: it was written as neither filled nor lapsed."""
    EXAM = k.to_checksum_address("0x" + "11" * 20)

    def run_measure(self, events_by_read, pending_by_read):
        """One order; `events_by_read`: what each eth_getLogs answers, in turn; `pending_by_read`: each `pending()`."""
        out = pathlib.Path(tempfile.mkdtemp()) / "m.json"
        self.addCleanup(shutil.rmtree, out.parent, ignore_errors=True)
        reads = {"logs": 0, "pending": 0}
        clock = {"now": 0.0}

        def rpc(method, params=()):
            self.assertEqual(method, "eth_getLogs")
            self.assertEqual(params[0]["topics"], [[k.FILLED, k.LAPSED, k.TRIGGER_FILLED]])
            reads["logs"] += 1
            return events_by_read[min(reads["logs"], len(events_by_read)) - 1]

        def call_view(to, sig, *a):
            reads["pending"] += 1
            clock["now"] += self.pending_takes  # the seconds the read of `pending()` took
            return (3, True, pending_by_read[min(reads["pending"], len(pending_by_read)) - 1], 0)

        def sleep(seconds):
            clock["now"] += seconds

        with mock.patch.object(k.c, "transact", return_value={"blockNumber": hex(100)}), \
                mock.patch.object(k.c, "rpc", side_effect=rpc), mock.patch.object(k.c, "call_view", side_effect=call_view), \
                mock.patch.object(k.time, "sleep", side_effect=sleep), mock.patch.object(k.time, "time", side_effect=lambda: clock["now"]), \
                mock.patch.object(k, "log"):
            return k.measure(self.EXAM, object(), 3, 12, 1, out, timeout_s=120), reads

    pending_takes = 0.0

    def event(self, topic, block, late=2, asset=3, tx=None):
        data = encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"], [True, 12, 7, 0, late, 1])
        return {"address": self.EXAM, "topics": [topic, "0x" + asset.to_bytes(32, "big").hex()], "data": "0x" + data.hex(),
                "blockNumber": hex(block), "transactionHash": tx or f"0x{block:064x}"}

    def test_a_fill_that_landed_between_the_two_reads_is_counted_filled(self):
        summary, reads = self.run_measure([[], [self.event(k.FILLED, 103)]], [0])
        self.assertEqual((summary["filled"], summary["rows"][0]["late"], summary["rows"][0]["filled_block"]), (1, 2, 103))
        self.assertEqual(reads, {"logs": 2, "pending": 1}, "the events read once more after no order was found waiting")

    def test_a_lapse_the_chain_wrote_is_a_lapse_and_an_order_gone_with_no_event_is_neither(self):
        summary, _ = self.run_measure([[], [self.event(k.LAPSED, 104)]], [12, 0])
        self.assertEqual((summary["filled"], summary["rows"][0]["lapsed"]), (0, True))
        summary, reads = self.run_measure([[]], [0])
        self.assertEqual((summary["filled"], summary["rows"][0]["lapsed"], summary["rows"][0]["late"]), (0, False, None))
        self.assertEqual(reads["logs"], 2)

    def test_events_of_the_block_it_was_placed_in_are_not_its_own(self):
        summary, _ = self.run_measure([[self.event(k.LAPSED, 100)], [self.event(k.LAPSED, 100), self.event(k.FILLED, 102, late=0)]], [12])
        self.assertEqual((summary["filled"], summary["rows"][0]["lapsed"], summary["rows"][0]["late"]), (1, False, 0))

    def test_a_stops_fill_and_another_assets_fill_are_not_the_orders(self):
        """`fillTrigger` writes a `Filled` of its own, beside its `TriggerFilled` in one transaction, and leaves the
        order waiting unless it closed the whole position: its lateness is the stop's, not the order's."""
        stop = [self.event(k.FILLED, 102, late=9, tx="0xaa"), self.event(k.TRIGGER_FILLED, 102, tx="0xaa")]
        other = [self.event(k.FILLED, 102, late=8, asset=4, tx="0xcc")]
        own = [self.event(k.FILLED, 104, late=1, tx="0xbb")]
        summary, _ = self.run_measure([stop + other, stop + other + own], [12])
        self.assertEqual((summary["filled"], summary["rows"][0]["late"], summary["rows"][0]["filled_block"]), (1, 1, 104))

    def test_no_order_waiting_when_the_time_runs_out_is_still_judged_by_the_events(self):
        self.pending_takes = 500.0  # the read of `pending()` comes back past the measure's two minutes
        summary, reads = self.run_measure([[], [self.event(k.FILLED, 103)]], [0])
        self.assertEqual((summary["filled"], summary["rows"][0]["lapsed"]), (1, False), "filled, not lapsed for want of a last read")
        self.assertEqual(reads, {"logs": 2, "pending": 1})

    def test_an_order_still_waiting_when_the_time_runs_out_is_written_lapsed(self):
        summary, reads = self.run_measure([[]], [12])
        self.assertEqual((summary["filled"], summary["rows"][0]["lapsed"]), (0, True))
        self.assertEqual(reads["logs"], 121, "the events read each second of the two minutes, the last time just before")


class TheRequestsCounted(unittest.TestCase):
    def test_the_keepers_requests_are_counted_by_method_and_start_again_once_taken(self):
        calls = []
        counted = k.Counted(lambda method, params=(): calls.append((method, params)) or "0x1")
        counted("eth_blockNumber")
        counted("eth_call", [{}, "latest"])
        counted("eth_call", [{}, "latest"])
        self.assertEqual(calls, [("eth_blockNumber", ()), ("eth_call", [{}, "latest"]), ("eth_call", [{}, "latest"])])
        self.assertEqual(counted.taken(), {"eth_blockNumber": 1, "eth_call": 2})
        self.assertEqual(counted.taken(), {})

    def test_every_call_of_the_plumbing_goes_through_the_count_and_it_is_put_there_once(self):
        with mock.patch.object(k.c, "rpc", return_value="0x" + (42).to_bytes(32, "big").hex()) as rpc:
            counted = k.count_requests()
            self.assertIs(k.count_requests(), counted, "not counted twice")
            self.assertEqual(int(k.c.rpc("eth_blockNumber"), 16), 42)
            self.assertEqual(k.view("0x" + "55" * 20, "head()", "uint256"), 42)
            self.assertEqual(counted.taken(), {"eth_blockNumber": 1, "eth_call": 1})
        self.assertEqual(rpc.call_count, 2)

    def test_the_keeper_says_it_is_alive_once_a_minute_with_its_requests_by_method(self):
        class Out(Exception):
            pass
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sk = k.SimKeeper("0x" + "33" * 20, None, "0x" + "44" * 20, True, tmp / "s.json", 1)
        said = []
        turns = {"n": 0}

        def sleep(_):
            turns["n"] += 1
            if turns["n"] == 3:
                raise Out

        with mock.patch.object(k.deployments, "load", return_value={"SimPoolFactory": "0x" + "33" * 20, "block": 1}), \
                mock.patch.object(k.sn, "confirm_service"), mock.patch.object(k, "SimKeeper", return_value=sk), \
                mock.patch.object(sk, "slow_pass"), mock.patch.object(k.c, "rpc", return_value="0x1"), \
                mock.patch.object(k.time, "sleep", side_effect=sleep), mock.patch.object(k, "ALIVE_EVERY_S", 0.0), \
                mock.patch.object(k, "log", side_effect=lambda event, **f: said.append((event, f))), \
                mock.patch.dict("os.environ", {"COLOSSEUM_STOPS": "inline"}):
            with self.assertRaises(Out):
                k.main(["--deployment", "sim1", "--dry-run", "--from", "0x" + "44" * 20])
        alive = [f for e, f in said if e == "keeper_alive"]
        self.assertEqual(len(alive), 3)
        self.assertEqual(alive[0]["requests"], {"eth_getCode": 1}, "the look for Multicall3 at the start")
        self.assertEqual(alive[1]["requests"], {"eth_blockNumber": 1}, "inline, the fast loop's own look at the head")
        self.assertNotIn("127.0.0.1", str(alive))


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
