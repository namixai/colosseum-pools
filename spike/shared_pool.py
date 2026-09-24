#!/usr/bin/env python3
"""Spike for the shared pool: two things its design leans on, measured on the live testnet.

    gas     What reading one seat costs at a settlement point. The runtime code of SharedPoolProbe is
            placed at an address with an eth_call state override and called against the live pools, so
            nothing is deployed and nothing is spent.
    atomic  Whether a precompile read can show a HyperCore transfer half done: the sender debited and the
            recipient not yet credited, or both at once. One thread reads both accounts in a single call
            (SharedPoolProbe.pair) as often as the RPC allows while the main thread sends USDC from one to
            the other; every read must add up to the same total. `--via api` sends from the key itself;
            `--via corewriter` sends from the key's SpikeAccount (action 6), the way pool contracts send.
    deploy  The SpikeAccount for `--via corewriter`, owned by the key, and its first USDC on HyperCore.

Testnet only, like the rest of the spike (hlspike.common refuses anything else). Keys are read by name from
COLOSSEUM_KEY_DIR and never printed. Results go to spike/results/<date>.jsonl; the raw reads of an `atomic`
run go to spike/results/atomic-*.jsonl.

    .venv/bin/python shared_pool.py gas
    COLOSSEUM_RPC_URL=https://rpcs.chain.link/hyperevm/testnet \\
        .venv/bin/python shared_pool.py atomic --key trader --to EXISTING_ACCOUNT_ADDRESS --usdc 0.2 --times 4
    .venv/bin/python shared_pool.py deploy --key YOUR_KEY_NAME --fund 1.2
    COLOSSEUM_RPC_URL=https://rpcs.chain.link/hyperevm/testnet \\
        .venv/bin/python shared_pool.py atomic --key YOUR_KEY_NAME --via corewriter --usdc 0.25 --times 4
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import threading
import time
from pathlib import Path

import eth_abi
from eth_utils import to_checksum_address

from hlspike import common as c

# An address with no code on chain; the probe's code is placed here for each call.
PROBE_AT = "0x00000000000000000000000000000000005ea7ed"
PAIR_OUT = ["uint256", "uint256", "uint64", "uint64", "int64", "int64"]
SEAT_OUT = ["uint256", "uint8", "address", "uint8", "uint64", "int64", "uint64", "int64", "uint256"]
MAX_USDC_PER_SEND = 1.0

_code: str | None = None


def probe_code() -> str:
    global _code
    if _code is None:
        _code = c.artifact("SharedPoolProbe")["deployedBytecode"]["object"]
    return _code


def probe(signature: str, types: list[str], args: list, out: list[str]) -> tuple:
    data = c.encode_call(signature, types, args)
    raw = c.rpc("eth_call", [{"to": PROBE_AT, "data": "0x" + data.hex()}, "latest", {PROBE_AT: {"code": probe_code()}}])
    return eth_abi.decode(out, bytes.fromhex(raw[2:]))


def pools_of(deployment: str) -> list[str]:
    path = c.REPO_ROOT / "deployments" / f"testnet-{deployment}.json"
    factory = json.loads(path.read_text())["PoolFactory"]
    return [to_checksum_address(p) for p in c.call_view(factory, "pools()", [], [], ["address[]"])[0]]


# ── gas ───────────────────────────────────────────────────────────────────────────────

def cmd_gas(_args) -> None:
    c.assert_testnet()
    seats = []
    for deployment in ("demo", "rehearsal"):
        for pool in pools_of(deployment):
            gas_used, stage, ch, ch_status, spot, equity, ch_spot, ch_equity, earned = probe(
                "seat(address)", ["address"], [pool], SEAT_OUT)
            row = {"deployment": deployment, "pool": pool, "gas": gas_used, "stage": stage,
                   "challenge": to_checksum_address(ch), "challenge_status": ch_status, "spot_1e8": spot,
                   "equity_1e6": equity, "challenge_spot_1e8": ch_spot, "challenge_equity_1e6": ch_equity,
                   "earned_1e6": earned}
            c.record("p_gas_seat", **row)
            seats.append(row)

    accounts = {"idle pool": seats[0]["pool"], "deployer (EOA)": c.address_of("deployer"),
                "no account": to_checksum_address("0x" + os.urandom(20).hex())}
    for row in seats:
        if int(row["challenge"], 16):
            accounts[f"challenge of {row['pool'][:8]}"] = row["challenge"]
    for label, who in accounts.items():
        spot, margin, exists = probe("unitCosts(address)", ["address"], [who], ["uint256", "uint256", "uint256"])
        c.record("p_gas_unit", label=label, account=who, spot_balance=spot, margin_summary=margin,
                 core_user_exists=exists, exists_on_core=c.core_user_exists(who))

    for row in seats:
        for who in (row["pool"], row["challenge"]):
            if not int(who, 16):
                continue
            gas_used, breach = probe("violationCost(address)", ["address"], [who], ["uint256", "uint8"])
            c.record("p_gas_violation", account=who, gas=gas_used, breach=breach)


# ── atomic ────────────────────────────────────────────────────────────────────────────

STATE_PATH = c.RESULTS_DIR / "state-shared.json"


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def cmd_deploy(args) -> None:
    """A SpikeAccount owned by the key, and its first USDC on HyperCore, sent by the key through the API.
    That transfer creates the contract's HyperCore account, which costs the key 1 USDC more."""
    c.assert_testnet()
    if args.fund > MAX_USDC_PER_SEND * 3:
        raise SystemExit("refusing: funding above the script cap")
    acct = c.account(args.key)
    if c.evm_balance(acct.address) == 0:
        raise SystemExit(f"{args.key} has no HYPE on HyperEVM for gas")
    state = load_state()
    addr = state.get(args.key)
    if addr is None:
        addr, rcpt = c.deploy(acct, "SpikeAccount", ["address"], [acct.address])
        state[args.key] = addr
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        c.record("p_deployed", owner=acct.address, address=addr, tx=rcpt["transactionHash"],
                 gas_used=int(rcpt["gasUsed"], 16))
    before = c.core_spot_balance(addr, c.USDC_TOKEN)["total"]
    resp = c.exchange(acct).spot_transfer(args.fund, addr, c.spot_token_wire("USDC"))
    c.record("p_fund_sent", to=addr, usdc=args.fund, response=resp)
    # The SDK hands back the exchange's answer without checking it.
    if not (isinstance(resp, dict) and resp.get("status") == "ok"):
        raise SystemExit(f"the funding transfer was refused: {resp}")
    deadline = time.time() + 120
    after = before
    while time.time() < deadline and after <= before:
        time.sleep(1)
        after = c.core_spot_balance(addr, c.USDC_TOKEN)["total"]
    c.record("p_fund_result", contract=addr, spot_before_1e8=before, spot_after_1e8=after,
             arrived=after > before, exists=c.core_user_exists(addr))
    if after <= before:
        raise SystemExit("the funding did not arrive within two minutes")


def cmd_atomic(args) -> None:
    c.assert_testnet()
    if args.usdc > MAX_USDC_PER_SEND:
        raise SystemExit(f"refusing: more than {MAX_USDC_PER_SEND} USDC per send")
    if args.times < 1:
        raise SystemExit("--times must be at least 1: a run with nothing sent proves nothing")
    acct = c.account(args.key)
    if args.via == "api":
        sender = acct.address
    else:
        sender = load_state().get(args.key)
        if sender is None:
            raise SystemExit(f"no SpikeAccount for {args.key}; run `deploy` first")
    if args.via == "api" and not args.to:
        raise SystemExit("--via api needs --to: the key sends, so without it the key would send to itself")
    to = to_checksum_address(args.to) if args.to else acct.address
    if to == sender:
        raise SystemExit("sender and recipient are the same account; a debit and a credit can't be told apart")
    if not c.core_user_exists(to):
        # A send that creates the recipient's account costs the sender 1 USDC more, and the reads would
        # no longer add up for a reason that has nothing to do with the question.
        raise SystemExit(f"{to} has no HyperCore account; pick an existing one")
    first = probe("pair(address,address)", ["address", "address"], [sender, to], PAIR_OUT)
    if first[2] < int(round(args.usdc * args.times * 1e8)):
        raise SystemExit(f"{sender} holds {first[2] / 1e8} USDC on spot, less than {args.usdc} x {args.times}")

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = c.RESULTS_DIR / f"atomic-{args.via}-{stamp}.jsonl"
    samples: list[dict] = []
    stop = threading.Event()
    period = 1.0 / args.hz

    def reader() -> None:
        with open(raw_path, "a", encoding="utf-8") as fh:
            while not stop.is_set():
                t0 = time.time()
                try:
                    block, ts, spot_a, spot_b, perp_a, perp_b = probe(
                        "pair(address,address)", ["address", "address"], [sender, to], PAIR_OUT)
                except Exception as exc:  # a throttled or failed read is a gap, not a result
                    row = {"t": round(t0, 3), "error": str(exc)[:200]}
                else:
                    row = {"t": round(t0, 3), "block": block, "ts": ts, "spot_a": spot_a, "spot_b": spot_b,
                           "perp_a": perp_a, "perp_b": perp_b}
                samples.append(row)
                fh.write(json.dumps(row) + "\n")
                time.sleep(max(0.0, period - (time.time() - t0)))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(args.window)  # reads before the first send
    wei = int(round(args.usdc * 1e8))
    wire = c.spot_token_wire("USDC")
    ex = c.exchange(acct) if args.via == "api" else None  # built once: the SDK fetches metadata on creation
    sends = []
    for i in range(args.times):
        t_send = time.time()
        if ex is not None:
            resp, tx_block = ex.spot_transfer(args.usdc, to, wire), None
            # The SDK hands back the exchange's answer without checking it; a refused send moves nothing.
            ok = isinstance(resp, dict) and resp.get("status") == "ok"
        else:
            rcpt = c.transact(acct, sender, "spotSend(address,uint64,uint64)", ["address", "uint64", "uint64"],
                              [to, c.USDC_TOKEN, wei])
            resp, tx_block = rcpt["transactionHash"], int(rcpt["blockNumber"], 16)
            ok = True  # mined; whether HyperCore carried it out shows only as a landing
        t_ack = time.time()
        sends.append({"i": i, "t_send": round(t_send, 3), "t_ack": round(t_ack, 3), "tx_block": tx_block, "ok": ok})
        c.record("p_atomic_send", via=args.via, i=i, sender=sender, to=to, usdc=args.usdc, response=resp,
                 tx_block=tx_block)
        time.sleep(args.window)
    stop.set()
    thread.join(timeout=10)
    summary = analyse(samples, sends, wei)
    c.record("p_atomic_result", via=args.via, sender=sender, to=to, usdc=args.usdc, times=args.times, hz=args.hz,
             rpc=c.RPC_URL, raw=str(raw_path.relative_to(c.REPO_ROOT)), **summary)


def analyse(samples: list[dict], sends: list[dict], step_1e8: int) -> dict:
    """Every good read's two spot balances must add up to the first read's total. Each change of the sender's
    balance is a transfer seen landing: say whether the recipient changed in the same read, how long after the
    send, and, for a CoreWriter send, how many blocks after the block that carried it. The check counts only
    if every send was accepted and every one was seen landing: a run where nothing moved proves nothing."""
    good = [s for s in samples if "error" not in s]
    if not good:
        return {"reads": len(samples), "good_reads": 0}

    # Spot only: a perp account value moves with an open position whether or not USDC moves between
    # the two, so it is watched on its own below instead of being part of the total.
    def total(s: dict) -> int:
        return s["spot_a"] + s["spot_b"]

    base = total(good[0])
    off = [s for s in good if total(s) != base]
    landings = []
    for prev, cur in zip(good, good[1:]):
        da, db = cur["spot_a"] - prev["spot_a"], cur["spot_b"] - prev["spot_b"]
        if da or db:
            before = [x for x in sends if x["t_send"] <= cur["t"]]
            sent = before[-1] if before else None
            row = {"block": cur["block"], "t": cur["t"], "d_sender_1e8": da, "d_recipient_1e8": db,
                   "same_read": da == -db, "steps": -da / step_1e8 if step_1e8 else None,
                   "after_send_s": round(cur["t"] - sent["t_send"], 3) if sent else None,
                   "prev_read_block": prev["block"]}
            if sent and sent.get("tx_block") is not None:
                row["tx_block"] = sent["tx_block"]
                row["blocks_after_tx"] = cur["block"] - sent["tx_block"]
                row["prev_read_blocks_after_tx"] = prev["block"] - sent["tx_block"]
            landings.append(row)
    blocks = sorted({s["block"] for s in good})
    gaps = [b for a, b in zip(blocks, blocks[1:]) if b - a > 1]
    accepted = sum(1 for x in sends if x.get("ok"))
    perp_moved = len({(s["perp_a"], s["perp_b"]) for s in good}) > 1
    # Each landing must be exactly one requested transfer: a smaller move that happened to be equal and
    # opposite would otherwise count for a send it isn't.
    complete = (len(sends) > 0 and accepted == len(sends) and len(landings) == len(sends)
                and all(x["same_read"] and x["d_sender_1e8"] == -step_1e8 for x in landings)
                and not perp_moved)
    return {"reads": len(samples), "good_reads": len(good), "failed_reads": len(samples) - len(good),
            "blocks_seen": len(blocks), "first_block": blocks[0], "last_block": blocks[-1],
            "blocks_skipped_between_reads": len(gaps), "total_1e8": base, "reads_off_total": len(off),
            "off_examples": off[:5], "sends": len(sends), "sends_accepted": accepted,
            "landings_seen": len(landings), "perp_moved": perp_moved, "complete": complete,
            "landings": landings}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gas")
    d = sub.add_parser("deploy")
    d.add_argument("--key", required=True, help="name of the owning key in COLOSSEUM_KEY_DIR")
    d.add_argument("--fund", type=float, default=1.2, help="USDC the key sends to the contract on HyperCore")
    a = sub.add_parser("atomic")
    a.add_argument("--key", required=True, help="name of the sending key in COLOSSEUM_KEY_DIR")
    a.add_argument("--via", choices=("api", "corewriter"), default="api",
                   help="api: the key sends; corewriter: the key's SpikeAccount sends (action 6)")
    a.add_argument("--to", help="an address that already has a HyperCore account (default: the key)")
    a.add_argument("--usdc", type=float, default=0.2)
    a.add_argument("--times", type=int, default=4)
    a.add_argument("--hz", type=float, default=2.5, help="reads per second; mind the RPC's limit")
    a.add_argument("--window", type=float, default=20.0, help="seconds of reads before and after each send")
    args = p.parse_args()
    {"gas": cmd_gas, "deploy": cmd_deploy, "atomic": cmd_atomic}[args.cmd](args)


if __name__ == "__main__":
    main()
