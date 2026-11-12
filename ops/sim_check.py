"""The checker of simulated pools: the open script behind every stop of a payout (docs/SIM-POOL.md).

    spike/.venv/bin/python -m ops.sim_check --deployment sim1 --once            # what it finds, sends nothing
    spike/.venv/bin/python -m ops.sim_check --deployment sim1 --wallet checker  # stops what it finds, a service

A payout waits PAYOUT_DELAY (48 hours). For each one still waiting, this reads every fill of every
exam and stage of the factory's pools and asks one question: did the paid stage hold a position
while another account of the platform held the opposite one, opened close in time and close in
size? That is the pair: two accounts, a long and a short, one of them passes on the other's loss.

  reason 1  → a fill of the paid stage and a fill of another account: the same asset, opposite
              sides, at most --window seconds apart, sizes within --size-tolerance of each other.
              `trades` names both fills' transactions; `paired` the other account.
  reason 2  → that, and the two traders' wallets linked: USDC sent to both from one address
              (`links` names those transfers). A link alone is no reason: a wallet an exchange
              pays out of funds thousands of strangers.

Copying a signal is no reason: the same side on two accounts is not a pair. Breaking a stage's
rules is no reason either: that stage ends at once and never pays.

Everything it reads is on chain, and so is everything it writes: the reason, the other account,
the hashes. Anyone can run this script on the same blocks and get the same pairs. It stops a payout
only while it waits and only once; it lifts its own stop with --lift ID when a stop was wrong.
`--wallet` is the factory's checker; without it, or with --dry-run, nothing is sent. Testnet only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from ops import sim_keeper as sk
from ops.contract_errors import refusal
from spike.hlspike import common as c

log = core.log
view = core.view
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
STOP = "stopPayout(uint256,uint8,address,bytes32[],bytes32[])"
STOP_TYPES = ["uint256", "uint8", "address", "bytes32[]", "bytes32[]"]
# A stop is sent no later than this before its payout is due: a block may take that long.
MARGIN_S = 300
# Fills older than this can pair with no payout still waiting: 48 hours of delay, the window, a margin.
KEEP_S = 72 * 3600
ADDRESS_BATCH = 100


# ── what is a pair, from what was read ───────────────────────────────────────────────

def pairs(paid: list[dict], others: list[dict], window_s: int, tolerance: float) -> list[tuple[dict, dict]]:
    """Every fill of the paid account matched with a fill of another account on the same asset, the
    other side, within `window_s` seconds and sizes within `tolerance` (0.2: the smaller at least 80 %
    of the larger). A fill is {account, asset, buy, sz, t, tx}."""
    out = []
    for a in paid:
        for b in others:
            if b["account"] == a["account"] or b["asset"] != a["asset"] or b["buy"] == a["buy"]:
                continue
            if abs(b["t"] - a["t"]) > window_s:
                continue
            if min(a["sz"], b["sz"]) < (1 - tolerance) * max(a["sz"], b["sz"]):
                continue
            out.append((a, b))
    return out


def links(transfers: list[dict], trader_a: str, trader_b: str) -> list[str]:
    """The transactions of USDC sent to both traders from one address: the first to each, per sender.
    A transfer is {from, to, tx}. Empty when nothing links them, or when they are one wallet."""
    if trader_a == trader_b:
        return []
    to_a: dict[str, str] = {}
    to_b: dict[str, str] = {}
    for t in transfers:  # in the chain's order: the first from each sender is kept (review of cp#264)
        if t["to"] == trader_a:
            to_a.setdefault(t["from"], t["tx"])
        if t["to"] == trader_b:
            to_b.setdefault(t["from"], t["tx"])
    out = []
    for sender in sorted(set(to_a) & set(to_b)):
        if sender in (trader_a, trader_b):
            continue  # one paying the other is a link too, but it is counted below
        out += [to_a[sender], to_b[sender]]
    direct = [t["tx"] for t in transfers if {t["from"], t["to"]} == {trader_a, trader_b}]
    return out + direct


def finding(pair_list: list[tuple[dict, dict]], link_txs: list[str]) -> tuple[int, str, list[str], list[str]] | None:
    """What a stop writes: the reason, the other account (the one with the most matched fills), the
    hashes of the pair's fills, and the links. None without a pair."""
    if not pair_list:
        return None
    counts: dict[str, int] = {}
    for _, b in pair_list:
        counts[b["account"]] = counts.get(b["account"], 0) + 1
    paired = max(sorted(counts), key=lambda acc: counts[acc])
    trades: list[str] = []
    for a, b in pair_list:
        if b["account"] == paired:
            for tx in (a["tx"], b["tx"]):
                if tx not in trades:
                    trades.append(tx)
    return (2 if link_txs else 1), paired, trades, link_txs


def stop_due(state: str, was_stopped: bool, due_at: int, now: int) -> bool:
    """Whether a payout can still be stopped: waiting, never stopped, and far enough from its time."""
    return state == "Due" and not was_stopped and now + MARGIN_S < due_at


def fill_of(log_entry: dict, t: int) -> dict:
    is_buy, sz, _, _, _, _ = abi_decode(["bool", "uint64", "uint64", "uint256", "uint256", "uint8"],
                                        bytes.fromhex(log_entry["data"][2:]))
    return {"account": to_checksum_address(log_entry["address"]), "asset": int(log_entry["topics"][1], 16),
            "buy": bool(is_buy), "sz": int(sz), "t": t, "tx": log_entry["transactionHash"]}


# ── the checker ──────────────────────────────────────────────────────────────────────

class Checker:
    def __init__(self, record: dict, wallet, sender: str, dry: bool, state_path: pathlib.Path,
                 window_s: int = 600, tolerance: float = 0.2):
        self.factory = to_checksum_address(record["SimPoolFactory"])
        self.usdc = to_checksum_address(record["usdc"])
        self.wallet = wallet
        self.sender = to_checksum_address(sender)
        self.dry = dry
        self.window_s = window_s
        self.tolerance = tolerance
        self.keeper = sk.SimKeeper(self.factory, None, sender, True, state_path, int(record["block"]))
        self.start_block = int(record["block"])
        self.fills_from = self.start_block
        # The accounts whose fills are read up to `fills_from`; one found later is read from the start (review of #271).
        self.fills_read: set[str] = set()
        self.fills: list[dict] = []
        self._times: dict[int, int] = {}
        # USDC transfers to each trader read so far, and the block to read on from (review of cp#264).
        self.transfers: dict[str, list[dict]] = {}
        self.transfers_from: dict[str, int] = {}

    def time_of(self, block: int) -> int:
        if block not in self._times:
            self._times[block] = int(c.rpc("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)
        return self._times[block]

    def read_fills(self, latest: int) -> None:
        """Every fill of the factory's accounts up to `latest`. The keeper's scan finds accounts a few windows at a
        time, so an account it finds later has fills from before the cursor: it is read from the deployment's
        block, once, and then with the others."""
        accounts = list(self.keeper.accounts)
        new = [a for a in accounts if a not in self.fills_read]
        self._read_fills([a for a in accounts if a in self.fills_read], self.fills_from, latest)
        self._read_fills(new, self.start_block, latest)
        self.fills_read.update(new)
        self.fills_from = latest + 1

    def _read_fills(self, accounts: list[str], lo: int, latest: int) -> None:
        while accounts and lo <= latest:
            hi = min(lo + core.MAX_LOG_WINDOW - 1, latest)
            for i in range(0, len(accounts), ADDRESS_BATCH):
                for l in c.rpc("eth_getLogs", [{"address": accounts[i:i + ADDRESS_BATCH], "topics": [sk.FILLED],
                                                "fromBlock": hex(lo), "toBlock": hex(hi)}]):
                    self.fills.append(fill_of(l, self.time_of(int(l["blockNumber"], 16))))
            lo = hi + 1

    def transfers_to(self, traders: list[str], latest: int) -> list[dict]:
        """USDC sent to each trader since the deployment (before it is not read: docs/SIM-POOL.md),
        each trader's read once and then from where it stopped."""
        for t in traders:
            lo = self.transfers_from.setdefault(t, self.start_block)
            got = self.transfers.setdefault(t, [])
            word = "0x" + "00" * 12 + t[2:].lower()
            while lo <= latest:
                hi = min(lo + core.MAX_LOG_WINDOW - 1, latest)
                for l in c.rpc("eth_getLogs", [{"address": self.usdc, "topics": [TRANSFER, None, word],
                                                "fromBlock": hex(lo), "toBlock": hex(hi)}]):
                    got.append({"from": to_checksum_address("0x" + l["topics"][1][-40:]),
                                "to": to_checksum_address("0x" + l["topics"][2][-40:]), "tx": l["transactionHash"]})
                lo = hi + 1
            self.transfers_from[t] = lo
        return [x for t in traders for x in self.transfers[t]]

    def prune(self, now: int) -> None:
        """Drops the fills and block times no payout still waiting can need (review of cp#264)."""
        cutoff = now - KEEP_S
        self.fills = [f for f in self.fills if f["t"] >= cutoff]
        self._times = {b: t for b, t in self._times.items() if t >= cutoff}

    def check(self, pool: str, pid: int, now: int, latest: int) -> tuple | None:
        p = view(pool, "payout(uint256)", sk.PAYOUT, ["uint256"], [pid])
        account, trader = to_checksum_address(p[0]), to_checksum_address(p[1])
        if not stop_due(sk.PAYOUT_STATE[p[4]], p[5], p[3], now):
            return None
        mine = [f for f in self.fills if f["account"] == account]
        found = pairs(mine, self.fills, self.window_s, self.tolerance)
        if not found:
            return None
        paired = finding(found, [])[1]
        other_trader = to_checksum_address(c.call_view(paired, "result()", [], [], ["uint8", "address", "uint64", "uint64"])[1])
        link_txs = links(self.transfers_to([trader, other_trader], latest), trader, other_trader)
        return finding(found, link_txs)

    def one_pass(self) -> list[dict]:
        latest = int(c.rpc("eth_blockNumber"), 16)
        now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
        self.keeper.scan(latest)
        self.read_fills(latest)
        self.prune(now)
        stops = []
        for pool in self.keeper.pools:
            head, count = view(pool, "head()", "uint256"), view(pool, "payoutCount()", "uint256")
            for pid in range(head, count):
                f = self.check(pool, pid, now, latest)
                if f is None:
                    continue
                reason, paired, trades, link_txs = f
                stop = {"pool": pool, "id": pid, "reason": reason, "paired": paired, "trades": trades, "links": link_txs}
                log("pair_found", **stop)
                stops.append(stop)
                self.send(pool, [pid, reason, paired, [bytes.fromhex(t[2:]) for t in trades],
                                 [bytes.fromhex(t[2:]) for t in link_txs]], STOP, STOP_TYPES)
        log("check_done", pools=len(self.keeper.pools), fills=len(self.fills), stops=len(stops))
        return stops

    def send(self, to: str, args: list, sig: str, types: list) -> bool:
        if self.dry:
            log("would_send", to=to, call=sig.split("(")[0])
            return False
        try:
            rcpt = c.transact(self.wallet, to, sig, types, args)
            log("sent", to=to, call=sig.split("(")[0], tx=rcpt["transactionHash"])
            return True
        except Exception as exc:
            log("send_failed", to=to, call=sig.split("(")[0], error=refusal(exc))
            return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True)
    p.add_argument("--wallet", help="the factory's checker; without it nothing is sent")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=3_600, help="seconds between passes")
    p.add_argument("--window", type=int, default=600, help="seconds two opposite fills may be apart")
    p.add_argument("--size-tolerance", type=float, default=0.2)
    p.add_argument("--lift", metavar="POOL:ID", help="lift the checker's own stop of payout ID of POOL")
    p.add_argument("--state", help="the scan's state (default ops/state/check-<deployment>.json)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    dry = args.dry_run or not args.wallet
    wallet = None if dry else c.account(args.wallet)
    sender = wallet.address if wallet else record.get("checker", core.ZERO)
    state = pathlib.Path(args.state) if args.state else sk.STATE_DIR / f"check-{args.deployment}.json"
    ch = Checker(record, wallet, sender, dry, state, args.window, args.size_tolerance)
    if args.lift:
        pool, pid = args.lift.split(":")
        return 0 if ch.send(to_checksum_address(pool), [int(pid)], "liftStop(uint256)", ["uint256"]) else 1
    while True:
        try:
            print(json.dumps(ch.one_pass()))
        except Exception as exc:
            log("pass_failed", error=str(exc)[:200])
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
