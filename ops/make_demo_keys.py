"""Makes the demo's agent keys: ordinary testnet keys for the pool gateway's demo signer.

    spike/.venv/bin/python ops/make_demo_keys.py --out <new directory> --count 20

Writes `demo-agent-NN.key` files (mode 600) into a new directory (mode 700), and
`addresses.txt` beside them, and prints only the addresses, one per line, ready for
`ops/deploy_testnet.py --keys-file`. The key files go to the gateway host; they never enter
git, a report or a log. Testnet only: these keys trade demo accounts holding mock USDC.

    spike/.venv/bin/python ops/make_demo_keys.py --wallet keeper --dir <owner-only directory>

makes one named wallet instead, `<name>.key` (mode 600) and `<name>.addr`, the layout the
spike tools and the keeper read from COLOSSEUM_KEY_DIR, and prints its address. It never
replaces a key that is there.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import secrets
import sys

from eth_account import Account

MAX_COUNT = 100


def make_keys(out: pathlib.Path, count: int) -> list[str]:
    if not 1 <= count <= MAX_COUNT:
        raise SystemExit(f"--count must be 1..{MAX_COUNT}")
    if out.exists():
        raise SystemExit(f"{out} exists; keys go into a new directory")
    out.mkdir(parents=True)
    os.chmod(out, 0o700)  # mkdir's mode is cut by the umask
    addresses = [_new_key(out / f"demo-agent-{i:02d}.key") for i in range(1, count + 1)]
    (out / "addresses.txt").write_text("\n".join(addresses) + "\n")
    return addresses


def make_wallet(directory: pathlib.Path, name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
        raise SystemExit(f"{name!r}: a wallet name is lower-case letters, digits and dashes")
    if directory.stat().st_mode & 0o077:
        raise SystemExit(f"{directory} is open to others; chmod 700 it")
    address = _new_key(directory / f"{name}.key")
    (directory / f"{name}.addr").write_text(address + "\n")
    return address


def _new_key(path: pathlib.Path) -> str:
    """A fresh key into a new owner-only file; its address."""
    account = Account.from_key(secrets.token_bytes(32))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f"{path} exists; a key is never replaced") from None
    with os.fdopen(fd, "w") as fh:
        fh.write("0x" + bytes(account.key).hex() + "\n")
    return account.address


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", help="a directory that doesn't exist yet")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--wallet", help="instead of agent keys: one named wallet key, e.g. keeper")
    p.add_argument("--dir", help="with --wallet: the owner-only directory it goes into")
    args = p.parse_args(argv)
    if args.wallet:
        if args.out or not args.dir:
            p.error("--wallet goes with --dir, not with --out")
        print(make_wallet(pathlib.Path(args.dir).expanduser(), args.wallet))
    else:
        if not args.out or args.dir:
            p.error("agent keys need --out, a directory that doesn't exist yet")
        print("\n".join(make_keys(pathlib.Path(args.out).expanduser(), args.count)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
