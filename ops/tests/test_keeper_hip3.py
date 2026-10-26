"""The keeper on every perp dex it is told to read (HIP-3, part 3): the perps by the number an order
uses for them, an account's positions and orders on every dex in one reading, the stop check and
the stop's cancel and forbidden-asset lists across dexes, and a HIP-3 position still open when the
pool's default says flat over the weekend."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from ops import keeper
from ops.tests.test_keeper import CHALLENGE_A, POOL_A, KeeperTest, challenge_log
from ops.tests import test_keeper_protect as protect_tests

ACCOUNT = "0x00000000000000000000000000000000000000a2"
XYZ = 65
XYZ100 = 750000


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class Venue:
    """Hyperliquid's info API for one account on two dexes: Hyperliquid's own and xyz (index 65)."""

    def __init__(self):
        self.bodies = []
        self.positions = {"": [("BTC", "0.001")], "xyz": [("xyz:XYZ100", "0.0005")]}
        self.orders = {"": [], "xyz": []}

    rules = (500, 1000, 300, (3, XYZ100))
    # What check_node reads at the keeper's start (audit M-1, cp#152).
    RPC_URL = "http://fake-node/evm"
    CHAIN_ID = 998

    def call_view(self, to, signature, types, args, out):
        assert signature == "rules()", signature
        return (self.rules,)

    def info_post(self, body):
        self.bodies.append(body)
        dex = body.get("dex", "")
        if body["type"] == "perpDexs":
            return [None] + [{"name": f"d{i}"} for i in range(1, XYZ)] + [{"name": "xyz"}]
        if body["type"] == "meta":
            names = ("xyz:XYZ100", "xyz:TSLA", "xyz:NVDA", "xyz:GOLD") if dex == "xyz" else ("SOL", "APT", "ATOM", "BTC", "ETH")
            return {"universe": [{"name": n} for n in names]}
        if body["type"] == "clearinghouseState":
            return {"marginSummary": {"accountValue": "1"},
                    "assetPositions": [{"position": {"coin": c, "szi": s}} for c, s in self.positions[dex]]}
        if body["type"] == "frontendOpenOrders":
            return list(self.orders[dex])
        raise AssertionError(body)


def stop(coin: str, oid: int, side: str = "A") -> dict:
    return {"coin": coin, "oid": oid, "side": side, "isTrigger": True, "reduceOnly": True,
            "orderType": "Stop Market", "isPositionTpsl": True, "sz": "0", "triggerPx": "1"}


class OnEveryDex(unittest.TestCase):
    def setUp(self):
        self.venue = Venue()
        for target, value in ((keeper, "c"),):
            patcher = mock.patch.object(target, value, self.venue)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("PERP_DEXES", ("xyz",)), ("_dex_index", {}), ("_perp_names", {})):
            patcher = mock.patch.object(keeper, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = mock.patch.object(keeper, "log").start()
        self.addCleanup(mock.patch.stopall)

    def logged(self, event):
        return [call.kwargs for call in self.log.call_args_list if call.args and call.args[0] == event]

    def test_every_dexs_perps_go_by_the_number_an_order_uses(self):
        names = keeper.perp_index_by_name()
        self.assertEqual((names["BTC"], names["SOL"], names["xyz:XYZ100"], names["xyz:GOLD"]), (3, 0, XYZ100, 750003))
        keeper.perp_index_by_name()
        self.assertEqual([b["type"] for b in self.venue.bodies].count("perpDexs"), 1, "the dex list is read once")

    def test_a_dex_left_out_of_the_setting_is_read_from_the_accounts_rules(self):
        # AUDIT В4-1: COLOSSEUM_PERP_DEXES forgot xyz, the account's rules name xyz:XYZ100 (750000).
        self.venue.orders["xyz"] = [stop("xyz:XYZ100", 77)]
        with mock.patch.object(keeper, "PERP_DEXES", ()), mock.patch.object(keeper, "_names_by_dex", {}):
            names = keeper.perp_index_by_name()
            self.assertNotIn("xyz:XYZ100", names, "the pass's names know only the configured dexes")
            book = keeper.read_book(ACCOUNT)
            cancels, extra = keeper.stop_inputs(ACCOUNT, {3, XYZ100}, names, book)
        self.assertIn("xyz:XYZ100", [p["position"]["coin"] for p in book[0]["assetPositions"]])
        self.assertEqual(cancels, [(XYZ100, 77)], "the resting order on xyz is cancelled at the stop and the settle")
        self.assertEqual(extra, [])

    def test_a_perp_listed_since_on_an_unconfigured_dex_is_named_on_the_next_read(self):
        with mock.patch.object(keeper, "PERP_DEXES", ()), mock.patch.object(keeper, "_names_by_dex", {}):
            names = keeper.perp_index_by_name()
            keeper.stop_inputs(ACCOUNT, {3, XYZ100}, names, keeper.read_book(ACCOUNT))  # xyz's names kept
            answer = self.venue.info_post

            def listed_since(body):
                if body["type"] == "meta" and body.get("dex") == "xyz":
                    return {"universe": [{"name": n} for n in ("xyz:XYZ100", "xyz:TSLA", "xyz:NVDA", "xyz:GOLD", "xyz:NEW")]}
                return answer(body)
            with mock.patch.object(self.venue, "info_post", listed_since):
                self.venue.orders["xyz"] = [stop("xyz:NEW", 91)]
                cancels, _ = keeper.stop_inputs(ACCOUNT, {3, XYZ100}, names, keeper.read_book(ACCOUNT))
        self.assertEqual(cancels, [(750004, 91)])

    def test_names_that_cannot_be_read_never_stop_a_stop_and_leave_the_check_open(self):
        self.venue.orders[""] = [stop("BTC", 5)]
        answer = self.venue.info_post

        def no_xyz_meta(body):
            if body["type"] == "meta" and body.get("dex") == "xyz":
                raise RuntimeError("429 Too Many Requests")
            return answer(body)
        with mock.patch.object(keeper, "PERP_DEXES", ()), mock.patch.object(keeper, "_names_by_dex", {}), \
                mock.patch.object(self.venue, "info_post", no_xyz_meta):
            names = keeper.perp_index_by_name()
            book = keeper.read_book(ACCOUNT)
            cancels, extra = keeper.stop_inputs(ACCOUNT, {3, XYZ100}, names, book)
            self.assertEqual(cancels, [(3, 5)], "the stop goes with what is known")
            self.assertEqual(len(self.logged("meta_read_failed")), 1)
            with self.assertRaises(RuntimeError):
                keeper.stop_inputs(ACCOUNT, {3, XYZ100}, names, book, strict=True)
            with mock.patch.object(keeper, "view", lambda *a, **k: 0):
                self.assertEqual(keeper.whats_broken(ACCOUNT, names), (0, None), "the pass stays open")

    def test_a_failed_refresh_keeps_the_names_already_read(self):
        with mock.patch.object(keeper, "PERP_DEXES", ()), mock.patch.object(keeper, "_names_by_dex", {}):
            names = keeper.perp_index_by_name()
            keeper.stop_inputs(ACCOUNT, {3, XYZ100}, names, keeper.read_book(ACCOUNT))  # xyz's names kept
            answer = self.venue.info_post

            def no_xyz_meta(body):
                if body["type"] == "meta" and body.get("dex") == "xyz":
                    raise RuntimeError("429 Too Many Requests")
                return answer(body)
            with mock.patch.object(self.venue, "info_post", no_xyz_meta):
                self.venue.orders["xyz"] = [stop("xyz:XYZ100", 77), stop("xyz:NEW", 91)]
                cancels, _ = keeper.stop_inputs(ACCOUNT, {3, XYZ100}, names, keeper.read_book(ACCOUNT))
        self.assertEqual(cancels, [(XYZ100, 77)], "the known perp's order still goes on the cancel list")
        self.assertEqual(len(self.logged("meta_read_failed")), 1)

    def test_hyperliquids_own_dex_alone_is_read_as_before(self):
        self.venue.rules = (500, 1000, 300, (3, 4))
        with mock.patch.object(keeper, "PERP_DEXES", ()):
            names = keeper.perp_index_by_name()
            keeper.read_book(ACCOUNT)
        self.assertNotIn("xyz:XYZ100", names)
        self.assertEqual(self.venue.bodies, [{"type": "meta"}, {"type": "clearinghouseState", "user": ACCOUNT},
                                             {"type": "frontendOpenOrders", "user": ACCOUNT}])

    def test_the_account_is_read_on_every_dex_in_one_shape(self):
        self.venue.orders["xyz"] = [stop("xyz:XYZ100", 77)]
        state, orders = keeper.read_book(ACCOUNT)
        self.assertEqual([p["position"]["coin"] for p in state["assetPositions"]], ["BTC", "xyz:XYZ100"])
        self.assertEqual([o["oid"] for o in orders], [77])
        self.assertIn({"type": "clearinghouseState", "user": ACCOUNT, "dex": "xyz"}, self.venue.bodies)
        self.assertIn({"type": "frontendOpenOrders", "user": ACCOUNT, "dex": "xyz"}, self.venue.bodies)

    def test_a_dex_answered_in_the_wrong_shape_is_a_failed_read_not_an_empty_dex(self):
        answer = self.venue.info_post

        def broken(body):
            out = answer(body)
            if body.get("dex") == "xyz" and body["type"] == "clearinghouseState":
                return {"marginSummary": {"accountValue": "1"}}  # no assetPositions
            return out
        with mock.patch.object(self.venue, "info_post", broken):
            with self.assertRaises(ValueError):
                keeper.read_book(ACCOUNT)
            self.assertIsNone(keeper.venue_book(ACCOUNT))
        self.assertEqual(len(self.logged("venue_read_failed")), 1)
        with mock.patch.object(self.venue, "info_post", lambda body: {"oops": 1} if body["type"] == "frontendOpenOrders" else answer(body)):
            self.assertIsNone(keeper.venue_book(ACCOUNT))

    def test_a_hip3_position_without_its_stop_is_called_out_and_with_it_is_not(self):
        self.venue.orders[""] = [stop("BTC", 5)]
        self.assertEqual(keeper.unprotected(ACCOUNT), [{"coin": "xyz:XYZ100", "size": "0.0005"}])
        self.venue.orders["xyz"] = [stop("xyz:XYZ100", 77)]
        self.assertEqual(keeper.unprotected(ACCOUNT), [])

    def test_a_stop_cancels_and_names_hip3_perps_by_their_order_number(self):
        names = keeper.perp_index_by_name()
        self.venue.orders["xyz"] = [stop("xyz:XYZ100", 77)]
        cancels, extra = keeper.stop_inputs(ACCOUNT, {3}, names, keeper.read_book(ACCOUNT))
        self.assertEqual(cancels, [(XYZ100, 77)])
        self.assertEqual(extra, [XYZ100], "a perp outside the rules is named by the number the contract is given")

    def test_a_hip3_position_open_over_the_weekend_is_said_and_one_of_hyperliquids_own_is_not(self):
        book = keeper.read_book(ACCOUNT)
        keeper.check_protection(ACCOUNT, book, utc(2026, 10, 23, 20, 29))  # Friday 16:29 New York
        self.assertEqual(self.logged("hip3_open_over_weekend"), [])
        keeper.check_protection(ACCOUNT, book, utc(2026, 10, 23, 20, 30))  # Friday 16:30
        keeper.check_protection(ACCOUNT, book, utc(2026, 11, 8, 23, 4))    # Sunday 18:04, after the clocks changed
        keeper.check_protection(ACCOUNT, book, utc(2026, 11, 8, 23, 5))    # Sunday 18:05: open again
        self.assertEqual(self.logged("hip3_open_over_weekend"),
                         [{"account": ACCOUNT, "coin": "xyz:XYZ100", "size": "0.0005"}] * 2)

    def run_main(self, argv, env=None):
        """keeper.main() once, with the chain, the deployment and the pass stood in for."""
        class OnePass:
            def __init__(self, *args, **kwargs):
                self.next_block = 0

            def one_pass(self):
                pass
        with mock.patch("sys.argv", ["keeper", "--deployment", "x", "--once", "--dry-run", *argv]), \
                mock.patch.dict("os.environ", env or {}, clear=False), \
                mock.patch.object(keeper.c, "assert_testnet", lambda: None, create=True), \
                mock.patch.object(keeper.deployments, "load", lambda label: {"PoolFactory": "0x" + "f1" * 20}), \
                mock.patch.object(keeper, "deployment_block", lambda record: 1), \
                mock.patch.object(keeper, "Keeper", OnePass):
            self.assertEqual(keeper.main(), 0)
        return keeper.PERP_DEXES

    def test_the_keeper_is_told_its_dexes_on_its_command_line_or_by_its_setting(self):
        self.assertEqual(self.run_main(["--perp-dexes", "xyz"]), ("xyz",))
        self.assertEqual(self.run_main(["--perp-dexes", "xyz, xyz,flx,xyz"]), ("xyz", "flx"), "each dex once")
        self.assertEqual(self.run_main([], {"COLOSSEUM_PERP_DEXES": "xyz, flx"}), ("xyz", "flx"))
        self.assertEqual(self.run_main([], {"COLOSSEUM_PERP_DEXES": ""}), ())



class WeekendInAPass(KeeperTest):
    """The alarm in a whole pass of the keeper, for an active challenge and for a funded pool. The
    protection check's set-up and its active challenge, without running that class's own tests again."""

    def setUp(self):
        super().setUp()
        self.chain = protect_tests.Chain()
        for patcher in (mock.patch.object(keeper, "c", self.chain),
                        mock.patch.object(keeper, "log", side_effect=lambda event, **f: self.logged.append((event, f)))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logged = []

    active_challenge = protect_tests.ProtectionCheck.active_challenge

    def weekend_logged(self):
        return [f for event, f in self.logged if event == "hip3_open_over_weekend"]

    def test_a_challenges_hip3_position_is_said_on_a_saturday_and_not_on_a_wednesday(self):
        # A deadline still ahead in 2026: past it, the pass takes the expiry branch and checks no stop.
        self.active_challenge([("xyz:XYZ100", "0.0005")], [protect_tests.stop_order("xyz:XYZ100")], deadline=int(utc(2027, 1, 1)))
        self.at(utc(2026, 10, 21, 14, 0))  # Wednesday
        self.make().one_pass()
        self.assertEqual(self.weekend_logged(), [])
        self.at(utc(2026, 10, 24, 12, 0))  # Saturday
        self.make().one_pass()
        self.assertEqual(self.weekend_logged(), [{"account": CHALLENGE_A, "coin": "xyz:XYZ100", "size": "0.0005"}])

    def test_a_funded_pools_hip3_position_is_said_on_a_saturday(self):
        self.chain.add_pool(POOL_A, stage=keeper.FUNDED)
        self.chain.logs.append(challenge_log(10, POOL_A))
        self.chain.sizes[POOL_A.lower()] = [("xyz:XYZ100", "-0.0005")]
        self.at(utc(2026, 10, 24, 12, 0))
        self.make().one_pass()
        self.assertEqual(self.weekend_logged(), [{"account": POOL_A, "coin": "xyz:XYZ100", "size": "-0.0005"}])


if __name__ == "__main__":
    unittest.main()
