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
import {MultiPool} from "./MultiPool.sol";

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

    /// The one who binds the pool and may extend it. They hold no power over anyone's money.
    address public immutable creator;
    IERC20 public immutable usdc;
    address public immutable ticketImpl;
    /// Smallest deposit a settlement point takes (1e8 = 1 USDC).
    uint64 public immutable minDeposit;

    MultiPool public pool;

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
    event Seeded(address indexed creator, uint256 shares);
    event TicketOpened(address indexed depositor, address indexed ticket, uint256 index);
    event DepositRecognized(address indexed depositor, address indexed ticket, uint256 amount, uint256 shares);
    event TicketSwept(address indexed ticket, uint64 amount);
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
    error NotEmpty();
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

    constructor(address creator_, IERC20 usdc_, uint64 minDeposit_) {
        // A deposit under SWEEP_MIN would be forgotten as dust in the same point that minted its
        // shares: shares for money the pool never takes in.
        if (minDeposit_ < SWEEP_MIN) revert BadDeposit();
        creator = creator_;
        usdc = usdc_;
        minDeposit = minDeposit_;
        ticketImpl = address(new ShareTicket());
    }

    modifier bound() {
        if (address(pool) == address(0)) revert NotBound();
        _;
    }

    // ── the creator ──────────────────────────────────────────────────────────────────

    /// @notice Makes `pool_` this contract's pool, once. The pool has to be owned here and have no
    ///         exam or account running, and both accounts have to exist on HyperCore, so that no
    ///         transfer between them pays for creating one. Whatever the pool already holds belongs
    ///         to the holders.
    function bind(MultiPool pool_) external {
        if (msg.sender != creator) revert NotCreator();
        if (address(pool) != address(0)) revert AlreadyBound();
        if (pool_.owner() != address(this)) revert NotOwnedHere();
        if (pool_.held() != 0) revert NotEmpty();
        if (!CoreOps.exists(address(this)) || !CoreOps.exists(address(pool_))) revert NotReady();
        pool = pool_;
        emit Bound(address(pool_));
    }

    /// @notice Extends the pool by `term`, in its last `notice` and only then, when every holder's
    ///         decision is in. What the shares asked out are worth now leaves the pool with the
    ///         extension, or the extension does not happen: nobody who asked to leave is held for a
    ///         new term. Whoever said nothing stays.
    function extend(uint32 term) external bound {
        if (msg.sender != creator) revert NotCreator();
        uint64 end = pool.endsAt();
        if (block.timestamp + pool.notice() <= end) revert NotInWindow(end - pool.notice() + 1);
        uint256 out = exitShares == 0 ? 0 : _closeRound();
        (uint64 core, uint256 evm6) = _fromPool(out);
        pool.extend(term, core, evm6);
        emit Extended(term, out);
    }

    // ── leaving ──────────────────────────────────────────────────────────────────────

    /// @notice Asks for `shares` to leave at the end of the term. They stay the caller's and keep
    ///         bearing the pool's result until the round closes, at its price. Up to `notice` before
    ///         the end, and again once the pool has stopped.
    function requestExit(uint256 shares) external bound {
        _decisionsOpen();
        _roll(msg.sender);
        uint256 free = sharesOf[msg.sender];
        if (shares == 0 || shares > free) revert NotFree(free);
        sharesOf[msg.sender] = free - shares;
        exitOf[msg.sender] += shares;
        exitRound[msg.sender] = round;
        exitShares += shares;
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
        emit ExitCancelled(msg.sender, shares);
    }

    /// @notice Once the pool has stopped: closes the open round at today's price and takes what it
    ///         owes out of the pool. Anyone may call it, whenever `blocker()` says nothing is in motion
    ///         and the pool can give the money; until its accounts are closed it cannot.
    function closeRound() external bound {
        if (block.timestamp <= pool.endsAt()) revert NotEnded();
        if (exitShares == 0) revert NothingRequested();
        uint256 out = _closeRound();
        (uint64 core, uint256 evm6) = _fromPool(out);
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
    function openTicket() external bound returns (address ticket) {
        if (block.timestamp > pool.endsAt()) revert Ended();
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
        int256 v = int256(pool.value());
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

    /// Decisions are taken up to `notice` before the end, and again once the pool has stopped.
    function _decisionsOpen() internal view {
        uint64 end = pool.endsAt();
        if (block.timestamp + pool.notice() > end && block.timestamp <= end) revert Decided(end);
    }

    /// Closes the open round at today's price: the shares asked out leave the count and what they are
    /// worth becomes owed. Returns that.
    function _closeRound() internal returns (uint256 out) {
        _quiet();
        uint256 v = _value();
        uint256 s = totalShares;
        out = (exitShares * v) / s;
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
    function _fromPool(uint256 out) internal view returns (uint64 core, uint256 evm6) {
        if (out == 0) return (0, 0);
        uint256 held = uint256(CoreOps.spotUsdc(address(this))) + usdc.balanceOf(address(this)) * Units.SPOT_PER_PERP;
        uint256 before = owed - out;
        uint256 spare = held > before ? held - before : 0;
        if (spare >= out) return (0, 0);
        uint256 need = out - spare;
        evm6 = usdc.balanceOf(address(pool));
        if (evm6 * Units.SPOT_PER_PERP > need) evm6 = need / Units.SPOT_PER_PERP;
        core = SafeCast.toUint64(need - evm6 * Units.SPOT_PER_PERP);
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
        address to = block.timestamp <= pool.endsAt() ? address(pool) : address(this);
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
