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
        p.terms = TERMS;
    }

    bytes32 constant TERMS = keccak256("the pool's terms, every clause");

    /// A shares contract, its pool, both on HyperCore, bound.
    function _stand() internal {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        m = new MultiPool(address(shares), factory, _examImpl(), _plan(), 180 days, 730 days, 14 days, 90 days);
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

    function _extend(uint32 term) internal {
        vm.prank(creator);
        shares.extend(term);
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
        m = new MultiPool(address(shares), factory, _examImpl(), _plan(), 180 days, 730 days, 14 days, 90 days);
        MultiPool other = new MultiPool(investor, factory, _examImpl(), _plan(), 180 days, 730 days, 14 days, 90 days);

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
        MultiPool m2 = new MultiPool(address(s2), factory, _examImpl(), _plan(), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(m2));
        vm.expectRevert(PoolShares.NotReady.selector);
        s2.bind(m2, TERMS);
        vm.stopPrank();
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
        vm.warp(m.endsAt() + 1);
        shares.requestExit(1e8);
        shares.cancelExit(1e8);
        vm.stopPrank();
        assertEq(shares.exitOf(bob), 7e8);
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

    /// Bob leaves with 40 of 100 shares when the pool is worth 110: the extension takes his 44 out
    /// and pays it once it has landed; alice's 60 shares are still worth 66, and she stays.
    function test_shares_extend_takesOutWhatTheLeaversAreWorth_theRestStay() public {
        _standWithMoney();
        _exit(bob, 40e8);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 110e8);
        _window();
        _extend(180 days);
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
        _window();
        // The creator's own extension cannot pay them, and so does not happen.
        vm.prank(creator);
        vm.expectRevert(PoolShares.CannotPay.selector);
        shares.extend(180 days);
        // The round the term itself renews on cannot pay them either: the pool stops instead.
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

    /// The term renews by itself; in its last days anyone closes the round of those who asked to
    /// leave, they are paid, and the pool runs on for those who stay.
    function test_shares_renew_byAnyone_paysTheLeavers_andThePoolRunsOn() public {
        _standWithMoney();
        _exit(bob, 40e8);
        assertEq(m.reserve(), 40e8, "the pool sells nothing against what is to leave");
        uint64 end = m.endsAt();
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotInWindow.selector, end - 14 days + 1));
        shares.renew();
        _window();
        vm.prank(stranger);
        shares.renew();
        assertEq(shares.round(), 1);
        assertEq(m.reserve(), 0);
        assertFalse(m.stopping());
        vm.expectRevert(PoolShares.NothingRequested.selector);
        shares.renew();
        CoreSimulatorLib.nextBlock();
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 40e8);
        vm.warp(end + 1);
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
        _window();
        _extend(180 days);
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
        _window();
        _extend(180 days);
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
        _window();
        _extend(180 days);
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
        _window();
        _extend(180 days);
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
        _window();
        _extend(180 days);
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
        m = new MultiPool(address(shares), factory, _examImpl(), _plan(), 180 days, 730 days, 14 days, 90 days);
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
        vm.expectRevert(PoolShares.NotReady.selector);
        shares.launch(); // bob's ticket is still on the list
        shares.settle(new address[](0));
        shares.launch();
        assertTrue(shares.launched());
        assertEq(m.endsAt(), block.timestamp + 365 days);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(m)), 100e8);
        assertEq(_spot(address(shares)), 0);
        vm.expectRevert(PoolShares.AlreadyLaunched.selector);
        shares.launch();
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
    /// taken is not counted.
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
        m = new MultiPool(address(shares), factory, _examImpl(), _plan(), 180 days, 730 days, 14 days, 90 days);
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
