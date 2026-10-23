"""Deploy factory v3 in one command: its key registry, the account implementations, the factory, the
paper exam, the reader, and on testnet the demo's own USDC. Then the factory's settings and the
agent keys. The CTO's 1/8/1, 19-21 October 2026.

    spike/.venv/bin/python ops/deploy_v3.py --label demo3 --keys-file <addresses.txt> --faucet 0x... --dry-run
    spike/.venv/bin/python ops/deploy_v3.py --label demo3 --keys-file <addresses.txt> --faucet 0x...
    spike/.venv/bin/python ops/deploy_v3.py --label v3 --network mainnet --dry-run

What it deploys, in order (big blocks on, then off):
- DemoUsdc (testnet only): the token prices and payouts are in. Its operator, the deployer, mints
  any amount; --faucet mints up to --faucet-daily a UTC day. The chain's testnet USDC is not ours
  to mint.
- KeyRegistry, AccountOps (the library the accounts link to), Pool, ChallengeAccount.
- PoolFactory(registry, Pool, ChallengeAccount, operator, token).
- PaperExam: the exam implementation every MultiPool sells.
- MultiPoolReader.

What it sets, in order:
- setAccountSource: the registry takes accounts from this factory alone.
- setPlatformAssets and setFundedAssets: BTC, ETH and SOL for exams; BTC and ETH for a funded
  account (deep audit N6).
- setExamImpl: the paper exam is the one a shares contract binds (cloud audit, section 6).
- setSharesCode: the hash of out/PoolShares.json's creation code, the one code the factory creates
  shares contracts from.
- setChallengeFee: CHALLENGE_FEE to the operator.
- setFaucet on the token: --faucet and --faucet-daily.
- publish: the keys of --keys-file. Each is refused if any registry this project has deployed
  knows it, by the deployment records and deployments/registries.json (audit window 2, A5-3).

Writes deployments/<network>-v3-<label>.json after every transaction, a `status` field included,
as ops/deploy_testnet.py does. The record holds every address and transaction, and the keccak of
each contract's creation code (out/) and of its code on chain. It also names the network's
parameters: chain id, token, CoreWriter and the precompiles.

--network mainnet (chain 999) prints the same plan with mainnet's parameters: the chain's USDC, and
no DemoUsdc. It sends nothing. This repository's plumbing (spike/hlspike/common.py) refuses
mainnet on import and on every send, and opening that is a decision of its own. So a mainnet run
without --dry-run is refused here, before anything is read.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from eth_utils import keccak, to_checksum_address  # noqa: E402

from hlspike import common as c  # noqa: E402
from ops.deploy_multi import FUNDED_ASSETS, shares_code  # noqa: E402
from ops.deploy_testnet import (  # noqa: E402
    CHALLENGE_FEE,
    PLATFORM_ASSETS,
    big_blocks,
    check_assets,
    check_keys,
    deploy_contract,
    git_head,
    mark_incomplete,
    registries_in_records,
    save,
)

# What a network gives the contracts. Precompiles and CoreWriter are the same on both HyperEVM
# chains; they are named here so that the record says what the build was deployed against.
NETWORKS = {
    "testnet": {"chain_id": 998, "usdc": None, "core_writer": c.CORE_WRITER,
                "precompiles": "0x0000000000000000000000000000000000000800"},
    "mainnet": {"chain_id": 999, "usdc": "0xb88339CB7199b77E23DB6E890353E22632Ba630f", "core_writer": c.CORE_WRITER,
                "precompiles": "0x0000000000000000000000000000000000000800"},
}
# What the demo's faucet may mint a UTC day unless told otherwise: a thousand dUSDC.
FAUCET_DAILY = 1_000 * 10**6
# The contracts, in the order they are deployed: (record key, contract).
CONTRACTS = (("DemoUsdc", "DemoUsdc"), ("KeyRegistry", "KeyRegistry"), ("AccountOps", "AccountOps"), ("PoolImpl", "Pool"),
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


def plan(network: str, token: str | None) -> list[str]:
    """What a run deploys, by record key."""
    return [key for key, _ in CONTRACTS if key != "DemoUsdc" or token is None]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True)
    p.add_argument("--network", choices=sorted(NETWORKS), default="testnet")
    p.add_argument("--keys-file", help="one agent key address per line")
    p.add_argument("--faucet", help="the address that mints the demo's USDC up to --faucet-daily a day (testnet)")
    p.add_argument("--faucet-daily", type=float, default=FAUCET_DAILY / 10**6, help="dUSDC a UTC day")
    p.add_argument("--dry-run", action="store_true", help="run every check and send nothing")
    args = p.parse_args()

    net = NETWORKS[args.network]
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
        print("deploy_v3 --network mainnet --dry-run: nothing was read from mainnet and nothing was sent.")
        print(f"  chain id         {net['chain_id']}")
        print(f"  token            {token}  (the chain's USDC; no DemoUsdc)")
        print(f"  CoreWriter       {net['core_writer']}, precompiles from {net['precompiles']}")
        print(f"  would deploy     {', '.join(deploys)}  (big blocks on, then off)")
        print(f"  creation code    " + "; ".join(f"{k} {v[:18]}" for k, v in hashes.items()))
        print(f"  would configure  setAccountSource, setPlatformAssets, setFundedAssets {list(FUNDED_ASSETS)}, "
              f"setExamImpl, setSharesCode {code[:18]}, setChallengeFee, publish")
        print("  🔴 mainnet's asset indices and the fee are to be read from mainnet's meta before a real run")
        return 0

    c.assert_testnet()
    assets = check_assets()
    keys = check_keys(pathlib.Path(args.keys_file).read_text().splitlines()) if args.keys_file else []
    deployer = c.account("deployer")
    if not c.core_user_exists(deployer.address):
        raise SystemExit("the deployer has no HyperCore account yet; fund it first")

    if args.dry_run:
        registries = registries_in_records()
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
        print(f"  would publish    {len(keys)} agent keys, checked against {len(registries)} registr(y/ies)"
              if keys else "  would publish    no keys: the registry cannot sell a challenge without them")
        print("  would NOT touch  any existing deployment record or contract")
        return 0

    record: dict = {"chain_id": net["chain_id"], "network": args.network, "label": args.label, "commit": commit,
                    "status": "deploying", "deployer": deployer.address, "operator": deployer.address,
                    "core_writer": net["core_writer"], "precompiles": net["precompiles"],
                    "platform_assets": PLATFORM_ASSETS, "funded_assets": list(FUNDED_ASSETS),
                    "challenge_fee": CHALLENGE_FEE, "shares_code": code, "initcode_keccak": hashes,
                    "codehash": {}, "tx": {}}
    a: dict[str, str] = {}
    big_blocks(deployer, True)
    try:
        try:
            if token is None:
                a["DemoUsdc"], _ = deploy_contract(deployer, out_path, record, "DemoUsdc", "DemoUsdc",
                                                   ["address"], [deployer.address])
                token = a["DemoUsdc"]
            record["usdc"] = token
            a["KeyRegistry"], _ = deploy_contract(deployer, out_path, record, "KeyRegistry", "KeyRegistry",
                                                  ["address"], [deployer.address])
            a["AccountOps"], _ = deploy_contract(deployer, out_path, record, "AccountOps", "AccountOps", [], [])
            libs = {"AccountOps": a["AccountOps"]}
            a["PoolImpl"], _ = deploy_contract(deployer, out_path, record, "PoolImpl", "Pool", [], [], libs)
            a["ChallengeAccountImpl"], _ = deploy_contract(deployer, out_path, record, "ChallengeAccountImpl",
                                                           "ChallengeAccount", [], [], libs)
            a["PoolFactory"], rcpt = deploy_contract(
                deployer, out_path, record, "PoolFactory", "PoolFactory",
                ["address", "address", "address", "address", "address"],
                [a["KeyRegistry"], a["PoolImpl"], a["ChallengeAccountImpl"], deployer.address, token])
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

    factory, registry = a["PoolFactory"], a["KeyRegistry"]
    steps = [
        ("setAccountSource", registry, "setAccountSource(address)", ["address"], [factory]),
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
    if keys:
        steps.append(("publish", registry, "publish(address[])", ["address[]"], [keys]))
    try:
        for name, to, sig, types, values in steps:
            record["tx"][name] = c.transact(deployer, to, sig, types, values)["transactionHash"]
            if name == "publish":
                record["published_keys"] = keys
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
