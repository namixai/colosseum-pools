"""Deploy a shared pool on testnet (chain 998), on a factory and key registry of its own or on an
existing deployment's.

    spike/.venv/bin/python ops/deploy_shared.py --label shared-run --keys-file <addresses.txt> \\
        --min-deposit 20 --lock 600 --fee-bps 1000
    spike/.venv/bin/python ops/deploy_shared.py --label shared-demo2 --on-factory-of demo2 \\
        --min-deposit 20 --lock 600 --fee-bps 1000 [--dry-run]

With --on-factory-of <label> only the SharedPool is deployed, and its seats are made by that
deployment's factory: they are pools of it like any other, served by its gateway, its pages and its
keeper on the operator's host, and a challenge on them takes a key from its registry. Don't run
ops/keeper.py with such a deployment yourself: it follows every challenge of that factory, the
deployment's own pools included. --on-demo-factory is --on-factory-of demo, the first deployment.

--dry-run makes every check a real run makes -- the label is free, the base deployment's source is
this commit's, the contracts build, the factory lists the assets, the operator is on HyperCore and
holds gas -- and sends nothing.

The run's seats don't trade, so its agent keys never go near the gateway host: a registry of its
own keeps them apart from the demo's, and a factory of its own keeps the run's seats out of the
demo's list of pools. The pool and challenge implementations are the demo's, cloned from the
addresses in deployments/testnet-demo.json; this refuses to go on if their source has changed
since the commit that deployment recorded.

Signs with the `shared-operator` key, which is both the operator and the platform here, so it
needs a HyperCore account (for the switch to big blocks) and HYPE for gas. Writes
deployments/testnet-<label>.json the way ops/deploy_testnet.py does, status field included.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from hlspike import common as c  # noqa: E402
from ops import deployments  # noqa: E402
from ops.deploy_testnet import (  # noqa: E402
    CHALLENGE_FEE,
    PLATFORM_ASSETS,
    big_blocks,
    check_assets,
    check_keys,
    deploy_contract,
    git,
    git_head,
    mark_incomplete,
    save,
)

# The sources the demo's implementations were built from; the clones this deployment makes run
# that code, so it has to be what this commit says it is.
CORE_SOURCES = ("src/Pool.sol", "src/ChallengeAccount.sol", "src/RuledAccount.sol", "src/Types.sol",
                "src/KeyRegistry.sol", "src/lib/CoreOps.sol", "lib")
USDC_1E8 = 100_000_000


# One SharedPool through big blocks. The three deployed so far used 5.1 to 5.2 million gas at 0.1 gwei,
# about 0.0005 HYPE each (their receipts, read 3 Oct 2026); four times that is a refusal to start, not
# an estimate. ops/deploy_testnet.py's floor is for four contracts and would refuse a wallet that
# holds fourteen times what this takes.
SHARED_GAS_FLOOR_WEI = 2 * 10**15


def gas_or_refuse(op, floor: int = SHARED_GAS_FLOOR_WEI) -> int:
    """The operator's HYPE, in wei; refuses below `floor`. Checked before anything is sent: a
    deployment that runs out half way leaves a record marked incomplete, and an empty wallet reads
    like a node problem until somebody looks."""
    gas_wei = int(c.rpc("eth_getBalance", [op.address, "latest"]), 16)
    if gas_wei < floor:
        raise SystemExit(f"shared-operator holds {gas_wei / 1e18:.4f} HYPE, which is below the "
                         f"{floor / 1e18:.3f} this deployment needs; fund it first")
    return gas_wei


def on_factory_of(args, out_path: pathlib.Path, commit: str, base: dict, assets: list[int]) -> int:
    """The SharedPool alone, owning seats the base deployment's factory makes. Nothing of the base
    deployment is changed."""
    label = base["label"]
    factory, registry = base["PoolFactory"], base["KeyRegistry"]
    for idx in assets:
        if not c.call_view(factory, "isPlatformAsset(uint32)", ["uint32"], [idx], ["bool"])[0]:
            raise SystemExit(f"the factory of {label} doesn't list asset {idx}")
    op = c.account("shared-operator")
    if not c.core_user_exists(op.address):
        raise SystemExit("shared-operator has no HyperCore account yet; fund it first")
    gas_wei = gas_or_refuse(op)
    min_deposit = int(round(args.min_deposit * USDC_1E8))
    fee = c.call_view(factory, "challengeFee()", [], [], ["uint256"])[0]
    if args.dry_run:
        print("deploy_shared --dry-run: nothing was sent.")
        print(f"  label            {args.label}  (deployments/testnet-{args.label}.json is free)")
        print(f"  commit           {commit}")
        print(f"  on the factory   of {label}: PoolFactory {factory}, KeyRegistry {registry}")
        print(f"  its assets       {assets} are all listed; its challenge fee is {fee}")
        print(f"  operator         {op.address}, {gas_wei / 1e18:.4f} HYPE, on HyperCore; it is the platform too")
        print(f"  would deploy     SharedPool(factory, operator, platform, min deposit {min_deposit}, "
              f"lock {args.lock} s, fee {args.fee_bps} bps)  (big blocks on, then off)")
        print(f"  would write      deployments/testnet-{args.label}.json")
        print("  would NOT touch  any existing deployment record or contract, and publishes no key")
        return 0
    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": commit, "status": "deploying",
                    "deployer": op.address, "operator": op.address, "platform": op.address,
                    "factory_from": label, "PoolFactory": factory, "KeyRegistry": registry,
                    "platform_assets": PLATFORM_ASSETS, "challenge_fee": fee,
                    "PoolImpl": base["PoolImpl"], "ChallengeAccountImpl": base["ChallengeAccountImpl"],
                    "implementations_from": base["commit"], "min_deposit": min_deposit, "lock": args.lock,
                    "fee_bps": args.fee_bps, "tx": {}}
    big_blocks(op, True)
    try:
        try:
            _, rcpt = deploy_contract(op, out_path, record, "SharedPool", "SharedPool",
                                      ["address", "address", "address", "uint64", "uint32", "uint16"],
                                      [factory, op.address, op.address, min_deposit, args.lock, args.fee_bps])
        finally:
            big_blocks(op, False)
    except BaseException as exc:
        mark_incomplete(out_path, record, exc)
        raise
    record.update({"block": int(rcpt["blockNumber"], 16), "status": "complete"})
    save(out_path, record)
    c.record("deployed_shared", **{k: v for k, v in record.items() if k != "tx"})
    print(f"wrote {out_path.relative_to(ROOT)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True)
    p.add_argument("--keys-file", help="agent key addresses for this deployment's own registry, one per line")
    p.add_argument("--on-factory-of", metavar="LABEL",
                   help="make the seats on that deployment's factory (deployments/testnet-<LABEL>.json); "
                        "deploys the SharedPool alone")
    p.add_argument("--on-demo-factory", action="store_true", help="the same as --on-factory-of demo")
    p.add_argument("--dry-run", action="store_true",
                   help="run every check and send nothing; says what a real run would do")
    p.add_argument("--min-deposit", type=float, default=20.0, help="USDC")
    p.add_argument("--lock", type=int, default=600, help="seconds after a deposit before a request")
    p.add_argument("--fee-bps", type=int, default=1000)
    args = p.parse_args()
    if args.on_demo_factory and args.on_factory_of not in (None, "demo"):
        raise SystemExit("--on-demo-factory is --on-factory-of demo; name one deployment")
    # An empty label is not "no label": it would pass for a deployment named and then fall back to the first
    # one, and the pool would be deployed on a factory nobody asked for.
    if args.on_factory_of is not None and not args.on_factory_of.strip():
        raise SystemExit("--on-factory-of needs the label of a deployment, as in deployments/testnet-<label>.json")
    base_label = "demo" if args.on_demo_factory else args.on_factory_of
    # Before anything reaches the chain. An empty --keys-file is still one.
    if (base_label is not None) == (args.keys_file is not None):
        raise SystemExit("either --keys-file for a registry of its own, or --on-factory-of <label> "
                         "(--on-demo-factory for the first deployment), whose registry has its keys")
    if args.dry_run and base_label is None:
        raise SystemExit("--dry-run is for --on-factory-of: a registry of its own has no dry run yet")

    c.assert_testnet()
    out_path = ROOT / "deployments" / f"testnet-{args.label}.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists; pick another label")
    if args.min_deposit < 1 or not 0 < args.lock < 2**32 or not 0 <= args.fee_bps <= 10_000:
        raise SystemExit("--min-deposit is at least 1 USDC, --lock a positive uint32, --fee-bps 0..10000")
    commit = git_head()
    # The implementations the seats clone: the base deployment's, or the first deployment's for a
    # run on a factory of its own.
    base = deployments.load("demo" if base_label is None else base_label)
    changed = git("diff", "--name-only", base["commit"], "HEAD", "--", *CORE_SOURCES).strip()
    if changed:
        raise SystemExit(f"the implementations of {base['label']} were built from other source; changed: {changed}")
    subprocess.run(["forge", "build"], cwd=ROOT, check=True, capture_output=True)
    assets = check_assets()
    if base_label is not None:
        return on_factory_of(args, out_path, commit, base, assets)
    demo = base
    keys = check_keys(pathlib.Path(args.keys_file).read_text().splitlines())
    if not keys:
        raise SystemExit("no agent keys: every challenge reserves one")

    op = c.account("shared-operator")
    if not c.core_user_exists(op.address):
        raise SystemExit("shared-operator has no HyperCore account yet; fund it first")
    min_deposit = int(round(args.min_deposit * USDC_1E8))

    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": commit, "status": "deploying",
                    "deployer": op.address, "operator": op.address, "platform": op.address,
                    "platform_assets": PLATFORM_ASSETS, "challenge_fee": CHALLENGE_FEE,
                    "PoolImpl": demo["PoolImpl"], "ChallengeAccountImpl": demo["ChallengeAccountImpl"],
                    "implementations_from": demo["commit"], "min_deposit": min_deposit, "lock": args.lock,
                    "fee_bps": args.fee_bps, "tx": {}}
    big_blocks(op, True)
    try:
        try:
            registry, _ = deploy_contract(op, out_path, record, "KeyRegistry", "KeyRegistry",
                                          ["address"], [op.address])
            factory, rcpt = deploy_contract(op, out_path, record, "PoolFactory", "PoolFactory",
                                            ["address", "address", "address", "address"],
                                            [registry, demo["PoolImpl"], demo["ChallengeAccountImpl"], op.address])
            shared, _ = deploy_contract(op, out_path, record, "SharedPool", "SharedPool",
                                        ["address", "address", "address", "uint64", "uint32", "uint16"],
                                        [factory, op.address, op.address, min_deposit, args.lock, args.fee_bps])
        finally:
            big_blocks(op, False)
    except BaseException as exc:  # a deployment or the switch back to small blocks failed
        mark_incomplete(out_path, record, exc)
        raise

    record.update({"block": int(rcpt["blockNumber"], 16), "status": "configuring"})
    save(out_path, record)
    steps = [
        ("setAccountSource", registry, "setAccountSource(address)", ["address"], [factory]),
        ("setPlatformAssets", factory, "setPlatformAssets(uint32[],bool)", ["uint32[]", "bool"], [assets, True]),
        ("setChallengeFee", factory, "setChallengeFee(uint256,address)", ["uint256", "address"],
         [CHALLENGE_FEE, op.address]),
        ("publish", registry, "publish(address[])", ["address[]"], [keys]),
    ]
    try:
        for name, to, sig, types, values in steps:
            record["tx"][name] = c.transact(op, to, sig, types, values)["transactionHash"]
            if name == "publish":
                record["published_keys"] = keys
            save(out_path, record)
    except BaseException as exc:
        mark_incomplete(out_path, record, exc)
        raise

    record["status"] = "complete"
    save(out_path, record)
    c.record("deployed_shared", **{k: v for k, v in record.items() if k != "tx"})
    print(f"wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
