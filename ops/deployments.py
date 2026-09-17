"""Addresses of a testnet deployment, read from the record ops/deploy_testnet.py wrote."""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTNET_CHAIN_ID = 998


def load(label: str) -> dict:
    path = ROOT / "deployments" / f"testnet-{label}.json"
    if not path.exists():
        raise SystemExit(f"no deployment named {label!r}: {path.relative_to(ROOT)} is missing")
    record = json.loads(path.read_text())
    if record.get("chain_id") != TESTNET_CHAIN_ID:
        raise SystemExit(f"{path.relative_to(ROOT)} is not a testnet deployment")
    if record.get("status") != "complete":
        raise SystemExit(f"deployment {label!r} is {record.get('status', 'of unknown state')}, not complete: "
                         f"{record.get('error', 'see the record')}")
    return record


def resolve(label: str | None, factory: str | None = None, registry: str | None = None) -> tuple[str, str]:
    """Addresses given on the command line win; the named deployment fills in the rest."""
    if label:
        record = load(label)
        factory = factory or record["PoolFactory"]
        registry = registry or record["KeyRegistry"]
    if not factory or not registry:
        raise SystemExit("pass --deployment <label>, or --factory and --registry")
    return factory, registry
