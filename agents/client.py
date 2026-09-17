"""A trader's client for the pool gateway: signs one order or one cancel with the trader's
wallet and posts it. Used by the demo bot and the AI agent."""

from __future__ import annotations

import math
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address

from gateway import auth

USER_AGENT = "colosseum-pools-agent"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def order_url(base: str) -> str:
    """Orders and answers carry account data, so a gateway on another machine has to be
    reached over https."""
    parts = urlsplit(base)
    local = parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
    if parts.scheme != "https" and not local:
        raise ValueError(f"the gateway must be reached over https, not {parts.scheme}://{parts.netloc}")
    return parts._replace(path=parts.path.rstrip("/") + "/v1/order", query="", fragment="").geturl()


class GatewayClient:
    def __init__(self, wallet: LocalAccount, gateway_url: str, timeout: float = 20.0):
        self.wallet = wallet
        self.url = order_url(gateway_url)
        self.timeout = timeout
        self._last_nonce = 0

    def _nonce(self) -> tuple[int, int]:
        # Nonces must be unique per trader; two requests in the same millisecond would collide.
        nonce = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = nonce
        return nonce, nonce + 45_000

    def _post(self, kind: str, fields: dict) -> Any:
        body = {"kind": kind, kind: fields, "signature": auth.sign(self.wallet, kind, fields)}
        resp = requests.post(self.url, json=body, timeout=self.timeout, headers={"User-Agent": USER_AGENT})
        try:
            return {"http": resp.status_code, **resp.json()}
        except ValueError:
            return {"http": resp.status_code, "status": "bad_response", "body": resp.text[:300]}

    def order(self, account: str, asset: int, is_buy: bool, limit_px: str, size: str,
              tif: str = "Gtc", reduce_only: bool = False) -> Any:
        nonce, expires = self._nonce()
        return self._post("order", {
            "account": to_checksum_address(account), "asset": asset, "isBuy": is_buy, "limitPx": limit_px, "size": size,
            "reduceOnly": reduce_only, "tif": tif, "nonce": nonce, "expiresAt": expires,
        })

    def cancel(self, account: str, asset: int, oid: int) -> Any:
        nonce, expires = self._nonce()
        return self._post("cancel", {
            "account": to_checksum_address(account), "asset": asset, "oid": oid,
            "nonce": nonce, "expiresAt": expires,
        })


def canonical(value: float, decimals: int) -> str:
    """A number the way Hyperliquid normalizes it: fixed decimals, no trailing zeros."""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def round_price(px: float, sz_decimals: int) -> str:
    """Five significant figures (an integer price is always allowed), at most 6 - szDecimals
    decimals. Halves round up, as in the app (Python's round() would go to even)."""
    if not px > 0:
        raise ValueError("price must be above zero")
    if px >= 10_000:
        return str(math.floor(px + 0.5))
    return canonical(float(f"{px:.5g}"), 6 - sz_decimals)


def round_size(sz: float, sz_decimals: int) -> str:
    """Size rounded down to the asset's size decimals."""
    factor = 10**sz_decimals
    size = int(sz * factor + 1e-9) / factor
    if not size > 0:
        raise ValueError(f"size rounds to zero at {sz_decimals} decimals")
    return canonical(size, sz_decimals)
