"""Deploy a shared pool on testnet (chain 998), on a factory and key registry of its own or on the demo's.

    spike/.venv/bin/python ops/deploy_shared.py --label shared-run --keys-file <addresses.txt> \\
        --min-deposit 20 --lock 600 --fee-bps 1000
    spike/.venv/bin/python ops/deploy_shared.py --label shared-demo --on-demo-factory \\
        --min-deposit 20 --lock 600 --fee-bps 1000

With --on-demo-factory only the SharedPool is deployed, and its seats are made by the demo's
factory: they are pools of the demo like any other, served by its gateway, its pages and its keeper
on the operator's host, and a challenge on them takes a key from the demo's registry. Don't run
ops/keeper.py with such a deployment yourself: it follows every challenge of the demo's factory, the
demo's own pools included.

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


def on_demo_factory(args, out_path: pathlib.Path, commit: str, demo: dict, assets: list[int]) -> int:
    """The SharedPool alone, owning seats the demo's factory makes. Nothing of the demo's is changed."""
    factory, registry = demo["PoolFactory"], demo["KeyRegistry"]
    for idx in assets:
        if not c.call_view(factory, "isPlatformAsset(uint32)", ["uint32"], [idx], ["bool"])[0]:
            raise SystemExit(f"the demo's factory doesn't list asset {idx}")
    op = c.account("shared-operator")
    if not c.core_user_exists(op.address):
        raise SystemExit("shared-operator has no HyperCore account yet; fund it first")
    min_deposit = int(round(args.min_deposit * USDC_1E8))
    fee = c.call_view(factory, "challengeFee()", [], [], ["uint256"])[0]
    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": commit, "status": "deploying",
                    "deployer": op.address, "operator": op.address, "platform": op.address,
                    "factory_from": "demo", "PoolFactory": factory, "KeyRegistry": registry,
                    "platform_assets": PLATFORM_ASSETS, "challenge_fee": fee,
                    "PoolImpl": demo["PoolImpl"], "ChallengeAccountImpl": demo["ChallengeAccountImpl"],
                    "implementations_from": demo["commit"], "min_deposit": min_deposit, "lock": args.lock,
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
    p.add_argument("--on-demo-factory", action="store_true",
                   help="make the seats on the demo's factory; deploys the SharedPool alone")
    p.add_argument("--min-deposit", type=float, default=20.0, help="USDC")
    p.add_argument("--lock", type=int, default=600, help="seconds after a deposit before a request")
    p.add_argument("--fee-bps", type=int, default=1000)
    args = p.parse_args()

    c.assert_testnet()
    out_path = ROOT / "deployments" / f"testnet-{args.label}.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists; pick another label")
    if args.min_deposit < 1 or not 0 < args.lock < 2**32 or not 0 <= args.fee_bps <= 10_000:
        raise SystemExit("--min-deposit is at least 1 USDC, --lock a positive uint32, --fee-bps 0..10000")
    commit = git_head()
    demo = deployments.load("demo")
    changed = git("diff", "--name-only", demo["commit"], "HEAD", "--", *CORE_SOURCES).strip()
    if changed:
        raise SystemExit(f"the demo's implementations were built from other source; changed: {changed}")
    if args.on_demo_factory == bool(args.keys_file):
        raise SystemExit("either --keys-file for a registry of its own, or --on-demo-factory, whose registry has its keys")
    subprocess.run(["forge", "build"], cwd=ROOT, check=True, capture_output=True)
    assets = check_assets()
    if args.on_demo_factory:
        return on_demo_factory(args, out_path, commit, demo, assets)
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
