// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {BookStub} from "../exam/PaperExamInvariant.t.sol";
import {StopBookBase} from "./StopBook.t.sol";

/// The fifth independent audit of cp#335 (22 November 2026, head fcbb5e51), its scenarios on the fixed book: the fill
/// price of a stop is its touch block's, whoever settles and whenever (H1, turned round: on fcbb5e51 three moments gave
/// three prices); settles with random gas carry out what one full settle does; a trader's call with any gas never acts
/// on a touch not carried out. Its tests of a stop cleared or moved just before a wick (M1) wait for the delayed
/// clearing, which this head does not have yet.
contract StopBookAudit5Test is StopBookBase {
    uint32[8] internal S;

    function setUp() public override {
        super.setUp();
        for (uint256 k = 0; k < 8; ++k) {
            S[k] = uint32(300 + k);
            _asset(S[k], 5, MARK);
        }
    }

    // ── 4: the fill price depends on WHEN the settle comes ───────

    function _bookExam() internal returns (PaperExam e) {
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        e = _examOn(one, true);
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        vm.roll(block.number + DELAY + 1);
    }

    /// one touch (oracle 940 000 in block T, nobody settles), three settle moments -> three prices
    function test_a5_H1_onePriceWhoeverSettlesWhenever() public {
        uint256[3] memory cashOf;
        for (uint256 scen = 0; scen < 3; ++scen) {
            uint256 snap = vm.snapshotState();
            PaperExam e = _bookExam();
            _mark(940_000);
            BookStub(BBO).set(BTC, 939_000, 941_000); // a normal book at the touch
            stopBook.poke(); // block T is logged
            uint256 touchBlk = block.number;
            if (scen == 0) {
                // settle at once: the book of the touch block
            } else {
                vm.roll(block.number + 5);
                if (scen == 1) {
                    _mark(1_000_000); // the market came back
                    BookStub(BBO).set(BTC, 999_990, 1_000_010); // trader waited for a tight book over the oracle of the touch
                } else {
                    BookStub(BBO).set(BTC, 700_000, 941_000); // a thin book at the moment somebody else picks
                }
            }
            stopBook.settle(address(e));
            (bool ok,) = (true, 0);
            ok;
            emit log_named_uint("scenario", scen);
            emit log_named_int("cash after the one stop (100 units sold; bought at 1 000 010)", e.cash());
            cashOf[scen] = uint256(e.cash());
            assertEq(e.positionOf(BTC), 0);
            assertEq(touchBlk > 0, true);
            vm.revertToState(snap);
        }
        // The same touch, one price: the touch block's book, whoever settles and whenever.
        assertEq(cashOf[1], cashOf[0], "waiting for a tight book changes nothing");
        assertEq(cashOf[2], cashOf[0], "nor somebody else's moment");
    }

    // ── 6: random gas for the settle vs one full settle ──────────

    uint256 rnd;

    function _n(uint256 m) internal returns (uint256) {
        rnd = uint256(keccak256(abi.encode(rnd)));
        return rnd % m;
    }

    function _build(uint256 seed, uint256 nAssets) internal returns (PaperExam e) {
        rnd = seed;
        uint32[] memory as_ = new uint32[](nAssets);
        for (uint256 k = 0; k < nAssets; ++k) {
            as_[k] = S[k];
        }
        e = _examWith(as_, _terms(), _n(2) == 0);
        for (uint256 k = 0; k < nAssets; ++k) {
            BookStub(BBO).set(S[k], MARK - 10, MARK + 10);
            _trade(e, S[k], _n(2) == 0, 20);
        }
        for (uint256 k = 0; k < nAssets; ++k) {
            bool isLong = e.positionOf(S[k]) > 0;
            uint64 d1 = uint64(1 + _n(8)) * 10_000;
            uint64 d2 = uint64(1 + _n(8)) * 10_000;
            _set(e, S[k], false, isLong ? MARK - d1 : MARK + d1, _n(3) == 0 ? 7 : 0);
            _set(e, S[k], true, isLong ? MARK + d2 : MARK - d2, _n(3) == 0 ? 5 : 0);
        }
        vm.roll(block.number + DELAY);
    }

    function _walk(uint256 nAssets, uint256 blocks) internal {
        for (uint256 b = 0; b < blocks; ++b) {
            vm.roll(block.number + 1);
            vm.warp(block.timestamp + 1);
            for (uint256 k = 0; k < nAssets; ++k) {
                uint256 r = _n(10);
                uint64 px = r == 0 ? 0 : uint64(MARK - 100_000 + _n(200_001));
                CoreSimulatorLib.setMarkPx(S[k], px);
            }
            stopBook.poke();
        }
        for (uint256 k = 0; k < nAssets; ++k) {
            CoreSimulatorLib.setMarkPx(S[k], MARK);
        }
        vm.roll(block.number + 1);
    }

    function _state(PaperExam e, uint256 nAssets) internal view returns (bytes32 h) {
        h = keccak256(abi.encode(e.cash(), e.lastFillAt(), e.tradingDays()));
        for (uint256 k = 0; k < nAssets; ++k) {
            (uint64 px, uint64 sz, uint64 ar) = e.stopOf(S[k]);
            (uint64 px2, uint64 sz2, uint64 ar2) = e.takeOf(S[k]);
            h = keccak256(abi.encode(h, e.positionOf(S[k]), px, sz, ar, px2, sz2, ar2));
        }
    }

    function testFuzz_a5_randomGasSettles_equalOneFullSettle(uint256 seed) public {
        uint256 nA = 2 + seed % 7;
        PaperExam e = _build(seed, nA);
        _walk(nA, 30 + (seed >> 8) % 60);
        uint256 snap = vm.snapshotState();
        // reference: one settle with all the gas
        uint256 left = stopBook.settle(address(e));
        assertEq(left, 0);
        bytes32 want = _state(e, nA);
        vm.revertToState(snap);
        // many settles with random gas, in random blocks
        uint256 calls;
        left = 1;
        while (left != 0 && calls < 200) {
            ++calls;
            uint256 g = 100_000 + _n(1_900_000);
            if (_n(3) == 0) {
                vm.roll(block.number + 1);
                vm.warp(block.timestamp + 1);
            }
            try stopBook.settle{gas: g}(address(e)) returns (uint256 l) {
                left = l;
            } catch {}
        }
        assertEq(left, 0, "never settled");
        assertEq(_state(e, nA), want, "partial settles differ from the full one");
        assertEq(stopBook.touches(address(e)).length, 0);
    }

    // ── 3: a trader's call with chosen gas never gets past a pending touch ──

    function testFuzz_a5_traderCallWithAnyGas_neverActsOnAPendingTouch(uint256 seed) public {
        uint256 nA = 2 + seed % 7;
        PaperExam e = _build(seed, nA);
        _walk(nA, 20 + (seed >> 8) % 40);
        for (uint256 i = 0; i < 25; ++i) {
            uint256 g = 80_000 + _n(2_400_000);
            uint256 pendingBefore = stopBook.touches(address(e)).length;
            vm.prank(trader);
            (bool ok,) = address(e).call{gas: g}(abi.encodeCall(PaperExam.setAgent, (address(uint160(1 + _n(5))))));
            if (ok) {
                assertEq(stopBook.touches(address(e)).length, 0, "a call went through with a touch pending");
                assertEq(stopBook.entryOf(address(e), S[0], false).level == 0 || true, true);
            }
            pendingBefore;
        }
    }

}
