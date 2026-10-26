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
from decimal import Decimal, InvalidOperation
from typing import Any

from eth_utils import keccak, to_checksum_address

from gateway.assets import MAIN_DEX, dex_index_of, order_asset, parse_perp_dexs
from gateway.chain import decode_rules
from spike.hlspike import common as c

from .client import GatewayClient, round_price, round_size

MIN_ORDER_USDC = 10.0
# An order whose answer settles nothing -- a 5xx, a page that is not the gateway's, a connection that
# broke -- stays spent until Hyperliquid's own list of the account's orders can say whether it got
# there. That list is final once no copy of the request can still be submitted: the gateway takes a
# request only before its `expiresAt` (45 s after it was signed, agents/client.py) and then places
# the stop and the take before it submits, so three minutes after the attempt is past both.
SETTLE_AFTER_S = 180
# Hyperliquid's historicalOrders gives the most recent 2,000 orders and no more. A full answer whose
# oldest order is newer than an attempt cannot say that attempt is absent.
HISTORY_LIMIT = 2000
RULES = "(uint16,uint16,uint32,uint32[])"
# A v3 account's rules: each asset's own leverage cap appended, in the order of its assets.
RULES_V3 = "(uint16,uint16,uint32,uint32[],uint32[])"


def account_rules(account: str) -> tuple:
    """rules() of an account of either kind, as five fields: the caps of each asset a v3 account has
    of its own, `()` for one before v3. The answer says which it is (`decode_rules`): read as four, a
    v3 account's answer would drop its caps without a word (AUDIT В6-2, 21 Oct 2026)."""
    return decode_rules(c.eth_call(account, keccak(text="rules()")[:4]), RULES, RULES_V3)
TERMS = "(uint64,uint64,uint16,uint32,uint16,uint16,uint64)"

# Enum names in declaration order; the tests hold them against the Solidity source.
STATUS = ("None", "Created", "Active", "Breached", "Expired", "Forfeited", "Passed", "Aborted", "Settled")
STAGE = ("Idle", "Challenge", "Funded", "Closing", "PassedAwaitingKey")
BREACH = ("None", "Drawdown", "DailyLoss", "Leverage", "ForbiddenAsset")
CANCEL = "(uint32,uint64)[]"   # the same shape ops/keeper.py sends


def view(addr: str, sig: str, out: str, types=(), args=()):
    return c.call_view(addr, sig, list(types), list(args), [out])[0]


def usd(units: int, decimals: int = 6) -> float:
    return units / 10**decimals


# ── contract errors, for readable reverts ────────────────────────────────────────────────

# Every custom error in the ABI of `ChallengeAccount`, of `Pool` and of `AccountOps`, the library
# both link to, whose errors reach a caller as the account's, by its signature. The table
# is carried here because a clone has no ABI until `forge build` has run, and a refusal has to be
# named without one. Read from the build output, the names took the client down with "run `forge
# build` first" on the first contract refusal in a clone that had installed only the Python side:
# no JSON, and for a transaction that was mined and reverted, no word that it had gone out
# (5 October 2026). `scripts/abi-check.py` holds this table against the compiled contracts.
CONTRACT_ERRORS = (
    "AlreadyCheckpointed()",
    "BadStage(uint8)",
    "BadStatus(uint8)",
    "CapitalArrived()",
    "CoreWriterLib__CannotSelfTransfer()",
    "InvalidInitialization()",
    "KeyAvailable()",
    "KeyWorks()",
    "KeySpoiled()",
    "NoBreach()",
    "NotAllowed()",
    "NotChallenge()",
    "NotEnoughCapital(uint64,uint64)",
    "NotFlat()",
    "NotInitializing()",
    "NotOnDex()",
    "NotOwner()",
    "NotOwnerRun()",
    "NotReady()",
    "NotRenewable(uint64)",
    "NotStartedOnCore()",
    "NotStopped()",
    "NotTrader()",
    "OutsideCheckpointWindow()",
    "PrecompileLib__AccountMarginSummaryPrecompileFailed()",
    "PrecompileLib__CoreUserExistsPrecompileFailed()",
    "PrecompileLib__MarkPxPrecompileFailed()",
    "PrecompileLib__PerpAssetInfoPrecompileFailed()",
    "PrecompileLib__Position2PrecompileFailed()",
    "PrecompileLib__SpotBalancePrecompileFailed()",
    "PrecompileLib__WithdrawablePrecompileFailed()",
    "RekeyClosed()",
    "RuleBroken(uint8)",
    "SafeCastOverflowedUintDowncast(uint8,uint256)",
    "SafeERC20FailedOperation(address)",
    "SalesClosed()",
    "TargetNotMet(int64,int256)",
    "TooEarly()",
    "TooLate()",
    "TooManyAssets()",
    "TooManyCancels()",
    "UnsupportedSizeDecimals(uint32,uint8)",
    "WithdrawnThisBlock()",
)


def _error_names() -> dict[str, str]:
    return {"0x" + keccak(text=sig)[:4].hex(): sig.split("(")[0] for sig in CONTRACT_ERRORS}


def revert_reason(exc: Exception, names: dict[str, str]) -> str:
    text = str(exc)
    for data in re.findall(r"0x[0-9a-fA-F]{8,}", text):
        name = names.get(data[:10].lower())
        if name:
            return name
    return text[:200]

class Refused(Exception):
    """A request the desk won't pass on; the message says why."""


def _settles_nothing(answer: Any) -> bool:
    """An answer after which the order may or may not be on Hyperliquid: a 5xx (the gateway's
    `venue_unreachable` and `uncertain`, or nginx's and the CDN's), a body that is not the gateway's
    JSON, or `replayed`, whose nonce a copy that may have reached Hyperliquid took."""
    if not isinstance(answer, dict):
        return True
    return answer.get("http", 0) >= 500 or answer.get("status") == "bad_response" or answer.get("code") == "replayed"


def _oids(answer: Any) -> list[int]:
    """The oids Hyperliquid gave in the gateway's answer to an order (`venue`): resting or filled."""
    try:
        statuses = answer["venue"]["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return []
    return [s[k]["oid"] for s in statuses if isinstance(s, dict) for k in ("resting", "filled")
            if isinstance(s.get(k), dict) and isinstance(s[k].get("oid"), int)]


def _oid(h: Any) -> int | None:
    o = h.get("order") if isinstance(h, dict) else None
    return o.get("oid") if isinstance(o, dict) and isinstance(o.get("oid"), int) else None


def _stamp(h: Any) -> int:
    o = h.get("order") if isinstance(h, dict) else None
    try:
        return int(o.get("timestamp", 0)) if isinstance(o, dict) else 0
    except (TypeError, ValueError):
        return 0


def _same_order(h: Any, d: dict) -> bool:
    """Is the entry `h` of Hyperliquid's order history the doubtful attempt `d`: same coin, side,
    price and size, placed after the attempt began (five seconds of clock skew allowed). An entry of
    another shape is no match, and never an error: this runs before every command of the day."""
    o = h.get("order") if isinstance(h, dict) else None
    if not isinstance(o, dict):
        return False
    try:
        return (o.get("coin") == d["coin"] and o.get("side") == d["side"]
                and Decimal(str(o.get("limitPx"))) == Decimal(d["px"])
                and Decimal(str(o.get("origSz", o.get("sz")))) == Decimal(d["sz"])
                and int(o.get("timestamp", 0)) >= (d["at"] - 5) * 1000)
    except (InvalidOperation, TypeError, ValueError):
        return False


def _on_dex(body: dict, dex: str) -> dict:
    """An info request for one perp dex: Hyperliquid's own is asked with no `dex` field, as before HIP-3."""
    return body if dex == MAIN_DEX else {**body, "dex": dex}


def perp_table(assets) -> dict[int, dict]:
    """Every perp of Hyperliquid's own dex and of each dex one of `assets` is on, by the number an order
    uses for it (100000 + dex * 10000 + index on a HIP-3 dex): its coin ("xyz:XYZ100"), its size
    decimals, its dex's name and its index in that dex's `meta`. A dex no asset is on is not read."""
    wanted = {dex_index_of(a) for a in assets} | {0}
    dex_names = {0: MAIN_DEX}
    if wanted != {0}:
        dex_names.update({i: n for n, i in parse_perp_dexs(c.info_post({"type": "perpDexs"})).items()})
    missing = sorted(wanted - set(dex_names))
    if missing:  # before any meta is read: nothing is spent on a list that can't be named whole
        raise SystemExit(f"Hyperliquid lists no perp dex {missing[0]}, which this account's list names")
    table = {}
    for d in sorted(wanted):
        for i, a in enumerate(c.info_post(_on_dex({"type": "meta"}, dex_names[d]))["universe"]):
            table[order_asset(d, i)] = {"coin": a["name"], "sz_decimals": a["szDecimals"], "dex": dex_names[d], "index": i}
    return table


def market_mid(coin: str) -> float:
    """The coin's mid on Hyperliquid's testnet now, from its own dex ("xyz:GOLD" from dex xyz). Missing,
    unreadable, not positive or not finite, it refuses: max(limit, NaN) would quietly be the limit again."""
    mids = c.info_post(_on_dex({"type": "allMids"}, coin.partition(":")[0] if ":" in coin else MAIN_DEX))
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
    """What one session may do. The per-order cap is a SHARE of the rule's own ceiling rather
    than a number, so it moves with the capital it is protecting.

    A flat 100 USDC was sized for a 70-USDC challenge. On a funded stage of 700 the same number
    left the agent at 0.57x of a 5x rule -- ten times tamer than the audition it had just passed,
    which is not a cap on recklessness but a cap on the product.

    The share is 0.4 against a headroom of 0.8, so two orders reach the whole exposure the headroom
    allows and two of the four daily orders are left for a correction. 0.5 would also take two, but
    it lets a single order commit 62% of the headroom where 0.4 commits exactly half: same order
    budget, smaller mistake.

    `gateway_max_notional` is what the gateway said it would take, from `/v1/health`. The share can
    exceed it on large capital -- 0.4 of 3500 is 1400 against the demo signer's 400 -- and the gateway
    refuses an order over its cap before anything is sent. That answer carries `sent: false`, so the
    attempt comes back, but the round trip is wasted and the agent's plan comes up short. So the cap
    is the SMALLER of the two.
    """
    gateway_max_notional: float = 400.0
    max_order_share_of_rule: float = 0.4
    max_orders: int = 4
    leverage_headroom: float = 0.8  # new exposure stays under this share of the leverage rule

    def order_cap(self, equity: float, leverage_x100: int) -> float:
        """The most one order may carry, given the account's equity now."""
        by_rule = max(equity, 0.0) * leverage_x100 / 100 * self.max_order_share_of_rule
        return min(by_rule, self.gateway_max_notional)


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
        self.stops_left = 1
        self.graduations_left = 1
        self.forfeits_left = 1
        self.is_challenge = view(self.factory, "isChallenge(address)", "bool", ["address"], [self.account])
        if not self.is_challenge and not view(self.factory, "isPool(address)", "bool", ["address"], [self.account]):
            raise SystemExit(f"{self.account} is not an account of this factory")
        daily, drawdown, leverage, assets, caps = account_rules(self.account)
        self.rules = {"daily_loss_bps": daily, "drawdown_bps": drawdown, "leverage_x100": leverage,
                      "asset_leverage_x100": dict(zip(assets, caps))}
        # The account's perps by order number, on every dex its list is on (HIP-3), and the dexes read.
        self.table = perp_table(assets)
        self.dexes = sorted({t["dex"] for t in self.table.values()})
        self.index_of = {t["coin"]: a for a, t in self.table.items()}
        self.perps = {self.table[a]["coin"]: (a, self.table[a]["sz_decimals"]) for a in assets if a in self.table}
        self._errors: dict[str, str] | None = None
        # Orders spent on an answer that settled nothing, until Hyperliquid says (`settle_doubts`).
        self.doubtful_orders: list[dict] = []
        # Today's orders already accounted for, by Hyperliquid's oid: those the gateway's answer named,
        # and those a doubt was settled by. Neither may settle another doubt. Kept with the counters.
        self.known_oids: list[int] = []

    # reads

    def _perp(self, coin: str) -> tuple[int, int]:
        if coin not in self.perps:
            raise Refused(f"{coin} is not on this account's list: {', '.join(sorted(self.perps))}")
        return self.perps[coin]

    def _state(self) -> dict:
        """The account on every dex its list is on: positions side by side, and the equity, the notional
        and the margin summed (each dex's clearinghouse holds a part when balances are separate). Each
        dex's own equity is kept beside the sum, so a dex the capital hasn't reached yet can be said."""
        states = [c.info_post(_on_dex({"type": "clearinghouseState", "user": self.account}, d)) for d in self.dexes]
        self._by_dex = {d: float(s["marginSummary"]["accountValue"]) for d, s in zip(self.dexes, states)}
        if len(states) == 1:
            return states[0]
        summed = {k: str(sum(float(s["marginSummary"].get(k) or 0) for s in states))
                  for k in ("accountValue", "totalNtlPos", "totalMarginUsed")}
        return {**states[0], "marginSummary": {**states[0]["marginSummary"], **summed},
                "assetPositions": [p for s in states for p in s.get("assetPositions", [])]}

    def account_view(self) -> dict:
        state = self._state()
        by_dex = self._by_dex
        summary = state["marginSummary"]
        equity = float(summary["accountValue"])
        notional = float(summary["totalNtlPos"])
        positions = [p["position"] for p in state.get("assetPositions", [])]
        held_outside = sorted({self.index_of[p["coin"]] for p in positions
                               if p["coin"] in self.index_of and p["coin"] not in self.perps})
        verdict = view(self.account, "violation(uint32[])", "uint8", ["uint32[]"], [held_outside])
        base = usd(view(self.account, "drawdownBase()", "int64"))
        day_start = usd(view(self.account, "dayStartEquity()", "int64"))
        # The same trap `app/lib/verdict.js` was fixed for on 24 September, and nobody carried the
        # fix across to here: a settled account has handed its money back, so a live `violation()`
        # reads a hundred per cent below where it started and answers Drawdown whatever the stop
        # was actually for. The trader bot found it again on 1 October 2026, on a challenge the
        # contract had stopped for Leverage. The recorded reason wins; an account that has finished
        # trading with none recorded says so; only a live account gets a live reading.
        number = view(self.account, "status()" if self.is_challenge else "stage()", "uint8")
        recorded = view(self.account, "breachReason()" if self.is_challenge else "fundedEndReason()", "uint8")
        # A challenge past Active has finished. A pool needs `cutBlock` as well as the stage: a
        # funded stage that ended CLEANLY records `fundedEndReason` None, which is byte for byte
        # what a pool that never funded anyone records -- and the reservation a pool gives back when
        # nobody passes its challenge is exactly that (`Pool.sol` retires the spare key without
        # going through `_cutAgent`, the only writer of `cutBlock`, `RuledAccount.sol:169-178`).
        # So Idle with a cut block is a funded stage that is over; Idle without one never had one.
        # `app/lib/verdict.js:pastFundedStage` is where this rule already lived; this is the same
        # rule, not a second one.
        #
        # Measured on 1 October 2026, against the review bot's reading: a live `violation()` on an
        # idle POOL answers None, not Drawdown, because `fundedStart` is cleared on close and
        # `drawdownBase()` is then 0. So what this fixes is the WORD -- a pool that finished
        # cleanly was called "inside the rules" instead of finished -- and not a false alarm. The
        # Drawdown trap is the challenge's, whose drawdown base stays at its starting capital.
        if self.is_challenge:
            finished = number > 2
        else:
            finished = number == 3 or (number == 0 and view(self.account, "cutBlock()", "uint64") > 0)
        if recorded:
            said = f"stopped for {BREACH[recorded]} — what the contract recorded"
        elif finished:
            said = "finished with no rule broken"
        elif self.is_challenge and number == 1:
            # Created: bought, and not activated. Until its capital arrives the equity reads 0
            # against a drawdown base that is already the capital, so a live `violation()` answers
            # Drawdown. The contract judges nothing there -- `breach` is for an Active account
            # alone -- and a trader reading "Drawdown" on a challenge they have just bought is
            # reading a stop that cannot happen.
            said = "not active yet: nothing is judged before activation"
        else:
            said = "inside the rules" if verdict == 0 else BREACH[verdict]
        out = {
            "account": self.account,
            "kind": "challenge" if self.is_challenge else "funded pool",
            "state": STATUS[number] if self.is_challenge else STAGE[number],
            "equity_usdc": equity,
            "open_notional_usdc": notional,
            "margin_used_usdc": float(summary["totalMarginUsed"]),
            "leverage_now": round(notional / equity, 3) if equity > 0 else None,
            "rules": {
                "max_daily_loss_pct": self.rules["daily_loss_bps"] / 100,
                "max_drawdown_pct": self.rules["drawdown_bps"] / 100,
                "max_leverage": self.rules["leverage_x100"] / 100,
            # A v3 account's own cap for each asset; none before v3, where the account's alone holds.
            "max_leverage_by_asset": {self.table[a]["coin"] if a in self.table else f"#{a}": cap / 100
                                      for a, cap in self.rules["asset_leverage_x100"].items()},
                "perps": sorted(self.perps),
            },
            "limits_now": {
                "equity_floor_drawdown_usdc": round(base * (1 - self.rules["drawdown_bps"] / 1e4), 2),
                "equity_floor_today_usdc": (round(day_start * (1 - self.rules["daily_loss_bps"] / 1e4), 2)
                                            if day_start > 0 else None),
                "max_open_notional_by_rule_usdc": round(max(equity, 0) * self.rules["leverage_x100"] / 100, 2),
            },
            # AUDIT В4-2: the stage starts in two steps, and the capital reaches a HIP-3 dex after the
            # account is active. A dex of the list with nothing on it yet is a state to read, not an error.
            "equity_by_dex_usdc": {d or "hyperliquid": value for d, value in by_dex.items()},
            "capital_not_yet_on": [d for d, value in by_dex.items() if d != MAIN_DEX and value == 0],
            "contract_verdict": said,
            "positions": [{
                "coin": p["coin"], "size": float(p["szi"]), "entry_price": float(p["entryPx"] or 0),
                "unrealized_pnl_usdc": float(p["unrealizedPnl"]),
                "liquidation_price": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
            } for p in positions[:20]],
            "open_orders": [{
                "coin": o["coin"], "side": "buy" if o["side"] == "B" else "sell",
                "limit_price": float(o["limitPx"]), "size": float(o["sz"]), "oid": o["oid"],
            } for o in [o for d in self.dexes
                        for o in c.info_post(_on_dex({"type": "openOrders", "user": self.account}, d))][:30]],
            # Computed from the equity above, not a constant: this is the field an agent should
            # read before sizing an order, and it has to be the number the order path will use.
            "session": {"orders_left": self.orders_left, "cancels_left": self.cancels_left,
                        "orders_in_doubt": len(self.doubtful_orders),
                        "max_order_notional_usdc": round(
                            self.limits.order_cap(equity, self.rules["leverage_x100"]), 2)},
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
        where = self.table[index]
        meta, ctxs = c.info_post(_on_dex({"type": "metaAndAssetCtxs"}, where["dex"]))
        ctx = ctxs[where["index"]]
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
            "max_exchange_leverage": meta["universe"][where["index"]]["maxLeverage"],
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
        # The minimum is checked on an order that may open or grow a position. One that only reduces
        # a position is not refused here for being small: a position that has shrunk under the
        # minimum -- the price moved, or part of it was closed -- would otherwise have no way out
        # through this client at all, and `close` is how a trader gets flat before `graduate`.
        # Whether Hyperliquid takes such an order is Hyperliquid's to say. Its documentation gives
        # the 10 USDC minimum and names no exception for reduce-only orders (read 4 October 2026),
        # and none has been sent from here: if it refuses, the answer comes back as the venue's.
        if notional < MIN_ORDER_USDC and not reduce_only:
            raise Refused(f"{notional:.2f} USDC is under Hyperliquid's minimum order of {MIN_ORDER_USDC:.0f} USDC")
        if not reduce_only:
            if side != "buy":
                # A sell priced under the market fills at the market: it counts at the mid when
                # that is higher than its limit, as the gateway counts it.
                notional = max(float(px), market_mid(coin)) * float(sz)
            # The per-order cap now depends on equity, so it is checked AFTER the read the leverage
            # ceiling needs anyway -- one state read serves both, as it did before.
            summary = self._state()["marginSummary"]
            equity, open_notional = float(summary["accountValue"]), float(summary["totalNtlPos"])
            cap = self.limits.order_cap(equity, self.rules["leverage_x100"])
            if notional > cap:
                raise Refused(f"{notional:.2f} USDC is over this session's cap of {cap:.2f} per order "
                              f"({self.limits.max_order_share_of_rule:g} of the leverage rule on "
                              f"{equity:.2f} of equity, and no more than the gateway's "
                              f"{self.limits.gateway_max_notional:.2f})")
            ceiling = max(equity, 0) * self.rules["leverage_x100"] / 100 * self.limits.leverage_headroom
            if open_notional + notional > ceiling:
                raise Refused(f"open notional would reach {open_notional + notional:.2f} USDC; the leverage rule "
                                f"with this agent's headroom allows {ceiling:.2f}")
        self.orders_left -= 1
        tif = {"limit": "Gtc", "post_only": "Alo", "ioc": "Ioc"}[order_type]
        order = {"coin": coin, "side": side, "size": sz, "limit_price": px, "tif": tif, "reduce_only": reduce_only}
        if not self.send:
            return {"status": "not_sent", "order": order}
        doubt = {"coin": coin, "side": "B" if side == "buy" else "A", "px": str(px), "sz": str(sz), "at": time.time()}
        try:
            answer = self.client.order(self.account, index, side == "buy", px, sz, tif=tif, reduce_only=reduce_only)
        except Exception:
            self.doubtful_orders.append(doubt)  # the request may have got through before it broke
            raise
        if not self._refund_if_ours(answer, "orders_left") and _settles_nothing(answer):
            self.doubtful_orders.append(doubt)
        self.known_oids += [o for o in _oids(answer) if o not in self.known_oids]
        return {"order": order, "answer": answer}

    def settle_doubts(self) -> list[dict]:
        """Gives back each doubtful order Hyperliquid never got, once its answer is final.

        Spending the attempt before the call stays the rule (`_refund_if_ours`): giving it back while
        the order may still be on its way is how an agent sends one twice. What changes is that a
        doubt does not stay a charge for ever. SETTLE_AFTER_S after the attempt, the account's own
        order history on Hyperliquid is final for it: an order there with the same coin, side, price
        and size, placed after the attempt, keeps it spent; none gives it back. A history that cannot
        be read settles nothing, and the doubt waits for the next call. Seen live on 19 and 26 Oct
        2026: a gateway restart answered 502, nothing reached Hyperliquid, and the agent lost an order.
        """
        due = [d for d in self.doubtful_orders if time.time() - d["at"] >= SETTLE_AFTER_S]
        if not due:
            return []
        try:
            answers = [c.info_post(_on_dex({"type": "historicalOrders", "user": self.account}, d))
                       for d in self.dexes]
        except Exception:
            return []
        history = [h for a in answers for h in a]
        # A full answer is complete only back to its oldest order (HISTORY_LIMIT).
        complete_from = max((min((_stamp(h) for h in a), default=0) for a in answers if len(a) >= HISTORY_LIMIT),
                            default=0)
        settled = []
        for d in due:
            if (d["at"] - 5) * 1000 < complete_from:
                continue
            # One order on Hyperliquid answers for one attempt, in this call and in every later one:
            # an oid once taken, or named by the gateway's answer to an order of ours, is not free.
            at = next((i for i, h in enumerate(history)
                       if _oid(h) not in self.known_oids and _same_order(h, d)), None)
            got = at is not None
            if got:
                oid = _oid(history.pop(at))
                if oid is not None:
                    self.known_oids.append(oid)
            else:
                self._refund("orders_left")
            self.doubtful_orders.remove(d)
            settled.append({**d, "reached_hyperliquid": got})
        return settled

    def _refund_if_ours(self, answer: dict, counter: str) -> bool:
        """Gives the attempt back when the gateway says the action was not submitted.

        The counter is spent before the call on purpose: a send that throws, or one the gateway
        cannot resolve, may still have reached Hyperliquid, and an agent that got the attempt back
        would send it twice. An answer with `sent: false` has no such doubt: the gateway says the
        action never left it (gateway/server.py, `not_sent`). Every refusal before the submit carries
        it -- a check the order failed, a chain read or the signer that failed, the stop and the take
        that could not be placed, its own chain reads rate limited (`busy`, which older gateways
        answer without the field) -- except `replayed`, whose nonce a copy that may have reached
        Hyperliquid has taken. Answers after the submit stay charged. Charging a session order for our
        failure spends the agent's budget on our problem: seen live on 25 Sep 2026, when two of a
        session's four orders went on `busy`, and on 19 Oct 2026, when two went on answers the CDN
        had turned into its own HTML page.
        """
        if isinstance(answer, dict) and (answer.get("sent") is False or answer.get("status") == "busy"):
            self._refund(counter)
            return True
        return False

    def _refund(self, counter: str) -> None:
        """Gives one attempt back, and there is exactly one rule for when.

        Only an answer or an exception that proves NOTHING LEFT THE HOUSE: the gateway's `sent:
        false` (and `busy`, from a gateway older than that field), where it refused before the
        action was submitted, and `c.NotSent`, where the node never received the raw transaction. Anything else may have reached
        Hyperliquid or the mempool whatever it said, and an attempt returned there is one sent
        twice. Two detectors, one rule -- keep them pointing at this.
        """
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

    def stop_funded(self) -> dict:
        """Ends the funded stage without a breach, which `Pool.stopFunded` lets the trader do.

        The cancel and forbidden-asset lists go in empty. They are the keeper's to fill: it reads
        the account's open orders each pass and sends them with its own settle step, and a list
        this side guesses at would only be a second, staler copy. What empty costs is one more
        keeper pass before the resting orders are gone, and nothing else.
        """
        if self.is_challenge:
            raise Refused("stopFunded is for the pool, not the challenge; pass the pool's address")
        if self.stops_left <= 0:
            raise Refused("the funded stage was already stopped in this session")
        self.stops_left -= 1
        if not self.send:
            return {"status": "not_sent", "call": "stopFunded"}
        return self._spend("stops_left", f"stopFunded({CANCEL},uint32[],bytes32)",
                           [CANCEL, "uint32[]", "bytes32"], [[], [], os.urandom(32)])

    def forfeit(self) -> dict:
        """Walks away from the challenge, which `ChallengeAccount.forfeit` lets its trader alone do.

        As with `stop_funded`, the cancel and forbidden-asset lists go in empty: the keeper reads the
        account's open orders and sends them with its own settle steps. The contract refuses anyone
        but the trader at the gas estimate, which gives the attempt back. Once sent, the challenge's
        status is read back, so the caller sees `Forfeited` without another command. Added 19 October
        2026: the live challenge's agent was gone from Hyperliquid, and walking away was its trader's
        only way to a new challenge.
        """
        if not self.is_challenge:
            raise Refused("forfeit is for a challenge; a funded pool's stage ends with stop-funded")
        if self.forfeits_left <= 0:
            raise Refused("this challenge was already forfeited in this session")
        self.forfeits_left -= 1
        if not self.send:
            return {"status": "not_sent", "call": "forfeit"}
        out = self._spend("forfeits_left", f"forfeit({CANCEL},uint32[],bytes32)",
                          [CANCEL, "uint32[]", "bytes32"], [[], [], os.urandom(32)])
        if out.get("status") == "sent":
            # The transaction has gone: a failed read now must not turn it into an error the trader
            # would act on. The attempt stays spent either way (review, #158).
            try:
                out["challenge_status"] = STATUS[view(self.account, "status()", "uint8")]
            except Exception:
                out["challenge_status"] = "unavailable"
        return out

    def graduate(self) -> dict:
        if not self.is_challenge:
            raise Refused("only a challenge can graduate")
        if self.graduations_left <= 0:
            raise Refused("graduation was already requested in this session")
        self.graduations_left -= 1
        if not self.send:
            return {"status": "not_sent", "call": "graduate"}
        return self._spend("graduations_left", "graduate(bytes32)", ["bytes32"], [os.urandom(32)])

    def _spend(self, counter: str, signature: str, types: list[str], args: list) -> dict:
        """Send one call out of a small budget, and give the attempt back only when the
        transaction provably never left.

        The budget is there so a bot cannot loop: one graduation a day, one stop a session. But a
        contract's refusal surfaces at the GAS ESTIMATE, before anything is broadcast, and burning
        the attempt there spends the budget on a call that never happened. On a stand with a
        one-day term one premature `graduate` is the whole day's right to pass; on a funded stage
        it is the only stop. `c.NotSent` is the one exception that says nothing reached the node
        (`spike/hlspike/common.py`), and it is the only one that gets the attempt back.

        Past the broadcast the attempt stays spent even when the error reads like a refusal: the
        transaction may be in the mempool whatever the node answered, and a second one let through
        there could land on top of the first.

        `attempt_returned` says which happened, because the caller is a program deciding whether
        it may try again today -- "refused" alone does not answer that.
        """
        try:
            receipt = c.transact(self.wallet, self.account, signature, types, args)
        except c.NotSent as exc:
            self._refund(counter)
            return {**self._refusal(exc), "attempt_returned": True}
        except c.Reverted as exc:
            return {**self._refusal(exc, by_contract=True), "attempt_returned": False}
        except Exception as exc:
            return {**self._refusal(exc), "attempt_returned": False}
        return {"status": "sent", "tx": receipt["transactionHash"]}

    def _refusal(self, exc: Exception, by_contract: bool | None = None) -> dict:
        """The status and the reason for a call that did not go through.

        `refused_by_contract` where the contract actually said no: a custom error of ours decoded
        out of the gas estimate, or `c.Reverted` -- mined and rejected, whose receipt carries no
        revert data to name. Everything else is `failed`: a node refusing the read before the
        estimate, a timeout after the send. Calling those a contract refusal tells a reader the
        contract said no when it was never asked (review, 3 October 2026). `attempt_returned` was
        already right either way; it was the word that misled.
        """
        if self._errors is None:
            self._errors = _error_names()
        reason = revert_reason(exc, self._errors)
        said_no = by_contract if by_contract is not None else reason in self._errors.values()
        return {"status": "refused_by_contract" if said_no else "failed", "reason": reason}



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
        self.names = {a: t["coin"] for a, t in perp_table(set()).items()}
        self._unread_dexes: set[int] = set()  # dexes whose names could not be read for this listing
        self.offers: dict[str, dict] = {}

    def _name(self, asset: int) -> str:
        """A perp's coin, its dex's names read the first time one of them is asked (HIP-3)."""
        dex = dex_index_of(asset)
        if asset not in self.names and dex != 0 and dex not in self._unread_dexes:
            try:
                self.names.update({a: t["coin"] for a, t in perp_table({asset}).items()})
            except SystemExit:
                # Not asked again for this listing; a new listing (a new Shop) asks once more.
                self._unread_dexes.add(dex)
        return self.names.get(asset, f"#{asset}")

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
            daily, drawdown, leverage, assets, caps = account_rules(pool)
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
                          "max_leverage_by_asset": {self._name(i): cap / 100 for i, cap in zip(assets, caps)},
                          "perps": [self._name(i) for i in assets]},
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
        try:
            c.transact(self.wallet, self.usdc, "approve(address,uint256)", ["address", "uint256"], [pool, total])
            receipt = c.transact(self.wallet, pool, "buyChallenge()")
        except c.NotSent as exc:
            # The one purchase a day is spent before the send so a bot cannot loop, and it comes back
            # only here, under the rule `Desk._refund` states: `c.NotSent` proves nothing reached the
            # node, so nothing was bought. A wallet short of the price and the fee ends exactly here,
            # at the gas estimate of `buyChallenge`, and used to lose its whole day for it. Past the
            # broadcast the purchase stays spent: it may be in the mempool whatever the error says.
            self.purchases_left += 1
            raise Refused("the purchase never reached the node, so nothing was bought and today's purchase "
                          f"is not spent: {str(exc)[:200]}") from exc
        challenge = to_checksum_address(view(pool, "challenge()", "address"))
        return {"status": "bought", "pool": pool, "challenge": challenge, "tx": receipt["transactionHash"]}
