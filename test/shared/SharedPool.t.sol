// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreState} from "@hyper-evm-lib/test/simulation/hyper-core/CoreState.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";

import {Rules, Terms, Cancel, Units} from "../../src/Types.sol";
import {KeyRegistry} from "../../src/KeyRegistry.sol";
import {PoolFactory} from "../../src/PoolFactory.sol";
import {Pool} from "../../src/Pool.sol";
import {ChallengeAccount} from "../../src/ChallengeAccount.sol";
import {SharedPool} from "../../src/shared/SharedPool.sol";
import {DepositTicket} from "../../src/shared/DepositTicket.sol";

contract SharedMockUsdc is ERC20 {
    constructor() ERC20("USDC", "USDC") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }
}

/// The shared pool on the hyper-evm-lib simulator, offline. A deposit's arrival on HyperCore is set
/// with forceSpotBalance, which is what a transfer from the depositor's own account ends in; the
/// sweeps and top-ups the contracts send go through the simulator's CoreWriter. Rule breaks are
/// set through the precompile answers, as in PoolFlow.t.sol, because the simulator's account value
/// is not Hyperliquid's once a position is open.
contract SharedPoolTest is Test {
    uint32 constant BTC = 3;
    uint64 constant BTC_MARK = 764000; // 76400.0
    bytes32 constant SALT = keccak256("shared-salt");
    uint64 constant SEED = 10e8;
    uint64 constant MIN = 20e8;
    uint64 constant NEED = 101e8; // capitalNeeded of the seat below: (20 + 80) USDC plus 1 for the challenge account

    HyperCore hyperCore;
    SharedMockUsdc usdc;
    KeyRegistry registry;
    PoolFactory factory;
    SharedPool sp;

    address operator = makeAddr("operator");
    address platform = makeAddr("platform");
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address trader = makeAddr("trader");
    address stranger = makeAddr("stranger");
    address[] keys;

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        CoreSimulatorLib.setRevertOnFailure(true);
        CoreSimulatorLib.setPerpMakerFee(0);
        CoreSimulatorLib.setSpotMakerFee(0);
        hyperCore.registerPerpAssetInfo(
            BTC,
            PrecompileLib.PerpAssetInfo({
                coin: "BTC", marginTableId: 54, szDecimals: 5, maxLeverage: 40, onlyIsolated: false
            })
        );
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);

        SharedMockUsdc impl = new SharedMockUsdc();
        vm.etch(HLConstants.usdc(), address(impl).code);
        usdc = SharedMockUsdc(HLConstants.usdc());

        registry = new KeyRegistry(operator);
        factory = new PoolFactory(registry, address(new Pool()), address(new ChallengeAccount()), operator);
        for (uint256 i = 0; i < 8; ++i) {
            keys.push(makeAddr(string.concat("shared-key-", vm.toString(i))));
        }
        uint32[] memory listed = new uint32[](1);
        listed[0] = BTC;
        vm.startPrank(operator);
        registry.setAccountSource(factory);
        registry.publish(keys);
        factory.setPlatformAssets(listed, true);
        vm.stopPrank();

        sp = new SharedPool(factory, operator, platform, MIN);
    }

    // ── helpers ──────────────────────────────────────────────────────────────────────

    function _rules() internal pure returns (Rules memory r) {
        uint32[] memory assets = new uint32[](1);
        assets[0] = BTC;
        r = Rules({dailyLossBps: 500, maxDrawdownBps: 1000, maxLeverageX100: 500, assets: assets});
    }

    function _terms() internal pure returns (Terms memory) {
        return Terms({
            price: 1e6,
            capital: 20e6,
            targetBps: 800,
            duration: 7 days,
            traderShareChallengeBps: 5000,
            traderShareFundedBps: 8000,
            fundedCapital: 80e6
        });
    }

    function _start(uint64 seed) internal {
        CoreSimulatorLib.forceSpotBalance(address(sp), 0, seed);
        vm.prank(operator);
        sp.start();
        CoreSimulatorLib.nextBlock();
    }

    function _ticket(address who, uint64 amount) internal returns (address t) {
        vm.prank(who);
        t = sp.openTicket();
        // What a transfer from the depositor's own HyperCore account ends in.
        CoreSimulatorLib.forceSpotBalance(t, 0, amount);
    }

    function _list(address a) internal pure returns (address[] memory l) {
        l = new address[](1);
        l[0] = a;
    }

    function _list(address a, address b) internal pure returns (address[] memory l) {
        l = new address[](2);
        l[0] = a;
        l[1] = b;
    }

    function _spot(address a) internal view returns (uint64) {
        return PrecompileLib.spotBalance(a, 0).total;
    }

    function _equity(address a) internal view returns (int64) {
        return PrecompileLib.accountMarginSummary(0, a).accountValue;
    }

    /// Started with the seed, `deposit` from alice recognized and swept into the pool.
    function _funded(uint64 deposit) internal {
        _start(SEED);
        address t = _ticket(alice, deposit);
        sp.settle(_list(t));
        CoreSimulatorLib.nextBlock();
    }

    /// A seat that is on HyperCore, prepared and holding what it needs to sell a challenge.
    function _armedSeat() internal returns (Pool seat) {
        vm.prank(operator);
        seat = Pool(sp.addSeat(_rules(), _terms()));
        sp.armSeat(address(seat));
        CoreSimulatorLib.nextBlock();
        seat.prepareAccount();
        CoreSimulatorLib.nextBlock();
    }

    function _started(Pool seat) internal returns (ChallengeAccount ch) {
        deal(address(usdc), trader, 1e6);
        vm.startPrank(trader);
        usdc.approve(address(seat), 1e6);
        ch = ChallengeAccount(seat.buyChallenge());
        vm.stopPrank();
        CoreSimulatorLib.nextBlock();
        ch.activate();
        CoreSimulatorLib.nextBlock();
    }

    function _trade(address account, bool isBuy, uint64 sz1e8) internal {
        // Simulator quirk (see PoolFlow.t.sol): pin the perp balance before the first order.
        CoreSimulatorLib.forcePerpBalance(account, hyperCore.readPerpBalance(account));
        CoreSimulatorLib.forcePerpLeverage(account, BTC, 10);
        hyperCore.executePerpLimitOrder(
            account,
            CoreState.LimitOrderAction({
                asset: BTC,
                isBuy: isBuy,
                limitPx: isBuy ? type(uint64).max / 2 : 1,
                sz: sz1e8,
                reduceOnly: false,
                encodedTif: 3,
                cloid: 0
            })
        );
    }

    /// Challenge capital 20: 0.001 BTC bought at 76400 and sold at 78692 makes 2.292 USDC, past the
    /// 8% target, flat again.
    function _passed(Pool seat) internal returns (ChallengeAccount ch) {
        ch = _started(seat);
        _trade(address(ch), true, 0.001e8);
        CoreSimulatorLib.setMarkPx(BTC, 786920);
        _trade(address(ch), false, 0.001e8);
        ch.graduate(SALT);
        CoreSimulatorLib.nextBlock();
    }

    function _mockMargin(address account, int64 accountValue, uint64 ntlPos) internal {
        vm.mockCall(
            address(0x080F),
            abi.encode(uint32(0), account),
            abi.encode(PrecompileLib.AccountMarginSummary({
                accountValue: accountValue, marginUsed: ntlPos / 10, ntlPos: ntlPos, rawUsd: accountValue
            }))
        );
    }

    function _assertBlocked(SharedPool.Blocker want, address who) internal {
        (SharedPool.Blocker reason, address account) = sp.blocker();
        assertEq(uint8(reason), uint8(want), "blocker reason");
        assertEq(account, who, "blocker account");
        vm.expectRevert(abi.encodeWithSelector(SharedPool.NotQuiet.selector, want, who));
        sp.settle(new address[](0));
    }

    function _assertQuiet() internal view {
        (SharedPool.Blocker reason,) = sp.blocker();
        assertEq(uint8(reason), uint8(SharedPool.Blocker.None), "nothing should hold the point up");
    }

    // ── starting ─────────────────────────────────────────────────────────────────────

    function test_start_turnsTheSeedIntoThePlatformsShares_once() public {
        vm.prank(operator);
        vm.expectRevert(SharedPool.NotReady.selector);
        sp.start();

        CoreSimulatorLib.forceSpotBalance(address(sp), 0, SEED);
        vm.prank(stranger);
        vm.expectRevert(SharedPool.NotOperator.selector);
        sp.start();

        vm.prank(operator);
        sp.start();
        assertEq(sp.sharesOf(platform), SEED, "the seed belongs to the platform");
        assertEq(sp.sharesOf(operator), 0);
        assertEq(sp.totalShares(), SEED);
        assertEq(sp.seedShares(), SEED);
        assertEq(sp.seedValue(), SEED);

        vm.prank(operator);
        vm.expectRevert(SharedPool.AlreadyStarted.selector);
        sp.start();
    }

    /// A minimum under the dust line would let a deposit be counted and then forgotten in one point.
    function test_minimumDeposit_isAtLeastWhatASweepTakes() public {
        uint64 dustLine = sp.SWEEP_MIN();
        vm.expectRevert(SharedPool.BadDeposit.selector);
        new SharedPool(factory, operator, platform, dustLine - 1);
        new SharedPool(factory, operator, platform, dustLine);
    }

    function test_nothingRunsBeforeTheStart() public {
        vm.expectRevert(SharedPool.NotStarted.selector);
        sp.openTicket();
        vm.expectRevert(SharedPool.NotStarted.selector);
        sp.settle(new address[](0));
        vm.prank(operator);
        vm.expectRevert(SharedPool.NotStarted.selector);
        sp.addSeat(_rules(), _terms());
    }

    // ── the seat plan ────────────────────────────────────────────────────────────────

    function test_addSeat_ownedByThePool_onlyByTheOperator() public {
        _start(SEED);
        vm.prank(stranger);
        vm.expectRevert(SharedPool.NotOperator.selector);
        sp.addSeat(_rules(), _terms());

        vm.prank(operator);
        address seat = sp.addSeat(_rules(), _terms());
        assertEq(Pool(seat).owner(), address(sp), "the shared pool owns its seats");
        assertTrue(sp.isSeat(seat));
        assertTrue(factory.isPool(seat));
        assertEq(sp.planCapital(), NEED);
        (uint64 capital, uint16 targetBps, uint16 shareChallenge, uint16 shareFunded) = sp.seatTerms(seat);
        assertEq(capital, 20e6);
        assertEq(targetBps, 800);
        assertEq(shareChallenge, 5000);
        assertEq(shareFunded, 8000);
    }

    /// 5% of one seat's 101 USDC is 5.05 USDC: a seed of exactly that is enough, a unit less is not.
    function test_addSeat_keepsTheSeedAtFivePercentOfThePlan() public {
        _start(5.05e8);
        vm.prank(operator);
        sp.addSeat(_rules(), _terms());

        SharedPool short = new SharedPool(factory, operator, platform, MIN);
        CoreSimulatorLib.forceSpotBalance(address(short), 0, 5.05e8 - 1);
        vm.startPrank(operator);
        short.start();
        vm.expectRevert(abi.encodeWithSelector(SharedPool.SeedTooSmall.selector, uint256(5.05e8 - 1), uint256(NEED)));
        short.addSeat(_rules(), _terms());
        vm.stopPrank();
    }

    function test_addSeat_refusesASecondSeatTheSeedCannotCover() public {
        _start(SEED); // covers 200 USDC of plan, one seat is 101
        vm.startPrank(operator);
        sp.addSeat(_rules(), _terms());
        vm.expectRevert(abi.encodeWithSelector(SharedPool.SeedTooSmall.selector, uint256(SEED), uint256(2 * NEED)));
        sp.addSeat(_rules(), _terms());
        vm.stopPrank();
    }

    // ── tickets ──────────────────────────────────────────────────────────────────────

    function test_ticket_isKnownBeforehand_andPaysOnlyIntoItsPool() public {
        _start(SEED);
        address predicted = sp.ticketAddress(alice, 0);
        vm.prank(alice);
        address t = sp.openTicket();
        assertEq(t, predicted, "the address can be shown before the ticket exists");
        assertEq(sp.ticketAddress(alice, 1) == t, false);
        (address depositor, SharedPool.TicketState state) = sp.tickets(t);
        assertEq(depositor, alice);
        assertEq(uint8(state), uint8(SharedPool.TicketState.Open));
        assertEq(DepositTicket(t).pool(), address(sp));
        assertEq(DepositTicket(t).depositor(), alice);

        CoreSimulatorLib.forceSpotBalance(t, 0, MIN);
        vm.prank(stranger);
        vm.expectRevert(DepositTicket.NotPool.selector);
        DepositTicket(t).sweep(MIN);

        DepositTicket impl = DepositTicket(sp.ticketImpl());
        vm.expectRevert(DepositTicket.Initialized.selector);
        impl.init(stranger);
        vm.expectRevert(DepositTicket.Initialized.selector);
        DepositTicket(t).init(stranger);
    }

    // ── settlement points: deposits ──────────────────────────────────────────────────

    /// The pool has made 50% (value 15 on 10 shares). Alice's 20 and Bob's 30 come in at 1.5 both,
    /// priced at the value without them, and the price of a share doesn't move.
    function test_settle_pricesDepositsAtTheValueWithoutThem_allAtOnePrice() public {
        _start(SEED);
        address ta = _ticket(alice, 20e8);
        address tb = _ticket(bob, 30e8);
        CoreSimulatorLib.forceSpotBalance(address(sp), 0, 15e8);
        assertEq(sp.value(), 15e8, "open tickets are not the pool's yet");

        sp.settle(_list(ta, tb));
        // One price for both, 1.5 a share, rounded down in the pool's favour.
        assertEq(sp.sharesOf(alice), (uint256(20e8) * SEED) / 15e8);
        assertEq(sp.sharesOf(bob), 20e8, "bob's price is not moved by alice's shares");
        assertEq(sp.value(), 65e8, "15 plus both tickets, still on their addresses");
        assertEq(sp.totalShares(), SEED + sp.sharesOf(alice) + sp.sharesOf(bob));

        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(sp)), 65e8, "both swept into the pool");
        assertEq(sp.value(), 65e8, "a sweep landing changes nothing");
    }

    function test_settle_takesATicketOnlyFromTheMinimum() public {
        _start(SEED);
        address t = _ticket(alice, MIN - 1);
        sp.settle(_list(t));
        assertEq(sp.sharesOf(alice), 0, "below the minimum");
        (, SharedPool.TicketState state) = sp.tickets(t);
        assertEq(uint8(state), uint8(SharedPool.TicketState.Open), "still open, can be topped up");

        CoreSimulatorLib.forceSpotBalance(t, 0, MIN);
        sp.settle(_list(t));
        assertEq(sp.sharesOf(alice), MIN, "at a price of 1");
    }

    function test_ticketIsRecognizedOnce() public {
        _start(SEED);
        address t = _ticket(alice, MIN);
        sp.settle(_list(t));
        uint256 minted = sp.sharesOf(alice);
        // Same block: the sweep hasn't landed, the ticket still shows the deposit.
        sp.settle(_list(t));
        assertEq(sp.sharesOf(alice), minted, "a closed ticket is never counted again");
        assertEq(sp.totalShares(), SEED + minted);
    }

    function test_closedTicket_countsUntilItsMoneyLands_thenIsForgotten() public {
        _start(SEED);
        address t = _ticket(alice, MIN);
        sp.settle(_list(t));
        assertEq(sp.closedTickets().length, 1);
        assertEq(_spot(t), MIN, "the sweep is in flight");
        assertEq(sp.value(), SEED + MIN, "in flight, the deposit is still counted where it is");

        CoreSimulatorLib.nextBlock();
        assertEq(_spot(t), 0);
        assertEq(sp.value(), SEED + MIN);

        vm.expectEmit(true, false, false, false, address(sp));
        emit SharedPool.TicketEmptied(t);
        sp.settle(new address[](0));
        assertEq(sp.closedTickets().length, 0);
    }

    /// Alice sends again to the address after her deposit was counted: it is a gift to every holder.
    function test_moneySentToAClosedTicket_belongsToThePool() public {
        _funded(MIN);
        address t = sp.ticketAddress(alice, 0);
        uint256 aliceShares = sp.sharesOf(alice);
        CoreSimulatorLib.forceSpotBalance(t, 0, 5e8);
        assertEq(sp.value(), SEED + MIN + 5e8);
        sp.settle(new address[](0));
        CoreSimulatorLib.nextBlock();
        assertEq(sp.sharesOf(alice), aliceShares, "no shares for it");
        assertEq(_spot(address(sp)), SEED + MIN + 5e8, "swept into the pool");
    }

    /// The same after the ticket was emptied and forgotten: naming it at a point takes the money in.
    function test_strayMoneyOnAForgottenTicket_isTakenIn() public {
        _funded(MIN);
        address t = sp.ticketAddress(alice, 0);
        sp.settle(new address[](0)); // the emptied ticket is forgotten
        assertEq(sp.closedTickets().length, 0);
        CoreSimulatorLib.forceSpotBalance(t, 0, 5e8);
        assertEq(sp.value(), SEED + MIN, "not counted until someone names it");

        sp.settle(_list(t));
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(sp)), SEED + MIN + 5e8);
        assertEq(sp.sharesOf(alice), MIN, "no shares for it");
    }

    /// Less than a dollar left on a closed ticket is dust: forgotten, not swept, not counted, and
    /// naming the ticket doesn't bring it back.
    function test_dustOnAClosedTicket_isForgotten() public {
        _funded(MIN);
        address t = sp.ticketAddress(alice, 0);
        CoreSimulatorLib.forceSpotBalance(t, 0, 0.5e8); // lands after the sweep, before the next point
        assertEq(sp.value(), SEED + MIN + 0.5e8, "still on the list, still counted");
        sp.settle(new address[](0));
        assertEq(sp.closedTickets().length, 0, "forgotten");
        assertEq(sp.value(), SEED + MIN);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(t), 0.5e8, "not swept");

        sp.settle(_list(t));
        assertEq(sp.closedTickets().length, 0, "dust doesn't put it back");
        CoreSimulatorLib.forceSpotBalance(t, 0, 1e8);
        sp.settle(_list(t));
        assertEq(sp.closedTickets().length, 1, "a dollar does");
    }

    function test_settle_isOpenToAnyone() public {
        _start(SEED);
        address t = _ticket(alice, MIN);
        vm.prank(stranger);
        sp.settle(_list(t));
        assertEq(sp.sharesOf(alice), MIN);
        assertEq(sp.points(), 1);
    }

    // ── seats ────────────────────────────────────────────────────────────────────────

    function test_armSeat_topsUpAFreshSeat_payingItsAccountFeeOnce() public {
        _funded(150e8); // 160 in the pool
        vm.prank(operator);
        address seat = sp.addSeat(_rules(), _terms());
        vm.prank(stranger);
        sp.armSeat(seat);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(seat), NEED, "the seat can sell a challenge");
        assertEq(_spot(address(sp)), 160e8 - NEED - Units.NEW_ACCOUNT_FEE, "the new account cost 1 USDC");
        assertEq(sp.value(), 160e8 - Units.NEW_ACCOUNT_FEE);
    }

    function test_armSeat_refusesWithoutRoomForTheAccountFee() public {
        _funded(91e8); // 101 in the pool: the seat's capital, not the fee for its new account
        vm.prank(operator);
        address seat = sp.addSeat(_rules(), _terms());
        vm.expectRevert(abi.encodeWithSelector(SharedPool.NotEnoughFree.selector, uint64(NEED), NEED + Units.NEW_ACCOUNT_FEE));
        sp.armSeat(seat);
    }

    function test_armSeat_waitsForThePreviousTopUp() public {
        _funded(250e8);
        vm.prank(operator);
        address seat = sp.addSeat(_rules(), _terms());
        sp.armSeat(seat);
        // Not landed yet: the seat still shows nothing, and a second top-up would send twice.
        vm.warp(block.timestamp + sp.ARM_WAIT());
        vm.expectRevert(abi.encodeWithSelector(SharedPool.TooSoon.selector, seat));
        sp.armSeat(seat);
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(seat), NEED);
    }

    function test_armSeat_onlyAnIdleSeat() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        _started(seat);
        vm.warp(block.timestamp + sp.ARM_WAIT() + 1);
        vm.expectRevert(abi.encodeWithSelector(SharedPool.SeatBusy.selector, address(seat)));
        sp.armSeat(address(seat));
    }

    // ── value ────────────────────────────────────────────────────────────────────────

    /// 160 in the pool; arming costs 1 for the seat's account and buying costs the seat 1 for the
    /// challenge's; the price paid counts once the challenge starts, and so does USDC the pool holds
    /// on HyperEVM.
    function test_value_countsSeatsChallengesAndPricesOnBothSides() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        assertEq(sp.value(), 159e8);

        deal(address(usdc), trader, 1e6);
        vm.startPrank(trader);
        usdc.approve(address(seat), 1e6);
        ChallengeAccount ch = ChallengeAccount(seat.buyChallenge());
        vm.stopPrank();
        assertEq(sp.value(), 159e8, "a price that can still be refunded is not the pool's");
        CoreSimulatorLib.nextBlock();
        assertEq(_spot(address(ch)), 20e8);
        assertEq(sp.value(), 158e8, "the challenge's account cost 1");

        ch.activate();
        CoreSimulatorLib.nextBlock();
        assertEq(_equity(address(ch)), int64(20e6), "the challenge capital is on perp");
        assertEq(sp.value(), 159e8, "the price counts once the challenge starts");

        deal(address(usdc), address(sp), 2e6);
        assertEq(sp.value(), 161e8, "USDC on HyperEVM counts too");
    }

    /// A funded seat 10 USDC up: the pool's value rises by the 20% the trader doesn't get.
    function test_value_leavesOutTheTradersShareOfAFundedProfit() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        _passed(seat);
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Funded));
        int64 start_ = seat.fundedStart();
        uint256 flat = sp.value();

        _mockMargin(address(seat), start_ + 10e6, 0);
        assertEq(sp.value(), flat + 2e8);
        _mockMargin(address(seat), start_ - 5e6, 0);
        assertEq(sp.value(), flat - 5e8, "a loss is the pool's whole");
    }

    /// A challenge at or past its target: the trader could graduate now and take half the profit.
    function test_value_leavesOutTheTradersShareOfAChallengeAtItsTarget() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        ChallengeAccount ch = _started(seat);
        uint256 flat = sp.value();

        _mockMargin(address(ch), 21e6, 0); // below the 21.6 target
        assertEq(sp.value(), flat + 1e8);
        _mockMargin(address(ch), 23e6, 0); // past it: 3 up, 1.5 of it the trader's
        assertEq(sp.value(), flat + 1.5e8);
    }

    /// A passed challenge's share is the trader's from the moment it passes: the value doesn't move
    /// when it is paid.
    function test_value_leavesOutAPassedChallengesUnpaidShare() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        _funded(150e8);
        Pool seat = _armedSeat();
        ChallengeAccount ch = _passed(seat);
        assertGt(ch.payoutOwed(), 0);
        uint256 before_ = sp.value();

        uint32[] memory none = new uint32[](0);
        for (uint256 i = 0; i < 8 && ch.status() != ChallengeAccount.Status.Settled; ++i) {
            ch.settle(new Cancel[](0), none);
            CoreSimulatorLib.nextBlock();
        }
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Settled));
        assertEq(_spot(trader), ch.payoutOwed(), "the trader was paid");
        assertEq(sp.value(), before_, "paying what was owed changes nothing");
    }

    // ── settlement points: what holds them up ───────────────────────────────────────

    function test_settle_waitsWhileAChallengeBreaksARule() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        ChallengeAccount ch = _started(seat);
        _assertQuiet();

        _mockMargin(address(ch), 17e6, 0); // 20 less 15%: past the 10% drawdown
        _assertBlocked(SharedPool.Blocker.RuleBroken, address(ch));

        vm.clearMockedCalls();
        _assertQuiet();
        sp.settle(new address[](0));
    }

    function test_settle_waitsWhileAStoppedChallengeHoldsPositions() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        ChallengeAccount ch = _started(seat);
        _trade(address(ch), true, 0.001e8);
        _mockMargin(address(ch), 17e6, 76.4e6);
        ch.breach(new Cancel[](0), new uint32[](0), SALT);
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Breached));
        _assertBlocked(SharedPool.Blocker.PositionsOpen, address(ch));

        _mockMargin(address(ch), 17e6, 0); // closed
        _assertQuiet();
    }

    function test_settle_waitsWhileAFundedSeatBreaksARule() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        _passed(seat);
        int64 start_ = seat.fundedStart();
        _mockMargin(address(seat), start_ - 9e6, 0); // 80 less 11.25%
        _assertBlocked(SharedPool.Blocker.RuleBroken, address(seat));
    }

    function test_settle_waitsWhileASeatCloses() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        _passed(seat);
        vm.prank(trader);
        seat.stopFunded(new Cancel[](0), new uint32[](0), SALT);
        assertEq(uint8(seat.stage()), uint8(Pool.Stage.Closing));
        _assertBlocked(SharedPool.Blocker.SeatClosing, address(seat));
    }

    function test_settle_waitsWhileAPayoutIsInFlight() public {
        CoreSimulatorLib.forceAccountActivation(trader);
        _funded(150e8);
        Pool seat = _armedSeat();
        ChallengeAccount ch = _passed(seat);
        uint32[] memory none = new uint32[](0);
        // First step moves the perp side to spot; the next one sends the payout.
        for (uint256 i = 0; i < 4 && !ch.payoutDone(); ++i) {
            ch.settle(new Cancel[](0), none);
            if (!ch.payoutDone()) CoreSimulatorLib.nextBlock();
        }
        assertTrue(ch.payoutDone());
        _assertBlocked(SharedPool.Blocker.PayoutInFlight, address(ch));

        CoreSimulatorLib.nextBlock(); // it lands
        _assertQuiet();
    }

    function test_settle_withNothingInMotion_countsEverySeat() public {
        _funded(150e8);
        Pool seat = _armedSeat();
        _started(seat);
        address t = _ticket(bob, 159e8);
        sp.settle(_list(t));
        // Value 159 on 160 shares: bob's 159 buy 160 shares.
        assertEq(sp.sharesOf(bob), 160e8);
    }
}
