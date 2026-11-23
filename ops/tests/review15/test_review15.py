"""Independent review 15 of PR 338 (head 0f29bd37). Each test states the CORRECT behaviour; red = a defect.
Run: COLOSSEUM_KEY_DIR=<empty dir> COLOSSEUM_RPC_URL=http://127.0.0.1:9 python -m unittest ops.tests.review15.test_review15 -v"""
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from ops import payout_review as pr
from ops import review_service as rs
from ops import stop_filler as sf
from ops.tests import test_payout_review as T
from ops.tests.test_price_journals import HEAD, row

MT = T.MissedTouches


def world(a, b, times=False, candles=None, bps=100):
    """The MissedTouches world judged by the keeper's journal `a` and the second's `b` ({block: px} or None)."""
    mt = MT()
    ch, _ = mt.world(set(range(1_300, 1_306)))
    j = pr.Journal()
    for name, rows in (("keeper", a), ("second", b)):
        if rows is not None:
            j.add_text("\n".join(json.dumps({"b": k, "t": 60 * k if times else None, "w": 0, "px": {"3": v}})
                                 for k, v in sorted(rows.items()) if 1_002 <= k <= 1_400) + "\n", None, name)
    ch.d["journal"] = j
    ch.d["journalCheck"] = pr.check_journal(list(ch.d["exam_events"]), ch.d["exam_events"], j, 2_000, None)
    if candles is not None:
        ch.d.update(candles=candles, candleBps=bps)
    return pr.review(ch.d, T.POOL, 0), mt


def truth():
    return MT().prices(set(range(1_300, 1_306)), until=2_001)


class OneSourceIsNotACrossCheck(unittest.TestCase):
    def test_a_lie_in_one_journal_where_the_other_has_a_hole_is_not_clean(self):
        """The oracle really was at 940 000 in 1 150 (a reach nobody carried out). The keeper's journal says the mark
        there; the second writer's own journal is behind / was restarted over 1 140..1 160. Union coverage: no hole, no
        disagreement, no sample: clean/approve."""
        mt = MT()
        real = {**truth(), 1_150: mt.LOW}
        lie = {**truth(), 1_150: mt.MARK}
        second = {b: px for b, px in real.items() if not 1_140 <= b <= 1_160}
        got, _ = world(lie, second)
        self.assertNotEqual((got["verdict"], got["action"]), ("clean", "approve"),
                            "a block only the keeper's journal holds is taken as it says: no cross-check was made")

    def test_no_second_journal_at_all_is_not_a_clean_review(self):
        """--prices2 unset, or the second machine's files not there / of another deployment: the review passes the
        payout as clean on the keeper's journal alone and the document does not say so."""
        got, _ = world(truth(), None)
        self.assertNotEqual((got["verdict"], got["action"]), ("clean", "approve"),
                            "the independent cross-check the owner decided on did not happen, and 'clean' says nothing of it")


class CandleScreenSilence(unittest.TestCase):
    def candles(self, px, wick=0):
        return {3: {60 * b: (p * (10_000 - wick) // 10_000, p * (10_000 + wick) // 10_000, p) for b, p in px.items()}}

    @unittest.expectedFailure  # TODO(review 15): until its fix lands in this round
    def test_journal_rows_without_a_time_do_not_switch_the_candle_screen_off(self):
        """`candle_doubts` builds its minutes only from blocks that have a time (`head_t` is set for polled heads only;
        a step over several blocks writes t=null for the rest). With no times there are no minutes: no doubt at all,
        not even 'candles cover 0 of 0'. Here the candles are 4 % past the stop all over and the journal shows no reach."""
        tr = truth()
        far = {3: {60 * b: (900_000, 1_000_000, 920_000) for b in tr}}
        got, _ = world(tr, tr, times=False, candles=far)
        self.assertEqual(got["action"], "hold", "the venue contradicts the journal in every minute and nothing is held")


class ScanWorld(unittest.TestCase):
    """LogScan over a fake chain; `logs` is patched."""
    POOLS = [T.POOL]
    USDC = T.addr(0xCC)

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.chain = []     # [{"address","topics","blockNumber"(int),...}]
        self.blind = None   # (lo, hi): a lagging replica that returns nothing for those blocks, for one pass

    def fake(self):
        def logs(address, topics, lo, hi):
            addrs = {a.lower() for a in ([address] if isinstance(address, str) else address)}
            out = []
            for l in self.chain:
                b = l["blockNumber"]
                if not lo <= b <= hi or l["address"].lower() not in addrs:
                    continue
                if self.blind and self.blind[0] <= b <= self.blind[1]:
                    continue
                if topics:
                    t0 = topics[0]
                    if isinstance(t0, list) and l["topics"][0] not in t0:
                        continue
                    if isinstance(t0, str) and l["topics"][0] != t0:
                        continue
                    if len(topics) > 2 and topics[2] and l["topics"][2] not in topics[2]:
                        continue
                out.append({**l, "blockNumber": hex(b), "transactionHash": T.tx(b), "logIndex": "0x0", "data": "0x"})
            return out
        return logs

    def word(self, a):
        return "0x" + "00" * 12 + a[2:].lower()

    def scan(self, latest, part=10):
        s = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        with mock.patch.object(pr, "logs", self.fake()), \
                mock.patch.object(pr.c, "rpc", side_effect=lambda m, p=(): {"timestamp": hex(1_000 * int(p[0], 16))}):
            s.read(self.POOLS, self.USDC, latest, part=part)
        return s



class ScanFaults(ScanWorld):
    def test_a_log_a_lagging_node_left_out_is_read_again_by_a_later_pass(self):
        """A pass reads to `latest` and keeps it for good. A load-balanced provider whose replica is a few blocks behind
        answers an empty getLogs for the newest blocks: the event is never read again (the old whole re-read healed it)."""
        exam, trader = T.addr(0x201), T.addr(0x101)
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(exam), self.word(trader), self.word(T.addr(0xB0))],
                           "blockNumber": 38})
        self.blind = (36, 40)
        self.scan(40)
        self.blind = None
        later = self.scan(80)
        self.assertEqual(later.accounts(), [exam], "the sale at block 38 is lost for good: nothing re-reads a tail")

    @unittest.expectedFailure  # TODO(review 15): until its fix lands in this round
    def test_funding_of_a_new_trader_older_than_a_day_is_still_a_link(self):
        """LINK_BACK: the transfers into a new trader are read from 86 400 blocks before their first purchase only. Funding
        wallets sent USDC to two buyers two days before they bought: invisible, so no `link`, and a pair is reason 1."""
        a, b, funder = T.addr(0x101), T.addr(0x102), T.addr(0xF0)
        far = 300_000
        for k, who in enumerate((a, b)):
            self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(who)],
                               "blockNumber": 10 + k})
            self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(T.addr(0x201 + k)), self.word(who), self.word(T.addr(0xB0))],
                               "blockNumber": far + k})
        s = pr.LogScan(self.dir / "scan.json", T.addr(0xFA), 998, 1)
        s.d["scannedTo"] = far - 1
        s.d["pools"] = list(self.POOLS)
        with mock.patch.object(pr, "logs", self.fake()), \
                mock.patch.object(pr.c, "rpc", side_effect=lambda m, p=(): {"timestamp": hex(1_000 * int(p[0], 16))}):
            s.read(self.POOLS, self.USDC, far + 5, part=100)
        got = {(t["from"].lower(), t["to"].lower()) for t in s.d["transfers"]}
        self.assertIn((funder.lower(), a.lower()), got, "the funder of two buyers, 3 days before they bought, is not read")


class ScanControls(ScanWorld):
    """The same fake chain with no fault: the harness itself finds the sale and a transfer a few hours old (green)."""

    def test_control_the_sale_and_a_near_transfer_are_read(self):
        exam, trader, funder = T.addr(0x201), T.addr(0x101), T.addr(0xF0)
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(exam), self.word(trader), self.word(T.addr(0xB0))],
                           "blockNumber": 38})
        self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(trader)], "blockNumber": 30})
        s = self.scan(80)
        self.assertEqual(s.accounts(), [exam])
        self.assertEqual([(t["from"], t["block"]) for t in s.d["transfers"]], [(funder, 30)])

    def test_a_log_the_tail_reads_again_is_kept_once(self):
        """The tail read again each pass (M2) finds what it read before: each log is kept once."""
        exam, trader, funder = T.addr(0x201), T.addr(0x101), T.addr(0xF0)
        self.chain.append({"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, self.word(exam), self.word(trader), self.word(T.addr(0xB0))],
                           "blockNumber": 38})
        self.chain.append({"address": self.USDC, "topics": [pr.TRANSFER, self.word(funder), self.word(trader)], "blockNumber": 39})
        self.chain.append({"address": exam, "topics": ["0x" + "11" * 32], "blockNumber": 39})
        self.scan(40)
        s = self.scan(80)
        self.assertEqual((len(s.d["pool_logs"]), len(s.d["transfers"]), len(s.d["account_logs"][exam])), (1, 1, 1))


class AlarmChannel(unittest.TestCase):
    def test_an_alarm_with_no_channel_on_the_host_is_not_taken(self):
        """`EnvironmentFile=-/etc/colosseum/watch.env` (optional) and `Alarm.__call__(wait=True)` returns True once a flag
        file holds the line: with COLOSSEUM_ALERT_CMD unset, every held-payout alarm is 'said' into a file nobody reads
        and not repeated for 6 hours."""
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with mock.patch.dict("os.environ", {"COLOSSEUM_ALERT_CMD": ""}):
            took = sf.Alarm(d / "ALARM")("payout_held", wait=True, pool="0x1")
        self.assertIsNot(took, True, "no channel: the alarm reached a person only if someone reads the flag file")


class CostOfANewBuyer(unittest.TestCase):
    @unittest.expectedFailure  # TODO(review 15): until its fix lands in this round
    def test_one_new_buyer_in_a_ten_minute_pass_costs_a_bounded_number_of_calls(self):
        """A pass that reads 600 new blocks in which one new trader bought: the look-back of LINK_BACK (86 400 blocks) is
        86 400 / 50 getLogs calls of its own, in the part's one step, which `until` (checked only after a part is saved)
        cannot cut: at 0.3 s a call that is 9 minutes against a unit TimeoutStartSec of 540, the part never saved."""
        exam, trader = T.addr(0x201), T.addr(0x101)
        sale = {"address": T.POOL, "topics": [pr.sk.EXAM_SOLD, "0x" + "00" * 12 + exam[2:].lower(),
                                             "0x" + "00" * 12 + trader[2:].lower(), "0x" + "00" * 12 + T.addr(0xB0)[2:].lower()],
                "blockNumber": hex(1_000_300), "transactionHash": T.tx(1), "logIndex": "0x0", "data": "0x"}
        calls = []

        def rpc(method, params=()):
            if method == "eth_getLogs":
                calls.append(params[0])
                f, t = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
                return [sale] if f <= 1_000_300 <= t and params[0]["address"] == [T.POOL] else []
            return {"timestamp": "0x1"}
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        s = pr.LogScan(d / "scan.json", T.addr(0xFA), 998, 1)
        s.d["scannedTo"] = 1_000_000
        s.d["pools"] = [T.POOL]
        with mock.patch.object(pr.c, "rpc", side_effect=rpc):
            s.read([T.POOL], T.addr(0xCC), 1_000_600)
        self.assertLess(len(calls), 400, f"{len(calls)} eth_getLogs calls for 600 blocks and one new buyer")


class PassOrder(unittest.TestCase):
    @unittest.expectedFailure  # TODO(review 15): until its fix lands in this round
    def test_every_document_is_written_before_the_first_alarm_that_may_hang(self):
        """`one_pass` writes a payout's document and says its alarm in one loop: the channel's timeout is 30 s an alarm, so
        a hung channel and ten held payouts is 300 s of the pass's 540 before the last documents are written, and a kill
        in the middle leaves their old documents (an old 'clean' stays until it is an hour old) and `said` unsaved."""
        r = lambda i: {"pool": "0xP", "id": i, "verdict": "suspicious", "action": "hold", "approvals": True, "approved": False,
                       "trader": "0xT", "kinds": ["stop-unchecked"], "reasons": ["x"], "dueAt": 10, "hardAt": 99_999_999}
        chain = {"payouts": [{"state": "Due", "pool": "0xP", "id": i} for i in range(3)], "now": 100, "latest": 5}
        seen = []
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)

        def alarm(event, **fields):
            seen.append(sorted(p.name for p in d.glob("0xP-*.json")))
            return True
        with mock.patch.object(rs.pr, "read_chain", return_value=chain), mock.patch.object(rs.pr, "review", side_effect=lambda c, p, i: r(i)), \
                mock.patch.object(rs.pr, "card", side_effect=lambda rv, c: {"pool": rv["pool"], "id": rv["id"]}), mock.patch.object(rs.core, "log"):
            rs.one_pass({}, d, None, {}, alarm)
        self.assertEqual(seen[0], ["0xP-0.json", "0xP-1.json", "0xP-2.json"], f"documents on disk at the first alarm: {seen[0]}")


class JournalMemory(unittest.TestCase):
    @unittest.expectedFailure  # TODO(review 15): until its fix lands in this round
    def test_a_day_of_two_journals_of_three_assets_fits_a_stop_age_of_ten_days_in_the_units_memory(self):
        """The review reads every day file from the day before the OLDEST stop of any waiting payout's exam or stage (lo_t
        in `read_chain`), for both journals, into dicts. The unit's MemoryMax is 1 GB: ten days of stop age may take at most
        ~45 MB a day here (a budget: 10 days = 450 MB beside the texts and the rest of the review). Measured at 8 days of two journals: 890 MB peak RSS (the whole review lives beside it)."""
        import tracemalloc
        n = 86_400
        text = "\n".join('{"b":%d,"t":%d,"w":0,"px":{"0":%d,"1":%d,"5":%d}}' % (30_000_000 + i, 1_800_000_000 + i, 6_100_000 + i % 97 * 10, 330_000 + i % 89, 15_000 + i % 7)
                         for i in range(n)) + "\n"
        tracemalloc.start()
        j = pr.Journal()
        j.add_text(text, None, "keeper")
        j.add_text(text, None, "second")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak / 1e6, 45, f"{peak / 1e6:.0f} MB for one day of two journals: ten days is {peak / 1e5:.0f} MB of a 1 GB unit")


if __name__ == "__main__":
    unittest.main()
