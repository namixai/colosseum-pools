// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";
import {CoreOps} from "../lib/CoreOps.sol";

/// What the book asks of an exam: carry out its stop or take touched at `px` in block `touchBlock`. It answers
/// `FILLED`, `CLOSED` (filled, and the position is closed, so its other trigger is gone too) or `ENDED` (the exam
/// had ended or ran out of time: nothing filled, the trigger removed), and calls nothing in the book while it does.
interface IStopExam {
    function fireTrigger(uint32 asset, bool isTake, uint64 level, uint64 armedAt, uint64 px, uint64 touchBlock)
        external
        returns (uint8);
}

/// The factory's register of its pools and their exams.
interface IPoolRegister {
    function poolOf(address account) external view returns (address);
}

/// @title StopBook
/// @notice Every stop and take of the exams of one simulated-pool factory, by asset, and the check that carries
///         them out. Anyone may call `poke()`, once a block does the work: it reads each asset's oracle price
///         once and, in the same call, carries out every stop and take that price has reached, at that price.
///         The touch and the fill are one transaction in one block, so a touch is never missed in a block the
///         book checked, and nobody can take a touched stop back: every exam function that changes anything
///         calls `settle` first (`PaperExam`), which checks the block before the exam does anything else.
/// @dev On each asset two heaps of armed entries: `below`, entries that fire at a price at or under their
///      level (a long's stop, a short's take), the highest level on top; `above`, those that fire at or over it
///      (a short's stop, a long's take), the lowest on top. A check takes off the top all the price has reached,
///      log(n) an entry. An entry set in block N acts from block `armedAt` (N plus the exam's `fillDelay`): until
///      then it waits in a third heap, by `armedAt`, which each check empties into the two first, so an entry
///      armed in this block is checked in it.
///
///      A check carries out at most `MAX_FIRES` entries, each in a call of at most `FIRE_GAS` gas. A touched entry
///      leaves the book only once its exam has answered: a call that fails, whatever the reason, leaves the entry
///      latched at the price and block of its touch, never erased, and the exam's other trigger, once a fill has
///      closed the position, is taken out by the book after the call, with the check's own gas. An entry reached
///      past `MAX_FIRES`, or whose exam still has an earlier touch latched, is latched: taken off its heap with
///      this block's price and number, and carried out at that price by a later check or by its exam's next
///      `settle`, the oldest touch of the exam first; while an exam has a latched entry, `settle` tells it so
///      and the exam refuses to act. At most `MAX_LATCHES` entries are latched a check; past them the rest
///      stays on its heap, and the book remembers, for that side of the asset, the price and block of the touch
///      (`TouchBacklog`, `overloadOf`): the next checks carry the rest out at that price and block before they look at
///      their own price there, and meanwhile `settle` holds every exam with an entry that price reached, as if latched.
///      An overload delays a fill; it never changes its price. An entry armed after that touch which its price would
///      reach waits to be armed until the overload is done. A check that would start with less than `POKE_GAS` gas
///      reverts, so a call too poor to do a check's whole work cannot take the block from one that can. The caps are
///      small, so that a check plus the call of the exam that paid for it fits a small HyperEVM block with room.
///
///      No way out is built for a latched entry whose exam's call fails at every try: that exam stays held
///      (`StopsPending`). A `PaperExam`'s `fireTrigger` has no loop and calls nothing in the book; its gas is flat.
///      A block nobody checked is shown by
///      the next check (`Gap`).
///
///      Block numbers are kept in 40 bits: room for some 34 000 years of one-second blocks.
contract StopBook {
    /// Entries moved from waiting to armed in one check.
    uint256 public constant MAX_ARMS = 3;
    /// Entries carried out in one check, latched ones first.
    uint256 public constant MAX_FIRES = 2;
    /// Entries latched in one check.
    uint256 public constant MAX_LATCHES = 2;
    /// Exams of the latch queue one check visits.
    uint256 public constant MAX_QUEUE_STEPS = 4;
    /// Gas each exam's `fireTrigger` gets.
    uint256 public constant FIRE_GAS = 150_000;
    /// The gas a check keeps for itself after a `fireTrigger`: the book's own records and events.
    uint256 internal constant AFTER_FIRE = 40_000;
    /// The least gas a check starts with: its whole work at the caps, cold, with room (some 627 000 measured,
    /// `test_gas_ofACheckAtItsCaps_underASmallBlock`), so that a check that starts never runs short of a fill's gas.
    /// Small caps are safe: what they leave is carried out at the price of its touch (`overloadOf`).
    uint256 public constant POKE_GAS = 800_000;

    /// What an exam's `fireTrigger` answered (`Fired.result`): its call failed (the entry stays latched), it filled,
    /// it filled and closed the position, or the exam had ended.
    uint8 public constant FAILED = 0;
    uint8 public constant FILLED = 1;
    uint8 public constant CLOSED = 2;
    uint8 public constant ENDED = 3;

    // Where an entry is (`Slot.where`).
    uint8 internal constant NONE = 0;
    uint8 internal constant WAIT_BELOW = 1;
    uint8 internal constant WAIT_ABOVE = 2;
    uint8 internal constant BELOW = 3;
    uint8 internal constant ABOVE = 4;
    uint8 internal constant LATCHED = 5;

    /// One stop or take: where it is and at which index there, its level and `armedAt`, and, latched, the
    /// price and block of its touch. One word.
    struct Slot {
        uint8 where;
        uint32 pos;
        uint64 level;
        uint40 armedAt;
        uint64 px;
        uint40 touchBlock;
    }

    /// The factory whose exams alone may add and remove entries.
    address public immutable factory;

    /// The last block checked, and the lock of a check: one word.
    uint64 public lastPokeBlock;
    bool internal _locked;

    mapping(address exam => mapping(uint32 asset => mapping(bool isTake => Slot))) internal _slot;
    /// Armed entries, as `level << 161 | exam << 1 | isTake`.
    mapping(uint32 asset => uint256[]) internal _below;
    mapping(uint32 asset => uint256[]) internal _above;
    /// Entries waiting for `armedAt`, as `armedAt << 193 | exam << 33 | asset << 1 | isTake`.
    uint256[] internal _waiting;
    /// The assets with an armed entry, and each one's index there plus one.
    uint32[] internal _assets;
    mapping(uint32 asset => uint256) internal _assetPos;
    /// An exam's latched entries, as `asset << 1 | isTake`, and the exams that had one, in order.
    mapping(address exam => uint256[]) internal _latched;
    address[] internal _latchQueue;
    uint256 internal _latchHead;
    /// What an overload left on each side of an asset: the price and block of the touch the rest of that heap is
    /// carried out at (block zero: none); and how many sides have one.
    struct Overload {
        uint64 belowPx;
        uint40 belowBlock;
        uint64 abovePx;
        uint40 aboveBlock;
    }

    mapping(uint32 asset => Overload) internal _overloads;
    uint256 internal _overloadSides;

    /// What one check has left to do: fills and latches made so far.
    struct Work {
        uint256 fires;
        uint256 latches;
    }

    /// The oracle price of `asset` this block, read by a check with an armed entry on it.
    event PriceSeen(uint32 indexed asset, uint64 px);
    /// The oracle gave no price for `asset` this block: its entries wait for the next check.
    event NoPrice(uint32 indexed asset);
    /// No check from block `from` to block `to`, both counted.
    event Gap(uint256 from, uint256 to);
    /// More entries reached `armedAt` than one check arms: `waiting` still wait, the rest are armed next check.
    event ArmingBacklog(uint256 waiting);
    /// More entries on one side of `asset` were reached at `px` in `touchBlock` than one check carries out and latches:
    /// the rest are carried out at that price by the next checks (`overloadOf`). An alarm: fills delayed.
    event TouchBacklog(uint32 indexed asset, bool below, uint64 px, uint256 touchBlock);
    event Latched(address indexed exam, uint32 indexed asset, bool isTake, uint64 px, uint256 touchBlock);
    /// `result`: `FILLED`, `CLOSED`, `ENDED`, or `FAILED` (the call failed: the entry is latched, to be tried again).
    event Fired(address indexed exam, uint32 indexed asset, bool isTake, uint64 px, uint256 touchBlock, uint8 result);

    error NotExam();
    error Reentered();
    /// A check needs at least `needed` gas to start.
    error PokeGas(uint256 needed);

    constructor() {
        factory = msg.sender;
    }

    // ── exams ────────────────────────────────────────────────────

    /// @notice Sets the caller's stop or take on `asset`, replacing the one there: it fires at a price at or under
    ///         `level` (`below`) or at or over it, from block `armedAt`. Only an exam of the factory, and not from
    ///         inside a check.
    function add(uint32 asset, bool isTake, bool below, uint64 level, uint64 armedAt) external {
        _onlyExam();
        Slot memory s = _slot[msg.sender][asset][isTake];
        if (s.where != NONE) _drop(msg.sender, asset, isTake, s);
        _slot[msg.sender][asset][isTake] =
            Slot(below ? WAIT_BELOW : WAIT_ABOVE, 0, level, uint40(armedAt), 0, 0);
        _push(
            _waiting,
            uint256(armedAt) << 193 | uint256(uint160(msg.sender)) << 33 | uint256(asset) << 1 | (isTake ? 1 : 0),
            false,
            0,
            true
        );
    }

    /// @notice Removes the caller's stop or take on `asset`, if it has one here. Only an exam of the factory, and
    ///         not from inside a check.
    function remove(uint32 asset, bool isTake) external {
        _onlyExam();
        Slot memory s = _slot[msg.sender][asset][isTake];
        if (s.where != NONE) _drop(msg.sender, asset, isTake, s);
    }

    // ── anyone ───────────────────────────────────────────────────

    /// @notice Checks this block: carries out every armed stop and take the oracle price has reached. Once a
    ///         block; a second call in it does nothing. The first needs `POKE_GAS` gas left, or reverts.
    function poke() external {
        _poke();
    }

    /// @notice `poke()`, then carries out what `exam` has latched, the oldest touch first, as far as the gas goes.
    ///         Returns how many of its entries still wait: latched, or on a heap an overload's price has reached and
    ///         later checks are still working off (`overloadOf`). An exam acts only once none does.
    function settle(address exam) external returns (uint256) {
        _poke();
        if (_latched[exam].length != 0) {
            _enter();
            _drainExam(exam, 0, type(uint256).max);
            _locked = false;
        }
        uint256 n = _latched[exam].length;
        if (_overloadSides != 0) n += _held(exam);
        return n;
    }

    // ── views ────────────────────────────────────────────────────

    /// @notice How many of `exam`'s entries are latched.
    function pending(address exam) external view returns (uint256) {
        return _latched[exam].length;
    }

    /// @notice The top of `asset`'s two heaps (zero if empty) and their sizes.
    function top(uint32 asset) external view returns (uint64 below, uint64 above, uint256 nBelow, uint256 nAbove) {
        nBelow = _below[asset].length;
        nAbove = _above[asset].length;
        if (nBelow != 0) below = uint64(_below[asset][0] >> 161);
        if (nAbove != 0) above = uint64(_above[asset][0] >> 161);
    }

    /// @notice Where `exam`'s stop or take on `asset` is (0 none, 1 and 2 waiting, 3 below, 4 above, 5 latched),
    ///         its level and `armedAt`, and, latched, its price and block.
    function slotOf(address exam, uint32 asset, bool isTake) external view returns (Slot memory) {
        return _slot[exam][asset][isTake];
    }

    /// @notice What an overload left on `asset`: on each side, the price and block of the touch the rest of the heap is
    ///         carried out at; block zero, none.
    function overloadOf(uint32 asset) external view returns (Overload memory) {
        return _overloads[asset];
    }

    /// @notice The assets with an armed entry, and how many entries wait for `armedAt`.
    function watched() external view returns (uint32[] memory assets, uint256 waiting) {
        return (_assets, _waiting.length);
    }

    // ── the check ────────────────────────────────────────────────

    function _poke() internal {
        uint256 last = lastPokeBlock;
        if (last == block.number) return;
        // A call that cannot pay for the whole check does not take the block: it reverts, and the next one checks.
        if (gasleft() < POKE_GAS) revert PokeGas(POKE_GAS);
        _enter();
        if (last != 0 && block.number > last + 1) emit Gap(last + 1, block.number - 1);
        lastPokeBlock = uint64(block.number);
        Work memory w = Work(_drainQueue(), 0);
        // First what overloads left, at the price and block of their touch; then the entries due are armed, those an
        // unfinished overload's price would reach waiting; then this block's prices, on every side with none left.
        // From the end: an asset whose heaps empty leaves its place to the last one, already checked.
        if (_overloadSides != 0) {
            for (uint256 i = _assets.length; i > 0; --i) {
                uint32 a = _assets[i - 1];
                _workOff(a, true, w);
                _workOff(a, false, w);
                _maybeIdle(a);
            }
        }
        _arm();
        for (uint256 i = _assets.length; i > 0; --i) {
            uint32 a = _assets[i - 1];
            uint64 px = _oracle(a);
            if (px == 0) {
                emit NoPrice(a);
            } else {
                emit PriceSeen(a, px);
                _side(a, true, px, w);
                _side(a, false, px, w);
            }
            _maybeIdle(a);
        }
        _locked = false;
    }

    /// What an overload left on one side of `asset`, carried out at the price and block of its touch; the overload is
    /// done once nothing it reached is left.
    function _workOff(uint32 asset, bool below, Work memory w) internal {
        (uint64 opx, uint40 ob) = _overload(asset, below);
        if (ob != 0 && _touch(asset, below, opx, ob, w)) _setOverload(asset, below, 0, 0);
    }

    /// What this block's price `px` reaches on one side of `asset`, if no overload is left there. What the caps leave
    /// waits with this price and block: an overload delays a fill, it never changes its price.
    function _side(uint32 asset, bool below, uint64 px, Work memory w) internal {
        (, uint40 ob) = _overload(asset, below);
        if (ob == 0 && !_touch(asset, below, px, block.number, w)) {
            _setOverload(asset, below, px, uint40(block.number));
        }
    }

    /// Moves the entries whose `armedAt` has come from waiting to their heap. One that an overload's price would reach
    /// waits until that overload is done: it was not armed at its touch, and is not carried out at its price.
    function _arm() internal {
        uint256[MAX_ARMS] memory later;
        uint256 nLater;
        for (uint256 n = 0; _waiting.length != 0; ++n) {
            uint256 e = _waiting[0];
            if (e >> 193 > block.number) break;
            if (n == MAX_ARMS) {
                emit ArmingBacklog(_waiting.length + nLater);
                break;
            }
            address exam = address(uint160(e >> 33));
            uint32 asset = uint32(e >> 1);
            bool isTake = e & 1 == 1;
            _removeAt(_waiting, 0, false, 0, true);
            Slot storage s = _slot[exam][asset][isTake];
            bool below = s.where == WAIT_BELOW;
            // An overload's price is carried out on what was armed at its touch: one it would reach, armed since, waits.
            if (_reachedBy(asset, below, s.level)) {
                later[nLater++] = e;
                continue;
            }
            s.where = below ? BELOW : ABOVE;
            if (_assetPos[asset] == 0) {
                _assets.push(asset);
                _assetPos[asset] = _assets.length;
            }
            _push(
                below ? _below[asset] : _above[asset],
                uint256(s.level) << 161 | uint256(uint160(exam)) << 1 | (isTake ? 1 : 0),
                below,
                asset,
                false
            );
        }
        for (uint256 i = 0; i < nLater; ++i) {
            _push(_waiting, later[i], false, 0, true);
        }
    }

    /// Whether an overload on `asset`'s `below` side has a price that reaches `level`.
    function _reachedBy(uint32 asset, bool below, uint64 level) internal view returns (bool) {
        (uint64 opx, uint40 ob) = _overload(asset, below);
        return ob != 0 && (below ? opx <= level : opx >= level);
    }

    function _overload(uint32 asset, bool below) internal view returns (uint64, uint40) {
        Overload storage o = _overloads[asset];
        return below ? (o.belowPx, o.belowBlock) : (o.abovePx, o.aboveBlock);
    }

    /// Sets (`b` not zero) or clears what an overload left on one side of `asset`.
    function _setOverload(uint32 asset, bool below, uint64 px, uint40 b) internal {
        Overload storage o = _overloads[asset];
        uint40 was = below ? o.belowBlock : o.aboveBlock;
        if (was == 0 && b != 0) ++_overloadSides;
        else if (was != 0 && b == 0) --_overloadSides;
        if (below) (o.belowPx, o.belowBlock) = (px, b);
        else (o.abovePx, o.aboveBlock) = (px, b);
    }

    /// Takes off `asset`'s heap everything `px`, seen in block `touchBlock`, has reached: carried out while the check
    /// has fires and gas left and its exam has no earlier touch latched, latched otherwise. Returns false if it stopped
    /// at the caps with more reached.
    function _touch(uint32 asset, bool below, uint64 px, uint256 touchBlock, Work memory w) internal returns (bool) {
        uint256[] storage h = below ? _below[asset] : _above[asset];
        while (h.length != 0) {
            uint256 e = h[0];
            uint64 level = uint64(e >> 161);
            if (below ? px > level : px < level) break;
            // An exam with an earlier touch latched has that one carried out first: this one waits behind it.
            bool fire = w.fires < MAX_FIRES && _latched[address(uint160(e >> 1))].length == 0 && _gasForFire();
            if (!fire && w.latches == MAX_LATCHES) {
                emit TouchBacklog(asset, below, px, touchBlock);
                return false;
            }
            _removeAt(h, 0, below, asset, false);
            if (fire) ++w.fires;
            else ++w.latches;
            _reached(asset, e, px, touchBlock, fire);
        }
        return true;
    }

    /// The entry `e`, just taken off `asset`'s heap, reached at `px` in `touchBlock`: carried out with `fire`, latched
    /// without, and latched too if its call failed.
    function _reached(uint32 asset, uint256 e, uint64 px, uint256 touchBlock, bool fire) internal {
        address exam = address(uint160(e >> 1));
        bool isTake = e & 1 == 1;
        if (fire) {
            uint8 r = _call(exam, asset, isTake, px, touchBlock);
            if (r != FAILED) return _done(exam, asset, isTake, r);
        }
        _latch(exam, asset, isTake, px, touchBlock);
    }

    /// Latches `exam`'s entry, just taken off its heap, with `px` and `touchBlock`.
    function _latch(address exam, uint32 asset, bool isTake, uint64 px, uint256 touchBlock) internal {
        Slot storage s = _slot[exam][asset][isTake];
        uint256[] storage l = _latched[exam];
        if (l.length == 0) _latchQueue.push(exam);
        (s.where, s.pos, s.px, s.touchBlock) = (LATCHED, uint32(l.length), px, uint40(touchBlock));
        l.push(uint256(asset) << 1 | (isTake ? 1 : 0));
        emit Latched(exam, asset, isTake, px, touchBlock);
    }

    /// Carries out latched entries, the oldest exams first, while the check has fires left. An exam whose call
    /// failed goes to the back of the queue: it does not hold up the exams behind it, and is tried again by the next
    /// check, once a check at most.
    function _drainQueue() internal returns (uint256 fires) {
        uint256 end = _latchQueue.length;
        for (uint256 n = 0; n < MAX_QUEUE_STEPS && _latchHead < end; ++n) {
            address exam = _latchQueue[_latchHead];
            bool failed;
            (fires, failed) = _drainExam(exam, fires, MAX_FIRES);
            if (!failed && _latched[exam].length != 0) return fires;
            delete _latchQueue[_latchHead];
            ++_latchHead;
            if (failed) _latchQueue.push(exam);
        }
    }

    /// Carries out `exam`'s latched entries at the price of their touch, the oldest touch first, while `fires` is
    /// under `cap` and the gas lasts; stops at a call that fails, which stays latched.
    function _drainExam(address exam, uint256 fires, uint256 cap) internal returns (uint256, bool) {
        uint256[] storage l = _latched[exam];
        while (l.length != 0 && fires < cap && _gasForFire()) {
            uint256 i;
            uint256 oldest = type(uint256).max;
            for (uint256 j = 0; j < l.length; ++j) {
                uint256 kj = l[j];
                uint256 b = _slot[exam][uint32(kj >> 1)][kj & 1 == 1].touchBlock;
                if (b < oldest) (i, oldest) = (j, b);
            }
            uint256 k = l[i];
            uint32 asset = uint32(k >> 1);
            bool isTake = k & 1 == 1;
            Slot storage s = _slot[exam][asset][isTake];
            ++fires;
            uint8 r = _call(exam, asset, isTake, s.px, s.touchBlock);
            if (r == FAILED) return (fires, true);
            _unlatchAt(exam, i);
            _done(exam, asset, isTake, r);
        }
        return (fires, false);
    }

    /// One exam's fill, in a call of `FIRE_GAS`: what it answered, or `FAILED` if it reverted. The exam calls nothing
    /// in the book meanwhile (`add` and `remove` refuse inside a check), so the book is as it was before the call.
    function _call(address exam, uint32 asset, bool isTake, uint64 px, uint256 touchBlock) internal returns (uint8 r) {
        Slot storage s = _slot[exam][asset][isTake];
        try IStopExam(exam).fireTrigger{gas: FIRE_GAS}(asset, isTake, s.level, s.armedAt, px, uint64(touchBlock)) returns (
            uint8 x
        ) {
            r = x;
        } catch {}
        emit Fired(exam, asset, isTake, px, touchBlock, r);
    }

    /// The entry the exam has answered for leaves the book; a fill that closed the position takes its other trigger
    /// with it, as the exam has already let go of it.
    function _done(address exam, uint32 asset, bool isTake, uint8 r) internal {
        delete _slot[exam][asset][isTake];
        if (r == CLOSED) {
            Slot memory o = _slot[exam][asset][!isTake];
            if (o.where != NONE) _drop(exam, asset, !isTake, o);
        }
    }

    /// Whether the gas left gives a `fireTrigger` its whole `FIRE_GAS` (a call gets 63/64 of what is left) and the
    /// check what it needs after it: so whoever calls cannot starve one fill and keep the check going.
    function _gasForFire() internal view returns (bool) {
        return gasleft() > FIRE_GAS * 64 / 63 + AFTER_FIRE;
    }

    /// The oracle price of `asset` now; zero if the precompile did not answer or answered no uint64, so that no
    /// answer of it can make a check revert.
    function _oracle(uint32 asset) internal view returns (uint64) {
        (bool ok, bytes memory r) =
            HLConstants.ORACLE_PX_PRECOMPILE_ADDRESS.staticcall(abi.encode(CoreOps.pxIndex(asset)));
        if (!ok || r.length < 32) return 0;
        uint256 px = abi.decode(r, (uint256));
        return px > type(uint64).max ? 0 : uint64(px);
    }

    function _enter() internal {
        if (_locked) revert Reentered();
        _locked = true;
    }

    function _onlyExam() internal view {
        if (_locked) revert Reentered();
        address p = IPoolRegister(factory).poolOf(msg.sender);
        if (p == address(0) || p == msg.sender) revert NotExam();
    }

    /// Takes the entry `s` of `exam` out of wherever it is.
    function _drop(address exam, uint32 asset, bool isTake, Slot memory s) internal {
        if (s.where == LATCHED) {
            _unlatchAt(exam, s.pos);
        } else if (s.where == BELOW) {
            _removeAt(_below[asset], s.pos, true, asset, false);
        } else if (s.where == ABOVE) {
            _removeAt(_above[asset], s.pos, false, asset, false);
        } else {
            _removeAt(_waiting, s.pos, false, 0, true);
        }
        delete _slot[exam][asset][isTake];
        _maybeIdle(asset);
    }

    /// Takes the `i`-th of `exam`'s latched entries off its list, the last one taking its place.
    function _unlatchAt(address exam, uint256 i) internal {
        uint256[] storage l = _latched[exam];
        uint256 last = l.length - 1;
        if (i != last) {
            uint256 k = l[last];
            l[i] = k;
            _slot[exam][uint32(k >> 1)][k & 1 == 1].pos = uint32(i);
        }
        l.pop();
    }

    /// How many of `exam`'s armed entries an overload's price has reached: later checks carry them out at it.
    function _held(address exam) internal view returns (uint256 n) {
        for (uint256 i = 0; i < _assets.length; ++i) {
            uint32 a = _assets[i];
            for (uint256 k = 0; k < 2; ++k) {
                Slot storage s = _slot[exam][a][k == 1];
                uint8 where = s.where;
                if ((where == BELOW || where == ABOVE) && _reachedBy(a, where == BELOW, s.level)) ++n;
            }
        }
    }

    /// Takes `asset` off the list once both its heaps are empty, and with them what an overload left.
    function _maybeIdle(uint32 asset) internal {
        uint256 p = _assetPos[asset];
        if (p == 0 || _below[asset].length != 0 || _above[asset].length != 0) return;
        _setOverload(asset, true, 0, 0);
        _setOverload(asset, false, 0, 0);
        uint32 last = _assets[_assets.length - 1];
        _assets[p - 1] = last;
        _assetPos[last] = p;
        _assets.pop();
        delete _assetPos[asset];
    }

    // ── heaps ────────────────────────────────────────────────────
    // `max`: the largest word on top (`below`); otherwise the smallest (`above`, waiting). `waiting`: the word carries
    // its asset (the waiting heap); otherwise `asset` is the heap's. Every move writes the entry's new index.

    function _push(uint256[] storage h, uint256 e, bool max, uint32 asset, bool waiting) internal {
        h.push(e);
        _up(h, h.length - 1, e, max, asset, waiting);
    }

    function _removeAt(uint256[] storage h, uint256 i, bool max, uint32 asset, bool waiting) internal {
        uint256 last = h.length - 1;
        uint256 m = h[last];
        h.pop();
        if (i == last) return;
        if (i > 0 && (max ? m > h[(i - 1) / 2] : m < h[(i - 1) / 2])) _up(h, i, m, max, asset, waiting);
        else _down(h, i, m, max, asset, waiting);
    }

    function _up(uint256[] storage h, uint256 i, uint256 e, bool max, uint32 asset, bool waiting) internal {
        while (i > 0) {
            uint256 p = (i - 1) / 2;
            uint256 pe = h[p];
            if (max ? e <= pe : e >= pe) break;
            _set(h, i, pe, asset, waiting);
            i = p;
        }
        _set(h, i, e, asset, waiting);
    }

    function _down(uint256[] storage h, uint256 i, uint256 e, bool max, uint32 asset, bool waiting) internal {
        uint256 n = h.length;
        while (true) {
            uint256 c = 2 * i + 1;
            if (c >= n) break;
            uint256 ce = h[c];
            if (c + 1 < n) {
                uint256 r = h[c + 1];
                if (max ? r > ce : r < ce) (c, ce) = (c + 1, r);
            }
            if (max ? ce <= e : ce >= e) break;
            _set(h, i, ce, asset, waiting);
            i = c;
        }
        _set(h, i, e, asset, waiting);
    }

    function _set(uint256[] storage h, uint256 i, uint256 e, uint32 asset, bool waiting) internal {
        h[i] = e;
        if (waiting) _slot[address(uint160(e >> 33))][uint32(e >> 1)][e & 1 == 1].pos = uint32(i);
        else _slot[address(uint160(e >> 1))][asset][e & 1 == 1].pos = uint32(i);
    }
}
