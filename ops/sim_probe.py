"""Reads, and only reads, what a simulated pool on a network depends on: for each platform asset, the perp asset
info, oracle price and best bid and offer precompiles at its index (ops/sim_network.py), held against the info
API's own meta and oracle price; and the network's USDC (name, version, decimals, and the EIP-3009 read the
relayer uses). No key, no transaction.

    spike/.venv/bin/python -m ops.sim_probe --network mainnet [--out spike/results/sim-probe-mainnet.json]

It talks to the public endpoints of the network it is given (spike/hlspike/network.py), whatever
COLOSSEUM_NETWORK says: reading the other network is harmless, and it lets testnet's host look at mainnet.
COLOSSEUM_RPC_URL is not read here. Exit status 0 when every check holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

import eth_abi
import requests
from eth_utils import keccak

from ops import sim_network as sn
from spike.hlspike import network as networks

USER_AGENT = "colosseum-pools-probe"
# How far the oracle precompile may stand from the info API's oracle price read a moment apart, in bps.
ORACLE_TOLERANCE_BPS = 50
# A probe address for authorizationState: any address answers false for a nonce never used.
PROBE_ADDR = "0x" + "00" * 19 + "01"


def selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


class Reader:
    def __init__(self, net: networks.Network, session=None):
        self.net = net
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Content-Type": "application/json"})

    def rpc(self, method: str, params: list) -> object:
        body = self.s.post(self.net.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                           timeout=30).json()
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]

    def call(self, to: str, data: bytes, block: str) -> bytes:
        return bytes.fromhex(self.rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])[2:])

    def info(self, body: dict) -> object:
        return self.s.post(self.net.api_url + "/info", json=body, timeout=30).json()


def px_from_precompile(raw: int, sz_decimals: int) -> float:
    """A perp's price as the precompiles give it: an integer with 6 - szDecimals decimals."""
    return raw / 10 ** (6 - sz_decimals)


def probe(r: Reader) -> dict:
    net = r.net
    table = sn.sim(net)
    out: dict = {"network": net.name, "rpc": net.rpc_url, "api": net.api_url, "checks": [], "assets": {}}

    def check(name: str, ok: bool, detail: str) -> None:
        out["checks"].append({"check": name, "ok": bool(ok), "detail": detail})

    chain = int(r.rpc("eth_chainId", []), 16)
    check("chain id", chain == net.chain_id, f"{chain}, expected {net.chain_id}")
    block = r.rpc("eth_getBlockByNumber", ["latest", False])
    tag = block["number"]
    out["block"] = int(tag, 16)
    out["block_time_utc"] = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc).isoformat()
    meta, ctxs = r.info({"type": "metaAndAssetCtxs"})
    out["read_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    universe = meta["universe"]
    for name, idx in table.assets.items():
        a: dict = {"index": idx}
        api = universe[idx] if idx < len(universe) else {}
        a["meta_name"] = api.get("name")
        check(f"{name}: meta index {idx}", api.get("name") == name and not api.get("isDelisted"),
              f"meta says {api.get('name')}{' (delisted)' if api.get('isDelisted') else ''}")
        word = eth_abi.encode(["uint32"], [idx])
        coin, _, sz_dec, max_lev, _ = eth_abi.decode(["(string,uint32,uint8,uint8,bool)"],
                                                    r.call(sn.PERP_ASSET_INFO, word, tag))[0]
        a.update(precompile_coin=coin, sz_decimals=sz_dec, max_leverage=max_lev)
        check(f"{name}: perpAssetInfo 0x…080a", coin == name, f"precompile says {coin}")
        oracle_raw = eth_abi.decode(["uint64"], r.call(sn.ORACLE_PX, word, tag))[0]
        oracle = px_from_precompile(oracle_raw, sz_dec)
        ctx = ctxs[idx] if idx < len(ctxs) and isinstance(ctxs[idx], dict) else {}
        api_oracle = float(ctx.get("oraclePx") or 0)
        gap = abs(oracle - api_oracle) / api_oracle * 10_000 if api_oracle else float("inf")
        # No API price leaves the gap undefined: None, which JSON can carry, and the check fails (review of #300).
        a.update(oracle_raw=oracle_raw, oracle_px=oracle, api_oracle_px=api_oracle,
                 gap_bps=round(gap, 2) if api_oracle else None)
        check(f"{name}: oraclePx 0x…0807", oracle_raw > 0 and gap <= ORACLE_TOLERANCE_BPS,
              f"{oracle:g} against the API's {api_oracle:g}, {gap:.2f} bp apart")
        bid_raw, ask_raw = eth_abi.decode(["uint64", "uint64"], r.call(sn.BBO, eth_abi.encode(["uint64"], [idx]), tag))
        bid, ask = px_from_precompile(bid_raw, sz_dec), px_from_precompile(ask_raw, sz_dec)
        a.update(bid=bid, ask=ask, spread_bps=round((ask - bid) / oracle * 10_000, 2) if oracle else None,
                 bid_off_oracle_bps=round((oracle - bid) / oracle * 10_000, 2) if oracle else None,
                 ask_off_oracle_bps=round((ask - oracle) / oracle * 10_000, 2) if oracle else None)
        check(f"{name}: bbo 0x…080e", 0 < bid_raw <= ask_raw, f"bid {bid:g}, ask {ask:g}")
        out["assets"][name] = a
    usdc: dict = {"address": net.usdc_erc20}
    for sig, typ in (("name()", "string"), ("symbol()", "string"), ("version()", "string"), ("decimals()", "uint8")):
        try:
            usdc[sig[:-2]] = eth_abi.decode([typ], r.call(net.usdc_erc20, selector(sig), tag))[0]
        except Exception as exc:  # a token without the read says so in the result
            usdc[sig[:-2]] = f"no answer: {str(exc)[:80]}"
    try:
        data = selector("authorizationState(address,bytes32)") + eth_abi.encode(["address", "bytes32"],
                                                                                 [PROBE_ADDR, b"\x00" * 32])
        usdc["authorizationState"] = eth_abi.decode(["bool"], r.call(net.usdc_erc20, data, tag))[0]
    except Exception as exc:
        usdc["authorizationState"] = f"no answer: {str(exc)[:80]}"
    check("USDC decimals 6", usdc.get("decimals") == 6, str(usdc.get("decimals")))
    check("USDC EIP-3009 authorizationState", usdc.get("authorizationState") is False,
          str(usdc.get("authorizationState")))
    out["usdc"] = usdc
    out["ok"] = all(x["ok"] for x in out["checks"])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--network", choices=sorted(networks.NETWORKS), default=networks.DEFAULT)
    p.add_argument("--out", help="also write the result here, as JSON")
    args = p.parse_args(argv)
    result = probe(Reader(networks.NETWORKS[args.network]))
    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.out:
        path = pathlib.Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
