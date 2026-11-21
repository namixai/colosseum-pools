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

// Exams the handler trades: enough for more entries on one side of an asset than one check carries out and latches.
uint256 constant N_EXAMS = 6;

/// The book with its heaps open to reading.
contract StopBookHarness is StopBook {
    function heap(uint32 asset, bool below) external view returns (uint256[] memory) {
        return below ? _below[asset] : _above[asset];
    }

    function waiting() external view returns (uint256[] memory) {
        return _waiting;
    }

    function latched(address exam) external view returns (uint256[] memory) {
        return _latched[exam];
    }
}

/// Six exams on two assets, at random: positions opened, turned and closed by orders, stops and takes set, moved and
/// removed near the price, the price moved block by block, mostly checked by the book every block and now and then
/// not, so that entries are reached many at a time, more than one check carries out. Under them, `BACKGROUND` other
/// exams keep a stop and a take on each asset far from the price: every heap is some 60 entries deep, and every move
/// in it walks a tall tree (review of cp#335: the heaps of a test were too small to show H1).
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
    /// The last block whose check left entries due unarmed (`ArmingBacklog`).
    uint256 public armingBacklogAt;

    /// Reads the book's events of whatever the call did, the checks the exams' own calls made included.
    modifier watch() {
        vm.recordLogs();
        _;
        _scan(vm.getRecordedLogs());
    }

    function _scan(Vm.Log[] memory logs) internal {
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == StopBook.Fired.selector) calls["fired"] += 1;
            if (logs[i].topics[0] == StopBook.Latched.selector) calls["latched"] += 1;
            if (logs[i].topics[0] == StopBook.TouchBacklog.selector) calls["overload"] += 1;
            if (logs[i].topics[0] == StopBook.ArmingBacklog.selector) armingBacklogAt = block.number;
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

    function poke() public {
        vm.recordLogs();
        uint256 before = gasleft();
        book.poke();
        if (before - gasleft() > 2_500_000) calls["heavyPoke"] += 1;
        calls["poke"] += 1;
        _scan(vm.getRecordedLogs());
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
        bytes4[] memory sel = new bytes4[](9);
        (sel[0], sel[1]) = (StopBookHandler.trade.selector, StopBookHandler.trade.selector);
        (sel[2], sel[3]) = (StopBookHandler.setTrigger.selector, StopBookHandler.setTrigger.selector);
        sel[4] = StopBookHandler.clearTrigger.selector;
        (sel[5], sel[6]) = (StopBookHandler.move.selector, StopBookHandler.move.selector);
        sel[7] = StopBookHandler.poke.selector;
        sel[8] = StopBookHandler.crowdBelow.selector;
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
        while (true) {
            stopBook.poke();
            (, uint256 waiting) = stopBook.watched();
            if (waiting == 0) break;
            vm.roll(block.number + 1);
        }
        (,, uint256 nb, uint256 na) = stopBook.top(3);
        assertEq(nb + na, 2 * BACKGROUND, "the background is armed");
    }

    /// B1. The book's records are the exams' stops and takes: none without the other, at the same level and `armedAt`,
    ///     on the heap of its side.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_recordsAreTheExamsStops() public view {
        for (uint256 i = 0; i < N_EXAMS; ++i) {
            for (uint32 a = 3; a <= 4; ++a) {
                for (uint256 k = 0; k < 2; ++k) {
                    bool isTake = k == 1;
                    (uint64 tpx,, uint64 armedAt) = isTake ? exams[i].takeOf(a) : exams[i].stopOf(a);
                    StopBook.Slot memory s = harness.slotOf(address(exams[i]), a, isTake);
                    assertEq(tpx != 0, s.where != 0, "a record without a stop, or a stop without one");
                    if (tpx == 0) continue;
                    assertEq(s.level, tpx);
                    assertEq(s.armedAt, armedAt);
                    bool below = isTake == (exams[i].positionOf(a) < 0);
                    if (s.where == 1 || s.where == 3) assertTrue(below, "on the below side");
                    if (s.where == 2 || s.where == 4) assertFalse(below, "on the above side");
                }
            }
        }
    }

    /// B2. After the check of a block, no armed entry the block's price has reached is left on a heap, unless an
    ///     overload is being worked off on that side; then what is left is what its price reached: an overload is kept
    ///     exactly as long as an entry its price reached is left (none is left once it is done). No entry whose
    ///     `armedAt` has come still waits, unless the check could not arm them all, or an overload's price reaches it.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_afterACheckNothingReachedIsLeft() public view {
        if (harness.lastPokeBlock() != block.number) return;
        uint64[2] memory p = [handler.px(0), handler.px(1)];
        for (uint32 a = 3; a <= 4; ++a) {
            (uint64 b, uint64 ab, uint256 nb, uint256 na) = harness.top(a);
            StopBook.Overload memory o = harness.overloadOf(a);
            if (o.belowBlock != 0) assertTrue(nb != 0 && o.belowPx <= b, "an overload kept with nothing it reached left");
            else if (nb != 0) assertGt(p[a - 3], b, "a below entry the price has reached");
            if (o.aboveBlock != 0) assertTrue(na != 0 && o.abovePx >= ab, "an overload kept with nothing it reached left");
            else if (na != 0) assertLt(p[a - 3], ab, "an above entry the price has reached");
        }
        if (handler.armingBacklogAt() == block.number) return;
        uint256[] memory w = harness.waiting();
        for (uint256 i = 0; i < w.length; ++i) {
            if (w[i] >> 193 > block.number) continue;
            uint32 a = uint32(w[i] >> 1);
            StopBook.Slot memory s = harness.slotOf(address(uint160(w[i] >> 33)), a, w[i] & 1 == 1);
            StopBook.Overload memory o = harness.overloadOf(a);
            bool deferred = s.where == 1 ? o.belowBlock != 0 && o.belowPx <= s.level
                : o.aboveBlock != 0 && o.abovePx >= s.level;
            assertTrue(deferred, string.concat("an entry past its armedAt still waits: armedAt ", vm.toString(w[i] >> 193), " block ", vm.toString(block.number), " backlogAt ", vm.toString(handler.armingBacklogAt()), " where ", vm.toString(s.where), " level ", vm.toString(s.level), " i ", vm.toString(i), " n ", vm.toString(w.length)));
        }
    }

    /// B3. Every heap is a heap, and every entry's slot says where it is.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_heapsAndTheirIndexes() public view {
        for (uint32 a = 3; a <= 4; ++a) {
            for (uint256 k = 0; k < 2; ++k) {
                bool below = k == 0;
                uint256[] memory h = harness.heap(a, below);
                for (uint256 i = 0; i < h.length; ++i) {
                    if (i > 0) {
                        uint256 parent = h[(i - 1) / 2];
                        assertTrue(below ? parent >= h[i] : parent <= h[i], "not a heap");
                    }
                    StopBook.Slot memory s = harness.slotOf(address(uint160(h[i] >> 1)), a, h[i] & 1 == 1);
                    assertEq(s.where, below ? 3 : 4);
                    assertEq(s.pos, i);
                    assertEq(s.level, uint64(h[i] >> 161));
                }
            }
        }
        uint256[] memory w = harness.waiting();
        for (uint256 i = 0; i < w.length; ++i) {
            if (i > 0) assertLe(w[(i - 1) / 2], w[i], "not a heap");
            StopBook.Slot memory s = harness.slotOf(address(uint160(w[i] >> 33)), uint32(w[i] >> 1), w[i] & 1 == 1);
            assertTrue(s.where == 1 || s.where == 2);
            assertEq(s.pos, i);
            assertEq(uint256(s.armedAt), w[i] >> 193);
        }
        for (uint256 e = 0; e < N_EXAMS; ++e) {
            uint256[] memory l = harness.latched(address(exams[e]));
            for (uint256 i = 0; i < l.length; ++i) {
                StopBook.Slot memory s = harness.slotOf(address(exams[e]), uint32(l[i] >> 1), l[i] & 1 == 1);
                assertEq(s.where, 5);
                assertEq(s.pos, i);
            }
        }
    }

    /// B4. No check weighs more than a small block can hold, with room.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_noHeavyCheck() public view {
        assertEq(handler.calls("heavyPoke"), 0);
    }

    function afterInvariant() external {
        emit log(string.concat("reached: fill=", vm.toString(handler.calls("fill")), " setTrigger=",
            vm.toString(handler.calls("setTrigger")), " clearTrigger=", vm.toString(handler.calls("clearTrigger")),
            " poke=", vm.toString(handler.calls("poke")), " fired=", vm.toString(handler.calls("fired")), " latched=",
            vm.toString(handler.calls("latched")), " overload=", vm.toString(handler.calls("overload"))));
    }
}
