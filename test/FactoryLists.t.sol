// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {Pool} from "../src/Pool.sol";
import {PoolHarness} from "./PoolHarness.sol";

/// The audit's K-1: the keeper read every pool the factory made in one call, and anyone creates
/// pools for free. The factory keeps the pools that have sold a challenge apart, each once, in the
/// order of their first, and gives both lists out in pages.
contract FactoryListsTest is PoolHarness {
    function _list(address[] memory a) internal pure returns (bytes32) {
        return keccak256(abi.encode(a));
    }

    function test_K1_onlyPoolsThatSoldAChallenge_eachOnce_inTheOrderOfTheirFirst() public {
        Pool a = _readyPool();
        Pool b = _readyPool();
        Pool c = _readyPool();
        assertEq(factory.poolCount(), 3);
        assertEq(factory.challengedCount(), 0, "pools alone are not on it");
        _buy(b);
        assertEq(factory.challengedCount(), 1);
        assertTrue(factory.hasChallenged(address(b)));
        assertFalse(factory.hasChallenged(address(a)));
        CoreSimulatorLib.nextBlock();
        _buy(a);
        assertEq(factory.challengedCount(), 2);
        address[] memory want = new address[](2);
        want[0] = address(b);
        want[1] = address(a);
        assertEq(_list(factory.challengedFrom(0, 10)), _list(want), "b first, then a; c never");
        assertFalse(factory.hasChallenged(address(c)));
    }

    function test_K1_aPoolSellingAgainIsNotListedTwice() public {
        Pool p = _readyPool();
        _buy(p);
        assertEq(factory.challengedCount(), 1);
        // A second challenge of the same pool adds nothing.
        vm.prank(address(p));
        factory.createChallenge(trader);
        assertEq(factory.challengedCount(), 1);
        assertEq(factory.challengedFrom(0, 1)[0], address(p));
        assertEq(factory.challengedFrom(1, 5).length, 0);
    }

    function test_K1_pagesAreClippedAtTheEnd_andEmptyPastIt() public {
        Pool[] memory ps = new Pool[](3);
        for (uint256 i = 0; i < 3; ++i) {
            ps[i] = _readyPool();
            _buy(ps[i]);
            CoreSimulatorLib.nextBlock();
        }
        _readyPool(); // one that sold none: on the factory's list, not on the challenged one
        address[] memory two = factory.challengedFrom(1, 5);
        assertEq(two.length, 2);
        assertEq(two[0], address(ps[1]));
        assertEq(two[1], address(ps[2]));
        assertEq(factory.challengedFrom(3, 1).length, 0);
        assertEq(factory.challengedFrom(type(uint256).max, type(uint256).max).length, 0);
        assertEq(factory.challengedFrom(0, 0).length, 0);
        address[] memory one = factory.poolsFrom(2, 1);
        assertEq(one.length, 1);
        assertEq(one[0], address(ps[2]));
        assertEq(factory.poolsFrom(0, type(uint256).max).length, 4);
        assertEq(factory.poolCount(), 4);
        assertEq(factory.challengedCount(), 3);
        assertEq(factory.poolsFrom(5, 1).length, 0);
    }
}
