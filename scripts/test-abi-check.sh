#!/usr/bin/env bash
# Falsifies scripts/abi-check.py: drifts each copy of the structs in turn and requires the check
# to go red and to name the copy it is unhappy with. Then breaks a copy so it no longer parses,
# and requires exit 2 -- "could not run", never mistaken for "the copies disagree". Then puts the
# tree back and requires green.
#
#   ./scripts/test-abi-check.sh
#
# The originals are saved before anything is touched and restored on any exit, including a kill.
# A check that cannot go red proves nothing, which is the only reason this file exists.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CHECK="$HERE/abi-check.py"
JS="$ROOT/app/lib/chain.js"
PY="$ROOT/agents/desk.py"

SAVE="$(mktemp -d -t abitest.XXXXXX)" || { echo "mktemp failed"; exit 2; }
drop_save() { find "$SAVE" -type f -delete 2>/dev/null; rmdir "$SAVE" 2>/dev/null; }
# Registered before anything can fail, so a failed backup does not leave the directory behind.
trap drop_save EXIT

if ! cp "$JS" "$SAVE/chain.js" || ! cp "$PY" "$SAVE/desk.py"; then
  echo "could not save the originals"
  exit 2
fi
put_back() { cp "$SAVE/chain.js" "$JS"; cp "$SAVE/desk.py" "$PY"; }
# From here on the originals exist, so every exit puts them back first.
trap 'put_back; drop_save' EXIT

fail=0

# Prove the check is green before anything is drifted: a red that was already red proves nothing.
if ! out=$(python3 "$CHECK" 2>&1); then
  echo "FAIL: the check is red on an untouched tree, so nothing below means anything"
  printf '%s\n' "$out"
  exit 1
fi

expect() { # exit-code  label  text-the-message-must-contain
  want="$1"; label="$2"; named="$3"
  out=$(python3 "$CHECK" 2>&1); code=$?
  if [ "$code" -ne "$want" ]; then
    echo "FAIL: ${label}: exit ${code}, expected ${want}"
    printf '%s\n' "$out"
    fail=1
  elif ! printf '%s' "$out" | grep -q "$named"; then
    echo "FAIL: ${label}: exit ${code}, but did not name ${named}"
    printf '%s\n' "$out"
    fail=1
  elif printf '%s' "$out" | grep -q "Traceback"; then
    echo "FAIL: ${label}: a traceback instead of a message"
    printf '%s\n' "$out"
    fail=1
  else
    echo "ok: ${label}"
  fi
  put_back
}

drift() { # file  old  new -- a literal replacement that must hit exactly once
  # A drift that silently fails to apply would leave a green check and a passing case that
  # tested nothing. So a text that is not there exactly once stops the whole run.
  if ! python3 - "$1" "$2" "$3" <<'EOF'
import pathlib, sys
p, old, new = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
t = p.read_text()
if t.count(old) != 1:
    print(f"drift: {old!r} is in {p.name} {t.count(old)} times, not once")
    raise SystemExit(1)
p.write_text(t.replace(old, new, 1))
EOF
  then
    echo "FAIL: the drift above did not apply, so its case would test nothing"
    exit 2
  fi
}

# ── exit 1: the copies disagree ───────────────────────────────────────────────────────────

# 1. A type in the app's copy drifts.
drift "$JS" "uint64 price" "uint128 price"
expect 1 "a type in the app's copy" "app/lib/chain.js TERMS"

# 2. Two fields of the same width swap names. Nothing about the encoding changes; the app just
#    reads one share where it means the other. This is the case no other test would catch.
drift "$JS" "uint16 traderShareChallengeBps" "uint16 __SWAP__"
drift "$JS" "uint16 traderShareFundedBps" "uint16 traderShareChallengeBps"
drift "$JS" "uint16 __SWAP__" "uint16 traderShareFundedBps"
expect 1 "two same-width fields swapped by name" "app/lib/chain.js TERMS"

# 3. A field goes missing from the agents' copy.
drift "$PY" 'TERMS = "(uint64,' 'TERMS = "('
expect 1 "a field missing from the agents' copy" "agents/desk.py TERMS"

# 4. A type in the agents' copy drifts.
drift "$PY" 'RULES = "(uint16,' 'RULES = "(uint32,'
expect 1 "a type in the agents' copy" "agents/desk.py RULES"

# ── exit 2: the check could not run, and says which copy it could not read ────────────────

# 5. A field of the app's copy loses its name: a type alone is not "type name".
drift "$JS" "uint64 price," "uint64,"
expect 2 "a field of the app's copy without a name" "app/lib/chain.js TERMS field 0"

# 6. The app's copy moves or is renamed.
drift "$JS" "const TERMS =" "const TERMS_V2 ="
expect 2 "the app's copy moved" "no TERMS in app/lib/chain.js"

# 7. The agents' copy stops being a tuple.
drift "$PY" 'RULES = "(' 'RULES = "'
expect 2 "the agents' copy is not a tuple" "agents/desk.py RULES is not a tuple"

# 8. forge itself answers with something that is not an ABI. A fake forge first on PATH, for
#    this one run only; it lives in the backup directory, so the exit trap takes it away too.
printf '#!/bin/sh\necho "Error: this is not an ABI"\nexit 0\n' >"$SAVE/forge" && chmod +x "$SAVE/forge"
out=$(PATH="$SAVE:$PATH" python3 "$CHECK" 2>&1); code=$?
rm_fake() { find "$SAVE" -maxdepth 1 -name forge -type f -delete; }
if [ "$code" -eq 2 ] && printf '%s' "$out" | grep -q "did not print an ABI" && ! printf '%s' "$out" | grep -q Traceback; then
  echo "ok: forge printing something that is not an ABI"
else
  echo "FAIL: forge printing something that is not an ABI: exit ${code}"
  printf '%s\n' "$out"
  fail=1
fi
rm_fake

# 9. Restored, the check is green again.
if out=$(python3 "$CHECK" 2>&1); then
  echo "ok: green again once the copies match"
else
  echo "FAIL: still red after putting the originals back"
  printf '%s\n' "$out"
  fail=1
fi

if [ "$fail" -eq 0 ]; then echo "test-abi-check: every case behaved."; fi
exit "$fail"
