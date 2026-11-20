"""The stops and takes of the paper exams, looked at in every block with nothing in the way, and the watch that says at
once when one was reached and not carried out (docs/SIM-POOL.md, "The stops").

    spike/.venv/bin/python -m ops.sim_keeper --deployment sim1                      # the keeper; this loop runs inside it
    spike/.venv/bin/python -m ops.stop_filler --deployment sim1                     # the watch alone: no key, sends nothing

A stop exists for the chain only in a block whose transaction carries it out: `fillTrigger` takes it while the oracle
price of that very block is at the stop or past it, and inside its window only from the factory's filler. A reach that
no transaction of the filler's lands in leaves no trace. So the loop that carries stops out must look at every block and
must not wait for anything else. Before this, the keeper's one loop did, in turn: a slow pass every 30 seconds with no
look at the stops while it ran; each fill and each stop waiting for its own receipt, the others queued behind it; a node's
rate limit holding the whole process for up to 63 seconds; and a stop set while the keeper was behind its logs unknown
until the slow pass. This loop runs in a thread of its own, beside the keeper's:

  every block      → one `eth_getLogs` of the blocks since its last look (the stops and takes set, cleared and filled,
                     the fills that move a position, the exams sold and ended), one read of the oracle price for each
                     asset with a stop or a take standing, and the stops whose price has reached them compared here, not
                     tried one by one with an `eth_call`;
  a stop reached   → `fillTrigger` signed with a nonce kept here and sent at once, with a fixed gas, no reads before it
                     and no wait for its receipt: the receipt is looked at in the next blocks, beside the other work. A
                     stop the exam refused once is tried with an `eth_call` first the next time (the book past the
                     ceiling, say), as before;
  its own node     → `--stop-rpc` (COLOSSEUM_STOP_RPC_URL), the keeper's node when not set. Every call is tried once:
                     a busy node costs the loop that block, not a minute; the blocks it missed are read back the next
                     time, `BACKFILL` at most, and the ones past that are said (`stop_watch_blind`);
  a tip            → `--stop-tip-gwei` (COLOSSEUM_STOP_TIP_GWEI), zero when not set, as every send of the keeper's.

The watch runs on the same prices. For each stop or take standing, armed and with a position open, a stretch of blocks
in which the oracle stood at it or past it is a reach. A reach that ends, the price back, the stop taken off or moved by
the trader, the position closed or the exam over, with no `TriggerFilled` inside it, is an alarm (`stop_missed`); a
reach that has lasted `--stop-overdue` blocks and is not filled yet is one too (`stop_overdue`). An alarm is written as
the host's watchdogs write theirs (ops/host/watchdogs/watchdog-run.sh): an `ALARM` line in the journal, the line added
to a flag file (`--stop-alarm`, next to the keeper's state), and the line given to COLOSSEUM_ALERT_CMD when the host
has a channel. A miss does not mend itself, so its flag stays until a person removes it.

Every price read goes to a journal (`--price-journal`, next to the keeper's state): one JSON object, {"asset:block": px},
kept valid as it grows. It is the file `ops/payout_review.py --prices` takes. It is the keeper's own record, not an
independent one: check a sample of it against the node.

What this does not do: carry out a stop whose reach is shorter than the way from a block seen to a transaction landed
(about one or two blocks); carry out a stop the exam refuses (its book too far from the oracle); stop a trader taking a
stop off while it is reached; or go on while the filler's one key and its one machine are down. Those need the contract
or a second machine (the design note of 20 November 2026).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time

import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from ops import keeper as core
from ops import sim_keeper as sk
from spike.hlspike import common as c

log = core.log

# PaperExam's events this loop reads besides the keeper's; the tests hold each against the Solidity source.
STOPPED = "0x" + keccak(text="Stopped(uint8,uint8,int256)").hex()
PASSED = "0x" + keccak(text="Passed(address,int256)").hex()
ACCOUNT_TOPICS = [sk.TRIGGER_SET, sk.TRIGGER_CLEARED, sk.TRIGGER_FILLED, sk.FILLED, STOPPED, PASSED]
POOL_TOPICS = [sk.EXAM_SOLD, sk.FUNDED]
ORACLE_PX = c.PRECOMPILE["oraclePx"]
HIP3_BASE = 100_000  # CoreOps.HIP3_BASE: the oracle's index of an asset is pxIndex(asset)
STOP_GAS = sk.FILL_GAS
# How many blocks back the loop reads when the head ran ahead of it (a busy node, a slow answer). Each costs a price read
# a standing asset; past this the blocks are said as not seen, not read.
BACKFILL = 20
# Blocks a reach may last unfilled before it is an alarm while it still lasts.
OVERDUE = 5
# Blocks a stop's transaction may go without a receipt before it is taken for lost: its nonce is read again from the node.
RECEIPT_GIVE_UP = 10
GWEI = 10 ** 9


def px_index(asset: int) -> int:
    """`CoreOps.pxIndex`: the oracle's index of an exam's asset."""
    return asset - HIP3_BASE if asset >= HIP3_BASE else asset


def hit(take: bool, sign: int, oracle: int, px: int) -> bool:
    """Whether the oracle price has reached a stop or a take of a position of `sign` (`PaperExam._triggered`): a take on
    a long and a stop on a short at the price or above it, the other two at it or below. No position, no reach."""
    if sign == 0 or oracle <= 0:
        return False
    return oracle >= px if take == (sign > 0) else oracle <= px


def sign_of(position: int) -> int:
    return (position > 0) - (position < 0)


# ── a node, tried once ───────────────────────────────────────────────────────────────

class Node:
    """One JSON-RPC node, its own HTTP session (the keeper's thread has the other), every call tried once: HTTP 429 and
    the public node's -32005 raise c.NodeBusy, anything else as it comes. The loop has the next block to go to."""

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": c.USER_AGENT, "Content-Type": "application/json"})

    def call(self, method: str, params=()):
        resp = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
                                 timeout=self.timeout)
        if resp.status_code == 429:
            raise c.NodeBusy(f"{method}: HTTP 429")
        resp.raise_for_status()
        body = resp.json()
        error = body.get("error")
        if error is None:
            return body["result"]
        if isinstance(error, dict) and error.get("code") == c.RATE_LIMITED:
            raise c.NodeBusy(f"{method}: {error}")
        raise RuntimeError(f"{method}: {error}")


# ── one key's nonces, for every thread that sends with it ────────────────────────────

class Nonces:
    """The filler's nonces for the keeper's thread and this loop: each send takes the next one here, so neither reads it
    from the node before it sends and neither takes one the other has in flight. The lock is held for no network call.
    A send that failed, or one that never got a receipt, makes the next send read the node's count again; a nonce in
    flight is never handed out twice."""

    def __init__(self, address: str):
        self.address = to_checksum_address(address)
        self.lock = threading.Lock()
        self.next: int | None = None
        self.stale = True
        self.inflight: set[int] = set()

    def take(self, call) -> int:
        with self.lock:
            need = self.stale
        if need:
            counted = int(call("eth_getTransactionCount", [self.address, "pending"]), 16)
        with self.lock:
            if need and self.stale:
                self.next = max(counted, max(self.inflight, default=-1) + 1)
                self.stale = False
            nonce = self.next
            self.next += 1
            self.inflight.add(nonce)
            return nonce

    def done(self, nonce: int) -> None:
        with self.lock:
            self.inflight.discard(nonce)

    def failed(self, nonce: int | None = None) -> None:
        with self.lock:
            if nonce is not None:
                self.inflight.discard(nonce)
            self.stale = True

    def broadcast(self, wallet, call, to: str, data: bytes, gas: int, gas_price: int, tip: int = 0) -> tuple[str, int]:
        """Signs and sends one transaction; its hash and nonce. No receipt is waited for. Nothing reached the node:
        c.NotSent. A node that says the nonce is taken: read again, and tried once more."""
        for attempt in (0, 1):
            try:
                nonce = self.take(call)
            except Exception as exc:
                raise c.NotSent(str(exc)) from exc
            tx = {"chainId": c.CHAIN_ID, "nonce": nonce, "value": 0, "data": data, "type": 2, "to": to_checksum_address(to),
                  "maxPriorityFeePerGas": tip, "maxFeePerGas": gas_price * 2 + tip, "gas": gas}
            try:
                signed = wallet.sign_transaction(tx)
            except Exception as exc:
                self.failed(nonce)
                raise c.NotSent(str(exc)) from exc
            tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
            try:
                call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
            except Exception as exc:
                message = str(exc)
                if "already known" in message:
                    self.done(nonce)
                    return tx_hash, nonce
                self.failed(nonce)
                if "nonce too low" in message.lower() and attempt == 0:
                    continue
                raise
            self.done(nonce)
            return tx_hash, nonce
        raise RuntimeError("unreachable")

    def send_and_wait(self, wallet, to: str, data: bytes, gas: int | None = None, call=None) -> dict:
        """The keeper's own sends (a fill, a slow call) on the shared nonces: the gas estimated with a quarter more when not
        given, sent, and its receipt waited for, as c.send_tx does. A reverted one raises c.Reverted."""
        call = call or c.rpc
        try:
            if gas is None:
                probe = {"from": self.address, "to": to_checksum_address(to), "data": "0x" + data.hex()}
                gas = int(int(call("eth_estimateGas", [probe]), 16) * 1.25)
            gas_price = int(call("eth_gasPrice"), 16)
        except Exception as exc:
            raise c.NotSent(str(exc)) from exc
        tx_hash, _ = self.broadcast(wallet, call, to, data, gas, gas_price)
        try:
            rcpt = c.wait_receipt(tx_hash)
        except Exception:
            self.failed()
            raise
        if int(rcpt["status"], 16) != 1:
            raise c.Reverted(f"transaction {tx_hash} reverted")
        return rcpt


# ── the journal of prices ────────────────────────────────────────────────────────────

class PriceJournal:
    """The oracle prices this loop read, as one JSON object {"asset:block": px}: the file `ops/payout_review.py --prices`
    takes. Each block's prices go in at its end, before the closing brace, so the file reads as JSON between writes. A
    write cut short (the machine stopped) is mended when the journal is opened: the entry it left half is dropped."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            path.write_text("{}")
        self.empty = self._mend()

    def _mend(self) -> bool:
        """The file ends in its closing brace; whether it holds no entry."""
        with open(self.path, "r+b") as fh:
            size = fh.seek(0, os.SEEK_END)
            tail_at = max(0, size - 4096)
            fh.seek(tail_at)
            tail = fh.read()
            text = tail.rstrip()
            if text.endswith(b"}"):
                end = tail_at + len(text)
                fh.truncate(end)
                return text == b"{}" if tail_at == 0 else False
            # A write cut short: drop back to the last whole entry.
            comma = tail.rfind(b",")
            if comma >= 0:
                fh.truncate(tail_at + comma)
                fh.seek(tail_at + comma)
                fh.write(b"}")
                return False
            fh.seek(0)
            fh.truncate(0)
            fh.write(b"{}")
            return True

    def add(self, block: int, prices: dict[int, int]) -> None:
        if not prices:
            return
        body = ", ".join(f'"{asset}:{block}": {int(px)}' for asset, px in sorted(prices.items()))
        with open(self.path, "r+b") as fh:
            fh.seek(-1, os.SEEK_END)
            fh.write(((", " if not self.empty else "") + body + "}").encode())
        self.empty = False


# ── the alarm ────────────────────────────────────────────────────────────────────────

class Alarm:
    """What the host's watchdogs do with a finding (ops/host/watchdogs/watchdog-run.sh), done from a running loop: the
    line in the journal marked ALARM, added to a flag file, and handed to COLOSSEUM_ALERT_CMD when there is one. The
    channel runs in a thread of its own, so a slow one does not stop the loop. No other channel is opened here."""

    def __init__(self, flag: pathlib.Path | None, command: str | None = None):
        self.flag = flag
        self.command = os.environ.get("COLOSSEUM_ALERT_CMD", "") if command is None else command
        self.said_no_channel = False

    def __call__(self, event: str, **fields) -> None:
        line = json.dumps({"t": int(time.time()), "event": event, **fields}, default=str)
        log(event, alarm="ALARM", **fields)
        where = "the journal"
        if self.flag is not None:
            try:
                self.flag.parent.mkdir(parents=True, exist_ok=True)
                with open(self.flag, "a", encoding="utf-8") as fh:
                    fh.write(f"at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n{line}\n")
                where = "the flag and the journal"
            except OSError as exc:
                log("stop_alarm_flag_failed", flag=str(self.flag), error=str(exc)[:200])
        if self.command:
            threading.Thread(target=self._send, args=(line, where), daemon=True).start()
        elif not self.said_no_channel:
            self.said_no_channel = True
            log("stop_alarm_no_channel", why="COLOSSEUM_ALERT_CMD is not set", where=where)

    def _send(self, line: str, where: str) -> None:
        try:
            subprocess.run(["sh", "-c", self.command], input=line + "\n", text=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            log("stop_alarm_sent")
        except Exception as exc:
            log("stop_alarm_not_sent", error=str(exc)[:200], where=where)


# ── the loop ─────────────────────────────────────────────────────────────────────────

class StopLoop:
    """The stops and takes of the keeper's exams, every block (see the module's text). `keeper` is the SimKeeper whose
    pools and accounts are watched; `wallet` and `nonces` are None for a watch that sends nothing."""

    def __init__(self, keeper, node: Node, *, wallet=None, nonces: Nonces | None = None, tip_wei: int = 0,
                 journal: PriceJournal | None = None, alarm: Alarm | None = None, backfill: int = BACKFILL,
                 overdue: int = OVERDUE):
        self.keeper = keeper
        self.node = node
        self.wallet = wallet
        self.nonces = nonces
        self.tip = tip_wei
        self.journal = journal
        self.alarm = alarm or Alarm(None, "")
        self.backfill = max(1, min(backfill, core.MAX_LOG_WINDOW))
        self.overdue = max(1, overdue)
        self.block = 0  # the last block whose events and prices are in
        self.triggers: dict[tuple[str, int, bool], dict] = {}  # key -> {"px", "armed", "touch", "said", "first"}
        self.signs: dict[tuple[str, int], int | None] = {}  # (exam, asset) -> the position's side; None: not read
        self.event_at: dict[tuple, int] = {}  # a trigger's or a position's last block of an event or a read
        self.extra: set[str] = set()  # accounts sold since the keeper's last look
        self.ended: set[str] = set()
        self.pending: dict[tuple[str, int, bool], tuple[str, int, int]] = {}  # key -> (tx, nonce, block sent at)
        self.probe: set[tuple[str, int, bool]] = set()  # refused once: an eth_call before the next send
        self.blind_from: int | None = None
        self.inbox: queue.Queue = queue.Queue()
        self.gas_price = 0
        self.gas_read_at = -10**9
        self.quiet_until: dict[str, float] = {}

    # ── what the keeper hands in ─────────────────────────────────────────────────────

    def watched(self) -> tuple[list[str], list[str]]:
        """The accounts and the pools whose logs are read: the keeper's, as its thread left them, and the ones sold since."""
        done = set(self.keeper.done)
        accounts = [a for a in list(self.keeper.accounts) if a not in done]
        accounts = [a for a in dict.fromkeys(accounts + sorted(self.extra)) if a not in self.ended]
        return accounts, list(self.keeper.pools)

    def reread(self, exam: str, call=None) -> None:
        """An exam's stops and takes and its positions as the contract holds them, read at one block, and handed to the
        loop. Called by the keeper's slow pass, in its thread: what was set while nothing read the logs (a keeper down,
        or behind) is known at the next pass. The loop keeps what its own events say after that block."""
        call = call or c.rpc
        at = self.block or int(call("eth_blockNumber"), 16)
        tag = hex(at)

        def read(sig, types, args, out):
            raw = call("eth_call", [{"to": exam, "data": "0x" + c.encode_call(sig, types, args).hex()}, tag])
            return abi_decode(out, bytes.fromhex(raw[2:]))

        found: dict[tuple[str, int, bool], tuple[int, int]] = {}
        signs: dict[int, int] = {}
        for asset in read("rules()", [], [], [sk.RULES])[0][3]:
            asset = int(asset)
            for is_take, getter in ((False, "stopOf(uint32)"), (True, "takeOf(uint32)")):
                px, _, armed, _ = read(getter, ["uint32"], [asset], [sk.TRIGGER])[0]
                if px:
                    found[(exam, asset, is_take)] = (int(px), int(armed))
            if any(k[1] == asset for k in found):
                signs[asset] = sign_of(read("positionOf(uint32)", ["uint32"], [asset], ["int256"])[0])
        self.inbox.put((exam, at, found, signs))

    def apply_reread(self, exam: str, at: int, found: dict, signs: dict) -> None:
        if exam in self.ended:
            return
        for key in [k for k in self.triggers if k[0] == exam]:
            if key not in found and self.event_at.get(key, 0) <= at:
                log("stop_dropped_by_read", exam=exam, asset=key[1], take=key[2], at=at)
                self._drop(key)
        for key, (px, armed) in found.items():
            if self.event_at.get(key, 0) > at:
                continue
            t = self.triggers.get(key)
            if t is None:
                log("stop_found_by_read", exam=exam, asset=key[1], take=key[2], px=px, armed=armed, at=at)
                self.triggers[key] = {"px": px, "armed": armed, "touch": None, "said": False, "first": None}
            elif (t["px"], t["armed"]) != (px, armed):
                t.update(px=px, armed=armed, touch=None, said=False, first=None)
        for asset, s in signs.items():
            if self.event_at.get((exam, asset), 0) <= at:
                self.signs[(exam, asset)] = s

    # ── a step ───────────────────────────────────────────────────────────────────────

    def step(self, head: int) -> list[tuple[str, int, bool]]:
        """Everything up to block `head`: the keeper's reads, the receipts, the logs, each block's prices and its watch,
        then the stops reached at `head` sent for the next block. The keys it sent for."""
        while True:
            try:
                self.apply_reread(*self.inbox.get_nowait())
            except queue.Empty:
                break
        if head <= self.block:
            return []
        lo = head if not self.block else self.block + 1
        if head - lo + 1 > self.backfill:
            self._blind(lo, head - self.backfill, "the head ran ahead of the loop")
            lo = head - self.backfill + 1
            self.block = lo - 1
        accounts, pools = self.watched()
        if not accounts and not pools:
            self.block = head
            return []
        cache: dict[int, dict[int, int] | None] = {}
        sent: list[tuple[str, int, bool]] = []
        if lo < head and self.triggers:
            # Behind: the stops reached at the head go out first, and the blocks before it are read after.
            cache[head] = self._prices(head)
            if cache[head] is not None:
                sent = self._act(head, cache[head])
        by_block = self._logs(accounts, pools, lo, head)
        for b in range(lo, head + 1):
            prices = cache.get(b)
            if prices is None or set(prices) != {k[1] for k in self.triggers}:
                prices = self._prices(b)  # a busy node ends the step here; the next one starts at this block
            cache[b] = prices
            if prices is not None:
                self._watch(b, prices)
                if self.journal is not None:
                    try:
                        self.journal.add(b, prices)
                    except OSError as exc:
                        self._say("stop_journal_failed", error=str(exc)[:200])
            self._events(b, by_block.get(b, []))
            self.block = b
        self._receipts(head)  # one read a stop in flight: a refused or lost one is known before the next send
        sent += self._act(head, cache.get(head) or {}, skip=set(sent))
        return sent

    def _logs(self, accounts: list[str], pools: list[str], lo: int, hi: int) -> dict[int, list[dict]]:
        found = self.node.call("eth_getLogs", [{"address": accounts + pools, "topics": [ACCOUNT_TOPICS + POOL_TOPICS],
                                                "fromBlock": hex(lo), "toBlock": hex(hi)}]) if accounts or pools else []
        known = set(accounts)
        pool_set = {p.lower() for p in pools}
        new = []
        for l in found:
            if l["topics"][0] in POOL_TOPICS and l["address"].lower() in pool_set:
                a = to_checksum_address("0x" + l["topics"][1][-40:])
                if a not in known and a not in self.keeper.accounts:
                    self.extra.add(a)
                    new.append(a)
                    known.add(a)
        if new:  # their stops in the same blocks, which the read above could not name
            found = found + self.node.call("eth_getLogs", [{"address": new, "topics": [ACCOUNT_TOPICS],
                                                            "fromBlock": hex(lo), "toBlock": hex(hi)}])
        by_block: dict[int, list[dict]] = {}
        for l in found:
            if l["topics"][0] in ACCOUNT_TOPICS and to_checksum_address(l["address"]) in known:
                by_block.setdefault(int(l["blockNumber"], 16), []).append(l)
        for logs in by_block.values():
            logs.sort(key=lambda l: int(l.get("logIndex", "0x0"), 16))
        return by_block

    def _prices(self, block: int) -> dict[int, int] | None:
        """The oracle price, at `block`, of each asset with a stop or a take standing; None if one of them was not read."""
        assets = sorted({k[1] for k in self.triggers})
        prices: dict[int, int] = {}
        try:
            for asset in assets:
                raw = self.node.call("eth_call", [{"to": ORACLE_PX, "data": "0x" + abi_encode(["uint32"], [px_index(asset)]).hex()},
                                                  hex(block)])
                prices[asset] = int(abi_decode(["uint64"], bytes.fromhex(raw[2:]))[0])
        except c.NodeBusy:
            raise
        except Exception as exc:  # a block the node keeps no state of, an answer that did not parse
            self._blind(block, block, str(exc)[:120])
            return None
        if self.blind_from is not None:
            log("stop_watch_sees_again", blind_from=self.blind_from, to=block - 1)
            self.blind_from = None
        return prices

    def _blind(self, lo: int, hi: int, why: str) -> None:
        """Blocks whose prices the loop has not: nothing is judged in them. Said once for a run of them."""
        if self.blind_from is None:
            self.blind_from = lo
            log("stop_watch_blind", from_block=lo, to=hi, why=why,
                standing=len(self.triggers), note="a reach inside these blocks is not seen; the journal has a gap")

    # ── the watch ────────────────────────────────────────────────────────────────────

    def _watch(self, block: int, prices: dict[int, int]) -> None:
        for key, t in list(self.triggers.items()):
            exam, asset, take = key
            s = self.signs.get((exam, asset))
            if s is None or asset not in prices or t["armed"] > block:
                continue
            if hit(take, s, prices[asset], t["px"]):
                if t["touch"] is None:
                    t["touch"], t["first"] = block, prices[asset]
                if not t["said"] and block - t["touch"] + 1 >= self.overdue:
                    t["said"] = True
                    self.alarm("stop_overdue", **self._what(key, t, block), reason="reached and not carried out yet")
            elif t["touch"] is not None:
                self._missed(key, t, block - 1, "the price went back")

    def _missed(self, key, t: dict, last: int, reason: str) -> None:
        self.alarm("stop_missed", **self._what(key, t, last), reason=reason)
        t.update(touch=None, said=False, first=None)

    def _what(self, key, t: dict, last: int) -> dict:
        exam, asset, take = key
        return {"exam": exam, "asset": asset, "kind": "take" if take else "stop", "px": t["px"],
                "reached_from": t["touch"], "reached_to": last, "oracle_first": t["first"],
                "side": "long" if self.signs.get((exam, asset), 0) > 0 else "short"}

    def _events(self, block: int, logs: list[dict]) -> None:
        filled_in_tx = {(l["transactionHash"], to_checksum_address(l["address"]), int(l["topics"][1], 16),
                         bool(int(l["data"][2:66], 16))) for l in logs if l["topics"][0] == sk.TRIGGER_FILLED}
        moved: set[tuple[str, int]] = set()
        for l in logs:
            topic = l["topics"][0]
            exam = to_checksum_address(l["address"])
            if topic in (STOPPED, PASSED):
                for key in [k for k in self.triggers if k[0] == exam]:
                    if self.triggers[key]["touch"] is not None:
                        self._missed(key, self.triggers[key], block, "the exam ended")
                    self._drop(key)
                self.ended.add(exam)
                continue
            asset = int(l["topics"][1], 16)
            if topic == sk.FILLED:
                moved.add((exam, asset))
                continue
            take = bool(int(l["data"][2:66], 16))
            key = (exam, asset, take)
            self.event_at[key] = block
            t = self.triggers.get(key)
            if topic == sk.TRIGGER_FILLED:
                if t is not None and t["touch"] is not None:
                    log("stop_caught", exam=exam, asset=asset, kind="take" if take else "stop", px=t["px"],
                        reached_from=t["touch"], filled=block, late=block - t["touch"], tx=l["transactionHash"])
                else:
                    log("stop_filled", exam=exam, asset=asset, kind="take" if take else "stop", filled=block,
                        tx=l["transactionHash"])
                self._drop(key)
            elif topic == sk.TRIGGER_CLEARED:
                if (l["transactionHash"], exam, asset, take) in filled_in_tx:
                    continue  # the fill's own clearing, read with its TriggerFilled
                if t is not None and t["touch"] is not None:
                    self._missed(key, t, block, "taken off by the trader while reached")
                self._drop(key)
            elif topic == sk.TRIGGER_SET:
                _, px, _, armed = abi_decode(["bool", "uint64", "uint64", "uint256"], bytes.fromhex(l["data"][2:]))
                if t is not None and t["touch"] is not None:
                    self._missed(key, t, block, "moved by the trader while reached")
                self.triggers[key] = {"px": int(px), "armed": int(armed), "touch": None, "said": False, "first": None}
                self.probe.discard(key)
                moved.add((exam, asset))
        for exam, asset in moved:
            if any(k[0] == exam and k[1] == asset for k in self.triggers):
                self._read_sign(exam, asset, block)

    def _read_sign(self, exam: str, asset: int, block: int) -> None:
        """The side of a position after the events of `block`, read at it; None until it is read."""
        self.event_at[(exam, asset)] = block
        try:
            raw = self.node.call("eth_call", [{"to": exam, "data": "0x" + c.encode_call(
                "positionOf(uint32)", ["uint32"], [asset]).hex()}, hex(block)])
            s = sign_of(abi_decode(["int256"], bytes.fromhex(raw[2:]))[0])
        except Exception as exc:
            self._say("stop_position_unread", exam=exam, asset=asset, error=str(exc)[:120])
            self.signs[(exam, asset)] = None
            return
        if s == 0:
            for take in (False, True):
                t = self.triggers.get((exam, asset, take))
                if t is not None and t["touch"] is not None:
                    self._missed((exam, asset, take), t, block, "the position was closed")
        self.signs[(exam, asset)] = s

    def _drop(self, key) -> None:
        self.triggers.pop(key, None)
        self.probe.discard(key)

    # ── carrying out ─────────────────────────────────────────────────────────────────

    def _act(self, head: int, prices: dict[int, int], skip: set | frozenset = frozenset()) -> list[tuple[str, int, bool]]:
        """The stops reached at `head`, armed for the next block and not in flight, sent at once. One whose position is
        not read, or that the exam refused once, is tried with an eth_call first."""
        sent = []
        for key, t in list(self.triggers.items()):
            exam, asset, take = key
            if key in skip or t["armed"] > head + 1 or key in self.pending or exam in self.keeper.done:
                continue
            s = self.signs.get((exam, asset))
            if s == 0:
                continue
            data = c.encode_call("fillTrigger(uint32,bool)", ["uint32", "bool"], [asset, take])
            if s is None or key in self.probe:
                try:
                    self.node.call("eth_call", [{"from": self.keeper.sender, "to": exam, "data": "0x" + data.hex()}, "latest"])
                except Exception:
                    continue
            elif asset not in prices or not hit(take, s, prices[asset], t["px"]):
                continue
            sent.append(key)
            if self.wallet is None or self.nonces is None or not self.keeper.is_filler():
                if t.get("would") != t["touch"]:
                    t["would"] = t["touch"]
                    log("would_fill_trigger", exam=exam, asset=asset, take=take, block=head)
                continue
            try:
                tx, nonce = self.nonces.broadcast(self.wallet, self.node.call, exam, data, STOP_GAS, self._gas_price(head),
                                                  self.tip)
            except Exception as exc:
                log("trigger_fill_failed", exam=exam, asset=asset, take=take, error=str(exc)[:200])
                continue
            self.pending[key] = (tx, nonce, head)
            self.probe.discard(key)
            log("trigger_sent", exam=exam, asset=asset, take=take, tx=tx, seen=head)
        return sent

    def _gas_price(self, head: int) -> int:
        if head - self.gas_read_at >= 10 or not self.gas_price:
            self.gas_price = int(self.node.call("eth_gasPrice"), 16)
            self.gas_read_at = head
        return self.gas_price

    def _receipts(self, head: int) -> None:
        for key, (tx, nonce, at) in list(self.pending.items()):
            try:
                rcpt = self.node.call("eth_getTransactionReceipt", [tx])
            except Exception as exc:
                self._say("stop_receipt_unread", error=str(exc)[:120])
                return
            exam, asset, take = key
            if rcpt:
                del self.pending[key]
                if int(rcpt["status"], 16) == 1:
                    log("trigger_filled", exam=exam, asset=asset, take=take, tx=tx, block=int(rcpt["blockNumber"], 16),
                        seen=at)
                else:
                    self.probe.add(key)
                    log("trigger_fill_refused", exam=exam, asset=asset, take=take, tx=tx,
                        block=int(rcpt["blockNumber"], 16), seen=at)
            elif head - at > RECEIPT_GIVE_UP:
                del self.pending[key]
                self.nonces.failed()
                log("trigger_fill_lost", exam=exam, asset=asset, take=take, tx=tx, seen=at,
                    why=f"no receipt in {RECEIPT_GIVE_UP} blocks; the nonce is read again")

    # ── running ──────────────────────────────────────────────────────────────────────

    def _say(self, event: str, every: float = 30.0, **fields) -> None:
        """A line that would repeat each block, said once in `every` seconds."""
        now = time.monotonic()
        if now >= self.quiet_until.get(event, 0.0):
            self.quiet_until[event] = now + every
            log(event, **fields)

    def run(self, poll: float, stop: threading.Event | None = None) -> None:
        """Forever (or until `stop`): the head every `poll` seconds, and a step for each new one. Nothing here waits on the
        keeper's thread, nor it on this one; a busy node costs the look it refused."""
        stop = stop or threading.Event()
        log("stop_loop_started", node=self.node.url.split("?")[0], tip_wei=self.tip, sends=self.wallet is not None,
            journal=str(self.journal.path) if self.journal else None,
            flag=str(self.alarm.flag) if self.alarm.flag else None)
        while not stop.is_set():
            try:
                head = int(self.node.call("eth_blockNumber"), 16)
                if head > self.block:
                    self.step(head)
            except c.NodeBusy as exc:
                self._say("stop_node_busy", error=str(exc)[:120])
            except Exception as exc:
                self._say("stop_loop_failed", error=str(exc)[:200])
            stop.wait(poll)


def check_chain(node: Node) -> None:
    """The stops' node on the network the keeper's is on: a stop sent to another chain is no stop."""
    chain = int(node.call("eth_chainId"), 16)
    if chain != c.CHAIN_ID:
        raise SystemExit(f"refusing to run: the stops' node is on chain {chain}, expected {c.CHAIN_ID}")


def add_args(p: argparse.ArgumentParser) -> None:
    """The settings of the stops' loop, each defaulting to what the keeper did before them."""
    p.add_argument("--stops", choices=("loop", "inline"), default=os.environ.get("COLOSSEUM_STOPS", "loop"),
                   help="loop: the stops in a thread of their own, every block (ops/stop_filler.py); inline: in the "
                        "keeper's one loop, as before")
    p.add_argument("--stop-rpc", default=os.environ.get("COLOSSEUM_STOP_RPC_URL", ""),
                   help="the node the stops are read from and sent to; the keeper's (COLOSSEUM_RPC_URL) when empty")
    p.add_argument("--stop-tip-gwei", type=float, default=float(os.environ.get("COLOSSEUM_STOP_TIP_GWEI", "0") or 0),
                   help="the priority fee of a stop's transaction, in gwei; zero, as every send of the keeper's")
    p.add_argument("--stop-poll", type=float, default=None, help="seconds between looks at the head (default --poll)")
    p.add_argument("--stop-overdue", type=int, default=OVERDUE, help="blocks a reach may last unfilled before its alarm")
    p.add_argument("--price-journal", default=None,
                   help="the journal of oracle prices (default prices-<deployment>.json next to the state); '' for none")
    p.add_argument("--stop-alarm", default=None,
                   help="the flag file of missed stops (default stops-<deployment>.alarm next to the state); '' for none")


def build(args, keeper, state: pathlib.Path, wallet=None, nonces: Nonces | None = None) -> StopLoop:
    """The loop from the settings, its node checked against the network."""
    url = args.stop_rpc or c.RPC_URL
    node = Node(url)
    if c.NETWORK.name == "mainnet":
        core.check_node(url, c.CHAIN_ID)
    check_chain(node)
    journal_path = (state.parent / f"prices-{args.deployment}.json") if args.price_journal is None else (
        pathlib.Path(args.price_journal) if args.price_journal else None)
    flag = (state.parent / f"stops-{args.deployment}.alarm") if args.stop_alarm is None else (
        pathlib.Path(args.stop_alarm) if args.stop_alarm else None)
    if args.stop_tip_gwei < 0:
        raise SystemExit("--stop-tip-gwei must not be negative")
    return StopLoop(keeper, node, wallet=wallet, nonces=nonces, tip_wei=int(round(args.stop_tip_gwei * GWEI)),
                    journal=PriceJournal(journal_path) if journal_path else None, alarm=Alarm(flag),
                    overdue=args.stop_overdue)


def start(loop: StopLoop, poll: float) -> threading.Thread:
    thread = threading.Thread(target=loop.run, args=(poll,), name="stops", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    """The watch alone: the factory's accounts found as the keeper finds them, their stops reread every --every seconds,
    and the loop with no key. It sends nothing; run it on another machine and node to see what the keeper missed even
    when the keeper itself is down."""
    from ops import deployments
    from ops import sim_network as sn

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/ with a SimPoolFactory")
    p.add_argument("--every", type=int, default=300, help="seconds between reads of the pools, accounts and stops")
    p.add_argument("--poll", type=float, default=0.5)
    p.add_argument("--state", help="the watch's own state file (default ops/state/stops-<deployment>.json)")
    add_args(p)
    sn.add_args(p)
    args = p.parse_args(argv)
    sn.selected(args, c.NETWORK)
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    state = pathlib.Path(args.state) if args.state else sk.STATE_DIR / f"stops-{args.deployment}.json"
    keeper = sk.SimKeeper(record["SimPoolFactory"], None, "0x" + "00" * 20, True, state, int(record["block"]))
    loop = build(args, keeper, state)
    start(loop, args.stop_poll or args.poll)
    while True:
        try:
            keeper.scan(int(c.rpc("eth_blockNumber"), 16))
            for a in keeper.live():
                if a not in loop.ended and keeper.accounts[a]["kind"] in ("exam", "stage"):
                    try:
                        loop.reread(a)
                    except Exception as exc:
                        log("stops_unread", account=a, error=str(exc)[:200])
        except Exception as exc:
            log("watch_pass_failed", error=str(exc)[:200])
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
