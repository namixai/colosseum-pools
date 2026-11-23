// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {StopBook} from "../../src/sim/StopBook.sol";

/// What an exam started with a filler reads from its pool: the pool's factory (`factory()`), and that factory's
/// stop book (`stopBook()`). A test that starts such an exam itself stands in for both, and for the factory's
/// register: as the factory's, it names this contract, the pool, its own pool, and takes any other contract for an
/// exam of it; an address with no code belongs to no pool (the factory's own register: `test/sim/SimPool.t.sol`).
abstract contract BookStand {
    StopBook public stopBook;

    function _makeBook() internal {
        stopBook = new StopBook();
    }

    function factory() external view returns (address) {
        return address(this);
    }

    function poolOf(address account) external view returns (address) {
        return account.code.length != 0 ? address(this) : address(0);
    }

    /// Every asset listed: the book logs any (the factory's own list: `test/sim/SimPool.t.sol`).
    function watchable(uint32) external pure returns (bool) {
        return true;
    }
}
