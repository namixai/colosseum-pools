"""The keeper's relay for the key binder: an account that asked for a key gets one, on the publisher's word.

An account on a factory with a key binder (src/KeyBinder.sol, docs/KEY-BINDING.md) opens a request when it
needs a key: a challenge once its capital is in place, a funded stage, a renewal, a spoiled key's
replacement, a key the guardian revoked. The publisher signs a fresh key for that one request under EIP-712,
and anyone may carry the signature in. This module is the keeper's side of that:

    request open, no delay        → a fresh key, the publisher's signature, `bind`
    renewal with a delay set      → `commit` with the key's hash; once the delay has passed, `reveal`
    binder paused                 → `binder_paused`, nothing sent
    no publisher given to keeper  → `key_asked`, nothing sent: the request waits for one

The publisher here is `LocalPublisher`, a testnet key in a file (as `PUBLISHER_PK` in test/BinderStand.sol).
It makes the agent key too, and writes it into the gateway's key directory, owner-only, before the binding is
sent: a key the chain binds is a key the gateway already holds. The enclave takes its place later, with the
same two calls (`fresh_key`, `sign`); the binder does not care who the publisher is, only that its address
signed. A local publisher is refused on mainnet.

The digest is computed here, from the binder's domain, and must equal the binder's own `digest(...)`: a binder
with another domain, or a node that answers for another contract, gets no signature.
"""

from __future__ import annotations

import os
import pathlib
import secrets

import eth_abi
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak, to_checksum_address

# KeyBinder.Purpose
NONE, START, FUNDED, RENEW, REKEY, REVOKED = range(6)
PURPOSES = {START: "Start", FUNDED: "Funded", RENEW: "Renew", REKEY: "Rekey", REVOKED: "Revoked"}
REQUEST_OUT = "(uint8,address,uint64,uint64,bytes32,uint64)"
DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
BINDING_TYPEHASH = keccak(text="Binding(address account,uint64 request,address trader,bytes32 keyHash)")
DOMAIN_NAME = "colosseum-pools KeyBinder"
DOMAIN_VERSION = "1"
ZERO32 = b"\x00" * 32
MAINNET_CHAIN_ID = 999
KEY_PREFIX = "agent-"
# A request older than this is said on every pass it is seen (`key_request_old`): a start window is an hour, and a
# relay that has not answered in ten minutes is a relay to look at.
REQUEST_OLD_S = 600


def key_hash(key: str) -> bytes:
    """What the binder commits a renewal's key by: keccak256(abi.encode(key))."""
    return keccak(eth_abi.encode(["address"], [to_checksum_address(key)]))


def binding_digest(chain_id: int, binder: str, account: str, request_id: int, trader: str, khash: bytes) -> bytes:
    """`KeyBinder.digest(account, id, trader, keyHash)`, computed off chain."""
    domain = keccak(eth_abi.encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [DOMAIN_TYPEHASH, keccak(text=DOMAIN_NAME), keccak(text=DOMAIN_VERSION), chain_id, to_checksum_address(binder)]))
    struct = keccak(eth_abi.encode(
        ["bytes32", "address", "uint64", "address", "bytes32"],
        [BINDING_TYPEHASH, to_checksum_address(account), request_id, to_checksum_address(trader), khash]))
    return keccak(b"\x19\x01" + domain + struct)


class LocalPublisher:
    """A publisher key in a file, for testnet: it makes agent keys into `keys_dir` and signs their bindings."""

    def __init__(self, account: LocalAccount, keys_dir: pathlib.Path, chain_id: int):
        if chain_id == MAINNET_CHAIN_ID:
            raise SystemExit("a publisher key in a file is for testnet; on mainnet the publisher is the enclave")
        d = pathlib.Path(keys_dir).expanduser()
        if not d.is_dir():
            raise SystemExit(f"{d} is not a directory")
        if d.stat().st_mode & 0o077:
            raise SystemExit(f"{d} is open to others; chmod 700 it")
        self.account = account
        self.address = account.address
        self.keys_dir = d

    def fresh_key(self) -> str:
        """A new agent key, written owner-only into the gateway's directory before anything is sent. Its
        address is returned; the private part never leaves the file."""
        key = Account.from_key("0x" + secrets.token_hex(32))
        path = self.keys_dir / f"{KEY_PREFIX}{key.address}.key"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key.key.hex() + "\n")
        return key.address

    def key_for(self, khash: bytes) -> str | None:
        """The key this publisher made whose hash a renewal committed, if it is still in the directory."""
        for f in sorted(self.keys_dir.glob(f"{KEY_PREFIX}*.key")):
            address = f.name[len(KEY_PREFIX):-len(".key")]
            try:
                if key_hash(address) == khash:
                    return to_checksum_address(address)
            except ValueError:
                continue
        return None

    def sign(self, digest: bytes) -> bytes:
        """The 65-byte signature (r, s, v) the binder recovers the publisher from."""
        return bytes(Account.unsafe_sign_hash(digest, self.account.key).signature)


class Relay:
    """Answers the open request of an account with the publisher's signature. One per keeper; `publisher`
    None makes it an alarm only. The keeper's `view`, `send`, `info` and `log` are passed in."""

    def __init__(self, publisher, chain_id: int, view, send, info, log):
        self.publisher = publisher
        self.chain_id = chain_id
        self.view, self.send, self.info, self.log = view, send, info, log
        self._binders: dict[str, str] = {}
        # The key made for a request, by (account, request id): a pass whose send failed tries the same key
        # again instead of making another, until the key turns out spoiled or taken.
        self._made: dict[tuple[str, int], str] = {}

    def binder_of(self, account: str) -> str:
        factory = to_checksum_address(self.view(account, "factory()", "address"))
        if factory not in self._binders:
            self._binders[factory] = to_checksum_address(self.view(factory, "binder()", "address"))
        return self._binders[factory]

    def serve(self, wallet, account: str, now: int, dry: bool) -> None:
        """One look at `account`'s request, and what is due for it."""
        account = to_checksum_address(account)
        binder = self.binder_of(account)
        purpose, trader, rid, opened, khash, activates = self.view(binder, "requestOf(address)", REQUEST_OUT,
                                                                   ["address"], [account])
        khash = bytes(khash)
        if purpose == NONE:
            return
        trader = to_checksum_address(trader)
        what = {"account": account, "purpose": PURPOSES.get(purpose, purpose), "request": rid,
                "waiting_s": max(0, now - opened)}
        if what["waiting_s"] > REQUEST_OLD_S and (khash == ZERO32 or now >= activates):
            self.log("key_request_old", binder=binder, **what)
        if self.view(binder, "paused()", "bool"):
            self.log("binder_paused", binder=binder, **what)
            return
        if self.publisher is None:
            self.log("key_asked", binder=binder, **what)
            return
        appointed = to_checksum_address(self.view(binder, "publisher()", "address"))
        if appointed != self.publisher.address:
            self.log("publisher_not_appointed", binder=binder, appointed=appointed, ours=self.publisher.address, **what)
            return
        if khash != ZERO32:
            if now < activates:
                return  # a committed renewal, its delay not over
            key = self.publisher.key_for(khash)
            if key is None:
                self.log("committed_key_missing", binder=binder, key_hash="0x" + khash.hex(), **what)
                return
            self.log("key_reveal", binder=binder, key=key, **what)
            self.send(wallet, binder, "reveal(address,address)", ["address", "address"], [account, key], dry=dry)
            return
        delayed = purpose == RENEW and self.view(binder, "renewDelay()", "uint64") != 0
        made = (account, rid)
        key = self._made.get(made)
        if key is not None and not self.still_new(binder, key):
            self.log("made_key_unusable", binder=binder, key=key, **what)
            key = None
        if key is None:
            if dry:
                self.log("would_bind", binder=binder, **what)
                return
            key = self.publisher.fresh_key()
            self._made = {k: v for k, v in self._made.items() if k[0] != account}
            self._made[made] = key
        h = key_hash(key)
        digest = binding_digest(self.chain_id, binder, account, rid, trader, h)
        theirs = bytes(self.view(binder, "digest(address,uint64,address,bytes32)", "bytes32",
                                 ["address", "uint64", "address", "bytes32"], [account, rid, trader, h]))
        if theirs != digest:
            self.log("digest_differs", binder=binder, ours="0x" + digest.hex(), theirs="0x" + theirs.hex(), **what)
            return
        signature = self.publisher.sign(digest)
        if delayed:
            self.log("key_commit", binder=binder, key_hash="0x" + h.hex(), **what)
            self.send(wallet, binder, "commit(address,bytes32,bytes)", ["address", "bytes32", "bytes"],
                      [account, h, signature], dry=dry)
        else:
            self.log("key_bind", binder=binder, key=key, **what)
            self.send(wallet, binder, "bind(address,address,bytes)", ["address", "address", "bytes"],
                      [account, key, signature], dry=dry)

    def still_new(self, binder: str, key: str) -> bool:
        """Whether a key made earlier can still be bound: unknown to the binder and with no HyperCore account.
        A key that gained one (anyone can send it 1 USDC once its address is out) is never bound."""
        state = self.view(binder, "bindingOf(address)", "(uint8,address,address)", ["address"], [key])[0]
        if state != 0:
            return False
        try:
            role = (self.info({"type": "userRole", "user": key}) or {}).get("role")
        except Exception:
            return True  # not read: the binder checks it again and refuses a spoiled key
        return role in (None, "missing")
