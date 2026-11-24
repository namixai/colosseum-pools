#!/usr/bin/env bash
# Puts one commit of this repository on the pools host and switches to it. The host's network is the host's own
# setting, /etc/colosseum/host.env, read there by install.sh: nothing here names one (docs/HOSTING.md).
#
#   COLOSSEUM_SSH_KEY=<key file> ops/host/deploy.sh <commit> <user@host>
#   COLOSSEUM_SSH_KEY=<key file> ops/host/deploy.sh <commit> <user@second machine> second-writer <name> <label> <network>
#
# The commit's tracked files travel as a git archive, so nothing untracked (keys, venvs, a
# local deployment record) goes with them; then ops/host/install.sh from that archive
# finishes the release on the host (docs/HOSTING.md). Without COLOSSEUM_SSH_KEY, ssh uses its
# own configuration. The host's address stays out of this repository.
#
# With `second-writer` the machine is the second one, which writes the second journal of prices and reads the pools
# host's pulse: the same archive, finished there by ops/host/second-writer/install.sh for the instance <name> of the
# deployment <label> on <network> (docs/HOSTING.md, "The second writer of prices").
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
usage="usage: ops/host/deploy.sh <commit> <user@host> [second-writer <name> <label> <testnet|mainnet>]"
sha="$(git rev-parse --verify "${1:?$usage}^{commit}")"
target="${2:?$usage}"
installer="ops/host/install.sh $sha"
if [ $# -gt 2 ]; then
  # What follows goes into the command the second machine runs: held here to the shapes its installer takes, so
  # that nothing in it is anything a shell reads otherwise.
  [ "$3" = second-writer ] && [ $# -eq 6 ] && [[ "$4" =~ ^[a-z][a-z0-9]{0,15}$ ]] && [[ "$5" =~ ^[a-z0-9-]+$ ]] \
    && { [ "$6" = testnet ] || [ "$6" = mainnet ]; } || { echo "$usage" >&2; exit 2; }
  installer="ops/host/second-writer/install.sh $sha $4 $5 $6"
fi
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
"${ssh[@]}" -n "sudo bash $upload/$installer"
