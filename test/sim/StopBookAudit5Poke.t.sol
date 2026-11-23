// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PaperExam} from "../../src/exam/PaperExam.sol";
import {BookStub} from "../exam/PaperExamInvariant.t.sol";
import {StopBookBase} from "./StopBook.t.sol";

/// The fifth audit's measure of a check against the number of assets it logs (M2): it grows by each asset, so the
/// factory lists at most `SimPoolFactory.MAX_LISTED` (20) at once; past that, as here, a check outgrows a small block.
contract StopBookAudit5PokeTest is StopBookBase {
    function _cool() internal {
        vm.cool(address(stopBook));
        vm.cool(ORACLE);
        vm.cool(address(hyperCore));
    }

    /// poke gas as the number of watched assets grows: 16, 48, 96 assets (6 exams of 16 assets), one stop each.
    function test_a5_pokeGas_byWatchedAssets() public {
        vm.pauseGasMetering();
        uint256[3] memory counts = [uint256(16), 48, 96];
        uint256 made_;
        for (uint256 c = 0; c < 3; ++c) {
            while (made_ < counts[c]) {
                uint32[] memory as_ = new uint32[](16);
                for (uint256 k = 0; k < 16; ++k) {
                    as_[k] = uint32(1000 + made_ + k);
                    _asset(as_[k], 5, MARK);
                    BookStub(BBO).set(as_[k], MARK - 10, MARK + 10);
                }
                PaperExam e = _examOn(as_, false);
                for (uint256 k = 0; k < 16; ++k) {
                    _trade(e, as_[k], true, 5);
                    _set(e, as_[k], false, 900_000, 0);
                }
                made_ += 16;
            }
            assertEq(stopBook.watched().length, counts[c]);
            uint256 sum;
            uint256 most;
            for (uint256 b = 0; b < 40; ++b) {
                vm.roll(block.number + 1);
                _cool();
                vm.resumeGasMetering();
                uint256 g0 = gasleft();
                stopBook.poke();
                uint256 used = g0 - gasleft();
                vm.pauseGasMetering();
                sum += used;
                if (used > most) most = used;
            }
            emit log_named_uint("watched assets", counts[c]);
            emit log_named_uint("  poke avg of 40", sum / 40);
            emit log_named_uint("  poke max of 40", most);
        }
    }
}
