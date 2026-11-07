// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {CoreWriterLib} from "@hyper-evm-lib/src/CoreWriterLib.sol";

import {Units} from "../Types.sol";
import {CoreOps} from "../lib/CoreOps.sol";

/// @title StopGasProbe
/// @notice What the worst stop of a ruled account costs on HyperEVM's own precompiles and CoreWriter
///         (audit window 2, В6-3, the CTO's 22 October 2026). Run read-only, by eth_call with this
///         code put at an address (spike/run.py stopgas). Every read and every action of
///         ChallengeAccount.breach, in its order, with each position taken as open so that every
///         closing order goes out:
///         - violation(): the margin of the main dex and the account's, each asset's cap
///           (position and mark), the extra assets' positions;
///         - the cut: a keyless address (HyperCore asked whether it exists) and its approval;
///         - the cancels;
///         - the closing orders of the rules' assets and the extra ones (position, size decimals,
///           mark, the order);
///         - the equity the Stopped event says (the two margins again).
///         The account's own storage, events and the registry's retire are not here; test/spike's
///         StopGas test measures them on the simulator as the stop's gas less this probe's.
contract StopGasProbe {
    struct Used {
        uint256 violation;
        uint256 cut;
        uint256 cancels;
        uint256 closes;
        uint256 equity;
        uint256 total;
    }

    /// @param acct the account whose reads are made (any address: HyperCore answers for it)
    /// @param dex the account's dex, 0 for the main one
    /// @param rules the rules' assets; @param extra the extra ones (order ids)
    /// @param cancels how many cancels the stop carries
    function run(address acct, uint32 dex, uint32[] calldata rules, uint32[] calldata extra, uint256 cancels)
        external
        returns (Used memory u)
    {
        uint256 start = gasleft();
        u.violation = _violation(acct, dex, rules, extra);
        u.cut = _cut(acct);
        u.cancels = _cancels(rules, cancels);
        u.closes = _closes(acct, rules, extra);
        uint256 g = gasleft();
        _sink(uint256(uint64(CoreOps.equity(acct, dex))));
        u.equity = g - gasleft();
        u.total = start - gasleft();
    }

    function _violation(address acct, uint32 dex, uint32[] calldata rules, uint32[] calldata extra)
        internal
        view
        returns (uint256 used)
    {
        uint256 g = gasleft();
        uint256 sink = uint256(uint64(CoreOps.margin(acct, dex).accountValue));
        for (uint256 i = 0; i < rules.length; ++i) {
            sink += uint256(uint64(CoreOps.positionSize(acct, rules[i])));
            sink += PrecompileLib.markPx(CoreOps.pxIndex(rules[i]));
        }
        for (uint256 i = 0; i < extra.length; ++i) {
            sink += uint256(uint64(CoreOps.positionSize(acct, extra[i])));
        }
        _sink(sink);
        used = g - gasleft();
    }

    function _cut(address acct) internal returns (uint256 used) {
        uint256 g = gasleft();
        address keyless = CoreOps.keylessAddress(acct, 0, bytes32(0));
        CoreOps.setAgent(keyless, (block.timestamp + 179 days) * 1000);
        used = g - gasleft();
    }

    function _cancels(uint32[] calldata rules, uint256 cancels) internal returns (uint256 used) {
        uint256 g = gasleft();
        for (uint256 i = 0; i < cancels; ++i) {
            CoreOps.cancel(rules.length != 0 ? rules[i % rules.length] : 0, uint64(i + 1));
        }
        used = g - gasleft();
    }

    function _closes(address acct, uint32[] calldata rules, uint32[] calldata extra) internal returns (uint256 used) {
        uint256 g = gasleft();
        for (uint256 i = 0; i < rules.length + extra.length; ++i) {
            _close(acct, i < rules.length ? rules[i] : extra[i - rules.length]);
        }
        used = g - gasleft();
    }

    /// A closing order as CoreOps.closePositions sends it, for a position taken as open.
    function _close(address acct, uint32 perp) internal {
        _sink(uint256(uint64(CoreOps.positionSize(acct, perp))));
        uint8 szDecimals = CoreOps.sizeDecimals(perp);
        uint64 px = CoreOps.crossingPx1e8(perp, szDecimals, false, 500);
        CoreWriterLib.placeLimitOrder(perp, false, px, uint64(10 ** (8 - szDecimals)), true, Units.TIF_IOC, 0);
    }

    /// Keeps the reads from being optimised away.
    function _sink(uint256 x) internal pure {
        if (x == type(uint256).max) revert();
    }
}
