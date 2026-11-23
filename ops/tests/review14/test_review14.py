"""Independent review of PR 338 (cp14): each test states the CORRECT behaviour; red = a defect.
Run: COLOSSEUM_KEY_DIR=<empty> COLOSSEUM_RPC_URL=http://127.0.0.1:9 python -m unittest ops.tests.review14.test_review14 -v"""
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

from ops import payout_review as r
from ops import review_service as rs
from ops import stop_filler as sf
from ops.tests import test_payout_review as T


def world(low, rows=None, drop=(), extra_lines=(), node=None, latest=2_000, events=None, reads=r.JOURNAL_CHECKS,
          second=None):
    """The MissedTouches world (a long, a stop at 950 000 armed at 1 003, carried out at 1 305), judged by a journal made
    of `rows` {block: px} (default: the truth), lines appended after it, and a node that answers with the truth."""
    mt = T.MissedTouches()
    ch, s1 = mt.world(low)
    if events is not None:
        ch.d["exam_events"][s1] = events
    truth = mt.prices(low, until=2_001)
    rows = truth if rows is None else rows
    lines = [json.dumps({"b": b, "t": 1_000 * b, "w": 0, "px": {"3": px}}) for b, px in sorted(rows.items())
             if b not in drop and 1_002 <= b <= 1_400]
    j = r.load_prices("\n".join(lines + list(extra_lines)) + "\n")
    if second is not None:  # the second writer's journal, from another machine and node
        j.add_text("\n".join(json.dumps({"b": b, "t": 1_000 * b, "w": 0, "px": {"3": px}}) for b, px in sorted(second.items())
                             if 1_002 <= b <= 1_400) + "\n", None, "second")
    asked = []

    def at(asset, block):
        asked.append(block)
        return (node or truth).get(block, mt.MARK)
    ch.d["journal"] = j
    ch.d["journalCheck"] = r.check_journal([s1], ch.d["exam_events"], j, latest, at, reads)
    return r.review(ch.d, T.POOL, 0), asked, mt


class JournalFaults(unittest.TestCase):
    def test_a_block_written_twice_with_two_prices_is_not_a_clean_journal(self):
        """Two instances, or a replay after a failed step, can write one block twice. The later line wins in `Journal.parse`
        and hides the touch of the first; a clean verdict needs the journal to agree with itself."""
        low = set(range(1_300, 1_306))
        _, asked, mt = world(low)
        unsampled = next(b for b in range(1_010, 1_300) if b not in asked)
        touch = {**mt.prices(low), unsampled: mt.LOW}
        dup = json.dumps({"b": unsampled, "t": 1_000 * unsampled, "w": 1, "px": {"3": mt.MARK}})
        got, _, _ = world(low, rows=touch, extra_lines=[dup], node={**mt.prices(low, until=2_001)})
        self.assertNotEqual((got["verdict"], got["action"]), ("clean", "approve"),
                            "block %d shows the stop reached in one line and not in the next: clean" % unsampled)

    def test_one_falsified_block_outside_the_sample_passes_as_clean(self):
        """The sample is the two ends, every journal hit and blocks spread evenly: 200 an account. A block where the
        journal shows no touch and the oracle had one is found only if it falls in the sample.

        Carried over to the design chosen after this review (no archive node): the node's sample is an optional third
        line, and the falsified block is caught by the second writer's journal, which read the oracle there itself."""
        low = set(range(1_300, 1_306))
        _, asked, mt = world(low)
        unsampled = next(b for b in range(1_010, 1_300) if b not in asked)
        truth = {**mt.prices(low, until=2_001), unsampled: mt.LOW}   # the oracle really was at 940 000 there
        got, asked2, _ = world(low, rows=mt.prices(low), node=truth, second=truth)  # the journal says the mark
        self.assertNotIn(unsampled, asked2)
        self.assertNotEqual((got["verdict"], got["action"]), ("clean", "approve"),
                            "the oracle was past the stop in block %d, the journal says not, and nothing asked" % unsampled)

    def test_a_valid_json_line_of_the_wrong_shape_does_not_take_the_whole_review_down(self):
        for bad in ('{"b": null}', '{"b": "x"}', '{"gap": [1]}', '{"gap": null}', '{"b": 5, "px": {"3": "x"}}',
                    '{"b": 5, "px": {"3": null}}', '{"b": 5, "t": "soon", "px": {}}', '{"b": 5, "px": [1]}'):
            try:
                r.load_prices('{"b": 4, "t": 1, "px": {"3": 7}}\n' + bad + '\n{"b": 6, "t": 2, "px": {"3": 7}}\n')
            except Exception as exc:  # noqa: BLE001
                self.fail(f"{bad!r} raises {type(exc).__name__}: one bad line stops every payout's review")

    def test_a_journal_that_is_empty_text_is_no_journal(self):
        got = r.load_prices("")
        self.assertNotEqual(got, {}, "")  # '' -> Journal, fine; a literal {} below is the old map
        mt = T.MissedTouches()
        ch, s1 = mt.world(set(range(1_300, 1_306)))
        ch.d["journal"] = r.load_prices("{}")  # a journal file holding '{}' is read as the old price map: journal None
        self.assertIsNone(ch.d["journal"] if isinstance(ch.d["journal"], r.Journal) else None)

    def test_a_journal_check_of_zero_reads_is_not_a_check(self):
        got, _, _ = world(set(range(1_300, 1_306)), reads=0)
        self.assertNotEqual((got["verdict"], got["action"]), ("clean", "approve"),
                            "--journal-checks 0 asks the node nothing and the journal is 'borne out'")

    def test_a_journal_that_starts_after_the_stop_was_armed_holds(self):
        got, _, _ = world(set(range(1_300, 1_306)), drop=set(range(1_003, 1_100)))
        self.assertEqual(got["action"], "hold")

    def test_a_touch_exactly_at_the_level_holds(self):
        mt = T.MissedTouches()
        prices = {**mt.prices(set(range(1_300, 1_306))), 1_150: mt.STOP}
        got, _, _ = world(set(range(1_300, 1_306)), rows=prices, node=prices)
        self.assertEqual(got["action"], "hold")


class LateButCarriedOut(unittest.TestCase):
    def test_a_stop_carried_out_two_hundred_blocks_after_the_touch_is_not_clean(self):
        """Reached from 1 100 and carried out at 1 305 at the price of 1 305: the whole stretch is 'the close's own'
        (`missed_touches` pops it) and `triggerLate` flags only against others' median with >= min_fills of each."""
        got, _, _ = world(set(range(1_100, 1_306)))
        self.assertEqual(got["missedTouch"]["stage"]["missed"], [])
        self.assertNotEqual(got["verdict"], "clean",
                            "a stop filled 205 blocks (about 3.5 minutes) after the oracle reached it, at the worse price of "
                            "the later block, passes with no reason")


class Scale(unittest.TestCase):
    def test_a_pass_after_one_day_of_chain_asks_for_a_bounded_number_of_logs(self):
        """`read_chain` reads every log from the deployment's block, in windows of keeper.MAX_LOG_WINDOW (50) blocks, for the
        pools, for each 100 accounts and for each trader's USDC: all again every 10 minutes (the unit's timeout is 540 s)."""
        windows = []
        traders = [T.addr(0x100 + k) for k in range(3)]
        word = lambda a: "0x" + "00" * 12 + a[2:].lower()
        sold = [{"address": T.POOL, "topics": [r.sk.EXAM_SOLD, word(T.addr(0x200 + k)), word(t), word(T.addr(0xB0))],
                 "data": "0x" + r.abi_encode(["uint256", "uint64", "uint64", "uint256", "bool", "bytes32"],
                                             [2, 1, 1_000, 0, False, b"\x11" * 32]).hex(),
                 "blockNumber": hex(11), "transactionHash": T.tx(k), "logIndex": "0x0"} for k, t in enumerate(traders)]

        def rpc(method, params=()):
            if method == "eth_chainId":
                return hex(998)
            if method == "eth_blockNumber":
                return hex(10 + 86_400)
            if method == "eth_getBlockByNumber":
                return {"timestamp": hex(1)}
            if method == "eth_getLogs":
                q = params[0]
                windows.append(1)
                return sold if q["address"] == [T.POOL] and q["topics"] and len(q["topics"][0]) > 3 else []
            raise AssertionError(method)
        with mock.patch.object(r.c, "rpc", side_effect=rpc), mock.patch.object(r.core, "view", return_value=[T.POOL]), \
                mock.patch.object(r.c, "call_view", side_effect=lambda to, sig, types, args, out: {
                    "balanceOf(address)": (0,), "menu()": ([(1, 1)],), "result()": (2, T.addr(1), 0, 0), "accountOf(address)": (2, 0, True),
                    "payoutsPerDay()": (3,), "PAYOUT_DAY()": (86_400,), "payRoom()": (3,), "nextPayAt()": (0,), "approver()": (T.addr(0xAB),),
                    "PAYOUT_HARD()": (8 * 86_400,), "PAYOUT_DELAY()": (72 * 3_600,)}.get(sig, (0,))):
            try:
                r.read_chain({"SimPoolFactory": T.addr(0xFA), "usdc": T.USDC, "block": 10})
            except Exception as exc:  # noqa: BLE001
                print("read_chain stopped:", type(exc).__name__, str(exc)[:80], "after", len(windows), "getLogs")
        # each trader's USDC history alone is 1 728 getLogs for one day; the unit allows 540 s: 5 400 calls at 0.1 s
        self.assertGreaterEqual(len(windows), 1_728, "the harness did not reach the transfers loop")
        self.assertLessEqual(len(windows), 5_400, f"{len(windows)} getLogs after ONE day of blocks (3 traders), growing "
                             "linearly: the pass cannot finish in 540 s")


class Alarms(unittest.TestCase):
    def test_an_alarm_whose_channel_failed_is_said_again(self):
        """`one_pass` records `said` the moment it calls the alarm; the channel runs in a thread and its failure is a log line."""
        mt = T.MissedTouches()
        ch, s1 = mt.world({1_150, *range(1_300, 1_306)})
        ch.d["payouts"][0]["state"] = "Due"
        sent = []
        alarm = sf.Alarm(None, command="exit 1")
        with mock.patch.object(sf.Alarm, "_send", side_effect=lambda self_line, where: sent.append(1)) if False else mock.patch.object(rs.pr, "read_chain", return_value=ch.d):
            said = {}
            real_send = sf.Alarm._send

            def failing(self_, line, where):
                sent.append(line)
                raise RuntimeError("channel down")
            with mock.patch.object(sf.Alarm, "_send", failing):
                for _ in range(3):
                    rs.one_pass({"x": 1}, pathlib.Path(tempfile.mkdtemp()), None, said, alarm)
                    time.sleep(0.05)
        self.assertGreaterEqual(len(sent), 2, "three passes, the channel down each time: the held payout was said once and never again")

    def test_a_held_payout_is_reminded_before_its_ceiling(self):
        mt = T.MissedTouches()
        ch, s1 = mt.world({1_150, *range(1_300, 1_306)})
        said, got = {}, []
        out = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.object(rs.pr, "read_chain", return_value=ch.d):
            for _ in range(5):
                rs.one_pass({"x": 1}, out, None, said, lambda event, **kw: got.append(event))
        self.assertGreaterEqual(len(got), 2, "five passes over a payout still held, with its ceiling nearer each time: one alarm in all")


if __name__ == "__main__":
    unittest.main()


class TheServiceItselfFails(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parents[3]

    def test_a_pass_that_fails_tells_somebody_or_the_unit_names_who_is_told(self):
        """The timer runs a oneshot unit: a node that does not answer, a journal line the parser trips on, a pass past
        TimeoutStartSec=540 each end it with no document written and no alarm. The documents on disk go stale in an hour, the
        payouts held earlier stay un-alarmed until their ceiling, and nothing says the service is down."""
        unit = (self.ROOT / "ops/host/colosseum-payout-review@.service").read_text()
        out = pathlib.Path(tempfile.mkdtemp())
        raised = []
        with mock.patch.object(rs.pr, "read_chain", side_effect=RuntimeError("eth_getLogs: node down")), \
                mock.patch.object(rs.deployments, "load", return_value={"SimPoolFactory": "0x" + "11" * 20, "block": 1}), \
                mock.patch.object(rs.sn, "confirm_service"), \
                mock.patch.object(rs.sf.Alarm, "__call__", lambda self_, event, **kw: raised.append(event)):
            try:
                rs.main(["--deployment", "x", "--out", str(out), "--flag", str(out / "ALARM")])
            except BaseException:  # noqa: BLE001
                pass
        flagged = (out / "ALARM").exists()
        self.assertTrue(raised or flagged or "OnFailure=" in unit,
                        "the review service failed: no alarm, no flag, and the unit has no OnFailure=")

    def test_the_review_has_a_watchdog_on_its_documents_age(self):
        names = " ".join(p.name for p in (self.ROOT / "ops/host/watchdogs").iterdir())
        text = " ".join(p.read_text() for p in (self.ROOT / "ops/host/watchdogs").glob("*.py"))
        self.assertTrue("review" in names or "reviews-" in text,
                        "no watchdog looks at the review documents' age (or the journal's), so a dead review service is silent")
