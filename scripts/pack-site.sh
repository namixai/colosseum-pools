#!/usr/bin/env bash
# The site, packed for upload from one commit:
#
#   ./scripts/pack-site.sh <commit> [out-dir]
#
# Copies app/ of that commit file by file from its blobs into out-dir (default
# ~/Downloads/pools-app-<first 8 of the commit>), checks every file against its blob, and writes
# build.json beside index.html: {"commit": "<the full hash>"}. The site shows that commit in its
# footer, so anyone can check the copy they are looking at against the code it came from.
# build.json is the one file that is not in the commit; the last line says it apart from the rest.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
SHA="$(git rev-parse --verify "${1:?a commit}^{commit}")"
OUT="${2:-$HOME/Downloads/pools-app-${SHA:0:8}}"
[ -e "$OUT" ] && { echo "exists: $OUT"; exit 1; }
mkdir -p "$OUT"
n=0
while IFS=$'\t' read -r meta path; do
  blob=$(echo "$meta" | awk '{print $3}'); rel="${path#app/}"
  mkdir -p "$OUT/$(dirname "$rel")"
  git cat-file blob "$blob" > "$OUT/$rel"; n=$((n+1))
done < <(git ls-tree -r "$SHA" -- app/)
[ -e "$OUT/build.json" ] && { echo "app/build.json is in the commit; it is the packer's to write"; exit 1; }
printf '{"commit":"%s"}\n' "$SHA" > "$OUT/build.json"
ok=0; bad=0
while IFS=$'\t' read -r meta path; do
  blob=$(echo "$meta" | awk '{print $3}'); rel="${path#app/}"
  if cmp -s <(git cat-file blob "$blob") "$OUT/$rel" && [ "$(git hash-object --no-filters "$OUT/$rel")" = "$blob" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "DIFF $rel"; fi
done < <(git ls-tree -r "$SHA" -- app/)
extra=$(( $(find "$OUT" -type f | wc -l) - n - 1 ))
echo "files=$n match=$ok differ=$bad extra=$extra build=$(cat "$OUT/build.json") bytes=$(find "$OUT" -type f -exec cat {} + | wc -c | tr -d ' ') out=$OUT"
