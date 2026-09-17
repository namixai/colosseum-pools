// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

/// @notice Rules a pool sets for whoever trades its capital. Copied into every challenge at
///         creation, so they can't change under a running challenge.
struct Rules {
    /// Equity may not fall this far below the first snapshot of the UTC day (bps).
    uint16 dailyLossBps;
    /// Equity may not fall this far below the starting capital, static (bps).
    uint16 maxDrawdownBps;
    /// Notional over equity may not exceed this, times 100 (500 = 5x).
    uint32 maxLeverageX100;
    /// Perp asset indices the trader may hold; a subset of the platform list.
    uint32[] assets;
}

/// @notice What a pool sells and what it pays out.
struct Terms {
    /// Price of one challenge, in HyperEVM USDC units (6 decimals).
    uint64 price;
    /// Capital the challenge trades, in HyperCore perp USD units (1e6 = 1 USDC).
    uint64 capital;
    /// Profit target over the challenge capital (bps).
    uint16 targetBps;
    /// How long a challenge may run, in seconds.
    uint32 duration;
    /// Trader's share of the challenge profit when it passes (bps).
    uint16 traderShareBps;
    /// Capital a funded trader trades on the pool account, perp USD units (1e6 = 1 USDC).
    uint64 fundedCapital;
}

/// @notice An order the caller of a stop wants cancelled. No precompile lists open orders,
///         so whoever pulls the stop names them (from the info API).
struct Cancel {
    uint32 asset;
    uint64 oid;
}

/// @notice Why an account is (or would be) stopped.
enum Breach {
    None,
    Drawdown,
    DailyLoss,
    Leverage,
    ForbiddenAsset
}

library Units {
    /// @dev HyperCore spot USDC has 8 decimals, perp USD has 6.
    uint64 internal constant SPOT_PER_PERP = 100;
    /// @dev What HyperCore charges the sender, on top of the amount, when a spot transfer
    ///      creates the recipient's account: 1 USDC in spot units. Seen on testnet on
    ///      17 Sep 2026, from an EOA and from a contract alike.
    uint64 internal constant NEW_ACCOUNT_FEE = 100_000_000;
    uint16 internal constant BPS = 10_000;
    uint32 internal constant USDC_TOKEN = 0;
    uint32 internal constant PERP_DEX = 0;
    uint8 internal constant ABSTRACTION_DISABLED = 1;
    uint8 internal constant TIF_IOC = 3;
    /// Upper bound on how many assets one stop or settle call walks.
    uint256 internal constant MAX_ASSETS = 16;
    /// Upper bound on how many named orders one stop cancels.
    uint256 internal constant MAX_CANCELS = 32;
}
