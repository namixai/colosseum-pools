// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {StopBookModelTest} from "./StopBookModel.t.sol";

/// The fifth audit's shape of the reference model (22 November 2026): 16 exams, 6 assets, 300 steps, books on, delay 1..9, a deadline
/// that falls in the run half the time.
contract StopBookAudit5ModelTest is StopBookModelTest {
    /// forge-config: default.fuzz.runs = 1500
    function testFuzz_a5_long(uint256 seed) public {
        (n, na, steps, dl, stepDuration, books) =
            (16, 6, 300, uint16(1 + seed % 9), (seed >> 20) % 2 == 0 ? 7 days : uint32(60 + (seed >> 30) % 400), true);
        _init(seed);
        _run();
    }
}
