"""Hyperliquid pieces the gateway needs: the action hash, recovering who signed an L1 action,
and submitting a signed action. Testnet only."""

from __future__ import annotations

from typing import Any

import requests
from hyperliquid.utils.signing import action_hash, recover_agent_or_user_from_l1_action

TESTNET_EXCHANGE_URL = "https://api.hyperliquid-testnet.xyz/exchange"
USER_AGENT = "colosseum-pools-gateway"


def hash_action(action: dict, nonce: int) -> bytes:
    """keccak(msgpack(action) || nonce || no-vault flag): what Hyperliquid signs over.
    The dict's key order is part of the bytes, so the action must travel unchanged."""
    return action_hash(action, None, nonce, None)


def recover_signer(action: dict, signature: dict, nonce: int) -> str:
    """The address Hyperliquid will attribute a testnet L1 action to (phantom agent, source b)."""
    return recover_agent_or_user_from_l1_action(action, signature, None, nonce, None, False)


def submit(action: dict, nonce: int, signature: dict, timeout: float = 15.0) -> Any:
    body = {"action": action, "nonce": nonce, "signature": signature, "vaultAddress": None}
    resp = requests.post(TESTNET_EXCHANGE_URL, json=body, timeout=timeout, headers={"User-Agent": USER_AGENT})
    try:
        return resp.json()
    except ValueError:
        return {"status": "unreadable", "http": resp.status_code, "body": resp.text[:500]}


def _confirmed(status: Any) -> bool:
    return status == "success" or (isinstance(status, dict) and any(
        isinstance(status.get(outcome), dict) for outcome in ("resting", "filled")))


def venue_outcome(answer: Any) -> tuple[str | None, bool]:
    """(Hyperliquid's reason for refusing, whether it confirmed the action).

    Hyperliquid answers `{"status": "err", "response": reason}` when it refuses the whole
    action, and `{"status": "ok", "response": {"data": {"statuses": [...]}}}` otherwise. The
    gateway sends only limit orders without grouping and cancels by oid: an order gets
    `{"resting": {...}}`, `{"filled": {...}}` or `{"error": reason}`, a cancel `"success"` or
    `{"error": reason}`. Anything else, `{}` included, confirms nothing."""
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
