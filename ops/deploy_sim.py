"""Deploys the simulated pool's contracts on testnet, and opens a pool on them (docs/SIM-POOL.md).

    spike/.venv/bin/python -m ops.deploy_sim deploy --label sim1 --filler 0x.. --checker 0x..           # the plan
    spike/.venv/bin/python -m ops.deploy_sim deploy --label sim1 --filler 0x.. --checker 0x.. --send    # sends
    spike/.venv/bin/python -m ops.deploy_sim pool --deployment sim1 \\
        --plan deployments/plans/sim-first-pool.json --creator creator                                  # the plan
    ... pool ... --send                                                                                # sends
    spike/.venv/bin/python -m ops.deploy_sim codes --pool 0x.. \\
        --plan deployments/plans/sim-first-pool.json --creator creator [--send]                        # its codes

`deploy` puts PaperExam (the implementation every exam clones), SimPool (every pool's), a TermsBook
and the SimPoolFactory on HyperEVM testnet, names the filler, the checker and the platform's
recipient, and lists the assets with their leverage caps from ops/leverage.py (HYPE is left out:
the platform's decision of 11 November 2026). It writes deployments/testnet-<label>.json, with the
same keeper nodes as demo3, so the host's keeper unit can run on it.

`buy` approves a line's price and buys an exam with a code from the trader's key, for --measure in
ops/sim_keeper.py; it prints the exam.

`codes` sets a pool's codes from the plan's `codes` (setCode, from the creator's key): each a trading
group's code, the address its owner gave and its share, 0 or 4 000 to 6 000 bps. Every sale names a
code, so a pool sells nothing until one is set. A code a sale has named is fixed: one that matches is
skipped, one that does not is refused before anything is sent.

`pool` publishes a pool's terms in the TermsBook, approves the USDC for the two locked payouts and
calls `create` with 1 HYPE, from the creator's key, then sets the plan's codes as `codes` does. The
plan file gives the menu, both stages' rules and terms, the fund window, the book's ceiling, the codes
and the terms' text.

Without --send nothing is sent and no key is read: each command prints what it would do. With it,
the transactions go out from the named keys. Testnet only; on mainnet only after an audit.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops.deploy_testnet import PLATFORM_ASSETS, big_blocks, deploy_contract, git_head, mark_incomplete, save
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
PLAN_T = f"((uint64,uint64)[],{RULES_T},{TERMS_T},{RULES_T},{TERMS_T},uint32,bool,uint16,bytes32)"
# SimPoolFactory.MIN_PARTNER_BPS, MAX_PARTNER_BPS and MAX_CODES; the tests hold them against the source.
MIN_PARTNER_BPS, MAX_PARTNER_BPS, MAX_CODES = 4000, 6000, 32
SET_CODE = "setCode(bytes32,address,uint16)"
CODE_OF = ["address", "uint16", "bool", "bool", "uint256"]
ZERO = "0x" + "00" * 20


def asset_list(names: list[str]) -> list[tuple[str, int, int]]:
    """Each named asset's id and leverage cap, for setAsset. Refuses one without a cap."""
    out = []
    for name in names:
        if name not in PLATFORM_ASSETS:
            raise SystemExit(f"{name} is not a testnet platform asset ({', '.join(PLATFORM_ASSETS)})")
        cap = ASSET_LEVERAGE_X100.get(name)
        if not cap:
            raise SystemExit(f"{name} has no leverage cap in ops/leverage.py")
        out.append((name, PLATFORM_ASSETS[name], cap))
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
            raw["fund_window_s"], raw["from_book"], raw["max_worse_bps"], terms_hash)


def code_hash(text: str) -> bytes:
    """What a pool keys a code by, and a sale names: the keccak256 of its text."""
    return keccak(text=text)


def codes_of(raw: dict, creator: str) -> list[tuple[str, bytes, str, int]]:
    """The plan's codes as (text, hash, partner, bps), checked as the pool checks them. "creator" as a partner is
    the creator's own address: a code with no share, for sales the platform makes itself."""
    out, seen = [], set()
    for entry in raw.get("codes", []):
        text = entry["code"]
        partner = creator if entry["partner"] == "creator" else entry["partner"]
        partner, bps = to_checksum_address(partner), int(entry["bps"])
        if not text or text in seen:
            raise SystemExit(f"code {text!r}: empty or twice in the plan")
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
    """The codes still to send. `on_chain(hash)` is the pool's codeOf: a code a sale named is fixed, so one that
    matches is skipped and one that does not stops everything before a transaction."""
    out = []
    for text, h, partner, bps in codes:
        have, have_bps, used = on_chain(h)[:3]
        same = to_checksum_address(have) == partner and int(have_bps) == bps
        if used and not same:
            raise SystemExit(f"code {text!r} is fixed on chain as {have} {have_bps} bps: a sale named it")
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


def locked_of(raw: dict) -> int:
    """The USDC a pool locks at opening: LOCKED_PAYOUTS payouts of the largest size (6 decimals)."""
    largest = max(usdc_units(m["size_usdc"]) for m in raw["menu"])
    return largest * PAYOUT_BPS // 10_000 * LOCKED_PAYOUTS


def deploy(args) -> int:
    assets = asset_list(args.assets.split(","))
    out = ROOT / "deployments" / f"testnet-{args.label}.json"
    if out.exists():
        raise SystemExit(f"{out.relative_to(ROOT)} exists; pick another label")
    filler, checker = to_checksum_address(args.filler), to_checksum_address(args.checker)
    if filler == checker:
        raise SystemExit("the filler and the checker are two keys")
    print(f"deploy_sim deploy: label {args.label}, USDC {c.TESTNET_USDC_ERC20} (Circle's, EIP-3009)")
    print("  would deploy     PaperExam, SimPool, TermsBook, SimPoolFactory(usdc, exam, pool, operator)  (big blocks on, then off)")
    print(f"  would configure  setFiller {filler}, setChecker {checker}, setFeeRecipient {args.fee_recipient or 'the operator'}, "
          f"setTermsBook, setAsset " + ", ".join(f"{n} ({i}) {cap / 100:g}x" for n, i, cap in assets))
    print(f"  would write      deployments/testnet-{args.label}.json")
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    c.assert_testnet()
    op = c.account(args.operator)
    recipient = to_checksum_address(args.fee_recipient) if args.fee_recipient else op.address
    if op.address in (filler, checker):
        raise SystemExit("the operator is neither the filler nor the checker")
    demo3 = deployments.load("v3-demo3")
    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": git_head(), "status": "deploying",
                    "deployer": op.address, "operator": op.address, "usdc": c.TESTNET_USDC_ERC20,
                    "filler": filler, "checker": checker, "fee_recipient": recipient,
                    "platform_assets": {n: i for n, i, _ in assets}, "asset_caps_x100": {n: cap for n, _, cap in assets},
                    "keeper_nodes": demo3["keeper_nodes"], "tx": {}}
    big_blocks(op, True)
    try:
        try:
            exam, rcpt = deploy_contract(op, out, record, "PaperExamImpl", "PaperExam", [], [])
            record["block"] = int(rcpt["blockNumber"], 16)
            pool, _ = deploy_contract(op, out, record, "SimPoolImpl", "SimPool", [], [])
            book, _ = deploy_contract(op, out, record, "TermsBook", "TermsBook", [], [])
            factory, _ = deploy_contract(op, out, record, "SimPoolFactory", "SimPoolFactory",
                                         ["address", "address", "address", "address"],
                                         [c.TESTNET_USDC_ERC20, exam, pool, op.address])
        finally:
            big_blocks(op, False)
        record["status"] = "configuring"
        save(out, record)
        steps = [("setFiller", "setFiller(address)", ["address"], [filler]),
                 ("setChecker", "setChecker(address)", ["address"], [checker]),
                 ("setFeeRecipient", "setFeeRecipient(address)", ["address"], [recipient]),
                 ("setTermsBook", "setTermsBook(address)", ["address"], [book])]
        steps += [(f"setAsset_{n}", "setAsset(uint32,uint32)", ["uint32", "uint32"], [i, cap]) for n, i, cap in assets]
        for name, sig, types, values in steps:
            record["tx"][name] = c.transact(op, factory, sig, types, values)["transactionHash"]
            save(out, record)
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
    text = raw["terms_text"]
    terms_hash = keccak(text=text)
    plan = plan_of(raw, terms_hash)
    codes = codes_of(raw, c.address_of(args.creator))
    locked = locked_of(raw)
    factory = record.get("SimPoolFactory", "(none in this record)")
    print(f"deploy_sim pool: factory {factory}, {len(plan[0])} sizes "
          + ", ".join(f"{m[0] / 1e6:g} for {m[1] / 1e6:g}" for m in plan[0]) + " USDC")
    print(f"  locks            {locked / 1e6:g} USDC (two payouts of the largest size), pays 1 HYPE")
    print(f"  book             {'at the book, ceiling ' + str(plan[7]) + ' bp' if plan[6] else 'at the oracle'}; "
          f"fill window {plan[2][7]} blocks")
    print_codes(codes)
    print(f"  terms            {len(text)} bytes, keccak 0x{terms_hash.hex()}")
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    c.assert_testnet()
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


def codes(args) -> int:
    pool_ = to_checksum_address(args.pool)
    codes_ = codes_of(json.loads(pathlib.Path(args.plan).read_text()), c.address_of(args.creator))
    print(f"deploy_sim codes: pool {pool_}, from {args.creator}")
    print_codes(codes_)
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    c.assert_testnet()
    send_codes(c.account(args.creator), pool_, codes_)
    return 0


def buy(args) -> int:
    record = deployments.load(args.deployment)
    pool_ = to_checksum_address(args.pool)
    print(f"deploy_sim buy: pool {pool_}, line {args.line}, code {args.code!r}, from {args.trader}")
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    c.assert_testnet()
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
    d.add_argument("--fee-recipient")
    d.add_argument("--assets", default="BTC,ETH,SOL")
    d.add_argument("--send", action="store_true")
    q = sub.add_parser("pool")
    q.add_argument("--deployment", required=True)
    q.add_argument("--plan", required=True)
    q.add_argument("--creator", default="creator")
    q.add_argument("--send", action="store_true")
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
    k.add_argument("--send", action="store_true")
    args = p.parse_args(argv)
    return {"deploy": deploy, "pool": pool, "buy": buy, "codes": codes}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
