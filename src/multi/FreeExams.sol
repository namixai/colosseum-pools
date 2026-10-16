// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

/// The one call a pool of many traders answers for this contract.
interface IFreeSeller {
    function sellFree(address trader, uint8 line) external returns (address exam);
    function value() external view returns (uint256);
}

/// @title FreeExams
/// @notice A pool's free exams: the invitations its creator signs, each number serving once, and
///         the month's count. The pool creates it in its own constructor, so no other pool can be
///         paired with it, and sells a free exam to whoever this contract names.
/// @dev Kept out of `MultiPool`, which is close to the size a contract may have.
///      An invitation is the inviter's EIP-712 signature of
///      `Invite(address trader,uint8 line,uint64 nonce,uint64 expiresAt)` under this contract's
///      domain (`domainSeparator()`). Who is invited is the inviter's choice, made off chain; this
///      contract checks only the signature, the number, the time and the month's count.
contract FreeExams {
    /// The month free exams are counted in, from the pool's start.
    uint32 public constant MONTH = 30 days;
    /// The pool's value each free exam of a month needs (1e8 = 1 USDC): no more than 10 a month for
    /// every 100,000 USDC in the pool (Alex, 15 October 2026), whatever the plan says.
    uint256 public constant VALUE_PER_FREE_EXAM = 10_000e8;
    bytes32 public constant INVITE_TYPEHASH =
        keccak256("Invite(address trader,uint8 line,uint64 nonce,uint64 expiresAt)");
    bytes32 private constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");

    address public immutable pool;
    /// Who signs the invitations: the pool's creator.
    address public immutable inviter;
    uint16 public immutable perMonth;
    uint64 public immutable startedAt;

    /// The month counted in now, and how many free exams were taken in it.
    uint32 public month;
    uint16 public taken;
    /// Invitation numbers used or withdrawn, by the inviter's numbering.
    mapping(uint64 nonce => bool) public spent;

    event FreeExamTaken(address indexed exam, address indexed trader, uint64 nonce);
    event InviteWithdrawn(uint64 nonce);

    error NotInvited();
    error InviteSpent();
    error InviteLapsed();
    error NoFreeLeft();

    constructor(address inviter_, uint16 perMonth_) {
        pool = msg.sender;
        inviter = inviter_;
        perMonth = perMonth_;
        startedAt = uint64(block.timestamp);
    }

    /// @notice Takes a free exam on an invitation (v, r, s) signed for the caller. The exam is the
    ///         pool's menu line at no price, sold under the pool's own limits.
    function take(uint8 line, uint64 nonce, uint64 expiresAt, uint8 v, bytes32 r, bytes32 s)
        external
        returns (address exam)
    {
        if (block.timestamp > expiresAt) revert InviteLapsed();
        if (spent[nonce]) revert InviteSpent();
        bytes32 invite = keccak256(abi.encode(INVITE_TYPEHASH, msg.sender, line, nonce, expiresAt));
        // A second form of the same signature serves nothing: the number is spent either way.
        address signer = ecrecover(keccak256(abi.encodePacked("\x19\x01", domainSeparator(), invite)), v, r, s);
        // The pool makes this contract only with an inviter, so a signature that recovers to
        // nobody is never the inviter's.
        if (signer != inviter) revert NotInvited();
        uint32 m = uint32((block.timestamp - startedAt) / MONTH);
        uint16 n = m == month ? taken : 0;
        if (n >= perMonth || n >= IFreeSeller(pool).value() / VALUE_PER_FREE_EXAM) revert NoFreeLeft();
        month = m;
        taken = n + 1;
        spent[nonce] = true;
        exam = IFreeSeller(pool).sellFree(msg.sender, line);
        emit FreeExamTaken(exam, msg.sender, nonce);
    }

    /// @notice The inviter withdraws an invitation nobody has taken yet.
    function withdraw(uint64 nonce) external {
        if (msg.sender != inviter) revert NotInvited();
        spent[nonce] = true;
        emit InviteWithdrawn(nonce);
    }

    /// @notice The EIP-712 domain an invitation is signed under: this contract on this chain.
    function domainSeparator() public view returns (bytes32) {
        return
            keccak256(abi.encode(DOMAIN_TYPEHASH, keccak256("FreeExams"), keccak256("1"), block.chainid, address(this)));
    }
}
