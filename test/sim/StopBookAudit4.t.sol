// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {PaperExam} from "../../src/exam/PaperExam.sol";
import {StopBookBase} from "./StopBook.t.sol";

/// The scenarios of the fourth independent audit of cp#335 (21 November 2026, head debe1528), on the journal of prices.
/// Its models are `StopBookModel.t.sol` (`testFuzz_model_audit4*`); L1 is `test_I4_*`, L2 `test_settle_stoppedShort_*`
/// and the gas of `StopBookGas.t.sol`, L4 `test_I8_aFillThatNeedsMoreGas_*`. L3 (the gas a check kept after a failed
/// fill) and the latch queue that grew without end are gone with the check that carried fills out.
contract StopBookAudit4Test is StopBookBase {
    uint32[16] internal S;

    function setUp() public override {
        super.setUp();
        for (uint256 k = 0; k < 16; ++k) {
            S[k] = uint32(300 + k);
            _asset(S[k], 5, MARK);
        }
    }

    /// `n` exams long 20 on each of 16 assets, a stop at 990 000 and a take at 1 300 000 on each.
    function _wide(uint256 n) internal returns (PaperExam[] memory es) {
        uint32[] memory as_ = new uint32[](16);
        for (uint256 k = 0; k < 16; ++k) {
            as_[k] = S[k];
        }
        es = new PaperExam[](n);
        for (uint256 i = 0; i < n; ++i) {
            es[i] = _examOn(as_, false);
            for (uint256 k = 0; k < 16; ++k) {
                _trade(es[i], S[k], true, 20);
            }
            for (uint256 k = 0; k < 16; ++k) {
                _set(es[i], S[k], false, uint64(990_000 - i), 0);
                _set(es[i], S[k], true, uint64(1_300_000 + i), 0);
            }
        }
    }

    /// H1: eight wide exams touched by one move (256 entries on 16 assets) cost a check nothing more, and an exam with no
    /// trigger at all acts in a small block in that block and the next ones: on debe1528 it was refused in 70 of 70.
    function test_a4_H1_aWideCrowdTouched_anotherExamActsInASmallBlock() public {
        PaperExam[] memory es = _wide(8);
        PaperExam other = _exam();
        vm.roll(block.number + DELAY);
        stopBook.poke();
        for (uint256 b = 0; b < 5; ++b) {
            vm.roll(block.number + 1);
            for (uint256 k = 0; k < 16; ++k) {
                CoreSimulatorLib.setMarkPx(S[k], uint64(980_000 - b * 1_000)); // every stop touched
            }
            vm.cool(address(stopBook));
            uint256 g0 = gasleft();
            stopBook.poke();
            uint256 used = g0 - gasleft();
            assertLe(used + 25_000, 1_500_000, "the check: its assets, not its entries");
            vm.prank(trader);
            other.setAgent{gas: 2_000_000}(stranger);
        }
        for (uint256 i = 0; i < es.length; ++i) {
            assertEq(stopBook.settle(address(es[i])), 0);
            assertEq(es[i].positionOf(S[0]), 0);
        }
    }

    /// H2 and M1: a crowd touched, and in the blocks of the touch more triggers set by other exams, many with the same
    /// `armedAt`: on debe1528 their parking and arming held exams hundreds to thousands of blocks. Here every exam is
    /// settled in one call right away, at its own touch; the journal grows a position a block whatever they do.
    function test_a4_H2M1_aCrowdAndTriggersSetMeanwhile_holdNobody() public {
        PaperExam victim = _exam();
        _trade(victim, BTC, true, 100);
        _set(victim, BTC, false, 950_000, 0);
        PaperExam[] memory crowd = new PaperExam[](60);
        for (uint256 i = 0; i < 60; ++i) {
            crowd[i] = _exam();
            _trade(crowd[i], BTC, true, 10);
            _set(crowd[i], BTC, false, uint64(960_000 + i * 100), 0);
        }
        vm.roll(block.number + DELAY);
        uint256 len = stopBook.logged();
        for (uint256 b = 0; b < 30; ++b) {
            vm.roll(block.number + 1);
            _mark(uint64(955_000 - b * 100)); // the crowd touched, not yet the victim
            stopBook.poke();
            // Each block, more of the crowd moves a take in: all armed in the same later block.
            for (uint256 i = 0; i < 2; ++i) {
                PaperExam c = crowd[(b * 2 + i) % 60];
                if (c.positionOf(BTC) == 0) continue;
                vm.prank(trader);
                try c.setTrigger(BTC, true, 1_200_000, 0) {} catch {}
            }
        }
        vm.roll(block.number + 1);
        _mark(940_000); // the victim's touch
        stopBook.poke();
        uint256 touch = block.number;
        assertEq(stopBook.logged(), len + 31, "a position a block");
        vm.roll(touch + 1);
        assertEq(stopBook.settle(address(victim)), 0, "settled in one call, held by nobody");
        assertEq(victim.cash(), 94e6, "at its touch");
        for (uint256 i = 0; i < 60; ++i) {
            assertEq(stopBook.settle(address(crowd[i])), 0);
            assertEq(crowd[i].positionOf(BTC), 0);
        }
    }

    /// M1: 240 entries of other exams armed in one block, then the victim's stop, touched in the block it arms: the
    /// victim's own call settles it at once; on debe1528 the arming queue held such a victim up to 88 blocks.
    function test_a4_M1_manyArmingTogether_theVictimActsAtOnce() public {
        for (uint256 i = 0; i < 120; ++i) {
            PaperExam o = _exam();
            _trade(o, ETH, true, 40);
        }
        vm.roll(block.number + 1);
        for (uint256 i = 0; i < 120; ++i) {
            PaperExam o = made[i];
            _set(o, ETH, false, uint64(200_000 - i), 0);
            _set(o, ETH, true, uint64(300_000 + i), 0);
        }
        PaperExam v = _exam();
        _trade(v, BTC, true, 100);
        _set(v, BTC, false, 950_000, 0);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        vm.prank(trader);
        v.place(BTC, true, 10);
        assertEq(v.positionOf(BTC), 0, "the stop first, in the block it armed and was touched");
        assertEq(v.cash(), 94e6);
    }
}
