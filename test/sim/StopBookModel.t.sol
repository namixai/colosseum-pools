// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {BookStub} from "../exam/PaperExamInvariant.t.sol";
import {StopBookBase} from "./StopBook.t.sol";

/// The stop book against a model of the market. A random run of prices by block and of what the traders do: set, move
/// and remove stops and takes of any size, longs and shorts, on 12 to 16 exams and 4 to 6 assets; blocks whose oracle
/// gives no price; crowds of triggers set in one block; blocks nobody checks; settles by the executor, by strangers and
/// by the traders' own calls, at any time; a `fillDelay` of 1 to 9 blocks; a deadline that may fall in the run; exams that
/// fill at the book. Then every exam is settled. The model fills every armed stop and take at the first checked block,
/// from its `armedAt`, whose oracle price reached it, at that price (or the book's from it), a stop before a take, and
/// drops it unfilled if that block's time is past the deadline. At the end the positions and the cash of every exam, the
/// price, touch block and size of every fill and the triggers left are the model's.
contract StopBookModelTest is StopBookBase {
    uint256 constant MAX_N = 16;
    uint256 constant MAX_A = 6;

    // A run's shape.
    uint256 n;
    uint256 na;
    uint256 steps;
    uint16 dl;
    uint32 stepDuration;
    bool books;

    PaperExam[MAX_N] ex;
    bool[MAX_N] atBook;
    uint32[MAX_A] A;
    uint64[MAX_A] base;
    uint64[MAX_A] px; // this block's oracle price; zero: none
    uint64[MAX_A] lastNZ;
    uint64[2][MAX_A] bbo; // bid, ask

    struct T {
        uint64 level;
        uint64 armedAt;
        bool below;
        uint64 sz;
    }

    struct F {
        uint64 px;
        uint256 blk;
        uint64 sz;
    }

    mapping(uint256 => mapping(uint256 => mapping(bool => T))) mt;
    mapping(uint256 => mapping(uint256 => int256)) mpos;
    mapping(uint256 => int256) mcash;
    mapping(uint256 => mapping(uint256 => mapping(bool => F[]))) mF;
    mapping(uint256 => mapping(uint256 => mapping(bool => F[]))) cF;
    mapping(uint256 => uint256) deadlineOf;
    uint256 lastChecked;
    uint256 rnd;

    function setUp() public override {
        super.setUp();
        A = [BTC, ETH, uint32(100), uint32(101), uint32(102), uint32(103)];
        base = [MARK, ETH_MARK, MARK, MARK, MARK, MARK];
        for (uint256 k = 2; k < MAX_A; ++k) {
            _asset(A[k], 5, MARK);
        }
    }

    function _next(uint256 m) internal returns (uint256) {
        rnd = uint256(keccak256(abi.encode(rnd)));
        return rnd % m;
    }

    function _init(uint256 seed) internal {
        rnd = seed;
        uint32[] memory as_ = new uint32[](na);
        for (uint256 k = 0; k < na; ++k) {
            as_[k] = A[k];
            BookStub(BBO).set(A[k], base[k] - 1, base[k] + 1); // a tight book for the opening trades
        }
        ExamTerms memory t = _terms();
        t.fillDelay = dl;
        t.stepDuration = stepDuration;
        for (uint256 i = 0; i < n; ++i) {
            atBook[i] = books && i % 2 == 1;
            ex[i] = _examWith(as_, t, atBook[i]);
            deadlineOf[i] = ex[i].deadline();
        }
        for (uint256 i = 0; i < n; ++i) {
            for (uint256 k = 0; k < na; ++k) {
                _trade(ex[i], A[k], i < n / 2, 60);
            }
        }
        vm.roll(block.number + 1);
        stopBook.poke();
        for (uint256 k = 0; k < na; ++k) {
            (px[k], lastNZ[k]) = (base[k], base[k]);
        }
        _drawBooks();
        lastChecked = stopBook.lastPokeBlock();
        for (uint256 i = 0; i < n; ++i) {
            for (uint256 k = 0; k < na; ++k) {
                mpos[i][k] = ex[i].positionOf(A[k]);
            }
            mcash[i] = ex[i].cash();
        }
    }

    /// The price a stop's or a take's fill takes from the oracle price `o` and the book of the block of its touch, as
    /// the journal logs it (millionths of `o`, rounded down) and the exam reads it (`PaperExam._fillPrice`, clamped).
    function _fillPx(uint256 i, uint256 k, bool sell, uint64 o) internal view returns (uint64 p) {
        uint256 bid = bbo[k][0];
        uint256 ask = bbo[k][1];
        if (!atBook[i] || bid == 0 || ask < bid) return o;
        uint256 off = sell ? (bid < o ? (o - bid) * 1e6 / o : 0) : (ask > o ? (ask - o) * 1e6 / o : 0);
        if (off > 65_534) off = 65_534;
        uint256 gap = uint256(o) * off / 1e6;
        if (gap * 10_000 > uint256(o) * WORSE) gap = uint256(o) * WORSE / 10_000;
        p = uint64(sell ? o - gap : o + gap);
    }

    /// Each asset's book drawn again: empty now and then, otherwise off its price by up to 3 % either side.
    function _drawBooks() internal {
        for (uint256 k = 0; k < na; ++k) {
            uint64 c = lastNZ[k];
            if (_next(4) == 0) {
                bbo[k] = [uint64(0), uint64(0)];
            } else {
                uint64 d1 = uint64(c * _next(300) / 10_000);
                uint64 d2 = uint64(c * _next(300) / 10_000);
                bbo[k] = [c - d1, c + d2];
            }
            BookStub(BBO).set(A[k], bbo[k][0], bbo[k][1]);
        }
    }

    /// The model checks this block, once, at its prices: every armed trigger they reach, a stop before a take.
    function _modelCheck() internal {
        if (lastChecked == block.number) return;
        lastChecked = block.number;
        for (uint256 i = 0; i < n; ++i) {
            for (uint256 k = 0; k < na; ++k) {
                uint64 p = px[k];
                if (p == 0) continue;
                for (uint256 t = 0; t < 2; ++t) {
                    T memory tr = mt[i][k][t == 1];
                    if (tr.level == 0 || tr.armedAt > block.number) continue;
                    if (tr.below ? p > tr.level : p < tr.level) continue;
                    delete mt[i][k][t == 1];
                    if (deadlineOf[i] != 0 && block.timestamp > deadlineOf[i]) continue; // over: dropped, unfilled
                    int256 pos = mpos[i][k];
                    int256 abs_ = pos < 0 ? -pos : pos;
                    int256 close = (tr.sz == 0 || int256(uint256(tr.sz)) > abs_) ? abs_ : int256(uint256(tr.sz));
                    int256 signed_ = pos < 0 ? -close : close;
                    uint64 fp = _fillPx(i, k, pos > 0, p);
                    mcash[i] += signed_ * int256(uint256(fp));
                    mpos[i][k] = pos - signed_;
                    mF[i][k][t == 1].push(F(fp, block.number, uint64(uint256(close))));
                    if (mpos[i][k] == 0) {
                        delete mt[i][k][true];
                        delete mt[i][k][false];
                    }
                }
            }
        }
    }

    function _newBlock(bool jump) internal {
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + 1 + _next(3));
        for (uint256 k = 0; k < na; ++k) {
            if (_next(12) == 0) {
                px[k] = 0;
                CoreSimulatorLib.setMarkPx(A[k], 0);
                continue;
            }
            uint64 cur = lastNZ[k];
            uint256 bps = jump ? 300 + _next(600) : _next(150);
            uint256 d = uint256(cur) * bps / 10_000;
            uint64 nx = _next(2) == 0 ? cur + uint64(d) : cur - uint64(d);
            if (nx > base[k] * 12 / 10) nx = base[k] * 12 / 10;
            if (nx < base[k] * 8 / 10) nx = base[k] * 8 / 10;
            (px[k], lastNZ[k]) = (nx, nx);
            CoreSimulatorLib.setMarkPx(A[k], nx);
        }
        // The book moves from block to block, between a touch and its settle too: the fill is the touch block's.
        if (_next(2) == 0) _drawBooks();
    }

    function _idx(address e) internal view returns (uint256 i) {
        for (i = 0; i < n; ++i) {
            if (address(ex[i]) == e) return i;
        }
    }

    function _assetIdx(uint32 a) internal view returns (uint256 k) {
        for (k = 0; k < na; ++k) {
            if (A[k] == a) return k;
        }
    }

    /// The fills of the call just made, kept if it went through (the recorder also keeps what a reverted call emitted).
    function _take(bool ok) internal {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        if (!ok) return;
        bytes32 sig = PaperExam.TriggerFilled.selector;
        for (uint256 j = 0; j < logs.length; ++j) {
            if (logs[j].topics[0] != sig) continue;
            uint256 i = _idx(logs[j].emitter);
            if (i == n) continue;
            uint256 k = _assetIdx(uint32(uint256(logs[j].topics[1])));
            (bool isTake, uint64 sz, uint64 p,,,, uint256 touch,) =
                abi.decode(logs[j].data, (bool, uint64, uint64, uint256, uint64, uint8, uint256, uint64));
            cF[i][k][isTake].push(F(p, touch, sz));
        }
    }

    function _poke() internal {
        vm.recordLogs();
        stopBook.poke();
        _take(true);
        _modelCheck();
    }

    function _settle(uint256 i) internal {
        vm.recordLogs();
        vm.prank(stranger);
        stopBook.settle(address(ex[i]));
        _take(true);
        _modelCheck();
    }

    function _trySet(uint256 i, uint256 k, bool isTake, uint256 bps) internal {
        bool isLong = i < n / 2;
        bool below = isTake != isLong;
        uint64 cur = lastNZ[k];
        uint64 d = uint64(uint256(cur) * bps / 10_000) + 1;
        uint64 level = below ? cur - d : cur + d;
        uint64 sz = _next(4) == 0 ? uint64(10 + _next(45)) : 0;
        vm.recordLogs();
        vm.prank(trader);
        try ex[i].setTrigger(A[k], isTake, level, sz) {
            _take(true);
            _modelCheck();
            assertTrue(mpos[i][k] != 0, "a trigger set on a position the model has closed");
            assertTrue(px[k] != 0, "set while the oracle was silent");
            mt[i][k][isTake] = T(level, uint64(block.number) + dl, below, sz);
        } catch {
            _take(false);
        }
    }

    function _tryClear(uint256 i, uint256 k, bool isTake) internal {
        vm.recordLogs();
        vm.prank(trader);
        try ex[i].clearTrigger(A[k], isTake) {
            _take(true);
            _modelCheck();
            assertTrue(mt[i][k][isTake].level != 0, "a trigger the model has filled was removed");
            delete mt[i][k][isTake];
        } catch {
            _take(false);
        }
    }

    function _run() internal {
        for (uint256 s = 0; s < steps; ++s) {
            uint256 r = _next(100);
            if (r < 28) {
                _newBlock(_next(6) == 0);
                if (_next(10) < 7) {
                    _poke();
                    // The executor settles the exams it sees touched; here, some of them.
                    for (uint256 i = 0; i < n; ++i) {
                        if (_next(3) == 0) _settle(i);
                    }
                }
            } else if (r < 33) {
                vm.roll(block.number + 1 + _next(3)); // blocks nobody checks, the prices as they were
                vm.warp(block.timestamp + 2 + _next(4));
            } else if (r < 58) {
                _trySet(_next(n), _next(na), _next(2) == 0, 20 + _next(400));
            } else if (r < 66) {
                _tryClear(_next(n), _next(na), _next(2) == 0);
            } else if (r < 74) {
                _settle(_next(n));
            } else {
                // A crowd: every exam sets the same kind of trigger close to the price, in one block, on one or two
                // assets.
                uint256 k = _next(na);
                bool isTake = _next(2) == 0;
                uint256 bps = 20 + _next(150);
                bool two = _next(3) == 0;
                for (uint256 i = 0; i < n; ++i) {
                    _trySet(i, k, isTake, bps + i);
                    if (two) _trySet(i, (k + 1) % na, isTake, bps + i);
                }
            }
        }
        // The oracle answers again, the prices stay; one check and one settle of each exam carry out everything.
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + 1);
        for (uint256 k = 0; k < na; ++k) {
            if (px[k] == 0) {
                px[k] = lastNZ[k];
                CoreSimulatorLib.setMarkPx(A[k], px[k]);
            }
        }
        _poke();
        for (uint256 i = 0; i < n; ++i) {
            vm.recordLogs();
            assertEq(stopBook.settle(address(ex[i])), 0, "one settle carries out all");
            _take(true);
        }
        _compare();
    }

    function _compare() internal view {
        for (uint256 i = 0; i < n; ++i) {
            assertEq(stopBook.touches(address(ex[i])).length, 0, "no touch left");
            for (uint256 k = 0; k < na; ++k) {
                for (uint256 t = 0; t < 2; ++t) {
                    bool tk = t == 1;
                    F[] storage m = mF[i][k][tk];
                    F[] storage c = cF[i][k][tk];
                    assertEq(c.length, m.length, "the number of fills");
                    for (uint256 j = 0; j < m.length; ++j) {
                        assertEq(c[j].px, m[j].px, "the fill price: the first touch's");
                        assertEq(c[j].blk, m[j].blk, "the touch block: the first checked block that reached it");
                        assertEq(c[j].sz, m[j].sz, "the fill size");
                    }
                    StopBook.Entry memory e = stopBook.entryOf(address(ex[i]), A[k], tk);
                    assertEq(e.level, mt[i][k][tk].level, "the book holds what the model holds");
                }
                assertEq(ex[i].positionOf(A[k]), mpos[i][k], "the position");
            }
            assertEq(ex[i].cash(), mcash[i], "the cash");
        }
    }

    /// The reference run: 12 to 16 exams, 4 to 6 assets, a delay of 1 to 9 blocks, a deadline in the run half the time,
    /// half the exams at the book half the time.
    /// forge-config: default.fuzz.runs = 10000
    function testFuzz_model_everyTouchFillsAtItsFirstCheckedPrice(uint256 seed) public {
        n = 12 + seed % 5;
        na = 4 + (seed >> 8) % 3;
        steps = 70 + (seed >> 16) % 51;
        dl = uint16(1 + (seed >> 24) % 9);
        stepDuration = (seed >> 32) % 2 == 0 ? 7 days : uint32(40 + (seed >> 40) % 300);
        books = (seed >> 48) % 2 == 0;
        _init(seed);
        _run();
    }

    /// The independent review's models of cp#335 (21 November 2026), on this book: 12 exams on 4 assets, a delay of 2.
    /// forge-config: default.fuzz.runs = 1500
    function testFuzz_model_audit4(uint256 seed) public {
        (n, na, steps, dl, stepDuration, books) = (12, 4, 70, 2, 7 days, false);
        _init(seed);
        _run();
    }

    /// ...16 exams, 120 steps.
    /// forge-config: default.fuzz.runs = 4000
    function testFuzz_model_audit4Big(uint256 seed) public {
        (n, na, steps, dl, stepDuration, books) = (16, 4, 120, 2, 7 days, false);
        _init(seed);
        _run();
    }

    /// ...a delay of 1 to 9 blocks.
    /// forge-config: default.fuzz.runs = 1500
    function testFuzz_model_audit4Delay(uint256 seed, uint8 d) public {
        (n, na, steps, dl, stepDuration, books) = (12, 4, 70, uint16(1 + d % 9), 7 days, false);
        _init(seed);
        _run();
    }
}
