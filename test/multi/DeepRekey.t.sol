// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";

import {Rules, Cancel, Breach} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {RuledAccount} from "../../src/RuledAccount.sol";
import {KeyRegistry} from "../../src/KeyRegistry.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The deep audit's F10 (16 October 2026), turned round: a funded seat whose key was spoiled in
/// the block it was bound takes a fresh key (`Pool.rekey`, anyone), so the trader keeps the stage
/// they earned and the seat's money works. On f8bc60aa nobody could end or mend such a seat before
/// its idle limit, up to 90 days. The stand and the spoiling are the audit's (SpoiledKeyDeadSeat),
/// its helpers copied so that the pool suite does not run twice.
contract DeepRekeyTest is PoolHarness {
    uint64 constant UP = 786920; // BTC +3%

    PaperExam examImpl;
    address alice = makeAddr("alice");
    address attacker = makeAddr("attacker");


    function _examImpl() internal returns (address) {
        if (address(examImpl) == address(0)) examImpl = new PaperExam();
        return address(examImpl);
    }

    function _plan(AfterTarget mode) internal pure returns (Plan memory p) {
        p.menu = new Size[](3);
        p.menu[0] = Size({size: 5e6, price: 1});
        p.menu[1] = Size({size: 10e6, price: 1});
        p.menu[2] = Size({size: 25e6, price: 1});
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
        p.fundedTargetBps = mode == AfterTarget.TraderStops ? 0 : 1000;
        p.traderShareBps = 8000;
        p.afterTarget = mode;
        p.inWorkBps = 8000;
        p.maxSeats = 8;
        p.claimWindow = 7 days;
        p.term = 365 days;
        p.fundedIdle = 30 days;
        p.termsBook = TERMS_BOOK;
    }

    function _planOf(MultiPool m) internal view returns (Plan memory) {
        return abi.decode(CodeStore.read(m.planData()), (Plan));
    }

    function _pool(Plan memory p, uint64 spot1e8) internal returns (MultiPool m) {
        m = new MultiPool(investor, factory, _examImpl(), _tie(p), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, spot1e8);
        vm.prank(investor);
        m.begin(TERMS);
    }

    function _buy(MultiPool m, address who, uint256 line) internal returns (PaperExam e) {
        uint256 price = _planOf(m).menu[line].price + factory.challengeFee();
        deal(address(usdc), who, price);
        vm.startPrank(who);
        usdc.approve(address(m), price);
        e = PaperExam(m.buy(line));
        vm.stopPrank();
    }

    /// Four times the exam's balance long, 3% up, closed: about 12%, past the 10% target.
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
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Passed));
    }

    function _settle(Pool seat) internal {
        (Cancel[] memory c, uint32[] memory a) = _none();
        for (uint256 i = 0; i < 8 && seat.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            seat.settleFunded(c, a);
        }
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Idle));
    }

    function _seatState(MultiPool m, address seat) internal view returns (MultiPool.SeatState s, address who) {
        (, s,, who,,,,) = m.seatInfo(seat);
    }

    function _examState(MultiPool m, address exam) internal view returns (MultiPool.ExamState s, address seat) {
        (, s, seat,) = m.examInfo(exam);
    }

    /// Alice passes and claims; the seat is Armed and its money has landed. Returns the exam,
    /// the seat, and the key the attacker predicts: the top of the registry's public free list.
    function _armed(MultiPool m) internal returns (PaperExam e, Pool seat, address nextKey) {
        e = _buy(m, alice, 2);
        _pass(e, alice);
        vm.prank(alice);
        seat = Pool(m.claim(address(e)));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(seat)), 25e8, "the seat's money is there");
        // No challenge was ever sold on this stand, so every published key is still free and
        // `assign` takes the last one published (KeyRegistry.sol:126). KeyPublished events and
        // `freeCount()` are public: the attacker knows the key before the block.
        assertEq(registry.freeCount(), keys.length);
        nextKey = keys[keys.length - 1];
        assertEq(uint8(registry.bindingOf(nextKey).state), uint8(KeyRegistry.State.Free));
        assertFalse(PrecompileLib.coreUserExists(nextKey), "the key has no account yet");
    }

    /// The attacker calls `open` (anyone may) and lands 1 USDC on the predicted key AFTER the
    /// start-of-block read in KeyRegistry.assign and BEFORE the CoreWriter action executes.
    function _openOnASpoiledKey(MultiPool m, Pool seat, address nextKey) internal returns (uint64 openedAt) {
        vm.prank(attacker);
        m.open(address(seat));
        openedAt = uint64(block.timestamp);
        // The seat is Funded on `nextKey`: the registry bound it, the pool set it as the agent.
        assertEq(seat.agentKey(), nextKey, "the predicted key was bound");
        assertTrue(registry.isBound(nextKey, address(seat), alice));
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Funded));
        // The spoiling transfer lands on HyperCore before the queued addApiWallet runs: the key
        // now has an account, and HyperCore does not take such an address as an agent
        // (spike question 8, cited at KeyRegistry.sol:17-19 and Pool.sol:331-334).
        CoreSimulatorLib.forceSpotBalance(nextKey, 0, 1e8);
        assertTrue(PrecompileLib.coreUserExists(nextKey), "the key has an account when the agent action runs");
        CoreSimulatorLib.nextBlock();
        // Nothing on chain records the refusal: the stage looks open.
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Funded));
        assertEq(seat.agentKey(), nextKey);
        assertEq(_equity(address(seat)), 25e6, "the pool's 25 USDC sit on the dead account");
    }

    /// The seat opened on a spoiled key takes the next free key; the trader keeps the stage.
    function test_deep_F10_aSeatOnASpoiledKeyTakesAFreshOne() public {
        MultiPool m = _pool(_plan(AfterTarget.AccountCloses), 100e8);
        CoreSimulatorLib.forceAccountActivation(alice);
        (, Pool seat, address spoiled) = _armed(m);
        _openOnASpoiledKey(m, seat, spoiled);
        vm.prank(alice);
        seat.rekey();
        address fresh = seat.agentKey();
        assertTrue(fresh != spoiled, "a fresh key");
        assertFalse(PrecompileLib.coreUserExists(fresh), "one HyperCore will take as an agent");
        assertTrue(registry.isBound(fresh, address(seat), alice), "bound to the seat, for alice");
        assertEq(uint8(registry.bindingOf(spoiled).state), uint8(KeyRegistry.State.Retired));
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Funded), "the stage goes on");
        assertEq(seat.fundedTrader(), alice);
        vm.prank(alice);
        vm.expectRevert(RuledAccount.KeyWorks.selector);
        seat.rekey(); // the fresh key works: it stays
    }

    /// The audit's N-2 (16 October 2026), turned round: a key HyperCore took keeps working after it
    /// gains an account, so a stranger's 1 USDC must not be enough to replace it, and the free list
    /// must not drain. Only the stage's trader may ask for a fresh key, in the stage's first day,
    /// twice at most. On ae0224cd a stranger spent every free key.
    function test_deep_AUD2_aStrangerCannotSpendTheKeys_andTheTraderOnlyTwiceInTheFirstDay() public {
        MultiPool m = _pool(_plan(AfterTarget.AccountCloses), 100e8);
        CoreSimulatorLib.forceAccountActivation(alice);
        (, Pool seat, address k) = _armed(m);
        vm.prank(stranger);
        m.open(address(seat));
        CoreSimulatorLib.nextBlock();
        uint256 free0 = registry.freeCount();
        CoreSimulatorLib.forceSpotBalance(k, 0, 1e8); // the stranger's 1 USDC on a working key
        vm.prank(attacker);
        vm.expectRevert(Pool.NotAllowed.selector);
        seat.rekey();
        assertEq(seat.agentKey(), k, "the trader's key stays");
        assertEq(registry.freeCount(), free0, "no free key spent");
        // The trader may ask, twice; then no more.
        for (uint256 i = 0; i < 2; ++i) {
            CoreSimulatorLib.forceSpotBalance(seat.agentKey(), 0, 1e8);
            vm.prank(alice);
            seat.rekey();
        }
        CoreSimulatorLib.forceSpotBalance(seat.agentKey(), 0, 1e8);
        vm.prank(alice);
        vm.expectRevert(RuledAccount.RekeyClosed.selector);
        seat.rekey();
        assertEq(registry.freeCount(), free0 - 2);
    }

    /// After the stage's first day no fresh key is taken.
    function test_deep_AUD2_noFreshKeyAfterTheFirstDay() public {
        MultiPool m = _pool(_plan(AfterTarget.AccountCloses), 100e8);
        CoreSimulatorLib.forceAccountActivation(alice);
        (, Pool seat, address spoiled) = _armed(m);
        _openOnASpoiledKey(m, seat, spoiled);
        vm.warp(block.timestamp + 1 days + 1);
        vm.prank(alice);
        vm.expectRevert(RuledAccount.RekeyClosed.selector);
        seat.rekey();
    }

    /// A seat whose key works keeps it.
    function test_deep_F10_aWorkingKeyStays() public {
        MultiPool m = _pool(_plan(AfterTarget.AccountCloses), 100e8);
        CoreSimulatorLib.forceAccountActivation(alice);
        (, Pool seat,) = _armed(m);
        m.open(address(seat));
        CoreSimulatorLib.nextBlock();
        vm.prank(alice);
        vm.expectRevert(RuledAccount.KeyWorks.selector);
        seat.rekey();
    }
}
