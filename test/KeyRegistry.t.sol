// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {KeyRegistry, IAccountSource} from "../src/KeyRegistry.sol";

contract FakeSource is IAccountSource {
    mapping(address => bool) public isAccount;

    function set(address a, bool v) external {
        isAccount[a] = v;
    }
}

contract Caller {
    function assign(KeyRegistry r, address trader) external returns (address) {
        return r.assign(trader);
    }

    function retire(KeyRegistry r, address key) external {
        r.retire(key);
    }
}

contract KeyRegistryTest is Test {
    HyperCore hyperCore;
    KeyRegistry registry;
    FakeSource source;
    Caller accountA;
    Caller accountB;

    address operator = makeAddr("operator");
    address trader = makeAddr("trader");
    address k1 = makeAddr("k1");
    address k2 = makeAddr("k2");

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        registry = new KeyRegistry(operator);
        source = new FakeSource();
        accountA = new Caller();
        accountB = new Caller();
        source.set(address(accountA), true);
        source.set(address(accountB), true);
        vm.prank(operator);
        registry.setAccountSource(source);
    }

    function _publish(address a, address b) internal {
        address[] memory keys = new address[](2);
        keys[0] = a;
        keys[1] = b;
        vm.prank(operator);
        registry.publish(keys);
    }

    function test_publish_onlyOperator() public {
        address[] memory keys = new address[](1);
        keys[0] = k1;
        vm.expectRevert(KeyRegistry.NotOperator.selector);
        registry.publish(keys);
    }

    function test_publish_refusesZeroDuplicateAndCoreUsers() public {
        address[] memory keys = new address[](1);

        keys[0] = address(0);
        vm.prank(operator);
        vm.expectRevert(KeyRegistry.ZeroAddress.selector);
        registry.publish(keys);

        keys[0] = k1;
        vm.prank(operator);
        registry.publish(keys);
        vm.prank(operator);
        vm.expectRevert(abi.encodeWithSelector(KeyRegistry.KeyNotNew.selector, k1));
        registry.publish(keys);

        // An address HyperCore already knows is a user, not a fresh agent key.
        CoreSimulatorLib.forceAccountActivation(k2);
        keys[0] = k2;
        vm.prank(operator);
        vm.expectRevert(abi.encodeWithSelector(KeyRegistry.KeyExistsOnCore.selector, k2));
        registry.publish(keys);
    }

    function test_accountSource_isSetOnce() public {
        vm.prank(operator);
        vm.expectRevert(KeyRegistry.SourceAlreadySet.selector);
        registry.setAccountSource(source);
    }

    function test_assign_onlyAccounts_oneKeyEach() public {
        _publish(k1, k2);

        vm.expectRevert(abi.encodeWithSelector(KeyRegistry.NotAnAccount.selector, address(this)));
        registry.assign(trader);

        address got = accountA.assign(registry, trader);
        assertEq(got, k2, "last published is handed out first");
        assertEq(registry.keyOf(address(accountA)), k2);
        assertTrue(registry.isBound(k2, address(accountA), trader));
        assertFalse(registry.isBound(k2, address(accountB), trader));
        assertFalse(registry.isBound(k2, address(accountA), address(0xBEEF)));
        assertEq(registry.freeCount(), 1);

        vm.expectRevert(abi.encodeWithSelector(KeyRegistry.AccountHasKey.selector, address(accountA), k2));
        accountA.assign(registry, trader);

        assertEq(accountB.assign(registry, trader), k1);
        Caller accountC = new Caller();
        source.set(address(accountC), true);
        vm.expectRevert(KeyRegistry.NoFreeKey.selector);
        accountC.assign(registry, trader);
    }

    function test_retire_isFinal() public {
        _publish(k1, k2);
        address key = accountA.assign(registry, trader);

        vm.expectRevert(abi.encodeWithSelector(KeyRegistry.NotBoundHere.selector, key, address(accountB)));
        accountB.retire(registry, key);

        accountA.retire(registry, key);
        assertEq(uint8(registry.bindingOf(key).state), uint8(KeyRegistry.State.Retired));
        assertEq(registry.keyOf(address(accountA)), address(0));
        assertFalse(registry.isBound(key, address(accountA), trader));

        vm.expectRevert(abi.encodeWithSelector(KeyRegistry.NotBoundHere.selector, key, address(accountA)));
        accountA.retire(registry, key);

        // A retired key can never be published again.
        address[] memory keys = new address[](1);
        keys[0] = key;
        vm.prank(operator);
        vm.expectRevert(abi.encodeWithSelector(KeyRegistry.KeyNotNew.selector, key));
        registry.publish(keys);

        // The account can take a new one; the free list never returns the old key.
        address next = accountA.assign(registry, trader);
        assertTrue(next != key);
    }

    function test_operatorTransfer_isTwoStep() public {
        address next = makeAddr("next");
        vm.prank(operator);
        registry.transferOperator(next);
        assertEq(registry.operator(), operator);
        vm.expectRevert(KeyRegistry.NotPendingOperator.selector);
        registry.acceptOperator();
        vm.prank(next);
        registry.acceptOperator();
        assertEq(registry.operator(), next);
    }
}
