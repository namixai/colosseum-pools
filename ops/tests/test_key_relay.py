"""The keeper's relay for the key binder (ops/key_relay.py), and where the keeper calls it (ops/keeper.py).

    spike/.venv/bin/python -m unittest ops.tests.test_key_relay

Offline. The binder is a fake that answers as src/KeyBinder.sol does; its digest is computed with
eth_account's own EIP-712 encoder, not with the relay's code, so the two have to agree on their own.
"""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from ops import keeper, key_relay
from ops.key_relay import FUNDED, NONE, RENEW, REVOKED, START, ZERO32, LocalPublisher, Relay, key_hash
from ops.tests.test_keeper import CHALLENGE_A, NOON, POOL_A, KeeperTest, challenge_log

BINDER = to_checksum_address("0x00000000000000000000000000000000000000B9")
FACTORY = to_checksum_address("0x00000000000000000000000000000000000000F9")
ACCOUNT = to_checksum_address("0x00000000000000000000000000000000000000A9")
TRADER = to_checksum_address("0x00000000000000000000000000000000000000D9")
CHAIN = 998


def eip712_digest(chain_id, binder, account, rid, trader, khash) -> bytes:
    message = encode_typed_data(full_message={
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
            "Binding": [{"name": "account", "type": "address"}, {"name": "request", "type": "uint64"},
                        {"name": "trader", "type": "address"}, {"name": "keyHash", "type": "bytes32"}],
        },
        "primaryType": "Binding",
        "domain": {"name": "colosseum-pools KeyBinder", "version": "1", "chainId": chain_id,
                   "verifyingContract": binder},
        "message": {"account": account, "request": rid, "trader": trader, "keyHash": khash},
    })
    return keccak(b"\x19" + message.version + message.header + message.body)


def recover(digest: bytes, signature: bytes) -> str:
    return Account._recover_hash(digest, signature=signature)


class FakeBinder:
    """One account, its factory and its binder, answering the relay's reads; sends recorded."""

    def __init__(self, publisher: str):
        self.request = (NONE, TRADER, 0, NOON - 60, ZERO32, 0)
        self.paused = False
        self.publisher = publisher
        self.renew_delay = 0
        self.bindings: dict[str, int] = {}
        self.roles: dict[str, str] = {}
        self.sent: list[tuple[str, str, list]] = []
        self.logs: list[tuple[str, dict]] = []
        self.chain_id = CHAIN  # the chain the binder's own digest is for
        self.files_at_send: list[list[str]] = []
        self.keys_dir: pathlib.Path | None = None

    def ask(self, purpose, rid=1, khash=ZERO32, activates=0):
        self.request = (purpose, TRADER, rid, NOON - 60, khash, activates)

    def view(self, addr, sig, out, types=(), args=()):
        name = sig.split("(")[0]
        if addr == ACCOUNT and name == "factory":
            return FACTORY
        if addr == FACTORY and name == "binder":
            return BINDER
        assert addr == BINDER, (addr, sig)
        if name == "requestOf":
            assert args == [ACCOUNT]
            return self.request
        if name == "paused":
            return self.paused
        if name == "publisher":
            return self.publisher
        if name == "renewDelay":
            return self.renew_delay
        if name == "bindingOf":
            return (self.bindings.get(args[0], 0), ACCOUNT, TRADER)
        if name == "digest":
            return eip712_digest(self.chain_id, BINDER, *args)
        raise AssertionError(sig)

    def send(self, wallet, addr, sig, types=(), args=(), dry=False):
        if self.keys_dir is not None:
            self.files_at_send.append(sorted(f.name for f in self.keys_dir.iterdir()))
        self.sent.append((addr, sig.split("(")[0], list(args)))

    def info(self, body):
        return {"role": self.roles.get(body["user"], "missing")}

    def log(self, event, **fields):
        self.logs.append((event, fields))

    def events(self):
        return [e for e, _ in self.logs]


class Publishers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name) / "keys"
        self.dir.mkdir()
        os.chmod(self.dir, 0o700)
        self.key = Account.create(os.urandom(32))

    def test_the_digest_is_eip712s_under_the_binders_domain(self):
        khash = key_hash(TRADER)
        ours = key_relay.binding_digest(CHAIN, BINDER, ACCOUNT, 7, TRADER, khash)
        self.assertEqual(ours, eip712_digest(CHAIN, BINDER, ACCOUNT, 7, TRADER, khash))
        for other in (key_relay.binding_digest(999, BINDER, ACCOUNT, 7, TRADER, khash),
                      key_relay.binding_digest(CHAIN, FACTORY, ACCOUNT, 7, TRADER, khash),
                      key_relay.binding_digest(CHAIN, BINDER, ACCOUNT, 8, TRADER, khash),
                      key_relay.binding_digest(CHAIN, BINDER, ACCOUNT, 7, ACCOUNT, khash)):
            self.assertNotEqual(ours, other)

    def test_a_keys_hash_is_keccak_of_its_abi_encoding(self):
        self.assertEqual(key_hash(TRADER), keccak(bytes(12) + bytes.fromhex(TRADER[2:])))

    def test_a_publisher_in_a_file_is_refused_on_mainnet(self):
        with self.assertRaises(SystemExit):
            LocalPublisher(self.key, self.dir, 999)

    def test_a_directory_open_to_others_is_refused(self):
        os.chmod(self.dir, 0o750)
        with self.assertRaises(SystemExit):
            LocalPublisher(self.key, self.dir, CHAIN)
        with self.assertRaises(SystemExit):
            LocalPublisher(self.key, self.dir / "missing", CHAIN)

    def test_a_fresh_key_is_an_owner_only_file_the_gateway_reads(self):
        publisher = LocalPublisher(self.key, self.dir, CHAIN)
        address = publisher.fresh_key()
        path = self.dir / f"agent-{address}.key"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(Account.from_key(path.read_text().strip()).address, address)
        self.assertNotEqual(publisher.fresh_key(), address, "each key is new")
        from gateway.demo_signer import DemoSigner
        self.assertIn(address, [k.address for k in DemoSigner.load_keys(str(self.dir))])

    def test_a_committed_key_is_found_by_its_hash(self):
        publisher = LocalPublisher(self.key, self.dir, CHAIN)
        address = publisher.fresh_key()
        publisher.fresh_key()
        (self.dir / "agent-not-an-address.key").write_text("x")
        self.assertEqual(publisher.key_for(key_hash(address)), address)
        self.assertIsNone(publisher.key_for(key_hash(TRADER)))

    def test_the_signature_recovers_to_the_publisher(self):
        publisher = LocalPublisher(self.key, self.dir, CHAIN)
        digest = key_relay.binding_digest(CHAIN, BINDER, ACCOUNT, 1, TRADER, key_hash(TRADER))
        signature = publisher.sign(digest)
        self.assertEqual(len(signature), 65)
        self.assertIn(signature[64], (27, 28), "v as ECDSA.recover takes it")
        self.assertEqual(recover(digest, signature), self.key.address)


class Relaying(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        os.chmod(self.dir, 0o700)
        self.key = Account.create(os.urandom(32))
        self.publisher = LocalPublisher(self.key, self.dir, CHAIN)
        self.binder = FakeBinder(self.key.address)
        self.binder.keys_dir = self.dir
        self.relay = self.make(self.publisher)

    def make(self, publisher):
        b = self.binder
        return Relay(publisher, CHAIN, b.view, b.send, b.info, b.log)

    def serve(self, now=NOON, dry=False):
        self.relay.serve("wallet", ACCOUNT, now, dry)

    def keys(self):
        return sorted(f.name for f in self.dir.glob("agent-*.key"))

    def test_the_factory_and_the_binder_are_read_once_an_account(self):
        reads = []
        real = self.binder.view

        def counting(addr, sig, *rest):
            reads.append(sig)
            return real(addr, sig, *rest)

        self.relay.view = counting
        self.serve()
        self.serve()
        self.assertEqual((reads.count("factory()"), reads.count("binder()")), (1, 1))

    def test_no_request_nothing(self):
        self.serve()
        self.assertEqual((self.binder.sent, self.keys()), ([], []))

    def test_a_start_is_bound_with_the_publishers_signature_on_a_key_the_gateway_already_holds(self):
        self.binder.ask(START, rid=3)
        self.serve()
        [(to, fn, (account, key, signature))] = self.binder.sent
        self.assertEqual((to, fn, account), (BINDER, "bind", ACCOUNT))
        self.assertEqual(self.keys(), [f"agent-{key}.key"])
        self.assertEqual(self.binder.files_at_send, [[f"agent-{key}.key"]], "the file is there before the send")
        digest = eip712_digest(CHAIN, BINDER, ACCOUNT, 3, TRADER, key_hash(key))
        self.assertEqual(recover(digest, signature), self.key.address)
        self.assertIn("key_bind", self.binder.events())

    def test_every_purpose_without_a_delay_is_a_bind(self):
        for purpose in (FUNDED, REVOKED, RENEW):
            self.binder.sent.clear()
            self.binder.ask(purpose, rid=purpose)
            self.serve()
            self.assertEqual([fn for _, fn, _ in self.binder.sent], ["bind"], purpose)

    def test_a_request_older_than_ten_minutes_is_said(self):
        self.relay = self.make(None)
        for waited, said in ((key_relay.REQUEST_OLD_S, False), (key_relay.REQUEST_OLD_S + 1, True)):
            self.binder.logs.clear()
            self.binder.ask(START)
            self.serve(now=NOON - 60 + waited)
            self.assertEqual("key_request_old" in self.binder.events(), said, waited)

    def test_a_paused_binder_gets_nothing(self):
        self.binder.ask(START)
        self.binder.paused = True
        self.serve()
        self.assertEqual((self.binder.sent, self.keys()), ([], []))
        self.assertIn("binder_paused", self.binder.events())

    def test_without_a_publisher_it_only_says_so(self):
        self.binder.ask(START)
        self.relay = self.make(None)
        self.serve()
        self.assertEqual(self.binder.sent, [])
        [(event, fields)] = self.binder.logs
        self.assertEqual((event, fields["purpose"], fields["waiting_s"]), ("key_asked", "Start", 60))

    def test_a_publisher_the_owner_has_not_appointed_signs_nothing(self):
        self.binder.ask(START)
        self.binder.publisher = Account.create(os.urandom(32)).address
        self.serve()
        self.assertEqual((self.binder.sent, self.keys()), ([], []))
        self.assertIn("publisher_not_appointed", self.binder.events())

    def test_a_binder_whose_digest_differs_gets_no_signature(self):
        self.binder.ask(START)
        self.binder.chain_id = 999
        self.serve()
        self.assertEqual(self.binder.sent, [])
        self.assertIn("digest_differs", self.binder.events())

    def test_a_renewal_with_a_delay_is_committed_by_hash_and_revealed_after_it(self):
        self.binder.renew_delay = 86_400
        self.binder.ask(RENEW, rid=5)
        self.serve()
        [(to, fn, (account, khash, signature))] = self.binder.sent
        self.assertEqual((to, fn, account), (BINDER, "commit", ACCOUNT))
        [name] = self.keys()
        key = name[len("agent-"):-len(".key")]
        self.assertEqual(khash, key_hash(key))
        self.assertEqual(recover(eip712_digest(CHAIN, BINDER, ACCOUNT, 5, TRADER, khash), signature), self.key.address)
        # Committed: nothing until the delay is over, then the key is shown.
        self.binder.sent.clear()
        self.binder.ask(RENEW, rid=5, khash=khash, activates=NOON + 10)
        self.serve(now=NOON + 9)
        self.assertEqual(self.binder.sent, [])
        self.serve(now=NOON + 10)
        self.assertEqual(self.binder.sent, [(BINDER, "reveal", [ACCOUNT, key])])
        self.assertEqual(len(self.keys()), 1, "no new key for a reveal")

    def test_a_committed_key_that_is_gone_is_an_alarm(self):
        self.binder.ask(RENEW, rid=5, khash=key_hash(TRADER), activates=NOON - 1)
        self.serve()
        self.assertEqual(self.binder.sent, [])
        self.assertIn("committed_key_missing", self.binder.events())

    def test_a_send_that_failed_is_tried_again_with_the_same_key(self):
        self.binder.ask(START, rid=2)
        self.serve()
        self.serve()
        first, second = (args[1] for _, _, args in self.binder.sent)
        self.assertEqual(first, second)
        self.assertEqual(len(self.keys()), 1)

    def test_a_key_spoiled_or_taken_meanwhile_is_replaced(self):
        self.binder.ask(START, rid=2)
        self.serve()
        spoiled = self.binder.sent[-1][2][1]
        self.binder.roles[spoiled] = "user"
        self.serve()
        fresh = self.binder.sent[-1][2][1]
        self.assertNotEqual(fresh, spoiled)
        self.assertIn("made_key_unusable", self.binder.events())
        self.binder.bindings[fresh] = 2
        self.serve()
        self.assertNotIn(self.binder.sent[-1][2][1], (spoiled, fresh))

    def test_a_new_request_gets_a_new_key(self):
        self.binder.ask(START, rid=2)
        self.serve()
        self.binder.ask(REVOKED, rid=3)
        self.serve()
        first, second = (args[1] for _, _, args in self.binder.sent)
        self.assertNotEqual(first, second)

    def test_a_dry_run_makes_no_key_and_sends_nothing(self):
        self.binder.ask(START)
        self.serve(dry=True)
        self.assertEqual((self.binder.sent, self.keys()), ([], []))
        self.assertIn("would_bind", self.binder.events())


class TheKeeperCallsIt(KeeperTest):
    """Where the keeper calls the relay: an account on a factory with a key binder has `keyAskedAt`; one before it
    reverts the read and is served as before (demo2, demo3)."""

    def setUp(self):
        super().setUp()
        self.served: list[str] = []
        fake = mock.Mock()
        fake.serve.side_effect = lambda wallet, account, now, dry: self.served.append(account.lower())
        patcher = mock.patch.object(keeper, "RELAY", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_pass(self):
        self.make().one_pass()

    def test_a_challenge_that_asked_is_answered_and_not_activated(self):
        self.follow_a(status=keeper.CREATED, keyAskedAt=NOON - 60, capitalArrived=True)
        del self.chain.challenges[CHALLENGE_A.lower()]["keySpoiled"]
        self.run_pass()
        self.assertEqual(self.served, [CHALLENGE_A.lower()])
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [])

    def test_a_start_window_after_the_request_it_is_aborted(self):
        self.follow_a(status=keeper.CREATED, keyAskedAt=NOON - keeper.START_WINDOW, capitalArrived=True)
        self.run_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [], "not a second sooner")
        self.chain.challenges[CHALLENGE_A.lower()]["keyAskedAt"] = NOON - keeper.START_WINDOW - 1
        self.served.clear()
        self.run_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [("abort", [])])
        self.assertEqual(self.served, [])

    def test_a_challenge_on_a_binder_activates_with_no_spoiled_key_to_read(self):
        self.follow_a(status=keeper.CREATED, keyAskedAt=0, capitalArrived=True)
        del self.chain.challenges[CHALLENGE_A.lower()]["keySpoiled"]
        self.run_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [("activate", [])])
        self.assertEqual(self.served, [])

    def test_an_account_before_the_binder_is_served_as_before(self):
        self.follow_a(status=keeper.CREATED, keySpoiled=True)
        self.run_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [("abort", [])])
        self.assertEqual(self.served, [])

    def test_a_trading_account_that_asked_is_answered_and_not_renewed_again(self):
        self.follow_a(status=keeper.ACTIVE, keyAskedAt=NOON - 60, agentKey=keeper.ZERO, agentValidUntil=NOON * 1000)
        self.run_pass()
        self.assertEqual(self.served, [CHALLENGE_A.lower()])
        self.assertNotIn(("renewAgent", []), self.chain.calls_to(CHALLENGE_A))
        # Answered: the renewal is the keeper's again.
        self.chain.challenges[CHALLENGE_A.lower()]["keyAskedAt"] = 0
        self.chain.challenges[CHALLENGE_A.lower()]["agentKey"] = "0x" + "a9" * 20
        self.run_pass()
        self.assertIn(("renewAgent", []), self.chain.calls_to(CHALLENGE_A))

    def test_a_passed_pool_that_asked_is_answered_and_not_asked_again(self):
        self.chain.add_pool(POOL_A, stage=keeper.PASSED_AWAITING_KEY, keyAskedAt=NOON - 60)
        self.chain.logs.append(challenge_log(10, POOL_A))
        self.run_pass()
        self.assertEqual(self.served, [POOL_A.lower()])
        self.assertEqual(self.chain.calls_to(POOL_A), [])

    def test_a_passed_pool_that_has_not_asked_asks(self):
        self.chain.add_pool(POOL_A, stage=keeper.PASSED_AWAITING_KEY, keyAskedAt=0, ownerRun=False)
        self.chain.logs.append(challenge_log(10, POOL_A))
        self.run_pass()
        self.assertEqual(self.chain.calls_to(POOL_A), [("openFundedStage", [])])
        self.assertEqual(self.served, [])

    def test_the_binder_read_is_asked_once_of_an_account_before_it(self):
        self.follow_a(status=keeper.CREATED)
        reads = []
        real = self.chain.call_view

        def counting(to, signature, *rest):
            reads.append(signature)
            return real(to, signature, *rest)

        self.chain.call_view = counting
        self.run_pass()
        self.run_pass()
        self.assertEqual(reads.count("keyAskedAt()"), 1)


class TheRelayTheKeeperMakes(KeeperTest):
    def test_with_no_publisher_given_it_says_so_and_sends_nothing(self):
        relay = keeper.relay()
        self.assertIsNone(relay.publisher)
        self.assertIs(keeper.relay(), relay, "one a run")


if __name__ == "__main__":
    unittest.main()
