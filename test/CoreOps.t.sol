// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {CoreOps} from "../src/lib/CoreOps.sol";

contract RoundHarness {
    function round(uint256 px, uint8 decimals, bool up) external pure returns (uint256) {
        return CoreOps.roundPerpPx(px, decimals, up);
    }
}

/// Price rounding against Hyperliquid's published rule: "Prices can have up to 5 significant
/// figures, but an integer price is always allowed, regardless of the number of significant
/// figures", with at most 6 - szDecimals decimals for perps. Inputs are in the mark
/// precompile's format: an integer with `decimals` = 6 - szDecimals decimal places.
/// Every expected value below is worked out by hand from that rule, not by the code.
contract CoreOpsRoundingTest is Test {
    RoundHarness h = new RoundHarness();

    function test_btcMark_isAlreadyValid() public view {
        // BTC, szDecimals 5 -> 1 decimal. 76400.0: integer part has 5 digits.
        assertEq(h.round(764000, 1, false), 764000);
        assertEq(h.round(764000, 1, true), 764000);
    }

    function test_btc_integerPartOfFiveDigits_dropsTheDecimal() public view {
        // 76412.3 -> 76412.0 down, 76413.0 up
        assertEq(h.round(764123, 1, false), 764120);
        assertEq(h.round(764123, 1, true), 764130);
    }

    function test_docsExample_12345point6_isNotValid() public view {
        // The docs name 12345.6 as invalid and 123456.0 as valid.
        assertEq(h.round(123456, 1, false), 123450);
        assertEq(h.round(1234560, 1, false), 1234560);
    }

    function test_eth_fourIntegerDigits_keepsFiveSignificant() public view {
        // ETH, szDecimals 4 -> 2 decimals. 2413.71 -> 2413.70 / 2413.80
        assertEq(h.round(241371, 2, false), 241370);
        assertEq(h.round(241371, 2, true), 241380);
    }

    function test_roundingUpCanCarryIntoANewDigit() public view {
        // 9999.95 up -> 10000.00, an integer, valid.
        assertEq(h.round(999995, 2, true), 1000000);
        assertEq(h.round(999995, 2, false), 999990);
    }

    function test_smallPrices() public view {
        // HYPE-like, 4 decimals: 26.0000 has 6 digits, the zeros make it valid already.
        assertEq(h.round(260000, 4, false), 260000);
        // 0.123456 with 6 decimals -> 0.12345
        assertEq(h.round(123456, 6, false), 123450);
        // 0.012345 is five significant figures already.
        assertEq(h.round(12345, 6, false), 12345);
        assertEq(h.round(1, 3, false), 1);
        assertEq(h.round(0, 1, true), 0);
    }

    function test_largeIntegerPart_dropsAllDecimals() public view {
        // 1234567.89 -> 1234567.00
        assertEq(h.round(123456789, 2, false), 123456700);
        assertEq(h.round(123456789, 2, true), 123456800);
    }

    function testFuzz_roundingStaysCloseAndInDirection(uint64 px, uint8 decimals, bool up) public view {
        decimals = uint8(bound(decimals, 0, 6));
        uint256 r = h.round(px, decimals, up);
        if (up) assertGe(r, px);
        else assertLe(r, px);

        // Distance is below one unit of the digit that was dropped.
        uint256 d = r > px ? r - px : px - r;
        uint256 digits = _digits(px);
        uint256 unit = digits > 5 ? 10 ** (digits - 5) : 1;
        uint256 decUnit = 10 ** uint256(decimals);
        assertLt(d, unit > decUnit ? unit : decUnit);

        // The result obeys the rule: an integer, or at most five significant figures.
        if (r != 0) {
            bool isInteger = r % decUnit == 0;
            assertTrue(isInteger || _significant(r) <= 5, "rule broken");
        }
    }

    function _digits(uint256 x) internal pure returns (uint256 n) {
        while (x != 0) {
            x /= 10;
            ++n;
        }
    }

    function _significant(uint256 x) internal pure returns (uint256) {
        while (x != 0 && x % 10 == 0) x /= 10;
        return _digits(x);
    }
}
