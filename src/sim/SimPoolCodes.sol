// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SimPool} from "./SimPool.sol";
import {SimPoolFactory} from "./SimPoolFactory.sol";

// The scale of `SimPool.accrual`: what one unit of a code's weight (what its sales brought times its share in bps)
// has accrued since the pool opened, times this. Large enough that a withdrawal of one unit still counts when the
// pool's sales have brought more than ten million USDC.
uint256 constant ACCRUAL_SCALE = 1e27;

/// @title SimPoolCodes
/// @notice The codes of a simulated pool: setting them, a sale by one, and what each has accrued and sends its
///         partner. A withdrawal shares its profit among the codes by one step of `SimPool.accrual`, whatever their
///         number; each code takes its part of every step it was in the moment it is next touched (a sale, a raise,
///         a claim), so no call ever goes through every code. A library with external functions runs in the pool's
///         own storage and context (DELEGATECALL): the codes are the pool's, transfers are the pool's, and so are its
///         events. The pool keeps its own totals (`brought`, `weight`, `sharesDue`, `held`).
library SimPoolCodes {
    /// Sets code `code` (`SimPool.setCode`). Returns what the code's weight grew by: a raise of a used code, from now
    /// on; whatever its old share accrued until now stays its own.
    function set(
        mapping(bytes32 => SimPool.Code) storage codeOf,
        SimPoolFactory factory,
        uint256 accrual,
        bytes32 code,
        address partner,
        uint16 bps
    ) external returns (uint256 raised) {
        SimPool.Code storage c = codeOf[code];
        if (c.used && (partner != c.partner || bps <= c.bps)) revert SimPool.CodeUsed();
        if (partner == address(0) && bps != 0) revert SimPool.BadShare();
        if (bps != 0 && (bps < factory.MIN_PARTNER_BPS() || bps > factory.MAX_PARTNER_BPS())) revert SimPool.BadShare();
        _settle(c, code, accrual);
        if (c.used) raised = c.brought * (bps - c.bps);
        c.partner = partner;
        c.bps = bps;
        emit SimPool.CodeSet(code, partner, bps);
    }

    /// A sale by code `code` brought the pool `got`. Returns the code's partner and what the code's weight grew by.
    function sold(mapping(bytes32 => SimPool.Code) storage codeOf, uint256 accrual, bytes32 code, uint256 got)
        external
        returns (address partner, uint256 added)
    {
        SimPool.Code storage c = codeOf[code];
        partner = c.partner;
        if (partner == address(0)) revert SimPool.NoSuchCode();
        _settle(c, code, accrual);
        c.used = true;
        c.brought += got;
        added = got * c.bps;
    }

    /// Sends each of `codes` what it has accrued and not yet sent, to its partner. A part the token refuses is set
    /// aside for the partner (`heldOf`), as a refused payout is for its trader. Returns all that left what the codes
    /// are owed, and what of it was set aside.
    function claim(
        mapping(bytes32 => SimPool.Code) storage codeOf,
        mapping(address => uint256) storage heldOf,
        IERC20 usdc,
        uint256 accrual,
        bytes32[] calldata codes
    ) external returns (uint256 out, uint256 setAside) {
        for (uint256 i = 0; i < codes.length; ++i) {
            bytes32 k = codes[i];
            SimPool.Code storage c = codeOf[k];
            _settle(c, k, accrual);
            uint256 amount = c.due;
            if (amount == 0) continue;
            c.due = 0;
            c.got += amount;
            out += amount;
            address to = c.partner;
            (bool ok, bytes memory ret) = address(usdc).call(abi.encodeCall(IERC20.transfer, (to, amount)));
            bool sent = ok && (ret.length == 0 || abi.decode(ret, (bool)));
            if (!sent) {
                heldOf[to] += amount;
                setAside += amount;
            }
            emit SimPool.SharePaid(k, to, amount, sent);
        }
    }

    /// What code `code` has accrued and not yet sent, the part not yet counted in included.
    function due(mapping(bytes32 => SimPool.Code) storage codeOf, uint256 accrual, bytes32 code)
        external
        view
        returns (uint256)
    {
        SimPool.Code storage c = codeOf[code];
        return c.due + _pending(c, accrual);
    }

    /// What the code accrued since it was last touched: its weight times the steps of `accrual` since.
    function _pending(SimPool.Code storage c, uint256 accrual) private view returns (uint256) {
        return c.brought * c.bps * (accrual - c.mark) / ACCRUAL_SCALE;
    }

    /// Counts what the code accrued since it was last touched into what it is owed, before its weight changes.
    function _settle(SimPool.Code storage c, bytes32 code, uint256 accrual) private {
        uint256 p = _pending(c, accrual);
        c.mark = accrual;
        if (p == 0) return;
        c.due += p;
        emit SimPool.ShareAccrued(code, p);
    }
}
