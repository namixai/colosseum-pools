// What the trading page's chart asks Hyperliquid for and how the answer becomes a series. No browser here: the
// chart itself is app/views/examchart.js, and every rule about candles that can be tested without it is in this file.

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** Every interval Hyperliquid's `candleSnapshot` answers (checked against its API on 17 Nov 2026: 6h, 2d and any other
 *  name get an error), with the length of one candle in milliseconds. A month is taken as 30 days: it only sizes the
 *  window asked for, a month's real candle is whatever Hyperliquid sends. */
export const INTERVALS = Object.freeze([
  { key: "1m", ms: MIN },
  { key: "3m", ms: 3 * MIN },
  { key: "5m", ms: 5 * MIN },
  { key: "15m", ms: 15 * MIN },
  { key: "30m", ms: 30 * MIN },
  { key: "1h", ms: HOUR },
  { key: "2h", ms: 2 * HOUR },
  { key: "4h", ms: 4 * HOUR },
  { key: "8h", ms: 8 * HOUR },
  { key: "12h", ms: 12 * HOUR },
  { key: "1d", ms: DAY },
  { key: "3d", ms: 3 * DAY },
  { key: "1w", ms: 7 * DAY },
  { key: "1M", ms: 30 * DAY },
]);
/** The intervals that have a button in the chart's header; the rest are in its "More" list. */
export const BAR_INTERVALS = Object.freeze(["1m", "5m", "15m", "1h", "4h", "1d"]);
export const MORE_INTERVALS = Object.freeze(INTERVALS.map((i) => i.key).filter((k) => !BAR_INTERVALS.includes(k)));
export const DEFAULT_INTERVAL = "15m";
/** Candles asked for when an asset or an interval is chosen, and for each stretch added to the left of what is there. */
export const CANDLES_SHOWN = 500;
export const CANDLES_MORE = 500;
/** Hyperliquid answers at most this many of the latest candles of an interval; before them it has none to give. */
export const CANDLES_KEPT = 5000;
/** A stretch that comes back empty is a quiet time, not necessarily the start of the history: this many in a row end it. */
export const EMPTY_STRETCHES = 3;

export const isInterval = (key) => INTERVALS.some((i) => i.key === key);

function step(interval) {
  const found = INTERVALS.find((i) => i.key === interval);
  if (!found) throw new Error(`${interval} is not an interval of the chart`);
  return found.ms;
}

/** The `startTime` and `endTime` (ms) of a candle request for `interval`: the last `count` candles up to `nowMs`. */
export function candleWindow(interval, nowMs, count = CANDLES_SHOWN) {
  return { startTime: nowMs - step(interval) * count, endTime: nowMs };
}

/**
 * The stretch to the left of what is already drawn: `count` candles ending the millisecond before `beforeMs` (the start
 * of what was asked last, so two stretches never overlap and never leave a hole). Null when Hyperliquid no longer keeps
 * candles that old for `interval` (`CANDLES_KEPT` back from `nowMs`).
 */
export function olderWindow(interval, beforeMs, nowMs, count = CANDLES_MORE) {
  const ms = step(interval);
  if (beforeMs <= nowMs - ms * CANDLES_KEPT) return null;
  return { startTime: beforeMs - ms * count, endTime: beforeMs - 1 };
}

/** Where the left edge of the history stands after a stretch was asked: always at the stretch's start, and one more
 *  empty stretch in a row when it came back with nothing. */
export function afterOlder(state, window, got) {
  const misses = got.length ? 0 : state.misses + 1;
  return { beforeMs: window.startTime, misses, done: misses >= EMPTY_STRETCHES };
}

/** The history's state at the start: the oldest candle read is the left edge. */
export function historyStart(rows) {
  return { beforeMs: rows.length ? rows[0].time * 1000 : Infinity, misses: 0, done: rows.length === 0 };
}

/** Candle rows (`candleRows`, app/lib/exam.js) joined: oldest first, one row a time, the later reading of a time
 *  replacing the earlier (the candle that is still forming is sent again and again). */
export function mergeRows(have, got) {
  const byTime = new Map();
  for (const c of have) byTime.set(c.time, c);
  for (const c of got) byTime.set(c.time, c);
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

/** Rows of the latest reading that go to the chart as updates to the series: the forming candle and anything after it,
 *  oldest first. An older candle cannot be updated in the library, and the first reading already holds it. */
export function tailRows(have, got) {
  const last = have.length ? have[have.length - 1].time : -Infinity;
  return mergeRows([], got).filter((c) => c.time >= last);
}

export const UP = "#2F7FC9";
export const DOWN = "#C0622A";

/** The library's two series out of candle rows: candles (time in seconds, UTC) and the volume under them, each bar in
 *  the colour of its candle at a lighter strength. A time Hyperliquid has no candle for is left out, not filled. */
export function toSeries(rows, { up = UP, down = DOWN } = {}) {
  const clean = mergeRows([], rows);
  return {
    candles: clean.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })),
    volume: clean.map((c) => ({ time: c.time, value: Number.isFinite(c.volume) ? c.volume : 0, color: `${c.close >= c.open ? up : down}55` })),
  };
}

/** How many decimals a price is written with: Hyperliquid's perp rule, five significant figures and no more than
 *  6 - szDecimals decimals (an integer price is always allowed). */
export function priceDecimals(price, szDecimals = 0) {
  if (!(price > 0)) return 2;
  const intDigits = Math.floor(Math.log10(price)) + 1;
  return Math.max(0, Math.min(6 - szDecimals, 5 - intDigits));
}

/** The candle under the pointer, in words: its time (UTC) and open, high, low, close. */
export function candleText(c) {
  if (!c) return "";
  const t = new Date(c.time * 1000).toISOString().replace("T", " ").slice(0, 16);
  return `${t} UTC · O ${c.open} H ${c.high} L ${c.low} C ${c.close}`;
}
