"""Live testnet spike: one subcommand per question, run in the order below.

    spike/.venv/bin/python spike/run.py status
    spike/.venv/bin/python spike/run.py gas --buy-hype 0.5 --to-evm 0.3
    spike/.venv/bin/python spike/run.py deploy
    spike/.venv/bin/python spike/run.py fund --usdc 40          # Q3a: EOA -> new contract account
    spike/.venv/bin/python spike/run.py q3                      # Q3b: contract -> contract -> perp -> back
    spike/.venv/bin/python spike/run.py q4                      # equity: precompile vs info API
    spike/.venv/bin/python spike/run.py q1                      # agent replacement + resting orders (+Q6)
    spike/.venv/bin/python spike/run.py q2                      # one agent address, two accounts
    spike/.venv/bin/python spike/run.py q5 --usdc 5             # trader pays on HyperEVM

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

from eth_utils import to_checksum_address
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


def cmd_gas(args, _state: dict) -> None:
    """Buy a little testnet HYPE on HyperCore spot and move part of it to the deployer's
    HyperEVM address, which needs it for gas. Mock funds only."""
    deployer = c.account("deployer")
    ex = c.exchange(deployer)
    spot = c.info_post({"type": "spotMeta"})
    tokens = {t["index"]: t for t in spot["tokens"]}
    pair = next(p for p in spot["universe"] if [tokens[x]["name"] for x in p["tokens"]] == ["HYPE", "USDC"])
    coin = pair["name"]
    px = float(c.info_post({"type": "allMids"})[coin])
    if args.buy_hype * px > 30:
        raise SystemExit(f"refusing: {args.buy_hype} HYPE at {px} is more than 30 USDC")
    resp = ex.market_open(coin, True, args.buy_hype, None, 0.05)
    c.record("gas_buy_hype", pair=coin, mid=px, size=args.buy_hype, response=resp)
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
    resp = c.exchange(deployer).spot_transfer(args.usdc, a, c.spot_token_wire("USDC"))
    c.record("fund_sent", to=a, usdc=args.usdc, response=resp, exists_before=c.core_user_exists(a))
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
    q5 = sub.add_parser("q5")
    q5.add_argument("--usdc", type=float, default=5.0)
    args = p.parse_args()

    c.assert_testnet()
    state = load_state()
    handlers = {"status": cmd_status, "gas": cmd_gas, "deploy": cmd_deploy, "fund": cmd_fund,
                "q3": cmd_q3, "q4": cmd_q4, "q1": cmd_q1, "q2": cmd_q2, "q5": cmd_q5}
    handlers[args.cmd](args, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
