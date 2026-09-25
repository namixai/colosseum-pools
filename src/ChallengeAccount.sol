// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {Rules, Terms, Cancel, Breach, Units} from "./Types.sol";
import {CoreOps} from "./lib/CoreOps.sol";
import {RuledAccount, IFactoryView} from "./RuledAccount.sol";

interface IPoolHooks {
    function onChallengeStarted() external;
    function onChallengeAborted() external;
    function onChallengePassed(address trader) external;
    function onChallengeSettled() external;
    function builder() external view returns (address, uint64);
}

/// @title ChallengeAccount
/// @notice One purchased challenge: its own HyperCore account, its own agent key, the pool's
///         rules as they were at purchase.
contract ChallengeAccount is RuledAccount {
    enum Status {
        None,
        Created,
        Active,
        Breached,
        Expired,
        Forfeited,
        Passed,
        Aborted,
        Settled
    }

    /// How long the capital has to arrive and the challenge to start before anyone may abort.
    uint64 public constant START_WINDOW = 1 hours;

    IPoolHooks public pool;
    address public trader;
    Terms internal _terms;

    Status public status;
    Breach public breachReason;
    uint64 public createdAt;
    uint64 public startedAt;
    uint64 public deadline;

    /// Trader's share of the profit, owed once the challenge has passed (1e8 = 1 USDC).
    uint64 public payoutOwed;
    /// What was actually sent to the trader. Sent once, never re-sent, so a slow HyperCore
    /// can't make it go out twice.
    uint64 public payoutSent;
    bool public payoutDone;
    /// Spot balance when the payout was sent, and when. The rest goes to the pool only after
    /// the payout shows up in the balance (or after PAYOUT_WAIT), so the pool's transfer can
    /// never be executed ahead of the trader's.
    uint64 public payoutSpotBefore;
    /// What the last return to the pool sent, so the next step can tell our own send landing
    /// from money that arrived afterwards.
    uint64 public returnSpotBefore;
    /// When this account first had anything to hand back. Set ONCE and never moved again: a
    /// window that a stranger can restart is not a window, which is what the old check was.
    uint64 public returnAt;
    uint64 public payoutAt;

    uint64 public constant PAYOUT_WAIT = 5 minutes;
    /// How long the account's own money may be in flight before nothing here holds the
    /// settlement open any more. Anything that turns up after it is not lost: `sweep()`.
    uint64 public constant RETURN_WAIT = 5 minutes;

    event Started(address indexed trader, address indexed key, uint64 capital, uint64 deadline);
    event Stopped(Status indexed status, Breach indexed reason, int64 equity);
    event Passed(address indexed trader, int64 equity, uint64 payout);
    event PayoutSent(address indexed trader, uint64 amount);
    event ReturnedToPool(uint64 amount);
    event Settled();

    error BadStatus(Status status);
    error NotTrader();
    error NotStartedOnCore();
    error CapitalArrived();
    error TooEarly();
    error TooLate();
    error NotFlat();
    error TargetNotMet(int64 equity, int256 target);
    error RuleBroken(Breach reason);
    error KeySpoiled();

    constructor() {
        _disableInitializers();
    }

    function initialize(IFactoryView factory_, IPoolHooks pool_, address trader_, Rules memory rules_, Terms memory terms_)
        external
        initializer
    {
        __RuledAccount_init(factory_, rules_);
        pool = pool_;
        trader = trader_;
        _terms = terms_;
        status = Status.Created;
        createdAt = uint64(block.timestamp);
        // The key is reserved now, so a trader who paid can always start.
        agentKey = factory_.registry().assign(trader_);
    }

    function terms() external view returns (Terms memory) {
        return _terms;
    }

    function drawdownBase() public view override returns (int64) {
        return int64(_terms.capital);
    }

    function isStopped() public view override returns (bool) {
        Status s = status;
        return s == Status.Breached || s == Status.Expired || s == Status.Forfeited || s == Status.Passed
            || s == Status.Aborted;
    }

    modifier inStatus(Status s) {
        if (status != s) revert BadStatus(status);
        _;
    }

    // ── start ────────────────────────────────────────────────────────────────────────

    /// @notice Whether the capital has reached this account on HyperCore.
    function capitalArrived() public view returns (bool) {
        return CoreOps.exists(address(this))
            && CoreOps.spotUsdc(address(this)) >= _terms.capital * Units.SPOT_PER_PERP;
    }

    /// @notice Whether the reserved key gained a HyperCore account before the start. HyperCore
    ///         doesn't take such an address as an agent, so the challenge can't start, and
    ///         anyone may abort it at once.
    function keySpoiled() public view returns (bool) {
        return status == Status.Created && CoreOps.exists(agentKey);
    }

    /// @notice Starts the challenge once the capital has reached this account on HyperCore.
    ///         Anyone may call it.
    function activate() external inStatus(Status.Created) {
        if (!capitalArrived()) revert NotStartedOnCore();
        if (CoreOps.exists(agentKey)) revert KeySpoiled();

        CoreOps.separateBalances(address(this));
        CoreOps.toPerp(_terms.capital);
        (address builder_, uint64 maxFee) = pool.builder();
        CoreOps.approveBuilder(builder_, maxFee);
        _setAgent(agentKey);

        startedAt = uint64(block.timestamp);
        deadline = startedAt + _terms.duration;
        _startDay(int64(_terms.capital));
        status = Status.Active;
        pool.onChallengeStarted();
        emit Started(trader, agentKey, _terms.capital, deadline);
    }

    /// @notice If the capital never arrived, anyone may call this after the start window; if
    ///         the reserved key is spoiled, at any time. The trader's payment is refunded (the
    ///         platform fee is not) and the reserved key retired. The capital, whenever it
    ///         arrives, goes back to the pool through `settle`.
    function abort() external inStatus(Status.Created) {
        if (!keySpoiled()) {
            if (block.timestamp <= createdAt + START_WINDOW) revert TooEarly();
            if (capitalArrived()) revert CapitalArrived();
        }
        status = Status.Aborted;
        address key = agentKey;
        agentKey = address(0);
        factory.registry().retire(key);
        pool.onChallengeAborted();
        emit Stopped(Status.Aborted, Breach.None, 0);
    }

    // ── while it runs ────────────────────────────────────────────────────────────────

    /// @notice Takes the day's snapshot, in the first minutes of the UTC day. The operator's
    ///         keeper calls it at midnight; anyone may.
    function checkpoint() external inStatus(Status.Active) {
        _checkpoint();
    }

    /// @notice Stops the challenge if a rule is broken right now. Anyone may call it.
    /// @param cancels open orders to cancel (read them from the info API)
    /// @param extraAssets assets outside the pool's list that the account may hold
    /// @param salt any value; it only picks which keyless address replaces the agent
    function breach(Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        external
        inStatus(Status.Active)
    {
        Breach reason = violation(extraAssets);
        if (reason == Breach.None) revert NoBreach();
        breachReason = reason;
        _end(Status.Breached, reason, cancels, extraAssets, salt);
    }

    /// @notice Ends a challenge whose time ran out. Anyone may call it.
    function expire(Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        external
        inStatus(Status.Active)
    {
        if (block.timestamp <= deadline) revert TooEarly();
        _end(Status.Expired, Breach.None, cancels, extraAssets, salt);
    }

    /// @notice The trader walks away.
    function forfeit(Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        external
        inStatus(Status.Active)
    {
        if (msg.sender != trader) revert NotTrader();
        _end(Status.Forfeited, Breach.None, cancels, extraAssets, salt);
    }

    /// @notice Passes the challenge: no rule broken, account flat, target met, in time.
    ///         Anyone may call it. The pool then funds the trader with a new key.
    function graduate(bytes32 salt) external inStatus(Status.Active) {
        if (block.timestamp > deadline) revert TooLate();
        Breach reason = violation(new uint32[](0));
        if (reason != Breach.None) revert RuleBroken(reason);
        if (CoreOps.margin(address(this)).ntlPos != 0) revert NotFlat();

        int64 eq = CoreOps.equity(address(this));
        int256 capital = int256(uint256(_terms.capital));
        int256 target = capital + (capital * int256(uint256(_terms.targetBps))) / int256(uint256(Units.BPS));
        if (int256(eq) < target) revert TargetNotMet(eq, target);

        uint256 profit = uint256(int256(eq) - capital);
        payoutOwed = SafeCast.toUint64((profit * _terms.traderShareChallengeBps * Units.SPOT_PER_PERP) / Units.BPS);

        status = Status.Passed;
        _cutAgent(salt);
        emit Passed(trader, eq, payoutOwed);
        pool.onChallengePassed(trader);
    }

    function _end(Status s, Breach reason, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        internal
    {
        status = s;
        _cutAgent(salt);
        _cancelAll(cancels);
        _closeAll(extraAssets);
        emit Stopped(s, reason, CoreOps.equity(address(this)));
    }

    // ── after it ends ────────────────────────────────────────────────────────────────

    /// @notice One settlement step; call until `status` is Settled. Anyone may call it.
    /// @param cancels orders still resting on the account (a resting order holds margin)
    /// @param extraAssets assets outside the pool's list that may still hold a position
    function settle(Cancel[] calldata cancels, uint32[] calldata extraAssets) external {
        if (!isStopped()) revert BadStatus(status);
        (uint256 open, uint64 free, uint64 spot) = _drainStep(cancels, extraAssets);
        if (open != 0) return; // a real position has to close first

        // One clock for the whole hand-back, started the first time there is anything to give
        // and never moved afterwards. That is the fix: the old check asked whether this step's
        // balance was smaller than the last one we sent, so a stranger sending the SAME unit
        // every step was never "smaller", and could hold the account open for as long as the
        // gas was worth it to them. A window they cannot restart bounds that.
        if (returnAt == 0 && (free != 0 || spot != 0)) returnAt = uint64(block.timestamp);
        bool waited = returnAt != 0 && block.timestamp > returnAt + RETURN_WAIT;

        // The perp side needs a moment to reach spot, and the share below is paid out of spot.
        // Inside the window that wait is the account's own money; past it, a perp balance is
        // somebody else's transfer, already sent across by _drainStep, and it gets no veto.
        if (free != 0 && !waited) return;

        if (payoutOwed != 0 && !payoutDone) {
            if (spot == 0) return;
            // A trader with no HyperCore account yet pays for creating it out of the share.
            uint64 pay = CoreOps.sendableTo(trader, spot < payoutOwed ? spot : payoutOwed);
            payoutDone = true;
            payoutSent = pay;
            payoutSpotBefore = spot;
            payoutAt = uint64(block.timestamp);
            CoreOps.sendUsdc(trader, pay);
            emit PayoutSent(trader, pay);
            if (pay != 0) return;
        }

        if (payoutDone && spot + payoutSent > payoutSpotBefore && block.timestamp <= payoutAt + PAYOUT_WAIT) {
            return; // the payout hasn't landed yet
        }

        if (spot != 0) {
            // Our own first return has landed when the balance came DOWN from what it was --
            // and `returnSpotBefore` is frozen at that first send, so a later donation is
            // measured against the capital, not against itself. When even that cannot tell
            // (the account had nothing of its own to send, so the first "return" was the
            // stranger's unit), the window decides instead.
            bool landed = returnSpotBefore != 0 && (spot < returnSpotBefore || waited);
            CoreOps.sendUsdc(address(pool), spot);
            emit ReturnedToPool(spot);
            if (!landed) {
                // Frozen at the FIRST send, so a later donation is measured against the
                // capital rather than against itself, and a dust attack is shrugged off in a
                // step instead of waiting out the window. No test pins this: the only case
                // where freezing and re-setting differ is a settlement already held up by a
                // resting order's margin, and this harness cannot make an order rest.
                if (returnSpotBefore == 0) returnSpotBefore = spot;
                return;
            }
        }

        // Finish when nothing is HELD here: no position, and everything left is withdrawable.
        // A resting order's margin is not withdrawable, so this still waits for one -- ending
        // with margin held would leave it on a settled account. What is merely in flight, or
        // arrives afterwards, is not stranded either: anyone may `sweep()` it to the pool.
        if (CoreOps.margin(address(this)).ntlPos == 0
            && CoreOps.equity(address(this)) <= int64(CoreOps.withdrawable(address(this)))) {
            status = Status.Settled;
            emit Settled();
            pool.onChallengeSettled();
        }
    }

    /// @notice Pushes whatever is left on a settled account back to the pool. Anyone may call
    ///         it, as often as they like.
    /// @dev    This is what lets `settle` finish on time. Without it the step would have to
    ///         wait until every last unit had landed before ending, and a stranger who keeps
    ///         sending units decides when that is. With it, ending early strands nothing: a
    ///         late fill, a send of ours that did not land, a donation that arrived afterwards
    ///         -- all of it comes here and goes home. It changes no state and reads no rule,
    ///         so it cannot reopen a settlement or alter what anybody was paid.
    function sweep() external {
        if (status != Status.Settled) revert BadStatus(status);
        uint64 free = CoreOps.withdrawable(address(this));
        if (free != 0) {
            CoreOps.toSpot(free);
            emit MovedToSpot(free);
            return; // it lands next block; call again then
        }
        uint64 spot = CoreOps.spotUsdc(address(this));
        if (spot != 0) {
            CoreOps.sendUsdc(address(pool), spot);
            emit ReturnedToPool(spot);
        }
    }
}
