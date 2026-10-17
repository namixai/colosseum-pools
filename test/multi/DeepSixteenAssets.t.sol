// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";

import {Rules, Terms, Cancel, Breach, Units} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {ChallengeAccount} from "../../src/ChallengeAccount.sol";
import {RuledAccount} from "../../src/RuledAccount.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {MultiPool, Plan, Size, AfterTarget} from "../../src/multi/MultiPool.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The deep audit's F11 (16 October 2026), turned round: a pool that lists the most assets it may
/// still closes a position in one it does not list, by naming it; the extra assets are bounded
/// apart from the list. On f8bc60aa naming the 17th asset reverted TooManyAssets, and without it
/// the stop closed nothing and settlement waited for ever. The stand is the audit's (Assets16Lock).
contract DeepSixteenAssetsTest is PoolHarness {
    /// The 17th asset: listed by the platform (the harness registers ETH with a mark), not by
    /// the pool. A position in it is what `violation(extraAssets)` calls ForbiddenAsset.
    uint32 constant ODD = ETH;
    uint32 constant UP = 786920; // BTC +3%


    uint32[] sixteen;
    PaperExam examImpl;
    address alice = makeAddr("alice");

    // ── stand ────────────────────────────────────────────────────────────────────────

    /// Registers 15 more perps on the simulator, lists them on the platform, and keeps BTC plus
    /// those 15 as the pool's list: Units.MAX_ASSETS exactly, which the factory accepts.
    function _listAssets(uint256 count) internal {
        delete sixteen;
        sixteen.push(BTC);
        uint32[] memory fresh = new uint32[](count - 1);
        for (uint32 i = 0; i < count - 1; ++i) {
            uint32 perp = 100 + i;
            hyperCore.registerPerpAssetInfo(
                perp,
                PrecompileLib.PerpAssetInfo({
                    coin: "X", marginTableId: 1, szDecimals: 4, maxLeverage: 10, onlyIsolated: false
                })
            );
            CoreSimulatorLib.setMarkPx(perp, 100000);
            fresh[i] = perp;
            sixteen.push(perp);
        }
        vm.startPrank(operator);
        factory.setPlatformAssets(fresh, true);
        factory.setFundedAssets(fresh, true);
        vm.stopPrank();
        assertEq(sixteen.length, count);
    }

    function _rulesN() internal view returns (Rules memory r) {
        r = Rules({dailyLossBps: 500, maxDrawdownBps: 1000, maxLeverageX100: 500, assets: sixteen});
    }

    function _readyPoolN() internal returns (Pool p) {
        vm.prank(investor);
        p = Pool(factory.createPool(_rulesN(), _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
    }

    function _odd() internal pure returns (uint32[] memory a) {
        a = new uint32[](1);
        a[0] = ODD;
    }

    /// Whether the logs hold `UnnamedPositionOpen` from `who`, and how many closing orders
    /// (CoreWriter action 1, a limit order) `who` sent.
    function _scan(Vm.Log[] memory logs, address who) internal pure returns (bool unnamed, uint256 closingOrders) {
        bytes32 sig = keccak256("UnnamedPositionOpen(uint64)");
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter == who && logs[i].topics[0] == sig) unnamed = true;
        }
        (address[] memory from, uint24[] memory kind,) = _actions(logs);
        for (uint256 i = 0; i < kind.length; ++i) {
            if (from[i] == who && kind[i] == 1) ++closingOrders;
        }
    }

    // ── the finding, turned round ────────────────────────────────────────────────────

    /// A challenge on a pool of 16 assets: the breach names the 17th, a closing order goes out, and
    /// settlement finishes.
    function test_deep_F11_aChallengeOfSixteenAssetsClosesASeventeenth() public {
        _listAssets(Units.MAX_ASSETS);
        Pool p = _readyPoolN();
        ChallengeAccount ch = _started(p);
        assertEq(ch.rules().assets.length, 16);
        _mockPosition(address(ch), ODD, 100);
        _mockMargin(address(ch), 100e6, 24e6);
        (Cancel[] memory none,) = _none();
        CoreSimulatorLib.setRevertOnFailure(false);
        vm.recordLogs();
        ch.breach(none, _odd(), SALT);
        (, uint256 closing) = _scan(vm.getRecordedLogs(), address(ch));
        assertEq(closing, 1, "a closing order for the 17th asset goes out");
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Breached));
        vm.clearMockedCalls();
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle), "the pool's capital is free again");
    }

    /// A funded stage on a pool of 16 assets: the breach names the 17th and closes it.
    function test_deep_F11_aFundedStageOfSixteenAssetsClosesASeventeenth() public {
        _listAssets(Units.MAX_ASSETS);
        Pool p = _readyPoolN();
        vm.startPrank(investor);
        p.runByOwner(false);
        p.openFundedFor(trader);
        vm.stopPrank();
        CoreSimulatorLib.nextBlock();
        _mockPosition(address(p), ODD, 100);
        _mockMargin(address(p), 200e6, 48e6);
        (Cancel[] memory none,) = _none();
        CoreSimulatorLib.setRevertOnFailure(false);
        vm.recordLogs();
        p.breach(none, _odd(), SALT);
        (, uint256 closing) = _scan(vm.getRecordedLogs(), address(p));
        assertEq(closing, 1);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));
    }

    // ── 3. a seat of a MultiPool ─────────────────────────────────────────────────────

    function _examImpl() internal returns (address) {
        if (address(examImpl) == address(0)) examImpl = new PaperExam();
        return address(examImpl);
    }

    function _plan16() internal view returns (Plan memory p) {
        p.menu = new Size[](1);
        p.menu[0] = Size({size: 5e6, price: 1});
        p.examRules = _rules();
        uint16[] memory targets = new uint16[](1);
        targets[0] = 1000;
        p.exam = ExamTerms({
            capital: 0,
            targetsBps: targets,
            stepDuration: 7 days,
            maxIdle: 0,
            minTradingDays: 0,
            costPpm: 0,
            fillDelay: 1,
            fillWindow: 5
        });
        p.fundedRules = _rulesN(); // the funded accounts list the full 16
        p.fundedTargetBps = 1000;
        p.traderShareBps = 8000;
        p.afterTarget = AfterTarget.AccountCloses;
        p.inWorkBps = 8000;
        p.maxSeats = 8;
        p.claimWindow = 7 days;
        p.term = 365 days;
        p.fundedIdle = 30 days;

        p.termsBook = TERMS_BOOK;
    }

    function _planOf(MultiPool m) internal view returns (Plan memory) {
        return abi.decode(CodeStore.read(m.planData()), (Plan));
    }

    function _buyExam(MultiPool m, address who, uint256 line) internal returns (PaperExam e) {
        uint256 price = _planOf(m).menu[line].price + factory.challengeFee();
        deal(address(usdc), who, price);
        vm.startPrank(who);
        usdc.approve(address(m), price);
        e = PaperExam(m.buy(line));
        vm.stopPrank();
    }

    /// Four times the exam's balance long, 3% up, closed: about 12%, past the 10% target.
    function _pass(PaperExam e, address who) internal {
        (,, uint64 capital,) = e.result();
        uint64 sz = (capital * 4) / BTC_MARK;
        vm.prank(who);
        e.place(BTC, true, sz);
        vm.roll(block.number + 1);
        e.fill();
        CoreSimulatorLib.setMarkPx(BTC, UP);
        vm.prank(who);
        e.place(BTC, false, sz);
        vm.roll(block.number + 1);
        e.fill();
        vm.prank(who);
        e.graduate();
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Passed));
    }

    function _fund(MultiPool m, address who, uint256 line) internal returns (Pool seat) {
        PaperExam e = _buyExam(m, who, line);
        _pass(e, who);
        vm.prank(who);
        seat = Pool(m.claim(address(e)));
        CoreSimulatorLib.nextBlock();
        m.open(address(seat));
        CoreSimulatorLib.nextBlock();
    }

    /// A seat of a MultiPool of 16 assets: the breach names the 17th and closes it.
    function test_deep_F11_aSeatOfSixteenAssetsClosesASeventeenth() public {
        _listAssets(Units.MAX_ASSETS);
        CoreSimulatorLib.forceAccountActivation(investor);
        MultiPool m = new MultiPool(investor, factory, _examImpl(), _tie(_plan16()), 180 days, 730 days, 14 days, 90 days);
        CoreSimulatorLib.forceSpotBalance(address(m), 0, 100e8);
        vm.prank(investor);
        m.begin(TERMS);
        Pool seat = _fund(m, alice, 0);
        assertEq(seat.rules().assets.length, 16);
        _mockPosition(address(seat), ODD, 100);
        _mockMargin(address(seat), 5e6, 1e6);
        (Cancel[] memory none,) = _none();
        CoreSimulatorLib.setRevertOnFailure(false);
        vm.recordLogs();
        seat.breach(none, _odd(), SALT);
        (, uint256 closing) = _scan(vm.getRecordedLogs(), address(seat));
        assertEq(closing, 1);
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Closing));
    }

    // ── control: one asset fewer and the same position closes ────────────────────────

    function test_control_fifteenAssets_oddPositionIsClosedByName() public {
        _listAssets(Units.MAX_ASSETS - 1);
        Pool p = _readyPoolN();
        ChallengeAccount ch = _started(p);
        assertEq(ch.rules().assets.length, 15);
        _mockPosition(address(ch), ODD, 100);
        _mockMargin(address(ch), 100e6, 24e6);
        (Cancel[] memory none,) = _none();
        uint32[] memory odd = _odd();

        // With a slot left in _assetsWith the breach can be pulled with the asset named...
        // (the position is a precompile mock, not a simulator position, so the simulator cannot
        // fill the closing order; the order's bytes still go to CoreWriter, which is the claim)
        CoreSimulatorLib.setRevertOnFailure(false);
        vm.recordLogs();
        ch.breach(none, odd, SALT);
        (, uint256 closing) = _scan(vm.getRecordedLogs(), address(ch));
        assertEq(closing, 1, "a closing order for the 17th asset goes out");
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Breached));
        // ...and once it fills (the mocks come off), settlement proceeds.
        vm.clearMockedCalls();
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }
}
