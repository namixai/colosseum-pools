// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {Rules, Breach, Units} from "../Types.sol";
import {CoreOps} from "../lib/CoreOps.sol";

/// @notice What an exam asks of its trader, next to the `Rules`. Copied in at creation.
struct ExamTerms {
    /// The balance every step starts with, in perp USD units (1e6 = 1 USDC). A number in this
    /// contract; nobody's money.
    uint64 capital;
    /// Profit target of each step over `capital` (bps), in order. One entry is an exam in one step.
    uint16[] targetsBps;
    /// How long one step may run, in seconds; 0 for no deadline.
    uint32 stepDuration;
    /// How long a step may go without a fill, in seconds, from its start or its latest fill; 0
    /// for no such limit.
    uint32 maxIdle;
    /// UTC days with at least one fill that a step needs before it can be passed; 0 for none.
    uint16 minTradingDays;
    /// What a fill costs, in millionths of its notional (950 is 0.095%). Stands for what the
    /// same fill would cost on the exchange.
    uint32 costPpm;
    /// Blocks between an order and the first block that may fill it; at least 1.
    uint16 fillDelay;
    /// For how many blocks, counting that one, the order can be filled; at least 1.
    uint16 fillWindow;
}

/// @title PaperExam
/// @notice An exam traded on no exchange and on nobody's money. The trader's orders are
///         transactions, the position is a number here, and every price is the mark the chain
///         itself shows. Anyone can recompute the result from the chain, and nobody writes it.
/// @dev The rules are RuledAccount's, with the same arithmetic: static drawdown from the starting
///      balance, daily loss from a snapshot taken just after UTC midnight, leverage as notional
///      over equity, and a list of assets. They are judged against the marks of the moment someone
///      asks, so a dip that nobody caught is not a breach, as on a real account.
///
///      A mark read here is the start of the block. A trader with a faster feed knows where the
///      next one will be, so an order is never filled in its own block: it waits `fillDelay`
///      blocks and takes the mark of the block that fills it. Anyone may fill it, and it lapses
///      after `fillWindow` blocks. While nobody else fills, the trader still chooses whether to;
///      `costPpm` is what makes that choice worth less than it costs.
///
///      What this does not model: the depth of a book (a fill takes the mark at any size),
///      funding, and resting orders.
contract PaperExam is Initializable {
    enum Status {
        None,
        Active,
        Passed,
        Breached,
        Expired
    }

    /// An order waiting for its block. `sz` is in the asset's size units; zero means none.
    struct Order {
        uint32 asset;
        bool isBuy;
        uint64 sz;
        uint64 placedBlock;
    }

    uint256 public constant MAX_STEPS = 4;
    uint32 public constant MAX_COST_PPM = 100_000;
    /// The most leverage an exam's rules may allow, times 100: 10x.
    uint32 public constant MAX_LEVERAGE_X100 = 1000;
    /// The least notional a fill needs to count as a trade -- a trading day, and the end of a
    /// stretch without one: the exchange's own smallest order, 10 USDC.
    uint256 public constant MIN_TRADE_NOTIONAL = 10e6;
    uint256 internal constant PPM = 1_000_000;
    /// Upper bound for `fillDelay` and for `fillWindow`.
    uint16 public constant MAX_FILL_BLOCKS = 600;
    /// How long after UTC midnight the day's snapshot may be taken.
    uint256 public constant CHECKPOINT_WINDOW = 15 minutes;

    /// The account that created this exam, and reads its result.
    address public pool;
    address public trader;
    /// A second key the trader lets place orders, if any.
    address public agent;

    Status public status;
    /// The step being traded, counting from 0.
    uint8 public step;
    uint64 public stepStartedAt;
    /// When the current step ends; 0 for no deadline.
    uint64 public deadline;
    /// When the step's latest fill was, or when the step started if it has none.
    uint64 public lastFillAt;
    uint64 public passedAt;
    Breach public endReason;
    /// Equity at the breach or at the pass (1e6 = 1 USDC); zero for an expired exam.
    int256 public endEquity;

    /// The balance outside positions (1e6 = 1 USDC). Equity is this plus every position at its mark.
    int256 public cash;
    /// Signed position in the asset's size units.
    mapping(uint32 asset => int256) public positionOf;
    /// UTC day of the latest daily snapshot, and equity at that snapshot.
    uint32 public day;
    int256 public dayStartEquity;
    /// UTC days of this step that saw a fill.
    uint16 public tradingDays;
    Order public pending;

    Rules internal _rules;
    ExamTerms internal _terms;
    mapping(uint32 asset => bool) internal _allowedAsset;
    /// The UTC day of the latest fill, plus one; zero before the step's first fill.
    uint32 private _lastFillDayPlusOne;

    event Started(address indexed trader, address indexed pool, uint64 capital);
    event AgentSet(address indexed agent);
    event Placed(uint32 indexed asset, bool isBuy, uint64 sz, uint256 fillFrom, uint256 fillUntil);
    event Lapsed(uint32 indexed asset, bool isBuy, uint64 sz);
    event Filled(uint32 indexed asset, bool isBuy, uint64 sz, uint64 px, uint256 cost);
    event DaySnapshot(uint32 indexed day, int256 equity);
    event StepPassed(uint8 indexed step, int256 equity);
    event Passed(address indexed trader, int256 equity);
    event Stopped(Status indexed status, Breach indexed reason, int256 equity);

    error BadTerms();
    error BadRules();
    error UnsupportedSizeDecimals(uint32 asset, uint8 szDecimals);
    error BadStatus(Status status);
    error NotTrader();
    error OwnOrder();
    error AssetNotAllowed(uint32 asset);
    error ZeroSize();
    error OrderPending();
    error NoOrder();
    error TooEarly(uint256 fillFrom);
    error OrderLapsed();
    error TooLate();
    error NoPrice(uint32 asset);
    error OverLeverage();
    error OutsideCheckpointWindow();
    error AlreadyCheckpointed();
    error NoBreach();
    error NotOver();
    error RuleBroken(Breach reason);
    error NotFlat();
    error TargetNotMet(int256 equity, int256 target);
    error NotEnoughDays(uint16 traded, uint16 needed);

    modifier inStatus(Status s) {
        if (status != s) revert BadStatus(status);
        _;
    }

    constructor() {
        _disableInitializers();
    }

    /// @notice Starts the exam for `trader_`. The caller is recorded as the pool.
    function initialize(address trader_, Rules calldata rules_, ExamTerms calldata terms_) external initializer {
        if (trader_ == address(0)) revert BadTerms();
        _checkTerms(terms_);
        _checkRules(rules_);

        pool = msg.sender;
        trader = trader_;
        _rules.dailyLossBps = rules_.dailyLossBps;
        _rules.maxDrawdownBps = rules_.maxDrawdownBps;
        _rules.maxLeverageX100 = rules_.maxLeverageX100;
        for (uint256 i = 0; i < rules_.assets.length; ++i) {
            _rules.assets.push(rules_.assets[i]);
            _allowedAsset[rules_.assets[i]] = true;
        }
        _rules.assetLeverageX100 = rules_.assetLeverageX100;
        _terms = terms_;

        status = Status.Active;
        emit Started(trader_, msg.sender, terms_.capital);
        _startStep();
    }

    // ── views ────────────────────────────────────────────────────

    function rules() external view returns (Rules memory) {
        return _rules;
    }

    function terms() external view returns (ExamTerms memory) {
        return _terms;
    }

    /// @notice Reverts if an exam could not be started on these rules and terms; `initialize` makes
    ///         the same two checks. For whoever sells exams, to find out before the first sale.
    function check(Rules calldata rules_, ExamTerms calldata terms_) external view {
        _checkTerms(terms_);
        _checkRules(rules_);
    }

    /// @notice What a pool needs to act on this exam: who took it, on what balance, and how it stands.
    function result() external view returns (Status, address, uint64, uint64) {
        return (status, trader, _terms.capital, passedAt);
    }

    /// @notice The balance plus every position at the mark of this block (1e6 = 1 USDC).
    function equity() public view returns (int256 eq) {
        (eq,) = _book();
    }

    /// @notice Every position at the mark of this block, sides added together (1e6 = 1 USDC).
    function notional() external view returns (uint256 ntl) {
        (, ntl) = _book();
    }

    /// @notice Which rule the exam breaks right now, if any.
    function violation() public view returns (Breach) {
        (int256 eq, uint256 ntl) = _book();
        return _judge(eq, ntl);
    }

    // ── the trader ─────────────────────────────────────────────────�

    /// @notice Lets `agent_` place orders as well as the trader; zero takes that back.
    function setAgent(address agent_) external {
        if (msg.sender != trader) revert NotTrader();
        agent = agent_;
        emit AgentSet(agent_);
    }

    /// @notice Leaves an order to buy or sell `sz` of `asset` at the mark of a later block. One
    ///         order waits at a time; an order nobody filled in its window is replaced.
    function place(uint32 asset, bool isBuy, uint64 sz) external inStatus(Status.Active) {
        if (msg.sender != trader && msg.sender != agent) revert NotTrader();
        if (_over()) revert TooLate();
        if (!_allowedAsset[asset]) revert AssetNotAllowed(asset);
        if (sz == 0) revert ZeroSize();
        Order memory old = pending;
        if (old.sz != 0) {
            if (block.number < _fillFrom(old) + _terms.fillWindow) revert OrderPending();
            emit Lapsed(old.asset, old.isBuy, old.sz);
        }
        pending = Order({asset: asset, isBuy: isBuy, sz: sz, placedBlock: uint64(block.number)});
        uint256 from = block.number + _terms.fillDelay;
        emit Placed(asset, isBuy, sz, from, from + _terms.fillWindow - 1);
    }

    // ── anyone ─────────────────────────────────────────────────�

    /// @notice Fills the waiting order at this block's oracle price. Anyone but the trader and their
    ///         agent may call it, from the order's first block to the last of its window: whoever
    ///         placed the order does not pick the block it fills in (deep audit, N6). An order that
    ///         would take the account over its leverage rule is refused; one that only reduces a
    ///         position never is.
    function fill() external inStatus(Status.Active) {
        if (msg.sender == trader || msg.sender == agent) revert OwnOrder();
        Order memory o = pending;
        if (o.sz == 0) revert NoOrder();
        uint256 from = _fillFrom(o);
        if (block.number < from) revert TooEarly(from);
        if (block.number >= from + _terms.fillWindow) revert OrderLapsed();
        if (_over()) revert TooLate();
        delete pending;

        uint64 px = _mark(o.asset);
        uint256 ntl = uint256(o.sz) * px;
        uint256 cost = (ntl * _terms.costPpm + PPM - 1) / PPM;
        int256 delta = o.isBuy ? int256(uint256(o.sz)) : -int256(uint256(o.sz));
        int256 before = positionOf[o.asset];
        int256 afterFill = before + delta;
        positionOf[o.asset] = afterFill;
        cash -= delta * int256(uint256(px)) + int256(cost);

        if (_abs(afterFill) > _abs(before)) {
            (int256 eq, uint256 open) = _book();
            if (eq <= 0 || open * 100 > uint256(eq) * _rules.maxLeverageX100 || _overAssetCap(eq)) {
                revert OverLeverage();
            }
        }

        // A fill smaller than the exchange would take is still a fill -- dust has to be closable
        // -- but it is not a trade: it neither makes a trading day nor ends an idle stretch.
        if (ntl >= MIN_TRADE_NOTIONAL) {
            uint32 today = _today();
            if (_lastFillDayPlusOne != today + 1) {
                _lastFillDayPlusOne = today + 1;
                tradingDays += 1;
            }
            lastFillAt = uint64(block.timestamp);
        }
        emit Filled(o.asset, o.isBuy, o.sz, px, cost);
    }

    /// @notice Takes the day's snapshot, in the first minutes of the UTC day. Anyone may.
    function checkpoint() external inStatus(Status.Active) {
        if (block.timestamp % 1 days >= CHECKPOINT_WINDOW) revert OutsideCheckpointWindow();
        uint32 today = _today();
        if (today <= day) revert AlreadyCheckpointed();
        // A day's base is never the equity that broke yesterday's rule: while a rule is broken the
        // snapshot waits, and `breach` ends the exam (deep audit, F12).
        (int256 eq, uint256 ntl) = _book();
        Breach reason = _judge(eq, ntl);
        if (reason != Breach.None) revert RuleBroken(reason);
        day = today;
        dayStartEquity = eq;
        emit DaySnapshot(today, eq);
    }

    /// @notice Ends the exam if a rule is broken right now. Anyone may call it.
    function breach() external inStatus(Status.Active) {
        (int256 eq, uint256 ntl) = _book();
        Breach reason = _judge(eq, ntl);
        if (reason == Breach.None) revert NoBreach();
        _end(Status.Breached, reason, eq);
    }

    /// @notice Ends an exam whose step ran past its deadline, or went longer without a fill than
    ///         its terms allow. Anyone may call it. It reads no price, so a price that will not
    ///         answer cannot keep an exam past its time; an expired exam records no equity.
    function expire() external inStatus(Status.Active) {
        if (!_over()) revert NotOver();
        _end(Status.Expired, Breach.None, 0);
    }

    /// @notice Passes the current step: no rule broken, nothing open, target met, in time, and
    ///         enough days traded. The last step passes the exam; any other starts the next one
    ///         on a fresh balance. Only the trader or their agent: the pass starts the clock the
    ///         trader then has to take the account, and nobody else picks that moment for them.
    function graduate() external inStatus(Status.Active) {
        if (msg.sender != trader && msg.sender != agent) revert NotTrader();
        if (_over()) revert TooLate();
        (int256 eq, uint256 ntl) = _book();
        Breach reason = _judge(eq, ntl);
        if (reason != Breach.None) revert RuleBroken(reason);
        if (ntl != 0) revert NotFlat();

        int256 capital = int256(uint256(_terms.capital));
        int256 target = capital + (capital * int256(uint256(_terms.targetsBps[step]))) / int256(uint256(Units.BPS));
        if (eq < target) revert TargetNotMet(eq, target);
        if (tradingDays < _terms.minTradingDays) revert NotEnoughDays(tradingDays, _terms.minTradingDays);

        if (uint256(step) + 1 < _terms.targetsBps.length) {
            emit StepPassed(step, eq);
            step += 1;
            delete pending;
            _startStep();
        } else {
            status = Status.Passed;
            passedAt = uint64(block.timestamp);
            endEquity = eq;
            emit Passed(trader, eq);
        }
    }

    // ── internals ───────────────────────────────────────────────�

    function _startStep() internal {
        cash = int256(uint256(_terms.capital));
        stepStartedAt = uint64(block.timestamp);
        deadline = _terms.stepDuration == 0 ? 0 : uint64(block.timestamp) + _terms.stepDuration;
        lastFillAt = uint64(block.timestamp);
        tradingDays = 0;
        _lastFillDayPlusOne = 0;
        day = _today();
        dayStartEquity = cash;
        emit DaySnapshot(day, dayStartEquity);
    }

    function _end(Status s, Breach reason, int256 eq) internal {
        status = s;
        endReason = reason;
        endEquity = eq;
        emit Stopped(s, reason, eq);
    }

    /// @dev Equity and open notional at this block's marks. Size times mark is already 1e6 USDC:
    ///      a perp's price carries `6 - szDecimals` decimals and its size `szDecimals`.
    function _book() internal view returns (int256 eq, uint256 ntl) {
        eq = cash;
        uint256 n = _rules.assets.length;
        for (uint256 i = 0; i < n; ++i) {
            uint32 a = _rules.assets[i];
            int256 pos = positionOf[a];
            if (pos == 0) continue;
            uint256 px = _mark(a);
            eq += pos * int256(px);
            ntl += _abs(pos) * px;
        }
    }

    /// @dev RuledAccount.violation, on numbers kept here. Equity at or under zero is already under
    ///      the drawdown line, so the leverage line only ever sees a positive one.
    function _judge(int256 eq, uint256 ntl) internal view returns (Breach) {
        int256 bps = int256(uint256(Units.BPS));
        int256 base = int256(uint256(_terms.capital));
        if (eq * bps < base * (bps - int256(uint256(_rules.maxDrawdownBps)))) return Breach.Drawdown;
        if (dayStartEquity > 0) {
            if (eq * bps < dayStartEquity * (bps - int256(uint256(_rules.dailyLossBps)))) return Breach.DailyLoss;
        }
        if (ntl * 100 > uint256(eq) * _rules.maxLeverageX100) return Breach.Leverage;
        if (_overAssetCap(eq)) return Breach.Leverage;
        return Breach.None;
    }

    /// @dev Whether a position's notional over equity is past its asset's own cap
    ///      (`Rules.assetLeverageX100`), at the same marks as `_book`. Equity is positive here.
    function _overAssetCap(int256 eq) internal view returns (bool) {
        uint256 n = _rules.assetLeverageX100.length;
        for (uint256 i = 0; i < n; ++i) {
            uint32 a = _rules.assets[i];
            int256 pos = positionOf[a];
            if (pos != 0 && _abs(pos) * _mark(a) * 100 > uint256(eq) * _rules.assetLeverageX100[i]) return true;
        }
        return false;
    }

    /// @dev Past the step's deadline, or longer without a fill than the terms allow. From then
    ///      on nothing is placed, filled or passed, and anyone may end the exam.
    function _over() internal view returns (bool) {
        if (deadline != 0 && block.timestamp > deadline) return true;
        return _terms.maxIdle != 0 && block.timestamp > uint256(lastFillAt) + _terms.maxIdle;
    }

    /// The chain's oracle price, which the validators take from the exchanges' prices outside: an
    /// account trading a thin book on Hyperliquid moves its mark, not this (deep audit, N6).
    function _mark(uint32 asset) internal view returns (uint64 px) {
        px = PrecompileLib.oraclePx(CoreOps.pxIndex(asset));
        if (px == 0) revert NoPrice(asset);
    }

    function _fillFrom(Order memory o) internal view returns (uint256) {
        return uint256(o.placedBlock) + _terms.fillDelay;
    }

    function _today() internal view returns (uint32) {
        return uint32(block.timestamp / 1 days);
    }

    function _abs(int256 x) internal pure returns (uint256) {
        return x < 0 ? uint256(-x) : uint256(x);
    }

    function _checkTerms(ExamTerms calldata t) internal pure {
        if (t.capital == 0) revert BadTerms();
        uint256 steps = t.targetsBps.length;
        if (steps == 0 || steps > MAX_STEPS) revert BadTerms();
        for (uint256 i = 0; i < steps; ++i) {
            if (t.targetsBps[i] == 0 || t.targetsBps[i] > Units.BPS) revert BadTerms();
        }
        if (t.costPpm > MAX_COST_PPM) revert BadTerms();
        if (t.fillDelay == 0 || t.fillDelay > MAX_FILL_BLOCKS) revert BadTerms();
        if (t.fillWindow == 0 || t.fillWindow > MAX_FILL_BLOCKS) revert BadTerms();
        // A step that cannot hold the days it asks for can never be passed, and its price would
        // have been taken for nothing.
        if (t.stepDuration != 0 && uint256(t.minTradingDays) > uint256(t.stepDuration) / 1 days + 1) {
            revert BadTerms();
        }
    }

    /// @dev PoolFactory's bounds, with a ceiling on leverage it does not have, and without its list
    ///      of assets: whoever creates the exam holds that.
    function _checkRules(Rules calldata r) internal view {
        if (r.assets.length == 0 || r.assets.length > Units.MAX_ASSETS) revert BadRules();
        if (r.dailyLossBps == 0 || r.dailyLossBps >= Units.BPS) revert BadRules();
        if (r.maxDrawdownBps == 0 || r.maxDrawdownBps >= Units.BPS) revert BadRules();
        if (r.maxLeverageX100 < 100 || r.maxLeverageX100 > MAX_LEVERAGE_X100) revert BadRules();
        uint256 caps = r.assetLeverageX100.length;
        if (caps != 0 && caps != r.assets.length) revert BadRules();
        for (uint256 i = 0; i < r.assets.length; ++i) {
            for (uint256 j = 0; j < i; ++j) {
                if (r.assets[j] == r.assets[i]) revert BadRules();
            }
            if (caps != 0 && (r.assetLeverageX100[i] < 100 || r.assetLeverageX100[i] > r.maxLeverageX100)) {
                revert BadRules();
            }
            uint8 szDecimals = PrecompileLib.perpAssetInfo(CoreOps.pxIndex(r.assets[i])).szDecimals;
            if (szDecimals > 6) revert UnsupportedSizeDecimals(r.assets[i], szDecimals);
        }
    }
}
