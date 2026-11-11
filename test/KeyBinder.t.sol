// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {KeyBinder, IKeyAccounts, IBoundAccount} from "../src/KeyBinder.sol";

contract FakeAccounts is IKeyAccounts {
    mapping(address => bool) public isAccount;

    function set(address a, bool v) external {
        isAccount[a] = v;
    }
}

/// An account as the binder sees one: it asks, cancels and retires, and records what reached it.
contract FakeAccount is IBoundAccount {
    KeyBinder.Purpose public lastPurpose;
    address public lastKey;
    address public revoked;
    bytes32 public revokedSalt;
    uint256 public bound;

    function ask(KeyBinder b, address trader, KeyBinder.Purpose purpose) external returns (uint64) {
        return b.request(trader, purpose);
    }

    function cancel(KeyBinder b) external {
        b.cancel();
    }

    function retire(KeyBinder b, address key) external {
        b.retire(key);
    }

    function onKeyBound(address key, KeyBinder.Purpose purpose) external {
        lastKey = key;
        lastPurpose = purpose;
        ++bound;
    }

    function onKeyRevoked(address key, bytes32 salt) external {
        revoked = key;
        revokedSalt = salt;
    }
}

/// The key binder alone: each function against each role, each request state, the publisher's
/// signature in every way it can be wrong, the pause, the daily limit and the renewal's delay.
contract KeyBinderTest is Test {
    HyperCore hyperCore;
    KeyBinder binder;
    FakeAccounts accounts;
    FakeAccount account;
    FakeAccount other;

    address owner = makeAddr("owner");
    address guardian = makeAddr("guardian");
    address stranger = makeAddr("stranger");
    address trader = makeAddr("trader");
    uint256 constant PUBLISHER_PK = 0xA11CE;
    address publisher = vm.addr(PUBLISHER_PK);
    bytes32 constant PCR = keccak256("pcr0-2");
    bytes32 constant DOC = keccak256("attestation document");
    uint256 keySeq;

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        binder = new KeyBinder(owner, 50, 1 days);
        accounts = new FakeAccounts();
        account = new FakeAccount();
        other = new FakeAccount();
        accounts.set(address(account), true);
        accounts.set(address(other), true);
        vm.startPrank(owner);
        binder.setAccountSource(accounts);
        binder.setPublisher(publisher, PCR, DOC);
        binder.setGuardian(guardian);
        vm.stopPrank();
    }

    function _key() internal returns (address) {
        return makeAddr(string.concat("key-", vm.toString(keySeq++)));
    }

    function _sig(uint256 pk, KeyBinder b, address acct, address key) internal view returns (bytes memory) {
        KeyBinder.Request memory r = b.requestOf(acct);
        (uint8 v, bytes32 rr, bytes32 s) = vm.sign(pk, b.digest(acct, r.id, r.trader, keccak256(abi.encode(key))));
        return abi.encodePacked(rr, s, v);
    }

    function _asked(KeyBinder.Purpose purpose) internal returns (address key, bytes memory sig) {
        account.ask(binder, trader, purpose);
        key = _key();
        sig = _sig(PUBLISHER_PK, binder, address(account), key);
    }

    // ── roles ────────────────────────────────────────────────────────────────────────

    function test_ownerAlone_appointsSetsAndLifts() public {
        address[2] memory notOwner = [guardian, stranger];
        for (uint256 i = 0; i < 2; ++i) {
            vm.startPrank(notOwner[i]);
            vm.expectRevert(KeyBinder.NotOwner.selector);
            binder.setPublisher(stranger, PCR, DOC);
            vm.expectRevert(KeyBinder.NotOwner.selector);
            binder.setGuardian(stranger);
            vm.expectRevert(KeyBinder.NotOwner.selector);
            binder.setMaxBindsPerDay(1000);
            vm.expectRevert(KeyBinder.NotOwner.selector);
            binder.setRenewDelay(0);
            vm.expectRevert(KeyBinder.NotOwner.selector);
            binder.unpause();
            vm.expectRevert(KeyBinder.NotOwner.selector);
            binder.transferOwnership(notOwner[i]);
            vm.expectRevert(KeyBinder.NotOwner.selector);
            binder.setAccountSource(accounts);
            vm.stopPrank();
        }
        vm.startPrank(owner);
        binder.setMaxBindsPerDay(1000);
        binder.setRenewDelay(0);
        binder.setGuardian(stranger);
        vm.stopPrank();
        assertEq(binder.maxBindsPerDay(), 1000);
        assertEq(binder.renewDelay(), 0);
        assertEq(binder.guardian(), stranger);
    }

    /// Debug mode reports zero measurements: a publisher is appointed only with both hashes, which
    /// the event carries for anyone to check against the published document.
    function test_setPublisher_refusesZeroHashes_andTheEventCarriesThem() public {
        vm.startPrank(owner);
        vm.expectRevert(KeyBinder.ZeroHash.selector);
        binder.setPublisher(stranger, bytes32(0), DOC);
        vm.expectRevert(KeyBinder.ZeroHash.selector);
        binder.setPublisher(stranger, PCR, bytes32(0));
        vm.expectRevert(KeyBinder.ZeroAddress.selector);
        binder.setPublisher(address(0), PCR, DOC);
        vm.expectEmit(address(binder));
        emit KeyBinder.PublisherSet(stranger, PCR, DOC);
        binder.setPublisher(stranger, PCR, DOC);
        vm.stopPrank();
        assertEq(binder.publisher(), stranger);
    }

    /// The guardian stops and nothing more: pause, a lower limit, a revocation. Raising the limit
    /// and lifting the pause are the owner's.
    function test_guardian_onlyStops() public {
        vm.prank(stranger);
        vm.expectRevert(KeyBinder.NotGuardian.selector);
        binder.pause();
        vm.prank(stranger);
        vm.expectRevert(KeyBinder.NotGuardian.selector);
        binder.lowerMaxBindsPerDay(1);
        vm.prank(stranger);
        vm.expectRevert(KeyBinder.NotGuardian.selector);
        binder.revoke(address(1), bytes32(0));

        vm.prank(guardian);
        vm.expectRevert(KeyBinder.NotLower.selector);
        binder.lowerMaxBindsPerDay(50);
        vm.prank(guardian);
        binder.lowerMaxBindsPerDay(10);
        assertEq(binder.maxBindsPerDay(), 10);
        vm.prank(guardian);
        binder.pause();
        assertTrue(binder.paused());
        vm.prank(guardian);
        vm.expectRevert(KeyBinder.NotOwner.selector);
        binder.unpause();
        vm.prank(owner);
        binder.pause(); // the owner may stop too
        vm.prank(owner);
        binder.unpause();
        assertFalse(binder.paused());
    }

    function test_ownership_isTwoStep() public {
        vm.prank(owner);
        binder.transferOwnership(stranger);
        assertEq(binder.owner(), owner, "not until it is accepted");
        vm.prank(guardian);
        vm.expectRevert(KeyBinder.NotPendingOwner.selector);
        binder.acceptOwnership();
        vm.prank(stranger);
        binder.acceptOwnership();
        assertEq(binder.owner(), stranger);
        assertEq(binder.pendingOwner(), address(0));
    }

    function test_accountSource_isSetOnce() public {
        vm.prank(owner);
        vm.expectRevert(KeyBinder.SourceAlreadySet.selector);
        binder.setAccountSource(accounts);
    }

    function test_renewDelay_isBounded() public {
        uint64 max = binder.MAX_RENEW_DELAY();
        vm.prank(owner);
        vm.expectRevert(KeyBinder.DelayTooLong.selector);
        binder.setRenewDelay(max + 1);
        vm.expectRevert(KeyBinder.DelayTooLong.selector);
        new KeyBinder(owner, 1, max + 1);
        vm.expectRevert(KeyBinder.ZeroAddress.selector);
        new KeyBinder(address(0), 1, 0);
    }

    // ── requests ─────────────────────────────────────────────────────────────────────

    function test_request_accountsOnly_oneAtATime_andCancelled() public {
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.NotAnAccount.selector, stranger));
        binder.request(trader, KeyBinder.Purpose.Start);
        vm.expectRevert(KeyBinder.BadPurpose.selector);
        account.ask(binder, trader, KeyBinder.Purpose.None);
        assertEq(account.ask(binder, trader, KeyBinder.Purpose.Start), 1);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.Asked.selector, address(account)));
        account.ask(binder, trader, KeyBinder.Purpose.Funded);
        KeyBinder.Request memory r = binder.requestOf(address(account));
        assertEq(uint8(r.purpose), uint8(KeyBinder.Purpose.Start));
        assertEq(r.trader, trader);
        assertEq(r.openedAt, block.timestamp);
        account.cancel(binder);
        assertEq(uint8(binder.requestOf(address(account)).purpose), uint8(KeyBinder.Purpose.None));
        account.cancel(binder); // nothing to cancel: no refusal
        assertEq(account.ask(binder, trader, KeyBinder.Purpose.Start), 2, "numbers only grow");
    }

    // ── the publisher's signature ────────────────────────────────────────────────────

    function test_bind_bindsTheSignedKey_andTheAccountApprovesIt() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        vm.prank(stranger); // anyone carries it
        binder.bind(address(account), key, sig);
        assertEq(account.lastKey(), key);
        assertEq(uint8(account.lastPurpose()), uint8(KeyBinder.Purpose.Start));
        assertTrue(binder.isBound(key, address(account), trader));
        assertEq(binder.keyOf(address(account)), key);
        assertEq(uint8(binder.requestOf(address(account)).purpose), uint8(KeyBinder.Purpose.None));
        assertEq(binder.bindsToday(), 1);
    }

    function test_bind_refusesAnotherSigner() public {
        account.ask(binder, trader, KeyBinder.Purpose.Start);
        address key = _key();
        bytes memory sig = _sig(0xBAD, binder, address(account), key);
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(account), key, sig);
    }

    /// The request number is in what is signed, so a signature dies with its request: cancelled
    /// and asked again, the old signature fits nothing.
    function test_bind_aSignatureIsGoodForItsRequestOnce() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        account.cancel(binder);
        account.ask(binder, trader, KeyBinder.Purpose.Start);
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(account), key, sig);
        // And once used, it cannot bind again.
        bytes memory fresh = _sig(PUBLISHER_PK, binder, address(account), key);
        binder.bind(address(account), key, fresh);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.NotAsked.selector, address(account)));
        binder.bind(address(account), key, fresh);
        account.ask(binder, trader, KeyBinder.Purpose.Renew);
        vm.prank(owner);
        binder.setRenewDelay(0);
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(account), key, fresh);
    }

    /// A signature under another chain's id fits nothing here.
    function test_bind_aSignatureForAnotherChainFitsNothing() public {
        account.ask(binder, trader, KeyBinder.Purpose.Start);
        address key = _key();
        uint256 here = block.chainid;
        vm.chainId(999);
        bytes memory sig = _sig(PUBLISHER_PK, binder, address(account), key);
        vm.chainId(here);
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(account), key, sig);
    }

    /// A signature for another deployment's binder, with the same publisher and the same account,
    /// fits nothing here: the binder's address is in what is signed.
    function test_bind_aSignatureForAnotherBinderFitsNothing() public {
        KeyBinder second = new KeyBinder(owner, 50, 0);
        vm.startPrank(owner);
        second.setAccountSource(accounts);
        second.setPublisher(publisher, PCR, DOC);
        vm.stopPrank();
        account.ask(binder, trader, KeyBinder.Purpose.Start);
        account.ask(second, trader, KeyBinder.Purpose.Start);
        address key = _key();
        bytes memory sig = _sig(PUBLISHER_PK, second, address(account), key);
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(account), key, sig);
        second.bind(address(account), key, sig);
    }

    /// The publisher replaced: what the old one signed and nobody carried in yet fits nothing.
    function test_bind_theOldPublishersSignatureFitsNothingAfterTheChange() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        uint256 nextPk = 0xB0B;
        vm.prank(owner);
        binder.setPublisher(vm.addr(nextPk), keccak256("pcr of the new image"), keccak256("its document"));
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(account), key, sig);
        binder.bind(address(account), key, _sig(nextPk, binder, address(account), key));
        assertEq(account.lastKey(), key);
    }

    /// The signature is for this account and this trader alone.
    function test_bind_notForAnotherAccount_norAnotherTrader() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        other.ask(binder, trader, KeyBinder.Purpose.Start);
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(other), key, sig);
        account.cancel(binder);
        account.ask(binder, stranger, KeyBinder.Purpose.Start);
        KeyBinder.Request memory r = binder.requestOf(address(account));
        (uint8 v, bytes32 rr, bytes32 s) =
            vm.sign(PUBLISHER_PK, binder.digest(address(account), r.id, trader, keccak256(abi.encode(key))));
        vm.expectRevert(KeyBinder.BadSignature.selector);
        binder.bind(address(account), key, abi.encodePacked(rr, s, v));
    }

    /// A key is new, without a HyperCore account, and never zero.
    function test_bind_onlyANewKeyWithNoAccount() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        binder.bind(address(account), key, sig);
        other.ask(binder, trader, KeyBinder.Purpose.Start);
        bytes memory again = _sig(PUBLISHER_PK, binder, address(other), key);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.KeyNotNew.selector, key));
        binder.bind(address(other), key, again);
        address spoiled = _key();
        CoreSimulatorLib.forceAccountActivation(spoiled);
        bytes memory s2 = _sig(PUBLISHER_PK, binder, address(other), spoiled);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.KeyExistsOnCore.selector, spoiled));
        binder.bind(address(other), spoiled, s2);
        bytes memory s3 = _sig(PUBLISHER_PK, binder, address(other), address(0));
        vm.expectRevert(KeyBinder.ZeroAddress.selector);
        binder.bind(address(other), address(0), s3);
    }

    /// A second key for an account retires the first, in the same transaction.
    function test_bind_aFreshKeyRetiresTheOld() public {
        vm.prank(owner);
        binder.setRenewDelay(0);
        (address first, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        binder.bind(address(account), first, sig);
        (address second, bytes memory sig2) = _asked(KeyBinder.Purpose.Renew);
        binder.bind(address(account), second, sig2);
        assertEq(uint8(binder.bindingOf(first).state), uint8(KeyBinder.State.Retired));
        assertTrue(binder.isBound(second, address(account), trader));
        assertEq(binder.keyOf(address(account)), second);
    }

    // ── the daily limit ──────────────────────────────────────────────────────────────

    function test_dailyLimit_countsBindsAndCommits_andStartsAgainEachDay() public {
        vm.prank(owner);
        binder.setMaxBindsPerDay(1);
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        binder.bind(address(account), key, sig);
        other.ask(binder, trader, KeyBinder.Purpose.Start);
        address k2 = _key();
        bytes memory s2 = _sig(PUBLISHER_PK, binder, address(other), k2);
        vm.expectRevert(KeyBinder.DailyLimit.selector);
        binder.bind(address(other), k2, s2);
        vm.warp((block.timestamp / 1 days + 1) * 1 days);
        binder.bind(address(other), k2, s2);
        assertEq(binder.bindsToday(), 1);
    }

    // ── the renewal's delay ──────────────────────────────────────────────────────────

    function test_renew_withTheDelay_goesByCommitAndReveal() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Renew);
        vm.expectRevert(KeyBinder.WrongStep.selector);
        binder.bind(address(account), key, sig);
        vm.expectRevert(KeyBinder.WrongStep.selector);
        binder.reveal(address(account), key); // nothing committed
        bytes32 h = keccak256(abi.encode(key));
        binder.commit(address(account), h, sig);
        vm.expectRevert(KeyBinder.WrongStep.selector);
        binder.commit(address(account), h, sig); // once
        uint64 at = binder.requestOf(address(account)).activatesAt;
        assertEq(at, block.timestamp + 1 days);
        vm.warp(at - 1);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.TooEarly.selector, at));
        binder.reveal(address(account), key);
        vm.warp(at);
        vm.expectRevert(KeyBinder.WrongKey.selector);
        binder.reveal(address(account), _key());
        assertEq(account.bound(), 0, "nothing before the reveal");
        binder.reveal(address(account), key);
        assertEq(account.lastKey(), key);
        assertEq(uint8(account.lastPurpose()), uint8(KeyBinder.Purpose.Renew));
        assertEq(binder.bindsToday(), 1, "the commitment counted, the reveal did not");
    }

    function test_commit_onlyForARenewal_andOnlyWithTheDelayOn() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        vm.expectRevert(KeyBinder.WrongStep.selector);
        binder.commit(address(account), keccak256(abi.encode(key)), sig);
        account.cancel(binder);
        account.ask(binder, trader, KeyBinder.Purpose.Renew);
        vm.prank(owner);
        binder.setRenewDelay(0);
        bytes memory s = _sig(PUBLISHER_PK, binder, address(account), key);
        vm.expectRevert(KeyBinder.WrongStep.selector);
        binder.commit(address(account), keccak256(abi.encode(key)), s);
        binder.bind(address(account), key, s);
    }

    // ── the pause ────────────────────────────────────────────────────────────────────

    /// The pause stops `bind`, `commit` and `reveal`; requests, cancels and retirements go on.
    function test_pause_stopsBindCommitAndReveal_notCancelNorRetire() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        binder.bind(address(account), key, sig);
        (address renewal, bytes memory rsig) = _asked(KeyBinder.Purpose.Renew);
        binder.commit(address(account), keccak256(abi.encode(renewal)), rsig);
        other.ask(binder, trader, KeyBinder.Purpose.Start);
        address k2 = _key();
        bytes memory s2 = _sig(PUBLISHER_PK, binder, address(other), k2);

        vm.prank(guardian);
        binder.pause();
        vm.expectRevert(KeyBinder.IsPaused.selector);
        binder.bind(address(other), k2, s2);
        vm.warp(block.timestamp + 1 days);
        vm.expectRevert(KeyBinder.IsPaused.selector);
        binder.reveal(address(account), renewal);
        other.cancel(binder);
        other.ask(binder, trader, KeyBinder.Purpose.Renew);
        bytes memory s3 = _sig(PUBLISHER_PK, binder, address(other), k2);
        vm.expectRevert(KeyBinder.IsPaused.selector);
        binder.commit(address(other), keccak256(abi.encode(k2)), s3);
        account.retire(binder, key);
        assertEq(uint8(binder.bindingOf(key).state), uint8(KeyBinder.State.Retired));
        other.cancel(binder);
        assertEq(uint8(binder.requestOf(address(other)).purpose), uint8(KeyBinder.Purpose.None));

        vm.prank(owner);
        binder.unpause();
        binder.reveal(address(account), renewal);
        assertEq(account.lastKey(), renewal);
    }

    // ── retire and revoke ────────────────────────────────────────────────────────────

    function test_retire_onlyByItsAccount_andForGood() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        binder.bind(address(account), key, sig);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.NotBoundHere.selector, key, address(other)));
        other.retire(binder, key);
        account.retire(binder, key);
        assertEq(binder.keyOf(address(account)), address(0));
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.NotBoundHere.selector, key, address(account)));
        account.retire(binder, key);
    }

    /// A revoked key is retired, the account's commitment in flight is void, and the account is told
    /// with the salt the guardian chose.
    function test_revoke_retiresTheKey_voidsTheCommitment_andTellsTheAccount() public {
        (address key, bytes memory sig) = _asked(KeyBinder.Purpose.Start);
        binder.bind(address(account), key, sig);
        (address renewal, bytes memory rsig) = _asked(KeyBinder.Purpose.Renew);
        binder.commit(address(account), keccak256(abi.encode(renewal)), rsig);
        bytes32 salt = keccak256("guardian's salt");
        vm.expectEmit(address(binder));
        emit KeyBinder.KeyRevoked(key, address(account), guardian);
        vm.prank(guardian);
        binder.revoke(key, salt);
        assertEq(uint8(binder.bindingOf(key).state), uint8(KeyBinder.State.Retired));
        assertEq(binder.keyOf(address(account)), address(0));
        assertEq(uint8(binder.requestOf(address(account)).purpose), uint8(KeyBinder.Purpose.None));
        assertEq(account.revoked(), key);
        assertEq(account.revokedSalt(), salt);
        vm.warp(block.timestamp + 1 days);
        vm.expectRevert(KeyBinder.WrongStep.selector);
        binder.reveal(address(account), renewal);
        vm.prank(guardian);
        vm.expectRevert(abi.encodeWithSelector(KeyBinder.NotBoundHere.selector, key, guardian));
        binder.revoke(key, salt);
    }
}
