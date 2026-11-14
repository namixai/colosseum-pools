#!/usr/bin/env bash
# One flat Solidity file of the simulated pool's contracts, at a named commit, for an AI code check that takes a
# single file (CertiK's, for one).
#
#     scripts/flatten-sim.sh <commit> [out-dir]
#
# The contracts ops/deploy_sim.py deploys -- PaperExam, SimPool (with its libraries, through its imports), TermsBook
# and SimPoolFactory -- and everything they import, once each, in one file: `forge flatten` of an entry file that imports the four. The
# entry is written in a temporary git worktree of the commit and never committed; this checkout is not touched.
#
# Writes to out-dir (default: a new directory under $TMPDIR):
#   sim-<commit12>-flat.sol   the flat file
#   MANIFEST.txt              the commit, forge and solc versions, each source file with its sha256, the flat
#                             file's sha256, and that the flat file compiled on its own
#
# The flat file only repeats the sources of that commit, so it is not committed here: run this again for any
# later commit, the one that brings the menu of sizes for one, and upload the new file with its MANIFEST.
#
# The library (lib/hyper-evm-lib and its own submodules) is checked out at the commit's pinned revision with
# `git submodule update --init --recursive`, which reads GitHub. FLATTEN_LIB_FROM=<dir> uses a local checkout of
# hyper-evm-lib instead, and only if its HEAD is the revision the commit pins.
set -euo pipefail

usage() { echo "usage: $0 <commit> [out-dir]" >&2; exit 2; }
[ $# -ge 1 ] && [ $# -le 2 ] || usage

ROOT=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)
COMMIT=$(git -C "$ROOT" rev-parse --verify "$1^{commit}") || { echo "no such commit: $1" >&2; exit 2; }
SHORT=${COMMIT:0:12}
OUT=${2:-$(mktemp -d "${TMPDIR:-/tmp}/sim-flat-$SHORT.XXXXXX")}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

CONTRACTS=(src/exam/PaperExam.sol src/sim/SimPool.sol src/multi/TermsBook.sol src/sim/SimPoolFactory.sol)

WT=$(mktemp -d "${TMPDIR:-/tmp}/sim-flat-wt.XXXXXX")
cleanup() { git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"; }
trap cleanup EXIT
rmdir "$WT"
git -C "$ROOT" worktree add --detach --quiet "$WT" "$COMMIT"

for f in "${CONTRACTS[@]}"; do
  [ -f "$WT/$f" ] || { echo "$f is not in $SHORT" >&2; exit 1; }
done

PIN=$(git -C "$WT" ls-tree HEAD lib/hyper-evm-lib | awk '{print $3}')
if [ -n "${FLATTEN_LIB_FROM:-}" ]; then
  HAVE=$(git -C "$FLATTEN_LIB_FROM" rev-parse HEAD)
  [ "$HAVE" = "$PIN" ] || { echo "FLATTEN_LIB_FROM is at $HAVE, the commit pins $PIN" >&2; exit 1; }
  # Its HEAD names a commit, not the files: a change not committed would be flattened under the pin's name.
  [ -z "$(git -C "$FLATTEN_LIB_FROM" status --porcelain --ignore-submodules=none)" ] \
    || { echo "FLATTEN_LIB_FROM has uncommitted changes, or a submodule off its pinned revision" >&2; exit 1; }
  rmdir "$WT/lib/hyper-evm-lib" 2>/dev/null || true
  ln -s "$(cd "$FLATTEN_LIB_FROM" && pwd)" "$WT/lib/hyper-evm-lib"
else
  git -C "$WT" submodule update --init --recursive --quiet
fi

ENTRY=src/_FlatEntry.sol
{
  echo "// SPDX-License-Identifier: MIT"
  echo "pragma solidity ^0.8.28;"
  for f in "${CONTRACTS[@]}"; do echo "import \"./${f#src/}\";"; done
} > "$WT/$ENTRY"

FLAT="$OUT/sim-$SHORT-flat.sol"
(cd "$WT" && forge flatten "$ENTRY" --output "$FLAT" >/dev/null)
rm "$WT/$ENTRY"

# The flat file on its own, with nothing to import: it must compile as it is uploaded.
mkdir -p "$WT/flatcheck/src"
cp "$FLAT" "$WT/flatcheck/src/Flat.sol"
SOLC=$(sed -n 's/^solc_version *= *"\(.*\)"/\1/p' "$WT/foundry.toml")
EVM=$(sed -n 's/^evm_version *= *"\(.*\)"/\1/p' "$WT/foundry.toml")
(cd "$WT/flatcheck" && forge build --root . --contracts src --use "$SOLC" --evm-version "$EVM" --optimize \
  --optimizer-runs 200 --skip test --skip script >"$WT/flatcheck.log" 2>&1) \
  || { cat "$WT/flatcheck.log" >&2; echo "the flat file does not compile on its own" >&2; exit 1; }

sha() { local d; d=$(shasum -a 256 "$1" | awk '{print $1}'); [ ${#d} -eq 64 ] || { echo "no sha256 of $1" >&2; exit 1; }; echo "$d"; }
FLAT_SHA=$(sha "$FLAT")
SOURCES=$(grep -E '^// (src|lib)/' "$FLAT" | sed 's#^// ##' | sort -u)
SUMS=""
for f in $SOURCES; do
  if [ -f "$WT/$f" ]; then digest=$(sha "$WT/$f"); SUMS+="  $digest  $f"$'\n'; fi
done
{
  echo "simulated pool, flat file for an AI code check"
  echo "commit        $COMMIT"
  echo "made (UTC)    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "forge         $(forge --version | head -1)"
  echo "solc          $SOLC, evm $EVM, optimizer 200 runs (foundry.toml of the commit)"
  echo "hyper-evm-lib $PIN"
  echo "flat file     $(basename "$FLAT")  sha256 $FLAT_SHA  $(wc -l < "$FLAT" | tr -d ' ') lines"
  echo "compiles      on its own, with nothing to import"
  echo "contracts     ${CONTRACTS[*]}"
  echo "sources (sha256 at the commit):"
  printf '%s' "$SUMS"
} > "$OUT/MANIFEST.txt"

echo "flat file: $FLAT"
echo "manifest:  $OUT/MANIFEST.txt"
