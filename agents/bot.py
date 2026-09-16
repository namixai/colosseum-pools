"""Scripted demo trader. Each mode shows one thing on camera, through the pool gateway, with
the trader's own testnet wallet.

    spike/.venv/bin/python -m agents.bot rest      --deployment demo --account 0x…
    spike/.venv/bin/python -m agents.bot roundtrip --deployment demo --account 0x… --notional 20
    spike/.venv/bin/python -m agents.bot over-cap  --deployment demo --account 0x… --asset 3 --notional 900
    spike/.venv/bin/python -m agents.bot leverage  --deployment demo --account 0x… --notional 300 --orders 2
    spike/.venv/bin/python -m agents.bot outside   --deployment demo --account 0x…

over-cap asks for more than the enclave's per-order cap, so the enclave refuses it: pick a
--notional above the cap, for example 900 USDC of BTC against a 0.01 BTC cap. outside picks a
perp that is not on the account's list (or the one given with --asset), so the gateway
refuses it. leverage stacks orders until the account breaks its leverage rule.

The wallet is the testnet key named by --wallet (default "trader"), read from outside the
repository and never printed. Every response is printed as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from gateway.chain import JsonRpcReader
from ops import deployments
from spike.hlspike import common as c

from .client import GatewayClient, round_price, round_size

# Hyperliquid refuses orders under 10 USDC of notional; the margin covers rounding the size down.
MIN_NOTIONAL = 12.0


def show(label: str, payload) -> None:
    print(json.dumps({"step": label, **(payload if isinstance(payload, dict) else {"value": payload})},
                     default=str), flush=True)


def first_asset(reader: JsonRpcReader, account: str) -> int:
    assets = sorted(reader.allowed_assets(account))
    if not assets:
        raise SystemExit("the account allows no assets")
    return assets[0]


def asset_outside(reader: JsonRpcReader, account: str) -> int:
    allowed = reader.allowed_assets(account)
    mids = c.info_post({"type": "allMids"})
    for i, a in enumerate(c.info_post({"type": "meta"})["universe"]):
        if i not in allowed and not a.get("isDelisted") and a["name"] in mids:
            return i
    raise SystemExit("every listed perp is on the account's list")


def market(asset: int) -> tuple[str, int, float]:
    meta = c.info_post({"type": "meta"})["universe"][asset]
    mid = float(c.info_post({"type": "allMids"})[meta["name"]])
    return meta["name"], meta["szDecimals"], mid


def sized(notional: float, px: float, szd: int) -> str:
    try:
        return round_size(notional / px, szd)
    except ValueError:
        raise SystemExit(f"{notional} USDC is less than one size step at {px}") from None


def open_oids(account: str, coin: str) -> list[int]:
    return [o["oid"] for o in c.info_post({"type": "openOrders", "user": account}) if o["coin"] == coin]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["rest", "roundtrip", "over-cap", "leverage", "outside"])
    p.add_argument("--account", required=True)
    p.add_argument("--gateway", default="http://127.0.0.1:8787")
    p.add_argument("--wallet", default="trader")
    p.add_argument("--deployment", help="label of a record in deployments/")
    p.add_argument("--factory", help="PoolFactory address, instead of --deployment")
    p.add_argument("--registry", help="KeyRegistry address, instead of --deployment")
    p.add_argument("--asset", type=int)
    p.add_argument("--notional", type=float, default=20.0)
    p.add_argument("--orders", type=int, default=1)
    args = p.parse_args()

    c.assert_testnet()
    factory, registry = deployments.resolve(args.deployment, args.factory, args.registry)
    reader = JsonRpcReader(c.RPC_URL, factory, registry)
    client = GatewayClient(c.account(args.wallet), args.gateway)
    if args.asset is not None:
        asset = args.asset
    elif args.mode == "outside":
        asset = asset_outside(reader, args.account)
    else:
        asset = first_asset(reader, args.account)
    coin, szd, mid = market(asset)
    show("market", {"asset": asset, "coin": coin, "mid": mid})

    floor = max(args.notional, MIN_NOTIONAL)
    if args.mode == "rest":
        px = round_price(mid * 0.7, szd)
        size = sized(floor, float(px), szd)
        show("rest", client.order(args.account, asset, True, px, size, tif="Alo"))
        time.sleep(3)
        for oid in open_oids(args.account, coin):
            show("cancel", client.cancel(args.account, asset, oid))

    elif args.mode == "roundtrip":
        size = sized(floor, mid, szd)
        show("buy", client.order(args.account, asset, True, round_price(mid * 1.01, szd), size, tif="Ioc"))
        time.sleep(3)
        show("sell", client.order(args.account, asset, False, round_price(mid * 0.99, szd), size,
                                  tif="Ioc", reduce_only=True))

    elif args.mode == "over-cap":
        size = sized(args.notional, mid, szd)
        show("over-cap", client.order(args.account, asset, True, round_price(mid * 0.7, szd), size, tif="Alo"))

    elif args.mode == "leverage":
        size = sized(args.notional, mid, szd)
        for i in range(args.orders):
            show(f"leg-{i + 1}", client.order(args.account, asset, True, round_price(mid * 1.01, szd), size, tif="Ioc"))
            time.sleep(2)
        show("state", c.info_post({"type": "clearinghouseState", "user": args.account})["marginSummary"])

    elif args.mode == "outside":
        size = sized(floor, mid, szd)
        show("outside", client.order(args.account, asset, True, round_price(mid * 0.7, szd), size, tif="Alo"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
