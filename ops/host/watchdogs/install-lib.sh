# Sourced by ops/host/install.sh, never run on its own: puts the host's watchdogs in place from the
# release being installed (docs/HOSTING.md, "The host's watchdogs").
#
#   watchdogs_install <release dir> <full sha> <instances>   before the switch: check, stage, install
#   watchdogs_smoke                                          after the restarts: one run of each
#
# <instances> is what `python -m ops.host_instances list` printed: one "name label port" per line.
# The caller defines say() and fail(); both are install.sh's own.
#
# What it does, in this order. Every step that can fail comes BEFORE anything is switched:
#
#   1. the release has to carry the watchdogs and every unit it is about to install;
#   2. the files are copied to /opt/colosseum-watch/releases/<sha> and the copy's checksums have to equal the
#      release's own directory's, or nothing switches: a copy cut short by a full disk is no watchdog;
#   3. the units are installed, `daemon-reload` runs, `current` points at the new tree and the timers
#      are restarted. The units name /opt/colosseum-watch/current, which is never inside a pools
#      release: a rollback of the pools release leaves the watchdogs where they are;
#   4. what an earlier, hand-made install left in /opt/colosseum-watch is removed.
#
# The first deployment gets its pair of watchdogs when /etc/colosseum/deployment.env is here. An
# instance gets its pair when the release ships units named for it (colosseum-watch-keys-<name>.service
# and the rest) and /etc/colosseum/deployment-<name>.env is here: install.sh writes both files from
# the records before it calls this. A unit that reads a file that is not there would not start, so it
# is not installed.
#
# These are plain variables, not settings: the test stand (ops/tests/test_host_watchdogs.py) sets them
# after sourcing this file, to a directory of its own. The two `*_own` ones are option strings, split
# into words on purpose, and empty on the stand, where nobody is root.
watch_base=/opt/colosseum-watch
watch_state=/var/lib/colosseum-watch
unit_dir=/etc/systemd/system
etc_dir=/etc/colosseum
root_own="-o root -g root"
watch_own="-o colosseum-watch -g colosseum-watch"
watch_keep=5          # releases of the watchdogs kept; the one `current` names is always among them
watch_smoke_wait=120  # seconds the first runs may take, all together, before the install goes on
watch_multis=""       # the instances whose record names a MultiPool: install.sh sets it before watchdogs_install

# What a release's ops/host/watchdogs has to carry to run at all: the wrapper, the two checks, the ladder.
watch_runtime="watchdog-run.sh keys_watch.py keeper_watch.py watch_backoff.py multi_watch.py"
# What an earlier, hand-made install put in /opt/colosseum-watch itself: the files this one replaces.
watch_legacy="watchdog-run.sh watchdogs/hl_take.py watchdogs/pools-keeper-watch.py watchdogs/pools-keys-watch.py watchdogs/pools_watch_backoff.py"

# watchdogs_tree_sum <directory>: a checksum line for every file under it, in a fixed order. Compared instead of
# `diff -r`, which a minimal host may not have; sha256sum is what install.sh already needs.
watchdogs_tree_sum() {
  local sum="sha256sum"
  command -v sha256sum >/dev/null 2>&1 || sum="shasum -a 256"
  # shellcheck disable=SC2086
  (cd "$1" && find . -type f -print0 | sort -z | xargs -0 $sum)
}

# watchdogs_pick <the release's ops/host/watchdogs> <instances>: sets watch_units, the units to install.
watchdogs_pick() {
  local src="$1" instances="$2" name rest
  watch_units=""
  if [ -f "$etc_dir/deployment.env" ]; then
    watch_units="colosseum-watch-keys colosseum-watch-keeper"
  else
    say "no $etc_dir/deployment.env: the first deployment's watchdogs are not installed"
  fi
  while read -r name rest; do
    [ -n "$name" ] || continue
    if [ ! -f "$src/colosseum-watch-keys-$name.service" ]; then
      say "instance $name: the release ships no watchdogs for it"
    elif [ ! -f "$etc_dir/deployment-$name.env" ]; then
      say "instance $name: no $etc_dir/deployment-$name.env, so its watchdogs are not installed"
    else
      watch_units="$watch_units colosseum-watch-keys-$name colosseum-watch-keeper-$name"
    fi
  done <<< "$instances"
  # A MultiPool's keeper is watched by one templated unit, colosseum-watch-multi@<name>, for each instance whose
  # record names a MultiPool (watch_multis) and whose deployment-<name>.env is here.
  for name in $watch_multis; do
    if [ -f "$etc_dir/deployment-$name.env" ]; then
      watch_units="$watch_units colosseum-watch-multi@$name"
    else
      say "instance $name: no $etc_dir/deployment-$name.env, so its MultiPool watchdog is not installed"
    fi
  done
}

# watchdogs_file <unit>: the file a unit is installed from, the template's for an instance of one (a@b -> a@).
watchdogs_file() {
  case "$1" in *@*) printf '%s@' "${1%%@*}" ;; *) printf '%s' "$1" ;; esac
}

watchdogs_install() {
  local rel="$1" sha="$2" instances="$3"
  local src="$rel/ops/host/watchdogs" dest="$watch_base/releases/$sha"
  local file unit timers="" old removed=0

  [ -d "$src" ] || fail "release $sha carries no ops/host/watchdogs; nothing switched"
  for file in $watch_runtime; do
    [ -f "$src/$file" ] || fail "release $sha carries no ops/host/watchdogs/$file; nothing switched"
  done
  watchdogs_pick "$src" "$instances"
  for unit in $watch_units; do
    file="$(watchdogs_file "$unit")"
    [ -f "$src/$file.service" ] && [ -f "$src/$file.timer" ] \
      || fail "release $sha carries no complete unit $file (.service and .timer); nothing switched"
  done

  # The user and the directories. The user exists on a host that was set up before; a new host gets it here.
  if ! id colosseum-watch >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /sbin/nologin colosseum-watch
    say "user colosseum-watch created"
  fi
  # shellcheck disable=SC2086
  install -d -m 0700 $watch_own "$watch_state"
  # shellcheck disable=SC2086
  install -d -m 0755 $root_own "$watch_base" "$watch_base/releases"

  # The release's files, copied whole and compared with the release's own before the copy is used.
  rm -rf "${dest:?}.partial"
  cp -a "$src" "$dest.partial"
  if [ "$(watchdogs_tree_sum "$src")" != "$(watchdogs_tree_sum "$dest.partial")" ]; then
    rm -rf "${dest:?}.partial"
    fail "the copy of the watchdogs in $dest is not the release's own; nothing switched"
  fi
  chmod 0755 "$dest.partial/watchdog-run.sh"
  rm -rf "${dest:?}"
  mv "$dest.partial" "$dest"

  for unit in $watch_units; do
    file="$(watchdogs_file "$unit")"
    # shellcheck disable=SC2086
    install -m 0644 $root_own "$src/$file.service" "$src/$file.timer" "$unit_dir/"
    timers="$timers $unit.timer"
  done
  systemctl daemon-reload

  ln -sfn "releases/$sha" "$watch_base/current.next"
  if ! mv -T "$watch_base/current.next" "$watch_base/current" 2>/dev/null; then
    # Not GNU mv (a test stand): the same end state, without the atomic rename.
    ln -sfn "releases/$sha" "$watch_base/current"
    rm -f "$watch_base/current.next"
  fi
  say "watchdogs: current -> releases/$sha"

  if [ -n "$timers" ]; then
    # shellcheck disable=SC2086
    systemctl enable --quiet $timers
    # shellcheck disable=SC2086
    systemctl restart $timers
    say "watchdogs:$timers enabled and restarted"
  fi
  # A MultiPool watchdog an earlier release enabled for an instance that is no longer selected is stopped: its
  # deployment's file no longer names a pool, and every run would say "could not check".
  while read -r name rest; do
    [ -n "$name" ] || continue
    case " $watch_units " in *" colosseum-watch-multi@$name "*) continue ;; esac
    if systemctl is-enabled --quiet "colosseum-watch-multi@$name.timer" 2>/dev/null; then
      systemctl disable --quiet --now "colosseum-watch-multi@$name.timer"
      say "instance $name: no MultiPool now; its watchdog's timer is stopped"
    fi
  done <<< "$instances"

  for old in $watch_legacy; do
    if [ -e "$watch_base/$old" ]; then
      rm -f "$watch_base/$old"
      removed=$((removed + 1))
    fi
  done
  rmdir "$watch_base/watchdogs" 2>/dev/null || true
  [ "$removed" = 0 ] || say "watchdogs: removed $removed file(s) an earlier hand-made install left in $watch_base"

  ls -1t "$watch_base/releases" | tail -n +$((watch_keep + 1)) | while read -r old; do
    if [ -n "$old" ] && [ "$old" != "$sha" ]; then rm -rf "${watch_base:?}/releases/${old:?}"; fi
  done
  return 0
}

# One run of each watchdog this install put in place, started by systemd, so that what is tried is
# what the timers will run. They run together and are waited for, up to watch_smoke_wait seconds in
# all. A watchdog that could not check (the node, its allowance) is a warning here and never a
# failure of the release: the release is already switched, and a watchdog speaks about the NODE and
# the chain, not about the code.
watchdogs_smoke() {
  local unit state pending deadline result status last
  [ -n "${watch_units:-}" ] || return 0
  for unit in $watch_units; do systemctl start --no-block "$unit.service" || true; done
  deadline=$((SECONDS + watch_smoke_wait))
  while :; do
    pending=0
    for unit in $watch_units; do
      state="$(systemctl is-active "$unit.service" 2>/dev/null || true)"
      case "$state" in activating | active | reloading) pending=1 ;; esac
    done
    [ "$pending" = 0 ] && break
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep 2
  done
  for unit in $watch_units; do
    state="$(systemctl is-active "$unit.service" 2>/dev/null || true)"
    result="$(systemctl show -p Result --value "$unit.service" 2>/dev/null || true)"
    status="$(systemctl show -p ExecMainStatus --value "$unit.service" 2>/dev/null || true)"
    last="$(journalctl -u "$unit.service" -n 2 --no-pager -o cat 2>/dev/null | tail -n 1 | cut -c1-240 || true)"
    case "$state" in
      activating | active | reloading)
        say "WARNING $unit: still running after ${watch_smoke_wait} s; read: journalctl -u $unit" ;;
      *)
        if [ "$result" = success ]; then
          say "$unit: first run ok: $last"
        else
          say "WARNING $unit: first run ended as ${result:-unknown} (exit ${status:-?}): $last; read: journalctl -u $unit"
        fi ;;
    esac
  done
  return 0
}
