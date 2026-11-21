// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";
import {PoolShares} from "../../src/multi/PoolShares.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// An exam a `MultiPool` sells is started without a filler (`PaperExam.initialize`, its only call of the exam's
/// start), so it has no stop book: its stops are carried out as before the book, by anyone but the trader in a block
/// whose oracle price is at them (`fillTrigger`). The stand is `DeepStopEnd.t.sol`'s.
contract MultiPoolStopsTest is PoolHarness {
    PaperExam examImpl;
    PoolShares shares;
    MultiPool m;
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address creator = makeAddr("creator");

    function _plan() internal pure returns (Plan memory p) {
        p.menu = new Size[](1);
        p.menu[0] = Size({size: 10e6, price: 1});
        p.examRules = _rules();
        uint16[] memory targets = new uint16[](1);
        targets[0] = 1000;
        p.exam = ExamTerms({capital: 0, targetsBps: targets, stepDuration: 60 days, maxIdle: 0, minTradingDays: 0,
            costPpm: 0, fillDelay: 1, fillWindow: 5});
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
        examImpl = new PaperExam();
        shares = _newShares(creator, 1e8, 0);
        m = new MultiPool(address(shares), factory, address(examImpl), _tieShares(shares, _plan()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceAccountActivation(address(shares));
        CoreSimulatorLib.forceAccountActivation(address(m));
        vm.prank(creator);
        shares.bind(m, TERMS);
        shares.launch();
        CoreSimulatorLib.forceAccountActivation(alice);
        CoreSimulatorLib.forceAccountActivation(creator);
        vm.prank(alice);
        address t = shares.openTicket(TERMS);
        CoreSimulatorLib.forceAccountActivation(t);
        CoreSimulatorLib.forceSpotBalance(t, 0, 100e8);
        address[] memory l = new address[](1);
        l[0] = t;
        shares.settle(l);
        CoreSimulatorLib.nextBlock();
    }

    function _buy(address who) internal returns (PaperExam e) {
        uint256 price = abi.decode(CodeStore.read(m.planData()), (Plan)).menu[0].price + factory.challengeFee();
        deal(address(usdc), who, price);
        vm.startPrank(who);
        usdc.approve(address(m), price);
        e = PaperExam(m.buy(0));
        vm.stopPrank();
    }

    function test_multiPoolExam_noFiller_noBook_stopsAsBefore() public {
        _stand();
        PaperExam e = _buy(bob);
        assertEq(e.filler(), address(0), "started without a filler");
        assertEq(e.stopBook(), address(0), "so without a stop book");
        uint32 asset = _rules().assets[0];
        uint64 px = uint64(PrecompileOracle.read(asset));
        vm.prank(bob);
        e.place(asset, true, 10);
        vm.roll(block.number + 1);
        vm.prank(alice);
        e.fill();
        vm.prank(bob);
        e.setTrigger(asset, false, px, 0); // at the price: reached once armed
        vm.roll(block.number + 1);
        vm.prank(bob);
        vm.expectRevert(PaperExam.OwnOrder.selector);
        e.fillTrigger(asset, false);
        vm.prank(alice);
        e.fillTrigger(asset, false);
        assertEq(e.positionOf(asset), 0);
    }
}

library PrecompileOracle {
    function read(uint32 asset) internal view returns (uint256) {
        (bool ok, bytes memory r) = address(0x0000000000000000000000000000000000000807).staticcall(abi.encode(asset));
        require(ok && r.length >= 32);
        return abi.decode(r, (uint256));
    }
}
