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
///      and the exam refuses to act. At most `MAX_LATCHES` entries are latched a check; past them the rest stays
///      on its heap for the next check, at that block's price (`TouchBacklog`: an alarm, a touch carried out at a
///      later price). A check that would start with less than `POKE_GAS` gas reverts, so a call too poor to do a
///      check's whole work cannot take the block from one that can. The caps keep a check, plus the call of the
///      exam that paid for it, inside a small HyperEVM block of 3 000 000 gas. A block nobody checked is shown by
///      the next check (`Gap`).
///
///      Block numbers are kept in 40 bits: room for some 34 000 years of one-second blocks.
contract StopBook {
    /// Entries moved from waiting to armed in one check.
    uint256 public constant MAX_ARMS = 8;
    /// Entries carried out in one check, latched ones first.
    uint256 public constant MAX_FIRES = 3;
    /// Entries latched in one check.
    uint256 public constant MAX_LATCHES = 6;
    /// Exams of the latch queue one check visits.
    uint256 public constant MAX_QUEUE_STEPS = 8;
    /// Gas each exam's `fireTrigger` gets.
    uint256 public constant FIRE_GAS = 150_000;
    /// The gas a check keeps for itself after a `fireTrigger`: the book's own records and events.
    uint256 internal constant AFTER_FIRE = 40_000;
    /// The least gas a check starts with: its whole work at the caps, cold, with room (some 1 220 000 measured,
    /// `test_gas_ofACheckAtItsCaps_underASmallBlock`), so that a check that starts never runs short of a fill's gas.
    uint256 public constant POKE_GAS = 1_500_000;

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

    /// The oracle price of `asset` this block, read by a check with an armed entry on it.
    event PriceSeen(uint32 indexed asset, uint64 px);
    /// The oracle gave no price for `asset` this block: its entries wait for the next check.
    event NoPrice(uint32 indexed asset);
    /// No check from block `from` to block `to`, both counted.
    event Gap(uint256 from, uint256 to);
    /// More entries reached `armedAt` than one check arms: `waiting` still wait, the rest are armed next check.
    event ArmingBacklog(uint256 waiting);
    /// More entries on `asset` were reached than one check carries out and latches: the rest wait for the next
    /// check, at its price. An alarm: a touch carried out at a later block's price.
    event TouchBacklog(uint32 indexed asset, bool below);
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
    ///         Returns how many of its entries are still latched; an exam acts only once none is.
    function settle(address exam) external returns (uint256) {
        _poke();
        if (_latched[exam].length != 0) {
            _enter();
            _drainExam(exam, 0, type(uint256).max);
            _locked = false;
        }
        return _latched[exam].length;
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
        _arm();
        uint256 fires = _drainQueue();
        uint256 latches;
        // From the end: an asset whose heaps empty leaves its place to the last one, already checked.
        for (uint256 i = _assets.length; i > 0; --i) {
            uint32 a = _assets[i - 1];
            uint64 px = _oracle(a);
            if (px == 0) {
                emit NoPrice(a);
                continue;
            }
            emit PriceSeen(a, px);
            (fires, latches) = _touch(a, true, px, fires, latches);
            (fires, latches) = _touch(a, false, px, fires, latches);
            _maybeIdle(a);
        }
        _locked = false;
    }

    /// Moves the entries whose `armedAt` has come from waiting to their heap.
    function _arm() internal {
        for (uint256 n = 0; _waiting.length != 0; ++n) {
            uint256 e = _waiting[0];
            if (e >> 193 > block.number) return;
            if (n == MAX_ARMS) {
                emit ArmingBacklog(_waiting.length);
                return;
            }
            address exam = address(uint160(e >> 33));
            uint32 asset = uint32(e >> 1);
            bool isTake = e & 1 == 1;
            _removeAt(_waiting, 0, false, 0, true);
            Slot storage s = _slot[exam][asset][isTake];
            bool below = s.where == WAIT_BELOW;
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
    }

    /// Takes off `asset`'s heap everything `px` has reached: carried out while the check has fires and gas left
    /// and its exam has no earlier touch latched, latched otherwise, left for the next check past `MAX_LATCHES`.
    function _touch(uint32 asset, bool below, uint64 px, uint256 fires, uint256 latches)
        internal
        returns (uint256, uint256)
    {
        uint256[] storage h = below ? _below[asset] : _above[asset];
        while (h.length != 0) {
            uint256 e = h[0];
            uint64 level = uint64(e >> 161);
            if (below ? px > level : px < level) break;
            // An exam with an earlier touch latched has that one carried out first: this one waits behind it.
            bool fire = fires < MAX_FIRES && _latched[address(uint160(e >> 1))].length == 0 && _gasForFire();
            if (!fire && latches == MAX_LATCHES) {
                emit TouchBacklog(asset, below);
                break;
            }
            _removeAt(h, 0, below, asset, false);
            if (fire) ++fires;
            else ++latches;
            _reached(asset, e, px, fire);
        }
        return (fires, latches);
    }

    /// The entry `e`, just taken off `asset`'s heap at `px`: carried out with `fire`, latched without, and latched
    /// too if its call failed.
    function _reached(uint32 asset, uint256 e, uint64 px, bool fire) internal {
        address exam = address(uint160(e >> 1));
        bool isTake = e & 1 == 1;
        if (fire) {
            uint8 r = _call(exam, asset, isTake, px, block.number);
            if (r != FAILED) return _done(exam, asset, isTake, r);
        }
        _latch(exam, asset, isTake, px);
    }

    /// Latches `exam`'s entry, just taken off its heap, with `px` and this block.
    function _latch(address exam, uint32 asset, bool isTake, uint64 px) internal {
        Slot storage s = _slot[exam][asset][isTake];
        uint256[] storage l = _latched[exam];
        if (l.length == 0) _latchQueue.push(exam);
        (s.where, s.pos, s.px, s.touchBlock) = (LATCHED, uint32(l.length), px, uint40(block.number));
        l.push(uint256(asset) << 1 | (isTake ? 1 : 0));
        emit Latched(exam, asset, isTake, px, block.number);
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

    /// Takes `asset` off the list once both its heaps are empty.
    function _maybeIdle(uint32 asset) internal {
        uint256 p = _assetPos[asset];
        if (p == 0 || _below[asset].length != 0 || _above[asset].length != 0) return;
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
