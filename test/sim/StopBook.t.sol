// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test, Vm} from "forge-std/Test.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";

import {Rules} from "../../src/Types.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {OracleFollowsMark} from "../PoolHarness.sol";
import {BookStand} from "../exam/BookStand.sol";
import {BookStub} from "../exam/PaperExamInvariant.t.sol";

/// An exam whose `fireTrigger` fails: it reverts, or burns all the gas it is given.
contract BadExam {
    StopBook immutable b;
    bool immutable burns;

    constructor(StopBook b_, bool burns_) {
        b = b_;
        burns = burns_;
    }

    function arm(uint32 asset, bool below, uint64 level, uint64 armedAt) external {
        b.add(asset, false, below, level, armedAt);
    }

    function fireTrigger(uint32, bool, uint64, uint64, uint64, uint64) external view returns (uint8) {
        if (!burns) revert("no");
        uint256 x;
        while (gasleft() > 0) ++x;
        return x == 0 ? 1 : 0;
    }
}

/// An exam whose `fireTrigger` calls the book back: it sets a stop on the next asset, a write into the waiting heap in
/// the middle of a check.
contract BackCaller {
    StopBook immutable b;

    constructor(StopBook b_) {
        b = b_;
    }

    function arm(uint32 asset, bool below, uint64 level, uint64 armedAt) external {
        b.add(asset, false, below, level, armedAt);
    }

    function fireTrigger(uint32 asset, bool, uint64 level, uint64 armedAt, uint64, uint64) external returns (uint8) {
        b.add(asset + 1, false, true, level, armedAt + 100);
        return 1;
    }
}

/// A precompile that does not answer.
contract Silent {
    fallback() external {
        revert();
    }
}

/// The stop book: a stop or a take the oracle price reaches is carried out in the first block the book checks at that
/// price, at that price, and nothing the trader does in that block comes before it.
///
/// BTC stands at 100 000.0 (1 000 000) and an exam starts with 100 USDC: 100 size units long is 100 USDC of notional,
/// and every 1 % of price is 1 USDC. The prices of the precompiles are the simulator's (the oracle follows its mark);
/// the gas of the oracle precompile on HyperEVM is not what it costs here.
contract StopBookTest is Test, BookStand {
    uint32 constant BTC = 3;
    uint32 constant ETH = 4;
    uint64 constant MARK = 1_000_000;
    uint64 constant ETH_MARK = 250_000;
    uint16 constant DELAY = 2;
    uint16 constant WINDOW = 3;
    uint16 constant WORSE = 150;
    address constant BBO = 0x000000000000000000000000000000000000080e;
    address constant ORACLE = 0x0000000000000000000000000000000000000807;

    HyperCore hyperCore;
    PaperExam impl;
    address trader = makeAddr("trader");
    address filler = makeAddr("filler");
    address stranger = makeAddr("stranger");

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        vm.etch(ORACLE, type(OracleFollowsMark).runtimeCode);
        vm.etch(BBO, type(BookStub).runtimeCode);
        _asset(BTC, 5, MARK);
        _asset(ETH, 4, ETH_MARK);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);
        impl = new PaperExam();
        _makeBook();
    }

    // ── helpers ──────────────────────────────────────────────────

    function _asset(uint32 a, uint8 szDecimals, uint64 px) internal {
        hyperCore.registerPerpAssetInfo(
            a,
            PrecompileLib.PerpAssetInfo({coin: "X", marginTableId: 54, szDecimals: szDecimals, maxLeverage: 40, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(a, px);
    }

    function _rules(uint32[] memory assets) internal pure returns (Rules memory) {
        return Rules({dailyLossBps: 5000, maxDrawdownBps: 5000, maxLeverageX100: 500, assets: assets, assetLeverageX100: new uint32[](0)});
    }

    function _two() internal pure returns (uint32[] memory a) {
        a = new uint32[](2);
        (a[0], a[1]) = (BTC, ETH);
    }

    function _terms() internal pure returns (ExamTerms memory) {
        uint16[] memory t = new uint16[](1);
        t[0] = 1000;
        return ExamTerms({capital: 100e6, targetsBps: t, stepDuration: 7 days, maxIdle: 0, minTradingDays: 0, costPpm: 0,
            fillDelay: DELAY, fillWindow: WINDOW});
    }

    function _exam() internal returns (PaperExam) {
        return _examOn(_two(), false);
    }

    function _examOn(uint32[] memory assets, bool fromBook) internal returns (PaperExam e) {
        e = PaperExam(Clones.clone(address(impl)));
        e.initializeWithFiller(trader, _rules(assets), _terms(), filler, fromBook, fromBook ? WORSE : 0, 0, false);
    }

    function _trade(PaperExam e, uint32 asset, bool isBuy, uint64 sz) internal {
        vm.prank(trader);
        e.place(asset, isBuy, sz);
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        e.fill();
    }

    function _set(PaperExam e, uint32 asset, bool isTake, uint64 px, uint64 sz) internal {
        vm.prank(trader);
        e.setTrigger(asset, isTake, px, sz);
    }

    function _mark(uint64 px) internal {
        CoreSimulatorLib.setMarkPx(BTC, px);
    }

    function _px(PaperExam e, uint32 asset, bool isTake) internal view returns (uint64 px) {
        (px,,) = isTake ? e.takeOf(asset) : e.stopOf(asset);
    }

    function _where(PaperExam e, uint32 asset, bool isTake) internal view returns (uint8) {
        return stopBook.slotOf(address(e), asset, isTake).where;
    }

    /// A long of 100 on BTC with a stop at 950 000, armed `DELAY` blocks on, and the block it arms in.
    function _longWithStop() internal returns (PaperExam e, uint256 armedAt) {
        e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        armedAt = block.number + DELAY;
    }

    // ── the four triggers ────────────────────────────────────────

    /// A long's stop: the one block whose oracle price is under it closes it, in that block, at that price.
    function test_longStop_aOneBlockTouch_isFilledInThatBlock_atThatPrice() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 5);
        _mark(940_000); // one block under the stop, a gap through it
        uint256 touch = block.number;
        vm.expectEmit(address(e));
        emit PaperExam.Filled(BTC, false, 100, 940_000, 0, touch - armedAt, 0);
        vm.expectEmit(address(e));
        emit PaperExam.TriggerFilled(BTC, false, 100, 940_000, 0, 950_000, 0, touch);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, false, 940_000, touch, 2); // CLOSED
        stopBook.poke();
        vm.roll(touch + 1);
        _mark(MARK); // back over it the next block: nothing to undo
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6, "100 units sold at 940 000, the gap's price, not the stop's");
        assertEq(_where(e, BTC, false), 0);
    }

    function test_longTake_atItsLevel_closesItsSizeOnly_andTheStopStays() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _set(e, BTC, true, 1_050_000, 40);
        vm.roll(armedAt);
        _mark(1_049_999);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 100, "under the take: nothing");
        vm.roll(armedAt + 1);
        _mark(1_050_000); // at it is enough
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, true, 1_050_000, block.number, 1); // FILLED: the position stays open
        stopBook.poke();
        assertEq(e.positionOf(BTC), 60);
        assertEq(_px(e, BTC, false), 950_000, "a smaller position keeps its stop");
        assertEq(_where(e, BTC, false), 3, "and the book keeps it, below");
        assertEq(_px(e, BTC, true), 0);
        assertEq(_where(e, BTC, true), 0);
    }

    function test_shortStopAndTake_triggerTheOtherWay() public {
        PaperExam e = _exam();
        _trade(e, BTC, false, 100);
        _set(e, BTC, false, 1_050_000, 0);
        _set(e, BTC, true, 950_000, 50);
        assertEq(stopBook.slotOf(address(e), BTC, false).where, 2, "a short's stop waits for the above heap");
        assertEq(stopBook.slotOf(address(e), BTC, true).where, 1, "a short's take for the below heap");
        vm.roll(block.number + DELAY);
        _mark(960_000); // not yet the take
        stopBook.poke();
        assertEq(e.positionOf(BTC), -100);
        vm.roll(block.number + 1);
        _mark(950_000);
        stopBook.poke();
        assertEq(e.positionOf(BTC), -50, "the take bought back half");
        vm.roll(block.number + 1);
        _mark(1_070_000);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0, "the stop bought back the rest, at the gap's price");
        assertEq(e.cash(), 100e6 + 100 * 1_000_000 - 50 * 950_000 - 50 * 1_070_000);
    }

    // ── arming ───────────────────────────────────────────────────

    /// Not before `armedAt`, even past the price; in the block of `armedAt`, yes: a stop the price passes in its delay
    /// waits the delay as an order does.
    function test_arming_notBeforeArmedAt_inItsBlockYes() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        uint256 armedAt = block.number + DELAY;
        vm.roll(armedAt - 1);
        _mark(940_000); // past it, a block early
        stopBook.poke();
        assertEq(e.positionOf(BTC), 100, "a block early");
        (uint32[] memory watched, uint256 waiting) = stopBook.watched();
        assertEq(watched.length, 0);
        assertEq(waiting, 1);
        vm.roll(armedAt);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0, "in its block");
    }

    /// Blocks nobody checked: the next check says so, arms what came due in them, and checks it at its own price.
    function test_gap_isShown_andWhatArmedInItIsChecked() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        stopBook.poke(); // the stop's own block was checked by `setTrigger`; so is this one, a second time: nothing
        uint256 last = stopBook.lastPokeBlock();
        vm.roll(armedAt + 10);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Gap(last + 1, block.number - 1);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        vm.roll(block.number + 1);
        vm.recordLogs();
        stopBook.poke();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; ++i) {
            assertTrue(logs[i].topics[0] != StopBook.Gap.selector, "the next block: no gap");
        }
    }

    // ── the trader in the block of the touch ─────────────────────

    /// Before any check in the block of the touch, the trader's `clearTrigger` finds the stop already carried out by
    /// the check it starts with, and is refused; the next check of the block carries it out.
    function test_touchBlock_clearTriggerIsRefused_theStopIsFilledFirst() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(940_000);
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.clearTrigger(BTC, false);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6);
    }

    /// Moving it in that block: the stop is carried out first, so there is no position left to set it on.
    function test_touchBlock_setTriggerIsRefused_theStopIsFilledFirst() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(940_000);
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoPosition.selector);
        e.setTrigger(BTC, false, 900_000, 0);
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoPosition.selector);
        e.setTrigger(BTC, true, 2_000_000, 0);
        assertLt(stopBook.lastPokeBlock(), block.number, "the refusals undid their checks: this block is still unchecked");
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
    }

    /// A call that goes through in that block has the stop carried out before it: the order is placed on a flat
    /// position, a breach is judged after the close.
    function test_touchBlock_placeAndBreach_comeAfterTheStop() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(940_000);
        vm.recordLogs();
        vm.prank(trader);
        e.place(ETH, true, 10);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 filledAt;
        uint256 placedAt;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == PaperExam.TriggerFilled.selector) filledAt = i + 1;
            if (logs[i].topics[0] == PaperExam.Placed.selector) placedAt = i + 1;
        }
        assertTrue(filledAt != 0 && filledAt < placedAt, "the stop, then the order");
        assertEq(e.positionOf(BTC), 0);

        // A breach in the block of a touch: judged on the account the stop left.
        _mark(MARK);
        PaperExam b = _exam();
        _trade(b, BTC, true, 400); // 4x
        _set(b, BTC, false, 900_000, 0);
        vm.roll(block.number + DELAY);
        _mark(880_000); // 4 x 12 % = 48 USDC lost at this price; the stop sells at it
        vm.expectRevert(PaperExam.NoBreach.selector);
        b.breach();
        stopBook.poke();
        assertEq(b.cash(), 100e6 - 400 * 1_000_000 + 400 * 880_000);
        assertEq(uint8(b.status()), uint8(PaperExam.Status.Active), "52 USDC left, over the 50 % line");
    }

    function test_touchBlock_graduateAndCheckpoint_comeAfterTheStop() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, true, 1_100_000, 0); // a take at the target
        vm.roll(block.number + DELAY);
        _mark(1_100_000);
        // Not flat before the check; the check takes the take, and the pass comes after it.
        _graduateOk(e);
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Passed));

        _mark(MARK);
        PaperExam c = _exam();
        _trade(c, BTC, true, 100);
        _set(c, BTC, false, 995_000, 0);
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 60);
        vm.roll(block.number + DELAY);
        _mark(990_000);
        c.checkpoint();
        assertEq(c.positionOf(BTC), 0, "the stop first");
        assertEq(c.dayStartEquity(), 100e6 - 100 * 10_000, "the day's base is the closed account");
    }

    /// The filler's fill and the trader's `setAgent` in the block of a touch: the stop first.
    function test_touchBlock_fillAndSetAgent_comeAfterTheStop() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.prank(trader);
        e.place(ETH, true, 10);
        vm.roll(armedAt);
        _mark(940_000);
        vm.prank(filler);
        e.fill();
        assertEq(e.positionOf(BTC), 0, "the stop, by the fill's check");
        _mark(MARK);
        (PaperExam a, uint256 armed) = _longWithStop();
        vm.roll(armed);
        _mark(940_000);
        vm.prank(trader);
        a.setAgent(stranger);
        assertEq(a.positionOf(BTC), 0, "the stop, by setAgent's check");
    }

    /// Ending one exam checks the block too: another exam's stop reached in it is carried out.
    function test_touchBlock_expireOfOneExam_fillsAnothersStop() public {
        PaperExam old = _exam();
        vm.warp(block.timestamp + 4 days);
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.warp(block.timestamp + 4 days); // past the first exam's week, not the second's
        vm.roll(armedAt);
        _mark(940_000);
        old.expire();
        assertEq(e.positionOf(BTC), 0);
    }

    function _graduateOk(PaperExam e) internal {
        vm.prank(trader);
        e.graduate();
    }

    // ── the price of a fill ──────────────────────────────────────

    /// At the book: the book's price when it is within `maxWorseBps` of the oracle; past it, the bound, never refused;
    /// a book that does not answer, the oracle.
    function test_book_pastTheBoundFillsAtTheBound_noAnswerAtTheOracle() public {
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        PaperExam e = _examOn(one, true);
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 40);
        _set(e, BTC, true, 1_050_000, 30);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        BookStub(BBO).set(BTC, 900_000, 941_000); // the bid 4.3 % under: past 1.5 %
        stopBook.poke();
        uint256 bound = 940_000 - uint256(940_000) * WORSE / 10_000;
        assertEq(e.cash(), int256(100e6) - 100 * 1_000_010 + 40 * int256(bound), "sold at the bound");
        int256 cash = e.cash();
        vm.roll(block.number + 1);
        _mark(1_060_000);
        vm.etch(BBO, type(Silent).runtimeCode);
        vm.expectEmit(address(e));
        emit PaperExam.TriggerFilled(BTC, true, 30, 1_060_000, 0, 1_050_000, e.SOURCE_ORACLE_FALLBACK(), block.number);
        stopBook.poke();
        assertEq(e.cash(), cash + 30 * 1_060_000, "no answer: at the oracle");
        assertEq(e.positionOf(BTC), 30);
    }

    // ── the book's records follow the exam's ─────────────────────

    function test_records_goWithAClose_aTurnAndAReplacement() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_100_000, 0);
        _set(e, BTC, false, 960_000, 0); // a replacement: the old record goes
        (,, uint256 waiting) = (0, 0, 0);
        (, waiting) = stopBook.watched();
        assertEq(waiting, 2, "one stop, one take");
        assertEq(stopBook.slotOf(address(e), BTC, false).level, 960_000);
        _trade(e, BTC, false, 160); // turned round: both go
        assertEq(_where(e, BTC, false) + _where(e, BTC, true), 0);
        (uint32[] memory assets, uint256 w) = stopBook.watched();
        assertEq(assets.length + w, 0, "nothing left in the book");
        _set(e, BTC, false, 1_100_000, 0);
        vm.roll(block.number + DELAY);
        stopBook.poke();
        (assets,) = stopBook.watched();
        assertEq(assets.length, 1);
        _trade(e, BTC, true, 100); // closed by an order
        assertEq(_where(e, BTC, false), 0);
        (assets, w) = stopBook.watched();
        assertEq(assets.length + w, 0, "an empty asset leaves the list");
    }

    /// A fill closes the position, and the take on it: the exam lets go of the take without calling the book, and the
    /// book, once the exam has answered `CLOSED`, takes the take out of its other heap with its own gas.
    function test_aStopThatCloses_theBookDropsTheTake_afterTheCall() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_050_000, 0);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, false, 940_000, block.number, 2); // CLOSED
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        assertEq(_px(e, BTC, true), 0);
        assertEq(_where(e, BTC, true), 0, "the take is out of the book too");
        (uint32[] memory assets, uint256 w) = stopBook.watched();
        assertEq(assets.length + w, 0);
    }

    /// The same the other way: a take that closes the position takes the stop with it, out of the book.
    function test_aTakeThatCloses_theBookDropsTheStop_afterTheCall() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _set(e, BTC, true, 1_050_000, 0);
        vm.roll(armedAt);
        _mark(1_060_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, true, 1_060_000, block.number, 2); // CLOSED
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        assertEq(_px(e, BTC, false), 0);
        assertEq(_where(e, BTC, false), 0, "the stop is out of the book too");
    }

    // ── more reached than one check carries out ──────────────────

    function _crowd(uint256 n) internal returns (PaperExam[] memory es, uint256 armedAt) {
        es = new PaperExam[](n);
        for (uint256 i = 0; i < n; ++i) {
            es[i] = _exam();
            _trade(es[i], BTC, true, 100);
            _set(es[i], BTC, false, uint64(950_000 + i), 0);
        }
        armedAt = block.number + DELAY;
    }

    /// One more than a check carries out: the last is latched with the price of its touch and carried out by the next
    /// check at that price, though the price has come back; until then its exam refuses to act when the gas left
    /// cannot carry it out, and carries it out first when it can.
    function test_overflow_theLastIsLatched_filledLaterAtTheTouchPrice() public {
        uint256 n = stopBook.MAX_FIRES() + 1;
        (PaperExam[] memory es, uint256 armedAt) = _crowd(n);
        vm.roll(armedAt);
        _mark(940_000);
        uint256 touch = block.number;
        vm.expectEmit(address(stopBook));
        emit StopBook.Latched(address(es[0]), BTC, false, 940_000, touch); // the lowest stop, last off the heap
        stopBook.poke();
        for (uint256 i = 1; i < n; ++i) {
            assertEq(es[i].positionOf(BTC), 0);
        }
        assertEq(es[0].positionOf(BTC), 100, "latched, not yet carried out");
        assertEq(stopBook.pending(address(es[0])), 1);
        assertEq(_where(es[0], BTC, false), 5);
        // The exam acts only once it is carried out: with too little gas for that, it refuses.
        vm.prank(trader);
        vm.expectRevert(PaperExam.StopsPending.selector);
        es[0].place{gas: 190_000}(ETH, true, 10);
        vm.roll(touch + 1);
        _mark(MARK); // back where it was
        (,, uint64 armed0) = es[0].stopOf(BTC);
        vm.expectEmit(address(es[0]));
        emit PaperExam.Filled(BTC, false, 100, 940_000, 0, touch - armed0, 0); // late to its touch, not to the fill
        vm.expectEmit(address(es[0]));
        emit PaperExam.TriggerFilled(BTC, false, 100, 940_000, 0, 950_000, 0, touch);
        stopBook.poke();
        assertEq(es[0].cash(), 94e6, "at the price of its touch, not of the block it was carried out in");
        assertEq(stopBook.pending(address(es[0])), 0);
    }

    /// The same, carried out by the exam's own call: `settle` carries out its latched stop before the exam acts.
    function test_overflow_theExamsOwnCallCarriesItOutFirst() public {
        uint256 n = stopBook.MAX_FIRES() + 1;
        (PaperExam[] memory es, uint256 armedAt) = _crowd(n);
        vm.roll(armedAt);
        _mark(940_000);
        stopBook.poke();
        _mark(MARK);
        vm.prank(trader);
        es[0].place(ETH, true, 10); // same block: the check is done, the latch is carried out
        assertEq(es[0].positionOf(BTC), 0);
        assertEq(es[0].cash(), 94e6);
    }

    /// Past the fills and the latches of one check: the rest stays on its heap, said, for the next check at its price.
    function test_overflow_pastTheLatches_theRestWaitsForTheNextCheck() public {
        uint256 n = stopBook.MAX_FIRES() + stopBook.MAX_LATCHES() + 1;
        (PaperExam[] memory es, uint256 armedAt) = _crowd(n);
        vm.roll(armedAt);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.TouchBacklog(BTC, true);
        stopBook.poke();
        assertEq(es[0].positionOf(BTC), 100);
        assertEq(_where(es[0], BTC, false), 3, "still on the heap");
        vm.roll(armedAt + 1);
        _mark(930_000);
        stopBook.poke(); // the latches first, then the heap at this price: latched there, the fills are spent
        assertEq(stopBook.slotOf(address(es[0]), BTC, false).px, 930_000, "touched by the next check, at its price");
        for (uint256 i = 2; i < 6; ++i) {
            vm.roll(armedAt + i);
            stopBook.poke();
        }
        for (uint256 i = 0; i < n; ++i) {
            assertEq(es[i].positionOf(BTC), 0);
        }
        assertEq(es[0].cash(), 93e6, "the next check's price");
    }

    /// More entries come due in one block than one check arms: the rest wait, said, and are armed by the next check.
    function test_armingBacklog_theRestIsArmedNextCheck() public {
        uint256 n = stopBook.MAX_ARMS() + 1;
        PaperExam[] memory es = new PaperExam[](n);
        for (uint256 i = 0; i < n; ++i) {
            es[i] = _exam();
            _trade(es[i], BTC, true, 100);
        }
        for (uint256 i = 0; i < n; ++i) {
            _set(es[i], BTC, false, 900_000, 0); // all in one block
        }
        vm.roll(block.number + DELAY);
        vm.expectEmit(address(stopBook));
        emit StopBook.ArmingBacklog(1);
        stopBook.poke();
        (, uint256 waiting) = stopBook.watched();
        assertEq(waiting, 1);
        vm.roll(block.number + 1);
        stopBook.poke();
        (, waiting) = stopBook.watched();
        assertEq(waiting, 0);
    }

    // ── one exam does not stop the check ─────────────────────────

    function test_anEndedExam_itsStopIsRemoved_theCheckGoesOn() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        PaperExam other = _exam();
        _trade(other, BTC, true, 100);
        _set(other, BTC, false, 945_000, 0);
        vm.warp(block.timestamp + 8 days); // past the step's deadline
        vm.roll(armedAt + 10);
        _mark(940_000);
        vm.expectEmit(address(e));
        emit PaperExam.TriggerCleared(BTC, false);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, false, 940_000, block.number, 3); // ENDED
        stopBook.poke();
        assertEq(_px(e, BTC, false), 0);
        assertEq(e.positionOf(BTC), 100, "an exam out of time fills nothing");
        e.expire();
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Expired));
    }

    function test_aFailingExam_isReported_andTheOthersAreFilled() public {
        BadExam rev = new BadExam(stopBook, false);
        BadExam burn = new BadExam(stopBook, true);
        rev.arm(BTC, true, 990_000, uint64(block.number + 1));
        burn.arm(BTC, true, 980_000, uint64(block.number + 1));
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(rev), BTC, false, 940_000, block.number, 0); // FAILED
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(burn), BTC, false, 940_000, block.number, 0);
        uint256 before = gasleft();
        stopBook.poke();
        assertLt(before - gasleft(), 2 * stopBook.FIRE_GAS() + 400_000, "a call that burns gas burns its cap only");
        assertEq(e.positionOf(BTC), 0);
    }

    /// Whoever calls cannot starve the check and take its block: a call with less than `POKE_GAS` reverts, the block is
    /// still unchecked, and the next call checks it at its price.
    function test_tooLittleGas_reverts_theBlockStaysForTheNextCall() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt);
        _mark(940_000);
        uint256 needed = stopBook.POKE_GAS();
        uint256 last = stopBook.lastPokeBlock();
        vm.expectRevert(abi.encodeWithSelector(StopBook.PokeGas.selector, needed));
        stopBook.poke{gas: needed}();
        assertEq(stopBook.lastPokeBlock(), last, "the block is not taken");
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(StopBook.PokeGas.selector, needed));
        e.place{gas: needed}(ETH, true, 10); // the exam's own call checks the block first: the same
        stopBook.poke{gas: needed + 20_000}(); // just over it: the whole check, the stop carried out
        assertEq(stopBook.pending(address(e)), 0);
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6);
    }

    /// A price of zero: the asset is skipped and said, and its entries wait.
    function test_noPrice_skipsTheAsset() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt);
        _mark(0);
        vm.expectEmit(address(stopBook));
        emit StopBook.NoPrice(BTC);
        stopBook.poke();
        assertEq(_where(e, BTC, false), 3);
        vm.roll(armedAt + 1);
        vm.etch(ORACLE, type(Silent).runtimeCode);
        vm.expectEmit(address(stopBook));
        emit StopBook.NoPrice(BTC);
        stopBook.poke();
        assertEq(_where(e, BTC, false), 3);
        vm.etch(ORACLE, type(OracleFollowsMark).runtimeCode);
        vm.roll(armedAt + 2);
        _mark(940_000);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
    }

    // ── who may call what ────────────────────────────────────────

    function test_onlyTheBookFires_onlyExamsAddAndRemove() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotBook.selector);
        e.fireTrigger(BTC, false, 950_000, uint64(armedAt), 900_000, uint64(block.number));
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotBook.selector);
        e.fillTrigger(BTC, false);
        vm.prank(address(stopBook));
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.fireTrigger(BTC, false, 950_001, uint64(armedAt), 900_000, uint64(block.number)); // not this stop
        vm.prank(stranger);
        vm.expectRevert(StopBook.NotExam.selector);
        stopBook.add(BTC, false, true, 1, 1);
        vm.prank(stranger);
        vm.expectRevert(StopBook.NotExam.selector);
        stopBook.remove(BTC, false);
        vm.prank(address(this)); // the factory's register names this contract a pool, not an exam
        vm.expectRevert(StopBook.NotExam.selector);
        stopBook.add(BTC, false, true, 1, 1);
    }

    /// The exam's call comes back into the same exam through the book, before it reads anything: the account is right.
    function test_reentry_placeThenTheBookFillsTheSameExam_theAccountIsRight() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _set(e, BTC, true, 1_200_000, 0);
        vm.roll(armedAt);
        _mark(940_000);
        vm.prank(trader);
        e.place(BTC, true, 50); // the stop closes 100 at 940 000; the order waits on a flat position
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6);
        (, , uint64 sz,) = e.pending();
        assertEq(sz, 50);
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        e.fill();
        assertEq(e.positionOf(BTC), 50);
        assertEq(e.cash(), 94e6 - 50 * 940_000);
    }

    // ── the start ────────────────────────────────────────────────

    /// An exam with a filler has the stop book of its pool's factory, and cannot start without one; an exam without a
    /// filler has none.
    function test_start_aFillerMeansABook_noFillerNoBook() public {
        PaperExam e = _exam();
        assertEq(e.stopBook(), address(stopBook));
        PaperExam x = PaperExam(Clones.clone(address(impl)));
        NoBookPool nb = new NoBookPool();
        vm.expectRevert(PaperExam.BadTerms.selector);
        nb.start(x, trader, _rules(_two()), _terms(), filler);
        PaperExam y = PaperExam(Clones.clone(address(impl)));
        NotAPool np = new NotAPool();
        vm.expectRevert();
        np.start(y, trader, _rules(_two()), _terms(), filler);
        PaperExam z = PaperExam(Clones.clone(address(impl)));
        z.initialize(trader, _rules(_two()), _terms());
        assertEq(z.stopBook(), address(0));
        assertEq(z.filler(), address(0));
    }

    // ── the independent review of cp#335 (20 November 2026) ─────

    /// H1: the take of a position sits on a big heap, and the stop closes the position. The exam lets go of the take
    /// without calling the book, and the book takes it out after the call with its own gas: the fill is done inside
    /// `FIRE_GAS` whatever the heaps hold, the stop is carried out, and the take leaves both.
    function _bigHeapSibling(uint256 n, bool fromBook) internal {
        for (uint256 i = 0; i < n; ++i) {
            PaperExam o = _examOn(_two(), fromBook);
            _trade(o, BTC, true, 100);
            _set(o, BTC, true, uint64(1_050_000 + i), 0);
        }
        PaperExam v = _examOn(_two(), fromBook);
        _trade(v, BTC, true, 100);
        _set(v, BTC, false, 950_000, 0);
        _set(v, BTC, true, 1_000_100, 0); // the lowest take: the top of the above heap
        vm.roll(block.number + DELAY);
        stopBook.poke();
        vm.roll(block.number + 1);
        _mark(940_000);
        vm.cool(address(stopBook));
        vm.cool(address(v));
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(v), BTC, false, 940_000, block.number, 2); // CLOSED
        stopBook.poke();
        assertEq(v.positionOf(BTC), 0, "the stop is carried out");
        assertEq(_px(v, BTC, true), 0);
        assertEq(_where(v, BTC, true), 0, "and the take is out of the book");
        assertEq(_where(v, BTC, false), 0);
        (,,, uint256 nAbove) = stopBook.top(BTC);
        assertEq(nAbove, n, "the others' takes stay");
    }

    function test_review_H1_aBigHeap_256_atTheBook() public {
        _bigHeapSibling(256, true);
    }

    function test_review_H1_aBigHeap_1024() public {
        _bigHeapSibling(1024, false);
    }

    /// H1: the gas of an exam's `fireTrigger`, cold, at the book, a stop that closes the position and takes the take
    /// with it: what it needs of `FIRE_GAS`, printed with `-vv`. It calls nothing in the book, so no heap weighs on it.
    function test_review_H1_fireTriggerGas_underHalfItsCap() public {
        PaperExam v = _examOn(_two(), true);
        _trade(v, BTC, true, 100);
        _set(v, BTC, false, 950_000, 0);
        _set(v, BTC, true, 1_050_000, 0);
        (,, uint64 armedAt) = v.stopOf(BTC);
        vm.roll(armedAt);
        _mark(940_000);
        vm.cool(address(v));
        vm.prank(address(stopBook));
        uint256 g = gasleft();
        uint8 r = v.fireTrigger(BTC, false, 950_000, armedAt, 940_000, uint64(block.number));
        uint256 used = g - gasleft();
        emit log_named_uint("fireTrigger, cold, at the book, closes the position", used);
        assertEq(r, 2);
        assertLt(used, stopBook.FIRE_GAS() / 2);
    }

    /// H1: a fill whose call fails, whatever the reason, leaves the stop latched at the price and block of its touch,
    /// never erased; nobody takes it back meanwhile, and once the call goes through it is carried out at that price.
    function test_review_H1_aFailedCall_latchesAtTheTouch_neverErases() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt);
        _mark(940_000);
        uint256 touch = block.number;
        vm.mockCallRevert(address(e), abi.encodeWithSelector(PaperExam.fireTrigger.selector), "out of gas");
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, false, 940_000, touch, 0); // FAILED
        vm.expectEmit(address(stopBook));
        emit StopBook.Latched(address(e), BTC, false, 940_000, touch);
        stopBook.poke();
        StopBook.Slot memory s = stopBook.slotOf(address(e), BTC, false);
        assertEq(s.where, 5, "latched, not erased");
        assertEq(s.px, 940_000);
        assertEq(s.touchBlock, touch);
        assertEq(e.positionOf(BTC), 100);
        vm.prank(trader);
        vm.expectRevert(PaperExam.StopsPending.selector);
        e.clearTrigger(BTC, false);
        vm.roll(touch + 1);
        _mark(MARK);
        stopBook.poke(); // still failing: tried again, still latched
        assertEq(stopBook.pending(address(e)), 1);
        vm.clearMockedCalls();
        vm.roll(touch + 2);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6, "at the price of its touch");
        assertEq(stopBook.pending(address(e)), 0);
        assertEq(_where(e, BTC, false), 0);
    }

    /// H1: an exam whose call keeps failing goes to the back of the latch queue: the exams latched behind it are
    /// carried out, and it is tried again by every check, once a check.
    function test_review_H1_aFailingExam_doesNotHoldUpTheQueue() public {
        BadExam rev = new BadExam(stopBook, false);
        rev.arm(BTC, true, 990_000, uint64(block.number + 1));
        (PaperExam[] memory es, uint256 armedAt) = _crowd(stopBook.MAX_FIRES());
        vm.roll(armedAt);
        _mark(940_000);
        uint256 touch = block.number;
        stopBook.poke(); // `rev` first, the highest: its call fails, latched; two of the crowd carried out, one latched
        assertEq(stopBook.pending(address(rev)), 1);
        assertEq(stopBook.pending(address(es[0])), 1);
        vm.roll(touch + 1);
        _mark(MARK);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(rev), BTC, false, 940_000, touch, 0);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(es[0]), BTC, false, 940_000, touch, 2);
        vm.recordLogs();
        stopBook.poke();
        assertEq(_count(vm.getRecordedLogs(), address(rev)), 1, "once a check");
        assertEq(es[0].cash(), 94e6);
        assertEq(stopBook.pending(address(rev)), 1, "still latched");
        vm.roll(touch + 2);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(rev), BTC, false, 940_000, touch, 0); // tried again
        stopBook.poke();
    }

    /// H1: an exam calls nothing in the book while the book carries out its stop: `add` and `remove` refuse inside a
    /// check, the call fails, and the entry stays latched at its touch, the heaps as they were.
    function test_review_H1_anExamCannotCallTheBookFromItsFill() public {
        BackCaller x = new BackCaller(stopBook);
        x.arm(BTC, true, 990_000, uint64(block.number + 1));
        vm.roll(block.number + 1);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(x), BTC, false, 940_000, block.number, 0); // FAILED
        stopBook.poke();
        assertEq(stopBook.slotOf(address(x), BTC, false).where, 5, "latched");
        (, uint256 waiting) = stopBook.watched();
        assertEq(waiting, 0, "nothing written in the middle of the check");
    }

    function _count(Vm.Log[] memory logs, address exam) internal pure returns (uint256 n) {
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == StopBook.Fired.selector && address(uint160(uint256(logs[i].topics[1]))) == exam) {
                ++n;
            }
        }
    }

    /// H2: a stop or a take the oracle price has already reached is refused, all four, at the level too, as an exchange
    /// refuses a stop that would trigger at once; a tick short of it is taken.
    function test_review_H2_aTriggerPastThePrice_isRefused() public {
        PaperExam l = _exam();
        _trade(l, BTC, true, 100);
        PaperExam s = _exam();
        _trade(s, BTC, false, 100);
        _refused(l, false, MARK);
        _refused(l, false, type(uint64).max);
        _refused(l, true, MARK);
        _refused(l, true, 1);
        _refused(s, false, MARK);
        _refused(s, false, 1);
        _refused(s, true, MARK);
        _refused(s, true, type(uint64).max);
        _set(l, BTC, false, MARK - 1, 0);
        _set(l, BTC, true, MARK + 1, 0);
        _set(s, BTC, false, MARK + 1, 0);
        _set(s, BTC, true, MARK - 1, 0);
        assertEq(_where(l, BTC, false) * _where(l, BTC, true) * _where(s, BTC, false) * _where(s, BTC, true), 2 * 1 * 2 * 1);
    }

    function _refused(PaperExam e, bool isTake, uint64 px) internal {
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.WouldTrigger.selector, MARK, px));
        e.setTrigger(BTC, isTake, px, 1);
    }

    /// H2, the review's flood: 30 exams try every block to keep on the heap entries the price has always reached, to
    /// take a check's fills and latches; they are refused, and the victim's stop is carried out in its one-block dip.
    function test_review_H2_aFloodOfAlwaysReachedEntries_isRefused_theVictimIsFilled() public {
        PaperExam v = _exam();
        _trade(v, BTC, true, 100);
        _set(v, BTC, false, 950_000, 0);
        uint256 n = 30;
        PaperExam[] memory a = new PaperExam[](n);
        for (uint256 i = 0; i < n; ++i) {
            a[i] = _exam();
            _trade(a[i], BTC, true, 100);
        }
        uint256 start = block.number;
        uint256 refused;
        for (uint256 b = 0; b < 12; ++b) {
            vm.roll(start + b);
            _mark(b == 6 ? 940_000 : MARK);
            stopBook.poke();
            for (uint256 i = 0; i < n; ++i) {
                vm.prank(trader);
                try a[i].setTrigger(BTC, false, type(uint64).max, 1) {}
                catch {
                    ++refused;
                }
            }
        }
        assertEq(refused, 12 * n);
        assertEq(v.positionOf(BTC), 0, "the victim's stop, carried out in the dip");
        assertEq(v.cash(), 94e6, "at the dip's price");
    }

    /// M1, the review's way to it: a poor first call in every block kept a touched stop latched, and the take touched
    /// later was carried out before it. Such a call now reverts and takes no block: the stop is carried out in its own.
    function test_review_M1_aPoorFirstCall_cannotHoldAStop() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_050_000, 0);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        bytes memory poor = abi.encodeWithSelector(StopBook.PokeGas.selector, stopBook.POKE_GAS());
        vm.expectRevert(poor);
        stopBook.poke{gas: 244_500}();
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6, "the stop at its touch, not the take");
        assertEq(_where(e, BTC, true), 0);
    }

    /// M1: one exam's latched stop and take go oldest touch first. The stop is latched in block t behind a crowd that
    /// takes the check's fills; in t+1 the crowd ahead of it in the queue takes them again, and its take is touched
    /// and latched; in t+2 the stop is carried out first, at its touch, and the take goes with the closed position.
    function test_review_M1_latchesGoOldestTouchFirst() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_050_000, 0);
        uint256 c = stopBook.MAX_FIRES() + 4;
        for (uint256 i = 0; i < c; ++i) {
            PaperExam x = _exam();
            _trade(x, BTC, true, 100);
            _set(x, BTC, false, uint64(960_000 + i), 0);
        }
        vm.roll(block.number + DELAY);
        _mark(940_000);
        uint256 t = block.number;
        stopBook.poke();
        assertEq(stopBook.pending(address(e)), 1, "the stop, latched in t");
        vm.roll(t + 1);
        _mark(1_060_000);
        stopBook.poke();
        assertEq(stopBook.pending(address(e)), 2, "and the take, in t+1");
        vm.roll(t + 2);
        _mark(MARK);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, false, 940_000, t, 2);
        stopBook.poke();
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6, "the stop's loss, not the take's profit");
        assertEq(stopBook.pending(address(e)), 0);
        assertEq(_where(e, BTC, true), 0);
    }

    /// M1: an exam with a touch still latched has every later touch latched behind it, never carried out first, even
    /// with fills to spare: here its stop's call fails in t and again in t+1, when its take is touched; in t+2 the
    /// stop goes through first.
    function test_review_M1_aLaterTouch_waitsBehindTheLatchedOne() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_050_000, 0);
        (,, uint64 armedAt) = e.stopOf(BTC);
        vm.roll(armedAt);
        _mark(940_000);
        uint256 t = block.number;
        vm.mockCallRevert(
            address(e), abi.encodeCall(PaperExam.fireTrigger, (BTC, false, 950_000, armedAt, 940_000, uint64(t))), ""
        );
        stopBook.poke();
        vm.roll(t + 1);
        _mark(1_060_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Latched(address(e), BTC, true, 1_060_000, t + 1);
        stopBook.poke();
        assertEq(stopBook.pending(address(e)), 2);
        vm.clearMockedCalls();
        vm.roll(t + 2);
        _mark(MARK);
        stopBook.poke();
        assertEq(e.cash(), 94e6, "the stop first");
        assertEq(_where(e, BTC, true), 0);
    }

    /// L1: an exam that ends, by `expire` or by `breach`, takes its stops and takes out of the book.
    function test_review_L1_anEndedExam_leavesTheBook() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _trade(e, ETH, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_050_000, 0);
        _set(e, ETH, false, 240_000, 0);
        PaperExam b = _exam();
        _trade(b, BTC, true, 100);
        _set(b, BTC, true, 1_050_000, 0);
        vm.roll(block.number + DELAY);
        stopBook.poke();
        vm.warp(block.timestamp + 8 days);
        vm.roll(block.number + 1);
        e.expire();
        assertEq(_where(e, BTC, false) + _where(e, BTC, true) + _where(e, ETH, false), 0, "expired: out of the book");
        assertEq(_px(e, BTC, false) + _px(e, BTC, true) + _px(e, ETH, false), 0);
        assertEq(_where(b, BTC, true), 4);
        vm.warp(block.timestamp - 8 days);
        vm.roll(block.number + 1);
        _mark(400_000); // 60 lost of 100: past the drawdown, the take untouched
        b.breach();
        assertEq(_where(b, BTC, true), 0, "breached: out of the book");
        (uint32[] memory assets, uint256 w) = stopBook.watched();
        assertEq(assets.length + w, 0);
    }

    // ── gas ──────────────────────────────────────────────────────

    /// The check with entries on 0, 1, 5 and 16 assets and nothing reached, a second check in the block, one fill, and
    /// a check at its caps; printed with `-vv`. Without the 21 000 of a transaction. The oracle here is the
    /// simulator's: on HyperEVM its precompile costs what it costs there.
    function test_gas_ofAChecks() public {
        uint32[] memory all = new uint32[](16);
        for (uint32 i = 0; i < 16; ++i) {
            all[i] = 100 + i;
            _asset(100 + i, 5, MARK);
        }
        PaperExam e = _examOn(all, false);
        vm.roll(block.number + 1);
        emit log_named_uint("poke, 0 assets", _pokeGas(e));
        uint256[3] memory counts = [uint256(1), 5, 16];
        uint256 done;
        for (uint256 k = 0; k < 3; ++k) {
            for (; done < counts[k]; ++done) {
                _trade(e, all[done], true, 10);
                _set(e, all[done], false, 900_000, 0);
            }
            vm.roll(block.number + DELAY);
            stopBook.poke(); // arms them
            vm.roll(block.number + 1);
            emit log_named_uint(string.concat("poke, ", vm.toString(counts[k]), " assets"), _pokeGas(e));
        }
        uint256 g = gasleft();
        stopBook.poke();
        emit log_named_uint("poke, again in the block", g - gasleft());
        assertLt(g - gasleft(), 5_000);
        vm.roll(block.number + 1);
        CoreSimulatorLib.setMarkPx(all[0], 890_000);
        uint256 one = _pokeGas(e);
        emit log_named_uint("poke, 16 assets, one fill", one);
        assertLt(one, 500_000);
    }

    /// `MAX_ARMS` exams long on ETH, with no stop yet.
    function _armers() internal returns (PaperExam[] memory es) {
        es = new PaperExam[](stopBook.MAX_ARMS());
        for (uint256 i = 0; i < es.length; ++i) {
            es[i] = _exam();
            _trade(es[i], ETH, true, 10);
        }
    }

    /// Their stops, all set in this block: all due together.
    function _arm(PaperExam[] memory es) internal {
        for (uint256 i = 0; i < es.length; ++i) {
            _set(es[i], ETH, false, 100_000, 0);
        }
    }

    /// A check at its caps, cold: `MAX_FIRES` fills, `MAX_LATCHES` latches, `MAX_ARMS` armed. `POKE_GAS` covers it.
    function test_gas_ofACheckAtItsCaps_underASmallBlock() public {
        uint256 n = stopBook.MAX_FIRES() + stopBook.MAX_LATCHES() + 1;
        (PaperExam[] memory crowd, uint256 armedAt) = _crowd(n);
        PaperExam[] memory armers = _armers();
        if (block.number < armedAt) vm.roll(armedAt);
        stopBook.poke();
        _arm(armers);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        vm.cool(address(stopBook));
        for (uint256 i = 0; i < n; ++i) {
            vm.cool(address(crowd[i]));
        }
        vm.recordLogs();
        uint256 g = gasleft();
        stopBook.poke();
        uint256 used = g - gasleft();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256[3] memory seen; // fired, latched, backlog
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == StopBook.Fired.selector) ++seen[0];
            if (logs[i].topics[0] == StopBook.Latched.selector) ++seen[1];
            if (logs[i].topics[0] == StopBook.TouchBacklog.selector) ++seen[2];
        }
        assertEq(seen[0], stopBook.MAX_FIRES());
        assertEq(seen[1], stopBook.MAX_LATCHES());
        assertEq(seen[2], 1);
        (, uint256 waiting) = stopBook.watched();
        assertEq(waiting, 0, "all armed");
        emit log_named_uint("poke at its caps: 3 fills, 6 latches, 8 armed", used);
        assertLt(used, stopBook.POKE_GAS(), "what POKE_GAS asks for covers it");
        assertLt(used + 21_000, 2_000_000, "inside a small block of 3 000 000, with room");
    }

    /// M2: the worst a trader's call pays when it is the first of its block: the check at its caps (three exams ahead
    /// of it in the latch queue take the fills, `MAX_LATCHES` latched, `MAX_ARMS` armed), then two of its own latched
    /// stops carried out by its `settle`, then its own `place`. Cold; under 3 000 000 with room.
    function test_gas_aTradersFirstCall_paysTheCheckAtItsCaps_andTwoLatches() public {
        PaperExam[] memory q = new PaperExam[](3);
        for (uint256 i = 0; i < 3; ++i) {
            q[i] = _exam();
            _trade(q[i], BTC, true, 100);
            _set(q[i], BTC, false, uint64(990_000 - i), 0);
        }
        PaperExam x = _exam();
        _trade(x, BTC, true, 100);
        _trade(x, ETH, true, 100);
        _set(x, BTC, false, 975_000, 0);
        _set(x, ETH, false, 247_000, 0);
        (PaperExam[] memory crowd, uint256 armedAt) = _crowd(stopBook.MAX_LATCHES() + 1); // 950 000 and up
        PaperExam[] memory armers = _armers();
        if (block.number < armedAt) vm.roll(armedAt);
        stopBook.poke();
        // Block a: the queue's three are touched, fail and are latched; in a+1 they fail again, and take the fills
        // while `x`'s two are touched: those are latched behind them.
        for (uint256 i = 0; i < 3; ++i) {
            vm.mockCallRevert(address(q[i]), abi.encodeWithSelector(PaperExam.fireTrigger.selector), "");
        }
        vm.roll(block.number + 1);
        _mark(980_000);
        stopBook.poke();
        vm.roll(block.number + 1);
        _mark(970_000);
        CoreSimulatorLib.setMarkPx(ETH, 245_000);
        stopBook.poke();
        assertEq(stopBook.pending(address(x)), 2);
        _arm(armers); // due in block t
        vm.clearMockedCalls();
        vm.roll(block.number + DELAY); // t: nobody checked in between
        _mark(940_000); // the crowd touched: no fills left for it
        vm.cool(address(stopBook));
        vm.cool(address(x));
        for (uint256 i = 0; i < 3; ++i) {
            vm.cool(address(q[i]));
        }
        for (uint256 i = 0; i < crowd.length; ++i) {
            vm.cool(address(crowd[i]));
        }
        vm.recordLogs();
        vm.prank(trader);
        uint256 g = gasleft();
        x.place(ETH, false, 10);
        uint256 used = g - gasleft();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256[2] memory seen; // fired, latched
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == StopBook.Fired.selector) ++seen[0];
            if (logs[i].topics[0] == StopBook.Latched.selector) ++seen[1];
        }
        assertEq(seen[0], stopBook.MAX_FIRES() + 2, "the check's fills and x's two");
        assertEq(seen[1], stopBook.MAX_LATCHES());
        (, uint256 waiting) = stopBook.watched();
        assertEq(waiting, 0, "all armed");
        emit log_named_uint("first call of the block: check at its caps, two latches, place", used);
        assertEq(stopBook.pending(address(x)), 0);
        assertEq(x.positionOf(BTC), 0);
        assertLt(used + 21_000, 2_500_000, "inside a small block of 3 000 000, with room");
    }

    /// L1: what `expire` costs an exam on 16 assets with a stop and a take on each, 32 entries leaving the book.
    function test_gas_expireOf32Entries() public {
        uint32[] memory all = new uint32[](16);
        for (uint32 i = 0; i < 16; ++i) {
            all[i] = 100 + i;
            _asset(100 + i, 5, MARK);
        }
        PaperExam e = _examOn(all, false);
        for (uint256 i = 0; i < 16; ++i) {
            _trade(e, all[i], true, 10);
            vm.startPrank(trader);
            e.setTrigger(all[i], false, 900_000, 0);
            e.setTrigger(all[i], true, 1_100_000, 0);
            vm.stopPrank();
        }
        vm.roll(block.number + DELAY);
        stopBook.poke();
        vm.warp(block.timestamp + 8 days);
        vm.roll(block.number + 1);
        stopBook.poke();
        vm.cool(address(stopBook));
        vm.cool(address(e));
        uint256 g = gasleft();
        e.expire();
        uint256 used = g - gasleft();
        emit log_named_uint("expire, 32 entries out of the book", used);
        (uint32[] memory assets, uint256 w) = stopBook.watched();
        assertEq(assets.length + w, 0);
        assertLt(used + 21_000, 2_500_000);
    }

    function _pokeGas(PaperExam e) internal returns (uint256 used) {
        vm.cool(address(stopBook));
        vm.cool(address(e));
        uint256 g = gasleft();
        stopBook.poke();
        used = g - gasleft();
    }
}

/// A pool whose factory has no stop book.
contract NoBookPool {
    function factory() external view returns (address) {
        return address(this);
    }

    function stopBook() external pure returns (address) {
        return address(0);
    }

    function start(PaperExam e, address trader, Rules calldata r, ExamTerms calldata t, address filler) external {
        e.initializeWithFiller(trader, r, t, filler, false, 0, 0, false);
    }
}

/// A caller that is no simulated pool at all.
contract NotAPool {
    function start(PaperExam e, address trader, Rules calldata r, ExamTerms calldata t, address filler) external {
        e.initializeWithFiller(trader, r, t, filler, false, 0, 0, false);
    }
}
