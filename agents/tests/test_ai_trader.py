"""The AI trader's limits and its session loop. Offline: the chain, Hyperliquid, the gateway
and Claude are all fakes; the SDK's own tool runner drives the loop.

    spike/.venv/bin/python -m unittest discover -s agents/tests -t .
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import unittest
from contextlib import redirect_stdout
from unittest import mock

import anthropic
from anthropic.lib.tools import ToolError
from anthropic.types.beta.parsed_beta_message import ParsedBetaMessage

from agents import ai_trader as ai

ROOT = pathlib.Path(__file__).resolve().parents[2]
FACTORY = "0x00000000000000000000000000000000000000F1"
REGISTRY = "0x00000000000000000000000000000000000000F2"
USDC = "0x00000000000000000000000000000000000000F3"
ACCOUNT = "0x00000000000000000000000000000000000000A1"
POOL = "0x00000000000000000000000000000000000000B1"
POOL_BUSY = "0x00000000000000000000000000000000000000B2"
POOL_POOR = "0x00000000000000000000000000000000000000B3"
NEW_CHALLENGE = "0x00000000000000000000000000000000000000C1"
NOW = 1_800_000_000
UNIVERSE = [{"name": "SOL", "szDecimals": 2, "maxLeverage": 20}, {"name": "APT", "szDecimals": 2, "maxLeverage": 10},
            {"name": "ATOM", "szDecimals": 2, "maxLeverage": 10}, {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]


class Wallet:
    address = "0x00000000000000000000000000000000000000D1"


class FakeChain:
    """A challenge on BTC and ETH with 1000 USDC, 5% daily loss, 10% drawdown, 3x leverage,
    and three pools for sale, of which only POOL can sell now."""

    def __init__(self):
        self.challenge = True
        self.rules = (500, 1000, 300, (3, 4))
        self.terms = (20_000_000, 1_000_000_000, 1000, 7 * 86400, 5000, 5_000_000_000)
        self.verdict = 0
        self.equity, self.notional = 1000.0, 0.0
        self.positions: list[dict] = []
        self.orders: list[dict] = []
        self.sent: list[tuple[str, str, list]] = []
        self.revert: str | None = None
        self.allowance = 0
        self.pools = {
            POOL.lower(): {"stage": 0, "ready": True, "challenge": "0x" + "00" * 20, "spot": 600_000_000_000},
            POOL_BUSY.lower(): {"stage": 1, "ready": True, "challenge": NEW_CHALLENGE, "spot": 600_000_000_000},
            POOL_POOR.lower(): {"stage": 0, "ready": True, "challenge": "0x" + "00" * 20, "spot": 1},
        }

    def call_view(self, to, signature, types, args, out):
        name = signature.split("(")[0]
        to = to.lower()
        if to == FACTORY.lower():
            return {"isChallenge": (self.challenge,), "isPool": (not self.challenge,), "usdc": (USDC,),
                    "pools": ([POOL, POOL_BUSY, POOL_POOR],)}[name]
        if to == USDC.lower():
            return {"decimals": (6,), "allowance": (self.allowance,), "balanceOf": (100_000_000,)}[name]
        if to in self.pools:
            pool = self.pools[to]
            return {"stage": (pool["stage"],), "accountReady": (pool["ready"],), "challenge": (pool["challenge"],),
                    "terms": (self.terms,), "rules": (self.rules,)}[name]
        return {"rules": (self.rules,), "terms": (self.terms,), "violation": (self.verdict,), "status": (2,),
                "stage": (2,), "drawdownBase": (1_000_000_000,), "dayStartEquity": (990_000_000,),
                "deadline": (NOW + 36 * 3600,)}[name]

    def core_spot_balance(self, user, token):
        return {"total": self.pools[user.lower()]["spot"]}

    def info_post(self, body):
        kind = body["type"]
        if kind == "meta":
            return {"universe": UNIVERSE}
        if kind == "clearinghouseState":
            return {"marginSummary": {"accountValue": str(self.equity), "totalNtlPos": str(self.notional),
                                      "totalMarginUsed": "0"},
                    "assetPositions": [{"position": p} for p in self.positions]}
        if kind == "openOrders":
            return self.orders
        if kind == "allMids":
            return {"BTC": "60000", "ETH": "3000"}
        raise AssertionError(f"unexpected info call {kind}")

    def transact(self, wallet, to, signature, types=(), args=()):
        if self.revert:
            raise RuntimeError(f"eth_estimateGas: {{'code': 3, 'message': 'execution reverted', 'data': '{self.revert}'}}")
        self.sent.append((to.lower(), signature.split("(")[0], list(args)))
        if signature == "buyChallenge()":
            self.pools[to.lower()]["challenge"] = NEW_CHALLENGE
        return {"transactionHash": "0x" + "ab" * 32}

    def artifact(self, contract):
        return {"abi": [{"type": "error", "name": "NotFlat", "inputs": []},
                        {"type": "error", "name": "TargetNotMet", "inputs": [{"type": "int64"}, {"type": "int256"}]}]}


class FakeGateway:
    def __init__(self):
        self.orders, self.cancels = [], []

    def order(self, account, asset, is_buy, px, size, tif="Gtc", reduce_only=False):
        self.orders.append((account, asset, is_buy, px, size, tif, reduce_only))
        return {"http": 200, "status": "submitted"}

    def cancel(self, account, asset, oid):
        self.cancels.append((account, asset, oid))
        return {"http": 200, "status": "submitted"}


class WithChain(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain()
        for target, value in ((ai, "c"),):
            patcher = mock.patch.object(target, value, self.chain)
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = mock.patch.object(ai.time, "time", return_value=float(NOW))
        clock.start()
        self.addCleanup(clock.stop)
        quiet = mock.patch.object(ai, "emit")
        self.emitted = quiet.start()
        self.addCleanup(quiet.stop)
        self.gateway = FakeGateway()

    def desk(self, send=True, **limits):
        return ai.Desk(FACTORY, ACCOUNT, ai.Limits(**limits), self.gateway if send else None, Wallet(), send)


class DeskLimits(WithChain):
    def test_only_perps_on_the_accounts_list(self):
        with self.assertRaises(ToolError):
            self.desk().place_order("SOL", "buy", 1, 150, "limit", False)
        with self.assertRaises(ToolError):
            self.desk().market_view("SOL")
        self.assertEqual(self.gateway.orders, [])

    def test_minimum_and_per_order_cap(self):
        desk = self.desk(max_notional=100)
        with self.assertRaises(ToolError):
            desk.place_order("BTC", "buy", 0.0001, 60000, "limit", False)  # 6 USDC
        with self.assertRaises(ToolError):
            desk.place_order("BTC", "buy", 0.002, 60000, "limit", False)  # 120 USDC
        desk.place_order("BTC", "buy", 0.0015, 60000, "limit", False)  # 90 USDC
        self.assertEqual(self.gateway.orders, [(ai.to_checksum_address(ACCOUNT), 3, True, "60000", "0.0015", "Gtc", False)])

    def test_headroom_under_the_leverage_rule(self):
        self.chain.equity, self.chain.notional = 100.0, 200.0  # rule 3x: 300; headroom 0.8: 240
        desk = self.desk(max_notional=100)
        with self.assertRaises(ToolError):
            desk.place_order("ETH", "buy", 0.02, 3000, "ioc", False)  # +60 -> 260
        desk.place_order("ETH", "buy", 0.013, 3000, "ioc", False)  # +39 -> 239
        desk.place_order("ETH", "sell", 0.1, 3000, "ioc", True)  # reduce-only: 300 USDC, no cap
        self.assertEqual([o[5:] for o in self.gateway.orders], [("Ioc", False), ("Ioc", True)])

    def test_orders_per_session(self):
        desk = self.desk(max_orders=2)
        desk.place_order("ETH", "buy", 0.005, 3000, "post_only", False)
        with self.assertRaises(ToolError):
            desk.place_order("ETH", "buy", 0.001, 3000, "post_only", False)  # refused, and not counted
        desk.place_order("ETH", "buy", 0.005, 3000, "post_only", False)
        with self.assertRaises(ToolError):
            desk.place_order("ETH", "buy", 0.005, 3000, "post_only", False)
        self.assertEqual(len(self.gateway.orders), 2)

    def test_no_orders_mode_sends_nothing(self):
        desk = self.desk(send=False)
        out = desk.place_order("BTC", "sell", 0.001, 61000.4, "limit", False)
        self.assertEqual(out, {"status": "not_sent", "order": {"coin": "BTC", "side": "sell", "size": "0.001",
                                                                "limit_price": "61000", "tif": "Gtc",
                                                                "reduce_only": False}})
        self.assertEqual(desk.cancel_order("BTC", 9)["status"], "not_sent")
        self.assertEqual(desk.graduate()["status"], "not_sent")
        self.assertEqual((self.gateway.orders, self.gateway.cancels, self.chain.sent), ([], [], []))

    def test_close_position_crosses_the_book_reduce_only(self):
        desk = self.desk()
        with self.assertRaises(ToolError):
            desk.close_position("BTC")
        self.chain.positions = [{"coin": "BTC", "szi": "0.0031", "entryPx": "59000", "unrealizedPnl": "3.1"}]
        desk.close_position("BTC")
        self.assertEqual(self.gateway.orders[-1][2:], (False, "58800", "0.0031", "Ioc", True))

    def test_graduation_once_with_a_readable_refusal(self):
        desk = self.desk()
        selector = "0x" + ai.keccak(text="NotFlat()")[:4].hex()
        self.chain.revert = selector
        self.assertEqual(desk.graduate(), {"status": "refused_by_contract", "reason": "NotFlat"})
        with self.assertRaises(ToolError):
            desk.graduate()

    def test_account_view_shows_the_floors(self):
        view = self.desk().account_view()
        self.assertEqual(view["limits_now"], {"equity_floor_drawdown_usdc": 900.0, "equity_floor_today_usdc": 940.5,
                                              "max_open_notional_by_rule_usdc": 3000.0})
        self.assertEqual(view["challenge"]["target_equity_usdc"], 1100.0)
        self.assertEqual(view["challenge"]["hours_left"], 36.0)
        self.assertEqual(view["contract_verdict_now"], "inside the rules")
        self.chain.verdict = 3
        self.assertEqual(self.desk().account_view()["contract_verdict_now"], "Leverage")

    def test_a_stranger_account_is_refused(self):
        self.chain.challenge = False
        self.chain.call_view = lambda to, sig, *a: (False,) if to.lower() == FACTORY.lower() else (None,)
        with self.assertRaises(SystemExit):
            self.desk()


class ShopLimits(WithChain):
    def shop(self, send=True, max_price=25.0):
        return ai.Shop(FACTORY, max_price, Wallet(), send)

    def test_listing_shows_only_pools_that_can_sell_now(self):
        offers = self.shop().listing()
        self.assertEqual([o["pool"] for o in offers], [ai.to_checksum_address(POOL)])
        self.assertEqual(offers[0]["price_usdc"], 20.0)
        self.assertEqual(offers[0]["rules"]["perps"], ["BTC", "ETH"])
        self.assertNotIn("_price_units", offers[0])

    def test_buy_needs_the_listing_price_and_the_cap(self):
        shop = self.shop(max_price=15.0)
        with self.assertRaises(ToolError):
            shop.buy(POOL, 20.0)  # not listed yet
        shop.listing()
        with self.assertRaises(ToolError):
            shop.buy(POOL, 19.0)  # not the listed price
        with self.assertRaises(ToolError):
            shop.buy(POOL, 20.0)  # over the cap
        self.assertEqual(self.chain.sent, [])

    def test_one_purchase_approving_the_exact_price(self):
        shop = self.shop()
        shop.listing()
        out = shop.buy(POOL, 20.0)
        self.assertEqual(out["challenge"], ai.to_checksum_address(NEW_CHALLENGE))
        self.assertEqual(self.chain.sent, [(USDC.lower(), "approve", [ai.to_checksum_address(POOL), 20_000_000]),
                                           (POOL.lower(), "buyChallenge", [])])
        with self.assertRaises(ToolError):
            shop.buy(POOL, 20.0)

    def test_dry_shop_buys_nothing(self):
        shop = self.shop(send=False)
        shop.listing()
        self.assertEqual(shop.buy(POOL, 20.0)["status"], "not_sent")
        self.assertEqual(self.chain.sent, [])


def message(stop_reason, *blocks, input_tokens=1000, output_tokens=100):
    return ParsedBetaMessage.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": list(blocks),
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    })


def tool_use(name, tool_id, **inputs):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inputs}


def text(value):
    return {"type": "text", "text": value}


class Session(WithChain):
    def session(self, replies, steps=8, budget=1.0, send=True):
        desk = self.desk(send=send)
        params = ai.request_params("claude-opus-5", "high", 16000, ai.TRADE_SYSTEM, "go", ai.trade_tools(desk),
                                   steps, fallbacks=True)
        client = anthropic.Anthropic(api_key="placeholder-not-a-key")
        with mock.patch.object(client.beta.messages, "parse", side_effect=replies) as parse:
            outcome = ai.run_session(client, params, budget)
        return outcome, parse

    def test_tools_run_and_their_results_go_back(self):
        outcome, parse = self.session([
            message("tool_use", tool_use("get_account", "t1")),
            message("tool_use", tool_use("place_order", "t2", coin="BTC", side="buy", size=0.001,
                                         limit_price=60000, order_type="limit", reduce_only=False),
                    tool_use("place_order", "t3", coin="SOL", side="buy", size=1, limit_price=150,
                             order_type="limit", reduce_only=False)),
            message("end_turn", text("Bought a little BTC.")),
        ])
        self.assertEqual(outcome["stopped"], "finished")
        self.assertEqual(outcome["turns"], 3)
        self.assertEqual(len(self.gateway.orders), 1)
        last_user = parse.call_args_list[2].kwargs["messages"][-1]
        results = {r["tool_use_id"]: r for r in last_user["content"]}
        self.assertEqual(set(results), {"t2", "t3"})  # both results, in one message
        self.assertTrue(results["t3"].get("is_error"))
        self.assertIn("not on this account's list", str(results["t3"]["content"]))
        self.assertAlmostEqual(outcome["spent_usd"], 3 * (1000 * 5 + 100 * 25) / 1e6)

    def test_budget_stops_before_the_turns_tools_run(self):
        outcome, _ = self.session([
            message("tool_use", tool_use("place_order", "t1", coin="BTC", side="buy", size=0.001, limit_price=60000,
                                         order_type="limit", reduce_only=False), input_tokens=200_000),
        ], budget=0.5)
        self.assertEqual(outcome["stopped"], "budget")
        self.assertEqual(self.gateway.orders, [])

    def test_turn_cap(self):
        outcome, parse = self.session([message("tool_use", tool_use("get_account", f"t{i}")) for i in range(5)], steps=2)
        self.assertEqual((outcome["stopped"], outcome["turns"], parse.call_count), ("turn_cap", 2, 2))

    def test_refusal_and_truncation_run_no_tools(self):
        buy = tool_use("place_order", "t1", coin="BTC", side="buy", size=0.001, limit_price=60000,
                       order_type="limit", reduce_only=False)
        self.assertEqual(self.session([message("refusal", buy)])[0]["stopped"], "refused")
        self.assertEqual(self.session([message("max_tokens", buy)])[0]["stopped"], "truncated")
        self.assertEqual(self.gateway.orders, [])


class Requests(unittest.TestCase):
    def test_opus_asks_for_adaptive_thinking_and_default_fallbacks(self):
        p = ai.request_params("claude-opus-5", "high", 16000, "sys", "go", [], 8, fallbacks=True)
        self.assertEqual(p["thinking"], {"type": "adaptive"})
        self.assertEqual(p["output_config"], {"effort": "high"})
        self.assertEqual((p["betas"], p["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(p["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(p["max_iterations"], 8)
        off = ai.request_params("claude-opus-5", "high", 16000, "sys", "go", [], 8, fallbacks=False)
        self.assertNotIn("fallbacks", off)

    def test_other_models(self):
        haiku = ai.request_params("claude-haiku-4-5", "high", 4000, "sys", "go", [], 8, fallbacks=True)
        self.assertFalse({"thinking", "output_config", "fallbacks", "betas"} & set(haiku))
        sonnet = ai.request_params("claude-sonnet-5", "medium", 4000, "sys", "go", [], 8, fallbacks=True)
        self.assertEqual(sonnet["output_config"], {"effort": "medium"})
        self.assertNotIn("fallbacks", sonnet)

    def test_cost(self):
        usage = mock.Mock(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=2000,
                          cache_read_input_tokens=10_000)
        self.assertAlmostEqual(ai.cost_usd("claude-opus-5", usage), 0.025)

    def test_every_model_has_a_price(self):
        self.assertTrue(set(ai.FALLBACK_MODELS) <= set(ai.PRICES))
        self.assertIn(ai.MODEL, ai.PRICES)


class DryRun(WithChain):
    def test_prints_the_request_and_calls_nothing(self):
        out = io.StringIO()
        with mock.patch.object(ai, "Anthropic", side_effect=AssertionError("no client in a dry run")), \
                mock.patch.object(ai.deployments, "resolve", return_value=(FACTORY, REGISTRY)), \
                mock.patch.object(self.chain, "assert_testnet", create=True), \
                mock.patch.object(self.chain, "account", side_effect=AssertionError("no key in a dry run"), create=True), \
                redirect_stdout(out):
            code = ai.main(["trade", "--deployment", "demo", "--account", ACCOUNT, "--dry-run"])
        self.assertEqual(code, 0)
        request = json.loads(out.getvalue())
        self.assertEqual([t["name"] for t in request["tools"]],
                         ["get_account", "get_market", "place_order", "cancel_order", "close_position",
                          "request_graduation"])
        self.assertTrue(all(t["strict"] for t in request["tools"]))
        self.assertEqual((self.chain.sent, self.gateway.orders), ([], []))


class MatchesTheContracts(unittest.TestCase):
    @staticmethod
    def enum(path: str, name: str) -> tuple[str, ...]:
        body = re.search(rf"enum {name} \{{(.*?)\}}", (ROOT / path).read_text(), re.S).group(1)
        return tuple(m.strip() for m in body.split(",") if m.strip())

    def test_enum_names(self):
        self.assertEqual(ai.STATUS, self.enum("src/ChallengeAccount.sol", "Status"))
        self.assertEqual(ai.STAGE, self.enum("src/Pool.sol", "Stage"))
        self.assertEqual(ai.BREACH, self.enum("src/Types.sol", "Breach"))


if __name__ == "__main__":
    unittest.main()
