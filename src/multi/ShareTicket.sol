// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreOps} from "../lib/CoreOps.sol";

/// @title ShareTicket
/// @notice One deposit into a `PoolShares`: an address of its own on HyperCore that the depositor
///         sends USDC to. A contract can't see who sent a HyperCore transfer, so the address is the
///         attribution, as with `DepositTicket`. The difference is where the money goes: straight into
///         the pool of many traders while it runs, so that the shares contract's own balance holds
///         only what it owes, and to the shares contract once the pool has stopped.
/// @dev Cloned by the shares contract for each deposit (EIP-1167, deterministic from the depositor and
///      a counter). It holds nothing on HyperEVM and pays only where its shares contract says.
contract ShareTicket {
    address public shares;
    address public depositor;

    error NotShares();
    error Initialized();

    constructor() {
        // The implementation itself is never a ticket.
        shares = address(1);
    }

    function init(address depositor_) external {
        if (shares != address(0)) revert Initialized();
        shares = msg.sender;
        depositor = depositor_;
    }

    /// @notice Sends `amount1e8` of this ticket's spot USDC to `to` on HyperCore.
    function sweep(address to, uint64 amount1e8) external {
        if (msg.sender != shares) revert NotShares();
        CoreOps.sendUsdc(to, amount1e8);
    }
}
