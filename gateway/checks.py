"""Everything the gateway checks before it asks the enclave to sign. No network here: the chain
is reached through a ChainReader, so the checks can be tested against a fake."""

from __future__ import annotations

import heapq
import re
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from eth_utils import is_hex_address, to_checksum_address

from . import auth

MAX_EXPIRY_MS = 60_000
# Requests live for at most a minute, so the nonce book holds at most a minute of traffic;
# past this many entries the gateway answers "busy" instead of growing.
MAX_NONCES = 100_000
TIFS = ("Alo", "Gtc", "Ioc")
# Hyperliquid normalizes numbers before it checks a signature, so a non-canonical string
# ("0.0010", "60000.0") signs one thing and verifies another. Only canonical ones pass.
DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,15})(\.[0-9]{0,9}[1-9])?$")
U32 = 2**32
U64 = 2**64


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


def _bad(detail: str) -> GatewayError:
    return GatewayError(400, "bad_request", detail)


def _uint(fields: dict, name: str, limit: int, minimum: int = 0) -> int:
    v = fields.get(name)
    if not isinstance(v, int) or isinstance(v, bool) or not minimum <= v < limit:
        raise _bad(f"{name} must be an integer in [{minimum}, {limit})")
    return v


def _flag(fields: dict, name: str) -> bool:
    v = fields.get(name)
    if not isinstance(v, bool):
        raise _bad(f"{name} must be true or false")
    return v


def _decimal(fields: dict, name: str) -> str:
    v = fields.get(name)
    if not isinstance(v, str) or not DECIMAL.match(v) or float(v) <= 0:
        raise _bad(f"{name} must be a positive number written canonically, like 0.0002 or 60000")
    return v


@dataclass(frozen=True)
class Request:
    kind: str
    message: dict
    signature: str

    @property
    def account(self) -> str:
        return self.message["account"]

    @property
    def asset(self) -> int:
        return self.message["asset"]

    @property
    def nonce(self) -> int:
        return self.message["nonce"]

    @property
    def expires_at(self) -> int:
        return self.message["expiresAt"]

    @staticmethod
    def from_json(body: Any) -> "Request":
        if not isinstance(body, dict):
            raise _bad("body must be a JSON object")
        kind = body.get("kind")
        if kind not in ("order", "cancel"):
            raise _bad("kind must be order or cancel")
        fields = body.get(kind)
        if not isinstance(fields, dict):
            raise _bad(f"missing object {kind}")
        signature = body.get("signature")
        if not (isinstance(signature, str) and signature.startswith("0x") and len(signature) == 132):
            raise _bad("signature must be 65 bytes of hex")

        account = fields.get("account")
        if not (isinstance(account, str) and is_hex_address(account)):
            raise _bad("account is not an address")
        message: dict = {"account": to_checksum_address(account), "asset": _uint(fields, "asset", U32)}
        if kind == "order":
            tif = fields.get("tif")
            if tif not in TIFS:
                raise _bad(f"tif must be one of {', '.join(TIFS)}")
            message.update({
                "isBuy": _flag(fields, "isBuy"),
                "limitPx": _decimal(fields, "limitPx"),
                "size": _decimal(fields, "size"),
                "reduceOnly": _flag(fields, "reduceOnly"),
                "tif": tif,
            })
        else:
            message["oid"] = _uint(fields, "oid", U64, 1)
        message["nonce"] = _uint(fields, "nonce", U64, 1)
        message["expiresAt"] = _uint(fields, "expiresAt", U64, 1)
        expected = set(message)
        if set(fields) != expected:
            raise _bad(f"{kind} must carry exactly: {', '.join(sorted(expected))}")
        return Request(kind, message, signature)

    def action(self) -> dict:
        """The Hyperliquid action, with keys in the order Hyperliquid hashes them."""
        m = self.message
        if self.kind == "order":
            return {
                "type": "order",
                "orders": [{
                    "a": m["asset"], "b": m["isBuy"], "p": m["limitPx"], "s": m["size"],
                    "r": m["reduceOnly"], "t": {"limit": {"tif": m["tif"]}},
                }],
                "grouping": "na",
            }
        return {"type": "cancel", "cancels": [{"a": m["asset"], "o": m["oid"]}]}


class NonceBook:
    """(trader, nonce) pairs already used. Hyperliquid refuses a repeated nonce too; this stops
    a replayed request before it costs an enclave call.

    An entry is kept until its request expires. After that the request is refused as expired
    before its nonce is looked up, so the entry has nothing left to stop."""

    def __init__(self, limit: int = MAX_NONCES) -> None:
        self._seen: dict[tuple[str, int], int] = {}
        self._by_expiry: list[tuple[int, tuple[str, int]]] = []
        self._limit = limit
        self._lock = threading.Lock()

    def claim(self, trader: str, nonce: int, expires_at: int, now_ms: int) -> bool:
        with self._lock:
            while self._by_expiry and self._by_expiry[0][0] <= now_ms:
                expired, old = heapq.heappop(self._by_expiry)
                # A pair given back and claimed again has a newer entry; this one is stale.
                if self._seen.get(old) == expired:
                    del self._seen[old]
            k = (trader.lower(), nonce)
            if k in self._seen:
                return False
            if len(self._seen) >= self._limit:
                raise GatewayError(503, "busy", "too many requests in the last minute")
            self._seen[k] = expires_at
            heapq.heappush(self._by_expiry, (expires_at, k))
            return True

    def release(self, trader: str, nonce: int) -> None:
        """Gives a pair back when its request never reached Hyperliquid, so the same signed
        request can be retried. Its expiry entry stays and is dropped when it comes due."""
        with self._lock:
            self._seen.pop((trader.lower(), nonce), None)


@dataclass(frozen=True)
class Cleared:
    trader: str
    key: str


def check(req: Request, reader: ChainReader, now_ms: int, nonces: NonceBook) -> Cleared:
    if not now_ms < req.expires_at <= now_ms + MAX_EXPIRY_MS:
        raise GatewayError(400, "expired", "expiresAt must be in the next minute")

    try:
        trader = auth.recover_trader(req.kind, req.message, req.signature)
    except Exception:  # malformed signature bytes
        raise GatewayError(401, "bad_signature") from None

    if not reader.is_account(req.account):
        raise GatewayError(403, "not_an_account", req.account)
    key = reader.trading_key(req.account)
    if key is None:
        raise GatewayError(409, "not_trading", "the account is not in a trading state")
    if not reader.is_bound(key, req.account, trader):
        raise GatewayError(403, "not_your_account", "the key on this account is not bound to the signer")
    if req.asset not in reader.allowed_assets(req.account):
        raise GatewayError(403, "asset_not_allowed", str(req.asset))

    if not nonces.claim(trader, req.nonce, req.expires_at, now_ms):
        raise GatewayError(409, "replayed", "this nonce was already used")
    return Cleared(trader=trader, key=key)
