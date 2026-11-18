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
 * Returns `{ block, got, perAsset, fresh }`: the block, the account's answers, a row for every asset of the exam
 * (`read` is true for the ones whose stop, take, prices and book were read on this pass; the others carry the price
 * `prices` kept for them), and `fresh`, a Map asset -> oracle price of the assets read on this pass. The kept prices
 * are not handed back whole: a reading of the list may have changed them while this pass was out, so whoever keeps
 * them puts `fresh` over what they hold now (`withPrices`).
 */
export async function readPass(io, { assets, selected, prices = new Map() }) {
  // The market on screen is read at once, beside the account and the sizes: it is wanted whatever the sizes are.
  const details = async (asset) => {
    const [stop, take, oracleHex, bboHex] = await Promise.all([
      attempt(() => io.stop(asset)), attempt(() => io.take(asset)), attempt(() => io.oracle(asset)), attempt(() => io.bbo(asset)),
    ]);
    const bbo = bboOf(bboHex);
    return { stop, take, oracle: oracleOf(oracleHex), bid: bbo?.bid ?? null, ask: bbo?.ask ?? null };
  };
  const [block, got, sizes, first] = await Promise.all([
    io.block(), io.account(), Promise.all(assets.map((a) => io.position(a))),
    assets.includes(selected) ? details(selected) : null,
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
      : { asset, size: size.get(asset), stop: null, take: null, oracle: prices.get(asset) ?? null, bid: null, ask: null, read: false };
  });
  return { block, got, perAsset, fresh: new Map([...read].map(([a, d]) => [a, d.oracle])) };
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
