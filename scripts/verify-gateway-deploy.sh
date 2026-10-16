#!/usr/bin/env bash
# After the gateway is deployed: which commit the pools host runs, and the leverage tests run on that
# very commit.
#
#   ./scripts/verify-gateway-deploy.sh [health-url]        (default https://pools-api.usenami.io/v1/health)
#
# It asks the gateway's /v1/health for the commit its release names (gateway/server.py, REVISION),
# checks that the commit is on origin/main, checks it out into a directory of its own and runs there the
# tests of the leverage check before the signature: an order over the pool's rule is refused before
# anything is signed, an ordinary order goes, and an order that takes the leverage exactly to the rule
# goes. It changes nothing on the host and nothing in this checkout. The last line says PASS or FAIL.
# A health-url may be file:///… for a saved answer; VERIFY_ANY_COMMIT=1 lets a commit that is not on
# origin/main through, to try this script before its commit is merged.
set -uo pipefail
URL="${1:-https://pools-api.usenami.io/v1/health}"
cd "$(dirname "$0")/.." || exit 2
PY="${PYTHON:-$PWD/spike/.venv/bin/python}"
[ -x "$PY" ] || { echo "FAIL no python at $PY; set PYTHON to one with the gateway's requirements"; exit 2; }

health="$(curl -s -m 15 "$URL")" || { echo "FAIL $URL did not answer"; exit 1; }
echo "health: $health"
sha="$(printf '%s' "$health" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("commit") or "")' 2>/dev/null)"
if ! printf '%s' "$sha" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "FAIL the gateway does not say which commit it runs: it is older than the leverage check, or not deployed by ops/host/deploy.sh"
  exit 1
fi
echo "deployed commit: $sha"
git fetch -q origin || { echo "FAIL could not fetch origin"; exit 1; }
if git merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
  echo "on origin/main: yes"
elif [ "${VERIFY_ANY_COMMIT:-}" = 1 ]; then
  echo "on origin/main: no (VERIFY_ANY_COMMIT=1: trying the script on a commit before it is merged)"
else
  echo "FAIL $sha is not on origin/main as fetched just now"
  exit 1
fi

dir="$(mktemp -d -t gwdeploy.XXXXXX)"
cleanup() { git worktree remove --force "$dir/tree" >/dev/null 2>&1; rmdir "$dir" 2>/dev/null; }
trap cleanup EXIT
git worktree add -q --detach "$dir/tree" "$sha" || { echo "FAIL could not check out $sha"; exit 1; }
cd "$dir/tree" || exit 1
TESTS=(
  gateway.tests.test_protect.Leverage.test_an_order_that_takes_the_account_over_its_leverage_is_refused_before_anything_is_signed
  gateway.tests.test_protect.Leverage.test_an_ordinary_order_well_inside_the_rule_is_signed_and_sent
  gateway.tests.test_protect.Leverage.test_an_order_that_takes_the_leverage_exactly_to_the_rule_is_signed_and_sent
)
echo "tests at $sha:"
if "$PY" -m unittest -v "${TESTS[@]}" 2>&1 | grep -Ev '^\{"t": '; [ "${PIPESTATUS[0]}" -eq 0 ]; then
  echo "PASS the pools host runs $sha, and its leverage tests pass"
else
  echo "FAIL the leverage tests of $sha did not pass"
  exit 1
fi
