// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test, Vm} from "forge-std/Test.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {HyperCore} from "@hyper-evm-lib/test/simulation/HyperCore.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";

import {Rules} from "../../src/Types.sol";
import {PaperExam, ExamTerms} from "../../src/exam/PaperExam.sol";
import {OracleFollowsMark} from "../PoolHarness.sol";
import {BookStub} from "./PaperExamInvariant.t.sol";

/// The rules of an exam, written again from their text (docs/PAPER-EXAM.md, "What it does", and the first pool's
/// terms), not from PaperExam.sol: a reading of the rules that is wrong in the contract is not wrong here the same
/// way (CTO, 13 November). One asset, a filler, fills at the book.
///
/// - Equity is the balance plus the position at the oracle price; a fill moves the balance by the size times its
///   price and charges `costPpm` of its notional, rounded up.
/// - A fill at the book: a buy at the ask, a sell at the bid, never better for the trader than the oracle, and
///   refused in that block when more than `maxWorseBps` worse than it; a book empty on a side or crossed fills at
///   the oracle. An order that would take the account over its leverage is refused; one that only reduces never is.
/// - The rules, judged at the moment asked: equity below the drawdown line; below the day's snapshot by
///   `dailyLossBps`; notional over equity above `maxLeverageX100`. The drawdown line, from the start: the starting
///   balance less `maxDrawdownBps` of it. Trailing to the start: the highest daily snapshot of the step less
///   `maxDrawdownBps` of the starting balance, and never above the starting balance.
/// - A stop and a take for the position: set by the trader on an open position, acting `fillDelay` blocks later;
///   once the oracle is at the price or past it (against the position for a stop, for it for a take) the filler
///   closes the set size, or all of it, at the price of a fill in that block and with its cost. A position
///   closed or turned round loses both; one carried out is gone.
/// - The day's snapshot in the first 15 minutes of the UTC day, once a day. A trading day is a UTC day with a fill
///   of at least 10 USDC of notional.
/// - A pass: no rule broken, nothing open, equity at the target, the step's trading days, before the limits. Past
///   the step's deadline, or its idle limit counted from its start or its last trade, nothing is placed, filled or
///   passed, and anyone may end it.
contract RulesModel {
    int256 public constant BPS = 10_000;
    // The first pool's terms, as its text states them (two trading days asked, so the count of days is tried, and
    // an idle limit of 10 days, so it can bind).
    int256 public constant capital = 100e6;
    uint16 public constant targetBps = 1000;
    uint16 public constant dd = 600;
    uint16 public constant daily = 300;
    uint32 public constant lev = 1000;
    uint32 public constant costPpm = 450;
    uint16 public constant maxWorse = 150;
    uint32 public constant duration = 30 days;
    uint32 public constant maxIdle = 10 days;
    uint16 public constant minDays = 2;
    uint256 public constant delay = 1;

    struct Trigger {
        int256 px;
        int256 sz;
        uint256 armedAt;
    }

    Trigger public stop_;
    Trigger public take_;

    /// 0 from the start, 1 trailing the daily snapshots to the start.
    uint8 public immutable kind;
    int256 public cash;
    int256 public pos;
    int256 public dayStart;
    int256 public highestSnapshot;
    uint256 public day;
    uint16 public tradingDays;
    uint256 public lastTradeDay; // day + 1; 0 for none
    uint256 public stepStart;
    uint256 public lastTrade;
    uint8 public status = 1; // 1 active, 2 passed, 3 breached, 4 expired

    constructor(uint256 start, uint8 kind_) {
        kind = kind_;
        // The step's starting balance is its first snapshot.
        highestSnapshot = capital;
        cash = capital;
        // NOT IN THE TEXT (found by this test, 13 November): until the first snapshot, the day's loss counts from the
        // step's starting balance. docs/PAPER-EXAM.md says it now.
        dayStart = capital;
        stepStart = start;
        lastTrade = start;
    }

    function equity(int256 oracle) public view returns (int256) {
        return cash + pos * oracle;
    }

    function over(uint256 now_) public view returns (bool) {
        if (duration != 0 && now_ > stepStart + duration) return true;
        return maxIdle != 0 && now_ > lastTrade + maxIdle;
    }

    /// The drawdown line times BPS, as the text draws it.
    function lineBps() public view returns (int256) {
        int256 limit = capital * int256(uint256(dd));
        if (kind == 0) return capital * BPS - limit;
        int256 trailing = highestSnapshot * BPS - limit;
        return trailing > capital * BPS ? capital * BPS : trailing;
    }

    /// The first rule broken now, in the text's order: 1 drawdown, 2 the day's loss, 3 leverage; 0 none.
    function broken(int256 oracle) public view returns (uint8) {
        int256 eq = equity(oracle);
        if (eq * BPS < lineBps()) return 1;
        if (dayStart > 0 && eq * BPS < dayStart * (BPS - int256(uint256(daily)))) return 2;
        int256 ntl = (pos < 0 ? -pos : pos) * oracle;
        if (ntl * 100 > eq * int256(uint256(lev))) return 3;
        return 0;
    }

    /// The price of a fill, or 0 if the book is too far from the oracle in this block.
    function fillPrice(bool isBuy, int256 oracle, int256 bid, int256 ask) public view returns (int256) {
        if (bid == 0 || ask == 0 || ask < bid) return oracle;
        int256 px = isBuy ? (ask > oracle ? ask : oracle) : (bid < oracle ? bid : oracle);
        int256 worse = isBuy ? px - oracle : oracle - px;
        if (worse * BPS > oracle * int256(uint256(maxWorse))) return 0;
        return px;
    }

    /// Whether the fill goes through, and if so it is made.
    function fill(bool isBuy, int256 sz, int256 oracle, int256 bid, int256 ask, uint256 now_) external returns (bool) {
        if (status != 1 || over(now_)) return false;
        int256 px = fillPrice(isBuy, oracle, bid, ask);
        if (px == 0) return false;
        int256 after_ = pos + (isBuy ? sz : -sz);
        int256 newCash = cash - (isBuy ? sz : -sz) * px - (sz * px * int256(uint256(costPpm)) + 1_000_000 - 1) / 1_000_000;
        if (_grows(after_) && _overLev(newCash, after_, oracle)) return false;
        cash = newCash;
        if (pos != 0 && (after_ == 0 || (after_ > 0) != (pos > 0))) {
            delete stop_;
            delete take_;
        }
        pos = after_;
        if (sz * px >= 10e6) {
            uint256 d = now_ / 1 days;
            if (lastTradeDay != d + 1) {
                lastTradeDay = d + 1;
                tradingDays += 1;
            }
            lastTrade = now_;
        }
        return true;
    }

    function _grows(int256 after_) internal view returns (bool) {
        return (after_ < 0 ? -after_ : after_) > (pos < 0 ? -pos : pos);
    }

    function _overLev(int256 newCash, int256 after_, int256 oracle) internal pure returns (bool) {
        int256 eq = newCash + after_ * oracle;
        return eq <= 0 || (after_ < 0 ? -after_ : after_) * oracle * 100 > eq * int256(uint256(lev));
    }

    function setTrigger(bool isTake, int256 px, int256 sz, uint256 blockNo, uint256 now_) external returns (bool) {
        if (status != 1 || over(now_) || pos == 0 || px == 0) return false;
        Trigger memory t = Trigger(px, sz, blockNo + delay);
        if (isTake) take_ = t;
        else stop_ = t;
        return true;
    }

    function fillTrigger(bool isTake, int256 oracle, int256 bid, int256 ask, uint256 blockNo, uint256 now_)
        external
        returns (bool)
    {
        Trigger memory t = isTake ? take_ : stop_;
        if (status != 1 || over(now_) || t.px == 0 || blockNo < t.armedAt || pos == 0) return false;
        bool hit = isTake == (pos > 0) ? oracle >= t.px : oracle <= t.px;
        if (!hit) return false;
        int256 open = pos < 0 ? -pos : pos;
        int256 sz = t.sz == 0 || t.sz > open ? open : t.sz;
        if (!this.fill(pos < 0, sz, oracle, bid, ask, now_)) return false;
        if (isTake) delete take_;
        else delete stop_;
        return true;
    }

    function checkpoint(int256 oracle, uint256 now_) external returns (bool) {
        if (status != 1 || now_ % 1 days >= 15 minutes || now_ / 1 days <= day) return false;
        // NOT IN THE TEXT (found by this test, 13 November): no snapshot while a rule is broken; the day's base is
        // never the equity that broke a rule (the contract's deep audit, F12). docs/PAPER-EXAM.md says it now.
        if (broken(oracle) != 0) return false;
        day = now_ / 1 days;
        dayStart = equity(oracle);
        if (dayStart > highestSnapshot) highestSnapshot = dayStart;
        return true;
    }

    function breach(int256 oracle) external returns (uint8 reason) {
        if (status != 1) return 0;
        reason = broken(oracle);
        if (reason != 0) status = 3;
    }

    function expire(uint256 now_) external returns (bool) {
        if (status != 1 || !over(now_)) return false;
        status = 4;
        return true;
    }

    function graduate(int256 oracle, uint256 now_) external returns (bool) {
        if (status != 1 || over(now_) || broken(oracle) != 0 || pos != 0) return false;
        if (equity(oracle) < capital + capital * int256(uint256(targetBps)) / BPS) return false;
        if (tradingDays < minDays) return false;
        status = 2;
        return true;
    }
}

/// Drives the exam and the model with the same calls and prices: the oracle moves, the book is drawn anew, orders
/// of any size and side are filled at any block of their window, snapshots, passes, breaches and expiries are tried.
/// After every call the two must agree: whether it went through, the status, the equity, the reason of a breach.
contract DifferentialHandler is Test {
    uint32 constant BTC = 3;
    uint16 constant DELAY = 1;
    uint16 constant WINDOW = 10;
    PaperExam public exam;
    RulesModel public model;
    address public trader = makeAddr("trader");
    address public filler = makeAddr("filler");
    BookStub internal book = BookStub(0x000000000000000000000000000000000000080e);

    int256 public oracle = 1_000_000;
    int256 public bid;
    int256 public ask;
    bool public hasOrder;
    bool public orderBuy;
    int256 public orderSz;
    uint256 public placedBlock;

    uint256 public mismatches;
    string public firstMismatch;
    mapping(string => uint256) public calls;

    constructor(PaperExam exam_, RulesModel model_) {
        exam = exam_;
        model = model_;
    }

    function _differ(string memory what) internal {
        if (mismatches == 0) firstMismatch = what;
        mismatches += 1;
    }

    function _compare(string memory what) internal {
        if (uint8(exam.status()) != model.status()) _differ(string.concat(what, ": status"));
        if (exam.equity() != model.equity(oracle)) _differ(string.concat(what, ": equity"));
        if (exam.drawdownLine() != (model.lineBps() + 9_999) / 10_000) _differ(string.concat(what, ": drawdown line"));
    }

    function move(uint256 seed) external {
        // Up to 3 % either way, now and then 8 %: enough to break a rule, pass a target or cross the ceiling.
        int256 bps = int256(seed % 601) - 300;
        if (seed % 13 == 0) bps = bps > 0 ? int256(800) : int256(-800);
        oracle = oracle * (10_000 + bps) / 10_000;
        if (oracle < 100_000) oracle = 100_000;
        CoreSimulatorLib.setMarkPx(BTC, uint64(uint256(oracle)));
        vm.roll(block.number + 1);
        _compare("move");
    }

    function place(bool isBuy, uint256 sz) external {
        // One order at a time: a standing one is let lapse first, past its window and the fallback.
        if (hasOrder && block.number <= placedBlock + DELAY + WINDOW + 61) vm.roll(placedBlock + DELAY + WINDOW + 62);
        sz = bound(sz, 1, 1_500);
        vm.prank(trader);
        try exam.place(BTC, isBuy, uint64(sz)) {
            (hasOrder, orderBuy, orderSz, placedBlock) = (true, isBuy, int256(sz), block.number);
            calls["place"] += 1;
        } catch {}
    }

    function fill(uint256 at, uint256 bookSeed) external {
        if (!hasOrder) return;
        uint256 first = placedBlock + DELAY;
        uint256 target = first + (at % WINDOW);
        if (target <= block.number) return; // its block is gone: leave it, or a new order replaces it later
        vm.roll(target);
        _drawBook(bookSeed);
        bool expect = model.fill(orderBuy, orderSz, oracle, bid, ask, block.timestamp);
        vm.prank(filler);
        bool got;
        try exam.fill() {
            got = true;
        } catch {}
        if (got != expect) _differ(got ? "fill: the exam filled, the text refuses" : "fill: the exam refused, the text fills");
        if (got) {
            hasOrder = false;
            calls["fill"] += 1;
        }
        _compare("fill");
    }

    function setTrigger(bool isTake, uint256 pxSeed, uint256 szSeed) external {
        int256 px = oracle * (9_000 + int256(pxSeed % 2_001)) / 10_000;
        uint256 sz = szSeed % 4 == 0 ? 0 : bound(szSeed, 1, 1_500);
        vm.roll(block.number + 1);
        bool expect = model.setTrigger(isTake, px, int256(sz), block.number, block.timestamp);
        bool got;
        vm.prank(trader);
        try exam.setTrigger(BTC, isTake, uint64(uint256(px)), uint64(sz)) {
            got = true;
            calls["setTrigger"] += 1;
        } catch {}
        if (got != expect) _differ(got ? "setTrigger: the exam took it, the text refuses" : "setTrigger: the exam refused, the text takes it");
    }

    function fillTrigger(bool isTake, uint256 bookSeed) external {
        vm.roll(block.number + 1);
        _drawBook(bookSeed);
        bool expect = model.fillTrigger(isTake, oracle, bid, ask, block.number, block.timestamp);
        bool got;
        vm.prank(filler);
        try exam.fillTrigger(BTC, isTake) {
            got = true;
            calls["fillTrigger"] += 1;
        } catch {}
        if (got != expect) _differ(got ? "fillTrigger: the exam filled, the text refuses" : "fillTrigger: the exam refused, the text fills");
        _compare("fillTrigger");
    }

    function checkpoint(uint256 secs) external {
        vm.warp((block.timestamp / 1 days + 1) * 1 days + bound(secs, 0, 20 minutes));
        vm.roll(block.number + 1);
        bool expect = model.checkpoint(oracle, block.timestamp);
        bool got;
        try exam.checkpoint() {
            got = true;
            calls["checkpoint"] += 1;
        } catch {}
        if (got != expect) _differ(got ? "checkpoint: the exam took it, the text refuses" : "checkpoint: the exam refused, the text takes it");
        _compare("checkpoint");
    }

    function breach() external {
        uint8 expect = model.breach(oracle);
        vm.recordLogs();
        uint8 got;
        try exam.breach() {
            Vm.Log[] memory logs = vm.getRecordedLogs();
            for (uint256 i = 0; i < logs.length; ++i) {
                if (logs[i].topics.length == 3 && logs[i].topics[0] == PaperExam.Stopped.selector) {
                    got = uint8(uint256(logs[i].topics[2]));
                }
            }
            calls["breach"] += 1;
        } catch {}
        if (got != expect) _differ("breach: reason");
        _compare("breach");
    }

    function graduate() external {
        bool expect = model.graduate(oracle, block.timestamp);
        bool got;
        vm.prank(trader);
        try exam.graduate() {
            got = true;
            calls["graduate"] += 1;
        } catch {}
        if (got != expect) _differ(got ? "graduate: the exam passed, the text refuses" : "graduate: the exam refused, the text passes");
        _compare("graduate");
    }

    function expire() external {
        bool expect = model.expire(block.timestamp);
        bool got;
        try exam.expire() {
            got = true;
            calls["expire"] += 1;
        } catch {}
        if (got != expect) _differ("expire");
        _compare("expire");
    }

    function warp(uint256 secs) external {
        vm.warp(block.timestamp + bound(secs, 1, 8 hours));
        vm.roll(block.number + 1);
        _compare("warp");
    }

    function _drawBook(uint256 seed) internal {
        uint256 kind = seed % 5;
        int256 a = oracle * int256((seed >> 8) % 300) / 10_000;
        int256 b = oracle * int256((seed >> 24) % 300) / 10_000;
        if (kind == 0) (bid, ask) = (oracle - a, oracle + b);
        else if (kind == 1) (bid, ask) = (oracle - a - b, oracle - a);
        else if (kind == 2) (bid, ask) = (oracle + a, oracle + a + b);
        else if (kind == 3) (bid, ask) = (oracle + a, oracle - b);
        else (bid, ask) = (0, oracle + a);
        book.set(BTC, uint64(uint256(bid)), uint64(uint256(ask)));
    }
}

contract PaperExamDifferentialTest is Test {
    uint32 constant BTC = 3;

    /// The drawdown line of the exam under test: from the start here, trailing to the start in the suite below.
    function _kind() internal pure virtual returns (uint8) {
        return 0;
    }

    PaperExam exam;
    RulesModel model;
    DifferentialHandler handler;

    function setUp() public {
        HyperCore hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        vm.etch(0x0000000000000000000000000000000000000807, type(OracleFollowsMark).runtimeCode);
        vm.etch(0x000000000000000000000000000000000000080e, type(BookStub).runtimeCode);
        hyperCore.registerPerpAssetInfo(
            BTC, PrecompileLib.PerpAssetInfo({coin: "BTC", marginTableId: 54, szDecimals: 5, maxLeverage: 40, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(BTC, 1_000_000);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);
        uint32[] memory assets = new uint32[](1);
        assets[0] = BTC;
        uint16[] memory targets = new uint16[](1);
        targets[0] = 1000;
        // The first pool's numbers, with two trading days asked so that the count of days is tried too.
        Rules memory r = Rules({dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 1000, assets: assets,
            assetLeverageX100: new uint32[](0)});
        ExamTerms memory t = ExamTerms({capital: 100e6, targetsBps: targets, stepDuration: 30 days, maxIdle: 10 days,
            minTradingDays: 2, costPpm: 450, fillDelay: 1, fillWindow: 10});
        exam = PaperExam(Clones.clone(address(new PaperExam())));
        model = new RulesModel(block.timestamp, _kind());
        handler = new DifferentialHandler(exam, model);
        exam.initializeWithFiller(handler.trader(), r, t, handler.filler(), true, 150, _kind());
        targetContract(address(handler));
        bytes4[] memory sel = new bytes4[](19);
        (sel[0], sel[1], sel[2]) = (DifferentialHandler.move.selector, DifferentialHandler.move.selector, DifferentialHandler.move.selector);
        (sel[3], sel[4], sel[5]) = (DifferentialHandler.place.selector, DifferentialHandler.place.selector, DifferentialHandler.place.selector);
        (sel[6], sel[7], sel[8], sel[9]) = (DifferentialHandler.fill.selector, DifferentialHandler.fill.selector,
            DifferentialHandler.fill.selector, DifferentialHandler.fill.selector);
        (sel[10], sel[11], sel[12]) = (DifferentialHandler.checkpoint.selector, DifferentialHandler.breach.selector, DifferentialHandler.graduate.selector);
        (sel[13], sel[14]) = (DifferentialHandler.expire.selector, DifferentialHandler.warp.selector);
        (sel[15], sel[16]) = (DifferentialHandler.setTrigger.selector, DifferentialHandler.setTrigger.selector);
        (sel[17], sel[18]) = (DifferentialHandler.fillTrigger.selector, DifferentialHandler.fillTrigger.selector);
        targetSelector(FuzzSelector({addr: address(handler), selectors: sel}));
    }

    /// D1. The exam and the rules written from their text agree after every call: whether it went through, the
    ///     status, the equity, and the reason of a breach.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 150
    function invariant_theExamIsItsRulesAsWritten() public view {
        assertEq(handler.mismatches(), 0, handler.firstMismatch());
    }

    function afterInvariant() external virtual {
        emit log(string.concat("reached: place=", vm.toString(handler.calls("place")), " fill=",
            vm.toString(handler.calls("fill")), " checkpoint=", vm.toString(handler.calls("checkpoint")), " breach=",
            vm.toString(handler.calls("breach")), " graduate=", vm.toString(handler.calls("graduate")), " expire=",
            vm.toString(handler.calls("expire")), " setTrigger=", vm.toString(handler.calls("setTrigger")),
            " fillTrigger=", vm.toString(handler.calls("fillTrigger"))));
    }
}

/// D1 again, on an exam whose drawdown line trails its daily snapshots up to its starting balance.
contract PaperExamDifferentialTrailingTest is PaperExamDifferentialTest {
    function _kind() internal pure override returns (uint8) {
        return 1;
    }

    /// D1 on the trailing line.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 150
    function invariant_theTrailingExamIsItsRulesAsWritten() public view {
        assertEq(handler.mismatches(), 0, handler.firstMismatch());
    }

    /// Not an invariant: how high the snapshots took the line, printed with `-vv`.
    function afterInvariant() external override {
        emit log(string.concat("reached: checkpoint=", vm.toString(handler.calls("checkpoint")), " highest snapshot=",
            vm.toString(model.highestSnapshot()), " line=", vm.toString(exam.drawdownLine()), " breach=",
            vm.toString(handler.calls("breach"))));
    }
}
