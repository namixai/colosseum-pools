// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Rules, Terms, Breach, Units} from "../Types.sol";
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
///         becomes shares at the next settlement point, priced at the pool's value without it. The
///         pool's value is what it would have if every account were closed at the mark now and everyone
///         it owes were paid; `value()` spells it out.
/// @dev Value and shares are in HyperCore spot units, 1e8 = 1 USDC; the first shares are one per unit.
///      Settlement points are open to anyone. `blocker()` is the one place that decides whether a point
///      may run: while a loss is still moving or a payment is in flight, no price is struck.
///      Testnet only; not part of the reviewed core.
contract SharedPool {
    enum Blocker {
        None,
        RuleBroken,
        SeatClosing,
        PositionsOpen,
        PayoutInFlight
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

    PoolFactory public immutable factory;
    IERC20 public immutable usdc;
    address public immutable ticketImpl;
    address public immutable operator;
    /// Holder of the starting shares, which never leave.
    address public immutable platform;
    /// Smallest deposit a settlement point takes (1e8 = 1 USDC).
    uint64 public immutable minDeposit;

    uint256 public totalShares;
    mapping(address holder => uint256) public sharesOf;
    uint256 public seedValue;
    uint256 public seedShares;

    /// Sum of the seats' `capitalNeeded()` (1e8).
    uint256 public planCapital;
    address[] internal _seats;
    mapping(address seat => bool) public isSeat;
    mapping(address seat => SeatTerms) public seatTerms;
    mapping(address seat => uint64) public armedAt;

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
    event SeatAdded(address indexed seat, uint256 capitalNeeded);
    event SeatArmed(address indexed seat, uint64 amount);
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
    error NotSeat(address seat);
    error SeatBusy(address seat);
    error TooSoon(address seat);
    error AlreadyArmed(address seat);
    error NotEnoughFree(uint64 free, uint64 needed);
    error NotQuiet(Blocker reason, address account);
    error NoValue();

    constructor(PoolFactory factory_, address operator_, address platform_, uint64 minDeposit_) {
        // A deposit under SWEEP_MIN would be closed and forgotten as dust in the same point that minted
        // its shares: shares for money the pool never takes in.
        if (minDeposit_ < SWEEP_MIN) revert BadDeposit();
        factory = factory_;
        usdc = factory_.usdc();
        operator = operator_;
        platform = platform_;
        minDeposit = minDeposit_;
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

    /// @notice Publishes a seat: a new pool owned by this contract, with the platform's rules and terms.
    ///         The starting shares must stay worth at least `SEED_BPS` of the whole seat plan.
    function addSeat(Rules calldata rules_, Terms calldata terms_) external onlyOperator started returns (address seat) {
        if (_seats.length >= MAX_SEATS) revert TooMany();
        seat = factory.createPool(rules_, terms_);
        uint256 need = Pool(seat).capitalNeeded();
        uint256 plan = planCapital + need;
        if (seedValue * Units.BPS < plan * SEED_BPS) revert SeedTooSmall(seedValue, plan);
        planCapital = plan;
        _seats.push(seat);
        isSeat[seat] = true;
        seatTerms[seat] = SeatTerms({
            capital: terms_.capital,
            targetBps: terms_.targetBps,
            shareChallengeBps: terms_.traderShareChallengeBps,
            shareFundedBps: terms_.traderShareFundedBps
        });
        emit SeatAdded(seat, need);
    }

    // ── seats ────────────────────────────────────────────────────────────────────────

    /// @notice Tops an idle seat up to what it needs to sell a challenge, from the pool's spot on
    ///         HyperCore. A seat that has no HyperCore account yet costs the pool 1 USDC more, once;
    ///         after that anyone calls `prepareAccount` on it. Anyone may call this.
    function armSeat(address seat) external started {
        if (!isSeat[seat]) revert NotSeat(seat);
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
        armedAt[seat] = uint64(block.timestamp);
        CoreOps.sendUsdc(seat, amount);
        emit SeatArmed(seat, amount);
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
            emit DepositRecognized(tk.depositor, t, amount, minted);
        }
        _sweepClosed();

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

    // ── views ────────────────────────────────────────────────────────────────────────

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
