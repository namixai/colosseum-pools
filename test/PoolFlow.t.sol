// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;
import {Test, Vm} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreState} from "@hyper-evm-lib/test/simulation/hyper-core/CoreState.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";
import {Rules, Terms, Cancel, Breach, Units} from "../src/Types.sol";
import {KeyRegistry} from "../src/KeyRegistry.sol";
import {PoolFactory} from "../src/PoolFactory.sol";
import {Pool} from "../src/Pool.sol";
import {ChallengeAccount} from "../src/ChallengeAccount.sol";
import {RuledAccount} from "../src/RuledAccount.sol";
import {CoreOps} from "../src/lib/CoreOps.sol";

import {PoolHarness, CloseHarness, MockUsdc, TwoStepsInOneBlock} from "./PoolHarness.sol";

contract PoolFlowTest is PoolHarness {
    function test_createPool_checksRulesAndTerms() public {
        Rules memory r = _rules();
        Terms memory t = _terms();

        r.assets[0] = 7; // not listed by the platform
        vm.expectRevert(abi.encodeWithSelector(PoolFactory.AssetNotListed.selector, uint32(7)));
        factory.createPool(r, t);

        r = _rules();
        r.maxDrawdownBps = 0;
        vm.expectRevert(PoolFactory.BadRules.selector);
        factory.createPool(r, t);

        r = _rules();
        r.maxLeverageX100 = 99;
        vm.expectRevert(PoolFactory.BadRules.selector);
        factory.createPool(r, t);

        t.capital = 0;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t = _terms();
        t.traderShareChallengeBps = 10_001;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t = _terms();
        t.traderShareFundedBps = 10_001;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        // A challenge given away spends the investor's capital and a published key on nothing.
        t = _terms();
        t.price = 0;
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t.price = 1; // one unit is enough; the contract sets no floor above zero
        factory.createPool(_rules(), t);
    }
    function test_createPool_refusesCapitalThatCannotBeHeldOnSpot() public {
        // capital + fundedCapital, in spot units, plus the fee for the challenge's new account
        uint64 limit = (type(uint64).max - Units.NEW_ACCOUNT_FEE) / Units.SPOT_PER_PERP;
        Terms memory t = _terms();
        t.capital = 1;
        t.fundedCapital = limit; // one unit over what a uint64 spot balance can hold
        vm.expectRevert(PoolFactory.BadTerms.selector);
        factory.createPool(_rules(), t);

        t.fundedCapital = limit - 1; // exactly at the limit
        factory.createPool(_rules(), t);
    }
    /// Capital reaches a pool as a HyperCore spot transfer. There is no HyperEVM deposit to
    /// call: on testnet the USDC bridge credits nothing to a contract bridging to itself.
    function test_capitalOnlyArrivesOnCore() public {
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        deal(address(usdc), investor, 50e6);
        vm.startPrank(investor);
        usdc.approve(address(p), 50e6);
        (bool ok,) = address(p).call(abi.encodeWithSignature("deposit(uint256)", 50e6));
        vm.stopPrank();
        assertFalse(ok, "no HyperEVM deposit entry point");
        assertEq(usdc.balanceOf(investor), 50e6, "nothing was pulled");

        // The investor's spot transfer on HyperCore is what makes the pool sellable.
        CoreSimulatorLib.forceSpotBalance(address(p), 0, p.capitalNeeded());
        p.prepareAccount();
        _buy(p);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Challenge));
    }
    function test_implementationsCannotBeInitialized() public {
        Pool impl = Pool(factory.poolImpl());
        vm.expectRevert();
        impl.initialize(factory, stranger, _rules(), _terms());
    }
    function test_prepareAccount_needsCoreAccount_andSeparatesBalances() public {
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        vm.expectRevert(Pool.NotReady.selector);
        p.prepareAccount();

        CoreSimulatorLib.forceSpotBalance(address(p), 0, 10e8);
        vm.expectEmit(true, false, false, true, CORE_WRITER);
        emit RawAction(address(p), abi.encodePacked(uint8(1), uint24(16), abi.encode(address(p), uint8(1))));
        p.prepareAccount();
        assertTrue(p.accountReady());
    }
    /// The pool needs the challenge capital, the funded capital, and 1 USDC for creating the
    /// challenge's account, which HyperCore charges the sender on top of the transfer.
    function test_buyChallenge_needsCapitalForChallengeAndFunding() public {
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        assertEq(p.capitalNeeded(), 301e8);
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 301e8 - 1);
        p.prepareAccount();
        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(abi.encodeWithSelector(Pool.NotEnoughCapital.selector, uint64(301e8 - 1), uint64(301e8)));
        p.buyChallenge();
        vm.stopPrank();

        // Exactly enough: the capital arrives in full and the funded capital is still there.
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 301e8);
        vm.prank(trader);
        ChallengeAccount ch = ChallengeAccount(p.buyChallenge());
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), 100e8, "challenge capital in full");
        assertEq(_spot(address(p)), 200e8, "funded capital left after the account fee");
    }
    function test_buyChallenge_reservesKey_andSendsCapital() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);

        assertEq(uint8(p.stage()), uint8(Pool.Stage.Challenge));
        assertEq(usdc.balanceOf(address(p)), 25e6, "price held by the pool");
        assertEq(p.heldPrice(), 25e6);
        address key = ch.agentKey();
        assertTrue(registry.isBound(key, address(ch), trader), "key reserved for this challenge and trader");
        assertTrue(factory.isChallenge(address(ch)));

        // A second buyer is refused while the pool is taken.
        address other = makeAddr("other");
        deal(address(usdc), other, 25e6);
        vm.startPrank(other);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Challenge));
        p.buyChallenge();
        vm.stopPrank();

        assertEq(_spot(address(ch)), 0, "nothing moves inside the block");
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), 100e8);
    }
    function test_activate_waitsForCapital_thenStarts() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);

        vm.expectRevert(ChallengeAccount.NotStartedOnCore.selector);
        ch.activate();

        CoreSimulatorLib.nextBlock();
        address key = ch.agentKey();
        vm.recordLogs();
        ch.activate();
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());

        // Separate balances, capital to perp, then the reserved key as the unnamed agent.
        assertEq(kind.length, 3);
        assertEq(from[0], address(ch));
        assertEq(kind[0], 16);
        assertEq(args[0], abi.encode(address(ch), uint8(1)));
        assertEq(kind[1], 7);
        assertEq(args[1], abi.encode(uint64(100e6), true));
        assertEq(kind[2], 9);
        assertEq(args[2], abi.encode(key, ""));

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
        assertEq(p.earned(), 25e6, "price becomes the investor's once the challenge starts");
        assertEq(p.heldPrice(), 0);

        CoreSimulatorLib.nextBlock();
        assertEq(_equity(address(ch)), int64(100e6));
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));

        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.BadStatus.selector, ChallengeAccount.Status.Active));
        ch.activate();
    }
    function test_noBreach_meansNoStop() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 95e6, 380e6); // -5% total, 4x: inside every rule
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.expectRevert(RuledAccount.NoBreach.selector);
        ch.breach(c, a, SALT);
    }
    function test_drawdown_boundary() public {
        ChallengeAccount ch = _started(_readyPool());
        // Floor is 90 USDC: exactly 90 is allowed, a micro-dollar less is not. The day
        // limit (95 from the first day's base) would trip first, so take a new day's
        // snapshot at 90.
        _nextMidnight();
        _mockMargin(address(ch), 90e6, 0);
        ch.checkpoint();
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 90e6 - 1, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Drawdown));
    }
    function test_dailyLoss_boundary() public {
        ChallengeAccount ch = _started(_readyPool());
        // Day base is the capital, 100; the limit is 5%.
        _mockMargin(address(ch), 95e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 95e6 - 1, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.DailyLoss));
    }
    function test_dailyLoss_countsFromTheDaysSnapshot() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 96e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));

        // Next day: the snapshot takes the new base, once.
        _nextMidnight();
        ch.checkpoint();
        assertEq(ch.dayStartEquity(), int64(96e6));
        _mockMargin(address(ch), 92e6, 0);
        vm.expectRevert(RuledAccount.AlreadyCheckpointed.selector);
        ch.checkpoint();
        // 96 * 0.95 = 91.2: 92 is fine, 91.1 is not, and both are above the 90 floor.
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 91.1e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.DailyLoss));
    }
    /// Nobody can take the snapshot later in the day, the trader included: a base picked
    /// after a loss would let the day lose twice.
    function test_checkpoint_onlyJustAfterMidnight() public {
        ChallengeAccount ch = _started(_readyPool());
        vm.warp((block.timestamp / 1 days + 1) * 1 days + 15 minutes);
        _mockMargin(address(ch), 97e6, 0);
        vm.expectRevert(RuledAccount.OutsideCheckpointWindow.selector);
        ch.checkpoint();
        assertEq(ch.dayStartEquity(), int64(100e6));
    }
    /// Without a snapshot for the new day, the last one stays in force: the rule is still
    /// checked, by the stop and by graduation alike.
    function test_dailyLoss_carriesTheLastSnapshot() public {
        ChallengeAccount ch = _started(_readyPool());
        vm.warp(block.timestamp + 1 days + 1 hours);
        _mockMargin(address(ch), 94e6, 0);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.DailyLoss));
        (Cancel[] memory c, uint32[] memory a) = _none();
        ch.breach(c, a, SALT);
        assertEq(uint8(ch.breachReason()), uint8(Breach.DailyLoss));
    }
    function test_graduate_refusedWhileTheDayIsDown() public {
        ChallengeAccount ch = _started(_readyPool());
        _nextMidnight();
        _mockMargin(address(ch), 120e6, 0);
        ch.checkpoint();
        _mockMargin(address(ch), 113e6, 0); // above the 108 target, 5.8% under today's 120
        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.RuleBroken.selector, Breach.DailyLoss));
        ch.graduate(SALT);
    }
    function test_leverage_boundary() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 100e6, 500e6); // exactly 5x
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        _mockMargin(address(ch), 100e6, 500e6 + 1);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Leverage));
    }
    function test_negativeEquity_withPositions_isADrawdown() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), -1, 10e6);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Drawdown));
    }
    function test_forbiddenAsset_onlyWhenNamed() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockPosition(address(ch), ETH, 100); // ETH is listed by the platform, not by this pool
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
        uint32[] memory extra = new uint32[](1);
        extra[0] = ETH;
        assertEq(uint8(ch.violation(extra)), uint8(Breach.ForbiddenAsset));
        // A position in an allowed asset named by the caller is not a breach by itself.
        extra[0] = BTC;
        _mockPosition(address(ch), BTC, 100);
        assertEq(uint8(ch.violation(extra)), uint8(Breach.None));
    }
    function test_breach_cutsAgent_cancels_closes_thenSettles() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        address key = ch.agentKey();
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 741080);

        Cancel[] memory cancels = new Cancel[](1);
        cancels[0] = Cancel({asset: BTC, oid: 424242});
        _mockMargin(address(ch), 88.5e6, 370e6); // what Hyperliquid would report after -3%
        vm.recordLogs();
        vm.prank(stranger);
        ch.breach(cancels, new uint32[](0), SALT);
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        vm.clearMockedCalls();

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Breached));
        assertEq(uint8(ch.breachReason()), uint8(Breach.Drawdown));
        assertEq(kind.length, 3);

        // 1. the agent is replaced by an address nobody holds, and the key is retired
        assertEq(from[0], address(ch));
        assertEq(kind[0], 9);
        (address keyless, string memory name) = abi.decode(args[0], (address, string));
        assertTrue(keyless != key && keyless != address(0));
        assertEq(bytes(name).length, 0);
        assertFalse(PrecompileLib.coreUserExists(keyless));
        assertEq(uint8(registry.bindingOf(key).state), uint8(KeyRegistry.State.Retired));
        assertEq(ch.agentKey(), address(0));
        assertEq(ch.cutKey(), key, "the cut key stays on record for whoever checks it");
        assertEq(ch.cutBlock(), block.number);

        // 2. the named order is cancelled
        assertEq(kind[1], 10);
        assertEq(args[1], abi.encode(BTC, uint64(424242)));

        // 3. the long is closed: sell, 5% under the mark, rounded, reduce-only, IOC.
        //    74108.0 * 0.95 = 70402.6 -> 70402.0 -> 1e8 scale
        assertEq(kind[2], 1);
        assertEq(args[2], abi.encode(BTC, false, uint64(7040200000000), uint64(0.005e8), true, uint8(3), uint128(0)));

        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.position(address(ch), BTC).szi, 0, "position closed");

        uint64 poolSpotBefore = _spot(address(p));
        int64 left = _equity(address(ch));
        _settleChallenge(ch);
        assertEq(_spot(address(p)), poolSpotBefore + uint64(left) * 100, "everything back in the pool");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(p.challenge(), address(0));
    }
    function test_breach_skipsAKeylessAddressSomeoneActivated() public {
        ChallengeAccount ch = _started(_readyPool());
        address key = ch.agentKey();
        address first = _keylessCandidate(address(ch), 0, SALT, 0);
        address second = _keylessCandidate(address(ch), 0, SALT, 1);
        CoreSimulatorLib.forceAccountActivation(first);
        _mockMargin(address(ch), 80e6, 0);

        vm.recordLogs();
        (Cancel[] memory c, uint32[] memory a) = _none();
        ch.breach(c, a, SALT);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(kind[0], 9);
        (address keyless,) = abi.decode(args[0], (address, string));
        assertEq(keyless, second, "the pre-activated first candidate is skipped");
        assertTrue(keyless != key);
        assertFalse(PrecompileLib.coreUserExists(keyless));
    }
    /// Even if every candidate was funded in advance, the stop goes through: it uses the last
    /// candidate rather than revert, and anyone can replace the agent again with `recut`.
    function test_breach_goesThroughEvenIfEveryCandidateWasFunded() public {
        ChallengeAccount ch = _started(_readyPool());
        address last;
        for (uint256 i = 0; i < 8; ++i) {
            last = _keylessCandidate(address(ch), 0, SALT, i);
            CoreSimulatorLib.forceAccountActivation(last);
        }
        _mockMargin(address(ch), 80e6, 0);
        (Cancel[] memory c, uint32[] memory a) = _none();

        vm.expectRevert(RuledAccount.NotStopped.selector);
        ch.recut(SALT);

        vm.recordLogs();
        vm.prank(stranger);
        ch.breach(c, a, SALT);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Breached));
        assertEq(kind[0], 9);
        (address used,) = abi.decode(args[0], (address, string));
        assertEq(used, last);

        bytes32 other = keccak256("another salt");
        address fresh = _keylessCandidate(address(ch), 1, other, 0);
        vm.recordLogs();
        vm.prank(stranger);
        ch.recut(other);
        (, kind, args) = _actions(vm.getRecordedLogs());
        assertEq(kind.length, 1);
        assertEq(kind[0], 9);
        (used,) = abi.decode(args[0], (address, string));
        assertEq(used, fresh);
    }
    /// `recut` replaces the agent again but keeps pointing at the key that was cut, so a
    /// keeper can still check whether that key lost its agent role.
    function test_recut_keepsTheCutKeyOnRecord() public {
        ChallengeAccount ch = _started(_readyPool());
        address key = ch.agentKey();
        _mockMargin(address(ch), 80e6, 0);
        (Cancel[] memory c, uint32[] memory a) = _none();
        ch.breach(c, a, SALT);
        vm.clearMockedCalls();
        uint64 first = ch.cutBlock();

        vm.roll(block.number + 7);
        vm.prank(stranger);
        ch.recut(keccak256("again"));
        assertEq(ch.cutKey(), key);
        assertEq(ch.cutBlock(), first + 7, "the latest replacement's block");
    }
    /// A reserved key that gained a HyperCore account before the start can't become the
    /// challenge's agent. The challenge refuses to start, and anyone can abort it at once:
    /// the trader gets the price back and the capital goes back to the pool.
    function test_aSpoiledKey_blocksTheStart_andAbortRefunds() public {
        Pool p = _readyPool();
        uint64 poolSpot = _spot(address(p));
        ChallengeAccount ch = _buy(p);
        CoreSimulatorLib.nextBlock();
        assertFalse(ch.keySpoiled());
        CoreSimulatorLib.forceAccountActivation(ch.agentKey());
        assertTrue(ch.keySpoiled());
        assertTrue(ch.capitalArrived());

        vm.expectRevert(ChallengeAccount.KeySpoiled.selector);
        ch.activate();

        address key = ch.agentKey();
        vm.prank(stranger);
        ch.abort(); // inside the start window, with the capital there
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Aborted));
        assertEq(usdc.balanceOf(trader), 25e6, "price refunded");
        assertEq(uint8(registry.bindingOf(key).state), uint8(KeyRegistry.State.Retired));
        _settleChallenge(ch);
        assertEq(_spot(address(p)), poolSpot - Units.NEW_ACCOUNT_FEE, "capital back; the account fee is spent");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }
    /// A resting order holds margin, and nobody can list open orders on chain, so every
    /// settlement step accepts the orders to cancel, not just the stop.
    function test_settle_cancelsNamedOrdersEveryCall() public {
        ChallengeAccount ch = _started(_readyPool());
        _mockMargin(address(ch), 80e6, 0);
        (Cancel[] memory none, uint32[] memory a) = _none();
        ch.breach(none, a, SALT);
        vm.clearMockedCalls();

        Cancel[] memory late = new Cancel[](2);
        late[0] = Cancel({asset: BTC, oid: 11});
        late[1] = Cancel({asset: BTC, oid: 12});
        for (uint256 round = 0; round < 2; ++round) {
            vm.recordLogs();
            ch.settle(late, a);
            (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
            assertEq(kind[0], 10);
            assertEq(args[0], abi.encode(BTC, uint64(11)));
            assertEq(kind[1], 10);
            assertEq(args[1], abi.encode(BTC, uint64(12)));
            CoreSimulatorLib.nextBlock();
        }
    }
    function test_abort_refusedOnceTheCapitalArrived() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);
        CoreSimulatorLib.nextBlock();
        vm.warp(block.timestamp + 1 hours + 1);
        vm.prank(stranger);
        vm.expectRevert(ChallengeAccount.CapitalArrived.selector);
        ch.abort();
        ch.activate();
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
    }
    /// Settlement may be called every block while HyperCore is still executing the last
    /// send. The trader's share still goes out once.
    function test_payoutIsSentOnce() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        (Cancel[] memory c, uint32[] memory a) = _none();

        ch.settle(c, a); // perp -> spot
        CoreSimulatorLib.nextBlock();

        vm.recordLogs();
        ch.settle(c, a); // payout
        ch.settle(c, a); // same block: the spot balance still looks untouched
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        uint256 toTrader;
        uint256 toPool;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (from[i] != address(ch) || kind[i] != 6) continue;
            (address to,,) = abi.decode(args[i], (address, uint64, uint64));
            if (to == trader) ++toTrader;
            if (to == address(p)) ++toPool;
        }
        assertEq(toTrader, 1, "one payout send");
        assertEq(toPool, 0, "nothing to the pool before the payout lands");
        assertEq(ch.payoutSent(), ch.payoutOwed());

        _settleChallenge(ch);
        assertEq(_spot(trader), ch.payoutOwed());
    }
    /// After a funded stage closes, the passed challenge may still be settling. The pool
    /// must not sell a new challenge until it has, or the old one could never report back.
    function test_noNewChallengeWhileThePassedOneSettles() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();

        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        for (uint256 i = 0; i < 6 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            p.settleFunded(c, a);
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(p.challenge(), address(ch), "the passed challenge has not settled yet");

        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Idle));
        p.buyChallenge();
        vm.stopPrank();

        _settleChallenge(ch);
        assertEq(p.challenge(), address(0));
        _buy(p);
    }
    function test_expire_and_forfeit() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        (Cancel[] memory c, uint32[] memory a) = _none();

        vm.expectRevert(ChallengeAccount.TooEarly.selector);
        ch.expire(c, a, SALT);
        vm.prank(stranger);
        vm.expectRevert(ChallengeAccount.NotTrader.selector);
        ch.forfeit(c, a, SALT);

        vm.warp(block.timestamp + 7 days + 1);
        ch.expire(c, a, SALT);
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Expired));
        _settleChallenge(ch);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));

        ChallengeAccount ch2 = _started(p);
        vm.prank(trader);
        ch2.forfeit(c, a, SALT);
        assertEq(uint8(ch2.status()), uint8(ChallengeAccount.Status.Forfeited));
    }
    function test_abort_refundsTheTrader() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);
        address key = ch.agentKey();

        vm.expectRevert(ChallengeAccount.TooEarly.selector);
        ch.abort();

        vm.warp(block.timestamp + 1 hours + 1);
        ch.abort();
        assertEq(usdc.balanceOf(trader), 25e6, "refunded");
        assertEq(p.heldPrice(), 0);
        assertEq(uint8(registry.bindingOf(key).state), uint8(KeyRegistry.State.Retired));

        // The capital lands after the abort and goes straight back.
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }
    function test_buyChallenge_paysThePlatformFee_whichAnAbortKeeps() public {
        address feeTo = makeAddr("fee-recipient");
        vm.prank(operator);
        factory.setChallengeFee(3e6, feeTo);
        Pool p = _readyPool();

        deal(address(usdc), trader, 28e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(); // the price alone is approved, not the fee
        p.buyChallenge();
        usdc.approve(address(p), 28e6);
        ChallengeAccount ch = ChallengeAccount(p.buyChallenge());
        vm.stopPrank();
        assertEq(usdc.balanceOf(feeTo), 3e6);
        assertEq(usdc.balanceOf(address(p)), 25e6);

        vm.warp(block.timestamp + 1 hours + 1);
        ch.abort();
        assertEq(usdc.balanceOf(trader), 25e6, "the price comes back");
        assertEq(usdc.balanceOf(feeTo), 3e6, "the fee doesn't");
    }
    function test_setChallengeFee_operatorOnly_andNeedsARecipient() public {
        vm.prank(stranger);
        vm.expectRevert(PoolFactory.NotOperator.selector);
        factory.setChallengeFee(1, stranger);

        vm.startPrank(operator);
        vm.expectRevert(PoolFactory.BadFee.selector);
        factory.setChallengeFee(1, address(0));
        factory.setChallengeFee(0, address(0)); // switching it off needs no recipient
        vm.stopPrank();
        assertEq(factory.challengeFee(), 0);
    }
    /// A stranger can spoil every published key by giving it a HyperCore account, one USDC
    /// each. Before the sale reserved the funded key, that was enough to take the funded stage
    /// away from a trader who had already met the target: graduate reached into an empty
    /// registry and reverted, and after the deadline the trader could only expire -- no funded
    /// stage, no share, and the challenge's profit left in the pool. Now the key is already in
    /// hand when the trader passes, so nobody outside can reach it.
    function test_graduate_recordsThePassWithTheRegistryDrained_andTheStageOpensLater() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);

        uint256 spoiled;
        for (uint256 i = 0; i < keys.length; ++i) {
            if (registry.bindingOf(keys[i]).state == KeyRegistry.State.Free) {
                CoreSimulatorLib.forceAccountActivation(keys[i]);
                ++spoiled;
            }
        }
        assertGt(spoiled, 0, "there was something left to spoil");
        // And the key this sale put aside for the funded stage, which a stranger could reach
        // too: KeyBound named it publicly the moment the challenge was sold.
        CoreSimulatorLib.forceAccountActivation(p.reservedKey());

        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        // The pass goes through with the registry empty: it does not ask for a key any more.
        ch.graduate(SALT);
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Passed), "the pass is recorded");
        assertGt(ch.payoutOwed(), 0, "and the trader's share of the challenge is owed to them");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey), "the pool waits for a key");
        assertEq(p.fundedTrader(), trader, "for this trader and nobody else");

        // The pool holds its capital while it waits, so the investor cannot take it back out
        // from under someone who passed.
        vm.prank(investor);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.PassedAwaitingKey));
        p.withdrawOnCore(1e8);

        // Opening the stage is what needs a key, and it can simply be tried again.
        vm.expectRevert(KeyRegistry.NoFreeKey.selector);
        p.openFundedStage();

        // One key published by the operator and the trader gets what they earned. Nobody had to
        // be quick about it, and no stranger could decide the outcome.
        address[] memory more = new address[](1);
        more[0] = makeAddr("enclave-key-late");
        vm.prank(operator);
        registry.publish(more);
        p.openFundedStage();

        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.agentKey(), more[0], "funded on the key that was published");
        assertTrue(registry.isBound(more[0], address(p), trader));

        // What this does NOT claim: draining the registry still stops NEW sales. Say so here so
        // nobody reads the test as proof the free list cannot be emptied.
        Pool other = _readyPool();
        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(other), 25e6);
        vm.expectRevert(KeyRegistry.NoFreeKey.selector);
        other.buyChallenge();
        vm.stopPrank();
    }
    /// The sale takes two keys, and one of them is only needed if the trader passes. When
    /// nobody does, the pool gives it back -- otherwise the pool would still be holding it at
    /// the next sale, and a pool may hold only one.
    function test_aChallengeNobodyPassesGivesTheReservedKeyBack() public {
        Pool p = _readyPool();
        uint256 free0 = registry.freeCount();
        ChallengeAccount ch = _started(p);
        assertEq(registry.freeCount(), free0 - 2, "the challenge's own key, and one held for the pass");
        address reserved = p.reservedKey();

        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);
        _settleChallenge(ch);

        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(p.reservedKey(), address(0), "the pool is not still holding it");
        assertEq(uint8(registry.bindingOf(reserved).state), uint8(KeyRegistry.State.Retired));

        // The proof that it was really given back: the pool can sell again.
        _buy(p);
        assertTrue(p.reservedKey() != address(0) && p.reservedKey() != reserved);
    }
    /// Audit A-01. Every settlement step that sees ANY spot balance sends it to the pool and
    /// returns, and Settled is reached only when the balance is zero at the start of a block.
    /// So a stranger who tops the account up by one unit before each step holds the settlement
    /// open for as long as they care to pay the gas -- and with it the pool, because
    /// withdrawOnCore is Idle-only: the investor's capital cannot come out while this lasts.
    /// One unit is 1e-8 USDC, and after the first transfer the account exists, so there is not
    /// even an activation fee to pay.
    function test_settle_isNotHeldOpenByAStrangersDust() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);

        for (uint256 i = 0; i < 12 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            uint64 held = CoreOps.spotUsdc(address(ch));
            CoreSimulatorLib.forceSpotBalance(address(ch), 0, held + 1); // the stranger's dust
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
        }

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled),
            "a stranger's dust must not hold the settlement open");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle), "and the pool must come back to Idle");
    }
    /// The other half of that rule, and the one that keeps it honest: while the account's OWN
    /// return has not landed yet, finishing would leave real capital sitting in a settled
    /// account. Only money that turns up AFTER our send landed is swept and ignored.
    function test_settle_stillWaitsWhileItsOwnReturnHasNotLanded() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);

        for (uint256 i = 0; i < 8 && ch.returnSpotBefore() == 0; ++i) {
            ch.settle(new Cancel[](0), none);
            if (ch.returnSpotBefore() == 0) CoreSimulatorLib.nextBlock();
        }
        assertTrue(ch.returnSpotBefore() != 0, "the capital was sent back to the pool");
        assertGt(ch.returnSpotBefore(), 0);

        // Same block, so that send is still queued and the balance reads untouched.
        ch.settle(new Cancel[](0), none);
        assertTrue(ch.status() != ChallengeAccount.Status.Settled,
            "must not finish while its own capital is still sitting here");
    }
    /// Audit A-01, the pool's door, now closed. A stranger cannot reach the pool's spot
    /// balance -- that money is the pool's and it keeps it -- but they could push USDC into its
    /// perp account, which the closing step has to move across before the pool may return to
    /// Idle. The pool's capital only leaves in Idle (withdrawOnCore), so one unit a block held
    /// the investor's money for as long as the stranger cared to pay the gas.
    function test_settleFunded_isNotHeldOpenByAStrangersPerpDust() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        ch;
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));

        for (uint256 i = 0; i < 12 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.forcePerpBalance(address(p), 1); // the stranger's dust
            p.settleFunded(c, a);
            CoreSimulatorLib.nextBlock();
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle),
            "a stranger's perp dust must not hold the pool in Closing");
        assertEq(p.fundedDrainBlock(), 0, "and the drain block is cleared for the next cycle");
    }
    /// The other side of that rule. The pool's OWN closing proceeds need a block to reach spot,
    /// and finishing before they land would leave them on the perp account -- where nothing can
    /// move them, because Closing is the only stage that drains. So the first step still waits.
    function test_settleFunded_stillWaitsForItsOwnProceedsToReachSpot() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        ch;
        (Cancel[] memory c, uint32[] memory a) = _none();
        // Let the funded capital actually reach the perp side first: graduate only queues that
        // transfer, so without this the stage is stopped with nothing there to drain.
        CoreSimulatorLib.nextBlock();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();

        // Step until the proceeds start moving across. Nobody donates anything here, so the
        // only thing on the perp side is the pool's own money.
        for (uint256 i = 0; i < 8 && p.fundedDrainBlock() == 0; ++i) {
            p.settleFunded(c, a);
            if (p.fundedDrainBlock() == 0) CoreSimulatorLib.nextBlock();
        }
        assertTrue(p.fundedDrainBlock() != 0, "a step sent the perp side across and noted the block");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing),
            "and it did not finish while its own capital was still on the perp side");
    }
    /// Audit finding A-11: a regression I put into the pool's closing step and did not see.
    /// Closing the perp door, I replaced "wait for the perp side to reach spot" with a one-time
    /// FLAG. A flag records that something happened; it does not record WHEN. Precompiles answer
    /// with the start of the block, so a second settleFunded in the same block reads the same
    /// stale numbers -- and with the flag set it walked past the wait and paid the funded trader
    /// their share out of a balance that predated the drain. Once, because the payout marks
    /// itself done. In the audit's run: 9.232 USDC owed, 1e-8 sent.
    ///
    /// The scenario is theirs, unchanged; only the assertions differ, because theirs pinned the
    /// bug and these pin the fix. It lives here rather than in its own file: inheriting the
    /// harness would re-run every test in it a second time under another contract name, and a
    /// suite whose count means two different things is how numbers start drifting.
    function test_regression_twoStepsInOneBlock_payTheFundedShareFromAStaleSpot() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        CoreSimulatorLib.forceSpotBalance(griefer, 0, 1e8);

        // A pool holding exactly what one sale needs: after the pass, all of it is on perp.
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, p.capitalNeeded());
        p.prepareAccount();
        _passed(p); // the challenge's own return is left unsettled on purpose
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(p)), 0, "everything is on perp or in the challenge");

        // The funded trader makes a profit and stops the stage, no breach.
        _trade(address(p), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 810000);
        _trade(address(p), BTC, false, 0.005e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);

        // One unit lands on the pool's spot (anyone can send it; the investor has the motive).
        hyperCore.executeSpotSend(griefer, CoreState.SpotSendAction({destination: address(p), token: 0, _wei: 1}));
        CoreSimulatorLib.nextBlock();

        new TwoStepsInOneBlock().run(p);
        CoreSimulatorLib.nextBlock();

        assertGt(p.fundedPayoutOwed(), 1e8, "a real share is owed");
        assertFalse(p.fundedPayoutDone(), "two steps in one block must not spend the one payout");
        assertEq(p.fundedPayoutSent(), 0, "and nothing was paid out of the stale spot");

        // The next block has the drained capital on spot, so the share is paid in full.
        for (uint256 i = 0; i < 8 && !p.fundedPayoutDone(); ++i) {
            p.settleFunded(c, a);
            CoreSimulatorLib.nextBlock();
        }
        assertTrue(p.fundedPayoutDone(), "the payout happens on a later block");
        assertEq(p.fundedPayoutSent(), p.fundedPayoutOwed(), "and it is the whole share");
    }
/// Audit A-11, the half the share test cannot reach. When no share is owed the payout branch
    /// is skipped whole, and the only thing left between the drain and Stage.Idle is the block
    /// number: every read here is the start of the block, so "nothing is held on perp" is true of
    /// a moment before this very step sent the capital across. Ending Closing there ends the one
    /// stage that can drain, while the money is still on its way to spot.
    function test_regression_twoStepsInOneBlock_endTheStageBeforeTheCapitalLands() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, p.capitalNeeded());
        p.prepareAccount();
        _passed(p); // the challenge's own return is left unsettled, as in the share test
        CoreSimulatorLib.nextBlock();

        // The stage ends a little below where it started, so nothing is owed and the payout
        // branch -- which is what catches two steps in the share test -- is never entered.
        _trade(address(p), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 760000);
        _trade(address(p), BTC, false, 0.005e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();

        new TwoStepsInOneBlock().run(p);

        assertEq(p.fundedPayoutOwed(), 0, "the stage ended below its start, so no share is owed");
        assertEq(
            uint8(p.stage()), uint8(Pool.Stage.Closing),
            "two steps in one block must not finish the stage before the capital has landed"
        );

        CoreSimulatorLib.nextBlock();
        for (uint256 i = 0; i < 8 && p.stage() != Pool.Stage.Idle; ++i) {
            p.settleFunded(c, a);
            CoreSimulatorLib.nextBlock();
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle), "and it does finish, on a later block");
    }

        /// Audit's recheck of the first A-01 fix: the amount comparison alone could still be held
    /// open. When the account has nothing of its own to hand back, the first "return" IS the
    /// stranger's unit, and a unit of the same size after it is never "smaller than last time".
    /// The window is what ends it, and the stranger cannot restart the window.
    function test_settle_spotDust_whenTheOwnReturnIsTiny_noLongerHolds() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _buy(p);
        vm.warp(block.timestamp + 1 hours + 1);
        ch.abort();
        uint32[] memory none = new uint32[](0);

        // The same unit before every step: under the old rule this ran for ever.
        for (uint256 i = 0; i < 20 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            uint64 held = CoreOps.spotUsdc(address(ch));
            CoreSimulatorLib.forceSpotBalance(address(ch), 0, held + 1);
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
            vm.warp(block.timestamp + 60);
        }
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled),
            "the window ends it even when no amount comparison can");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }
    /// The challenge's other door: a stranger pushing USDC onto its PERP account, which the
    /// step has to move across before it can finish. Same window, same reason.
    function test_settle_challengePerpDust_noLongerHolds() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);

        for (uint256 i = 0; i < 20 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            CoreSimulatorLib.forcePerpBalance(address(ch), 1);
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
            vm.warp(block.timestamp + 60);
        }
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled),
            "perp dust must not hold the challenge open either");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
    }
    /// Finishing on time is only safe because nothing is stranded by it. Whatever turns up on
    /// a settled account -- our own send that did not land, a late fill, someone's donation --
    /// goes home when anyone calls sweep().
    function test_sweep_sendsWhatArrivesAfterTheEndBackToThePool() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        uint32[] memory none = new uint32[](0);
        vm.prank(trader);
        ch.forfeit(new Cancel[](0), none, SALT);
        _settleChallenge(ch);

        // A live challenge refuses: sweep is only for accounts that are finished with.
        ChallengeAccount fresh = _buy(p);
        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.BadStatus.selector, ChallengeAccount.Status.Created));
        fresh.sweep();
        // Its capital leaves the pool on the next block; measure after that, not before.
        CoreSimulatorLib.nextBlock();

        uint64 poolBefore = _spot(address(p));
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 5e8); // 5 USDC turns up afterwards
        ch.sweep();
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), 0, "the settled account is empty again");
        assertEq(_spot(address(p)), poolBefore + 5e8, "and the pool has it");
    }
    /// Holding a pool's capital for a trader who passed is right, and holding it for ever is
    /// not: if no key is ever published the investor would never get their money back, which is
    /// a worse hole than the one the wait closes. After the window anyone may release the pool.
    /// The trader keeps the pass and the challenge share; the event records that THIS POOL never
    /// funded them, which is a mark on the pool.
    function test_abandonFundedStage_onlyAfterTheWindow_andThenAnyoneMay() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        for (uint256 i = 0; i < keys.length; ++i) {
            if (registry.bindingOf(keys[i]).state == KeyRegistry.State.Free) {
                CoreSimulatorLib.forceAccountActivation(keys[i]);
            }
        }
        address reserved = p.reservedKey();
        CoreSimulatorLib.forceAccountActivation(reserved);

        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey));

        vm.prank(stranger);
        vm.expectRevert(Pool.TooEarly.selector);
        p.abandonFundedStage();

        vm.warp(block.timestamp + p.AWAIT_KEY_WINDOW() + 1);

        // The window alone is not enough, and this is the half the audit asked for. While the
        // registry still lists keys, the pool is NOT released: a week of nobody calling
        // openFundedStage must not cost a trader their stage when a key was there to be had.
        assertGt(registry.freeCount(), 0, "spoiled keys are still listed until somebody sweeps");
        vm.prank(stranger);
        vm.expectRevert(Pool.KeyAvailable.selector);
        p.abandonFundedStage();

        // Somebody says so on chain: purgeSpoiled stops at the first key that is still good, so
        // reaching zero is proof and not an assumption.
        registry.purgeSpoiled(keys.length);
        assertEq(registry.freeCount(), 0, "nothing left that could have been used");

        vm.prank(stranger); // anyone, not just the owner whose capital it is
        p.abandonFundedStage();

        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle), "the pool is released");
        assertEq(p.fundedTrader(), address(0));
        assertEq(p.reservedKey(), address(0), "and the key it was holding is given back");
        assertEq(uint8(registry.bindingOf(reserved).state), uint8(KeyRegistry.State.Retired));
        assertGt(ch.payoutOwed(), 0, "the trader still earned the challenge share");
    }
    /// When the stranger spoils only the reserved key, opening the stage takes a live one
    /// instead. HyperCore accepts an address that already has an account as an agent by doing
    /// NOTHING and saying nothing (spike question 8), so a stage opened on a spoiled key would
    /// look funded and be unable to trade -- which is worse than any refusal.
/// Audit A-02, the other side of the swap below. When the reserved key is still live the funded
    /// stage opens on exactly that key and takes no second one. Nothing else says so: the swap test
    /// only names the key that is NOT used when the reserved one is spoiled, and a pool that
    /// quietly assigned a fresh key every time would pass it while leaving the reserved key bound
    /// to itself for good -- a registry that empties one key per pool with nobody able to say
    /// where they went.
    function test_theFundedStageOpensOnTheKeyReservedAtTheSale() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        address reserved = p.reservedKey();
        uint256 freeBefore = registry.freeCount();
        assertTrue(reserved != address(0), "the sale reserved one");

        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        p.openFundedStage();

        assertEq(p.agentKey(), reserved, "the stage opened on the key reserved at the sale");
        assertEq(p.reservedKey(), address(0), "and the pool is not still holding it");
        assertEq(registry.freeCount(), freeBefore, "no second key was taken");
        assertTrue(registry.isBound(reserved, address(p), trader));
    }

        function test_aSpoiledReservedKeyIsSwappedForALiveOne() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        address reserved = p.reservedKey();
        CoreSimulatorLib.forceAccountActivation(reserved);

        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        p.openFundedStage();

        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertTrue(p.agentKey() != reserved, "not the spoiled one");
        assertFalse(CoreOps.exists(p.agentKey()), "the stage opened on a key with no account");
        assertEq(uint8(registry.bindingOf(reserved).state), uint8(KeyRegistry.State.Retired));
    }
    /// Audit A-04. The trader's share is measured against spot, and the step used to wait only
    /// for `withdrawable` to read zero -- which it does both when the perp side is empty and when
    /// an order is sitting on the money. Paying on that reading hands the trader whatever reached
    /// spot in time and marks the share paid for good; the difference stays with the pool. The
    /// trader's own orders are named by the keeper, so waiting for them is right.
    function test_settle_doesNotPayTheShareWhileAnOrderHoldsMargin() public {
        // A trader who already has a HyperCore account, so the activation fee does not come out
        // of the share and the only thing this test measures is A-04.
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        p.openFundedStage();
        assertGt(ch.payoutOwed(), 0, "a share is owed for the challenge");

        uint32[] memory none = new uint32[](0);
        _mockHeldMargin(address(ch), 5e6); // an order resting on 5 USDC of the account's money
        for (uint256 i = 0; i < 6; ++i) {
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
        }
        assertFalse(ch.payoutDone(), "the share is not paid while an order holds the money");
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Passed), "and it has not finished");

        vm.clearMockedCalls(); // the keeper named the order and it is gone
        _settleChallenge(ch);
        assertTrue(ch.payoutDone());
        assertEq(ch.payoutSent(), ch.payoutOwed(), "paid in full, not out of whatever had landed");
    }
    /// Audit A-04 on the funded stage, the half found later. The same reading, the same loss,
    /// and worse odds: a pool that funded a trader out of everything it had holds almost nothing
    /// on spot, so "whatever reached spot" is a much smaller number than the share.
    function test_settleFunded_doesNotPayTheShareWhileAnOrderHoldsMargin() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        ch;
        CoreSimulatorLib.nextBlock();
        _trade(address(p), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 810000);
        _trade(address(p), BTC, false, 0.005e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();

        // Take the result, then put an order back on the money before the share is paid.
        p.settleFunded(c, a);
        CoreSimulatorLib.nextBlock();
        uint64 owed = p.fundedPayoutOwed();
        assertGt(owed, 0, "a share is owed for the funded stage");
        uint64 traderBefore = _spot(trader);
        _mockHeldMargin(address(p), 5e6);
        for (uint256 i = 0; i < 6; ++i) {
            p.settleFunded(c, a);
            CoreSimulatorLib.nextBlock();
        }
        assertFalse(p.fundedPayoutDone(), "not paid while an order holds the money");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing), "and the stage has not closed");

        vm.clearMockedCalls();
        for (uint256 i = 0; i < 8 && p.stage() != Pool.Stage.Idle; ++i) {
            p.settleFunded(c, a);
            CoreSimulatorLib.nextBlock();
        }
        // Reaching Idle wipes every funded* field, so what the trader actually received is the
        // only thing left to measure -- and the only thing that ever mattered.
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle), "the stage closed once the order was gone");
        assertEq(_spot(trader) - traderBefore, owed, "the trader got the whole share, not part of it");
    }
    /// The audit's condition on abandonFundedStage, and the case it named: a week of silence
    /// must not cost a trader their stage when the key was sitting right there. Nobody calls
    /// openFundedStage, the window runs out, and the pool still refuses to let go -- because a
    /// live key means the answer is to open the stage, not to give up on it.
    function test_abandonFundedStage_isRefusedWhileAKeyIsThereToBeHad() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey));
        address reserved = p.reservedKey();
        assertFalse(CoreOps.exists(reserved), "the reserved key is still good");

        // Empty the free list so that the ONLY thing standing between this pool and release is
        // the key it is already holding. Without that the count check alone would refuse and
        // this test would not be about the reserved key at all.
        for (uint256 i = 0; i < keys.length; ++i) {
            if (registry.bindingOf(keys[i]).state == KeyRegistry.State.Free) {
                CoreSimulatorLib.forceAccountActivation(keys[i]);
            }
        }
        registry.purgeSpoiled(keys.length);
        assertEq(registry.freeCount(), 0, "nothing free in the registry");

        vm.warp(block.timestamp + p.AWAIT_KEY_WINDOW() + 1);
        vm.prank(stranger);
        vm.expectRevert(Pool.KeyAvailable.selector);
        p.abandonFundedStage();

        // What should happen instead, and anyone can make it happen.
        vm.prank(stranger);
        p.openFundedStage();
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.agentKey(), reserved, "on the key that was waiting the whole time");
    }
    /// Audit A-05, and it is a limit of the design rather than a bug to fix: the target is
    /// measured from the account's perp equity, and that rises for any USDC sent in. HyperCore
    /// gives no way to tell a deposit from a realised gain, so a pass says "the account reached
    /// the target", not "this trader can trade". docs/DESIGN.md says so; this makes it checkable.
    function test_theTargetCanBeReachedByDepositing_notOnlyByTrading() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);

        // Not one order: the money simply arrives.
        CoreSimulatorLib.forcePerpBalance(address(ch), 108e6);
        ch.graduate(SALT);

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Passed), "passed without trading");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey), "and the pool is ready to fund them");
        assertGt(ch.payoutOwed(), 0, "with a share of the 'profit' owed back to the trader");
    }

    /// Audit A-04, the half left open after the first fix. Waiting for the margin to be let go
    /// is not enough: it is let go DURING a block, and the money it was holding reaches spot only
    /// after that block, while `spot` read here is the start of it. So the step that first sees
    /// "nothing held" pays the share out of a balance the released money has not arrived in --
    /// once, because the payout marks itself done. The same start-of-block blindness as A-11, one
    /// line further along.
    function test_settle_doesNotPayTheShareInTheBlockTheMarginIsReleased() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        p.openFundedStage();
        uint64 owed = ch.payoutOwed();
        assertGt(owed, 1e8, "the share is bigger than the crumb below");

        // A little on spot -- enough that the old "spot == 0" guard does not hide the hole -- and
        // the rest of the money held by a resting order.
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 1e8);
        uint32[] memory none = new uint32[](0);
        // Held margin means equity ABOVE what can be withdrawn -- money that is there and cannot
        // be taken out. Mocking it below the real withdrawable, which is what I did first, says
        // the opposite and the test then passes for no reason at all.
        _mockHeldMargin(address(ch), int64(CoreOps.withdrawable(address(ch))) + 5e6);
        ch.settle(new Cancel[](0), none);
        assertFalse(ch.payoutDone(), "nothing paid while the order holds the money");

        // Let the return window run out. Until it does, the door guard happens to protect the
        // payout as a side effect -- it waits while anything is crossing. Past the window it stops
        // doing that, by design, so dust cannot hold a settlement open for ever. That is where the
        // payout is left uncovered, and a resting order holding margin for five minutes is not an
        // unusual thing.
        vm.warp(block.timestamp + ch.RETURN_WAIT() + 1);
        ch.settle(new Cancel[](0), none);
        assertFalse(ch.payoutDone(), "still nothing while the order holds it");

        // The order is named and the margin comes free -- and the step runs in the SAME block,
        // which is the case this test exists for.
        vm.clearMockedCalls();
        ch.settle(new Cancel[](0), none);
        assertFalse(ch.payoutDone(), "and not in the block the margin was released either");
        assertEq(ch.payoutSent(), 0, "a crumb of spot is not the trader's share");

        _settleChallenge(ch);
        assertEq(ch.payoutSent(), owed, "the whole share, once the money had somewhere to be");
    }

    /// The same hole on the funded side, where it is likelier: a pool that funded a trader out of
    /// what it had holds little on spot, so "whatever reached spot" is far short of the share.
    function test_settleFunded_doesNotPayTheShareInTheBlockTheMarginIsReleased() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        ch;
        CoreSimulatorLib.nextBlock();
        _trade(address(p), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 810000);
        _trade(address(p), BTC, false, 0.005e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();

        p.settleFunded(c, a); // takes the result and starts the drain
        CoreSimulatorLib.nextBlock();
        uint64 owed = p.fundedPayoutOwed();
        assertGt(owed, 1e8, "a share worth more than the crumb below");

        // Little on spot, the rest held by a resting order. Held margin is equity ABOVE what can
        // be withdrawn, so the mock has to sit above the real figure.
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1e8);
        // Real money on the perp side: this is what the order is holding and what comes free when
        // it is named. Mocking alone changes a reading, not a balance, so without this there is
        // nothing to release and the step is honestly paying out a genuine shortfall.
        CoreSimulatorLib.forcePerpBalance(address(p), 5e6);
        _mockHeldMargin(address(p), 10e6); // above the 5 that can be withdrawn: held
        p.settleFunded(c, a);
        assertFalse(p.fundedPayoutDone(), "nothing paid while the order holds the money");

        vm.clearMockedCalls(); // the order is named, the margin comes free, same block
        p.settleFunded(c, a);
        assertFalse(p.fundedPayoutDone(), "and not in the block it was released");
        assertEq(p.fundedPayoutSent(), 0, "a crumb of spot is not the trader's share");
    }

    /// Audit A-08. buyChallenge checks the pool's balance through a precompile, which answers with
    /// the START of the block, so a withdrawal earlier in the same block is invisible to it: the
    /// sale went through, the transfer to the fresh challenge quietly did not, and the buyer was
    /// out the platform's fee and an hour's wait for a refund. It refuses now instead.
    function test_buyChallenge_refusesInTheBlockTheOwnerWithdrew() public {
        Pool p = _readyPool();
        vm.prank(investor);
        p.withdrawOnCore(1e8);
        assertEq(p.withdrewAtBlock(), block.number, "the withdrawal noted its block");

        deal(address(usdc), trader, 25e6);
        vm.startPrank(trader);
        usdc.approve(address(p), 25e6);
        vm.expectRevert(Pool.WithdrawnThisBlock.selector);
        p.buyChallenge();
        vm.stopPrank();

        // The next block sees the balance it is actually checking, and the sale goes through.
        CoreSimulatorLib.nextBlock();
        ChallengeAccount ch = _buy(p);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), uint64(_terms().capital) * Units.SPOT_PER_PERP,
            "and the challenge got its capital");
    }

    /// Audit A-12, a hole my own A-04 fix opened. Waiting while the share is short only ends by
    /// itself if the dust closes the gap, and after a real loss it never will: the account has
    /// less than the share and a unit a step keeps something crossing for ever. RETURN_WAIT sits
    /// upstream of that line and PAYOUT_WAIT never starts, because no payout was made. So the
    /// wait has its own clock, and the settlement finishes with whatever is actually there.
    function test_settle_shortShareWithDustEveryStep_stillFinishes() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        p.openFundedStage();
        uint64 owed = ch.payoutOwed();

        // The account loses almost everything after the pass -- an order left resting at the pass
        // opened a position and it went against them -- so there is less here than the share.
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 2e8);
        assertLt(2e8, owed, "less on the account than the trader is owed");

        uint32[] memory none = new uint32[](0);
        for (uint256 i = 0; i < 40 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            CoreSimulatorLib.forcePerpBalance(address(ch), 1); // a unit before every step
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
            vm.warp(block.timestamp + 60);
        }
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled),
            "a stranger cannot hold a short settlement open for ever");
        assertGt(ch.payoutSent(), 0, "and the trader got what there was");
        // The pool is Funded here, because this trader passed and was funded -- what the
        // settlement releases is the challenge slot, which is what a stuck one holds.
        assertEq(p.challenge(), address(0), "the pool is not held by a challenge any more");
    }

    /// The clock that bounds the short-share wait resets while margin is held, and this is why:
    /// a release can come in pieces. The keeper names at most 32 resting orders a pass, so an
    /// account with more than that lets its margin go over several passes. Without the reset the
    /// clock started on the first piece would run out while the rest was still held, and the step
    /// would pay from a stale balance the moment the last piece came free -- which is the A-04
    /// bug again, arriving by the back door. A stranger cannot hold margin on somebody else's
    /// account, so the reset is not theirs to lean on.
    function test_settle_theShortShareClockRestartsWhenMarginIsHeldAgain() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), BTC, false, 0.005e8);
        ch.graduate(SALT);
        p.openFundedStage();
        uint32[] memory none = new uint32[](0);

        // Short, with money crossing: the clock starts.
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 1e8);
        CoreSimulatorLib.forcePerpBalance(address(ch), 1e5);
        // The door guard from A-01 is upstream of the payout and waits on its own window first,
        // so let that run out; past it the payout's own wait is what decides.
        ch.settle(new Cancel[](0), none);
        vm.warp(block.timestamp + ch.RETURN_WAIT() + 1);
        CoreSimulatorLib.nextBlock();
        CoreSimulatorLib.forcePerpBalance(address(ch), 1e5);
        ch.settle(new Cancel[](0), none);
        assertTrue(ch.payoutShortAt() != 0, "the wait noted when it started");

        // Another piece of the release is still held, and it takes longer than the clock.
        _mockHeldMargin(address(ch), int64(CoreOps.withdrawable(address(ch))) + 5e6);
        ch.settle(new Cancel[](0), none);
        assertEq(ch.payoutShortAt(), 0, "held again, so the clock is back to nothing");
        vm.warp(block.timestamp + ch.PAYOUT_WAIT() + 1);
        CoreSimulatorLib.nextBlock();

        // The last piece comes free, and it is crossing to spot as it does. The old clock must
        // not be what decides whether the trader is paid before it lands.
        vm.clearMockedCalls();
        CoreSimulatorLib.forcePerpBalance(address(ch), 1e5);
        ch.settle(new Cancel[](0), none);
        assertFalse(ch.payoutDone(), "not paid out of a balance the release has not reached");
    }

    function test_graduate_needsTargetAndFlat() public {
        Pool p = _readyPool();
        ChallengeAccount ch = _started(p);
        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.TargetNotMet.selector, int64(100e6), int256(108e6)));
        ch.graduate(SALT);

        _trade(address(ch), BTC, true, 0.005e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        vm.expectRevert(ChallengeAccount.NotFlat.selector);
        ch.graduate(SALT);
    }
    function test_graduate_fundsTraderWithANewKey_andPaysTheShare() public {
        CoreSimulatorLib.forceAccountActivation(trader); // a trader who already uses HyperCore
        Pool p = _readyPool();
        vm.recordLogs();
        (ChallengeAccount ch, address oldKey) = _passed(p);
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());

        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Passed));
        assertEq(uint8(registry.bindingOf(oldKey).state), uint8(KeyRegistry.State.Retired));

        address newKey = p.agentKey();
        assertTrue(newKey != address(0) && newKey != oldKey, "a new key, never the old one");
        assertTrue(registry.isBound(newKey, address(p), trader));
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.fundedTrader(), trader);
        assertEq(p.fundedStart(), int64(200e6));

        // The pool approved the new key and moved the funded capital to perp.
        bool sawAgent;
        bool sawPerp;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (from[i] != address(p)) continue;
            if (kind[i] == 9 && keccak256(args[i]) == keccak256(abi.encode(newKey, ""))) sawAgent = true;
            if (kind[i] == 7 && keccak256(args[i]) == keccak256(abi.encode(uint64(200e6), true))) sawPerp = true;
        }
        assertTrue(sawAgent && sawPerp);

        // Profit 0.005 * (78692.0 - 76400.0) = 11.46 USDC; 50% to the trader, 1e8 scale.
        assertEq(ch.payoutOwed(), 573000000);
        CoreSimulatorLib.nextBlock();
        assertEq(_equity(address(p)), int64(200e6));

        uint64 poolSpotBefore = _spot(address(p));
        _settleChallenge(ch);
        assertEq(_spot(trader), 573000000, "trader paid on HyperCore");
        assertEq(ch.payoutSent(), ch.payoutOwed());
        assertGt(_spot(address(p)), poolSpotBefore, "the rest went back to the pool");
        assertEq(p.challenge(), address(0));
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded), "the funded stage goes on");
    }
    /// A trader with no HyperCore account yet receives the share less the 1 USDC that creating
    /// the account costs; the challenge spends exactly the share.
    function test_payout_toATraderWithoutAnAccount_coversTheFeeFromTheShare() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        assertFalse(PrecompileLib.coreUserExists(trader));
        uint64 owed = ch.payoutOwed();
        int64 left = _equity(address(ch));
        uint64 poolSpotBefore = _spot(address(p));

        _settleChallenge(ch);
        assertEq(ch.payoutSent(), owed - Units.NEW_ACCOUNT_FEE);
        assertEq(_spot(trader), owed - Units.NEW_ACCOUNT_FEE, "share less the account fee");
        assertEq(_spot(address(p)), poolSpotBefore + uint64(left) * 100 - owed, "the pool gets the rest");
    }
    /// A share that the account fee would eat whole isn't sent; the challenge settles and
    /// everything goes back to the pool.
    function test_payout_smallerThanTheAccountFee_isNotSent() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 500; // 5% of 11.46 USDC: 0.573
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        assertEq(ch.payoutOwed(), 57300000);
        int64 left = _equity(address(ch));
        uint64 poolSpotBefore = _spot(address(p));

        _settleChallenge(ch);
        assertTrue(ch.payoutDone());
        assertEq(ch.payoutSent(), 0);
        assertFalse(PrecompileLib.coreUserExists(trader), "nothing was sent to the trader");
        assertEq(_spot(address(p)), poolSpotBefore + uint64(left) * 100, "all of it back in the pool");
    }
    function test_fundedBreach_closesAndReturnsToIdle() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        address fundedKey = p.agentKey();

        _trade(address(p), BTC, true, 0.01e8); // 787 USDC notional on 200: 3.9x
        CoreSimulatorLib.setMarkPx(BTC, 763310); // -3%
        _mockMargin(address(p), 176.4e6, 763e6); // Hyperliquid's view: -23.6 on 200
        assertEq(uint8(p.violation(new uint32[](0))), uint8(Breach.Drawdown));

        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(stranger);
        p.breach(c, a, SALT);
        vm.clearMockedCalls();
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));
        assertEq(uint8(p.fundedEndReason()), uint8(Breach.Drawdown));
        assertEq(p.fundedPayoutOwed(), 0, "no share after a breach");
        assertEq(uint8(registry.bindingOf(fundedKey).state), uint8(KeyRegistry.State.Retired));

        for (uint256 i = 0; i < 6 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            p.settleFunded(c, a);
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(PrecompileLib.position(address(p), BTC).szi, 0);
        assertEq(_equity(address(p)), 0, "perp balance back on spot");

        // The pool can be sold again, with a fresh key.
        ChallengeAccount next = _buy(p);
        assertTrue(next.agentKey() != fundedKey);
    }
    function test_stopFunded_byInvestor_paysTheShare() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);

        _trade(address(p), BTC, true, 0.01e8);
        CoreSimulatorLib.setMarkPx(BTC, 810520); // +3% from 78692.0: about +23.6
        (Cancel[] memory c, uint32[] memory a) = _none();

        vm.prank(stranger);
        vm.expectRevert(Pool.NotAllowed.selector);
        p.stopFunded(c, a, SALT);

        uint64 traderSpotBefore = _spot(trader);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        assertEq(p.fundedPayoutOwed(), 0, "not known while the position is open");
        assertFalse(p.fundedResultTaken());

        uint64 owed;
        for (uint256 i = 0; i < 8 && p.stage() != Pool.Stage.Idle; ++i) {
            CoreSimulatorLib.nextBlock();
            p.settleFunded(c, a);
            if (p.fundedResultTaken() && owed == 0) owed = p.fundedPayoutOwed();
        }
        assertGt(owed, 0);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(_spot(trader) - traderSpotBefore, owed);
    }
    /// The share is computed from what closing realized. At the stop the position is marked
    /// at +40; the close fills at +5; the trader gets 80% of 5, and the investor doesn't pay
    /// for the gap.
    function test_fundedShare_comesFromWhatClosingRealized() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        (Cancel[] memory c, uint32[] memory a) = _none();

        _trade(address(p), BTC, true, 0.01e8);
        _mockMargin(address(p), 240e6, 787e6);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        assertEq(p.fundedStopEquity(), 240e6);
        assertEq(p.fundedPayoutOwed(), 0);

        CoreSimulatorLib.nextBlock(); // the reduce-only close executes
        assertEq(PrecompileLib.position(address(p), BTC).szi, 0);
        _mockMargin(address(p), 205e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();

        assertTrue(p.fundedResultTaken());
        assertEq(p.fundedResult(), 205e6);
        assertEq(p.fundedPayoutOwed(), 4e8, "80% of the realized 5 USDC, in 1e8 units");
    }
    /// The two shares are independent, and this is the pair the demo deploys: nothing for
    /// passing the audition, 80% of what the trader makes on the pool's own capital. The
    /// challenge pays zero and no HyperCore account is created for the trader by it; the
    /// funded stage then pays the full 80%.
    function test_shares_areIndependent_zeroOnTheChallenge_eightyOnTheFunded() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 0;
        t.traderShareFundedBps = 8000;
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();

        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        assertEq(ch.payoutOwed(), 0, "the challenge pays nothing at a zero challenge share");
        _settleChallenge(ch);
        assertEq(_spot(trader), 0, "and nothing reached the trader on HyperCore");
        assertFalse(PrecompileLib.coreUserExists(trader));

        (Cancel[] memory c, uint32[] memory a) = _none();
        _trade(address(p), BTC, true, 0.01e8);
        _mockMargin(address(p), 240e6, 787e6);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();
        _mockMargin(address(p), 205e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();
        assertEq(p.fundedPayoutOwed(), 4e8, "80% of the realized 5 USDC, in 1e8 units");
    }
    /// The mirror: the challenge share paid, the funded share zero. Reading one field where
    /// the other is meant would give this pool the same numbers as the one above.
    function test_shares_areIndependent_eightyOnTheChallenge_zeroOnTheFunded() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 8000;
        t.traderShareFundedBps = 0;
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();

        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        // Profit 0.005 * (78692.0 - 76400.0) = 11.46 USDC; 80% of it, 1e8 scale.
        assertEq(ch.payoutOwed(), 916800000, "the challenge pays its own rate");
        _settleChallenge(ch);

        (Cancel[] memory c, uint32[] memory a) = _none();
        _trade(address(p), BTC, true, 0.01e8);
        _mockMargin(address(p), 240e6, 787e6);
        vm.prank(investor);
        p.stopFunded(c, a, SALT);
        CoreSimulatorLib.nextBlock();
        _mockMargin(address(p), 205e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();
        assertEq(p.fundedResult(), 205e6, "the funded stage did realize a profit");
        assertEq(p.fundedPayoutOwed(), 0, "and none of it is owed at a zero funded share");
    }
    /// A position in an asset nobody named still shows in the account's notional. Settlement
    /// waits for it: nothing moves to spot and the result isn't taken.
    function test_settle_waitsWhileAnUnnamedPositionIsOpen() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);

        CoreSimulatorLib.nextBlock();
        _mockMargin(address(p), 200e6, 90e6); // an ETH position nobody named
        vm.recordLogs();
        p.settleFunded(c, a);
        (address[] memory from, uint24[] memory kind,) = _actions(vm.getRecordedLogs());
        for (uint256 i = 0; i < kind.length; ++i) {
            assertFalse(from[i] == address(p) && kind[i] == 7, "nothing may move to spot");
        }
        assertFalse(p.fundedResultTaken());

        _mockMargin(address(p), 200e6, 0);
        p.settleFunded(c, a);
        vm.clearMockedCalls();
        assertTrue(p.fundedResultTaken());
    }
    /// An asset whose size decimals leave no room for a price refuses by name instead of
    /// underflowing.
    function test_close_refusesAnAssetWithTooManySizeDecimals() public {
        uint32 odd = 9;
        hyperCore.registerPerpAssetInfo(
            odd,
            PrecompileLib.PerpAssetInfo({coin: "ODD", marginTableId: 1, szDecimals: 7, maxLeverage: 3, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(odd, 1);
        CloseHarness h = new CloseHarness();
        _mockPosition(address(h), odd, 5);
        uint32[] memory perps = new uint32[](1);
        perps[0] = odd;
        vm.expectRevert(abi.encodeWithSelector(CoreOps.UnsupportedSizeDecimals.selector, odd, uint8(7)));
        h.close(perps);
    }
    /// The pool goes back to idle only after the funded trader's payout has left its balance,
    /// or a new challenge could be sold against money already on its way out.
    function test_poolStaysClosingUntilTheFundedPayoutLands() public {
        Pool p = _readyPool();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        uint64 traderSpotBefore = _spot(trader);

        _trade(address(p), BTC, true, 0.01e8);
        CoreSimulatorLib.setMarkPx(BTC, 810520);
        _trade(address(p), BTC, false, 0.01e8); // profit realized, flat
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(investor);
        p.stopFunded(c, a, SALT);

        p.settleFunded(c, a); // result taken, perp -> spot
        assertGt(p.fundedPayoutOwed(), 0);
        CoreSimulatorLib.nextBlock();
        vm.recordLogs();
        p.settleFunded(c, a); // payout sent
        uint64 sent = p.fundedPayoutSent();
        assertGt(sent, 0);
        p.settleFunded(c, a); // same block: not landed yet
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Closing));
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        uint256 toTrader;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (from[i] != address(p) || kind[i] != 6) continue;
            (address to,,) = abi.decode(args[i], (address, uint64, uint64));
            if (to == trader) ++toTrader;
        }
        assertEq(toTrader, 1, "one payout send");

        CoreSimulatorLib.nextBlock();
        p.settleFunded(c, a);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(_spot(trader) - traderSpotBefore, sent, "the share arrived once");
    }
    /// The funded share goes to a trader who still has no HyperCore account (the challenge
    /// share was too small to send): it arrives less the 1 USDC for creating the account.
    function test_fundedPayout_toATraderWithoutAnAccount_coversTheFee() public {
        Terms memory t = _terms();
        t.traderShareChallengeBps = 500;
        t.traderShareFundedBps = 500;
        vm.prank(investor);
        Pool p = Pool(factory.createPool(_rules(), t));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
        (ChallengeAccount ch,) = _passed(p);
        CoreSimulatorLib.nextBlock();
        _settleChallenge(ch);
        assertFalse(PrecompileLib.coreUserExists(trader));

        _trade(address(p), BTC, true, 0.01e8);
        CoreSimulatorLib.setMarkPx(BTC, 810520);
        _trade(address(p), BTC, false, 0.01e8);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        p.stopFunded(c, a, SALT);
        for (uint256 i = 0; i < 8 && p.stage() != Pool.Stage.Idle; ++i) {
            p.settleFunded(c, a);
            if (p.fundedPayoutDone() && p.fundedPayoutSent() != 0) {
                assertEq(p.fundedPayoutSent(), p.fundedPayoutOwed() - Units.NEW_ACCOUNT_FEE);
            }
            CoreSimulatorLib.nextBlock();
        }
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        // The mark was 78692.0 after the challenge: 0.01 * (81052 - 78692) = 23.60 USDC realized,
        // 5% of it is 1.18, and the account fee takes 1 of that.
        assertEq(_spot(trader), 18000000);
    }
    function test_access() public {
        Pool p = _readyPool();
        vm.startPrank(stranger);
        vm.expectRevert(Pool.NotChallenge.selector);
        p.onChallengeStarted();
        vm.expectRevert(Pool.NotChallenge.selector);
        p.onChallengePassed(stranger);
        vm.expectRevert(Pool.NotOwner.selector);
        p.withdrawOnCore(1e8);
        vm.expectRevert(PoolFactory.NotPool.selector);
        factory.createChallenge(stranger);
        vm.stopPrank();

        _buy(p);
        vm.prank(investor);
        vm.expectRevert(abi.encodeWithSelector(Pool.BadStage.selector, Pool.Stage.Challenge));
        p.withdrawOnCore(1e8);

        vm.prank(investor);
        p.withdrawEarned();
        assertEq(usdc.balanceOf(investor), 0, "nothing earned before the challenge starts");
    }
}
