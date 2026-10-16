// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CodeStore} from "../lib/CodeStore.sol";

/// @title TermsBook
/// @notice The terms of pools, every clause written out, open to anyone on chain. A text is kept
///         once, under its keccak256, and changed by nobody. A pool names the hash of its terms;
///         whoever accepts them -- the owner who starts the pool, each investor who deposits --
///         names the same hash in their own call, and the contract checks it against the pool's.
/// @dev A text is kept as the code of a contract of its own (`CodeStore`), so it is read with one
///      call however old it is, and it is at most 24,575 bytes.
contract TermsBook {
    mapping(bytes32 hash => address) public textAt;

    event Published(bytes32 indexed hash, address text, uint256 length);

    error Unknown();

    /// @notice Keeps `text` and returns its hash; a text already kept is not kept twice.
    function publish(string calldata text) external returns (bytes32 hash) {
        hash = keccak256(bytes(text));
        if (textAt[hash] != address(0)) return hash;
        address a = CodeStore.write(bytes(text));
        textAt[hash] = a;
        emit Published(hash, a, bytes(text).length);
    }

    /// @notice The text kept under `hash`.
    function text(bytes32 hash) external view returns (string memory) {
        address a = textAt[hash];
        if (a == address(0)) revert Unknown();
        return string(CodeStore.read(a));
    }
}
