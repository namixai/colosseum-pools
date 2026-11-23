"""The review's service (ops/review_service.py) on the made-up chains of ops/tests/test_payout_review.py: it writes a
document a payout and raises an alarm for each payout a person has to act on (review 12 of #336, M-2)."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from ops import payout_review as r
from ops import review_service as svc
from ops import sim_check as chk
from ops.tests.test_payout_review import POOL, TheBookHoldsWhatItCannotClear, honest_world

ROOT = pathlib.Path(__file__).resolve().parents[2]


class TheService(unittest.TestCase):
    hold = TheBookHoldsWhatItCannotClear()

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
        ch, _ = self.hold.world([(1_100, 1_110)])
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
            ch, _ = self.hold.world([(1_100, 1_110)])
            where, key, value = change
            (ch.d["books"][POOL] if where == "books" else ch.d["payouts"][0])[key] = value
            _, docs, alarms = self.run_pass(ch)
            self.assertEqual([e for e, _ in alarms], ["payout_needs_a_stop"], change)
            self.assertEqual(alarms[0][1]["deadline"], deadline(ch.d["payouts"][0]), change)
            self.assertIn("stops it for reason 3", alarms[0][1]["do"])

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
        install = (ROOT / "ops/host/install.sh").read_text()
        self.assertIn('"$rel/ops/host/colosseum-payout-review@.service" "$rel/ops/host/colosseum-payout-review@.timer"', install)


if __name__ == "__main__":
    unittest.main()
