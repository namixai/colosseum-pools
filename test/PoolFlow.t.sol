// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test, Vm} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreState} from "@hyper-evm-lib/test/simulation/hyper-core/CoreState.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";

import {Rules, Terms, Cancel, Breach, Units} from "../src/Types.sol";
import {KeyRegistry} from "../src/KeyRegistry.sol";
import {PoolFactory} from "../src/PoolFactory.sol";
import {Pool} from "../src/Pool.sol";
import {ChallengeAccount} from "../src/ChallengeAccount.sol";
import {RuledAccount} from "../src/RuledAccount.sol";
import {CoreOps} from "../src/lib/CoreOps.sol";

contract CloseHarness {
    function close(uint32[] memory perps) external returns (uint256) {
        return CoreOps.closePositions(address(this), perps, 500);
    }
}

contract MockUsdc is ERC20 {
    constructor() ERC20("USDC", "USDC") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }
}

/// End-to-end flows on the hyper-evm-lib simulator, offline.
///
/// The simulator executes perp orders (at the mark), spot sends and spot/perp transfers. It
/// ignores API wallet changes, cancels, builder fees and margin-mode changes, so for those
/// the tests check the bytes our contracts send to CoreWriter, not an effect.
/// Calls settleFunded twice inside one transaction, so both calls read the same start-of-block
/// state. Written by the audit as test/audit/AuditRegression.t.sol; kept as they wrote it.
contract TwoStepsInOneBlock {
    function run(Pool p) external {
        Cancel[] memory c = new Cancel[](0);
        uint32[] memory a = new uint32[](0);
        p.settleFunded(c, a);
        p.settleFunded(c, a);
    }
}

contract PoolFlowTest is Test {
    address griefer = makeAddr("griefer");

    event RawAction(address indexed user, bytes data);

    address constant CORE_WRITER = 0x3333333333333333333333333333333333333333;
    bytes32 constant RAW_ACTION = keccak256("RawAction(address,bytes)");
    uint32 constant BTC = 3;
    uint32 constant ETH = 4;
    uint64 constant BTC_MARK = 764000; // 76400.0, one decimal (szDecimals 5)
    uint64 constant ETH_MARK = 241370; // 2413.70, two decimals (szDecimals 4)
    bytes32 constant SALT = keccak256("test-salt");

    HyperCore hyperCore;
    MockUsdc usdc;
    KeyRegistry registry;
    PoolFactory factory;

    address operator = makeAddr("operator");
    address investor = makeAddr("investor");
    address trader = makeAddr("trader");
    address stranger = makeAddr("stranger");
    address[] keys;

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        CoreSimulatorLib.setRevertOnFailure(true);
        // Fees are not what these tests are about; without them the arithmetic is exact.
        CoreSimulatorLib.setPerpMakerFee(0);
        CoreSimulatorLib.setSpotMakerFee(0);

        hyperCore.registerPerpAssetInfo(
            BTC,
            PrecompileLib.PerpAssetInfo({
                coin: "BTC", marginTableId: 54, szDecimals: 5, maxLeverage: 40, onlyIsolated: false
            })
        );
        hyperCore.registerPerpAssetInfo(
            ETH,
            PrecompileLib.PerpAssetInfo({
                coin: "ETH", marginTableId: 55, szDecimals: 4, maxLeverage: 25, onlyIsolated: false
            })
        );
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
        CoreSimulatorLib.setMarkPx(ETH, ETH_MARK);

        MockUsdc impl = new MockUsdc();
        vm.etch(HLConstants.usdc(), address(impl).code);
        usdc = MockUsdc(HLConstants.usdc());

        registry = new KeyRegistry(operator);
        factory = new PoolFactory(registry, address(new Pool()), address(new ChallengeAccount()), operator);
        for (uint256 i = 0; i < 6; ++i) {
            keys.push(makeAddr(string.concat("enclave-key-", vm.toString(i))));
        }
        uint32[] memory listed = new uint32[](2);
        listed[0] = BTC;
        listed[1] = ETH;
        vm.startPrank(operator);
        registry.setAccountSource(factory);
        registry.publish(keys);
        factory.setPlatformAssets(listed, true);
        vm.stopPrank();
    }

    // ── helpers ──────────────────────────────────────────────────────────────────────

    function _rules() internal pure returns (Rules memory r) {
        uint32[] memory assets = new uint32[](1);
        assets[0] = BTC;
        r = Rules({dailyLossBps: 500, maxDrawdownBps: 1000, maxLeverageX100: 500, assets: assets});
    }

    function _terms() internal pure returns (Terms memory) {
        return Terms({
            price: 25e6,
            capital: 100e6,
            targetBps: 800,
            duration: 7 days,
            // The two shares are deliberately different here: a test that reads one where it
            // means the other changes a number instead of passing by coincidence.
            traderShareChallengeBps: 5000,
            traderShareFundedBps: 8000,
            fundedCapital: 200e6
        });
    }

    function _readyPool() internal returns (Pool p) {
        vm.prank(investor);
        p = Pool(factory.createPool(_rules(), _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
    }

    function _buy(Pool p) internal returns (ChallengeAccount ch) {
        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        ch = ChallengeAccount(p.buyChallenge());
        vm.stopPrank();
    }

    function _started(Pool p) internal returns (ChallengeAccount ch) {
        ch = _buy(p);
        CoreSimulatorLib.nextBlock();
        ch.activate();
        CoreSimulatorLib.nextBlock();
    }

    /// Stands in for the trader's agent: an order executed on the account at the mark.
    function _trade(address account, uint32 perp, bool isBuy, uint64 sz1e8) internal {
        // Simulator quirk: the first perp order on an account re-seeds its perp balance from
        // the chain unless the balance was set with forcePerpBalance. Capital that arrived
        // through a spot-to-perp transfer would be wiped, so pin it before trading.
        CoreSimulatorLib.forcePerpBalance(account, hyperCore.readPerpBalance(account));
        CoreSimulatorLib.forcePerpLeverage(account, perp, 10);
        hyperCore.executePerpLimitOrder(
            account,
            CoreState.LimitOrderAction({
                asset: perp,
                isBuy: isBuy,
                limitPx: isBuy ? type(uint64).max / 2 : 1,
                sz: sz1e8,
                reduceOnly: false,
                encodedTif: 3,
                cloid: 0
            })
        );
    }

    /// The simulator reports account value with unrealized PnL divided by the position's
    /// leverage, and computes it in unsigned math that underflows once notional passes the
    /// balance at leverage 1. Hyperliquid does neither. So the rule tests below set the
    /// precompile answers directly, which is what our contracts read anyway, and the flow
    /// tests use the simulator only where orders have to execute.
    function _mockMargin(address account, int64 accountValue, uint64 ntlPos) internal {
        vm.mockCall(
            address(0x080F),
            abi.encode(uint32(0), account),
            abi.encode(PrecompileLib.AccountMarginSummary({
                accountValue: accountValue, marginUsed: ntlPos / 10, ntlPos: ntlPos, rawUsd: accountValue
            }))
        );
    }

    function _mockPosition(address account, uint32 perp, int64 szi) internal {
        vm.mockCall(
            address(0x0813),
            abi.encode(account, perp),
            abi.encode(PrecompileLib.Position({
                szi: szi, entryNtl: 0, isolatedRawUsd: 0, leverage: 10, isIsolated: false
            }))
        );
    }

    function _equity(address a) internal returns (int64) {
        return PrecompileLib.accountMarginSummary(0, a).accountValue;
    }

    function _spot(address a) internal returns (uint64) {
        return PrecompileLib.spotBalance(a, 0).total;
    }

    function _none() internal pure returns (Cancel[] memory c, uint32[] memory a) {
        c = new Cancel[](0);
        a = new uint32[](0);
    }

    /// Every CoreWriter action in the recorded logs, as (sender, kind, args).
    function _actions(Vm.Log[] memory logs)
        internal
        pure
        returns (address[] memory from, uint24[] memory kind, bytes[] memory args)
    {
        uint256 n;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter == CORE_WRITER && logs[i].topics[0] == RAW_ACTION) ++n;
        }
        from = new address[](n);
        kind = new uint24[](n);
        args = new bytes[](n);
        uint256 k;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter != CORE_WRITER || logs[i].topics[0] != RAW_ACTION) continue;
            bytes memory data = abi.decode(logs[i].data, (bytes));
            from[k] = address(uint160(uint256(logs[i].topics[1])));
            kind[k] = (uint24(uint8(data[1])) << 16) | (uint24(uint8(data[2])) << 8) | uint24(uint8(data[3]));
            bytes memory tail = new bytes(data.length - 4);
            for (uint256 j = 0; j < tail.length; ++j) {
                tail[j] = data[j + 4];
            }
            args[k] = tail;
            ++k;
        }
    }

    function _settleChallenge(ChallengeAccount ch) internal {
        uint32[] memory none = new uint32[](0);
        for (uint256 i = 0; i < 8 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
        }
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled), "challenge did not settle");
    }

    // ── creation ─────────────────────────────────────────────────────────────────────

    function test_createPool_checksRulesAndTerms() public {
        Rules memory r = _rules();
        Terms memory t = _terms();

        r.assets[0] = 7; // not listed by the platform
        vm.expectRevert(abi.encodeWithSelector(PoolFactory.AssetNotListed.selector, uint32(7)));
        factory.createPool(r, t);

        r = _rules();
        r.maxDrawdownBps = 0;
        vm.expectRevert(PoolFactory.BadRules.selector);
        factory.createPool(r, t);

        r = _rules();
        r.maxLeverageX100 = 99;
        vm.expectRevert(PoolFactory.BadRules.selector);
        factory.createPool(r, t);

        t.capital = 0;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t = _terms();
        t.traderShareChallengeBps = 10_001;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t = _terms();
        t.traderShareFundedBps = 10_001;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        // A challenge given away spends the investor's capital and a published key on nothing.
        t = _terms();
        t.price = 0;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t.price = 1; // one unit is enough; the contract sets no floor above zero
        factory.createPool(_rules(), t);
    }

    function test_createPool_refusesCapitalThatCannotBeHeldOnSpot() public {
        // capital + fundedCapital, in spot units, plus the fee for the challenge's new account
        uint64 limit = (type(uint64).max - Units.NEW_ACCOUNT_FEE) / Units.SPOT_PER_PERP;
        Terms memory t = _terms();
        t.capital = 1;
        t.fundedCapital = limit; // one unit over what a uint64 spot balance can hold
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t.fundedCapital = limit - 1; // exactly at the limit
        factory.createPool(_rules(), t);
    }

    /// Capital reaches a pool as a HyperCore spot transfer. There is no HyperEVM deposit to
    /// call: on testnet the USDC bridge credits nothing to a contract bridging to itself.
    function test_capitalOnlyArrivesOnCore() public {
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        deal(address(usdc), investor, 50e6);
        vm.startPrank(investor);
        usdc.approve(address(p), 50e6);
        (bool ok,) = address(p).call(abi.encodeWithSignature("deposit(uint256)", 50e6));
        vm.stopPrank();
        assertFalse(ok, "no HyperEVM deposit entry point");
        assertEq(usdc.balanceOf(investor), 50e6, "nothing was pulled");

        // The investor's spot transfer on HyperCore is what makes the pool sellable.
        CoreSimulatorLib.forceSpotBalance(address(p), 0, p.capitalNeeded());
        p.prepareAccount();
        _buy(p);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Challenge));
    }

    function test_implementationsCannotBeInitialized() public {
        Pool impl = Pool(factory.poolImpl());
        vm.expectRevert();
        impl.initialize(factory, stranger, _rules(), _terms());
    }

    function test_prepareAccount_needsCoreAccount_andSeparatesBalances() public {
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        vm.expectRevert(Pool.NotReady.selector);
        p.prepareAccount();

        CoreSimulatorLib.forceSpotBalance(address(p), 0, 10e8);
        vm.expectEmit(true, false, false, true, CORE_WRITER);
        emit RawAction(address(p), abi.encodePacked(uint8(1), uint24(16), abi.encode(address(p), uint8(1))));
        p.prepareAccount();
        assertTrue(p.accountReady());
    }

    // ── buying and starting ──────────────────────────────────────────────────────────

    /// The pool needs the challenge capital, the funded capital, and 1 USDC for creating the
    /// challenge's account, which HyperCore charges the sender on top of the transfer.
    function test_buyChallenge_needsCapitalForChallengeAndFunding() public {
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        assertEq(p.capitalNeeded(), 301e8);
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 301e8 - 1);
        p.prepareAccount();
        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(abi.encodeWithSelector(Pool.NotEnoughCapital.selector, uint64(301e8 - 1), uint64(301e8)));
        p.buyChallenge();
        vm.stopPrank();

        // Exactly enough: the capital arrives in full and the funded capital is still there.
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 301e8);
        vm.prank(trader);
        ChallengeAccount ch = ChallengeAccount(p.buyChallenge());
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), 100e8, "challenge capital in full");
        assertEq(_spot(address(p)), 200e8, "funded capital left after the account fee");
    }

    function test_buyChallenge_reservesKey_andSendsCapital() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);

        assertEq(uint8(p.stage()), uint8(Pool.Stage.Challenge));
        assertEq(usdc.balanceOf(address(p)), 25e6, "price held by the pool");
        assertEq(p.heldPrice(), 25e6);
        address key = ch.agentKey();
        assertTrue(registry.isBound(key, address(ch), trader), "key reserved for this challenge and trader");
        assertTrue(factory.isChallenge(address(ch)));

        // A second buyer is refused while the pool is taken.
        address other = makeAddr("other");
        deal(address(usdc), other, 25e6);
        vm.startPrank(other);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Challenge));
        p.buyChallenge();
        vm.stopPrank();

        assertEq(_spot(address(ch)), 0, "nothing moves inside the block");
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), 100e8);
    }

    function test_activate_waitsForCapital_thenStarts() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);

        vm.expectRevert(ChallengeAccount.NotStartedOnCore.selector);
        ch.activate();

        CoreSimulatorLib.nextBlock();
        address key = ch.agentKey();
        vm.recordLogs();
        ch.activate();
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());

        // Separate balances, capital to perp, then the reserved key as the unnamed agent.
        assertEq(kind.length, 3);
        assertEq(from[0], address(ch));
        assertEq(kind[0], 16);
        assertEq(args[0], abi.encode(address(ch), uint8(1)));
        assertEq(kind[1], 7);
        assertEq(args[1], abi.encode(uint64(100e6), true));
        assertEq(kind[2], 9);
        assertEq(args[2], abi.encode(key, ""));

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
        assertEq(p.earned(), 25e6, "price becomes the investor's once the challenge starts");
        assertEq(p.heldPrice(), 0);

        CoreSimulatorLib.nextBlock();
        assertEq(_equity(address(ch)), int64(100e6));
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));

        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.BadStatus.selector, ChallengeAccount.Status.Active));
        ch.activate();
    }

    // ── rules ────────────────────────────────────────────────────────────────────────

    function test_noBreach_meansNoStop() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 95e6, 380e6); // -5% total, 4x: inside every rule
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.expectRevert(RuledAccount.NoBreach.selector);
        ch.breach(c, a, SALT);
    }

    /// A minute past the next UTC midnight, inside the snapshot window.
    function _nextMidnight() internal {
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 60);
    }

    function test_drawdown_boundary() public {
        ChallengeAccount ch = _started(_readyPool());
        // Floor is 90 USDC: exactly 90 is allowed, a micro-dollar less is not. The day
        // limit (95 from the first day's base) would trip first, so take a new day's
        // snapshot at 90.
        _nextMidnight();
        _mockMargin(address(ch), 90e6, 0);
        ch.checkpoint();
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 90e6 - 1, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Drawdown));
    }

    function test_dailyLoss_boundary() public {
        ChallengeAccount ch = _started(_readyPool());
        // Day base is the capital, 100; the limit is 5%.
        _mockMargin(address(ch), 95e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 95e6 - 1, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.DailyLoss));
    }

    function test_dailyLoss_countsFromTheDaysSnapshot() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 96e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));

        // Next day: the snapshot takes the new base, once.
        _nextMidnight();
        ch.checkpoint();
        assertEq(ch.dayStartEquity(), int64(96e6));
        _mockMargin(address(ch), 92e6, 0);
        vm.expectRevert(RuledAccount.AlreadyCheckpointed.selector);
        ch.checkpoint();
        // 96 * 0.95 = 91.2: 92 is fine, 91.1 is not, and both are above the 90 floor.
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 91.1e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.DailyLoss));
    }

    /// Nobody can take the snapshot later in the day, the trader included: a base picked
    /// after a loss would let the day lose twice.
    function test_checkpoint_onlyJustAfterMidnight() public {
        ChallengeAccount ch = _started(_readyPool());
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 15 minutes);
        _mockMargin(address(ch), 97e6, 0);
        vm.expectRevert(RuledAccount.OutsideCheckpointWindow.selector);
        ch.checkpoint();
        assertEq(ch.dayStartEquity(), int64(100e6));
    }

    /// Without a snapshot for the new day, the last one stays in force: the rule is still
    /// checked, by the stop and by graduation alike.
    function test_dailyLoss_carriesTheLastSnapshot() public {
        ChallengeAccount ch = _started(_readyPool());
        vm.warp(block.timestamp + 1 days + 1 hours);
        _mockMargin(address(ch), 94e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.DailyLoss));
        (Cancel[] memory c, uint32[] memory a) = _none();
        ch.breach(c, a, SALT);
        assertEq(uint8(ch.breachReason()), uint8(Breach.DailyLoss));
    }

    function test_graduate_refusedWhileTheDayIsDown() public {
        ChallengeAccount ch = _started(_readyPool());
        _nextMidnight();
        _mockMargin(address(ch), 120e6, 0);
        ch.checkpoint();
        _mockMargin(address(ch), 113e6, 0); // above the 108 target, 5.8% under today's 120
        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.RuleBroken.selector, Breach.DailyLoss));
        ch.graduate(SALT);
    }

    function test_leverage_boundary() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 100e6, 500e6); // exactly 5x
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 100e6, 500e6 + 1);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Leverage));
    }

    function test_negativeEquity_withPositions_isADrawdown() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), -1, 10e6);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Drawdown));
    }

    function test_forbiddenAsset_onlyWhenNamed() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockPosition(address(ch), ETH, 100); // ETH is listed by the platform, not by this pool
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        uint32[] memory extra = new uint32[](1);
        extra[0] = ETH;
        assertEq(uint8(ch.violation(extra)), uint8(Breach.ForbiddenAsset));
        // A position in an allowed asset named by the caller is not a breach by itself.
        extra[0] = BTC;
        _mockPosition(address(ch), BTC, 100);
        assertEq(uint8(ch.violation(extra)), uint8(Breach.None));
    }

    function test_breach_cutsAgent_cancels_closes_thenSettles() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        address key = ch.agentKey();
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 741080);

        Cancel[] memory cancels = new Cancel[](1);
        cancels[0] = Cancel({asset: BTC, oid: 424242});
        _mockMargin(address(ch), 88.5e6, 370e6); // what Hyperliquid would report after -3%
        vm.recordLogs();
        vm.prank(stranger);
        ch.breach(cancels, new uint32[](0), SALT);
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        vm.clearMockedCalls();

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Breached));
        assertEq(uint8(ch.breachReason()), uint8(Breach.Drawdown));
        assertEq(kind.length, 3);

        // 1. the agent is replaced by an address nobody holds, and the key is retired
        assertEq(from[0], address(ch));
        assertEq(kind[0], 9);
        (address keyless, string memory name) = abi.decode(args[0], (address, string));
        assertTrue(keyless != key && keyless != address(0));
        assertEq(bytes(name).length, 0);
        assertFalse(PrecompileLib.coreUserExists(keyless));
        assertEq(uint8(registry.bindingOf(key).state), uint8(KeyRegistry.State.Retired));
        assertEq(ch.agentKey(), address(0));
        assertEq(ch.cutKey(), key, "the cut key stays on record for whoever checks it");
        assertEq(ch.cutBlock(), block.number);

        // 2. the named order is cancelled
        assertEq(kind[1], 10);
        assertEq(args[1], abi.encode(BTC, uint64(424242)));

        // 3. the long is closed: sell, 5% under the mark, rounded, reduce-only, IOC.
        //    74108.0 * 0.95 = 70402.6 -> 70402.0 -> 1e8 scale
        assertEq(kind[2], 1);
        assertEq(args[2], abi.encode(BTC, false, uint64(7040200000000), uint64(0.005e8), true, uint8(3), uint128(0)));

        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.position(address(ch), BTC).szi, 0, "position closed");

        uint64 poolSpotBefore = _spot(address(p));
        int64 left = _equity(address(ch));
        _settleChallenge(ch);
        assertEq(_spot(address(p)), poolSpotBefore + uint64(left) * 100, "everything back in the pool");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(p.challenge(), address(0));
    }

    /// Someone who predicts the stop's first keyless address and funds it on HyperCore
    /// doesn't block the stop: the next candidate is used.
    function _keylessCandidate(address account, uint256 nonce, bytes32 salt, uint256 i)
        internal
        view
        returns (address)
    {
        return address(
            uint160(
                uint256(
                    keccak256(
                        abi.encode(
                            "colosseum-pools/keyless", account, nonce, salt, i, block.number, blockhash(block.number - 1)
                        )
                    )
                )
            )
        );
    }

    function test_breach_skipsAKeylessAddressSomeoneActivated() public {
        ChallengeAccount ch = _started(_readyPool());
        address key = ch.agentKey();
        address first = _keylessCandidate(address(ch), 0, SALT, 0);
        address second = _keylessCandidate(address(ch), 0, SALT, 1);
        CoreSimulatorLib.forceAccountActivation(first);
        _mockMargin(address(ch), 80e6, 0);

        vm.recordLogs();
        (Cancel[] memory c, uint32[] memory a) = _none();
        ch.breach(c, a, SALT);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(kind[0], 9);
        (address keyless,) = abi.decode(args[0], (address, string));
        assertEq(keyless, second, "the pre-activated first candidate is skipped");
        assertTrue(keyless != key);
        assertFalse(PrecompileLib.coreUserExists(keyless));
    }

    /// Even if every candidate was funded in advance, the stop goes through: it uses the last
    /// candidate rather than revert, and anyone can replace the agent again with `recut`.
    function test_breach_goesThroughEvenIfEveryCandidateWasFunded() public {
        ChallengeAccount ch = _started(_readyPool());
        address last;
        for (uint256 i = 0; i < 8; ++i) {
            last = _keylessCandidate(address(ch), 0, SALT, i);
            CoreSimulatorLib.forceAccountActivation(last);
        }
        _mockMargin(address(ch), 80e6, 0);
        (Cancel[] memory c, uint32[] memory a) = _none();

        vm.expectRevert(RuledAccount.NotStopped.selector);
        ch.recut(SALT);

        vm.recordLogs();
        vm.prank(stranger);
        ch.breach(c, a, SALT);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Breached));
        assertEq(kind[0], 9);
        (address used,) = abi.decode(args[0], (address, string));
        assertEq(used, last);

        bytes32 other = keccak256("another salt");
        address fresh = _keylessCandidate(address(ch), 1, other, 0);
        vm.recordLogs();
        vm.prank(stranger);
        ch.recut(other);
        (, kind, args) = _actions(vm.getRecordedLogs());
        assertEq(kind.length, 1);
        assertEq(kind[0], 9);
        (used,) = abi.decode(args[0], (address, string));
        assertEq(used, fresh);
    }

    /// `recut` replaces the agent again but keeps pointing at the key that was cut, so a
    /// keeper can still check whether that key lost its agent role.
    function test_recut_keepsTheCutKeyOnRecord() public {
        ChallengeAccount ch = _started(_readyPool());
        address key = ch.agentKey();
        _mockMargin(address(ch), 80e6, 0);
        (Cancel[] memory c, uint32[] memory a) = _none();
        ch.breach(c, a, SALT);
        vm.clearMockedCalls();
        uint64 first = ch.cutBlock();

        vm.roll(block.number + 7);
        vm.prank(stranger);
        ch.recut(keccak256("again"));
        assertEq(ch.cutKey(), key);
        assertEq(ch.cutBlock(), first + 7, "the latest replacement's block");
    }

    /// A reserved key that gained a HyperCore account before the start can't become the
    /// challenge's agent. The challenge refuses to start, and anyone can abort it at once:
    /// the trader gets the price back and the capital goes back to the pool.
    function test_aSpoiledKey_blocksTheStart_andAbortRefunds() public {
        Pool p = _readyPool();
        uint64 poolSpot = _spot(address(p));
        ChallengeAccount ch = _buy(p);
        CoreSimulatorLib.nextBlock();
        assertFalse(ch.keySpoiled());
        CoreSimulatorLib.forceAccountActivation(ch.agentKey());
        assertTrue(ch.keySpoiled());
        assertTrue(ch.capitalArrived());

        vm.expectRevert(ChallengeAccount.KeySpoiled.selector);
        ch.activate();

        address key = ch.agentKey();
        vm.prank(stranger);
        ch.abort(); // inside the start window, with the capital there
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Aborted));
        assertEq(usdc.balanceOf(trader), 25e6, "price refunded");
        assertEq(uint8(registry.bindingOf(key).state), uint8(KeyRegistry.State.Retired));
        _settleChallenge(ch);
        assertEq(_spot(address(p)), poolSpot - Units.NEW_ACCOUNT_FEE, "capital back; the account fee is spent");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }

    /// A resting order holds margin, and nobody can list open orders on chain, so every
    /// settlement step accepts the orders to cancel, not just the stop.
    function test_settle_cancelsNamedOrdersEveryCall() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 80e6, 0);
        (Cancel[] memory none, uint32[] memory a) = _none();
        ch.breach(none, a, SALT);
        vm.clearMockedCalls();

        Cancel[] memory late = new Cancel[](2);
        late[0] = Cancel({asset: BTC, oid: 11});
        late[1] = Cancel({asset: BTC, oid: 12});
        for (uint256 round = 0; round < 2; ++round) {
            vm.recordLogs();
            ch.settle(late, a);
            (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
            assertEq(kind[0], 10);
            assertEq(args[0], abi.encode(BTC, uint64(11)));
            assertEq(kind[1], 10);
            assertEq(args[1], abi.encode(BTC, uint64(12)));
            CoreSimulatorLib.nextBlock();
        }
    }

    function test_abort_refusedOnceTheCapitalArrived() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 1 hours + 1);
        vm.prank(stranger);
        vm.expectRevert(ChallengeAccount.CapitalArrived.selector);
        ch.abort();
        ch.activate();
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
    }

    /// Settlement may be called every block while HyperCore is still executing the last
    /// send. The trader's share still goes out once.
    function test_payoutIsSentOnce() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        (Cancel[] memory c, uint32[] memory a) = _none();

        ch.settle(c, a); // perp -> spot
        CoreSimulatorLib.nextBlock();

        vm.recordLogs();
        ch.settle(c, a); // payout
        ch.settle(c, a); // same block: the spot balance still looks untouched
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        uint256 toTrader;
        uint256 toPool;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (from[i] != address(ch) || kind[i] != 6) continue;
            (address to,,) = abi.decode(args[i], (address, uint64, uint64));
            if (to == trader) ++toTrader;
            if (to == address(p)) ++toPool;
        }
        assertEq(toTrader, 1, "one payout send");
        assertEq(toPool, 0, "nothing to the pool before the payout lands");
        assertEq(ch.payoutSent(), ch.payoutOwed());

        _settleChallenge(ch);
        assertEq(_spot(trader), ch.payoutOwed());
    }

    /// After a funded stage closes, the passed challenge may still be settling. The pool
    /// must not sell a new challenge until it has, or the old one could never report back.
    function test_noNewChallengeWhileThePassedOneSettles() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();

        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        for (uint256 i = 0; i < 6 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            p.settleFunded(c, a);
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(p.challenge(), address(ch), "the passed challenge has not settled yet");

        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Idle));
        p.buyChallenge();
        vm.stopPrank();

        _settleChallenge(ch);
        assertEq(p.challenge(), address(0));
        _buy(p);
    }

    function test_expire_and_forfeit() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        (Cancel[] memory c, uint32[] memory a) = _none();

        vm.expectRevert(ChallengeAccount.TooEarly.selector);
        ch.expire(c, a, SALT);
        vm.prank(stranger);
        vm.expectRevert(ChallengeAccount.NotTrader.selector);
        ch.forfeit(c, a, SALT);

        vm.warp(block.timestamp + 7 days + 1);
        ch.expire(c, a, SALT);
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Expired));
        _settleChallenge(ch);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));

        ChallengeAccount ch2 = _started(p);
        vm.prank(trader);
        ch2.forfeit(c, a, SALT);
        assertEq(uint8(ch2.status()), uint8(ChallengeAccount.Status.Forfeited));
    }

    function test_abort_refundsTheTrader() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);
        address key = ch.agentKey();

        vm.expectRevert(ChallengeAccount.TooEarly.selector);
        ch.abort();

        vm.warp(block.timestamp + 1 hours + 1);
        ch.abort();
        assertEq(usdc.balanceOf(trader), 25e6, "refunded");
        assertEq(p.heldPrice(), 0);
        assertEq(uint8(registry.bindingOf(key).state), uint8(KeyRegistry.State.Retired));

        // The capital lands after the abort and goes straight back.
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }

    function test_buyChallenge_paysThePlatformFee_whichAnAbortKeeps() public {
        address feeTo = makeAddr("fee-recipient");
        vm.prank(operator);
        factory.setChallengeFee(3e6, feeTo);
        Pool p = _readyPool();

        deal(address(usdc), trader, 28e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(); // the price alone is approved, not the fee
        p.buyChallenge();
        usdc.approve(address(p), 28e6);
        ChallengeAccount ch = ChallengeAccount(p.buyChallenge());
        vm.stopPrank();
        assertEq(usdc.balanceOf(feeTo), 3e6);
        assertEq(usdc.balanceOf(address(p)), 25e6);

        vm.warp(block.timestamp + 1 hours + 1);
        ch.abort();
        assertEq(usdc.balanceOf(trader), 25e6, "the price comes back");
        assertEq(usdc.balanceOf(feeTo), 3e6, "the fee doesn't");
    }

    function test_setChallengeFee_operatorOnly_andNeedsARecipient() public {
        vm.prank(stranger);
        vm.expectRevert(PoolFactory.NotOperator.selector);
        factory.setChallengeFee(1, stranger);

        vm.startPrank(operator);
        vm.expectRevert(PoolFactory.BadFee.selector);
        factory.setChallengeFee(1, address(0));
        factory.setChallengeFee(0, address(0)); // switching it off needs no recipient
        vm.stopPrank();
        assertEq(factory.challengeFee(), 0);
    }

    // ── passing, and the funded stage ────────────────────────────────────────────────

    function _passed(Pool p) internal returns (ChallengeAccount ch, address oldKey) {
        ch = _started(p);
        oldKey = ch.agentKey();
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920); // +3%: about +11.46 USDC
        _trade(address(ch), BTC, false, 0.005e8); // flat again, profit realized
        ch.graduate(SALT);
    }

    /// A stranger can spoil every published key by giving it a HyperCore account, one USDC
    /// each. Before the sale reserved the funded key, that was enough to take the funded stage
    /// away from a trader who had already met the target: graduate reached into an empty
    /// registry and reverted, and after the deadline the trader could only expire -- no funded
    /// stage, no share, and the challenge's profit left in the pool. Now the key is already in
    /// hand when the trader passes, so nobody outside can reach it.
    function test_graduate_survivesAnEmptyRegistry_becauseTheSaleReservedTheKey() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);

        uint256 spoiled;
        for (uint256 i = 0; i < keys.length; ++i) {
            if (registry.bindingOf(keys[i]).state == KeyRegistry.State.Free) {
                CoreSimulatorLib.forceAccountActivation(keys[i]);
                ++spoiled;
            }
        }
        assertGt(spoiled, 0, "there was something left to spoil");

        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);

        // The whole assertion is behaviour: the trader who met the target IS funded, although
        // every key still on the free list was spoiled while the challenge ran.
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertTrue(registry.isBound(p.agentKey(), address(p), trader), "funded on a key of its own");

        // What the fix does NOT claim: the attack still stops NEW sales. Say so here, so that
        // nobody reads this test as proof the registry cannot be drained.
        Pool other = _readyPool();
        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(other), 25e6);
        vm.expectRevert(KeyRegistry.NoFreeKey.selector);
        other.buyChallenge();
        vm.stopPrank();
    }

    /// The sale takes two keys, and one of them is only needed if the trader passes. When
    /// nobody does, the pool gives it back -- otherwise the pool would still be holding it at
    /// the next sale, and a pool may hold only one.
    function test_aChallengeNobodyPassesGivesTheReservedKeyBack() public {
        Pool p = _readyPool();
        uint256 free0 = registry.freeCount();
        ChallengeAccount ch = _started(p);
        assertEq(registry.freeCount(), free0 - 2, "the challenge's own key, and one held for the pass");
        address reserved = p.reservedKey();

        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);
        _settleChallenge(ch);

        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(p.reservedKey(), address(0), "the pool is not still holding it");
        assertEq(uint8(registry.bindingOf(reserved).state), uint8(KeyRegistry.State.Retired));

        // The proof that it was really given back: the pool can sell again.
        _buy(p);
        assertTrue(p.reservedKey() != address(0) && p.reservedKey() != reserved);
    }

    /// Audit A-01. Every settlement step that sees ANY spot balance sends it to the pool and
    /// returns, and Settled is reached only when the balance is zero at the start of a block.
    /// So a stranger who tops the account up by one unit before each step holds the settlement
    /// open for as long as they care to pay the gas -- and with it the pool, because
    /// withdrawOnCore is Idle-only: the investor's capital cannot come out while this lasts.
    /// One unit is 1e-8 USDC, and after the first transfer the account exists, so there is not
    /// even an activation fee to pay.
    function test_settle_isNotHeldOpenByAStrangersDust() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);

        for (uint256 i = 0; i < 12 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            uint64 held = CoreOps.spotUsdc(address(ch));
            CoreSimulatorLib.forceSpotBalance(address(ch), 0, held + 1); // the stranger's dust
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
        }

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled),
            "a stranger's dust must not hold the settlement open");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle), "and the pool must come back to Idle");
    }

    /// The other half of that rule, and the one that keeps it honest: while the account's OWN
    /// return has not landed yet, finishing would leave real capital sitting in a settled
    /// account. Only money that turns up AFTER our send landed is swept and ignored.
    function test_settle_stillWaitsWhileItsOwnReturnHasNotLanded() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);

        for (uint256 i = 0; i < 8 && ch.returnSpotBefore() == 0; ++i) {
            ch.settle(new Cancel[](0), none);
            if (ch.returnSpotBefore() == 0) CoreSimulatorLib.nextBlock();
        }
        assertTrue(ch.returnSpotBefore() != 0, "the capital was sent back to the pool");
        assertGt(ch.returnSpotBefore(), 0);

        // Same block, so that send is still queued and the balance reads untouched.
        ch.settle(new Cancel[](0), none);
        assertTrue(ch.status() != ChallengeAccount.Status.Settled,
            "must not finish while its own capital is still sitting here");
    }

    /// Audit A-01, the pool's door, now closed. A stranger cannot reach the pool's spot
    /// balance -- that money is the pool's and it keeps it -- but they could push USDC into its
    /// perp account, which the closing step has to move across before the pool may return to
    /// Idle. The pool's capital only leaves in Idle (withdrawOnCore), so one unit a block held
    /// the investor's money for as long as the stranger cared to pay the gas.
    function test_settleFunded_isNotHeldOpenByAStrangersPerpDust() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        ch;
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));

        for (uint256 i = 0; i < 12 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.forcePerpBalance(address(p), 1); // the stranger's dust
            p.settleFunded(c, a);
            CoreSimulatorLib.nextBlock();
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle),
            "a stranger's perp dust must not hold the pool in Closing");
        assertEq(p.fundedDrainBlock(), 0, "and the drain block is cleared for the next cycle");
    }

    /// The other side of that rule. The pool's OWN closing proceeds need a block to reach spot,
    /// and finishing before they land would leave them on the perp account -- where nothing can
    /// move them, because Closing is the only stage that drains. So the first step still waits.
    function test_settleFunded_stillWaitsForItsOwnProceedsToReachSpot() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        ch;
        (Cancel[] memory c, uint32[] memory a) = _none();
        // Let the funded capital actually reach the perp side first: graduate only queues that
        // transfer, so without this the stage is stopped with nothing there to drain.
        CoreSimulatorLib.nextBlock();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();

        // Step until the proceeds start moving across. Nobody donates anything here, so the
        // only thing on the perp side is the pool's own money.
        for (uint256 i = 0; i < 8 && p.fundedDrainBlock() == 0; ++i) {
            p.settleFunded(c, a);
            if (p.fundedDrainBlock() == 0) CoreSimulatorLib.nextBlock();
        }
        assertTrue(p.fundedDrainBlock() != 0, "a step sent the perp side across and noted the block");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing),
            "and it did not finish while its own capital was still on the perp side");
    }

    /// Audit finding A-11: a regression I put into the pool's closing step and did not see.
    /// Closing the perp door, I replaced "wait for the perp side to reach spot" with a one-time
    /// FLAG. A flag records that something happened; it does not record WHEN. Precompiles answer
    /// with the start of the block, so a second settleFunded in the same block reads the same
    /// stale numbers -- and with the flag set it walked past the wait and paid the funded trader
    /// their share out of a balance that predated the drain. Once, because the payout marks
    /// itself done. In the audit's run: 9.232 USDC owed, 1e-8 sent.
    ///
    /// The scenario is theirs, unchanged; only the assertions differ, because theirs pinned the
    /// bug and these pin the fix. It lives here rather than in its own file: inheriting the
    /// harness would re-run every test in it a second time under another contract name, and a
    /// suite whose count means two different things is how numbers start drifting.
    function test_regression_twoStepsInOneBlock_payTheFundedShareFromAStaleSpot() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        CoreSimulatorLib.forceSpotBalance(griefer, 0, 1e8);

        // A pool holding exactly what one sale needs: after the pass, all of it is on perp.
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, p.capitalNeeded());
        p.prepareAccount();
        _passed(p); // the challenge's own return is left unsettled on purpose
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(p)), 0, "everything is on perp or in the challenge");

        // The funded trader makes a profit and stops the stage, no breach.
        _trade(address(p), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 810000);
        _trade(address(p), BTC, false, 0.005e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);

        // One unit lands on the pool's spot (anyone can send it; the investor has the motive).
        hyperCore.executeSpotSend(griefer, CoreState.SpotSendAction({destination: address(p), token: 0, _wei: 1}));
        CoreSimulatorLib.nextBlock();

        new TwoStepsInOneBlock().run(p);
        CoreSimulatorLib.nextBlock();

        assertGt(p.fundedPayoutOwed(), 1e8, "a real share is owed");
        assertFalse(p.fundedPayoutDone(), "two steps in one block must not spend the one payout");
        assertEq(p.fundedPayoutSent(), 0, "and nothing was paid out of the stale spot");

        // The next block has the drained capital on spot, so the share is paid in full.
        for (uint256 i = 0; i < 8 && !p.fundedPayoutDone(); ++i) {
            p.settleFunded(c, a);
            CoreSimulatorLib.nextBlock();
        }
        assertTrue(p.fundedPayoutDone(), "the payout happens on a later block");
        assertEq(p.fundedPayoutSent(), p.fundedPayoutOwed(), "and it is the whole share");
    }

    /// Audit's recheck of the first A-01 fix: the amount comparison alone could still be held
    /// open. When the account has nothing of its own to hand back, the first "return" IS the
    /// stranger's unit, and a unit of the same size after it is never "smaller than last time".
    /// The window is what ends it, and the stranger cannot restart the window.
    function test_settle_spotDust_whenTheOwnReturnIsTiny_noLongerHolds() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);
        vm.warp(block.timestamp + 1 hours + 1);
        ch.abort();
        uint32[] memory none = new uint32[](0);

        // The same unit before every step: under the old rule this ran for ever.
        for (uint256 i = 0; i < 20 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            uint64 held = CoreOps.spotUsdc(address(ch));
            CoreSimulatorLib.forceSpotBalance(address(ch), 0, held + 1);
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
            vm.warp(block.timestamp + 60);
        }
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled),
            "the window ends it even when no amount comparison can");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }

    /// The challenge's other door: a stranger pushing USDC onto its PERP account, which the
    /// step has to move across before it can finish. Same window, same reason.
    function test_settle_challengePerpDust_noLongerHolds() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);

        for (uint256 i = 0; i < 20 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            CoreSimulatorLib.forcePerpBalance(address(ch), 1);
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
            vm.warp(block.timestamp + 60);
        }
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled),
            "perp dust must not hold the challenge open either");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }

    /// Finishing on time is only safe because nothing is stranded by it. Whatever turns up on
    /// a settled account -- our own send that did not land, a late fill, someone's donation --
    /// goes home when anyone calls sweep().
    function test_sweep_sendsWhatArrivesAfterTheEndBackToThePool() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);
        _settleChallenge(ch);

        // A live challenge refuses: sweep is only for accounts that are finished with.
        ChallengeAccount fresh = _buy(p);
        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.BadStatus.selector, ChallengeAccount.Status.Created));
        fresh.sweep();
        // Its capital leaves the pool on the next block; measure after that, not before.
        CoreSimulatorLib.nextBlock();

        uint64 poolBefore = _spot(address(p));
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 5e8); // 5 USDC turns up afterwards
        ch.sweep();
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), 0, "the settled account is empty again");
        assertEq(_spot(address(p)), poolBefore + 5e8, "and the pool has it");
    }

    function test_graduate_needsTargetAndFlat() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.TargetNotMet.selector, int64(100e6), int256(108e6)));
        ch.graduate(SALT);

        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        vm.expectRevert(ChallengeAccount.NotFlat.selector);
        ch.graduate(SALT);
    }

    function test_graduate_fundsTraderWithANewKey_andPaysTheShare() public {
        CoreSimulatorLib.forceAccountActivation(trader); // a trader who already uses HyperCore
        Pool p = _readyPool();
        vm.recordLogs();
        (ChallengeAccount ch, address oldKey) = _passed(p);
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Passed));
        assertEq(uint8(registry.bindingOf(oldKey).state), uint8(KeyRegistry.State.Retired));

        address newKey = p.agentKey();
        assertTrue(newKey != address(0) && newKey != oldKey, "a new key, never the old one");
        assertTrue(registry.isBound(newKey, address(p), trader));
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.fundedTrader(), trader);
        assertEq(p.fundedStart(), int64(200e6));

        // The pool approved the new key and moved the funded capital to perp.
        bool sawAgent;
        bool sawPerp;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (from[i] != address(p)) continue;
            if (kind[i] == 9 && keccak256(args[i]) == keccak256(abi.encode(newKey, ""))) sawAgent = true;
            if (kind[i] == 7 && keccak256(args[i]) == keccak256(abi.encode(uint64(200e6), true))) sawPerp = true;
        }
        assertTrue(sawAgent && sawPerp);

        // Profit 0.005 * (78692.0 - 76400.0) = 11.46 USDC; 50% to the trader, 1e8 scale.
        assertEq(ch.payoutOwed(), 573000000);
        CoreSimulatorLib.nextBlock();
        assertEq(_equity(address(p)), int64(200e6));

        uint64 poolSpotBefore = _spot(address(p));
        _settleChallenge(ch);
        assertEq(_spot(trader), 573000000, "trader paid on HyperCore");
        assertEq(ch.payoutSent(), ch.payoutOwed());
        assertGt(_spot(address(p)), poolSpotBefore, "the rest went back to the pool");
        assertEq(p.challenge(), address(0));
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded), "the funded stage goes on");
    }

    /// A trader with no HyperCore account yet receives the share less the 1 USDC that creating
    /// the account costs; the challenge spends exactly the share.
    function test_payout_toATraderWithoutAnAccount_coversTheFeeFromTheShare() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        assertFalse(PrecompileLib.coreUserExists(trader));
        uint64 owed = ch.payoutOwed();
        int64 left = _equity(address(ch));
        uint64 poolSpotBefore = _spot(address(p));

        _settleChallenge(ch);
        assertEq(ch.payoutSent(), owed - Units.NEW_ACCOUNT_FEE);
        assertEq(_spot(trader), owed - Units.NEW_ACCOUNT_FEE, "share less the account fee");
        assertEq(_spot(address(p)), poolSpotBefore + uint64(left) * 100 - owed, "the pool gets the rest");
    }

    /// A share that the account fee would eat whole isn't sent; the challenge settles and
    /// everything goes back to the pool.
    function test_payout_smallerThanTheAccountFee_isNotSent() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 500; // 5% of 11.46 USDC: 0.573
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        assertEq(ch.payoutOwed(), 57300000);
        int64 left = _equity(address(ch));
        uint64 poolSpotBefore = _spot(address(p));

        _settleChallenge(ch);
        assertTrue(ch.payoutDone());
        assertEq(ch.payoutSent(), 0);
        assertFalse(PrecompileLib.coreUserExists(trader), "nothing was sent to the trader");
        assertEq(_spot(address(p)), poolSpotBefore + uint64(left) * 100, "all of it back in the pool");
    }

    function test_fundedBreach_closesAndReturnsToIdle() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        address fundedKey = p.agentKey();

        _trade(address(p), BTC, true, 0.01e8); // 787 USDC notional on 200: 3.9x
        CoreSimulatorLib.setMarkPx(BTC, 763310); // -3%
        _mockMargin(address(p), 176.4e6, 763e6); // Hyperliquid's view: -23.6 on 200
        assertEq(uint8(p.violation(new uint32[](0))), uint8(Breach.Drawdown));

        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(stranger);
        p.breach(c, a, SALT);
        vm.clearMockedCalls();
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));
        assertEq(uint8(p.fundedEndReason()), uint8(Breach.Drawdown));
        assertEq(p.fundedPayoutOwed(), 0, "no share after a breach");
        assertEq(uint8(registry.bindingOf(fundedKey).state), uint8(KeyRegistry.State.Retired));

        for (uint256 i = 0; i < 6 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            p.settleFunded(c, a);
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(PrecompileLib.position(address(p), BTC).szi, 0);
        assertEq(_equity(address(p)), 0, "perp balance back on spot");

        // The pool can be sold again, with a fresh key.
        ChallengeAccount next = _buy(p);
        assertTrue(next.agentKey() != fundedKey);
    }

    function test_stopFunded_byInvestor_paysTheShare() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);

        _trade(address(p), BTC, true, 0.01e8);
        CoreSimulatorLib.setMarkPx(BTC, 810520); // +3% from 78692.0: about +23.6
        (Cancel[] memory c, uint32[] memory a) = _none();

        vm.prank(stranger);
        vm.expectRevert(Pool.NotAllowed.selector);
        p.stopFunded(c, a, SALT);

        uint64 traderSpotBefore = _spot(trader);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        assertEq(p.fundedPayoutOwed(), 0, "not known while the position is open");
        assertFalse(p.fundedResultTaken());

        uint64 owed;
        for (uint256 i = 0; i < 8 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            p.settleFunded(c, a);
            if (p.fundedResultTaken() && owed == 0) owed = p.fundedPayoutOwed();
        }
        assertGt(owed, 0);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(_spot(trader) - traderSpotBefore, owed);
    }

    /// The share is computed from what closing realized. At the stop the position is marked
    /// at +40; the close fills at +5; the trader gets 80% of 5, and the investor doesn't pay
    /// for the gap.
    function test_fundedShare_comesFromWhatClosingRealized() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        (Cancel[] memory c, uint32[] memory a) = _none();

        _trade(address(p), BTC, true, 0.01e8);
        _mockMargin(address(p), 240e6, 787e6);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        assertEq(p.fundedStopEquity(), 240e6);
        assertEq(p.fundedPayoutOwed(), 0);

        CoreSimulatorLib.nextBlock(); // the reduce-only close executes
        assertEq(PrecompileLib.position(address(p), BTC).szi, 0);
        _mockMargin(address(p), 205e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();

        assertTrue(p.fundedResultTaken());
        assertEq(p.fundedResult(), 205e6);
        assertEq(p.fundedPayoutOwed(), 4e8, "80% of the realized 5 USDC, in 1e8 units");
    }

    /// The two shares are independent, and this is the pair the demo deploys: nothing for
    /// passing the audition, 80% of what the trader makes on the pool's own capital. The
    /// challenge pays zero and no HyperCore account is created for the trader by it; the
    /// funded stage then pays the full 80%.
    function test_shares_areIndependent_zeroOnTheChallenge_eightyOnTheFunded() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 0;
        t.traderShareFundedBps = 8000;
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();

        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        assertEq(ch.payoutOwed(), 0, "the challenge pays nothing at a zero challenge share");
        _settleChallenge(ch);
        assertEq(_spot(trader), 0, "and nothing reached the trader on HyperCore");
        assertFalse(PrecompileLib.coreUserExists(trader));

        (Cancel[] memory c, uint32[] memory a) = _none();
        _trade(address(p), BTC, true, 0.01e8);
        _mockMargin(address(p), 240e6, 787e6);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();
        _mockMargin(address(p), 205e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();
        assertEq(p.fundedPayoutOwed(), 4e8, "80% of the realized 5 USDC, in 1e8 units");
    }

    /// The mirror: the challenge share paid, the funded share zero. Reading one field where
    /// the other is meant would give this pool the same numbers as the one above.
    function test_shares_areIndependent_eightyOnTheChallenge_zeroOnTheFunded() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 8000;
        t.traderShareFundedBps = 0;
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();

        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        // Profit 0.005 * (78692.0 - 76400.0) = 11.46 USDC; 80% of it, 1e8 scale.
        assertEq(ch.payoutOwed(), 916800000, "the challenge pays its own rate");
        _settleChallenge(ch);

        (Cancel[] memory c, uint32[] memory a) = _none();
        _trade(address(p), BTC, true, 0.01e8);
        _mockMargin(address(p), 240e6, 787e6);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();
        _mockMargin(address(p), 205e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();
        assertEq(p.fundedResult(), 205e6, "the funded stage did realize a profit");
        assertEq(p.fundedPayoutOwed(), 0, "and none of it is owed at a zero funded share");
    }

    /// A position in an asset nobody named still shows in the account's notional. Settlement
    /// waits for it: nothing moves to spot and the result isn't taken.
    function test_settle_waitsWhileAnUnnamedPositionIsOpen() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);

        CoreSimulatorLib.nextBlock();
        _mockMargin(address(p), 200e6, 90e6); // an ETH position nobody named
        vm.recordLogs();
        p.settleFunded(c, a);
        (address[] memory from, uint24[] memory kind,) = _actions(vm.getRecordedLogs());
        for (uint256 i = 0; i < kind.length; ++i) {
            assertFalse(from[i] == address(p) && kind[i] == 7, "nothing may move to spot");
        }
        assertFalse(p.fundedResultTaken());

        _mockMargin(address(p), 200e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();
        assertTrue(p.fundedResultTaken());
    }

    /// An asset whose size decimals leave no room for a price refuses by name instead of
    /// underflowing.
    function test_close_refusesAnAssetWithTooManySizeDecimals() public {
        uint32 odd = 9;
        hyperCore.registerPerpAssetInfo(
            odd,
            PrecompileLib.PerpAssetInfo({coin: "ODD", marginTableId: 1, szDecimals: 7, maxLeverage: 3, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(odd, 1);
        CloseHarness h = new CloseHarness();
        _mockPosition(address(h), odd, 5);
        uint32[] memory perps = new uint32[](1);
        perps[0] = odd;
        vm.expectRevert(abi.encodeWithSelector(CoreOps.UnsupportedSizeDecimals.selector, odd, uint8(7)));
        h.close(perps);
    }

    /// The pool goes back to idle only after the funded trader's payout has left its balance,
    /// or a new challenge could be sold against money already on its way out.
    function test_poolStaysClosingUntilTheFundedPayoutLands() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        uint64 traderSpotBefore = _spot(trader);

        _trade(address(p), BTC, true, 0.01e8);
        CoreSimulatorLib.setMarkPx(BTC, 810520);
        _trade(address(p), BTC, false, 0.01e8); // profit realized, flat
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);

        p.settleFunded(c, a); // result taken, perp -> spot
        assertGt(p.fundedPayoutOwed(), 0);
        CoreSimulatorLib.nextBlock();
        vm.recordLogs();
        p.settleFunded(c, a); // payout sent
        uint64 sent = p.fundedPayoutSent();
        assertGt(sent, 0);
        p.settleFunded(c, a); // same block: not landed yet
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        uint256 toTrader;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (from[i] != address(p) || kind[i] != 6) continue;
            (address to,,) = abi.decode(args[i], (address, uint64, uint64));
            if (to == trader) ++toTrader;
        }
        assertEq(toTrader, 1, "one payout send");

        CoreSimulatorLib.nextBlock();
        p.settleFunded(c, a);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(_spot(trader) - traderSpotBefore, sent, "the share arrived once");
    }

    /// The funded share goes to a trader who still has no HyperCore account (the challenge
    /// share was too small to send): it arrives less the 1 USDC for creating the account.
    function test_fundedPayout_toATraderWithoutAnAccount_coversTheFee() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 500;
        t.traderShareFundedBps = 500;
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        assertFalse(PrecompileLib.coreUserExists(trader));

        _trade(address(p), BTC, true, 0.01e8);
        CoreSimulatorLib.setMarkPx(BTC, 810520);
        _trade(address(p), BTC, false, 0.01e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);
        for (uint256 i = 0; i < 8 && p.stage() != Pool.Stage.Idle; ++i) {
            p.settleFunded(c, a);
            if (p.fundedPayoutDone() && p.fundedPayoutSent() != 0) {
                assertEq(p.fundedPayoutSent(), p.fundedPayoutOwed() - Units.NEW_ACCOUNT_FEE);
            }
            CoreSimulatorLib.nextBlock();
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        // The mark was 78692.0 after the challenge: 0.01 * (81052 - 78692) = 23.60 USDC realized,
        // 5% of it is 1.18, and the account fee takes 1 of that.
        assertEq(_spot(trader), 18000000);
    }

    // ── access ───────────────────────────────────────────────────────────────────────

    function test_access() public {
        Pool p = _readyPool();
        vm.startPrank(stranger);
        vm.expectRevert(Pool.NotChallenge.selector);
        p.onChallengeStarted();
        vm.expectRevert(Pool.NotChallenge.selector);
        p.onChallengePassed(stranger);
        vm.expectRevert(Pool.NotOwner.selector);
        p.withdrawOnCore(1e8);
        vm.expectRevert(PoolFactory.NotPool.selector);
        factory.createChallenge(stranger);
        vm.stopPrank();

        _buy(p);
        vm.prank(investor);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Challenge));
        p.withdrawOnCore(1e8);

        vm.prank(investor);
        p.withdrawEarned();
        assertEq(usdc.balanceOf(investor), 0, "nothing earned before the challenge starts");
    }
}
