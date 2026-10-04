"""The stop and the take the gateway keeps on Hyperliquid for every position an account may hold.

A pool's rules are enforced by the contracts after the fact: someone sees a broken rule and calls
`breach`, and HyperCore closes the positions a few seconds later. In a fast market that is too
late. So the gateway also leaves the closing to Hyperliquid itself: before it submits an order that
may open or grow a position, it makes sure the account has, for that asset and that direction,

- a STOP at the rule line: the mark price at which the account's equity would reach the nearest
  rule, the static drawdown floor or the day's loss floor, whichever is higher; and
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
import threading
import time
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any, Callable, Protocol

from .checks import GatewayError

BPS = Decimal(10_000)
# What a challenge's take has to clear beyond the target, in basis points of the notional, and
# where each part of it comes from.
TAKER_FEE_BPS = Decimal("4.5")   # `userFees` for the trader wallet, read 1 October 2026
SLIPPAGE_BPS = Decimal(2)        # a stop filled 1.1 bps worse than its trigger, 29 Sep 2026
SPARE_BPS = Decimal(5)           # so the margin is a multiple of the slippage, not equal to it
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
# The same distance BEYOND the line is left alone too. The mark the line is computed from and the
# mark inside the account's equity are two reads, a moment apart, and they differ by a few basis
# points. A take pulled in for every tick of that is walked in by modifies until it is short enough
# to be carried out again: the audit counted 39 in an hour of nothing happening (4 Oct 2026).
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


class Venue(Protocol):
    def book(self, account: str, markets: dict[int, Market]) -> Book: ...

    def markets(self) -> dict[int, Market]: ...


def parse_markets(meta_and_ctxs: Any) -> dict[int, Market]:
    """`metaAndAssetCtxs`: the perp universe in index order, and each asset's mark."""
    meta, ctxs = meta_and_ctxs
    out = {}
    for i, (asset, ctx) in enumerate(zip(meta["universe"], ctxs)):
        mark = ctx.get("markPx")
        if mark is not None and Decimal(mark) > 0:
            out[i] = Market(asset["name"], Decimal(mark), int(asset["szDecimals"]))
    return out


def parse_book(state: Any, orders: Any, markets: dict[int, Market]) -> Book:
    """`clearinghouseState` and `frontendOpenOrders` for one account."""
    index = {m.coin: i for i, m in markets.items()}
    book = Book(equity=Decimal(state["marginSummary"]["accountValue"]))
    for entry in state.get("assetPositions", []):
        p = entry["position"]
        size = Decimal(p["szi"])
        if size != 0:
            if p["coin"] not in index:
                raise GatewayError(502, "no_market_price", f"no mark for {p['coin']}")
            book.positions[index[p["coin"]]] = size
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
    return book


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


def _moved(mark: Decimal, notional: Decimal, amount: Decimal) -> Decimal:
    """Where the mark is when a position of this notional has gained `amount` (lost, if negative)."""
    return mark * (notional + amount) / notional


def lines(limits: RuleLimits, equity: Decimal, exp: dict[int, dict[str, Decimal]],
          markets: dict[int, Market]) -> dict[tuple[int, str], Line]:
    """The stop and the take for every asset and direction that has exposure."""
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
                    # The stop rounds TOWARDS the mark, so it is never looser than the line. The
                    # take rounds AWAY from it: the room already carries what closing will cost,
                    # and rounding back towards the mark would eat a tick of exactly that. Both
                    # stay at least one tick from the mark.
                    stop = min(valid_px(_moved(m.mark, notional, -budget), m.sz_decimals, ROUND_CEILING),
                               valid_px(m.mark - step, m.sz_decimals, ROUND_FLOOR))
                    take = max(valid_px(_moved(m.mark, notional, room), m.sz_decimals, ROUND_CEILING),
                               valid_px(m.mark + step, m.sz_decimals, ROUND_CEILING))
                else:
                    stop = max(valid_px(_moved(m.mark, notional, budget), m.sz_decimals, ROUND_FLOOR),
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
              hold_takes: bool = False) -> Plan:
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
            elif hold_takes:
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


# Signs one of the gateway's own actions with the account's key and submits it, under a nonce other
# than the one given: (why it failed, whether Hyperliquid confirmed it, Hyperliquid's answer).
Act = Callable[[str, dict, "int | None"], tuple["str | None", bool, Any]]


def log_line(**fields) -> None:
    print(json.dumps({"t": int(time.time()), **fields}, default=str), flush=True)


class Protector:
    def __init__(self, reader: LimitsReader, venue: Venue, act: Act, clock: Callable[[], float] = time.time):
        self.reader = reader
        self.venue = venue
        self.act = act
        self.clock = clock
        self._limits: dict[tuple[str, str], tuple[RuleLimits, float]] = {}
        self._watch: dict[str, str] = {}  # account -> key, for the sweep
        # (account, asset, direction) -> the take triggers the trader asked for and Hyperliquid did
        # not refuse. By direction, because an order the other way gets a take of its own, and placing
        # that one says nothing about this one. In memory only: after a restart every take is the
        # gateway's until its trader moves it again.
        self._trader_takes: dict[tuple[str, int, str], set[Decimal]] = {}
        # Keys whose account could not be read when the gateway started; each sweep tries them again.
        self._pending: list[str] = []
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

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

    def plan(self, account: str, key: str, extra: Extra | None = None) -> tuple[Plan, dict, RuleLimits, Book]:
        markets = self.venue.markets()
        book = self.venue.book(account, markets)
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

    def apply(self, key: str, plan: Plan, avoid_nonce: int | None = None) -> None:
        for action in plan.actions:
            refusal, confirmed, venue = self.act(key, action, avoid_nonce)
            if refusal is not None or not confirmed:
                raise GatewayError(502, "protection_failed",
                                   f"Hyperliquid did not confirm the stop and take: {refusal or venue}"[:300])

    def before_opening(self, account: str, key: str, asset: int, buys: bool, size: Decimal,
                       avoid_nonce: int | None = None) -> list[dict]:
        """Puts the stop and the take in place for an order that may open or grow a position,
        before the order goes. Refuses the order if they can't be placed, or if the account has
        nothing left to lose before its rule line, or a challenge has already met its target."""
        with self._lock(account):
            plan, _, limits, book = self.plan(account, key, Extra(asset, buys, size))
            if book.equity - limits.floor() <= 0:
                raise GatewayError(409, "at_rule_line",
                                   "equity is at the pool's rule line; nothing may open until the account is stopped")
            if limits.challenge and limits.gain_room(book.equity) <= 0:
                raise GatewayError(409, "target_met",
                                   "the challenge has met its target; close and graduate instead of opening more")
            self.apply(key, plan, avoid_nonce)
            self._forget_takes(account, plan.report)
        self.watch(account, key)
        return plan.report

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
        with self._guard:
            self._watch[account] = key

    def take_on(self, keys: list[str]) -> list[str]:
        """Watches every account one of these keys trades right now, and says which.

        The list of accounts to sweep lives in this process and is filled by orders. A gateway that
        has just started has an empty one, so an account with a position open would not be swept
        again until its trader sent the next order: a day's new snapshot would not tighten its stop,
        and its take would not follow funding. The registry knows which account each key is bound
        to, and the account says whether that key trades it now.

        A key whose read fails is kept and tried again by every sweep until the node answers:
        dropped, its account would stay unwatched until the next order or the next restart, which
        is the gap this closes. An account taken on with nothing open costs one read: the first
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

    def _unwatch(self, account: str) -> None:
        with self._guard:
            self._watch.pop(account, None)

    def sweep(self) -> None:
        """One pass over the accounts the gateway has traded: a position that opened from an
        order that rested, a stop that fired while an order still rested, or a new day's
        snapshot that moved the line all get their stop and take here."""
        try:
            self._sweep_watched()
        finally:
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

    def _sweep_watched(self) -> None:
        watched = self.watching()
        if not watched:
            return
        markets = self.venue.markets()
        for account, key in watched.items():
            try:
                with self._lock(account):
                    book = self.venue.book(account, markets)
                    exp = exposure(book)
                    if not exp:
                        self._unwatch(account)
                        self._forget_takes(account)
                        continue
                    limits = self.limits(account, key)
                    want = lines(limits, book.equity, exp, markets)
                    plan = reconcile(book, want, markets, self._follows(book, exp), self._takes_of(account),
                                     hold_takes=not limits.challenge)
                    if not plan.actions:
                        continue
                    # Something to send: only while this key still trades this account.
                    if (self.reader.trading_key(account) or "").lower() != key.lower():
                        self._unwatch(account)
                        log_line(event="protect_unwatched", account=account, reason="not trading with this key")
                        continue
                    self.apply(key, plan)
                    self._forget_takes(account, plan.report)
                    log_line(event="protect_swept", account=account, protection=plan.report)
            except Exception as exc:  # one account must not stop the sweep
                log_line(event="protect_sweep_failed", account=account, error=str(exc)[:200])

    def run(self, every: float, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.wait(every):
            try:
                self.sweep()
            except Exception as exc:
                log_line(event="protect_sweep_failed", error=str(exc)[:200])
