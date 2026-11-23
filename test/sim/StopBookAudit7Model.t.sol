// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {BookStub} from "../exam/PaperExamInvariant.t.sol";
import {StopBookBase} from "./StopBook.t.sol";

contract AuditBook is StopBook {
    function countOf(uint32 a) external view returns (uint256) {
        return _count[a];
    }

    function assetPos(uint32 a) external view returns (uint256) {
        return _assetPos[a];
    }

    function findRaw(uint32 asset, uint256 s, uint256 len, uint256 lo, uint256 hi) external view returns (uint256) {
        return _find(asset, s, len, lo, hi);
    }

    function offOf(uint32 asset) external pure returns (uint256) {
        return _off(asset);
    }

    function rawAt(uint32 asset, uint256 pos) external view returns (uint64) {
        return _raw(asset, pos + _off(asset));
    }

    function feed(uint32 asset, uint256 g, uint256 x) external {
        _log(asset, g + _off(asset), x);
    }

    function skipTo(uint256 n) external {
        _len = uint64(n);
    }
}

/// Independent reference run (auditor 7): as the authors' model, plus partial (gas-limited) settles, orders that close
/// or turn a position round, a `maxIdle`, and after every full settle: the book's count of entries per asset, its list of
/// assets, and the exam's in-force trigger against the book's entry.
contract StopBookAudit7ModelTest is StopBookBase {
    uint256 constant MAX_N = 12;
    uint256 constant MAX_A = 5;

    uint256 n;
    uint256 na;
    uint256 steps;
    uint16 dl;
    uint32 stepDuration;
    uint32 maxIdle;
    bool books;
    bool heavy;

    PaperExam[MAX_N] ex;
    bool[MAX_N] atBook;
    uint32[MAX_A] A;
    uint64[MAX_A] base;
    uint64[MAX_A] px;
    uint64[MAX_A] lastNZ;
    uint64[2][MAX_A] bbo;

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
    mapping(uint256 => mapping(uint256 => mapping(bool => T))) mc;
    mapping(uint256 => mapping(uint256 => int256)) mpos;
    mapping(uint256 => int256) mcash;
    mapping(uint256 => mapping(uint256 => mapping(bool => F[]))) mF;
    mapping(uint256 => mapping(uint256 => mapping(bool => F[]))) cF;
    mapping(uint256 => uint256) deadlineOf;
    mapping(uint256 => uint256) lastFill;
    uint256 lastChecked;
    uint256 rnd;
    uint256 public partials;
    uint256 public trades;

    function setUp() public override {
        super.setUp();
        stopBook = new AuditBook();
        A = [BTC, ETH, uint32(100), uint32(101), uint32(102)];
        base = [MARK, ETH_MARK, MARK, MARK, MARK];
        for (uint256 k = 2; k < MAX_A; ++k) {
            _asset(A[k], 5, MARK);
        }
    }

    function _book() internal view returns (AuditBook) {
        return AuditBook(address(stopBook));
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
            BookStub(BBO).set(A[k], base[k] - 1, base[k] + 1);
        }
        ExamTerms memory t = _terms();
        t.fillDelay = dl;
        t.stepDuration = stepDuration;
        t.maxIdle = maxIdle;
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
            lastFill[i] = ex[i].lastFillAt();
        }
    }

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

    function _over(uint256 i, uint256 t) internal view returns (bool) {
        if (deadlineOf[i] != 0 && t > deadlineOf[i]) return true;
        return maxIdle != 0 && t > lastFill[i] + maxIdle;
    }

    function _modelCheck() internal {
        if (stopBook.lastPokeBlock() != block.number) return;
        if (lastChecked == block.number) return;
        lastChecked = block.number;
        for (uint256 i = 0; i < n; ++i) {
            for (uint256 k = 0; k < na; ++k) {
                uint64 p = px[k];
                for (uint256 t = 0; t < 2; ++t) {
                    _changeInForce(i, k, t == 1);
                    if (p == 0) continue;
                    T memory tr = mt[i][k][t == 1];
                    if (tr.level == 0 || tr.armedAt > block.number) continue;
                    if (tr.below ? p > tr.level : p < tr.level) continue;
                    delete mt[i][k][t == 1];
                    delete mc[i][k][t == 1];
                    if (_over(i, block.timestamp)) continue;
                    int256 pos = mpos[i][k];
                    int256 abs_ = pos < 0 ? -pos : pos;
                    int256 close = (tr.sz == 0 || int256(uint256(tr.sz)) > abs_) ? abs_ : int256(uint256(tr.sz));
                    int256 signed_ = pos < 0 ? -close : close;
                    uint64 fp = _fillPx(i, k, pos > 0, p);
                    mcash[i] += signed_ * int256(uint256(fp));
                    mpos[i][k] = pos - signed_;
                    mF[i][k][t == 1].push(F(fp, block.number, uint64(uint256(close))));
                    if (uint256(close) * fp >= 10e6) lastFill[i] = block.timestamp;
                    if (mpos[i][k] == 0) {
                        delete mt[i][k][true];
                        delete mt[i][k][false];
                        delete mc[i][k][true];
                        delete mc[i][k][false];
                    }
                }
            }
        }
    }

    function _changeInForce(uint256 i, uint256 k, bool tk) internal {
        T memory c = mc[i][k][tk];
        if (c.armedAt == 0 || c.armedAt > block.number) return;
        mt[i][k][tk] = c.level == 0 ? T(0, 0, false, 0) : T(c.level, c.armedAt, mt[i][k][tk].below, c.sz);
        delete mc[i][k][tk];
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

    struct Ord {
        bool had;
        uint256 i;
        uint256 k;
        bool buy;
        uint64 sz;
        uint64 px;
    }

    Ord internal lastOrd;

    /// Keeps the trigger fills of the call; the exam's last order fill in it goes to `lastOrd`.
    function _take(bool ok) internal {
        delete lastOrd;
        Vm.Log[] memory logs = vm.getRecordedLogs();
        if (!ok) return;
        for (uint256 j = 0; j < logs.length; ++j) {
            _one(logs, j);
        }
    }

    function _one(Vm.Log[] memory logs, uint256 j) internal {
        bytes32 sig = PaperExam.TriggerFilled.selector;
        if (logs[j].topics[0] == PaperExam.Filled.selector) {
            if (j + 1 < logs.length && logs[j + 1].topics[0] == sig) return;
            (bool b, uint64 sz, uint64 p,,,) = abi.decode(logs[j].data, (bool, uint64, uint64, uint256, uint256, uint8));
            lastOrd = Ord(true, _idx(logs[j].emitter), _assetIdx(uint32(uint256(logs[j].topics[1]))), b, sz, p);
            return;
        }
        if (logs[j].topics[0] != sig) return;
        uint256 i = _idx(logs[j].emitter);
        if (i == n) return;
        uint256 k = _assetIdx(uint32(uint256(logs[j].topics[1])));
        (bool isTake, uint64 sz2, uint64 p2,,,, uint256 touch,) =
            abi.decode(logs[j].data, (bool, uint64, uint64, uint256, uint64, uint8, uint256, uint64));
        cF[i][k][isTake].push(F(p2, touch, sz2));
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

    /// A settle sent with little gas: it may stop short (owed entries), run out, or finish.
    function _partial(uint256 i) internal {
        uint256 g = 180_000 + _next(330_000);
        vm.recordLogs();
        vm.prank(stranger);
        try stopBook.settle{gas: g}(address(ex[i])) returns (uint256 left) {
            _take(true);
            if (left != 0) ++partials;
        } catch {
            _take(false);
        }
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
            _changeInForce(i, k, isTake);
            if (mt[i][k][isTake].level != 0) mc[i][k][isTake] = T(level, uint64(block.number) + dl, below, sz);
            else mt[i][k][isTake] = T(level, uint64(block.number) + dl, below, sz);
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
            _changeInForce(i, k, isTake);
            assertTrue(mt[i][k][isTake].level != 0, "a trigger the model has filled was removed");
            mc[i][k][isTake] = T(0, uint64(block.number) + dl, false, 0);
        } catch {
            _take(false);
        }
    }

    /// An order that closes or turns the position round, placed, then filled `fillDelay` blocks on by the filler.
    function _tryTrade(uint256 i, uint256 k) internal {
        if (atBook[i]) return;
        int256 pos = mpos[i][k];
        if (pos == 0) return;
        uint256 ap = uint256(pos < 0 ? -pos : pos);
        uint64 sz = uint64(1 + _next(2 * ap));
        if (sz > 2 * ap - 1) sz = uint64(ap);
        bool isBuy = pos < 0;
        vm.recordLogs();
        vm.prank(trader);
        try ex[i].place(A[k], isBuy, sz) {
            _take(true);
            _modelCheck();
        } catch {
            _take(false);
            return;
        }
        vm.roll(block.number + dl);
        vm.warp(block.timestamp + 1);
        vm.recordLogs();
        vm.prank(filler);
        try ex[i].fill() {
            _take(true);
            _modelCheck();
            _applyOrder(i);
        } catch {
            _take(false);
        }
    }

    function _applyOrder(uint256 i) internal {
        Ord memory o = lastOrd;
        assertTrue(o.had, "order fill event");
        assertEq(o.i, i);
        ++trades;
        int256 before = mpos[o.i][o.k];
        int256 delta = o.buy ? int256(uint256(o.sz)) : -int256(uint256(o.sz));
        int256 aft = before + delta;
        mpos[o.i][o.k] = aft;
        mcash[o.i] -= delta * int256(uint256(o.px));
        if (before != 0 && (aft == 0 || (aft > 0) != (before > 0))) {
            delete mt[o.i][o.k][true];
            delete mt[o.i][o.k][false];
            delete mc[o.i][o.k][true];
            delete mc[o.i][o.k][false];
        }
        if (uint256(o.sz) * o.px >= 10e6) lastFill[o.i] = block.timestamp;
    }

    function _settleAllAndCheck() internal {
        _poke();
        for (uint256 i = 0; i < n; ++i) {
            vm.recordLogs();
            assertEq(stopBook.settle(address(ex[i])), 0, "settle");
            _take(true);
        }
        _modelCheck();
        for (uint256 i = 0; i < n; ++i) {
            for (uint256 k = 0; k < na; ++k) {
                _changeInForce(i, k, false);
                _changeInForce(i, k, true);
            }
        }
        _compare();
        _bookInvariants();
    }

    function _bookInvariants() internal view {
        uint256 total;
        uint32[] memory w = stopBook.watched();
        assertLe(w.length, 20, "assets logged");
        for (uint256 k = 0; k < na; ++k) {
            uint256 c;
            for (uint256 i = 0; i < n; ++i) {
                c += (_where(ex[i], A[k], false) != 0 ? 1 : 0) + (_where(ex[i], A[k], true) != 0 ? 1 : 0);
            }
            assertEq(_book().countOf(A[k]), c, "count of entries per asset");
            bool listed;
            for (uint256 j = 0; j < w.length; ++j) {
                if (w[j] == A[k]) {
                    listed = true;
                    assertEq(_book().assetPos(A[k]), j + 1, "assetPos");
                }
            }
            assertEq(listed, c != 0, "watched iff an entry");
            if (c != 0) ++total;
        }
        assertEq(total, w.length, "no stray watched asset");
    }

    function _compare() internal view {
        for (uint256 i = 0; i < n; ++i) {
            for (uint256 k = 0; k < na; ++k) {
                for (uint256 t = 0; t < 2; ++t) {
                    bool tk = t == 1;
                    F[] storage m = mF[i][k][tk];
                    F[] storage c = cF[i][k][tk];
                    assertEq(c.length, m.length, "the number of fills");
                    for (uint256 j = 0; j < m.length; ++j) {
                        assertEq(c[j].px, m[j].px, "fill price");
                        assertEq(c[j].blk, m[j].blk, "touch block");
                        assertEq(c[j].sz, m[j].sz, "fill size");
                    }
                    StopBook.Entry memory e = stopBook.entryOf(address(ex[i]), A[k], tk);
                    assertEq(e.level, mt[i][k][tk].level, "book level");
                    if (e.level != 0) assertEq(e.armedAt, mt[i][k][tk].armedAt, "book armedAt");
                    assertEq(e.nextLevel, mc[i][k][tk].level, "change level");
                    assertEq(e.nextAt, mc[i][k][tk].armedAt, "change from");
                    // The exam's own in-force trigger (a change in force takes the place, lazily).
                    (uint64 xp,, uint64 xa) = tk ? ex[i].takeOf(A[k]) : ex[i].stopOf(A[k]);
                    (uint64 cp,, uint64 ca) = ex[i].changeOf(A[k], tk);
                    if (ca != 0 && ca <= block.number) (xp, xa) = (cp, cp == 0 ? 0 : ca);
                    assertEq(xp, mt[i][k][tk].level, "exam level == model");
                    if (xp != 0) assertEq(xa, mt[i][k][tk].armedAt, "exam armedAt == model");
                }
                assertEq(ex[i].positionOf(A[k]), mpos[i][k], "position");
            }
            assertEq(ex[i].cash(), mcash[i], "cash");
            assertEq(uint256(ex[i].lastFillAt()), lastFill[i], "lastFillAt");
        }
    }

    function _run() internal {
        for (uint256 s = 0; s < steps; ++s) {
            uint256 r = _next(100);
            if (r < 24) {
                _newBlock(_next(6) == 0);
                if (_next(10) < 7) {
                    _poke();
                    for (uint256 i = 0; i < n; ++i) {
                        uint256 q = heavy ? 1 : _next(4);
                        if (q == 0) _settle(i);
                        else if (q == 1) _partial(i);
                    }
                }
            } else if (r < 28) {
                vm.roll(block.number + 1 + _next(3));
                vm.warp(block.timestamp + 2 + _next(4));
            } else if (r < 50) {
                _trySet(_next(n), _next(na), _next(2) == 0, 20 + _next(400));
            } else if (r < 58) {
                _tryClear(_next(n), _next(na), _next(2) == 0);
            } else if (r < 64) {
                _settle(_next(n));
            } else if (r < 70) {
                uint256 k = _next(na);
                bool isTake = _next(2) == 0;
                uint256 bps = 20 + _next(150);
                for (uint256 i = 0; i < n; ++i) {
                    _trySet(i, k, isTake, bps + i);
                }
            } else if (r < 82) {
                _tryTrade(_next(n), _next(na));
            } else if (r < 90) {
                _partial(_next(n));
            } else if (r < 94) {
                _settleAllAndCheck();
            } else {
                _newBlock(false);
                _partial(_next(n));
            }
        }
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + 1);
        for (uint256 k = 0; k < na; ++k) {
            if (px[k] == 0) {
                px[k] = lastNZ[k];
                CoreSimulatorLib.setMarkPx(A[k], px[k]);
            }
        }
        _settleAllAndCheck();
        for (uint256 i = 0; i < n; ++i) {
            assertEq(stopBook.touches(address(ex[i])).length, 0, "no touch left");
        }
    }

    /// forge-config: default.fuzz.runs = 12000
    function testFuzz_a7_model(uint256 seed) public {
        n = 6 + seed % 7;
        na = 3 + (seed >> 8) % 3;
        steps = 70 + (seed >> 16) % 61;
        dl = uint16(1 + (seed >> 24) % 6);
        stepDuration = (seed >> 32) % 2 == 0 ? 7 days : uint32(60 + (seed >> 40) % 300);
        maxIdle = (seed >> 56) % 3 == 0 ? uint32(25 + (seed >> 60) % 150) : 0;
        books = (seed >> 48) % 2 == 0;
        _init(seed);
        _run();
    }

    /// Partial settles at every chance: a settle sent with 180 000 to 510 000 gas stops between steps, again and again.
    /// forge-config: default.fuzz.runs = 6000
    function testFuzz_a7_modelHeavyPartial(uint256 seed) public {
        heavy = true;
        n = 5 + seed % 5;
        na = 3 + (seed >> 8) % 3;
        steps = 80 + (seed >> 16) % 61;
        dl = uint16(1 + (seed >> 24) % 4);
        stepDuration = 7 days;
        maxIdle = (seed >> 56) % 3 == 0 ? uint32(25 + (seed >> 60) % 150) : 0;
        books = (seed >> 48) % 2 == 0;
        _init(seed);
        _run();
    }

    function _fills() internal view returns (uint256 f, uint256 chg) {
        for (uint256 i = 0; i < n; ++i) {
            for (uint256 k = 0; k < na; ++k) {
                f += cF[i][k][false].length + cF[i][k][true].length;
            }
        }
        chg = trades;
    }

    function test_a7_coverageProbe() public {
        uint256 seed = vm.envOr("A7SEED", uint256(12345));
        n = 8;
        na = 4;
        steps = 120;
        dl = 2;
        stepDuration = 7 days;
        maxIdle = 90;
        books = true;
        _init(seed);
        _run();
        (uint256 f,) = _fills();
        emit log_named_uint("trigger fills", f);
        emit log_named_uint("order trades", trades);
        emit log_named_uint("partial settles left>0", partials);
    }
}
