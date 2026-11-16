// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SimPool} from "./SimPool.sol";
import {SimPoolFactory} from "./SimPoolFactory.sol";

// The scale of `SimPool.accrual`: what one unit of a code's weight (what its sales brought times its share in bps)
// has accrued since the pool opened, times this. Large enough that a withdrawal of one unit still counts when the
// pool's sales have brought more than ten million USDC.
uint256 constant ACCRUAL_SCALE = 1e27;

// Basis points: 10 000 is the whole.
uint256 constant BPS = 10_000;

/// @title SimPoolCodes
/// @notice The codes of a simulated pool: setting them, a sale by one, and what each has accrued and sends its
///         partner and, on a code for two, its author. A withdrawal shares its profit among the codes by one step of `SimPool.accrual`, whatever their
///         number; each code takes its part of every step it was in the moment it is next touched (a sale, a raise,
///         a claim), so no call ever goes through every code. A library with external functions runs in the pool's
///         own storage and context (DELEGATECALL): the codes are the pool's, transfers are the pool's, and so are its
///         events. The pool keeps its own totals (`brought`, `weight`, `sharesDue`, `held`).
library SimPoolCodes {
    /// Sets code `code` (`SimPool.setCode`, `SimPool.setSplitCode`): `author` and `authorBps` zero for a code for
    /// one. Returns what the code's weight grew by: a raise of a used code, from now on; whatever its old share
    /// accrued until now stays its own.
    function set(
        mapping(bytes32 => SimPool.Code) storage codeOf,
        SimPoolFactory factory,
        uint256 accrual,
        bytes32 code,
        address partner,
        uint16 bps,
        address author,
        uint16 authorBps
    ) external returns (uint256 raised) {
        SimPool.Code storage c = codeOf[code];
        if (c.used && (partner != c.partner || author != c.author || authorBps != c.authorBps || bps <= c.bps)) {
            revert SimPool.CodeUsed();
        }
        if (partner == address(0) && (bps != 0 || author != address(0))) revert SimPool.BadShare();
        if (bps != 0 && (bps < factory.MIN_PARTNER_BPS() || bps > factory.MAX_PARTNER_BPS())) revert SimPool.BadShare();
        // A code for two names its author and gives it a part of the code's share, never all of it.
        if ((author == address(0)) != (authorBps == 0) || authorBps >= BPS) revert SimPool.BadShare();
        _settle(c, code, accrual);
        if (c.used) raised = c.brought * (bps - c.bps);
        c.partner = partner;
        c.bps = bps;
        c.author = author;
        c.authorBps = authorBps;
        emit SimPool.CodeSet(code, partner, bps, author, authorBps);
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

    /// Sends each of `codes` what it has accrued and not yet sent, to its partner and to its author, each apart. A
    /// part the token refuses is set aside for whom it is (`heldOf`), as a refused payout is for its trader. Returns
    /// all that left what the codes are owed, and what of it was set aside.
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
            uint256 amount = c.partnerDue;
            if (amount != 0) {
                c.partnerDue = 0;
                c.partnerGot += amount;
                out += amount;
                setAside += _pay(heldOf, usdc, k, c.partner, amount);
            }
            amount = c.authorDue;
            if (amount != 0) {
                c.authorDue = 0;
                c.authorGot += amount;
                out += amount;
                setAside += _pay(heldOf, usdc, k, c.author, amount);
            }
        }
    }

    /// What code `code` has accrued and not yet sent its partner and its author, the part not yet counted in
    /// included.
    function due(mapping(bytes32 => SimPool.Code) storage codeOf, uint256 accrual, bytes32 code)
        external
        view
        returns (uint256 toPartner, uint256 toAuthor)
    {
        SimPool.Code storage c = codeOf[code];
        uint256 p = _pending(c, accrual);
        uint256 a = p * c.authorBps / BPS;
        return (c.partnerDue + p - a, c.authorDue + a);
    }

    /// Sends `amount` of code `code`'s share to `to`; if the token refuses, sets it aside for `to` and returns it.
    function _pay(mapping(address => uint256) storage heldOf, IERC20 usdc, bytes32 code, address to, uint256 amount)
        private
        returns (uint256 setAside)
    {
        (bool ok, bytes memory ret) = address(usdc).call(abi.encodeCall(IERC20.transfer, (to, amount)));
        bool sent = ok && (ret.length == 0 || abi.decode(ret, (bool)));
        if (!sent) {
            heldOf[to] += amount;
            setAside = amount;
        }
        emit SimPool.SharePaid(code, to, amount, sent);
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
        // The author's part of it, rounded down, and the partner the rest: the two always add up to what accrued.
        uint256 a = p * c.authorBps / BPS;
        c.partnerDue += p - a;
        c.authorDue += a;
        emit SimPool.ShareAccrued(code, p - a, a);
    }
}
