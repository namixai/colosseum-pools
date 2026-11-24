#!/usr/bin/env bash
# The packer still does what it says: every file of app/ equal to its blob, build.json with the commit
# and nothing else beside them, and the sha256 list of all of it beside the folder. Packs this checkout's
# HEAD into a directory of its own and removes it.
set -euo pipefail
cd "$(dirname "$0")/.."
TMP="$(mktemp -d -t packsite.XXXXXX)"; trap 'rm -rf "$TMP"' EXIT
HEAD="$(git rev-parse HEAD)"
n="$(git ls-tree -r HEAD -- app/ | wc -l | tr -d ' ')"
line="$(./scripts/pack-site.sh HEAD "$TMP/out" | tail -1)"
want="files=$n match=$n differ=0 extra=0 build={\"commit\":\"$HEAD\"} "
case "$line" in
  "$want"*) echo "ok   packed $n files and build.json for $HEAD" ;;
  *) echo "FAIL the packer said: $line"; echo "     expected it to start: $want"; exit 1 ;;
esac
[ "$(cat "$TMP/out/build.json")" = "{\"commit\":\"$HEAD\"}" ] || { echo "FAIL build.json"; exit 1; }
# The list scripts/verify-live.py reads: beside the pack, every file of it and build.json, and it checks the pack.
SUMS="$TMP/out.sha256"
case "$line" in *" sums=$SUMS") ;; *) echo "FAIL the last line does not name the list: $line"; exit 1 ;; esac
[ "$(wc -l < "$SUMS" | tr -d ' ')" -eq $((n + 1)) ] || { echo "FAIL the list does not hold every file and build.json"; exit 1; }
grep -q '^[0-9a-f]\{64\}  \./build\.json$' "$SUMS" || { echo "FAIL the list leaves out build.json"; exit 1; }
[ -z "$(find "$TMP/out" -name '*.sha256')" ] || { echo "FAIL the list went into the pack"; exit 1; }
SUM=(sha256sum); command -v sha256sum >/dev/null 2>&1 || SUM=(shasum -a 256)
(cd "$TMP/out" && "${SUM[@]}" -c --status "$SUMS") || { echo "FAIL the list does not check the pack"; exit 1; }
echo "ok   the list beside the pack checks every file of it"
./scripts/pack-site.sh HEAD "$TMP/slash/" >/dev/null
[ -f "$TMP/slash.sha256" ] && [ -z "$(find "$TMP/slash" -name '*.sha256')" ] || { echo "FAIL a trailing slash put the list into the pack"; exit 1; }
echo "ok   a trailing slash on the folder keeps the list beside it"
if ./scripts/pack-site.sh HEAD "$TMP/out" >/dev/null 2>&1; then echo "FAIL packed over an existing directory"; exit 1; fi
echo "ok   refuses a directory that exists"
: > "$TMP/listed.sha256"
if ./scripts/pack-site.sh HEAD "$TMP/listed" >/dev/null 2>&1 || [ -e "$TMP/listed" ]; then echo "FAIL packed beside a list that exists"; exit 1; fi
echo "ok   refuses a list that exists, and writes nothing"
# A commit with no site, and one whose app/ holds a symlink: both refused, and nothing written. The two
# commits are made here, as objects no branch points to; a runner has no name for them, so they get one.
export GIT_AUTHOR_NAME=test-pack-site GIT_AUTHOR_EMAIL=test-pack-site
export GIT_COMMITTER_NAME=test-pack-site GIT_COMMITTER_EMAIL=test-pack-site
none="$(git commit-tree "$(git mktree </dev/null)" -m "no site")"
if ./scripts/pack-site.sh "$none" "$TMP/none" >/dev/null 2>&1 || [ -e "$TMP/none" ]; then echo "FAIL packed a commit with no app/index.html"; exit 1; fi
echo "ok   refuses a commit with no site"
index="$(git rev-parse HEAD:app/index.html)"
link="$(printf 'index.html' | git hash-object -w --stdin)"
app="$(printf '100644 blob %s\tindex.html\n120000 blob %s\tlink.html\n' "$index" "$link" | git mktree)"
linked="$(git commit-tree "$(printf '040000 tree %s\tapp\n' "$app" | git mktree)" -m "a symlink in app")"
if ./scripts/pack-site.sh "$linked" "$TMP/linked" >/dev/null 2>&1 || [ -e "$TMP/linked" ]; then echo "FAIL packed a symlink as a file"; exit 1; fi
echo "ok   refuses a symlink in app/"
# Another network. Testnet named is the default; a network the packer does not know is refused, and so is mainnet
# for a commit with nothing on mainnet for a site to read, writing nothing either way.
line2="$(./scripts/pack-site.sh --network testnet HEAD "$TMP/named" | tail -1)"
[ "${line2%% out=*}" = "${line%% out=*}" ] || { echo "FAIL --network testnet: it packed otherwise than the default: $line2"; exit 1; }
echo "ok   --network testnet packs as the default does"
if ./scripts/pack-site.sh --network devnet HEAD "$TMP/devnet" >/dev/null 2>&1 || [ -e "$TMP/devnet" ]; then echo "FAIL --network devnet: packed for a network there is none of"; exit 1; fi
echo "ok   refuses a network it does not know"
if [ -z "$(git show HEAD:deployments/LIVE.json | python3 -c 'import json,sys; print(*[e["label"] for e in json.load(sys.stdin)["deployments"] if e["network"] == "mainnet" and e["role"] in ("live", "staged")])')" ]; then
  if ./scripts/pack-site.sh --network mainnet HEAD "$TMP/nomain" >/dev/null 2>&1 || [ -e "$TMP/nomain" ]; then echo "FAIL mainnet pack: packed a commit with nothing on mainnet for a site to read"; exit 1; fi
  echo "ok   refuses mainnet for a commit with nothing on mainnet for a site to read, and writes nothing"
fi
# A commit with one: HEAD's tree, with an example mainnet entry in LIVE.json and its record beside testnet's.
liveblob="$(git show HEAD:deployments/LIVE.json | python3 -c '
import json, sys
d = json.load(sys.stdin)
d["deployments"].append({"label": "v3-net", "network": "mainnet", "chainId": 999, "role": "live",
  "record": "deployments/mainnet-v3-net.json", "factory": "0x00000000000000000000000000000000000000F1", "pool": None,
  "gateway": "https://pools-api.usenami.io/net", "deployer": "0x00000000000000000000000000000000000000D1",
  "keeper": "0x00000000000000000000000000000000000000E1"})
print(json.dumps(d, indent=2))' | git hash-object -w --stdin)"
recblob="$(printf '%s\n' '{"label": "v3-net", "network": "mainnet", "chain_id": 999, "status": "complete", "block": 1,
 "PoolFactory": "0x00000000000000000000000000000000000000F1", "KeyRegistry": "0x00000000000000000000000000000000000000A1",
 "deployer": "0x00000000000000000000000000000000000000D1", "platform_assets": {"BTC": 0, "ETH": 1}}' | git hash-object -w --stdin)"
GIT_INDEX_FILE="$TMP/index" git read-tree HEAD
GIT_INDEX_FILE="$TMP/index" git update-index --add --cacheinfo "100644,$liveblob,deployments/LIVE.json" \
  --cacheinfo "100644,$recblob,deployments/mainnet-v3-net.json"
netc="$(git commit-tree "$(GIT_INDEX_FILE="$TMP/index" git write-tree)" -p HEAD -m "a mainnet deployment")"
line3="$(./scripts/pack-site.sh --network mainnet "$netc" "$TMP/net" 2>&1 | tail -1)" || { echo "FAIL mainnet pack: refused: $line3"; exit 1; }
want3="files=$n match=$n differ=0 extra=0 build={\"commit\":\"$netc\",\"network\":\"mainnet\"} "
case "$line3" in "$want3"*) ;; *) echo "FAIL mainnet pack: the packer said: $line3"; exit 1 ;; esac
python3 scripts/app_config.py --network mainnet --commit "$netc" > "$TMP/net-config.js"
cmp -s "$TMP/net-config.js" "$TMP/net/config.js" || { echo "FAIL mainnet pack: its config.js is not the script's mainnet output"; exit 1; }
grep -q '^  chainId: 999,$' "$TMP/net/config.js" && grep -q '^  hyperliquidChain: "Mainnet",$' "$TMP/net/config.js" \
  || { echo "FAIL mainnet pack: its config.js is not mainnet's"; exit 1; }
python3 scripts/app_config.py --network mainnet --index --commit "$netc" > "$TMP/net-index.html"
cmp -s "$TMP/net-index.html" "$TMP/net/index.html" || { echo "FAIL mainnet pack: its index.html is not the script's mainnet output"; exit 1; }
if grep -qi 'testnet\|mock' "$TMP/net/index.html"; then echo "FAIL mainnet pack: its index.html still says testnet"; exit 1; fi
for f in app.js lib/hlsend.js style.css; do
  cmp -s <(git show "$netc:app/$f") "$TMP/net/$f" || { echo "FAIL mainnet pack: app/$f is not its blob"; exit 1; }
done
(cd "$TMP/net" && "${SUM[@]}" -c --status "$TMP/net.sha256") || { echo "FAIL mainnet pack: the list does not check the pack"; exit 1; }
echo "ok   packs mainnet with that commit's mainnet settings in config.js and mainnet's words in index.html, and every other file its blob"
# A mainnet site of simulation pools: HEAD's tree with an example staged simulation-pool deployment on mainnet and its
# record, shaped like deployments/testnet-rehearsal998.json. No live deployment on mainnet: the pack is the one
# factory's site, and its page has none of the other pools' lines.
simblob="$(git show HEAD:deployments/LIVE.json | python3 -c '
import json, sys
d = json.load(sys.stdin)
d["deployments"].append({"label": "sim-net", "network": "mainnet", "chainId": 999, "role": "staged",
  "record": "deployments/mainnet-sim-net.json", "factory": "0x00000000000000000000000000000000000000F5", "pool": None,
  "gateway": None, "deployer": "0x00000000000000000000000000000000000000D1", "keeper": None})
print(json.dumps(d, indent=2))' | git hash-object -w --stdin)"
simrec="$(printf '%s\n' '{"label": "sim-net", "network": "mainnet", "chain_id": 999, "status": "complete", "block": 1,
 "SimPoolFactory": "0x00000000000000000000000000000000000000F5", "usdc": "0xb88339CB7199b77E23DB6E890353E22632Ba630f",
 "deployer": "0x00000000000000000000000000000000000000D1", "platform_assets": {"BTC": 0, "ETH": 1, "SOL": 5}}' | git hash-object -w --stdin)"
GIT_INDEX_FILE="$TMP/simindex" git read-tree HEAD
GIT_INDEX_FILE="$TMP/simindex" git update-index --add --cacheinfo "100644,$simblob,deployments/LIVE.json" \
  --cacheinfo "100644,$simrec,deployments/mainnet-sim-net.json"
simc="$(git commit-tree "$(GIT_INDEX_FILE="$TMP/simindex" git write-tree)" -p HEAD -m "a mainnet simulation-pool deployment")"
line4="$(./scripts/pack-site.sh --network mainnet "$simc" "$TMP/sim" 2>&1 | tail -1)" || { echo "FAIL mainnet sim pack: refused: $line4"; exit 1; }
want4="files=$n match=$n differ=0 extra=0 build={\"commit\":\"$simc\",\"network\":\"mainnet\"} "
case "$line4" in "$want4"*) ;; *) echo "FAIL mainnet sim pack: the packer said: $line4"; exit 1 ;; esac
python3 scripts/app_config.py --network mainnet --commit "$simc" > "$TMP/sim-config.js"
python3 scripts/app_config.py --network mainnet --index --commit "$simc" > "$TMP/sim-index.html"
cmp -s "$TMP/sim-config.js" "$TMP/sim/config.js" || { echo "FAIL mainnet sim pack: its config.js is not the script's mainnet output"; exit 1; }
cmp -s "$TMP/sim-index.html" "$TMP/sim/index.html" || { echo "FAIL mainnet sim pack: its index.html is not the script's mainnet output"; exit 1; }
grep -q '^  chainId: 999,$' "$TMP/sim/config.js" && grep -q '^    factory: "0x00000000000000000000000000000000000000F5",$' "$TMP/sim/config.js" \
  && grep -q '^  deployments: \[$' "$TMP/sim/config.js" && ! grep -qi 'testnet' "$TMP/sim/config.js" \
  || { echo "FAIL mainnet sim pack: its config.js is not the one factory's, on mainnet"; exit 1; }
grep -q '<div class="notice">Mainnet. Real USDC. Unaudited.</div>' "$TMP/sim/index.html" \
  || { echo "FAIL mainnet sim pack: its index.html does not carry mainnet's plaque"; exit 1; }
if grep -qi 'testnet\|mock\|#/new\|#/shared\|#/economics\|#/verify\|#/evidence\|#/terms' "$TMP/sim/index.html"; then
  echo "FAIL mainnet sim pack: its index.html says testnet or links the other pools' pages"; exit 1
fi
(cd "$TMP/sim" && "${SUM[@]}" -c --status "$TMP/sim.sha256") || { echo "FAIL mainnet sim pack: the list does not check the pack"; exit 1; }
echo "ok   packs a mainnet site of simulation pools: the one factory in config.js, mainnet's page, every other file its blob"
# A commit whose page no longer reads as the script expects is not packed for mainnet: its words would be a guess.
pageblob="$(git show HEAD:app/index.html | sed 's/and mock USDC\./with mock USDC./' | git hash-object -w --stdin)"
GIT_INDEX_FILE="$TMP/simindex" git update-index --add --cacheinfo "100644,$pageblob,app/index.html"
movedc="$(git commit-tree "$(GIT_INDEX_FILE="$TMP/simindex" git write-tree)" -p HEAD -m "the notice reworded")"
if ./scripts/pack-site.sh --network mainnet "$movedc" "$TMP/moved" >/dev/null 2>&1 || [ -e "$TMP/moved" ]; then echo "FAIL mainnet sim pack: packed a page whose words the script cannot place"; exit 1; fi
./scripts/pack-site.sh "$movedc" "$TMP/moved-testnet" >/dev/null || { echo "FAIL the same commit does not pack for testnet"; exit 1; }
echo "ok   refuses mainnet for a commit whose page moved its words, writes nothing, and still packs it for testnet"
echo "test-pack-site: the packer behaves"
