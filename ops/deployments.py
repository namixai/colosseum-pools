"""Addresses of a deployment, read from its record: deployments/<network>-<label>.json, which the deploy wrote.

The network is the one COLOSSEUM_NETWORK names (spike/hlspike/network.py), the same switch the tools read through
spike/hlspike/common.py, so a keeper started on mainnet reads mainnet's records and never testnet's. A record must
say it is on that network: its chain_id is the network's chain id, and a `network` field, where the record has one,
names it. deployments/LIVE.json lists the records and what serves each (docs/DEPLOYMENTS.md).
"""

from __future__ import annotations

import json
import pathlib

from spike.hlspike import network as networks

ROOT = pathlib.Path(__file__).resolve().parents[1]


def record_path(label: str, net: networks.Network | None = None) -> pathlib.Path:
    """Where the record of deployment `label` on `net` (the selected network when none is given) is kept."""
    net = net or networks.select()
    return ROOT / "deployments" / f"{net.name}-{label}.json"


def records(net: networks.Network | None = None) -> list[pathlib.Path]:
    """Every record of `net` (the selected network when none is given), by file name."""
    net = net or networks.select()
    directory = ROOT / "deployments"
    return sorted(directory.glob(f"{net.name}-*.json")) if directory.is_dir() else []


def load(label: str, net: networks.Network | None = None) -> dict:
    net = net or networks.select()
    path = record_path(label, net)
    if not path.exists():
        raise SystemExit(f"no deployment named {label!r} on {net.name}: {path.relative_to(ROOT)} is missing")
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{path.relative_to(ROOT)} cannot be read: {exc}") from None
    if record.get("chain_id") != net.chain_id:
        raise SystemExit(f"{path.relative_to(ROOT)} is not a {net.name} deployment: chain_id "
                         f"{record.get('chain_id')}, not {net.chain_id}")
    if record.get("network", net.name) != net.name:
        raise SystemExit(f"{path.relative_to(ROOT)} says it is on {record['network']}, not {net.name}")
    if record.get("status") != "complete":
        raise SystemExit(f"deployment {label!r} is {record.get('status', 'of unknown state')}, not complete: "
                         f"{record.get('error', 'see the record')}")
    return record


def registry_operator(record: dict) -> dict:
    """Who operates the deployment's key registry, as its record says: the deployer, unless the deploy handed the
    role over (--operator, `registry_operator` in the record). A handover is `pending` until the address the owner
    gave accepts it on chain (acceptOperator), and the record says `accepted` once someone writes that it has."""
    handover = record.get("registry_operator")
    if not handover:
        return {"operator": record.get("deployer"), "pending": None}
    if handover.get("state") == "accepted":
        return {"operator": handover["to"], "pending": None}
    if handover.get("state") != "pending":
        raise SystemExit(f"registry_operator state {handover.get('state')!r}: pending or accepted")
    return {"operator": handover["from"], "pending": handover["to"]}


def resolve(label: str | None, factory: str | None = None, registry: str | None = None) -> tuple[str, str]:
    """Addresses given on the command line win; the named deployment fills in the rest."""
    if label:
        record = load(label)
        factory = factory or record["PoolFactory"]
        # A deployment with a key binder answers the same reads (bindingOf, isBound, keyOf) at its address.
        registry = registry or record.get("KeyRegistry") or record.get("KeyBinder")
    if not factory or not registry:
        raise SystemExit("pass --deployment <label>, or --factory and --registry")
    return factory, registry
