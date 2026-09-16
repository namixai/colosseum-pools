// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {CoreWriterLib} from "@hyper-evm-lib/src/CoreWriterLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";

/// @title SpikeAccount
/// @notice Spike harness, not product code. It gives one owner a direct handle on each
///         CoreWriter action the pool design depends on, so every action can be observed on
///         Hyperliquid testnet before the real contracts are written around it.
/// @dev Every action is fire-and-forget: CoreWriter only emits a log, HyperCore executes it
///      a few seconds later, and a failure there does not revert here. Read the effect from
///      the info API or from a precompile in a later block.
contract SpikeAccount {
    using SafeERC20 for IERC20;

    address public immutable owner;

    /// @notice USDC pulled from each payer through `pay`. The point of the spike question
    ///         "how does the trader pay": a HyperEVM payment is attributable to msg.sender,
    ///         a HyperCore transfer into this account is not visible to the contract at all.
    mapping(address payer => uint256 amount) public paidBy;

    event Paid(address indexed payer, uint256 amount);

    error NotOwner();
    error ZeroAmount();

    constructor(address owner_) {
        owner = owner_;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ── CoreWriter actions ───────────────────────────────────────────────────────────

    /// @notice Action 9. An empty name targets the unnamed ("main") API wallet.
    function addApiWallet(address wallet, string calldata name) external onlyOwner {
        CoreWriterLib.addApiWallet(wallet, name);
    }

    /// @notice Action 1. Prices and sizes are 1e8-scaled; tif 1 = ALO, 2 = GTC, 3 = IOC.
    function placeLimitOrder(
        uint32 asset,
        bool isBuy,
        uint64 limitPx,
        uint64 sz,
        bool reduceOnly,
        uint8 tif,
        uint128 cloid
    ) external onlyOwner {
        CoreWriterLib.placeLimitOrder(asset, isBuy, limitPx, sz, reduceOnly, tif, cloid);
    }

    /// @notice Action 10.
    function cancelByOid(uint32 asset, uint64 oid) external onlyOwner {
        CoreWriterLib.cancelOrderByOrderId(asset, oid);
    }

    /// @notice Action 11.
    function cancelByCloid(uint32 asset, uint128 cloid) external onlyOwner {
        CoreWriterLib.cancelOrderByCloid(asset, cloid);
    }

    /// @notice Action 7. `ntl` is in perp units (1e6 = 1 USDC).
    function usdClassTransfer(uint64 ntl, bool toPerp) external onlyOwner {
        CoreWriterLib.transferUsdClass(ntl, toPerp);
    }

    /// @notice Action 6. `amountWei` is in the token's wei (USDC: 1e8 = 1 USDC).
    function spotSend(address to, uint64 token, uint64 amountWei) external onlyOwner {
        CoreWriterLib.spotSend(to, token, amountWei);
    }

    /// @notice Action 13.
    function sendAsset(
        address destination,
        address subAccount,
        uint32 sourceDex,
        uint32 destinationDex,
        uint64 token,
        uint64 amountWei
    ) external onlyOwner {
        CoreWriterLib.sendAsset(destination, subAccount, sourceDex, destinationDex, token, amountWei);
    }

    /// @notice Action 12. `maxFeeRate` is in decibps.
    function approveBuilderFee(uint64 maxFeeRate, address builder) external onlyOwner {
        CoreWriterLib.approveBuilderFee(maxFeeRate, builder);
    }

    // ── payment on HyperEVM (spike question 5) ───────────────────────────────────────

    /// @notice Pulls `amount` of the chain's linked USDC from the caller.
    function pay(uint256 amount) external {
        if (amount == 0) revert ZeroAmount();
        paidBy[msg.sender] += amount;
        IERC20(HLConstants.usdc()).safeTransferFrom(msg.sender, address(this), amount);
        emit Paid(msg.sender, amount);
    }

    /// @notice Moves USDC this contract holds on HyperEVM into its own HyperCore spot balance.
    function bridgeUsdcToCore(uint256 evmAmount) external onlyOwner {
        CoreWriterLib.bridgeToCore(HLConstants.USDC_TOKEN_INDEX, evmAmount);
    }

    // ── reads (precompiles see the state at the start of the current block) ──────────

    function marginSummary(address user) external view returns (PrecompileLib.AccountMarginSummary memory) {
        return PrecompileLib.accountMarginSummary(HLConstants.DEFAULT_PERP_DEX, user);
    }

    function positionOf(address user, uint32 perp) external view returns (PrecompileLib.Position memory) {
        return PrecompileLib.position(user, perp);
    }

    function spotBalanceOf(address user, uint64 token) external view returns (PrecompileLib.SpotBalance memory) {
        return PrecompileLib.spotBalance(user, token);
    }

    function withdrawableOf(address user) external view returns (uint64) {
        return PrecompileLib.withdrawable(user);
    }

    function coreUserExists(address user) external view returns (bool) {
        return PrecompileLib.coreUserExists(user);
    }
}
