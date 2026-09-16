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
        return {"status": "err", "http": resp.status_code, "body": resp.text[:500]}
