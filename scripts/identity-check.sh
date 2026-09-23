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
#
# KNOWN_IDENTITIES below names the commits this check accepts anyway, one line each: the full
# 40-character commit id, a space, and the reason it is there. An id names one commit and
# everything in it, author and committer included -- change any of that and the id changes with
# it, so an entry cannot come to cover a different identity later. Nothing is skipped by
# address or by pattern, a shortened id never matches the full id the log prints, and every
# commit not named here is checked as before. Each run says which commits it skipped and why,
# because a list nobody reads is a hole. Adding a line is a decision to publish that identity:
# it is reviewed like any other change, and the reason is what the reviewer weighs.

set -uo pipefail

KNOWN_IDENTITIES='
ceee373edce212f1bf074039a5a2cb224cdb5307 merged on 22 Sep 2026 with the GitHub merge button, which rewrote the committer; same tree and same parent as f834847, the head that passed this check
'

REV="${1:-HEAD}"
git rev-parse --verify --quiet "$REV" >/dev/null || { echo "identity-check: unknown revision $REV"; exit 2; }

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "identity-check: shallow clone, history incomplete; fetch with depth 0"
  exit 2
fi

allowed='(@users\.noreply\.github\.com|^noreply@github\.com)$'
bad=0
total=0
known=0
while IFS=' ' read -r sha author committer; do
  total=$((total + 1))
  entry="$(printf '%s\n' "$KNOWN_IDENTITIES" | grep "^${sha} " | head -1)"
  if [ -n "$entry" ]; then
    echo "identity-check: ${sha} known, not checked: ${entry#* }"
    known=$((known + 1))
    continue
  fi
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
echo "identity-check: clean (${total} commits, ${known} known and not checked)"
