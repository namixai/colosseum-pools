// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {KeyBinder} from "../src/KeyBinder.sol";
import {Pool} from "../src/Pool.sol";
import {ChallengeAccount} from "../src/ChallengeAccount.sol";

/// What every suite that runs accounts needs of the key binder: the publisher's test key, and the
/// keeper's part, a fresh key signed and bound for an account that asked.
abstract contract BinderStand is Test {
    KeyBinder binder;
    address guardian = makeAddr("guardian");
    /// The publisher's key: in the platform it is born in the enclave; here it is a test key.
    uint256 constant PUBLISHER_PK = 0xB15D;
    address publisher = vm.addr(PUBLISHER_PK);
    /// Keys handed out so far, each new.
    uint256 internal keySeq;

    // ── keys ─────────────────────────────────────────────────────────────────────────

    function _newKey() internal returns (address) {
        return makeAddr(string.concat("enclave-key-", vm.toString(keySeq++)));
    }

    /// The key the next binding will name.
    function _peekKey() internal returns (address) {
        return makeAddr(string.concat("enclave-key-", vm.toString(keySeq)));
    }

    /// The publisher's signature of `key` for the request `account` has open.
    function _sign(address account, address key) internal view returns (bytes memory) {
        KeyBinder.Request memory r = binder.requestOf(account);
        (uint8 v, bytes32 rr, bytes32 s) =
            vm.sign(PUBLISHER_PK, binder.digest(account, r.id, r.trader, keccak256(abi.encode(key))));
        return abi.encodePacked(rr, s, v);
    }

    /// What the publisher and the keeper do for an account that asked: a fresh key, signed, bound.
    function _bind(address account) internal returns (address key) {
        key = _newKey();
        binder.bind(account, key, _sign(account, key));
    }

    function _asked(address account) internal view returns (bool) {
        return binder.requestOf(account).purpose != KeyBinder.Purpose.None;
    }

    /// Binds a key if the account asked for one. After a call a test expects to revert, it asks
    /// nothing, and binds nothing.
    function _bindIfAsked(address account) internal returns (address key) {
        if (_asked(account)) key = _bind(account);
    }

    /// `activate`, and the key the start then asks for.
    function _activate(ChallengeAccount ch) internal {
        ch.activate();
        _bindIfAsked(address(ch));
    }

    /// `openFundedStage`, and the key the stage then asks for.
    function _openFunded(Pool p) internal {
        p.openFundedStage();
        _bindIfAsked(address(p));
    }
}
