// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";
import {Rules, Terms, Units} from "./Types.sol";
import {KeyRegistry, IAccountSource} from "./KeyRegistry.sol";
import {IFactoryView} from "./RuledAccount.sol";
import {Pool} from "./Pool.sol";
import {ChallengeAccount, IPoolHooks} from "./ChallengeAccount.sol";

/// @title PoolFactory
/// @notice Creates pools and their challenges, and is the only word on which addresses are
///         ours. Keeps the platform's asset list (perp indices from the testnet meta) and the
///         builder fee every account approves.
contract PoolFactory is IAccountSource, IFactoryView {
    address public immutable poolImpl;
    address public immutable challengeImpl;
    KeyRegistry public immutable registry;
    IERC20 public immutable usdc;

    address public operator;
    address public builderAddress;
    uint64 public builderMaxFee;

    mapping(uint32 asset => bool) public isPlatformAsset;
    mapping(address => bool) public isPool;
    mapping(address => bool) public isChallenge;
    address[] private _pools;

    event PoolCreated(address indexed pool, address indexed owner);
    event ChallengeCreated(address indexed challenge, address indexed pool, address indexed trader);
    event PlatformAssetSet(uint32 indexed asset, bool allowed);
    event BuilderSet(address indexed builder, uint64 maxFee);
    event OperatorSet(address indexed operator);

    error NotOperator();
    error NotPool();
    error BadRules();
    error BadTerms();
    error AssetNotListed(uint32 asset);

    constructor(KeyRegistry registry_, address poolImpl_, address challengeImpl_, address operator_) {
        registry = registry_;
        poolImpl = poolImpl_;
        challengeImpl = challengeImpl_;
        operator = operator_;
        usdc = IERC20(HLConstants.usdc());
        emit OperatorSet(operator_);
    }

    modifier onlyOperator() {
        if (msg.sender != operator) revert NotOperator();
        _;
    }

    // ── operator ─────────────────────────────────────────────────────────────────────

    function setOperator(address operator_) external onlyOperator {
        operator = operator_;
        emit OperatorSet(operator_);
    }

    /// @notice Lists or delists perp assets pools may choose from. Delisting doesn't touch
    ///         pools that already hold the asset in their rules.
    function setPlatformAssets(uint32[] calldata assets, bool allowed) external onlyOperator {
        for (uint256 i = 0; i < assets.length; ++i) {
            isPlatformAsset[assets[i]] = allowed;
            emit PlatformAssetSet(assets[i], allowed);
        }
    }

    function setBuilder(address builder_, uint64 maxFee) external onlyOperator {
        builderAddress = builder_;
        builderMaxFee = maxFee;
        emit BuilderSet(builder_, maxFee);
    }

    // ── views ────────────────────────────────────────────────────────────────────────

    function isAccount(address account) external view returns (bool) {
        return isPool[account] || isChallenge[account];
    }

    function builder() external view returns (address, uint64) {
        return (builderAddress, builderMaxFee);
    }

    function pools() external view returns (address[] memory) {
        return _pools;
    }

    function poolCount() external view returns (uint256) {
        return _pools.length;
    }

    // ── creation ─────────────────────────────────────────────────────────────────────

    /// @notice Creates a pool owned by the caller.
    function createPool(Rules calldata rules, Terms calldata terms) external returns (address pool) {
        _checkRules(rules);
        _checkTerms(terms);
        pool = Clones.clone(poolImpl);
        isPool[pool] = true;
        _pools.push(pool);
        Pool(pool).initialize(this, msg.sender, rules, terms);
        emit PoolCreated(pool, msg.sender);
    }

    /// @notice Called by a pool when a trader buys a challenge.
    function createChallenge(address trader) external returns (address ch) {
        if (!isPool[msg.sender]) revert NotPool();
        Pool pool = Pool(msg.sender);
        ch = Clones.clone(challengeImpl);
        isChallenge[ch] = true;
        ChallengeAccount(ch).initialize(this, IPoolHooks(msg.sender), trader, pool.rules(), pool.terms());
        emit ChallengeCreated(ch, msg.sender, trader);
    }

    function _checkRules(Rules calldata r) internal view {
        if (r.assets.length == 0 || r.assets.length > Units.MAX_ASSETS) revert BadRules();
        if (r.dailyLossBps == 0 || r.dailyLossBps >= Units.BPS) revert BadRules();
        if (r.maxDrawdownBps == 0 || r.maxDrawdownBps >= Units.BPS) revert BadRules();
        if (r.maxLeverageX100 < 100) revert BadRules();
        for (uint256 i = 0; i < r.assets.length; ++i) {
            if (!isPlatformAsset[r.assets[i]]) revert AssetNotListed(r.assets[i]);
            for (uint256 j = 0; j < i; ++j) {
                if (r.assets[j] == r.assets[i]) revert BadRules();
            }
        }
    }

    function _checkTerms(Terms calldata t) internal pure {
        if (t.capital == 0 || t.fundedCapital == 0 || t.duration == 0 || t.targetBps == 0) revert BadTerms();
        if (t.traderShareBps > Units.BPS) revert BadTerms();
        // buyChallenge holds capital + fundedCapital on spot, in units 100 times finer, as a uint64.
        if (uint256(t.capital) + t.fundedCapital > type(uint64).max / Units.SPOT_PER_PERP) revert BadTerms();
    }
}
