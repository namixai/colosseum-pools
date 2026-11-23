// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {StopBookBase} from "./StopBook.t.sol";
import {AuditBook} from "./StopBookAudit7Model.t.sol";

contract StopBookAudit7ScenariosTest is StopBookBase {
    function setUp() public override {
        super.setUp();
        stopBook = new AuditBook();
    }

    function _delay(uint16 d) internal returns (PaperExam e) {
        ExamTerms memory t = _terms();
        t.fillDelay = d;
        e = _examWith(_two(), t, false);
        _trade(e, BTC, true, 100);
    }

    function _clearT(PaperExam e, uint32 a, bool tk) internal {
        vm.prank(trader);
        e.clearTrigger(a, tk);
    }

    /// (a) A change asked a block before a wick. With `fillDelay` 2 the stop there is carried out at the wick; with
    /// `fillDelay` 1 the change would be in force in the wick's own block, so a simulated pool's plan takes 2 at least
    /// (`SimPoolFactory.MIN_FILL_DELAY`; the auditor's `..._delay1_escapes`, turned round:
    /// `test_a7_planWithFillDelay1_isRefused` in test/sim/SimPool.t.sol).
    function test_a7_change_aBlockBeforeTheWick_delay2_stopFires() public {
        PaperExam e = _delay(2);
        _set(e, BTC, false, 950_000, 0);
        vm.roll(block.number + 2);
        _check(); // armed
        _set(e, BTC, false, 900_000, 0); // asked in N, in force in N+2
        vm.roll(block.number + 1);
        _mark(940_000); // the wick, N+1
        _check();
        assertEq(e.positionOf(BTC), 0, "the old stop was carried out at the wick");
    }

    /// Two changes in a row: the second replaces the first and counts the delay again; the old stop stays armed until then.
    function test_a7_change_twoInARow_oldStaysArmedUntilTheLast() public {
        PaperExam e = _delay(3);
        _set(e, BTC, false, 950_000, 0);
        vm.roll(block.number + 3);
        _check();
        _set(e, BTC, false, 900_000, 0); // in force N+3
        vm.roll(block.number + 2);
        _check();
        _set(e, BTC, false, 880_000, 0); // replaces it: in force N+5
        StopBook.Entry memory en = stopBook.entryOf(address(e), BTC, false);
        assertEq(en.level, 950_000);
        assertEq(en.nextLevel, 880_000);
        vm.roll(block.number + 2); // N+4... the old change's block has passed, the new is not in force
        _mark(940_000);
        _check();
        assertEq(e.positionOf(BTC), 0, "the old stop, not the replaced change, takes the touch");
    }

    /// A change asked in the block of the touch: the book's own check finds the touch first.
    function test_a7_change_inTheTouchBlock_isTooLate() public {
        PaperExam e = _delay(2);
        _set(e, BTC, false, 950_000, 0);
        vm.roll(block.number + 2);
        _mark(940_000);
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.clearTrigger(BTC, false); // settled first: carried out, nothing to remove (the whole call reverts)
        assertEq(e.positionOf(BTC), 100, "the reverted call carried nothing out");
        stopBook.settle(address(e)); // anyone: the touch is carried out at its block's price
        assertEq(e.positionOf(BTC), 0);
    }

    /// A change pending, then an order closes the position: the book keeps nothing, the asset is not logged, no count left.
    function test_a7_change_thenOrderCloses_bookEmpty() public {
        PaperExam e = _delay(2);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_100_000, 0);
        vm.roll(block.number + 2);
        _check();
        _set(e, BTC, false, 900_000, 0);
        _set(e, BTC, true, 1_200_000, 0);
        _trade(e, BTC, false, 100); // sells all; fills before the changes are in force
        assertEq(e.positionOf(BTC), 0);
        assertEq(stopBook.watched().length, 0);
        assertEq(AuditBook(address(stopBook)).countOf(BTC), 0);
        assertEq(stopBook.entries(address(e)), 0);
        (uint64 p,,) = e.stopOf(BTC);
        (uint64 cp,, uint64 ca) = e.changeOf(BTC, false);
        assertEq(p, 0);
        assertEq(cp, 0);
        assertEq(ca, 0);
    }

    /// A stop and a take, both with changes pending, the wick in between hits the old stop and the position closes: both
    /// entries go, with their changes, and the take's change is not promoted later.
    function test_a7_change_stopAndTake_bothPending_stopFiresClosesAll() public {
        PaperExam e = _delay(3);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_100_000, 0);
        vm.roll(block.number + 3);
        _check();
        _set(e, BTC, false, 900_000, 0);
        _set(e, BTC, true, 1_150_000, 0);
        vm.roll(block.number + 1);
        _mark(949_000);
        _check();
        assertEq(e.positionOf(BTC), 0);
        assertEq(stopBook.entries(address(e)), 0);
        vm.roll(block.number + 5);
        _mark(1_200_000);
        _check();
        assertEq(e.positionOf(BTC), 0);
        assertEq(AuditBook(address(stopBook)).countOf(BTC), 0);
    }

    /// (e) The path with no book: `initialize`, the old way. Set, replaced at once, cleared at once, taken by anyone.
    function test_a7_noBookExam_unchanged() public {
        PaperExam e = PaperExam(Clones.clone(address(impl)));
        e.initialize(trader, _rules(_two()), _terms());
        assertEq(e.stopBook(), address(0));
        // the exam's own pool is this test (initialize records the caller); open a position through the order path
        vm.prank(trader);
        e.place(BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.prank(stranger);
        e.fill();
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, false, 940_000, 0); // replaced at once: no change
        (uint64 p,, uint64 a) = e.stopOf(BTC);
        assertEq(p, 940_000);
        (uint64 cp,, uint64 ca) = e.changeOf(BTC, false);
        assertEq(cp + ca, 0, "no change without a book");
        vm.roll(block.number + DELAY);
        _mark(930_000);
        vm.prank(stranger);
        e.fillTrigger(BTC, false);
        assertEq(e.positionOf(BTC), 0);
        a;
    }

    function test_a7_noBookExam_clearIsImmediate() public {
        PaperExam e = PaperExam(Clones.clone(address(impl)));
        e.initialize(trader, _rules(_two()), _terms());
        vm.prank(trader);
        e.place(BTC, true, 100);
        vm.roll(block.number + DELAY);
        vm.prank(stranger);
        e.fill();
        _set(e, BTC, false, 950_000, 0);
        _clearT(e, BTC, false);
        (uint64 p,,) = e.stopOf(BTC);
        assertEq(p, 0, "cleared at once");
        vm.prank(trader);
        vm.expectRevert(PaperExam.NoTrigger.selector);
        e.clearTrigger(BTC, false);
    }

    /// (з) What the events say: a stop set, moved, touched in the old phase: `TriggerSet` twice (px, armedAt), then
    /// `Fired` with the touch block, `TriggerFilled` with the level of the version carried out, and `TriggerCleared`.
    function test_a7_events_reconstructWhichVersionFilledAtWhatPrice() public {
        PaperExam e = _delay(2);
        vm.recordLogs();
        _set(e, BTC, false, 950_000, 0);
        uint256 a1 = block.number + 2;
        vm.roll(block.number + 2);
        _check();
        _set(e, BTC, false, 900_000, 0);
        uint256 a2 = block.number + 2;
        vm.roll(block.number + 1);
        _mark(930_000); // under the old (950 000), over the new (900 000)
        uint256 touch = block.number;
        _check();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 sets;
        bool fired;
        bool filled;
        bool cleared;
        for (uint256 i = 0; i < logs.length; ++i) {
            bytes32 t0 = logs[i].topics[0];
            if (t0 == PaperExam.TriggerSet.selector) {
                (, uint64 px,, uint256 armed) = abi.decode(logs[i].data, (bool, uint64, uint64, uint256));
                ++sets;
                assertEq(armed, sets == 1 ? a1 : a2);
                assertEq(px, sets == 1 ? 950_000 : 900_000);
            } else if (t0 == StopBook.Fired.selector) {
                (, uint64 px, uint256 blk, uint8 res) = abi.decode(logs[i].data, (bool, uint64, uint256, uint8));
                assertEq(px, 930_000);
                assertEq(blk, touch);
                assertEq(res, 2);
                fired = true;
            } else if (t0 == PaperExam.TriggerFilled.selector) {
                (, uint64 sz, uint64 fpx,, uint64 level,, uint256 tb, uint64 tpx) =
                    abi.decode(logs[i].data, (bool, uint64, uint64, uint256, uint64, uint8, uint256, uint64));
                assertEq(level, 950_000, "the version carried out: the old one");
                assertEq(sz, 100);
                assertEq(fpx, 930_000);
                assertEq(tb, touch);
                assertEq(tpx, 930_000);
                filled = true;
            } else if (t0 == PaperExam.TriggerCleared.selector) {
                cleared = true; // for the version carried out, and with it the change
            }
        }
        assertEq(sets, 2);
        assertTrue(fired && filled && cleared);
        (uint64 cp,,) = e.changeOf(BTC, false);
        assertEq(cp, 0, "the change went with the fill");
    }

    // ── P4: the top of the price range ───────────────────────────

    function test_a7_p4_priceOverMax_isNone_andSetTriggerReverts() public {
        PaperExam e = _delay(2);
        _set(e, BTC, false, 950_000, 0);
        vm.roll(block.number + 2);
        _mark(uint64(stopBook.MAX_PRICE()) + 1);
        stopBook.poke();
        assertEq(stopBook.priceAt(BTC, stopBook.logged() - 1), 0, "over the top: logged as none");
        assertEq(e.positionOf(BTC), 100);
        uint64 top = uint64(stopBook.MAX_PRICE());
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(PaperExam.NoPrice.selector, BTC));
        e.setTrigger(BTC, true, top, 0);
        _mark(uint64(stopBook.MAX_PRICE()));
        vm.roll(block.number + 1);
        stopBook.poke();
        assertEq(stopBook.priceAt(BTC, stopBook.logged() - 1), stopBook.MAX_PRICE(), "at the top: a price");
    }

    function test_a7_p4_levelOverMax_isRefused_levelAtMaxIsKept() public {
        PaperExam e = _examOn(_two(), false);
        _trade(e, BTC, false, 100); // short: its stop fires at or over its level
        uint64 top = uint64(stopBook.MAX_PRICE());
        vm.prank(trader);
        vm.expectRevert(abi.encodeWithSelector(StopBook.LevelTooHigh.selector, top + 1));
        e.setTrigger(BTC, false, top + 1, 0);
        _set(e, BTC, false, uint64(stopBook.MAX_PRICE()), 0);
        assertEq(stopBook.entryOf(address(e), BTC, false).level, uint32(stopBook.MAX_PRICE()));
        vm.roll(block.number + DELAY);
        _mark(uint64(stopBook.MAX_PRICE()));
        _check();
        assertEq(e.positionOf(BTC), 0, "a touch at exactly the top fires");
    }

    // ── an exam that ends with entries and changes in the book ────

    function test_a7_end_expire_withChangesPending_leavesTheBookEmpty() public {
        PaperExam e = _delay(2);
        _trade(e, ETH, true, 100);
        _set(e, BTC, false, 950_000, 0);
        _set(e, BTC, true, 1_100_000, 0);
        _set(e, ETH, false, 200_000, 0);
        vm.roll(block.number + 2);
        _check();
        _set(e, BTC, false, 900_000, 0);
        _clearT(e, ETH, false);
        assertEq(stopBook.entries(address(e)), 3);
        vm.warp(block.timestamp + 8 days);
        vm.roll(block.number + 1);
        _mark(MARK);
        e.expire();
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Expired));
        assertEq(stopBook.entries(address(e)), 0);
        assertEq(stopBook.watched().length, 0);
        assertEq(AuditBook(address(stopBook)).countOf(BTC), 0);
        assertEq(AuditBook(address(stopBook)).countOf(ETH), 0);
        // the journal stops: no asset, no position logged
        uint256 l = stopBook.logged();
        vm.roll(block.number + 1);
        stopBook.poke();
        assertEq(stopBook.logged(), l);
    }

    function test_a7_end_breach_touchedStopFirst_thenBreach() public {
        PaperExam e = _delay(2);
        _set(e, BTC, false, 600_000, 0); // a stop at -40 %: the loss of the stop is the breach (drawdown 50 %)
        vm.roll(block.number + 2);
        _mark(450_000); // under the stop in one block: the stop fills at 450 000 and the account is under the line
        e.breach(); // settled first: the stop is carried out (position closed), then the judge
        assertEq(e.positionOf(BTC), 0);
        assertEq(uint8(e.status()), uint8(PaperExam.Status.Breached));
        assertEq(stopBook.entries(address(e)), 0);
    }
}
