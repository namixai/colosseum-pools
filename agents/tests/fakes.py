"""Fakes shared by the agent tests: a chain with one challenge and three pools, Hyperliquid's
info API, and the pool gateway."""

from __future__ import annotations

import pathlib

from spike.hlspike import common as c

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
NEEDED = (1_000_000_000 + 5_000_000_000) * 100 + 100_000_000
UNIVERSE = [{"name": "SOL", "szDecimals": 2, "maxLeverage": 20}, {"name": "APT", "szDecimals": 2, "maxLeverage": 10},
            {"name": "ATOM", "szDecimals": 2, "maxLeverage": 10}, {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]


class Wallet:
    address = "0x00000000000000000000000000000000000000D1"


class FakeChain:
    """A challenge on BTC and ETH with 1000 USDC, 5% daily loss, 10% drawdown, 3x leverage,
    and three pools for sale, of which only POOL can sell now."""

    # The tests put this object where `desk.c` is, so it stands in for the whole module -- and the
    # exception the desk catches has to be the REAL class, not a look-alike, or the branch that
    # gives an attempt back would be reached by nothing in production.
    NotSent = c.NotSent
    Reverted = c.Reverted

    def __init__(self):
        self.challenge = True
        self.rules = (500, 1000, 300, (3, 4))
        # price, capital, target, duration, challenge share, funded share, funded capital.
        # The two shares differ: a reader that takes one for the other changes a number.
        self.terms = (20_000_000, 1_000_000_000, 1000, 7 * 86400, 0, 8000, 5_000_000_000)
        self.verdict = 0
        self.status = 2        # Active
        self.stage = 2         # Funded
        self.recorded = 0      # breachReason()/fundedEndReason(): ничего не записано
        self.equity, self.notional = 1000.0, 0.0
        self.mids = {"BTC": "60000", "ETH": "3000"}
        self.positions: list[dict] = []
        self.orders: list[dict] = []
        self.sent: list[tuple[str, str, list]] = []
        self.revert: str | None = None            # refused at the gas estimate: never broadcast
        self.revert_after_send = False             # broadcast, then reverted on chain
        self.not_sent: str | None = None           # never left, and no revert data: a node said no
        # Which call the three flags above apply to; None is every call, so the first one fails. A
        # purchase is two transactions, and a wallet short of the price has the approval go through and
        # the purchase refused: a fake that could only fail the approval cannot show that.
        self.failing_call: str | None = None
        # A pool keeps the block where it cut the funded trader's key. 0 means it never funded
        # anyone -- which is also what a released reservation leaves behind.
        self.cut_block = 0
        self.allowance = 0
        self.fee = 0
        self.pools = {
            # What each pool needs on spot to sell: (capital + funded) * 100 plus the new account's fee.
            POOL.lower(): {"stage": 0, "ready": True, "challenge": "0x" + "00" * 20, "spot": NEEDED},
            POOL_BUSY.lower(): {"stage": 1, "ready": True, "challenge": NEW_CHALLENGE, "spot": NEEDED},
            POOL_POOR.lower(): {"stage": 0, "ready": True, "challenge": "0x" + "00" * 20, "spot": NEEDED - 1},
        }

    def call_view(self, to, signature, types, args, out):
        name = signature.split("(")[0]
        to = to.lower()
        if to == FACTORY.lower():
            return {"isChallenge": (self.challenge,), "isPool": (not self.challenge,), "usdc": (USDC,),
                    "pools": ([POOL, POOL_BUSY, POOL_POOR],), "challengeFee": (self.fee,)}[name]
        if to == USDC.lower():
            return {"decimals": (6,), "allowance": (self.allowance,), "balanceOf": (100_000_000,)}[name]
        if to in self.pools:
            pool = self.pools[to]
            return {"stage": (pool["stage"],), "accountReady": (pool["ready"],), "challenge": (pool["challenge"],),
                    "terms": (self.terms,), "rules": (self.rules,), "capitalNeeded": (NEEDED,)}[name]
        return {"rules": (self.rules,), "terms": (self.terms,), "violation": (self.verdict,),
                "status": (self.status,), "stage": (self.stage,), "breachReason": (self.recorded,),
                "fundedEndReason": (self.recorded,), "cutBlock": (self.cut_block,),
                "drawdownBase": (1_000_000_000,),
                "dayStartEquity": (990_000_000,), "deadline": (NOW + 36 * 3600,)}[name]

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
            return self.mids
        if kind == "metaAndAssetCtxs":
            ctx = {"midPx": "60000", "markPx": "60010", "oraclePx": "60005", "funding": "0.0000125",
                   "openInterest": "12.5", "dayNtlVlm": "1000000", "prevDayPx": "59000"}
            return [{"universe": UNIVERSE}, [ctx for _ in UNIVERSE]]
        if kind == "candleSnapshot":
            return [{"c": str(59000 + i * 40)} for i in range(24)]
        raise AssertionError(f"unexpected info call {kind}")

    def transact(self, wallet, to, signature, types=(), args=()):
        # Two shapes, and the difference is the whole point: a refusal at the GAS ESTIMATE never
        # reached the node, which the real `send_tx` reports as `c.NotSent`; a transaction that was
        # broadcast and reverted on chain is a plain error. A fake that raised one type for both
        # would make the attempt-returned path untestable while looking like it tested it.
        fails = self.failing_call is None or signature == self.failing_call
        if self.not_sent and fails:
            raise c.NotSent(self.not_sent)
        if self.revert and fails:
            raise c.NotSent(f"eth_estimateGas: {{'code': 3, 'message': 'execution reverted', 'data': '{self.revert}'}}")
        if self.revert_after_send and fails:
            self.sent.append((to.lower(), signature.split("(")[0], list(args)))
            raise c.Reverted(f"transaction 0x{'cd' * 32} reverted")
        self.sent.append((to.lower(), signature.split("(")[0], list(args)))
        if signature == "buyChallenge()":
            self.pools[to.lower()]["challenge"] = NEW_CHALLENGE
        return {"transactionHash": "0x" + "ab" * 32}

    def artifact(self, contract):
        # A clone where `forge build` has not run, as `spike/hlspike/common.py` answers in one. The
        # desk has to name a contract's refusal without the build output.
        raise SystemExit(f"missing out/{contract}.sol/{contract}.json; run `forge build` first")


# What the live gateway answers when its own chain reads are rate limited: it refuses BEFORE it
# signs anything, so nothing reaches Hyperliquid.
BUSY = {"http": 429, "status": "busy", "code": "upstream_busy",
        "detail": "the chain node is refusing reads right now; try again in a moment"}


class FakeGateway:
        # The desk asks the gateway what it will take per order. The fake answers the demo signer's
    # real number so the tests size against the same cap production does; a test that wants the
    # unpublished case sets `says_cap = None`.
    says_cap: float | None = 400.0

    def max_order_notional(self, fallback):
        return fallback if self.says_cap is None else self.says_cap

    def __init__(self, busy_for=0):
        self.orders, self.cancels = [], []
        self.busy_for = busy_for   # answer this many calls with busy before accepting any

    def _answer(self):
        if self.busy_for > 0:
            self.busy_for -= 1
            return dict(BUSY)
        return {"http": 200, "status": "submitted"}

    def order(self, account, asset, is_buy, px, size, tif="Gtc", reduce_only=False):
        self.orders.append((account, asset, is_buy, px, size, tif, reduce_only))
        return self._answer()

    def cancel(self, account, asset, oid):
        self.cancels.append((account, asset, oid))
        return self._answer()
