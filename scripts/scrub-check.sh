#!/usr/bin/env bash
# Hygiene gate for this repository. Runs in CI on every push and pull request.
#
#   ./scripts/scrub-check.sh [path]
#
# Generic patterns only, on purpose. A script that greps a repository for one specific
# server address has to contain that address, and it would sit in the very repository it
# is meant to protect. The exact-string list lives outside this repository and runs before
# every push. This layer is the half anyone can read.
#
# Portable to bash 3.2, because that is what ships on macOS, and a gate that only runs on
# the CI box catches things after they are already pushed.
#
# Exit 0 = clean. Exit 1 = something matched; read it before you argue with it.
# Exit 2 = the gate itself could not run.

set -uo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || { echo "cannot enter $ROOT"; exit 2; }

LIST="$(mktemp -t scrubfiles.XXXXXX)" || { echo "mktemp failed"; exit 2; }
trap 'rm -f "$LIST"' EXIT

if git rev-parse --git-dir >/dev/null 2>&1; then
  # Tracked AND not-yet-tracked files, minus anything gitignored. `git ls-files` alone
  # reports "clean" on a tree whose new files are still untracked, which is exactly the
  # moment the gate is needed.
  git ls-files -z --cached --others --exclude-standard >"$LIST"
else
  find . -type f -not -path './node_modules/*' -not -path './.git/*' -not -path './lib/*' -print0 >"$LIST"
fi

nfiles=$(tr -dc '\0' <"$LIST" | wc -c | tr -d ' ')
if [ "$nfiles" -eq 0 ]; then
  echo "scrub-check: no files to scan under $ROOT"
  exit 0
fi

SELF="scripts/$(basename "$0")"
hits=0

scan() { # label  extended-regex  [regex of lines to ignore]
  label="$1"; re="$2"; drop="${3:-}"
  out=$(xargs -0 grep -InE "$re" <"$LIST" 2>/dev/null || true)
  out=$(printf '%s\n' "$out" | grep -v "^\.\{0,2\}/\{0,1\}${SELF}:" || true)
  if [ -n "$drop" ]; then
    out=$(printf '%s\n' "$out" | grep -vE "$drop" || true)
  fi
  out=$(printf '%s' "$out" | sed '/^$/d')
  if [ -n "$out" ]; then
    echo "── ${label}"
    printf '%s\n' "$out" | head -20
    hits=$((hits + 1))
  fi
}

# An IPv4 literal anywhere. Version numbers do not have four parts, so this stays quiet in
# practice; a hit is either infrastructure or a false positive worth a look.
scan "IPv4 literal" \
  '(^|[^0-9.])(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}([^0-9.]|$)' \
  '0\.0\.0\.0|127\.0\.0\.1|255\.255\.255|1\.2\.3\.4'

# EC2 instance id. An enclave module id in an attestation document looks the same but ends
# in -encXXXX, and the public /attestation endpoint hands that one to anyone who asks.
scan "EC2 instance id" 'i-[0-9a-f]{8,17}([^0-9a-f-]|$)' 'i-[0-9a-f]{8,17}-enc'

scan "private key block"   '\-\-\-\-\-BEGIN [A-Z ]*PRIVATE KEY'
scan "certificate block"   '\-\-\-\-\-BEGIN CERTIFICATE'
scan "AWS access key id"   '(AKIA|ASIA)[0-9A-Z]{16}'
scan "key file reference"  '(id_rsa|id_ed25519)|[A-Za-z0-9_.-]+\.pem'
scan "internal tree path"  '_hub/'
scan "slack/github/telegram token" \
  'xox[bp]-[0-9A-Za-z-]{10,}|gh[pousr]_[0-9A-Za-z]{30,}|bot[0-9]{6,}:AA[0-9A-Za-z_-]{30,}'

# A 32-byte hex value on the same line as the word "private" (any case): the shape of a
# pasted secp256k1 key. Transaction hashes and digests don't usually sit next to that word;
# when one does, explain it rather than widen the ignore list.
scan "private key literal" '[Pp][Rr][Ii][Vv][Aa][Tt][Ee].{0,60}(0x)?[0-9a-fA-F]{64}'

# Any e-mail address. No allowlist by design: if one belongs here, add it deliberately and
# say why, rather than teaching the gate to stay quiet.
scan "e-mail address" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'

echo
if [ "$hits" -ne 0 ]; then
  echo "scrub-check: ${hits} pattern group(s) matched. Nothing is pushed until each one is explained."
  echo "Reminder: git keeps history. A revert does not un-publish."
  exit 1
fi
echo "scrub-check: clean (${nfiles} files)."
