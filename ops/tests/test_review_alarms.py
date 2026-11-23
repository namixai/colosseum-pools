"""No silence around the payout review (ops/review_service.py, ops/stop_filler.py): an alarm is remembered only once the
channel took it, a held payout is said again every 6 hours and 24 and 6 hours before its ceiling, a review that stops
running is said by the stops' loop, a failed unit by its OnFailure, and a second journal that stops coming is said."""
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from ops import review_service as rs
from ops import stop_filler as sf
from ops.tests import test_stop_filler as S
from ops.tests.test_review_service import Held

ROOT = pathlib.Path(__file__).resolve().parents[2]
H = 3_600


class Channel(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_waited_for_the_channels_own_answer_is_returned(self):
        with mock.patch.object(sf, "log"):
            self.assertTrue(sf.Alarm(self.dir / "f", "cat >/dev/null")("x", wait=True))
            self.assertFalse(sf.Alarm(self.dir / "f", "exit 1")("x", wait=True))
            self.assertTrue(sf.Alarm(self.dir / "f", "")("x", wait=True), "no channel: the flag holds it")
            self.assertFalse(sf.Alarm(None, "")("x", wait=True), "no channel and no flag: nobody has it")


class Repeats(unittest.TestCase):
    hold = Held()

    def passes(self, times, ceiling_in, took=lambda: True):
        ch, _ = self.hold.world()
        start = ch.d["now"]
        said, got = {}, []
        with tempfile.TemporaryDirectory() as out, mock.patch.object(rs.pr, "read_chain", return_value=ch.d), \
                mock.patch.object(rs.core, "log"), mock.patch.object(rs, "stop_deadline", lambda r: start + ceiling_in):
            for at in times:
                ch.d["now"] = start + at
                rs.one_pass({}, pathlib.Path(out), None, said,
                            lambda event, **f: (got.append((at, event)), took())[1])
        return [a for a, _ in got]

    def test_said_again_every_six_hours_while_held(self):
        self.assertEqual(self.passes([0, H, 5 * H, 6 * H, 7 * H, 12 * H], ceiling_in=100 * H), [0, 6 * H, 12 * H])

    def test_said_again_as_its_ceiling_comes_within_24_and_6_hours(self):
        # The ceiling 29 h after the first pass: 24 h before it comes 5 h 10 min after the first alarm, 6 h before it
        # 5 h 10 min after the one of 18 h: each said although six hours have not passed since the last.
        got = self.passes([0, 5 * H + 600, 12 * H, 18 * H, 23 * H + 600, 23 * H + 1_200], ceiling_in=29 * H)
        self.assertEqual(got, [0, 5 * H + 600, 12 * H, 18 * H, 23 * H + 600])
        got = self.passes([0, 1 * H, 2 * H], ceiling_in=23 * H)
        self.assertEqual(got, [0], "within 24 h from the first: said with it, not again at once")

    def test_one_the_channel_did_not_take_is_said_at_every_pass_until_it_does(self):
        answers = iter([False, False, True, True])
        self.assertEqual(self.passes([0, 600, 1_200, 1_800], ceiling_in=100 * H, took=lambda: next(answers)),
                         [0, 600, 1_200])

    def test_the_held_payouts_alarm_says_when_it_goes_by_itself(self):
        ch, _ = self.hold.world()
        got = []
        with tempfile.TemporaryDirectory() as out, mock.patch.object(rs.pr, "read_chain", return_value=ch.d), \
                mock.patch.object(rs.core, "log"):
            rs.one_pass({}, pathlib.Path(out), None, {}, lambda event, **f: got.append(f))
            status = json.loads((pathlib.Path(out) / "status.json").read_text())
        self.assertIn("goes out by itself at its ceiling", got[0]["do"])
        self.assertIn("UTC", got[0]["do"])
        self.assertEqual((status["due"], status["held"]), (1, 1))


class ReviewAge(S.Base):
    """The stops' loop watches the review's status.json, from the keeper's process."""

    def status(self, age, due):
        path = self.dir / "status.json"
        path.write_text(json.dumps({"at": 1_000_000 + self.node.head - age, "due": due}))
        return path

    def test_a_review_older_than_30_minutes_with_payouts_waiting_is_an_alarm_said_again_after_6_hours(self):
        path = self.status(1_900, 1)
        loop = self.loop(review_status=path)
        loop._review_age()
        self.assertEqual(len(self.events("review_stale")), 1)
        self.node.head += 600
        loop._review_age()
        self.assertEqual(len(self.events("review_stale")), 1, "not every five minutes")
        self.node.head += 6 * H
        loop._review_age()
        self.assertEqual(len(self.events("review_stale")), 2)

    def test_fresh_or_quiet_is_no_alarm_and_missing_is(self):
        loop = self.loop(review_status=self.status(600, 3))
        loop._review_age()
        loop = self.loop(review_status=self.status(1_900, 0))
        loop._review_age()
        self.assertEqual(self.events("review_stale"), [], "no payout waiting at its last pass: 2 hours allowed")
        loop = self.loop(review_status=self.status(8_000, 0))
        loop._review_age()
        loop2 = self.loop(review_status=self.dir / "none.json")
        loop2._review_age()
        self.assertEqual(len(self.events("review_stale")), 2)


class Failures(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_a_failed_unit_is_said_once_an_hour(self):
        said = []
        with mock.patch.object(sf.Alarm, "__call__", lambda self_, event, **kw: said.append(kw["unit"])):
            rs.main(["--unit-failed", "payout-review-sim1", "--out", str(self.dir)])
            rs.main(["--unit-failed", "payout-review-sim1", "--out", str(self.dir)])
        self.assertEqual(said, ["payout-review-sim1"])

    def test_a_pass_that_fails_is_an_alarm_once_an_hour(self):
        raised = []
        for _ in range(2):
            with mock.patch.object(rs.pr, "read_chain", side_effect=RuntimeError("node down")), \
                    mock.patch.object(rs.deployments, "load", return_value={"SimPoolFactory": "0x" + "11" * 20, "block": 1}), \
                    mock.patch.object(rs.sn, "confirm_service"), mock.patch.object(rs, "fetch_candles", return_value=None), \
                    mock.patch.object(sf.Alarm, "__call__", lambda self_, event, **kw: raised.append(event)):
                with self.assertRaises(BaseException):
                    rs.main(["--deployment", "x", "--out", str(self.dir)])
        self.assertEqual(raised, ["review_failed"])

    def test_the_second_journal_not_coming_is_said_every_6_hours(self):
        path = self.dir / "watch-prices-sim1.jsonl"
        day = self.dir / "watch-prices-sim1-2026-11-24.jsonl"
        day.write_text("{}\n")
        os.utime(day, (1_000, 1_000))
        said, got = {}, []
        alarm = lambda event, **kw: got.append(event)  # noqa: E731
        rs.second_stale(path, 1_000 + 600, said, alarm)
        self.assertEqual(got, [])
        rs.second_stale(path, 1_000 + 3_600, said, alarm)
        rs.second_stale(path, 1_000 + 7_200, said, alarm)
        self.assertEqual(got, ["second_journal_stale"])
        rs.second_stale(path, 1_000 + 3_600 + 6 * H, said, alarm)
        self.assertEqual(len(got), 2)


class Units(unittest.TestCase):
    def test_the_review_unit_says_its_failures_and_reads_the_channel(self):
        unit = (ROOT / "ops/host/colosseum-payout-review@.service").read_text()
        self.assertIn("OnFailure=colosseum-unit-failed@%n.service", unit)
        self.assertIn("EnvironmentFile=-/etc/colosseum/watch.env", unit)
        self.assertIn("MemoryMax=", unit)
        self.assertIn("--prices2 /var/lib/colosseum-keeper/second-prices/watch-prices-${COLOSSEUM_DEPLOYMENT}.jsonl", unit)
        failed = (ROOT / "ops/host/colosseum-unit-failed@.service").read_text()
        self.assertIn("-m ops.review_service --unit-failed %i", failed)
        install = (ROOT / "ops/host/install.sh").read_text()
        self.assertIn('"$rel/ops/host/colosseum-unit-failed@.service"', install)

    def test_the_second_writer_runs_alone_with_no_key_and_its_journal_goes_to_the_host_by_ssh(self):
        d = ROOT / "ops/host/second-writer"
        watch = (d / "colosseum-price-watch@.service").read_text()
        self.assertIn("-m ops.stop_filler --deployment ${COLOSSEUM_DEPLOYMENT}", watch)
        self.assertIn("--network ${COLOSSEUM_NETWORK} --confirm-mainnet ${COLOSSEUM_CONFIRM_MAINNET}", watch)
        self.assertIn("COLOSSEUM_KEY_DIR=/var/lib/colosseum-prices/no-keys", watch)
        sync = (d / "colosseum-price-sync@.service").read_text()
        self.assertIn("rsync --append-verify", sync)
        self.assertIn("StrictHostKeyChecking=yes", sync)
        self.assertIn("OnUnitActiveSec=1min", (d / "colosseum-price-sync@.timer").read_text())


if __name__ == "__main__":
    unittest.main()
