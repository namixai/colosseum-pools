// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {StopBookBase, StopBookHarness} from "./StopBook.t.sol";

/// Worst cases for the two-phase search: 32 entries on 16 assets, every one with a change in force, a month of journal,
/// touches spread over the month, taken in small blocks (1 500 000 for a call).
contract StopBookAudit7GasTest is StopBookBase {
    uint256 constant LIMIT = 2_000_000;
    uint256 constant TX = 25_000;
    uint256 constant MONTH = 30 days;
    uint32[16] internal S;

    function setUp() public override {
        super.setUp();
        for (uint256 k = 0; k < 16; ++k) {
            S[k] = uint32(200 + k);
            _asset(S[k], 5, MARK);
        }
    }

    function _wide() internal returns (PaperExam e) {
        uint32[] memory a = new uint32[](16);
        for (uint256 k = 0; k < 16; ++k) {
            a[k] = S[k];
        }
        ExamTerms memory t = _terms();
        t.stepDuration = 0;
        e = _examWith(a, t, false);
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

    function _smallBlocks(PaperExam e, uint256 gasPerCall) internal returns (uint256 calls, uint256 worst) {
        uint256 left = 1;
        while (left != 0) {
            ++calls;
            require(calls < 400, "too many calls");
            if (calls > 1) {
                vm.roll(block.number + 1);
                stopBook.poke();
            }
            _cool(address(e));
            uint256 g0 = gasleft();
            left = stopBook.settle{gas: gasPerCall}(address(e));
            uint256 used = g0 - gasleft();
            if (used > worst) worst = used;
        }
    }

    /// Every entry moved at the start of the month; stops and takes touched at the end of it under the new levels, some
    /// right after the change, some in the middle; settled in calls of a small block.
    function test_a7_gas_allMoved_touchedAcrossMonth() public {
        PaperExam e = _wide();
        vm.roll(block.number + 2);
        stopBook.poke();
        uint256 p0 = stopBook.logged() - 1;
        uint256 t0 = block.timestamp;
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, S[k], false, 890_000, 0);
            _set(e, S[k], true, 1_110_000, 0);
        }
        vm.roll(block.number + 2);
        stopBook.poke();
        _harness().skip(MONTH);
        vm.roll(block.number + MONTH);
        vm.warp(block.timestamp + MONTH);
        uint256 seed = 11;
        for (uint256 k = 0; k < 16; ++k) {
            seed = uint256(keccak256(abi.encode(seed)));
            uint256 pos = p0 + 5 + seed % (MONTH - 6);
            bool take = (seed >> 128) % 2 == 0;
            _harness().plant(S[k], pos, take ? 1_120_000 : 880_000, block.number - (p0 + MONTH - pos), t0 + (pos - p0));
        }
        stopBook.poke();
        uint256 snap = vm.snapshotState();
        (uint256 calls, uint256 worst) = _smallBlocks(e, 1_500_000 - TX);
        emit log_named_uint("calls of 1 475 000 gas", calls);
        emit log_named_uint("worst call", worst);
        assertLe(worst + TX, LIMIT);
        int256 cash = e.cash();
        vm.revertToState(snap);
        // the same in one call of everything the block has
        _cool(address(e));
        uint256 g0 = gasleft();
        uint256 left = stopBook.settle{gas: 30_000_000}(address(e));
        emit log_named_uint("one call: gas", g0 - gasleft());
        assertEq(left, 0);
        assertEq(e.cash(), cash, "partial settles == one settle");
    }

    /// Worst for the doubled reserve: a call with exactly the gas a search needs starting, many times over: no call
    /// may revert and the result is the same as one call.
    function testFuzz_a7_gas_anyGasSameResult(uint256 gasPerCall) public {
        gasPerCall = bound(gasPerCall, 800_000, 1_500_000); // below ~800 000 (cold storage) a 32-entry exam with changes makes no progress: test_a7_gas_scanLowGas
        PaperExam e = _wide();
        vm.roll(block.number + 2);
        stopBook.poke();
        uint256 p0 = stopBook.logged() - 1;
        uint256 t0 = block.timestamp;
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, S[k], false, 890_000, 0);
        }
        vm.roll(block.number + 2);
        stopBook.poke();
        _harness().skip(2000);
        vm.roll(block.number + 2000);
        vm.warp(block.timestamp + 2000);
        for (uint256 k = 0; k < 16; ++k) {
            uint256 pos = p0 + 30 + 111 * k;
            _harness().plant(S[k], pos, 880_000, block.number - (p0 + 2000 - pos), t0 + (pos - p0));
        }
        stopBook.poke();
        uint256 snap = vm.snapshotState();
        uint256 left = stopBook.settle{gas: 30_000_000}(address(e));
        assertEq(left, 0);
        int256 cash = e.cash();
        vm.revertToState(snap);
        (uint256 calls,) = _smallBlocks(e, gasPerCall);
        calls;
        assertEq(e.cash(), cash, "partial settles == one settle");
    }

    function _scenario() internal returns (PaperExam e) {
        e = _wide();
        vm.roll(block.number + 2);
        stopBook.poke();
        uint256 p0 = stopBook.logged() - 1;
        uint256 t0 = block.timestamp;
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, S[k], false, 890_000, 0);
        }
        vm.roll(block.number + 2);
        stopBook.poke();
        _harness().skip(2000);
        vm.roll(block.number + 2000);
        vm.warp(block.timestamp + 2000);
        for (uint256 k = 0; k < 16; ++k) {
            uint256 pos = p0 + 30 + 111 * k;
            _harness().plant(S[k], pos, 880_000, block.number - (p0 + 2000 - pos), t0 + (pos - p0));
        }
        stopBook.poke();
    }

    uint256 empty;

    function test_a7_gas_scanLowGas() public {
        PaperExam e = _scenario();
        uint256 snap = vm.snapshotState();
        for (uint256 g = 600_000; g <= 1_300_000; g += 50_000) {
            uint256 left = 1;
            uint256 calls;
            uint256 prev = type(uint256).max;
            while (left != 0 && calls < 60) {
                ++calls;
                vm.roll(block.number + 1);
                stopBook.poke();
                _cool(address(e));
                // Under the rule of the audit's L2: a call either does a step or reverts `NoProgress`, never an
                // empty call that returns.
                try stopBook.settle{gas: g}(address(e)) returns (uint256 l) {
                    left = l;
                } catch (bytes memory err) {
                    assertEq(bytes4(err), StopBook.NoProgress.selector, "only an empty call reverts");
                    ++empty;
                }
                prev = left;
            }
            emit log_named_uint("gas per call", g);
            emit log_named_uint("  calls", calls);
            emit log_named_uint("  left", left);
            emit log_named_uint("  empty calls refused", empty);
            if (g >= 1_000_000) assertEq(left, 0, "settled within the calls");
            assertEq(empty, 0, "every call with this gas does a step");
            empty = 0;
            vm.revertToState(snap);
            snap = vm.snapshotState();
        }
    }

    /// Stops set first, takes a day later (different starts: the staggered pair), a month of journal, some touches, some
    /// changes. Settle sent with every gas from 280 000 to 900 000 in steps: it never reverts (a revert wastes the call: the
    /// reserve for the book's own records would not hold).
    function _staggered() internal returns (PaperExam e) {
        uint32[] memory a = new uint32[](16);
        for (uint256 k = 0; k < 16; ++k) {
            a[k] = S[k];
        }
        ExamTerms memory t = _terms();
        t.stepDuration = 0;
        e = _examWith(a, t, false);
        for (uint256 k = 0; k < 16; ++k) {
            _trade(e, S[k], true, 20);
        }
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, S[k], false, 900_000, 0);
        }
        vm.roll(block.number + 2);
        stopBook.poke();
        uint256 p0 = stopBook.logged() - 1;
        uint256 t0 = block.timestamp;
        _harness().skip(86_400);
        vm.roll(block.number + 86_400);
        vm.warp(block.timestamp + 86_400);
        stopBook.poke();
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, S[k], true, 1_100_000, 0);
            if (k % 3 == 0) _set(e, S[k], false, 890_000, 0);
        }
        vm.roll(block.number + 2);
        stopBook.poke();
        _harness().skip(MONTH);
        vm.roll(block.number + MONTH);
        vm.warp(block.timestamp + MONTH);
        uint256 seed = 5;
        for (uint256 k = 0; k < 16; ++k) {
            seed = uint256(keccak256(abi.encode(seed)));
            if (k % 2 == 0) continue;
            uint256 pos = stopBook.logged() - 1 - seed % (MONTH - 10);
            _harness().plant(S[k], pos, k % 4 == 1 ? 880_000 : 1_120_000, block.number - (stopBook.logged() - 1 - pos), t0 + (pos - p0));
        }
        stopBook.poke();
    }

    /// L2: a settle by anyone but the exam that does nothing, entries still owed, reverts `NoProgress`; one that
    /// checks a new block has done something and returns the count; the exam's own settle returns the count however
    /// little it did, so its functions answer `StopsPending` as before.
    function test_P3_anEmptySettleReverts_theExamsOwnReturnsTheCount() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        uint256 len = stopBook.logged();
        assertEq(stopBook.settle{gas: 150_000}(address(e)), 1, "the block checked, the search short of gas");
        assertEq(stopBook.logged(), len + 1);
        vm.expectRevert(abi.encodeWithSelector(StopBook.NoProgress.selector, 1));
        stopBook.settle{gas: 150_000}(address(e)); // the block checked already: nothing done
        vm.prank(address(e));
        assertEq(stopBook.settle{gas: 150_000}(address(e)), 1, "the exam's own settle: the count");
        assertEq(stopBook.settle(address(e)), 0, "with the gas, settled");
    }

    /// A settle that carries out one fill and runs short for the next, with nothing else found, has done something:
    /// it returns, and the fill stands (it is not an empty call to refuse). A stop of 50 touched in one block and a
    /// take of 50 in the next, both found by an earlier short settle; then every gas, one call each.
    function test_P3_aSettleThatFillsOneAndRunsShort_keepsTheFill() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 50);
        _set(e, BTC, true, 1_100_000, 50);
        vm.roll(block.number + DELAY + 1);
        _mark(940_000);
        stopBook.poke();
        vm.roll(block.number + 1);
        _mark(1_200_000);
        stopBook.poke();
        bool found;
        for (uint256 g = 120_000; g < 600_000 && !found; g += 2_000) {
            try stopBook.settle{gas: g}(address(e)) returns (uint256 l) {
                found = l == 2 && e.positionOf(BTC) == 100; // both found, none carried out yet
            } catch {}
        }
        assertTrue(found, "a short settle that finds both");
        found = false;
        for (uint256 g = 120_000; g < 900_000 && !found; g += 2_000) {
            uint256 snap = vm.snapshotState();
            try stopBook.settle{gas: g}(address(e)) returns (uint256 l) {
                found = l == 1 && e.positionOf(BTC) == 50;
            } catch {}
            vm.revertToState(snap);
        }
        assertTrue(found, "one fill and short for the next: it returns and the fill stands");
    }

    function test_a7_gas_staggeredScan() public {
        PaperExam e = _staggered();
        uint256 snap = vm.snapshotState();
        uint256 g0 = gasleft();
        assertEq(stopBook.settle{gas: 30_000_000}(address(e)), 0);
        emit log_named_uint("one call, all: gas", g0 - gasleft());
        int256 cash = e.cash();
        vm.revertToState(snap);
        snap = vm.snapshotState();
        for (uint256 g = 280_000 + uint256(vm.envOr("A7G", uint256(0))); g <= 900_000; g += 41_000) {
            uint256 left = 1;
            uint256 calls;
            while (left != 0 && calls < 30) {
                ++calls;
                vm.roll(block.number + 1);
                stopBook.poke();
                try stopBook.settle{gas: g}(address(e)) returns (uint256 l) {
                    left = l;
                } catch (bytes memory why) {
                    emit log_named_uint("REVERT at gas", g);
                    emit log_named_uint("  call number", calls);
                    emit log_named_bytes("  data", why);
                    break;
                }
            }
            if (left == 0) assertEq(e.cash(), cash, "same result at any gas");
            vm.revertToState(snap);
            snap = vm.snapshotState();
        }
    }
}
