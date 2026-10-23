// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Rules, Terms, Cancel, Breach, Units} from "../Types.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {CoreOps} from "../lib/CoreOps.sol";
import {Pool} from "../Pool.sol";
import {PoolFactory} from "../PoolFactory.sol";
import {PaperExam, ExamTerms} from "../exam/PaperExam.sol";
import {FreeExams} from "./FreeExams.sol";

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
    /// How long a funded account may go without a trade, at the least and at the most.
    uint32 public constant MIN_FUNDED_IDLE = 1 days;
    uint32 public constant MAX_FUNDED_IDLE = 90 days;

    address public immutable owner;
    PoolFactory public immutable factory;
    IERC20 public immutable usdc;
    address public immutable examImpl;
    /// The shortest and the longest term a plan or an extension may name, and the shortest last
    /// stretch in which nothing is sold. Set by whoever deploys, not by the plan: 6 months, 24
    /// months and 14 days where the pool is offered, less where a whole term has to be watched.
    uint32 public immutable minTerm;
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
    /// The part of `inWork` whose money is still on the pool's own balance: exams not yet taken.
    uint256 public promised;
    /// How many exams that is.
    uint256 public waiting;

    mapping(address exam => ExamInfo) public examInfo;
    mapping(address seat => SeatInfo) public seatInfo;
    address[] internal _seats;

    /// When the pool stops: after it nothing is sold or opened, and every account is closed.
    uint64 public endsAt;
    /// The pool's free exams, where its plan has them (`FreeExams`, created with the pool).
    address public immutable freeExams;

    /// What this pool sent from its HyperCore balance within the last `ARM_WAIT` (1e8 = 1 USDC).
    uint64 public inFlight;
    uint64 public lastSendAt;
    /// What this pool sent its owner within the last `ARM_WAIT` (1e8): money that has left its
    /// books and may still show in its balance. `value()` does not count it.
    uint64 public outFlight;
    uint64 public lastOutAt;

    /// A funded account's positions and their entries as last seen, and since when they have
    /// looked that way. A change between two looks is a trade.
    mapping(address seat => bytes32) public activityPrint;
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
    event Swept(address indexed seat, uint64 amount);
    event SeatActive(address indexed seat);
    event SeatIdle(address indexed seat, address indexed trader);
    event Withdrawn(address indexed to, uint64 amount);
    event WithdrawnEvm(address indexed to, uint256 amount);
    event Extended(uint64 endsAt);
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
        uint32 notice_
    ) {
        if (owner_ == address(0) || examImpl_ == address(0)) revert BadPlan();
        if (notice_ == 0 || notice_ > MAX_NOTICE) revert BadPlan();
        // This keeps the shortest term above zero too.
        if (uint256(notice_) * NOTICE_SHARE > minTerm_) revert BadPlan();
        minTerm = minTerm_;
        maxTerm = maxTerm_;
        notice = notice_;
        owner = owner_;
        factory = factory_;
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
        if (plan_.maxRetakes != 0 && plan_.afterTarget != AfterTarget.AccountStays) revert BadPlan();
        if (plan_.fundedRules.maxLeverageX100 > MAX_LEVERAGE_X100) revert BadPlan();
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
        _plan.freePerMonth = plan_.freePerMonth;
        _plan.inviter = plan_.inviter;
        _plan.noter = plan_.noter;
        address desk;
        if (plan_.freePerMonth != 0) desk = address(new FreeExams(plan_.inviter, plan_.freePerMonth));
        freeExams = desk;
        endsAt = uint64(block.timestamp) + plan_.term;

        // One seat for every line, so the factory has said yes to the funded rules and to every
        // size before the first exam is sold on them.
        for (uint256 i = 0; i < lines; ++i) {
            _newSeat(uint8(i));
        }
    }

    // ── views ────────────────────────────────────────────────────

    function plan() external view returns (Plan memory) {
        return _plan;
    }

    function seats() external view returns (address[] memory) {
        return _seats;
    }

    /// @notice The pool's value (1e8 = 1 USDC): its spot on HyperCore, its USDC on HyperEVM, and
    ///         every seat in use at its spot and perp value, less what its trader is owed if the
    ///         stage ended now without a breach.
    function value() public view returns (uint256) {
        int256 v = int256(uint256(CoreOps.spotUsdc(address(this))));
        v += int256(usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP);
        v -= int256(uint256(_recentOut()));
        uint256 n = _seats.length;
        for (uint256 i = 0; i < n; ++i) {
            address seat = _seats[i];
            if (seatInfo[seat].state == SeatState.Free) continue;
            v += _seatValue(Pool(seat));
        }
        return v > 0 ? uint256(v) : 0;
    }

    /// @notice How many more exams of this line the pool would sell right now.
    function room(uint256 line) external view returns (uint256) {
        if (line >= _plan.menu.length) revert NoSuchLine();
        uint64 size = _plan.menu[line].size;
        if (closing()) return 0;
        uint256 n = _plan.maxSeats - held;
        uint256 cap = (value() * _plan.inWorkBps) / Units.BPS / Units.SPOT_PER_PERP;
        uint256 byShare = cap > inWork ? (cap - inWork) / size : 0;
        if (byShare < n) n = byShare;
        uint256 each = uint256(size) * Units.SPOT_PER_PERP + Units.NEW_ACCOUNT_FEE;
        uint256 owed = _owedOnCore() + _recent();
        uint256 spot = CoreOps.spotUsdc(address(this));
        uint256 byBalance = spot > owed ? (spot - owed) / each : 0;
        return byBalance < n ? byBalance : n;
    }

    /// @notice Whether the pool was deployed with the bounds it is offered with: a term of six months
    ///         to two years and fourteen days of notice. A page shows any other pool as such.
    function productBounds() external view returns (bool) {
        return minTerm == PRODUCT_MIN_TERM && maxTerm == PRODUCT_MAX_TERM && notice == MAX_NOTICE;
    }

    /// @notice Whether the pool is in the last stretch of its term, where nothing is sold, or past it.
    function closing() public view returns (bool) {
        return block.timestamp + saleLead > endsAt;
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
        if (inWorkAfter * Units.SPOT_PER_PERP * Units.BPS > value() * _plan.inWorkBps) revert NoRoom();
        inWork = inWorkAfter;
        held += 1;
        promised += m.size;
        waiting += 1;
        // The money a pass will ask for has to be where it will be paid from, now.
        if (CoreOps.spotUsdc(address(this)) < _owedOnCore() + _recent()) revert NoRoom();

        if (!free) {
            usdc.safeTransferFrom(msg.sender, address(this), m.price);
            uint256 fee = factory.challengeFee();
            if (fee != 0) {
                address recipient = factory.feeRecipient();
                usdc.safeTransferFrom(msg.sender, recipient, fee);
                emit ExamFeePaid(msg.sender, recipient, fee);
            }
        }

        exam = Clones.clone(examImpl);
        ExamTerms memory t = _plan.exam;
        t.capital = m.size;
        PaperExam(exam).initialize(trader, _plan.examRules, t);
        examInfo[exam] = ExamInfo({line: uint8(line), state: ExamState.Sold, seat: address(0)});
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
        x.seat = seat;
        promised -= _plan.menu[x.line].size;
        waiting -= 1;
        SeatInfo storage si = seatInfo[seat];
        si.state = SeatState.Armed;
        si.trader = trader;
        emit Claimed(exam, trader, seat);
        _arm(seat, si);
    }

    /// @notice Takes a seat again after its target, for the trader it is waiting for.
    function reopen(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Waiting) revert BadSeat();
        if (msg.sender != si.trader) revert NotTrader();
        if (block.timestamp > si.waitUntil) revert TooLate();
        if (block.timestamp > endsAt) revert Ended();
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
            || (s == PaperExam.Status.Passed && block.timestamp > uint256(passedAt) + _plan.claimWindow)
            || block.timestamp > endsAt;
        if (!over) revert StillRunning();
        x.state = ExamState.Released;
        uint64 size = _plan.menu[x.line].size;
        promised -= size;
        waiting -= 1;
        _unhold(size);
        emit ExamReleased(exam);
    }

    /// @notice Starts the funded account on a seat whose money has arrived.
    function open(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Armed) revert BadSeat();
        if (block.timestamp > endsAt) revert Ended();
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
        SeatInfo storage si = seatInfo[seat];
        Pool p = Pool(seat);
        if (si.state != SeatState.Funded || p.stage() != Pool.Stage.Funded) revert BadSeat();
        if (CoreOps.margin(seat).ntlPos != 0) revert NotFlat();
        Breach reason = p.violation(new uint32[](0));
        if (reason != Breach.None) revert RuleBroken(reason);
        int256 start = p.fundedStart();
        int256 eq = CoreOps.equity(seat);
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
        bool over = block.timestamp > endsAt;
        // A seat whose money was sent and whose account never started has nothing to settle; it
        // is freed only once the pool has stopped.
        if (si.state == SeatState.Armed ? !over : si.state != SeatState.Funded && si.state != SeatState.Waiting) {
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
        si.state = SeatState.Free;
        si.targetHit = false;
        si.trader = address(0);
        si.retakes = 0;
        si.restUntil = uint64(block.timestamp) + ARM_WAIT;
        _unhold(_plan.menu[si.line].size);
        if (back != 0) Pool(seat).withdrawOnCore(back);
        emit SeatFreed(seat, back);
    }

    /// @notice Once the pool has stopped: ends a funded stage, without a breach. The trader is
    ///         paid their share of what it made when the stage settles. Anyone may call it.
    function endSeat(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt) external {
        if (block.timestamp <= endsAt) revert NotEnded();
        SeatInfo storage si = seatInfo[seat];
        Pool p = Pool(seat);
        if (si.state != SeatState.Funded || p.stage() != Pool.Stage.Funded) revert BadSeat();
        emit SeatEnded(seat, si.trader);
        p.stopFunded(cancels, extraAssets, salt);
    }

    /// @notice Records that a funded account has traded since it was last looked at, if it has.
    ///         Anyone may call it; a keeper does, so that a trader who traded is not taken for one
    ///         who did not.
    function noteActivity(address seat) external {
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Funded || Pool(seat).stage() != Pool.Stage.Funded) revert BadSeat();
        _look(seat);
    }

    /// @notice Records a trade the chain cannot see. An account that opens and closes between two
    ///         looks is flat at both, and to the chain a round trip from flat to flat looks like a
    ///         gift landing on the account. Only the plan's `noter`, the keeper that reads the
    ///         account's fills on the exchange, may say it traded. It can keep an account from being
    ///         closed as idle and nothing more.
    function noteTrade(address seat) external {
        if (msg.sender != _plan.noter) revert NotNoter();
        SeatInfo storage si = seatInfo[seat];
        if (si.state != SeatState.Funded || Pool(seat).stage() != Pool.Stage.Funded) revert BadSeat();
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
        SeatInfo storage si = seatInfo[seat];
        Pool p = Pool(seat);
        if (si.state != SeatState.Funded || p.stage() != Pool.Stage.Funded) revert BadSeat();
        if (_look(seat)) return;
        uint256 until = uint256(activeSince[seat]) + _plan.fundedIdle;
        if (block.timestamp <= until) revert StillTrading(until);
        emit SeatIdle(seat, si.trader);
        p.stopFunded(cancels, extraAssets, salt);
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

    /// @notice Moves the pool's end `term` further on, only while it has not stopped. In the last
    ///         `notice` of a term the owner may take money out in the same call: `out1e8` of the
    ///         HyperCore balance and `outEvm` of the USDC on HyperEVM, on the conditions of any
    ///         withdrawal. Earlier the call only moves the end. The stretch with no exam sold may
    ///         be longer than `notice`; the money's window is not.
    function extend(uint32 term, uint64 out1e8, uint256 outEvm) external onlyOwner {
        if (block.timestamp > endsAt) revert Ended();
        if (term < minTerm || term > maxTerm) revert BadPlan();
        if ((out1e8 != 0 || outEvm != 0) && block.timestamp + notice <= endsAt) revert Locked();
        endsAt += term;
        emit Extended(endsAt);
        if (outEvm != 0) _payEvm(outEvm);
        if (out1e8 != 0) _pay(out1e8);
    }

    /// @notice Once the pool has stopped: sends `amount1e8` of its HyperCore balance to the owner.
    ///         What stays has to keep the sizes still spoken for inside the plan's share, and the
    ///         money promised to exams nobody has released where it is. Before the pool stops the
    ///         owner's money is locked; `extend` is the one way some of it leaves earlier.
    function withdraw(uint64 amount1e8) external onlyOwner {
        if (block.timestamp <= endsAt) revert Locked();
        _pay(amount1e8);
    }

    /// @notice Once the pool has stopped: sends `amount` of the USDC it holds on HyperEVM, the
    ///         exams' prices, to the owner, on the same condition about the share.
    function withdrawEvm(uint256 amount) external onlyOwner {
        if (block.timestamp <= endsAt) revert Locked();
        _payEvm(amount);
    }

    // ── internals ───────────────────────────────────────────────�

    function _pay(uint64 amount1e8) internal {
        _keepShare(amount1e8);
        _spend(amount1e8, _owedOnCore());
        outFlight = _recentOut() + amount1e8;
        lastOutAt = uint64(block.timestamp);
        uint64 sent = CoreOps.sendableTo(owner, amount1e8);
        CoreOps.sendUsdc(owner, sent);
        emit Withdrawn(owner, sent);
    }

    function _payEvm(uint256 amount) internal {
        _keepShare(amount * Units.SPOT_PER_PERP);
        usdc.safeTransfer(owner, amount);
        emit WithdrawnEvm(owner, amount);
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
        emit SeatCreated(seat, line);
    }

    /// @dev A free seat of this line, resting or not (`open` waits for the rest), or a new one.
    ///      A line never has more seats than `maxSeats`: one is made only when none of its seats
    ///      is free, and fewer than `maxSeats` sizes are spoken for before this one.
    function _takeSeat(uint8 line) internal returns (address) {
        uint256 n = _seats.length;
        for (uint256 i = 0; i < n; ++i) {
            SeatInfo storage si = seatInfo[_seats[i]];
            if (si.line == line && si.state == SeatState.Free) return _seats[i];
        }
        return _newSeat(line);
    }

    /// @dev Tops the seat up to its size. False if it already holds it.
    function _arm(address seat, SeatInfo storage si) internal returns (bool) {
        uint64 need = _plan.menu[si.line].size * Units.SPOT_PER_PERP;
        uint64 has = CoreOps.spotUsdc(seat);
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

    function _recent() internal view returns (uint64) {
        return block.timestamp > lastSendAt + ARM_WAIT ? 0 : inFlight;
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
            PrecompileLib.Position memory pos = PrecompileLib.position(seat, assets[i]);
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

    function _unhold(uint64 size) internal {
        inWork -= size;
        held -= 1;
    }

    function _keepShare(uint256 out1e8) internal view {
        uint256 v = value();
        if (out1e8 > v) revert BufferNeeded();
        if (inWork * Units.SPOT_PER_PERP * Units.BPS > (v - out1e8) * _plan.inWorkBps) revert BufferNeeded();
    }

    function _seatValue(Pool p) internal view returns (int256 v) {
        int256 eq = CoreOps.equity(address(p));
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
