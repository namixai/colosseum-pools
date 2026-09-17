"""Deploy the pool contracts to HyperEVM testnet (chain 998). Nothing else.

    spike/.venv/bin/python ops/deploy_testnet.py --label rehearsal
    spike/.venv/bin/python ops/deploy_testnet.py --label demo --keys-file <file with enclave key addresses>

The implementations are larger than a small HyperEVM block allows (3M gas), so the deployer
switches itself to big blocks (about one a minute, 30M gas) for the deployment and back
afterwards. That switch is a HyperCore action, so the deployer must already exist there.

Writes deployments/testnet-<label>.json: addresses, transaction hashes, the git commit the
bytecode was built from, and the platform asset list. Refuses to overwrite a label. The record
is written after every contract and every later transaction, with a `status` field, and each
write replaces the whole file, so a failure half way still leaves the addresses on disk. A
failure after the first transaction marks the record `incomplete`, which nothing downstream
loads.

Key addresses published with --keys-file must be enclave-minted keys from the Signer team.
Local test keys belong in a rehearsal deployment only; a registry never forgets a key.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
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
# The platform fee on every challenge, in HyperEVM USDC units, paid to the operator (the
# deployer). 10 test USDC, the CTO's decision of 17 Sep 2026: the demo has to show that a
# challenge starts only after payment, and the fee keeps anyone from using up enclave keys.
CHALLENGE_FEE = 10_000_000


# Everything the bytecode is built from.
BUILD_INPUTS = ("src", "lib", "foundry.toml", "foundry.lock", "remappings.txt")


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def git_head() -> str:
    """The commit the bytecode is built from. Refuses to deploy if git can't say, or if any
    build input differs from that commit."""
    if git("status", "--porcelain", "--", *BUILD_INPUTS).strip():
        raise SystemExit(f"uncommitted changes in {', '.join(BUILD_INPUTS)}; deploy only committed bytecode")
    commit = git("rev-parse", "--verify", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"git rev-parse gave {commit!r}, not a commit")
    return commit


def check_assets() -> list[int]:
    meta = c.info_post({"type": "meta"})["universe"]
    out = []
    for name, idx in PLATFORM_ASSETS.items():
        if meta[idx]["name"] != name or meta[idx].get("isDelisted"):
            raise SystemExit(f"testnet meta changed: index {idx} is {meta[idx]['name']}, expected {name}")
        out.append(idx)
    return out


def check_keys(lines: list[str]) -> list[str]:
    """Key addresses from a keys file, checked the way KeyRegistry.publish would check them,
    before anything is deployed."""
    keys: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            key = to_checksum_address(line)
        except ValueError:
            raise SystemExit(f"not an address: {line!r}") from None
        if int(key, 16) == 0:
            raise SystemExit("the zero address can't be a key")
        if key in keys:
            raise SystemExit(f"{key} is listed twice")
        if c.core_user_exists(key):
            raise SystemExit(f"{key} already exists on HyperCore; the registry would refuse it")
        keys.append(key)
    return keys


def save(path: pathlib.Path, record: dict) -> None:
    """Replace the record whole, and only once the new version is on disk: a write that fails
    leaves the previous version, and a power cut after the rename doesn't bring it back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name == "posix":  # the rename itself is durable once the directory is synced
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def mark_incomplete(path: pathlib.Path, record: dict, exc: BaseException) -> None:
    record["status"] = "incomplete"
    record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    save(path, record)


def deploy_contract(deployer, path: pathlib.Path, record: dict, key: str, contract: str,
                    types: list, values: list) -> tuple[str, dict]:
    """Deploy one contract and put its address on disk before the next transaction is sent."""
    addr, rcpt = c.deploy(deployer, contract, types, values)
    record[key] = addr
    record["tx"][key] = rcpt["transactionHash"]
    save(path, record)
    return addr, rcpt


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

    keys = check_keys(pathlib.Path(args.keys_file).read_text().splitlines()) if args.keys_file else []

    deployer = c.account("deployer")
    if not c.core_user_exists(deployer.address):
        raise SystemExit("the deployer has no HyperCore account yet; fund it first")

    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": commit, "status": "deploying",
                    "deployer": deployer.address, "platform_assets": PLATFORM_ASSETS,
                    "challenge_fee": CHALLENGE_FEE, "tx": {}}
    big_blocks(deployer, True)
    try:
        try:
            registry, _ = deploy_contract(deployer, out_path, record, "KeyRegistry", "KeyRegistry",
                                          ["address"], [deployer.address])
            pool_impl, _ = deploy_contract(deployer, out_path, record, "PoolImpl", "Pool", [], [])
            challenge_impl, _ = deploy_contract(deployer, out_path, record, "ChallengeAccountImpl",
                                                "ChallengeAccount", [], [])
            factory, rcpt = deploy_contract(deployer, out_path, record, "PoolFactory", "PoolFactory",
                                            ["address", "address", "address", "address"],
                                            [registry, pool_impl, challenge_impl, deployer.address])
        finally:
            big_blocks(deployer, False)
    except BaseException as exc:  # a deployment or the switch back to small blocks failed
        mark_incomplete(out_path, record, exc)
        raise

    record.update({"block": int(rcpt["blockNumber"], 16), "status": "configuring"})
    save(out_path, record)

    steps = [
        ("setAccountSource", registry, "setAccountSource(address)", ["address"], [factory]),
        ("setPlatformAssets", factory, "setPlatformAssets(uint32[],bool)", ["uint32[]", "bool"], [assets, True]),
        ("setChallengeFee", factory, "setChallengeFee(uint256,address)", ["uint256", "address"],
         [CHALLENGE_FEE, deployer.address]),
    ]
    if keys:
        steps.append(("publish", registry, "publish(address[])", ["address[]"], [keys]))
    try:
        for name, to, sig, types, values in steps:
            record["tx"][name] = c.transact(deployer, to, sig, types, values)["transactionHash"]
            if name == "publish":
                record["published_keys"] = keys
            save(out_path, record)
    except BaseException as exc:
        mark_incomplete(out_path, record, exc)
        raise

    record["status"] = "complete"
    save(out_path, record)
    c.record("deployed_product", **{k: v for k, v in record.items() if k != "tx"})
    print(f"wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
