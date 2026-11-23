// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PaperExam} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {StopBookBase, Garbage} from "./StopBook.t.sol";

/// The review of the stop book on its way in (23 November 2026): a settle that stops short never answers zero, the
/// changes in force are put in place with gas of their own so that they never take a fill back, and a removal asked of
/// a stop the price has touched reverts with the fill, which the next settle carries out at the same price.
contract StopBookThreadsTest is StopBookBase {
    uint256 constant MONTH = 30 days;

    /// A settle that stops short of its end never answers "settled" (zero): here the gas runs out after the one live
    /// entry is searched, before a key whose entry is gone, while that entry's removal is in force and not yet put
    /// in place. Any gas: an answer of zero means the exam searched to the end and its removal applied.
    function test_t1_aSettleThatStopsShortNeverAnswersZero() public {
        // The first asset's stop removed (a change, two searches), six live stops after it, the last key's entry gone.
        uint32[] memory as_ = new uint32[](8);
        for (uint256 k = 0; k < 8; ++k) {
            as_[k] = uint32(600 + k);
            _asset(as_[k], 5, MARK);
        }
        PaperExam e = _examWith(as_, _terms(), false);
        for (uint256 k = 0; k < 8; ++k) {
            _trade(e, as_[k], true, 20);
            _set(e, as_[k], false, 900_000, 0);
        }
        _trade(e, as_[7], false, 20); // closed by an order: the entry gone, its key listed last
        vm.roll(block.number + DELAY + 1);
        stopBook.poke();
        vm.prank(trader);
        e.clearTrigger(as_[0], false); // a removal, in force DELAY blocks on
        for (uint256 b = 0; b < DELAY + 1; ++b) {
            vm.roll(block.number + 1);
            stopBook.poke();
        }
        _harness().skip(MONTH); // a month of checks: searches as dear as on the chain
        vm.roll(block.number + MONTH);
        vm.warp(block.timestamp + MONTH);
        stopBook.poke();
        uint256 zeros;
        for (uint256 g = 100_000; g < 700_000; g += 1_000) {
            uint256 snap = vm.snapshotState();
            _cold(address(e));
            try stopBook.settle{gas: g}(address(e)) returns (uint256 left) {
                if (left == 0) {
                    ++zeros;
                    assertEq(stopBook.checkedTo(address(e)), stopBook.logged(), "zero: searched to the end");
                    assertEq(stopBook.entryOf(address(e), as_[0], false).level, 0, "zero: the removal in force applied");
                }
            } catch {}
            vm.revertToState(snap);
        }
        assertGt(zeros, 0, "with the gas, settled");
    }

    /// Sixteen assets, a stop and a take on each, all thirty-two removed (in force `DELAY` blocks on), one stop touched
    /// before its removal is: one settle carries the fill out and takes the other thirty out, a heavy pass. With any
    /// gas the settle returns, or reverts as an empty call (`NoProgress`): never runs out of gas taking back the fill.
    function test_t2_theChangesInForceNeverTakeAFillBack() public {
        uint32[] memory as_ = new uint32[](16);
        for (uint256 k = 0; k < 16; ++k) {
            as_[k] = uint32(500 + k);
            _asset(as_[k], 5, MARK);
        }
        PaperExam e = _examWith(as_, _terms(), false);
        for (uint256 k = 0; k < 16; ++k) {
            _trade(e, as_[k], true, 20);
        }
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, as_[k], false, 900_000, 0);
            _set(e, as_[k], true, 1_100_000, 0);
        }
        vm.roll(block.number + DELAY + 1);
        stopBook.poke();
        for (uint256 k = 0; k < 16; ++k) {
            vm.startPrank(trader);
            e.clearTrigger(as_[k], false);
            e.clearTrigger(as_[k], true);
            vm.stopPrank();
        }
        vm.roll(block.number + 1);
        _markOf(as_[0], 850_000); // the stop on the first asset touched before its removal is in force
        stopBook.poke();
        vm.roll(block.number + DELAY + 1);
        _markOf(as_[0], MARK);
        stopBook.poke();
        uint256 settled;
        for (uint256 g = 300_000; g < 3_000_000; g += 5_000) {
            uint256 snap = vm.snapshotState();
            _cold(address(e));
            try stopBook.settle{gas: g}(address(e)) returns (uint256 left) {
                if (left == 0) {
                    ++settled;
                    assertEq(e.positionOf(as_[0]), 0, "the touched stop filled");
                    assertEq(stopBook.watched().length, 0, "the removals put in place");
                }
            } catch (bytes memory err) {
                assertEq(err.length >= 4 ? bytes4(err) : bytes4(0), StopBook.NoProgress.selector, "only an empty call");
            }
            vm.revertToState(snap);
        }
        assertGt(settled, 0);
    }

    /// A removal asked of a stop the price has touched: the trader's call settles first and fills the stop, then finds
    /// no stop to remove and reverts (`NoTrigger`), taking the fill back with it. The fill is not lost: the next settle,
    /// anyone's, carries it out at the price of its touch.
    function test_t3_aRemovalOfATouchedStop_revertsWithTheFill_theNextSettleCarriesItOut() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(940_000);
        stopBook.poke();
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.clearTrigger(BTC, false);
        assertEq(e.positionOf(BTC), 100, "the fill went back with the call");
        vm.roll(block.number + 3);
        _mark(1_000_000);
        vm.prank(stranger);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6, "at the price of its touch, 940 000");
    }

    /// An order at the book against a book that answers an ask past 64 bits fills at the oracle, as one that does not
    /// answer (the order's own read of the book, `PaperExam._liveGap`; the stop's is the journal's, `StopBook._bbo`).
    function test_t4_aGarbageBook_anOrderFillsAtTheOracle() public {
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        PaperExam e = _examOn(one, true);
        vm.etch(BBO, type(Garbage).runtimeCode);
        _trade(e, BTC, true, 100);
        assertEq(e.positionOf(BTC), 100);
        assertEq(e.cash(), int256(100e6) - 100 * int256(uint256(MARK)), "at the oracle");
    }

    /// Storage as cold as a transaction of its own finds it.
    function _cold(address e) internal {
        vm.cool(address(stopBook));
        vm.cool(e);
        vm.cool(ORACLE);
        vm.cool(address(hyperCore));
    }

    function _markOf(uint32 a, uint64 px) internal {
        CoreSimulatorLib.setMarkPx(a, px);
    }
}
