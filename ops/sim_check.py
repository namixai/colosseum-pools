"""The checker of simulated pools: the open script behind every stop of a payout (docs/SIM-POOL.md).

    spike/.venv/bin/python -m ops.sim_check --deployment sim1 --once            # what it finds, sends nothing
    spike/.venv/bin/python -m ops.sim_check --deployment sim1 --wallet checker  # stops what it finds, a service

A payout waits PAYOUT_DELAY (72 hours) from its claim. For each one still waiting, this reads every fill of every
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
only while it waits and only once. It never lifts a stop: lifting is the factory's operator's, our Safe
(`deploy_sim lift`), not the checker's.

Until when it stops is the pool's own rule. A pool with payout approval takes a stop until the payout
is paid, approved or not: an unapproved one waits up to 7 days from its claim, and a pair found on the
fourth day still stops it. A pool from before approvals pays at 72 hours by itself, and a stop is sent
no later than 5 minutes before that. Either way only for a pair (reasons 1 and 2): a result disputed
without a pair (reason 3) and prohibited play (reason 4, a series of exams traded at the edge of their risk, or a
hedge between accounts) need a written analysis and are never sent from here.
`--wallet` is the factory's checker; without it, or with --dry-run, nothing is sent.

As a service (no --once; on the host colosseum-sim-checker@<name>, docs/HOSTING.md) it runs the same pass every
--every seconds, 600 by default, and writes one JSON line a pass (`check_done`) and one an action: `sent` with the
pool, the payout's id, the reason and the transaction's hash, `would_send` the same without a key, `send_failed` with
the refusal. Before it sends it reads the payout again, at the head as it is then, and sends only if it can still be
stopped; a payout it stopped, or would have, it does not take up again while it runs, and the pool takes one stop a
payout anyway. A pass the node fails is logged (`pass_failed`) and the next one comes on time. A read the node breaks
off (the fills of the accounts, or one trader's transfers) is dropped whole and read again from the same block in the
next pass, so nothing is counted twice; what was read whole before the failure is kept. A node whose head is behind
what was read already is read nothing from. Its calls start --rpc-gap seconds apart at least, 1 by default, to leave
the node's allowance to the keeper.
Testnet unless both COLOSSEUM_NETWORK=mainnet and --network mainnet name mainnet; on mainnet it sends only with
--confirm-mainnet <the deployment's label> and a mainnet key directory (ops/sim_network.py).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from ops import sim_keeper as sk
from ops import sim_network as sn
from ops.contract_errors import refusal
from spike.hlspike import common as c

log = core.log
view = core.view
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
STOP = "stopPayout(uint256,uint8,address,bytes32[],bytes32[])"
STOP_TYPES = ["uint256", "uint8", "address", "bytes32[]", "bytes32[]"]
# On a pool from before approvals a stop is sent no later than this before its payout is due: a block may
# take that long.
MARGIN_S = 300
# `payout(id)` of a pool from before approvals: the same fields without the eighth, `approved`.
PAYOUT_BEFORE_APPROVALS = "(address,address,uint64,uint64,uint8,bool,uint64)"
ADDRESS_BATCH = 100
# The service's pace: a pass every this many seconds, and its node reads at least this far apart. A payout waits 72
# hours from its claim, and a pair is there from the claim on: ten minutes late is nothing for a stop, and a
# restarted service finds what it missed in its first pass. The gap holds the checker to about 60 reads a minute
# while it reads, on a node the keeper (its fills, every block) shares.
DEFAULT_EVERY_S = 600
RPC_GAP_S = 1.0


# A URL in an error's text (requests writes the node's whole address, path and key with it), and urllib3's path after
# "url: ": neither reaches a log line or a reason (review 9 of #336).
_URL_REST = r"[^\s'\"<>()]*"  # what goes on in the same address: its path, its key
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>()]+")
_HOST = re.compile(r"(host=['\"])[^'\"]*(['\"])")
_URL_PATH = re.compile(r"(url: ?)/[^\s'\"<>()]*")


def quiet(exc, limit: int | None = 200) -> str:
    """An exception's text with no node address in it, cut to `limit` (None: not cut) after the address is gone: a
    node's URL may hold its key. The configured nodes' addresses are taken out first, the longest first, each with
    whatever goes on after it in the same address (an address that begins with a configured one and goes on into a
    path would leave that end, its key, behind), then any other URL, then a bare path."""
    text = str(exc)
    urls = [u for u in (getattr(c, "RPC_URL", None), os.environ.get("COLOSSEUM_RPC_URL"),
                        os.environ.get("COLOSSEUM_RPC_BACKUP_URL")) if u]
    for url in sorted(set(urls), key=len, reverse=True):
        text = re.sub(re.escape(url) + _URL_REST, "<node>", text)
    text = _URL_PATH.sub(r"\1<path>", _URL.sub("<node>", text))
    # A key may be the node's host name itself (KEY.provider.example): urllib3 writes it as host='…' and in its
    # resolver's words; the configured nodes' host names go wherever they stand (review 12 of #336).
    text = _HOST.sub(r"\1<host>\2", text)
    for url in urls:
        host = urllib.parse.urlparse(url).hostname
        if host and host not in ("127.0.0.1", "localhost"):
            text = re.sub(re.escape(host), "<host>", text, flags=re.I)
    return text[:limit]


@contextlib.contextmanager
def node_errors():
    """A failure of the node ends the program in one line with no node address in it, never a traceback that prints
    the address (and its key) with the exception."""
    try:
        yield
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"refusing to go on: the node failed: {quiet(exc)}") from None


def paced(rpc, gap_s: float, clock=time.monotonic, sleep=time.sleep):
    """`rpc` with every call started at least `gap_s` after the last one ended. One process, one node: the pace holds
    across every call of the process (views and sends go through `rpc` too). A call `rpc` retries on a 429 is spaced by
    its own ladder instead (1, 2, 4 ... 32 seconds), longer than the default gap from its first step on."""
    last: list[float | None] = [None]

    def call(method, params=()):
        if last[0] is not None:
            wait = last[0] + gap_s - clock()
            if wait > 0:
                sleep(wait)
        try:
            return rpc(method, params)
        finally:
            last[0] = clock()

    return call


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


def stop_due(state: str, was_stopped: bool, due_at: int, now: int, until_paid: bool = False) -> bool:
    """Whether a payout can still be stopped: waiting, never stopped, and in time by its pool's rule. A pool with
    payout approval takes a stop until the payout is paid (`until_paid`: SimPool.stopPayout asks no time); one
    from before approvals pays at `due_at` by itself, so the stop goes MARGIN_S before it."""
    return state == "Due" and not was_stopped and (until_paid or now + MARGIN_S < due_at)


def takes_approvals(pool: str) -> bool | None:
    """Whether the pool has payout approval: it answers `approver()`. False where the call reverts (a pool from
    before approvals has no such getter); None where it was not read (the node, the rate), never taken for "no"."""
    try:
        c.call_view(pool, "approver()", [], [], ["address"])
        return True
    except Exception as exc:
        if "revert" in str(exc).lower():
            return False
        log("not_read", address=pool, call="approver()", error=quiet(exc, 120))
        return None


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
        # Stages that can no longer be paid or stopped, and each stage's start: what `keep_from` reads.
        self.settled: set[str] = set()
        self.started: dict[str, int] = {}
        # Each pool's kind, read once: with payout approval (a stop until the payout is paid) or from before it.
        self.approvals: dict[str, bool] = {}
        # The payouts this process stopped, or would have without a key: never taken up again while it runs. A
        # stop that failed is not here, and is tried again in the next pass.
        self.stopped: set[tuple[str, int]] = set()

    def until_paid(self, pool: str) -> bool:
        """Whether this pool takes a stop until the payout is paid. Not read, it counts as yes for this pass and is
        asked again in the next: a stop the pool refuses is a logged revert, a stop not sent is a payout gone."""
        if pool not in self.approvals:
            got = takes_approvals(pool)
            if got is None:
                return True
            self.approvals[pool] = got
        return self.approvals[pool]

    def payout(self, pool: str, pid: int) -> tuple:
        """`payout(id)` as this pool holds it: a pool from before approvals answers a word shorter."""
        kind = sk.PAYOUT if self.until_paid(pool) else PAYOUT_BEFORE_APPROVALS
        return view(pool, "payout(uint256)", kind, ["uint256"], [pid])

    def time_of(self, block: int) -> int:
        if block not in self._times:
            self._times[block] = int(c.rpc("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)
        return self._times[block]

    def read_fills(self, latest: int) -> None:
        """Every fill of the factory's accounts up to `latest`. The keeper's scan finds accounts a few windows at a
        time, so an account it finds later has fills from before the cursor: it is read from the deployment's
        block, once, and then with the others."""
        if latest < self.fills_from - 1:
            # A node behind the head already read (another node behind a balancer, or one catching up): nothing is read
            # from it, and the cursor stays where it is; moved back, it would read kept fills a second time.
            log("node_behind", head=latest, read_to=self.fills_from - 1)
            return
        accounts = list(self.keeper.accounts)
        new = [a for a in accounts if a not in self.fills_read]
        # Kept only once both reads are whole: a read the node breaks off is read again from the same place in the
        # next pass, and a fill kept from the broken one would be counted twice.
        got = self._read_fills([a for a in accounts if a in self.fills_read], self.fills_from, latest)
        got += self._read_fills(new, self.start_block, latest)
        self.fills += got
        self.fills_read.update(new)
        self.fills_from = latest + 1

    def _read_fills(self, accounts: list[str], lo: int, latest: int) -> list[dict]:
        got = []
        while accounts and lo <= latest:
            hi = min(lo + core.MAX_LOG_WINDOW - 1, latest)
            for i in range(0, len(accounts), ADDRESS_BATCH):
                for l in c.rpc("eth_getLogs", [{"address": accounts[i:i + ADDRESS_BATCH], "topics": [sk.FILLED],
                                                "fromBlock": hex(lo), "toBlock": hex(hi)}]):
                    got.append(fill_of(l, self.time_of(int(l["blockNumber"], 16))))
            lo = hi + 1
        return got

    def transfers_to(self, traders: list[str], latest: int) -> list[dict]:
        """USDC sent to each trader since the deployment (before it is not read: docs/SIM-POOL.md),
        each trader's read once and then from where it stopped."""
        for t in traders:
            lo = self.transfers_from.setdefault(t, self.start_block)
            got = []  # kept once the trader's read is whole, as the fills are
            word = "0x" + "00" * 12 + t[2:].lower()
            while lo <= latest:
                hi = min(lo + core.MAX_LOG_WINDOW - 1, latest)
                for l in c.rpc("eth_getLogs", [{"address": self.usdc, "topics": [TRANSFER, None, word],
                                                "fromBlock": hex(lo), "toBlock": hex(hi)}]):
                    got.append({"from": to_checksum_address("0x" + l["topics"][1][-40:]),
                                "to": to_checksum_address("0x" + l["topics"][2][-40:]), "tx": l["transactionHash"]})
                lo = hi + 1
            self.transfers.setdefault(t, []).extend(got)
            self.transfers_from[t] = lo
        return [x for t in traders for x in self.transfers[t]]

    def keep_from(self, now: int) -> int:
        """The oldest time a fill can still matter from: the start of every stage that can still be paid and stopped
        (running, passed and not yet claimed, or with its payout still stoppable), less the pairing window. A fixed
        age would drop the early fills of a stage whose claim came late, say while the keeper was down (review of
        #273): a stage's fills reach back its whole length before the pass, and the payout waits from the claim."""
        stoppable = set()
        for pool in self.keeper.pools:
            head, count = view(pool, "head()", "uint256"), view(pool, "payoutCount()", "uint256")
            for pid in range(head, count):
                p = self.payout(pool, pid)
                if stop_due(sk.PAYOUT_STATE[p[4]], p[5], p[3], now, self.until_paid(pool)):
                    stoppable.add(to_checksum_address(p[0]))
        starts = []
        for a, info in self.keeper.accounts.items():
            if info["kind"] != "stage" or a in self.settled:
                continue
            if a not in stoppable and c.call_view(info["pool"], "accountOf(address)", ["address"], [a],
                                                  ["uint8", "uint8", "bool"])[2]:
                self.settled.add(a)  # released, or claimed with its payout past stopping: never again
                continue
            if a not in self.started:
                self.started[a] = view(a, "stepStartedAt()", "uint64")
            starts.append(self.started[a])
        return (min(starts) if starts else now) - self.window_s

    def prune(self, cutoff: int) -> None:
        """Drops the fills and block times from before `cutoff` (review of cp#264)."""
        self.fills = [f for f in self.fills if f["t"] >= cutoff]
        self._times = {b: t for b, t in self._times.items() if t >= cutoff}

    def check(self, pool: str, pid: int, now: int, latest: int) -> tuple | None:
        p = self.payout(pool, pid)
        account, trader = to_checksum_address(p[0]), to_checksum_address(p[1])
        if not stop_due(sk.PAYOUT_STATE[p[4]], p[5], p[3], now, self.until_paid(pool)):
            return None
        mine = [f for f in self.fills if f["account"] == account]
        found = pairs(mine, self.fills, self.window_s, self.tolerance)
        if not found:
            return None
        paired = finding(found, [])[1]
        other_trader = to_checksum_address(c.call_view(paired, "result()", [], [], ["uint8", "address", "uint64", "uint64"])[1])
        # The factory's pools pay every trader: a pool is no link between two it paid (review of #283).
        link_txs = links([t for t in self.transfers_to([trader, other_trader], latest) if t["from"] not in self.keeper.pools],
                         trader, other_trader)
        return finding(found, link_txs)

    def one_pass(self) -> list[dict]:
        latest = int(c.rpc("eth_blockNumber"), 16)
        now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
        self.keeper.scan(latest)
        self.read_fills(latest)
        # Nothing is dropped while the scan is behind: an account it has not found yet may be a stage whose
        # pairs are among the fills already read, and those are not read again (review of #273).
        if self.keeper.next_block > latest:
            self.prune(self.keep_from(now))
        stops = []
        for pool in self.keeper.pools:
            head, count = view(pool, "head()", "uint256"), view(pool, "payoutCount()", "uint256")
            for pid in range(head, count):
                if (pool, pid) in self.stopped:
                    continue
                f = self.check(pool, pid, now, latest)
                if f is None:
                    continue
                reason, paired, trades, link_txs = f
                stop = {"pool": pool, "id": pid, "reason": reason, "paired": paired, "trades": trades, "links": link_txs}
                log("pair_found", **stop)
                if not self.still_due(pool, pid):
                    continue
                stops.append(stop)
                if self.send(pool, [pid, reason, paired, [bytes.fromhex(t[2:]) for t in trades],
                                    [bytes.fromhex(t[2:]) for t in link_txs]], STOP, STOP_TYPES,
                             id=pid, reason=reason, paired=paired) or self.dry:
                    self.stopped.add((pool, pid))
        log("check_done", pools=len(self.keeper.pools), fills=len(self.fills), stops=len(stops))
        return stops

    def still_due(self, pool: str, pid: int) -> bool:
        """The payout read again right before its stop, at the head's time as it is now: a pass reads for minutes,
        and in them the payout may have been paid, stopped by another hand, or come within MARGIN_S of its time."""
        now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
        p = self.payout(pool, pid)
        if stop_due(sk.PAYOUT_STATE[p[4]], p[5], p[3], now, self.until_paid(pool)):
            return True
        log("stop_not_sent", pool=pool, id=pid, state=sk.PAYOUT_STATE[p[4]], was_stopped=bool(p[5]))
        return False

    def send(self, to: str, args: list, sig: str, types: list, **fields) -> bool:
        if self.dry:
            log("would_send", to=to, call=sig.split("(")[0], **fields)
            return False
        try:
            rcpt = c.transact(self.wallet, to, sig, types, args)
            log("sent", to=to, call=sig.split("(")[0], **fields, tx=rcpt["transactionHash"])
            return True
        except Exception as exc:
            # The address goes out of the whole text first: `refusal` cuts it to 200, and a host name cut in two
            # is no longer known for one.
            log("send_failed", to=to, call=sig.split("(")[0], **fields, error=refusal(Exception(quiet(exc, None))))
            return False


def run(ch: Checker, every: float, once: bool, sleep=time.sleep) -> int:
    """The service: a pass, then `every` seconds, for ever. A pass that fails (the node, mostly) is logged and the next
    one comes on time; the process never ends on it. --once: one pass, its stops printed, 1 if it failed."""
    while True:
        try:
            stops = ch.one_pass()
            if once:
                print(json.dumps(stops))
        except Exception as exc:
            log("pass_failed", error=quiet(exc, 200))
            if once:
                return 1
        if once:
            return 0
        sleep(every)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True)
    p.add_argument("--wallet", help="the factory's checker; without it nothing is sent")
    p.add_argument("--once", action="store_true")
    p.add_argument("--every", type=int, default=DEFAULT_EVERY_S, help="seconds between passes")
    p.add_argument("--rpc-gap", type=float, default=RPC_GAP_S, help="seconds between two reads of the node, at least")
    p.add_argument("--window", type=int, default=600, help="seconds two opposite fills may be apart")
    p.add_argument("--size-tolerance", type=float, default=0.2)
    p.add_argument("--state", help="the scan's state (default ops/state/check-<deployment>.json)")
    p.add_argument("--dry-run", action="store_true")
    sn.add_args(p, service=True)
    args = p.parse_args(argv)
    dry = args.dry_run or not args.wallet
    with node_errors():
        sn.confirm_service(args, args.deployment, c, sends=not dry)
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    wallet = None if dry else c.account(args.wallet)
    c.rpc = paced(c.rpc, max(0.0, args.rpc_gap))
    sender = wallet.address if wallet else record.get("checker", core.ZERO)
    state = pathlib.Path(args.state) if args.state else sk.STATE_DIR / f"check-{args.deployment}.json"
    ch = Checker(record, wallet, sender, dry, state, args.window, args.size_tolerance)
    if not args.once:
        log("checker_started", deployment=args.deployment, sends=not dry, sender=sender, every=args.every,
            rpc_gap=args.rpc_gap)
    return run(ch, args.every, args.once)


if __name__ == "__main__":
    sys.exit(main())
