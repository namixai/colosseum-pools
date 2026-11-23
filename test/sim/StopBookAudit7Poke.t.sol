// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PaperExam} from "../../src/exam/PaperExam.sol";
import {StopBookBase} from "../sim/StopBook.t.sol";

/// A book precompile that burns the gas it is given (a failing precompile on a real chain may do the same).
contract Burner {
    fallback() external {
        while (true) {}
    }
}

/// The seventh independent audit of cp#335 (23 November 2026, head 9ee58e92), its M1: the check at the cap of assets
/// when every read of the book precompile burns all the gas it gets. With the reads at 15 000 each (`StopBook.bboGas`)
/// and the cap at 16 (`SimPoolFactory.MAX_LISTED`), the dearest check, every asset closing runs on eleven levels, stays
/// under 1 500 000 (on 9ee58e92, 50 000 a read and 20 assets: 1 644 756 typical, 2 242 969 deepest).
contract StopBookAudit7PokeTest is StopBookBase {
    uint256 constant CAP = 16; // SimPoolFactory.MAX_LISTED

    function _twenty() internal returns (uint32[] memory as_) {
        as_ = new uint32[](CAP);
        for (uint256 k = 0; k < CAP; ++k) {
            as_[k] = uint32(400 + k);
            _asset(as_[k], 5, MARK);
            uint32[] memory one = new uint32[](1);
            one[0] = as_[k];
            PaperExam e = _examOn(one, false);
            _trade(e, as_[k], true, 20);
            _set(e, as_[k], false, 900_000, 0);
        }
    }

    function _cool() internal {
        vm.cool(address(stopBook));
        vm.cool(ORACLE);
        vm.cool(BBO);
        vm.cool(address(hyperCore));
    }

    function test_a7_poke_atTheCap_bookReadBurnsItsGas() public {
        uint32[] memory as_ = _twenty();
        vm.etch(BBO, type(Burner).runtimeCode);
        uint256 most;
        uint256 deep;
        for (uint256 b = 0; b < 8; ++b) {
            vm.roll(block.number + 1);
            for (uint256 k = 0; k < CAP; ++k) {
                CoreSimulatorLib.setMarkPx(as_[k], uint64(MARK - 5_000 + ((b * 7 + k * 3) % 11) * 1_000));
            }
            _cool();
            uint256 g0 = gasleft();
            stopBook.poke();
            uint256 used = g0 - gasleft();
            if (used > most) most = used;
        }
        for (uint256 k = 0; k < CAP; ++k) {
            uint256 span = 1 << 22;
            uint256 g = ((stopBook.logged() + _harness().off(as_[k]) + 1 + span - 1) / span) * span - 1 - _harness().off(as_[k]);
            _harness().skip(g - stopBook.logged());
            vm.roll(block.number + 1);
            _cool();
            uint256 g0 = gasleft();
            stopBook.poke();
            uint256 used = g0 - gasleft();
            if (used > deep) deep = used;
        }
        emit log_named_uint("poke, 16 assets, every book read burns its 15 000: most of 8 checks", most);
        emit log_named_uint("poke, 16 assets, every book read burns its 15 000: deepest closing", deep);
        assertLe(most, 1_500_000);
        assertLe(deep, 1_500_000, "the dearest check fits a small block with a quarter to spare");
    }

    /// The trader's first call in a block at the cap: 16 assets of its own with 32 entries, 4 more assets logged for others,
    /// the dearest check (every asset closing runs), the settle of 32 entries since the last block, and the order.
    function test_a7_firstTraderCall_atTheCap() public {
        uint32[] memory mine = new uint32[](16);
        for (uint256 k = 0; k < 16; ++k) {
            mine[k] = uint32(200 + k);
            _asset(mine[k], 5, MARK);
        }
        PaperExam e = _examOn(mine, false);
        for (uint256 k = 0; k < 16; ++k) {
            _trade(e, mine[k], true, 20);
        }
        for (uint256 k = 0; k < 16; ++k) {
            _set(e, mine[k], false, 900_000, 0);
            _set(e, mine[k], true, 1_100_000, 0);
        }
        assertEq(stopBook.watched().length, CAP);
        vm.roll(block.number + 3);
        stopBook.settle(address(e));
        uint256 worst;
        for (uint256 k = 0; k < CAP; ++k) {
            uint32 a = mine[k];
            uint256 span = 1 << 22;
            uint256 off = _harness().off(a);
            uint256 g = ((stopBook.logged() + off + 1 + span - 1) / span) * span - 1 - off;
            _harness().skip(g - 1 - stopBook.logged());
            vm.roll(block.number + 1);
            stopBook.settle(address(e)); // the exam is settled up to the block before (the executor does it each block)
            vm.roll(block.number + 1);
            _cool();
            vm.cool(address(e));
            vm.prank(trader);
            uint256 g0 = gasleft();
            e.place(mine[k % 16], true, 1);
            uint256 used = g0 - gasleft();
            if (used > worst) worst = used;
            vm.roll(block.number + 5);
            stopBook.settle(address(e));
        }
        emit log_named_uint("trader's first call in a block, 16 assets, dearest of 16 closing blocks", worst);
        assertLe(worst, 1_500_000);
    }
}
