"""ops/multi_keeper.py: what is due on a pool of many traders, and the noter, which never lets a seat
that traded be closed as idle."""
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from ops import multi_keeper as k
from ops import multi_run as r
from ops.tests.test_multi_run import EXAM, FREE, MULTI, POOL, enum_names, struct_types

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHARES = (ROOT / "src/multi/PoolShares.sol").read_text()
DAY = 86_400
NOW = 1_790_000_000 + 3_600  # an hour into a UTC day: no checkpoint is due


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class Copies(unittest.TestCase):
    def test_names_and_layouts_follow_the_contracts(self):
        self.assertEqual(k.EXAM_STATUS, r.EXAM_STATUS)
        self.assertEqual(k.SEAT_STATE, r.SEAT_STATE)
        self.assertEqual(k.EXAM_STATE, enum_names(MULTI, "ExamState"))
        self.assertEqual(k.SEAT_INFO, r.SEAT_INFO)
        self.assertEqual(k.EXAM_INFO, struct_types(MULTI, "ExamInfo", ("ExamState",)))
        self.assertEqual([k.EXAM_TERMS], r.EXAM_TERMS)
        self.assertEqual(k.EXAM_SOLD, r.EXAM_SOLD)
        self.assertEqual(enum_names(MULTI, "AfterTarget").index("TraderStops"), k.TRADER_STOPS)
        self.assertIn("uint64 public constant ARM_WAIT = 1 minutes;", MULTI)
        self.assertEqual(k.ARM_WAIT, 60)
        body = re.search(r"struct Plan \{(.*?)\n\}", MULTI, re.S).group(1)
        fields = [l.strip().rstrip(";").split(" ")[-1] for l in body.split("\n") if l.strip().endswith(";")]
        self.assertEqual(k.PLAN_FIELDS, tuple(snake(f) for f in fields))

    def test_the_calls_it_makes_exist_and_are_open_to_anyone(self):
        stop = "Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt) external {"
        for sig in ("function release(address exam) external {", "function open(address seat) external {",
                    "function rearm(address seat) external {", "function finish(address seat) external {",
                    "function sweep(address seat) external {", "function noteTrade(address seat) external {",
                    f"function hitTarget(address seat, {stop}", f"function endSeat(address seat, {stop}",
                    f"function closeIdle(address seat, {stop}",
                    "mapping(address exam => ExamInfo) public examInfo;", "address public immutable planData;",
                    "uint64 public immutable notice;", "function endsAt() public view returns (uint64)",
                    "address public immutable owner;"):
            self.assertIn(sig, MULTI)
        self.assertRegex(MULTI, r"mapping\(address seat => uint64\) public activeSince;")
        for sig in ("function checkpoint() external inStatus(Status.Active) {",
                    "function breach() external inStatus(Status.Active) {",
                    "function expire() external inStatus(Status.Active) {",
                    "function violation() public view returns (Breach)", "uint64 public deadline;",
                    "uint64 public lastFillAt;", "uint32 public day;"):
            self.assertIn(sig, EXAM)
        for sig in ("function renew() external bound {", "uint256 public exitShares;", "MultiPool public pool;",
                    "if (end == 0 || block.timestamp + n <= end) revert NotInWindow"):
            self.assertIn(sig, SHARES)
        self.assertIn("int64 public fundedStart;", POOL)
        types = (ROOT / "src/Types.sol").read_text()
        self.assertRegex(types, r"struct Cancel \{\s*uint32 \w+;\s*uint64 \w+;\s*\}")
        self.assertEqual(k.STOP, "(address,(uint32,uint64)[],uint32[],bytes32)")
        self.assertNotIn("noteTrade", FREE)


class Exams(unittest.TestCase):
    def calls(self, status, over=False, **kw):
        return k.exam_calls("Sold", status, NOW, over, claim_window=3_600, **kw)

    def test_a_running_exam(self):
        self.assertEqual(self.calls("Active", deadline=NOW + 1, day=NOW // DAY), [])
        self.assertEqual(self.calls("Active", deadline=NOW - 1, day=NOW // DAY), ["expire"])
        self.assertEqual(self.calls("Active", deadline=0, last_fill=NOW - 101, max_idle=100, day=NOW // DAY), ["expire"])
        self.assertEqual(self.calls("Active", deadline=0, last_fill=NOW - 100, max_idle=100, day=NOW // DAY), [])
        self.assertEqual(self.calls("Active", broken=True, deadline=NOW - 1, day=NOW // DAY), ["breach"])
        self.assertEqual(self.calls("Active", over=True), ["release"], "the pool is over: its size comes back")

    def test_the_days_snapshot(self):
        start = (NOW // DAY) * DAY
        self.assertEqual(k.exam_calls("Sold", "Active", start + 60, False, day=NOW // DAY - 1), ["checkpoint"])
        self.assertEqual(k.exam_calls("Sold", "Active", start + 60, False, day=NOW // DAY), [])
        self.assertEqual(k.exam_calls("Sold", "Active", start + 60, False, broken=True, day=NOW // DAY - 1),
                         ["checkpoint", "breach"])

    def test_an_exam_over(self):
        self.assertEqual(self.calls("Breached"), ["release"])
        self.assertEqual(self.calls("Expired"), ["release"])
        self.assertEqual(self.calls("Passed", passed_at=NOW - 3_600), [], "the trader may still take it")
        self.assertEqual(self.calls("Passed", passed_at=NOW - 3_601), ["release"])
        self.assertEqual(self.calls("Passed", over=True, passed_at=NOW), ["release"])
        for state in ("None", "Claimed", "Released"):
            self.assertEqual(k.exam_calls(state, "Passed", NOW, True), [])


class Seats(unittest.TestCase):
    def test_a_free_seat_is_swept_only_when_money_reached_it(self):
        self.assertEqual(k.seat_calls("Free", k.IDLE, NOW, False, spot=1), ["sweep"])
        self.assertEqual(k.seat_calls("Free", k.IDLE, NOW, False, spot=0), [])

    def test_an_armed_seat(self):
        armed = dict(size=500_000_000, armed_at=NOW - 60, rest_until=NOW - 1)
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW, False, spot=0, **armed), [], "a minute is not up yet")
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW + 1, False, spot=0, **armed), ["rearm"])
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW, False, spot=500_000_000, **armed), ["open"])
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW, False, spot=500_000_000, size=500_000_000,
                                      armed_at=NOW, rest_until=NOW), [], "still resting")
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW, True, spot=0, **armed), ["finish"])

    def test_a_settled_seat_is_finished(self):
        self.assertEqual(k.seat_calls("Funded", k.IDLE, NOW, False), ["finish"])
        self.assertEqual(k.seat_calls("Waiting", k.IDLE, NOW, False, wait_until=NOW), [], "its trader may come back")
        self.assertEqual(k.seat_calls("Waiting", k.IDLE, NOW, False, wait_until=NOW - 1), ["finish"])
        self.assertEqual(k.seat_calls("Waiting", k.IDLE, NOW, True, wait_until=NOW + 9), ["finish"])
        self.assertEqual(k.seat_calls("Funded", k.FUNDED, NOW, False), [])
        self.assertEqual(k.seat_calls("None", k.IDLE, NOW, True), [])

    def test_a_funded_stage(self):
        base = dict(has_target=True, flat=True, equity=11_000_000, start=10_000_000, target_bps=1_000, idle_due=True)
        self.assertEqual(k.funded_calls(True, **base), ["endSeat"])
        self.assertEqual(k.funded_calls(False, **base), ["hitTarget"])
        self.assertEqual(k.funded_calls(False, **{**base, "equity": 10_999_999}), ["closeIdle"])
        self.assertEqual(k.funded_calls(False, **{**base, "flat": False}), ["closeIdle"])
        self.assertEqual(k.funded_calls(False, **{**base, "has_target": False}), ["closeIdle"])
        self.assertEqual(k.funded_calls(False, **{**base, "has_target": False, "idle_due": False}), [])

    def test_fills_and_the_window(self):
        self.assertTrue(k.traded_since([{"time": NOW * 1000 + 1}], NOW))
        self.assertFalse(k.traded_since([{"time": NOW * 1000}], NOW))
        self.assertFalse(k.traded_since([], NOW))
        notice = 14 * DAY
        self.assertFalse(k.in_renew_window(NOW, NOW + notice, notice), "PoolShares refuses at end - notice")
        self.assertTrue(k.in_renew_window(NOW + 1, NOW + notice, notice))
        self.assertFalse(k.in_renew_window(NOW, 0, notice), "a pool not begun")


SEAT = "0x" + "5e" * 20
NOTER = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20


class Chain:
    """A funded seat as the keeper reads it: the pool's activeSince and the seat's stage and start,
    the margin summary, and the fills Hyperliquid answers with."""

    def __init__(self, since, fills=(), fills_fail=False, equity=10_000_000, flat=True):
        self.since, self.fills, self.fills_fail = since, list(fills), fills_fail
        self.equity, self.flat = equity, flat
        self.sent, self.fill_reads = [], 0

    def view(self, addr, sig, out, types=(), args=()):
        return {"activeSince(address)": self.since, "stage()": k.FUNDED, "fundedStart()": 10_000_000}[sig]

    def info(self, body):
        self.fill_reads += 1
        if self.fills_fail:
            raise RuntimeError("429")
        return self.fills

    def send(self, to, sig, types=(), args=()):
        self.sent.append(sig.split("(")[0])
        if sig.startswith("noteTrade"):
            self.since = NOW
        return True


def keeper(sender, idle=DAY, after_target=1, note_every=3_600, tmp=None):
    kp = k.MultiKeeper("0x" + "11" * 20, None, sender, False, pathlib.Path(tmp) / "s.json", 1, note_every=note_every)
    kp.plan = {"noter": NOTER, "funded_idle": idle, "after_target": after_target, "funded_target_bps": 1_000,
               "claim_window": 3_600}
    return kp


def run_funded(kp, chain, over=False):
    with mock.patch.object(k, "view", chain.view), mock.patch.object(k.core, "pool_pass", lambda *a: True), \
            mock.patch.object(k.c, "info_post", chain.info), \
            mock.patch.object(k.c, "core_margin_summary",
                              lambda s: {"accountValue": chain.equity, "ntlPos": 0 if chain.flat else 1}), \
            mock.patch.object(k.core, "stop_args", lambda *a: ([], [])), mock.patch.object(kp, "send", chain.send), \
            mock.patch.object(k, "log"):
        kp.funded_pass(SEAT, NOW, over, {}, None)


class TheNoter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_an_idle_seat_with_no_fill_is_closed(self):
        chain = Chain(since=NOW - DAY - 1)
        run_funded(keeper(NOTER, tmp=self.tmp), chain)
        self.assertEqual(chain.sent, ["closeIdle"])

    def test_a_round_trip_the_chain_cannot_see_is_noted_and_nothing_is_closed(self):
        chain = Chain(since=NOW - DAY - 1, fills=[{"time": (NOW - 600) * 1000}])
        run_funded(keeper(NOTER, tmp=self.tmp), chain)
        self.assertEqual(chain.sent, ["noteTrade"])

    def test_fills_that_cannot_be_read_close_nothing(self):
        chain = Chain(since=NOW - DAY - 1, fills_fail=True)
        run_funded(keeper(NOTER, tmp=self.tmp), chain)
        self.assertEqual(chain.sent, [])

    def test_a_keeper_that_is_not_the_noter_leaves_a_seat_that_traded(self):
        chain = Chain(since=NOW - DAY - 1, fills=[{"time": (NOW - 600) * 1000}])
        run_funded(keeper(OTHER, tmp=self.tmp), chain)
        self.assertEqual(chain.sent, [])
        chain = Chain(since=NOW - DAY - 1)
        run_funded(keeper(OTHER, tmp=self.tmp), chain)
        self.assertEqual(chain.sent, ["closeIdle"], "and closes one that did not")

    def test_a_trade_is_noted_on_the_way_without_waiting_for_the_idle_limit(self):
        chain = Chain(since=NOW - 7_200, fills=[{"time": (NOW - 600) * 1000}])
        kp = keeper(NOTER, tmp=self.tmp)
        run_funded(kp, chain)
        self.assertEqual((chain.sent, chain.fill_reads), (["noteTrade"], 1))
        # Read again only once --note-every has gone by.
        run_funded(kp, chain)
        self.assertEqual(chain.fill_reads, 1)
        self.assertEqual(kp.fills_read_at[SEAT], NOW)

    def test_a_stage_past_its_target_ends_there_and_the_pools_end_ends_it(self):
        chain = Chain(since=NOW - 60, equity=11_000_000)
        run_funded(keeper(NOTER, tmp=self.tmp), chain)
        self.assertEqual(chain.sent, ["hitTarget"])
        chain = Chain(since=NOW - DAY - 1, equity=11_000_000)
        run_funded(keeper(NOTER, after_target=k.TRADER_STOPS, tmp=self.tmp), chain)
        self.assertEqual(chain.sent, ["closeIdle"], "a plan with no target never hits one")
        chain = Chain(since=NOW - 60)
        run_funded(keeper(NOTER, tmp=self.tmp), chain, over=True)
        self.assertEqual((chain.sent, chain.fill_reads), (["endSeat"], 0))


class Dispatch(unittest.TestCase):
    """What a pass sends, to whom, from the reads of one seat or one exam."""
    POOL = "0x" + "11" * 20
    EXAM = "0x" + "e0" * 20

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kp = keeper(NOTER, tmp=self.tmp)
        self.kp.plan["menu"] = [(5_000_000, 50_000)]
        self.sent = []

    def seat(self, state, stage, spot=0, armed_at=0, rest_until=0, wait_until=0, over=False):
        info = (0, k.SEAT_STATE.index(state), False, OTHER, armed_at, rest_until, wait_until, 0)
        with mock.patch.object(k.c, "call_view", lambda *a: info), mock.patch.object(k, "view", lambda *a, **kw: stage), \
                mock.patch.object(k.c, "core_spot_balance", lambda a, t: {"total": spot}), \
                mock.patch.object(self.kp, "send", lambda to, sig, *x: self.sent.append((to, sig))):
            self.kp.seat_pass(SEAT, NOW, over, {}, None)
        return self.sent

    def test_seats(self):
        self.assertEqual(self.seat("Free", k.IDLE, spot=7), [(self.kp.pool, "sweep(address)")])
        self.sent = []
        self.assertEqual(self.seat("Armed", k.IDLE, spot=500_000_000, rest_until=NOW - 1), [(self.kp.pool, "open(address)")])
        self.sent = []
        self.assertEqual(self.seat("Armed", k.IDLE, spot=499_999_999, armed_at=NOW - 61), [(self.kp.pool, "rearm(address)")])
        self.sent = []
        self.assertEqual(self.seat("Funded", k.IDLE), [(self.kp.pool, "finish(address)")])

    def test_a_closing_stage_goes_to_the_core_keeper(self):
        passes = []
        with mock.patch.object(k.core, "pool_pass", lambda w, a, *x: passes.append(a)):
            self.seat("Funded", k.CLOSING)
            self.seat("Waiting", k.CLOSING)
        self.assertEqual((passes, self.sent), ([SEAT, SEAT], []))

    def exam(self, state, status, over=False, passed_at=0, **reads):
        def call_view(to, sig, types, args, out):
            if sig == "examInfo(address)":
                return (0, k.EXAM_STATE.index(state), k.ZERO, False)
            return (k.EXAM_STATUS.index(status), OTHER, 5_000_000, passed_at)

        values = {"terms()": (5_000_000, [10], 3_600, reads.get("max_idle", 0), 0, 100, 2, 300),
                  "violation()": reads.get("violation", 0), "deadline()": reads.get("deadline", NOW + 1),
                  "lastFillAt()": reads.get("last_fill", NOW), "day()": reads.get("day", NOW // DAY)}
        with mock.patch.object(k.c, "call_view", call_view), mock.patch.object(k, "view", lambda a, s, o, *x: values[s]), \
                mock.patch.object(self.kp, "send", lambda to, sig, *x: self.sent.append((to, sig))):
            kept = self.kp.exam_pass(self.EXAM, reads.get("now", NOW), over)
        return kept, self.sent

    def test_exams(self):
        self.assertEqual(self.exam("Sold", "Breached"), (True, [(self.kp.pool, "release(address)")]))
        self.sent = []
        self.assertEqual(self.exam("Sold", "Active", violation=2), (True, [(self.EXAM, "breach()")]))
        self.sent = []
        self.assertEqual(self.exam("Sold", "Active", deadline=NOW - 1), (True, [(self.EXAM, "expire()")]))
        self.sent = []
        start = (NOW // DAY) * DAY
        self.assertEqual(self.exam("Sold", "Active", now=start + 60, day=NOW // DAY - 1), (True, [(self.EXAM, "checkpoint()")]))
        self.sent = []
        self.assertEqual(self.exam("Sold", "Passed", passed_at=NOW - 3_601), (True, [(self.kp.pool, "release(address)")]))
        self.sent = []
        self.assertEqual(self.exam("Claimed", "Passed"), (False, []), "off the list for good")

    def test_the_shares_contract_is_the_owner_that_names_this_pool(self):
        pool = k.to_checksum_address(self.POOL)
        owner = k.to_checksum_address("0x" + "0c" * 20)
        for code, named, want in (("0x", pool, None), ("0x6080", pool, owner), ("0x6080", OTHER, None)):
            views = {"owner()": owner, "pool()": named}
            with mock.patch.object(k, "view", lambda a, s, o, *x: views[s]), \
                    mock.patch.object(k.c, "rpc", lambda m, p=(): code):
                self.assertEqual(k.shares_of(pool), want)

        def no_pool(a, s, o, *x):
            if s == "pool()":
                raise RuntimeError("execution reverted")
            return owner

        with mock.patch.object(k, "view", no_pool), mock.patch.object(k.c, "rpc", lambda m, p=(): "0x6080"):
            self.assertIsNone(k.shares_of(pool), "a contract with no pool() is not PoolShares")


class Scanning(unittest.TestCase):
    def test_exams_are_found_window_by_window_and_a_refusal_keeps_the_place(self):
        exam = k.to_checksum_address("0x" + "0" * 38 + "aa")
        windows = []

        def rpc(method, params=()):
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            windows.append((lo, hi))
            if lo >= 201:
                raise RuntimeError("rate limited")
            topics = [k.EXAM_SOLD, "0x" + "0" * 24 + exam[2:].lower(), "0x" + "0" * 64]
            return [{"topics": topics}] if lo == 101 else []

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(k.c, "rpc", rpc), mock.patch.object(k, "log"):
            kp = k.MultiKeeper("0x" + "11" * 20, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1, window=50,
                               max_windows=10)
            kp.scan(10_000)
            self.assertEqual(windows[:5], [(1, 50), (51, 100), (101, 150), (151, 200), (201, 250)])
            self.assertEqual((kp.exams, kp.next_block), ([exam], 201))
            again = k.MultiKeeper("0x" + "11" * 20, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1)
            self.assertEqual((again.exams, again.next_block), ([exam], 201), "kept on disk")

    def test_no_more_than_max_windows_a_pass(self):
        windows = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(k.c, "rpc", lambda m, p=(): windows.append(p) or []), mock.patch.object(k, "log"):
            kp = k.MultiKeeper("0x" + "11" * 20, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1, window=50,
                               max_windows=3)
            kp.scan(10_000)
        self.assertEqual((len(windows), kp.next_block), (3, 151))


class TheRound(unittest.TestCase):
    def run_renew(self, now, exit_shares, shares="0x" + "cc" * 20, refused=False, logged=None):
        sent = []
        views = {"notice()": 14 * DAY, "exitShares()": exit_shares}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(k, "view", lambda a, s, o, t=(), g=(): views[s]), \
                mock.patch.object(k, "log", lambda event, **f: (logged if logged is not None else []).append((event, f))):
            kp = keeper(NOTER, tmp=tmp)
            kp.shares = shares
            with mock.patch.object(kp, "send", lambda to, sig, *x: sent.append((to, sig)) or not refused):
                kp.renew_pass(now, NOW + 14 * DAY)
        return sent

    def test_a_round_the_pool_will_not_close_yet_is_said_with_the_time_left(self):
        logged = []
        self.run_renew(NOW + 1, 5, refused=True, logged=logged)
        self.assertEqual(logged, [("round_not_closed", {"shares": "0x" + "cc" * 20, "ends": NOW + 14 * DAY,
                                                        "left_s": 14 * DAY - 1})])
        logged = []
        self.run_renew(NOW + 1, 5, logged=logged)
        self.assertEqual(logged, [])

    def test_renew_in_the_window_when_somebody_asked_to_leave(self):
        self.assertEqual(self.run_renew(NOW + 1, 5), [("0x" + "cc" * 20, "renew()")])
        self.assertEqual(self.run_renew(NOW, 5), [], "before the window")
        self.assertEqual(self.run_renew(NOW + 1, 0), [], "nobody leaving")
        self.assertEqual(self.run_renew(NOW + 1, 5, shares=None), [], "an owner that is not PoolShares")


if __name__ == "__main__":
    unittest.main()
