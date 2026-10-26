// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {PoolShares} from "../../src/multi/PoolShares.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The cloud audit's creator: a contract that runs the whole drain in one transaction with USDC on
/// HyperEVM only (F2_CreatorRefundDrain).
contract CreatorBot {
    function bind(PoolShares s, MultiPool m, bytes32 terms) external {
        s.bind(m, terms);
    }

    function drain(PoolShares s, IERC20 usdc, address[] calldata holders, uint256 inflate6, uint256 seed6) external {
        usdc.transfer(address(s), inflate6);
        for (uint256 i = 0; i < holders.length; ++i) s.refund(holders[i]);
        usdc.transfer(address(s), seed6);
        s.settle(new address[](0));
        s.refund(address(this));
        s.pay(address(this));
    }
}

/// The deep audit's F2 (cloud, 16 October 2026), turned round: once a raise that fell short has
/// taken its price, no share is made, and what reaches the contract after that is the creator's
/// one for one. On f8bc60aa and 5eb75719 the creator raised the price before taking it, had the
/// contract seed them shares after it, and was refunded those at the raised price, out of the
/// holders' money. The stand is the audit's (F2_CreatorRefundDrain), its helpers copied so that the
/// shares suite does not run twice.
contract DeepRefundTest is PoolHarness {
    PaperExam examImpl;
    PoolShares shares;
    MultiPool m;
    address creator = makeAddr("creator");
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

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

    function _withPlatformFee() internal {
        vm.prank(operator);
        factory.setChallengeFee(0, operator);
        CoreSimulatorLib.forceAccountActivation(operator);
    }

    function _standRaise(address who, uint256 target) internal {
        vm.warp(1_790_000_000);
        shares = _newShares(who, 1e8, target);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        if (who.code.length == 0) {
            vm.prank(who);
            shares.bind(m, TERMS);
        } else {
            CreatorBot(who).bind(shares, m, TERMS);
        }
        CoreSimulatorLib.forceAccountActivation(alice);
        CoreSimulatorLib.forceAccountActivation(bob);
        CoreSimulatorLib.forceAccountActivation(who);
    }

    function _deposit(address who, uint64 amount) internal {
        vm.prank(who);
        address t = shares.openTicket(TERMS);
        CoreSimulatorLib.forceAccountActivation(t);
        CoreSimulatorLib.forceSpotBalance(t, 0, amount);
        address[] memory l = new address[](1);
        l[0] = t;
        shares.settle(l);
        CoreSimulatorLib.nextBlock();
    }

    /// Holders put in 60 + 40 against a target of 1000, and the thirty days pass.
    function _failedRaiseWith100Deposited(address who) internal {
        _standRaise(who, 1000e8);
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
        shares.settle(new address[](0));
        assertEq(shares.totalShares(), 100e8);
        vm.warp(shares.raiseEnds() + 1);
    }

    function _coreSendTo(address to, uint64 amount) internal {
        CoreSimulatorLib.forceSpotBalance(to, 0, _spot(to) + amount);
        CoreSimulatorLib.nextBlock();
    }

    function _holdersPaidInFull(uint256 alice8, uint256 bob8) internal {
        vm.warp(block.timestamp + 2 minutes);
        shares.pay(alice);
        shares.pay(bob);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(alice), alice8, "alice is paid what she is owed");
        assertEq(_spot(bob), bob8, "bob is paid what he is owed");
        assertEq(shares.owedOf(alice) + shares.owedOf(bob), 0);
    }

    /// The audit's scenario: 50 before the price, every holder refunded, 300 after it. The 300 make
    /// no shares; the creator is owed them back, less 0.5 %, and the claims never exceed what is held.
    function test_deep_F2_noShareIsMadeAfterTheRefundsPrice_soTheCreatorTakesOnlyWhatTheyPutInAfter() public {
        _withPlatformFee();
        _failedRaiseWith100Deposited(creator);
        _coreSendTo(address(shares), 50e8);
        vm.startPrank(creator);
        shares.refund(alice);
        shares.refund(bob);
        vm.stopPrank();
        assertEq(shares.refundValue(), 150e8, "the 50 sent before the price are the holders'");
        _coreSendTo(address(shares), 300e8);
        shares.settle(new address[](0));
        assertEq(shares.sharesOf(creator), 0, "no share after the price");
        assertEq(shares.totalShares(), 0);
        assertEq(shares.owedOf(creator), 300e8 - 150e6, "the 300 are owed back one for one, less 0.5 %");
        assertEq(shares.owed(), 450e8);
        assertEq(shares.owed(), _spot(address(shares)), "the claims are what is held");
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotFree.selector, 0));
        shares.refund(creator);
        shares.pay(creator);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(creator), 300e8 - 150e6, "the creator: their 300 less 0.5 %, no more");
        _holdersPaidInFull(90e8 - 45e6, 60e8 - 30e6);
        vm.warp(block.timestamp + 2 minutes);
        shares.payFee();
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(shares)), 0, "everything held was owed, and is paid");
    }

    /// The audit's one-transaction form, with USDC on HyperEVM only: the creator has no shares to be
    /// refunded at the raised price, so the drain does not go through.
    function test_deep_F2_theOneTransactionDrainDoesNotGoThrough() public {
        _withPlatformFee();
        CreatorBot bot = new CreatorBot();
        _failedRaiseWith100Deposited(address(bot));
        deal(address(usdc), address(bot), 350e6);
        address[] memory holders = new address[](2);
        holders[0] = alice;
        holders[1] = bob;
        vm.expectRevert(abi.encodeWithSelector(PoolShares.NotFree.selector, 0));
        bot.drain(shares, IERC20(address(usdc)), holders, 50e6, 300e6);
        assertEq(usdc.balanceOf(address(bot)), 350e6, "nothing moved");
        shares.refund(alice);
        shares.refund(bob);
        _holdersPaidInFull(60e8 - 30e6, 40e8 - 20e6);
    }

    /// Control, as the audit had it: an honest raise that fell short refunds every holder in full,
    /// less 0.5 %.
    function test_deep_F2_control_anHonestFailedRaiseRefundsInFull() public {
        _withPlatformFee();
        _failedRaiseWith100Deposited(creator);
        shares.refund(alice);
        shares.refund(bob);
        assertEq(shares.owed(), 100e8);
        _holdersPaidInFull(60e8 - 30e6, 40e8 - 20e6);
    }

    /// Money sent before the price was taken, with no holder: the creator's shares, refunded at the
    /// one price like anyone's.
    function test_deep_F2_control_beforeThePriceTheCreatorsMoneyIsSharesAtTheOnePrice() public {
        _standRaise(creator, 1000e8);
        _coreSendTo(address(shares), 30e8);
        shares.settle(new address[](0));
        assertEq(shares.sharesOf(creator), 30e8);
        _deposit(alice, 60e8);
        vm.warp(shares.raiseEnds() + 1);
        shares.refund(creator);
        shares.refund(alice);
        assertEq(shares.owedOf(creator), 30e8);
        assertEq(shares.owedOf(alice), 60e8);
    }
}
