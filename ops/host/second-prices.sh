#!/usr/bin/env bash
# Runs on the POOLS host as root, once: sets up where the second machine's journal of prices is written (docs/HOSTING.md,
# "The second writer of prices"). Run again, it adds only what is missing.
#
#   sudo bash /opt/colosseum-pools/current/ops/host/second-prices.sh [<the second machine's PUBLIC key file>]
#
#   1. the user colosseum-prices, in the keeper's group, with a home for its authorized_keys;
#   2. /var/lib/colosseum-keeper lets its group pass (mode 0710): that user and the index reach their own folders
#      under it and list nothing of it; every key directory under it stays 0700;
#   3. /var/lib/colosseum-keeper/second-prices on a file system of its own, 2 GB in one file: what the second machine's
#      key writes can fill that and nothing else, never the disk of the keeper's state and journal. Named in fstab
#      with `nofail`: a broken image is a folder not mounted, never a host that does not start;
#   4. given the second machine's public key file: that key alone in the user's authorized_keys, held to
#      `rrsync -wo -munge -no-del` into that one folder, and only while the folder is mounted: it writes there, deletes
#      nothing, follows no link out of it and does nothing else on this host. The file and its .ssh folder are root's:
#      the key's user cannot write the line that holds it.
#
# It makes no key and reads no private one; the public key is the second machine's own, made there
# (ops/host/second-writer/install.sh says how).
set -euo pipefail

# Plain variables, not settings: the test stand (ops/tests/test_mainnet_host.py) sets them after sourcing this file, to
# a directory of its own; the `*_own` ones are option strings, empty on the stand, where nobody is root.
keeper_home=/var/lib/colosseum-keeper
folder=/var/lib/colosseum-keeper/second-prices
image=/var/lib/colosseum-second-prices.img
image_size=2G
fstab=/etc/fstab
prices_user=colosseum-prices
prices_home=/var/lib/colosseum-prices-home
keeper_group=colosseum-keeper
folder_own="colosseum-prices:colosseum-keeper"
# authorized_keys and its .ssh folder: root's, 0644 and 0755 (sshd's StrictModes takes that), never the key's user's.
ssh_owner=root:root
# rrsync (rsync's own, 3.2.4 or later) as the key's one command: -wo writes and sends nothing back, -munge makes every
# symbolic link it is sent point nowhere (a link to authorized_keys would be a way out of the folder), -no-del refuses
# rsync's --delete: the key adds the journal's days and never erases what is there.
rrsync_cmd="rrsync -wo -munge -no-del"
host_key=/etc/ssh/ssh_host_ed25519_key.pub
# One public key on one line, and nothing before it: an option before a key is a command the key would run.
key_line='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/=]+( [A-Za-z0-9@._-]+)?$'

say() { printf 'second-prices: %s\n' "$*"; }
fail() { printf 'second-prices: %s\n' "$*" >&2; exit 1; }

# The key file is read before anything is made.
prices_key() {
  key="${1:-}"
  [ $# -le 1 ] || { echo "second-prices: usage: second-prices.sh [<the second machine's public key file>]" >&2; exit 2; }
  [ -z "$key" ] && return 0
  [ -f "$key" ] || { echo "second-prices: no such file: $key" >&2; exit 2; }
  [ "$(grep -c . "$key")" = 1 ] && grep -Eq "$key_line" "$key" \
    || { echo "second-prices: $key is not one public key on one line: the .pub file ssh-keygen made beside the key, never the key itself" >&2; exit 2; }
}

prices_user_and_pass() {
  id "$keeper_group" >/dev/null 2>&1 || fail "no user $keeper_group on this host: ops/host/bootstrap.sh comes first"
  command -v rrsync >/dev/null 2>&1 \
    || fail "rrsync is not on this host: it is what holds the second machine's key to one folder; install it (it comes with rsync, or as a package of its own) and run this again"
  grep -q -- '-munge' "$(command -v rrsync)" && grep -q -- '-no-del' "$(command -v rrsync)" \
    || fail "rrsync on this host takes no -munge or -no-del (rsync before 3.2.4): without them the key could erase the journal and write out of its folder by a link; install a newer rsync and run this again"
  if id "$prices_user" >/dev/null 2>&1; then
    say "user $prices_user exists"
  else
    # A shell, because sshd runs the key's one command through it; the key can run nothing else (`restrict`).
    useradd --system --create-home --home-dir "$prices_home" --shell /bin/sh -g "$keeper_group" "$prices_user"
    say "user $prices_user created, in the group $keeper_group"
  fi
  chmod 0710 "$keeper_home"
  say "$keeper_home lets its group pass (0710)"
}

# The folder on a file system of its own. One that is there and mounted is left as it is.
prices_folder() {
  if mountpoint -q "$folder"; then
    say "$folder is a file system of its own already"
  else
    if [ -d "$folder" ] && [ -n "$(ls -A "$folder")" ]; then
      fail "$folder holds files and is no file system of its own: mounting one over it would hide them. Move them away first"
    fi
    if [ ! -f "$image" ]; then
      fallocate -l "$image_size" "$image.next"
      mkfs.ext4 -q -m 0 "$image.next"
      mv "$image.next" "$image"
      say "made $image, $image_size"
    fi
    mkdir -p "$folder"
    grep -qF "$image $folder " "$fstab" \
      || printf '%s\n' "$image $folder ext4 loop,nodev,nosuid,noexec,nofail 0 2" >> "$fstab"
    mount "$folder"
    mountpoint -q "$folder" || fail "$folder is still no file system of its own after mount: nothing more is done"
    say "$folder is mounted from $image"
  fi
  chown "$folder_own" "$folder"
  chmod 2750 "$folder"
}

# The second machine's key, held to writing into the one folder.
prices_authorize() {
  if [ -z "$key" ]; then
    say "no key given yet: the second machine's PUBLIC key file, made there (ops/host/second-writer/install.sh says how),"
    say "  then: sudo bash /opt/colosseum-pools/current/ops/host/second-prices.sh <that .pub file>"
    return 0
  fi
  # The folder root's first, then nothing in it is the user's: a file it left there is not written through.
  install -d -m 0755 "$prices_home/.ssh"
  chown "$ssh_owner" "$prices_home/.ssh"
  chmod 0755 "$prices_home/.ssh"
  rm -f "$prices_home/.ssh/authorized_keys.next"
  # Written aside and put in place whole: the one line is the restriction and the key, or the file is not there. The
  # command runs rrsync only while the folder is its own file system: not mounted, the key writes nowhere, never into
  # the bare folder on the root disk.
  (umask 022 && printf 'command="mountpoint -q %s && exec %s %s",restrict %s\n' "$folder" "$rrsync_cmd" "$folder" "$(cat "$key")" \
    > "$prices_home/.ssh/authorized_keys.next")
  chown "$ssh_owner" "$prices_home/.ssh/authorized_keys.next"
  chmod 0644 "$prices_home/.ssh/authorized_keys.next"
  mv -f "$prices_home/.ssh/authorized_keys.next" "$prices_home/.ssh/authorized_keys"
  say "the key of $key may write into $folder while it is mounted and do nothing else here ($rrsync_cmd, restrict)"
  if [ -f "$host_key" ] && command -v ssh-keygen >/dev/null 2>&1; then
    say "this host's own key, to hold the second machine's known_hosts against: $(ssh-keygen -lf "$host_key")"
  fi
  say "from the second machine, as its $prices_user, this must be refused by rrsync and not answered:"
  say "  ssh -i /var/lib/colosseum-prices/.ssh/sync -o BatchMode=yes $prices_user@<this host> id"
}

second_prices() {
  prices_key "$@"
  prices_user_and_pass
  prices_folder
  prices_authorize
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  second_prices "$@"
fi
