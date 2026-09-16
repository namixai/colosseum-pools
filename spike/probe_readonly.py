"""Read-only facts the spike depends on. Sends nothing.

    spike/.venv/bin/python spike/probe_readonly.py

Prints one JSON line per fact and appends it to spike/results/<date>.jsonl.
"""

from __future__ import annotations

import os
import sys

from eth_utils import to_checksum_address

from hlspike import common as c


def main() -> int:
    c.assert_testnet()

    head = c.rpc("eth_getBlockByNumber", ["latest", False])
    c.record(
        "rpc",
        chain_id=int(c.rpc("eth_chainId"), 16),
        block=int(head["number"], 16),
        block_gas_limit=int(head["gasLimit"], 16),
        base_fee_wei=int(head.get("baseFeePerGas", "0x0"), 16),
    )

    code = c.rpc("eth_getCode", [c.CORE_WRITER, "latest"])
    c.record("corewriter_code", address=c.CORE_WRITER, code_bytes=(len(code) - 2) // 2)

    usdc_symbol = c.call_view(c.TESTNET_USDC_ERC20, "symbol()", [], [], ["string"])[0]
    usdc_decimals = c.call_view(c.TESTNET_USDC_ERC20, "decimals()", [], [], ["uint8"])[0]
    c.record("usdc_erc20", address=c.TESTNET_USDC_ERC20, symbol=usdc_symbol, decimals=usdc_decimals)

    fee = c.call_view(c.TESTNET_CORE_DEPOSIT_WALLET, "newCoreAccountFee()", [], [], ["uint256"])[0]
    c.record("core_deposit_wallet", address=c.TESTNET_CORE_DEPOSIT_WALLET, new_core_account_fee=fee)

    meta = c.info_post({"type": "meta"})
    perps = {a["name"]: (i, a["szDecimals"], a["maxLeverage"]) for i, a in enumerate(meta["universe"])}
    c.record("perp_assets", **{k: perps[k] for k in ("BTC", "ETH", "SOL", "HYPE") if k in perps})

    spot = c.info_post({"type": "spotMeta"})
    by_index = {t["index"]: t for t in spot["tokens"]}
    hype_pair = next(
        p for p in spot["universe"] if [by_index[x]["name"] for x in p["tokens"]] == ["HYPE", "USDC"]
    )
    c.record(
        "spot_hype_usdc",
        pair=hype_pair["name"],
        spot_index=hype_pair["index"],
        asset_id=10000 + hype_pair["index"],
        hype_token=next(t["index"] for t in spot["tokens"] if t["name"] == "HYPE"),
    )

    # Our own addresses: expected to be unknown to HyperCore until funded.
    for name in ("deployer", "trader"):
        addr = c.address_of(name)
        c.record(
            "our_address",
            name=name,
            address=addr,
            core_user_exists=c.core_user_exists(addr),
            evm_hype_wei=c.evm_balance(addr),
            evm_usdc=c.erc20_balance(c.TESTNET_USDC_ERC20, addr),
            spot_usdc=c.core_spot_balance(addr, c.USDC_TOKEN)["total"],
        )

    # Controls for the precompile reader: the HYPE system address must exist, a freshly
    # generated address must not. A vanity "dead" address is no control: 0x…dEaD already
    # exists on testnet HyperCore (someone sent it funds), which also means a stop that
    # "replaces the agent with a dead address" must use a fresh address, not a famous one.
    fresh = to_checksum_address("0x" + os.urandom(20).hex())
    c.record(
        "core_user_exists_controls",
        hype_system=c.core_user_exists(c.HYPE_SYSTEM),
        fresh_random=c.core_user_exists(fresh),
        vanity_dead=c.core_user_exists("0x000000000000000000000000000000000000dEaD"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
