"""ops/multi_keeper.py: what is due on a pool of many traders, and the noter, which never lets a seat
that traded be closed as idle."""
import json
import pathlib
import re
import tempfile
import types
import unittest
from unittest import mock

import eth_abi

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
                    "if (term == 0 && !late) {"):
            self.assertIn(sig, SHARES)
        self.assertIn("int64 public fundedStart;", POOL)
        types = (ROOT / "src/Types.sol").read_text()
        self.assertRegex(types, r"struct Cancel \{\s*uint32 \w+;\s*uint64 \w+;\s*\}")
        self.assertEqual(k.STOP, "(address,(uint32,uint64)[],uint32[],bytes32)")
        self.assertNotIn("noteTrade", FREE)


class Exams(unittest.TestCase):
    def calls(self, status, over=False, **kw):
        return k.exam_calls("Sold", status, NOW, over, claim_window=3_600, **kw)

    def test_deep_N6_a_waiting_order_is_filled_by_the_keeper(self):
        self.assertEqual(self.calls("Active", pending=True, day=NOW // 86_400), ["fill"])
        self.assertEqual(self.calls("Active", pending=False, day=NOW // 86_400), [])
        self.assertEqual(self.calls("Active", pending=True, broken=True), ["breach"], "a broken rule first")
        exam = (ROOT / "src/exam/PaperExam.sol").read_text()
        self.assertIn("if (msg.sender == trader || msg.sender == agent) revert OwnOrder();", exam)
        self.assertIn("Order public pending;", exam)

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
                         ["breach"], "a broken rule first: the snapshot is refused while it stands")

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

    def test_deep_N2_an_armed_seat_unopened_past_the_claim_window_is_freed(self):
        # The contract frees it then, hands the trader the price back and the pool the size.
        late = dict(size=500_000_000, armed_at=NOW - 3_601, rest_until=NOW - 1, claim_window=3_600)
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW, False, spot=500_000_000, **late), ["open", "finish"],
                         "open first: a key may have come back")
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW, False, spot=0, **late), ["finish"], "no rearm then")
        self.assertEqual(k.seat_calls("Armed", k.IDLE, NOW, False, spot=500_000_000,
                                      **{**late, "armed_at": NOW - 3_600}), ["open"], "not past it yet")

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

    def send(self, to, sig, types=(), args=(), gas=None):
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


class PlanByVersion(unittest.TestCase):
    """A plan kept by a pool from before v3 (four-field Rules) reads by its own type, with no cap of
    its own for any asset; one of another shape is refused by name (audit window 2, В6-4)."""

    EXAM = (100_000_000, [1000], 86400, 1, 2, 3, 4, 5)
    TAIL = (1000, 8000, 2, 1, 4, 86400, 86400, 0, 0, 0, "0x" + "00" * 20, "0x" + "00" * 20, b"\x11" * 32,
            "0x" + "00" * 20)

    def read(self, type_, plan):
        blob = b"\x00" + eth_abi.encode([type_], [plan])
        with mock.patch.object(k, "view", lambda *a: "0x" + "22" * 20), \
                mock.patch.object(k.c, "rpc", lambda m, p=(): "0x" + blob.hex()):
            return k.plan_of(POOL_ADDR)

    def test_a_v3_plan_reads_with_each_assets_cap(self):
        rules = (300, 600, 1000, [0, 3], [500, 1000])
        plan = self.read(k.PLAN_T, ([(100_000_000, 5_000_000)], rules, self.EXAM, rules, *self.TAIL))
        self.assertEqual(plan["exam_rules"], (300, 600, 1000, (0, 3), (500, 1000)))

    def test_a_plan_from_before_v3_reads_with_no_caps(self):
        rules = (300, 600, 500, [0, 3])
        plan = self.read(k.PLAN_T_V2, ([(100_000_000, 5_000_000)], rules, self.EXAM, rules, *self.TAIL))
        self.assertEqual(plan["exam_rules"], (300, 600, 500, (0, 3), ()))
        self.assertEqual(plan["funded_rules"], (300, 600, 500, (0, 3), ()))
        self.assertEqual(plan["noter"], "0x" + "00" * 20)

    def test_a_plan_of_another_shape_is_refused_by_name(self):
        short = k.PLAN_T_V2.replace(",address,address,bytes32,address)", ",address,bytes32,address)")
        rules = (300, 600, 500, [0, 3])
        with self.assertRaisesRegex(SystemExit, "a plan of another version \\(17 words in its head"):
            self.read(short, ([(100_000_000, 5_000_000)], rules, self.EXAM, rules, *self.TAIL[:-3], *self.TAIL[-2:]))


class SeatStopFits(unittest.TestCase):
    """A seat's stop names what fits a small block, extra positions first (audit window 2, В6-3)."""

    def test_the_worst_seat_stop_is_cut_and_capped(self):
        kp = keeper(NOTER, tmp=tempfile.mkdtemp())
        cancels, extra = [(3, i) for i in range(1, 33)], list(range(100, 116))
        sent = []
        model = lambda to, sig, types, args: 1_430_000 + 27_200 * len(args[1]) + 59_000 * len(args[2])
        with mock.patch.object(k.core, "stop_args", lambda *a: (cancels, extra)), \
                mock.patch.object(kp, "gas", model), \
                mock.patch.object(kp, "send", lambda to, sig, types, args, gas=None: sent.append((args, gas)) or True), \
                mock.patch.object(k, "log"):
            self.assertTrue(kp.stop_call("hitTarget", SEAT, {}))
        [(args, gas)] = sent
        self.assertEqual((args[0], args[1], args[2]), (SEAT, [], extra[:6]))
        self.assertEqual(gas, k.core.SMALL_BLOCK_GAS)


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

    def seat(self, state, stage, spot=0, armed_at=NOW - 60, rest_until=0, wait_until=0, over=False):
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

    def test_deep_N2_an_open_refused_for_spoiled_keys_purges_them_and_opens(self):
        registry = "0x" + "4e" * 20
        answers = iter([False, True, True])

        def send(to, sig, *x):
            self.sent.append((to, sig))
            ok = next(answers)
            self.kp.last_refusal = None if ok else "TooManySpoiledKeys()"
            return ok

        info = (0, k.SEAT_STATE.index("Armed"), False, OTHER, NOW - 60, NOW - 1, 0, 0)
        with mock.patch.object(k.c, "call_view", lambda *a: info), mock.patch.object(k, "view", lambda *a, **kw: k.IDLE), \
                mock.patch.object(k.c, "core_spot_balance", lambda a, t: {"total": 500_000_000}), \
                mock.patch.object(self.kp, "send", send), mock.patch.object(self.kp, "registry", lambda: registry):
            self.kp.seat_pass(SEAT, NOW, False, {}, None)
        self.assertEqual(self.sent, [(self.kp.pool, "open(address)"), (registry, "purgeSpoiled(uint256)"),
                                     (self.kp.pool, "open(address)")])

    def test_deep_N2_an_open_refused_for_another_reason_purges_nothing(self):
        def send(to, sig, *x):
            self.sent.append((to, sig))
            self.kp.last_refusal = "NoFreeKey()"
            return False

        info = (0, k.SEAT_STATE.index("Armed"), False, OTHER, NOW - 60, NOW - 1, 0, 0)
        with mock.patch.object(k.c, "call_view", lambda *a: info), mock.patch.object(k, "view", lambda *a, **kw: k.IDLE), \
                mock.patch.object(k.c, "core_spot_balance", lambda a, t: {"total": 500_000_000}), \
                mock.patch.object(self.kp, "send", send):
            self.kp.seat_pass(SEAT, NOW, False, {}, None)
        self.assertEqual(self.sent, [(self.kp.pool, "open(address)")])

    def test_deep_N3_keys_short_is_said_on_every_pass(self):
        logs = []
        for free, held, said in ((3, 3, True), (4, 3, False), (0, 0, True)):
            reads = {"freeCount()": free, "held()": held}
            with mock.patch.object(k, "view", lambda a, sig, *x: reads[sig]), \
                    mock.patch.object(self.kp, "registry", lambda: "0x" + "4e" * 20), \
                    mock.patch.object(k, "log", lambda ev, **kw: logs.append(ev)):
                self.kp.keys_pass()
            self.assertEqual(logs.count("keys_short"), 1 if said else 0, (free, held))
            logs.clear()

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
            if sig == "pending()":
                return (3, True, reads.get("pending_sz", 0), 1)
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
        self.sent = []
        self.assertEqual(self.exam("Sold", "Active", pending_sz=100), (True, [(self.EXAM, "fill()")]))

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


POOL_ADDR = k.to_checksum_address("0x" + "11" * 20)
READER = k.to_checksum_address("0x" + "4f" * 20)
SHARES_ADDR = k.to_checksum_address("0x" + "cc" * 20)


class Scanning(unittest.TestCase):
    def test_exams_and_holders_are_found_window_by_window_and_a_refusal_keeps_the_place(self):
        exam = k.to_checksum_address("0x" + "0" * 38 + "aa")
        holder = k.to_checksum_address("0x" + "0" * 38 + "bb")
        seeded = k.to_checksum_address("0x" + "0" * 38 + "cc")
        windows = []

        def topic(a):
            return "0x" + "0" * 24 + a[2:].lower()

        def rpc(method, params=()):
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            windows.append((lo, hi))
            self.assertEqual(q["address"], [POOL_ADDR, SHARES_ADDR])
            self.assertEqual(q["topics"], [[k.EXAM_SOLD, k.DEPOSIT_RECOGNIZED, k.SEEDED]])
            if lo >= 201:
                raise RuntimeError("rate limited")
            if lo != 101:
                return []
            return [{"address": POOL_ADDR, "topics": [k.EXAM_SOLD, topic(exam), "0x" + "0" * 64]},
                    {"address": SHARES_ADDR, "topics": [k.DEPOSIT_RECOGNIZED, topic(holder), topic(exam)]},
                    {"address": SHARES_ADDR, "topics": [k.SEEDED, topic(seeded)]},
                    # The same topic from the other contract is not what it seems, and is passed over.
                    {"address": SHARES_ADDR, "topics": [k.EXAM_SOLD, topic(holder), "0x" + "0" * 64]}]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(k.c, "rpc", rpc), mock.patch.object(k, "log"):
            kp = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1, window=50,
                               max_windows=10)
            kp.shares = SHARES_ADDR
            kp.scan(10_000)
            self.assertEqual(windows[:5], [(1, 50), (51, 100), (101, 150), (151, 200), (201, 250)])
            self.assertEqual((kp.exams, kp.holders, kp.next_block), ([exam], [holder, seeded], 201))
            again = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1)
            self.assertEqual((again.exams, again.holders, again.next_block), ([exam], [holder, seeded], 201),
                             "kept on disk")

    def test_no_more_than_max_windows_a_pass(self):
        windows = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(k.c, "rpc", lambda m, p=(): windows.append(p) or []), mock.patch.object(k, "log"):
            kp = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1, window=50,
                               max_windows=3)
            kp.scan(10_000)
        self.assertEqual((len(windows), kp.next_block), (3, 151))
        self.assertEqual(windows[0][0]["address"], [POOL_ADDR], "no shares contract, the pool alone")


class TheRound(unittest.TestCase):
    END = NOW + 14 * DAY

    def run_round(self, now, exit_shares, reserve_for=None, over=False, refused=False, logged=None):
        sent = []
        views = {"notice()": 14 * DAY, "exitShares()": exit_shares,
                 "reserveFor()": self.END if reserve_for is None else reserve_for}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(k, "view", lambda a, s, o, t=(), g=(): views[s]), \
                mock.patch.object(k, "log", lambda event, **f: (logged if logged is not None else []).append((event, f))):
            kp = keeper(NOTER, tmp=tmp)
            kp.shares = SHARES_ADDR
            with mock.patch.object(kp, "send", lambda to, sig, *x: sent.append((to, sig)) or not refused):
                kp.round_pass(now, self.END, over)
        return sent

    def test_a_round_the_pool_will_not_close_yet_is_said_with_the_time_left(self):
        logged = []
        self.run_round(NOW + 1, 5, reserve_for=NOW - DAY, refused=True, logged=logged)
        self.assertEqual(logged, [("round_not_closed", {"shares": SHARES_ADDR, "ends": self.END,
                                                        "left_s": 14 * DAY - 1, "late": True})])
        logged = []
        self.run_round(NOW + 1, 5, reserve_for=NOW - DAY, logged=logged)
        self.assertEqual(logged, [])

    def test_no_renew_in_the_last_days_the_round_waits_for_its_end(self):
        # DECISIONS 103: the round is paid once the end it was asked for has passed.
        self.assertEqual(self.run_round(NOW + 1, 5), [], "in the last days")
        self.assertEqual(self.run_round(self.END, 5), [], "at the end itself")
        self.assertEqual(self.run_round(NOW + 1, 0, reserve_for=NOW - DAY), [], "nobody leaving")

    def test_renew_after_an_end_that_passed_with_the_round_open(self):
        # Asked out for an end ten days ago; the term renewed by itself to END.
        self.assertEqual(self.run_round(NOW, 5, reserve_for=NOW - 10 * DAY), [(SHARES_ADDR, "renew()")])
        self.assertEqual(self.run_round(NOW, 5, reserve_for=NOW), [], "the end asked for has not passed")

    def test_close_round_once_the_pool_has_stopped_and_ended(self):
        self.assertEqual(self.run_round(self.END + 1, 5, over=True), [(SHARES_ADDR, "closeRound()")])

    def test_the_decisions(self):
        self.assertEqual(k.round_calls(False, 0, True), [])
        self.assertEqual(k.round_calls(True, 1, True), ["closeRound"])
        self.assertEqual(k.round_calls(False, 1, True), ["renew"])
        self.assertEqual(k.round_calls(False, 1, False), [], "not in the last days, only after the end")
        self.assertTrue(k.is_late(NOW + 1, 1, NOW, NOW + 90 * DAY))
        self.assertFalse(k.is_late(NOW, 1, NOW, NOW + 90 * DAY), "the end itself is not past")
        self.assertFalse(k.is_late(NOW + 1, 0, NOW, NOW + 90 * DAY))
        self.assertFalse(k.is_late(NOW + 1, 1, NOW, NOW), "an end that did not move: the pool stopped")
        self.assertIn("return exitShares != 0 && block.timestamp > asked && asked < pool.endsAt();", SHARES)


class TheRaise(unittest.TestCase):
    def test_the_decisions(self):
        self.assertEqual(k.raise_calls(True, NOW, NOW, 0, 0, 0, 0), [])
        self.assertEqual(k.raise_calls(False, NOW, NOW, 100, 100, 0, 0), ["launch"])
        self.assertEqual(k.raise_calls(False, NOW, NOW, 99, 100, 0, 0), [])
        self.assertEqual(k.raise_calls(False, NOW + 1, NOW, 100, 100, 0, 0), ["refund"], "the thirty days are over")
        self.assertEqual(k.raise_calls(False, NOW + 1, NOW, 0, 100, 5, 3), ["reclaim", "refund", "payFee"])
        self.assertIn("if (block.timestamp > raiseEnds) revert RaiseOver();", SHARES)
        self.assertIn("if (v < target) revert RaiseShort(v, target);", SHARES)
        self.assertIn("if (launched || block.timestamp <= raiseEnds) revert NotRefunding();", SHARES)

    def test_tickets_named(self):
        self.assertEqual(k.tickets_due({"a": 5, "b": 4, "c": 6}, 5), ["a", "c"])
        self.assertEqual(len(k.tickets_due({str(i): 9 for i in range(20)}, 5)), k.MAX_PER_POINT)
        self.assertIn("uint256 public constant MAX_PER_POINT = 8;", SHARES)
        self.assertIn("uint64 public constant SWEEP_MIN = 1e8;", SHARES)
        self.assertEqual(k.SWEEP_MIN, 10**8)
        for sig in ("event DepositRecognized(address indexed depositor, address indexed ticket, uint256 amount, uint256 shares);",
                    "event Seeded(address indexed creator, uint256 shares);", "function launch() external bound {",
                    "function refund(address holder) external bound {", "function reclaim() external bound {",
                    "function payFee() external bound {", "function closeRound() external bound {",
                    "function settle(address[] calldata recognize) external bound {", "function pay(address holder) external {",
                    "function closedTickets() external view", "function openTicketCount() external view",
                    "uint256 public refundShares;", "uint64 public raiseEnds;", "bool public launched;"):
            self.assertIn(sig, SHARES)
        self.assertIn("uint64 public reserveFor;", MULTI)


class SharesDispatch(unittest.TestCase):
    """One pass over PoolShares, from its reads: what is sent, and to whom."""
    ALICE = k.to_checksum_address("0x" + "a0" * 20)
    BOB = k.to_checksum_address("0x" + "b0" * 20)
    CREATOR = k.to_checksum_address("0x" + "c0" * 20)
    T1 = k.to_checksum_address("0x" + "71" * 20)

    def run_pass(self, launched=False, raise_ends=NOW - 1, value=0, target=100, pool_value=0, fee=0,
                 refund_shares=0, shares_of=None, due=None, ticket_balance=0, closed=(), exit_shares=0,
                 reserve_for=NOW + DAY):
        shares_of = shares_of or {}
        due = due or {}
        sent = []
        views = {"launched()": launched, "refundShares()": refund_shares, "raiseEnds()": raise_ends,
                 "value()": value, "target()": target, "feeOwed()": fee, "creator()": self.CREATOR,
                 "openTicketCount()": 1, "minDeposit()": 2 * 10**8, "closedTickets()": list(closed),
                 "exitShares()": exit_shares, "notice()": 14 * DAY, "reserveFor()": reserve_for, "endsAt()": NOW + DAY}

        def view(addr, sig, out, types=(), args=()):
            if sig == "value()":
                return pool_value if addr == POOL_ADDR else value
            if sig == "sharesOf(address)":
                return shares_of.get(args[0], 0)
            if sig == "openTickets(uint256,uint256)":
                return [self.T1]
            return views[sig]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(k, "view", view), \
                mock.patch.object(k.c, "call_view", lambda a, s, t, g, o: (0, 0, due.get(g[-1], 0))), \
                mock.patch.object(k.c, "core_spot_balance", lambda a, tok: {"total": ticket_balance}), \
                mock.patch.object(k, "log"):
            kp = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1, reader=READER)
            kp.shares = SHARES_ADDR
            kp.holders = [self.ALICE, self.BOB]
            with mock.patch.object(kp, "send", lambda to, sig, types=(), args=(): sent.append((sig, list(args))) or True):
                kp.shares_pass(NOW, NOW + DAY, False)
        return sent

    def test_a_raise_that_fell_short(self):
        # A round asked out for an end that has passed: a pool that never launched has none to close.
        sent = self.run_pass(pool_value=100, fee=3, shares_of={self.ALICE: 5}, due={self.BOB: 7}, exit_shares=5,
                             reserve_for=NOW - DAY)
        self.assertEqual(sent, [("reclaim()", []), ("refund(address)", [self.ALICE]), ("payFee()", []),
                                ("pay(address)", [self.BOB])])

    def test_a_raise_at_its_target_launches(self):
        self.assertEqual(self.run_pass(raise_ends=NOW, value=100), [("launch()", [])])

    def test_tickets_are_named_by_what_they_hold(self):
        sent = self.run_pass(raise_ends=NOW, ticket_balance=2 * 10**8)
        self.assertEqual(sent, [("settle(address[])", [[self.T1]])])
        self.assertEqual(self.run_pass(raise_ends=NOW, ticket_balance=2 * 10**8 - 1), [], "under minDeposit, nothing")
        # Once a raise that fell short took its price, what an open ticket holds is handed back, from
        # SWEEP_MIN on.
        sent = self.run_pass(refund_shares=1, ticket_balance=10**8)
        self.assertEqual(sent[0], ("settle(address[])", [[self.T1]]))
        # Closed tickets still listed are swept even with nothing to count.
        sent = self.run_pass(raise_ends=NOW, closed=[self.T1])
        self.assertEqual(sent, [("settle(address[])", [[]])])

    def test_a_launched_pool_closes_its_rounds_and_pays(self):
        sent = self.run_pass(launched=True, exit_shares=5, due={self.CREATOR: 1}, reserve_for=NOW - DAY)
        self.assertEqual(sent, [("renew()", []), ("pay(address)", [self.CREATOR])])
        sent = self.run_pass(launched=True, exit_shares=5, due={self.CREATOR: 1})
        self.assertEqual(sent, [("pay(address)", [self.CREATOR])], "in the last days the round waits for its end")



class OwnerAndTickets(unittest.TestCase):
    """The deep audit's F14 and F15 (16 October 2026): the keeper asks again whether the pool's owner
    is PoolShares until the node answers for sure, and its reads of tickets are bounded per pass."""

    def test_deep_F14_a_node_that_does_not_answer_is_not_a_no(self):
        pool = k.to_checksum_address("0x" + "11" * 20)
        owner = k.to_checksum_address("0x" + "0c" * 20)

        def busy(a, s, o, *x):
            if s == "pool()":
                raise RuntimeError("eth_call: rate limited 6 times in a row")
            return owner

        with mock.patch.object(k, "view", busy), mock.patch.object(k.c, "rpc", lambda m, p=(): "0x6080"):
            with self.assertRaises(RuntimeError):
                k.shares_of(pool)

    def test_deep_F14_the_owner_is_asked_again_until_it_is_known(self):
        logged, answers = [], [RuntimeError("rate limited"), SHARES_ADDR]
        served = []

        def shares_of(pool):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        with tempfile.TemporaryDirectory() as tmp:
            kp = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1)
            views = {"endsAt()": NOW + DAY, "seats()": []}
            scans = []
            with mock.patch.object(k, "plan_of", lambda pool: {"noter": NOTER}), \
                    mock.patch.object(k, "shares_of", shares_of), mock.patch.object(kp, "scan", lambda latest: scans.append(latest)), \
                    mock.patch.object(k, "view", lambda a, s, o, *x: views[s]), \
                    mock.patch.object(k.c, "rpc", lambda m, p=(): hex(100)), \
                    mock.patch.object(k.core, "perp_index_by_name", lambda: {}), \
                    mock.patch.object(kp, "shares_pass", lambda *a: served.append(kp.shares)), \
                    mock.patch.object(k, "log", lambda event, **f: logged.append(event)):
                kp.one_pass()
                self.assertEqual((kp.owner_known, kp.shares, served), (False, None, []))
                self.assertIn("owner_unknown", logged)
                self.assertEqual(scans, [], "no scan before the owner is known: its holders' events would be passed over")
                kp.one_pass()
                self.assertEqual(scans, [100])
        self.assertEqual((kp.owner_known, kp.shares, served), (True, SHARES_ADDR, [SHARES_ADDR]))
        self.assertIn("owner_known", logged)

    def test_deep_F15_a_flood_of_tickets_costs_a_bounded_number_of_reads_a_pass(self):
        flood = [k.to_checksum_address("0x" + f"{i:040x}") for i in range(1, 5001)]
        reads = {"pages": 0, "balances": 0}
        real = flood[4000]

        def view(addr, sig, out, types=(), args=()):
            if sig == "openTicketCount()":
                return len(flood)
            if sig == "openTickets(uint256,uint256)":
                reads["pages"] += 1
                return flood[args[0]:args[0] + args[1]]
            return {"minDeposit()": 10**8, "closedTickets()": []}[sig]

        def balance(t, token):
            reads["balances"] += 1
            return {"total": 5 * 10**8 if t == real else 0}

        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            kp = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1)
            kp.shares = SHARES_ADDR
            with mock.patch.object(k, "view", view), mock.patch.object(k.c, "core_spot_balance", balance), \
                    mock.patch.object(kp, "send", lambda to, sig, types=(), args=(): sent.append(args)):
                for n in range(1, 200):
                    kp.passes = n
                    before = dict(reads)
                    kp.tickets_pass(False)
                    self.assertLessEqual(reads["pages"] - before["pages"], k.PAGES_PER_PASS)
                    self.assertLessEqual(reads["balances"] - before["balances"], k.NEW_PER_PASS + k.RECHECK_PER_PASS)
                    if sent:
                        break
        self.assertEqual(sent, [[[real]]], "the real deposit is found, a bounded pass at a time")


if __name__ == "__main__":
    unittest.main()


class Uncovered(unittest.TestCase):
    """The cloud audit's N12, N13 and N15, the audit's tests turned round (18 October 2026)."""

    def test_deep_N12_an_exam_whose_price_does_not_answer_is_still_expired(self):
        sent = []
        fake = types.SimpleNamespace(pool=POOL_ADDR, plan={"claim_window": 7 * 86400},
                                     send=lambda to, fn, *a, **kw: sent.append(fn))

        def call_view(addr, sig, *a, **kw):
            if sig.startswith("examInfo"):
                return (0, k.EXAM_STATE.index("Sold"), "0x" + "00" * 20, False)
            if sig == "pending()":
                return (0, False, 0, 0)
            return (k.EXAM_STATUS.index("Active"), "0x" + "33" * 20, 0, 0)

        def view(addr, sig, *a, **kw):
            if sig == "violation()":
                raise RuntimeError("execution reverted: NoPrice")
            if sig == "terms()":
                return (0, [], 0, 3600, 0, 0, 0, 0)
            if sig == "deadline()":
                return 1  # long past
            return 0

        with mock.patch.object(k.c, "call_view", call_view), mock.patch.object(k, "view", view), \
                mock.patch.object(k, "log"):
            k.MultiKeeper.exam_pass(fake, "0x" + "22" * 20, now=10 * 86400 + 3600, over=False)
        self.assertEqual(sent, ["expire()"], "expire reads no price, and is sent")

    def test_deep_N13_an_old_state_file_is_scanned_again_for_its_holders(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(k, "log"):
            p = pathlib.Path(d) / "state.json"
            p.write_text(json.dumps({"pool": POOL_ADDR, "next_block": 5_000_000, "exams": []}))
            kp = k.MultiKeeper(POOL_ADDR, None, POOL_ADDR, True, p, 1)
        self.assertEqual(kp.holders, [])
        self.assertEqual(kp.next_block, 1, "the blocks with the holders' deposits are read again")
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "state.json"
            p.write_text(json.dumps({"pool": POOL_ADDR, "next_block": 5_000_000, "exams": [], "holders": []}))
            kp = k.MultiKeeper(POOL_ADDR, None, POOL_ADDR, True, p, 1)
        self.assertEqual(kp.next_block, 5_000_000, "a file that keeps its holders keeps its place")

    def test_deep_N15_the_windows_are_the_chains(self):
        kp = keeper(NOTER, tmp=tempfile.mkdtemp())
        day = 20_000
        chain = day * 86400 + 60  # in the chain's checkpoint window; the host 15 minutes behind
        with mock.patch.object(k.c, "rpc", lambda m, p=(): {"timestamp": hex(chain)}):
            self.assertEqual(kp.chain_now(), chain)
        self.assertIn("checkpoint", k.exam_calls("Sold", "Active", chain, False, day=day - 1))

        def down(m, p=()):
            raise RuntimeError("node down")

        with mock.patch.object(k.c, "rpc", down), mock.patch.object(k, "log"), mock.patch.object(k.time, "time", lambda: 123.0):
            self.assertEqual(kp.chain_now(), 123, "the host's clock only when the node will not say")


class DueOf(unittest.TestCase):
    """What a holder is owed, read through the reader, or from the contract's getters for a record
    from before it (CTO, 18 October 2026)."""

    def test_through_the_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            kp = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1, reader=READER)
        seen = []
        with mock.patch.object(k.c, "call_view", lambda a, s, t, g, o: seen.append((a, s, g)) or (1, 2, 7)):
            self.assertEqual(kp.due_of(SHARES_ADDR, OTHER), 7)
        self.assertEqual(seen, [(READER, "positionOf(address,address)", [SHARES_ADDR, OTHER])])

    def test_from_the_getters_without_a_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            kp = k.MultiKeeper(POOL_ADDR, None, NOTER, False, pathlib.Path(tmp) / "s.json", 1)
        reads = {"owedOf(address)": 5, "exitOf(address)": 40, "exitRound(address)": 0, "round()": 1,
                 "roundValue(uint256)": 110, "roundShares(uint256)": 100}
        with mock.patch.object(k, "view", lambda a, sig, *x: reads[sig]):
            self.assertEqual(kp.due_of(SHARES_ADDR, OTHER), 5 + 40 * 110 // 100, "a closed round's part, as _roll counts it")
        reads["round()"] = 0
        with mock.patch.object(k, "view", lambda a, sig, *x: reads[sig]):
            self.assertEqual(kp.due_of(SHARES_ADDR, OTHER), 5, "an open round owes nothing yet")
