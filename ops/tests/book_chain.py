"""A made-up chain for the stop book's loop: a mempool, nonces, the replacement rule (+10 %), a basefee, small blocks of
2 000 000 gas, the book's views, and a clock under the test's hand. One node over one chain; `lag` blocks behind."""
import time
from unittest import mock

from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from ops import book_keeper as bk
from ops import stop_filler as sf
from spike.hlspike import common as c

BOOK = to_checksum_address("0x" + "b0" * 20)
KEY_A = Account.from_key("0x" + "42" * 32)
KEY_B = Account.from_key("0x" + "43" * 32)


def sel(sig):
    return "0x" + keccak(text=sig)[:4].hex()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def mono(self):
        return self.t

    def wall(self):
        return self.t

    def advance(self, s):
        self.t += s

    def patch(self, test):
        for name, fn in (("monotonic", self.mono), ("time", self.wall)):
            p = mock.patch.object(time, name, fn)
            p.start()
            test.addCleanup(p.stop)


class Chain:
    """One node over one chain. `lag`: how many blocks behind the real head this node answers."""

    def __init__(self, clock, url="http://127.0.0.1:9", lag=0, latency=0.0):
        self.clock = clock
        self.url = url
        self.head = 100
        self.lag = lag
        self.latency = latency
        self.basefee = 100_000_000
        self.mempool = {}      # addr -> {nonce: dict}
        self.nonce = {}        # addr -> next nonce on chain
        self.receipts = {}
        self.last_poke = 0
        self.watched = [3]
        self.balance = 10 ** 21
        self.calls = []
        self.dead = False
        self.failing = {}      # method -> exception to raise
        self.lose = lambda addr, nonce, raw, n: False   # a transaction the node takes and forgets
        self.skew = 0.0        # seconds the blocks' time runs behind this machine's clock
        self.sent = []
        self.poked = []        # blocks a poke ran in
        self.stamp = None
        self.frozen_ts = None
        self.auto = False      # blocks are made as the clock passes them
        self.next_block = clock.t + 1.0
        self.no_price_assets = set()  # assets whose oracle read fails in a poke: NoPrice, a zero logged
        self.no_price = []  # (block, asset) of each NoPrice

    def catch_up(self):
        while self.auto and self.clock.t >= self.next_block:
            self.next_block += 1.0
            self.mine()

    def mine(self):
        self.head += 1
        used = 0
        for addr, pool in self.mempool.items():
            while self.nonce.get(addr, 0) in pool:
                n = self.nonce.get(addr, 0)
                tx = pool[n]
                if tx["maxFee"] < self.basefee or used + tx["gas"] > 2_000_000:
                    break
                used += tx["gas"]
                del pool[n]
                self.nonce[addr] = n + 1
                ok = True
                if tx["sel"] == sel("poke()") and self.last_poke != self.head:
                    for asset in self.no_price_assets:
                        if asset in self.watched:
                            self.no_price.append((self.head, asset))
                if tx["sel"] == sel("poke()"):
                    self.poked.append(self.head)
                    self.last_poke = self.head
                    ok = not getattr(self, "revert_pokes", False)
                self.receipts[tx["hash"]] = {"status": "0x1" if ok else "0x0", "blockNumber": hex(self.head),
                                              "transactionHash": tx["hash"], "gasUsed": hex(100_000)}
                for h in tx["all"]:
                    self.receipts[h] = self.receipts[tx["hash"]]

    def call(self, method, params=()):
        self.clock.advance(self.latency)
        self.catch_up()
        self.calls.append((self.clock.t, method))
        if self.dead:
            raise sf.Unsure(f"{method}: Timeout")
        if method in self.failing:
            raise self.failing[method]
        view = self.head - self.lag
        if method == "eth_blockNumber":
            return hex(view)
        if method == "eth_getBlockByNumber":
            return {"number": hex(view), "timestamp": hex(int((self.clock.t if self.frozen_ts is None else self.frozen_ts) - self.skew)), "baseFeePerGas": hex(self.basefee)}
        if method == "eth_chainId":
            return hex(c.CHAIN_ID)
        if method == "eth_gasPrice":
            return hex(self.basefee)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_getTransactionCount":
            addr = to_checksum_address(params[0])
            base = self.nonce.get(addr, 0)
            if params[1] == "pending":
                base += len(self.mempool.get(addr, {}))
            return hex(base)
        if method == "eth_sendRawTransaction":
            raw = params[0]
            from eth_account.typed_transactions import TypedTransaction
            from hexbytes import HexBytes
            t = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
            addr = to_checksum_address(Account.recover_transaction(raw))
            h = "0x" + keccak(hexstr=raw).hex()
            n = t["nonce"]
            if n < self.nonce.get(addr, 0):
                raise RuntimeError("eth_sendRawTransaction: {'code': -32000, 'message': 'nonce too low'}")
            if self.balance == 0:
                raise RuntimeError("eth_sendRawTransaction: {'code': -32000, 'message': 'insufficient funds for gas * price + value'}")
            self.sent.append((self.clock.t, addr, n, h))
            if self.lose(addr, n, raw, len(self.sent)):
                return h
            pool = self.mempool.setdefault(addr, {})
            old = pool.get(n)
            if old is not None:
                if old["hash"] == h or h in old["all"]:
                    raise RuntimeError("already known")
                if t["maxFeePerGas"] * 100 < old["maxFee"] * 110 or t["maxPriorityFeePerGas"] * 100 < old["tip"] * 110:
                    raise RuntimeError("replacement transaction underpriced")
                old_all = old["all"]
            else:
                old_all = []
            pool[n] = {"hash": h, "maxFee": t["maxFeePerGas"], "tip": t["maxPriorityFeePerGas"], "gas": t["gas"],
                       "sel": "0x" + bytes(t["data"])[:4].hex(), "all": old_all + [h]}
            return h
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            topic = "0x" + keccak(text="NoPrice(uint32)").hex()
            if topic not in q["topics"][0]:
                return []
            return [{"address": BOOK, "topics": [topic, "0x" + a.to_bytes(32, "big").hex()], "data": "0x",
                     "blockNumber": hex(b), "logIndex": "0x0", "transactionHash": "0x" + "00" * 32}
                    for b, a in self.no_price if lo <= b <= min(hi, self.head - self.lag)]
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_call":
            q, tag = params
            data = q["data"]
            if data.startswith(sel("watched()")):
                return "0x" + encode(["uint32[]"], [self.watched]).hex()
            if data.startswith(sel("lastPokeBlock()")):
                return "0x" + self.last_poke.to_bytes(32, "big").hex()
            if data.startswith(sel("oracleGas()")) or data.startswith(sel("bboGas()")):
                return "0x" + (50_000).to_bytes(32, "big").hex()
            raise RuntimeError("unexpected call " + data[:10])
        raise RuntimeError("unexpected method " + method)


def alarms():
    out = []
    m = mock.Mock(side_effect=lambda e, **f: out.append((e, f)))
    m.flag = None
    return m, out
