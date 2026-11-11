"""Deploy factory v3 in one command: its key binder, the account implementations, the factory, the
paper exam, the reader, and on testnet the demo's own USDC. Then the factory's and the binder's
settings, and the handover of both to their owner. The pools team, 19-21 October 2026; the key
binder, 11 November 2026 (docs/KEY-BINDING.md).

    spike/.venv/bin/python ops/deploy_v3.py --label demo4 --faucet 0x... --safe 0xSafe... \
        --publisher 0x... --pcr-hash 0x... --attestation-hash 0x... --guardian 0x... --dry-run
    spike/.venv/bin/python ops/deploy_v3.py --label v3 --network mainnet --dry-run --safe 0xSafe... ...

What it deploys, in order (big blocks on, then off):
- DemoUsdc (testnet only): the token prices and payouts are in. Its operator, the deployer, mints
  any amount; --faucet mints up to --faucet-daily a UTC day. The chain's testnet USDC is not ours
  to mint.
- KeyBinder(deployer, --max-binds-per-day, --renew-delay), AccountOps (the library the accounts link
  to), Pool, ChallengeAccount.
- PoolFactory(binder, Pool, ChallengeAccount, operator, token).
- PaperExam: the exam implementation every MultiPool sells.
- MultiPoolReader.

What it sets, in order:
- setAccountSource: the binder takes requests from this factory's accounts alone; setPublisher and
  setGuardian when given (ops/binder_deploy.py says what each flag is).
- setPlatformAssets and setFundedAssets: BTC, ETH and SOL for exams; BTC and ETH for a funded
  account (deep audit N6).
- setExamImpl: the paper exam is the one a shares contract binds (cloud audit, section 6).
- setSharesCode: the hash of out/PoolShares.json's creation code, the one code the factory creates
  shares contracts from.
- setChallengeFee: CHALLENGE_FEE to the operator.
- setFaucet on the token: --faucet and --faucet-daily.
- With --safe, last: transferOwnership on the binder and transferOperator on the factory, to the
  owner (a Safe), which accepts each from the Safe itself. No agent key is published: a key is bound
  to an account that asks for one, on the publisher's signature.

Writes deployments/<network>-v3-<label>.json after every transaction, a `status` field included,
as ops/deploy_testnet.py does. The record holds every address and transaction, and the keccak of
each contract's creation code (out/) and of its code on chain. It also names the network's
parameters: chain id, token, CoreWriter and the precompiles.

Each network's chain id, RPC, API and USDC come from spike/hlspike/network.py. --network mainnet
--dry-run prints the whole plan for mainnet and sends nothing: the chain's USDC and no DemoUsdc, the
address of each contract when --deployer names the address the owner will deploy from (read from
its nonce on mainnet), mainnet's perp numbers for the listed assets (read from its meta), the gas
and its price today (scripts/mainnet_gas.py) and the record it would write. It only reads public
endpoints, and holds no key. A mainnet run without --dry-run is refused here, before anything is
read: sending to mainnet is a decision of its own.

    spike/.venv/bin/python ops/deploy_v3.py --label v3 --network mainnet --dry-run --deployer 0x...
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from eth_utils import keccak, to_checksum_address  # noqa: E402

from hlspike import common as c  # noqa: E402
from ops.deploy_multi import FUNDED_ASSETS, shares_code  # noqa: E402
from ops import binder_deploy as kb  # noqa: E402
from ops.plan_reads import created_by, perp_numbers, read_rpc  # noqa: E402
from ops.deploy_testnet import (  # noqa: E402
    CHALLENGE_FEE,
    PLATFORM_ASSETS,
    big_blocks,
    check_assets,
    deploy_contract,
    git_head,
    mark_incomplete,
    save,
)

# What a network gives the contracts, from spike/hlspike/network.py. Testnet's prices are in the demo's
# own token, deployed first (usdc None); mainnet's in the chain's USDC. Precompiles and CoreWriter are
# the same on both HyperEVM chains; they are named so that the record says what the build was
# deployed against.
PRECOMPILES = "0x0000000000000000000000000000000000000800"
DEMO_TOKEN = {"testnet"}
NETWORKS = {
    name: {"chain_id": net.chain_id, "usdc": None if name in DEMO_TOKEN else net.usdc_erc20,
           "core_writer": c.CORE_WRITER, "precompiles": PRECOMPILES, "rpc": net.rpc_url, "api": net.api_url}
    for name, net in c.network.NETWORKS.items()
}
GAS = ROOT / "scripts" / "mainnet_gas.py"
# What the demo's faucet may mint a UTC day unless told otherwise: a thousand dUSDC.
FAUCET_DAILY = 1_000 * 10**6
# The contracts, in the order they are deployed: (record key, contract).
CONTRACTS = (("DemoUsdc", "DemoUsdc"), ("KeyBinder", "KeyBinder"), ("AccountOps", "AccountOps"), ("PoolImpl", "Pool"),
             ("ChallengeAccountImpl", "ChallengeAccount"), ("PoolFactory", "PoolFactory"),
             ("PaperExamImpl", "PaperExam"), ("MultiPoolReader", "MultiPoolReader"))


def initcode_keccak(contract: str) -> str:
    """The keccak of a contract's creation code as `forge build` left it in out/, a library it links
    to at the zero address (its address is in the record)."""
    art = c.artifact(contract)
    libs = {name: "0x" + "00" * 20 for refs in (art["bytecode"].get("linkReferences") or {}).values() for name in refs}
    return "0x" + keccak(c.link(art, libs)).hex()


def code_keccak(addr: str) -> str:
    """The keccak of the code at `addr` on chain, immutables and all."""
    return "0x" + keccak(bytes.fromhex(c.rpc("eth_getCode", [addr, "latest"]).removeprefix("0x"))).hex()


def gas_estimate() -> dict:
    """scripts/mainnet_gas.py's reading: the gas of the v3 deployment's testnet receipts at mainnet's prices now."""
    spec = importlib.util.spec_from_file_location("mainnet_gas", GAS)
    gas = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gas)
    return gas.read(ROOT / "deployments" / "testnet-v3-demo3.json")


def mainnet_plan(args, net: dict, deploys: list[str], hashes: dict, code: str, out_path: pathlib.Path,
                 binder: kb.BinderSettings) -> int:
    """The whole plan for mainnet, read from public endpoints, with nothing sent."""
    print("deploy_v3 --network mainnet --dry-run: read only; nothing was sent, and no key was used.")
    print(f"  network          mainnet, chain id {net['chain_id']}, rpc {net['rpc']}, api {net['api']}"
          "  (spike/hlspike/network.py)")
    print(f"  token            {net['usdc']}  (the chain's USDC; no DemoUsdc)")
    print(f"  CoreWriter       {net['core_writer']}, precompiles from {net['precompiles']}")
    if args.deployer:
        deployer = to_checksum_address(args.deployer)
        try:
            nonce = int(read_rpc(net["rpc"], "eth_getTransactionCount", [deployer, "latest"]), 16)
            hype = int(read_rpc(net["rpc"], "eth_getBalance", [deployer, "latest"]), 16) / 10**18
        except Exception as exc:  # a plan says what it could not read rather than stopping
            print(f"  deployer         {deployer}: nonce not read ({exc})")
            print(f"  would deploy     {', '.join(deploys)}  (addresses not read)")
        else:
            print(f"  deployer         {deployer}, the address the owner gives (--deployer): nonce {nonce}, {hype:.4f} HYPE")
            print("  would deploy     " + ", ".join(f"{k} at {created_by(deployer, nonce + i)}" for i, k in enumerate(deploys)))
            print("                   (each address holds only if the deployer sends nothing else first)")
    else:
        print(f"  would deploy     {', '.join(deploys)}  (addresses: pass --deployer, the address the owner gives)")
    print("  big blocks       on before the deployments, off after: a HyperCore action, no EVM nonce")
    print("  creation code    " + "; ".join(f"{k} {v[:18]}" for k, v in hashes.items()))
    try:
        found = perp_numbers(net["api"], PLATFORM_ASSETS)
        print("  perp numbers     " + ", ".join(f"{n} {found[n]}" if n in found else f"{n} not listed" for n in PLATFORM_ASSETS)
              + f"  (mainnet's meta, read now; funded: {', '.join(FUNDED_ASSETS)})")
    except Exception as exc:  # a plan says what it could not read rather than stopping
        print(f"  perp numbers     not read: {exc}")
    print(f"  would configure  setAccountSource, setPlatformAssets, setFundedAssets {list(FUNDED_ASSETS)}, "
          f"setExamImpl, setSharesCode {code[:18]}, setChallengeFee")
    for line in kb.plan_lines(binder):
        print(line)
    try:
        g = gas_estimate()
        dep, ten = g["deployment"], g["deployment_at_ten_times_the_price"]
        print(f"  gas              {dep['gas_total']:,} over {dep['transactions']} transactions: {dep['hype']:.4f} HYPE, "
              f"${dep['usd']:.2f} at today's prices ({ten['hype']:.4f} HYPE at ten times); big blocks for "
              f"{', '.join(dep['need_big_block']) or 'none'}  (scripts/mainnet_gas.py, read {g['read_at_utc']})")
    except Exception as exc:
        print(f"  gas              not read: {exc}")
    print(f"  would write      {out_path.relative_to(ROOT)}, after every transaction; then its entry in "
          "deployments/LIVE.json, role staged, by hand (docs/DEPLOYMENTS.md)")
    print("  would NOT send   anything: a mainnet run without --dry-run is refused")
    return 0


def plan(network: str, token: str | None) -> list[str]:
    """What a run deploys, by record key."""
    return [key for key, _ in CONTRACTS if key != "DemoUsdc" or token is None]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True)
    p.add_argument("--network", choices=sorted(NETWORKS), default="testnet")
    p.add_argument("--faucet", help="the address that mints the demo's USDC up to --faucet-daily a day (testnet)")
    p.add_argument("--faucet-daily", type=float, default=FAUCET_DAILY / 10**6, help="dUSDC a UTC day")
    p.add_argument("--dry-run", action="store_true", help="run every check and send nothing")
    p.add_argument("--deployer", help="mainnet plan: the address the owner will deploy from, to name each contract's address")
    kb.add_args(p)
    args = p.parse_args()

    net = NETWORKS[args.network]
    binder = kb.settings(args, args.network)  # before anything is read: a bad address costs nothing
    if args.network == "mainnet" and not args.dry_run:
        raise SystemExit("--network mainnet is a dry run only: this repository sends nothing to mainnet "
                         "(spike/hlspike/common.py), and opening that is a decision of its own")
    if args.faucet and args.network == "mainnet":
        raise SystemExit("--faucet is the demo token's, and mainnet takes the chain's USDC")
    faucet = to_checksum_address(args.faucet) if args.faucet else None
    daily = round(args.faucet_daily * 10**6)
    out_path = ROOT / "deployments" / f"{args.network}-v3-{args.label}.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists; pick another label")
    commit = git_head()
    subprocess.run(["forge", "build"], cwd=ROOT, check=True, capture_output=True)
    token = net["usdc"]
    deploys = plan(args.network, token)
    hashes = {key: initcode_keccak(contract) for key, contract in CONTRACTS if key in deploys}
    code = "0x" + shares_code().hex()

    if args.network == "mainnet":
        return mainnet_plan(args, net, deploys, hashes, code, out_path, binder)

    c.assert_testnet()
    assets = check_assets()
    deployer = c.account("deployer")
    if not c.core_user_exists(deployer.address):
        raise SystemExit("the deployer has no HyperCore account yet; fund it first")

    if args.dry_run:
        print("deploy_v3 --dry-run: nothing was sent.")
        print(f"  label            {args.label}  ({out_path.relative_to(ROOT)} is free)")
        print(f"  commit           {commit}")
        print(f"  deployer         {deployer.address}, on HyperCore; the operator of the factory and the token")
        print(f"  token            DemoUsdc, deployed first; faucet {faucet or 'none'}, {daily / 10**6:g} dUSDC a day")
        print(f"  would deploy     {', '.join(deploys)}  (big blocks on, then off)")
        print(f"  creation code    " + "; ".join(f"{k} {v[:18]}" for k, v in hashes.items()))
        print(f"  would configure  setAccountSource, setPlatformAssets {assets}, setFundedAssets {list(FUNDED_ASSETS)}, "
              f"setExamImpl, setSharesCode {code[:18]}, setChallengeFee {CHALLENGE_FEE}"
              + (", setFaucet" if faucet else ""))
        for line in kb.plan_lines(binder):
            print(line)
        print(f"  would write      {out_path.relative_to(ROOT)}, after every transaction")
        print("  would NOT touch  any existing deployment record or contract")
        return 0

    record: dict = {"chain_id": net["chain_id"], "network": args.network, "label": args.label, "commit": commit,
                    "status": "deploying", "deployer": deployer.address, "operator": deployer.address,
                    "core_writer": net["core_writer"], "precompiles": net["precompiles"],
                    "platform_assets": PLATFORM_ASSETS, "funded_assets": list(FUNDED_ASSETS),
                    "challenge_fee": CHALLENGE_FEE, "shares_code": code, "initcode_keccak": hashes,
                    "key_binder": kb.record_of(deployer.address, binder), "codehash": {}, "tx": {}}
    a: dict[str, str] = {}
    big_blocks(deployer, True)
    try:
        try:
            if token is None:
                a["DemoUsdc"], _ = deploy_contract(deployer, out_path, record, "DemoUsdc", "DemoUsdc",
                                                   ["address"], [deployer.address])
                token = a["DemoUsdc"]
            record["usdc"] = token
            a["KeyBinder"], _ = deploy_contract(deployer, out_path, record, "KeyBinder", "KeyBinder",
                                                *kb.constructor(deployer.address, binder))
            a["AccountOps"], _ = deploy_contract(deployer, out_path, record, "AccountOps", "AccountOps", [], [])
            libs = {"AccountOps": a["AccountOps"]}
            a["PoolImpl"], _ = deploy_contract(deployer, out_path, record, "PoolImpl", "Pool", [], [], libs)
            a["ChallengeAccountImpl"], _ = deploy_contract(deployer, out_path, record, "ChallengeAccountImpl",
                                                           "ChallengeAccount", [], [], libs)
            a["PoolFactory"], rcpt = deploy_contract(
                deployer, out_path, record, "PoolFactory", "PoolFactory",
                ["address", "address", "address", "address", "address"],
                [a["KeyBinder"], a["PoolImpl"], a["ChallengeAccountImpl"], deployer.address, token])
            a["PaperExamImpl"], _ = deploy_contract(deployer, out_path, record, "PaperExamImpl", "PaperExam", [], [])
            a["MultiPoolReader"], _ = deploy_contract(deployer, out_path, record, "MultiPoolReader",
                                                      "MultiPoolReader", [], [])
        finally:
            big_blocks(deployer, False)
        record["codehash"] = {key: code_keccak(addr) for key, addr in a.items()}
        record.update({"block": int(rcpt["blockNumber"], 16), "status": "configuring"})
        save(out_path, record)
    except BaseException as exc:
        mark_incomplete(out_path, record, exc)
        raise

    factory, key_binder = a["PoolFactory"], a["KeyBinder"]
    steps = kb.binder_steps(key_binder, factory, binder) + [
        ("setPlatformAssets", factory, "setPlatformAssets(uint32[],string[],bool)", ["uint32[]", "string[]", "bool"],
         [assets, list(PLATFORM_ASSETS), True]),
        ("setFundedAssets", factory, "setFundedAssets(uint32[],string[],bool)", ["uint32[]", "string[]", "bool"],
         [[PLATFORM_ASSETS[name] for name in FUNDED_ASSETS], list(FUNDED_ASSETS), True]),
        ("setExamImpl", factory, "setExamImpl(address,bool)", ["address", "bool"], [a["PaperExamImpl"], True]),
        ("setSharesCode", factory, "setSharesCode(bytes32)", ["bytes32"], [bytes.fromhex(code[2:])]),
        ("setChallengeFee", factory, "setChallengeFee(uint256,address)", ["uint256", "address"],
         [CHALLENGE_FEE, deployer.address]),
    ]
    if faucet and "DemoUsdc" in a:
        steps.append(("setFaucet", a["DemoUsdc"], "setFaucet(address,uint256)", ["address", "uint256"], [faucet, daily]))
    steps += kb.handover(key_binder, factory, binder)
    try:
        for name, to, sig, types, values in steps:
            record["tx"][name] = c.transact(deployer, to, sig, types, values)["transactionHash"]
            kb.after_step(record, name, binder, deployer.address)
            if name == "setFaucet":
                record["faucet"] = {"address": faucet, "daily": daily}
            save(out_path, record)
    except BaseException as exc:
        mark_incomplete(out_path, record, exc)
        raise

    record["status"] = "complete"
    save(out_path, record)
    c.record("deployed_v3", **{k: v for k, v in record.items() if k != "tx"})
    print(f"wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
