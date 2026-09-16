"""The trader's authorization for one order or one cancel: an EIP-712 signature over the
order's own fields, so the wallet shows the person what they are signing (asset, side,
price, size) instead of an opaque hash. The gateway builds the Hyperliquid action from the
same fields, so what is signed and what is submitted can't drift apart."""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data

DOMAIN = {"name": "colosseum-pools gateway", "version": "2", "chainId": 998}

DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
]

ORDER_TYPE = [
    {"name": "account", "type": "address"},
    {"name": "asset", "type": "uint32"},
    {"name": "isBuy", "type": "bool"},
    {"name": "limitPx", "type": "string"},
    {"name": "size", "type": "string"},
    {"name": "reduceOnly", "type": "bool"},
    {"name": "tif", "type": "string"},
    {"name": "nonce", "type": "uint64"},
    {"name": "expiresAt", "type": "uint64"},
]

CANCEL_TYPE = [
    {"name": "account", "type": "address"},
    {"name": "asset", "type": "uint32"},
    {"name": "oid", "type": "uint64"},
    {"name": "nonce", "type": "uint64"},
    {"name": "expiresAt", "type": "uint64"},
]


def typed_data(kind: str, message: dict) -> dict:
    primary = {"order": "Order", "cancel": "Cancel"}[kind]
    fields = ORDER_TYPE if kind == "order" else CANCEL_TYPE
    return {
        "domain": DOMAIN,
        "types": {"EIP712Domain": DOMAIN_TYPE, primary: fields},
        "primaryType": primary,
        "message": message,
    }


def recover_trader(kind: str, message: dict, signature: str) -> str:
    return Account.recover_message(encode_typed_data(full_message=typed_data(kind, message)), signature=signature)


def sign(wallet, kind: str, message: dict) -> str:
    """What a trader's client does. Used by the demo agents and the tests."""
    signed = wallet.sign_message(encode_typed_data(full_message=typed_data(kind, message)))
    return "0x" + signed.signature.hex().removeprefix("0x")
