"""What an agent may read and do for one trading account, and for buying a challenge, with
the limits applied in code. The Claude Code window that trades the demo calls these through
`python -m agents.client`; `agents.ai_trader` gives the same desk to Claude over the API.

The limits that matter live here and in the gateway, not in a prompt: the account's own perp
list, a notional cap per order, headroom under the leverage rule, a number of orders, cancels
and graduation requests, a price cap and one purchase. Testnet only.
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass

from eth_utils import keccak, to_checksum_address

from spike.hlspike import common as c

from .client import GatewayClient, round_price, round_size

MIN_ORDER_USDC = 10.0
RULES = "(uint16,uint16,uint32,uint32[])"
TERMS = "(uint64,uint64,uint16,uint32,uint16,uint16,uint64)"

# Enum names in declaration order; the tests hold them against the Solidity source.
STATUS = ("None", "Created", "Active", "Breached", "Expired", "Forfeited", "Passed", "Aborted", "Settled")
STAGE = ("Idle", "Challenge", "Funded", "Closing")
BREACH = ("None", "Drawdown", "DailyLoss", "Leverage", "ForbiddenAsset")


def view(addr: str, sig: str, out: str, types=(), args=()):
    return c.call_view(addr, sig, list(types), list(args), [out])[0]


def usd(units: int, decimals: int = 6) -> float:
    return units / 10**decimals


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

class Refused(Exception):
    """A request the desk won't pass on; the message says why."""


def market_mid(coin: str) -> float:
    """The coin's mid on Hyperliquid's testnet now. Missing, unreadable, not positive or not
    finite, it refuses: max(limit, NaN) would quietly be the limit again."""
    mids = c.info_post({"type": "allMids"})
    try:
        mid = float(mids[coin])
    except (KeyError, IndexError, TypeError, ValueError):
        mid = math.nan
    if not math.isfinite(mid) or mid <= 0:
        raise Refused(f"Hyperliquid gave no usable mid for {coin}; nothing was sent")
    return mid


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
            raise Refused(f"{coin} is not on this account's list: {', '.join(sorted(self.perps))}")
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
            _, capital, target_bps, _, ch_share_bps, funded_share_bps, _ = c.call_view(
                self.account, "terms()", [], [], [TERMS])[0]
            deadline = view(self.account, "deadline()", "uint64")
            out["challenge"] = {
                "capital_usdc": usd(capital),
                "target_equity_usdc": round(usd(capital) * (1 + target_bps / 1e4), 2),
                "deadline_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(deadline)) if deadline else None,
                "hours_left": round((deadline - time.time()) / 3600, 1) if deadline else None,
                "trader_share_of_challenge_profit_pct": ch_share_bps / 100,
                "trader_share_of_funded_profit_pct": funded_share_bps / 100,
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
            raise Refused("no orders left in this session")
        try:
            px = round_price(limit_price, size_decimals)
            sz = round_size(size, size_decimals)
        except ValueError as exc:
            raise Refused(str(exc)) from None
        notional = float(px) * float(sz)
        if notional < MIN_ORDER_USDC:
            raise Refused(f"{notional:.2f} USDC is under Hyperliquid's minimum order of {MIN_ORDER_USDC:.0f} USDC")
        if not reduce_only:
            if side != "buy":
                # A sell priced under the market fills at the market: it counts at the mid when
                # that is higher than its limit, as the gateway counts it.
                notional = max(float(px), market_mid(coin)) * float(sz)
            if notional > self.limits.max_notional:
                raise Refused(f"{notional:.2f} USDC is over this session's cap of {self.limits.max_notional} per order")
            summary = self._state()["marginSummary"]
            equity, open_notional = float(summary["accountValue"]), float(summary["totalNtlPos"])
            ceiling = max(equity, 0) * self.rules["leverage_x100"] / 100 * self.limits.leverage_headroom
            if open_notional + notional > ceiling:
                raise Refused(f"open notional would reach {open_notional + notional:.2f} USDC; the leverage rule "
                                f"with this agent's headroom allows {ceiling:.2f}")
        self.orders_left -= 1
        tif = {"limit": "Gtc", "post_only": "Alo", "ioc": "Ioc"}[order_type]
        order = {"coin": coin, "side": side, "size": sz, "limit_price": px, "tif": tif, "reduce_only": reduce_only}
        if not self.send:
            return {"status": "not_sent", "order": order}
        answer = self.client.order(self.account, index, side == "buy", px, sz, tif=tif, reduce_only=reduce_only)
        self._refund_if_ours(answer, "orders_left")
        return {"order": order, "answer": answer}

    def _refund_if_ours(self, answer: dict, counter: str) -> None:
        """Gives the attempt back when the gateway refused for a reason of OURS.

        The counter is spent before the call on purpose: a send that throws, or one the gateway
        cannot resolve, may still have reached Hyperliquid, and an agent that got the attempt back
        would send it twice. `busy` is the one answer with no such doubt -- the gateway's own chain
        reads were rate limited, so it refused before signing anything and nothing left the house.
        Charging a session order for our node's bad minute spends the agent's budget on our
        problem: seen live on 25 Sep 2026, when two of a session's four orders went on `busy`.
        """
        if isinstance(answer, dict) and answer.get("status") == "busy":
            setattr(self, counter, getattr(self, counter) + 1)

    def cancel_order(self, coin: str, oid: int) -> dict:
        index, _ = self._perp(coin)
        if self.cancels_left <= 0:
            raise Refused("no cancels left in this session")
        self.cancels_left -= 1
        if not self.send:
            return {"status": "not_sent", "cancel": {"coin": coin, "oid": oid}}
        answer = self.client.cancel(self.account, index, oid)
        self._refund_if_ours(answer, "cancels_left")
        return {"answer": answer}

    def close_position(self, coin: str) -> dict:
        self._perp(coin)
        position = next((p["position"] for p in self._state().get("assetPositions", [])
                         if p["position"]["coin"] == coin), None)
        size = float(position["szi"]) if position else 0.0
        if size == 0:
            raise Refused(f"no open {coin} position")
        mid = market_mid(coin)
        closing_buy = size < 0
        price = mid * (1.02 if closing_buy else 0.98)  # crosses the book; reduce-only caps the size
        return self.place_order(coin, "buy" if closing_buy else "sell", abs(size), price, "ioc", reduce_only=True)

    def graduate(self) -> dict:
        if not self.is_challenge:
            raise Refused("only a challenge can graduate")
        if self.graduations_left <= 0:
            raise Refused("graduation was already requested in this session")
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
        self.fee_units = view(self.factory, "challengeFee()", "uint256")
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
            (price, capital, target_bps, duration, ch_share_bps, funded_share_bps,
             funded) = c.call_view(pool, "terms()", [], [], [TERMS])[0]
            if c.core_spot_balance(pool, 0)["total"] < view(pool, "capitalNeeded()", "uint64"):
                continue
            daily, drawdown, leverage, assets = c.call_view(pool, "rules()", [], [], [RULES])[0]
            offers[pool] = {
                "pool": pool,
                "price_usdc": usd(price, self.decimals),
                "platform_fee_usdc": usd(self.fee_units, self.decimals),
                "total_to_pay_usdc": usd(price + self.fee_units, self.decimals),
                "challenge_capital_usdc": usd(capital),
                "target_profit_pct": target_bps / 100,
                "days": round(duration / 86400, 2),
                "trader_share_of_challenge_profit_pct": ch_share_bps / 100,
                "trader_share_of_funded_profit_pct": funded_share_bps / 100,
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
            raise Refused("that pool isn't in the current listing; call list_pools first")
        if self.purchases_left <= 0:
            raise Refused("one challenge per session")
        if abs(offer["price_usdc"] - price_usdc) > 1e-9:
            raise Refused(f"the pool's price is {offer['price_usdc']} USDC, not {price_usdc}")
        if offer["total_to_pay_usdc"] > self.max_price:
            raise Refused(f"{offer['total_to_pay_usdc']} USDC with the platform fee is over this session's "
                            f"cap of {self.max_price}")
        self.purchases_left -= 1
        if not self.send:
            return {"status": "not_sent", "pool": pool, "total_usdc": offer["total_to_pay_usdc"]}
        # The pool pulls the price and the fee; approve exactly both, so a fee raised after
        # the listing makes the purchase fail instead of costing more.
        total = offer["_price_units"] + self.fee_units
        c.transact(self.wallet, self.usdc, "approve(address,uint256)", ["address", "uint256"], [pool, total])
        receipt = c.transact(self.wallet, pool, "buyChallenge()")
        challenge = to_checksum_address(view(pool, "challenge()", "address"))
        return {"status": "bought", "pool": pool, "challenge": challenge, "tx": receipt["transactionHash"]}
