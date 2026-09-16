#!/usr/bin/env bash
# Falsifies scripts/scrub-check.sh: plants one fake secret of each class in a throwaway git
# repository and requires the gate to name that class. Then checks the two things the gate
# must stay quiet about. The planted values are assembled at runtime, so this file never
# contains a string the gate would flag.
#
#   ./scripts/test-scrub-check.sh
#
# Exit 0 only if every case behaves.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GATE="$HERE/scrub-check.sh"
WORK="$(mktemp -d -t scrubtest.XXXXXX)" || { echo "mktemp failed"; exit 2; }
trap 'rm -rf "$WORK"' EXIT

fail=0
dot="."
at="@"
hex64="$(printf 'ab%.0s' $(seq 1 32))"

new_repo() {
  rm -rf "$WORK/r" && mkdir -p "$WORK/r/scripts" && cp "$GATE" "$WORK/r/scripts/" &&
    git -C "$WORK/r" init -q && printf 'clean\n' >"$WORK/r/README.md"
}

expect_hit() { # label  file-content
  new_repo
  printf '%s\n' "$2" >"$WORK/r/planted.txt"   # left untracked on purpose
  rc=0; out="$("$WORK/r/scripts/scrub-check.sh" "$WORK/r")" || rc=$?
  if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -qF -- "── $1"; then
    echo "ok   caught: $1"
  else
    echo "FAIL missed: $1 (rc=$rc)"; printf '%s\n' "$out"; fail=1
  fi
}

expect_clean() { # label  file-content
  new_repo
  printf '%s\n' "$2" >"$WORK/r/planted.txt"
  rc=0; out="$("$WORK/r/scripts/scrub-check.sh" "$WORK/r")" || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "ok   quiet:  $1"
  else
    echo "FAIL noisy:  $1 (rc=$rc)"; printf '%s\n' "$out"; fail=1
  fi
}

expect_hit "IPv4 literal"        "box at 10${dot}20${dot}30${dot}40 port 22"
expect_hit "EC2 instance id"     "instance i-""0abc1234def567890 stopped"
expect_hit "private key block"   "-----BEGIN EC PRIV""ATE KEY-----"
expect_hit "AWS access key id"   "AKIA""ABCDEFGHIJKLMNOP"
expect_hit "key file reference"  "ssh -i ~/.ssh/box-key${dot}pem"
expect_hit "internal tree path"  "see _hu""b/INBOX for details"
expect_hit "private key literal" "PRIVATE_KEY=0x${hex64}"
expect_hit "e-mail address"      "contact someone${at}example${dot}org"

expect_clean "enclave module id" "module i-""0abc1234def567890-enc0123456789abcdef"
expect_clean "a version number"  "solc 0${dot}8${dot}28 and forge 1${dot}7${dot}1"

if [ "$fail" -ne 0 ]; then
  echo "test-scrub-check: FAILED"
  exit 1
fi
echo "test-scrub-check: all cases behave"
