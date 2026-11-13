// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test, Vm} from "forge-std/Test.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";

import {Rules, Breach} from "../../src/Types.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {OracleFollowsMark} from "../PoolHarness.sol";

/// The exam on the hyper-evm-lib simulator, offline. The simulator serves the mark and the asset
/// info; everything else is the exam's own arithmetic.
///
/// The numbers are round on purpose. BTC stands at 100 000.0 and the exam starts with 100 USDC,
/// so a position of 100 size units (0.001 BTC) is 100 USDC of notional, 1x, and every 1% of
/// price is 1 USDC of equity.
contract PaperExamTest is Test {
    uint32 constant BTC = 3;
    uint32 constant ETH = 4;
    uint32 constant UNLISTED = 9;
    uint64 constant MARK = 1_000_000; // 100000.0, one decimal (szDecimals 5)
    uint64 constant ETH_MARK = 250_000; // 2500.00, two decimals (szDecimals 4)
    int256 constant CAPITAL = 100e6;
    uint16 constant DELAY = 2;
    uint16 constant WINDOW = 3;

    HyperCore hyperCore;
    PaperExam impl;

    address trader = makeAddr("trader");
    address bot = makeAddr("bot");
    address stranger = makeAddr("stranger");

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        // The exam reads the oracle price; the simulator keeps none, so here it follows the mark.
        vm.etch(0x0000000000000000000000000000000000000807, type(OracleFollowsMark).runtimeCode);
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
        CoreSimulatorLib.setMarkPx(BTC, MARK);
        CoreSimulatorLib.setMarkPx(ETH, ETH_MARK);
        // Noon of some UTC day: far from the checkpoint window on both sides.
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);
        impl = new PaperExam();
    }

    // ── helpers ──────────────────────────────────────────────

    function _rules() internal pure returns (Rules memory r) {
        uint32[] memory assets = new uint32[](2);
        assets[0] = BTC;
        assets[1] = ETH;
        r = Rules({dailyLossBps: 500, maxDrawdownBps: 1000, maxLeverageX100: 500, assets: assets, assetLeverageX100: new uint32[](0)});
    }

    function _targets(uint16 a) internal pure returns (uint16[] memory t) {
        t = new uint16[](1);
        t[0] = a;
    }

    function _targets(uint16 a, uint16 b) internal pure returns (uint16[] memory t) {
        t = new uint16[](2);
        t[0] = a;
        t[1] = b;
    }

    /// One step of 10%, a week, no cost: the arithmetic of the other tests is exact.
    function _terms() internal pure returns (ExamTerms memory) {
        return ExamTerms({
            capital: 100e6,
            targetsBps: _targets(1000),
            stepDuration: 7 days,
            maxIdle: 0,
            minTradingDays: 0,
            costPpm: 0,
            fillDelay: DELAY,
            fillWindow: WINDOW
        });
    }

    function _create(Rules memory r, ExamTerms memory t) internal returns (PaperExam e) {
        e = PaperExam(Clones.clone(address(impl)));
        e.initialize(trader, r, t);
    }

    /// HIP-3 (the pools team, 21 October 2026): a builder dex's market is priced at the index the precompiles
    /// take, its order id less 100000, and its size decimals are checked there; read at the order id,
    /// the precompiles would answer for another dex's market (spike q9).
    function test_exam_aBuilderDexMarketIsPricedAndCheckedAtItsIndex() public {
        uint32 xyz = 110_000;
        hyperCore.registerPerpAssetInfo(
            10_000,
            PrecompileLib.PerpAssetInfo({coin: "xyz:XYZ100", marginTableId: 30, szDecimals: 4, maxLeverage: 30, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(10_000, 2_933_300); // 29,333.00; the oracle here follows it
        Rules memory r = _rules();
        r.assets[1] = xyz;
        PaperExam e = _create(r, _terms());
        _trade(e, xyz, true, 30); // 0.0030
        CoreSimulatorLib.setMarkPx(10_000, 2_934_300); // +10.00
        assertEq(e.equity(), CAPITAL + 30 * 1000, "0.0030 up 10.00: 0.03 USDC");
        hyperCore.registerPerpAssetInfo(
            10_001,
            PrecompileLib.PerpAssetInfo({coin: "xyz:FINE", marginTableId: 30, szDecimals: 7, maxLeverage: 30, onlyIsolated: false})
        );
        r.assets[1] = 110_001;
        vm.expectRevert(abi.encodeWithSelector(PaperExam.UnsupportedSizeDecimals.selector, uint32(110_001), uint8(7)));
        e.check(r, _terms());
    }

    function _exam() internal returns (PaperExam) {
        return _create(_rules(), _terms());
    }

    function _place(PaperExam e, uint32 asset, bool isBuy, uint64 sz) internal {
        vm.prank(trader);
        e.place(asset, isBuy, sz);
    }

    /// An order filled at the first block that takes it, by somebody who is not the trader.
    function _trade(PaperExam e, uint32 asset, bool isBuy, uint64 sz) internal {
        _place(e, asset, isBuy, sz);
        vm.roll(block.number + DELAY);
        vm.prank(stranger);
        e.fill();
    }

    function _graduate(PaperExam e) internal {
        vm.prank(trader);
        e.graduate();
    }

    function _mark(uint64 px) internal {
        CoreSimulatorLib.setMarkPx(BTC, px);
    }

    function _nextMidnight() internal {
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 60);
    }

    function _status(PaperExam e) internal view returns (uint8) {
        return uint8(e.status());
    }

    // ── creation ─────────────────────────────────────────────

    function test_exam_startsActiveOnItsBalance() public {
        PaperExam e = _exam();
        assertEq(_status(e), uint8(PaperExam.Status.Active));
        assertEq(e.pool(), address(this), "the creator is the pool");
        assertEq(e.trader(), trader);
        assertEq(e.cash(), CAPITAL);
        assertEq(e.equity(), CAPITAL);
        assertEq(e.dayStartEquity(), CAPITAL);
        assertEq(e.day(), uint32(block.timestamp / 1 days));
        assertEq(e.deadline(), block.timestamp + 7 days);
        assertEq(e.step(), 0);
        (PaperExam.Status s, address who, uint64 capital, uint64 passedAt) = e.result();
        assertEq(uint8(s), uint8(PaperExam.Status.Active));
        assertEq(who, trader);
        assertEq(capital, 100e6);
        assertEq(passedAt, 0);
        assertEq(e.terms().targetsBps.length, 1);
        assertEq(e.rules().assets.length, 2);
    }

    function test_exam_cannotBeStartedTwice_norTheImplementationAtAll() public {
        PaperExam e = _exam();
        vm.expectRevert(Initializable.InvalidInitialization.selector);
        e.initialize(stranger, _rules(), _terms());
        vm.expectRevert(Initializable.InvalidInitialization.selector);
        impl.initialize(trader, _rules(), _terms());
        assertEq(uint8(impl.status()), uint8(PaperExam.Status.None));
    }

    function test_exam_noDeadlineWhenTheStepHasNoDuration() public {
        ExamTerms memory t = _terms();
        t.stepDuration = 0;
        PaperExam e = _create(_rules(), t);
        assertEq(e.deadline(), 0);
        vm.warp(block.timestamp + 400 days);
        vm.expectRevert(PaperExam.NotOver.selector);
        e.expire();
        _trade(e, BTC, true, 100);
        assertEq(e.positionOf(BTC), 100, "an exam with no deadline still trades a year on");
    }

    function _expectBadTerms(ExamTerms memory t) internal {
        PaperExam e = PaperExam(Clones.clone(address(impl)));
        vm.expectRevert(PaperExam.BadTerms.selector);
        e.initialize(trader, _rules(), t);
    }

    function test_exam_terms_capitalAndTrader() public {
        ExamTerms memory t = _terms();
        t.capital = 0;
        _expectBadTerms(t);
        PaperExam e = PaperExam(Clones.clone(address(impl)));
        vm.expectRevert(PaperExam.BadTerms.selector);
        e.initialize(address(0), _rules(), _terms());
    }

    function test_exam_terms_steps() public {
        ExamTerms memory t = _terms();
        t.targetsBps = new uint16[](0);
        _expectBadTerms(t);
        t.targetsBps = new uint16[](5);
        for (uint256 i = 0; i < 5; ++i) {
            t.targetsBps[i] = 500;
        }
        _expectBadTerms(t);
        t.targetsBps = _targets(1000, 0);
        _expectBadTerms(t);
        t.targetsBps = _targets(10_001);
        _expectBadTerms(t);
        // Four steps, and a target of exactly 100%, are the edges that are allowed.
        t.targetsBps = new uint16[](4);
        for (uint256 i = 0; i < 4; ++i) {
            t.targetsBps[i] = 10_000;
        }
        assertEq(_create(_rules(), t).terms().targetsBps.length, 4);
    }

    function test_exam_terms_costAndFillBlocks() public {
        ExamTerms memory t = _terms();
        t.costPpm = 100_001;
        _expectBadTerms(t);
        t = _terms();
        t.fillDelay = 0;
        _expectBadTerms(t);
        t.fillDelay = 601;
        _expectBadTerms(t);
        t = _terms();
        t.fillWindow = 0;
        _expectBadTerms(t);
        t.fillWindow = 601;
        _expectBadTerms(t);
        t = _terms();
        t.costPpm = 100_000;
        t.fillDelay = 600;
        t.fillWindow = 600;
        assertEq(_create(_rules(), t).terms().costPpm, 100_000);
    }

    function test_exam_terms_daysMustFitTheStep() public {
        ExamTerms memory t = _terms();
        // A week can touch eight UTC days and no more.
        t.minTradingDays = 9;
        _expectBadTerms(t);
        t.minTradingDays = 8;
        assertEq(_create(_rules(), t).terms().minTradingDays, 8);
        // With no deadline any number of days fits.
        t.stepDuration = 0;
        t.minTradingDays = 400;
        assertEq(_create(_rules(), t).terms().minTradingDays, 400);
    }

    /// The same answer as `initialize` gives, asked of the implementation and without starting anything.
    function test_exam_check_saysWhatInitializeWouldSay() public {
        impl.check(_rules(), _terms());
        ExamTerms memory t = _terms();
        t.fillDelay = 0;
        vm.expectRevert(PaperExam.BadTerms.selector);
        impl.check(_rules(), t);
        Rules memory r = _rules();
        r.maxLeverageX100 = 99;
        vm.expectRevert(PaperExam.BadRules.selector);
        impl.check(r, _terms());
    }

    function _expectBadRules(Rules memory r) internal {
        PaperExam e = PaperExam(Clones.clone(address(impl)));
        vm.expectRevert(PaperExam.BadRules.selector);
        e.initialize(trader, r, _terms());
    }

    function test_exam_rules_bounds() public {
        Rules memory r = _rules();
        r.dailyLossBps = 0;
        _expectBadRules(r);
        r.dailyLossBps = 10_000;
        _expectBadRules(r);
        r = _rules();
        r.maxDrawdownBps = 0;
        _expectBadRules(r);
        r.maxDrawdownBps = 10_000;
        _expectBadRules(r);
        r = _rules();
        r.maxLeverageX100 = 99;
        _expectBadRules(r);
        r.maxLeverageX100 = 1001;
        _expectBadRules(r);
        r.maxLeverageX100 = 1000;
        assertEq(_create(r, _terms()).rules().maxLeverageX100, 1000);
        r = _rules();
        r.assets = new uint32[](0);
        _expectBadRules(r);
        r.assets = new uint32[](17);
        _expectBadRules(r);
        r.assets = new uint32[](2);
        r.assets[0] = BTC;
        r.assets[1] = BTC;
        _expectBadRules(r);
        r = _rules();
        r.dailyLossBps = 9999;
        r.maxDrawdownBps = 9999;
        r.maxLeverageX100 = 100;
        assertEq(_create(r, _terms()).rules().maxLeverageX100, 100);
    }

    function test_exam_rules_refusesAnAssetWhoseSizeItCannotCount() public {
        hyperCore.registerPerpAssetInfo(
            UNLISTED,
            PrecompileLib.PerpAssetInfo({
                coin: "ODD", marginTableId: 1, szDecimals: 7, maxLeverage: 3, onlyIsolated: false
            })
        );
        Rules memory r = _rules();
        r.assets[1] = UNLISTED;
        PaperExam e = PaperExam(Clones.clone(address(impl)));
        vm.expectRevert(abi.encodeWithSelector(PaperExam.UnsupportedSizeDecimals.selector, UNLISTED, uint8(7)));
        e.initialize(trader, r, _terms());
    }

    // ── orders ───────────────────────────────────────────────

    function test_exam_place_onlyTheTraderOrTheAgent() public {
        PaperExam e = _exam();
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotTrader.selector);
        e.place(BTC, true, 100);

        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotTrader.selector);
        e.setAgent(stranger);

        vm.prank(trader);
        e.setAgent(bot);
        vm.prank(bot);
        e.place(BTC, true, 100);
        (uint32 asset, bool isBuy, uint64 sz, uint64 placedBlock) = e.pending();
        assertEq(asset, BTC);
        assertTrue(isBuy);
        assertEq(sz, 100);
        assertEq(placedBlock, block.number);

        // Taken back, the agent is a stranger again.
        vm.roll(block.number + DELAY + WINDOW);
        vm.prank(trader);
        e.setAgent(address(0));
        vm.prank(bot);
        vm.expectRevert(PaperExam.NotTrader.selector);
        e.place(BTC, true, 100);
    }

    function test_exam_place_refusesAnAssetOutsideTheListAndAnEmptyOrder() public {
        PaperExam e = _exam();
        vm.startPrank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.AssetNotAllowed.selector, UNLISTED));
        e.place(UNLISTED, true, 100);
        vm.expectRevert(PaperExam.ZeroSize.selector);
        e.place(BTC, true, 0);
        vm.stopPrank();
    }

    function test_exam_place_oneOrderWaitsAtATime_andALapsedOneIsReplaced() public {
        PaperExam e = _exam();
        _place(e, BTC, true, 100);
        // Through the last block of its window the order still stands.
        vm.roll(block.number + DELAY + WINDOW - 1);
        vm.prank(trader);
        vm.expectRevert(PaperExam.OrderPending.selector);
        e.place(BTC, false, 50);
        // One block later it has lapsed, and the next order takes its place.
        vm.roll(block.number + 1);
        _place(e, BTC, false, 50);
        (, bool isBuy, uint64 sz,) = e.pending();
        assertFalse(isBuy);
        assertEq(sz, 50);
        assertEq(e.positionOf(BTC), 0, "the lapsed order never traded");
    }

    function test_exam_fill_notInTheOrdersOwnBlockNorBeforeTheDelay() public {
        PaperExam e = _exam();
        vm.expectRevert(PaperExam.NoOrder.selector);
        e.fill();
        _place(e, BTC, true, 100);
        uint256 from = block.number + DELAY;
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooEarly.selector, from));
        e.fill();
        vm.roll(from - 1);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooEarly.selector, from));
        e.fill();
        vm.roll(from);
        e.fill();
        assertEq(e.positionOf(BTC), 100);
    }

    function test_exam_fill_lapsesAfterItsWindow() public {
        PaperExam e = _exam();
        _place(e, BTC, true, 100);
        uint256 from = block.number + DELAY;
        // The last block of the window still fills.
        uint256 snap = vm.snapshotState();
        vm.roll(from + WINDOW - 1);
        e.fill();
        assertEq(e.positionOf(BTC), 100);
        vm.revertToState(snap);
        // The block after it does not.
        vm.roll(from + WINDOW);
        vm.expectRevert(PaperExam.OrderLapsed.selector);
        e.fill();
    }

    /// The point of the delay: the price is the filling block's, not the one the trader saw.
    function test_exam_fill_takesThePriceOfTheBlockThatFillsIt() public {
        PaperExam e = _exam();
        _place(e, BTC, true, 100);
        _mark(1_010_000);
        vm.roll(block.number + DELAY);
        vm.prank(stranger);
        e.fill();
        // 100 units bought at 101 000.0 cost 101 USDC; at that same mark equity is unchanged.
        assertEq(e.cash(), CAPITAL - 101e6);
        assertEq(e.equity(), CAPITAL);
        (,, uint64 sz,) = e.pending();
        assertEq(sz, 0, "a filled order is gone");
        vm.expectRevert(PaperExam.NoOrder.selector);
        e.fill();
    }

    /// The cloud audit's N6, turned round, first half: whoever placed the order does not fill it, so
    /// they do not pick the block it fills in. Neither the trader nor their agent; anyone else may.
    function test_deep_N6_theTraderAndTheAgentDoNotFillTheirOwnOrder() public {
        PaperExam e = _exam();
        vm.prank(trader);
        e.setAgent(bot);
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.prank(trader);
        vm.expectRevert(PaperExam.OwnOrder.selector);
        e.fill();
        vm.prank(bot);
        vm.expectRevert(PaperExam.OwnOrder.selector);
        e.fill();
        vm.prank(stranger);
        e.fill();
        assertEq(e.positionOf(BTC), 100);
    }

    /// The second half: a fill is at the oracle price, which the validators take from the exchanges'
    /// prices outside, and not at the mark, which one account trading a thin book moves. The audit's
    /// pass, fill at the mark before the push and close at the mark after it, makes nothing: the
    /// oracle has not moved.
    function test_deep_N6_aFillIsAtTheOraclePrice_notAPushedMark() public {
        vm.mockCall(address(0x0000000000000000000000000000000000000807), abi.encode(BTC), abi.encode(MARK));
        PaperExam e = _exam();
        _trade(e, BTC, true, 400);
        _mark(1_030_000); // the thin book pushed 3 % by the trader's own account
        assertEq(e.equity(), CAPITAL, "the mark moved; the oracle did not, and neither did the equity");
        _trade(e, BTC, false, 400);
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), CAPITAL, "bought and sold at the oracle price: nothing made");
    }

    function test_exam_fill_longShortAndThroughZero() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_050_000);
        assertEq(e.equity(), CAPITAL + 5e6, "a long gains what the price gains");
        assertEq(e.notional(), 105e6);

        // Sell three times the long: now short twice that size, at 105 000.0.
        _trade(e, BTC, false, 300);
        assertEq(e.positionOf(BTC), -200);
        assertEq(e.equity(), CAPITAL + 5e6, "a fill at the mark moves nothing");
        _mark(1_000_000);
        assertEq(e.equity(), CAPITAL + 15e6, "a short gains what the price loses");
        assertEq(e.notional(), 200e6, "notional counts a short as its size");

        _trade(e, BTC, true, 200);
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), CAPITAL + 15e6);
        assertEq(e.notional(), 0);
    }

    function test_exam_fill_twoAssetsAddUp() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100); // 100 USDC long
        _trade(e, ETH, false, 400); // 0.04 ETH at 2500.00: 100 USDC short
        assertEq(e.notional(), 200e6);
        _mark(1_020_000); // BTC +2%: +2
        CoreSimulatorLib.setMarkPx(ETH, 252_500); // ETH +1%: the short loses 1
        assertEq(e.equity(), CAPITAL + 1e6);
    }

    function test_exam_fill_chargesTheCost_roundedUp() public {
        ExamTerms memory t = _terms();
        t.costPpm = 1000;
        PaperExam e = _create(_rules(), t);
        _trade(e, BTC, true, 100);
        // 0.10% of 100 USDC.
        assertEq(e.equity(), CAPITAL - 100_000);
        // 0.10% of 1.000001 USDC is 1000.001 units: the exam takes 1001.
        _mark(1_000_001);
        int256 before = e.cash();
        _trade(e, BTC, false, 1);
        assertEq(e.cash(), before + 1_000_001 - 1001);
    }

    function test_exam_fill_refusedOnAPricelessAsset() public {
        PaperExam e = _exam();
        _place(e, BTC, true, 100);
        _mark(0);
        vm.roll(block.number + DELAY);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NoPrice.selector, BTC));
        e.fill();
    }

    // ── leverage at the fill ─────────────────────────────────

    function test_exam_fill_leverage_boundary() public {
        PaperExam e = _exam();
        // 500 USDC of notional on 100 of equity is exactly 5x: allowed.
        _trade(e, BTC, true, 500);
        assertEq(e.notional(), 500e6);
        assertEq(uint8(e.violation()), uint8(Breach.None));

        PaperExam f = _exam();
        _place(f, BTC, true, 501);
        vm.roll(block.number + DELAY);
        vm.expectRevert(PaperExam.OverLeverage.selector);
        f.fill();
        assertEq(f.positionOf(BTC), 0);
        (,, uint64 sz,) = f.pending();
        assertEq(sz, 501, "a refused order stays until its window ends");
    }

    /// BTC at most 2x and ETH at most 5x of the exam's equity, the exam under 5x.
    function _capped() internal pure returns (Rules memory r) {
        r = _rules();
        r.assetLeverageX100 = new uint32[](2);
        r.assetLeverageX100[0] = 200;
        r.assetLeverageX100[1] = 500;
    }

    /// Each asset's own cap (the pools team, 21 October 2026): a fill past it is refused though the exam is
    /// under its own cap, a mark that carries a position past it is a Leverage breach, a short counts
    /// the same, and each asset is held to the cap at its own place in the list.
    function test_exam_eachAssetsOwnCap() public {
        PaperExam e = _create(_capped(), _terms());
        _trade(e, BTC, true, 200); // 200 USDC on 100: BTC's 2x exactly
        assertEq(uint8(e.violation()), uint8(Breach.None));
        _place(e, BTC, true, 1);
        vm.roll(block.number + DELAY);
        vm.expectRevert(PaperExam.OverLeverage.selector);
        e.fill(); // 2.01x of BTC, the exam far under 5x
        _mark(990_000); // -1%: BTC 198 on 98, the exam at 2.02x of its 5x
        assertEq(uint8(e.violation()), uint8(Breach.Leverage));
        _mark(MARK);

        PaperExam both = _create(_capped(), _terms());
        _trade(both, BTC, true, 200);
        _trade(both, ETH, true, 1200); // 0.12 ETH at 2500.00: 300 USDC, 3x of ETH's 5x, the exam at 5x
        assertEq(uint8(both.violation()), uint8(Breach.None), "each asset its own cap");

        PaperExam s = _create(_capped(), _terms());
        _trade(s, BTC, false, 200);
        _mark(1_010_000); // a short 1% against: 202 on 98
        assertEq(uint8(s.violation()), uint8(Breach.Leverage), "a short the same");

        Rules memory r = _capped();
        r.assetLeverageX100[0] = 500;
        r.assetLeverageX100[1] = 200;
        PaperExam o = _create(r, _terms());
        _trade(o, BTC, true, 300); // BTC 3x of its 5x
        _place(o, ETH, true, 1000); // ETH 250 on 100: past its 2x
        vm.roll(block.number + DELAY);
        vm.expectRevert(PaperExam.OverLeverage.selector);
        o.fill();
    }

    function test_exam_assetCaps_none_orOneForEachAsset_from1xToTheExamsCap() public {
        Rules memory r = _capped();
        r.assetLeverageX100 = new uint32[](1);
        r.assetLeverageX100[0] = 200;
        _expectBadRules(r);
        r = _capped();
        r.assetLeverageX100[1] = 99;
        _expectBadRules(r);
        r = _capped();
        r.assetLeverageX100[1] = 501;
        _expectBadRules(r);
        r = _capped();
        r.assetLeverageX100[0] = 100;
        r.assetLeverageX100[1] = 500;
        PaperExam e = _create(r, _terms());
        assertEq(keccak256(abi.encode(e.rules().assetLeverageX100)), keccak256(abi.encode(r.assetLeverageX100)));
        impl.check(_rules(), _terms()); // none
    }

    function test_exam_fill_aReducingOrderIsNeverRefused() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 500);
        // -1%: equity 95, notional 495, over 5x.
        _mark(990_000);
        assertEq(uint8(e.violation()), uint8(Breach.Leverage));
        // Adding is refused...
        _place(e, BTC, true, 1);
        vm.roll(block.number + DELAY);
        vm.expectRevert(PaperExam.OverLeverage.selector);
        e.fill();
        vm.roll(block.number + WINDOW);
        // ...and the way out is not, even a step of it that leaves the account over the rule:
        // 490 units are 485.1 of notional on 95, still more than 5x.
        _trade(e, BTC, false, 10);
        assertEq(e.positionOf(BTC), 490);
        assertEq(uint8(e.violation()), uint8(Breach.Leverage));
        _trade(e, BTC, false, 90);
        assertEq(e.positionOf(BTC), 400);
        assertEq(uint8(e.violation()), uint8(Breach.None));
    }

    // ── rules ────────────────────────────────────────────────

    function test_exam_drawdown_boundary() public {
        Rules memory r = _rules();
        r.dailyLossBps = 9999; // out of the way
        PaperExam e = _create(r, _terms());
        _trade(e, BTC, true, 100);
        _mark(900_000); // exactly -10%
        assertEq(e.equity(), 90e6);
        assertEq(uint8(e.violation()), uint8(Breach.None));
        vm.expectRevert(PaperExam.NoBreach.selector);
        e.breach();
        _mark(899_999);
        assertEq(uint8(e.violation()), uint8(Breach.Drawdown));
    }

    function test_exam_dailyLoss_boundary() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(950_000); // exactly -5% of the day's start
        assertEq(uint8(e.violation()), uint8(Breach.None));
        _mark(949_999);
        assertEq(uint8(e.violation()), uint8(Breach.DailyLoss));
    }

    function test_exam_dailyLoss_countsFromTheDaysSnapshot() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_080_000);
        _nextMidnight();
        vm.prank(stranger);
        e.checkpoint();
        assertEq(e.dayStartEquity(), 108e6, "the snapshot is equity with the position at its mark");
        assertEq(e.day(), uint32(block.timestamp / 1 days));
        // 5% under 108 is 102.6: above the starting balance, and still the day's limit.
        _mark(1_026_000);
        assertEq(uint8(e.violation()), uint8(Breach.None));
        _mark(1_025_999);
        assertEq(uint8(e.violation()), uint8(Breach.DailyLoss));
    }

    function test_exam_checkpoint_onlyJustAfterMidnight_oncePerDay() public {
        PaperExam e = _exam();
        vm.expectRevert(PaperExam.OutsideCheckpointWindow.selector);
        e.checkpoint();
        // The day the exam started on already has its snapshot.
        vm.warp((block.timestamp / 1 days) * 1 days + 60);
        vm.expectRevert(PaperExam.AlreadyCheckpointed.selector);
        e.checkpoint();
        _nextMidnight();
        e.checkpoint();
        vm.expectRevert(PaperExam.AlreadyCheckpointed.selector);
        e.checkpoint();
        // The last second of the window is in; the next one is out.
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 15 minutes - 1);
        e.checkpoint();
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 15 minutes);
        vm.expectRevert(PaperExam.OutsideCheckpointWindow.selector);
        e.checkpoint();
    }

    function test_exam_leverage_breachWhenEquityFallsUnderThePosition() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 500);
        // -1%: equity 95 (not under the day's 95), notional 495, and 495 > 5 x 95.
        _mark(990_000);
        assertEq(e.equity(), 95e6);
        assertEq(uint8(e.violation()), uint8(Breach.Leverage));
        vm.prank(stranger);
        e.breach();
        assertEq(_status(e), uint8(PaperExam.Status.Breached));
        assertEq(uint8(e.endReason()), uint8(Breach.Leverage));
        assertEq(e.endEquity(), 95e6);
    }

    function test_exam_leverage_boundary() public {
        Rules memory r = _rules();
        r.dailyLossBps = 9999; // out of the way, both of them
        r.maxDrawdownBps = 9999;
        PaperExam e = _create(r, _terms());
        // 500 of notional on 100 of equity: 5x exactly, and that is inside the rule.
        _trade(e, BTC, true, 500);
        assertEq(uint8(e.violation()), uint8(Breach.None));
        // One tick down, equity loses more of itself than the notional does.
        _mark(999_999);
        assertEq(uint8(e.violation()), uint8(Breach.Leverage));
    }

    function test_exam_breach_endsIt_andNothingMovesAfter() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(890_000);
        vm.prank(stranger);
        e.breach();
        assertEq(_status(e), uint8(PaperExam.Status.Breached));
        assertEq(uint8(e.endReason()), uint8(Breach.Drawdown));
        assertEq(e.endEquity(), 89e6);

        bytes memory bad = abi.encodeWithSelector(PaperExam.BadStatus.selector, PaperExam.Status.Breached);
        vm.prank(trader);
        vm.expectRevert(bad);
        e.place(BTC, false, 100);
        vm.expectRevert(bad);
        e.fill();
        vm.expectRevert(bad);
        _graduate(e);
        vm.expectRevert(bad);
        e.breach();
        vm.expectRevert(bad);
        e.expire();
        vm.expectRevert(bad);
        e.checkpoint();
    }

    // ── the deadline ─────────────────────────────────────────

    function test_exam_expire_onlyAfterTheDeadline() public {
        PaperExam e = _exam();
        uint256 deadline = e.deadline();
        vm.warp(deadline);
        vm.expectRevert(PaperExam.NotOver.selector);
        e.expire();
        vm.warp(deadline + 1);
        vm.prank(stranger);
        e.expire();
        assertEq(_status(e), uint8(PaperExam.Status.Expired));
        assertEq(uint8(e.endReason()), uint8(Breach.None));
        assertEq(e.endEquity(), 0, "an expired exam records no equity");
    }

    /// A position in an asset whose price will not answer cannot be judged, and must still end.
    function test_exam_expire_needsNoPrice() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(0);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NoPrice.selector, BTC));
        e.breach();
        vm.warp(e.deadline() + 1);
        e.expire();
        assertEq(_status(e), uint8(PaperExam.Status.Expired));
    }

    function test_exam_afterTheDeadline_noOrderNoFillNoPass() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_100_000);
        _trade(e, BTC, false, 100);
        assertEq(e.cash(), CAPITAL + 10e6, "the target is met and realized");
        _place(e, BTC, true, 100);

        vm.warp(e.deadline() + 1);
        vm.roll(block.number + DELAY);
        vm.expectRevert(PaperExam.TooLate.selector);
        e.fill();
        vm.expectRevert(PaperExam.TooLate.selector);
        _graduate(e);
        vm.roll(block.number + WINDOW);
        vm.prank(trader);
        vm.expectRevert(PaperExam.TooLate.selector);
        e.place(BTC, true, 100);
    }

    function test_exam_atTheDeadlinesLastSecond_everythingStillWorks() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_100_000);
        vm.warp(e.deadline());
        _trade(e, BTC, false, 100);
        _graduate(e);
        assertEq(_status(e), uint8(PaperExam.Status.Passed));
    }

    // ── a step nobody trades ─────────────────────────────────

    function _idleTerms() internal pure returns (ExamTerms memory t) {
        t = _terms();
        t.stepDuration = 30 days;
        t.maxIdle = 14 days;
    }

    function test_exam_idle_endsAStepNobodyTrades() public {
        PaperExam e = _create(_rules(), _idleTerms());
        uint256 start = e.stepStartedAt();
        assertEq(e.lastFillAt(), start);
        // Fourteen days to the second: still open, and still tradable.
        vm.warp(start + 14 days);
        vm.expectRevert(PaperExam.NotOver.selector);
        e.expire();
        _place(e, BTC, true, 100);

        vm.warp(start + 14 days + 1);
        vm.roll(block.number + DELAY);
        vm.expectRevert(PaperExam.TooLate.selector);
        e.fill();
        vm.expectRevert(PaperExam.TooLate.selector);
        _graduate(e);
        vm.roll(block.number + WINDOW);
        vm.prank(trader);
        vm.expectRevert(PaperExam.TooLate.selector);
        e.place(BTC, true, 100);
        vm.prank(stranger);
        e.expire();
        assertEq(_status(e), uint8(PaperExam.Status.Expired));
        assertLt(block.timestamp, e.deadline(), "well inside its thirty days");
    }

    function test_exam_idle_aFillStartsTheCountAgain() public {
        PaperExam e = _create(_rules(), _idleTerms());
        vm.warp(block.timestamp + 10 days);
        _trade(e, BTC, true, 100);
        uint256 filled = block.timestamp;
        assertEq(e.lastFillAt(), filled);
        // An order placed and never filled is not a trade.
        vm.warp(filled + 13 days);
        _place(e, BTC, false, 100);
        vm.warp(filled + 14 days);
        vm.expectRevert(PaperExam.NotOver.selector);
        e.expire();
        vm.warp(filled + 14 days + 1);
        e.expire();
        assertEq(_status(e), uint8(PaperExam.Status.Expired));
    }

    function test_exam_idle_countsFromTheStartOfEachStep() public {
        ExamTerms memory t = _idleTerms();
        t.targetsBps = _targets(800, 500);
        PaperExam e = _create(_rules(), t);
        _trade(e, BTC, true, 100);
        _mark(1_080_000);
        _trade(e, BTC, false, 100);
        // Thirteen days after its last fill the first step is passed...
        vm.warp(block.timestamp + 13 days);
        _graduate(e);
        assertEq(e.step(), 1);
        assertEq(e.lastFillAt(), block.timestamp);
        // ...and the second has its own fourteen, from its own start.
        vm.warp(block.timestamp + 14 days);
        vm.expectRevert(PaperExam.NotOver.selector);
        e.expire();
        vm.warp(block.timestamp + 1);
        e.expire();
    }

    /// A fill too small for the exchange to take is no trade: no trading day, no end to idleness.
    function test_exam_aFillUnderTenUsdcIsNoTrade() public {
        ExamTerms memory t = _terms();
        t.maxIdle = 14 days;
        t.stepDuration = 30 days;
        PaperExam e = _create(_rules(), t);
        uint256 start = e.stepStartedAt();
        vm.warp(start + 10 days);
        _trade(e, BTC, true, 9); // 9 USDC
        assertEq(e.tradingDays(), 0);
        assertEq(e.lastFillAt(), start);
        _trade(e, BTC, false, 9);
        // Ten units, ten USDC: that is a trade.
        _trade(e, BTC, true, 10);
        assertEq(e.tradingDays(), 1);
        assertEq(e.lastFillAt(), block.timestamp);
    }

    // ── passing ──────────────────────────────────────────────

    function test_exam_graduate_target_boundary() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_099_990); // +9.999 USDC
        _trade(e, BTC, false, 100);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TargetNotMet.selector, int256(109_999_000), int256(110e6)));
        _graduate(e);

        _trade(e, BTC, true, 100);
        _mark(1_100_000); // one more tenth of a cent on 100 units
        _trade(e, BTC, false, 100);
        assertEq(e.cash(), 110e6);
        vm.warp(block.timestamp + 1 hours);
        _graduate(e);
        assertEq(_status(e), uint8(PaperExam.Status.Passed));
        assertEq(e.passedAt(), block.timestamp);
        assertEq(e.endEquity(), 110e6);
        (PaperExam.Status s, address who, uint64 capital, uint64 passedAt) = e.result();
        assertEq(uint8(s), uint8(PaperExam.Status.Passed));
        assertEq(who, trader);
        assertEq(capital, 100e6);
        assertEq(passedAt, block.timestamp);
    }

    /// The pass starts the trader's clock to take the account, so only the trader, or the key
    /// they named, may make it.
    function test_exam_graduate_onlyTheTraderOrTheAgent() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_100_000);
        _trade(e, BTC, false, 100);
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotTrader.selector);
        e.graduate();
        vm.prank(trader);
        e.setAgent(bot);
        vm.prank(bot);
        e.graduate();
        assertEq(_status(e), uint8(PaperExam.Status.Passed));
    }

    function test_exam_graduate_needsEverythingClosed() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_200_000);
        assertEq(e.equity(), 120e6, "well past the target, on paper");
        vm.expectRevert(PaperExam.NotFlat.selector);
        _graduate(e);
    }

    /// Closed, past the target, and under the day's limit all the same: that is a breach, not a pass.
    function test_exam_graduate_refusedWhileARuleIsBroken() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _mark(1_200_000);
        _nextMidnight();
        e.checkpoint();
        assertEq(e.dayStartEquity(), 120e6);
        _mark(1_120_000); // 112, and the day's limit is 114
        _trade(e, BTC, false, 100);
        assertEq(e.cash(), 112e6);
        assertEq(e.notional(), 0);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.RuleBroken.selector, Breach.DailyLoss));
        _graduate(e);
        e.breach();
        assertEq(_status(e), uint8(PaperExam.Status.Breached));
        assertEq(uint8(e.endReason()), uint8(Breach.DailyLoss));
    }

    function test_exam_graduate_needsItsTradingDays() public {
        ExamTerms memory t = _terms();
        t.minTradingDays = 2;
        PaperExam e = _create(_rules(), t);
        _trade(e, BTC, true, 100);
        _mark(1_100_000);
        _trade(e, BTC, false, 100);
        assertEq(e.tradingDays(), 1, "two fills on one day are one day");
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NotEnoughDays.selector, uint16(1), uint16(2)));
        _graduate(e);

        vm.warp(block.timestamp + 1 days);
        _trade(e, BTC, true, 10);
        _trade(e, BTC, false, 10);
        assertEq(e.tradingDays(), 2);
        _graduate(e);
        assertEq(_status(e), uint8(PaperExam.Status.Passed));
    }

    function test_exam_twoSteps_theSecondStartsOnAFreshBalance() public {
        ExamTerms memory t = _terms();
        t.targetsBps = _targets(800, 500);
        t.minTradingDays = 1;
        PaperExam e = _create(_rules(), t);

        _trade(e, BTC, true, 100);
        _mark(1_080_000);
        _trade(e, BTC, false, 100);
        // Something left waiting must not carry into the next step.
        vm.warp(block.timestamp + 2 days);
        _place(e, BTC, true, 100);
        _graduate(e);

        assertEq(_status(e), uint8(PaperExam.Status.Active), "one step of two is not a pass");
        assertEq(e.step(), 1);
        assertEq(e.cash(), CAPITAL, "the first step's profit is not carried");
        assertEq(e.dayStartEquity(), CAPITAL);
        assertEq(e.day(), uint32(block.timestamp / 1 days));
        assertEq(e.stepStartedAt(), block.timestamp);
        assertEq(e.deadline(), block.timestamp + 7 days, "the second step has its own week");
        assertEq(e.tradingDays(), 0);
        assertEq(e.passedAt(), 0);
        (,, uint64 sz,) = e.pending();
        assertEq(sz, 0);

        // 4% is not the second step's 5%...
        _mark(1_000_000);
        _trade(e, BTC, true, 100);
        _mark(1_040_000);
        _trade(e, BTC, false, 100);
        assertEq(e.cash(), 104e6);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TargetNotMet.selector, int256(104e6), int256(105e6)));
        _graduate(e);
        // ...5% is.
        _trade(e, BTC, true, 100);
        _mark(1_050_000);
        _trade(e, BTC, false, 100);
        assertEq(e.cash(), 105e6);
        _graduate(e);
        assertEq(_status(e), uint8(PaperExam.Status.Passed));
        assertEq(e.step(), 1);
    }

    function test_exam_tradingDays_countAgainOnTheNextStepsFirstDay() public {
        ExamTerms memory t = _terms();
        t.targetsBps = _targets(800, 500);
        t.minTradingDays = 1;
        PaperExam e = _create(_rules(), t);
        _trade(e, BTC, true, 100);
        _mark(1_080_000);
        _trade(e, BTC, false, 100);
        _graduate(e);
        assertEq(e.step(), 1);
        // Same UTC day as the first step's fills: it still counts for the second step.
        _trade(e, BTC, true, 100);
        assertEq(e.tradingDays(), 1);
    }

    // ── a filler: the trader picks no block ──────────────────────

    address filler = makeAddr("filler");

    function _fillerExam() internal returns (PaperExam e) {
        e = PaperExam(Clones.clone(address(impl)));
        e.initializeWithFiller(trader, _rules(), _terms(), filler, false, 0, 0);
    }

    function _fillerTrade(PaperExam e, bool isBuy, uint64 sz) internal {
        _place(e, BTC, isBuy, sz);
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        e.fill();
    }

    // ── the drawdown line trailing to the start ──────────────────

    /// The rules of `_rules` with the day's loss out of the way, so that only the drawdown line can be broken.
    function _ddOnly() internal pure returns (Rules memory r) {
        r = _rules();
        r.dailyLossBps = 9999;
    }

    function _trailing(Rules memory r, ExamTerms memory t) internal returns (PaperExam e) {
        e = PaperExam(Clones.clone(address(impl)));
        e.initializeWithFiller(trader, r, t, filler, false, 0, 1);
    }

    function _fromStart(Rules memory r) internal returns (PaperExam e) {
        e = PaperExam(Clones.clone(address(impl)));
        e.initializeWithFiller(trader, r, _terms(), filler, false, 0, 0);
    }

    /// The next UTC day's snapshot, at `px`.
    function _snapshotAt(PaperExam e, uint64 px) internal {
        _nextMidnight();
        _mark(px);
        e.checkpoint();
    }

    function test_drawdownKind_isNamedAtTheStart_zeroOrOne() public {
        assertEq(_exam().drawdownKind(), 0, "initialize: from the start");
        assertEq(_fillerExam().drawdownKind(), 0);
        PaperExam e = _trailing(_rules(), _terms());
        assertEq(e.drawdownKind(), e.TRAILING_TO_START());
        assertEq(e.peakSnapshot(), CAPITAL, "the starting balance is the first snapshot");
        assertEq(e.drawdownLine(), 90e6, "until a higher snapshot, the line is the start's");
        PaperExam x = PaperExam(Clones.clone(address(impl)));
        vm.expectRevert(PaperExam.BadTerms.selector);
        x.initializeWithFiller(trader, _rules(), _terms(), filler, false, 0, 2);
    }

    function test_trailingLine_risesWithTheDaysSnapshots_andStopsAtTheStart() public {
        PaperExam e = _trailing(_ddOnly(), _terms());
        _fillerTrade(e, true, 100); // 1x long: equity is 100 times the mark's change
        _mark(1_040_000);
        assertEq(e.drawdownLine(), 90e6, "no snapshot yet: the rise moves nothing");
        _snapshotAt(e, 1_040_000);
        assertEq(e.peakSnapshot(), 104e6);
        assertEq(e.drawdownLine(), 94e6);
        _snapshotAt(e, 1_070_000);
        assertEq(e.drawdownLine(), 97e6);
        _snapshotAt(e, 1_120_000);
        assertEq(e.peakSnapshot(), 112e6, "102 is above the start: the start");
        assertEq(e.drawdownLine(), 100e6, "102 is above the start: the start");
        _snapshotAt(e, 1_150_000);
        assertEq(e.peakSnapshot(), 115e6, "and it stays there");
        assertEq(e.drawdownLine(), 100e6, "and it stays there");
        // On the line holds; a unit under it breaks.
        _mark(1_000_000);
        assertEq(e.equity(), 100e6);
        assertEq(uint8(e.violation()), uint8(Breach.None));
        _mark(999_999);
        assertEq(uint8(e.violation()), uint8(Breach.Drawdown));
        vm.prank(stranger);
        e.breach();
        assertEq(uint8(e.endReason()), uint8(Breach.Drawdown));
    }

    function test_trailingLine_breaksWhereTheLineFromTheStartWouldNot() public {
        PaperExam e = _trailing(_ddOnly(), _terms());
        PaperExam f = _fromStart(_ddOnly());
        _fillerTrade(e, true, 100);
        _fillerTrade(f, true, 100);
        _nextMidnight();
        _mark(1_040_000);
        e.checkpoint();
        f.checkpoint();
        assertEq(f.peakSnapshot(), 104e6, "recorded for both");
        assertEq(f.drawdownLine(), 90e6, "but the line from the start does not move");
        _mark(939_000); // 93.9
        assertEq(uint8(e.violation()), uint8(Breach.Drawdown), "under 94");
        assertEq(uint8(f.violation()), uint8(Breach.None), "over 90");
    }

    function test_trailingLine_aRiseWithinTheDayMovesNothing() public {
        PaperExam e = _trailing(_ddOnly(), _terms());
        _fillerTrade(e, true, 100);
        _mark(1_080_000); // 108 at noon, and back before midnight
        assertEq(e.drawdownLine(), 90e6);
        _snapshotAt(e, 1_010_000);
        assertEq(e.peakSnapshot(), 101e6);
        assertEq(e.drawdownLine(), 91e6);
    }

    function test_trailingLine_aDayWhoseSnapshotNobodyTakesMovesNothing() public {
        PaperExam e = _trailing(_ddOnly(), _terms());
        _fillerTrade(e, true, 100);
        _nextMidnight();
        _mark(1_060_000); // 106 in the window, and nobody takes the snapshot
        _snapshotAt(e, 1_020_000);
        assertEq(e.peakSnapshot(), 102e6, "the 106 never counted");
        assertEq(e.drawdownLine(), 92e6, "the 106 never counted");
    }

    function test_trailingLine_neverComesDown() public {
        PaperExam e = _trailing(_ddOnly(), _terms());
        _fillerTrade(e, true, 100);
        _snapshotAt(e, 1_070_000);
        _snapshotAt(e, 1_040_000);
        assertEq(e.dayStartEquity(), 104e6, "the day's base is the day's snapshot");
        assertEq(e.peakSnapshot(), 107e6, "the line is the highest one's");
        assertEq(e.drawdownLine(), 97e6, "the line is the highest one's");
    }

    function test_trailingLine_noSnapshotWhileItIsBroken() public {
        PaperExam e = _trailing(_ddOnly(), _terms());
        _fillerTrade(e, true, 100);
        _snapshotAt(e, 1_040_000);
        _mark(939_000);
        _nextMidnight();
        vm.expectRevert(abi.encodeWithSelector(PaperExam.RuleBroken.selector, Breach.Drawdown));
        e.checkpoint();
        assertEq(e.peakSnapshot(), 104e6);
        e.breach();
        assertEq(_status(e), uint8(PaperExam.Status.Breached));
    }

    function test_trailingLine_aNewStepStartsAgainFromItsBalance() public {
        ExamTerms memory t = _terms();
        t.targetsBps = _targets(1000, 1000);
        PaperExam e = _trailing(_ddOnly(), t);
        _fillerTrade(e, true, 100);
        _snapshotAt(e, 1_120_000);
        assertEq(e.drawdownLine(), 100e6);
        _fillerTrade(e, false, 100);
        _graduate(e);
        assertEq(e.step(), 1);
        assertEq(e.peakSnapshot(), CAPITAL, "the new step's own start");
        assertEq(e.drawdownLine(), 90e6, "the new step's own start");
        assertEq(e.drawdownKind(), 1, "the same kind");
    }

    // ── stops and takes ──────────────────────────────────────────

    function _setTrigger(PaperExam e, bool isTake, uint64 px, uint64 sz) internal {
        vm.prank(trader);
        e.setTrigger(BTC, isTake, px, sz);
    }

    function _armed(PaperExam e, bool isTake) internal view returns (uint64 armedAt) {
        (,, armedAt,) = isTake ? e.takeOf(BTC) : e.stopOf(BTC);
    }

    function test_trigger_onlyTheTraderSetsIt_onAnOpenPosition_actingAfterTheDelay() public {
        PaperExam e = _fillerExam();
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoPosition.selector);
        e.setTrigger(BTC, false, 950_000, 0);
        _fillerTrade(e, true, 100);
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotTrader.selector);
        e.setTrigger(BTC, false, 950_000, 0);
        vm.prank(trader);
        vm.expectRevert(PaperExam.ZeroSize.selector);
        e.setTrigger(BTC, false, 0, 0);
        vm.expectEmit(address(e));
        emit PaperExam.TriggerSet(BTC, false, 950_000, 0, block.number + DELAY);
        _setTrigger(e, false, 950_000, 0);
        assertEq(_armed(e, false), block.number + DELAY);
    }

    function test_stop_onALong_closesItWhenTheOracleIsAtOrUnderIt_atThatBlocksPrice() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, true, 100);
        _setTrigger(e, false, 950_000, 0);
        _setTrigger(e, true, 1_100_000, 0);
        _mark(940_000);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooEarly.selector, block.number + DELAY));
        e.fillTrigger(BTC, false);
        vm.roll(block.number + DELAY);
        _mark(951_000);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NotTriggered.selector, uint64(951_000), uint64(950_000)));
        e.fillTrigger(BTC, false);
        _mark(940_000); // past it: a stop is a market order once triggered, at this block's price
        vm.expectEmit(address(e));
        emit PaperExam.TriggerFilled(BTC, false, 100, 940_000, 0, 950_000, 0);
        vm.prank(filler);
        e.fillTrigger(BTC, false);
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6, "100 units sold at 940 000");
        (uint64 takePx,,,) = e.takeOf(BTC);
        assertEq(takePx, 0, "the position is closed: its take goes with it");
    }

    function test_take_onALong_closesItsSizeOnly_andTheStopStays() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, true, 100);
        _setTrigger(e, true, 1_050_000, 40);
        _setTrigger(e, false, 950_000, 0);
        vm.roll(block.number + DELAY);
        _mark(1_049_999);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NotTriggered.selector, uint64(1_049_999), uint64(1_050_000)));
        e.fillTrigger(BTC, true);
        _mark(1_050_000); // at it is enough
        vm.prank(filler);
        e.fillTrigger(BTC, true);
        assertEq(e.positionOf(BTC), 60);
        (uint64 stopPx,,,) = e.stopOf(BTC);
        assertEq(stopPx, 950_000, "a smaller position keeps its stop");
        (uint64 takePx,,,) = e.takeOf(BTC);
        assertEq(takePx, 0, "a take is used once");
    }

    function test_stopAndTake_onAShort_triggerTheOtherWay() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, false, 100);
        _setTrigger(e, false, 1_050_000, 0);
        _setTrigger(e, true, 950_000, 0);
        vm.roll(block.number + DELAY);
        _mark(1_000_000);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NotTriggered.selector, uint64(1_000_000), uint64(1_050_000)));
        e.fillTrigger(BTC, false);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NotTriggered.selector, uint64(1_000_000), uint64(950_000)));
        e.fillTrigger(BTC, true);
        _mark(1_060_000);
        vm.prank(filler);
        e.fillTrigger(BTC, false);
        assertEq(e.positionOf(BTC), 0, "the short bought back");
        assertEq(e.cash(), 100e6 + 100 * 1_000_000 - 100 * 1_060_000);
    }

    function test_stop_setPastThePrice_waitsTheDelayAsAnOrderDoes() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, true, 100);
        _setTrigger(e, false, 1_200_000, 0); // already past: it would close at once
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooEarly.selector, block.number + DELAY));
        e.fillTrigger(BTC, false);
        vm.roll(block.number + DELAY - 1);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooEarly.selector, block.number + 1));
        e.fillTrigger(BTC, false);
        vm.roll(block.number + 1);
        vm.prank(filler);
        e.fillTrigger(BTC, false);
        assertEq(e.positionOf(BTC), 0);
    }

    function test_trigger_goesWithAPositionAnOrderClosesOrTurnsRound() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, true, 100);
        _setTrigger(e, false, 950_000, 0);
        _setTrigger(e, true, 1_100_000, 0);
        _fillerTrade(e, false, 40); // smaller: both stay
        (uint64 stopPx,,,) = e.stopOf(BTC);
        assertEq(stopPx, 950_000);
        _fillerTrade(e, false, 160); // from 60 long to 100 short: both go
        (stopPx,,,) = e.stopOf(BTC);
        (uint64 takePx,,,) = e.takeOf(BTC);
        assertEq(stopPx + takePx, 0);
        assertEq(e.positionOf(BTC), -100);
    }

    function test_trigger_aStalledFillerLeavesItToAnyone_aMinuteAfterItWasSeen() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, true, 100);
        _setTrigger(e, false, 950_000, 0);
        vm.roll(block.number + DELAY);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NotTriggered.selector, uint64(MARK), uint64(950_000)));
        e.seeTrigger(BTC, false);
        _mark(940_000);
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotFiller.selector);
        e.fillTrigger(BTC, false);
        vm.expectEmit(address(e));
        emit PaperExam.TriggerSeen(BTC, false, 940_000, block.number);
        vm.prank(stranger);
        e.seeTrigger(BTC, false);
        uint256 seen = block.number;
        // Seen again later, by the filler say: the clock does not start again, or a stalled filler could hold it.
        vm.roll(seen + 30);
        vm.prank(filler);
        e.seeTrigger(BTC, false);
        (,,, uint64 seenAt) = e.stopOf(BTC);
        assertEq(seenAt, seen);
        vm.roll(seen + e.FALLBACK_BLOCKS() - 1);
        vm.prank(trader);
        vm.expectRevert(PaperExam.NotFiller.selector);
        e.fillTrigger(BTC, false);
        vm.roll(seen + e.FALLBACK_BLOCKS());
        _mark(960_000); // back over it: the fallback still needs the trigger to hold
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NotTriggered.selector, uint64(960_000), uint64(950_000)));
        e.fillTrigger(BTC, false);
        _mark(945_000);
        vm.prank(trader);
        e.fillTrigger(BTC, false);
        assertEq(e.positionOf(BTC), 0);
    }

    function test_trigger_withoutAFiller_anyoneButTheTrader() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _setTrigger(e, false, 950_000, 0);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        vm.prank(trader);
        vm.expectRevert(PaperExam.OwnOrder.selector);
        e.fillTrigger(BTC, false);
        vm.prank(stranger);
        e.fillTrigger(BTC, false);
        assertEq(e.positionOf(BTC), 0);
    }

    function test_trigger_theTraderClearsIt_andAFillOfItIsATrade() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, true, 100);
        _setTrigger(e, true, 1_050_000, 0);
        vm.prank(trader);
        e.clearTrigger(BTC, true);
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.clearTrigger(BTC, true);
        vm.roll(block.number + DELAY);
        _mark(1_060_000);
        vm.prank(filler);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.fillTrigger(BTC, true);
        // Set again and carried out the next UTC day: that day is a trading day.
        _setTrigger(e, true, 1_050_000, 0);
        uint16 days_ = e.tradingDays();
        _nextMidnight();
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        e.fillTrigger(BTC, true);
        assertEq(e.tradingDays(), days_ + 1);
    }

    function test_filler_isNamedAtTheStartAndIsNeitherTheTraderNorThePool() public {
        PaperExam e = PaperExam(Clones.clone(address(impl)));
        vm.expectEmit(address(e));
        emit PaperExam.FillerSet(filler, false, 0);
        e.initializeWithFiller(trader, _rules(), _terms(), filler, false, 0, 0);
        assertEq(e.filler(), filler);
        assertEq(_exam().filler(), address(0), "an exam started the old way has none");
        address[3] memory bad = [address(0), trader, address(this)];
        for (uint256 i = 0; i < bad.length; ++i) {
            PaperExam x = PaperExam(Clones.clone(address(impl)));
            vm.expectRevert(PaperExam.BadTerms.selector);
            x.initializeWithFiller(trader, _rules(), _terms(), bad[i], false, 0, 0);
        }
    }

    function test_filler_aloneFillsInsideTheWindow() public {
        PaperExam e = _fillerExam();
        address agent = makeAddr("agent");
        vm.prank(trader);
        e.setAgent(agent);
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotFiller.selector);
        e.fill();
        vm.prank(trader);
        vm.expectRevert(PaperExam.OwnOrder.selector);
        e.fill();
        vm.prank(agent);
        vm.expectRevert(PaperExam.OwnOrder.selector);
        e.fill();
        vm.roll(block.number + WINDOW - 1); // the window's last block
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotFiller.selector);
        e.fill();
        vm.prank(filler);
        e.fill();
        assertEq(e.positionOf(BTC), 100);
    }

    function test_filler_isNeverTheTradersAgent() public {
        PaperExam e = _fillerExam();
        _place(e, BTC, true, 100);
        vm.prank(trader);
        vm.expectRevert(PaperExam.BadTerms.selector);
        e.setAgent(filler);
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        e.fill();
        assertEq(e.positionOf(BTC), 100, "the filler still fills");
        vm.prank(trader);
        e.setAgent(makeAddr("agent")); // any other key
    }

    function test_filler_aLapsedOrderCostsNothingAndIsReplaced() public {
        PaperExam e = _fillerExam();
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY + WINDOW); // past the window: nobody filled it
        vm.prank(filler);
        vm.expectRevert(PaperExam.OrderLapsed.selector);
        e.fill();
        assertEq(e.equity(), CAPITAL, "a lapse costs the trader nothing");
        vm.expectEmit(address(e));
        emit PaperExam.Lapsed(BTC, true, 100);
        _place(e, BTC, true, 50);
        (, , uint64 sz,) = e.pending();
        assertEq(sz, 50);
    }

    function test_filler_aSmallerPositionIsAnyonesOnceTheFallbackHasPassed() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, true, 100);
        _place(e, BTC, false, 60);
        uint256 from = block.number + DELAY;
        vm.roll(from + WINDOW);
        vm.prank(trader);
        vm.expectRevert(PaperExam.OrderLapsed.selector);
        e.fill();
        vm.roll(from + WINDOW + e.FALLBACK_BLOCKS() - 1);
        vm.prank(trader);
        vm.expectRevert(PaperExam.OrderLapsed.selector);
        e.fill();
        vm.roll(from + WINDOW + e.FALLBACK_BLOCKS());
        vm.expectEmit(address(e));
        emit PaperExam.Filled(BTC, false, 60, MARK, 0, WINDOW + e.FALLBACK_BLOCKS(), 0);
        vm.prank(trader);
        e.fill();
        assertEq(e.positionOf(BTC), 40);
    }

    function test_filler_aClosingOrderToFlatFallsBackToo() public {
        PaperExam e = _fillerExam();
        _fillerTrade(e, false, 100);
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY + WINDOW + e.FALLBACK_BLOCKS());
        vm.prank(stranger);
        e.fill();
        assertEq(e.positionOf(BTC), 0);
    }

    function test_filler_anOrderThatOpensOrGrowsOrFlipsNeverFallsBack() public {
        PaperExam e = _fillerExam();
        // From flat: it opens.
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY + WINDOW + e.FALLBACK_BLOCKS());
        vm.prank(trader);
        vm.expectRevert(PaperExam.OrderLapsed.selector);
        e.fill();
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        e.fill();
        // The same side: it grows. Past the position: it flips.
        bool[2] memory sides = [true, false];
        uint64[2] memory sizes = [uint64(10), uint64(101)];
        for (uint256 i = 0; i < 2; ++i) {
            _place(e, BTC, sides[i], sizes[i]);
            vm.roll(block.number + DELAY + WINDOW + e.FALLBACK_BLOCKS());
            vm.prank(trader);
            vm.expectRevert(PaperExam.OrderLapsed.selector);
            e.fill();
        }
        assertEq(e.positionOf(BTC), 100);
    }

    function test_noFiller_nothingFallsBack_asBefore() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _place(e, BTC, false, 100);
        vm.roll(block.number + DELAY + WINDOW + e.FALLBACK_BLOCKS());
        vm.prank(stranger);
        vm.expectRevert(PaperExam.OrderLapsed.selector);
        e.fill();
    }

    /// A price that spikes for one block of the window and comes back: filled at the first block (late 0), the order
    /// gains nothing from it; the only way to take the spike is the filler choosing its block, and the fill then says
    /// it came a block late (CTO, 13 November).
    function test_filler_aOneBlockSpike_noGainAtTheFirstBlock_aLateFillShowsIt() public {
        PaperExam e = _fillerExam();
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.recordLogs();
        vm.prank(filler);
        e.fill();
        (, , uint64 px,, uint256 late,) = _lastFilled();
        assertEq(late, 0, "the first block");
        assertEq(px, MARK);
        _mark(MARK * 9 / 10); // the spike, one block
        vm.roll(block.number + 1);
        _mark(MARK); // and back
        vm.roll(block.number + 1);
        _fillerTrade(e, false, 100);
        assertEq(e.equity(), CAPITAL, "nothing gained from a spike the fill did not take");

        PaperExam f = _fillerExam();
        _place(f, BTC, true, 100);
        vm.roll(block.number + DELAY + 1); // the window's second block
        _mark(MARK * 9 / 10);
        vm.recordLogs();
        vm.prank(filler);
        f.fill();
        (, , px,, late,) = _lastFilled();
        assertEq(px, MARK * 9 / 10, "the spike's price");
        assertEq(late, 1, "and the fill says it came a block late, for anyone to count");
        _mark(MARK);
        _fillerTrade(f, false, 100);
        assertEq(f.equity(), CAPITAL + 10e6, "the spike's 10 % went to the trader, through the filler's choice alone");
    }

    function _lastFilled() internal returns (bool isBuy, uint64 sz, uint64 px, uint256 cost, uint256 late, uint8 source) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = logs.length; i > 0; --i) {
            if (logs[i - 1].topics.length == 0 || logs[i - 1].topics[0] != PaperExam.Filled.selector) continue;
            return abi.decode(logs[i - 1].data, (bool, uint64, uint64, uint256, uint256, uint8));
        }
        revert("no Filled");
    }

    function test_filled_saysHowManyBlocksAfterTheFirstItCame() public {
        PaperExam e = _fillerExam();
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.expectEmit(address(e));
        emit PaperExam.Filled(BTC, true, 100, MARK, 0, 0, 0);
        vm.prank(filler);
        e.fill();
        _place(e, BTC, false, 100);
        vm.roll(block.number + DELAY + 2);
        vm.expectEmit(address(e));
        emit PaperExam.Filled(BTC, false, 100, MARK, 0, 2, 0);
        vm.prank(filler);
        e.fill();
    }

    // ── a fill at the book ───────────────────────────────────────

    function _book(uint64 bid, uint64 ask) internal {
        vm.etch(0x000000000000000000000000000000000000080e, type(BboStub).runtimeCode);
        BboStub(0x000000000000000000000000000000000000080e).set(BTC, bid, ask);
    }

    function _bookExam(uint16 maxSpread) internal returns (PaperExam e) {
        e = PaperExam(Clones.clone(address(impl)));
        e.initializeWithFiller(trader, _rules(), _terms(), filler, true, maxSpread, 0);
    }

    function _bookFill(PaperExam e, bool isBuy, uint64 sz) internal returns (uint64 px, uint8 source) {
        _place(e, BTC, isBuy, sz);
        vm.roll(block.number + DELAY);
        vm.recordLogs();
        vm.prank(filler);
        e.fill();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        (,, px,,, source) = abi.decode(logs[logs.length - 1].data, (bool, uint64, uint64, uint256, uint256, uint8));
    }

    function test_book_aBuyTakesTheAskASellTheBid() public {
        _book(MARK - 50, MARK + 50); // 1 bp of spread around the oracle
        PaperExam e = _bookExam(20);
        (uint64 px, uint8 source) = _bookFill(e, true, 100);
        assertEq(px, MARK + 50, "the ask");
        assertEq(source, e.SOURCE_BOOK());
        (px, source) = _bookFill(e, false, 100);
        assertEq(px, MARK - 50, "the bid");
        assertEq(source, e.SOURCE_BOOK());
        assertEq(e.equity(), CAPITAL - 100 * 100, "half the spread each way: 0.01 USDC on 100");
    }

    function test_book_neverBetterForTheTraderThanTheOracle() public {
        // A book that stands under the oracle, as testnet's do, or one the trader put there.
        _book(MARK - 2000, MARK - 1990);
        PaperExam e = _bookExam(20);
        (uint64 px, uint8 source) = _bookFill(e, true, 100);
        assertEq(px, MARK, "a buy no cheaper than the oracle");
        assertEq(source, e.SOURCE_BOOK());
        (px,) = _bookFill(e, false, 100);
        assertEq(px, MARK - 2000, "a sell at the bid, under it: 20 bp, the most allowed");
        // And over it: a sell no dearer than the oracle.
        _book(MARK + 1990, MARK + 2000);
        (px,) = _bookFill(e, true, 100);
        assertEq(px, MARK + 2000);
        (px,) = _bookFill(e, false, 100);
        assertEq(px, MARK, "a sell no dearer than the oracle");
    }

    function test_book_tooFarFromTheOracleWaitsForALaterBlock() public {
        PaperExam e = _bookExam(20);
        _book(MARK + 1990, MARK + 2001); // the ask 20.01 bp over the oracle
        _place(e, BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooFarFromOracle.selector, MARK + 2001, MARK));
        e.fill();
        (,, uint64 waiting,) = e.pending();
        assertEq(waiting, 100, "the order still waits");
        _book(MARK - 10, MARK + 10);
        vm.roll(block.number + 1);
        vm.expectEmit(address(e));
        emit PaperExam.Filled(BTC, true, 100, MARK + 10, 0, 1, 1);
        vm.prank(filler);
        e.fill();
        // A sell the same way, under the oracle.
        _book(MARK - 2001, MARK - 1990);
        _place(e, BTC, false, 100);
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooFarFromOracle.selector, MARK - 2001, MARK));
        e.fill();
    }

    function test_book_anEmptyOneSidedOrCrossedBookFillsAtTheOracle() public {
        PaperExam e = _bookExam(20);
        uint64[2][4] memory books = [
            [uint64(0), uint64(0)], // nothing on either side: a sell would fill at zero
            [uint64(0), MARK + 50], // no bid
            [MARK - 50, uint64(0)], // no ask
            [MARK + 50, MARK - 50] // crossed
        ];
        _bookFill(e, true, 200); // a position to sell from
        for (uint256 i = 0; i < 4; ++i) {
            _book(books[i][0], books[i][1]);
            (uint64 px, uint8 source) = _bookFill(e, i % 2 == 1, 100);
            assertEq(px, MARK);
            assertEq(source, e.SOURCE_ORACLE_FALLBACK());
        }
    }

    function test_book_aBookThatDoesNotAnswerFillsAtTheOracle() public {
        vm.etch(0x000000000000000000000000000000000000080e, type(Refuses).runtimeCode);
        PaperExam e = _bookExam(20);
        (uint64 px, uint8 source) = _bookFill(e, true, 100);
        assertEq(px, MARK);
        assertEq(source, e.SOURCE_ORACLE_FALLBACK());
        // An answer too short to be a bid and an ask.
        vm.etch(0x000000000000000000000000000000000000080e, type(AnswersShort).runtimeCode);
        (px, source) = _bookFill(e, false, 100);
        assertEq(px, MARK);
        assertEq(source, e.SOURCE_ORACLE_FALLBACK());
    }

    function test_book_aSmallerPositionAfterTheFallbackTakesTheSameRule() public {
        PaperExam e = _bookExam(20);
        _book(MARK - 10, MARK + 10);
        _bookFill(e, true, 100);
        _place(e, BTC, false, 100);
        vm.roll(block.number + DELAY + WINDOW + e.FALLBACK_BLOCKS());
        _book(MARK - 2001, MARK - 1990);
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TooFarFromOracle.selector, MARK - 2001, MARK));
        e.fill();
        _book(MARK - 30, MARK + 30);
        vm.expectEmit(address(e));
        emit PaperExam.Filled(BTC, false, 100, MARK - 30, 0, WINDOW + e.FALLBACK_BLOCKS(), 1);
        vm.prank(trader);
        e.fill();
    }

    function test_book_theOracleKeepsEquityAndTheRules() public {
        _book(MARK - 50, MARK + 50);
        PaperExam e = _bookExam(20);
        _bookFill(e, true, 100);
        _book(MARK - 5000, MARK - 4000); // the book falls, the oracle stays
        assertEq(e.equity(), CAPITAL - 50 * 100, "valued at the oracle");
    }

    function test_book_theCeilingIsBoundedAndOnlyWithTheBook() public {
        uint16[3] memory bad = [uint16(0), 501, 2000];
        for (uint256 i = 0; i < 3; ++i) {
            PaperExam x = PaperExam(Clones.clone(address(impl)));
            vm.expectRevert(PaperExam.BadTerms.selector);
            x.initializeWithFiller(trader, _rules(), _terms(), filler, true, bad[i], 0);
        }
        PaperExam y = PaperExam(Clones.clone(address(impl)));
        vm.expectRevert(PaperExam.BadTerms.selector);
        y.initializeWithFiller(trader, _rules(), _terms(), filler, false, 20, 0);
        PaperExam z = _bookExam(500);
        assertTrue(z.fromBook());
        assertEq(z.maxWorseBps(), 500);
    }

    function test_book_anOracleExamFillsAtTheOracleWhateverTheBook() public {
        _book(MARK - 50, MARK + 50);
        PaperExam e = _fillerExam();
        (uint64 px, uint8 source) = _bookFill(e, true, 100);
        assertEq(px, MARK);
        assertEq(source, e.SOURCE_ORACLE());
    }
}

/// The chain's best bid and offer, as the precompile answers: (bid, ask) for an asset.
contract BboStub {
    mapping(uint64 => uint64[2]) internal _b;

    function set(uint64 asset, uint64 bid, uint64 ask) external {
        _b[asset] = [bid, ask];
    }

    fallback(bytes calldata data) external returns (bytes memory) {
        uint64 asset = abi.decode(data, (uint64));
        return abi.encode(_b[asset][0], _b[asset][1]);
    }
}

contract AnswersShort {
    fallback(bytes calldata) external returns (bytes memory) {
        return abi.encode(uint64(1));
    }
}

contract Refuses {
    fallback() external {
        revert();
    }
}
