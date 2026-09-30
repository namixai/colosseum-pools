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

/// The stand every pools test runs on: the simulator, a factory, a registry, and the
/// helpers that drive them. It holds no tests of its own, so a suite that inherits it does
/// not re-run somebody else's -- which is what happened the first time an audit regression
/// test was given its own file, and turned one suite count into two different numbers.
contract PoolHarness is Test {
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






    // ── buying and starting ──────────────────────────────────────────────────────────




    // ── rules ────────────────────────────────────────────────────────────────────────


    /// A minute past the next UTC midnight, inside the snapshot window.
    function _nextMidnight() internal {
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 60);
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













    // ── passing, and the funded stage ────────────────────────────────────────────────

    function _passed(Pool p) internal returns (ChallengeAccount ch, address oldKey) {
        ch = _started(p);
        oldKey = ch.agentKey();
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920); // +3%: about +11.46 USDC
        _trade(address(ch), BTC, false, 0.005e8); // flat again, profit realized
        ch.graduate(SALT);
        // Since audit A-02 the pass and the funding are two calls: graduate records that the
        // trader passed whatever the key registry looks like, and this opens the stage.
        p.openFundedStage();
    }













    /// Makes the margin precompile report money on the perp side while `withdrawable` -- a
    /// different precompile, left real -- still reads zero. That is what a resting limit order
    /// looks like from a contract: the money is there and it cannot be taken out.
    /// A pool in Closing whose result is taken and which holds LESS than the trader's share --
    /// a loss after the stop -- with `perp1e6` left crossing. Written by the audit for A-12's
    /// funded half.
    function _fundedClosingShort(uint64 spot1e8, uint64 perp1e6) internal returns (Pool p, uint64 owed) {
        CoreSimulatorLib.forceAccountActivation(trader);
        p = _readyPool();
        _passed(p);
        CoreSimulatorLib.nextBlock();
        _trade(address(p), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 810000);
        _trade(address(p), BTC, false, 0.005e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();
        p.settleFunded(c, a); // takes the result and starts the drain
        CoreSimulatorLib.nextBlock();
        owed = p.fundedPayoutOwed();
        CoreSimulatorLib.forceSpotBalance(address(p), 0, spot1e8);
        CoreSimulatorLib.forcePerpBalance(address(p), perp1e6);
    }

    function _mockHeldMargin(address account, int64 accountValue) internal {
        vm.mockCall(
            address(0x080F),
            abi.encode(uint32(0), account),
            abi.encode(PrecompileLib.AccountMarginSummary({
                accountValue: accountValue, marginUsed: uint64(int64(accountValue)), ntlPos: 0, rawUsd: accountValue
            }))
        );
    }

















    // ── access ───────────────────────────────────────────────────────────────────────

}
