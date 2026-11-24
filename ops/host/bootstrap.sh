#!/usr/bin/env bash
# One-time setup of the pools host, run as root on the host (docs/HOSTING.md). Running it
# again creates only what is missing and changes nothing that is there.
#
#   - Python 3.12 and nginx from the distribution, if missing;
#   - users colosseum-gw (the gateway) and colosseum-keeper, each able to read only its own
#     /var/lib directory;
#   - /opt/colosseum-pools for releases, /etc/colosseum for settings;
#   - gateway.env, keeper.env and shared-keeper.env: paths, URLs, the app's origin and the shared
#     pool its keeper serves, no secret;
#   - the TLS key for pools-api.usenami.io and a certificate request (CSR) for Cloudflare's
#     Origin CA. The key never leaves the host; the CSR is printed, it is public.
#
# The agent keys and the keeper key are made afterwards, as their own users (docs/HOSTING.md).
#
#   ops/host/bootstrap.sh --instance <name> <label> <port>
#
# then adds a further instance: a gateway and a keeper for one more deployment beside the first
# (ops/host_instances.py). It makes the instance's own key directories and writes, when missing,
# /etc/colosseum/deployment-<name> (the label), gateway-<name>.env and keeper-<name>.env. The
# instance's keys are made or copied afterwards, as for the first; install.sh then routes it.
#
#   ops/host/bootstrap.sh --sim-instance <name> <label>
#
# adds a simulated pool's instance (docs/SIM-POOL.md): no exchange, so no gateway, no instance keeper and no
# nginx route. colosseum-multi-keeper@<name> runs for it, with the factory's filler, and colosseum-sim-checker@<name>,
# with the factory's checker. It makes the keeper's directories and writes, when missing, deployment-<name> and
# keeper-<name>.env; never gateway-<name>.env, whose absence is how ops/host_instances.py and install.sh know the
# instance has no gateway.
#
#   ops/host/bootstrap.sh --network <testnet|mainnet>
#
# names the one network this host serves, in /etc/colosseum/host.env, and sets the host up for it. One host serves one
# network: a host without the file is a testnet host, as every host was before it, and the file once written is never
# changed here. A mainnet host gets none of testnet's settings (gateway.env, keeper.env, shared-keeper.env) and no
# instance but one simulated pool's, whose keeper-<name>.env is written for mainnet, with the lines a person adds by
# hand named in it, and whose index's settings, sim-index-<name>.env, beside it (docs/HOSTING.md, "Mainnet").
set -euo pipefail

say() { printf 'bootstrap: %s\n' "$*"; }

# The host's network as ops/host_instances.py reads it (`host_network`; a test holds the two to the same files): one
# line of its own in host.env, COLOSSEUM_NETWORK=testnet or COLOSSEUM_NETWORK=mainnet, comment lines apart; no file,
# testnet.
host_env=/etc/colosseum/host.env
host_network() {
  local lines
  [ -e "$host_env" ] || { echo testnet; return 0; }
  lines="$(grep -v '^[[:space:]]*#' "$host_env" | grep -v '^[[:space:]]*$' || true)"
  case "$lines" in
    COLOSSEUM_NETWORK=testnet) echo testnet ;;
    COLOSSEUM_NETWORK=mainnet) echo mainnet ;;
    *) return 1 ;;
  esac
}

instance=""
sim=""
want_network=""
if [ "${1:-}" = "--network" ]; then
  [ $# -eq 2 ] && { [ "$2" = testnet ] || [ "$2" = mainnet ]; } \
    || { echo "bootstrap: usage: bootstrap.sh --network <testnet|mainnet>" >&2; exit 2; }
  want_network="$2"
elif [ "${1:-}" = "--sim-instance" ]; then
  [ $# -eq 3 ] || { echo "bootstrap: usage: bootstrap.sh --sim-instance <name> <label> (no port: no gateway)" >&2; exit 2; }
  instance="$2"
  instance_label="$3"
  sim=1
elif [ "${1:-}" = "--instance" ]; then
  [ $# -eq 4 ] || { echo "bootstrap: usage: bootstrap.sh --instance <name> <label> <port>" >&2; exit 2; }
  instance="$2"
  instance_label="$3"
  instance_port="$4"
  # Its own port: 8787 is the first gateway's.
  [[ "$instance_port" =~ ^[0-9]+$ ]] && [ "$instance_port" -ge 8788 ] && [ "$instance_port" -le 8799 ] \
    || { echo "bootstrap: an instance's port is 8788..8799 (8787 is the first gateway's)" >&2; exit 2; }
elif [ $# -gt 0 ]; then
  echo "bootstrap: usage: bootstrap.sh [--network <testnet|mainnet> | --instance <name> <label> <port> | --sim-instance <name> <label>]" >&2
  exit 2
fi
if [ -n "$instance" ]; then
  # Either kind, the same rules as ops/host_instances.py: a path segment and a unit's name, and a label.
  [[ "$instance" =~ ^[a-z][a-z0-9]{0,15}$ ]] && [ "$instance" != v1 ] \
    || { echo "bootstrap: an instance's name is a-z then up to 15 of a-z0-9, and not v1" >&2; exit 2; }
  [[ "$instance_label" =~ ^[a-z0-9-]+$ ]] || { echo "bootstrap: odd label $instance_label" >&2; exit 2; }
fi

# One host, one network: read before anything is installed or made, and what is asked for the other one is refused
# here.
network="$(host_network)" \
  || { echo "bootstrap: $host_env names the host's network on one line of its own, COLOSSEUM_NETWORK=testnet or COLOSSEUM_NETWORK=mainnet, and nothing else" >&2; exit 2; }
if [ -n "$want_network" ] && [ "$want_network" != "$network" ]; then
  if [ -e "$host_env" ]; then
    echo "bootstrap: this host serves $network ($host_env): one host serves one network, and the file is never changed here" >&2
    exit 2
  fi
  # A host that served testnet is no mainnet host while anything of testnet's is still named on it.
  for left in /etc/colosseum/deployment /etc/colosseum/deployment-* /etc/colosseum/shared-keeper.env; do
    [ ! -e "$left" ] \
      || { echo "bootstrap: $left is here: this host serves testnet's deployments. Take them off first (docs/HOSTING.md, \"Mainnet\"): one host serves one network" >&2; exit 2; }
  done
  network="$want_network"
fi
if [ "$network" = mainnet ] && [ -n "$instance" ]; then
  [ -n "$sim" ] \
    || { echo "bootstrap: this is a mainnet host ($host_env): it serves one simulated pool's instance (--sim-instance), never an exchange's" >&2; exit 2; }
  for other in /etc/colosseum/deployment-*; do
    case "$other" in
      *.env | *.next | *.previous | "/etc/colosseum/deployment-$instance" | "/etc/colosseum/deployment-*") continue ;;
    esac
    echo "bootstrap: this is a mainnet host ($host_env) and $other names an instance already: it serves one, as one user (docs/HOSTING.md, \"Mainnet\")" >&2
    exit 2
  done
fi

# Amazon Linux 2023: its python3 is 3.9, and CI tests on 3.12.
for pkg in python3.12 nginx; do
  if command -v "$pkg" >/dev/null 2>&1; then
    say "$pkg is installed"
  else
    dnf -y -q install "$pkg"
    say "$pkg installed"
  fi
done

for user in colosseum-gw colosseum-keeper; do
  if id "$user" >/dev/null 2>&1; then
    say "user $user exists"
  else
    useradd --system --no-create-home --home-dir /nonexistent --shell /sbin/nologin "$user"
    say "user $user created"
  fi
done

install -d -m 0700 -o colosseum-gw -g colosseum-gw /var/lib/colosseum-gw
# The keeper's own directory: its user's alone, as before. Where the group already passes through it (mode 0710: set
# below for a mainnet instance's index, and by ops/host/second-prices.sh for the second journal's user), that is kept,
# so that a later run does not shut them out again. The group only passes: it lists nothing here, and every key
# directory under it stays 0700.
keeper_mode=0700
[ "$(stat -c '%a' /var/lib/colosseum-keeper 2>/dev/null || true)" != 710 ] || keeper_mode=0710
install -d -m "$keeper_mode" -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper
install -d -m 0700 -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper/secrets
install -d -m 0700 -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper/shared \
  /var/lib/colosseum-keeper/shared/secrets
install -d -m 0755 -o root -g root /opt/colosseum-pools /opt/colosseum-pools/releases /etc/colosseum
install -d -m 0700 -o root -g root /etc/colosseum/tls

# The host's one network, written once: install.sh reads it at every release (ops/host_instances.py).
if [ -n "$want_network" ] && [ ! -e "$host_env" ]; then
  printf '%s\n' "# The one network this host serves (docs/HOSTING.md). Written once by bootstrap.sh --network." \
    "COLOSSEUM_NETWORK=$network" | install -m 0644 -o root -g root /dev/stdin "$host_env"
  say "wrote $host_env: this host serves $network"
else
  say "this host serves $network"
fi

# The three settings files below are testnet's: the first gateway's, the first keeper's and the shared pools' keeper's.
# A mainnet host gets none of them (down to the `fi` that says so): it has no gateway and no exchange's keeper.
if [ "$network" = testnet ]; then
# The gateway and the keeper deliberately use DIFFERENT nodes. Both rate-limit per IP, and
# this host is one IP: while they shared a node, anyone outside could spend the keeper's budget
# by calling the public gateway, which costs five chain reads per request before it refuses.
# A blinded keeper does not stop a trader who has broken the rules, and the investor pays for
# the delay. The keeper is the one that cannot move: Hyperliquid's node refuses its eth_getLogs
# from this host (see the note below), so the keeper keeps the node that serves logs and the
# gateway takes the other one. The gateway makes no eth_getLogs call at all -- it only reads
# state with eth_call -- so that refusal does not reach it.
#
# The node the gateway moves TO is the tighter of the two: about 100 calls a minute against
# the other's 200. ops/host/nginx-pools-api.conf.in is cut to fit that, and the two must be
# deployed together -- the old limits against this node would have the gateway refusing
# honest traders out of a budget we set ourselves.
if [ ! -f /etc/colosseum/gateway.env ]; then
  install -m 0644 -o root -g root /dev/stdin /etc/colosseum/gateway.env <<'EOF'
GATEWAY_SIGNER=demo
GATEWAY_KEYS_DIR=/var/lib/colosseum-gw/keys
GATEWAY_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm
GATEWAY_BIND=127.0.0.1:8787
GATEWAY_ALLOW_ORIGIN=https://pools.usenami.io
GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4"}
GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5"}
GATEWAY_PERP_DEXES=
EOF
  say "wrote /etc/colosseum/gateway.env"
fi

if [ ! -f /etc/colosseum/keeper.env ]; then
  # Hyperliquid's own node, the gateway's too. Chainlink's, which the keeper used from 24 Sep 2026,
  # went down on 17 Oct ("all upstream attempts failed") and both keepers missed the day's
  # snapshot; drpc's could not read the HyperCore precompiles. Hyperliquid's answers the keeper's
  # reads and its eth_getLogs near the head, but times old ranges out, which is why the unit runs
  # --max-windows 5 and the keeper also sweeps the factory's pool list (docs/HOSTING.md, 16-18 Oct
  # 2026). This file is only written when it is missing: a host set up earlier keeps its URL and
  # is edited by hand.
  install -m 0644 -o root -g root /dev/stdin /etc/colosseum/keeper.env <<'EOF'
COLOSSEUM_KEY_DIR=/var/lib/colosseum-keeper/secrets
COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm
EOF
  say "wrote /etc/colosseum/keeper.env"
fi

if [ ! -f /etc/colosseum/shared-keeper.env ]; then
  # The shared pools' keeper (colosseum-shared-keeper.service): the pools it keeps, joined by commas --
  # the one the site takes deposits into and the earlier one, whose holders its points still pay --
  # a key of its own, and the keepers' node. Written only when missing, like the files above: a host
  # set up earlier keeps the line it has, and a pool added later is added to that line by hand.
  install -m 0644 -o root -g root /dev/stdin /etc/colosseum/shared-keeper.env <<'EOF'
COLOSSEUM_SHARED_DEPLOYMENT=shared-demo2b,shared-demo2c
COLOSSEUM_KEY_DIR=/var/lib/colosseum-keeper/shared/secrets
COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm
EOF
  say "wrote /etc/colosseum/shared-keeper.env"
fi
fi  # testnet's three settings files

tls=/etc/colosseum/tls
if [ ! -f "$tls/origin.key" ]; then
  # Written aside and moved in whole: a run cut short leaves no half-written key behind.
  (umask 077 && openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tls/origin.key.next")
  mv "$tls/origin.key.next" "$tls/origin.key"
  rm -f "$tls/origin.csr"  # a request for an earlier key is of no use
  say "made the TLS key"
fi
# Made from the key that is there, so a run cut short between the two steps is finished here.
if [ ! -f "$tls/origin.csr" ]; then
  openssl req -new -key "$tls/origin.key" -out "$tls/origin.csr" \
    -subj "/CN=pools-api.usenami.io" -addext "subjectAltName=DNS:pools-api.usenami.io"
  chmod 0644 "$tls/origin.csr"
  say "made the CSR for the TLS key"
fi

if [ -f /etc/colosseum/tls/origin.crt ]; then
  say "the Origin CA certificate is in place"
else
  say "waiting for the Origin CA certificate at /etc/colosseum/tls/origin.crt; the CSR:"
  cat /etc/colosseum/tls/origin.csr
fi

[ -n "$instance" ] || exit 0

# A further instance. Its keys apart from the first's, each readable by its own user alone; the
# units keep each instance out of the other's (colosseum-*@.service).
install -d -m 0700 -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper/instances \
  "/var/lib/colosseum-keeper/instances/$instance" "/var/lib/colosseum-keeper/instances/$instance/secrets"
if [ -z "$sim" ]; then
  install -d -m 0700 -o colosseum-gw -g colosseum-gw /var/lib/colosseum-gw/instances \
    "/var/lib/colosseum-gw/instances/$instance" "/var/lib/colosseum-gw/instances/$instance/keys"
  install -d -m 0700 -o colosseum-keeper -g colosseum-keeper "/var/lib/colosseum-keeper/instances/$instance/made-keys"
fi
# made-keys: where the keeper's publisher, when the deployment's record names a test one, makes the agent keys
# it binds; colosseum-key-copy@<name> copies each into the gateway's keys (ops/host/key_copy.py).

if [ -f "/etc/colosseum/deployment-$instance" ]; then
  say "instance $instance names $(tr -d '[:space:]' < "/etc/colosseum/deployment-$instance"); left as it is"
else
  printf '%s\n' "$instance_label" | install -m 0644 -o root -g root /dev/stdin "/etc/colosseum/deployment-$instance"
  say "instance $instance serves deployment $instance_label"
fi

# The first gateway's settings with this instance's keys and port, and the HIP-3 perp the CTO set for
# demo3 (23 Oct 2026): xyz:XYZ100, order number 750000 on testnet, 0.015 an order, 8x. Written only
# when missing, like the first's: an instance set up earlier is edited by hand. Never for a simulated pool's.
if [ -z "$sim" ] && [ ! -f "/etc/colosseum/gateway-$instance.env" ]; then
  install -m 0644 -o root -g root /dev/stdin "/etc/colosseum/gateway-$instance.env" <<EOF
GATEWAY_SIGNER=demo
GATEWAY_KEYS_DIR=/var/lib/colosseum-gw/instances/$instance/keys
GATEWAY_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm
GATEWAY_BIND=127.0.0.1:$instance_port
GATEWAY_ALLOW_ORIGIN=https://pools.usenami.io
GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4", "750000": "0.015"}
GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5", "750000": "8"}
GATEWAY_PERP_DEXES=xyz
EOF
  say "wrote /etc/colosseum/gateway-$instance.env"
fi

# A mainnet host's one instance, a simulated pool's (checked at the top). Its keeper's settings name the network, and in
# comments the lines a person writes by hand before the release with its record: the confirmation, without which its
# services send nothing on mainnet (ops/sim_network.py) and install.sh switches to nothing, and the host's own nodes,
# which no public record holds. Root's alone, mode 0600: it will hold a node's key. Its index's settings beside it.
# And the keeper's directory lets its group pass, for the index (colosseum-sim-index@, which reads the review's folder
# and the time of the journal's day file as that group) and for the second journal's user.
if [ "$network" = mainnet ]; then
  key_dir="/var/lib/colosseum-keeper/instances/$instance/secrets"
  if [ ! -f "/etc/colosseum/keeper-$instance.env" ]; then
    printf '%s\n' "COLOSSEUM_KEY_DIR=$key_dir" \
      "COLOSSEUM_NETWORK=mainnet" \
      "# By hand, each on a line of its own, before the release that carries the record (docs/HOSTING.md, \"Mainnet\"):" \
      "#   COLOSSEUM_CONFIRM_MAINNET=$instance_label     the deployment's label: nothing is sent on mainnet without it" \
      "#   COLOSSEUM_HOST_RPC_URL=<this host's own node>   the keeper's: read here only, it wins over the record's public one" \
      "#   COLOSSEUM_STOP_RPC_URL=<a node for the stops>   the stops' loop's and the key's transactions; the keeper's when not set" \
      "#   COLOSSEUM_ARCHIVE_RPC_URL=<a node that answers for old blocks>   optional: the review's third check" \
      | install -m 0600 -o root -g root /dev/stdin "/etc/colosseum/keeper-$instance.env"
    say "wrote /etc/colosseum/keeper-$instance.env for mainnet (mode 0600)"
  fi
  if [ ! -f "/etc/colosseum/sim-index-$instance.env" ]; then
    printf '%s\n' "COLOSSEUM_NETWORK=mainnet" \
      "SIM_INDEX_RECORD=deployments/mainnet-$instance_label.json" \
      "# By hand, each on a line of its own, before the index is enabled (docs/GATEWAY.md, \"The index of a simulated pool\"):" \
      "#   SIM_INDEX_ALLOW_ORIGIN=<the site's origin>   the page that reads the index" \
      "#   SIM_INDEX_RPC_URL=<a node of the index's own>   optional: the network's public node when not set" \
      | install -m 0600 -o root -g root /dev/stdin "/etc/colosseum/sim-index-$instance.env"
    say "wrote /etc/colosseum/sim-index-$instance.env for mainnet (mode 0600)"
  fi
  chmod 0710 /var/lib/colosseum-keeper
  say "instance $instance: a simulated pool's on mainnet, no gateway. Next, by hand (docs/HOSTING.md, \"Mainnet\"):"
  say "  1. the filler's and the checker's wallets, filler.key/.addr and checker.key/.addr, in $key_dir"
  say "  2. in /etc/colosseum/keeper-$instance.env: COLOSSEUM_CONFIRM_MAINNET=$instance_label and the host's own nodes"
  say "  3. in /etc/colosseum/watch.env: COLOSSEUM_ALERT_CMD, the alarm channel; nothing starts on mainnet without it"
  say "  4. deploy the release with the record: install.sh checks all of it, then enables colosseum-multi-keeper@$instance"
  say "     and colosseum-sim-checker@$instance"
  exit 0
fi

# Its keeper sends with its own key, from its own directory: two keepers on one key would race for
# its nonces. The node is the first keeper's.
if [ ! -f "/etc/colosseum/keeper-$instance.env" ]; then
  install -m 0644 -o root -g root /dev/stdin "/etc/colosseum/keeper-$instance.env" <<EOF
COLOSSEUM_KEY_DIR=/var/lib/colosseum-keeper/instances/$instance/secrets
COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm
COLOSSEUM_PERP_DEXES=xyz
EOF
  say "wrote /etc/colosseum/keeper-$instance.env"
fi
if [ -n "$sim" ]; then
  say "instance $instance: a simulated pool's, no gateway. Put the filler's and the checker's wallets, filler.key/.addr"
  say "  and checker.key/.addr, in /var/lib/colosseum-keeper/instances/$instance/secrets (docs/HOSTING.md, \"A simulated"
  say "  pool's keeper\") and deploy: install.sh checks them and enables colosseum-multi-keeper@$instance and"
  say "  colosseum-sim-checker@$instance"
  exit 0
fi
say "instance $instance: put its agent keys in /var/lib/colosseum-gw/instances/$instance/keys and its keeper key in"
say "  /var/lib/colosseum-keeper/instances/$instance/secrets (docs/HOSTING.md), deploy, then enable"
say "  colosseum-gateway@$instance and colosseum-keeper@$instance"
