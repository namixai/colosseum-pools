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
import {StopBookHarness} from "./StopBook.t.sol";

// Exams the handler trades.
uint256 constant N_EXAMS = 6;

contract StopBookHandler is Test {
    uint32 public constant BTC = 3;
    uint32 public constant ETH = 4;
    uint16 constant DELAY = 2;

    StopBook public book;
    PaperExam[N_EXAMS] public exams;
    address public trader = makeAddr("trader");
    address public filler = makeAddr("filler");
    uint64[2] public px = [uint64(1_000_000), uint64(250_000)];
    mapping(string => uint256) public calls;
    /// The most gas a check took.
    uint256 public heaviestPoke;

    /// Reads the book's events of whatever the call did, the checks the exams' own calls made included.
    modifier watch() {
        vm.recordLogs();
        _;
        _scan(vm.getRecordedLogs());
    }

    function _scan(Vm.Log[] memory logs) internal {
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == StopBook.Fired.selector) calls["fired"] += 1;
        }
    }

    constructor(StopBook book_, PaperExam[N_EXAMS] memory exams_) {
        book = book_;
        exams = exams_;
    }

    function _asset(uint256 seed) internal pure returns (uint32) {
        return seed % 2 == 0 ? BTC : ETH;
    }

    function trade(uint256 e, uint256 a, bool isBuy, uint256 sz) external watch {
        PaperExam x = exams[e % N_EXAMS];
        uint32 asset = _asset(a);
        vm.prank(trader);
        try x.place(asset, isBuy, uint64(bound(sz, 1, asset == BTC ? 150 : 600))) {} catch { return; }
        vm.roll(block.number + DELAY);
        vm.prank(filler);
        try x.fill() {
            calls["fill"] += 1;
        } catch {}
    }

    function setTrigger(uint256 e, uint256 a, bool isTake, uint256 offset, uint256 sz) external watch {
        PaperExam x = exams[e % N_EXAMS];
        uint32 asset = _asset(a);
        uint256 p = px[asset == BTC ? 0 : 1];
        // Within 2 % either side of the price, past it too.
        uint64 level = uint64(p * (9_800 + offset % 401) / 10_000);
        vm.prank(trader);
        try x.setTrigger(asset, isTake, level, uint64(sz % 3 == 0 ? 0 : bound(sz, 1, 100))) {
            calls["setTrigger"] += 1;
        } catch {}
    }

    /// Every exam with a position on the asset sets an entry on its below side just under the price, a long its stop
    /// and a short its take, each at its own level: one jump down reaches more than one check carries out and latches.
    function crowdBelow(uint256 a) external watch {
        uint32 asset = _asset(a);
        uint256 p = px[asset == BTC ? 0 : 1];
        for (uint256 i = 0; i < N_EXAMS; ++i) {
            int256 pos = exams[i].positionOf(asset);
            if (pos == 0) continue;
            vm.prank(trader);
            try exams[i].setTrigger(asset, pos < 0, uint64(p * (9_990 - i * 10) / 10_000), 0) {
                calls["setTrigger"] += 1;
            } catch {}
        }
    }

    function clearTrigger(uint256 e, uint256 a, bool isTake) external watch {
        vm.prank(trader);
        try exams[e % N_EXAMS].clearTrigger(_asset(a), isTake) {
            calls["clearTrigger"] += 1;
        } catch {}
    }

    /// The next block, the price of one asset moved up to 1.5 % either way, now and then 5 %; checked unless `skip`.
    function move(uint256 a, uint256 bps, bool skip) external {
        uint256 i = a % 2;
        int256 d = int256(bps % 301) - 150;
        if (bps % 7 == 0) d = d > 0 ? int256(500) : int256(-500); // now and then a jump past many levels at once
        px[i] = uint64(uint256(int256(uint256(px[i])) * (10_000 + d) / 10_000));
        vm.roll(block.number + 1);
        CoreSimulatorLib.setMarkPx(i == 0 ? BTC : ETH, px[i]);
        if (!skip) poke();
    }

    /// The check of this block, then, as an executor does, a settle of every exam.
    function poke() public {
        vm.recordLogs();
        uint256 before = gasleft();
        book.poke();
        uint256 used = before - gasleft();
        if (used > heaviestPoke) heaviestPoke = used;
        calls["poke"] += 1;
        for (uint256 i = 0; i < N_EXAMS; ++i) {
            book.settle(address(exams[i]));
        }
        _scan(vm.getRecordedLogs());
    }

    /// The check of this block and the settle of one exam only: the others' touches wait.
    function settleOne(uint256 e) external watch {
        book.settle(address(exams[e % N_EXAMS]));
        calls["settleOne"] += 1;
    }
}

contract StopBookInvariantTest is Test, BookStand {
    StopBookHandler handler;
    StopBookHarness harness;
    PaperExam[N_EXAMS] exams;

    function setUp() public {
        HyperCore hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        vm.etch(0x0000000000000000000000000000000000000807, type(OracleFollowsMark).runtimeCode);
        hyperCore.registerPerpAssetInfo(
            3, PrecompileLib.PerpAssetInfo({coin: "BTC", marginTableId: 54, szDecimals: 5, maxLeverage: 40, onlyIsolated: false})
        );
        hyperCore.registerPerpAssetInfo(
            4, PrecompileLib.PerpAssetInfo({coin: "ETH", marginTableId: 55, szDecimals: 4, maxLeverage: 25, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(3, 1_000_000);
        CoreSimulatorLib.setMarkPx(4, 250_000);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);
        harness = new StopBookHarness();
        stopBook = harness;
        PaperExam impl = new PaperExam();
        uint32[] memory assets = new uint32[](2);
        (assets[0], assets[1]) = (3, 4);
        uint16[] memory t = new uint16[](1);
        t[0] = 1000;
        Rules memory r = Rules({dailyLossBps: 9000, maxDrawdownBps: 9000, maxLeverageX100: 1000, assets: assets,
            assetLeverageX100: new uint32[](0)});
        ExamTerms memory terms = ExamTerms({capital: 100e6, targetsBps: t, stepDuration: 30 days, maxIdle: 0,
            minTradingDays: 0, costPpm: 0, fillDelay: 2, fillWindow: 3});
        address trader = makeAddr("trader");
        address filler = makeAddr("filler");
        for (uint256 i = 0; i < N_EXAMS; ++i) {
            exams[i] = PaperExam(Clones.clone(address(impl)));
            exams[i].initializeWithFiller(trader, r, terms, filler, false, 0, 0, false);
        }
        _background(impl, r, terms, trader, filler);
        handler = new StopBookHandler(harness, exams);
        targetContract(address(handler));
        bytes4[] memory sel = new bytes4[](10);
        (sel[0], sel[1]) = (StopBookHandler.trade.selector, StopBookHandler.trade.selector);
        (sel[2], sel[3]) = (StopBookHandler.setTrigger.selector, StopBookHandler.setTrigger.selector);
        sel[4] = StopBookHandler.clearTrigger.selector;
        (sel[5], sel[6]) = (StopBookHandler.move.selector, StopBookHandler.move.selector);
        sel[7] = StopBookHandler.poke.selector;
        sel[8] = StopBookHandler.crowdBelow.selector;
        sel[9] = StopBookHandler.settleOne.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sel}));
    }

    uint256 constant BACKGROUND = 60;

    /// `BACKGROUND` exams, each long on both assets with a stop at a half and a take at twice the price, every level
    /// its own; armed before the run starts.
    function _background(PaperExam impl, Rules memory r, ExamTerms memory terms, address trader, address filler)
        internal
    {
        for (uint256 i = 0; i < BACKGROUND; ++i) {
            PaperExam x = PaperExam(Clones.clone(address(impl)));
            x.initializeWithFiller(trader, r, terms, filler, false, 0, 0, false);
            for (uint32 a = 3; a <= 4; ++a) {
                uint64 p = a == 3 ? 1_000_000 : 250_000;
                vm.prank(trader);
                x.place(a, true, a == 3 ? 10 : 40);
                vm.roll(block.number + 2);
                vm.prank(filler);
                x.fill();
                vm.startPrank(trader);
                x.setTrigger(a, false, uint64(p / 2 - i * (p / 1_000)), 0);
                x.setTrigger(a, true, uint64(p * 2 + i * (p / 1_000)), 0);
                vm.stopPrank();
            }
        }
        vm.roll(block.number + 2);
        stopBook.poke();
        assertEq(stopBook.watched().length, 2, "the background is on both assets");
    }

    /// B1. The book's records are the exams' stops and takes: none without the other, at the same level and `armedAt`,
    ///     on the side it fires on.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_recordsAreTheExamsStops() public view {
        for (uint256 i = 0; i < N_EXAMS; ++i) {
            uint256 n;
            for (uint32 a = 3; a <= 4; ++a) {
                for (uint256 k = 0; k < 2; ++k) {
                    if (_sameRecord(exams[i], a, k == 1)) ++n;
                }
            }
            assertEq(harness.entries(address(exams[i])), n, "its list is its records");
        }
    }

    /// The exam's stop or take on `a` and its change asked, against the book's entry: a change in force takes the
    /// stop's place in either once it acts, so each is read as in force in this block. Whether the book has an entry.
    function _sameRecord(PaperExam x, uint32 a, bool isTake) internal view returns (bool) {
        (uint64 tpx,, uint64 armedAt) = isTake ? x.takeOf(a) : x.stopOf(a);
        (uint64 cpx,, uint64 cAt) = x.changeOf(a, isTake);
        if (cAt != 0 && cAt <= block.number) (tpx, armedAt, cpx, cAt) = (cpx, cpx == 0 ? 0 : cAt, 0, 0);
        StopBook.Entry memory s = harness.entryOf(address(x), a, isTake);
        uint256[4] memory b = [uint256(s.level), s.armedAt, s.nextLevel, s.nextAt];
        if (b[3] != 0 && b[3] <= block.number) b = [b[2], b[2] == 0 ? 0 : b[3], 0, 0];
        assertEq(tpx != 0, b[0] != 0, "a record without a stop, or a stop without one");
        if (tpx != 0) {
            assertEq(b[0], tpx);
            assertEq(b[1], armedAt);
            assertEq(b[2], cpx, "the change asked");
            assertEq(b[3], cAt, "from the same block");
            assertEq(s.below, isTake == (x.positionOf(a) < 0), "on its side");
        }
        return s.level != 0;
    }

    /// B2. An exam settled up to the journal's end has no touch left; and what `touches` lists of any exam is its own
    ///     entries, in the order of their touches, each at a logged price that reaches its level.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_settledMeansNothingTouchedIsLeft() public view {
        uint256 len = harness.logged();
        for (uint256 i = 0; i < N_EXAMS; ++i) {
            StopBook.Touch[] memory t = harness.touches(address(exams[i]));
            if (harness.checkedTo(address(exams[i])) == len) assertEq(t.length, 0, "settled, and a touch left");
            for (uint256 j = 0; j < t.length; ++j) {
                StopBook.Entry memory e = harness.entryOf(address(exams[i]), t[j].asset, t[j].isTake);
                // At or after the block of a change asked, the change's level; before it, the entry's own.
                bool changed = e.nextAt != 0 && t[j].blk >= e.nextAt;
                uint256 level = changed ? e.nextLevel : e.level;
                assertEq(level, t[j].level);
                assertTrue(level != 0, "no touch of a stop taken back");
                assertTrue(e.below ? t[j].px <= level : t[j].px >= level, "reaches it");
                assertGe(t[j].blk, changed ? e.nextAt : e.armedAt, "not before armedAt");
                assertEq(t[j].px, harness.priceAt(t[j].asset, t[j].pos));
                if (j != 0) assertGe(t[j].pos, t[j - 1].pos, "in the order of the touches");
            }
        }
    }

    /// B3. Every whole run of the trees near the journal's end holds the codes of the lowest and the highest of its prices.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_runsAreTheirPrices() public view {
        uint256 len = harness.logged();
        for (uint32 a = 3; a <= 4; ++a) {
            uint256 off = harness.off(a);
            for (uint256 k = 1; k <= 3; ++k) {
                uint256 size = 1 << (2 * k);
                uint256 qn = len + off;
                for (uint256 back = 1; back <= 3; ++back) {
                    if (qn / size < back) break;
                    uint256 j = qn / size - back;
                    if (j * size < off) break; // before the asset's first position
                    (uint256 lo, uint256 hi) = harness.run(a, k, j);
                    uint64 wlo;
                    uint64 whi;
                    for (uint256 q = j * size; q < (j + 1) * size; ++q) {
                        uint64 p = uint32(harness.raw(a, q));
                        if (p == 0) continue;
                        if (whi == 0 || p < wlo) wlo = p;
                        if (p > whi) whi = p;
                    }
                    assertEq(hi, whi, "the run's highest");
                    assertEq(lo, wlo, "the run's lowest");
                }
            }
        }
    }

    /// B4. A check's gas depends on its assets, not on the stops: two assets and some hundred entries stay light.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_aCheckStaysLight() public view {
        assertLt(handler.heaviestPoke(), 250_000);
    }

    function afterInvariant() external {
        emit log_named_uint("pokes", handler.calls("poke"));
        emit log_named_uint("fired", handler.calls("fired"));
        emit log_named_uint("setTrigger", handler.calls("setTrigger"));
        emit log_named_uint("settleOne", handler.calls("settleOne"));
    }
}
