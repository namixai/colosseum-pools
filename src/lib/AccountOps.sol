// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {Rules} from "../Types.sol";
import {CoreOps} from "./CoreOps.sol";

/// @title AccountOps
/// @notice What a ruled account (Pool, ChallengeAccount) does on HyperCore that takes the most code:
///         closing its positions at a crossing price, choosing the keyless address of a stop, and
///         approving an agent by name for a term. A linked library: its public functions run by
///         DELEGATECALL in the account's own context, so CoreWriter still sees the account as the
///         sender. Out of the accounts, which HIP-3 brought over the size a contract may have (the
///         CTO, 21 October 2026).
library AccountOps {
    /// @notice A reduce-only IOC order for every open position among `perps`; how many were open.
    function closePositions(address account, uint32[] memory perps, uint16 slippageBps) public returns (uint256) {
        return CoreOps.closePositions(account, perps, slippageBps);
    }

    function keylessAddress(address account, uint256 nonce, bytes32 salt) public view returns (address) {
        return CoreOps.keylessAddress(account, nonce, salt);
    }

    function setAgent(address agent, uint256 validUntilMs) public {
        CoreOps.setAgent(agent, validUntilMs);
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
