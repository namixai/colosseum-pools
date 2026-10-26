// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {Breach, Units} from "../Types.sol";
import {CoreOps} from "../lib/CoreOps.sol";
import {Pool} from "../Pool.sol";
import {ShareTicket} from "./ShareTicket.sol";
import {MultiPool, Plan} from "./MultiPool.sol";
import {PoolFactory} from "../PoolFactory.sol";
import {CodeStore} from "../lib/CodeStore.sol";
import {TermsBook} from "./TermsBook.sol";

/// @title PoolShares
/// @notice Many investors' money in one pool of many traders. This contract is the `MultiPool`'s
///         owner, and a share is a claim on a part of the pool's value. The money is USDC and nothing
///         else, and it is locked for the pool's term the way a token lock holds tokens.
///
///         A deposit arrives on HyperCore at an address made for it (`ShareTicket`, like the tickets
///         of `SharedPool`) and becomes shares at the next settlement point, priced at the value
///         without it; from the ticket the money goes straight into the pool. An investor who wants
///         out says so with
///         `requestExit` no later than `notice` before the end of the term, fourteen days in the
///         product, and may take it back until then. From there on what was said holds. In those last
///         days the creator may extend the pool: the extension takes out of the pool exactly what the
///         leaving shares are worth at that moment, or it does not happen. Whoever said nothing stays
///         for the new term. A pool that is not extended stops, and then everyone may leave.
/// @dev Value and shares are in HyperCore spot units, 1e8 = 1 USDC; the first shares are one per unit.
///      The share accounting, the tickets and the way a payment is split and recorded are
///      `SharedPool`'s; what is new is that leaving goes by the term and not by a queue. This
///      contract's own HyperCore balance holds what it owes and little else, and nothing leaves it
///      but payments to holders.
contract PoolShares {
    using SafeERC20 for IERC20;

    enum Blocker {
        None,
        RuleBroken,
        SeatClosing,
        PaymentInFlight,
        PoolPaying
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

    /// What the contract has paid a holder so far, each part where it was paid, and when it last paid
    /// them. `evm` is USDC on HyperEVM (1e6 = 1 USDC); `core` is the spot USDC sent to the holder's
    /// HyperCore account (1e8 = 1 USDC), after the 1 USDC a holder who had no account there paid out of
    /// their first HyperCore part for creating it.
    struct Payments {
        uint64 at;
        uint96 evm;
        uint96 core;
    }

    uint256 public constant MAX_PER_POINT = 8;
    /// Less than this left on a closed ticket is dust: not swept, not counted, forgotten.
    uint64 public constant SWEEP_MIN = 1e8;
    /// A payment this contract sends on HyperCore is given this long to land: until then no price is
    /// struck and the same money is not spent again.
    uint64 public constant LAND_WAIT = 1 minutes;
    /// How long a raise runs from the bind.
    uint32 public constant RAISE_TIME = 30 days;
    /// The platform's charge on a deposit handed back when a raise falls short: its 0.5 % on a
    /// withdrawal (Alex, 4 October 2026, item 28), on every deposit (Alex, 15 October 2026).
    uint16 public constant RAISE_FEE_BPS = 50;

    /// The one who binds the pool and may extend it. They hold no power over anyone's money.
    address public immutable creator;
    IERC20 public immutable usdc;
    address public immutable ticketImpl;
    /// Smallest deposit a settlement point takes (1e8 = 1 USDC).
    uint64 public immutable minDeposit;

    MultiPool public pool;
    /// What the raise has to collect before the pool starts (1e8); zero starts it at once.
    uint256 public immutable target;
    /// The hash of the pool's terms, which every deposit names.
    bytes32 public terms;
    uint64 public raiseEnds;
    bool public launched;
    /// A raise that fell short is handed back at one price for everyone, taken once.
    uint256 public refundValue;
    uint256 public refundShares;
    /// The platform's charge on what was handed back, kept here until `payFee`.
    uint256 public feeOwed;

    /// Every share, those asked out in the open round too: they bear the pool's result until the
    /// round closes.
    uint256 public totalShares;
    /// A holder's shares that stay.
    mapping(address holder => uint256) public sharesOf;

    /// Rounds closed so far. A round closes when the creator extends, or once the pool has stopped.
    uint256 public round;
    /// Shares asked out in the open round.
    uint256 public exitShares;
    mapping(address holder => uint256) public exitOf;
    mapping(address holder => uint256) public exitRound;
    /// The price each round closed at: its value over its shares.
    mapping(uint256 r => uint256) public roundValue;
    mapping(uint256 r => uint256) public roundShares;
    /// What is owed for shares whose round has closed and has not been paid (1e8); `owedOf` is
    /// brought up to date by the holder's next call.
    mapping(address holder => uint256) public owedOf;
    uint256 public owed;
    mapping(address holder => Payments) public payments;

    /// What this contract paid holders on HyperCore in the last `LAND_WAIT`, and when it last paid.
    uint64 public coreFlight;
    uint64 public lastPaidAt;

    mapping(address ticket => Ticket) public tickets;
    mapping(address depositor => uint256) public ticketCount;
    address[] internal _open;
    mapping(address ticket => uint256) internal _openSlot; // index + 1
    /// Recognized tickets that may still hold money; counted in the value until they are empty.
    address[] internal _closed;
    mapping(address ticket => uint256) internal _closedSlot; // index + 1

    uint256 public points;
    uint64 public lastPoint;

    event Bound(address indexed pool);
    event TermsAccepted(address indexed who, bytes32 terms);
    event Launched(uint256 value);
    event Refunded(address indexed holder, uint256 shares, uint256 amount, uint256 fee);
    event RoundStopped(uint256 out);
    event Seeded(address indexed creator, uint256 shares);
    event TicketOpened(address indexed depositor, address indexed ticket, uint256 index);
    event DepositRecognized(address indexed depositor, address indexed ticket, uint256 amount, uint256 shares);
    event TicketSwept(address indexed ticket, uint64 amount);
    event TicketReturned(address indexed ticket, address indexed depositor, uint64 amount, uint64 fee);
    event Reclaimed(uint256 amount, uint256 fee);
    event TicketEmptied(address indexed ticket);
    event PointSettled(uint256 indexed point, uint256 value, uint256 sharesBefore, uint256 sharesAfter);
    event ExitRequested(address indexed holder, uint256 shares);
    event ExitCancelled(address indexed holder, uint256 shares);
    event RoundClosed(uint256 indexed round, uint256 value, uint256 shares, uint256 out);
    event Extended(uint32 term, uint256 out);
    event Paid(address indexed holder, uint256 amount, uint256 evmAmount, uint64 coreAmount);

    error NotCreator();
    error NotBound();
    error AlreadyBound();
    error NotOwnedHere();
    error NotReady();
    error BadDeposit();
    error TooMany();
    error NotQuiet(Blocker reason, address account);
    error NoValue();
    error Decided(uint64 endsAt);
    error NotFree(uint256 free);
    error NothingRequested();
    error NothingOwed();
    error NothingToSend();
    error NotInWindow(uint64 opensAt);
    error NotEnded();
    error Ended();
    error NotEnoughFree(uint64 spot, uint64 inFlight, uint64 cost);
    error WrongTerms();
    error NotLaunched();
    error AlreadyLaunched();
    error RaiseOver();
    error RaiseShort(uint256 value, uint256 target);
    error NotRefunding();
    error CannotPay();
    error Unpublished();

    constructor(address creator_, IERC20 usdc_, uint64 minDeposit_, uint256 target_) {
        // A deposit under SWEEP_MIN would be forgotten as dust in the same point that minted its
        // shares: shares for money the pool never takes in.
        if (minDeposit_ < SWEEP_MIN) revert BadDeposit();
        creator = creator_;
        usdc = usdc_;
        minDeposit = minDeposit_;
        target = target_;
        ticketImpl = address(new ShareTicket());
    }

    modifier bound() {
        if (address(pool) == address(0)) revert NotBound();
        _;
    }

    // ── the creator ──────────────────────────────────────────────────────────────────

    /// @notice Makes `pool_` this contract's pool, once, and starts the raise. The pool has to be
    ///         owned here, and both accounts have to exist on HyperCore, so that no transfer between
    ///         them pays for creating one. A pool owned here cannot have started: only its owner
    ///         starts it. The creator accepts the pool's terms by naming their hash.
    function bind(MultiPool pool_, bytes32 terms_) external {
        if (msg.sender != creator) revert NotCreator();
        if (address(pool) != address(0)) revert AlreadyBound();
        if (pool_.owner() != address(this)) revert NotOwnedHere();
        if (!CoreOps.exists(address(this)) || !CoreOps.exists(address(pool_))) revert NotReady();
        Plan memory plan = abi.decode(CodeStore.read(pool_.planData()), (Plan));
        if (plan.terms != terms_) revert WrongTerms();
        // Terms are accepted as written: a hash whose text nobody can read is not accepted. A
        // ticket names the hash bound here, so its text is there too (the book changes nothing).
        if (TermsBook(plan.termsBook).textAt(terms_) == address(0)) revert Unpublished();
        pool = pool_;
        terms = terms_;
        raiseEnds = uint64(block.timestamp) + RAISE_TIME;
        emit Bound(address(pool_));
        emit TermsAccepted(msg.sender, terms_);
    }

    /// @notice Starts the pool once the raise has collected its target, within its thirty days:
    ///         every deposit is sent on into the pool and the pool's term begins. Anyone may call it.
    ///         What the pool already held is the creator's, as shares at the price of the raise.
    function launch() external bound {
        if (launched) revert AlreadyLaunched();
        if (block.timestamp > raiseEnds) revert RaiseOver();
        _quiet();
        if (_closed.length != 0) revert NotReady();
        uint256 v = _value();
        if (v < target) revert RaiseShort(v, target);
        uint256 already = pool.value();
        if (already != 0) {
            uint256 seed = totalShares == 0 ? already : (already * totalShares) / v;
            sharesOf[creator] += seed;
            totalShares += seed;
            emit Seeded(creator, seed);
        }
        launched = true;
        uint64 spot = CoreOps.spotUsdc(address(this));
        if (spot != 0) CoreOps.sendUsdc(address(pool), spot);
        pool.begin(terms);
        emit Launched(v + already);
    }

    /// @notice Once a raise has fallen short: turns `holder`'s shares into what they are owed, at one
    ///         price for every holder, less the platform's 0.5 %; `pay` then sends it. Anyone may call it.
    function refund(address holder) external bound {
        if (launched || block.timestamp <= raiseEnds) revert NotRefunding();
        if (refundShares == 0) {
            _quiet();
            refundValue = _value();
            refundShares = totalShares;
        }
        uint256 n = sharesOf[holder];
        if (n == 0) revert NotFree(0);
        uint256 amount = (n * refundValue) / refundShares;
        uint256 fee = PoolFactory(pool.factory()).feeRecipient() == address(0) ? 0 : (amount * RAISE_FEE_BPS) / 10_000;
        sharesOf[holder] = 0;
        totalShares -= n;
        owedOf[holder] += amount - fee;
        feeOwed += fee;
        owed += amount;
        emit Refunded(holder, n, amount - fee, fee);
    }

    /// @notice Once a raise has fallen short: ends the pool, which never began, and takes back what it
    ///         holds. That was the creator's before the raise, and is owed to them less the
    ///         platform's 0.5 % on a withdrawal, as every deposit handed back (audit M-1; the terms,
    ///         D7 and D10). Anyone may call it, and again for money that reaches the pool later;
    ///         `pay` then sends it.
    function reclaim() external bound {
        if (launched || block.timestamp <= raiseEnds) revert NotRefunding();
        if (pool.endsAt() == 0) pool.setStop(true);
        uint256 v = pool.value();
        if (v == 0) revert NothingToSend();
        uint256 evm6 = usdc.balanceOf(address(pool));
        if (evm6 * Units.SPOT_PER_PERP > v) evm6 = v / Units.SPOT_PER_PERP;
        uint256 core = v - evm6 * Units.SPOT_PER_PERP;
        uint256 fee = PoolFactory(pool.factory()).feeRecipient() == address(0) ? 0 : (v * RAISE_FEE_BPS) / 10_000;
        owedOf[creator] += v - fee;
        feeOwed += fee;
        owed += v;
        emit Reclaimed(v - fee, fee);
        if (evm6 != 0) pool.withdrawEvm(evm6);
        if (core != 0) pool.withdraw(SafeCast.toUint64(core));
    }

    /// @notice Sends the platform what refunds charged, on HyperCore.
    function payFee() external bound {
        uint256 fee = feeOwed;
        if (fee == 0) revert NothingOwed();
        address to = PoolFactory(pool.factory()).feeRecipient();
        uint64 sent = CoreOps.sendableTo(to, SafeCast.toUint64(fee));
        if (sent == 0) revert NothingToSend();
        _spendCore(SafeCast.toUint64(fee));
        feeOwed = 0;
        owed -= fee;
        CoreOps.sendUsdc(to, sent);
    }

    /// @notice The term renews by itself; in its last `notice`, when every holder's decision is in,
    ///         anyone may close the round of those who asked to leave. What their shares are worth
    ///         now leaves the pool, and the pool runs on for those who stay. If the pool cannot give
    ///         that money, it stops at the end of its term instead, and they are paid then: nobody
    ///         who asked to leave is held for a renewal. If nobody called in those days, anyone may
    ///         call at any time after the end, at the price of that moment (audit M-2).
    function renew() external bound {
        _renew(0);
    }

    /// @notice The creator stops the pool at the end of its term: nothing more is sold, and once it
    ///         has ended every holder may leave and be paid (`closeRound`).
    function stop() external bound {
        if (msg.sender != creator) revert NotCreator();
        pool.setStop(true);
    }

    /// @notice The creator extends the term by `term` as well, in the same last days; the round
    ///         closes the same way, and if the pool cannot pay it, nothing happens.
    function extend(uint32 term) external bound {
        if (msg.sender != creator) revert NotCreator();
        _renew(term);
    }

    function _renew(uint32 term) internal {
        uint64 end = pool.endsAt();
        uint64 n = pool.notice();
        if (!(term == 0 && _late()) && (end == 0 || block.timestamp + n <= end)) {
            revert NotInWindow(end == 0 ? 0 : end - n + 1);
        }
        if (term == 0 && exitShares == 0) revert NothingRequested();
        uint256 out;
        if (exitShares != 0) {
            _quiet();
            uint256 v = _value();
            uint256 s = totalShares;
            out = (exitShares * v) / s;
            (uint64 core, uint256 evm6) = _fromPool(out, owed);
            try pool.extend(term, core, evm6) {
                _close(v, s, out);
            } catch {
                if (term != 0) revert CannotPay();
                pool.setStop(true);
                emit RoundStopped(out);
                return;
            }
        } else {
            pool.extend(term, 0, 0);
        }
        _setReserve();
        emit Extended(term, out);
    }

    // ── leaving ──────────────────────────────────────────────────────────────────────

    /// @notice Asks for `shares` to leave at the end of the term. They stay the caller's and keep
    ///         bearing the pool's result until the round closes, at its price. Up to `notice` before
    ///         the end, and again once the pool has stopped.
    function requestExit(uint256 shares) external bound {
        if (!launched) revert NotLaunched();
        _decisionsOpen();
        _roll(msg.sender);
        uint256 free = sharesOf[msg.sender];
        if (shares == 0 || shares > free) revert NotFree(free);
        sharesOf[msg.sender] = free - shares;
        exitOf[msg.sender] += shares;
        exitRound[msg.sender] = round;
        exitShares += shares;
        _setReserve();
        emit ExitRequested(msg.sender, shares);
    }

    /// @notice Takes back a request to leave, for `shares` of it, while decisions are still open.
    function cancelExit(uint256 shares) external bound {
        _decisionsOpen();
        _roll(msg.sender);
        uint256 asked = exitOf[msg.sender];
        if (shares == 0 || shares > asked) revert NotFree(asked);
        exitOf[msg.sender] = asked - shares;
        exitShares -= shares;
        sharesOf[msg.sender] += shares;
        _setReserve();
        emit ExitCancelled(msg.sender, shares);
    }

    /// @notice Once the pool has stopped: closes the open round at today's price and takes what it
    ///         owes out of the pool. Anyone may call it, whenever `blocker()` says nothing is in motion
    ///         and the pool can give the money; until its accounts are closed it cannot.
    function closeRound() external bound {
        uint64 end = pool.endsAt();
        if (end == 0 || block.timestamp <= end) revert NotEnded();
        if (exitShares == 0) revert NothingRequested();
        _quiet();
        uint256 v = _value();
        uint256 s = totalShares;
        uint256 out = (exitShares * v) / s;
        (uint64 core, uint256 evm6) = _fromPool(out, owed);
        _close(v, s, out);
        _setReserve();
        if (evm6 != 0) pool.withdrawEvm(evm6);
        if (core != 0) pool.withdraw(core);
    }

    /// @notice Pays `holder` what their closed rounds owe them: USDC on HyperEVM first, the rest on
    ///         HyperCore once it has landed here. Anyone may call it; the money goes to the holder.
    function pay(address holder) external {
        _roll(holder);
        uint256 amount = owedOf[holder];
        if (amount == 0) revert NothingOwed();
        uint256 evmHeld = usdc.balanceOf(address(this));
        uint256 evm6 = amount / Units.SPOT_PER_PERP;
        if (evm6 > evmHeld) evm6 = evmHeld;
        uint64 core = SafeCast.toUint64(amount - evm6 * Units.SPOT_PER_PERP);
        // A holder with no HyperCore account pays for creating it out of this part. A part that
        // would only do that sends nothing, so it stays owed until the account exists or USDC on
        // HyperEVM covers it (audit S-2).
        uint64 sent = core == 0 ? 0 : CoreOps.sendableTo(holder, core);
        uint256 paid = evm6 * Units.SPOT_PER_PERP + (sent == 0 ? 0 : core);
        if (paid == 0) revert NothingToSend();
        if (sent != 0) _spendCore(core);
        owedOf[holder] = amount - paid;
        owed -= paid;
        _send(holder, evm6, sent);
        emit Paid(holder, paid, evm6, sent);
    }

    // ── deposits ─────────────────────────────────────────────────────────────────────

    /// @notice Opens an address for one deposit by the caller. Send USDC to it on HyperCore; the next
    ///         settlement point that names it turns what it holds into shares. The first transfer to
    ///         the address creates its HyperCore account, which HyperCore charges the sender 1 USDC for.
    function openTicket(bytes32 terms_) external bound returns (address ticket) {
        uint64 end = pool.endsAt();
        if (end != 0 && block.timestamp > end) revert Ended();
        if (!launched && block.timestamp > raiseEnds) revert RaiseOver();
        if (terms_ != terms) revert WrongTerms();
        emit TermsAccepted(msg.sender, terms_);
        uint256 n = ticketCount[msg.sender]++;
        ticket = Clones.cloneDeterministic(ticketImpl, _salt(msg.sender, n));
        ShareTicket(ticket).init(msg.sender);
        tickets[ticket] = Ticket({depositor: msg.sender, state: TicketState.Open});
        _open.push(ticket);
        _openSlot[ticket] = _open.length;
        emit TicketOpened(msg.sender, ticket, n);
    }

    /// @notice Where `depositor`'s ticket number `n` is or will be.
    function ticketAddress(address depositor, uint256 n) external view returns (address) {
        return Clones.predictDeterministicAddress(ticketImpl, _salt(depositor, n));
    }

    /// @notice A settlement point: turns the named open tickets that hold at least `minDeposit` into
    ///         shares, all at one price, the value before them; closes those tickets; and sweeps what
    ///         the closed tickets hold into the pool, or here once the pool has stopped. Anyone may
    ///         call it, whenever `blocker()` says nothing is in motion.
    function settle(address[] calldata recognize) external bound {
        if (recognize.length > MAX_PER_POINT) revert TooMany();
        _quiet();
        uint256 v = _value();
        uint256 s = totalShares;
        // With no shares, what the pool already holds is nobody's yet: the creator's money put in
        // before the bind, or what is left once every share has gone. It becomes the creator's
        // shares, one per unit, before anything is counted, so a first deposit buys in at one per
        // unit too and does not take what was there before it (audit S-1).
        if (s == 0 && v != 0) {
            sharesOf[creator] += v;
            totalShares = v;
            emit Seeded(creator, v);
        }
        if (s != 0 && v == 0) revert NoValue();

        for (uint256 i = 0; i < recognize.length; ++i) {
            address t = recognize[i];
            Ticket storage tk = tickets[t];
            if (tk.state == TicketState.Closed) {
                // Money sent to a ticket after it was counted and emptied belongs to the holders too;
                // naming the ticket takes it in. Dust drops off again in the sweep below.
                if (_closedSlot[t] == 0) _addClosed(t);
                continue;
            }
            // A ticket that was never opened here is passed over: the caller's list is only a list.
            if (tk.state != TicketState.Open) continue;
            uint64 amount = CoreOps.spotUsdc(t);
            if (refundShares != 0) {
                // A raise that fell short has taken its price, and nothing more is counted: what
                // reaches an open ticket now goes back to whoever opened it, each time (audit H-1).
                if (amount >= SWEEP_MIN) _handBack(t, tk.depositor, amount);
                continue;
            }
            if (amount < minDeposit) continue;
            uint256 minted = s == 0 ? amount : (uint256(amount) * s) / v;
            tk.state = TicketState.Closed;
            _dropOpen(t);
            _addClosed(t);
            sharesOf[tk.depositor] += minted;
            totalShares += minted;
            emit DepositRecognized(tk.depositor, t, amount, minted);
        }
        _sweepClosed();

        lastPoint = uint64(block.timestamp);
        emit PointSettled(++points, v, s, totalShares);
    }

    // ── value ────────────────────────────────────────────────────────────────────────

    /// @notice Whether a price may be struck now, and if not, the reason and the account holding it
    ///         up: a funded account that has broken a rule and is not closed yet, or one closing; a
    ///         payment to a holder on HyperCore that may not have landed; money the pool sent here in
    ///         the last minute, which the pool still counts against itself.
    function blocker() public view returns (Blocker, address) {
        if (lastPaidAt != 0 && block.timestamp <= lastPaidAt + LAND_WAIT) {
            return (Blocker.PaymentInFlight, address(this));
        }
        uint64 poolOut = pool.lastOutAt();
        if (poolOut != 0 && block.timestamp <= poolOut + pool.ARM_WAIT()) return (Blocker.PoolPaying, address(pool));
        uint32[] memory none = new uint32[](0);
        address[] memory seats = pool.seats();
        // A seat that is free, waiting or not opened yet is idle, and passes both questions.
        for (uint256 i = 0; i < seats.length; ++i) {
            Pool p = Pool(seats[i]);
            Pool.Stage stage = p.stage();
            if (stage == Pool.Stage.Closing) return (Blocker.SeatClosing, seats[i]);
            if (stage == Pool.Stage.Funded && p.violation(none) != Breach.None) {
                return (Blocker.RuleBroken, seats[i]);
            }
        }
        return (Blocker.None, address(0));
    }

    /// @notice The holders' value (1e8 = 1 USDC): the pool's value, what this contract holds on
    ///         HyperCore and HyperEVM, and the closed tickets not yet swept, less what closed rounds
    ///         owe. While `blocker()` names something, this is indicative only.
    function value() external view bound returns (uint256) {
        return _value();
    }

    function _value() internal view returns (uint256) {
        // Before the launch the pool holds nothing of the holders': what it held is the creator's.
        int256 v = launched ? int256(pool.value()) : int256(0);
        v += int256(uint256(CoreOps.spotUsdc(address(this))));
        v += int256(usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP);
        for (uint256 i = 0; i < _closed.length; ++i) {
            v += int256(uint256(CoreOps.spotUsdc(_closed[i])));
        }
        v -= int256(owed);
        return v > 0 ? uint256(v) : 0;
    }

    // ── views ────────────────────────────────────────────────────────────────────────

    /// @notice What `holder` holds, as their next call would see it: shares that stay, shares asked
    ///         out in an open round, and what closed rounds owe them (1e8).
    function positionOf(address holder) external view returns (uint256 staying, uint256 leaving, uint256 due) {
        staying = sharesOf[holder];
        leaving = exitOf[holder];
        due = owedOf[holder];
        uint256 r = exitRound[holder];
        if (leaving != 0 && r < round) {
            due += (leaving * roundValue[r]) / roundShares[r];
            leaving = 0;
        }
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

    function _quiet() internal view {
        (Blocker reason, address account) = blocker();
        if (reason != Blocker.None) revert NotQuiet(reason, account);
    }

    /// Decisions are taken up to `notice` before the end, and again once the pool has stopped. A
    /// round whose end has passed unclosed stands as it was at that end until somebody closes it.
    function _decisionsOpen() internal view {
        uint64 end = pool.endsAt();
        if (block.timestamp + pool.notice() > end && block.timestamp <= end) revert Decided(end);
        if (_late()) revert Decided(pool.reserveFor());
    }

    /// Whether the open round's term has ended and renewed by itself with the round still open:
    /// shares asked out for an end that has passed (audit M-2). The pool keeps that end.
    function _late() internal view returns (bool) {
        uint64 asked = pool.reserveFor();
        return exitShares != 0 && block.timestamp > asked && asked < pool.endsAt();
    }

    /// Closes the open round at today's price: the shares asked out leave the count and what they are
    /// worth becomes owed. Returns that.
    /// Closes the open round at the price `v` over `s`: the shares asked out leave the count and
    /// what they are worth, `out`, becomes owed.
    function _close(uint256 v, uint256 s, uint256 out) internal {
        uint256 r = round;
        roundValue[r] = v;
        roundShares[r] = s;
        round = r + 1;
        totalShares = s - exitShares;
        exitShares = 0;
        owed += out;
        emit RoundClosed(r, v, s, out);
    }

    /// What has to come out of the pool for `out` just owed: what this contract holds beyond what it
    /// owed before covers part, and the pool's USDC on HyperEVM goes before its HyperCore balance.
    /// A round closes only when no payment is landing, so the balance read here is what is here.
    function _fromPool(uint256 out, uint256 before) internal view returns (uint64 core, uint256 evm6) {
        if (out == 0) return (0, 0);
        uint256 held = uint256(CoreOps.spotUsdc(address(this))) + usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP;
        uint256 spare = held > before ? held - before : 0;
        if (spare >= out) return (0, 0);
        uint256 need = out - spare;
        evm6 = usdc.balanceOf(address(pool));
        if (evm6 * Units.SPOT_PER_PERP > need) evm6 = need / Units.SPOT_PER_PERP;
        core = SafeCast.toUint64(need - evm6 * Units.SPOT_PER_PERP);
    }

    /// Tells the pool what the shares asked out are worth now, so that it sells nothing against them.
    function _setReserve() internal {
        uint256 s = totalShares;
        pool.setReserve(s == 0 ? 0 : (exitShares * _value()) / s);
    }

    function _recentCore() internal view returns (uint64) {
        return block.timestamp > lastPaidAt + LAND_WAIT ? 0 : coreFlight;
    }

    /// Counts `amount` as leaving this contract's HyperCore balance, or refuses if the balance as this
    /// block shows it does not cover it on top of what was paid in the last `LAND_WAIT`. HyperCore
    /// drops a transfer it cannot make without a word, so nothing is sent on a balance that is not
    /// there. Payments are the only thing that leaves this balance, so nobody can hold them up by
    /// making it move.
    function _spendCore(uint64 amount) internal {
        uint64 spot = CoreOps.spotUsdc(address(this));
        uint64 recent = _recentCore();
        if (uint256(spot) < uint256(recent) + amount) revert NotEnoughFree(spot, recent, amount);
        coreFlight = recent + amount;
        lastPaidAt = uint64(block.timestamp);
    }

    /// Brings a holder's request up to date: shares asked out in a round that has closed become what
    /// that round owes them.
    function _roll(address h) internal {
        uint256 leaving = exitOf[h];
        if (leaving == 0) return;
        uint256 r = exitRound[h];
        if (r >= round) return;
        owedOf[h] += (leaving * roundValue[r]) / roundShares[r];
        exitOf[h] = 0;
    }

    /// Sends a holder the two parts of a payment and adds them to what has been paid them.
    function _send(address h, uint256 evm6, uint64 sent) internal {
        if (evm6 != 0) usdc.safeTransfer(h, evm6);
        if (sent != 0) CoreOps.sendUsdc(h, sent);
        Payments storage p = payments[h];
        p.at = uint64(block.timestamp);
        p.evm += SafeCast.toUint96(evm6);
        p.core += sent;
    }

    /// Sends what an open ticket holds back to its depositor, less the platform's 0.5 % as on every
    /// deposit handed back (the terms, D7 and D10). Both parts leave from the ticket itself and
    /// nothing is owed or counted here, so a send still landing cannot pay twice: a second send
    /// finds the ticket empty. A part too small to open the platform's account stays the depositor's.
    function _handBack(address t, address depositor, uint64 amount) internal {
        address to = PoolFactory(pool.factory()).feeRecipient();
        uint64 cut = to == address(0) ? 0 : uint64((uint256(amount) * RAISE_FEE_BPS) / 10_000);
        uint64 fee = cut == 0 ? 0 : CoreOps.sendableTo(to, cut);
        if (fee == 0) cut = 0;
        uint64 back = CoreOps.sendableTo(depositor, amount - cut);
        if (fee != 0) ShareTicket(t).sweep(to, fee);
        if (back != 0) ShareTicket(t).sweep(depositor, back);
        emit TicketReturned(t, depositor, back, fee);
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

    /// Sends what each closed ticket holds into the pool, or here once the pool has stopped, and
    /// forgets the tickets that hold only dust. A ticket whose previous sweep is still in flight shows
    /// the same money again; the second send then finds nothing left on HyperCore and does nothing.
    function _sweepClosed() internal {
        uint64 end = pool.endsAt();
        address to = launched && (end == 0 || block.timestamp <= end) ? address(pool) : address(this);
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
            ShareTicket(t).sweep(to, held);
            emit TicketSwept(t, held);
            ++i;
        }
    }
}
