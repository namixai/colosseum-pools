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

# The list of known commits. The gate's own list holds a commit of this repository, which no
# throwaway history can contain, so these cases run copies of the gate with one line added to
# that list. The gate itself has no way to be told about extra commits: a switch for that would
# be the hole the list is written to avoid.
gate_with() { # id reason -> a copy of the gate whose list also holds that line
  copy="$WORK/gate-$(echo "$1" | cut -c1-8).sh"
  awk -v line="$1 $2" '{ print } /^KNOWN_IDENTITIES=/ { print line }' "$GATE" > "$copy"
  chmod +x "$copy"
  printf '%s' "$copy"
}

# Its own history: the cases above left bad commits in "r", and a list that skipped those too
# would prove nothing about the one being listed here.
commit_in() { # author-email committer-email message
  GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL="$1" GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL="$2" \
    git -C "$WORK/l" commit -q --allow-empty -m "$3"
}
git init -q "$WORK/l"
commit_in "$noreply" "$noreply" one
commit_in "$noreply" "$personal" two
listed_sha="$(git -C "$WORK/l" rev-parse HEAD)"

rc=0; out="$(cd "$WORK/l" && "$(gate_with "$listed_sha" "a reason a reviewer would weigh")")" || rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "${listed_sha} known, not checked: a reason a reviewer would weigh"; then
  echo "ok   a listed commit passes, and the run says which and why"
else
  echo "FAIL listed commit passes: rc=$rc"; echo "$out"; fail=1
fi
if printf '%s' "$out" | grep -qF "$personal"; then
  echo "FAIL the gate printed the address of a known commit"; fail=1
fi

# One entry covers one commit: the next bad one is caught even while the list is in use.
commit_in "$personal" "$noreply" three
other_sha="$(git -C "$WORK/l" rev-parse HEAD)"
rc=0; out="$(cd "$WORK/l" && "$(gate_with "$listed_sha" "a reason a reviewer would weigh")")" || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "${other_sha} author_ok=0"; then
  echo "ok   list covers the commit it names and no other"
else
  echo "FAIL list covers only what it names: rc=$rc"; echo "$out"; fail=1
fi

# A shortened id is not an id: it must not stand for the commit whose id starts with it.
rc=0; out="$(cd "$WORK/l" && "$(gate_with "$(echo "$other_sha" | cut -c1-8)" "a short id is not an id")")" || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "${other_sha} author_ok=0"; then
  echo "ok   a shortened id in the list matches nothing"
else
  echo "FAIL a short id does not match: rc=$rc"; echo "$out"; fail=1
fi

# An entry about a history this check is not looking at changes nothing.
git init -q "$WORK/clean"
GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL="$noreply" GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL="$noreply" \
  git -C "$WORK/clean" commit -q --allow-empty -m one
rc=0; out="$(cd "$WORK/clean" && "$(gate_with "$listed_sha" "a commit of another history")")" || rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "0 known"; then
  echo "ok   an entry for a commit that is not here is harmless"
else
  echo "FAIL an entry for another history is harmless: rc=$rc"; echo "$out"; fail=1
fi

if [ "$fail" -ne 0 ]; then echo "test-identity-check: FAILED"; exit 1; fi
echo "test-identity-check: all cases behave"
