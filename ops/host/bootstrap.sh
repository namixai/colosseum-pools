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
set -euo pipefail

say() { printf 'bootstrap: %s\n' "$*"; }

instance=""
if [ "${1:-}" = "--instance" ]; then
  instance="${2:?usage: bootstrap.sh --instance <name> <label> <port>}"
  instance_label="${3:?usage: bootstrap.sh --instance <name> <label> <port>}"
  instance_port="${4:?usage: bootstrap.sh --instance <name> <label> <port>}"
  # The same rules as ops/host_instances.py: a path segment and a unit's name, its own port.
  [[ "$instance" =~ ^[a-z][a-z0-9]{0,15}$ ]] && [ "$instance" != v1 ] \
    || { echo "bootstrap: an instance's name is a-z then up to 15 of a-z0-9, and not v1" >&2; exit 2; }
  [[ "$instance_label" =~ ^[a-z0-9-]+$ ]] || { echo "bootstrap: odd label $instance_label" >&2; exit 2; }
  [[ "$instance_port" =~ ^[0-9]+$ ]] && [ "$instance_port" -ge 8788 ] && [ "$instance_port" -le 8799 ] \
    || { echo "bootstrap: an instance's port is 8788..8799 (8787 is the first gateway's)" >&2; exit 2; }
elif [ $# -gt 0 ]; then
  echo "bootstrap: usage: bootstrap.sh [--instance <name> <label> <port>]" >&2
  exit 2
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
install -d -m 0700 -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper
install -d -m 0700 -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper/secrets
install -d -m 0700 -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper/shared \
  /var/lib/colosseum-keeper/shared/secrets
install -d -m 0755 -o root -g root /opt/colosseum-pools /opt/colosseum-pools/releases /etc/colosseum
install -d -m 0700 -o root -g root /etc/colosseum/tls

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
install -d -m 0700 -o colosseum-gw -g colosseum-gw /var/lib/colosseum-gw/instances \
  "/var/lib/colosseum-gw/instances/$instance" "/var/lib/colosseum-gw/instances/$instance/keys"
install -d -m 0700 -o colosseum-keeper -g colosseum-keeper /var/lib/colosseum-keeper/instances \
  "/var/lib/colosseum-keeper/instances/$instance" "/var/lib/colosseum-keeper/instances/$instance/secrets" \
  "/var/lib/colosseum-keeper/instances/$instance/made-keys"
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
# when missing, like the first's: an instance set up earlier is edited by hand.
if [ ! -f "/etc/colosseum/gateway-$instance.env" ]; then
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
say "instance $instance: put its agent keys in /var/lib/colosseum-gw/instances/$instance/keys and its keeper key in"
say "  /var/lib/colosseum-keeper/instances/$instance/secrets (docs/HOSTING.md), deploy, then enable"
say "  colosseum-gateway@$instance and colosseum-keeper@$instance"
