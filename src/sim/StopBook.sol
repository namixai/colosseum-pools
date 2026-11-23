// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";
import {CoreOps} from "../lib/CoreOps.sol";

/// What the book asks of an exam: carry out its stop or take touched at oracle price `px` in block `touchBlock`, at
/// time `touchTime`, with that block's best bid and offer `book` (`StopBook.NO_BOOK`, or `askOff << 16 | bidOff`:
/// how far each stood off `px` on the trader's worse side, in millionths of it). It answers `FILLED`, `CLOSED` (filled, and the position is closed, so its other trigger is gone
/// too) or `ENDED` (the exam had ended, or was out of time at `touchTime`: nothing filled, the trigger removed), and
/// calls nothing in the book while it does.
interface IStopExam {
    function fireTrigger(
        uint32 asset,
        bool isTake,
        uint64 level,
        uint64 armedAt,
        uint64 px,
        uint32 book,
        uint64 touchBlock,
        uint64 touchTime
    ) external returns (uint8);
}

/// The factory's register of its pools and their exams, and whether its list leaves room for the book to log an
/// asset (`SimPoolFactory.watchable`).
interface IPoolRegister {
    function poolOf(address account) external view returns (address);
    function watchable(uint32 asset) external view returns (bool);
}

/// @title StopBook
/// @notice Every stop and take of the exams of one simulated-pool factory, and the journal of oracle prices they are
///         carried out by. Anyone may call `poke()`, once a block: it logs this block's oracle price of every asset
///         with a stop or a take on it, and carries nothing out. Anyone may call `settle(exam)`: it carries out each
///         of that exam's stops and takes at the first logged price, from its `armedAt`, that reached it, at that
///         price, in that block's name and at that block's time. Every exam function that changes anything calls
///         `settle` first (`PaperExam`), so no exam acts on a state its touches have not reached: a stop the oracle
///         price has touched is carried out before the trader can take it back, move it or trade. Nor does taking
///         back or moving a stop act sooner than setting one: a change waits the exam's delay as an order does, and
///         until then the stop there is carried out at its touches (`add`).
/// @dev The journal. A check of a block logs one position: its block and time (`at`), and each watched asset's oracle
///      price there, zero when the oracle gave none. On each asset the prices sit in a tree of four branches: level 0
///      the prices, four to a word, each with the block's best bid and offer as offsets from it; level k, for each run
///      of 4^k positions, its lowest and its highest price (32 bits each: a price is under `MAX_PRICE`), four runs to a
///      word, written once the run is whole. The first position from `s` whose price reached a level is found by climbing from `s`, a word a level,
///      until a run reaches it and going down that run to the price itself: some twenty words read for a stop a month
///      old, whatever the number of stops and exams. Each asset's positions are shifted by a number of its own (`_off`)
///      so that the assets' runs do not all end in the same check.
///
///      A check's cost is its assets' prices and the runs they close; nothing in it depends on how many stops there
///      are, nor on any exam. A settle reads the exam's own entries and the journal: no other exam can make it later or
///      dearer. Within an exam the touches are carried out in the order of their positions, a stop before a take at the
///      same one; a fill that closes a position takes the asset's other trigger with it. A settle that runs short of
///      gas stops between two steps and keeps what it found (`Entry.cursor`): the next one goes on from there. A fill
///      that fails leaves its entry as it was; a `PaperExam`'s fill has nothing that reverts but the gas, and it gets
///      all the gas the call has but what the book keeps, so a settle sent with enough gas carries it out.
///
///      A block nobody checked is shown by the next check (`Gap`); its prices are not in the journal.
///
///      Block numbers and times are kept in 40 bits: some 34 000 years of one-second blocks.
contract StopBook {
    /// What an exam's `fireTrigger` answered (`Fired.result`): its call failed (the entry stays), it filled, it filled
    /// and closed the position, or the exam had ended.
    uint8 public constant FAILED = 0;
    uint8 public constant FILLED = 1;
    uint8 public constant CLOSED = 2;
    uint8 public constant ENDED = 3;
    /// `Fired.result` of a fill that ran out of the gas it was given (the caller sent too little): the entry stays.
    uint8 public constant STARVED = 4;

    /// The gas the oracle precompile gets for one price, and the book precompile for one best bid and offer; a check
    /// with less left reverts (`PokeGas`).
    uint256 public constant ORACLE_GAS = 50_000;
    uint256 public constant BBO_GAS = 50_000;
    /// The highest price and level the journal takes: a price at it or over is logged as none, a level refused.
    uint256 public constant MAX_PRICE = type(uint32).max - 1;
    /// A block whose book did not answer, was empty on a side or crossed (`IStopExam.fireTrigger`'s `book`).
    uint32 public constant NO_BOOK = type(uint32).max;
    /// How far a side of the book is logged off the oracle price at most, in millionths of it: 6.5 %, past the bound of
    /// any fill (`PaperExam.MAX_WORSE_BPS`, 5 %).
    uint256 internal constant MAX_OFF = 65_534;
    /// The least gas a search of the journal starts with, and a fill (which gets all the call has but what the book
    /// keeps for itself). A settle with less stops there and keeps what it found.
    uint256 public constant SEARCH_GAS = 120_000;
    uint256 public constant FIRE_GAS = 150_000;
    /// What the book keeps for itself after a step: its own records, and keeping what it found of each entry.
    uint256 internal constant KEEP_BASE = 40_000;
    uint256 internal constant KEEP_PER_ENTRY = 4_000;
    /// The span of the assets' shifts (`_off`): 4^12 positions.
    uint256 internal constant OFF_MASK = (1 << 24) - 1;

    /// One stop or take: its level, the side it fires on, from which block, from which position it is searched
    /// (`from`, the first logged after it was set), and up to which a settle that stopped short found nothing
    /// (`cursor`; or, touched there and not yet carried out, its touch); whether its key is on the exam's list,
    /// which it stays on once there, and where; and the change asked of it (`add`): from block `nextAt` its level is
    /// `nextLevel`, zero: none from then; zero `nextAt`: no change asked. One word; zero `level`: none. A level is
    /// under `MAX_PRICE`, so 32 bits hold it.
    struct Entry {
        uint32 level;
        uint40 armedAt;
        uint40 from;
        uint40 cursor;
        bool below;
        bool listed;
        uint8 at;
        uint32 nextLevel;
        uint40 nextAt;
    }

    /// A touch not yet carried out (`touches`): the entry, and the price, block, time and position of its touch.
    struct Touch {
        uint32 asset;
        bool isTake;
        uint64 level;
        uint64 px;
        uint32 book;
        uint256 blk;
        uint256 time;
        uint256 pos;
    }

    /// The factory whose exams alone may add and remove entries.
    address public immutable factory;

    /// The last block checked, how many positions the journal holds, and the lock of a settle: one word.
    uint64 public lastPokeBlock;
    uint64 internal _len;
    bool internal _locked;

    mapping(address exam => mapping(uint32 asset => mapping(bool isTake => Entry))) internal _entry;
    /// The keys `asset << 1 | isTake` an exam has ever had an entry on: at most two for each of its assets, so at most
    /// 32 (`PaperExam`); an entry gone leaves its key here.
    mapping(address exam => uint64[]) internal _keys;
    /// For each exam, the positions under which every one of its entries has been searched.
    mapping(address exam => uint256) internal _checked;
    /// The assets with an entry, each one's index there plus one, and how many entries it has.
    uint32[] internal _assets;
    mapping(uint32 asset => uint256) internal _assetPos;
    mapping(uint32 asset => uint256) internal _count;
    /// Each position's block and time, `block << 40 | time`, three to a word.
    mapping(uint256 word => uint256) internal _at;
    /// The trees, by `asset << 224 | level << 200 | word`: level 0 four positions a word, each `book << 32 | price`
    /// (zero: no price); the others four runs a word, each `highest << 32 | lowest` of its prices (zero: none).
    mapping(uint256 key => uint256) internal _tree;

    /// The oracle price of `asset` this block, logged by a check.
    event PriceSeen(uint32 indexed asset, uint64 px);
    /// The oracle gave no price for `asset` this block: nothing on it is touched in this block.
    event NoPrice(uint32 indexed asset);
    /// No check from block `from` to block `to`, both counted.
    event Gap(uint256 from, uint256 to);
    /// `result`: `FILLED`, `CLOSED`, `ENDED`, or `FAILED` (the call failed: the entry stays, to be tried again).
    event Fired(address indexed exam, uint32 indexed asset, bool isTake, uint64 px, uint256 touchBlock, uint8 result);

    error NotExam();
    error Reentered();
    /// A level the journal cannot hold (`MAX_PRICE`).
    error LevelTooHigh(uint64 level);
    /// A first entry on `asset`, which the factory has delisted, while the assets logged are at the factory's most
    /// (`SimPoolFactory.MAX_LISTED`, `watchable`): as a market takes no new orders on an instrument off its list.
    error NoPlaceForAsset(uint32 asset);
    /// A check needs at least `needed` gas left for each asset's price.
    error PokeGas(uint256 needed);

    constructor() {
        factory = msg.sender;
    }

    // ── exams ────────────────────────────────────────────────────

    /// @notice Sets the caller's stop or take on `asset`. On a side with none: one that fires at a price at or under
    ///         `level` (`below`) or at or over it, from block `armedAt`. On a side with one, a change, as an exchange
    ///         takes a cancel or a new price no sooner than an order: the one there stays, and is carried out at its
    ///         touches, up to block `armedAt`, and from that block `level` takes its place (zero: none). A change asked
    ///         before and not yet in force is replaced. Only an exam of the factory, and not from inside a settle; an
    ///         exam calls it settled, so no change of its is due (`_due`). A first entry on an asset the book does not
    ///         log yet and the factory has delisted waits for room under the factory's most (`NoPlaceForAsset`).
    function add(uint32 asset, bool isTake, bool below, uint64 level, uint64 armedAt) external {
        _onlyExam();
        if (level > MAX_PRICE) revert LevelTooHigh(level);
        Entry storage cur = _entry[msg.sender][asset][isTake];
        if (cur.level != 0) {
            (cur.nextLevel, cur.nextAt) = (uint32(level), uint40(armedAt));
            return;
        }
        Entry memory old = cur;
        if (!old.listed) {
            old.at = uint8(_keys[msg.sender].length);
            _keys[msg.sender].push(uint64(uint256(asset) << 1 | (isTake ? 1 : 0)));
        }
        if (old.level == 0 && _count[asset]++ == 0) {
            // A new asset to log: one off the factory's list only while the list leaves room, so that a check always
            // fits a small block.
            if (!IPoolRegister(factory).watchable(asset)) revert NoPlaceForAsset(asset);
            _assets.push(asset);
            _assetPos[asset] = _assets.length;
        }
        _entry[msg.sender][asset][isTake] =
            Entry(uint32(level), uint40(armedAt), uint40(_len), 0, below, true, old.at, 0, 0);
    }

    /// @notice Removes the caller's stop or take on `asset` at once, with any change asked of it, if it has one here:
    ///         for a position that is gone. Only an exam of the factory, and not from inside a settle.
    function remove(uint32 asset, bool isTake) external {
        _onlyExam();
        if (_entry[msg.sender][asset][isTake].level != 0) _drop(msg.sender, asset, isTake);
    }

    // ── anyone ───────────────────────────────────────────────────

    /// @notice Checks this block: logs the oracle price of every asset with an entry. Once a block; a second call in
    ///         it does nothing. It logs every one of them or reverts.
    function poke() external {
        _poke();
    }

    /// @notice `poke()`, then carries out every stop and take of `exam` that a logged price has touched, in the order
    ///         of their touches, each at its touch. Never reverts for want of gas between its steps: it stops, keeps
    ///         what it found, and returns how many of the exam's entries are still to be searched or carried out. An
    ///         exam acts only once none is.
    function settle(address exam) external returns (uint256) {
        _poke();
        if (_locked) revert Reentered();
        _locked = true;
        uint256 left = _settle(exam);
        _locked = false;
        return left;
    }

    // ── views ────────────────────────────────────────────────────

    /// @notice `exam`'s stop or take on `asset`: its level, `armedAt`, the side it fires on, where its search starts.
    function entryOf(address exam, uint32 asset, bool isTake) external view returns (Entry memory) {
        return _entry[exam][asset][isTake];
    }

    /// @notice How many entries `exam` has.
    function entries(address exam) external view returns (uint256 n) {
        uint64[] memory keys = _keys[exam];
        for (uint256 i = 0; i < keys.length; ++i) {
            (uint32 a, bool tk) = _split(keys[i]);
            if (_entry[exam][a][tk].level != 0) ++n;
        }
    }

    /// @notice The positions under which every entry of `exam` has been searched.
    function checkedTo(address exam) external view returns (uint256) {
        return _checked[exam];
    }

    /// @notice How many positions the journal holds.
    function logged() external view returns (uint256) {
        return _len;
    }

    /// @notice The block and time of position `pos`.
    function at(uint256 pos) external view returns (uint256 blk, uint256 time) {
        return _atPos(pos);
    }

    /// @notice The oracle price of `asset` logged at position `pos`; zero if none was.
    function priceAt(uint32 asset, uint256 pos) external view returns (uint64) {
        return uint32(_raw(asset, pos + _off(asset)));
    }

    /// @notice The best bid and offer of `asset` logged at position `pos` (`IStopExam.fireTrigger`'s `book`).
    function bookAt(uint32 asset, uint256 pos) external view returns (uint32) {
        return uint32(_raw(asset, pos + _off(asset)) >> 32);
    }

    /// @notice The assets with an entry: the ones a check logs.
    function watched() external view returns (uint32[] memory) {
        return _assets;
    }

    /// @notice The touches of `exam` that the journal holds and no settle has carried out yet, in the order a settle
    ///         carries them out: what its state will be once settled. A fill that closes a position takes the asset's
    ///         other entry with it, so a later touch of that one is not carried out.
    function touches(address exam) external view returns (Touch[] memory list) {
        uint256 len = _len;
        uint64[] memory keys = _keys[exam];
        uint256 n = keys.length;
        uint256[] memory found = new uint256[](n);
        uint256 m;
        for (uint256 i = 0; i < n; ++i) {
            found[i] = _touchOf(exam, keys[i], len);
            if (found[i] < len) ++m;
        }
        list = new Touch[](m);
        for (uint256 j = 0; j < m; ++j) {
            uint256 i = _earliest(keys, found, len);
            list[j] = _touch(exam, keys[i], found[i]);
            found[i] = len;
        }
    }

    /// The first touch of `exam`'s entry `key` from where its search starts; `len` if none, or if it has no entry.
    function _touchOf(address exam, uint64 key, uint256 len) internal view returns (uint256) {
        (uint32 a, bool tk) = _split(key);
        Entry memory e = _entry[exam][a][tk];
        return e.level == 0 ? len : _findOne(a, e, _start(e, _checked[exam], len), len);
    }

    function _touch(address exam, uint64 key, uint256 pos) internal view returns (Touch memory t) {
        (t.asset, t.isTake) = _split(key);
        uint64 x = _raw(t.asset, pos + _off(t.asset));
        (t.px, t.book) = (uint32(x), uint32(x >> 32));
        (t.blk, t.time) = _atPos(pos);
        (t.level,) = _phase(_entry[exam][t.asset][t.isTake], t.blk);
        t.pos = pos;
    }

    // ── the check ────────────────────────────────────────────────

    function _poke() internal {
        uint256 last = lastPokeBlock;
        if (last == block.number) return;
        if (last != 0 && block.number > last + 1) emit Gap(last + 1, block.number - 1);
        lastPokeBlock = uint64(block.number);
        uint32[] memory assets = _assets;
        if (assets.length == 0) return;
        uint256 g = _len;
        _len = uint64(g + 1);
        _at[g / 3] |= (block.number << 40 | block.timestamp) << (g % 3 * 80);
        for (uint256 i = 0; i < assets.length; ++i) {
            uint32 a = assets[i];
            uint64 px = _oracle(a);
            if (px == 0) {
                emit NoPrice(a);
                _log(a, g + _off(a), 0);
            } else {
                emit PriceSeen(a, px);
                _log(a, g + _off(a), uint256(_bbo(a, px)) << 32 | px);
            }
        }
    }

    /// Writes the price and book `x` (`book << 32 | price`, zero: no price) at position `q` of `asset`'s tree, and the
    /// run of each level that it closes: each from the word of its four children, just written.
    function _log(uint32 asset, uint256 q, uint256 x) internal {
        uint256 key = _key(asset, 0, q >> 2);
        uint256 w = _tree[key];
        if (x != 0) {
            w |= x << (q % 4 * 64);
            _tree[key] = w;
        }
        uint256 n = q + 1;
        for (uint256 k = 1; n % (1 << (2 * k)) == 0; ++k) {
            uint256 j = (n >> (2 * k)) - 1;
            uint256 run = k == 1 ? _ofPrices(w) : _ofRuns(w);
            key = _key(asset, k, j >> 2);
            w = _tree[key];
            if (run != 0) {
                w |= run << (j % 4 * 64);
                _tree[key] = w;
            }
        }
    }

    /// The run of four prices (zero: none): the highest over the lowest.
    function _ofPrices(uint256 w) internal pure returns (uint256) {
        uint256 lo = type(uint256).max;
        uint256 hi;
        for (uint256 e = 0; e < 4; ++e) {
            uint256 x = uint32(w >> (e * 64));
            if (x == 0) continue;
            if (x < lo) lo = x;
            if (x > hi) hi = x;
        }
        return hi == 0 ? 0 : hi << 32 | lo;
    }

    /// The run of four runs: the highest of their highest prices over the lowest of their lowest, runs with no price
    /// left out.
    function _ofRuns(uint256 w) internal pure returns (uint256) {
        uint256 lo = type(uint256).max;
        uint256 hi;
        for (uint256 e = 0; e < 4; ++e) {
            uint256 r = uint64(w >> (e * 64));
            if (r == 0) continue;
            if (uint32(r) < lo) lo = uint32(r);
            if (r >> 32 > hi) hi = r >> 32;
        }
        return hi == 0 ? 0 : hi << 32 | lo;
    }

    // ── a settle ─────────────────────────────────────────────────

    /// Searches every entry of `exam` up to the journal's end, then carries out what was touched, the earliest first.
    /// Out of gas between two steps, it keeps what it found and returns how many entries are still owed.
    function _settle(address exam) internal returns (uint256) {
        uint256 len = _len;
        uint256 done = _checked[exam];
        if (done >= len) return 0;
        uint64[] memory keys = _keys[exam];
        uint256 n = keys.length;
        if (n == 0) return 0;
        uint256[] memory found = new uint256[](n);
        for (uint256 i = 0; i < n; ++i) {
            found[i] = UNSEARCHED;
        }
        bool changes;
        for (uint256 i = 0; i < n; ++i) {
            if (found[i] != UNSEARCHED) continue;
            // An entry with a change asked is two searches (`_findOne`); one searched with another has none.
            bool changing = _changing(exam, keys[i]);
            if (changing) changes = true;
            if (gasleft() < (changing ? 2 * SEARCH_GAS : SEARCH_GAS) + _keep(n)) return _stop(exam, keys, found, len);
            _search(exam, keys, found, i, done, len);
        }
        for (uint256 i = _earliest(keys, found, len); i < n; i = _earliest(keys, found, len)) {
            if (!_carry(exam, keys, found, i, len)) return _stop(exam, keys, found, len);
        }
        // Every deferred entry has met its fill above; one left would keep what it found.
        for (uint256 i = 0; i < n; ++i) {
            if (found[i] & DEFERRED != 0) return _stop(exam, keys, found, len);
        }
        _checked[exam] = len;
        if (changes) _due(exam, keys);
        return 0;
    }

    /// Once every entry of `exam` is searched up to this block, each change in force by it (`add`) takes its entry's
    /// place, or, none, takes the entry out; its exam does the same (`PaperExam._due`).
    function _due(address exam, uint64[] memory keys) internal {
        for (uint256 i = 0; i < keys.length; ++i) {
            (uint32 a, bool tk) = _split(keys[i]);
            Entry storage e = _entry[exam][a][tk];
            uint256 from = e.nextAt;
            if (from == 0 || from > block.number) continue;
            if (e.nextLevel == 0) _drop(exam, a, tk);
            else (e.level, e.armedAt, e.nextLevel, e.nextAt) = (e.nextLevel, uint40(from), 0, 0);
        }
    }

    /// What `found` holds of an entry: under `len` its touch; `len` none up to there; `UNSEARCHED`; or, `DEFERRED`
    /// with a position, none before it, searched from there only if the fill of its asset's other entry, touched
    /// first, leaves the position open.
    uint256 internal constant DEFERRED = 1 << 255;
    uint256 internal constant UNSEARCHED = DEFERRED - 1;

    /// Searches entry `i` of `exam`, and with it the asset's other entry: from the same position, the first position
    /// where either is touched, the other, if not touched there, deferred past it; from different ones, the one from
    /// earlier first, and the other deferred to its start if the first is touched before it. An entry with a change
    /// asked is searched on its own: at its level up to the change, at the change's from there (`_findOne`).
    function _search(address exam, uint64[] memory keys, uint256[] memory found, uint256 i, uint256 done, uint256 len)
        internal
        view
    {
        (uint32 a, bool tk) = _split(keys[i]);
        Entry memory e = _entry[exam][a][tk];
        if (e.level == 0) {
            found[i] = len;
            return;
        }
        uint256 s = _start(e, done, len);
        Entry memory o = _entry[exam][a][!tk];
        if (o.level == 0 || found[o.at] != UNSEARCHED || e.nextAt != 0 || o.nextAt != 0) {
            found[i] = _findOne(a, e, s, len);
            return;
        }
        _pair(a, found, i, e, s, o, _start(o, done, len), len);
    }

    /// Whether `exam`'s entry `key` has a change asked of it.
    function _changing(address exam, uint64 key) internal view returns (bool) {
        (uint32 a, bool tk) = _split(key);
        return _entry[exam][a][tk].nextAt != 0;
    }

    /// Entry `i` (`e`, from `s`) and the asset's other entry `o` (from `so`), neither with a change asked, searched
    /// together (`_search`).
    function _pair(
        uint32 a,
        uint256[] memory found,
        uint256 i,
        Entry memory e,
        uint256 s,
        Entry memory o,
        uint256 so,
        uint256 len
    ) internal view {
        if (so != s) {
            // The one searched from earlier first: touched before the other's start, the other waits for its fill.
            if (s < so) _staggered(a, found, i, e, s, o, so, len);
            else _staggered(a, found, o.at, o, so, e, s, len);
            return;
        }
        (uint64 lo, uint256 hi) = e.below ? (uint64(e.level), uint256(o.level)) : (uint64(o.level), uint256(e.level));
        uint256 t = _find(a, s, len, lo, hi);
        if (t == len) {
            (found[i], found[o.at]) = (len, len);
            return;
        }
        uint256 x = uint32(_raw(a, t + _off(a)));
        found[i] = _hits(e, x) ? t : DEFERRED | (t + 1);
        found[o.at] = _hits(o, x) ? t : DEFERRED | (t + 1);
    }

    /// Entry `i1` (`e1`, from `s1`) searched; the asset's other entry `e2`, from the later `s2`, deferred to it if `e1`
    /// is touched before it, searched otherwise.
    function _staggered(
        uint32 a,
        uint256[] memory found,
        uint256 i1,
        Entry memory e1,
        uint256 s1,
        Entry memory e2,
        uint256 s2,
        uint256 len
    ) internal view {
        uint256 t = _findOne(a, e1, s1, len);
        found[i1] = t;
        found[e2.at] = t < s2 ? DEFERRED | s2 : _findOne(a, e2, s2, len);
    }

    /// The first position from `s`, under `len`, at which the price of `a` touched `e`; `len` if none. With a change
    /// asked: at its level before the first position of a block at or after `nextAt`, and from there at `nextLevel`
    /// (none, for zero).
    function _findOne(uint32 a, Entry memory e, uint256 s, uint256 len) internal view returns (uint256) {
        uint256 end = e.nextAt == 0 ? len : _armed(s, e.nextAt, len);
        uint256 t = _find(a, s, end, e.below ? e.level : 0, e.below ? NO_LEVEL : e.level);
        if (t < end || e.nextLevel == 0) return t < end ? t : len;
        return _find(a, end, len, e.below ? e.nextLevel : 0, e.below ? NO_LEVEL : e.nextLevel);
    }

    /// The level and the `armedAt` of `e` in block `blk`: its change's from `nextAt` on, its own before.
    function _phase(Entry memory e, uint256 blk) internal pure returns (uint64, uint64) {
        if (e.nextAt != 0 && blk >= e.nextAt) return (e.nextLevel, e.nextAt);
        return (e.level, e.armedAt);
    }

    function _hits(Entry memory e, uint256 x) internal pure returns (bool) {
        return e.below ? x != 0 && x <= e.level : x >= e.level;
    }

    /// Where `e`'s search starts: where its last one stopped, or the exam's, at the earliest its first position, and
    /// not before its first position of a block at or after its `armedAt`.
    function _start(Entry memory e, uint256 done, uint256 len) internal view returns (uint256 s) {
        s = e.from;
        if (e.cursor > s) s = e.cursor;
        if (done > s) s = done;
        return _armed(s, e.armedAt, len);
    }

    /// Carries out the touch of entry `i`, the earliest, if its entry is still there; then the asset's other entry
    /// goes with a fill that closed the position, or, deferred, is searched: false if the gas is short or the exam's
    /// call failed, the entries left as they were.
    function _carry(address exam, uint64[] memory keys, uint256[] memory found, uint256 i, uint256 len)
        internal
        returns (bool)
    {
        (uint32 a, bool tk) = _split(keys[i]);
        uint256 keep = _keep(keys.length);
        uint8 r;
        if (_entry[exam][a][tk].level != 0) {
            if (gasleft() < FIRE_GAS + keep) return false;
            r = _fire(exam, keys[i], found[i], keep);
            if (r == FAILED) return false;
            _drop(exam, a, tk);
        }
        found[i] = len;
        Entry memory o = _entry[exam][a][!tk];
        if (o.level == 0) return true;
        if (r == CLOSED) {
            _drop(exam, a, !tk);
            found[o.at] = len;
        } else if (found[o.at] & DEFERRED != 0) {
            if (gasleft() < SEARCH_GAS + keep) return false;
            found[o.at] = _findOne(a, o, found[o.at] ^ DEFERRED, len);
        }
        return true;
    }

    /// The entry with the earliest touch in `found`: by position, a stop before a take, then by its place in `keys`.
    function _earliest(uint64[] memory keys, uint256[] memory found, uint256 len) internal pure returns (uint256 best) {
        best = keys.length;
        for (uint256 i = 0; i < keys.length; ++i) {
            if (found[i] >= len) continue;
            if (best == keys.length || found[i] < found[best]) best = i;
            else if (found[i] == found[best] && keys[i] & 1 < keys[best] & 1) best = i;
        }
    }

    /// A settle out of gas: each entry left keeps where its search got (its touch, or the journal's end), and the
    /// count of those still to be searched or carried out is returned.
    function _stop(address exam, uint64[] memory keys, uint256[] memory found, uint256 len)
        internal
        returns (uint256 left)
    {
        for (uint256 i = 0; i < keys.length; ++i) {
            (uint32 a, bool tk) = _split(keys[i]);
            Entry storage e = _entry[exam][a][tk];
            if (e.level == 0) continue;
            uint256 f = found[i];
            if (f == UNSEARCHED) {
                ++left;
                continue;
            }
            if (f != len) ++left;
            if (f & DEFERRED != 0) f ^= DEFERRED;
            if (f > e.cursor) e.cursor = uint40(f);
        }
    }

    /// `exam`'s entry `key` carried out at its touch at position `pos`: what the exam answered, or `FAILED` if its
    /// call failed. The call gets all the gas there is but `keep`.
    function _fire(address exam, uint64 key, uint256 pos, uint256 keep) internal returns (uint8 r) {
        Touch memory t = _touch(exam, key, pos);
        (, uint64 armedAt) = _phase(_entry[exam][t.asset][t.isTake], t.blk);
        try IStopExam(exam).fireTrigger{gas: gasleft() - keep}(
            t.asset,
            t.isTake,
            t.level,
            armedAt,
            t.px,
            t.book,
            uint64(t.blk),
            uint64(t.time)
        ) returns (uint8 x) {
            if (x == FILLED || x == CLOSED || x == ENDED) r = x;
        } catch {
            // Its gas all spent: short of what the caller sent, the entry as it was; else the fill itself failed.
            if (gasleft() <= keep) r = STARVED;
        }
        if (r == STARVED) {
            emit Fired(exam, t.asset, t.isTake, t.px, t.blk, r);
            return FAILED;
        }
        emit Fired(exam, t.asset, t.isTake, t.px, t.blk, r);
    }

    function _keep(uint256 n) internal pure returns (uint256) {
        return KEEP_BASE + KEEP_PER_ENTRY * n;
    }

    // ── the journal ──────────────────────────────────────────────

    /// The first position from `s` whose block is `armedAt` or later (a position's block is at least the one before's
    /// plus one, so it is at most `armedAt` less the block at `s` positions on).
    function _armed(uint256 s, uint256 armedAt, uint256 len) internal view returns (uint256) {
        if (s >= len) return s;
        (uint256 b,) = _atPos(s);
        if (b >= armedAt) return s;
        uint256 hi = s + (armedAt - b);
        if (hi > len) hi = len;
        uint256 lo = s + 1;
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            (uint256 bm,) = _atPos(mid);
            if (bm < armedAt) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }

    /// A search: the asset, where its tree's logged positions end, the level a price at or under reaches (zero: none)
    /// and the one a price at or over reaches (`NO_LEVEL`: none).
    struct Search {
        uint32 asset;
        uint256 qn;
        uint256 lo;
        uint256 hi;
        uint256 loCode;
        uint256 hiCode;
    }

    uint256 internal constant NONE = type(uint256).max;
    uint256 internal constant NO_LEVEL = type(uint256).max;

    /// The first position from `s`, under `len`, whose price of `asset` is at or under `lo` or at or over `hi`; `len`
    /// if none. Climbs from `s`, a word of four runs a level, until a run whose prices reach a level, or the run still
    /// being logged, and goes down it.
    function _find(uint32 asset, uint256 s, uint256 len, uint256 lo, uint256 hi) internal view returns (uint256) {
        if (s >= len) return len;
        uint256 off = _off(asset);
        uint256 q = _climb(
            Search(asset, len + off, lo, hi, lo, hi),
            s + off
        );
        return q == NONE ? len : q - off;
    }

    /// `_find` in the tree, from tree position `p`: the tree position found, or `NONE`.
    function _climb(Search memory f, uint256 p) internal view returns (uint256) {
        unchecked {
            for (uint256 k = 0;; ++k) {
                uint256 sh = 2 * k;
                uint256 w = _tree[uint256(f.asset) << 224 | k << 200 | p >> 2];
                for (uint256 end = p | 3; p <= end; ++p) {
                    if (p << sh >= f.qn) return NONE;
                    uint256 x = uint64(w >> ((p & 3) << 6));
                    if (k == 0) {
                        x = uint32(x);
                        if (x != 0 && x <= f.lo || x >= f.hi) return p;
                        continue;
                    }
                    bool open = (p + 1) << sh > f.qn;
                    if (open || x != 0 && uint32(x) <= f.loCode || x >> 32 >= f.hiCode) {
                        uint256 q = _down(f, k, p);
                        if (q != NONE || open) return q; // past the run still being logged, nothing
                    }
                }
                p >>= 2;
            }
        }
    }

    /// The first position in run `p` of level `k` (above 0) whose price reaches the level, `NONE` if none: its four
    /// children, one word, each gone down that its prices reach or that is still being logged.
    function _down(Search memory f, uint256 k, uint256 p) internal view returns (uint256) {
        unchecked {
            --k;
            uint256 sh = 2 * k;
            uint256 w = _tree[uint256(f.asset) << 224 | k << 200 | p];
            uint256 c = p << 2;
            for (uint256 e = 0; e < 4; ++e) {
                if (c << sh >= f.qn) return NONE;
                uint256 x = uint64(w >> (e << 6));
                if (k == 0) {
                    x = uint32(x);
                    if (x != 0 && x <= f.lo || x >= f.hi) return c;
                } else {
                    bool open = (c + 1) << sh > f.qn;
                    if (open || x != 0 && uint32(x) <= f.loCode || x >> 32 >= f.hiCode) {
                        uint256 q = _down(f, k, c);
                        if (q != NONE || open) return q;
                    }
                }
                ++c;
            }
            return NONE;
        }
    }

    function _raw(uint32 asset, uint256 q) internal view returns (uint64) {
        return uint64(_tree[_key(asset, 0, q >> 2)] >> (q % 4 * 64));
    }

    function _key(uint32 asset, uint256 k, uint256 word) internal pure returns (uint256) {
        return uint256(asset) << 224 | k << 200 | word;
    }

    /// Where `asset`'s positions start in its tree: a shift of its own, so that the runs of different assets do not
    /// close in the same check.
    function _off(uint32 asset) internal pure returns (uint256) {
        return uint256(keccak256(abi.encode(asset))) & OFF_MASK;
    }

    function _atPos(uint256 pos) internal view returns (uint256 blk, uint256 time) {
        uint256 w = _at[pos / 3] >> (pos % 3 * 80);
        return (uint40(w >> 40), uint40(w));
    }

    /// The oracle price of `asset` now; zero if the precompile did not answer or answered no uint64. It gets its whole
    /// `ORACLE_GAS`: a call too poor for that reverts, so that no caller can log a price as missing.
    function _oracle(uint32 asset) internal view returns (uint64) {
        if (gasleft() < ORACLE_GAS * 64 / 63 + 10_000) revert PokeGas(ORACLE_GAS);
        (bool ok, bytes memory r) =
            HLConstants.ORACLE_PX_PRECOMPILE_ADDRESS.staticcall{gas: ORACLE_GAS}(abi.encode(CoreOps.pxIndex(asset)));
        if (!ok || r.length < 32) return 0;
        uint256 px = abi.decode(r, (uint256));
        return px > MAX_PRICE ? 0 : uint64(px);
    }

    /// This block's best bid and offer of `asset`, logged as how far each side stands off the oracle price `px` on the
    /// trader's worse side, in millionths of it (`askOff << 16 | bidOff`); `NO_BOOK` for a book that does not answer,
    /// is empty on a side or crossed. It gets its whole `BBO_GAS`, as the oracle does.
    function _bbo(uint32 asset, uint64 px) internal view returns (uint32) {
        if (gasleft() < BBO_GAS * 64 / 63 + 10_000) revert PokeGas(BBO_GAS);
        (bool ok, bytes memory r) =
            HLConstants.BBO_PRECOMPILE_ADDRESS.staticcall{gas: BBO_GAS}(abi.encode(uint64(CoreOps.pxIndex(asset))));
        if (!ok || r.length < 64) return NO_BOOK;
        (uint256 bid, uint256 ask) = abi.decode(r, (uint256, uint256));
        if (bid == 0 || ask < bid || ask > type(uint64).max) return NO_BOOK;
        uint256 bidOff = bid < px ? (px - bid) * 1e6 / px : 0;
        uint256 askOff = ask > px ? (ask - px) * 1e6 / px : 0;
        if (bidOff > MAX_OFF) bidOff = MAX_OFF;
        if (askOff > MAX_OFF) askOff = MAX_OFF;
        return uint32(askOff << 16 | bidOff);
    }

    // ── records ──────────────────────────────────────────────────

    function _onlyExam() internal view {
        if (_locked) revert Reentered();
        address p = IPoolRegister(factory).poolOf(msg.sender);
        if (p == address(0) || p == msg.sender) revert NotExam();
    }

    function _split(uint64 key) internal pure returns (uint32, bool) {
        return (uint32(key >> 1), key & 1 == 1);
    }

    /// Takes `exam`'s entry out with any change asked of it (its key stays listed), and its asset off the watched ones
    /// once it has no entry left.
    function _drop(address exam, uint32 asset, bool isTake) internal {
        Entry storage e = _entry[exam][asset][isTake];
        (e.level, e.nextLevel, e.nextAt) = (0, 0, 0);
        if (--_count[asset] == 0) {
            uint256 ap = _assetPos[asset];
            uint32 lastAsset = _assets[_assets.length - 1];
            _assets[ap - 1] = lastAsset;
            _assetPos[lastAsset] = ap;
            _assets.pop();
            delete _assetPos[asset];
        }
    }
}
