// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {Rules, Cancel, Breach, Units} from "./Types.sol";
import {CoreOps} from "./lib/CoreOps.sol";
import {AccountOps} from "./lib/AccountOps.sol";
import {KeyRegistry} from "./KeyRegistry.sol";

interface IFactoryView {
    function registry() external view returns (KeyRegistry);
}

/// @title RuledAccount
/// @notice A contract that owns a HyperCore account, lets one agent key trade it, and can be
///         stopped by anyone when the account breaks its rules.
/// @dev Shared by Pool and ChallengeAccount. Precompile reads reflect the start of the block;
///      CoreWriter writes land a few seconds later. So a stop is a sequence of calls: one
///      that cuts the agent and sends the closing orders, then settle calls until the
///      account is flat and its perp balance is back on spot.
///
///      Daily loss is measured from the latest snapshot. A snapshot can only be taken in the
///      first minutes of a UTC day, so nobody, the trader included, can pick a convenient
///      moment later in the day. If nobody takes one, the previous snapshot stays in force;
///      depending on how equity moved since, that makes the day's limit tighter or looser
///      than the true start of the day would. The operator's keeper takes it at midnight,
///      and anyone else may.
abstract contract RuledAccount is Initializable {
    /// Slippage allowed on the orders a stop sends to close positions.
    uint16 public constant CLOSE_SLIPPAGE_BPS = 500;
    /// How long after UTC midnight the day's snapshot may be taken.
    uint256 public constant CHECKPOINT_WINDOW = 15 minutes;

    IFactoryView public factory;

    Rules internal _rules;
    mapping(uint32 asset => bool) internal _allowedAsset;
    /// The perp dex the account's assets trade on, 0 for the main one: one dex an account, where
    /// its capital is (HIP-3, the CTO, 21 October 2026).
    uint32 public dex;
    /// A start on a builder dex takes two steps (audit window 2, В4-2): the capital is sent, and the
    /// account starts once a later block shows it there. When it was sent, and how much; zero when
    /// nothing is on its way.
    uint64 public sentToDexAt;
    uint64 public sentToDex;
    /// What was over the target when the account started on its dex (1e6): somebody's units, or a
    /// send that was only late landing with its repeat. It stays where it is and is no part of the
    /// account's equity, so it is never the trader's gain, nor more to trade with or to lose,
    /// whatever HyperCore does with a transfer; the settlement takes it back with the rest
    /// (self-audit v3, round 1, T1).
    uint64 public startSurplus;

    /// UTC day of the latest daily snapshot, and equity at that snapshot (1e6 = 1 USDC).
    uint32 public day;
    int64 public dayStartEquity;

    /// Agent key currently approved on this account, if any.
    address public agentKey;
    /// When HyperCore was asked to approve `agentKey`, for `AGENT_TERM` from then
    /// (`agentValidUntil`). An agent approved with no name lived fourteen days and was gone (a live
    /// challenge's, 18 October 2026); one approved by name carries its term, which HyperCore shows
    /// (КОЛИЗЕЙ-2's measurement, 19 October 2026).
    uint64 public agentApprovedAt;
    uint256 private _keylessNonce;
    /// The key the last stop cut off, and the block of the latest replacement. A replacement
    /// can fail on HyperCore without a trace; anyone who sees `cutKey` still acting as this
    /// account's agent a few blocks after `cutBlock` should call `recut`.
    address public cutKey;
    uint64 public cutBlock;

    event DaySnapshot(uint32 indexed day, int64 equity);
    event AgentSet(address indexed key);
    event AgentCut(address indexed oldKey, address indexed keyless);
    event CancelSent(uint32 indexed asset, uint64 oid);
    event ClosingOrders(uint256 positions);
    event MovedToSpot(uint64 usd1e6);
    /// A position outside the named assets is still open (notional in 1e6 USDC); settlement
    /// waits until someone names its asset.
    event UnnamedPositionOpen(uint64 notional);

    error TooManyAssets();
    error NotRenewable(uint64 opens);
    error KeyWorks();
    error RekeyClosed();
    error TooManyCancels();
    error NotOnDex();
    error NoBreach();
    error RuleBroken(Breach reason);
    error NotStopped();
    error OutsideCheckpointWindow();
    error AlreadyCheckpointed();

    function __RuledAccount_init(IFactoryView factory_, Rules memory rules_) internal onlyInitializing {
        factory = factory_;
        if (rules_.assets.length > Units.MAX_ASSETS) revert TooManyAssets();
        _rules.dailyLossBps = rules_.dailyLossBps;
        _rules.maxDrawdownBps = rules_.maxDrawdownBps;
        _rules.maxLeverageX100 = rules_.maxLeverageX100;
        for (uint256 i = 0; i < rules_.assets.length; ++i) {
            _rules.assets.push(rules_.assets[i]);
            _allowedAsset[rules_.assets[i]] = true;
        }
        _rules.assetLeverageX100 = rules_.assetLeverageX100;
        if (rules_.assets.length != 0) dex = CoreOps.dexOf(rules_.assets[0]);
    }

    // ── views ────────────────────────────────────────────────────────────────────────

    function rules() external view returns (Rules memory) {
        return _rules;
    }

    function isAllowedAsset(uint32 asset) external view returns (bool) {
        return _allowedAsset[asset];
    }

    /// @notice Perp account value (1e6 = 1 USDC): the main dex's and the account's dex's, less what
    ///         was over the target at the start (`startSurplus`).
    function accountEquity() public view returns (int64) {
        return CoreOps.equity(address(this), dex) - int64(startSurplus);
    }

    /// @notice Notional open on the main dex and the account's dex (1e6).
    function openNotional() external view returns (uint64) {
        return CoreOps.margin(address(this), dex).ntlPos;
    }

    /// @notice Equity the static drawdown is measured from (1e6 = 1 USDC).
    function drawdownBase() public view virtual returns (int64);

    /// @notice Whether the account has been stopped and not yet fully settled.
    function isStopped() public view virtual returns (bool);

    /// @notice Which rule the account breaks right now, if any. `extraAssets` are assets
    ///         outside the pool's list that the caller believes the account holds.
    function violation(uint32[] memory extraAssets) public view returns (Breach) {
        PrecompileLib.AccountMarginSummary memory m = CoreOps.margin(address(this), dex);
        int256 eq = m.accountValue - int64(startSurplus);
        int256 bps = int256(uint256(Units.BPS));

        int256 base = drawdownBase();
        if (eq * bps < base * (bps - int256(uint256(_rules.maxDrawdownBps)))) return Breach.Drawdown;

        if (dayStartEquity > 0) {
            if (eq * bps < int256(dayStartEquity) * (bps - int256(uint256(_rules.dailyLossBps)))) {
                return Breach.DailyLoss;
            }
        }

        if (m.ntlPos > 0) {
            if (eq <= 0) return Breach.Drawdown;
            if (uint256(m.ntlPos) * 100 > uint256(eq) * _rules.maxLeverageX100) return Breach.Leverage;
            if (AccountOps.overAssetCap(_rules, uint256(eq))) return Breach.Leverage;
        }

        if (extraAssets.length > Units.MAX_ASSETS) revert TooManyAssets();
        for (uint256 i = 0; i < extraAssets.length; ++i) {
            uint32 a = extraAssets[i];
            if (!_allowedAsset[a] && CoreOps.positionSize(address(this), a) != 0) return Breach.ForbiddenAsset;
        }
        return Breach.None;
    }

    // ── anyone ───────────────────────────────────────────────────────────────────────

    /// @notice Replaces the agent of a stopped account again, with another fresh keyless
    ///         address. Useful if the replacement sent by the stop did not take effect on
    ///         HyperCore (for example because someone funded the chosen address first).
    ///         `salt` is any value the caller picks; it only changes which keyless address
    ///         is used.
    function recut(bytes32 salt) external {
        if (!isStopped()) revert NotStopped();
        _cutAgent(salt);
    }

    // ── internals for the stop and the settlement ────────────────────────────────────

    function _today() internal view returns (uint32) {
        return uint32(block.timestamp / 1 days);
    }

    /// @dev The day's snapshot, allowed once per day and only just after UTC midnight.
    function _checkpoint() internal {
        if (block.timestamp % 1 days >= CHECKPOINT_WINDOW) revert OutsideCheckpointWindow();
        uint32 today = _today();
        if (today <= day) revert AlreadyCheckpointed();
        // A day's base is never the equity that broke yesterday's rule: while a money rule is
        // broken the snapshot waits, and `breach` stops the account (deep audit, F12).
        Breach reason = violation(new uint32[](0));
        if (reason != Breach.None) revert RuleBroken(reason);
        day = today;
        dayStartEquity = accountEquity();
        emit DaySnapshot(today, dayStartEquity);
    }

    function _startDay(int64 equity_) internal {
        day = _today();
        dayStartEquity = equity_;
        emit DaySnapshot(day, equity_);
    }

    /// The term every agent is approved for. HyperCore takes 180 days at most, counted from its own
    ///      clock, which runs a few seconds behind the block's: one second over is refused without a
    ///      word, and the account is left with no agent (КОЛИЗЕЙ-2, 19 October 2026). So 179.
    uint64 public constant AGENT_TERM = 179 days;
    /// The last days of a term, in which the agent may be renewed with a fresh key (`_renewAgent`).
    uint64 public constant RENEW_WINDOW = 14 days;
    /// A key spoiled in the block it was approved in may be replaced within a day of that approval,
    /// twice at most for each key the account opened or was renewed with, on its trader's word alone
    /// (deep audit F10, AUD-2; audit window 2, A5-1).
    uint64 public constant REKEY_WINDOW = 1 days;
    uint8 public constant MAX_REKEYS = 2;
    /// Fresh keys taken since the account last opened a stage or was renewed.
    uint8 public rekeys;

    /// @notice When the approval of the agent ends, in HyperCore's milliseconds: the term in the name
    ///         it was approved under.
    function agentValidUntil() public view returns (uint64) {
        return (agentApprovedAt + AGENT_TERM) * 1000;
    }

    function _setAgent(address key) internal {
        agentKey = key;
        agentApprovedAt = uint64(block.timestamp);
        AccountOps.setAgent(key, agentValidUntil());
        emit AgentSet(key);
    }

    /// @dev Retires the agent key for good and gives `trader_` a fresh one from the registry, approved
    ///      under the same name, so that it replaces the old one on HyperCore.
    function _freshKey(address trader_) internal {
        KeyRegistry registry = factory.registry();
        registry.retire(agentKey);
        _setAgent(registry.assign(trader_));
    }

    /// @dev A term that runs out while the account trades is renewed with a fresh key, never with the
    ///      same one: HyperCore ignores a second approval of a key it holds, and a key it has let go
    ///      may have its signed actions sent again once their nonces are pruned (Hyperliquid's docs;
    ///      КОЛИЗЕЙ-2 and audit window 2, 19 October 2026). From the last `RENEW_WINDOW` of the term
    ///      on, and at any time after it: a stage whose window passed unrenewed would otherwise be
    ///      left with no key, and a fresh key carries no risk once HyperCore has let the old one go
    ///      (audit window 2, A5-2). The caller checks the account trades.
    function _renewAgent(address trader_) internal {
        uint64 opens = agentApprovedAt + AGENT_TERM - RENEW_WINDOW;
        if (block.timestamp < opens) revert NotRenewable(opens);
        rekeys = 0;
        _freshKey(trader_);
    }

    /// @dev A fresh key for `trader_` when the account's own has gained a HyperCore account, which
    ///      HyperCore does not take as an agent: a transfer that lands in the block the key is
    ///      approved in spoils it. Within a day of the approval, twice at most for each key the
    ///      account opened or was renewed with: a key HyperCore already took keeps working after it
    ///      gains an account, and on anyone's word a stranger's 1 USDC would replace a working key and
    ///      spend the registry's free keys one by one (deep audit, AUD-2). The caller checks that the
    ///      trader asks and that the account trades. A key that has no account stays.
    function _rekey(address trader_) internal {
        if (block.timestamp > uint256(agentApprovedAt) + REKEY_WINDOW || rekeys >= MAX_REKEYS) revert RekeyClosed();
        if (!CoreOps.exists(agentKey)) revert KeyWorks();
        ++rekeys;
        _freshKey(trader_);
    }

    /// @dev Replaces the agent with a fresh keyless address and retires the old key for good.
    ///      Never reverts on the choice of address, so nobody can block a stop by funding
    ///      the candidates in advance. The keyless address is approved under the key's name,
    ///      `pools`, which is all HyperCore takes for the name, for a term of its own from now: a key
    ///      past its term would otherwise be cut under a term HyperCore refuses.
    function _cutAgent(bytes32 salt) internal {
        address old = agentKey;
        address keyless = AccountOps.keylessAddress(address(this), _keylessNonce++, salt);
        AccountOps.setAgent(keyless, (block.timestamp + AGENT_TERM) * 1000);
        agentKey = address(0);
        cutBlock = uint64(block.number);
        if (old != address(0)) {
            cutKey = old;
            factory.registry().retire(old);
        }
        emit AgentCut(old, keyless);
    }

    /// @dev Moves what the perp side can give to spot, the account's dex's whole value first, and
    ///      returns what was asked to move (1e6).
    /// A send a builder dex still does not show this long after is made again.
    uint64 public constant RESEND_WAIT = 1 minutes;

    /// Sends a start's capital to the account's dex, for the start to end on a later block.
    function _sendToDex(uint64 amount) internal {
        CoreOps.toPerp(amount, dex);
        sentToDex = amount;
        sentToDexAt = uint64(block.timestamp);
    }

    /// Whether the capital a start sent shows on the dex: equity at `target` or over it, read in a
    /// later second than the send, as a precompile answers with the start of the block. Anybody may
    /// send the account a unit, and nobody but the account can take one off, so a unit never holds
    /// the start; only somebody sending as much as the send itself could stand in for it, with their
    /// own money. HyperCore drops an action without a trace, so a send the dex still does not show
    /// after RESEND_WAIT is made again, and this says false; before that it refuses. What is over
    /// `target` neither holds the start nor moves its clock: it is the start's surplus, outside the
    /// account's equity from then on (self-audit v3, round 1, T1; audit window 2, В5-1).
    function _landed(int64 target) internal returns (bool) {
        uint64 at = sentToDexAt;
        if (block.timestamp > at) {
            int64 eq = accountEquity();
            if (eq >= target) {
                sentToDexAt = 0;
                startSurplus = uint64(eq - target);
                return true;
            }
        }
        if (block.timestamp <= at + RESEND_WAIT) revert NotOnDex();
        _sendToDex(sentToDex);
        return false;
    }

    function _toSpotAll() internal returns (uint64 free) {
        free = AccountOps.toSpotAll(dex);
        if (free != 0) emit MovedToSpot(free);
    }

    function _cancelAll(Cancel[] memory cancels) internal {
        if (cancels.length > Units.MAX_CANCELS) revert TooManyCancels();
        for (uint256 i = 0; i < cancels.length; ++i) {
            CoreOps.cancel(cancels[i].asset, cancels[i].oid);
            emit CancelSent(cancels[i].asset, cancels[i].oid);
        }
    }

    function _closeAll(uint32[] memory extra) internal returns (uint256 open) {
        open = AccountOps.closeAll(_rules, extra, CLOSE_SLIPPAGE_BPS);
        if (open != 0) emit ClosingOrders(open);
    }

    /// @dev One settlement step: cancel the named orders (a resting order holds margin, so
    ///      this has to be repeatable), close what is open, and if nothing is, move the free
    ///      perp balance to spot. Returns what the start-of-block state showed. A position in
    ///      an asset nobody named still counts in the account's notional; until it is gone,
    ///      nothing moves to spot and `open` stays above zero.
    /// @dev True when the perp side is holding nothing back: no position open, and every dollar
    ///      still there is withdrawable. A RESTING ORDER'S MARGIN IS NOT WITHDRAWABLE -- that is
    ///      the whole point of asking it this way. `withdrawable == 0` reads the same whether the
    ///      perp side is empty or a limit order is sitting on the money, and a share paid on that
    ///      reading comes out of whatever happened to reach spot in time (audit A-04). Somebody
    ///      else's dust does not trip this: a donation is withdrawable, so it keeps equity and
    ///      withdrawable equal.
    ///      The value is the main dex's and the account's dex's together, and withdrawable is the
    ///      main dex's alone: on a builder-deployed dex nothing is held only once its side is empty,
    ///      HyperCore showing no withdrawable for it and a resting order's margin there not to be
    ///      told apart.
    function _nothingHeldOnPerp() internal view returns (bool) {
        PrecompileLib.AccountMarginSummary memory m = CoreOps.margin(address(this), dex);
        return m.ntlPos == 0 && m.accountValue <= int64(CoreOps.withdrawable(address(this)));
    }

    function _drainStep(Cancel[] memory cancels, uint32[] memory extra)
        internal
        returns (uint256 open, uint64 free, uint64 spot)
    {
        _cancelAll(cancels);
        open = _closeAll(extra);
        if (open != 0) return (open, 0, 0);
        uint64 notional = CoreOps.margin(address(this), dex).ntlPos;
        if (notional != 0) {
            emit UnnamedPositionOpen(notional);
            return (1, 0, 0);
        }
        free = _toSpotAll();
        spot = CoreOps.spotUsdc(address(this));
    }
}
