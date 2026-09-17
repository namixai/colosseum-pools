// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreOps} from "./lib/CoreOps.sol";

interface IAccountSource {
    function isAccount(address account) external view returns (bool);
}

/// @title KeyRegistry
/// @notice Addresses of Hyperliquid agent keys minted in the Signer enclave, and which account
///         and trader each one serves.
/// @dev A key moves Free -> Bound -> Retired and never back. Hyperliquid warns that actions
///      signed by a removed agent can be replayed once its nonces are pruned, so a key that
///      has served one account is never handed out again.
///
///      Anyone can send USDC to a published address, which gives it a HyperCore account, and
///      HyperCore doesn't take an address with an account as an agent (spike question 8,
///      17 Sep 2026). Such a key is retired when it comes up instead of being handed out.
///
///      What this contract can't show: that a published address was minted inside the
///      enclave. The operator publishes the addresses and the Signer build in use offers no
///      outside proof of where a key was born.
contract KeyRegistry {
    enum State {
        Unknown,
        Free,
        Bound,
        Retired
    }

    struct Binding {
        State state;
        address account;
        address trader;
    }

    /// How many spoiled keys one `assign` skips before giving up; `purgeSpoiled` clears more.
    uint256 public constant MAX_SPOILED_SKIPS = 16;

    address public operator;
    address public pendingOperator;
    IAccountSource public accounts;

    mapping(address key => Binding) private _bindings;
    mapping(address account => address key) public keyOf;
    address[] private _free;

    event OperatorTransferStarted(address indexed from, address indexed to);
    event OperatorTransferred(address indexed from, address indexed to);
    event AccountSourceSet(address indexed source);
    event KeyPublished(address indexed key);
    event KeyBound(address indexed key, address indexed account, address indexed trader);
    event KeyRetired(address indexed key, address indexed account);
    event KeySpoiled(address indexed key);

    error NotOperator();
    error NotPendingOperator();
    error SourceAlreadySet();
    error NotAnAccount(address caller);
    error KeyNotNew(address key);
    error KeyExistsOnCore(address key);
    error ZeroAddress();
    error NoFreeKey();
    error AccountHasKey(address account, address key);
    error NotBoundHere(address key, address caller);
    error TooManySpoiledKeys();

    constructor(address operator_) {
        if (operator_ == address(0)) revert ZeroAddress();
        operator = operator_;
        emit OperatorTransferred(address(0), operator_);
    }

    modifier onlyOperator() {
        if (msg.sender != operator) revert NotOperator();
        _;
    }

    // ── operator ─────────────────────────────────────────────────────────────────────

    /// @notice One-time link to the factory that says which addresses are pool accounts.
    function setAccountSource(IAccountSource source) external onlyOperator {
        if (address(accounts) != address(0)) revert SourceAlreadySet();
        if (address(source) == address(0)) revert ZeroAddress();
        accounts = source;
        emit AccountSourceSet(address(source));
    }

    function transferOperator(address to) external onlyOperator {
        pendingOperator = to;
        emit OperatorTransferStarted(operator, to);
    }

    function acceptOperator() external {
        if (msg.sender != pendingOperator) revert NotPendingOperator();
        emit OperatorTransferred(operator, msg.sender);
        operator = msg.sender;
        pendingOperator = address(0);
    }

    /// @notice Adds key addresses to the free list. Each must be new to this registry and
    ///         unknown to HyperCore: an agent address has no account of its own.
    function publish(address[] calldata keys) external onlyOperator {
        for (uint256 i = 0; i < keys.length; ++i) {
            address key = keys[i];
            if (key == address(0)) revert ZeroAddress();
            if (_bindings[key].state != State.Unknown) revert KeyNotNew(key);
            if (CoreOps.exists(key)) revert KeyExistsOnCore(key);
            _bindings[key].state = State.Free;
            _free.push(key);
            emit KeyPublished(key);
        }
    }

    // ── accounts ─────────────────────────────────────────────────────────────────────

    /// @notice Hands the next free key to the calling account for `trader`. An account can
    ///         only take a key for itself, and only one at a time. Keys that gained a
    ///         HyperCore account on the way are retired and skipped.
    function assign(address trader) external returns (address key) {
        if (!accounts.isAccount(msg.sender)) revert NotAnAccount(msg.sender);
        address current = keyOf[msg.sender];
        if (current != address(0)) revert AccountHasKey(msg.sender, current);
        for (uint256 skipped = 0;; ++skipped) {
            if (_free.length == 0) revert NoFreeKey();
            key = _free[_free.length - 1];
            if (!CoreOps.exists(key)) break;
            if (skipped == MAX_SPOILED_SKIPS) revert TooManySpoiledKeys();
            _free.pop();
            _spoil(key);
        }
        _free.pop();
        _bindings[key] = Binding({state: State.Bound, account: msg.sender, trader: trader});
        keyOf[msg.sender] = key;
        emit KeyBound(key, msg.sender, trader);
    }

    /// @notice Retires up to `max` keys from the top of the free list that have gained a
    ///         HyperCore account since they were published. Anyone may call it, and
    ///         `assign` needs it once more than MAX_SPOILED_SKIPS such keys are stacked up.
    function purgeSpoiled(uint256 max) external returns (uint256 purged) {
        while (purged < max && _free.length != 0) {
            address key = _free[_free.length - 1];
            if (!CoreOps.exists(key)) break;
            _free.pop();
            _spoil(key);
            ++purged;
        }
    }

    function _spoil(address key) internal {
        _bindings[key].state = State.Retired;
        emit KeySpoiled(key);
    }

    /// @notice The account a key is bound to gives it up for good.
    function retire(address key) external {
        Binding storage b = _bindings[key];
        if (b.state != State.Bound || b.account != msg.sender) revert NotBoundHere(key, msg.sender);
        b.state = State.Retired;
        if (keyOf[msg.sender] == key) keyOf[msg.sender] = address(0);
        emit KeyRetired(key, msg.sender);
    }

    // ── views ────────────────────────────────────────────────────────────────────────

    function bindingOf(address key) external view returns (Binding memory) {
        return _bindings[key];
    }

    function freeCount() external view returns (uint256) {
        return _free.length;
    }

    /// @notice What the pool gateway asks before it forwards an order: is `key` currently
    ///         bound to `account` on behalf of `trader`?
    function isBound(address key, address account, address trader) external view returns (bool) {
        Binding memory b = _bindings[key];
        return b.state == State.Bound && b.account == account && b.trader == trader;
    }
}
