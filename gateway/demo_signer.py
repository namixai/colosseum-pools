"""The demo's signer: testnet agent keys held by the gateway itself, and the platform's caps
checked in code before anything is signed.

The demo doesn't use Usenami Signer. Its agent keys are ordinary testnet keys made for it
(`ops/make_demo_keys.py`), kept on the gateway host as owner-only files, one `<name>.key`
per key; the address comes from the key, never from a file name. `KeyRegistry` publishes
the addresses as before. A key file can sign anything Hyperliquid lets an agent sign, so
whoever holds the host holds these keys.

On a deployment with a key binder (src/KeyBinder.sol) no key is made in advance: the keeper's
relay writes one into the directory just before it is bound (ops/key_relay.py). So a key the
signer does not hold sends it back to the directory, at most once every RELOAD_EVERY_S, before
the order is refused; a directory that cannot be read then leaves the keys it had.
"""

from __future__ import annotations

import json
import pathlib
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.signing import sign_l1_action

from . import hl
from .checks import GatewayError
from .signer import SignResult

# The platform's list of perps and a size cap for each, per order, by order number
# (gateway/assets.py), is the platform's setting and not code (CTO, 21 Oct 2026: perps are added and
# taken off without a release). The gateway reads it from GATEWAY_SIZE_CAPS, JSON
# {"<order number>": "<size>"}; a perp with no cap there is not signed. On testnet since 17 Sep 2026
# the CTO's caps are {"3": "0.005", "4": "0.15", "0": "4"}: BTC, ETH and SOL.
MAX_NOTIONAL = Decimal("400")  # USDC per order
LIMIT_TIFS = ("Alo", "Gtc", "Ioc")
ORDER_FIELDS = ["a", "b", "p", "s", "r", "t"]
TRIGGER_FIELDS = ["isMarket", "triggerPx", "tpsl"]


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


def size_caps(text: str) -> dict[int, Decimal]:
    """GATEWAY_SIZE_CAPS: each perp's size cap by order number. Empty means no perp is signed."""
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"GATEWAY_SIZE_CAPS is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise SystemExit("GATEWAY_SIZE_CAPS must be a JSON object of order number -> size")
    caps = {}
    for asset, size in raw.items():
        cap = _positive(size)
        if not (isinstance(asset, str) and asset.isdigit()) or cap is None:
            raise SystemExit(f"GATEWAY_SIZE_CAPS: {asset!r}: {size!r} is not an order number and a positive size")
        caps[int(asset)] = cap
    return caps


def market_mid(asset: int) -> Decimal:
    """The asset's mid on Hyperliquid's testnet now. Without one, a sell isn't signed."""
    coin = hl.coin_of(asset)
    # A HIP-3 perp's mids are its dex's: "xyz:GOLD" is asked of dex "xyz".
    mids = hl.mids(dex=coin.partition(":")[0] if ":" in coin else hl.MAIN_DEX)
    mid = _positive(mids.get(coin)) if isinstance(mids, dict) else None
    if mid is None:
        raise GatewayError(502, "no_market_price", f"Hyperliquid gave no mid for {coin}")
    return mid


def _protective(order: Any, caps: Mapping[int, Decimal]) -> None:
    """One of the gateway's own stop or take orders (gateway/protect.py): a position TP/SL that
    is reduce-only, sized 0 (the whole position, whatever it is), and a market trigger. It can
    only close a position, so no cap applies to it."""
    t = order.get("t") if isinstance(order, dict) else None
    trigger = t.get("trigger") if isinstance(t, dict) and list(t) == ["trigger"] else None
    if not (isinstance(order, dict) and list(order) == ORDER_FIELDS and isinstance(trigger, dict)
            and list(trigger) == TRIGGER_FIELDS and trigger["isMarket"] is True and trigger["tpsl"] in ("sl", "tp")
            and order["r"] is True and order["s"] == "0" and isinstance(order["b"], bool)):
        raise GatewayError(403, "policy", "not a reduce-only stop or take for the whole position")
    if order["a"] not in caps:
        raise GatewayError(403, "policy", f"asset {order['a']} is not on the platform list")
    _number(order["p"])
    _number(trigger["triggerPx"])


def check_protective(action: Any, caps: Mapping[int, Decimal]) -> None:
    """Placing the gateway's stop and take (one or two position TP/SL orders), or moving one."""
    if action.get("type") == "order" and list(action) == ["type", "orders", "grouping"]:
        orders = action["orders"]
        if action["grouping"] != "positionTpsl" or not isinstance(orders, list) or not 1 <= len(orders) <= 2:
            raise GatewayError(403, "policy", "a stop and a take are one or two position TP/SL orders")
        for order in orders:
            _protective(order, caps)
        # One asset and one direction per action: Hyperliquid refuses a position TP/SL action that
        # mixes the two sides as a whole ("Trigger order has unexpected side", 28 Sep 2026).
        if len({(order["a"], order["b"]) for order in orders}) != 1:
            raise GatewayError(403, "policy", "a stop and a take for one asset and one direction per action")
        return
    if action.get("type") == "batchModify" and list(action) == ["type", "modifies"]:
        modifies = action["modifies"]
        if not (isinstance(modifies, list) and len(modifies) == 1 and isinstance(modifies[0], dict)
                and list(modifies[0]) == ["oid", "order"] and type(modifies[0]["oid"]) is int and modifies[0]["oid"] > 0):
            raise GatewayError(403, "policy", "one stop or take moved at a time, by its oid")
        _protective(modifies[0]["order"], caps)
        return
    raise GatewayError(403, "policy", "not a stop or a take")


def check_close(action: Any, caps: Mapping[int, Decimal]) -> None:
    """The gateway's own close of a position (gateway/protect.py, `close_action`): one reduce-only IOC
    order, alone, on an asset of the platform's list. Reduce-only, it can only close what the account
    holds, so no size cap applies to it."""
    orders = action.get("orders") if isinstance(action, dict) else None
    order = orders[0] if isinstance(orders, list) and len(orders) == 1 else None
    if not (list(action) == ["type", "orders", "grouping"] and action["type"] == "order" and action["grouping"] == "na"
            and isinstance(order, dict) and list(order) == ORDER_FIELDS and order["r"] is True
            and order["t"] == {"limit": {"tif": "Ioc"}} and isinstance(order["b"], bool)):
        raise GatewayError(403, "policy", "not one reduce-only IOC order that closes a position")
    if order["a"] not in caps:
        raise GatewayError(403, "policy", f"asset {order['a']} is not on the platform list")
    _number(order["p"])
    _number(order["s"])


def check_caps(kind: str, action: Any, mid: Callable[[int], Decimal], caps: Mapping[int, Decimal]) -> None:
    """Refuses, before signing, anything but one limit order within the caps, one cancel, or the
    gateway's own stop and take, on an asset of the platform's list. `mid` gives an asset's
    market mid; only a sell that isn't reduce-only asks."""
    if not isinstance(action, dict):
        raise GatewayError(403, "policy", "not an action")
    if kind == "protect":
        check_protective(action, caps)
        return
    if kind == "close":
        check_close(action, caps)
        return
    if kind == "cancel":
        cancels = action.get("cancels")
        if (action.get("type") != "cancel" or not isinstance(cancels, list) or len(cancels) != 1
                or not isinstance(cancels[0], dict)):
            raise GatewayError(403, "policy", "one cancel at a time")
        if cancels[0].get("a") not in caps:
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
    if asset not in caps:
        raise GatewayError(403, "policy", f"asset {asset} is not on the platform list")
    size, price = _number(order.get("s")), _number(order.get("p"))
    if size > caps[asset]:
        raise GatewayError(403, "over_cap", f"size {size} is over the cap of {caps[asset]} for asset {asset}")
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
    # What this signer refuses above, per order, so `/v1/health` can say it and a client need not
    # guess. A client that guesses high sends an order the signer rejects, and the refusal is not
    # `busy`, so it costs the agent one of its few daily orders for a number it could have read.
    # The ENCLAVE signer has no such attribute on purpose: it enforces its own caps and this
    # gateway does not know them, so the field is absent rather than wrong.
    max_order_notional_usdc = float(MAX_NOTIONAL)

    RELOAD_EVERY_S = 5.0

    def __init__(self, keys: list[LocalAccount], mid: Callable[[int], Decimal] = market_mid,
                 caps: Mapping[int, Decimal] | None = None, directory: str | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._keys = {k.address.lower(): k for k in keys}
        self._mid = mid
        self.caps = dict(caps or {})  # the platform's list and its size caps: none, nothing is signed
        self._directory = directory
        self._clock = clock
        # The first key it does not hold reads the directory at once; the next not sooner than RELOAD_EVERY_S on.
        self._reloaded_at = clock() - self.RELOAD_EVERY_S

    @staticmethod
    def load_keys(directory: str, may_be_empty: bool = False) -> list[LocalAccount]:
        """Every `*.key` file in an owner-only directory, each owner-only too. None at all is refused unless the
        keys come later: on a deployment with a key binder each is written just before it is bound."""
        d = pathlib.Path(directory).expanduser()
        if d.stat().st_mode & 0o077:
            raise SystemExit(f"{d} is open to others; chmod 700 it")
        keys = []
        for f in sorted(d.glob("*.key")):
            if f.stat().st_mode & 0o077:
                raise SystemExit(f"{f} is readable by others; chmod 600 it")
            keys.append(Account.from_key(f.read_text().strip()))
        if not keys and not may_be_empty:
            raise SystemExit(f"no *.key files in {d}")
        return keys

    def __len__(self) -> int:
        return len(self._keys)

    def has_key(self, key: str) -> bool:
        if key.lower() not in self._keys:
            self._reload()
        return key.lower() in self._keys

    def _reload(self) -> None:
        """The directory read again, for a key bound since the last read; never sooner than RELOAD_EVERY_S after
        it, so a stream of orders for keys nobody holds does not read the disk on each. Keys are only added."""
        if self._directory is None or self._clock() - self._reloaded_at < self.RELOAD_EVERY_S:
            return
        self._reloaded_at = self._clock()
        # One file at a time, under the start's rules: a directory open to others is not read at all, and a file that
        # is open to others or is not a key is passed over, so it never keeps the next key out.
        d = pathlib.Path(self._directory).expanduser()
        try:
            if d.stat().st_mode & 0o077:
                return
            files = sorted(d.glob("*.key"))
        except OSError:
            return
        for f in files:
            try:
                if f.stat().st_mode & 0o077:
                    continue
                k = Account.from_key(f.read_text().strip())
            except (OSError, ValueError):
                continue
            self._keys.setdefault(k.address.lower(), k)

    def addresses(self) -> list[str]:
        """The keys this signer holds, by address: what the gateway asks the registry about when it
        starts. The enclave signer has no such list here, and a gateway on it takes nothing on."""
        return [k.address for k in self._keys.values()]

    def sign(self, key: str, kind: str, action: dict, nonce: int) -> SignResult:
        check_caps(kind, action, self._mid, self.caps)
        signature = sign_l1_action(self._keys[key.lower()], action, None, nonce, None, False)
        return SignResult(200, signature, None, {"signature": signature})
