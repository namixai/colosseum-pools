"""The review's service (ops/review_service.py) on the made-up chains of ops/tests/test_payout_review.py: it writes a
document a payout and raises an alarm for each payout a person has to act on."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from ops import payout_review as r
from ops import review_service as svc
from ops import sim_check as chk
from ops.tests import test_payout_review as tpr
from ops.tests.test_payout_review import POOL, honest_world

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Held:
    """A payout whose stop the journal shows reached in block 1 150 and nobody carried out (`TheStopsJournal`)."""
    sj = tpr.TheStopsJournal()

    def world(self):
        ch, s1 = self.sj.mt.world({1_150, *range(1_300, 1_306)})
        j = self.sj.journal(self.sj.mt.prices({1_150, *range(1_300, 1_306)}))
        ch.d["journal"] = j
        truth = self.sj.mt.prices({1_150, *range(1_300, 1_306)}, until=2_001)
        ch.d["journalCheck"] = r.check_journal([s1], ch.d["exam_events"], j, 2_000, self.sj.node(truth))
        return ch, s1


class TheService(unittest.TestCase):
    hold = Held()

    def run_pass(self, ch, said=None):
        alarms = []
        with tempfile.TemporaryDirectory() as out, mock.patch.object(r, "read_chain", return_value=ch.d), \
                mock.patch.object(svc.core, "log"):
            reviews = svc.one_pass({}, pathlib.Path(out), None, {} if said is None else said,
                                   lambda event, **f: alarms.append((event, f)))
            docs = {p.name: json.loads(p.read_text()) for p in pathlib.Path(out).glob("0x*.json")}
        return reviews, docs, alarms

    def test_a_clean_payout_is_written_and_no_alarm_is_raised(self):
        ch, _, _ = honest_world()
        reviews, docs, alarms = self.run_pass(ch)
        self.assertEqual(list(docs), [f"{POOL}-0.json"])
        self.assertEqual((docs[f"{POOL}-0.json"]["schema"], docs[f"{POOL}-0.json"]["action"]), (r.SCHEMA, "approve"))
        self.assertEqual(alarms, [])

    def test_a_held_payout_is_an_alarm_once_while_it_stays_as_it_is(self):
        ch, _ = self.hold.world()
        said = {}
        _, docs, alarms = self.run_pass(ch, said)
        self.assertEqual(docs[f"{POOL}-0.json"]["action"], "hold")
        self.assertEqual([e for e, _ in alarms], ["payout_held"])
        self.assertIn("do not approve", alarms[0][1]["do"])
        self.assertEqual(alarms[0][1]["kinds"], ["missed-stop"])
        _, _, again = self.run_pass(ch, said)
        self.assertEqual(again, [], "said once while it stays as it is")

    def test_a_suspicious_payout_the_pool_pays_by_itself_is_an_alarm_with_its_deadline(self):
        for change, deadline in ((("books", "approvals", False), lambda d: d["dueAt"] - chk.MARGIN_S),
                                 (("payouts", "approved", True), lambda d: d["dueAt"]),
                                 (("books", "approvals", None), lambda d: d["dueAt"] - chk.MARGIN_S)):
            ch, _ = self.hold.world()
            where, key, value = change
            (ch.d["books"][POOL] if where == "books" else ch.d["payouts"][0])[key] = value
            _, docs, alarms = self.run_pass(ch)
            self.assertEqual([e for e, _ in alarms], ["payout_needs_a_stop"], change)
            self.assertEqual(alarms[0][1]["deadline"], deadline(ch.d["payouts"][0]), change)
            self.assertIn("stops it for reason 3", alarms[0][1]["do"])

    def test_a_payout_the_pool_would_not_stop_again_is_an_alarm_that_names_no_stop_and_no_deadline(self):
        """A payout stopped once and its stop lifted is due again, and the pool takes no second stop of it
        (`wasStopped`). The alarm stays; it does not send a person to a stop the pool refuses, with a deadline for it."""
        ch, _ = self.hold.world()
        ch.d["payouts"][0].update(approved=True, wasStopped=True)
        reviews, _, alarms = self.run_pass(ch)
        self.assertIs(reviews[0]["stoppable"], False)
        self.assertEqual([e for e, _ in alarms], ["payout_needs_a_stop"])
        said = alarms[0][1]
        self.assertIsNone(said["deadline"], "no deadline for a stop that cannot be sent")
        self.assertIn("the pool would refuse a stop of it now", said["do"])
        self.assertIn("it pays by itself (" + svc.utc(ch.d["payouts"][0]["dueAt"]) + ")", said["do"])
        self.assertNotIn("a person stops it", said["do"])
        # Held, not approved, and stopped once already: not approved, and no stop to send either.
        ch, _ = self.hold.world()
        ch.d["payouts"][0]["wasStopped"] = True
        reviews, _, alarms = self.run_pass(ch)
        self.assertEqual(([e for e, _ in alarms], reviews[0]["stoppable"]), (["payout_held"], False))
        self.assertIn("do not approve it", alarms[0][1]["do"])
        self.assertIn("the pool would refuse a stop of it now", alarms[0][1]["do"])
        self.assertNotIn("a person stops it", alarms[0][1]["do"])
        self.assertIsNone(alarms[0][1]["deadline"])
        # One the pool would still stop: told to stop it, with the deadline, as before.
        ch, _ = self.hold.world()
        reviews, _, alarms = self.run_pass(ch)
        self.assertIs(reviews[0]["stoppable"], True)
        self.assertIn("unless a person stops it for reason 3", alarms[0][1]["do"])
        self.assertEqual(alarms[0][1]["deadline"], svc.stop_deadline(reviews[0]))

    def test_the_alarm_names_a_draft_of_a_stop_only_where_the_review_made_one(self):
        """A draft of a stop of reason 3 is made for a result that does not count up or a stop in doubt. A payout
        suspicious for another reason (here: an account not read) has none, and the alarm does not point at one."""
        ch, _, s1 = honest_world()
        ch.d["unread"].append(s1)
        reviews, _, alarms = self.run_pass(ch)
        self.assertEqual((reviews[0]["verdict"], reviews[0]["stopDraft"]), ("suspicious", None))
        self.assertEqual(len(alarms), 1)
        self.assertNotIn("the draft is in the review", alarms[0][1]["do"])
        self.assertIn("the review made no draft of one", alarms[0][1]["do"])
        ch, _ = self.hold.world()
        reviews, _, alarms = self.run_pass(ch)
        self.assertIsNotNone(reviews[0]["stopDraft"])
        self.assertIn("(the draft is in the review)", alarms[0][1]["do"])

    def test_a_memory_of_alarms_that_is_not_an_object_is_no_memory_and_the_pass_goes_on(self):
        """said.json holding `[]` (or a word, a number, null) is JSON, and not what the pass keeps its alarms in: read
        as it is, this pass and every one after fails on it. It is no memory: the alarm is said, the file written anew."""
        ch, _ = self.hold.world()
        for bad in ("[]", '"x"', "5", "null", "{broken"):
            with tempfile.TemporaryDirectory() as d:
                (pathlib.Path(d) / "said.json").write_text(bad)
                raised = []
                with mock.patch.object(svc.pr, "read_chain", return_value=ch.d), mock.patch.object(svc.core, "log"), \
                        mock.patch.object(svc.deployments, "load", return_value={"SimPoolFactory": "0x" + "11" * 20, "block": 1}), \
                        mock.patch.object(svc.sn, "confirm_service"), \
                        mock.patch.object(svc.sf.Alarm, "__call__", lambda self_, event, **kw: raised.append(event)):
                    self.assertEqual(svc.main(["--deployment", "x", "--out", d, "--no-candles"]), 0, bad)
                self.assertEqual(raised, ["payout_held"], bad)
                kept = json.loads((pathlib.Path(d) / "said.json").read_text())
                self.assertIsInstance(kept, dict, bad)
                self.assertIn(f"{POOL}-0", kept, bad)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "said.json"
            path.write_text(json.dumps({"_failed": "x", "_second": None, f"{POOL}-0": {"mark": [], "at": 1, "before": []}}))
            self.assertEqual(svc.load_said(path), {f"{POOL}-0": {"mark": [], "at": 1, "before": []}},
                             "a time that is no number is forgotten, the rest kept")
            path.write_text(json.dumps({"_failed": 5.5, "_second": 7}))
            self.assertEqual(svc.load_said(path), {"_failed": 5.5, "_second": 7})

    def test_no_journal_to_read_is_no_journal_and_the_payout_with_a_stop_is_held(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(svc.core, "log") as said:
            self.assertIsNone(svc.read_prices(pathlib.Path(d) / "none.jsonl"))
            self.assertEqual(said.call_args[0][0], "prices_unread")
            (pathlib.Path(d) / "j.jsonl").write_text('{"b": 1, "px": {"3": 5}}\n')
            self.assertEqual(svc.read_prices(pathlib.Path(d) / "j.jsonl").px, {3: {1: 5}})
        ch, _ = self.hold.sj.mt.world(set(range(1_300, 1_306)))  # no journal at all
        _, docs, alarms = self.run_pass(ch)
        self.assertEqual(docs[f"{POOL}-0.json"]["action"], "hold")
        self.assertEqual(alarms[0][1]["kinds"], ["stop-unchecked"])

    def test_a_stop_the_checker_sends_itself_is_no_alarm(self):
        self.assertIsNone(svc.alarm_of({"verdict": "suspicious", "action": "stop"}))
        self.assertIsNone(svc.alarm_of({"verdict": "clean", "action": "approve"}))

    def test_the_host_runs_it_every_ten_minutes_and_install_puts_both_files_in_place(self):
        unit = (ROOT / "ops/host/colosseum-payout-review@.service").read_text()
        timer = (ROOT / "ops/host/colosseum-payout-review@.timer").read_text()
        self.assertIn("-m ops.review_service --deployment ${COLOSSEUM_DEPLOYMENT}", unit)
        self.assertIn("--out /var/lib/colosseum-keeper/reviews-%i", unit)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("OnUnitActiveSec=10min", timer)
        self.assertIn("--prices /var/lib/colosseum-keeper/prices-${COLOSSEUM_DEPLOYMENT}.jsonl", unit,
                      "the stops' loop's journal, by the keeper's own default name")
        install = (ROOT / "ops/host/install.sh").read_text()
        self.assertIn('"$rel/ops/host/colosseum-payout-review@.service" "$rel/ops/host/colosseum-payout-review@.timer"', install)


if __name__ == "__main__":
    unittest.main()
