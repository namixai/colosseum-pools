// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreWriterLib} from "@hyper-evm-lib/src/CoreWriterLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {Units} from "../Types.sol";

/// @title CoreOps
/// @notice Reads and writes against HyperCore for an account contract acting on itself.
/// @dev Reads come from precompiles and reflect the start of the current block. Writes go
///      through CoreWriter and execute on HyperCore a few seconds later; a failure there does
///      not revert here.
library CoreOps {
    error UnsupportedSizeDecimals(uint32 perp, uint8 szDecimals);

    // ── reads ────────────────────────────────────────────────────────────────────────

    function exists(address account) internal view returns (bool) {
        return PrecompileLib.coreUserExists(account);
    }

    /// @notice Perp account value, 1e6 = 1 USDC.
    function equity(address account) internal view returns (int64) {
        return PrecompileLib.accountMarginSummary(Units.PERP_DEX, account).accountValue;
    }

    function margin(address account) internal view returns (PrecompileLib.AccountMarginSummary memory) {
        return PrecompileLib.accountMarginSummary(Units.PERP_DEX, account);
    }

    /// @notice Signed position size in the asset's size units (szDecimals).
    function positionSize(address account, uint32 perp) internal view returns (int64) {
        return PrecompileLib.position(account, perp).szi;
    }

    /// @notice Free perp balance that can move to spot, 1e6 = 1 USDC.
    function withdrawable(address account) internal view returns (uint64) {
        return PrecompileLib.withdrawable(account);
    }

    /// @notice Spot USDC, 1e8 = 1 USDC.
    function spotUsdc(address account) internal view returns (uint64) {
        return PrecompileLib.spotBalance(account, Units.USDC_TOKEN).total;
    }

    // ── price rules ──────────────────────────────────────────────────────────────────

    /// @notice Rounds a perp price to what Hyperliquid accepts: at most five significant
    ///         figures, except that a price whose integer part already has five or more
    ///         digits is rounded to an integer instead.
    /// @param px the price as an integer with `decimals` decimals (the mark precompile's
    ///        format, where decimals = 6 - szDecimals)
    /// @param up round away from zero (a buy that must cross) instead of toward it (a sell)
    function roundPerpPx(uint256 px, uint8 decimals, bool up) internal pure returns (uint256) {
        if (px == 0) return 0;
        uint256 digits = _digits(px);
        uint256 intDigits = digits > decimals ? digits - decimals : 0;
        uint256 drop;
        if (intDigits >= 5) {
            drop = decimals;
        } else if (digits > 5) {
            drop = digits - 5;
        }
        if (drop == 0) return px;
        uint256 unit = 10 ** drop;
        uint256 down = (px / unit) * unit;
        if (!up || down == px) return down;
        return down + unit;
    }

    function _digits(uint256 x) private pure returns (uint256 n) {
        while (x != 0) {
            x /= 10;
            ++n;
        }
    }

    /// @notice Limit price for an order that should cross the book now: the mark moved by
    ///         `slippageBps` against us, rounded to the price rules, scaled to 1e8 as
    ///         CoreWriter expects.
    function crossingPx1e8(uint32 perp, bool isBuy, uint16 slippageBps) internal view returns (uint64) {
        uint8 szDecimals = PrecompileLib.perpAssetInfo(perp).szDecimals;
        if (szDecimals > 6) revert UnsupportedSizeDecimals(perp, szDecimals);
        uint8 decimals = 6 - szDecimals;
        uint256 mark = PrecompileLib.markPx(perp);
        uint256 bps = uint256(Units.BPS);
        uint256 px = isBuy ? (mark * (bps + slippageBps)) / bps : (mark * (bps - slippageBps)) / bps;
        px = roundPerpPx(px, decimals, isBuy);
        return SafeCast.toUint64(px * 10 ** (8 - decimals));
    }

    // ── writes ───────────────────────────────────────────────────────────────────────

    /// @notice Sends a reduce-only IOC order for every open position among `perps`.
    /// @return open how many positions were found open (and got a closing order)
    function closePositions(address account, uint32[] memory perps, uint16 slippageBps)
        internal
        returns (uint256 open)
    {
        for (uint256 i = 0; i < perps.length; ++i) {
            uint32 perp = perps[i];
            int64 szi = positionSize(account, perp);
            if (szi == 0) continue;
            ++open;
            bool isBuy = szi < 0;
            uint64 size = uint64(isBuy ? -szi : szi);
            uint64 px = crossingPx1e8(perp, isBuy, slippageBps); // reverts on an unsupported asset
            uint8 szDecimals = PrecompileLib.perpAssetInfo(perp).szDecimals;
            uint64 size1e8 = SafeCast.toUint64(uint256(size) * 10 ** (8 - szDecimals));
            CoreWriterLib.placeLimitOrder(perp, isBuy, px, size1e8, true, Units.TIF_IOC, 0);
        }
    }

    function cancel(uint32 asset, uint64 oid) internal {
        CoreWriterLib.cancelOrderByOrderId(asset, oid);
    }

    /// @notice Makes `agent` this account's unnamed API wallet, replacing whatever was there.
    function setAgent(address agent) internal {
        CoreWriterLib.addApiWallet(agent, "");
    }

    function separateBalances(address account) internal {
        CoreWriterLib.setAbstraction(account, Units.ABSTRACTION_DISABLED);
    }

    function toPerp(uint64 usd1e6) internal {
        if (usd1e6 != 0) CoreWriterLib.transferUsdClass(usd1e6, true);
    }

    function toSpot(uint64 usd1e6) internal {
        if (usd1e6 != 0) CoreWriterLib.transferUsdClass(usd1e6, false);
    }

    function sendUsdc(address to, uint64 amount1e8) internal {
        if (amount1e8 != 0) CoreWriterLib.spotSend(to, Units.USDC_TOKEN, amount1e8);
    }

    function approveBuilder(address builder, uint64 maxFeeDecibps) internal {
        if (builder != address(0)) CoreWriterLib.approveBuilderFee(maxFeeDecibps, builder);
    }

    uint256 internal constant KEYLESS_TRIES = 8;

    function keylessCandidate(address account, uint256 nonce, bytes32 salt, uint256 i)
        internal
        view
        returns (address)
    {
        return address(
            uint160(
                uint256(
                    keccak256(
                        abi.encode(
                            "colosseum-pools/keyless", account, nonce, salt, i, block.number, blockhash(block.number - 1)
                        )
                    )
                )
            )
        );
    }

    /// @notice An address nobody holds a key for (it is a hash output), preferably one
    ///         HyperCore has not seen. Used to replace an agent when a trader is cut off.
    /// @dev If every candidate already exists on HyperCore (someone predicted and funded
    ///      them), the last one is returned anyway rather than reverting: a revert here would
    ///      let anyone block the stop. Whether Hyperliquid accepts an existing user as an
    ///      agent is checked live by the spike; if the replacement fails, `recut` retries
    ///      with a different salt, and settlement drains the account either way.
    function keylessAddress(address account, uint256 nonce, bytes32 salt) internal view returns (address candidate) {
        for (uint256 i = 0; i < KEYLESS_TRIES; ++i) {
            candidate = keylessCandidate(account, nonce, salt, i);
            if (!exists(candidate)) return candidate;
        }
    }
}
