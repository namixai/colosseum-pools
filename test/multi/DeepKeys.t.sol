// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {Cancel} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {KeyRegistry} from "../../src/KeyRegistry.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The deep audit's key findings (cloud N2, N3, 16 October 2026), turned round, and the MultiPool
/// side of the audit's K-3.
///
/// N3: a pool sold exams with no regard to the registry's keys, which every funded account retires
/// for good; once they ran out, passes were sold that no key could open. Now a sale needs a free
/// key for every seat the pool has spoken for, and `room()` says so.
///
/// N2: more spoiled keys on top of the list than one `assign` skips blocked every opening until
/// someone purged them (the keeper now does), and a trader whose account could not open had paid
/// for nothing. Now a seat freed before its account opened hands its trader back what the pool
/// kept of the price.
///
/// The stand is the audit's (N2_MultiPoolAssignBlock), its helpers copied so that the MultiPool
/// suite does not run twice; the price is below the seat's size, as it is in a real plan.
contract DeepKeysTest is PoolHarness {
    uint64 constant UP = 786920; // BTC +3%
    uint64 constant PRICE = 2e6;

    PaperExam examImpl;
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address attacker = makeAddr("attacker");

    function _examImpl() internal returns (address) {
        if (address(examImpl) == address(0)) examImpl = new PaperExam();
        return address(examImpl);
    }

    function _plan() internal pure returns (Plan memory p) {
        p.menu = new Size[](3);
        p.menu[0] = Size({size: 5e6, price: PRICE});
        p.menu[1] = Size({size: 10e6, price: PRICE});
        p.menu[2] = Size({size: 25e6, price: PRICE});
        p.examRules = _rules();
        uint16[] memory targets = new uint16[](1);
        targets[0] = 1000;
        p.exam = ExamTerms({
            capital: 0,
            targetsBps: targets,
            stepDuration: 7 days,
            maxIdle: 0,
            minTradingDays: 0,
            costPpm: 0,
            fillDelay: 1,
            fillWindow: 5
        });
        p.fundedRules = _rules();
        p.fundedTargetBps = 1000;
        p.traderShareBps = 8000;
        p.afterTarget = AfterTarget.AccountCloses;
        p.inWorkBps = 8000;
        p.maxSeats = 8;
        p.claimWindow = 7 days;
        p.term = 365 days;
        p.fundedIdle = 30 days;
        p.termsBook = TERMS_BOOK;
    }

    function _pool() internal returns (MultiPool m) {
        m = new MultiPool(investor, factory, _examImpl(), _tie(_plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 100e8);
        vm.prank(investor);
        m.begin(TERMS);
    }

    /// Spoils every free key but the first `keep` (the bottom of the list) and purges them, so
    /// that `keep` free keys are left.
    function _leaveKeys(uint256 keep) internal {
        for (uint256 i = keep; i < keys.length; ++i) {
            if (registry.bindingOf(keys[i]).state == KeyRegistry.State.Free) {
                CoreSimulatorLib.forceAccountActivation(keys[i]);
            }
        }
        registry.purgeSpoiled(keys.length);
        assertEq(registry.freeCount(), keep);
    }

    /// Gives the top `count` free keys a HyperCore account: `assign` will find them spoiled.
    function _spoilTop(uint256 count) internal {
        for (uint256 i = 0; i < count; ++i) {
            CoreSimulatorLib.forceAccountActivation(keys[keys.length - 1 - i]);
        }
    }

    function _buy(MultiPool m, address who, uint256 line) internal returns (PaperExam e) {
        deal(address(usdc), who, PRICE);
        vm.startPrank(who);
        usdc.approve(address(m), PRICE);
        e = PaperExam(m.buy(line));
        vm.stopPrank();
    }

    function _pass(PaperExam e, address who) internal {
        (,, uint64 capital,) = e.result();
        uint64 sz = (capital * 4) / BTC_MARK;
        vm.prank(who);
        e.place(BTC, true, sz);
        vm.roll(block.number + 1);
        e.fill();
        CoreSimulatorLib.setMarkPx(BTC, UP);
        vm.prank(who);
        e.place(BTC, false, sz);
        vm.roll(block.number + 1);
        e.fill();
        vm.prank(who);
        e.graduate();
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
    }

    function _seatState(MultiPool m, address seat) internal view returns (MultiPool.SeatState s) {
        (, s,,,,,,) = m.seatInfo(seat);
    }

    /// alice buys line 2 (25 USDC), passes, claims: the seat is armed and its money has landed.
    function _armed(MultiPool m) internal returns (Pool seat) {
        CoreSimulatorLib.forceAccountActivation(alice);
        PaperExam e = _buy(m, alice, 2);
        _pass(e, alice);
        vm.prank(alice);
        seat = Pool(m.claim(address(e)));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(seat)), 25e8);
    }

    // ── N3: no sale without a key ───────────────────────────────────────────────────

    /// The cloud's N3, turned round: two free keys, two exams; the third is not sold, and `room()`
    /// says there is none, though the share and the seats would allow it.
    function test_deep_N3_noSaleWithoutAFreeKeyForEverySeatSpokenFor() public {
        MultiPool m = _pool();
        _leaveKeys(2);
        assertEq(m.room(0), 2, "as many as there are keys");
        _buy(m, alice, 0);
        _buy(m, bob, 0);
        assertEq(m.room(0), 0, "no key left for a third");
        deal(address(usdc), attacker, PRICE);
        vm.startPrank(attacker);
        usdc.approve(address(m), PRICE);
        vm.expectRevert(MultiPool.NoKey.selector);
        m.buy(0);
        vm.stopPrank();
        assertEq(usdc.balanceOf(attacker), PRICE, "and no price taken");

        // A key published by the operator lets the next sale through.
        address[] memory more = new address[](1);
        more[0] = makeAddr("enclave-key-late");
        vm.prank(operator);
        registry.publish(more);
        assertEq(m.room(0), 1);
        _buy(m, attacker, 0);
    }

    /// A funded seat holds its key, and still counts: the registry keeps a key for every seat the
    /// pool has spoken for, so a pass is never sold on a key a funded account will retire.
    function test_deep_N3_aFundedSeatStillCounts() public {
        MultiPool m = _pool();
        _leaveKeys(2);
        Pool seat = _armed(m);
        m.open(address(seat));
        assertEq(registry.freeCount(), 1, "the funded account took one");
        assertEq(m.room(0), 0, "one free key, one seat spoken for");
    }

    // ── N2: a pass whose account never opens is handed its price back ─────────────

    /// The cloud's N2, turned round: seventeen spoiled keys on top block the opening. The trader
    /// claimed; the seat cannot open within the claim window; whoever frees it then hands the
    /// trader back the price the pool kept, and the pool its size.
    function test_deep_N2_aSeatThatNeverOpensHandsItsTraderThePriceBack() public {
        MultiPool m = _pool();
        Pool seat = _armed(m);
        _spoilTop(registry.MAX_SPOILED_SKIPS() + 1);
        vm.expectRevert(KeyRegistry.TooManySpoiledKeys.selector);
        m.open(address(seat));
        vm.expectRevert(MultiPool.BadSeat.selector);
        m.finish(address(seat)); // the claim window is the account's to open in
        uint256 before = m.value();
        uint64 aliceBefore = _spot(alice);
        vm.warp(block.timestamp + 7 days + 1);
        vm.expectEmit(true, true, false, true, address(m));
        emit MultiPool.PriceReturned(address(seat), alice, uint64(PRICE) * 100);
        m.finish(address(seat));
        assertEq(uint8(_seatState(m, address(seat))), uint8(MultiPool.SeatState.Free));
        assertEq(m.value(), before - uint256(PRICE) * 100, "the price leaves the pool's value at once");
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(alice) - aliceBefore, uint256(PRICE) * 100, "alice has her price back");
        assertEq(_spot(address(seat)), 0);
        // Once the minute the pool counts what it sent against itself has passed, as after any
        // payment of its own; meanwhile no price is struck on it (PoolShares' PoolPaying).
        vm.warp(block.timestamp + 2 minutes);
        assertEq(m.value(), before - uint256(PRICE) * 100, "and the size is back in the pool");
    }

    /// A seat whose account opened gives nothing back when it is freed: the price was for that.
    function test_deep_N2_aSeatThatOpenedGivesNothingBack() public {
        MultiPool m = _pool();
        Pool seat = _armed(m);
        m.open(address(seat));
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 31 days);
        (Cancel[] memory c, uint32[] memory a) = _none();
        m.closeIdle(address(seat), c, a, SALT);
        for (uint256 i = 0; i < 8 && seat.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            seat.settleFunded(c, a);
        }
        uint64 aliceBefore = _spot(alice);
        m.finish(address(seat));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(alice), aliceBefore, "nothing handed back");
    }

    /// A seat sends on HyperCore to anyone only on its pool's word, and only while no trader holds it.
    function test_deep_N2_onlyThePoolSendsFromASeat_andNotFromAFundedOne() public {
        MultiPool m = _pool();
        Pool seat = _armed(m);
        vm.prank(attacker);
        vm.expectRevert(Pool.NotOwner.selector);
        seat.sendOnCore(attacker, 1e8);
        m.open(address(seat));
        vm.prank(address(m));
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Funded));
        seat.sendOnCore(attacker, 1e8);
    }

    /// The way out of the block, which anyone may take and the keeper takes: purge the spoiled
    /// keys, and the seat opens on the live one underneath.
    function test_deep_N2_control_aPurgeOpensTheSeat() public {
        MultiPool m = _pool();
        Pool seat = _armed(m);
        _spoilTop(registry.MAX_SPOILED_SKIPS() + 1);
        vm.expectRevert(KeyRegistry.TooManySpoiledKeys.selector);
        m.open(address(seat));
        vm.prank(attacker);
        registry.purgeSpoiled(type(uint256).max);
        m.open(address(seat));
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Funded));
    }

    // ── K-3: a MultiPool the factory knows ───────────────────────────────────────────

    /// With the operator's word missing, a MultiPool sells and arms but opens nothing; with it,
    /// the same seat opens.
    function test_deep_K3_aMultiPoolOpensOnlyOnceTheFactoryKnowsIt() public {
        vm.clearMockedCalls();
        MultiPool m = _pool();
        Pool seat = _armed(m);
        vm.expectRevert(Pool.NotOwnerRun.selector);
        m.open(address(seat));
        vm.prank(operator);
        factory.setMultiPool(address(m), true);
        m.open(address(seat));
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Funded));
    }
}
