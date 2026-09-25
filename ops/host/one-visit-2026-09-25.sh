#!/usr/bin/env bash
# One visit to the pools host: everything the host needs on 25 September 2026, in one run.
#
#   ops/host/one-visit-2026-09-25.sh <user@host>     from a laptop: copies itself over and runs
#   sudo bash one-visit-2026-09-25.sh                on the host itself
#
# With no argument it prints this and changes nothing, so a half-pasted command cannot act.
#
# What it does, in order, checking after every step and refusing to continue on a bad one:
#
#   0. Asks the node the gateway is about to move to whether it answers FROM THIS HOST. Every
#      later step is pointless if it does not, and a laptop cannot answer this question: the
#      refusal we are working around is tied to this machine. A failure here stops the run
#      with nothing touched.
#   1. Points the gateway at that node, so it stops sharing a rate limit with the keeper
#      (audit A-03). Verifies the gateway comes back AND still reads the chain, and puts the
#      old file back by itself if either fails.
#   2. Cuts the nginx rate limits to fit the new node, which allows about half what the old
#      one did. nginx validates the file before it is used.
#   3. Adds 12 agent keys: generates them here, so no private key ever crosses the network,
#      installs them for the gateway and prints only the addresses.
#   4. Seeds a second keeper's state from the working one, if a second keeper is installed.
#
# Nothing here deploys code or touches a contract. Every file it edits is copied first, and
# the last thing it prints is the single line that undoes the whole run.
set -uo pipefail

GATEWAY_ENV=/etc/colosseum/gateway.env
NGINX_SITE=/etc/nginx/conf.d/pools-api.conf
KEYS_DIR=/var/lib/colosseum-gw/keys
KEEPER_STATE=/var/lib/colosseum-keeper/keeper-demo.json
KEEPER2_STATE=/var/lib/colosseum-keeper/keeper2-demo.json
NEW_RPC=https://rpc.hyperliquid-testnet.xyz/evm
REGISTRY=0x6b256B983b849934e0AA500cF2e3Ca176B0d35BA
FREE_COUNT_SELECTOR=0xa6f48c90          # freeCount()
NEW_KEYS=12
KEY_TAG=r2-20260925                     # keeps new files from overwriting demo-agent-NN.key
BASE=/opt/colosseum-pools

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
undo=()
step() { printf '\n══ %s\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
note() { printf '   · %s\n' "$*"; }
die()  { printf '\n✗ %s\n' "$*" >&2; show_undo; exit 1; }

show_undo() {
  if [ "${#undo[@]}" -eq 0 ]; then
    printf '\nNothing was changed, so there is nothing to undo.\n'
  else
    printf '\nTo undo this whole run, one line:\n\n  sudo %s\n\n' "$(printf '%s && ' "${undo[@]}" | sed 's/ && $//')"
  fi
}

backup() {                               # backup <file> -- and remember how to put it back
  [ -f "$1" ] || die "$1 is missing; this is not a pools host, or it was never set up"
  cp -p "$1" "$1.bak-$stamp" || die "could not copy $1"
  undo+=("cp -p '$1.bak-$stamp' '$1'")
  note "copied $1 to $1.bak-$stamp"
}

node_answers() {                         # node_answers <url> -- true when eth_call comes back
  local body out
  body=$(printf '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"%s","data":"%s"},"latest"]}' \
           "$REGISTRY" "$FREE_COUNT_SELECTOR")
  out=$(curl -s --max-time 20 -X POST "$1" -H 'Content-Type: application/json' --data-binary "$body") || return 1
  printf '%s' "$out" | grep -q '"result":"0x' || { printf '%s\n' "$out" >&2; return 1; }
  printf '%s' "$out"
}

gateway_health() { curl -s --max-time 10 http://127.0.0.1:8787/v1/health; }
gateway_keys()   { gateway_health | sed -n 's/.*"keys": *\([0-9]*\).*/\1/p'; }

# ── run from a laptop: carry this file over and run it there ─────────────────────────────
if [ ! -f "$GATEWAY_ENV" ]; then
  target="${1-}"
  case "$target" in
    "" ) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 2 ;;
    *@* ) : ;;
    * ) printf 'Give the host as user@host, the same one you pass to ops/host/deploy.sh.\n' >&2; exit 2 ;;
  esac
  printf 'Copying this script to %s and running it there.\n' "$target"
  scp -q "$0" "$target:/tmp/one-visit-$stamp.sh" || { printf 'scp failed\n' >&2; exit 1; }
  exec ssh -t "$target" "sudo bash /tmp/one-visit-$stamp.sh"
fi

[ "$(id -u)" -eq 0 ] || die "run this with sudo: sudo bash $0"

# ── 0. the gate ──────────────────────────────────────────────────────────────────────────
step "0. Does $NEW_RPC answer from this host?"
if answer=$(node_answers "$NEW_RPC"); then
  ok "it does: $(printf '%s' "$answer" | sed -n 's/.*"result":"\(0x[0-9a-fA-F]*\)".*/\1/p')"
  note "that is freeCount() on the key registry, so the gateway's kind of read works here"
else
  die "it does not. Nothing has been changed. The gateway stays where it is, and audit A-03
  stays open on this host -- which is the honest outcome, not a failure of this run. Send the
  output above to the pools window."
fi

# ── 1. the gateway moves to its own node ─────────────────────────────────────────────────
step "1. Point the gateway at that node (audit A-03)"
if grep -q "^GATEWAY_RPC_URL=$NEW_RPC$" "$GATEWAY_ENV"; then
  ok "already there; leaving it alone"
else
  before_keys="$(gateway_keys)"
  backup "$GATEWAY_ENV"
  sed -i "s|^GATEWAY_RPC_URL=.*|GATEWAY_RPC_URL=$NEW_RPC|" "$GATEWAY_ENV" || die "could not edit $GATEWAY_ENV"
  systemctl restart colosseum-gateway || die "the gateway would not restart"
  sleep 4
  if ! gateway_health | grep -q '"ok": *true'; then
    cp -p "$GATEWAY_ENV.bak-$stamp" "$GATEWAY_ENV"; systemctl restart colosseum-gateway
    die "the gateway did not come back. Its old configuration is already restored and it has
  been restarted -- check 'journalctl -u colosseum-gateway -n 30'."
  fi
  ok "gateway is up on the new node, $(gateway_keys) keys loaded (was ${before_keys:-?})"
fi

# Health says the process is alive; it does not say the chain reads work. Ask for one.
step "1b. Does the gateway still read the chain?"
probe_py=$(cat <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from eth_account import Account
from agents.client import GatewayClient
# A throwaway key asking about an address that is not a pool account. The gateway has to READ
# THE CHAIN to know that, so a "not_an_account" refusal proves the read path works. A broken
# RPC answers "uncertain" or nothing at all.
try:
    r = GatewayClient(Account.create(), "http://127.0.0.1:8787").cancel(
        "0x000000000000000000000000000000000000dEaD", 0, 1)
    print(json.dumps(r))
except Exception as exc:
    print(json.dumps({"error": str(exc)[:200]}))
PY
)
rel="$BASE/current"
if [ -x "$BASE/venv/bin/python" ] && [ -d "$rel" ]; then
  probe="$("$BASE/venv/bin/python" -c "$probe_py" "$rel" 2>&1)"
  case "$probe" in
    *not_an_account*) ok "yes: refused an unknown account, which it can only know from the chain" ;;
    *uncertain*|*upstream_busy*|*error*)
      cp -p "$GATEWAY_ENV.bak-$stamp" "$GATEWAY_ENV" 2>/dev/null && systemctl restart colosseum-gateway
      die "the gateway is up but cannot read the chain: $probe
  The old configuration is back and the gateway restarted." ;;
    *) note "could not tell from the answer, so nothing is claimed either way: $probe" ;;
  esac
else
  note "no release venv at $BASE/venv, so the chain-read probe was skipped -- step 0 still
     showed the node answers from here, but the gateway itself was not asked"
fi

# ── 2. nginx limits for the new node ─────────────────────────────────────────────────────
step "2. Cut the nginx limits to fit the new node"
if [ ! -f "$NGINX_SITE" ]; then
  note "no $NGINX_SITE on this host, so there is nothing to cut; skipping"
elif grep -q 'zone=pools_all:1m rate=15r/m' "$NGINX_SITE"; then
  ok "already cut; leaving it alone"
else
  backup "$NGINX_SITE"
  sed -i 's/zone=pools_all:1m rate=30r\/m/zone=pools_all:1m rate=15r\/m/; s/limit_req zone=pools_all burst=20 nodelay/limit_req zone=pools_all burst=10 nodelay/' "$NGINX_SITE"
  if ! nginx -t -q; then
    cp -p "$NGINX_SITE.bak-$stamp" "$NGINX_SITE"
    die "nginx refused the edited site; the previous one is back and nginx was not reloaded"
  fi
  systemctl reload nginx || die "nginx validated the file but would not reload"
  ok "shared zone now 15 requests a minute, burst 10: 90 chain reads a minute against a node
     that allows about 100. Honest traders will meet 429 earlier than before -- that is the
     trade, and it is written up in docs/HOSTING.md."
fi

# ── 3. more agent keys ───────────────────────────────────────────────────────────────────
step "3. Add $NEW_KEYS agent keys"
keys_before="$(gateway_keys)"
if [ ! -x "$BASE/venv/bin/python" ] || [ ! -f "$rel/ops/make_demo_keys.py" ]; then
  note "no release venv or generator on this host; skipping. Nothing was changed by this step."
elif ! "$BASE/venv/bin/python" -c 'import eth_account' 2>/dev/null; then
  note "the release venv has no eth_account, so keys cannot be made here; skipping"
else
  fresh="/root/newkeys-$stamp"
  if ! addresses="$("$BASE/venv/bin/python" "$rel/ops/make_demo_keys.py" --out "$fresh" --count "$NEW_KEYS")"; then
    note "the generator refused; nothing was installed"
  else
    # The generator names files demo-agent-NN.key and this host already has files by that
    # name. Installing them as they are would OVERWRITE live keys and break whoever holds
    # them, so every new file is renamed on the way in.
    [ -d "$KEYS_DIR" ] || die "$KEYS_DIR is missing; the gateway was never set up here"
    [ -e "$fresh/demo-agent-01.key" ] || die "the generator made no key files in $fresh"
    i=0
    for f in "$fresh"/demo-agent-*.key; do
      i=$((i + 1))
      dest="$KEYS_DIR/demo-agent-$KEY_TAG-$(printf '%02d' "$i").key"
      [ -e "$dest" ] && die "$dest already exists; refusing to overwrite a key"
      install -o colosseum-gw -g colosseum-gw -m 600 "$f" "$dest" || die "could not install $dest"
      undo+=("rm -f '$dest'")
    done
    systemctl restart colosseum-gateway || die "the gateway would not restart after the new keys"
    sleep 4
    keys_after="$(gateway_keys)"
    if [ -z "$keys_after" ] || [ "${keys_after:-0}" -lt "$((${keys_before:-0} + NEW_KEYS))" ]; then
      rm -f "$KEYS_DIR"/demo-agent-$KEY_TAG-*.key; systemctl restart colosseum-gateway
      die "the gateway loaded $keys_after keys, expected $((${keys_before:-0} + NEW_KEYS)).
  The new files have been removed and the gateway restarted."
    fi
    ok "gateway now holds $keys_after keys, was ${keys_before:-?}"
    rm -rf "$fresh"
    printf '\n   The %s addresses, for publishing to the registry. Send these back; they are\n' "$NEW_KEYS"
    printf '   public. The private halves stay on this host and must not be sent anywhere.\n\n'
    printf '%s\n' "$addresses" | sed 's/^/     /'
  fi
fi

# ── 4. a second keeper, if there is one ──────────────────────────────────────────────────
step "4. Seed a second keeper, if one is installed"
if ! systemctl list-unit-files 'colosseum-keeper2.service' 2>/dev/null | grep -q colosseum-keeper2; then
  note "no colosseum-keeper2 on this host; skipping"
elif [ ! -f "$KEEPER_STATE" ]; then
  note "the first keeper has no state file yet, so there is nothing to copy from; skipping"
elif [ -f "$KEEPER2_STATE" ] && systemctl is-active --quiet colosseum-keeper2; then
  ok "the second keeper is running with a state file of its own; leaving it alone"
else
  systemctl stop colosseum-keeper2
  # The whole file, not part of it: the keeper reads factory, next_block and live, and dies on
  # a bare KeyError if any is missing.
  install -o colosseum-keeper -g colosseum-keeper -m 600 "$KEEPER_STATE" "$KEEPER2_STATE" \
    || die "could not seed $KEEPER2_STATE"
  undo+=("rm -f '$KEEPER2_STATE'")
  systemctl start colosseum-keeper2 || die "the second keeper would not start"
  sleep 4
  systemctl is-active --quiet colosseum-keeper2 \
    && ok "second keeper seeded at the first one's block and started" \
    || die "the second keeper is not running; check 'journalctl -u colosseum-keeper2 -n 30'"
fi

# ── what is deliberately not here ────────────────────────────────────────────────────────
step "Not in this run"
note "Watchdog timers for the keepers: operations has not sent the file yet, so there is"
note "nothing to install. When it arrives it goes in as its own step, not by hand."

step "Done"
printf '   Gateway:  %s\n' "$(gateway_health)"
systemctl is-active --quiet colosseum-keeper && printf '   Keeper:   running\n' || printf '   Keeper:   NOT RUNNING -- look at it\n'
show_undo
