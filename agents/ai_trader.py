"""An AI trader, run by Claude, for the demo: it picks a pool, buys a challenge and trades it
through the same pool gateway a person uses.

    spike/.venv/bin/python -m agents.ai_trader shop  --deployment demo --max-price 25 --dry-run
    spike/.venv/bin/python -m agents.ai_trader shop  --deployment demo --max-price 25
    spike/.venv/bin/python -m agents.ai_trader trade --deployment demo --account 0x… --dry-run
    spike/.venv/bin/python -m agents.ai_trader trade --deployment demo --account 0x… --no-orders
    spike/.venv/bin/python -m agents.ai_trader trade --deployment demo --account 0x… --budget-usd 0.50

Claude sees the chain and the market only through the tools below, and acts with the agent's
own testnet wallet (--wallet, default "ai-trader"). It never holds an account's agent key. The
limits that matter are enforced in this file, outside the model: the account's own perp list,
a notional cap per order, headroom under the leverage rule, a number of orders per session, a
price cap and one purchase per shop session, a number of model turns and a spending budget.

--dry-run builds the first request, prints it and stops: no call to Claude, no transaction,
no order. --no-orders runs the model but answers every tool that would act with "not sent".
Credentials for Claude come from the environment (ANTHROPIC_API_KEY or an `ant auth login`
profile); this script never reads or prints them. Testnet only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Literal

from anthropic import Anthropic, beta_tool
from anthropic.lib.tools import ToolError

from gateway.chain import JsonRpcReader
from ops import deployments
from spike.hlspike import common as c

from .client import GatewayClient
from .desk import Desk, Limits, Refused, Shop, usd

MODEL = "claude-opus-5"
# USD per million tokens (input, output), first-party API rates.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# Models that take `fallbacks: "default"`, which re-runs a declined request on another model.
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def emit(event: str, **fields) -> None:
    print(json.dumps({"t": int(time.time()), "event": event, **fields}, default=str), flush=True)



def answered(call, limit: int = 1500) -> str:
    """A desk call's result for the model; a refusal goes back as a tool error."""
    try:
        return trimmed(call(), limit)
    except Refused as exc:
        raise ToolError(str(exc)) from None


def trimmed(payload: Any, limit: int = 1500) -> str:
    text = json.dumps(payload, default=str)
    return text if len(text) <= limit else text[:limit] + "…"



# ── costs ────────────────────────────────────────────────────────────────────────────────

def cost_usd(model: str, usage) -> float:
    price_in, price_out = PRICES[model]
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (usage.input_tokens * price_in + written * price_in * 1.25 + read * price_in * 0.1
            + usage.output_tokens * price_out) / 1e6


# ── trading one account ──────────────────────────────────────────────────────────────────


def trade_tools(desk: Desk) -> list:
    @beta_tool(strict=True)
    def get_account() -> str:
        """The trading account right now: equity, open notional and leverage, positions, open
        orders, the rules and the equity floors they imply, the contract's verdict, the
        challenge target and deadline, and what is left of this session's order budget. Call it
        at the start of every session and again after any order or cancel."""
        return answered(desk.account_view, 6000)

    @beta_tool(strict=True)
    def get_market(coin: str) -> str:
        """Market data for one perp on the account's list: mid, mark and oracle price, hourly
        funding, open interest, 24h volume and change, and the last 24 hourly closes. Call it
        before deciding whether to trade that perp.

        Args:
            coin: Perp name exactly as get_account lists it, for example BTC.
        """
        return answered(lambda: desk.market_view(coin), 3000)

    @beta_tool(strict=True)
    def place_order(coin: str, side: Literal["buy", "sell"], size: float, limit_price: float,
                    order_type: Literal["limit", "post_only", "ioc"], reduce_only: bool) -> str:
        """Send one order through the pool gateway, signed by this agent's wallet. Price and size
        are rounded to the exchange's steps. Refused before sending if the perp isn't on the
        list, the order is under 10 USDC, over the per-order cap, or would take open notional
        past the agent's headroom under the leverage rule; reduce-only orders skip the last two
        checks. The answer shows what the gateway and Hyperliquid said.

        Args:
            coin: Perp name from the account's list.
            side: buy or sell.
            size: Size in coins, for example 0.0005 for BTC.
            limit_price: Limit price in USDC.
            order_type: limit rests on the book, post_only only rests (cancelled if it would
                cross), ioc fills what it can at once and cancels the rest.
            reduce_only: True to only shrink an existing position.
        """
        return answered(lambda: desk.place_order(coin, side, size, limit_price, order_type, reduce_only))

    @beta_tool(strict=True)
    def cancel_order(coin: str, oid: int) -> str:
        """Cancel one open order by its id, as get_account lists it.

        Args:
            coin: Perp name of the order.
            oid: Order id.
        """
        return answered(lambda: desk.cancel_order(coin, oid))

    @beta_tool(strict=True)
    def close_position(coin: str) -> str:
        """Close the whole open position in one perp with a reduce-only order that crosses the
        book. Counts as one order.

        Args:
            coin: Perp name of the position.
        """
        return answered(lambda: desk.close_position(coin))

    tools = [get_account, get_market, place_order, cancel_order, close_position]
    if desk.is_challenge:
        @beta_tool(strict=True)
        def request_graduation() -> str:
            """Ask the challenge contract to pass the challenge. It succeeds only if equity is at
            or above the target, the deadline hasn't passed, no rule is broken and there is no
            open position; otherwise the answer names what is missing. Call it once, when
            get_account shows all four."""
            return answered(desk.graduate)

        tools.append(request_graduation)
    return tools


TRADE_SYSTEM = """You trade one account on the Hyperliquid testnet, for a live demo of trading pools. The \
capital is mock USDC, but trade it as if it were real: the demo is about trading well inside rules.

How the account works:
- The account is a smart contract. You reach the market only through an order gateway, which checks \
your wallet's signature, the account's list of perps and the platform's size and notional caps. The key \
that signs for the account is held by that gateway, and you never see it.
- The contract holds the pool's rules: a maximum daily loss measured from the day's first snapshot, \
a maximum drawdown from the starting equity, a maximum leverage (open notional divided by equity), \
and the list of perps. If any rule is broken, anyone can stop the account: its key is cut off, its \
orders are cancelled and its positions closed. For a challenge, that ends it.
- A challenge passes when equity reaches the target before the deadline, with no rule broken and \
no open position. Its trader then trades the pool's own capital.

How to work:
- Start with get_account, then get_market for any perp you consider.
- Trade only when you can say why. A session with no trade is fine.
- Keep a margin from every limit. Size positions so that an ordinary move against you breaks \
neither the daily loss nor the drawdown floor, and keep leverage well below the rule.
- Price limit orders near the market. The smallest order is 10 USDC.
- If the gateway or the exchange refuses an order, read the reason and don't send the same order \
again unchanged.
- To pass a challenge, close every position once equity is at the target, check get_account, then \
call request_graduation.
- Finish with two or three plain sentences: what you did and why, or why you did nothing."""


# ── picking a pool ───────────────────────────────────────────────────────────────────────


def shop_tools(shop: Shop) -> list:
    @beta_tool(strict=True)
    def list_pools() -> str:
        """The pools that can sell a challenge right now, with each one's price, the platform's
        fee on top of it, challenge capital, profit target, duration, the trader's share of the
        challenge profit and of the funded profit (two separate numbers), the capital a passing
        trader gets, and its rules. Call it before choosing."""
        return answered(shop.listing, 8000)

    @beta_tool(strict=True)
    def buy_challenge(pool: str, price_usdc: float) -> str:
        """Buy a challenge from one pool with this agent's wallet. Refused if the pool isn't in the
        latest listing, the price differs from the listing, the price plus the platform fee is
        over the session's cap, or a challenge was already bought in this session. Returns the
        new challenge account.

        Args:
            pool: Pool address from list_pools.
            price_usdc: The price list_pools showed for that pool.
        """
        return answered(lambda: shop.buy(pool, price_usdc))

    return [list_pools, buy_challenge]


SHOP_SYSTEM = """You pick a pool to buy a trading challenge from, for a live demo of trading pools on the \
Hyperliquid testnet (mock USDC).

Each pool sells a challenge: a smaller account with its own capital, a profit target, a deadline and \
rules (maximum daily loss, maximum drawdown, maximum leverage, a list of perps). A challenge that \
reaches its target within the rules is passed, and its trader then trades the pool's capital for a \
share of the profit. You will trade the challenge you buy yourself, in later sessions, through an \
order gateway, keeping well inside the rules.

Call list_pools, then choose the pool whose terms give you the best chance of passing: a target \
you can reach in the time, rules loose enough to trade the perps it lists, a fair price. Buy at \
most one challenge with buy_challenge, and only within the price cap you are given. If nothing \
fits, buy nothing. Finish with two or three plain sentences on what you chose and why."""


# ── the session ──────────────────────────────────────────────────────────────────────────

def request_params(model: str, effort: str, max_tokens: int, system: str, first_message: str,
                   tools: list, steps: int, fallbacks: bool) -> dict:
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": first_message}],
        "tools": tools,
        "max_iterations": steps,
    }
    if not model.startswith("claude-haiku"):
        params["thinking"] = {"type": "adaptive"}
        params["output_config"] = {"effort": effort}
    if fallbacks and model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def describe(params: dict) -> dict:
    """The request as it would be sent, for --dry-run."""
    shown = {k: v for k, v in params.items() if k != "tools"}
    shown["tools"] = [t.to_dict() for t in params["tools"]]
    return shown


def run_session(client, params: dict, budget_usd: float) -> dict:
    """Runs the tool loop until Claude finishes, the turn cap is hit or the budget is spent.
    The budget is checked after each turn and before its tools run."""
    model = params["model"]
    runner = client.beta.messages.tool_runner(**params)
    spent, turns, stopped, last = 0.0, 0, "finished", None
    for message in runner:
        turns += 1
        last = message.stop_reason
        spent += cost_usd(model, message.usage)
        for block in message.content:
            if block.type == "text" and block.text.strip():
                emit("model_says", text=block.text)
            elif block.type == "tool_use":
                emit("model_calls", tool=block.name, input=block.input)
        emit("turn", n=turns, stop_reason=message.stop_reason, model=message.model,
             input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens,
             cache_read=getattr(message.usage, "cache_read_input_tokens", 0), spent_usd=round(spent, 4))
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            emit("refused", category=getattr(details, "category", None))
            stopped = "refused"
            break
        if message.stop_reason == "max_tokens":
            stopped = "truncated"
            break
        if message.stop_reason == "tool_use":
            if spent >= budget_usd:
                stopped = "budget"
                break
            reply = runner.generate_tool_call_response()
            for result in (reply or {}).get("content", []):
                emit("tool_result", tool_use_id=result["tool_use_id"], is_error=result.get("is_error", False),
                     content=str(result["content"])[:400])
    else:
        if last == "tool_use":  # the runner ran out of turns with tool results Claude never saw
            stopped = "turn_cap"
    emit("session_done", stopped=stopped, turns=turns, spent_usd=round(spent, 4))
    return {"stopped": stopped, "turns": turns, "spent_usd": spent}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["shop", "trade"])
    p.add_argument("--deployment", required=True)
    p.add_argument("--account", help="trade: the challenge or funded pool to trade")
    p.add_argument("--gateway", default="http://127.0.0.1:8787")
    p.add_argument("--wallet", default="ai-trader")
    p.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-tokens", type=int, default=16000)
    p.add_argument("--steps", type=int, default=8, help="model turns per session, at most")
    p.add_argument("--budget-usd", type=float, default=0.50, help="estimated spend per session, at most")
    p.add_argument("--no-fallbacks", action="store_true", help="don't re-run a declined request on another model")
    p.add_argument("--max-notional", type=float, default=100.0, help="trade: USDC per order, at most")
    p.add_argument("--max-orders", type=int, default=4, help="trade: orders per session, at most")
    p.add_argument("--max-price", type=float, default=25.0, help="shop: challenge price in USDC, at most")
    p.add_argument("--dry-run", action="store_true", help="print the first request; call nothing")
    p.add_argument("--no-orders", action="store_true", help="run the model, but act on nothing")
    args = p.parse_args(argv)
    if args.mode == "trade" and not args.account:
        p.error("trade needs --account")

    c.assert_testnet()
    factory, registry = deployments.resolve(args.deployment)
    send = not (args.dry_run or args.no_orders)
    wallet = c.account(args.wallet) if send else None
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    if args.mode == "shop":
        shop = Shop(factory, args.max_price, wallet, send)
        tools = shop_tools(shop)
        system = SHOP_SYSTEM
        balance = None if wallet is None else usd(
            c.call_view(shop.usdc, "balanceOf(address)", ["address"], [wallet.address], ["uint256"])[0], shop.decimals)
        first = (f"Session at {now}. Your price cap is {args.max_price} USDC"
                 + ("" if balance is None else f"; your wallet holds {balance} USDC") + ". Choose a pool.")
    else:
        if send:
            reader = JsonRpcReader(c.RPC_URL, factory, registry)
            key = reader.trading_key(args.account)
            if key is None:
                raise SystemExit("the account isn't trading right now")
            if not reader.is_bound(key, args.account, wallet.address):
                raise SystemExit("this wallet is not the account's trader")
        client = GatewayClient(wallet, args.gateway) if send else None
        desk = Desk(factory, args.account, Limits(args.max_notional, args.max_orders), client, wallet, send)
        tools = trade_tools(desk)
        system = TRADE_SYSTEM
        first = (f"Session at {now}. The account is {desk.account}. This session allows {args.max_orders} "
                 f"orders of at most {args.max_notional} USDC each. Decide what to do.")

    params = request_params(args.model, args.effort, args.max_tokens, system, first, tools, args.steps,
                            fallbacks=not args.no_fallbacks)
    if args.dry_run:
        print(json.dumps(describe(params), indent=2, default=str))
        return 0

    emit("session_start", mode=args.mode, model=args.model, effort=args.effort, steps=args.steps,
         budget_usd=args.budget_usd, orders="not sent" if args.no_orders else "sent",
         fallbacks=params.get("fallbacks"))
    run_session(Anthropic(), params, args.budget_usd)  # credentials come from the environment
    return 0


if __name__ == "__main__":
    sys.exit(main())
