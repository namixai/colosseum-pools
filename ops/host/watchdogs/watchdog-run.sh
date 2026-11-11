#!/usr/bin/env bash
# The wrapper of a host watchdog: runs one check and sends its outcome to the right place.
#
#   watchdog-run.sh <name> <command...>
#
# Telling the outcomes apart is all it does, and it is deliberately dumb:
#
#   0  the check ran, quiet     -> a line in the journal; the alarm flag is CLEARED (if there was one)
#   1  the check ran, a finding -> ALARM in the journal + a flag file + the alert command, if there is one
#                                   (a flag that cannot be written is exit 4: the unit fails)
#   3  COULD NOT CHECK          -> a warning in the journal, the flag is NOT TOUCHED at all
#   other  unknown code         -> loud, and a non-zero exit: nothing can be judged by such a code
#
# Why code 3 leaves the flag alone: "could not check" is neither "all is well" nor "all is wrong".
# Clearing the flag out of ignorance would put out a live alarm; raising it would start a false one.
#
# There is no alert channel on the host (nothing in docs/HOSTING.md, bootstrap.sh, install.sh or
# deploy.sh sends a message anywhere). So the channel here is ONE variable, `COLOSSEUM_ALERT_CMD`:
# when a channel exists, its command goes there and nothing else is rewritten. Until then the
# wrapper says so in the journal rather than keep quiet: "no channel configured" and "there was no
# alarm" must read differently.
set -uo pipefail

NAME="${1:-}"; shift || true
[ -n "$NAME" ] && [ "$#" -gt 0 ] || { echo "watchdog-run: usage: watchdog-run.sh <name> <command...>" >&2; exit 64; }

FLAG_DIR="${COLOSSEUM_WATCH_DIR:-/var/lib/colosseum-watch}"
FLAG="$FLAG_DIR/$NAME.alarm"
log() { printf '%s %s\n' "$NAME:" "$*"; }        # journald takes the unit's stdout

mkdir -p "$FLAG_DIR" 2>/dev/null || true
OUT="$("$@" 2>&1)"; RC=$?

case "$RC" in
  0)
    if [ -f "$FLAG" ]; then
      log "RECOVERED: the check is clean again, removing the flag $FLAG"
      rm -f "$FLAG"
    fi
    log "quiet: $(printf '%s' "$OUT" | tail -1)"
    ;;
  1)
    # The flag is written whole and moved into place: a reader never sees half a file. A write that fails (a full
    # disk, a directory nobody can write to) leaves the flag that stood as it was, is said, and turns the unit
    # red (exit 4) after the finding has been put in the journal and sent to the channel: an alarm that exists
    # in the journal only is still an alarm, and must not look like a success.
    FLAG_OK=1
    TMP="$(mktemp "$FLAG.XXXXXX" 2>/dev/null)" || FLAG_OK=0
    if [ "$FLAG_OK" = 1 ]; then
      { printf 'at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; printf '%s\n' "$OUT"; } > "$TMP" \
        && mv -f "$TMP" "$FLAG" || { FLAG_OK=0; rm -f "$TMP"; }
    fi
    log "ALARM: finding, flag $FLAG"
    printf '%s\n' "$OUT" | while IFS= read -r line; do log "  $line"; done
    [ "$FLAG_OK" = 1 ] || log "ERROR: the flag $FLAG could not be written; the alarm stands in this journal and the alert channel only"
    if [ -n "${COLOSSEUM_ALERT_CMD:-}" ]; then
      # "Sent" is not claimed from the channel's exit code alone: say what came of it.
      if printf '%s\n' "$OUT" | sh -c "$COLOSSEUM_ALERT_CMD" >/dev/null 2>&1; then
        log "the channel took the alarm (COLOSSEUM_ALERT_CMD)"
      else
        log "WARNING: the channel did not take the alarm (COLOSSEUM_ALERT_CMD failed); the flag and the journal are in place"
      fi
    else
      log "no alert channel on the host (COLOSSEUM_ALERT_CMD is not set): the alarm is in the flag and the journal only"
    fi
    [ "$FLAG_OK" = 1 ] || exit 4
    ;;
  3)
    log "COULD NOT CHECK (code 3): $(printf '%s' "$OUT" | tail -1)"
    log "this is a failure of the tool, not a statement about the chain; the flag is untouched"
    exit 3
    ;;
  *)
    log "UNKNOWN CODE $RC: nothing can be judged by it: $(printf '%s' "$OUT" | tail -1)"
    exit "$RC"
    ;;
esac
exit 0
