"""Reads for a deployment's plan on a network it does not send to: one JSON-RPC call to a public node,
Hyperliquid's info API, a perp's number from the meta, and the address CREATE gives a deployer's next
contracts. The plumbing (spike/hlspike/common.py) talks to the network COLOSSEUM_NETWORK names; these
read the one a script's --network names. No key, no transaction."""

from __future__ import annotations

import json
import urllib.request

from eth_utils import keccak, to_checksum_address


def read_rpc(url: str, method: str, params: list) -> object:
    """One JSON-RPC read from a public node, for the mainnet plan: the plumbing's own rpc() talks to
    the network COLOSSEUM_NETWORK names, and this reads the one --network names."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "deploy-plan"})
    with urllib.request.urlopen(req, timeout=20) as response:
        answer = json.load(response)
    if "error" in answer:
        raise RuntimeError(f"{method}: {answer['error']}")
    return answer["result"]


def read_info(api: str, body: dict) -> object:
    req = urllib.request.Request(api.rstrip("/") + "/info", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "deploy-plan"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def perp_numbers(api: str, names) -> dict:
    """Each named perp's number on the main dex of the network whose API this is, from its meta. A delisted perp
    keeps its place in the list, so the others keep their numbers, and is left out of the answer."""
    universe = read_info(api, {"type": "meta"})["universe"]
    live = {a["name"]: i for i, a in enumerate(universe) if not a.get("isDelisted")}
    return {name: live[name] for name in names if name in live}


def created_by(sender: str, nonce: int) -> str:
    """The address CREATE gives the `nonce`-th contract `sender` makes."""
    if nonce == 0:
        encoded = b"\x80"
    elif nonce < 0x80:
        encoded = bytes([nonce])
    else:
        raw = nonce.to_bytes((nonce.bit_length() + 7) // 8, "big")
        encoded = bytes([0x80 + len(raw)]) + raw
    payload = b"\x94" + bytes.fromhex(sender.removeprefix("0x")) + encoded
    return to_checksum_address(keccak(bytes([0xC0 + len(payload)]) + payload)[12:])
