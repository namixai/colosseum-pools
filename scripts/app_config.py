#!/usr/bin/env python3
"""Writes the app's settings, app/config.js, for one network: from spike/hlspike/network.py and from that network's
entries in deployments/LIVE.json, with their records.

    python3 scripts/app_config.py [--network testnet|mainnet] [--commit SHA] [--root DIR] [--gateway URL] [--out FILE]

The committed app/config.js is its testnet output, and app/tests/config.test.mjs holds the two equal: change
network.py or LIVE.json, run `python3 scripts/app_config.py --out app/config.js`, and commit both. With --commit it
reads that commit's files instead of this checkout's: scripts/pack-site.sh packs a mainnet site that way, from the
commit it packs. --root reads another directory laid out like this repository (the tests' examples). --gateway
puts another gateway in, for a gateway on this machine (http://127.0.0.1:8787); the test then goes red until the
committed file is written again without it.

The site reads the network's `live` entry, then its `archive` ones, in LIVE.json's order, and opens the first
`shared` entry on the live one's factory. Its `testers` entries it reads and offers nothing on: they go into
`readOnlyDeployments`, apart from the deployments the pages buy, trade and open pools on. A network with no live entry has nothing for a site to read, and the
script stops rather than write settings for one. It only reads files and needs no key or network.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
NETWORK_PY = "spike/hlspike/network.py"
LIVE = "deployments/LIVE.json"
# The pool the team opened on testnet's live deployment for the live run on 3 Oct 2026 (createPool in block
# 65902379). No record carries it: those say what was deployed, and this pool was opened on it afterwards.
LIVE_RUN_POOLS = {("testnet", "demo2"): "0x70066669eC7Eb992055c82B615eD822CFF14e44A"}
NATIVE = '{ name: "HYPE", symbol: "HYPE", decimals: 18 }'  # HYPE, 18 decimals, on both networks (network.py's docs)

HEAD = """\
// The app's settings for Hyperliquid {name}, written by scripts/app_config.py from spike/hlspike/network.py and
// deployments/LIVE.json. {how}
// `gateway` is the pools host behind Cloudflare (docs/HOSTING.md); for a gateway on this machine, run the script
// with --gateway http://127.0.0.1:8787.
"""
# The committed app/config.js is the default network's; another network's goes only into a pack.
HOW_COMMITTED = """Edit those and run `python3 scripts/app_config.py --out app/config.js`
// rather than this file: app/tests/config.test.mjs holds the committed copy to the script's output."""
HOW_PACKED = """scripts/pack-site.sh --network {name} puts this in a pack in place of the
// committed app/config.js, which is another network's."""
DEPLOYMENTS_NOTE = """\
  // The deployments the site reads: the network's live entry in deployments/LIVE.json, then its archive ones. Every
  // field comes from the entry or its record, deployments/<network>-<label>.json, and app/tests/deployments.test.mjs
  // holds each one to them, so the site and the host cannot part silently.
  //   factory, registry  the deployment's PoolFactory, and where its keys are bound: its KeyRegistry, or its
  //                      KeyBinder on a deployment after it (docs/KEY-BINDING.md);
  //   keyBinder          true when `registry` is a KeyBinder: chain.js then reads it with the binder's ABI, which
  //                      answers bindingOf, isBound and keyOf as the registry does and has no freeCount. Absent for a
  //                      KeyRegistry.
  //   deployBlock        its first block: where the check-it-yourself page says its history starts; nothing scans
  //                      back towards it (app/lib/keys.js);
  //   deployer           the deploy key: the pools it owns are our benches, which the list names (app/lib/listing.js);
  //   platformAssets     the perp indices the factory lists. The chain stays the authority -- the new-pool form asks
  //                      isPlatformAsset for each -- this only keeps the form from probing every perp at once.
  //   sharedPool         the shared pool #/shared opens when the link names no address, and the only one the page
  //                      takes a deposit into: the network's first shared entry on the live factory. Its seats are
  //                      made by this deployment's factory.
  //   sharedPoolRecord   the label of that pool's record: the page's test holds the address to it. The archive has
  //                      none: its shared pools open by address and take no deposit.
  //   liveRunPool        a pool the team opened on the deployment after it was deployed, for a live run. The deploy
  //                      key owns it as it owns the benches, and its terms are not a bench's, so the list names it
  //                      apart (app/lib/listing.js). No record carries it; scripts/app_config.py keeps it.
  //   assetLeverage      true for a factory of v3 (its record names a PaperExamImpl), whose Rules carry each asset's
  //                      own leverage cap (assetLeverageX100): createPool then sends five fields
  //                      (app/lib/deployments.js, poolRules) and the pages show those caps. Absent for a factory
  //                      before v3, which takes four.
  // The live one is what the pool gateway and the keepers serve: pools are bought, traded and opened there. An
  // archive one is kept so its records can still be read and checked: it sells nothing.
"""


class Files:
    """This checkout's files, another directory's, or a commit's."""

    def __init__(self, root: pathlib.Path, commit: str | None):
        self.root, self.commit = root, commit

    def text(self, path: str) -> str:
        if self.commit is None:
            return (self.root / path).read_text()
        answer = subprocess.run(["git", "-C", str(self.root), "show", f"{self.commit}:{path}"],
                                capture_output=True, text=True)
        if answer.returncode != 0:
            raise SystemExit(f"no {path} in {self.commit}: {answer.stderr.strip()}")
        return answer.stdout


def load_network(files: Files, name: str):
    """network.py as the files have it, run as a module of its own: a commit's, when one is named. Returns the
    network and the name of the default one, whose settings are the committed app/config.js."""
    module = types.ModuleType("app_config_network")
    sys.modules[module.__name__] = module  # dataclass looks its module up while it builds the class
    exec(compile(files.text(NETWORK_PY), NETWORK_PY, "exec"), module.__dict__)
    if name not in module.NETWORKS:
        raise SystemExit(f"no network {name!r} in {NETWORK_PY}: {', '.join(module.NETWORKS)}")
    return module.NETWORKS[name], module.DEFAULT


def js(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(js(v) for v in value) + "]"
    raise TypeError(f"no JavaScript for {value!r}")


def record_of(entry: dict, network, files: Files) -> dict:
    """The entry's record, which must be on the network's chain."""
    record = json.loads(files.text(entry["record"]))
    if record.get("chain_id") != network.chain_id:
        raise SystemExit(f"{entry['record']}: chain_id {record.get('chain_id')}, not {network.name}'s {network.chain_id}")
    return record


def is_v3(record: dict) -> bool:
    """A v3 factory's record names its exam implementation; one before v3 has none."""
    return "PaperExamImpl" in record


def keys_of(record: dict) -> dict:
    """Where the deployment's keys are bound, as a row's fields: its KeyRegistry, or its KeyBinder with `keyBinder`
    true. The same rule as the host's (ops/host_instances.py, keys_contract): a record with both, or neither, names
    no one contract and is refused, rather than a site that reads keys from the wrong one."""
    named = [k for k in ("KeyRegistry", "KeyBinder") if record.get(k)]
    if len(named) != 1:
        raise SystemExit(f"{record.get('label')}: the record must name a KeyRegistry or a KeyBinder, one of them: the "
                         "site reads its keys' bindings from it")
    return {"registry": record[named[0]], **({"keyBinder": True} if named[0] == "KeyBinder" else {})}


def site_entries(network, entries: list[dict], files: Files) -> list[dict]:
    mine = [e for e in entries if e["network"] == network.name]
    live = [e for e in mine if e["role"] == "live"]
    if len(live) != 1:
        raise SystemExit(f"{len(live)} live deployments on {network.name} in {LIVE}: the site reads exactly one")
    live = live[0]
    shared = next((e for e in mine if e["role"] == "shared" and e["factory"] == live["factory"]), None)
    out = []
    for entry in [live] + [e for e in mine if e["role"] == "archive"]:
        record = record_of(entry, network, files)
        row = {
            "label": entry["label"],
            "role": entry["role"],
            "factory": entry["factory"],
            **keys_of(record),
            "deployBlock": record["block"],
            "deployer": entry["deployer"],
            "platformAssets": list(record["platform_assets"].values()),
        }
        if entry["role"] == "live" and shared is not None:
            row["sharedPool"] = shared["pool"]
            row["sharedPoolRecord"] = shared["label"]
        run = LIVE_RUN_POOLS.get((network.name, entry["label"]))
        if run:
            row["liveRunPool"] = run
        if is_v3(record):
            row["assetLeverage"] = True
        out.append(row)
    return out


READ_ONLY_NOTE = """\
  // Deployments the site reads and offers nothing on: no purchase, no order, no new pool (app/lib/deployments.js,
  // READ_ONLY_DEPLOYMENTS). They are the network's `testers` entries in deployments/LIVE.json, kept out of
  // `deployments` above, which the pages buy, trade and open pools on. app/tests/readonly.test.mjs holds each to its
  // record. The fields of `deployments` above, and:
  //   record         the entry's label in LIVE.json, whose record is deployments/<network>-<record>.json;
  //   usdc           the token its pools take a challenge's price in, where the record names its own;
  //   gateway        its own gateway instance, which serves its accounts' orders;
  //   builtAt        the commit its contracts were built from. app/data/abi-v3-demo3.json is the ABI of demo3's at
  //                  that commit, and the test holds every call the app makes to them to it.
"""


def read_only_entries(network, entries: list[dict], files: Files) -> list[dict]:
    """The network's `testers` entries, as the site reads them: apart, and read only."""
    out = []
    for entry in (e for e in entries if e["network"] == network.name and e["role"] == "testers"):
        record = record_of(entry, network, files)
        row = {
            "label": record["label"],
            "record": entry["label"],
            "role": "read-only",
            "factory": entry["factory"],
            **keys_of(record),
            "deployBlock": record["block"],
            "deployer": entry["deployer"],
            "platformAssets": list(record["platform_assets"].values()),
        }
        if is_v3(record):
            row["assetLeverage"] = True
        if record.get("usdc"):
            row["usdc"] = record["usdc"]
        row["gateway"] = entry["gateway"]
        row["builtAt"] = record["commit"]
        out.append(row)
    return out


def config(network, entries: list[dict], files: Files, gateway: str | None = None, default: str = "testnet") -> str:
    site = site_entries(network, entries, files)
    live = next(e for e in entries if e["network"] == network.name and e["role"] == "live")
    fields = [
        ("chainId", js(network.chain_id)),
        ("chainHex", js(hex(network.chain_id))),
        ("chainName", js(network.chain_name)),
        ("nativeCurrency", NATIVE),
        ("rpc", js(network.rpc_url)),
        ("hlInfo", js(network.api_url + "/info")),
        ("hlExchange", js(network.api_url + "/exchange")),
        ("hlApp", js(network.app_url)),
        ("hyperliquidChain", js(network.hyperliquid_chain)),
        ("gateway", js(gateway if gateway is not None else live["gateway"])),
        ("usdc", js(network.usdc_erc20)),
    ]
    how = HOW_COMMITTED if network.name == default else HOW_PACKED.format(name=network.name)
    lines = [HEAD.format(name=network.name, how=how).rstrip("\n"), "export const CONFIG = {"]
    lines += [f"  {key}: {value}," for key, value in fields]
    lines.append(DEPLOYMENTS_NOTE.rstrip("\n"))
    lines.append("  deployments: [")
    for row in site:
        lines.append("    {")
        lines += [f"      {key}: {js(value)}," for key, value in row.items()]
        lines.append("    },")
    lines.append("  ],")
    read_only = read_only_entries(network, entries, files)
    if read_only:
        lines.append(READ_ONLY_NOTE.rstrip("\n"))
        lines.append("  readOnlyDeployments: [")
        for row in read_only:
            lines.append("    {")
            lines += [f"      {key}: {js(value)}," for key, value in row.items()]
            lines.append("    },")
        lines.append("  ],")
    lines += ["};", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--network", default="testnet")
    p.add_argument("--commit", help="read this commit's files rather than the checkout's")
    p.add_argument("--root", type=pathlib.Path, default=ROOT, help="the repository, or a directory laid out like it")
    p.add_argument("--gateway", help="another gateway than the live entry's, e.g. http://127.0.0.1:8787")
    p.add_argument("--out", type=pathlib.Path, help="write here rather than print")
    args = p.parse_args(argv)
    if args.gateway is not None and not args.gateway.startswith(("http://", "https://")):
        p.error(f"--gateway takes a URL, not {args.gateway!r}")
    files = Files(args.root, args.commit)
    network, default = load_network(files, args.network)
    entries = json.loads(files.text(LIVE))["deployments"]
    text = config(network, entries, files, args.gateway, default)
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
