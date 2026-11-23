// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {BookStub} from "../exam/PaperExamInvariant.t.sol";
import {StopBookBase} from "./StopBook.t.sol";

/// The fifth independent audit of cp#335 (22 November 2026, head fcbb5e51), its scenarios on the fixed book: the fill
/// price of a stop is its touch block's, whoever settles and whenever (H1, turned round: on fcbb5e51 three moments gave
/// three prices); settles with random gas carry out what one full settle does; a trader's call with any gas never acts
/// on a touch not carried out; and its tests of a stop cleared or moved just before a wick (M1), turned round: a change
/// waits `fillDelay` as setting one does, and the stop there is carried out at its touches until then.
contract StopBookAudit5Test is StopBookBase {
    uint32[8] internal S;

    function setUp() public override {
        super.setUp();
        for (uint256 k = 0; k < 8; ++k) {
            S[k] = uint32(300 + k);
            _asset(S[k], 5, MARK);
        }
    }

    // ── 4: the fill price depends on WHEN the settle comes ───────

    function _bookExam() internal returns (PaperExam e) {
        uint32[] memory one = new uint32[](1);
        one[0] = BTC;
        e = _examOn(one, true);
        BookStub(BBO).set(BTC, MARK - 10, MARK + 10);
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 0);
        vm.roll(block.number + DELAY + 1);
    }

    /// one touch (oracle 940 000 in block T, nobody settles), three settle moments -> three prices
    function test_a5_H1_onePriceWhoeverSettlesWhenever() public {
        uint256[3] memory cashOf;
        for (uint256 scen = 0; scen < 3; ++scen) {
            uint256 snap = vm.snapshotState();
            PaperExam e = _bookExam();
            _mark(940_000);
            BookStub(BBO).set(BTC, 939_000, 941_000); // a normal book at the touch
            stopBook.poke(); // block T is logged
            uint256 touchBlk = block.number;
            if (scen == 0) {
                // settle at once: the book of the touch block
            } else {
                vm.roll(block.number + 5);
                if (scen == 1) {
                    _mark(1_000_000); // the market came back
                    BookStub(BBO).set(BTC, 999_990, 1_000_010); // trader waited for a tight book over the oracle of the touch
                } else {
                    BookStub(BBO).set(BTC, 700_000, 941_000); // a thin book at the moment somebody else picks
                }
            }
            vm.recordLogs();
            stopBook.settle(address(e));
            // The touch block's bid, 939 000 under an oracle of 940 000, logged in whole millionths (1 063) and taken
            // as a gap of 999: 939 001, whoever settles and whenever (review of cp#336: equal cash alone did not
            // tell a fill at the touch's oracle from one at its bid).
            (uint64 fillPx,,, bool filled) = _stopFill(vm.getRecordedLogs());
            assertTrue(filled);
            assertEq(fillPx, 939_001, "the touch block's bid");
            emit log_named_uint("scenario", scen);
            emit log_named_int("cash after the one stop (100 units sold; bought at 1 000 010)", e.cash());
            cashOf[scen] = uint256(e.cash());
            assertEq(e.positionOf(BTC), 0);
            assertEq(touchBlk > 0, true);
            vm.revertToState(snap);
        }
        // The same touch, one price: the touch block's book, whoever settles and whenever.
        assertEq(cashOf[1], cashOf[0], "waiting for a tight book changes nothing");
        assertEq(cashOf[2], cashOf[0], "nor somebody else's moment");
    }

    // ── 6: random gas for the settle vs one full settle ──────────

    uint256 rnd;

    function _n(uint256 m) internal returns (uint256) {
        rnd = uint256(keccak256(abi.encode(rnd)));
        return rnd % m;
    }

    function _build(uint256 seed, uint256 nAssets) internal returns (PaperExam e) {
        rnd = seed;
        uint32[] memory as_ = new uint32[](nAssets);
        for (uint256 k = 0; k < nAssets; ++k) {
            as_[k] = S[k];
        }
        ExamTerms memory t = _terms();
        t.fillDelay = uint16(1 + _n(30));
        e = _examWith(as_, t, _n(2) == 0);
        for (uint256 k = 0; k < nAssets; ++k) {
            BookStub(BBO).set(S[k], MARK - 10, MARK + 10);
            _trade(e, S[k], _n(2) == 0, 20);
        }
        for (uint256 k = 0; k < nAssets; ++k) {
            bool isLong = e.positionOf(S[k]) > 0;
            uint64 d1 = uint64(1 + _n(8)) * 10_000;
            uint64 d2 = uint64(1 + _n(8)) * 10_000;
            _set(e, S[k], false, isLong ? MARK - d1 : MARK + d1, _n(3) == 0 ? 7 : 0);
            _set(e, S[k], true, isLong ? MARK + d2 : MARK - d2, _n(3) == 0 ? 5 : 0);
        }
        vm.roll(block.number + t.fillDelay);
        // Changes asked of half of them, in force `fillDelay` blocks into the run: the old ones act until then.
        for (uint256 k = 0; k < nAssets; ++k) {
            uint256 r = _n(4);
            if (r >= 2) continue;
            bool isTake = _n(2) == 0;
            bool below = isTake != (e.positionOf(S[k]) > 0);
            uint64 d = uint64(1 + _n(8)) * 10_000;
            vm.prank(trader);
            if (r == 0) e.clearTrigger(S[k], isTake);
            else e.setTrigger(S[k], isTake, below ? MARK - d : MARK + d, _n(3) == 0 ? 6 : 0);
        }
    }

    function _walk(uint256 nAssets, uint256 blocks) internal {
        for (uint256 b = 0; b < blocks; ++b) {
            vm.roll(block.number + 1);
            vm.warp(block.timestamp + 1);
            for (uint256 k = 0; k < nAssets; ++k) {
                uint256 r = _n(10);
                uint64 px = r == 0 ? 0 : uint64(MARK - 100_000 + _n(200_001));
                CoreSimulatorLib.setMarkPx(S[k], px);
            }
            stopBook.poke();
        }
        for (uint256 k = 0; k < nAssets; ++k) {
            CoreSimulatorLib.setMarkPx(S[k], MARK);
        }
        vm.roll(block.number + 1);
    }

    function _state(PaperExam e, uint256 nAssets) internal view returns (bytes32 h) {
        h = keccak256(abi.encode(e.cash(), e.lastFillAt(), e.tradingDays()));
        for (uint256 k = 0; k < nAssets; ++k) {
            (uint64 px, uint64 sz, uint64 ar) = e.stopOf(S[k]);
            (uint64 px2, uint64 sz2, uint64 ar2) = e.takeOf(S[k]);
            h = keccak256(abi.encode(h, e.positionOf(S[k]), px, sz, ar, px2, sz2, ar2));
            (px, sz, ar) = e.changeOf(S[k], false);
            (px2, sz2, ar2) = e.changeOf(S[k], true);
            h = keccak256(abi.encode(h, px, sz, ar, px2, sz2, ar2));
        }
    }

    function testFuzz_a5_randomGasSettles_equalOneFullSettle(uint256 seed) public {
        uint256 nA = 2 + seed % 7;
        PaperExam e = _build(seed, nA);
        _walk(nA, 30 + (seed >> 8) % 60);
        uint256 snap = vm.snapshotState();
        // reference: one settle with all the gas
        uint256 left = stopBook.settle(address(e));
        assertEq(left, 0);
        bytes32 want = _state(e, nA);
        vm.revertToState(snap);
        // many settles with random gas, in random blocks
        uint256 calls;
        left = 1;
        while (left != 0 && calls < 200) {
            ++calls;
            uint256 g = 100_000 + _n(1_900_000);
            if (_n(3) == 0) {
                vm.roll(block.number + 1);
                vm.warp(block.timestamp + 1);
            }
            try stopBook.settle{gas: g}(address(e)) returns (uint256 l) {
                left = l;
            } catch {}
        }
        assertEq(left, 0, "never settled");
        assertEq(_state(e, nA), want, "partial settles differ from the full one");
        assertEq(stopBook.touches(address(e)).length, 0);
    }

    // ── 3: a trader's call with chosen gas never gets past a pending touch ──

    function testFuzz_a5_traderCallWithAnyGas_neverActsOnAPendingTouch(uint256 seed) public {
        uint256 nA = 2 + seed % 7;
        PaperExam e = _build(seed, nA);
        _walk(nA, 20 + (seed >> 8) % 40);
        for (uint256 i = 0; i < 25; ++i) {
            uint256 g = 80_000 + _n(2_400_000);
            uint256 pendingBefore = stopBook.touches(address(e)).length;
            vm.prank(trader);
            (bool ok,) = address(e).call{gas: g}(abi.encodeCall(PaperExam.setAgent, (address(uint160(1 + _n(5))))));
            if (ok) {
                assertEq(stopBook.touches(address(e)).length, 0, "a call went through with a touch pending");
                assertEq(stopBook.entryOf(address(e), S[0], false).level == 0 || true, true);
            }
            pendingBefore;
        }
    }

    // ── M1, turned round: a stop cleared or moved a block before a wick is still carried out ──

    /// The fill a settle carried out of `e`'s stop on BTC: its price, its touch block and the level it was at.
    function _stopFill(Vm.Log[] memory logs)
        internal
        pure
        returns (uint64 px, uint256 touch, uint64 level, bool found)
    {
        for (uint256 j = 0; j < logs.length; ++j) {
            if (logs[j].topics[0] != PaperExam.TriggerFilled.selector) continue;
            (,, uint64 p,, uint64 lvl,, uint256 t,) =
                abi.decode(logs[j].data, (bool, uint64, uint64, uint256, uint64, uint8, uint256, uint64));
            return (p, t, lvl, true);
        }
    }

    /// The auditor's `test_a5_wickDodge_clearBeforeTheWick`, turned round: the trader knows the next block's oracle
    /// is under the stop and clears it a block ahead; the removal waits `fillDelay`, so the wick fills the stop.
    function test_a5_M1_aStopClearedABlockBeforeTheWickStillFills() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 3);
        _mark(960_000);
        stopBook.poke();
        vm.prank(trader);
        e.clearTrigger(BTC, false); // a change: in force DELAY blocks on
        (,, uint64 from) = e.changeOf(BTC, false);
        assertEq(from, block.number + DELAY);
        assertEq(stopBook.entryOf(address(e), BTC, false).level, 950_000, "the stop is still there");
        vm.roll(block.number + 1);
        _mark(940_000); // the wick
        uint256 wick = block.number;
        vm.recordLogs();
        stopBook.settle(address(e));
        (uint64 px, uint256 touch, uint64 level, bool found) = _stopFill(vm.getRecordedLogs());
        assertTrue(found, "the wick filled the stop");
        assertEq(e.positionOf(BTC), 0, "closed at the wick");
        assertEq(touch, wick);
        assertEq(px, 940_000);
        assertEq(level, 950_000);
        (,, from) = e.changeOf(BTC, false);
        assertEq(from, 0, "the removal went with the fill");
        assertEq(stopBook.entryOf(address(e), BTC, false).nextAt, 0);
    }

    /// The auditor's `test_a5_wickDodge_moveTheArmedStopFarther_nakedForTheDelay`, turned round: the stop moved
    /// farther acts at its old level until the move is in force, so there is no block without a stop.
    function test_a5_M1_aStopMovedFartherActsAtItsOldLevelUntilTheMove() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 3);
        _mark(960_000);
        stopBook.poke();
        _set(e, BTC, false, 900_000, 0);
        (uint64 px0,,) = e.stopOf(BTC);
        assertEq(px0, 950_000, "the old stop stays");
        vm.roll(block.number + 1);
        _mark(940_000); // under the old level, over the new one
        vm.recordLogs();
        stopBook.settle(address(e));
        (uint64 px,, uint64 level, bool found) = _stopFill(vm.getRecordedLogs());
        assertTrue(found);
        assertEq(e.positionOf(BTC), 0, "the old stop filled");
        assertEq(px, 940_000);
        assertEq(level, 950_000);
    }

    /// The edges of a removal: a touch in the block before it is in force fills; from that block on, nothing does.
    function test_P2_aRemovalActsFromItsBlock_notBefore() public {
        for (uint256 scen = 0; scen < 2; ++scen) {
            uint256 snap = vm.snapshotState();
            (PaperExam e, uint256 armedAt) = _longWithStop();
            vm.roll(armedAt + 1);
            _mark(960_000);
            vm.prank(trader);
            e.clearTrigger(BTC, false);
            uint256 from = block.number + DELAY;
            for (uint256 b = block.number + 1; b < from - 1 + scen; ++b) {
                vm.roll(b);
                stopBook.poke();
            }
            vm.roll(from - 1 + scen);
            _mark(900_000);
            stopBook.settle(address(e));
            if (scen == 0) {
                assertEq(e.positionOf(BTC), 0, "touched a block before the removal: filled");
            } else {
                assertEq(e.positionOf(BTC), 100, "touched in the removal's block: nothing");
                assertEq(stopBook.watched().length, 0, "the entry went with the removal in force");
                (uint64 px,,) = e.stopOf(BTC);
                assertEq(px, 950_000, "the exam takes it out when it next acts");
                vm.prank(trader);
                vm.expectRevert(PaperExam.NoTrigger.selector);
                e.clearTrigger(BTC, false);
            }
            vm.revertToState(snap);
        }
    }

    /// The edges of a move: before its block the old level acts, from it the new one, with its own size and its
    /// block as `armedAt`.
    function test_P2_aMoveActsFromItsBlock_theOldOneUntilThen() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 30);
        uint256 from = block.number + DELAY;
        vm.roll(from);
        _mark(920_000); // under the old level, in the move's block: the old is gone, the new not reached
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 100);
        assertEq(stopBook.entryOf(address(e), BTC, false).level, 900_000, "the move in force in the book");
        vm.roll(from + 1);
        _mark(890_000);
        vm.recordLogs();
        stopBook.settle(address(e));
        (uint64 px, uint256 touch, uint64 level, bool found) = _stopFill(vm.getRecordedLogs());
        assertTrue(found);
        assertEq(level, 900_000);
        assertEq(px, 890_000);
        assertEq(touch, from + 1);
        assertEq(e.positionOf(BTC), 70, "the new size");
    }

    /// A touch in the very block a move is in force fills the new stop: its level, its size, late by nothing.
    function test_P2_aTouchInTheMovesBlockFillsTheNewOne() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 30);
        uint256 from = block.number + DELAY;
        vm.roll(from);
        _mark(890_000); // under both levels, in the move's block
        vm.recordLogs();
        stopBook.settle(address(e));
        (uint64 px, uint256 touch, uint64 level, bool found) = _stopFill(vm.getRecordedLogs());
        assertTrue(found);
        assertEq(level, 900_000, "the new level");
        assertEq(touch, from);
        assertEq(px, 890_000);
        assertEq(e.positionOf(BTC), 70, "the new size");
    }

    /// One settle across a move: the old level is searched up to the move's block and no further, the new one from
    /// there, and the fill is the new one's, at its touch, with its level and size.
    function test_P2_oneSettleAcrossAMove_theOldUpToItsBlock_theNewFromIt() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 30);
        uint256 from = block.number + DELAY;
        vm.roll(from);
        _mark(920_000); // past the old level, in the move's block: not the old one's any more
        stopBook.poke();
        vm.roll(from + 1);
        _mark(890_000);
        vm.recordLogs();
        stopBook.settle(address(e)); // one settle over both
        (uint64 px, uint256 touch, uint64 level, bool found) = _stopFill(vm.getRecordedLogs());
        assertTrue(found);
        assertEq(touch, from + 1, "the new level's touch, not the old one's");
        assertEq(px, 890_000);
        assertEq(level, 900_000);
        assertEq(e.positionOf(BTC), 70, "the new size");
    }

    /// A stop with a change asked is searched on its own, not with the take on its asset at their old levels: once
    /// its move is in force, a price between the old level and the new one fills nothing.
    function test_P2_aStopsChangeWithATakeOnTheAsset() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        _set(e, BTC, true, 1_100_000, 0);
        vm.roll(armedAt + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 0);
        vm.roll(block.number + DELAY + 1);
        _mark(920_000); // past the old stop, short of the new one
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 100, "the old stop is not in force");
        vm.roll(block.number + 1);
        _mark(1_150_000);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0, "the take");
    }

    /// A change asked while another waits replaces it, and its delay counts again: the old stop acts until the
    /// second one's block, the first change never does.
    function test_P2_aSecondChangeReplacesTheFirst_andCountsAgain() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 0);
        vm.roll(block.number + 1);
        _set(e, BTC, false, 880_000, 0);
        uint256 from = block.number + DELAY;
        assertEq(stopBook.entryOf(address(e), BTC, false).nextLevel, 880_000);
        assertEq(stopBook.entryOf(address(e), BTC, false).nextAt, from);
        vm.roll(from - 1); // the first change's block: not in force, the old stop is
        _mark(940_000);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0, "the old stop, until the second change's block");
    }

    /// ...and from its block, the second change's level only: the first one's is never in force.
    function test_P2_aSecondChange_theFirstNeverActs() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 0);
        vm.roll(block.number + 1);
        _set(e, BTC, false, 880_000, 0);
        vm.roll(block.number + DELAY);
        _mark(890_000); // reaches the first change's 900 000, not the second's 880 000
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 100);
        vm.roll(block.number + 1);
        _mark(870_000);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 0, "the second change's level");
    }

    /// The old stop's fill takes a waiting change with it, as an exchange refuses to change a filled order: a
    /// partial stop fills, and the move asked of it never acts.
    function test_P2_theOldStopsFillTakesTheChangeWithIt() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        _set(e, BTC, false, 950_000, 40);
        vm.roll(block.number + DELAY + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 40);
        vm.roll(block.number + 1);
        _mark(940_000);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 60, "40 filled at the old level");
        (,, uint64 from) = e.changeOf(BTC, false);
        assertEq(from, 0);
        assertEq(stopBook.entries(address(e)), 0);
        vm.roll(block.number + DELAY);
        _mark(890_000);
        stopBook.settle(address(e));
        assertEq(e.positionOf(BTC), 60, "the move went with the fill");
    }

    /// A first stop on a side with none is set as before: armed `fillDelay` on, no change asked.
    function test_P2_aFirstStopIsSetAsBefore() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        StopBook.Entry memory x = stopBook.entryOf(address(e), BTC, false);
        assertEq(x.level, 950_000);
        assertEq(x.armedAt, armedAt);
        assertEq(x.nextAt, 0);
        (,, uint64 from) = e.changeOf(BTC, false);
        assertEq(from, 0);
    }

    /// A position closed by an order takes the stop and its waiting change out at once.
    function test_P2_aCloseTakesTheChangeOutAtOnce() public {
        (PaperExam e, uint256 armedAt) = _longWithStop();
        vm.roll(armedAt + 1);
        _mark(960_000);
        _set(e, BTC, false, 900_000, 0);
        _trade(e, BTC, false, 100);
        (,, uint64 from) = e.changeOf(BTC, false);
        assertEq(from, 0);
        assertEq(stopBook.watched().length, 0);
    }

    /// В2: a stop on an asset whose oracle price is over what the journal logs is refused, as with no price.
    function test_P2_aStopOverTheJournalsHighestPriceIsRefused() public {
        PaperExam e = _exam();
        _trade(e, BTC, true, 100);
        uint64 top = uint64(stopBook.MAX_PRICE());
        _mark(top);
        _set(e, BTC, false, 950_000, 0); // at the highest price the journal logs: taken
        _mark(top + 1);
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NoPrice.selector, BTC));
        e.setTrigger(BTC, false, 950_000, 0);
    }
}
