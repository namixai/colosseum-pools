// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {Cancel} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";
import {PoolShares} from "../../src/multi/PoolShares.sol";
import {ShareTicket} from "../../src/multi/ShareTicket.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// Investors' shares of one pool of many traders, on the simulator. The pool is the one the
/// MultiPool tests use (a menu of 5, 10 and 25, 80% in work, a year's term, fourteen days of
/// notice), owned by the shares contract; alice puts in 60 and bob 40 in most tests.
contract PoolSharesTest is PoolHarness {
    uint64 constant UP = 786920; // BTC +3%

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


    /// A shares contract, its pool, both on HyperCore, bound.
    function _stand() internal {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        shares.bind(m, TERMS);
        shares.launch(); // no target: the pool starts at once
        for (uint256 i = 0; i < 4; ++i) {
            CoreSimulatorLib.forceAccountActivation([alice, bob, carol, dave][i]);
        }
    }

    /// The creator stops the pool: from here it ends at the end of its term.
    function _creatorStops() internal {
        vm.prank(creator);
        shares.stop();
    }

    /// Alice's 60 and bob's 40, in the pool.
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

    /// A deposit counted and swept into the pool.
    function _deposit(address who, uint64 amount) internal returns (address t) {
        t = _ticket(who, amount);
        shares.settle(_list(t));
        CoreSimulatorLib.nextBlock();
    }

    function _window() internal {
        vm.warp(m.endsAt() - m.notice() + 1);
    }

    /// The round is paid once the end it was asked for has passed, by anyone (the terms, D5 and D6;
    /// DECISIONS 103).
    function _payRound() internal {
        vm.warp(uint256(m.reserveFor()) + 1);
        shares.renew();
    }

    function _extend(uint32 term) internal {
        vm.prank(creator);
        shares.extend(term);
    }

    function _exit(address who, uint256 n) internal {
        vm.prank(who);
        shares.requestExit(n);
    }

    function _buy(address who, uint256 line) internal returns (PaperExam e) {
        uint256 price = abi.decode(CodeStore.read(m.planData()), (Plan)).menu[line].price;
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
    }

    /// A passed exam taken and its account started.
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

    // ── binding ──────────────────────────────────────────────────────────────────────

    /// The creator binds once, a pool owned here that has sold nothing, with both accounts on
    /// HyperCore; and nothing works before.
    function test_shares_bind_onceByTheCreator_onAnEmptyPoolOwnedHere() public {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        MultiPool other = new MultiPool(investor, factory, _examImpl(), _tie(_plan()), 180 days, 730 days, 14 days, 90 days);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);

        vm.expectRevert(PoolShares.NotBound.selector);
        shares.openTicket(TERMS);
        vm.expectRevert(PoolShares.NotBound.selector);
        shares.settle(new address[](0));
        vm.expectRevert(PoolShares.NotBound.selector);
        shares.requestExit(1);

        vm.prank(stranger);
        vm.expectRevert(PoolShares.NotCreator.selector);
        shares.bind(m, TERMS);
        vm.startPrank(creator);
        vm.expectRevert(PoolShares.NotOwnedHere.selector);
        shares.bind(other, TERMS);
        // Neither account is on HyperCore yet, then only this contract's.
        vm.expectRevert(PoolShares.NotReady.selector);
        shares.bind(m, TERMS);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        vm.expectRevert(PoolShares.NotReady.selector);
        shares.bind(m, TERMS);
        CoreSimulatorLib.forceAccountActivation(address(m));
        shares.bind(m, TERMS);
        assertEq(address(shares.pool()), address(m));
        vm.expectRevert(PoolShares.AlreadyBound.selector);
        shares.bind(m, TERMS);
        // Only the pool's.
        PoolShares s2 = new PoolShares(creator, usdc, 1e8, 0);
        MultiPool m2 = new MultiPool(address(s2), factory, _examImpl(), _tie(_plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(m2));
        vm.expectRevert(PoolShares.NotReady.selector);
        s2.bind(m2, TERMS);
        vm.stopPrank();
    }

    /// The cloud audit's N5, turned round: the raise's amount and smallest deposit are this contract's,
    /// in no plan, and a creator published terms stating a raise of 100,000, bound them to a shares
    /// contract raising nothing, and launched it at once. The terms' second line now names the raise
    /// they were written for, and `bind` takes no terms written for another, nor terms that name none.
    function test_deep_N5_termsWrittenForAnotherRaiseAreRefused() public {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        m = new MultiPool(address(shares), factory, _examImpl(),
            _tie(_plan(), string.concat(_sharesLine(100_000e8, 1e8), TERMS_TEXT)), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        vm.expectRevert(PoolShares.WrongTerms.selector);
        shares.bind(m, TERMS);

        MultiPool none = new MultiPool(address(shares), factory, _examImpl(), _tie(_plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(none));
        vm.prank(creator);
        vm.expectRevert(PoolShares.WrongTerms.selector);
        shares.bind(none, TERMS); // the single-investor text: no raise named

        MultiPool small = new MultiPool(address(shares), factory, _examImpl(),
            _tie(_plan(), string.concat(_sharesLine(0, 2e8), TERMS_TEXT)), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(small));
        vm.prank(creator);
        vm.expectRevert(PoolShares.WrongTerms.selector);
        shares.bind(small, TERMS); // the right amount, another smallest deposit

        MultiPool right = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(right));
        vm.prank(creator);
        shares.bind(right, TERMS);
        assertEq(address(shares.pool()), address(right));
    }

    /// A ticket pays only where its shares contract says, and is nobody else's to set up again.
    function test_shares_aTicketAnswersOnlyItsShares() public {
        _stand();
        address t = _ticket(alice, 10e8);
        vm.prank(stranger);
        vm.expectRevert(ShareTicket.NotShares.selector);
        ShareTicket(t).sweep(stranger, 10e8);
        vm.prank(stranger);
        vm.expectRevert(ShareTicket.Initialized.selector);
        ShareTicket(t).init(stranger);
        ShareTicket impl = ShareTicket(shares.ticketImpl());
        vm.expectRevert(ShareTicket.Initialized.selector);
        impl.init(stranger);
        assertEq(ShareTicket(t).depositor(), alice);
        assertEq(ShareTicket(t).shares(), address(shares));
        assertEq(shares.ticketAddress(alice, 0), t);
    }

    function test_shares_aDepositIsOneUSDCAtTheLeast() public {
        vm.expectRevert(PoolShares.BadDeposit.selector);
        new PoolShares(creator, usdc, 1e8 - 1, 0);
        assertEq(new PoolShares(creator, usdc, 1e8, 0).minDeposit(), 1e8);
    }

    // ── deposits ─────────────────────────────────────────────────────────────────────

    /// The first shares are one per unit; the money goes from the ticket straight into the pool, and
    /// the value reads the same while it moves.
    function test_shares_deposit_firstOnePerUnit_straightIntoThePool() public {
        _stand();
        address t = _ticket(alice, 60e8);
        shares.settle(_list(t));
        assertEq(shares.sharesOf(alice), 60e8);
        assertEq(shares.totalShares(), 60e8);
        assertEq(shares.value(), 60e8); // still on the ticket, counted there
        assertEq(shares.closedTickets().length, 1);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(t), 0);
        assertEq(_spot(address(shares)), 0);
        assertEq(_spot(address(m)), 60e8);
        assertEq(shares.value(), 60e8);
        shares.settle(new address[](0));
        assertEq(shares.closedTickets().length, 0);
        // Money sent to a ticket after it was counted belongs to the holders: naming it takes it in.
        CoreSimulatorLib.forceSpotBalance(t, 0, 3e8);
        shares.settle(_list(t));
        assertEq(shares.closedTickets().length, 1);
        assertEq(shares.value(), 63e8);
        assertEq(shares.totalShares(), 60e8);
    }

    /// Audit S-1: the creator put 100 in the pool before binding it, and a stranger's first deposit
    /// of 1 took all of it -- 101 paid out for 1. What the pool holds with no shares is the
    /// creator's, as shares, before the first deposit is priced.
    function test_shares_whatThePoolHeldBeforeTheFirstDepositIsTheCreators() public {
        _stand();
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 100e8);
        _deposit(stranger, 1e8);
        assertEq(shares.sharesOf(creator), 100e8);
        assertEq(shares.sharesOf(stranger), 1e8);
        assertEq(shares.totalShares(), 101e8);
        assertEq(shares.value(), 101e8);
        CoreSimulatorLib.forceAccountActivation(stranger);
        _creatorStops();
        vm.warp(m.endsAt() + 1);
        _exit(stranger, 1e8);
        shares.closeRound();
        CoreSimulatorLib.nextBlock();
        shares.pay(stranger);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(stranger), 1e8, "1 back for 1");
    }

    /// Once every share has gone, what is left is nobody's until the next deposit: then it is the
    /// creator's, and the deposit buys in at the value as it stands.
    function test_shares_whatIsLeftWhenEveryShareHasGoneIsTheCreators() public {
        _standWithMoney();
        _creatorStops();
        vm.warp(m.endsAt() + 1);
        _exit(alice, 60e8);
        _exit(bob, 40e8);
        shares.closeRound();
        assertEq(shares.totalShares(), 0);
        CoreSimulatorLib.nextBlock();
        // Something lands afterwards: a gift, or a seat's late return.
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 5e8);
        vm.warp(block.timestamp + 2 minutes);
        shares.settle(new address[](0));
        assertEq(shares.sharesOf(creator), 5e8);
        assertEq(shares.totalShares(), 5e8);
    }

    /// A later deposit is priced at the value without it.
    function test_shares_deposit_pricedAtTheValueWithoutIt() public {
        _standWithMoney();
        // The pool makes 10.
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 110e8);
        address t = _ticket(carol, 22e8);
        shares.settle(_list(t));
        assertEq(shares.sharesOf(carol), 20e8);
        assertEq(shares.totalShares(), 120e8);
        assertEq(shares.value(), 132e8);
    }

    /// Under the smallest deposit, a ticket stays open; a ticket never opened here is passed over;
    /// a point names eight tickets at most.
    function test_shares_settle_whatItCountsAndWhatNot() public {
        _stand();
        address small = _ticket(alice, 1e8 - 1);
        shares.settle(_list(small));
        assertEq(shares.sharesOf(alice), 0);
        assertEq(shares.openTicketCount(), 1);
        shares.settle(_list(makeAddr("nobody")));
        assertEq(shares.totalShares(), 0);
        address[] memory nine = new address[](9);
        vm.expectRevert(PoolShares.TooMany.selector);
        shares.settle(nine);
        shares.settle(new address[](8));
        // Exactly the smallest deposit counts.
        address least = _ticket(bob, 1e8);
        shares.settle(_list(least));
        assertEq(shares.sharesOf(bob), 1e8);
    }

    /// A pool that has lost everything takes no deposit: there is no price to give it.
    function test_shares_settle_noPriceForAPoolWorthNothing() public {
        _standWithMoney();
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 0);
        assertEq(shares.value(), 0);
        address t = _ticket(carol, 10e8);
        vm.expectRevert(PoolShares.NoValue.selector);
        shares.settle(_list(t));
    }

    // ── deciding ─────────────────────────────────────────────────────────────────────

    /// An investor says they are leaving, or takes it back, up to the notice before the end; in the
    /// last fourteen days what was said holds; once the pool has stopped they may say it again.
    function test_shares_decisions_untilTheNoticeBeforeTheEnd() public {
        _standWithMoney();
        _exit(bob, 10e8);
        assertEq(shares.sharesOf(bob), 30e8);
        assertEq(shares.exitOf(bob), 10e8);
        assertEq(shares.exitShares(), 10e8);
        assertEq(shares.totalShares(), 100e8);
        vm.prank(bob);
        shares.cancelExit(4e8);
        assertEq(shares.exitOf(bob), 6e8);
        assertEq(shares.exitShares(), 6e8);
        assertEq(shares.sharesOf(bob), 34e8);

        vm.warp(m.endsAt() - m.notice());
        _exit(bob, 1e8);
        vm.warp(block.timestamp + 1);
        vm.startPrank(bob);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.Decided.selector, m.endsAt()));
        shares.requestExit(1e8);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.Decided.selector, m.endsAt()));
        shares.cancelExit(1e8);
        vm.warp(m.endsAt());
        vm.expectRevert(abi.encodeWithSelector(PoolShares.Decided.selector, m.endsAt()));
        shares.requestExit(1e8);
        uint64 end = m.endsAt();
        vm.warp(end + 1);
        // The term renewed by itself with the round still open: the round stands as it was at its
        // end until somebody closes it, and then decisions are open for the next end (audit M-2).
        vm.expectRevert(abi.encodeWithSelector(PoolShares.Decided.selector, end));
        shares.requestExit(1e8);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.Decided.selector, end));
        shares.cancelExit(1e8);
        vm.stopPrank();
        assertEq(shares.exitOf(bob), 7e8);
        shares.renew();
        (, uint256 leaving, uint256 due) = shares.positionOf(bob);
        assertEq(leaving, 0);
        assertGt(due, 0);
        vm.startPrank(bob);
        shares.requestExit(1e8);
        shares.cancelExit(1e8);
        vm.stopPrank();
    }

    function test_shares_exit_noMoreThanYouHold() public {
        _standWithMoney();
        vm.startPrank(bob);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotFree.selector, 40e8));
        shares.requestExit(40e8 + 1);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotFree.selector, 40e8));
        shares.requestExit(0);
        shares.requestExit(40e8);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotFree.selector, 40e8));
        shares.cancelExit(40e8 + 1);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotFree.selector, 40e8));
        shares.cancelExit(0);
        shares.cancelExit(40e8);
        vm.stopPrank();
        assertEq(shares.sharesOf(bob), 40e8);
        assertEq(shares.exitShares(), 0);
    }

    // ── extending ────────────────────────────────────────────────────────────────────

    /// Only the creator extends, and only in the last fourteen days, when every decision is in:
    /// an extension made earlier would move the decision away from whoever had not made it yet.
    function test_shares_extend_byTheCreator_inTheLastDaysOnly() public {
        _standWithMoney();
        uint64 end = m.endsAt();
        vm.prank(creator);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end - 14 days + 1));
        shares.extend(180 days);
        vm.warp(end - 14 days);
        vm.prank(creator);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end - 14 days + 1));
        shares.extend(180 days);
        _window();
        vm.prank(stranger);
        vm.expectRevert(PoolShares.NotCreator.selector);
        shares.extend(180 days);
        _extend(180 days);
        assertEq(m.endsAt(), end + 180 days);
        // Nobody asked to leave: nothing left the pool.
        assertEq(_spot(address(m)), 100e8);
        assertEq(shares.round(), 0);
    }

    /// Bob leaves with 40 of 100 shares when the pool is worth 110. The creator's extension in the last
    /// days only moves the end; once the end bob asked for has passed, his round takes his 44 out and
    /// pays it once it has landed; alice's 60 shares are still worth 66, and she stays.
    function test_shares_extend_movesTheEnd_andTheRoundIsPaidAfterIt_theRestStay() public {
        _standWithMoney();
        _exit(bob, 40e8);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 110e8);
        uint64 end = m.endsAt();
        _window();
        _extend(180 days);
        assertEq(m.endsAt(), end + 180 days);
        assertEq(shares.round(), 0, "in the last days the end only moves on, and bob waits for his");
        vm.warp(end);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end + 1));
        shares.renew();
        vm.warp(end + 1);
        shares.renew();
        assertEq(shares.round(), 1);
        assertEq(shares.roundValue(0), 110e8);
        assertEq(shares.roundShares(0), 100e8);
        assertEq(shares.totalShares(), 60e8);
        assertEq(shares.owed(), 44e8);
        assertEq(shares.exitShares(), 0);
        (uint256 staying, uint256 leaving, uint256 due) = shares.positionOf(bob);
        assertEq(staying, 0);
        assertEq(leaving, 0);
        assertEq(due, 44e8);
        // Sent and not landed: the pool counts it as gone, and here it is owed.
        assertEq(m.value(), 66e8);

        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotEnoughFree.selector, 0, 0, 44e8));
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(shares)), 44e8);
        // Here and owed: it is not the holders'. (In the first minute the pool still counts what it
        // sent against itself, and no price is struck.)
        vm.warp(block.timestamp + 1 minutes);
        assertEq(shares.value(), 66e8);
        shares.pay(bob);
        assertEq(shares.owed(), 0);
        assertEq(shares.owedOf(bob), 0);
        (uint64 at,, uint96 core) = shares.payments(bob);
        assertEq(at, block.timestamp);
        assertEq(core, 44e8);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 44e8);
        vm.expectRevert(PoolShares.NothingOwed.selector);
        shares.pay(bob);

        // Alice said nothing and stays; her decisions open again for the new term.
        assertEq(shares.sharesOf(alice), 60e8);
        vm.warp(block.timestamp + 2 minutes);
        assertEq(shares.value(), 66e8);
        _exit(alice, 1e8);
    }

    /// If what the leavers are worth is in the traders' accounts, the extension does not happen:
    /// nobody who asked to leave is held for another term. The pool stops at its end, its accounts
    /// close, and the round pays.
    function test_shares_renew_whenTheLeaversMoneyIsInWork_thePoolStops_andTheEndPays() public {
        _standWithMoney();
        // A minute apart: the pool counts what it sent to a seat in the last minute against its balance.
        Pool s1 = _fund(carol, 2);
        vm.warp(block.timestamp + 1 minutes);
        Pool s2 = _fund(dave, 2);
        vm.warp(block.timestamp + 1 minutes);
        Pool s3 = _fund(trader, 2);
        assertEq(m.inWork(), 75e6);
        _exit(bob, 40e8);
        uint64 end = m.endsAt();
        _window();
        // In the last days the creator's extension only moves the end; bob waits for his.
        _extend(180 days);
        assertEq(shares.round(), 0);
        vm.warp(end + 1);
        // After it, the round cannot be paid: the pool stops instead.
        vm.prank(stranger);
        shares.renew();
        assertTrue(m.stopping());
        assertEq(shares.round(), 0);
        assertEq(shares.exitOf(bob), 40e8);

        vm.expectRevert(PoolShares.NotEnded.selector);
        shares.closeRound();
        vm.warp(m.endsAt() + 1);
        (Cancel[] memory c, uint32[] memory a) = _none();
        Pool[3] memory seats = [s1, s2, s3];
        for (uint256 i = 0; i < 3; ++i) {
            m.endSeat(address(seats[i]), c, a, SALT);
            (PoolShares.Blocker why, address who) = shares.blocker();
            assertEq(uint8(why), uint8(PoolShares.Blocker.SeatClosing));
            assertEq(who, address(seats[i]));
            _settle(seats[i]);
            m.finish(address(seats[i]));
        }
        vm.warp(block.timestamp + 2 minutes);
        uint256 v = shares.value();
        shares.closeRound();
        uint256 due = (40e8 * v) / 100e8;
        assertEq(shares.owed(), due);
        CoreSimulatorLib.nextBlock();
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        // The exams' prices, on HyperEVM, are paid first; the rest on HyperCore.
        assertGt(usdc.balanceOf(bob), 0);
        assertEq(uint256(_spot(bob)) + usdc.balanceOf(bob) * 100, due);
    }

    /// The term renews by itself; once the end those who asked to leave asked for has passed, anyone
    /// closes their round, at the pool's value then: never in the term's last days (the terms, D5
    /// and D6; DECISIONS 103). They are paid, and the pool runs on for those who stay.
    function test_shares_renew_byAnyone_paysTheLeavers_afterTheEnd_andThePoolRunsOn() public {
        _standWithMoney();
        _exit(bob, 40e8);
        assertEq(m.reserve(), 40e8, "the pool sells nothing against what is to leave");
        uint64 end = m.endsAt();
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end + 1));
        shares.renew();
        _window();
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end + 1));
        shares.renew();
        vm.warp(end);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end + 1));
        shares.renew();
        vm.warp(end + 1);
        assertEq(m.endsAt(), end + 90 days, "the term renewed by itself");
        vm.prank(stranger);
        shares.renew();
        assertEq(shares.round(), 1);
        assertEq(m.reserve(), 0);
        assertFalse(m.stopping());
        vm.expectRevert(PoolShares.NothingRequested.selector);
        shares.renew();
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 40e8);
        assertEq(m.endsAt(), end + 90 days, "the pool ran on");
    }

    /// The reserve follows what the holders asked out, at the price of the moment.
    function test_shares_reserve_followsTheLeavers() public {
        _standWithMoney();
        _exit(bob, 10e8);
        assertEq(m.reserve(), 10e8);
        vm.prank(bob);
        shares.cancelExit(4e8);
        assertEq(m.reserve(), 6e8);
        vm.prank(stranger);
        vm.expectRevert(MultiPool.NotOwner.selector);
        m.setReserve(0);
    }

    /// A pool of several investors ends only when its creator stops it; until then it renews.
    function test_shares_onlyTheCreatorStopsThePool() public {
        _standWithMoney();
        vm.prank(stranger);
        vm.expectRevert(PoolShares.NotCreator.selector);
        shares.stop();
        _creatorStops();
        assertTrue(m.stopping());
    }

    // ── after the end ────────────────────────────────────────────────────────────────

    /// A pool not extended stops; everyone may then leave, and a round is closed by anyone.
    function test_shares_afterTheEnd_everyoneMayLeave() public {
        _standWithMoney();
        vm.expectRevert(PoolShares.NotEnded.selector);
        shares.closeRound();
        _creatorStops();
        vm.warp(m.endsAt());
        vm.expectRevert(PoolShares.NotEnded.selector);
        shares.closeRound();
        vm.warp(m.endsAt() + 1);
        vm.expectRevert(PoolShares.NothingRequested.selector);
        shares.closeRound();
        _exit(alice, 60e8);
        _exit(bob, 40e8);
        vm.prank(stranger);
        shares.closeRound();
        assertEq(shares.totalShares(), 0);
        assertEq(shares.owed(), 100e8);
        CoreSimulatorLib.nextBlock();
        shares.pay(alice);
        // Two payments in one block are counted against each other: the second sees the first.
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(alice), 60e8);
        assertEq(_spot(bob), 40e8);
        assertEq(shares.value(), 0);
    }

    /// Nobody opens a ticket after the end; tickets opened up to it and paid into after still count,
    /// their money stays here, and leaving pays out of it without touching the pool.
    function test_shares_afterTheEnd_lateDepositsAreKeptHere() public {
        _standWithMoney();
        _creatorStops();
        vm.warp(m.endsAt());
        vm.prank(carol);
        address tc = shares.openTicket(TERMS);
        vm.prank(dave);
        address td = shares.openTicket(TERMS);
        vm.warp(m.endsAt() + 1);
        vm.prank(carol);
        vm.expectRevert(PoolShares.Ended.selector);
        shares.openTicket(TERMS);
        address[] memory both = new address[](2);
        both[0] = tc;
        both[1] = td;
        for (uint256 i = 0; i < 2; ++i) {
            CoreSimulatorLib.forceAccountActivation(both[i]);
            CoreSimulatorLib.forceSpotBalance(both[i], 0, 10e8);
        }
        shares.settle(both);
        assertEq(shares.sharesOf(carol), 10e8);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(shares)), 20e8);
        assertEq(_spot(address(m)), 100e8);
        assertEq(shares.value(), 120e8);
        // 15 leave; the 20 here cover them, and the pool is not asked.
        _exit(carol, 10e8);
        _exit(dave, 5e8);
        shares.closeRound();
        assertEq(m.lastOutAt(), 0);
        shares.pay(carol);
        shares.pay(dave);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(carol), 10e8);
        assertEq(_spot(dave), 5e8);
    }

    // ── what holds a price back ──────────────────────────────────────────────────────

    /// No price is struck in the minute after the pool has sent money here, while it still counts
    /// it against itself, nor while a payment to a holder may not have landed.
    function test_shares_noPrice_whileMoneyIsLanding() public {
        _standWithMoney();
        _exit(bob, 40e8);
        _payRound();
        CoreSimulatorLib.nextBlock();
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotQuiet.selector, PoolShares.Blocker.PoolPaying, address(m)));
        shares.settle(new address[](0));
        vm.warp(block.timestamp + 1 minutes + 1);
        shares.settle(new address[](0));
        shares.pay(bob);
        vm.expectRevert(
            abi.encodeWithSelector(PoolShares.NotQuiet.selector, PoolShares.Blocker.PaymentInFlight, address(shares))
        );
        shares.settle(new address[](0));
        vm.warp(block.timestamp + 1 minutes + 1);
        shares.settle(new address[](0));
    }

    /// A deposit on its way into the pool holds nothing back: the value reads the same before and
    /// after, so the next point may strike a price in the same block.
    function test_shares_aDepositOnItsWay_holdsNoPriceBack() public {
        _stand();
        // The second point sweeps alice's ticket again, as it still shows her money in this block;
        // HyperCore finds nothing left for that send and drops it. The simulator would fail the test.
        CoreSimulatorLib.setRevertOnFailure(false);
        address t = _ticket(alice, 60e8);
        shares.settle(_list(t));
        address t2 = _ticket(bob, 40e8);
        address[] memory both = new address[](2);
        both[0] = t2;
        both[1] = t; // closed and still on the list: not counted twice
        shares.settle(both);
        assertEq(shares.sharesOf(bob), 40e8);
        assertEq(shares.closedTickets().length, 2);
        assertEq(shares.value(), 100e8);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(m)), 100e8);
        assertEq(shares.value(), 100e8);
    }

    /// A funded account that has broken a rule and is not closed yet holds the price back.
    function test_shares_noPrice_whileARuleIsBroken() public {
        _standWithMoney();
        Pool seat = _fund(carol, 2);
        _mockMargin(address(seat), 20e6, 0);
        (PoolShares.Blocker why, address who) = shares.blocker();
        assertEq(uint8(why), uint8(PoolShares.Blocker.RuleBroken));
        assertEq(who, address(seat));
        vm.expectRevert(
            abi.encodeWithSelector(PoolShares.NotQuiet.selector, PoolShares.Blocker.RuleBroken, address(seat))
        );
        shares.settle(new address[](0));
        vm.clearMockedCalls();
        shares.settle(new address[](0));
    }

    // ── money ────────────────────────────────────────────────────────────────────────

    /// What leaves is taken from the pool's USDC on HyperEVM first, and paid from it first.
    function test_shares_evmFirst() public {
        _standWithMoney();
        deal(address(usdc), address(m), 4e6);
        _exit(bob, 40e8);
        _payRound();
        // 40 shares of 104: 41.6, of which 4 on HyperEVM.
        assertEq(shares.owed(), 416e7);
        assertEq(usdc.balanceOf(address(shares)), 4e6);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(shares)), 376e7);
        vm.warp(block.timestamp + 1 minutes);
        assertEq(shares.value(), 624e7);
        shares.pay(bob);
        assertEq(usdc.balanceOf(bob), 4e6);
        (, uint96 evm, uint96 core) = shares.payments(bob);
        assertEq(evm, 4e6);
        assertEq(core, 376e7);
    }

    /// Of the pool's USDC on HyperEVM no more is taken than what is owed.
    function test_shares_evm_noMoreThanOwed() public {
        _standWithMoney();
        deal(address(usdc), address(m), 50e6);
        _exit(bob, 10e8);
        _payRound();
        // 10 shares of 150: 15, all of it on HyperEVM.
        assertEq(shares.owed(), 15e8);
        assertEq(usdc.balanceOf(address(shares)), 15e6);
        assertEq(usdc.balanceOf(address(m)), 35e6);
        shares.pay(bob);
        assertEq(usdc.balanceOf(bob), 15e6);
    }

    /// A deposit made while money is owed here goes into the pool, and what is owed stays here.
    function test_shares_aDepositWhileMoneyIsOwed_goesIntoThePool() public {
        _standWithMoney();
        _exit(bob, 40e8);
        _payRound();
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 1 minutes + 1);
        _deposit(carol, 10e8);
        assertEq(shares.sharesOf(carol), 10e8);
        assertEq(_spot(address(shares)), 40e8);
        assertEq(_spot(address(m)), 70e8);
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 40e8);
    }

    /// A holder who left in one round and asks again in the next keeps what the first one owes.
    function test_shares_aSecondRequest_keepsWhatTheFirstRoundOwes() public {
        _standWithMoney();
        _exit(bob, 10e8);
        _payRound();
        CoreSimulatorLib.nextBlock();
        assertEq(shares.owedOf(bob), 0); // brought up to date by bob's next call
        _exit(bob, 5e8);
        assertEq(shares.owedOf(bob), 10e8);
        assertEq(shares.exitOf(bob), 5e8);
        assertEq(shares.exitRound(bob), 1);
        (uint256 staying, uint256 leaving, uint256 due) = shares.positionOf(bob);
        assertEq(staying, 25e8);
        assertEq(leaving, 5e8);
        assertEq(due, 10e8);
        shares.pay(bob);
        assertEq(shares.exitOf(bob), 5e8);
    }

    /// Nothing is paid out of a balance that is not there: payments in one block are counted
    /// against each other, since the balance shows none of them leaving until the next.
    function test_shares_pay_onlyOutOfWhatIsHere() public {
        _standWithMoney();
        _deposit(carol, 10e8);
        _creatorStops();
        vm.warp(m.endsAt() + 1);
        _exit(alice, 60e8);
        _exit(bob, 40e8);
        _exit(carol, 10e8);
        shares.closeRound();
        CoreSimulatorLib.nextBlock();
        shares.pay(alice);
        // Alice's 60 still shows; with 90 showing, bob's 40 is not there.
        CoreSimulatorLib.forceSpotBalance(address(shares), 0, 90e8);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotEnoughFree.selector, 90e8, 60e8, 40e8));
        shares.pay(bob);
        CoreSimulatorLib.forceSpotBalance(address(shares), 0, 105e8);
        shares.pay(bob);
        // 100 have left in this block; with 105 showing, carol's 10 are not there.
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotEnoughFree.selector, 105e8, 100e8, 10e8));
        shares.pay(carol);
        CoreSimulatorLib.forceSpotBalance(address(shares), 0, 110e8);
        shares.pay(carol);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(alice), 60e8);
        assertEq(_spot(bob), 40e8);
        assertEq(_spot(carol), 10e8);
    }

    /// Audit S-2: a holder with no HyperCore account, owed less than creating one costs there, was
    /// sent nothing and owed nothing after `pay`. The part that cannot be sent stays owed.
    function test_shares_pay_keepsOwedWhatItCannotSend() public {
        address eve = makeAddr("eve");
        _standWithMoney();
        _deposit(eve, 1e8);
        _creatorStops();
        vm.warp(m.endsAt() + 1);
        _exit(eve, 5e7); // worth 0.5, less than the 1 creating an account costs
        shares.closeRound();
        CoreSimulatorLib.nextBlock();
        vm.expectRevert(PoolShares.NothingToSend.selector);
        shares.pay(eve);
        (,, uint256 due) = shares.positionOf(eve);
        assertEq(due, 5e7, "still owed");
        assertEq(shares.owed(), 5e7);
        // Once the account exists, the same call pays it.
        CoreSimulatorLib.forceAccountActivation(eve);
        shares.pay(eve);
        assertEq(shares.owedOf(eve), 0);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(eve), 5e7);
    }

    /// Paid from HyperEVM in part, a holder is still owed the HyperCore part that could not go.
    function test_shares_pay_sendsTheEvmPartAndKeepsTheRest() public {
        address eve = makeAddr("eve");
        _standWithMoney();
        _deposit(eve, 1e8);
        deal(address(usdc), address(m), 3e5); // 0.3 of exam prices on HyperEVM
        _creatorStops();
        vm.warp(m.endsAt() + 1);
        _exit(eve, 1e8);
        shares.closeRound();
        CoreSimulatorLib.nextBlock();
        (,, uint256 owedNow) = shares.positionOf(eve);
        assertEq(owedNow, 1e8 * shares.roundValue(0) / shares.roundShares(0));
        shares.pay(eve);
        assertEq(usdc.balanceOf(eve), 3e5);
        assertEq(shares.owedOf(eve), owedNow - 3e7, "the HyperCore part stays owed");
        assertEq(shares.owed(), owedNow - 3e7);
    }

    function _standRaise(uint256 target) internal {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, target);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        shares.bind(m, TERMS);
        for (uint256 i = 0; i < 4; ++i) {
            CoreSimulatorLib.forceAccountActivation([alice, bob, carol, dave][i]);
        }
    }

    /// A raise collects its target within thirty days and then starts the pool: until then the
    /// money waits here, nothing is sold and nobody asks to leave.
    function test_shares_raise_startsThePoolAtItsTarget() public {
        _standRaise(100e8);
        assertEq(shares.raiseEnds(), block.timestamp + 30 days);
        _deposit(alice, 60e8);
        assertEq(_spot(address(shares)), 60e8, "it waits here");
        assertEq(m.endsAt(), 0);
        vm.prank(alice);
        vm.expectRevert(PoolShares.NotLaunched.selector);
        shares.requestExit(1e8);
        shares.settle(new address[](0)); // the empty ticket drops off
        vm.expectRevert(abi.encodeWithSelector(PoolShares.RaiseShort.selector, 60e8, 100e8));
        shares.launch();
        _deposit(bob, 40e8);
        shares.launch(); // bob's ticket, emptied, still on the list holds nothing back (deep audit, N1)
        assertTrue(shares.launched());
        assertEq(m.endsAt(), block.timestamp + 365 days);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(m)), 100e8);
        assertEq(_spot(address(shares)), 0);
        vm.expectRevert(PoolShares.AlreadyLaunched.selector);
        shares.launch();
    }

    /// The cloud audit's N1, turned round: a stranger settled one fresh ticket of their own at every
    /// pass, so that the closed list was never empty when the launch was tried, and a raise that met
    /// its target never started. A ticket closed in the very block of the launch holds it back no
    /// more: its money is counted, and lands here.
    function test_deep_N1_aTicketClosedInTheLaunchsBlockDoesNotHoldItBack() public {
        _standRaise(100e8);
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
        shares.settle(new address[](0));
        address eve = makeAddr("eve");
        CoreSimulatorLib.forceAccountActivation(eve);
        address t = _ticket(eve, 1e8);
        shares.settle(_list(t)); // closed, its sweep on its way, still listed
        assertEq(shares.closedTickets().length, 1);
        vm.prank(stranger);
        shares.launch();
        assertTrue(shares.launched());
        assertEq(shares.sharesOf(eve), 1e8);
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        assertEq(shares.value(), 101e8, "eve's 1 is counted once, landed here");
        shares.settle(new address[](0));
        assertEq(shares.closedTickets().length, 0, "the emptied ticket drops off");
    }

    /// The other way the audit found: a closed ticket paid into again and named once more, in the
    /// block an honest caller launches. It holds nothing back either.
    function test_deep_N1_aRelistedTicketDoesNotHoldTheLaunchBack() public {
        _standRaise(100e8);
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
        shares.settle(new address[](0));
        address t = _deposit(carol, 1e8);
        shares.settle(new address[](0));
        CoreSimulatorLib.forceSpotBalance(t, 0, 1e8);
        shares.settle(_list(t)); // listed again
        vm.prank(stranger);
        shares.launch();
        assertTrue(shares.launched());
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        assertEq(shares.value(), 102e8, "carol's second 1 is the holders', counted once");
    }

    /// A raise short of its target after thirty days is handed back, every deposit at one price,
    /// less the platform's 0.5 %.
    function test_shares_raise_fallsShort_andIsHandedBackLessTheFee() public {
        vm.prank(operator);
        factory.setChallengeFee(0, operator);
        _standRaise(100e8);
        _deposit(alice, 60e8);
        _deposit(bob, 20e8);
        shares.settle(new address[](0));
        vm.expectRevert(PoolShares.NotRefunding.selector);
        shares.refund(alice);
        vm.warp(shares.raiseEnds() + 1);
        vm.expectRevert(PoolShares.RaiseOver.selector);
        shares.launch();
        vm.prank(carol);
        vm.expectRevert(PoolShares.RaiseOver.selector);
        shares.openTicket(TERMS);
        shares.refund(alice);
        shares.refund(bob);
        assertEq(shares.owedOf(alice), 597e7);
        assertEq(shares.owedOf(bob), 199e7);
        assertEq(shares.feeOwed(), 4e7);
        assertEq(shares.totalShares(), 0, "every share handed back");
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotFree.selector, 0));
        shares.refund(alice);
        shares.pay(alice);
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        CoreSimulatorLib.forceAccountActivation(operator);
        shares.payFee();
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(alice), 597e7);
        assertEq(_spot(bob), 199e7);
        assertEq(_spot(operator), 4e7);
        assertEq(shares.owed(), 0);
    }

    /// With no fee recipient the platform takes nothing; a ticket paid into after the price was
    /// taken is not counted, and goes back to whoever opened it.
    function test_shares_raise_noRecipientNoFee_andNothingCountedAfterThePrice() public {
        _standRaise(100e8);
        _deposit(alice, 60e8);
        shares.settle(new address[](0));
        address late = _ticket(bob, 10e8);
        vm.warp(shares.raiseEnds() + 1);
        shares.refund(alice);
        assertEq(shares.owedOf(alice), 60e8);
        assertEq(shares.feeOwed(), 0);
        shares.settle(_list(late));
        assertEq(shares.sharesOf(bob), 0);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 10e8);
    }

    /// The audit's H-1: a deposit nobody counted before a raise that fell short took its price,
    /// and whatever reaches its ticket later, goes back to the one who opened the ticket, whole,
    /// and nothing of it is owed or counted.
    function test_shares_raise_aDepositNotCountedGoesBackToItsDepositor() public {
        _standRaise(1000e8);
        _deposit(alice, 60e8);
        address late = _ticket(bob, 40e8); // sent in the raise's last hour, not settled yet
        vm.warp(shares.raiseEnds() + 1);
        shares.refund(alice); // anyone; the refund's price is taken now
        uint256 owedBefore = shares.owed();
        vm.expectEmit(true, true, false, true, address(shares));
        emit PoolShares.TicketReturned(late, bob, 40e8, 0);
        shares.settle(_list(late));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(late), 0);
        assertEq(_spot(bob), 40e8, "back to bob");
        assertEq(shares.sharesOf(bob), 0);
        assertEq(shares.owed(), owedBefore, "nothing owed for it");
        (, PoolShares.TicketState st) = shares.tickets(late);
        assertEq(uint8(st), uint8(PoolShares.TicketState.Open), "it stays bob's ticket");
        // Whatever reaches the ticket later goes the same way.
        CoreSimulatorLib.forceSpotBalance(late, 0, 7e8);
        shares.settle(_list(late));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 47e8);
        // Less than SWEEP_MIN is dust, and stays.
        CoreSimulatorLib.forceSpotBalance(late, 0, 9e7);
        shares.settle(_list(late));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 47e8);
    }

    /// With a fee recipient, a deposit handed back pays the platform's 0.5 % like every deposit
    /// handed back (the terms, D7 and D10), straight from the ticket; so does the pool's own money.
    function test_shares_raise_whatIsHandedBackPaysThePlatformsHalfPercent() public {
        vm.prank(operator);
        factory.setChallengeFee(0, operator);
        CoreSimulatorLib.forceAccountActivation(operator);
        _standRaise(1000e8);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 100e8); // the creator's, in the pool
        _deposit(alice, 60e8);
        address late = _ticket(bob, 40e8);
        vm.warp(shares.raiseEnds() + 1);
        shares.refund(alice);
        vm.expectEmit(true, true, false, true, address(shares));
        emit PoolShares.TicketReturned(late, bob, 398e7, 2e7);
        shares.settle(_list(late));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 398e7);
        assertEq(_spot(operator), 2e7);
        assertEq(_spot(late), 0);
        uint256 feeBefore = shares.feeOwed();
        shares.reclaim();
        assertEq(shares.owedOf(creator), 995e7);
        assertEq(shares.feeOwed(), feeBefore, "the pool keeps the 0.5 % as it pays, not this contract");
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(operator), 2e7 + 5e7, "the platform has its part of the hand-back and of the pool's money");
    }

    /// The platform's part of a deposit handed back is kept whatever its account (D10; CTO, 17
    /// October 2026): with no HyperCore account to take it yet, it is kept here and owed to the
    /// platform, and `payFee` sends it once the account can be paid.
    function test_shares_raise_aFeeTheRecipientsAccountCannotTakeYetIsKeptForIt() public {
        address newcomer = makeAddr("fee-recipient-with-no-account");
        vm.prank(operator);
        factory.setChallengeFee(0, newcomer);
        _standRaise(1000e8);
        _deposit(alice, 60e8);
        address late = _ticket(bob, 40e8); // 0.5 % is 0.2, under the 1 USDC an account costs
        vm.warp(shares.raiseEnds() + 1);
        shares.refund(alice);
        uint256 feeBefore = shares.feeOwed();
        uint256 owedBefore = shares.owed();
        vm.expectEmit(true, true, false, true, address(shares));
        emit PoolShares.TicketReturned(late, bob, 398e7, 2e7);
        shares.settle(_list(late));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 398e7, "bob: his 40 less 0.5 %");
        assertEq(_spot(late), 0);
        assertEq(shares.feeOwed(), feeBefore + 2e7, "the 0.2 is the platform's, kept here");
        assertEq(shares.owed(), owedBefore + 2e7);
        CoreSimulatorLib.forceAccountActivation(newcomer);
        shares.payFee();
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(newcomer), feeBefore + 2e7);
    }

    /// A depositor with no HyperCore account gets their deposit less the 1 USDC that creating it
    /// costs; the account is theirs.
    function test_shares_raise_aDepositGoesBackLessWhatTheDepositorsAccountCosts() public {
        _standRaise(1000e8);
        _deposit(alice, 60e8);
        address erin = makeAddr("erin");
        address late = _ticket(erin, 40e8);
        vm.warp(shares.raiseEnds() + 1);
        shares.refund(alice);
        shares.settle(_list(late));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(erin), 39e8);
        assertEq(_spot(late), 0);
    }

    /// The audit's M-1: what the pool held when a raise fell short was the creator's, and it comes
    /// back to them whole. The pool, which never began, ends; nothing can begin it afterwards.
    function test_shares_raise_aFailedRaiseGivesThePoolsMoneyBackToTheCreator() public {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 1000e8);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(creator);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 100e8); // the creator's 100
        deal(address(usdc), address(m), 5e6); // and 5 on HyperEVM
        vm.prank(creator);
        shares.bind(m, TERMS);
        vm.expectRevert(PoolShares.NotRefunding.selector);
        shares.reclaim();
        vm.warp(shares.raiseEnds() + 400 days);
        vm.prank(stranger);
        shares.reclaim();
        assertTrue(m.closing());
        assertEq(m.endsAt(), block.timestamp - 1, "the pool that never began has ended");
        assertEq(shares.owedOf(creator), 105e8, "no fee recipient, no fee");
        assertEq(usdc.balanceOf(address(m)), 0);
        // In flight, nothing more to take.
        vm.expectRevert(PoolShares.NothingToSend.selector);
        shares.reclaim();
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        shares.pay(creator);
        CoreSimulatorLib.nextBlock();
        assertEq(usdc.balanceOf(creator), 5e6);
        assertEq(_spot(creator), 100e8);
        assertEq(_spot(address(m)), 0);
        assertEq(shares.owed(), 0);
        vm.prank(address(shares));
        vm.expectRevert(MultiPool.Started.selector);
        m.begin(TERMS);
    }

    /// The audit's M-2: a holder who asked to leave is not held for a renewal because nobody closed
    /// the round in the last fourteen days. After the end, anyone may close it, at that moment's
    /// price, and the pool pays it; until then the round stands as it was.
    function test_shares_aLeaverNobodyRenewedForLeavesAtAnyTimeAfterTheEnd() public {
        _standWithMoney();
        uint256 n = shares.sharesOf(bob);
        vm.prank(bob);
        shares.requestExit(n);
        uint64 end = m.endsAt();
        vm.warp(end + 1);
        assertEq(m.endsAt() - end, 90 days, "the term renewed by itself");
        vm.expectRevert(PoolShares.NotEnded.selector);
        shares.closeRound(); // the pool has not stopped
        vm.prank(creator);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end + 90 days - 14 days + 1));
        shares.extend(180 days); // an extension is still the last days' only
        vm.warp(end + 30 days);
        vm.prank(bob);
        shares.renew();
        (uint256 staying, uint256 leaving, uint256 due) = shares.positionOf(bob);
        assertEq((staying + leaving), 0);
        assertEq(due, 40e8, "at the price of this moment");
        assertEq(m.reserve(), 0);
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 40e8);
        assertEq(shares.totalShares(), 60e8);
        assertEq(m.endsAt(), end + 90 days, "the pool runs on for those who stay");
        // Closed, nothing more to close.
        vm.expectRevert(PoolShares.NothingRequested.selector);
        shares.renew();
    }

    /// What the pool held before the launch is the creator's, as shares at the raise's price.
    function test_shares_launch_whatThePoolHeldIsTheCreators() public {
        _standRaise(50e8);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 100e8);
        _deposit(alice, 50e8);
        assertEq(shares.value(), 50e8, "before the launch the pool's own money is not the holders'");
        shares.settle(new address[](0));
        shares.launch();
        assertEq(shares.sharesOf(creator), 100e8);
        assertEq(shares.sharesOf(alice), 50e8);
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        assertEq(shares.value(), 150e8);
    }

    /// The creator accepts the pool's terms by their hash, and so does every depositor.
    function test_shares_terms_areAcceptedByTheirHash() public {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        vm.expectRevert(PoolShares.WrongTerms.selector);
        shares.bind(m, keccak256("other terms"));
        vm.prank(creator);
        vm.expectEmit(true, false, false, true, address(shares));
        emit PoolShares.TermsAccepted(creator, TERMS);
        shares.bind(m, TERMS);
        assertEq(shares.terms(), TERMS);
        vm.prank(alice);
        vm.expectRevert(PoolShares.WrongTerms.selector);
        shares.openTicket(keccak256("other terms"));
        vm.prank(alice);
        vm.expectEmit(true, false, false, true, address(shares));
        emit PoolShares.TermsAccepted(alice, TERMS);
        shares.openTicket(TERMS);
    }

    /// A creator binds only terms whose text the plan's book holds; a ticket names the hash bound.
    function test_shares_bind_takesOnlyThePoolsOwnTerms() public {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        // Terms nobody can read never make a pool (MultiPool's constructor, deep audit F13), and
        // the creator binds only the hash the pool holds.
        vm.prank(creator);
        vm.expectRevert(PoolShares.WrongTerms.selector);
        shares.bind(m, keccak256(bytes(TERMS_TEXT)));
        vm.prank(creator);
        shares.bind(m, TERMS);
    }

    /// The deep audit's F5 (16 October 2026), turned round, on its stand: the window is missed, the
    /// late renew cannot pay the leaver because the money is in the traders' accounts, so the pool
    /// stops; it stops now, as it would have at the end the leaver asked for, not at the end of the
    /// renewal. Bob is paid the day after E. On f8bc60aa the stop landed at E + 90 days.
    function test_deep_F5_aLateRenewThatCannotPayStopsThePoolNow() public {
        _standWithMoney();
        Pool s1 = _fund(carol, 2);
        vm.warp(block.timestamp + 1 minutes);
        Pool s2 = _fund(dave, 2);
        vm.warp(block.timestamp + 1 minutes);
        Pool s3 = _fund(trader, 2);
        _exit(bob, 40e8);
        uint64 end = m.endsAt();
        vm.warp(end + 1);
        assertEq(m.endsAt(), end + 90 days, "renewed by itself");
        vm.prank(stranger);
        shares.renew();
        assertTrue(m.stopping());
        assertEq(m.endsAt(), block.timestamp, "the stop ends the term now, not at E + 90 days");
        vm.warp(block.timestamp + 1);
        (Cancel[] memory c, uint32[] memory a) = _none();
        Pool[3] memory seats = [s1, s2, s3];
        for (uint256 i = 0; i < 3; ++i) {
            m.endSeat(address(seats[i]), c, a, SALT);
            _settle(seats[i]);
            m.finish(address(seats[i]));
        }
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        shares.closeRound();
        assertEq(shares.round(), 1);
        CoreSimulatorLib.nextBlock();
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertGt(_spot(bob), 38e8);
        assertLt(block.timestamp, end + 1 days, "bob is paid within a day of the end he asked for");
    }

    /// The deep audit's F16 (16 October 2026), turned round, on its stand: the creator cannot stop a
    /// raise before it launches, so one that has its target launches. On f8bc60aa the creator's stop
    /// ended the pool that never began, and every depositor waited 30 days and paid 0.5 %.
    function test_deep_F16_theCreatorCannotStopARaise_andAMetRaiseLaunches() public {
        vm.prank(operator);
        factory.setChallengeFee(0, operator);
        _standRaise(100e8);
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
        shares.settle(new address[](0));
        vm.prank(creator);
        vm.expectRevert(PoolShares.NotLaunched.selector);
        shares.stop();
        assertEq(m.endsAt(), 0, "the pool has not begun, and has not ended");
        vm.prank(stranger);
        shares.launch();
        assertTrue(shares.launched());
        vm.prank(creator);
        shares.stop(); // once launched, the creator's stop is as it was
        assertTrue(m.stopping());
    }

    /// The deep audit's F15 (16 October 2026), turned round in part: one depositor holds four open
    /// tickets at most, and gets another once one is counted. On f8bc60aa one address opened 300.
    function test_deep_F15_aDepositorHoldsFourOpenTicketsAtMost() public {
        _stand();
        for (uint256 i = 0; i < 4; ++i) {
            vm.prank(alice);
            shares.openTicket(TERMS);
        }
        vm.prank(alice);
        vm.expectRevert(PoolShares.TooMany.selector);
        shares.openTicket(TERMS);
        address t = shares.openTickets(0, 1)[0];
        CoreSimulatorLib.forceAccountActivation(t);
        CoreSimulatorLib.forceSpotBalance(t, 0, 10e8);
        shares.settle(_list(t));
        assertEq(shares.openOf(alice), 3, "one counted");
        vm.prank(alice);
        shares.openTicket(TERMS);
    }

    /// The cloud audit's F38, closed the way Alex chose (DECISIONS 103): a leaver is paid at the end of
    /// the term or after it, at the pool's value then, as D5 and D6 say. Nothing leaves in the last
    /// days, not with the creator's extension either, and what the pool made in them is the
    /// leaver's part too.
    function test_deep_F38_aLeaverIsPaidAtTheValueAfterTheEnd_notInTheLastDays() public {
        _standWithMoney();
        _exit(bob, 40e8);
        uint64 end = m.endsAt();
        _window();
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 110e8);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end + 1));
        shares.renew();
        _extend(180 days);
        assertEq(shares.round(), 0, "the extension paid nothing");
        assertEq(shares.owed(), 0);
        vm.prank(address(shares));
        vm.expectRevert(MultiPool.Locked.selector);
        m.extend(0, 1e8, 0); // nor may the pool's owner take money out in those days
        vm.warp(end - 1);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 120e8);
        vm.warp(end + 1);
        shares.renew();
        assertEq(shares.roundValue(0), 120e8, "at the value at the end");
        (,, uint256 due) = shares.positionOf(bob);
        assertEq(due, 48e8, "bob: 40 % of 120, the last days' gain with it");
    }

    /// The cloud audit's N4, turned round: bob asks 40 of 100 out, and a seat gains 12 after (111 once
    /// its account fee is gone). The share in work is measured on the pool less the leavers' part of
    /// it as it is now (44.4), not as it was when they asked (40): a 25 sells, 50 in work within 80 %
    /// of 66.6, and a 5 after it does not. After the end the round pays bob and the pool runs on,
    /// where the snapshot let the 5 through and the round stopped the pool.
    function test_deep_N4_aSaleIsMeasuredOnTheLeaversPartAsItIsNow_andTheRoundStillPays() public {
        _standWithMoney();
        _exit(bob, 40e8);
        Pool seat = _fund(carol, 2);
        vm.warp(block.timestamp + 1 minutes + 1);
        CoreSimulatorLib.forceSpotBalance(address(seat), 0, _spot(address(seat)) + 12e8);
        uint64 end = m.endsAt();
        _window();
        _buy(dave, 2);
        assertEq(m.inWork(), 50e6);
        assertEq(m.room(0), 0, "no 5 more");
        uint256 price = abi.decode(CodeStore.read(m.planData()), (Plan)).menu[0].price;
        deal(address(usdc), trader, price);
        vm.startPrank(trader);
        usdc.approve(address(m), price);
        vm.expectRevert(MultiPool.NoRoom.selector);
        m.buy(0);
        vm.stopPrank();
        vm.warp(end + 1);
        shares.renew();
        assertFalse(m.stopping(), "the pool runs on");
        assertEq(shares.round(), 1, "bob's round is paid");
    }

    /// The cloud audit's F3, turned round: in the minute after the pool sent a seat its money it counts
    /// that money against its balance, and a round closed in it found the balance short and stopped a
    /// pool that could pay. Now the round is not closed in that minute (PoolSending): it waits, and a
    /// minute later it pays and the pool runs on.
    function test_deep_F3_aRoundIsNotClosedInTheMinuteAfterAnArm_andIsPaidAfterIt() public {
        _standWithMoney();
        _fund(carol, 2);
        vm.warp(block.timestamp + 1 minutes + 1);
        _exit(bob, 30e8);
        uint64 end = m.endsAt();
        vm.warp(end - 1 days);
        PaperExam e = _buy(dave, 2);
        _pass(e, dave);
        vm.warp(end + 1);
        vm.prank(dave);
        m.claim(address(e));
        CoreSimulatorLib.nextBlock();
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotQuiet.selector, PoolShares.Blocker.PoolSending, address(m)));
        shares.renew();
        assertFalse(m.stopping(), "not stopped");
        vm.warp(m.lastSendAt() + m.ARM_WAIT() + 1);
        shares.renew();
        assertFalse(m.stopping(), "the pool runs on");
        assertEq(shares.round(), 1, "bob is paid");
    }

    /// Only the pool's word that the money is not there stops it; any other refusal of the payment
    /// is passed on, the pool runs on, and the round waits for the next call.
    function test_deep_F3_anotherRefusalIsPassedOn_andThePoolDoesNotStop() public {
        _standWithMoney();
        _exit(bob, 40e8);
        vm.warp(uint256(m.reserveFor()) + 1);
        vm.mockCallRevert(address(m), abi.encodeWithSelector(MultiPool.extend.selector),
            abi.encodeWithSelector(MultiPool.Locked.selector));
        vm.expectRevert(MultiPool.Locked.selector);
        shares.renew();
        assertFalse(m.stopping());
        vm.clearMockedCalls();
        _everyOwnerRecognized();
        shares.renew();
        assertEq(shares.round(), 1);
    }

    /// The other way a stranger could turn the round into a stop: buy an exam just before it, so that
    /// the leavers' money goes to a seat. A sale leaves their part of the pool on its balance, or does
    /// not happen. Here a seat holds 75 of a pool of 150, 60 of it bob's: a 25 is refused, though the
    /// share in work would take it, and after the end the round pays bob.
    function test_deep_F3_aSaleThatWouldLeaveTheLeaversMoneyInASeatIsRefused() public {
        _standWithMoney();
        _exit(bob, 40e8);
        Pool seat = _fund(carol, 2);
        vm.warp(block.timestamp + 1 minutes + 1);
        CoreSimulatorLib.forceSpotBalance(address(seat), 0, _spot(address(seat)) + 50e8);
        assertGt(m.room(2), 0, "the share in work would take a 25");
        uint256 price = abi.decode(CodeStore.read(m.planData()), (Plan)).menu[2].price;
        deal(address(usdc), dave, price);
        vm.startPrank(dave);
        usdc.approve(address(m), price);
        vm.expectRevert(MultiPool.NoRoom.selector);
        m.buy(2);
        vm.stopPrank();
        vm.warp(uint256(m.reserveFor()) + 1);
        shares.renew();
        assertFalse(m.stopping(), "the pool runs on");
        assertEq(shares.round(), 1, "bob is paid his 40 % of 150");
    }

    /// A round paid out of money this contract already holds takes nothing out of the pool, and still
    /// leaves no reserve behind: what was reserved for the leavers has left with them.
    function test_shares_renew_paidFromMoneyHere_leavesNoReserve() public {
        _standWithMoney();
        _exit(bob, 40e8);
        assertGt(m.reserve(), 0);
        CoreSimulatorLib.forceSpotBalance(address(shares), 0, 100e8); // the holders' money, here
        _payRound();
        assertEq(shares.round(), 1);
        assertEq(m.reserve(), 0, "nothing reserved for leavers who have gone");
    }

    /// The creator has no way to the money: the pool answers only this contract, and this
    /// contract pays only holders.
    function test_shares_theCreatorHasNoWayToTheMoney() public {
        _standWithMoney();
        _creatorStops();
        vm.warp(m.endsAt() + 1);
        vm.startPrank(creator);
        vm.expectRevert(MultiPool.NotOwner.selector);
        m.withdraw(1e8);
        vm.expectRevert(MultiPool.NotOwner.selector);
        m.extend(180 days, 0, 0);
        vm.expectRevert(PoolShares.NothingOwed.selector);
        shares.pay(creator);
        vm.stopPrank();
    }
}
