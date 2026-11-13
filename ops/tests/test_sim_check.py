"""The checker of simulated pools: what it calls a pair, a link and a stop, and that it writes what
the pool's stopPayout takes."""
import json
import pathlib
import re
import unittest

from eth_abi import encode

from ops import sim_check as s

ROOT = pathlib.Path(__file__).resolve().parents[2]
A, B, C = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "cc" * 20


def fill(account, asset=3, buy=True, sz=100, t=1_000, tx="0x01"):
    return {"account": account, "asset": asset, "buy": buy, "sz": sz, "t": t, "tx": tx}


class APair(unittest.TestCase):
    def test_the_same_asset_the_other_side_close_in_time_and_size(self):
        a = fill(A)
        self.assertEqual(s.pairs([a], [fill(B, buy=False, t=1_600, sz=80, tx="0x02")], 600, 0.2),
                         [(a, fill(B, buy=False, t=1_600, sz=80, tx="0x02"))])

    def test_what_is_no_pair(self):
        a = fill(A)
        for b in (fill(B, buy=True),              # the same side: a copied signal
                  fill(B, buy=False, asset=4),    # another asset
                  fill(B, buy=False, t=1_601),    # too far apart
                  fill(B, buy=False, sz=79),      # too far in size
                  fill(A, buy=False)):            # its own account
            self.assertEqual(s.pairs([a], [b], 600, 0.2), [], b)


class ALink(unittest.TestCase):
    T1, T2, F = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20

    def test_one_sender_paid_both(self):
        t = [{"from": self.F, "to": self.T1, "tx": "0xa"}, {"from": self.F, "to": self.T2, "tx": "0xb"},
             {"from": C, "to": self.T1, "tx": "0xc"}]
        self.assertEqual(s.links(t, self.T1, self.T2), ["0xa", "0xb"])

    def test_one_paid_the_other(self):
        self.assertEqual(s.links([{"from": self.T1, "to": self.T2, "tx": "0xd"}], self.T1, self.T2), ["0xd"])

    def test_the_first_transfer_from_each_sender_is_named(self):
        t = [{"from": self.F, "to": self.T1, "tx": "0xa"}, {"from": self.F, "to": self.T1, "tx": "0xlater"},
             {"from": self.F, "to": self.T2, "tx": "0xb"}]
        self.assertEqual(s.links(t, self.T1, self.T2), ["0xa", "0xb"])

    def test_nothing_links_them_or_they_are_one_wallet(self):
        t = [{"from": self.F, "to": self.T1, "tx": "0xa"}, {"from": C, "to": self.T2, "tx": "0xb"}]
        self.assertEqual(s.links(t, self.T1, self.T2), [])
        self.assertEqual(s.links([{"from": self.F, "to": self.T1, "tx": "0xa"}], self.T1, self.T1), [])


class AStop(unittest.TestCase):
    def test_reason_one_without_links_two_with_them(self):
        p = [(fill(A, tx="0x1"), fill(B, buy=False, tx="0x2"))]
        self.assertEqual(s.finding(p, []), (1, B, ["0x1", "0x2"], []))
        self.assertEqual(s.finding(p, ["0xa", "0xb"]), (2, B, ["0x1", "0x2"], ["0xa", "0xb"]))
        self.assertIsNone(s.finding([], ["0xa"]), "a link alone is no reason")

    def test_the_other_account_is_the_one_most_matched_and_each_hash_once(self):
        a1, a2 = fill(A, tx="0x1"), fill(A, tx="0x3")
        p = [(a1, fill(C, buy=False, tx="0x9")), (a1, fill(B, buy=False, tx="0x2")), (a2, fill(B, buy=False, tx="0x2"))]
        self.assertEqual(s.finding(p, []), (1, B, ["0x1", "0x2", "0x3"], []))

    def test_only_a_waiting_payout_never_stopped_far_from_its_time(self):
        self.assertTrue(s.stop_due("Due", False, 10_000, 10_000 - s.MARGIN_S - 1))
        self.assertFalse(s.stop_due("Due", False, 10_000, 10_000 - s.MARGIN_S))
        self.assertFalse(s.stop_due("Due", True, 10_000, 0), "a payout is stopped once")
        self.assertFalse(s.stop_due("Stopped", False, 10_000, 0))
        self.assertFalse(s.stop_due("Paid", False, 10_000, 0))

    def test_it_calls_the_pools_stop_as_declared(self):
        pool = (ROOT / "src/sim/SimPool.sol").read_text()
        m = re.search(r"function stopPayout\(([^)]*)\)", pool)
        types = [part.split()[0] for part in m.group(1).split(",")]
        self.assertEqual(s.STOP, f"stopPayout({','.join(t.replace('calldata', '') for t in types)})")
        self.assertEqual(s.STOP_TYPES, ["uint256", "uint8", "address", "bytes32[]", "bytes32[]"])

    def test_a_fill_reads_off_its_log(self):
        data = encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"], [False, 120, 7, 0, 2, 1])
        log = {"address": A, "topics": ["0x00", "0x" + "00" * 31 + "04"], "data": "0x" + data.hex(), "transactionHash": "0xf"}
        self.assertEqual(s.fill_of(log, 55), {"account": s.to_checksum_address(A), "asset": 4, "buy": False, "sz": 120,
                                              "t": 55, "tx": "0xf"})


class TheService(unittest.TestCase):
    def checker(self):
        ch = s.Checker.__new__(s.Checker)
        ch.start_block, ch.usdc = 10, "0x" + "dd" * 20
        ch.transfers, ch.transfers_from, ch.fills, ch._times = {}, {}, [], {}
        ch.fills_from, ch.fills_read = 10, set()
        ch.settled, ch.started, ch.window_s = set(), {}, 600
        return ch

    def test_an_account_found_later_has_its_fills_read_from_the_start(self):
        from types import SimpleNamespace
        from unittest import mock
        ch = self.checker()
        a, b = (s.to_checksum_address("0x" + x * 20) for x in ("aa", "bb"))
        ch.keeper = SimpleNamespace(accounts={a: {}})
        with mock.patch.object(s.c, "rpc", return_value=[]) as rpc:
            ch.read_fills(59)
            ch.keeper.accounts[b] = {}
            rpc.reset_mock()
            ch.read_fills(70)
        reads = {}
        for call in rpc.call_args_list:
            reads.setdefault(tuple(call[0][1][0]["address"]), call[0][1][0]["fromBlock"])
        self.assertEqual(reads[(a,)], hex(60), "the one read before, from where it stopped")
        self.assertEqual(reads[(b,)], hex(10), "the one found later, from the deployment's block")
        self.assertEqual(ch.fills_read, {a, b})
        self.assertEqual(ch.fills_from, 71)

    def test_transfers_are_read_once_and_then_from_where_they_stopped(self):
        from unittest import mock
        ch = self.checker()
        t1 = s.to_checksum_address("0x" + "11" * 20)
        with mock.patch.object(s.c, "rpc", return_value=[]) as rpc:
            ch.transfers_to([t1], 59)
            first = rpc.call_count
            ch.transfers_to([t1], 59)
            self.assertEqual(rpc.call_count, first, "nothing new, nothing read")
            ch.transfers_to([t1], 60)
        self.assertEqual(rpc.call_args[0][1][0]["fromBlock"], hex(60))
        self.assertEqual(ch.transfers_from[t1], 61)

    def test_old_fills_and_times_are_dropped(self):
        ch = self.checker()
        cutoff = 1_000_000
        ch.fills = [fill(A, t=cutoff - 1), fill(A, t=cutoff)]
        ch._times = {1: cutoff - 1, 2: cutoff + 5}
        ch.prune(cutoff)
        self.assertEqual([f["t"] for f in ch.fills], [cutoff])
        self.assertEqual(ch._times, {2: cutoff + 5})

    def keep_case(self, payouts, done, starts):
        """A checker over one pool whose payouts are `payouts` (account, due), with `done` the stages the pool holds
        done and `starts` each stage's start."""
        from unittest import mock
        ch = self.checker()
        ch.window_s, ch.settled, ch.started = 600, set(), {}
        pool = s.to_checksum_address("0x" + "55" * 20)
        ch.keeper = mock.Mock(pools=[pool], accounts={a: {"pool": pool, "kind": "stage"} for a in starts})

        def view(to, sig, out="uint256", types=(), args=()):
            if sig == "head()":
                return 0
            if sig == "payoutCount()":
                return len(payouts)
            if sig == "payout(uint256)":
                acct, due = payouts[args[0]]
                return (acct, "0x" + "77" * 20, 800, due, 1, False)
            return starts[to]

        accounts_of = lambda pool_, sig, types, args, out: (2, 1, args[0] in done)
        return ch, mock.patch.object(s, "view", side_effect=view), mock.patch.object(s.c, "call_view", side_effect=accounts_of)

    def test_a_stage_claimed_late_keeps_every_fill_of_its_stage(self):
        """Passed on day 40, claimed on day 45 (the keeper was down): its payout is due on day 48, and its fills from
        day 10 on must still pair. A fixed 72 hours, or a stage's length plus the delay, would have dropped them."""
        day = 86_400
        S1, S2 = (s.to_checksum_address("0x" + x * 20) for x in ("a1", "a2"))
        ch, v, cv = self.keep_case({0: (S1, 48 * day)}, done={S1}, starts={S1: 10 * day, S2: 30 * day})
        with v, cv:
            self.assertEqual(ch.keep_from(45 * day), 10 * day - 600)
        ch.fills = [fill(A, t=10 * day), fill(B, t=10 * day - 600), fill(C, t=10 * day - 601)]
        ch.prune(10 * day - 600)
        self.assertEqual([f["account"] for f in ch.fills], [A, B], "its fills, and the others' within the window")

    def test_a_settled_stage_lets_its_fills_go(self):
        day = 86_400
        S1, S2 = (s.to_checksum_address("0x" + x * 20) for x in ("a1", "a2"))
        ch, v, cv = self.keep_case({0: (S1, 48 * day)}, done={S1}, starts={S1: 10 * day, S2: 30 * day})
        with v, cv:
            self.assertEqual(ch.keep_from(48 * day), 30 * day - 600, "S1's payout can no longer be stopped")
            self.assertEqual(ch.settled, {S1})
        ch2, v2, cv2 = self.keep_case({}, done=set(), starts={})
        with v2, cv2:
            self.assertEqual(ch2.keep_from(5_000), 5_000 - 600, "nothing to pay: the window only")

    def test_a_stage_found_after_its_pair_still_has_it(self):
        """The scan finds a few windows of accounts a pass. Pass one finds the other side's account B (and C, an old
        one) but not the stage S; pass two finds S. B's fill is read once, in pass one: it must still be there when S
        is found, so nothing is dropped until the scan has caught up. Once it has, C's old fill goes (review of #273)."""
        from types import SimpleNamespace
        from unittest import mock
        day = 86_400
        ch = self.checker()
        ch.dry, ch.tolerance = True, 0.2
        pool, S, Bx, Cx, T1, T2 = (s.to_checksum_address("0x" + x * 20) for x in ("55", "a1", "b1", "c1", "71", "72"))
        times = {20: 1 * day, 30: 10 * day, 31: 10 * day + 100}
        chain = [(Cx, 20, True), (S, 30, True), (Bx, 31, False)]  # account, block, is a buy
        found = [({Bx: "exam", Cx: "exam"}, 51), ({S: "stage"}, 101)]

        def scan(latest):
            accounts, nxt = found.pop(0)
            ch.keeper.accounts.update({a: {"pool": pool, "kind": k} for a, k in accounts.items()})
            ch.keeper.next_block = nxt

        ch.keeper = SimpleNamespace(pools=[pool], accounts={}, next_block=10, scan=scan)

        def rpc(method, params=()):
            if method == "eth_blockNumber":
                return hex(100)
            if method == "eth_getBlockByNumber":
                return {"timestamp": hex(45 * day if params[0] == "latest" else times[int(params[0], 16)])}
            q = params[0]
            if q["topics"][0] != s.sk.FILLED:
                return []
            return [{"address": a, "blockNumber": hex(b), "transactionHash": "0x%064x" % b,
                     "topics": [s.sk.FILLED, "0x%064x" % 3],
                     "data": "0x" + encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"],
                                           [buy, 100, 0, 0, 0, 0]).hex()}
                    for a, b, buy in chain
                    if a in q["address"] and int(q["fromBlock"], 16) <= b <= int(q["toBlock"], 16)]

        def view(to, sig, out="uint256", types=(), args=()):
            return {"head()": 0, "payoutCount()": 1, "stepStartedAt()": 10 * day,
                    "payout(uint256)": (S, T1, 800, 48 * day, 1, False)}[sig]

        def call_view(to, sig, types, args, out):
            return (2, T2, 0, 0) if sig == "result()" else (2, 1, True)

        with mock.patch.object(s.c, "rpc", side_effect=rpc), mock.patch.object(s, "view", side_effect=view), \
                mock.patch.object(s.c, "call_view", side_effect=call_view), mock.patch.object(s, "log"):
            self.assertEqual(ch.one_pass(), [], "S not found yet: nothing to check")
            self.assertEqual(sorted(f["account"] for f in ch.fills), sorted([Bx, Cx]), "nothing dropped while behind")
            stops = ch.one_pass()
        self.assertEqual([(x["id"], x["paired"], x["reason"]) for x in stops], [(0, Bx, 1)], "the pair is found")
        self.assertEqual(sorted(f["account"] for f in ch.fills), sorted([S, Bx]), "C's old fill is dropped once caught up")

    def test_a_pool_that_paid_both_traders_is_no_link(self):
        """Two traders the same pool paid before are not linked by it: the pool pays every trader, and a stop would
        write its payouts as links of reason 2 (review of #283). A sender of both that is not a pool still is."""
        from types import SimpleNamespace
        from unittest import mock
        ch = self.checker()
        pool, S, Bx, T1, T2, W = (s.to_checksum_address("0x" + x * 20) for x in ("55", "a1", "b1", "71", "72", "99"))
        ch.keeper = SimpleNamespace(pools=[pool], accounts={})
        ch.tolerance = 0.2
        ch.fills = [fill(S, buy=True, t=1_000, tx="0x0a"), fill(Bx, buy=False, t=1_100, tx="0x0b")]
        ch.transfers_to = lambda traders, latest: [
            {"from": pool, "to": T1, "tx": "0xp1"}, {"from": pool, "to": T2, "tx": "0xp2"}] + extra
        payout = (S, T1, 800, 10_000, 1, False, 0)
        with mock.patch.object(s, "view", return_value=payout), \
                mock.patch.object(s.c, "call_view", return_value=(1, T2, 0, 0)):
            extra = []
            self.assertEqual(s.Checker.check(ch, pool, 0, 0, 0), (1, Bx, ["0x0a", "0x0b"], []))
            extra = [{"from": W, "to": T1, "tx": "0xw1"}, {"from": W, "to": T2, "tx": "0xw2"}]
            self.assertEqual(s.Checker.check(ch, pool, 0, 0, 0), (2, Bx, ["0x0a", "0x0b"], ["0xw1", "0xw2"]))


if __name__ == "__main__":
    unittest.main()
