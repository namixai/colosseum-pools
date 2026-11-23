"""The stop book's own watch (ops/book_watch.py): a process of its own that raises an alarm when the book's blocks go
unchecked and, with a key, checks them itself while the keeper is behind. On the node made up in test_book_keeper.py."""
import pathlib
import time
import unittest
from unittest import mock

from ops import book_keeper as bk
from ops import book_watch as bw
from ops import stop_filler as sf
from ops.tests.test_book_keeper import BOOK, KEY, Base, Node
from ops.tests.test_book_keeper_hardening import OTHER


class TheWatch(Base):
    def watch(self, wallet=KEY, **kw):
        alarm = mock.Mock(side_effect=lambda e, **f: self.alarms.append((e, f)))
        lanes = [bk.Lane("watcher", sf.Sender(wallet, self.node))] if wallet else []
        return bk.Poker(BOOK, self.node, lanes, alarm, takeover=2, hold=5, role="watch", **kw)

    def ticks(self, p, *heads):
        out = []
        for h in heads:
            self.node.head = h
            out.append(p.tick())
        return out

    def test_a_watch_with_a_key_sends_once_an_hour_as_a_sign_of_life(self):
        p = self.watch()
        self.assertEqual(self.ticks(p, 100, 101), [True, False], "at the start, then not while the book is checked")
        p.sent_at -= bk.HEARTBEAT_S
        self.assertEqual(self.ticks(p, 102), [True])

    def test_a_book_checked_by_the_keeper_is_left_alone(self):
        p = self.watch()
        p.sent_at = time.monotonic()  # its sign of life sent
        self.assertEqual(self.ticks(p, 100, 101, 102), [False] * 3)
        self.assertEqual(self.node.raw, [])

    def test_a_book_two_blocks_behind_is_checked_by_the_watch_every_block_for_a_while(self):
        p = self.watch()
        p.sent_at = time.monotonic()
        self.node.last_poke = 98
        self.assertEqual(self.ticks(p, 100), [True])
        self.assertEqual([a["last_poke"] for a in self.alarm_of("book_poke_taken_over")], [98])
        self.node.last_poke = None  # its own check landed: the book is not behind any more
        self.assertEqual(self.ticks(p, 101, 102, 103, 104, 105, 106), [True] * 5 + [False])
        self.assertEqual(len(self.node.pokes()), 6)

    def test_with_no_key_it_sends_nothing_and_raises_the_lag(self):
        p = self.watch(wallet=None)
        self.node.last_poke = 95
        self.assertEqual(self.ticks(p, 100), [False])
        self.assertEqual(self.node.raw, [])
        self.assertEqual([a["role"] for a in self.alarm_of("book_poke_lagging")], ["watch"])

    def test_it_builds_on_its_own_nodes_and_the_factorys_book(self):
        args = bw.parser().parse_args(["--deployment", "sim1", "--rpc", "http://127.0.0.1:9", "--wallet", "watcher"])
        node = Node()
        with mock.patch.object(bk, "book_of", return_value=BOOK) as book_of, \
                mock.patch.object(bk, "precompile_gas_of", return_value=30_000):
            p = bw.build(args, {"SimPoolFactory": "0x" + "33" * 20}, wallet=OTHER, alarm=mock.Mock(),
                         nodes=bk.Nodes(node))
        self.assertEqual(book_of.call_args.kwargs["call"].__self__.first, node)
        self.assertEqual((p.takeover, p.role, [l.name for l in p.lanes]), (2, "watch", ["watcher"]))
        self.assertEqual(p.gas(1), bk.poke_gas(1, 30_000))

    def test_it_refuses_to_send_on_mainnet_without_the_label_and_runs_with_no_node_of_its_own(self):
        from ops import sim_network as sn
        net = mock.Mock()
        net.name = "mainnet"
        with mock.patch.object(sn, "selected", return_value=net):
            with self.assertRaisesRegex(SystemExit, "--confirm-mainnet sim1"):
                bw.main(["--deployment", "sim1", "--rpc", "http://127.0.0.1:9", "--wallet", "w", "--network", "mainnet"])
        with self.assertRaisesRegex(SystemExit, "own node"):
            bw.main(["--deployment", "sim1", "--rpc", ""])


class TheUnit(unittest.TestCase):
    def test_the_watch_has_a_unit_of_its_own_with_no_key_of_the_keepers(self):
        host = pathlib.Path(__file__).resolve().parents[1] / "host"
        unit = (host / "colosseum-book-watch@.service").read_text()
        self.assertIn("-m ops.book_watch --deployment ${COLOSSEUM_DEPLOYMENT}", unit)
        self.assertIn("EnvironmentFile=/etc/colosseum/book-watch-%i.env", unit)
        self.assertIn("InaccessiblePaths=", unit)
        self.assertIn("DynamicUser=yes", unit, "a user of its own: not the keepers', who could read its environment")
        self.assertNotIn("User=colosseum-keeper", unit)
        self.assertIn("Environment=COLOSSEUM_KEY_DIR=%d", unit)
        self.assertNotIn("keeper-%i.env", unit)
        self.assertIn("colosseum-book-watch@.service", (host / "install.sh").read_text())



if __name__ == "__main__":
    unittest.main()
