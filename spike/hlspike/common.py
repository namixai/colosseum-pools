"""Shared plumbing for the live testnet spike.

Everything here refuses to talk to mainnet. Keys are read from a directory outside the
repository (COLOSSEUM_KEY_DIR, default ~/secure/colosseum-testnet), one hex key per
`<name>.key` file next to a `<name>.addr` file, and they are never printed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

import eth_abi
import requests
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak, to_checksum_address
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

RPC_URL = "https://rpc.hyperliquid-testnet.xyz/evm"
API_URL = constants.TESTNET_API_URL
CHAIN_ID = 998
USER_AGENT = "colosseum-pools-spike"

CORE_WRITER = "0x3333333333333333333333333333333333333333"
HYPE_SYSTEM = "0x2222222222222222222222222222222222222222"
USDC_SYSTEM = "0x2000000000000000000000000000000000000000"
TESTNET_USDC_ERC20 = "0x2B3370eE501B4a559b57D449569354196457D8Ab"
TESTNET_CORE_DEPOSIT_WALLET = "0x0B80659a4076E9E93C7DbE0f10675A16a3e5C206"
USDC_TOKEN = 0
HYPE_TOKEN = 1105

PRECOMPILE = {
    "spotBalance": "0x0000000000000000000000000000000000000801",
    "withdrawable": "0x0000000000000000000000000000000000000803",
    "markPx": "0x0000000000000000000000000000000000000806",
    "l1BlockNumber": "0x0000000000000000000000000000000000000809",
    "accountMarginSummary": "0x000000000000000000000000000000000000080F",
    "coreUserExists": "0x0000000000000000000000000000000000000810",
    "position2": "0x0000000000000000000000000000000000000813",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "spike" / "results"
KEY_DIR = Path(os.environ.get("COLOSSEUM_KEY_DIR", "~/secure/colosseum-testnet")).expanduser()

if API_URL == constants.MAINNET_API_URL or "testnet" not in API_URL:
    raise SystemExit("refusing to run: API URL is not the Hyperliquid testnet")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Content-Type": "application/json"})


# ── keys ─────────────────────────────────────────────────────────────────────────────

def account(name: str) -> LocalAccount:
    """Load a testnet key by name and check it against the recorded address."""
    key_path = KEY_DIR / f"{name}.key"
    addr_path = KEY_DIR / f"{name}.addr"
    if name.startswith("burned-"):
        raise SystemExit(f"{name} is burned and must not be used")
    mode = key_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"{key_path} is readable by others (mode {oct(mode)}); fix permissions first")
    acct = Account.from_key(bytes.fromhex(key_path.read_text().strip().removeprefix("0x")))
    recorded = addr_path.read_text().strip()
    if acct.address.lower() != recorded.lower():
        raise SystemExit(f"{name}: key does not match {addr_path.name}")
    return acct


def address_of(name: str) -> str:
    return to_checksum_address((KEY_DIR / f"{name}.addr").read_text().strip())


# ── results log (public data only: addresses, hashes, responses) ─────────────────────

def record(step: str, **fields: Any) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    row = {"utc": now.isoformat(timespec="seconds"), "step": step, **fields}
    with open(RESULTS_DIR / f"{now.date().isoformat()}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    print(json.dumps(row, sort_keys=True, default=str))
    return row


# ── HyperEVM JSON-RPC ────────────────────────────────────────────────────────────────

def rpc(method: str, params: Sequence[Any] = ()) -> Any:
    resp = _session.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


def assert_testnet() -> None:
    chain = int(rpc("eth_chainId"), 16)
    if chain != CHAIN_ID:
        raise SystemExit(f"refusing to run: RPC chain id is {chain}, expected {CHAIN_ID}")


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, types: Sequence[str] = (), args: Sequence[Any] = ()) -> bytes:
    return selector(signature) + eth_abi.encode(list(types), list(args))


def eth_call(to: str, data: bytes, block: str = "latest") -> bytes:
    return bytes.fromhex(rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])[2:])


def call_view(to: str, signature: str, types: Sequence[str], args: Sequence[Any], out: Sequence[str]) -> tuple:
    return eth_abi.decode(list(out), eth_call(to, encode_call(signature, types, args)))


def precompile(name: str, types: Sequence[str], args: Sequence[Any], out: Sequence[str]) -> tuple:
    raw = eth_call(PRECOMPILE[name], eth_abi.encode(list(types), list(args)))
    return eth_abi.decode(list(out), raw)


def core_user_exists(user: str) -> bool:
    return precompile("coreUserExists", ["address"], [user], ["bool"])[0]


def core_spot_balance(user: str, token: int) -> dict:
    total, hold, entry = precompile("spotBalance", ["address", "uint64"], [user, token], ["uint64", "uint64", "uint64"])
    return {"total": total, "hold": hold, "entryNtl": entry}


def core_margin_summary(user: str, dex: int = 0) -> dict:
    av, mu, ntl, raw = precompile(
        "accountMarginSummary", ["uint32", "address"], [dex, user], ["int64", "uint64", "uint64", "int64"]
    )
    return {"accountValue": av, "marginUsed": mu, "ntlPos": ntl, "rawUsd": raw}


def evm_balance(addr: str) -> int:
    return int(rpc("eth_getBalance", [addr, "latest"]), 16)


def erc20_balance(token: str, addr: str) -> int:
    return call_view(token, "balanceOf(address)", ["address"], [addr], ["uint256"])[0]


def wait_receipt(tx_hash: str, timeout_s: int = 90) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rcpt = rpc("eth_getTransactionReceipt", [tx_hash])
        if rcpt:
            return rcpt
        time.sleep(1)
    raise TimeoutError(f"no receipt for {tx_hash} after {timeout_s}s")


def send_tx(acct: LocalAccount, to: str | None, data: bytes = b"", value: int = 0, gas: int | None = None) -> dict:
    """Sign and send one transaction on HyperEVM testnet, then wait for its receipt."""
    assert_testnet()
    nonce = int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
    base_fee = int(rpc("eth_gasPrice"), 16)
    tx: dict[str, Any] = {
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "value": value,
        "data": data,
        "type": 2,
        "maxPriorityFeePerGas": 0,
        "maxFeePerGas": base_fee * 2,
    }
    if to is not None:
        tx["to"] = to_checksum_address(to)
    probe = {"from": acct.address, "value": hex(value), "data": "0x" + data.hex()}
    if to is not None:
        probe["to"] = tx["to"]
    tx["gas"] = gas if gas is not None else int(int(rpc("eth_estimateGas", [probe]), 16) * 1.25)
    signed = acct.sign_transaction(tx)
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
    rcpt = wait_receipt(tx_hash)
    if int(rcpt["status"], 16) != 1:
        raise RuntimeError(f"transaction {tx_hash} reverted")
    return rcpt


def artifact(contract: str) -> dict:
    path = REPO_ROOT / "out" / f"{contract}.sol" / f"{contract}.json"
    if not path.exists():
        raise SystemExit(f"missing {path}; run `forge build` first")
    return json.loads(path.read_text())


def deploy(acct: LocalAccount, contract: str, ctor_types: Sequence[str], ctor_args: Sequence[Any]) -> tuple[str, dict]:
    art = artifact(contract)
    code = bytes.fromhex(art["bytecode"]["object"].removeprefix("0x"))
    rcpt = send_tx(acct, None, code + eth_abi.encode(list(ctor_types), list(ctor_args)))
    return to_checksum_address(rcpt["contractAddress"]), rcpt


def transact(acct: LocalAccount, to: str, signature: str, types: Sequence[str] = (), args: Sequence[Any] = ()) -> dict:
    return send_tx(acct, to, encode_call(signature, types, args))


def wait_blocks(n: int = 2, poll_s: float = 1.0) -> int:
    """HyperCore executes CoreWriter actions a few seconds after the EVM block, and
    precompiles read the start of the block. Give both time before reading."""
    start = int(rpc("eth_blockNumber"), 16)
    while True:
        cur = int(rpc("eth_blockNumber"), 16)
        if cur >= start + n:
            return cur
        time.sleep(poll_s)


# ── Hyperliquid API ──────────────────────────────────────────────────────────────────

def info() -> Info:
    return Info(API_URL, skip_ws=True)


def exchange(acct: LocalAccount, account_address: str | None = None) -> Exchange:
    return Exchange(acct, API_URL, account_address=account_address)


def info_post(body: dict) -> Any:
    resp = _session.post(f"{API_URL}/info", json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def core_snapshot(user: str) -> dict:
    """What the info API says about an account: spot USDC, perp value, open orders, role."""
    spot = info_post({"type": "spotClearinghouseState", "user": user})
    perp = info_post({"type": "clearinghouseState", "user": user})
    usdc = next((b for b in spot.get("balances", []) if b.get("coin") == "USDC"), None)
    hype = next((b for b in spot.get("balances", []) if b.get("coin") == "HYPE"), None)
    return {
        "user": user,
        "spotUSDC": usdc["total"] if usdc else "0",
        "spotHYPE": hype["total"] if hype else "0",
        "perpAccountValue": perp["marginSummary"]["accountValue"],
        "withdrawable": perp.get("withdrawable"),
        "openOrders": info_post({"type": "openOrders", "user": user}),
        "role": info_post({"type": "userRole", "user": user}),
    }


def spot_token_wire(name: str) -> str:
    """spotSend wants `NAME:tokenId`."""
    meta = info_post({"type": "spotMeta"})
    for tok in meta["tokens"]:
        if tok["name"] == name:
            return f"{tok['name']}:{tok['tokenId']}"
    raise KeyError(name)
