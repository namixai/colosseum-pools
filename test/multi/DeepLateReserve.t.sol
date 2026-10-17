// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {PoolShares} from "../../src/multi/PoolShares.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The deep audit's F6 (16 October 2026), turned round: shares asked out always reserve something
/// (rounded up), so the pool's rule for money reserved to leave answers for them, and a late renew
/// pays a round the pool can pay instead of stopping the pool. On f8bc60aa a one-share exit below
/// par reserved nothing, and a late renew stopped a solvent pool. The stand is the audit's
/// (LateReserveZero), its helpers copied so that the shares suite does not run twice.
contract DeepLateReserveTest is PoolHarness {
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

    function _stand() internal {
        vm.warp(1_790_000_000);
        shares = new PoolShares(creator, usdc, 1e8, 0);
        m = new MultiPool(address(shares), factory, _examImpl(), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        shares.bind(m, TERMS);
        shares.launch();
        CoreSimulatorLib.forceAccountActivation(alice);
        CoreSimulatorLib.forceAccountActivation(bob);
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

    /// Alice's 60 and bob's 40 in the pool: 100e8 shares, one per unit.
    function _standWithMoney() internal {
        _stand();
        _deposit(alice, 60e8);
        _deposit(bob, 40e8);
        assertEq(shares.totalShares(), 100e8);
        assertEq(_spot(address(m)), 100e8);
    }

    /// Bob asks `n` shares out while the pool is worth 99 for 100 shares (price 0.99, a 1% loss).
    function _belowParExit(uint256 n) internal returns (uint64 end) {
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 99e8);
        assertEq(m.value(), 99e8);
        assertLt(m.value(), shares.totalShares(), "price below 1.0");
        vm.prank(bob);
        shares.requestExit(n);
        end = m.endsAt();
        assertEq(m.reserveFor(), end);
    }

    // ── the finding, turned round ────────────────────────────────────────────────────

    /// One share asked out below par reserves one unit; the window is missed, the price recovers,
    /// and the late renew pays the round and the pool runs on.
    function test_deep_F6_oneShareExitBelowPar_lateRenewPaysAndThePoolRunsOn() public {
        _standWithMoney();
        uint64 end = _belowParExit(1);
        assertEq(m.reserve(), 1, "rounded up, not to zero");
        vm.warp(end + 1);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 101e8);
        vm.prank(stranger);
        shares.renew();
        assertFalse(m.stopping(), "the pool runs on");
        assertEq(shares.round(), 1, "the round closed");
        assertEq(shares.exitShares(), 0);
        assertEq(m.endsAt(), end + 90 days);
    }

    /// The same exit with a larger reserve, as the audit's contrast had it: paid at today's price.
    function test_deep_F6_contrast_aLargerExitIsPaidAtTodaysPrice() public {
        _standWithMoney();
        uint64 end = _belowParExit(1e8);
        assertEq(m.reserve(), 99e6);
        vm.warp(end + 1);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 101e8);
        vm.prank(stranger);
        shares.renew();
        assertFalse(m.stopping());
        assertEq(shares.round(), 1);
        assertEq(shares.owed(), 101e6, "1.01 USDC at today's price");
    }
}
