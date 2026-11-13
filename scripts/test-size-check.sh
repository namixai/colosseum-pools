#!/usr/bin/env bash
# Falsifies scripts/size-check.py on a made-up table and source tree, so the real tree is never touched:
# a contract of src/ one byte over the runtime limit, one over the creation limit, and one missing from
# the table must each turn it red, with the right exit and the name; a table where all fit must be green;
# a contract over the limit that is not declared in src/ (the test library's simulator) must not count.
#
#   ./scripts/test-size-check.sh
#
# A check that cannot go red proves nothing, which is the only reason this file exists.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK="$HERE/size-check.py"
WORK="$(mktemp -d)" || { echo "mktemp failed"; exit 2; }
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/src/sim"
printf 'contract Pool {}\nabstract contract Base {}\ninterface IThing {}\nlibrary Maths {}\n    contract Indented {}\n// contract Gone {}\n/* contract Old {\n} */\n' > "$WORK/src/sim/Pool.sol"

failures=0
case_() {  # case_ <name> <expected exit> <text the output must carry> <json>
  printf '%s' "$4" > "$WORK/sizes.json"
  out="$(python3 "$CHECK" --sizes "$WORK/sizes.json" --src "$WORK/src" 2>&1)"
  got=$?
  if [ "$got" -ne "$2" ] || ! grep -q -- "$3" <<<"$out"; then
    echo "FAIL $1: exit $got (wanted $2), output: $out"
    failures=$((failures + 1))
  else
    echo "ok   $1"
  fi
}

fit='"runtime_size":24576,"init_size":49152'
small='"runtime_size":100,"init_size":150'
case_ "all fit, the limits themselves included; commented-out contracts are not asked for" 0 "size-check: clean" \
  "{\"Pool\":{$fit},\"Maths\":{$small},\"Indented\":{$small},\"HyperCore\":{\"runtime_size\":54894,\"init_size\":55000}}"
case_ "a runtime one byte over" 1 "over the limit.*Pool" \
  "{\"Pool\":{\"runtime_size\":24577,\"init_size\":30000},\"Maths\":{$small},\"Indented\":{$small}}"
case_ "a creation code one byte over" 1 "over the limit.*Maths" \
  "{\"Pool\":{$small},\"Maths\":{\"runtime_size\":100,\"init_size\":49153},\"Indented\":{$small}}"
case_ "an indented contract over the limit" 1 "over the limit.*Indented" \
  "{\"Pool\":{$small},\"Maths\":{$small},\"Indented\":{\"runtime_size\":30000,\"init_size\":30000}}"
case_ "a contract of src/ missing from the table" 2 "not in forge's table: Maths" \
  "{\"Pool\":{$small},\"Indented\":{$small}}"
case_ "a table that is not JSON" 2 "could not run" "not json"

if [ "$failures" -ne 0 ]; then
  echo "test-size-check: $failures case(s) misbehaved"
  exit 1
fi
echo "test-size-check: every case behaved."
