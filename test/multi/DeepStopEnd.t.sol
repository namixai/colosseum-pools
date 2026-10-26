// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {Cancel} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";
import {PoolShares} from "../../src/multi/PoolShares.sol";
import {PoolHarness} from "../PoolHarness.sol";
import {console2} from "forge-std/console2.sol";

/// The deep audit's F7 (16 October 2026), turned round: a stop waits for the exams still running,
/// and only for them. Exams already claimed or released hold nothing, so a stop's end is the end
/// the leavers asked for. On f8bc60aa the stop's end was lastExamEnds, a mark set by every sale that
/// never came down. The stand is the audit's (StopInheritsLastExamEnds): a 60-day exam step.
contract DeepStopEndTest is PoolHarness {
    uint64 constant UP = 786920; // BTC +3%
    uint64 constant DOWN = 756360; // BTC -1%
    uint64 constant CRASH = 741080; // BTC -3%

    PaperExam examImpl;
    PoolShares shares;
    MultiPool m;
    address creator = makeAddr("creator");
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address carol = makeAddr("carol");
    address dave = makeAddr("dave");


    function _examImpl() internal returns (address) {
        if (address(examImpl) == address(0)) examImpl = new PaperExam();
        return address(examImpl);
    }

    /// PoolShares.t.sol's plan, with a 60-day exam step instead of 7: saleLead = 67 days.
    function _plan() internal pure returns (Plan memory p) {
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
            stepDuration: 60 days,
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
        p.fundedIdle = 90 days;
        p.termsBook = TERMS_BOOK;
    }

    function _stand() internal {
        vm.warp(1_790_000_000);
        shares = _newShares(creator, 1e8, 0);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        shares.bind(m, TERMS);
        shares.launch();
        for (uint256 i = 0; i < 5; ++i) {
            CoreSimulatorLib.forceAccountActivation([alice, bob, carol, dave, creator][i]);
        }
    }

    function _standWithMoney() internal {
        _stand();
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
    }

    function _ticket(address who, uint64 amount) internal returns (address t) {
        vm.prank(who);
        t = shares.openTicket(TERMS);
        CoreSimulatorLib.forceAccountActivation(t);
        CoreSimulatorLib.forceSpotBalance(t, 0, amount);
    }

    function _list(address t) internal pure returns (address[] memory l) {
        l = new address[](1);
        l[0] = t;
    }

    function _deposit(address who, uint64 amount) internal returns (address t) {
        t = _ticket(who, amount);
        shares.settle(_list(t));
        CoreSimulatorLib.nextBlock();
    }

    function _exit(address who, uint256 n) internal {
        vm.prank(who);
        shares.requestExit(n);
    }

    function _buy(address who, uint256 line) internal returns (PaperExam e) {
        uint256 price = abi.decode(CodeStore.read(m.planData()), (Plan)).menu[line].price + factory.challengeFee();
        deal(address(usdc), who, price);
        vm.startPrank(who);
        usdc.approve(address(m), price);
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
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Passed));
    }

    /// The exam's trader goes 4x long and the market falls 3%: 12% down, past the 10% drawdown line.
    function _kill(PaperExam e, address who) internal {
        (,, uint64 capital,) = e.result();
        uint64 sz = (capital * 4) / BTC_MARK;
        vm.prank(who);
        e.place(BTC, true, sz);
        vm.roll(block.number + 1);
        e.fill();
        CoreSimulatorLib.setMarkPx(BTC, CRASH);
        e.breach();
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Breached));
    }

    function _fund(address who, uint256 line) internal returns (Pool seat) {
        PaperExam e = _buy(who, line);
        _pass(e, who);
        vm.prank(who);
        seat = Pool(m.claim(address(e)));
        CoreSimulatorLib.nextBlock();
        m.open(address(seat));
        CoreSimulatorLib.nextBlock();
    }

    /// The seat's trader loses about 4% (4x long, 1% down) and is flat again: inside every rule.
    function _loss(Pool seat) internal {
        uint64 sz1e8 = ((seat.terms().fundedCapital * 40) / BTC_MARK) * 100 / 1000 * 1000;
        _trade(address(seat), BTC, true, sz1e8);
        CoreSimulatorLib.setMarkPx(BTC, DOWN);
        _trade(address(seat), BTC, false, sz1e8);
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
    }

    function _settle(Pool seat) internal {
        (Cancel[] memory c, uint32[] memory a) = _none();
        for (uint256 i = 0; i < 8 && seat.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            seat.settleFunded(c, a);
        }
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Idle));
    }

    /// Ends every funded seat once the pool is over, settles, frees them.
    function _closeSeats(Pool[2] memory seats) internal {
        (Cancel[] memory c, uint32[] memory a) = _none();
        for (uint256 i = 0; i < 2; ++i) {
            m.endSeat(address(seats[i]), c, a, SALT);
            _settle(seats[i]);
            m.finish(address(seats[i]));
        }
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
    }

    function _paidAt(address who) internal view returns (uint64 at) {
        (at,,) = shares.payments(who);
    }

    /// Two seats sold 16 days before the end, both claimed: no exam runs. The keeper's renew after E1
    /// cannot pay bob and stops the pool at once; bob is paid the day after E1.
    function test_deep_F7_aStopEndsAtTheLeaversEnd_whenNoExamStillRuns() public {
        _standWithMoney();
        uint64 e1 = m.endsAt();
        vm.warp(e1 - 16 days);
        Pool s1 = _fund(carol, 2);
        vm.warp(block.timestamp + 1 minutes);
        Pool s2 = _fund(dave, 2);
        vm.warp(block.timestamp + 1 minutes);
        assertGt(m.lastExamEnds(), e1 + 50 days);
        assertEq(m.waiting(), 0, "both exams were claimed");
        vm.warp(e1 - 15 days);
        _exit(bob, 40e8);
        vm.warp(e1 - 14 days + 1);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, e1 + 1));
        shares.renew(); // never in the last days (DECISIONS 103)
        vm.warp(e1 + 1);
        vm.prank(stranger);
        shares.renew();
        assertTrue(m.stopping());
        assertEq(m.endsAt(), e1 + 1, "the stop ends at once, right after E1, not at lastExamEnds");
        vm.warp(e1 + 2);
        _closeSeats([s1, s2]);
        shares.closeRound();
        CoreSimulatorLib.nextBlock();
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertLt(_paidAt(bob), e1 + 1 days, "bob is paid the day after E1");
    }

    /// The creator's dead exam, bought, broken and released 20 days before the end, holds nothing:
    /// the creator's stop ends at E1.
    function test_deep_F7_aDeadExamDoesNotHoldTheCreatorsStop() public {
        _standWithMoney();
        uint64 e1 = m.endsAt();
        vm.warp(block.timestamp + 1 days);
        _fund(carol, 2);
        vm.warp(e1 - 20 days);
        PaperExam dead = _buy(creator, 0);
        _kill(dead, creator);
        m.release(address(dead));
        assertEq(m.waiting(), 0);
        vm.warp(e1 - 15 days);
        _exit(bob, 40e8);
        vm.prank(creator);
        shares.stop();
        assertEq(m.endsAt(), e1, "the creator's stop ends at E1");
    }

    /// An exam still running when the pool is stopped has all its time: the stop waits for it.
    function test_deep_F7_control_anExamStillRunningHoldsTheStop() public {
        _standWithMoney();
        uint64 e1 = m.endsAt();
        vm.warp(e1 - 20 days);
        _buy(carol, 0);
        assertEq(m.waiting(), 1);
        uint64 lee = m.lastExamEnds();
        vm.prank(creator);
        shares.stop();
        assertEq(m.endsAt(), lee, "no exam is cut short");
    }

    // ── 3. control: without a late sale the same stop pays bob right after E1 ───────────────

    /// The same pool, the same seats, the same notice, and no sale in the last saleLead: the
    /// fallback stop ends at E1 and bob is paid the day after. The only difference from test 1 is
    /// when the seats were sold.
    function test_control_withoutALateSale_theFallbackStopEndsAtE1_andBobIsPaidThen() public {
        _standWithMoney();
        uint64 e1 = m.endsAt();
        vm.warp(block.timestamp + 1 days);
        Pool s1 = _fund(carol, 2);
        vm.warp(block.timestamp + 1 minutes);
        Pool s2 = _fund(dave, 2);
        vm.warp(block.timestamp + 1 minutes);
        vm.warp(e1 - 15 days);
        _exit(bob, 40e8);
        vm.warp(e1 - 14 days + 1);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, e1 + 1));
        shares.renew(); // never in the last days (DECISIONS 103)
        vm.warp(e1 + 1);
        vm.prank(stranger);
        shares.renew();
        assertTrue(m.stopping());
        assertEq(m.endsAt(), e1 + 1, "no late sale: the stop ends at once, right after E1");
        vm.warp(e1 + 2);
        _closeSeats([s1, s2]);
        shares.closeRound();
        CoreSimulatorLib.nextBlock();
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertLt(_paidAt(bob), e1 + 1 days, "bob is paid the day after E1");
        console2.log("[control] endsAt() == E1                      :", m.endsAt() == e1);
        console2.log("[control] seconds after E1 bob was paid       :", _paidAt(bob) - e1);
    }
}
