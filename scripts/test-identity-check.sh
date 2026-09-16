#!/usr/bin/env bash
# Falsifies scripts/identity-check.sh: a history of noreply commits must pass, and one
# commit with a personal-looking author or committer must fail and be named. The planted
# address is assembled at runtime so this file holds nothing the scrub gate would flag.

set -uo pipefail

GATE="$(cd "$(dirname "$0")" && pwd)/identity-check.sh"
WORK="$(mktemp -d -t idtest.XXXXXX)" || { echo "mktemp failed"; exit 2; }
trap 'rm -rf "$WORK"' EXIT

at="@"
noreply="1+bot${at}users.noreply.github.com"
personal="someone${at}example.org"
fail=0

commit_as() { # author-email committer-email message
  GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL="$1" GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL="$2" \
    git -C "$WORK/r" commit -q --allow-empty -m "$3"
}

git init -q "$WORK/r"
commit_as "$noreply" "$noreply" one
commit_as "$noreply" "noreply${at}github.com" two

rc=0; out="$(cd "$WORK/r" && "$GATE")" || rc=$?
if [ "$rc" -eq 0 ]; then echo "ok   clean history passes"; else echo "FAIL clean history: rc=$rc"; echo "$out"; fail=1; fi

commit_as "$personal" "$noreply" three
bad_sha="$(git -C "$WORK/r" rev-parse HEAD)"
rc=0; out="$(cd "$WORK/r" && "$GATE")" || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "${bad_sha} author_ok=0 committer_ok=1"; then
  echo "ok   personal author caught"
else
  echo "FAIL personal author: rc=$rc"; echo "$out"; fail=1
fi
if printf '%s' "$out" | grep -qF "$personal"; then
  echo "FAIL the gate printed the address it caught"; fail=1
fi

commit_as "$noreply" "$personal" four
bad_sha="$(git -C "$WORK/r" rev-parse HEAD)"
rc=0; out="$(cd "$WORK/r" && "$GATE")" || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "${bad_sha} author_ok=1 committer_ok=0"; then
  echo "ok   personal committer caught"
else
  echo "FAIL personal committer: rc=$rc"; echo "$out"; fail=1
fi

if [ "$fail" -ne 0 ]; then echo "test-identity-check: FAILED"; exit 1; fi
echo "test-identity-check: all cases behave"
