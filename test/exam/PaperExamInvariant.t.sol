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

uint32 constant BTC = 3;
uint32 constant ETH = 4;
uint64 constant BTC_MARK = 1_000_000; // 100000.0
uint64 constant ETH_MARK = 250_000; // 2500.00
int256 constant CAPITAL = 100e6;
uint16 constant DELAY = 1;
uint16 constant WINDOW = 10;
uint16 constant MAX_WORSE = 150;

/// The chain's best bid and offer, as the precompile at 0x80e answers them.
contract BookStub {
    mapping(uint64 => uint64[2]) internal _b;

    function set(uint64 asset, uint64 bid, uint64 ask) external {
        _b[asset] = [bid, ask];
    }

    fallback(bytes calldata data) external returns (bytes memory) {
        uint64 asset = abi.decode(data, (uint64));
        return abi.encode(_b[asset][0], _b[asset][1]);
    }
}

/// Drives one paper exam, filled at the book by its filler, at random: orders of any size and side on two assets,
/// fills at any block of their window or after it, the day's snapshot, passes, breaches and expiries tried at any
/// time. The oracle never moves. With `randomBook`, the book is drawn again before every fill: under, over, around,
/// far from the oracle, one-sided, crossed or empty.
contract PaperExamHandler is Test {
    PaperExam public exam;
    address public trader = makeAddr("trader");
    address public filler = makeAddr("filler");
    bool public randomBook;
    BookStub internal book = BookStub(0x000000000000000000000000000000000000080e);

    /// A pass that went through.
    uint256 public ghostPassed;
    /// A fill better for the trader than the oracle, or worse than `maxWorseBps` from it at the book.
    uint256 public ghostBadPrice;
    uint256 public fills;
    mapping(string => uint256) public calls;

    constructor(PaperExam exam_, bool randomBook_) {
        exam = exam_;
        randomBook = randomBook_;
    }

    function place(uint256 assetSeed, bool isBuy, uint256 sz) external {
        uint32 asset = assetSeed % 2 == 0 ? BTC : ETH;
        // Up to a little past the 10x cap at either mark: past it the fill is refused, which is part of the run.
        sz = bound(sz, 1, asset == BTC ? 1_200 : 4_800);
        vm.prank(trader);
        try exam.place(asset, isBuy, uint64(sz)) {
            calls["place"] += 1;
        } catch {}
    }

    function fill(uint256 blocks, uint256 bookSeed) external {
        // Mostly inside the window, at any of its blocks; now and then past it and past the fallback.
        uint256 ahead = blocks % 5 == 0 ? bound(blocks >> 8, 1, DELAY + WINDOW + 61) : bound(blocks >> 8, DELAY, DELAY + WINDOW - 1);
        vm.roll(block.number + ahead);
        if (randomBook) _drawBook(bookSeed);
        (uint32 asset, bool isBuy,,) = exam.pending();
        vm.recordLogs();
        vm.prank(filler);
        try exam.fill() {
            _checkPrice(asset, isBuy);
            calls["fill"] += 1;
        } catch {
            // After the window and the fallback, anyone may fill an order that only makes the position smaller.
            vm.prank(trader);
            try exam.fill() {
                _checkPrice(asset, isBuy);
                calls["fallbackFill"] += 1;
            } catch {}
        }
    }

    function checkpoint(uint256 secs) external {
        vm.warp((block.timestamp / 1 days + 1) * 1 days + bound(secs, 0, 14 minutes));
        vm.roll(block.number + 1);
        try exam.checkpoint() {
            calls["checkpoint"] += 1;
        } catch {}
    }

    function graduate() external {
        vm.prank(trader);
        try exam.graduate() {
            ghostPassed += 1;
        } catch {}
        calls["graduate"] += 1;
    }

    function breachOrExpire(bool expire) external {
        if (expire) {
            try exam.expire() {
                calls["expire"] += 1;
            } catch {}
        } else {
            try exam.breach() {
                calls["breach"] += 1;
            } catch {}
        }
    }

    function warp(uint256 secs) external {
        vm.warp(block.timestamp + bound(secs, 1, 6 hours));
        vm.roll(block.number + 1);
    }

    function _drawBook(uint256 seed) internal {
        for (uint256 k = 0; k < 2; ++k) {
            uint64 mark = k == 0 ? BTC_MARK : ETH_MARK;
            uint256 s = uint256(keccak256(abi.encode(seed, k)));
            uint256 kind = s % 6;
            // Up to 4 % off the oracle on either side: inside and outside the 1.5 % ceiling.
            uint64 a = uint64(uint256(mark) * ((s >> 8) % 400) / 10_000);
            uint64 b = uint64(uint256(mark) * ((s >> 24) % 400) / 10_000);
            uint64 bid;
            uint64 ask;
            if (kind == 0) (bid, ask) = (mark - a, mark + b); // around
            else if (kind == 1) (bid, ask) = (mark - a - b, mark - a); // under
            else if (kind == 2) (bid, ask) = (mark + a, mark + a + b); // over
            else if (kind == 3) (bid, ask) = (mark + a, mark - b); // crossed
            else if (kind == 4) (bid, ask) = (0, mark + a); // one side empty
            else (bid, ask) = (0, 0); // empty
            book.set(k == 0 ? BTC : ETH, bid, ask);
        }
    }

    /// The fill just made: never better for the trader than the oracle, and at the book never more than `maxWorseBps`
    /// worse than it.
    function _checkPrice(uint32 asset, bool isBuy) internal {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics.length == 0 || logs[i].topics[0] != PaperExam.Filled.selector) continue;
            (, , uint64 px,,, uint8 source) = abi.decode(logs[i].data, (bool, uint64, uint64, uint256, uint256, uint8));
            uint256 oracle = asset == BTC ? BTC_MARK : ETH_MARK;
            uint256 worst = oracle * MAX_WORSE / 10_000;
            if (isBuy ? px < oracle : px > oracle) ghostBadPrice += 1;
            if (source == exam.SOURCE_BOOK() && (isBuy ? px > oracle + worst : px + worst < oracle)) ghostBadPrice += 1;
            if (source != exam.SOURCE_BOOK() && px != oracle) ghostBadPrice += 1;
            fills += 1;
        }
    }
}

/// The paper exam at random with the oracle standing still: no sequence of orders, fills, snapshots and passes earns
/// anything, so none passes (CTO, 13 November: "no pass without the price moving").
contract PaperExamStillOracleInvariantTest is Test {
    PaperExam exam;
    PaperExamHandler handler;

    function _setUp(bool randomBook) internal {
        HyperCore hyperCore = CoreSimulatorLib.init();
        hyperCore.setUseRealL1Read(false);
        vm.etch(0x0000000000000000000000000000000000000807, type(OracleFollowsMark).runtimeCode);
        vm.etch(0x000000000000000000000000000000000000080e, type(BookStub).runtimeCode);
        hyperCore.registerPerpAssetInfo(
            BTC, PrecompileLib.PerpAssetInfo({coin: "BTC", marginTableId: 54, szDecimals: 5, maxLeverage: 40, onlyIsolated: false})
        );
        hyperCore.registerPerpAssetInfo(
            ETH, PrecompileLib.PerpAssetInfo({coin: "ETH", marginTableId: 55, szDecimals: 4, maxLeverage: 25, onlyIsolated: false})
        );
        CoreSimulatorLib.setMarkPx(BTC, BTC_MARK);
        CoreSimulatorLib.setMarkPx(ETH, ETH_MARK);
        // A book a little around the oracle, as a quiet market's; the random one is drawn before each fill.
        BookStub(0x000000000000000000000000000000000000080e).set(BTC, BTC_MARK - 50, BTC_MARK + 50);
        BookStub(0x000000000000000000000000000000000000080e).set(ETH, ETH_MARK - 25, ETH_MARK + 25);
        vm.warp(20_000 days + 12 hours);
        vm.roll(1000);

        uint32[] memory assets = new uint32[](2);
        assets[0] = BTC;
        assets[1] = ETH;
        uint16[] memory targets = new uint16[](1);
        targets[0] = 1000;
        // The first pool's numbers: 10 % to pass, 6 % in all and 3 % a day to lose, 10x, 0.045 % a fill, thirty days.
        Rules memory r = Rules({dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 1000, assets: assets,
            assetLeverageX100: new uint32[](0)});
        ExamTerms memory t = ExamTerms({capital: uint64(uint256(CAPITAL)), targetsBps: targets, stepDuration: 30 days,
            maxIdle: 30 days, minTradingDays: 0, costPpm: 450, fillDelay: DELAY, fillWindow: WINDOW});
        address impl = address(new PaperExam());
        exam = PaperExam(Clones.clone(impl));
        handler = new PaperExamHandler(exam, randomBook);
        exam.initializeWithFiller(handler.trader(), r, t, handler.filler(), true, MAX_WORSE);
        targetContract(address(handler));
        bytes4[] memory sel = new bytes4[](11);
        sel[0] = PaperExamHandler.place.selector;
        sel[1] = PaperExamHandler.place.selector;
        sel[2] = PaperExamHandler.place.selector;
        sel[3] = PaperExamHandler.fill.selector;
        sel[4] = PaperExamHandler.fill.selector;
        sel[5] = PaperExamHandler.fill.selector;
        sel[6] = PaperExamHandler.fill.selector;
        sel[7] = PaperExamHandler.checkpoint.selector;
        sel[8] = PaperExamHandler.graduate.selector;
        sel[9] = PaperExamHandler.breachOrExpire.selector;
        sel[10] = PaperExamHandler.warp.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sel}));
    }

    function setUp() public virtual {
        _setUp(false);
    }

    /// N1. With the oracle still, the exam is never passed, and its equity never rises above the balance it started
    ///     with: every fill costs, and none is better than the oracle.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_noPassWithoutTheOracleMoving() public view {
        assertEq(handler.ghostPassed(), 0, "a pass with the oracle standing still");
        assertTrue(exam.status() != PaperExam.Status.Passed, "Passed with the oracle standing still");
        assertLe(exam.equity(), CAPITAL, "equity above the start with the oracle standing still");
    }

    /// N2. Every fill is no better for the trader than the oracle, and at the book no more than `maxWorseBps` worse.
    /// forge-config: default.invariant.runs = 64
    /// forge-config: default.invariant.depth = 120
    function invariant_everyFillWithinTheOracleAndTheCeiling() public view {
        assertEq(handler.ghostBadPrice(), 0, "a fill better than the oracle, or past the ceiling at the book");
    }

    function afterInvariant() external {
        console2_log();
    }

    function console2_log() internal {
        string memory line = string.concat("reached: place=", vm.toString(handler.calls("place")), " fill=",
            vm.toString(handler.calls("fill")), " fallbackFill=", vm.toString(handler.calls("fallbackFill")),
            " checkpoint=", vm.toString(handler.calls("checkpoint")), " graduate=", vm.toString(handler.calls("graduate")),
            " breach=", vm.toString(handler.calls("breach")), " expire=", vm.toString(handler.calls("expire")),
            " fillsChecked=", vm.toString(handler.fills()));
        emit log(line);
    }
}

/// The same with the book drawn again before every fill.
contract PaperExamRandomBookInvariantTest is PaperExamStillOracleInvariantTest {
    function setUp() public override {
        _setUp(true);
    }
}
