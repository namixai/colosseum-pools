// What one pass of the trading screen reads from the chain, and for which markets.
//
// The screen used to read five things of every market of the exam on every pass (its size, its stop, its take, its
// oracle price and its best bid and offer), 5 x N reads for N markets, every 5 seconds. Those reads already ride
// together in packs through Multicall3 (app/lib/gentle.js), so the node is asked a handful of calls, but each of the
// reads is still run by the node and its answer still comes back: a hundred markets are 500 reads and about 70 KB
// every pass. A pass now reads what the screen shows or works out from:
//   - the account (its getters), and the size of the position in every market, 32 bytes each: the account's equity
//     and its limits are judged on every open position, so none may be missed;
//   - the stop, the take, the oracle price and the book's best prices of the market on screen and of every market
//     that has a position: the ticket, the chart's lines, the positions table;
//   - the oracle price of the other markets only for the market list, when it is open (`readList`), on its own timer.
// Where a market has no position and is not on screen, nothing of it is read on a pass: an account is not worked
// out from its price (app/lib/exam.js skips a position of size zero) and no stop or take can stand where there is no
// position (PaperExam.setTrigger reverts NoPosition).
//
// No browser globals here: `io` is what the page reads with, so node's test runner loads it
// (app/tests/examreads.test.mjs).
import { bboOf } from "./exam.js";

/** How long the list's prices stand while it is open before they are read again. */
export const LIST_MS = 15_000;

const attempt = (fn) => Promise.resolve().then(fn).catch(() => null);
const big = (hex) => (hex && hex !== "0x" ? BigInt(hex) : null);

/** An oracle price as the screen takes it: null where it was not read, or read as zero (no price). */
export function oracleOf(hex) {
  const px = big(hex);
  return px === 0n ? null : px;
}

/**
 * One pass. `io` reads: `block()`, `account()` (the exam's getters, all at once), `position(asset)`, `stop(asset)`,
 * `take(asset)`, `oracle(asset)` and `bbo(asset)` (the last two the precompiles' raw answers). A price, a stop or a
 * take that could not be read is null, as before; the account, the block and a position that cannot be read fail the pass.
 *
 * `change(asset, isTake)` and `touches()`, where given, read an exam with a stop book: the change asked of a stop or a
 * take (`changeOf`) and the book's touches of the exam not yet carried out (`StopBook.touches`).
 *
 * Returns `{ block, got, perAsset, touches, fresh }`: the block, the account's answers, a row for every asset of the exam
 * (`read` is true for the ones whose stop, take, prices and book were read on this pass; the others carry the price
 * `prices` kept for them), and `fresh`, a Map asset -> oracle price of the assets read on this pass. The kept prices
 * are not handed back whole: a reading of the list may have changed them while this pass was out, so whoever keeps
 * them puts `fresh` over what they hold now (`withPrices`).
 */
export async function readPass(io, { assets, selected, prices = new Map() }) {
  // The market on screen is read at once, beside the account and the sizes: it is wanted whatever the sizes are.
  const details = async (asset) => {
    // A change asked of the stop or the take (`changeOf`), where the reader has it: an exam with a stop book.
    const change = (isTake) => (io.change ? attempt(() => io.change(asset, isTake)) : null);
    const [stop, take, oracleHex, bboHex, stopChange, takeChange] = await Promise.all([
      attempt(() => io.stop(asset)), attempt(() => io.take(asset)), attempt(() => io.oracle(asset)), attempt(() => io.bbo(asset)),
      change(false), change(true),
    ]);
    const bbo = bboOf(bboHex);
    return { stop, take, ...(io.change ? { stopChange, takeChange } : {}), oracle: oracleOf(oracleHex), bid: bbo?.bid ?? null, ask: bbo?.ask ?? null };
  };
  // The stop book's touches of this exam not yet carried out, where the reader has them: one read for every asset.
  const [block, got, sizes, first, touches] = await Promise.all([
    io.block(), io.account(), Promise.all(assets.map((a) => io.position(a))),
    assets.includes(selected) ? details(selected) : null, io.touches ? attempt(() => io.touches()) : null,
  ]);
  const size = new Map(assets.map((a, i) => [a, sizes[i]]));
  const held = assets.filter((a) => a !== selected && BigInt(size.get(a) ?? 0) !== 0n);
  const rest = await Promise.all(held.map(async (a) => [a, await details(a)]));
  const read = new Map(rest);
  if (first) read.set(selected, first);
  const perAsset = assets.map((asset) => {
    const d = read.get(asset);
    return d
      ? { asset, size: size.get(asset), ...d, read: true }
      : { asset, size: size.get(asset), stop: null, take: null, ...(io.change ? { stopChange: null, takeChange: null } : {}),
        oracle: prices.get(asset) ?? null, bid: null, ask: null, read: false };
  });
  return { block, got, perAsset, touches: touches ?? [], fresh: new Map([...read].map(([a, d]) => [a, d.oracle])) };
}

/** `prices` (a Map asset -> price) with `fresh` put over it: a new Map, nothing changed in place. */
export const withPrices = (prices, fresh) => new Map([...prices, ...fresh]);

/**
 * The oracle prices of the markets the list shows and a pass does not read (`skip`: the assets a pass reads), as a Map
 * asset -> price, null for one that could not be read: the ones read now, to be put over what is kept (`withPrices`).
 */
export async function readList(io, { assets, skip = new Set() }) {
  const wanted = assets.filter((a) => !skip.has(a));
  const hexes = await Promise.all(wanted.map((a) => attempt(() => io.oracle(a))));
  return new Map(wanted.map((a, i) => [a, oracleOf(hexes[i])]));
}

/**
 * The list's prices (`fresh`) without the markets the latest pass reads in full (`covered`): a pass that finished while the
 * list was being read has a newer price for them, and a position may have opened in one of them meanwhile.
 */
export function listPrices(fresh, covered) {
  return new Map([...fresh].filter(([asset]) => !covered.has(asset)));
}

/** The assets a pass of this account reads in full: the one on screen and every one with a position. */
export function fullyRead(perAsset) {
  return new Set(perAsset.filter((a) => a.read).map((a) => a.asset));
}

/**
 * Which passes are out and which one may draw. `start()` takes a number; only the pass with the highest number draws, so
 * an older answer never covers a newer one (`isLatest`), and an error of one that was passed by is not the screen's
 * business either. The background poll asks `running` and does not start a pass while one is out: a pass that takes
 * longer than the interval is let to finish and draw, instead of being passed by the next poll again and again. Passes
 * the page starts itself (after a transaction, after a change of market) are not held off, and pass the older ones. A pass
 * older than `staleMs` is not waited for (a request the node never answers would otherwise stop the poll for good).
 */
export function createPasses({ now = Date.now, staleMs = Infinity } = {}) {
  let last = 0;
  const out = new Map();   // seq -> when it started, for the passes not ended
  return {
    start() {
      const seq = ++last;
      out.set(seq, now());
      return {
        seq,
        isLatest: () => seq === last,
        end() {
          out.delete(seq);
        },
      };
    },
    /** Whether a pass is out that the poll should wait for: not one older than `staleMs`, which may never come back. A
     *  pass given up like that is not ended, but the poll's next pass is the latest, so if it does come back it is not drawn. */
    get running() {
      for (const startedAt of out.values()) if (now() - startedAt < staleMs) return true;
      return false;
    },
  };
}

/** How many poll intervals a pass may be out before the poll stops waiting for it. */
export const STALE_PASS_POLLS = 3;

/**
 * The price shown for a market: the one kept for it (a kept null is a price that was not read, and stands as that), else
 * the row's own where a pass read the row in full, else none. A row a pass did not read carries only what was kept when
 * the pass began, which the list may have dropped since.
 */
export function shownPrice(prices, row) {
  if (!row) return null;
  if (prices.has(row.asset)) return prices.get(row.asset);
  return row.read ? row.oracle : null;
}
