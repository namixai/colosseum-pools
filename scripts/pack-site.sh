#!/usr/bin/env bash
# The site, packed for upload from one commit:
#
#   ./scripts/pack-site.sh [--network testnet|mainnet] <commit> [out-dir]
#
# Copies app/ of that commit file by file from its blobs into out-dir (default
# ~/Downloads/pools-app-<first 8 of the commit>), checks every file against its blob, and writes
# build.json beside index.html: {"commit": "<the full hash>"}. The site shows that commit in its
# footer, so anyone can check the copy they are looking at against the code it came from.
# build.json is the one file that is not in the commit; the last line says it apart from the rest.
#
# Beside out-dir, not in it, goes <out-dir>.sha256: a sha256 for every file of the pack, build.json
# included, as `shasum -a 256 -c` reads it from inside out-dir. scripts/verify-live.py checks the site
# against it once the folder is uploaded. It stays out of the folder because the folder is what is
# uploaded, and build.json is to remain the one file on the site that is not in the commit.
#
# --network mainnet packs the same commit for mainnet. Its config.js and its index.html are then not the commit's,
# which are testnet's: they are what that commit's scripts/app_config.py writes for mainnet (the settings, and with
# --index the page), from the same commit's spike/hlspike/network.py, deployments/LIVE.json and app/index.html, and
# build.json says so: {"commit": "<hash>", "network": "mainnet"}. Those two files are checked against that output and
# every other file against its blob, as for testnet. The script stops when the commit has nothing for a mainnet site
# to read (no live deployment and no staged simulation-pool deployment on mainnet), or when the page's words have
# moved from where the script replaces them, and the packer with it, writing nothing. Testnet, the default, packs as
# it always has: the commit's own config.js and index.html, and build.json with the commit alone.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
NETWORK=testnet
if [ "${1:-}" = "--network" ]; then NETWORK="${2:?testnet or mainnet}"; shift 2; fi
case "$NETWORK" in testnet|mainnet) ;; *) echo "no network $NETWORK: testnet or mainnet"; exit 1 ;; esac
SHA="$(git rev-parse --verify "${1:?a commit}^{commit}")"
OUT="${2:-$HOME/Downloads/pools-app-${SHA:0:8}}"
OUT="${OUT%/}"
SUMS="$OUT.sha256"
[ -e "$OUT" ] && { echo "exists: $OUT"; exit 1; }
[ -e "$SUMS" ] && { echo "exists: $SUMS"; exit 1; }
# A commit with no site has nothing to upload: an empty copy would report every file matching.
git cat-file -e "$SHA:app/index.html" 2>/dev/null || { echo "no app/index.html in $SHA: nothing to upload"; exit 1; }
# Files only: a symlink or a submodule would be written as the text of its target and still match its blob.
odd=$(git ls-tree -r "$SHA" -- app/ | awk '$1 != "100644" && $1 != "100755"')
[ -z "$odd" ] || { echo "app/ of $SHA holds something other than files, which the packer does not copy:"; printf '%s\n' "$odd"; exit 1; }
# Another network's config.js and index.html, written before anything else is: a commit that cannot have them gets
# no folder.
CONFIG=""; INDEX=""
if [ "$NETWORK" != testnet ]; then
  WORK="$(mktemp -d -t pack-site.XXXXXX)"; trap 'rm -rf "$WORK"' EXIT
  git show "$SHA:scripts/app_config.py" > "$WORK/app_config.py" 2>/dev/null \
    || { echo "no scripts/app_config.py in $SHA: it cannot be packed for $NETWORK"; exit 1; }
  python3 "$WORK/app_config.py" --network "$NETWORK" --commit "$SHA" --root "$REPO" --out "$WORK/config.js" \
    || { echo "no $NETWORK settings in $SHA: nothing packed"; exit 1; }
  python3 "$WORK/app_config.py" --network "$NETWORK" --index --commit "$SHA" --root "$REPO" --out "$WORK/index.html" \
    || { echo "no $NETWORK index.html in $SHA: nothing packed"; exit 1; }
  CONFIG="$WORK/config.js"; INDEX="$WORK/index.html"
fi
mkdir -p "$OUT"
n=0
while IFS=$'\t' read -r meta path; do
  blob=$(echo "$meta" | awk '{print $3}'); rel="${path#app/}"
  mkdir -p "$OUT/$(dirname "$rel")"
  git cat-file blob "$blob" > "$OUT/$rel"; n=$((n+1))
done < <(git ls-tree -r "$SHA" -- app/)
[ -e "$OUT/build.json" ] && { echo "app/build.json is in the commit; it is the packer's to write"; exit 1; }
if [ -n "$CONFIG" ]; then
  cp "$CONFIG" "$OUT/config.js"
  cp "$INDEX" "$OUT/index.html"
  printf '{"commit":"%s","network":"%s"}\n' "$SHA" "$NETWORK" > "$OUT/build.json"
else
  printf '{"commit":"%s"}\n' "$SHA" > "$OUT/build.json"
fi
ok=0; bad=0
while IFS=$'\t' read -r meta path; do
  blob=$(echo "$meta" | awk '{print $3}'); rel="${path#app/}"
  if [ -n "$CONFIG" ] && [ "$rel" = config.js ]; then
    if cmp -s "$CONFIG" "$OUT/$rel"; then ok=$((ok+1)); else bad=$((bad+1)); echo "DIFF $rel"; fi
  elif [ -n "$INDEX" ] && [ "$rel" = index.html ]; then
    if cmp -s "$INDEX" "$OUT/$rel"; then ok=$((ok+1)); else bad=$((bad+1)); echo "DIFF $rel"; fi
  elif cmp -s <(git cat-file blob "$blob") "$OUT/$rel" && [ "$(git hash-object --no-filters "$OUT/$rel")" = "$blob" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "DIFF $rel"; fi
done < <(git ls-tree -r "$SHA" -- app/)
extra=$(( $(find "$OUT" -type f | wc -l) - n - 1 ))
line="files=$n match=$ok differ=$bad extra=$extra build=$(cat "$OUT/build.json") bytes=$(find "$OUT" -type f -exec cat {} + | wc -c | tr -d ' ') out=$OUT"
# A pack that is not its commit gets no list: the site would be checked against the wrong files.
if [ "$bad" -ne 0 ] || [ "$extra" -ne 0 ]; then echo "$line"; echo "no $SUMS: the pack differs from its commit"; exit 1; fi
# The same tool on Linux and on a Mac, whichever is there: both print "<sha256>  ./<file>".
SUM=(sha256sum); command -v sha256sum >/dev/null 2>&1 || SUM=(shasum -a 256)
(cd "$OUT" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 "${SUM[@]}") > "$SUMS"
listed=$(wc -l < "$SUMS" | tr -d ' ')
[ "$listed" -eq $((n + 1)) ] || { echo "$SUMS lists $listed files, the pack holds $((n + 1))"; exit 1; }
echo "$line sums=$SUMS"
