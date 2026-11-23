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
            docs = {p.name: json.loads(p.read_text()) for p in pathlib.Path(out).glob("*.json")}
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
