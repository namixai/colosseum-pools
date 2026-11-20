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

    def test_the_delay_and_the_ceiling_are_the_factorys_own(self):
        ch, _, _ = honest_world()
        ch.d["times"] = {"delay": 48 * 3600, "hard": 8 * 86_400}
        got = r.review(ch.d, POOL, 0)
        due = ch.d["now"] + 3_600
        self.assertEqual((got["claimedAt"], got["hardAt"]), (due - 48 * 3600, due - 48 * 3600 + 8 * 86_400))
        self.assertIn(f"paid unapproved from {got['hardAt']}", r.summary(got))

    def test_a_ceiling_not_read_offers_nothing_and_says_no_time(self):
        for times in ({"delay": 72 * 3600, "hard": None}, {"delay": None, "hard": 7 * 86_400}):
            ch, _, _ = honest_world()
            ch.d["times"] = times
            got = r.review(ch.d, POOL, 0)
            self.assertEqual((got["tx"]["kind"], got["tx"]["data"], got["hardAt"], got["stoppable"]), ("none", None, None, None))
            self.assertIn("not read the factory's delay and ceiling", got["tx"]["note"])
            self.assertEqual(got["reasons"], ["not read: the factory's delay and ceiling"])
            self.assertIn("not read when it is paid", r.summary(got))
        ch, _, _ = honest_world()
        ch.d["times"] = {"delay": None, "hard": None}
        ch.d["books"][POOL]["approvals"] = False
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["claimedAt"], got["tx"]["note"], got["reasons"]), (None, r.NO_APPROVAL, []),
                         "a pool without approvals needs no ceiling: it pays at its due time")

    def test_the_factorys_times_are_read_each_or_none(self):
        F = addr(0xFA)

        def call_view(to, sig, types, args, out):
            if sig == "PAYOUT_HARD()":
                raise Exception("execution reverted")
            return {"PAYOUT_DELAY()": (72 * 3600,)}[sig]

        with mock.patch.object(r.c, "call_view", side_effect=call_view):
            self.assertEqual(r.payout_times(F), {"delay": 72 * 3600, "hard": None})

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
        ch.d["books"][POOL].update(sharesDue=400 * 10**6)
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual(got["free"], 5_500 * 10**6, "nor what the codes accrued and their owners have not claimed")
        # Not read (the node failed): unknown, never enough; balance 1 000, 400 kept for codes, 800 running.
        ch.d["books"][POOL].update(balance=1_000 * 10**6 + 800 * 10**6, held=0, reserved=0, sharesDue=None)
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual((got["free"], got["enough"]), (None, None), "what the codes keep was not read: unknown")
        card = r.card(r.review(ch.d, POOL, 0), ch.d)["evidence"]["running"]
        self.assertEqual((card["free"], card["enough"]), (None, None), "the card says null, not the text None")
        self.assertIn('"free": null', json.dumps(card))

    def test_what_the_codes_keep_is_read_as_0_on_an_old_pool_and_as_unknown_when_the_read_fails(self):
        from unittest import mock
        with mock.patch.object(r.core, "view", return_value=400):
            self.assertEqual(r.share_reserve(POOL), 400)
        with mock.patch.object(r.core, "view", side_effect=RuntimeError("execution reverted")):
            self.assertEqual(r.share_reserve(POOL), 0, "an old pool has no getter and keeps nothing for codes")
        with mock.patch.object(r.core, "view", side_effect=RuntimeError("429 Too Many Requests")):
            self.assertIsNone(r.share_reserve(POOL), "a failed read is not 0")

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

    def test_a_fill_that_turns_a_position_over_is_the_fill_that_opened_the_new_one(self):
        fill = lambda tx_, buy, sz, t_: {"account": "x", "asset": 7, "buy": buy, "sz": sz, "t": t_, "tx": tx_}
        # Short 100, then a buy of 150: long 50, the buy opened it. Against another account's short of 50.
        mine = [fill("sell", False, 100, 0), fill("turn", True, 150, 1_000)]
        got = r.mirror(mine, [fill("theirs", False, 50, 1_100)], 5_000, 0.5)
        self.assertEqual(got["examples"], [["turn", "theirs"]], "the buy that turned it over, not the sell it undid")
        # A fill that only makes the position smaller opens nothing.
        smaller = [fill("sell", False, 100, 0), fill("less", True, 30, 1_000)]
        self.assertEqual(r.mirror(smaller, [fill("theirs", True, 50, 1_100)], 5_000, 0.5)["examples"], [["sell", "theirs"]])

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

    def test_a_claim_is_dated_by_the_factorys_delay_in_the_income_window(self):
        ch = self.world(40, 5)
        # Due 60 hours into the window: claimed 12 hours before it on a 72-hour delay, 12 hours into it on a 48-hour one.
        ch.d["payouts"][0].update(amount=3_100 * 10**6, dueAt=ch.d["now"] - 30 * 86_400 + 60 * 3_600)
        self.assertEqual(r.review(ch.d, POOL, 0)["passRates"]["payoutsToIncome"]["ratio"], 0.0, "the design's 72 hours")
        ch.d["times"] = {"delay": 48 * 3_600, "hard": 7 * 86_400}
        pr = r.review(ch.d, POOL, 0)["passRates"]
        self.assertEqual((pr["payoutsToIncome"]["ratio"], pr["payoutsToIncome"]["level"], pr["batch"]), (0.775, "red", False))

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

    def test_a_price_of_zero_after_the_fill_is_no_move_for_the_lead_test(self):
        got = r.review(self.world(n=16, after=lambda o, buy, i: o if buy else 0).d, POOL, 0)
        self.assertEqual((got["timing"]["lead"]["fills"], got["timing"]["lead"]["flag"]), (0, False),
                         "eight sells followed by a zero price are eight unavailable prices, not eight moves")
        self.assertNotIn("timing", got["kinds"])

    def test_a_zero_oracle_price_in_a_window_is_not_judged_and_not_divided_by(self):
        ch = self.world(pick=self.best)
        ch.d["oracle"]["9:1000"] = 0  # the first block of the first order's window
        got = r.review(ch.d, POOL, 0)["timing"]
        self.assertEqual(got["filler"]["fills"], 11, "that order is left out; the other eleven are still judged")
        self.assertTrue(got["filler"]["flag"])

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

    def test_only_an_unapproved_payout_of_a_pool_with_approvals_is_held(self):
        ch, _ = self.world(passed=self.C + 999_999)
        held = r.review(ch.d, POOL, 0)
        self.assertEqual((held["action"], held["tx"]), ("hold", None))
        ch.d["payouts"][0]["approved"] = True  # payable from its due time whatever the recount says
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["action"], got["tx"]["kind"], got["tx"]["note"]), ("none", "none", r.APPROVED))
        self.assertIsNotNone(got["stopDraft"], "the draft of a stop stays")
        self.assertNotIn("until None", r.summary(got))
        ch.d["payouts"][0]["approved"] = False
        ch.d["books"][POOL]["approvals"] = False  # a pool without approvals pays by itself: nothing to hold back
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["action"], got["tx"]["note"]), ("none", r.NO_APPROVAL))
        self.assertNotIn("None", r.summary(got))
        self.assertIsNotNone(got["stopDraft"])

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


class ACoinFlip(unittest.TestCase):
    """The profile of a pass that needs no view of the market, on a stage of 10 000 with 3 % a day: round trips at
    a small leverage, a loss closed just over the day's line, nothing more that day, the pass by the last one."""

    C = 10_000 * 10**6
    DAY = 86_400
    PX = 1_000_000

    @classmethod
    def events(cls, trips, from_day=0):
        """Each of `trips` is (UTC day, leverage, result in bps of the capital[, second of the day]): a buy and a
        sell of asset 3, ten minutes apart, with a snapshot at each new day's start, and the pass after the last.
        `from_day` moves the whole account that many days on."""
        n = [0]

        def e(name, t, **kw):
            n[0] += 1
            return {"name": name, "block": n[0], "index": 0, "t": t + from_day * cls.DAY, "tx": tx(7_000 + n[0]), **kw}

        events, cash, day, t = [e("Started", 0, capital=cls.C)], cls.C, 0, 0
        for d, lev, bps, *at in trips:
            t = d * cls.DAY + (at[0] if at else 3_600)
            if d != day:
                events.append(e("DaySnapshot", d * cls.DAY + 60, day=d, equity=cash))
                day = d
            sz = int(cash * lev) // cls.PX
            out = cls.PX + (cls.C * bps // 10_000) // sz
            events += [e("Filled", t, asset=3, buy=True, sz=sz, px=cls.PX, cost=0, late=0, source=1),
                       e("Filled", t + 600, asset=3, buy=False, sz=sz, px=out, cost=0, late=0, source=1)]
            cash += sz * (out - cls.PX)
        return events + [e("Passed", t + 700, equity=cash)]

    def world(self, trips, daily=300):
        ch, _, s1 = honest_world()
        ch.d["exam_events"] = {s1: self.events(trips), addr(0x201): [{"name": "Started", "block": 0, "index": 0, "t": 0, "tx": tx(1), "capital": self.C},
                                                                     {"name": "Passed", "block": 0, "index": 0, "t": 0, "tx": tx(2), "equity": self.C}]}
        if daily is not None:
            ch.d["dailyLossBps"] = {s1: daily}
        return ch

    def flip(self, trips, **kw):
        return r.review(self.world(trips, **kw).d, POOL, 0)["recount"]["stage"]["flip"]

    PLAYER = [(0, 1.5, -290), (1, 1.5, -270), (2, 1.5, 1_600)]

    def test_two_stops_over_the_day_line_a_day_apart_and_a_pass_are_the_profile(self):
        ch = self.world(self.PLAYER)
        got = r.review(ch.d, POOL, 0)
        f = got["recount"]["stage"]["flip"]
        self.assertEqual((f["trips"], f["stops"], f["leverage"]), (3, 2, 1.5))
        self.assertIn("coin flip with a stop over the daily line", f["text"])
        self.assertEqual(got["recount"]["stage"]["flags"], [], "the result counts up")
        self.assertEqual((got["verdict"], got["kinds"], got["reasons"], got["action"]), ("clean", [], [], "approve"),
                         "said, never counted: no reason, no colour, the approval as it was")
        self.assertIn("  profile of the stage: passed in 3 round trips at 1.50x at most", r.summary(got))
        self.assertEqual(r.card(got, ch.d)["evidence"]["recount"]["stage"]["flip"], f)

    def test_one_round_trip_to_the_target_is_the_same_game_won_at_once(self):
        f = self.flip([(0, 1.5, 1_100)], daily=None)
        self.assertEqual((f["trips"], f["stops"], f["leverage"]), (1, 0, 1.5))
        self.assertIn("one round trip", f["text"])

    def test_a_trader_with_stops_is_not_the_profile(self):
        self.assertIsNone(self.flip([(0, 2, -100), (1, 2, 1_200)]), "a stop at 1 %, far over the day's line")
        self.assertIsNone(self.flip([(0, 2, -100, 3_600), (0, 2, 150, 7_200), (1, 2, -80), (1, 2, 300, 20_000),
                                     (2, 2, 400), (3, 2, 400)]), "stops, and back in the market the same day")
        self.assertIsNotNone(self.flip([(0, 1.5, -240), (1, 1.5, 1_300)]), "80 % of the day's 3 % gone: at the line")
        self.assertIsNone(self.flip([(0, 1.5, -239), (1, 1.5, 1_300)]), "a little less: not at the line")
        self.assertIsNone(self.flip([(0, 1.5, -290), (1, 1.5, -10), (2, 1.5, 1_600)]),
                          "the second stop at 0.1 % of its own day's start: each day's line is counted from its snapshot")

    def test_back_in_the_market_the_same_day_is_not_the_profile(self):
        self.assertIsNone(self.flip([(0, 1.5, -290, 3_600), (0, 1.5, 1_400, 40_000)]))
        self.assertIsNotNone(self.flip([(0, 1.5, -290, 80_000), (1, 1.5, 1_400, 60)]), "an hour and a half later, the next day")

    def test_a_large_leverage_or_many_round_trips_are_not_the_profile(self):
        self.assertIsNotNone(self.flip([(0, 3, -290), (1, 3, 1_400)]))
        self.assertIsNone(self.flip([(0, 3.1, -290), (1, 3, 1_400)]), "over 3 times the account")
        four = [(0, 1.5, -290), (1, 1.5, -270), (2, 1.5, 0), (3, 1.5, 1_700)]
        self.assertIsNone(self.flip(four), "the third closed at nothing: no stop at the line")
        stops = [(0, 1, -290), (1, 1, -270), (2, 1, -230), (3, 1, -225)]
        self.assertIsNotNone(self.flip(stops[:3] + [(3, 1, 2_500)]), "four in a step")
        self.assertIsNone(self.flip(stops + [(4, 1, 2_500)]), "five in a step")

    def test_a_day_line_not_read_holds_no_loss_to_it(self):
        self.assertIsNone(self.flip(self.PLAYER, daily=None))

    def test_round_trips_too_small_to_be_a_position_are_not_counted_either_way(self):
        dust = self.PLAYER + [(2, 0.01, 0, 50_000), (3, 0.04, 1)]
        f = self.flip(dust)
        self.assertEqual((f["trips"], f["stops"]), (3, 2), "what fills a trading day after the target")
        self.assertIsNone(self.flip([(0, 0.04, 1)]), "nothing but dust is no round trip at all")
        self.assertIsNone(self.flip(self.PLAYER + [(3, 0.06, 1)]), "5 % of the account and more is a position: the pass is its")

    def test_an_exam_of_two_steps_is_held_to_it_a_step_at_a_time(self):
        ch = self.world([(0, 1.5, -290), (1, 1.5, 1_400)])
        s1 = addr(0x301)
        ev = ch.d["exam_events"][s1]
        second = [dict(x, block=x["block"] + 100, t=x["t"] + 5 * self.DAY, tx=tx(8_000 + x["block"])) for x in ev[1:]]
        first_end = dict(ev[-1], name="StepPassed", step=0)
        # The second step starts on the capital again: the same fills, five days on, lose and win what they did.
        ch.d["exam_events"][s1] = ev[:-1] + [first_end] + second
        rc = r.review(ch.d, POOL, 0)["recount"]["stage"]
        self.assertEqual((rc["flags"], rc["flip"]["trips"], rc["flip"]["stops"]), ([], 4, 2))

    def test_the_days_of_every_step_count_as_the_fills_of_every_step_do(self):
        ch = self.world([(0, 1.5, -290), (1, 1.5, 1_400)])
        s1 = addr(0x301)
        ev = ch.d["exam_events"][s1]
        second = [dict(x, block=x["block"] + 100, t=x["t"] + 5 * self.DAY, tx=tx(8_000 + x["block"])) for x in ev[1:]]
        ch.d["exam_events"][s1] = ev[:-1] + [dict(ev[-1], name="StepPassed", step=0)] + second
        rc = r.review(ch.d, POOL, 0)["recount"]["stage"]
        self.assertEqual((rc["fills"], rc["days"]), (8, 4), "days 0 and 1 of the first step, 5 and 6 of the second")

    def test_the_pool_counts_its_passes_in_this_shape_over_the_window_of_its_share_of_passes(self):
        ch = PassRates.world(PassRates(), 40, 10)
        exams = [addr(0x2000 + 0x1000 + i) for i in range(40)]
        for ex in exams[:3]:
            ch.d["exam_events"][ex] = self.events(self.PLAYER)
        ch.d["exam_events"][exams[3]] = self.events([(0, 1.5, 1_100)])
        ch.d["exam_events"][exams[39]] = self.events(self.PLAYER)[:-1]  # the same fills on an exam that did not pass
        ch.d["dailyLossBps"] = {ex: 300 for ex in exams}
        got = r.review(ch.d, POOL, 0)
        self.assertEqual(got["passRates"]["coinFlipPasses"], {"hits": 4, "withStops": 3, "passes": 10, "share": 0.4})
        plain = r.review(PassRates.world(PassRates(), 40, 10).d, POOL, 0)
        self.assertEqual((got["passRates"]["level"], got["kinds"], got["action"]),
                         (plain["passRates"]["level"], plain["kinds"], plain["action"]),
                         "a number said beside the others: the level, the reasons and what is offered as without it")
        self.assertIn("  pool, for reference: 4 of the last 10 passes in the shape of a coin flip, 3 of them with a stop at the "
                      "day's line", r.summary(got))
        self.assertEqual(r.card(got, ch.d)["evidence"]["passRates"]["coinFlipPasses"]["hits"], 4)
        # Only the window of the share of passes counts, the last 100 exams ended: of 130 that passed, four in this
        # shape ended first and are outside it, one ended last and is in it.
        ch = PassRates.world(PassRates(), 130, 130)
        exams = [addr(0x2000 + 0x1000 + i) for i in range(130)]
        for ex in exams[:4]:
            ch.d["exam_events"][ex] = self.events(self.PLAYER)
        ch.d["exam_events"][exams[5]] = self.events(self.PLAYER, from_day=30)
        ch.d["dailyLossBps"] = {ex: 300 for ex in exams}
        cf = r.review(ch.d, POOL, 0)["passRates"]["coinFlipPasses"]
        self.assertEqual((cf["hits"], cf["passes"]), (1, 100))
        self.assertEqual(r.review(PassRates.world(PassRates(), 40, 0).d, POOL, 0)["passRates"]["coinFlipPasses"],
                         {"hits": 0, "withStops": 0, "passes": 0, "share": None})

    def test_the_days_limit_is_read_of_every_account_that_passed(self):
        evs = self.events(self.PLAYER)
        self.assertEqual(r.passed_accounts({addr(1): evs, addr(2): evs[:-1], addr(3): []}), [addr(1)])
        got = {}
        with mock.patch.object(r.c, "call_view", return_value=((300, 600, 1_000, [3], [1_000]),)):
            r.read_daily(addr(1), got)
        with mock.patch.object(r.c, "call_view", side_effect=Exception("429 Too Many Requests")), mock.patch.object(r.core, "log"):
            r.read_daily(addr(2), got)
        self.assertEqual(got, {addr(1): 300}, "one not read is left out, and its losses are held to no line")

    def test_an_account_that_did_not_pass_has_none(self):
        ch = self.world(self.PLAYER)
        ev = ch.d["exam_events"][addr(0x301)]
        ev[-1] = dict(ev[-1], name="Stopped", status=3)
        self.assertIsNone(r.review(ch.d, POOL, 0)["recount"]["stage"]["flip"])


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


class LateStopsAndTakes(unittest.TestCase):
    """The lateness of stops and takes, a series of its own: blocks from the oracle reaching the price to the close
    (`TriggerFilled`), against everyone else's, by the orders' rule. Each account holds longs of 100 bought at a mark
    of 1 000 000, each closed by a stop at 950 000 that acts from 2 blocks after it is set; the oracle stands at the
    mark until the block it reaches 940 000, and there until the close."""

    MARK, STOP, LOW = 1_000_000, 950_000, 940_000

    @staticmethod
    def e(name, block, index=0, **kw):
        return {"name": name, "block": block, "index": index, "t": 1_000 * block, "tx": tx(70_000 + block), **kw}

    def account(self, ch, account, lates, start=1_000):
        """Stops carried out `late` blocks after the oracle reached them, one round trip each, 200 blocks apart."""
        evs = [self.e("Started", start - 1, capital=100 * 10**6)]
        for k, late in enumerate(lates):
            b = start + 200 * k
            reach, close = b + 50, b + 50 + late
            t = tx(90_000_000 + int(account, 16) % 10**6 * 1_000 + k)
            evs += [self.e("Filled", b, asset=3, buy=True, sz=100, px=self.MARK, cost=0, late=1, source=0),
                    self.e("TriggerSet", b + 1, asset=3, take=False, px=self.STOP, sz=0, armedAt=b + 3),
                    {**self.e("TriggerCleared", close, 0, asset=3, take=False), "tx": t},
                    {**self.e("Filled", close, 1, asset=3, buy=False, sz=100, px=self.LOW, cost=0, late=close - b - 3, source=0), "tx": t},
                    {**self.e("TriggerFilled", close, 2, asset=3, take=False, sz=100, px=self.LOW, cost=0,
                              trigger_px=self.STOP, source=0), "tx": t}]
            for x in range(b, close + 1):
                ch.d["oracle"][r.oracle_key(3, x)] = self.LOW if x >= reach else self.MARK
        ch.d.setdefault("exam_events", {})[account] = evs

    def world(self, mine, theirs):
        ch, t1, s1 = honest_world()
        ch.d["oracle"] = {}
        self.account(ch, s1, mine)
        for i, lates in enumerate(theirs):
            self.account(ch, addr(0x210 + i), lates, start=50_000 + 10_000 * i)
        return ch, s1

    def test_a_trader_whose_stops_are_carried_out_later_than_everyone_elses_is_suspicious(self):
        ch, _ = self.world([20] * 6, [[1]] * 8)
        got = r.review(ch.d, POOL, 0)
        tl = got["triggerLate"]
        self.assertEqual(tl["trader"], {"n": 6, "median": 20, "max": 20, "atLeast": 0, "unread": 0})
        self.assertEqual(tl["others"], {"n": 8, "median": 1, "p90": 1, "atLeast": 0, "unread": 0})
        self.assertTrue(tl["outlier"])
        self.assertIn("stops and takes carried out later than everyone else's after the oracle reached them", got["reasons"])
        self.assertEqual(got["kinds"].count("late"), 1)
        self.assertFalse(got["late"]["outlier"], "the orders' lateness is its own series, and in order here")
        self.assertNotIn("fills later than everyone else's", got["reasons"])
        self.assertEqual(r.card(got, ch.d)["evidence"]["triggerLate"], tl)
        self.assertIn("late stops and takes, blocks after the oracle reached them: median 20 of 6; others median 1, p90 1 of 8",
                      r.summary(got))

    def test_late_orders_with_stops_in_order_are_the_orders_finding_only(self):
        ch, s1 = self.world([1] * 6, [[1]] * 8)
        for i in range(6):
            ch.fill(s1, buy=bool(i % 2), t=100_000 + i * 10_000, asset=9, late=20)
        for e in [addr(0x210 + i) for i in range(8)]:
            ch.fill(e, buy=True, t=500_000, asset=8, late=1)
        got = r.review(ch.d, POOL, 0)
        self.assertTrue(got["late"]["outlier"])
        self.assertFalse(got["triggerLate"]["outlier"])
        self.assertEqual((got["triggerLate"]["trader"]["median"], got["triggerLate"]["others"]["p90"]), (1, 1))
        self.assertIn("fills later than everyone else's", got["reasons"])
        self.assertNotIn("stops and takes carried out later than everyone else's after the oracle reached them", got["reasons"])

    def test_too_few_stops_on_either_side_say_nothing(self):
        for mine, theirs in [([20] * 4, [[1]] * 8), ([20] * 6, [[1]] * 4), ([], [[1]] * 8)]:
            ch, _ = self.world(mine, theirs)
            got = r.review(ch.d, POOL, 0)
            self.assertEqual((got["triggerLate"]["trader"]["n"], got["triggerLate"]["others"]["n"]), (len(mine), len(theirs)))
            self.assertFalse(got["triggerLate"]["outlier"], (mine, theirs))
            self.assertNotIn("late", got["kinds"])
        # A stop whose prices were not read is said as not read, and counts on neither side.
        ch, s1 = self.world([20] * 6, [[1]] * 8)
        for k in range(2):
            del ch.d["oracle"][r.oracle_key(3, 1_000 + 200 * k + 60)]
        got = r.review(ch.d, POOL, 0)["triggerLate"]
        self.assertEqual((got["trader"]["n"], got["trader"]["unread"], got["outlier"]), (4, 2, False))
        # No events read at all: nothing to say.
        ch, _ = self.world([20] * 6, [[1]] * 8)
        del ch.d["exam_events"]
        self.assertEqual(r.review(ch.d, POOL, 0)["triggerLate"]["trader"]["n"], 0)

    def test_the_lateness_runs_from_the_stretch_the_oracle_held_the_price_never_before_it_could_act(self):
        prices = {b: self.MARK for b in range(100, 200)}
        close = {"block": 150, "asset": 3, "take": False, "side": 1, "px": self.STOP, "armedAt": 102}
        reached = lambda pr, c=close, back=r.TRIGGER_BACK: r.trigger_reached(c, pr.get, back)
        self.assertEqual(reached({**prices, **{b: self.LOW for b in range(140, 151)}}), (10, False))
        # Reached at 120, back over at 130, reached again at 140: the stretch the close is in counts.
        self.assertEqual(reached({**prices, **{b: self.LOW for b in (*range(120, 130), *range(140, 151))}}), (10, False))
        self.assertEqual(reached({**prices, **{b: self.STOP for b in range(140, 151)}}), (10, False), "at the price is reached")
        # Held since before the stop could act: from `armedAt`.
        self.assertEqual(reached({b: self.LOW for b in range(100, 151)}), (48, False))
        # Held longer than is read back: at least that.
        self.assertEqual(reached({b: self.LOW for b in range(100, 151)}, back=20), (20, True))
        self.assertEqual(reached({b: self.LOW for b in range(100, 151)}, back=48), (48, False), "to `armedAt` exactly is exact")
        # A price on the way not read, or zero, or the close's own block not reached: not read.
        held = {b: self.LOW for b in range(100, 151)}
        self.assertEqual(reached({**held, 145: None}), (None, False))
        self.assertEqual(reached({**held, 145: 0}), (None, False))
        self.assertEqual(reached({**held, 150: self.MARK}), (None, False))
        # A stop on a short at the price or above; a take on a long at it or above; a take on a short at it or below.
        up = {**prices, **{b: 1_060_000 for b in range(147, 151)}}
        self.assertEqual(reached(up, {**close, "side": -1, "px": 1_050_000}), (3, False))
        self.assertEqual(reached(up, {**close, "take": True, "px": 1_050_000}), (3, False))
        self.assertEqual(reached({**prices, **{b: self.LOW for b in range(149, 151)}}, {**close, "take": True, "side": -1}), (1, False))

    def test_a_close_is_its_trigger_that_stood_on_the_side_it_closed(self):
        ch, s1 = self.world([5, 7], [])
        evs = ch.d["exam_events"][s1]
        self.assertEqual(r.trigger_closes(evs), [
            {"block": 1_055, "asset": 3, "take": False, "side": 1, "px": self.STOP, "armedAt": 1_003, "tx": evs[5]["tx"]},
            {"block": 1_257, "asset": 3, "take": False, "side": 1, "px": self.STOP, "armedAt": 1_203, "tx": evs[10]["tx"]}])
        self.assertEqual(r.trigger_lates(evs, ch.d["oracle"]), {"lates": [5, 7], "atLeast": 0, "unread": 0})
        short = [{**x, "buy": not x["buy"]} if x["name"] == "Filled" else x for x in evs]
        self.assertEqual([c["side"] for c in r.trigger_closes(short)], [-1, -1], "the close of a short buys")
        # No trigger standing (never set, or gone with the step), or no fill of its own: not a close.
        self.assertEqual(r.trigger_closes([x for x in evs if x["name"] != "TriggerSet"]), [])
        stepped = evs[:3] + [self.e("StepPassed", 1_002, equity=0)] + evs[3:6]
        self.assertEqual(r.trigger_closes(stepped), [])
        self.assertEqual(r.trigger_closes([x for x in evs if x["name"] != "Filled" or x["late"] == 1]), [])

    def test_the_oracle_is_read_back_from_each_close_while_it_held_the_price(self):
        ch, s1 = self.world([5], [[61]])
        prices = dict(ch.d["oracle"])
        asked = []

        def oracle_at(asset, block):
            asked.append(block)
            return prices.get(r.oracle_key(asset, block))

        oracle = {r.oracle_key(3, 1_055): self.MARK}  # what a --prices file gave stands
        with mock.patch.object(r, "oracle_at", side_effect=oracle_at):
            r.read_trigger_oracle(ch.d["exam_events"], oracle)
        theirs = 50_000 + 50 + 61
        self.assertEqual(oracle[r.oracle_key(3, 1_055)], self.MARK)
        self.assertNotIn(1_055, asked)
        self.assertEqual([b for b in asked if b < 2_000], [], "the stop not reached in its block: nothing more is read")
        self.assertEqual([b for b in asked if b > 2_000], list(range(theirs, theirs - r.TRIGGER_BACK - 1, -1)),
                         "the close's block and 60 blocks back, no further")
        self.assertEqual(r.trigger_lates(ch.d["exam_events"][addr(0x210)], oracle), {"lates": [60], "atLeast": 1, "unread": 0})
        self.assertEqual(r.TRIGGER_BACK, 60)
        self.assertIn("uint16 public constant MAX_FILL_WINDOW = 60;", _source("src/sim/SimPoolFactory.sol"))

    def test_a_price_the_node_did_not_give_is_asked_once_and_never_kept_as_a_price(self):
        ch, _ = self.world([], [[5], [5]])
        both = {addr(0x210): ch.d["exam_events"][addr(0x210)]}
        both[addr(0x211)] = [{**x, "tx": tx(int(x["tx"], 16) + 1)} for x in both[addr(0x210)]]  # the same blocks, another account
        missing = r.oracle_key(3, 50_000 + 50 + 2)
        prices = {k: v for k, v in ch.d["oracle"].items() if k != missing}
        asked = []

        def oracle_at(asset, block):
            asked.append(block)
            return prices.get(r.oracle_key(asset, block))

        oracle = {}
        with mock.patch.object(r, "oracle_at", side_effect=oracle_at):
            r.read_trigger_oracle(both, oracle)
        self.assertEqual(asked.count(50_052), 1, "the second stop does not ask the node again")
        self.assertEqual(asked, [50_055, 50_054, 50_053, 50_052])
        self.assertNotIn(missing, oracle)
        self.assertEqual(r.trigger_lates(both[addr(0x211)], oracle), {"lates": [], "atLeast": 0, "unread": 1})


class MissedTouches(unittest.TestCase):
    """The stops and takes of the payout's own accounts that the oracle reached while they stood and nobody carried out
    then. The stage holds a long of 100 bought at a mark of 1 000 000 in block 1 000, with a stop at 950 000 set in block
    1 001 that acts from 1 003; the oracle stands at the mark but where a test says it was lower."""

    MARK, STOP, LOW = 1_000_000, 950_000, 940_000

    @staticmethod
    def e(name, block, index=0, **kw):
        return {"name": name, "block": block, "index": index, "t": 1_000 * block, "tx": tx(80_000 + block), **kw}

    def events(self, end="carried out", at=1_305, buy=True):
        evs = [self.e("Started", 999, capital=100 * 10**6),
               self.e("Filled", 1_000, asset=3, buy=buy, sz=100, px=self.MARK, cost=0, late=1, source=0),
               self.e("TriggerSet", 1_001, asset=3, take=False, px=self.STOP if buy else 1_050_000, sz=0, armedAt=1_003)]
        t = tx(99_000)
        if end == "carried out":
            evs += [{**self.e("TriggerCleared", at, 0, asset=3, take=False), "tx": t},
                    {**self.e("Filled", at, 1, asset=3, buy=not buy, sz=100, px=self.LOW, cost=0, late=at - 1_003, source=0), "tx": t},
                    {**self.e("TriggerFilled", at, 2, asset=3, take=False, sz=100, px=self.LOW, cost=0,
                              trigger_px=self.STOP, source=0), "tx": t}]
        elif end == "taken off":
            evs.append({**self.e("TriggerCleared", at, asset=3, take=False), "tx": t})
        elif end == "closed":
            evs += [{**self.e("TriggerCleared", at, 0, asset=3, take=False), "tx": t},
                    {**self.e("Filled", at, 1, asset=3, buy=not buy, sz=100, px=self.MARK, cost=0, late=1, source=0), "tx": t}]
        elif end == "replaced":
            evs.append(self.e("TriggerSet", at, asset=3, take=False, px=900_000, sz=0, armedAt=at + 2))
        elif end == "ended":
            evs.append(self.e("Stopped", at, status=3, reason=1, equity=0))
        return evs

    def prices(self, low=(), until=1_400):
        return {b: self.LOW if b in low else self.MARK for b in range(990, until)}

    def missed(self, evs, prices, every=1, latest=2_000):
        return r.missed_touches(evs, {3: prices}, latest, every)

    def world(self, low):
        ch, t1, s1 = honest_world()
        ch.d["latest"] = 2_000
        ch.d["exam_events"] = {s1: self.events(), addr(0x201): [self.e("Started", 500, capital=100 * 10**6)]}
        ch.d["oracle"] = {r.oracle_key(3, b): px for b, px in self.prices(low).items()}
        ch.d["touchEvery"] = {s1: 60}
        return ch, s1

    def test_a_stop_the_oracle_reached_and_left_before_it_was_carried_out_is_shown_and_counted_against_nobody(self):
        ch, s1 = self.world({*range(1_100, 1_131), *range(1_300, 1_306)})
        got = r.review(ch.d, POOL, 0)
        m = got["missedTouch"]
        self.assertEqual(m["stage"], {"read": True, "every": 60, "spans": 1, "unread": 0, "missed": [
            {"asset": 3, "take": False, "px": self.STOP, "side": 1, "armedAt": 1_003, "first": 1_100, "last": 1_130,
             "reads": 31, "seen": False, "then": "carried out", "at": 1_305}]})
        self.assertEqual(m["exam"], {"read": False, "every": None, "spans": 0, "unread": 0, "missed": []},
                         "an account the review did not read along is said as not read")
        self.assertEqual((got["verdict"], got["reasons"]), ("clean", []), "a fact shown, in no reason")
        self.assertEqual(r.card(got, ch.d)["evidence"]["missedTouch"], m)
        text = r.summary(got)
        self.assertIn("  stops and takes the oracle reached and nobody carried out, stage: 1 stretches missed over 1 "
                      "stops and takes that stood, read one block in 60\n    the stop at 950000 on asset 3 (long): the oracle at it or past it from block 1100, "
                      "31 reads to block 1130; then carried out in block 1305", text)
        self.assertIn("  stops and takes the oracle reached and nobody carried out, exam: not read", text)
        # Reached only in the stretch it was carried out in, or held from `armedAt` to the close: nothing missed.
        for low in (range(1_300, 1_306), range(990, 1_306)):
            ch, _ = self.world(set(low))
            self.assertEqual(r.review(ch.d, POOL, 0)["missedTouch"]["stage"]["missed"], [], low)

    def test_how_a_stops_time_ended(self):
        def ends(evs, latest=2_000):
            return [(s["then"], s["end"], s["side"], s["armedAt"]) for s in r.trigger_spans(evs, latest)]
        self.assertEqual(ends(self.events()), [("carried out", 1_305, 1, 1_003)])
        self.assertEqual(ends(self.events("taken off", 1_200)), [("taken off", 1_200, 1, 1_003)])
        self.assertEqual(ends(self.events("closed", 1_200)), [("closed", 1_200, 1, 1_003)])
        self.assertEqual(ends(self.events("replaced", 1_200)), [("replaced", 1_200, 1, 1_003), ("standing", 2_000, 1, 1_202)])
        self.assertEqual(ends(self.events("ended", 1_200)), [("ended", 1_200, 1, 1_003)])
        self.assertEqual(ends(self.events("standing")), [("standing", 2_000, 1, 1_003)])
        self.assertEqual(ends(self.events("taken off", 1_200, buy=False)), [("taken off", 1_200, -1, 1_003)], "on a short")
        stepped = self.events("standing")[:3] + [self.e("StepPassed", 1_100, equity=0),
                                                 self.e("TriggerSet", 1_101, asset=3, take=True, px=1, sz=0, armedAt=1_103)]
        self.assertEqual(ends(stepped), [("ended", 1_100, 1, 1_003)], "a step starts flat: a trigger set then is on nothing")
        # The take the stop's close closes with it is gone with the position, not carried out.
        evs = self.events()
        evs.insert(3, self.e("TriggerSet", 1_002, asset=3, take=True, px=1_100_000, sz=0, armedAt=1_004))
        evs.insert(5, {**self.e("TriggerCleared", 1_305, 1, asset=3, take=True), "tx": evs[4]["tx"]})
        self.assertEqual(ends(evs), [("carried out", 1_305, 1, 1_003), ("closed", 1_305, 1, 1_004)])

    def test_a_stop_missed_and_then_taken_off_replaced_or_left_is_said_with_how_it_ended(self):
        low = self.prices(set(range(1_100, 1_131)))
        for end in ("taken off", "replaced", "closed", "ended", "standing"):
            got = self.missed(self.events(end, 1_200), low)
            at = 2_000 if end == "standing" else 1_200
            self.assertEqual([(m["first"], m["last"], m["then"], m["at"]) for m in got["missed"]], [(1_100, 1_130, end, at)], end)
        # Reached in the very block it was taken off in: it could still have been carried out there first.
        got = self.missed(self.events("taken off", 1_200), self.prices({1_200}))
        self.assertEqual([(m["first"], m["then"]) for m in got["missed"]], [(1_200, "taken off")])
        # The stop that replaced it stands at 900 000, which the oracle never reached.
        self.assertEqual(len(self.missed(self.events("replaced", 1_200), low)["missed"]), 1)
        # A stop on a short is reached at its price or above; a take on a long at it or above.
        up = {b: 1_060_000 if 1_100 <= b <= 1_110 else self.MARK for b in range(990, 2_001)}
        self.assertEqual([m["side"] for m in self.missed(self.events("standing", buy=False), up)["missed"]], [-1])
        take = [x if x["name"] != "TriggerSet" else {**x, "take": True, "px": 1_050_000} for x in self.events("standing")]
        self.assertEqual([(m["take"], m["first"], m["last"]) for m in self.missed(take, up)["missed"]], [(True, 1_100, 1_110)])
        self.assertEqual(self.missed(take, low)["missed"], [], "a take on a long is not reached by a fall")

    def test_before_it_could_act_not_read_and_a_zero_price_are_no_touch(self):
        self.assertEqual(self.missed(self.events(), self.prices({1_001, 1_002, *range(1_300, 1_306)}))["missed"], [],
                         "before `armedAt` no trigger acts")
        gone = {b: px for b, px in self.prices(set(range(1_100, 1_131))).items() if not 1_090 <= b <= 1_140}
        got = self.missed(self.events(), gone)
        self.assertEqual((got["missed"], got["unread"]), ([], 51))
        zero = {r.oracle_key(3, b): (0 if 1_100 <= b <= 1_130 else self.MARK) for b in range(990, 1_400)}
        got = r.missed_touches(self.events(), r.oracle_by_asset(zero), 2_000, 1)
        self.assertEqual((got["missed"], got["unread"]), ([], 31), "a zero price is no price")
        self.assertEqual(r.missed_touches(self.events(), {}, 2_000, None)["read"], False)
        self.assertEqual(r.missed_touches(self.events("standing"), {}, 2_000, 60)["unread"], len(range(1_003, 2_001, 60)) + 1)

    def test_a_sighting_on_chain_is_a_touch_and_two_reads_at_the_price_are_one_stretch(self):
        evs = self.events()
        evs.insert(3, self.e("TriggerSeen", 1_150, asset=3, take=False, oracle=self.LOW, seenAt=1_150))
        got = self.missed(evs, {1_003: self.MARK, 1_200: self.MARK, 1_305: self.LOW}, every=60)
        self.assertEqual([(m["first"], m["last"], m["reads"], m["seen"]) for m in got["missed"]], [(1_150, 1_150, 1, True)])
        # Nothing read off the price between the sighting and the close: what fell between is not known, and the two
        # are taken as the close's own stretch.
        self.assertEqual(self.missed(evs, {1_003: self.MARK, 1_305: self.LOW}, every=60)["missed"], [])
        self.assertEqual(self.missed(evs, {1_003: self.MARK, 1_100: self.MARK, 1_305: self.LOW}, every=60)["missed"], [])
        # A stretch with a read off the price after it, up to the close, is not the close's own, even as the last one.
        got = self.missed(self.events(), {**self.prices(set(range(1_100, 1_131))), 1_305: self.MARK})
        self.assertEqual([(m["first"], m["last"]) for m in got["missed"]], [(1_100, 1_130)])

    def test_the_oracle_is_read_along_each_stop_one_block_in_sixty_at_the_least(self):
        dense = {r.oracle_key(3, b): px for b, px in self.prices({*range(1_100, 1_131), *range(1_300, 1_306)}).items()}
        missing = r.oracle_key(3, 1_063)
        asked = []

        def oracle_at(asset, block):
            asked.append(block)
            return None if r.oracle_key(asset, block) == missing else dense.get(r.oracle_key(asset, block))

        s1, s2 = addr(0x301), addr(0x302)
        events = {s1: self.events(), s2: [{**x, "tx": tx(int(x["tx"], 16) + 7)} for x in self.events()], addr(0x303): self.events()}
        oracle = {r.oracle_key(3, 1_003): self.MARK}  # what was read already, or a --prices file gave, stands
        before = dict(oracle)
        with mock.patch.object(r, "oracle_at", side_effect=oracle_at):
            every, touch = r.read_touch_oracle([s1, s2], events, oracle, 2_000)
        self.assertEqual(every, {s1: 60, s2: 60})
        self.assertEqual(asked, [1_063, 1_123, 1_183, 1_243, 1_303, 1_305],
                         "from `armedAt` one in sixty and the last block, each asked once; the price not given is not asked again")
        self.assertEqual(oracle, before, "the review's other prices are not changed")
        self.assertEqual(sorted(touch), [r.oracle_key(3, b) for b in (1_123, 1_183, 1_243, 1_303, 1_305)])
        got = r.missed_touches(events[s1], r.oracle_by_asset({**oracle, **touch}), 2_000, every[s1])
        self.assertEqual(([(m["first"], m["reads"]) for m in got["missed"]], got["unread"]), ([(1_123, 1)], 1))
        # A price the review's reads before did not get is not asked again either.
        asked.clear()
        with mock.patch.object(r, "oracle_at", side_effect=oracle_at):
            r.read_touch_oracle([s1], events, {}, 2_000, unread={r.oracle_key(3, 1_003), missing})
        self.assertEqual(asked, [1_123, 1_183, 1_243, 1_303, 1_305])
        asked.clear()
        with mock.patch.object(r, "oracle_at", side_effect=oracle_at):
            r.read_trigger_oracle({s1: events[s1]}, {}, {r.oracle_key(3, 1_305)})
        self.assertEqual(asked, [], "nor by the read back from a close")
        # More blocks than the reads allow at sixty: the step grows to fit them, each span's last block counted.
        asked.clear()
        with mock.patch.object(r, "oracle_at", side_effect=oracle_at):
            self.assertEqual(r.read_touch_oracle([s1], events, {}, 2_000, reads=2)[0], {s1: 302})
        self.assertEqual(asked, [1_003, 1_305])
        self.assertEqual(r.touch_every([{"armedAt": 0, "end": 600}], 5), 150)
        self.assertEqual(r.touch_points({"armedAt": 0, "end": 600}, 150), [0, 150, 300, 450, 600])
        self.assertEqual(r.touch_every([], 1), 60)
        self.assertEqual((r.TOUCH_EVERY, r.TOUCH_READS), (60, 3000))
        self.assertIn("uint16 public constant FALLBACK_BLOCKS = 60;", _source("src/exam/PaperExam.sol"))

    def test_the_reads_stay_within_their_number_however_many_stops_were_set(self):
        """Review of #330: each span's first and last block count in the reads, and past the reads nothing is asked."""
        def spans(n, length):
            return [{"armedAt": 1_000 * k, "end": 1_000 * k + length} for k in range(n)]
        for n, length, reads in ((10, 60, 25), (10, 600, 25), (100, 60, 50), (3, 0, 2), (1, 2_592_000, 3_000)):
            every = r.touch_every(spans(n, length), reads)
            got = r.touch_count(spans(n, length), every)
            self.assertEqual(got, sum(len(r.touch_points(x, every)) for x in spans(n, length)), (n, length))
            if 2 * n <= reads or (length == 0 and n <= reads):
                self.assertLessEqual(got, reads, (n, length, reads))
                self.assertTrue(every == r.TOUCH_EVERY or r.touch_count(spans(n, length), every - 1) > reads,
                                "the smallest step that fits")
        self.assertEqual(r.touch_every(spans(100, 60), 50), 61, "the first and last blocks alone are more: one step past the longest")
        # A trader who set a hundred stops gets the reads and no more; the rest is said as not read.
        evs = [self.e("Started", 999, capital=100 * 10**6),
               self.e("Filled", 1_000, asset=3, buy=True, sz=100, px=self.MARK, cost=0, late=1, source=0)]
        for k in range(100):
            evs.append({**self.e("TriggerSet", 2_000 + 100 * k, asset=3, take=False, px=self.STOP, sz=0, armedAt=2_002 + 100 * k),
                        "tx": tx(7_000 + 2 * k)})
            evs.append({**self.e("TriggerCleared", 2_062 + 100 * k, asset=3, take=False), "tx": tx(7_001 + 2 * k)})
        asked = []
        with mock.patch.object(r, "oracle_at", side_effect=lambda a, b: asked.append(b) or self.MARK):
            every, touch = r.read_touch_oracle([addr(0x301)], {addr(0x301): evs}, {}, 30_000, reads=50)
        self.assertEqual((len(asked), every), (50, {addr(0x301): 61}))
        got = r.missed_touches(evs, r.oracle_by_asset(touch), 30_000, every[addr(0x301)])
        self.assertEqual((got["spans"], got["unread"], got["missed"]), (100, 150, []))
        self.assertIn("150 blocks not read", "\n".join(r.missed_lines("stage", got)))

    def test_its_prices_reach_no_other_check(self):
        """Review of #330: the prices read along the stops are kept apart; the lateness of stops (and every other check)
        reads only what it read before."""
        ch, s1 = self.world({*range(1_100, 1_131), *range(1_300, 1_306)})
        ch.d["touchOracle"] = {k: v for k, v in ch.d["oracle"].items() if int(k.split(":")[1]) >= 1_003}
        ch.d["oracle"] = {k: v for k, v in ch.d["oracle"].items() if int(k.split(":")[1]) < 1_003}
        got = r.review(ch.d, POOL, 0)
        self.assertEqual([(m["first"], m["last"]) for m in got["missedTouch"]["stage"]["missed"]], [(1_100, 1_130)])
        self.assertEqual((got["triggerLate"]["trader"]["n"], got["triggerLate"]["trader"]["unread"]), (0, 1))
        self.assertEqual(got["recount"]["stage"]["checked"]["fills"], 1, "the close's price is not the recount's")


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
            self.assertEqual(r.read_chain.call_args.args[3], r.TOUCH_READS)
            r.main(["--deployment", "x", "--touch-reads", "500"])
            self.assertEqual(r.read_chain.call_args.args[3], 500)
        self.assertEqual((got["schema"], got["id"]), ("payout-review/3", 0))


class WhatThePoolWouldTake(unittest.TestCase):
    """Review of #283: no call the pool would refuse."""

    def pair(self, ch, s1):
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        ch.fill(s1, buy=True, t=60_000, asset=5)
        ch.fill(e9, buy=False, t=60_100, asset=5)
        return t9, e9

    def test_a_pool_without_approvals_is_offered_none_and_says_it_pays_by_itself(self):
        ch, _, _ = honest_world()
        ch.d["books"][POOL]["approvals"] = False
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["tx"]["data"], got["tx"]["note"]), ("none", None, r.NO_APPROVAL))
        self.assertIsNone(got["hardAt"], "no ceiling where there is no approval")
        self.assertEqual(got["action"], "none", "nothing to sign: it pays by itself")
        self.assertIn("paid by itself then", r.summary(got))

    def test_whether_the_pool_takes_approvals_has_three_answers(self):
        F, P_ = addr(0xFA), POOL
        def node(answers):
            def call_view(to, sig, types, args, out):
                got = answers[(to, sig)]
                if isinstance(got, Exception):
                    raise got
                return got
            return call_view
        revert, down = Exception("execution reverted"), Exception("429 Too Many Requests")
        cases = [({(F, "PAYOUT_HARD()"): (1,), (P_, "approver()"): (addr(1),)}, True),
                 ({(F, "PAYOUT_HARD()"): revert}, False),
                 ({(F, "PAYOUT_HARD()"): down}, None),
                 ({(F, "PAYOUT_HARD()"): (1,), (P_, "approver()"): down}, None),
                 ({(F, "PAYOUT_HARD()"): (1,), (P_, "approver()"): revert}, None)]
        for answers, want in cases:
            with mock.patch.object(r.c, "call_view", side_effect=node(answers)):
                self.assertIs(r.has_approvals(F, P_), want, answers)

    def test_not_read_whether_the_pool_takes_approvals_offers_nothing_and_says_no_time(self):
        ch, _, s1 = honest_world()
        self.pair(ch, s1)
        ch.d["books"][POOL]["approvals"] = None
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["tx"]["data"], got["hardAt"], got["stoppable"]), ("none", None, None, None))
        self.assertEqual(got["action"], "none", "nothing is offered while it is not read")
        self.assertIn("not read whether the pool takes approvals", got["tx"]["note"])
        self.assertIn("not read: whether the pool takes approvals", got["reasons"])
        self.assertNotIn(r.NO_APPROVAL, got["tx"]["note"], "no word that it pays by itself")
        self.assertIn("not read when it is paid", r.summary(got))
        self.assertNotIn("paid by itself", r.summary(got), "the summary keeps the three answers too")

    def test_no_stop_for_a_payout_the_pool_would_refuse_to_stop(self):
        for state, was, now, why in [("Paid", False, None, "it is paid"), ("Stopped", True, None, "it is stopped"),
                                     ("Due", True, None, "stopped once already"), ("Due", False, "late", "time to stop is over")]:
            ch, _, s1 = honest_world()
            self.pair(ch, s1)
            ch.d["books"][POOL]["approvals"] = False
            pay = ch.d["payouts"][0]
            pay["state"], pay["wasStopped"] = state, was
            if now == "late":
                ch.d["now"] = pay["dueAt"] - 60  # inside the checker's last 5 minutes
            got = r.review(ch.d, POOL, 0)
            self.assertIsNotNone(got["stop"], "the pair is still said")
            self.assertEqual((got["stoppable"], got["tx"]["kind"]), (False, "none"), why)
            self.assertIn(why, got["tx"]["note"])

    def test_a_pool_with_approvals_stops_until_it_is_paid(self):
        ch, _, s1 = honest_world()
        self.pair(ch, s1)
        ch.d["now"] = ch.d["payouts"][0]["dueAt"] + 3_600  # past its 72 hours, not paid
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["stoppable"], got["tx"]["kind"]), (True, "stop"))

    def test_a_payout_approved_already_is_offered_no_second_approval_and_can_still_be_stopped(self):
        ch, _, s1 = honest_world()
        ch.d["payouts"][0]["approved"] = True
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["tx"]["data"], got["tx"]["note"], got["approved"]), ("none", None, r.APPROVED, True))
        self.assertEqual(got["action"], "none", "nothing to sign: it is approved")
        self.assertEqual(got["verdict"], "clean")
        self.assertIn("approved: paid from then", r.summary(got))
        self.pair(ch, s1)
        ch.d["now"] = ch.d["payouts"][0]["dueAt"] + 3_600  # approved, past its 72 hours, not paid yet
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["stoppable"], got["tx"]["kind"]), (True, "stop"), "the pool stops an approved payout until it is paid")

    def test_a_payout_that_is_not_waiting_is_offered_neither_an_approval_nor_a_stop(self):
        """`--id` can name a payout already paid or stopped: the pool reverts an approval of it as it does a stop."""
        for state in ("Paid", "Stopped", "Held", "Refused"):
            ch, _, s1 = honest_world()
            ch.d["payouts"][0]["state"] = state
            got = r.review(ch.d, POOL, 0)
            self.assertEqual((got["tx"]["kind"], got["tx"]["call"], got["tx"]["data"], got["stoppable"]), ("none", None, None, False), state)
            self.assertEqual(got["action"], "none", state)
            self.assertEqual(got["tx"]["note"], f"the payout is {state.lower()}, not waiting: nothing to approve and nothing to stop")
            self.assertIn(f"to sign (none): the payout is {state.lower()}", r.summary(got))
            self.pair(ch, s1)
            got = r.review(ch.d, POOL, 0)
            self.assertEqual((got["tx"]["kind"], got["tx"]["data"]), ("none", None), f"{state}, with a pair: no stop either")
        ch, _ = Recount().world(passed=Recount.C + 999_999)  # a result that does not count up, on a payout already paid
        ch.d["payouts"][0]["state"] = "Paid"
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["action"], got["tx"]["kind"]), ("none", "none"), "nothing left to hold back")
        self.assertEqual(r.card(got, ch.d)["call"]["kind"], "none")
        ch, _, _ = honest_world()
        ch.d["payouts"][0].update(state="Due", wasStopped=True)  # a stop lifted: waiting again, and approvable
        self.assertEqual(r.review(ch.d, POOL, 0)["tx"]["kind"], "approve")

    def test_a_pool_that_names_no_approver_is_offered_no_approval(self):
        ch, _, _ = honest_world()
        ch.d["books"][POOL]["approver"] = r.ZERO_ADDRESS
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["tx"]["data"], got["tx"]["note"]), ("none", None, r.NO_APPROVER))
        self.assertEqual(got["action"], "none", "nothing to sign: nobody approves")
        self.assertEqual(got["hardAt"], got["claimedAt"] + r.PAYOUT_HARD, "the ceiling stands: it goes out then")
        ch.d["books"][POOL]["approver"] = addr(0xAB)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["approver"]), ("approve", addr(0xAB)))

    def test_a_payout_whose_approval_was_not_read_is_offered_nothing(self):
        ch, _, _ = honest_world()
        ch.d["payouts"][0]["approved"] = None
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["tx"]["data"], got["hardAt"]), ("none", None, None))
        self.assertIn("not read whether the payout is approved", got["tx"]["note"])
        self.assertEqual(got["reasons"], ["not read: whether the payout is approved"])
        ch.d["books"][POOL]["approvals"] = False
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["note"], got["reasons"]), (r.NO_APPROVAL, []), "a pool from before approvals has no such field")

    def test_the_payout_is_read_with_its_approval_or_as_a_pool_from_before_approvals_holds_it(self):
        old = (addr(1), addr(2), 800, 5_000, 1, False, 0)

        def view(a, sig, out, types=(), args=()):
            if out == sk.PAYOUT:
                raise Exception("insufficient data for the eighth field")
            self.assertEqual(out, r.PAYOUT_BEFORE_APPROVALS)
            return old

        with mock.patch.object(r.core, "view", side_effect=view):
            self.assertEqual(r.payout_struct(POOL, 0), old + (None,))
        with mock.patch.object(r.core, "view", return_value=old + (1,)):
            self.assertEqual(r.payout_struct(POOL, 0), old + (True,))

    def test_an_approver_not_read_leaves_the_pool_not_read(self):
        with mock.patch.object(r.c, "call_view", side_effect=Exception("429 Too Many Requests")):
            self.assertEqual(r.with_approver(True, POOL), (None, None))
        with mock.patch.object(r.c, "call_view", return_value=("0x" + "00" * 20,)):
            self.assertEqual(r.with_approver(True, POOL), (True, r.ZERO_ADDRESS), "the zero address is an answer: nobody approves")
        with mock.patch.object(r.c, "call_view", side_effect=AssertionError("not asked")):
            self.assertEqual(r.with_approver(False, POOL), (False, None))
            self.assertEqual(r.with_approver(None, POOL), (None, None))

    def test_a_wide_sender_is_no_link_in_a_stop_either(self):
        ch, t1, s1 = honest_world()
        t9, _ = self.pair(ch, s1)
        ch.send(EXCHANGE, t9)  # the exchange funded every buyer and now trader 9 too
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["stop"]["reason"], got["stop"]["links"]), (1, []), "the review does not count it, nor does the stop")
        funder = addr(0xF1)
        a, b = ch.send(funder, t1), ch.send(funder, t9)
        self.assertEqual(r.review(ch.d, POOL, 0)["stop"]["links"], [a, b], "a sender of the two alone still links them")

    def test_the_pools_own_floor_and_staged_beside_the_stricter_total(self):
        ch, _, _ = honest_world()
        ch.d["books"][POOL].update(staged=0, largestPayout=800 * 10**6)
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual((got["staged"], got["floor"], got["stagesWorst"], got["stagedMatches"]),
                         (0, 3 * 800 * 10**6, 0, True), "no stage running: the floor is three of the largest")
        self.assertEqual(got["worst"], 8 * 800 * 10**6, "the eight running exams, each as if it passed")
        ch.d["books"][POOL]["staged"] = 800 * 10**6
        self.assertFalse(r.review(ch.d, POOL, 0)["running"]["stagedMatches"], "the pool keeps a stage this count does not see")
        ch.stage(addr(0x110), addr(0x210), addr(0x310), status="Active", done=False)  # one of the eight starts its stage
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual((got["stages"], got["stagesWorst"], got["stagedMatches"]), (1, 800 * 10**6, True))
        del ch.d["books"][POOL]["staged"]
        self.assertIsNone(r.review(ch.d, POOL, 0)["running"]["floor"], "an older pool without staged: not guessed")
        # A pool of 16 November answers its own floor (the largest line live or selling): that is what is shown.
        ch.d["books"][POOL].update(staged=0, floor=60_000 * 10**6)
        self.assertEqual(r.review(ch.d, POOL, 0)["running"]["floor"], 60_000 * 10**6)

    def test_an_account_not_read_makes_the_pool_total_incomplete(self):
        ch, _, _ = honest_world()
        ch.d["unread"] = [addr(0x210)]
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual((got["enough"], got["unread"]), (False, [addr(0x210)]))


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
            log(POOL, [r.PAYOUT_WAITS_LIMIT, "0x" + "00" * 31 + "07"], encode(["uint64"], [9_000]), 31, tx(17)),
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
                if block == 22:
                    raise RuntimeError("missing trie node")
                return "0x" + encode(["uint64"], [7 if block <= 26 else 8]).hex()
            q = params[0]
            if q["address"] == USDC:
                return usdc_logs
            if q["topics"] == []:
                return [l for l in exam_logs if l["address"] in q["address"]]
            return pool_logs

        views = {("pools()",): [POOL], ("owed()",): 800, ("held()",): 0, ("reserved()",): 0, ("largestPayout()",): 800,
                 ("staged()",): 1_600,
                 ("payout(uint256)",): (s1, t1, 800, 5_000, 1, False, 0, False)}

        def view(a, sig, out, types=(), args=()):
            return views[(sig,)]

        def call_view(to, sig, types, args, out):
            return {"balanceOf(address)": (9_000,), "result()": (2, t1, 0, 0), "accountOf(address)": (2, 0, True),
                    "menu()": ([(SIZE // 2, 50), (SIZE, 100)],),
                    "terms()": ((capital, [800], 0, 0, 0, 1_000, 1, 10),),
                    "rules()": ((300, 600, 1_000, [3], [1_000]),), "drawdownKind()": (1,),
                    "payoutsPerDay()": (3,), "PAYOUT_DAY()": (86_400,), "payRoom()": (3,), "nextPayAt()": (0,),
                    "approver()": (addr(0xAB),), "PAYOUT_HARD()": (8 * 86_400,), "PAYOUT_DELAY()": (72 * 3_600,)}[sig]

        with mock.patch.object(r.c, "rpc", side_effect=rpc), mock.patch.object(r.core, "view", side_effect=view), \
                mock.patch.object(r.c, "call_view", side_effect=call_view), \
                mock.patch.object(r, "read_trigger_oracle", wraps=r.read_trigger_oracle) as back, \
                mock.patch.object(r, "read_touch_oracle", wraps=r.read_touch_oracle) as along:
            got = r.read_chain({"SimPoolFactory": factory, "usdc": USDC, "block": 10}, prices={"9:9": 1}, touch_reads=500)
        back.assert_called_once()
        along.assert_called_once()
        self.assertEqual(along.call_args.args[0], [s1, e1], "along the stops of the waiting payout's own accounts only")
        self.assertIs(along.call_args.args[2], got["oracle"])
        self.assertEqual(along.call_args.args[3:5], (40, 500))
        self.assertIs(along.call_args.args[5], back.call_args.args[2], "one set of prices not given for all the reads")
        self.assertIn("3:22", along.call_args.args[5], "a fill's neighbour the node did not give")
        self.assertEqual([b for _, b in asked].count(22), 1)
        self.assertEqual((got["touchEvery"], got["touchOracle"]), ({s1: 60, e1: 60}, {}))
        self.assertIs(back.call_args.args[1], got["oracle"], "the stops' prices go where the review reads them")
        self.assertEqual(list(back.call_args.args[0]), [e1, s1], "every account of the factory, not only the payouts'")
        self.assertEqual((got["now"], got["latest"], got["pools"]), (1_000, 40, [POOL]))
        self.assertEqual(got["sales"], [{"pool": POOL, "exam": e1, "trader": t1, "partner": partner, "line": 2,
                                         "size": SIZE, "price": 1_000, "fee": 0, "code": "0x" + code.hex(), "block": 11, "tx": tx(1)}])
        self.assertEqual(got["funded"], [{"pool": POOL, "stage": s1, "exam": e1, "trader": t1, "block": 20}])
        self.assertEqual(got["payouts"], [{"pool": POOL, "id": 7, "account": s1, "trader": t1, "amount": 800,
                                           "dueAt": 5_000, "block": 30, "state": "Due", "wasStopped": False,
                                           "approved": False, "trail": r.payout_trail(["PayoutDue"])}])
        self.assertEqual(got["fills"], [{"account": s1, "asset": 3, "buy": True, "sz": 100, "t": 2_500, "tx": tx(4), "late": 4, "via": None},
                                        {"account": s1, "asset": 3, "buy": False, "sz": 100, "t": 2_800, "tx": tx(15), "late": 4, "via": None}])
        self.assertEqual([e["name"] for e in got["exam_events"][s1]], ["Started", "Placed", "Filled", "Placed", "Filled", "Passed"])
        self.assertEqual(got["ended"], {e1: 1_900, s1: 2_900})
        self.assertEqual(got["costPpm"], {s1: 1_000, e1: 1_000})
        self.assertEqual(sorted({b for _, b in asked}), list(range(21, 34)),
                         "each fill's and order's block, their neighbours, and every block of each order's window of 10")
        self.assertEqual(got["fillWindow"], {s1: 10, e1: 10})
        self.assertEqual(got["dailyLossBps"], {s1: 300, e1: 300}, "the day's allowed loss, the first of each account's rules")
        self.assertEqual(got["payCap"], {POOL: {"limit": 3, "day": 86_400, "room": 3, "nextAt": 0}},
                         "the limit is what the factory answers (here 3), not a number of this file's")
        self.assertEqual((got["settled"], got["waitsLimit"]), ({POOL: []}, {(POOL, 7): {"nextAt": 9_000, "time": 3_100}}))
        self.assertEqual((got["drawdownKind"], got["maxDrawdownBps"]), ({s1: 1, e1: 1}, {s1: 600, e1: 600}),
                         "how the line is drawn and the drawdown allowed: the second of each account's rules")
        self.assertEqual({a for a, _ in asked}, {3})
        self.assertEqual((got["oracle"]["3:25"], got["oracle"]["3:28"], got["oracle"]["9:9"]), (7, 8, 1), "the file stands in too")
        self.assertEqual(got["transfers"], [{"from": EXCHANGE, "to": t1, "tx": tx(5), "block": 5}])
        self.assertEqual(got["books"], {POOL: {"balance": 9_000, "owed": 800, "held": 0, "reserved": 0, "sizes": [SIZE // 2, SIZE],
                                               "approvals": True, "largestPayout": 800, "staged": 1_600,
                                               "floor": None, "sharesDue": None, "approver": addr(0xAB)}})
        self.assertEqual((got["status"][s1], got["done"][s1], got["unread"]), ("Passed", True, []))
        self.assertEqual(got["times"], {"delay": 72 * 3_600, "hard": 8 * 86_400}, "what the factory answers, not 7 days by rote")
        review = r.review(got, POOL, 7)
        self.assertEqual(review["hardAt"], 5_000 - 72 * 3_600 + 8 * 86_400)
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

    def test_the_accounts_rules_are_read_in_the_order_the_contract_holds_them(self):
        types = (ROOT / "src/Types.sol").read_text()
        fields = re.search(r"struct Rules \{(.*?)\n\}", types, re.S).group(1)
        self.assertEqual(re.findall(r"^\s*(\w+(?:\[\])?) (\w+);", fields, re.M),
                         [("uint16", "dailyLossBps"), ("uint16", "maxDrawdownBps"), ("uint32", "maxLeverageX100"),
                          ("uint32[]", "assets"), ("uint32[]", "assetLeverageX100")])
        self.assertEqual(r.EXAM_RULES, "(uint16,uint16,uint32,uint32[],uint32[])")
        self.assertIn("function rules() external view returns (Rules memory)", (ROOT / "src/exam/PaperExam.sol").read_text())

    POOL_SRC = (ROOT / "src/sim/SimPool.sol").read_text()
    FACTORY_SRC = (ROOT / "src/sim/SimPoolFactory.sol").read_text()

    @staticmethod
    def function(src: str, name: str) -> tuple[str, str]:
        """A Solidity function's head (to its opening brace) and its body, braces matched."""
        i = src.index(f"function {name}(")
        j = src.index("{", i)
        depth = 0
        for k in range(j, len(src)):
            depth += (src[k] == "{") - (src[k] == "}")
            if depth == 0:
                return src[i:j], src[j:k + 1]
        raise AssertionError(f"{name}: no end")

    def test_the_approval_call_is_the_pools_own_by_its_source(self):
        head, body = self.function(self.POOL_SRC, "approvePayouts")
        types = [p.split()[0] for p in re.search(r"approvePayouts\((.*?)\)", head, re.S).group(1).split(",")]
        self.assertEqual((f"approvePayouts({','.join(types)})", types), (r.APPROVE, r.APPROVE_TYPES))
        self.assertIn("external", head)
        selector = "0x" + keccak(text=f"approvePayouts({','.join(types)})")[:4].hex()
        self.assertEqual(r.approve_call(POOL, 3)["data"][:10], selector, "the selector of the source's own signature")
        # Only the approver, never the zero address; only a payout waiting and not approved yet: what
        # `approve_call` holds back (NO_APPROVER, APPROVED).
        self.assertIn("if (msg.sender != approver || msg.sender == address(0)) revert NotApprover();", body)
        self.assertIn("if (p.state != PayoutState.Due || p.approved) continue;", body)
        self.assertRegex(self.POOL_SRC, r"\n    address public approver;", "the getter `has_approvals` and `approver_of` ask")

    def test_the_delay_and_the_ceiling_are_the_factorys_by_its_source(self):
        unit = {"hours": 3_600, "days": 86_400}
        for name, mine in (("PAYOUT_DELAY", r.PAYOUT_DELAY), ("PAYOUT_HARD", r.PAYOUT_HARD)):
            m = re.search(rf"uint32 public constant {name} = (\d+) (hours|days);", self.FACTORY_SRC)
            self.assertIsNotNone(m, f"{name} is a uint32 the factory answers: `payout_times` decodes it so")
            self.assertEqual(int(m.group(1)) * unit[m.group(2)], mine)
        # The ceiling as the pool counts it: the claim (`dueAt` less the delay) plus PAYOUT_HARD; and `pay` pays a
        # payout from `dueAt` once approved, from the ceiling if not.
        self.assertIn("return p.dueAt - factory.PAYOUT_DELAY() + factory.PAYOUT_HARD();", self.POOL_SRC)
        _, pay = self.function(self.POOL_SRC, "pay")
        self.assertIn("uint256 hardAfter = uint256(factory.PAYOUT_HARD()) - factory.PAYOUT_DELAY();", pay)
        self.assertIn("if (block.timestamp < p.dueAt) break;", pay)
        self.assertIn("if (!p.approved && block.timestamp < uint256(p.dueAt) + hardAfter) {", pay)

    def test_the_payout_is_read_field_by_field_as_the_pool_holds_it(self):
        fields = re.findall(r"^\s*(\w+) (\w+);", re.search(r"struct Payout \{(.*?)\n    \}", self.POOL_SRC, re.S).group(1), re.M)
        self.assertEqual([n for _, n in fields],
                         ["account", "trader", "amount", "dueAt", "state", "wasStopped", "stoppedAt", "approved"])
        types = ["uint8" if t == "PayoutState" else t for t, _ in fields]
        self.assertEqual(sk.PAYOUT, "(" + ",".join(types) + ")")
        self.assertEqual(r.PAYOUT_BEFORE_APPROVALS, "(" + ",".join(types[:7]) + ")")
        states = re.sub(r"///[^\n]*", "", re.search(r"enum PayoutState \{(.*?)\}", self.POOL_SRC, re.S).group(1))
        self.assertEqual(tuple(states.replace(",", " ").split()), sk.PAYOUT_STATE)
        self.assertIn("function payout(uint256 id) external view returns (Payout memory)", self.POOL_SRC)

    def test_a_stop_is_taken_until_the_payout_is_paid_by_the_pools_source(self):
        head, body = self.function(self.POOL_SRC, "stopPayout")
        self.assertIn("onlyChecker", head)
        # The pool's checks of a stop run in its library (SimPoolStops, DELEGATECALL), for the size of its code.
        self.assertIn("SimPoolStops.stop(", body)
        body += self.function((ROOT / "src/sim/SimPoolStops.sol").read_text(), "stop")[1]
        self.assertIn("if (p.state != SimPool.PayoutState.Due || p.wasStopped) revert SimPool.BadPayout();", body)
        # No time and no approval in the pool's rule: waiting and never stopped is all it asks, so the review
        # offers a stop on a pool with approvals until the payout is paid (`in_time`).
        for word in ("dueAt", "block.timestamp <", "block.timestamp >", "TooLate", "approved", "PAYOUT_"):
            self.assertNotIn(word, body)

    def test_it_reads_and_never_sends(self):
        src = (ROOT / "ops/payout_review.py").read_text()
        for word in ("c.transact(", "c.account(", "sign_transaction", "eth_sendRawTransaction", "private_key", "--wallet"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()


# ── the trailing drawdown line, stops and takes, and a payout waiting for approval by its own events ──────────

def _source(path: str) -> str:
    return (ROOT / path).read_text()


class TheDrawdownLine(unittest.TestCase):
    """PaperExam's drawdown line as the contract's own tests count it (test/exam/PaperExam.t.sol): a capital of
    100, a drawdown of 10 %."""

    C100 = 100 * 10**6

    def line(self, kind, peak, capital=None, bps=1_000):
        return r.drawdown_line(kind, capital or self.C100, bps, peak * 10**6 if peak < 10**6 else peak)

    def test_the_line_trails_the_highest_snapshot_to_the_starting_balance_and_no_higher(self):
        # test_trailingLine_risesWithTheDaysSnapshots_andStopsAtTheStart: 90, 94, 97, 100, 100.
        self.assertEqual([self.line(r.TRAILING_TO_START, p) for p in (100, 104, 107, 112, 115)],
                         [v * 10**6 for v in (90, 94, 97, 100, 100)])
        self.assertEqual(self.line(r.FROM_START, 104), 90 * 10**6, "the line from the start does not move")
        # Kept exact, and rounded up as the contract rounds it.
        self.assertEqual(r.drawdown_line_bps(r.TRAILING_TO_START, self.C100, 1_000, 104 * 10**6 + 1),
                         (104 * 10**6 + 1) * 10_000 - self.C100 * 1_000)
        self.assertEqual(r.drawdown_line(r.TRAILING_TO_START, self.C100, 1_000, 104 * 10**6 + 1), 94 * 10**6 + 1)
        self.assertEqual(r.drawdown_line(r.TRAILING_TO_START, 10_001, 3_333, 10_001), 6_668)
        with self.assertRaises(ValueError):
            r.drawdown_line(2, self.C100, 1_000, self.C100)

    def test_it_is_the_contracts_by_its_source(self):
        src = _source("src/exam/PaperExam.sol")
        self.assertIn("if (drawdownKind == FROM_START) return base * (bps - dd);", src)
        self.assertIn("int256 trailing = peakSnapshot * bps - base * dd;", src)
        self.assertIn("return trailing < base * bps ? trailing : base * bps;", src)
        self.assertIn("return (_drawdownLineBps() + bps - 1) / bps;", src)
        self.assertIn("if (eq * bps < _drawdownLineBps()) return Breach.Drawdown;", src)
        self.assertIn("if (eq > peakSnapshot) peakSnapshot = eq;", src)
        self.assertRegex(src, r"uint8 public constant FROM_START = 0;\s+uint8 public constant TRAILING_TO_START = 1;")
        self.assertEqual((r.FROM_START, r.TRAILING_TO_START), (0, 1))
        breach = re.search(r"enum Breach \{(.*?)\}", _source("src/Types.sol"), re.S).group(1)
        self.assertEqual(re.sub(r"///[^\n]*", "", breach).replace(",", " ").split()[r.BREACH_DRAWDOWN], "Drawdown")
        # The events the recount reads, held to the source.
        for name, sig in (("TRIGGER_SEEN", "TriggerSeen(uint32,bool,uint64,uint256)"),):
            self.assertEqual(getattr(r, name), "0x" + keccak(text=sig).hex())
        for sig in ("TriggerSet(uint32 indexed asset, bool isTake, uint64 px, uint64 sz, uint256 armedAt)",
                    "TriggerCleared(uint32 indexed asset, bool isTake)",
                    "TriggerSeen(uint32 indexed asset, bool isTake, uint64 oracle, uint256 seenAt)"):
            self.assertIn(f"event {sig};", re.sub(r"\s+", " ", src))
        self.assertIn("event TriggerFilled( uint32 indexed asset, bool isTake, uint64 sz, uint64 px, uint256 cost, "
                      "uint64 triggerPx, uint8 source );", re.sub(r"\s+", " ", src))
        self.assertIn("event Stopped(Status indexed status, Breach indexed reason, int256 equity);", src)


class StopsAndTheLineInTheRecount(unittest.TestCase):
    """The result counted again for an account with stops, takes and a trailing line: a capital of 100, one asset
    at a mark of 1 000 000, a long of 100 bought at the mark (cash 0, equity 100 times the mark's move)."""

    C = 100 * 10**6
    MARK = 1_000_000
    TAKE, STOP = True, False

    def world(self, events, *, kind=r.TRAILING_TO_START, bps=1_000, prices=None):
        ch, t1, s1 = honest_world()
        ch.d["exam_events"] = {s1: events, addr(0x201): [self.e("Started", 0, capital=self.C), self.e("Passed", 0, equity=self.C)]}
        ch.d["oracle"] = {f"3:{b}": v for b, v in (prices or {}).items() if v is not None}
        if kind is not None:
            ch.d["drawdownKind"], ch.d["maxDrawdownBps"] = {s1: kind}, {s1: bps}
        return ch, s1

    @staticmethod
    def e(name, block, index=0, **kw):
        return {"name": name, "block": block, "index": index, "t": 1_000 * block, "tx": tx(7_000 + block), **kw}

    def bought(self):
        """Started, a long of 100 bought at the mark in block 12 (an order's fill), the stop and the take set in 20."""
        return [self.e("Started", 1, capital=self.C), self.e("Placed", 10, asset=3),
                self.e("Filled", 12, asset=3, buy=True, sz=100, px=self.MARK, cost=0, late=0, source=0)]

    def tset(self, block, take, px, sz=0, armed=None, index=1):
        return self.e("TriggerSet", block, index, asset=3, take=take, px=px, sz=sz, armedAt=block + 2 if armed is None else armed)

    def cleared(self, block, take, index=0, tx_of=None):
        return {**self.e("TriggerCleared", block, index, asset=3, take=take), **({"tx": tx_of} if tx_of else {})}

    def closed_by(self, block, take, px, trigger_px, sz=100, buy=False, cost=0, source=0, clears=(), index=2):
        """What `fillTrigger` writes, in its order: the trigger or triggers cleared, `Filled`, `TriggerFilled`."""
        t = tx(8_000 + block)
        out = [{**self.cleared(block, c, index=index + k), "tx": t} for k, c in enumerate(clears)]
        n = index + len(out)
        out.append({**self.e("Filled", block, n, asset=3, buy=buy, sz=sz, px=px, cost=cost, late=0, source=source), "tx": t})
        out.append({**self.e("TriggerFilled", block, n + 1, asset=3, take=take, sz=sz, px=px, cost=cost, trigger_px=trigger_px, source=source), "tx": t})
        return out

    def flags(self, ch, s1):
        return [(f["what"], f["block"]) for f in r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"]]

    def stopped_world(self, **kw):
        """A long of 100 with a stop at 950 000 and a take at 1 100 000, the stop carried out at 940 000 in block 510."""
        evs = self.bought() + [self.tset(500, self.STOP, 950_000), self.tset(500, self.TAKE, 1_100_000, index=2),
                               self.e("TriggerSeen", 505, asset=3, take=self.STOP, oracle=940_000, seenAt=505)]
        evs += self.closed_by(510, self.STOP, 940_000, 950_000, clears=(self.STOP, self.TAKE))
        evs.append(self.e("Passed", 511, equity=94 * 10**6))
        prices = {10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK, 509: 945_000, 510: 940_000, 511: 940_000}
        return self.world(evs, prices=prices, **kw)

    # ── a stop's close is a fill, and it counts ──

    def test_a_stop_carried_out_is_a_fill_that_counts_in_the_cash_and_clean_when_it_is_the_triggers(self):
        ch, s1 = self.stopped_world()
        rc = r.review(ch.d, POOL, 0)["recount"]["stage"]
        self.assertEqual((rc["flags"], rc["fills"], rc["triggers"], rc["checked"]["results"], rc["checked"]["fills"]),
                         ([], 2, {"set": 2, "filled": 1}, 1, 2), "100 sold at 940 000 leaves 94: the result says 94")
        self.assertEqual(rc["moves"], [{"tx": tx(7_012), "bps": 0.0}], "only the order's move; the stop's close was no order")

    def test_a_result_that_ignores_the_stops_close_does_not_count_up(self):
        ch, s1 = self.stopped_world()
        ch.d["exam_events"][s1][-1]["equity"] = 100 * 10**6
        self.assertEqual(self.flags(ch, s1), [("result", 511)])

    # ── what a trigger fill is held to ──

    def test_each_departure_from_the_contract_in_a_trigger_fill_is_a_flag(self):
        def one(change, **kw):
            ch, s1 = self.stopped_world(**kw)
            change(ch.d["exam_events"][s1], ch)
            return self.flags(ch, s1)

        def at(evs, name):
            return next(x for x in evs if x["name"] == name and x["block"] == 510)
        # Before the block it acts from (set in 500, a delay of 2: from 502).
        self.assertIn(("trigger-early", 501), one(lambda evs, ch: [x.update(block=501) for x in evs if x["block"] == 510]))
        self.assertNotIn(("trigger-early", 502), one(lambda evs, ch: [x.update(block=502) for x in evs if x["block"] == 510]),
                         "from its block on, the block it acts from included")
        # The oracle not at it: a stop on a long is reached at or under its price.
        self.assertIn(("trigger-not-reached", 510), one(lambda evs, ch: ch.d["oracle"].update({"3:510": 960_000})))
        # Reached exactly at its price is enough.
        self.assertNotIn(("trigger-not-reached", 510), one(lambda evs, ch: ch.d["oracle"].update({"3:510": 950_000})))
        # Another price for the trigger than the one standing.
        self.assertEqual(one(lambda evs, ch: at(evs, "TriggerFilled").update(trigger_px=951_000)), [("trigger-price", 510)])
        # A size the trigger does not name: it closes all of 100, here 60 were sold.
        def short(evs, ch):
            for x in evs:
                if x["block"] == 510 and x["name"] in ("Filled", "TriggerFilled"):
                    x["sz"] = 60
        self.assertIn(("trigger-size", 510), one(short))
        # The wrong side: a buy where a long is closed.
        self.assertIn(("trigger-size", 510), one(lambda evs, ch: [x.update(buy=True) for x in evs if x["name"] == "Filled" and x["block"] == 510]))
        # No stop standing: it was never set.
        self.assertIn(("trigger-unset", 510), one(lambda evs, ch: evs.__delitem__(slice(3, 5))))
        # A TriggerFilled that does not come right after its own Filled.
        self.assertIn(("trigger-unmatched", 510), one(lambda evs, ch: at(evs, "TriggerFilled").update(px=939_000)))
        self.assertIn(("trigger-unmatched", 510), one(lambda evs, ch: evs.remove(at(evs, "Filled"))))

    def test_a_take_of_a_size_closes_that_size_and_the_stop_stays(self):
        evs = self.bought() + [self.tset(500, self.STOP, 950_000), self.tset(500, self.TAKE, 1_050_000, sz=40, index=2)]
        evs += self.closed_by(510, self.TAKE, 1_050_000, 1_050_000, sz=40, clears=(self.TAKE,))
        ch, s1 = self.world(evs, prices={10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK, 509: 1_050_000, 510: 1_050_000, 511: 1_050_000})
        self.assertEqual(self.flags(ch, s1), [])
        evs.extend(self.closed_by(520, self.STOP, 940_000, 950_000, sz=60, clears=(self.STOP,)))
        ch.d["oracle"].update({"3:519": 940_000, "3:520": 940_000, "3:521": 940_000})
        self.assertEqual(self.flags(ch, s1), [], "the stop closes what is left, 60")
        # A size bigger than what is open closes all of it.
        big = self.bought() + [self.tset(500, self.TAKE, 1_050_000, sz=500)]
        big += self.closed_by(510, self.TAKE, 1_050_000, 1_050_000, sz=100, clears=(self.TAKE,))
        big_ch, big_s1 = self.world(big, prices={10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK, 509: 1_050_000, 510: 1_050_000, 511: 1_050_000})
        self.assertEqual(self.flags(big_ch, big_s1), [])
        # A take at 1 050 000 is reached from below: an oracle under it is not.
        ch.d["oracle"]["3:510"] = 1_049_999
        self.assertIn(("trigger-not-reached", 510), self.flags(ch, s1))

    def test_a_short_is_reached_the_other_way(self):
        evs = [self.e("Started", 1, capital=self.C), self.e("Placed", 10, asset=3),
               self.e("Filled", 12, asset=3, buy=False, sz=100, px=self.MARK, cost=0, late=0, source=0),
               self.tset(500, self.STOP, 1_050_000)]
        evs += self.closed_by(510, self.STOP, 1_060_000, 1_050_000, buy=True, clears=(self.STOP,))
        prices = {10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK, 509: 1_060_000, 510: 1_060_000, 511: 1_060_000}
        ch, s1 = self.world(evs, prices=prices)
        self.assertEqual(self.flags(ch, s1), [], "a stop on a short is reached at or over its price")
        ch.d["oracle"]["3:510"] = 1_040_000
        self.assertIn(("trigger-not-reached", 510), self.flags(ch, s1))

    # ── what is left behind, and what is set on nothing ──

    def test_a_trigger_left_on_a_position_closed_or_turned_round_is_a_flag(self):
        evs = self.bought() + [self.tset(500, self.STOP, 950_000)]
        sell = lambda block, sz, clears: [*[{**self.cleared(block, c, index=0), "tx": tx(9_000 + block)} for c in clears],
                                          {**self.e("Filled", block, 1, asset=3, buy=False, sz=sz, px=self.MARK, cost=0, late=0, source=0),
                                           "tx": tx(9_000 + block)}]
        prices = {10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK, 599: self.MARK, 600: self.MARK, 601: self.MARK}
        for sz, name in ((100, "closed"), (160, "turned round")):
            ch, s1 = self.world(evs + sell(600, sz, ()), prices=prices)
            self.assertEqual(self.flags(ch, s1), [("trigger-kept", 600)], name)
            ch, s1 = self.world(evs + sell(600, sz, (self.STOP,)), prices=prices)
            self.assertEqual(self.flags(ch, s1), [], f"{name}: the stop taken off first, as `_apply` writes it")
        ch, s1 = self.world(evs + sell(600, 40, ()), prices=prices)
        self.assertEqual(self.flags(ch, s1), [], "a smaller position keeps its stop")

    def test_a_stop_set_on_no_position_and_a_sighting_of_none_are_flags(self):
        evs = [self.e("Started", 1, capital=self.C), self.tset(5, self.STOP, 950_000),
               self.e("TriggerSeen", 8, asset=3, take=self.TAKE, oracle=1_100_000, seenAt=8)]
        ch, s1 = self.world(evs)
        self.assertEqual(self.flags(ch, s1), [("trigger-no-position", 5), ("trigger-unset", 8)])

    def test_an_order_waiting_when_a_stop_fires_is_still_the_order_whose_move_is_measured(self):
        evs = self.bought() + [self.tset(500, self.STOP, 950_000), self.e("Placed", 505, asset=3)]
        evs += self.closed_by(510, self.STOP, 940_000, 950_000, clears=(self.STOP,))
        evs.append(self.e("Filled", 515, asset=3, buy=True, sz=10, px=900_000, cost=0, late=1, source=0))
        prices = {10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK, 505: 960_000, 509: 940_000, 510: 940_000, 511: 940_000,
                  514: 900_000, 515: 900_000, 516: 900_000}
        ch, s1 = self.world(evs, prices=prices)
        moves = r.review(ch.d, POOL, 0)["recount"]["stage"]["moves"]
        self.assertEqual([m["tx"] for m in moves], [tx(7_012), tx(7_515)],
                         "the order placed in 505 and filled in 515 has its move; the stop in between took none of it")
        self.assertEqual(moves[1]["bps"], round((960_000 - 900_000) * 10_000 / 960_000, 1))

    # ── the trailing line in the snapshots and in the end ──

    def snapshots(self, equities, kind=r.TRAILING_TO_START):
        """A long of 100 bought at the mark; a snapshot a day with these equities, the mark at 1 000 000 times each / 100."""
        evs = self.bought()
        prices = {10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK}
        for k, eq in enumerate(equities):
            evs.append(self.e("DaySnapshot", 100 + k, asset=0, day=k + 1, equity=eq * 10**6))
            prices[100 + k] = eq * 10**6 // 100
        return self.world(evs, kind=kind, prices=prices)

    def test_a_snapshot_is_held_to_the_line_as_it_stood_before_it(self):
        # test_trailingLine_risesWithTheDaysSnapshots: 104, then 107 over the line 94; the line then 97.
        ch, s1 = self.snapshots([104, 107])
        self.assertEqual(self.flags(ch, s1), [])
        rc = r.review(ch.d, POOL, 0)["recount"]["stage"]
        self.assertEqual(rc["drawdown"], {"kind": "trailing-to-start", "bps": 1_000, "line": 97 * 10**6, "peak": 107 * 10**6, "checked": 2})
        # 104 then 93: under the line 94 that the 104 drew. From the start, 93 is over 90.
        ch, s1 = self.snapshots([104, 93])
        self.assertEqual(self.flags(ch, s1), [("snapshot-under-line", 101)])
        ch, s1 = self.snapshots([104, 93], kind=r.FROM_START)
        self.assertEqual(self.flags(ch, s1), [])
        self.assertEqual(r.review(ch.d, POOL, 0)["recount"]["stage"]["drawdown"]["line"], 90 * 10**6)
        # On the line holds; a unit under breaks.
        ch, s1 = self.snapshots([104])
        ch.d["exam_events"][s1].append(self.e("DaySnapshot", 105, asset=0, day=9, equity=94 * 10**6))
        ch.d["oracle"]["3:105"] = 940_000
        self.assertEqual(self.flags(ch, s1), [])
        ch.d["exam_events"][s1][-1]["equity"] -= 100
        ch.d["oracle"]["3:105"] = 939_999
        self.assertEqual(self.flags(ch, s1), [("snapshot-under-line", 105)])
        # The line does not come down with a smaller snapshot, and a new step starts again from its balance.
        ch, s1 = self.snapshots([107, 101])
        ch.d["exam_events"][s1].append(self.e("DaySnapshot", 110, asset=0, day=3, equity=96 * 10**6))
        ch.d["oracle"]["3:110"] = 960_000
        self.assertEqual(self.flags(ch, s1), [("snapshot-under-line", 110)], "96 is under the 107's 97, though over the 101's")

    def test_a_new_step_starts_the_line_again_from_its_balance(self):
        evs = self.bought()
        evs += [self.e("DaySnapshot", 20, day=1, equity=112 * 10**6)]
        evs += [self.e("Filled", 25, asset=3, buy=False, sz=100, px=1_120_000, cost=0, late=0, source=0),
                self.e("StepPassed", 26, equity=112 * 10**6 + 12 * 10**6),
                self.e("DaySnapshot", 26, 1, day=2, equity=self.C),
                self.e("DaySnapshot", 40, day=3, equity=91 * 10**6)]
        ch, s1 = self.world(evs, prices={10: self.MARK, 11: self.MARK, 12: self.MARK, 13: self.MARK, 20: 1_120_000, 24: 1_120_000,
                                         25: 1_120_000, 26: 1_120_000})
        # The first step's peak was 112 (a line at the start's 100); the second starts from 100, whose line is 90.
        self.assertEqual([w for w in self.flags(ch, s1) if w[0] == "snapshot-under-line"], [], "91 is over the new step's 90")
        self.assertEqual(r.review(ch.d, POOL, 0)["recount"]["stage"]["drawdown"]["peak"], self.C)

    def test_an_end_by_the_drawdown_is_held_to_the_line_at_that_time(self):
        def ended(equity, reason, status=3, kind=r.TRAILING_TO_START):
            ch, s1 = self.snapshots([104], kind=kind)
            ch.d["exam_events"][s1].append(self.e("Stopped", 200, status=status, reason=reason, equity=equity * 10**6 if equity else 0))
            return self.flags(ch, s1)
        self.assertEqual(ended(93, 1), [], "under the line of 94, ended by the drawdown")
        self.assertEqual(ended(93, 2), [("breach-line", 200)], "under the drawdown line, which is judged first: not the day's loss")
        self.assertEqual(ended(95, 1), [("breach-line", 200)], "over the line: no drawdown")
        self.assertEqual(ended(95, 2), [], "over the line, broken by the day's loss")
        self.assertEqual(ended(93, 1, kind=r.FROM_START), [("breach-line", 200)], "from the start the line is 90: 93 is over it")
        self.assertEqual(ended(0, 0, status=4), [], "an expired exam records no equity and no reason")

    def test_without_the_kind_read_nothing_is_said_of_the_line(self):
        ch, s1 = self.snapshots([104, 93], kind=None)
        got = r.review(ch.d, POOL, 0)["recount"]["stage"]
        self.assertEqual((got["flags"], got["drawdown"]), ([], None))
        # Pre-existing chains (no kind kept at all) read as before.
        del ch.d["exam_events"][s1][1:]
        self.assertEqual(r.review(ch.d, POOL, 0)["recount"]["stage"]["flags"], [])

    # ── the fills of a stop are nobody's timing ──

    def test_a_stops_close_is_left_out_of_the_lateness_and_the_timing(self):
        evs = self.bought() + [self.tset(500, self.STOP, 950_000)] + self.closed_by(510, self.STOP, 940_000, 950_000, clears=(self.STOP,))
        ch, s1 = self.world(evs, prices={10: self.MARK, 12: self.MARK, 509: 940_000, 510: 940_000, 511: 940_000})
        ch.d["fills"] = [{"account": s1, "asset": 3, "buy": True, "sz": 100, "t": 12_000, "tx": tx(7_012), "late": 4, "via": None},
                         {"account": s1, "asset": 3, "buy": False, "sz": 100, "t": 510_000, "tx": tx(8_510), "late": 0, "via": "stop"}]
        ch.d["fillWindow"] = {s1: 5}
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["late"]["trader"]["n"], got["late"]["trader"]["median"]), (1, 4), "the stop's 0 is no lateness")
        self.assertEqual(got["timing"]["fills"], 1)
        self.assertEqual(r.trigger_fills(ch.d["exam_events"][s1]), {5: "stop"}, "the Filled the TriggerFilled follows")
        # A Filled with no TriggerFilled right after it in its transaction, or another's numbers, is an order's.
        evs2 = [x for x in ch.d["exam_events"][s1]]
        evs2[-1] = {**evs2[-1], "tx": tx(1)}
        self.assertEqual(r.trigger_fills(evs2), {})

    def test_the_triggers_events_are_read_as_the_contract_writes_them(self):
        def log(topics, data, block=30):
            return {"topics": topics, "data": "0x" + data.hex(), "blockNumber": hex(block), "transactionHash": tx(1), "logIndex": "0x1"}
        asset = "0x" + f"{3:064x}"
        got = r.exam_event_of(log([sk.TRIGGER_SET, asset], encode(["bool", "uint64", "uint64", "uint256"], [True, 1_050_000, 40, 502])), 7)
        self.assertEqual((got["name"], got["asset"], got["take"], got["px"], got["sz"], got["armedAt"]), ("TriggerSet", 3, True, 1_050_000, 40, 502))
        got = r.exam_event_of(log([sk.TRIGGER_CLEARED, asset], encode(["bool"], [False])), 7)
        self.assertEqual((got["name"], got["asset"], got["take"]), ("TriggerCleared", 3, False))
        got = r.exam_event_of(log([r.TRIGGER_SEEN, asset], encode(["bool", "uint64", "uint256"], [False, 940_000, 510])), 7)
        self.assertEqual((got["name"], got["take"], got["oracle"], got["seenAt"]), ("TriggerSeen", False, 940_000, 510))
        got = r.exam_event_of(log([sk.TRIGGER_FILLED, asset], encode(["bool", "uint64", "uint64", "uint256", "uint64", "uint8"],
                                                                      [False, 100, 940_000, 7, 950_000, 1])), 7)
        self.assertEqual((got["name"], got["take"], got["sz"], got["px"], got["cost"], got["trigger_px"], got["source"]),
                         ("TriggerFilled", False, 100, 940_000, 7, 950_000, 1))
        got = r.exam_event_of(log([r.STOPPED, "0x" + f"{3:064x}", "0x" + f"{1:064x}"], encode(["int256"], [93])), 7)
        self.assertEqual((got["name"], got["status"], got["reason"], got["equity"]), ("Stopped", 3, 1, 93))
        got = r.exam_event_of(log([r.STOPPED, "0x" + f"{3:064x}"], encode(["int256"], [93])), 7)
        self.assertEqual((got["status"], got["reason"]), (3, None), "a log with the reason not indexed is not a crash")

    def test_a_trigger_is_carried_out_in_the_contracts_order_and_one_the_oracle_has_reached(self):
        src = _source("src/exam/PaperExam.sol")
        self.assertIn("bool hit = isTake == (pos > 0) ? oracle >= t.px : oracle <= t.px;", src)
        self.assertTrue(r.trigger_hit(False, 100, 950_000, 950_000))
        self.assertTrue(r.trigger_hit(False, 100, 940_000, 950_000))
        self.assertFalse(r.trigger_hit(False, 100, 951_000, 950_000))
        self.assertTrue(r.trigger_hit(True, 100, 1_050_000, 1_050_000))
        self.assertFalse(r.trigger_hit(True, 100, 1_049_999, 1_050_000))
        self.assertTrue(r.trigger_hit(False, -100, 1_050_000, 1_050_000), "a stop on a short is reached at or over")
        self.assertFalse(r.trigger_hit(False, -100, 1_049_999, 1_050_000))
        self.assertTrue(r.trigger_hit(True, -100, 950_000, 950_000), "a take on a short at or under")
        self.assertFalse(r.trigger_hit(True, -100, 950_001, 950_000))
        self.assertIn("uint64 sz = t.sz == 0 || t.sz > open ? uint64(open) : t.sz;", src)
        self.assertIn("emit Filled(asset, before < 0, sz, px, cost, block.number - t.armedAt, source);", src)
        self.assertIn("emit TriggerFilled(asset, isTake, sz, px, cost, t.px, source);", src)
        self.assertIn("if (block.number < t.armedAt) revert TooEarly(t.armedAt);", src)


class AWaitingPayoutByItsEvents(unittest.TestCase):
    """Whether a payout waits for approval is what its own recorded events say, not a guess from the clock."""

    def world(self, trail, *, approved=False, state="Due", now=None):
        ch, t1, s1 = honest_world()
        pay = ch.d["payouts"][0]
        pay["state"], pay["approved"] = state, approved
        if trail is not None:
            pay["trail"] = r.payout_trail(trail)
        if now is not None:
            ch.d["now"] = now
        return ch

    def test_a_payout_waits_while_it_is_due_and_no_approval_was_written(self):
        T = r.payout_trail
        self.assertEqual((T(["PayoutDue"])["waitingApproval"], T(["PayoutDue"])["state"]), (True, "Due"))
        self.assertFalse(T(["PayoutDue", "PayoutApproved"])["waitingApproval"])
        self.assertFalse(T(["PayoutDue", "PayoutPaid"])["waitingApproval"])
        self.assertFalse(T(["PayoutDue", "PayoutStopped"])["waitingApproval"], "stopped: not waiting for an approval")
        # An approval is kept through a stop and its lifting (`liftStop` leaves `approved`), and a stop is lifted once.
        lifted = T(["PayoutDue", "PayoutApproved", "PayoutStopped", "StopLifted"])
        self.assertEqual((lifted["state"], lifted["approved"], lifted["wasStopped"], lifted["waitingApproval"]), ("Due", True, True, False))
        self.assertTrue(T(["PayoutDue", "PayoutStopped", "StopLifted"])["waitingApproval"], "lifted without an approval: waiting again")
        self.assertEqual(T(["PayoutDue", "PayoutStopped", "StopFinal"])["state"], "Refused")
        self.assertEqual(T(["PayoutDue", "PayoutHeld"])["state"], "Held")
        self.assertEqual(T([])["state"], None)

    def test_events_the_contract_could_not_have_written_make_a_trail_that_says_nothing(self):
        T = r.payout_trail
        for names in (["PayoutApproved"], ["PayoutDue", "PayoutApproved", "PayoutApproved"], ["PayoutDue", "PayoutPaid", "PayoutApproved"],
                      ["PayoutDue", "PayoutStopped", "PayoutStopped"], ["PayoutDue", "PayoutStopped", "PayoutApproved"],
                      ["PayoutDue", "StopLifted"], ["PayoutDue", "PayoutDue"], ["PayoutDue", "PayoutStopped", "PayoutPaid"]):
            got = T(names)
            self.assertTrue(got["problems"], names)
            self.assertFalse(got["waitingApproval"], names)

    def test_the_trail_is_the_contracts_by_its_source(self):
        pool = _source("src/sim/SimPool.sol")
        for sig in ("PayoutApproved(uint256 indexed id, address indexed approver)", "PayoutHeld(uint256 indexed id, address indexed trader, uint64 amount)",
                    "StopLifted(uint256 indexed id, uint256 stoppedCount, uint256 liftedCount, uint256 paidCount)",
                    "StopFinal(uint256 indexed id, uint64 amount, uint256 reserved)",
                    "PayoutPaid(uint256 indexed id, address indexed trader, uint64 amount, uint256 paidCount, uint256 paidTotal)"):
            self.assertIn(f"event {sig};", re.sub(r"\s+", " ", pool))
        self.assertIn("event PayoutStopped( uint256 indexed id, address indexed trader, uint64 amount, uint8 reason, address paired, "
                      "bytes32[] trades, bytes32[] links, uint256 stoppedCount, uint256 liftedCount, uint256 paidCount );", re.sub(r"\s+", " ", pool))
        for topic, sig in ((r.PAYOUT_APPROVED, "PayoutApproved(uint256,address)"), (r.PAYOUT_HELD, "PayoutHeld(uint256,address,uint64)"),
                           (r.STOP_LIFTED, "StopLifted(uint256,uint256,uint256,uint256)"), (r.STOP_FINAL, "StopFinal(uint256,uint64,uint256)"),
                           (r.PAYOUT_PAID, "PayoutPaid(uint256,address,uint64,uint256,uint256)")):
            self.assertEqual(topic, "0x" + keccak(text=sig).hex())
        # `liftStop` leaves `approved` alone and `approvePayouts` asks a payout waiting and not approved: the trail's rules.
        # (the pool's `liftStop` runs its checks in the library `SimPoolStops`, by DELEGATECALL)
        _, lift = AgainstTheContracts.function(_source("src/sim/SimPoolStops.sol"), "lift")
        self.assertNotIn("approved", lift)
        self.assertIn("p.state = SimPool.PayoutState.Due;", lift)
        self.assertEqual(sorted(r.PAYOUT_TRAIL.values()), sorted(["PayoutDue", "PayoutApproved", "PayoutStopped", "StopLifted", "StopFinal",
                                                                  "PayoutPaid", "PayoutHeld"]))

    def test_past_its_72_hours_with_no_approval_written_it_waits_for_one(self):
        ch = self.world(["PayoutDue"])
        due = ch.d["payouts"][0]["dueAt"]
        for now in (due - 3_600, due + 3_600, due + 4 * 86_400):
            ch.d["now"] = now
            got = r.review(ch.d, POOL, 0)
            self.assertTrue(got["waitingApproval"], now)
            self.assertEqual(got["trail"], ["PayoutDue"])
            self.assertEqual((got["tx"]["kind"], got["action"]), ("approve", "approve"))
        self.assertIn("waiting for approval", r.summary(got))

    def test_an_approval_written_ends_the_wait_at_any_time(self):
        ch = self.world(["PayoutDue", "PayoutApproved"], approved=True)
        due = ch.d["payouts"][0]["dueAt"]
        for now in (due - 3_600, due + 3_600):
            ch.d["now"] = now
            got = r.review(ch.d, POOL, 0)
            self.assertFalse(got["waitingApproval"], now)
            self.assertEqual((got["tx"]["kind"], got["tx"]["note"]), ("none", r.APPROVED))
        self.assertNotIn("waiting for approval", r.summary(got))

    def test_a_pool_without_approvals_has_none_to_wait_for(self):
        ch = self.world(["PayoutDue"])
        ch.d["books"][POOL]["approvals"] = False
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["waitingApproval"], got["tx"]["note"]), (False, r.NO_APPROVAL))

    def test_the_trail_and_the_pools_storage_must_agree_or_nothing_is_offered(self):
        # Events say approved, the storage says not: a payout approved twice would revert, one not approved is not paid.
        ch = self.world(["PayoutDue", "PayoutApproved"], approved=False)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["waitingApproval"], got["action"]), ("none", False, "none"))
        self.assertIn("recorded events say approved, the pool says the other", got["tx"]["note"])
        self.assertEqual(got["reasons"], ["not read: the payout's recorded events say approved, the pool says the other"])
        ch = self.world(["PayoutDue", "PayoutStopped"], state="Due")
        self.assertIn("recorded events say Stopped, the pool says Due", r.review(ch.d, POOL, 0)["tx"]["note"])
        ch = self.world(["PayoutDue", "PayoutApproved", "PayoutApproved"], approved=True)
        self.assertIn("not a payout's life", r.review(ch.d, POOL, 0)["tx"]["note"])
        # Without a trail (a chain read before it was kept) nothing is compared; the storage stands as before.
        ch = self.world(None)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["trail"], got["waitingApproval"], got["tx"]["kind"]), (None, True, "approve"))
        # Where the storage's answer has no `approved`, the events say it: waiting, so the approval is offered; approved
        # already, so none is (one offered twice would revert). With no trail, or one that is not a payout's life or
        # does not end where the storage says, it stays not read.
        ch = self.world(["PayoutDue"], approved=None)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["approved"], got["waitingApproval"], got["tx"]["kind"]), (False, True, "approve"))
        ch = self.world(["PayoutDue", "PayoutApproved"], approved=None)
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["approved"], got["waitingApproval"], got["tx"]["kind"], got["tx"]["note"]), (True, False, "none", r.APPROVED))
        note = "not read whether the payout is approved: nothing to sign until it is"
        self.assertEqual(r.review(self.world(None, approved=None).d, POOL, 0)["tx"]["note"], note)
        for bad, why in ((["PayoutDue", "PayoutApproved", "PayoutApproved"], "not a payout's life"),
                         (["PayoutDue", "PayoutStopped"], "recorded events say Stopped, the pool says Due")):
            got = r.review(self.world(bad, approved=None).d, POOL, 0)
            self.assertEqual((got["tx"]["kind"], got["approved"]), ("none", None), "not the events' word")
            self.assertIn(why, got["tx"]["note"])

    def test_a_result_that_does_not_count_up_holds_a_payout_that_waits_by_its_events(self):
        rec = Recount()
        ch, _ = rec.world(passed=Recount.C + 999_999)
        ch.d["payouts"][0]["trail"] = r.payout_trail(["PayoutDue"])
        self.assertEqual((r.review(ch.d, POOL, 0)["action"], r.review(ch.d, POOL, 0)["tx"]), ("hold", None))
        ch.d["payouts"][0].update(approved=True, trail=r.payout_trail(["PayoutDue", "PayoutApproved"]))
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["action"], got["tx"]["note"]), ("none", r.APPROVED), "approved on the chain: nothing to hold back")


class AHeldPayoutKeepsTheBatchRule(unittest.TestCase):
    """The hold branch gives no approval to sign, and must not lose what the pool's own rate says of approving in a
    batch (review of #290, which found the page's side of it dropping the field)."""

    def card(self, *, red):
        rec = Recount()
        ch, _ = rec.world(passed=Recount.C + 999_999)
        if red:
            ch.d["payouts"][0]["amount"] = 2_000 * 10**6  # payouts above the net income before 30 exams ended: red
        got = r.review(ch.d, POOL, 0)
        return got, r.card(got, ch.d)

    def test_a_held_payout_of_a_red_pool_is_still_a_card_that_forbids_the_batch(self):
        got, k = self.card(red=True)
        self.assertEqual((got["action"], got["passRates"]["level"], got["passRates"]["batch"]), ("hold", "red", False))
        self.assertEqual((k["action"], k["call"], k["batchApproval"]), ("hold", None, False))
        self.assertIn("do not sign", r.summary(got))

    def test_a_held_payout_of_a_green_pool_keeps_the_batch_open(self):
        got, k = self.card(red=False)
        self.assertEqual((got["action"], got["passRates"]["batch"], k["action"], k["call"], k["batchApproval"]), ("hold", True, "hold", None, True))

    def test_a_held_payout_whose_pool_rates_were_not_read_leaves_the_batch_as_the_card_defaults_it(self):
        rec = Recount()
        ch, _ = rec.world(passed=Recount.C + 999_999)
        del ch.d["exam_events"]
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["passRates"], r.card(got, ch.d)["batchApproval"]), (None, True))


# ── the daily limit on payouts (src/sim/SimPool.sol, `pay`; the number is read, never kept here) ───────────────────

class TheDailyLimit(unittest.TestCase):
    D = 86_400
    T = 1_000_000

    def test_a_wave_of_payouts_goes_out_as_the_pool_settles_them(self):
        T, D = self.T, self.D
        # docs/SIM-POOL.md: five claimed together and approved go out two at their 72 hours, two a day later, one the next.
        five = [(i, T) for i in range(5)]
        self.assertEqual(r.settle_order(five, [0, 0], D, T), {0: T, 1: T, 2: T + D, 3: T + D, 4: T + 2 * D})
        # Not approved: the third and fourth 8 days after their claim, the fifth 9 (the ceiling is 7 days from the claim).
        claim = T - 7 * D
        self.assertEqual(r.settle_order([(i, claim + 7 * D) for i in range(5)], [0, 0], D, T),
                         {0: T, 1: T, 2: T + D, 3: T + D, 4: T + 2 * D})
        # The limit is the contract's number: with three places the same five go out three and two.
        self.assertEqual(r.settle_order(five, [0, 0, 0], D, T), {0: T, 1: T, 2: T, 3: T + D, 4: T + D})
        self.assertEqual(r.settle_order(five[:3], [0], D, T), {0: T, 1: T + D, 2: T + 2 * D}, "and with one, one a day")

    def test_the_places_used_in_the_last_day_hold_a_payout_until_the_oldest_is_a_day_old(self):
        T, D = self.T, self.D
        self.assertEqual(r.settle_order([(0, T)], [T - 3_600, T - 1_800], D, T), {0: T - 3_600 + D}, "no 24 hours see three")
        self.assertEqual(r.settle_order([(0, T), (1, T)], [T - 3_600, T - 1_800], D, T), {0: T - 3_600 + D, 1: T - 1_800 + D})
        self.assertEqual(r.settle_order([(0, T)], [T - D, T - 1_800], D, T), {0: T}, "a settlement a day old is a free place")
        self.assertEqual(r.settle_order([(0, T)], [T - D + 1, T - 1_800], D, T), {0: T + 1})

    def test_a_payout_not_ready_is_passed_over_and_one_that_gets_ready_before_the_limit_frees_goes_first(self):
        T, D = self.T, self.D
        self.assertEqual(r.settle_order([(0, T + 1_000), (1, T)], [0, 0], D, T), {1: T, 0: T + 1_000}, "passed over, not waited for")
        # Both places used, the first free at T + 300: payout 0 gets ready at T + 100, before it, and goes first.
        self.assertEqual(r.settle_order([(0, T + 100), (1, T)], [T - D + 300, T - D + 400], D, T), {0: T + 300, 1: T + 400})
        # A payout whose time never comes before the limit's does not wait for it: the later one that is ready is held by the limit.
        self.assertEqual(r.settle_order([(0, T + 5 * D), (1, T)], [T - D + 300, T - D + 400], D, T), {1: T + 300, 0: T + 5 * D})
        self.assertEqual(r.settle_order([], [0, 0], D, T), {})

    def world(self, *, settled, room=None, next_at=None, limit=2, approved=False, now=None, state="Due", pids=1):
        ch, t1, s1 = honest_world()
        pay = ch.d["payouts"][0]
        if now is not None:
            ch.d["now"] = now
            pay["dueAt"] = now - 3_600 if approved else pay["dueAt"]
        pay["state"], pay["approved"] = state, approved
        for pid in range(1, pids):
            ch.payout(t1, s1, pid=pid)
            ch.d["payouts"][-1].update(approved=approved, state=state, dueAt=pay["dueAt"])
        slots = ([0] * limit + settled)[-limit:]
        now = ch.d["now"]
        room = sum(1 for t in slots if now >= t + self.D) if room is None else room
        next_at = (slots[0] + self.D if slots[0] else 0) if next_at is None else next_at
        ch.d["payCap"] = {POOL: {"limit": limit, "day": self.D, "room": room, "nextAt": next_at}}
        ch.d["settled"] = {POOL: settled}
        return ch

    def cap(self, ch, pid=0):
        return r.review(ch.d, POOL, pid)["dailyCap"]

    def test_a_chain_read_without_the_limit_says_nothing_of_it(self):
        ch, _, _ = honest_world()
        self.assertIsNone(r.review(ch.d, POOL, 0)["dailyCap"])
        self.assertIsNone(r.card(r.review(ch.d, POOL, 0), ch.d)["dailyCap"])
        self.assertNotIn("daily limit", r.summary(r.review(ch.d, POOL, 0)))

    def test_a_factory_without_the_limit_has_none_for_a_payout_to_wait_for(self):
        ch, _, _ = honest_world()
        ch.d["payCap"] = {POOL: False}
        got = self.cap(ch)
        self.assertEqual((got["status"], got["limit"], got["settleAt"]), ("none", None, None))
        self.assertNotIn("daily limit", r.summary(r.review(ch.d, POOL, 0)))

    def test_a_payout_waiting_for_its_approval_is_not_ready_and_the_limit_is_counted_after_its_ceiling(self):
        ch = self.world(settled=[])
        got = self.cap(ch)
        due = ch.d["payouts"][0]["dueAt"]
        ceiling = due - 72 * 3_600 + 7 * 86_400
        self.assertEqual((got["status"], got["readyAt"], got["settleAt"], got["ahead"], got["limit"], got["room"]),
                         ("not-ready", ceiling, ceiling, 0, 2, 2))
        ch = self.world(settled=[], approved=True, now=2_000_000)  # approved and due an hour ago
        got = self.cap(ch)
        self.assertEqual((got["status"], got["settleAt"], got["readyAt"]), ("free", ch.d["now"], ch.d["payouts"][0]["dueAt"]))
        self.assertIn("a place is free", r.summary(r.review(ch.d, POOL, 0)))

    def test_a_ready_payout_with_the_days_places_used_waits_for_the_oldest_to_be_a_day_old(self):
        now = 2_000_000
        ch = self.world(settled=[now - 3_600, now - 1_800], approved=True, now=now)
        got = self.cap(ch)
        self.assertEqual((got["status"], got["settleAt"], got["nextAt"], got["room"], got["ahead"]),
                         ("waits-limit", now - 3_600 + self.D, now - 3_600 + self.D, 0, 0))
        text = r.summary(r.review(ch.d, POOL, 0))
        self.assertIn("daily limit (2 a day, room 0): ready, settled from", text)
        k = r.card(r.review(ch.d, POOL, 0), ch.d)["dailyCap"]
        self.assertEqual((k["status"], k["limit"], k["dayS"]), ("waits-limit", 2, self.D))
        self.assertEqual(r.review(ch.d, POOL, 0)["verdict"], "clean", "a payout the limit holds is not suspicious")

    def test_the_payouts_ahead_take_the_places_first(self):
        ch = self.world(settled=[], approved=True, now=2_000_000, pids=3)
        got = [self.cap(ch, pid) for pid in range(3)]
        self.assertEqual([g["status"] for g in got], ["free", "free", "waits-limit"])
        self.assertEqual([g["ahead"] for g in got], [0, 1, 2])
        self.assertEqual(got[2]["settleAt"], ch.d["now"] + self.D)

    def test_the_limit_is_what_the_contract_says_not_two(self):
        now = 2_000_000
        three = self.world(settled=[now - 5_000, now - 4_000, now - 3_000], limit=3, approved=True, now=now)
        got = self.cap(three)
        self.assertEqual((got["limit"], got["status"], got["settleAt"]), (3, "waits-limit", now - 5_000 + self.D))
        # The same three settlements under a limit of 4 leave a place; under 2 the last two are the ones counted.
        four = self.world(settled=[now - 5_000, now - 4_000, now - 3_000], limit=4, approved=True, now=now)
        self.assertEqual((self.cap(four)["limit"], self.cap(four)["status"]), (4, "free"))
        two = self.world(settled=[now - 5_000, now - 4_000, now - 3_000], limit=2, approved=True, now=now)
        self.assertEqual(self.cap(two)["settleAt"], now - 4_000 + self.D)
        src = (ROOT / "ops/payout_review.py").read_text()
        self.assertNotRegex(src, r"(?m)^(PAYOUTS_PER_DAY|PAYOUTS_A_DAY|DAILY_CAP|PAY_PER_DAY)\b", "no number of payouts a day kept here")

    def test_what_the_events_and_the_pool_do_not_agree_on_is_not_given_a_time(self):
        now = 2_000_000
        ch = self.world(settled=[now - 3_600, now - 1_800], approved=True, now=now, room=2)  # the pool says room, the events say none
        got = self.cap(ch)
        self.assertEqual((got["status"], got["settleAt"], got["readyAt"]), ("unread", None, None))
        self.assertIn("give room 0", got["problem"])
        self.assertIn("not read:", r.summary(r.review(ch.d, POOL, 0)))
        ch = self.world(settled=[now - 3_600, now - 1_800], approved=True, now=now, next_at=7)
        self.assertEqual(self.cap(ch)["status"], "unread")
        ch = self.world(settled=[], approved=True, now=now)
        del ch.d["settled"]
        self.assertIn("settlements were not read", self.cap(ch)["problem"])
        ch = self.world(settled=[], approved=None, now=now)
        self.assertEqual(self.cap(ch)["status"], "unread", "whether it is approved was not read")
        ch = self.world(settled=[], approved=True, now=now)
        ch.d["payouts"].append({**ch.d["payouts"][0], "id": 5, "approved": None})
        self.assertIn("payout 5 is approved", self.cap(ch)["problem"])
        ch = self.world(settled=[], approved=True, now=now)
        ch.d["books"][POOL]["approvals"] = None
        self.assertEqual(self.cap(ch)["status"], "unread")
        ch = self.world(settled=[], approved=True, now=now)
        ch.d["times"] = {"delay": None, "hard": None}
        self.assertEqual(self.cap(ch)["status"], "unread")

    def test_a_payout_not_waiting_has_no_limit_to_wait_for(self):
        ch = self.world(settled=[], state="Paid", approved=True)
        self.assertEqual(self.cap(ch)["status"], "not-waiting")
        self.assertNotIn("daily limit", r.summary(r.review(ch.d, POOL, 0)))

    def test_the_latest_wait_the_pool_wrote_is_kept_beside_it(self):
        now = 2_000_000
        ch = self.world(settled=[now - 3_600, now - 1_800], approved=True, now=now)
        ch.d["waitsLimit"] = {(POOL, 0): {"nextAt": now - 3_600 + self.D, "time": now - 10}}
        self.assertEqual(self.cap(ch)["recorded"], {"nextAt": now - 3_600 + self.D, "time": now - 10})
        self.assertIsNone(self.cap(self.world(settled=[]))["recorded"])

    def test_the_limits_reads_are_the_contracts_by_their_source(self):
        factory, pool = (ROOT / "src/sim/SimPoolFactory.sol").read_text(), (ROOT / "src/sim/SimPool.sol").read_text()
        self.assertIn("uint8 public constant MIN_PAYOUTS_PER_DAY = 1;", factory)
        self.assertIn("uint8 public constant MAX_PAYOUTS_PER_DAY = uint8(MAX_PAYOUTS_A_DAY);", factory)
        self.assertIn("uint32 public constant PAYOUT_DAY = 24 hours;", factory)
        self.assertNotRegex(factory, r"(?<![A-Z_])PAYOUTS_PER_DAY\(", "the factory no longer answers a number of payouts a day: the pool does")
        self.assertIn("uint8 public payoutsPerDay;", pool)
        self.assertIn("payoutsPerDay = p.payoutsPerDay;", pool)
        self.assertIn("function payRoom() external view returns (uint256 room) {", pool)
        self.assertIn("function nextPayAt() external view returns (uint64) {", pool)
        self.assertIn("event PayoutWaitsLimit(uint256 indexed id, uint64 nextAt);", pool)
        self.assertEqual(r.PAYOUT_WAITS_LIMIT, "0x" + keccak(text="PayoutWaitsLimit(uint256,uint64)").hex())
        # `pay`: a place is free when its settlement is a day old; the check comes after the approval and before the money;
        # a payout it holds keeps its place, and so does every one after it; a settlement, paid or set aside, takes the oldest place.
        _, body = AgainstTheContracts.function(pool, "pay")
        order = ["if (block.timestamp < p.dueAt) break;", "if (!p.approved && block.timestamp < uint256(p.dueAt) + hardAfter) {",
                 "uint256 freeAt = uint256(last[0]) + day;", "emit PayoutWaitsLimit(i, uint64(freeAt));", "emit PayoutQueued(i, bal);",
                 "last[perDay - 1] = uint64(block.timestamp);", "if (_send(p.trader, p.amount)) {"]
        self.assertEqual(sorted(order, key=body.index), order)
        self.assertIn("if (block.timestamp < freeAt) {", body)
        self.assertIn("last[k] = last[k + 1];", body)
        self.assertIn("uint256 perDay = payoutsPerDay;", body, "the places counted are the pool's own number")
        _, room = AgainstTheContracts.function(pool, "payRoom")
        self.assertIn("for (uint256 k = 0; k < payoutsPerDay; ++k) {", room)
        self.assertIn("if (block.timestamp >= uint256(_settledAt[k]) + day) room += 1;", room)
        _, nxt = AgainstTheContracts.function(pool, "nextPayAt")
        self.assertIn("return oldest == 0 ? 0 : oldest + factory.PAYOUT_DAY();", nxt)

    def test_the_limit_and_the_pool_are_read_as_the_chain_answers(self):
        asked = []

        def view(answers):
            def call_view(to, sig, types, args, out):
                asked.append((to, sig))
                got = answers[sig]
                if isinstance(got, Exception):
                    raise got
                return (got,)
            return call_view
        FACTORY = addr(0xFA)
        ok = {"payoutsPerDay()": 5, "PAYOUT_DAY()": 86_400, "payRoom()": 1, "nextPayAt()": 5}
        with mock.patch.object(r.c, "call_view", side_effect=view(ok)):
            self.assertEqual(r.read_pay_cap(FACTORY, POOL), {"limit": 5, "day": 86_400, "room": 1, "nextAt": 5},
                             "the number is the pool's own setting")
        self.assertEqual([(to, sig) for to, sig in asked if sig == "payoutsPerDay()"], [(POOL, "payoutsPerDay()")], "asked of the pool")
        self.assertEqual([(to, sig) for to, sig in asked if sig == "PAYOUT_DAY()"], [(FACTORY, "PAYOUT_DAY()")], "the day of the factory")
        revert = Exception("execution reverted")
        # A pool from before the limit has neither: no limit. One with `payRoom()` and no number of its own: not guessed.
        with mock.patch.object(r.c, "call_view", side_effect=view({**ok, "payoutsPerDay()": revert, "payRoom()": revert})):
            self.assertIs(r.read_pay_cap(FACTORY, POOL), False, "a pool without the limit")
        with mock.patch.object(r.c, "call_view", side_effect=view({**ok, "payoutsPerDay()": revert})):
            self.assertIsNone(r.read_pay_cap(FACTORY, POOL), "the factory's old constant is not assumed for a pool with room but no number")
        busy = Exception("429 Too Many Requests")
        for sig in ok:
            with mock.patch.object(r.c, "call_view", side_effect=view({**ok, sig: busy})):
                self.assertIsNone(r.read_pay_cap(FACTORY, POOL), f"{sig} not read: not taken for no limit")
        with mock.patch.object(r.c, "call_view", side_effect=view({**ok, "payRoom()": revert})):
            self.assertIsNone(r.read_pay_cap(FACTORY, POOL), "a pool with the limit and no room: not read")
        with mock.patch.object(r.c, "call_view", side_effect=view({**ok, "payoutsPerDay()": 0})):
            self.assertIsNone(r.read_pay_cap(FACTORY, POOL), "a limit of none is not one the factory allows")
        with mock.patch.object(r.c, "call_view", side_effect=view({**ok, "payoutsPerDay()": 8})):
            self.assertEqual(r.read_pay_cap(FACTORY, POOL)["limit"], 8, "up to the factory's most")


class TheTiersAreTheAccountsOwn(unittest.TestCase):
    """The menu's tier (cp#302) sets each line's loss limit, target and kind of loss line, and they are on the account
    that line starts: the review reads them there (`rules()`, `drawdownKind()`), never from the pool's plan."""

    def test_the_pool_starts_each_account_with_its_lines_tier(self):
        # The rules are kept once and the line's tier put in when the account starts (A7 of 16 November): the
        # account holds its tier all the same.
        setup = _source("src/sim/SimPoolSetup.sol")
        self.assertIn("r.maxDrawdownBps = k.maxDrawdownBps;", setup)
        self.assertIn("t.targetsBps[j] = k.targetBps;", setup)
        self.assertIn("rules.fromBook, rules.maxWorseBps, k.drawdownKind, !funded);", setup)
        pool = _source("src/sim/SimPool.sol")
        self.assertIn("exam = _start(trader, line, false);", pool)
        self.assertIn("stage = _start(trader, a.line, true);", pool)
        self.assertIn("a = SimPoolSetup.start(_rules, funded, l, trader, factory);", pool)
        self.assertIn("SimLine storage l = _lines[line];", pool)
        self.assertIn("t.capital = k.size;", setup)

    def test_the_review_reads_the_drawdown_and_its_kind_from_the_account(self):
        asked = []

        def call_view(to, sig, types, args, out):
            asked.append((to, sig))
            return {"rules()": ((300, 450, 1_000, [3], [1_000]),), "drawdownKind()": (1,)}[sig]
        account = addr(0x301)
        kinds, bps = {}, {}
        with mock.patch.object(r.c, "call_view", side_effect=call_view):
            r.read_drawdown(account, kinds, bps)
        self.assertEqual((kinds, bps), ({account: 1}, {account: 450}))
        self.assertEqual(asked, [(account, "rules()"), (account, "drawdownKind()")], "of the account, nothing of the pool")
        src = (ROOT / "ops/payout_review.py").read_text()
        for word in ("tiers()", "SimTier", "examRules", "fundedRules", "targetBps"):
            self.assertNotIn(word, src, "no plan or tier of the pool's is read here")
        lib = _source("app/lib/exam.js")
        for word in ("tiers", "targetBps(", "examRules", "fundedRules"):
            self.assertNotIn(word, lib)

    def test_a_stop_of_a_reason_this_file_has_not_heard_of_is_a_stop(self):
        log = lambda topic, block, index: {"address": POOL, "topics": [topic, "0x" + f"{4:064x}"], "data": "0x", "blockNumber": hex(block),
                                          "logIndex": hex(index), "transactionHash": tx(block)}
        events = [log(r.PAYOUT_DUE, 10, 0), log(r.PAYOUT_STOPPED, 12, 0), log(r.STOP_LIFTED, 20, 0)]
        life = r.payout_lives(events)
        self.assertEqual(life, {(POOL, 4): ["PayoutDue", "PayoutStopped", "StopLifted"]})
        got = r.payout_trail(life[(POOL, 4)])
        self.assertEqual((got["state"], got["problems"], got["wasStopped"]), ("Due", [], True))
        # The reason is in the data, never read: a data of any reason (1 to 4) gives the same stop.
        for reason in (1, 2, 3, 4):
            data = encode(["uint64", "uint8", "address", "bytes32[]", "bytes32[]", "uint256", "uint256", "uint256"],
                          [800, reason, addr(0xB1), [b"\x01" * 32, b"\x02" * 32], [], 1, 0, 0])
            stopped = {**log(r.PAYOUT_STOPPED, 12, 0), "data": "0x" + data.hex()}
            self.assertEqual(r.payout_lives([log(r.PAYOUT_DUE, 10, 0), stopped])[(POOL, 4)], ["PayoutDue", "PayoutStopped"], reason)
        src = (ROOT / "ops/payout_review.py").read_text()
        self.assertNotRegex(src, r"reason in \(1, 2, 3\)|reason <= 3|reason < 4", "no list of the stop reasons that 4 could fall out of")


class WhyAPayoutWasStopped(unittest.TestCase):
    """A stop's reason, 4 (prohibited play, cp#303) included, is named: by the pool's `stopReasonOf(id)`, else by the
    event, never an unknown stop."""

    def test_the_words_are_the_page_librarys_and_the_contracts_reasons(self):
        lib = _source("app/lib/simpool.js")
        block = re.search(r"export const STOP_REASON = Object.freeze\(\{(.*?)\}\);", lib, re.S).group(1)
        words = {int(n): re.sub(r"\s+", " ", w) for n, w in re.findall(r'(\d): "(.*?)",\n', block)}
        self.assertEqual(words, {k: re.sub(r"\s+", " ", v) for k, v in r.STOP_REASONS.items()})
        self.assertEqual(sorted(r.STOP_REASONS), [1, 2, 3, 4])
        stops = _source("src/sim/SimPoolStops.sol")
        for n in (1, 2, 3, 4):
            self.assertRegex(stops, rf"reason == {n}\b")
        self.assertIn("mapping(uint256 id => uint8) public stopReasonOf;", _source("src/sim/SimPool.sol"))
        self.assertIn("reasonOf[id] = reason;", stops)
        self.assertEqual(r.STOPPED_DATA[1], "uint8", "the reason is the second word of PayoutStopped's data")

    def test_every_reason_has_words_and_one_this_file_has_none_for_is_named_by_its_number(self):
        for n in (1, 2, 3, 4):
            self.assertEqual(r.stop_reason_text(n), r.STOP_REASONS[n])
        self.assertIn("Prohibited play", r.stop_reason_text(4))
        text = r.stop_reason_text(7)
        self.assertTrue(text.startswith("Reason 7:"), text)
        self.assertNotIn("nknown", text)

    def stopped(self, **got):
        ch, t1, s1 = honest_world()
        pay = ch.d["payouts"][0]
        pay.update(state="Stopped", wasStopped=True, approved=True, stopReason=got)
        return ch

    def test_the_review_says_the_reason_in_words(self):
        for n in (1, 2, 3, 4, 9):
            ch = self.stopped(recorded=n, read=n)
            got = r.review(ch.d, POOL, 0)
            self.assertEqual((got["stoppedFor"]["reason"], got["stoppedFor"]["text"], got["stoppedFor"]["disagree"]),
                             (n, r.stop_reason_text(n), None))
            self.assertIn(f"stopped for reason {n}: ", r.summary(got))
        self.assertIn("Prohibited play", r.summary(r.review(self.stopped(recorded=4, read=4).d, POOL, 0)))

    def test_the_pools_answer_stands_in_for_the_event_and_the_event_for_a_pool_that_has_none(self):
        self.assertEqual(r.review(self.stopped(recorded=None, read=4).d, POOL, 0)["stoppedFor"]["reason"], 4)
        self.assertEqual(r.review(self.stopped(recorded=4, read=None).d, POOL, 0)["stoppedFor"]["reason"], 4)
        got = r.review(self.stopped(recorded=None, read=None).d, POOL, 0)
        self.assertEqual((got["stoppedFor"]["reason"], got["stoppedFor"]["text"]), (None, None))
        self.assertNotIn("stopped for reason", r.summary(got))

    def test_events_and_pool_that_differ_name_no_reason(self):
        got = r.review(self.stopped(recorded=3, read=4).d, POOL, 0)
        self.assertEqual((got["stoppedFor"]["reason"], got["stoppedFor"]["text"]), (None, None))
        self.assertIn("the stop's reason: the events say 3, the pool says 4", got["stoppedFor"]["disagree"])
        self.assertIn("unread", got["kinds"])
        self.assertIn("not read: the stop's reason", got["reasons"][-1])

    def test_a_payout_never_stopped_has_no_reason(self):
        ch, _, _ = honest_world()
        self.assertIsNone(r.review(ch.d, POOL, 0)["stoppedFor"])
        self.assertNotIn("stopped for", r.summary(r.review(ch.d, POOL, 0)))

    def test_the_reason_is_read_from_the_event_and_from_the_pool(self):
        def log(reason, pid=4):
            data = encode(r.STOPPED_DATA, [800, reason, addr(0xB1), [b"\x01" * 32, b"\x02" * 32], [b"\x03" * 32], 1, 0, 0])
            return {"address": POOL, "topics": [r.PAYOUT_STOPPED, "0x" + f"{pid:064x}"], "data": "0x" + data.hex(),
                    "blockNumber": hex(12), "logIndex": "0x0", "transactionHash": tx(12)}
        for reason in (1, 2, 3, 4):
            self.assertEqual(r.payout_stop_reasons([log(reason)]), {(POOL, 4): reason})
        self.assertEqual(r.payout_stop_reasons([log(4, pid=1), log(2, pid=2)]), {(POOL, 1): 4, (POOL, 2): 2})
        self.assertEqual(r.payout_stop_reasons([{**log(4), "data": "0x"}]), {}, "a log that is not a stop's is no reason")
        with mock.patch.object(r.c, "call_view", return_value=(4,)) as view:
            self.assertEqual(r.stop_reason_read(POOL, 7), 4)
        self.assertEqual(view.call_args.args[:2], (POOL, "stopReasonOf(uint256)"))
        with mock.patch.object(r.c, "call_view", side_effect=Exception("execution reverted")):
            self.assertIsNone(r.stop_reason_read(POOL, 7), "a pool from before the reason is kept")
        with mock.patch.object(r.c, "call_view", side_effect=Exception("429 Too Many Requests")):
            self.assertIsNone(r.stop_reason_read(POOL, 7), "not read: not a reason")
