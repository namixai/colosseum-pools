#!/usr/bin/env bash
# The packer still does what it says: every file of app/ equal to its blob, build.json with the commit and the network
# and nothing else beside them, and the sha256 list of all of it beside the folder. Packs this checkout's
# HEAD into a directory of its own and removes it.
set -euo pipefail
cd "$(dirname "$0")/.."
TMP="$(mktemp -d -t packsite.XXXXXX)"; trap 'rm -rf "$TMP"' EXIT
HEAD="$(git rev-parse HEAD)"
n="$(git ls-tree -r HEAD -- app/ | wc -l | tr -d ' ')"
line="$(./scripts/pack-site.sh HEAD "$TMP/out" | tail -1)"
want="files=$n match=$n differ=0 extra=0 build={\"commit\":\"$HEAD\",\"network\":\"testnet\"} "
case "$line" in
  "$want"*) echo "ok   packed $n files and build.json for $HEAD" ;;
  *) echo "FAIL the packer said: $line"; echo "     expected it to start: $want"; exit 1 ;;
esac
[ "$(cat "$TMP/out/build.json")" = "{\"commit\":\"$HEAD\",\"network\":\"testnet\"}" ] || { echo "FAIL build.json: a testnet pack names its commit and testnet"; exit 1; }
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
# A commit with a live PoolFactory deployment on mainnet: HEAD's tree, with an example mainnet entry in LIVE.json and
# its record beside testnet's. The examples stand beside testnet's entries only: a mainnet deployment that is really
# listed is left out of them.
liveblob="$(git show HEAD:deployments/LIVE.json | python3 -c '
import json, sys
d = json.load(sys.stdin)
d["deployments"] = [e for e in d["deployments"] if e["network"] == "testnet"]
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
# A live deployment on mainnet gets no pack: a mainnet site is its simulation pools' alone, and settings written from
# a live deployment would name no factory to hold a pool's address to.
if ./scripts/pack-site.sh --network mainnet "$netc" "$TMP/net" >/dev/null 2>&1 || [ -e "$TMP/net" ] || [ -e "$TMP/net.sha256" ]; then
  echo "FAIL mainnet live: packed a mainnet site for a live deployment, with no factory of simulation pools in its settings"; exit 1
fi
./scripts/pack-site.sh "$netc" "$TMP/net-testnet" >/dev/null || { echo "FAIL mainnet live: the same commit does not pack for testnet"; exit 1; }
echo "ok   refuses mainnet for a commit with a live deployment on mainnet, writes nothing, and still packs it for testnet"
# A mainnet site of simulation pools: HEAD's tree with an example staged simulation-pool deployment on mainnet and its
# record, shaped like deployments/testnet-rehearsal998.json. No live deployment on mainnet: the pack is the one
# factory's site, and its page has none of the other pools' lines.
simblob="$(git show HEAD:deployments/LIVE.json | python3 -c '
import json, sys
d = json.load(sys.stdin)
d["deployments"] = [e for e in d["deployments"] if e["network"] == "testnet"]
d["deployments"].append({"label": "sim-net", "network": "mainnet", "chainId": 999, "role": "staged",
  "record": "deployments/mainnet-sim-net.json", "factory": "0x00000000000000000000000000000000000000F5", "pool": None,
  "gateway": None, "deployer": "0x00000000000000000000000000000000000000D1", "keeper": None})
print(json.dumps(d, indent=2))' | git hash-object -w --stdin)"
simrec="$(printf '%s\n' '{"label": "sim-net", "network": "mainnet", "chain_id": 999, "status": "complete", "block": 1,
 "SimPoolFactory": "0x00000000000000000000000000000000000000F5", "usdc": "0xb88339CB7199b77E23DB6E890353E22632Ba630f",
 "deployer": "0x00000000000000000000000000000000000000D1", "platform_assets": {"BTC": 0, "ETH": 1, "SOL": 5}}' | git hash-object -w --stdin)"
# Its Terms of Use with every "[TO CONFIRM: ...]" place filled in, as they will be before the site goes up: a commit
# whose Terms or page still hold one is not packed for mainnet (the cases further down).
termsblob="$(git show HEAD:app/data/terms-mainnet.md | sed 's/\[TO CONFIRM: [^]]*\]/Filled in for the test./g' | git hash-object -w --stdin)"
git cat-file blob "$termsblob" | grep -qF '[TO CONFIRM' && { echo "FAIL mainnet terms: the test's Terms still hold a [TO CONFIRM place"; exit 1; }
GIT_INDEX_FILE="$TMP/simindex" git read-tree HEAD
GIT_INDEX_FILE="$TMP/simindex" git update-index --add --cacheinfo "100644,$simblob,deployments/LIVE.json" \
  --cacheinfo "100644,$simrec,deployments/mainnet-sim-net.json" --cacheinfo "100644,$termsblob,app/data/terms-mainnet.md"
simc="$(git commit-tree "$(GIT_INDEX_FILE="$TMP/simindex" git write-tree)" -p HEAD -m "a mainnet simulation-pool deployment")"
line4="$(./scripts/pack-site.sh --network mainnet "$simc" "$TMP/sim" 2>&1 | tail -1)" || { echo "FAIL mainnet pack: refused: $line4"; exit 1; }
want4="files=$n match=$n differ=0 extra=0 build={\"commit\":\"$simc\",\"network\":\"mainnet\"} "
case "$line4" in "$want4"*) ;; *) echo "FAIL mainnet pack: the packer said: $line4"; exit 1 ;; esac
python3 scripts/app_config.py --network mainnet --commit "$simc" > "$TMP/sim-config.js"
python3 scripts/app_config.py --network mainnet --index --commit "$simc" > "$TMP/sim-index.html"
cmp -s "$TMP/sim-config.js" "$TMP/sim/config.js" || { echo "FAIL mainnet pack: its config.js is not the script's mainnet output"; exit 1; }
cmp -s "$TMP/sim-index.html" "$TMP/sim/index.html" || { echo "FAIL mainnet pack: its index.html is not the script's mainnet output"; exit 1; }
grep -q '^  chainId: 999,$' "$TMP/sim/config.js" && grep -q '^    factory: "0x00000000000000000000000000000000000000F5",$' "$TMP/sim/config.js" \
  && grep -q '^  deployments: \[$' "$TMP/sim/config.js" && ! grep -qi 'testnet' "$TMP/sim/config.js" \
  || { echo "FAIL mainnet pack: its config.js is not the one factory's, on mainnet"; exit 1; }
grep -q '<div class="notice">Mainnet. Real USDC. Unaudited.</div>' "$TMP/sim/index.html" \
  || { echo "FAIL mainnet pack: its index.html does not carry mainnet's plaque"; exit 1; }
if grep -qi 'testnet\|mock\|#/new\|#/shared\|#/economics\|#/verify\|#/evidence\|#/terms' "$TMP/sim/index.html"; then
  echo "FAIL mainnet pack: its index.html says testnet or links the other pools' pages"; exit 1
fi
(cd "$TMP/sim" && "${SUM[@]}" -c --status "$TMP/sim.sha256") || { echo "FAIL mainnet pack: the list does not check the pack"; exit 1; }
echo "ok   packs a mainnet site of simulation pools: the one factory in config.js, mainnet's page, every other file its blob"
# With no folder named, a mainnet pack goes to a folder that says mainnet, and testnet's keeps the name it had: the two
# packs of one commit are not taken for each other at the upload.
HOME="$TMP/home" ./scripts/pack-site.sh --network mainnet "$simc" >/dev/null 2>&1 \
  || { echo "FAIL mainnet pack: not packed into a default folder"; exit 1; }
HOME="$TMP/home" ./scripts/pack-site.sh "$simc" >/dev/null 2>&1 \
  || { echo "FAIL mainnet pack: testnet's pack of the same commit found its default folder taken"; exit 1; }
[ -f "$TMP/home/Downloads/pools-app-mainnet-${simc:0:8}/build.json" ] && [ -f "$TMP/home/Downloads/pools-app-mainnet-${simc:0:8}.sha256" ] \
  && grep -q '"network":"mainnet"' "$TMP/home/Downloads/pools-app-mainnet-${simc:0:8}/build.json" \
  || { echo "FAIL mainnet pack: its default folder does not say mainnet"; exit 1; }
[ "$(cat "$TMP/home/Downloads/pools-app-${simc:0:8}/build.json")" = "{\"commit\":\"$simc\",\"network\":\"testnet\"}" ] \
  || { echo "FAIL the default folder of a testnet pack is not pools-app-<first 8>"; exit 1; }
echo "ok   names a mainnet pack's default folder for mainnet, and testnet's as before"
# The same commit with the Terms of Use as committed, ten places still marked "[TO CONFIRM": no mainnet pack, nothing
# written, and the places named; testnet packs it as before. Then the Terms filled in and a page's text with a place
# open (its description, from app_config.py): refused the same way.
GIT_INDEX_FILE="$TMP/openindex" git read-tree "$simc"
GIT_INDEX_FILE="$TMP/openindex" git update-index --add --cacheinfo "100644,$(git rev-parse HEAD:app/data/terms-mainnet.md),app/data/terms-mainnet.md"
openc="$(git commit-tree "$(GIT_INDEX_FILE="$TMP/openindex" git write-tree)" -p HEAD -m "the Terms of Use not filled in")"
if said="$(./scripts/pack-site.sh --network mainnet "$openc" "$TMP/open" 2>&1)" || [ -e "$TMP/open" ] || [ -e "$TMP/open.sha256" ]; then
  echo "FAIL mainnet terms: packed a mainnet site whose Terms of Use hold [TO CONFIRM places"; exit 1
fi
case "$said" in *"[TO CONFIRM places still open in $openc"*) ;; *) echo "FAIL mainnet terms: refused for another reason: $said"; exit 1 ;; esac
[ "$(printf '%s\n' "$said" | grep -c '^  app/data/terms-mainnet.md:[0-9]*:.*\[TO CONFIRM')" -eq "$(git show HEAD:app/data/terms-mainnet.md | grep -c '\[TO CONFIRM')" ] \
  || { echo "FAIL mainnet terms: the refusal does not name every open place: $said"; exit 1; }
./scripts/pack-site.sh "$openc" "$TMP/open-testnet" >/dev/null || { echo "FAIL mainnet terms: the same commit does not pack for testnet"; exit 1; }
cfgblob="$(git show HEAD:scripts/app_config.py | sed 's/No real money is traded in the exam\./No real money is traded in the exam. [TO CONFIRM: wording]/' | git hash-object -w --stdin)"
git cat-file blob "$cfgblob" | grep -qF '[TO CONFIRM: wording]' || { echo "FAIL mainnet terms: the test found no description to open a place in"; exit 1; }
GIT_INDEX_FILE="$TMP/metaindex" git read-tree "$simc"
GIT_INDEX_FILE="$TMP/metaindex" git update-index --add --cacheinfo "100644,$cfgblob,scripts/app_config.py"
metac="$(git commit-tree "$(GIT_INDEX_FILE="$TMP/metaindex" git write-tree)" -p HEAD -m "a page text not filled in")"
if said="$(./scripts/pack-site.sh --network mainnet "$metac" "$TMP/meta" 2>&1)" || [ -e "$TMP/meta" ] || [ -e "$TMP/meta.sha256" ]; then
  echo "FAIL mainnet terms: packed a mainnet site whose page's texts hold a [TO CONFIRM place"; exit 1
fi
case "$said" in *"  index.html:"*"[TO CONFIRM: wording]"*) ;; *) echo "FAIL mainnet terms: refused for another reason: $said"; exit 1 ;; esac
echo "ok   refuses mainnet while its Terms of Use or its page's texts hold [TO CONFIRM places, writes nothing, and still packs it for testnet"
# A commit whose page no longer reads as the script expects is not packed for mainnet: its words would be a guess.
pageblob="$(git show HEAD:app/index.html | sed 's/and mock USDC\./with mock USDC./' | git hash-object -w --stdin)"
GIT_INDEX_FILE="$TMP/simindex" git update-index --add --cacheinfo "100644,$pageblob,app/index.html"
movedc="$(git commit-tree "$(GIT_INDEX_FILE="$TMP/simindex" git write-tree)" -p HEAD -m "the notice reworded")"
if ./scripts/pack-site.sh --network mainnet "$movedc" "$TMP/moved" >/dev/null 2>&1 || [ -e "$TMP/moved" ]; then echo "FAIL mainnet pack: packed a page whose words the script cannot place"; exit 1; fi
./scripts/pack-site.sh "$movedc" "$TMP/moved-testnet" >/dev/null || { echo "FAIL the same commit does not pack for testnet"; exit 1; }
echo "ok   refuses mainnet for a commit whose page moved its words, writes nothing, and still packs it for testnet"
echo "test-pack-site: the packer behaves"
