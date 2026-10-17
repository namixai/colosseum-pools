// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {Pool} from "../../src/Pool.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {PoolShares} from "../../src/multi/PoolShares.sol";
import {TermsBook} from "../../src/multi/TermsBook.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The deep audit's F8 (16 October 2026), turned round: the platform takes what the accepted terms
/// say (ops/terms.py, template 2, D6, D7 and D10), 0.5 % of each exam's price and 0.5 % of every
/// withdrawal, whatever the factory's flat fee is. Each test failed on f8bc60aa, where the exam paid
/// the factory's flat fee and no withdrawal paid anything. The stand is the audit's.
contract DeepFeesTest is PoolHarness {
    PaperExam examImpl;
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address creator = makeAddr("creator");

    // ops/plans/multi-testnet-run.json: a 5 USDC exam priced 0.05 USDC; ops/deploy_testnet.py:49: fee 0.7 USDC.
    uint64 constant TESTNET_PRICE = 50_000;
    uint256 constant TESTNET_FEE = 700_000;
    uint256 constant TERMS_FEE_BPS = 50;

    /// The real clauses, verbatim from ops/terms.py:55-62 and 72-73 (template 2).
    function _clauses() internal pure returns (string memory) {
        return string.concat(
            "Terms of a pool of many traders on Hyperliquid, template 2.\n",
            "D6. If you said \"I'm leaving\", your share is paid out at the end of the term at the pool's value at that ",
            "moment, less the withdrawal fee; a pool with several investors keeps working. A single-investor pool ends ",
            "only when its owner says \"stop\": no new challenges are sold, positions are closed and profit is split. If ",
            "at the end of a term there is not enough money to pay those leaving, the pool stops in the same way.\n",
            "D7. The creator sets the amount to raise; the raise lasts 30 days. If the amount is reached, the pool starts ",
            "and its term begins. If not, every deposit is returned less 0.5 % \xe2\x80\x94 the platform's withdrawal fee. ",
            "[PoolShares.RAISE_TIME = 2592000; PoolShares.RAISE_FEE_BPS = 50]\n",
            "D10. The platform takes 0.5 % of each challenge price and 0.5 % of every withdrawal, including the return ",
            "of a deposit when a raise falls short; both are already in the estimate.\n"
        );
    }

    function _realTerms() internal pure returns (bytes32) {
        return keccak256(bytes(_clauses()));
    }

    function _examImpl() internal returns (address) {
        if (address(examImpl) == address(0)) examImpl = new PaperExam();
        return address(examImpl);
    }

    function _plan(uint64 price) internal pure returns (Plan memory p) {
        p.menu = new Size[](3);
        p.menu[0] = Size({size: 5e6, price: price});
        p.menu[1] = Size({size: 10e6, price: price * 2});
        p.menu[2] = Size({size: 25e6, price: price * 5});
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

    /// Publishes the real clauses in the book and gives the platform a HyperCore account, so that a
    /// fee could be sent there if the code ever sent one.
    function _stand() internal {
        vm.warp(1_790_000_000);
        bytes32 h = TermsBook(TERMS_BOOK).publish(_clauses());
        assertEq(h, _realTerms());
        assertTrue(bytes(TermsBook(TERMS_BOOK).text(h)).length > 0, "the clauses are readable on chain");
        CoreSimulatorLib.forceAccountActivation(operator);
        CoreSimulatorLib.forceAccountActivation(investor);
        vm.prank(operator);
        factory.setChallengeFee(TESTNET_FEE, operator);
    }

    function _ownerPool(uint64 spot1e8) internal returns (MultiPool m) {
        m = new MultiPool(investor, factory, _examImpl(), _tie(_plan(TESTNET_PRICE), _clauses()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, spot1e8);
        vm.prank(investor);
        m.begin(TERMS); // the owner accepts D6, D7, D10 by naming their hash
    }

    function _fund(address who, MultiPool m, uint256 line, uint256 allowance) internal returns (uint256 cost) {
        cost = abi.decode(CodeStore.read(m.planData()), (Plan)).menu[line].price;
        if (usdc.balanceOf(who) < cost) deal(address(usdc), who, cost);
        vm.prank(who);
        usdc.approve(address(m), allowance);
    }

    // ── 1. the exam fee ──────────────────────────────────────────────────────────────

    /// An exam pays the platform 0.5 % of its price, out of the price (item 28), not the factory's
    /// flat fee, however the operator sets that afterwards; a free exam pays nothing.
    function test_fees_anExamPaysHalfAPercentOfItsPrice_whateverTheFactorysFlatFee() public {
        _stand();
        MultiPool m = _ownerPool(100e8);
        uint256 termsFee = (uint256(TESTNET_PRICE) * TERMS_FEE_BPS) / 10_000; // 250 units
        uint256 cost = _fund(alice, m, 0, type(uint256).max);
        assertEq(cost, TESTNET_PRICE, "the buyer pays the price and no more");
        vm.expectEmit(true, true, false, true, address(m));
        emit MultiPool.ExamFeePaid(alice, operator, termsFee);
        vm.prank(alice);
        m.buy(0);
        assertEq(usdc.balanceOf(address(m)), TESTNET_PRICE - termsFee, "the pool got the price less 0.5 %");
        assertEq(usdc.balanceOf(operator), termsFee, "the platform got 0.5 % of it");
        assertEq(usdc.balanceOf(alice), 0);

        // The operator's flat fee moves; this pool's fee does not.
        vm.prank(operator);
        factory.setChallengeFee(100e6, operator);
        uint256 before = usdc.balanceOf(operator);
        _fund(bob, m, 0, type(uint256).max);
        vm.prank(bob);
        m.buy(0);
        assertEq(usdc.balanceOf(operator) - before, termsFee, "still 0.5 % of the price");
        assertEq(usdc.balanceOf(bob), 0, "bob paid the price, nothing more");
    }

    // ── 2. the owner's withdrawals ───────────────────────────────────────────────────

    /// The single-investor pool's three ways out, `extend` with money reserved to leave once the end
    /// it was reserved for has passed, `withdraw` and `withdrawEvm`, each pay the platform 0.5 % of
    /// what leaves.
    function test_fees_everyOwnerWithdrawalPaysHalfAPercent() public {
        _stand();
        MultiPool m = _ownerPool(100e8);
        deal(address(usdc), address(m), 10e6); // exam prices held on HyperEVM
        uint64 opCoreBefore = _spot(operator);
        uint256 opEvmBefore = usdc.balanceOf(operator);

        vm.prank(investor);
        m.setReserve(25e8);
        vm.warp(m.endsAt() + 1);
        vm.prank(investor);
        m.extend(180 days, 20e8, 4e6);
        assertEq(usdc.balanceOf(investor), 398e4, "4 less 0.5 %");
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(investor), 199e7, "20 less 0.5 %");

        vm.prank(investor);
        m.setStop(true);
        vm.warp(m.endsAt() + 1);
        vm.startPrank(investor);
        m.withdraw(30e8);
        m.withdrawEvm(6e6);
        vm.stopPrank();
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(investor), 199e7 + 2985e6, "30 more, less 0.5 %");
        assertEq(usdc.balanceOf(investor), 398e4 + 597e4, "6 more, less 0.5 %");
        assertEq(_spot(operator) - opCoreBefore, 25e6, "0.5 % of 50 USDC on HyperCore");
        assertEq(usdc.balanceOf(operator) - opEvmBefore, 5e4, "0.5 % of 10 USDC on HyperEVM");
    }

    // ── 3. a shared pool's leavers ───────────────────────────────────────────────────

    PoolShares shares;
    MultiPool sm;

    function _ticket(address who, uint64 amount) internal returns (address t) {
        vm.prank(who);
        t = shares.openTicket(TERMS);
        CoreSimulatorLib.forceAccountActivation(t);
        CoreSimulatorLib.forceSpotBalance(t, 0, amount);
    }

    function _deposit(address who, uint64 amount) internal {
        address t = _ticket(who, amount);
        address[] memory l = new address[](1);
        l[0] = t;
        shares.settle(l);
        CoreSimulatorLib.nextBlock();
    }

    function _sharedStand(uint256 target) internal {
        _stand();
        shares = new PoolShares(creator, usdc, 1e8, target);
        sm = new MultiPool(address(shares), factory, _examImpl(), _tie(_plan(TESTNET_PRICE), string.concat(_sharesLine(shares.target(), shares.minDeposit()), _clauses())), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(sm));
        CoreSimulatorLib.forceAccountActivation(alice);
        CoreSimulatorLib.forceAccountActivation(bob);
        vm.prank(creator);
        shares.bind(sm, TERMS);
    }

    /// A leaver is paid what their shares are worth less 0.5 %, whether the round closes after the
    /// end it was asked for or with `closeRound` after a stop. The pool keeps the platform's part as it pays,
    /// so nothing is charged twice and `feeOwed` stays zero.
    function test_fees_aLeaverIsPaidLessHalfAPercent_chargedOnce() public {
        _sharedStand(0);
        shares.launch();
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
        uint64 opBefore = _spot(operator);

        vm.prank(bob);
        shares.requestExit(40e8);
        vm.warp(sm.endsAt() - sm.notice() + 1);
        vm.prank(creator);
        shares.extend(180 days);
        vm.warp(uint256(sm.reserveFor()) + 1);
        shares.renew();
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 1 minutes);
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 398e7, "bob got his 40 less 0.5 %");
        assertEq(shares.feeOwed(), 0, "the pool kept the platform's part as it paid");

        vm.warp(block.timestamp + 2 minutes);
        vm.prank(creator);
        shares.stop();
        vm.warp(sm.endsAt() + 1);
        vm.prank(alice);
        shares.requestExit(60e8);
        shares.closeRound();
        CoreSimulatorLib.nextBlock();
        shares.pay(alice);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(alice), 597e7, "alice got her 60 less 0.5 %");
        assertEq(_spot(operator) - opBefore, 5e7, "the platform got 0.5 % of the 100 USDC that left");
        assertEq(shares.owed(), 0);
    }

    /// What a leaver is owed and what leaves the pool add up to the same: a round closed partly out
    /// of money this contract already holds pays the platform its part of that too, through payFee.
    function test_fees_theFeeOnMoneyAlreadyHereGoesThroughPayFee() public {
        _sharedStand(0);
        shares.launch();
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
        vm.prank(bob);
        shares.requestExit(40e8);
        CoreSimulatorLib.forceSpotBalance(address(shares), 0, 10e8); // 10 already here, the holders'
        vm.warp(sm.endsAt() - sm.notice() + 1);
        vm.prank(creator);
        shares.extend(180 days);
        vm.warp(uint256(sm.reserveFor()) + 1);
        shares.renew();
        // 110 in all: bob's 40 % is 44, less 0.22; the pool paid 34 and kept 0.17 of it.
        assertEq(shares.feeOwed(), 22e6 - 17e6, "the 0.5 % on the 10 that did not come from the pool");
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 2 minutes);
        shares.pay(bob);
        shares.payFee();
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(bob), 4378e6, "44 less 0.5 %");
    }

    /// A raise that falls short is handed back less 0.5 %, as before.
    function test_fees_aFailedRaiseStillTakesHalfAPercent() public {
        _sharedStand(1000e8);
        _deposit(alice, 60e8);
        vm.warp(shares.raiseEnds() + 1);
        shares.refund(alice);
        assertEq(shares.feeOwed(), 3e7);
        assertEq(shares.owedOf(alice), 597e7);
    }
}
