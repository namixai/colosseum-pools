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
    function isMultiPool(address pool) external view returns (bool);
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
    /// Set once by the owner: this pool sells no challenge of its own, and its owner funds the
    /// traders it has examined elsewhere (`openFundedFor`).
    bool public ownerRun;
    /// On a pool its owner runs, whether the funded trader may end the stage themselves.
    bool public traderMayStop;

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
    /// The block a withdrawal left in. A sale in the same block would check the balance through a
    /// precompile, which answers with the START of the block and cannot see the money going out.
    uint64 public withdrewAtBlock;
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
    /// When this step first had to wait because the share was short and money was crossing.
    uint64 public fundedPayoutShortAt;
    /// The block in which a closing step sent the perp side across to spot. A BLOCK and not a
    /// flag: precompiles answer with the start of the block, so two steps in one block read the
    /// same stale numbers, and a flag would wave the second one through. Zero until it happens.
    uint64 public fundedDrainBlock;

    uint64 public constant PAYOUT_WAIT = 5 minutes;
    /// How long a pool holds its capital for a trader who passed but has no key yet. After it,
    /// anyone may release the pool -- the pass and the challenge share stay with the trader, and
    /// the event says the platform never funded them.
    uint64 public constant AWAIT_KEY_WINDOW = 7 days;
    /// When the funded stage running now opened.
    uint64 public fundedAt;

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
    event RunByOwner(bool traderMayStop);

    error NotOwner();
    error NotChallenge();
    error BadStage(Stage stage);
    error NotReady();
    error NotEnoughCapital(uint64 spot, uint64 needed);
    error NotAllowed();
    error TooEarly();
    error KeyAvailable();
    error WithdrawnThisBlock();
    error SalesClosed();
    error NotOwnerRun();

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
        withdrewAtBlock = uint64(block.number);
        CoreOps.sendUsdc(owner, amount1e8);
        emit WithdrawnOnCore(owner, amount1e8);
    }

    /// @notice Sends spot USDC on HyperCore to `to` on the owner's word, as `withdrawOnCore` does
    ///         to the owner. Only while no trader holds the pool.
    function sendOnCore(address to, uint64 amount1e8) external onlyOwner inStage(Stage.Idle) {
        withdrewAtBlock = uint64(block.number);
        CoreOps.sendUsdc(to, amount1e8);
        emit WithdrawnOnCore(to, amount1e8);
    }

    function withdrawEarned() external onlyOwner {
        uint256 amount = earned;
        earned = 0;
        IPoolFactory(address(factory)).usdc().safeTransfer(owner, amount);
        emit EarnedWithdrawn(owner, amount);
    }

    // ── a pool its owner runs ───────────────────────────────────────────

    /// @notice Hands the choice of trader to the owner, for good: from now on this pool sells no
    ///         challenge, and only `openFundedFor` puts a trader on its capital. For an owner that
    ///         examines traders some other way. `traderMayStop_` says whether a trader funded
    ///         this way may end the stage themselves, as a trader who passed a challenge may.
    function runByOwner(bool traderMayStop_) external onlyOwner inStage(Stage.Idle) {
        // A challenge sold before this could still pass, and would fund its trader the old way.
        if (ownerRun || challenge != address(0)) revert NotAllowed();
        ownerRun = true;
        traderMayStop = traderMayStop_;
        emit RunByOwner(traderMayStop_);
    }

    /// @notice Funds `trader_` on this pool's capital, at once and in full. Only the owner of a
    ///         pool it runs may call it, and only an owner the factory knows as a MultiPool: each
    ///         stage takes a key from the shared registry and retires it, so a pool anyone could
    ///         run this way would empty the registry for gas (audit K-3).
    /// @dev    Unlike `openFundedStage`, this never funds less than the terms say: there is no
    ///         challenge whose activation fee could have eaten into the capital, so a short
    ///         balance is the owner's to top up first. The balance read is the start of the
    ///         block, so a withdrawal earlier in the same block is refused the way a sale is
    ///         (audit A-08).
    function openFundedFor(address trader_) external onlyOwner inStage(Stage.Idle) {
        if (!ownerRun || !IPoolFactory(address(factory)).isMultiPool(owner)) revert NotOwnerRun();
        if (!accountReady) revert NotReady();
        if (withdrewAtBlock >= block.number) revert WithdrawnThisBlock();
        uint64 funded = _terms.fundedCapital;
        uint64 needed = funded * Units.SPOT_PER_PERP;
        uint64 spot = CoreOps.spotUsdc(address(this));
        if (spot < needed) revert NotEnoughCapital(spot, needed);

        address key = factory.registry().assign(trader_);
        fundedTrader = trader_;
        fundedEndReason = Breach.None;
        if (dex != 0) {
            // On a builder dex the stage opens once the capital shows there (audit window 2,
            // В4-2): the pool waits as one whose trader passed, and `openFundedStage` opens it.
            passedAt = uint64(block.timestamp);
            stage = Stage.PassedAwaitingKey;
        }
        _fund(key, funded);
    }

    /// @notice Renews the funded stage's agent from the last fourteen days of its term on, a pool's own
    ///         stage or a MultiPool's seat: the old key is retired for good and the trader gets a fresh
    ///         one, approved for a new term, as `rekey` does. Anyone may call it; the keeper does
    ///         fourteen days before the term HyperCore shows ends (the CTO, 19 October 2026).
    function renewAgent() external inStage(Stage.Funded) {
        _renewAgent(fundedTrader);
    }

    /// @notice Gives a funded stage a fresh key when its own has gained a HyperCore account, which
    ///         HyperCore does not take as an agent: a transfer that lands in the block the key is
    ///         bound spoils it. The trader keeps the stage they earned and the capital does not sit
    ///         idle (deep audit, F10). Only the stage's trader, within a day of the key's approval,
    ///         twice at most for each key the stage opens or is renewed with: a key HyperCore already
    ///         took keeps working after it gains an account, so on anyone's word a stranger's 1 USDC
    ///         would replace a working key, and the registry's free keys could be spent one by one
    ///         (deep audit, AUD-2). The fresh key of a renewal can be spoiled the same way, so the
    ///         day runs from the latest approval, not from the stage's opening (audit window 2,
    ///         A5-1). A key that has no account stays.
    function rekey() external inStage(Stage.Funded) {
        if (msg.sender != fundedTrader) revert NotAllowed();
        _rekey(fundedTrader);
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
        if (ownerRun) revert SalesClosed();
        if (!accountReady) revert NotReady();
        // Audit A-08. The capital check below reads a precompile, which answers with the start of
        // the block, so a withdrawal earlier in the SAME block is invisible to it: the sale would
        // be allowed on money that had already left, the transfer to the fresh challenge would
        // quietly not happen, and the buyer would be out the platform's fee and an hour's wait for
        // a refund. Same start-of-block blindness as A-11 and A-04, on the way in this time.
        // SharedPool.releaseSeat goes through withdrawOnCore, so it is covered by the same line.
        if (block.number <= withdrewAtBlock) revert WithdrawnThisBlock();
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
    ///         On a builder dex a first call sends the capital there and a later one opens the
    ///         stage, once the dex shows it (audit window 2, В4-2).
    function openFundedStage() external inStage(Stage.PassedAwaitingKey) {
        // A pool its owner runs waits here only for its owner's stage on a builder dex, and opens it
        // by its owner's call: a MultiPool holds the seat as not yet opened until the pool is
        // Funded, and a stage opened past it would leave it giving the price back for a stage the
        // trader had (self-audit v3, round 1, T3).
        if (ownerRun && msg.sender != owner) revert NotAllowed();
        bool sent = sentToDexAt != 0;
        if (sent && !_landed(fundedStart)) return;
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
        if (sent) return _open(key, sentToDex);
        // Sending the challenge capital may have cost an activation fee, so fund what is
        // there, up to the terms.
        uint64 spot1e6 = CoreOps.spotUsdc(address(this)) / Units.SPOT_PER_PERP;
        uint64 funded = spot1e6 < _terms.fundedCapital ? spot1e6 : _terms.fundedCapital;
        _fund(key, funded);
    }

    /// The capital of a funded stage, from spot to the account's dex: on the main dex the stage
    /// opens with it, on a builder dex it opens on a later block, and the key waits for it.
    function _fund(address key, uint64 funded) internal {
        fundedStart = accountEquity() + int64(funded);
        if (dex != 0) {
            reservedKey = key;
            _sendToDex(funded);
            return;
        }
        _open(key, funded);
        CoreOps.toPerp(funded, dex);
    }

    function _open(address key, uint64 funded) internal {
        stage = Stage.Funded;
        fundedAt = uint64(block.timestamp);
        rekeys = 0;
        _setAgent(key);
        _startDay(fundedStart);
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
        // On a builder dex the stage may wait for its capital and not for a key: the key is held
        // live the whole time, and a dex its owner halted takes no transfer. Past the window, a
        // dex that does not show the capital, read in a later second than the last send, releases
        // the pool; one that shows it opens the stage instead, so the key decides as below (Grok
        // on cp#167, self-audit v3, round 1; a challenge's abort reads the dex the same way).
        if (sentToDexAt == 0 || block.timestamp <= sentToDexAt || accountEquity() >= fundedStart) {
            // The window is not enough on its own. A pool may only be released when there is no
            // key to be had RIGHT NOW -- otherwise a week of nobody calling openFundedStage would
            // drop a trader who passed while a usable key sat in the registry, which is the
            // outcome the wait exists to prevent. Audit's condition on this function, and it was
            // right.
            if (reservedKey != address(0) && !CoreOps.exists(reservedKey)) revert KeyAvailable();
            // freeCount() counts entries, and a spoiled key is still an entry until somebody
            // sweeps it. Refusing on a non-zero count is deliberate: it does not decide from here
            // whether those keys are alive, it makes someone say so on chain first. Anyone may
            // call KeyRegistry.purgeSpoiled, which stops at the first key that is still good -- so
            // a zero count after a sweep means there really is nothing, and a live key keeps the
            // count up and this refusal standing.
            if (factory.registry().freeCount() != 0) revert KeyAvailable();
        }
        address spare = reservedKey;
        if (spare != address(0)) {
            reservedKey = address(0);
            factory.registry().retire(spare);
        }
        // Capital already sent to a builder dex for the stage comes back to spot, for the owner.
        if (sentToDexAt != 0) {
            sentToDexAt = 0;
            _toSpotAll();
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
        if (msg.sender != owner && ownerRun && !traderMayStop) revert NotAllowed();
        _endFunded(Breach.None, cancels, extraAssets, salt);
    }

    function _endFunded(Breach reason, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)
        internal
    {
        // A precompile answers with the start of the block, so on the main dex the stage's opening
        // block reads the pool without the stage's capital on the perp side: a full drawdown.
        // Nothing ends the stage in that block; nothing can trade in it, the agent is approved after
        // it (self-audit v3, round 1).
        if (block.timestamp <= fundedAt) revert TooEarly();
        stage = Stage.Closing;
        fundedEndReason = reason;
        int64 eq = accountEquity();
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
            int64 result = accountEquity();
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
            // Audit A-04, the same on this side as on a passed challenge: the share is measured
            // against `spot`, and a resting order keeps its margin outside `withdrawable`, so
            // `free == 0` does not mean the perp side has let go. A pool that funded a trader out
            // of everything it had holds little on spot, which is exactly when this bites.
            if (!_nothingHeldOnPerp()) {
                fundedPayoutShortAt = 0;
                return;
            }
            // And margin let go in THIS block reaches spot only after it, while `spot` here is
            // the start of the block -- the same blindness as A-11, one line further on. So
            // unless spot already covers the share, wait until nothing is on its way across.
            // Somebody else's dust delays this only while the share is short, and every unit of
            // it lands in spot and brings the share closer: paying to delay a payout is paying
            // into it.
            // Audit A-12, which my own A-04 fix opened. I reasoned that dust could only delay
            // this while the share was short, because every unit of it lands in spot and brings
            // the share closer -- true when the shortfall is a crumb, false when the account
            // genuinely lost most of itself after the pass. Then the gap never closes, a unit a
            // step holds the settlement for as long as somebody keeps paying gas, and nothing
            // else stops it: RETURN_WAIT is upstream of here and PAYOUT_WAIT never starts,
            // because no payout was made. So this wait gets a clock of its own. It resets while
            // margin is held, for a release that comes in pieces -- more than 32 resting orders
            // take several passes -- and a stranger cannot hold margin on somebody else's
            // account, so the reset is not theirs to use.
            if (spot < fundedPayoutOwed && free != 0) {
                if (fundedPayoutShortAt == 0) fundedPayoutShortAt = uint64(block.timestamp);
                if (block.timestamp <= fundedPayoutShortAt + PAYOUT_WAIT) return;
            }
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
        if (_nothingHeldOnPerp()) {
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
            fundedPayoutShortAt = 0;
            fundedDrainBlock = 0;
            startSurplus = 0;
            stage = Stage.Idle;
        }
    }
}
