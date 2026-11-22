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
import {ACCRUAL_SCALE} from "../../src/sim/SimPoolCodes.sol";
import {SimPool, SimPlan, SimSize, SimTier} from "../../src/sim/SimPool.sol";
import {SimPoolFactory} from "../../src/sim/SimPoolFactory.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
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
    address approver = makeAddr("approver");
    /// The keccak256 of a trading group's code; `_open` sets it with no share.
    bytes32 constant CODE = keccak256("GROUP-ONE");
    address trader = makeAddr("trader");
    address other = makeAddr("other");
    address stranger = makeAddr("stranger");
    /// The factory's operator, our Safe on a deploy: here this contract, which deploys the factory. It alone lifts a
    /// stop; the checker stops.
    address operator = address(this);

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
        factory = new SimPoolFactory(IERC20(address(usdc)), address(new PaperExam()), address(new SimPool()), operator, 1 ether);
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
        p.tiers = new SimTier[](2);
        // As the plan's rules and terms: drawdown 6 %, target 10 %, from the start.
        p.tiers[0] = SimTier({maxDrawdownBps: 600, targetBps: 1000, drawdownKind: 0});
        p.tiers[1] = SimTier({maxDrawdownBps: 600, targetBps: 1000, drawdownKind: 0});
        p.payoutsPerDay = 2;
        // Two payouts of the larger line, both lines on.
        p.locked = 2 * LARGE_PAYOUT;
        p.on = 3;
    }

    function _open(SimPlan memory p) internal returns (SimPool pool) {
        vm.startPrank(creator);
        pool = SimPool(factory.create{value: 1 ether}(p));
        pool.setCode(CODE, partner, 0);
        pool.setApprover(approver);
        vm.stopPrank();
    }

    /// Claims a passed stage's payout and has the approver approve it: paid from its 72 hours.
    function _claim(SimPool pool, address stage) internal returns (uint256 id) {
        id = pool.claimPayout(stage);
        _approve(pool, id);
    }

    function _approve(SimPool pool, uint256 id) internal {
        uint256[] memory ids = new uint256[](1);
        ids[0] = id;
        vm.prank(approver);
        pool.approvePayouts(ids);
    }

    function _share(SimPool pool, uint16 bps) internal {
        vm.prank(creator);
        pool.setCode(CODE, partner, bps);
    }

    /// Sends code `code`'s partner what the code accrued (`claimShares`): anyone may, here a stranger.
    function _takeShares(SimPool pool, bytes32 code) internal {
        bytes32[] memory list = new bytes32[](1);
        list[0] = code;
        vm.prank(stranger);
        pool.claimShares(list);
    }

    /// What code `code` has accrued and not yet sent, its partner's part and its author's together.
    function _due(SimPool pool, bytes32 code) internal view returns (uint256) {
        (uint256 toPartner, uint256 toAuthor) = pool.shareDue(code);
        return toPartner + toAuthor;
    }

    function _partnerOf(SimPool pool, bytes32 code) internal view returns (address p) {
        (p,,,,,,,,,,,) = pool.codeOf(code);
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

    /// The factory makes its stop book with itself and never changes it; every exam it sells takes it; nobody but its
    /// exams adds to it, the pool neither; and a stop set on a sold exam is carried out by a check of the book in the
    /// block of its touch.
    function test_stopBook_isTheFactorysAndEveryExamTakesIt() public {
        StopBook b = factory.stopBook();
        assertTrue(address(b).code.length != 0);
        assertEq(b.factory(), address(factory));
        SimPool pool = _pool();
        PaperExam e = _buy(pool, trader, 0);
        assertEq(e.stopBook(), address(b));
        vm.prank(stranger);
        vm.expectRevert(StopBook.NotExam.selector);
        b.add(BTC, false, true, 1, 1);
        vm.prank(address(pool));
        vm.expectRevert(StopBook.NotExam.selector);
        b.remove(BTC, false);
        _fill(e, trader, true, 100);
        vm.prank(trader);
        e.setTrigger(BTC, false, 950_000, 0);
        vm.roll(block.number + 1);
        _setMark(940_000);
        b.settle(address(e)); // the check logs the touch; the settle carries it out
        assertEq(e.positionOf(BTC), 0);
        _setMark(1_000_000);
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
        stage = _passedStage(pool, _buy(pool, who, line), who);
    }

    /// An exam bought already, passed, funded, passed. A test with several stages buys every exam first, while the
    /// line sells: a running stage keeps its payout out of what is free (`SimPool.free`).
    function _passedStage(SimPool pool, PaperExam exam, address who) internal returns (PaperExam stage) {
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

    /// A factory of another fee, configured as this test's: what `ops/deploy_sim.py deploy` makes on testnet.
    function _factoryOf(uint256 fee) internal returns (SimPoolFactory f) {
        f = new SimPoolFactory(IERC20(address(usdc)), factory.examImpl(), factory.poolImpl(), address(this), fee);
        f.setFiller(filler);
        f.setChecker(checker);
        f.setCloser(closer);
        f.setFeeRecipient(platform);
        f.setAsset(BTC, 1000);
        f.setTermsBook(book);
        vm.prank(creator);
        usdc.approve(address(f), type(uint256).max);
    }

    /// The fee is the factory's, set at its deploy: a testnet factory of 0.1 HYPE takes exactly that, and sends it to
    /// the platform; 1 HYPE, mainnet's fee, is the wrong fee there.
    function test_open_theFeeIsTheOneSetAtTheDeploy() public {
        vm.expectEmit(false, false, false, true);
        emit SimPoolFactory.CreationFeeSet(0, 0.1 ether);
        SimPoolFactory f = _factoryOf(0.1 ether);
        assertEq(f.creationFee(), 0.1 ether, "the fee the deploy named");
        assertEq(factory.creationFee(), 1 ether, "the fee of this test's factory, mainnet's");
        SimPlan memory p = _plan();
        uint256[3] memory wrong = [uint256(1 ether), 0.1 ether - 1, 0.1 ether + 1];
        for (uint256 i = 0; i < wrong.length; ++i) {
            vm.prank(creator);
            vm.expectRevert(SimPoolFactory.WrongFee.selector);
            f.create{value: wrong[i]}(p);
        }
        uint256 hype = platform.balance;
        uint256 held = creator.balance;
        vm.prank(creator);
        address pool = f.create{value: 0.1 ether}(p);
        assertEq(f.poolOf(pool), pool);
        assertEq(platform.balance - hype, 0.1 ether, "0.1 HYPE to the platform");
        assertEq(held - creator.balance, 0.1 ether, "the creator paid the fee and no more");
        assertEq(address(f).balance, 0, "the factory keeps none of it");
    }

    /// A fee of none is refused at the deploy; any fee above it is taken, however large: there is no cap.
    function test_deploy_refusesAFeeOfNone_andHasNoCap() public {
        (address exam, address impl) = (factory.examImpl(), factory.poolImpl());
        vm.expectRevert(SimPoolFactory.BadFee.selector);
        new SimPoolFactory(IERC20(address(usdc)), exam, impl, address(this), 0);
        uint256[4] memory good = [uint256(1), 1 ether, 1_000_000 ether, type(uint256).max];
        for (uint256 i = 0; i < good.length; ++i) {
            SimPoolFactory f = new SimPoolFactory(IERC20(address(usdc)), exam, impl, address(this), good[i]);
            assertEq(f.creationFee(), good[i]);
        }
    }

    /// The operator changes the fee to any amount above zero, with an event of the old and the new; nobody else,
    /// and never to none. A pool opened before keeps what it paid; the next takes the new fee and no other.
    function test_setCreationFee_onlyTheOperator_neverNone() public {
        SimPool before = _pool();
        vm.expectRevert(SimPoolFactory.BadFee.selector);
        factory.setCreationFee(0);
        address[4] memory notOperator = [creator, platform, closer, stranger];
        for (uint256 i = 0; i < notOperator.length; ++i) {
            vm.prank(notOperator[i]);
            vm.expectRevert(SimPoolFactory.NotOperator.selector);
            factory.setCreationFee(2 ether);
        }
        assertEq(factory.creationFee(), 1 ether, "nothing changed it");

        vm.expectEmit(false, false, false, true, address(factory));
        emit SimPoolFactory.CreationFeeSet(1 ether, 2 ether);
        factory.setCreationFee(2 ether);
        assertEq(factory.creationFee(), 2 ether);
        assertEq(factory.poolOf(address(before)), address(before), "the pool opened before is still the factory's");
        assertEq(before.creator(), creator);

        // The fee read before the change: refused, nothing paid. The new one: taken, and all of it to the platform.
        SimPlan memory p = _plan();
        vm.prank(creator);
        vm.expectRevert(SimPoolFactory.WrongFee.selector);
        factory.create{value: 1 ether}(p);
        uint256 hype = platform.balance;
        vm.prank(creator);
        factory.create{value: 2 ether}(p);
        assertEq(platform.balance - hype, 2 ether, "the new fee to the platform");

        // Down as well as up, as far as the operator sets it: any amount above zero, with no cap. A large fee is
        // taken whole by `create` and sent on.
        factory.setCreationFee(1);
        assertEq(factory.creationFee(), 1);
        factory.setCreationFee(type(uint256).max);
        assertEq(factory.creationFee(), type(uint256).max);
        factory.setCreationFee(50 ether);
        vm.deal(creator, 60 ether);
        hype = platform.balance;
        vm.prank(creator);
        factory.create{value: 50 ether}(p);
        assertEq(platform.balance - hype, 50 ether, "a fee of 50 HYPE, taken and sent on");
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
        bad[14].tiers[1].drawdownKind = 2; // neither from the start nor trailing to it
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
        for (uint256 i = 0; i < 5; ++i) {
            assertEq(_partnerOf(pool, keccak256(abi.encode(i))), i == 1 || i == 4 ? address(0) : partner, "the three that took");
        }
    }

    function test_open_needsItsTermsWrittenOut() public {
        SimPlan memory p = _plan();
        p.terms = keccak256("never published");
        vm.prank(creator);
        vm.expectRevert(SimPoolFactory.Unpublished.selector);
        factory.create{value: 1 ether}(p);
    }

    /// At most `MAX_LISTED` assets listed at once, so that the stop book's check of a block fits a small block: a
    /// cap changed on a listed asset takes no place, a delisting frees one.
    function test_listing_atMostMaxListed_aDelistingFreesAPlace() public {
        uint256 cap = factory.MAX_LISTED();
        assertEq(cap, 20, "the cap whose check test_gas_poke_atTheCap measures");
        assertEq(factory.listedAssets(), 1, "BTC");
        for (uint32 a = 1000; factory.listedAssets() < cap; ++a) {
            factory.setAsset(a, 500);
        }
        vm.expectRevert(SimPoolFactory.TooManyAssets.selector);
        factory.setAsset(9999, 500);
        factory.setAsset(BTC, 600);
        assertEq(factory.listedAssets(), cap);
        factory.setAsset(BTC, 0);
        factory.setAsset(BTC, 0);
        assertEq(factory.listedAssets(), cap - 1, "a delisting frees a place, once");
        factory.setAsset(9999, 500);
        assertEq(factory.listedAssets(), cap);
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
            new SimPoolFactory(IERC20(address(usdc)), address(new TakesAnyRules()), address(new SimPool()), address(this), 1 ether);
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
            _signed(pk, address(pool), LARGE_PRICE, block.timestamp + 10 minutes, keccak256(abi.encode(CODE, uint256(1), salt)));
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
            _signed(pk, address(pool), SMALL_PRICE, block.timestamp + 1 hours, keccak256(abi.encode(CODE, uint256(1), salt)));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, block.timestamp + 1 hours, v, r, s);
    }

    function test_buyWithAuthorization_aSignatureBuysOnce_inItsTime_atItsPool() public {
        SimPool pool = _pool();
        (address signer, uint256 pk) = makeAddrAndKey("signer");
        deal(address(usdc), signer, 2 * LARGE_PRICE);
        bytes32 salt = keccak256("one");
        uint256 until = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _signed(pk, address(pool), LARGE_PRICE, until, keccak256(abi.encode(CODE, uint256(1), salt)));
        // Another pool cannot use it: the token takes it only from its payee.
        SimPool other_ = _pool();
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        other_.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        vm.expectRevert(bytes("FiatTokenV2: authorization is used or canceled"));
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        bytes32 later = keccak256("two");
        (v, r, s) = _signed(pk, address(pool), LARGE_PRICE, until, keccak256(abi.encode(CODE, uint256(1), later)));
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
        (uint8 v, bytes32 r, bytes32 s) = _signed(pk, address(pool), LARGE_PRICE, until, keccak256(abi.encode(CODE, uint256(1), salt)));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pool.buyWithAuthorization(signer, 1, theirs, salt, 0, until, v, r, s);
        pool.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s);
        (,,,,, uint256 got,,,,,,) = pool.codeOf(CODE);
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
        SimPoolFactory f = new SimPoolFactory(IERC20(address(plain)), factory.examImpl(), factory.poolImpl(), address(this), 1 ether);
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
        uint256 a = _claim(pool, address(_passedStage(pool, trader, 1)));
        uint256 b = _claim(pool, address(_passedStage(pool, other, 1)));
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
        uint256 id = _claim(pool, address(_passedStage(pool, trader, 1)));
        usdc.saysFalseTo(trader);
        vm.warp(block.timestamp + 72 hours);
        pool.pay(10);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Held));
        assertEq(pool.heldOf(trader), LARGE_PAYOUT);
    }

    function test_pay_neverSpendsWhatIsHeldForAnotherTrader() public {
        SimPool pool = _pool();
        // Every exam bought first, while its line sells.
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        PaperExam bought3 = _buy(pool, stranger, 0);
        _claim(pool, address(_passedStage(pool, bought1, trader))); // A, 800
        _claim(pool, address(_passedStage(pool, bought2, other))); // C, 800
        usdc.block_(trader);
        vm.warp(block.timestamp + 72 hours);
        pool.pay(10); // A's is held, C's is paid
        assertEq(pool.held(), LARGE_PAYOUT);
        assertLt(usdc.balanceOf(address(pool)) - pool.held(), 400e6, "what is free is under a small payout");
        uint256 b = _claim(pool, address(_passedStage(pool, bought3, stranger))); // B, 400
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
        _claim(pool, address(_passedStage(pool, trader, 1)));
        deal(address(usdc), address(pool), 6_000e6);
        assertEq(pool.owed(), LARGE_PAYOUT);
        assertEq(pool.withdrawable(), 200e6, "before the term, the profit only");
        vm.warp(pool.lockedUntil());
        assertEq(pool.withdrawable(), 2_800e6);
        _ask(pool, 2_800e6);
        vm.prank(creator);
        pool.withdraw();
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(partner), 99_500_000, "half of 200 after the platform's part, not of 1 000");
        assertEq(pool.principal(), 2_400e6, "the rest came back as principal");
    }

    function test_finish_leavesWhatIsHeld_andTheTraderTakesItAfter() public {
        SimPool pool = _pool();
        _claim(pool, address(_passedStage(pool, trader, 1))); // the locked 1 600 covers it
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
        _approve(pool, id);
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
        uint256 id = _claim(pool, address(_passedStage(pool, trader, 0)));
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
        // The paired exam is bought first: once the stage runs, the pool has less than two payouts of 800 free.
        paired = address(_buy(pool, other, 1));
        id = _claim(pool, address(_passedStage(pool, trader, 1)));
    }

    function test_stop_theCheckerAloneWithAReasonTheChainKeeps() public {
        SimPool pool = _pool();
        (uint256 id, address paired) = _duePayout(pool);
        address[3] memory notChecker = [creator, stranger, operator];
        for (uint256 i = 0; i < notChecker.length; ++i) {
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

    function test_stop_untilItIsPaid_approvedOrNot_andOnce() public {
        SimPool pool = _pool();
        deal(address(usdc), address(pool), 10_000e6);
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        address paired = address(_buy(pool, other, 1));
        vm.warp(block.timestamp + 72 hours);
        assertFalse(pool.payout(id).approved);
        vm.prank(checker);
        pool.stopPayout(id, 1, paired, _trades(), new bytes32[](0)); // past its 72 hours, not approved, not paid

        (uint256 id2, address paired2) = _duePayout(pool);
        assertTrue(pool.payout(id2).approved);
        vm.warp(block.timestamp + 72 hours);
        vm.prank(checker);
        pool.stopPayout(id2, 1, paired2, _trades(), new bytes32[](0)); // approved, not yet paid
        vm.prank(operator);
        pool.liftStop(id2);
        vm.prank(checker);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.stopPayout(id2, 1, paired2, _trades(), new bytes32[](0)); // once
        assertEq(pool.pay(10), 1);
        (uint256 id3, address paired3) = _duePayout(pool);
        vm.warp(block.timestamp + 72 hours);
        pool.pay(10);
        vm.prank(checker);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.stopPayout(id3, 1, paired3, _trades(), new bytes32[](0)); // paid: too late
    }

    /// Lifting a stop is the factory's operator's alone: the checker that stopped the payout, the creator, the
    /// approver, the closer, the filler and anyone else are refused. Lifted, the payout is due again and paid as any
    /// other.
    function test_liftStop_theOperatorAlone_thePayoutIsDueAgainAndPaidAsAnyOther() public {
        SimPool pool = _pool();
        (uint256 id, address paired) = _duePayout(pool);
        vm.prank(checker);
        pool.stopPayout(id, 1, paired, _trades(), new bytes32[](0));
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 0);
        address[6] memory notOperator = [checker, creator, approver, closer, filler, stranger];
        for (uint256 i = 0; i < notOperator.length; ++i) {
            vm.prank(notOperator[i]);
            vm.expectRevert(SimPoolFactory.NotOperator.selector);
            pool.liftStop(id);
        }
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Stopped), "refused: still stopped");
        vm.expectEmit(address(pool));
        emit SimPool.StopLifted(id, 1, 1, 0);
        vm.prank(operator);
        pool.liftStop(id);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Due), "due again");
        assertEq(pool.owed(), LARGE_PAYOUT);
        assertEq(pool.reserved(), 0);
        uint256 before = usdc.balanceOf(trader);
        assertEq(pool.pay(10), 1, "past its time: paid at once");
        assertEq(usdc.balanceOf(trader) - before, LARGE_PAYOUT);
        vm.prank(operator);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.liftStop(id);
    }

    /// The pool asks the factory for its operator at the lift: once the factory is handed to another key (the Safe,
    /// on a deploy), that key lifts, and the key that was the operator before, and the checker, are refused.
    function test_liftStop_theOperatorTheFactoryNamesNow() public {
        SimPool pool = _pool();
        uint256 id = _claim(pool, address(_passedStage(pool, trader, 1)));
        _stop(pool, id);
        address safe = makeAddr("safe");
        factory.setOperator(safe);
        address[2] memory notOperator = [operator, checker];
        for (uint256 i = 0; i < notOperator.length; ++i) {
            vm.prank(notOperator[i]);
            vm.expectRevert(SimPoolFactory.NotOperator.selector);
            pool.liftStop(id);
        }
        vm.prank(safe);
        pool.liftStop(id);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Due));
        assertEq(pool.owed(), LARGE_PAYOUT);
        assertEq(pool.reserved(), 0);
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
        uint256 id = _claim(pool, address(_passedStage(pool, trader, 1)));
        _stop(pool, id);
        uint256 bal = usdc.balanceOf(address(pool));
        assertEq(pool.owed(), 0);
        assertEq(pool.reserved(), LARGE_PAYOUT);
        assertEq(pool.withdrawable(), bal - 3 * LARGE_PAYOUT - LARGE_PAYOUT, "a stop's money is kept for 14 days");
        vm.warp(block.timestamp + 14 days - 1);
        vm.expectRevert(SimPool.ReserveRunning.selector);
        pool.finalizeStop(id);
        vm.warp(block.timestamp + 1);
        vm.prank(operator);
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
        vm.prank(operator);
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
        uint256 id = _claim(pool, address(_passedStage(pool, trader, 1)));
        _stop(pool, id);
        deal(address(usdc), address(pool), 6_000e6);
        assertEq(pool.owed(), 0);
        assertEq(pool.withdrawable(), 200e6, "before the term, the profit only");
        vm.warp(pool.lockedUntil());
        assertEq(pool.withdrawable(), 2_800e6);
        _ask(pool, 2_800e6);
        vm.prank(creator);
        pool.withdraw();
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(partner), 99_500_000, "half of 200 after the platform's part, not of 1 000");
        assertEq(pool.principal(), 2_400e6);
    }

    function test_stopReserve_payNeverSpendsIt_soALiftedStopFindsItsMoney() public {
        SimPool pool = _pool();
        // Every exam bought first, while its line sells.
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        PaperExam bought3 = _buy(pool, stranger, 1);
        uint256 a = _claim(pool, address(_passedStage(pool, bought1, trader)));
        uint256 b = _claim(pool, address(_passedStage(pool, bought2, other)));
        uint256 c = _claim(pool, address(_passedStage(pool, bought3, stranger)));
        _stop(pool, a);
        // 1 600 + 3 x 84.575 in the pool, 800 of it kept for A's stop.
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 1, "B is paid; C waits, A's kept money is not C's");
        assertEq(uint8(pool.payout(b).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(c).state), uint8(SimPool.PayoutState.Due));
        vm.warp(block.timestamp + 10 days);
        vm.prank(operator);
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
        // Every exam bought first, while its line sells.
        PaperExam[4] memory bought;
        for (uint256 i = 0; i < 4; ++i) bought[i] = _buy(pool, who[i], 1);
        for (uint256 i = 0; i < 4; ++i) id[i] = _claim(pool, address(_passedStage(pool, bought[i], who[i])));
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
        // Every exam bought first, while its line sells.
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        uint256 a = _claim(pool, address(_passedStage(pool, bought1, trader)));
        uint256 b = _claim(pool, address(_passedStage(pool, bought2, other)));
        _stop(pool, a);
        _stop(pool, b);
        assertEq(pool.payout(a).stoppedAt, block.timestamp);
        vm.warp(block.timestamp + 14 days - 1);
        vm.startPrank(operator);
        pool.liftStop(a);
        vm.warp(block.timestamp + 1);
        vm.expectRevert(SimPool.ReserveOver.selector);
        pool.liftStop(b);
        vm.stopPrank();
    }

    function test_stopReserve_theTraderObjectsOnChain_withinTheFourteenDays() public {
        SimPool pool = _pool();
        uint256 id = _claim(pool, address(_passedStage(pool, trader, 1)));
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
        uint256 id = _claim(pool, address(_passedStage(pool, trader, 1)));
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
        // Every exam bought first, while its line sells.
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        PaperExam bought3 = _buy(pool, stranger, 1);
        // Three passes on 1 600 locked and three prices: the third payout waits.
        uint256 a = _claim(pool, address(_passedStage(pool, bought1, trader)));
        uint256 b = _claim(pool, address(_passedStage(pool, bought2, other)));
        uint256 c = _claim(pool, address(_passedStage(pool, bought3, stranger)));
        assertEq(pool.withdrawable(), 0);
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(1), 1);
        // A day on, so that the daily limit is not what holds the third: the money is.
        vm.warp(block.timestamp + 24 hours);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutQueued(c, 1_600e6 + 3 * (LARGE_PRICE - 425_000) - 2 * LARGE_PAYOUT);
        assertEq(pool.pay(10), 1);
        assertEq(uint8(pool.payout(a).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(b).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(c).state), uint8(SimPool.PayoutState.Due));
        assertEq(pool.head(), c);
        vm.prank(creator);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.requestWithdraw(1);
        // Nothing is free: no line sells while the queue waits (16 November), so no new price can pay it, nor be
        // promised to a new buyer. The creator's money pays it.
        deal(address(usdc), stranger, 100e6);
        vm.startPrank(stranger);
        usdc.approve(address(pool), 100e6);
        vm.expectRevert(abi.encodeWithSelector(SimPool.NotCovered.selector, 2 * uint256(LARGE_PAYOUT)));
        pool.buy(1, CODE);
        vm.stopPrank();
        vm.startPrank(creator);
        usdc.approve(address(pool), LARGE_PAYOUT);
        pool.addToPot(LARGE_PAYOUT);
        vm.stopPrank();
        assertEq(pool.pay(10), 1);
        assertEq(pool.owed(), 0);
    }

    // ── the daily limit ──────────────────────────────────────────

    /// `n` payouts claimed now, each by a trader of its own, the pool holding the money for all of them;
    /// `approved`: every one approved.
    function _wave(SimPool pool, uint256 n, bool approved) internal returns (address[] memory traders) {
        uint256 first = pool.payoutCount();
        traders = _claimedPayouts(pool, n);
        deal(address(usdc), address(pool), usdc.balanceOf(address(pool)) + n * uint256(LARGE_PAYOUT));
        if (!approved) return traders;
        uint256[] memory ids = new uint256[](n);
        for (uint256 k = 0; k < n; ++k) {
            ids[k] = first + k;
        }
        vm.prank(approver);
        pool.approvePayouts(ids);
    }

    function test_dailyLimit_isTwoPayoutsInTwentyFourHours() public {
        assertEq(_pool().payoutsPerDay(), 2);
        assertEq(factory.PAYOUT_DAY(), 24 hours);
    }

    function test_dailyLimit_aWaveOfFiveIsPaidTwoTwoOne() public {
        SimPool pool = _pool();
        address[] memory traders = _wave(pool, 5, true);
        uint256 t0 = block.timestamp + 72 hours;
        vm.warp(t0);
        assertEq(pool.payRoom(), 2);
        assertEq(pool.nextPayAt(), 0, "nothing settled yet");
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(2, uint64(t0 + 24 hours));
        assertEq(pool.pay(10), 2, "day 3: two");
        assertEq(pool.payRoom(), 0);
        assertEq(pool.nextPayAt(), t0 + 24 hours);
        assertEq(pool.head(), 2, "the third keeps its place");
        assertEq(pool.owed(), 3 * uint256(LARGE_PAYOUT), "and stays owed");
        // A second before the day is over: nothing, and the same event.
        vm.warp(t0 + 24 hours - 1);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(2, uint64(t0 + 24 hours));
        assertEq(pool.pay(10), 0);
        vm.warp(t0 + 24 hours);
        assertEq(pool.payRoom(), 2);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(4, uint64(t0 + 48 hours));
        assertEq(pool.pay(10), 2, "day 4: two");
        vm.warp(t0 + 48 hours);
        assertEq(pool.pay(10), 1, "day 5: one");
        assertEq(pool.owed(), 0);
        assertEq(pool.paidCount(), 5);
        assertEq(pool.paidTotal(), 5 * uint256(LARGE_PAYOUT));
        for (uint256 k = 0; k < 5; ++k) {
            assertEq(usdc.balanceOf(traders[k]), LARGE_PAYOUT);
        }
    }

    // ── the menu: tiers line by line, and the pool's own daily limit ──

    /// The CFO's menu of 13 November: loss limit / target on 5 000
    /// and 10 000, trailing to the start, at the market's prices. Two lines share 40.
    function _menuPlan() internal view returns (SimPlan memory p) {
        p = _plan();
        uint16[4] memory dd = [uint16(300), 400, 500, 600];
        uint16[4] memory target = [uint16(900), 1000, 1000, 1000];
        uint64[8] memory price = [uint64(20e6), 40e6, 30e6, 60e6, 40e6, 79e6, 50e6, 95e6];
        p.menu = new SimSize[](8);
        p.tiers = new SimTier[](8);
        for (uint256 i = 0; i < 8; ++i) {
            p.menu[i] = SimSize({size: i % 2 == 0 ? SMALL : LARGE, price: price[i]});
            p.tiers[i] = SimTier({maxDrawdownBps: dd[i / 2], targetBps: target[i / 2], drawdownKind: 1});
        }
        p.on = 0xff;
    }

    function test_menu_eachLineStartsBothStagesOnItsTier() public {
        SimPool pool = _open(_menuPlan());
        // Money for every line to sell as the payouts are claimed one after another.
        deal(address(usdc), address(pool), 100_000e6);
        SimTier[] memory k = pool.tiers();
        assertEq(k.length, 8);
        for (uint256 i = 0; i < 8; ++i) {
            address who = makeAddr(string(abi.encode("tier trader", i)));
            PaperExam exam = _buy(pool, who, i);
            for (uint256 s = 0; s < 2; ++s) {
                assertEq(exam.rules().maxDrawdownBps, k[i].maxDrawdownBps, "the tier's loss limit");
                assertEq(exam.rules().dailyLossBps, 300, "the plan's daily loss");
                assertEq(exam.terms().targetsBps.length, 1);
                assertEq(exam.terms().targetsBps[0], k[i].targetBps, "the tier's target");
                assertEq(exam.terms().capital, i % 2 == 0 ? SMALL : LARGE, "the line's size");
                assertEq(exam.drawdownKind(), 1, "trailing to the start");
                if (s == 1) break;
                _pass(exam, who);
                vm.prank(who);
                exam = PaperExam(pool.fund(address(exam)));
            }
            _pass(exam, who);
            uint256 id = pool.claimPayout(address(exam));
            assertEq(pool.payout(id).amount, i % 2 == 0 ? SMALL_PAYOUT : LARGE_PAYOUT, "8 % of the line's size");
            _setMark(1_000_000);
        }
        assertEq(k[0].maxDrawdownBps, 300);
        assertEq(k[0].targetBps, 900);
        assertEq(k[7].maxDrawdownBps, 600);
        assertEq(k[7].targetBps, 1000);
        assertEq(pool.largestPayout(), LARGE_PAYOUT);
    }

    function test_menu_theTiersLossLimitEndsTheExam_notThePlans() public {
        SimPlan memory p = _menuPlan();
        p.examRules.dailyLossBps = 900; // out of the way: the loss limit alone is judged
        SimPool pool = _open(p);
        PaperExam tight = _buy(pool, trader, 0); // 3 %
        PaperExam loose = _buy(pool, other, 6); // 6 %
        (,, uint64 capital,) = tight.result();
        _fill(tight, trader, true, capital / mark);
        _fill(loose, other, true, capital / mark);
        _setMark(mark * 96 / 100); // 4 % down: past 3, short of 6
        tight.breach();
        (PaperExam.Status s1,,,) = tight.result();
        assertEq(uint8(s1), uint8(PaperExam.Status.Breached));
        vm.expectRevert(PaperExam.NoBreach.selector);
        loose.breach();
    }

    function test_menu_twoLinesAtOnePrice_aSignatureBuysTheLineItNames() public {
        SimPool pool = _open(_menuPlan());
        (address signer, uint256 pk) = makeAddrAndKey("signer");
        deal(address(usdc), signer, 40e6);
        bytes32 salt = keccak256("one");
        uint256 until = block.timestamp + 1 hours;
        // Signed for line 1 (3 % on 10 000), sent for line 4 (5 % on 5 000) at the same 40.
        (uint8 v, bytes32 r, bytes32 s) =
            _signed(pk, address(pool), 40e6, until, keccak256(abi.encode(CODE, uint256(1), salt)));
        vm.prank(stranger);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pool.buyWithAuthorization(signer, 4, CODE, salt, 0, until, v, r, s);
        vm.prank(stranger);
        PaperExam e = PaperExam(pool.buyWithAuthorization(signer, 1, CODE, salt, 0, until, v, r, s));
        assertEq(e.rules().maxDrawdownBps, 300, "the line signed for");
        assertEq(e.terms().capital, LARGE);
    }

    function test_open_refusesTiersThatAreNotOneForEachLine_orThatAnExamWouldRefuse() public {
        SimPlan[] memory bad = new SimPlan[](5);
        for (uint256 i = 0; i < bad.length; ++i) bad[i] = _menuPlan();
        bad[0].tiers = new SimTier[](7); // one line without a tier
        bad[1].tiers = new SimTier[](9); // a tier without a line
        for (uint256 i = 0; i < 9; ++i) bad[1].tiers[i] = SimTier({maxDrawdownBps: 600, targetBps: 1000, drawdownKind: 0});
        for (uint256 i = 0; i < 7; ++i) bad[0].tiers[i] = SimTier({maxDrawdownBps: 600, targetBps: 1000, drawdownKind: 0});
        bad[2].tiers[5].drawdownKind = 2;
        bad[3].tiers[3].targetBps = 0; // a step with no target: PaperExam refuses it
        bad[4].tiers[6].maxDrawdownBps = 10_000; // a loss limit of the whole size: PaperExam refuses it
        for (uint256 i = 0; i < bad.length; ++i) {
            vm.prank(creator);
            vm.expectRevert(SimPoolFactory.BadPlan.selector);
            factory.create{value: 1 ether}(bad[i]);
        }
    }

    function test_open_payoutsPerDayIsWithinTheFactorysBounds() public {
        assertEq(factory.MIN_PAYOUTS_PER_DAY(), 1);
        assertEq(factory.MAX_PAYOUTS_PER_DAY(), 8);
        uint8[4] memory n = [uint8(0), 1, 8, 9];
        for (uint256 i = 0; i < 4; ++i) {
            SimPlan memory p = _plan();
            p.payoutsPerDay = n[i];
            vm.prank(creator);
            if (i == 0 || i == 3) {
                vm.expectRevert(SimPoolFactory.BadPlan.selector);
                factory.create{value: 1 ether}(p);
            } else {
                SimPool pool = SimPool(factory.create{value: 1 ether}(p));
                assertEq(pool.payoutsPerDay(), n[i]);
                assertEq(pool.payRoom(), n[i], "a new pool has the whole day");
            }
        }
    }

    function test_dailyLimit_aPoolOfThreeADay_paysAWaveOfSevenThreeThreeOne() public {
        SimPlan memory p = _plan();
        p.payoutsPerDay = 3;
        SimPool pool = _open(p);
        _wave(pool, 7, true);
        uint256 t0 = block.timestamp + 72 hours;
        vm.warp(t0);
        assertEq(pool.payRoom(), 3);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(3, uint64(t0 + 24 hours));
        assertEq(pool.pay(10), 3, "day 3: three");
        assertEq(pool.payRoom(), 0);
        assertEq(pool.nextPayAt(), t0 + 24 hours);
        vm.warp(t0 + 12 hours);
        assertEq(pool.pay(10), 0, "the day is not over");
        vm.warp(t0 + 24 hours);
        assertEq(pool.pay(10), 3, "day 4: three");
        vm.warp(t0 + 48 hours);
        assertEq(pool.pay(10), 1, "day 5: one");
        assertEq(pool.paidCount(), 7);
        assertEq(pool.owed(), 0);
    }

    function test_dailyLimit_aPoolOfOneADay_paysOneADay() public {
        SimPlan memory p = _plan();
        p.payoutsPerDay = 1;
        SimPool pool = _open(p);
        _wave(pool, 2, true);
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 1);
        assertEq(pool.payRoom(), 0);
        vm.warp(block.timestamp + 24 hours - 1);
        assertEq(pool.pay(10), 0);
        vm.warp(block.timestamp + 1);
        assertEq(pool.payRoom(), 1);
        assertEq(pool.pay(10), 1);
    }

    function test_dailyLimit_isAnyTwentyFourHours_notACalendarDay() public {
        SimPool pool = _pool();
        _wave(pool, 4, true);
        uint256 t0 = block.timestamp + 72 hours;
        vm.warp(t0);
        assertEq(pool.pay(1), 1);
        vm.warp(t0 + 10 hours);
        assertEq(pool.payRoom(), 1);
        assertEq(pool.pay(1), 1);
        assertEq(pool.payRoom(), 0);
        assertEq(pool.nextPayAt(), t0 + 24 hours, "when the first of the two is a day old");
        assertEq(pool.pay(10), 0);
        // The first is a day old: room for one, not for two, since the second was paid ten hours later.
        vm.warp(t0 + 24 hours);
        assertEq(pool.payRoom(), 1);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(3, uint64(t0 + 34 hours));
        assertEq(pool.pay(10), 1);
        vm.warp(t0 + 34 hours - 1);
        assertEq(pool.pay(10), 0);
        vm.warp(t0 + 34 hours);
        assertEq(pool.pay(10), 1);
        assertEq(pool.owed(), 0);
    }

    function test_dailyLimit_countsThePayoutsThatGoOutAtSevenDaysToo() public {
        // None approved: at their 7 days all three may be paid, and two are. Without this the limit would be
        // passed by not approving.
        SimPool pool = _pool();
        _wave(pool, 3, false);
        uint256 t7 = block.timestamp + 7 days;
        vm.warp(t7);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(2, uint64(t7 + 24 hours));
        assertEq(pool.pay(10), 2);
        assertEq(uint8(pool.payout(2).state), uint8(SimPool.PayoutState.Due), "past its 7 days, and waiting for the day");
        vm.warp(t7 + 24 hours);
        assertEq(pool.pay(10), 1, "8 days after its claim");
    }

    function test_dailyLimit_countsAPayoutSetAsideForItsTrader_andNotItsClaim() public {
        SimPool pool = _pool();
        address[] memory traders = _wave(pool, 3, true);
        usdc.block_(traders[0]);
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 2);
        assertEq(uint8(pool.payout(0).state), uint8(SimPool.PayoutState.Held), "refused by the token: set aside");
        assertEq(uint8(pool.payout(1).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(2).state), uint8(SimPool.PayoutState.Due), "the held one took a place of the day");
        assertEq(pool.payRoom(), 0);
        uint64 next = pool.nextPayAt();
        // Taking what was set aside is no payout of the day: it goes out at once and changes nothing of the limit.
        vm.prank(traders[0]);
        pool.claimHeld(other);
        assertEq(usdc.balanceOf(other), LARGE_PAYOUT);
        assertEq(pool.payRoom(), 0);
        assertEq(pool.nextPayAt(), next);
        assertEq(pool.pay(10), 0);
    }

    function test_dailyLimit_aPayoutPassedOverForItsApprovalTakesNoPlaceOfTheDay() public {
        SimPool pool = _pool();
        _wave(pool, 4, false);
        uint256[] memory ids = new uint256[](3);
        (ids[0], ids[1], ids[2]) = (1, 2, 3);
        vm.prank(approver);
        pool.approvePayouts(ids);
        uint256 t0 = block.timestamp + 72 hours;
        vm.warp(t0);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(3, uint64(t0 + 24 hours));
        assertEq(pool.pay(10), 2, "the two behind the waiting head");
        assertEq(uint8(pool.payout(0).state), uint8(SimPool.PayoutState.Due));
        assertEq(pool.head(), 0);
    }

    function test_dailyLimit_isSaidBeforeTheMoney() public {
        // The third payout has neither a place in the day nor its money: the event names the limit.
        SimPool pool = _pool();
        // Every exam bought first, while its line sells.
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        PaperExam bought3 = _buy(pool, stranger, 1);
        _claim(pool, address(_passedStage(pool, bought1, trader)));
        _claim(pool, address(_passedStage(pool, bought2, other)));
        uint256 c = _claim(pool, address(_passedStage(pool, bought3, stranger)));
        uint256 t0 = block.timestamp + 72 hours;
        vm.warp(t0);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(c, uint64(t0 + 24 hours));
        assertEq(pool.pay(10), 2);
    }

    function test_dailyLimit_finishIsNoPayoutOfTheDay() public {
        SimPool pool = _pool();
        _wave(pool, 2, true);
        vm.prank(closer);
        pool.close();
        vm.warp(block.timestamp + 7 days);
        assertEq(pool.pay(10), 2);
        assertEq(pool.payRoom(), 0, "two payouts today");
        uint256 left = usdc.balanceOf(address(pool));
        assertGt(left, 0);
        uint256 before = usdc.balanceOf(creator);
        vm.prank(creator);
        pool.finish();
        assertEq(usdc.balanceOf(address(pool)), 0, "everything left goes out the same day");
        assertGt(usdc.balanceOf(creator), before);
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
        _claim(pool, address(_passedStage(pool, trader, 1)));
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
        // Kept for the code: its half, rounded up to the unit above (1 000 less 5 is 995, sold by one code).
        vm.expectEmit(address(pool));
        emit SimPool.Withdrawn(creator, 1_000e6, 5e6, 497_500_001, true);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(partner), 0, "nothing sent within the withdrawal: the share is accrued");
        assertEq(_due(pool, CODE), 497_500_000);
        assertEq(pool.sharesDue(), 497_500_001);
        assertEq(usdc.balanceOf(creator) - before, 497_499_999, "the creator's part less the unit kept for no one");
        vm.expectEmit(address(pool));
        emit SimPool.SharePaid(CODE, partner, 497_500_000, true);
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(partner), 497_500_000, "half of what is left after the platform's part");
        assertEq(pool.sharesDue(), 1, "the unit of rounding stays in the pool, for no one");
        assertEq(_due(pool, CODE), 0);
        (,,,,,,,,, uint256 got,,) = pool.codeOf(CODE);
        assertEq(got, 497_500_000, "what the code got, in all");
        _takeShares(pool, CODE); // a second claim sends nothing
        assertEq(usdc.balanceOf(partner), 497_500_000);
        assertEq(pool.principal(), 1_600e6, "all of it was profit: the principal stays");
    }

    function test_setCode_onlyTheCreator_changeableUntilASaleNamesIt() public {
        SimPool pool = _pool();
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.setCode(CODE, stranger, 5000);
        vm.expectEmit(address(pool));
        emit SimPool.CodeSet(CODE, partner, 6000, address(0), 0);
        _share(pool, 6000); // before any sale: changed
        (address who, uint16 bps,,,,,,,,,,) = pool.codeOf(CODE);
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
        assertEq(_partnerOf(pool, CODE), partner);
    }

    function test_setCode_aUsedCodesShareOnlyRises_forTheSamePartner_withinTheBound() public {
        SimPool pool = _pool();
        _share(pool, 4000);
        _buy(pool, trader, 0);
        vm.expectEmit(address(pool));
        emit SimPool.CodeSet(CODE, partner, 5000, address(0), 0);
        _share(pool, 5000); // the owner's next step: raised
        (address who, uint16 bps, bool used,,,,,,,,,) = pool.codeOf(CODE);
        assertEq(who, partner);
        assertEq(bps, 5000);
        assertTrue(used);
        vm.startPrank(creator);
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setCode(CODE, partner, 5000); // not higher
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setCode(CODE, partner, 4500); // lower
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setCode(CODE, creator, 6000); // another partner, even with a higher share
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setCode(CODE, partner, 6001); // past the platform's bound
        pool.setCode(CODE, partner, 6000);
        vm.stopPrank();
        (, bps,,,,,,,,,,) = pool.codeOf(CODE);
        assertEq(bps, 6000);

        // A used code with no share: raised only into the bounds.
        bytes32 none = keccak256("NO-SHARE");
        vm.prank(creator);
        pool.setCode(none, makeAddr("group"), 0);
        _buyWith(pool, other, 0, none);
        vm.startPrank(creator);
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setCode(none, makeAddr("group"), 3000);
        pool.setCode(none, makeAddr("group"), 4000);
        vm.stopPrank();
    }

    function test_withdraw_aRaiseCountsFromTheNextWithdrawal_andNoOtherCodesPart() public {
        // Codes A (40 %) and B (50 %), one large sale each: each brought half of all.
        SimPool pool = _pool();
        _share(pool, 4000);
        address ownerB = makeAddr("owner B");
        bytes32 codeB = keccak256("GROUP-TWO");
        vm.prank(creator);
        pool.setCode(codeB, ownerB, 5000);
        _buy(pool, trader, 1);
        _buyWith(pool, other, 1, codeB);
        deal(address(usdc), address(pool), 6_000e6);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        _takeShares(pool, CODE);
        _takeShares(pool, codeB);
        uint256 net = 1_000e6 - 5e6;
        assertEq(usdc.balanceOf(partner), net / 2 * 4000 / 10_000, "A: 40 % of half");
        assertEq(usdc.balanceOf(ownerB), net / 2 * 5000 / 10_000, "B: 50 % of half");
        _share(pool, 6000); // A raised
        assertEq(usdc.balanceOf(partner), net / 2 * 4000 / 10_000, "what was paid is not recomputed");
        uint256 a0 = usdc.balanceOf(partner);
        uint256 b0 = usdc.balanceOf(ownerB);
        deal(address(usdc), address(pool), usdc.balanceOf(address(pool)) + 1_000e6);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        _takeShares(pool, CODE);
        _takeShares(pool, codeB);
        assertEq(usdc.balanceOf(partner) - a0, net / 2 * 6000 / 10_000, "A: 60 % from the raise on");
        assertEq(usdc.balanceOf(ownerB) - b0, net / 2 * 5000 / 10_000, "B: the same 50 %, A's raise takes none of it");
        assertLe(usdc.balanceOf(partner) - a0 + usdc.balanceOf(ownerB) - b0, net * 6000 / 10_000, "all partners: 60 % at most");
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
        assertEq(_partnerOf(pool, CODE), partner, "set again, on sale again");
        _buy(pool, trader, 0);
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
        vm.prank(creator);
        pool.withdraw();
        uint256 kept = pool.sharesDue();
        assertEq(_due(pool, CODE), 895_500_000);
        assertEq(_due(pool, codeB), 358_200_000);
        assertEq(_due(pool, codeC), 0, "no share, nothing accrued");
        bytes32[] memory all3 = new bytes32[](3);
        (all3[0], all3[1], all3[2]) = (CODE, codeB, codeC);
        vm.expectEmit(address(pool));
        emit SimPool.SharePaid(CODE, partner, 895_500_000, true);
        vm.expectEmit(address(pool));
        emit SimPool.SharePaid(codeB, ownerB, 358_200_000, true);
        vm.prank(stranger); // anyone, for any codes: the money goes to each code's partner
        pool.claimShares(all3);
        // 3 600 less the platform's 18 is 3 582: A brought half of it all, B a quarter, C a quarter.
        assertEq(usdc.balanceOf(partner), 895_500_000, "50 % of A's half");
        assertEq(usdc.balanceOf(ownerB), 358_200_000, "40 % of B's quarter");
        assertEq(usdc.balanceOf(stranger), 0, "who sends it gets nothing");
        assertLe(kept - 895_500_000 - 358_200_000, 1, "kept for the codes: theirs, and a unit at most");
        assertEq(usdc.balanceOf(creator) - before, 3_582e6 - kept, "C's quarter stays the creator's");
        assertEq(pool.principal(), 1_600e6, "all of it was profit");
    }

    /// The number of codes `test_codes_twoThousand_*` sets, each with a share and a sale of its own.
    uint256 constant MANY_CODES = 2_000;

    /// A pool with `MANY_CODES` codes, each its own partner, a share of 40 % and one sale of line 1; the setup is not
    /// metered.
    function _manyCodes() internal returns (SimPool pool) {
        vm.pauseGasMetering();
        pool = _pool();
        vm.startPrank(creator);
        for (uint256 i = 0; i < MANY_CODES; ++i) {
            pool.setCode(keccak256(abi.encode("group", i)), makeAddr(string(abi.encode("owner", i))), 4000);
        }
        vm.stopPrank();
        for (uint256 i = 0; i < MANY_CODES; ++i) {
            _manyExams.push(_buyWith(pool, makeAddr(string(abi.encode("buyer", i))), 1, keccak256(abi.encode("group", i))));
        }
        vm.resumeGasMetering();
    }

    PaperExam[] internal _manyExams;

    /// The pool, the token, the factory and the pool's code cold, as a transaction of its own finds them.
    function _coolPool(SimPool pool) internal {
        vm.cool(address(pool));
        vm.cool(address(usdc));
        vm.cool(address(factory));
        vm.cool(factory.poolImpl());
    }

    /// Every exam of `_manyExams` past its time, ended and released.
    function _releaseAll(SimPool pool) internal {
        for (uint256 i = 0; i < _manyExams.length; ++i) {
            _manyExams[i].expire();
            pool.release(address(_manyExams[i]));
        }
    }

    function test_codes_twoThousand_noLimit_withdrawFinishAndAPurchaseFitTheGate() public {
        // No limit on codes (Alex, 16 November): a withdrawal accrues every code's share by one step, and no call
        // goes through all the codes. With 2 000 codes sharing, a purchase, a withdrawal and `finish` each stay
        // within two thirds of a small block, as with one.
        SimPool pool = _manyCodes();
        _coolPool(pool);
        uint256 g = gasleft();
        PaperExam bought = _buyWith(pool, trader, 1, keccak256(abi.encode("group", MANY_CODES - 1)));
        g -= gasleft();
        _manyExams.push(bought);
        emit log_named_uint("buy with 2 000 codes (with deal and approve)", g);
        assertLe(g, CREATE_GAS_GATE, "a purchase");
        vm.pauseGasMetering();
        _ask(pool, pool.withdrawable());
        _coolPool(pool);
        vm.resumeGasMetering();
        vm.prank(creator);
        g = gasleft();
        pool.withdraw();
        g -= gasleft();
        emit log_named_uint("withdraw with 2 000 codes sharing", g);
        assertLe(g, 200_000, "a withdrawal: no loop over the codes");
        assertGt(pool.sharesDue(), 0);
        // Two more withdrawals before the pool closes.
        vm.pauseGasMetering();
        for (uint256 k = 0; k < 2; ++k) {
            deal(address(usdc), address(pool), usdc.balanceOf(address(pool)) + 1_000_000);
            _ask(pool, 1_000_000);
            vm.prank(creator);
            pool.withdraw();
        }
        vm.resumeGasMetering();
        vm.pauseGasMetering();
        vm.warp(pool.lockedUntil());
        vm.prank(creator);
        pool.close();
        // Every exam past its time and released: none is live when the pool finishes.
        vm.warp(block.timestamp + 400 days);
        assertEq(pool.live(), MANY_CODES + 1, "every exam still to be released");
        _releaseAll(pool);
        uint256 before = usdc.balanceOf(creator);
        _coolPool(pool);
        vm.resumeGasMetering();
        vm.prank(creator);
        g = gasleft();
        pool.finish();
        g -= gasleft();
        assertTrue(pool.finished());
        assertGt(usdc.balanceOf(creator), before, "finish sent the creator the rest");
        emit log_named_uint("finish with 2 000 codes sharing", g);
        assertLe(g, 200_000, "finish: no loop over the codes");
        // One code's partner takes its share by itself, at the gas of one code.
        bytes32 last = keccak256(abi.encode("group", MANY_CODES - 1));
        uint256 due = _due(pool, last);
        assertGt(due, 0);
        bytes32[] memory one = new bytes32[](1);
        one[0] = last;
        _coolPool(pool);
        g = gasleft();
        pool.claimShares(one);
        g -= gasleft();
        emit log_named_uint("claimShares, one code", g);
        assertLe(g, 200_000, "one code's claim: the gas of one code");
        assertEq(usdc.balanceOf(makeAddr(string(abi.encode("owner", MANY_CODES - 1)))), due);
        // Every code takes its own, 200 codes a call: what stays kept is the codes' fractions and the rounding up.
        vm.pauseGasMetering();
        uint256 gotAll = due;
        for (uint256 from = 0; from < MANY_CODES; from += 200) {
            bytes32[] memory batch = new bytes32[](200);
            for (uint256 i = 0; i < 200; ++i) batch[i] = keccak256(abi.encode("group", from + i));
            uint256 before = usdc.balanceOf(address(pool));
            pool.claimShares(batch);
            gotAll += before - usdc.balanceOf(address(pool));
        }
        vm.resumeGasMetering();
        uint256 left = pool.sharesDue();
        emit log_named_uint("kept for the codes after all 2 000 took their own, in units", left);
        assertEq(left, (pool.accruedScaled() + ACCRUAL_SCALE - 1) / ACCRUAL_SCALE - gotAll, "all accrued, less all sent");
        assertLe(left, MANY_CODES, "less than a unit a code with a share, and one more");
        assertEq(usdc.balanceOf(address(pool)), pool.held() + left, "and nothing else stays in the finished pool");
    }

    /// `n` payouts claimed in one block, each to a trader of its own with no USDC, none approved: all wait.
    /// `n` payouts of line 1 claimed. Every exam is bought first, while the line sells (`COVER_PAYOUTS` of its
    /// payouts free: running exams are not counted), and passed after: so the payouts can come to more than the pool
    /// holds, as they can when many exams pass at once.
    function _claimedPayouts(SimPool pool, uint256 n) internal returns (address[] memory traders) {
        traders = new address[](n);
        PaperExam[] memory exams = new PaperExam[](n);
        for (uint256 i = 0; i < n; ++i) {
            traders[i] = makeAddr(string(abi.encode("queued trader", i)));
            exams[i] = _buy(pool, traders[i], 1);
        }
        for (uint256 i = 0; i < n; ++i) {
            _pass(exams[i], traders[i]);
            vm.prank(traders[i]);
            PaperExam stage = PaperExam(pool.fund(address(exams[i])));
            _pass(stage, traders[i]);
            _setMark(1_000_000);
            pool.claimPayout(address(stage));
        }
    }

    /// What `ops/sim_keeper.py` asks of one `pay` at most: PAY_PER_CALL payouts paid, all the daily limit lets
    /// one call settle, and SCAN_PAYOUTS looked at (its test holds its numbers to these two).
    uint256 constant KEEPER_PAYS = 2;
    uint256 constant KEEPER_SCAN = 192;
    /// A transaction's own gas and the call's 36 bytes, which `gasleft` around the call does not see.
    uint256 constant PAY_INTRINSIC = 21_300;

    /// One `pay(looked)` from cold storage, as a transaction of its own finds it: the last KEEPER_PAYS of the
    /// `looked` payouts approved and due, every one before them waiting for its approval. `refused` of the
    /// payable ones are refused by the token, by a revert and by its answer in turn.
    function _payGas(SimPool pool, address[] memory traders, uint256 looked, uint256 refused) internal returns (uint256 used) {
        uint256 snap = vm.snapshotState();
        uint256[] memory ids = new uint256[](KEEPER_PAYS);
        for (uint256 k = 0; k < KEEPER_PAYS; ++k) {
            ids[k] = looked - KEEPER_PAYS + k;
        }
        vm.prank(approver);
        pool.approvePayouts(ids);
        for (uint256 k = 0; k < refused; ++k) {
            address t = traders[looked - 1 - k];
            if (k % 2 == 0) usdc.block_(t);
            else usdc.saysFalseTo(t);
        }
        vm.cool(address(pool));
        vm.cool(address(usdc));
        vm.cool(address(factory));
        vm.cool(factory.poolImpl());
        uint256 before = gasleft();
        uint256 n = pool.pay(looked);
        used = before - gasleft() + PAY_INTRINSIC;
        assertEq(n, KEEPER_PAYS, "every payable payout settled in the one call");
        assertEq(pool.paidCount(), KEEPER_PAYS - refused, "the refused ones are held, the rest paid");
        assertEq(pool.held(), refused * uint256(LARGE_PAYOUT));
        assertEq(pool.head(), 0, "the head still waits for its approval");
        vm.revertToState(snap);
    }

    function test_pay_theKeepersFullBatch_fitsASmallBlock() public {
        // The costliest call the keeper sends, measured and not estimated (review of #286): the two payouts of
        // a day paid at the far end of all it looks at, each to a trader of its own who holds no USDC, every
        // payout before them waiting for its approval. From cold storage, with none, one and both refused by
        // the token.
        SimPool pool = _pool();
        address[] memory traders = _claimedPayouts(pool, KEEPER_SCAN);
        deal(address(usdc), address(pool), KEEPER_SCAN * uint256(LARGE_PAYOUT));
        vm.warp(block.timestamp + 72 hours);
        uint256 worst96;
        uint256 worst;
        for (uint256 refused = 0; refused <= KEEPER_PAYS; ++refused) {
            uint256 g96 = _payGas(pool, traders, 96, refused);
            uint256 g = _payGas(pool, traders, KEEPER_SCAN, refused);
            emit log_named_uint("refused by the token", refused);
            emit log_named_uint("  pay(96), two settled, gas", g96);
            emit log_named_uint("  pay(192), two settled, gas", g);
            if (g96 > worst96) worst96 = g96;
            if (g > worst) worst = g;
        }
        emit log_named_uint("each further payout looked at, gas", (worst - worst96) / (KEEPER_SCAN - 96));
        assertLt(worst96, 800_000, "pay(96) with the day's two payments");
        // The keeper's reach: within two thirds of a small block's 2 000 000, the rest left for a token whose
        // transfer costs more than the test's.
        assertLt(worst, 1_300_000, "pay(192) with the day's two payments");
    }

    function test_withdraw_aPartnerTheTokenRefusesHasItsPartSetAside() public {
        SimPool pool = _pool();
        _share(pool, 5000);
        _withSales(pool, 30);
        usdc.block_(partner);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(partner), 0);
        assertEq(pool.heldOf(partner), 497_500_000, "set aside for the partner");
        assertEq(pool.held(), 497_500_000);
        assertEq(pool.withdrawable(), usdc.balanceOf(address(pool)) - 497_500_000 - pool.sharesDue() - 3 * LARGE_PAYOUT,
            "and kept out of what the creator may take");
        vm.prank(partner);
        pool.claimHeld(makeAddr("partner's other address"));
        assertEq(usdc.balanceOf(makeAddr("partner's other address")), 497_500_000);
    }

    function test_withdraw_aCreatorTheTokenRefusesHasItsPartSetAside_theCodesAndFinishGoOn() public {
        // The token refuses the creator's address: the withdrawal goes through, the creator's part is set aside for
        // it, and the code's share does not depend on it (`finish`: the next test).
        SimPool pool = _pool();
        _share(pool, 6000);
        for (uint256 i = 0; i < 30; ++i) _buy(pool, _who(i), 1);
        vm.warp(pool.lockedUntil() + 1);
        uint256 w = pool.withdrawable();
        _ask(pool, w);
        usdc.block_(creator);
        uint256 before = usdc.balanceOf(creator);
        vm.recordLogs();
        vm.prank(creator);
        pool.withdraw();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        (,,, bool sent) = abi.decode(logs[logs.length - 1].data, (uint256, uint256, uint256, bool));
        assertFalse(sent, "Withdrawn says the creator's part was not sent");
        uint256 fee = w * 50 / 10_000;
        uint256 rest = w - fee - pool.sharesDue();
        assertEq(usdc.balanceOf(creator), before, "nothing reached the refused address");
        assertEq(pool.heldOf(creator), rest, "set aside for the creator");
        assertEq(pool.held(), rest);
        _takeShares(pool, CODE);
        assertGt(usdc.balanceOf(partner), 0, "the partner is paid whatever the creator's address");
        vm.prank(creator);
        pool.claimHeld(other);
        assertEq(usdc.balanceOf(other), rest, "the creator takes it to another address");
        assertEq(pool.held(), 0);
    }

    function test_withdraw_aRefusedCreatorDoesNotStopFinish() public {
        SimPool pool = _pool();
        _share(pool, 5000);
        PaperExam exam = _buy(pool, trader, 1);
        vm.prank(closer);
        pool.close();
        _fail(exam, trader);
        pool.release(address(exam));
        usdc.block_(creator);
        vm.warp(block.timestamp + factory.WITHDRAW_DELAY());
        uint256 bal = usdc.balanceOf(address(pool));
        vm.prank(closer);
        pool.finish();
        assertTrue(pool.finished());
        uint256 rest = bal - bal * 50 / 10_000 - pool.sharesDue();
        assertEq(pool.heldOf(creator), rest, "the creator's part set aside, not a refused finish");
        _takeShares(pool, CODE);
        assertGt(usdc.balanceOf(partner), 0, "and the code's partner takes its share after the finish");
        vm.prank(creator);
        pool.claimHeld(other);
        assertEq(usdc.balanceOf(other), rest);
    }

    function test_codes_whatTheyAccruedIsKeptFromSalesPayoutsAndTheNextProfit() public {
        // What a withdrawal accrued to a code stays in the pool until its partner takes it: it is in no free money,
        // no later profit, nothing the creator may withdraw and no payout.
        SimPool pool = _pool();
        _share(pool, 5000);
        _withSales(pool, 30);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        uint256 due = pool.sharesDue();
        assertEq(due, 497_500_001);
        uint256 bal = usdc.balanceOf(address(pool));
        assertEq(pool.free(), bal - due, "not free for sales or lines");
        // Before the term, only profit: the pool's money above what the creator put in and what the code accrued.
        // The creator adds 2 000, so the profit, not the floor, is what limits it.
        vm.startPrank(creator);
        usdc.approve(address(pool), 2_000e6);
        pool.addToPot(2_000e6);
        vm.stopPrank();
        assertEq(pool.withdrawable(), bal + 2_000e6 - due - pool.principal(), "not profit a second time");
        // A payout claimed now is paid only out of what is not the code's.
        PaperExam stage = _passedStage(pool, trader, 1);
        uint256 id = _claim(pool, address(stage));
        deal(address(usdc), address(pool), due + LARGE_PAYOUT - 1);
        vm.warp(block.timestamp + 72 hours);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutQueued(id, LARGE_PAYOUT - 1);
        pool.pay(10);
        assertEq(pool.paidCount(), 0, "one unit short of the payout without the code's money: it waits");
        deal(address(usdc), address(pool), due + LARGE_PAYOUT);
        pool.pay(10);
        assertEq(pool.paidCount(), 1);
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(partner), 497_500_000, "and the partner still takes all of it");
    }

    /// Five withdrawals of 3 units each by a code of 50 % that brought all the pool took, its partner sending its
    /// share after each one (`claimEach`) or once at the end; returns what the partner got in all.
    function _smallWithdrawals(bool claimEach) internal returns (uint256) {
        SimPool pool = _pool();
        _share(pool, 5000);
        _withSales(pool, 30);
        for (uint256 i = 0; i < 5; ++i) {
            _ask(pool, 3);
            vm.prank(creator);
            pool.withdraw();
            if (claimEach) _takeShares(pool, CODE);
        }
        _takeShares(pool, CODE);
        assertEq(pool.sharesDue(), (pool.accruedScaled() + ACCRUAL_SCALE - 1) / ACCRUAL_SCALE - usdc.balanceOf(partner),
            "kept for the code: all it accrued, rounded up, less what it got");
        return usdc.balanceOf(partner);
    }

    function test_codes_aClaimBetweenWithdrawalsTakesNothingFromTheCode() public {
        // 3 units less the platform's 0 is 3; half of it is 1.5 a withdrawal, 7.5 in five. Whether anyone sends the
        // code's share after each withdrawal or only at the end, its partner gets the same 7 units: the half unit of
        // each withdrawal stays the code's and counts the next time.
        uint256 snap = vm.snapshotState();
        uint256 each = _smallWithdrawals(true);
        vm.revertToState(snap);
        uint256 once = _smallWithdrawals(false);
        assertEq(each, 7, "claimed after every withdrawal: 7 of the 7.5");
        assertEq(once, each, "a claim between withdrawals takes nothing from the code");
    }

    function test_codes_aSaleOrARaiseKeepsWhatWasAccrued() public {
        // A (40 %) and B (50 %), one large sale each. Each withdrawal accrues to both; a raise of A, and later a
        // further sale by A, come after a withdrawal and before A's partner takes what it accrued: neither changes
        // what was accrued, and the next withdrawal shares by what each code brought then and its share then.
        SimPool pool = _pool();
        _share(pool, 4000);
        address ownerB = makeAddr("owner B");
        bytes32 codeB = keccak256("GROUP-TWO");
        vm.prank(creator);
        pool.setCode(codeB, ownerB, 5000);
        _buy(pool, trader, 1);
        _buyWith(pool, other, 1, codeB);
        deal(address(usdc), address(pool), 6_000e6);
        uint256 net = 1_000e6 - 5e6;
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        _share(pool, 6000); // raised after the withdrawal, before the claim
        assertEq(_due(pool, CODE), net / 2 * 4000 / 10_000, "A: 40 % of half, raised since");
        _takeShares(pool, CODE);
        deal(address(usdc), address(pool), usdc.balanceOf(address(pool)) + 1_000e6);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        _buy(pool, stranger, 1); // sold after the withdrawal, before the claim
        assertEq(_due(pool, CODE), net / 2 * 6000 / 10_000, "A: 60 % of half, sold since");
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(partner), net / 2 * 4000 / 10_000 + net / 2 * 6000 / 10_000);
        deal(address(usdc), address(pool), usdc.balanceOf(address(pool)) + 1_000e6);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        uint256 a0 = usdc.balanceOf(partner);
        _takeShares(pool, CODE);
        _takeShares(pool, codeB);
        assertEq(usdc.balanceOf(partner) - a0, net * 2 * 6000 / (3 * 10_000), "A: 60 % of two thirds");
        assertEq(usdc.balanceOf(ownerB), 2 * (net / 2 * 5000 / 10_000) + net * 5000 / (3 * 10_000),
            "B: 50 % of half twice, then of a third");
    }

    // ── a code for two: a group's owner and an author ────────────

    function test_splitCode_100ExamsAt60PercentHalfAndHalf_eachTakesItsPartApart() public {
        // The example of the letter to the CFO: one code for two, 60 % of the profit it brings, half to the group's
        // owner and half to the author; 100 exams sold by it, and the creator withdraws 1 000 of profit.
        SimPool pool = _pool();
        bytes32 two = keccak256("BLOGGER-1");
        address author = makeAddr("author");
        vm.expectEmit(address(pool));
        emit SimPool.CodeSet(two, partner, 6000, author, 5000);
        vm.prank(creator);
        pool.setSplitCode(two, partner, 6000, author, 5000);
        for (uint256 i = 0; i < 100; ++i) _buyWith(pool, _who(i), 1, two);
        uint256 before = usdc.balanceOf(creator);
        _ask(pool, 1_000e6);
        vm.prank(creator);
        pool.withdraw();
        // 1 000 less the platform's 5 is 995; the code brought all the pool took: 60 % of it is 597, half each.
        (uint256 toPartner, uint256 toAuthor) = pool.shareDue(two);
        assertEq(toPartner, 298_500_000, "the owner's half of 597");
        assertEq(toAuthor, 298_500_000, "the author's half of 597");
        assertLe(pool.sharesDue() - 597e6, 1);
        assertEq(usdc.balanceOf(creator) - before, 995e6 - pool.sharesDue(), "the creator the rest, 398");
        usdc.block_(author); // the token refuses the author: only the author's part waits
        vm.expectEmit(address(pool));
        emit SimPool.SharePaid(two, partner, 298_500_000, true);
        vm.expectEmit(address(pool));
        emit SimPool.SharePaid(two, author, 298_500_000, false);
        _takeShares(pool, two);
        assertEq(usdc.balanceOf(partner), 298_500_000, "the owner is paid whatever the author's address");
        assertEq(pool.heldOf(author), 298_500_000, "the author's part set aside for the author");
        (,,,,,,,,, uint256 partnerGot, uint256 authorGot,) = pool.codeOf(two);
        assertEq(partnerGot, 298_500_000);
        assertEq(authorGot, 298_500_000);
        vm.prank(author);
        pool.claimHeld(makeAddr("author's other address"));
        assertEq(usdc.balanceOf(makeAddr("author's other address")), 298_500_000);
        _takeShares(pool, two); // a second claim sends neither of them anything
        assertEq(usdc.balanceOf(partner), 298_500_000);
        assertEq(pool.heldOf(author), 0);
    }

    function test_splitCode_theBoundsAreOnTheCodesShare_theSplitIsFixedByTheFirstSale() public {
        SimPool pool = _pool();
        bytes32 two = keccak256("BLOGGER-2");
        address author = makeAddr("author");
        vm.startPrank(creator);
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setSplitCode(two, partner, 6000, address(0), 5000); // a part with no author
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setSplitCode(two, partner, 6000, author, 0); // an author with no part
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setSplitCode(two, partner, 6000, author, 10_000); // all of it to the author: a code for one
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setSplitCode(two, address(0), 0, author, 5000); // an author on a code off sale
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setSplitCode(two, partner, 6000, partner, 5000); // the author is the partner: a code for one
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setSplitCode(two, partner, 3999, author, 5000); // the code's share under the bound
        vm.expectRevert(SimPool.BadShare.selector);
        pool.setSplitCode(two, partner, 6001, author, 5000);
        // 40 % in all, 20 % each: the lower bound is the code's share, not each part.
        pool.setSplitCode(two, partner, 4000, author, 5000);
        pool.setSplitCode(two, partner, 4000, author, 3000); // changed until a sale names it
        vm.stopPrank();
        (address p, uint16 bps,, uint16 ab, address au,,,,,,,) = pool.codeOf(two);
        assertEq(abi.encode(p, bps, ab, au), abi.encode(partner, uint16(4000), uint16(3000), author));
        _buyWith(pool, trader, 0, two);
        vm.startPrank(creator);
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setSplitCode(two, partner, 5000, makeAddr("another author"), 3000);
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setSplitCode(two, partner, 5000, author, 4000); // the split stays
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setCode(two, partner, 5000); // and so does the author
        vm.expectRevert(SimPool.CodeUsed.selector);
        pool.setSplitCode(two, partner, 4000, author, 3000); // not a raise
        pool.setSplitCode(two, partner, 5000, author, 3000); // a raise: the same three, a higher share
        vm.stopPrank();
        uint256 got;
        (, bps,,,, got,,,,,,) = pool.codeOf(two);
        assertEq(bps, 5000);
        assertEq(pool.weight(), got * 5000, "the raise counts in the pool's weight from now on");
    }

    function test_splitCode_theAuthorsPartIsRoundedDown_theTwoAddUpToWhatAccrued() public {
        SimPool pool = _pool();
        bytes32 two = keccak256("BLOGGER-3");
        vm.prank(creator);
        pool.setSplitCode(two, partner, 4000, makeAddr("author"), 3333);
        for (uint256 i = 0; i < 30; ++i) _buyWith(pool, _who(i), 1, two);
        _ask(pool, 123_456_789);
        vm.prank(creator);
        pool.withdraw();
        (uint256 toPartner, uint256 toAuthor) = pool.shareDue(two);
        uint256 asked = 123_456_789;
        uint256 net = asked - asked * 50 / 10_000;
        uint256 all = net * 4000 / 10_000;
        assertEq(toPartner + toAuthor, all, "the code's 40 %, to the unit");
        assertEq(toAuthor, all * 3333 / 10_000, "the author's part, rounded down");
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
        SimPoolFactory none = new SimPoolFactory(IERC20(address(usdc)), factory.examImpl(), factory.poolImpl(), address(this), 1 ether);
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
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(closer), 0, "the closer gets none of it");
        assertEq(usdc.balanceOf(partner), toPartner, "the code's partner its share");
        assertLe(pool.sharesDue(), 1, "a unit of rounding at most stays");
        assertEq(usdc.balanceOf(creator) - before, bal - bal * 50 / 10_000 - toPartner - pool.sharesDue(), "the creator the rest");
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
        assertEq(usdc.balanceOf(address(pool)), pool.sharesDue(), "what the code accrued stays for its partner");
        _takeShares(pool, CODE); // after the pool has finished too
        assertEq(usdc.balanceOf(partner), toPartner, "a share of the profit only");
        assertLe(usdc.balanceOf(address(pool)), 1, "a unit of rounding at most stays");
        assertEq(usdc.balanceOf(creator) - before, bal - bal * 50 / 10_000 - toPartner - pool.sharesDue());
        vm.prank(operator);
        vm.expectRevert(SimPool.Done.selector);
        pool.liftStop(0);
    }

    // ── the page's data, from this contract's own events (app/tests/simpool-chain.test.mjs) ──

    /// The file the app's test reads: what a page of this pool is drawn from, recorded off this contract.
    string constant PAGE_FIXTURE = "app/tests/fixtures/sim-page-chain.json";
    string[] internal _pageEvents;
    /// Each exam's and stage's own events the counts read (activityCounts), by account, in the order first seen.
    address[] internal _examAccounts;
    mapping(address account => string[]) internal _examEvents;
    uint256 internal _pageFirstBlock;
    mapping(uint256 block_ => uint256) internal _pageIndex;

    /// The name of a pool event the page reads, by its topic; "" for the others (the creator's money, ...).
    function _pageName(bytes32 topic) internal pure returns (string memory) {
        if (topic == SimPool.ExamSold.selector) return "ExamSold";
        if (topic == SimPool.Funded.selector) return "Funded";
        if (topic == SimPool.Released.selector) return "Released";
        if (topic == SimPool.PayoutDue.selector) return "PayoutDue";
        if (topic == SimPool.PayoutQueued.selector) return "PayoutQueued";
        if (topic == SimPool.PayoutPaid.selector) return "PayoutPaid";
        if (topic == SimPool.PayoutStopped.selector) return "PayoutStopped";
        if (topic == SimPool.PayoutHeld.selector) return "PayoutHeld";
        if (topic == SimPool.PayoutApproved.selector) return "PayoutApproved";
        if (topic == SimPool.ApproverSet.selector) return "ApproverSet";
        if (topic == SimPool.StopLifted.selector) return "StopLifted";
        if (topic == SimPool.StopObjected.selector) return "StopObjected";
        if (topic == SimPool.StopFinal.selector) return "StopFinal";
        if (topic == SimPool.CodeSet.selector) return "CodeSet";
        if (topic == SimPool.SharePaid.selector) return "SharePaid";
        return "";
    }

    /// The pool's events since the last call, each as the node would give it: its topics and data as they are, its
    /// block and time, and its index in the block. No transaction hash exists in a test; the one written is made up
    /// from the block and the index, and says so by being the keccak of them.
    function _pageCollect(SimPool pool) internal {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        if (_pageFirstBlock == 0) _pageFirstBlock = block.number;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter != address(pool)) {
                _examCollect(pool, logs[i]);
                continue;
            }
            string memory name = _pageName(logs[i].topics[0]);
            if (bytes(name).length == 0) continue;
            uint256 index = _pageIndex[block.number]++;
            string memory topics = "";
            for (uint256 t = 0; t < logs[i].topics.length; ++t) {
                topics = string.concat(topics, t == 0 ? "" : ",", '"', vm.toString(logs[i].topics[t]), '"');
            }
            _pageEvents.push(string.concat(
                '{"name":"', name, '","block":', vm.toString(block.number), ',"time":', vm.toString(block.timestamp),
                ',"index":', vm.toString(index), ',"tx":"', vm.toString(keccak256(abi.encode(block.number, index))),
                '","topics":[', topics, '],"data":"', vm.toString(logs[i].data), '"}'
            ));
        }
    }

    /// The name of an exam's or a stage's event the counts read; "" for the others (orders placed, fills lapsed, ...).
    function _examName(bytes32 topic) internal pure returns (string memory) {
        if (topic == PaperExam.Started.selector) return "Started";
        if (topic == PaperExam.Filled.selector) return "Filled";
        if (topic == PaperExam.StepPassed.selector) return "StepPassed";
        if (topic == PaperExam.Passed.selector) return "Passed";
        if (topic == PaperExam.Stopped.selector) return "Stopped";
        return "";
    }

    /// `_pass`, with the logs collected after each fill: a fill moves to the next block, and a log takes the block it
    /// is collected in, so each is collected before the next block (review of #281).
    function _passCollected(SimPool pool, PaperExam e, address who) internal {
        (,, uint64 capital,) = e.result();
        uint64 sz = capital / mark;
        _fill(e, who, true, sz);
        _pageCollect(pool);
        _setMark(mark * 12 / 10);
        _fill(e, who, false, sz);
        vm.prank(who);
        e.graduate();
    }

    /// An exam's or a stage's own event, kept by its account, as the node would give it in a second pass over the logs
    /// of the pool's accounts. An account is the pool's if its Started names the pool; the rest are left out.
    function _examCollect(SimPool pool, Vm.Log memory log) internal {
        string memory name = _examName(log.topics[0]);
        if (bytes(name).length == 0) return;
        if (_examEvents[log.emitter].length == 0) {
            if (log.topics[0] != PaperExam.Started.selector || log.topics[2] != bytes32(uint256(uint160(address(pool))))) return;
            _examAccounts.push(log.emitter);
        }
        uint256 index = _pageIndex[block.number]++;
        string memory topics = "";
        for (uint256 t = 0; t < log.topics.length; ++t) {
            topics = string.concat(topics, t == 0 ? "" : ",", '"', vm.toString(log.topics[t]), '"');
        }
        _examEvents[log.emitter].push(string.concat(
            '{"name":"', name, '","block":', vm.toString(block.number), ',"time":', vm.toString(block.timestamp),
            ',"index":', vm.toString(index), ',"tx":"', vm.toString(keccak256(abi.encode(block.number, index))),
            '","topics":[', topics, '],"data":"', vm.toString(log.data), '"}'
        ));
    }

    function _examJson() internal view returns (string memory j) {
        for (uint256 i = 0; i < _examAccounts.length; ++i) {
            string[] storage list = _examEvents[_examAccounts[i]];
            string memory events = "";
            for (uint256 k = 0; k < list.length; ++k) events = string.concat(events, k == 0 ? "" : ",\n      ", list[k]);
            j = string.concat(j, i == 0 ? "" : ",\n    ", '"', vm.toString(_examAccounts[i]), '": [\n      ', events, "\n    ]");
        }
    }

    function _pageReads(SimPool pool) internal view returns (string memory r) {
        SimSize[] memory m = pool.menu();
        string memory menu = "";
        for (uint256 i = 0; i < m.length; ++i) {
            menu = string.concat(menu, i == 0 ? "" : ",", '{"size":"', vm.toString(m[i].size), '","price":"',
                vm.toString(m[i].price), '"}');
        }
        string memory list = "";
        for (uint256 i = 0; i < pool.payoutCount(); ++i) {
            SimPool.Payout memory p = pool.payout(i);
            list = string.concat(list, i == 0 ? "" : ",", '{"account":"', vm.toString(p.account), '","trader":"',
                vm.toString(p.trader), '","amount":"', vm.toString(p.amount), '","dueAt":"', vm.toString(p.dueAt),
                '","state":', vm.toString(uint8(p.state)), ',"wasStopped":', p.wasStopped ? "true" : "false",
                ',"stoppedAt":"', vm.toString(p.stoppedAt), '","approved":', p.approved ? "true" : "false", "}");
        }
        r = string.concat('{"menu":[', menu, '],"payouts":[', list, '],"paidCount":"', vm.toString(pool.paidCount()),
            '","paidTotal":"', vm.toString(pool.paidTotal()), '","stoppedCount":"', vm.toString(pool.stoppedCount()),
            '","liftedCount":"', vm.toString(pool.liftedCount()), '","owed":"', vm.toString(pool.owed()));
        r = string.concat(r, '","held":"', vm.toString(pool.held()), '","reserved":"', vm.toString(pool.reserved()),
            '","live":"', vm.toString(pool.live()), '","closed":', pool.closed() ? "true" : "false", ',"finished":',
            pool.finished() ? "true" : "false", ',"lockedUntil":"', vm.toString(pool.lockedUntil()), '","closedEarly":',
            pool.closedEarly() ? "true" : "false");
        r = string.concat(r, ',"codes":[', _pageCodes(pool), '],"terms":"', vm.toString(pool.terms()),
            '","fundWindow":', vm.toString(pool.fundWindow()), "}");
    }

    /// The codes of the recorded chain, in the order they were set (a page finds them in the pool's `CodeSet` events).
    bytes32[] internal _pageKeys;

    /// Each code as the page reads it: its key, and codeOf's partner, share, whether a sale fixed it, what it brought.
    function _pageCodes(SimPool pool) internal view returns (string memory list) {
        bytes32[] memory keys = _pageKeys;
        for (uint256 i = 0; i < keys.length; ++i) {
            (address p, uint16 bps, bool used,,, uint256 got,,,,,,) = pool.codeOf(keys[i]);
            list = string.concat(list, i == 0 ? "" : ",", '{"code":"', vm.toString(keys[i]), '","partner":"',
                vm.toString(p), '","bps":', vm.toString(bps), ',"used":', used ? "true" : "false", ',"brought":"',
                vm.toString(got), '"}');
        }
    }

    function _pageJson(SimPool pool) internal view returns (string memory j) {
        string memory events = "";
        for (uint256 i = 0; i < _pageEvents.length; ++i) events = string.concat(events, i == 0 ? "" : ",\n    ", _pageEvents[i]);
        j = string.concat(
            "{\n  \"about\": \"A simulated pool's page data, recorded off SimPool by test/sim/SimPool.t.sol ",
            "(test_page_recordedChain); app/tests/simpool-chain.test.mjs draws the page from it. Regenerate: ",
            "SIM_PAGE_FIXTURE=write forge test --match-test test_page_recordedChain\",\n",
            "  \"pool\": \"", vm.toString(address(pool)), "\",\n  \"usdc\": \"", vm.toString(address(usdc)), "\",\n",
            "  \"chainId\": ", vm.toString(block.chainid), ",\n  \"now\": ", vm.toString(block.timestamp), ",\n"
        );
        j = string.concat(j,
            "  \"scan\": {\"from\": ", vm.toString(_pageFirstBlock), ", \"to\": ", vm.toString(block.number),
            ", \"readTo\": ", vm.toString(block.number), ", \"complete\": true},\n",
            "  \"reads\": ", _pageReads(pool), ",\n  \"events\": [\n    ", events, "\n  ],\n"
        );
        j = string.concat(j, "  \"examEvents\": {\n    ", _examJson(), "\n  }\n}\n");
    }

    /// Two trading groups' codes, 50 % and 40 %. Three traders buy with the first, pass both stages and claim; the
    /// checker stops the second payout and its trader objects; nothing is free, so the creator adds 2 200 to the pot
    /// and ten more exams are sold with the first code and thirty with the second; the token refuses the third trader and the second group's owner; after the payout delay one
    /// payout is paid, and the creator withdraws 400, sharing its profit with both codes, which anyone then sends their
    /// owners; then, long before the
    /// pool's 184 days, the platform's closer closes it. The page drawn from
    /// these events must show 800 USDC paid, with that payment's event, the stop and the held payout apart, and each
    /// code with what it brought and what its owner was sent or had set aside, and the pool's term with the platform's
    /// early close.
    function test_page_recordedChain() public {
        vm.recordLogs();
        SimPool pool = _pool();
        _share(pool, 5000);
        address owner2 = makeAddr("the second group's owner");
        bytes32 two = keccak256("GROUP-TWO");
        vm.prank(creator);
        pool.setCode(two, owner2, 4000);
        _pageCollect(pool);
        _pageKeys.push(CODE);
        _pageKeys.push(two);
        address[3] memory who = [trader, other, makeAddr("refused by the token")];
        uint256[3] memory ids;
        // Every exam bought first, while line 1 sells.
        PaperExam[3] memory exams;
        for (uint256 i = 0; i < 3; ++i) {
            exams[i] = _buy(pool, who[i], 1);
            _pageCollect(pool);
        }
        for (uint256 i = 0; i < 3; ++i) {
            PaperExam exam = exams[i];
            _passCollected(pool, exam, who[i]);
            vm.prank(who[i]);
            PaperExam stage = PaperExam(pool.fund(address(exam)));
            _pageCollect(pool);
            _passCollected(pool, stage, who[i]);
            ids[i] = pool.claimPayout(address(stage));
            _pageCollect(pool);
            // The pool's approver approves each payout, so it goes out at its 72 hours.
            _approve(pool, ids[i]);
            _pageCollect(pool);
            vm.roll(block.number + 1);
        }
        _stop(pool, ids[1]);
        _pageCollect(pool);
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + 1 hours);
        vm.prank(other);
        pool.objectStop(ids[1], keccak256("those were hedges on another exchange"));
        _pageCollect(pool);
        // A stopped payout's money is set aside for its 14 days, and nothing is free: no line sells until the creator
        // adds to the pot. Then prices of further exams pay the third payout.
        vm.startPrank(creator);
        usdc.approve(address(pool), 2_200e6);
        pool.addToPot(2_200e6);
        vm.stopPrank();
        _pageCollect(pool);
        _withSales(pool, 10);
        _pageCollect(pool);
        vm.roll(block.number + 1);
        for (uint256 i = 0; i < 30; ++i) {
            address b = makeAddr(string(abi.encode("the second group's buyer", i)));
            deal(address(usdc), b, LARGE_PRICE);
            vm.startPrank(b);
            usdc.approve(address(pool), LARGE_PRICE);
            pool.buy(1, two);
            vm.stopPrank();
        }
        _pageCollect(pool);
        usdc.block_(who[2]);
        usdc.block_(owner2);
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + factory.PAYOUT_DELAY());
        assertEq(pool.pay(10), 2, "the first paid, the third held, the stopped one passed over");
        _pageCollect(pool);
        // The creator asks to withdraw, and carries it out a week later: the only way money leaves for the creator.
        vm.roll(block.number + 1);
        vm.prank(creator);
        pool.requestWithdraw(400e6);
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + factory.WITHDRAW_DELAY());
        vm.prank(creator);
        pool.withdraw();
        _pageCollect(pool);
        // Anyone sends both codes' owners what the withdrawal accrued to them.
        vm.roll(block.number + 1);
        vm.prank(stranger);
        pool.claimShares(_pageKeys);
        _pageCollect(pool);
        assertEq(pool.paidCount(), 1);
        assertEq(pool.paidTotal(), LARGE_PAYOUT);
        assertGt(usdc.balanceOf(partner), 0, "the first group's owner was sent its part");
        assertGt(pool.heldOf(owner2), 0, "the second's was set aside: the token refuses it");
        assertEq(pool.held(), LARGE_PAYOUT + pool.heldOf(owner2));
        assertEq(pool.reserved(), LARGE_PAYOUT);
        // Long before its 184 days, the platform's closer closes the pool: it sells no more exams.
        vm.roll(block.number + 1);
        vm.prank(closer);
        pool.close();
        _pageCollect(pool);
        assertTrue(pool.closedEarly());
        assertLt(block.timestamp, pool.lockedUntil());

        string memory json = _pageJson(pool);
        if (keccak256(bytes(vm.envOr("SIM_PAGE_FIXTURE", string("")))) == keccak256("write")) {
            vm.writeFile(PAGE_FIXTURE, json);
        } else {
            assertEq(vm.readFile(PAGE_FIXTURE), json,
                "the page's recorded chain is not this contract's: SIM_PAGE_FIXTURE=write forge test --match-test test_page_recordedChain");
        }
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
        _claim(pool, address(_passedStage(pool, trader, 1)));
        _claim(pool, address(_passedStage(pool, other, 1)));
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
        uint256 kept = pool.sharesDue();
        _takeShares(pool, CODE);
        assertEq(usdc.balanceOf(platform) - fees, 9_601_250, "0.5 % of 1 920.25");
        assertEq(usdc.balanceOf(partner), 627_784_589, "A: 50 % of 1 910.64875 x 46/70");
        assertLe(kept - 627_784_589, 1, "kept for the code: its part, and a unit of rounding at most");
        assertEq(usdc.balanceOf(creator) - before, 1_282_864_161 - (kept - 627_784_589), "the creator the rest, B's part with it");
        assertEq(usdc.balanceOf(address(pool)), 2_400e6 + pool.sharesDue(), "the floor of three payouts stays");
    }

    function test_withdraw_neverMoreThanWasAsked_whateverCameInSince() public {
        SimPool pool = _pool();
        _withSales(pool, 30);
        _ask(pool, 500e6);
        _withSales(pool, 30);
        uint256 bal = usdc.balanceOf(address(pool));
        vm.prank(creator);
        pool.withdraw();
        assertEq(bal - usdc.balanceOf(address(pool)), 500e6, "what was asked, not more for what came in since");
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
        _claim(pool, address(_passedStage(pool, trader, 1)));
        _claim(pool, address(_passedStage(pool, other, 1)));
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

    // ── a payout goes out with the approver's approval, or 7 days after its claim ──

    function test_approve_theApproverTheCreatorNames_onceAWaitingPayout() public {
        SimPool pool = _pool();
        // Every exam bought first, while its line sells.
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        uint256 id = pool.claimPayout(address(_passedStage(pool, bought1, trader)));
        uint256[] memory ids = new uint256[](1);
        ids[0] = id;
        address[3] memory notApprover = [creator, checker, stranger];
        for (uint256 i = 0; i < 3; ++i) {
            vm.prank(notApprover[i]);
            vm.expectRevert(SimPool.NotApprover.selector);
            pool.approvePayouts(ids);
        }
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.setApprover(stranger);
        vm.expectEmit(address(pool));
        emit SimPool.PayoutApproved(id, approver);
        vm.prank(approver);
        pool.approvePayouts(ids);
        assertTrue(pool.payout(id).approved);
        vm.recordLogs();
        vm.prank(approver);
        pool.approvePayouts(ids); // once: approved again, it is passed over
        assertEq(vm.getRecordedLogs().length, 0, "approved once, no second event");
        ids[0] = id + 1;
        vm.prank(approver);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.approvePayouts(ids); // no such payout
        uint256 id2 = pool.claimPayout(address(_passedStage(pool, bought2, other)));
        _stop(pool, id2);
        ids[0] = id2;
        vm.recordLogs();
        vm.prank(approver);
        pool.approvePayouts(ids); // stopped: passed over
        assertEq(vm.getRecordedLogs().length, 0, "a stopped payout is not approved");
        assertFalse(pool.payout(id2).approved);
        vm.expectEmit(address(pool));
        emit SimPool.ApproverSet(stranger);
        vm.prank(creator);
        pool.setApprover(stranger);
        assertEq(pool.approver(), stranger);
    }

    /// A batch the approver sent while one of its payouts went out at `PAYOUT_HARD`, or was stopped, still approves
    /// the rest: the one paid or stopped is passed over without an event, and only an id past the last is refused.
    function test_approve_aBatchPassesOverOnePaidOrStoppedMeanwhile() public {
        SimPool pool = _pool();
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        PaperExam bought3 = _buy(pool, stranger, 1);
        _withSales(pool, 30);
        uint256 id0 = pool.claimPayout(address(_passedStage(pool, bought1, trader)));
        vm.warp(block.timestamp + 6 days);
        uint256 id1 = pool.claimPayout(address(_passedStage(pool, bought2, other)));
        uint256 id2 = pool.claimPayout(address(_passedStage(pool, bought3, stranger)));
        _stop(pool, id2);
        vm.warp(block.timestamp + 1 days);
        assertEq(pool.pay(10), 1, "the first goes out at 7 days, unapproved");
        assertEq(uint8(pool.payout(id0).state), uint8(SimPool.PayoutState.Paid));
        uint256[] memory ids = new uint256[](3);
        (ids[0], ids[1], ids[2]) = (id0, id1, id2);
        vm.recordLogs();
        vm.prank(approver);
        pool.approvePayouts(ids);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(logs.length, 1, "one approval");
        assertEq(logs[0].topics[0], SimPool.PayoutApproved.selector);
        assertEq(uint256(logs[0].topics[1]), id1);
        assertTrue(pool.payout(id1).approved, "the waiting one is approved");
        assertFalse(pool.payout(id0).approved, "the paid one is passed over");
        assertFalse(pool.payout(id2).approved, "the stopped one is passed over");
        vm.warp(block.timestamp + 72 hours);
        uint256 before = usdc.balanceOf(other);
        assertEq(pool.pay(10), 1, "and it is paid from its 72 hours");
        assertEq(usdc.balanceOf(other) - before, LARGE_PAYOUT);
        ids = new uint256[](2);
        (ids[0], ids[1]) = (id1, id2 + 1);
        vm.prank(approver);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.approvePayouts(ids);
    }

    function test_pay_anUnapprovedPayoutGoesOutSevenDaysAfterItsClaim() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        assertEq(pool.payoutHardAt(id), block.timestamp + 7 days);
        assertEq(factory.PAYOUT_HARD(), 7 days);
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 0, "72 hours, no approval: it waits");
        vm.warp(pool.payoutHardAt(id) - 1);
        assertEq(pool.pay(10), 0);
        vm.warp(pool.payoutHardAt(id));
        uint256 before = usdc.balanceOf(trader);
        assertEq(pool.pay(10), 1, "7 days: it goes, approved or not");
        assertEq(usdc.balanceOf(trader) - before, LARGE_PAYOUT);
    }

    function test_pay_withNoApprover_everyPayoutWaitsSevenDays() public {
        SimPool pool = _pool();
        vm.prank(creator);
        pool.setApprover(address(0));
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        uint256[] memory ids = new uint256[](1);
        ids[0] = id;
        vm.prank(address(0));
        vm.expectRevert(SimPool.NotApprover.selector);
        pool.approvePayouts(ids);
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 0);
        vm.warp(pool.payoutHardAt(id));
        assertEq(pool.pay(10), 1);
    }

    function test_pay_passesAnUnapprovedOne_butKeepsItsMoneyForIt() public {
        SimPool pool = _pool();
        // Every exam bought first, while its line sells.
        PaperExam bought1 = _buy(pool, trader, 1);
        PaperExam bought2 = _buy(pool, other, 1);
        uint256 a = pool.claimPayout(address(_passedStage(pool, bought1, trader))); // not approved
        uint256 b = _claim(pool, address(_passedStage(pool, bought2, other)));
        vm.warp(block.timestamp + 72 hours);
        deal(address(usdc), address(pool), 1_000e6); // enough for one payout, not for both
        vm.expectEmit(address(pool));
        emit SimPool.PayoutQueued(b, 1_000e6);
        assertEq(pool.pay(10), 0, "b would take a's money: it waits");
        deal(address(usdc), address(pool), 1_600e6);
        assertEq(pool.pay(10), 1, "both covered: b is paid, a waits for its approval");
        assertEq(uint8(pool.payout(b).state), uint8(SimPool.PayoutState.Paid));
        assertEq(uint8(pool.payout(a).state), uint8(SimPool.PayoutState.Due));
        assertEq(pool.head(), a, "the head stays at the first unpaid");
        vm.warp(pool.payoutHardAt(a));
        assertEq(pool.pay(10), 1);
        assertEq(pool.head(), 2);
    }

    function test_pay_looksAtMostAtMaxWaitingPayouts() public {
        SimPool pool = _pool();
        deal(address(usdc), address(pool), 10_000e6);
        for (uint256 i = 0; i < 3; ++i) pool.claimPayout(address(_passedStage(pool, makeAddr(string(abi.encode("t", i))), 1)));
        uint256 last = _claim(pool, address(_passedStage(pool, trader, 1)));
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(3), 0, "three looked at, none approved");
        assertEq(pool.pay(4), 1, "the fourth is");
        assertEq(uint8(pool.payout(last).state), uint8(SimPool.PayoutState.Paid));
        assertEq(pool.head(), 0);
        // A settled payout counts as looked at too: past the paid fourth, a fifth needs five (review of #286).
        uint256 fifth = _claim(pool, address(_passedStage(pool, other, 1)));
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(4), 0, "the three waiting and the paid one");
        assertEq(pool.pay(5), 1);
        assertEq(uint8(pool.payout(fifth).state), uint8(SimPool.PayoutState.Paid));
    }

    function test_stop_reasonThree_theResultDisputed_noPairOneTextHash() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        bytes32[] memory one = new bytes32[](1);
        one[0] = keccak256("fill 0x.. at block 123: the oracle stood 4 % off");
        bytes32[] memory text = new bytes32[](1);
        text[0] = keccak256("the published dispute");
        vm.startPrank(checker);
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 3, other, one, text); // no pair named
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 3, address(0), new bytes32[](0), text); // a transaction at least
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 3, address(0), one, new bytes32[](0)); // the text's hash
        bytes32[] memory two = new bytes32[](2);
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 3, address(0), one, two); // one hash, not two
        vm.expectRevert(SimPool.BadPaired.selector);
        pool.stopPayout(id, 1, address(0), _trades(), new bytes32[](0)); // reasons 1 and 2 still need a pair
        vm.expectEmit(address(pool));
        emit SimPool.PayoutStopped(id, trader, LARGE_PAYOUT, 3, address(0), one, text, 1, 0, 0);
        pool.stopPayout(id, 3, address(0), one, text);
        vm.stopPrank();
        assertEq(pool.reserved(), LARGE_PAYOUT, "kept 14 days, as any stop");
        assertEq(pool.stopReasonOf(id), 3);
        vm.prank(trader);
        pool.objectStop(id, keccak256("the exam was right"));
    }

    /// Prohibited play (reason 4): the published analysis, at least two transactions, and another account of the factory
    /// or none. Nothing moves but what any stop moves: the payout's money is kept 14 days, the trader may object.
    function test_stop_reasonFour_prohibitedPlay_twoTransactionsOneAnalysis_aPairOrNone() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        bytes32[] memory analysis = new bytes32[](1);
        analysis[0] = keccak256("the published analysis: six exams in a day from two linked wallets, each all in");
        bytes32[] memory one = new bytes32[](1);
        bytes32[] memory two = new bytes32[](2);
        address own = pool.payout(id).account;
        vm.startPrank(checker);
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 4, address(0), one, analysis); // two transactions at least
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 4, address(0), _trades(), new bytes32[](0)); // the analysis's hash
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 4, address(0), _trades(), two); // one hash, not two
        address[3] memory bad = [own, stranger, address(pool)];
        for (uint256 i = 0; i < 3; ++i) {
            vm.expectRevert(SimPool.BadPaired.selector);
            pool.stopPayout(id, 4, bad[i], _trades(), analysis);
        }
        vm.expectRevert(SimPool.BadReason.selector);
        pool.stopPayout(id, 5, address(0), _trades(), analysis); // no fifth reason
        assertEq(pool.stopReasonOf(id), 0, "not stopped yet");
        vm.expectEmit(address(pool));
        emit SimPool.PayoutStopped(id, trader, LARGE_PAYOUT, 4, address(0), _trades(), analysis, 1, 0, 0);
        pool.stopPayout(id, 4, address(0), _trades(), analysis);
        vm.stopPrank();
        assertEq(pool.stopReasonOf(id), 4);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Stopped));
        assertEq(pool.owed(), 0);
        assertEq(pool.reserved(), LARGE_PAYOUT, "kept 14 days, as any stop");
        vm.warp(block.timestamp + 72 hours);
        assertEq(pool.pay(10), 0, "a stopped payout is not paid");
        vm.prank(trader);
        pool.objectStop(id, keccak256("one exam a week, never all in"));
        vm.warp(block.timestamp + 14 days);
        pool.finalizeStop(id);
        assertEq(pool.reserved(), 0, "final: the money is the pool's again");
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Refused));
    }

    /// A hedge between two accounts names the other one, of any pool of the factory; a lifted stop keeps its reason on
    /// record and the payout is owed again.
    function test_stop_reasonFour_aHedgeNamesTheOtherAccount_andALiftKeepsTheRecord() public {
        SimPool pool = _pool();
        uint256 id = pool.claimPayout(address(_passedStage(pool, trader, 1)));
        SimPool elsewhere = _pool();
        address hedge = address(_buy(elsewhere, other, 0));
        bytes32[] memory analysis = new bytes32[](1);
        analysis[0] = keccak256("the published analysis: the same size short on the other account");
        vm.prank(checker);
        pool.stopPayout(id, 4, hedge, _trades(), analysis);
        assertEq(pool.stopReasonOf(id), 4);
        vm.prank(operator);
        pool.liftStop(id);
        assertEq(uint8(pool.payout(id).state), uint8(SimPool.PayoutState.Due));
        assertEq(pool.owed(), LARGE_PAYOUT);
        assertEq(pool.reserved(), 0);
        assertEq(pool.stopReasonOf(id), 4, "the record stays: stopped once, for this reason");
        vm.prank(checker);
        vm.expectRevert(SimPool.BadPayout.selector);
        pool.stopPayout(id, 4, hedge, _trades(), analysis); // once
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
        // Bought while line 1 sells: once a payout is owed, less than two of 800 are free.
        PaperExam exam = _buy(pool, stranger, 1);
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
            // Money for line 1 to sell as the stages start; the CFO's balance is set after.
            deal(address(usdc), address(pool), 100_000e6);
            for (uint256 i = 0; i < ks[j]; ++i) _runningStage(pool, makeAddr(string(abi.encode("k", j, i))), 1);
            deal(address(usdc), address(pool), 4_320_250_000);
            assertEq(pool.staged(), ks[j] * LARGE_PAYOUT);
            assertEq(pool.withdrawable(), free[j], "the greater of 2 400 and the stages' payouts stays");
        }
        // By size: four large and two small stages keep 4 000, not six large payouts.
        SimPool mixed = _pool();
        deal(address(usdc), address(mixed), 100_000e6);
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

    // ── 24 lines, and what opening a pool costs in gas ───────────

    uint32 constant SOL = 5;
    /// HyperEVM's small block: "Fast block duration is set to 1 seconds with a 3M gas limit" (its documentation,
    /// https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/dual-block-architecture, read 16
    /// November 2026; `gasLimit` 3 000 000 on mainnet and testnet blocks the same day). A creator's `create` is an
    /// ordinary transaction, sent to a small block: the first pool's plan, and the largest plan the factory takes,
    /// must fit there with room to spare. The gate is two thirds of the block, the transaction's base and calldata
    /// counted.
    uint256 constant SMALL_BLOCK_GAS = 3_000_000;
    uint256 constant CREATE_GAS_GATE = 2_000_000;

    function _threeAssets(SimPlan memory p) internal {
        hyperCore.registerPerpAssetInfo(
            ETH, PrecompileLib.PerpAssetInfo({coin: "ETH", marginTableId: 55, szDecimals: 4, maxLeverage: 25, onlyIsolated: false})
        );
        hyperCore.registerPerpAssetInfo(
            SOL, PrecompileLib.PerpAssetInfo({coin: "SOL", marginTableId: 56, szDecimals: 2, maxLeverage: 20, onlyIsolated: false})
        );
        factory.setAsset(ETH, 1000);
        factory.setAsset(SOL, 500);
        uint32[] memory a = new uint32[](3);
        (a[0], a[1], a[2]) = (BTC, ETH, SOL);
        uint32[] memory caps = new uint32[](3);
        (caps[0], caps[1], caps[2]) = (1000, 1000, 500);
        p.examRules.assets = a;
        p.examRules.assetLeverageX100 = caps;
        p.examRules.maxLeverageX100 = 1000;
        p.fundedRules = p.examRules;
    }

    /// The first pool's plan: 5 000, 10 000, 25 000, 50 000, 100 000 and 250 000 on the loss limits 3, 4, 5 and 6 %,
    /// at the CFO's prices (a size of 50 000 and up at its size / 10 000 times the price of 10 000), BTC, ETH and SOL
    /// each at its own cap, one step, trailing to the start; 4 000 locked, the lines up to 25 000 on (CFO, Alex,
    /// 16 November).
    function _firstPlan() internal returns (SimPlan memory p) {
        p = _plan();
        _threeAssets(p);
        uint16[4] memory dd = [uint16(300), 400, 500, 600];
        uint16[4] memory target = [uint16(900), 1000, 1000, 1000];
        uint64[6] memory size = [uint64(5_000e6), 10_000e6, 25_000e6, 50_000e6, 100_000e6, 250_000e6];
        uint64[12] memory price = [uint64(20e6), 30e6, 40e6, 50e6, 40e6, 60e6, 79e6, 95e6, 100e6, 150e6, 198e6, 238e6];
        p.menu = new SimSize[](24);
        p.tiers = new SimTier[](24);
        for (uint256 i = 0; i < 24; ++i) {
            uint64 pr = i < 12 ? price[i] : price[4 + i % 4] * (size[i / 4] / 10_000e6);
            p.menu[i] = SimSize({size: size[i / 4], price: pr});
            p.tiers[i] = SimTier({maxDrawdownBps: dd[i % 4], targetBps: target[i % 4], drawdownKind: 1});
        }
        p.locked = 4_000e6;
        p.on = 0xfff;
    }

    /// The gas of `create` as a transaction: what the call spends, the base of 21 000 and the calldata (EIP-2028).
    function _createGas(SimPlan memory p) internal returns (uint256 total, SimPool pool) {
        bytes memory data = abi.encodeCall(factory.create, (p));
        uint256 cd;
        for (uint256 i = 0; i < data.length; ++i) {
            cd += data[i] == 0 ? 4 : 16;
        }
        vm.prank(creator);
        uint256 g = gasleft();
        pool = SimPool(factory.create{value: 1 ether}(p));
        total = g - gasleft() + 21_000 + cd;
    }

    /// Where `ops/deploy_sim.py check` writes the factory's terms book, roles and asset caps, and the book's texts,
    /// when it puts this code in an eth_call by a state override: these slots, and the script's constants are these.
    uint256 constant FACTORY_FILLER_SLOT = 1;
    uint256 constant FACTORY_CHECKER_SLOT = 2;
    uint256 constant FACTORY_CLOSER_SLOT = 3;
    uint256 constant FACTORY_FEE_SLOT = 4;
    uint256 constant FACTORY_TERMS_BOOK_SLOT = 5;
    uint256 constant FACTORY_ASSET_CAP_SLOT = 6;
    uint256 constant FACTORY_CREATION_FEE_SLOT = 9;
    uint256 constant BOOK_TEXT_AT_SLOT = 0;

    function _slotAddress(address at, uint256 slot) internal view returns (address) {
        return address(uint160(uint256(vm.load(at, bytes32(slot)))));
    }

    function test_storage_theSlotsTheDeployScriptsCheckWrites() public view {
        assertEq(_slotAddress(address(factory), FACTORY_FILLER_SLOT), factory.filler());
        assertEq(_slotAddress(address(factory), FACTORY_CHECKER_SLOT), factory.checker());
        assertEq(_slotAddress(address(factory), FACTORY_CLOSER_SLOT), factory.closer());
        assertEq(_slotAddress(address(factory), FACTORY_FEE_SLOT), factory.feeRecipient());
        assertEq(_slotAddress(address(factory), FACTORY_TERMS_BOOK_SLOT), address(book));
        assertEq(uint256(vm.load(address(factory), bytes32(FACTORY_CREATION_FEE_SLOT))), factory.creationFee());
        bytes32 cap = keccak256(abi.encode(uint256(BTC), FACTORY_ASSET_CAP_SLOT));
        assertEq(uint256(vm.load(address(factory), cap)), 1000);
        bytes32 text = keccak256(abi.encode(termsHash, BOOK_TEXT_AT_SLOT));
        assertEq(address(uint160(uint256(vm.load(address(book), text)))), book.textAt(termsHash));
        assertTrue(book.textAt(termsHash) != address(0));
    }

    function test_gas_createOfTheFirstPlanFitsASmallBlock() public {
        (uint256 g, SimPool pool) = _createGas(_firstPlan());
        emit log_named_uint("create, first plan (24 lines), as a transaction", g);
        assertEq(pool.menu().length, 24);
        assertEq(pool.onMask(), 0xfff, "5 000, 10 000 and 25 000 on, the rest off");
        assertLe(g, CREATE_GAS_GATE, "two thirds of a small block at most");
    }

    /// The largest plan the factory takes: `MAX_MENU` lines, an exam of four steps, three assets.
    function test_gas_createOfTheLargestPlanFitsASmallBlock() public {
        SimPlan memory p = _firstPlan();
        assertEq(p.menu.length, factory.MAX_MENU());
        uint16[] memory t4 = new uint16[](4);
        for (uint256 i = 0; i < 4; ++i) t4[i] = 1000;
        p.exam.targetsBps = t4;
        (uint256 g,) = _createGas(p);
        emit log_named_uint("create, 24 lines and 4 steps, as a transaction", g);
        assertLe(g, CREATE_GAS_GATE, "two thirds of a small block at most");
    }

    function test_gas_aPurchaseOnTheFirstPlanFitsASmallBlock() public {
        (, SimPool pool) = _createGas(_firstPlan());
        vm.prank(creator);
        pool.setCode(CODE, partner, 0);
        uint256 g = gasleft();
        PaperExam e = _buy(pool, trader, 11);
        g -= gasleft();
        emit log_named_uint("buy, line 11 (with deal and approve)", g);
        assertLe(g, CREATE_GAS_GATE, "two thirds of a small block at most");
        assertEq(e.rules().assets.length, 3);
    }

    function test_menu_twentyFourLinesAtMost() public {
        SimPlan memory p = _firstPlan();
        assertEq(factory.check(p), 250_000e6, "24 lines, the largest 250 000");
        SimPlan memory past = _firstPlan();
        past.menu = new SimSize[](25);
        past.tiers = new SimTier[](25);
        for (uint256 i = 0; i < 25; ++i) {
            past.menu[i] = i < 24 ? p.menu[i] : p.menu[0];
            past.tiers[i] = i < 24 ? p.tiers[i] : p.tiers[0];
        }
        vm.prank(creator);
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.create{value: 1 ether}(past);
    }

    /// The rules and terms are kept once and the tier put in at the start: every exam and every stage starts on
    /// what the pool kept for its line before (the plan's rules and terms with the line's loss limit, target, kind
    /// and size), each stage on its own rules, here unlike the exam's.
    function _checkStart(PaperExam e, Rules memory r, ExamTerms memory t, SimSize memory m, SimTier memory k)
        internal
        view
    {
        Rules memory got = e.rules();
        assertEq(got.dailyLossBps, r.dailyLossBps, "the plan's daily loss");
        assertEq(got.maxDrawdownBps, k.maxDrawdownBps, "the tier's loss limit");
        assertEq(got.maxLeverageX100, r.maxLeverageX100);
        assertEq(abi.encode(got.assets), abi.encode(r.assets), "the plan's assets");
        assertEq(abi.encode(got.assetLeverageX100), abi.encode(r.assetLeverageX100), "their caps");
        ExamTerms memory tt = e.terms();
        assertEq(tt.capital, m.size, "the line's size");
        assertEq(tt.targetsBps.length, t.targetsBps.length, "the plan's steps");
        for (uint256 j = 0; j < tt.targetsBps.length; ++j) {
            assertEq(tt.targetsBps[j], k.targetBps, "the tier's target, every step");
        }
        assertEq(tt.stepDuration, t.stepDuration);
        assertEq(tt.maxIdle, t.maxIdle);
        assertEq(tt.minTradingDays, t.minTradingDays);
        assertEq(tt.costPpm, t.costPpm);
        assertEq(tt.fillDelay, t.fillDelay);
        assertEq(tt.fillWindow, t.fillWindow);
        assertEq(e.drawdownKind(), k.drawdownKind, "the tier's kind of loss line");
    }

    function test_menu_eachExamAndStageStartsOnThePlansRulesWithItsLinesTier() public {
        SimPlan memory p = _firstPlan();
        uint16[] memory two = new uint16[](2);
        (two[0], two[1]) = (1, 2); // the values are not read: the tier's target takes their place
        p.exam.targetsBps = two;
        p.exam.costPpm = 950;
        p.funded.stepDuration = 20 days;
        p.funded.maxIdle = 10 days;
        p.funded.fillWindow = 9;
        p.funded.minTradingDays = 2;
        uint32[] memory btcEth = new uint32[](2);
        (btcEth[0], btcEth[1]) = (BTC, ETH);
        uint32[] memory caps = new uint32[](2);
        (caps[0], caps[1]) = (300, 200);
        // A struct of its own: `_threeAssets` set both to one struct in memory.
        p.fundedRules =
            Rules({dailyLossBps: 400, maxDrawdownBps: 600, maxLeverageX100: 500, assets: btcEth, assetLeverageX100: caps});
        p.tiers[5].drawdownKind = 0; // one line from the start
        SimPool pool = _open(p);
        PaperExam[12] memory exams;
        for (uint256 i = 0; i < 12; ++i) {
            exams[i] = _buy(pool, makeAddr(string(abi.encode("twelve", i))), i);
            _checkStart(exams[i], p.examRules, p.exam, p.menu[i], p.tiers[i]);
        }
        uint256[3] memory funded = [uint256(0), 5, 11];
        for (uint256 n = 0; n < 3; ++n) {
            uint256 i = funded[n];
            address who = makeAddr(string(abi.encode("twelve", i)));
            _pass(exams[i], who);
            _pass(exams[i], who);
            vm.prank(who);
            PaperExam stage = PaperExam(pool.fund(address(exams[i])));
            _checkStart(stage, p.fundedRules, p.funded, p.menu[i], p.tiers[i]);
            _setMark(1_000_000);
        }
    }

    function test_open_refusesATierPaperExamWouldRefuse() public {
        uint16[4] memory dd = [uint16(0), 10_000, 600, 600];
        uint16[4] memory target = [uint16(1000), 1000, 0, 10_001];
        for (uint256 i = 0; i < 4; ++i) {
            SimPlan memory p = _plan();
            p.tiers[1] = SimTier({maxDrawdownBps: dd[i], targetBps: target[i], drawdownKind: 1});
            vm.prank(creator);
            vm.expectRevert(SimPoolFactory.BadPlan.selector);
            factory.create{value: 1 ether}(p);
        }
        SimPlan memory edge = _plan();
        edge.tiers[1] = SimTier({maxDrawdownBps: 9_999, targetBps: 10_000, drawdownKind: 1});
        assertEq(factory.check(edge), LARGE, "PaperExam's bounds themselves pass");
    }

    /// The factory checks a tier on every line as PaperExam would at that line's start: the same answer, whatever
    /// the loss limit and the target.
    function testFuzz_open_aTierPassesExactlyWhenPaperExamWouldStartOnIt(uint16 dd, uint16 target) public view {
        SimPlan memory p = _plan();
        p.tiers[1] = SimTier({maxDrawdownBps: dd, targetBps: target, drawdownKind: 1});
        Rules memory r = p.examRules;
        r.maxDrawdownBps = dd;
        ExamTerms memory t = p.exam;
        t.capital = p.menu[1].size;
        t.targetsBps[0] = target;
        bool exam;
        try PaperExam(factory.examImpl()).check(r, t) {
            exam = true;
        } catch {}
        bool pool;
        try factory.check(p) returns (uint256) {
            pool = true;
        } catch {}
        assertEq(pool, exam);
    }

    // ── lines on and off: the creator's switch, and two payouts free (Alex, CFO, 16 November) ──

    function _firstPool() internal returns (SimPool) {
        return _open(_firstPlan());
    }

    /// `who` tries to buy line `line` and is refused with `err`.
    function _buyRefused(SimPool pool, address who, uint256 line, bytes memory err) internal {
        uint64 price = pool.menu()[line].price;
        deal(address(usdc), who, price);
        vm.startPrank(who);
        usdc.approve(address(pool), price);
        vm.expectRevert(err);
        pool.buy(line, CODE);
        vm.stopPrank();
    }

    function _lineState(SimPool pool, uint256 line, bool on, bool sells, bool canSwitchOn, uint256 short) internal view {
        (bool o, bool s_, bool c, uint256 sh) = pool.lineState(line);
        assertEq(o, on, "on");
        assertEq(s_, sells, "sells");
        assertEq(c, canSwitchOn, "can switch on");
        assertEq(sh, short, "short");
    }

    /// The first pool: 4 000 locked, so the lines up to 25 000 are on (two payouts of 2 000 at most) and the larger
    /// ones are off, each short of its two payouts by what 4 000 lacks; the floor is three payouts of 25 000.
    function test_lines_theFirstPool_onAtTheStartWhatItsLockCovers() public {
        uint256 before = usdc.balanceOf(creator);
        SimPool pool = _firstPool();
        assertEq(before - usdc.balanceOf(creator), 4_000e6, "the creator locks what the plan says");
        assertEq(pool.principal(), 4_000e6);
        assertEq(pool.free(), 4_000e6);
        uint256[6] memory short = [uint256(0), 0, 0, 4_000e6, 12_000e6, 36_000e6];
        for (uint256 i = 0; i < 24; ++i) {
            bool on = i < 12;
            _lineState(pool, i, on, on, on, short[i / 4]);
        }
        assertEq(pool.floor(), 6_000e6, "three payouts of 2 000");
        assertEq(pool.withdrawable(), 0);
        assertEq(pool.largestPayout(), 20_000e6, "the menu's largest, off or on");
        _buyRefused(pool, trader, 12, abi.encodeWithSelector(SimPool.LineOff.selector));
        _buy(pool, trader, 11);
    }

    function test_lines_onlyTheCreatorSwitchesALineOn_onlyWithTwoPayoutsFree_neverByItself() public {
        SimPool pool = _firstPool();
        vm.prank(stranger);
        vm.expectRevert(SimPool.NotCreator.selector);
        pool.setLine(12, true);
        vm.prank(creator);
        vm.expectRevert(abi.encodeWithSelector(SimPool.NotCovered.selector, 4_000e6));
        pool.setLine(12, true);
        // The money is there, as if prices came in: the line may be switched on, and still is not.
        deal(address(usdc), address(pool), 1_000_000e6);
        _lineState(pool, 12, false, false, true, 0);
        _buyRefused(pool, trader, 12, abi.encodeWithSelector(SimPool.LineOff.selector));
        vm.expectEmit(address(pool));
        emit SimPool.LineSet(12, true);
        vm.prank(creator);
        pool.setLine(12, true);
        _lineState(pool, 12, true, true, true, 0);
        assertEq(pool.onMask(), 0x1fff);
        PaperExam e = _buy(pool, trader, 12);
        assertEq(e.terms().capital, 50_000e6);
        vm.startPrank(creator);
        vm.expectRevert(SimPool.NoSuchLine.selector);
        pool.setLine(24, true);
        vm.expectRevert(SimPool.NoSuchLine.selector);
        pool.setLine(24, false);
        vm.stopPrank();
        vm.expectRevert(SimPool.NoSuchLine.selector);
        pool.lineState(24);
    }

    function test_lines_createRefusesALineTheLockDoesNotCover_andALockUnderTwoOfTheSmallest() public {
        SimPlan memory p = _firstPlan();
        p.on = 0x1fff; // 50 000 on: two payouts of 4 000, and 4 000 locked
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.check(p);
        vm.prank(creator);
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.create{value: 1 ether}(p);
        p.on = 0xfff;
        p.locked = 3_999_999_999; // a cent short of two payouts of 25 000
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.check(p);
        p.on = 0;
        p.locked = 799_999_999; // short of two payouts of 5 000, even with no line on
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.check(p);
        p.on = uint32(1 << 24); // a line past the menu
        p.locked = 1_000_000e6;
        vm.expectRevert(SimPoolFactory.BadPlan.selector);
        factory.check(p);
        // Two payouts of the smallest line, and nothing on: a pool that sells nothing until its creator says.
        p.on = 0;
        p.locked = 800e6;
        uint256 before = usdc.balanceOf(creator);
        SimPool pool = _open(p);
        assertEq(before - usdc.balanceOf(creator), 800e6);
        _lineState(pool, 0, false, false, true, 0);
        _lineState(pool, 4, false, false, false, 800e6);
        _buyRefused(pool, trader, 0, abi.encodeWithSelector(SimPool.LineOff.selector));
        assertEq(pool.floor(), 0, "nothing on, nothing sold: nothing to keep");
    }

    /// A line on sells only while the pool has two of its payouts free; short of that it stays on, and sells again
    /// when the money is back, the creator doing nothing (the choice made, 16 November).
    function test_lines_aSaleNeedsTwoPayoutsFree_theLineStaysOn_andSellsAgainWhenTheMoneyIsBack() public {
        SimPool pool = _pool();
        _buy(pool, other, 1);
        PaperExam stage = _runningStage(pool, trader, 1);
        // 1 600 locked and two prices of 84.575, less the stage's 800: 969.15 free, 630.85 short of two of 800.
        assertEq(pool.free(), 1_600e6 + 2 * (LARGE_PRICE - 425_000) - LARGE_PAYOUT);
        _buyRefused(pool, stranger, 1, abi.encodeWithSelector(SimPool.NotCovered.selector, 630_850_000));
        _lineState(pool, 1, true, false, false, 630_850_000);
        assertEq(pool.onMask(), 3, "still on");
        _lineState(pool, 0, true, true, true, 0);
        // By signature too, refused before any money moves: the signature is not spent.
        (address signer, uint256 pk) = makeAddrAndKey("signer");
        deal(address(usdc), signer, LARGE_PRICE);
        bytes32 nonce = keccak256(abi.encode(CODE, uint256(1), bytes32("s")));
        (uint8 v, bytes32 r, bytes32 s_) = _signed(pk, address(pool), LARGE_PRICE, block.timestamp + 10 minutes, nonce);
        vm.expectRevert(abi.encodeWithSelector(SimPool.NotCovered.selector, 630_850_000));
        pool.buyWithAuthorization(signer, 1, CODE, bytes32("s"), 0, block.timestamp + 10 minutes, v, r, s_);
        assertFalse(usdc.authorizationState(signer, nonce));
        // The stage fails: its payout is free again, and the line sells.
        _fail(stage, trader);
        pool.release(address(stage));
        _lineState(pool, 1, true, true, true, 0);
        pool.buyWithAuthorization(signer, 1, CODE, bytes32("s"), 0, block.timestamp + 10 minutes, v, r, s_);
        _buy(pool, stranger, 1);
    }

    function test_lines_offAlways_forNewSalesOnly_whatWasSoldKeepsItsWay() public {
        SimPool pool = _pool();
        PaperExam exam = _buy(pool, trader, 1);
        vm.expectEmit(address(pool));
        emit SimPool.LineSet(1, false);
        vm.prank(creator);
        pool.setLine(1, false);
        _lineState(pool, 1, false, false, true, 0);
        _buyRefused(pool, other, 1, abi.encodeWithSelector(SimPool.LineOff.selector));
        // The exam sold before goes on: its stage starts on the line, on its rules, and pays 800.
        _pass(exam, trader);
        vm.prank(trader);
        PaperExam stage = PaperExam(pool.fund(address(exam)));
        assertEq(stage.terms().capital, LARGE);
        _pass(stage, trader);
        assertEq(pool.payout(pool.claimPayout(address(stage))).amount, LARGE_PAYOUT);
        // Closed: off still, on no more.
        vm.prank(closer);
        pool.close();
        _lineState(pool, 0, true, false, false, 0);
        assertEq(pool.floor(), 0, "closed: no line sells, and nothing is live");
        vm.startPrank(creator);
        vm.expectRevert(SimPool.Closing.selector);
        pool.setLine(1, true);
        pool.setLine(0, false);
        vm.stopPrank();
        assertEq(pool.onMask(), 0);
    }

    /// The floor is three payouts of the largest line with an exam or a stage not yet done, or on and selling: a
    /// trader with a live exam of 250 000 keeps 60 000 in, the line switched off or not (CFO, 16 November, item 3).
    function test_floor_aLiveExamOf250000KeepsSixtyThousand_theCreatorWithdrawsNoLower() public {
        SimPool pool = _firstPool();
        deal(address(usdc), address(pool), 100_000e6); // prices sold before, as profit
        vm.prank(creator);
        pool.setLine(20, true);
        PaperExam exam = _buy(pool, trader, 20);
        assertEq(exam.terms().capital, 250_000e6);
        vm.startPrank(creator);
        for (uint256 i = 0; i < 24; ++i) pool.setLine(i, false);
        vm.stopPrank();
        assertEq(pool.floor(), 60_000e6, "three payouts of 20 000, the line off");
        uint256 bal = usdc.balanceOf(address(pool));
        uint256 w = pool.withdrawable();
        assertEq(w, bal - 60_000e6);
        _ask(pool, w);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(address(pool)), 60_000e6, "the floor is what stays");
        vm.prank(creator);
        vm.expectRevert(SimPool.TooMuch.selector);
        pool.requestWithdraw(1);
        // The exam fails: nothing live, nothing on, nothing kept; a line on again keeps three of its payouts.
        _fail(exam, trader);
        pool.release(address(exam));
        assertEq(pool.floor(), 0);
        vm.prank(creator);
        pool.setLine(8, true);
        assertEq(pool.floor(), 6_000e6, "three payouts of 25 000");
    }

    /// Two payouts a day, and one of 20 000 third in line: it waits for its day with its money kept from the
    /// creator's withdrawal, and is paid the next day.
    function test_dailyLimit_aPayoutOf20000WaitsItsDay_keepsItsMoney_andIsPaid() public {
        SimPool pool = _firstPool();
        deal(address(usdc), address(pool), 100_000e6);
        vm.prank(creator);
        pool.setLine(20, true);
        address[3] memory who = [trader, other, stranger];
        uint256[3] memory line = [uint256(0), 0, 20];
        PaperExam[3] memory exams;
        for (uint256 i = 0; i < 3; ++i) exams[i] = _buy(pool, who[i], line[i]);
        // The creator asks for all it may now, to carry it out when the large payout waits.
        vm.startPrank(creator);
        pool.requestWithdraw(pool.withdrawable());
        vm.stopPrank();
        uint256[3] memory ids;
        for (uint256 i = 0; i < 3; ++i) {
            ids[i] = _claim(pool, address(_passedStage(pool, exams[i], who[i])));
            _setMark(1_000_000);
        }
        assertEq(pool.owed(), 20_800e6);
        vm.warp(block.timestamp + factory.WITHDRAW_DELAY());
        vm.expectEmit(address(pool));
        emit SimPool.PayoutWaitsLimit(ids[2], uint64(block.timestamp + 24 hours));
        assertEq(pool.pay(10), 2, "two of 400");
        assertEq(pool.owed(), 20_000e6, "20 000 waits");
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(address(pool)), 20_000e6 + 60_000e6, "what it owes, and the floor of the line on");
        vm.warp(block.timestamp + 24 hours);
        assertEq(pool.pay(10), 1);
        assertEq(usdc.balanceOf(stranger), 20_000e6, "paid in full, a day late");
        assertEq(pool.owed(), 0);
        assertEq(usdc.balanceOf(address(pool)), 60_000e6);
    }

    // ── A1 of the pre-deploy check (16 November): an exam passed and not yet funded keeps its payout in the pool
    //    from the pass on, against the creator's withdrawal, new sales and switching lines on.

    function _who(uint256 i) internal returns (address) {
        return makeAddr(string(abi.encodePacked("buyer", vm.toString(i))));
    }

    /// The check's own case: 40 sales, the creator asks for all it may take, five traders pass on the sixth day and
    /// the creator carries the request out on the seventh. Before A1 the five were not in `staged`: the withdrawal
    /// left three payouts, and two of the five payouts were owed for ever on an empty pool.
    function test_A1_passedExamAwaitingFundIsKeptForByWithdraw() public {
        SimPool pool = _pool();
        PaperExam[] memory exams = new PaperExam[](40);
        for (uint256 i = 0; i < 40; ++i) exams[i] = _buy(pool, _who(i), 1);
        uint256 before = pool.withdrawable();
        vm.prank(creator);
        pool.requestWithdraw(before);
        vm.warp(block.timestamp + 6 days);
        for (uint256 i = 0; i < 5; ++i) _pass(exams[i], _who(i));
        assertEq(pool.staged(), 5 * LARGE_PAYOUT, "five passed exams, five payouts staged");
        assertEq(pool.floor(), 5 * LARGE_PAYOUT, "the floor is what they would pay");
        uint256 bal = usdc.balanceOf(address(pool));
        assertEq(pool.withdrawable(), bal - 5 * LARGE_PAYOUT, "withdrawable sees them");
        assertEq(pool.free(), bal - 5 * LARGE_PAYOUT, "and so does free");
        vm.warp(block.timestamp + 1 days + 1);
        vm.prank(creator);
        pool.withdraw();
        assertEq(usdc.balanceOf(address(pool)), 5 * LARGE_PAYOUT, "the five payouts stay");
        address[] memory stages = new address[](5);
        for (uint256 i = 0; i < 5; ++i) {
            vm.prank(_who(i));
            stages[i] = pool.fund(address(exams[i]));
        }
        assertEq(pool.staged(), 5 * LARGE_PAYOUT, "funding moves nothing: staged since the pass");
        vm.prank(closer);
        pool.close();
        for (uint256 i = 0; i < 5; ++i) {
            _pass(PaperExam(stages[i]), _who(i));
            _claim(pool, stages[i]);
        }
        assertEq(pool.staged(), 0, "claimed: owed now");
        vm.warp(block.timestamp + 72 hours);
        for (uint256 d = 0; d < 4; ++d) {
            pool.pay(10);
            vm.warp(block.timestamp + 1 days);
        }
        assertEq(pool.paidCount(), 5, "all five paid");
        assertEq(pool.owed(), 0, "nothing owed");
    }

    /// The pass is told to the pool by the exam itself: no keeper, no call of the trader's.
    function test_A1_theExamTellsThePoolWhenItPasses() public {
        SimPool pool = _pool();
        PaperExam e = _buy(pool, trader, 0);
        assertTrue(e.tellsPool(), "a pool's exam tells it");
        assertFalse(pool.passedOf(address(e)));
        (,, uint64 capital,) = e.result();
        uint64 sz = capital / mark;
        _fill(e, trader, true, sz);
        _setMark(mark * 12 / 10);
        _fill(e, trader, false, sz);
        vm.expectEmit(address(pool));
        emit SimPool.ExamPassed(address(e), SMALL_PAYOUT);
        vm.prank(trader);
        e.graduate();
        assertTrue(pool.passedOf(address(e)));
        assertEq(pool.staged(), SMALL_PAYOUT);
        assertEq(pool.live(), 1, "still live until funded or released");
    }

    /// A stage does not tell the pool: its payout is in `staged` once, from the exam's pass to the claim.
    function test_A1_aStageDoesNotTellThePool() public {
        SimPool pool = _pool();
        PaperExam e = _buy(pool, trader, 1);
        _pass(e, trader);
        vm.prank(trader);
        PaperExam stage = PaperExam(pool.fund(address(e)));
        assertFalse(stage.tellsPool(), "a stage is claimed instead");
        _pass(stage, trader);
        assertEq(pool.staged(), LARGE_PAYOUT, "once");
        pool.claimPayout(address(stage));
        assertEq(pool.staged(), 0);
        assertEq(pool.owed(), LARGE_PAYOUT);
    }

    /// Anything but an exam of the pool, or a second call, changes nothing.
    function test_A1_examPassedFromAnyoneElseChangesNothing() public {
        SimPool pool = _pool();
        PaperExam e = _buy(pool, trader, 1);
        vm.prank(stranger);
        pool.examPassed();
        assertEq(pool.staged(), 0, "a stranger");
        assertFalse(pool.passedOf(stranger));
        _pass(e, trader);
        vm.prank(trader);
        PaperExam stage = PaperExam(pool.fund(address(e)));
        vm.prank(address(e));
        pool.examPassed();
        vm.prank(address(stage));
        pool.examPassed();
        assertEq(pool.staged(), LARGE_PAYOUT, "neither the exam again nor its stage");
    }

    /// A pass not funded within `fundWindow` is released, and its payout is free again: as a stage that ran out.
    function test_A1_aPassNotFundedInTimeIsFreeAgain() public {
        SimPool pool = _pool();
        PaperExam e = _buy(pool, trader, 1);
        uint256 free0 = pool.free();
        _pass(e, trader);
        assertEq(pool.free(), free0 - LARGE_PAYOUT, "kept from the pass");
        vm.expectRevert(SimPool.StillRunning.selector);
        pool.release(address(e));
        vm.warp(block.timestamp + 7 days + 1);
        pool.release(address(e));
        assertEq(pool.staged(), 0, "released: no longer staged");
        assertEq(pool.free(), free0, "free again");
        vm.prank(trader);
        vm.expectRevert(SimPool.Done.selector);
        pool.fund(address(e));
    }

    /// A pass the pool was not told of (here the call is mocked away) is never taken out of `staged` by `release`,
    /// which goes by the pool's own mark, not by the exam's status; its `fund` counts it then, once, so the stage's
    /// claim takes out what was put in (review of cp#310).
    function test_A1_aPassThePoolWasNotToldOfIsPairedByItsMark() public {
        SimPool pool = _pool();
        PaperExam e = _buy(pool, trader, 1);
        PaperExam f = _buy(pool, _who(1), 1);
        vm.mockCall(address(pool), abi.encodeCall(SimPool.examPassed, ()), "");
        _pass(e, trader);
        _pass(f, _who(1));
        vm.clearMockedCalls();
        (PaperExam.Status st,,,) = e.result();
        assertEq(uint8(st), uint8(PaperExam.Status.Passed));
        assertFalse(pool.passedOf(address(e)));
        assertEq(pool.staged(), 0, "not told: not staged");
        vm.prank(_who(1));
        PaperExam stage = PaperExam(pool.fund(address(f)));
        assertTrue(pool.passedOf(address(f)), "counted at its fund");
        assertEq(pool.staged(), LARGE_PAYOUT);
        vm.warp(block.timestamp + 7 days + 1);
        pool.release(address(e));
        assertEq(pool.staged(), LARGE_PAYOUT, "the release of a pass never counted takes nothing out");
        _pass(stage, _who(1));
        pool.claimPayout(address(stage));
        assertEq(pool.staged(), 0, "the claim takes out what the fund put in");
        assertEq(pool.owed(), LARGE_PAYOUT);
    }

    /// A failed exam never was in `staged`, and its release takes nothing out of it.
    function test_A1_aFailedExamTakesNothingOutOfStaged() public {
        SimPool pool = _pool();
        PaperExam a = _buy(pool, trader, 1);
        PaperExam b = _buy(pool, _who(1), 1);
        _pass(a, trader);
        _fail(b, _who(1));
        pool.release(address(b));
        assertEq(pool.staged(), LARGE_PAYOUT, "the pass stays");
    }

    /// A pass takes its payout out of what is free: a line whose two payouts are no longer free stops selling, and
    /// the creator cannot switch it on, until the stage ends or the creator adds money.
    function test_A1_aPassStopsSalesWhenItsPayoutIsNotFree() public {
        SimPool pool = _pool();
        // 1 600 locked and one price: two payouts of the larger line free, just.
        PaperExam e = _buy(pool, trader, 1);
        (, bool sells,,) = pool.lineState(1);
        assertTrue(sells, "two payouts free before the pass");
        _pass(e, trader);
        uint256 short;
        (, sells,, short) = pool.lineState(1);
        assertFalse(sells, "the pass keeps one payout: no longer two free");
        assertEq(short, 2 * LARGE_PAYOUT - pool.free());
        vm.prank(creator);
        pool.setLine(1, false);
        vm.prank(creator);
        vm.expectRevert(abi.encodeWithSelector(SimPool.NotCovered.selector, short));
        pool.setLine(1, true);
        SimSize[] memory m = pool.menu();
        deal(address(usdc), stranger, m[1].price);
        vm.startPrank(stranger);
        usdc.approve(address(pool), m[1].price);
        vm.expectRevert(SimPool.LineOff.selector);
        pool.buy(1, CODE);
        vm.stopPrank();
    }

    /// More passes than the pool's money: sales stop and the creator takes nothing; the payouts wait for the money
    /// in their order, as the queue always did. The creator's withdrawal never makes this happen (I4).
    function test_A1_moreObligationsThanMoneyStopSalesAndWithdrawals() public {
        SimPool pool = _pool();
        PaperExam[] memory exams = new PaperExam[](12);
        for (uint256 i = 0; i < 12; ++i) exams[i] = _buy(pool, _who(i), 1);
        for (uint256 i = 0; i < 12; ++i) _pass(exams[i], _who(i));
        uint256 bal = usdc.balanceOf(address(pool));
        assertGt(pool.staged(), bal, "twelve payouts above the pool's money");
        assertEq(pool.free(), 0);
        assertEq(pool.withdrawable(), 0);
        (, bool sells0,,) = pool.lineState(0);
        (, bool sells1,,) = pool.lineState(1);
        assertFalse(sells0 || sells1, "no line sells");
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

