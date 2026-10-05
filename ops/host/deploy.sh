#!/usr/bin/env bash
# Puts one commit of this repository on the pools host and switches to it. Testnet only.
#
#   COLOSSEUM_SSH_KEY=<key file> ops/host/deploy.sh <commit> <user@host>
#
# The commit's tracked files travel as a git archive, so nothing untracked (keys, venvs, a
# local deployment record) goes with them; then ops/host/install.sh from that archive
# finishes the release on the host (docs/HOSTING.md). Without COLOSSEUM_SSH_KEY, ssh uses its
# own configuration. The host's address stays out of this repository.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
usage="usage: ops/host/deploy.sh <commit> <user@host>"
sha="$(git rev-parse --verify "${1:?$usage}^{commit}")"
target="${2:?$usage}"
ssh=(ssh -o BatchMode=yes)
if [ -n "${COLOSSEUM_SSH_KEY:-}" ]; then
  ssh+=(-i "$COLOSSEUM_SSH_KEY")
fi
ssh+=("$target")

# Only what is on origin/main goes to the host. This used to say so and carry on, which left the
# stopping to whoever was reading the screen.
if git merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
  echo "deploy: $sha is on origin/main"
else
  echo "deploy: $sha is NOT on origin/main; nothing was sent. Fetch, or merge it first." >&2
  exit 1
fi

upload="/opt/colosseum-pools/releases/.$sha.upload"
git archive --format=tar "$sha" |
  "${ssh[@]}" "sudo rm -rf $upload && sudo install -d -m 0755 $upload && sudo tar -x --no-same-owner -C $upload"
"${ssh[@]}" -n "sudo bash $upload/ops/host/install.sh $sha"
