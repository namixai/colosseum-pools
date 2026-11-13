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
/// the creator's withdrawals and money put in, closing (by the creator from the pool's term on, or by the
/// platform's closer at any time) and finishing. It counts every dollar that goes into the pool and out of
/// it, so that the invariants can hold the pool to its own books.
contract SimPoolHandler is Test {
    uint32 constant BTC = 3;
    uint64 constant BASE_MARK = 1_000_000;

    SimPool public immutable pool;
    Usdc3009 public immutable usdc;
    address public immutable filler;
    address public immutable checker;
    address public immutable creator;
    /// The factory's closer: the only one who may close the pool before its term.
    address public immutable closer;
    /// Neither creator, checker nor closer: whatever only they may do, it may not.
    address public immutable stranger = makeAddr("stranger");
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
    /// A request to withdraw one unit more than `withdrawable` that went through.
    uint256 public ghostOverWithdraw;
    /// The standing request as the handler made it: how much, and when.
    uint256 public ghostRequested;
    uint256 public ghostRequestedAt;
    /// Money the creator got from the pool other than by `withdraw()` of a request `WITHDRAW_DELAY` old and not
    /// lapsed, or by `finish` `WITHDRAW_DELAY` after the close; or a request, a cancel or a close that moved money.
    uint256 public ghostInstant;
    /// A withdrawal that paid other than the less of what was asked and what `withdrawable` allowed then.
    uint256 public ghostWithdrawMismatch;
    /// A stranger's cancel of a request that went through.
    uint256 public ghostBadCancel;
    /// A payout paid (or set aside for a refused trader) before its 72 hours, unapproved before its 7 days, or
    /// that was not waiting when `pay` began (stopped, say).
    uint256 public ghostEarlyPay;
    /// Who approves payouts, named by the creator in setUp.
    address public approver = makeAddr("approver");
    /// `finish` went through with something still owed, running, or kept for a stop.
    uint256 public ghostEarlyFinish;
    /// A finish by the creator or the closer refused with the pool closed a week ago, nothing owed, running or kept for a
    /// stop.
    uint256 public ghostFinishRefused;
    /// A `pay` that paid or set aside more than was free of what is held and what stops keep.
    uint256 public ghostSpentKept;
    /// In a wind-down, past every deadline and window: a passed stage that would not take its claim, or an
    /// account that would not be released. Recorded rather than reverted, so the fuzzer cannot pass over it.
    uint256 public ghostStuck;

    /// The trading groups' codes the handler may set, the keccak256 of each text, and each code's own partner.
    bytes32[] internal _codeKeys;
    address[] internal _partners;
    mapping(address => uint256) internal _keyOf;
    /// What each code's sales brought, and all of them: each price less the platform's part.
    mapping(bytes32 code => uint256) public ghostBroughtOf;
    uint256 public ghostBrought;
    /// A code's partner and share when a sale first named it: fixed from then on.
    mapping(bytes32 code => bool) public ghostUsed;
    mapping(bytes32 code => address) public firstPartner;
    mapping(bytes32 code => uint16) public firstBps;
    /// A `setCode` that went through on a code a sale had named, or was refused on one no sale had.
    uint256 public ghostCodeChanged;
    uint256 public ghostCodeRefused;
    /// A withdrawal or `finish` whose partners' parts, creator's part or platform's fee were not the shares.
    uint256 public ghostShareMismatch;
    /// What the token refused to send to partners, set aside for them.
    uint256 public ghostPartnerHeld;
    /// Every partner's part of every withdrawal, sent or set aside: to see that runs reach shares at all.
    uint256 public ghostToPartners;
    uint256 internal _salt;

    /// What the creator put in: the locked two payouts and every `addToPot`. Before the term, until the pool is
    /// finished, the pool's `principal` is exactly this: no path gives the creator any of it back.
    uint256 public ghostPut;
    /// What the platform's recipient got: 0.5 % of each price and of each withdrawal, and nothing else.
    uint256 public ghostPlatform;
    /// When the pool was closed, and whether by the closer.
    uint256 public ghostClosedAt;
    bool public ghostClosedByCloser;
    /// A `close` before the term that went through from the creator, or a `close` or `finish` from a stranger.
    uint256 public ghostBadClose;

    mapping(string => uint256) public calls;

    constructor(
        SimPool pool_,
        Usdc3009 usdc_,
        address filler_,
        address checker_,
        address creator_,
        address closer_,
        uint256 locked
    ) {
        pool = pool_;
        usdc = usdc_;
        filler = filler_;
        checker = checker_;
        creator = creator_;
        closer = closer_;
        ghostIn = locked;
        ghostPut = locked;
        for (uint256 i = 0; i < 5; ++i) {
            (address a, uint256 key) = makeAddrAndKey(string(abi.encode("trader", i)));
            _actors.push(a);
            _keyOf[a] = key;
        }
        for (uint256 i = 0; i < 6; ++i) {
            _codeKeys.push(keccak256(abi.encode("GROUP", i)));
            _partners.push(makeAddr(string(abi.encode("group owner", i))));
        }
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

    function codeCount() external view returns (uint256) {
        return _codeKeys.length;
    }

    function codeKey(uint256 i) external view returns (bytes32) {
        return _codeKeys[i];
    }

    function partner(uint256 i) external view returns (address) {
        return _partners[i];
    }

    /// Everyone something can be set aside for: the traders and the codes' partners.
    function holderCount() external view returns (uint256) {
        return _actors.length + _partners.length;
    }

    function holder(uint256 i) public view returns (address) {
        return i < _actors.length ? _actors[i] : _partners[i - _actors.length];
    }

    // ── traders ──────────────────────────────────────────────────

    /// A purchase, its exam passed, its stage started and passed, and the payout claimed: the whole way to a
    /// payout in one call, beside the steps one at a time.
    function earn(uint256 who, uint256 line) external {
        if (pool.closed()) return;
        address e = _buy(who, line, who >> 8);
        if (e == address(0)) return;
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

    function buy(uint256 who, uint256 line, uint256 code) external {
        if (pool.closed()) return;
        _buy(who, line, code);
    }

    function _buy(uint256 who, uint256 line, uint256 codeSeed) internal returns (address e) {
        bytes32 code = _saleCode(codeSeed);
        if (code == bytes32(0)) return address(0);
        line = bound(line, 0, 1);
        address t = _actors[who % _actors.length];
        SimSize[] memory m = pool.menu();
        deal(address(usdc), t, usdc.balanceOf(t) + m[line].price);
        vm.startPrank(t);
        usdc.approve(address(pool), m[line].price);
        e = pool.buy(line, code);
        vm.stopPrank();
        _sold(e, code, m[line].price);
        calls["buy"] += 1;
    }

    /// Ten purchases at once, over the codes on sale: most exams fail, and their prices are the pool's profit.
    function sales(uint256 seed) external {
        if (pool.closed()) return;
        for (uint256 i = 0; i < 10; ++i) {
            if (_buy(seed % 1000 + i, seed >> (i + 1), (seed >> 16) % 1000 + i) == address(0)) return;
        }
        calls["sales"] += 1;
    }

    /// A purchase by the trader's signature (EIP-3009), sent by a relayer: the signed nonce carries the code.
    function buySigned(uint256 who, uint256 line, uint256 codeSeed) external {
        if (pool.closed()) return;
        bytes32 code = _saleCode(codeSeed);
        if (code == bytes32(0)) return;
        line = bound(line, 0, 1);
        address t = _actors[who % _actors.length];
        SimSize[] memory m = pool.menu();
        deal(address(usdc), t, usdc.balanceOf(t) + m[line].price);
        bytes32 salt = bytes32(++_salt);
        uint256 until = block.timestamp + 1 hours;
        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                usdc.DOMAIN_SEPARATOR(),
                keccak256(
                    abi.encode(
                        usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(),
                        t,
                        address(pool),
                        uint256(m[line].price),
                        0,
                        until,
                        keccak256(abi.encode(code, salt))
                    )
                )
            )
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(_keyOf[t], digest);
        vm.prank(rescue); // anyone sends it
        address e = pool.buyWithAuthorization(t, line, code, salt, 0, until, v, r, s);
        _sold(e, code, m[line].price);
        calls["buySigned"] += 1;
    }

    function _sold(address e, bytes32 code, uint64 price) internal {
        uint256 got = price - uint256(price) * 50 / 10_000;
        ghostPlatform += uint256(price) * 50 / 10_000;
        _exams.push(e);
        purchases += 1;
        ghostIn += got;
        ghostBrought += got;
        ghostBroughtOf[code] += got;
        if (!ghostUsed[code]) {
            ghostUsed[code] = true;
            (address p, uint16 bps,,,) = pool.codeOf(code);
            firstPartner[code] = p;
            firstBps[code] = bps;
        }
    }

    /// The first code on sale (a partner set) from `seed` on, or none.
    function _saleCode(uint256 seed) internal view returns (bytes32) {
        uint256 n = _codeKeys.length;
        for (uint256 k = 0; k < n; ++k) {
            bytes32 code = _codeKeys[(seed % n + k) % n];
            (address p,,,,) = pool.codeOf(code);
            if (p != address(0)) return code;
        }
        return bytes32(0);
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

    /// The approver approves one waiting payout, now and then; a stranger must be refused.
    function approve(uint256 seed) external {
        uint256 n = pool.payoutCount();
        if (n == 0 || seed % 3 == 0) return;
        uint256[] memory ids = new uint256[](1);
        ids[0] = (seed >> 8) % n;
        SimPool.Payout memory p = pool.payout(ids[0]);
        if (p.state != SimPool.PayoutState.Due || p.approved) return;
        vm.prank(stranger);
        try pool.approvePayouts(ids) {
            ghostEarlyPay += 1;
        } catch {}
        vm.prank(approver);
        pool.approvePayouts(ids);
        calls["approve"] += 1;
    }

    function _pay(uint256 max) internal {
        uint256 n = pool.payoutCount();
        SimPool.PayoutState[] memory was = new SimPool.PayoutState[](n);
        for (uint256 i = 0; i < n; ++i) was[i] = pool.payout(i).state;
        _payAndCount(max);
        for (uint256 i = 0; i < n; ++i) {
            SimPool.Payout memory p = pool.payout(i);
            if (p.state == was[i]) continue;
            // Only a waiting payout changes in `pay`, to paid or set aside: never a stopped or final one.
            if (was[i] != SimPool.PayoutState.Due) ghostEarlyPay += 1;
            if (block.timestamp < p.dueAt) ghostEarlyPay += 1;
            if (!p.approved && block.timestamp < pool.payoutHardAt(i)) ghostEarlyPay += 1;
        }
    }

    function _payAndCount(uint256 max) internal {
        uint256 before = usdc.balanceOf(address(pool));
        uint256 kept = pool.held() + pool.reserved();
        uint256 free = before > kept ? before - kept : 0;
        uint256 paidBefore = _sumIn(SimPool.PayoutState.Paid);
        uint256 heldBefore = _sumIn(SimPool.PayoutState.Held);
        pool.pay(max);
        uint256 out = before - usdc.balanceOf(address(pool));
        uint256 paid = _sumIn(SimPool.PayoutState.Paid) - paidBefore;
        if (out != paid) ghostPayMismatch += 1;
        // Paid and newly held alike were covered from what was free: never from what is held or kept for stops.
        if (paid + _sumIn(SimPool.PayoutState.Held) - heldBefore > free) ghostSpentKept += 1;
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

    /// Past a stop's 14 days at once, so that stops are made final and not only lifted.
    function pastReserve() external {
        vm.warp(block.timestamp + _reserve() + 1 hours);
        vm.roll(block.number + 1);
        calls["pastReserve"] += 1;
    }

    /// The token refuses every transfer to one of the traders, or to one of the codes' partners, from now on.
    function refuse(uint256 who) external {
        if (!_rare(who, 4)) return;
        usdc.block_(holder(who % (_actors.length + _partners.length)));
        calls["refuse"] += 1;
    }

    function claimHeld(uint256 who) external {
        address t = holder(who % (_actors.length + _partners.length));
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
        // Until it is paid, approved or not.
        if (p.state != SimPool.PayoutState.Due || p.wasStopped) return;
        address paired = _exams[pairSeed % _exams.length];
        if (paired == p.account) paired = _exams[(pairSeed % _exams.length + 1) % _exams.length];
        bytes32[] memory trades = new bytes32[](2);
        trades[0] = keccak256(abi.encode("fill", id, 0));
        trades[1] = keccak256(abi.encode("fill", id, 1));
        bytes32[] memory links = new bytes32[](reasonTwo ? 1 : 0);
        if (reasonTwo) links[0] = keccak256(abi.encode("link", id));
        vm.prank(checker);
        if (pairSeed % 5 == 0) {
            // The result disputed: no pair, a transaction, the hash of the dispute's text.
            bytes32[] memory text = new bytes32[](1);
            text[0] = keccak256(abi.encode("dispute", id));
            pool.stopPayout(id, 3, address(0), trades, text);
        } else {
            pool.stopPayout(id, reasonTwo ? 2 : 1, paired, trades, links);
        }
        calls["stop"] += 1;
    }

    function lift(uint256 seed) external {
        uint256 n = pool.payoutCount();
        if (n == 0 || pool.finished()) return;
        uint256 id = seed % n;
        SimPool.Payout memory p = pool.payout(id);
        if (p.state != SimPool.PayoutState.Stopped || block.timestamp >= uint256(p.stoppedAt) + _reserve()) return;
        vm.prank(checker);
        pool.liftStop(id);
        calls["lift"] += 1;
    }

    // ── a stopped payout's trader, and anyone ────────────────────

    function objectStop(uint256 seed) external {
        uint256 n = pool.payoutCount();
        if (n == 0) return;
        uint256 id = seed % n;
        SimPool.Payout memory p = pool.payout(id);
        if (p.state != SimPool.PayoutState.Stopped || block.timestamp >= uint256(p.stoppedAt) + _reserve()) return;
        vm.prank(p.trader);
        pool.objectStop(id, keccak256(abi.encode("those were hedges", id)));
        calls["objectStop"] += 1;
    }

    function finalizeStop(uint256 seed) external {
        uint256 n = pool.payoutCount();
        if (n == 0) return;
        uint256 id = seed % n;
        SimPool.Payout memory p = pool.payout(id);
        if (p.state != SimPool.PayoutState.Stopped || block.timestamp < uint256(p.stoppedAt) + _reserve()) return;
        pool.finalizeStop(id);
        calls["finalizeStop"] += 1;
    }

    function _reserve() internal view returns (uint256) {
        return pool.factory().STOP_RESERVE();
    }

    // ── the creator ──────────────────────────────────────────────

    /// Asks to withdraw at most what `withdrawable` allows; one unit more must be refused. Nothing moves.
    function requestWithdraw(uint256 amount) external {
        if (pool.finished()) return;
        uint256 w = pool.withdrawable();
        vm.prank(creator);
        try pool.requestWithdraw(w + 1) {
            ghostOverWithdraw += 1;
            return;
        } catch {}
        if (w == 0) return;
        amount = bound(amount, 1, w);
        uint256 c0 = usdc.balanceOf(creator);
        uint256 p0 = usdc.balanceOf(address(pool));
        vm.prank(creator);
        pool.requestWithdraw(amount);
        if (usdc.balanceOf(creator) != c0 || usdc.balanceOf(address(pool)) != p0) ghostInstant += 1;
        ghostRequested = amount;
        ghostRequestedAt = block.timestamp;
        calls["request"] += 1;
    }

    /// Carries out the standing request: now and then at once, which must be refused before the week is out;
    /// otherwise from when it is ready to a day past its window, so that some requests lapse.
    function withdraw(uint256 seed) external {
        if (pool.finished() || pool.requested() == 0) return;
        uint256 ready = pool.readyAt();
        if (pool.readyAt() != ghostRequestedAt + _delay() || pool.requested() != ghostRequested) ghostWithdrawMismatch += 1;
        if (block.timestamp < ready) {
            if (seed % 4 == 0) {
                uint256 p0 = usdc.balanceOf(address(pool));
                vm.prank(creator);
                try pool.withdraw() {
                    ghostInstant += 1;
                    ghostOut += p0 - usdc.balanceOf(address(pool));
                } catch {}
                return;
            }
            vm.warp(ready + bound(seed >> 8, 0, uint256(_window()) + 1 days));
            vm.roll(block.number + 1);
        }
        uint256 w = pool.withdrawable();
        uint256 r = pool.requested();
        uint256 amount = r < w ? r : w;
        bool good = block.timestamp < uint256(ready) + _window();
        Before memory b = _before();
        vm.prank(creator);
        try pool.withdraw() {
            uint256 out = b.bal - usdc.balanceOf(address(pool));
            ghostOut += out;
            if (!good || amount == 0 || block.timestamp < ghostRequestedAt + _delay()) ghostInstant += 1;
            if (out != amount - _checkShares(b, amount)) ghostWithdrawMismatch += 1;
            if (pool.requested() != 0 || pool.readyAt() != 0) ghostWithdrawMismatch += 1;
            uint256 floor = uint256(pool.largestPayout()) * 3;
            if (pool.staged() > floor) floor = pool.staged();
            uint256 keep = pool.owed() + pool.held() + pool.reserved() + floor;
            if (usdc.balanceOf(address(pool)) < keep) ghostBelowFloor += 1;
            calls["withdraw"] += 1;
        } catch {
            // Ready, not lapsed, and something to take: a refusal is a finding.
            if (good && amount != 0) ghostStuck += 1;
        }
    }

    /// Cancels the standing request: the creator or the closer; a stranger must be refused. Nothing moves.
    function cancelWithdraw(uint256 seed) external {
        if (pool.requested() == 0) return;
        vm.prank(stranger);
        try pool.cancelWithdraw() {
            ghostBadCancel += 1;
            return;
        } catch {}
        uint256 c0 = usdc.balanceOf(creator);
        uint256 p0 = usdc.balanceOf(address(pool));
        vm.prank(seed % 2 == 0 ? closer : creator);
        pool.cancelWithdraw();
        if (usdc.balanceOf(creator) != c0 || usdc.balanceOf(address(pool)) != p0) ghostInstant += 1;
        calls["cancel"] += 1;
    }

    function _delay() internal view returns (uint256) {
        return pool.factory().WITHDRAW_DELAY();
    }

    function _window() internal view returns (uint256) {
        return pool.factory().WITHDRAW_WINDOW();
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
        ghostPut += amount;
        calls["addToPot"] += 1;
    }

    /// Rarely: a closed pool sells nothing more, so a close early in a run would leave little to drive. Before the
    /// term the creator is refused and only the closer closes it; from the term on, either may.
    function close(uint256 seed) external {
        if (pool.closed() || !_rare(seed, 24)) return;
        _close(seed >> 128);
    }

    /// Before the term: the creator and a stranger are refused (a pass is recorded, not reverted), and the closer
    /// closes the pool early. From the term on: the creator closes it, or now and then the closer.
    function _close(uint256 seed) internal {
        vm.prank(stranger);
        try pool.close() {
            ghostBadClose += 1;
        } catch {}
        bool early = block.timestamp < pool.lockedUntil();
        if (early) {
            vm.prank(creator);
            try pool.close() {
                ghostBadClose += 1;
                ghostClosedAt = block.timestamp;
                calls["close"] += 1;
                return;
            } catch {}
        }
        bool byCloser = early || seed % 4 == 0;
        uint256 c0 = usdc.balanceOf(creator);
        uint256 p0 = usdc.balanceOf(address(pool));
        vm.prank(byCloser ? closer : creator);
        pool.close();
        if (usdc.balanceOf(creator) != c0 || usdc.balanceOf(address(pool)) != p0) ghostInstant += 1;
        ghostClosedAt = block.timestamp;
        ghostClosedByCloser = byCloser;
        calls[early ? "closeEarly" : "close"] += 1;
    }

    /// Past the pool's term at once: from here on the creator may close it and take back what it put in.
    function pastTerm(uint256 seed) external {
        if (block.timestamp >= pool.lockedUntil() || !_rare(seed, 4)) return;
        vm.warp(uint256(pool.lockedUntil()) + bound(seed >> 8, 0, 3 days));
        vm.roll(block.number + 1);
        calls["pastTerm"] += 1;
    }

    function finish(bool byCloser) external {
        _finish(byCloser);
    }

    /// Rarely, the whole end of a pool in one call: closed, every passed stage claimed, every exam and stage run
    /// out and released, the queue paid as far as the money goes, and finished if nothing is owed or running.
    /// A trader the token refused may still claim what was set aside afterwards.
    function windDown(uint256 seed) external {
        if (pool.finished() || !_rare(seed, 16)) return;
        if (!pool.closed()) {
            // Before the term: half the time the closer closes it early, half the time it runs out its term first.
            if (block.timestamp < pool.lockedUntil() && (seed >> 64) % 2 == 0) {
                vm.warp(pool.lockedUntil());
                vm.roll(block.number + 1);
            }
            _close(seed >> 128);
        }
        for (uint256 i = 0; i < _stages.length; ++i) {
            (PaperExam.Status st,,,) = PaperExam(_stages[i]).result();
            (,, bool done) = pool.accountOf(_stages[i]);
            if (st == PaperExam.Status.Passed && !done) {
                try pool.claimPayout(_stages[i]) {} catch { ghostStuck += 1; }
            }
        }
        vm.warp(block.timestamp + 31 days);
        vm.roll(block.number + 1);
        _endAll(_exams);
        _endAll(_stages);
        // Every stop is older than its 14 days by now: each one becomes final, and its money free.
        for (uint256 i = 0; i < pool.payoutCount(); ++i) {
            if (pool.payout(i).state != SimPool.PayoutState.Stopped) continue;
            try pool.finalizeStop(i) {} catch { ghostStuck += 1; }
        }
        _pay(type(uint256).max);
        _finish((seed >> 192) % 2 == 0);
        calls["windDown"] += 1;
    }

    function _endAll(address[] storage list) internal {
        for (uint256 i = 0; i < list.length; ++i) {
            (,, bool done) = pool.accountOf(list[i]);
            if (done) continue;
            // An exam or stage still running is past its deadline here, so `expire` ends it; one already ended
            // refuses, and that refusal means nothing.
            try PaperExam(list[i]).expire() {} catch {}
            // Every account not done is over by now: a refusal is a finding, kept for the invariant.
            try pool.release(list[i]) {} catch { ghostStuck += 1; }
        }
    }

    /// By the creator, or by the closer: the money goes the same way, and none of it to the closer. A stranger's
    /// finish must be refused; if it went through, it is recorded (I12) and its money counted as any finish's, read
    /// before it, so that only I12 names it (review).
    function _finish(bool byCloser) internal {
        if (!pool.closed() || pool.finished()) return;
        bool owing = pool.owed() != 0 || pool.live() != 0 || pool.reserved() != 0;
        Before memory b = _before();
        vm.prank(stranger);
        try pool.finish() {
            ghostBadClose += 1;
            _finished(b, owing);
            return;
        } catch {}
        vm.prank(byCloser ? closer : creator);
        try pool.finish() {
            _finished(b, owing);
            calls[byCloser ? "finishByCloser" : "finish"] += 1;
        } catch {
            // With nothing owed, running or kept, a week after the close, the pool has no ground to refuse it
            // (review of #279).
            if (!owing && block.timestamp >= uint256(pool.closedAt()) + _delay()) ghostFinishRefused += 1;
        }
    }

    function _finished(Before memory b, bool owing) internal {
        if (owing) ghostEarlyFinish += 1;
        if (block.timestamp < uint256(pool.closedAt()) + _delay()) ghostInstant += 1;
        if (pool.requested() != 0 || pool.readyAt() != 0) ghostWithdrawMismatch += 1;
        // What finish withdraws: all the pool holds that is not set aside (nothing is owed or kept by now).
        uint256 amount = b.bal - b.held - b.reserved;
        ghostOut += amount == 0 ? 0 : amount - _checkShares(b, amount);
    }

    // ── a withdrawal's shares, worked out apart from the pool ────

    struct Before {
        uint256 bal;
        uint256 held;
        uint256 reserved;
        uint256 owed;
        uint256 principal;
        uint256 creator;
        uint256 platform;
        bytes32[] codes;
        uint256[] partnerBal;
        uint256[] partnerHeld;
    }

    function _before() internal view returns (Before memory b) {
        b.bal = usdc.balanceOf(address(pool));
        b.held = pool.held();
        b.reserved = pool.reserved();
        b.owed = pool.owed();
        b.principal = pool.principal();
        b.creator = usdc.balanceOf(creator);
        b.platform = usdc.balanceOf(pool.factory().feeRecipient());
        b.codes = pool.codes();
        b.partnerBal = new uint256[](b.codes.length);
        b.partnerHeld = new uint256[](b.codes.length);
        for (uint256 i = 0; i < b.codes.length; ++i) {
            (address p,,,,) = pool.codeOf(b.codes[i]);
            if (p == address(0)) continue;
            b.partnerBal[i] = usdc.balanceOf(p);
            b.partnerHeld[i] = pool.heldOf(p);
        }
    }

    /// The shares of a withdrawal of `amount` as docs/SIM-POOL.md states them: the platform takes 0.5 % of it; the
    /// profit is what is above what is owed, held, kept for stops and put in by the creator, and the withdrawal takes
    /// it first; of that profit less the platform's 0.5 %, each code's partner gets the code's share in the part its
    /// sales brought of all the pool took, sent or, if the token refuses, set aside for the partner; the creator gets
    /// the rest. Returns what was set aside for partners: it stays in the pool.
    function _checkShares(Before memory b, uint256 amount) internal returns (uint256 setAsideAll) {
        uint256 free = b.bal - b.held - b.reserved;
        free = free > b.owed ? free - b.owed : 0;
        uint256 profit = free > b.principal ? free - b.principal : 0;
        uint256 shared = amount < profit ? amount : profit;
        uint256 net = shared - shared * 50 / 10_000;
        uint256 fee = amount * 50 / 10_000;
        uint256 toPartners;
        for (uint256 i = 0; i < b.codes.length; ++i) {
            (uint256 part, uint256 setAside, bool right) = _partnerPart(b, i, net);
            toPartners += part;
            setAsideAll += setAside;
            if (!right) ghostShareMismatch += 1;
        }
        if (toPartners > net * 6000 / 10_000) ghostShareMismatch += 1;
        ghostPlatform += fee;
        ghostToPartners += toPartners;
        if (usdc.balanceOf(creator) - b.creator != amount - fee - toPartners) ghostShareMismatch += 1;
        if (usdc.balanceOf(pool.factory().feeRecipient()) - b.platform != fee) ghostShareMismatch += 1;
        ghostPartnerHeld += setAsideAll;
    }

    /// Code `i`'s partner's part of `net`, what of it was set aside, and whether what the partner got is that part.
    function _partnerPart(Before memory b, uint256 i, uint256 net)
        internal
        view
        returns (uint256 part, uint256 setAside, bool right)
    {
        (address p, uint16 bps,,, uint256 got) = pool.codeOf(b.codes[i]);
        if (p == address(0)) return (0, 0, true);
        uint256 all = pool.brought();
        part = (net == 0 || all == 0 || bps == 0 || got == 0) ? 0 : net * got * bps / (all * 10_000);
        setAside = pool.heldOf(p) - b.partnerHeld[i];
        right = usdc.balanceOf(p) - b.partnerBal[i] + setAside == part;
    }

    // ── the creator's codes ──────────────────────────────────────

    /// Sets one of the handler's codes, with its own partner and a share of 0 or 40 to 60 %, or takes it off sale.
    /// Once a sale has named a code, the pool must refuse.
    function setCode(uint256 i, uint256 bpsSeed, bool offSale) external {
        bytes32 code = _codeKeys[i % _codeKeys.length];
        address p = offSale ? address(0) : _partners[i % _codeKeys.length];
        uint16 bps = offSale || bpsSeed % 3 == 0 ? 0 : uint16(4000 + bpsSeed % 2001);
        vm.prank(creator);
        try pool.setCode(code, p, bps) {
            if (ghostUsed[code]) ghostCodeChanged += 1;
            calls["setCode"] += 1;
        } catch {
            if (!ghostUsed[code]) ghostCodeRefused += 1;
        }
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
            address a = list[(seed % n + k) % n];
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
    address closer = makeAddr("closer");

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
        factory.setCloser(closer);
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

        handler = new SimPoolHandler(pool, usdc, filler, checker, creator, closer, usdc.balanceOf(address(pool)));
        address approver = handler.approver();
        vm.prank(creator);
        pool.setApprover(approver);
        // Three codes to start: a partner with 40 %, one with 60 %, and a code that gives its partner nothing.
        uint16[3] memory shares = [uint16(4000), 6000, 0];
        for (uint256 i = 0; i < 3; ++i) {
            (bytes32 code, address owner) = (handler.codeKey(i), handler.partner(i));
            vm.prank(creator);
            pool.setCode(code, owner, shares[i]);
        }
        targetContract(address(handler));
        bytes4[] memory s = new bytes4[](46);
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
        s[15] = SimPoolHandler.requestWithdraw.selector;
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
        s[29] = SimPoolHandler.objectStop.selector;
        s[30] = SimPoolHandler.finalizeStop.selector;
        s[31] = SimPoolHandler.stop.selector;
        s[32] = SimPoolHandler.pastReserve.selector;
        s[33] = SimPoolHandler.finalizeStop.selector;
        s[34] = SimPoolHandler.buySigned.selector;
        s[35] = SimPoolHandler.setCode.selector;
        s[36] = SimPoolHandler.withdraw.selector;
        s[37] = SimPoolHandler.sales.selector;
        s[38] = SimPoolHandler.sales.selector;
        s[39] = SimPoolHandler.refuse.selector;
        s[40] = SimPoolHandler.pastTerm.selector;
        s[41] = SimPoolHandler.cancelWithdraw.selector;
        s[42] = SimPoolHandler.requestWithdraw.selector;
        s[43] = SimPoolHandler.approve.selector;
        s[44] = SimPoolHandler.approve.selector;
        s[45] = SimPoolHandler.approve.selector;
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
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_moneyIsAccountedFor() public view {
        assertEq(usdc.balanceOf(address(pool)), handler.ghostIn() - handler.ghostOut());
    }

    /// I2. What the token refused is set aside, and always there: the pool's USDC never falls below it. What stops
    ///     keep is every stopped payout's amount, and `pay` spends neither: only what is free of both.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_whatIsHeldIsThere() public view {
        assertGe(usdc.balanceOf(address(pool)), pool.held());
        (uint256 heldPayouts,) = _sum(SimPool.PayoutState.Held);
        assertEq(
            pool.held(),
            heldPayouts + handler.ghostPartnerHeld() - handler.ghostClaimed(),
            "held: refused payouts and partners' parts, less what was claimed"
        );
        uint256 byHolder;
        for (uint256 i = 0; i < handler.holderCount(); ++i) byHolder += pool.heldOf(handler.holder(i));
        assertEq(byHolder, pool.held(), "held: the traders' and partners' own amounts");
        // What stops keep may exceed what the pool holds, when payouts came faster than prices (review of #269):
        // so it is held to the stopped payouts, not to the balance.
        (uint256 stoppedPayouts,) = _sum(SimPool.PayoutState.Stopped);
        assertEq(pool.reserved(), stoppedPayouts, "reserved: the stopped payouts");
        assertEq(handler.ghostSpentKept(), 0, "a pay spent what is held or kept for a stop");
    }

    /// I3. `paidTotal` is the sum of what was paid, and what left the pool in `pay` is exactly that.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_paidTotalIsTheSumOfPayments() public view {
        (uint256 paid, uint256 count) = _sum(SimPool.PayoutState.Paid);
        assertEq(pool.paidTotal(), paid);
        assertEq(pool.paidCount(), count);
        assertEq(pool.paidTotal(), handler.ghostPaidOut(), "what left the pool in pay");
        assertEq(handler.ghostPayMismatch(), 0, "a pay sent other than what it marked paid");
    }

    /// I4. The creator never withdraws below three payouts of the largest size above what is owed, held and kept
    ///     for stops, and not one unit past `withdrawable`; before the pool's term, not past its profit either.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_creatorKeepsThreePayouts() public view {
        assertEq(handler.ghostBelowFloor(), 0, "a withdrawal left less than three payouts");
        assertEq(handler.ghostOverWithdraw(), 0, "a request past withdrawable went through");
        if (!pool.finished()) {
            uint256 floor = uint256(pool.largestPayout()) * 3;
            if (pool.staged() > floor) floor = pool.staged();
            uint256 keep = pool.owed() + pool.held() + pool.reserved() + floor;
            uint256 bal = usdc.balanceOf(address(pool));
            uint256 w = bal > keep ? bal - keep : 0;
            if (block.timestamp < pool.lockedUntil()) {
                uint256 kept = pool.owed() + pool.held() + pool.reserved();
                uint256 free = bal > kept ? bal - kept : 0;
                uint256 profit = free > pool.principal() ? free - pool.principal() : 0;
                if (w > profit) w = profit;
            }
            assertEq(pool.withdrawable(), w);
        }
    }

    /// I5. One purchase, at most one payout: each payout is of a stage, each stage of one exam, each exam
    ///     funded at most once, and no stage paid twice.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
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
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_owedIsTheDuePayouts() public view {
        (uint256 due,) = _sum(SimPool.PayoutState.Due);
        assertEq(pool.owed(), due);
    }

    /// I7. The counts of stops on the page: stops less lifts are those standing and those made final.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_standingStopsAreStopsLessLifts() public view {
        (, uint256 stopped) = _sum(SimPool.PayoutState.Stopped);
        (, uint256 refused) = _sum(SimPool.PayoutState.Refused);
        assertEq(pool.stoppedCount() - pool.liftedCount(), stopped + refused);
    }

    /// I8. Nothing due is passed over: below the head of the queue no payout is still due.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_noDuePayoutBehindTheHead() public view {
        uint256 head = pool.head();
        assertLe(head, pool.payoutCount());
        for (uint256 i = 0; i < head; ++i) {
            assertTrue(pool.payout(i).state != SimPool.PayoutState.Due, "a due payout behind the head");
        }
    }

    /// I9. `live` counts the exams and stages not done, `staged` the payouts of the stages not done, each by its size,
    ///     and a finished pool holds only what is set aside.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_liveAndFinished() public view {
        uint256 running;
        for (uint256 i = 0; i < handler.examCount(); ++i) {
            (,, bool done) = pool.accountOf(handler.exam(i));
            if (!done) running += 1;
        }
        uint256 staged;
        SimSize[] memory menu = pool.menu();
        for (uint256 i = 0; i < handler.stageCount(); ++i) {
            (, uint8 line, bool done) = pool.accountOf(handler.stage(i));
            if (!done) {
                running += 1;
                staged += uint256(menu[line].size) * 800 / 10_000;
            }
        }
        assertEq(pool.live(), running);
        // The payouts of the stages still running, each by its size: what withdrawals stay above (CFO, 13 November).
        assertEq(pool.staged(), staged, "staged is not the running stages' payouts");
        assertEq(handler.ghostEarlyFinish(), 0, "finished with something owed, running or kept for a stop");
        assertEq(handler.ghostFinishRefused(), 0, "a finish by the creator or the closer refused with nothing owed, running or kept");
        if (pool.finished()) assertEq(pool.reserved(), 0, "a finished pool keeps nothing for stops");
        assertEq(handler.ghostStuck(), 0, "past every deadline, an account that would not end or a stage not claimed");
        if (pool.finished()) assertEq(usdc.balanceOf(address(pool)), pool.held(), "a finished pool keeps only what is held");
    }

    /// I10. The codes: what all of them brought is the sum of each code's, and each is what its sales brought; a
    ///      code a sale has named keeps the partner and share it had then, and a code no sale has named can be set.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_codesKeepTheirBooks() public view {
        bytes32[] memory codes = pool.codes();
        uint256 sum;
        for (uint256 i = 0; i < codes.length; ++i) {
            (address p, uint16 bps, bool used,, uint256 got) = pool.codeOf(codes[i]);
            sum += got;
            assertEq(got, handler.ghostBroughtOf(codes[i]), "a code's brought is what its sales brought");
            assertEq(used, handler.ghostUsed(codes[i]), "used: a sale named it");
            if (used) {
                assertEq(p, handler.firstPartner(codes[i]), "a used code's partner changed");
                assertEq(bps, handler.firstBps(codes[i]), "a used code's share changed");
            }
        }
        assertEq(pool.brought(), sum, "brought: the codes' together");
        assertEq(pool.brought(), handler.ghostBrought(), "brought: every price less the platform's part");
        assertEq(handler.ghostCodeChanged(), 0, "a used code was set again");
        assertEq(handler.ghostCodeRefused(), 0, "an unused code could not be set");
    }

    /// I11. A withdrawal's shares: the platform's 0.5 %, each partner's part of the profit by its code's share and what
    ///      its code brought (sent, or set aside if the token refused), the creator the rest, and all partners together
    ///      never more than 60 % of the profit less the platform's part.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_withdrawalsShareByTheCodes() public view {
        assertEq(handler.ghostShareMismatch(), 0, "a withdrawal's shares were not the codes'");
    }

    /// I12. The term: before it, until the pool is finished, the creator has none of what it put in back, by any
    ///      path (withdrawals, money put in and taken out again, closing, finishing): `principal` is all it put in.
    ///      Before the term only the closer closes the pool, and that is `closedEarly`; a stranger neither closes
    ///      nor finishes it.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_beforeTheTermWhatTheCreatorPutInStays() public view {
        if (block.timestamp < pool.lockedUntil() && !pool.finished()) {
            assertEq(pool.principal(), handler.ghostPut(), "before the term, the creator took back some of what it put in");
        }
        assertLe(pool.principal(), handler.ghostPut(), "principal above what the creator put in");
        assertEq(handler.ghostBadClose(), 0, "a close before the term by the creator, or a close or finish by a stranger");
        if (pool.closed()) {
            bool early = handler.ghostClosedAt() < pool.lockedUntil();
            assertEq(pool.closedEarly(), early, "closedEarly: closed before the term");
            if (early) assertTrue(handler.ghostClosedByCloser(), "closed before the term by other than the closer");
        } else {
            assertFalse(pool.closedEarly(), "closedEarly on an open pool");
        }
    }

    /// I13. Closed early or not, finished by the creator or the closer, the money goes the same way: traders their
    ///      payouts and what was held for them, the codes' partners their shares, the creator the rest (I11). The
    ///      closer gets none of it, and the platform only its 0.5 % of each price and each withdrawal.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_theCloserGetsNothing_thePlatformItsHalfPercent() public view {
        assertEq(usdc.balanceOf(closer), 0, "the closer got money");
        assertEq(usdc.balanceOf(platform), handler.ghostPlatform(), "the platform got other than 0.5 % of prices and withdrawals");
        assertEq(handler.ghostShareMismatch(), 0, "a withdrawal's or finish's shares were not the codes'");
    }

    /// I14. No money leaves the pool for the creator but by `withdraw()` of its own request, once `WITHDRAW_DELAY`
    ///      has passed and before `WITHDRAW_WINDOW` more has, or by `finish`, `WITHDRAW_DELAY` after the close. A
    ///      withdrawal pays the less of what was asked and what `withdrawable` allows when it is carried out. A
    ///      request, a cancel and a close move nothing; a stranger cancels nothing.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_noWithdrawalWithoutAWeeksRequest() public view {
        assertEq(handler.ghostInstant(), 0, "the creator got money without a request a week old, or finish a week after the close");
        assertEq(handler.ghostWithdrawMismatch(), 0, "a withdrawal paid other than the less of the request and withdrawable");
        assertEq(handler.ghostBadCancel(), 0, "a stranger cancelled a request");
        if (pool.requested() != 0) {
            assertEq(pool.requested(), handler.ghostRequested(), "the standing request is not the creator's last");
            assertEq(pool.readyAt(), handler.ghostRequestedAt() + pool.factory().WITHDRAW_DELAY(), "a request ready sooner than a week");
        }
    }

    /// I15. No payout leaves the pool, paid or set aside for a refused trader, before its 72 hours; none not
    ///      approved before its 7 days; none that was not waiting when `pay` began (a stopped one, say). Only
    ///      the approver approves.
    /// forge-config: default.invariant.runs = 48
    /// forge-config: default.invariant.depth = 120
    /// forge-config: default.invariant.fail-on-revert = true
    function invariant_aPayoutWaitsItsApprovalOrSevenDays() public view {
        assertEq(handler.ghostEarlyPay(), 0, "a payout left early, unapproved before 7 days, or stopped; or a stranger approved");
    }

    /// Not an invariant: what each run reached, printed with `-vv`, so that a run that drove nothing does not pass
    /// unseen for one that did.
    function afterInvariant() external view {
        string[31] memory names = ["approve", "request", "cancel", "earn", "buy", "passExam", "passStage", "fail", "fund", "claim", "pay", "release", "warp",
            "refuse", "claimHeld", "stop", "lift", "withdraw", "addToPot", "close", "finish", "windDown", "objectStop", "finalizeStop", "pastReserve", "buySigned", "setCode", "sales",
            "closeEarly", "pastTerm", "finishByCloser"];
        string memory line = "reached:";
        for (uint256 i = 0; i < names.length; ++i) {
            line = string.concat(line, " ", names[i], "=", vm.toString(handler.calls(names[i])));
        }
        line = string.concat(line, " payouts=", vm.toString(pool.payoutCount()), " paid=", vm.toString(pool.paidCount()),
            " toPartners=", vm.toString(handler.ghostToPartners()), " partnerHeld=", vm.toString(handler.ghostPartnerHeld()), " brought=", vm.toString(pool.brought()));
        console2.log(line);
    }
}
