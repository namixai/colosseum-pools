// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Test.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";

import {PaperExam} from "../../src/exam/PaperExam.sol";
import {StopBook} from "../../src/sim/StopBook.sol";
import {StopBookBase} from "./StopBook.t.sol";

/// The stop book against a model of the market: a random run of prices by block and of what the traders do (set, move
/// and remove stops and takes, longs and shorts, several exams and two assets, blocks with crowds that overload a
/// check and blocks nobody checks), then the book is checked until it has nothing left. Every armed stop or take must
/// have filled at the price of the first checked block, from its `armedAt`, whose oracle price reached it: the model
/// fills it there, the contract may fill it later but at that price and block. At the end the positions and the cash of
/// every exam, and the price and block of every fill, are the model's; no trigger the model has not filled is gone, and
/// none the model has filled is left.
contract StopBookModelTest is StopBookBase {
    uint256 constant N = 8; // exams 0..3 long, 4..7 short, 100 units on BTC and on ETH
    uint256 constant STEPS = 48;

    PaperExam[N] ex;

    // The model. A trigger: its level, armedAt, which side it fires on; zero level, none.
    struct T {
        uint64 level;
        uint64 armedAt;
        bool below;
    }

    mapping(uint256 => mapping(uint32 => mapping(bool => T))) mt; // exam, asset, isTake
    mapping(uint256 => mapping(uint32 => int256)) mpos;
    mapping(uint256 => int256) mcash;
    mapping(uint256 => mapping(uint32 => mapping(bool => uint256))) mFillPx; // what the model filled, at what price
    mapping(uint256 => mapping(uint32 => mapping(bool => uint256))) mFillBlock;
    mapping(uint256 => mapping(uint32 => mapping(bool => uint256))) cFillPx; // what the contract filled
    mapping(uint256 => mapping(uint32 => mapping(bool => uint256))) cFillBlock;
    uint256 lastChecked;
    uint64 pxBtc;
    uint64 pxEth;
    uint256 rnd;

    function setUp() public override {
        super.setUp();
        for (uint256 i = 0; i < N; ++i) {
            ex[i] = _exam();
        }
        for (uint256 i = 0; i < N; ++i) {
            _trade(ex[i], BTC, i < 4, 100);
            _trade(ex[i], ETH, i < 4, 100);
        }
        vm.roll(block.number + 1);
        stopBook.poke();
    }

    function _next(uint256 m) internal returns (uint256) {
        rnd = uint256(keccak256(abi.encode(rnd)));
        return rnd % m;
    }

    function _setPx(uint32 a, uint64 px) internal {
        CoreSimulatorLib.setMarkPx(a, px);
    }

    function _pxOf(uint32 a) internal view returns (uint64) {
        return a == BTC ? pxBtc : pxEth;
    }

    /// The model checks this block, once, at this block's prices: every armed trigger they reach fills, at them.
    function _modelCheck() internal {
        if (lastChecked == block.number) return;
        lastChecked = block.number;
        for (uint256 i = 0; i < N; ++i) {
            for (uint256 k = 0; k < 2; ++k) {
                uint32 a = k == 0 ? BTC : ETH;
                uint64 px = _pxOf(a);
                for (uint256 t = 0; t < 2; ++t) {
                    T memory tr = mt[i][a][t == 1];
                    if (tr.level == 0 || tr.armedAt > block.number) continue;
                    if (tr.below ? px > tr.level : px < tr.level) continue;
                    // Filled, the whole position: the other trigger goes with it.
                    int256 p = mpos[i][a];
                    mcash[i] += p * int256(uint256(px)); // the cash of the sale, or what the buy-back costs
                    mpos[i][a] = 0;
                    mFillPx[i][a][t == 1] = px;
                    mFillBlock[i][a][t == 1] = block.number;
                    delete mt[i][a][true];
                    delete mt[i][a][false];
                    break;
                }
            }
        }
    }

    function _newBlock(bool jump) internal {
        vm.roll(block.number + 1);
        for (uint256 k = 0; k < 2; ++k) {
            uint32 a = k == 0 ? BTC : ETH;
            uint64 base = a == BTC ? MARK : ETH_MARK;
            uint64 px = _pxOf(a);
            uint256 bps = jump ? 300 + _next(600) : _next(150);
            uint256 d = uint256(px) * bps / 10_000;
            uint64 nx = _next(2) == 0 ? px + uint64(d) : px - uint64(d);
            if (nx > base * 12 / 10) nx = base * 12 / 10;
            if (nx < base * 8 / 10) nx = base * 8 / 10;
            if (a == BTC) pxBtc = nx;
            else pxEth = nx;
            _setPx(a, nx);
        }
    }

    function _poke() internal {
        vm.recordLogs();
        stopBook.poke();
        _take(true);
        _modelCheck();
    }

    /// The fills of the call just made, kept if it went through (the recorder also keeps what a reverted call emitted).
    function _take(bool ok) internal {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        if (!ok) return;
        bytes32 sig = PaperExam.TriggerFilled.selector;
        for (uint256 j = 0; j < logs.length; ++j) {
            if (logs[j].topics[0] != sig) continue;
            uint256 i = N;
            for (uint256 q = 0; q < N; ++q) {
                if (logs[j].emitter == address(ex[q])) i = q;
            }
            if (i == N) continue;
            uint32 a = uint32(uint256(logs[j].topics[1]));
            (bool isTake,, uint64 px,,,, uint256 touch) =
                abi.decode(logs[j].data, (bool, uint64, uint64, uint256, uint64, uint8, uint256));
            assertEq(cFillBlock[i][a][isTake], 0, "filled once");
            (cFillPx[i][a][isTake], cFillBlock[i][a][isTake]) = (px, touch);
        }
    }

    /// A stop or a take on exam `i`, `bps` from the price, on the side it may be set on, not crossing its other trigger.
    function _trySet(uint256 i, uint32 a, bool isTake, uint256 bps) internal {
        bool isLong = i < 4;
        bool below = isTake != isLong;
        uint64 px = _pxOf(a);
        uint64 d = uint64(uint256(px) * bps / 10_000) + 1;
        uint64 level = below ? px - d : px + d;
        T memory other = mt[i][a][!isTake];
        if (other.level != 0 && (below ? level >= other.level : level <= other.level)) return;
        vm.recordLogs();
        vm.prank(trader);
        try ex[i].setTrigger(a, isTake, level, 0) {
            _take(true);
            _modelCheck();
            assertTrue(mpos[i][a] != 0, "a trigger set on a position the model has closed");
            mt[i][a][isTake] = T(level, uint64(block.number) + DELAY, below);
        } catch {
            _take(false);
        }
    }

    function _tryClear(uint256 i, uint32 a, bool isTake) internal {
        vm.recordLogs();
        vm.prank(trader);
        try ex[i].clearTrigger(a, isTake) {
            _take(true);
            _modelCheck();
            assertTrue(mt[i][a][isTake].level != 0, "a trigger the model has filled was removed");
            delete mt[i][a][isTake];
        } catch {
            _take(false);
        }
    }

    function _run(uint256 seed) internal {
        rnd = seed;
        (pxBtc, pxEth) = (MARK, ETH_MARK);
        lastChecked = stopBook.lastPokeBlock();
        for (uint256 i = 0; i < N; ++i) {
            mpos[i][BTC] = i < 4 ? int256(100) : -100;
            mpos[i][ETH] = mpos[i][BTC];
            mcash[i] = int256(ex[i].cash());
        }
        for (uint256 s = 0; s < STEPS; ++s) {
            uint256 r = _next(100);
            if (r < 30) {
                _newBlock(_next(8) == 0);
                if (_next(10) < 7) _poke();
            } else if (r < 36) {
                vm.roll(block.number + 1 + _next(3)); // blocks nobody checks, the price as it was
            } else if (r < 66) {
                _trySet(_next(N), _next(3) == 0 ? ETH : BTC, _next(2) == 0, 20 + _next(400));
            } else if (r < 76) {
                _tryClear(_next(N), _next(3) == 0 ? ETH : BTC, _next(2) == 0);
            } else {
                // A crowd: every exam sets the same kind of trigger close to the price, in one block.
                uint32 a = _next(3) == 0 ? ETH : BTC;
                bool isTake = _next(2) == 0;
                uint256 bps = 20 + _next(150);
                for (uint256 i = 0; i < N; ++i) {
                    _trySet(i, a, isTake, bps + i);
                }
            }
        }
        // The prices stay; the book is checked until nothing it holds waits.
        for (uint256 k = 0; k < 40; ++k) {
            vm.roll(block.number + 1);
            _poke();
        }
        _compare();
    }

    function _compare() internal {
        for (uint256 i = 0; i < N; ++i) {
            for (uint256 k = 0; k < 4; ++k) {
                uint32 a = k < 2 ? BTC : ETH;
                bool t = k % 2 == 1;
                assertEq(cFillPx[i][a][t], mFillPx[i][a][t], "fill price: the first touch's");
                assertEq(
                    cFillBlock[i][a][t], mFillBlock[i][a][t], "touch block: the first checked block that reached it"
                );
            }
        }
        for (uint256 i = 0; i < N; ++i) {
            assertEq(stopBook.pending(address(ex[i])), 0, "nothing latched is left");
            for (uint256 k = 0; k < 2; ++k) {
                uint32 a = k == 0 ? BTC : ETH;
                assertEq(ex[i].positionOf(a), mpos[i][a], "position");
                for (uint256 t = 0; t < 2; ++t) {
                    uint8 w = stopBook.slotOf(address(ex[i]), a, t == 1).where;
                    // Left in the book exactly what the model has not filled, and armed by now.
                    assertEq(w != 0, mt[i][a][t == 1].level != 0, "the book holds what the model holds");
                    if (w != 0) assertTrue(w == 3 || w == 4, "armed, not latched, not waiting");
                }
            }
            assertEq(ex[i].cash(), mcash[i], "cash");
        }
    }

    /// forge-config: default.fuzz.runs = 10000
    function testFuzz_model_everyTouchFillsAtItsFirstCheckedPrice(uint256 seed) public {
        _run(seed);
    }
}
