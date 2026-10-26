// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {Rules, Units} from "../Types.sol";
import {CoreOps} from "./CoreOps.sol";

/// @title AccountOps
/// @notice What a ruled account (Pool, ChallengeAccount) does on HyperCore that takes the most code:
///         closing its positions at a crossing price, choosing the keyless address of a stop, and
///         approving an agent by name for a term. A linked library: its public functions run by
///         DELEGATECALL in the account's own context, so CoreWriter still sees the account as the
///         sender. Out of the accounts, which HIP-3 brought over the size a contract may have (the
///         CTO, 21 October 2026).
library AccountOps {
    error TooManyAssets();

    /// @notice A reduce-only IOC order for every open position among the rules' assets and `extra`;
    ///         how many were open.
    function closeAll(Rules storage r, uint32[] memory extra, uint16 slippageBps) public returns (uint256) {
        return CoreOps.closePositions(address(this), assetsWith(r, extra), slippageBps);
    }

    /// @notice The rules' assets plus whatever the caller adds, without duplicates. The two are
    ///         bounded apart: a pool that lists the most assets it may can still close a position in
    ///         one it does not list (deep audit, F11).
    function assetsWith(Rules storage r, uint32[] memory extra) public view returns (uint32[] memory all) {
        uint256 n = r.assets.length;
        if (extra.length > Units.MAX_ASSETS) revert TooManyAssets();
        all = new uint32[](n + extra.length);
        for (uint256 i = 0; i < n; ++i) {
            all[i] = r.assets[i];
        }
        uint256 k = n;
        for (uint256 i = 0; i < extra.length; ++i) {
            bool seen = false;
            for (uint256 j = 0; j < k; ++j) {
                if (all[j] == extra[i]) {
                    seen = true;
                    break;
                }
            }
            if (!seen) all[k++] = extra[i];
        }
        assembly {
            mstore(all, k)
        }
    }

    function keylessAddress(address account, uint256 nonce, bytes32 salt) public view returns (address) {
        return CoreOps.keylessAddress(account, nonce, salt);
    }

    function setAgent(address agent, uint256 validUntilMs) public {
        CoreOps.setAgent(agent, validUntilMs);
    }

    /// @notice Moves what the perp side can give to spot, `dex`'s whole value first, and returns what
    ///         was asked to move (1e6).
    function toSpotAll(uint32 dex) public returns (uint64 free) {
        int64 onDex = CoreOps.onDex(address(this), dex);
        if (onDex > 0) {
            free = uint64(onDex);
            CoreOps.toSpot(free, dex);
        }
        uint64 main = CoreOps.withdrawable(address(this));
        if (main != 0) CoreOps.toSpot(main, Units.PERP_DEX);
        free += main;
    }

    /// @notice Whether a position's notional over `eq` (1e6) is past its asset's own cap
    ///         (`Rules.assetLeverageX100`). Size times mark is already 1e6 USDC: a perp's price
    ///         carries `6 - szDecimals` decimals and its size `szDecimals`.
    function overAssetCap(Rules storage r, uint256 eq) public view returns (bool) {
        uint256 n = r.assetLeverageX100.length;
        for (uint256 i = 0; i < n; ++i) {
            uint32 a = r.assets[i];
            int64 szi = CoreOps.positionSize(address(this), a);
            if (szi == 0) continue;
            uint256 ntl = uint256(uint64(szi < 0 ? -szi : szi)) * PrecompileLib.markPx(CoreOps.pxIndex(a));
            if (ntl * 100 > eq * r.assetLeverageX100[i]) return true;
        }
        return false;
    }
}
