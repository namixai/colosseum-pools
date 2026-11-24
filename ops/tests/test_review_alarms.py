"""No silence around the payout review (ops/review_service.py, ops/stop_filler.py): an alarm is remembered only once the
channel took it, a held payout is said again every 6 hours and 24 and 6 hours before its ceiling, a review that stops
running is said by the stops' loop, a failed unit by its OnFailure, and a second journal that stops coming is said."""
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ops import payout_review as pr
from ops import review_service as rs
from ops import stop_filler as sf
from ops.tests import test_stop_filler as S
from ops.tests.test_payout_review import POOL, honest_world
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
            # Review 15 of #338 (M4): the flag file holds it, and nobody reads it: not taken, said again.
            self.assertFalse(sf.Alarm(self.dir / "f", "")("x", wait=True), "no channel: written down, not delivered")
            self.assertIn('"x"', (self.dir / "f").read_text())
            self.assertFalse(sf.Alarm(None, "")("x", wait=True), "no channel and no flag: nobody has it")

    def test_a_channel_of_spaces_alone_is_no_channel(self):
        """`sh -c "   "` runs and says nothing failed: taken for a channel, every alarm is 'delivered' to nobody and
        remembered as said. A command of spaces alone, given or in COLOSSEUM_ALERT_CMD, is no channel wherever the
        alarm asks after one: not taken with `wait`, no thread without it, nothing run, and said once as no channel."""
        for blank in (" ", "   ", "\t\n", " \n  "):
            for given in (True, False):
                with mock.patch.object(sf, "log") as log, mock.patch.object(sf.subprocess, "run") as ran, \
                        mock.patch.object(sf.threading, "Thread") as thread, \
                        mock.patch.dict(os.environ, {"COLOSSEUM_ALERT_CMD": blank}):
                    alarm = sf.Alarm(self.dir / "f", blank) if given else sf.Alarm(self.dir / "f")
                    self.assertIs(alarm("x", wait=True), False, (blank, given))
                    self.assertIsNone(alarm("x"), (blank, given))
                ran.assert_not_called()
                thread.assert_not_called()
                said = [call.args[0] for call in log.call_args_list]
                self.assertEqual((said.count("stop_alarm_no_channel"), said.count("stop_alarm_sent")), (1, 0), (blank, given))
        self.assertEqual((self.dir / "f").read_text().count('"x"'), 16, "each is still written to the flag file")
        # A command with spaces around it is the channel it names.
        with mock.patch.object(sf, "log"):
            self.assertTrue(sf.Alarm(self.dir / "f", "  cat >/dev/null \n")("x", wait=True))

    def test_on_mainnet_the_review_and_the_stops_loop_do_not_start_without_a_channel(self):
        """Review 15 of #338 (M4): on mainnet, no COLOSSEUM_ALERT_CMD is a refusal at the start, loud; testnet as before."""
        from ops.tests.test_sim_network import on
        from spike.hlspike import network as networks
        loud = mock.patch.object(rs.deployments, "load", side_effect=AssertionError("started"))
        for env in ({"COLOSSEUM_ALERT_CMD": ""}, {"COLOSSEUM_ALERT_CMD": "  "}):
            with on(networks.MAINNET), mock.patch.dict(os.environ, env), loud:
                with self.assertRaises(SystemExit) as caught:
                    sf.require_channel("the stops' loop")
                self.assertIn("COLOSSEUM_ALERT_CMD is not set", str(caught.exception))
                with mock.patch.object(rs.sn, "confirm_service"), self.assertRaises(SystemExit) as caught:
                    rs.main(["--deployment", "sim1", "--out", str(self.dir), "--network", "mainnet",
                             "--confirm-mainnet", "sim1"])
                self.assertIn("refusing to run the payout review on mainnet", str(caught.exception))
                with self.assertRaises(SystemExit):
                    sf.build(mock.Mock(), None, self.dir / "s.json")
        with on(networks.MAINNET), mock.patch.dict(os.environ, {"COLOSSEUM_ALERT_CMD": "cat >/dev/null"}):
            sf.require_channel("the stops' loop")
        with on(networks.TESTNET), mock.patch.dict(os.environ, {"COLOSSEUM_ALERT_CMD": ""}):
            sf.require_channel("the stops' loop")


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


class FundingStillRead(unittest.TestCase):
    """Each new buyer has the funding of every buyer read again, and until it is read to the end every payout waiting is
    held as not read (`payout_review.LINKS_UNREAD`). A person can do nothing about that hold: its alarm waits until it
    has lasted over an hour without a break. The hold itself does not wait, and nothing waits near the ceiling."""
    KEY = f"{POOL}-0"

    def setUp(self):
        self.out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.ch, self.trader, self.stage = honest_world()
        self.ch.d["chainId"] = 998
        self.start = self.ch.d["now"]
        self.hard = self.ch.d["payouts"][0]["dueAt"] - pr.PAYOUT_DELAY + pr.PAYOUT_HARD  # when it goes out by itself
        self.said, self.got, self.took = {}, [], True

    def at(self, minutes, links=True) -> dict:
        """One pass `minutes` after the first, the alarms' memory read from said.json and written back, as the service
        does; the review of the payout."""
        self.ch.d["now"] = self.start + minutes * 60
        self.ch.d["linksUnread"] = [self.trader] if links else []
        with mock.patch.object(rs.pr, "read_chain", return_value=self.ch.d), mock.patch.object(rs.core, "log"):
            reviews = rs.one_pass({}, self.out, None, self.said,
                                  lambda event, **f: (self.got.append((minutes, event, f)), self.took)[1])
        rs.save_said(self.out / "said.json", self.said)
        self.said = rs.load_said(self.out / "said.json")
        return reviews[0]

    def alarms(self) -> list:
        return [(minutes, event) for minutes, event, _ in self.got]

    def test_the_hold_is_in_the_document_at_once_and_its_alarm_comes_once_it_has_lasted_over_an_hour(self):
        for minutes in range(0, 70, 10):
            r = self.at(minutes)
            self.assertEqual((r["verdict"], r["action"], r["kinds"]), ("suspicious", "hold", [pr.LINKS_UNREAD]), minutes)
            doc = json.loads((self.out / f"{self.KEY}.json").read_text())
            self.assertEqual((doc["action"], doc["reasons"][0]["kind"], len(doc["reasons"])), ("hold", "links-unread", 1))
            pay = self.ch.d["payouts"][0]
            why = pr.card_problem(doc, POOL, 0, pay, 998, self.ch.d["latest"], self.ch.d["now"])
            self.assertIn("the review says 'hold'", why, "no approval rests on it, from the first pass")
        self.assertEqual(self.alarms(), [], "an hour of it: nothing a person could do")
        self.assertEqual(self.said[self.KEY], {"linksSince": self.start}, "the pass that first saw it, kept in said.json")
        self.at(70)
        self.assertEqual(self.alarms(), [(70, "payout_held")])
        self.assertEqual((self.got[0][2]["kinds"], self.got[0][2]["paysAt"]), ([pr.LINKS_UNREAD], self.hard))
        self.assertEqual(self.said[self.KEY]["linksSince"], self.start)
        # Said, it is repeated as any other held payout: every six hours, not at every pass and not after another hour.
        for minutes in (80, 150, 70 + 6 * 60 - 10):
            self.at(minutes)
        self.assertEqual(self.alarms(), [(70, "payout_held")])
        self.at(70 + 6 * 60)
        self.assertEqual(self.alarms(), [(70, "payout_held"), (430, "payout_held")])

    def test_status_json_says_how_far_the_reading_is_in_two_numbers_and_nothing_else_of_it(self):
        self.ch.d["linksLeft"] = {"buyers": 3, "blocks": 12_500, "of": [self.trader]}
        self.at(0)
        text = (self.out / "status.json").read_text()
        status = json.loads(text)
        self.assertEqual(status["linksLeft"], {"buyers": 3, "blocks": 12_500})
        self.assertEqual((status["due"], status["held"]), (1, 1))
        self.assertNotIn("0x", text, "numbers, no address")
        self.assertLess(len(text), 4_096, "the index's pulse reads its time only from a file under 4 KB")
        for none in ({}, None, {"buyers": "many"}, {"buyers": 1}, [3, 4]):
            self.ch.d["linksLeft"] = none
            self.at(10)
            self.assertIsNone(json.loads((self.out / "status.json").read_text())["linksLeft"], none)
        del self.ch.d["linksLeft"]
        self.at(20)
        self.assertIsNone(json.loads((self.out / "status.json").read_text())["linksLeft"], "a reading that gave none")

    def test_the_kind_has_words_of_its_own_on_the_route_the_page_reads(self):
        from gateway import simindex
        self.assertIn(pr.LINKS_UNREAD, simindex.REASON_WORDS)
        self.assertNotEqual(simindex.REASON_WORDS[pr.LINKS_UNREAD], simindex.REASON_WORDS["unread"])

    def test_read_to_the_end_within_the_hour_it_is_no_alarm_and_a_later_hold_starts_its_own_hour(self):
        for minutes in (0, 10, 20):
            self.at(minutes)
        self.assertEqual(self.at(30, links=False)["action"], "approve")
        self.assertIsNone(self.said[self.KEY])
        for minutes in range(40, 110, 10):  # the next buyer: held again from minute 40, an hour of it at minute 100
            self.at(minutes)
        self.assertEqual(self.alarms(), [], "the first hold's half hour is not counted into the second")
        self.at(110)
        self.assertEqual(self.alarms(), [(110, "payout_held")])

    def test_another_reason_beside_it_is_said_at_once(self):
        self.ch.d["unread"].append(self.stage)  # the account's own events not read: a reason a person looks at
        r = self.at(0)
        self.assertEqual((r["action"], r["kinds"]), ("hold", ["unread", pr.LINKS_UNREAD]))
        self.assertEqual(self.alarms(), [(0, "payout_held")])

    def test_another_reason_that_joins_or_takes_its_place_is_said_at_once_and_the_hour_starts_anew_after_it(self):
        self.at(0)
        self.at(10)
        self.ch.d["unread"].append(self.stage)
        self.at(20)
        self.assertEqual(self.alarms(), [(20, "payout_held")], "another reason joined: said in that pass")
        self.at(30, links=False)
        self.assertEqual(self.alarms(), [(20, "payout_held"), (30, "payout_held")], "the reasons changed: said again")
        self.ch.d["unread"].remove(self.stage)
        for minutes in range(40, 110, 10):  # held by the funding alone again from minute 40
            self.at(minutes)
        self.assertEqual(len(self.alarms()), 2, "an hour of it alone, counted from minute 40")
        self.at(110)
        self.assertEqual(self.alarms()[2:], [(110, "payout_held")])

    def test_another_reason_whose_own_alarm_did_not_go_out_ends_the_hour_all_the_same(self):
        """The hour is of the hold by the funding alone, without a break. Another reason breaks it whether or not the
        channel took that reason's alarm: the minutes before it are not counted when the funding is alone again."""
        self.at(0)
        self.ch.d["unread"].append(self.stage)
        self.took = False
        self.at(30)
        self.assertEqual(self.alarms(), [(30, "payout_held")], "said at once; the channel did not take it")
        self.assertNotIn("linksSince", self.said[self.KEY])
        self.ch.d["unread"].remove(self.stage)
        self.took = True
        for minutes in range(70, 140, 10):  # the funding alone again from minute 70: an hour of it at minute 130
            self.at(minutes)
        self.assertEqual(self.alarms(), [(30, "payout_held")], "counted from minute 70, not from minute 0")
        self.assertEqual(self.said[self.KEY], {"linksSince": self.start + 70 * 60})
        self.at(140)
        self.assertEqual(self.alarms()[1:], [(140, "payout_held")])

    def test_from_24_hours_before_it_goes_out_by_itself_nothing_waits(self):
        day_before = (self.hard - self.start - 24 * H) // 60  # the minute at which 24 hours are left
        self.at(day_before)
        self.assertEqual(self.alarms(), [(day_before, "payout_held")], "first seen with 24 hours left: said at once")

    def test_a_wait_ends_as_24_hours_are_left_and_the_alarm_six_hours_before_is_said_too(self):
        day_before = (self.hard - self.start - 24 * H) // 60
        for minutes in (day_before - 30, day_before - 20, day_before - 10):
            self.at(minutes)
        self.assertEqual(self.alarms(), [], "half an hour of it, more than 24 hours left")
        self.at(day_before)
        self.assertEqual(self.alarms(), [(day_before, "payout_held")], "24 hours left: said, the hour not waited out")
        self.at(day_before + 10)
        self.at(day_before + 17 * 60)       # more than six hours after the last: said again
        self.at(day_before + 18 * 60 - 10)  # fifty minutes later: nothing new
        self.at(day_before + 18 * 60)       # six hours left: said, an hour after the last
        self.assertEqual([m - day_before for m, _ in self.alarms()], [0, 17 * 60, 18 * 60])

    def test_a_payout_that_pays_by_itself_is_said_at_once_whatever_is_still_being_read(self):
        self.ch.d["books"][POOL]["approvals"] = False  # no approvals: it goes out at its due time unless stopped
        self.ch.d["payouts"][0]["dueAt"] = self.start + 48 * H  # two days away: it is not its nearness that says it
        r = self.at(0)
        self.assertEqual((r["verdict"], r["kinds"]), ("suspicious", [pr.LINKS_UNREAD]))
        self.assertNotEqual(r["action"], "hold")
        self.assertEqual(self.alarms(), [(0, "payout_needs_a_stop")])
        self.assertEqual(self.got[0][2]["paysAt"], self.start + 48 * H)

    def test_one_the_channel_did_not_take_after_the_hour_is_said_at_the_next_pass(self):
        self.at(0)
        self.took = False
        self.at(70)
        self.at(80)
        self.took = True
        self.at(90)
        self.at(100)
        self.assertEqual(self.alarms(), [(70, "payout_held"), (80, "payout_held"), (90, "payout_held")])


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
        host = (d / "colosseum-host-watch@.service").read_text()
        self.assertIn("-m ops.review_service --watch-host ${COLOSSEUM_HOST_HEALTH_URL}", host)
        self.assertIn("EnvironmentFile=/etc/colosseum/price-watch-%i.env", host)
        self.assertIn("OnUnitActiveSec=1min", (d / "colosseum-host-watch@.timer").read_text())
        self.assertIn("Environment=SIM_INDEX_KEEPER_DIR=/var/lib/colosseum-keeper",
                      (ROOT / "ops/host/colosseum-sim-index@.service").read_text())


class HostWatch(unittest.TestCase):
    """Review 15 of #338: the second machine reads the host's pulse once a minute and says on its own channel that the
    host does not answer, or its stops' journal or its review stopped; again every hour while the same."""

    NOW = 1_800_000_000

    def look(self, body, state, took=True, now=None):
        said = []
        fetch = (lambda url: body) if not isinstance(body, Exception) else (lambda url: (_ for _ in ()).throw(body))
        with mock.patch.object(rs.core, "log"):
            got = rs.watch_host("https://pools.example/v1/simindex/health", state,
                                lambda event, **f: (said.append((event, f)), took)[1], now or self.NOW, fetch)
        return got, said

    def pulse(self, prices=-5, review=-300):
        return {"now": self.NOW, "prices_at": self.NOW + prices, "review_at": self.NOW + review, "scanned_to": 9}

    def test_a_live_host_is_no_alarm(self):
        state = {}
        self.assertEqual(self.look(self.pulse(), state), (None, []))
        self.assertEqual(state, {})

    def test_silence_an_old_journal_or_an_old_review_is_an_alarm_said_again_every_hour(self):
        for body, kind in ((OSError("refused"), "silent"), (self.pulse(prices=-181), "prices"),
                           (self.pulse(review=-1_801), "review"), ({"now": self.NOW}, "prices")):
            state = {}
            why, said = self.look(body, state)
            self.assertEqual([(e, f["kind"]) for e, f in said], [("host_not_alive", kind)], body)
            self.assertNotIn("pools.example", json.dumps(said), "the route's address goes to no alarm")
            self.assertEqual(self.look(body, state, now=self.NOW + 60)[1], [], "not every minute")
            self.assertEqual(len(self.look(body, state, now=self.NOW + rs.HOST_REPEAT_S)[1]), 1, "again after an hour")
        state = {}
        self.look(self.pulse(prices=-181), state)
        self.assertEqual(len(self.look(OSError("x"), state, now=self.NOW + 60)[1]), 1, "another fault is said at once")
        self.assertEqual(self.look(self.pulse(), state, now=self.NOW + 120), (None, []))
        self.assertEqual(state, {}, "alive again: forgotten")

    def test_an_answer_stamped_far_from_this_machines_clock_is_an_alarm_and_one_not_taken_is_said_again(self):
        """Review 16 of #338: the answer's own time is held to this machine's clock. More than 120 s apart, either way,
        is an old answer (a cache, a replay) or a clock gone wrong: `host_not_alive`, kind clock, whatever ages it
        carries. Within it the ages are counted on the host's clock."""
        def stamped(apart, prices=-5, review=-300):
            return {"now": self.NOW + apart, "prices_at": self.NOW + apart + prices, "review_at": self.NOW + apart + review}
        for apart in (-10_000, 10_000, -86_400, -(rs.HOST_CLOCK_APART_S + 1), rs.HOST_CLOCK_APART_S + 1):
            why, said = self.look(stamped(apart), {})
            self.assertEqual([(e, f["kind"]) for e, f in said], [("host_not_alive", "clock")], apart)
            self.assertIn("before" if apart < 0 else "after", why)
        for apart in (-rs.HOST_CLOCK_APART_S, rs.HOST_CLOCK_APART_S, 0):
            self.assertEqual(self.look(stamped(apart), {})[1], [], apart)
        # The host 100 s behind this machine, its journal written 150 s before its own clock: by the host's clock 150 s
        # (no alarm), not the 250 s this machine's would count.
        self.assertEqual(self.look(stamped(-100, prices=-150), {})[1], [])
        self.assertEqual(self.look(stamped(100, prices=-181), {})[1][0][1]["kind"], "prices")
        for body in ({"prices_at": self.NOW - 5, "review_at": self.NOW - 300}, {**stamped(0), "now": True},
                     {**stamped(0), "now": float(self.NOW)}, {**stamped(0), "now": str(self.NOW)}):
            self.assertEqual(self.look(body, {})[1][0][1]["kind"], "clock", "no time of its own: not told from an old one")
        state = {}
        self.look(OSError("x"), state, took=False)
        self.assertEqual(state, {})
        self.assertEqual(len(self.look(OSError("x"), state, now=self.NOW + 60)[1]), 1)

    def test_each_question_carries_a_parameter_no_question_before_had(self):
        asked = []

        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return json.dumps({"now": 1}).encode()

        def urlopen(request, timeout=None):
            asked.append((request.full_url, request.get_header("Cache-control")))
            return Answer()
        with mock.patch("urllib.request.urlopen", urlopen):
            rs.fetch_health("https://pools.example/v1/simindex/health")
            rs.fetch_health("https://pools.example/v1/simindex/health")
            rs.fetch_health("https://pools.example/v1/simindex/health?x=1")
        urls = [u for u, _ in asked]
        self.assertRegex(urls[0], r"^https://pools\.example/v1/simindex/health\?r=[0-9a-f]{16}$")
        self.assertNotEqual(urls[0], urls[1], "a cache between the machines has no answer kept for it")
        self.assertRegex(urls[2], r"\?x=1&r=[0-9a-f]{16}$")
        self.assertEqual({h for _, h in asked}, {"no-cache"})
        from gateway import simindex as si
        self.assertIn('{**index.health(), **pulse.answer()}, cache="no-store")', pathlib.Path(si.__file__).read_text())

    def test_it_does_not_run_without_a_channel_on_any_network(self):
        """Review 16 of #338: the watch is nothing but its alarm. Its unit is testnet's unless its file says mainnet, so
        a channel asked for on mainnet alone let a line forgotten in that file pass: the watch ran and told nobody."""
        from ops.tests.test_sim_network import on
        from spike.hlspike import network as networks
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        argv = ["--watch-host", "https://pools.example/v1/simindex/health", "--out", str(d)]
        for net in (networks.MAINNET, networks.TESTNET):
            for blank in ("", "  ", "\t\n"):
                with on(net), mock.patch.dict(os.environ, {"COLOSSEUM_ALERT_CMD": blank}), \
                        mock.patch.object(rs, "watch_host") as watched, self.assertRaises(SystemExit) as caught:
                    rs.main(argv)
                watched.assert_not_called()
                self.assertIn("COLOSSEUM_ALERT_CMD is not set", str(caught.exception))
            with on(net), mock.patch.dict(os.environ, {"COLOSSEUM_ALERT_CMD": "true"}), \
                    mock.patch.object(rs, "fetch_health", return_value={**self.pulse(), "now": int(time.time()),
                                                                        "prices_at": int(time.time()) - 5,
                                                                        "review_at": int(time.time()) - 300}):
                self.assertEqual(rs.main(argv), 0)
        with on(networks.TESTNET), mock.patch.dict(os.environ, clear=False) as env, \
                mock.patch.object(rs, "watch_host") as watched, self.assertRaises(SystemExit):
            env.pop("COLOSSEUM_ALERT_CMD", None)
            rs.main(argv)
        watched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
