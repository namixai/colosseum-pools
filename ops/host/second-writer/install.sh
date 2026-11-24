#!/usr/bin/env bash
# Runs on the SECOND machine as root: the machine apart from the pools host that writes the second journal of oracle
# prices, sends it to the pools host and watches that host's pulse (docs/HOSTING.md, "The second writer of prices").
# ops/host/deploy.sh unpacks a commit into /opt/colosseum-pools/releases/.<sha>.upload and runs this file from there:
#
#   COLOSSEUM_SSH_KEY=<key file> ops/host/deploy.sh <commit> <user@second machine> second-writer <name> <label> <network>
#
# or, on the machine itself, from a release already there:
#
#   sudo bash /opt/colosseum-pools/current/ops/host/second-writer/install.sh <full commit sha> <name> <label> <network>
#
# <name> is the instance's name as on the pools host, <label> its deployment's label, <network> testnet or mainnet.
# Run again, it adds only what is missing and switches to the release named:
#
#   1. the release and a venv for its requirements, built once per requirements file, as on the pools host;
#   2. the user colosseum-prices with /var/lib/colosseum-prices, an empty no-keys directory in it (the writer holds no
#      key: it sends nothing) and .ssh for the key the journal is sent with;
#   3. the units: colosseum-price-watch@ (the writer), colosseum-price-sync@ (its journal to the pools host, every
#      minute) and colosseum-host-watch@ (the pools host's pulse, every minute);
#   4. /etc/colosseum/price-watch-<name>.env, written when missing with the deployment and the network, and the lines
#      a person adds by hand named in it; one that is there is held to the same deployment and network;
#   5. `current` and `venv` point at the new ones; then, once the settings are whole, the writer and the host's watch
#      are enabled and restarted, and the sync's timer once its key and the pools host's key are there too.
#
# It makes no key and prints none, and writes no secret: the node's address, the alarm channel's command and the ssh
# key are a person's, and what is still missing is said by name, never by value. No key of the pools is on this
# machine, and nothing here sends a transaction.
set -euo pipefail

# Plain variables, not settings: the test stand (ops/tests/test_mainnet_host.py) sets them after sourcing this file, to
# a directory of its own. The `*_own` ones are option strings, split into words on purpose, and empty on the stand,
# where nobody is root.
base=/opt/colosseum-pools
etc_dir=/etc/colosseum
unit_dir=/etc/systemd/system
prices_home=/var/lib/colosseum-prices
prices_user=colosseum-prices
root_own="-o root -g root"
prices_own="-o colosseum-prices -g colosseum-prices"
python=python3.12
units="colosseum-price-watch@.service colosseum-price-sync@.service colosseum-price-sync@.timer colosseum-host-watch@.service colosseum-host-watch@.timer"

say() { printf 'second-writer: %s\n' "$*"; }
fail() { printf 'second-writer: %s\n' "$*" >&2; exit 1; }

second_sum() {  # the first 12 hex digits of a file's sha256, with whichever tool the machine has
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -c1-12; else shasum -a 256 "$1" | cut -c1-12; fi
}

# second_args <sha> <name> <label> <network>: the same rules as the pools host's (ops/host_instances.py).
second_args() {
  [ $# -eq 4 ] || { echo "second-writer: usage: install.sh <full commit sha> <name> <label> <testnet|mainnet>" >&2; exit 2; }
  sha="$1" name="$2" label="$3" network="$4"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "second-writer: not a full commit sha: $sha" >&2; exit 2; }
  [[ "$name" =~ ^[a-z][a-z0-9]{0,15}$ ]] && [ "$name" != v1 ] \
    || { echo "second-writer: an instance's name is a-z then up to 15 of a-z0-9, and not v1" >&2; exit 2; }
  [[ "$label" =~ ^[a-z0-9-]+$ ]] || { echo "second-writer: odd label $label" >&2; exit 2; }
  [ "$network" = testnet ] || [ "$network" = mainnet ] \
    || { echo "second-writer: the network is testnet or mainnet, not $network" >&2; exit 2; }
}

# 1. The release and its venv. Nothing is switched yet.
second_release() {
  rel="$base/releases/$sha"
  local upload="$base/releases/.$sha.upload"
  command -v "$python" >/dev/null 2>&1 || fail "$python is not on this machine: install it from the distribution first; nothing switched"
  command -v rsync >/dev/null 2>&1 || fail "rsync is not on this machine: the journal is sent with it; install it first; nothing switched"
  if [ -d "$rel" ]; then
    say "release $sha is already here"
  else
    [ -d "$upload" ] || fail "nothing uploaded at $upload; nothing switched"
    rm -rf "$rel.partial"
    cp -a "$upload" "$rel.partial"
    mv "$rel.partial" "$rel"
  fi
  # shellcheck disable=SC2086
  [ -z "$root_own" ] || chown -R root:root "$rel"
  chmod -R u=rwX,go=rX "$rel"
  printf '%s\n' "$sha" > "$rel/REVISION"
  for unit in $units; do
    [ -f "$rel/ops/host/second-writer/$unit" ] || fail "release $sha carries no ops/host/second-writer/$unit; nothing switched"
  done
  [ -f "$rel/deployments/$network-$label.json" ] \
    || fail "release $sha carries no record deployments/$network-$label.json: the writer reads its factory from it; nothing switched"
  local req="$rel/spike/requirements.txt"
  venv="$base/venv-$(second_sum "$req")"
  if [ ! -f "$venv/.complete" ]; then
    rm -rf "$venv"
    "$python" -m venv "$venv"
    "$venv/bin/python" -m pip install --quiet --no-cache-dir --disable-pip-version-check -r "$req"
    touch "$venv/.complete"
    say "built $venv"
  fi
}

# 2. The user and its directories: the writer's state and journal, no key of any kind, and where its ssh key goes.
second_user() {
  if id "$prices_user" >/dev/null 2>&1; then
    say "user $prices_user exists"
  else
    useradd --system --create-home --home-dir "$prices_home" --shell "$(command -v nologin || echo /sbin/nologin)" "$prices_user"
    say "user $prices_user created"
  fi
  # shellcheck disable=SC2086
  install -d -m 0750 $prices_own "$prices_home"
  # shellcheck disable=SC2086
  install -d -m 0700 $prices_own "$prices_home/no-keys" "$prices_home/.ssh"
  # shellcheck disable=SC2086
  install -d -m 0755 $root_own "$etc_dir"
}

# 3. The units, from the release.
second_units() {
  local unit
  for unit in $units; do
    # shellcheck disable=SC2086
    install -m 0644 $root_own "$rel/ops/host/second-writer/$unit" "$unit_dir/"
  done
  systemctl daemon-reload
}

# second_value <NAME>: the value the settings file gives NAME, as systemd reads the line; nothing when it has none.
second_value() {
  sed -n "s/^$1=//p" "$settings" | tail -n 1 | sed -e "s/^'\(.*\)'\$/\1/" -e 's/^"\(.*\)"$/\1/'
}

# 4. The settings: written when missing; one that is there is held to this deployment and network. Root's alone, mode
# 0600: it will hold a node's address and the channel's command.
second_settings() {
  settings="$etc_dir/price-watch-$name.env"
  if [ ! -f "$settings" ]; then
    # Written aside with nobody else able to read it, then put in place whole.
    (umask 077 && printf '%s\n' "COLOSSEUM_DEPLOYMENT=$label" \
      "COLOSSEUM_NETWORK=$network" \
      "# By hand, each on a line of its own (docs/HOSTING.md, \"The second writer of prices\"); nothing starts before:" \
      "#   COLOSSEUM_CONFIRM_MAINNET=$label   on mainnet only: the deployment's label; the writer does not start there without it" \
      "#   COLOSSEUM_HOST_RPC_URL=<this machine's own node>   of ANOTHER provider than the keeper's; its address stays out of the journal" \
      "#   COLOSSEUM_PRICE_SYNC_TO=$prices_user@<the pools host>:   where the journal goes, the colon at its end" \
      "#   COLOSSEUM_HOST_HEALTH_URL=https://<the pools API's domain>/v1/simindex/health   the pools host's pulse" \
      "#   COLOSSEUM_ALERT_CMD=<this machine's alarm channel>   a command that takes an alarm's line on its input" \
      > "$settings.next")
    # shellcheck disable=SC2086
    install -m 0600 $root_own "$settings.next" "$settings"
    rm -f "$settings.next"
    say "wrote $settings (mode 0600)"
  fi
  [ "$(second_value COLOSSEUM_DEPLOYMENT)" = "$label" ] \
    || fail "$settings names another deployment than $label: one file, one deployment; nothing switched"
  local named
  named="$(second_value COLOSSEUM_NETWORK)"
  [ "${named:-testnet}" = "$network" ] \
    || fail "$settings names another network than $network: one instance, one network; nothing switched"
}

# What the settings still lack, by name, never by value: the names on one line, or nothing when they are whole.
second_missing() {
  local missing="" key
  [ -n "$(second_value COLOSSEUM_HOST_RPC_URL)$(second_value COLOSSEUM_RPC_URL)" ] || missing="$missing COLOSSEUM_HOST_RPC_URL"
  for key in COLOSSEUM_PRICE_SYNC_TO COLOSSEUM_HOST_HEALTH_URL COLOSSEUM_ALERT_CMD; do
    [ -n "$(second_value "$key" | tr -d '[:space:]')" ] || missing="$missing $key"
  done
  if [ "$network" = mainnet ] && [ "$(second_value COLOSSEUM_CONFIRM_MAINNET)" != "$label" ]; then
    missing="$missing COLOSSEUM_CONFIRM_MAINNET"
  fi
  case "$(second_value COLOSSEUM_PRICE_SYNC_TO)" in "" | *:) ;; *) missing="$missing COLOSSEUM_PRICE_SYNC_TO(the-colon-at-its-end)" ;; esac
  printf '%s' "${missing# }"
}

# 5. The switch, and what can run is enabled and restarted; what cannot is said, with what a person does about it.
second_switch() {
  ln -sfn "venv-${venv##*/venv-}" "$base/venv.next"
  mv -T "$base/venv.next" "$base/venv" 2>/dev/null || { ln -sfn "venv-${venv##*/venv-}" "$base/venv"; rm -f "$base/venv.next"; }
  ln -sfn "releases/$sha" "$base/current.next"
  mv -T "$base/current.next" "$base/current" 2>/dev/null || { ln -sfn "releases/$sha" "$base/current"; rm -f "$base/current.next"; }
  say "current -> $sha"

  local missing
  missing="$(second_missing)"
  if [ -n "$missing" ]; then
    say "NOT STARTED: $settings still lacks: $missing"
    say "  write them by hand (sudoedit $settings), try the channel once:"
    say "    python -m ops.review_service --say-test   (docs/HOSTING.md, \"The second writer of prices\")"
    say "  and run this again: the writer and the watch start then"
    return 0
  fi
  systemctl enable --quiet "colosseum-price-watch@$name.service" "colosseum-host-watch@$name.timer"
  systemctl restart "colosseum-price-watch@$name.service" "colosseum-host-watch@$name.timer"
  say "the writer and the watch of the pools host are enabled and restarted (colosseum-price-watch@$name, colosseum-host-watch@$name)"
  if [ -f "$prices_home/.ssh/sync" ] && [ -s "$prices_home/.ssh/known_hosts" ]; then
    systemctl enable --quiet "colosseum-price-sync@$name.timer"
    systemctl restart "colosseum-price-sync@$name.timer"
    say "the journal goes to the pools host every minute (colosseum-price-sync@$name)"
  else
    say "NOT SENT YET: the journal's sync waits for its key and the pools host's. By hand, as $prices_user:"
    say "  1. the key, made here and never copied anywhere:"
    say "       sudo -u $prices_user ssh-keygen -t ed25519 -N '' -f $prices_home/.ssh/sync"
    say "  2. its PUBLIC half, $prices_home/.ssh/sync.pub, to the pools host, and there:"
    say "       sudo bash /opt/colosseum-pools/current/ops/host/second-prices.sh <that .pub file>"
    say "  3. the pools host's key into $prices_home/.ssh/known_hosts, its fingerprint held against the one the host"
    say "     itself shows (second-prices.sh prints it):"
    say "       sudo -u $prices_user sh -c 'ssh-keyscan -t ed25519 <the pools host> >> $prices_home/.ssh/known_hosts'"
    say "       sudo -u $prices_user ssh-keygen -lf $prices_home/.ssh/known_hosts"
    say "  then run this again: the sync's timer starts then"
  fi
}

second_install() {
  second_args "$@"
  second_release
  second_user
  second_units
  second_settings
  second_switch
  # The three newest releases are kept, the one that runs among them.
  { ls -1t "$base/releases" | grep -E '^[0-9a-f]{40}$' | tail -n +4 || true; } | while read -r old; do
    [ "$old" = "$sha" ] || rm -rf "${base:?}/releases/${old:?}"
  done
  rm -rf "$base/releases/.$sha.upload"  # this script may run from there; bash keeps it open to the end
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  second_install "$@"
fi
