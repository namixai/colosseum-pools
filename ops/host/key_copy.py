"""The agent keys an instance's keeper makes for the key binder, copied into its gateway's key directory.

On testnet, before the enclave, the key binder's publisher is a key in a file and the keeper carries its
signatures in (ops/key_relay.py): it makes each agent key itself, as the keeper's user, into an owner-only
directory, /var/lib/colosseum-keeper/instances/<name>/made-keys. The gateway that signs with those keys runs
as another user and reads its own owner-only directory, /var/lib/colosseum-gw/instances/<name>/keys. Neither
user can read the other's, and neither directory is opened to a group: this copies each new key across,
owned by the gateway's user, mode 600.

    python -I ops/host/key_copy.py <instance>

colosseum-key-copy@<name>.path runs it each time the keeper's directory changes, as root, in a unit that sees
those two directories alone (ops/host/colosseum-key-copy@.service). A file is copied only if it is a regular
file of the keeper's user, owner-only, named agent-<address>.key, and holds the key of that address; a key the
gateway already holds under that name is left as it is, and one that differs is never overwritten. Nothing
is removed: the keeper keeps its copy for a renewal it reveals later. The output names addresses, never a key.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import sys

from eth_account import Account

INSTANCE = re.compile(r"[a-z][a-z0-9]{0,15}")  # as ops/host_instances.py names one
NAME = re.compile(r"agent-(0x[0-9a-fA-F]{40})\.key")  # as ops/key_relay.py writes one
MAX_BYTES = 200  # a key is 64 hex digits, with 0x and a line end at most 67
KEEPER_USER = "colosseum-keeper"
GATEWAY_USER = "colosseum-gw"


def made_keys(instance: str) -> str:
    return f"/var/lib/colosseum-keeper/instances/{instance}/made-keys"


def gateway_keys(instance: str) -> str:
    return f"/var/lib/colosseum-gw/instances/{instance}/keys"


def say(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def open_dir(path: str, uid: int) -> int:
    """A directory that is no link, is `uid`'s and is open to nobody else; its descriptor."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    st = os.fstat(fd)
    if st.st_uid != uid or st.st_mode & 0o077:
        os.close(fd)
        raise SystemExit(f"{path} must be its user's alone (uid {uid}, mode 700), not uid {st.st_uid} "
                         f"mode {oct(stat.S_IMODE(st.st_mode))}")
    return fd


def read_key(dir_fd: int, name: str, uid: int) -> str | None:
    """The key in `name` if the file may be copied: a regular file, no link, `uid`'s, owner-only, small, and
    the key of the address its name says. None, and a line saying why, otherwise."""
    m = NAME.fullmatch(name)
    if not m:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        say("key_skipped", file=name, why=exc.strerror or "cannot open")
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077 or st.st_size > MAX_BYTES:
            say("key_skipped", file=name, why="not a regular owner-only file of the keeper's user")
            return None
        text = os.read(fd, MAX_BYTES + 1).decode("ascii", "replace").strip()
    finally:
        os.close(fd)
    try:
        address = Account.from_key(text).address
    except ValueError:
        say("key_skipped", file=name, why="not a key")
        return None
    if address.lower() != m.group(1).lower():
        say("key_skipped", file=name, why="the key is not the address its name says")
        return None
    return text


def held(dir_fd: int, name: str) -> str | None:
    """What the gateway's directory holds under `name`, None when nothing; "" for something that is not a file
    to read, a link say, which is then left as it is."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""  # a directory or anything else that is not a file: left as it is, never read
        return os.read(fd, MAX_BYTES + 1).decode("ascii", "replace").strip()
    except OSError:
        return ""
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """All of `data`, however few bytes one write takes: a key cut short must never get its name."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def write_key(dir_fd: int, name: str, key: str, uid: int, gid: int) -> bool:
    """The key under `name`, owned by `uid`:`gid`, mode 600, whole before it has its name. False if the name
    was taken meanwhile: it is never replaced."""
    tmp = f".{name}.tmp"
    try:
        os.unlink(tmp, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    # From here the temporary file is removed however this ends: a key left under no name is still a key.
    try:
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o600)
            write_all(fd, (key + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.fsync(dir_fd)


def copy(source: str, target: str, keeper_uid: int, gateway_uid: int, gateway_gid: int) -> int:
    """Every new key from `source` into `target`; how many were copied."""
    src = open_dir(source, keeper_uid)
    try:
        dst = open_dir(target, gateway_uid)
        try:
            copied = 0
            for name in sorted(os.listdir(src)):
                key = read_key(src, name, keeper_uid)
                if key is None:
                    continue
                address = NAME.fullmatch(name).group(1)
                there = held(dst, name)
                if there is not None:
                    if there != key:
                        say("key_differs", address=address, why="the gateway holds another key under this name")
                    continue
                if write_key(dst, name, key, gateway_uid, gateway_gid):
                    say("key_copied", address=address)
                    copied += 1
            return copied
        finally:
            os.close(dst)
    finally:
        os.close(src)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not INSTANCE.fullmatch(argv[0]):
        print("usage: key_copy.py <instance>: a-z then up to 15 of a-z0-9", file=sys.stderr)
        return 2
    keeper, gateway = pwd.getpwnam(KEEPER_USER), pwd.getpwnam(GATEWAY_USER)
    copy(made_keys(argv[0]), gateway_keys(argv[0]), keeper.pw_uid, gateway.pw_uid, gateway.pw_gid)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
