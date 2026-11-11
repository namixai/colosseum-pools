// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {CoreOps} from "./lib/CoreOps.sol";

interface IKeyAccounts {
    function isAccount(address account) external view returns (bool);
}

/// What an account does when a key reaches it, or is taken from it.
interface IBoundAccount {
    function onKeyBound(address key, KeyBinder.Purpose purpose) external;
    function onKeyRevoked(address key, bytes32 salt) external;
}

/// @title KeyBinder
/// @notice Binds a Hyperliquid agent key to an account that asked for one, on the word of the
///         publisher: a key born in the signing enclave, appointed by the owner. Nothing waits
///         here in advance. An account that needs a key opens a request; the publisher signs a
///         fresh key for that request alone; anyone carries the signature in; the account approves
///         the key in the same transaction (docs/KEY-BINDING.md).
/// @dev A key moves Unknown -> Bound -> Retired and never back: Hyperliquid warns that actions
///      signed by a removed agent can be replayed once its nonces are pruned, so a key that has
///      served one account never serves another. `Free` is never used; it keeps the states where
///      the key registry of the earlier deployments has them, for the readers of both.
///
///      Roles. The owner (a multisig) appoints the publisher and the guardian, sets the daily
///      limit and the renewal delay, and alone lifts a pause. The guardian can only stop: pause,
///      lower the limit, revoke a bound key. The publisher only signs.
///
///      What this contract can't show: that the publisher is the enclave. `setPublisher` records
///      the hashes of the enclave's measurements and of its attestation document, which is
///      published beside it; anyone can check the one against the other.
contract KeyBinder is EIP712 {
    enum State {
        Unknown,
        Free,
        Bound,
        Retired
    }

    enum Purpose {
        None,
        Start,
        Funded,
        Renew,
        Rekey,
        Revoked
    }

    struct Binding {
        State state;
        address account;
        address trader;
    }

    struct Request {
        Purpose purpose;
        address trader;
        uint64 id;
        uint64 openedAt;
        /// A renewal's key, committed and not yet shown, and when it may be shown.
        bytes32 keyHash;
        uint64 activatesAt;
    }

    bytes32 public constant BINDING_TYPEHASH =
        keccak256("Binding(address account,uint64 request,address trader,bytes32 keyHash)");
    /// A renewal opens in the last fourteen days of a term; a delay past half of that would let
    /// the term run out first.
    uint64 public constant MAX_RENEW_DELAY = 7 days;

    address public owner;
    address public pendingOwner;
    address public publisher;
    address public guardian;
    IKeyAccounts public accounts;
    bool public paused;
    uint32 public maxBindsPerDay;
    uint32 public bindsToday;
    uint32 public bindDay;
    uint64 public renewDelay;

    mapping(address key => Binding) private _bindings;
    mapping(address account => address key) public keyOf;
    mapping(address account => Request) private _requests;
    mapping(address account => uint64) public requestCount;

    event OwnershipTransferStarted(address indexed from, address indexed to);
    event OwnershipTransferred(address indexed from, address indexed to);
    event AccountSourceSet(address indexed source);
    event PublisherSet(address indexed publisher, bytes32 pcrHash, bytes32 attestationDocHash);
    event GuardianSet(address indexed guardian);
    event MaxBindsSet(uint32 perDay);
    event RenewDelaySet(uint64 delay);
    event Paused(address indexed by);
    event Unpaused();
    event KeyRequested(address indexed account, address indexed trader, Purpose purpose, uint64 id);
    event RequestCancelled(address indexed account, uint64 id);
    event KeyCommitted(address indexed account, uint64 id, bytes32 keyHash, uint64 activatesAt);
    event KeyBound(address indexed key, address indexed account, address indexed trader, Purpose purpose, uint64 id);
    event KeyRetired(address indexed key, address indexed account);
    event KeyRevoked(address indexed key, address indexed account, address indexed by);

    error NotOwner();
    error NotPendingOwner();
    error NotGuardian();
    error SourceAlreadySet();
    error ZeroAddress();
    error ZeroHash();
    error NotAnAccount(address caller);
    error BadPurpose();
    error Asked(address account);
    error NotAsked(address account);
    error WrongStep();
    error IsPaused();
    error DailyLimit();
    error NotLower();
    error DelayTooLong();
    error BadSignature();
    error KeyNotNew(address key);
    error KeyExistsOnCore(address key);
    error TooEarly(uint64 activatesAt);
    error WrongKey();
    error NotBoundHere(address key, address caller);

    constructor(address owner_, uint32 maxBindsPerDay_, uint64 renewDelay_) EIP712("colosseum-pools KeyBinder", "1") {
        if (owner_ == address(0)) revert ZeroAddress();
        if (renewDelay_ > MAX_RENEW_DELAY) revert DelayTooLong();
        owner = owner_;
        maxBindsPerDay = maxBindsPerDay_;
        renewDelay = renewDelay_;
        emit OwnershipTransferred(address(0), owner_);
        emit MaxBindsSet(maxBindsPerDay_);
        emit RenewDelaySet(renewDelay_);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyGuardianOrOwner() {
        if (msg.sender != guardian && msg.sender != owner) revert NotGuardian();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert IsPaused();
        _;
    }

    // ── owner ────────────────────────────────────────────────────────────────────────

    /// @notice One-time link to the factory that says which addresses are pool accounts.
    function setAccountSource(IKeyAccounts source) external onlyOwner {
        if (address(accounts) != address(0)) revert SourceAlreadySet();
        if (address(source) == address(0)) revert ZeroAddress();
        accounts = source;
        emit AccountSourceSet(address(source));
    }

    /// @notice Appoints the publisher. Neither hash may be zero: an enclave in debug mode reports
    ///         its measurements as zeros, and a publisher without a document to check is no
    ///         better than a hot key.
    function setPublisher(address publisher_, bytes32 pcrHash, bytes32 attestationDocHash) external onlyOwner {
        if (publisher_ == address(0)) revert ZeroAddress();
        if (pcrHash == bytes32(0) || attestationDocHash == bytes32(0)) revert ZeroHash();
        publisher = publisher_;
        emit PublisherSet(publisher_, pcrHash, attestationDocHash);
    }

    function setGuardian(address guardian_) external onlyOwner {
        guardian = guardian_;
        emit GuardianSet(guardian_);
    }

    function setMaxBindsPerDay(uint32 perDay) external onlyOwner {
        maxBindsPerDay = perDay;
        emit MaxBindsSet(perDay);
    }

    function setRenewDelay(uint64 delay) external onlyOwner {
        if (delay > MAX_RENEW_DELAY) revert DelayTooLong();
        renewDelay = delay;
        emit RenewDelaySet(delay);
    }

    function unpause() external onlyOwner {
        paused = false;
        emit Unpaused();
    }

    function transferOwnership(address to) external onlyOwner {
        pendingOwner = to;
        emit OwnershipTransferStarted(owner, to);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();
        emit OwnershipTransferred(owner, msg.sender);
        owner = msg.sender;
        pendingOwner = address(0);
    }

    // ── guardian, or owner ───────────────────────────────────────────────────────────

    /// @notice Stops every binding, commitment and reveal until the owner lifts it. Requests,
    ///         cancels and retirements go on: an account can always give a key up.
    function pause() external onlyGuardianOrOwner {
        paused = true;
        emit Paused(msg.sender);
    }

    function lowerMaxBindsPerDay(uint32 perDay) external onlyGuardianOrOwner {
        if (perDay >= maxBindsPerDay) revert NotLower();
        maxBindsPerDay = perDay;
        emit MaxBindsSet(perDay);
    }

    /// @notice Takes a bound key from its account for good. The account cuts it as a stop does,
    ///         `salt` picking the keyless address, and asks for a fresh key, which only a
    ///         binding can give it.
    function revoke(address key, bytes32 salt) external onlyGuardianOrOwner {
        Binding storage b = _bindings[key];
        if (b.state != State.Bound) revert NotBoundHere(key, msg.sender);
        address account = b.account;
        b.state = State.Retired;
        delete keyOf[account];
        _cancel(account);
        emit KeyRevoked(key, account, msg.sender);
        IBoundAccount(account).onKeyRevoked(key, salt);
    }

    // ── accounts ─────────────────────────────────────────────────────────────────────

    /// @notice The calling account asks for a key for `trader`. One request at a time.
    function request(address trader, Purpose purpose) external returns (uint64 id) {
        if (!accounts.isAccount(msg.sender)) revert NotAnAccount(msg.sender);
        // A key for nobody would be signed and bound, and serve no order: the accounts never ask for
        // one, and the binder does not take the request.
        if (trader == address(0)) revert ZeroAddress();
        if (purpose == Purpose.None) revert BadPurpose();
        if (_requests[msg.sender].purpose != Purpose.None) revert Asked(msg.sender);
        id = ++requestCount[msg.sender];
        _requests[msg.sender] = Request({
            purpose: purpose, trader: trader, id: id, openedAt: uint64(block.timestamp), keyHash: bytes32(0), activatesAt: 0
        });
        emit KeyRequested(msg.sender, trader, purpose, id);
    }

    /// @notice The calling account withdraws its request, if it has one.
    function cancel() external {
        _cancel(msg.sender);
    }

    /// @notice The account a key is bound to gives it up for good.
    function retire(address key) external {
        Binding storage b = _bindings[key];
        if (b.state != State.Bound || b.account != msg.sender) revert NotBoundHere(key, msg.sender);
        b.state = State.Retired;
        delete keyOf[msg.sender];
        emit KeyRetired(key, msg.sender);
    }

    // ── anyone, on the publisher's word ──────────────────────────────────────────────

    /// @notice Binds `key` to the request `account` has open, the publisher having signed it, and
    ///         lets the account approve it. A renewal while the delay is on goes by `commit`.
    function bind(address account, address key, bytes calldata signature) external whenNotPaused {
        Request storage r = _requests[account];
        if (r.purpose == Purpose.None) revert NotAsked(account);
        if (r.keyHash != bytes32(0) || (r.purpose == Purpose.Renew && renewDelay != 0)) revert WrongStep();
        _check(account, r, keccak256(abi.encode(key)), signature);
        _take();
        _bind(account, key, r);
    }

    /// @notice A renewal's key, signed and committed by its hash. It can be shown and approved
    ///         once the delay has passed, and until then nobody can spoil it, nor see it.
    function commit(address account, bytes32 keyHash, bytes calldata signature) external whenNotPaused {
        Request storage r = _requests[account];
        if (r.purpose != Purpose.Renew || renewDelay == 0 || r.keyHash != bytes32(0)) revert WrongStep();
        _check(account, r, keyHash, signature);
        _take();
        r.keyHash = keyHash;
        r.activatesAt = uint64(block.timestamp) + renewDelay;
        emit KeyCommitted(account, r.id, keyHash, r.activatesAt);
    }

    /// @notice Shows a committed key once its delay has passed, and binds it.
    function reveal(address account, address key) external whenNotPaused {
        Request storage r = _requests[account];
        if (r.keyHash == bytes32(0)) revert WrongStep();
        if (block.timestamp < r.activatesAt) revert TooEarly(r.activatesAt);
        if (keccak256(abi.encode(key)) != r.keyHash) revert WrongKey();
        _bind(account, key, r);
    }

    function _check(address account, Request storage r, bytes32 keyHash, bytes calldata signature) internal view {
        if (ECDSA.recover(digest(account, r.id, r.trader, keyHash), signature) != publisher) revert BadSignature();
    }

    function _take() internal {
        uint32 today = uint32(block.timestamp / 1 days);
        if (today != bindDay) {
            bindDay = today;
            bindsToday = 0;
        }
        if (bindsToday >= maxBindsPerDay) revert DailyLimit();
        ++bindsToday;
    }

    function _bind(address account, address key, Request storage r) internal {
        if (key == address(0)) revert ZeroAddress();
        if (_bindings[key].state != State.Unknown) revert KeyNotNew(key);
        // An agent address has no account of its own: HyperCore takes no such address as an agent.
        if (CoreOps.exists(key)) revert KeyExistsOnCore(key);
        Purpose purpose = r.purpose;
        address trader = r.trader;
        uint64 id = r.id;
        address old = keyOf[account];
        if (old != address(0)) {
            _bindings[old].state = State.Retired;
            emit KeyRetired(old, account);
        }
        _bindings[key] = Binding({state: State.Bound, account: account, trader: trader});
        keyOf[account] = key;
        delete _requests[account];
        emit KeyBound(key, account, trader, purpose, id);
        IBoundAccount(account).onKeyBound(key, purpose);
    }

    function _cancel(address account) internal {
        uint64 id = _requests[account].id;
        if (_requests[account].purpose == Purpose.None) return;
        delete _requests[account];
        emit RequestCancelled(account, id);
    }

    // ── views ────────────────────────────────────────────────────────────────────────

    /// @notice What the publisher signs for a request: EIP-712, under this contract's address and
    ///         the chain's id, so a signature is good for one request on one deployment.
    function digest(address account, uint64 id, address trader, bytes32 keyHash) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(BINDING_TYPEHASH, account, id, trader, keyHash)));
    }

    function requestOf(address account) external view returns (Request memory) {
        return _requests[account];
    }

    function bindingOf(address key) external view returns (Binding memory) {
        return _bindings[key];
    }

    /// @notice What the pool gateway asks before it forwards an order: is `key` currently bound
    ///         to `account` on behalf of `trader`?
    function isBound(address key, address account, address trader) external view returns (bool) {
        Binding memory b = _bindings[key];
        return b.state == State.Bound && b.account == account && b.trader == trader;
    }
}
