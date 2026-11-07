// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

/// @title CodeStore
/// @notice Bytes kept as the code of a contract of their own: written once, read by anyone with one
///         call, and changed by nobody. The code starts with STOP, so the contract cannot be run.
/// @dev The creation code copies what follows it and returns it as the new contract's code:
///      PUSH2 len, DUP1, PUSH1 0x0a, RETURNDATASIZE, CODECOPY, RETURNDATASIZE, RETURN.
library CodeStore {
    error NotStored();
    error TooLong();

    /// A contract's code is 24,576 bytes at most (EIP-170), and the first of them is STOP.
    uint256 internal constant MAX_LENGTH = 24_575;

    function write(bytes memory data) internal returns (address a) {
        // Longer, and the length would not fit the creation code's two bytes, or the code would
        // not be kept: what was stored would not be what was given (audit, Info).
        if (data.length > MAX_LENGTH) revert TooLong();
        bytes memory init = abi.encodePacked(hex"61", uint16(data.length + 1), hex"80600a3d393df300", data);
        assembly {
            a := create(0, add(init, 32), mload(init))
        }
        if (a == address(0)) revert NotStored();
    }

    function read(address a) internal view returns (bytes memory data) {
        uint256 n = a.code.length;
        if (n == 0) revert NotStored();
        data = new bytes(n - 1);
        assembly {
            extcodecopy(a, add(data, 32), 1, sub(n, 1))
        }
    }
}
