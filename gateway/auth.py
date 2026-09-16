"""The trader's authorization for one order: an EIP-712 signature over the account, the exact
Hyperliquid action hash, the nonce and an expiry."""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data

from . import hl

DOMAIN = {"name": "colosseum-pools gateway", "version": "1", "chainId": 998}

TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "GatewayOrder": [
        {"name": "account", "type": "address"},
        {"name": "actionHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint64"},
        {"name": "expiresAt", "type": "uint64"},
    ],
}


def typed_data(account: str, action: dict, nonce: int, expires_at: int) -> dict:
    return {
        "domain": DOMAIN,
        "types": TYPES,
        "primaryType": "GatewayOrder",
        "message": {
            "account": account,
            "actionHash": hl.hash_action(action, nonce),
            "nonce": nonce,
            "expiresAt": expires_at,
        },
    }


def recover_trader(account: str, action: dict, nonce: int, expires_at: int, signature: str) -> str:
    message = encode_typed_data(full_message=typed_data(account, action, nonce, expires_at))
    return Account.recover_message(message, signature=signature)


def sign_order(wallet, account: str, action: dict, nonce: int, expires_at: int) -> str:
    """What a trader's client does. Used by the demo agents and the tests."""
    message = encode_typed_data(full_message=typed_data(account, action, nonce, expires_at))
    return "0x" + wallet.sign_message(message).signature.hex().removeprefix("0x")
