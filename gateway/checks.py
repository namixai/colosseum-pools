"""Everything the gateway checks before it asks the enclave to sign. No network here: the chain
is reached through a ChainReader, so the checks can be tested against a fake."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Protocol

from eth_utils import is_hex_address, to_checksum_address

from . import auth

MAX_ENTRIES = 64
MAX_EXPIRY_MS = 60_000


class GatewayError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.status = status
        self.code = code
        self.detail = detail


class ChainReader(Protocol):
    def is_account(self, account: str) -> bool: ...

    def trading_key(self, account: str) -> str | None:
        """The agent key of an account that is allowed to trade right now, else None."""

    def is_bound(self, key: str, account: str, trader: str) -> bool: ...

    def allowed_assets(self, account: str) -> set[int]: ...


@dataclass(frozen=True)
class OrderRequest:
    account: str
    kind: str
    action: dict
    nonce: int
    expires_at: int
    signature: str

    @staticmethod
    def from_json(body: Any) -> "OrderRequest":
        if not isinstance(body, dict):
            raise GatewayError(400, "bad_request", "body must be a JSON object")
        try:
            account = body["account"]
            kind = body["kind"]
            action = body["action"]
            nonce = body["nonce"]
            expires_at = body["expiresAt"]
            signature = body["signature"]
        except KeyError as missing:
            raise GatewayError(400, "bad_request", f"missing field {missing}") from None
        if not (isinstance(account, str) and is_hex_address(account)):
            raise GatewayError(400, "bad_request", "account is not an address")
        if kind not in ("order", "cancel"):
            raise GatewayError(400, "bad_request", "kind must be order or cancel")
        if not isinstance(action, dict):
            raise GatewayError(400, "bad_request", "action must be an object")
        for name, value in (("nonce", nonce), ("expiresAt", expires_at)):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 2**64:
                raise GatewayError(400, "bad_request", f"{name} must be a positive integer")
        if not (isinstance(signature, str) and signature.startswith("0x") and len(signature) == 132):
            raise GatewayError(400, "bad_request", "signature must be 65 bytes of hex")
        return OrderRequest(to_checksum_address(account), kind, action, nonce, expires_at, signature)


def _entries(req: OrderRequest) -> list[dict]:
    field = "orders" if req.kind == "order" else "cancels"
    if req.action.get("type") != req.kind:
        raise GatewayError(400, "bad_request", "action.type does not match kind")
    other = "cancels" if field == "orders" else "orders"
    if other in req.action:
        raise GatewayError(400, "bad_request", f"an {req.kind} action carries no {other}")
    entries = req.action.get(field)
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise GatewayError(400, "bad_request", f"{field} must hold 1 to {MAX_ENTRIES} entries")
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("a"), int) or isinstance(e.get("a"), bool):
            raise GatewayError(400, "bad_request", "every entry needs an integer asset index a")
    return entries


class NonceBook:
    """(trader, nonce) pairs already used. Hyperliquid refuses a repeated nonce too; this stops
    a replayed request before it costs an enclave call."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def claim(self, trader: str, nonce: int) -> bool:
        with self._lock:
            k = (trader.lower(), nonce)
            if k in self._seen:
                return False
            self._seen.add(k)
            return True


@dataclass(frozen=True)
class Cleared:
    trader: str
    key: str


def check(req: OrderRequest, reader: ChainReader, now_ms: int, nonces: NonceBook) -> Cleared:
    entries = _entries(req)

    if not now_ms < req.expires_at <= now_ms + MAX_EXPIRY_MS:
        raise GatewayError(400, "expired", "expiresAt must be in the next minute")

    try:
        trader = auth.recover_trader(req.account, req.action, req.nonce, req.expires_at, req.signature)
    except Exception:  # malformed signature bytes
        raise GatewayError(401, "bad_signature") from None

    if not reader.is_account(req.account):
        raise GatewayError(403, "not_an_account", req.account)
    key = reader.trading_key(req.account)
    if key is None:
        raise GatewayError(409, "not_trading", "the account is not in a trading state")
    if not reader.is_bound(key, req.account, trader):
        raise GatewayError(403, "not_your_account", "the key on this account is not bound to the signer")

    allowed = reader.allowed_assets(req.account)
    for e in entries:
        if e["a"] not in allowed:
            raise GatewayError(403, "asset_not_allowed", str(e["a"]))

    if not nonces.claim(trader, req.nonce):
        raise GatewayError(409, "replayed", "this nonce was already used")
    return Cleared(trader=trader, key=key)
