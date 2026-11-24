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


class Before(dict):
    """Every pool as one from before approvals, unless a test names its kind."""

    def __contains__(self, pool):
        return True

    def __missing__(self, pool):
        return False


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

    def test_a_pool_with_approvals_takes_a_stop_until_the_payout_is_paid(self):
        late = 10_000 + 3 * 86_400
        self.assertTrue(s.stop_due("Due", False, 10_000, late, True), "past its 72 hours and not paid: unapproved, or not paid yet")
        self.assertFalse(s.stop_due("Due", False, 10_000, late), "a pool from before approvals paid it at its 72 hours")
        self.assertFalse(s.stop_due("Due", True, 10_000, 0, True), "a payout is stopped once, there too")
        self.assertFalse(s.stop_due("Paid", False, 10_000, 0, True))

    def test_the_pools_own_stop_asks_no_time_and_no_approval(self):
        pool = (ROOT / "src/sim/SimPool.sol").read_text()
        i = pool.index("function stopPayout(")
        body = pool[pool.index("{", i):pool.index("\n    }\n", i)]
        # The pool's checks of a stop run in its library (SimPoolStops, DELEGATECALL), for the size of its code.
        self.assertIn("SimPoolStops.stop(", body)
        stops = (ROOT / "src/sim/SimPoolStops.sol").read_text()
        i = stops.index("function stop(")
        body += stops[stops.index("{", stops.index(") external returns", i)):stops.index("\n    }\n", i)]
        self.assertIn("if (p.state != SimPool.PayoutState.Due || p.wasStopped) revert SimPool.BadPayout();", body)
        for word in ("dueAt", "block.timestamp <", "block.timestamp >", "approved"):
            self.assertNotIn(word, body, "waiting and never stopped is all the pool asks: until it is paid")
        fields = re.findall(r"^\s*(\w+) (\w+);", re.search(r"struct Payout \{(.*?)\n    \}", pool, re.S).group(1), re.M)
        types = ["uint8" if kind == "PayoutState" else kind for kind, _ in fields]
        self.assertEqual((s.sk.PAYOUT, s.PAYOUT_BEFORE_APPROVALS), ("(" + ",".join(types) + ")", "(" + ",".join(types[:7]) + ")"))
        self.assertEqual(fields[7], ("bool", "approved"))

    def test_whether_a_pool_takes_approvals_has_three_answers(self):
        from unittest import mock
        P = "0x" + "55" * 20
        with mock.patch.object(s.c, "call_view", return_value=("0x" + "00" * 20,)):
            self.assertIs(s.takes_approvals(P), True, "the zero address is an answer: the pool has approvals")
        with mock.patch.object(s.c, "call_view", side_effect=Exception("execution reverted")):
            self.assertIs(s.takes_approvals(P), False)
        with mock.patch.object(s.c, "call_view", side_effect=Exception("429 Too Many Requests")), mock.patch.object(s, "log"):
            self.assertIs(s.takes_approvals(P), None)

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
        ch.approvals = Before()
        ch.stopped = set()
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

    def test_on_a_pool_with_approvals_a_payout_past_its_72_hours_keeps_its_stages_fills(self):
        day = 86_400
        S1, S2 = (s.to_checksum_address("0x" + x * 20) for x in ("a1", "a2"))
        ch, v, cv = self.keep_case({0: (S1, 48 * day)}, done={S1}, starts={S1: 10 * day, S2: 30 * day})
        ch.approvals = {s.to_checksum_address("0x" + "55" * 20): True}
        with v, cv:
            self.assertEqual(ch.keep_from(50 * day), 10 * day - 600, "not paid on day 50: a pair can still stop it")
            self.assertEqual(ch.settled, set())

    def test_a_pair_stops_a_payout_past_its_72_hours_only_on_a_pool_with_approvals(self):
        from types import SimpleNamespace
        from unittest import mock
        ch = self.checker()
        pool, S, Bx, T1, T2 = (s.to_checksum_address("0x" + x * 20) for x in ("55", "a1", "b1", "71", "72"))
        ch.keeper = SimpleNamespace(pools=[pool], accounts={})
        ch.tolerance = 0.2
        ch.fills = [fill(S, buy=True, t=1_000, tx="0x0a"), fill(Bx, buy=False, t=1_100, tx="0x0b")]
        ch.transfers_to = lambda traders, latest: []
        due, asked = 10_000, []

        def view(to, sig, out="uint256", types=(), args=()):
            asked.append(out)
            return (S, T1, 800, due, 1, False, 0, True)  # approved, and not paid yet

        with mock.patch.object(s, "view", side_effect=view), mock.patch.object(s.c, "call_view", return_value=(1, T2, 0, 0)):
            ch.approvals = {pool: True}
            self.assertEqual(s.Checker.check(ch, pool, 0, due + 86_400, 0), (1, Bx, ["0x0a", "0x0b"], []),
                             "a day past its 72 hours, approved: stopped while it is not paid")
            self.assertEqual(asked[-1], s.sk.PAYOUT)
            ch.approvals = {pool: False}
            self.assertIsNone(s.Checker.check(ch, pool, 0, due + 86_400, 0), "a pool from before approvals paid it by itself")
            self.assertIsNone(s.Checker.check(ch, pool, 0, due - s.MARGIN_S, 0), "and takes no stop in its last 5 minutes")
            self.assertIsNotNone(s.Checker.check(ch, pool, 0, due - s.MARGIN_S - 1, 0))
            self.assertEqual(asked[-1], s.PAYOUT_BEFORE_APPROVALS, "read as a pool from before approvals holds it")

    def test_a_pools_kind_is_read_once_and_one_not_read_is_stopped_as_with_approvals(self):
        from unittest import mock
        ch = self.checker()
        ch.approvals = {}
        P = "0x" + "55" * 20
        with mock.patch.object(s.c, "call_view", side_effect=Exception("429 Too Many Requests")), mock.patch.object(s, "log"):
            self.assertTrue(ch.until_paid(P), "a stop the pool refuses is a logged revert; a stop not sent is a payout gone")
            self.assertEqual(ch.approvals, {}, "and it is asked again in the next pass")
        with mock.patch.object(s.c, "call_view", side_effect=Exception("execution reverted")):
            self.assertFalse(ch.until_paid(P))
        with mock.patch.object(s.c, "call_view", side_effect=AssertionError("asked twice")):
            self.assertFalse(ch.until_paid(P))

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



class Node:
    """A node of one pool with one payout claimed: the stage S paid, B on the other side of every fill of S. It
    answers what the checker reads; `down` makes every call fail as a node does that is not there."""

    def __init__(self, pairs=True):
        self.pool, self.S, self.B, self.T1, self.T2 = (s.to_checksum_address("0x" + x * 20)
                                                       for x in ("55", "a1", "b1", "71", "72"))
        self.latest, self.now, self.due = 100, 50_000, 300_000
        # account, block, is a buy; a block's time is 1000 + 10 a block.
        self.fills = [(self.S, 30, True), (self.B, 31, not pairs)]
        # account, trader, claimed, due, state (1 Due, 2 Stopped), was stopped, paid at, approved
        self.payouts = [[self.S, self.T1, 800, self.due, 1, False, 0, False]]
        self.down = False
        self.calls = 0
        self.sent = []

    def rpc(self, method, params=()):
        self.calls += 1
        if self.down:
            raise RuntimeError("connection refused")
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(self.now if params[0] == "latest" else 1_000 + 10 * int(params[0], 16))}
        if method != "eth_getLogs":
            return None  # a view or a send: answered by the methods below
        q = params[0]
        if q["topics"][0] != s.sk.FILLED:
            return []  # no USDC moved: reason 1
        return [{"address": a, "blockNumber": hex(b), "transactionHash": "0x%064x" % b,
                 "topics": [s.sk.FILLED, "0x%064x" % 3],
                 "data": "0x" + encode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"],
                                       [buy, 100, 0, 0, 0, 0]).hex()}
                for a, b, buy in self.fills
                if a in q["address"] and int(q["fromBlock"], 16) <= b <= int(q["toBlock"], 16)]

    def view(self, to, sig, out="uint256", types=(), args=()):
        self.rpc("eth_call")
        return {"head()": 0, "payoutCount()": len(self.payouts), "stepStartedAt()": 1_000,
                "payout(uint256)": tuple(self.payouts[args[0]]) if args else None}[sig]

    def call_view(self, to, sig, types, args, out):
        self.rpc("eth_call")
        if sig == "approver()":
            return ("0x" + "99" * 20,)  # a pool with payout approval
        if sig == "result()":
            return (2, self.T2, 0, 0)
        return (2, 1, False)  # accountOf: not done

    def transact(self, wallet, to, sig, types, args):
        self.rpc("eth_sendRawTransaction")
        self.sent.append((to, sig, args))
        self.payouts[args[0]][4:6] = [2, True]  # the pool holds it stopped, and stopped once
        return {"transactionHash": "0x" + "ee" * 32}


class TheLoop(unittest.TestCase):
    """The service: passes on a node that answers, fails and comes back, with a key and without one."""

    def checker(self, node, dry=False):
        import tempfile
        from types import SimpleNamespace
        state = pathlib.Path(tempfile.mkdtemp()) / "check.json"
        record = {"SimPoolFactory": "0x" + "fa" * 20, "usdc": "0x" + "dd" * 20, "block": 10}
        ch = s.Checker(record, None if dry else object(), "0x" + "c1" * 20, dry, state)
        ch.keeper = SimpleNamespace(pools=[node.pool], next_block=node.latest + 1, scan=lambda latest: None,
                                    accounts={node.S: {"pool": node.pool, "kind": "stage"},
                                              node.B: {"pool": node.pool, "kind": "exam"}})
        return ch

    def on(self, node):
        from contextlib import ExitStack
        from unittest import mock
        stack = ExitStack()
        stack.enter_context(mock.patch.object(s.c, "rpc", side_effect=node.rpc))
        stack.enter_context(mock.patch.object(s, "view", side_effect=node.view))
        stack.enter_context(mock.patch.object(s.c, "call_view", side_effect=node.call_view))
        stack.enter_context(mock.patch.object(s.c, "transact", side_effect=node.transact))
        self.lines = []
        stack.enter_context(mock.patch.object(s, "log", side_effect=lambda event, **f: self.lines.append({"event": event, **f})))
        return stack

    def events(self, name):
        return [l for l in self.lines if l["event"] == name]

    def test_a_payout_with_a_pair_is_stopped_once_and_the_line_says_which_why_and_the_hash(self):
        node = Node()
        ch = self.checker(node)
        with self.on(node):
            stops = ch.one_pass()
            self.assertEqual([(x["id"], x["reason"], x["paired"]) for x in stops], [(0, 1, node.B)])
            self.assertEqual(len(node.sent), 1)
            to, sig, args = node.sent[0]
            self.assertEqual((to, sig, args[:3]), (node.pool, s.STOP, [0, 1, node.B]))
            sent = self.events("sent")
            self.assertEqual(len(sent), 1)
            self.assertEqual({k: sent[0][k] for k in ("to", "call", "id", "reason", "paired", "tx")},
                             {"to": node.pool, "call": "stopPayout", "id": 0, "reason": 1, "paired": node.B,
                              "tx": "0x" + "ee" * 32})
            self.assertEqual(self.events("check_done")[-1]["stops"], 1)
            # The next pass: the pool holds it stopped, and the checker remembers it. Nothing is sent.
            self.assertEqual(ch.one_pass(), [])
            self.assertEqual(len(node.sent), 1)
            self.assertEqual(self.events("check_done")[-1]["stops"], 0)

    def test_a_payout_it_stopped_is_not_taken_up_again_even_before_the_pool_shows_it(self):
        node = Node()
        ch = self.checker(node)
        with self.on(node):
            ch.one_pass()
            node.payouts[0][4:6] = [1, False]  # a node behind the head that saw no stop yet
            self.assertEqual(ch.one_pass(), [])
        self.assertEqual(len(node.sent), 1)

    def test_a_payout_whose_stop_the_operator_lifted_is_never_stopped_again(self):
        node = Node()
        ch = self.checker(node)
        with self.on(node):
            ch.one_pass()
            ch.stopped.clear()  # a restarted service
            node.payouts[0][4:6] = [1, True]  # lifted: waiting again, stopped once
            self.assertEqual(ch.one_pass(), [])
        self.assertEqual(len(node.sent), 1, "the pool's one stop a payout, and the checker's own rule")

    def test_no_pair_no_stop(self):
        node = Node(pairs=False)
        ch = self.checker(node)
        with self.on(node):
            self.assertEqual(ch.one_pass(), [])
        self.assertEqual(node.sent, [])
        self.assertEqual(self.events("pair_found"), [])

    def test_the_payout_is_read_again_before_the_stop_and_one_stopped_meanwhile_is_left(self):
        node = Node()
        ch = self.checker(node)
        real = ch.check

        def check(pool, pid, now, latest):
            got = real(pool, pid, now, latest)
            node.payouts[pid][4:6] = [2, True]  # another hand stopped it while this pass read
            return got

        ch.check = check
        with self.on(node):
            self.assertEqual(ch.one_pass(), [])
        self.assertEqual(node.sent, [])
        self.assertEqual([(l["id"], l["state"]) for l in self.events("stop_not_sent")], [(0, "Stopped")])

    def test_without_a_key_it_only_says_what_it_would_send_and_says_it_once(self):
        node = Node()
        ch = self.checker(node, dry=True)
        with self.on(node):
            self.assertEqual(len(ch.one_pass()), 1)
            ch.one_pass()
        self.assertEqual(node.sent, [])
        would = self.events("would_send")
        self.assertEqual([(l["to"], l["call"], l["id"], l["reason"]) for l in would], [(node.pool, "stopPayout", 0, 1)])
        self.assertEqual(self.events("sent"), [])

    def test_a_stop_that_failed_is_tried_again_in_the_next_pass(self):
        node = Node()
        ch = self.checker(node)
        real = node.transact
        failures = [RuntimeError("execution reverted")]

        def transact(*a):
            if failures:
                raise failures.pop()
            return real(*a)

        node.transact = transact
        with self.on(node):
            ch.one_pass()
            self.assertEqual([l["id"] for l in self.events("send_failed")], [0])
            self.assertEqual(ch.stopped, set())
            ch.one_pass()
        self.assertEqual(len(node.sent), 1)

    def test_a_node_that_fails_skips_the_pass_the_process_lives_and_the_next_pass_stops_it(self):
        node = Node()
        ch = self.checker(node)
        sleeps = []

        class Enough(Exception):
            pass

        def sleep(t):
            sleeps.append(t)
            if len(sleeps) == 1:
                node.down = False  # the node is back for the second pass
            if len(sleeps) == 2:
                raise Enough

        node.down = True
        with self.on(node), mock_print():
            with self.assertRaises(Enough):
                s.run(ch, 600, once=False, sleep=sleep)
        self.assertEqual(sleeps, [600, 600], "the next pass on time, not at once")
        self.assertEqual(len(self.events("pass_failed")), 1)
        self.assertIn("connection refused", self.events("pass_failed")[0]["error"])
        self.assertEqual(len(node.sent), 1, "the second pass stopped it")

    def test_no_line_of_the_service_writes_the_nodes_address(self):
        """Review 9 of #336 (L2): a node's address may hold its key. A pass the node fails and a stop it refuses are
        said without it, and a node that fails before the first pass ends the program without it too."""
        from unittest import mock
        url = "https://rpc.example.org/v2/SECRETKEY123"
        node = Node()
        ch = self.checker(node)
        real, down = node.rpc, [True]

        def rpc(method, params=()):
            if down:
                raise RuntimeError(f"403 Client Error: Forbidden for url: {url}")
            return real(method, params)

        node.rpc = rpc
        failures = [RuntimeError(f"Max retries exceeded with url: /v2/SECRETKEY123 ({url})")]

        def transact(*a):
            if failures:
                raise failures.pop()
            return node.sent.append(a) or {"transactionHash": "0x" + "ee" * 32}

        node.transact = transact
        with self.on(node), mock_print():
            self.assertEqual(s.run(ch, 600, once=True, sleep=lambda t: None), 1)
            down.clear()
            ch.one_pass()
        said = json.dumps(self.lines)
        self.assertEqual([len(self.events(e)) for e in ("pass_failed", "send_failed")], [1, 1])
        self.assertNotIn("SECRETKEY123", said)
        self.assertNotIn("rpc.example.org", said)
        self.assertIn("403", self.events("pass_failed")[0]["error"])
        with mock.patch.object(s.sn, "confirm_service", side_effect=RuntimeError(f"down: {url}")), \
                mock.patch.object(s.deployments, "load") as load:
            with self.assertRaises(SystemExit) as caught:
                s.main(["--deployment", "sim1", "--once"])
        self.assertNotIn("SECRETKEY123", str(caught.exception))
        self.assertIn("the node failed", str(caught.exception))
        load.assert_not_called()

    def test_a_read_the_node_breaks_off_is_read_again_whole_and_nothing_is_counted_twice(self):
        node = Node()
        ch = self.checker(node)
        node.latest = 200  # four windows of 50 blocks
        node.fills.append((node.S, 180, True))
        real = node.rpc
        state = {"n": 0}

        def rpc(method, params=()):
            if method == "eth_getLogs" and params[0]["topics"][0] == s.sk.FILLED:
                state["n"] += 1
                if state["n"] == 3:
                    raise RuntimeError("timeout")
            return real(method, params)

        node.rpc = rpc
        with self.on(node):
            with self.assertRaises(RuntimeError):
                ch.read_fills(200)
            self.assertEqual((ch.fills, ch.fills_from, ch.fills_read), ([], 10, set()), "nothing kept of it")
            ch.read_fills(200)
        self.assertEqual(sorted(f["tx"] for f in ch.fills), ["0x%064x" % b for b in (30, 31, 180)])

    def test_a_node_behind_the_head_already_read_is_read_nothing_from(self):
        node = Node()
        ch = self.checker(node)
        with self.on(node):
            ch.read_fills(100)
            self.assertEqual((len(ch.fills), ch.fills_from), (2, 101))
            ch.read_fills(60)  # another node, behind
            self.assertEqual((len(ch.fills), ch.fills_from), (2, 101), "no fill twice, the cursor where it was")
            self.assertEqual([(l["head"], l["read_to"]) for l in self.events("node_behind")], [(60, 100)])
            ch.read_fills(100)
        self.assertEqual(len(ch.fills), 2)

    def test_once_prints_the_stops_and_says_a_failed_pass_by_its_code(self):
        node = Node()
        ch = self.checker(node, dry=True)
        with self.on(node), mock_print() as out:
            self.assertEqual(s.run(ch, 600, once=True, sleep=lambda t: self.fail("--once never sleeps")), 0)
        self.assertEqual([x["id"] for x in json.loads(out.getvalue())], [0])
        node.down = True
        with self.on(node), mock_print():
            self.assertEqual(s.run(self.checker(node, dry=True), 600, once=True, sleep=lambda t: None), 1)

    def test_the_checker_never_lifts_a_stop(self):
        """Lifting a stop is the factory's operator's (SimPool.liftStop; `deploy_sim lift` for our Safe): the checker's
        script has no lift, as a service or as a command, and refuses --lift before it reads anything."""
        from unittest import mock
        src = (ROOT / "ops" / "sim_check.py").read_text()
        self.assertNotIn('"liftStop(uint256)"', src)
        self.assertNotIn("args.lift", src)
        with mock.patch.object(s.deployments, "load") as load, mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                s.main(["--deployment", "sim1", "--wallet", "checker", "--lift", "0x" + "ab" * 20 + ":1"])
        self.assertEqual(caught.exception.code, 2, "an unknown flag")
        load.assert_not_called()


class NoKeyOfTheNodeInALine(unittest.TestCase):
    """A node's address may hold its key, in its path or in its host name: `quiet` takes every configured address out."""

    def test_one_configured_address_that_begins_another_leaves_none_of_the_others_key(self):
        """The first node's address is the start of the second's, which ends in its key: taken out in the order they
        are configured, the first would turn the second into "<node>/KEY", a text no later rule knows for an address."""
        import os
        from unittest import mock
        short, long = "https://rpc.example.org", "https://rpc.example.org/SECRETKEY123"
        texts = (f"403 Client Error: Forbidden for url: {long}", f"down: {long}", f"both: {short} and {long}, twice {long}")
        for first, backup in ((short, long), (long, short)):
            with mock.patch.object(s.c, "RPC_URL", first), \
                    mock.patch.dict(os.environ, {"COLOSSEUM_RPC_URL": first, "COLOSSEUM_RPC_BACKUP_URL": backup}):
                for text in texts:
                    got = s.quiet(RuntimeError(text), 400)
                    self.assertNotIn("SECRETKEY123", got, (first, text))
                    self.assertNotIn("rpc.example.org", got, (first, text))
                    self.assertIn("<node>", got)
                self.assertEqual(s.quiet(RuntimeError(f"down: {long}"), 400), "down: <node>")

    def test_a_stop_the_node_refused_is_said_with_no_host_name_cut_in_two(self):
        """The refusal's text is cut to 200. Cut first, a host name that is the node's key stands there in part,
        `host='KEY.prov`, with no end for any rule to know it by: the address goes out of the whole text, then the cut."""
        import os
        from unittest import mock
        from ops import contract_errors as ce
        url = "https://SECRETKEY123.provider.example/v2"
        node = Node()
        ch = TheLoop().checker(node)
        lines = []
        for lead in range(150, 200):
            text = "x" * lead + " HTTPSConnectionPool(host='SECRETKEY123.provider.example', port=443): Max retries exceeded"
            with mock.patch.object(s.c, "RPC_URL", url), mock.patch.dict(os.environ, {"COLOSSEUM_RPC_URL": url}), \
                    mock.patch.object(s.c, "transact", side_effect=RuntimeError(text)), \
                    mock.patch.object(s, "log", side_effect=lambda event, **f: lines.append({"event": event, **f})):
                self.assertFalse(ch.send(node.pool, [], s.STOP, s.STOP_TYPES, id=0))
        self.assertEqual(len(lines), 50)
        self.assertNotIn("SECRETKEY123", json.dumps(lines))
        self.assertTrue(all(len(l["error"]) <= 200 for l in lines))
        # What the pool itself refused with is still said by its name, whatever stands around it.
        sel, sig = next((k, v) for k, v in ce.BY_SELECTOR.items() if v.endswith("()"))
        refused = RuntimeError(f"{{'code': 3, 'message': 'execution reverted', 'data': '{sel}'}} for url: {url}/SECRETKEY123")
        with mock.patch.object(s.c, "RPC_URL", url), mock.patch.object(s.c, "transact", side_effect=refused), \
                mock.patch.object(s, "log", side_effect=lambda event, **f: lines.append({"event": event, **f})):
            ch.send(node.pool, [], s.STOP, s.STOP_TYPES, id=0)
        self.assertEqual(lines[-1]["error"], sig)


class ThePace(unittest.TestCase):
    def test_reads_are_spaced_by_the_gap_from_the_end_of_the_last(self):
        clock = [100.0]
        waits = []

        def sleep(t):
            waits.append(round(t, 6))
            clock[0] += t

        def rpc(method, params=()):
            clock[0] += 0.25  # the node takes a quarter of a second
            return method

        call = s.paced(rpc, 1.0, clock=lambda: clock[0], sleep=sleep)
        self.assertEqual(call("a"), "a")
        call("b")
        clock[0] += 3
        call("c")
        self.assertEqual(waits, [1.0], "the first at once, the second a second after the first ended, the third late enough")

    def test_a_failed_read_counts_for_the_pace_too(self):
        clock, waits = [0.0], []

        def rpc(method, params=()):
            raise RuntimeError("429")

        call = s.paced(rpc, 2.0, clock=lambda: clock[0], sleep=waits.append)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                call("x")
        self.assertEqual(waits, [2.0])

    def test_the_defaults(self):
        self.assertEqual(s.DEFAULT_EVERY_S, 600)
        self.assertLessEqual(s.DEFAULT_EVERY_S + 3_600, 72 * 3_600, "a pass, even a slow one, comes long before 72 hours")
        self.assertEqual(s.RPC_GAP_S, 1.0)


def mock_print():
    import contextlib
    import io
    return contextlib.redirect_stdout(io.StringIO())


if __name__ == "__main__":
    unittest.main()
