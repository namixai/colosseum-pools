#!/usr/bin/env bash
# Runs on the pools host as root: ops/host/deploy.sh unpacks a commit into
# /opt/colosseum-pools/releases/.<sha>.upload and runs this file from there. It finishes that
# release and switches to it:
#
#   1. a venv for the release's requirements, built once per requirements file;
#   2. the systemd units, and the nginx site once the Origin CA certificate is on the host;
#   3. /etc/colosseum/deployment.env from the record named in /etc/colosseum/deployment, and
#      deployment-<name>.env for each further instance named by /etc/colosseum/deployment-<name>
#      (ops/host_instances.py);
#   4. `current` and `venv` point at the new ones, then the enabled services restart, the
#      instances' too, and the shared pool's keeper when it is enabled;
#   5. the watchdogs come with the release: their files, units and timers are put in place from it
#      before the switch (ops/host/watchdogs/install-lib.sh), and each is run once after the restarts;
#   6. an instance whose record names a test publisher has its keeper's made keys copied to its gateway:
#      colosseum-key-copy@<name>.path and .timer are enabled and the copier run once (ops/host/key_copy.py);
#   7. an instance whose record names a MultiPool has its keeper enabled, colosseum-multi-keeper@<name>
#      (ops/multi_keeper.py), and the watchdog on it.
#
# It makes no key and reads none; the copier it starts moves an instance's agent keys from its keeper to
# its gateway, and nothing else. Testnet only.
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
install -m 0644 -o root -g root "$rel/ops/host/colosseum-gateway@.service" /etc/systemd/system/
install -m 0644 -o root -g root "$rel/ops/host/colosseum-keeper@.service" /etc/systemd/system/
install -m 0644 -o root -g root "$rel/ops/host/colosseum-shared-keeper.service" /etc/systemd/system/
install -m 0644 -o root -g root "$rel/ops/host/colosseum-key-copy@.service" /etc/systemd/system/
install -m 0644 -o root -g root "$rel/ops/host/colosseum-key-copy@.path" /etc/systemd/system/
install -m 0644 -o root -g root "$rel/ops/host/colosseum-key-copy@.timer" /etc/systemd/system/
install -m 0644 -o root -g root "$rel/ops/host/colosseum-multi-keeper@.service" /etc/systemd/system/
systemctl daemon-reload

# The further instances, read once: a file that names one badly stops the release here, before nginx
# or any unit is touched.
instances="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances list /etc/colosseum)" \
  || fail "the instances in /etc/colosseum are not as ops/host_instances.py reads them; nothing switched"
locations="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances nginx /etc/colosseum)" \
  || fail "could not write the instances' nginx locations; nothing switched"
upstreams="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances nginx-upstreams /etc/colosseum)" \
  || fail "could not write the instances' nginx upstreams; nothing switched"

# Each deployment named, the first's and every instance's, has its complete record in this release:
# checked here, so that nginx never routes to an instance whose gateway this release can't start.
first_label="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances first-label /etc/colosseum)" \
  || fail "could not read /etc/colosseum/deployment; nothing switched"
if [ -f /etc/colosseum/deployment ]; then
  [[ "$first_label" =~ ^[a-z0-9-]+$ ]] || fail "odd label in /etc/colosseum/deployment: $first_label"
fi
for label in $first_label $(awk '{print $2}' <<< "$instances"); do
  (cd "$rel" && "$venv/bin/python" -m ops.host_instances deployment-env "$label") > /dev/null \
    || fail "deployment $label: no complete testnet record in this release; nothing switched"
done

# A deployment whose record names a test publisher, a key in a file before the enclave (ops/key_relay.py), is
# served as an instance: its keeper signs bindings with the publisher's wallet, kept beside its own, and makes
# the agent keys into its made-keys directory, from where colosseum-key-copy@<name> hands them to the gateway.
# All of it is checked here, or that keeper would start, die on a missing wallet and restart for ever.
lower() { tr '[:upper:]' '[:lower:]'; }
# owned_alone <path> <user> <mode>: the path is that user's with that mode exactly, as the keeper's loader and the
# copier require (owner-only); checked here so that neither refuses it after the switch.
owned_alone() { [ "$(stat -c '%U %a' "$1" 2>/dev/null)" = "$2 $3" ]; }
publishers=""
if [ -n "$first_label" ]; then
  first_publisher="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances publisher "$first_label")" \
    || fail "deployment $first_label: could not read its publisher; nothing switched"
  [ -z "$first_publisher" ] \
    || fail "deployment $first_label names a test publisher: serve it as an instance (bootstrap.sh --instance); nothing switched"
fi
while read -r name label _; do
  [ -n "$name" ] || continue
  publisher="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances publisher "$label")" \
    || fail "deployment $label: could not read its publisher; nothing switched"
  [ -n "$publisher" ] || continue
  own="/var/lib/colosseum-keeper/instances/$name"
  gw_keys="/var/lib/colosseum-gw/instances/$name/keys"
  [ -f "$own/secrets/publisher.key" ] && [ -f "$own/secrets/publisher.addr" ] && [ -d "$own/made-keys" ] \
    || fail "instance $name: its record names the test publisher $publisher, so its keeper needs $own/secrets/publisher.key and .addr, and $own/made-keys (docs/HOSTING.md, \"A test publisher\"); nothing switched"
  owned_alone "$own/secrets/publisher.key" colosseum-keeper 600 && owned_alone "$own/made-keys" colosseum-keeper 700 \
    && owned_alone "$gw_keys" colosseum-gw 700 \
    || fail "instance $name: $own/secrets/publisher.key must be colosseum-keeper's, mode 600, $own/made-keys colosseum-keeper's and $gw_keys colosseum-gw's, mode 700: the keeper and the copier refuse anything open to others; nothing switched"
  had="$(tr -d '[:space:]' 2>/dev/null < "$own/secrets/publisher.addr" || true)"
  [ "$(lower <<< "$had")" = "$(lower <<< "$publisher")" ] \
    || fail "instance $name: $own/secrets/publisher.addr says ${had:-nothing}, its record's publisher is $publisher; nothing switched"
  publishers+="$name "
done <<< "$instances"

# A deployment whose record names a MultiPool has its calls made by colosseum-multi-keeper@<name>
# (ops/multi_keeper.py), with a wallet of its own beside the instance keeper's: one key in two processes would
# race for its nonces. It is served as an instance, and its wallet is checked here, or that keeper would start,
# die on the missing key and restart for ever.
multis=""
if [ -n "$first_label" ]; then
  first_multi="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances multi "$first_label")" \
    || fail "deployment $first_label: could not read its MultiPool; nothing switched"
  [ -z "$first_multi" ] \
    || fail "deployment $first_label names a MultiPool: serve it as an instance (bootstrap.sh --instance); nothing switched"
fi
while read -r name label _; do
  [ -n "$name" ] || continue
  multi="$(cd "$rel" && "$venv/bin/python" -m ops.host_instances multi "$label")" \
    || fail "deployment $label: could not read its MultiPool; nothing switched"
  [ -n "$multi" ] || continue
  own="/var/lib/colosseum-keeper/instances/$name"
  [ -f "$own/secrets/multi-keeper.key" ] && [ -f "$own/secrets/multi-keeper.addr" ] \
    && owned_alone "$own/secrets/multi-keeper.key" colosseum-keeper 600 \
    || fail "instance $name: its record names the MultiPool $multi, so its keeper needs its own wallet, $own/secrets/multi-keeper.key (colosseum-keeper's, mode 600) and .addr (docs/HOSTING.md, \"A MultiPool's keeper\"); nothing switched"
  multis+="$name "
done <<< "$instances"

# The shared pool's keeper, when its settings are on the host: a complete record with a SharedPool in this
# release, or nothing switches -- the unit would start, die on the record and restart for ever. The label is
# read the way it has to be written, one line of its own, KEY=label and nothing else, so the label checked
# here is the one systemd passes, character for character. Several pools are several labels joined by
# single commas, each checked, none twice: one process keeps them all with one wallet. Anything else that names the key -- spaces
# around "=", quotes, a comment after the value, a space inside the label, an empty value, the line
# commented out, written twice, or not there at all -- stops the release with the lines it read, rather
# than checking a label systemd would not pass.
shared_env=/etc/colosseum/shared-keeper.env
if [ -f "$shared_env" ]; then
  shared_lines="$(grep -n 'COLOSSEUM_SHARED_DEPLOYMENT' "$shared_env" || true)"
  shared_exact="$(grep -E '^COLOSSEUM_SHARED_DEPLOYMENT=[a-z0-9-]+(,[a-z0-9-]+)*$' "$shared_env" || true)"
  if [ "$(grep -c 'COLOSSEUM_SHARED_DEPLOYMENT' "$shared_env" || true)" != 1 ] || [ -z "$shared_exact" ]; then
    fail "$shared_env must name the pool on one line of its own, COLOSSEUM_SHARED_DEPLOYMENT=<label>, or several labels joined by commas: lower-case letters, digits and dashes, no space, quote or comment; it has: ${shared_lines:-no such line}; nothing switched"
  fi
  shared_label="${shared_exact#COLOSSEUM_SHARED_DEPLOYMENT=}"
  (cd "$rel" && "$venv/bin/python" -c 'import sys; from ops import deployments; n = sys.argv[1].split(","); sys.exit(0 if len(set(n)) == len(n) and all("SharedPool" in deployments.load(x) for x in n) else 1)' "$shared_label") \
    || fail "shared pool $shared_label: no complete testnet record with a SharedPool in this release; nothing switched"
fi

site=/etc/nginx/conf.d/pools-api.conf
if [ -f /etc/colosseum/tls/origin.crt ] && [ -f /etc/colosseum/tls/origin.key ]; then
  ranges="$(curl -fsS --max-time 15 https://www.cloudflare.com/ips-v4; echo; curl -fsS --max-time 15 https://www.cloudflare.com/ips-v6)" \
    || fail "could not read Cloudflare's ranges; nginx left as it was"
  real_ip="$(printf '%s\n' "$ranges" | awk '/^[0-9a-fA-F:.]+\/[0-9]+$/ {print "    set_real_ip_from " $0 ";"}')"
  [ "$(printf '%s\n' "$real_ip" | grep -c set_real_ip_from)" -ge 10 ] || fail "too few Cloudflare ranges; nginx left as it was"
  [ -f "$site" ] && cp -p "$site" "$site.previous"
  REAL_IP="$real_ip" LOCATIONS="$locations" UPSTREAMS="$upstreams" awk '
    $0 == "@CLOUDFLARE_REAL_IP_FROM@" { print ENVIRON["REAL_IP"]; next }
    $0 == "@INSTANCE_UPSTREAMS@" { print ENVIRON["UPSTREAMS"]; next }
    $0 == "@INSTANCE_LOCATIONS@" { print ENVIRON["LOCATIONS"]; next }
    { print }' "$rel/ops/host/nginx-pools-api.conf.in" > "$site"
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

# 3. Which deployment each set of services uses: one label, addresses from its committed record.
deployment_env() {  # <label> <env file>
  if ! (cd "$rel" && "$venv/bin/python" -m ops.host_instances deployment-env "$1") > "$2.next"; then
    rm -f "$2.next"
    fail "deployment $1: no complete testnet record in this release"
  fi
  chmod 0644 "$2.next"
  mv "$2.next" "$2"
}
if [ -n "$first_label" ]; then
  deployment_env "$first_label" /etc/colosseum/deployment.env
  say "deployment $first_label"
else
  say "no deployment named in /etc/colosseum/deployment: the services can't start yet"
fi
while read -r name label port; do
  [ -n "$name" ] || continue
  deployment_env "$label" "/etc/colosseum/deployment-$name.env"
  say "instance $name: deployment $label, gateway on 127.0.0.1:$port, /$name/v1/ on nginx"
done <<< "$instances"

# The watchdogs ride with the release. Their files are checked and put in place here, still before
# the switch, from the deployment env files just written (docs/HOSTING.md, "The host's watchdogs").
. "$rel/ops/host/watchdogs/install-lib.sh"
watch_multis="$multis"
watchdogs_install "$rel" "$sha" "$instances"

# 4. Switch, restart what is enabled, keep the three newest releases.
ln -sfn "venv-$req_id" "$base/venv.next" && mv -T "$base/venv.next" "$base/venv"
ln -sfn "releases/$sha" "$base/current.next" && mv -T "$base/current.next" "$base/current"
say "current -> $sha"
# The test publishers' keys, watched before the keepers restart so that none they make waits; one copy now
# for any made while nothing watched.
for name in $publishers; do
  systemctl enable --quiet --now "colosseum-key-copy@$name.path" "colosseum-key-copy@$name.timer"
  systemctl start "colosseum-key-copy@$name.service" \
    || say "WARNING instance $name: the first copy failed; the timer tries again each minute; read: journalctl -u colosseum-key-copy@$name"
  say "instance $name: its test publisher's keys go to its gateway (colosseum-key-copy@$name)"
done
# A MultiPool's keeper is enabled by its record (checked above), and restarts below with the rest.
for name in $multis; do
  systemctl enable --quiet "colosseum-multi-keeper@$name"
  say "instance $name: its MultiPool's keeper is enabled (colosseum-multi-keeper@$name)"
done
units=(colosseum-gateway colosseum-keeper)
while read -r name _; do
  [ -n "$name" ] && units+=("colosseum-gateway@$name" "colosseum-keeper@$name")
done <<< "$instances"
for name in $multis; do units+=("colosseum-multi-keeper@$name"); done
# An instance whose record no longer names a test publisher, or a MultiPool, has what an earlier release enabled
# for it stopped: a keeper left running would go on sending to the pool it loaded, a copier would watch for nothing.
while read -r name _; do
  [ -n "$name" ] || continue
  case " $publishers " in *" $name "*) ;; *)
    if systemctl is-enabled --quiet "colosseum-key-copy@$name.path" 2>/dev/null \
        || systemctl is-enabled --quiet "colosseum-key-copy@$name.timer" 2>/dev/null; then
      systemctl disable --quiet --now "colosseum-key-copy@$name.path" "colosseum-key-copy@$name.timer"
      say "instance $name: no test publisher in its record now; its key copier is stopped"
    fi ;;
  esac
  case " $multis " in *" $name "*) ;; *)
    if systemctl is-enabled --quiet "colosseum-multi-keeper@$name" 2>/dev/null; then
      systemctl disable --quiet --now "colosseum-multi-keeper@$name"
      say "instance $name: no MultiPool in its record now; its keeper is stopped"
    fi ;;
  esac
done <<< "$instances"
units+=(colosseum-shared-keeper)
for unit in "${units[@]}"; do
  if systemctl is-enabled --quiet "$unit"; then
    systemctl restart "$unit"
    say "$unit restarted"
  else
    say "$unit is not enabled"
  fi
done
# One run of each watchdog, started now so that a node that cannot be read, or a deployment that is
# not named, shows in this output and not a quarter of an hour later. Never fatal: see install-lib.sh.
watchdogs_smoke || true
find "$base/releases" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9a-f]{40}' \
  -printf '%T@ %f\n' | sort -rn | tail -n +4 | cut -d' ' -f2 | while read -r old; do
  [ "$old" = "$sha" ] || rm -rf "${base:?}/releases/$old"
done
rm -rf "$upload"  # this script runs from there; bash keeps it open to the end
