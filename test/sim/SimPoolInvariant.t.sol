// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test, console2} from "forge-std/Test.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {Rules} from "../../src/Types.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {TermsBook} from "../../src/multi/TermsBook.sol";
import {SimPool, SimPlan, SimSize} from "../../src/sim/SimPool.sol";
import {SimPoolFactory} from "../../src/sim/SimPoolFactory.sol";
import {OracleFollowsMark} from "../PoolHarness.sol";
import {Usdc3009} from "./SimPool.t.sol";

/// Drives one simulated pool at random: purchases, exams and stages passed and failed, stages started,
/// payouts claimed, stopped, lifted and paid, a token that refuses a trader, what it set aside claimed,
/// the creator's withdrawals and money put in, closing and finishing. It counts every dollar that goes
/// into the pool and out of it, so that the invariants can hold the pool to its own books.
contract SimPoolHandler is Test {
    uint32 constant BTC = 3;
    uint64 constant BASE_MARK = 1_000_000;

    SimPool public immutable pool;
    Usdc3009 public immutable usdc;
    address public immutable filler;
    address public immutable checker;
    address public immutable creator;
    /// Where a trader the token refuses takes what was set aside for them: never refused itself.
    address public immutable rescue = makeAddr("rescue");

    address[] internal _actors;
    address[] internal _exams;
    address[] internal _stages;
    mapping(address stage => address) public examOf;
    mapping(address exam => uint256) public stagesOf;

    /// Every exam sold.
    uint256 public purchases;
    /// The pool's USDC in: the locked two payouts, money the creator put in, and each price less the fee.
    uint256 public ghostIn;
    /// The pool's USDC out: payouts, withdrawals (all of each: fee, partner and creator), claims of what was held,
    /// and what `finish` sent.
    uint256 public ghostOut;
    /// What left the pool in `pay`, which must be exactly the payouts it marked paid.
    uint256 public ghostPaidOut;
    uint256 public ghostClaimed;
    /// A `pay` whose outflow differed from the payouts it marked paid.
    uint256 public ghostPayMismatch;
    /// A withdrawal that left less than three payouts of the largest size above what is owed and held.
    uint256 public ghostBelowFloor;
    /// A withdrawal of one unit more than `withdrawable` that went through.
    uint256 public ghostOverWithdraw;
    /// `finish` went through with something still owed or running.
    uint256 public ghostEarlyFinish;

    mapping(bytes32 => uint256) public calls;

    constructor(SimPool pool_, Usdc3009 usdc_, address filler_, address checker_, address creator_, uint256 locked) {
        pool = pool_;
        usdc = usdc_;
        filler = filler_;
        checker = checker_;
        creator = creator_;
        ghostIn = locked;
        for (uint256 i = 0; i < 5; ++i) _actors.push(makeAddr(string(abi.encode("trader", i))));
    }

    // ── what the invariants read ─────────────────────────────────

    function examCount() external view returns (uint256) {
        return _exams.length;
    }

    function stageCount() external view returns (uint256) {
        return _stages.length;
    }

    function exam(uint256 i) external view returns (address) {
        return _exams[i];
    }

    function stage(uint256 i) external view returns (address) {
        return _stages[i];
    }

    function actorCount() external view returns (uint256) {
        return _actors.length;
    }

    function actor(uint256 i) external view returns (address) {
        return _actors[i];
    }

    // ── traders ──────────────────────────────────────────────────

    /// A purchase, its exam passed, its stage started and passed, and the payout claimed: the whole way to a
    /// payout in one call, beside the steps one at a time.
    function earn(uint256 who, uint256 line) external {
        if (pool.closed()) return;
        address e = _buy(who, line);
        try this.passIt(PaperExam(e)) {} catch { return; }
        (, address t,,) = PaperExam(e).result();
        vm.prank(t);
        address st = pool.fund(e);
        _stages.push(st);
        examOf[st] = e;
        stagesOf[e] += 1;
        try this.passIt(PaperExam(st)) {} catch { return; }
        pool.claimPayout(st);
        calls["earn"] += 1;
    }

    function buy(uint256 who, uint256 line) external {
        if (pool.closed()) return;
        _buy(who, line);
    }

    function _buy(uint256 who, uint256 line) internal returns (address e) {
        line = bound(line, 0, 1);
        address t = _actors[who % _actors.length];
        SimSize[] memory m = pool.menu();
        uint256 fee = uint256(m[line].price) * 50 / 10_000;
        deal(address(usdc), t, usdc.balanceOf(t) + m[line].price);
        vm.startPrank(t);
        usdc.approve(address(pool), m[line].price);
        e = pool.buy(line);
        vm.stopPrank();
        _exams.push(e);
        purchases += 1;
        ghostIn += m[line].price - fee;
        calls["buy"] += 1;
    }

    function passExam(uint256 seed) external {
        address e = _pick(_exams, seed, PaperExam.Status.Active);
        if (e == address(0)) return;
        try this.passIt(PaperExam(e)) {
            calls["passExam"] += 1;
        } catch {}
    }

    function passStage(uint256 seed) external {
        address s = _pick(_stages, seed, PaperExam.Status.Active);
        if (s == address(0)) return;
        try this.passIt(PaperExam(s)) {
            calls["passStage"] += 1;
        } catch {}
    }

    function fail(uint256 seed, bool aStage) external {
        address a = _pick(aStage ? _stages : _exams, seed, PaperExam.Status.Active);
        if (a == address(0)) return;
        try this.failIt(PaperExam(a)) {
            calls["fail"] += 1;
        } catch {}
    }

    function fund(uint256 seed) external {
        address e = _pick(_exams, seed, PaperExam.Status.Passed);
        if (e == address(0)) return;
        (, address t,,) = PaperExam(e).result();
        vm.prank(t);
        try pool.fund(e) returns (address s) {
            _stages.push(s);
            examOf[s] = e;
            stagesOf[e] += 1;
            calls["fund"] += 1;
        } catch {}
    }

    // ── anyone ───────────────────────────────────────────────────

    function claim(uint256 seed) external {
        address s = _pick(_stages, seed, PaperExam.Status.Passed);
        if (s == address(0)) return;
        try pool.claimPayout(s) {
            calls["claim"] += 1;
        } catch {}
    }

    function pay(uint256 max) external {
        _pay(bound(max, 1, 5));
    }

    function _pay(uint256 max) internal {
        uint256 before = usdc.balanceOf(address(pool));
        uint256 paidBefore = _sumIn(SimPool.PayoutState.Paid);
        pool.pay(max);
        uint256 out = before - usdc.balanceOf(address(pool));
        uint256 paid = _sumIn(SimPool.PayoutState.Paid) - paidBefore;
        if (out != paid) ghostPayMismatch += 1;
        ghostOut += out;
        ghostPaidOut += paid;
        calls["pay"] += 1;
    }

    function release(uint256 seed, bool aStage) external {
        address[] storage list = aStage ? _stages : _exams;
        if (list.length == 0) return;
        address a = list[seed % list.length];
        (,, bool done) = pool.accountOf(a);
        if (done) return;
        try PaperExam(a).expire() {} catch {}
        try pool.release(a) {
            calls["release"] += 1;
        } catch {}
    }

    function warp(uint256 secs) external {
        secs = bound(secs, 1 minutes, 4 days);
        vm.warp(block.timestamp + secs);
        vm.roll(block.number + 1);
        calls["warp"] += 1;
    }

    /// The token refuses every transfer to one of the traders from now on.
    function refuse(uint256 who) external {
        if (!_rare(who, 4)) return;
        usdc.block_(_actors[who % _actors.length]);
        calls["refuse"] += 1;
    }

    function claimHeld(uint256 who) external {
        address t = _actors[who % _actors.length];
        uint256 amount = pool.heldOf(t);
        if (amount == 0) return;
        vm.prank(t);
        pool.claimHeld(rescue);
        ghostOut += amount;
        ghostClaimed += amount;
        calls["claimHeld"] += 1;
    }

    // ── the checker ──────────────────────────────────────────────

    function stop(uint256 seed, uint256 pairSeed, bool reasonTwo) external {
        uint256 n = pool.payoutCount();
        if (n == 0 || _exams.length < 2) return;
        uint256 id = seed % n;
        SimPool.Payout memory p = pool.payout(id);
        if (p.state != SimPool.PayoutState.Due || p.wasStopped || block.timestamp >= p.dueAt) return;
        address paired = _exams[pairSeed % _exams.length];
        if (paired == p.account) paired = _exams[(pairSeed + 1) % _exams.length];
        bytes32[] memory trades = new bytes32[](2);
        trades[0] = keccak256(abi.encode("fill", id, 0));
        trades[1] = keccak256(abi.encode("fill", id, 1));
        bytes32[] memory links = new bytes32[](reasonTwo ? 1 : 0);
        if (reasonTwo) links[0] = keccak256(abi.encode("link", id));
        vm.prank(checker);
        pool.stopPayout(id, reasonTwo ? 2 : 1, paired, trades, links);
        calls["stop"] += 1;
    }

    function lift(uint256 seed) external {
        uint256 n = pool.payoutCount();
        if (n == 0 || pool.finished()) return;
        uint256 id = seed % n;
        if (pool.payout(id).state != SimPool.PayoutState.Stopped) return;
        vm.prank(checker);
        pool.liftStop(id);
        calls["lift"] += 1;
    }

    // ── the creator ──────────────────────────────────────────────

    function withdraw(uint256 amount) external {
        if (pool.finished()) return;
        uint256 w = pool.withdrawable();
        vm.prank(creator);
        try pool.withdraw(w + 1) {
            ghostOverWithdraw += 1;
            ghostOut += w + 1;
        } catch {}
        if (w == 0) return;
        amount = bound(amount, 1, w);
        vm.prank(creator);
        pool.withdraw(amount);
        ghostOut += amount;
        uint256 keep = pool.owed() + pool.held() + uint256(pool.largestPayout()) * 3;
        if (usdc.balanceOf(address(pool)) < keep) ghostBelowFloor += 1;
        calls["withdraw"] += 1;
    }

    function addToPot(uint256 amount) external {
        if (pool.finished()) return;
        amount = bound(amount, 1, 2_000e6);
        deal(address(usdc), creator, usdc.balanceOf(creator) + amount);
        vm.startPrank(creator);
        usdc.approve(address(pool), amount);
        pool.addToPot(amount);
        vm.stopPrank();
        ghostIn += amount;
        calls["addToPot"] += 1;
    }

    /// Rarely: a closed pool sells nothing more, so a close early in a run would leave little to drive.
    function close(uint256 seed) external {
        if (pool.closed() || !_rare(seed, 24)) return;
        vm.prank(creator);
        pool.close();
        calls["close"] += 1;
    }

    function finish() external {
        _finish();
    }

    /// Rarely, the whole end of a pool in one call: closed, every passed stage claimed, every exam and stage run
    /// out and released, the queue paid as far as the money goes, and finished if nothing is owed or running.
    /// A trader the token refused may still claim what was set aside afterwards.
    function windDown(uint256 seed) external {
        if (pool.finished() || !_rare(seed, 16)) return;
        if (!pool.closed()) {
            vm.prank(creator);
            pool.close();
            calls["close"] += 1;
        }
        for (uint256 i = 0; i < _stages.length; ++i) {
            (PaperExam.Status st,,,) = PaperExam(_stages[i]).result();
            (,, bool done) = pool.accountOf(_stages[i]);
            if (st == PaperExam.Status.Passed && !done) pool.claimPayout(_stages[i]);
        }
        vm.warp(block.timestamp + 31 days);
        vm.roll(block.number + 1);
        _endAll(_exams);
        _endAll(_stages);
        _pay(type(uint256).max);
        _finish();
        calls["windDown"] += 1;
    }

    function _endAll(address[] storage list) internal {
        for (uint256 i = 0; i < list.length; ++i) {
            (,, bool done) = pool.accountOf(list[i]);
            if (done) continue;
            try PaperExam(list[i]).expire() {} catch {}
            pool.release(list[i]);
        }
    }

    function _finish() internal {
        if (!pool.closed() || pool.finished()) return;
        bool owing = pool.owed() != 0 || pool.live() != 0;
        uint256 before = usdc.balanceOf(address(pool));
        vm.prank(creator);
        try pool.finish() {
            if (owing) ghostEarlyFinish += 1;
            ghostOut += before - usdc.balanceOf(address(pool));
            calls["finish"] += 1;
        } catch {}
    }

    // ── the trader's trades, called through `this` so that a refusal undoes all of it ──

    /// Long at about 1x, the price up 20 %, flat, the step passed, and the price back: nobody holds a
    /// position between two calls, so the price moves nobody else.
    function passIt(PaperExam e) external {
        require(msg.sender == address(this));
        (, address t, uint64 capital,) = e.result();
        uint64 sz = capital / BASE_MARK;
        _fill(e, t, true, sz);
        CoreSimulatorLib.setMarkPx(BTC, BASE_MARK * 12 / 10);
        _fill(e, t, false, sz);
        vm.prank(t);
        e.graduate();
        CoreSimulatorLib.setMarkPx(BTC, BASE_MARK);
    }

    /// Long at about 1x, the price down past the drawdown line, the rule called, and the price back.
    function failIt(PaperExam e) external {
        require(msg.sender == address(this));
        (, address t, uint64 capital,) = e.result();
        _fill(e, t, true, capital / BASE_MARK);
        CoreSimulatorLib.setMarkPx(BTC, BASE_MARK * 90 / 100);
        e.breach();
        CoreSimulatorLib.setMarkPx(BTC, BASE_MARK);
    }

    function _fill(PaperExam e, address t, bool isBuy, uint64 sz) internal {
        vm.prank(t);
        e.place(BTC, isBuy, sz);
        vm.roll(block.number + 1);
        vm.prank(filler);
        e.fill();
    }

    /// True about once in `n` calls, whatever values the fuzzer favours (it likes zero).
    function _rare(uint256 seed, uint256 n) internal pure returns (bool) {
        return uint256(keccak256(abi.encode(seed))) % n == 0;
    }

    /// The first account of `list` from `seed` on in `status` and not done in the pool.
    function _pick(address[] storage list, uint256 seed, PaperExam.Status status) internal view returns (address) {
        uint256 n = list.length;
        for (uint256 k = 0; k < n; ++k) {
            address a = list[(seed + k) % n];
            (PaperExam.Status s,,,) = PaperExam(a).result();
            (,, bool done) = pool.accountOf(a);
            if (s == status && !done) return a;
        }
        return address(0);
    }

    function _sumIn(SimPool.PayoutState state) internal view returns (uint256 sum) {
        uint256 n = pool.payoutCount();
        for (uint256 i = 0; i < n; ++i) {
            SimPool.Payout memory p = pool.payout(i);
            if (p.state == state) sum += p.amount;
        }
    }
}

/// The money of a simulated pool, held to its books whatever is called in whatever order.
///
/// Not an invariant, by design: that the pool's USDC covers every payout owed. Passes can come faster than
/// prices; a payout the pool cannot cover yet waits in order (`PayoutQueued`) and is paid first from the prices
/// that come in (docs/SIM-POOL.md, "A pass comes faster than prices"). What does hold: what is set aside for
/// traders is always there, a withdrawal never takes the pool below three payouts above what it owes, and every
/// dollar in and out is accounted for.
contract SimPoolInvariantTest is Test {
    uint32 constant BTC = 3;
    uint64 constant SMALL = 5_000e6;
    uint64 constant LARGE = 10_000e6;

    HyperCore hyperCore;
    Usdc3009 usdc;
    SimPoolFactory factory;
    SimPool pool;
    SimPoolHandler handler;

    address filler = makeAddr("filler");
    address checker = makeAddr("checker");
    address platform = makeAddr("platform");
    address creator = makeAddr("creator");

    function setUp() public {
        hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        vm.etch(0x0000000000000000000000000000000000000807, type(OracleFollowsMark).runtimeCode);
        hyperCore.registerPerpAssetInfo(
            BTC,
            PrecompileLib.PerpAssetInfo({coin: "BTC", marginTableId: 54, szDecimals: 5, maxLeverage: 40, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(BTC, 1_000_000);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);

        usdc = new Usdc3009();
        TermsBook book = new TermsBook();
        bytes32 termsHash = book.publish("The simulated pool's terms, every clause written out.");
        factory = new SimPoolFactory(IERC20(address(usdc)), address(new PaperExam()), address(new SimPool()), address(this));
        factory.setFiller(filler);
        factory.setChecker(checker);
        factory.setFeeRecipient(platform);
        factory.setAsset(BTC, 1000);
        factory.setTermsBook(book);
        vm.deal(creator, 10 ether);
        deal(address(usdc), creator, 100_000e6);
        vm.prank(creator);
        usdc.approve(address(factory), type(uint256).max);

        SimPlan memory p;
        p.menu = new SimSize[](2);
        p.menu[0] = SimSize({size: SMALL, price: 45e6});
        p.menu[1] = SimSize({size: LARGE, price: 85e6});
        p.examRules = _rules();
        p.exam = _terms();
        p.fundedRules = _rules();
        p.funded = _terms();
        p.fundWindow = 7 days;
        p.fromBook = true;
        p.maxWorseBps = 50;
        p.terms = termsHash;
        vm.prank(creator);
        pool = SimPool(factory.create{value: 1 ether}(p));

        handler = new SimPoolHandler(pool, usdc, filler, checker, creator, usdc.balanceOf(address(pool)));
        targetContract(address(handler));
        bytes4[] memory s = new bytes4[](29);
        s[0] = SimPoolHandler.buy.selector;
        s[1] = SimPoolHandler.buy.selector;
        s[2] = SimPoolHandler.passExam.selector;
        s[3] = SimPoolHandler.passExam.selector;
        s[4] = SimPoolHandler.passStage.selector;
        s[5] = SimPoolHandler.fail.selector;
        s[6] = SimPoolHandler.fund.selector;
        s[7] = SimPoolHandler.claim.selector;
        s[8] = SimPoolHandler.pay.selector;
        s[9] = SimPoolHandler.release.selector;
        s[10] = SimPoolHandler.warp.selector;
        s[11] = SimPoolHandler.refuse.selector;
        s[12] = SimPoolHandler.claimHeld.selector;
        s[13] = SimPoolHandler.stop.selector;
        s[14] = SimPoolHandler.lift.selector;
        s[15] = SimPoolHandler.withdraw.selector;
        s[16] = SimPoolHandler.addToPot.selector;
        s[17] = SimPoolHandler.close.selector;
        s[18] = SimPoolHandler.finish.selector;
        // Weights: the steps a payout needs come up more often than the rest.
        s[19] = SimPoolHandler.fund.selector;
        s[20] = SimPoolHandler.passStage.selector;
        s[21] = SimPoolHandler.claim.selector;
        s[22] = SimPoolHandler.warp.selector;
        s[23] = SimPoolHandler.warp.selector;
        s[24] = SimPoolHandler.pay.selector;
        s[25] = SimPoolHandler.claimHeld.selector;
        s[26] = SimPoolHandler.windDown.selector;
        s[27] = SimPoolHandler.earn.selector;
        s[28] = SimPoolHandler.earn.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: s}));
    }

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

    function _sum(SimPool.PayoutState state) internal view returns (uint256 sum, uint256 count) {
        uint256 n = pool.payoutCount();
        for (uint256 i = 0; i < n; ++i) {
            SimPool.Payout memory p = pool.payout(i);
            if (p.state == state) {
                sum += p.amount;
                count += 1;
            }
        }
    }

    /// I1. Every dollar in and out is accounted for: the pool's USDC is what came in less what went out.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_moneyIsAccountedFor() public view {
        assertEq(usdc.balanceOf(address(pool)), handler.ghostIn() - handler.ghostOut());
    }

    /// I2. What the token refused is set aside, and always there: the pool's USDC never falls below it.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_whatIsHeldIsThere() public view {
        assertGe(usdc.balanceOf(address(pool)), pool.held());
        (uint256 heldPayouts,) = _sum(SimPool.PayoutState.Held);
        assertEq(pool.held(), heldPayouts - handler.ghostClaimed(), "held: refused payouts less what was claimed");
        uint256 byTrader;
        for (uint256 i = 0; i < handler.actorCount(); ++i) byTrader += pool.heldOf(handler.actor(i));
        assertEq(byTrader, pool.held(), "held: the traders' own amounts");
    }

    /// I3. `paidTotal` is the sum of what was paid, and what left the pool in `pay` is exactly that.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_paidTotalIsTheSumOfPayments() public view {
        (uint256 paid, uint256 count) = _sum(SimPool.PayoutState.Paid);
        assertEq(pool.paidTotal(), paid);
        assertEq(pool.paidCount(), count);
        assertEq(pool.paidTotal(), handler.ghostPaidOut(), "what left the pool in pay");
        assertEq(handler.ghostPayMismatch(), 0, "a pay sent other than what it marked paid");
    }

    /// I4. The creator never withdraws below three payouts of the largest size above what is owed and held,
    ///     and not one unit past `withdrawable`.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_creatorKeepsThreePayouts() public view {
        assertEq(handler.ghostBelowFloor(), 0, "a withdrawal left less than three payouts");
        assertEq(handler.ghostOverWithdraw(), 0, "a withdrawal past withdrawable went through");
        if (!pool.finished()) {
            uint256 keep = pool.owed() + pool.held() + uint256(pool.largestPayout()) * 3;
            uint256 bal = usdc.balanceOf(address(pool));
            assertEq(pool.withdrawable(), bal > keep ? bal - keep : 0);
        }
    }

    /// I5. One purchase, at most one payout: each payout is of a stage, each stage of one exam, each exam
    ///     funded at most once, and no stage paid twice.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_onePurchaseAtMostOnePayout() public view {
        uint256 n = pool.payoutCount();
        assertLe(handler.stageCount(), handler.examCount());
        assertLe(handler.examCount(), handler.purchases());
        assertLe(n, handler.stageCount());
        for (uint256 i = 0; i < n; ++i) {
            address s = pool.payout(i).account;
            (SimPool.Kind kind,,) = pool.accountOf(s);
            assertEq(uint8(kind), uint8(SimPool.Kind.Stage), "a payout of something other than a stage");
            assertEq(handler.stagesOf(handler.examOf(s)), 1, "an exam funded twice");
            for (uint256 j = 0; j < i; ++j) assertTrue(pool.payout(j).account != s, "one stage, two payouts");
        }
    }

    /// I6. `owed` is every payout due and neither paid, held nor stopped.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_owedIsTheDuePayouts() public view {
        (uint256 due,) = _sum(SimPool.PayoutState.Due);
        assertEq(pool.owed(), due);
    }

    /// I7. The counts of stops on the page: those standing are stops less lifts.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_standingStopsAreStopsLessLifts() public view {
        (, uint256 stopped) = _sum(SimPool.PayoutState.Stopped);
        assertEq(pool.stoppedCount() - pool.liftedCount(), stopped);
    }

    /// I8. Nothing due is passed over: below the head of the queue no payout is still due.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_noDuePayoutBehindTheHead() public view {
        uint256 head = pool.head();
        assertLe(head, pool.payoutCount());
        for (uint256 i = 0; i < head; ++i) {
            assertTrue(pool.payout(i).state != SimPool.PayoutState.Due, "a due payout behind the head");
        }
    }

    /// I9. `live` counts the exams and stages not done, and a finished pool holds only what is set aside.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    function invariant_liveAndFinished() public view {
        uint256 running;
        for (uint256 i = 0; i < handler.examCount(); ++i) {
            (,, bool done) = pool.accountOf(handler.exam(i));
            if (!done) running += 1;
        }
        for (uint256 i = 0; i < handler.stageCount(); ++i) {
            (,, bool done) = pool.accountOf(handler.stage(i));
            if (!done) running += 1;
        }
        assertEq(pool.live(), running);
        assertEq(handler.ghostEarlyFinish(), 0, "finished with something owed or running");
        if (pool.finished()) assertEq(usdc.balanceOf(address(pool)), pool.held(), "a finished pool keeps only what is held");
    }

    /// Not an invariant: what each run reached, printed with `-vv`, so that a run that drove nothing does not pass
    /// unseen for one that did.
    function afterInvariant() external view {
        string[19] memory names = ["earn", "buy", "passExam", "passStage", "fail", "fund", "claim", "pay", "release", "warp",
            "refuse", "claimHeld", "stop", "lift", "withdraw", "addToPot", "close", "finish", "windDown"];
        string memory line = "reached:";
        for (uint256 i = 0; i < names.length; ++i) {
            line = string.concat(line, " ", names[i], "=", vm.toString(handler.calls(bytes32(bytes(names[i])))));
        }
        line = string.concat(line, " payouts=", vm.toString(pool.payoutCount()), " paid=", vm.toString(pool.paidCount()));
        console2.log(line);
    }
}
