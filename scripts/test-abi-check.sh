#!/usr/bin/env bash
# Falsifies scripts/abi-check.py: drifts each copy of the structs in turn and requires the check
# to go red and to name the copy it is unhappy with. Then puts the tree back and requires green.
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
cp "$JS" "$SAVE/chain.js" && cp "$PY" "$SAVE/desk.py" || { echo "could not save the originals"; exit 2; }
put_back() { cp "$SAVE/chain.js" "$JS"; cp "$SAVE/desk.py" "$PY"; }
cleanup() { put_back; find "$SAVE" -type f -delete; rmdir "$SAVE" 2>/dev/null; }
trap cleanup EXIT

fail=0

# Prove the check is green before anything is drifted: a red that was already red proves nothing.
if ! out=$(python3 "$CHECK" 2>&1); then
  echo "FAIL: the check is red on an untouched tree, so nothing below means anything"
  printf '%s\n' "$out"
  exit 1
fi

expect_red() { # label  text-the-message-must-contain
  label="$1"; named="$2"
  out=$(python3 "$CHECK" 2>&1); code=$?
  if [ "$code" -ne 1 ]; then
    echo "FAIL: ${label}: exit ${code}, expected 1"
    printf '%s\n' "$out"
    fail=1
  elif ! printf '%s' "$out" | grep -q "$named"; then
    echo "FAIL: ${label}: red, but did not name ${named}"
    printf '%s\n' "$out"
    fail=1
  else
    echo "ok: ${label}"
  fi
  put_back
}

# 1. A type in the app's copy drifts.
python3 - "$JS" <<'EOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
p.write_text(t.replace("uint64 price", "uint128 price", 1))
EOF
expect_red "a type in the app's copy" "app/lib/chain.js TERMS"

# 2. Two fields of the same width swap names. Nothing about the encoding changes; the app just
#    reads one share where it means the other. This is the case no other test would catch.
python3 - "$JS" <<'EOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t = t.replace("uint16 traderShareChallengeBps", "uint16 __SWAP__", 1)
t = t.replace("uint16 traderShareFundedBps", "uint16 traderShareChallengeBps", 1)
p.write_text(t.replace("uint16 __SWAP__", "uint16 traderShareFundedBps", 1))
EOF
expect_red "two same-width fields swapped by name" "app/lib/chain.js TERMS"

# 3. A field goes missing from the agents' copy.
python3 - "$PY" <<'EOF'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
p.write_text(re.sub(r'^TERMS = "\(uint64,', 'TERMS = "(', t, count=1, flags=re.M))
EOF
expect_red "a field missing from the agents' copy" "agents/desk.py TERMS"

# 4. A type in the agents' copy drifts.
python3 - "$PY" <<'EOF'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
p.write_text(re.sub(r'^RULES = "\(uint16,', 'RULES = "(uint32,', t, count=1, flags=re.M))
EOF
expect_red "a type in the agents' copy" "agents/desk.py RULES"

# 5. Restored, the check is green again.
if out=$(python3 "$CHECK" 2>&1); then
  echo "ok: green again once the copies match"
else
  echo "FAIL: still red after putting the originals back"
  printf '%s\n' "$out"
  fail=1
fi

[ "$fail" -eq 0 ] && echo "test-abi-check: every case behaved."
exit "$fail"
