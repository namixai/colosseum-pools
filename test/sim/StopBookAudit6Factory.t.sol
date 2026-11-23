// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {PaperExam} from "../../src/exam/PaperExam.sol";
import {SimPool} from "../../src/sim/SimPool.sol";
import {SimPoolFactory} from "../../src/sim/SimPoolFactory.sol";

/// The sixth audit's model of the listed count: 150 random listings and delistings against a count kept aside.
contract StopBookAudit6FactoryTest is Test {
    function testFuzz_a6_listedCountModel(uint256 seed) public {
        SimPoolFactory f = new SimPoolFactory(
            IERC20(address(0xBEEF)), address(new PaperExam()), address(new SimPool()), address(this), 1 ether
        );
        uint256 model;
        bool[40] memory on;
        uint256 r = seed;
        for (uint256 i = 0; i < 150; ++i) {
            r = uint256(keccak256(abi.encode(r)));
            uint32 a = uint32(r % 40);
            uint32 cap = (r >> 8) % 3 == 0 ? 0 : uint32(100 + (r >> 16) % 1900);
            bool bad = cap != 0 && (cap < 100 || cap > f.MAX_LEVERAGE_X100());
            if (bad) {
                vm.expectRevert();
                f.setAsset(a, cap);
                continue;
            }
            bool willRevert = !on[a] && cap != 0 && model == f.MAX_LISTED();
            if (willRevert) {
                vm.expectRevert(SimPoolFactory.TooManyAssets.selector);
                f.setAsset(a, cap);
            } else {
                f.setAsset(a, cap);
                if (!on[a] && cap != 0) {
                    on[a] = true;
                    ++model;
                } else if (on[a] && cap == 0) {
                    on[a] = false;
                    --model;
                }
            }
            assertEq(f.listedAssets(), model);
            assertEq(f.assetCapX100(a) != 0, on[a]);
        }
    }
}
