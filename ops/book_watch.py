"""The stop book's own watch: a process of its own, on another machine and another node, that sees whether the book's
blocks are being checked, raises an alarm when they are not, and with a key of its own checks them itself.

    spike/.venv/bin/python -m ops.book_watch --deployment sim1 --rpc <its own node>               # watches, sends nothing
    spike/.venv/bin/python -m ops.book_watch --deployment sim1 --rpc <its own node> --wallet watcher   # and checks

A stop the price touched is carried out only if `poke()` logged that block (docs/SIM-POOL.md, "The stops"), and the
keeper's loop (ops/book_keeper.py) is what sends it every block. This watch shares nothing with that loop: not its state,
not its node, not its key, not its machine when it runs on another one. Every `--poll` seconds it reads the head of its
own node (with a backup, `--backup-rpc`), fresh by its block's time, and on each new head the book's `watched()` and
`lastPokeBlock()`:

  the book behind  → more than `--lag-alarm` blocks between `lastPokeBlock()` and the head while the book watches an asset:
                     an alarm (`book_poke_lagging`), again every 30 seconds while it lasts;
  its own node     → a head that cannot be read, or whose block is old on every node: an alarm (`book_head_unread`,
                     `book_head_stale`). The alarm goes out on this machine, so it does not depend on the node that failed;
  with a key       → while the book is `--takeover` blocks behind or more (2 by default), this watch sends `poke()` itself
                     (anyone may), every block for `--hold` blocks after the last time it was behind, and says so
                     (`book_poke_taken_over`). Its checks are sent as the keeper's are: a fixed gas, signed again with a
                     quarter more fee when late, an alarm when its key's money pays fewer than `--runway-hours` hours.

An alarm is written as the keeper's loop writes one: an `ALARM` line in the journal, the line added to a flag file
(`--alarm`), and the line given to COLOSSEUM_ALERT_CMD when the host has a channel. Its node's address is never written:
a key may ride in its path.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

from ops import book_keeper as bk
from ops import keeper as core
from ops import sim_keeper as sk
from ops import stop_filler as sf
from spike.hlspike import common as c

log = core.log

# Seconds between two reads of the book's read gas (`oracleGas`, `bboGas`), for the gas of its checks.
READ_GAS_EVERY_S = 60.0
# Seconds between two alarms of a watch that cannot start (systemd starts it again every ten seconds).
START_ALARM_EVERY_S = 300.0


class Watch:
    """The watch's checks' gas: the book's read gas, read at the start and again every `READ_GAS_EVERY_S` (a
    `ReadGasSet` is seen within a minute); after a check reverted, a quarter more for `bk.FLOOR_BLOCKS` blocks, and the
    read gas read again at once."""

    def __init__(self, nodes, book: str):
        self.nodes = nodes
        self.book = book
        self.read_gas = 2 * bk.PRECOMPILE_GAS
        self.read_at = -1e18
        self.floor = 0
        self.floor_until = 0
        self.poker = None

    def gas(self, watched: int) -> int:
        if time.monotonic() - self.read_at >= READ_GAS_EVERY_S:
            self.read_at = time.monotonic()
            self.read_gas = bk.precompile_gas_of(self.nodes, self.book)
        head = self.poker.head if self.poker is not None else 0
        if self.floor and head > self.floor_until:
            self.floor = 0
        return max(self.floor, bk.poke_gas(max(watched, 1), self.read_gas))

    def reverted(self, head: int) -> None:
        self.read_at = -1e18
        needed = bk.poke_gas(1, self.read_gas)
        self.floor = min(bk.MAX_TX_GAS, max(self.floor, needed) * 5 // 4)
        self.floor_until = head + bk.FLOOR_BLOCKS


def build(args, record: dict, wallet=None, alarm=None, nodes=None) -> bk.Poker:
    """The watch from its settings: its own nodes checked against the network, the factory's book, its key's lane."""
    if nodes is None:
        backup = None
        if args.backup_rpc and args.backup_rpc.rstrip("/") != args.rpc.rstrip("/"):
            backup = bk.checked_node(args.backup_rpc, bk.POKE_TIMEOUT_S)
        nodes = bk.Nodes(bk.checked_node(args.rpc, bk.POKE_TIMEOUT_S), backup, stale_s=args.stale_s)
    factory = record["SimPoolFactory"]
    book = bk.book_of(record, factory, call=nodes.call)
    if book is None:
        raise SystemExit(f"deployment {args.deployment!r}: the factory has no stop book; nothing to watch")
    flag = pathlib.Path(args.alarm) if args.alarm else sk.STATE_DIR / f"book-watch-{args.deployment}.alarm"
    alarm = alarm or sf.Alarm(flag)
    lanes = [bk.Lane(args.wallet, bk.poke_sender(wallet, nodes))] if wallet is not None else []
    watch = Watch(nodes, book)
    watch.gas(1)  # the book's read gas read now, not before the first check
    poker = bk.Poker(book, nodes, lanes, alarm, gas=watch.gas, takeover=args.takeover, hold=args.hold,
                     lag_alarm=args.lag_alarm, tip_wei=int(round(args.tip_gwei * sf.GWEI)),
                     runway_hours=args.runway_hours, role="watch", on_revert=watch.reverted)
    watch.poker = poker
    return poker


def parser() -> argparse.ArgumentParser:
    from ops import sim_network as sn

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/ with a SimPoolFactory")
    p.add_argument("--rpc", default=os.environ.get("COLOSSEUM_BOOK_WATCH_RPC_URL", ""),
                   help="this watch's own node (COLOSSEUM_BOOK_WATCH_RPC_URL): not the keeper's")
    p.add_argument("--backup-rpc", default=os.environ.get("COLOSSEUM_BOOK_WATCH_BACKUP_RPC_URL", ""),
                   help="the node a call goes to when the first fails, or answers an old block")
    p.add_argument("--wallet", default=os.environ.get("COLOSSEUM_BOOK_WATCH_WALLET", "") or None,
                   help="a key of its own in the key directory: with it the watch checks the blocks the keeper leaves "
                        "unchecked; without it, it sends nothing")
    p.add_argument("--takeover", type=int, default=2, help="blocks behind from which this watch checks itself")
    p.add_argument("--hold", type=int, default=600,
                   help="blocks this watch goes on checking after the last time the book was behind")
    p.add_argument("--lag-alarm", type=int, default=bk.LAG_ALARM_BLOCKS,
                   help="blocks behind past which it is an alarm")
    p.add_argument("--poll", type=float, default=0.25, help="seconds between looks at the head")
    p.add_argument("--stale-s", type=float, default=bk.STALE_S,
                   help="a node whose newest block is older than this many seconds has stopped")
    p.add_argument("--runway-hours", type=float, default=bk.RUNWAY_HOURS,
                   help="with --wallet: a key whose money pays fewer hours of checks than this is an alarm")
    p.add_argument("--tip-gwei", type=float, default=0.0, help="with --wallet: the priority fee of a check, in gwei")
    p.add_argument("--alarm", default=None,
                   help="the flag file of its alarms (default ops/state/book-watch-<deployment>.alarm)")
    sn.add_args(p, service=True)
    return p


def main(argv: list[str] | None = None) -> int:
    from ops import deployments
    from ops import sim_network as sn

    args = parser().parse_args(argv)
    net = sn.selected(args, c.NETWORK)
    if not args.rpc:
        raise SystemExit("refusing to run: --rpc (COLOSSEUM_BOOK_WATCH_RPC_URL) names this watch's own node")
    if args.takeover < 1 or args.lag_alarm < 1 or args.tip_gwei < 0:
        raise SystemExit("--takeover and --lag-alarm must be at least 1, --tip-gwei not negative")
    if args.wallet and net.name == "mainnet":
        # A key that sends on mainnet: the same switches as a service of the keeper's (sim_network.confirm_service),
        # its chain checked on its own node below rather than on COLOSSEUM_RPC_URL.
        if getattr(args, "confirm_mainnet", None) != args.deployment:
            raise SystemExit(f"refusing to send on mainnet: --confirm-mainnet {args.deployment} is needed, "
                             "the deployment's label")
        sn.key_dir_ok(net)
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    flag = pathlib.Path(args.alarm) if args.alarm else sk.STATE_DIR / f"book-watch-{args.deployment}.alarm"
    alarm = sf.Alarm(flag)
    try:
        wallet = c.account(args.wallet) if args.wallet else None
        poker = build(args, record, wallet, alarm=alarm)
    except Exception as exc:
        # Its node down at the start, its key unreadable, the book not read: an alarm before it ends (systemd starts it
        # again), at most once every START_ALARM_EVERY_S, not a silent restart every ten seconds.
        start_failed(alarm, flag, exc)
        raise
    poker.run(args.poll)
    return 0


def start_failed(alarm, flag: pathlib.Path, exc: Exception) -> None:
    mark = flag.with_name(flag.name + ".start-failed")
    try:
        last = mark.stat().st_mtime
    except OSError:
        last = 0.0
    if time.time() - last < START_ALARM_EVERY_S:
        log("book_watch_cannot_start", error=sf.host_free(str(exc))[:200], note="its alarm was raised already")
        return
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(str(int(time.time())))
    except OSError:
        pass
    alarm("book_watch_cannot_start", error=sf.host_free(str(exc))[:200],
          why="the book's own watch cannot start: nothing watches the book's checks from here")
    alarm.drain(30.0)


if __name__ == "__main__":
    sys.exit(main())
