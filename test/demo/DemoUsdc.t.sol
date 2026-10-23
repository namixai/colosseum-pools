// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {DemoUsdc} from "../../src/demo/DemoUsdc.sol";

/// The testnet demo's own USDC: the operator mints any amount, the keeper's faucet up to a daily
/// limit, nobody else; it is never deployed on mainnet.
contract DemoUsdcTest is Test {
    address operator = makeAddr("operator");
    address faucet = makeAddr("faucet");
    address trader = makeAddr("trader");
    address stranger = makeAddr("stranger");
    DemoUsdc token;

    function setUp() public {
        vm.chainId(998);
        token = new DemoUsdc(operator);
        vm.prank(operator);
        token.setFaucet(faucet, 100e6);
    }

    function test_demoUsdc_sixDecimals_andTheOperatorMintsAnyAmount() public {
        assertEq(token.decimals(), 6);
        vm.prank(operator);
        token.mint(trader, 1_000_000e6);
        assertEq(token.balanceOf(trader), 1_000_000e6);
    }

    function test_demoUsdc_theFaucetMintsUpToItsDailyLimit_andAgainTheNextDay() public {
        vm.warp(20_000 days + 1 hours);
        vm.startPrank(faucet);
        token.mint(trader, 60e6);
        token.mint(stranger, 40e6);
        vm.expectRevert(abi.encodeWithSelector(DemoUsdc.OverDaily.selector, 0));
        token.mint(trader, 1);
        vm.warp(20_001 days);
        token.mint(trader, 100e6);
        vm.stopPrank();
        assertEq(token.balanceOf(trader), 160e6);
        assertEq(token.faucetMinted(20_000), 100e6);
        assertEq(token.faucetMinted(20_001), 100e6);
    }

    function test_demoUsdc_aLimitLoweredBelowTheDaysMintsLeavesNothing() public {
        vm.prank(faucet);
        token.mint(trader, 80e6);
        vm.prank(operator);
        token.setFaucet(faucet, 50e6);
        vm.prank(faucet);
        vm.expectRevert(abi.encodeWithSelector(DemoUsdc.OverDaily.selector, 0));
        token.mint(trader, 1);
        vm.prank(operator);
        token.setFaucet(faucet, 100e6);
        vm.prank(faucet);
        vm.expectRevert(abi.encodeWithSelector(DemoUsdc.OverDaily.selector, 20e6));
        token.mint(trader, 21e6);
    }

    function test_demoUsdc_nobodyElseMints_andOnlyTheOperatorSetsTheFaucetOrHandsOver() public {
        vm.prank(stranger);
        vm.expectRevert(DemoUsdc.NotMinter.selector);
        token.mint(stranger, 1);
        vm.prank(stranger);
        vm.expectRevert(DemoUsdc.NotOperator.selector);
        token.setFaucet(stranger, type(uint256).max);
        vm.prank(stranger);
        vm.expectRevert(DemoUsdc.NotOperator.selector);
        token.setOperator(stranger);
        vm.prank(operator);
        token.setOperator(stranger);
        vm.prank(operator);
        vm.expectRevert(DemoUsdc.NotMinter.selector);
        token.mint(operator, 1);
        vm.prank(stranger);
        token.mint(stranger, 5e6);
        assertEq(token.balanceOf(stranger), 5e6);
    }

    function test_demoUsdc_isNeverDeployedOnMainnet() public {
        vm.chainId(999);
        vm.expectRevert(DemoUsdc.NotOnMainnet.selector);
        new DemoUsdc(operator);
    }
}
