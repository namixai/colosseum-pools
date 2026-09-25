#!/usr/bin/env bash
# What is still left to do on the pools host, in one run.
#
#   ops/host/one-visit-2026-09-25.sh <user@host>     from a laptop: copies itself over and runs
#   sudo bash one-visit-2026-09-25.sh                on the host itself
#
# With no argument it prints this and changes nothing, so a half-pasted command cannot act.
#
# It used to carry the A-03 node split and twelve new agent keys as well. Both were done by hand
# on 25 September 2026 and checked there (docs/HOSTING.md records what was seen), so they are
# gone from here rather than left in to be run a second time.
#
# What remains:
#
#   1. Seeds a second keeper's state from the working one, if a second keeper is installed.
#      Seeding, not starting it empty: a keeper catching up spends fifty eth_getLogs a pass and
#      starves the one that is actually enforcing, which was measured on this host.
#   2. Watchdog timers for the keepers -- NOT HERE YET. Operations has not sent that file, and
#      inventing timers for the thing that stops live traders would be worse than an empty slot.
#
# Nothing here deploys code or touches a contract, and the last thing it prints is the single
# line that undoes the run.
set -uo pipefail

GATEWAY_ENV=/etc/colosseum/gateway.env
KEEPER_STATE=/var/lib/colosseum-keeper/keeper-demo.json
KEEPER2_STATE=/var/lib/colosseum-keeper/keeper2-demo.json

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
undo=()
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   [ok] %s\n' "$*"; }
note() { printf '   .   %s\n' "$*"; }
die()  { printf '\n[FAILED] %s\n' "$*" >&2; show_undo; exit 1; }

show_undo() {
  if [ "${#undo[@]}" -eq 0 ]; then
    printf '\nNothing was changed, so there is nothing to undo.\n'
  else
    printf '\nTo undo this whole run, one line:\n\n  sudo %s\n\n' "$(printf '%s && ' "${undo[@]}" | sed 's/ && $//')"
  fi
}

# -- run from a laptop: carry this file over and run it there -----------------------------
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

# -- 1. a second keeper, if there is one --------------------------------------------------
step "1. Seed a second keeper, if one is installed"
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

# -- 2. watchdog timers -------------------------------------------------------------------
step "2. Watchdog timers for the keepers"
note "Not here. Operations has not sent the file, and a timer that decides when a keeper is"
note "considered dead is not a thing to guess at -- it stops live traders. The slot stays open."

step "Done"
printf '   Gateway:  %s\n' "$(curl -s --max-time 10 http://127.0.0.1:8787/v1/health)"
systemctl is-active --quiet colosseum-keeper && printf '   Keeper:   running\n' || printf '   Keeper:   NOT RUNNING -- look at it\n'
show_undo
