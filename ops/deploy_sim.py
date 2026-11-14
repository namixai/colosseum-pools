"""Deploys the simulated pool's contracts on testnet, and opens a pool on them (docs/SIM-POOL.md).

    spike/.venv/bin/python -m ops.deploy_sim deploy --label sim1 --filler 0x.. --checker 0x.. \\
        --closer 0x..                                                                                  # the plan
    ... deploy ... --send                                                                              # sends
    spike/.venv/bin/python -m ops.deploy_sim pool --deployment sim1 \\
        --plan deployments/plans/sim-first-pool.json --creator creator                                  # the plan
    ... pool ... --send                                                                                # sends
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
and the SimPoolFactory on HyperEVM testnet, names the filler, the checker, the closer (the platform's
key that alone may close a pool before its 6 months, 184 days; four keys, none of them the operator, who
opens our pools) and the platform's recipient, and lists the assets with their leverage caps from ops/leverage.py (HYPE is left out:
the platform's decision of 11 November 2026). It writes deployments/testnet-<label>.json, with the
same keeper nodes as demo3, so the host's keeper unit can run on it.

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

`pool` publishes a pool's terms in the TermsBook, approves the USDC for the two locked payouts and
calls `create` with 1 HYPE, from the creator's key, then sets the plan's codes as `codes` does. The
plan file gives the menu, both stages' rules and terms, the fund window, the book's ceiling, the codes
and the terms' text.

Without --send nothing is sent and no key is read: each command prints what it would do.

With --signer ADDRESS (pool, codes, close, finish, request, withdraw, cancel, approver, approve) the call is
from an address that signs elsewhere: our pool's creator is a Safe with two signatures, its approver of payouts
a Ledger. The command reads the chain as that address, checks as it would for a key, and prints the calls in
order as the Safe's Transaction Builder takes them (to, value, data); it sends nothing and reads no key. With it,
the transactions go out from the named keys. Testnet only; on mainnet only after an audit.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

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
    closer = to_checksum_address(args.closer)
    # Before anything is deployed: a zero key would pass the checks below and fail only at setCloser, four
    # contracts later (review of #282).
    if any(int(a, 16) == 0 for a in (filler, checker, closer)):
        raise SystemExit("the filler, the checker and the closer are real keys, none of them 0x0")
    if len({filler, checker, closer}) != 3:
        raise SystemExit("the filler, the checker and the closer are three keys")
    print(f"deploy_sim deploy: label {args.label}, USDC {c.TESTNET_USDC_ERC20} (Circle's, EIP-3009)")
    print("  would deploy     PaperExam, SimPoolStops, SimPoolSetup, SimPool (linked to both), TermsBook, "
          "SimPoolFactory(usdc, exam, pool, operator)  (big blocks on, then off)")
    print(f"  would configure  setFiller {filler}, setChecker {checker}, setCloser {closer}, setFeeRecipient {args.fee_recipient or 'the operator'}, "
          f"setTermsBook, setAsset " + ", ".join(f"{n} ({i}) {cap / 100:g}x" for n, i, cap in assets))
    print(f"  would write      deployments/testnet-{args.label}.json")
    if not args.send:
        print("deploy_sim: nothing was sent (--send sends).")
        return 0
    c.assert_testnet()
    op = c.account(args.operator)
    recipient = to_checksum_address(args.fee_recipient) if args.fee_recipient else op.address
    if op.address in (filler, checker, closer):
        raise SystemExit("the operator is neither the filler, the checker nor the closer: it opens our pools, and a "
                         "pool's creator is none of them")
    demo3 = deployments.load("v3-demo3")
    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": git_head(), "status": "deploying",
                    "deployer": op.address, "operator": op.address, "usdc": c.TESTNET_USDC_ERC20,
                    "filler": filler, "checker": checker, "closer": closer, "fee_recipient": recipient,
                    "platform_assets": {n: i for n, i, _ in assets}, "asset_caps_x100": {n: cap for n, _, cap in assets},
                    "keeper_nodes": demo3["keeper_nodes"], "tx": {}}
    big_blocks(op, True)
    try:
        try:
            exam, rcpt = deploy_contract(op, out, record, "PaperExamImpl", "PaperExam", [], [])
            record["block"] = int(rcpt["blockNumber"], 16)
            # SimPool's stops and the copy of its plan are libraries it calls (EIP-170): deployed first, and linked.
            stops, _ = deploy_contract(op, out, record, "SimPoolStops", "SimPoolStops", [], [])
            setup, _ = deploy_contract(op, out, record, "SimPoolSetup", "SimPoolSetup", [], [])
            pool, _ = deploy_contract(op, out, record, "SimPoolImpl", "SimPool", [], [],
                                      {"SimPoolStops": stops, "SimPoolSetup": setup})
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
                 ("setCloser", "setCloser(address)", ["address"], [closer]),
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
    codes = codes_of(raw, signer_of(args, "creator"))
    locked = locked_of(raw)
    factory = record.get("SimPoolFactory", "(none in this record)")
    print(f"deploy_sim pool: factory {factory}, {len(plan[0])} sizes "
          + ", ".join(f"{m[0] / 1e6:g} for {m[1] / 1e6:g}" for m in plan[0]) + " USDC")
    print(f"  locks            {locked / 1e6:g} USDC (two payouts of the largest size), pays 1 HYPE")
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
    c.assert_testnet()
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
    d.add_argument("--closer", required=True, help="the platform's key that alone may close a pool before its term")
    d.add_argument("--fee-recipient")
    d.add_argument("--assets", default="BTC,ETH,SOL")
    d.add_argument("--send", action="store_true")
    q = sub.add_parser("pool")
    q.add_argument("--deployment", required=True)
    q.add_argument("--plan", required=True)
    q.add_argument("--creator", default="creator")
    q.add_argument("--signer", help="the creator as an address that signs elsewhere (our Safe): print the calls, send nothing")
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
    args = p.parse_args(argv)
    return {"deploy": deploy, "pool": pool, "buy": buy, "codes": codes, "close": close, "finish": finish,
            "request": request, "withdraw": withdraw, "cancel": cancel, "approver": approver_cmd,
            "approve": approve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
