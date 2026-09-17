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
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.signing import sign_l1_action

from .checks import GatewayError
from .signer import SignResult

# The platform's caps per order, by testnet perp index: BTC 3, ETH 4, SOL 0 (CTO, 17 Sep 2026).
MAX_SIZE = {3: Decimal("0.005"), 4: Decimal("0.15"), 0: Decimal("4")}
MAX_NOTIONAL = Decimal("400")  # USDC per order
LIMIT_TIFS = ("Alo", "Gtc", "Ioc")


def _number(text: Any) -> Decimal:
    try:
        value = Decimal(text) if isinstance(text, str) else Decimal("NaN")
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or value <= 0:
        raise GatewayError(403, "policy", f"not a positive decimal string: {text!r}")
    return value


def check_caps(kind: str, action: Any) -> None:
    """Refuses, before signing, anything but one limit order within the caps, or one cancel,
    on an asset of the platform's list."""
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
    if size * price > MAX_NOTIONAL:
        raise GatewayError(403, "over_cap", f"notional {size * price} is over the cap of {MAX_NOTIONAL} USDC")


class DemoSigner:
    def __init__(self, keys: list[LocalAccount]):
        self._keys = {k.address.lower(): k for k in keys}

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
        check_caps(kind, action)
        signature = sign_l1_action(self._keys[key.lower()], action, None, nonce, None, False)
        return SignResult(200, signature, None, {"signature": signature})
