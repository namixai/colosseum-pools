// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {Rules, Breach, Cancel} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {RuledAccount} from "../../src/RuledAccount.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The deep audit's F12 (16 October 2026), turned round: the day's snapshot is not taken while a
/// rule is broken, so midnight forgives nothing, whoever calls first. On f8bc60aa `checkpoint`
/// took the broken equity as the new day's base and the breach was refused. The stand is the
/// audit's (CheckpointForgivesDailyLoss).
contract DeepCheckpointTest is PoolHarness {
    uint64 constant ROUND_MARK = 1_000_000;
    uint64 constant UP = 786920; // BTC +3% over BTC_MARK, as in test/multi/MultiPool.t.sol
    uint16 constant DELAY = 2;

    PaperExam examImpl;
    address alice = makeAddr("alice");

    // ── helpers (copied from test/exam/PaperExam.t.sol and test/multi/MultiPool.t.sol) ───

    function _examImpl() internal returns (address) {
        if (address(examImpl) == address(0)) examImpl = new PaperExam();
        return address(examImpl);
    }

    function _targets(uint16 a) internal pure returns (uint16[] memory t) {
        t = new uint16[](1);
        t[0] = a;
    }

    function _examTerms() internal pure returns (ExamTerms memory) {
        return ExamTerms({
            capital: 100e6,
            targetsBps: _targets(1000),
            stepDuration: 7 days,
            maxIdle: 0,
            minTradingDays: 0,
            costPpm: 0,
            fillDelay: DELAY,
            fillWindow: 3
        });
    }

    /// An exam on 100 USDC at noon of a UTC day, long 100 units (100 USDC, 1x) at 100 000.0.
    function _examLongAtNoon() internal returns (PaperExam e, uint256 day) {
        CoreSimulatorLib.setMarkPx(BTC, ROUND_MARK);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);
        e = PaperExam(Clones.clone(_examImpl()));
        e.initialize(trader, _rules(), _examTerms());
        _examTrade(e, true, 100);
        day = block.timestamp / 1 days;
        assertEq(e.dayStartEquity(), 100e6);
    }

    function _examTrade(PaperExam e, bool isBuy, uint64 sz) internal {
        vm.prank(trader);
        e.place(BTC, isBuy, sz);
        vm.roll(block.number + DELAY);
        vm.prank(stranger);
        e.fill();
    }

    /// 23:59:30 of the exam's first day: -6%, 94 USDC. Under the day's line (95) and above the
    /// drawdown floor (90); 94 of notional on 94 of equity is 1x. DailyLoss, and nothing else.
    function _breakTheDayAt235930(PaperExam e, uint256 day) internal {
        vm.warp(day * 1 days + 23 hours + 59 minutes + 30 seconds);
        CoreSimulatorLib.setMarkPx(BTC, 940_000);
        assertEq(e.equity(), 94e6);
        assertEq(uint8(e.violation()), uint8(Breach.DailyLoss));
    }

    function _plan() internal pure returns (Plan memory p) {
        p.menu = new Size[](3);
        p.menu[0] = Size({size: 5e6, price: 1});
        p.menu[1] = Size({size: 10e6, price: 1});
        p.menu[2] = Size({size: 25e6, price: 1});
        p.examRules = _rules();
        p.exam = ExamTerms({
            capital: 0,
            targetsBps: _targets(1000),
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


    function _planOf(MultiPool m) internal view returns (Plan memory) {
        return abi.decode(CodeStore.read(m.planData()), (Plan));
    }

    function _pool() internal returns (MultiPool m) {
        m = new MultiPool(investor, factory, _examImpl(), _tie(_plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 100e8);
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

    function _seatState(MultiPool m, address seat) internal view returns (MultiPool.SeatState s, address who) {
        (, s,, who,,,,) = m.seatInfo(seat);
    }

    // ── the finding, turned round ────────────────────────────────────────────────────

    /// The keeper's old order, checkpoint then breach: the checkpoint is refused, the breach ends
    /// the exam for the daily loss.
    function test_deep_F12_aCheckpointDoesNotForgiveTheDaysBreach() public {
        (PaperExam e, uint256 day) = _examLongAtNoon();
        _breakTheDayAt235930(e, day);
        vm.warp((day + 1) * 1 days + 5);
        vm.roll(block.number + 5);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.RuleBroken.selector, Breach.DailyLoss));
        e.checkpoint();
        assertEq(e.dayStartEquity(), 100e6, "yesterday's base stands");
        vm.prank(stranger);
        e.breach();
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Breached));
        assertEq(uint8(e.endReason()), uint8(Breach.DailyLoss));
    }

    /// The trader's own checkpoint at midnight is refused too.
    function test_deep_F12_theTraderCannotForgiveTheirOwnBreach() public {
        (PaperExam e, uint256 day) = _examLongAtNoon();
        _breakTheDayAt235930(e, day);
        vm.warp((day + 1) * 1 days);
        vm.roll(block.number + 1);
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.RuleBroken.selector, Breach.DailyLoss));
        e.checkpoint();
        vm.prank(stranger);
        e.breach();
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Breached));
    }

    /// Control, as the audit had it: breach first ends the exam.
    function test_deep_F12_control_breachBeforeCheckpoint_endsTheExam() public {
        (PaperExam e, uint256 day) = _examLongAtNoon();
        _breakTheDayAt235930(e, day);
        vm.warp((day + 1) * 1 days + 5);
        vm.roll(block.number + 5);
        vm.prank(stranger);
        e.breach();
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Breached));
        assertEq(e.endEquity(), 94e6);
    }

    /// On a funded seat, where the money is the pool's: the checkpoint is refused while the day's
    /// rule is broken, and the breach stops the seat.
    function test_deep_F12_aFundedSeatsCheckpointDoesNotForgiveTheDay() public {
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);
        MultiPool m = _pool();
        PaperExam e = _buy(m, alice, 0);
        (,, uint64 capital,) = e.result();
        uint64 sz = (capital * 4) / BTC_MARK;
        vm.prank(alice);
        e.place(BTC, true, sz);
        vm.roll(block.number + 1);
        e.fill();
        CoreSimulatorLib.setMarkPx(BTC, UP);
        vm.prank(alice);
        e.place(BTC, false, sz);
        vm.roll(block.number + 1);
        e.fill();
        vm.prank(alice);
        e.graduate();
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
        vm.prank(alice);
        Pool seat = Pool(m.claim(address(e)));
        CoreSimulatorLib.nextBlock();
        m.open(address(seat));
        CoreSimulatorLib.nextBlock();
        uint256 day = block.timestamp / 1 days;
        vm.warp(day * 1 days + 23 hours + 59 minutes + 30 seconds);
        _mockMargin(address(seat), 4_700_000, 0);
        vm.warp((day + 1) * 1 days + 5);
        vm.roll(block.number + 1);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.RuleBroken.selector, Breach.DailyLoss));
        seat.checkpoint();
        assertEq(seat.dayStartEquity(), int64(5e6), "the day's base stands");
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(stranger);
        seat.breach(c, a, SALT);
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Closing), "stopped for the day's loss");
    }
}
