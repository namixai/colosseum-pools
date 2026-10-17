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

/// The cloud audit's F1 bundler (second round, poc lens). Bundles the two permissionless calls in ONE transaction: `finish`
/// frees a settled seat (its balance leaves for the pool through CoreWriter and lands only after
/// the block) and `settle` / `renew` / `closeRound` strikes a price in the same block.
contract F1Bundler {
    function settle(MultiPool m, PoolShares s, address seat, address[] calldata tickets) external {
        m.finish(seat);
        s.settle(tickets);
    }

    function renew(MultiPool m, PoolShares s, address seat) external {
        m.finish(seat);
        s.renew();
    }

    function closeRound(MultiPool m, PoolShares s, address seat) external {
        m.finish(seat);
        s.closeRound();
    }

    /// The seat's last settlement step, the freeing and the price in one transaction: the attacker
    /// does not depend on a keeper leaving a settled seat unfinished.
    function lastStepThenSettle(MultiPool m, PoolShares s, Pool seat, address[] calldata tickets) external {
        Cancel[] memory c = new Cancel[](0);
        uint32[] memory a = new uint32[](0);
        seat.settleFunded(c, a);
        require(seat.stage() == Pool.Stage.Idle, "not the last step");
        m.finish(address(seat));
        s.settle(tickets);
    }
}

/// The deep audit's F1 (cloud, 16 October 2026), turned round: a free seat counts in the pool's value,
/// so what `finish` sends back from it is counted once, on the seat until it lands on the pool, and a
/// price struck in the block `finish` frees a seat is the whole price. On f8bc60aa and 5eb75719
/// `value()` skipped a free seat, and a deposit settled in that block bought in at a discount, or a
/// round closed in it paid its leavers short. The stand is the audit's (F1_ValueFinishBlock), its
/// helpers copied so that the shares suite does not run twice.
contract DeepValueFinishTest is PoolHarness {
    PaperExam examImpl;
    PoolShares shares;
    MultiPool m;
    F1Bundler atk;
    address creator = makeAddr("creator");
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address carol = makeAddr("carol");
    address dave = makeAddr("dave");
    uint256 constant DUST = 1000; // integer rounding of a share price, 1e-5 USDC

    function _examImpl() internal returns (address) {
        if (address(examImpl) == address(0)) examImpl = new PaperExam();
        return address(examImpl);
    }

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

    function _stand() internal {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        atk = new F1Bundler();
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        shares.bind(m, TERMS);
        shares.launch();
        for (uint256 i = 0; i < 4; ++i) {
            CoreSimulatorLib.forceAccountActivation([alice, bob, carol, dave][i]);
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

    function _buy(address who, uint256 line) internal returns (PaperExam e) {
        uint256 price = abi.decode(CodeStore.read(m.planData()), (Plan)).menu[line].price;
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
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        vm.prank(who);
        e.place(BTC, false, sz);
        vm.roll(block.number + 1);
        e.fill();
        vm.prank(who);
        e.graduate();
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
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

    function _settle(Pool seat) internal {
        (Cancel[] memory c, uint32[] memory a) = _none();
        for (uint256 i = 0; i < 8 && seat.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            seat.settleFunded(c, a);
        }
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Idle));
    }

    /// Carol's 25 USDC seat, funded, never traded, closed as idle and settled: Pool stage Idle,
    /// MultiPool seat state still Funded (nobody called `finish` yet).
    function _idleSettledSeat() internal returns (Pool seat) {
        seat = _fund(carol, 2);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.warp(block.timestamp + 31 days);
        m.closeIdle(address(seat), c, a, SALT);
        _settle(seat);
        assertEq(uint8(_seatState(address(seat))), uint8(MultiPool.SeatState.Funded), "not freed yet");
        assertEq(_spot(address(seat)), 25e8, "the seat holds its whole size on spot again");
    }

    function _seatState(address seat) internal view returns (MultiPool.SeatState s) {
        (, s,,,,,,) = m.seatInfo(seat);
    }

    function _worth(address who) internal view returns (uint256) {
        return (shares.sharesOf(who) * shares.value()) / shares.totalShares();
    }

    /// The audit's first proof, turned round: `finish` leaves the pool's value as it was, in its own
    /// block and after.
    function test_deep_F1_finishLeavesTheValueWhole_inItsBlockAndAfter() public {
        _standWithMoney();
        Pool seat = _idleSettledSeat();
        uint256 before = m.value();
        m.finish(address(seat));
        assertEq(uint8(_seatState(address(seat))), uint8(MultiPool.SeatState.Free));
        assertEq(_spot(address(seat)), 25e8, "still on the seat");
        assertEq(m.value(), before, "and counted there");
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(seat)), 0);
        assertEq(m.value(), before, "landed, and counted once");
    }

    /// Free the seat and settle a deposit in one transaction: the deposit buys in at the whole price.
    function test_deep_F1_finishThenSettleInOneTransaction_pricesTheDepositFairly() public {
        _standWithMoney();
        Pool seat = _idleSettledSeat();
        address t = _ticket(dave, 75e8);
        uint256 fairValue = shares.value();
        uint256 fairShares = (75e8 * shares.totalShares()) / fairValue;
        atk.settle(m, shares, address(seat), _list(t));
        assertEq(shares.sharesOf(dave), fairShares, "priced on the whole value");
        CoreSimulatorLib.nextBlock();
        assertEq(shares.value(), fairValue + 75e8);
        assertApproxEqAbs(_worth(dave), 75e8, DUST, "dave's shares are worth what he put in");
        assertApproxEqAbs(_worth(alice), (60e8 * fairValue) / 100e8, DUST, "alice keeps her money");
        assertApproxEqAbs(_worth(bob), (40e8 * fairValue) / 100e8, DUST, "bob keeps his money");
    }

    /// Free the seat and renew in one transaction: the round closes at the whole value.
    function test_deep_F1_finishThenRenewInOneTransaction_paysTheLeaverInFull() public {
        _standWithMoney();
        Pool seat = _fund(carol, 2);
        vm.prank(bob);
        shares.requestExit(40e8);
        vm.warp(uint256(m.reserveFor()) + 1); // the round is paid after the end it was asked for
        (Cancel[] memory c, uint32[] memory a) = _none();
        m.closeIdle(address(seat), c, a, SALT);
        _settle(seat);
        uint256 fairValue = shares.value();
        atk.renew(m, shares, address(seat));
        assertEq(shares.round(), 1, "the round closed");
        assertEq(shares.roundValue(0), fairValue, "at the whole value");
        (,, uint256 due) = shares.positionOf(bob);
        assertEq(due, (40e8 * fairValue) / 100e8, "bob: his 40 % of it");
    }

    /// Free the seat and close the round in one transaction, after the pool stopped: the same.
    function test_deep_F1_finishThenCloseRoundInOneTransaction_paysTheLeaverInFull() public {
        _standWithMoney();
        Pool seat = _fund(carol, 2);
        vm.prank(creator);
        shares.stop();
        vm.warp(m.endsAt() + 1);
        (Cancel[] memory c, uint32[] memory a) = _none();
        m.endSeat(address(seat), c, a, SALT);
        _settle(seat);
        vm.prank(bob);
        shares.requestExit(40e8);
        uint256 fairValue = shares.value();
        atk.closeRound(m, shares, address(seat));
        assertEq(shares.roundValue(0), fairValue, "at the whole value");
        (,, uint256 due) = shares.positionOf(bob);
        assertEq(due, (40e8 * fairValue) / 100e8);
    }

    /// The seat's last settlement step, `finish` and `settle` in one transaction: the whole price too.
    function test_deep_F1_theLastSettlementStepBundledToo_pricesFairly() public {
        _standWithMoney();
        Pool seat = _fund(carol, 2);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.warp(block.timestamp + 31 days);
        m.closeIdle(address(seat), c, a, SALT);
        CoreSimulatorLib.nextBlock();
        seat.settleFunded(c, a);
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Closing));
        CoreSimulatorLib.nextBlock();
        address t = _ticket(dave, 75e8);
        uint256 fairValue = shares.value();
        atk.lastStepThenSettle(m, shares, seat, _list(t));
        assertEq(shares.sharesOf(dave), (75e8 * 100e8) / fairValue, "priced on the whole value");
    }

    /// What reaches a free seat after it was freed (a late landing, a send HyperCore dropped) is the
    /// pool's and counts at once; `sweep` moves it without changing the value.
    function test_deep_F1_moneyOnAFreeSeatCounts_andSweepChangesNothing() public {
        _standWithMoney();
        Pool seat = _idleSettledSeat();
        m.finish(address(seat));
        CoreSimulatorLib.nextBlock();
        uint256 whole = m.value();
        CoreSimulatorLib.forceSpotBalance(address(seat), 0, 10e8);
        assertEq(m.value(), whole + 10e8, "counted on the free seat");
        m.sweep(address(seat));
        assertEq(m.value(), whole + 10e8, "sweep changes nothing in its block");
        CoreSimulatorLib.nextBlock();
        assertEq(m.value(), whole + 10e8, "nor after it");
    }

    /// Control, as the audit had it: the same settlement a block later was always fair.
    function test_deep_F1_control_finishThenSettleNextBlock_pricesFairly() public {
        _standWithMoney();
        Pool seat = _idleSettledSeat();
        address t = _ticket(dave, 75e8);
        uint256 fairValue = shares.value();
        uint256 fairShares = (75e8 * shares.totalShares()) / fairValue;
        m.finish(address(seat));
        CoreSimulatorLib.nextBlock();
        assertEq(shares.value(), fairValue);
        shares.settle(_list(t));
        assertEq(shares.sharesOf(dave), fairShares);
    }
}
