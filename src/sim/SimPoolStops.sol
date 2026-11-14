// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {SimPool} from "./SimPool.sol";
import {SimPoolFactory} from "./SimPoolFactory.sol";

/// @title SimPoolStops
/// @notice The checks and the events of a simulated pool's stops: `stopPayout`, `liftStop`, `objectStop` and
///         `finalizeStop` of `SimPool`, each called by the pool on its own payouts. A library with external
///         functions runs in the pool's own storage and context (DELEGATECALL): the payout it changes is the
///         pool's, the sender is the pool's caller, and its events are the pool's. The pool keeps its own
///         counters and money (`owed`, `reserved`, `stoppedCount`, `liftedCount`, `head`) and passes the
///         counts the events carry. Moved out of `SimPool` only for the size of its code (EIP-170); what it does
///         is what the pool did.
library SimPoolStops {
    /// The checks and the record of a stop; the amount the pool now sets aside. The reason is kept by the payout's id
    /// (`SimPool.stopReasonOf`).
    function stop(
        SimPool.Payout[] storage payouts,
        mapping(uint256 => uint8) storage reasonOf,
        SimPoolFactory factory,
        uint256 id,
        uint8 reason,
        address paired,
        bytes32[] calldata trades,
        bytes32[] calldata links,
        uint256[3] memory counts
    ) external returns (uint64 amount) {
        if (id >= payouts.length) revert SimPool.BadPayout();
        SimPool.Payout storage p = payouts[id];
        if (p.state != SimPool.PayoutState.Due || p.wasStopped) revert SimPool.BadPayout();
        if (reason == 3) {
            if (paired != address(0) || trades.length == 0 || links.length != 1) revert SimPool.BadReason();
        } else if (reason == 4) {
            // Prohibited play: a series of exams bought by one person or by linked wallets, each traded at the edge of
            // its risk to pass by luck, or one account hedged by another. The published analysis is the one link;
            // the transactions that show it are at least two; `paired`, if named, is another account of the factory.
            if (trades.length < 2 || links.length != 1) revert SimPool.BadReason();
            if (
                paired != address(0)
                    && (paired == p.account || factory.poolOf(paired) == address(0) || factory.poolOf(paired) == paired)
            ) revert SimPool.BadPaired();
        } else {
            if (reason == 1) {
                if (links.length != 0) revert SimPool.BadReason();
            } else if (reason == 2) {
                if (links.length == 0) revert SimPool.BadReason();
            } else {
                revert SimPool.BadReason();
            }
            if (trades.length < 2) revert SimPool.BadReason();
            if (paired == p.account || factory.poolOf(paired) == address(0) || factory.poolOf(paired) == paired) {
                revert SimPool.BadPaired();
            }
        }
        p.state = SimPool.PayoutState.Stopped;
        p.wasStopped = true;
        p.stoppedAt = uint64(block.timestamp);
        reasonOf[id] = reason;
        amount = p.amount;
        emit SimPool.PayoutStopped(id, p.trader, amount, reason, paired, trades, links, counts[0], counts[1], counts[2]);
    }

    /// The checks and the record of a lift; the amount the pool owes again.
    function lift(SimPool.Payout[] storage payouts, SimPoolFactory factory, uint256 id, bool finished, uint256[3] memory counts)
        external
        returns (uint64 amount)
    {
        if (finished) revert SimPool.Done();
        if (id >= payouts.length) revert SimPool.BadPayout();
        SimPool.Payout storage p = payouts[id];
        if (p.state != SimPool.PayoutState.Stopped) revert SimPool.BadPayout();
        if (block.timestamp >= uint256(p.stoppedAt) + factory.STOP_RESERVE()) revert SimPool.ReserveOver();
        p.state = SimPool.PayoutState.Due;
        amount = p.amount;
        emit SimPool.StopLifted(id, counts[0], counts[1], counts[2]);
    }

    /// The trader's objection: nothing changes but the record.
    function object(SimPool.Payout[] storage payouts, SimPoolFactory factory, uint256 id, bytes32 text) external {
        if (id >= payouts.length) revert SimPool.BadPayout();
        SimPool.Payout storage p = payouts[id];
        if (p.state != SimPool.PayoutState.Stopped) revert SimPool.BadPayout();
        if (msg.sender != p.trader) revert SimPool.NotTrader();
        if (block.timestamp >= uint256(p.stoppedAt) + factory.STOP_RESERVE()) revert SimPool.ReserveOver();
        emit SimPool.StopObjected(id, msg.sender, text);
    }

    /// The checks and the record of a final stop; the amount the pool no longer sets aside.
    function finalize(SimPool.Payout[] storage payouts, SimPoolFactory factory, uint256 id, uint256 reserved)
        external
        returns (uint64 amount)
    {
        if (id >= payouts.length) revert SimPool.BadPayout();
        SimPool.Payout storage p = payouts[id];
        if (p.state != SimPool.PayoutState.Stopped) revert SimPool.BadPayout();
        if (block.timestamp < uint256(p.stoppedAt) + factory.STOP_RESERVE()) revert SimPool.ReserveRunning();
        p.state = SimPool.PayoutState.Refused;
        amount = p.amount;
        emit SimPool.StopFinal(id, amount, reserved - amount);
    }
}
