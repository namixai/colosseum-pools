#!/usr/bin/env bash
# Every commit in this repository's history must carry a GitHub noreply identity, as
# author and as committer. The repository is meant to go public, and a personal address
# in commit metadata is published with it; git keeps it after any later fix.
#
#   ./scripts/identity-check.sh [rev]      # default: HEAD, whole history
#
# Commits made by GitHub itself (merges from the web UI) carry GitHub's own noreply
# address as committer, which is allowed. What a merge puts in the AUTHOR field depends on the
# merging account's e-mail privacy setting, and this check is how we find out.
#
# Exit 0 = clean, 1 = at least one commit with another identity, 2 = could not run.

set -uo pipefail

REV="${1:-HEAD}"
git rev-parse --verify --quiet "$REV" >/dev/null || { echo "identity-check: unknown revision $REV"; exit 2; }

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "identity-check: shallow clone, history incomplete; fetch with depth 0"
  exit 2
fi

allowed='(@users\.noreply\.github\.com|^noreply@github\.com)$'
bad=0
total=0
while IFS=' ' read -r sha author committer; do
  total=$((total + 1))
  a_ok=0; c_ok=0
  printf '%s\n' "$author" | grep -Eq "$allowed" && a_ok=1
  printf '%s\n' "$committer" | grep -Eq "$allowed" && c_ok=1
  if [ "$a_ok" -ne 1 ] || [ "$c_ok" -ne 1 ]; then
    # Name the commit and which field is wrong, never the address itself.
    echo "identity-check: ${sha} author_ok=${a_ok} committer_ok=${c_ok}"
    bad=$((bad + 1))
  fi
done < <(git log --format='%H %ae %ce' "$REV")

if [ "$bad" -ne 0 ]; then
  echo "identity-check: ${bad} of ${total} commits carry a non-noreply identity"
  exit 1
fi
echo "identity-check: clean (${total} commits)"
