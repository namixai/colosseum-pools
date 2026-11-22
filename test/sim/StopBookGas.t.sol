// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBookBase} from "./StopBook.t.sol";

/// The gas of the stop book against a small HyperEVM block (2 000 000), each with a quarter of it to spare: at most
/// 1 500 000 for a transaction, 21 000 of it for the transaction itself and its data. Storage is read cold, as a block's
/// first transaction reads it. The oracle here is the simulator's, dearer than the chain's precompile.
///
/// A month of one-second blocks is 2 592 000 checks; the journal is moved on that far without logging them
/// (`StopBookHarness.skip`), and the touches a test wants are written into it as checks would have (`plant`): a search
/// reads as many words whatever the prices it skips hold.
contract StopBookGasTest is StopBookBase {
    uint256 constant LIMIT = 1_500_000;
    uint256 constant TX = 25_000; // the transaction and its data
    uint256 constant MONTH = 30 days;

    uint32[16] internal S;

    function setUp() public override {
        super.setUp();
        for (uint256 k = 0; k < 16; ++k) {
            S[k] = uint32(200 + k);
            _asset(S[k], 5, MARK);
        }
    }

    function _sixteen() internal view returns (uint32[] memory a) {
        a = new uint32[](16);
        for (uint256 k = 0; k < 16; ++k) {
            a[k] = S[k];
        }
    }

    /// An exam long 20 on each of the 16 assets, with a stop at 900 000 and a take at 1 100 000 on each: 32 entries.
    function _wide(uint32 stepDuration) internal returns (PaperExam e) {
        ExamTerms memory t = _terms();
        t.stepDuration = stepDuration;
        e = _examWith(_sixteen(), t, false);
        for (uint256 k = 0; k < 16; ++k) {
            _trade(e, S[k], true, 20);
        }
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, S[k], false, 900_000, 0);
            _set(e, S[k], true, 1_100_000, 0);
        }
    }

    function _cool(address e) internal {
        vm.cool(address(stopBook));
        vm.cool(e);
        vm.cool(ORACLE);
        vm.cool(address(hyperCore));
    }

    function _pokeGas() internal returns (uint256 used) {
        _cool(address(0));
        uint256 g0 = gasleft();
        stopBook.poke();
        used = g0 - gasleft();
    }

    function _settleGas(PaperExam e) internal returns (uint256 used, uint256 left) {
        _cool(address(e));
        uint256 g0 = gasleft();
        left = stopBook.settle(address(e));
        used = g0 - gasleft();
    }

    /// Settles `e` in calls of a small block's gas, each in a block of its own, until nothing is owed: how many.
    function _settleInSmallBlocks(PaperExam e) internal returns (uint256 calls) {
        uint256 left = 1;
        while (left != 0) {
            ++calls;
            assertLe(calls, 4, "a few blocks at most");
            if (calls > 1) {
                vm.roll(block.number + 1);
                stopBook.poke();
            }
            _cool(address(e));
            uint256 g0 = gasleft();
            left = stopBook.settle{gas: LIMIT - TX}(address(e));
            emit log_named_uint("  one call in a small block", g0 - gasleft());
            emit log_named_uint("  entries still owed", left);
        }
    }

    /// The next position at or after `from` whose check closes a run of 4^11 positions of `asset`: eleven levels.
    function _deepest(uint32 asset, uint256 from) internal view returns (uint256 g) {
        uint256 span = 1 << 22;
        uint256 off = _harness().off(asset);
        g = ((from + off + 1 + span - 1) / span) * span - 1 - off;
    }

    // ── a check ──────────────────────────────────────────────────

    /// A check of 16 assets, block after block, and in the check where each asset closes runs on eleven levels at once
    /// (written into empty words here, dearer than on a journal that has them).
    function test_gas_poke_sixteenAssets() public {
        _wide(7 days);
        uint256 most;
        uint256 sum;
        for (uint256 b = 0; b < 64; ++b) {
            vm.roll(block.number + 1);
            for (uint256 k = 0; k < 16; ++k) {
                CoreSimulatorLib.setMarkPx(S[k], uint64(MARK - 5_000 + ((b * 7 + k * 3) % 11) * 1_000));
            }
            uint256 used = _pokeGas();
            sum += used;
            if (used > most) most = used;
        }
        emit log_named_uint("poke, 16 assets, average of 64 checks", sum / 64);
        emit log_named_uint("poke, 16 assets, most of 64 checks", most);
        uint256 deep;
        for (uint256 k = 0; k < 16; ++k) {
            uint256 g = _deepest(S[k], stopBook.logged());
            _harness().skip(g - stopBook.logged());
            vm.roll(block.number + 1);
            uint256 used = _pokeGas();
            if (used > deep) deep = used;
        }
        emit log_named_uint("poke, 16 assets, the check closing eleven levels of an asset (most)", deep);
        assertLe(most + TX, LIMIT);
        assertLe(deep + TX, LIMIT);
    }

    /// A check of one asset: what logging its price costs a block (for the cost of the journal a day).
    function test_gas_poke_oneAsset() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        uint256 sum;
        uint256 most;
        for (uint256 b = 0; b < 256; ++b) {
            vm.roll(block.number + 1);
            _mark(uint64(MARK - 3_000 + (b * 7 % 13) * 500));
            uint256 used = _pokeGas();
            sum += used;
            if (used > most) most = used;
        }
        emit log_named_uint("poke, one asset, average of 256 checks", sum / 256);
        emit log_named_uint("poke, one asset, most of 256 checks", most);
        vm.roll(block.number + 1);
        stopBook.poke();
        uint256 warmSecond = gasleft();
        stopBook.poke(); // a second call in the block
        emit log_named_uint("poke, a second call in the block", warmSecond - gasleft());
    }

    // ── a settle of 32 entries armed a month ago ────────────────

    /// 32 entries on 16 assets, armed, then a month of checks and no settle: the exam, a stranger or the executor
    /// settles it in one call.
    function _monthOld() internal returns (PaperExam e, uint256 p0, uint256 t0) {
        e = _wide(0);
        vm.roll(block.number + DELAY);
        stopBook.poke(); // the first position of the entries' search
        p0 = stopBook.logged() - 1;
        t0 = block.timestamp;
        _harness().skip(MONTH);
        vm.roll(block.number + MONTH);
        vm.warp(block.timestamp + MONTH);
    }

    function test_gas_settle_32Entries_aMonthOld_noneTouched() public {
        (PaperExam e,,) = _monthOld();
        stopBook.poke();
        (uint256 used, uint256 left) = _settleGas(e);
        emit log_named_uint("settle, 32 entries on 16 assets, a month old, none touched", used);
        assertEq(left, 0);
        assertLe(used + TX, LIMIT);
    }

    /// Each asset's stop touched a few blocks before the end of the month, its take never: every search climbs the
    /// whole month and goes down it, and sixteen fills close sixteen positions.
    function test_gas_settle_32Entries_aMonthOld_everyStopTouchedAtTheEnd() public {
        (PaperExam e, uint256 p0, uint256 t0) = _monthOld();
        for (uint256 k = 0; k < 16; ++k) {
            uint256 pos = p0 + MONTH - 7 - k;
            _harness().plant(S[k], pos, 899_000, block.number - (p0 + MONTH - pos), t0 + (pos - p0));
        }
        stopBook.poke();
        uint256 snap = vm.snapshotState();
        (uint256 used, uint256 left) = _settleGas(e);
        emit log_named_uint("settle, 32 entries, a month old, every stop touched at its end, one call", used);
        assertEq(left, 0);
        vm.revertToState(snap);
        uint256 calls = _settleInSmallBlocks(e);
        emit log_named_uint("settle, the same in small blocks: calls", calls);
        for (uint256 k = 0; k < 16; ++k) {
            assertEq(e.positionOf(S[k]), 0);
        }
        assertEq(e.cash(), int256(100e6) - 16 * 20 * 1_000_000 + 16 * 20 * 899_000, "every stop at its touch");
    }

    /// The touches spread over the month, a stop or a take on each asset.
    function test_gas_settle_32Entries_aMonthOld_touchesAcrossTheMonth() public {
        (PaperExam e, uint256 p0, uint256 t0) = _monthOld();
        uint256 seed = 7;
        for (uint256 k = 0; k < 16; ++k) {
            seed = uint256(keccak256(abi.encode(seed)));
            uint256 pos = p0 + 1 + seed % (MONTH - 1);
            bool take = (seed >> 128) % 2 == 0;
            _harness().plant(S[k], pos, take ? 1_101_000 : 899_000, block.number - (p0 + MONTH - pos), t0 + (pos - p0));
        }
        stopBook.poke();
        uint256 snap = vm.snapshotState();
        (uint256 used, uint256 left) = _settleGas(e);
        emit log_named_uint("settle, 32 entries, a month old, touches across the month, one call", used);
        assertEq(left, 0);
        int256 cash = e.cash();
        vm.revertToState(snap);
        uint256 calls = _settleInSmallBlocks(e);
        emit log_named_uint("settle, the same in small blocks: calls", calls);
        assertEq(e.cash(), cash, "the same fills");
    }

    // ── a trader's first call in a block ────────────────────────

    /// The trader of the 32-entry exam, with 16 assets watched, places an order in a new block: the check of the block,
    /// the settle of its 32 entries since the last block, and the order.
    function test_gas_firstTraderCall_sixteenAssets() public {
        PaperExam e = _wide(7 days);
        vm.roll(block.number + DELAY);
        stopBook.settle(address(e));
        vm.roll(block.number + 1);
        _cool(address(e));
        vm.prank(trader);
        uint256 g0 = gasleft();
        e.place(S[0], true, 1);
        uint256 quiet = g0 - gasleft();
        emit log_named_uint("first trader call, 16 assets, 32 entries, nothing touched", quiet);

        // The next block a stop is touched: the trader's call carries it out first.
        vm.roll(block.number + DELAY + WINDOW);
        CoreSimulatorLib.setMarkPx(S[1], 890_000);
        _cool(address(e));
        vm.prank(trader);
        g0 = gasleft();
        e.place(S[2], true, 1);
        uint256 touched = g0 - gasleft();
        emit log_named_uint("first trader call, 16 assets, 32 entries, one stop touched in the block", touched);
        assertEq(e.positionOf(S[1]), 0);

        // The block whose check closes eleven levels of an asset.
        uint256 g = _deepest(S[3], stopBook.logged());
        _harness().skip(g - 1 - stopBook.logged());
        vm.roll(block.number + WINDOW + 1);
        stopBook.settle(address(e)); // the journal moved on: the exam is settled up to the block before
        vm.roll(block.number + 1);
        _cool(address(e));
        vm.prank(trader);
        g0 = gasleft();
        e.place(S[4], true, 1);
        uint256 deep = g0 - gasleft();
        emit log_named_uint("first trader call, 16 assets, 32 entries, the check closing eleven levels", deep);
        assertLe(quiet + TX, LIMIT);
        assertLe(touched + TX, LIMIT);
        assertLe(deep + TX, LIMIT);
    }
}
