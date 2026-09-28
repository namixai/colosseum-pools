"""The live check of the gateway's stop and take (docs/GATEWAY.md, "The stop and the take on
Hyperliquid"), on testnet, with our own deployment `shared-run` and its own published agent keys;
nothing of the first window's is touched.

    spike/.venv/bin/python -m spike.stop_take_live setup                # a pool, its capital, a challenge, started
    spike/.venv/bin/python -m spike.stop_take_live trade --keys <dir>   # the whole check, below
    spike/.venv/bin/python -m spike.stop_take_live cleanup              # forfeit, settle, and the capital back

`trade` runs this branch's gateway in this process, on 127.0.0.1, with the demo signer holding the
agent keys found in <dir> (an owner-only directory of `*.key` files: shared-run's published keys),
and talks to it over HTTP the way a trader's client does:

  1. a position through the gateway: the stop and the take go to Hyperliquid before the order;
  2. the account's open orders on Hyperliquid, and one pass of the keeper, sending nothing;
  3. what the trader may and may not do: cancel the stop (refused), a looser stop (refused), a
     tighter stop, a take past the target (refused), a nearer take;
  4. a sweep with everything in place, which sends nothing;
  5. the position closed with a reduce-only order, and what is left on the book after it.

The pool: a 3 USDC challenge, 3 % a day, 6 % drawdown, 5x, an 8 % target, on SOL, BTC and ETH. The
position: 0.0042 ETH, about 11 USDC, Hyperliquid's minimum. Every step goes to
spike/results/<date>.jsonl; the pool and the challenge to spike/results/state-stop-take.json.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import tempfile
import threading
import time
from decimal import Decimal

from agents.client import GatewayClient, round_price
from gateway import protect, server
from gateway.chain import JsonRpcReader
from gateway.demo_signer import DemoSigner
from ops import deployments
from spike.hlspike import common as c

DEPLOYMENT = "shared-run"
OPERATOR, TRADER, SPARE = "shared-operator", "shared-trader", "shared-dep-a"
RULES_T = "(uint16,uint16,uint32,uint32[])"
TERMS_T = "(uint64,uint64,uint16,uint32,uint16,uint16,uint64)"
RULES = (300, 600, 500, [0, 3, 4])
TERMS = (10_000, 3_000_000, 800, 86_400, 0, 8_000, 1)  # 0.01 USDC price, 3 USDC capital, funded 1e-6
ETH, SIZE = 4, "0.0042"
STATE = pathlib.Path(__file__).resolve().parent / "results" / "state-stop-take.json"
CANCEL = "(uint32,uint64)[]"


def view(to, sig, out, types=(), args=()):
    return c.call_view(to, sig, list(types), list(args), [out])[0]


def state() -> dict:
    return json.loads(STATE.read_text())


def orders(account: str) -> list:
    return c.info_post({"type": "frontendOpenOrders", "user": account})


def eth_position(account: str) -> str:
    for p in c.info_post({"type": "clearinghouseState", "user": account}).get("assetPositions", []):
        if p["position"]["coin"] == "ETH":
            return p["position"]["szi"]
    return "0"


def mid() -> float:
    return float(c.info_post({"type": "allMids"})["ETH"])


def wait_until(what: str, read, ok, seconds: int = 90):
    end = time.time() + seconds
    while time.time() < end:
        value = read()
        if ok(value):
            return value
        time.sleep(3)
    raise SystemExit(f"gave up waiting for {what}")


def cmd_setup(_args) -> None:
    factory = deployments.resolve(DEPLOYMENT)[0]
    op, tr, spare = c.account(OPERATOR), c.account(TRADER), c.account(SPARE)
    rcpt = c.transact(op, factory, f"createPool({RULES_T},{TERMS_T})", [RULES_T, TERMS_T], [RULES, TERMS])
    pool = view(factory, "pools()", "address[]")[-1]
    c.record("stop_take_pool_created", pool=pool, tx=rcpt["transactionHash"])
    needed = view(pool, "capitalNeeded()", "uint64")
    resp = c.exchange(op).spot_transfer(needed / 1e8, pool, c.spot_token_wire("USDC"))
    c.record("stop_take_pool_funded", pool=pool, usdc=needed / 1e8, response=resp)
    wait_until("the pool's capital", lambda: c.core_spot_balance(pool, c.USDC_TOKEN)["total"], lambda v: v >= needed)
    price, fee = TERMS[0], view(factory, "challengeFee()", "uint256")
    for who, amount in ((op, 300_000), (spare, 500_000)):
        rcpt = c.transact(who, c.TESTNET_USDC_ERC20, "transfer(address,uint256)", ["address", "uint256"],
                          [tr.address, amount])
        c.record("stop_take_trader_usdc", frm=who.address, usdc_1e6=amount, tx=rcpt["transactionHash"])
    c.transact(tr, c.TESTNET_USDC_ERC20, "approve(address,uint256)", ["address", "uint256"], [pool, price + fee])
    rcpt = c.transact(tr, pool, "buyChallenge()")
    ch = view(pool, "challenge()", "address")
    c.record("stop_take_challenge_bought", pool=pool, challenge=ch, key=view(ch, "agentKey()", "address"),
             tx=rcpt["transactionHash"])
    wait_until("the challenge's capital", lambda: c.core_spot_balance(ch, c.USDC_TOKEN)["total"],
               lambda v: v >= TERMS[1] * 100)
    rcpt = c.transact(op, ch, "activate()")
    c.record("stop_take_challenge_started", challenge=ch, tx=rcpt["transactionHash"],
             status=view(ch, "status()", "uint8"))
    STATE.write_text(json.dumps({"pool": pool, "challenge": ch}, indent=2) + "\n")


@contextlib.contextmanager
def gateway_here(keys_dir: str):
    """This branch's gateway, on 127.0.0.1 and a free port, with its sweep; every line it logs is
    kept and recorded when it stops."""
    factory, registry = deployments.resolve(DEPLOYMENT)
    reader = JsonRpcReader(c.RPC_URL, factory, registry)
    reader.check_chain()
    lines: list[dict] = []

    def keep(**fields):
        lines.append({"t": int(time.time()), **fields})

    server.log_line, protect.log_line = keep, keep
    gw = server.Gateway(reader, DemoSigner(DemoSigner.load_keys(keys_dir)))
    stop = threading.Event()
    threading.Thread(target=gw.protector.run, args=(server.PROTECT_EVERY_S, stop), daemon=True).start()
    http = server.BoundedServer(("127.0.0.1", 0), server.make_handler(gw))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}", gw
    finally:
        stop.set()
        http.shutdown()
        http.server_close()
        c.record("stop_take_gateway_log", lines=lines)


def client(url: str) -> GatewayClient:
    return GatewayClient(c.account(TRADER), url)


def book(ch: str, step: str) -> dict:
    row = c.record(step, challenge=ch, position=eth_position(ch), orders=orders(ch),
                   equity=c.info_post({"type": "clearinghouseState", "user": ch})["marginSummary"]["accountValue"])
    return row


def open_position(cl: GatewayClient, ch: str) -> None:
    px = round_price(mid() * 1.01, 4)
    answer = cl.order(ch, ETH, True, px, SIZE, tif="Ioc")
    c.record("stop_take_open", challenge=ch, limit_px=px, size=SIZE, answer=answer)
    time.sleep(2)
    book(ch, "stop_take_book_after_open")


def cmd_book(_args) -> None:
    book(state()["challenge"], "stop_take_book")


def cmd_trade(args) -> None:
    ch = state()["challenge"]
    with gateway_here(args.keys) as (url, gw):
        cl = client(url)
        open_position(cl, ch)
        cmd_keeper(args)
        refusals(cl, ch)
        watching = sorted(gw.protector.watching())
        gw.protector.sweep()
        c.record("stop_take_sweep_in_place", watching=watching, book=orders(ch))
        close_position(cl, ch)


def cmd_keeper(_args) -> None:
    """One pass of ops/keeper.py over this pool, sending nothing, from a state file that starts at
    the head and already follows the pool."""
    from ops import keeper

    s = state()
    factory = deployments.resolve(DEPLOYMENT)[0]
    latest = int(c.rpc("eth_blockNumber"), 16)
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "keeper.json"
        path.write_text(json.dumps({"factory": factory, "next_block": latest, "live": [s["pool"]]}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            keeper.Keeper(factory, None, True, path, latest).one_pass()
    c.record("stop_take_keeper_pass", lines=[json.loads(line) for line in out.getvalue().splitlines() if line])


def refusals(cl: GatewayClient, ch: str) -> None:
    now = orders(ch)
    stop = next(o for o in now if o["orderType"] == "Stop Market")
    take = next(o for o in now if o["orderType"] == "Take Profit Market")
    m = mid()
    c.record("stop_take_cancel_stop", answer=cl.cancel(ch, ETH, int(stop["oid"])))
    looser = round_price(float(stop["triggerPx"]) * 0.99, 4)
    c.record("stop_take_stop_looser", trigger=looser, answer=cl.stop(ch, ETH, looser))
    tighter = round_price(float(stop["triggerPx"]) + (m - float(stop["triggerPx"])) / 3, 4)
    c.record("stop_take_stop_tighter", trigger=tighter, answer=cl.stop(ch, ETH, tighter))
    beyond = round_price(float(take["triggerPx"]) * 1.01, 4)
    c.record("stop_take_take_beyond", trigger=beyond, answer=cl.take(ch, ETH, beyond))
    nearer = round_price(float(take["triggerPx"]) - (float(take["triggerPx"]) - m) / 3, 4)
    c.record("stop_take_take_nearer", trigger=nearer, answer=cl.take(ch, ETH, nearer))
    time.sleep(2)
    book(ch, "stop_take_book_after_moves")


def close_position(cl: GatewayClient, ch: str) -> None:
    size = eth_position(ch)
    if Decimal(size) <= 0:
        raise SystemExit(f"no long ETH position to close: {size}")
    px = round_price(mid() * 0.99, 4)
    answer = cl.order(ch, ETH, False, px, str(Decimal(size).normalize()), tif="Ioc", reduce_only=True)
    c.record("stop_take_close", challenge=ch, limit_px=px, size=size, answer=answer)
    time.sleep(3)
    book(ch, "stop_take_book_after_close")


def cmd_fills(_args) -> None:
    """Every fill on the challenge's account, with its time: the pair to set against the trigger."""
    ch = state()["challenge"]
    c.record("stop_take_fills", challenge=ch, fills=c.info_post({"type": "userFills", "user": ch}))


def cmd_cleanup(_args) -> None:
    s = state()
    pool, ch = s["pool"], s["challenge"]
    op, tr = c.account(OPERATOR), c.account(TRADER)
    if view(ch, "status()", "uint8") == 2:  # Active: the trader walks away
        rcpt = c.transact(tr, ch, f"forfeit({CANCEL},uint32[],bytes32)", [CANCEL, "uint32[]", "bytes32"],
                          [[], [], os.urandom(32)])
        c.record("stop_take_forfeit", challenge=ch, tx=rcpt["transactionHash"])
    for _ in range(10):
        if view(ch, "status()", "uint8") == 8:  # Settled
            break
        time.sleep(6)
        rcpt = c.transact(op, ch, f"settle({CANCEL},uint32[])", [CANCEL, "uint32[]"], [[], [ETH]])
        c.record("stop_take_settle_step", challenge=ch, tx=rcpt["transactionHash"])
    wait_until("the pool to be idle", lambda: view(pool, "stage()", "uint8"), lambda v: v == 0)
    spot = c.core_spot_balance(pool, c.USDC_TOKEN)["total"]
    rcpt = c.transact(op, pool, "withdrawOnCore(uint64)", ["uint64"], [spot])
    c.record("stop_take_withdrawn", pool=pool, usdc=spot / 1e8, tx=rcpt["transactionHash"])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["setup", "trade", "book", "keeper", "fills", "cleanup"])
    p.add_argument("--keys", help="owner-only directory of the agent keys the gateway under test holds")
    args = p.parse_args()
    c.assert_testnet()
    globals()[f"cmd_{args.step}"](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
