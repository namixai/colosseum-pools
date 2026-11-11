#!/usr/bin/env python3
"""What is left on the challenge accounts that have settled, read from the chain. It only reads and needs no key.

    spike/.venv/bin/python scripts/settled_accounts.py [--out FILE] [--md FILE] [--red-above USDC]

A settled account can still receive money: a late fill, a send of its own that landed after the settlement, a
donation. `ChallengeAccount.sweep()` sends whatever is there back to the pool, and anyone may call it. This reads
whether there is anything for it to send. With `--red-above USDC` it also exits 2 when a settled account holds that
much or more -- its spot USDC on HyperCore, what its perp accounts would let it withdraw and its USDC on HyperEVM,
together -- which is what the weekly watch (.github/workflows/settled-accounts-watch.yml) goes red on.
It also reads who operates each deployment's key registry, operator() and pendingOperator(), and exits 1 when
either is not what the record says: the deployer and nobody pending, or, after a handover at the deploy
(registry_operator), the deployer and the address given while it is pending, that address once accepted. The
weekly watch goes red on that too; money left behind (2) is said first. A deployment with a key binder
(src/KeyBinder.sol) has no registry: for it the watch reads the binder's owner(), pendingOwner(), publisher(),
guardian() and paused(), and the factory's operator() and pendingOperator(), and exits 1 the same way when any is
not what the record's `key_binder` and `factory_operator` say, or when the binder is paused.

Every account a factory creates, a pool or a challenge, is a clone made with CREATE, so its address follows from the
factory's address and nonce. The accounts are found that way, not from the factory's logs: HyperEVM answers
eth_getLogs over 50 blocks at most and times old ranges out. For each challenge it reads, at one block:
- on HyperEVM: `status()`, `returnAt()`, the HYPE balance and the deployment's USDC balance;
- on HyperCore: the spot USDC balance, the perp account's value and withdrawable amount (the pool's dex too, on a
  builder dex), and every non-funding ledger update with its time.

It reads the network COLOSSEUM_NETWORK names, testnet unless it names mainnet (spike/hlspike/network.py), and the
deployments deployments/LIVE.json lists for that network: the challenges of its live and testers entries, and the
seats of the shared pool the site opens (watched()).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.request
from decimal import Decimal

from eth_utils import keccak, to_checksum_address

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from spike.hlspike import network as networks  # noqa: E402

# The network COLOSSEUM_NETWORK names (testnet unless it names mainnet), and its public RPC and info API.
NET = networks.select()
RPC = NET.rpc_url
INFO = NET.api_url + "/info"
LIVE = ROOT / "deployments" / "LIVE.json"
# ChallengeAccount.Status, in its order (src/ChallengeAccount.sol).
STATUS = ("None", "Created", "Active", "Breached", "Expired", "Forfeited", "Passed", "Aborted", "Settled")
# Past the factory's nonce: a clone made while this runs is read too, or found missing.
SPARE = 2


def watched(net=None, live: dict | None = None) -> tuple[list[str], str | None]:
    """The records whose challenges are read and the record of the shared pool whose seats are marked, from
    deployments/LIVE.json: the network's `live` and `testers` entries in its order, and its first `shared` entry on
    the live one's factory, the shared pool the site opens (scripts/app_config.py). None for a network with no
    shared pool there."""
    net = net or NET
    entries = [e for e in (live or json.loads(LIVE.read_text()))["deployments"] if e["network"] == net.name]
    records = [e["record"] for e in entries if e["role"] in ("live", "testers")]
    if not records:
        raise SystemExit(f"nothing to read on {net.name}: deployments/LIVE.json has no live or testers entry")
    factories = {e["factory"] for e in entries if e["role"] == "live"}
    shared = next((e["record"] for e in entries if e["role"] == "shared" and e["factory"] in factories), None)
    return records, shared


def post(url: str, body: dict, tries: int = 5):
    data = json.dumps(body).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data, {"Content-Type": "application/json",
                                                     "User-Agent": "settled-accounts"})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)
        except (OSError, ValueError):  # a network failure, or an answer that is not JSON (JSONDecodeError)
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def rpc(method: str, params: list):
    answer = post(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in answer:
        raise RuntimeError(f"{method}: {answer['error']}")
    return answer["result"]


def call(to: str, signature: str, args: tuple[str, ...] = (), block: str = "latest") -> str:
    data = "0x" + keccak(text=signature)[:4].hex() + "".join(a.lower().removeprefix("0x").rjust(64, "0") for a in args)
    return rpc("eth_call", [{"to": to, "data": data}, block])


def words(answer: str) -> list[str]:
    body = answer.removeprefix("0x")
    return [body[i:i + 64] for i in range(0, len(body), 64)]


def uint(answer: str, index: int = 0) -> int:
    return int(words(answer)[index], 16)


def address(answer: str, index: int = 0) -> str:
    return to_checksum_address("0x" + words(answer)[index][-40:])


def created_by(sender: str, nonce: int) -> str:
    """The address CREATE gives the `nonce`-th contract `sender` makes: keccak(rlp([sender, nonce]))[12:]."""
    if nonce == 0:
        encoded_nonce = b"\x80"
    elif nonce < 0x80:
        encoded_nonce = bytes([nonce])
    else:
        raw = nonce.to_bytes((nonce.bit_length() + 7) // 8, "big")
        encoded_nonce = bytes([0x80 + len(raw)]) + raw
    payload = b"\x94" + bytes.fromhex(sender.removeprefix("0x")) + encoded_nonce
    return to_checksum_address(keccak(bytes([0xC0 + len(payload)]) + payload)[12:])


def core(account: str, dexes: list[str]) -> dict:
    spot = post(INFO, {"type": "spotClearinghouseState", "user": account})
    usdc = next((b for b in (spot.get("balances") or []) if b.get("coin") == "USDC"), {})
    perps = {}
    for dex in dexes:
        body = {"type": "clearinghouseState", "user": account}
        if dex:
            body["dex"] = dex
        state = post(INFO, body)
        perps[dex or "main"] = {"account_value": (state.get("marginSummary") or {}).get("accountValue", "0"),
                                "withdrawable": state.get("withdrawable") or "0",
                                "positions": len(state.get("assetPositions") or [])}
    ledger = post(INFO, {"type": "userNonFundingLedgerUpdates", "user": account, "startTime": 0})
    return {"spot_usdc": usdc.get("total") or "0", "spot_usdc_hold": usdc.get("hold") or "0", "perp": perps,
            "ledger": [movement(account, u) for u in (ledger if isinstance(ledger, list) else [])]}


def movement(account: str, update: dict) -> dict:
    """One ledger update, with its direction for this account and not the other party's address."""
    delta = update.get("delta") or {}
    to = (delta.get("destination") or "").lower()
    sender = (delta.get("user") or "").lower()
    direction = "in" if to == account.lower() else "out" if sender == account.lower() or to else "own"
    return {"time_ms": update.get("time") or 0, "type": delta.get("type"), "direction": direction,
            "usdc": delta.get("usdc") or delta.get("amount")}


def read(block: str, dex_names: list[str], seats: set[str], record: dict) -> dict:
    factory = to_checksum_address(record["PoolFactory"])
    usdc = address(call(factory, "usdc()", block=block))
    nonce = int(rpc("eth_getTransactionCount", [factory, block]), 16)
    pools, challenges = [], []
    for n in range(1, nonce + SPARE):
        account = created_by(factory, n)
        if rpc("eth_getCode", [account, block]) in ("0x", None):
            continue
        if uint(call(factory, "isPool(address)", (account,), block)) == 1:
            pools.append({"nonce": n, "pool": account, "shared_pool_seat": account in seats})
            continue
        pool = address(call(account, "pool()", block=block))
        dexes = [""]
        if "PaperExamImpl" in record:  # a v3 deployment: its pools trade on a builder dex
            index = uint(call(pool, "dex()", block=block))
            if not 0 <= index < len(dex_names) or not dex_names[index]:
                # Its perp account there would go unread, and money on it unseen: stop rather than guess.
                raise RuntimeError(f"{pool} trades on dex {index}, which perpDexs does not name")
            dexes.append(dex_names[index])
        code = uint(call(account, "status()", block=block))
        # A value this list does not have is kept as its number, not dropped and not a crash.
        status = STATUS[code] if code < len(STATUS) else f"Unknown({code})"
        row = {"nonce": n, "challenge": account, "pool": pool, "shared_pool_seat": pool in seats,
               "status": status,
               "created_at": uint(call(account, "createdAt()", block=block)),
               "return_at": uint(call(account, "returnAt()", block=block)),
               "evm_hype_wei": str(int(rpc("eth_getBalance", [account, block]), 16)),
               "evm_usdc": str(uint(call(usdc, "balanceOf(address)", (account,), block)))}
        row.update(core(account, dexes))
        challenges.append(row)
    registry = None
    if record.get("KeyRegistry"):
        # Who operates the registry that gives every new account its key, on chain and as the record says: a
        # handover at the deploy (--operator) waits for the address the owner gave to accept it.
        reg = to_checksum_address(record["KeyRegistry"])
        handover = record.get("registry_operator")
        registry = {"address": reg, "operator": address(call(reg, "operator()", block=block)),
                    "pending_operator": address(call(reg, "pendingOperator()", block=block)),
                    "record_says": handover or {"operator": record.get("deployer")}}
    binder = None
    if record.get("KeyBinder"):
        # Who owns the binder that binds every new account's key, who signs for it and who can stop it, and who
        # operates the factory: on chain, and as the record says (ops/binder_deploy.py).
        kb = to_checksum_address(record["KeyBinder"])
        binder = {"address": kb, "owner": address(call(kb, "owner()", block=block)),
                  "pending_owner": address(call(kb, "pendingOwner()", block=block)),
                  "publisher": address(call(kb, "publisher()", block=block)),
                  "guardian": address(call(kb, "guardian()", block=block)),
                  "paused": uint(call(kb, "paused()", block=block)) == 1,
                  "max_binds_per_day": uint(call(kb, "maxBindsPerDay()", block=block)),
                  "renew_delay": uint(call(kb, "renewDelay()", block=block)),
                  "factory_operator": address(call(factory, "operator()", block=block)),
                  "factory_pending_operator": address(call(factory, "pendingOperator()", block=block)),
                  "record_says": {"deployer": record.get("deployer"), "key_binder": record.get("key_binder") or {},
                                  "factory_operator": record.get("factory_operator")}}
    return {"label": record.get("label"), "factory": factory, "factory_nonce": nonce, "usdc": usdc, "registry": registry,
            "binder": binder, "pools": pools, "challenges": challenges}


USDC_EVM_DECIMALS = 6


def usdc_left(challenge: dict) -> Decimal:
    """USDC a challenge account still holds, wherever it is: spot on HyperCore, withdrawable on each perp account,
    and the token on HyperEVM. Hold on spot is part of the spot total. HYPE is not USDC and is not counted; the
    reading keeps it for the eye. In Decimal, so that a threshold is met at equality rather than missed by a
    rounding."""
    return (Decimal(challenge["spot_usdc"] or 0)
            + sum((Decimal(p["withdrawable"] or 0) for p in challenge["perp"].values()), Decimal(0))
            + Decimal(int(challenge["evm_usdc"] or 0)) / 10 ** USDC_EVM_DECIMALS)


def left_behind(result: dict, minimum: Decimal) -> list[dict]:
    """The settled accounts holding `minimum` USDC or more, with what they hold: the ones `sweep()` has something
    to send back for. An account that has not settled still has an owner for its money."""
    found = []
    for deployment in result["deployments"]:
        for c in deployment["challenges"]:
            if c["status"] == "Settled" and usdc_left(c) >= minimum:
                found.append({"deployment": deployment["label"], "challenge": c["challenge"], "pool": c["pool"],
                              "usdc": usdc_left(c)})
    return found


def verdict(result: dict, red_above: Decimal | None, err=None) -> int:
    """The exit code `--red-above` asks for: 2 when a settled account holds that much USDC or more, each such
    account named on stderr; else 0. With no threshold, always 0."""
    if red_above is None:
        return 0
    left = left_behind(result, red_above)
    for row in left:
        amount = format(row["usdc"].normalize(), "f")  # 3, not 3.0 or 3E+0
        print(f"left behind: {amount} USDC on {row['challenge']} ({row['deployment']}, pool {row['pool']})",
              file=err or sys.stderr)
    return 2 if left else 0


ZERO = "0x" + "00" * 20


def expected_registry(record_says: dict) -> tuple[str, str] | None:
    """Who the record says operates the registry, and who it says waits to: the deployer and nobody without a
    handover; with one (--operator), the deployer and the address given while it is pending, that address and
    nobody once accepted. None for a handover that says neither, or lacks its addresses: a broken record."""
    if not {"from", "to", "state"} & set(record_says):
        return record_says.get("operator") or ZERO, ZERO
    state, frm, to = record_says.get("state"), record_says.get("from"), record_says.get("to")
    if state not in ("pending", "accepted") or not isinstance(frm, str) or not isinstance(to, str):
        return None
    return (to, ZERO) if state == "accepted" else (frm, to)


def registry_mismatches(result: dict) -> list[str]:
    """Each deployment whose registry, on chain, has another operator or another pending one than its record says."""
    out = []
    for deployment in result["deployments"]:
        reg = deployment.get("registry")
        if not reg:
            continue
        expected = expected_registry(reg["record_says"])
        if expected is None:
            out.append(f"registry operator does not match the record: {deployment['label']} {reg['address']}: the "
                       f"record's registry_operator {reg['record_says']} is not a pending or accepted handover")
            continue
        operator, pending = expected
        if reg["operator"].lower() != operator.lower() or reg["pending_operator"].lower() != pending.lower():
            out.append(f"registry operator does not match the record: {deployment['label']} {reg['address']}: "
                       f"operator {reg['operator']} and pending {reg['pending_operator']} on chain, "
                       f"{operator} and {pending} in the record")
    return out


def binder_handover(says: dict | None, deployer: str | None) -> dict:
    """A binder's or a factory's handover as the record says it, in the registry's terms: the deployer and nobody
    before one is sent ("the deployer", "named, not sent", or nothing said)."""
    if not says or says.get("state") in ("the deployer", "named, not sent"):
        return {"operator": (says or {}).get("from") or deployer}
    return says


def binder_mismatches(result: dict) -> list[str]:
    """Each deployment whose key binder or factory, on chain, is not what its record says, and each paused binder."""
    out = []
    for deployment in result["deployments"]:
        b = deployment.get("binder")
        if not b:
            continue
        says = b["record_says"]
        where = f"{deployment['label']} {b['address']}"
        for what, held, pending, handover in (
                ("key binder owner", b["owner"], b["pending_owner"], says["key_binder"].get("owner")),
                ("factory operator", b["factory_operator"], b["factory_pending_operator"], says.get("factory_operator"))):
            expected = expected_registry(binder_handover(handover, says.get("deployer")))
            if expected is None:
                out.append(f"{what} does not match the record: {where}: the record's {handover} is not a handover")
            elif held.lower() != expected[0].lower() or pending.lower() != expected[1].lower():
                out.append(f"{what} does not match the record: {where}: {held} and pending {pending} on chain, "
                           f"{expected[0]} and {expected[1]} in the record")
        publisher = (says["key_binder"].get("publisher") or {}).get("address") or ZERO
        guardian = says["key_binder"].get("guardian") or ZERO
        for what, held, expected in (("publisher", b["publisher"], publisher), ("guardian", b["guardian"], guardian)):
            if held.lower() != expected.lower():
                out.append(f"key binder {what} does not match the record: {where}: {held} on chain, {expected} in the record")
        if b["paused"]:
            out.append(f"key binder is paused: {where}: no key is bound until the owner lifts it")
        # The fuse and the renewal's wait, as deployed or as the record was last told: a guardian's cut, to zero
        # above all (no key bound at all), shows here until the record says it.
        for what, held, field in (("daily limit", b.get("max_binds_per_day"), "max_binds_per_day"),
                                  ("renewal delay", b.get("renew_delay"), "renew_delay")):
            recorded = says["key_binder"].get(field)
            if held is not None and recorded is not None and held != recorded:
                out.append(f"key binder {what} does not match the record: {where}: {held} on chain, {recorded} in the record")
        if b.get("max_binds_per_day") == 0:
            out.append(f"key binder binds nothing: {where}: its daily limit is 0")
    return out


def exit_code(result: dict, red_above: Decimal | None, err=None) -> int:
    """2 when a settled account holds what --red-above names (verdict); else 1 when a registry's operator, or a key
    binder's owner, publisher or guardian or its factory's operator, is not the one its record says, or the binder
    is paused, each named on stderr; else 0."""
    code = verdict(result, red_above, err)
    mismatches = registry_mismatches(result) + binder_mismatches(result)
    for line in mismatches:
        print(line, file=err or sys.stderr)
    return code or (1 if mismatches else 0)


def utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(seconds)) if seconds else "-"


def table(result: dict) -> str:
    """The reading as a Markdown table: one row a challenge, the money left on it, where and since when."""
    seat_no = {a: i + 1 for i, a in enumerate((result.get("shared_pool") or {}).get("seats", []))}
    rows = ["| Deployment | Pool | Challenge | Status | `returnAt` (UTC) | Last move out on HyperCore (UTC) "
            "| In after it (USDC) | Spot USDC | Perp withdrawable | HYPE on HyperEVM (wei) | USDC on HyperEVM |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for deployment in result["deployments"]:
        for c in deployment["challenges"]:
            outs = [u["time_ms"] for u in c["ledger"] if u["direction"] == "out"]
            last_out = max(outs, default=0)
            # What came in after the last move out: only a settled account's figure says something is left over.
            arrived = sum(float(u["usdc"] or 0) for u in c["ledger"] if u["direction"] == "in" and u["time_ms"] > last_out)
            after = f"{arrived:g}" if c["status"] == "Settled" else "-"
            pool = f"shared pool, seat {seat_no[c['pool']]}" if c["pool"] in seat_no else f"`{c['pool']}`"
            perp = ", ".join(f"{k} {v['withdrawable']}" for k, v in c["perp"].items())
            rows.append(f"| {deployment['label']} | {pool} | `{c['challenge']}` | {c['status']} | {utc(c['return_at'])} "
                        f"| {utc(last_out / 1000)} | {after} | {c['spot_usdc']} | {perp} | {c['evm_hype_wei']} "
                        f"| {c['evm_usdc']} |")
    return "\n".join(rows) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=pathlib.Path, help="write the JSON here as well as to stdout")
    parser.add_argument("--md", type=pathlib.Path, help="write the reading as a Markdown table here")
    parser.add_argument("--red-above", type=Decimal, metavar="USDC",
                        help="exit 2 when a settled account holds this much USDC or more")
    options = parser.parse_args()
    out_path = options.out
    head = rpc("eth_getBlockByNumber", ["latest", False])
    block = int(head["number"], 16)
    at = int(head["timestamp"], 16)
    pin = hex(block)
    records, shared_record = watched()
    shared, seats = None, []
    if shared_record:
        shared = json.loads((ROOT / shared_record).read_text())
        answer = call(shared["SharedPool"], "seats()", block=pin)
        seats = [address(answer, 2 + i) for i in range(uint(answer, 1))]  # in the pool's own order
    dex_names = [(d or {}).get("name", "") for d in post(INFO, {"type": "perpDexs"})]
    result = {"read_at_block": block, "read_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
              "rpc": RPC, "info": INFO,
              "shared_pool": {"address": to_checksum_address(shared["SharedPool"]), "seats": seats} if shared else None,
              "deployments": [read(pin, dex_names, set(seats), json.loads((ROOT / r).read_text())) for r in records]}
    text = json.dumps(result, indent=2) + "\n"
    if out_path:
        out_path.write_text(text)
    if options.md:
        options.md.write_text(f"Read at block {block}, {result['read_at']}.\n\n" + table(result))
    print(text, end="")
    code = exit_code(result, options.red_above)
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
