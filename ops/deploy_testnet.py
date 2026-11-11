"""Deploy the pool contracts to HyperEVM testnet (chain 998). Nothing else.

Testnet only, on purpose: the contracts in this repository are the ones before v3, and mainnet's deployment is
v3's, whose source is not on this branch (docs/DEPLOYMENTS.md, What stays on testnet). PLATFORM_ASSETS are
testnet's perp numbers; mainnet's are listed in that document.

    spike/.venv/bin/python ops/deploy_testnet.py --label rehearsal
    spike/.venv/bin/python ops/deploy_testnet.py --label demo --publisher 0x... --pcr-hash 0x... --attestation-hash 0x...

The implementations are larger than a small HyperEVM block allows (3M gas), so the deployer
switches itself to big blocks (about one a minute, 30M gas) for the deployment and back
afterwards. That switch is a HyperCore action, so the deployer must already exist there.

Writes deployments/testnet-<label>.json: addresses, transaction hashes, the git commit the
bytecode was built from, and the platform asset list. Refuses to overwrite a label. The record
is written after every contract and every later transaction, with a `status` field, and each
write replaces the whole file, so a failure half way still leaves the addresses on disk. A
failure after the first transaction marks the record `incomplete`, which nothing downstream
loads.

The factory on this commit takes a key binder (src/KeyBinder.sol): no agent key is published, and
each account that needs one asks for it and gets it on the publisher's signature (docs/KEY-BINDING.md).
The binder's flags are ops/binder_deploy.py's; with --safe the binder and the factory are handed
over to it last. The helpers below that check key addresses against the registries on record
(`check_keys`, `registries_in_records`) stay for the deployments before the binder.
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
sys.path.insert(0, str(ROOT))

from eth_utils import to_checksum_address  # noqa: E402

from hlspike import common as c  # noqa: E402
from ops import binder_deploy as kb  # noqa: E402

# The one network these deploys go to, and the first part of their records' names (docs/DEPLOYMENTS.md,
# What stays on testnet). c.assert_testnet() checks the selected network and its RPC against it.
NETWORK = "testnet"

# Perp indices from the testnet meta, the three with real volume on 16 Sep 2026:
# BTC 3, ETH 4, SOL 0. Checked again at deploy time against the live meta.
PLATFORM_ASSETS = {"BTC": 3, "ETH": 4, "SOL": 0}
# The platform fee on every challenge, in HyperEVM USDC units, paid to the operator (the
# deployer). The demo has to show that a challenge starts only after payment, and the fee keeps
# anyone from using up the agent keys (CTO, 17 Sep 2026). 0.7 test USDC, a tenth of the demo
# pool's price: the demo pool is 700 funded, 70 per challenge and a price of 7 (app/lib/demo.js),
# which is what the mock USDC we hold pays for -- the faucet gives 1,000 per wallet and ours has
# had its 1,000.
CHALLENGE_FEE = 700_000


# Everything the bytecode is built from.
BUILD_INPUTS = ("src", "lib", "foundry.toml", "foundry.lock", "remappings.txt")
# Four contracts through big blocks. Measured deployments used well under this; the floor is a
# refusal to start, not an estimate.
GAS_FLOOR_WEI = 2 * 10**16


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


def registries_in_records(records_dir: pathlib.Path | None = None) -> dict[str, str]:
    """Every key registry a deployment record names, and the record that named it first.

    The records are the list of registries, not the list of keys. Keys get published to a live
    registry long after the deployment that made it -- by hand, from the host, as the demo needs
    more -- and none of that reaches a file here. A check that read `published_keys` would have
    answered "all clear" for exactly the keys this is meant to catch, which is how it was built
    first: measured 29 Sep 2026, a run against the twenty-four published into the demo's registry
    that morning passed them, because no record names them.
    """
    directory = records_dir if records_dir is not None else ROOT / "deployments"
    found = journal_registries(directory)
    # A directory that is not there reads the same as one with nothing in it, and neither can be
    # told from a wrong path. So the run prints how many records it consulted: "0 records" is a
    # sentence somebody can notice, where a silent pass is not.
    for path in sorted(directory.glob(f"{NETWORK}-*.json")) if directory.is_dir() else []:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise SystemExit(f"{path.name} cannot be read ({exc}); refusing rather than skipping "
                             f"a record that may name a registry") from None
        registry = record.get("KeyRegistry")
        if registry:
            found.setdefault(to_checksum_address(registry), path.name)
    return found


# Every key registry this project has deployed, whether or not its deployment record is in this
# checkout: the multi stand's (testnet-multi-1.json) lives on a branch of its own, and its spare keys
# passed this check (audit window 2, A5-3). A deployment that makes a registry adds it here.
REGISTRY_JOURNAL = "registries.json"


def journal_registries(directory: pathlib.Path) -> dict[str, str]:
    """The journal's registries on this chain, each with where it came from."""
    path = directory / REGISTRY_JOURNAL
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{path.name} cannot be read ({exc}); refusing rather than skipping the "
                         f"journal of every registry") from None
    return {to_checksum_address(e["address"]): f"{REGISTRY_JOURNAL}: {e['deployment']}"
            for e in entries if int(e["chain_id"]) == c.CHAIN_ID}


def journal_registry(directory: pathlib.Path, registry: str, deployment: str) -> None:
    """Adds a registry just deployed to the journal, once."""
    path = directory / REGISTRY_JOURNAL
    entries = json.loads(path.read_text()) if path.exists() else []
    if any(to_checksum_address(e["address"]) == to_checksum_address(registry) for e in entries):
        return
    entries.append({"address": to_checksum_address(registry), "chain_id": c.CHAIN_ID, "deployment": deployment})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n")


def key_is_known_to(registry: str, key: str) -> bool:
    """Whether a registry has ever heard of this key. State 0 is Unknown; anything else it holds.

    A registry never forgets a key, so Free, Bound and Retired all count. Free is the one that
    matters here and the one nothing else catches: a spare key has no HyperCore account, which is
    what `check_keys` tests for, so it passes every other rule while being an address two
    registries would both hand out -- and then one key two accounts can each take as their agent.
    """
    binding = c.call_view(registry, "bindingOf(address)", ["address"], [key], ["(uint8,address,address)"])[0]
    return int(binding[0]) != 0


def check_keys(lines: list[str], records_dir: pathlib.Path | None = None) -> list[str]:
    """Key addresses from a keys file, checked the way KeyRegistry.publish would check them,
    before anything is deployed -- and against every registry a deployment record names."""
    registries = registries_in_records(records_dir)
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
        for registry, named_by in registries.items():
            try:
                known = key_is_known_to(registry, key)
            except Exception as exc:  # a node that will not answer is not an all-clear
                raise SystemExit(f"could not ask registry {registry} (from {named_by}) about "
                                 f"{key}: {exc}") from None
            if known:
                raise SystemExit(f"{key} is already in registry {registry}, named by "
                                 f"deployments/{named_by}. One address in two registries is one "
                                 f"key two accounts can each take as their agent. Make new keys "
                                 f"on the gateway host")
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
                    types: list, values: list, libraries: dict[str, str] | None = None) -> tuple[str, dict]:
    """Deploy one contract and put its address on disk before the next transaction is sent."""
    addr, rcpt = c.deploy(deployer, contract, types, values, libraries) if libraries else c.deploy(deployer, contract, types, values)
    record[key] = addr
    record["tx"][key] = rcpt["transactionHash"]
    save(path, record)
    if contract == "KeyRegistry":
        journal_registry(path.parent, addr, path.name)
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
    p.add_argument("--dry-run", action="store_true",
                   help="run every check and send nothing; says what a real run would do")
    kb.add_args(p)
    args = p.parse_args()
    binder = kb.settings(args, NETWORK)  # before anything is read: a bad address costs nothing

    # Testnet only: these are the contracts before v3, and mainnet's deployment is v3's, whose source is not on
    # this branch (docs/DEPLOYMENTS.md, What stays on testnet).
    c.assert_testnet()
    out_path = ROOT / "deployments" / f"{NETWORK}-{args.label}.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists; pick another label")
    commit = git_head()
    subprocess.run(["forge", "build"], cwd=ROOT, check=True, capture_output=True)
    assets = check_assets()

    deployer = c.account("deployer")
    if not c.core_user_exists(deployer.address):
        raise SystemExit("the deployer has no HyperCore account yet; fund it first")

    # Gas is checked here rather than discovered half way. A deployment that runs out after the
    # registry leaves a record marked incomplete and a live contract nothing points at, and an
    # empty wallet reads like a node problem until somebody looks -- it cost an hour once.
    gas_wei = int(c.rpc("eth_getBalance", [deployer.address, "latest"]), 16)
    if gas_wei < GAS_FLOOR_WEI:
        raise SystemExit(f"the deployer holds {gas_wei / 1e18:.4f} HYPE, which is below the "
                         f"{GAS_FLOOR_WEI / 1e18:.2f} these four contracts need; fund it first")

    if args.dry_run:
        print("deploy --dry-run: nothing was sent.")
        print(f"  label            {args.label}  ({out_path.relative_to(ROOT)} is free)")
        print(f"  commit           {commit}")
        print(f"  deployer         {deployer.address}, {gas_wei / 1e18:.4f} HYPE, on HyperCore")
        print(f"  would deploy     KeyBinder, Pool, ChallengeAccount, PoolFactory (big blocks on, then off)")
        print(f"  would configure  setAccountSource, setPlatformAssets {assets}, "
              f"setChallengeFee {CHALLENGE_FEE}")
        for line in kb.plan_lines(binder):
            print(line)
        print("  would NOT touch  any existing deployment record or contract")
        return 0

    record: dict = {"chain_id": c.CHAIN_ID, "network": NETWORK, "label": args.label, "commit": commit,
                    "status": "deploying", "deployer": deployer.address, "platform_assets": PLATFORM_ASSETS,
                    "challenge_fee": CHALLENGE_FEE, "key_binder": kb.record_of(deployer.address, binder), "tx": {}}
    big_blocks(deployer, True)
    try:
        try:
            key_binder, _ = deploy_contract(deployer, out_path, record, "KeyBinder", "KeyBinder",
                                            *kb.constructor(deployer.address, binder))
            libs = {"AccountOps": deploy_contract(deployer, out_path, record, "AccountOps", "AccountOps", [], [])[0]}
            pool_impl, _ = deploy_contract(deployer, out_path, record, "PoolImpl", "Pool", [], [], libs)
            challenge_impl, _ = deploy_contract(deployer, out_path, record, "ChallengeAccountImpl",
                                                "ChallengeAccount", [], [], libs)
            factory, rcpt = deploy_contract(deployer, out_path, record, "PoolFactory", "PoolFactory",
                                            ["address", "address", "address", "address", "address"],
                                            [key_binder, pool_impl, challenge_impl, deployer.address, c.TESTNET_USDC_ERC20])
        finally:
            big_blocks(deployer, False)
    except BaseException as exc:  # a deployment or the switch back to small blocks failed
        mark_incomplete(out_path, record, exc)
        raise

    record.update({"block": int(rcpt["blockNumber"], 16), "status": "configuring"})
    save(out_path, record)

    steps = kb.binder_steps(key_binder, factory, binder) + [
        ("setPlatformAssets", factory, "setPlatformAssets(uint32[],string[],bool)", ["uint32[]", "string[]", "bool"],
         [assets, list(PLATFORM_ASSETS), True]),
        ("setChallengeFee", factory, "setChallengeFee(uint256,address)", ["uint256", "address"],
         [CHALLENGE_FEE, deployer.address]),
    ]
    steps += kb.handover(key_binder, factory, binder)
    try:
        for name, to, sig, types, values in steps:
            record["tx"][name] = c.transact(deployer, to, sig, types, values)["transactionHash"]
            kb.after_step(record, name, binder, deployer.address)
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
