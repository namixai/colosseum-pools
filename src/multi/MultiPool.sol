// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Rules, Terms, Cancel, Breach, Units} from "../Types.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {CoreOps} from "../lib/CoreOps.sol";
import {CodeStore} from "../lib/CodeStore.sol";
import {Pool} from "../Pool.sol";
import {PoolFactory} from "../PoolFactory.sol";
import {KeyRegistry} from "../KeyRegistry.sol";
import {PaperExam, ExamTerms} from "../exam/PaperExam.sol";
import {FreeExams} from "./FreeExams.sol";
import {TermsBook} from "./TermsBook.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";

/// @notice One line of the menu.
struct Size {
    /// What the exam is taken on, and what the funded account then holds (1e6 = 1 USDC).
    uint64 size;
    /// What the exam costs, in HyperEVM USDC units (6 decimals).
    uint64 price;
}

/// @notice What happens to a funded account that has reached its target.
enum AfterTarget {
    /// There is no target: the trader ends the stage when they choose, and is paid their share.
    TraderStops,
    /// The stage ends at the target, the trader is paid their share, and the seat is free again.
    AccountCloses,
    /// The stage ends at the target, the trader is paid their share, and may take the same seat again.
    AccountStays
}

/// @notice Everything the creator of a pool sets. Fixed at creation.
struct Plan {
    Size[] menu;
    Rules examRules;
    /// The exam's terms. `capital` is not read: every exam takes the size it was bought at.
    ExamTerms exam;
    Rules fundedRules;
    /// Profit over the funded account's starting equity that counts as the target (bps); zero
    /// with `TraderStops` and only then.
    uint16 fundedTargetBps;
    /// The trader's share of the profit a funded stage made (bps).
    uint16 traderShareBps;
    AfterTarget afterTarget;
    /// The most of the pool's value that may be spoken for at once (bps). The rest is its buffer.
    uint16 inWorkBps;
    /// How many sizes may be spoken for at once, whatever they add up to.
    uint16 maxSeats;
    /// How long a trader who passed has to take the funded account, in seconds.
    uint32 claimWindow;
    /// How long the pool runs before it stops, in seconds. The owner may extend it.
    uint32 term;
    /// With `AccountStays`: how many times a trader may take the same seat again after a
    /// target; zero for no limit, and zero in the other two modes.
    uint16 maxRetakes;
    /// How long a funded account may go without a trade before anyone may close it, without a
    /// breach, in seconds: one day to ninety.
    uint32 fundedIdle;
    /// Free exams a month: exams off the menu at no price, each taken on an invitation `inviter`
    /// signed for one trader (`FreeExams`). Zero for none. A month is 30 days from the pool's start.
    uint16 freePerMonth;
    /// Who signs those invitations: the pool's creator. Needed only where there are free exams.
    address inviter;
    /// Who may record a trade the chain cannot see (`noteTrade`): the keeper that reads a funded
    /// account's fills on the exchange. Zero for nobody.
    address noter;
    /// The keccak256 of the pool's terms, every clause written out, and the `TermsBook` that holds
    /// the text for anyone to read. The owner accepts them by naming the hash to `begin`.
    bytes32 terms;
    address termsBook;
}

/// @title MultiPool
/// @notice One owner's capital, many traders. A trader buys an exam off the menu; the exam is a
///         PaperExam, traded on nobody's money. From that moment the size is spoken for: nothing
///         else is sold against it. A trader who passes takes a funded account of that size on
///         the pool's capital, by their own call; one who does not gives the size back.
/// @dev A funded account is a `Pool` this contract owns and runs (`Pool.runByOwner`), so the
///      funded stage is the one every pool has. The owner is a single address here: an investor
///      on their own, or a contract that holds many investors' shares.
///
///      Two limits hold a sale back. The sizes spoken for may not pass `inWorkBps` of the pool's
///      value; and the money for every exam still running has to be on the pool's own HyperCore
///      balance, where a passing trader's account will be paid from. Both are counted in this
///      contract's storage, so two sales in one block see each other; a balance read from
///      HyperCore is the start of the block and never alone decides anything here. What the pool
///      has sent and may not show in that balance yet is counted against it for `ARM_WAIT`.
contract MultiPool {
    using SafeERC20 for IERC20;

    enum ExamState {
        None,
        Sold,
        Claimed,
        Released
    }

    enum SeatState {
        None,
        Free,
        Armed,
        Funded,
        /// Settled after its target; its trader may take it again until `waitUntil`.
        Waiting
    }

    struct ExamInfo {
        uint8 line;
        ExamState state;
        address seat;
        /// Taken on an invitation, at no price.
        bool free;
    }

    struct SeatInfo {
        uint8 line;
        SeatState state;
        bool targetHit;
        address trader;
        uint64 armedAt;
        /// A freed seat is not taken again before this: what it sent back may not have left yet.
        uint64 restUntil;
        uint64 waitUntil;
        /// How many times its trader has taken it again after a target.
        uint16 retakes;
    }

    uint256 public constant MAX_MENU = 8;
    uint256 public constant MAX_SEATS = 16;
    uint32 public constant MIN_CLAIM_WINDOW = 1 hours;
    uint32 public constant MAX_CLAIM_WINDOW = 30 days;
    /// How long a transfer between HyperCore accounts is given to show in a balance.
    uint64 public constant ARM_WAIT = 1 minutes;
    /// The platform's part of every exam's price, out of the price, and of every payment out to the
    /// owner (the accepted terms, D6 and D10; Alex, 4 October 2026, item 28). A free exam pays none.
    uint16 public constant PLATFORM_FEE_BPS = 50;
    /// The most leverage a funded account's rules may allow, times 100: 10x.
    uint32 public constant MAX_LEVERAGE_X100 = 1000;
    /// The longest last stretch any deployment may give a pool, and the most of its shortest term
    /// that stretch may be: the owner's money stays locked until the last fourteen days, or the
    /// last twelfth of a shorter term, whoever deploys. Every term, an extension too, is at least
    /// the shortest one. A testnet pool whose shortest term is three hours gets fifteen minutes.
    uint64 public constant MAX_NOTICE = 14 days;
    uint256 public constant NOTICE_SHARE = 12;
    /// The bounds the pool is offered with. `productBounds()` says whether a pool has them.
    uint32 public constant PRODUCT_MIN_TERM = 180 days;
    uint32 public constant PRODUCT_MAX_TERM = 730 days;
    /// A term nobody stops runs on for this long, again and again, on the same plan.
    uint32 public constant PRODUCT_RENEWAL = 90 days;
    /// How long a funded account may go without a trade, at the least and at the most.
    uint32 public constant MIN_FUNDED_IDLE = 1 days;
    uint32 public constant MAX_FUNDED_IDLE = 90 days;

    address public immutable owner;
    PoolFactory public immutable factory;
    /// The factory's registry of agent keys, which every funded account takes one from.
    KeyRegistry internal immutable keyRegistry;
    IERC20 public immutable usdc;
    address public immutable examImpl;
    /// The shortest and the longest term a plan or an extension may name, and the shortest last
    /// stretch in which nothing is sold. Set by whoever deploys, not by the plan: 6 months, 24
    /// months and 14 days where the pool is offered, less where a whole term has to be watched.
    uint32 public immutable minTerm;
    /// How long the pool runs on when its term ends and the owner has not said stop.
    uint32 public immutable renewal;
    uint32 public immutable maxTerm;
    uint64 public immutable notice;
    /// How long before its end the pool stops selling: the whole time an exam may take and the
    /// time its trader then has to take the account, so that neither is cut short, and never
    /// less than `notice`.
    uint64 public immutable saleLead;

    Plan internal _plan;

    /// Sizes spoken for, added up (1e6 = 1 USDC): exams running or passed and not yet taken, and
    /// funded accounts until their seat is free again.
    uint256 public inWork;
    /// How many sizes that is.
    uint256 public held;
    /// For each line: the sizes spoken for (exams sold and not given back, seats in use) and the
    /// seats made. A sale keeps the second at least the first (deep audit, F9).
    mapping(uint8 line => uint16) internal heldOn;
    mapping(uint8 line => uint16) internal seatsOn;
    /// The part of `inWork` whose money is still on the pool's own balance: exams not yet taken.
    uint256 public promised;
    /// How many exams that is.
    uint256 public waiting;

    mapping(address exam => ExamInfo) public examInfo;
    mapping(address seat => SeatInfo) public seatInfo;
    /// What the pool kept of the price of the exam that claimed a seat (1e8): handed back to its
    /// trader if the seat is freed before its account opened (deep audit, N2).
    mapping(address seat => uint256) internal paidFor;
    /// Seats taken by a free pass: they count among the free exams until they are freed, since the
    /// seat is taken from the start of the exam (the terms, D8; the cloud audit, N14).
    mapping(address seat => bool) internal freeSeat;
    address[] internal _seats;

    /// When the pool stops: after it nothing is sold or opened, and every account is closed.
    /// The end of the term as last written; `endsAt()` adds the renewals since. Zero until `begin`.
    uint64 internal _end;
    /// The owner has said the pool stops at the end of its term: nothing more is sold, and there is
    /// no renewal.
    bool public stopping;
    /// When the last exam sold, all its steps and the time to take its account, is over.
    uint64 public lastExamEnds;
    /// What the owner has said will leave at the end of the term (1e8), kept out of what the share
    /// in work is measured on.
    uint256 public reserve;
    /// The end of the term the reserve was last set in. Once that end has passed and the term has
    /// renewed by itself, the owner may take the reserve out once, at any time (audit M-2).
    uint64 public reserveFor;
    /// Until when, after a stop that ended a renewed term early for those who were to leave at its
    /// first end, only their part of the pool may leave (deep audit, AUD-1).
    uint64 public lockedUntil;
    /// The reserve as a part of the pool (1e18 = all of it), rounded up: what may leave after its end
    /// is that part of the pool as it is then (deep audit, F4).
    uint256 public reserveShare;
    /// Free exams sold and not yet released, and the seats free passes took until they are freed: no
    /// more than a quarter of the seats at once.
    uint256 public freeRunning;
    /// The plan as the pool holds it (the exam's capital set to the first line's size), ABI-encoded,
    /// kept as the code of a contract made with the pool:
    /// `abi.decode(CodeStore.read(planData), (Plan))`. Out of this contract's own code, which is
    /// close to the size a contract may have.
    address public immutable planData;
    /// The pool's free exams, where its plan has them (`FreeExams`, created with the pool).
    address public immutable freeExams;
    /// The price under which an exam is sold at a loss to the pool, as a part of its size (bps): the
    /// CFO's floor, worked out from the plan (a file outside this repository). An exam
    /// priced under it counts as a free one under the free exams' limits (the cloud audit's N11 and
    /// F33; the CTO, 18 October 2026).
    uint256 public immutable priceFloorBps;
    /// The menu's lines priced under the floor, one bit a line.
    uint8 internal immutable cheapLines;

    /// What this pool sent from its HyperCore balance within the last `ARM_WAIT` (1e8 = 1 USDC).
    uint64 public inFlight;
    uint64 public lastSendAt;
    /// What this pool sent its owner within the last `ARM_WAIT` (1e8): money that has left its
    /// books and may still show in its balance. `value()` does not count it.
    uint64 public outFlight;
    /// The platform's 0.5 % of payments out that its HyperCore account could not take yet: the
    /// platform's, kept here until `payFee` sends it, and no part of the pool's value (D10; the CTO,
    /// 18 October 2026).
    uint256 public feeOwed;
    uint64 public lastOutAt;

    /// A funded account's positions and their entries as last seen, and since when they have
    /// looked that way. A change between two looks is a trade.
    mapping(address seat => bytes32) internal activityPrint;
    mapping(address seat => uint64) public activeSince;

    event ExamSold(address indexed exam, address indexed trader, uint256 line, uint64 size, uint64 price);
    event ExamFeePaid(address indexed trader, address indexed recipient, uint256 fee);
    event ExamReleased(address indexed exam);
    event Claimed(address indexed exam, address indexed trader, address indexed seat);
    event SeatCreated(address indexed seat, uint256 line);
    event SeatArmed(address indexed seat, uint64 amount);
    event SeatOpened(address indexed seat, address indexed trader);
    event TargetHit(address indexed seat, address indexed trader, int256 equity);
    event SeatWaiting(address indexed seat, address indexed trader, uint64 until);
    event SeatFreed(address indexed seat, uint64 returned);
    event PriceReturned(address indexed seat, address indexed trader, uint64 amount);
    event Swept(address indexed seat, uint64 amount);
    event SeatActive(address indexed seat);
    event SeatIdle(address indexed seat, address indexed trader);
    event Withdrawn(address indexed to, uint64 amount);
    event WithdrawnEvm(address indexed to, uint256 amount);
    event Extended(uint64 endsAt);
    event Begun(uint64 endsAt, bytes32 terms);
    event Stopping(bool stop, uint64 endsAt);
    event ReserveSet(uint256 reserve);
    event SeatEnded(address indexed seat, address indexed trader);

    error BadPlan();
    error NotOwner();
    error NoSuchLine();
    error Full();
    error NoRoom();
    error NotSold();
    error StillRunning();
    error NotPassed();
    error NotTrader();
    error TooLate();
    error StillTrading(uint256 until);
    error NotEnoughFree(uint64 spot, uint64 inFlight, uint64 cost);
    error BadSeat();
    error TooSoon();
    error AlreadyArmed();
    error NoTarget();
    error NotFlat();
    error RuleBroken(Breach reason);
    error TargetNotMet(int256 equity, int256 target);
    error BufferNeeded();
    error Ending();
    error Ended();
    error NotEnded();
    error Locked();
    error NotInvited();
    error NotNoter();
    error NotStarted();
    error Started();
    error WrongTerms();
    error TooManyFree();
    error Unpublished();
    error NoKey();
    error NothingToSend();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(
        address owner_,
        PoolFactory factory_,
        address examImpl_,
        Plan memory plan_,
        uint32 minTerm_,
        uint32 maxTerm_,
        uint32 notice_,
        uint32 renewal_
    ) {
        if (owner_ == address(0) || examImpl_ == address(0)) revert BadPlan();
        if (notice_ == 0 || notice_ > MAX_NOTICE) revert BadPlan();
        // This keeps the shortest term above zero too.
        if (uint256(notice_) * NOTICE_SHARE > minTerm_) revert BadPlan();
        // A renewal leaves the money's window no larger a part of it than the product's does:
        // fourteen days of ninety.
        if (uint256(notice_) * PRODUCT_RENEWAL > uint256(renewal_) * MAX_NOTICE) revert BadPlan();
        minTerm = minTerm_;
        maxTerm = maxTerm_;
        notice = notice_;
        renewal = renewal_;
        owner = owner_;
        factory = factory_;
        keyRegistry = factory_.registry();
        usdc = factory_.usdc();
        examImpl = examImpl_;

        uint256 lines = plan_.menu.length;
        if (lines == 0 || lines > MAX_MENU) revert BadPlan();
        for (uint256 i = 0; i < lines; ++i) {
            Size memory m = plan_.menu[i];
            if (m.size == 0 || m.price == 0) revert BadPlan();
            if (i != 0 && m.size <= plan_.menu[i - 1].size) revert BadPlan();
            _plan.menu.push(m);
        }
        if (plan_.inWorkBps == 0 || plan_.inWorkBps > Units.BPS) revert BadPlan();
        if (plan_.maxSeats == 0 || plan_.maxSeats > MAX_SEATS) revert BadPlan();
        if (plan_.claimWindow < MIN_CLAIM_WINDOW || plan_.claimWindow > MAX_CLAIM_WINDOW) revert BadPlan();
        if (plan_.term < minTerm_ || plan_.term > maxTerm_) revert BadPlan();
        if (plan_.fundedIdle < MIN_FUNDED_IDLE || plan_.fundedIdle > MAX_FUNDED_IDLE) revert BadPlan();
        if (plan_.freePerMonth != 0 && plan_.inviter == address(0)) revert BadPlan();
        if (plan_.terms == bytes32(0)) revert BadPlan();
        _checkTerms(plan_);
        if (plan_.maxRetakes != 0 && plan_.afterTarget != AfterTarget.AccountStays) revert BadPlan();
        if (plan_.fundedRules.maxLeverageX100 > MAX_LEVERAGE_X100) revert BadPlan();
        // A funded account trades the pool's money on the exchange's book: only on assets the factory
        // lists as deep enough, where one account cannot cross a wide spread against the pool from an
        // account of its own (deep audit, N6).
        for (uint256 i = 0; i < plan_.fundedRules.assets.length; ++i) {
            if (!factory_.isFundedAsset(plan_.fundedRules.assets[i])) revert BadPlan();
        }
        // An exam with no deadline could be cut short by the pool's end; one whose steps take
        // the whole term could never be sold.
        if (plan_.exam.stepDuration == 0) revert BadPlan();
        uint256 span = uint256(plan_.exam.stepDuration) * plan_.exam.targetsBps.length + plan_.claimWindow;
        saleLead = uint64(span > notice_ ? span : notice_);
        if (saleLead >= plan_.term) revert BadPlan();
        if (plan_.afterTarget == AfterTarget.TraderStops) {
            if (plan_.fundedTargetBps != 0) revert BadPlan();
        } else if (plan_.fundedTargetBps == 0 || plan_.fundedTargetBps > Units.BPS) {
            revert BadPlan();
        }
        for (uint256 i = 0; i < plan_.examRules.assets.length; ++i) {
            if (!factory_.isPlatformAsset(plan_.examRules.assets[i])) revert BadPlan();
        }
        // The exam's own answer on its rules and terms, asked once and before anything is sold.
        plan_.exam.capital = plan_.menu[0].size;
        PaperExam(examImpl_).check(plan_.examRules, plan_.exam);

        _plan.examRules = plan_.examRules;
        _plan.exam = plan_.exam;
        _plan.fundedRules = plan_.fundedRules;
        _plan.fundedTargetBps = plan_.fundedTargetBps;
        _plan.traderShareBps = plan_.traderShareBps;
        _plan.afterTarget = plan_.afterTarget;
        _plan.inWorkBps = plan_.inWorkBps;
        _plan.maxSeats = plan_.maxSeats;
        _plan.claimWindow = plan_.claimWindow;
        _plan.term = plan_.term;
        _plan.maxRetakes = plan_.maxRetakes;
        _plan.fundedIdle = plan_.fundedIdle;
        _plan.noter = plan_.noter;
        _plan.terms = plan_.terms;
        // The free exams' number and inviter live in FreeExams, and the book's address in planData:
        // nothing here reads them after this constructor.
        address desk;
        if (plan_.freePerMonth != 0) desk = address(new FreeExams(plan_.inviter, plan_.freePerMonth));
        freeExams = desk;
        planData = CodeStore.write(abi.encode(plan_));
        uint256 floor = _priceFloorBps(plan_);
        priceFloorBps = floor;
        uint8 cheap;
        for (uint256 i = 0; i < lines; ++i) {
            if (uint256(plan_.menu[i].price) * Units.BPS < floor * plan_.menu[i].size) cheap |= uint8(1 << i);
        }
        cheapLines = cheap;
        // Free exams and lines under the floor share a quarter of the seats; under four seats that is
        // none, and such a line would be on the menu and never sell (audit window 2, A3-2).
        if ((cheap != 0 || plan_.freePerMonth != 0) && plan_.maxSeats < 4) revert BadPlan();

        // One seat for every line, so the factory has said yes to the funded rules and to every
        // size before the first exam is sold on them.
        for (uint256 i = 0; i < lines; ++i) {
            _newSeat(uint8(i));
        }
    }

    // ── views ────────────────────────────────────────────────────

    function seats() external view returns (address[] memory) {
        return _seats;
    }

    /// @notice The pool's value (1e8 = 1 USDC): its spot on HyperCore, its USDC on HyperEVM, and
    ///         every seat at its spot and perp value, less what its trader is owed if the stage
    ///         ended now without a breach. A free seat counts too: what `finish` or `sweep` sends
    ///         back from it is the pool's on its way, and is counted once, on the seat until it
    ///         lands here (deep audit, F1).
    function value() public view returns (uint256) {
        int256 v = int256(uint256(CoreOps.spotUsdc(address(this))));
        v += int256(usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP);
        v -= int256(uint256(_recentOut())) + int256(feeOwed);
        uint256 n = _seats.length;
        for (uint256 i = 0; i < n; ++i) {
            v += _seatValue(Pool(_seats[i]));
        }
        return v > 0 ? uint256(v) : 0;
    }

    // How many more exams of a line the pool would sell, and whether it has the product's bounds,
    // are read through `MultiPoolReader`: out of this contract, which is close to the size a
    // contract may have (the CTO, 18 October 2026).

    /// @notice Whether the pool is in the last stretch of its term, where nothing is sold, or past it.
    function closing() public view returns (bool) {
        return _end == 0 || stopping;
    }

    /// @notice When the term ends: the end as written, and once that has passed with nobody saying
    ///         stop, the end of the renewal running now. Zero before `begin`.
    function endsAt() public view returns (uint64) {
        uint64 e = _end;
        if (e == 0 || stopping || block.timestamp <= e) return e;
        return uint64(e + ((block.timestamp - e + renewal - 1) / renewal) * renewal);
    }

    // ── a trader ─────────────────────────────────────────────────

    /// @notice Buys an exam of the menu's `line`. The size is spoken for from this call on.
    function buy(uint256 line) external returns (address exam) {
        return _sell(line, msg.sender, false);
    }

    /// @notice A free exam for `trader`, at no price: the call of the pool's `FreeExams` alone, once
    ///         it has checked the invitation and the month's count. Sold under the same limits as
    ///         any exam: the share in work, the seats, the last stretch.
    function sellFree(address trader, uint8 line) external returns (address exam) {
        if (msg.sender != freeExams) revert NotInvited();
        return _sell(line, trader, true);
    }

    function _sell(uint256 line, address trader, bool free) internal returns (address exam) {
        if (line >= _plan.menu.length) revert NoSuchLine();
        Size memory m = _plan.menu[line];
        if (closing()) revert Ending();
        if (held >= _plan.maxSeats) revert Full();
        uint256 inWorkAfter = inWork + m.size;
        if (inWorkAfter * Units.SPOT_PER_PERP * Units.BPS > _working() * _plan.inWorkBps) revert NoRoom();
        uint64 examEnds = uint64(block.timestamp) + saleLead;
        if (examEnds > lastExamEnds) lastExamEnds = examEnds;
        inWork = inWorkAfter;
        // A key for every seat spoken for, the funded ones too: the registry's keys are retired one
        // by one and published by hand, so a pass is not sold that no key could open (deep audit, N3).
        if (_spareKeys() == 0) revert NoKey();
        held += 1;
        // The seat a pass on this line will take is made now, while the factory still says yes to
        // the funded rules: a pass is never left without one (deep audit, F9).
        if (++heldOn[uint8(line)] > seatsOn[uint8(line)]) _newSeat(uint8(line));
        promised += m.size;
        waiting += 1;
        // The money a pass will ask for has to be where it will be paid from, now, and the leavers'
        // part with it: a sale just before their round would otherwise leave their money in a seat,
        // and the round would stop the pool (deep audit, F3 and N4).
        if (CoreOps.spotUsdc(address(this)) < _owedOnCore() + _recent() + _leaving(value())) revert NoRoom();

        if (_countsFree(uint8(line), free)) {
            // Free exams running at once, and exams priced under the floor, which the pool sells at a
            // loss, are a quarter of the seats at most (CTO, 15 and 18 October 2026).
            if ((freeRunning + 1) * 4 > _plan.maxSeats) revert TooManyFree();
            ++freeRunning;
        }
        if (!free) {
            // The buyer pays the price and no more: the platform's 0.5 % is out of it, and the pool
            // keeps the rest (Alex, item 28; DECISIONS item 101, 16 October 2026).
            (uint256 fee, address recipient) = _platformFee(m.price);
            usdc.safeTransferFrom(msg.sender, address(this), m.price - fee);
            if (fee != 0) {
                usdc.safeTransferFrom(msg.sender, recipient, fee);
                emit ExamFeePaid(msg.sender, recipient, fee);
            }
        }

        exam = Clones.clone(examImpl);
        ExamTerms memory t = _plan.exam;
        t.capital = m.size;
        PaperExam(exam).initialize(trader, _plan.examRules, t);
        examInfo[exam] = ExamInfo({line: uint8(line), state: ExamState.Sold, seat: address(0), free: free});
        emit ExamSold(exam, trader, line, m.size, free ? 0 : m.price);
    }

    /// @notice Takes the funded account an exam earned. Only its trader, only once it has passed,
    ///         and within the pool's window after the pass. The money leaves for the seat in this
    ///         call; `open` starts the account once it is there.
    function claim(address exam) external returns (address seat) {
        ExamInfo storage x = examInfo[exam];
        if (x.state != ExamState.Sold) revert NotSold();
        (PaperExam.Status s, address trader,, uint64 passedAt) = PaperExam(exam).result();
        if (s != PaperExam.Status.Passed) revert NotPassed();
        if (msg.sender != trader) revert NotTrader();
        // The window always closes before the pool stops: no exam is sold later than its own
        // time and this window before the end.
        if (block.timestamp > uint256(passedAt) + _plan.claimWindow) revert TooLate();

        seat = _takeSeat(x.line);
        x.state = ExamState.Claimed;
        freeSeat[seat] = _countsFree(x.line, x.free);
        x.seat = seat;
        promised -= _plan.menu[x.line].size;
        waiting -= 1;
        SeatInfo storage si = seatInfo[seat];
        si.state = SeatState.Armed;
        si.trader = trader;
        si.armedAt = uint64(block.timestamp);
        uint256 price = x.free ? 0 : _plan.menu[x.line].price;
        (uint256 fee,) = _platformFee(price);
        paidFor[seat] = (price - fee) * Units.SPOT_PER_PERP;
        emit Claimed(exam, trader, seat);
        _arm(seat, si);
    }

    /// @notice Takes a seat again after its target, for the trader it is waiting for.
    function reopen(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Waiting) revert BadSeat();
        if (msg.sender != si.trader) revert NotTrader();
        if (block.timestamp > si.waitUntil) revert TooLate();
        if (_over()) revert Ended();
        si.state = SeatState.Funded;
        si.retakes += 1;
        Pool(seat).openFundedFor(si.trader);
        _startClock(seat);
        emit SeatOpened(seat, si.trader);
    }

    // ── anyone ─────────────────────────────────────────────────�

    /// @notice Gives back the size of an exam that ended without a pass, or whose pass nobody
    ///         took in time.
    function release(address exam) external {
        ExamInfo storage x = examInfo[exam];
        if (x.state != ExamState.Sold) revert NotSold();
        (PaperExam.Status s,,, uint64 passedAt) = PaperExam(exam).result();
        bool over = s == PaperExam.Status.Breached || s == PaperExam.Status.Expired
            || (s == PaperExam.Status.Passed && block.timestamp > uint256(passedAt) + _plan.claimWindow) || _over();
        if (!over) revert StillRunning();
        x.state = ExamState.Released;
        if (_countsFree(x.line, x.free)) --freeRunning;
        promised -= _plan.menu[x.line].size;
        waiting -= 1;
        _unhold(x.line);
        emit ExamReleased(exam);
    }

    /// @notice Starts the funded account on a seat whose money has arrived.
    function open(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Armed) revert BadSeat();
        if (_over()) revert Ended();
        // What a freed seat sent back may not have left it yet; a funded stage opened on that
        // balance would start short. The wait is the seat's, and nobody can stretch it once the
        // seat is claimed: `sweep` acts on free seats only.
        if (block.timestamp <= si.restUntil) revert TooSoon();
        si.state = SeatState.Funded;
        Pool p = Pool(seat);
        if (!p.accountReady()) p.prepareAccount();
        p.openFundedFor(si.trader);
        _startClock(seat);
        emit SeatOpened(seat, si.trader);
    }

    /// @notice Sends a seat its money again if, a minute after the first time, it is not there.
    function rearm(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Armed) revert BadSeat();
        if (block.timestamp <= si.armedAt + ARM_WAIT) revert TooSoon();
        if (!_arm(seat, si)) revert AlreadyArmed();
    }

    /// @notice Ends a funded stage that has reached the pool's target with nothing open and no
    ///         rule broken. The trader is paid their share when the stage settles.
    function hitTarget(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt) external {
        if (_plan.afterTarget == AfterTarget.TraderStops) revert NoTarget();
        SeatInfo storage si = _funded(seat);
        Pool p = Pool(seat);
        if (p.openNotional() != 0) revert NotFlat();
        Breach reason = p.violation(new uint32[](0));
        if (reason != Breach.None) revert RuleBroken(reason);
        int256 start = p.fundedStart();
        int256 eq = p.accountEquity();
        int256 target = start + (start * int256(uint256(_plan.fundedTargetBps))) / int256(uint256(Units.BPS));
        if (eq < target) revert TargetNotMet(eq, target);
        si.targetHit = true;
        emit TargetHit(seat, si.trader, eq);
        p.stopFunded(cancels, extraAssets, salt);
    }

    /// @notice Once a seat's stage has settled: frees the seat and takes its money back, or,
    ///         where the plan lets the account stay after its target, holds it for its trader.
    function finish(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        bool over = _over();
        // A seat whose money was sent and whose account never started has nothing to settle; it
        // is freed once the pool has stopped, or once it has had the claim window to open in and
        // did not (say no key was free): its size comes back (deep audit, F18).
        bool unopened = over || block.timestamp > uint256(si.armedAt) + _plan.claimWindow;
        if (si.state == SeatState.Armed ? !unopened : si.state != SeatState.Funded && si.state != SeatState.Waiting) {
            revert BadSeat();
        }
        if (Pool(seat).stage() != Pool.Stage.Idle) revert StillRunning();
        bool again =
            _plan.afterTarget == AfterTarget.AccountStays && (_plan.maxRetakes == 0 || si.retakes < _plan.maxRetakes);
        if (!over && si.state == SeatState.Funded && si.targetHit && again) {
            si.targetHit = false;
            si.state = SeatState.Waiting;
            si.waitUntil = uint64(block.timestamp) + _plan.claimWindow;
            emit SeatWaiting(seat, si.trader, si.waitUntil);
            return;
        }
        if (!over && si.state == SeatState.Waiting && block.timestamp <= si.waitUntil) revert StillRunning();

        uint64 back = CoreOps.spotUsdc(seat);
        // A pass whose account never opened (no key, say) is not sold after all: the price the pool
        // kept goes back to its trader, from the seat's returning balance (deep audit, N2).
        uint64 paid = si.state == SeatState.Armed ? uint64(paidFor[seat]) : 0;
        if (freeSeat[seat]) {
            freeSeat[seat] = false;
            --freeRunning;
        }
        if (paid > back) paid = back;
        address trader = si.trader;
        si.state = SeatState.Free;
        si.targetHit = false;
        si.trader = address(0);
        si.retakes = 0;
        si.restUntil = uint64(block.timestamp) + ARM_WAIT;
        _unhold(si.line);
        uint64 sent = CoreOps.sendableTo(trader, paid);
        if (sent != 0) {
            outFlight = _recentOut() + paid;
            lastOutAt = uint64(block.timestamp);
            Pool(seat).sendOnCore(trader, sent);
            emit PriceReturned(seat, trader, sent);
        } else {
            paid = 0;
        }
        if (back > paid) Pool(seat).withdrawOnCore(back - paid);
        emit SeatFreed(seat, back - paid);
    }

    /// @notice Once the pool has stopped: ends a funded stage, without a breach. The trader is
    ///         paid their share of what it made when the stage settles. Anyone may call it.
    function endSeat(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt) external {
        if (!_over()) revert NotEnded();
        emit SeatEnded(seat, _funded(seat).trader);
        Pool(seat).stopFunded(cancels, extraAssets, salt);
    }

    /// @notice Records that a funded account has traded since it was last looked at, if it has.
    ///         Anyone may call it; a keeper does, so that a trader who traded is not taken for one
    ///         who did not.
    function noteActivity(address seat) external {
        _funded(seat);
        _look(seat);
    }

    /// @notice Records a trade the chain cannot see. An account that opens and closes between two
    ///         looks is flat at both, and to the chain a round trip from flat to flat looks like a
    ///         gift landing on the account. Only the plan's `noter`, the keeper that reads the
    ///         account's fills on the exchange, may say it traded. It can keep an account from being
    ///         closed as idle and nothing more.
    function noteTrade(address seat) external {
        if (msg.sender != _plan.noter) revert NotNoter();
        _funded(seat);
        activityPrint[seat] = _print(seat);
        activeSince[seat] = uint64(block.timestamp);
        emit SeatActive(seat);
    }

    /// @notice Ends a funded stage that has gone `fundedIdle` without a trade, without a breach:
    ///         the trader is paid their share of what it made, as at the pool's end, and the seat
    ///         is free once it settles. A stage whose key never worked ends this way too. Anyone
    ///         may call it. If the account has traded since it was last looked at, this records
    ///         that and ends nothing.
    function closeIdle(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt) external {
        SeatInfo storage si = _funded(seat);
        if (_look(seat)) return;
        uint256 until = uint256(activeSince[seat]) + _plan.fundedIdle;
        if (block.timestamp <= until) revert StillTrading(until);
        emit SeatIdle(seat, si.trader);
        Pool(seat).stopFunded(cancels, extraAssets, salt);
    }

    /// @notice Takes back whatever reached a free seat after it was freed.
    function sweep(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Free) revert BadSeat();
        uint64 left = CoreOps.spotUsdc(seat);
        // Nothing to take, nothing to wait for: a sweep of an empty seat changes nothing.
        if (left == 0) return;
        si.restUntil = uint64(block.timestamp) + ARM_WAIT;
        Pool(seat).withdrawOnCore(left);
        emit Swept(seat, left);
    }

    // ── the owner ────────────────────────────────────────────────

    /// @notice Moves the pool's end `term` further on, only while it has not stopped. Once the end a
    ///         reserve was set for has passed, the owner may take that reserve out in the same call:
    ///         `out1e8` of the HyperCore balance and `outEvm` of the USDC on HyperEVM, on the
    ///         conditions of any withdrawal. Before that the call only moves the end.
    function extend(uint32 term, uint64 out1e8, uint256 outEvm) external onlyOwner {
        uint64 e = endsAt();
        if (e == 0) revert NotStarted();
        if (_over()) revert Ended();
        if (term != 0 && (term < minTerm || term > maxTerm)) revert BadPlan();
        if (out1e8 != 0 || outEvm != 0) {
            // Money reserved to leave leaves once the end it was reserved for has passed, whenever
            // the call comes, at the pool's value then: never in the term's last days (the terms,
            // D5 and D6; DECISIONS 103; audit M-2). No more than the reserve's part of the pool
            // leaves (deep audit, F4), and the reserve is spent.
            if (reserve == 0 || block.timestamp <= reserveFor) revert Locked();
            if ((out1e8 + outEvm * Units.SPOT_PER_PERP) * 1e18 > reserveShare * value()) revert Locked();
            reserve = 0;
        }
        _end = e + term;
        if (term != 0) stopping = false;
        emit Extended(_end);
        if (outEvm != 0) _payEvm(outEvm);
        if (out1e8 != 0) _pay(out1e8);
    }

    /// @notice Starts the term, once, by the owner, who accepts the pool's terms by naming their hash.
    ///         Nothing is sold before.
    function begin(bytes32 terms_) external onlyOwner {
        if (_end != 0) revert Started();
        if (terms_ != _plan.terms) revert WrongTerms();
        _end = uint64(block.timestamp) + _plan.term;
        emit Begun(_end, terms_);
    }

    /// @notice The owner says the pool stops at the end of its term, or takes that back. From the
    ///         word, nothing more is sold, and the term ends no earlier than the last exam sold has
    ///         had all its time and its trader the time to take the account.
    ///         A pool that never began ends at once.
    function setStop(bool stop) external onlyOwner {
        uint64 e = endsAt();
        if (e == 0) {
            // A pool that never began has sold nothing, so nothing waits for its end: the owner's
            // stop ends it at once, and what it holds is the owner's to take back (audit M-1).
            if (!stop) revert NotStarted();
            _end = uint64(block.timestamp) - 1;
            stopping = true;
            emit Stopping(true, _end);
            return;
        }
        if (_over()) revert Ended();
        if (stop) {
            // A stop after the end a reserve was for, the term having renewed since, ends the term
            // now, as it would have at that end: those who were to leave then are not held for the
            // renewal (deep audit, F5).
            // Until the renewed end, only their part may leave (deep audit, AUD-1).
            if (reserve != 0 && block.timestamp > reserveFor) {
                lockedUntil = e;
                e = uint64(block.timestamp);
            }
            // An exam still running has all its time; one claimed or released holds nothing
            // (deep audit, F7).
            if (waiting != 0 && lastExamEnds > e) e = lastExamEnds;
        }
        _end = e;
        stopping = stop;
        emit Stopping(stop, e);
    }

    /// @notice What the owner has said will leave at the end of the term (1e8): the share in work is
    ///         measured on the pool's value without it, so that money is not sold against.
    function setReserve(uint256 reserve_) external onlyOwner {
        reserve = reserve_;
        // After an early end, the part that may leave before the renewed end stays as it was: a new
        // reserve cannot widen it (deep audit, AUD-1).
        if (block.timestamp > lockedUntil) {
            reserveFor = endsAt();
            uint256 v = value();
            reserveShare = v == 0 ? 0 : (reserve_ * 1e18 + v - 1) / v;
        }
        emit ReserveSet(reserve_);
    }

    /// @notice Once the pool has stopped: sends `amount1e8` of its HyperCore balance to the owner.
    ///         What stays has to keep the sizes still spoken for inside the plan's share, and the
    ///         money promised to exams nobody has released where it is. Before the pool stops the
    ///         owner's money is locked; `extend` is the one way some of it leaves earlier.
    function withdraw(uint64 amount1e8) external onlyOwner {
        _afterTheEnd(amount1e8);
        _pay(amount1e8);
    }

    /// @notice Once the pool has stopped: sends `amount` of the USDC it holds on HyperEVM, the
    ///         exams' prices, to the owner, on the same condition about the share.
    /// @notice Sends the platform what payments out kept for it, once its HyperCore account can take
    ///         it. Anyone may call it.
    function payFee() external {
        uint64 fee = uint64(feeOwed);
        address to = factory.feeRecipient();
        uint64 sent = CoreOps.sendableTo(to, fee);
        if (sent == 0) revert NothingToSend();
        _spend(fee, _owedOnCore());
        feeOwed = 0;
        outFlight = _recentOut() + fee;
        lastOutAt = uint64(block.timestamp);
        CoreOps.sendUsdc(to, sent);
    }

    function withdrawEvm(uint256 amount) external onlyOwner {
        _afterTheEnd(amount * Units.SPOT_PER_PERP);
        _payEvm(amount);
    }

    // ── internals ───────────────────────────────────────────────�

    function _pay(uint64 amount1e8) internal {
        _keepShare(amount1e8);
        _spend(amount1e8, _owedOnCore());
        (uint256 fee, address to) = _platformFee(amount1e8);
        // The platform's 0.5 % is kept whatever its account: sent on if its HyperCore account can
        // take it, or else kept here and owed to it, as a hand-back's is (D10; the CTO, 18 October).
        uint64 leaving = amount1e8;
        uint64 feeSent = CoreOps.sendableTo(to, uint64(fee));
        if (feeSent == 0) {
            feeOwed += fee;
            leaving -= uint64(fee);
        } else {
            CoreOps.sendUsdc(to, feeSent);
        }
        outFlight = _recentOut() + leaving;
        lastOutAt = uint64(block.timestamp);
        uint64 sent = CoreOps.sendableTo(owner, amount1e8 - uint64(fee));
        CoreOps.sendUsdc(owner, sent);
        emit Withdrawn(owner, sent);
    }

    /// A seat whose funded stage is running, as the pool and the seat both see it.
    function _funded(address seat) internal view returns (SeatInfo storage si) {
        si = seatInfo[seat];
        if (si.state != SeatState.Funded || Pool(seat).stage() != Pool.Stage.Funded) revert BadSeat();
    }

    /// Money leaves after the end; after an early one (`lockedUntil`), until the renewed end, no more
    /// than the reserve's part of the pool as it is now, and what is left of that part after `out`
    /// is the same money as a part of what is left, rounded up (deep audit, AUD-1).
    function _afterTheEnd(uint256 out) internal {
        if (!_over()) revert Locked();
        if (block.timestamp > lockedUntil) return;
        uint256 v = value();
        if (out * 1e18 > reserveShare * v) revert Locked();
        reserveShare = v > out ? (reserveShare * v - out * 1e18 + v - out - 1) / (v - out) : 0;
    }

    /// The platform's 0.5 % of `amount`, and where it goes; nothing without a recipient.
    function _platformFee(uint256 amount) internal view returns (uint256 fee, address to) {
        to = factory.feeRecipient();
        if (to != address(0)) fee = (amount * PLATFORM_FEE_BPS) / Units.BPS;
    }

    function _payEvm(uint256 amount) internal {
        _keepShare(amount * Units.SPOT_PER_PERP);
        (uint256 fee, address to) = _platformFee(amount);
        if (fee != 0) usdc.safeTransfer(to, fee);
        usdc.safeTransfer(owner, amount - fee);
        emit WithdrawnEvm(owner, amount - fee);
    }

    /// The terms are this plan's, as written: the book is the platform's, which keeps every text under
    /// its own hash and an empty one not at all, and the text holds the plan's terms and begins with
    /// `Plan 0x<hash>`, the hash of the plan as given with its two terms fields left zero. So a text
    /// cannot be accepted for a plan it was not written for, and anyone can render the text again
    /// from the plan and compare (deep audit, F13). Run once, at creation.
    function _checkTerms(Plan memory p) internal view {
        address book = p.termsBook;
        if (book.code.length == 0 || book.codehash != keccak256(type(TermsBook).runtimeCode)) revert BadPlan();
        address at = TermsBook(book).textAt(p.terms);
        if (at == address(0)) revert Unpublished();
        bytes32 terms = p.terms;
        p.terms = 0;
        p.termsBook = address(0);
        bytes memory head = abi.encodePacked("Plan ", Strings.toHexString(uint256(keccak256(abi.encode(p))), 32), "\n");
        p.terms = terms;
        p.termsBook = book;
        bytes memory text = CodeStore.read(at);
        if (text.length < head.length) revert WrongTerms();
        for (uint256 i = 0; i < head.length; ++i) {
            if (text[i] != head[i]) revert WrongTerms();
        }
    }

    function _newSeat(uint8 line) internal returns (address seat) {
        uint64 size = _plan.menu[line].size;
        // A seat sells no challenge, so only the funded half of its terms is ever read; the
        // rest are the smallest values the factory takes.
        Terms memory t = Terms({
            price: 1,
            capital: size,
            targetBps: 1,
            duration: 1,
            traderShareChallengeBps: 0,
            traderShareFundedBps: _plan.traderShareBps,
            fundedCapital: size
        });
        seat = factory.createPool(_plan.fundedRules, t);
        Pool(seat).runByOwner(_plan.afterTarget == AfterTarget.TraderStops);
        seatInfo[seat].line = line;
        seatInfo[seat].state = SeatState.Free;
        _seats.push(seat);
        ++seatsOn[line];
        emit SeatCreated(seat, line);
    }

    /// @dev A free seat of this line, resting or not (`open` waits for the rest), or a new one.
    ///      A line never has more seats than `maxSeats`: one is made only when none of its seats
    ///      is free, and fewer than `maxSeats` sizes are spoken for before this one.
    /// A sale makes sure its line has a seat for every size spoken for, so a free one is always
    /// here for a pass (deep audit, F9).
    function _takeSeat(uint8 line) internal view returns (address) {
        uint256 n = _seats.length;
        for (uint256 i = 0; i < n; ++i) {
            SeatInfo storage si = seatInfo[_seats[i]];
            if (si.line == line && si.state == SeatState.Free) return _seats[i];
        }
        revert BadSeat();
    }

    /// @dev Tops the seat up to its size. False if it already holds it.
    function _arm(address seat, SeatInfo storage si) internal returns (bool) {
        uint64 need = _plan.menu[si.line].size * Units.SPOT_PER_PERP;
        // A resting seat's balance is on its way back to the pool (`finish` or `sweep` sent it):
        // none of it is the size (deep audit, F17).
        uint64 has = block.timestamp <= si.restUntil ? 0 : CoreOps.spotUsdc(seat);
        if (has >= need) return false;
        uint64 amount = need - has;
        uint64 cost = CoreOps.exists(seat) ? amount : amount + Units.NEW_ACCOUNT_FEE;
        _spend(cost, _owedOnCore());
        si.armedAt = uint64(block.timestamp);
        CoreOps.sendUsdc(seat, amount);
        emit SeatArmed(seat, amount);
        return true;
    }

    /// @dev Counts `cost` against the pool's HyperCore balance, together with what it sent in
    ///      the last `ARM_WAIT` and with `keep`, which has to stay. The balance is the start of
    ///      the block; the count is not.
    function _spend(uint64 cost, uint256 keep) internal {
        uint64 recent = _recent();
        uint64 spot = CoreOps.spotUsdc(address(this));
        if (spot < uint256(recent) + cost + keep) revert NotEnoughFree(spot, recent, cost);
        inFlight = recent + cost;
        lastSendAt = uint64(block.timestamp);
    }

    /// @dev Whether the term has ended: only ever once the owner has said stop.
    function _over() internal view returns (bool) {
        return _end != 0 && block.timestamp > endsAt();
    }

    /// @dev The value the share in work is measured on: the pool's, less what is to leave, as a part
    ///      of the pool as it is now. A snapshot of what it was worth when said let a sale in a rising
    ///      pool leave too little for those leaving, and the round stopped a pool that could have
    ///      paid them (deep audit, N4).
    function _working() internal view returns (uint256) {
        uint256 v = value();
        uint256 r = _leaving(v);
        return v > r ? v - r : 0;
    }

    /// @dev The reserve's part of a pool worth `v`.
    function _leaving(uint256 v) internal view returns (uint256) {
        return reserve == 0 ? 0 : (v * reserveShare) / 1e18;
    }

    function _recent() internal view returns (uint64) {
        return block.timestamp > lastSendAt + ARM_WAIT ? 0 : inFlight;
    }

    /// @dev Whether an exam of `line` counts among the free ones: given away, or priced under the floor.
    function _countsFree(uint8 line, bool free) internal view returns (bool) {
        return free || ((cheapLines >> line) & 1) != 0;
    }

    /// @dev The CFO's floor, in bps of a seat's size, from the plan: the chance of a pass at all its
    ///      steps, P = product of D / (t_i + D) (D the exam's drawdown, t_i its targets), times what a
    ///      purchase costs the pool by what follows a target, s * a + l (s the trader's share; a and l,
    ///      bps, 96 and 25 where the account closes, 152 and 29 where it stays, 96 and 19 where the
    ///      trader stops), over the price the pool keeps after the platform's 0.5 %. Rounded up. No
    ///      referral payout yet: the code makes none (the terms, D12).
    function _priceFloorBps(Plan memory p) internal pure returns (uint256) {
        uint256 d = p.examRules.maxDrawdownBps;
        uint256 num = 1;
        uint256 den = 1;
        for (uint256 i = 0; i < p.exam.targetsBps.length; ++i) {
            num *= d;
            den *= p.exam.targetsBps[i] + d;
        }
        (uint256 a, uint256 l) = p.afterTarget == AfterTarget.AccountStays
            ? (152, 29)
            : p.afterTarget == AfterTarget.AccountCloses ? (uint256(96), uint256(25)) : (uint256(96), uint256(19));
        uint256 x = num * (uint256(p.traderShareBps) * a + l * Units.BPS);
        uint256 y = den * (Units.BPS - PLATFORM_FEE_BPS);
        return (x + y - 1) / y;
    }

    /// @dev Free keys in the registry beyond one for every seat spoken for.
    function _spareKeys() internal view returns (uint256) {
        uint256 k = keyRegistry.freeCount();
        return k > held ? k - held : 0;
    }

    function _recentOut() internal view returns (uint64) {
        return block.timestamp > lastOutAt + ARM_WAIT ? 0 : outFlight;
    }

    /// @dev A funded account's positions in the plan's assets, each with the notional it was entered
    ///      at. A new entry changes that even at the same size, so a trader who closes and reopens
    ///      is seen; the market alone does not. Equity is not in it: money landing on an empty
    ///      account is not a trade.
    function _print(address seat) internal view returns (bytes32 h) {
        uint32[] memory assets = _plan.fundedRules.assets;
        for (uint256 i = 0; i < assets.length; ++i) {
            PrecompileLib.Position memory pos = PrecompileLib.position(seat, CoreOps.pxIndex(assets[i]));
            h = keccak256(abi.encode(h, pos.szi, pos.entryNtl));
        }
    }

    /// @dev True if the account looks different from the last time: a trade, recorded now.
    function _look(address seat) internal returns (bool traded) {
        bytes32 h = _print(seat);
        if (h == activityPrint[seat]) return false;
        activityPrint[seat] = h;
        activeSince[seat] = uint64(block.timestamp);
        emit SeatActive(seat);
        return true;
    }

    function _startClock(address seat) internal {
        activityPrint[seat] = _print(seat);
        activeSince[seat] = uint64(block.timestamp);
    }

    /// @dev What the exams still running may ask of the pool's HyperCore balance (1e8): their
    ///      sizes, and for each the fee of an account its seat may not have yet.
    function _owedOnCore() internal view returns (uint256) {
        return promised * Units.SPOT_PER_PERP + waiting * Units.NEW_ACCOUNT_FEE;
    }

    function _unhold(uint8 line) internal {
        inWork -= _plan.menu[line].size;
        held -= 1;
        --heldOn[line];
    }

    function _keepShare(uint256 out1e8) internal view {
        uint256 v = value();
        if (out1e8 > v) revert BufferNeeded();
        if (inWork * Units.SPOT_PER_PERP * Units.BPS > (v - out1e8) * _plan.inWorkBps) revert BufferNeeded();
    }

    function _seatValue(Pool p) internal view returns (int256 v) {
        int256 eq = p.accountEquity();
        v = int256(uint256(CoreOps.spotUsdc(address(p)))) + eq * int256(uint256(Units.SPOT_PER_PERP));
        Pool.Stage st = p.stage();
        if (st == Pool.Stage.Funded) {
            v -= _share(eq - int256(p.fundedStart()));
        } else if (st == Pool.Stage.Closing) {
            if (p.fundedEndReason() != Breach.None) return v;
            if (!p.fundedResultTaken()) {
                v -= _share(eq - int256(p.fundedStart()));
            } else if (!p.fundedPayoutDone()) {
                v -= int256(uint256(p.fundedPayoutOwed()));
            } else if (
                CoreOps.spotUsdc(address(p)) + p.fundedPayoutSent() > p.fundedPayoutSpotBefore()
                    && block.timestamp <= p.fundedPayoutAt() + p.PAYOUT_WAIT()
            ) {
                // Sent and not yet gone from the seat's balance: still the trader's.
                v -= int256(uint256(p.fundedPayoutSent()));
            }
        }
    }

    /// The trader's share of `profit1e6`, in 1e8 units, the way `Pool` computes it; nothing on a loss.
    function _share(int256 profit1e6) internal view returns (int256) {
        if (profit1e6 <= 0) return 0;
        return (profit1e6 * int256(uint256(_plan.traderShareBps)) * int256(uint256(Units.SPOT_PER_PERP)))
            / int256(uint256(Units.BPS));
    }
}
