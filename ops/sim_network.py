"""What the simulated pool's scripts need per network, in one place, and the gate on sending to mainnet.

The network itself (chain id, JSON-RPC, the info API, USDC) is spike/hlspike/network.py's, chosen by
COLOSSEUM_NETWORK; this module adds what is the simulated pool's own: the perp index of each asset a pool may list
(testnet's and mainnet's differ), the asset --measure trades, and the precompiles the paper exam reads its prices
from. The precompiles are at the same addresses on both networks (they are hyper-evm-lib's HLConstants, compiled
into PaperExam); they are here so that ops/sim_probe.py reads the same ones.

Where the values come from:

- the perp indices: Hyperliquid's info API, `{"type": "meta"}`, the position of the asset in `universe`
  (https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals). Read on
  14 November 2026: mainnet BTC 0, ETH 1, SOL 5; testnet BTC 3, ETH 4, SOL 0. `check_assets` reads it again
  before anything is sent and stops on a difference.
- the precompiles: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/interacting-with-hypercore
  and hyper-evm-lib's src/common/HLConstants.sol: oracle price 0x…0807, best bid and offer 0x…080e, perp asset
  info 0x…080a.

HYPE is not listed: the platform's decision of 11 November 2026 (ops/deploy_sim.py).

Mainnet is only ever chosen by naming it twice: COLOSSEUM_NETWORK=mainnet, which the plumbing reads when it is
imported, and --network mainnet on the command line. A command that only reads, or that prints calls for a signer
elsewhere (--signer: our Safe, the Ledger), needs nothing more. A command that sends needs a confirmation as well:

- a one-off command (deploy_sim's, sim_check --lift, sim_keeper --measure) asks the person at the terminal to
  type the confirmation phrase, and refuses without a terminal;
- a service (sim_keeper, sim_relay, sim_check with a wallet) takes --confirm-mainnet with the deployment's label,
  so that a unit file copied from testnet does not start on mainnet until someone writes the label into it.

Either way the keys must come from a directory named for mainnet: COLOSSEUM_KEY_DIR set, and not testnet's
default.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable

from spike.hlspike import network as networks

ORACLE_PX = "0x0000000000000000000000000000000000000807"
BBO = "0x000000000000000000000000000000000000080e"
PERP_ASSET_INFO = "0x000000000000000000000000000000000000080a"

CONFIRM_PHRASE = "send on mainnet"
TESTNET_KEY_DIR = os.path.expanduser("~/secure/colosseum-testnet")


@dataclass(frozen=True)
class SimNet:
    name: str
    assets: dict          # asset name -> perp index on this network
    measure_asset: str    # what sim_keeper --measure trades by default

    def index(self, name: str) -> int:
        if name not in self.assets:
            raise SystemExit(f"{name} is not a {self.name} platform asset ({', '.join(self.assets)})")
        return self.assets[name]


SIM = {
    "testnet": SimNet("testnet", {"BTC": 3, "ETH": 4, "SOL": 0}, "BTC"),
    "mainnet": SimNet("mainnet", {"BTC": 0, "ETH": 1, "SOL": 5}, "BTC"),
}


def sim(net: networks.Network | None = None) -> SimNet:
    return SIM[(net or networks.select()).name]


def add_args(p, service: bool = False) -> None:
    """--network for every simulated-pool script; --confirm-mainnet for the services."""
    p.add_argument("--network", choices=sorted(networks.NETWORKS), default=networks.DEFAULT,
                   help="testnet unless named; mainnet also needs COLOSSEUM_NETWORK=mainnet")
    if service:
        p.add_argument("--confirm-mainnet", metavar="LABEL",
                       help="on mainnet, the deployment's label: a service sends nothing there without it")


def selected(args, net: networks.Network | None = None) -> networks.Network:
    """The network, when --network and COLOSSEUM_NETWORK agree; otherwise the program stops."""
    net = net or networks.select()
    flag = getattr(args, "network", None) or networks.DEFAULT
    if flag != net.name:
        raise SystemExit(f"refusing to run: --network {flag}, but {networks.ENV} selects {net.name}; "
                         "name the same network in both")
    return net


def key_dir_ok(net: networks.Network, env=None) -> None:
    env = os.environ if env is None else env
    if net.name != "mainnet":
        return
    raw = env.get("COLOSSEUM_KEY_DIR", "")
    if not raw or os.path.realpath(os.path.expanduser(raw)) == os.path.realpath(TESTNET_KEY_DIR):
        raise SystemExit("refusing to send on mainnet: COLOSSEUM_KEY_DIR must name mainnet's key directory, "
                         "not testnet's default")


def confirm_send(args, what: str, c, ask: Callable[[str], str] | None = None, tty: bool | None = None) -> None:
    """Before a one-off command sends. Testnet: the plumbing's own check, as before. Mainnet: both switches, a
    mainnet key directory, the phrase typed at a terminal, and the RPC's chain id."""
    net = selected(args, c.NETWORK)
    if net.name != "mainnet":
        c.assert_testnet()
        return
    key_dir_ok(net)
    tty = sys.stdin.isatty() if tty is None else tty
    if not tty:
        raise SystemExit("refusing to send on mainnet without a terminal: the phrase is typed by a person")
    ask = ask or input
    print(f"MAINNET, chain id {net.chain_id}, RPC {c.RPC_URL.split('?')[0]}: {what}")
    if ask(f"type '{CONFIRM_PHRASE}' to send, anything else to stop: ").strip() != CONFIRM_PHRASE:
        raise SystemExit("not confirmed; nothing was sent")
    c.assert_network()


def confirm_service(args, label: str, c, sends: bool) -> None:
    """Before a service starts. Testnet: the plumbing's check. Mainnet: both switches; a service that will send
    also needs --confirm-mainnet with its deployment's label and a mainnet key directory. Reading needs neither."""
    net = selected(args, c.NETWORK)
    if net.name != "mainnet":
        c.assert_testnet()
        return
    if sends:
        if getattr(args, "confirm_mainnet", None) != label:
            raise SystemExit(f"refusing to send on mainnet: --confirm-mainnet {label} is needed, the deployment's label")
        key_dir_ok(net)
    c.assert_network()


def check_assets(names, net: networks.Network, info_post) -> None:
    """The network's meta, read now, against the table: each name at its index and not delisted."""
    universe = info_post({"type": "meta"})["universe"]
    table = sim(net)
    for name in names:
        idx = table.index(name)
        got = universe[idx] if idx < len(universe) else {}
        if got.get("name") != name or got.get("isDelisted"):
            raise SystemExit(f"{net.name} meta changed: index {idx} is {got.get('name')}, expected {name}")
