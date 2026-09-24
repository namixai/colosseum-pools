// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreOps} from "../lib/CoreOps.sol";

/// @title DepositTicket
/// @notice One deposit into a shared pool: an address of its own on HyperCore that the depositor sends
///         USDC to. A contract can't see who sent a HyperCore transfer, so the address is the
///         attribution: whatever lies here when the shared pool counts it is this depositor's deposit.
///         From then on the ticket is closed and everything on it belongs to the pool.
/// @dev Cloned by the shared pool for each deposit (EIP-1167, deterministic from the depositor and a
///      counter). It holds nothing on HyperEVM and can pay only into its own pool.
contract DepositTicket {
    address public pool;
    address public depositor;

    error NotPool();
    error Initialized();

    constructor() {
        // The implementation itself is never a ticket.
        pool = address(1);
    }

    function init(address depositor_) external {
        if (pool != address(0)) revert Initialized();
        pool = msg.sender;
        depositor = depositor_;
    }

    /// @notice Sends `amount1e8` of this ticket's spot USDC to its pool on HyperCore.
    function sweep(uint64 amount1e8) external {
        if (msg.sender != pool) revert NotPool();
        CoreOps.sendUsdc(pool, amount1e8);
    }
}
