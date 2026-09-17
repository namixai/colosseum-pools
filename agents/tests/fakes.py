"""Fakes shared by the agent tests: a chain with one challenge and three pools, Hyperliquid's
info API, and the pool gateway."""

from __future__ import annotations

import pathlib

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
        if kind == "metaAndAssetCtxs":
            ctx = {"midPx": "60000", "markPx": "60010", "oraclePx": "60005", "funding": "0.0000125",
                   "openInterest": "12.5", "dayNtlVlm": "1000000", "prevDayPx": "59000"}
            return [{"universe": UNIVERSE}, [ctx for _ in UNIVERSE]]
        if kind == "candleSnapshot":
            return [{"c": str(59000 + i * 40)} for i in range(24)]
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
