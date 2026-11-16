// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {Rules, Units} from "../Types.sol";
import {PaperExam, ExamTerms} from "../exam/PaperExam.sol";
import {SimPoolFactory} from "./SimPoolFactory.sol";
import {SimPoolStops} from "./SimPoolStops.sol";
import {SimPoolSetup} from "./SimPoolSetup.sol";
import {SimPoolCodes, ACCRUAL_SCALE} from "./SimPoolCodes.sol";

/// @notice The part of USDC's EIP-3009 a pool takes a price through: Circle's FiatToken V2.2, on
///         HyperEVM and its testnet. The token itself checks the signature, the time, the nonce,
///         and that the caller is the payee.
interface IReceiveWithAuthorization {
    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external;

    function authorizationState(address authorizer, bytes32 nonce) external view returns (bool);
}

/// @notice One line of a simulated pool's menu.
struct SimSize {
    /// What the exam and then the stage are taken on: a number, not money (1e6 = 1 USDC).
    uint64 size;
    /// What the exam costs, in HyperEVM USDC units (6 decimals).
    uint64 price;
}

/// @notice One tier of a simulated pool's menu, line by line with `menu`: what both stages of the line are judged by.
struct SimTier {
    /// The loss limit of both stages, in bps of the line's size: it takes the place of the plan's `maxDrawdownBps`.
    uint16 maxDrawdownBps;
    /// The profit target of every step of both stages, in bps of the line's size: it takes the place of the
    /// plan's `targetsBps`, whose length alone is read (how many steps).
    uint16 targetBps;
    /// How the loss line of both stages is drawn: `PaperExam.FROM_START` or `PaperExam.TRAILING_TO_START`.
    uint8 drawdownKind;
}

/// @notice One line of a pool's menu as the pool keeps it, in one storage slot: its size and price (`SimSize`), its
///         tier (`SimTier`), and how many of its exams and stages are still running or able to pay.
struct SimLine {
    uint64 size;
    uint64 price;
    uint16 maxDrawdownBps;
    uint16 targetBps;
    uint8 drawdownKind;
    /// Exams and stages of the line not yet done (`SimPool.Account.done`): what `SimPool.floor` counts in.
    uint32 live;
}

/// @notice What a pool keeps once for every line of its menu (`SimPoolSetup`): the rules and terms both stages start
///         on, but for what each line's tier sets (`maxDrawdownBps`, the values of `targetsBps`, the kind of loss line)
///         and its size (`capital`), put in when an exam or a stage starts.
struct SimRules {
    Rules examRules;
    ExamTerms exam;
    Rules fundedRules;
    ExamTerms funded;
    /// The plan's `fromBook` and `maxWorseBps`, the same for both stages.
    bool fromBook;
    uint16 maxWorseBps;
}

/// @notice Everything the creator of a simulated pool sets, at creation.
struct SimPlan {
    SimSize[] menu;
    /// The rules of the exam, but for `maxDrawdownBps`, which each line's tier sets.
    Rules examRules;
    /// The exam's terms. `capital` is not read: every exam takes the size it was bought at; nor the values of
    /// `targetsBps`, which each line's tier sets.
    ExamTerms exam;
    /// The rules of the stage, but for `maxDrawdownBps`, as for the exam.
    Rules fundedRules;
    /// The stage after the exam: one step. Neither `capital` nor the value of its target is read either.
    ExamTerms funded;
    /// How long a trader who passed the exam has to start the stage, in seconds.
    uint32 fundWindow;
    /// Whether both stages fill at the book, never better than the oracle and at most `maxWorseBps`
    /// worse than it (`PaperExam`); zero `maxWorseBps` without the book.
    bool fromBook;
    uint16 maxWorseBps;
    /// The keccak256 of the pool's terms, every clause written out, as a `TermsBook` keeps them.
    bytes32 terms;
    /// Each line's tier, in the order of `menu`, one for each line.
    SimTier[] tiers;
    /// How many payouts the pool settles at most in any `PAYOUT_DAY`, within the factory's `MIN_PAYOUTS_PER_DAY`
    /// and `MAX_PAYOUTS_PER_DAY` (CFO, 13 November: a number of the pool, two for the first).
    uint8 payoutsPerDay;
    /// What the creator locks at the start, in HyperEVM USDC units: at least `COVER_PAYOUTS` payouts of the smallest
    /// line (CFO, 16 November).
    uint256 locked;
    /// The lines on at the start, bit `i` for line `i` of `menu`: only lines whose `COVER_PAYOUTS` payouts `locked`
    /// covers. The others start off; the creator switches a line on later, when the pool has the money (`setLine`).
    uint32 on;
}

// The most payouts any pool settles in a `PAYOUT_DAY`, and so how many times of settlement a pool has room to keep:
// the factory publishes it as `MAX_PAYOUTS_PER_DAY`.
uint256 constant MAX_PAYOUTS_A_DAY = 8;

// How many payouts of a line the pool must have free for the line to be switched on or sold, and what the creator's
// lock covers of every line on at the start (CFO, 16 November: K = 2). The factory publishes it as `LOCKED_PAYOUTS`.
uint256 constant COVER_PAYOUTS = 2;

/// @title SimPool
/// @notice A pool where both stages are paper exams and nobody's capital is traded. A trader buys
///         an exam off the menu; whoever passes it starts the stage on the same size; whoever passes
///         the stage is paid 8 % of the size, once, out of the pool's USDC: the prices traders paid,
///         and what its creator locked at the start. A payout waits 72 hours and the approval of the
///         approver the creator names, or 7 days without it; until it is paid the factory's checker
///         alone may stop it, with a reason and the transactions that show it; with no stop it goes to
///         the trader, sent by anyone. A pool settles at most `payoutsPerDay` payouts in any 24 hours:
///         one more waits in its place for the next day, however it came due.
///         A line sells only while its creator has it switched on and the pool has `COVER_PAYOUTS` of its
///         payouts free; the creator switches a line on only while the pool has that money.
/// @dev The money is this contract's USDC on HyperEVM; nothing goes to HyperCore. What it owes
///      (`owed`) is every payout neither paid nor stopped. The creator withdraws only what stays
///      above `floor()` and above what it owes, so a payout that comes due finds its money, unless
///      payouts came faster than prices: then payouts are paid in the order they were claimed, as
///      money comes in.
contract SimPool is Initializable {
    using SafeERC20 for IERC20;

    enum Kind {
        None,
        Exam,
        Stage
    }

    enum PayoutState {
        None,
        Due,
        Stopped,
        Paid,
        /// Due and sent, but the token refused it (say the trader's address was blocked): the money is
        /// set aside for the trader, who takes it with `claimHeld`, and the queue moves on.
        Held,
        /// Stopped, and not lifted within `STOP_RESERVE` of the stop: the refusal is final.
        Refused
    }

    struct Account {
        Kind kind;
        uint8 line;
        /// Released, funded or claimed: nothing more happens to it here.
        bool done;
    }

    /// A trading group's code: every sale names one. It gets `bps` of the pool's profit in the part the code
    /// brought of every price the pool took (`withdraw`, `finish`): accrued to the code at each withdrawal, and
    /// sent by `claimShares`, which anyone may call. Its partner, the group's owner, gets all of it, or, on a code
    /// for two (`setSplitCode`), all but the author's `authorBps` of it, which goes to the author.
    struct Code {
        address partner;
        uint16 bps;
        /// A sale named it: its partner, author, split and share are fixed from then on, but for a raise.
        bool used;
        /// The author's part of the code's share, in bps of it; zero on a code for one.
        uint16 authorBps;
        /// The second of a code for two, say a blogger who brings the group's traders; zero on a code for one.
        address author;
        /// What the code's sales brought the pool: each price less the platform's part.
        uint256 brought;
        /// The pool's `accrual` when the code was last counted (`SimPoolCodes`).
        uint256 mark;
        /// What the code has accrued for its partner and its author and not yet sent or set aside, as last counted
        /// (`shareDue` counts the rest).
        uint256 partnerDue;
        uint256 authorDue;
        /// What the code has sent its partner and its author, or set aside for them, in all.
        uint256 partnerGot;
        uint256 authorGot;
        /// The fraction of a unit the code accrued and was not yet counted, times `ACCRUAL_SCALE`.
        uint256 frac;
    }

    struct Payout {
        address account;
        address trader;
        uint64 amount;
        uint64 dueAt;
        PayoutState state;
        /// Stopped once already: a payout is stopped at most once.
        bool wasStopped;
        /// When it was stopped; zero if never.
        uint64 stoppedAt;
        /// The pool's approver approved it: `pay` pays it from `dueAt`, not only from `PAYOUT_HARD`.
        bool approved;
    }

    SimPoolFactory public factory;
    IERC20 public usdc;
    address public creator;
    /// Who approves payouts (`approvePayouts`), named by the creator; zero: none, and payouts go out at
    /// `PAYOUT_HARD` from their claim.
    address public approver;
    bytes32 public terms;
    /// The largest payout of the menu, on or off.
    uint64 public largestPayout;
    /// What the creator put in and has not taken back: a withdrawal shares only what is above it.
    uint256 public principal;
    /// Every payout neither paid nor stopped.
    uint256 public owed;
    /// The payouts of the exams passed and not yet funded or released, and of the stages still running, each by its
    /// own size: what they would pay if they all passed. An exam's payout counts from its pass (`examPassed`), not
    /// from its stage (A1, 16 November). The creator withdraws only above `floor()`, which is never less (CFO, 13
    /// November).
    uint256 public staged;
    /// Exams and stages still running or still able to pay.
    uint256 public live;
    bool public closed;
    bool public finished;
    /// Before this time the creator neither closes the pool nor takes back what it put in: the
    /// pool's creation plus the factory's `MIN_TERM`. Only the factory's closer closes it sooner.
    uint64 public lockedUntil;
    /// The factory's closer closed it before `lockedUntil`.
    bool public closedEarly;
    /// When it was closed: `finish` waits `WITHDRAW_DELAY` from then.
    uint64 public closedAt;
    /// The creator's standing request to withdraw: how much, and from when it can be carried out
    /// (`withdraw`), for `WITHDRAW_WINDOW` from then. Zero `requested`: none.
    uint256 public requested;
    uint64 public readyAt;
    /// The first payout not yet settled: payouts are paid in the order they were claimed (`pay`).
    uint256 public head;
    uint256 public stoppedCount;
    uint256 public liftedCount;
    uint256 public paidCount;
    uint256 public paidTotal;
    /// How many payouts the pool settles at most in any `PAYOUT_DAY`, set at its creation.
    uint8 public payoutsPerDay;
    /// When `pay` settled its last `payoutsPerDay` payouts (paid, or set aside when the token refused), the
    /// oldest first, in the first `payoutsPerDay` places; zero for none yet. `pay` settles one more only once the
    /// oldest is `PAYOUT_DAY` old, so no 24 hours see more than `payoutsPerDay` (CFO, 13 November).
    uint64[MAX_PAYOUTS_A_DAY] internal _settledAt;
    /// Payouts, partners' parts and the creator's withdrawals the token refused to send, set aside for whom they were
    /// for (`claimHeld`).
    uint256 public held;
    mapping(address holder => uint256) public heldOf;
    /// Payouts stopped within `STOP_RESERVE` and not yet lifted or made final: the money stays set
    /// aside, as `held` is, so a lifted stop finds it (CFO, 12 November).
    uint256 public reserved;
    /// Every code, by the keccak256 of its text, and what all of them brought together. Codes are not listed and
    /// have no number limit: every one is in a `CodeSet` event.
    mapping(bytes32 code => Code) public codeOf;
    uint256 public brought;
    /// Every code's weight together: what it brought times its share in bps.
    uint256 public weight;
    /// What one unit of a code's weight has accrued from the pool's withdrawals, times `ACCRUAL_SCALE`.
    uint256 public accrual;
    /// What all the codes have accrued together, times `ACCRUAL_SCALE`, to the last fraction: `sharesDue` is it rounded
    /// up, less what the codes have sent or set aside.
    uint256 public accruedScaled;
    /// What the codes have accrued and not yet sent their partners or set aside: never the creator's, kept out of
    /// every withdrawal and payout as `held` is.
    uint256 public sharesDue;

    SimLine[] internal _lines;
    SimRules internal _rules;
    uint32 public fundWindow;
    /// The lines the creator has switched on, bit `i` for line `i` (`setLine`).
    uint32 public onMask;

    mapping(address => Account) public accountOf;
    Payout[] internal _payouts;
    /// The exams that told the pool they passed (`examPassed`): their payouts are in `staged`.
    mapping(address exam => bool) public passedOf;
    /// Why the checker stopped payout `id` (`PayoutStopped.reason`, 1 to 4); zero if it never was.
    mapping(uint256 id => uint8) public stopReasonOf;

    /// `bySignature`: the price came with the trader's signature (EIP-3009), sent by whoever paid the gas.
    /// `code` is the keccak256 of the sale's code, and `partner` its partner.
    event ExamSold(
        address indexed exam,
        address indexed trader,
        uint256 line,
        uint64 size,
        uint64 price,
        uint256 fee,
        bool bySignature,
        bytes32 code,
        address indexed partner
    );
    /// Exam `exam` passed: its payout is kept in `staged` from now on, until its stage starts or it is released.
    event ExamPassed(address indexed exam, uint256 staged);
    event Funded(address indexed stage, address indexed exam, address indexed trader);
    event Released(address indexed account);
    event PayoutDue(uint256 indexed id, address indexed account, address indexed trader, uint64 amount, uint64 dueAt);
    event PayoutQueued(uint256 indexed id, uint256 balance);
    /// Payout `id` may be paid and the pool may hold its money, but the pool has settled `payoutsPerDay`
    /// payouts in the last 24 hours: it waits in its place, and `pay` settles it from `nextAt` on.
    event PayoutWaitsLimit(uint256 indexed id, uint64 nextAt);
    event PayoutPaid(uint256 indexed id, address indexed trader, uint64 amount, uint256 paidCount, uint256 paidTotal);
    /// `reason` 1: opposite positions with `paired`; 2: that, and the wallets linked by `links`; 3: the
    /// result is disputed (a flaw of the exam, the oracle or the filler), shown by `trades`, with no
    /// `paired` and `links` the keccak256 of the published text of the dispute; 4: prohibited play, a series
    /// of exams bought by one person or linked wallets and traded at the edge of their risk, or one account
    /// hedged by another, shown by at least two `trades`, `links` the keccak256 of the published analysis and
    /// `paired` another account of the factory, or none.
    event PayoutStopped(
        uint256 indexed id,
        address indexed trader,
        uint64 amount,
        uint8 reason,
        address paired,
        bytes32[] trades,
        bytes32[] links,
        uint256 stoppedCount,
        uint256 liftedCount,
        uint256 paidCount
    );
    event PayoutHeld(uint256 indexed id, address indexed trader, uint64 amount);
    event PayoutApproved(uint256 indexed id, address indexed approver);
    event ApproverSet(address indexed approver);
    event HeldClaimed(address indexed trader, address indexed to, uint256 amount);
    event StopLifted(uint256 indexed id, uint256 stoppedCount, uint256 liftedCount, uint256 paidCount);
    /// The trader objects to the stop of payout `id`; `text` is the keccak256 of the objection's text.
    event StopObjected(uint256 indexed id, address indexed trader, bytes32 text);
    /// The stop of payout `id` was not lifted within `STOP_RESERVE`: final, and its money free.
    event StopFinal(uint256 indexed id, uint64 amount, uint256 reserved);
    event AddedToPot(uint256 amount, uint256 principal);
    /// `partnerAmount` is what the withdrawal accrued to the codes, which stays in the pool for their partners
    /// (`claimShares`); `sent` is false when the token refused the creator's part, set aside for it (`claimHeld`).
    event Withdrawn(address indexed to, uint256 amount, uint256 fee, uint256 partnerAmount, bool sent);
    /// The creator asks to withdraw `amount`, which can be carried out from `readyAt` until `lapsesAt`.
    event WithdrawRequested(uint256 amount, uint64 readyAt, uint64 lapsesAt);
    /// A request to withdraw `amount` cancelled by `by`: the creator, or the factory's closer.
    event WithdrawCancelled(address indexed by, uint256 amount);
    /// `author` and `authorBps` are zero on a code for one.
    event CodeSet(bytes32 indexed code, address indexed partner, uint16 bps, address indexed author, uint16 authorBps);
    /// Code `code` was counted what it accrued since it was last touched (a sale, a raise, a claim): its partner's
    /// part and its author's.
    event ShareAccrued(bytes32 indexed code, uint256 toPartner, uint256 toAuthor);
    /// What a code had accrued, sent to `to` by `claimShares`, or set aside for `to` (`claimHeld`) if the token
    /// refused.
    event SharePaid(bytes32 indexed code, address indexed to, uint256 amount, bool sent);
    event Closed();
    /// The factory's closer closed the pool before its term; the money goes where it would anyway.
    event ClosedEarly(address indexed closer);
    event Finished(uint256 amount);
    /// The creator switched line `line` on or off: new sales only; what was sold keeps its rules.
    event LineSet(uint256 indexed line, bool on);

    error NotFactory();
    error NotCreator();
    error NotChecker();
    error NotTrader();
    error NoSuchLine();
    error Closing();
    error NotAnExam();
    error NotAStage();
    error Done();
    error NotPassed();
    error TooLate();
    error StillRunning();
    error BadPayout();
    error BadReason();
    error BadPaired();
    error TooMuch();
    error BadShare();
    error NotOver();
    error NoAuthorization();
    error ReserveOver();
    error ReserveRunning();
    error NoSuchCode();
    error CodeUsed();
    error Locked();
    error NoRequest();
    error NotReady();
    error RequestLapsed();
    error NotApprover();
    error LineOff();
    /// The pool has `short` less free than `COVER_PAYOUTS` payouts of the line.
    error NotCovered(uint256 short);

    modifier onlyCreator() {
        if (msg.sender != creator) revert NotCreator();
        _;
    }

    modifier onlyChecker() {
        if (msg.sender != factory.checker()) revert NotChecker();
        _;
    }

    constructor() {
        _disableInitializers();
    }

    /// @notice Called by the factory that clones it, which has checked the plan.
    function initialize(address creator_, SimPlan calldata p) external initializer {
        factory = SimPoolFactory(msg.sender);
        usdc = factory.usdc();
        creator = creator_;
        terms = p.terms;
        fundWindow = p.fundWindow;
        lockedUntil = uint64(block.timestamp) + factory.MIN_TERM();
        payoutsPerDay = p.payoutsPerDay;
        onMask = p.on;
        largestPayout = _payoutOf(SimPoolSetup.copy(p, _lines, _rules));
    }

    /// @notice The factory's note of what the creator locked at the start.
    function noteLocked(uint256 amount) external {
        if (msg.sender != address(factory)) revert NotFactory();
        principal += amount;
        emit AddedToPot(amount, principal);
    }

    // ── views ────────────────────────────────────────────────────

    function menu() external view returns (SimSize[] memory m) {
        _returnRaw(SimPoolSetup.shown(_lines, false));
    }

    /// @notice Each line's tier, in the order of `menu()`: the loss limit, the target and the kind of loss line
    ///         both stages of the line start on.
    function tiers() external view returns (SimTier[] memory k) {
        _returnRaw(SimPoolSetup.shown(_lines, true));
    }

    /// @notice What the pool has free for new sales: its USDC less what it owes, holds, keeps for recent stops and
    ///         keeps for the stages still running (`staged`) (CFO, 16 November).
    function free() public view returns (uint256) {
        uint256 bal = _bal();
        uint256 kept = owed + held + reserved + staged + sharesDue;
        return bal > kept ? bal - kept : 0;
    }

    /// @notice Line `line` as a page shows it: whether the creator has it on; whether it sells now (on, the pool
    ///         open, and `COVER_PAYOUTS` of its payouts free); whether the creator may switch it on now; and how
    ///         much the pool is short of that, zero if nothing.
    function lineState(uint256 line) external view returns (bool on, bool sells, bool canSwitchOn, uint256 short) {
        if (line >= _lines.length) revert NoSuchLine();
        on = _isOn(line);
        short = _short(_lines[line].size, free());
        canSwitchOn = short == 0 && !closed;
        sells = on && canSwitchOn;
    }

    /// @notice What the pool keeps while it is not finished, whatever the creator asks: the greater of `staged` and
    ///         `FLOOR_PAYOUTS` payouts of the largest line with an exam or a stage not yet done, or on and selling
    ///         now (CFO, 16 November). A trader with a live exam on the largest line keeps three of its payouts in.
    function floor() public view returns (uint256 f) {
        f = uint256(_payoutOf(SimPoolSetup.top(_lines, closed ? 0 : onMask, free(), factory))) * factory.FLOOR_PAYOUTS();
        if (staged > f) f = staged;
    }

    /// @notice Whether both stages fill at the book, and how much worse than the oracle at most (`SimPlan`).
    function fromBook() external view returns (bool) {
        return _rules.fromBook;
    }

    function maxWorseBps() external view returns (uint16) {
        return _rules.maxWorseBps;
    }

    function payout(uint256 id) external view returns (Payout memory) {
        return _payouts[id];
    }

    /// @notice What code `code` has accrued and not yet sent its partner and its author: `claimShares` sends it.
    function shareDue(bytes32 code) external view returns (uint256 toPartner, uint256 toAuthor) {
        return SimPoolCodes.due(codeOf, accrual, code);
    }

    /// @notice From when payout `id` goes out whether approved or not: its claim plus `PAYOUT_HARD`.
    function payoutHardAt(uint256 id) public view returns (uint64) {
        return _hardAt(_payouts[id]);
    }

    function payoutCount() external view returns (uint256) {
        return _payouts.length;
    }

    /// @notice How many payouts `pay` may settle now under the daily limit: `payoutsPerDay` less those it
    ///         settled in the last `PAYOUT_DAY`.
    function payRoom() external view returns (uint256 room) {
        uint256 day = factory.PAYOUT_DAY();
        for (uint256 k = 0; k < payoutsPerDay; ++k) {
            if (block.timestamp >= uint256(_settledAt[k]) + day) room += 1;
        }
    }

    /// @notice From when `pay` may settle one more payout under the daily limit: the oldest of its last
    ///         `payoutsPerDay` settlements plus `PAYOUT_DAY`; zero while it has settled fewer.
    function nextPayAt() external view returns (uint64) {
        uint64 oldest = _settledAt[0];
        return oldest == 0 ? 0 : oldest + factory.PAYOUT_DAY();
    }

    /// @notice What the creator may withdraw now: the pool's USDC above what it owes, holds and keeps
    ///         for recent stops and, until it is finished, above `floor()`: a funded trader's payout stays
    ///         in the pool for as long as the stage runs.
    ///         Before `lockedUntil`, only profit: what the creator put in stays until the term, or until
    ///         `finish` if the factory's closer closed the pool sooner.
    function withdrawable() public view returns (uint256) {
        uint256 bal = _bal();
        uint256 keep = owed + held + reserved + sharesDue + (finished ? 0 : floor());
        uint256 w = bal > keep ? bal - keep : 0;
        if (block.timestamp < lockedUntil) {
            uint256 p = _profit();
            if (w > p) w = p;
        }
        return w;
    }

    // ── traders ──────────────────────────────────────────────────

    /// @notice Buys an exam off line `line` of the menu with `code`, the keccak256 of a trading group's
    ///         code that the creator set (`setCode`). No seat and no share limits a sale: the prices of
    ///         exams that fail stay in the pool and pay for the ones that pass. The line must be on and the pool
    ///         must have `COVER_PAYOUTS` of its payouts free (`lineState`): otherwise `LineOff` or `NotCovered`.
    function buy(uint256 line, bytes32 code) external returns (address exam) {
        SimLine memory m = _line(line);
        uint256 fee = _fee(m.price);
        usdc.safeTransferFrom(msg.sender, address(this), m.price - fee);
        if (fee != 0) usdc.safeTransferFrom(msg.sender, factory.feeRecipient(), fee);
        exam = _sold(msg.sender, line, m, fee, false, code);
    }

    /// @notice Buys an exam for `trader` with the trader's signature of a USDC transfer of the line's
    ///         price to this pool (EIP-3009 `receiveWithAuthorization`), so the trader needs no gas:
    ///         anyone may send it, a relayer of the platform's or a partner's. The signed amount is
    ///         the line's price or the token refuses the signature; the token keeps the nonce, so a
    ///         signature buys once; and only this pool, the payee, can use it. The price and the fee
    ///         are those of `buy`. A token without EIP-3009 refuses with `NoAuthorization`. The code is
    ///         in the signature, and so is the line: the signed nonce is keccak256(abi.encode(code, line,
    ///         salt)), so whoever sends it can name neither another code nor another line at the same price.
    function buyWithAuthorization(
        address trader,
        uint256 line,
        bytes32 code,
        bytes32 salt,
        uint256 validAfter,
        uint256 validBefore,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external returns (address exam) {
        SimLine memory m = _line(line);
        _receive(trader, m.price, keccak256(abi.encode(code, line, salt)), validAfter, validBefore, v, r, s);
        uint256 fee = _fee(m.price);
        if (fee != 0) usdc.safeTransfer(factory.feeRecipient(), fee);
        exam = _sold(trader, line, m, fee, true, code);
    }

    /// Takes `price` from `trader` by the trader's EIP-3009 signature over `nonce`.
    function _receive(
        address trader,
        uint64 price,
        bytes32 nonce,
        uint256 validAfter,
        uint256 validBefore,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) internal {
        try IReceiveWithAuthorization(address(usdc)).authorizationState(trader, nonce) returns (bool) {}
        catch {
            revert NoAuthorization();
        }
        IReceiveWithAuthorization(address(usdc)).receiveWithAuthorization(
            trader, address(this), price, validAfter, validBefore, nonce, v, r, s
        );
    }

    /// @notice Starts the stage an exam earned: its trader alone, within `fundWindow` of the pass.
    ///         Nothing is paid and nothing moves: the stage is a paper exam too.
    function fund(address exam) external returns (address stage) {
        Account storage a = accountOf[exam];
        if (a.kind != Kind.Exam) revert NotAnExam();
        if (a.done) revert Done();
        (PaperExam.Status s, address trader,, uint64 passedAt) = PaperExam(exam).result();
        if (s != PaperExam.Status.Passed) revert NotPassed();
        if (msg.sender != trader) revert NotTrader();
        if (block.timestamp > uint256(passedAt) + fundWindow) revert TooLate();
        a.done = true;
        _end(a.line);
        // Its payout is in `staged` since the pass (`examPassed`), and the stage carries it on; an exam that did not
        // tell the pool is counted now, once (review of cp#310).
        if (!passedOf[exam]) _notePassed(exam, a.line);
        stage = _start(trader, a.line, true);
        accountOf[stage] = Account({kind: Kind.Stage, line: a.line, done: false});
        emit Funded(stage, exam, trader);
    }

    /// @notice Called by an exam of this pool when its trader passes it (`PaperExam.graduate`): from then on its
    ///         payout is in `staged`, so `floor()` keeps it from the creator's withdrawal and `free()` leaves it out
    ///         of new sales and of switching lines on, before the trader starts the stage (`fund`). Released unfunded
    ///         past `fundWindow`, it is free again (`release`) (A1, 16 November). A call from anything but an exam
    ///         of this pool, or a second call, changes nothing.
    function examPassed() external {
        if (accountOf[msg.sender].kind != Kind.Exam || passedOf[msg.sender]) return;
        _notePassed(msg.sender, accountOf[msg.sender].line);
    }

    /// An exam's payout into `staged`, marked in `passedOf`: `release` takes it out by the same mark, never by the
    /// exam's status, so the two always go in pairs (review of cp#310).
    function _notePassed(address exam, uint256 line) internal {
        passedOf[exam] = true;
        staged += _payoutOf(_lines[line].size);
        emit ExamPassed(exam, staged);
    }

    // ── anyone ───────────────────────────────────────────────────

    /// @notice Writes the payout a passed stage earned, due `PAYOUT_DELAY` from now. Anyone may.
    ///         One stage, one payout: the stage has one step and its pass ends it.
    function claimPayout(address stage) external returns (uint256 id) {
        Account storage a = accountOf[stage];
        if (a.kind != Kind.Stage) revert NotAStage();
        if (a.done) revert Done();
        (PaperExam.Status s, address trader,,) = PaperExam(stage).result();
        if (s != PaperExam.Status.Passed) revert NotPassed();
        a.done = true;
        _end(a.line);
        uint64 amount = _payoutOf(_lines[a.line].size);
        staged -= amount;
        uint64 dueAt = uint64(block.timestamp) + factory.PAYOUT_DELAY();
        id = _payouts.length;
        _payouts.push(
            Payout({
                account: stage,
                trader: trader,
                amount: amount,
                dueAt: dueAt,
                state: PayoutState.Due,
                wasStopped: false,
                stoppedAt: 0,
                approved: false
            })
        );
        owed += amount;
        emit PayoutDue(id, stage, trader, amount, dueAt);
    }

    /// @notice Pays the payouts that are due, in the order of their claims, looking at up to `max` of them from the
    ///         head, settled ones included,
    ///         while the pool's USDC covers the next. A payout is due `PAYOUT_DELAY` after its claim once
    ///         the approver approved it, and `PAYOUT_HARD` after its claim if not. One not yet due for want
    ///         of approval keeps its place: `pay` moves on to later ones, but pays one only if the money
    ///         also covers every such payout before it. At most `payoutsPerDay` payouts are settled in any
    ///         `PAYOUT_DAY`, whether they came due by approval or by `PAYOUT_HARD`, paid or set aside for a
    ///         trader the token refused: one more waits in its place (`PayoutWaitsLimit`), and every payout
    ///         after it, until the oldest of the last settlements is a day old. Anyone may.
    function pay(uint256 max) external returns (uint256 n) {
        uint256 len = _payouts.length;
        uint256 i = head;
        uint256 seen;
        // What the payouts passed over for want of approval will need, and the first of them: the head.
        uint256 ahead;
        uint256 waiting = len;
        // Read once: the factory's constants, not a call for each payout (review of #286).
        uint256 hardAfter = uint256(factory.PAYOUT_HARD()) - factory.PAYOUT_DELAY();
        // The last settlements, oldest first, and the day they are counted in.
        uint64[MAX_PAYOUTS_A_DAY] memory last = _settledAt;
        uint256 perDay = payoutsPerDay;
        uint256 day = factory.PAYOUT_DAY();
        // Every payout looked at counts toward `max`, settled ones too: payouts paid out of order behind a head that
        // waits for its approval cannot make a call run long (review of #286).
        while (i < len && seen < max) {
            Payout storage p = _payouts[i];
            seen += 1;
            if (p.state == PayoutState.Due) {
                if (block.timestamp < p.dueAt) break;
                if (!p.approved && block.timestamp < uint256(p.dueAt) + hardAfter) {
                    ahead += p.amount;
                    if (waiting == len) waiting = i;
                    i += 1;
                    continue;
                }
                // The daily limit: the pool has settled `payoutsPerDay` payouts since a day ago. This one keeps
                // its place, and so does every payout after it (CFO, 13 November).
                uint256 freeAt = uint256(last[0]) + day;
                if (block.timestamp < freeAt) {
                    emit PayoutWaitsLimit(i, uint64(freeAt));
                    break;
                }
                // What is held is its traders' already, and what a recent stop keeps may be its trader's
                // again: neither is paid out to anyone else (CFO, 12 November). Stops can keep more than the
                // pool holds, when payouts came faster than prices: then nothing is free (review of #269).
                uint256 bal = _bal();
                uint256 kept = held + reserved + sharesDue;
                bal = bal > kept ? bal - kept : 0;
                if (bal < ahead + p.amount) {
                    emit PayoutQueued(i, bal);
                    break;
                }
                owed -= p.amount;
                n += 1;
                // Counted before the token is called, sent or refused alike: the money leaves the queue.
                for (uint256 k = 0; k + 1 < perDay; ++k) {
                    last[k] = last[k + 1];
                }
                last[perDay - 1] = uint64(block.timestamp);
                _settledAt = last;
                if (_send(p.trader, p.amount)) {
                    p.state = PayoutState.Paid;
                    paidCount += 1;
                    paidTotal += p.amount;
                    emit PayoutPaid(i, p.trader, p.amount, paidCount, paidTotal);
                } else {
                    // One refused transfer does not hold up every payout after it (review of cp#261).
                    p.state = PayoutState.Held;
                    held += p.amount;
                    heldOf[p.trader] += p.amount;
                    emit PayoutHeld(i, p.trader, p.amount);
                }
            }
            i += 1;
        }
        head = waiting < i ? waiting : i;
    }

    /// @notice Ends an exam or a stage that can do nothing more here: one that broke a rule or ran
    ///         out of time, or an exam whose pass went unfunded past `fundWindow`. Anyone may.
    function release(address account) external {
        Account storage a = accountOf[account];
        if (a.kind == Kind.None) revert NotAnExam();
        if (a.done) revert Done();
        (PaperExam.Status s,,, uint64 passedAt) = PaperExam(account).result();
        bool over = s == PaperExam.Status.Breached || s == PaperExam.Status.Expired
            || (a.kind == Kind.Exam && s == PaperExam.Status.Passed && block.timestamp > uint256(passedAt) + fundWindow);
        if (!over) revert StillRunning();
        a.done = true;
        _end(a.line);
        // A stage, or an exam passed and not funded in time: its payout was in `staged`, and is free again.
        if (a.kind == Kind.Stage || passedOf[account]) staged -= _payoutOf(_lines[a.line].size);
        emit Released(account);
    }

    /// @notice Sends a trader, a partner or the creator what was set aside for them when a transfer was refused,
    ///         to an address they name. Also after the pool has finished.
    function claimHeld(address to) external {
        uint256 amount = heldOf[msg.sender];
        if (amount == 0) revert TooMuch();
        heldOf[msg.sender] = 0;
        held -= amount;
        usdc.safeTransfer(to, amount);
        emit HeldClaimed(msg.sender, to, amount);
    }

    /// @notice Sends each of `codes` what it has accrued to its partner and to its author, each apart, or sets a
    ///         part aside for whom it is (`claimHeld`) if the token refuses it. Anyone may, for any codes, also after
    ///         the pool has finished: the money goes to each code's partner and author only. The work is the number
    ///         of codes named, never all of them.
    function claimShares(bytes32[] calldata codes) external {
        (uint256 out, uint256 setAside) = SimPoolCodes.claim(codeOf, heldOf, usdc, accrual, codes);
        sharesDue -= out;
        held += setAside;
    }

    // ── the checker ──────────────────────────────────────────────

    /// @notice Stops payout `id` before it is paid, approved or not, once, with a reason the chain keeps:
    ///         `reason` 1, opposite positions with `paired`, shown by `trades`; 2, that and the wallets
    ///         linked, shown by `links`; `paired` is an exam or a stage of any pool of the factory. 3, the
    ///         result is disputed: no `paired`, `trades` (one at least) show the flaw, and `links` is one
    ///         hash, of the published text of the dispute. 4, prohibited play: a series of exams bought by
    ///         one person or linked wallets and traded at the edge of their risk, or one account hedged by
    ///         another; `trades` (two at least) show it, `links` is one hash, of the published analysis, and
    ///         `paired` is the other account, of any pool of the factory, or none. The reason stays on record
    ///         (`stopReasonOf`). The money stays in the pool, set aside for
    ///         `STOP_RESERVE`: the trader can object (`objectStop`) and the checker can lift the stop;
    ///         after it the stop is final.
    function stopPayout(uint256 id, uint8 reason, address paired, bytes32[] calldata trades, bytes32[] calldata links)
        external
        onlyChecker
    {
        uint64 amount = SimPoolStops.stop(
            _payouts, stopReasonOf, factory, id, reason, paired, trades, links, [stoppedCount + 1, liftedCount, paidCount]
        );
        owed -= amount;
        reserved += amount;
        stoppedCount += 1;
    }

    /// @notice Lifts the checker's own stop of payout `id`, a stop that was wrong, within `STOP_RESERVE`
    ///         of it: the payout is due again, out of the money the stop kept, and paid as any other
    ///         once its time has come. After that the stop is final.
    function liftStop(uint256 id) external onlyChecker {
        uint64 amount = SimPoolStops.lift(_payouts, factory, id, finished, [stoppedCount, liftedCount + 1, paidCount]);
        reserved -= amount;
        owed += amount;
        liftedCount += 1;
        if (id < head) head = id;
    }

    // ── the approver ─────────────────────────────────────────────

    /// @notice Approves payouts `ids`, each waiting and not yet approved: each is paid from its `dueAt`
    ///         instead of from `PAYOUT_HARD`. Approving sets neither the amount nor whom it goes to, and
    ///         a payout approved can still be stopped until it is paid.
    function approvePayouts(uint256[] calldata ids) external {
        if (msg.sender != approver || msg.sender == address(0)) revert NotApprover();
        for (uint256 k = 0; k < ids.length; ++k) {
            uint256 id = ids[k];
            if (id >= _payouts.length) revert BadPayout();
            Payout storage p = _payouts[id];
            if (p.state != PayoutState.Due || p.approved) revert BadPayout();
            p.approved = true;
            emit PayoutApproved(id, msg.sender);
        }
    }

    // ── a stopped payout's trader, and anyone ────────────────────

    /// @notice The trader of stopped payout `id` objects to the stop, within `STOP_RESERVE` of it:
    ///         the chain keeps the keccak256 of the objection's text, so the dispute is seen as the
    ///         stop is. Nothing else changes; lifting the stop is the checker's.
    function objectStop(uint256 id, bytes32 text) external {
        SimPoolStops.object(_payouts, factory, id, text);
    }

    /// @notice Makes the stop of payout `id` final once `STOP_RESERVE` has passed without a lift: the
    ///         money it kept is the pool's again. Anyone may.
    function finalizeStop(uint256 id) external {
        uint64 amount = SimPoolStops.finalize(_payouts, factory, id, reserved);
        reserved -= amount;
    }

    // ── the creator ──────────────────────────────────────────────

    /// @notice Puts more of the creator's money in, for more payouts at once than the locked two. It is
    ///         principal: never shared with the partners. A withdrawal takes profit first; the
    ///         principal comes back once what is above it has been taken, or at `finish`.
    function addToPot(uint256 amount) external onlyCreator {
        if (finished) revert Done();
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        principal += amount;
        emit AddedToPot(amount, principal);
    }

    /// @notice Asks to withdraw `amount`, at most what `withdrawable` allows now. Nothing moves: the
    ///         creator carries it out with `withdraw` from `WITHDRAW_DELAY` on, within `WITHDRAW_WINDOW`.
    ///         One request a pool: a new one replaces the standing one and starts the wait again.
    function requestWithdraw(uint256 amount) external onlyCreator {
        if (finished) revert Done();
        if (amount == 0 || amount > withdrawable()) revert TooMuch();
        uint64 ready = uint64(block.timestamp) + factory.WITHDRAW_DELAY();
        requested = amount;
        readyAt = ready;
        emit WithdrawRequested(amount, ready, ready + factory.WITHDRAW_WINDOW());
    }

    /// @notice Carries out the standing request, once ready and before it lapses: the less of what was
    ///         asked and what `withdrawable` allows now: never more than was asked, and payouts that came
    ///         due meanwhile come first. The platform takes its 0.5 %. Profit is
    ///         the pool's money above what it owes, holds and keeps for recent stops, and above what the
    ///         creator put in; a withdrawal takes it first, and each code accrues its partner's share of
    ///         it (`claimShares`). There is no other way for the creator to withdraw before `finish`.
    function withdraw() external onlyCreator {
        if (finished) revert Done();
        uint256 r = requested;
        if (r == 0) revert NoRequest();
        if (block.timestamp < readyAt) revert NotReady();
        if (block.timestamp >= uint256(readyAt) + factory.WITHDRAW_WINDOW()) revert RequestLapsed();
        uint256 w = withdrawable();
        uint256 amount = r < w ? r : w;
        if (amount == 0) revert TooMuch();
        requested = 0;
        readyAt = 0;
        _withdraw(amount);
    }

    /// @notice Cancels the standing request: the creator, or the factory's closer, who only cancels and
    ///         changes neither the amount nor where the money goes.
    function cancelWithdraw() external {
        if (msg.sender != creator && msg.sender != factory.closer()) revert NotCreator();
        uint256 r = requested;
        if (r == 0) revert NoRequest();
        requested = 0;
        readyAt = 0;
        emit WithdrawCancelled(msg.sender, r);
    }

    /// @notice Names who approves payouts; zero for none, and every payout then goes out at `PAYOUT_HARD`.
    /// @notice Switches line `line` on or off, for new sales only: exams and stages sold keep their rules. Off,
    ///         always; on, only while the pool is open and has `COVER_PAYOUTS` of the line's payouts free
    ///         (`NotCovered` says how much it is short). A line never switches itself on (Alex, 16 November).
    function setLine(uint256 line, bool on) external onlyCreator {
        if (line >= _lines.length) revert NoSuchLine();
        if (on) {
            if (closed) revert Closing();
            uint256 short = _short(_lines[line].size, free());
            if (short != 0) revert NotCovered(short);
            onMask |= uint32(1 << line);
        } else {
            onMask &= ~uint32(1 << line);
        }
        emit LineSet(line, on);
    }

    function setApprover(address approver_) external onlyCreator {
        approver = approver_;
        emit ApproverSet(approver_);
    }

    /// @notice Sets a trading group's code: `code` is the keccak256 of its text, `partner` the group's
    ///         owner and `bps` its share, zero or within the platform's bounds. A code can be changed
    ///         until a sale names it. After that only its share can be raised, for the same partner and
    ///         within the platform's bounds (an owner's steps of 40, 50 and 60 %, CFO, 13 November); its
    ///         partner never changes and its share never falls: the partner does not depend on the
    ///         creator once it has brought traders. A raise counts from the next withdrawal on. A zero
    ///         `partner` takes an unused code off sale. A pool has as many codes as its creator sets.
    function setCode(bytes32 code, address partner, uint16 bps) external onlyCreator {
        weight += SimPoolCodes.set(codeOf, factory, accrual, code, partner, bps, address(0), 0);
    }

    /// @notice Sets a code for two: as `setCode`, with an author besides the partner (say a blogger who brings the
    ///         group's traders), who gets `authorBps` of the code's share, and the partner the rest. The code's share
    ///         `bps` is within the platform's bounds as a whole, not each part. Its partner, author and split are
    ///         fixed from the first sale by the code on; after it only its share can be raised, by this call with
    ///         the same partner, author and split.
    function setSplitCode(bytes32 code, address partner, uint16 bps, address author, uint16 authorBps)
        external
        onlyCreator
    {
        weight += SimPoolCodes.set(codeOf, factory, accrual, code, partner, bps, author, authorBps);
    }

    /// @notice Stops selling exams. What runs, runs to its end. The creator may from `lockedUntil` on;
    ///         the factory's closer may at any time, and before the term that is the only way to close
    ///         a pool (`closedEarly`). Who closes it changes nothing about where the money goes.
    function close() external {
        if (closed) revert Closing();
        bool early = block.timestamp < lockedUntil;
        if (msg.sender == factory.closer()) {
            if (early) {
                closedEarly = true;
                emit ClosedEarly(msg.sender);
            }
        } else if (msg.sender != creator) {
            revert NotCreator();
        } else if (early) {
            revert Locked();
        }
        closed = true;
        closedAt = uint64(block.timestamp);
        emit Closed();
    }

    /// @notice Once closed for `WITHDRAW_DELAY`, with nothing running, nothing owed and no stop still able
    ///         to be lifted: sends the creator everything left, the principal first without the partners'
    ///         shares. The creator or the factory's closer may call it; the money goes to the creator
    ///         either way. The wait keeps closing and finishing from being a withdrawal in one block.
    function finish() external {
        if (msg.sender != creator && msg.sender != factory.closer()) revert NotCreator();
        if (!closed || finished) revert Done();
        if (live != 0 || owed != 0 || reserved != 0) revert NotOver();
        if (block.timestamp < uint256(closedAt) + factory.WITHDRAW_DELAY()) revert NotReady();
        finished = true;
        requested = 0;
        readyAt = 0;
        uint256 bal = _bal() - held - sharesDue;
        if (bal != 0) _withdraw(bal);
        emit Finished(bal);
    }

    // ── internals ────────────────────────────────────────────────

    /// The line a sale is for, while it sells: checked before any money comes in, so the price is not counted free.
    function _line(uint256 line) internal view returns (SimLine memory m) {
        if (line >= _lines.length) revert NoSuchLine();
        if (closed) revert Closing();
        if (!_isOn(line)) revert LineOff();
        m = _lines[line];
        uint256 short = _short(m.size, free());
        if (short != 0) revert NotCovered(short);
    }

    /// Returns `data`, ABI-encoded already, as the call's return data: `menu()` and `tiers()`, which the library
    /// encodes, so the pool's code does not hold a decoder for each of them (EIP-170).
    function _returnRaw(bytes memory data) internal pure {
        assembly ("memory-safe") {
            return(add(data, 32), mload(data))
        }
    }

    function _isOn(uint256 line) internal view returns (bool) {
        return onMask & (1 << line) != 0;
    }

    /// How much `freeNow` is short of `COVER_PAYOUTS` payouts of a line of `size`; zero if nothing.
    function _short(uint64 size, uint256 freeNow) internal view returns (uint256) {
        uint256 need = uint256(_payoutOf(size)) * COVER_PAYOUTS;
        return freeNow >= need ? 0 : need - freeNow;
    }

    /// An exam or a stage of line `line` is done.
    function _end(uint256 line) internal {
        live -= 1;
        _lines[line].live -= 1;
    }

    function _fee(uint64 price) internal view returns (uint256) {
        return uint256(price) * factory.FEE_BPS() / Units.BPS;
    }

    function _sold(address trader, uint256 line, SimLine memory m, uint256 fee, bool bySignature, bytes32 code)
        internal
        returns (address exam)
    {
        uint256 got = m.price - fee;
        (address partner, uint256 added) = SimPoolCodes.sold(codeOf, accrual, code, got);
        weight += added;
        brought += got;
        exam = _start(trader, line, false);
        accountOf[exam] = Account({kind: Kind.Exam, line: uint8(line), done: false});
        emit ExamSold(exam, trader, line, m.size, m.price, fee, bySignature, code, partner);
    }

    /// An exam (`funded` false) or a stage of line `line`, on the kept rules with the line's tier (`SimPoolSetup`).
    function _start(address trader, uint256 line, bool funded) internal returns (address a) {
        SimLine storage l = _lines[line];
        a = SimPoolSetup.start(_rules, funded, l, trader, factory);
        live += 1;
        l.live += 1;
    }

    function _withdraw(uint256 amount) internal {
        // Profit is the pool's money above what it owes, holds and keeps for recent stops, and above what
        // the creator put in; the partners' shares are of that alone (CFO, 12 November). At finish nothing is
        // owed or kept.
        uint256 profit = _profit();
        uint256 shared = amount < profit ? amount : profit;
        principal -= amount - shared;
        uint256 fee = amount * factory.FEE_BPS() / Units.BPS;
        if (fee != 0) usdc.safeTransfer(factory.feeRecipient(), fee);
        // Each code's share of the profit less the platform's part, in the part the code brought of all the pool
        // took: one step of `accrual` for all of them. The step is rounded up, so a code's part is its share to the
        // unit. What is kept for the codes is all they accrued together, to the fraction, rounded up: each code is
        // counted its whole units and keeps its fraction for the next time, so once every code has taken its own,
        // what stays is the codes' fractions and the rounding up: at most a unit a code with a share.
        // It stays in the pool for their partners and authors (`claimShares`).
        uint256 net = shared - shared * factory.FEE_BPS() / Units.BPS;
        uint256 toPartners;
        uint256 all = brought * Units.BPS;
        if (net != 0 && all != 0) {
            uint256 step = (net * ACCRUAL_SCALE + all - 1) / all;
            accrual += step;
            uint256 was = (accruedScaled + ACCRUAL_SCALE - 1) / ACCRUAL_SCALE;
            accruedScaled += weight * step;
            toPartners = (accruedScaled + ACCRUAL_SCALE - 1) / ACCRUAL_SCALE - was;
            sharesDue += toPartners;
        }
        // The creator's part, sent or, if the token refuses it, set aside for the creator: a refused creator holds
        // up neither the withdrawal nor `finish` nor any code's share (A5, 16 November).
        uint256 rest = amount - fee - toPartners;
        bool sent = _send(creator, rest);
        if (!sent) {
            held += rest;
            heldOf[creator] += rest;
        }
        emit Withdrawn(creator, amount, fee, toPartners, sent);
    }

    /// The pool's money above what it owes, holds and keeps for recent stops, and above the principal.
    ///      Zero, not a revert, when stops keep more than the pool holds.
    function _profit() internal view returns (uint256) {
        uint256 bal = _bal();
        uint256 kept = held + reserved + owed + sharesDue;
        uint256 free = bal > kept ? bal - kept : 0;
        return free > principal ? free - principal : 0;
    }

    /// A transfer that may be refused: true if the token took it.
    function _send(address to, uint256 amount) internal returns (bool) {
        (bool ok, bytes memory ret) = address(usdc).call(abi.encodeCall(IERC20.transfer, (to, amount)));
        return ok && (ret.length == 0 || abi.decode(ret, (bool)));
    }

    function _hardAt(Payout storage p) internal view returns (uint64) {
        return p.dueAt - factory.PAYOUT_DELAY() + factory.PAYOUT_HARD();
    }

    function _bal() internal view returns (uint256) {
        return usdc.balanceOf(address(this));
    }

    function _payoutOf(uint64 size) internal view returns (uint64) {
        return uint64(uint256(size) * factory.PAYOUT_BPS() / Units.BPS);
    }
}
