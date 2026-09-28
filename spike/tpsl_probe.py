"""How Hyperliquid's testnet treats the protective orders the gateway is about to place: reduce-only
trigger orders (stop market and take market), with grouping "na" and "positionTpsl", with and without
an open position, and what is left of them after the position closes.

    spike/.venv/bin/python -m spike.tpsl_probe --wallet shared-operator

Uses one of our own testnet wallets directly (no pool, no gateway): a position of about 11 USDC in
ETH, opened and closed within a minute. Every answer goes to spike/results/<date>.jsonl.
"""

from __future__ import annotations

import argparse
import math
import time

from spike.hlspike import common as c

COIN = "ETH"
SIZE = 0.0042  # about 11 USDC at 2600, over Hyperliquid's 10 USDC minimum


def px(x: float) -> float:
    """Five significant figures, the way Hyperliquid wants a perp price."""
    digits = 5 - int(math.floor(math.log10(abs(x)))) - 1
    return round(x, max(digits, 0))


def mid() -> float:
    return float(c.info_post({"type": "allMids"})[COIN])


def orders(user: str) -> list:
    return c.info_post({"type": "frontendOpenOrders", "user": user})


def position(user: str) -> dict | None:
    state = c.info_post({"type": "clearinghouseState", "user": user})
    for p in state.get("assetPositions", []):
        if p["position"]["coin"] == COIN:
            return p["position"]
    return None


def trigger(is_buy: bool, size: float, trigger_px: float, tpsl: str) -> dict:
    return {"coin": COIN, "is_buy": is_buy, "sz": size, "limit_px": trigger_px,
            "order_type": {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": tpsl}},
            "reduce_only": True}


def second(ex, user: str) -> None:
    """Position TP/SL placed with no position, a second stop over the first, and moving one."""
    m = mid()
    c.record("tpsl_E_position_stop_no_position", mid=m,
             answer=ex.bulk_orders([trigger(False, 0, px(m * 0.97), "sl")], grouping="positionTpsl"))
    c.record("tpsl_E_orders", orders=orders(user))

    m = mid()
    c.record("tpsl_open2", mid=m, answer=ex.order(COIN, True, SIZE, px(m * 1.01), {"limit": {"tif": "Ioc"}}))
    c.record("tpsl_E_orders_after_open", position=position(user), orders=orders(user))

    m = mid()
    first = trigger(False, 0, px(m * 0.97), "sl")
    first["limit_px"] = px(m * 0.97 * 0.9)  # a market stop's price: does the venue keep it?
    c.record("tpsl_F_stop1", answer=ex.bulk_orders([first], grouping="positionTpsl"))
    c.record("tpsl_G_stop2", answer=ex.bulk_orders([trigger(False, 0, px(m * 0.975), "sl")], grouping="positionTpsl"))
    now = orders(user)
    c.record("tpsl_FG_orders", orders=now)
    stops = [o for o in now if o["isPositionTpsl"] and o["orderType"] == "Stop Market"]
    if stops:
        oid = stops[0]["oid"]
        c.record("tpsl_H_modify_stop", oid=oid, answer=ex.modify_order(
            oid, COIN, False, 0, px(m * 0.98), {"trigger": {"triggerPx": px(m * 0.98), "isMarket": True, "tpsl": "sl"}},
            reduce_only=True))
        c.record("tpsl_H_orders", orders=orders(user))
    c.record("tpsl_I_take", answer=ex.bulk_orders([trigger(False, 0, px(m * 1.03), "tp")], grouping="positionTpsl"))
    c.record("tpsl_I_orders", orders=orders(user))

    time.sleep(1)
    m = mid()
    c.record("tpsl_close2", mid=m,
             answer=ex.order(COIN, False, SIZE, px(m * 0.99), {"limit": {"tif": "Ioc"}}, reduce_only=True))
    time.sleep(2)
    c.record("tpsl_after_close2_orders", position=position(user), orders=orders(user))
    for o in orders(user):
        c.record("tpsl_cleanup2_cancel", oid=o["oid"], answer=ex.cancel(COIN, o["oid"]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wallet", default="shared-operator")
    p.add_argument("--second", action="store_true", help="the second round: position TP/SL with no position, "
                   "a second stop, moving a stop")
    args = p.parse_args()
    acct = c.account(args.wallet)
    user = acct.address
    ex = c.exchange(acct)
    if args.second:
        second(ex, user)
        c.record("tpsl_probe2_end", orders=orders(user), snapshot=c.core_snapshot(user)["perpAccountValue"])
        return 0

    snap = c.core_snapshot(user)
    c.record("tpsl_probe_start", user=user, spot=snap["spotUSDC"], perp=snap["perpAccountValue"])
    if float(snap["perpAccountValue"]) < 0.45:
        amount = math.floor(float(snap["spotUSDC"]) * 100) / 100
        c.record("tpsl_to_perp", answer=ex.usd_class_transfer(amount, True), amount=amount)
    c.record("tpsl_leverage", answer=ex.update_leverage(25, COIN, is_cross=True))

    m = mid()
    # A: a reduce-only stop with no position at all.
    c.record("tpsl_A_na_stop_no_position", mid=m,
             answer=ex.bulk_orders([trigger(False, SIZE, px(m * 0.97), "sl")], grouping="na"))
    c.record("tpsl_A_orders", orders=orders(user))
    for o in orders(user):
        c.record("tpsl_A_cancel", answer=ex.cancel(COIN, o["oid"]))

    # Open the position.
    m = mid()
    c.record("tpsl_open", mid=m, answer=ex.order(COIN, True, SIZE, px(m * 1.01), {"limit": {"tif": "Ioc"}}))
    c.record("tpsl_position", position=position(user))

    # B: position TP/SL with size 0 (the whole position), stop and take in one action.
    m = mid()
    c.record("tpsl_B_position_tpsl_size0", mid=m, answer=ex.bulk_orders(
        [trigger(False, 0, px(m * 0.97), "sl"), trigger(False, 0, px(m * 1.03), "tp")], grouping="positionTpsl"))
    # C: a fixed-size reduce-only stop, grouping "na", next to it.
    c.record("tpsl_C_na_stop_sized", answer=ex.bulk_orders([trigger(False, SIZE, px(m * 0.96), "sl")], grouping="na"))
    c.record("tpsl_BC_orders", orders=orders(user))

    # Close the position with a reduce-only IOC, the way a trader or a stop would.
    time.sleep(2)
    m = mid()
    c.record("tpsl_close", mid=m,
             answer=ex.order(COIN, False, SIZE, px(m * 0.99), {"limit": {"tif": "Ioc"}}, reduce_only=True))
    time.sleep(2)
    c.record("tpsl_after_close_orders", position=position(user), orders=orders(user))
    for o in orders(user):
        c.record("tpsl_cleanup_cancel", oid=o["oid"], answer=ex.cancel(COIN, o["oid"]))
    c.record("tpsl_probe_end", orders=orders(user), snapshot=c.core_snapshot(user)["perpAccountValue"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
