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
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal

from anthropic import Anthropic, beta_tool
from anthropic.lib.tools import ToolError
from eth_utils import keccak, to_checksum_address

from gateway.chain import JsonRpcReader
from ops import deployments
from spike.hlspike import common as c

from .client import GatewayClient, round_price, round_size

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
MIN_ORDER_USDC = 10.0
RULES = "(uint16,uint16,uint32,uint32[])"
TERMS = "(uint64,uint64,uint16,uint32,uint16,uint64)"

# Enum names in declaration order; the tests hold them against the Solidity source.
STATUS = ("None", "Created", "Active", "Breached", "Expired", "Forfeited", "Passed", "Aborted", "Settled")
STAGE = ("Idle", "Challenge", "Funded", "Closing")
BREACH = ("None", "Drawdown", "DailyLoss", "Leverage", "ForbiddenAsset")


def emit(event: str, **fields) -> None:
    print(json.dumps({"t": int(time.time()), "event": event, **fields}, default=str), flush=True)


def view(addr: str, sig: str, out: str, types=(), args=()):
    return c.call_view(addr, sig, list(types), list(args), [out])[0]


def usd(units: int, decimals: int = 6) -> float:
    return units / 10**decimals


def trimmed(payload: Any, limit: int = 1500) -> str:
    text = json.dumps(payload, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


# ── contract errors, for readable reverts ────────────────────────────────────────────────

def _error_names() -> dict[str, str]:
    names = {}
    for contract in ("ChallengeAccount", "Pool"):
        for item in c.artifact(contract)["abi"]:
            if item["type"] == "error":
                sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
                names["0x" + keccak(text=sig)[:4].hex()] = item["name"]
    return names


def revert_reason(exc: Exception, names: dict[str, str]) -> str:
    text = str(exc)
    for data in re.findall(r"0x[0-9a-fA-F]{8,}", text):
        name = names.get(data[:10].lower())
        if name:
            return name
    return text[:200]


# ── costs ────────────────────────────────────────────────────────────────────────────────

def cost_usd(model: str, usage) -> float:
    price_in, price_out = PRICES[model]
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (usage.input_tokens * price_in + written * price_in * 1.25 + read * price_in * 0.1
            + usage.output_tokens * price_out) / 1e6


# ── trading one account ──────────────────────────────────────────────────────────────────

@dataclass
class Limits:
    max_notional: float = 100.0
    max_orders: int = 4
    leverage_headroom: float = 0.8  # new exposure stays under this share of the leverage rule


class Desk:
    """Everything the model can read or do for one account, with the limits applied."""

    def __init__(self, factory: str, account: str, limits: Limits, client: GatewayClient | None,
                 wallet=None, send: bool = True):
        self.factory = to_checksum_address(factory)
        self.account = to_checksum_address(account)
        self.limits = limits
        self.client = client
        self.wallet = wallet
        self.send = send
        self.orders_left = limits.max_orders
        self.cancels_left = 2 * limits.max_orders
        self.graduations_left = 1
        self.is_challenge = view(self.factory, "isChallenge(address)", "bool", ["address"], [self.account])
        if not self.is_challenge and not view(self.factory, "isPool(address)", "bool", ["address"], [self.account]):
            raise SystemExit(f"{self.account} is not an account of this factory")
        daily, drawdown, leverage, assets = c.call_view(self.account, "rules()", [], [], [RULES])[0]
        self.rules = {"daily_loss_bps": daily, "drawdown_bps": drawdown, "leverage_x100": leverage}
        universe = c.info_post({"type": "meta"})["universe"]
        self.index_of = {a["name"]: i for i, a in enumerate(universe)}
        self.perps = {universe[i]["name"]: (i, universe[i]["szDecimals"]) for i in assets if i < len(universe)}
        self._errors: dict[str, str] | None = None

    # reads

    def _perp(self, coin: str) -> tuple[int, int]:
        if coin not in self.perps:
            raise ToolError(f"{coin} is not on this account's list: {', '.join(sorted(self.perps))}")
        return self.perps[coin]

    def _state(self) -> dict:
        return c.info_post({"type": "clearinghouseState", "user": self.account})

    def account_view(self) -> dict:
        state = self._state()
        summary = state["marginSummary"]
        equity = float(summary["accountValue"])
        notional = float(summary["totalNtlPos"])
        positions = [p["position"] for p in state.get("assetPositions", [])]
        held_outside = sorted({self.index_of[p["coin"]] for p in positions
                               if p["coin"] in self.index_of and p["coin"] not in self.perps})
        verdict = view(self.account, "violation(uint32[])", "uint8", ["uint32[]"], [held_outside])
        base = usd(view(self.account, "drawdownBase()", "int64"))
        day_start = usd(view(self.account, "dayStartEquity()", "int64"))
        out = {
            "account": self.account,
            "kind": "challenge" if self.is_challenge else "funded pool",
            "state": (STATUS[view(self.account, "status()", "uint8")] if self.is_challenge
                      else STAGE[view(self.account, "stage()", "uint8")]),
            "equity_usdc": equity,
            "open_notional_usdc": notional,
            "margin_used_usdc": float(summary["totalMarginUsed"]),
            "leverage_now": round(notional / equity, 3) if equity > 0 else None,
            "rules": {
                "max_daily_loss_pct": self.rules["daily_loss_bps"] / 100,
                "max_drawdown_pct": self.rules["drawdown_bps"] / 100,
                "max_leverage": self.rules["leverage_x100"] / 100,
                "perps": sorted(self.perps),
            },
            "limits_now": {
                "equity_floor_drawdown_usdc": round(base * (1 - self.rules["drawdown_bps"] / 1e4), 2),
                "equity_floor_today_usdc": (round(day_start * (1 - self.rules["daily_loss_bps"] / 1e4), 2)
                                            if day_start > 0 else None),
                "max_open_notional_by_rule_usdc": round(max(equity, 0) * self.rules["leverage_x100"] / 100, 2),
            },
            "contract_verdict_now": "inside the rules" if verdict == 0 else BREACH[verdict],
            "positions": [{
                "coin": p["coin"], "size": float(p["szi"]), "entry_price": float(p["entryPx"] or 0),
                "unrealized_pnl_usdc": float(p["unrealizedPnl"]),
                "liquidation_price": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
            } for p in positions[:20]],
            "open_orders": [{
                "coin": o["coin"], "side": "buy" if o["side"] == "B" else "sell",
                "limit_price": float(o["limitPx"]), "size": float(o["sz"]), "oid": o["oid"],
            } for o in c.info_post({"type": "openOrders", "user": self.account})[:30]],
            "session": {"orders_left": self.orders_left, "cancels_left": self.cancels_left,
                        "max_order_notional_usdc": self.limits.max_notional},
        }
        if self.is_challenge:
            _, capital, target_bps, _, share_bps, _ = c.call_view(self.account, "terms()", [], [], [TERMS])[0]
            deadline = view(self.account, "deadline()", "uint64")
            out["challenge"] = {
                "capital_usdc": usd(capital),
                "target_equity_usdc": round(usd(capital) * (1 + target_bps / 1e4), 2),
                "deadline_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(deadline)) if deadline else None,
                "hours_left": round((deadline - time.time()) / 3600, 1) if deadline else None,
                "trader_share_of_profit_pct": share_bps / 100,
                "passes_when": "equity reaches the target before the deadline, no rule broken, no open position",
            }
        return out

    def market_view(self, coin: str) -> dict:
        index, size_decimals = self._perp(coin)
        meta, ctxs = c.info_post({"type": "metaAndAssetCtxs"})
        ctx = ctxs[index]
        now_ms = int(time.time() * 1000)
        candles = c.info_post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1h", "startTime": now_ms - 24 * 3600 * 1000, "endTime": now_ms}})
        prev = float(ctx["prevDayPx"])
        mark = float(ctx["markPx"])
        return {
            "coin": coin,
            "mid": float(ctx["midPx"]) if ctx.get("midPx") else None,
            "mark": mark,
            "oracle": float(ctx["oraclePx"]),
            "funding_rate_hourly": float(ctx["funding"]),
            "open_interest": float(ctx["openInterest"]),
            "volume_24h_usdc": float(ctx["dayNtlVlm"]),
            "change_24h_pct": round((mark / prev - 1) * 100, 3) if prev else None,
            "size_decimals": size_decimals,
            "max_exchange_leverage": meta["universe"][index]["maxLeverage"],
            "hourly_closes": [float(k["c"]) for k in candles][-24:],
        }

    # actions

    def place_order(self, coin: str, side: str, size: float, limit_price: float,
                    order_type: str = "limit", reduce_only: bool = False) -> dict:
        index, size_decimals = self._perp(coin)
        if self.orders_left <= 0:
            raise ToolError("no orders left in this session")
        try:
            px = round_price(limit_price, size_decimals)
            sz = round_size(size, size_decimals)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        notional = float(px) * float(sz)
        if notional < MIN_ORDER_USDC:
            raise ToolError(f"{notional:.2f} USDC is under Hyperliquid's minimum order of {MIN_ORDER_USDC:.0f} USDC")
        if not reduce_only:
            if notional > self.limits.max_notional:
                raise ToolError(f"{notional:.2f} USDC is over this session's cap of {self.limits.max_notional} per order")
            summary = self._state()["marginSummary"]
            equity, open_notional = float(summary["accountValue"]), float(summary["totalNtlPos"])
            ceiling = max(equity, 0) * self.rules["leverage_x100"] / 100 * self.limits.leverage_headroom
            if open_notional + notional > ceiling:
                raise ToolError(f"open notional would reach {open_notional + notional:.2f} USDC; the leverage rule "
                                f"with this agent's headroom allows {ceiling:.2f}")
        self.orders_left -= 1
        tif = {"limit": "Gtc", "post_only": "Alo", "ioc": "Ioc"}[order_type]
        order = {"coin": coin, "side": side, "size": sz, "limit_price": px, "tif": tif, "reduce_only": reduce_only}
        if not self.send:
            return {"status": "not_sent", "order": order}
        answer = self.client.order(self.account, index, side == "buy", px, sz, tif=tif, reduce_only=reduce_only)
        return {"order": order, "answer": answer}

    def cancel_order(self, coin: str, oid: int) -> dict:
        index, _ = self._perp(coin)
        if self.cancels_left <= 0:
            raise ToolError("no cancels left in this session")
        self.cancels_left -= 1
        if not self.send:
            return {"status": "not_sent", "cancel": {"coin": coin, "oid": oid}}
        return {"answer": self.client.cancel(self.account, index, oid)}

    def close_position(self, coin: str) -> dict:
        self._perp(coin)
        position = next((p["position"] for p in self._state().get("assetPositions", [])
                         if p["position"]["coin"] == coin), None)
        size = float(position["szi"]) if position else 0.0
        if size == 0:
            raise ToolError(f"no open {coin} position")
        mid = float(c.info_post({"type": "allMids"})[coin])
        closing_buy = size < 0
        price = mid * (1.02 if closing_buy else 0.98)  # crosses the book; reduce-only caps the size
        return self.place_order(coin, "buy" if closing_buy else "sell", abs(size), price, "ioc", reduce_only=True)

    def graduate(self) -> dict:
        if not self.is_challenge:
            raise ToolError("only a challenge can graduate")
        if self.graduations_left <= 0:
            raise ToolError("graduation was already requested in this session")
        self.graduations_left -= 1
        if not self.send:
            return {"status": "not_sent", "call": "graduate"}
        try:
            receipt = c.transact(self.wallet, self.account, "graduate(bytes32)", ["bytes32"], [os.urandom(32)])
        except Exception as exc:
            if self._errors is None:
                self._errors = _error_names()
            return {"status": "refused_by_contract", "reason": revert_reason(exc, self._errors)}
        return {"status": "sent", "tx": receipt["transactionHash"]}


def trade_tools(desk: Desk) -> list:
    @beta_tool(strict=True)
    def get_account() -> str:
        """The trading account right now: equity, open notional and leverage, positions, open
        orders, the rules and the equity floors they imply, the contract's verdict, the
        challenge target and deadline, and what is left of this session's order budget. Call it
        at the start of every session and again after any order or cancel."""
        return trimmed(desk.account_view(), 6000)

    @beta_tool(strict=True)
    def get_market(coin: str) -> str:
        """Market data for one perp on the account's list: mid, mark and oracle price, hourly
        funding, open interest, 24h volume and change, and the last 24 hourly closes. Call it
        before deciding whether to trade that perp.

        Args:
            coin: Perp name exactly as get_account lists it, for example BTC.
        """
        return trimmed(desk.market_view(coin), 3000)

    @beta_tool(strict=True)
    def place_order(coin: str, side: Literal["buy", "sell"], size: float, limit_price: float,
                    order_type: Literal["limit", "post_only", "ioc"], reduce_only: bool) -> str:
        """Send one order through the pool gateway, signed by this agent's wallet. Price and size
        are rounded to the exchange's steps. Refused before sending if the perp isn't on the
        list, the order is under 10 USDC, over the per-order cap, or would take open notional
        past the agent's headroom under the leverage rule; reduce-only orders skip the last two
        checks. The answer shows what the gateway, the enclave and Hyperliquid said.

        Args:
            coin: Perp name from the account's list.
            side: buy or sell.
            size: Size in coins, for example 0.0005 for BTC.
            limit_price: Limit price in USDC.
            order_type: limit rests on the book, post_only only rests (cancelled if it would
                cross), ioc fills what it can at once and cancels the rest.
            reduce_only: True to only shrink an existing position.
        """
        return trimmed(desk.place_order(coin, side, size, limit_price, order_type, reduce_only))

    @beta_tool(strict=True)
    def cancel_order(coin: str, oid: int) -> str:
        """Cancel one open order by its id, as get_account lists it.

        Args:
            coin: Perp name of the order.
            oid: Order id.
        """
        return trimmed(desk.cancel_order(coin, oid))

    @beta_tool(strict=True)
    def close_position(coin: str) -> str:
        """Close the whole open position in one perp with a reduce-only order that crosses the
        book. Counts as one order.

        Args:
            coin: Perp name of the position.
        """
        return trimmed(desk.close_position(coin))

    tools = [get_account, get_market, place_order, cancel_order, close_position]
    if desk.is_challenge:
        @beta_tool(strict=True)
        def request_graduation() -> str:
            """Ask the challenge contract to pass the challenge. It succeeds only if equity is at
            or above the target, the deadline hasn't passed, no rule is broken and there is no
            open position; otherwise the answer names what is missing. Call it once, when
            get_account shows all four."""
            return trimmed(desk.graduate())

        tools.append(request_graduation)
    return tools


TRADE_SYSTEM = """You trade one account on the Hyperliquid testnet, for a live demo of trading pools. The \
capital is mock USDC, but trade it as if it were real: the demo is about trading well inside rules.

How the account works:
- The account is a smart contract. You reach the market only through an order gateway, which checks \
your wallet's signature and the account's list of perps. The key that signs for the account is held \
in an enclave, and you never see it. The enclave refuses orders above its size and notional caps and \
says so in a signed receipt.
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
- If the gateway, the enclave or the exchange refuses an order, read the reason and don't send the \
same order again unchanged.
- To pass a challenge, close every position once equity is at the target, check get_account, then \
call request_graduation.
- Finish with two or three plain sentences: what you did and why, or why you did nothing."""


# ── picking a pool ───────────────────────────────────────────────────────────────────────

class Shop:
    """The pools that can sell a challenge right now, and one purchase at most."""

    LISTING_LIMIT = 50

    def __init__(self, factory: str, max_price: float, wallet=None, send: bool = True):
        self.factory = to_checksum_address(factory)
        self.max_price = max_price
        self.wallet = wallet
        self.send = send
        self.purchases_left = 1
        self.usdc = to_checksum_address(view(self.factory, "usdc()", "address"))
        self.decimals = view(self.usdc, "decimals()", "uint8")
        universe = c.info_post({"type": "meta"})["universe"]
        self.names = {i: a["name"] for i, a in enumerate(universe)}
        self.offers: dict[str, dict] = {}

    def listing(self) -> list[dict]:
        pools = view(self.factory, "pools()", "address[]")[-self.LISTING_LIMIT:]
        offers = {}
        for raw in pools:
            pool = to_checksum_address(raw)
            if view(pool, "stage()", "uint8") != 0 or not view(pool, "accountReady()", "bool"):
                continue
            if int(view(pool, "challenge()", "address"), 16) != 0:
                continue
            price, capital, target_bps, duration, share_bps, funded = c.call_view(pool, "terms()", [], [], [TERMS])[0]
            spot = c.core_spot_balance(pool, 0)["total"]
            if spot < (capital + funded) * 100:
                continue
            daily, drawdown, leverage, assets = c.call_view(pool, "rules()", [], [], [RULES])[0]
            offers[pool] = {
                "pool": pool,
                "price_usdc": usd(price, self.decimals),
                "challenge_capital_usdc": usd(capital),
                "target_profit_pct": target_bps / 100,
                "days": round(duration / 86400, 2),
                "trader_share_of_profit_pct": share_bps / 100,
                "funded_capital_after_passing_usdc": usd(funded),
                "rules": {"max_daily_loss_pct": daily / 100, "max_drawdown_pct": drawdown / 100,
                          "max_leverage": leverage / 100,
                          "perps": [self.names.get(i, f"#{i}") for i in assets]},
                "_price_units": price,
            }
        self.offers = offers
        return [{k: v for k, v in o.items() if not k.startswith("_")} for o in offers.values()]

    def buy(self, pool: str, price_usdc: float) -> dict:
        pool = to_checksum_address(pool)
        offer = self.offers.get(pool)
        if offer is None:
            raise ToolError("that pool isn't in the current listing; call list_pools first")
        if self.purchases_left <= 0:
            raise ToolError("one challenge per session")
        if abs(offer["price_usdc"] - price_usdc) > 1e-9:
            raise ToolError(f"the pool's price is {offer['price_usdc']} USDC, not {price_usdc}")
        if offer["price_usdc"] > self.max_price:
            raise ToolError(f"{offer['price_usdc']} USDC is over this session's price cap of {self.max_price}")
        self.purchases_left -= 1
        if not self.send:
            return {"status": "not_sent", "pool": pool, "price_usdc": offer["price_usdc"]}
        price = offer["_price_units"]
        allowance = c.call_view(self.usdc, "allowance(address,address)", ["address", "address"],
                                [self.wallet.address, pool], ["uint256"])[0]
        if allowance < price:
            c.transact(self.wallet, self.usdc, "approve(address,uint256)", ["address", "uint256"], [pool, price])
        receipt = c.transact(self.wallet, pool, "buyChallenge()")
        challenge = to_checksum_address(view(pool, "challenge()", "address"))
        return {"status": "bought", "pool": pool, "challenge": challenge, "tx": receipt["transactionHash"]}


def shop_tools(shop: Shop) -> list:
    @beta_tool(strict=True)
    def list_pools() -> str:
        """The pools that can sell a challenge right now, with each one's price, challenge
        capital, profit target, duration, profit share, the capital a passing trader gets, and
        its rules. Call it before choosing."""
        return trimmed(shop.listing(), 8000)

    @beta_tool(strict=True)
    def buy_challenge(pool: str, price_usdc: float) -> str:
        """Buy a challenge from one pool with this agent's wallet. Refused if the pool isn't in the
        latest listing, the price differs from the listing, the price is over the session's cap,
        or a challenge was already bought in this session. Returns the new challenge account.

        Args:
            pool: Pool address from list_pools.
            price_usdc: The price list_pools showed for that pool.
        """
        return trimmed(shop.buy(pool, price_usdc))

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
