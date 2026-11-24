#!/usr/bin/env python3
"""Writes the app's settings, app/config.js, for one network: from spike/hlspike/network.py and from that network's
entries in deployments/LIVE.json, with their records. With --index it writes the site's index.html for that network
instead.

    python3 scripts/app_config.py [--network testnet|mainnet] [--index] [--commit SHA] [--root DIR] [--gateway URL]
                                  [--out FILE]

The committed app/config.js is its testnet output, and app/tests/config.test.mjs holds the two equal: change
network.py or LIVE.json, run `python3 scripts/app_config.py --out app/config.js`, and commit both. With --commit it
reads that commit's files instead of this checkout's: scripts/pack-site.sh packs a mainnet site that way, from the
commit it packs. --root reads another directory laid out like this repository (the tests' examples). --gateway
puts another gateway in, for a gateway on this machine (http://127.0.0.1:8787); the test then goes red until the
committed file is written again without it.

The site reads the network's `live` entry, then its `archive` ones, in LIVE.json's order, and opens the first
`shared` entry on the live one's factory. Its `testers` entries it reads and offers nothing on: they go into
`readOnlyDeployments`, apart from the deployments the pages buy, trade and open pools on.

A network with no live entry gets a site of simulation pools: its one `staged` simulation-pool deployment (a record
that names a SimPoolFactory) goes into `sim`, the settings name no other deployment, and the pages show that
factory's pools and nothing else (docs/DEPLOYMENTS.md). A network with neither has nothing for a site to read, and
the script stops rather than write settings for one. Testnet has a live entry, so its staged simulation-pool
deployment is not in its settings: its simulation pages open by a pool's address, as they did.

Everything a page says of the network comes from here: the chain a wallet is offered, the public node, the name the
pages write (`networkName`), and index.html's own words (--index): the committed app/index.html is testnet's, and
another network's is that file with the lines that name the network replaced, each of which must be found exactly
once. It only reads files and needs no key or network.
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
# What the pages call each network. network.py's `chain_name` is the chain as a wallet lists it; this is the name a
# page writes beside a pool, and no page writes one of its own (app/lib/network.js).
NETWORK_NAME = {"testnet": "Hyperliquid testnet", "mainnet": "Hyperliquid"}
INDEX = "app/index.html"
# index.html's lines that name the network, and what a site built for mainnet says in their place: (the committed
# text, mainnet's, how many times the committed file has it). The plaque is the panels' own (app/lib/examscreen.js,
# SCREEN_TEXT.mainnetPlaque; app/tests/config.test.mjs holds the two equal). The picture of a shared link has testnet's
# plaque drawn into it, so a mainnet page names no picture until there is one of its own.
INDEX_WORDS = {
    "mainnet": [
        ("<title>Pools on Hyperliquid · testnet</title>", "<title>Pools on Hyperliquid</title>", 1),
        ('<meta property="og:title" content="Pools on Hyperliquid · testnet">',
         '<meta property="og:title" content="Pools on Hyperliquid">', 1),
        (' Hyperliquid testnet.">', ' Hyperliquid.">', 2),
        ('  <meta property="og:image" content="https://pools.usenami.io/brand/og-image-1200x630.png">\n'
         '  <meta property="og:image:width" content="1200">\n'
         '  <meta property="og:image:height" content="630">\n'
         '  <meta property="og:image:alt" content="Upools, pools.usenami.io">\n'
         '  <meta name="twitter:card" content="summary_large_image">\n', "", 1),
        ('<div class="notice">Hyperliquid <strong>testnet</strong> and mock USDC.</div>',
         '<div class="notice">Mainnet. Real USDC. Unaudited.</div>', 1),
        ("<p>This is a hackathon demo on Hyperliquid <strong>testnet</strong>, with mock funds that have no value.</p>",
         "<p>Mainnet. Real USDC. Unaudited.</p>", 1),
        ("I understand this is a testnet demo.", "I understand.", 1),
    ],
}
# The same file's lines that belong to the pools of a PoolFactory deployment: their landing line, their pages in the
# menu, the footer's sentence about their keys and the terms of their demo. A site of simulation pools has none of them.
INDEX_SIM_ONLY = [
    ("Investors open pools with rules; traders and AI agents take challenges without ever holding a key.", "Pools on", 2),
    ('      <a href="#/new">New pool</a>\n'
     '      <a href="#/shared">Shared pool</a>\n'
     '      <a href="#/economics">Economics</a>\n'
     '      <a href="#/verify">Check it yourself</a>\n'
     '      <a href="#/evidence">On chain</a>\n', "", 1),
    ("    <span>No trader holds a key: in this demo the pool gateway holds them.</span>\n", "", 1),
    ('    <a href="#/terms">Terms</a>\n', "", 1),
]

HEAD = """\
// The app's settings for Hyperliquid {name}, written by scripts/app_config.py from spike/hlspike/network.py and
// deployments/LIVE.json. {how}
"""
GATEWAY_NOTE = """\
// `gateway` is the pools host behind Cloudflare (docs/HOSTING.md); for a gateway on this machine, run the script
// with --gateway http://127.0.0.1:8787.
"""
SIM_NOTE = """\
  // The simulation pools the site shows, and the only pools it shows: the network's one staged simulation-pool
  // deployment in deployments/LIVE.json, which a network with no live deployment gives its site (docs/DEPLOYMENTS.md).
  //   label     the entry's label, whose record is deployments/<network>-<label>.json;
  //   factory   the record's SimPoolFactory: the first page lists its `pools()`, and a pool's pages, an exam's screen
  //             and the panels open only for a pool that list names (app/lib/simsite.js).
  // `simIndex`, when the entry names a `gateway`, is where the pools' index answers (gateway/simindex.py); without
  // one the panels read the chain themselves.
"""
NO_DEPLOYMENTS_NOTE = """\
  // No PoolFactory deployment is live on this network: the site has none of those pools' pages.
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


def keys_of(record: dict, label: str) -> dict:
    """Where the deployment's keys are bound, as a row's fields: its KeyRegistry, or its KeyBinder with `keyBinder`
    true. The same rule as the host's (ops/host_instances.py, keys_contract): a record with both, or neither, names
    no one contract and is refused, rather than a site that reads keys from the wrong one."""
    named = [k for k in ("KeyRegistry", "KeyBinder") if record.get(k)]
    if len(named) != 1:
        raise SystemExit(f"{label}: the record must name a KeyRegistry or a KeyBinder, one of them: the "
                         "site reads its keys' bindings from it")
    return {"registry": record[named[0]], **({"keyBinder": True} if named[0] == "KeyBinder" else {})}


def site_entries(network, entries: list[dict], files: Files) -> list[dict]:
    """The deployments of PoolFactory pools the site reads: the live one, then the archive. None on a network with no
    live entry, whose site is of simulation pools (sim_entry)."""
    mine = [e for e in entries if e["network"] == network.name]
    live = [e for e in mine if e["role"] == "live"]
    if len(live) > 1:
        raise SystemExit(f"{len(live)} live deployments on {network.name} in {LIVE}: the site reads exactly one")
    if not live:
        return []
    live = live[0]
    shared = next((e for e in mine if e["role"] == "shared" and e["factory"] == live["factory"]), None)
    out = []
    for entry in [live] + [e for e in mine if e["role"] == "archive"]:
        record = record_of(entry, network, files)
        row = {
            "label": entry["label"],
            "role": entry["role"],
            "factory": entry["factory"],
            **keys_of(record, entry["label"]),
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


def sim_entry(network, entries: list[dict], files: Files) -> dict | None:
    """The simulation-pool deployment the site of a network with no live entry shows: its one `staged` entry whose
    record names a SimPoolFactory. None on a network with a live entry, whose settings name no simulation pool. A
    network with neither, or with two such entries, gets no settings: the site shows the pools of one factory."""
    here = [e for e in entries if e["network"] == network.name]
    if any(e["role"] == "live" for e in here):
        return None
    staged = [(e, record_of(e, network, files)) for e in here if e["role"] == "staged"]
    sims = [(e, r) for e, r in staged if "SimPoolFactory" in r]
    if not sims:
        raise SystemExit(f"0 live deployments on {network.name} in {LIVE}, and no staged simulation-pool deployment: "
                         "there is nothing for a site to read")
    if len(sims) > 1:
        raise SystemExit(f"{len(sims)} staged simulation-pool deployments on {network.name} in {LIVE}: the site shows "
                         "the pools of one; retire the others")
    entry, record = sims[0]
    if record["SimPoolFactory"] != entry["factory"]:
        raise SystemExit(f"{entry['label']}: the entry's factory is not the record's SimPoolFactory")
    # The pages call the pools' token USDC, and on mainnet real USDC: a factory on another token is not shown as one.
    if str(record.get("usdc", "")).lower() != network.usdc_erc20.lower():
        raise SystemExit(f"{entry['record']}: usdc {record.get('usdc')}, not {network.name}'s {network.usdc_erc20}: "
                         "the pages call the pools' token USDC")
    row = {"label": entry["label"], "factory": entry["factory"]}
    return {"row": row, "index": entry["gateway"]}


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
            **keys_of(record, entry["label"]),
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


def network_name(network) -> str:
    """The name the pages write for the network. One network.py defines and this file does not name gets no settings:
    a page would otherwise say nothing, or another network's name."""
    if network.name not in NETWORK_NAME:
        raise SystemExit(f"no name for the pages to call {network.name!r}: add it to NETWORK_NAME in scripts/app_config.py")
    return NETWORK_NAME[network.name]


def config(network, entries: list[dict], files: Files, gateway: str | None = None, default: str = "testnet") -> str:
    site = site_entries(network, entries, files)
    sim = sim_entry(network, entries, files)
    live = next((e for e in entries if e["network"] == network.name and e["role"] == "live"), None)
    # Orders go to the live deployment's gateway. A site of simulation pools sends none and has no gateway.
    orders = [] if live is None else [
        ("gateway", js(gateway if gateway is not None else live["gateway"])),
    ]
    fields = [
        ("chainId", js(network.chain_id)),
        ("chainHex", js(hex(network.chain_id))),
        ("chainName", js(network.chain_name)),
        ("networkName", js(network_name(network))),
        ("nativeCurrency", NATIVE),
        ("rpc", js(network.rpc_url)),
        ("hlInfo", js(network.api_url + "/info")),
        ("hlExchange", js(network.api_url + "/exchange")),
        ("hlApp", js(network.app_url)),
        ("hyperliquidChain", js(network.hyperliquid_chain)),
        *orders,
        ("usdc", js(network.usdc_erc20)),
    ]
    how = HOW_COMMITTED if network.name == default else HOW_PACKED.format(name=network.name)
    head = HEAD.format(name=network.name, how=how) + (GATEWAY_NOTE if live is not None else "")
    lines = [head.rstrip("\n"), "export const CONFIG = {"]
    lines += [f"  {key}: {value}," for key, value in fields]
    if sim is not None:
        # Where the index answers: --gateway for one on this machine, else the entry's own, else none is named.
        index = gateway if gateway is not None else sim["index"]
        lines.append(SIM_NOTE.rstrip("\n"))
        lines.append("  sim: {")
        lines += [f"    {key}: {js(value)}," for key, value in sim["row"].items()]
        lines.append("  },")
        if index is not None:
            lines.append(f"  simIndex: {js(index)},")
    lines.append((DEPLOYMENTS_NOTE if site else NO_DEPLOYMENTS_NOTE).rstrip("\n"))
    lines.append("  deployments: [")
    for row in site:
        lines.append("    {")
        lines += [f"      {key}: {js(value)}," for key, value in row.items()]
        lines.append("    },")
    lines.append("  ],")
    # A site of simulation pools names no other deployment: it has no page for a PoolFactory's pools, read or sold.
    read_only = [] if sim is not None else read_only_entries(network, entries, files)
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


def index(network, entries: list[dict], files: Files) -> str:
    """The site's index.html for the network: the files' own, with the lines that name another network replaced by
    this one's, and on a site of simulation pools without the lines that belong to the other pools. A line that is not
    there exactly as many times as expected stops the script: the page was edited, and its words for this network have
    to follow it by hand, not by a guess."""
    network_name(network)
    text = files.text(INDEX)
    words = list(INDEX_WORDS.get(network.name, []))
    if sim_entry(network, entries, files) is not None:
        words += INDEX_SIM_ONLY
    for old, new, times in words:
        found = text.count(old)
        if found != times:
            raise SystemExit(f"{INDEX}: {found} places read {old!r}, not {times}: the page changed, and its words for "
                             f"{network.name} in scripts/app_config.py have to follow it")
        text = text.replace(old, new)
    return text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--network", default="testnet")
    p.add_argument("--index", action="store_true", help="write the site's index.html for the network, not its settings")
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
    if args.index:
        if args.gateway is not None:
            p.error("--gateway is a setting: index.html has none")
        site_entries(network, entries, files)  # the same refusals as the settings': no page for a network with no site
        text = index(network, entries, files)
    else:
        text = config(network, entries, files, args.gateway, default)
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
