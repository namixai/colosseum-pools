#!/usr/bin/env bash
# Runs on the pools host as root: ops/host/deploy.sh unpacks a commit into
# /opt/colosseum-pools/releases/.<sha>.upload and runs this file from there. It finishes that
# release and switches to it:
#
#   1. a venv for the release's requirements, built once per requirements file;
#   2. the systemd units, and the nginx site once the Origin CA certificate is on the host;
#   3. /etc/colosseum/deployment.env from the record named in /etc/colosseum/deployment;
#   4. `current` and `venv` point at the new ones, then the enabled services restart.
#
# It makes and moves no key. Testnet only.
set -euo pipefail

sha="${1:?usage: install.sh <full commit sha>}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "install: not a full commit sha: $sha" >&2; exit 2; }
base=/opt/colosseum-pools
rel="$base/releases/$sha"
upload="$base/releases/.$sha.upload"

say() { printf 'install: %s\n' "$*"; }
fail() { printf 'install: %s\n' "$*" >&2; exit 1; }

# 1. The release and its venv.
if [ -d "$rel" ]; then
  say "release $sha is already here"
else
  [ -d "$upload" ] || fail "nothing uploaded at $upload"
  rm -rf "$rel.partial"
  cp -a "$upload" "$rel.partial"
  mv "$rel.partial" "$rel"
fi
chown -R root:root "$rel"
chmod -R u=rwX,go=rX "$rel"
printf '%s\n' "$sha" > "$rel/REVISION"

req="$rel/spike/requirements.txt"
req_id="$(sha256sum "$req" | cut -c1-12)"
venv="$base/venv-$req_id"
if [ ! -f "$venv/.complete" ]; then
  rm -rf "$venv"
  python3.12 -m venv "$venv"
  "$venv/bin/python" -m pip install --quiet --no-cache-dir --disable-pip-version-check -r "$req"
  touch "$venv/.complete"
  say "built $venv"
fi

# 2. Units, and nginx when the certificate is here.
install -m 0644 -o root -g root "$rel/ops/host/colosseum-gateway.service" /etc/systemd/system/
install -m 0644 -o root -g root "$rel/ops/host/colosseum-keeper.service" /etc/systemd/system/
systemctl daemon-reload

site=/etc/nginx/conf.d/pools-api.conf
if [ -f /etc/colosseum/tls/origin.crt ] && [ -f /etc/colosseum/tls/origin.key ]; then
  ranges="$(curl -fsS --max-time 15 https://www.cloudflare.com/ips-v4; echo; curl -fsS --max-time 15 https://www.cloudflare.com/ips-v6)" \
    || fail "could not read Cloudflare's ranges; nginx left as it was"
  real_ip="$(printf '%s\n' "$ranges" | awk '/^[0-9a-fA-F:.]+\/[0-9]+$/ {print "    set_real_ip_from " $0 ";"}')"
  [ "$(printf '%s\n' "$real_ip" | grep -c set_real_ip_from)" -ge 10 ] || fail "too few Cloudflare ranges; nginx left as it was"
  [ -f "$site" ] && cp -p "$site" "$site.previous"
  REAL_IP="$real_ip" awk '$0 == "@CLOUDFLARE_REAL_IP_FROM@" { print ENVIRON["REAL_IP"]; next } { print }' \
    "$rel/ops/host/nginx-pools-api.conf.in" > "$site"
  if ! nginx -t -q; then
    if [ -f "$site.previous" ]; then mv "$site.previous" "$site"; else rm -f "$site"; fi
    fail "nginx rejected the new site; the previous one is back"
  fi
  systemctl enable --quiet nginx
  systemctl reload-or-restart nginx
  say "nginx serves pools-api.usenami.io"
else
  say "no Origin CA certificate yet: nginx stays off"
fi

# 3. Which deployment the services use: one label, addresses from its committed record.
if [ -f /etc/colosseum/deployment ]; then
  label="$(tr -d '[:space:]' < /etc/colosseum/deployment)"
  [[ "$label" =~ ^[a-z0-9-]+$ ]] || fail "odd label in /etc/colosseum/deployment: $label"
  if ! (cd "$rel" && "$venv/bin/python" - "$label") > /etc/colosseum/deployment.env.next <<'PY'
import sys
from ops import deployments
label = sys.argv[1]
record = deployments.load(label)
print(f"COLOSSEUM_DEPLOYMENT={label}")
print(f"GATEWAY_FACTORY={record['PoolFactory']}")
print(f"GATEWAY_REGISTRY={record['KeyRegistry']}")
PY
  then
    rm -f /etc/colosseum/deployment.env.next
    fail "deployment $label: no complete testnet record in this release"
  fi
  chmod 0644 /etc/colosseum/deployment.env.next
  mv /etc/colosseum/deployment.env.next /etc/colosseum/deployment.env
  say "deployment $label"
else
  say "no deployment named in /etc/colosseum/deployment: the services can't start yet"
fi

# 4. Switch, restart what is enabled, keep the three newest releases.
ln -sfn "venv-$req_id" "$base/venv.next" && mv -T "$base/venv.next" "$base/venv"
ln -sfn "releases/$sha" "$base/current.next" && mv -T "$base/current.next" "$base/current"
say "current -> $sha"
for unit in colosseum-gateway colosseum-keeper; do
  if systemctl is-enabled --quiet "$unit"; then
    systemctl restart "$unit"
    say "$unit restarted"
  else
    say "$unit is not enabled"
  fi
done
find "$base/releases" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9a-f]{40}' \
  -printf '%T@ %f\n' | sort -rn | tail -n +4 | cut -d' ' -f2 | while read -r old; do
  [ "$old" = "$sha" ] || rm -rf "${base:?}/releases/$old"
done
rm -rf "$upload"  # this script runs from there; bash keeps it open to the end
