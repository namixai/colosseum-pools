"""What was decided on the findings of review 16 of PR 338, beside the reviewer's own tests (test_review16.py)."""
import unittest
from unittest import mock

from ops import payout_review as pr
from ops.tests import test_payout_review as T
from ops.tests.review15.test_review15 import ScanWorld


class EveryBuyersFunding(ScanWorld):
    """Two buyers funded by one wallet are a pair by the funding of both, and the second has no payout of its own: once
    a payout waits, the funding of every buyer of the factory is read back to the deployment, and until it is, each
    payout waiting is held. With no payout waiting, none of it is read."""

    def buyer(self, who, block, exam):
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(exam), self.word(who), self.word(T.addr(0xB0))],
                           "blockNumber": block})

    def counted(self):
        calls, real = [], self.fake()

        def logs(address, topics, lo, hi):
            calls.append((lo, hi, len(topics[2]) if len(topics) > 2 and topics[2] else 0))
            return real(address, topics, lo, hi)
        return calls, logs

    def test_no_payout_waiting_reads_no_funding_at_all(self):
        self.buyer(T.addr(0x101), 5_000, T.addr(0x201))
        s = self.scan(5_010, part=5_010)
        calls, logs = self.counted()
        with mock.patch.object(pr, "logs", logs):
            self.assertEqual(s.read_links([], self.USDC), [])
        self.assertEqual(calls, [], "a buyer with no payout waiting anywhere costs the node nothing")
        self.assertFalse(s.links_read(T.addr(0x101)))

    def test_a_payout_waits_until_every_buyers_funding_is_read_not_only_its_own_traders(self):
        a, b, funder = T.addr(0x101), T.addr(0x102), T.addr(0xF0)
        self.buyer(a, 2_000, T.addr(0x201))
        self.buyer(b, 5_000, T.addr(0x202))
        self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(b)], "blockNumber": 4_500})
        s = self.scan(5_010, part=5_010)
        clock = iter([0.0, 0.0, 99.0, 99.0])  # two chunks, and the time is out: a's blocks read, b's not
        with mock.patch.object(pr, "logs", self.fake()), mock.patch.object(pr.time, "monotonic", lambda: next(clock)):
            self.assertEqual(s.read_links([a], self.USDC, until=1.0), [a], "a's payout is held while b is not read")
        self.assertEqual((s.links_read(a), s.links_read(b)), (True, False))
        self.assertEqual(self.links(s, [a]), [])
        self.assertTrue(s.links_read(b))
        self.assertIn((funder, b, 4_500), {(t["from"], t["to"], t["block"]) for t in s.d["transfers"]})

    def test_a_buyer_first_seen_after_the_others_were_read_is_read_from_the_deployment_too(self):
        a, c, funder = T.addr(0x101), T.addr(0x103), T.addr(0xF0)
        self.buyer(a, 2_000, T.addr(0x201))
        s = self.scan(2_010, part=2_010)
        self.assertEqual(self.links(s, [a]), [])
        self.buyer(c, 6_000, T.addr(0x203))
        self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(c)], "blockNumber": 15})
        with mock.patch.object(pr, "logs", self.fake()), \
                mock.patch.object(pr.c, "rpc", side_effect=lambda m, p=(): {"timestamp": hex(1_000 * int(p[0], 16))}):
            s.read(self.POOLS, self.USDC, 6_010, part=6_010)
        self.assertFalse(s.links_read(c))
        calls, logs = self.counted()
        with mock.patch.object(pr, "logs", logs):
            self.assertEqual(s.read_links([a], self.USDC), [])
        to = 5_510  # the scan's step that first saw the buyer began at 5 511: from there on its transfers are the scan's
        self.assertEqual(s.d["links"][c]["to"], to)
        self.assertEqual([(lo, hi, n) for lo, hi, n in calls], [(1 + 1_000 * k, min(1_000 * (k + 1), to), 1) for k in range(6)],
                         "the new buyer alone, from the deployment's block, a chunk a call")
        self.assertIn((funder, c, 15), {(t["from"], t["to"], t["block"]) for t in s.d["transfers"]})

    def test_buyers_go_a_hundred_to_a_call(self):
        for k in range(250):  # each in a block of its own, all first seen in the scan's step from block 10 001
            self.buyer(T.addr(0x1000 + k), 10_001 + k, T.addr(0x5000 + k))
        s = self.scan(10_300, part=10_300)
        calls, logs = self.counted()
        with mock.patch.object(pr, "logs", logs):
            self.assertEqual(s.read_links([T.addr(0x1000)], self.USDC), [])
        self.assertEqual(len(calls), 30, "10 chunks of 1 000 blocks, 250 buyers in three calls each")
        self.assertEqual(max(n for _, _, n in calls), pr.chk.ADDRESS_BATCH)


class EachAlarmAlone(unittest.TestCase):
    """An alarm that raises is that alarm's failure: the ones after it are said, and it is said again at the next pass."""

    def test_an_alarm_that_raised_is_not_remembered_and_the_others_are(self):
        import pathlib
        import tempfile
        from ops.tests.review16.test_review16 import Alarms16
        d = pathlib.Path(tempfile.mkdtemp())
        seen, said, broken = [], {}, [True]

        def alarm(event, **fields):
            seen.append(fields["id"])
            if fields["id"] == 0 and broken:
                raise RuntimeError("the channel's own fault")
            return True
        Alarms16().run_pass(d, alarm, said)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(sorted(k for k, v in said.items() if isinstance(v, dict)), ["0xP-1", "0xP-2"],
                         "the one that raised is not remembered as said")
        self.assertEqual(sorted(p.name for p in d.glob("0xP-*.json")), ["0xP-0.json", "0xP-1.json", "0xP-2.json"])
        broken.clear()
        del seen[:]
        Alarms16().run_pass(d, alarm, said)
        self.assertEqual(seen, [0], "said again at the next pass, the others not twice")


if __name__ == "__main__":
    unittest.main()
