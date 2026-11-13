"""The review of a simulated pool's payout, on a chain made up here: a clean payout, a pair inside the
factory, a pair whose two traders were funded from one wallet, and a code bought up from new wallets."""
import json
import pathlib
import re
import unittest
from unittest import mock

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from ops import payout_review as r
from ops import sim_check as chk
from ops import sim_keeper as sk

ROOT = pathlib.Path(__file__).resolve().parents[2]


def addr(n: int) -> str:
    return to_checksum_address("0x" + f"{n:040x}")


POOL, OTHER_POOL = addr(0xA1), addr(0xA2)
EXCHANGE = addr(0xEE)
USDC = addr(0xD0)
SIZE = 10_000 * 10**6  # an exam on 10 000: a payout of 800


def tx(n: int) -> str:
    return "0x" + f"{n:064x}"


class Chain:
    """A made-up chain in the shape `read_chain` returns."""

    def __init__(self):
        self.d = {"now": 2_000_000, "latest": 900, "pools": [POOL, OTHER_POOL], "sales": [], "funded": [],
                  "payouts": [], "status": {}, "done": {}, "fills": [], "transfers": [], "unread": [],
                  "books": {POOL: {"balance": 50_000 * 10**6, "owed": 800 * 10**6, "held": 0, "reserved": 0},
                            OTHER_POOL: {"balance": 0, "owed": 0, "held": 0, "reserved": 0}}}
        self.n = 0

    def _tx(self) -> str:
        self.n += 1
        return tx(self.n)

    def sale(self, trader, exam, code="0x" + "11" * 32, partner=addr(0xB0), pool=POOL, status="Active", done=False):
        self.d["sales"].append({"pool": pool, "exam": exam, "trader": trader, "partner": partner, "line": 0,
                                "size": SIZE, "price": 100 * 10**6, "code": code, "block": len(self.d["sales"]) + 1,
                                "tx": self._tx()})
        self.d["status"][exam], self.d["done"][exam] = status, done
        return exam

    def stage(self, trader, exam, stage, pool=POOL, status="Passed", done=True):
        self.d["funded"].append({"pool": pool, "stage": stage, "exam": exam, "trader": trader, "block": 100})
        self.d["status"][exam], self.d["done"][exam] = "Passed", True
        self.d["status"][stage], self.d["done"][stage] = status, done
        return stage

    def payout(self, trader, stage, pid=0, pool=POOL):
        self.d["payouts"].append({"pool": pool, "id": pid, "account": stage, "trader": trader, "amount": 800 * 10**6,
                                  "dueAt": self.d["now"] + 3_600, "block": 800, "state": "Due", "wasStopped": False})

    def fill(self, account, buy=True, sz=100, t=1_000, asset=3, late=0):
        h = self._tx()
        self.d["fills"].append({"account": account, "asset": asset, "buy": buy, "sz": sz, "t": t, "tx": h, "late": late})
        return h

    def send(self, frm, to):
        h = self._tx()
        self.d["transfers"].append({"from": frm, "to": to, "tx": h, "block": self.n})
        return h


def honest_world() -> tuple[Chain, str, str]:
    """Trader 1 passed exam and stage and claimed; eight other buyers trade on their own; an exchange
    funded all of them, and the pool paid trader 1 and trader 2 before."""
    ch = Chain()
    t1, e1, s1 = addr(0x101), addr(0x201), addr(0x301)
    ch.sale(t1, e1)
    ch.stage(t1, e1, s1)
    ch.payout(t1, s1)
    ch.fill(e1, buy=True, t=1_000)
    ch.fill(e1, buy=False, t=5_000)  # a passed exam and a passed stage are flat
    ch.fill(s1, buy=True, t=50_000)
    ch.fill(s1, buy=False, t=53_600)
    ch.send(EXCHANGE, t1)
    for i in range(8):
        t, e = addr(0x110 + i), addr(0x210 + i)
        ch.sale(t, e)
        ch.fill(e, buy=True, t=1_000 + i)  # the same side as trader 1's exam: copying is no pair
        ch.send(EXCHANGE, t)
    ch.send(POOL, t1)
    ch.send(POOL, addr(0x110))
    return ch, t1, s1


class ACleanPayout(unittest.TestCase):
    def test_nothing_found_is_clean_and_the_approval_is_written(self):
        ch, t1, s1 = honest_world()
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["trader"], got["stage"], got["amount"]), (t1, s1, 800 * 10**6))
        self.assertEqual(got["fills"], {"stage": 2, "exam": 2})
        self.assertEqual(got["mirrors"], {"stage": [], "exam": []}, "everyone long the same asset mirrors nobody")
        self.assertEqual(got["pairs"], {"stage": [], "exam": []})
        self.assertIsNone(got["stop"])
        self.assertTrue(all(l["wide"] for l in got["links"]), "the exchange funded nine buyers: named, not counted")
        self.assertFalse(any(l["sender"] == POOL for l in got["links"]), "the pool pays every trader")
        self.assertEqual(got["verdict"], "clean")
        self.assertEqual(got["reasons"], [])
        self.assertEqual((got["tx"]["kind"], got["tx"]["to"], got["tx"]["args"]), ("approve", POOL, [[0]]))
        self.assertEqual(got["tx"]["data"][:10], "0x" + keccak(text="approvePayouts(uint256[])")[:4].hex())
        self.assertEqual(decode(["uint256[]"], bytes.fromhex(got["tx"]["data"][10:])), ((0,),))

    def test_the_72_hours_and_the_ceiling_are_counted_from_the_claim(self):
        ch, _, _ = honest_world()
        got = r.review(ch.d, POOL, 0)
        due = ch.d["now"] + 3_600
        self.assertEqual((got["claimedAt"], got["dueAt"], got["hardAt"]), (due - 72 * 3600, due, due + 4 * 24 * 3600))

    def test_without_an_approval_call_nothing_is_assembled(self):
        ch, _, _ = honest_world()
        with mock.patch.object(r, "APPROVE", None):
            got = r.review(ch.d, POOL, 0)
        self.assertIsNone(got["tx"]["data"])

    def test_what_is_running_is_counted_against_the_free_money(self):
        ch, _, _ = honest_world()
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual((got["exams"], got["stages"]), (8, 0), "trader 1's exam and stage are done")
        self.assertEqual(got["worst"], 8 * 800 * 10**6)
        self.assertEqual(got["free"], 49_200 * 10**6)
        self.assertTrue(got["enough"])
        ch.d["books"][POOL].update(balance=7_000 * 10**6, held=100 * 10**6, reserved=200 * 10**6)
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual(got["free"], 5_900 * 10**6, "what is owed, held back and kept for stops is not free")
        self.assertFalse(got["enough"])

    def test_a_running_stage_and_a_passed_exam_not_yet_funded_count_a_finished_one_does_not(self):
        ch = Chain()
        t, e, s = addr(0x101), addr(0x201), addr(0x301)
        ch.sale(t, e)
        ch.stage(t, e, s, status="Active", done=False)
        ch.sale(addr(0x102), addr(0x202), status="Passed")
        ch.sale(addr(0x103), addr(0x203), status="Breached")
        ch.sale(addr(0x104), addr(0x204), status="Expired", done=True)
        got = r.running(POOL, ch.d)
        self.assertEqual((got["exams"], got["stages"], got["worst"]), (1, 1, 2 * 800 * 10**6))


class APairInTheFactory(unittest.TestCase):
    def world(self):
        ch, t1, s1 = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9, pool=OTHER_POOL)
        mine = ch.fill(s1, buy=True, sz=100, t=60_000)
        other = ch.fill(e9, buy=False, sz=90, t=60_300)
        return ch, t1, t9, e9, mine, other

    def test_the_stage_against_an_exam_of_another_pool_of_the_factory_is_a_stop_of_reason_one(self):
        ch, _, t9, e9, mine, other = self.world()
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(len(got["pairs"]["stage"]), 1)
        self.assertEqual(got["pairs"]["stage"][0]["trader"], t9)
        self.assertEqual(got["stop"], {"on": "stage", "by": "pair", "reason": 1, "paired": e9, "pairedTrader": t9,
                                       "trades": [mine, other], "links": []})
        self.assertEqual(got["verdict"], "suspicious")
        self.assertIn("pair on the stage", got["reasons"][0])

    def test_the_stop_is_the_pools_own_call_with_the_pairs_hashes(self):
        ch, _, _, e9, mine, other = self.world()
        t = r.review(ch.d, POOL, 0)["tx"]
        self.assertEqual((t["kind"], t["to"], t["call"]), ("stop", POOL, chk.STOP))
        self.assertEqual(t["data"][:10], "0x" + keccak(text=chk.STOP)[:4].hex())
        pid, reason, paired, trades, links = decode(chk.STOP_TYPES, bytes.fromhex(t["data"][10:]))
        self.assertEqual((pid, reason, to_checksum_address(paired), links), (0, 1, e9, ()))
        self.assertEqual(["0x" + h.hex() for h in trades], [mine, other])

    def test_a_pool_paying_both_traders_does_not_make_it_reason_two(self):
        ch, t1, t9, _, _, _ = self.world()
        ch.send(POOL, t9)  # the pool paid trader 1 before, and now trader 9
        self.assertEqual(r.review(ch.d, POOL, 0)["stop"]["reason"], 1)

    def test_too_far_apart_or_too_unlike_in_size_is_no_pair_but_the_positions_held_are_a_mirror(self):
        ch, _, _ = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        mine = ch.fill(addr(0x301), buy=True, sz=100, t=60_000)
        far = ch.fill(e9, buy=False, sz=100, t=60_601)
        ch.fill(e9, buy=False, sz=79, t=60_000)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["pairs"]["stage"], [], "601 s apart, and 79 against 100: no pair by the fills")
        self.assertEqual((got["stop"]["by"], got["stop"]["paired"], got["stop"]["reason"]), ("mirror", e9, 1))
        self.assertIn(mine, got["stop"]["trades"])
        self.assertIn(far, got["stop"]["trades"])
        self.assertEqual(len(got["stop"]["trades"]), len(set(got["stop"]["trades"])), "each hash once")
        self.assertEqual(got["kinds"], ["mirror"])

    def test_a_pair_on_the_exam_alone_is_a_stop_too(self):
        ch, _, _ = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        ch.fill(addr(0x201), buy=False, sz=100, t=1_200, asset=5)  # trader 1's exam, against trader 9's exam
        ch.fill(e9, buy=True, sz=100, t=1_250, asset=5)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["stop"]["on"], "exam")
        self.assertEqual(got["stop"]["paired"], e9)
        self.assertEqual(got["tx"]["kind"], "stop")


class MirroredAccounts(unittest.TestCase):
    """Positions held against positions, whatever the timing and size of the fills: the stage of trader 1 is
    long 100 of asset 7 for ten hours; trader 9's exam in the factory's other pool does something meanwhile."""

    T0, H = 100_000, 3_600

    def world(self, *fills):
        ch, t1, s1 = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9, pool=OTHER_POOL)
        ch.d["fills"] = [f for f in ch.d["fills"] if f["account"] != s1]  # the stage trades asset 7 alone here
        opened = ch.fill(s1, buy=True, sz=100, t=self.T0, asset=7)
        ch.fill(s1, buy=False, sz=100, t=self.T0 + 10 * self.H, asset=7)
        txs = [ch.fill(e9, buy=buy, sz=sz, t=self.T0 + dt, asset=asset) for buy, sz, dt, asset in fills]
        return ch, e9, t9, opened, txs

    def test_a_pair_built_in_pieces_is_a_mirror_and_a_stop(self):
        ch, e9, t9, opened, txs = self.world((False, 40, 600, 7), (False, 30, 1_200, 7), (False, 30, 1_800, 7),
                                             (True, 100, 10 * self.H + 700, 7))
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["pairs"]["stage"], [], "no two fills close in time and size")
        [m] = got["mirrors"]["stage"]
        self.assertEqual((m["account"], m["trader"], m["assets"]), (e9, t9, [7]))
        self.assertGreater(m["share"], 0.95, "40 against 100 is under half; from the second piece on it is a mirror")
        self.assertEqual(m["exposed"], 10 * self.H)
        self.assertEqual(m["mirrored"], 10 * self.H - 1_200)
        self.assertEqual((got["stop"]["by"], got["stop"]["reason"], got["stop"]["paired"]), ("mirror", 1, e9))
        self.assertEqual(got["stop"]["trades"][:2], [opened, txs[1]], "the stage's opening fill and the piece that made it a mirror")
        self.assertEqual(got["tx"]["kind"], "stop")
        self.assertIn("mirrored positions on the stage with " + e9, got["reasons"][0])

    def test_a_pair_an_hour_late_is_a_mirror(self):
        ch, e9, _, _, _ = self.world((False, 100, self.H, 7), (True, 100, 11 * self.H, 7))
        [m] = r.review(ch.d, POOL, 0)["mirrors"]["stage"]
        self.assertEqual((m["account"], m["share"]), (e9, 0.9))

    def test_a_pair_at_sixty_percent_of_the_size_is_a_mirror(self):
        ch, e9, _, _, _ = self.world((False, 60, 60, 7), (True, 60, 10 * self.H, 7))
        [m] = r.review(ch.d, POOL, 0)["mirrors"]["stage"]
        self.assertEqual(m["account"], e9)
        self.assertGreater(m["share"], 0.99)

    def test_the_same_trend_another_asset_or_a_short_overlap_is_no_mirror(self):
        later = 10 * self.H + 700  # each closes apart from the stage: no pair of closing fills either
        for fills in [((True, 100, 60, 7), (False, 100, later, 7)),        # long too: the same trend
                      ((False, 100, 60, 8), (True, 100, later, 8)),        # short another asset
                      ((False, 100, 8 * self.H, 7), (True, 100, later, 7)),  # two hours of ten
                      ((False, 30, 60, 7), (True, 30, later, 7))]:          # 30 against 100
            ch, _, _, _, _ = self.world(*fills)
            got = r.review(ch.d, POOL, 0)
            self.assertEqual(got["mirrors"]["stage"], [], fills)
            self.assertEqual(got["verdict"], "clean", fills)

    def test_an_account_that_ended_holds_nothing_from_then(self):
        ch, e9, _, _, _ = self.world((False, 100, -1_000, 7))  # short before the stage, and never closed
        self.assertEqual(r.review(ch.d, POOL, 0)["mirrors"]["stage"][0]["account"], e9, "still open: a mirror")
        ch.d["ended"] = {e9: self.T0 - 500}  # breached before the stage opened
        self.assertEqual(r.review(ch.d, POOL, 0)["mirrors"]["stage"], [])

    def test_a_mirror_whose_traders_were_funded_from_one_wallet_is_reason_two(self):
        ch, e9, t9, _, _ = self.world((False, 100, self.H, 7), (True, 100, 11 * self.H, 7))
        funder = addr(0xF1)
        a, b = ch.send(funder, addr(0x101)), ch.send(funder, t9)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["stop"]["by"], got["stop"]["reason"], got["stop"]["links"]), ("mirror", 2, [a, b]))
        self.assertIn("and linked wallets", got["reasons"][0])
        self.assertEqual(got["kinds"], ["mirror", "link"])

    def test_the_measure_on_its_own(self):
        f = lambda acc, buy, sz, t, tx: {"account": acc, "asset": 1, "buy": buy, "sz": sz, "t": t, "tx": tx}
        mine = [f("A", True, 10, 0, "a1"), f("A", False, 10, 100, "a2")]
        m = r.mirror(mine, [f("B", False, 10, 50, "b1")], 1_000, 0.5)
        self.assertEqual((m["exposed"], m["mirrored"], m["share"], m["examples"]), (100, 50, 0.5, [["a1", "b1"]]))
        self.assertEqual(r.mirror([], mine, 1_000, 0.5)["share"], 0.0)
        still = r.mirror([f("A", True, 10, 0, "a1")], [f("B", False, 10, 0, "b1")], 1_000, 0.5)
        self.assertEqual((still["exposed"], still["mirrored"]), (1_000, 1_000), "a position still open runs to now")


class TheOppositeSideSpread(unittest.TestCase):
    """The losing side spread over many small accounts: trader 1's stage long 100 of asset 7 for ten hours, and
    accounts of the factory's other pool short against it, each too small to be a mirror on its own."""

    T0, H = 100_000, 3_600

    def world(self):
        ch, t1, s1 = honest_world()
        ch.d["fills"] = [f for f in ch.d["fills"] if f["account"] != s1]
        ch.fill(s1, buy=True, sz=100, t=self.T0, asset=7)
        ch.fill(s1, buy=False, sz=100, t=self.T0 + 10 * self.H, asset=7)
        return ch

    def small(self, ch, n, sz, start=60, end=None, base=0x600):
        out = []
        for i in range(n):
            t, e = addr(base + i), addr(base + 0x100 + i)
            ch.sale(t, e, pool=OTHER_POOL)
            ch.fill(e, buy=False, sz=sz, t=self.T0 + start + i, asset=7)
            ch.fill(e, buy=True, sz=sz, t=self.T0 + (end or 10 * self.H + 700) + i, asset=7)
            out.append((t, e))
        return out

    def test_five_accounts_of_twenty_percent_are_found_and_no_stop_is_made(self):
        ch = self.world()
        five = self.small(ch, 5, 20)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["mirrors"]["stage"], [], "each is a fifth of the stage: no mirror on its own")
        o = got["opposingSum"]["stage"]
        self.assertEqual(sorted(a["account"] for a in o["accounts"]), sorted(e for _, e in five))
        self.assertTrue(all(abs(a["part"] - 0.2) < 0.01 for a in o["accounts"]), o["accounts"])
        self.assertGreater(o["share"], 0.95)
        self.assertEqual(o["sharedSenders"], [])
        self.assertEqual(got["kinds"], ["mirror-sum"])
        self.assertEqual((got["stop"], got["action"]), (None, "decide"), "a sum is no pair: suspicious, never a stop")

    def test_a_sender_funding_several_of_them_is_named(self):
        ch = self.world()
        five = self.small(ch, 5, 20)
        funder = addr(0xF7)
        for t, _ in five[:3]:
            ch.send(funder, t)
        ch.send(addr(0xF8), five[4][0])  # a sender of one of them alone links nothing
        o = r.review(ch.d, POOL, 0)["opposingSum"]["stage"]
        self.assertEqual(o["sharedSenders"], [funder])
        self.assertIn("1 sender(s) funding several of them", r.review(ch.d, POOL, 0)["reasons"][0])

    def test_a_busy_market_where_someone_is_always_short_is_not(self):
        # Twenty accounts trading asset 7 for five days, each an hour long or short every three hours: at any
        # moment several are short against the stage, but each is mostly elsewhere in time, so none is given to it.
        ch = self.world()
        for i in range(20):
            t, e = addr(0x700 + i), addr(0x800 + i)
            ch.sale(t, e, pool=OTHER_POOL)
            for k in range(40):
                at = self.T0 - 2 * 86_400 + k * 3 * self.H + i * 600
                buy = (i + k) % 2 == 0
                ch.fill(e, buy=buy, sz=30, t=at, asset=7)
                ch.fill(e, buy=not buy, sz=30, t=at + self.H, asset=7)
        got = r.review(ch.d, POOL, 0)
        self.assertIsNone(got["opposingSum"]["stage"])
        self.assertEqual(got["verdict"], "clean")

    def test_one_account_given_to_it_is_no_spread_side(self):
        ch = self.world()
        self.small(ch, 1, 40)
        self.assertIsNone(r.review(ch.d, POOL, 0)["opposingSum"]["stage"], "one account is a mirror's question, not a sum's")

    def test_accounts_given_to_it_but_too_small_together_are_not(self):
        ch = self.world()
        self.small(ch, 4, 10)
        self.assertIsNone(r.review(ch.d, POOL, 0)["opposingSum"]["stage"], "40 against 100: under half")

    def test_two_accounts_in_turn_are_each_its_own_question_not_a_spread_side(self):
        ch = self.world()
        self.small(ch, 1, 100, start=60, end=5 * self.H - 30, base=0x900)
        self.small(ch, 1, 100, start=5 * self.H + 30, end=10 * self.H + 700, base=0xA00)
        got = r.review(ch.d, POOL, 0)
        self.assertIsNone(got["opposingSum"]["stage"], "never two against it at once")

    def test_an_account_that_ended_is_closed_at_its_end(self):
        fills = [{"account": "B", "asset": 1, "buy": False, "sz": 5, "t": 10, "tx": "b1"}]
        self.assertEqual(r.flat_at(fills, None), fills)
        self.assertEqual(r.flat_at(fills, 20)[-1], {"account": "B", "asset": 1, "buy": True, "sz": 5, "t": 20, "tx": None})
        self.assertEqual(r.flat_at(fills, 5), [])


class PassRates(unittest.TestCase):
    """The pool's passes against the CFO's thresholds (13 November): passes among the last 100 exams ended (yellow
    24, red 28), payouts among the last 100 sold (yellow 6, red 8), payouts over net income over 30 days (yellow
    0.5, red 0.75); shares from 30 exams ended, before that red only if payouts exceed the net income."""

    def world(self, n, passed, payouts=0, code=None, base=0x2000, fills=5, t0=1_900_000, ended=True):
        ch, t1, s1 = honest_world()
        ch.d["exam_events"] = {addr(0x201): [], s1: []}
        for i in range(n):
            tr, ex = addr(base + i), addr(base + 0x1000 + i)
            ch.sale(tr, ex, code=code or "0x" + "11" * 32)
            evs = [{"name": "Started", "block": 1, "index": 0, "t": t0 + i, "tx": tx(1), "capital": 1}]
            evs += [{"name": "Filled", "block": 2, "index": 0, "t": t0 + i + k * 86_400, "tx": tx(2), "asset": 3, "buy": True,
                     "sz": 1, "px": 1, "cost": 0, "late": 0, "source": 0} for k in range(fills)]
            end = {"block": 3, "index": 0, "t": t0 + i + 10 * 3_600, "tx": tx(3), "equity": 1}
            if i < passed:
                evs.append({**end, "name": "Passed"})
            elif ended:
                evs.append({**end, "name": "Stopped", "status": 3})
            if i < payouts:
                st = addr(base + 0x2000 + i)
                ch.stage(tr, ex, st, status="Passed", done=True)
                ch.d["payouts"].append({"pool": POOL, "id": 100 + i, "account": st, "trader": tr, "amount": 1,
                                        "dueAt": 1, "block": 1, "state": "Paid", "wasStopped": False})
            ch.d["exam_events"][ex] = evs
        return ch

    def test_the_exact_tail(self):
        self.assertAlmostEqual(r.binom_tail(0, 10, 0.3), 1.0)
        self.assertAlmostEqual(r.binom_tail(10, 10, 0.5), 0.5 ** 10)
        self.assertAlmostEqual(r.binom_tail(1, 10, 0.1), 1 - 0.9 ** 10)

    def test_before_thirty_exams_end_it_is_red_only_if_payouts_exceed_the_net_income(self):
        got = r.review(self.world(10, 9).d, POOL, 0)
        pr = got["passRates"]
        self.assertEqual((pr["finished"], pr["early"]["payouts"], pr["early"]["net"], pr["level"]),
                         (10, 800 * 10**6, 19 * 100 * 10**6, "few"), "trader 1's 800 against 19 sales of 100")
        self.assertNotIn("pass-rate", got["kinds"])
        ch = self.world(10, 9)
        ch.d["payouts"][0]["amount"] = 2_000 * 10**6
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["passRates"]["level"], got["passRates"]["batch"]), ("red", False))
        self.assertIn("above net income", got["reasons"][0])

    def test_passes_among_the_last_hundred_ended(self):
        for passed, level in ((28, "red"), (24, "yellow"), (20, "ok")):
            pr = r.review(self.world(100, passed).d, POOL, 0)["passRates"]
            self.assertEqual((pr["passes"]["hits"], pr["passes"]["of"], pr["passes"]["level"]), (passed, 100, level))
        got = r.review(self.world(100, 28).d, POOL, 0)
        self.assertEqual(got["kinds"][0], "pass-rate", "said first, on every payout of the pool")
        self.assertEqual((got["action"], got["tx"]["kind"]), ("decide", "approve"), "the trader is not to blame")
        self.assertFalse(r.card(got, self.world(100, 28).d)["batchApproval"], "red: one by one")

    def test_fewer_than_a_hundred_are_counted_per_hundred(self):
        pr = r.review(self.world(50, 12).d, POOL, 0)["passRates"]
        self.assertEqual((pr["passes"]["of"], pr["passes"]["perHundred"], pr["passes"]["level"]), (50, 24.0, "yellow"))

    def test_payouts_among_the_last_hundred_sold(self):
        for paid, level in ((8, "red"), (6, "yellow"), (5, "ok")):
            pr = r.review(self.world(100, 10, payouts=paid).d, POOL, 0)["passRates"]
            self.assertEqual((pr["payouts"]["hits"], pr["payouts"]["level"]), (paid, level))

    def test_payouts_over_net_income_over_thirty_days(self):
        ch = self.world(40, 5)
        ch.d["payouts"][0]["amount"] = 3_100 * 10**6  # against 40 sales of 100 in the window
        pr = r.review(ch.d, POOL, 0)["passRates"]
        self.assertEqual((pr["payoutsToIncome"]["ratio"], pr["payoutsToIncome"]["level"]), (0.775, "red"))
        ch.d["payouts"][0]["amount"] = 2_000 * 10**6
        self.assertEqual(r.review(ch.d, POOL, 0)["passRates"]["payoutsToIncome"]["level"], "yellow")

    def test_thresholds_from_a_file(self):
        th = {"passes": {"window": 100, "yellow": 40, "red": 50}}
        self.assertEqual(r.review(self.world(100, 28).d, POOL, 0, thresholds=th)["passRates"]["passes"]["level"], "ok")

    def test_the_slices(self):
        ch = self.world(40, 10, code="0x" + "44" * 32)
        pr = r.review(ch.d, POOL, 0)["passRates"]
        self.assertEqual(pr["byCode"]["0x" + "44" * 32], {"sold": 40, "passed": 10, "payouts": 0})
        self.assertEqual(list(pr["byAsset"]), ["3"])
        self.assertEqual(set(pr["byHour"]), {"9"}, "1 900 000 s plus ten hours is 09:46 UTC")
        self.assertEqual(sum(pr["byWeekday"].values()), 10)
        self.assertEqual(r.review(self.world(40, 10, fills=2).d, POOL, 0)["passRates"]["fewFillPasses"], 10)

    def test_without_the_accounts_events_there_is_nothing_to_judge(self):
        ch, _, _ = honest_world()
        self.assertIsNone(r.review(ch.d, POOL, 0)["passRates"])


class Timing(unittest.TestCase):
    """Fills judged by the oracle of every block of their window (5 blocks) and of the block after: trader 1's stage
    trades asset 9, twelve orders, each with prices made up here."""

    W = 5

    def world(self, n=12, pick=lambda window, buy: 0, after=None, seed=7):
        ch, _, s1 = honest_world()
        rnd = seed
        events, oracle = [], {}
        for i in range(n):
            buy = i % 2 == 0
            first = 1_000 + 20 * i
            window = []
            for k in range(self.W):
                rnd = (rnd * 1_103_515_245 + 12_345) % 2**31
                window.append(1_000 + rnd % 9 - 4)  # within 0.4 %: no oracle outlier for the recount to flag
            late = pick(window, buy)
            block = first + late
            for k, px in enumerate(window):
                oracle[f"9:{first + k}"] = px
            o = window[late]
            if after is not None:  # the block after the fill, as the test makes it (it may be in the window)
                oracle[f"9:{block + 1}"] = after(o, buy, i)
            events.append({"name": "Filled", "block": block, "index": 0, "t": 1_000 * block, "tx": tx(7_000 + i), "asset": 9,
                           "buy": buy, "sz": 1, "px": o, "cost": 0, "late": late, "source": 0})
        ch.d["exam_events"] = {s1: events, addr(0x201): []}
        ch.d["oracle"] = oracle
        ch.d["fillWindow"] = {s1: self.W}
        return ch

    best = staticmethod(lambda window, buy: window.index(min(window) if buy else max(window)))

    def test_a_fair_filler_at_once_is_no_flag(self):
        got = r.review(self.world().d, POOL, 0)["timing"]
        self.assertEqual((got["lateMedian"], got["filler"]["fills"]), (0, 12))
        self.assertFalse(got["filler"]["flag"])
        self.assertNotIn("timing", r.review(self.world().d, POOL, 0)["kinds"])

    def test_a_filler_who_picks_the_best_block_for_the_trader_is_flagged(self):
        got = r.review(self.world(pick=self.best).d, POOL, 0)
        f = got["timing"]["filler"]
        self.assertEqual((f["fills"], f["atBest"], f["flag"]), (12, 12, True))
        self.assertLess(f["p"], 0.001)
        self.assertGreater(f["gainBps"], 0)
        self.assertIn("timing", got["kinds"])
        self.assertEqual(got["action"], "decide")

    def test_a_filler_at_random_blocks_is_no_flag(self):
        rnd = [3]

        def anywhere(window, buy):
            rnd[0] = (rnd[0] * 69_069 + 1) % 2**32
            return rnd[0] % TimingW
        got = r.review(self.world(pick=anywhere, n=24).d, POOL, 0)["timing"]["filler"]
        self.assertFalse(got["flag"], got)
        self.assertLess(got["atBest"], 12, "a random block is the best about one time in five")

    def test_a_trader_a_block_ahead_of_the_oracle_is_flagged(self):
        ahead = lambda o, buy, i: o + 3 if buy else o - 3
        got = r.review(self.world(after=ahead).d, POOL, 0)
        lead = got["timing"]["lead"]
        self.assertEqual((lead["fills"], lead["ahead"], lead["flag"]), (12, 12, True))
        self.assertIn("the oracle moved the trader's way right after 12 of 12 fills", got["reasons"][0])

    def test_moves_that_go_either_way_are_no_flag(self):
        either = lambda o, buy, i: o + (3 if i % 4 < 2 else -3)
        lead = r.review(self.world(after=either).d, POOL, 0)["timing"]["lead"]
        self.assertEqual((lead["fills"], lead["ahead"], lead["flag"]), (12, 6, False))

    def test_too_few_fills_are_not_judged(self):
        got = r.review(self.world(n=5, pick=self.best, after=lambda o, buy, i: o + 3 if buy else o - 3).d, POOL, 0)["timing"]
        self.assertEqual((got["filler"]["flag"], got["lead"]["flag"]), (False, False))
        self.assertNotIn("p", got["filler"])
        self.assertNotIn("p", got["lead"], "five fills: not judged at all")


TimingW = Timing.W


class Recount(unittest.TestCase):
    """The stage's result counted again from its own events and the oracle's prices: trader 1's stage buys 100 of
    asset 3 at 1 000 in block 12, holds it over a day's snapshot in block 20, sells at 1 010 in block 30 and passes."""

    C = 10_000_000

    def world(self, *, buy_px=1_000, buy_source=1, sell_px=1_010, passed=None, oracle=None, snapshot=None, cost=1):
        ch, t1, s1 = honest_world()
        e = lambda name, block, **kw: {"name": name, "block": block, "index": 0, "t": 1_000 * block, "tx": tx(5_000 + block), **kw}
        cash = self.C - 100 * buy_px - cost
        events = [e("Started", 1, capital=self.C), e("Placed", 10, asset=3),
                  e("Filled", 12, asset=3, buy=True, sz=100, px=buy_px, cost=cost, late=0, source=buy_source),
                  e("DaySnapshot", 20, day=0, equity=cash + 100 * 1_005 if snapshot is None else snapshot),
                  e("Placed", 28, asset=3),
                  e("Filled", 30, asset=3, buy=False, sz=100, px=sell_px, cost=cost, late=0, source=1)]
        cash += 100 * sell_px - cost
        events.append(e("Passed", 31, equity=cash if passed is None else passed))
        ch.d["exam_events"] = {s1: events, addr(0x201): [e("Started", 0, capital=self.C), e("Passed", 0, equity=self.C)]}
        prices = {10: 990, 11: 1_000, 12: 1_000, 13: 1_000, 20: 1_005, 28: 1_010, 29: 1_010, 30: 1_010, 31: 1_010}
        prices.update(oracle or {})
        ch.d["oracle"] = {f"3:{b}": v for b, v in prices.items() if v is not None}
        return ch, s1

    def test_a_result_that_counts_up_is_clean(self):
        ch, s1 = self.world()
        got = r.review(ch.d, POOL, 0)
        rc = got["recount"]["stage"]
        self.assertEqual((rc["read"], rc["capital"], rc["fills"], rc["flags"]), (True, self.C, 2, []))
        self.assertEqual(rc["checked"], {"results": 1, "snapshots": 1, "unchecked": 0, "fills": 2})
        self.assertEqual(rc["moves"][0], {"tx": tx(5_012), "bps": -101.0}, "bought at 1 000 after ordering at 990: 1 % against it")
        self.assertEqual((got["verdict"], got["stopDraft"], got["action"]), ("clean", None, "approve"))

    def test_a_result_that_does_not_count_up_is_flagged_with_a_draft_of_reason_three(self):
        ch, s1 = self.world(passed=self.C + 999_999)
        got = r.review(ch.d, POOL, 0)
        [f] = got["recount"]["stage"]["flags"]
        self.assertEqual((f["what"], f["tx"]), ("result", tx(5_031)))
        self.assertEqual(got["kinds"], ["recount"])
        self.assertEqual(got["reasons"], ["recount on the stage: result"])
        d = got["stopDraft"]
        self.assertEqual((d["reason"], d["paired"], d["trades"], d["links"], d["data"]),
                         (3, r.ZERO_ADDRESS, [tx(5_031)], [r.ANALYSIS], None))
        self.assertEqual((got["action"], got["tx"]), ("hold", None), "no approval offered: nothing to sign until the analysis")
        k = r.card(got, ch.d)
        self.assertEqual((k["action"], k["call"], k["stopDraft"]["reason"]), ("hold", None, 3))
        self.assertIn("do not sign", r.summary(got))
        self.assertNotIn("to sign (approve)", r.summary(got))

    def test_a_snapshot_that_does_not_count_up_or_cannot_be_checked(self):
        ch, _ = self.world(snapshot=1)
        self.assertEqual([f["what"] for f in r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"]], ["snapshot"])
        ch, _ = self.world(oracle={20: None})
        rc = r.review(ch.d, POOL, 0)["recount"]["stage"]
        self.assertEqual((rc["flags"], rc["checked"]["unchecked"]), ([], 1), "a position open and no price: not guessed")

    def test_a_fill_better_than_the_oracle_or_off_it_at_the_oracle(self):
        ch, _ = self.world(buy_px=990)
        self.assertEqual([f["what"] for f in r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"]], ["better-than-oracle"])
        ch, _ = self.world(buy_px=1_001, buy_source=0)
        self.assertEqual([f["what"] for f in r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"]], ["not-the-oracle"])
        ch, _ = self.world(buy_px=1_001, buy_source=1)
        self.assertEqual(r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"], [], "worse than the oracle at the book is allowed")

    def test_one_oracle_price_far_from_both_neighbours_is_an_outlier(self):
        ch, _ = self.world(buy_px=1_100, oracle={12: 1_100})
        flags = r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"]
        self.assertEqual([(f["what"], f["block"]) for f in flags], [("oracle-outlier", 12)])
        ch, _ = self.world(buy_px=1_100, oracle={12: 1_100, 13: 1_100})
        self.assertEqual(r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"], [], "a move that stays is no outlier")

    def test_an_oracle_far_from_the_venue_by_the_file(self):
        ch, _ = self.world()
        ch.d["venue"] = {"3": {str(12_000 // 60 * 60): 1_050}}
        self.assertEqual([f["what"] for f in r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"]], ["oracle-off-venue"])
        ch.d["venue"] = {"3": {str(12_000 // 60 * 60): 1_005}}
        self.assertEqual(r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"], [], "half a percent: within it")

    def test_a_cost_the_terms_do_not_give(self):
        ch, s1 = self.world(cost=1)
        ch.d["costPpm"] = {s1: 950}
        flags = r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"]
        self.assertEqual([f["what"] for f in flags], ["cost", "cost"], "100 x 1 000 at 950 ppm is 95, not 1")
        ch.d["costPpm"] = {s1: 9}
        self.assertEqual(r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"], [], "100 000 x 9 / 1e6 is 1, rounded up")

    def test_a_pass_in_two_fills_on_the_largest_size_is_a_profile_not_a_reason(self):
        ch, _ = self.world()
        got = r.review(ch.d, POOL, 0)
        self.assertIsNotNone(got["recount"]["stage"]["profile"])
        self.assertEqual(got["verdict"], "clean")
        ch.d["books"][POOL]["sizes"] = [SIZE, 2 * SIZE]
        self.assertIsNone(r.review(ch.d, POOL, 0)["recount"]["stage"]["profile"], "not the largest size")

    def test_an_exam_of_two_steps_starts_its_second_on_the_capital_again(self):
        ch, _ = self.world()
        e = lambda name, block, **kw: {"name": name, "block": block, "index": 0, "t": 1_000 * block, "tx": tx(6_000 + block), **kw}
        fill = lambda block, buy, px: e("Filled", block, asset=3, buy=buy, sz=100, px=px, cost=1, late=0, source=1)
        first = self.C - 100 * 1_000 - 1 + 100 * 1_010 - 1
        second = self.C + 100 * 1_010 - 1 - 100 * 1_000 - 1  # a short in the second step: it starts on C, not on `first`
        ch.d["exam_events"][addr(0x201)] = [e("Started", 1, capital=self.C), fill(12, True, 1_000), fill(30, False, 1_010),
                                            e("StepPassed", 31, step=0, equity=first),
                                            fill(40, False, 1_010), fill(41, True, 1_000), e("Passed", 42, equity=second)]
        rc = r.review(ch.d, POOL, 0)["recount"]["exam"]
        self.assertEqual((rc["checked"]["results"], rc["flags"]), (2, []))

    def test_an_account_whose_events_were_not_read_is_said(self):
        ch, s1 = self.world()
        del ch.d["exam_events"][s1]
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["recount"]["stage"]["read"], got["kinds"]), (False, ["unread"]))

    def test_with_a_pair_the_stop_is_the_pairs_and_no_draft_is_made(self):
        ch, s1 = self.world(passed=1)
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        ch.fill(s1, buy=True, t=60_000, asset=5)
        ch.fill(e9, buy=False, t=60_100, asset=5)
        ch.d["exam_events"][e9] = []
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["stop"]["by"], got["stopDraft"]), ("pair", None))
        self.assertIn("recount", got["kinds"])


class APairFundedFromOneWallet(unittest.TestCase):
    def test_a_sender_of_both_makes_it_reason_two_with_both_transfers(self):
        ch, t1, s1 = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        funder = addr(0xF1)
        to1, to9 = ch.send(funder, t1), ch.send(funder, t9)
        ch.send(funder, t1)  # a second transfer: the first from each sender is the one named
        mine = ch.fill(s1, buy=True, t=60_000)
        other = ch.fill(e9, buy=False, t=60_100)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["stop"]["reason"], 2)
        self.assertEqual(got["stop"]["links"], [to1, to9])
        self.assertEqual(got["stop"]["trades"], [mine, other])
        counted = [l for l in got["links"] if not l["wide"]]
        self.assertEqual(counted, [{"trader": t9, "sender": funder, "txs": [to1, to9], "wide": False}])
        self.assertEqual(len(got["reasons"]), 2)
        self.assertIn("wallet linked to " + t9, got["reasons"][1])
        _, reason, _, _, links = decode(chk.STOP_TYPES, bytes.fromhex(got["tx"]["data"][10:]))
        self.assertEqual((reason, ["0x" + h.hex() for h in links]), (2, [to1, to9]))

    def test_one_paying_the_other_is_a_link(self):
        ch, t1, s1 = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        direct = ch.send(t9, t1)
        ch.fill(s1, buy=True, t=60_000)
        ch.fill(e9, buy=False, t=60_100)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["stop"]["reason"], got["stop"]["links"]), (2, [direct]))

    def test_a_link_without_a_pair_is_suspicious_but_no_stop(self):
        ch, t1, _ = honest_world()
        t9 = addr(0x199)
        ch.sale(t9, addr(0x299))
        funder = addr(0xF1)
        ch.send(funder, t1)
        ch.send(funder, t9)
        got = r.review(ch.d, POOL, 0)
        self.assertIsNone(got["stop"])
        self.assertEqual(got["verdict"], "suspicious")
        self.assertEqual(got["tx"]["kind"], "approve")
        self.assertIn("no pair", got["tx"]["note"])


class ACodeBoughtUp(unittest.TestCase):
    CODE = "0x" + "22" * 32

    def world(self, traded: int):
        ch, t1, s1 = honest_world()
        owner = addr(0xC0)
        ch.d["sales"][0]["code"], ch.d["sales"][0]["partner"] = self.CODE, owner  # trader 1 bought under it
        for i in range(12):
            t, e = addr(0x400 + i), addr(0x500 + i)
            ch.sale(t, e, code=self.CODE, partner=owner)
            if i < traded:
                ch.fill(e, buy=True, t=1_000 + i)
        ch.sale(owner, addr(0x5FF), code=self.CODE, partner=owner)
        return ch

    def test_a_run_of_new_wallets_that_never_trade_flags_the_code(self):
        got = r.review(self.world(traded=0).d, POOL, 0)
        k = got["code"]
        self.assertEqual((k["code"], k["sales"], k["wallets"], k["run"]), (self.CODE, 14, 14, 13))
        self.assertEqual((k["idle"], k["paired"], k["partnerBuys"], k["flag"]), (13, 0, True, True))
        self.assertEqual(got["verdict"], "suspicious")
        self.assertIn("13 sales in a row from new wallets", got["reasons"][0])
        self.assertEqual(got["tx"]["kind"], "approve", "a code is no reason the pool takes for a stop")
        self.assertIn("no pair", got["tx"]["note"])
        self.assertEqual(got["action"], "decide", "suspicious, the result counts up: the choice is the signer's")
        self.assertIn("wait for the ceiling", got["tx"]["note"])

    def test_the_same_run_of_buyers_who_trade_is_no_flag(self):
        got = r.review(self.world(traded=12).d, POOL, 0)
        self.assertEqual((got["code"]["idle"], got["code"]["flag"]), (1, False))
        self.assertEqual(got["verdict"], "clean")

    def test_buyers_who_never_trade_but_not_in_a_run_are_no_flag(self):
        ch, _, _ = honest_world()
        for i in range(12):
            ch.sale(addr(0x400 + i), addr(0x500 + i), code=self.CODE, partner=addr(0xC0))
            ch.sale(addr(0x600 + i), addr(0x700 + i))  # another code's sale between each two
        ch.d["sales"][0]["code"] = self.CODE
        k = r.review(ch.d, POOL, 0)["code"]
        self.assertEqual((k["idle"], k["run"], k["flag"]), (12, 1, False))

    def test_the_run_counts_sales_in_a_row_each_from_a_new_wallet(self):
        k, x = "0x" + "22" * 32, "0x" + "33" * 32
        sales = [{"code": c_, "trader": t} for c_, t in
                 [(k, "a"), (k, "b"), (x, "c"), (k, "d"), (k, "e"), (k, "f"), (k, "e"), (k, "g")]]
        self.assertEqual(r.longest_new_wallet_run(sales, k), 3)
        self.assertEqual(r.longest_new_wallet_run(sales, x), 1)
        self.assertEqual(r.longest_new_wallet_run([], k), 0)
        again = [{"code": k, "trader": w} for w in ["d", "e", "f", "e", "g", "h"]]
        self.assertEqual(r.longest_new_wallet_run(again, k), 4, "f e g h: the run goes on after the earlier e")


class LateFills(unittest.TestCase):
    def test_a_trader_filled_later_than_everyone_else_is_suspicious(self):
        ch, t1, s1 = honest_world()
        for i in range(6):
            ch.fill(s1, buy=bool(i % 2), t=100_000 + i * 10_000, asset=9, late=20)
        for e in [addr(0x210 + i) for i in range(8)]:
            ch.fill(e, buy=True, t=500_000, asset=8, late=1)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["late"]["trader"], {"n": 10, "median": 20, "max": 20})
        self.assertTrue(got["late"]["outlier"])
        self.assertIn("fills later than everyone else's", got["reasons"])

    def test_too_few_fills_say_nothing(self):
        self.assertFalse(r.late_outlier([50, 50], [0] * 20, 5))
        self.assertFalse(r.late_outlier([50] * 9, [0] * 4, 5))
        self.assertTrue(r.late_outlier([3] * 5, [0] * 8 + [2, 2], 5))
        self.assertFalse(r.late_outlier([2] * 5, [0] * 8 + [2, 2], 5), "at the others' p90 is not above it")


class Unread(unittest.TestCase):
    def test_an_account_not_read_is_said(self):
        ch, _, s1 = honest_world()
        ch.d["unread"].append(s1)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["verdict"], "suspicious")
        self.assertEqual(got["reasons"], ["not read: " + s1])


class TheSummary(unittest.TestCase):
    def test_it_says_the_verdict_and_what_to_sign(self):
        ch, _, _ = honest_world()
        text = r.summary(r.review(ch.d, POOL, 0))
        self.assertIn("800.00 USDC", text)
        self.assertIn("verdict: clean", text)
        self.assertIn("to sign (approve): " + POOL + " approvePayouts(uint256[]) 0x", text)
        self.assertNotRegex(text, r"[\u0400-\u04ff]")


class TheDocument(unittest.TestCase):
    """The JSON a page reads: its fields fixed by the schema's name."""

    KEYS = {"schema", "pool", "id", "trader", "stage", "exam", "amount", "claimedAt", "dueAt", "hardAt", "verdict", "action",
            "reasons", "evidence", "call", "stopDraft", "batchApproval", "dailyCap", "readAt"}

    def test_a_clean_payout_carries_its_approval(self):
        ch, t1, s1 = honest_world()
        k = r.card(r.review(ch.d, POOL, 0), ch.d)
        self.assertEqual(set(k), self.KEYS)
        self.assertEqual((k["schema"], k["pool"], k["id"], k["trader"], k["stage"]), ("payout-review/3", POOL, 0, t1, s1))
        self.assertEqual(k["amount"], str(800 * 10**6), "a string: a JSON number loses digits past 2**53")
        self.assertEqual((k["verdict"], k["reasons"]), ("clean", []))
        self.assertEqual((k["call"]["kind"], k["call"]["method"], k["call"]["args"]), ("approve", "approvePayouts", [[0]]))
        self.assertEqual(k["evidence"]["running"]["worst"], str(8 * 800 * 10**6))
        self.assertEqual(k["readAt"], {"block": 900, "time": ch.d["now"]})
        json.dumps(k)

    def test_each_reason_is_named_by_its_kind(self):
        ch, t1, s1 = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        funder = addr(0xF1)
        ch.send(funder, t1)
        ch.send(funder, t9)
        ch.fill(s1, buy=True, t=60_000)
        ch.fill(e9, buy=False, t=60_100)
        ch.d["unread"].append(s1)
        k = r.card(r.review(ch.d, POOL, 0), ch.d)
        self.assertEqual([x["kind"] for x in k["reasons"]], ["pair", "link", "unread"])
        self.assertEqual((k["call"]["kind"], k["call"]["method"]), ("stop", "stopPayout"))
        self.assertEqual(k["evidence"]["stop"]["reason"], 2)

    def test_out_writes_one_file_a_payout(self):
        import tempfile
        ch, _, _ = honest_world()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(r.c, "assert_testnet"), \
                mock.patch.object(r.deployments, "load", return_value={"SimPoolFactory": addr(0xFA)}), \
                mock.patch.object(r, "read_chain", return_value=ch.d), \
                mock.patch("builtins.print"):
            self.assertEqual(r.main(["--deployment", "x", "--out", d]), 0)
            got = json.loads((pathlib.Path(d) / f"{POOL}-0.json").read_text())
        self.assertEqual((got["schema"], got["id"]), ("payout-review/3", 0))


class ReadingTheChain(unittest.TestCase):
    """`read_chain` against a node made up here: the logs as the contracts write them."""

    def test_it_reads_sales_stages_payouts_the_accounts_events_the_oracle_and_transfers(self):
        factory = addr(0xFA)
        t1, e1, s1, partner = addr(0x101), addr(0x201), addr(0x301), addr(0xB0)
        code = b"\x11" * 32
        capital = 10_000_000_000

        def word(a):
            return "0x" + "00" * 12 + a[2:].lower()

        def log(address, topics, data, block, h, index=0):
            return {"address": address, "topics": topics, "data": "0x" + data.hex(), "blockNumber": hex(block),
                    "transactionHash": h, "logIndex": hex(index)}

        num = lambda n: "0x" + f"{n:064x}"
        pool_logs = [
            log(POOL, [sk.EXAM_SOLD, word(e1), word(t1), word(partner)],
                encode(["uint256", "uint64", "uint64", "uint256", "bool", "bytes32"], [2, SIZE, 1_000, 0, False, code]), 11, tx(1)),
            log(POOL, [sk.FUNDED, word(s1), word(e1), word(t1)], b"", 20, tx(2)),
            log(POOL, [r.PAYOUT_DUE, "0x" + "00" * 31 + "07", word(s1), word(t1)],
                encode(["uint64", "uint64"], [800, 5_000]), 30, tx(3)),
        ]
        filled = lambda buy, px: encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"], [buy, 100, px, 1, 4, 1])
        exam_logs = [
            log(e1, [r.STARTED, word(t1), word(POOL)], encode(["uint64"], [capital]), 12, tx(10)),
            log(e1, [r.PASSED, word(t1)], encode(["int256"], [capital]), 19, tx(11)),
            log(s1, [r.STARTED, word(t1), word(POOL)], encode(["uint64"], [capital]), 21, tx(12)),
            log(s1, [sk.PLACED, num(3)], encode(["bool", "uint64", "uint256", "uint256"], [True, 100, 24, 33]), 23, tx(13)),
            log(s1, [sk.FILLED, num(3)], filled(True, 7), 25, tx(4)),
            log(s1, [sk.PLACED, num(3)], encode(["bool", "uint64", "uint256", "uint256"], [False, 100, 27, 36]), 26, tx(14)),
            log(s1, [sk.FILLED, num(3)], filled(False, 8), 28, tx(15)),
            log(s1, [r.PASSED, word(t1)], encode(["int256"], [capital - 700 - 1 + 800 - 1]), 29, tx(16)),
        ]
        usdc_logs = [log(USDC, [r.TRANSFER, word(EXCHANGE), word(t1)], encode(["uint256"], [5]), 5, tx(5))]
        asked = []

        def rpc(method, params=()):
            if method == "eth_blockNumber":
                return hex(40)
            if method == "eth_getBlockByNumber":
                return {"timestamp": hex(1_000 if params[0] == "latest" else 100 * int(params[0], 16))}
            if method == "eth_call":
                block = int(params[1], 16)
                asked.append((int(params[0]["data"], 16), block))
                self.assertEqual(params[0]["to"], r.ORACLE_PX)
                return "0x" + encode(["uint64"], [7 if block <= 26 else 8]).hex()
            q = params[0]
            if q["address"] == USDC:
                return usdc_logs
            if q["topics"] == []:
                return [l for l in exam_logs if l["address"] in q["address"]]
            return pool_logs

        views = {("pools()",): [POOL], ("owed()",): 800, ("held()",): 0, ("reserved()",): 0,
                 ("payout(uint256)",): (s1, t1, 800, 5_000, 1, False, 0)}

        def view(a, sig, out, types=(), args=()):
            return views[(sig,)]

        def call_view(to, sig, types, args, out):
            return {"balanceOf(address)": (9_000,), "result()": (2, t1, 0, 0), "accountOf(address)": (2, 0, True),
                    "menu()": ([(SIZE // 2, 50), (SIZE, 100)],),
                    "terms()": ((capital, [800], 0, 0, 0, 1_000, 1, 10),)}[sig]

        with mock.patch.object(r.c, "rpc", side_effect=rpc), mock.patch.object(r.core, "view", side_effect=view), \
                mock.patch.object(r.c, "call_view", side_effect=call_view):
            got = r.read_chain({"SimPoolFactory": factory, "usdc": USDC, "block": 10}, prices={"9:9": 1})
        self.assertEqual((got["now"], got["latest"], got["pools"]), (1_000, 40, [POOL]))
        self.assertEqual(got["sales"], [{"pool": POOL, "exam": e1, "trader": t1, "partner": partner, "line": 2,
                                         "size": SIZE, "price": 1_000, "fee": 0, "code": "0x" + code.hex(), "block": 11, "tx": tx(1)}])
        self.assertEqual(got["funded"], [{"pool": POOL, "stage": s1, "exam": e1, "trader": t1, "block": 20}])
        self.assertEqual(got["payouts"], [{"pool": POOL, "id": 7, "account": s1, "trader": t1, "amount": 800,
                                           "dueAt": 5_000, "block": 30, "state": "Due", "wasStopped": False}])
        self.assertEqual(got["fills"], [{"account": s1, "asset": 3, "buy": True, "sz": 100, "t": 2_500, "tx": tx(4), "late": 4},
                                        {"account": s1, "asset": 3, "buy": False, "sz": 100, "t": 2_800, "tx": tx(15), "late": 4}])
        self.assertEqual([e["name"] for e in got["exam_events"][s1]], ["Started", "Placed", "Filled", "Placed", "Filled", "Passed"])
        self.assertEqual(got["ended"], {e1: 1_900, s1: 2_900})
        self.assertEqual(got["costPpm"], {s1: 1_000, e1: 1_000})
        self.assertEqual(sorted({b for _, b in asked}), list(range(21, 34)),
                         "each fill's and order's block, their neighbours, and every block of each order's window of 10")
        self.assertEqual(got["fillWindow"], {s1: 10, e1: 10})
        self.assertEqual({a for a, _ in asked}, {3})
        self.assertEqual((got["oracle"]["3:25"], got["oracle"]["3:28"], got["oracle"]["9:9"]), (7, 8, 1), "the file stands in too")
        self.assertEqual(got["transfers"], [{"from": EXCHANGE, "to": t1, "tx": tx(5), "block": 5}])
        self.assertEqual(got["books"], {POOL: {"balance": 9_000, "owed": 800, "held": 0, "reserved": 0, "sizes": [SIZE // 2, SIZE]}})
        self.assertEqual((got["status"][s1], got["done"][s1], got["unread"]), ("Passed", True, []))
        review = r.review(got, POOL, 7)
        self.assertEqual((review["exam"], review["size"], review["verdict"]), (e1, SIZE, "clean"))
        rc = review["recount"]["stage"]
        self.assertEqual((rc["capital"], rc["fills"], rc["checked"]["results"], rc["checked"]["fills"], rc["flags"]),
                         (capital, 2, 1, 2, []))
        self.assertEqual(rc["profile"], "passed in 2 fills over 1 days on the largest size: a pass that could be luck, "
                                        "not a sign of cheating")
        self.assertNotIn("profile", review["kinds"], "a profile is said, not counted against the trader")


class MissingParts(unittest.TestCase):
    def test_parts_given_as_none_are_parts_not_read(self):
        ch, _, s1 = honest_world()
        ch.d.update(ended=None, oracle=None, venue=None, costPpm=None, unread=None)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["verdict"], "clean")
        self.assertEqual(r.exam_event_of({"topics": [], "data": "0x", "blockNumber": "0x1", "transactionHash": "0x"}, 0), None,
                         "an anonymous log is none of PaperExam's")


class AgainstTheContracts(unittest.TestCase):
    def test_the_events_and_the_payout_share_are_the_contracts(self):
        pool = (ROOT / "src/sim/SimPool.sol").read_text()
        factory = (ROOT / "src/sim/SimPoolFactory.sol").read_text()
        self.assertIn("event PayoutDue(uint256 indexed id, address indexed account, address indexed trader, "
                      "uint64 amount, uint64 dueAt);", pool)
        self.assertEqual(r.PAYOUT_DUE, "0x" + keccak(text="PayoutDue(uint256,address,address,uint64,uint64)").hex())
        self.assertRegex(factory, rf"PAYOUT_BPS = {r.PAYOUT_BPS};")
        self.assertIn("function stopPayout(uint256 id, uint8 reason, address paired, bytes32[] calldata trades, "
                      "bytes32[] calldata links)", pool)
        m = re.search(r"event ExamSold\(\s*address indexed exam,\s*address indexed trader,\s*uint256 line,\s*uint64 size,"
                      r"\s*uint64 price,\s*uint256 fee,\s*bool bySignature,\s*bytes32 code,\s*address indexed partner", pool)
        self.assertIsNotNone(m, "sale_of reads the data in this order")

    def test_it_reads_and_never_sends(self):
        src = (ROOT / "ops/payout_review.py").read_text()
        for word in ("c.transact(", "c.account(", "sign_transaction", "eth_sendRawTransaction", "private_key", "--wallet"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
