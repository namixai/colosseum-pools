"""The shared pool's testnet run, one step at a time: deposit, shares, a request, payment.

    spike/.venv/bin/python ops/shared_run.py --deployment shared-run status
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run wallets
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run start --seed 1
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run seat
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run arm
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run deposit --who shared-dep-a --usdc 20
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run settle
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run buy
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run request --who shared-dep-a
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run expire
    spike/.venv/bin/python ops/shared_run.py --deployment shared-run release

Testnet only (hlspike.common refuses anything else). Wallets are read by name from
COLOSSEUM_KEY_DIR and never printed: `shared-operator` runs the pool, `shared-dep-a` and
`shared-dep-b` deposit, `shared-trader` buys a challenge on the seat. Nobody trades in this run;
the challenge is there so that capital is at work while someone waits to be paid. Every step
reads the chain again before acting and records what it did and what it saw in
spike/results/<date>.jsonl.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from hlspike import common as c  # noqa: E402
from ops import deployments  # noqa: E402

DEPOSITORS = ("shared-dep-a", "shared-dep-b")
TRADER = "shared-trader"
USDC_1E8 = 100_000_000
USDC_1E6 = 1_000_000

# The run's one seat: challenge 2, funded 8, so capitalNeeded is 11 USDC; price 0.5 on HyperEVM; a
# 1% target and half an hour, so the challenge ends by itself; a funded term of a quarter hour.
RULES = (300, 600, 500)  # daily loss, drawdown (bps), leverage x100
TERMS = {"price": 500_000, "capital": 2 * USDC_1E6, "targetBps": 100, "duration": 1800,
         "traderShareChallengeBps": 0, "traderShareFundedBps": 8000, "fundedCapital": 8 * USDC_1E6}
FUNDED_TERM = 900

TERMS_TYPE = "(uint64,uint64,uint16,uint32,uint16,uint16,uint64)"
RULES_TYPE = "(uint16,uint16,uint32,uint32[])"


def pool_of(record: dict) -> str:
    return record["SharedPool"]


def view(to: str, sig: str, types: list, args: list, out: list) -> tuple:
    return c.call_view(to, sig, types, args, out)


def seats(record: dict) -> list[str]:
    return list(view(pool_of(record), "seats()", [], [], ["address[]"])[0])


def snapshot(record: dict) -> dict:
    """What the run looks like now: the pool's value and shares, the queue, each seat, balances."""
    sp = pool_of(record)
    holders = [c.address_of(n) for n in ("shared-operator", *DEPOSITORS)]
    out = {
        "value_1e8": view(sp, "value()", [], [], ["uint256"])[0],
        "total_shares": view(sp, "totalShares()", [], [], ["uint256"])[0],
        "queued_shares": view(sp, "queuedShares()", [], [], ["uint256"])[0],
        "blocker": view(sp, "blocker()", [], [], ["uint8", "address"]),
        "pool_spot_1e8": c.core_spot_balance(sp, c.USDC_TOKEN)["total"],
        "pool_evm_usdc_1e6": c.erc20_balance(c.TESTNET_USDC_ERC20, sp),
        "holders": {},
        "seats": [],
    }
    for h in holders:
        out["holders"][h] = {
            "shares": view(sp, "sharesOf(address)", ["address"], [h], ["uint256"])[0],
            "queued": view(sp, "queuedOf(address)", ["address"], [h], ["uint256"])[0],
            "spot_1e8": c.core_spot_balance(h, c.USDC_TOKEN)["total"],
            "evm_usdc_1e6": c.erc20_balance(c.TESTNET_USDC_ERC20, h),
        }
    for s in seats(record):
        ch = view(s, "challenge()", [], [], ["address"])[0]
        row = {"seat": s, "stage": view(s, "stage()", [], [], ["uint8"])[0],
               "spot_1e8": c.core_spot_balance(s, c.USDC_TOKEN)["total"],
               "earned_1e6": view(s, "earned()", [], [], ["uint256"])[0], "challenge": ch}
        if int(ch, 16):
            row["challenge_status"] = view(ch, "status()", [], [], ["uint8"])[0]
            row["challenge_equity_1e6"] = c.core_margin_summary(ch)["accountValue"]
            row["challenge_spot_1e8"] = c.core_spot_balance(ch, c.USDC_TOKEN)["total"]
        out["seats"].append(row)
    return out


def wait_until(label: str, probe, ok, timeout_s: int = 180):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        value = probe()
        if ok(value):
            return value
        time.sleep(2)
    raise SystemExit(f"timed out waiting for {label}")


def api_ok(resp) -> bool:
    return isinstance(resp, dict) and resp.get("status") == "ok"


# ── steps ─────────────────────────────────────────────────────────────────────────────

def cmd_status(record: dict, _args) -> None:
    c.record("shared_status", **snapshot(record))


def cmd_wallets(record: dict, args) -> None:
    """From shared-operator: USDC on HyperCore to each depositor (their deposit, the 1 USDC a new
    ticket's account costs them, and what creating their own account costs the sender), HYPE on
    HyperEVM for their transactions; to the trader, the price and the fee on HyperEVM and HYPE."""
    op = c.account("shared-operator")
    ex = c.exchange(op)
    wire = c.spot_token_wire("USDC")
    for name in DEPOSITORS:
        to = c.address_of(name)
        resp = ex.spot_transfer(args.deposit + 1.0, to, wire)
        c.record("shared_wallet_usdc", to=to, usdc=args.deposit + 1.0, response=resp)
        if not api_ok(resp):
            raise SystemExit(f"transfer to {name} refused: {resp}")
    for name in (*DEPOSITORS, TRADER):
        to = c.address_of(name)
        rcpt = c.send_tx(op, to, value=int(args.hype * 1e18))
        c.record("shared_wallet_hype", to=to, hype=args.hype, tx=rcpt["transactionHash"])
    trader_evm = int(round(args.trader_evm * USDC_1E6))
    rcpt = c.transact(op, c.TESTNET_USDC_ERC20, "transfer(address,uint256)", ["address", "uint256"],
                      [c.address_of(TRADER), trader_evm])
    c.record("shared_wallet_trader_usdc", to=c.address_of(TRADER), usdc_1e6=trader_evm, tx=rcpt["transactionHash"])


def cmd_start(record: dict, args) -> None:
    """The seed goes straight to the pool's HyperCore address, which creates its account (the sender
    pays 1 USDC more for that); then start() turns it into the platform's shares."""
    op = c.account("shared-operator")
    sp = pool_of(record)
    resp = c.exchange(op).spot_transfer(args.seed, sp, c.spot_token_wire("USDC"))
    c.record("shared_seed_sent", to=sp, usdc=args.seed, response=resp)
    if not api_ok(resp):
        raise SystemExit(f"seed refused: {resp}")
    wait_until("the seed to land", lambda: c.core_spot_balance(sp, c.USDC_TOKEN)["total"], lambda v: v > 0)
    rcpt = c.transact(op, sp, "start()")
    c.record("shared_started", tx=rcpt["transactionHash"], **snapshot(record))


def cmd_seat(record: dict, _args) -> None:
    op = c.account("shared-operator")
    assets = sorted(record["platform_assets"].values())
    t = TERMS
    rcpt = c.transact(op, pool_of(record), f"addSeat({RULES_TYPE},{TERMS_TYPE},uint32)",
                      [RULES_TYPE, TERMS_TYPE, "uint32"],
                      [(RULES[0], RULES[1], RULES[2], assets),
                       (t["price"], t["capital"], t["targetBps"], t["duration"], t["traderShareChallengeBps"],
                        t["traderShareFundedBps"], t["fundedCapital"]), FUNDED_TERM])
    c.record("shared_seat_added", tx=rcpt["transactionHash"], seats=seats(record))


def cmd_arm(record: dict, _args) -> None:
    """Tops the seat up from the pool, waits for it to land, then prepares the seat's account."""
    op = c.account("shared-operator")
    seat = seats(record)[0]
    need = view(seat, "capitalNeeded()", [], [], ["uint64"])[0]
    rcpt = c.transact(op, pool_of(record), "armSeat(address)", ["address"], [seat])
    c.record("shared_seat_armed", seat=seat, tx=rcpt["transactionHash"])
    wait_until("the top-up to land", lambda: c.core_spot_balance(seat, c.USDC_TOKEN)["total"], lambda v: v >= need)
    if not view(seat, "accountReady()", [], [], ["bool"])[0]:
        rcpt = c.transact(op, seat, "prepareAccount()")
        c.record("shared_seat_prepared", seat=seat, tx=rcpt["transactionHash"])
    c.record("shared_after_arm", **snapshot(record))


def cmd_deposit(record: dict, args) -> None:
    """The depositor opens a ticket on HyperEVM and sends USDC to it on HyperCore."""
    who = c.account(args.who)
    sp = pool_of(record)
    n = view(sp, "ticketCount(address)", ["address"], [who.address], ["uint256"])[0]
    rcpt = c.transact(who, sp, "openTicket()")
    ticket = view(sp, "ticketAddress(address,uint256)", ["address", "uint256"], [who.address, n], ["address"])[0]
    c.record("shared_ticket_opened", who=who.address, ticket=ticket, index=n, tx=rcpt["transactionHash"])
    resp = c.exchange(who).spot_transfer(args.usdc, ticket, c.spot_token_wire("USDC"))
    c.record("shared_deposit_sent", who=who.address, ticket=ticket, usdc=args.usdc, response=resp)
    if not api_ok(resp):
        raise SystemExit(f"deposit refused: {resp}")
    wait_until("the deposit to land", lambda: c.core_spot_balance(ticket, c.USDC_TOKEN)["total"],
               lambda v: v >= int(round(args.usdc * USDC_1E8)))


def cmd_settle(record: dict, _args) -> None:
    """A settlement point naming every open ticket that holds money."""
    op = c.account("shared-operator")
    sp = pool_of(record)
    count = view(sp, "openTicketCount()", [], [], ["uint256"])[0]
    opened = list(view(sp, "openTickets(uint256,uint256)", ["uint256", "uint256"], [0, count], ["address[]"])[0])
    funded = [t for t in opened if c.core_spot_balance(t, c.USDC_TOKEN)["total"] > 0][:8]
    before = snapshot(record)
    reason = before["blocker"]
    if reason[0] != 0:
        raise SystemExit(f"a settlement point can't run now: blocker {reason}")
    rcpt = c.transact(op, sp, "settle(address[])", ["address[]"], [funded])
    c.record("shared_point", tickets=funded, tx=rcpt["transactionHash"], block=int(rcpt["blockNumber"], 16),
             gas_used=int(rcpt["gasUsed"], 16), before=before)
    time.sleep(6)
    c.record("shared_after_point", **snapshot(record))


def cmd_buy(record: dict, _args) -> None:
    """The trader pays the price and the fee on HyperEVM; anyone starts the challenge once its
    capital is there."""
    tr = c.account(TRADER)
    op = c.account("shared-operator")
    seat = seats(record)[0]
    price = TERMS["price"]
    fee = view(record["PoolFactory"], "challengeFee()", [], [], ["uint256"])[0]
    c.transact(tr, c.TESTNET_USDC_ERC20, "approve(address,uint256)", ["address", "uint256"], [seat, price + fee])
    rcpt = c.transact(tr, seat, "buyChallenge()")
    ch = view(seat, "challenge()", [], [], ["address"])[0]
    c.record("shared_challenge_bought", seat=seat, challenge=ch, tx=rcpt["transactionHash"])
    wait_until("the challenge's capital to land", lambda: c.core_spot_balance(ch, c.USDC_TOKEN)["total"],
               lambda v: v >= TERMS["capital"] * 100)
    rcpt = c.transact(op, ch, "activate()")
    c.record("shared_challenge_started", challenge=ch, tx=rcpt["transactionHash"], **snapshot(record))


def cmd_request(record: dict, args) -> None:
    who = c.account(args.who)
    sp = pool_of(record)
    shares = view(sp, "sharesOf(address)", ["address"], [who.address], ["uint256"])[0]
    queued = view(sp, "queuedOf(address)", ["address"], [who.address], ["uint256"])[0]
    amount = shares - queued if args.shares == "all" else int(args.shares)
    rcpt = c.transact(who, sp, "requestRedeem(uint256)", ["uint256"], [amount])
    c.record("shared_requested", who=who.address, shares=amount, tx=rcpt["transactionHash"])


def cmd_expire(record: dict, _args) -> None:
    """Ends the seat's challenge once its time is up and settles it back into the seat."""
    op = c.account("shared-operator")
    seat = seats(record)[0]
    ch = view(seat, "challenge()", [], [], ["address"])[0]
    if not int(ch, 16):
        raise SystemExit("the seat has no challenge")
    deadline = view(ch, "deadline()", [], [], ["uint64"])[0]
    if time.time() <= deadline:
        raise SystemExit(f"the challenge runs until {deadline}; {int(deadline - time.time())} s to go")
    no_cancels, no_assets = [], []
    if view(ch, "status()", [], [], ["uint8"])[0] == 2:  # Active
        rcpt = c.transact(op, ch, "expire((uint32,uint64)[],uint32[],bytes32)",
                          ["(uint32,uint64)[]", "uint32[]", "bytes32"], [no_cancels, no_assets, b"\x00" * 32])
        c.record("shared_challenge_expired", challenge=ch, tx=rcpt["transactionHash"])
    for _ in range(8):
        if view(ch, "status()", [], [], ["uint8"])[0] == 8:  # Settled
            break
        rcpt = c.transact(op, ch, "settle((uint32,uint64)[],uint32[])", ["(uint32,uint64)[]", "uint32[]"],
                          [no_cancels, no_assets])
        c.record("shared_challenge_settle_step", challenge=ch, tx=rcpt["transactionHash"])
        time.sleep(6)
    c.record("shared_after_expire", **snapshot(record))


def cmd_release(record: dict, _args) -> None:
    op = c.account("shared-operator")
    seat = seats(record)[0]
    rcpt = c.transact(op, pool_of(record), "releaseSeat(address)", ["address"], [seat])
    c.record("shared_seat_released", seat=seat, tx=rcpt["transactionHash"])
    wait_until("the seat's capital to land", lambda: c.core_spot_balance(seat, c.USDC_TOKEN)["total"],
               lambda v: v == 0)
    c.record("shared_after_release", **snapshot(record))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True)
    sub = p.add_subparsers(dest="step", required=True)
    sub.add_parser("status")
    w = sub.add_parser("wallets")
    w.add_argument("--deposit", type=float, default=20.0)
    w.add_argument("--hype", type=float, default=0.004)
    w.add_argument("--trader-evm", type=float, default=1.2)
    s = sub.add_parser("start")
    s.add_argument("--seed", type=float, default=1.0)
    sub.add_parser("seat")
    sub.add_parser("arm")
    d = sub.add_parser("deposit")
    d.add_argument("--who", choices=DEPOSITORS, required=True)
    d.add_argument("--usdc", type=float, default=20.0)
    sub.add_parser("settle")
    sub.add_parser("buy")
    r = sub.add_parser("request")
    r.add_argument("--who", choices=DEPOSITORS, required=True)
    r.add_argument("--shares", default="all")
    sub.add_parser("expire")
    sub.add_parser("release")
    args = p.parse_args()

    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "SharedPool" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no shared pool")
    steps = {"status": cmd_status, "wallets": cmd_wallets, "start": cmd_start, "seat": cmd_seat, "arm": cmd_arm,
             "deposit": cmd_deposit, "settle": cmd_settle, "buy": cmd_buy, "request": cmd_request,
             "expire": cmd_expire, "release": cmd_release}
    steps[args.step](record, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
