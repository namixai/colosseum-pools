// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test, Vm} from "forge-std/Test.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {Rules} from "../../src/Types.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {TermsBook} from "../../src/multi/TermsBook.sol";
import {SimPool, SimPlan, SimSize} from "../../src/sim/SimPool.sol";
import {SimPoolFactory} from "../../src/sim/SimPoolFactory.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {MockUsdc, OracleFollowsMark} from "../PoolHarness.sol";

/// The simulated pool on the hyper-evm-lib simulator, offline. BTC starts at 100 000.0, so on a size
/// of 5 000 USDC a position of 5 000 size units is 1x, and every 10 % of price is the target.
contract SimPoolTest is Test {
    uint32 constant BTC = 3;
    uint32 constant ETH = 4;
    uint64 constant SMALL = 5_000e6;
    uint64 constant LARGE = 10_000e6;
    uint64 constant SMALL_PRICE = 45e6;
    uint64 constant LARGE_PRICE = 85e6;
    uint64 constant LARGE_PAYOUT = 800e6;
    uint64 constant SMALL_PAYOUT = 400e6;

    HyperCore hyperCore;
    Usdc3009 usdc;
    TermsBook book;
    SimPoolFactory factory;
    bytes32 termsHash;

    address filler = makeAddr("filler");
    address checker = makeAddr("checker");
    address platform = makeAddr("platform");
    address creator = makeAddr("creator");
    address partner = makeAddr("partner");
    address closer = makeAddr("closer");
    /// The keccak256 of a trading group's code; `_open` sets it with no share.
    bytes32 constant CODE = keccak256("GROUP-ONE");
    address trader = makeAddr("trader");
    address other = makeAddr("other");
    address stranger = makeAddr("stranger");

    uint64 mark = 1_000_000;

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        vm.etch(0x0000000000000000000000000000000000000807, type(OracleFollowsMark).runtimeCode);
        hyperCore.registerPerpAssetInfo(
            BTC,
            PrecompileLib.PerpAssetInfo({coin: "BTC", marginTableId: 54, szDecimals: 5, maxLeverage: 40, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(BTC, mark);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);

        usdc = new Usdc3009();
        book = new TermsBook();
        termsHash = book.publish("The simulated pool's terms, every clause written out.");
        factory = new SimPoolFactory(IERC20(address(usdc)), address(new PaperExam()), address(new SimPool()), address(this));
        factory.setFiller(filler);
        factory.setChecker(checker);
        factory.setCloser(closer);
        factory.setFeeRecipient(platform);
        factory.setAsset(BTC, 1000);
        factory.setTermsBook(book);
        vm.deal(creator, 10 ether);
        deal(address(usdc), creator, 100_000e6);
        vm.prank(creator);
        usdc.approve(address(factory), type(uint256).max);
    }

    // ── helpers ──────────────────────────────────────────────────

    function _rules() internal pure returns (Rules memory r) {
        uint32[] memory assets = new uint32[](1);
        assets[0] = BTC;
        r = Rules({dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 500, assets: assets, assetLeverageX100: new uint32[](0)});
    }

    function _terms() internal pure returns (ExamTerms memory t) {
        uint16[] memory targets = new uint16[](1);
        targets[0] = 1000;
        t = ExamTerms({
            capital: 0,
            targetsBps: targets,
            stepDuration: 30 days,
            maxIdle: 30 days,
            minTradingDays: 0,
            costPpm: 0,
            fillDelay: 1,
            fillWindow: 5
        });
    }

    function _plan() internal view returns (SimPlan memory p) {
        p.menu = new SimSize[](2);
        p.menu[0] = SimSize({size: SMALL, price: SMALL_PRICE});
        p.menu[1] = SimSize({size: LARGE, price: LARGE_PRICE});
        p.examRules = _rules();
        p.exam = _terms();
        p.fundedRules = _rules();
        p.funded = _terms();
        p.fundWindow = 7 days;
        p.fromBook = true;
        p.maxWorseBps = 50;
        p.terms = termsHash;
    }

    function _open(SimPlan memory p) internal returns (SimPool pool) {
        vm.startPrank(creator);
        pool = SimPool(factory.create{value: 1 ether}(p));
        pool.setCode(CODE, partner, 0);
        vm.stopPrank();
    }

    function _share(SimPool pool, uint16 bps) internal {
        vm.prank(creator);
        pool.setCode(CODE, partner, bps);
    }

    function _pool() internal returns (SimPool) {
        return _open(_plan());
    }

    function _buy(SimPool pool, address who, uint256 line) internal returns (PaperExam) {
        SimSize[] memory m = pool.menu();
        deal(address(usdc), who, usdc.balanceOf(who) + m[line].price);
        vm.startPrank(who);
        usdc.approve(address(pool), m[line].price);
        address exam = pool.buy(line, CODE);
        vm.stopPrank();
        return PaperExam(exam);
    }

    function _fill(PaperExam e, address who, bool isBuy, uint64 sz) internal {
        vm.prank(who);
        e.place(BTC, isBuy, sz);
        vm.roll(block.number + 1);
        vm.prank(filler);
        e.fill();
    }

    function _setMark(uint64 px) internal {
        mark = px;
        CoreSimulatorLib.setMarkPx(BTC, px);
    }

    /// Long at about 1x, the price up 20 % (the size rounds down, so more than the target), flat again,
    /// and the step passed.
    function _pass(PaperExam e, address who) internal {
        (,, uint64 capital,) = e.result();
        uint64 sz = capital / mark;
        _fill(e, who, true, sz);
        _setMark(mark * 12 / 10);
        _fill(e, who, false, sz);
        vm.prank(who);
        e.graduate();
    }

    /// The price down past the drawdown line, and the rule called.
    function _fail(PaperExam e, address who) internal {
        (,, uint64 capital,) = e.result();
        uint64 sz = capital / mark;
        _fill(e, who, true, sz);
        _setMark(mark * 90 / 100);
        e.breach();
        _setMark(mark * 100 / 90);
    }

    /// Bought, passed, funded, passed: the stage, ready for its payout.
    function _passedStage(SimPool pool, address who, uint256 line) internal returns (PaperExam stage) {
        PaperExam exam = _buy(pool, who, line);
        _pass(exam, who);
        vm.prank(who);
        stage = PaperExam(pool.fund(address(exam)));
        _pass(stage, who);
    }

    function _trades() internal pure returns (bytes32[] memory t) {
        t = new bytes32[](2);
        t[0] = keccak256("fill one");
        t[1] = keccak256("fill two");
    }

    function _links() internal pure returns (bytes32[] memory l) {
        l = new bytes32[](1);
        l[0] = keccak256("a deposit from one wallet to both");
    }

    // ── opening ──────────────────────────────────────────────────

    function test_open_costsOneHypeAndLocksTwoPayoutsOfTheLargestSize() public {
        uint256 hype = platform.balance;
        uint256 before = usdc.balanceOf(creator);
        SimPool pool = _pool();
        assertEq(platform.balance - hype, 1 ether, "1 HYPE to the platform");
        assertEq(before - usdc.balanceOf(creator), 2 * LARGE_PAYOUT, "two payouts of 800");
        assertEq(usdc.balanceOf(address(pool)), 1_600e6);
        assertEq(pool.principal(), 1_600e6);
        assertEq(pool.largestPayout(), LARGE_PAYOUT, "8 % of 10 000");
        assertEq(pool.creator(), creator);
        assertEq(factory.poolOf(address(pool)), address(pool));
    }

    function test_open_takesExactlyOneHype() public {
        SimPlan memory p = _plan();
        uint256[2] memory wrong = [uint256(0.5 ether), 2 ether];
        for (uint256 i = 0; i < 2; ++i) {
            vm.prank(creator);
            vm.expectRevert(SimPoolFactory.WrongFee.selector);
            factory.create{value: wrong[i]}(p);
        }
    }

    function test_open_theCreatorIsNeitherTheFillerNorTheChecker() public {
        SimPlan memory p = _plan();
        address[2] memory roles = [filler, checker];
        for (uint256 i = 0; i < 2; ++i) {
            vm.deal(roles[i], 1 ether);
            vm.prank(roles[i]);
            vm.expectRevert(SimPoolFactory.BadRole.selector);
            factory.create{value: 1 ether}(p);
        }
    }

    function test_open_refusesAPlanOutsideThePlatformsBounds() public {
        SimPlan[] memory bad = new SimPlan[](17);
        for (uint256 i = 0; i < bad.length; ++i) bad[i] = _plan();
        bad[0].exam.fillWindow = 61; // past a minute
        bad[1].funded.stepDuration = 0; // a stage that never ends
        bad[2].exam.maxIdle = 0;
        bad[3].examRules.assets[0] = ETH; // not listed
        bad[4].fundedRules.maxLeverageX100 = 1001;
        bad[5].fundWindow = 30 minutes;
        bad[6].menu[1].price = 0;
        bad[7].examRules.assets[0] = ETH; // not listed, under a cap of its own of nothing
        bad[7].examRules.assetLeverageX100 = new uint32[](1);
        bad[8].examRules.maxLeverageX100 = 1001; // past 10x, whatever the asset's own cap
        bad[8].examRules.assetLeverageX100 = new uint32[](1);
        bad[8].examRules.assetLeverageX100[0] = 500;
        bad[9].maxWorseBps = 0; // the book with no ceiling
        bad[10].maxWorseBps = 501;
        bad[11].fromBook = false; // a ceiling with no book
        bad[12].examRules.assetLeverageX100 = new uint32[](2); // two caps for one asset
        bad[12].examRules.assetLeverageX100[0] = 500;
        bad[12].examRules.assetLeverageX100[1] = 500;
        bad[13].fundedRules.assets = new uint32[](2); // two assets, one cap
        bad[13].fundedRules.assets[0] = BTC;
        bad[13].fundedRules.assets[1] = BTC;
        bad[13].fundedRules.assetLeverageX100 = new uint32[](1);
        bad[13].fundedRules.assetLeverageX100[0] = 500;
        bad[14].menu[1].price = SMALL_PRICE; // two lines at one price: a signature could buy either
        bad[15].funded.fillDelay = 0; // a stage PaperExam would refuse to start
        bad[16].exam.costPpm = 100_001; // over PaperExam's ceiling
        for (uint256 i = 0; i < bad.length; ++i) {
            vm.prank(creator);
            vm.expectRevert(SimPoolFactory.BadPlan.selector);
            factory.create{value: 1 ether}(bad[i]);
        }
        SimPlan memory two = _plan();
        uint16[] memory steps = new uint16[](2);
        steps[0] = 500;
        steps[1] = 500;
        two.funded.targetsBps = steps; // the stage is one step
        vm.prank(creator);
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.create{value: 1 ether}(two);
    }

    function test_setCode_aShareIsZeroOrFortyToSixtyPercent() public {
        SimPool pool = _pool();
        uint16[5] memory shares = [uint16(0), 3999, 4000, 6000, 6001];
        for (uint256 i = 0; i < 5; ++i) {
            vm.prank(creator);
            if (i == 1 || i == 4) vm.expectRevert(SimPool.BadShare.selector);
            pool.setCode(keccak256(abi.encode(i)), partner, shares[i]);
        }
        vm.prank(creator);
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setCode(keccak256("nobody's"), address(0), 5000); // a share with no partner
        assertEq(pool.codes().length, 4, "CODE and the three that took");
    }

    function test_open_needsItsTermsWrittenOut() public {
        SimPlan memory p = _plan();
        p.terms = keccak256("never published");
        vm.prank(creator);
        vm.expectRevert(SimPoolFactory.Unpublished.selector);
        factory.create{value: 1 ether}(p);
    }

    function test_open_eachAssetsCapIsTheListsAtMost() public {
        factory.setAsset(BTC, 400); // under the plan's 5x
        SimPlan memory p = _plan();
        vm.prank(creator);
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.create{value: 1 ether}(p);
        factory.setAsset(BTC, 500);
        _open(p);
    }

    /// The factory's own bounds, not only PaperExam's: against an exam that would take any rules, the factory
    /// still refuses an asset off its list and leverage past 10x. PaperExam has the same two ceilings, so with
    /// it behind the factory a missing check of the factory's would not show (mutants SM13, SM15).
    function test_open_theFactoryHoldsItsOwnBounds_notOnlyTheExams() public {
        SimPoolFactory lax =
            new SimPoolFactory(IERC20(address(usdc)), address(new TakesAnyRules()), address(new SimPool()), address(this));
        lax.setAsset(BTC, 1000);
        lax.setTermsBook(book);
        lax.check(_plan()); // the plan itself is within every bound

        SimPlan memory offList = _plan(); // ETH is not listed, under a cap of its own of nothing
        offList.examRules.assets[0] = ETH;
        offList.examRules.assetLeverageX100 = new uint32[](1);
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        lax.check(offList);

        SimPlan memory past = _plan(); // past 10x, under an asset cap within the list's
        past.fundedRules.maxLeverageX100 = 1001;
        past.fundedRules.assetLeverageX100 = new uint32[](1);
        past.fundedRules.assetLeverageX100[0] = 500;
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        lax.check(past);
    }

    function test_buy_theExamFillsAtTheBookUnderThePoolsCeiling() public {
        PaperExam exam = _buy(_pool(), trader, 0);
        assertTrue(exam.fromBook());
        assertEq(exam.maxWorseBps(), 50);
    }

    // ── a purchase by signature (EIP-3009): the trader needs no gas ──

    function _signed(uint256 pk, address to, uint256 value, uint256 validBefore, bytes32 nonce)
        internal
        view
        returns (uint8 v, bytes32 r, bytes32 s)
    {
        address from = vm.addr(pk);
        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                usdc.DOMAIN_SEPARATOR(),
                keccak256(abi.encode(usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(), from, to, value, 0, validBefore, nonce))
            )
        );
        (v, r, s) = vm.sign(pk, digest);
    }

    function test_buyWithAuthorization_aRelayerSendsIt_theTraderNeedsNoGas() public {
        SimPool pool = _pool();
        (address signer, uint256 pk) = makeAddrAndKey("signer");
        deal(address(usdc), signer, LARGE_PRICE);
        uint256 pot = usdc.balanceOf(address(pool));
        bytes32 salt = keccak256("one");
        (uint8 v, bytes32 r, bytes32 s) =
            _signed(pk, address(pool), LARGE_PRICE, block.timestamp + 10 minutes, keccak256(abi.encode(CODE, salt)));
        vm.recordLogs();
        vm.prank(stranger); // the relayer
        address exam = pool.buyWithAuthorization(signer, 1, CODE, salt, 0, block.timestamp + 10 minutes, v, r, s);
        assertEq(PaperExam(exam).trader(), signer, "the exam is the signer's");
        assertEq(signer.balance, 0, "who signed had no gas");
        assertEq(usdc.balanceOf(signer), 0);
        assertEq(usdc.balanceOf(platform), 425_000, "the same 0.5 %");
        assertEq(usdc.balanceOf(address(pool)) - pot, LARGE_PRICE - 425_000);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool seen;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter == address(pool) && logs[i].topics[0] == SimPool.ExamSold.selector) {
                (,,,, bool bySignature, bytes32 code) =
                    abi.decode(logs[i].data, (uint256, uint64, uint64, uint256, bool, bytes32));
                assertTrue(bySignature);
                assertEq(code, CODE);
                assertEq(logs[i].topics[3], bytes32(uint256(uint160(partner))), "the code's partner");
                seen = true;
            }
        }
        assertTrue(seen);
        assertEq(pool.live(), 1);
    }

    function test_buyWithAuthorization_theSignedAmountIsTheLinesPrice() public {
        SimPool pool = _pool();
        (address signer, uint256 pk) = makeAddrAndKey("signer");
        deal(address(usdc), signer, LARGE_PRICE);
        bytes32 salt = keccak256("one");
        // Signed for the small line's price, sent for the large line.
        (uint8 v, bytes32 r, bytes32 s) =
            _signed(pk, address(pool), SMALL_PRICE, block.timestamp + 1 hours, keccak256(abi.encode(CODE, salt)));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, block.timestamp + 1 hours, v, r, s);
    }

    function test_buyWithAuthorization_aSignatureBuysOnce_inItsTime_atItsPool() public {
        SimPool pool = _pool();
        (address signer, uint256 pk) = makeAddrAndKey("signer");
        deal(address(usdc), signer, 2 * LARGE_PRICE);
        bytes32 salt = keccak256("one");
        uint256 until = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _signed(pk, address(pool), LARGE_PRICE, until, keccak256(abi.encode(CODE, salt)));
        // Another pool cannot use it: the token takes it only from its payee.
        SimPool other_ = _pool();
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        other_.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        vm.expectRevert(bytes("FiatTokenV2: authorization is used or canceled"));
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        bytes32 later = keccak256("two");
        (v, r, s) = _signed(pk, address(pool), LARGE_PRICE, until, keccak256(abi.encode(CODE, later)));
        vm.warp(until);
        vm.expectRevert(bytes("FiatTokenV2: authorization is expired"));
        pool.buyWithAuthorization(signer, 1, CODE, later, 0, until, v, r, s);
    }

    function test_buyWithAuthorization_whoSendsItCannotNameAnotherCode() public {
        SimPool pool = _pool();
        bytes32 theirs = keccak256("GROUP-TWO");
        vm.prank(creator);
        pool.setCode(theirs, makeAddr("other owner"), 5000);
        (address signer, uint256 pk) = makeAddrAndKey("signer");
        deal(address(usdc), signer, LARGE_PRICE);
        bytes32 salt = keccak256("one");
        uint256 until = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _signed(pk, address(pool), LARGE_PRICE, until, keccak256(abi.encode(CODE, salt)));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pool.buyWithAuthorization(signer, 1, theirs, salt, 0, until, v, r, s);
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        (,,,, uint256 got) = pool.codeOf(CODE);
        assertEq(got, LARGE_PRICE - 425_000, "the signed code brought it");
    }

    function test_buyWithAuthorization_notOnceClosed_noSuchLine() public {
        SimPool pool = _pool();
        vm.expectRevert(SimPool.NoSuchLine.selector);
        pool.buyWithAuthorization(trader, 2, CODE, 0, 0, 0, 0, 0, 0);
        vm.prank(closer);
        pool.close();
        vm.expectRevert(SimPool.Closing.selector);
        pool.buyWithAuthorization(trader, 0, CODE, 0, 0, 0, 0, 0, 0);
    }

    function test_buyWithAuthorization_aTokenWithoutItRefusesClearly_buyStillWorks() public {
        MockUsdc plain = new MockUsdc();
        SimPoolFactory f = new SimPoolFactory(IERC20(address(plain)), factory.examImpl(), factory.poolImpl(), address(this));
        f.setFiller(filler);
        f.setChecker(checker);
        f.setCloser(closer);
        f.setFeeRecipient(platform);
        f.setAsset(BTC, 1000);
        f.setTermsBook(book);
        deal(address(plain), creator, 10_000e6);
        vm.startPrank(creator);
        plain.approve(address(f), type(uint256).max);
        SimPool pool = SimPool(f.create{value: 1 ether}(_plan()));
        pool.setCode(CODE, partner, 0);
        vm.stopPrank();
        vm.expectRevert(SimPool.NoAuthorization.selector);
        pool.buyWithAuthorization(trader, 0, CODE, keccak256("one"), 0, block.timestamp + 1 hours, 27, bytes32(0), bytes32(0));
        deal(address(plain), trader, SMALL_PRICE);
        vm.startPrank(trader);
        plain.approve(address(pool), SMALL_PRICE);
        pool.buy(0, CODE);
        vm.stopPrank();
        assertEq(pool.live(), 1);
    }

    function test_aRefusedTransferIsHeldForItsTrader_andTheQueueMovesOn() public {
        SimPool pool = _pool();
        _withSales(pool, 40);
        uint256 a = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        uint256 b = pool.claimPayout(address(_passedStage(pool, other, 1)));
        usdc.block_(trader); // say the token blocks the trader's address after the pass
        vm.warp(block.timestamp + 72 hours);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutHeld(a, trader, LARGE_PAYOUT);
        assertEq(pool.pay(10), 2);
        assertEq(uint8(pool.payout(a).state), uint8(SimPool.PayoutState.Held));
        assertEq(uint8(pool.payout(b).state), uint8(SimPool.PayoutState.Paid), "the next one is paid");
        assertEq(pool.owed(), 0);
        assertEq(pool.held(), LARGE_PAYOUT);
        assertEq(pool.paidCount(), 1);
        uint256 bal = usdc.balanceOf(address(pool));
        assertEq(pool.withdrawable(), bal - LARGE_PAYOUT - 3 * LARGE_PAYOUT, "held money is not the creator's");
        address safe = makeAddr("safe");
        vm.prank(trader);
        pool.claimHeld(safe);
        assertEq(usdc.balanceOf(safe), LARGE_PAYOUT, "to an address the trader names");
        assertEq(pool.held(), 0);
        vm.prank(trader);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.claimHeld(safe);
    }

    function test_aTransferAnsweredFalseIsHeldToo() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        usdc.saysFalseTo(trader);
        vm.warp(block.timestamp + 72 hours);
        pool.pay(10);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Held));
        assertEq(pool.heldOf(trader), LARGE_PAYOUT);
    }

    function test_pay_neverSpendsWhatIsHeldForAnotherTrader() public {
        SimPool pool = _pool();
        pool.claimPayout(address(_passedStage(pool, trader, 1))); // A, 800
        pool.claimPayout(address(_passedStage(pool, other, 1))); // C, 800
        usdc.block_(trader);
        vm.warp(block.timestamp + 72 hours);
        pool.pay(10); // A's is held, C's is paid
        assertEq(pool.held(), LARGE_PAYOUT);
        assertLt(usdc.balanceOf(address(pool)) - pool.held(), 400e6, "what is free is under a small payout");
        uint256 b = pool.claimPayout(address(_passedStage(pool, stranger, 0))); // B, 400
        vm.warp(block.timestamp + 72 hours);
        pool.pay(10);
        assertEq(uint8(pool.payout(b).state), uint8(SimPool.PayoutState.Due), "B waits; A's money is not B's");
        vm.prank(trader);
        pool.claimHeld(makeAddr("safe"));
        assertEq(usdc.balanceOf(makeAddr("safe")), LARGE_PAYOUT, "A takes all of it");
    }

    function test_withdraw_aPartnersShareIsOfWhatIsAboveTheDebtAndThePrincipal() public {
        // The CFO's case: 1 600 locked and 3 400 added, 6 000 in the pool, 800 owed, a 50 % share.
        SimPool pool = _pool();
        _share(pool, 5000);
        vm.startPrank(creator);
        usdc.approve(address(pool), 3_400e6);
        pool.addToPot(3_400e6);
        vm.stopPrank();
        pool.claimPayout(address(_passedStage(pool, trader, 1)));
        deal(address(usdc), address(pool), 6_000e6);
        assertEq(pool.owed(), LARGE_PAYOUT);
        assertEq(pool.withdrawable(), 200e6, "before the term, the profit only");
        vm.warp(pool.lockedUntil());
        assertEq(pool.withdrawable(), 2_800e6);
        _ask(pool, 2_800e6);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(partner), 99_500_000, "half of 200 after the platform's part, not of 1 000");
        assertEq(pool.principal(), 2_400e6, "the rest came back as principal");
    }

    function test_finish_leavesWhatIsHeld_andTheTraderTakesItAfter() public {
        SimPool pool = _pool();
        pool.claimPayout(address(_passedStage(pool, trader, 1))); // the locked 1 600 covers it
        usdc.block_(trader);
        vm.warp(block.timestamp + 72 hours);
        pool.pay(10);
        vm.prank(closer);
        pool.close();
        vm.warp(block.timestamp + 7 days);
        vm.prank(creator);
        pool.finish();
        assertEq(usdc.balanceOf(address(pool)), LARGE_PAYOUT, "the held payout stays");
        vm.prank(trader);
        pool.claimHeld(other);
        assertEq(usdc.balanceOf(address(pool)), 0);
    }

    function test_noteAccount_onlyAPoolOfTheFactory() public {
        vm.prank(stranger);
        vm.expectRevert(SimPoolFactory.NotPool.selector);
        factory.noteAccount(stranger);
        assertEq(factory.poolOf(stranger), address(0));
    }

    function test_operator_oneKeyFillsAnotherStops() public {
        vm.expectRevert(SimPoolFactory.BadRole.selector);
        factory.setFiller(checker);
        vm.expectRevert(SimPoolFactory.BadRole.selector);
        factory.setChecker(filler);
        vm.prank(stranger);
        vm.expectRevert(SimPoolFactory.NotOperator.selector);
        factory.setAsset(ETH, 1000);
    }

    // ── selling ──────────────────────────────────────────────────

    function test_buy_takesThePriceAndThePlatformsHalfPercentOutOfIt() public {
        SimPool pool = _pool();
        uint256 pot = usdc.balanceOf(address(pool));
        PaperExam exam = _buy(pool, trader, 1);
        assertEq(usdc.balanceOf(platform), 425_000, "0.5 % of 85");
        assertEq(usdc.balanceOf(address(pool)) - pot, LARGE_PRICE - 425_000);
        assertEq(exam.filler(), filler, "the exam is filled by the platform's filler");
        assertEq(exam.trader(), trader);
        (,, uint64 capital,) = exam.result();
        assertEq(capital, LARGE);
        assertEq(factory.poolOf(address(exam)), address(pool));
        assertEq(pool.live(), 1);
    }

    function test_buy_hasNoSeatsAndNoShareToRunOutOf() public {
        SimPool pool = _pool();
        for (uint256 i = 0; i < 40; ++i) _buy(pool, trader, i % 2);
        assertEq(pool.live(), 40);
    }

    function test_buy_notOnceClosed() public {
        SimPool pool = _pool();
        vm.prank(closer);
        pool.close();
        deal(address(usdc), trader, 100e6);
        vm.startPrank(trader);
        usdc.approve(address(pool), 100e6);
        vm.expectRevert(SimPool.Closing.selector);
        pool.buy(0, CODE);
        vm.stopPrank();
    }

    // ── the stage and the payout ─────────────────────────────────

    function test_aPassedStagePaysEightPercentOnceAfterSeventyTwoHours() public {
        SimPool pool = _pool();
        PaperExam stage = _passedStage(pool, trader, 1);
        vm.recordLogs();
        uint256 id = pool.claimPayout(address(stage));
        assertEq(pool.owed(), LARGE_PAYOUT);
        SimPool.Payout memory p = pool.payout(id);
        assertEq(p.amount, LARGE_PAYOUT);
        assertEq(p.dueAt, block.timestamp + 72 hours);
        assertEq(p.trader, trader);

        vm.warp(block.timestamp + 72 hours - 1);
        assertEq(pool.pay(10), 0, "not before its time");
        vm.warp(block.timestamp + 1);
        uint256 before = usdc.balanceOf(trader);
        vm.prank(stranger);
        assertEq(pool.pay(10), 1, "anyone sends it");
        assertEq(usdc.balanceOf(trader) - before, LARGE_PAYOUT);
        assertEq(pool.owed(), 0);
        assertEq(pool.paidCount(), 1);
        assertEq(pool.paidTotal(), LARGE_PAYOUT);
        assertEq(pool.payout(id).amount, LARGE_PAYOUT);

        vm.expectRevert(SimPool.Done.selector);
        pool.claimPayout(address(stage));
        assertEq(pool.live(), 0, "the stage ended with its payout");
    }

    function test_aSmallStagePaysEightPercentOfItsOwnSize() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 0)));
        assertEq(pool.payout(id).amount, 400e6);
    }

    function test_fund_theTraderAloneWithinTheWindowAfterAPass() public {
        SimPool pool = _pool();
        PaperExam exam = _buy(pool, trader, 0);
        vm.prank(trader);
        vm.expectRevert(SimPool.NotPassed.selector);
        pool.fund(address(exam));
        _pass(exam, trader);
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotTrader.selector);
        pool.fund(address(exam));
        vm.warp(block.timestamp + 7 days + 1);
        vm.prank(trader);
        vm.expectRevert(SimPool.TooLate.selector);
        pool.fund(address(exam));
        pool.release(address(exam));
        assertEq(pool.live(), 0);
    }

    function test_fund_once() public {
        SimPool pool = _pool();
        PaperExam exam = _buy(pool, trader, 0);
        _pass(exam, trader);
        vm.startPrank(trader);
        address stage = pool.fund(address(exam));
        vm.expectRevert(SimPool.Done.selector);
        pool.fund(address(exam));
        vm.stopPrank();
        assertEq(PaperExam(stage).filler(), filler);
        assertEq(pool.live(), 1);
    }

    function test_release_aFailedExamOrStage_notARunningOne() public {
        SimPool pool = _pool();
        PaperExam exam = _buy(pool, trader, 0);
        vm.expectRevert(SimPool.StillRunning.selector);
        pool.release(address(exam));
        _fail(exam, trader);
        pool.release(address(exam));
        assertEq(pool.live(), 0);
        vm.expectRevert(SimPool.Done.selector);
        pool.release(address(exam));
    }

    function test_claimPayout_onlyForAPassedStage() public {
        SimPool pool = _pool();
        PaperExam exam = _buy(pool, trader, 0);
        _pass(exam, trader);
        vm.expectRevert(SimPool.NotAStage.selector);
        pool.claimPayout(address(exam));
        vm.prank(trader);
        address stage = pool.fund(address(exam));
        vm.expectRevert(SimPool.NotPassed.selector);
        pool.claimPayout(stage);
    }

    // ── stopping a payout ────────────────────────────────────────

    function _duePayout(SimPool pool) internal returns (uint256 id, address paired) {
        id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        paired = address(_buy(pool, other, 1));
    }

    function test_stop_theCheckerAloneWithAReasonTheChainKeeps() public {
        SimPool pool = _pool();
        (uint256 id, address paired) = _duePayout(pool);
        address[2] memory notChecker = [creator, stranger];
        for (uint256 i = 0; i < 2; ++i) {
            vm.prank(notChecker[i]);
            vm.expectRevert(SimPool.NotChecker.selector);
            pool.stopPayout(id, 1, paired, _trades(), new bytes32[](0));
        }
        vm.expectEmit(address(pool));
        emit SimPool.PayoutStopped(id, trader, LARGE_PAYOUT, 1, paired, _trades(), new bytes32[](0), 1, 0, 0);
        vm.prank(checker);
        pool.stopPayout(id, 1, paired, _trades(), new bytes32[](0));
        assertEq(pool.owed(), 0, "the money stays in the pool, owed to nobody");
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 0, "a stopped payout is not paid");
    }

    function test_stop_reasonTwoNeedsLinks_reasonOneHasNone() public {
        SimPool pool = _pool();
        (uint256 id, address paired) = _duePayout(pool);
        vm.startPrank(checker);
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 1, paired, _trades(), _links());
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 2, paired, _trades(), new bytes32[](0));
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 3, paired, _trades(), _links());
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 0, paired, _trades(), new bytes32[](0));
        bytes32[] memory one = new bytes32[](1);
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 1, paired, one, new bytes32[](0));
        pool.stopPayout(id, 2, paired, _trades(), _links());
        vm.stopPrank();
    }

    function test_stop_namesAnotherAccountOfTheFactory() public {
        SimPool pool = _pool();
        (uint256 id,) = _duePayout(pool);
        address own = pool.payout(id).account;
        address[3] memory bad = [own, stranger, address(pool)];
        for (uint256 i = 0; i < 3; ++i) {
            vm.prank(checker);
            vm.expectRevert(SimPool.BadPaired.selector);
            pool.stopPayout(id, 1, bad[i], _trades(), new bytes32[](0));
        }
        // An account of another pool of the same factory: the check is the platform's.
        SimPool elsewhere = _pool();
        address there = address(_buy(elsewhere, other, 0));
        vm.prank(checker);
        pool.stopPayout(id, 1, there, _trades(), new bytes32[](0));
    }

    function test_stop_onlyBeforeItIsDue_andOnce() public {
        SimPool pool = _pool();
        (uint256 id, address paired) = _duePayout(pool);
        vm.warp(block.timestamp + 72 hours);
        vm.prank(checker);
        vm.expectRevert(SimPool.NotDue.selector);
        pool.stopPayout(id, 1, paired, _trades(), new bytes32[](0));

        (uint256 id2, address paired2) = _duePayout(pool);
        vm.startPrank(checker);
        pool.stopPayout(id2, 1, paired2, _trades(), new bytes32[](0));
        pool.liftStop(id2);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.stopPayout(id2, 1, paired2, _trades(), new bytes32[](0));
        vm.stopPrank();
    }

    function test_liftStop_thePayoutIsDueAgainAndPaidAsAnyOther() public {
        SimPool pool = _pool();
        (uint256 id, address paired) = _duePayout(pool);
        vm.prank(checker);
        pool.stopPayout(id, 1, paired, _trades(), new bytes32[](0));
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 0);
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotChecker.selector);
        pool.liftStop(id);
        vm.expectEmit(address(pool));
        emit SimPool.StopLifted(id, 1, 1, 0);
        vm.prank(checker);
        pool.liftStop(id);
        assertEq(pool.owed(), LARGE_PAYOUT);
        uint256 before = usdc.balanceOf(trader);
        assertEq(pool.pay(10), 1, "past its time: paid at once");
        assertEq(usdc.balanceOf(trader) - before, LARGE_PAYOUT);
        vm.prank(checker);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.liftStop(id);
    }

    // ── a stopped payout's 14 days (CFO, 12 November) ───────────

    /// A stop of payout `id` of `pool`, the other account in another pool of the factory, so nothing of
    /// this pool runs because of it.
    function _stop(SimPool pool, uint256 id) internal {
        address there = address(_buy(_pool(), makeAddr("elsewhere"), 1));
        vm.prank(checker);
        pool.stopPayout(id, 1, there, _trades(), new bytes32[](0));
    }

    function test_stopReserve_theCreatorDoesNotWithdrawIt_untilTheStopIsFinal() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        _stop(pool, id);
        uint256 bal = usdc.balanceOf(address(pool));
        assertEq(pool.owed(), 0);
        assertEq(pool.reserved(), LARGE_PAYOUT);
        assertEq(pool.withdrawable(), bal - 3 * LARGE_PAYOUT - LARGE_PAYOUT, "a stop's money is kept for 14 days");
        vm.warp(block.timestamp + 14 days - 1);
        vm.expectRevert(SimPool.ReserveRunning.selector);
        pool.finalizeStop(id);
        vm.warp(block.timestamp + 1);
        vm.prank(checker);
        vm.expectRevert(SimPool.ReserveOver.selector);
        pool.liftStop(id);
        vm.expectEmit(address(pool));
        emit SimPool.StopFinal(id, LARGE_PAYOUT, 0);
        vm.prank(stranger);
        pool.finalizeStop(id);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Refused));
        assertEq(pool.reserved(), 0);
        assertEq(pool.withdrawable(), bal - 3 * LARGE_PAYOUT, "then it is the pool's again");
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.finalizeStop(id);
        vm.prank(checker);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.liftStop(id);
    }

    function test_stopReserve_isNotProfitForAPartnersShare() public {
        // The CFO's case of 12 November with a stop in place of the debt: 1 600 locked and 3 400 added,
        // 6 000 in the pool, 800 kept for a stop, a 50 % share.
        SimPool pool = _pool();
        _share(pool, 5000);
        vm.startPrank(creator);
        usdc.approve(address(pool), 3_400e6);
        pool.addToPot(3_400e6);
        vm.stopPrank();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        _stop(pool, id);
        deal(address(usdc), address(pool), 6_000e6);
        assertEq(pool.owed(), 0);
        assertEq(pool.withdrawable(), 200e6, "before the term, the profit only");
        vm.warp(pool.lockedUntil());
        assertEq(pool.withdrawable(), 2_800e6);
        _ask(pool, 2_800e6);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(partner), 99_500_000, "half of 200 after the platform's part, not of 1 000");
        assertEq(pool.principal(), 2_400e6);
    }

    function test_stopReserve_payNeverSpendsIt_soALiftedStopFindsItsMoney() public {
        SimPool pool = _pool();
        uint256 a = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        uint256 b = pool.claimPayout(address(_passedStage(pool, other, 1)));
        uint256 c = pool.claimPayout(address(_passedStage(pool, stranger, 1)));
        _stop(pool, a);
        // 1 600 + 3 x 84.575 in the pool, 800 of it kept for A's stop.
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 1, "B is paid; C waits, A's kept money is not C's");
        assertEq(uint8(pool.payout(b).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(c).state), uint8(SimPool.PayoutState.Due));
        vm.warp(block.timestamp + 10 days);
        vm.prank(checker);
        pool.liftStop(a);
        assertEq(pool.reserved(), 0);
        uint256 before = usdc.balanceOf(trader);
        pool.pay(10);
        assertEq(uint8(pool.payout(a).state), uint8(SimPool.PayoutState.Paid), "lifted: paid as any other");
        assertEq(usdc.balanceOf(trader) - before, LARGE_PAYOUT);
        assertEq(uint8(pool.payout(c).state), uint8(SimPool.PayoutState.Due));
    }

    function test_stopReserve_stopsBeyondThePoolsMoney_payStillRuns() public {
        // Four payouts of 800 on 1 600 locked and four prices: three stops keep 2 400, more than the pool holds.
        SimPool pool = _pool();
        uint256[4] memory id;
        address[4] memory who = [trader, other, stranger, makeAddr("fourth")];
        for (uint256 i = 0; i < 4; ++i) id[i] = pool.claimPayout(address(_passedStage(pool, who[i], 1)));
        for (uint256 i = 1; i < 4; ++i) _stop(pool, id[i]);
        assertGt(pool.reserved(), usdc.balanceOf(address(pool)));
        vm.warp(block.timestamp + 72 hours);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutQueued(id[0], 0);
        assertEq(pool.pay(10), 0, "nothing is free: the first payout waits, and pay does not revert");
        assertEq(pool.withdrawable(), 0);
    }

    function test_stopReserve_liftOnlyWithinFourteenDaysOfTheStop() public {
        SimPool pool = _pool();
        uint256 a = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        uint256 b = pool.claimPayout(address(_passedStage(pool, other, 1)));
        _stop(pool, a);
        _stop(pool, b);
        assertEq(pool.payout(a).stoppedAt, block.timestamp);
        vm.warp(block.timestamp + 14 days - 1);
        vm.startPrank(checker);
        pool.liftStop(a);
        vm.warp(block.timestamp + 1);
        vm.expectRevert(SimPool.ReserveOver.selector);
        pool.liftStop(b);
        vm.stopPrank();
    }

    function test_stopReserve_theTraderObjectsOnChain_withinTheFourteenDays() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        bytes32 text = keccak256("The other account is not mine.");
        vm.prank(trader);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.objectStop(id, text);
        _stop(pool, id);
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotTrader.selector);
        pool.objectStop(id, text);
        vm.expectEmit(address(pool));
        emit SimPool.StopObjected(id, trader, text);
        vm.prank(trader);
        pool.objectStop(id, text);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Stopped), "an objection changes nothing");
        vm.warp(block.timestamp + 14 days);
        vm.prank(trader);
        vm.expectRevert(SimPool.ReserveOver.selector);
        pool.objectStop(id, text);
    }

    function test_stopReserve_finishWaitsForTheStopToBeFinal() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        _stop(pool, id);
        vm.prank(closer);
        pool.close();
        vm.prank(creator);
        vm.expectRevert(SimPool.NotOver.selector);
        pool.finish();
        vm.warp(block.timestamp + 14 days);
        pool.finalizeStop(id);
        vm.prank(creator);
        pool.finish();
        assertEq(usdc.balanceOf(address(pool)), 0);
    }

    // ── a queue, when payouts come faster than prices ────────────

    function test_payoutsAreQueuedInOrder_andTheCreatorWithdrawsNothingMeanwhile() public {
        SimPool pool = _pool();
        // Three passes on 1 600 locked and three prices: the third payout waits.
        uint256 a = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        uint256 b = pool.claimPayout(address(_passedStage(pool, other, 1)));
        uint256 c = pool.claimPayout(address(_passedStage(pool, stranger, 1)));
        assertEq(pool.withdrawable(), 0);
        vm.warp(block.timestamp + 72 hours);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutQueued(c, 1_600e6 + 3 * (LARGE_PRICE - 425_000) - 2 * LARGE_PAYOUT);
        assertEq(pool.pay(10), 2);
        assertEq(uint8(pool.payout(a).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(b).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(c).state), uint8(SimPool.PayoutState.Due));
        assertEq(pool.head(), c);
        vm.prank(creator);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.requestWithdraw(1);
        // Prices keep coming in, and the queue is paid out of them.
        for (uint256 i = 0; i < 10; ++i) _buy(pool, makeAddr(string(abi.encode(i))), 1);
        assertEq(pool.pay(10), 1);
        assertEq(pool.owed(), 0);
    }

    // ── the creator ──────────────────────────────────────────────

    function _buyWith(SimPool pool, address who, uint256 line, bytes32 code) internal returns (PaperExam) {
        SimSize[] memory m = pool.menu();
        deal(address(usdc), who, usdc.balanceOf(who) + m[line].price);
        vm.startPrank(who);
        usdc.approve(address(pool), m[line].price);
        address exam = pool.buy(line, code);
        vm.stopPrank();
        return PaperExam(exam);
    }

    function _withSales(SimPool pool, uint256 n) internal {
        for (uint256 i = 0; i < n; ++i) _buy(pool, makeAddr(string(abi.encode("buyer", i))), 1);
    }

    /// The creator asks to withdraw `amount` and the week passes: `withdraw()` is next.
    function _ask(SimPool pool, uint256 amount) internal {
        vm.prank(creator);
        pool.requestWithdraw(amount);
        vm.warp(block.timestamp + factory.WITHDRAW_DELAY());
    }

    function test_withdraw_onlyAboveThreePayoutsAndWhatIsOwed() public {
        SimPool pool = _pool();
        _withSales(pool, 30); // 30 x 84.575 = 2 537.25 in, 4 137.25 held
        uint256 bal = usdc.balanceOf(address(pool));
        assertEq(pool.withdrawable(), bal - 3 * LARGE_PAYOUT);
        pool.claimPayout(address(_passedStage(pool, trader, 1)));
        bal = usdc.balanceOf(address(pool));
        assertEq(pool.withdrawable(), bal - 3 * LARGE_PAYOUT - LARGE_PAYOUT, "and above what is owed");
        uint256 w = pool.withdrawable();
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.requestWithdraw(w);
        vm.prank(creator);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.requestWithdraw(w + 1);
    }

    function test_withdraw_halfAPercentToThePlatform() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        uint256 fees = usdc.balanceOf(platform);
        uint256 before = usdc.balanceOf(creator);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(platform) - fees, 5e6, "0.5 % of 1 000");
        assertEq(usdc.balanceOf(creator) - before, 995e6);
    }

    function test_withdraw_aPartnerGetsItsShareOfTheProfit() public {
        SimPool pool = _pool();
        _share(pool, 5000);
        _withSales(pool, 30);
        uint256 before = usdc.balanceOf(creator);
        _ask(pool, 1_000e6);
        vm.expectEmit(address(pool));
        emit SimPool.Withdrawn(creator, 1_000e6, 5e6, 497_500_000);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(partner), 497_500_000, "half of what is left after the platform's part");
        assertEq(usdc.balanceOf(creator) - before, 497_500_000);
        assertEq(pool.principal(), 1_600e6, "all of it was profit: the principal stays");
    }

    function test_setCode_onlyTheCreator_changeableUntilASaleNamesIt() public {
        SimPool pool = _pool();
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.setCode(CODE, stranger, 5000);
        vm.expectEmit(address(pool));
        emit SimPool.CodeSet(CODE, partner, 6000);
        _share(pool, 6000); // before any sale: changed
        (address who, uint16 bps,,,) = pool.codeOf(CODE);
        assertEq(who, partner);
        assertEq(bps, 6000);
        _buy(pool, trader, 0);
        vm.startPrank(creator);
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setCode(CODE, creator, 6000); // the partner's address stays
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setCode(CODE, partner, 4000); // and so does its share
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setCode(CODE, address(0), 0); // and it stays on sale
        vm.stopPrank();
        assertEq(pool.codes().length, 1);
    }

    function test_buy_needsACodeTheCreatorSet() public {
        SimPool pool = _pool();
        bytes32 unset = keccak256("NOT-A-CODE");
        deal(address(usdc), trader, SMALL_PRICE);
        vm.startPrank(trader);
        usdc.approve(address(pool), SMALL_PRICE);
        vm.expectRevert(SimPool.NoSuchCode.selector);
        pool.buy(0, unset);
        vm.stopPrank();
        // Taken off sale before any sale named it: refused the same way, and still listed once.
        vm.prank(creator);
        pool.setCode(CODE, address(0), 0);
        vm.prank(trader);
        vm.expectRevert(SimPool.NoSuchCode.selector);
        pool.buy(0, CODE);
        _share(pool, 5000);
        assertEq(pool.codes().length, 1, "set again, listed once");
        vm.prank(creator);
        pool.setCode(keccak256("NEVER-ON-SALE"), address(0), 0);
        assertEq(pool.codes().length, 1, "a code set off sale before it was ever listed takes no place");
    }

    function test_setCode_atMostMaxCodes() public {
        SimPool pool = _pool();
        vm.startPrank(creator);
        for (uint256 i = 1; i < factory.MAX_CODES(); ++i) pool.setCode(keccak256(abi.encode(i)), partner, 0);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.setCode(keccak256("one too many"), partner, 0);
        pool.setCode(CODE, makeAddr("next"), 0); // one already listed can still change
        vm.stopPrank();
        assertEq(pool.codes().length, factory.MAX_CODES());
    }

    function test_withdraw_eachCodeGetsItsShareInThePartItBrought() public {
        // Code A, 50 %, brings two large sales; code B, 40 %, one; code C, no share, one.
        SimPool pool = _pool();
        _share(pool, 5000);
        address ownerB = makeAddr("owner B");
        bytes32 codeB = keccak256("GROUP-TWO");
        bytes32 codeC = keccak256("PLATFORM");
        vm.startPrank(creator);
        pool.setCode(codeB, ownerB, 4000);
        pool.setCode(codeC, creator, 0);
        vm.stopPrank();
        _buy(pool, trader, 1);
        _buy(pool, other, 1);
        _buyWith(pool, stranger, 1, codeB);
        _buyWith(pool, makeAddr("fourth"), 1, codeC);
        uint256 before = usdc.balanceOf(creator);
        assertEq(pool.brought(), 4 * (LARGE_PRICE - 425_000), "each sale brought 84.575");
        deal(address(usdc), address(pool), 6_000e6); // 4 400 above the 1 600 locked; 2 400 kept while open
        _ask(pool, 3_600e6);
        vm.expectEmit(address(pool));
        emit SimPool.PartnerPaid(CODE, partner, 895_500_000, true);
        vm.expectEmit(address(pool));
        emit SimPool.PartnerPaid(codeB, ownerB, 358_200_000, true);
        vm.prank(creator);
        pool.withdraw();
        // 3 600 less the platform's 18 is 3 582: A brought half of it all, B a quarter, C a quarter.
        assertEq(usdc.balanceOf(partner), 895_500_000, "50 % of A's half");
        assertEq(usdc.balanceOf(ownerB), 358_200_000, "40 % of B's quarter");
        assertEq(usdc.balanceOf(creator) - before, 3_582e6 - 895_500_000 - 358_200_000, "C's quarter stays the creator's");
        assertEq(pool.principal(), 1_600e6, "all of it was profit");
    }

    function test_withdraw_withEveryCodeSharing_staysWithinAMillionAndAHalfGas() public {
        // MAX_CODES codes, each with a share and a sale: the most transfers one withdrawal makes.
        SimPool pool = _pool();
        uint256 n = factory.MAX_CODES();
        vm.startPrank(creator);
        pool.setCode(CODE, partner, 4000);
        for (uint256 i = 1; i < n; ++i) {
            pool.setCode(keccak256(abi.encode("group", i)), makeAddr(string(abi.encode("owner", i))), 4000);
        }
        vm.stopPrank();
        _buy(pool, trader, 1);
        for (uint256 i = 1; i < n; ++i) {
            _buyWith(pool, makeAddr(string(abi.encode("buyer", i))), 1, keccak256(abi.encode("group", i)));
        }
        deal(address(usdc), address(pool), 6_000e6);
        _ask(pool, pool.withdrawable());
        vm.recordLogs();
        vm.prank(creator);
        uint256 before = gasleft();
        pool.withdraw();
        uint256 used = before - gasleft();
        uint256 paid;
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == SimPool.PartnerPaid.selector) paid += 1;
        }
        assertEq(paid, n, "every code's partner is paid in the one withdrawal");
        emit log_named_uint("withdraw gas, 32 codes sharing", used);
        assertLt(used, 1_500_000, "a bounded loop: about one transfer a code");
    }

    function test_withdraw_aPartnerTheTokenRefusesHasItsPartSetAside() public {
        SimPool pool = _pool();
        _share(pool, 5000);
        _withSales(pool, 30);
        usdc.block_(partner);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(partner), 0);
        assertEq(pool.heldOf(partner), 497_500_000, "set aside for the partner");
        assertEq(pool.held(), 497_500_000);
        assertEq(pool.withdrawable(), usdc.balanceOf(address(pool)) - 497_500_000 - 3 * LARGE_PAYOUT,
            "and kept out of what the creator may take");
        vm.prank(partner);
        pool.claimHeld(makeAddr("partner's other address"));
        assertEq(usdc.balanceOf(makeAddr("partner's other address")), 497_500_000);
    }

    function test_addToPot_comesBackFirstWithoutTheShare() public {
        SimPool pool = _pool();
        _share(pool, 5000);
        vm.startPrank(creator);
        usdc.approve(address(pool), 4_000e6);
        pool.addToPot(4_000e6);
        vm.stopPrank();
        assertEq(pool.principal(), 5_600e6);
        assertEq(pool.withdrawable(), 0, "before the term only profit leaves, and there is none");
        vm.warp(pool.lockedUntil());
        // 5 600 in, 2 400 kept: 3 200 may leave, and all of it is the creator's own money.
        assertEq(pool.withdrawable(), 3_200e6);
        _ask(pool, 3_200e6);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(partner), 0, "no profit, no share");
        assertEq(pool.principal(), 2_400e6);
    }

    // ── the term: 6 months (184 days) before the creator closes or takes back what it put in ──

    function test_open_thePoolIsLockedForTheFactorysTerm_andTheCloserOpensNone() public {
        SimPool pool = _pool();
        assertEq(pool.lockedUntil(), block.timestamp + 184 days);
        assertEq(factory.MIN_TERM(), 184 days, "the longest half-year: at least 6 months whatever the day it opens");
        vm.deal(closer, 1 ether);
        vm.prank(closer);
        vm.expectRevert(SimPoolFactory.BadRole.selector);
        factory.create{value: 1 ether}(_plan());
        vm.prank(stranger);
        vm.expectRevert(SimPoolFactory.NotOperator.selector);
        factory.setCloser(stranger);
        vm.expectRevert(SimPoolFactory.BadRole.selector);
        factory.setCloser(address(0));

        // A factory with no closer opens no pool: nobody could close one before its term.
        SimPoolFactory none = new SimPoolFactory(IERC20(address(usdc)), factory.examImpl(), factory.poolImpl(), address(this));
        none.setFiller(filler);
        none.setChecker(checker);
        none.setFeeRecipient(platform);
        none.setAsset(BTC, 1000);
        none.setTermsBook(book);
        vm.startPrank(creator);
        usdc.approve(address(none), type(uint256).max);
        vm.expectRevert(SimPoolFactory.BadRole.selector);
        none.create{value: 1 ether}(_plan());
        vm.stopPrank();
        none.setCloser(closer);
        vm.prank(creator);
        none.create{value: 1 ether}(_plan());
    }

    function test_close_theCreatorOnlyFromTheTerm_noOneElseButTheCloser() public {
        SimPool pool = _pool();
        vm.warp(pool.lockedUntil() - 1);
        vm.prank(creator);
        vm.expectRevert(SimPool.Locked.selector);
        pool.close();
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.close();
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.finish();
        vm.warp(pool.lockedUntil());
        vm.prank(creator);
        pool.close();
        assertTrue(pool.closed());
        assertFalse(pool.closedEarly(), "closed at its term");
    }

    function test_close_earlyByTheCloser_theMoneyGoesWhereItWouldAnyway() public {
        SimPool pool = _pool();
        _share(pool, 5000);
        PaperExam exam = _buy(pool, trader, 1);
        vm.expectEmit(address(pool));
        emit SimPool.ClosedEarly(closer);
        vm.prank(closer);
        pool.close();
        assertTrue(pool.closedEarly());
        _fail(exam, trader);
        pool.release(address(exam));
        uint256 bal = usdc.balanceOf(address(pool));
        uint256 profit = bal - 1_600e6;
        uint256 toPartner = (profit - profit * 50 / 10_000) * 5000 / 10_000;
        uint256 before = usdc.balanceOf(creator);
        vm.warp(block.timestamp + 7 days);
        vm.prank(closer); // the closer may finish it too
        pool.finish();
        assertEq(usdc.balanceOf(closer), 0, "the closer gets none of it");
        assertEq(usdc.balanceOf(partner), toPartner, "the code's partner its share");
        assertEq(usdc.balanceOf(creator) - before, bal - bal * 50 / 10_000 - toPartner, "the creator the rest");
    }

    function test_withdraw_beforeTheTermTheProfitOnly_neverWhatTheCreatorPutIn() public {
        SimPool pool = _pool();
        vm.startPrank(creator);
        usdc.approve(address(pool), 4_000e6);
        pool.addToPot(4_000e6);
        vm.stopPrank();
        _withSales(pool, 10); // 845.75 of profit; 5 600 put in, 2 400 kept while open
        uint256 profit = 10 * (LARGE_PRICE - 425_000);
        assertEq(pool.withdrawable(), profit, "the profit, not the 3 200 of principal above the floor");
        vm.prank(creator);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.requestWithdraw(profit + 1);
        _ask(pool, profit);
        vm.prank(creator);
        pool.withdraw();
        assertEq(pool.principal(), 5_600e6, "every unit put in stays");
        assertEq(pool.withdrawable(), 0);
        vm.warp(pool.lockedUntil());
        assertEq(pool.withdrawable(), 3_200e6, "from the term on, as before");
    }

    function test_finish_onceClosedAndNothingRunsOrIsOwed_principalFirst() public {
        SimPool pool = _pool();
        _share(pool, 5000);
        PaperExam exam = _buy(pool, trader, 1);
        vm.startPrank(creator);
        vm.expectRevert(SimPool.Done.selector);
        pool.finish();
        vm.stopPrank();
        vm.prank(closer);
        pool.close();
        vm.prank(creator);
        vm.expectRevert(SimPool.NotOver.selector);
        pool.finish();
        _fail(exam, trader);
        pool.release(address(exam));

        uint256 bal = usdc.balanceOf(address(pool)); // 1 600 + 84.575
        uint256 profit = bal - 1_600e6;
        uint256 toPartner = (profit - profit * 50 / 10_000) * 5000 / 10_000;
        uint256 before = usdc.balanceOf(creator);
        vm.warp(block.timestamp + 7 days);
        vm.prank(creator);
        pool.finish();
        assertEq(usdc.balanceOf(address(pool)), 0);
        assertEq(usdc.balanceOf(partner), toPartner, "a share of the profit only");
        assertEq(usdc.balanceOf(creator) - before, bal - bal * 50 / 10_000 - toPartner);
        vm.prank(checker);
        vm.expectRevert(SimPool.Done.selector);
        pool.liftStop(0);
    }

    // ── a withdrawal waits a week from its request ───────────────

    function test_withdraw_onlyByARequest_thereIsNoOtherWay() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        vm.prank(creator);
        vm.expectRevert(SimPool.NoRequest.selector);
        pool.withdraw();
        // The withdrawal of an amount at once is gone from the pool.
        vm.prank(creator);
        (bool ok,) = address(pool).call(abi.encodeWithSignature("withdraw(uint256)", 1e6));
        assertFalse(ok, "no withdraw(amount)");
        assertEq(usdc.balanceOf(address(pool)), 1_600e6 + 30 * (LARGE_PRICE - 425_000), "nothing left the pool");
    }

    function test_withdraw_aWeekAfterTheRequest_theLessOfWhatWasAskedAndWhatIsFreeThen() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        uint256 w0 = pool.withdrawable();
        uint64 ready = uint64(block.timestamp) + 7 days;
        vm.expectEmit(address(pool));
        emit SimPool.WithdrawRequested(w0, ready, ready + 3 days);
        vm.prank(creator);
        pool.requestWithdraw(w0);
        assertEq(pool.requested(), w0);
        assertEq(pool.readyAt(), ready);
        assertEq(factory.WITHDRAW_DELAY(), 7 days);
        assertEq(factory.WITHDRAW_WINDOW(), 3 days);
        vm.warp(ready - 1);
        vm.prank(creator);
        vm.expectRevert(SimPool.NotReady.selector);
        pool.withdraw();
        // Within the week: more sales come in, and two stages pass and are claimed.
        _withSales(pool, 10);
        pool.claimPayout(address(_passedStage(pool, trader, 1)));
        pool.claimPayout(address(_passedStage(pool, other, 1)));
        vm.warp(ready);
        uint256 now_ = pool.withdrawable();
        assertLt(now_, w0, "the payouts that came due come first");
        uint256 before = usdc.balanceOf(creator);
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.withdraw();
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(creator) - before, now_ - now_ * 50 / 10_000, "what was free then, less 0.5 %");
        assertEq(pool.requested(), 0);
        assertEq(pool.readyAt(), 0);
        vm.prank(creator);
        vm.expectRevert(SimPool.NoRequest.selector);
        pool.withdraw();
    }

    function test_withdraw_theCfosExample_aWeekOfSalesAndPayouts_thenTheLessOfTheTwo() public {
        // The example of the letter to the CFO, 13 November: code A (50 %) and code B (the creator's, no share).
        SimPool pool = _pool();
        _share(pool, 5000);
        bytes32 codeB = keccak256("PLATFORM");
        vm.prank(creator);
        pool.setCode(codeB, creator, 0);
        for (uint256 i = 0; i < 40; ++i) _buyWith(pool, makeAddr(string(abi.encode("a", i))), 1, CODE);
        for (uint256 i = 0; i < 20; ++i) _buyWith(pool, makeAddr(string(abi.encode("b", i))), 1, codeB);
        deal(address(usdc), address(pool), 5_074_500_000); // 1 600 + 60 x 84.575, less two payouts paid
        assertEq(pool.withdrawable(), 2_674_500_000, "day 0: 5 074.50 less the floor of 2 400");
        vm.prank(creator);
        pool.requestWithdraw(2_000e6);
        for (uint256 i = 0; i < 6; ++i) _buyWith(pool, makeAddr(string(abi.encode("a2", i))), 1, CODE);
        for (uint256 i = 0; i < 4; ++i) _buyWith(pool, makeAddr(string(abi.encode("b2", i))), 1, codeB);
        deal(address(usdc), address(pool), usdc.balanceOf(address(pool)) - 1_600e6); // two more payouts paid
        assertEq(usdc.balanceOf(address(pool)), 4_320_250_000);
        vm.warp(block.timestamp + 7 days);
        assertEq(pool.withdrawable(), 1_920_250_000, "day 7");
        uint256 fees = usdc.balanceOf(platform);
        uint256 before = usdc.balanceOf(creator);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(platform) - fees, 9_601_250, "0.5 % of 1 920.25");
        assertEq(usdc.balanceOf(partner), 627_784_589, "A: 50 % of 1 910.64875 x 46/70");
        assertEq(usdc.balanceOf(creator) - before, 1_282_864_161, "the creator the rest, B's part with it");
        assertEq(usdc.balanceOf(address(pool)), 2_400e6, "the floor of three payouts stays");
    }

    function test_withdraw_moneyThatCameInAfterTheRequestStays() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        _ask(pool, 500e6);
        _withSales(pool, 30);
        uint256 bal = usdc.balanceOf(address(pool));
        vm.prank(creator);
        pool.withdraw();
        assertEq(bal - usdc.balanceOf(address(pool)), 500e6, "what was asked, not what came in since");
    }

    function test_withdraw_aRequestLapsesThreeDaysAfterItIsReady_aNewOneWaitsAgain() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        _ask(pool, 500e6);
        vm.warp(pool.readyAt() + 3 days);
        vm.prank(creator);
        vm.expectRevert(SimPool.RequestLapsed.selector);
        pool.withdraw();
        vm.prank(creator);
        pool.requestWithdraw(400e6);
        assertEq(pool.readyAt(), block.timestamp + 7 days, "a new request starts the week again");
        vm.warp(pool.readyAt() + 3 days - 1);
        vm.prank(creator);
        pool.withdraw();
        assertEq(pool.requested(), 0);
    }

    function test_requestWithdraw_aNewOneReplacesTheStandingOne() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        _ask(pool, 500e6); // ready now
        vm.prank(creator);
        pool.requestWithdraw(100e6);
        assertEq(pool.requested(), 100e6);
        vm.prank(creator);
        vm.expectRevert(SimPool.NotReady.selector);
        pool.withdraw(); // the ready request is gone with it
        vm.prank(creator);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.requestWithdraw(0);
    }

    function test_cancelWithdraw_theCreatorOrTheCloser_movesNothing() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        vm.prank(closer);
        vm.expectRevert(SimPool.NoRequest.selector);
        pool.cancelWithdraw();
        vm.prank(creator);
        pool.requestWithdraw(500e6);
        uint256 bal = usdc.balanceOf(address(pool));
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.cancelWithdraw();
        vm.expectEmit(address(pool));
        emit SimPool.WithdrawCancelled(closer, 500e6);
        vm.prank(closer);
        pool.cancelWithdraw();
        assertEq(pool.requested(), 0);
        assertEq(usdc.balanceOf(address(pool)), bal, "nothing moved");
        assertEq(usdc.balanceOf(closer), 0);
        vm.warp(block.timestamp + 7 days);
        vm.prank(creator);
        vm.expectRevert(SimPool.NoRequest.selector);
        pool.withdraw();
        vm.prank(creator);
        pool.requestWithdraw(300e6);
        vm.expectEmit(address(pool));
        emit SimPool.WithdrawCancelled(creator, 300e6);
        vm.prank(creator);
        pool.cancelWithdraw();
    }

    function test_withdraw_nothingFreeWhenReady_refused_theRequestStands() public {
        SimPool pool = _pool();
        _withSales(pool, 20);
        uint256 w = pool.withdrawable();
        _ask(pool, w);
        // Payouts claimed within the week take all of it.
        pool.claimPayout(address(_passedStage(pool, trader, 1)));
        pool.claimPayout(address(_passedStage(pool, other, 1)));
        assertEq(pool.withdrawable(), 0);
        vm.prank(creator);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.withdraw();
        assertEq(pool.requested(), w, "still standing, until it lapses or is replaced");
    }

    function test_finish_aWeekAfterTheClose_evenAnEarlyOne_andItEndsAStandingRequest() public {
        SimPool pool = _pool();
        deal(address(usdc), address(pool), 5_000e6); // money, and nothing running
        vm.prank(creator);
        pool.requestWithdraw(500e6);
        vm.prank(closer);
        pool.close();
        assertEq(pool.closedAt(), block.timestamp);
        vm.warp(block.timestamp + 7 days - 1);
        vm.prank(creator);
        vm.expectRevert(SimPool.NotReady.selector);
        pool.finish();
        vm.warp(block.timestamp + 1);
        vm.prank(closer);
        pool.finish();
        assertEq(pool.requested(), 0, "finish sent everything: no request stands");
        assertEq(pool.readyAt(), 0);
        vm.prank(creator);
        vm.expectRevert(SimPool.Done.selector);
        pool.requestWithdraw(1);
        vm.prank(creator);
        vm.expectRevert(SimPool.Done.selector);
        pool.withdraw();
    }

    // ── a running stage's payout stays in the pool (CFO, 13 November) ──

    /// A stage funded and still running: its payout is in `staged` until it ends.
    function _runningStage(SimPool pool, address who, uint256 line) internal returns (PaperExam stage) {
        PaperExam exam = _buy(pool, who, line);
        _pass(exam, who);
        vm.prank(who);
        stage = PaperExam(pool.fund(address(exam)));
    }

    function test_staged_eachRunningStagesPayoutBySize_untilItIsClaimedOrReleased() public {
        SimPool pool = _pool();
        PaperExam large = _runningStage(pool, trader, 1);
        assertEq(pool.staged(), LARGE_PAYOUT);
        PaperExam small = _runningStage(pool, other, 0);
        assertEq(pool.staged(), LARGE_PAYOUT + SMALL_PAYOUT, "each by its own size");
        _pass(large, trader);
        pool.claimPayout(address(large));
        assertEq(pool.staged(), SMALL_PAYOUT, "claimed: owed now, no longer staged");
        assertEq(pool.owed(), LARGE_PAYOUT);
        _fail(small, other);
        pool.release(address(small));
        assertEq(pool.staged(), 0, "failed and released: free again");
        PaperExam exam = _buy(pool, stranger, 1);
        _fail(exam, stranger);
        pool.release(address(exam));
        assertEq(pool.staged(), 0, "an exam is not a stage");
    }

    function test_withdrawable_theGreaterOfThreePayoutsAndTheRunningStages_theCfosCases() public {
        // The CFO's day 7, 13 November: 4 320.25 in the pool, nothing owed; k large stages running.
        uint256[3] memory ks = [uint256(1), 3, 5];
        uint256[3] memory free = [uint256(1_920_250_000), 1_920_250_000, 320_250_000];
        for (uint256 j = 0; j < 3; ++j) {
            SimPool pool = _pool();
            for (uint256 i = 0; i < ks[j]; ++i) _runningStage(pool, makeAddr(string(abi.encode("k", j, i))), 1);
            deal(address(usdc), address(pool), 4_320_250_000);
            assertEq(pool.staged(), ks[j] * LARGE_PAYOUT);
            assertEq(pool.withdrawable(), free[j], "the greater of 2 400 and the stages' payouts stays");
        }
        // By size: four large and two small stages keep 4 000, not six large payouts.
        SimPool mixed = _pool();
        for (uint256 i = 0; i < 4; ++i) _runningStage(mixed, makeAddr(string(abi.encode("L", i))), 1);
        for (uint256 i = 0; i < 2; ++i) _runningStage(mixed, makeAddr(string(abi.encode("S", i))), 0);
        deal(address(usdc), address(mixed), 4_320_250_000);
        assertEq(mixed.staged(), 4 * LARGE_PAYOUT + 2 * SMALL_PAYOUT);
        assertEq(mixed.withdrawable(), 4_320_250_000 - 4_000e6);
    }

    function test_withdraw_aRequestCarriedOutAfterStagesStarted_paysOnlyAboveThem() public {
        SimPool pool = _pool();
        deal(address(usdc), address(pool), 6_000e6);
        uint256 w = pool.withdrawable();
        _ask(pool, w); // the week passes
        for (uint256 i = 0; i < 5; ++i) _runningStage(pool, makeAddr(string(abi.encode("w", i))), 1);
        uint256 bal = usdc.balanceOf(address(pool));
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(address(pool)), 5 * LARGE_PAYOUT, "the five stages' payouts stay");
        assertLt(bal - usdc.balanceOf(address(pool)), w, "less than asked: stages started in the week");
    }

    function test_finish_fromTheTerm_closeAndFinishAreNeverOneBlock() public {
        SimPool pool = _pool();
        deal(address(usdc), address(pool), 5_000e6);
        vm.warp(pool.lockedUntil());
        vm.startPrank(creator);
        pool.close();
        vm.expectRevert(SimPool.NotReady.selector);
        pool.finish();
        vm.warp(block.timestamp + 7 days);
        pool.finish();
        vm.stopPrank();
        assertEq(usdc.balanceOf(address(pool)), 0);
    }
}

/// An exam that takes any rules and terms: behind a factory, it leaves the factory's own bounds the only ones.
contract TakesAnyRules {
    function check(Rules calldata, ExamTerms calldata) external pure {}
}

/// USDC as Circle's FiatToken V2.2 takes a transfer by signature (EIP-3009), with its own words for
/// a refusal: the parts of it a pool calls, and nothing else.
contract Usdc3009 is ERC20 {
    bytes32 public constant RECEIVE_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 public immutable DOMAIN_SEPARATOR;
    mapping(address => mapping(bytes32 => bool)) public authorizationState;

    constructor() ERC20("USDC", "USDC") {
        DOMAIN_SEPARATOR = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("USDC"),
                keccak256("2"),
                block.chainid,
                address(this)
            )
        );
    }

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    mapping(address => bool) public blocked;
    mapping(address => bool) public saysFalse;

    function saysFalseTo(address who) external {
        saysFalse[who] = true;
    }

    function transfer(address to, uint256 value) public override returns (bool) {
        if (saysFalse[to]) return false; // a token that refuses by its answer, not by a revert
        return super.transfer(to, value);
    }

    function block_(address who) external {
        blocked[who] = true;
    }

    function _update(address from, address to, uint256 value) internal override {
        require(!blocked[to], "Blacklistable: account is blacklisted");
        super._update(from, to, value);
    }

    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        require(to == msg.sender, "FiatTokenV2: caller must be the payee");
        require(block.timestamp > validAfter, "FiatTokenV2: authorization is not yet valid");
        require(block.timestamp < validBefore, "FiatTokenV2: authorization is expired");
        require(!authorizationState[from][nonce], "FiatTokenV2: authorization is used or canceled");
        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                DOMAIN_SEPARATOR,
                keccak256(abi.encode(RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce))
            )
        );
        require(ecrecover(digest, v, r, s) == from, "FiatTokenV2: invalid signature");
        authorizationState[from][nonce] = true;
        _transfer(from, to, value);
    }
}
