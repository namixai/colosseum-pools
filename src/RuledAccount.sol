// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {Rules, Cancel, Breach, Units} from "./Types.sol";
import {CoreOps} from "./lib/CoreOps.sol";
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

    /// UTC day of the latest daily snapshot, and equity at that snapshot (1e6 = 1 USDC).
    uint32 public day;
    int64 public dayStartEquity;

    /// Agent key currently approved on this account, if any.
    address public agentKey;
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
    error TooManyCancels();
    error NoBreach();
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
    }

    // ── views ────────────────────────────────────────────────────────────────────────

    function rules() external view returns (Rules memory) {
        return _rules;
    }

    function isAllowedAsset(uint32 asset) external view returns (bool) {
        return _allowedAsset[asset];
    }

    /// @notice Equity the static drawdown is measured from (1e6 = 1 USDC).
    function drawdownBase() public view virtual returns (int64);

    /// @notice Whether the account has been stopped and not yet fully settled.
    function isStopped() public view virtual returns (bool);

    /// @notice Which rule the account breaks right now, if any. `extraAssets` are assets
    ///         outside the pool's list that the caller believes the account holds.
    function violation(uint32[] memory extraAssets) public view returns (Breach) {
        PrecompileLib.AccountMarginSummary memory m = CoreOps.margin(address(this));
        int256 eq = m.accountValue;
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
        day = today;
        dayStartEquity = CoreOps.equity(address(this));
        emit DaySnapshot(today, dayStartEquity);
    }

    function _startDay(int64 equity_) internal {
        day = _today();
        dayStartEquity = equity_;
        emit DaySnapshot(day, equity_);
    }

    function _setAgent(address key) internal {
        agentKey = key;
        CoreOps.setAgent(key);
        emit AgentSet(key);
    }

    /// @dev Replaces the agent with a fresh keyless address and retires the old key for good.
    ///      Never reverts on the choice of address, so nobody can block a stop by funding
    ///      the candidates in advance.
    function _cutAgent(bytes32 salt) internal {
        address old = agentKey;
        address keyless = CoreOps.keylessAddress(address(this), _keylessNonce++, salt);
        CoreOps.setAgent(keyless);
        agentKey = address(0);
        cutBlock = uint64(block.number);
        if (old != address(0)) {
            cutKey = old;
            factory.registry().retire(old);
        }
        emit AgentCut(old, keyless);
    }

    function _cancelAll(Cancel[] memory cancels) internal {
        if (cancels.length > Units.MAX_CANCELS) revert TooManyCancels();
        for (uint256 i = 0; i < cancels.length; ++i) {
            CoreOps.cancel(cancels[i].asset, cancels[i].oid);
            emit CancelSent(cancels[i].asset, cancels[i].oid);
        }
    }

    /// @dev The pool's assets plus whatever the caller adds, without duplicates.
    function _assetsWith(uint32[] memory extra) internal view returns (uint32[] memory all) {
        uint256 n = _rules.assets.length;
        if (n + extra.length > Units.MAX_ASSETS) revert TooManyAssets();
        all = new uint32[](n + extra.length);
        for (uint256 i = 0; i < n; ++i) {
            all[i] = _rules.assets[i];
        }
        uint256 k = n;
        for (uint256 i = 0; i < extra.length; ++i) {
            bool seen = false;
            for (uint256 j = 0; j < k; ++j) {
                if (all[j] == extra[i]) {
                    seen = true;
                    break;
                }
            }
            if (!seen) all[k++] = extra[i];
        }
        assembly {
            mstore(all, k)
        }
    }

    function _closeAll(uint32[] memory extra) internal returns (uint256 open) {
        open = CoreOps.closePositions(address(this), _assetsWith(extra), CLOSE_SLIPPAGE_BPS);
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
    function _nothingHeldOnPerp() internal view returns (bool) {
        return CoreOps.margin(address(this)).ntlPos == 0
            && CoreOps.equity(address(this)) <= int64(CoreOps.withdrawable(address(this)));
    }

    function _drainStep(Cancel[] memory cancels, uint32[] memory extra)
        internal
        returns (uint256 open, uint64 free, uint64 spot)
    {
        _cancelAll(cancels);
        open = _closeAll(extra);
        if (open != 0) return (open, 0, 0);
        uint64 notional = CoreOps.margin(address(this)).ntlPos;
        if (notional != 0) {
            emit UnnamedPositionOpen(notional);
            return (1, 0, 0);
        }
        free = CoreOps.withdrawable(address(this));
        if (free != 0) {
            CoreOps.toSpot(free);
            emit MovedToSpot(free);
        }
        spot = CoreOps.spotUsdc(address(this));
    }
}
