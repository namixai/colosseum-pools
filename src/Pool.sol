// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeCast} from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {CoreWriterLib} from "@hyper-evm-lib/src/CoreWriterLib.sol";
import {Rules, Terms, Cancel, Breach, Units} from "./Types.sol";
import {CoreOps} from "./lib/CoreOps.sol";
import {RuledAccount, IFactoryView} from "./RuledAccount.sol";
import {KeyRegistry} from "./KeyRegistry.sol";

interface IPoolFactory is IFactoryView {
    function usdc() external view returns (IERC20);
    function builder() external view returns (address, uint64);
    function createChallenge(address trader) external returns (address);
}

/// @title Pool
/// @notice An investor's capital on its own HyperCore account, sold to one trader at a time:
///         first as a challenge on a slice of it, then, if the trader passes, as a funded
///         account traded with a new key.
contract Pool is RuledAccount {
    using SafeERC20 for IERC20;

    enum Stage {
        Idle,
        Challenge,
        Funded,
        Closing
    }

    address public owner;
    Terms internal _terms;
    bool public accountReady;

    Stage public stage;
    address public challenge;
    address public challengeTrader;
    /// Price paid for the current challenge, held until it starts (HyperEVM USDC units).
    uint256 public heldPrice;
    /// Prices of started challenges, withdrawable by the owner (HyperEVM USDC units).
    uint256 public earned;

    address public fundedTrader;
    int64 public fundedStart;
    Breach public fundedEndReason;
    /// Funded trader's share of the profit at the stop (1e8 = 1 USDC), and what was sent:
    /// once, never re-sent.
    uint64 public fundedPayoutOwed;
    uint64 public fundedPayoutSent;

    event AccountReady();
    event Deposited(address indexed from, uint256 amount);
    event ChallengeSold(address indexed challenge, address indexed trader, uint256 price);
    event ChallengeRefunded(address indexed challenge, address indexed trader, uint256 price);
    event TraderFunded(address indexed trader, address indexed key, uint64 capital);
    event FundedStopped(address indexed trader, Breach indexed reason, int64 equity, uint64 payout);
    event FundedPayoutSent(address indexed trader, uint64 amount);
    event FundedClosed(address indexed trader);
    event WithdrawnOnCore(address indexed to, uint64 amount);
    event EarnedWithdrawn(address indexed to, uint256 amount);

    error NotOwner();
    error NotChallenge();
    error BadStage(Stage stage);
    error NotReady();
    error NotEnoughCapital(uint64 spot, uint64 needed);
    error NotAllowed();

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

    /// @notice Pulls HyperEVM USDC and moves it to this pool's HyperCore spot balance.
    ///         Capital can also be sent to the pool address directly on HyperCore.
    function deposit(uint256 amount) external {
        IERC20 usdc = IPoolFactory(address(factory)).usdc();
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        CoreWriterLib.bridgeToCore(Units.USDC_TOKEN, amount);
        emit Deposited(msg.sender, amount);
    }

    /// @notice Once the pool exists on HyperCore: separate spot and perp balances, so spare
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

    /// @notice Buys a challenge. The pool must be idle and hold enough spot USDC for the
    ///         challenge capital and for funding the trader afterwards.
    function buyChallenge() external inStage(Stage.Idle) returns (address ch) {
        if (!accountReady) revert NotReady();
        // A passed challenge can still be settling after its funded stage has closed.
        if (challenge != address(0)) revert BadStage(stage);
        uint64 needed = (_terms.capital + _terms.fundedCapital) * Units.SPOT_PER_PERP;
        uint64 spot = CoreOps.spotUsdc(address(this));
        if (spot < needed) revert NotEnoughCapital(spot, needed);

        uint256 price = _terms.price;
        heldPrice = price;
        stage = Stage.Challenge;
        challengeTrader = msg.sender;
        IPoolFactory(address(factory)).usdc().safeTransferFrom(msg.sender, address(this), price);

        ch = IPoolFactory(address(factory)).createChallenge(msg.sender);
        challenge = ch;
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

    /// @notice The trader passed: bind a new key to this account for them and fund it.
    function onChallengePassed(address trader) external onlyChallenge inStage(Stage.Challenge) {
        stage = Stage.Funded;
        fundedTrader = trader;
        fundedEndReason = Breach.None;

        address key = factory.registry().assign(trader);
        _setAgent(key);
        // Sending the challenge capital may have cost an activation fee, so fund what is
        // there, up to the terms.
        uint64 spot1e6 = CoreOps.spotUsdc(address(this)) / Units.SPOT_PER_PERP;
        uint64 funded = spot1e6 < _terms.fundedCapital ? spot1e6 : _terms.fundedCapital;
        int64 start = CoreOps.equity(address(this)) + int64(funded);
        fundedStart = start;
        CoreOps.toPerp(funded);
        _startDay(start);
        emit TraderFunded(trader, key, funded);
    }

    function onChallengeSettled() external onlyChallenge {
        challenge = address(0);
        challengeTrader = address(0);
        if (stage == Stage.Challenge) stage = Stage.Idle;
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
        if (reason == Breach.None && eq > fundedStart) {
            uint256 profit = uint256(int256(eq) - int256(fundedStart));
            fundedPayoutOwed = SafeCast.toUint64((profit * _terms.traderShareBps * Units.SPOT_PER_PERP) / Units.BPS);
        }
        _cutAgent(salt);
        _cancelAll(cancels);
        _closeAll(extraAssets);
        emit FundedStopped(fundedTrader, reason, eq, fundedPayoutOwed);
    }

    /// @notice One step of closing the funded stage; call until the pool is idle. Capital
    ///         stays in the pool; only the trader's share leaves, once. Anyone may call it.
    function settleFunded(Cancel[] calldata cancels, uint32[] calldata extraAssets)
        external
        inStage(Stage.Closing)
    {
        (uint256 open, uint64 free, uint64 spot) = _drainStep(cancels, extraAssets);
        if (open != 0 || free != 0) return;

        if (fundedPayoutOwed != 0 && fundedPayoutSent == 0) {
            if (spot == 0) return;
            uint64 pay = spot < fundedPayoutOwed ? spot : fundedPayoutOwed;
            fundedPayoutSent = pay;
            CoreOps.sendUsdc(fundedTrader, pay);
            emit FundedPayoutSent(fundedTrader, pay);
            return;
        }

        if (CoreOps.equity(address(this)) <= 0) {
            emit FundedClosed(fundedTrader);
            fundedTrader = address(0);
            fundedStart = 0;
            fundedPayoutOwed = 0;
            fundedPayoutSent = 0;
            stage = Stage.Idle;
        }
    }
}
