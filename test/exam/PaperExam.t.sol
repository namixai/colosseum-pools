// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
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

    /// HIP-3 (the CTO, 21 October 2026): a builder dex's market is priced at the index the precompiles
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

    /// Each asset's own cap (the CTO, 21 October 2026): a fill past it is refused though the exam is
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
}
