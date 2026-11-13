// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Rules, Units} from "../Types.sol";
import {PaperExam, ExamTerms} from "../exam/PaperExam.sol";
import {SimPool, SimPlan, SimSize, PAYOUTS_A_DAY} from "./SimPool.sol";
import {TermsBook} from "../multi/TermsBook.sol";

/// @title SimPoolFactory
/// @notice Makes simulated pools: both stages are paper exams, nobody's capital is traded, and a
///         pass is paid out of the prices other traders paid. Anyone opens one for 1 HYPE and the
///         money for two payouts of its largest size. The platform's numbers are constants here,
///         the same for every pool: what a pool pays the platform, what it pays a trader, what it
///         keeps, how long a payout waits, how many payouts a pool pays a day, how long a pool runs at
///         least, how long a withdrawal waits.
/// @dev The operator lists the assets and their leverage caps, and names the four roles the
///      pools read from here: the filler of every exam's orders, the checker who may stop a
///      payout with a reason, the closer who alone may close a pool before `MIN_TERM` and may cancel
///      a creator's request to withdraw, and the
///      recipient of the platform's part. A pool's creator is neither the filler, the checker
///      nor the closer.
contract SimPoolFactory {
    using SafeERC20 for IERC20;

    /// What opening a pool costs, in HYPE (18 decimals), paid to the platform.
    uint256 public constant CREATION_FEE = 1 ether;
    /// The platform's part of every exam's price, out of the price, and of every withdrawal (bps).
    uint16 public constant FEE_BPS = 50;
    /// A payout, out of the size the trader passed on (bps): 8 %.
    uint16 public constant PAYOUT_BPS = 800;
    /// Payouts of the largest size a pool locks when it opens.
    uint8 public constant LOCKED_PAYOUTS = 2;
    /// Payouts of the largest size a pool keeps while it is open: the creator withdraws above this.
    uint8 public constant FLOOR_PAYOUTS = 3;
    /// How long a payout waits, for the checker to stop it with a reason.
    uint32 public constant PAYOUT_DELAY = 72 hours;
    /// How long after its claim a payout goes out without the pool's approver: before it, `pay` pays a
    /// payout only once approved; from it, whether approved or not. The trader's money never depends on
    /// the approver for longer.
    uint32 public constant PAYOUT_HARD = 7 days;
    /// How many payouts a pool settles at most in any `PAYOUT_DAY`, however they came due, approved or by
    /// `PAYOUT_HARD`: one more waits in its place for the next day. Whatever the rules let through, a pool
    /// settles no more than this many payouts in a day, the largest size at most each (CFO, 13 November).
    uint8 public constant PAYOUTS_PER_DAY = uint8(PAYOUTS_A_DAY);
    uint32 public constant PAYOUT_DAY = 24 hours;
    /// How long a stopped payout's money stays set aside after the stop: the trader can object and the
    /// checker can lift the stop; after it the stop is final and the money is the pool's again.
    uint32 public constant STOP_RESERVE = 14 days;
    /// How long a pool runs at least: six months, the longest half-year (1 August to 1 February), so "at
    /// least 6 months" holds whatever the day it opens. Before it, its creator neither stops its sales nor
    /// takes back what it put in; only the factory's closer may close it, and the money goes where it would anyway.
    uint32 public constant MIN_TERM = 184 days;
    /// How long a creator's request to withdraw waits before it can be carried out, and how long after
    /// closing a pool `finish` waits: the platform, the traders and the codes' partners see every
    /// withdrawal a week before the money moves.
    uint32 public constant WITHDRAW_DELAY = 7 days;
    /// How long a request stays good once it is ready: one not carried out by then lapses, so a
    /// request cannot be left standing to make the wait nothing.
    uint32 public constant WITHDRAW_WINDOW = 3 days;
    /// The bounds of a fill window, in blocks: about a minute of HyperEVM's small blocks at most.
    uint16 public constant MIN_FILL_WINDOW = 1;
    uint16 public constant MAX_FILL_WINDOW = 60;
    /// The share of a withdrawal's profit a trading group's code may give its owner (bps), when it gives one.
    uint16 public constant MIN_PARTNER_BPS = 4000;
    uint16 public constant MAX_PARTNER_BPS = 6000;
    /// How many codes one pool may have: a withdrawal pays every code's partner in one loop.
    uint8 public constant MAX_CODES = 32;
    uint256 public constant MAX_MENU = 8;
    uint32 public constant MIN_FUND_WINDOW = 1 hours;
    uint32 public constant MAX_FUND_WINDOW = 30 days;
    /// The most a fill at the book may be worse than the oracle price, in bps of it: PaperExam's bound.
    uint16 public constant MAX_WORSE_BPS = 500;
    /// The most leverage any stage may allow, times 100: 10x.
    uint32 public constant MAX_LEVERAGE_X100 = 1000;

    IERC20 public immutable usdc;
    address public immutable examImpl;
    address public immutable poolImpl;

    address public operator;
    address public filler;
    address public checker;
    /// The platform's key that alone may close a pool before `MIN_TERM` (`SimPool.close`).
    address public closer;
    address public feeRecipient;
    /// Where every pool's terms are written out: a pool names the hash of a text kept there.
    TermsBook public termsBook;
    /// Each listed asset's leverage cap, times 100; zero for an asset no pool may trade.
    mapping(uint32 asset => uint32) public assetCapX100;
    /// The pool each pool and each of its exams and stages belongs to.
    mapping(address => address) public poolOf;
    address[] internal _pools;

    event OperatorSet(address indexed operator);
    event FillerSet(address indexed filler);
    event CheckerSet(address indexed checker);
    event CloserSet(address indexed closer);
    event FeeRecipientSet(address indexed recipient);
    event TermsBookSet(address indexed book);
    event AssetSet(uint32 indexed asset, uint32 capX100);
    event PoolCreated(address indexed pool, address indexed creator, uint256 locked);

    error NotOperator();
    error NotPool();
    error BadRole();
    error BadPlan();
    error WrongFee();
    error FeeNotSent();
    error Unpublished();

    modifier onlyOperator() {
        if (msg.sender != operator) revert NotOperator();
        _;
    }

    constructor(IERC20 usdc_, address examImpl_, address poolImpl_, address operator_) {
        if (address(usdc_) == address(0) || examImpl_ == address(0) || poolImpl_ == address(0)) revert BadRole();
        if (operator_ == address(0)) revert BadRole();
        usdc = usdc_;
        examImpl = examImpl_;
        poolImpl = poolImpl_;
        operator = operator_;
        emit OperatorSet(operator_);
    }

    // ── operator ─────────────────────────────────────────────────

    function setOperator(address operator_) external onlyOperator {
        if (operator_ == address(0)) revert BadRole();
        operator = operator_;
        emit OperatorSet(operator_);
    }

    /// @notice The filler of the orders of every exam sold from now on; an exam keeps the one it
    ///         started with. Not the checker: one key fills, another stops payouts.
    function setFiller(address filler_) external onlyOperator {
        if (filler_ == address(0) || filler_ == checker) revert BadRole();
        filler = filler_;
        emit FillerSet(filler_);
    }

    /// @notice The checker every pool reads when a payout is stopped or a stop lifted. Not the filler.
    function setChecker(address checker_) external onlyOperator {
        if (checker_ == address(0) || checker_ == filler) revert BadRole();
        checker = checker_;
        emit CheckerSet(checker_);
    }

    /// @notice The key that may close any pool of the factory before its term. Closing early changes
    ///         nothing about where a pool's money goes; the closer gets none of it.
    function setCloser(address closer_) external onlyOperator {
        if (closer_ == address(0)) revert BadRole();
        closer = closer_;
        emit CloserSet(closer_);
    }

    function setFeeRecipient(address recipient) external onlyOperator {
        if (recipient == address(0)) revert BadRole();
        feeRecipient = recipient;
        emit FeeRecipientSet(recipient);
    }

    function setTermsBook(TermsBook book) external onlyOperator {
        if (address(book).code.length == 0 || address(book).codehash != keccak256(type(TermsBook).runtimeCode)) {
            revert BadRole();
        }
        termsBook = book;
        emit TermsBookSet(address(book));
    }

    /// @notice Lists `asset` for new pools with its leverage cap, or delists it with zero. Pools
    ///         already open keep what they were checked against.
    function setAsset(uint32 asset, uint32 capX100) external onlyOperator {
        if (capX100 != 0 && (capX100 < 100 || capX100 > MAX_LEVERAGE_X100)) revert BadPlan();
        assetCapX100[asset] = capX100;
        emit AssetSet(asset, capX100);
    }

    // ── anyone ───────────────────────────────────────────────────

    /// @notice Opens a pool on `plan` for 1 HYPE, sent with the call, and the money for
    ///         `LOCKED_PAYOUTS` payouts of its largest size, taken from the caller.
    function create(SimPlan calldata plan) external payable returns (address pool) {
        if (msg.value != CREATION_FEE) revert WrongFee();
        address to = feeRecipient;
        // No pool without a closer: before its term, only the closer can close it (review of #279).
        if (filler == address(0) || checker == address(0) || closer == address(0) || to == address(0)) revert BadRole();
        if (msg.sender == filler || msg.sender == checker || msg.sender == closer) revert BadRole();
        uint256 largest = _check(plan);

        pool = Clones.clone(poolImpl);
        poolOf[pool] = pool;
        _pools.push(pool);
        uint256 locked = (largest * PAYOUT_BPS / Units.BPS) * LOCKED_PAYOUTS;
        SimPool(pool).initialize(msg.sender, plan);
        usdc.safeTransferFrom(msg.sender, pool, locked);
        SimPool(pool).noteLocked(locked);
        emit PoolCreated(pool, msg.sender, locked);

        (bool ok,) = to.call{value: msg.value}("");
        if (!ok) revert FeeNotSent();
    }

    /// @notice A pool notes an exam or a stage it started, so that a stop elsewhere can name it.
    function noteAccount(address account) external {
        if (poolOf[msg.sender] != msg.sender) revert NotPool();
        poolOf[account] = msg.sender;
    }

    function pools() external view returns (address[] memory) {
        return _pools;
    }

    /// @notice Reverts unless `plan` could open a pool now; returns the largest size of its menu.
    function check(SimPlan calldata plan) external view returns (uint256) {
        return _check(plan);
    }

    // ── internals ────────────────────────────────────────────────

    function _check(SimPlan calldata p) internal view returns (uint256 largest) {
        uint256 n = p.menu.length;
        if (n == 0 || n > MAX_MENU) revert BadPlan();
        for (uint256 i = 0; i < n; ++i) {
            SimSize calldata m = p.menu[i];
            if (m.size == 0 || m.price == 0) revert BadPlan();
            if (m.size > largest) largest = m.size;
            // A signed purchase names an amount, not a line: no two lines at one price (review of cp#261).
            for (uint256 j = 0; j < i; ++j) {
                if (p.menu[j].price == m.price) revert BadPlan();
            }
        }
        _checkRules(p.examRules);
        _checkRules(p.fundedRules);
        _checkTerms(p.exam);
        _checkTerms(p.funded);
        // Everything PaperExam will refuse at an exam's start, refused at the pool's (review of cp#261).
        _examCheck(p.examRules, p.exam, uint64(largest));
        _examCheck(p.fundedRules, p.funded, uint64(largest));
        if (p.funded.targetsBps.length != 1) revert BadPlan();
        if (p.fundWindow < MIN_FUND_WINDOW || p.fundWindow > MAX_FUND_WINDOW) revert BadPlan();
        if (p.fromBook ? p.maxWorseBps == 0 || p.maxWorseBps > MAX_WORSE_BPS : p.maxWorseBps != 0) revert BadPlan();
        if (address(termsBook) == address(0) || termsBook.textAt(p.terms) == address(0)) revert Unpublished();
    }

    /// Every asset listed, and each one's cap, its own or the account's, within the list's.
    function _checkRules(Rules calldata r) internal view {
        if (r.maxLeverageX100 > MAX_LEVERAGE_X100) revert BadPlan();
        uint256 n = r.assets.length;
        bool own = r.assetLeverageX100.length != 0;
        // One cap for each asset, or none: a shorter list would be read past its end (review of cp#261).
        if (own && r.assetLeverageX100.length != n) revert BadPlan();
        for (uint256 i = 0; i < n; ++i) {
            uint32 listed = assetCapX100[r.assets[i]];
            uint32 cap = own ? r.assetLeverageX100[i] : r.maxLeverageX100;
            if (listed == 0 || cap > listed) revert BadPlan();
        }
    }

    function _examCheck(Rules calldata r, ExamTerms calldata t, uint64 capital) internal view {
        ExamTerms memory m = t;
        m.capital = capital;
        try PaperExam(examImpl).check(r, m) {}
        catch {
            revert BadPlan();
        }
    }

    /// A stage that always ends, and a fill window of a minute at most. The rest PaperExam checks.
    function _checkTerms(ExamTerms calldata t) internal pure {
        if (t.stepDuration == 0 || t.maxIdle == 0) revert BadPlan();
        if (t.fillWindow < MIN_FILL_WINDOW || t.fillWindow > MAX_FILL_WINDOW) revert BadPlan();
    }
}
