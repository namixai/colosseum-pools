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
///         once and carries out every stop and take that price has reached, at that price and in that block's name.
///         Nobody can take a touched stop back: every exam function that changes anything calls `settle` first
///         (`PaperExam`), which checks the block before the exam does anything else, and holds the exam while it
///         has a touch not yet carried out.
/// @dev On each asset two heaps of armed entries: `below`, entries that fire at a price at or under their level
///      (a long's stop, a short's take), the highest level on top; `above`, those that fire at or over it (a short's
///      stop, a long's take), the lowest on top. An entry set in block N acts from block `armedAt` (N plus the exam's
///      `fillDelay`): until then it waits in a heap by `armedAt`.
///
///      The touch journal. A check's work is capped (`MAX_FIRES`, `MAX_LATCHES`, `MAX_ARMS`, `MAX_SCAN`), so that it
///      fits a small HyperEVM block; what the caps leave is never lost, it is carried out later at the price and block
///      of its own touch. For that each asset keeps, while anything on it is behind, the price and block of every
///      check (`pairAt`): the journal. A side of an asset is either in step (everything the prices of the checked
///      blocks reached is carried out) or behind, with a cursor: no journal pair before the cursor reaches its heap's
///      top. A check takes the top, finds the first pair from the cursor that reaches it (the last pair is this
///      block's price) and carries it out at that pair; the cursor stays there for the next top. A heap is ordered by
///      level, so a pair that does not reach the top reaches nothing under it. An entry joins a heap only while its
///      side is in step: so every entry of a heap acts for every pair from the cursor on. An entry armed while its
///      side is behind, or armed late (more entries due than `MAX_ARMS`), is first compared with every pair from its
///      `armedAt`: touched, it is carried out or latched at its first touch; not touched, it joins the heap if the side
///      is in step, or is parked with its own cursor until it is. A pair stays in the journal while some cursor or some
///      entry due and not armed may need it; then the journal is emptied.
///
///      A touched entry leaves the book only once its exam has answered: a call that fails, whatever the reason,
///      leaves the entry latched at the price and block of its touch, never erased, and the exam's other trigger, once
///      a fill has closed the position, is taken out by the book after the call, with the check's own gas. An exam
///      with a touch not yet carried out (latched, on a side behind with its level within the side's deepest price,
///      parked, or due and not armed) is busy: its other touches are latched, not filled, and its latched ones are
///      carried out the oldest first once it is not busy. While it is busy or latched, `settle` tells it so and the
///      exam refuses to act. A check that would start with less than `pokeGas()` reverts, so a call too poor to do a
///      check's whole work cannot take the block from one that can.
///
///      No way out is built for a latched entry whose exam's call fails at every try: that exam stays held
///      (`StopsPending`). A `PaperExam`'s `fireTrigger` has no loop and calls nothing in the book; its gas is flat.
///      A block nobody checked is shown by the next check (`Gap`).
///
///      Block numbers are kept in 40 bits: room for some 34 000 years of one-second blocks.
contract StopBook {
    /// Entries a check arms, compares with the journal on arming, or takes out of parking.
    uint256 public constant MAX_ARMS = 3;
    /// Entries carried out in one check, latched ones first.
    uint256 public constant MAX_FIRES = 2;
    /// Entries latched in one check off a heap.
    uint256 public constant MAX_LATCHES = 2;
    /// Exams of the latch queue one check visits.
    uint256 public constant MAX_QUEUE_STEPS = 4;
    /// Journal pairs one check reads from storage.
    uint256 public constant MAX_SCAN = 12;
    /// Gas each exam's `fireTrigger` gets.
    uint256 public constant FIRE_GAS = 150_000;
    /// The gas a check keeps for itself after a `fireTrigger`: the book's own records and events.
    uint256 internal constant AFTER_FIRE = 40_000;
    /// The least gas a check starts with: its work at the caps that does not depend on the assets, cold, with room
    /// (`test_gas_ofACheckAtItsCaps_underASmallBlock`)...
    uint256 public constant POKE_GAS_BASE = 800_000;
    /// ...and, for each asset with an entry, its price, its two sides and its journal pair.
    uint256 public constant POKE_GAS_PER_ASSET = 40_000;

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
    uint8 internal constant PARKED_BELOW = 6;
    uint8 internal constant PARKED_ABOVE = 7;

    /// One stop or take: where it is and at which index there, its level and `armedAt`; latched, the price and block
    /// of its touch; parked, its cursor in `touchBlock`. One word.
    struct Slot {
        uint8 where;
        uint32 pos;
        uint64 level;
        uint40 armedAt;
        uint64 px;
        uint40 touchBlock;
    }

    /// A side of an asset behind: from which journal pair on its heap is still to be checked, and the deepest price
    /// of the pairs since it fell behind (its heap's entries that price reaches are touched, the others are not).
    struct Lag {
        bool on;
        uint40 cursor;
        uint64 deep;
    }

    /// What one check has done so far.
    struct Work {
        uint256 fires;
        uint256 latches;
        uint256 arms;
        uint256 scans;
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
    /// Entries waiting for `armedAt`, as `armedAt << 193 | exam << 33 | asset << 1 | isTake`, and how many per asset.
    uint256[] internal _waiting;
    mapping(uint32 asset => uint256) internal _waitCount;
    /// Entries parked while their side is behind, as `exam << 1 | isTake`.
    mapping(uint32 asset => mapping(bool below => uint256[])) internal _parked;
    /// The assets with an entry waiting, armed or parked, and each one's index there plus one.
    uint32[] internal _assets;
    mapping(uint32 asset => uint256) internal _assetPos;
    /// The journal: each asset's checks while it is needed, as `px << 40 | block`, and how many.
    mapping(uint32 asset => mapping(uint256 => uint256)) internal _pairs;
    mapping(uint32 asset => uint256) internal _tail;
    mapping(uint32 asset => mapping(bool below => Lag)) internal _lag;
    /// An exam's latched entries, as `asset << 1 | isTake`, and the exams that had one, in order.
    mapping(address exam => uint256[]) internal _latched;
    address[] internal _latchQueue;
    uint256 internal _latchHead;
    /// Every entry of an exam, as `asset << 1 | isTake`, and each one's index there plus one.
    mapping(address exam => uint256[]) internal _keys;
    mapping(address exam => mapping(uint256 key => uint256)) internal _keyPos;

    /// The oracle price of `asset` this block, read by a check with an entry on it.
    event PriceSeen(uint32 indexed asset, uint64 px);
    /// The oracle gave no price for `asset` this block: nothing on it is checked in this block.
    event NoPrice(uint32 indexed asset);
    /// No check from block `from` to block `to`, both counted.
    event Gap(uint256 from, uint256 to);
    /// More entries reached `armedAt` than one check arms: `waiting` still wait, compared with the journal when armed.
    event ArmingBacklog(uint256 waiting);
    /// One side of `asset` is behind: its heap's top is reached by the journal pair `px`, `touchBlock`, and the check's
    /// caps are spent. The next checks carry it out at that pair. An alarm: fills delayed, never repriced.
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
        _slot[msg.sender][asset][isTake] = Slot(below ? WAIT_BELOW : WAIT_ABOVE, 0, level, uint40(armedAt), 0, 0);
        _push(
            _waiting,
            uint256(armedAt) << 193 | uint256(uint160(msg.sender)) << 33 | uint256(asset) << 1 | (isTake ? 1 : 0),
            false,
            0,
            true
        );
        ++_waitCount[asset];
        if (_assetPos[asset] == 0) {
            _assets.push(asset);
            _assetPos[asset] = _assets.length;
        }
        uint256 key = uint256(asset) << 1 | (isTake ? 1 : 0);
        _keys[msg.sender].push(key);
        _keyPos[msg.sender][key] = _keys[msg.sender].length;
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
    ///         block; a second call in it does nothing. The first needs `pokeGas()` gas left, or reverts.
    function poke() external {
        _poke();
    }

    /// @notice `poke()`, then, if `exam` is not busy, carries out what it has latched, the oldest touch first, as far
    ///         as the gas goes. Returns how many of its entries still wait: latched, or touched and not yet carried
    ///         out (`busy`). An exam acts only once none does.
    function settle(address exam) external returns (uint256) {
        _poke();
        uint256 held = _held(exam, type(uint256).max);
        if (held == 0 && _latched[exam].length != 0) {
            _enter();
            _drainExam(exam, 0, type(uint256).max);
            _locked = false;
        }
        return _latched[exam].length + held;
    }

    // ── views ────────────────────────────────────────────────────

    /// @notice The least gas a check starts with: `POKE_GAS_BASE` plus `POKE_GAS_PER_ASSET` for each asset with an
    ///         entry.
    function pokeGas() public view returns (uint256) {
        return POKE_GAS_BASE + POKE_GAS_PER_ASSET * _assets.length;
    }

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

    /// @notice Where `exam`'s stop or take on `asset` is (0 none, 1 and 2 waiting, 3 below, 4 above, 5 latched, 6 and
    ///         7 parked), its level and `armedAt`; latched, its price and block; parked, its cursor.
    function slotOf(address exam, uint32 asset, bool isTake) external view returns (Slot memory) {
        return _slot[exam][asset][isTake];
    }

    /// @notice Whether a side of `asset` is behind, its cursor and deepest price; how many entries are parked there.
    function lagOf(uint32 asset, bool below) external view returns (Lag memory, uint256 parked) {
        return (_lag[asset][below], _parked[asset][below].length);
    }

    /// @notice How many pairs `asset`'s journal holds, and the `k`-th: price and block.
    function pairAt(uint32 asset, uint256 k) external view returns (uint256 length, uint64 px, uint256 blk) {
        uint256 p = _pairs[asset][k];
        return (_tail[asset], uint64(p >> 40), uint40(p));
    }

    /// @notice The assets with an entry, and how many entries wait for `armedAt`.
    function watched() external view returns (uint32[] memory assets, uint256 waiting) {
        return (_assets, _waiting.length);
    }

    // ── the check ────────────────────────────────────────────────

    function _poke() internal {
        uint256 last = lastPokeBlock;
        if (last == block.number) return;
        // A call that cannot pay for the whole check does not take the block: it reverts, and the next one checks.
        uint256 need = pokeGas();
        if (gasleft() < need) revert PokeGas(need);
        _enter();
        if (last != 0 && block.number > last + 1) emit Gap(last + 1, block.number - 1);
        lastPokeBlock = uint64(block.number);
        uint32[] memory assets = _assets;
        uint64[] memory pxs = new uint64[](assets.length);
        for (uint256 i = 0; i < assets.length; ++i) {
            uint64 px = _oracle(assets[i]);
            pxs[i] = px;
            if (px == 0) emit NoPrice(assets[i]);
            else emit PriceSeen(assets[i], px);
        }
        Work memory w = Work(_drainQueue(), 0, 0, 0);
        // First the entries due, each compared with the journal from its `armedAt` and this block's price; then each
        // side of each asset, from its cursor; then what is left in the journal, kept or emptied.
        _arm(assets, pxs, w);
        bool backlog = _waiting.length != 0 && _waiting[0] >> 193 <= block.number;
        for (uint256 i = assets.length; i > 0; --i) {
            uint32 a = assets[i - 1];
            if (_assetPos[a] == 0) continue;
            _side(a, true, pxs[i - 1], w);
            _side(a, false, pxs[i - 1], w);
            _keep(a, pxs[i - 1], backlog);
            _maybeIdle(a);
        }
        _locked = false;
    }

    /// Moves at most `MAX_ARMS` entries whose `armedAt` has come out of waiting. Each is compared with the journal from
    /// its `armedAt` and this block's price: touched, it is carried out (or latched) at its first touch; not, it joins
    /// its heap if its side is in step, or is parked there with its cursor.
    function _arm(uint32[] memory assets, uint64[] memory pxs, Work memory w) internal {
        while (_waiting.length != 0) {
            uint256 e = _waiting[0];
            if (e >> 193 > block.number) break;
            if (w.arms == MAX_ARMS) {
                emit ArmingBacklog(_waiting.length);
                break;
            }
            ++w.arms;
            _removeAt(_waiting, 0, false, 0, true);
            _armOne(e, assets, pxs, w);
        }
    }

    /// The entry `e`, just out of waiting: compared with the journal from its `armedAt` and placed.
    function _armOne(uint256 e, uint32[] memory assets, uint64[] memory pxs, Work memory w) internal {
        address exam = address(uint160(e >> 33));
        uint32 asset = uint32(e >> 1);
        bool isTake = e & 1 == 1;
        --_waitCount[asset];
        uint64 px;
        for (uint256 i = 0; i < assets.length; ++i) {
            if (assets[i] == asset) px = pxs[i];
        }
        _place(exam, asset, isTake, _firstFrom(asset, e >> 193), px, w);
    }

    /// An entry out of waiting or parking, not compared with the pairs before `from`: carried out or latched at its
    /// first touch from there, or put in its heap, or (its side behind, or the scan cut short) parked with its cursor.
    function _place(address exam, uint32 asset, bool isTake, uint256 from, uint64 px, Work memory w) internal {
        Slot storage s = _slot[exam][asset][isTake];
        bool below = s.where == WAIT_BELOW || s.where == PARKED_BELOW;
        (uint256 k, bool hit, bool done) = _find(asset, below, s.level, from, px, w);
        if (hit) return _touched(exam, asset, isTake, k, px, w);
        if (done && !_lag[asset][below].on) {
            (s.where, s.touchBlock) = (below ? BELOW : ABOVE, 0);
            _push(
                below ? _below[asset] : _above[asset],
                uint256(s.level) << 161 | uint256(uint160(exam)) << 1 | (isTake ? 1 : 0),
                below,
                asset,
                false
            );
        } else {
            uint256[] storage p = _parked[asset][below];
            (s.where, s.pos, s.touchBlock) = (below ? PARKED_BELOW : PARKED_ABOVE, uint32(p.length), uint40(k));
            p.push(uint256(uint160(exam)) << 1 | (isTake ? 1 : 0));
        }
    }

    /// `exam`'s entry, out of waiting or parking, first touched at journal pair `k`: carried out if the check can and
    /// the exam is not busy, latched otherwise.
    function _touched(address exam, uint32 asset, bool isTake, uint256 k, uint64 px, Work memory w) internal {
        (uint64 tpx, uint256 tb) = _pair(asset, k, px);
        bool fire = w.fires < MAX_FIRES && !_busy(exam, asset, isTake) && _gasForFire();
        if (fire) ++w.fires;
        _resolve(exam, asset, isTake, tpx, tb, fire);
    }

    /// One side of `asset`: its heap, from its cursor (in step: from this block's price `px`), each top carried out
    /// at the first journal pair that reaches it, until the top is reached by none or the caps are spent; then, in
    /// step, what is parked there.
    function _side(uint32 asset, bool below, uint64 px, Work memory w) internal {
        Lag storage g = _lag[asset][below];
        bool on = g.on;
        if (on && px != 0 && (below ? px < g.deep : px > g.deep)) g.deep = px;
        uint256 c = on ? g.cursor : _tail[asset];
        uint8 st;
        do {
            (c, st) = _step(asset, below, c, px, w);
        } while (st == 0);
        if (st == 2) return _behind(g, on, c, px);
        if (on) delete _lag[asset][below];
        _unpark(asset, below, px, w);
    }

    /// The top of one heap, from journal pair `c`: carried out at the first pair that reaches it (0); reached by none,
    /// the side in step (1); or the caps spent, the side behind at the returned cursor (2).
    function _step(uint32 asset, bool below, uint256 c, uint64 px, Work memory w) internal returns (uint256, uint8) {
        uint256[] storage h = below ? _below[asset] : _above[asset];
        if (h.length == 0) return (c, 1);
        uint256 e = h[0];
        (uint256 k, bool hit, bool done) = _find(asset, below, uint64(e >> 161), c, px, w);
        if (!done) return (k, 2);
        if (!hit) return (k, 1);
        return (k, _takeTop(asset, below, e, k, px, w));
    }

    /// The top `e` of one heap, reached at journal pair `k`: carried out or latched (0), or left there, the caps spent (2).
    function _takeTop(uint32 asset, bool below, uint256 e, uint256 k, uint64 px, Work memory w)
        internal
        returns (uint8)
    {
        address exam = address(uint160(e >> 1));
        bool isTake = e & 1 == 1;
        bool fire = w.fires < MAX_FIRES && !_busy(exam, asset, isTake) && _gasForFire();
        (uint64 tpx, uint256 tb) = _pair(asset, k, px);
        if (!fire && w.latches == MAX_LATCHES) {
            emit TouchBacklog(asset, below, tpx, tb);
            return 2;
        }
        _removeAt(below ? _below[asset] : _above[asset], 0, below, asset, false);
        if (fire) ++w.fires;
        else ++w.latches;
        _resolve(exam, asset, isTake, tpx, tb, fire);
        return 0;
    }

    /// A side left behind with its cursor at `c`; one that falls behind now starts its deepest price at `px`.
    function _behind(Lag storage g, bool on, uint256 c, uint64 px) internal {
        if (!on) (g.on, g.deep) = (true, px);
        g.cursor = uint40(c);
    }

    /// The entries parked on a side in step, while the check arms: each compared with the journal from its cursor.
    function _unpark(uint32 asset, bool below, uint64 px, Work memory w) internal {
        uint256[] storage p = _parked[asset][below];
        while (p.length != 0 && w.arms < MAX_ARMS) {
            ++w.arms;
            uint256 x = p[p.length - 1];
            address exam = address(uint160(x >> 1));
            bool isTake = x & 1 == 1;
            p.pop();
            _place(exam, asset, isTake, _slot[exam][asset][isTake].touchBlock, px, w);
        }
    }

    /// The first journal pair from `k` on that reaches `level` (the last one is this block's price `px`, unless zero):
    /// its index and `hit`; or, none, the index past the last and `done`; or, the check's `MAX_SCAN` spent, where it
    /// stopped, neither hit nor done.
    function _find(uint32 asset, bool below, uint64 level, uint256 k, uint64 px, Work memory w)
        internal
        view
        returns (uint256, bool hit, bool done)
    {
        uint256 tail = _tail[asset];
        for (; k < tail; ++k) {
            if (w.scans == MAX_SCAN) return (k, false, false);
            ++w.scans;
            uint64 p = uint64(_pairs[asset][k] >> 40);
            if (below ? p <= level : p >= level) return (k, true, true);
        }
        if (px != 0) {
            if (below ? px <= level : px >= level) return (k, true, true);
            ++k;
        }
        return (k, false, true);
    }

    /// The `k`-th journal pair of `asset`: stored, or, past them, this block's price and number.
    function _pair(uint32 asset, uint256 k, uint64 px) internal view returns (uint64, uint256) {
        if (k == _tail[asset]) return (px, block.number);
        uint256 p = _pairs[asset][k];
        return (uint64(p >> 40), uint40(p));
    }

    /// The index of `asset`'s first journal pair of block `b` or later (the stored ones are by block).
    function _firstFrom(uint32 asset, uint256 b) internal view returns (uint256 lo) {
        uint256 hi = _tail[asset];
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            if (uint40(_pairs[asset][mid]) < b) lo = mid + 1;
            else hi = mid;
        }
    }

    /// Keeps this block's pair in `asset`'s journal if a cursor or an entry due may still need it; empties the journal
    /// once nothing does.
    function _keep(uint32 asset, uint64 px, bool backlog) internal {
        bool need = _lag[asset][true].on || _lag[asset][false].on || _parked[asset][true].length != 0
            || _parked[asset][false].length != 0 || (backlog && _waitCount[asset] != 0);
        uint256 tail = _tail[asset];
        if (need) {
            if (px != 0) {
                _pairs[asset][tail] = uint256(px) << 40 | block.number;
                _tail[asset] = tail + 1;
            }
        } else if (tail != 0) {
            _tail[asset] = 0;
        }
    }

    /// Whether `exam` has a touch not yet carried out other than its entry `asset`, `isTake`: latched; on a heap whose
    /// side is behind, at a level the side's deepest price reaches; parked; or due and not armed past a kept check.
    function _busy(address exam, uint32 asset, bool isTake) internal view returns (bool) {
        if (_latched[exam].length != 0) return true;
        return _held(exam, uint256(asset) << 1 | (isTake ? 1 : 0)) != 0;
    }

    /// How many of `exam`'s entries, but the one `skip`, are touched and not carried out nor latched (see `_busy`).
    function _held(address exam, uint256 skip) internal view returns (uint256 n) {
        uint256[] storage keys = _keys[exam];
        for (uint256 i = 0; i < keys.length; ++i) {
            uint256 key = keys[i];
            if (key == skip) continue;
            uint32 a = uint32(key >> 1);
            Slot storage s = _slot[exam][a][key & 1 == 1];
            uint8 where = s.where;
            if (where == PARKED_BELOW || where == PARKED_ABOVE) {
                ++n;
            } else if (where == WAIT_BELOW || where == WAIT_ABOVE) {
                // Due and not armed, with checks since its `armedAt` kept in the journal: one may have touched it.
                if (s.armedAt <= block.number && _firstFrom(a, s.armedAt) < _tail[a]) ++n;
            } else if (where == BELOW || where == ABOVE) {
                Lag storage g = _lag[a][where == BELOW];
                if (g.on && (where == BELOW ? g.deep <= s.level : g.deep >= s.level)) ++n;
            }
        }
    }

    /// `exam`'s entry, out of the book's heaps, touched at `px` in `touchBlock`: carried out with `fire`, latched
    /// without, and latched too if its call failed.
    function _resolve(address exam, uint32 asset, bool isTake, uint64 px, uint256 touchBlock, bool fire) internal {
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
    /// failed, or that is busy with a touch not yet latched, goes to the back of the queue: it does not hold up the
    /// exams behind it, and is visited again by the next check, once a check at most.
    function _drainQueue() internal returns (uint256 fires) {
        uint256 end = _latchQueue.length;
        for (uint256 n = 0; n < MAX_QUEUE_STEPS && _latchHead < end; ++n) {
            address exam = _latchQueue[_latchHead];
            bool failed = _latched[exam].length != 0 && _held(exam, type(uint256).max) != 0;
            if (!failed) (fires, failed) = _drainExam(exam, fires, MAX_FIRES);
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
        try IStopExam(exam).fireTrigger{gas: FIRE_GAS}(
            asset, isTake, s.level, s.armedAt, px, uint64(touchBlock)
        ) returns (
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
        _unkey(exam, uint256(asset) << 1 | (isTake ? 1 : 0));
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
        } else if (s.where == PARKED_BELOW || s.where == PARKED_ABOVE) {
            uint256[] storage p = _parked[asset][s.where == PARKED_BELOW];
            uint256 x = p[p.length - 1];
            p[s.pos] = x;
            _slot[address(uint160(x >> 1))][asset][x & 1 == 1].pos = s.pos;
            p.pop();
        } else {
            _removeAt(_waiting, s.pos, false, 0, true);
            --_waitCount[asset];
        }
        delete _slot[exam][asset][isTake];
        _unkey(exam, uint256(asset) << 1 | (isTake ? 1 : 0));
        _maybeIdle(asset);
    }

    /// Takes `key` off `exam`'s list of entries, the last one taking its place.
    function _unkey(address exam, uint256 key) internal {
        uint256 p = _keyPos[exam][key];
        if (p == 0) return;
        uint256[] storage keys = _keys[exam];
        uint256 last = keys[keys.length - 1];
        keys[p - 1] = last;
        _keyPos[exam][last] = p;
        keys.pop();
        delete _keyPos[exam][key];
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

    /// Takes `asset` off the list once nothing waits, is armed or parked on it, and with it its journal and lags.
    function _maybeIdle(uint32 asset) internal {
        uint256 p = _assetPos[asset];
        if (
            p == 0 || _below[asset].length != 0 || _above[asset].length != 0 || _waitCount[asset] != 0
                || _parked[asset][true].length != 0 || _parked[asset][false].length != 0
        ) return;
        delete _lag[asset][true];
        delete _lag[asset][false];
        _tail[asset] = 0;
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
