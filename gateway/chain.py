"""Reads the gateway takes from HyperEVM testnet. Every answer comes from the chain at the
latest block; nothing is cached, so a stop lands in the gateway's view as soon as it is mined."""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

import eth_abi
import requests
from eth_utils import keccak, to_checksum_address

from .assets import precompile_index
from .protect import RuleLimits

CHAIN_ID = 998
# HyperEVM's perpAssetInfo precompile: (coin, marginTableId, szDecimals, maxLeverage, onlyIsolated).
PERP_ASSET_INFO = "0x000000000000000000000000000000000000080a"
USER_AGENT = "colosseum-pools-gateway"
RATE_LIMITED = -32005  # what the public HyperEVM RPC answers when it throttles
# An order is waiting on these reads, so a throttled one is retried after 0.25 s, 0.5 s, 1 s
# and 2 s. Three tries inside a second was not enough: on 24 Sep 2026 the node refused all
# three, the trader's order came back as a gateway error, and the very same reads went through
# seconds later. Four seconds of patience is cheap next to telling a visitor the demo is broken.
RPC_ATTEMPTS = 5
KEY_BOUND = 2  # KeyRegistry.State: 1 Free, 2 Bound, 3 Retired


class Throttled(RuntimeError):
    """The node refused every attempt because it is rate limiting this caller, not because the
    read was wrong. Worth telling apart: waiting helps, and the visitor can be told so."""

# ChallengeAccount.Status.Active and Pool.Stage.Funded
CHALLENGE_ACTIVE = 2
POOL_FUNDED = 2
# The Rules and Terms structs of src/Types.sol, as ABI tuples. gateway/tests hold them to the source.
# Rules' first four fields: v3 appends each asset's cap, and four read either deployment's rules.
RULES = "(uint16,uint16,uint32,uint32[])"
# A v3 account's rules: each asset's own leverage cap appended, in the order of its assets (CTO, 21 Oct 2026).
RULES_V3 = "(uint16,uint16,uint32,uint32[],uint32[])"


def decode_rules(raw: bytes, four: str = RULES, five: str = RULES_V3) -> tuple:
    """rules() as an account of either kind answers it, as five fields: an account before v3 has no
    cap of its own for an asset, and gives `()` for them. The answer is one tuple with arrays in it,
    so it opens with the tuple's offset, and the tuple's fourth word is where `assets` starts inside
    it: 0x80 after a head of four words, 0xa0 after five. That word says which, so neither is
    decoded as the other (AUDIT В6-2, 21 Oct 2026)."""
    at = int.from_bytes(raw[0:32], "big")
    assets_at = int.from_bytes(raw[at + 96:at + 128], "big")
    if assets_at == 0xA0:
        return eth_abi.decode([five], raw)[0]
    if assets_at == 0x80:
        return (*eth_abi.decode([four], raw)[0], ())
    raise RuntimeError(f"rules() answered neither four fields nor five (assets at {assets_at:#x})")
TERMS = "(uint64,uint64,uint16,uint32,uint16,uint16,uint64)"


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

    def _call_raw(self, to: str, signature: str) -> bytes:
        raw = self._rpc("eth_call", [{"to": to, "data": "0x" + keccak(text=signature)[:4].hex()}, "latest"])
        return bytes.fromhex(raw[2:])

    def perp_name(self, asset: int) -> str | None:
        """The coin HyperEVM's perpAssetInfo names for an order's asset, asked with the precompile's
        own number (gateway/assets.py), or None when HyperEVM knows no such perp. A number off by
        100000 does not fail there: it reads another dex's perp (testnet, 21 Oct 2026: 750000 is
        rbbt:RAB to the precompile, xyz:XYZ100 is 650000), which is why the name is checked."""
        data = "0x" + eth_abi.encode(["uint32"], [precompile_index(asset)]).hex()
        try:
            raw = self._rpc("eth_call", [{"to": PERP_ASSET_INFO, "data": data}, "latest"])
        except RuntimeError as exc:
            if isinstance(exc, Throttled) or "PrecompileError" not in str(exc):
                raise
            return None
        return eth_abi.decode(["(string,uint32,uint8,uint8,bool)"], bytes.fromhex(raw[2:]))[0][0]

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

    def account_of(self, key: str) -> str | None:
        """The account this key is bound to in the registry right now, or None for a key that is
        free or retired. `bindingOf` answers (state, account, trader)."""
        state, account, _ = self._call(self.registry, "bindingOf(address)", ["address"], [key],
                                       ["(uint8,address,address)"])[0]
        return to_checksum_address(account) if state == KEY_BOUND else None

    def is_bound(self, key: str, account: str, trader: str) -> bool:
        return self._call(
            self.registry, "isBound(address,address,address)", ["address", "address", "address"],
            [key, account, trader], ["bool"],
        )[0]

    def pool_of(self, account: str) -> str:
        """The pool an account trades for: a pool's account is its own, a challenge's is its pool's."""
        if self._call(self.factory, "isChallenge(address)", ["address"], [account], ["bool"])[0]:
            return to_checksum_address(self._call(account, "pool()", [], [], ["address"])[0])
        return to_checksum_address(account)

    def allowed_assets(self, account: str) -> set[int]:
        rules = self._call(account, "rules()", [], [], [RULES])[0]
        return set(rules[3])

    # ── what the stop and the take are worked out from (gateway/protect.py) ───────────

    def day_snapshot(self, account: str) -> tuple[int, int]:
        """The UTC day of the latest daily snapshot and the equity it took (1e-6 USDC)."""
        day = self._call(account, "day()", [], [], ["uint32"])[0]
        start = self._call(account, "dayStartEquity()", [], [], ["int64"])[0]
        return day, start

    def rule_limits(self, account: str) -> RuleLimits:
        challenge = self._call(self.factory, "isChallenge(address)", ["address"], [account], ["bool"])[0]
        daily, drawdown, leverage, assets, caps = decode_rules(self._call_raw(account, "rules()"))
        base = self._call(account, "drawdownBase()", [], [], ["int64"])[0]
        day, start = self.day_snapshot(account)
        target = self._call(account, "terms()", [], [], [TERMS])[0][2]
        return RuleLimits(challenge, daily, drawdown, base, day, start, target, leverage, tuple(zip(assets, caps)))
