// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {AuditBook} from "./StopBookAudit7Model.t.sol";

/// The journal's tree against brute force: long runs of prices across deep run boundaries, zeros (no price), quiet
/// stretches, spikes; searches from any position to any end, at both kinds of level, also while the run is being logged.
contract StopBookAudit7TreeTest is Test {
    AuditBook book;
    uint256 rnd;
    uint256 constant NONE = type(uint256).max;

    function setUp() public {
        book = new AuditBook();
    }

    function _next(uint256 m) internal returns (uint256) {
        rnd = uint256(keccak256(abi.encode(rnd)));
        return rnd % m;
    }

    function _brute(uint32 a, uint256[] memory pxs, uint256 g0, uint256 s, uint256 len, uint256 lo, uint256 hi)
        internal
        pure
        returns (uint256)
    {
        a;
        for (uint256 q = s; q < len; ++q) {
            uint256 x = pxs[q - g0];
            if ((x != 0 && x <= lo) || x >= hi) return q;
        }
        return len;
    }

    function _query(uint32 a, uint256[] memory pxs, uint256 g0, uint256 gEnd, uint256 bound) internal {
        // gEnd: positions logged so far are [g0, gEnd).
        uint256 s = g0 + _next(gEnd - g0);
        uint256 len = s + 1 + _next(gEnd - s);
        uint256 lo;
        uint256 hi = NONE;
        uint256 kind = _next(3);
        uint256 pick = pxs[_next(gEnd - g0)];
        if (pick == 0) pick = 1000;
        if (kind == 0) lo = pick - _next(pick / 50 + 1);
        else if (kind == 1) hi = pick + _next(pick / 50 + 1);
        else {
            lo = pick - _next(pick / 20 + 1);
            hi = pick + _next(pick / 20 + 1);
            if (lo >= hi) hi = lo + 1;
        }
        bound;
        uint256 got = book.findRaw(a, s, len, lo, hi);
        uint256 want = _brute(a, pxs, g0, s, len, lo, hi);
        assertEq(got, want, "find vs brute force");
    }

    function _run(uint256 seed, uint32 a, uint256 count, uint256 r) internal {
        rnd = seed;
        uint256 off = book.offOf(a);
        // start r positions before a boundary of 4^12 (twice): runs of up to 12 levels close within `count`
        uint256 g0 = (1 << 25) - off - r;
        uint256[] memory pxs = new uint256[](count);
        uint256 base = 1_000_000;
        uint256 mode;
        for (uint256 i = 0; i < count; ++i) {
            if (i % 97 == 0) mode = _next(4);
            uint256 x;
            if (mode == 0) x = base;
            else if (mode == 1) x = base - 3000 + _next(6000);
            else if (mode == 2) x = _next(5) == 0 ? 0 : base + _next(200_000) - 100_000;
            else x = _next(40) == 0 ? base * (80 + _next(40)) / 100 : base;
            if (_next(31) == 0) x = 0;
            if (_next(500) == 0) x = type(uint32).max - 1;
            pxs[i] = x;
            book.feed(a, g0 + i, x == 0 ? 0 : (uint256(_next(1 << 31)) << 32 | x));
            if (_next(23) == 0) _query(a, pxs, g0, g0 + i + 1, 0);
        }
        for (uint256 j = 0; j < 40; ++j) {
            _query(a, pxs, g0, g0 + count, 0);
        }
        // searches that start at the very last positions and ends at the logging end
        for (uint256 j = 0; j < 20; ++j) {
            uint256 s = g0 + count - 1 - _next(40 > count ? count : 40);
            uint256 want = _brute(a, pxs, g0, s, g0 + count, 0, 0) ; // lo=0: nothing; hi=0 -> x>=0 always: s
            assertEq(book.findRaw(a, s, g0 + count, 0, 0), want);
        }
    }

    /// forge-config: default.fuzz.runs = 120
    function testFuzz_a7_findVsBruteForce(uint256 seed, uint32 a, uint16 r) public {
        a = uint32(bound(a, 1, 100_000));
        _run(seed, a, 1500 + seed % 1500, uint256(r) % 1200 + 1);
    }

    uint256[] internal sp_px;

    /// A quiet market with one spike (up or down) and a few missing prices: a search from any earlier position, to any end,
    /// finds the spike, or nothing if it is cut off by the end.
    /// forge-config: default.fuzz.runs = 150
    function testFuzz_a7_singleSpike(uint256 seed, uint32 a, uint16 r, uint16 spikeAt, bool up) public {
        a = uint32(bound(a, 1, 100_000));
        rnd = seed;
        uint256 g0 = (1 << 25) - book.offOf(a) - (uint256(r) % 1300 + 1);
        for (uint256 i = 0; i < 2600; ++i) {
            uint256 x = 1_000_000;
            if (i == uint256(spikeAt) % 2600) x = up ? 1_300_000 : 700_000;
            else if (_next(50) == 0) x = 0;
            sp_px.push(x);
            book.feed(a, g0 + i, x == 0 ? 0 : (uint256(1) << 32 | x));
        }
        for (uint256 j = 0; j < 60; ++j) {
            uint256 s = g0 + _next(2600);
            uint256 len = s + 1 + _next(g0 + 2600 - s);
            assertEq(
                book.findRaw(a, s, len, up ? 0 : 800_000, up ? 1_200_000 : NONE),
                _brute(a, sp_px, g0, s, len, up ? 0 : 800_000, up ? 1_200_000 : NONE)
            );
        }
    }
}
