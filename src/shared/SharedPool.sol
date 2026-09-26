// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {Rules, Terms, Cancel, Breach, Units} from "../Types.sol";
import {CoreOps} from "../lib/CoreOps.sol";
import {Pool} from "../Pool.sol";
import {PoolFactory} from "../PoolFactory.sol";
import {ChallengeAccount} from "../ChallengeAccount.sol";
import {DepositTicket} from "./DepositTicket.sol";

/// @title SharedPool
/// @notice Many investors' capital in one book of seats. Each seat is an ordinary `Pool` this contract
///         owns, with its own challenge and funded stage; the platform publishes the seats, their rules
///         and terms before anyone deposits. A share is a claim on a part of the pool's value.
///
///         A deposit arrives on HyperCore at an address made for that deposit (`DepositTicket`) and
///         becomes shares at the next settlement point, priced at the pool's value without it. A request
///         to withdraw locks shares that keep bearing the pool's result until settlement points pay them,
///         at the price of the point that pays, all requests alike. The pool's value is what it would
///         have if every account were closed at the mark now and everyone it owes were paid; `value()`
///         spells it out.
/// @dev Value and shares are in HyperCore spot units, 1e8 = 1 USDC; the first shares are one per unit.
///      Settlement points are open to anyone. `blocker()` is the one place that decides whether a point
///      may run: while a loss is still moving or a payment is in flight, no price is struck.
///      Testnet only; not part of the reviewed core.
contract SharedPool {
    using SafeERC20 for IERC20;

    enum Blocker {
        None,
        RuleBroken,
        SeatClosing,
        PositionsOpen,
        PayoutInFlight,
        PaymentInFlight
    }

    enum TicketState {
        None,
        Open,
        Closed
    }

    struct Ticket {
        address depositor;
        TicketState state;
    }

    /// What the pool has paid a holder so far, each part where it was paid, and when it last paid them.
    /// `evm` is USDC on HyperEVM (1e6 = 1 USDC). `core` is the spot USDC the pool sent to the holder's
    /// HyperCore account (1e8 = 1 USDC), after the 1 USDC a holder who had no account there paid out of
    /// their first HyperCore part for creating it.
    struct Payments {
        uint64 at;
        uint96 evm;
        uint96 core;
    }

    /// The seat's terms that its value depends on, copied at creation: a pool's terms never change.
    struct SeatTerms {
        uint64 capital;
        uint16 targetBps;
        uint16 shareChallengeBps;
        uint16 shareFundedBps;
    }

    /// The platform's starting shares must be worth at least this much of the seat plan (bps).
    uint256 public constant SEED_BPS = 500;
    /// Time a top-up of a seat is given to land before the seat can be topped up again.
    uint256 public constant ARM_WAIT = 1 minutes;
    uint256 public constant MAX_SEATS = 12;
    uint256 public constant MAX_PER_POINT = 8;
    /// Less than this left on a closed ticket is dust: not swept, not counted, forgotten. Keeping a
    /// ticket on the list, which every settlement point reads, then costs whoever tries 1 USDC a point,
    /// and the pool keeps it.
    uint64 public constant SWEEP_MIN = 1e8;
    /// Holders waiting to be paid at once; a request past this waits for the queue to move.
    uint256 public constant MAX_QUEUE = 16;
    /// The pool's own HyperCore payments are given this long to land before the next point or top-up,
    /// the same wait the core contracts give a payout.
    uint64 public constant PAYOUT_WAIT = 5 minutes;

    PoolFactory public immutable factory;
    IERC20 public immutable usdc;
    address public immutable ticketImpl;
    address public immutable operator;
    /// Holder of the starting shares, which never leave.
    address public immutable platform;
    /// Smallest deposit a settlement point takes (1e8 = 1 USDC).
    uint64 public immutable minDeposit;
    /// How long after a holder's latest deposit they may ask to withdraw (seconds).
    uint32 public immutable lock;
    /// The platform's share of a holder's profit, taken when they withdraw (bps).
    uint16 public immutable feeBps;

    uint256 public totalShares;
    /// Set the first time a deposit from outside becomes shares. From then on the set of seats
    /// is fixed: holders put money into the pool they could read, and that is the pool they get.
    bool public depositsBegun;
    mapping(address holder => uint256) public sharesOf;
    uint256 public seedValue;
    uint256 public seedShares;
    /// What a holder's shares cost them (1e8); their profit is counted from it.
    mapping(address holder => uint256) public basis;
    mapping(address holder => uint64) public lastDeposit;

    /// Shares a holder asked to withdraw and hasn't been paid for yet; still theirs, still counted.
    mapping(address holder => uint256) public queuedOf;
    uint256 public queuedShares;
    address[] internal _queue;
    mapping(address holder => uint256) internal _queueSlot; // index + 1
    /// Kept so the app can show a payment's two parts apart from the contract's state alone: the public
    /// RPC answers `eth_getLogs` for 50 blocks at a time, which can't find a payment made a day ago.
    mapping(address holder => Payments) public payments;
    /// When the pool last paid anyone on HyperCore, and when it last topped a seat up there.
    uint64 public lastCorePayAt;
    uint64 public lastArmAt;

    /// Sum of the seats' `capitalNeeded()` (1e8).
    uint256 public planCapital;
    address[] internal _seats;
    mapping(address seat => bool) public isSeat;
    mapping(address seat => SeatTerms) public seatTerms;
    mapping(address seat => uint64) public armedAt;
    /// How long a funded stage may run on a seat, published with the seat (seconds).
    mapping(address seat => uint32) public fundedTerm;
    /// When the pool first saw the seat's current funded stage, and that stage's agent key: every funded
    /// stage gets a new key, so a new stage is never taken for the old one.
    mapping(address seat => uint64) public fundedSince;
    mapping(address seat => address) public fundedKey;

    mapping(address ticket => Ticket) public tickets;
    mapping(address depositor => uint256) public ticketCount;
    address[] internal _open;
    mapping(address ticket => uint256) internal _openSlot; // index + 1
    /// Recognized tickets that may still hold money; counted in the value until they are empty.
    address[] internal _closed;
    mapping(address ticket => uint256) internal _closedSlot; // index + 1

    uint256 public points;
    uint64 public lastPoint;

    event Started(address indexed platform, uint256 seed);
    event SeatAdded(address indexed seat, uint256 capitalNeeded, uint32 fundedTerm);
    event SeatArmed(address indexed seat, uint64 amount);
    event SeatReleased(address indexed seat, uint64 amount);
    event FundedNoted(address indexed seat, address indexed key, uint64 since);
    event FundedTermEnded(address indexed seat);
    event RedeemRequested(address indexed holder, uint256 shares);
    event Paid(address indexed holder, uint256 shares, uint256 feeShares, uint256 evmAmount, uint64 coreAmount);
    event TicketOpened(address indexed depositor, address indexed ticket, uint256 index);
    event DepositRecognized(address indexed depositor, address indexed ticket, uint256 amount, uint256 shares);
    event TicketSwept(address indexed ticket, uint64 amount);
    event TicketEmptied(address indexed ticket);
    event PointSettled(uint256 indexed point, uint256 value, uint256 sharesBefore, uint256 sharesAfter);

    error NotOperator();
    error NotStarted();
    error AlreadyStarted();
    error NotReady();
    error BadDeposit();
    error SeedTooSmall(uint256 seedValue, uint256 planCapital);
    error TooMany();
    error SeatsClosed();
    error NotSeat(address seat);
    error SeatBusy(address seat);
    error TooSoon(address seat);
    error AlreadyArmed(address seat);
    error NotEnoughFree(uint64 free, uint64 needed);
    error NotQuiet(Blocker reason, address account);
    error NoValue();
    error BadFee();
    error BadTerm();
    error NothingRequested();
    error NotFree(uint256 free);
    error Locked(uint64 until);
    error QueueWaiting();
    error NoQueue();
    error QueueCovered();
    error PaymentsLanding(uint64 until);
    error NotFunded(address seat);
    error TermNotOver(uint64 until);

    constructor(
        PoolFactory factory_,
        address operator_,
        address platform_,
        uint64 minDeposit_,
        uint32 lock_,
        uint16 feeBps_
    ) {
        // A deposit under SWEEP_MIN would be closed and forgotten as dust in the same point that minted
        // its shares: shares for money the pool never takes in.
        if (minDeposit_ < SWEEP_MIN) revert BadDeposit();
        if (feeBps_ > Units.BPS) revert BadFee();
        factory = factory_;
        usdc = factory_.usdc();
        operator = operator_;
        platform = platform_;
        minDeposit = minDeposit_;
        lock = lock_;
        feeBps = feeBps_;
        ticketImpl = address(new DepositTicket());
    }

    modifier onlyOperator() {
        if (msg.sender != operator) revert NotOperator();
        _;
    }

    modifier started() {
        if (totalShares == 0) revert NotStarted();
        _;
    }

    // ── the platform ─────────────────────────────────────────────────────────────────

    /// @notice Turns what the platform sent to this contract's HyperCore address into the starting
    ///         shares, once. They belong to the platform and never leave, which is also what makes the
    ///         first depositors safe from someone inflating the price of a nearly empty pool.
    function start() external onlyOperator {
        if (totalShares != 0) revert AlreadyStarted();
        if (!CoreOps.exists(address(this))) revert NotReady();
        uint256 seed = CoreOps.spotUsdc(address(this));
        if (seed == 0) revert NotReady();
        // Spot capital waiting in the pool never backs a position, whatever mode HyperCore defaults to.
        CoreOps.separateBalances(address(this));
        seedValue = seed;
        seedShares = seed;
        _mint(platform, seed);
        emit Started(platform, seed);
    }

    /// @notice Publishes a seat: a new pool owned by this contract, with the platform's rules and terms,
    ///         and how long a funded stage may run on it. The starting shares must stay worth at least
    ///         `SEED_BPS` of the whole seat plan.
    function addSeat(Rules calldata rules_, Terms calldata terms_, uint32 fundedTerm_)
        external
        onlyOperator
        started
        returns (address seat)
    {
        // Audit A-06. This contract's own header and docs/SHARED-POOL.md both promise that the
        // seats, their rules and their terms are published BEFORE anyone deposits, and nothing
        // used to hold the operator to it. A seat added afterwards -- 99.99% drawdown, 50x
        // leverage, the whole profit to the trader -- would take holders' money the next time
        // anyone armed a seat, and they cannot leave quickly: only a queue, a lock and
        // settlement points. So the promise is now the rule.
        if (depositsBegun) revert SeatsClosed();
        if (_seats.length >= MAX_SEATS) revert TooMany();
        if (fundedTerm_ == 0) revert BadTerm();
        seat = factory.createPool(rules_, terms_);
        uint256 need = Pool(seat).capitalNeeded();
        uint256 plan = planCapital + need;
        if (seedValue * Units.BPS < plan * SEED_BPS) revert SeedTooSmall(seedValue, plan);
        planCapital = plan;
        _seats.push(seat);
        isSeat[seat] = true;
        fundedTerm[seat] = fundedTerm_;
        seatTerms[seat] = SeatTerms({
            capital: terms_.capital,
            targetBps: terms_.targetBps,
            shareChallengeBps: terms_.traderShareChallengeBps,
            shareFundedBps: terms_.traderShareFundedBps
        });
        emit SeatAdded(seat, need, fundedTerm_);
    }

    // ── seats ────────────────────────────────────────────────────────────────────────

    /// @notice Tops an idle seat up to what it needs to sell a challenge, from the pool's spot on
    ///         HyperCore. A seat that has no HyperCore account yet costs the pool 1 USDC more, once;
    ///         after that anyone calls `prepareAccount` on it. Anyone may call this.
    function armSeat(address seat) external started {
        if (!isSeat[seat]) revert NotSeat(seat);
        // The pool's last payments may not show in its balance yet; the money is already theirs.
        _paymentsLanded();
        Pool p = Pool(seat);
        if (p.stage() != Pool.Stage.Idle || p.challenge() != address(0)) revert SeatBusy(seat);
        // The previous top-up may not show in the balance yet; sending again would send twice.
        if (armedAt[seat] != 0 && block.timestamp <= armedAt[seat] + ARM_WAIT) revert TooSoon(seat);
        uint64 need = p.capitalNeeded();
        uint64 has = CoreOps.spotUsdc(seat);
        if (has >= need) revert AlreadyArmed(seat);
        uint64 amount = need - has;
        uint64 cost = CoreOps.exists(seat) ? amount : amount + Units.NEW_ACCOUNT_FEE;
        uint64 free = CoreOps.spotUsdc(address(this));
        if (free < cost) revert NotEnoughFree(free, cost);
        // While someone waits to be paid, a seat is armed only from money the queue doesn't need: what
        // is left on HyperCore after the top-up, with the USDC on HyperEVM, must still cover the queue.
        if (queuedShares != 0) {
            uint256 left = uint256(free - cost) + usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP;
            if (left < _queuedValue()) revert QueueWaiting();
        }
        armedAt[seat] = uint64(block.timestamp);
        lastArmAt = uint64(block.timestamp);
        CoreOps.sendUsdc(seat, amount);
        emit SeatArmed(seat, amount);
    }

    /// @notice While the queue needs more than the pool has free, moves an idle seat's capital back into
    ///         the pool. Anyone may call it.
    function releaseSeat(address seat) external started {
        if (!isSeat[seat]) revert NotSeat(seat);
        if (queuedShares == 0) revert NoQueue();
        uint256 free = uint256(CoreOps.spotUsdc(address(this))) + usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP;
        if (free >= _queuedValue()) revert QueueCovered();
        Pool p = Pool(seat);
        if (p.stage() != Pool.Stage.Idle || p.challenge() != address(0)) revert SeatBusy(seat);
        uint64 held = CoreOps.spotUsdc(seat);
        p.withdrawOnCore(held);
        emit SeatReleased(seat, held);
    }

    /// @notice Moves the challenge prices a seat has earned to this contract on HyperEVM. The value
    ///         doesn't change; anyone may call it.
    function collect(address seat) external {
        if (!isSeat[seat]) revert NotSeat(seat);
        Pool(seat).withdrawEarned();
    }

    /// @notice Records when the seat's current funded stage began, as far as the pool can tell: the first
    ///         time anyone calls this, or a settlement point runs, while the stage is on. The keeper calls
    ///         it on every pass.
    function noteFunded(address seat) external {
        if (!isSeat[seat]) revert NotSeat(seat);
        _note(Pool(seat));
    }

    /// @notice Ends a funded stage that has run its published term, through `Pool.stopFunded`: no breach,
    ///         so the trader is paid their share of the profit when the stage settles. Anyone may call it.
    function endFundedTerm(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        external
    {
        if (!isSeat[seat]) revert NotSeat(seat);
        Pool p = Pool(seat);
        _note(p);
        uint64 since = fundedSince[seat];
        if (since == 0) revert NotFunded(seat);
        uint64 until = since + fundedTerm[seat];
        if (block.timestamp < until) revert TermNotOver(until);
        fundedSince[seat] = 0;
        fundedKey[seat] = address(0);
        p.stopFunded(cancels, extraAssets, salt);
        emit FundedTermEnded(seat);
    }

    // ── withdrawals ──────────────────────────────────────────────────────────────────

    /// @notice Asks to withdraw `shares`. They stay the caller's and keep bearing the pool's result until
    ///         settlement points pay them, at the price of the point that pays; a request can't be taken
    ///         back. Allowed once `lock` has passed since the caller's latest deposit. The platform's
    ///         starting shares never leave.
    function requestRedeem(uint256 shares) external started {
        if (shares == 0) revert NothingRequested();
        uint256 held = queuedOf[msg.sender] + (msg.sender == platform ? seedShares : 0);
        uint256 free = sharesOf[msg.sender] > held ? sharesOf[msg.sender] - held : 0;
        if (shares > free) revert NotFree(free);
        uint64 until = lastDeposit[msg.sender] + lock;
        if (block.timestamp < until) revert Locked(until);
        if (_queueSlot[msg.sender] == 0) {
            if (_queue.length >= MAX_QUEUE) revert TooMany();
            _queue.push(msg.sender);
            _queueSlot[msg.sender] = _queue.length;
        }
        queuedOf[msg.sender] += shares;
        queuedShares += shares;
        emit RedeemRequested(msg.sender, shares);
    }

    // ── deposits ─────────────────────────────────────────────────────────────────────

    /// @notice Opens an address for one deposit by the caller. Send USDC to it on HyperCore; the next
    ///         settlement point that names it turns what it holds into shares. The first transfer to
    ///         the address creates its HyperCore account, which HyperCore charges the sender 1 USDC for.
    function openTicket() external started returns (address ticket) {
        uint256 n = ticketCount[msg.sender]++;
        ticket = Clones.cloneDeterministic(ticketImpl, _salt(msg.sender, n));
        DepositTicket(ticket).init(msg.sender);
        tickets[ticket] = Ticket({depositor: msg.sender, state: TicketState.Open});
        _open.push(ticket);
        _openSlot[ticket] = _open.length;
        emit TicketOpened(msg.sender, ticket, n);
    }

    /// @notice Where `depositor`'s ticket number `n` is or will be.
    function ticketAddress(address depositor, uint256 n) external view returns (address) {
        return Clones.predictDeterministicAddress(ticketImpl, _salt(depositor, n));
    }

    // ── settlement points ────────────────────────────────────────────────────────────

    /// @notice A settlement point: turns the named open tickets that hold at least `minDeposit` into
    ///         shares, all at one price, the pool's value before them; closes those tickets; and sweeps
    ///         whatever the closed tickets hold into the pool. Anyone may call it, whenever `blocker()`
    ///         says nothing is in motion.
    function settle(address[] calldata recognize) external started {
        if (recognize.length > MAX_PER_POINT) revert TooMany();
        (Blocker reason, address account) = blocker();
        if (reason != Blocker.None) revert NotQuiet(reason, account);
        uint256 poolValue = _value();
        if (poolValue == 0) revert NoValue();
        uint256 sharesBefore = totalShares;

        for (uint256 i = 0; i < recognize.length; ++i) {
            address t = recognize[i];
            Ticket storage tk = tickets[t];
            if (tk.state == TicketState.Closed) {
                // Money sent to a ticket after it was counted and emptied belongs to the pool too;
                // naming the ticket takes it in. Dust drops off again in the sweep below.
                if (_closedSlot[t] == 0) _addClosed(t);
                continue;
            }
            // A ticket that was never opened here is passed over: the caller's list is only a list.
            if (tk.state != TicketState.Open) continue;
            uint64 amount = CoreOps.spotUsdc(t);
            if (amount < minDeposit) continue;
            uint256 minted = (uint256(amount) * sharesBefore) / poolValue;
            tk.state = TicketState.Closed;
            _dropOpen(t);
            _addClosed(t);
            _mint(tk.depositor, minted);
            basis[tk.depositor] += amount;
            lastDeposit[tk.depositor] = uint64(block.timestamp);
            depositsBegun = true;
            emit DepositRecognized(tk.depositor, t, amount, minted);
        }
        _sweepClosed();
        for (uint256 i = 0; i < _seats.length; ++i) {
            _note(Pool(_seats[i]));
        }
        if (queuedShares != 0) _pay(poolValue, sharesBefore);

        lastPoint = uint64(block.timestamp);
        emit PointSettled(++points, poolValue, sharesBefore, totalShares);
    }

    /// @notice Whether a settlement point may run now, and if not, the reason and the account holding
    ///         it up. This is the only rule settlement points obey; anyone can clear what it names by
    ///         calling `breach` or `settle` on that account.
    /// @dev What is not here on purpose: a transfer between two of the pool's own accounts. HyperCore
    ///      debits and credits it in one step and a read sees both sides from the same state, so the
    ///      value doesn't change while it is in flight (measured on the testnet on 24 Sep 2026).
    function blocker() public view returns (Blocker, address) {
        if (lastCorePayAt != 0 && block.timestamp <= lastCorePayAt + PAYOUT_WAIT) {
            return (Blocker.PaymentInFlight, address(this));
        }
        uint32[] memory none = new uint32[](0);
        for (uint256 i = 0; i < _seats.length; ++i) {
            Pool p = Pool(_seats[i]);
            Pool.Stage st = p.stage();
            if (st == Pool.Stage.Closing) return (Blocker.SeatClosing, address(p));
            if (st == Pool.Stage.Funded && p.violation(none) != Breach.None) return (Blocker.RuleBroken, address(p));
            address ch = p.challenge();
            if (ch == address(0)) continue;
            ChallengeAccount c = ChallengeAccount(ch);
            ChallengeAccount.Status s = c.status();
            if (s == ChallengeAccount.Status.Active) {
                if (c.violation(none) != Breach.None) return (Blocker.RuleBroken, ch);
            } else if (
                s == ChallengeAccount.Status.Breached || s == ChallengeAccount.Status.Expired
                    || s == ChallengeAccount.Status.Forfeited
            ) {
                if (CoreOps.margin(ch).ntlPos != 0) return (Blocker.PositionsOpen, ch);
            } else if (s == ChallengeAccount.Status.Passed) {
                if (c.payoutDone() && _payoutLanding(c)) return (Blocker.PayoutInFlight, ch);
            }
        }
        return (Blocker.None, address(0));
    }

    // ── value ────────────────────────────────────────────────────────────────────────

    /// @notice The pool's value (1e8 = 1 USDC): its spot on HyperCore and its USDC on HyperEVM; each
    ///         seat's spot and perp value, the challenge prices it has earned, and its running
    ///         challenge's spot and perp value; closed tickets not yet swept. Less what traders are owed:
    ///         a passed challenge's share not yet paid, and on a funded seat or a challenge that has met
    ///         its target, the trader's share of the profit as if the stage ended now without a breach.
    ///         While `blocker()` names something, this is indicative only.
    function value() external view returns (uint256) {
        return _value();
    }

    function _value() internal view returns (uint256) {
        int256 v = int256(uint256(CoreOps.spotUsdc(address(this))));
        v += int256(usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP);
        for (uint256 i = 0; i < _seats.length; ++i) {
            v += _seatValue(Pool(_seats[i]));
        }
        for (uint256 i = 0; i < _closed.length; ++i) {
            v += int256(uint256(CoreOps.spotUsdc(_closed[i])));
        }
        return v > 0 ? uint256(v) : 0;
    }

    function _seatValue(Pool p) internal view returns (int256 v) {
        SeatTerms memory t = seatTerms[address(p)];
        int256 eq = CoreOps.equity(address(p));
        v = int256(uint256(CoreOps.spotUsdc(address(p)))) + eq * int256(uint256(Units.SPOT_PER_PERP))
            + int256(p.earned() * Units.SPOT_PER_PERP);
        Pool.Stage st = p.stage();
        if (st == Pool.Stage.Funded) {
            v -= _share(eq - int256(p.fundedStart()), t.shareFundedBps);
        } else if (st == Pool.Stage.Closing) {
            v -= _closingOwed(p, eq, t.shareFundedBps);
        }
        address ch = p.challenge();
        if (ch != address(0)) v += _challengeValue(ChallengeAccount(ch), t);
    }

    function _challengeValue(ChallengeAccount c, SeatTerms memory t) internal view returns (int256 v) {
        int256 eq = CoreOps.equity(address(c));
        v = int256(uint256(CoreOps.spotUsdc(address(c)))) + eq * int256(uint256(Units.SPOT_PER_PERP));
        ChallengeAccount.Status s = c.status();
        if (s == ChallengeAccount.Status.Passed) {
            if (!c.payoutDone()) v -= int256(uint256(c.payoutOwed()));
            else if (_payoutLanding(c)) v -= int256(uint256(c.payoutSent()));
        } else if (s == ChallengeAccount.Status.Active) {
            int256 capital = int256(uint256(t.capital));
            int256 target = capital + (capital * int256(uint256(t.targetBps))) / int256(uint256(Units.BPS));
            if (eq >= target) v -= _share(eq - capital, t.shareChallengeBps);
        }
    }

    /// Indicative only: a closing seat holds settlement points up.
    function _closingOwed(Pool p, int256 eq, uint16 shareBps) internal view returns (int256) {
        if (p.fundedEndReason() != Breach.None) return 0;
        if (!p.fundedResultTaken()) return _share(eq - int256(p.fundedStart()), shareBps);
        return p.fundedPayoutDone() ? int256(0) : int256(uint256(p.fundedPayoutOwed()));
    }

    /// A trader's share of `profit1e6`, in 1e8 units, the way the contracts compute it; nothing on a loss.
    function _share(int256 profit1e6, uint16 bps) internal pure returns (int256) {
        if (profit1e6 <= 0) return 0;
        return (profit1e6 * int256(uint256(bps)) * int256(uint256(Units.SPOT_PER_PERP))) / int256(uint256(Units.BPS));
    }

    /// The same test `ChallengeAccount.settle` uses: the payout was sent and hasn't shown in the balance,
    /// and the wait for it isn't over.
    function _payoutLanding(ChallengeAccount c) internal view returns (bool) {
        return CoreOps.spotUsdc(address(c)) + c.payoutSent() > c.payoutSpotBefore()
            && block.timestamp <= c.payoutAt() + c.PAYOUT_WAIT();
    }

    // ── payment ──────────────────────────────────────────────────────────────────────

    /// Pays the queue at this point's price (`poolValue / sharesBefore`), every request alike: in full if
    /// the pool's free money covers it, otherwise the same fraction of each. Seats' earned prices are
    /// collected first, so USDC on HyperEVM pays first and HyperCore the rest, in the same proportion for
    /// every holder. The platform's fee is its share of the holder's profit over what their shares cost
    /// them; it stays in the pool as the platform's shares instead of being paid out.
    function _pay(uint256 poolValue, uint256 sharesBefore) internal {
        // A top-up sent to a seat moments ago may not show in the balance read below yet; paying out of
        // that balance would send money that is already gone. The queue waits for the next point.
        if (lastArmAt != 0 && block.timestamp <= lastArmAt + ARM_WAIT) return;
        for (uint256 i = 0; i < _seats.length; ++i) {
            if (Pool(_seats[i]).earned() != 0) Pool(_seats[i]).withdrawEarned();
        }
        // A copy: paying a holder in full takes them off the queue, which reorders it.
        address[] memory holders = _queue;
        uint256 n = holders.length;
        uint256[] memory burn = new uint256[](n);
        uint256[] memory feeShares = new uint256[](n);
        uint256[] memory net6 = new uint256[](n);

        // In full first, to see whether the money covers it.
        uint256 wanted6 = _quote(holders, poolValue, sharesBefore, 1, 1, burn, feeShares, net6);
        // A request worth less than the smallest amount a payment carries is cleared now: its shares are
        // burned for nothing (under a millionth of a dollar), so dust can never keep the queue waiting.
        for (uint256 k = 0; k < n; ++k) {
            if (net6[k] != 0) continue;
            address h = holders[k];
            uint256 dust = queuedOf[h];
            _settleHolder(h, dust, 0);
            _dropQueue(h);
            emit Paid(h, dust, 0, 0, 0);
            holders[k] = address(0);
        }
        uint256 evm6 = usdc.balanceOf(address(this));
        uint256 core6 = CoreOps.spotUsdc(address(this)) / Units.SPOT_PER_PERP;
        // A unit per holder is left for the rounding of each holder's split between the two.
        uint256 have6 = evm6 + core6 > n ? evm6 + core6 - n : 0;
        if (wanted6 > have6) {
            if (have6 == 0) return;
            wanted6 = _quote(holders, poolValue, sharesBefore, have6, wanted6, burn, feeShares, net6);
        }
        if (wanted6 == 0) return;
        uint256 fromEvm6 = evm6 < wanted6 ? evm6 : wanted6;

        bool core;
        for (uint256 k = 0; k < n; ++k) {
            address h = holders[k];
            // Cleared above, or a share of a short payment too small to carry: nothing burned, nothing
            // sent, the request waits.
            if (h == address(0) || net6[k] == 0) continue;
            uint256 e6 = (net6[k] * fromEvm6) / wanted6;
            uint64 c = uint64((net6[k] - e6) * Units.SPOT_PER_PERP);
            _settleHolder(h, burn[k], feeShares[k]);
            if (queuedOf[h] == 0) _dropQueue(h);
            if (_send(h, e6, c)) core = true;
            emit Paid(h, burn[k] + feeShares[k], feeShares[k], e6, c);
        }
        if (core) lastCorePayAt = uint64(block.timestamp);
    }

    /// What each holder gets for the fraction `num / den` of their queued shares: the shares burned, the
    /// shares that go to the platform as its fee, and the payment in 1e6 units, rounded down. Returns
    /// the sum of the payments.
    function _quote(
        address[] memory holders,
        uint256 poolValue,
        uint256 sharesBefore,
        uint256 num,
        uint256 den,
        uint256[] memory burn,
        uint256[] memory feeShares,
        uint256[] memory net6
    ) internal view returns (uint256 total6) {
        for (uint256 k = 0; k < holders.length; ++k) {
            address h = holders[k];
            if (h == address(0)) {
                (burn[k], feeShares[k], net6[k]) = (0, 0, 0);
                continue;
            }
            uint256 s = (queuedOf[h] * num) / den;
            uint256 gross = (s * poolValue) / sharesBefore;
            uint256 cost = (basis[h] * s) / sharesOf[h];
            uint256 f = 0;
            if (h != platform && gross > cost) {
                uint256 fee = ((gross - cost) * feeBps) / Units.BPS;
                f = (s * fee) / gross;
            }
            burn[k] = s - f;
            feeShares[k] = f;
            net6[k] = ((s - f) * poolValue) / sharesBefore / Units.SPOT_PER_PERP;
            total6 += net6[k];
        }
    }

    function _settleHolder(address h, uint256 burned, uint256 fee) internal {
        uint256 s = burned + fee;
        basis[h] -= (basis[h] * s) / sharesOf[h];
        sharesOf[h] -= s;
        queuedOf[h] -= s;
        queuedShares -= s;
        totalShares -= burned;
        sharesOf[platform] += fee;
    }

    /// Sends a holder the two parts of a payment and adds them to what the pool has paid them; whether
    /// anything went out on HyperCore.
    function _send(address h, uint256 evm6, uint64 core1e8) internal returns (bool onCore) {
        if (evm6 != 0) usdc.safeTransfer(h, evm6);
        uint64 sent;
        if (core1e8 != 0) {
            // A holder with no HyperCore account yet pays for creating it out of this part.
            sent = CoreOps.sendableTo(h, core1e8);
            CoreOps.sendUsdc(h, sent);
            onCore = true;
        }
        // A HyperCore part no larger than what creating the holder's account would cost sends nothing:
        // no payment to note.
        if (evm6 == 0 && sent == 0) return onCore;
        Payments storage p = payments[h];
        p.at = uint64(block.timestamp);
        p.evm += SafeCast.toUint96(evm6);
        p.core += sent;
    }

    function _dropQueue(address h) internal {
        uint256 slot = _queueSlot[h];
        address last = _queue[_queue.length - 1];
        _queue[slot - 1] = last;
        _queueSlot[last] = slot;
        _queue.pop();
        delete _queueSlot[h];
    }

    /// What the queued shares are worth now (1e8), indicative the way `value()` is.
    function _queuedValue() internal view returns (uint256) {
        return (queuedShares * _value()) / totalShares;
    }

    function _paymentsLanded() internal view {
        if (lastCorePayAt != 0 && block.timestamp <= lastCorePayAt + PAYOUT_WAIT) {
            revert PaymentsLanding(lastCorePayAt + PAYOUT_WAIT);
        }
    }

    function _note(Pool p) internal {
        address seat = address(p);
        if (p.stage() != Pool.Stage.Funded) {
            if (fundedSince[seat] != 0) {
                fundedSince[seat] = 0;
                fundedKey[seat] = address(0);
            }
            return;
        }
        address key = p.agentKey();
        if (fundedSince[seat] == 0 || fundedKey[seat] != key) {
            fundedSince[seat] = uint64(block.timestamp);
            fundedKey[seat] = key;
            emit FundedNoted(seat, key, uint64(block.timestamp));
        }
    }

    // ── views ────────────────────────────────────────────────────────────────────────

    function queue() external view returns (address[] memory) {
        return _queue;
    }

    function seats() external view returns (address[] memory) {
        return _seats;
    }

    function closedTickets() external view returns (address[] memory) {
        return _closed;
    }

    function openTicketCount() external view returns (uint256) {
        return _open.length;
    }

    /// @notice Open tickets from index `from`, at most `count`; they are not in any order worth relying on.
    function openTickets(uint256 from, uint256 count) external view returns (address[] memory page) {
        uint256 end = from + count > _open.length ? _open.length : from + count;
        page = new address[](end > from ? end - from : 0);
        for (uint256 i = from; i < end; ++i) {
            page[i - from] = _open[i];
        }
    }

    // ── internals ────────────────────────────────────────────────────────────────────

    function _mint(address to, uint256 amount) internal {
        sharesOf[to] += amount;
        totalShares += amount;
    }

    function _salt(address depositor, uint256 n) internal pure returns (bytes32) {
        return keccak256(abi.encode(depositor, n));
    }

    function _addClosed(address t) internal {
        _closed.push(t);
        _closedSlot[t] = _closed.length;
    }

    function _dropOpen(address t) internal {
        uint256 slot = _openSlot[t];
        address last = _open[_open.length - 1];
        _open[slot - 1] = last;
        _openSlot[last] = slot;
        _open.pop();
        delete _openSlot[t];
    }

    /// Sends what each closed ticket holds into the pool, and forgets the tickets that hold only dust.
    /// A ticket whose previous sweep is still in flight shows the same money again; the second send
    /// then finds nothing left on HyperCore and does nothing.
    function _sweepClosed() internal {
        uint256 i = 0;
        while (i < _closed.length) {
            address t = _closed[i];
            uint64 held = CoreOps.spotUsdc(t);
            if (held < SWEEP_MIN) {
                address last = _closed[_closed.length - 1];
                _closed[i] = last;
                _closedSlot[last] = i + 1;
                _closed.pop();
                delete _closedSlot[t];
                emit TicketEmptied(t);
                continue;
            }
            DepositTicket(t).sweep(held);
            emit TicketSwept(t, held);
            ++i;
        }
    }
}
