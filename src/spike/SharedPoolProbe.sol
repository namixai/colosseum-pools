// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";

interface IPoolRead {
    function stage() external view returns (uint8);
    function challenge() external view returns (address);
    function earned() external view returns (uint256);
    function fundedStart() external view returns (int64);
    function violation(uint32[] memory extraAssets) external view returns (uint8);
}

interface IChallengeRead {
    function status() external view returns (uint8);
    function payoutOwed() external view returns (uint64);
    function payoutDone() external view returns (bool);
    function violation(uint32[] memory extraAssets) external view returns (uint8);
}

/// @title SharedPoolProbe
/// @notice Spike harness, not product code. Two things the shared-pool design leans on and nobody has
///         measured: whether a precompile read can show a HyperCore transfer half done (`pair` reads both
///         sides in one call, so both come from the same state), and what reading one seat costs at a
///         settlement point (`seat`, `unitCosts`, `violationCost`).
/// @dev The spike never deploys this for the reads: it places the runtime code at an address with an
///      `eth_call` state override and calls it against live accounts. Stage and status numbers follow the
///      enums of `Pool` (Idle 0, Challenge 1, Funded 2, Closing 3) and `ChallengeAccount` (Active 2).
contract SharedPoolProbe {
    uint32 internal constant PERP_DEX = 0;
    uint64 internal constant USDC = 0;
    uint8 internal constant FUNDED = 2;
    uint8 internal constant ACTIVE = 2;

    struct Pair {
        uint256 blockNumber;
        uint256 timestamp;
        uint64 spotA;
        uint64 spotB;
        int64 perpA;
        int64 perpB;
    }

    struct Seat {
        uint256 gasUsed;
        uint8 stage;
        address challenge;
        uint8 challengeStatus;
        uint64 spot;
        int64 equity;
        uint64 challengeSpot;
        int64 challengeEquity;
        uint256 earned;
    }

    /// @notice Spot USDC (1e8) and perp account value (1e6) of two accounts, read in one call.
    function pair(address a, address b) external view returns (Pair memory p) {
        p.blockNumber = block.number;
        p.timestamp = block.timestamp;
        p.spotA = PrecompileLib.spotBalance(a, USDC).total;
        p.spotB = PrecompileLib.spotBalance(b, USDC).total;
        p.perpA = PrecompileLib.accountMarginSummary(PERP_DEX, a).accountValue;
        p.perpB = PrecompileLib.accountMarginSummary(PERP_DEX, b).accountValue;
    }

    /// @notice Gas of one read of each precompile a settlement point uses, against `user`.
    function unitCosts(address user) external view returns (uint256 spot, uint256 margin, uint256 exists) {
        uint256 g = gasleft();
        PrecompileLib.spotBalance(user, USDC);
        spot = g - gasleft();
        g = gasleft();
        PrecompileLib.accountMarginSummary(PERP_DEX, user);
        margin = g - gasleft();
        g = gasleft();
        PrecompileLib.coreUserExists(user);
        exists = g - gasleft();
    }

    /// @notice Gas of one external `violation()` call on a pool or a challenge, no extra assets.
    function violationCost(address account) external view returns (uint256 gasUsed, uint8 breach) {
        uint256 g = gasleft();
        breach = IPoolRead(account).violation(new uint32[](0));
        gasUsed = g - gasleft();
    }

    /// @notice One seat read the way a settlement point would read it: the seat's stage, its challenge, the
    ///         price it has earned, its balances and the challenge's, the challenge's payout, and whether a
    ///         live account breaks a rule right now.
    function seat(address pool) external view returns (Seat memory s) {
        uint256 g = gasleft();
        IPoolRead p = IPoolRead(pool);
        uint32[] memory none = new uint32[](0);
        s.stage = p.stage();
        s.challenge = p.challenge();
        s.earned = p.earned();
        s.spot = PrecompileLib.spotBalance(pool, USDC).total;
        s.equity = PrecompileLib.accountMarginSummary(PERP_DEX, pool).accountValue;
        if (s.stage == FUNDED) {
            p.fundedStart();
            p.violation(none);
        }
        if (s.challenge != address(0)) {
            IChallengeRead ch = IChallengeRead(s.challenge);
            s.challengeStatus = ch.status();
            s.challengeSpot = PrecompileLib.spotBalance(s.challenge, USDC).total;
            s.challengeEquity = PrecompileLib.accountMarginSummary(PERP_DEX, s.challenge).accountValue;
            ch.payoutOwed();
            ch.payoutDone();
            if (s.challengeStatus == ACTIVE) ch.violation(none);
        }
        s.gasUsed = g - gasleft();
    }
}
