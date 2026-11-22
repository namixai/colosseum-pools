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

/// The stop book with two hands for tests: the journal moved on by positions that log nothing, and a price written at
/// a position of it with the runs above it, as checks would have written them. For the gas of a journal a month long.
contract StopBookHarness is StopBook {
    function first(uint32 asset, uint256 s, uint256 len, uint64 level, bool below) external view returns (uint256) {
        return _find(asset, s, len, below ? level : 0, below ? NO_LEVEL : level);
    }

    function find(uint32 asset, uint256 s, uint256 len, uint256 lo, uint256 hi) external view returns (uint256) {
        return _find(asset, s, len, lo, hi);
    }

    function off(uint32 asset) external pure returns (uint256) {
        return _off(asset);
    }

    function raw(uint32 asset, uint256 q) external view returns (uint64) {
        return _raw(asset, q);
    }

    /// Run `j` of level `k`: the codes of its lowest and highest prices.
    function run(uint32 asset, uint256 k, uint256 j) external view returns (uint256 lo, uint256 hi) {
        uint256 r = uint64(_tree[_key(asset, k, j >> 2)] >> (j % 4 * 64));
        return (uint32(r), r >> 32);
    }


    function skip(uint256 n) external {
        _len = uint64(_len + n);
    }

    function plant(uint32 asset, uint256 pos, uint64 px, uint256 blk, uint256 time) external {
        uint256 sh = pos % 3 * 80;
        _at[pos / 3] = (_at[pos / 3] & ~(((uint256(1) << 80) - 1) << sh)) | ((blk << 40 | time) << sh);
        uint256 q = pos + _off(asset);
        uint256 k0 = _key(asset, 0, q >> 2);
        uint256 s0 = q % 4 * 64;
        _tree[k0] = (_tree[k0] & ~(uint256(type(uint64).max) << s0)) | (uint256(px) << s0);
        uint256 qn = _len + _off(asset);
        for (uint256 k = 1;; ++k) {
            uint256 j = q >> (2 * k);
            if ((j + 1) << (2 * k) > qn) break; // the run still being logged, and every one above it
            _plantRun(asset, k, j, px);
        }
    }

    function _plantRun(uint32 asset, uint256 k, uint256 j, uint64 px) internal {
        uint256 key = _key(asset, k, j >> 2);
        uint256 sh = j % 4 * 64;
        uint256 r = uint64(_tree[key] >> sh);
        uint256 lo = px;
        uint256 hi = px;
        if (r != 0) {
            if (uint32(r) < lo) lo = uint32(r);
            if (r >> 32 > hi) hi = r >> 32;
        }
        _tree[key] = (_tree[key] & ~(uint256(type(uint64).max) << sh)) | ((hi << 32 | lo) << sh);
    }
}

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

    function fireTrigger(uint32, bool, uint64, uint64, uint64, uint32, uint64, uint64) external view returns (uint8) {
        if (!burns) revert("no");
        uint256 x;
        while (gasleft() > 0) ++x;
        return x == 0 ? 1 : 0;
    }
}

/// An exam whose fill needs `need` gas: fewer and it runs out; given it, it fills.
contract GreedyExam {
    StopBook immutable b;
    uint256 immutable need;

    constructor(StopBook b_, uint256 need_) {
        b = b_;
        need = need_;
    }

    function arm(uint32 asset, bool below, uint64 level, uint64 armedAt) external {
        b.add(asset, false, below, level, armedAt);
    }

    function fireTrigger(uint32, bool, uint64, uint64, uint64, uint32, uint64, uint64) external view returns (uint8) {
        uint256 until = gasleft() - need;
        uint256 x;
        while (gasleft() > until) ++x;
        return x == 0 ? 0 : 1;
    }
}

/// An exam whose `fireTrigger` calls the book back: it sets a stop on the next asset in the middle of a settle.
contract BackCaller {
    StopBook immutable b;

    constructor(StopBook b_) {
        b = b_;
    }

    function arm(uint32 asset, bool below, uint64 level, uint64 armedAt) external {
        b.add(asset, false, below, level, armedAt);
    }

    function fireTrigger(uint32 asset, bool, uint64 level, uint64 armedAt, uint64, uint32, uint64, uint64)
        external
        returns (uint8)
    {
        b.add(asset + 1, false, true, level, armedAt + 100);
        return 1;
    }
}

/// A book precompile that answers words no price fits in.
contract Garbage {
    fallback(bytes calldata) external returns (bytes memory) {
        return abi.encode(uint256(900_000), type(uint256).max);
    }
}

/// A precompile that does not answer.
contract Silent {
    fallback() external {
        revert();
    }
}

/// The stop book: a stop or a take the oracle price reaches is carried out at the first logged block, from its
/// `armedAt`, whose oracle price reached it, at that price, and nothing the trader does after that touch comes before
/// it. A check (`poke`) only logs the prices; a settle (`settle`, and every exam function first) carries out.
///
/// BTC stands at 100 000.0 (1 000 000) and an exam starts with 100 USDC: 100 size units long is 100 USDC of notional,
/// and every 1 % of price is 1 USDC. The prices of the precompiles are the simulator's (the oracle follows its mark);
/// the gas of the oracle precompile on HyperEVM is not what it costs here.
abstract contract StopBookBase is Test, BookStand {
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
    /// Every exam a test started: what an executor settles after each check (`_check`).
    PaperExam[] internal made;

    function setUp() public virtual {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        vm.etch(ORACLE, type(OracleFollowsMark).runtimeCode);
        vm.etch(BBO, type(BookStub).runtimeCode);
        _asset(BTC, 5, MARK);
        _asset(ETH, 4, ETH_MARK);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);
        impl = new PaperExam();
        stopBook = new StopBookHarness();
    }

    // ── helpers ──────────────────────────────────────────────────

    function _asset(uint32 a, uint8 szDecimals, uint64 px) internal {
        hyperCore.registerPerpAssetInfo(
            a,
            PrecompileLib.PerpAssetInfo({
                coin: "X", marginTableId: 54, szDecimals: szDecimals, maxLeverage: 40, onlyIsolated: false
            })
        );
        CoreSimulatorLib.setMarkPx(a, px);
    }

    function _rules(uint32[] memory assets) internal pure returns (Rules memory) {
        return Rules({
            dailyLossBps: 5000,
            maxDrawdownBps: 5000,
            maxLeverageX100: 500,
            assets: assets,
            assetLeverageX100: new uint32[](0)
        });
    }

    function _two() internal pure returns (uint32[] memory a) {
        a = new uint32[](2);
        (a[0], a[1]) = (BTC, ETH);
    }

    function _terms() internal pure returns (ExamTerms memory) {
        uint16[] memory t = new uint16[](1);
        t[0] = 1000;
        return ExamTerms({
            capital: 100e6,
            targetsBps: t,
            stepDuration: 7 days,
            maxIdle: 0,
            minTradingDays: 0,
            costPpm: 0,
            fillDelay: DELAY,
            fillWindow: WINDOW
        });
    }

    function _exam() internal returns (PaperExam) {
        return _examOn(_two(), false);
    }

    function _examOn(uint32[] memory assets, bool fromBook) internal returns (PaperExam e) {
        return _examWith(assets, _terms(), fromBook);
    }

    function _examWith(uint32[] memory assets, ExamTerms memory t, bool fromBook) internal returns (PaperExam e) {
        e = PaperExam(Clones.clone(address(impl)));
        e.initializeWithFiller(trader, _rules(assets), t, filler, fromBook, fromBook ? WORSE : 0, 0, false);
        made.push(e);
    }

    function _trade(PaperExam e, uint32 asset, bool isBuy, uint64 sz) internal {
        vm.prank(trader);
        e.place(asset, isBuy, sz);
        vm.roll(block.number + e.terms().fillDelay);
        vm.prank(filler);
        e.fill();
    }

    function _set(PaperExam e, uint32 asset, bool isTake, uint64 px, uint64 sz) internal {
        vm.prank(trader);
        e.setTrigger(asset, isTake, px, sz);
    }

    /// What an executor does each block: checks it, then settles every exam.
    function _check() internal {
        stopBook.poke();
        for (uint256 i = 0; i < made.length; ++i) {
            stopBook.settle(address(made[i]));
        }
    }

    function _harness() internal view returns (StopBookHarness) {
        return StopBookHarness(address(stopBook));
    }

    function _mark(uint64 px) internal {
        CoreSimulatorLib.setMarkPx(BTC, px);
    }

    function _px(PaperExam e, uint32 asset, bool isTake) internal view returns (uint64 px) {
        (px,,) = isTake ? e.takeOf(asset) : e.stopOf(asset);
    }

    /// Whether the book holds `e`'s stop or take on `asset`: 0 none, 3 one that fires at or under its level, 4 at or
    /// over it.
    function _where(PaperExam e, uint32 asset, bool isTake) internal view returns (uint8) {
        StopBook.Entry memory x = stopBook.entryOf(address(e), asset, isTake);
        return x.level == 0 ? 0 : x.below ? 3 : 4;
    }

    /// A long of 100 on BTC with a stop at 950 000, armed `DELAY` blocks on, and the block it arms in.
    function _longWithStop() internal returns (PaperExam e, uint256 armedAt) {
        e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        armedAt = block.number + DELAY;
    }

    /// The `TriggerFilled` events of the logs recorded, in order: exam, asset, isTake, size, touch block.
    function _fills(Vm.Log[] memory logs) internal pure returns (bytes32[] memory out, uint256 n) {
        out = new bytes32[](logs.length);
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] != PaperExam.TriggerFilled.selector) continue;
            (bool isTake, uint64 sz,,,,, uint256 touch,) =
                abi.decode(logs[i].data, (bool, uint64, uint64, uint256, uint64, uint8, uint256, uint64));
            out[n++] = keccak256(abi.encode(logs[i].emitter, uint32(uint256(logs[i].topics[1])), isTake, sz, touch));
        }
    }
}

contract StopBookTest is StopBookBase {
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
        emit PaperExam.TriggerFilled(BTC, false, 100, 940_000, 0, 950_000, 0, touch, 940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, false, 940_000, touch, 2); // CLOSED
        stopBook.settle(address(e));
        vm.roll(touch + 1);
        _mark(MARK); // back over it the next block: nothing to undo
        _check();
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6, "100 units sold at 940 000, the gap's price, not the stop's");
        assertEq(_where(e, BTC, false), 0);
    }

    function test_longTake_atItsLevel_closesItsSizeOnly_andTheStopStays() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _set(e, BTC, true, 1_050_000, 40);
        vm.roll(armedAt);
        _mark(1_049_999);
        _check();
        assertEq(e.positionOf(BTC), 100, "under the take: nothing");
        vm.roll(armedAt + 1);
        _mark(1_050_000); // at it is enough
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, true, 1_050_000, block.number, 1); // FILLED: the position stays open
        stopBook.settle(address(e));
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
        assertEq(_where(e, BTC, false), 4, "a short's stop fires at or above its level");
        assertEq(_where(e, BTC, true), 3, "a short's take at or below");
        vm.roll(block.number + DELAY);
        _mark(960_000); // not yet the take
        _check();
        assertEq(e.positionOf(BTC), -100);
        vm.roll(block.number + 1);
        _mark(950_000);
        _check();
        assertEq(e.positionOf(BTC), -50, "the take bought back half");
        vm.roll(block.number + 1);
        _mark(1_070_000);
        _check();
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
        _check();
        assertEq(e.positionOf(BTC), 100, "a block early");
        assertEq(stopBook.watched().length, 1, "its asset is watched from its setting");
        assertEq(stopBook.entries(address(e)), 1);
        vm.roll(armedAt);
        _check();
        assertEq(e.positionOf(BTC), 0, "in its block");
    }

    /// Blocks nobody checked: the next check says so, arms what came due in them, and checks it at its own price.
    function test_gap_isShown_andWhatArmedInItIsChecked() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _check(); // the stop's own block was checked by `setTrigger`; so is this one, a second time: nothing
        uint256 last = stopBook.lastPokeBlock();
        vm.roll(armedAt + 10);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Gap(last + 1, block.number - 1);
        _check();
        assertEq(e.positionOf(BTC), 0);
        vm.roll(block.number + 1);
        vm.recordLogs();
        _check();
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
        _check();
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
        assertLt(
            stopBook.lastPokeBlock(), block.number, "the refusals undid their checks: this block is still unchecked"
        );
        _check();
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
        _check();
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

    /// Ending one exam checks the block too: another exam's stop reached in it is touched there, and carried out at
    /// that touch by its own settle, whenever it comes.
    function test_touchBlock_expireOfOneExam_logsAnothersTouch() public {
        PaperExam old = _exam();
        vm.warp(block.timestamp + 4 days);
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.warp(block.timestamp + 4 days); // past the first exam's week, not the second's
        vm.roll(armedAt);
        _mark(940_000);
        old.expire();
        uint256 touch = block.number;
        assertEq(e.positionOf(BTC), 100, "one exam's call settles that exam only");
        StopBook.Touch[] memory t = stopBook.touches(address(e));
        assertEq(t.length, 1);
        assertEq(t[0].blk, touch, "the expiry's check logged the touch");
        vm.roll(touch + 5);
        _mark(MARK);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6);
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
        uint256 bound = 940_000 - uint256(940_000) * WORSE / 10_000;
        vm.expectEmit(address(e));
        emit PaperExam.TriggerFilled(BTC, false, 40, uint64(bound), 0, 950_000, 1, block.number, 940_000); // the touch's
        stopBook.settle(address(e));
        assertEq(e.cash(), int256(100e6) - 100 * 1_000_010 + 40 * int256(bound), "sold at the bound");
        int256 cash = e.cash();
        vm.roll(block.number + 1);
        _mark(1_060_000);
        vm.etch(BBO, type(Silent).runtimeCode);
        vm.expectEmit(address(e));
        emit PaperExam.TriggerFilled(
            BTC, true, 30, 1_060_000, 0, 1_050_000, e.SOURCE_ORACLE_FALLBACK(), block.number, 1_060_000
        );
        stopBook.settle(address(e));
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
        assertEq(stopBook.entries(address(e)), 2, "one stop, one take");
        assertEq(stopBook.entryOf(address(e), BTC, false).level, 960_000);
        _trade(e, BTC, false, 160); // turned round: both go
        assertEq(_where(e, BTC, false) + _where(e, BTC, true), 0);
        assertEq(stopBook.watched().length + stopBook.entries(address(e)), 0, "nothing left in the book");
        _set(e, BTC, false, 1_100_000, 0);
        vm.roll(block.number + DELAY);
        _check();
        assertEq(stopBook.watched().length, 1);
        _trade(e, BTC, true, 100); // closed by an order
        assertEq(_where(e, BTC, false), 0);
        assertEq(stopBook.watched().length + stopBook.entries(address(e)), 0, "an empty asset leaves the list");
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
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0);
        assertEq(_px(e, BTC, true), 0);
        assertEq(_where(e, BTC, true), 0, "the take is out of the book too");
        assertEq(stopBook.watched().length + stopBook.entries(address(e)), 0);
    }

    /// The same the other way: a take that closes the position takes the stop with it, out of the book.
    function test_aTakeThatCloses_theBookDropsTheStop_afterTheCall() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _set(e, BTC, true, 1_050_000, 0);
        vm.roll(armedAt);
        _mark(1_060_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(e), BTC, true, 1_060_000, block.number, 2); // CLOSED
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0);
        assertEq(_px(e, BTC, false), 0);
        assertEq(_where(e, BTC, false), 0, "the stop is out of the book too");
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
        stopBook.settle(address(e));
        assertEq(_px(e, BTC, false), 0);
        assertEq(e.positionOf(BTC), 100, "an exam out of time fills nothing");
        e.expire();
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Expired));
    }

    /// An exam whose fill fails holds nobody else: each exam is settled on its own, and the failing one keeps its
    /// entry, said by `Fired` with `FAILED`, to be tried again.
    function test_aFailingExam_isReported_andTheOthersAreFilled() public {
        BadExam rev = new BadExam(stopBook, false);
        BadExam burn = new BadExam(stopBook, true);
        rev.arm(BTC, true, 990_000, uint64(block.number + 1));
        burn.arm(BTC, true, 980_000, uint64(block.number + 1));
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(rev), BTC, false, 940_000, block.number, 0); // FAILED
        assertEq(stopBook.settle(address(rev)), 1, "its entry stays, owed");
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(burn), BTC, false, 940_000, block.number, 4); // STARVED: it spent all it got
        assertEq(stopBook.settle{gas: 1_000_000}(address(burn)), 1, "a call that burns all it gets: the same");
        assertEq(stopBook.entryOf(address(rev), BTC, false).level, 990_000, "never erased");
        assertEq(stopBook.entryOf(address(burn), BTC, false).level, 980_000);
        assertEq(stopBook.settle(address(e)), 0, "the others' failures are nothing to this exam");
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6);
    }

    /// Whoever calls cannot starve the check and take its block: a check too poor to give the oracle its whole gas
    /// reverts, the block is still unchecked, and the next call logs it at its price. A check either logs every
    /// watched asset or nothing.
    function test_tooLittleGas_reverts_theBlockStaysForTheNextCall() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt);
        _mark(940_000);
        uint256 last = stopBook.lastPokeBlock();
        uint256 len = stopBook.logged();
        vm.expectRevert(abi.encodeWithSelector(StopBook.PokeGas.selector, stopBook.ORACLE_GAS()));
        stopBook.poke{gas: 60_000}();
        assertEq(stopBook.lastPokeBlock(), last, "the block is not taken");
        assertEq(stopBook.logged(), len);
        vm.prank(trader);
        vm.expectRevert();
        e.place{gas: 60_000}(ETH, true, 10); // the exam's own call checks the block first: the same
        assertEq(stopBook.logged(), len);
        stopBook.poke();
        assertEq(stopBook.priceAt(BTC, len), 940_000, "logged at its price");
        assertEq(stopBook.settle(address(e)), 0);
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
        _check();
        assertEq(_where(e, BTC, false), 3);
        vm.roll(armedAt + 1);
        vm.etch(ORACLE, type(Silent).runtimeCode);
        vm.expectEmit(address(stopBook));
        emit StopBook.NoPrice(BTC);
        _check();
        assertEq(_where(e, BTC, false), 3);
        vm.etch(ORACLE, type(OracleFollowsMark).runtimeCode);
        vm.roll(armedAt + 2);
        _mark(940_000);
        _check();
        assertEq(e.positionOf(BTC), 0);
    }

    // ── who may call what ────────────────────────────────────────

    function test_onlyTheBookFires_onlyExamsAddAndRemove() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotBook.selector);
        e.fireTrigger(BTC, false, 950_000, uint64(armedAt), 900_000, 0, uint64(block.number), uint64(block.timestamp));
        vm.prank(stranger);
        vm.expectRevert(PaperExam.NotBook.selector);
        e.fillTrigger(BTC, false);
        vm.prank(address(stopBook));
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.fireTrigger(BTC, false, 950_001, uint64(armedAt), 900_000, 0, uint64(block.number), uint64(block.timestamp));
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
        (,, uint64 sz,) = e.pending();
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


    function _fillKey(PaperExam e, uint32 asset, bool isTake, uint64 sz, uint256 touch) internal pure returns (bytes32) {
        return keccak256(abi.encode(address(e), asset, isTake, sz, touch));
    }

    // ── И1: the first logged touch from armedAt, at its price and size ──

    /// A touch before `armedAt` is none; the first logged block from it whose price reached the level fills it, at
    /// that price, in that block's name, at its size; a deeper price after it changes nothing.
    function test_I1_theFirstLoggedTouchFromArmedAt_atItsPriceAndSize() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 30);
        uint256 armedAt = block.number + DELAY;
        vm.roll(armedAt - 1);
        _mark(949_000); // past it a block early: not a touch
        stopBook.poke();
        vm.roll(armedAt);
        _mark(951_000);
        stopBook.poke();
        vm.roll(armedAt + 1);
        _mark(948_000); // the first touch
        stopBook.poke();
        uint256 touch = block.number;
        vm.roll(armedAt + 2);
        _mark(900_000); // deeper: nothing to it
        stopBook.poke();
        vm.roll(armedAt + 9);
        _mark(MARK);
        StopBook.Touch[] memory t = stopBook.touches(address(e));
        assertEq(t.length, 1);
        assertEq(t[0].blk, touch);
        assertEq(t[0].px, 948_000);
        vm.expectEmit(address(e));
        emit PaperExam.Filled(BTC, false, 30, 948_000, 0, 1, 0); // late by one block from `armedAt`: its touch's
        vm.expectEmit(address(e));
        emit PaperExam.TriggerFilled(BTC, false, 30, 948_000, 0, 950_000, 0, touch, 948_000);
        assertEq(stopBook.settle(address(e)), 0);
        assertEq(e.positionOf(BTC), 70, "its size, no more");
        assertEq(e.cash(), 30 * 948_000, "at the first touch's price");
        assertEq(stopBook.touches(address(e)).length, 0);
    }

    /// The price of the touch is the block's own, through a gap: a short's stop over its level fills at the price.
    function test_I1_aGapThroughTheLevel_fillsAtTheTouchPrice_notTheLevel() public {
        PaperExam e = _exam();
        _trade(e, ETH, false, 100);
        _set(e, ETH, false, 260_000, 0);
        vm.roll(block.number + DELAY);
        CoreSimulatorLib.setMarkPx(ETH, 275_000);
        stopBook.poke();
        vm.roll(block.number + 3);
        CoreSimulatorLib.setMarkPx(ETH, ETH_MARK);
        stopBook.settle(address(e));
        assertEq(e.positionOf(ETH), 0);
        assertEq(e.cash(), int256(100e6) + 100 * 250_000 - 100 * 275_000);
        assertEq(stopBook.entries(address(e)), 0);
    }

    // ── И2: nothing the trader does comes before a touch ─────────

    /// A touch nobody settled for many blocks: the trader's first call carries it out at its touch before it acts;
    /// a call that is refused after it undoes it, and the next one carries it out again, at the same touch.
    function test_I2_aTouchLeftUnsettled_isCarriedOutByTheTradersCall_first() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 3);
        _mark(940_000);
        stopBook.poke();
        uint256 touch = block.number;
        vm.roll(touch + 1);
        _mark(MARK); // back over it: nothing to undo
        stopBook.poke();
        vm.roll(touch + 500);
        vm.warp(block.timestamp + 1 hours);
        assertEq(e.positionOf(BTC), 100, "the checks carry nothing out");
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.clearTrigger(BTC, false); // the settle carried it out: no stop left to remove
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoPosition.selector);
        e.setTrigger(BTC, false, 900_000, 0); // nor a position to move it on
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.TargetNotMet.selector, int256(94e6), int256(110e6)));
        e.graduate(); // flat after the stop, under the target: judged on the account the stop left
        assertEq(e.positionOf(BTC), 100, "each refusal undid its settle");
        vm.recordLogs();
        vm.prank(trader);
        e.place(ETH, true, 10);
        (bytes32[] memory f, uint256 n) = _fills(vm.getRecordedLogs());
        assertEq(n, 1);
        assertEq(f[0], _fillKey(e, BTC, false, 100, touch), "at the touch, however late");
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), 94e6);
    }

    /// An exam's call whose settle stops short (too little gas to carry the touch out) is refused: no function acts on
    /// a state its touches have not reached. With the gas, the stop is carried out first.
    function test_I2_aShortSettle_refusesTheExamsCall() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt);
        _mark(940_000);
        stopBook.poke();
        vm.roll(armedAt + 1);
        _mark(MARK);
        vm.prank(trader);
        vm.expectRevert(PaperExam.StopsPending.selector);
        e.clearTrigger{gas: 240_000}(BTC, false);
        assertEq(_px(e, BTC, false), 950_000, "the stop is still there, and still owed");
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.clearTrigger(BTC, false);
        stopBook.settle(address(e));
        assertEq(e.cash(), 94e6);
    }

    // ── И3: by touch block within an exam ───────────────────────

    /// A stop of 40 touched, then the take: settled together later, the stop first at its price, the take after at its
    /// own; the other way round, the take first. A stop that closes it all leaves no take to carry out.
    function test_I3_byTouchBlock_aStopTouchedFirstGoesFirst() public {
        PaperExam a = _exam();
        _trade(a, BTC, true, 100);
        _set(a, BTC, false, 950_000, 40);
        _set(a, BTC, true, 1_050_000, 0);
        PaperExam b = _exam();
        _trade(b, BTC, true, 100);
        _set(b, BTC, false, 900_000, 0);
        _set(b, BTC, true, 1_030_000, 40);
        PaperExam c = _exam();
        _trade(c, BTC, true, 100);
        _set(c, BTC, false, 950_000, 0);
        _set(c, BTC, true, 1_050_000, 0);
        vm.roll(block.number + DELAY);
        _mark(1_035_000); // b's take
        stopBook.poke();
        uint256 t0 = block.number;
        vm.roll(t0 + 1);
        _mark(945_000); // a's and c's stops
        stopBook.poke();
        uint256 t1 = t0 + 1;
        vm.roll(t1 + 1);
        _mark(1_060_000); // a's and c's takes; not b's stop, never touched
        stopBook.poke();
        uint256 t2 = t1 + 1;
        vm.roll(t2 + 1);
        _mark(890_000); // b's stop
        stopBook.poke();
        uint256 t3 = t2 + 1;
        vm.roll(t3 + 1);
        _mark(MARK);
        int256 a0 = a.cash();
        vm.recordLogs();
        stopBook.settle(address(a));
        stopBook.settle(address(b));
        stopBook.settle(address(c));
        (bytes32[] memory f, uint256 n) = _fills(vm.getRecordedLogs());
        assertEq(n, 5);
        assertEq(f[0], _fillKey(a, BTC, false, 40, t1), "a: its stop first");
        assertEq(f[1], _fillKey(a, BTC, true, 60, t2), "a: then its take, on what is left");
        assertEq(f[2], _fillKey(b, BTC, true, 40, t0), "b: its take first");
        assertEq(f[3], _fillKey(b, BTC, false, 60, t3), "b: then its stop");
        assertEq(f[4], _fillKey(c, BTC, false, 100, t1), "c: the stop, and its take went with the position");
        assertEq(a.cash(), a0 + 40 * 945_000 + 60 * 1_060_000);
        assertEq(c.cash(), 94_500_000);
        assertEq(stopBook.entries(address(c)), 0);
    }

    /// Across assets the same: a stop on ETH touched before one on BTC, settled together, goes first.
    function test_I3_acrossAssets_byTouchBlock() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _trade(e, ETH, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, ETH, false, 240_000, 0);
        vm.roll(block.number + DELAY);
        CoreSimulatorLib.setMarkPx(ETH, 235_000);
        stopBook.poke();
        uint256 t1 = block.number;
        vm.roll(t1 + 1);
        _mark(940_000);
        stopBook.poke();
        uint256 t2 = t1 + 1;
        vm.roll(t2 + 1);
        vm.recordLogs();
        stopBook.settle(address(e));
        (bytes32[] memory f, uint256 n) = _fills(vm.getRecordedLogs());
        assertEq(n, 2);
        assertEq(f[0], _fillKey(e, ETH, false, 100, t1), "ETH's stop, touched first");
        assertEq(f[1], _fillKey(e, BTC, false, 100, t2));
    }

    /// A take set blocks after the stop, its search starting later: the take touched, the stop never, the take filled.
    function test_I1_aTakeSetLater_isFilledAtItsTouch() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 3);
        stopBook.poke();
        _set(e, BTC, true, 1_050_000, 0);
        vm.roll(block.number + 1);
        stopBook.poke(); // a block the stop is searched over and the take, not armed yet, is not
        vm.roll(block.number + 1);
        _mark(1_055_000);
        stopBook.poke();
        uint256 touch = block.number;
        vm.roll(touch + 2);
        _mark(MARK);
        vm.recordLogs();
        stopBook.settle(address(e));
        (bytes32[] memory f, uint256 n) = _fills(vm.getRecordedLogs());
        assertEq(n, 1);
        assertEq(f[0], _fillKey(e, BTC, true, 100, touch));
        assertEq(stopBook.entries(address(e)), 0, "the stop went with the position");
    }

    /// A stop and a take that one price touches in one block (set across each other): the stop first, the take on what
    /// the stop left, at the same price.
    function test_I3_oneBlockTouchesBoth_theStopFirst() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 40);
        vm.roll(block.number + 1);
        _mark(940_000); // the stop is not armed yet
        _set(e, BTC, true, 945_000, 0); // a take over this price, under the stop
        vm.roll(block.number + 1);
        _mark(960_000);
        stopBook.poke();
        vm.roll(block.number + 1);
        _mark(947_000);
        stopBook.poke();
        uint256 t = block.number;
        vm.recordLogs();
        stopBook.settle(address(e));
        (bytes32[] memory f, uint256 n) = _fills(vm.getRecordedLogs());
        assertEq(n, 2);
        assertEq(f[0], _fillKey(e, BTC, false, 40, t));
        assertEq(f[1], _fillKey(e, BTC, true, 60, t));
        assertEq(e.positionOf(BTC), 0);
    }

    // ── И4: the time of the touch ───────────────────────────────

    /// Touched before the deadline, settled after it: filled, at the time of its touch; touched after it: removed,
    /// nothing filled (the exam was over), and the exam expires.
    function test_I4_aTouchBeforeTheDeadline_settledAfter_isFilled() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        uint256 deadline = e.deadline();
        vm.roll(armedAt + 1);
        vm.warp(deadline - 10);
        _mark(940_000);
        stopBook.poke();
        vm.roll(block.number + 20);
        vm.warp(deadline + 1 hours);
        _mark(MARK);
        assertEq(stopBook.settle(address(e)), 0);
        assertEq(e.positionOf(BTC), 0, "filled, not dropped");
        assertEq(e.cash(), 94e6);
        assertEq(e.lastFillAt(), deadline - 10, "a fill at the time of its touch");
        e.expire();
    }

    /// The same with `maxIdle`: touched inside it, settled past it, filled.
    function test_I4_aTouchInsideMaxIdle_settledPastIt_isFilled() public {
        ExamTerms memory t = _terms();
        t.maxIdle = 1 hours;
        PaperExam e = _examWith(_two(), t, false);
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        uint256 lastFill = e.lastFillAt();
        vm.roll(block.number + DELAY);
        vm.warp(lastFill + 1 hours - 1);
        _mark(940_000);
        stopBook.poke();
        vm.roll(block.number + 5);
        vm.warp(lastFill + 3 hours);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.lastFillAt(), lastFill + 1 hours - 1);
    }

    // ── И5: a check logs all or nothing ─────────────────────────

    /// Whatever gas a check is called with, it has logged every watched asset or it reverted: no call takes a block
    /// and logs a price as missing.
    function testFuzz_I5_aCheckLogsEveryAssetOrNothing(uint256 g) public {
        uint32[] memory as_ = new uint32[](6);
        for (uint256 k = 0; k < 6; ++k) {
            as_[k] = uint32(100 + k);
            _asset(as_[k], 5, MARK);
        }
        PaperExam e = _examOn(as_, false);
        for (uint256 k = 0; k < 6; ++k) {
            _trade(e, as_[k], true, 20);
            _set(e, as_[k], false, 900_000, 0);
        }
        vm.roll(block.number + 1);
        g = bound(g, 30_000, 900_000);
        uint256 len = stopBook.logged();
        (bool ok,) = address(stopBook).call{gas: g}(abi.encodeCall(StopBook.poke, ()));
        if (ok) {
            assertEq(stopBook.logged(), len + 1);
            assertEq(stopBook.lastPokeBlock(), block.number);
            for (uint256 k = 0; k < 6; ++k) {
                assertEq(stopBook.priceAt(as_[k], len), MARK, "every asset logged");
            }
        } else {
            assertEq(stopBook.logged(), len, "nothing logged");
            assertLt(stopBook.lastPokeBlock(), block.number, "the block not taken");
        }
    }

    // ── И7: nobody else makes an exam's settle later or dearer ───

    /// A crowd of other exams touched in the same block, settled or not: an exam's settle costs what the same settle
    /// costs before the crowd, and nobody's touch holds it.
    function test_I7_aCrowdChangesNothingForAnotherExam() public {
        PaperExam v1 = _exam();
        PaperExam v2 = _exam();
        _trade(v1, BTC, true, 100);
        _trade(v2, BTC, true, 100);
        _set(v1, BTC, false, 950_000, 0);
        _set(v2, BTC, false, 950_000, 0); // the same stop, set in the same block
        for (uint256 i = 0; i < 60; ++i) {
            PaperExam o = _exam();
            _trade(o, BTC, true, 10);
            _set(o, BTC, false, uint64(951_000 + i * 100), 0);
            _set(o, BTC, true, uint64(1_200_000 + i), 0);
        }
        vm.roll(block.number + DELAY + 1);
        _mark(940_000);
        stopBook.poke();
        vm.roll(block.number + 1);
        stopBook.poke();
        vm.cool(address(stopBook));
        vm.cool(address(v1));
        uint256 g0 = gasleft();
        assertEq(stopBook.settle(address(v1)), 0, "the crowd unsettled holds nobody");
        uint256 alone = g0 - gasleft();
        for (uint256 i = 0; i < 30; ++i) {
            stopBook.settle(address(made[2 + i]));
        }
        vm.cool(address(stopBook));
        vm.cool(address(v2));
        g0 = gasleft();
        assertEq(stopBook.settle(address(v2)), 0);
        uint256 crowded = g0 - gasleft();
        assertApproxEqAbs(crowded, alone, 300, "the same settle, the same gas");
        assertEq(v1.cash(), v2.cash());
    }

    // ── И8: a failed fill erases nothing; a way out ─────────────

    /// A settle too poor to carry the fill out stops before it, keeps the touch, and says it is owed; the next one
    /// carries it out at that touch.
    function test_I8_aSettleShortOfGas_keepsTheTouch_theNextFillsIt() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        for (uint256 b = 1; b <= 3; ++b) {
            vm.roll(armedAt - 2 + b);
            stopBook.poke(); // three blocks over the stop
        }
        vm.roll(armedAt + 2);
        _mark(940_000);
        stopBook.poke();
        uint256 touch = block.number;
        vm.roll(touch + 2);
        _mark(MARK);
        stopBook.poke();
        assertEq(stopBook.settle{gas: 200_000}(address(e)), 1, "owed");
        assertEq(stopBook.entryOf(address(e), BTC, false).level, 950_000, "kept");
        assertEq(stopBook.entryOf(address(e), BTC, false).cursor, stopBook.logged() - 2, "at its touch");
        assertEq(stopBook.logged() - 2, 3);
        vm.roll(block.number + 1);
        assertEq(stopBook.settle(address(e)), 0);
        assertEq(e.cash(), 94e6, "at the touch price");
    }

    /// The way out of a fill that fails at every try: the book caps no fill's gas. It gives the call all the gas there
    /// is but what it keeps for itself, so a fill that needs more than a small settle has is carried out by a settle
    /// sent with more; meanwhile its entry stays. A `PaperExam`'s fill reverts for nothing but the gas: no answer of
    /// the book's prices can make it (`_fillPrice` reads them as plain words).
    function test_I8_aFillThatNeedsMoreGas_isCarriedOutByASettleWithMore() public {
        GreedyExam g = new GreedyExam(stopBook, 700_000);
        g.arm(BTC, true, 950_000, uint64(block.number + 1));
        vm.roll(block.number + 1);
        _mark(940_000);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(g), BTC, false, 940_000, block.number, 0); // FAILED
        assertEq(stopBook.settle{gas: 600_000}(address(g)), 1);
        assertEq(stopBook.entryOf(address(g), BTC, false).level, 950_000, "never erased");
        uint256 touch = block.number;
        vm.roll(block.number + 1);
        vm.expectEmit(address(stopBook));
        emit StopBook.Fired(address(g), BTC, false, 940_000, touch, 1); // FILLED, at the touch
        assertEq(stopBook.settle{gas: 1_500_000}(address(g)), 0);
        assertEq(stopBook.entries(address(g)), 0);
    }

    /// An exam that calls the book from its fill is refused (`Reentered`): its fill fails, its entry stays.
    function test_I8_anExamCannotCallTheBookFromItsFill() public {
        BackCaller x = new BackCaller(stopBook);
        x.arm(BTC, true, 950_000, uint64(block.number + 1));
        vm.roll(block.number + 1);
        _mark(940_000);
        assertEq(stopBook.settle(address(x)), 1);
        assertEq(stopBook.entries(address(x)), 1, "kept, and nothing added from inside the settle");
        assertEq(stopBook.entryOf(address(x), BTC + 1, false).level, 0);
    }

    /// A book that answers garbage makes no stop's fill revert: it fills at the oracle.
    function test_I8_aGarbageBook_fillsAtTheOracle() public {
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        PaperExam e = _examOn(one, true);
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        vm.etch(BBO, type(Garbage).runtimeCode);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0);
        assertEq(e.cash(), int256(100e6) - 100 * 1_000_010 + 100 * 940_000);
    }

    // ── a settle keeps what it found ────────────────────────────

    /// Short of gas between its steps, a settle stops and keeps what it found; settles one after the other carry out
    /// what one settle with all the gas carries out, at the same touches.
    function test_settle_stoppedShort_goesOnFromWhereItWas() public {
        uint32[] memory as_ = new uint32[](8);
        for (uint256 k = 0; k < 8; ++k) {
            as_[k] = uint32(100 + k);
            _asset(as_[k], 5, MARK);
        }
        PaperExam a = _examOn(as_, false);
        PaperExam b = _examOn(as_, false);
        for (uint256 k = 0; k < 8; ++k) {
            _trade(a, as_[k], true, 20);
            _trade(b, as_[k], true, 20);
        }
        for (uint256 k = 0; k < 8; ++k) {
            _set(a, as_[k], false, 950_000, 0);
            _set(a, as_[k], true, 1_050_000, 10);
            _set(b, as_[k], false, 950_000, 0);
            _set(b, as_[k], true, 1_050_000, 10);
        }
        vm.roll(block.number + DELAY);
        for (uint256 r = 0; r < 12; ++r) {
            vm.roll(block.number + 1);
            for (uint256 k = 0; k < 8; ++k) {
                CoreSimulatorLib.setMarkPx(as_[k], uint64(MARK - 60_000 + ((k * 7 + r * 13) % 12) * 10_000));
            }
            stopBook.poke();
        }
        vm.roll(block.number + 1);
        stopBook.poke();
        uint256 rounds;
        uint256 left = stopBook.settle{gas: 450_000}(address(a));
        while (left != 0) {
            ++rounds;
            vm.roll(block.number + 1);
            stopBook.poke();
            uint256 now_ = stopBook.settle{gas: 450_000}(address(a));
            assertTrue(now_ < left || rounds < 40, "each settle gets further");
            left = now_;
        }
        assertGt(rounds, 0, "it took more than one");
        assertEq(stopBook.settle(address(b)), 0);
        assertEq(a.cash(), b.cash(), "the same fills");
        for (uint256 k = 0; k < 8; ++k) {
            assertEq(a.positionOf(as_[k]), b.positionOf(as_[k]));
        }
        assertEq(stopBook.entries(address(a)), stopBook.entries(address(b)));
    }

    // ── the journal itself ──────────────────────────────────────

    /// The journal logs only the assets with an entry, once a block, with the block and its time; a block with no
    /// price on an asset is logged as none.
    function test_journal_logsWatchedAssets_onceABlock() public {
        (PaperExam e,) = _longWithStop();
        vm.roll(block.number + 1);
        uint256 len = stopBook.logged();
        stopBook.poke();
        stopBook.poke();
        assertEq(stopBook.logged(), len + 1, "once a block");
        (uint256 blk, uint256 time) = stopBook.at(len);
        assertEq(blk, block.number);
        assertEq(time, block.timestamp);
        assertEq(stopBook.priceAt(BTC, len), MARK);
        assertEq(stopBook.priceAt(ETH, len), 0, "no entry on it: not logged");
        assertEq(stopBook.watched().length, 1);
        vm.prank(trader);
        e.clearTrigger(BTC, false);
        vm.roll(block.number + 1);
        stopBook.poke();
        assertEq(stopBook.logged(), len + 1, "nothing watched: nothing logged");
    }

    /// The search against every price one by one: on a long run of random prices, every level, from every start, finds
    /// the first position that reaches it.
    function testFuzz_journal_findsTheFirstReach(uint256 seed) public {
        (PaperExam e,) = _longWithStop();
        e;
        uint256 n = 90 + seed % 200;
        uint256 len0 = stopBook.logged();
        uint64[] memory px = new uint64[](n);
        for (uint256 i = 0; i < n; ++i) {
            vm.roll(block.number + 1);
            seed = uint256(keccak256(abi.encode(seed)));
            px[i] = seed % 9 == 0 ? 0 : uint64(900_000 + seed % 200_000);
            _mark(px[i]);
            stopBook.poke();
        }
        for (uint256 t = 0; t < 24; ++t) {
            seed = uint256(keccak256(abi.encode(seed)));
            uint256 s = seed % n;
            uint64 level = uint64(900_000 + (seed >> 64) % 200_000);
            bool below = (seed >> 128) % 2 == 0;
            uint256 want = n;
            for (uint256 i = s; i < n; ++i) {
                if (px[i] != 0 && (below ? px[i] <= level : px[i] >= level)) {
                    want = i;
                    break;
                }
            }
            assertEq(_harness().first(BTC, len0 + s, len0 + n, level, below), len0 + want);
            // Both sides at once: a level under the price and one over it, the first price that reaches either.
            uint64 lo = uint64(900_000 + (seed >> 160) % 100_000);
            uint64 hi = uint64(1_000_000 + (seed >> 192) % 100_000);
            want = n;
            for (uint256 i = s; i < n; ++i) {
                if (px[i] != 0 && (px[i] <= lo || px[i] >= hi)) {
                    want = i;
                    break;
                }
            }
            assertEq(_harness().find(BTC, len0 + s, len0 + n, lo, hi), len0 + want);
        }
    }

    /// The same with prices past 2^24, whose runs are kept in codes rounded outward: levels close to the prices, so
    /// that a run's codes reach a level its prices do not, and the search goes down it and on.
    function testFuzz_journal_bigPrices_findsTheFirstReach(uint256 seed) public {
        uint32 big = 77;
        _asset(big, 0, 150_000_000);
        uint32[] memory one = new uint32[](1);
        one[0] = big;
        PaperExam e = _examOn(one, false);
        _trade(e, big, true, 1);
        _set(e, big, false, 50_000_000, 0);
        uint256 n = 90 + seed % 200;
        uint256 len0 = stopBook.logged();
        uint64[] memory px = new uint64[](n);
        for (uint256 i = 0; i < n; ++i) {
            vm.roll(block.number + 1);
            seed = uint256(keccak256(abi.encode(seed)));
            px[i] = seed % 11 == 0 ? 0 : uint64(100_000_000 + seed % 100_000_000);
            CoreSimulatorLib.setMarkPx(big, px[i]);
            stopBook.poke();
        }
        for (uint256 t = 0; t < 32; ++t) {
            seed = uint256(keccak256(abi.encode(seed)));
            uint256 s = seed % n;
            uint64 near = px[(seed >> 32) % n];
            if (near == 0) near = 150_000_000;
            // A level within a few roundings (16 here) of a logged price.
            uint64 level = uint64(uint256(near) - 40 + (seed >> 64) % 80);
            bool below = (seed >> 128) % 2 == 0;
            uint256 want = n;
            for (uint256 i = s; i < n; ++i) {
                if (px[i] != 0 && (below ? px[i] <= level : px[i] >= level)) {
                    want = i;
                    break;
                }
            }
            assertEq(_harness().first(big, len0 + s, len0 + n, level, below), len0 + want);
        }
    }

    // ── the book of the touch's block (fifth audit, H1) and the journal's prices (M3) ─────

    /// A stop at the book is filled at the book of its touch's block, whoever settles it and whenever: the book that
    /// moves after the touch changes nothing.
    function test_P1_aStopsPriceIsItsTouchBlocksBook_whoeverSettlesWhenever() public {
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        PaperExam a = _examOn(one, true);
        PaperExam b = _examOn(one, true);
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        _trade(a, BTC, true, 100);
        _trade(b, BTC, true, 100);
        _set(a, BTC, false, 950_000, 0);
        _set(b, BTC, false, 950_000, 0);
        vm.roll(block.number + DELAY);
        _mark(940_000);
        BookStub(BBO).set(BTC, 935_000, 942_000); // the touch block's bid, 0.53 % under
        stopBook.poke();
        uint256 touch = block.number;
        uint256 pos = stopBook.logged() - 1;
        vm.roll(touch + 1);
        BookStub(BBO).set(BTC, 900_000, 990_000); // a far worse book after it
        vm.prank(stranger);
        stopBook.settle(address(a));
        vm.roll(touch + 7);
        BookStub(BBO).set(BTC, 939_999, 940_001); // and a better one
        vm.prank(trader);
        b.place(BTC, true, 1);
        uint256 off = uint256(940_000 - 935_000) * 1e6 / 940_000;
        uint256 px = 940_000 - 940_000 * off / 1e6;
        assertEq(a.cash(), b.cash(), "one price, whoever and whenever");
        assertEq(a.cash(), int256(100e6) - 100 * 1_000_010 + int256(100 * px), "the touch block's bid");
        assertEq(stopBook.bookAt(BTC, pos), uint32((uint256(2_000) * 1e6 / 940_000) << 16 | off));
    }

    /// The journal takes prices under `MAX_PRICE` only: an oracle price at it or over is logged as none, a level at it
    /// or over is refused; under it the runs hold the prices themselves, and a search goes down only where they reach.
    function test_P4_pricesAndLevelsPastTheJournalsRange() public {
        uint32 big = 78;
        _asset(big, 0, 100_000_000);
        uint32[] memory one = new uint32[](1);
        one[0] = big;
        PaperExam e = _examOn(one, false);
        _trade(e, big, true, 1);
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(StopBook.LevelTooHigh.selector, uint64(type(uint32).max)));
        e.setTrigger(big, true, type(uint32).max, 0);
        _set(e, big, false, 90_000_000, 0);
        vm.roll(block.number + DELAY);
        CoreSimulatorLib.setMarkPx(big, type(uint32).max);
        vm.expectEmit(address(stopBook));
        emit StopBook.NoPrice(big);
        stopBook.poke();
        assertEq(stopBook.priceAt(big, stopBook.logged() - 1), 0);
        vm.roll(block.number + 1);
        CoreSimulatorLib.setMarkPx(big, type(uint32).max - 2);
        stopBook.poke();
        assertEq(stopBook.priceAt(big, stopBook.logged() - 1), type(uint32).max - 2);
    }

    // ── the reviews before ──────────────────────────────────────

    /// A stop the price has already reached is refused, as an exchange refuses a stop that would trigger at once.
    function test_aTriggerPastThePrice_isRefused() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.WouldTrigger.selector, MARK, MARK));
        e.setTrigger(BTC, false, MARK, 0);
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.WouldTrigger.selector, MARK, 990_000));
        e.setTrigger(BTC, true, 990_000, 0);
    }

    /// An ended exam leaves the book: its entries go, and its asset with them if nobody else's is on it.
    function test_anEndedExam_leavesTheBook() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _set(e, BTC, true, 1_200_000, 0);
        vm.roll(armedAt + 1);
        vm.warp(block.timestamp + 8 days);
        e.expire();
        assertEq(stopBook.entries(address(e)), 0);
        assertEq(stopBook.watched().length, 0);
    }
}

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
