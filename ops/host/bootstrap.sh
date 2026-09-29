#!/usr/bin/env bash
# One-time setup of the pools host, run as root on the host (docs/HOSTING.md). Running it
# again creates only what is missing and changes nothing that is there.
#
#   - Python 3.12 and nginx from the distribution, if missing;
#   - users colosseum-gw (the gateway) and colosseum-keeper, each able to read only its own
#     /var/lib directory;
#   - /opt/colosseum-pools for releases, /etc/colosseum for settings;
#   - gateway.env and keeper.env: paths, URLs and the app's origin, no secret;
#   - the TLS key for pools-api.usenami.io and a certificate request (CSR) for Cloudflare's
#     Origin CA. The key never leaves the host; the CSR is printed, it is public.
#
# The agent keys and the keeper key are made afterwards, as their own users (docs/HOSTING.md).
set -euo pipefail

say() { printf 'bootstrap: %s\n' "$*"; }

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
EOF
  say "wrote /etc/colosseum/gateway.env"
fi

if [ ! -f /etc/colosseum/keeper.env ]; then
  # The keeper follows challenges through eth_getLogs, and Hyperliquid's own node refuses that
  # from this host -- "invalid block range" for any range, however near the head, while the same
  # call from elsewhere goes through (measured 24 Sep 2026, docs/HOSTING.md). The other node
  # serves it. This file is only written when it is missing, so a host set up before that
  # measurement keeps the old URL and has to be edited by hand.
  install -m 0644 -o root -g root /dev/stdin /etc/colosseum/keeper.env <<'EOF'
COLOSSEUM_KEY_DIR=/var/lib/colosseum-keeper/secrets
COLOSSEUM_RPC_URL=https://rpcs.chain.link/hyperevm/testnet
EOF
  say "wrote /etc/colosseum/keeper.env"
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
