"""HIP-3 in the gateway, part 1: the perps of more than one dex, numbered the way Hyperliquid numbers
them, each with what its dex says about it, and a market the pool may not open on refused before
anything is signed.

The fixtures follow what Hyperliquid's testnet answered on 19 and 21 Oct 2026: `perpDexs` lists
the first dex as null and xyz at index 65; xyz's `meta` has XYZ100 at 0 and GOLD at 3 (cross margin
allowed), CL at 29 ("noCross" on testnet), BRENTOIL at 49 (delisted) and SP500 at 52 (delisted,
"strictIsolated")."""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

import eth_abi

from gateway import assets, chain, demo_signer, hl, protect, server
from gateway.checks import GatewayError, check_market
from gateway.protect import LONG, Book, Market, Protective, hip3_closed, merge_books, parse_markets
from gateway.assets import weekend_close_due
from gateway.tests.test_gateway import ACCOUNT, NOW
from gateway.tests.test_protect import BTC, MARKETS, FlowBase, resting

XYZ = 65


def perp_dexs() -> list:
    return [None] + [{"name": f"d{i}", "fullName": "", "deployer": "0x0"} for i in range(1, XYZ)] + [
        {"name": "xyz", "fullName": "XYZ", "deployer": "0x88806a71d74ad0a510b350545c9ae490912f0888"}]


def xyz_meta_and_ctxs() -> list:
    universe = [{"name": f"xyz:F{i}", "szDecimals": 2, "maxLeverage": 10} for i in range(53)]
    universe[0] = {"name": "xyz:XYZ100", "szDecimals": 4, "maxLeverage": 30, "marginTableId": 30}
    universe[3] = {"name": "xyz:GOLD", "szDecimals": 4, "maxLeverage": 25, "marginTableId": 25}
    universe[29] = {"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20, "marginMode": "noCross"}
    universe[49] = {"name": "xyz:BRENTOIL", "szDecimals": 2, "maxLeverage": 20, "isDelisted": True}
    universe[52] = {"name": "xyz:SP500", "szDecimals": 3, "maxLeverage": 50, "marginMode": "strictIsolated",
                    "isDelisted": True}
    ctxs = [{"markPx": "100.0"} for _ in universe]
    ctxs[0], ctxs[3] = {"markPx": "29000.0"}, {"markPx": "4421.1"}
    return [{"universe": universe}, ctxs]


def main_meta_and_ctxs() -> list:
    universe = [{"name": "SOL", "szDecimals": 2, "maxLeverage": 20}, {"name": "APT", "szDecimals": 2, "maxLeverage": 10},
                {"name": "ATOM", "szDecimals": 2, "maxLeverage": 10, "onlyIsolated": True},
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]
    return [{"universe": universe}, [{"markPx": "150"}, {"markPx": "5"}, {"markPx": "4"}, {"markPx": "60000"}]]


class Numbering(unittest.TestCase):
    def test_an_order_numbers_a_hip3_perp_100000_above_its_dex_and_index_and_a_precompile_100000_below(self):
        self.assertEqual(assets.order_asset(0, 3), 3, "Hyperliquid's own dex keeps the plain index")
        self.assertEqual(assets.order_asset(XYZ, 3), 750003, "xyz:GOLD on testnet")
        self.assertEqual(assets.order_asset(1, 52), 110052, "xyz:SP500 on mainnet, where xyz is dex 1")
        self.assertEqual(assets.precompile_index(750003), 650003, "measured: markPx(650003) is xyz:GOLD's mark")
        self.assertEqual(assets.precompile_index(110052), 10052)
        self.assertEqual(assets.precompile_index(3), 3)
        self.assertEqual(assets.dex_index_of(750029), XYZ)
        self.assertEqual(assets.dex_index_of(4), 0)
        for bad in ((0, 10_000), (XYZ, -1), (-1, 0)):
            with self.assertRaises(ValueError, msg=str(bad)):
                assets.order_asset(*bad)

    def test_perp_dexs_name_each_dex_by_its_index_and_the_first_is_hyperliquids_own(self):
        names = assets.parse_perp_dexs(perp_dexs())
        self.assertEqual((names[assets.MAIN_DEX], names["xyz"], len(names)), (0, XYZ, XYZ + 1))
        for bad in ({}, [], [{"name": "xyz"}], [None, {"fullName": "no name"}], [None, {"name": "a"}, {"name": "a"}]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                assets.parse_perp_dexs(bad)


class Markets(unittest.TestCase):
    def test_a_hip3_dex_is_keyed_by_order_number_with_delisting_and_margin_mode(self):
        m = parse_markets(xyz_meta_and_ctxs(), XYZ)
        self.assertEqual(m[750003], Market("xyz:GOLD", Decimal("4421.1"), 4, max_leverage=25))
        self.assertEqual((m[750000].coin, m[750000].margin_mode, m[750000].delisted), ("xyz:XYZ100", None, False))
        self.assertEqual((m[750029].coin, m[750029].margin_mode), ("xyz:CL", "noCross"))
        self.assertEqual((m[750049].coin, m[750049].delisted), ("xyz:BRENTOIL", True))
        self.assertEqual((m[750052].coin, m[750052].delisted, m[750052].margin_mode), ("xyz:SP500", True, "strictIsolated"))
        self.assertNotIn(3, m, "no plain index from a HIP-3 dex")

    def test_hyperliquids_own_dex_keeps_plain_numbers_and_reads_the_older_isolated_flag(self):
        m = parse_markets(main_meta_and_ctxs())
        self.assertEqual((m[BTC].coin, m[BTC].mark, m[BTC].margin_mode, m[BTC].max_leverage), ("BTC", Decimal("60000"), None, 40))
        self.assertEqual(m[2].margin_mode, "noCross", "onlyIsolated is the older field for the same thing")


class Reads(unittest.TestCase):
    def answers(self):
        self.bodies = []

        def info(body, timeout):
            self.bodies.append(body)
            if body["type"] == "perpDexs":
                return perp_dexs()
            if body["type"] == "metaAndAssetCtxs":
                return xyz_meta_and_ctxs() if body.get("dex") == "xyz" else main_meta_and_ctxs()
            if body["type"] == "allMids":
                return {"xyz:GOLD": "4421.1"} if body.get("dex") == "xyz" else {"BTC": "60000"}
            raise AssertionError(body)
        return mock.patch.object(hl, "_info", info)

    def test_each_dex_is_one_request_with_its_name_and_the_dex_list_is_read_once(self):
        with self.answers():
            venue = hl.Venue(dexes=(hl.MAIN_DEX, "xyz"))
            first, second = venue.markets(), venue.markets()
        self.assertEqual(first, second)
        self.assertEqual((first[BTC].coin, first[750003].coin), ("BTC", "xyz:GOLD"))
        self.assertEqual(self.bodies, [{"type": "metaAndAssetCtxs"}, {"type": "perpDexs"},
                                       {"type": "metaAndAssetCtxs", "dex": "xyz"},
                                       {"type": "metaAndAssetCtxs"}, {"type": "metaAndAssetCtxs", "dex": "xyz"}])

    def test_a_dex_named_twice_is_read_once_and_its_equity_counted_once(self):
        self.assertEqual(hl.Venue(dexes=(hl.MAIN_DEX, "xyz", "xyz")).dexes, (hl.MAIN_DEX, "xyz"))

    def test_by_default_only_hyperliquids_own_dex_is_read(self):
        with self.answers():
            markets = hl.Venue().markets()
        self.assertEqual(self.bodies, [{"type": "metaAndAssetCtxs"}])
        self.assertEqual(set(markets), {0, 1, 2, 3})

    def test_a_dex_hyperliquid_does_not_list_is_an_error_after_one_fresh_read(self):
        with self.answers():
            venue = hl.Venue(dexes=(hl.MAIN_DEX, "nope"))
            with self.assertRaises(GatewayError) as e:
                venue.markets()
        self.assertEqual(e.exception.code, "no_such_dex")
        self.assertEqual([b["type"] for b in self.bodies], ["metaAndAssetCtxs", "perpDexs"])

    def test_mids_are_asked_of_one_dex(self):
        with self.answers():
            self.assertEqual(hl.mids(), {"BTC": "60000"})
            self.assertEqual(hl.mids(dex="xyz"), {"xyz:GOLD": "4421.1"})
        self.assertEqual(self.bodies, [{"type": "allMids"}, {"type": "allMids", "dex": "xyz"}])

    def test_a_coin_is_named_from_its_dexs_meta_once(self):
        hl._COINS.clear()
        self.addCleanup(hl._COINS.clear)
        bodies = []

        def answer(body, timeout):
            bodies.append(body)
            if body["type"] == "perpDexs":
                return perp_dexs()
            if body["type"] == "meta":
                return xyz_meta_and_ctxs()[0] if body.get("dex") == "xyz" else main_meta_and_ctxs()[0]
            raise AssertionError(body)
        with mock.patch.object(hl, "_info", answer):
            self.assertEqual(hl.coin_of(750003), "xyz:GOLD")
            self.assertEqual(hl.coin_of(750029), "xyz:CL")
            self.assertEqual(hl.coin_of(BTC), "BTC")
            with self.assertRaises(GatewayError):
                hl.coin_of(759_999)
        self.assertEqual(bodies[:4], [{"type": "perpDexs"}, {"type": "meta", "dex": "xyz"}, {"type": "meta"},
                                      {"type": "perpDexs"}])

    def test_a_dexs_names_go_in_whole_or_not_at_all(self):
        hl._COINS.clear()
        self.addCleanup(hl._COINS.clear)
        broken = {"universe": [{"name": "xyz:XYZ100"}, {"szDecimals": 4}]}  # the second entry has no name
        with mock.patch.object(hl, "_info", lambda body, timeout: perp_dexs() if body["type"] == "perpDexs" else broken):
            with self.assertRaises(KeyError):
                hl.coin_of(750000)
        self.assertEqual(hl._COINS, {}, "nothing of a dex whose meta could not be read is kept")

    def test_a_hip3_perps_mid_is_its_dexs(self):
        asked = []
        with mock.patch.object(hl, "coin_of", lambda asset, timeout=5.0: {750003: "xyz:GOLD"}[asset]), \
                mock.patch.object(hl, "mids", lambda timeout=5.0, dex=hl.MAIN_DEX: asked.append(dex) or {"xyz:GOLD": "4421.1"}):
            self.assertEqual(demo_signer.market_mid(750003), Decimal("4421.1"))
        self.assertEqual(asked, ["xyz"])

    def test_the_gateway_reads_the_dexes_it_is_told_to_besides_hyperliquids_own(self):
        body = inspect.getsource(server.main)
        self.assertIn('os.environ.get("GATEWAY_PERP_DEXES", "")', body)
        self.assertIn("venue=hl.Venue(dexes=(hl.MAIN_DEX, *dexes))", body)


class MarketCheck(unittest.TestCase):
    def test_a_perp_that_is_delisted_or_not_cross_margin_is_refused(self):
        m = parse_markets(xyz_meta_and_ctxs(), XYZ)
        check_market(750003, m)  # GOLD: cross margin allowed, listed
        check_market(750000, m)  # XYZ100
        cases = {750049: (403, "market_delisted"), 750052: (403, "market_delisted"), 750029: (403, "margin_mode"),
                 999_999: (502, "no_market_price")}
        for asset, want in cases.items():
            with self.assertRaises(GatewayError, msg=str(asset)) as e:
                check_market(asset, m)
            self.assertEqual((e.exception.status, e.exception.code), want, asset)

    def test_any_margin_mode_at_all_is_refused_a_new_one_too(self):
        for mode in ("noCross", "strictIsolated", "isolatedSomeNewWay"):
            with self.assertRaises(GatewayError, msg=mode) as e:
                check_market(1, {1: Market("X", Decimal(1), 2, margin_mode=mode)})
            self.assertEqual(e.exception.code, "margin_mode")
            self.assertIn(mode, e.exception.detail)


class Refusals(FlowBase):
    """Before anything is signed or placed: no stop, no take, no order."""

    def test_an_order_on_a_delisted_perp_sends_nothing(self):
        self.x.markets_now[BTC] = Market("BTC", Decimal("60000"), 5, delisted=True)
        status, out = self.send()
        self.assertEqual((status, out["code"], out["sent"]), (403, "market_delisted", False))
        self.assertEqual(self.x.sent, [])

    def test_an_order_on_a_perp_without_cross_margin_sends_nothing(self):
        self.x.markets_now[BTC] = Market("BTC", Decimal("60000"), 5, margin_mode="noCross")
        status, out = self.send()
        self.assertEqual((status, out["code"], out["sent"]), (403, "margin_mode", False))
        self.assertEqual(self.x.sent, [])

    def test_closing_a_position_on_a_delisted_perp_still_goes(self):
        # Delisting must not trap a position the account already holds.
        self.x.positions = {BTC: Decimal("0.005")}
        self.x.markets_now[BTC] = Market("BTC", Decimal("60000"), 5, delisted=True)
        status, out = self.send(isBuy=False, reduceOnly=True, limitPx="59000")
        self.assertEqual((status, out["status"]), (200, "submitted"))

    def test_an_order_on_an_ordinary_perp_is_unchanged(self):
        self.assertEqual(MARKETS[BTC].margin_mode, None)
        status, out = self.send()
        self.assertEqual((status, out["status"]), (200, "submitted"))



# ── part 2: the account on every dex, the name, the closed hours ────────────────────────────

XYZ100 = 750000


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class Books(unittest.TestCase):
    def test_books_on_two_dexes_are_one_account_equity_summed(self):
        main = Book(equity=Decimal("50"), positions={BTC: Decimal("0.001")}, marks={BTC: Decimal("60000")})
        xyz = Book(equity=Decimal("15"), positions={XYZ100: Decimal("0.0005")}, marks={XYZ100: Decimal("29000")},
                   protective=[Protective(9, XYZ100, LONG, "sl", Decimal("28000"))],
                   opening={XYZ100: {LONG: Decimal("0.0001"), "short": Decimal(0)}})
        book = merge_books([main, xyz])
        self.assertEqual(book.equity, Decimal("65"))
        self.assertEqual(book.positions, {BTC: Decimal("0.001"), XYZ100: Decimal("0.0005")})
        self.assertEqual(book.marks, {BTC: Decimal("60000"), XYZ100: Decimal("29000")})
        self.assertEqual([p.asset for p in book.protective], [XYZ100])
        self.assertEqual(set(book.opening), {XYZ100})
        with self.assertRaises(ValueError):
            merge_books([])

    def test_merged_books_keep_every_dex_resting_orders(self):
        a = Book(equity=Decimal(50), notional=Decimal(0), resting=[(3, 1)])
        b = Book(equity=Decimal(15), notional=Decimal(0), resting=[(XYZ100, 2)])
        self.assertEqual(merge_books([a, b]).resting, [(3, 1), (XYZ100, 2)])

    def test_a_notional_one_dex_left_out_is_unknown_for_the_whole_account(self):
        a = Book(equity=Decimal(50), notional=Decimal(60))
        b = Book(equity=Decimal(15), notional=None)
        self.assertEqual(merge_books([a, Book(equity=Decimal(15), notional=Decimal("14.5"))]).notional, Decimal("74.5"))
        self.assertIsNone(merge_books([a, b]).notional)
        self.assertEqual(merge_books([a]).notional, Decimal(60), "one dex alone keeps its own")

    def test_the_venue_reads_the_account_on_each_dex_with_its_name(self):
        bodies = []
        markets = {**parse_markets(main_meta_and_ctxs()), **parse_markets(xyz_meta_and_ctxs(), XYZ)}

        def info(body, timeout):
            bodies.append(body)
            on_xyz = body.get("dex") == "xyz"
            if body["type"] == "clearinghouseState":
                coin, szi, value, equity = ("xyz:XYZ100", "0.0005", "14.5", "15.0") if on_xyz else ("BTC", "0.001", "60", "50.0")
                return {"marginSummary": {"accountValue": equity, "totalNtlPos": value},
                        "assetPositions": [{"position": {"coin": coin, "szi": szi, "positionValue": value}}]}
            if body["type"] == "frontendOpenOrders":
                return [{"coin": "xyz:XYZ100", "side": "A", "isTrigger": True, "reduceOnly": True,
                         "orderType": "Stop Market", "oid": 9, "triggerPx": "28000", "sz": "0"}] if on_xyz else []
            raise AssertionError(body)
        with mock.patch.object(hl, "_info", info):
            book = hl.Venue(dexes=(hl.MAIN_DEX, "xyz")).book("0xabc", markets)
        self.assertEqual(book.equity, Decimal("65.0"))
        # The notional the leverage check counts from, summed over the dexes as the equity is.
        self.assertEqual(book.notional, Decimal("74.5"))
        self.assertEqual(book.positions, {BTC: Decimal("0.001"), XYZ100: Decimal("0.0005")})
        self.assertEqual([(p.asset, p.tpsl) for p in book.protective], [(XYZ100, "sl")])
        self.assertEqual(bodies, [{"type": "clearinghouseState", "user": "0xabc"}, {"type": "frontendOpenOrders", "user": "0xabc"},
                                  {"type": "clearinghouseState", "user": "0xabc", "dex": "xyz"},
                                  {"type": "frontendOpenOrders", "user": "0xabc", "dex": "xyz"}])


class WeekendCancel(unittest.TestCase):
    def test_from_friday_16_to_sunday_18_05_in_new_york_time(self):
        due = assets.weekend_cancel_due
        # EDT before 1 November: Friday 16:00 is 20:00 UTC, Sunday 18:05 is 22:05 UTC.
        self.assertEqual([due(utc(2026, 10, 23, 19, 59)), due(utc(2026, 10, 23, 20, 0))], [False, True])
        self.assertTrue(due(utc(2026, 10, 24, 12, 0)), "Saturday")
        self.assertEqual([due(utc(2026, 10, 25, 22, 4)), due(utc(2026, 10, 25, 22, 5))], [True, False])
        # EST after: an hour later in UTC.
        self.assertEqual([due(utc(2026, 11, 6, 20, 59)), due(utc(2026, 11, 6, 21, 0))], [False, True])
        self.assertEqual([due(utc(2026, 11, 8, 23, 4)), due(utc(2026, 11, 8, 23, 5))], [True, False])
        self.assertFalse(due(utc(2026, 11, 9, 22, 30)), "Monday's daily hour is not the weekend")
        # The same window the gateway opens nothing in.
        for t in (utc(2026, 11, 6, 21, 0), utc(2026, 11, 7, 3, 0), utc(2026, 11, 8, 23, 4)):
            self.assertTrue(hip3_closed(XYZ100, t).startswith("the weekend"))


class WeekendClose(unittest.TestCase):
    def test_the_hour_before_the_friday_close_in_new_york_time(self):
        due = [weekend_close_due(utc(2026, 10, 23, h, m)) for h, m in ((19, 59), (20, 0), (20, 59), (21, 0))]
        self.assertEqual(due, [False, True, True, False])  # EDT: 16:00 is 20:00 UTC
        # After the clocks go back on 1 November, 16:00 in New York is 21:00 UTC.
        self.assertEqual([weekend_close_due(utc(2026, 11, 6, h, 0)) for h in (20, 21)], [False, True])
        self.assertFalse(weekend_close_due(utc(2026, 10, 22, 20, 30)), "a Thursday")


class ClosedHours(unittest.TestCase):
    def test_a_hip3_perp_is_closed_over_the_weekend_and_in_the_daily_hour_in_new_york_time(self):
        # Before 1 Nov 2026, New York is UTC-4: the CTO's hours in UTC.
        open_, weekend, maintenance = None, "the weekend", "the daily maintenance hour"
        cases = [(utc(2026, 10, 23, 19, 59), open_), (utc(2026, 10, 23, 20, 0), weekend),
                 (utc(2026, 10, 25, 22, 4), weekend), (utc(2026, 10, 25, 22, 5), open_),
                 (utc(2026, 10, 26, 20, 59), open_), (utc(2026, 10, 26, 21, 0), maintenance),
                 (utc(2026, 10, 26, 22, 0), open_), (utc(2026, 10, 30, 21, 30), weekend),
                 # After 1 Nov 2026, UTC-5: the same New York hours, an hour later in UTC.
                 (utc(2026, 11, 6, 20, 0), open_), (utc(2026, 11, 6, 21, 0), weekend),
                 (utc(2026, 11, 8, 23, 4), weekend), (utc(2026, 11, 8, 23, 5), open_),
                 (utc(2026, 11, 9, 21, 30), open_), (utc(2026, 11, 9, 22, 30), maintenance)]
        for t, want in cases:
            got = hip3_closed(XYZ100, t)
            self.assertEqual(None if got is None else got.split(":")[0].split(",")[0], want,
                             datetime.fromtimestamp(t, timezone.utc).isoformat())

    def test_hyperliquids_own_perps_are_never_closed(self):
        for t in (utc(2026, 10, 24, 12, 0), utc(2026, 10, 26, 21, 30)):
            self.assertIsNone(hip3_closed(BTC, t))


class Names(unittest.TestCase):
    def test_the_chain_reader_asks_perpassetinfo_with_the_precompiles_number(self):
        reader = chain.JsonRpcReader("http://rpc", "0x" + "11" * 20, "0x" + "22" * 20)
        asked = []

        def rpc(method, params):
            asked.append(params[0]["data"])
            return "0x" + eth_abi.encode(["(string,uint32,uint8,uint8,bool)"], [("xyz:XYZ100", 30, 4, 30, False)]).hex()
        with mock.patch.object(reader, "_rpc", rpc):
            self.assertEqual(reader.perp_name(XYZ100), "xyz:XYZ100")
        self.assertEqual(asked, ["0x" + eth_abi.encode(["uint32"], [650000]).hex()])

    def test_a_perp_hyperevm_does_not_know_is_no_name_and_a_throttled_node_is_not(self):
        reader = chain.JsonRpcReader("http://rpc", "0x" + "11" * 20, "0x" + "22" * 20)
        with mock.patch.object(reader, "_rpc", side_effect=RuntimeError("eth_call: {'code': -32003, 'message': 'EVM error: PrecompileError'}")):
            self.assertIsNone(reader.perp_name(XYZ100))
        for exc in (chain.Throttled("eth_call: rate limited"), RuntimeError("eth_call: execution reverted")):
            with mock.patch.object(reader, "_rpc", side_effect=exc), self.assertRaises(RuntimeError):
                reader.perp_name(XYZ100)


class OnXyz(FlowBase):
    """An order on xyz:XYZ100 (order asset 750000) through the whole flow, with the asset on the
    account's rules and, for the test, on the demo signer's list."""

    def setUp(self):
        super().setUp()
        self.x.markets_now[XYZ100] = Market("xyz:XYZ100", Decimal("29000"), 4, max_leverage=30)
        self.reader.assets = frozenset({3, 4, XYZ100})
        self.reader.names = {**self.reader.names, XYZ100: "xyz:XYZ100"}
        self.now = utc(2026, 10, 21, 14, 0)  # a Wednesday afternoon: open
        self.gw.signer.caps[XYZ100] = Decimal("0.02")  # on the platform's list, as GATEWAY_SIZE_CAPS would put it

    def order(self, **over):
        fields = dict(asset=XYZ100, limitPx="29000", size="0.01", nonce=int(self.now * 1000),
                      expiresAt=int(self.now * 1000) + 30_000)
        return self.send(**{**fields, **over})

    def test_the_stop_and_take_of_an_xyz_order_carry_its_order_number(self):
        status, out = self.order()
        self.assertEqual((status, out["status"]), (200, "submitted"))
        protective, trader = self.x.sent
        self.assertEqual({o["a"] for o in protective["orders"]}, {XYZ100})
        self.assertEqual(protective["grouping"], "positionTpsl")
        self.assertEqual(trader["orders"][0]["a"], XYZ100)
        self.assertEqual(out["protection"][0]["coin"], "xyz:XYZ100")

    def test_a_name_hyperevm_gives_otherwise_is_refused_before_anything_is_placed(self):
        self.reader.names = {**self.reader.names, XYZ100: "rbbt:RAB"}
        status, out = self.order()
        self.assertEqual((status, out["code"], out["sent"]), (403, "market_name_mismatch", False))
        self.assertIn("rbbt:RAB", out["detail"])
        self.assertIn("perpAssetInfo(650000)", out["detail"])
        self.assertEqual(self.x.sent, [])

    def test_the_name_is_asked_once_and_then_trusted(self):
        asked = []
        names = self.reader.names
        self.reader.perp_name = lambda a: asked.append(a) or names.get(a)
        self.order()
        self.now += 60
        self.order()
        self.assertEqual(asked, [XYZ100])

    def test_the_start_checks_the_names_of_every_asset_it_is_given(self):
        self.reader.names = {**self.reader.names, 4: "rbbt:RAB"}
        out = self.gw.protector.check_names({BTC, 4, XYZ100, 999_999})
        self.assertEqual(out[BTC], "ok")
        self.assertEqual(out[XYZ100], "ok")
        self.assertEqual(out[999_999], "no market")
        self.assertIn("rbbt:RAB", out[4])

    def test_nothing_opens_on_xyz_over_the_weekend_and_a_close_still_goes(self):
        self.now = utc(2026, 10, 24, 12, 0)  # Saturday
        status, out = self.order()
        self.assertEqual((status, out["code"], out["sent"]), (409, "market_window", False))
        self.assertEqual(self.x.sent, [])
        self.x.positions = {XYZ100: Decimal("0.01")}
        status, out = self.order(isBuy=False, reduceOnly=True, limitPx="28000")
        self.assertEqual((status, out["status"]), (200, "submitted"))

    def test_a_pool_flat_over_the_weekend_has_its_xyz_positions_closed_from_friday_16_new_york(self):
        self.x.positions = {XYZ100: Decimal("0.01"), BTC: Decimal("0.005")}
        self.assertEqual(self.gw.protector.take_on([self.key.address]), [ACCOUNT])

        def closes():
            return [a["orders"][0] for a in self.x.sent if a.get("grouping") == "na"]
        self.now = utc(2026, 10, 23, 19, 59)  # Friday 15:59 in New York (EDT): not yet
        self.gw.protector.sweep()
        self.assertEqual(closes(), [])
        self.now = utc(2026, 10, 23, 20, 0)  # Friday 16:00
        self.gw.protector.sweep()
        # The xyz position, reduce-only IOC at the market; Hyperliquid's own BTC trades on.
        self.assertEqual([(o["a"], o["b"], o["s"], o["r"], o["t"]) for o in closes()],
                         [(XYZ100, False, "0.01", True, {"limit": {"tif": "Ioc"}})])
        self.x.sent = []
        self.now = utc(2026, 10, 23, 20, 0, 15)  # the next pass in the hour: tried again at once
        self.gw.protector.sweep()
        self.assertEqual(len(closes()), 1)
        # After the hour the position still open is tried again, but at most every five minutes.
        for t, sent in ((utc(2026, 10, 23, 21, 0), 1), (utc(2026, 10, 23, 21, 4), 0), (utc(2026, 10, 23, 21, 5), 1)):
            self.x.sent, self.now = [], t
            self.gw.protector.sweep()
            self.assertEqual(len(closes()), sent, datetime.fromtimestamp(t, timezone.utc).isoformat())

    def test_the_friday_hour_takes_off_resting_xyz_orders_before_the_close(self):
        self.x.positions = {XYZ100: Decimal("0.01")}
        self.x.orders = [resting("xyz:XYZ100", "B", "0.01", 77), resting("BTC", "B", "0.001", 78)]
        self.assertEqual(self.gw.protector.take_on([self.key.address]), [ACCOUNT])
        self.now = utc(2026, 10, 23, 20, 10)  # Friday 16:10 New York
        self.gw.protector.sweep()
        kinds = [(a["type"], a.get("grouping")) for a in self.x.sent]
        cancels = [a for a in self.x.sent if a["type"] == "cancel"]
        self.assertEqual([c["cancels"] for c in cancels], [[{"a": XYZ100, "o": 77}]], "the xyz order only; BTC's stays")
        self.assertLess(kinds.index(("cancel", None)), kinds.index(("order", "na")), "the order goes before the close")
        # After the reopening on Sunday nothing of it is taken off.
        self.x.sent, self.x.orders = [], [resting("xyz:XYZ100", "B", "0.01", 79)]
        self.now = utc(2026, 10, 25, 22, 5)  # Sunday 18:05 New York (EDT)
        self.gw.protector.sweep()
        self.assertEqual([a for a in self.x.sent if a["type"] == "cancel"], [])

    def test_an_order_still_resting_after_the_friday_hour_is_taken_off_until_sunday_18_05(self):
        # The perp trades through the weekend: an order whose cancel did not land in the Friday hour is
        # sent another at every sweep until the reopening, and the position is closed with it.
        self.x.positions = {XYZ100: Decimal("0.01")}
        self.assertEqual(self.gw.protector.take_on([self.key.address]), [ACCOUNT])

        def cancels_at(t, oid):
            self.x.sent, self.x.orders = [], [resting("xyz:XYZ100", "B", "0.01", oid), resting("BTC", "B", "0.001", 90)]
            self.now = t
            self.gw.protector.sweep()
            return [c["cancels"] for c in self.x.sent if c["type"] == "cancel"]

        def closes():
            return [a for a in self.x.sent if a.get("grouping") == "na"]
        # EST after 1 November: Friday 17:00 New York is 22:00 UTC, Sunday 18:05 is 23:05 UTC.
        for t, oid in ((utc(2026, 11, 6, 22, 0), 80), (utc(2026, 11, 7, 12, 0), 81), (utc(2026, 11, 8, 23, 4), 82)):
            with self.subTest(at=datetime.fromtimestamp(t, timezone.utc).isoformat()):
                self.assertEqual(cancels_at(t, oid), [[{"a": XYZ100, "o": oid}]], "the xyz order only; BTC's stays")
                self.assertEqual([o["orders"][0]["a"] for o in closes()], [XYZ100])
        self.assertEqual(cancels_at(utc(2026, 11, 8, 23, 5), 83), [], "Sunday 18:05: trading again")
        self.assertEqual(closes(), [], "and the position is the trader's again")
        self.assertEqual(cancels_at(utc(2026, 11, 9, 22, 30), 84), [], "the daily hour takes nothing off")
        self.assertEqual(cancels_at(utc(2026, 11, 6, 20, 59), 85), [], "Friday 15:59: not yet")

    def test_a_position_a_late_fill_opened_is_closed_on_saturday_and_again_no_sooner_than_five_minutes(self):
        # No position in the Friday hour; on Saturday one is there, from an order that filled before its
        # cancel landed. The pool is flat over the weekend, so it is closed, and tried again every five
        # minutes while it stays (the IOC found no level), its stop and take standing in between.
        self.x.positions = {BTC: Decimal("0.005")}
        self.assertEqual(self.gw.protector.take_on([self.key.address]), [ACCOUNT])

        def closes_at(t):
            self.x.sent, self.now = [], t
            self.gw.protector.sweep()
            return [(a["orders"][0]["a"], a["orders"][0]["r"]) for a in self.x.sent if a.get("grouping") == "na"]
        self.assertEqual(closes_at(utc(2026, 11, 6, 21, 30)), [], "nothing on xyz in the Friday hour")
        self.x.positions = {BTC: Decimal("0.005"), XYZ100: Decimal("0.01")}
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 0)), [(XYZ100, True)])
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 0, 15)), [], "the next pass: not yet")
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 4, 59)), [])
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 5)), [(XYZ100, True)], "five minutes on")
        self.assertEqual(closes_at(utc(2026, 11, 8, 23, 4)), [(XYZ100, True)], "Sunday 18:04")
        # Past the window, and past the five minutes as well, so it is the window that says no.
        self.assertEqual(closes_at(utc(2026, 11, 8, 23, 10)), [], "Sunday 18:10: trading again")
        self.assertEqual(protect.WEEKEND_CLOSE_EVERY_S, 300)

    def test_a_position_opened_again_after_one_was_closed_is_closed_at_once(self):
        # 12:00 the position is closed and the close fills; 12:01 the account is flat on xyz; 12:02 a late
        # fill opens it again. It does not wait for the first one's five minutes.
        self.x.positions = {BTC: Decimal("0.005"), XYZ100: Decimal("0.01")}
        self.gw.protector.take_on([self.key.address])

        def closes_at(t):
            self.x.sent, self.now = [], t
            self.gw.protector.sweep()
            return [a["orders"][0]["a"] for a in self.x.sent if a.get("grouping") == "na"]
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 0)), [XYZ100])
        self.x.positions = {BTC: Decimal("0.005")}
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 1)), [])
        self.x.positions = {BTC: Decimal("0.005"), XYZ100: Decimal("-0.02")}
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 2)), [XYZ100])
        self.assertEqual(closes_at(utc(2026, 11, 7, 12, 3)), [], "and then every five minutes again")

    def test_a_close_the_book_cannot_fill_is_one_log_line_a_try_and_nothing_raised(self):
        # Testnet's empty weekend book: the IOC comes back with the error Hyperliquid gives an order that
        # could not match (6 November 2026: `iocCancelRejected` in historicalOrders). Each try is one log
        # line with the refusal; the sweep goes on and tries again five minutes later.
        self.x.positions = {XYZ100: Decimal("0.01")}
        self.gw.protector.take_on([self.key.address])
        submit = self.gw.submit
        no_match = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"error": "Order could not immediately match against any resting orders. asset=750000"}]}}}
        self.gw.submit = lambda action, nonce, signature: (self.x.sent.append(action) or no_match
                                                           if action.get("grouping") == "na" else submit(action, nonce, signature))
        said = []
        with mock.patch.object(protect, "log_line", side_effect=lambda **kw: said.append(kw)):
            for t in (utc(2026, 11, 7, 12, 0), utc(2026, 11, 7, 12, 2), utc(2026, 11, 7, 12, 5)):
                self.now = t
                self.gw.protector.sweep()
        closed = [kw for kw in said if kw.get("event") == "protect_closed"]
        self.assertEqual(len(closed), 2, "12:00 and 12:05; not 12:02")
        for kw in closed:
            self.assertEqual((kw["asset"], kw["why"], kw["confirmed"]), (XYZ100, "flat_over_weekend", False))
            self.assertIn("could not immediately match", str(kw["refusal"]))

    def test_a_pool_that_holds_over_the_weekend_keeps_its_resting_xyz_orders_on_saturday(self):
        self.x.positions = {XYZ100: Decimal("0.01")}
        self.x.orders = [resting("xyz:XYZ100", "B", "0.01", 86)]
        self.gw.protector.take_on([self.key.address])
        self.gw.protector.hold_over_weekend = frozenset({ACCOUNT.lower()})
        self.reader.pool_of = lambda account: ACCOUNT
        self.now = utc(2026, 11, 7, 12, 0)  # Saturday
        self.gw.protector.sweep()
        self.assertEqual([a for a in self.x.sent if a["type"] == "cancel"], [])

    def test_a_pool_that_holds_over_the_weekend_keeps_its_xyz_positions(self):
        self.x.positions = {XYZ100: Decimal("0.01")}
        self.gw.protector.take_on([self.key.address])
        self.gw.protector.hold_over_weekend = frozenset({ACCOUNT.lower()})
        self.reader.pool_of = lambda account: ACCOUNT
        self.now = utc(2026, 10, 23, 20, 10)
        self.gw.protector.sweep()
        self.assertEqual([a for a in self.x.sent if a.get("grouping") == "na"], [])
        # A challenge of another pool is flat, the default.
        self.gw.protector._pool_of.clear()
        self.reader.pool_of = lambda account: "0x" + "ab" * 20
        self.gw.protector.sweep()
        self.assertEqual(len([a for a in self.x.sent if a.get("grouping") == "na"]), 1)

    def test_the_gateway_reads_the_weekend_setting_at_its_start(self):
        body = inspect.getsource(server.main)
        self.assertIn('os.environ.get("GATEWAY_HOLD_OVER_WEEKEND", "")', body)
        self.assertIn("hold_over_weekend=hold)", body)

    def test_the_gateway_says_the_names_at_its_start(self):
        body = inspect.getsource(server.take_on_at_start)
        self.assertIn("gateway.protector.check_names(", body)
        self.assertIn('log_line(event="market_names"', body)


GOLD = 750003           # xyz:GOLD on testnet: xyz is dex 65 there and GOLD its index 3
GOLD_MAINNET = 110003   # the same market on mainnet, where xyz is dex 1 (perpDexs and meta, read 10 Nov 2026)


class GoldHours(unittest.TestCase):
    """The weekend and the daily hour are a builder dex's, not a list's: they hold for xyz:GOLD on both networks
    as they do for xyz:XYZ100, with no number of its own anywhere in gateway/assets.py."""

    def test_gold_is_closed_and_open_exactly_when_xyz100_is_on_both_networks(self):
        hours = [utc(2026, 11, 6, h, m) for h, m in ((20, 59), (21, 0), (21, 30), (22, 5))]  # Friday, New York EST
        hours += [utc(2026, 11, 8, h, m) for h, m in ((23, 4), (23, 5))]                    # Sunday 18:04, 18:05
        hours += [utc(2026, 11, 9, h, m) for h, m in ((21, 59), (22, 0), (22, 59), (23, 0))]  # Monday's daily hour
        for t in hours:
            want = hip3_closed(XYZ100, t)
            for gold in (GOLD, GOLD_MAINNET):
                with self.subTest(at=datetime.fromtimestamp(t, timezone.utc).isoformat(), asset=gold):
                    self.assertEqual(hip3_closed(gold, t), want)

    def test_gold_is_closed_before_every_weekend_by_name_on_both_networks(self):
        self.assertIn("xyz:GOLD", assets.ALWAYS_FLAT_OVER_WEEKEND)
        self.assertEqual(assets.order_asset(65, 3), GOLD)
        self.assertEqual(assets.order_asset(1, 3), GOLD_MAINNET)


class OnGold(FlowBase):
    """xyz:GOLD through the flow: the weekend's close on every pool, and the daily hour, which opens nothing and
    closes nothing."""

    def setUp(self):
        super().setUp()
        self.x.markets_now[GOLD] = Market("xyz:GOLD", Decimal("4100"), 4, max_leverage=25)
        self.x.markets_now[XYZ100] = Market("xyz:XYZ100", Decimal("29000"), 4, max_leverage=30)
        self.reader.assets = frozenset({3, 4, XYZ100, GOLD})
        self.reader.names = {**self.reader.names, XYZ100: "xyz:XYZ100", GOLD: "xyz:GOLD"}
        self.gw.signer.caps[GOLD] = Decimal("0.1")
        self.gw.signer.caps[XYZ100] = Decimal("0.02")
        self.now = utc(2026, 11, 4, 15, 0)  # a Wednesday, 10:00 in New York: open

    def order(self, **over):
        fields = dict(asset=GOLD, limitPx="4100", size="0.05", nonce=int(self.now * 1000),
                      expiresAt=int(self.now * 1000) + 30_000)
        return self.send(**{**fields, **over})

    def closes(self):
        return [a["orders"][0] for a in self.x.sent if a.get("grouping") == "na"]

    def test_a_gold_order_goes_with_its_stop_and_take_on_an_open_day(self):
        status, out = self.order()
        self.assertEqual((status, out["status"]), (200, "submitted"))
        self.assertEqual(out["protection"][0]["coin"], "xyz:GOLD")

    def test_the_daily_hour_opens_nothing_on_gold_and_leaves_an_open_position_as_it_is(self):
        self.x.positions = {GOLD: Decimal("0.05")}
        self.assertEqual(self.gw.protector.take_on([self.key.address]), [ACCOUNT])
        self.now = utc(2026, 11, 9, 22, 30)  # Monday 17:30 in New York (EST)
        status, out = self.order()
        self.assertEqual((status, out["code"], out["sent"]), (409, "market_window", False))
        self.x.sent = []
        self.gw.protector.sweep()
        self.assertEqual(self.closes(), [], "the daily hour closes nothing: the position and its stop stay")

    def test_a_pool_that_holds_over_the_weekend_still_has_its_gold_closed_and_keeps_its_other_xyz(self):
        self.x.positions = {GOLD: Decimal("0.05"), XYZ100: Decimal("0.01")}
        self.x.orders = [resting("xyz:GOLD", "B", "0.05", 81), resting("xyz:XYZ100", "B", "0.01", 82)]
        self.gw.protector.take_on([self.key.address])
        self.gw.protector.hold_over_weekend = frozenset({ACCOUNT.lower()})
        self.reader.pool_of = lambda account: ACCOUNT
        self.now = utc(2026, 11, 6, 21, 10)  # Friday 16:10 in New York (EST)
        self.gw.protector.sweep()
        self.assertEqual([(o["a"], o["b"], o["s"], o["r"]) for o in self.closes()], [(GOLD, False, "0.05", True)])
        cancels = [c["cancels"] for c in self.x.sent if c["type"] == "cancel"]
        self.assertEqual(cancels, [[{"a": GOLD, "o": 81}]], "GOLD's resting order goes; XYZ100's stays with its pool")

    def test_a_pool_that_holds_over_the_weekend_has_a_gold_order_taken_off_on_saturday_and_keeps_xyz100s(self):
        self.x.positions = {GOLD: Decimal("0.05")}
        self.x.orders = [resting("xyz:GOLD", "B", "0.05", 91), resting("xyz:XYZ100", "B", "0.01", 92)]
        self.gw.protector.take_on([self.key.address])
        self.gw.protector.hold_over_weekend = frozenset({ACCOUNT.lower()})
        self.reader.pool_of = lambda account: ACCOUNT
        self.now = utc(2026, 11, 7, 12, 0)  # Saturday
        self.gw.protector.sweep()
        self.assertEqual([c["cancels"] for c in self.x.sent if c["type"] == "cancel"], [[{"a": GOLD, "o": 91}]])
        self.assertEqual([(o["a"], o["r"]) for o in self.closes()], [(GOLD, True)])

    def test_a_pool_that_holds_over_the_weekend_keeps_its_xyz100_position_on_saturday_and_has_gold_closed(self):
        self.x.positions = {GOLD: Decimal("0.05"), XYZ100: Decimal("0.01")}
        self.gw.protector.take_on([self.key.address])
        self.gw.protector.hold_over_weekend = frozenset({ACCOUNT.lower()})
        self.reader.pool_of = lambda account: ACCOUNT
        self.now = utc(2026, 11, 7, 12, 0)  # Saturday
        self.gw.protector.sweep()
        self.assertEqual([(o["a"], o["b"], o["s"], o["r"]) for o in self.closes()], [(GOLD, False, "0.05", True)])

    def test_a_pool_flat_over_the_weekend_closes_gold_with_the_rest(self):
        self.x.positions = {GOLD: Decimal("0.05"), XYZ100: Decimal("0.01")}
        self.gw.protector.take_on([self.key.address])
        self.now = utc(2026, 11, 6, 21, 10)
        self.gw.protector.sweep()
        self.assertEqual(sorted(o["a"] for o in self.closes()), [XYZ100, GOLD])

    def test_gold_opens_nothing_from_friday_16_and_again_from_sunday_18_05(self):
        self.now = utc(2026, 11, 6, 21, 0)  # Friday 16:00
        self.assertEqual(self.order()[1]["code"], "market_window")
        self.now = utc(2026, 11, 8, 23, 4)  # Sunday 18:04
        self.assertEqual(self.order()[1]["code"], "market_window")
        self.now = utc(2026, 11, 8, 23, 5)  # Sunday 18:05
        self.assertEqual(self.order()[0], 200)


if __name__ == "__main__":
    unittest.main()
