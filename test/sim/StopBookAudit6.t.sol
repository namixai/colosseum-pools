// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PaperExam} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {BookStub} from "../exam/PaperExamInvariant.t.sol";
import {StopBookBase, StopBookHarness, BadExam} from "./StopBook.t.sol";

contract A6Book is StopBook {
    function put(uint32 a, uint256 px, uint256 bk) external {
        uint256 g = _len;
        _len = uint64(g + 1);
        _at[g / 3] |= ((1000 + g) << 40 | (5000 + g)) << (g % 3 * 80);
        _log(a, g + _off(a), px == 0 ? 0 : bk << 32 | px);
    }

    function bulk(uint32 a, uint256 n, uint256 px, uint256 bk) external {
        for (uint256 i = 0; i < n; ++i) {
            uint256 g = _len;
            _len = uint64(g + 1);
            _log(a, g + _off(a), px == 0 ? 0 : bk << 32 | px);
        }
    }

    function find(uint32 a, uint256 s, uint256 len, uint256 lo, uint256 hi) external view returns (uint256) {
        return _find(a, s, len, lo, hi);
    }

    function bbo(uint32 a, uint64 px) external view returns (uint32) {
        return _bbo(a, px);
    }

    function pxAt(uint32 a, uint256 pos) external view returns (uint256) {
        return uint32(_raw(a, pos + _off(a)));
    }

    function rawAt(uint32 a, uint256 pos) external view returns (uint256) {
        return _raw(a, pos + _off(a));
    }
}

/// The sixth independent audit of cp#335 (22 November 2026, head 1ab0d913), its tests in the set: the book logged
/// off the oracle and read back, a stop's fill price against an order's in the same block on both sides, the price
/// whatever the settle's moment, a check's gas that cannot log a book as missing, the search against brute force, the
/// flat worst case (a step of O(log n)), and the bounds of a level.
contract StopBookAudit6Test is StopBookBase {
    A6Book ab;
    uint256 rnd;

    function setUp() public override {
        super.setUp();
        ab = new A6Book();
    }

    function _n(uint256 m) internal returns (uint256) {
        rnd = uint256(keccak256(abi.encode(rnd)));
        return rnd % m;
    }

    // ── P1(b): pack / unpack ─────────────────────────────────────

    function testFuzz_a6_bboRoundTrip(uint64 px, uint64 bid, uint64 ask, uint8 mode) public {
        px = uint64(bound(px, 1, ab.MAX_PRICE()));
        if (mode % 4 == 0) {
            // around the oracle, within 10 %
            bid = uint64(bound(bid, uint256(px) * 90 / 100, uint256(px) * 110 / 100 + 5));
            ask = uint64(bound(ask, bid, uint256(px) * 110 / 100 + 10));
            if (bid == 0) bid = 1;
        } else if (mode % 4 == 1) {
            bid = uint64(bound(bid, 1, px));
            ask = uint64(bound(ask, bid, type(uint64).max));
        }
        BookStub(BBO).set(BTC, bid, ask);
        uint32 b = ab.bbo(BTC, px);
        bool bad = bid == 0 || ask < bid;
        if (bad) {
            assertEq(b, ab.NO_BOOK());
            return;
        }
        assertTrue(b != ab.NO_BOOK(), "real value equals NO_BOOK");
        uint256 askOff = b >> 16;
        uint256 bidOff = uint16(b);
        uint256 gA = ask > px ? ask - px : 0;
        uint256 gB = bid < px ? px - bid : 0;
        uint256 rA = uint256(px) * askOff / 1e6;
        uint256 rB = uint256(px) * bidOff / 1e6;
        if (gA * 1e6 / px <= 65_534) {
            assertLe(rA, gA);
            assertLe(gA - rA, uint256(px) / 1e6 + 1);
        } else {
            assertEq(askOff, 65_534);
        }
        if (gB * 1e6 / px <= 65_534) {
            assertLe(rB, gB);
            assertLe(gB - rB, uint256(px) / 1e6 + 1);
        } else {
            assertEq(bidOff, 65_534);
        }
    }

    // ── P1(a,b): trigger price == ordinary fill price of the same block ──

    function _logsPx(Vm.Log[] memory L, address who) internal pure returns (uint256 n, uint64 px, uint8 src) {
        for (uint256 i = 0; i < L.length; ++i) {
            if (
                L[i].emitter == who
                    && L[i].topics[0] == keccak256("Filled(uint32,bool,uint64,uint64,uint256,uint256,uint8)")
            ) {
                (,, uint64 p,,, uint8 s) = abi.decode(L[i].data, (bool, uint64, uint64, uint256, uint256, uint8));
                ++n;
                px = p;
                src = s;
            }
        }
    }

    function testFuzz_a6_triggerPriceEqualsOrderPrice(uint64 touch, uint32 bOff, uint32 aOff, uint8 mode) public {
        touch = uint64(bound(touch, 700_000, 949_999));
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        PaperExam A = _examOn(one, true);
        PaperExam B = _examOn(one, true);
        _trade(A, BTC, true, 100);
        _trade(B, BTC, true, 100);
        _set(A, BTC, false, 950_000, 0);
        vm.roll(block.number + DELAY + 1);
        vm.prank(trader);
        B.place(BTC, false, 100);
        vm.roll(block.number + DELAY);
        _mark(touch);
        uint64 bid;
        uint64 ask;
        bid = uint64(uint256(touch) * (1_000_000 + 100_000 - bound(bOff, 0, 300_000)) / 1_000_000); // -20%..+10%
        ask = uint64(uint256(touch) * (1_000_000 - 100_000 + bound(aOff, 0, 300_000)) / 1_000_000);
        if (ask < bid && mode % 8 != 2) ask = bid + uint64(bound(aOff, 0, 5000));
        if (mode % 8 == 1) bid = 0;
        if (mode % 8 == 3) ask = 0;
        BookStub(BBO).set(BTC, bid, ask);
        vm.recordLogs();
        stopBook.poke();
        stopBook.settle(address(A));
        vm.prank(filler);
        try B.fill() {} catch {}
        _cmp(vm.getRecordedLogs(), A, B, touch);
    }

    function _cmp(Vm.Log[] memory L, PaperExam A, PaperExam B, uint64 touch) internal {
        (uint256 nA, uint64 pA, uint8 sA) = _logsPx(L, address(A));
        (uint256 nB, uint64 pB, uint8 sB) = _logsPx(L, address(B));
        assertEq(nA, 1, "stop filled");
        if (nB == 0) {
            assertEq(pA, touch - uint256(touch) * WORSE / 10_000, "refused order -> bound");
        } else {
            assertEq(sA, sB, "source");
            assertGe(pA, pB, "trigger never worse than order");
            assertLe(uint256(pA) - pB, uint256(touch) / 1e6 + 1, "diff > 1ppm+1");
            if (pA != pB) emit log_named_uint("diff", uint256(pA) - pB);
        }
    }

    /// whoever, whenever: wait up to 200 blocks with a wild book, price must stay
    function testFuzz_a6_priceIndependentOfSettleMoment(uint64 touch, uint8 wait, uint64 wb, uint64 wa) public {
        touch = uint64(bound(touch, 700_000, 949_999));
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        uint256 prices;
        uint64[2] memory res;
        for (uint256 sc = 0; sc < 2; ++sc) {
            uint256 snap = vm.snapshotState();
            BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
            PaperExam A = _examOn(one, true);
            _trade(A, BTC, true, 100);
            _set(A, BTC, false, 950_000, 0);
            vm.roll(block.number + DELAY + 1);
            _mark(touch);
            BookStub(BBO).set(BTC, touch - touch / 50, touch + 7);
            stopBook.poke();
            if (sc == 1) {
                vm.roll(block.number + 1 + wait);
                _mark(MARK);
                BookStub(BBO).set(BTC, uint64(bound(wb, 1, MARK)), uint64(bound(wa, 1, 2 * MARK)));
            }
            vm.recordLogs();
            stopBook.settle(address(A));
            Vm.Log[] memory L = vm.getRecordedLogs();
            (, res[sc],) = _logsPx(L, address(A));
            vm.revertToState(snap);
            prices++;
        }
        assertEq(res[0], res[1]);
        assertGt(res[0], 0);
    }

    // ── P1(c): poke with tuned gas ───────────────────────────────

    function testFuzz_a6_pokeGasCannotForgeNoBook(uint256 g) public {
        g = bound(g, 60_000, 700_000);
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        PaperExam A = _examOn(one, true);
        _trade(A, BTC, true, 100);
        _set(A, BTC, false, 950_000, 0);
        vm.roll(block.number + DELAY + 1);
        BookStub(BBO).set(BTC, MARK - 20_000, MARK + 3);
        uint256 snap = vm.snapshotState();
        stopBook.poke();
        uint32 want = StopBookBookAt(stopBook, BTC, stopBook.logged() - 1);
        vm.revertToState(snap);
        try stopBook.poke{gas: g}() {
            uint32 got = StopBookBookAt(stopBook, BTC, stopBook.logged() - 1);
            assertEq(got, want, "tuned gas changed the logged book");
        } catch {}
    }

    function StopBookBookAt(StopBook s, uint32 a, uint256 pos) internal view returns (uint32) {
        return s.bookAt(a, pos);
    }

    // ── P4: journal vs brute force near MAX_PRICE ────────────────

    function _val() internal returns (uint256) {
        uint256 r = _n(8);
        uint256 M = ab.MAX_PRICE();
        if (r == 0) return 0;
        if (r == 1) return M;
        if (r == 2) return M - 1 - _n(3);
        if (r == 3) return 1 + _n(3);
        if (r == 4) return (1 << 24) + _n(5) - 2;
        if (r == 5) return (1 << 31) + _n(1 << 16);
        return 1 + _n(M);
    }

    function testFuzz_a6_findEqualsBrute(uint256 seed) public {
        rnd = seed;
        uint32 a = 9;
        uint256 n = 1 + _n(300);
        uint256 base = _val();
        for (uint256 i = 0; i < n; ++i) {
            uint256 px = _n(3) == 0 ? _val() : base + _n(4) - (base > 2 ? 2 : 0);
            if (px > ab.MAX_PRICE()) px = ab.MAX_PRICE();
            ab.put(a, px, _n(4) == 0 ? type(uint32).max : _n(1 << 32));
        }
        for (uint256 t = 0; t < 12; ++t) {
            uint256 s = _n(n);
            uint256 lo = _n(3) == 0 ? 0 : _val();
            uint256 hi = _n(3) == 0 ? type(uint256).max : _val();
            if (hi == 0) hi = 1;
            uint256 want = n;
            for (uint256 i = s; i < n; ++i) {
                uint256 p = ab.pxAt(a, i);
                if ((p != 0 && p <= lo) || p >= hi) {
                    want = i;
                    break;
                }
            }
            assertEq(ab.find(a, s, n, lo, hi), want);
        }
    }

    function _worst(uint256 n, uint256 px) internal returns (uint256 gas, uint256 r) {
        vm.pauseGasMetering();
        A6Book b = new A6Book();
        b.bulk(7, n, px, 0);
        vm.cool(address(b));
        vm.resumeGasMetering();
        uint256 g0 = gasleft();
        r = b.find(7, 0, n, px - 1, type(uint256).max); // a stop level hugging the flat price, never touched
        gas = g0 - gasleft();
        vm.pauseGasMetering();
        r = r == n ? n : r;
        vm.resumeGasMetering();
    }

    function test_a6_worst_flat_gas() public {
        (uint256 g1, uint256 r1) = _worst(1000, (1 << 30) + 100);
        (uint256 g2, uint256 r2) = _worst(8000, (1 << 30) + 100);
        (uint256 g3, uint256 r3) = _worst(40000, (1 << 30) + 100);
        emit log_named_uint("gas n=1000", g1);
        emit log_named_uint("gas n=8000", g2);
        emit log_named_uint("gas n=40000", g3);
        assertEq(r1, 1000);
        assertEq(r2, 8000);
        assertEq(r3, 40000);
        // and the hit case: take level == price
        (uint256 g4,) = _worst(40000, ab.MAX_PRICE());
        emit log_named_uint("gas n=40000 at MAX_PRICE", g4);
    }

    function test_a6_levelBounds() public {
        address ex = address(new BadExam(ab, false));
        // only exams of the factory: the stand's poolOf says contracts are exams
        uint64 m = uint64(ab.MAX_PRICE());
        BadExam(ex).arm(5, true, m, 0);
        vm.expectRevert(abi.encodeWithSelector(StopBook.LevelTooHigh.selector, m + 1));
        BadExam(ex).arm(5, true, m + 1, 0);
    }

    /// the buy side (a short's stop above), partial sizes, the book around the touch on either side
    function testFuzz_a6_shortStopBuySide(uint64 touch, uint32 bOff, uint32 aOff, uint8 mode, uint8 part) public {
        uint64 sz = uint64(bound(part, 1, 100));
        touch = uint64(bound(touch, 1_050_000, 1_300_000));
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        PaperExam A = _examOn(one, true);
        PaperExam B = _examOn(one, true);
        _trade(A, BTC, false, 100);
        _trade(B, BTC, false, 100);
        _set(A, BTC, false, 1_050_000, sz);
        vm.roll(block.number + DELAY + 1);
        vm.prank(trader);
        B.place(BTC, true, sz);
        vm.roll(block.number + DELAY);
        _mark(touch);
        uint64 bid = uint64(uint256(touch) * (1_000_000 + 100_000 - bound(bOff, 0, 300_000)) / 1_000_000);
        uint64 ask = uint64(uint256(touch) * (1_000_000 - 100_000 + bound(aOff, 0, 300_000)) / 1_000_000);
        if (ask < bid && mode % 8 != 2) ask = bid + uint64(bound(aOff, 0, 5000));
        if (mode % 8 == 1) bid = 0;
        if (mode % 8 == 3) ask = 0;
        BookStub(BBO).set(BTC, bid, ask);
        vm.recordLogs();
        stopBook.poke();
        stopBook.settle(address(A));
        vm.prank(filler);
        try B.fill() {} catch {}
        _cmpBuy(vm.getRecordedLogs(), A, B, touch);
    }

    function _cmpBuy(Vm.Log[] memory L, PaperExam A, PaperExam B, uint64 touch) internal {
        (uint256 nA, uint64 pA, uint8 sA) = _logsPx(L, address(A));
        (uint256 nB, uint64 pB, uint8 sB) = _logsPx(L, address(B));
        assertEq(nA, 1);
        if (nB == 0) {
            assertEq(pA, touch + uint256(touch) * WORSE / 10_000, "refused order -> bound");
        } else {
            assertEq(sA, sB);
            assertLe(pA, pB, "buy: trigger never worse");
            assertLe(uint256(pB) - pA, uint256(touch) / 1e6 + 1);
        }
    }
}
