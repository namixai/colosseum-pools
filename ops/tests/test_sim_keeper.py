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
        # One call pays at most the day's payouts: the keeper's number is the contract's.
        pool = (ROOT / "src/sim/SimPool.sol").read_text()
        self.assertIn(f"uint256 constant PAYOUTS_A_DAY = {k.PAY_PER_CALL};", pool)
        self.assertIn("uint8 public constant PAYOUTS_PER_DAY = uint8(PAYOUTS_A_DAY);",
                      (ROOT / "src/sim/SimPoolFactory.sol").read_text())
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
        self.assertEqual([what for what, _ in logged], ["pay_waits_limit"])

    def test_a_wave_of_five_goes_out_two_two_one_and_no_pay_is_sent_while_the_limit_holds(self):
        self.queue(0, approved_after=5)
        t0 = self.now
        logged = self.passes(3)  # three passes in the same half minute
        self.assertEqual(self.calls, [(2, 2)], "one call, the day's two; then none while the limit holds")
        self.assertEqual([(what, kw["room"], kw["next_at"]) for what, kw in logged],
                         [("pay_waits_limit", 2, 0), ("pay_waits_limit", 0, t0 + self.DAY)],
                         "said once when it changes, not every pass")
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
