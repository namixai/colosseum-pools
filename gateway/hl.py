"""Hyperliquid pieces the gateway needs: the action hash, recovering who signed an L1 action,
submitting a signed action, the mids the demo's caps count a sell at, and what the stop and the
take are worked out from (gateway/protect.py). Testnet only."""

from __future__ import annotations

from typing import Any

import requests
from hyperliquid.utils.signing import action_hash, recover_agent_or_user_from_l1_action

from .protect import Book, Market, parse_book, parse_markets

TESTNET_EXCHANGE_URL = "https://api.hyperliquid-testnet.xyz/exchange"
TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
USER_AGENT = "colosseum-pools-gateway"


def hash_action(action: dict, nonce: int) -> bytes:
    """keccak(msgpack(action) || nonce || no-vault flag): what Hyperliquid signs over.
    The dict's key order is part of the bytes, so the action must travel unchanged."""
    return action_hash(action, None, nonce, None)


def recover_signer(action: dict, signature: dict, nonce: int) -> str:
    """The address Hyperliquid will attribute a testnet L1 action to (phantom agent, source b)."""
    return recover_agent_or_user_from_l1_action(action, signature, None, nonce, None, False)


def _info(body: dict, timeout: float) -> Any:
    resp = requests.post(TESTNET_INFO_URL, json=body, timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


class Venue:
    """The reads the stop and the take are worked out from: every perp's mark, and one account's
    equity, positions and open orders."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def markets(self) -> dict[int, Market]:
        return parse_markets(_info({"type": "metaAndAssetCtxs"}, self.timeout))

    def book(self, account: str, markets: dict[int, Market]) -> Book:
        state = _info({"type": "clearinghouseState", "user": account}, self.timeout)
        orders = _info({"type": "frontendOpenOrders", "user": account}, self.timeout)
        return parse_book(state, orders, markets)


def mids(timeout: float = 5.0) -> Any:
    """Every mid on Hyperliquid's testnet, keyed by coin name, as strings."""
    return _info({"type": "allMids"}, timeout)


def submit(action: dict, nonce: int, signature: dict, timeout: float = 15.0) -> Any:
    body = {"action": action, "nonce": nonce, "signature": signature, "vaultAddress": None}
    resp = requests.post(TESTNET_EXCHANGE_URL, json=body, timeout=timeout, headers={"User-Agent": USER_AGENT})
    try:
        return resp.json()
    except ValueError:
        return {"status": "unreadable", "http": resp.status_code, "body": resp.text[:500]}


def _confirmed(status: Any) -> bool:
    return status in ("success", "waitingForTrigger") or (isinstance(status, dict) and any(
        isinstance(status.get(outcome), dict) for outcome in ("resting", "filled")))


def venue_outcome(answer: Any) -> tuple[str | None, bool]:
    """(Hyperliquid's reason for refusing, whether it confirmed the action).

    Hyperliquid answers `{"status": "err", "response": reason}` when it refuses the whole
    action, and `{"status": "ok", "response": {"data": {"statuses": [...]}}}` otherwise. A
    trader's order is a limit order without grouping and gets `{"resting": {...}}`,
    `{"filled": {...}}` or `{"error": reason}`; a cancel by oid gets `"success"` or
    `{"error": reason}`. The gateway's own stop and take are position TP/SL orders, which get
    `"waitingForTrigger"` (with no oid), and moving one is a modify, which gets `{"resting": {...}}`
    (measured on testnet, 28 Sep 2026). Anything else, `{}` included, confirms nothing."""
    if not isinstance(answer, dict):
        return None, False
    if answer.get("status") == "err":
        return str(answer.get("response") or "refused")[:300], False
    if answer.get("status") != "ok":
        return None, False
    response = answer.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list) or not statuses:
        return None, False
    for status in statuses:
        if isinstance(status, dict) and "error" in status:
            return str(status["error"])[:300], False
    return None, all(_confirmed(status) for status in statuses)
