"""The checker of simulated pools: what it calls a pair, a link and a stop, and that it writes what
the pool's stopPayout takes."""
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
        return ch

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
        now = 1_000_000
        ch.fills = [fill(A, t=now - s.KEEP_S - 1), fill(A, t=now - s.KEEP_S)]
        ch._times = {1: now - s.KEEP_S - 1, 2: now}
        ch.prune(now)
        self.assertEqual([f["t"] for f in ch.fills], [now - s.KEEP_S])
        self.assertEqual(ch._times, {2: now})
        self.assertGreaterEqual(s.KEEP_S, 48 * 3600 + 600, "a waiting payout's pairs are kept")


if __name__ == "__main__":
    unittest.main()
