// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {Rules, Terms, Units} from "./Types.sol";
import {CoreOps} from "./lib/CoreOps.sol";
import {KeyRegistry, IAccountSource} from "./KeyRegistry.sol";
import {IFactoryView} from "./RuledAccount.sol";
import {Pool} from "./Pool.sol";
import {ChallengeAccount, IPoolHooks} from "./ChallengeAccount.sol";

interface IExamSeller {
    function examImpl() external view returns (address);
}

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
    /// Paid by every challenge buyer to `feeRecipient`, on top of the pool's price, and never
    /// refunded (HyperEVM USDC units). Each challenge uses one agent key for good; without
    /// a cost that the buyer can't get back, anyone could use up the published keys.
    uint256 public challengeFee;
    address public feeRecipient;

    mapping(uint32 asset => bool) public isPlatformAsset;
    /// Perp assets a funded account of a MultiPool may trade: those whose book one account cannot
    /// move, by the operator's reading of their depth (deep audit, N6).
    mapping(uint32 asset => bool) public isFundedAsset;
    mapping(address => bool) public isPool;
    mapping(address => bool) public isChallenge;
    /// MultiPools the operator has recognized. Only a pool one of them owns may put a trader of its
    /// own choosing on its capital, and so take a key from the registry with no challenge sold:
    /// anyone else's pool run by its owner would take and retire a key for gas (audit K-3).
    mapping(address => bool) public isMultiPool;
    /// Exam implementations the operator approved; a shares contract binds no pool that sells
    /// another (the cloud audit, section 6).
    mapping(address => bool) public isExamImpl;
    /// The hash of the code a shares contract is created from, which the operator sets, and for every
    /// shares contract created here the hash of the code it was created from; zero for any other
    /// address. A shares contract binds only a pool of the factory that created it, so the app checks
    /// one address, this factory's: that it names a hash, and one of the versions it trusts (the cloud
    /// audit, section 6; audit window 2, A3-1: the operator may set another code since).
    bytes32 public sharesCode;
    mapping(address => bytes32) public isPoolShares;
    address[] private _pools;
    /// Pools that have sold a challenge, each once, in the order of their first. The keeper walks
    /// these and never the rest: anyone creates pools for free, and a challenge costs its price and
    /// the platform's fee (audit, K-1).
    address[] private _challenged;
    mapping(address => bool) public hasChallenged;

    event PoolCreated(address indexed pool, address indexed owner);
    event ChallengeCreated(address indexed challenge, address indexed pool, address indexed trader);
    event PlatformAssetSet(uint32 indexed asset, bool allowed);
    event FundedAssetSet(uint32 indexed asset, bool allowed);
    event BuilderSet(address indexed builder, uint64 maxFee);
    event OperatorSet(address indexed operator);
    event ChallengeFeeSet(uint256 fee, address indexed recipient);
    event MultiPoolSet(address indexed pool, bool recognized);
    event ExamImplSet(address indexed impl, bool approved);
    event SharesCodeSet(bytes32 hash);
    event PoolSharesCreated(address indexed shares, address indexed creator, bytes32 code);

    error NotOperator();
    error NotPool();
    error BadRules();
    error BadTerms();
    error AssetNotListed(uint32 asset);
    error NotThatMarket(uint32 asset, string coin);
    error NotAnOrderId(uint32 asset);
    error BadNames();
    error BadFee();
    error BadCode();

    /// @param usdc_ The token prices and payouts on HyperEVM are in: the chain's USDC
    ///        (HLConstants.usdc()), or on testnet the demo's own (src/demo/DemoUsdc.sol), which the
    ///        chain's is not ours to mint. A deployment parameter, not a constant (the CTO, 19 October
    ///        2026); the deploy scripts name it and record it.
    constructor(KeyRegistry registry_, address poolImpl_, address challengeImpl_, address operator_, IERC20 usdc_) {
        registry = registry_;
        poolImpl = poolImpl_;
        challengeImpl = challengeImpl_;
        operator = operator_;
        usdc = usdc_;
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
    ///         `names` are the markets' as HyperCore names them ("BTC", "xyz:XYZ100"), checked against
    ///         each asset when it is listed: a builder dex's order id read by the precompiles one off
    ///         does not fail, it reads another dex's market (spike q9, 21 October 2026). Delisting
    ///         takes no names; a listing takes one for each asset (self-audit v3, round 1, T12).
    function setPlatformAssets(uint32[] calldata assets, string[] calldata names, bool allowed) external onlyOperator {
        if (allowed && names.length != assets.length) revert BadNames();
        for (uint256 i = 0; i < assets.length; ++i) {
            if (allowed) _checkName(assets[i], names[i]);
            isPlatformAsset[assets[i]] = allowed;
            emit PlatformAssetSet(assets[i], allowed);
        }
    }

    /// @notice Lists or delists perp assets a MultiPool's funded accounts may trade. Delisting doesn't
    ///         touch pools that already name the asset.
    function setFundedAssets(uint32[] calldata assets, string[] calldata names, bool allowed) external onlyOperator {
        if (allowed && names.length != assets.length) revert BadNames();
        for (uint256 i = 0; i < assets.length; ++i) {
            if (allowed) _checkName(assets[i], names[i]);
            isFundedAsset[assets[i]] = allowed;
            emit FundedAssetSet(assets[i], allowed);
        }
    }

    function _checkName(uint32 asset, string calldata name) internal view {
        // An order id is a main dex's index, under 10000, or a builder dex's, from 110000 on. 100000
        // to 109999 is dex 0 by dexOf: the main dex's market read under an order id HyperCore does
        // not give it (audit window 2, В4-4). 10000 to 99999 the precompiles read as a builder dex's
        // market, dex and index, while dexOf and CoreWriter take it for the main dex's (on mainnet
        // 10029 reads xyz:CL; self-audit v3, round 1, T2).
        if (asset >= CoreOps.HIP3_DEX && asset < CoreOps.HIP3_BASE + CoreOps.HIP3_DEX) revert NotAnOrderId(asset);
        string memory coin = PrecompileLib.perpAssetInfo(CoreOps.pxIndex(asset)).coin;
        if (keccak256(bytes(coin)) != keccak256(bytes(name))) revert NotThatMarket(asset, coin);
    }

    function setChallengeFee(uint256 fee, address recipient) external onlyOperator {
        if (fee != 0 && recipient == address(0)) revert BadFee();
        challengeFee = fee;
        feeRecipient = recipient;
        emit ChallengeFeeSet(fee, recipient);
    }

    function setMultiPool(address pool, bool recognized) external onlyOperator {
        isMultiPool[pool] = recognized;
        emit MultiPoolSet(pool, recognized);
    }

    function setExamImpl(address impl, bool approved) external onlyOperator {
        isExamImpl[impl] = approved;
        emit ExamImplSet(impl, approved);
    }

    function setSharesCode(bytes32 hash) external onlyOperator {
        sharesCode = hash;
        emit SharesCodeSet(hash);
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

    /// @notice Whether `pool` is a MultiPool the operator recognized, selling an exam the operator
    ///         approved: the pool a shares contract may bind (the cloud audit, section 6).
    function isPlatformPool(address pool) external view returns (bool) {
        return isMultiPool[pool] && isExamImpl[IExamSeller(pool).examImpl()];
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

    /// @notice Up to `n` pools from the `from`-th, in the order they were created.
    function poolsFrom(uint256 from, uint256 n) external view returns (address[] memory) {
        return _page(_pools, from, n);
    }

    function challengedCount() external view returns (uint256) {
        return _challenged.length;
    }

    /// @notice Up to `n` of the pools that have sold a challenge, from the `from`-th, in the order of
    ///         their first challenge.
    function challengedFrom(uint256 from, uint256 n) external view returns (address[] memory) {
        return _page(_challenged, from, n);
    }

    function _page(address[] storage list, uint256 from, uint256 n) internal view returns (address[] memory page) {
        uint256 len = list.length;
        if (from >= len) return page;
        if (n > len - from) n = len - from;
        page = new address[](n);
        for (uint256 i = 0; i < n; ++i) {
            page[i] = list[from + i];
        }
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

    /// @notice Creates a shares contract whose creator is the caller, from `code`, which has to be the
    ///         code the operator set: PoolShares' creation code, too large for this factory to hold.
    ///         Its constructor's refusals come back as they are.
    function createPoolShares(bytes calldata code, uint64 minDeposit, uint256 target) external returns (address shares) {
        bytes32 hash = keccak256(code);
        if (hash != sharesCode) revert BadCode();
        bytes memory init = abi.encodePacked(code, abi.encode(msg.sender, usdc, minDeposit, target));
        assembly ("memory-safe") {
            shares := create(0, add(init, 0x20), mload(init))
            if iszero(shares) {
                returndatacopy(0, 0, returndatasize())
                revert(0, returndatasize())
            }
        }
        isPoolShares[shares] = hash;
        emit PoolSharesCreated(shares, msg.sender, hash);
    }

    /// @notice Called by a pool when a trader buys a challenge.
    function createChallenge(address trader) external returns (address ch) {
        if (!isPool[msg.sender]) revert NotPool();
        if (!hasChallenged[msg.sender]) {
            hasChallenged[msg.sender] = true;
            _challenged.push(msg.sender);
        }
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
        // Each asset's own cap: none, or one for each asset, from 1x to the account's cap.
        uint256 caps = r.assetLeverageX100.length;
        if (caps != 0 && caps != r.assets.length) revert BadRules();
        // One dex an account: its capital is on the dex its markets trade on (HIP-3).
        uint32 dex = CoreOps.dexOf(r.assets[0]);
        for (uint256 i = 0; i < r.assets.length; ++i) {
            if (!isPlatformAsset[r.assets[i]]) revert AssetNotListed(r.assets[i]);
            if (CoreOps.dexOf(r.assets[i]) != dex) revert BadRules();
            if (caps != 0 && (r.assetLeverageX100[i] < 100 || r.assetLeverageX100[i] > r.maxLeverageX100)) {
                revert BadRules();
            }
            for (uint256 j = 0; j < i; ++j) {
                if (r.assets[j] == r.assets[i]) revert BadRules();
            }
        }
    }

    function _checkTerms(Terms calldata t) internal pure {
        // A price of zero is refused. A challenge is what the pool sells; give it away and
        // the pool funds strangers out of the investor's capital, and every published agent
        // key can be used up for nothing. This is a floor of one unit, not a judgement about
        // what a challenge is worth: what one costs the pool depends on the rules and on who
        // buys it, which no contract can see. The app works that out before you create a pool.
        if (t.price == 0) revert BadTerms();
        if (t.capital == 0 || t.fundedCapital == 0 || t.duration == 0 || t.targetBps == 0) revert BadTerms();
        if (t.traderShareChallengeBps > Units.BPS || t.traderShareFundedBps > Units.BPS) revert BadTerms();
        // buyChallenge holds capital + fundedCapital on spot, in units 100 times finer, plus the
        // fee for creating the challenge account, as a uint64.
        if ((uint256(t.capital) + t.fundedCapital) * Units.SPOT_PER_PERP + Units.NEW_ACCOUNT_FEE > type(uint64).max) {
            revert BadTerms();
        }
    }
}
