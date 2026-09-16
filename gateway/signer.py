"""Client for the Signer demo gateway. One bearer token per enclave key: the Signer keeps each
key under its own tenant, and the token is what selects it."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any

import requests

USER_AGENT = "colosseum-pools-gateway"


@dataclass
class SignResult:
    http_status: int
    signature: dict | None
    receipt: Any
    body: Any


class SignerClient:
    def __init__(self, base_url: str, tokens: dict[str, str], timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._tokens = {k.lower(): v for k, v in tokens.items()}
        self.timeout = timeout

    @staticmethod
    def load_tokens(path: str) -> dict[str, str]:
        """A JSON object {key address: bearer token}, kept outside the repository with
        owner-only permissions."""
        p = pathlib.Path(path).expanduser()
        if p.stat().st_mode & 0o077:
            raise SystemExit(f"{p} is readable by others; chmod 600 it")
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            raise SystemExit(f"{p} must hold a JSON object")
        return data

    def has_key(self, key: str) -> bool:
        return key.lower() in self._tokens

    def sign(self, key: str, kind: str, action: dict, nonce: int) -> SignResult:
        token = self._tokens[key.lower()]
        body = {"exchange": "hyperliquid_testnet", "kind": kind, "action": action, "nonce": nonce}
        resp = requests.post(
            f"{self.base_url}/sign",
            data=json.dumps(body),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": "non_json_response"}
        signature = payload.get("signature") if isinstance(payload, dict) else None
        receipt = payload.get("receipt") if isinstance(payload, dict) else None
        return SignResult(resp.status_code, signature, receipt, payload)
