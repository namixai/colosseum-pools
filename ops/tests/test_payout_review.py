"""The review of a simulated pool's payout, on a chain made up here: a clean payout, a pair inside the
factory, a pair whose two traders were funded from one wallet, and a code bought up from new wallets."""
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
    ch.fill(s1, buy=False, t=50_000)
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
        self.assertEqual(got["fills"], {"stage": 1, "exam": 1})
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
        self.assertEqual(got["stop"], {"on": "stage", "reason": 1, "paired": e9, "pairedTrader": t9,
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

    def test_too_far_apart_or_too_unlike_in_size_is_no_pair(self):
        ch, _, _ = honest_world()
        t9, e9 = addr(0x199), addr(0x299)
        ch.sale(t9, e9)
        ch.fill(addr(0x301), buy=True, sz=100, t=60_000)
        ch.fill(e9, buy=False, sz=100, t=60_601)
        ch.fill(e9, buy=False, sz=79, t=60_000)
        got = r.review(ch.d, POOL, 0)
        self.assertIsNone(got["stop"])
        self.assertEqual(got["verdict"], "clean")

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
        self.assertEqual(got["late"]["trader"], {"n": 8, "median": 20, "max": 20})
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
        self.assertEqual(got["verdict"], "clean")
        self.assertIn("approved: paid from then", r.summary(got))
        self.pair(ch, s1)
        ch.d["now"] = ch.d["payouts"][0]["dueAt"] + 3_600  # approved, past its 72 hours, not paid yet
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["stoppable"], got["tx"]["kind"]), (True, "stop"), "the pool stops an approved payout until it is paid")

    def test_a_pool_that_names_no_approver_is_offered_no_approval(self):
        ch, _, _ = honest_world()
        ch.d["books"][POOL]["approver"] = r.ZERO_ADDRESS
        got = r.review(ch.d, POOL, 0)
        self.assertEqual((got["tx"]["kind"], got["tx"]["data"], got["tx"]["note"]), ("none", None, r.NO_APPROVER))
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

    def test_an_account_not_read_makes_the_pool_total_incomplete(self):
        ch, _, _ = honest_world()
        ch.d["unread"] = [addr(0x210)]
        got = r.review(ch.d, POOL, 0)["running"]
        self.assertEqual((got["enough"], got["unread"]), (False, [addr(0x210)]))


class ReadingTheChain(unittest.TestCase):
    """`read_chain` against a node made up here: the logs as the contracts write them."""

    def test_it_reads_sales_stages_payouts_fills_and_transfers(self):
        factory = addr(0xFA)
        t1, e1, s1, partner = addr(0x101), addr(0x201), addr(0x301), addr(0xB0)
        code = b"\x11" * 32

        def word(a):
            return "0x" + "00" * 12 + a[2:].lower()

        def log(address, topics, data, block, h):
            return {"address": address, "topics": topics, "data": "0x" + data.hex(), "blockNumber": hex(block),
                    "transactionHash": h}

        pool_logs = [
            log(POOL, [sk.EXAM_SOLD, word(e1), word(t1), word(partner)],
                encode(["uint256", "uint64", "uint64", "uint256", "bool", "bytes32"], [2, SIZE, 100, 0, False, code]), 11, tx(1)),
            log(POOL, [sk.FUNDED, word(s1), word(e1), word(t1)], b"", 20, tx(2)),
            log(POOL, [r.PAYOUT_DUE, "0x" + "00" * 31 + "07", word(s1), word(t1)],
                encode(["uint64", "uint64"], [800, 5_000]), 30, tx(3)),
        ]
        fill_logs = [log(s1, [sk.FILLED, "0x" + "00" * 31 + "03"],
                         encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"], [True, 100, 7, 0, 4, 1]), 25, tx(4))]
        usdc_logs = [log(USDC, [r.TRANSFER, word(EXCHANGE), word(t1)], encode(["uint256"], [5]), 5, tx(5))]

        def rpc(method, params=()):
            if method == "eth_blockNumber":
                return hex(40)
            if method == "eth_getBlockByNumber":
                return {"timestamp": hex(1_000 if params[0] == "latest" else 100 * int(params[0], 16))}
            q = params[0]
            if q["address"] == USDC:
                return usdc_logs
            if q["topics"] == [sk.FILLED]:
                return fill_logs
            return pool_logs

        views = {("pools()",): [POOL], ("owed()",): 800, ("held()",): 0, ("reserved()",): 0, ("largestPayout()",): 800,
                 ("staged()",): 1_600,
                 ("payout(uint256)",): (s1, t1, 800, 5_000, 1, False, 0, False)}

        def view(a, sig, out, types=(), args=()):
            return views[(sig,)]

        def call_view(to, sig, types, args, out):
            return {"balanceOf(address)": (9_000,), "result()": (2, t1, 0, 0), "accountOf(address)": (2, 0, True),
                    "approver()": (addr(0xAB),), "PAYOUT_HARD()": (8 * 86_400,), "PAYOUT_DELAY()": (72 * 3_600,)}[sig]

        with mock.patch.object(r.c, "rpc", side_effect=rpc), mock.patch.object(r.core, "view", side_effect=view), \
                mock.patch.object(r.c, "call_view", side_effect=call_view):
            got = r.read_chain({"SimPoolFactory": factory, "usdc": USDC, "block": 10})
        self.assertEqual((got["now"], got["latest"], got["pools"]), (1_000, 40, [POOL]))
        self.assertEqual(got["sales"], [{"pool": POOL, "exam": e1, "trader": t1, "partner": partner, "line": 2,
                                         "size": SIZE, "price": 100, "code": "0x" + code.hex(), "block": 11, "tx": tx(1)}])
        self.assertEqual(got["funded"], [{"pool": POOL, "stage": s1, "exam": e1, "trader": t1, "block": 20}])
        self.assertEqual(got["payouts"], [{"pool": POOL, "id": 7, "account": s1, "trader": t1, "amount": 800,
                                           "dueAt": 5_000, "block": 30, "state": "Due", "wasStopped": False,
                                           "approved": False}])
        self.assertEqual(got["fills"], [{"account": s1, "asset": 3, "buy": True, "sz": 100, "t": 2_500, "tx": tx(4), "late": 4}])
        self.assertEqual(got["transfers"], [{"from": EXCHANGE, "to": t1, "tx": tx(5), "block": 5}])
        self.assertEqual(got["books"], {POOL: {"balance": 9_000, "owed": 800, "held": 0, "reserved": 0, "approvals": True,
                                               "largestPayout": 800, "staged": 1_600, "approver": addr(0xAB)}})
        self.assertEqual((got["status"][s1], got["done"][s1], got["unread"]), ("Passed", True, []))
        self.assertEqual(got["times"], {"delay": 72 * 3_600, "hard": 8 * 86_400}, "what the factory answers, not 7 days by rote")
        review = r.review(got, POOL, 7)
        self.assertEqual(review["hardAt"], 5_000 - 72 * 3_600 + 8 * 86_400)
        self.assertEqual((review["exam"], review["size"], review["verdict"]), (e1, SIZE, "clean"))


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
        self.assertIn("if (p.state != PayoutState.Due || p.approved) revert BadPayout();", body)
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
        self.assertIn("if (p.state != PayoutState.Due || p.wasStopped) revert BadPayout();", body)
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
