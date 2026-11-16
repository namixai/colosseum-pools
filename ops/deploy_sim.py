"""Deploys the simulated pool's contracts, on testnet unless mainnet is named, and opens a pool on them
(docs/SIM-POOL.md).

    spike/.venv/bin/python -m ops.deploy_sim deploy --label sim1 --filler 0x.. --checker 0x.. \\
        --closer 0x..                                                                                  # the plan
    ... deploy ... --send                                                                              # sends
    spike/.venv/bin/python -m ops.deploy_sim pool --deployment sim1 \\
        --plan deployments/plans/sim-first-pool.json --creator creator                                  # the plan
    ... pool ... --send                                                                                # sends
    spike/.venv/bin/python -m ops.deploy_sim check --plan deployments/plans/sim-first-pool.json        # factory says
    spike/.venv/bin/python -m ops.deploy_sim codes --pool 0x.. \\
        --plan deployments/plans/sim-first-pool.json --creator creator [--send]                        # its codes
    spike/.venv/bin/python -m ops.deploy_sim close --pool 0x.. --key closer [--send]                   # stop sales
    spike/.venv/bin/python -m ops.deploy_sim finish --pool 0x.. --key creator [--exam 0x..] [--send]   # pay out
    spike/.venv/bin/python -m ops.deploy_sim request --pool 0x.. --key creator --amount 100 [--send]   # ask
    spike/.venv/bin/python -m ops.deploy_sim withdraw --pool 0x.. --key creator [--send]               # a week on
    spike/.venv/bin/python -m ops.deploy_sim cancel --pool 0x.. --key closer [--send]                  # cancel it
    spike/.venv/bin/python -m ops.deploy_sim approver --pool 0x.. --key creator --address 0x.. [--send] # name it
    spike/.venv/bin/python -m ops.deploy_sim approve --pool 0x.. --key approver --ids 3,4 [--send]     # payouts

`deploy` puts PaperExam (the implementation every exam clones), SimPool (every pool's), a TermsBook
and the SimPoolFactory on HyperEVM, names the filler, the checker, the closer (the platform's
key that alone may close a pool before its 6 months, 184 days; four keys, none of them the operator, who
opens our pools) and the platform's recipient, and lists the assets with their leverage caps from ops/leverage.py (HYPE is left out:
the platform's decision of 11 November 2026), each at its perp index on the network (ops/sim_network.py). It
hands the factory to our Safe last (--safe; setOperator, read before and after, the Safe's threshold and owners read and shown first; needed on mainnet, where the
deployer's key must not stay its operator), and writes deployments/<network>-<label>.json; on testnet with the same keeper nodes as demo3, on mainnet with the
ones --keeper-nodes names (a JSON file, as a record's `keeper_nodes`: a node of the keeper's own, not the
gateway's), so the host's keeper unit can run on it.

`buy` approves a line's price and buys an exam with a code from the trader's key, for --measure in
ops/sim_keeper.py; it prints the exam.

`codes` sets a pool's codes from the plan's `codes` (setCode, from the creator's key): each a trading
group's code, the address its owner gave and its share, 0 or 4 000 to 6 000 bps. Every sale names a
code, so a pool sells nothing until one is set. A code a sale has named keeps its partner, and its share
can only rise: one that matches is skipped, a raise is sent, anything else is refused before anything is sent.

`close` closes a pool and `finish` pays out a closed one, from the named key. Without --send each only
reads the pool and says what it would send, from which key, or why the pool would refuse: before its
184 days only the factory's closer closes it; `finish` (the creator or the closer) waits until nothing
runs, nothing is owed and no stop can be lifted, and a week after the close; with --exam it says until
when that exam runs.

`request`, `withdraw` and `cancel` are the creator's withdrawal, which waits a week. `request` asks for
--amount USDC, at most what the pool lets go now; `withdraw` carries out the standing request from a week
after it, for three days, and pays the less of what was asked and what the pool lets go then; `cancel`
cancels it, from the creator's key or the closer's.

`approver` names the pool's approver of payouts (from the creator's key; zero for none) and `approve`
approves waiting payouts from it: an approved payout goes out 72 hours after its claim, one not
approved 7 days after. Without --send `approve` reads each payout and says whose it is, how much,
from when it goes out with the approval and from when without, or why the pool would refuse it. Like `close` and `finish`, without --send each reads
the pool and says what it would send, from which key, or why the pool would refuse.

`check` asks SimPoolFactory.check whether a plan could open a pool, by eth_call, sending nothing and reading no key:
with --deployment, the factory it names; without, the factory and PaperExam of this checkout, put in the call by a
state override on the network's precompiles, so a plan is checked before any factory is deployed.

`pool` publishes a pool's terms in the TermsBook, approves the USDC for the two locked payouts and
calls `create` with 1 HYPE, from the creator's key, then sets the plan's codes as `codes` does. The
plan file gives the menu, both stages' rules and terms, the fund window, the book's ceiling, the codes
and the terms' text.

Without --send nothing is sent and no key is read: each command prints what it would do.

With --signer ADDRESS (pool, codes, close, finish, request, withdraw, cancel, approver, approve) the call is
from an address that signs elsewhere: our pool's creator is a Safe with two signatures, its approver of payouts
a Ledger. The command reads the chain as that address, checks as it would for a key, and prints the calls in
order as the Safe's Transaction Builder takes them (to, value, data); it sends nothing and reads no key. With it,
the transactions go out from the named keys.

The network is testnet unless both COLOSSEUM_NETWORK=mainnet and --network mainnet name mainnet
(ops/sim_network.py). On mainnet every command reads and prints as on testnet, and --signer prints the calls for
the Safe or the Ledger; --send there also asks the person at the terminal to type the confirmation phrase, wants a
mainnet key directory, reads the network's meta to hold each asset's index, and `pool` refuses a plan written for
testnet. Mainnet only after the audit and by the owner's hand.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import sim_network as sn
from ops.deploy_testnet import big_blocks, deploy_contract, git_head, mark_incomplete, save
from ops.leverage import ASSET_LEVERAGE_X100
from spike.hlspike import common as c

ROOT = pathlib.Path(__file__).resolve().parents[1]
# What a finished deployment's record says: the one status `deployments.load` takes, so that `pool`, `buy`, the
# keeper and the checker can read the record `deploy` wrote.
DONE = "complete"
CREATION_FEE = 10**18  # SimPoolFactory.CREATION_FEE; the tests hold it against the source
PAYOUT_BPS = 800
LOCKED_PAYOUTS = 2
RULES_T = "(uint16,uint16,uint32,uint32[],uint32[])"
TERMS_T = "(uint64,uint16[],uint32,uint32,uint16,uint32,uint16,uint16)"
TIER_T = "(uint16,uint16,uint8)"
PLAN_T = (f"((uint64,uint64)[],{RULES_T},{TERMS_T},{RULES_T},{TERMS_T},uint32,bool,uint16,bytes32,{TIER_T}[],uint8,"
          "uint256,uint32)")
# A plan file without `payouts_per_day` is a pool of two a day, the first pool's (CFO, 13 November).
PAYOUTS_PER_DAY = 2
# SimPoolFactory.MIN_PARTNER_BPS, MAX_PARTNER_BPS and MAX_CODES; the tests hold them against the source.
MIN_PARTNER_BPS, MAX_PARTNER_BPS, MAX_CODES = 4000, 6000, 32
SET_CODE = "setCode(bytes32,address,uint16)"
CODE_OF = ["address", "uint16", "bool", "bool", "uint256"]
ZERO = "0x" + "00" * 20

# Each asset's perp index on the network selected now (ops/sim_network.py), read at each lookup.
PLATFORM_ASSETS = sn.Assets(lambda: c.NETWORK)


def asset_list(names: list[str]) -> list[tuple[str, int, int]]:
    """Each named asset's id and leverage cap, for setAsset. Refuses one without a cap."""
    out, table = [], sn.sim(c.NETWORK)
    for name in names:
        idx = table.index(name)
        cap = ASSET_LEVERAGE_X100.get(name)
        if not cap:
            raise SystemExit(f"{name} has no leverage cap in ops/leverage.py")
        out.append((name, idx, cap))
    return out


def usdc_units(amount) -> int:
    """USDC (6 decimals) of an amount written in dollars, rounded."""
    return int(round(amount * 10**6))


def _rules(r: dict) -> tuple:
    return (r["daily_loss_bps"], r["max_drawdown_bps"], r["max_leverage_x100"],
            [PLATFORM_ASSETS[a] for a in r["assets"]], r.get("asset_leverage_x100", []))


def _terms(t: dict) -> tuple:
    return (0, t["targets_bps"], t["step_duration_s"], t["max_idle_s"], t.get("min_trading_days", 0),
            t["cost_ppm"], t["fill_delay"], t["fill_window"])


def plan_of(raw: dict, terms_hash: bytes) -> tuple:
    """The SimPlan tuple of a plan file, the terms named by their hash."""
    # round, not int: 0.29 * 100 is 28.999... (review of cp#263)
    menu = [(usdc_units(m["size_usdc"]), usdc_units(m["price_usdc"])) for m in raw["menu"]]
    return (menu, _rules(raw["exam_rules"]), _terms(raw["exam"]), _rules(raw["funded_rules"]), _terms(raw["funded"]),
            raw["fund_window_s"], raw["from_book"], raw["max_worse_bps"], terms_hash,
            [_tier(raw, m) for m in raw["menu"]], raw.get("payouts_per_day", PAYOUTS_PER_DAY), locked_of(raw),
            on_of(raw))


def _tier(raw: dict, line: dict) -> tuple:
    """A menu line's tier (SimTier): its own `max_drawdown_bps`, `target_bps` and `drawdown_kind` (0 from the start, 1
    trailing to it), or, for a line without them, the plan's own, which both stages must then share."""
    if "max_drawdown_bps" in line:
        return (line["max_drawdown_bps"], line["target_bps"], line.get("drawdown_kind", 0))
    dd = {raw["exam_rules"]["max_drawdown_bps"], raw["funded_rules"]["max_drawdown_bps"]}
    targets = set(raw["exam"]["targets_bps"]) | set(raw["funded"]["targets_bps"])
    if len(dd) != 1 or len(targets) != 1:
        raise SystemExit("a line without its own tier needs one drawdown and one target for both stages")
    return (dd.pop(), targets.pop(), 0)


def code_hash(text: str) -> bytes:
    """What a pool keys a code by, and a sale names: the keccak256 of its text."""
    return keccak(text=text)


def codes_of(raw: dict, creator: str) -> list[tuple[str, bytes, str, int]]:
    """The plan's codes as (text, hash, partner, bps), checked as the pool checks them. "creator" as a partner is
    the creator's own address: a code with no share, for sales the platform makes itself."""
    out, seen = [], set()
    for entry in raw.get("codes", []):
        if not isinstance(entry, dict) or not {"code", "partner", "bps"} <= set(entry):
            raise SystemExit(f"a code in the plan needs \"code\", \"partner\" and \"bps\": {entry!r}")
        text = entry["code"]
        partner = creator if entry["partner"] == "creator" else entry["partner"]
        partner, bps = to_checksum_address(partner), int(entry["bps"])
        if not text or text != text.strip():
            raise SystemExit(f"code {text!r}: empty, or with spaces at its ends: a page trims what a buyer types, "
                             "so nobody could buy with it")
        if text in seen:
            raise SystemExit(f"code {text!r}: twice in the plan")
        if partner == ZERO:
            raise SystemExit(f"code {text!r}: no partner address")
        if bps != 0 and not MIN_PARTNER_BPS <= bps <= MAX_PARTNER_BPS:
            raise SystemExit(f"code {text!r}: a share is 0 or {MIN_PARTNER_BPS} to {MAX_PARTNER_BPS} bps, not {bps}")
        seen.add(text)
        out.append((text, code_hash(text), partner, bps))
    if len(out) > MAX_CODES:
        raise SystemExit(f"{len(out)} codes: a pool takes at most {MAX_CODES}")
    return out


def to_set(codes: list[tuple[str, bytes, str, int]], on_chain) -> list[tuple[str, bytes, str, int]]:
    """The codes still to send. `on_chain(hash)` is the pool's codeOf. A code a sale named keeps its partner and
    its share may only rise: one that matches is skipped, a raise for the same partner is sent, and anything else
    stops everything before a transaction."""
    out = []
    for text, h, partner, bps in codes:
        have, have_bps, used = on_chain(h)[:3]
        same = to_checksum_address(have) == partner and int(have_bps) == bps
        raise_ = to_checksum_address(have) == partner and bps > int(have_bps)
        if used and not same and not raise_:
            raise SystemExit(f"code {text!r} is fixed on chain as {have} {have_bps} bps: a sale named it; its share "
                             "can only rise, for the same partner")
        if not same:
            out.append((text, h, partner, bps))
    return out


def print_codes(codes: list[tuple[str, bytes, str, int]]) -> None:
    for text, h, partner, bps in codes:
        print(f"  code             {text!r} 0x{h.hex()[:12]}.. -> {partner} {bps / 100:g}%")
    if not codes:
        print("  codes            none: the pool sells nothing until `codes` sets one")


def send_codes(creator, pool_: str, codes: list[tuple[str, bytes, str, int]]) -> None:
    on_chain = lambda h: c.call_view(pool_, "codeOf(bytes32)", ["bytes32"], [h], CODE_OF)
    for text, h, partner, bps in to_set(codes, on_chain):
        rcpt = c.transact(creator, pool_, SET_CODE, ["bytes32", "address", "uint16"], [h, partner, bps])
        print(f"deploy_sim: code {text!r} set, tx {rcpt['transactionHash']}")


def _cover(line: dict) -> int:
    """LOCKED_PAYOUTS payouts of a line (6 decimals): what the pool must have free for it to be on or to sell."""
    return usdc_units(line["size_usdc"]) * PAYOUT_BPS // 10_000 * LOCKED_PAYOUTS


def locked_of(raw: dict) -> int:
    """The USDC a pool locks at opening (6 decimals): the plan's `locked_usdc`, set by its creator, at least
    LOCKED_PAYOUTS payouts of the smallest line; a plan without it locks LOCKED_PAYOUTS payouts of the largest size,
    as every pool did before 16 November, and so has every line on."""
    if "locked_usdc" not in raw:
        return max(_cover(m) for m in raw["menu"])
    locked = usdc_units(raw["locked_usdc"])
    if locked < min(_cover(m) for m in raw["menu"]):
        raise SystemExit(f"locked_usdc {raw['locked_usdc']} is less than {LOCKED_PAYOUTS} payouts of the smallest line")
    return locked


def on_of(raw: dict) -> int:
    """The lines on at the start, bit i for line i: each line's `on`, or, for a line without it, whether the lock
    covers its LOCKED_PAYOUTS payouts. A line on that the lock does not cover is refused here, as the factory would."""
    locked, mask = locked_of(raw), 0
    for i, m in enumerate(raw["menu"]):
        on = m.get("on", _cover(m) <= locked)
        if on and _cover(m) > locked:
            raise SystemExit(f"line {i} ({m['size_usdc']:g}) is on, and {locked / 1e6:g} locked does not cover "
                             f"{LOCKED_PAYOUTS} of its payouts ({_cover(m) / 1e6:g})")
        mask |= int(bool(on)) << i
    return mask


def keeper_nodes_of(args) -> dict | None:
    """The keeper's nodes for the record: --keeper-nodes's file, checked as the host checks a record's; on testnet
    without it, demo3's (read at --send). Mainnet has no default: its keeper needs a node of its own (audit M-1)."""
    if not args.keeper_nodes:
        if c.NETWORK.name == "mainnet":
            raise SystemExit("--keeper-nodes is needed on mainnet: the keeper's own node, not the gateway's (audit M-1)")
        return None
    nodes = json.loads(pathlib.Path(args.keeper_nodes).read_text())
    from ops.host_instances import keeper_nodes
    keeper_nodes({"keeper_nodes": nodes})
    return nodes


def mainnet_plan_ok(raw: dict) -> None:
    """On mainnet a plan written for testnet is refused: its terms would tell traders the wrong network."""
    if c.NETWORK.name == "mainnet" and any("testnet" in str(raw.get(f, "")).lower() for f in ("terms_text", "about")):
        raise SystemExit("this plan is written for testnet (its terms or about say so); mainnet takes a plan of its own")


def operator_of(factory: str) -> str:
    return to_checksum_address(c.call_view(factory, "operator()", [], [], ["address"])[0])


def safe_of(args, keys: tuple) -> str | None:
    """--safe, the factory's operator once it is set up: our Safe, which opens our pools and alone can change the
    factory's roles. Needed on mainnet, where the deployer's key must not stay in charge of the factory. Refused as
    0x0, and as one of the platform's three keys: a pool's creator is none of them."""
    if not args.safe:
        if c.NETWORK.name == "mainnet":
            raise SystemExit("--safe is needed on mainnet: the factory's operator after the deploy is our Safe, never "
                             "the deployer's key")
        return None
    safe = to_checksum_address(args.safe)
    if int(safe, 16) == 0:
        raise SystemExit("--safe is 0x0: setOperator would refuse it, after every contract is deployed")
    if safe in keys:
        raise SystemExit("--safe is the filler, the checker or the closer: the operator opens our pools, and a pool's "
                         "creator is none of them")
    return safe


def safe_ok(safe: str, deployer: str) -> None:
    """Before anything is sent: the Safe is not the deployer's own key."""
    if safe == to_checksum_address(deployer):
        raise SystemExit("--safe is the deployer's own key: the factory would stay with the key that deployed it")


def safe_facts(safe: str) -> tuple[int, list[str]]:
    """Read before the person confirms: --safe is a contract on this chain that answers as a Safe does, and the
    threshold and the owners it names, so that whoever types the phrase sees to whom the factory goes. Any read that
    fails is a refusal: an address that has code but no getThreshold() and getOwners() is not a Safe."""
    c.assert_network()      # the RPC is on the network named: what is read below is read from the right chain
    code = c.rpc("eth_getCode", [safe, "latest"])
    # Nodes and mocks differ on how they say "no code": "0x", "0x0", "0X", nothing at all.
    if not isinstance(code, str) or code.lower().removeprefix("0x") in ("", "0"):
        raise SystemExit(f"--safe {safe} has no code on {c.NETWORK.name}: a Safe is a contract; this is a typo, a "
                         "key, or a Safe of another chain")
    try:
        threshold = int(c.call_view(safe, "getThreshold()", [], [], ["uint256"])[0])
        owners = [to_checksum_address(o) for o in c.call_view(safe, "getOwners()", [], [], ["address[]"])[0]]
    except Exception as exc:
        raise SystemExit(f"--safe {safe} is not a Safe: getThreshold() or getOwners() did not answer ({exc})") from None
    if not owners or not 1 <= threshold <= len(owners):
        raise SystemExit(f"--safe {safe} is not a Safe: it reads threshold {threshold} with {len(owners)} owners")
    return threshold, owners


def hand_over(op, factory: str, safe: str, out: pathlib.Path, record: dict) -> None:
    """The last step of `deploy`: setOperator to the Safe, read before and after. Before, the operator is the
    deployer's key, or something else has happened and nothing is sent; after, it is the Safe, or the deploy is not
    complete. From then on only the Safe sets the factory's roles and assets."""
    before = operator_of(factory)
    if before != to_checksum_address(op.address):
        raise SystemExit(f"the factory's operator is {before}, not the deployer's key: setOperator not sent")
    record["tx"]["setOperator"] = c.transact(op, factory, "setOperator(address)", ["address"], [safe])["transactionHash"]
    save(out, record)
    after = operator_of(factory)
    if after != safe:
        raise SystemExit(f"after setOperator the factory's operator reads {after}, not the Safe {safe}")
    record["operator"] = safe
    save(out, record)
    print(f"deploy_sim: the factory's operator is the Safe {safe}, tx {record['tx']['setOperator']}")


def deploy(args) -> int:
    assets = asset_list(args.assets.split(","))
    out = deployments.record_path(args.label, c.NETWORK)
    nodes = keeper_nodes_of(args)
    if out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} exists; pick another label")
    filler, checker = to_checksum_address(args.filler), to_checksum_address(args.checker)
    closer = to_checksum_address(args.closer)
    # Before anything is deployed: a zero key would pass the checks below and fail only at setCloser, four
    # contracts later (review of #282).
    if any(int(a, 16) == 0 for a in (filler, checker, closer)):
        raise SystemExit("the filler, the checker and the closer are real keys, none of them 0x0")
    if len({filler, checker, closer}) != 3:
        raise SystemExit("the filler, the checker and the closer are three keys")
    safe = safe_of(args, (filler, checker, closer))
    print(f"deploy_sim deploy: {c.NETWORK.name} (chain {c.CHAIN_ID}), label {args.label}, USDC {c.USDC_ERC20} "
          "(Circle's, EIP-3009)")
    print("  would deploy     PaperExam, SimPoolStops, SimPoolSetup, SimPoolCodes, SimPool (linked to the three), TermsBook, "
          "SimPoolFactory(usdc, exam, pool, operator)  (big blocks on, then off)")
    print(f"  would configure  setFiller {filler}, setChecker {checker}, setCloser {closer}, setFeeRecipient {args.fee_recipient or 'the operator'}, "
          f"setTermsBook, setAsset " + ", ".join(f"{n} ({i}) {cap / 100:g}x" for n, i, cap in assets))
    if safe:
        print(f"  would hand over  setOperator {safe} last: the factory's operator read as the deployer's key before, "
              "and as the Safe after")
    else:
        print("  operator         stays the deployer's key (no --safe; testnet only)")
    print(f"  would write      {out.relative_to(ROOT)}")
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    told: list[str] = []

    def held() -> str:
        """What the Safe says of itself, read once and printed: to whom the factory goes, and how many of whom must
        sign for it. On mainnet it is read for the confirmation, once the person is at a terminal; on testnet,
        which asks for no phrase, right after. Either way before a transaction is sent."""
        if not told:
            threshold, owners = safe_facts(safe)
            told.append(f"the Safe {safe}, {threshold} of its {len(owners)} owners must sign: {', '.join(owners)}")
            print(f"  the Safe         {told[0]}")
        return told[0]

    sn.confirm_send(args, lambda: f"deploy the simulated pool's contracts as {args.label!r} from the key {args.operator!r}"
                    + (f", then hand the factory to {held()}" if safe else ""), c)
    if safe:
        held()
    sn.check_assets([n for n, _, _ in assets], c.NETWORK, c.info_post)
    op = c.account(args.operator)
    recipient = to_checksum_address(args.fee_recipient) if args.fee_recipient else op.address
    if op.address in (filler, checker, closer):
        raise SystemExit("the operator is neither the filler, the checker nor the closer: it opens our pools, and a "
                         "pool's creator is none of them")
    if safe:
        safe_ok(safe, op.address)
    if nodes is None:
        nodes = deployments.load("v3-demo3")["keeper_nodes"]
    record: dict = {"chain_id": c.CHAIN_ID, "network": c.NETWORK.name, "label": args.label, "commit": git_head(), "status": "deploying",
                    "deployer": op.address, "operator": op.address, "usdc": c.USDC_ERC20,
                    "filler": filler, "checker": checker, "closer": closer, "fee_recipient": recipient,
                    "platform_assets": {n: i for n, i, _ in assets}, "asset_caps_x100": {n: cap for n, _, cap in assets},
                    "keeper_nodes": nodes, "tx": {}}
    big_blocks(op, True)
    try:
        try:
            exam, rcpt = deploy_contract(op, out, record, "PaperExamImpl", "PaperExam", [], [])
            record["block"] = int(rcpt["blockNumber"], 16)
            # SimPool's stops, the copy of its plan and its codes are libraries it calls (EIP-170): deployed first,
            # and linked.
            stops, _ = deploy_contract(op, out, record, "SimPoolStops", "SimPoolStops", [], [])
            setup, _ = deploy_contract(op, out, record, "SimPoolSetup", "SimPoolSetup", [], [])
            codes_lib, _ = deploy_contract(op, out, record, "SimPoolCodes", "SimPoolCodes", [], [])
            pool, _ = deploy_contract(op, out, record, "SimPoolImpl", "SimPool", [], [],
                                      {"SimPoolStops": stops, "SimPoolSetup": setup, "SimPoolCodes": codes_lib})
            book, _ = deploy_contract(op, out, record, "TermsBook", "TermsBook", [], [])
            factory, _ = deploy_contract(op, out, record, "SimPoolFactory", "SimPoolFactory",
                                         ["address", "address", "address", "address"],
                                         [c.USDC_ERC20, exam, pool, op.address])
        finally:
            big_blocks(op, False)
        record["status"] = "configuring"
        save(out, record)
        steps = [("setFiller", "setFiller(address)", ["address"], [filler]),
                 ("setChecker", "setChecker(address)", ["address"], [checker]),
                 ("setCloser", "setCloser(address)", ["address"], [closer]),
                 ("setFeeRecipient", "setFeeRecipient(address)", ["address"], [recipient]),
                 ("setTermsBook", "setTermsBook(address)", ["address"], [book])]
        steps += [(f"setAsset_{n}", "setAsset(uint32,uint32)", ["uint32", "uint32"], [i, cap]) for n, i, cap in assets]
        for name, sig, types, values in steps:
            record["tx"][name] = c.transact(op, factory, sig, types, values)["transactionHash"]
            save(out, record)
        if safe:
            hand_over(op, factory, safe, out, record)
    except BaseException as exc:
        mark_incomplete(out, record, exc)
        raise
    record["status"] = DONE
    save(out, record)
    print(f"deploy_sim: deployed, record {out.relative_to(ROOT)}")
    return 0


def pool(args) -> int:
    record = deployments.load(args.deployment)
    raw = json.loads(pathlib.Path(args.plan).read_text())
    mainnet_plan_ok(raw)
    text = raw["terms_text"]
    terms_hash = keccak(text=text)
    plan = plan_of(raw, terms_hash)
    codes = codes_of(raw, signer_of(args, "creator"))
    locked = locked_of(raw)
    factory = record.get("SimPoolFactory", "(none in this record)")
    print(f"deploy_sim pool: factory {factory}, {len(plan[0])} sizes "
          + ", ".join(f"{m[0] / 1e6:g} for {m[1] / 1e6:g}" for m in plan[0]) + " USDC")
    on = plan[-1]
    print(f"  locks            {locked / 1e6:g} USDC, pays 1 HYPE; lines on at the start: "
          + (", ".join(str(i) for i in range(len(plan[0])) if on >> i & 1) or "none")
          + " (the creator switches the others on, with two of their payouts free)")
    print(f"  book             {'at the book, ceiling ' + str(plan[7]) + ' bp' if plan[6] else 'at the oracle'}; "
          f"fill window {plan[2][7]} blocks")
    print_codes(codes)
    print(f"  terms            {len(text)} bytes, keccak 0x{terms_hash.hex()}")
    if getattr(args, "signer", None):
        # The creator is whoever calls create: the Safe signs all three; its codes come after, by `codes --signer`,
        # once the pool's address is known.
        print("  then             `codes --pool <the new pool> --plan … --signer …` for the codes")
        return print_batch(args, "open a simulated pool", [
            (record["TermsBook"], 0, c.encode_call("publish(string)", ["string"], [text])),
            (record["usdc"], 0, c.encode_call("approve(address,uint256)", ["address", "uint256"], [factory, locked])),
            (factory, CREATION_FEE, c.encode_call(f"create({PLAN_T})", [PLAN_T], [plan]))])
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    sn.confirm_send(args, f"open a pool on {factory} from the key {args.creator!r}: locks {locked / 1e6:g} USDC, pays 1 HYPE", c)
    creator = c.account(args.creator)
    c.transact(creator, record["TermsBook"], "publish(string)", ["string"], [text])
    c.transact(creator, record["usdc"], "approve(address,uint256)", ["address", "uint256"], [factory, locked])
    data = c.encode_call(f"create({PLAN_T})", [PLAN_T], [plan])
    rcpt = c.send_tx(creator, factory, data, value=CREATION_FEE)
    topic = "0x" + keccak(text="PoolCreated(address,address,uint256)").hex()
    created = [l for l in rcpt["logs"] if l["topics"] and l["topics"][0] == topic]
    if not created:
        raise SystemExit(f"no PoolCreated in {rcpt['transactionHash']}: read the transaction before anything else")
    pool_ = to_checksum_address('0x' + created[0]['topics'][1][-40:])
    print(f"deploy_sim: pool {pool_}, tx {rcpt['transactionHash']}")
    send_codes(creator, pool_, codes)
    return 0


# ── checking a plan before anything is sent ──────────────────────────────────────

# Where the contracts `check` reads stand: addresses no key holds, given code by the call's state override.
CHECK_AT = {"exam": "0x" + "00" * 18 + "c0e1", "factory": "0x" + "00" * 18 + "c0f1", "book": "0x" + "00" * 18 + "c0b1",
            "pool": "0x" + "00" * 18 + "c0d1", "text": "0x" + "00" * 18 + "c0a1", "setup": "0x" + "00" * 18 + "c051",
            "stops": "0x" + "00" * 18 + "c052", "codes": "0x" + "00" * 18 + "c053", "creator": "0x" + "00" * 18 + "c0c1", "filler": "0x" + "00" * 18 + "c0a2",
            "checker": "0x" + "00" * 18 + "c0a3", "closer": "0x" + "00" * 18 + "c0a4", "fee": "0x" + "00" * 18 + "c0a5"}
# SimPoolFactory's slots of `termsBook` and `assetCapX100`, TermsBook's of `textAt` (forge inspect … storageLayout);
# test/sim/SimPoolFactory.t.sol holds them against the contracts.
FACTORY_TERMS_BOOK_SLOT, FACTORY_ASSET_CAP_SLOT, BOOK_TEXT_AT_SLOT = 5, 6, 0
# SimPoolFactory's slots of `filler`, `checker`, `closer` and `feeRecipient`: `create` refuses a factory without them.
FACTORY_ROLE_SLOTS = {"filler": 1, "checker": 2, "closer": 3, "fee": 4}
# CodeStore.MAX_LENGTH: TermsBook.publish refuses an empty text, and CodeStore one longer than this (review of cp#304).
TERMS_MAX_BYTES = 24_575
CHECK_ERRORS = ("BadPlan()", "Unpublished()", "BadRole()")
CHECK_GAS = 30_000_000


def _word(n: int) -> bytes:
    return int(n).to_bytes(32, "big")


def map_slot(key: int, slot: int) -> str:
    """The storage slot of `mapping[key]` declared at `slot`, for a key that fits a word."""
    return "0x" + keccak(_word(key) + _word(slot)).hex()


def runtime_of(contract: str, types: list, values: list, libraries: dict | None = None) -> str:
    """The code a contract would have once deployed with these arguments, immutables set, its libraries at the
    addresses given, read by an eth_call that creates it: nothing is sent."""
    code = c.link(c.artifact(contract), libraries) + c.eth_abi.encode(types, values)
    return c.rpc("eth_call", [{"data": "0x" + code.hex(), "gas": hex(CHECK_GAS)}, "latest"])


def check_overrides(assets: list[tuple[str, int, int]], terms_hash: bytes) -> dict:
    """The state the factory's `check` reads, as a state override: PaperExam and the factory as this checkout builds
    them, the factory listing `assets` with their caps as `deploy` would and its roles set, a TermsBook with the
    terms published, and SimPool with its three libraries, which `create` clones and calls when its gas is measured."""
    a = CHECK_AT
    factory = runtime_of("SimPoolFactory", ["address", "address", "address", "address"],
                         [c.USDC_ERC20, a["exam"], a["pool"], a["factory"]])
    state = {"0x%064x" % FACTORY_TERMS_BOOK_SLOT: "0x" + _word(int(a["book"], 16)).hex()}
    state.update({map_slot(i, FACTORY_ASSET_CAP_SLOT): "0x" + _word(cap).hex() for _, i, cap in assets})
    state.update({"0x%064x" % slot: "0x" + _word(int(a[role], 16)).hex() for role, slot in FACTORY_ROLE_SLOTS.items()})
    libraries = {"SimPoolSetup": a["setup"], "SimPoolStops": a["stops"], "SimPoolCodes": a["codes"]}
    return {a["exam"]: {"code": runtime_of("PaperExam", [], [])},
            a["pool"]: {"code": runtime_of("SimPool", [], [], libraries)},
            a["setup"]: {"code": runtime_of("SimPoolSetup", [], [])},
            a["stops"]: {"code": runtime_of("SimPoolStops", [], [])},
            a["codes"]: {"code": runtime_of("SimPoolCodes", [], [])},
            a["factory"]: {"code": factory, "stateDiff": state},
            a["book"]: {"code": runtime_of("TermsBook", [], []),
                        "stateDiff": {map_slot(int.from_bytes(terms_hash, "big"), BOOK_TEXT_AT_SLOT):
                                      "0x" + _word(int(a["text"], 16)).hex()}}}


def revert_named(exc: Exception) -> str:
    """Which of the factory's errors an eth_call's failure is, by its selector in the node's answer, or the answer."""
    text = str(exc)
    # A node may write the revert data in capitals: compared in one case (review of cp#311).
    for sig in CHECK_ERRORS:
        if "0x" + c.selector(sig).hex() in text.lower():
            return sig.removesuffix("()")
    return text


def check(args) -> int:
    """The plan against SimPoolFactory.check, by eth_call: nothing is sent and no key is read. With --deployment, the
    factory it names on chain, as it stands (its terms must be published, or it says Unpublished); without, the
    factory and PaperExam this checkout builds, put in the call by a state override over the network's precompiles,
    the assets listed as `deploy` lists them and the terms as `pool` publishes them."""
    raw = json.loads(pathlib.Path(args.plan).read_text(encoding="utf-8"))
    mainnet_plan_ok(raw)
    text = raw["terms_text"].encode("utf-8")
    if not text or len(text) > TERMS_MAX_BYTES:
        raise SystemExit(f"deploy_sim check: the terms are {len(text)} bytes; TermsBook.publish takes 1 to "
                         f"{TERMS_MAX_BYTES}")
    terms_hash = keccak(text=raw["terms_text"])
    plan = plan_of(raw, terms_hash)
    data = c.encode_call(f"check({PLAN_T})", [PLAN_T], [plan])
    if args.deployment:
        factory, overrides = deployments.load(args.deployment)["SimPoolFactory"], None
        where = f"the factory of {args.deployment}, {factory}"
    else:
        factory, overrides = CHECK_AT["factory"], check_overrides(asset_list(args.assets.split(",")), terms_hash)
        where = f"SimPoolFactory and PaperExam of this checkout ({git_head()[:12]}), assets {args.assets}"
    print(f"deploy_sim check: {c.NETWORK.name} (chain {c.CHAIN_ID}), {where}")
    for (size, price), (dd, target, kind) in zip(plan[0], plan[9]):
        print(f"  line             {size / 1e6:g} for {price / 1e6:g} USDC: loss limit {dd / 100:g} %, target "
              f"{target / 100:g} %, {'trailing to the start' if kind else 'from the start'}")
    print(f"  payouts a day    {plan[10]}; locks {locked_of(raw) / 1e6:g} USDC; terms keccak 0x{terms_hash.hex()}")
    params = [{"to": factory, "data": "0x" + data.hex(), "gas": hex(CHECK_GAS)}, "latest"]
    try:
        out = c.rpc("eth_call", params + [overrides] if overrides else params)
    except RuntimeError as exc:
        raise SystemExit(f"deploy_sim check: the factory refuses the plan: {revert_named(exc)}")
    if not out or out in ("0x", "0x0"):
        # No answer is no code there: a --deployment of another network, or a wrong address (review of cp#304).
        raise SystemExit(f"deploy_sim check: {factory} gave no answer on {c.NETWORK.name}: no factory there")
    largest = int(out, 16)
    if largest != max(m[0] for m in plan[0]):
        raise SystemExit(f"deploy_sim check: the factory answered {largest}, not the plan's largest size")
    print(f"deploy_sim check: the factory accepts the plan; largest size {largest / 1e6:g} USDC.")
    gas, limit = create_gas(factory, plan, overrides)
    if gas is None:
        raise SystemExit(f"deploy_sim check: create does not run on {limit} gas, a small block of {c.NETWORK.name}: "
                         "the pool would not open by an ordinary transaction")
    if gas + TOKEN_GAS > limit:
        raise SystemExit(f"deploy_sim check: create takes {gas} gas and USDC's transfer up to {TOKEN_GAS} more, above "
                         f"{limit}, a small block of {c.NETWORK.name}: the pool would not open by an ordinary transaction")
    print(f"deploy_sim check: create takes {gas} gas, and USDC's transfer up to {TOKEN_GAS} more, of {limit} in a small "
          f"block of {c.NETWORK.name}: the pool opens by an ordinary transaction. Nothing was sent.")
    return 0

# A token that answers every call with 1: `create`'s transferFrom of the locked USDC goes through for any creator,
# with no balance or allowance to fake. Runtime: PUSH1 1, PUSH1 0, MSTORE, PUSH1 32, PUSH1 0, RETURN.
TOKEN_STUB = "0x600160005260206000f3"
# What the stand-in leaves out: USDC's own transferFrom of the locked amount, well under this.
TOKEN_GAS = 60_000
# HyperEVM's small block when the network says nothing: 3 000 000 gas (its documentation, and `gasLimit` on mainnet
# and testnet blocks, 16 November 2026; test/sim/SimPool.t.sol SMALL_BLOCK_GAS holds the same).
SMALL_BLOCK_GAS = 3_000_000
# How many of the latest blocks the small block's limit is read from: big blocks come once a minute, small ones
# every second, so the least of these is a small block's.
BLOCKS_READ = 8
# Below this no `create` fits, whatever its plan: a node that runs the call on so little ignores the call's gas.
GAS_FLOOR = 100_000
GAS_STEP = 1_000


def small_block_gas() -> int:
    """The gas limit of the network's small block: the least `gasLimit` of the latest blocks, and never above
    SMALL_BLOCK_GAS, so a network that raised it does not pass a plan its documentation would not."""
    head = int(c.rpc("eth_blockNumber", []), 16)
    # A young chain has fewer blocks than BLOCKS_READ: never a block below 0 (review of cp#311).
    limits = [int(c.rpc("eth_getBlockByNumber", [hex(head - i), False])["gasLimit"], 16)
              for i in range(min(head + 1, BLOCKS_READ))]
    return min(limits + [SMALL_BLOCK_GAS])


def create_gas(factory: str, plan: tuple, overrides: dict | None) -> tuple[int | None, int]:
    """The least gas `create(plan)` with 1 HYPE runs on, from an address no key holds, by eth_call: USDC answered by a
    stand-in (TOKEN_STUB), the creator given the HYPE. Returns (gas or None if it does not run in a small block, the
    small block's limit)."""
    limit = small_block_gas()
    state = dict(overrides or {})
    state[c.USDC_ERC20] = {"code": TOKEN_STUB}
    state[CHECK_AT["creator"]] = {"balance": hex(2 * CREATION_FEE)}
    data = "0x" + c.encode_call(f"create({PLAN_T})", [PLAN_T], [plan]).hex()

    def runs(gas: int) -> bool:
        call = {"from": CHECK_AT["creator"], "to": factory, "data": data, "value": hex(CREATION_FEE), "gas": hex(gas)}
        try:
            c.rpc("eth_call", [call, "latest", state])
            return True
        except RuntimeError:
            return False
    if runs(GAS_FLOOR):
        raise SystemExit(f"deploy_sim check: the node ran create on {GAS_FLOOR} gas: it ignores a call's gas, and "
                         "the gas of create cannot be measured here")
    if not runs(limit):
        return None, limit
    lo, hi = GAS_FLOOR, limit
    while hi - lo > GAS_STEP:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if runs(mid) else (mid, hi)
    return hi, limit


# ── closing a pool ───────────────────────────────────────────────────────────────

STATUS = ["None", "Active", "Passed", "Breached", "Expired"]  # PaperExam.Status
TERMS_OUT = TERMS_T  # PaperExam.terms(): its stepDuration is the third field


def utc(t: int) -> str:
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def pool_state(pool_: str) -> dict:
    """What `close` and `finish` decide on, read from the pool and its factory."""
    v = lambda sig, out: c.call_view(pool_, sig, [], [], [out])[0]
    factory = v("factory()", "address")
    return {"creator": to_checksum_address(v("creator()", "address")),
            "closer": to_checksum_address(c.call_view(factory, "closer()", [], [], ["address"])[0]),
            "closed": bool(v("closed()", "bool")), "finished": bool(v("finished()", "bool")),
            "locked_until": int(v("lockedUntil()", "uint64")), "live": int(v("live()", "uint256")),
            "owed": int(v("owed()", "uint256")), "reserved": int(v("reserved()", "uint256")),
            "fund_window": int(v("fundWindow()", "uint32")), "closed_at": int(v("closedAt()", "uint64")),
            "requested": int(v("requested()", "uint256")), "ready_at": int(v("readyAt()", "uint64")),
            "withdrawable": int(v("withdrawable()", "uint256")),
            "delay": int(c.call_view(factory, "WITHDRAW_DELAY()", [], [], ["uint32"])[0]),
            "window": int(c.call_view(factory, "WITHDRAW_WINDOW()", [], [], ["uint32"])[0])}


def usdc(units: int) -> str:
    return f"{units / 1e6:,.6f}".rstrip("0").rstrip(".") + " USDC"


def close_check(st: dict, now: int, sender: str) -> str:
    """As whom `sender` closes the pool now, as the contract decides it, or why the pool would refuse."""
    if st["finished"] or st["closed"]:
        raise SystemExit("Closing: the pool is closed already")
    early = now < st["locked_until"]
    if sender == st["closer"]:
        return "the factory's closer, before the term (closedEarly)" if early else "the factory's closer"
    if sender != st["creator"]:
        raise SystemExit(f"NotCreator: {sender} is neither the pool's creator nor the factory's closer")
    if early:
        raise SystemExit(f"Locked: the creator closes from {utc(st['locked_until'])} on; before then only the closer")
    return "the creator"


def exam_text(exam: str, x: dict, now: int, fund_window: int) -> str:
    """Until when one exam or stage keeps the pool from finishing. `x`: its status, stepStartedAt, stepDuration,
    passedAt, and whether the pool holds it done."""
    if x["done"]:
        return f"{exam} is done here; another exam or stage still runs"
    status = STATUS[x["status"]]
    if status == "Active":
        until = x["started"] + x["duration"]
        # Each later step has a deadline of its own, a step's duration from its start (review of #279).
        later = max(x.get("steps", 1) - x.get("step", 0) - 1, 0)
        if now < until:
            if later:
                return (f"{exam} is on step {x['step'] + 1} of {x['steps']}: this step runs until {utc(until)} at the "
                        f"latest and, if it passes, {later} more step(s) of up to {x['duration'] // 86_400} day(s) each "
                        f"follow, so until {utc(until + later * x['duration'])} at the latest; sooner if it breaks a rule")
            return f"{exam} runs until {utc(until)} at the latest, sooner if it breaks a rule"
        return f"{exam} is past its deadline ({utc(until)}): the keeper expires and releases it at its next pass"
    if status == "Passed" and x.get("kind") == 2:  # Kind.Stage: its payout is claimed, it is not funded (review of #279)
        return f"{exam} passed its stage: the keeper claims its payout (claimPayout) at its next pass, and that ends it"
    if status == "Passed":
        until = x["passed_at"] + fund_window
        if now <= until:
            return f"{exam} passed; its stage can be started until {utc(until)}, then it is released"
        return f"{exam} passed and was not funded by {utc(until)}: the keeper releases it at its next pass"
    return f"{exam} has ended ({status}): the keeper releases it at its next pass"


def finish_check(st: dict, now: int, sender: str, exam_line: str | None = None) -> str:
    """As whom `sender` finishes the pool now, or why the pool would refuse, each reason named."""
    if st["finished"]:
        raise SystemExit("Done: the pool is finished already")
    if sender not in (st["creator"], st["closer"]):
        raise SystemExit(f"NotCreator: {sender} is neither the pool's creator nor the factory's closer")
    if not st["closed"]:
        when = "" if now >= st["locked_until"] else f"; before {utc(st['locked_until'])} only the closer closes it"
        raise SystemExit(f"Done: the pool is not closed yet: `close` first{when}")
    reasons = []
    if st["live"]:
        reasons.append(f"{st['live']} exam(s) or stage(s) still run" + (f": {exam_line}" if exam_line else
                                                                        " (--exam ADDRESS says until when)"))
    if st["owed"]:
        reasons.append(f"{st['owed'] / 1e6:g} USDC of payouts still owed")
    if st["reserved"]:
        reasons.append(f"{st['reserved'] / 1e6:g} USDC kept for stops that can still be lifted")
    ready = st["closed_at"] + st["delay"]
    if reasons:
        later = f"; and not before {utc(ready)}, a week after the close" if now < ready else ""
        raise SystemExit("NotOver: " + "; ".join(reasons) + later)
    if now < ready:
        raise SystemExit(f"NotReady: finish from {utc(ready)} on, a week after the close ({utc(st['closed_at'])})")
    return "the creator" if sender == st["creator"] else "the factory's closer"


def amount_units(text: str) -> int:
    """USDC as the creator types it ("250", "84.575") in the token's units, refused if not a positive amount
    of at most six decimals."""
    from decimal import Decimal, InvalidOperation
    try:
        x = Decimal(text.strip())
    except InvalidOperation:
        raise SystemExit(f"--amount {text!r} is not an amount of USDC")
    units = x * 10**6
    if not x.is_finite() or units != units.to_integral_value() or units <= 0:
        raise SystemExit(f"--amount {text!r}: a positive amount of USDC, at most six decimals")
    return int(units)


def request_check(st: dict, now: int, sender: str, amount: int) -> str:
    """What `requestWithdraw(amount)` from `sender` would do now, or why the pool would refuse."""
    if st["finished"]:
        raise SystemExit("Done: the pool is finished: nothing more to withdraw")
    if sender != st["creator"]:
        raise SystemExit(f"NotCreator: {sender} is not the pool's creator; only the creator asks to withdraw")
    if amount > st["withdrawable"]:
        raise SystemExit(f"TooMuch: the pool lets go at most {usdc(st['withdrawable'])} now")
    ready = now + st["delay"]
    text = f"asks for {usdc(amount)}, to be carried out from {utc(ready)} until {utc(ready + st['window'])}"
    if st["requested"]:
        text += f"; it replaces the standing request for {usdc(st['requested'])}"
    return text


def withdraw_check(st: dict, now: int, sender: str) -> int:
    """What the standing request pays if `sender` carries it out now, or why the pool would refuse."""
    if st["finished"]:
        raise SystemExit("Done: the pool is finished: nothing more to withdraw")
    if sender != st["creator"]:
        raise SystemExit(f"NotCreator: {sender} is not the pool's creator")
    if not st["requested"]:
        raise SystemExit("NoRequest: no request stands: `request` first, and a week later `withdraw`")
    if now < st["ready_at"]:
        raise SystemExit(f"NotReady: the request for {usdc(st['requested'])} can be carried out from {utc(st['ready_at'])}")
    lapses = st["ready_at"] + st["window"]
    if now >= lapses:
        raise SystemExit(f"RequestLapsed: the request lapsed at {utc(lapses)}: `request` again, and wait the week again")
    amount = min(st["requested"], st["withdrawable"])
    if not amount:
        raise SystemExit("TooMuch: the pool lets nothing go now (payouts owed came first); the request stands until "
                         + utc(lapses))
    return amount


PAYOUT_STATE = ["None", "Due", "Stopped", "Paid", "Held", "Refused"]  # SimPool.PayoutState
PAYOUT_T = "(address,address,uint64,uint64,uint8,bool,uint64,bool)"  # SimPool.payout(id)


def ids_of(text: str) -> list[int]:
    """Payout ids as typed: "3,4,7", each once."""
    try:
        ids = [int(x) for x in text.split(",") if x.strip()]
    except ValueError:
        raise SystemExit(f"--ids {text!r}: payout numbers, separated by commas")
    if not ids or len(set(ids)) != len(ids) or min(ids) < 0:
        raise SystemExit(f"--ids {text!r}: at least one payout number, each once")
    return ids


def approve_check(approver: str, sender: str, payouts: dict, ids: list[int], hard: int) -> list[str]:
    """What approving `ids` from `sender` would do, a line a payout, or why the pool would refuse. `payouts`:
    id → the pool's payout(id) as read (absent: no such payout); `hard`: PAYOUT_HARD less PAYOUT_DELAY."""
    if int(approver, 16) == 0:
        raise SystemExit("NotApprover: the pool has no approver: its creator names one with `approver`")
    if sender != approver:
        raise SystemExit(f"NotApprover: {sender} is not the pool's approver ({approver})")
    lines = []
    for i in ids:
        p = payouts.get(i)
        if p is None:
            raise SystemExit(f"BadPayout: payout {i} does not exist")
        state = PAYOUT_STATE[p[4]]
        if state != "Due":
            raise SystemExit(f"BadPayout: payout {i} is {state}, not waiting")
        if p[7]:
            raise SystemExit(f"BadPayout: payout {i} is approved already")
        lines.append(f"payout {i}: {usdc(p[2])} to {to_checksum_address(p[1])}, goes out from {utc(p[3])} approved; "
                     f"{utc(p[3] + hard)} without")
    return lines


def approver_cmd(args) -> int:
    pool_, sender = to_checksum_address(args.pool), signer_of(args)
    creator = to_checksum_address(c.call_view(pool_, "creator()", [], [], ["address"])[0])
    if sender != creator:
        raise SystemExit(f"NotCreator: {sender} is not the pool's creator ({creator})")
    who = to_checksum_address(args.address)
    print(f"deploy_sim approver: pool {pool_}, setApprover({who}) from {args.key or 'the signer'} ({sender})"
          + ("; no approver: every payout goes out 7 days after its claim" if int(who, 16) == 0 else ""))
    return send_or_say(args, pool_, "setApprover(address)", ["address"], [who])


def approve(args) -> int:
    pool_, sender = to_checksum_address(args.pool), signer_of(args)
    ids = ids_of(args.ids)
    count = int(c.call_view(pool_, "payoutCount()", [], [], ["uint256"])[0])
    payouts = {i: c.call_view(pool_, "payout(uint256)", ["uint256"], [i], [PAYOUT_T])[0] for i in ids if i < count}
    factory = c.call_view(pool_, "factory()", [], [], ["address"])[0]
    hard = int(c.call_view(factory, "PAYOUT_HARD()", [], [], ["uint32"])[0]) - \
        int(c.call_view(factory, "PAYOUT_DELAY()", [], [], ["uint32"])[0])
    approver = to_checksum_address(c.call_view(pool_, "approver()", [], [], ["address"])[0])
    lines = approve_check(approver, sender, payouts, ids, hard)
    print(f"deploy_sim approve: pool {pool_}, approvePayouts({ids}) from {args.key or 'the signer'} ({sender})")
    for line in lines:
        print("  " + line)
    print("  approving sets neither the amount nor the trader; the checker can still stop each until it is paid")
    return send_or_say(args, pool_, "approvePayouts(uint256[])", ["uint256[]"], [ids])


def cancel_check(st: dict, sender: str) -> str:
    """As whom `sender` cancels the standing request, or why the pool would refuse."""
    if sender not in (st["creator"], st["closer"]):
        raise SystemExit(f"NotCreator: {sender} is neither the pool's creator nor the factory's closer")
    if not st["requested"]:
        raise SystemExit("NoRequest: no request stands")
    return "the creator" if sender == st["creator"] else "the factory's closer"


def read_exam(pool_: str, exam: str) -> dict:
    status, _, _, passed_at = c.call_view(exam, "result()", [], [], ["uint8", "address", "uint64", "uint64"])
    terms = c.call_view(exam, "terms()", [], [], [TERMS_OUT])[0]
    kind, _, done = c.call_view(pool_, "accountOf(address)", ["address"], [exam], ["uint8", "uint8", "bool"])
    return {"status": int(status), "started": int(c.call_view(exam, "stepStartedAt()", [], [], ["uint64"])[0]),
            "duration": int(terms[2]), "passed_at": int(passed_at), "done": bool(done), "kind": int(kind),
            "step": int(c.call_view(exam, "step()", [], [], ["uint8"])[0]), "steps": len(terms[1])}


def chain_now() -> int:
    return int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)


def close(args) -> int:
    pool_, sender = to_checksum_address(args.pool), signer_of(args)
    role = close_check(pool_state(pool_), chain_now(), sender)
    print(f"deploy_sim close: pool {pool_}, close() from {args.key or 'the signer'} ({sender}) as {role}")
    print("  the money goes where it would at any finish; the closer gets none of it")
    return send_or_say(args, pool_, "close()")


def finish(args) -> int:
    pool_, sender = to_checksum_address(args.pool), signer_of(args)
    st, now = pool_state(pool_), chain_now()
    line = None
    if args.exam:
        exam = to_checksum_address(args.exam)
        line = exam_text(exam, read_exam(pool_, exam), now, st["fund_window"])
    role = finish_check(st, now, sender, line)
    print(f"deploy_sim finish: pool {pool_}, finish() from {args.key or 'the signer'} ({sender}) as {role}")
    print(f"  pays out to the pool's creator {st['creator']} what is left, each code's partner its share; "
          "what is held stays for its traders and partners")
    return send_or_say(args, pool_, "finish()")


def signer_of(args, key: str = "key") -> str:
    """Who the call is from: `--signer`, an address that signs elsewhere (our Safe, or the Ledger's address), or the
    named key's address. Neither given: refused."""
    if getattr(args, "signer", None):
        return to_checksum_address(args.signer)
    if getattr(args, key, None):
        return c.address_of(getattr(args, key))
    raise SystemExit(f"--{key} or --signer: who the call is from")


def batch(name: str, txs: list[tuple[str, int, bytes]]) -> dict:
    """The calls for a signer elsewhere, in order, as the Safe's Transaction Builder takes them (a file to load), and
    as any wallet shows them: to, value in wei, data."""
    return {"version": "1.0", "chainId": str(c.CHAIN_ID),
            "meta": {"name": name, "description": "deploy_sim: sign these in this order, from the signer named"},
            "transactions": [{"to": to_checksum_address(to), "value": str(v), "data": "0x" + d.hex()} for to, v, d in txs]}


def print_batch(args, name: str, txs: list[tuple[str, int, bytes]]) -> int:
    """With --signer nothing is sent and no key is read: the calls are printed for the Safe or the Ledger's wallet."""
    print(f"deploy_sim: nothing was sent; for {to_checksum_address(args.signer)} to sign, in this order "
          "(the Safe's Transaction Builder takes this JSON):")
    print(json.dumps(batch(name, txs), indent=1))
    return 0


def send_or_say(args, pool_: str, sig: str, types: list | None = None, values: list | None = None) -> int:
    if getattr(args, "signer", None):
        return print_batch(args, f"{sig} on {pool_}", [(pool_, 0, c.encode_call(sig, types or [], values or []))])
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    sn.confirm_send(args, f"{sig} on {pool_} from the key {args.key!r}", c)
    rcpt = c.transact(c.account(args.key), pool_, sig, types or [], values or [])
    print(f"deploy_sim: sent, tx {rcpt['transactionHash']}")
    return 0


def request(args) -> int:
    pool_, sender = to_checksum_address(args.pool), signer_of(args)
    amount = amount_units(args.amount)
    text = request_check(pool_state(pool_), chain_now(), sender, amount)
    print(f"deploy_sim request: pool {pool_}, requestWithdraw({amount}) from {args.key or 'the signer'} ({sender}): {text}")
    print("  nothing moves now; the closer may cancel it meanwhile")
    return send_or_say(args, pool_, "requestWithdraw(uint256)", ["uint256"], [amount])


def withdraw(args) -> int:
    pool_, sender = to_checksum_address(args.pool), signer_of(args)
    st = pool_state(pool_)
    amount = withdraw_check(st, chain_now(), sender)
    print(f"deploy_sim withdraw: pool {pool_}, withdraw() from {args.key or 'the signer'} ({sender}): pays {usdc(amount)}, the less "
          f"of the {usdc(st['requested'])} asked and the {usdc(st['withdrawable'])} the pool lets go now")
    print("  0.5 % to the platform, each code's partner its share of the profit in it, the rest to the creator")
    return send_or_say(args, pool_, "withdraw()")


def cancel(args) -> int:
    pool_, sender = to_checksum_address(args.pool), signer_of(args)
    st = pool_state(pool_)
    role = cancel_check(st, sender)
    print(f"deploy_sim cancel: pool {pool_}, cancelWithdraw() from {args.key or 'the signer'} ({sender}) as {role}: "
          f"the request for {usdc(st['requested'])} is cancelled; nothing moves")
    return send_or_say(args, pool_, "cancelWithdraw()")


def codes(args) -> int:
    pool_ = to_checksum_address(args.pool)
    codes_ = codes_of(json.loads(pathlib.Path(args.plan).read_text()), signer_of(args, "creator"))
    print(f"deploy_sim codes: pool {pool_}, from {args.signer or args.creator}")
    print_codes(codes_)
    if getattr(args, "signer", None):
        on_chain = lambda h: c.call_view(pool_, "codeOf(bytes32)", ["bytes32"], [h], CODE_OF)
        txs = [(pool_, 0, c.encode_call(SET_CODE, ["bytes32", "address", "uint16"], [h, partner, bps]))
               for _, h, partner, bps in to_set(codes_, on_chain)]
        return print_batch(args, f"codes on {pool_}", txs)
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    sn.confirm_send(args, f"set {len(codes_)} codes on {pool_} from the key {args.creator!r}", c)
    send_codes(c.account(args.creator), pool_, codes_)
    return 0


def buy(args) -> int:
    record = deployments.load(args.deployment)
    pool_ = to_checksum_address(args.pool)
    print(f"deploy_sim buy: pool {pool_}, line {args.line}, code {args.code!r}, from {args.trader}")
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    sn.confirm_send(args, f"buy line {args.line} of {pool_} from the key {args.trader!r}", c)
    trader = c.account(args.trader)
    price = c.call_view(pool_, "menu()", [], [], ["(uint64,uint64)[]"])[0][args.line][1]
    c.transact(trader, record["usdc"], "approve(address,uint256)", ["address", "uint256"], [pool_, price])
    rcpt = c.transact(trader, pool_, "buy(uint256,bytes32)", ["uint256", "bytes32"], [args.line, code_hash(args.code)])
    sold = "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64,uint256,bool,bytes32,address)").hex()
    found = [l for l in rcpt["logs"] if l["topics"] and l["topics"][0] == sold]
    if not found:
        raise SystemExit(f"no ExamSold in {rcpt['transactionHash']}: read the transaction before anything else")
    exam = found[0]["topics"][1]
    print(f"deploy_sim: exam {to_checksum_address('0x' + exam[-40:])}, tx {rcpt['transactionHash']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deploy")
    d.add_argument("--label", required=True)
    d.add_argument("--operator", default="deployer", help="the factory's operator and deployer key")
    d.add_argument("--filler", required=True)
    d.add_argument("--checker", required=True)
    d.add_argument("--closer", required=True, help="the platform's key that alone may close a pool before its term")
    d.add_argument("--fee-recipient")
    d.add_argument("--safe", help="our Safe: the factory's operator after the deploy, set last (setOperator); needed "
                                  "on mainnet")
    d.add_argument("--assets", default="BTC,ETH,SOL")
    d.add_argument("--keeper-nodes", help="a JSON file of the keeper's nodes, as a record's keeper_nodes; needed on mainnet")
    d.add_argument("--send", action="store_true")
    q = sub.add_parser("pool")
    q.add_argument("--deployment", required=True)
    q.add_argument("--plan", required=True)
    q.add_argument("--creator", default="creator")
    q.add_argument("--signer", help="the creator as an address that signs elsewhere (our Safe): print the calls, send nothing")
    q.add_argument("--send", action="store_true")
    h = sub.add_parser("check")
    h.add_argument("--plan", required=True)
    h.add_argument("--deployment", help="a deployed factory to check against; without it, this checkout's code")
    h.add_argument("--assets", default="BTC,ETH,SOL", help="without --deployment: the assets the factory lists")
    b = sub.add_parser("buy")
    b.add_argument("--deployment", required=True)
    b.add_argument("--pool", required=True)
    b.add_argument("--line", type=int, default=0)
    b.add_argument("--trader", default="trader")
    b.add_argument("--code", required=True, help="the code's text, as the plan lists it")
    b.add_argument("--send", action="store_true")
    k = sub.add_parser("codes")
    k.add_argument("--pool", required=True)
    k.add_argument("--plan", required=True)
    k.add_argument("--creator", default="creator")
    k.add_argument("--signer", help="the creator as an address that signs elsewhere (our Safe): print the calls, send nothing")
    k.add_argument("--send", action="store_true")
    for name in ("close", "finish"):
        z = sub.add_parser(name)
        z.add_argument("--pool", required=True)
        z.add_argument("--key", help="the key it is sent from: the closer, or the pool's creator")
        z.add_argument("--signer", help="an address that signs elsewhere (our Safe, a Ledger): print the call, send nothing")
        if name == "finish":
            z.add_argument("--exam", help="an exam or stage of the pool: says until when it keeps the pool from finishing")
        z.add_argument("--send", action="store_true")
    z = sub.add_parser("approver")
    z.add_argument("--pool", required=True)
    z.add_argument("--key", help="the pool's creator")
    z.add_argument("--signer", help="an address that signs elsewhere (our Safe): print the call, send nothing")
    z.add_argument("--address", required=True, help="the approver of payouts; 0x000…0 for none")
    z.add_argument("--send", action="store_true")
    z = sub.add_parser("approve")
    z.add_argument("--pool", required=True)
    z.add_argument("--key", help="the pool's approver")
    z.add_argument("--signer", help="an address that signs elsewhere (the Ledger's): print the call, send nothing")
    z.add_argument("--ids", required=True, help="payout numbers, as 3,4,7")
    z.add_argument("--send", action="store_true")
    for name in ("request", "withdraw", "cancel"):
        z = sub.add_parser(name)
        z.add_argument("--pool", required=True)
        z.add_argument("--key", help="the key it is sent from: the pool's creator, or for cancel the closer")
        z.add_argument("--signer", help="an address that signs elsewhere (our Safe): print the call, send nothing")
        if name == "request":
            z.add_argument("--amount", required=True, help='USDC, as "250" or "84.575"')
        z.add_argument("--send", action="store_true")
    for each in sub.choices.values():
        sn.add_args(each)
    args = p.parse_args(argv)
    if sn.selected(args, c.NETWORK).name == "mainnet" and args.cmd != "deploy":
        c.assert_network()  # what a mainnet command reads is the chain's it names, sent or not (review of #300)
    return {"deploy": deploy, "pool": pool, "check": check, "buy": buy, "codes": codes, "close": close, "finish": finish,
            "request": request, "withdraw": withdraw, "cancel": cancel, "approver": approver_cmd,
            "approve": approve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
