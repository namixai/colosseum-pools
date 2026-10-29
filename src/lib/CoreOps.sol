// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreWriterLib} from "@hyper-evm-lib/src/CoreWriterLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";
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

    /// The order id of a builder-deployed dex's asset is HIP3_BASE + dex * HIP3_DEX + index; the
    /// precompiles take that id less HIP3_BASE. A wrong index does not fail: it reads another dex's
    /// market (КОЛИЗЕЙ-2's spike q9, 21 October 2026), so the factory checks each listed asset's name.
    uint32 internal constant HIP3_BASE = 100_000;
    uint32 internal constant HIP3_DEX = 10_000;
    /// sendAsset's source or destination for the spot account.
    uint32 internal constant SPOT_DEX = type(uint32).max;

    /// @notice The index the precompiles take for the asset orders name as `asset`.
    function pxIndex(uint32 asset) internal pure returns (uint32) {
        return asset >= HIP3_BASE ? asset - HIP3_BASE : asset;
    }

    /// @notice The perp dex an asset trades on: 0 for the main one.
    function dexOf(uint32 asset) internal pure returns (uint32) {
        return asset >= HIP3_BASE ? (asset - HIP3_BASE) / HIP3_DEX : 0;
    }

    /// @notice Perp account value, 1e6 = 1 USDC: the main dex's and `dex`'s, an account trading on
    ///         a builder-deployed dex keeping its capital there.
    function equity(address account, uint32 dex) internal view returns (int64) {
        return margin(account, dex).accountValue;
    }

    function margin(address account, uint32 dex) internal view returns (PrecompileLib.AccountMarginSummary memory m) {
        m = PrecompileLib.accountMarginSummary(Units.PERP_DEX, account);
        if (dex != Units.PERP_DEX) {
            PrecompileLib.AccountMarginSummary memory d = PrecompileLib.accountMarginSummary(dex, account);
            m.accountValue += d.accountValue;
            m.marginUsed += d.marginUsed;
            m.ntlPos += d.ntlPos;
            m.rawUsd += d.rawUsd;
        }
    }

    /// @notice What `dex` alone holds for `account` (1e6), 0 for the main dex.
    function onDex(address account, uint32 dex) internal view returns (int64) {
        return dex == Units.PERP_DEX ? int64(0) : PrecompileLib.accountMarginSummary(dex, account).accountValue;
    }

    /// @notice Signed position size in the asset's size units (szDecimals).
    function positionSize(address account, uint32 perp) internal view returns (int64) {
        return PrecompileLib.position(account, pxIndex(perp)).szi;
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
        return crossingPx1e8(perp, sizeDecimals(perp), isBuy, slippageBps);
    }

    /// @notice The perp's size decimals, refused above six (the price would have none left).
    function sizeDecimals(uint32 perp) internal view returns (uint8) {
        uint8 szDecimals = PrecompileLib.perpAssetInfo(pxIndex(perp)).szDecimals;
        if (szDecimals > 6) revert UnsupportedSizeDecimals(perp, szDecimals);
        return szDecimals;
    }

    /// @dev The same with the size decimals already read: a closing order reads them once (audit
    ///      window 2, В6-3, the CTO, 22 October 2026).
    function crossingPx1e8(uint32 perp, uint8 szDecimals, bool isBuy, uint16 slippageBps)
        internal
        view
        returns (uint64)
    {
        uint8 decimals = 6 - szDecimals;
        uint256 mark = PrecompileLib.markPx(pxIndex(perp));
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
            uint8 szDecimals = sizeDecimals(perp); // reverts on an unsupported asset
            uint64 px = crossingPx1e8(perp, szDecimals, isBuy, slippageBps);
            uint64 size1e8 = SafeCast.toUint64(uint256(size) * 10 ** (8 - szDecimals));
            CoreWriterLib.placeLimitOrder(perp, isBuy, px, size1e8, true, Units.TIF_IOC, 0);
        }
    }

    function cancel(uint32 asset, uint64 oid) internal {
        CoreWriterLib.cancelOrderByOrderId(asset, oid);
    }

    /// @notice Makes `agent` this account's API wallet named `pools`, replacing the one of that name,
    ///         approved until `validUntilMs` (milliseconds, as HyperCore keeps time). HyperCore reads
    ///         the term from the name, `pools valid_until <ms>`: an agent approved with no name lives
    ///         for a time it does not show, and a cleanup pass removes it after (19 October 2026).
    function setAgent(address agent, uint256 validUntilMs) internal {
        CoreWriterLib.addApiWallet(agent, string.concat("pools valid_until ", Strings.toString(validUntilMs)));
    }

    function separateBalances(address account) internal {
        CoreWriterLib.setAbstraction(account, Units.ABSTRACTION_DISABLED);
    }

    /// @notice Moves `usd1e6` from spot to the perp side of `dex`: the main dex's by a class
    ///         transfer, a builder-deployed dex's by sendAsset to itself (spike q9: taken, and read
    ///         back by accountMarginSummary(dex, account)).
    function toPerp(uint64 usd1e6, uint32 dex) internal {
        if (usd1e6 == 0) return;
        if (dex == Units.PERP_DEX) CoreWriterLib.transferUsdClass(usd1e6, true);
        else CoreWriterLib.sendAsset(address(this), address(0), SPOT_DEX, dex, Units.USDC_TOKEN, usd1e6 * Units.SPOT_PER_PERP);
    }

    function toSpot(uint64 usd1e6, uint32 dex) internal {
        if (usd1e6 == 0) return;
        if (dex == Units.PERP_DEX) CoreWriterLib.transferUsdClass(usd1e6, false);
        else CoreWriterLib.sendAsset(address(this), address(0), dex, SPOT_DEX, Units.USDC_TOKEN, usd1e6 * Units.SPOT_PER_PERP);
    }

    function sendUsdc(address to, uint64 amount1e8) internal {
        if (amount1e8 != 0) CoreWriterLib.spotSend(to, Units.USDC_TOKEN, amount1e8);
    }

    /// @notice What can be sent to `to` out of `budget1e8` so that the whole transfer, the fee
    ///         for creating `to`'s account included, costs the sender at most the budget.
    ///         Zero if the fee alone takes the budget.
    function sendableTo(address to, uint64 budget1e8) internal view returns (uint64) {
        if (exists(to)) return budget1e8;
        return budget1e8 > Units.NEW_ACCOUNT_FEE ? budget1e8 - Units.NEW_ACCOUNT_FEE : 0;
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
    ///      let anyone block the stop. HyperCore doesn't take an existing user as an agent
    ///      (spike question 8, 17 Sep 2026): the replacement then does nothing and the old key
    ///      keeps trading until `recut` succeeds with another salt. The keeper watches for
    ///      that. Settlement drains the account either way.
    function keylessAddress(address account, uint256 nonce, bytes32 salt) internal view returns (address candidate) {
        for (uint256 i = 0; i < KEYLESS_TRIES; ++i) {
            candidate = keylessCandidate(account, nonce, salt, i);
            if (!exists(candidate)) return candidate;
        }
    }
}
