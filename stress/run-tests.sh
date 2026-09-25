#!/usr/bin/env bash
# Everything this package promises, in one command:
#   1. the rules, on synthetic data where the answer is known in advance (21 checks);
#   2. the breakage stand: 26 deliberate bugs, each one must turn a check red;
#   3. the anchor: the snapshot still produces the published numbers, and README.md still
#      says what the results say.
#
#   ./run-tests.sh            # needs python3 with numpy
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
"$PY" -c "import numpy" 2>/dev/null || { echo "numpy is required: pip install numpy"; exit 2; }
fail=0
for step in test_pool_stress.py mut_pool_stress.py test_anchor.py; do
  echo "──── $step"
  "$PY" "$HERE/$step" || fail=$((fail+1))
done
echo
[ "$fail" = 0 ] && echo "ALL GREEN" || echo "FAILED: $fail of 3 steps"
exit "$fail"
