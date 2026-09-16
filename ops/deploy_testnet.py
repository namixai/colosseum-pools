"""Deploy the pool contracts to HyperEVM testnet (chain 998). Nothing else.

    spike/.venv/bin/python ops/deploy_testnet.py --label rehearsal
    spike/.venv/bin/python ops/deploy_testnet.py --label demo --keys-file <file with enclave key addresses>

The implementations are larger than a small HyperEVM block allows (3M gas), so the deployer
switches itself to big blocks (about one a minute, 30M gas) for the deployment and back
afterwards. That switch is a HyperCore action, so the deployer must already exist there.

Writes deployments/testnet-<label>.json: addresses, transaction hashes, the git commit the
bytecode was built from, and the platform asset list. Refuses to overwrite a label.

Key addresses published with --keys-file must be enclave-minted keys from the Signer team.
Local test keys belong in a rehearsal deployment only; a registry never forgets a key.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))

from eth_utils import to_checksum_address  # noqa: E402

from hlspike import common as c  # noqa: E402

# Perp indices from the testnet meta, the three with real volume on 16 Sep 2026:
# BTC 3, ETH 4, SOL 0. Checked again at deploy time against the live meta.
PLATFORM_ASSETS = {"BTC": 3, "ETH": 4, "SOL": 0}


def git_head() -> str:
    dirty = subprocess.run(["git", "status", "--porcelain", "src"], cwd=ROOT, capture_output=True, text=True).stdout
    if dirty.strip():
        raise SystemExit("src/ has uncommitted changes; deploy only committed bytecode")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def check_assets() -> list[int]:
    meta = c.info_post({"type": "meta"})["universe"]
    out = []
    for name, idx in PLATFORM_ASSETS.items():
        if meta[idx]["name"] != name or meta[idx].get("isDelisted"):
            raise SystemExit(f"testnet meta changed: index {idx} is {meta[idx]['name']}, expected {name}")
        out.append(idx)
    return out


def big_blocks(acct, enable: bool) -> None:
    resp = c.exchange(acct).use_big_blocks(enable)
    c.record("big_blocks", enable=enable, response=resp)
    if not (isinstance(resp, dict) and resp.get("status") == "ok"):
        raise SystemExit(f"could not switch big blocks to {enable}: {resp}")
    time.sleep(3)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True)
    p.add_argument("--keys-file", help="one enclave key address per line")
    args = p.parse_args()

    c.assert_testnet()
    out_path = ROOT / "deployments" / f"testnet-{args.label}.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists; pick another label")
    commit = git_head()
    subprocess.run(["forge", "build"], cwd=ROOT, check=True, capture_output=True)
    assets = check_assets()

    keys: list[str] = []
    if args.keys_file:
        for line in pathlib.Path(args.keys_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keys.append(to_checksum_address(line))

    deployer = c.account("deployer")
    if not c.core_user_exists(deployer.address):
        raise SystemExit("the deployer has no HyperCore account yet; fund it first")

    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": commit,
                    "deployer": deployer.address, "platform_assets": PLATFORM_ASSETS, "tx": {}}
    big_blocks(deployer, True)
    try:
        registry, r1 = c.deploy(deployer, "KeyRegistry", ["address"], [deployer.address])
        pool_impl, r2 = c.deploy(deployer, "Pool", [], [])
        challenge_impl, r3 = c.deploy(deployer, "ChallengeAccount", [], [])
        factory, r4 = c.deploy(deployer, "PoolFactory", ["address", "address", "address", "address"],
                               [registry, pool_impl, challenge_impl, deployer.address])
    finally:
        big_blocks(deployer, False)

    record.update({"KeyRegistry": registry, "PoolImpl": pool_impl, "ChallengeAccountImpl": challenge_impl,
                   "PoolFactory": factory})
    record["tx"].update({"KeyRegistry": r1["transactionHash"], "PoolImpl": r2["transactionHash"],
                         "ChallengeAccountImpl": r3["transactionHash"], "PoolFactory": r4["transactionHash"]})

    rcpt = c.transact(deployer, registry, "setAccountSource(address)", ["address"], [factory])
    record["tx"]["setAccountSource"] = rcpt["transactionHash"]
    rcpt = c.transact(deployer, factory, "setPlatformAssets(uint32[],bool)", ["uint32[]", "bool"], [assets, True])
    record["tx"]["setPlatformAssets"] = rcpt["transactionHash"]
    if keys:
        rcpt = c.transact(deployer, registry, "publish(address[])", ["address[]"], [keys])
        record["tx"]["publish"] = rcpt["transactionHash"]
        record["published_keys"] = keys

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    c.record("deployed_product", **{k: v for k, v in record.items() if k != "tx"})
    print(f"wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
