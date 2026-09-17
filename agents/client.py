"""A trader's client for the pool gateway: signs one order or one cancel with the trader's
wallet and posts it. Used by the demo bot, by `agents.ai_trader`, and from the command line by
the Claude Code window that trades the demo:

    spike/.venv/bin/python -m agents.client --deployment demo pools
    spike/.venv/bin/python -m agents.client --deployment demo buy 0xPOOL 25
    spike/.venv/bin/python -m agents.client --deployment demo account 0xACCOUNT
    spike/.venv/bin/python -m agents.client --deployment demo market 0xACCOUNT BTC
    spike/.venv/bin/python -m agents.client --deployment demo order 0xACCOUNT BTC buy 0.0005 76000 --type limit
    spike/.venv/bin/python -m agents.client --deployment demo cancel 0xACCOUNT BTC 123456
    spike/.venv/bin/python -m agents.client --deployment demo close 0xACCOUNT BTC
    spike/.venv/bin/python -m agents.client --deployment demo graduate 0xACCOUNT

Every command prints one JSON object. The limits are those of `agents.desk`, counted per
wallet, per account and per UTC day in agents/state/, and set in this file: no option raises
them. The wallet's key is read from COLOSSEUM_KEY_DIR and never printed. --dry-run sends
nothing and needs no key. Testnet only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
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


# ── the command line ─────────────────────────────────────────────────────────────────────

STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"
# In code on purpose: an option would let whoever runs the command raise them.
WINDOW_MAX_ORDER_USDC = 100.0
WINDOW_MAX_ORDERS_PER_DAY = 4
WINDOW_MAX_PRICE_USDC = 40.0  # a challenge's price plus the platform fee
DESK_COUNTERS = ("orders_left", "cancels_left", "graduations_left")
SHOP_COUNTERS = ("purchases_left",)


def session_path(wallet: str, subject: str, now: float) -> pathlib.Path:
    """One counter file per wallet, per account (or the shop) and per UTC day."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    return STATE_DIR / f"{wallet.lower()}-{subject.lower()}-{day}.json"


def load_counters(obj: Any, names: tuple[str, ...], path: pathlib.Path) -> None:
    if path.exists():
        saved = json.loads(path.read_text())
        for name in names:
            setattr(obj, name, min(int(saved[name]), getattr(obj, name)))


def save_counters(obj: Any, names: tuple[str, ...], path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({name: getattr(obj, name) for name in names}) + "\n")
    tmp.replace(path)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m agents.client", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/")
    p.add_argument("--wallet", default="ai-trader", help="key name in COLOSSEUM_KEY_DIR")
    p.add_argument("--gateway", default=os.environ.get("COLOSSEUM_GATEWAY_URL", "http://127.0.0.1:8787"))
    p.add_argument("--dry-run", action="store_true", help="send nothing, sign nothing")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("pools", help="pools that can sell a challenge now")
    buy = sub.add_parser("buy", help="buy a challenge (one per day)")
    buy.add_argument("pool")
    buy.add_argument("price", type=float, help="the price `pools` showed, in USDC")
    for name, what in (("account", "an account's equity, positions, orders and rules"),
                       ("graduate", "ask the challenge contract to pass the challenge")):
        sub.add_parser(name, help=what).add_argument("account")
    market = sub.add_parser("market", help="market data for a perp on the account's list")
    market.add_argument("account")
    market.add_argument("coin")
    order = sub.add_parser("order", help="send one order through the gateway")
    order.add_argument("account")
    order.add_argument("coin")
    order.add_argument("side", choices=["buy", "sell"])
    order.add_argument("size", type=float)
    order.add_argument("price", type=float)
    order.add_argument("--type", dest="order_type", choices=["limit", "post_only", "ioc"], default="limit")
    order.add_argument("--reduce-only", action="store_true")
    cancel = sub.add_parser("cancel", help="cancel one open order")
    cancel.add_argument("account")
    cancel.add_argument("coin")
    cancel.add_argument("oid", type=int)
    close = sub.add_parser("close", help="close a whole position with a reduce-only order")
    close.add_argument("account")
    close.add_argument("coin")
    return p


def run(args: argparse.Namespace, chain, reader_for, gateway_for) -> Any:
    """One command against a chain module, a registry reader factory and a gateway client
    factory (the tests pass fakes)."""
    from . import desk

    from ops import deployments

    factory, registry = deployments.resolve(args.deployment)
    send = not args.dry_run
    who = chain.address_of(args.wallet)  # the address only; the key is loaded to send
    acts = args.command in ("buy", "order", "cancel", "close", "graduate")
    wallet = chain.account(args.wallet) if send and acts else None
    now = time.time()

    if args.command in ("pools", "buy"):
        shop = desk.Shop(factory, WINDOW_MAX_PRICE_USDC, wallet, send)
        if args.command == "pools":
            return shop.listing()
        path = session_path(who, "shop", now)
        load_counters(shop, SHOP_COUNTERS, path)
        shop.listing()
        try:
            return shop.buy(args.pool, args.price)
        finally:
            if send:
                save_counters(shop, SHOP_COUNTERS, path)

    trading = args.command in ("order", "cancel", "close")
    if trading and send:
        reader = reader_for(factory, registry)
        key = reader.trading_key(args.account)
        if key is None:
            raise desk.Refused("the account isn't trading right now")
        if not reader.is_bound(key, args.account, wallet.address):
            raise desk.Refused("this wallet is not the account's trader")
    limits = desk.Limits(max_notional=WINDOW_MAX_ORDER_USDC, max_orders=WINDOW_MAX_ORDERS_PER_DAY)
    client = gateway_for(wallet, args.gateway) if trading and send else None
    d = desk.Desk(factory, args.account, limits, client, wallet, send)
    path = session_path(who, d.account, now)
    load_counters(d, DESK_COUNTERS, path)
    try:
        if args.command == "account":
            return d.account_view()
        if args.command == "market":
            return d.market_view(args.coin)
        if args.command == "order":
            return d.place_order(args.coin, args.side, args.size, args.price, args.order_type, args.reduce_only)
        if args.command == "cancel":
            return d.cancel_order(args.coin, args.oid)
        if args.command == "close":
            return d.close_position(args.coin)
        return d.graduate()
    finally:
        if send and acts:
            save_counters(d, DESK_COUNTERS, path)


def main(argv: list[str] | None = None) -> int:
    from gateway.chain import JsonRpcReader
    from spike.hlspike import common as chain

    from .desk import Refused

    args = parser().parse_args(argv)
    chain.assert_testnet()
    try:
        result = run(args, chain, lambda f, r: JsonRpcReader(chain.RPC_URL, f, r),
                     lambda w, url: GatewayClient(w, url))
    except Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    print(json.dumps({"ok": True, "result": result}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
