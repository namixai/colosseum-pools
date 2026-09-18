"""The demo's signer: testnet agent keys held by the gateway itself, and the platform's caps
checked in code before anything is signed.

The demo doesn't use Usenami Signer. Its agent keys are ordinary testnet keys made for it
(`ops/make_demo_keys.py`), kept on the gateway host as owner-only files, one `<name>.key`
per key; the address comes from the key, never from a file name. `KeyRegistry` publishes
the addresses as before. A key file can sign anything Hyperliquid lets an agent sign, so
whoever holds the host holds these keys.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.signing import sign_l1_action

from . import hl
from .checks import GatewayError
from .signer import SignResult

# The platform's caps per order, by testnet perp index: BTC 3, ETH 4, SOL 0 (CTO, 17 Sep 2026).
MAX_SIZE = {3: Decimal("0.005"), 4: Decimal("0.15"), 0: Decimal("4")}
# The same perps by name, which is how Hyperliquid's testnet lists their mids.
COINS = {3: "BTC", 4: "ETH", 0: "SOL"}
MAX_NOTIONAL = Decimal("400")  # USDC per order
LIMIT_TIFS = ("Alo", "Gtc", "Ioc")


def _positive(text: Any) -> Decimal | None:
    try:
        value = Decimal(text) if isinstance(text, str) else None
    except InvalidOperation:
        value = None
    return value if value is not None and value.is_finite() and value > 0 else None


def _number(text: Any) -> Decimal:
    value = _positive(text)
    if value is None:
        raise GatewayError(403, "policy", f"not a positive decimal string: {text!r}")
    return value


def market_mid(asset: int) -> Decimal:
    """The asset's mid on Hyperliquid's testnet now. Without one, a sell isn't signed."""
    mids = hl.mids()
    mid = _positive(mids.get(COINS[asset])) if isinstance(mids, dict) else None
    if mid is None:
        raise GatewayError(502, "no_market_price", f"Hyperliquid gave no mid for {COINS[asset]}")
    return mid


def check_caps(kind: str, action: Any, mid: Callable[[int], Decimal]) -> None:
    """Refuses, before signing, anything but one limit order within the caps, or one cancel,
    on an asset of the platform's list. `mid` gives an asset's market mid; only a sell that
    isn't reduce-only asks."""
    if not isinstance(action, dict):
        raise GatewayError(403, "policy", "not an action")
    if kind == "cancel":
        cancels = action.get("cancels")
        if (action.get("type") != "cancel" or not isinstance(cancels, list) or len(cancels) != 1
                or not isinstance(cancels[0], dict)):
            raise GatewayError(403, "policy", "one cancel at a time")
        if cancels[0].get("a") not in MAX_SIZE:
            raise GatewayError(403, "policy", f"asset {cancels[0].get('a')} is not on the platform list")
        return
    orders = action.get("orders")
    if (kind != "order" or action.get("type") != "order" or action.get("grouping") != "na"
            or not isinstance(orders, list) or len(orders) != 1 or not isinstance(orders[0], dict)):
        raise GatewayError(403, "policy", "one ungrouped order at a time")
    order = orders[0]
    t = order.get("t")
    if not (isinstance(t, dict) and set(t) == {"limit"} and isinstance(t["limit"], dict)
            and t["limit"].get("tif") in LIMIT_TIFS):
        raise GatewayError(403, "policy", "limit orders only")
    asset = order.get("a")
    if asset not in MAX_SIZE:
        raise GatewayError(403, "policy", f"asset {asset} is not on the platform list")
    size, price = _number(order.get("s")), _number(order.get("p"))
    if size > MAX_SIZE[asset]:
        raise GatewayError(403, "over_cap", f"size {size} is over the cap of {MAX_SIZE[asset]} for asset {asset}")
    # The notional cap is on opening and growing a position (CTO, 18 Sep 2026). A reduce-only
    # order can't grow one, Hyperliquid refuses that, and a position that grew with the price
    # couldn't be closed in one order under the cap. The size cap still binds it.
    if order.get("r") is True:
        return
    # A buy never fills above its limit. A sell never fills below it, and one priced under the
    # market fills at the bids, all under the mid; so a sell counts at its limit or at the mid,
    # whichever is higher. At its limit alone, 4 SOL offered at 90 counted as 360 USDC and
    # filled at the market: 423 at a mid of 105.8 (18 Sep 2026).
    highest = price if order.get("b") is True else max(price, mid(asset))
    if size * highest > MAX_NOTIONAL:
        raise GatewayError(403, "over_cap", f"notional {size * highest} is over the cap of {MAX_NOTIONAL} USDC")


class DemoSigner:
    def __init__(self, keys: list[LocalAccount], mid: Callable[[int], Decimal] = market_mid):
        self._keys = {k.address.lower(): k for k in keys}
        self._mid = mid

    @staticmethod
    def load_keys(directory: str) -> list[LocalAccount]:
        """Every `*.key` file in an owner-only directory, each owner-only too."""
        d = pathlib.Path(directory).expanduser()
        if d.stat().st_mode & 0o077:
            raise SystemExit(f"{d} is open to others; chmod 700 it")
        keys = []
        for f in sorted(d.glob("*.key")):
            if f.stat().st_mode & 0o077:
                raise SystemExit(f"{f} is readable by others; chmod 600 it")
            keys.append(Account.from_key(f.read_text().strip()))
        if not keys:
            raise SystemExit(f"no *.key files in {d}")
        return keys

    def __len__(self) -> int:
        return len(self._keys)

    def has_key(self, key: str) -> bool:
        return key.lower() in self._keys

    def sign(self, key: str, kind: str, action: dict, nonce: int) -> SignResult:
        check_caps(kind, action, self._mid)
        signature = sign_l1_action(self._keys[key.lower()], action, None, nonce, None, False)
        return SignResult(200, signature, None, {"signature": signature})
