"""Live testnet spike: one subcommand per question, run in the order below.

    spike/.venv/bin/python spike/run.py status
    spike/.venv/bin/python spike/run.py gas --buy-hype 0.5 --to-evm 0.3
    spike/.venv/bin/python spike/run.py deploy
    spike/.venv/bin/python spike/run.py fund --usdc 40          # Q3a: EOA -> new contract account
    spike/.venv/bin/python spike/run.py q7                      # margin mode: default, contract, agent
    spike/.venv/bin/python spike/run.py q3                      # Q3b: contract -> contract -> perp -> back
    spike/.venv/bin/python spike/run.py q1                      # agent replacement + resting orders (+Q6)
    spike/.venv/bin/python spike/run.py q4                      # equity: precompile vs info API
    spike/.venv/bin/python spike/run.py q2                      # one agent address, two accounts
    spike/.venv/bin/python spike/run.py q5 --usdc 5             # trader pays on HyperEVM
    spike/.venv/bin/python spike/run.py q5b --usdc 1            # which bridge path credits a contract
    spike/.venv/bin/python spike/run.py q5c                     # the same bridge, a fresh contract, each mode
    spike/.venv/bin/python spike/run.py q5d                     # the same bridge to an EOA that sends nothing
    spike/.venv/bin/python spike/run.py q5e                     # the forwarding path (dex 0): EOA and contracts
    spike/.venv/bin/python spike/run.py q5f --usdc 3            # the same forward with a larger amount
    spike/.venv/bin/python spike/run.py q9 --dex xyz --coin XYZ100 --usdc 15   # HIP-3: a builder dex
    spike/.venv/bin/python spike/run.py q10 --dex xyz --coin XYZ100 --dry-run  # HIP-3 trading: what it would do
    spike/.venv/bin/python spike/run.py q10 --dex xyz --coin XYZ100 --usdc 15  # fill, stop and take, close, back
    spike/.venv/bin/python spike/run.py stopgas --dex xyz                      # the worst stop's gas, read-only

Testnet only (chain 998, api.hyperliquid-testnet.xyz); common.py refuses anything else.
Every observation goes to spike/results/<date>.jsonl. Nothing here prints a key.

An agent address is never approved twice. Hyperliquid's docs warn that actions signed by a
deregistered agent can be replayed once its nonce set is pruned, so every agent this script
has replaced is written to the state file as burned and refused from then on.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Callable

import eth_abi
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from hyperliquid.utils.signing import get_timestamp_ms, sign_agent
from hyperliquid.utils.types import Cloid

from hlspike import common as c

STATE_PATH = c.RESULTS_DIR / "state-testnet.json"
PERP = "BTC"                 # asset 3 on testnet, szDecimals 5
MIN_NOTIONAL = 11.0          # Hyperliquid refuses orders under $10
MAX_USDC_PER_STEP = 100.0    # a cap on this script, not on the venue


# ── state ────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"contracts": {}, "burned_agents": [], "approvals": []}


def save_state(state: dict) -> None:
    c.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def contract(state: dict, label: str) -> str:
    try:
        return state["contracts"][label]
    except KeyError:
        raise SystemExit(f"contract {label} not deployed yet; run `deploy` first")


def fresh_address() -> str:
    """An address nobody holds a key for and HyperCore has never seen."""
    for _ in range(5):
        addr = to_checksum_address("0x" + os.urandom(20).hex())
        if not c.core_user_exists(addr):
            return addr
    raise RuntimeError("could not find an unused address")


def guard_agent(state: dict, agent: str) -> None:
    if agent.lower() in (a.lower() for a in state["burned_agents"]):
        raise SystemExit(f"agent {agent} is burned; refusing to approve it again")


def burn(state: dict, agent: str, why: str) -> None:
    if agent.lower() not in (a.lower() for a in state["burned_agents"]):
        state["burned_agents"].append(agent)
    c.record("agent_burned", agent=agent, why=why)
    save_state(state)


# ── helpers ──────────────────────────────────────────────────────────────────────────

def wait_until(what: str, probe: Callable[[], Any], ok: Callable[[Any], bool], timeout_s: int = 45) -> Any:
    """Poll one observable until it changes the way we expect, or give up and say so."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = probe()
        if ok(last):
            return last
        time.sleep(2)
    c.record("wait_timeout", what=what, last=last, timeout_s=timeout_s)
    return last


def perp_meta(coin: str) -> tuple[int, int]:
    meta = c.info_post({"type": "meta"})
    for i, a in enumerate(meta["universe"]):
        if a["name"] == coin:
            return i, a["szDecimals"]
    raise KeyError(coin)


def mid(coin: str) -> float:
    return float(c.info_post({"type": "allMids"})[coin])


def perp_px(px: float, sz_decimals: int) -> float:
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def size_for(notional: float, px: float, sz_decimals: int) -> float:
    step = 10 ** (-sz_decimals)
    return round(math.ceil(notional / px / step) * step, sz_decimals)


def role(addr: str) -> Any:
    return c.info_post({"type": "userRole", "user": addr})


def is_agent_of(resp: Any, user: str) -> bool:
    return (isinstance(resp, dict) and resp.get("role") == "agent"
            and str(resp.get("data", {}).get("user", "")).lower() == user.lower())


def open_orders(user: str) -> list:
    return c.info_post({"type": "openOrders", "user": user})


def btc_position(user: str) -> float:
    state = c.info_post({"type": "clearinghouseState", "user": user})
    for ap in state.get("assetPositions", []):
        if ap["position"]["coin"] == PERP:
            return float(ap["position"]["szi"])
    return 0.0


def agent_order(agent_name: str, account: str, is_buy: bool, px: float, sz: float,
                tif: str = "Alo", cloid: Cloid | None = None, reduce_only: bool = False) -> Any:
    ex = c.exchange(c.account(agent_name), account_address=account)
    return ex.order(PERP, is_buy, sz, px, {"limit": {"tif": tif}}, reduce_only=reduce_only, cloid=cloid)


def resting_oid(resp: Any) -> int | None:
    try:
        st = resp["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return None
    return st.get("resting", {}).get("oid") if isinstance(st, dict) else None


def contract_add_agent(state: dict, label: str, agent: str, name: str = "") -> dict:
    guard_agent(state, agent)
    addr = contract(state, label)
    rcpt = c.transact(c.account("deployer"), addr, "addApiWallet(address,string)", ["address", "string"], [agent, name])
    state["approvals"].append({"by": addr, "agent": agent, "name": name, "tx": rcpt["transactionHash"]})
    save_state(state)
    c.record("add_api_wallet_sent", account=addr, agent=agent, name=name, tx=rcpt["transactionHash"],
             evm_block=int(rcpt["blockNumber"], 16))
    return rcpt


# ── subcommands ──────────────────────────────────────────────────────────────────────

def cmd_status(_args, state: dict) -> None:
    c.assert_testnet()
    for name in ("deployer", "trader"):
        addr = c.address_of(name)
        c.record("status", who=name, evm_hype_wei=c.evm_balance(addr),
                 evm_usdc=c.erc20_balance(c.TESTNET_USDC_ERC20, addr), **c.core_snapshot(addr))
    for label, addr in state["contracts"].items():
        c.record("status", who=label, core_user_exists=c.core_user_exists(addr),
                 evm_usdc=c.erc20_balance(c.TESTNET_USDC_ERC20, addr), **c.core_snapshot(addr))
    c.record("status_burned_agents", burned=state["burned_agents"])


def ensure_spot_usdc(acct, amount: float) -> None:
    """A transfer from someone's testnet account may land on the perp side (usdSend) or on
    the spot side (spotSend). The spike spends from spot, so move it over if needed."""
    snap = c.core_snapshot(acct.address)
    spot, perp = float(snap["spotUSDC"]), float(snap["withdrawable"] or 0)
    if spot >= amount:
        return
    move = min(perp, amount - spot)
    if move <= 0:
        raise SystemExit(f"{acct.address} has {spot} USDC on spot and {perp} on perp; need {amount}")
    resp = c.exchange(acct).usd_class_transfer(round(move, 6), False)
    c.record("perp_to_spot", amount=move, response=resp)
    wait_until("spot USDC", lambda: float(c.core_snapshot(acct.address)["spotUSDC"]), lambda v: v >= amount)


def cmd_gas(args, _state: dict) -> None:
    """Buy a little testnet HYPE on HyperCore spot and move part of it to the deployer's
    HyperEVM address, which needs it for gas. Mock funds only."""
    deployer = c.account("deployer")
    ensure_spot_usdc(deployer, 100.0)
    ex = c.exchange(deployer)
    spot = c.info_post({"type": "spotMeta"})
    tokens = {t["index"]: t for t in spot["tokens"]}
    pair = next(p for p in spot["universe"] if [tokens[x]["name"] for x in p["tokens"]] == ["HYPE", "USDC"])
    coin = pair["name"]
    # The testnet HYPE book is thin: on 17 Sep the best ask sat 10% above the mid, so a market
    # order with 5% slippage matched nothing. Buy at the best ask instead, capped in USDC.
    asks = c.info_post({"type": "l2Book", "coin": coin})["levels"][1]
    if not asks:
        raise SystemExit(f"nobody sells HYPE on the testnet book {coin}")
    ask = float(asks[0]["px"])
    if args.buy_hype * ask > 30:
        raise SystemExit(f"refusing: {args.buy_hype} HYPE at {ask} is more than 30 USDC")
    resp = ex.order(coin, True, args.buy_hype, ask, {"limit": {"tif": "Ioc"}})
    c.record("gas_buy_hype", pair=coin, ask=ask, size=args.buy_hype, response=resp)
    time.sleep(3)
    hype_wire = c.spot_token_wire("HYPE")
    resp = ex.spot_transfer(args.to_evm, c.HYPE_SYSTEM, hype_wire)
    c.record("gas_hype_to_evm", amount=args.to_evm, token=hype_wire, response=resp)
    got = wait_until("evm hype arrives", lambda: c.evm_balance(deployer.address), lambda v: v > 0)
    c.record("gas_result", evm_hype_wei=got)


def cmd_deploy(_args, state: dict) -> None:
    deployer = c.account("deployer")
    if c.evm_balance(deployer.address) == 0:
        raise SystemExit("deployer has no HYPE on HyperEVM; run `gas` first")
    for label in ("A", "B"):
        if label in state["contracts"]:
            c.record("deploy_skip", label=label, address=state["contracts"][label])
            continue
        addr, rcpt = c.deploy(deployer, "SpikeAccount", ["address"], [deployer.address])
        state["contracts"][label] = addr
        save_state(state)
        c.record("deployed", label=label, address=addr, tx=rcpt["transactionHash"],
                 gas_used=int(rcpt["gasUsed"], 16), core_user_exists=c.core_user_exists(addr))


def cmd_fund(args, state: dict) -> None:
    """Q3a: the deployer (an EOA) sends USDC on HyperCore to contract A, which has never
    existed there. What arrives, and what does the sender pay for creating the account?"""
    if args.usdc > MAX_USDC_PER_STEP:
        raise SystemExit("amount above the script cap")
    deployer = c.account("deployer")
    a = contract(state, args.to)
    before_sender = c.core_snapshot(deployer.address)
    before_dest = c.core_snapshot(a)
    existed = c.core_user_exists(a)
    resp = c.exchange(deployer).spot_transfer(args.usdc, a, c.spot_token_wire("USDC"))
    c.record("fund_sent", to=a, usdc=args.usdc, response=resp, exists_before=existed)
    after_dest = wait_until("usdc arrives at contract", lambda: c.core_snapshot(a),
                            lambda s: float(s["spotUSDC"]) > float(before_dest["spotUSDC"]))
    after_sender = c.core_snapshot(deployer.address)
    c.record(
        "fund_result",
        sender_spot_before=before_sender["spotUSDC"], sender_spot_after=after_sender["spotUSDC"],
        dest_spot_before=before_dest["spotUSDC"], dest_spot_after=after_dest["spotUSDC"],
        dest_exists_after=c.core_user_exists(a),
        sender_ledger=c.info_post({"type": "userNonFundingLedgerUpdates", "user": deployer.address,
                                   "startTime": get_timestamp_ms() - 600_000}),
    )


def cmd_q3(args, state: dict) -> None:
    """Q3b: contract A -> contract B (new on HyperCore) through CoreWriter action 6, then B
    moves it to perp and back (action 7), then B -> A. Every leg is read back."""
    deployer = c.account("deployer")
    a, b = contract(state, "A"), contract(state, "B")
    amount = args.usdc
    wei = int(round(amount * 1e8))
    ntl = int(round(amount * 1e6))

    def snap() -> dict:
        return {"A": c.core_snapshot(a), "B": c.core_snapshot(b), "B_exists": c.core_user_exists(b)}

    s0 = snap()
    c.record("q3_start", **s0)
    c.transact(deployer, a, "spotSend(address,uint64,uint64)", ["address", "uint64", "uint64"], [b, c.USDC_TOKEN, wei])
    s1 = wait_until("B receives USDC", snap, lambda s: float(s["B"]["spotUSDC"]) > float(s0["B"]["spotUSDC"]))
    c.record("q3_after_A_to_B", **s1)

    c.transact(deployer, b, "usdClassTransfer(uint64,bool)", ["uint64", "bool"], [ntl, True])
    s2 = wait_until("B perp funded", snap, lambda s: float(s["B"]["perpAccountValue"]) > 0)
    c.record("q3_after_B_to_perp", **s2, precompile_margin=c.core_margin_summary(b))

    c.transact(deployer, b, "usdClassTransfer(uint64,bool)", ["uint64", "bool"], [ntl, False])
    s3 = wait_until("B perp emptied", snap, lambda s: float(s["B"]["perpAccountValue"]) == 0)
    c.record("q3_after_B_to_spot", **s3)

    back = float(s3["B"]["spotUSDC"])
    c.transact(deployer, b, "spotSend(address,uint64,uint64)", ["address", "uint64", "uint64"],
               [a, c.USDC_TOKEN, int(round(back * 1e8))])
    s4 = wait_until("B emptied", snap, lambda s: float(s["B"]["spotUSDC"]) == 0)
    c.record("q3_after_B_to_A", **s4)
    for who, addr in (("A", a), ("B", b)):
        c.record("q3_ledger", who=who, ledger=c.info_post(
            {"type": "userNonFundingLedgerUpdates", "user": addr, "startTime": get_timestamp_ms() - 3_600_000}))


def cmd_q4(_args, state: dict) -> None:
    """Q4: equity through precompile 0x80F against the info API, on our own accounts."""
    for label, addr in state["contracts"].items():
        via_contract = c.call_view(addr, "marginSummary(address)", ["address"], [addr],
                                   ["(int64,uint64,uint64,int64)"])[0]
        c.record("q4", who=label, precompile_direct=c.core_margin_summary(addr),
                 precompile_via_contract=list(via_contract),
                 info_api=c.info_post({"type": "clearinghouseState", "user": addr})["marginSummary"])


def cmd_q1(args, state: dict) -> None:
    """Q1 + Q6. A approves agent-a, agent-a rests an order; A replaces agent-a with agent-b.
    Then: is agent-a refused, does its resting order survive, can A cancel it by oid, can A
    cancel an agent-b order by cloid, and does a fresh keyless address cut agent-b off."""
    deployer = c.account("deployer")
    a = contract(state, "A")
    agent_a, agent_b = c.address_of("agent-a"), c.address_of("agent-b")
    asset, szd = perp_meta(PERP)

    perp_value = float(c.core_snapshot(a)["perpAccountValue"])
    if perp_value < 5:
        ntl = int(args.margin * 1e6)
        c.transact(deployer, a, "usdClassTransfer(uint64,bool)", ["uint64", "bool"], [ntl, True])
        wait_until("A perp margin", lambda: c.core_snapshot(a), lambda s: float(s["perpAccountValue"]) >= 5)

    # 1. approve agent-a on A
    contract_add_agent(state, "A", agent_a)
    r = wait_until("agent-a becomes A's agent", lambda: role(agent_a), lambda v: is_agent_of(v, a))
    c.record("q1_role_agent_a_after_approve", role=r, expected_user=a)

    # 2. agent-a rests a far-away buy
    m = mid(PERP)
    px = perp_px(m * 0.6, szd)
    sz = size_for(MIN_NOTIONAL, px, szd)
    resp = agent_order("agent-a", a, True, px, sz)
    oid_a = resting_oid(resp)
    c.record("q1_agent_a_rests", px=px, sz=sz, response=resp, oid=oid_a)

    # 3. replace with agent-b (same, unnamed slot)
    contract_add_agent(state, "A", agent_b)
    r_b = wait_until("agent-b becomes A's agent", lambda: role(agent_b), lambda v: is_agent_of(v, a))
    burn(state, agent_a, "replaced on A by agent-b")
    c.record("q1_roles_after_replace", agent_a=role(agent_a), agent_b=r_b)

    # 4. agent-a tries again
    resp = agent_order("agent-a", a, True, perp_px(m * 0.6, szd), sz)
    c.record("q1_agent_a_after_replace", response=resp)

    # 5. did agent-a's resting order survive the replacement?
    oo = open_orders(a)
    c.record("q1_open_orders_after_replace", open_orders=oo,
             agent_a_order_still_open=any(o.get("oid") == oid_a for o in oo))

    # 6. Q6: A cancels it by oid through CoreWriter action 10
    if oid_a is not None:
        c.transact(deployer, a, "cancelByOid(uint32,uint64)", ["uint32", "uint64"], [asset, oid_a])
        oo = wait_until("oid cancelled", lambda: open_orders(a),
                        lambda v: not any(o.get("oid") == oid_a for o in v))
        c.record("q6_cancel_by_oid", oid=oid_a, still_open=any(o.get("oid") == oid_a for o in oo),
                 status=c.info_post({"type": "orderStatus", "user": a, "oid": oid_a}))

    # 7. Q6: agent-b rests with a cloid, A cancels by cloid (action 11)
    cloid_int = int.from_bytes(os.urandom(16), "big")
    resp = agent_order("agent-b", a, True, perp_px(m * 0.6, szd), sz, cloid=Cloid.from_int(cloid_int))
    oid_b = resting_oid(resp)
    c.record("q6_agent_b_rests_with_cloid", cloid=hex(cloid_int), response=resp, oid=oid_b)
    if oid_b is not None:
        c.transact(deployer, a, "cancelByCloid(uint32,uint128)", ["uint32", "uint128"], [asset, cloid_int])
        oo = wait_until("cloid cancelled", lambda: open_orders(a),
                        lambda v: not any(o.get("oid") == oid_b for o in v))
        c.record("q6_cancel_by_cloid", oid=oid_b, still_open=any(o.get("oid") == oid_b for o in oo),
                 status=c.info_post({"type": "orderStatus", "user": a, "oid": oid_b}))

    # 8. agent-b opens a small long, so the stop has a position to close, and leaves one
    #    order resting, so we can see what happens to it once the margin is gone
    open_px = perp_px(mid(PERP) * 1.02, szd)
    resp = agent_order("agent-b", a, True, open_px, sz, tif="Ioc")
    pos = wait_until("A has a BTC position", lambda: btc_position(a), lambda p: p != 0.0, timeout_s=20)
    c.record("q1_agent_b_opens_position", px=open_px, sz=sz, response=resp, position=pos)
    resp = agent_order("agent-b", a, True, perp_px(m * 0.6, szd), sz)
    oid_left = resting_oid(resp)
    c.record("q1_agent_b_leaves_resting_order", response=resp, oid=oid_left)

    # 9. the stop path: replace agent-b with a fresh keyless address
    dead = fresh_address()
    contract_add_agent(state, "A", dead)
    r_dead = wait_until("fresh address becomes A's agent", lambda: role(dead), lambda v: is_agent_of(v, a))
    burn(state, agent_b, "replaced on A by a fresh keyless address")
    burn(state, dead, "keyless stop address; nobody can sign for it")
    resp = agent_order("agent-b", a, True, perp_px(m * 0.6, szd), sz)
    c.record("q1_stop_path", dead=dead, dead_role=r_dead, agent_b_role=role(agent_b), agent_b_order=resp)

    # 10. the contract closes the position itself: reduce-only IOC through action 1.
    #     First with a price that ignores Hyperliquid's price rules, then a rounded one,
    #     so we learn whether the contract has to round (the EVM call succeeds either way).
    size_now = abs(btc_position(a))
    if size_now > 0:
        raw_px = mid(PERP) * 0.95
        c.transact(deployer, a, "placeLimitOrder(uint32,bool,uint64,uint64,bool,uint8,uint128)",
                   ["uint32", "bool", "uint64", "uint64", "bool", "uint8", "uint128"],
                   [asset, False, int(raw_px * 1e8), int(round(size_now * 1e8)), True, 3, 0])
        left = wait_until("close with unrounded price", lambda: btc_position(a), lambda p: p == 0.0, timeout_s=20)
        c.record("q1_close_unrounded", px_1e8=int(raw_px * 1e8), position_after=left)
        if left != 0.0:
            px = perp_px(mid(PERP) * 0.95, szd)
            c.transact(deployer, a, "placeLimitOrder(uint32,bool,uint64,uint64,bool,uint8,uint128)",
                       ["uint32", "bool", "uint64", "uint64", "bool", "uint8", "uint128"],
                       [asset, False, int(round(px * 1e8)), int(round(abs(left) * 1e8)), True, 3, 0])
            left = wait_until("close with rounded price", lambda: btc_position(a), lambda p: p == 0.0, timeout_s=20)
            c.record("q1_close_rounded", px=px, position_after=left)

    # 11. move the free perp balance back to spot while the orphan order still rests.
    #     Does Hyperliquid cancel it, keep it, or refuse the transfer?
    free = float(c.info_post({"type": "clearinghouseState", "user": a})["withdrawable"])
    if free > 0:
        c.transact(deployer, a, "usdClassTransfer(uint64,bool)", ["uint64", "bool"], [int(free * 1e6), False])
        time.sleep(10)
    oo = open_orders(a)
    c.record("q1_orphan_after_margin_out", withdrawable_before=free,
             perp_after=c.core_snapshot(a)["perpAccountValue"],
             orphan_still_open=any(o.get("oid") == oid_left for o in oo),
             orphan_status=c.info_post({"type": "orderStatus", "user": a, "oid": oid_left}) if oid_left else None)
    if oid_left is not None and any(o.get("oid") == oid_left for o in oo):
        c.transact(deployer, a, "cancelByOid(uint32,uint64)", ["uint32", "uint64"], [asset, oid_left])
    c.record("q1_end", snapshot=c.core_snapshot(a), fills=c.info_post({"type": "userFills", "user": a})[:10])


def cmd_q2(args, state: dict) -> None:
    """Q2. agent-d is approved by contract A under a name. Then contract B tries to take the
    same address as its unnamed agent, and the deployer EOA tries approveAgent on it
    through the API, which answers synchronously. Where does agent-d end up?"""
    deployer = c.account("deployer")
    a, b = contract(state, "A"), contract(state, "B")
    agent_d = c.address_of("agent-d")
    asset, szd = perp_meta(PERP)

    contract_add_agent(state, "A", agent_d, "q2")
    r1 = wait_until("agent-d becomes A's agent", lambda: role(agent_d), lambda v: is_agent_of(v, a))
    c.record("q2_after_A", role=r1, extra_agents_A=c.info_post({"type": "extraAgents", "user": a}))

    contract_add_agent(state, "B", agent_d)
    time.sleep(12)
    c.record("q2_after_B", role=role(agent_d),
             extra_agents_A=c.info_post({"type": "extraAgents", "user": a}),
             extra_agents_B=c.info_post({"type": "extraAgents", "user": b}))

    nonce = get_timestamp_ms()
    action = {"type": "approveAgent", "agentAddress": agent_d, "agentName": "q2eoa", "nonce": nonce}
    sig = sign_agent(deployer, action, False)
    ex = c.exchange(deployer)
    resp = ex._post_action(action, sig, nonce)
    c.record("q2_eoa_approve_same_agent", response=resp, role_after=role(agent_d))

    # Which account does an agent-d order land on now? Both need margin to rest it.
    if float(c.core_snapshot(a)["perpAccountValue"]) < 5:
        c.transact(deployer, a, "usdClassTransfer(uint64,bool)", ["uint64", "bool"], [int(args.margin * 1e6), True])
        wait_until("A perp margin", lambda: c.core_snapshot(a), lambda s: float(s["perpAccountValue"]) >= 5)
    if float(c.core_snapshot(b)["perpAccountValue"]) < 5:
        if float(c.core_snapshot(b)["spotUSDC"]) < args.margin:
            c.transact(deployer, a, "spotSend(address,uint64,uint64)", ["address", "uint64", "uint64"],
                       [b, c.USDC_TOKEN, int(round(args.margin * 1e8))])
            wait_until("B spot funded", lambda: c.core_snapshot(b), lambda s: float(s["spotUSDC"]) >= args.margin)
        c.transact(deployer, b, "usdClassTransfer(uint64,bool)", ["uint64", "bool"], [int(args.margin * 1e6), True])
        wait_until("B perp margin", lambda: c.core_snapshot(b), lambda s: float(s["perpAccountValue"]) >= 5)
    m = mid(PERP)
    px = perp_px(m * 0.6, szd)
    sz = size_for(MIN_NOTIONAL, px, szd)
    resp = agent_order("agent-d", a, True, px, sz)
    oid = resting_oid(resp)
    time.sleep(3)
    on_a = any(o.get("oid") == oid for o in open_orders(a))
    on_b = any(o.get("oid") == oid for o in open_orders(b))
    c.record("q2_agent_d_order_lands_on", response=resp, oid=oid, on_A=on_a, on_B=on_b)
    for who, addr in (("A", a), ("B", b)):
        if oid is not None and (on_a if who == "A" else on_b):
            c.transact(deployer, addr, "cancelByOid(uint32,uint64)", ["uint32", "uint64"], [asset, oid])
    burn(state, agent_d, "used in the two-account test")


def abstraction(user: str) -> Any:
    return c.info_post({"type": "userAbstraction", "user": user})


def cmd_q7(_args, state: dict) -> None:
    """Q7, added 16 Sep. Which margin mode does a new contract account get, and who can
    change it? In unified mode spot USDC backs perp positions, so a pool that keeps its
    spare capital on spot would be lending it to the trader's margin. The docs say agents
    can switch the mode too; the enclave only signs `order` and `cancel`, so its keys can't,
    but a plain key can, and that is worth seeing once."""
    deployer = c.account("deployer")
    a, b = contract(state, "A"), contract(state, "B")
    if not c.core_user_exists(b):
        c.transact(deployer, a, "spotSend(address,uint64,uint64)", ["address", "uint64", "uint64"],
                   [b, c.USDC_TOKEN, 6 * 10**8])
        wait_until("B exists on HyperCore", lambda: c.core_user_exists(b), lambda v: v, timeout_s=30)
    for who, addr in (("A", a), ("B", b)):
        snap = c.core_snapshot(addr)
        c.record("q7_default_mode", who=who, abstraction=abstraction(addr), spot_usdc=snap["spotUSDC"],
                 perp_account_value=snap["perpAccountValue"], withdrawable=snap["withdrawable"],
                 precompile_margin=c.core_margin_summary(addr))

    # The contract sets separate balances on itself (action 16, value 1).
    c.transact(deployer, a, "setAbstraction(uint8)", ["uint8"], [1])
    mode = wait_until("A mode set to disabled", lambda: abstraction(a), lambda v: v == "disabled", timeout_s=30)
    c.record("q7_contract_sets_disabled", abstraction=mode)

    # A plain agent key on B tries to switch B to unified.
    agent_e = c.address_of("agent-e")
    contract_add_agent(state, "B", agent_e, "q7")
    r = wait_until("agent-e becomes B's agent", lambda: role(agent_e), lambda v: is_agent_of(v, b))
    ex = c.exchange(c.account("agent-e"), account_address=b)
    resp = ex.agent_set_abstraction("u")
    time.sleep(6)
    c.record("q7_agent_sets_unified", agent_role=r, response=resp, abstraction_after=abstraction(b))

    # Put B back to separate balances from the contract side, then retire agent-e. Both keys
    # are marked spent before anything is checked: agent-e has held B's agent slot, so it is
    # never used again whether or not the cleanup below lands.
    c.transact(deployer, b, "setAbstraction(uint8)", ["uint8"], [1])
    dead = fresh_address()
    contract_add_agent(state, "B", dead, "q7")
    burn(state, agent_e, "used in the margin-mode test; replaced on B")
    burn(state, dead, "keyless replacement for agent-e")
    mode_b = wait_until("B mode back to disabled", lambda: abstraction(b), lambda v: v == "disabled", timeout_s=30)
    agent_e_role = wait_until("agent-e replaced on B", lambda: role(agent_e), lambda v: not is_agent_of(v, b))
    c.record("q7_end", abstraction_b=mode_b, agent_e_role=agent_e_role)
    if mode_b != "disabled" or is_agent_of(agent_e_role, b):
        raise SystemExit("q7 cleanup did not land: B is not back to separate balances, or agent-e still "
                         "trades B. Check the results log before running anything else on B.")


def cmd_q8(_args, state: dict) -> None:
    """Q8, added 17 Sep. Does HyperCore take an address that already has an account as an
    agent? A stop replaces the agent with a fresh keyless address and skips candidates that
    exist; if every candidate had been funded in advance, it would use one anyway. Contract B
    tries 0x…dEaD, which exists on testnet and whose key nobody holds."""
    b = contract(state, "B")
    target = "0x000000000000000000000000000000000000dEaD"
    exists = c.core_user_exists(target)
    before = role(target)
    contract_add_agent(state, "B", target, "q8")
    time.sleep(12)
    c.record("q8_existing_user_as_agent", target=target, exists_on_core=exists, role_before=before,
             role_after=role(target), extra_agents_B=c.info_post({"type": "extraAgents", "user": b}))
    burn(state, target, "tried as an agent in q8")


def cmd_q5(args, state: dict) -> None:
    """Q5. The trader pays on HyperEVM with testnet USDC (ERC-20), so the contract can see
    who paid. The deployer stands in for the trader: it moves USDC from HyperCore to its
    EVM address (sendAsset to the USDC system address), approves A, calls pay(), and A
    bridges the payment into its own HyperCore balance."""
    deployer = c.account("deployer")
    a = contract(state, "A")
    units = int(round(args.usdc * 1e6))
    ex = c.exchange(deployer)
    before = c.erc20_balance(c.TESTNET_USDC_ERC20, deployer.address)
    resp = ex.send_asset(c.USDC_SYSTEM, "spot", "spot", c.spot_token_wire("USDC"), args.usdc)
    c.record("q5_core_to_evm_sent", response=resp)
    got = wait_until("EVM USDC arrives", lambda: c.erc20_balance(c.TESTNET_USDC_ERC20, deployer.address),
                     lambda v: v >= before + units)
    c.record("q5_core_to_evm_result", evm_usdc_before=before, evm_usdc_after=got)

    c.transact(deployer, c.TESTNET_USDC_ERC20, "approve(address,uint256)", ["address", "uint256"], [a, units])
    rcpt = c.transact(deployer, a, "pay(uint256)", ["uint256"], [units])
    paid = c.call_view(a, "paidBy(address)", ["address"], [deployer.address], ["uint256"])[0]
    c.record("q5_paid", tx=rcpt["transactionHash"], paid_by_deployer=paid,
             contract_evm_usdc=c.erc20_balance(c.TESTNET_USDC_ERC20, a), logs=len(rcpt["logs"]))

    spot_before = c.core_snapshot(a)["spotUSDC"]
    c.transact(deployer, a, "bridgeUsdcToCore(uint256)", ["uint256"], [units])
    snap = wait_until("A core USDC grows", lambda: c.core_snapshot(a),
                      lambda s: float(s["spotUSDC"]) > float(spot_before))
    c.record("q5_bridged", spot_before=spot_before, spot_after=snap["spotUSDC"],
             contract_evm_usdc=c.erc20_balance(c.TESTNET_USDC_ERC20, a))


SPOT_DEX = 2**32 - 1  # Circle's CoreDepositWallet: type(uint32).max is HyperCore spot
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def balances(user: str) -> dict:
    snap = c.core_snapshot(user)
    return {"spotUSDC": snap["spotUSDC"], "perpAccountValue": snap["perpAccountValue"]}


def transfer_events(rcpt: dict) -> list[dict]:
    """The Transfer logs of a receipt: who emitted them, from, to, amount."""
    out = []
    for log in rcpt["logs"]:
        if log["topics"] and log["topics"][0] == TRANSFER:
            out.append({"emitter": to_checksum_address(log["address"]),
                        "from": to_checksum_address("0x" + log["topics"][1][-40:]),
                        "to": to_checksum_address("0x" + log["topics"][2][-40:]),
                        "amount": int(log["data"], 16)})
        else:
            out.append({"emitter": to_checksum_address(log["address"]), "topic0": log["topics"][0] if log["topics"] else None})
    return out


def cmd_q5b(args, state: dict) -> None:
    """Q5b, added 17 Sep. q5 took 5 USDC off contract A's EVM balance and nothing reached A on
    HyperCore. Circle's CoreDepositWallet has two paths. For spot (destinationDex = max uint32)
    it emits Transfer(recipient -> 0x2000...) and HyperCore is expected to credit the recipient.
    For an enabled perp dex (dex 0 on testnet) it emits Transfer(wallet -> 0x2000...), which
    credits the wallet's own spot, and forwards to the recipient with a CoreWriter sendAsset.
    A is a contract in `disabled` mode (q7); the deployer is an EOA in `default` mode. The
    deployer sends --usdc down three paths: to itself on spot, to A on spot, to A on dex 0."""
    deployer = c.account("deployer")
    a = contract(state, "A")
    wallet = c.TESTNET_CORE_DEPOSIT_WALLET
    unit = int(round(args.usdc * 1e6))
    if not 0 < args.usdc <= 5:
        raise SystemExit("--usdc must be above 0 and at most 5 for this probe")
    probes = [
        ("eoa_default_spot", deployer.address, "deposit(uint256,uint32)", ["uint256", "uint32"],
         [unit, SPOT_DEX], "spotUSDC"),
        ("contract_disabled_spot", a, "depositFor(address,uint256,uint32)", ["address", "uint256", "uint32"],
         [a, unit, SPOT_DEX], "spotUSDC"),
        ("contract_disabled_dex0", a, "depositFor(address,uint256,uint32)", ["address", "uint256", "uint32"],
         [a, unit, 0], "perpAccountValue"),
    ]
    need = unit * len(probes)
    have = c.erc20_balance(c.TESTNET_USDC_ERC20, deployer.address)
    if have < need:
        resp = c.exchange(deployer).send_asset(c.USDC_SYSTEM, "spot", "spot", c.spot_token_wire("USDC"),
                                               (need - have) / 1e6)
        c.record("q5b_core_to_evm_sent", response=resp, usdc=(need - have) / 1e6)
        have = wait_until("deployer EVM USDC", lambda: c.erc20_balance(c.TESTNET_USDC_ERC20, deployer.address),
                          lambda v: v >= need)
        if have < need:
            raise SystemExit("the deployer's EVM USDC did not arrive; nothing was deposited")
    c.transact(deployer, c.TESTNET_USDC_ERC20, "approve(address,uint256)", ["address", "uint256"], [wallet, need])

    for name, who, sig, types, values, field in probes:
        before = balances(who)
        start_ms = get_timestamp_ms()
        rcpt = c.transact(deployer, wallet, sig, types, values)
        after = wait_until(f"{name}: {field} grows", lambda: balances(who),
                           lambda b: float(b[field]) > float(before[field]), timeout_s=60)
        c.record("q5b_probe", probe=name, recipient=who, mode=abstraction(who), tx=rcpt["transactionHash"],
                 block=int(rcpt["blockNumber"], 16), events=transfer_events(rcpt), before=before, after=after,
                 credited=float(after[field]) > float(before[field]),
                 ledger=c.info_post({"type": "userNonFundingLedgerUpdates", "user": who, "startTime": start_ms - 5000}))


def core_credits(user: str, start_ms: int) -> list:
    """Ledger rows that credit `user` from HyperEVM: from the USDC system address, or a
    forward from Circle's wallet."""
    rows = c.info_post({"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_ms})
    me = user.lower()
    return [r for r in rows if r["delta"].get("destination", "").lower() == me and (
        (r["delta"].get("type") == "spotTransfer" and r["delta"].get("user", "").lower() == c.USDC_SYSTEM.lower())
        or (r["delta"].get("type") == "send"
            and r["delta"].get("user", "").lower() == c.TESTNET_CORE_DEPOSIT_WALLET.lower()))]


def evm_usdc_for_deposits(deployer, units: int, step: str = "q5c") -> None:
    have = c.erc20_balance(c.TESTNET_USDC_ERC20, deployer.address)
    if have < units:
        usdc = (units - have) / 1e6
        resp = c.exchange(deployer).send_asset(c.USDC_SYSTEM, "spot", "spot", c.spot_token_wire("USDC"), usdc)
        c.record(f"{step}_core_to_evm_sent", response=resp, usdc=usdc)
        have = wait_until("deployer EVM USDC", lambda: c.erc20_balance(c.TESTNET_USDC_ERC20, deployer.address),
                          lambda v: v >= units)
        if have < units:
            raise SystemExit("the deployer's EVM USDC did not arrive; nothing was deposited")
    c.transact(deployer, c.TESTNET_USDC_ERC20, "approve(address,uint256)", ["address", "uint256"],
               [c.TESTNET_CORE_DEPOSIT_WALLET, units])


def spot_bridge_probe(deployer, name: str, who: str, units: int, step: str = "q5c") -> bool:
    """depositFor(who, units, spot) from the deployer; True if HyperCore credited `who`."""
    existed = c.core_user_exists(who)
    mode = abstraction(who) if existed else None
    before = balances(who) if existed else None
    start_ms = get_timestamp_ms() - 5000
    rcpt = c.transact(deployer, c.TESTNET_CORE_DEPOSIT_WALLET, "depositFor(address,uint256,uint32)",
                      ["address", "uint256", "uint32"], [who, units, SPOT_DEX])
    credits = wait_until(f"{name}: credited", lambda: core_credits(who, start_ms), bool, timeout_s=60)
    c.record(f"{step}_probe", probe=name, recipient=who, existed_before=existed, mode=mode, usdc=units / 1e6,
             tx=rcpt["transactionHash"], block=int(rcpt["blockNumber"], 16), events=transfer_events(rcpt),
             credited=bool(credits), credits=credits, before=before,
             after=balances(who) if c.core_user_exists(who) else None)
    return bool(credits)


def cmd_q5c(_args, state: dict) -> None:
    """Q5c, added 17 Sep. q5b: the bridge credited the deployer (an EOA in default mode) and
    left contract A (disabled mode) with nothing, on both paths. A fresh contract C separates
    the two causes. The deployer sends USDC down the spot path to C four times: before C has a
    HyperCore account, in default mode, after C sets itself to disabled (action 16, value 1),
    and after it sets itself to unified (value 2)."""
    deployer = c.account("deployer")
    if "C" not in state["contracts"]:
        addr, rcpt = c.deploy(deployer, "SpikeAccount", ["address"], [deployer.address])
        state["contracts"]["C"] = addr
        save_state(state)
        c.record("deployed", label="C", address=addr, tx=rcpt["transactionHash"],
                 gas_used=int(rcpt["gasUsed"], 16), core_user_exists=c.core_user_exists(addr))
    cc = contract(state, "C")
    evm_usdc_for_deposits(deployer, 5_000_000)
    results = {}
    if not c.core_user_exists(cc):
        results["new_account"] = spot_bridge_probe(deployer, "contract_new_spot", cc, 2_000_000)
    if not c.core_user_exists(cc):
        resp = c.exchange(deployer).spot_transfer(2.0, cc, c.spot_token_wire("USDC"))
        c.record("q5c_created_by_spot_send", response=resp)
        wait_until("C exists on HyperCore", lambda: c.core_user_exists(cc), bool, timeout_s=30)
    steps = [("default", "contract_default_spot", 1, "disabled"),
             ("disabled", "contract_disabled_spot", 2, "unifiedAccount"),
             ("unifiedAccount", "contract_unified_spot", None, None)]
    for mode, name, next_value, next_mode in steps:
        if abstraction(cc) != mode:
            continue
        results[mode] = spot_bridge_probe(deployer, name, cc, 1_000_000)
        if next_value is not None:
            c.transact(deployer, cc, "setAbstraction(uint8)", ["uint8"], [next_value])
            wait_until(f"C mode {next_mode}", lambda: abstraction(cc), lambda v: v == next_mode, timeout_s=30)
    c.record("q5c_end", contract=cc, results=results, mode=abstraction(cc))


def cmd_q5d(_args, state: dict) -> None:
    """Q5d, added 17 Sep. q5c: a contract got nothing in any mode, while q5b credited the
    deployer, which had also sent the transaction. Is the rule about code at the recipient, or
    about the recipient being the sender? The deployer bridges to the trader wallet, an EOA that
    sends nothing here: first while it has no HyperCore account, then, if that fails, after a
    spot transfer has created one."""
    deployer = c.account("deployer")
    trader = c.address_of("trader")
    evm_usdc_for_deposits(deployer, 3_000_000, step="q5d")
    results = {"new_eoa": spot_bridge_probe(deployer, "eoa_new_not_sender_spot", trader, 2_000_000, step="q5d")}
    if not results["new_eoa"]:
        if not c.core_user_exists(trader):
            resp = c.exchange(deployer).spot_transfer(1.0, trader, c.spot_token_wire("USDC"))
            c.record("q5d_created_by_spot_send", response=resp)
            wait_until("trader exists on HyperCore", lambda: c.core_user_exists(trader), bool, timeout_s=30)
        results["existing_eoa"] = spot_bridge_probe(deployer, "eoa_existing_not_sender_spot", trader, 1_000_000,
                                                    step="q5d")
    c.record("q5d_end", recipient=trader, results=results)


def dex0_bridge_probe(deployer, name: str, who: str, units: int, step: str = "q5e") -> bool:
    """depositFor(who, units, 0) from the deployer: Circle's wallet credits itself and forwards
    to `who`'s perp balance with a sendAsset. True if the forward reached `who`."""
    mode = abstraction(who)
    before = balances(who)
    start_ms = get_timestamp_ms() - 5000
    rcpt = c.transact(deployer, c.TESTNET_CORE_DEPOSIT_WALLET, "depositFor(address,uint256,uint32)",
                      ["address", "uint256", "uint32"], [who, units, 0])
    credits = wait_until(f"{name}: forwarded", lambda: core_credits(who, start_ms), bool, timeout_s=60)
    c.record(f"{step}_probe", probe=name, recipient=who, mode=mode, code_bytes=(len(c.rpc("eth_getCode", [who, "latest"])) - 2) // 2,
             usdc=units / 1e6, tx=rcpt["transactionHash"], block=int(rcpt["blockNumber"], 16),
             events=transfer_events(rcpt), credited=bool(credits), credits=credits, before=before, after=balances(who),
             wallet_ledger=c.info_post({"type": "userNonFundingLedgerUpdates", "user": c.TESTNET_CORE_DEPOSIT_WALLET,
                                        "startTime": start_ms}))
    return bool(credits)


def cmd_q5e(_args, state: dict) -> None:
    """Q5e, added 17 Sep. Circle's forwarding path (dex 0) reached an EOA that did not send the
    transaction (a CCTP deposit at 08:03 UTC) but not contract A in q5b. The deployer forwards
    one USDC each to the trader wallet (an EOA, the control), to contract C (unified since q5c)
    and to a fresh contract D, created by a spot transfer and left in default mode."""
    deployer = c.account("deployer")
    if "D" not in state["contracts"]:
        addr, rcpt = c.deploy(deployer, "SpikeAccount", ["address"], [deployer.address])
        state["contracts"]["D"] = addr
        save_state(state)
        c.record("deployed", label="D", address=addr, tx=rcpt["transactionHash"],
                 gas_used=int(rcpt["gasUsed"], 16), core_user_exists=c.core_user_exists(addr))
    d = contract(state, "D")
    if not c.core_user_exists(d):
        resp = c.exchange(deployer).spot_transfer(1.0, d, c.spot_token_wire("USDC"))
        c.record("q5e_created_by_spot_send", response=resp)
        wait_until("D exists on HyperCore", lambda: c.core_user_exists(d), bool, timeout_s=30)
    evm_usdc_for_deposits(deployer, 3_000_000, step="q5e")
    results = {}
    for name, who in (("eoa_not_sender_dex0", c.address_of("trader")),
                      ("contract_unified_dex0", contract(state, "C")),
                      ("contract_default_dex0", d)):
        results[name] = dex0_bridge_probe(deployer, name, who, 1_000_000)
    c.record("q5e_end", results=results)


def cmd_q5f(args, state: dict) -> None:
    """Q5f, added 17 Sep. Every forward in q5e carried 1 USDC and none arrived; the two that
    arrived for others at 07:52 and 08:03 UTC carried 2 and 998.59. The same forward with
    --usdc to the trader wallet and to contract D rules the amount in or out."""
    if not 1 < args.usdc <= 5:
        raise SystemExit("--usdc must be above 1 and at most 5 for this probe")
    deployer = c.account("deployer")
    units = int(round(args.usdc * 1e6))
    evm_usdc_for_deposits(deployer, 2 * units, step="q5f")
    results = {name: dex0_bridge_probe(deployer, name, who, units, step="q5f")
               for name, who in (("eoa_not_sender_dex0", c.address_of("trader")),
                                 ("contract_default_dex0", contract(state, "D")))}
    c.record("q5f_end", usdc=args.usdc, results=results)


SPOT_DEX = 2**32 - 1  # sendAsset's source or destination for the spot account


def dex_index(name: str) -> int:
    """A builder-deployed perp dex's index, from perpDexs (index 0 is the main dex, listed as None)."""
    for i, d in enumerate(c.info_post({"type": "perpDexs"})):
        if d and d.get("name") == name:
            return i
    raise SystemExit(f"no perp dex named {name!r} on testnet")


def dex_market(dex: str, coin: str) -> dict:
    """A market on a builder dex: its name as the dex's meta has it, its index there, szDecimals and
    the order asset id, 100000 + dex * 10000 + index."""
    d = dex_index(dex)
    meta = c.info_post({"type": "meta", "dex": dex})
    for i, a in enumerate(meta["universe"]):
        if a["name"] in (coin, f"{dex}:{coin}"):
            return {"dex": d, "name": a["name"], "index": i, "sz_decimals": a["szDecimals"],
                    "asset": 100_000 + d * 10_000 + i, "max_leverage": a.get("maxLeverage"),
                    "only_isolated": bool(a.get("onlyIsolated")), "delisted": bool(a.get("isDelisted"))}
    raise SystemExit(f"no {coin} on dex {dex}")


def dex_asset(dex: str, coin: str) -> tuple[int, int, int]:
    """(index on the dex, szDecimals, order asset id)."""
    m = dex_market(dex, coin)
    return m["index"], m["sz_decimals"], m["asset"]


def try_precompile(name: str, types, args, out):
    try:
        return list(c.precompile(name, types, args, out))
    except Exception as exc:
        return f"failed: {str(exc)[:120]}"


def cmd_q9(args, state: dict) -> None:
    """Q9, added 21 Oct 2026, before any contract change for HIP-3 (the CTO's 1/8/1):
    1. Does a contract move its capital to a builder-deployed perp dex (sendAsset, action 13), from
       spot and from the main dex, and does accountMarginSummary(dex, account) read it?
    2. Does CoreWriter's limit order (action 1) take that dex's asset id, 100000 + dex * 10000 + i,
       and its cancel (action 10) the same id?
    3. Which index do markPx, perpAssetInfo and position2 take for such an asset: the order id, or
       the id less 100000?
    Contract B, the deployer its owner. A resting buy far under the mark that never fills; the
    capital goes back to spot at the end. Everything is recorded in spike/results."""
    deployer = c.account("deployer")
    b = contract(state, args.label)
    d = dex_index(args.dex)
    i, sz_dec, asset = dex_asset(args.dex, args.coin)
    wei = int(round(args.usdc * 1e8))
    c.record("q9_asset", dex=args.dex, dex_index=d, coin=args.coin, index=i, asset_id=asset, sz_decimals=sz_dec)

    def margins() -> dict:
        return {"main": c.core_margin_summary(b, 0), "dex": c.core_margin_summary(b, d),
                "info_dex": c.info_post({"type": "clearinghouseState", "user": b, "dex": args.dex}).get("marginSummary"),
                "spot": c.core_snapshot(b)["spotUSDC"]}

    m0 = margins()
    c.record("q9_start", **m0)
    send = "sendAsset(address,address,uint32,uint32,uint64,uint64)"
    send_t = ["address", "address", "uint32", "uint32", "uint64", "uint64"]
    zero = "0x0000000000000000000000000000000000000000"
    c.transact(deployer, b, send, send_t, [b, zero, SPOT_DEX, d, c.USDC_TOKEN, wei])
    m1 = wait_until("capital on the dex from spot", margins, lambda m: m["dex"]["accountValue"] > m0["dex"]["accountValue"])
    from_spot = m1["dex"]["accountValue"] > m0["dex"]["accountValue"]
    c.record("q9_spot_to_dex", taken=from_spot, **m1)
    if not from_spot:
        c.transact(deployer, b, "usdClassTransfer(uint64,bool)", ["uint64", "bool"], [int(round(args.usdc * 1e6)), True])
        wait_until("capital on the main dex", margins, lambda m: m["main"]["accountValue"] > m0["main"]["accountValue"])
        c.transact(deployer, b, send, send_t, [b, zero, 0, d, c.USDC_TOKEN, wei])
        m1 = wait_until("capital on the dex from the main dex", margins,
                        lambda m: m["dex"]["accountValue"] > m0["dex"]["accountValue"])
        c.record("q9_main_to_dex", taken=m1["dex"]["accountValue"] > m0["dex"]["accountValue"], **m1)

    mark = float(c.info_post({"type": "allMids", "dex": args.dex}).get(f"{args.dex}:{args.coin}")
                 or c.info_post({"type": "allMids", "dex": args.dex}).get(args.coin))
    px = perp_px(mark * 0.5, sz_dec)
    sz = size_for(12.0, px, sz_dec)
    cloid = int.from_bytes(keccak(text=f"q9-{time.time()}")[:16], "big")
    c.transact(deployer, b, "placeLimitOrder(uint32,bool,uint64,uint64,bool,uint8,uint128)",
               ["uint32", "bool", "uint64", "uint64", "bool", "uint8", "uint128"],
               [asset, True, int(round(px * 1e8)), int(round(sz * 1e8)), False, 1, cloid])

    def resting() -> list:
        return [o for o in c.info_post({"type": "frontendOpenOrders", "user": b, "dex": args.dex})
                if str(o.get("coin", "")).endswith(args.coin)]

    orders = wait_until("the order rests on the dex", resting, lambda o: bool(o))
    c.record("q9_order", asset_id=asset, px=px, sz=sz, rests=bool(orders), orders=orders)
    c.record("q9_precompiles", asset_id=asset,
             markPx_order_id=try_precompile("markPx", ["uint32"], [asset], ["uint64"]),
             markPx_less_100000=try_precompile("markPx", ["uint32"], [asset - 100_000], ["uint64"]),
             position2_order_id=try_precompile("position2", ["address", "uint32"], [b, asset], ["int64", "uint64", "int64", "uint32", "bool"]),
             position2_less_100000=try_precompile("position2", ["address", "uint32"], [b, asset - 100_000], ["int64", "uint64", "int64", "uint32", "bool"]),
             # One struct with a string in it: a single dynamic tuple, not five words.
             perpAssetInfo_order_id=try_precompile("perpAssetInfo", ["uint32"], [asset], ["(string,uint32,uint8,uint8,bool)"]),
             perpAssetInfo_less_100000=try_precompile("perpAssetInfo", ["uint32"], [asset - 100_000], ["(string,uint32,uint8,uint8,bool)"]),
             oraclePx_order_id=try_precompile("oraclePx", ["uint32"], [asset], ["uint64"]),
             oraclePx_less_100000=try_precompile("oraclePx", ["uint32"], [asset - 100_000], ["uint64"]),
             info_mark=mark)
    if orders:
        oid = int(orders[0]["oid"])
        c.transact(deployer, b, "cancelByOid(uint32,uint64)", ["uint32", "uint64"], [asset, oid])
        gone = wait_until("the order is cancelled by the same id", resting, lambda o: not o)
        c.record("q9_cancel", asset_id=asset, oid=oid, cancelled=not gone)

    back = margins()
    value = int(back["dex"]["accountValue"])
    if value > 0:
        c.transact(deployer, b, send, send_t, [b, zero, d, SPOT_DEX, c.USDC_TOKEN, value * 100])
        back = wait_until("capital back on spot", margins, lambda m: m["dex"]["accountValue"] == 0)
    c.record("q9_end", **back)


SEND_ASSET = "sendAsset(address,address,uint32,uint32,uint64,uint64)"
SEND_ASSET_T = ["address", "address", "uint32", "uint32", "uint64", "uint64"]
PLACE = "placeLimitOrder(uint32,bool,uint64,uint64,bool,uint8,uint128)"
PLACE_T = ["uint32", "bool", "uint64", "uint64", "bool", "uint8", "uint128"]
IOC = 3
ZERO = "0x0000000000000000000000000000000000000000"


def tpsl_order(name: str, is_buy: bool, trigger: float, tpsl: str, sz_decimals: int) -> dict:
    """One position TP/SL order in the shape gateway/protect.order_wire sends: size 0 (the whole
    position), reduce-only, a market trigger, its limit 10 % past the trigger."""
    limit = perp_px(trigger * (1.1 if is_buy else 0.9), sz_decimals)
    return {"coin": name, "is_buy": is_buy, "sz": 0.0, "limit_px": limit,
            "order_type": {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": tpsl}},
            "reduce_only": True}


def cmd_q10(args, state: dict) -> None:
    """Q10, added 21 Oct 2026 for H-5 (the CTO's 1/7/9), after q9: does a contract account trade a
    HIP-3 market from start to end the way a pool's account will?
    1. contract B fills an IOC buy on the dex by the market's order id (CoreWriter action 1);
    2. a named agent of B, approved by B, places the position's stop and take on the dex, grouped
       positionTpsl, in the gateway's shape; is the SDK's asset id for the market the contract's,
       and do both rest on the dex?
    3. B closes the position itself, reduce-only IOC at the mark less 5 %, as
       AccountOps.closePositions does; what is left of the stop and the take after that?
    4. B cancels what is left and sends the dex's whole value back to spot.
    The agent's key is made here and lives only in memory. At the end a keyless address takes the
    agent's name and both are burned. B's unnamed agent is not touched: a named one has its own slot.
    About 11 USDC of exposure for a minute or two; whatever fails, the end still closes, cancels and
    sends the capital back. --dry-run reads it all and sends nothing."""
    if args.usdc > MAX_USDC_PER_STEP:
        raise SystemExit("amount above the script cap")
    b = contract(state, args.label)
    m = dex_market(args.dex, args.coin)
    d, asset, name, szd = m["dex"], m["asset"], m["name"], m["sz_decimals"]
    wei = int(round(args.usdc * 1e8))

    def margins() -> dict:
        return {"dex": c.core_margin_summary(b, d), "spot": c.core_snapshot(b)["spotUSDC"]}

    def top() -> dict:
        bids, asks = c.info_post({"type": "l2Book", "coin": name})["levels"]
        return {"bid": float(bids[0]["px"]) if bids else None, "ask": float(asks[0]["px"]) if asks else None}

    def mark() -> float:
        mids = c.info_post({"type": "allMids", "dex": args.dex})
        return float(mids.get(name) or mids[args.coin])

    def position() -> float:
        st = c.info_post({"type": "clearinghouseState", "user": b, "dex": args.dex})
        for ap in st.get("assetPositions", []):
            if ap["position"]["coin"] == name:
                return float(ap["position"]["szi"])
        return 0.0

    def orders() -> list:
        return [o for o in c.info_post({"type": "frontendOpenOrders", "user": b, "dex": args.dex})
                if o.get("coin") == name]

    def agents() -> Any:
        return c.info_post({"type": "extraAgents", "user": b})

    start = margins()
    book = top()
    c.record("q10_start", market=m, book=book, mark=mark(), agents=agents(), position=position(),
             orders=orders(), **start)
    why = ("delisted" if m["delisted"] else "isolated margin only" if m["only_isolated"]
           else "nobody sells on the book" if book["ask"] is None else None)
    if why:
        raise SystemExit(f"{name}: {why}; nothing was sent")
    if float(start["spot"]) < args.usdc:
        raise SystemExit(f"{args.label} has {start['spot']} USDC on spot, needs {args.usdc}: "
                         f"run.py fund --to {args.label} --usdc {args.usdc}; nothing was sent")
    if args.dry_run:
        px = perp_px(book["ask"] * 1.01, szd)
        c.record("q10_dry_run", asset_id=asset, buy_px=px, buy_sz=size_for(MIN_NOTIONAL, px, szd),
                 usdc=args.usdc, sent="nothing")
        return

    deployer = c.account("deployer")
    c.transact(deployer, b, SEND_ASSET, SEND_ASSET_T, [b, ZERO, SPOT_DEX, d, c.USDC_TOKEN, wei])
    moved = wait_until("capital on the dex", margins,
                       lambda x: x["dex"]["accountValue"] > start["dex"]["accountValue"])
    c.record("q10_spot_to_dex", taken=moved["dex"]["accountValue"] > start["dex"]["accountValue"], **moved)
    agent, agent_name = None, ""
    try:
        # 1. the contract fills a buy at the best ask and a little over it
        px = perp_px(top()["ask"] * 1.01, szd)
        sz = size_for(MIN_NOTIONAL, px, szd)
        c.transact(deployer, b, PLACE, PLACE_T, [asset, True, int(round(px * 1e8)), int(round(sz * 1e8)), False, IOC, 0])
        pos = wait_until("a position on the dex", position, lambda p: p != 0.0, timeout_s=30)
        c.record("q10_fill", asset_id=asset, px=px, sz=sz, position=pos,
                 position2=try_precompile("position2", ["address", "uint32"], [b, asset - 100_000],
                                          ["int64", "uint64", "int64", "uint32", "bool"]),
                 fills=[f for f in c.info_post({"type": "userFills", "user": b}) if f.get("coin") == name][:5],
                 **margins())
        if pos == 0.0:
            return

        # 2. a named agent of B places the stop and the take
        agent = Account.create()
        agent_name = f"q10 valid_until {get_timestamp_ms() + 86_400_000}"
        contract_add_agent(state, args.label, agent.address, agent_name)
        r = wait_until("the named agent is B's", lambda: role(agent.address), lambda v: is_agent_of(v, b))
        c.record("q10_agent", agent=agent.address, name=agent_name, role=r, agents=agents())
        ex = c.exchange(agent, account_address=b, perp_dexs=["", args.dex])
        mk = mark()
        stop, take = perp_px(mk * 0.97, szd), perp_px(mk * 1.03, szd)
        resp = ex.bulk_orders([tpsl_order(name, False, stop, "sl", szd), tpsl_order(name, False, take, "tp", szd)],
                              grouping="positionTpsl")
        rest = wait_until("the stop and the take rest on the dex", orders, lambda o: len(o) >= 2, timeout_s=20)
        sdk_asset = ex.info.name_to_asset(name)
        c.record("q10_stop_and_take", sdk_asset=sdk_asset, same_id=sdk_asset == asset, mark=mk, stop=stop,
                 take=take, response=resp, orders=rest, main_dex_orders=open_orders(b))

        # 3. the contract closes the position, the way AccountOps.closePositions prices it
        size_now = abs(position())
        close_px = perp_px(mark() * 0.95, szd)
        c.transact(deployer, b, PLACE, PLACE_T,
                   [asset, False, int(round(close_px * 1e8)), int(round(size_now * 1e8)), True, IOC, 0])
        left = wait_until("the position closed by the contract", position, lambda p: p == 0.0, timeout_s=30)
        time.sleep(4)
        c.record("q10_close_by_contract", px=close_px, sz=size_now, position_after=left,
                 orders_after=orders(), **margins())
    finally:
        # 4. whatever happened above: nothing open, nothing resting, the agent burned, capital on spot
        left = position()
        if left != 0.0:
            px = perp_px(mark() * (0.95 if left > 0 else 1.05), szd)
            c.transact(deployer, b, PLACE, PLACE_T,
                       [asset, left < 0, int(round(px * 1e8)), int(round(abs(left) * 1e8)), True, IOC, 0])
            left = wait_until("nothing open on the dex", position, lambda p: p == 0.0, timeout_s=30)
        cancelled = []
        for o in orders():
            c.transact(deployer, b, "cancelByOid(uint32,uint64)", ["uint32", "uint64"], [asset, int(o["oid"])])
            cancelled.append(o)
        if cancelled:
            wait_until("nothing resting on the dex", orders, lambda o: not o)
        if agent is not None:
            dead = fresh_address()
            contract_add_agent(state, args.label, dead, agent_name)
            wait_until("a keyless address takes the agent's name", lambda: role(dead), lambda v: is_agent_of(v, b))
            burn(state, agent.address, "q10's named agent; its name went to a keyless address")
            burn(state, dead, "keyless; nobody can sign for it")
        back = margins()
        value = int(back["dex"]["accountValue"])
        if value > 0:
            c.transact(deployer, b, SEND_ASSET, SEND_ASSET_T, [b, ZERO, d, SPOT_DEX, c.USDC_TOKEN, value * 100])
            back = wait_until("capital back on spot", margins, lambda x: x["dex"]["accountValue"] == 0)
        c.record("q10_end", position=left, cancelled=cancelled, orders=orders(), agents=agents(),
                 agent_role=role(agent.address) if agent is not None else None, **back)


PROBE_ADDRESS = "0x00000000000000000000000000000000005709a5"  # nobody's; only eth_call puts code there
USED_FIELDS = ("violation", "cut", "cancels", "closes", "equity", "total")


def live_markets(dex: str) -> list[int]:
    """The order ids of a dex's markets that are not delisted, in the meta's order."""
    d = dex_index(dex) if dex else 0
    meta = c.info_post({"type": "meta", "dex": dex} if dex else {"type": "meta"})
    return [(100_000 + d * 10_000 + i if dex else i) for i, a in enumerate(meta["universe"]) if not a.get("isDelisted")]


def cmd_stopgas(args, _state: dict) -> None:
    """The worst stop of a ruled account on HyperEVM itself (audit window 2, В6-3): src/spike's
    StopGasProbe makes every precompile read and CoreWriter action of ChallengeAccount.breach for
    sixteen assets in the rules, sixteen extra and thirty-two cancels, each position taken as open.
    A read-only eth_call with the probe's code put at an address nobody holds: nothing is sent.
    test/spike/StopGas.t.sol measures the rest, the account's own work, on the simulator."""
    art = json.loads((c.REPO_ROOT / "out" / "StopGasProbe.sol" / "StopGasProbe.json").read_text())
    code = art["deployedBytecode"]["object"]
    d = dex_index(args.dex) if args.dex else 0
    markets = live_markets(args.dex)
    if len(markets) < args.rules + args.extra:
        raise SystemExit(f"dex {args.dex or 'main'} has {len(markets)} live markets, fewer than {args.rules + args.extra}")
    rules, extra = markets[:args.rules], markets[args.rules:args.rules + args.extra]
    sig = "run(address,uint32,uint32[],uint32[],uint256)"
    data = c.encode_call(sig, ["address", "uint32", "uint32[]", "uint32[]", "uint256"],
                         [PROBE_ADDRESS, d, rules, extra, args.cancels])
    call = {"to": PROBE_ADDRESS, "data": "0x" + data.hex()}
    override = {PROBE_ADDRESS: {"code": code}}
    out = c.rpc("eth_call", [{**call, "gas": hex(30_000_000)}, "latest", override])
    used = dict(zip(USED_FIELDS, eth_abi.decode(["(uint256,uint256,uint256,uint256,uint256,uint256)"],
                                                 bytes.fromhex(out[2:]))[0]))
    estimate = None
    if args.estimate:
        # keeper.fit cuts a call by eth_estimateGas, so the node has to answer a call over a small
        # block's 2 million with a number and not refuse it (audit window 2, the CTO, 22 October 2026).
        try:
            estimate = int(c.rpc("eth_estimateGas", [call, "latest", override]), 16)
        except Exception as exc:
            estimate = f"refused: {str(exc)[:200]}"
    c.record("stopgas", dex=args.dex or "main", dex_index=d, rules=rules, extra=extra, cancels_named=args.cancels,
             gas=used, estimate=estimate)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    g = sub.add_parser("gas")
    g.add_argument("--buy-hype", type=float, default=0.5)
    g.add_argument("--to-evm", type=float, default=0.3)
    sub.add_parser("deploy")
    f = sub.add_parser("fund")
    f.add_argument("--to", default="A")
    f.add_argument("--usdc", type=float, default=40.0)
    q3 = sub.add_parser("q3")
    q3.add_argument("--usdc", type=float, default=10.0)
    sub.add_parser("q4")
    q1 = sub.add_parser("q1")
    q1.add_argument("--margin", type=float, default=15.0)
    q2 = sub.add_parser("q2")
    q2.add_argument("--margin", type=float, default=5.0)
    sub.add_parser("q7")
    sub.add_parser("q8")
    q9 = sub.add_parser("q9", help="HIP-3: capital on a builder dex, its asset ids through CoreWriter and the precompiles")
    q9.add_argument("--dex", required=True, help="the builder-deployed perp dex's name, as perpDexs lists it")
    q9.add_argument("--coin", required=True, help="a coin on that dex")
    q9.add_argument("--usdc", type=float, default=15.0, help="capital to move there and back")
    q9.add_argument("--label", default="B", help="the spike contract")
    q10 = sub.add_parser("q10", help="HIP-3 trading: a fill, the stop and the take, the contract's close, back to spot")
    q10.add_argument("--dex", required=True, help="the builder-deployed perp dex's name, as perpDexs lists it")
    q10.add_argument("--coin", required=True, help="a cross-margin market on that dex")
    q10.add_argument("--usdc", type=float, default=15.0, help="capital to move there and back")
    q10.add_argument("--label", default="B", help="the spike contract")
    q10.add_argument("--dry-run", action="store_true", help="read the market, the book and the account; send nothing")
    sg = sub.add_parser("stopgas", help="the worst stop's precompile reads and CoreWriter actions, by a read-only eth_call")
    sg.add_argument("--dex", default="", help="the account's builder dex, by name; the main dex if left out")
    sg.add_argument("--rules", type=int, default=16, help="assets in the rules")
    sg.add_argument("--extra", type=int, default=16, help="extra assets")
    sg.add_argument("--cancels", type=int, default=32)
    sg.add_argument("--estimate", action="store_true", help="also ask eth_estimateGas for the same call")
    q5 = sub.add_parser("q5")
    q5.add_argument("--usdc", type=float, default=5.0)
    q5b = sub.add_parser("q5b")
    q5b.add_argument("--usdc", type=float, default=1.0)
    sub.add_parser("q5c")
    sub.add_parser("q5d")
    sub.add_parser("q5e")
    q5f = sub.add_parser("q5f")
    q5f.add_argument("--usdc", type=float, default=3.0)
    args = p.parse_args()

    c.assert_testnet()
    state = load_state()
    handlers = {"status": cmd_status, "gas": cmd_gas, "deploy": cmd_deploy, "fund": cmd_fund,
                "q3": cmd_q3, "q4": cmd_q4, "q1": cmd_q1, "q2": cmd_q2, "q5": cmd_q5, "q5b": cmd_q5b, "q5c": cmd_q5c, "q5d": cmd_q5d, "q5e": cmd_q5e, "q5f": cmd_q5f, "q7": cmd_q7,
                "q8": cmd_q8, "q9": cmd_q9, "q10": cmd_q10, "stopgas": cmd_stopgas}
    handlers[args.cmd](args, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
