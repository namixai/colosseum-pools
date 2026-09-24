#!/usr/bin/env bash
# Falsifies `scripts/mutations.py --check`: it must pass on this repository as it stands, and
# go red when a mutation stops matching the code it guards, when one matches twice, and when an
# id is used twice. The copies run from scripts/ so they resolve the repository the same way.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STAND="$HERE/mutations.py"
COPY="$HERE/.mutations-check-test.py"
trap 'rm -f "$COPY"' EXIT
fail=0

rc=0; out="$(python3 "$STAND" --check)" || rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "each matching once"; then
  echo "ok   the stand as it stands passes"
else
  echo "FAIL the stand as it stands: rc=$rc"; echo "$out"; fail=1
fi

# A mutation whose text is nowhere in the file it names.
python3 - "$STAND" "$COPY" <<'PY'
import pathlib, sys
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
text = src.read_text()
old = 'MUTATIONS = [\n'
assert text.count(old) == 1
dst.write_text(text.replace(old, old + '    ("Z1", "README.md", "a line no README holds", "x", ["nothing"]),\n', 1))
PY
rc=0; out="$(python3 "$COPY" --check)" || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "Z1: 0 matches in README.md"; then
  echo "ok   a mutation matching nothing is caught"
else
  echo "FAIL a mutation matching nothing: rc=$rc"; echo "$out"; fail=1
fi

# A mutation whose text matches more than once: it would apply to whichever came first.
python3 - "$STAND" "$COPY" <<'PY'
import pathlib, sys
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
text = src.read_text()
old = 'MUTATIONS = [\n'
dst.write_text(text.replace(old, old + '    ("Z2", "README.md", "the", "x", ["nothing"]),\n', 1))
PY
rc=0; out="$(python3 "$COPY" --check)" || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -qE "Z2: ([2-9]|[0-9]{2,}) matches in README.md"; then
  echo "ok   a mutation matching twice is caught"
else
  echo "FAIL a mutation matching twice: rc=$rc"; echo "$out"; fail=1
fi

# The same id twice: the second one is invisible in a run that names ids.
python3 - "$STAND" "$COPY" <<'PY'
import pathlib, sys, re
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
text = src.read_text()
first = re.search(r'\n    \("([A-Z]\d+)", "([^"]+)",\n     (.+?)\n', text, re.S)
mid = first.group(1)
old = 'MUTATIONS = [\n'
dst.write_text(text.replace(old, old + f'    ("{mid}", "README.md", "a line no README holds", "x", ["nothing"]),\n', 1))
PY
rc=0; out="$(python3 "$COPY" --check)" || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "used 2 times"; then
  echo "ok   an id used twice is caught"
else
  echo "FAIL an id used twice: rc=$rc"; echo "$out"; fail=1
fi

if [ "$fail" -ne 0 ]; then echo "test-mutations-check: FAILED"; exit 1; fi
echo "test-mutations-check: all cases behave"
