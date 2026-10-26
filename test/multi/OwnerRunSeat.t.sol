// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {Cancel, Breach, Terms} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {ChallengeAccount} from "../../src/ChallengeAccount.sol";
import {PoolFactory} from "../../src/PoolFactory.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// A pool whose owner chooses the trader: no challenge of its own, a funded stage opened by the
/// owner for a trader it examined some other way. Everything after the opening is the funded
/// stage every pool has, so these tests stop where that begins.
contract OwnerRunSeatTest is PoolHarness {
    address second = makeAddr("second trader");

    function _ownerRun(bool traderMayStop) internal returns (Pool p) {
        p = _readyPool();
        vm.prank(investor);
        p.runByOwner(traderMayStop);
    }

    function _funded(bool traderMayStop) internal returns (Pool p) {
        p = _ownerRun(traderMayStop);
        vm.prank(investor);
        p.openFundedFor(trader);
        CoreSimulatorLib.nextBlock();
    }

    function _settleToIdle(Pool p) internal {
        (Cancel[] memory c, uint32[] memory a) = _none();
        for (uint256 i = 0; i < 8 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            p.settleFunded(c, a);
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }

    // ── handing the pool to its owner ────────────────────────

    function test_ownerRun_setOnceByTheOwner() public {
        Pool p = _readyPool();
        assertFalse(p.ownerRun());
        vm.prank(stranger);
        vm.expectRevert(Pool.NotOwner.selector);
        p.runByOwner(true);

        vm.prank(investor);
        p.runByOwner(true);
        assertTrue(p.ownerRun());
        assertTrue(p.traderMayStop());

        // One way, and the choice about stopping goes with it.
        vm.prank(investor);
        vm.expectRevert(Pool.NotAllowed.selector);
        p.runByOwner(false);
        assertTrue(p.traderMayStop());

        assertFalse(_ownerRun(false).traderMayStop());
    }

    function test_ownerRun_refusedWhileAChallengeOfItsOwnExists() public {
        // Sold and running.
        Pool p = _readyPool();
        _started(p);
        vm.prank(investor);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Challenge));
        p.runByOwner(true);

        // Idle again, with a passed challenge that has not settled yet.
        Pool q = _readyPool();
        _passed(q);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        q.stopFunded(c, a, SALT);
        _settleToIdle(q);
        assertTrue(q.challenge() != address(0), "the passed challenge is still there");
        vm.prank(investor);
        vm.expectRevert(Pool.NotAllowed.selector);
        q.runByOwner(true);
    }

    function test_ownerRun_sellsNoChallenge() public {
        Pool p = _ownerRun(true);
        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(Pool.SalesClosed.selector);
        p.buyChallenge();
        vm.stopPrank();
    }

    // ── funding a trader ─────────────────────────────────────

    function test_openFundedFor_onlyTheOwner_ofAPoolItRuns_thatIsReady() public {
        Pool p = _readyPool();
        vm.prank(investor);
        vm.expectRevert(Pool.NotOwnerRun.selector);
        p.openFundedFor(trader);

        vm.prank(investor);
        p.runByOwner(true);
        vm.prank(stranger);
        vm.expectRevert(Pool.NotOwner.selector);
        p.openFundedFor(trader);

        // Money on the account is not enough: it has to have been prepared.
        vm.startPrank(investor);
        Pool raw = Pool(factory.createPool(_rules(), _terms()));
        raw.runByOwner(true);
        CoreSimulatorLib.forceSpotBalance(address(raw), 0, 1000e8);
        vm.expectRevert(Pool.NotReady.selector);
        raw.openFundedFor(trader);
        vm.stopPrank();
    }

    function test_openFundedFor_fundsInFull() public {
        Pool p = _ownerRun(false);
        // Exactly the funded capital of the terms, 200 USDC, and nothing over.
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 200e8);
        uint256 free = registry.freeCount();

        vm.prank(investor);
        p.openFundedFor(trader);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.fundedTrader(), trader);
        assertEq(p.fundedStart(), 200e6);
        assertEq(uint8(p.fundedEndReason()), uint8(Breach.None));
        assertEq(p.dayStartEquity(), 200e6);
        assertEq(registry.freeCount(), free - 1, "one key taken");
        assertEq(registry.keyOf(address(p)), p.agentKey());
        assertTrue(p.agentKey() != address(0));

        CoreSimulatorLib.nextBlock();
        assertEq(_equity(address(p)), 200e6, "the whole capital is on the perp side");
        assertEq(_spot(address(p)), 0);
    }

    function test_openFundedFor_notOnLessThanTheTermsSay() public {
        Pool p = _ownerRun(false);
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 200e8 - 1);
        vm.prank(investor);
        vm.expectRevert(abi.encodeWithSelector(Pool.NotEnoughCapital.selector, uint64(200e8 - 1), uint64(200e8)));
        p.openFundedFor(trader);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }

    /// Audit A-08, on this way in: the balance read is the start of the block.
    function test_openFundedFor_refusedInTheBlockOfAWithdrawal() public {
        Pool p = _ownerRun(false);
        // An owner with an account of its own, so the withdrawal costs no fee for opening one.
        CoreSimulatorLib.forceAccountActivation(investor);
        vm.startPrank(investor);
        p.withdrawOnCore(900e8);
        vm.expectRevert(Pool.WithdrawnThisBlock.selector);
        p.openFundedFor(trader);
        vm.stopPrank();

        CoreSimulatorLib.nextBlock();
        vm.prank(investor);
        vm.expectRevert(abi.encodeWithSelector(Pool.NotEnoughCapital.selector, uint64(100e8), uint64(200e8)));
        p.openFundedFor(trader);
    }

    // ── after the opening it is a funded stage like any other ─

    function test_ownerRun_theTraderStopsOnlyWhereTheOwnerLetThem() public {
        (Cancel[] memory c, uint32[] memory a) = _none();

        Pool held = _funded(false);
        vm.prank(trader);
        vm.expectRevert(Pool.NotAllowed.selector);
        held.stopFunded(c, a, SALT);
        vm.prank(stranger);
        vm.expectRevert(Pool.NotAllowed.selector);
        held.stopFunded(c, a, SALT);
        vm.prank(investor);
        held.stopFunded(c, a, SALT);
        assertEq(uint8(held.stage()), uint8(Pool.Stage.Closing));

        Pool free = _funded(true);
        vm.prank(trader);
        free.stopFunded(c, a, SALT);
        assertEq(uint8(free.stage()), uint8(Pool.Stage.Closing));
    }

    function test_ownerRun_paysTheShare_andTakesTheNextTrader() public {
        Pool p = _funded(false);
        CoreSimulatorLib.forceAccountActivation(trader);
        _trade(address(p), BTC, true, 0.01e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920); // +3% on 764 USDC of notional: +22.92
        (Cancel[] memory c, uint32[] memory a) = _none();
        uint64 before = _spot(trader);
        address firstKey = p.agentKey();

        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        _settleToIdle(p);
        // 80% of 22.92, in spot units.
        assertEq(_spot(trader) - before, 18_336_000_00);
        assertEq(p.fundedTrader(), address(0));

        CoreSimulatorLib.nextBlock();
        vm.prank(investor);
        p.openFundedFor(second);
        assertEq(p.fundedTrader(), second);
        assertTrue(p.agentKey() != firstKey, "a new stage, a new key");
    }

    function test_ownerRun_aBreachStopsItAsOnAnyPool() public {
        Pool p = _funded(false);
        // 10% under the 200 it started with, and one unit more.
        _mockMargin(address(p), 180e6 - 1, 0);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(stranger);
        p.breach(c, a, SALT);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));
        assertEq(uint8(p.fundedEndReason()), uint8(Breach.Drawdown));
    }

    /// A pool nobody handed to its owner keeps the old rule: the trader who passed may stop.
    function test_notOwnerRun_theTraderStopsAsBefore() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));
    }

    // ── only a MultiPool the factory knows (audit K-3) ───────

    function _tinyTerms() internal pure returns (Terms memory) {
        return Terms({
            price: 1,
            capital: 1,
            targetBps: 1,
            duration: 1 days,
            traderShareChallengeBps: 0,
            traderShareFundedBps: 0,
            fundedCapital: 1
        });
    }

    /// The audit's K-3, turned round: a stranger's own pool, run by its owner, took a key from the
    /// shared registry at every stage it opened and retired it at the end, for one account
    /// activation and gas, until the registry was empty. An owner the factory does not know as a
    /// MultiPool now opens nothing, and the keys stay for the pools that sell exams.
    function test_deep_K3_aStrangersOwnPoolTakesNoKey() public {
        vm.clearMockedCalls();
        address attacker = makeAddr("k3 attacker");
        vm.prank(attacker);
        Pool p = Pool(factory.createPool(_rules(), _tinyTerms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1e8);
        p.prepareAccount();
        vm.prank(attacker);
        p.runByOwner(true);
        uint256 free0 = registry.freeCount();
        vm.prank(attacker);
        vm.expectRevert(Pool.NotOwnerRun.selector);
        p.openFundedFor(attacker);
        assertEq(registry.freeCount(), free0, "no key taken");

        // An owner the operator recognized funds its trader on the same factory.
        vm.prank(operator);
        factory.setMultiPool(investor, true);
        Pool honest = _ownerRun(false);
        vm.prank(investor);
        honest.openFundedFor(trader);
        assertEq(uint8(honest.stage()), uint8(Pool.Stage.Funded));
        assertEq(registry.freeCount(), free0 - 1);
    }

    /// Only the operator recognizes, and may take the recognition back: from then on the pool's
    /// next stage is refused.
    function test_deep_K3_onlyTheOperatorRecognizes_andMayTakeItBack() public {
        vm.clearMockedCalls();
        vm.prank(stranger);
        vm.expectRevert(PoolFactory.NotOperator.selector);
        factory.setMultiPool(stranger, true);
        vm.prank(operator);
        vm.expectEmit(true, false, false, true, address(factory));
        emit PoolFactory.MultiPoolSet(investor, true);
        factory.setMultiPool(investor, true);
        assertTrue(factory.isMultiPool(investor));
        Pool p = _funded(false);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        _settleToIdle(p);
        vm.prank(operator);
        factory.setMultiPool(investor, false);
        vm.prank(investor);
        vm.expectRevert(Pool.NotOwnerRun.selector);
        p.openFundedFor(trader);
    }
}
