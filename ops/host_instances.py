"""The pools host's further instances: a gateway and a keeper for one more deployment beside the
first, on the same host (the CTO, 23 Oct 2026: demo3 beside demo2, whose live challenge stays served
by its own gateway and keeper). ops/host/install.sh asks this module what each instance needs.

The first deployment is named, as before, by /etc/colosseum/deployment, and served by
colosseum-gateway.service and colosseum-keeper.service. Each further one is a file
/etc/colosseum/deployment-<name> holding its deployment's label, made by
`ops/host/bootstrap.sh --instance <name> <label> <port>`, and is served by
colosseum-gateway@<name> and colosseum-keeper@<name>. Its gateway listens where
/etc/colosseum/gateway-<name>.env says (GATEWAY_BIND=127.0.0.1:<port>), and nginx sends
/<name>/v1/order, /<name>/v1/health, /<name>/v1/faucet and /<name>/v1/contacts of pools-api.usenami.io there.

    python -m ops.host_instances list /etc/colosseum             one line per instance: name label port
    python -m ops.host_instances nginx /etc/colosseum            the nginx locations for them
    python -m ops.host_instances nginx-upstreams /etc/colosseum  and their upstreams
    python -m ops.host_instances deployment-env <label>          COLOSSEUM_DEPLOYMENT, GATEWAY_FACTORY, the limits,
                                                                 the keeper's nodes, ...
    python -m ops.host_instances first-label /etc/colosseum      the first deployment's label, as served
    python -m ops.host_instances publisher <label>               its test publisher's address, or an empty line
    python -m ops.host_instances multi <label>                   what its multi keeper keeps (a MultiPool or a SimPoolFactory), or an empty line
    python -m ops.host_instances multi-pool <label>              its MultiPool, which has a watchdog, or an empty line

Testnet only, as the host is.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from dataclasses import dataclass
from decimal import Decimal

from ops import deployments

# A name is a path segment of the API and part of a unit's name: short, lower-case, and never "v1",
# which the first gateway's own paths begin with.
NAME = re.compile(r"[a-z][a-z0-9]{0,15}")
LABEL = re.compile(r"[a-z0-9-]+")
FIRST_PORT = 8787  # the first gateway's (ops/host/bootstrap.sh)
# nginx's own answer while a gateway is not listening: "restarting, nothing was sent"
# (ops/host/nginx-pools-api.conf.in). Every gateway's upstream falls back to it.
RESTARTING = "127.0.0.1:8786"
PORTS = range(8788, 8800)


@dataclass(frozen=True)
class Instance:
    name: str
    label: str
    port: int


def bind_port(env: pathlib.Path) -> int:
    """The port of GATEWAY_BIND in an instance's gateway settings: on 127.0.0.1, one of PORTS."""
    try:
        lines = env.read_text().splitlines()
    except OSError as exc:
        raise SystemExit(f"{env}: {exc.strerror}; an instance's gateway needs its settings") from None
    for line in lines:
        if line.startswith("GATEWAY_BIND="):
            host, _, port = line.split("=", 1)[1].strip().rpartition(":")
            if host != "127.0.0.1" or not port.isdigit() or int(port) not in PORTS:
                raise SystemExit(f"{env}: GATEWAY_BIND must be 127.0.0.1:<{PORTS.start}..{PORTS.stop - 1}>, not {line}")
            return int(port)
    raise SystemExit(f"{env}: no GATEWAY_BIND")


def first_label(etc: pathlib.Path) -> str:
    """The first deployment's label as install.sh serves it: /etc/colosseum/deployment with every
    space taken out, "de mo2" as "demo2"; "" when there is no such file. install.sh reads it here, so
    that the label it serves and the label the instances are checked against are one."""
    f = etc / "deployment"
    return "".join(f.read_text().split()) if f.is_file() else ""


def instances(etc: pathlib.Path) -> list[Instance]:
    """Every further instance named in `etc`, by name. A file that names one badly stops it all: a
    half-read list would leave an instance unrouted, or routed to another's gateway. No deployment is
    served twice, the first's included: two keepers on one label would share its state file,
    /var/lib/colosseum-keeper/keeper-<label>.json."""
    out = []
    for f in sorted(etc.glob("deployment-*")):
        if f.suffix in (".env", ".next", ".previous") or f.name.endswith(".env.next"):
            continue  # what install.sh writes beside them
        name = f.name[len("deployment-"):]
        if not NAME.fullmatch(name) or name == "v1":
            raise SystemExit(f"{f}: an instance's name is a-z then up to 15 of a-z0-9, and not v1")
        label = f.read_text().strip()
        if not LABEL.fullmatch(label):
            raise SystemExit(f"{f}: odd label {label!r}")
        out.append(Instance(name, label, bind_port(etc / f"gateway-{name}.env")))
    ports = [i.port for i in out]
    if len(set(ports)) != len(ports):
        raise SystemExit(f"two instances listen on one port: {sorted(ports)}")
    labels = [i.label for i in out]
    first = first_label(etc)
    if first:
        labels.append(first)
    if len(set(labels)) != len(labels):
        raise SystemExit(f"one deployment is served twice (the first's label included): {sorted(labels)}")
    return out


def nginx_upstreams(found: list[Instance]) -> str:
    """Each instance's upstream: its own gateway first, every time, and nginx's "restarting" answer
    only for a request that gateway could not be handed at all -- as the first gateway's."""
    return "\n".join(f"""upstream pools_gateway_{i.name} {{
    server 127.0.0.1:{i.port} max_fails=0;
    server {RESTARTING} backup;
}}
""" for i in found)


def nginx_locations(found: list[Instance]) -> str:
    """The locations that send an instance's paths to its gateway, under the same limits as the first
    gateway's: the zones are shared, so all the instances together keep inside the node's budget."""
    blocks = []
    for i in found:
        blocks.append(f"""    location = /{i.name}/v1/order {{
        limit_req zone=pools_ip burst=10 nodelay;
        limit_req zone=pools_all burst=10 nodelay;
        proxy_pass http://pools_gateway_{i.name}/v1/order;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 30s;
    }}

    location = /{i.name}/v1/health {{
        limit_req zone=pools_ip burst=10 nodelay;
        proxy_pass http://pools_gateway_{i.name}/v1/health;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }}

    location = /{i.name}/v1/faucet {{
        limit_req zone=pools_faucet_ip burst=1 nodelay;
        limit_req zone=pools_faucet_all burst=2 nodelay;
        proxy_pass http://pools_gateway_{i.name}/v1/faucet;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }}

    location = /{i.name}/v1/contacts {{
        limit_req zone=pools_contacts_ip burst=20 nodelay;
        limit_req zone=pools_contacts_write burst=2 nodelay;
        limit_req zone=pools_contacts_write_all burst=3 nodelay;
        proxy_pass http://pools_gateway_{i.name}/v1/contacts;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }}
""")
    return "\n".join(blocks)


DEX = re.compile(r"[a-z][a-z0-9]{0,15}")
HIP3_BASE, DEX_STRIDE = 100_000, 10_000  # gateway/assets.py
# Digits, and a point between digits: nothing that JSON would escape or systemd would read otherwise (it takes
# a backslash in an unquoted value as an escape), and no spelling the review could miss: no spaces, no
# underscores, no exponent.
DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")


def _number(text: object, least: Decimal, what: str) -> str:
    """A size or a leverage as the record writes it: a plain decimal string, at least `least` (above 0 for a
    size), written into the env file as it is."""
    if not isinstance(text, str) or not DECIMAL.fullmatch(text) or Decimal(text) < least or Decimal(text) == 0:
        raise SystemExit(f"gateway_limits: {what} {text!r} is not a plain decimal of at least {least}, above 0")
    return text


def gateway_limits(record: dict) -> str:
    """The gateway's limits for its deployment, from `gateway_limits` in the record, as the env lines the gateway
    and the keeper read: GATEWAY_SIZE_CAPS and GATEWAY_LEVERAGE_CAPS by order number, in the form
    gateway/demo_signer.py `size_caps` and gateway/protect.py `leverage_caps` take, and the builder dexes they
    read, GATEWAY_PERP_DEXES and COLOSSEUM_PERP_DEXES. A record without it gives none: the instance's
    gateway-<name>.env still says them, as before."""
    limits = record.get("gateway_limits")
    if limits is None:
        return ""
    if not isinstance(limits, dict):
        raise SystemExit(f"gateway_limits must be an object, not {limits!r}")
    dexes = limits.get("perp_dexes")
    perps = limits.get("perps")
    if not isinstance(dexes, list) or not all(isinstance(d, str) and DEX.fullmatch(d) for d in dexes):
        raise SystemExit(f"gateway_limits: perp_dexes must be a list of dex names, not {dexes!r}")
    if not isinstance(perps, list) or not perps:
        raise SystemExit("gateway_limits: perps must list at least one perp")
    sizes, leverages, names = {}, {}, set()
    for perp in perps:
        if not isinstance(perp, dict):
            raise SystemExit(f"gateway_limits: a perp must be an object, not {perp!r}")
        name, asset = perp.get("name"), perp.get("asset")
        if not isinstance(name, str) or name in names:
            raise SystemExit(f"gateway_limits: {name!r} is not a name of its own")
        names.add(name)
        dex = name.split(":")[0] if ":" in name else ""
        if not isinstance(asset, int) or isinstance(asset, bool) or str(asset) in sizes:
            raise SystemExit(f"gateway_limits: {name}: {asset!r} is not an order number of its own")
        # The first dex numbers its perps from 0 below 10000; a builder dex from 110000 (gateway/assets.py).
        ours = asset >= HIP3_BASE + DEX_STRIDE if dex else 0 <= asset < DEX_STRIDE
        if not ours:
            raise SystemExit(f"gateway_limits: {name}: {asset} is not an order number of its dex")
        if dex and dex not in dexes:
            raise SystemExit(f"gateway_limits: {name}: dex {dex} is not in perp_dexes")
        sizes[str(asset)] = _number(perp.get("size"), Decimal(0), f"{name} size")
        leverages[str(asset)] = _number(perp.get("leverage"), Decimal(1), f"{name} leverage")
    return (f"GATEWAY_SIZE_CAPS={json.dumps(sizes)}\n"
            f"GATEWAY_LEVERAGE_CAPS={json.dumps(leverages)}\n"
            f"GATEWAY_PERP_DEXES={','.join(dexes)}\n"
            f"COLOSSEUM_PERP_DEXES={','.join(dexes)}\n")


# A node the keeper reads, as the record names it: https, a host, a path, and nothing else. The record is public, so a
# node whose address carries a key has no place in it; and a line systemd reads must hold no space or quote.
NODE_URL = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?")
# The widest eth_getLogs window the keeper takes from each node (ops/keeper.py, MAX_LOG_WINDOW and
# MAX_BACKUP_LOG_WINDOW): Hyperliquid's node answers 50 blocks at most; of second nodes thirdweb's answered 1000 and
# dRPC's public one answers 101 (measured 11 Nov 2026), and the record names the window its own node takes.
KEEPER_MAX_LOG_WINDOW = {"primary": 50, "backup": 1000}
KEEPER_NODE_ENV = {"primary": ("COLOSSEUM_RPC_URL", "COLOSSEUM_LOG_WINDOW"),
                   "backup": ("COLOSSEUM_RPC_BACKUP_URL", "COLOSSEUM_BACKUP_LOG_WINDOW")}


def keeper_nodes(record: dict) -> str:
    """The keeper's nodes for its deployment, from `keeper_nodes` in the record, as the env lines the keeper reads:
    the node it reads everything from and the window of its log scan (COLOSSEUM_RPC_URL, COLOSSEUM_LOG_WINDOW), and
    the second node its scan takes a window to when the first refuses it, with that node's own window
    (COLOSSEUM_RPC_BACKUP_URL, COLOSSEUM_BACKUP_LOG_WINDOW). A record without it gives none: keeper.env still says
    the node, as before. With it, keeper.env's COLOSSEUM_RPC_URL is overridden, since systemd reads this file
    after it."""
    nodes = record.get("keeper_nodes")
    if nodes is None:
        return ""
    if not isinstance(nodes, dict) or "primary" not in nodes or not set(nodes) <= set(KEEPER_NODE_ENV):
        raise SystemExit(f"keeper_nodes must be an object with a primary and at most a backup, not {nodes!r}")
    lines = []
    for role in ("primary", "backup"):
        node = nodes.get(role)
        if node is None and role == "backup":
            continue
        if not isinstance(node, dict) or set(node) != {"url", "log_window"}:
            raise SystemExit(f"keeper_nodes: {role} must be an object of url and log_window, not {node!r}")
        url, window = node["url"], node["log_window"]
        if not isinstance(url, str) or not NODE_URL.fullmatch(url):
            raise SystemExit(f"keeper_nodes: {role} url {url!r} is not an https address with no key, space or quote")
        most = KEEPER_MAX_LOG_WINDOW[role]
        if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= most:
            raise SystemExit(f"keeper_nodes: {role} log_window {window!r} is not a whole number of blocks, 1 to {most}")
        url_name, window_name = KEEPER_NODE_ENV[role]
        lines += [f"{url_name}={url}\n", f"{window_name}={window}\n"]
    if nodes.get("backup") and nodes["backup"]["url"].rstrip("/") == nodes["primary"]["url"].rstrip("/"):
        raise SystemExit("keeper_nodes: the backup is the primary; a second try at the same node is no backup")
    return "".join(lines)


def deployment_env(label: str) -> str:
    """What a gateway and a keeper read of their deployment, from its committed record: its addresses, its
    gateway's limits where the record has them (`gateway_limits`), and its keeper's nodes where it has those
    (`keeper_nodes`). systemd reads this file after the instance's own gateway-<name>.env and keeper-<name>.env, so
    what the record says wins over a line by hand."""
    if not LABEL.fullmatch(label):
        raise SystemExit(f"odd label {label!r}")
    record = deployments.load(label)
    if "PoolFactory" not in record and record.get("SimPoolFactory"):
        # A simulated pool's deployment: no exchange, so no gateway lines; its keeper reads the record itself
        # and its nodes from here (ops/sim_keeper.py).
        return f"COLOSSEUM_DEPLOYMENT={label}\n" + keeper_nodes(record)
    return (f"COLOSSEUM_DEPLOYMENT={label}\n"
            f"GATEWAY_FACTORY={record['PoolFactory']}\n"
            f"GATEWAY_REGISTRY={keys_contract(record)}\n"
            + gateway_limits(record) + keeper_nodes(record) + binder_env(record) + multi_env(record))


# The wallet name a test publisher's key has beside the keeper's own (COLOSSEUM_KEY_DIR of the instance's keeper),
# which install.sh checks before it switches (ops/host/install.sh).
PUBLISHER_WALLET = "publisher"


def publisher(record: dict) -> str:
    """The address of the deployment's test publisher, a key in a file before the enclave (ops/key_relay.py), as
    its record says; "" when it has none, or its publisher is the enclave. The keeper of such a deployment signs
    the binder's bindings itself, with that wallet."""
    p = (record.get("key_binder") or {}).get("publisher") if record.get("KeyBinder") else None
    if not p or not p.get("test_key"):
        return ""
    return p["address"]


def multi_pool(record: dict) -> str:
    """The deployment's MultiPool, whose calls colosseum-multi-keeper@<name> makes (ops/multi_keeper.py); "" when it
    has none. Its keeper sends with a wallet of its own, MULTI_WALLET beside the instance keeper's."""
    return record.get("MultiPool") or ""


MULTI_WALLET = "multi-keeper"


def keeper_target(record: dict) -> str:
    """What colosseum-multi-keeper@<name> keeps: the deployment's MultiPool, or its SimPoolFactory, whose record
    ops/multi_keeper.py hands to ops/sim_keeper.py; "" for neither. Only a MultiPool has a watchdog (multi_env)."""
    return multi_pool(record) or record.get("SimPoolFactory") or ""


def multi_env(record: dict) -> str:
    """The MultiPool its watchdog reads (ops/host/watchdogs/multi_watch.py); its keeper reads the record itself."""
    pool = multi_pool(record)
    return f"COLOSSEUM_MULTI_POOL={pool}\n" if pool else ""


def binder_env(record: dict) -> str:
    """What a deployment on a key binder adds: its gateway starts with no key and reads each as it is bound
    (gateway/server.py), and with a test publisher its keeper answers the binder's requests with that wallet,
    making each key into its own directory, from where colosseum-key-copy@<name> hands it to the gateway."""
    if not record.get("KeyBinder"):
        return ""
    return "GATEWAY_KEYS_BOUND_LATER=1\n" + (
        f"COLOSSEUM_PUBLISHER_WALLET={PUBLISHER_WALLET}\n" if publisher(record) else "")


def keys_contract(record: dict) -> str:
    """Where the gateway asks which account a key is bound to: the deployment's KeyRegistry, or its KeyBinder on a
    deployment after it (docs/KEY-BINDING.md). Both answer the gateway's two reads, `bindingOf` and `isBound`, the
    same way (gateway/chain.py). A record with both, or neither, names no one contract and is refused."""
    named = [record[k] for k in ("KeyRegistry", "KeyBinder") if record.get(k)]
    if len(named) != 1:
        raise SystemExit("the record must name a KeyRegistry or a KeyBinder, one of them: the gateway reads its "
                         "keys' bindings from it")
    return named[0]


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "list":
        for i in instances(pathlib.Path(argv[1])):
            print(f"{i.name} {i.label} {i.port}")
        return 0
    if len(argv) == 2 and argv[0] == "nginx":
        sys.stdout.write(nginx_locations(instances(pathlib.Path(argv[1]))))
        return 0
    if len(argv) == 2 and argv[0] == "nginx-upstreams":
        sys.stdout.write(nginx_upstreams(instances(pathlib.Path(argv[1]))))
        return 0
    if len(argv) == 2 and argv[0] == "multi":
        if not LABEL.fullmatch(argv[1]):
            raise SystemExit(f"odd label {argv[1]!r}")
        print(keeper_target(deployments.load(argv[1])))
        return 0
    if len(argv) == 2 and argv[0] == "multi-pool":
        if not LABEL.fullmatch(argv[1]):
            raise SystemExit(f"odd label {argv[1]!r}")
        print(multi_pool(deployments.load(argv[1])))
        return 0
    if len(argv) == 2 and argv[0] == "publisher":
        if not LABEL.fullmatch(argv[1]):
            raise SystemExit(f"odd label {argv[1]!r}")
        print(publisher(deployments.load(argv[1])))
        return 0
    if len(argv) == 2 and argv[0] == "first-label":
        print(first_label(pathlib.Path(argv[1])))
        return 0
    if len(argv) == 2 and argv[0] == "deployment-env":
        sys.stdout.write(deployment_env(argv[1]))
        return 0
    print(__doc__.split("\n\n")[2], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
