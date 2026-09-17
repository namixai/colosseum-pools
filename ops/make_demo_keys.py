"""Makes the demo's agent keys: ordinary testnet keys for the pool gateway's demo signer.

    spike/.venv/bin/python ops/make_demo_keys.py --out <new directory> --count 20

Writes `demo-agent-NN.key` files (mode 600) into a new directory (mode 700), and
`addresses.txt` beside them, and prints only the addresses, one per line, ready for
`ops/deploy_testnet.py --keys-file`. The key files go to the gateway host; they never enter
git, a report or a log. Testnet only: these keys trade demo accounts holding mock USDC.
"""

from __future__ import annotations

import argparse
import os
import pathlib
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
    addresses = []
    for i in range(1, count + 1):
        account = Account.from_key(secrets.token_bytes(32))
        fd = os.open(out / f"demo-agent-{i:02d}.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("0x" + bytes(account.key).hex() + "\n")
        addresses.append(account.address)
    (out / "addresses.txt").write_text("\n".join(addresses) + "\n")
    return addresses


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="a directory that doesn't exist yet")
    p.add_argument("--count", type=int, default=20)
    args = p.parse_args(argv)
    print("\n".join(make_keys(pathlib.Path(args.out).expanduser(), args.count)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
