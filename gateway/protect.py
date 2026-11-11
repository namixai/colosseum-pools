"""The stop and the take the gateway keeps on Hyperliquid for every position an account may hold.

A pool's rules are enforced by the contracts after the fact: someone sees a broken rule and calls
`breach`, and HyperCore closes the positions a few seconds later. In a fast market that is too
late. So the gateway also leaves the closing to Hyperliquid itself: before it submits an order that
may open or grow a position, it makes sure the account has, for that asset and that direction,

- a STOP just before the rule line. The line is the mark price at which the account's equity would
  reach the nearest rule, the static drawdown floor or the day's loss floor, whichever is higher;
  the stop fires STOP_BUFFER_BPS (6) of that price earlier, so that the close at market, its taker
  fee and its slippage paid, lands on the floor or inside it; and
- a TAKE at the target: in a challenge, the mark price at which equity reaches the pass target;
  in a funded stage, which has no target of its own, at most one challenge target (`targetBps`
  of the equity at the moment) away.

Both are Hyperliquid position TP/SL orders: reduce-only, triggered by the mark price, executed as
market orders, and sized 0, which Hyperliquid reads as "the whole position", however large it
grows. Measured on testnet (spike/tpsl_probe.py, 28 Sep 2026): they can be placed before the
position exists and wait for it; several can stand side by side; Hyperliquid removes every one of
them when the position closes; `batchModify` moves one in a single action.

With more than one position the budget (equity minus the floor) is shared in proportion to the
notional of each: every stop sits the same fraction of its mark away, so if all of them are hit
together the account loses exactly the budget. Orders resting on the book that could open or grow
a position count as filled at the mark, which never places a stop looser than the one their fills
would need. The take is split the same way.

The trader may move a stop nearer the mark, never away from it, and may move a take anywhere
between the mark and the target. Cancelling either is refused. A background sweep repeats the
check, so a position that opens later, from an order that rested, gets the same protection.

A challenge's target is an amount, so its take has to keep up with everything the account pays while
the position is open, and funding is paid every hour. The sweep carries a take the GATEWAY placed
out to the line once it stands short of it; a take the TRADER moved stays where the trader put it,
short or not, because taking the profit early is theirs to decide. Only for an account whose
exposure is one position on one asset: with a second asset, or with an order resting that would
grow the position, the line moves with the price. A funded stage's take is placed and then left
where it is. Measured on 3 October 2026: a
take placed at 85378 stood there for six hours while funding took 0.178665 USDC, fired, and left
the account at 70.204304 against a target of 70.28.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from typing import Any, Callable, Protocol

from .assets import (ALWAYS_FLAT_OVER_WEEKEND, dex_index_of, hip3_closed, order_asset, precompile_index, weekend_cancel_due,
                     weekend_close_due)
from .checks import GatewayError, check_market

BPS = Decimal(10_000)
# What closing at market costs, in basis points of the notional, each number written here once with
# where it comes from; the take's allowance and the stop's buffer below are built from them.
TAKER_FEE_BPS = Decimal("4.5")            # `userFees` for the trader wallet, read 1 October 2026
MEASURED_SLIPPAGE_BPS = Decimal("1.1")    # a stop filled 1.1 bps worse than its trigger, 29 Sep 2026
SLIPPAGE_BPS = Decimal(2)                 # what the take plans for: the measured 1.1, rounded up
SPARE_BPS = Decimal(5)                    # so the margin is a multiple of the slippage, not equal to it
# How far before the rule line the stop fires, in basis points of the line's price: 6, set for the
# platform on 10 November 2026. The stop is a market trigger, so the account pays the taker fee and
# the fill's slippage AFTER the price reached it. A stop exactly at the line therefore left the
# account below the floor -- by 4.5 + 1.1 = 5.6 bps of the notional at the measured numbers -- and the
# contract counts that as a breach (`RuledAccount.violation`: equity < floor). Fired 6 bps early, the
# close lands 6 - 5.6 = 0.4 bps of the notional INSIDE the line. Against the take's planning figure
# for slippage (2 bps) the same 6 falls 0.5 short: 4.5 + 2 = 6.5. And the two stops the exchange
# itself fired on testnet, on 12 and 14 October 2026, filled 1.6 to 4.5 bps past their triggers in a
# calm market (docs/GATEWAY.md): against those, 6 falls 0.1 to 3.0 short. The test
# `test_the_buffer_covers_the_fee_and_the_measured_slippage` holds the first, and only the first.
STOP_BUFFER_BPS = Decimal(6)
# BOTH sides, not just the close. The take is placed with the order that opens the position, so
# the entry's fee is debited after the line is fixed -- and `reconcile` will not pull a resting
# take nearer, by design, since that is the trader's to move. Measured on the 10 bps this started
# as: the margin over the target came to 1.01 bps of the notional, which one bad fill eats.
CLOSE_COST_BPS = 2 * (TAKER_FEE_BPS + SLIPPAGE_BPS) + SPARE_BPS   # 18
# How far a take the gateway placed may stand short of the line before the sweep carries it out, in
# basis points of the mark. The take goes on the book with the order that opens the position, so the
# entry's own fee and slippage leave it this far short the moment the position exists: that is the
# allowance above being spent as planned, and a modify sent for it would go with every opening. A
# take within this still clears the target by the close and the whole spare.
#
# The same distance BEYOND the line is left alone too. Until 5 October 2026 the mark the line was
# computed from and the mark inside the account's equity were two requests, a moment apart. A take
# pulled in for every tick between them was walked in by modifies until it was short enough to be
# carried out again: the audit counted 39 in an hour of nothing happening (4 Oct 2026). For a
# position the account holds the two are one answer now (`as_the_book_saw`), and the difference
# that outgrew this lag -- 9 bps, measured -- is gone. The far side stays all the same: an order
# resting with no position behind it is still priced at the market's mark, and a tick of rounding
# is still a tick.
TAKE_LAG_BPS = TAKER_FEE_BPS + SLIPPAGE_BPS   # 6.5
USD = Decimal(1_000_000)  # chain units per USDC (perp USD and equity are in 1e-6 USDC)
# The farthest a stop or a take is put from the mark, as a fraction of it. A position too small to
# use up the whole budget has no rule line at all; Hyperliquid still needs a price.
MAX_DISTANCE = Decimal("0.5")
# Hyperliquid fills a triggered market TP/SL within 10% of the trigger (its docs); the order's own
# price is set to that bound, so it never makes the close stricter than the venue's rule.
MARKET_SLIPPAGE = Decimal("0.1")
PERP_DECIMALS = 6
SIG_FIGS = 5
LONG, SHORT = "long", "short"
# How frontendOpenOrders names the two kinds of order the gateway places.
MARKET_TRIGGERS = {"Stop Market": "sl", "Take Profit Market": "tp"}
# How long a day's snapshot read from the chain is trusted before the gateway reads it again, when
# the snapshot is from an earlier UTC day (nobody has taken today's yet).
SNAPSHOT_RECHECK_S = 60.0


# ── numbers ──────────────────────────────────────────────────────────────────────────────

def _decimals_at(x: Decimal, sz_decimals: int) -> int:
    """How many decimals a perp price near x may have: five significant figures, at most
    6 - szDecimals decimals, and a whole number always."""
    return max(0, min(SIG_FIGS - 1 - x.adjusted(), PERP_DECIMALS - sz_decimals))


def valid_px(x: Decimal, sz_decimals: int, rounding: str) -> Decimal:
    px = x.quantize(Decimal(1).scaleb(-_decimals_at(x, sz_decimals)), rounding=rounding) if x > 0 else x
    if px <= 0:
        raise ValueError(f"no valid price near {x}")
    return px


def tick(x: Decimal, sz_decimals: int) -> Decimal:
    return Decimal(1).scaleb(-_decimals_at(x, sz_decimals))


def wire_number(x: Decimal) -> str:
    """The way Hyperliquid normalizes a number before it checks a signature: no exponent, no
    trailing zeros."""
    return format(x.normalize(), "f")


# ── what the chain and Hyperliquid say ─────────────────────────────────────────────────

@dataclass(frozen=True)
class RuleLimits:
    """The account's rules and where they stand, read from its contract."""
    challenge: bool
    daily_loss_bps: int
    max_drawdown_bps: int
    drawdown_base: int  # 1e-6 USDC: the challenge capital, or the pool's equity at the funded start
    day: int  # UTC day of the latest daily snapshot
    day_start_equity: int  # 1e-6 USDC; 0 before any snapshot
    target_bps: int
    # The pool's leverage rule, times 100, as `rules()` gives it. None is a rule nobody read: the
    # gateway then signs nothing that may open or grow a position (`leverage_check`).
    max_leverage_x100: int | None = None
    # A v3 pool's own cap for each asset it trades, times 100, as (order number, cap) pairs, in the
    # order of `rules()`; none for a pool before v3 (`decode_rules` in gateway/chain.py).
    asset_caps_x100: tuple[tuple[int, int], ...] = ()
    # 1e-6 USDC: what was over the target when a v3 account started on its dex (`startSurplus`). The
    # contract holds it out of the account's equity, in every rule and in the leverage rule alike
    # (self-audit v3, round 1, T1), so the gateway does too: 0 for an account before it.
    start_surplus: int = 0

    def equity(self, raw: Decimal) -> Decimal:
        """The account's equity as its contract counts it: Hyperliquid's, less the start's surplus.
        Every comparison of equity with a rule goes through this, and only this."""
        return raw - Decimal(self.start_surplus) / USD

    def floor(self) -> Decimal:
        """Equity at which the nearest rule breaks, in USDC: the same comparison as
        `RuledAccount.violation`."""
        floor = Decimal(self.drawdown_base) * (BPS - self.max_drawdown_bps) / BPS / USD
        if self.day_start_equity > 0:
            floor = max(floor, Decimal(self.day_start_equity) * (BPS - self.daily_loss_bps) / BPS / USD)
        return floor

    def gain_room(self, equity: Decimal, notional: Decimal = Decimal(0)) -> Decimal:
        """How much equity may still gain before the take, in USDC. A challenge has a target
        (`graduate` asks for capital plus targetBps); a funded stage has none, so there it is
        one target's worth of the equity it has now.

        For a challenge the room reaches PAST the target by what closing will cost. The take is a
        market trigger: when it fires the position closes at market and pays a taker fee, so a
        take placed exactly at the target leaves the account flat a fee BELOW it -- and `graduate`
        asks for at least the target with no tolerance (`ChallengeAccount.sol:229-230`). Worked out
        on 1 October 2026 against this function and the demo's pass pool (capital 70, target
        25 bps), with the taker rate read from `userFees`: the take would sit at equity 70.17493
        against a target of 70.175, and after the close the account would hold 70.13. Trying again
        does not help: the next room is exactly that fee, so every attempt lands on `target - fee`
        again. It is a fixed point, not a sequence creeping up.

        No take at the target has fired on this deployment -- nobody has passed since 29 September
        -- so those are figures from the arithmetic and the live fee rate, not from a run.
        """
        if self.challenge:
            target = Decimal(self.drawdown_base) * (BPS + self.target_bps) / BPS / USD
            return target - equity + notional * CLOSE_COST_BPS / BPS
        return equity * self.target_bps / BPS


@dataclass(frozen=True)
class Market:
    coin: str
    mark: Decimal
    sz_decimals: int
    # From the dex's `meta` (HIP-3): a delisted perp, and Hyperliquid's `marginMode` -- no field
    # (None) when cross margin is allowed, "noCross" or "strictIsolated" when it is not.
    delisted: bool = False
    margin_mode: str | None = None
    max_leverage: int = 0


@dataclass(frozen=True)
class Protective:
    """A reduce-only trigger order on the book: the gateway's, since a trader can't place one."""
    oid: int
    asset: int
    closes: str  # LONG: it sells a long; SHORT: it buys back a short
    tpsl: str  # "sl" or "tp"
    trigger: Decimal


@dataclass
class Book:
    """What Hyperliquid says about one account."""
    equity: Decimal
    positions: dict[int, Decimal] = field(default_factory=dict)  # asset -> signed size
    opening: dict[int, dict[str, Decimal]] = field(default_factory=dict)  # asset -> side -> resting size
    protective: list[Protective] = field(default_factory=list)
    # asset -> the mark this answer valued the position at: its value over its size. The equity
    # above was counted at exactly these marks, in the same answer.
    marks: dict[int, Decimal] = field(default_factory=dict)
    # The account's own resting orders that may open or grow a position, (asset, oid): what the
    # Friday hour takes off on a pool that is flat over the weekend.
    resting: list[tuple[int, int]] = field(default_factory=list)
    # The notional of every position, in USDC, as the same answer counts it (`totalNtlPos`): the
    # `ntlPos` of the margin summary the contract's leverage rule reads (`RuledAccount.violation`).
    # None when the answer did not give it.
    notional: Decimal | None = None


class Venue(Protocol):
    def book(self, account: str, markets: dict[int, Market]) -> Book: ...

    def markets(self) -> dict[int, Market]: ...


def parse_markets(meta_and_ctxs: Any, dex_index: int = 0) -> dict[int, Market]:
    """`metaAndAssetCtxs` of one dex: its perp universe in index order and each asset's mark,
    keyed by the number an order uses for it (gateway/assets.py)."""
    meta, ctxs = meta_and_ctxs
    out = {}
    for i, (asset, ctx) in enumerate(zip(meta["universe"], ctxs)):
        mark = ctx.get("markPx")
        if mark is not None and Decimal(mark) > 0:
            # `onlyIsolated` is the older field for the same thing as "noCross".
            mode = asset.get("marginMode") or ("noCross" if asset.get("onlyIsolated") else None)
            out[order_asset(dex_index, i)] = Market(
                asset["name"], Decimal(mark), int(asset["szDecimals"]), delisted=bool(asset.get("isDelisted", False)),
                margin_mode=mode, max_leverage=int(asset.get("maxLeverage") or 0))
    return out


def parse_book(state: Any, orders: Any, markets: dict[int, Market]) -> Book:
    """`clearinghouseState` and `frontendOpenOrders` for one account."""
    index = {m.coin: i for i, m in markets.items()}
    summary = state["marginSummary"]
    notional = summary.get("totalNtlPos")
    book = Book(equity=Decimal(summary["accountValue"]), notional=None if notional is None else Decimal(notional))
    for entry in state.get("assetPositions", []):
        p = entry["position"]
        size = Decimal(p["szi"])
        if size != 0:
            if p["coin"] not in index:
                raise GatewayError(502, "no_market_price", f"no mark for {p['coin']}")
            book.positions[index[p["coin"]]] = size
            value = Decimal(p.get("positionValue") or 0)
            if value > 0:
                book.marks[index[p["coin"]]] = value / abs(size)
    for o in orders:
        asset = index.get(o["coin"])
        if asset is None:
            continue
        # "B" buys: it closes a short or grows a long; "A" sells.
        buys = o["side"] == "B"
        if o.get("isTrigger") and o.get("reduceOnly"):
            # Only a market trigger protects: a stop limit may rest unfilled past its trigger.
            kind = MARKET_TRIGGERS.get(o.get("orderType"))
            if kind is not None:
                book.protective.append(Protective(int(o["oid"]), asset, SHORT if buys else LONG, kind,
                                                  Decimal(o["triggerPx"])))
        elif not o.get("reduceOnly") and not o.get("isTrigger"):
            sides = book.opening.setdefault(asset, {LONG: Decimal(0), SHORT: Decimal(0)})
            sides[LONG if buys else SHORT] += Decimal(o["sz"])
            book.resting.append((asset, int(o["oid"])))
    return book


def merge_books(books: list[Book]) -> Book:
    """One account's books on several perp dexes as one: equity summed, positions and orders side by
    side. An account keeps separate balances (CoreWriter action 16), so each dex's clearinghouse
    holds its own part of the equity; asset numbers never repeat across dexes. No books at all is a
    venue told to read no dex, not an account with no equity (CodeRabbit, cp#161)."""
    if not books:
        raise ValueError("no book to merge: the venue reads no dex")
    out = Book(equity=sum((b.equity for b in books), Decimal(0)))
    # The notional the leverage check counts from, summed: unknown if any dex's answer left it out, so
    # nothing that may open is signed on a part of it (CodeRabbit, cp#164: dropped here, every order
    # that may open would have been refused as `no_notional` on the host).
    out.notional = None if any(b.notional is None for b in books) else sum((b.notional for b in books), Decimal(0))
    for b in books:
        out.positions.update(b.positions)
        out.opening.update(b.opening)
        out.protective.extend(b.protective)
        out.resting.extend(b.resting)
        out.marks.update(b.marks)
    return out


# ── the lines ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Extra:
    """The trader's order about to be submitted, counted as if it filled."""
    asset: int
    buys: bool
    size: Decimal


def exposure(book: Book, extra: Extra | None = None) -> dict[int, dict[str, Decimal]]:
    """How long and how short each asset could get: the position, plus every resting order that
    could open or grow one, plus the order on its way."""
    out: dict[int, dict[str, Decimal]] = {}
    for asset in set(book.positions) | set(book.opening) | ({extra.asset} if extra else set()):
        q = book.positions.get(asset, Decimal(0))
        resting = book.opening.get(asset, {})
        sides = {LONG: max(q, Decimal(0)) + resting.get(LONG, Decimal(0)),
                 SHORT: max(-q, Decimal(0)) + resting.get(SHORT, Decimal(0))}
        if extra is not None and extra.asset == asset:
            sides[LONG if extra.buys else SHORT] += extra.size
        if sides[LONG] > 0 or sides[SHORT] > 0:
            out[asset] = sides
    return out


@dataclass(frozen=True)
class Line:
    stop: Decimal
    take: Decimal


def worst_size(book: Book, asset: int, extra: Extra | None = None) -> Decimal:
    """The largest the position on `asset` could get, either way, if every resting order that may open
    or grow one filled, and `extra` with them: all the buys, or all the sells, on top of what it holds,
    max(|position + buys|, |position - sells|). A sell that closes a long brings it toward nothing
    before it can make a short, so it is not counted as one (AUDIT V7-3, 22 Oct 2026); `exposure`
    counts it as one for the stop and the take, which only has to be safe."""
    q = book.positions.get(asset, Decimal(0))
    resting = book.opening.get(asset, {})
    buys, sells = resting.get(LONG, Decimal(0)), resting.get(SHORT, Decimal(0))
    if extra is not None and extra.asset == asset:
        if extra.buys:
            buys += extra.size
        else:
            sells += extra.size
    return max(abs(q + buys), abs(q - sells))


def worst_notional(book: Book, markets: dict[int, Market], extra: Extra | None = None) -> Decimal:
    """The most the account's positions could come to, in USDC, if every resting order that may open
    or grow one filled, and `extra` with them: the notional the answer gives, with each asset that
    could change counted at the largest it could get either way (`worst_size`), at the answer's mark."""
    total = book.notional
    for asset in exposure(book, extra):
        if asset not in markets:
            raise GatewayError(502, "no_market_price", f"no mark for asset {asset}")
        mark = markets[asset].mark
        total += worst_size(book, asset, extra) * mark - abs(book.positions.get(asset, Decimal(0))) * mark
    return total


def leverage_caps(text: str) -> dict[int, Decimal]:
    """GATEWAY_LEVERAGE_CAPS: each perp's own leverage cap by order number, JSON
    {"<order number>": "<leverage>"}, in the form GATEWAY_SIZE_CAPS gives each perp's size cap (CTO,
    21 Oct 2026). Empty means none is set; the gateway then refuses to start (`server.main`)."""
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"GATEWAY_LEVERAGE_CAPS is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise SystemExit("GATEWAY_LEVERAGE_CAPS must be a JSON object of order number -> leverage")
    caps = {}
    for asset, leverage in raw.items():
        try:
            cap = Decimal(leverage) if isinstance(leverage, str) else None
        except InvalidOperation:
            cap = None
        if not (isinstance(asset, str) and asset.isdigit()) or cap is None or not cap.is_finite() or cap < 1:
            raise SystemExit(f"GATEWAY_LEVERAGE_CAPS: {asset!r}: {leverage!r} is not an order number and a leverage of at least 1")
        caps[int(asset)] = cap
    return caps


def asset_leverage_check(book: Book, markets: dict[int, Market], extra: Extra, caps: dict[int, Decimal],
                         pool_caps: dict[int, int] | None = None, equity: Decimal | None = None) -> None:
    """Refuses, before anything is signed, an order after which the position on its own asset could
    pass that asset's leverage cap: the platform's from GATEWAY_LEVERAGE_CAPS, or a v3 pool's own for
    the asset (`pool_caps`, times 100) where it is lower. The comparison is the v3 contracts'
    (`AccountOps.overAssetCap`): the position's notional times 100 against equity times the cap. As in
    `leverage_check`, the position is counted at the most it could come to with the account's resting
    orders, and an order that does not raise that is never refused here, so a trader can always come
    back under the cap. A cap over the pool's own rule is the rule: `leverage_check`, run first,
    holds the whole account to it, and one asset's position is never more than the whole."""
    if extra.asset not in caps:
        raise GatewayError(403, "no_leverage_cap",
                           f"the platform sets no leverage cap for asset {extra.asset}, so nothing that may open on it is signed")
    cap_x100 = int(caps[extra.asset] * 100)
    own = (pool_caps or {}).get(extra.asset)
    if own is not None:
        cap_x100 = min(cap_x100, own)
    mark = markets[extra.asset].mark

    def held(e: Extra | None) -> Decimal:
        return worst_size(book, extra.asset, e) * mark
    after = held(extra)
    if after <= held(None):
        return
    # The equity the contract counts (`RuleLimits.equity`), when the caller has the account's rules.
    eq = book.equity if equity is None else equity
    if eq <= 0 or after * 100 > eq * cap_x100:
        cap = Decimal(cap_x100) / 100
        share = "no equity" if eq <= 0 else f"{after / eq:.2f}x"
        raise GatewayError(403, "over_asset_leverage",
                           f"after this order the position on asset {extra.asset} could come to {after:.2f} USDC against "
                           f"{eq:.2f} of equity: {share}, over its {cap.normalize():f}x")


def leverage_check(limits: RuleLimits, book: Book, markets: dict[int, Market], extra: Extra) -> None:
    """Refuses, before anything is signed, an order after which the account's positions could break
    the pool's leverage rule. The comparison is the contract's (`RuledAccount.violation`): notional
    times 100 against equity times the rule, on the notional and the equity of Hyperliquid's own
    answer. An order that does not raise what the account could hold is never refused here, so a
    trader can always come back under the rule.

    The rule had one layer until this: the contract after the fact, and the keeper that stops the
    account. This is a second layer before the signature; the contract and the keeper stay."""
    if limits.max_leverage_x100 is None:
        raise GatewayError(503, "leverage_unknown", "the pool's leverage rule could not be read; nothing that may open is signed")
    if book.notional is None:
        raise GatewayError(502, "no_notional", "Hyperliquid's answer gave no notional for the account")
    before = worst_notional(book, markets)
    after = worst_notional(book, markets, extra)
    if after <= before:
        return
    eq = limits.equity(book.equity)
    if eq <= 0 or after * 100 > eq * limits.max_leverage_x100:
        cap = Decimal(limits.max_leverage_x100) / 100
        held = "no equity" if eq <= 0 else f"{after / eq:.2f}x"
        raise GatewayError(403, "over_leverage",
                           f"after this order the account could hold {after:.2f} USDC of positions against {eq:.2f} "
                           f"of equity: {held}, over the pool's {cap.normalize():f}x")


def as_the_book_saw(markets: dict[int, Market], book: Book) -> dict[int, Market]:
    """The markets, with the mark of every asset the account holds taken from the account's own
    answer instead of from the market's.

    A line is a distance from the mark, and the distance is worked out from the equity. The equity
    already holds the position's gain at one mark; the market's answer is another request and
    carries another. Whatever the price did between the two requests moved the stop and the take by
    exactly that much, with nothing paid and nothing changed. Measured on the live challenge on
    4 and 5 October 2026, from Hyperliquid's own order history: a take pulled 2.5 USD in on ETH
    and put back seventeen seconds later, and a stop that came 1.1 nearer and stayed, since a stop
    never moves away. The two reads were 9 bps apart; the take's lag is 6.5.

    Taken from one answer the two are of one instant, and that difference is gone. An asset with
    no position has nothing in the equity that depends on its mark, so the market's is used there.
    """
    if not book.marks:
        return markets
    return {asset: replace(m, mark=book.marks[asset]) if asset in book.marks else m for asset, m in markets.items()}


def unvalued(book: Book) -> frozenset[int]:
    """The assets the account holds a position in and its answer gave no value for.

    Hyperliquid's answer gives a value for every position. Were it to stop, the mark of such a
    position could only be the market's, another request, and its lines would be worked out from
    two requests again with nothing to say so."""
    return frozenset(a for a in book.positions if a not in book.marks)


def _moved(mark: Decimal, notional: Decimal, amount: Decimal) -> Decimal:
    """Where the mark is when a position of this notional has gained `amount` (lost, if negative)."""
    return mark * (notional + amount) / notional


def before_the_line(line: Decimal, side: str) -> Decimal:
    """Where the stop of a `side` position fires: STOP_BUFFER_BPS of the line's price nearer the mark
    than the rule line itself -- above it for a long, below it for a short.

    In equity that is STOP_BUFFER_BPS of the notional at the line, for every position alike: the
    budget is shared in proportion to notional, so if every stop fires together the account keeps
    STOP_BUFFER_BPS of its whole notional over the floor, and the close's fee and slippage come out of
    that instead of out of the floor."""
    return line * (BPS + STOP_BUFFER_BPS) / BPS if side == LONG else line * (BPS - STOP_BUFFER_BPS) / BPS


def lines(limits: RuleLimits, equity: Decimal, exp: dict[int, dict[str, Decimal]],
          markets: dict[int, Market]) -> dict[tuple[int, str], Line]:
    """The stop and the take for every asset and direction that has exposure. `equity` is
    Hyperliquid's: the start's surplus comes off it here (`RuleLimits.equity`)."""
    equity = limits.equity(equity)
    for asset in exp:
        if asset not in markets:
            raise GatewayError(502, "no_market_price", f"no mark for asset {asset}")
    with localcontext() as ctx:
        # Multiplied before divided and carried far past any price's digits, so a line that
        # falls exactly on a price stays exactly on it.
        ctx.prec = 60
        notional = sum((max(sides.values()) * markets[a].mark for a, sides in exp.items()), Decimal(0))
        if notional <= 0:
            return {}
        cap = MAX_DISTANCE * notional
        budget = min(max(equity - limits.floor(), Decimal(0)), cap)
        # The notional goes in because a challenge's room has to clear what closing costs,
        # and that is a share of what is being closed.
        room = min(max(limits.gain_room(equity, notional), Decimal(0)), cap)
        out = {}
        for asset, sides in exp.items():
            m = markets[asset]
            step = tick(m.mark, m.sz_decimals)
            for side, size in sides.items():
                if size <= 0:
                    continue
                if side == LONG:
                    # The stop fires STOP_BUFFER_BPS before the line and rounds TOWARDS the mark,
                    # so it is never looser than that. The take rounds AWAY from it: the room
                    # already carries what closing will cost, and rounding back towards the mark
                    # would eat a tick of exactly that. Both stay at least one tick from the mark.
                    line = _moved(m.mark, notional, -budget)
                    stop = min(valid_px(before_the_line(line, LONG), m.sz_decimals, ROUND_CEILING),
                               valid_px(m.mark - step, m.sz_decimals, ROUND_FLOOR))
                    take = max(valid_px(_moved(m.mark, notional, room), m.sz_decimals, ROUND_CEILING),
                               valid_px(m.mark + step, m.sz_decimals, ROUND_CEILING))
                else:
                    line = _moved(m.mark, notional, budget)
                    stop = max(valid_px(before_the_line(line, SHORT), m.sz_decimals, ROUND_FLOOR),
                               valid_px(m.mark + step, m.sz_decimals, ROUND_CEILING))
                    take = min(valid_px(_moved(m.mark, notional, -room), m.sz_decimals, ROUND_FLOOR),
                               valid_px(m.mark - step, m.sz_decimals, ROUND_FLOOR))
                out[(asset, side)] = Line(stop, take)
        return out


def tighter(a: Decimal, b: Decimal, side: str) -> bool:
    """Whether stop a is at least as close to the market as stop b: higher for a long's stop,
    lower for a short's."""
    return a >= b if side == LONG else a <= b


def within(take: Decimal, bound: Decimal, side: str) -> bool:
    """Whether a take is no further from the market than the bound."""
    return take <= bound if side == LONG else take >= bound


def order_wire(asset: int, side: str, tpsl: str, trigger: Decimal, sz_decimals: int) -> dict:
    """One position TP/SL order, in the field order Hyperliquid hashes. It closes `side`: a sell
    for a long, a buy for a short."""
    buys = side == SHORT
    worst = trigger * (1 + MARKET_SLIPPAGE) if buys else trigger * (1 - MARKET_SLIPPAGE)
    px = valid_px(worst, sz_decimals, ROUND_CEILING if buys else ROUND_FLOOR)
    return {"a": asset, "b": buys, "p": wire_number(px), "s": "0", "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": wire_number(trigger), "tpsl": tpsl}}}


def close_action(asset: int, size: Decimal, side: str, mark: Decimal, sz_decimals: int) -> dict:
    """A reduce-only IOC order that closes `size` of the `side` position at the market: a sell for a
    long, a buy for a short, priced MARKET_SLIPPAGE through the mark, as Hyperliquid prices its own
    market stop. Reduce-only, it can only close what is there."""
    buys = side == SHORT
    worst = mark * (1 + MARKET_SLIPPAGE) if buys else mark * (1 - MARKET_SLIPPAGE)
    px = valid_px(worst, sz_decimals, ROUND_CEILING if buys else ROUND_FLOOR)
    return {"type": "order", "orders": [{"a": asset, "b": buys, "p": wire_number(px), "s": wire_number(size),
                                         "r": True, "t": {"limit": {"tif": "Ioc"}}}], "grouping": "na"}


def unprotected(book: Book) -> list[tuple[int, Decimal]]:
    """The positions with no stop standing for them on the book, (asset, signed size): a stop that was
    never placed, or one taken off -- a market halted by its deployer takes every order off (ALGO,
    21 Oct 2026)."""
    stops = {(p.asset, p.closes) for p in book.protective if p.tpsl == "sl"}
    return [(a, q) for a, q in sorted(book.positions.items()) if q != 0 and (a, LONG if q > 0 else SHORT) not in stops]


def cancel_action(asset: int, oid: int) -> dict:
    """One cancel by oid, in the shape a trader's cancel takes (`gateway/checks.py`)."""
    return {"type": "cancel", "cancels": [{"a": asset, "o": oid}]}


def place_action(wires: list[dict]) -> dict:
    return {"type": "order", "orders": wires, "grouping": "positionTpsl"}


def modify_action(oid: int, wire: dict) -> dict:
    return {"type": "batchModify", "modifies": [{"oid": oid, "order": wire}]}


@dataclass
class Plan:
    actions: list[dict]
    report: list[dict]


def reconcile(book: Book, want: dict[tuple[int, str], Line], markets: dict[int, Market],
              follow_takes: bool = False,
              trader_takes: dict[tuple[int, str], frozenset[Decimal]] | None = None,
              hold_takes: bool = False, unvalued: frozenset[int] = frozenset()) -> Plan:
    """What to send so every (asset, direction) with exposure has its stop and its take. A stop
    that guards a position is only ever moved nearer the market; one that guards nothing yet
    (the position isn't there) simply follows the line. A take is left where it is while it is
    within the target.

    With `follow_takes` -- a challenge, whose target is an amount and not a distance -- one take is
    held to the line instead: the gateway's own. It is moved to the line when it stands more than
    TAKE_LAG_BPS of the mark away from it, short or beyond, and left alone inside that. Funding
    lowers the equity, which moves the stop's line TOWARDS the market and the take's line AWAY from
    it; "only ever nearer" follows the first and ignores the second, and the take then closes the
    position below the target. `trader_takes` holds the triggers the trader asked for, by asset and
    direction: a take standing exactly on one of them is theirs and is treated as before.

    A funded stage has no target of its own -- its line is one target's worth of the equity it has
    NOW, so it recedes as the position gains -- and a take that followed it would never fire.

    With `hold_takes` -- a funded stage -- a take that stands with its position is not moved at all.
    Its line is worked out from the mark and the equity of the moment, so when the position LOSES
    the line comes after the price, and "a take beyond the line comes in" pulled the take along with
    it, onto the losing side of the entry. Measured on 4 October 2026: a short entered at 84955 with
    its take at 84715, the price went to 85206, and the take stood at 84967. A take is placed one
    target from where the position is opened, and stays there.

    `unvalued` names the assets whose position the account's answer gave no value for. Their lines
    are worked out from two requests, so a take that stands with such a position is not moved
    either: pulled in by what the price did between the requests, it would close the position short
    of its target. The stop is still brought nearer when the line says so. The stop is what holds
    the pool's rule, and a new day's floor or a payment out of the account has to reach it. Over
    time the difference between two requests costs the trader room; a single placement can stand
    that difference too far, until a later sweep.
    """
    actions, report = [], []
    for (asset, side), line in sorted(want.items()):
        moves: list[dict] = []
        m = markets[asset]
        q = book.positions.get(asset, Decimal(0))
        held = q > 0 if side == LONG else q < 0
        mine = [p for p in book.protective if p.asset == asset and p.closes == side]
        stops = [p for p in mine if p.tpsl == "sl"]
        takes = [p for p in mine if p.tpsl == "tp"]
        new, row = [], {"asset": asset, "coin": m.coin, "side": side}

        if not stops:
            new.append(order_wire(asset, side, "sl", line.stop, m.sz_decimals))
            row.update(stop=wire_number(line.stop), stopWas="placed")
        else:
            best = max(stops, key=lambda p: p.trigger) if side == LONG else min(stops, key=lambda p: p.trigger)
            keep = tighter(best.trigger, line.stop, side) if held else best.trigger == line.stop
            if keep:
                row.update(stop=wire_number(best.trigger), stopWas="kept")
            else:
                moves.append(modify_action(best.oid, order_wire(asset, side, "sl", line.stop, m.sz_decimals)))
                row.update(stop=wire_number(line.stop), stopWas="moved")

        if not takes:
            new.append(order_wire(asset, side, "tp", line.take, m.sz_decimals))
            row.update(take=wire_number(line.take), takeWas="placed")
        else:
            near = min(takes, key=lambda p: p.trigger) if side == LONG else max(takes, key=lambda p: p.trigger)
            # How far the standing take is short of the line: above zero it is nearer the market than
            # the line, below zero it is beyond it.
            short_by = line.take - near.trigger if side == LONG else near.trigger - line.take
            if not held:
                keep = near.trigger == line.take
            elif hold_takes or asset in unvalued:
                keep = True
            elif follow_takes and near.trigger not in (trader_takes or {}).get((asset, side), ()):
                lag = m.mark * TAKE_LAG_BPS / BPS
                keep = -lag <= short_by <= lag
            else:
                keep = short_by >= 0
            if keep:
                row.update(take=wire_number(near.trigger), takeWas="kept")
            else:
                moves.append(modify_action(near.oid, order_wire(asset, side, "tp", line.take, m.sz_decimals)))
                row.update(take=wire_number(line.take), takeWas="moved")

        actions += ([place_action(new)] if new else []) + moves
        report.append(row)
    return Plan(actions, report)


# ── the protector ────────────────────────────────────────────────────────────────────────

class LimitsReader(Protocol):
    def rule_limits(self, account: str) -> RuleLimits: ...

    def day_snapshot(self, account: str) -> tuple[int, int]: ...

    def trading_key(self, account: str) -> str | None: ...

    def account_of(self, key: str) -> str | None: ...

    def perp_name(self, asset: int) -> str | None: ...

    def pool_of(self, account: str) -> str: ...


# Signs one of the gateway's own actions with the account's key and submits it, under a nonce other
# than the one given: (why it failed, whether Hyperliquid confirmed it, Hyperliquid's answer).
Act = Callable[[str, dict, "int | None"], tuple["str | None", bool, Any]]


def log_line(**fields) -> None:
    print(json.dumps({"t": int(time.time()), **fields}, default=str), flush=True)


# How long a key Hyperliquid answered "User or API Wallet ... does not exist" for gets no action from
# the gateway, the sweep's or the trader's. 18 Oct 2026: the live challenge's agent was gone from
# Hyperliquid, and the sweep sent a refused modify every ~16 seconds from 10:00 to 13:30 UTC, each one
# an action the venue counts. One attempt every this many seconds is enough to notice the key back.
AGENT_RETRY_S = 900


_AGENT_REFUSAL = re.compile(r"API Wallet (0x[0-9a-fA-F]{40}) does not exist")


def agent_unknown(refusal: Any, key: str) -> bool:
    """Whether Hyperliquid refused because it does not know THIS key as an agent of the account.

    The same words come back for a signature that recovers to some other address (Hyperliquid's
    signing docs: an incorrect signature reads "User or API Wallet 0x... does not exist"). That is a
    signing fault, not a missing agent, and must not silence a good key for AGENT_RETRY_S: only a
    refusal that names this key counts; any other is an ordinary failure."""
    found = _AGENT_REFUSAL.search(str(refusal or ""))
    return found is not None and found.group(1).lower() == key.lower()


# A stop the sweep could not put back, refused at every sweep pass for this long (CTO, 21 and 22 Oct
# 2026: "stop in any case", AUDIT V8-1): the position goes, closed at the market with the account's
# key. With a pass every 15 seconds that is three refusals in a row; one or two are a second chance.
CLOSE_AFTER_S = 30.0
# After the Friday hour, how often the sweep tries again to close a HIP-3 position of a pool that is flat over
# the weekend (one a late fill opened, or one the hour's closes did not fill), per position, until Sunday
# 18:05. Not at every pass: on testnet the close is an IOC that finds no level (6 November 2026: all 202 of
# the Friday hour came back `iocCancelRejected`, docs/GATEWAY.md), and every 15 seconds for 49 hours is
# about 11,800 refused IOCs per position. Every 5 minutes is about 590, one log line each, while on mainnet
# the book trades on through the weekend (xyz:GOLD on Saturday 7 November 2026: some 300 trades an hour),
# so each try meets a book that has moved. The stop and the take stand in between.
WEEKEND_CLOSE_EVERY_S = 300.0
# The words of the gateway's own failures that are not a "no" (gateway/server.py, `Gateway._act`):
# the call never reached Hyperliquid, or what came back was not the account's key's signature. What
# happened is not known, so it is not counted as a refusal.
NOT_KNOWN = ("Hyperliquid was unreachable", "it was signed by another key")


class Protector:
    def __init__(self, reader: LimitsReader, venue: Venue, act: Act, clock: Callable[[], float] = time.time,
                 asset_caps: dict[int, Decimal] | None = None, hold_over_weekend: frozenset[str] = frozenset()):
        self.reader = reader
        self.venue = venue
        self.act = act
        self.clock = clock
        # The pools whose accounts keep HIP-3 positions over the weekend (GATEWAY_HOLD_OVER_WEEKEND);
        # every other pool is flat over it, the default (CTO, 21 Oct 2026).
        self.hold_over_weekend = frozenset(p.lower() for p in hold_over_weekend)
        # account -> (when its run of refused stops began, the sweep pass of the latest refusal).
        self._refused: dict[str, tuple[float, int]] = {}
        # account -> how many sweep passes have read it: refusals count as "in a row" by these numbers.
        self._passes: dict[str, int] = {}
        # account -> its pool, for the weekend setting: a pool is its own, a challenge is its pool's.
        self._pool_of: dict[str, str] = {}
        # (account, asset) -> when the sweep last sent a weekend close for that position
        # (WEEKEND_CLOSE_EVERY_S). Kept only through the weekend.
        self._weekend_closes: dict[tuple[str, int], float] = {}
        # Each perp's own leverage cap, by order number (GATEWAY_LEVERAGE_CAPS). None is a gateway
        # built without the setting, as the tests build it; the gateway itself won't start without it.
        self.asset_caps = asset_caps
        self._limits: dict[tuple[str, str], tuple[RuleLimits, float]] = {}
        self._watch: dict[str, str] = {}  # account -> key, for the sweep
        # Order assets whose name HyperEVM's perpAssetInfo agrees with Hyperliquid's on. A name never
        # changes, so one agreement is enough; a disagreement is asked again next time.
        self._named: set[int] = set()
        # Accounts whose last answer showed nothing open. One such answer is not believed: the sweep
        # says why.
        self._flat: set[str] = set()
        # account -> the assets its last answer gave no value for, so that it is said once.
        self._unvalued_seen: dict[str, frozenset[int]] = {}
        # Protective orders seen standing for a direction with nothing behind them, by account:
        # the answer before this one has to have shown the same before they are taken off (A-22).
        self._stray_seen: dict[str, frozenset[tuple[int, str]]] = {}
        # (account, asset, direction) -> the take triggers the trader asked for and Hyperliquid did
        # not refuse. By direction, because an order the other way gets a take of its own, and placing
        # that one says nothing about this one. In memory only: after a restart every take is the
        # gateway's until its trader moves it again.
        self._trader_takes: dict[tuple[str, int, str], set[Decimal]] = {}
        # Keys whose account could not be read when the gateway started; each sweep tries them again.
        self._pending: list[str] = []
        # key -> when Hyperliquid last said it does not know it as an agent (`agent_unknown`).
        self._agent_gone: dict[str, float] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        # Accounts the sweep comes back to at the shorter period (`run`, `hot_every`): one with an order
        # resting that may open, a position with no stop, positions that moved since the last pass, or an
        # action just sent for it. Those are the moments a position can stand without a stop until the
        # sweep comes (risk desk, 5 Nov 2026); every other account waits for the longer period. A stop the
        # exchange took off is one of them already: an account holding a position has its orders read at
        # every pass (`hl.Venue.book`), so with the position still there it has no stop, and taken off
        # with its position (it fired) the positions moved.
        self._hot: set[str] = set()
        # account -> its open positions at the last pass.
        self._seen: dict[str, frozenset] = {}

    def _lock(self, account: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(account.lower(), threading.RLock())

    def holding(self, account: str) -> threading.RLock:
        """The account's lock, for a caller that must keep the sweep and the account's other
        requests out from the stop and take it placed until its own order has reached Hyperliquid:
        until then the book doesn't show the order the stop was placed for."""
        return self._lock(account)

    def limits(self, account: str, key: str) -> RuleLimits:
        """Rules, terms and the drawdown base don't change while a key trades an account, so
        they are read once per key. The day's snapshot is read again while it is older than
        today, at most once a minute."""
        now = self.clock()
        k = (account.lower(), key.lower())
        cached = self._limits.get(k)
        if cached is None:
            limits = self.reader.rule_limits(account)
        else:
            limits, read_at = cached
            if limits.day >= int(now // 86400) or now - read_at < SNAPSHOT_RECHECK_S:
                return limits
            day, start = self.reader.day_snapshot(account)
            limits = replace(limits, day=day, day_start_equity=start)
        self._limits[k] = (limits, now)
        return limits

    def _book(self, account: str, markets: dict[int, Market], reuse_orders: bool = False) -> Book:
        """The account's book; for the sweep, with its last list of orders when the venue may keep it,
        which it does only for a dex where the account holds no position (`hl.Venue.book`). A venue
        that keeps nothing is read whole every time."""
        if reuse_orders and getattr(self.venue, "reuses_orders", False):
            return self.venue.book(account, markets, reuse_orders=True)
        return self.venue.book(account, markets)

    def forget_orders(self, account: str) -> None:
        """The account's kept list of orders goes, where the venue keeps one: its next read takes them
        afresh. Called before anything is sent for the account, and after anything breaks off between a
        read of it and the use of that read."""
        forget = getattr(self.venue, "forget", None)
        if forget is not None:
            forget(account)

    def note_sent(self, account: str) -> None:
        """Something is sent for the account, by its trader or by the gateway: its next read takes the
        orders afresh, and the sweep comes back to it at the shorter period until it is calm again."""
        self.forget_orders(account)
        with self._guard:
            self._hot.add(account)

    def _send(self, account: str, key: str, action: dict, avoid_nonce: int | None = None):
        """The one door every action of the gateway's own goes out by: the stop and the take, a cancel,
        a close. The account's kept orders list goes before the action does, so whatever comes of it --
        an answer, a refusal, an answer lost after the action landed -- the next read sees the book as
        Hyperliquid has it."""
        self.note_sent(account)
        return self.act(key, action, avoid_nonce)

    def hot(self) -> set[str]:
        with self._guard:
            return set(self._hot)

    def accounts_with_position(self) -> int:
        """Watched accounts whose last pass found a position open: what the host's minute is spent on."""
        with self._guard:
            return sum(1 for a in self._watch if self._seen.get(a))

    def _note_seen(self, account: str, book: Book, naked: list) -> None:
        positions = frozenset((a, q) for a, q in book.positions.items() if q != 0)
        with self._guard:
            last = self._seen.get(account)
            moved = last is not None and last != positions
            self._seen[account] = positions
            if book.resting or naked or moved:
                self._hot.add(account)
            else:
                self._hot.discard(account)

    def read(self, account: str) -> tuple[dict[int, Market], Book]:
        """One read of the account: the markets, and the account's answer, with the marks of what it
        holds taken from that answer (`as_the_book_saw`)."""
        markets = self.venue.markets()
        book = self.venue.book(account, markets)
        return as_the_book_saw(markets, book), book

    def before_signing(self, account: str, key: str, asset: int, buys: bool, size: Decimal) -> tuple[dict[int, Market], Book]:
        """For an order that may open or grow a position, before it is signed: refuses it if the
        account could then break the pool's leverage rule (`leverage_check`), or its position on the
        order's asset could pass that asset's own cap (`asset_leverage_check`). Returns the read, for
        the stop and the take to be placed from the same answer."""
        with self._lock(account):
            read = self.read(account)
            limits = self.limits(account, key)
            leverage_check(limits, read[1], read[0], Extra(asset, buys, size))
            if self.asset_caps is not None:
                asset_leverage_check(read[1], read[0], Extra(asset, buys, size), self.asset_caps,
                                     dict(limits.asset_caps_x100), equity=limits.equity(read[1].equity))
            return read

    def plan(self, account: str, key: str, extra: Extra | None = None,
             read: tuple[dict[int, Market], Book] | None = None) -> tuple[Plan, dict, RuleLimits, Book]:
        markets, book = read if read is not None else self.read(account)
        limits = self.limits(account, key)
        want = lines(limits, book.equity, exposure(book, extra), markets)
        # Nothing follows here. With an order on its way the line prices more than the position
        # holds, so it moves with the mark; the sweep follows once the order has filled.
        return reconcile(book, want, markets, hold_takes=not limits.challenge), markets, limits, book

    @staticmethod
    def _follows(book: Book, exp: dict) -> bool:
        """Whether a challenge's takes follow the line: exposure on ONE asset, and all of it a
        position already held. (A funded stage holds its takes, and `reconcile` asks that first.)

        Then the line hardly moves with the price -- the position's own gain is in the equity and in
        the room alike -- and only what the account pays moves it. Two things break that, and with
        either the takes are left as they were before, where they can still stand short of the
        target (a limit of this, said in the docs):

        - two assets: each take's line moves with the OTHER position's price, and a take that
          followed it would be modified out and back in with every swing between them;
        - an order resting on the book that would grow the position: the line prices the larger
          size while the equity moves with the smaller, so the line runs ahead of a rising mark
          and a take that followed it would never be reached (review, 4 Oct 2026: 0.001 held,
          0.004 resting, a 60 USD rise moves the line 48 with nothing paid).
        """
        if len(exp) != 1:
            return False
        (asset, sides), = exp.items()
        q = book.positions.get(asset, Decimal(0))
        return sides == {LONG: max(q, Decimal(0)), SHORT: max(-q, Decimal(0))}

    def _takes_of(self, account: str) -> dict[tuple[int, str], frozenset[Decimal]]:
        """The takes the trader asked for on this account, by asset and direction."""
        with self._guard:
            return {(asset, side): frozenset(asked) for (a, asset, side), asked in self._trader_takes.items()
                    if a == account.lower()}

    def trader_asked(self, account: str, asset: int, side: str, trigger: Decimal) -> None:
        """Remembers a take the trader asked for. Called once Hyperliquid has NOT refused it.

        Every trigger asked for on the position is kept, not only the last, and a take counts as the
        trader's while it stands exactly on one of them. An answer that never came, or that confirms
        nothing, leaves the gateway not knowing which take stands: the old one or the new. Either is
        the trader's. A refusal is different -- Hyperliquid said the take did not move -- and a
        request it refused claims nothing, even when it named the very trigger the gateway's own
        take stands on (review, 4 Oct 2026).
        """
        with self._guard:
            self._trader_takes.setdefault((account.lower(), asset, side), set()).add(trigger)

    def _forget_takes(self, account: str, report: list[dict] | None = None) -> None:
        """A take the gateway placed or moved is the gateway's again. With no report, the account
        has nothing open and none of its takes is anybody's."""
        with self._guard:
            for k in [k for k in self._trader_takes if k[0] == account.lower()]:
                if report is None or any(r["asset"] == k[1] and r["side"] == k[2] and r.get("takeWas") != "kept"
                                         for r in report):
                    del self._trader_takes[k]

    def agent_gone(self, key: str) -> bool:
        """Whether Hyperliquid said, less than AGENT_RETRY_S ago, that it does not know this key."""
        with self._guard:
            at = self._agent_gone.get(key.lower())
        return at is not None and self.clock() - at < AGENT_RETRY_S

    def note_agent_gone(self, key: str) -> None:
        with self._guard:
            self._agent_gone[key.lower()] = self.clock()
        log_line(event="agent_missing", key=key, retry_after_s=AGENT_RETRY_S,
                 detail="Hyperliquid does not know this key as the account's agent; nothing is sent with it "
                        "until the next attempt")

    def apply(self, account: str, key: str, plan: Plan, avoid_nonce: int | None = None) -> None:
        for action in plan.actions:
            refusal, confirmed, venue = self._send(account, key, action, avoid_nonce)
            if agent_unknown(refusal, key):
                self.note_agent_gone(key)
                raise GatewayError(502, "agent_missing",
                                   f"Hyperliquid does not know the account's trading key as its agent: {refusal}"[:300])
            if refusal is not None or not confirmed:
                err = GatewayError(502, "protection_failed",
                                   f"Hyperliquid did not confirm the stop and take: {refusal or venue}"[:300])
                # A "no" said in so many words, by Hyperliquid's answer or by the signer. An answer that
                # confirms nothing, one that is not JSON (a 429 or a 502 from its edge), or none at all is
                # not known to be one: the stop may have landed (AUDIT V8-1).
                err.refused = refusal is not None and not str(refusal).startswith(NOT_KNOWN)
                raise err

    def before_opening(self, account: str, key: str, asset: int, buys: bool, size: Decimal,
                       avoid_nonce: int | None = None, read: tuple[dict[int, Market], Book] | None = None) -> list[dict]:
        """Puts the stop and the take in place for an order that may open or grow a position,
        before the order goes. Refuses the order if they can't be placed, or if the account has
        nothing left to lose before its rule line, or a challenge has already met its target.
        `read` is the account as `before_signing` read it, under the same lock: one answer for both."""
        with self._lock(account):
            plan, markets, limits, book = self.plan(account, key, Extra(asset, buys, size), read)
            # The market itself, from the read the plan used: nothing has been placed yet.
            check_market(asset, markets)
            closed = hip3_closed(asset, self.clock())
            if closed is not None:
                raise GatewayError(409, "market_window",
                                   f"{markets[asset].coin} opens nothing during {closed}; closing still goes")
            self.check_name(asset, markets)
            if limits.equity(book.equity) - limits.floor() <= 0:
                raise GatewayError(409, "at_rule_line",
                                   "equity is at the pool's rule line; nothing may open until the account is stopped")
            if limits.challenge and limits.gain_room(limits.equity(book.equity)) <= 0:
                raise GatewayError(409, "target_met",
                                   "the challenge has met its target; close and graduate instead of opening more")
            self.apply(account, key, plan, avoid_nonce)
            self._forget_takes(account, plan.report)
        self.watch(account, key)
        return plan.report

    def check_name(self, asset: int, markets: dict[int, Market]) -> None:
        """Refuses a perp whose name HyperEVM's perpAssetInfo doesn't give as Hyperliquid's does. The
        contracts read a perp through the precompiles, the gateway through the info API; a number
        that means one perp to one and another perp to the other fails no call, it just reads the
        wrong market (testnet, 21 Oct 2026: 750000 is rbbt:RAB to the precompile)."""
        if asset in self._named:
            return
        onchain, venue = self.reader.perp_name(asset), markets[asset].coin
        if onchain != venue:
            raise GatewayError(403, "market_name_mismatch",
                               f"asset {asset}: Hyperliquid names it {venue}, HyperEVM's perpAssetInfo"
                               f"({precompile_index(asset)}) {onchain or 'nothing'}")
        self._named.add(asset)

    def check_names(self, assets: set[int]) -> dict[int, str]:
        """The same check for a set of assets at once, for the gateway's start: what each came to,
        "ok", "no market" or the refusal's words. Refuses nothing itself."""
        markets = self.venue.markets()
        out = {}
        for asset in sorted(assets):
            if asset not in markets:
                out[asset] = "no market"
                continue
            try:
                self.check_name(asset, markets)
                out[asset] = "ok"
            except GatewayError as e:
                out[asset] = e.detail
        return out

    def refuse_protective_cancel(self, account: str, oid: int) -> None:
        markets = self.venue.markets()
        book = self.venue.book(account, markets)
        if any(p.oid == oid for p in book.protective):
            raise GatewayError(403, "protective_order",
                               "the pool's stop and take can't be cancelled; move them with a stop or take request")

    def move(self, account: str, key: str, tpsl: str, asset: int, trigger: Decimal) -> dict:
        """The action that moves an account's stop or take where the trader asked, if the rules
        allow it: a stop only nearer the mark than where it is, a take anywhere between the mark
        and the target."""
        with self._lock(account):
            markets = self.venue.markets()
            book = self.venue.book(account, markets)
            if asset not in markets:
                raise GatewayError(502, "no_market_price", f"no mark for asset {asset}")
            # The market is read first and the account after it, so the account's answer is the
            # later of the two, and the one its equity was counted in. Both questions are asked of
            # it: whether a stop or a take would close at once, and where the line it is held to
            # stands.
            markets = as_the_book_saw(markets, book)
            m = markets[asset]
            q = book.positions.get(asset, Decimal(0))
            if q == 0:
                raise GatewayError(409, "no_position", "there is no position on this asset to move a stop or take for")
            side = LONG if q > 0 else SHORT
            mine = [p for p in book.protective if p.asset == asset and p.closes == side and p.tpsl == tpsl]
            if tpsl == "sl":
                if (side == LONG and trigger >= m.mark) or (side == SHORT and trigger <= m.mark):
                    raise GatewayError(403, "stop_past_mark", "a stop at or past the mark would close at once")
                if mine:
                    best = max(mine, key=lambda p: p.trigger) if side == LONG else min(mine, key=lambda p: p.trigger)
                    floor_px, target = best.trigger, best.oid
                else:
                    want = lines(self.limits(account, key), book.equity, exposure(book), markets)
                    floor_px, target = want[(asset, side)].stop, None
                if not tighter(trigger, floor_px, side):
                    raise GatewayError(403, "stop_looser",
                                       f"a stop may only move nearer the mark than {wire_number(floor_px)}")
            else:
                if (side == LONG and trigger <= m.mark) or (side == SHORT and trigger >= m.mark):
                    raise GatewayError(403, "take_past_mark", "a take at or past the mark would close at once")
                want = lines(self.limits(account, key), book.equity, exposure(book), markets)
                bound = want[(asset, side)].take
                if not within(trigger, bound, side):
                    raise GatewayError(403, "take_beyond_target",
                                       f"a take may not be further from the mark than {wire_number(bound)}")
                target = (min(mine, key=lambda p: p.trigger) if side == LONG
                          else max(mine, key=lambda p: p.trigger)).oid if mine else None
            wire = order_wire(asset, side, tpsl, trigger, m.sz_decimals)
            return modify_action(target, wire) if target is not None else place_action([wire])

    # ── the sweep ───────────────────────────────────────────────────────────────────────

    def watch(self, account: str, key: str) -> None:
        self.note_sent(account)
        with self._guard:
            self._watch[account] = key
            self._flat.discard(account)  # an order is on its way: what was seen before it is old
            # And so is a pair seen standing for nothing: the order may be the one it was placed
            # for, or one that reuses it, and if it fills between the two requests of the next
            # sweep the pair looks like it guards nothing once more. Counted from before the
            # order, that would be the second time, and a fresh position would lose its stop.
            self._stray_seen.pop(account, None)

    def take_on(self, keys: list[str]) -> list[str]:
        """Watches every account one of these keys trades right now, and says which.

        The list of accounts to sweep lives in this process and is filled by orders. A gateway that
        has just started has an empty one, so an account with a position open would not be swept
        again until its trader sent the next order: a day's new snapshot would not tighten its stop,
        and its take would not follow funding. The registry knows which account each key is bound
        to, and the account says whether that key trades it now.

        A key whose read fails is kept and tried again by every sweep until the node answers:
        dropped, its account would stay unwatched until the next order or the next restart, which
        is the gap this closes. An account taken on with nothing open costs two reads: the second
        sweep lets it go.
        """
        taken, failed = [], []
        for key in keys:
            try:
                account = self.reader.account_of(key)
                if account is None or (self.reader.trading_key(account) or "").lower() != key.lower():
                    continue
            except Exception as exc:
                failed.append(key)
                if key not in self._pending:  # said once, not every fifteen seconds
                    log_line(event="protect_take_on_failed", key=key, error=str(exc)[:200])
                continue
            self.watch(account, key)
            taken.append(account)
        with self._guard:
            self._pending = failed
        return taken

    def watching(self) -> dict[str, str]:
        with self._guard:
            return dict(self._watch)

    def unread_keys(self) -> int:
        """How many of the keys `take_on` was given it has not read yet: the node refused them, and every sweep
        tries them again."""
        with self._guard:
            return len(self._pending)

    def _unwatch(self, account: str) -> None:
        with self._guard:
            self._watch.pop(account, None)
            self._refused.pop(account, None)
            self._unvalued_seen.pop(account, None)  # its next position is said anew
            self._stray_seen.pop(account, None)
            self._hot.discard(account)
            self._seen.pop(account, None)

    def _stray_again(self, account: str, stray: list[Protective]) -> list[Protective]:
        """Of the gateway's own stops and takes that stand for a direction with nothing behind
        them -- no position and no order that could open one -- those the answer before this one
        showed standing so as well. One answer is not believed, as with an empty account: the
        positions and the orders are two requests, and an order that fills between them is in
        neither, so its pair looks like it guards nothing for exactly one sweep (A-19)."""
        now = frozenset((p.asset, p.closes) for p in stray)
        with self._guard:
            before = self._stray_seen.get(account, frozenset())
            self._stray_seen[account] = now
        return [p for p in stray if (p.asset, p.closes) in before]

    def _unvalued(self, account: str, book: Book, market: dict[int, Market]) -> frozenset[int]:
        """The assets `unvalued` names for this account, each said in the journal once: again only
        after the answer has given a value for it and stopped, or after the account was let go."""
        blind = unvalued(book)
        with self._guard:
            known, self._unvalued_seen[account] = self._unvalued_seen.get(account, frozenset()), blind
        for asset in sorted(blind - known):
            log_line(event="protect_no_position_value", account=account, coin=market[asset].coin,
                     detail="the account's answer gives no value for this position: its lines are worked "
                            "out from two requests, and its take is not moved")
        return blind

    def _flat_again(self, account: str) -> bool:
        """Whether the answer before this one showed the account with nothing open as well."""
        with self._guard:
            if account in self._flat:
                self._flat.discard(account)
                return True
            self._flat.add(account)
            return False

    def sweep(self, full: bool = True) -> None:
        """One pass over the accounts the gateway has traded: a position that opened from an
        order that rested, a stop that fired while an order still rested, or a new day's
        snapshot that moved the line all get their stop and take here. Not `full`: the hot
        accounts alone (`_note_seen`), and no market read at all when there are none."""
        try:
            self._sweep_watched(full)
        finally:
            if full:
                self._retry_pending()

    def _retry_pending(self) -> None:
        """Keys the node would not read at start, tried again -- AFTER the accounts, never before.

        A read of a throttled node waits out its retries, close to four seconds a call. A pass
        that spent them first would tighten the stops of the accounts it already watches that much
        later, and send the same calls into the same throttled node (the audit, 4 Oct 2026). An
        account taken on here is swept from the next pass.
        """
        with self._guard:
            pending = list(self._pending)
        if pending:
            taken = self.take_on(pending)
            if taken:
                log_line(event="protect_took_on", accounts=taken, after="a read that failed at start")

    def _sweep_watched(self, full: bool = True) -> None:
        watched = self.watching()
        if not full:
            hot = self.hot()
            watched = {a: k for a, k in watched.items() if a in hot}
        if not watched:
            return
        market = self.venue.markets()
        for account, key in watched.items():
            if self.agent_gone(key):
                continue  # said once, at the refusal; asking again sooner only spends the venue's count
            try:
                self._sweep_one(account, key, market)
            except Exception as exc:  # one account must not stop the sweep
                self.forget_orders(account)  # what was read of it is not kept past a pass that broke off
                log_line(event="protect_sweep_failed", account=account, error=str(exc)[:200])

    def after_entry(self, account: str, key: str) -> None:
        """The account read again a moment after an order that may open a position went, rather than at
        the next sweep (ALGO, 21 Oct 2026: a fill in parts may leave a position the stop placed before
        it does not stand for). A stop missing then is placed now, or the position closed, as the sweep
        does; what was found is said either way."""
        try:
            if self.agent_gone(key):
                return
            self._sweep_one(account, key, self.venue.markets(), after_entry=True)
        except Exception as exc:
            self.forget_orders(account)
            log_line(event="after_entry_failed", account=account, error=str(exc)[:200])

    def weekend_flat(self, account: str) -> bool:
        """Whether the account's pool is flat over the weekend: yes unless GATEWAY_HOLD_OVER_WEEKEND
        names it. A pool that could not be read is flat, the default."""
        if not self.hold_over_weekend:
            return True
        with self._guard:
            pool = self._pool_of.get(account.lower())
        if pool is None:
            try:
                pool = self.reader.pool_of(account).lower()
            except Exception as exc:
                log_line(event="pool_unread", account=account, error=str(exc)[:200])
                return True
            with self._guard:
                self._pool_of[account.lower()] = pool
        return pool not in self.hold_over_weekend

    def _weekend_close_now(self, account: str, asset: int, now: float) -> bool:
        """Whether the sweep closes this HIP-3 position now: at every pass in the Friday hour, and after it
        once WEEKEND_CLOSE_EVERY_S has passed since the last close sent for it. Notes the close it allows."""
        held = (account.lower(), asset)
        with self._guard:
            last = self._weekend_closes.get(held)
            if not weekend_close_due(now) and last is not None and now - last < WEEKEND_CLOSE_EVERY_S:
                return False
            self._weekend_closes[held] = now
            return True

    def _forget_weekend_closes(self, account: str, keep: frozenset[int] = frozenset()) -> None:
        """Drops the last weekend close of every position of the account's but those on `keep`."""
        with self._guard:
            for held in [h for h in self._weekend_closes if h[0] == account.lower() and h[1] not in keep]:
                del self._weekend_closes[held]

    @staticmethod
    def _always_flat(asset: int, markets: dict[int, Market]) -> bool:
        """Whether `asset` is a market closed before every weekend on every pool, by its name."""
        return asset in markets and markets[asset].coin in ALWAYS_FLAT_OVER_WEEKEND

    def _close(self, account: str, key: str, asset: int, size: Decimal, markets: dict[int, Market], why: str) -> None:
        """Closes the account's position on `asset` at the market, with its key: reduce-only IOC."""
        market = markets[asset]
        action = close_action(asset, abs(size), LONG if size > 0 else SHORT, market.mark, market.sz_decimals)
        refusal, confirmed, _ = self._send(account, key, action)
        log_line(event="protect_closed", account=account, asset=asset, size=str(size), why=why,
                 refusal=refusal, confirmed=confirmed)

    def _stop_refused(self, account: str, key: str, market: dict[int, Market], naked: list, refused: bool,
                      detail: str) -> None:
        """A sweep pass at which the stop for a position with none could not be placed (AUDIT V8-1, the
        CTO's decisions of 22 Oct 2026). Only a "no" counts, Hyperliquid's or the signer's; an answer
        not known to be one breaks the run. A run is sweep passes in a row, each refused: a pass that
        finds a stop standing, or one not known, starts it again, and the read after an entry is not a
        pass. Once a run has lasted CLOSE_AFTER_S, the account is read again in this pass, and only a
        position that still has no stop is closed: one whose stop landed while its answer was lost
        stays."""
        if not naked:
            return
        if not refused:
            # Not counted, and the run is broken: the next refusal is not at the pass after the last.
            log_line(event="protect_stop_not_known", account=account, detail=detail[:200])
            return
        now = self.clock()
        with self._guard:
            this = self._passes.get(account, 0)
            since, last = self._refused.get(account, (now, this))
            since = since if last == this - 1 else now
            self._refused[account] = (since, this)
        log_line(event="protect_stop_refused", account=account, refused_for_s=round(now - since), detail=detail[:200],
                 unprotected=[{"asset": a, "size": str(q)} for a, q in naked])
        if now - since < CLOSE_AFTER_S:
            return
        book = self.venue.book(account, market)
        still = unprotected(book)
        if not still:
            with self._guard:
                self._refused.pop(account, None)
            log_line(event="protect_stop_found_on_reread", account=account)
            return
        markets = as_the_book_saw(market, book)
        for asset, size in still:
            self._close(account, key, asset, size, markets, why="stop_refused")

    def _sweep_one(self, account: str, key: str, market: dict[int, Market], after_entry: bool = False) -> None:
        with self._lock(account):
            if not after_entry:
                with self._guard:
                    self._passes[account] = self._passes.get(account, 0) + 1
            book = self._book(account, market, reuse_orders=not after_entry)
            markets = as_the_book_saw(market, book)
            # A pass that finds a stop standing for every position records no refusal, so a refusal
            # after it does not continue a run (CodeRabbit, cp#165; AUDIT V8-1): runs go by pass.
            naked = unprotected(book)
            self._note_seen(account, book, naked)
            if naked or after_entry:
                # A position with no stop is never taken as the normal state: it is said, and
                # the plan below puts the stop back.
                log_line(event="after_entry" if after_entry else "protect_stop_missing", account=account,
                         unprotected=[{"asset": a, "size": str(q)} for a, q in naked])
            # The hour before the Friday close: a pool flat over the weekend has its resting HIP-3
            # orders that may open taken off and its HIP-3 positions closed, with the account's key,
            # while this key still trades it. xyz trades through the weekend: an order left resting
            # would fill on Saturday into a position nothing closes until Sunday (AUDIT V8-2, the
            # CTO's decision of 22 Oct 2026). The orders go first, so none fills behind the close.
            hip3 = {a: q for a, q in book.positions.items() if q != 0 and dex_index_of(a) != 0}
            hip3_resting = [(a, oid) for a, oid in book.resting if dex_index_of(a) != 0]
            # From Friday 16:00 to Sunday 18:05 (`weekend_cancel_due`) the orders are taken off while one is
            # still on the book, and the positions are closed: at every pass in the Friday hour
            # (`weekend_close_due`), and after it at most once every WEEKEND_CLOSE_EVERY_S per position.
            now = self.clock()
            if not weekend_cancel_due(now):
                hip3, hip3_resting = {}, []
            # A position that is gone takes its last close with it, so one a later fill opens again is
            # closed at once rather than when the old one's WEEKEND_CLOSE_EVERY_S runs out.
            self._forget_weekend_closes(account, keep=frozenset(hip3))
            if hip3 or hip3_resting:
                if not self.weekend_flat(account):
                    # A pool that keeps its HIP-3 positions over the weekend still has the markets that are
                    # never held over it closed: those alone (gateway/assets.py, ALWAYS_FLAT_OVER_WEEKEND).
                    hip3 = {a: q for a, q in hip3.items() if self._always_flat(a, markets)}
                    hip3_resting = [(a, oid) for a, oid in hip3_resting if self._always_flat(a, markets)]
                if (self.reader.trading_key(account) or "").lower() == key.lower():
                    for asset, oid in hip3_resting:
                        refusal, confirmed, _ = self._send(account, key, cancel_action(asset, oid))
                        log_line(event="weekend_cancelled", account=account, asset=asset, oid=oid,
                                 refusal=refusal, confirmed=confirmed)
                    for asset, size in sorted(hip3.items()):
                        if self._weekend_close_now(account, asset, now):
                            self._close(account, key, asset, size, markets, why="flat_over_weekend")
            exp = exposure(book)
            # A stop or a take standing for a direction with nothing behind it: a pair goes
            # on the book before an order that may open a position, and an order that
            # rested and went without filling leaves its pair standing, which the trader
            # cannot cancel and `reconcile` never sees, since it walks the directions with
            # exposure alone. The team's red team left one such pair on 5 October 2026
            # (A-22). It is taken off at the second answer in a row that shows it standing
            # for nothing, one cancel each, only while this key still trades the account;
            # a cancel Hyperliquid does not confirm raises, the account stays watched, and
            # the next sweep tries again. An account is let go only once none is left.
            backed = {(a, side) for a, sides in exp.items() for side, q in sides.items() if q > 0}
            stray = [p for p in book.protective if (p.asset, p.closes) not in backed]
            gone = self._stray_again(account, stray)
            if gone:
                if key.lower() != (self.reader.trading_key(account) or "").lower():  # the pair is the next key's
                    self._unwatch(account)
                    log_line(event="protect_unwatched", account=account,
                             reason="not trading with this key; its stray pair is the next key's")
                    return
                self.apply(account, key, Plan([cancel_action(p.asset, p.oid) for p in gone], []))
                log_line(event="protect_cleared", account=account,
                         cancelled=[{"asset": p.asset, "tpsl": p.tpsl, "oid": p.oid} for p in gone])
                stray = [p for p in stray if p not in gone]
            if not exp:
                # Nothing open, by this answer. The positions and the orders are two
                # requests, and an order that fills between them is in neither: not yet a
                # position in the first, no longer an order in the second. Let go on that,
                # the account would hold a position that nothing sweeps, and a take its
                # trader moved would stop being theirs. So one such answer is not believed,
                # and the account goes at the second in a row -- with nothing of its own
                # left standing on the book.
                if self._flat_again(account) and not stray:
                    self._unwatch(account)
                    self._forget_takes(account)
                return
            with self._guard:
                self._flat.discard(account)
            limits = self.limits(account, key)
            want = lines(limits, book.equity, exp, markets)
            plan = reconcile(book, want, markets, self._follows(book, exp), self._takes_of(account),
                             hold_takes=not limits.challenge,
                             unvalued=self._unvalued(account, book, market))
            if not plan.actions:
                return
            # Something to send: only while this key still trades this account.
            if (self.reader.trading_key(account) or "").lower() != key.lower():
                self._unwatch(account)
                log_line(event="protect_unwatched", account=account, reason="not trading with this key")
                return
            try:
                self.apply(account, key, plan)
            except GatewayError as exc:
                if exc.code == "protection_failed" and not after_entry:
                    self._stop_refused(account, key, market, naked, getattr(exc, "refused", False), str(exc.detail))
                raise
            self._forget_takes(account, plan.report)
            log_line(event="protect_swept", account=account, protection=plan.report)

    def run(self, every: float, stop: threading.Event | None = None, hot_every: float | None = None) -> None:
        """A sweep of every account each `every` seconds, and, with `hot_every` shorter, a sweep of the
        hot accounts alone at the ticks between (risk desk, 5 Nov 2026: 30 seconds for the rest, 15 where a
        position can stand without a stop). Each wait starts when the sweep before it has finished."""
        # Zero or less would sweep without a pause, as fast as the venue answers, out of the host's minute.
        if not every > 0 or (hot_every is not None and not hot_every > 0):
            raise ValueError(f"the sweep's periods must be more than 0 seconds: {every}, {hot_every}")
        stop = stop or threading.Event()
        tick = hot_every if hot_every and hot_every < every else every
        per_full = max(1, round(every / tick))
        ticks = 0
        while not stop.wait(tick):
            ticks += 1
            try:
                self.sweep(full=ticks % per_full == 0)
            except Exception as exc:
                log_line(event="protect_sweep_failed", error=str(exc)[:200])
