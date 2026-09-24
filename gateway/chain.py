"""Reads the gateway takes from HyperEVM testnet. Every answer comes from the chain at the
latest block; nothing is cached, so a stop lands in the gateway's view as soon as it is mined."""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

import eth_abi
import requests
from eth_utils import keccak, to_checksum_address

CHAIN_ID = 998
USER_AGENT = "colosseum-pools-gateway"
RATE_LIMITED = -32005  # what the public HyperEVM RPC answers when it throttles
# An order is waiting on these reads, so a throttled one is retried after 0.25 s, 0.5 s, 1 s
# and 2 s. Three tries inside a second was not enough: on 24 Sep 2026 the node refused all
# three, the trader's order came back as a gateway error, and the very same reads went through
# seconds later. Four seconds of patience is cheap next to telling a visitor the demo is broken.
RPC_ATTEMPTS = 5


class Throttled(RuntimeError):
    """The node refused every attempt because it is rate limiting this caller, not because the
    read was wrong. Worth telling apart: waiting helps, and the visitor can be told so."""

# ChallengeAccount.Status.Active and Pool.Stage.Funded
CHALLENGE_ACTIVE = 2
POOL_FUNDED = 2


class JsonRpcReader:
    def __init__(self, rpc_url: str, factory: str, registry: str, timeout: float = 10.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.rpc_url = rpc_url
        self._sleep = sleep
        self.factory = to_checksum_address(factory)
        self.registry = to_checksum_address(registry)
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Content-Type": "application/json"})

    # ── plumbing ─────────────────────────────────────────────────────────────────────

    def _rpc(self, method: str, params: Sequence[Any]) -> Any:
        delay = 0.25
        for attempt in range(RPC_ATTEMPTS):
            resp = self._session.post(
                self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
                timeout=self.timeout,
            )
            if resp.status_code != 429:
                resp.raise_for_status()
                body = resp.json()
                error = body.get("error")
                if error is None:
                    return body["result"]
                if not (isinstance(error, dict) and error.get("code") == RATE_LIMITED):
                    raise RuntimeError(f"{method}: {error}")
            if attempt + 1 < RPC_ATTEMPTS:
                self._sleep(delay)
                delay *= 2
        raise Throttled(f"{method}: rate limited {RPC_ATTEMPTS} times in a row")

    def check_chain(self) -> None:
        chain = int(self._rpc("eth_chainId", []), 16)
        if chain != CHAIN_ID:
            raise SystemExit(f"refusing to start: RPC chain id is {chain}, expected {CHAIN_ID}")

    def _call(self, to: str, signature: str, types: Sequence[str], args: Sequence[Any], out: Sequence[str]) -> tuple:
        data = keccak(text=signature)[:4] + eth_abi.encode(list(types), list(args))
        raw = self._rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])
        return eth_abi.decode(list(out), bytes.fromhex(raw[2:]))

    # ── ChainReader ──────────────────────────────────────────────────────────────────

    def is_account(self, account: str) -> bool:
        return self._call(self.factory, "isAccount(address)", ["address"], [account], ["bool"])[0]

    def trading_key(self, account: str) -> str | None:
        if self._call(self.factory, "isChallenge(address)", ["address"], [account], ["bool"])[0]:
            status = self._call(account, "status()", [], [], ["uint8"])[0]
            trading = status == CHALLENGE_ACTIVE
        else:
            stage = self._call(account, "stage()", [], [], ["uint8"])[0]
            trading = stage == POOL_FUNDED
        if not trading:
            return None
        key = self._call(account, "agentKey()", [], [], ["address"])[0]
        return None if int(key, 16) == 0 else to_checksum_address(key)

    def is_bound(self, key: str, account: str, trader: str) -> bool:
        return self._call(
            self.registry, "isBound(address,address,address)", ["address", "address", "address"],
            [key, account, trader], ["bool"],
        )[0]

    def allowed_assets(self, account: str) -> set[int]:
        rules = self._call(account, "rules()", [], [], ["(uint16,uint16,uint32,uint32[])"])[0]
        return set(rules[3])
