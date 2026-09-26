// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Rules, Terms, Cancel, Breach, Units} from "./Types.sol";
import {CoreOps} from "./lib/CoreOps.sol";
import {RuledAccount, IFactoryView} from "./RuledAccount.sol";
import {KeyRegistry} from "./KeyRegistry.sol";

interface IPoolFactory is IFactoryView {
    function usdc() external view returns (IERC20);
    function builder() external view returns (address, uint64);
    function challengeFee() external view returns (uint256);
    function feeRecipient() external view returns (address);
    function createChallenge(address trader) external returns (address);
}

/// @title Pool
/// @notice An investor's capital on its own HyperCore account, sold to one trader at a time:
///         first as a challenge on a slice of it, then, if the trader passes, as a funded
///         account traded with a new key.
/// @dev Capital arrives as a HyperCore spot transfer to this address. There is no HyperEVM
///      deposit: on testnet the USDC bridge credited nothing to a contract bridging to itself
///      (spike question 5, 17 Sep 2026), so such a deposit would take the investor's USDC and
///      leave the pool empty.
contract Pool is RuledAccount {
    using SafeERC20 for IERC20;

    enum Stage {
        Idle,
        Challenge,
        Funded,
        Closing,
        /// The trader met the target and the pass is recorded, but the funded stage has no key
        /// yet. Appended rather than placed where it belongs in the life of a pool: the app
        /// reads this deployment and the one before it, and a number that means two different
        /// things on two factories has to be branched on everywhere it appears.
        PassedAwaitingKey
    }

    address public owner;
    Terms internal _terms;
    bool public accountReady;

    Stage public stage;
    address public challenge;
    address public challengeTrader;
    /// The agent key this pool has already taken for the funded stage, held from the moment the
    /// challenge is sold. Zero once it is in use or given back.
    address public reservedKey;
    /// Price paid for the current challenge, held until it starts (HyperEVM USDC units).
    uint256 public heldPrice;
    /// Prices of started challenges, withdrawable by the owner (HyperEVM USDC units).
    uint256 public earned;

    address public fundedTrader;
    /// When the pass was recorded, so the wait for a key cannot run for ever.
    uint64 public passedAt;
    int64 public fundedStart;
    Breach public fundedEndReason;
    /// Account value when the funded stage was stopped, positions marked at the start of that
    /// block (1e6). For the record only: the share is not computed from it.
    int64 public fundedStopEquity;
    /// Account value at the first settlement step with nothing open (1e6): what closing
    /// actually realized, before any of it moved to spot. The share is computed from this.
    int64 public fundedResult;
    bool public fundedResultTaken;
    /// Funded trader's share of the realized profit (1e8 = 1 USDC), and what was sent: once,
    /// never re-sent.
    uint64 public fundedPayoutOwed;
    uint64 public fundedPayoutSent;
    bool public fundedPayoutDone;
    /// Spot balance when the payout was sent, and when: the pool goes back to idle only once
    /// the payout shows in the balance (or after PAYOUT_WAIT), so a new challenge can't be
    /// sold against money that is already on its way out.
    uint64 public fundedPayoutSpotBefore;
    uint64 public fundedPayoutAt;
    /// The block in which a closing step sent the perp side across to spot. A BLOCK and not a
    /// flag: precompiles answer with the start of the block, so two steps in one block read the
    /// same stale numbers, and a flag would wave the second one through. Zero until it happens.
    uint64 public fundedDrainBlock;

    uint64 public constant PAYOUT_WAIT = 5 minutes;
    /// How long a pool holds its capital for a trader who passed but has no key yet. After it,
    /// anyone may release the pool -- the pass and the challenge share stay with the trader, and
    /// the event says the platform never funded them.
    uint64 public constant AWAIT_KEY_WINDOW = 7 days;

    event AccountReady();
    event ChallengeSold(address indexed challenge, address indexed trader, uint256 price);
    event ChallengeRefunded(address indexed challenge, address indexed trader, uint256 price);
    event TraderFunded(address indexed trader, address indexed key, uint64 capital);
    event ChallengeFeePaid(address indexed trader, address indexed recipient, uint256 fee);
    event FundedStopped(address indexed trader, Breach indexed reason, int64 equity);
    event FundedResult(address indexed trader, int64 realized, uint64 payout);
    event FundedPayoutSent(address indexed trader, uint64 amount);
    event TraderPassed(address indexed trader);
    event FundedStageAbandoned(address indexed trader);
    event FundedClosed(address indexed trader);
    event WithdrawnOnCore(address indexed to, uint64 amount);
    event EarnedWithdrawn(address indexed to, uint256 amount);

    error NotOwner();
    error NotChallenge();
    error BadStage(Stage stage);
    error NotReady();
    error NotEnoughCapital(uint64 spot, uint64 needed);
    error NotAllowed();
    error TooEarly();

    constructor() {
        _disableInitializers();
    }

    function initialize(IFactoryView factory_, address owner_, Rules memory rules_, Terms memory terms_)
        external
        initializer
    {
        __RuledAccount_init(factory_, rules_);
        owner = owner_;
        _terms = terms_;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyChallenge() {
        if (msg.sender != challenge) revert NotChallenge();
        _;
    }

    modifier inStage(Stage s) {
        if (stage != s) revert BadStage(stage);
        _;
    }

    function terms() external view returns (Terms memory) {
        return _terms;
    }

    function builder() external view returns (address, uint64) {
        return IPoolFactory(address(factory)).builder();
    }

    function drawdownBase() public view override returns (int64) {
        return fundedStart;
    }

    // ── investor ─────────────────────────────────────────────────────────────────────

    /// @notice Once the pool exists on HyperCore (the investor's first spot transfer creates it): separate spot and perp balances, so spare
    ///         capital on spot never backs a trader's positions, and approve the platform's
    ///         builder fee. Anyone may call it.
    function prepareAccount() external {
        if (!CoreOps.exists(address(this))) revert NotReady();
        CoreOps.separateBalances(address(this));
        (address builder_, uint64 maxFee) = IPoolFactory(address(factory)).builder();
        CoreOps.approveBuilder(builder_, maxFee);
        accountReady = true;
        emit AccountReady();
    }

    /// @notice Sends spot USDC to the owner on HyperCore. Only while no trader holds the pool.
    function withdrawOnCore(uint64 amount1e8) external onlyOwner inStage(Stage.Idle) {
        CoreOps.sendUsdc(owner, amount1e8);
        emit WithdrawnOnCore(owner, amount1e8);
    }

    function withdrawEarned() external onlyOwner {
        uint256 amount = earned;
        earned = 0;
        IPoolFactory(address(factory)).usdc().safeTransfer(owner, amount);
        emit EarnedWithdrawn(owner, amount);
    }

    // ── challenge ────────────────────────────────────────────────────────────────────

    /// @notice Spot USDC the pool needs to sell a challenge (1e8 = 1 USDC): the challenge
    ///         capital, the funded capital, and the fee for creating the challenge's account,
    ///         which is always new.
    function capitalNeeded() public view returns (uint64) {
        return (_terms.capital + _terms.fundedCapital) * Units.SPOT_PER_PERP + Units.NEW_ACCOUNT_FEE;
    }

    /// @notice Buys a challenge. The pool must be idle and hold enough spot USDC for the
    ///         challenge capital and for funding the trader afterwards.
    function buyChallenge() external inStage(Stage.Idle) returns (address ch) {
        if (!accountReady) revert NotReady();
        // A passed challenge can still be settling after its funded stage has closed.
        if (challenge != address(0)) revert BadStage(stage);
        uint64 needed = capitalNeeded();
        uint64 spot = CoreOps.spotUsdc(address(this));
        if (spot < needed) revert NotEnoughCapital(spot, needed);

        uint256 price = _terms.price;
        heldPrice = price;
        stage = Stage.Challenge;
        challengeTrader = msg.sender;
        IPoolFactory f = IPoolFactory(address(factory));
        IERC20 usdc = f.usdc();
        usdc.safeTransferFrom(msg.sender, address(this), price);
        // The platform's fee: a challenge takes an agent key for good, and a pool's owner
        // could otherwise sell challenges to itself for nothing. Not refunded on abort.
        uint256 fee = f.challengeFee();
        if (fee != 0) {
            address recipient = f.feeRecipient();
            usdc.safeTransferFrom(msg.sender, recipient, fee);
            emit ChallengeFeePaid(msg.sender, recipient, fee);
        }

        ch = IPoolFactory(address(factory)).createChallenge(msg.sender);
        challenge = ch;
        // The key for the funded stage is taken NOW, not when the trader passes. Free keys are
        // public and anyone can spoil one for the price of an account on HyperCore, so a trader
        // who did everything asked could otherwise reach the pass and find the registry empty --
        // losing the funded stage, and their share of it, to a stranger. The challenge's own key
        // is reserved at ChallengeAccount.initialize for exactly this reason; this is the other
        // half of the same promise.
        reservedKey = factory.registry().assign(msg.sender);
        CoreOps.sendUsdc(ch, _terms.capital * Units.SPOT_PER_PERP);
        emit ChallengeSold(ch, msg.sender, price);
    }

    function onChallengeStarted() external onlyChallenge {
        earned += heldPrice;
        heldPrice = 0;
    }

    function onChallengeAborted() external onlyChallenge {
        uint256 price = heldPrice;
        heldPrice = 0;
        IPoolFactory(address(factory)).usdc().safeTransfer(challengeTrader, price);
        emit ChallengeRefunded(challenge, challengeTrader, price);
    }

    /// @notice The trader passed. Recorded here and now, whatever state the key registry is
    ///         in: opening the funded stage is a separate call that anyone may make.
    /// @dev    This split is the whole of audit A-02. While this function took a key, a stranger
    ///         who had drained the free list decided whether a trader who had already met the
    ///         target got their stage at all -- graduate reverted, the deadline passed, and the
    ///         trader was left with `expire`: no funded stage, no share of one, and the
    ///         challenge's profit staying in the pool. Nothing a third party can do reaches this
    ///         function now. The pool holds its capital in the meantime, so the investor cannot
    ///         withdraw from under someone who passed.
    function onChallengePassed(address trader) external onlyChallenge inStage(Stage.Challenge) {
        stage = Stage.PassedAwaitingKey;
        fundedTrader = trader;
        fundedEndReason = Breach.None;
        passedAt = uint64(block.timestamp);
        emit TraderPassed(trader);
    }

    /// @notice Opens the funded stage for the trader who passed, as soon as a live key exists.
    ///         Anyone may call it; the keeper does.
    /// @dev    If there is no live key this reverts and the pool stays where it is, so the call
    ///         can simply be made again later -- which is the point: a refusal here costs a
    ///         retry, where before it cost the trader the stage.
    function openFundedStage() external inStage(Stage.PassedAwaitingKey) {
        address key = reservedKey;
        reservedKey = address(0);
        // The reserved key has been public since the sale -- KeyBound names it -- so a stranger
        // had the whole challenge term to give it an account, and HyperCore then takes such an
        // address as an agent silently and does nothing (spike question 8). A funded stage on a
        // dead key looks open and cannot trade, which is worse than any refusal.
        if (key == address(0) || CoreOps.exists(key)) {
            if (key != address(0)) factory.registry().retire(key);
            key = factory.registry().assign(fundedTrader);
        }
        stage = Stage.Funded;
        _setAgent(key);
        // Sending the challenge capital may have cost an activation fee, so fund what is
        // there, up to the terms.
        uint64 spot1e6 = CoreOps.spotUsdc(address(this)) / Units.SPOT_PER_PERP;
        uint64 funded = spot1e6 < _terms.fundedCapital ? spot1e6 : _terms.fundedCapital;
        int64 start = CoreOps.equity(address(this)) + int64(funded);
        fundedStart = start;
        CoreOps.toPerp(funded);
        _startDay(start);
        emit TraderFunded(fundedTrader, key, funded);
    }

    /// @notice Releases a pool that has been waiting for a key longer than the window. Anyone
    ///         may call it.
    /// @dev    Without this the pool holds its capital for ever if no key is ever published,
    ///         which is a worse hole than the one the wait closes. The trader keeps the pass and
    ///         the challenge share they earned; what the event records is that this pool never
    ///         funded them, which is a mark on the pool and not on the trader.
    function abandonFundedStage() external inStage(Stage.PassedAwaitingKey) {
        if (block.timestamp <= passedAt + AWAIT_KEY_WINDOW) revert TooEarly();
        address spare = reservedKey;
        if (spare != address(0)) {
            reservedKey = address(0);
            factory.registry().retire(spare);
        }
        emit FundedStageAbandoned(fundedTrader);
        fundedTrader = address(0);
        fundedEndReason = Breach.None;
        passedAt = 0;
        stage = Stage.Idle;
    }

    function onChallengeSettled() external onlyChallenge {
        challenge = address(0);
        challengeTrader = address(0);
        if (stage == Stage.Challenge) {
            stage = Stage.Idle;
            // Nobody passed, so the reserved key was never used. Give it up: a pool may hold
            // only one key, and the next challenge needs its own, bound to whoever buys that.
            address spare = reservedKey;
            if (spare != address(0)) {
                reservedKey = address(0);
                factory.registry().retire(spare);
            }
        }
    }

    // ── funded trader ────────────────────────────────────────────────────────────────

    function isStopped() public view override returns (bool) {
        return stage == Stage.Closing;
    }

    /// @notice Takes the day's snapshot, in the first minutes of the UTC day.
    function checkpoint() external inStage(Stage.Funded) {
        _checkpoint();
    }

    /// @notice Stops the funded trader if a rule is broken right now. Anyone may call it.
    function breach(Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        external
        inStage(Stage.Funded)
    {
        Breach reason = violation(extraAssets);
        if (reason == Breach.None) revert NoBreach();
        _endFunded(reason, cancels, extraAssets, salt);
    }

    /// @notice The investor or the trader ends the funded stage without a breach.
    function stopFunded(Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        external
        inStage(Stage.Funded)
    {
        if (msg.sender != owner && msg.sender != fundedTrader) revert NotAllowed();
        _endFunded(Breach.None, cancels, extraAssets, salt);
    }

    function _endFunded(Breach reason, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        internal
    {
        stage = Stage.Closing;
        fundedEndReason = reason;
        int64 eq = CoreOps.equity(address(this));
        fundedStopEquity = eq;
        _cutAgent(salt);
        _cancelAll(cancels);
        _closeAll(extraAssets);
        emit FundedStopped(fundedTrader, reason, eq);
    }

    /// @notice One step of closing the funded stage; call until the pool is idle. Capital
    ///         stays in the pool; only the trader's share leaves, once. Anyone may call it.
    function settleFunded(Cancel[] calldata cancels, uint32[] calldata extraAssets)
        external
        inStage(Stage.Closing)
    {
        (uint256 open, uint64 free, uint64 spot) = _drainStep(cancels, extraAssets);
        if (open != 0) return;

        if (!fundedResultTaken) {
            // The first step with nothing open. Precompiles show the start of this block, so
            // the account value is what closing realized and none of it has moved to spot yet
            // (this step's transfer lands after the block). Marked equity at the stop is not
            // used: a close can fill worse than the mark, and the investor would pay the gap.
            fundedResultTaken = true;
            int64 result = CoreOps.equity(address(this));
            fundedResult = result;
            fundedPayoutOwed = fundedEndReason == Breach.None && result > fundedStart
                ? SafeCast.toUint64(
                    (uint256(int256(result) - int256(fundedStart)) * _terms.traderShareFundedBps * Units.SPOT_PER_PERP)
                        / Units.BPS
                )
                : 0;
            emit FundedResult(fundedTrader, result, fundedPayoutOwed);
        }
        if (free != 0 && fundedDrainBlock == 0) {
            // The closing proceeds need a block to reach spot, so the first step to see them
            // sends them and waits. After that, a perp balance is somebody else's transfer:
            // this step has already sent it across, and it does not get to hold the pool in
            // Closing. Otherwise anyone could keep the investor's capital locked -- it only
            // leaves in Idle -- for one unit a block. Audit A-01.
            fundedDrainBlock = uint64(block.number);
            return;
        }
        // Still inside the block that sent it: every read below is the start of this block, so
        // the money is not in `spot` yet and the trader's share would be paid from a balance
        // that predates it -- once, because the payout marks itself done. Audit A-11, which is
        // what a bare flag cost: a flag says "it happened", a block number says "when".
        if (block.number <= fundedDrainBlock) return;

        if (fundedPayoutOwed != 0 && !fundedPayoutDone) {
            if (spot == 0) return;
            // A trader with no HyperCore account yet pays for creating it out of the share.
            uint64 pay = CoreOps.sendableTo(fundedTrader, spot < fundedPayoutOwed ? spot : fundedPayoutOwed);
            fundedPayoutDone = true;
            fundedPayoutSent = pay;
            fundedPayoutSpotBefore = spot;
            fundedPayoutAt = uint64(block.timestamp);
            CoreOps.sendUsdc(fundedTrader, pay);
            emit FundedPayoutSent(fundedTrader, pay);
            if (pay != 0) return;
        }

        if (
            fundedPayoutSent != 0 && spot + fundedPayoutSent > fundedPayoutSpotBefore
                && block.timestamp <= fundedPayoutAt + PAYOUT_WAIT
        ) {
            return; // the payout hasn't landed yet
        }

        // Finish only when nothing is HELD on the perp side: what is left there is all
        // withdrawable and already on its way to spot. A resting order's margin is not
        // withdrawable, so this still waits for one -- Closing is the only stage that can
        // drain, and finishing with margin held would strand it. The position check repeats
        // what _drainStep established at the top of this call; it is a cheap read and it
        // makes this condition true on its own rather than by code order.
        int64 eq = CoreOps.equity(address(this));
        if (eq <= int64(CoreOps.withdrawable(address(this))) && CoreOps.margin(address(this)).ntlPos == 0) {
            emit FundedClosed(fundedTrader);
            fundedTrader = address(0);
            fundedStart = 0;
            fundedStopEquity = 0;
            fundedResult = 0;
            fundedResultTaken = false;
            fundedPayoutOwed = 0;
            fundedPayoutDone = false;
            fundedPayoutSent = 0;
            fundedPayoutSpotBefore = 0;
            fundedPayoutAt = 0;
            fundedDrainBlock = 0;
            stage = Stage.Idle;
        }
    }
}
