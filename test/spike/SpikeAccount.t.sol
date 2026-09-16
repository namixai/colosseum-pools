// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";
import {SpikeAccount} from "../../src/spike/SpikeAccount.sol";

contract MockUsdc is ERC20 {
    constructor() ERC20("USDC", "USDC") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }
}

/// What this file can and cannot tell us.
///
/// The hyper-evm-lib simulator (pinned at 4eb7ab0) executes actions 1, 2, 3, 4, 5, 6, 7 and
/// 13. It silently ignores 9 (add API wallet), 10 and 11 (cancels) and 12 (builder fee): no
/// state changes and no revert. So for those four, a green test here proves only that our
/// contract sends the right bytes to CoreWriter. What HyperCore does with them is a question
/// for the live testnet, and `spike/README.md` records those runs.
///
/// Transfers are modelled, but the model is the library's, not Hyperliquid's: it charges a
/// 1 USDC activation fee to the sender of the first spotSend into a new account. The live
/// fee is one of the spike questions.
contract SpikeAccountTest is Test {
    event RawAction(address indexed user, bytes data);

    address constant CORE_WRITER = 0x3333333333333333333333333333333333333333;
    uint64 constant USDC = 0;

    HyperCore hyperCore;
    SpikeAccount pool;
    SpikeAccount challenge;

    address owner = makeAddr("owner");
    address trader = makeAddr("trader");
    address agentA = makeAddr("agentA");

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        CoreSimulatorLib.setRevertOnFailure(true);

        pool = new SpikeAccount(owner);
        challenge = new SpikeAccount(owner);
    }

    // ── bytes sent to CoreWriter ─────────────────────────────────────────────────────

    function _expectRaw(address from, uint24 kind, bytes memory args) internal {
        vm.expectEmit(true, false, false, true, CORE_WRITER);
        emit RawAction(from, abi.encodePacked(uint8(1), kind, args));
    }

    function test_addApiWallet_sendsAction9() public {
        _expectRaw(address(pool), 9, abi.encode(agentA, ""));
        vm.prank(owner);
        pool.addApiWallet(agentA, "");
    }

    function test_addApiWallet_namedSlot_sendsAction9() public {
        _expectRaw(address(pool), 9, abi.encode(agentA, "trader-1"));
        vm.prank(owner);
        pool.addApiWallet(agentA, "trader-1");
    }

    function test_cancelByOid_sendsAction10() public {
        _expectRaw(address(pool), 10, abi.encode(uint32(3), uint64(123456789)));
        vm.prank(owner);
        pool.cancelByOid(3, 123456789);
    }

    function test_cancelByCloid_sendsAction11() public {
        uint128 cloid = uint128(0x0123456789abcdef0123456789abcdef);
        _expectRaw(address(pool), 11, abi.encode(uint32(3), cloid));
        vm.prank(owner);
        pool.cancelByCloid(3, cloid);
    }

    function test_approveBuilderFee_sendsAction12() public {
        _expectRaw(address(pool), 12, abi.encode(uint64(10), owner));
        vm.prank(owner);
        pool.approveBuilderFee(10, owner);
    }

    /// Action 16 targets the account itself; the value 1 means separate spot and perp
    /// balances. The tail is written out by hand.
    function test_setAbstraction_sendsAction16() public {
        vm.expectEmit(true, false, false, true, CORE_WRITER);
        emit RawAction(
            address(pool),
            bytes.concat(
                hex"01000010",
                bytes32(uint256(uint160(address(pool)))),
                hex"0000000000000000000000000000000000000000000000000000000000000001"
            )
        );
        vm.prank(owner);
        pool.setAbstraction(1);
    }

    function test_limitOrder_sendsAction1_withReduceOnlyAndIoc() public {
        _expectRaw(
            address(pool),
            1,
            abi.encode(uint32(3), false, uint64(60_000e8), uint64(0.001e8), true, uint8(3), uint128(7))
        );
        vm.prank(owner);
        pool.placeLimitOrder(3, false, 60_000e8, 0.001e8, true, 3, 7);
    }

    /// The tests above build the expected bytes with abi.encode, the same way the library
    /// does, so a shared mistake would cancel out. These two vectors are written out by hand
    /// from the ABI spec and the CoreWriter docs (version byte 0x01, then the action id as
    /// three big-endian bytes, then the ABI-encoded fields), with no encoder involved.
    function test_vector_action9_literal() public {
        address agent = address(0xA1);
        vm.expectEmit(true, false, false, true, CORE_WRITER);
        emit RawAction(
            address(pool),
            hex"01000009"
            hex"00000000000000000000000000000000000000000000000000000000000000a1"
            hex"0000000000000000000000000000000000000000000000000000000000000040"
            hex"0000000000000000000000000000000000000000000000000000000000000008"
            hex"7472616465722d31000000000000000000000000000000000000000000000000"
        );
        vm.prank(owner);
        pool.addApiWallet(agent, "trader-1");
    }

    function test_vector_action10_literal() public {
        vm.expectEmit(true, false, false, true, CORE_WRITER);
        emit RawAction(
            address(pool),
            hex"0100000a"
            hex"0000000000000000000000000000000000000000000000000000000000000003"
            hex"00000000000000000000000000000000000000000000000000000000075bcd15"
        );
        vm.prank(owner);
        pool.cancelByOid(3, 123456789);
    }

    /// The simulator does not model API wallets at all. Pinning that here means a future
    /// library upgrade that starts modelling them turns this test red and gets noticed,
    /// instead of silently changing what our other tests mean.
    function test_simulatorIgnoresAction9() public {
        CoreSimulatorLib.forceAccountActivation(address(pool));
        CoreSimulatorLib.forceSpotBalance(address(pool), USDC, 50e8);
        vm.prank(owner);
        pool.addApiWallet(agentA, "");
        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.spotBalance(address(pool), USDC).total, 50e8, "no side effect expected");
    }

    function test_onlyOwner() public {
        vm.expectRevert(SpikeAccount.NotOwner.selector);
        pool.addApiWallet(agentA, "");
        vm.expectRevert(SpikeAccount.NotOwner.selector);
        pool.spotSend(address(challenge), USDC, 1e8);
        vm.expectRevert(SpikeAccount.NotOwner.selector);
        pool.cancelByOid(3, 1);
    }

    // ── pool -> challenge -> pool, as the library models it ─────────────────────────

    function test_poolToChallengeAndBack_underSimulatorModel() public {
        CoreSimulatorLib.forceAccountActivation(address(pool));
        CoreSimulatorLib.forceSpotBalance(address(pool), USDC, 1000e8);
        assertFalse(PrecompileLib.coreUserExists(address(challenge)), "challenge starts without a Core account");

        vm.prank(owner);
        pool.spotSend(address(challenge), USDC, 100e8);

        // Nothing moves inside the same block: the action is only queued.
        assertEq(PrecompileLib.spotBalance(address(pool), USDC).total, 1000e8);
        CoreSimulatorLib.nextBlock();

        assertEq(PrecompileLib.spotBalance(address(challenge), USDC).total, 100e8);
        // The model charges its 1 USDC activation fee to the sender.
        assertEq(PrecompileLib.spotBalance(address(pool), USDC).total, 899e8);

        // Challenge capital goes to perp margin and back.
        vm.prank(owner);
        challenge.usdClassTransfer(100e6, true);
        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.spotBalance(address(challenge), USDC).total, 0);
        assertEq(challenge.marginSummary(address(challenge)).accountValue, int64(100e6));

        vm.prank(owner);
        challenge.usdClassTransfer(100e6, false);
        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.spotBalance(address(challenge), USDC).total, 100e8);

        vm.prank(owner);
        challenge.spotSend(address(pool), USDC, 100e8);
        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.spotBalance(address(challenge), USDC).total, 0);
        assertEq(PrecompileLib.spotBalance(address(pool), USDC).total, 999e8);
    }

    /// An action from an account that does not exist on HyperCore does nothing, and the EVM
    /// call still succeeds. The real contracts must check `coreUserExists` themselves.
    function test_actionFromInactiveAccount_isSilentNoOp() public {
        CoreSimulatorLib.setRevertOnFailure(false);
        vm.prank(owner);
        challenge.usdClassTransfer(1e6, true);
        CoreSimulatorLib.nextBlock();
        assertFalse(PrecompileLib.coreUserExists(address(challenge)));
    }

    // ── equity read (spike question 4) ───────────────────────────────────────────────

    function test_equityComesFromAccountMarginSummary() public {
        CoreSimulatorLib.forcePerpBalance(address(challenge), 250e6);
        PrecompileLib.AccountMarginSummary memory ms = pool.marginSummary(address(challenge));
        assertEq(ms.accountValue, int64(250e6));
        assertEq(ms.marginUsed, 0);
        assertEq(ms.ntlPos, 0);
    }

    // ── payment on HyperEVM (spike question 5) ───────────────────────────────────────

    function test_payIsAttributedToTheCaller() public {
        MockUsdc impl = new MockUsdc();
        address usdc = HLConstants.usdc();
        vm.etch(usdc, address(impl).code);
        deal(usdc, trader, 25e6);

        vm.startPrank(trader);
        MockUsdc(usdc).approve(address(pool), 25e6);
        vm.expectEmit(true, false, false, true, address(pool));
        emit SpikeAccount.Paid(trader, 25e6);
        pool.pay(25e6);
        vm.stopPrank();

        assertEq(pool.paidBy(trader), 25e6);
        assertEq(MockUsdc(usdc).balanceOf(address(pool)), 25e6);
    }

    function test_payZeroReverts() public {
        vm.expectRevert(SpikeAccount.ZeroAmount.selector);
        vm.prank(trader);
        pool.pay(0);
    }
}
