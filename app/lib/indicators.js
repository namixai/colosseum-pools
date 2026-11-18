// The indicators of the trading page's chart, without a browser: the exponential moving average, the list the
// "Indicators" button offers and how the choice is kept. The chart that draws them is app/views/examchart.js.

/**
 * The exponential moving average of `values` over `period`: the same length as `values`, null where the average does
 * not exist yet. It starts at index `period - 1` with the plain average of the first `period` values and goes on with
 * the usual smoothing 2 / (period + 1). A series shorter than the period has no average at all; an empty one gives [].
 * A value that is not a finite number cannot be bridged: the average is null from there on.
 */
export function ema(values, period) {
  if (!Number.isInteger(period) || period < 1) throw new Error("the period of an average is a whole number from 1");
  const out = new Array(values.length).fill(null);
  if (values.length < period) return out;
  const k = 2 / (period + 1);
  let sum = 0;
  for (let i = 0; i < period; i += 1) {
    if (!Number.isFinite(values[i])) return out;
    sum += values[i];
  }
  let prev = sum / period;
  out[period - 1] = prev;
  for (let i = period; i < values.length; i += 1) {
    if (!Number.isFinite(values[i])) return out;
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

/** The average of the closing prices of candle rows ({time, close}, oldest first) as points of a line: only where it exists. */
export function emaPoints(rows, period) {
  const line = ema(rows.map((r) => r.close), period);
  const points = [];
  for (let i = 0; i < rows.length; i += 1) if (line[i] !== null) points.push({ time: rows[i].time, value: line[i] });
  return points;
}

/** What the "Indicators" button offers, in order. A line has its period and its colour (apart from the candles' two). */
export const INDICATORS = Object.freeze([
  { key: "volume", label: "Volume" },
  { key: "ema20", label: "EMA 20", period: 20, color: "#C9930A" },
  { key: "ema50", label: "EMA 50", period: 50, color: "#8A4FBF" },
  { key: "ema200", label: "EMA 200", period: 200, color: "#1F8A70" },
]);
export const DEFAULT_CHOICE = Object.freeze({ volume: true, ema20: false, ema50: false, ema200: false });

/** The choice kept as text: the keys that are on. */
export const choiceText = (choice) => JSON.stringify(INDICATORS.filter((i) => choice[i.key]).map((i) => i.key));

/** The choice out of kept text; anything that is not a list of known keys is the default. An empty list is a choice: all off. */
export function readChoice(text) {
  if (typeof text !== "string") return { ...DEFAULT_CHOICE };
  let keys;
  try {
    keys = JSON.parse(text);
  } catch {
    return { ...DEFAULT_CHOICE };
  }
  if (!Array.isArray(keys) || !keys.every((k) => INDICATORS.some((i) => i.key === k))) return { ...DEFAULT_CHOICE };
  return Object.fromEntries(INDICATORS.map((i) => [i.key, keys.includes(i.key)]));
}
