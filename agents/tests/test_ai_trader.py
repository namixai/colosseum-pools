"""The AI trader's session loop over the desk. Offline: the chain, Hyperliquid, the gateway
and Claude are all fakes; the SDK's own tool runner drives the loop.

    spike/.venv/bin/python -m unittest discover -s agents/tests -t .
"""

from __future__ import annotations

import io
import json
import re
import unittest
from contextlib import redirect_stdout
from unittest import mock

import anthropic
from anthropic.lib.tools import ToolError
from anthropic.types.beta.parsed_beta_message import ParsedBetaMessage

from agents import ai_trader as ai
from agents import desk as dk
from agents.tests.fakes import ACCOUNT, FACTORY, NOW, REGISTRY, ROOT, FakeChain, FakeGateway, Wallet

class WithChain(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain()
        for target in (ai, dk):
            patcher = mock.patch.object(target, "c", self.chain)
            patcher.start()
            self.addCleanup(patcher.stop)
            clock = mock.patch.object(target.time, "time", return_value=float(NOW))
            clock.start()
            self.addCleanup(clock.stop)
        quiet = mock.patch.object(ai, "emit")
        self.emitted = quiet.start()
        self.addCleanup(quiet.stop)
        self.gateway = FakeGateway()

    def desk(self, send=True, **limits):
        return ai.Desk(FACTORY, ACCOUNT, ai.Limits(**limits), self.gateway if send else None, Wallet(), send)


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


class Refusals(unittest.TestCase):
    def test_a_desk_refusal_reaches_the_model_as_a_tool_error(self):
        def refuse():
            raise dk.Refused("no orders left in this session")

        with self.assertRaises(ToolError) as caught:
            ai.answered(refuse)
        self.assertEqual(str(caught.exception), "no orders left in this session")
        self.assertEqual(ai.answered(lambda: {"ok": 1}), '{"ok": 1}')


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
        self.assertEqual(dk.STATUS, self.enum("src/ChallengeAccount.sol", "Status"))
        self.assertEqual(dk.STAGE, self.enum("src/Pool.sol", "Stage"))
        self.assertEqual(dk.BREACH, self.enum("src/Types.sol", "Breach"))


if __name__ == "__main__":
    unittest.main()
