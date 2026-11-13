// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {Rules, Units} from "../Types.sol";
import {PaperExam, ExamTerms} from "../exam/PaperExam.sol";
import {SimPoolFactory} from "./SimPoolFactory.sol";

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

/// @notice Everything the creator of a simulated pool sets, at creation.
struct SimPlan {
    SimSize[] menu;
    Rules examRules;
    /// The exam's terms. `capital` is not read: every exam takes the size it was bought at.
    ExamTerms exam;
    Rules fundedRules;
    /// The stage after the exam: one step. `capital` is not read either.
    ExamTerms funded;
    /// How long a trader who passed the exam has to start the stage, in seconds.
    uint32 fundWindow;
    /// Whether both stages fill at the book, never better than the oracle and at most `maxWorseBps`
    /// worse than it (`PaperExam`); zero `maxWorseBps` without the book.
    bool fromBook;
    uint16 maxWorseBps;
    /// The keccak256 of the pool's terms, every clause written out, as a `TermsBook` keeps them.
    bytes32 terms;
}

/// @title SimPool
/// @notice A pool where both stages are paper exams and nobody's capital is traded. A trader buys
///         an exam off the menu; whoever passes it starts the stage on the same size; whoever passes
///         the stage is paid 8 % of the size, once, out of the pool's USDC: the prices traders paid,
///         and what its creator locked at the start. A payout waits 72 hours, in which the factory's
///         checker alone may stop it, with a reason, the other account, and the transactions that
///         show it; with no stop it goes to the trader, sent by anyone.
/// @dev The money is this contract's USDC on HyperEVM; nothing goes to HyperCore. What it owes
///      (`owed`) is every payout neither paid nor stopped. The creator withdraws only what stays
///      above `FLOOR_PAYOUTS` payouts of the largest size and above what it owes, so a payout that
///      comes due finds its money, unless payouts came faster than prices: then payouts are paid in
///      the order they came due, as money comes in.
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

    /// A trading group's code: every sale names one. Its partner, the group's owner, gets `bps` of the
    /// pool's profit in the part the code brought of every price the pool took (`withdraw`, `finish`).
    struct Code {
        address partner;
        uint16 bps;
        /// A sale named it: its partner and share are fixed from then on.
        bool used;
        /// In `codes()`: set once at least.
        bool listed;
        /// What the code's sales brought the pool: each price less the platform's part.
        uint256 brought;
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
    }

    SimPoolFactory public factory;
    IERC20 public usdc;
    address public creator;
    bytes32 public terms;
    /// The largest payout of the menu: what `LOCKED_PAYOUTS` and `FLOOR_PAYOUTS` count in.
    uint64 public largestPayout;
    /// What the creator put in and has not taken back: a withdrawal shares only what is above it.
    uint256 public principal;
    /// Every payout neither paid nor stopped.
    uint256 public owed;
    /// The payouts of the stages still running, each by its own size: what they would pay if they all passed.
    /// While the pool is open the creator withdraws only above the greater of this and `FLOOR_PAYOUTS` of the
    /// largest size (CFO, 13 November).
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
    /// The first payout not yet settled: payouts are paid in the order they came due.
    uint256 public head;
    uint256 public stoppedCount;
    uint256 public liftedCount;
    uint256 public paidCount;
    uint256 public paidTotal;
    /// Payouts, and partners' parts, the token refused to send, set aside for whom they were for (`claimHeld`).
    uint256 public held;
    mapping(address holder => uint256) public heldOf;
    /// Payouts stopped within `STOP_RESERVE` and not yet lifted or made final: the money stays set
    /// aside, as `held` is, so a lifted stop finds it (CFO, 12 November).
    uint256 public reserved;
    /// Every code, by the keccak256 of its text, and what all of them brought together.
    mapping(bytes32 code => Code) public codeOf;
    bytes32[] internal _codes;
    uint256 public brought;

    SimSize[] internal _menu;
    Rules internal _examRules;
    ExamTerms internal _exam;
    Rules internal _fundedRules;
    ExamTerms internal _funded;
    uint32 public fundWindow;
    bool public fromBook;
    uint16 public maxWorseBps;

    mapping(address => Account) public accountOf;
    Payout[] internal _payouts;

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
    event Funded(address indexed stage, address indexed exam, address indexed trader);
    event Released(address indexed account);
    event PayoutDue(uint256 indexed id, address indexed account, address indexed trader, uint64 amount, uint64 dueAt);
    event PayoutQueued(uint256 indexed id, uint256 balance);
    event PayoutPaid(uint256 indexed id, address indexed trader, uint64 amount, uint256 paidCount, uint256 paidTotal);
    /// `reason` 1: opposite positions with `paired`; 2: that, and the wallets linked by `links`.
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
    event HeldClaimed(address indexed trader, address indexed to, uint256 amount);
    event StopLifted(uint256 indexed id, uint256 stoppedCount, uint256 liftedCount, uint256 paidCount);
    /// The trader objects to the stop of payout `id`; `text` is the keccak256 of the objection's text.
    event StopObjected(uint256 indexed id, address indexed trader, bytes32 text);
    /// The stop of payout `id` was not lifted within `STOP_RESERVE`: final, and its money free.
    event StopFinal(uint256 indexed id, uint64 amount, uint256 reserved);
    event AddedToPot(uint256 amount, uint256 principal);
    event Withdrawn(address indexed to, uint256 amount, uint256 fee, uint256 partnerAmount);
    /// The creator asks to withdraw `amount`, which can be carried out from `readyAt` until `lapsesAt`.
    event WithdrawRequested(uint256 amount, uint64 readyAt, uint64 lapsesAt);
    /// A request to withdraw `amount` cancelled by `by`: the creator, or the factory's closer.
    event WithdrawCancelled(address indexed by, uint256 amount);
    event CodeSet(bytes32 indexed code, address indexed partner, uint16 bps);
    /// A partner's part of a withdrawal: sent, or set aside for the partner (`claimHeld`) if the token refused.
    event PartnerPaid(bytes32 indexed code, address indexed partner, uint256 amount, bool sent);
    event Closed();
    /// The factory's closer closed the pool before its term; the money goes where it would anyway.
    event ClosedEarly(address indexed closer);
    event Finished(uint256 amount);

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
    error NotDue();
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
        fromBook = p.fromBook;
        maxWorseBps = p.maxWorseBps;
        uint64 largest;
        for (uint256 i = 0; i < p.menu.length; ++i) {
            _menu.push(p.menu[i]);
            if (p.menu[i].size > largest) largest = p.menu[i].size;
        }
        largestPayout = _payoutOf(largest);
        _copyRules(_examRules, p.examRules);
        _copyRules(_fundedRules, p.fundedRules);
        _exam = p.exam;
        _funded = p.funded;
    }

    /// @notice The factory's note of what the creator locked at the start.
    function noteLocked(uint256 amount) external {
        if (msg.sender != address(factory)) revert NotFactory();
        principal += amount;
        emit AddedToPot(amount, principal);
    }

    // ── views ────────────────────────────────────────────────────

    function menu() external view returns (SimSize[] memory) {
        return _menu;
    }

    function payout(uint256 id) external view returns (Payout memory) {
        return _payouts[id];
    }

    /// @notice Every code of the pool, as `codeOf` keys them, in the order they were set.
    function codes() external view returns (bytes32[] memory) {
        return _codes;
    }

    function payoutCount() external view returns (uint256) {
        return _payouts.length;
    }

    /// @notice What the creator may withdraw now: the pool's USDC above what it owes, holds and keeps
    ///         for recent stops and, while it is open, above the greater of `FLOOR_PAYOUTS` payouts of the
    ///         largest size and the payouts of every stage still running, each by its size (`staged`): a
    ///         funded trader's payout stays in the pool for as long as the stage runs.
    ///         Before `lockedUntil`, only profit: what the creator put in stays until the term, or until
    ///         `finish` if the factory's closer closed the pool sooner.
    function withdrawable() public view returns (uint256) {
        uint256 bal = usdc.balanceOf(address(this));
        uint256 floor = uint256(largestPayout) * factory.FLOOR_PAYOUTS();
        if (staged > floor) floor = staged;
        uint256 keep = owed + held + reserved + (finished ? 0 : floor);
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
    ///         exams that fail stay in the pool and pay for the ones that pass.
    function buy(uint256 line, bytes32 code) external returns (address exam) {
        SimSize memory m = _line(line);
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
    ///         in the signature: the signed nonce is keccak256(abi.encode(code, salt)), so whoever
    ///         sends it cannot name another code.
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
        SimSize memory m = _line(line);
        _receive(trader, m.price, keccak256(abi.encode(code, salt)), validAfter, validBefore, v, r, s);
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
        live -= 1;
        staged += _payoutOf(_menu[a.line].size);
        stage = _start(trader, _fundedRules, _funded, _menu[a.line].size);
        accountOf[stage] = Account({kind: Kind.Stage, line: a.line, done: false});
        emit Funded(stage, exam, trader);
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
        live -= 1;
        uint64 amount = _payoutOf(_menu[a.line].size);
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
                stoppedAt: 0
            })
        );
        owed += amount;
        emit PayoutDue(id, stage, trader, amount, dueAt);
    }

    /// @notice Pays the payouts that are due, in the order they came due, up to `max` of them,
    ///         while the pool's USDC covers the next. Anyone may.
    function pay(uint256 max) external returns (uint256 n) {
        uint256 len = _payouts.length;
        uint256 i = head;
        while (i < len && n < max) {
            Payout storage p = _payouts[i];
            if (p.state == PayoutState.Due) {
                if (block.timestamp < p.dueAt) break;
                // What is held is its traders' already, and what a recent stop keeps may be its trader's
                // again: neither is paid out to anyone else (CFO, 12 November). Stops can keep more than the
                // pool holds, when payouts came faster than prices: then nothing is free (review of #269).
                uint256 bal = usdc.balanceOf(address(this));
                uint256 kept = held + reserved;
                bal = bal > kept ? bal - kept : 0;
                if (bal < p.amount) {
                    emit PayoutQueued(i, bal);
                    break;
                }
                owed -= p.amount;
                n += 1;
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
        head = i;
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
        live -= 1;
        if (a.kind == Kind.Stage) staged -= _payoutOf(_menu[a.line].size);
        emit Released(account);
    }

    /// @notice Sends a trader, or a partner, what was set aside for them when a transfer was refused, to
    ///         an address they name. Also after the pool has finished.
    function claimHeld(address to) external {
        uint256 amount = heldOf[msg.sender];
        if (amount == 0) revert TooMuch();
        heldOf[msg.sender] = 0;
        held -= amount;
        usdc.safeTransfer(to, amount);
        emit HeldClaimed(msg.sender, to, amount);
    }

    // ── the checker ──────────────────────────────────────────────

    /// @notice Stops payout `id` before it is due, once, with a reason the chain keeps: `reason` 1,
    ///         opposite positions with `paired`, shown by `trades`; 2, that and the wallets linked,
    ///         shown by `links`. `paired` is an exam or a stage of any pool of the factory. The
    ///         money stays in the pool, set aside for `STOP_RESERVE`: the trader can object
    ///         (`objectStop`) and the checker can lift the stop; after it the stop is final.
    function stopPayout(uint256 id, uint8 reason, address paired, bytes32[] calldata trades, bytes32[] calldata links)
        external
        onlyChecker
    {
        if (id >= _payouts.length) revert BadPayout();
        Payout storage p = _payouts[id];
        if (p.state != PayoutState.Due || p.wasStopped) revert BadPayout();
        if (block.timestamp >= p.dueAt) revert NotDue();
        if (reason == 1) {
            if (links.length != 0) revert BadReason();
        } else if (reason == 2) {
            if (links.length == 0) revert BadReason();
        } else {
            revert BadReason();
        }
        if (trades.length < 2) revert BadReason();
        if (paired == p.account || factory.poolOf(paired) == address(0) || factory.poolOf(paired) == paired) {
            revert BadPaired();
        }
        p.state = PayoutState.Stopped;
        p.wasStopped = true;
        p.stoppedAt = uint64(block.timestamp);
        owed -= p.amount;
        reserved += p.amount;
        stoppedCount += 1;
        emit PayoutStopped(id, p.trader, p.amount, reason, paired, trades, links, stoppedCount, liftedCount, paidCount);
    }

    /// @notice Lifts the checker's own stop of payout `id`, a stop that was wrong, within `STOP_RESERVE`
    ///         of it: the payout is due again, out of the money the stop kept, and paid as any other
    ///         once its time has come. After that the stop is final.
    function liftStop(uint256 id) external onlyChecker {
        if (finished) revert Done();
        if (id >= _payouts.length) revert BadPayout();
        Payout storage p = _payouts[id];
        if (p.state != PayoutState.Stopped) revert BadPayout();
        if (block.timestamp >= uint256(p.stoppedAt) + factory.STOP_RESERVE()) revert ReserveOver();
        p.state = PayoutState.Due;
        reserved -= p.amount;
        owed += p.amount;
        liftedCount += 1;
        if (id < head) head = id;
        emit StopLifted(id, stoppedCount, liftedCount, paidCount);
    }

    // ── a stopped payout's trader, and anyone ────────────────────

    /// @notice The trader of stopped payout `id` objects to the stop, within `STOP_RESERVE` of it:
    ///         the chain keeps the keccak256 of the objection's text, so the dispute is seen as the
    ///         stop is. Nothing else changes; lifting the stop is the checker's.
    function objectStop(uint256 id, bytes32 text) external {
        if (id >= _payouts.length) revert BadPayout();
        Payout storage p = _payouts[id];
        if (p.state != PayoutState.Stopped) revert BadPayout();
        if (msg.sender != p.trader) revert NotTrader();
        if (block.timestamp >= uint256(p.stoppedAt) + factory.STOP_RESERVE()) revert ReserveOver();
        emit StopObjected(id, msg.sender, text);
    }

    /// @notice Makes the stop of payout `id` final once `STOP_RESERVE` has passed without a lift: the
    ///         money it kept is the pool's again. Anyone may.
    function finalizeStop(uint256 id) external {
        if (id >= _payouts.length) revert BadPayout();
        Payout storage p = _payouts[id];
        if (p.state != PayoutState.Stopped) revert BadPayout();
        if (block.timestamp < uint256(p.stoppedAt) + factory.STOP_RESERVE()) revert ReserveRunning();
        p.state = PayoutState.Refused;
        reserved -= p.amount;
        emit StopFinal(id, p.amount, reserved);
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
    ///         asked and what `withdrawable` allows now, so money that came in after the request stays,
    ///         and payouts that came due meanwhile come first. The platform takes its 0.5 %. Profit is
    ///         the pool's money above what it owes, holds and keeps for recent stops, and above what the
    ///         creator put in; a withdrawal takes it first, and each code's partner gets its share of
    ///         it (`_payPartners`). There is no other way for the creator to withdraw before `finish`.
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

    /// @notice Sets a trading group's code: `code` is the keccak256 of its text, `partner` the group's
    ///         owner and `bps` its share, zero or within the platform's bounds. A code can be changed
    ///         until a sale names it, never after: the partner does not depend on the creator once
    ///         it has brought traders. A zero `partner` takes an unused code off sale.
    function setCode(bytes32 code, address partner, uint16 bps) external onlyCreator {
        Code storage c = codeOf[code];
        if (c.used) revert CodeUsed();
        if (partner == address(0) && bps != 0) revert BadShare();
        if (bps != 0 && (bps < factory.MIN_PARTNER_BPS() || bps > factory.MAX_PARTNER_BPS())) revert BadShare();
        if (!c.listed && partner != address(0)) {
            if (_codes.length >= factory.MAX_CODES()) revert TooMuch();
            c.listed = true;
            _codes.push(code);
        }
        c.partner = partner;
        c.bps = bps;
        emit CodeSet(code, partner, bps);
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
        uint256 bal = usdc.balanceOf(address(this)) - held;
        if (bal != 0) _withdraw(bal);
        emit Finished(bal);
    }

    // ── internals ────────────────────────────────────────────────

    function _line(uint256 line) internal view returns (SimSize memory) {
        if (line >= _menu.length) revert NoSuchLine();
        if (closed) revert Closing();
        return _menu[line];
    }

    function _fee(uint64 price) internal view returns (uint256) {
        return uint256(price) * factory.FEE_BPS() / Units.BPS;
    }

    function _sold(address trader, uint256 line, SimSize memory m, uint256 fee, bool bySignature, bytes32 code)
        internal
        returns (address exam)
    {
        Code storage c = codeOf[code];
        address partner = c.partner;
        if (partner == address(0)) revert NoSuchCode();
        c.used = true;
        uint256 got = m.price - fee;
        c.brought += got;
        brought += got;
        exam = _start(trader, _examRules, _exam, m.size);
        accountOf[exam] = Account({kind: Kind.Exam, line: uint8(line), done: false});
        emit ExamSold(exam, trader, line, m.size, m.price, fee, bySignature, code, partner);
    }

    function _start(address trader, Rules storage r, ExamTerms storage t, uint64 size) internal returns (address a) {
        a = Clones.clone(factory.examImpl());
        ExamTerms memory terms_ = t;
        terms_.capital = size;
        PaperExam(a).initializeWithFiller(trader, r, terms_, factory.filler(), fromBook, maxWorseBps);
        factory.noteAccount(a);
        live += 1;
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
        uint256 toPartners = _payPartners(shared - shared * factory.FEE_BPS() / Units.BPS);
        usdc.safeTransfer(creator, amount - fee - toPartners);
        emit Withdrawn(creator, amount, fee, toPartners);
    }

    /// The pool's money above what it owes, holds and keeps for recent stops, and above the principal.
    ///      Zero, not a revert, when stops keep more than the pool holds.
    function _profit() internal view returns (uint256) {
        uint256 bal = usdc.balanceOf(address(this));
        uint256 kept = held + reserved + owed;
        uint256 free = bal > kept ? bal - kept : 0;
        return free > principal ? free - principal : 0;
    }

    /// Each code's partner gets its `bps` of `net` in the part the code brought of all the pool took.
    /// A part the token refuses is set aside for the partner, as a refused payout is for its trader.
    function _payPartners(uint256 net) internal returns (uint256 total) {
        uint256 all = brought;
        if (net == 0 || all == 0) return 0;
        for (uint256 i = 0; i < _codes.length; ++i) {
            bytes32 k = _codes[i];
            Code storage c = codeOf[k];
            if (c.bps == 0 || c.brought == 0) continue;
            uint256 part = net * c.brought * c.bps / (all * Units.BPS);
            if (part == 0) continue;
            total += part;
            bool sent = _send(c.partner, part);
            if (!sent) {
                held += part;
                heldOf[c.partner] += part;
            }
            emit PartnerPaid(k, c.partner, part, sent);
        }
    }

    /// A transfer that may be refused: true if the token took it.
    function _send(address to, uint256 amount) internal returns (bool) {
        (bool ok, bytes memory ret) = address(usdc).call(abi.encodeCall(IERC20.transfer, (to, amount)));
        return ok && (ret.length == 0 || abi.decode(ret, (bool)));
    }

    function _payoutOf(uint64 size) internal view returns (uint64) {
        return uint64(uint256(size) * factory.PAYOUT_BPS() / Units.BPS);
    }

    function _copyRules(Rules storage to, Rules calldata r) internal {
        to.dailyLossBps = r.dailyLossBps;
        to.maxDrawdownBps = r.maxDrawdownBps;
        to.maxLeverageX100 = r.maxLeverageX100;
        to.assets = r.assets;
        to.assetLeverageX100 = r.assetLeverageX100;
    }
}
