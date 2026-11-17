// The chart's data rules (app/lib/chartdata.js): which intervals, which stretch of candles is asked for, how
// Hyperliquid's answer becomes the library's series, and how history is added on the left without a hole or a repeat.
// No browser: the chart itself (app/views/examchart.js) is looked at by eye.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import {
  INTERVALS, BAR_INTERVALS, MORE_INTERVALS, DEFAULT_INTERVAL, CANDLES_SHOWN, CANDLES_MORE, CANDLES_KEPT, EMPTY_STRETCHES,
  isInterval, candleWindow, olderWindow, afterOlder, historyStart, mergeRows, tailRows, toSeries, priceDecimals, candleText,
} from "../lib/chartdata.js";
import { candleRows } from "../lib/exam.js";

const NOW = 1_794_600_000_000;

test("the chart offers every interval Hyperliquid answers, and the header's buttons and its list split them without a gap", () => {
  // As the info API answered a candle request on 17 November 2026: these fourteen; "6h", "2d" and "7m" are refused.
  assert.deepEqual(INTERVALS.map((i) => i.key), ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M"]);
  assert.deepEqual([...BAR_INTERVALS], ["1m", "5m", "15m", "1h", "4h", "1d"]);
  assert.deepEqual([...MORE_INTERVALS], ["3m", "30m", "2h", "8h", "12h", "3d", "1w", "1M"]);
  assert.deepEqual([...BAR_INTERVALS, ...MORE_INTERVALS].sort(), INTERVALS.map((i) => i.key).sort());
  assert.ok(INTERVALS.every((i, n) => n === 0 || i.ms > INTERVALS[n - 1].ms), "shortest first");
  assert.ok(isInterval(DEFAULT_INTERVAL) && isInterval("1M") && !isInterval("6h") && !isInterval("1m "));
});

test("the first stretch is the last CANDLES_SHOWN candles of the chosen interval, for every interval, and no more than Hyperliquid keeps", () => {
  for (const { key, ms } of INTERVALS) {
    assert.deepEqual(candleWindow(key, NOW), { startTime: NOW - ms * CANDLES_SHOWN, endTime: NOW }, key);
    assert.ok(CANDLES_SHOWN <= CANDLES_KEPT);
  }
  assert.deepEqual(candleWindow("4h", NOW, 10), { startTime: NOW - 10 * 4 * 3_600_000, endTime: NOW });
  assert.throws(() => candleWindow("6h", NOW), /not an interval/);
  assert.throws(() => olderWindow("6h", NOW, NOW), /not an interval/);
});

test("a stretch added on the left ends the millisecond before the last one began, and none is asked beyond what is kept", () => {
  for (const { key, ms } of INTERVALS) {
    const before = NOW - ms * CANDLES_SHOWN;
    const w = olderWindow(key, before, NOW);
    assert.deepEqual(w, { startTime: before - ms * CANDLES_MORE, endTime: before - 1 }, key);
  }
  const ms = 60_000;
  assert.equal(olderWindow("1m", NOW - ms * CANDLES_KEPT, NOW), null, "the oldest candle Hyperliquid keeps is this one's start");
  assert.ok(olderWindow("1m", NOW - ms * CANDLES_KEPT + 1, NOW));
});

test("Hyperliquid's answer becomes the library's series: seconds, oldest first, one candle a time, nothing made up for a gap", () => {
  const raw = [
    { t: 1794578400000, o: "69414.0", c: "69500.0", h: "69550.0", l: "69400.0", v: "0.1" },
    { t: 1794577500000, o: "69631.0", c: "69414.0", h: "69723.0", l: "69020.0", v: "0.22174" },
    // 1794579300000 and 1794580200000 have no candle: nothing traded.
    { t: 1794581100000, o: "69500.0", c: "69480.0", h: "69510.0", l: "69470.0", v: "0" },
    { t: 1794578400000, o: "69414.0", c: "69520.0", h: "69560.0", l: "69400.0", v: "0.12" },   // the same candle again, later
    { t: 1794582000000, o: "0", c: "0", h: "0", l: "0", v: "0" },                                // no price: left out
  ];
  const s = toSeries(candleRows(raw), { up: "#2F7FC9", down: "#C0622A" });
  assert.deepEqual(s.candles.map((c) => c.time), [1794577500, 1794578400, 1794581100], "gaps stay gaps");
  assert.deepEqual(s.candles[1], { time: 1794578400, open: 69414, high: 69560, low: 69400, close: 69520 }, "the later reading of a time wins");
  assert.deepEqual(s.volume.map((v) => v.time), s.candles.map((c) => c.time));
  assert.deepEqual(s.volume.map((v) => v.value), [0.22174, 0.12, 0]);
  assert.deepEqual(s.volume.map((v) => v.color), ["#C0622A55", "#2F7FC955", "#C0622A55"], "a bar takes its candle's colour, lighter");
  assert.deepEqual(toSeries([]), { candles: [], volume: [] });
  assert.deepEqual(toSeries(candleRows({ error: "x" })), { candles: [], volume: [] });
});

test("the candle that is still forming is sent again at each reading: it replaces itself, and an older candle is not an update", () => {
  const have = candleRows([
    { t: 1000_000, o: "1", c: "2", h: "3", l: "1", v: "1" },
    { t: 2000_000, o: "2", c: "2", h: "3", l: "2", v: "1" },
  ]);
  const got = candleRows([
    { t: 1000_000, o: "1", c: "2", h: "3", l: "1", v: "1" },       // older than the last drawn: already there
    { t: 2000_000, o: "2", c: "5", h: "5", l: "2", v: "2" },       // the same candle, grown
    { t: 3000_000, o: "5", c: "6", h: "6", l: "5", v: "1" },       // a new one
  ]);
  const tail = tailRows(have, got);
  assert.deepEqual(tail.map((c) => [c.time, c.close]), [[2000, 5], [3000, 6]]);
  const merged = mergeRows(have, tail);
  assert.deepEqual(merged.map((c) => [c.time, c.close]), [[1000, 2], [2000, 5], [3000, 6]], "no candle twice");
  assert.deepEqual(tailRows([], got).map((c) => c.time), [1000, 2000, 3000], "nothing drawn yet: everything");
  assert.deepEqual(tailRows(merged, []), []);
});

// A stand-in for Hyperliquid: candles of one interval at every step from `from` to `to`, except the times in `quiet`,
// answered for a window the way the info API does, and nothing older than CANDLES_KEPT candles back.
function server(interval, from, to, quiet = new Set()) {
  const ms = INTERVALS.find((i) => i.key === interval).ms;
  const all = [];
  for (let t = from; t <= to; t += ms) if (!quiet.has(t)) all.push({ t, o: "10", c: "11", h: "12", l: "9", v: "1" });
  return { all, ask: (startTime, endTime) => all.filter((c) => c.t >= startTime && c.t <= endTime) };
}

// What the chart does when the view reaches the left edge, with the library taken out.
function scrollLeft(interval, srv, now) {
  let rows = candleRows(srv.ask(...Object.values(candleWindow(interval, now))));
  let hist = historyStart(rows);
  const asked = [];
  while (!hist.done) {
    const w = olderWindow(interval, hist.beforeMs, now);
    if (!w) { hist = { ...hist, done: true }; break; }
    asked.push(w);
    const got = candleRows(srv.ask(w.startTime, w.endTime));
    hist = afterOlder(hist, w, got);
    rows = mergeRows(rows, got);
  }
  return { rows, asked };
}

test("scrolling left adds older candles without a hole or a repeat, for every interval", () => {
  for (const { key, ms } of INTERVALS) {
    const now = 1_794_600_000_000;
    const start = now - ms * 1800;
    const srv = server(key, start - (start % ms), now - (now % ms));
    const { rows, asked } = scrollLeft(key, srv, now);
    // Hyperliquid answers 1800 candles here, all of them within what it keeps: every one is on the chart, once, in order.
    assert.deepEqual(rows.map((c) => c.time * 1000), srv.all.map((c) => c.t), key);
    assert.equal(new Set(rows.map((c) => c.time)).size, rows.length, `${key}: no repeat`);
    for (let i = 1; i < asked.length; i++) assert.equal(asked[i].endTime + 1, asked[i - 1].startTime, `${key}: each stretch ends where the last began`);
  }
});

test("quiet stretches do not end the history early, and enough of them in a row do", () => {
  const ms = 60_000;
  const now = 1_794_600_000_000;
  const edge = now - (now % ms) - ms * CANDLES_SHOWN;
  // 600 candles older than the first stretch, then a quiet stretch of CANDLES_MORE minus one, then more candles.
  const quiet = new Set();
  for (let i = 0; i < CANDLES_MORE; i++) quiet.add(edge - ms * (CANDLES_MORE + i));
  const srv = server("1m", edge - ms * 4000, now - (now % ms), quiet);
  const { rows } = scrollLeft("1m", srv, now);
  assert.equal(rows[0].time * 1000, srv.all[0].t, "the candles beyond the quiet stretch were reached");
  assert.equal(rows.length, srv.all.length);
  // The reading stops after EMPTY_STRETCHES empty stretches in a row.
  let hist = { beforeMs: 1_000_000, misses: 0, done: false };
  for (let i = 1; i <= EMPTY_STRETCHES; i++) {
    assert.equal(hist.done, false);
    hist = afterOlder(hist, olderWindow("1m", hist.beforeMs, 1_000_000), []);
    assert.equal(hist.misses, i);
  }
  assert.equal(hist.done, true);
  assert.equal(afterOlder({ beforeMs: 5, misses: 2, done: false }, { startTime: 1, endTime: 4 }, [{}]).misses, 0, "a candle read starts the count again");
  assert.deepEqual(historyStart([]), { beforeMs: Infinity, misses: 0, done: true }, "no candles: no history to ask for");
});

test("Hyperliquid keeps the latest five thousand candles: the 1m history stops there", () => {
  const ms = 60_000;
  const now = 1_794_600_000_000;
  const srv = server("1m", now - ms * 12000, now - (now % ms));
  // The stand-in answers like Hyperliquid: only the latest CANDLES_KEPT.
  const kept = { ask: (a, b) => srv.ask(a, b).filter((c) => c.t > now - ms * CANDLES_KEPT) };
  const { rows, asked } = scrollLeft("1m", kept, now);
  assert.ok(rows.length <= CANDLES_KEPT + 1);
  assert.ok(rows.length >= CANDLES_KEPT - 1, "everything it keeps is read");
  assert.ok(asked.length <= Math.ceil(CANDLES_KEPT / CANDLES_MORE) + 1, "no asking beyond what is kept");
});

test("prices are written with Hyperliquid's decimals: five significant figures, at most 6 less the size decimals", () => {
  assert.equal(priceDecimals(69414, 5), 0);
  assert.equal(priceDecimals(3412.5, 4), 1);
  assert.equal(priceDecimals(2.4731, 2), 4);
  assert.equal(priceDecimals(0.0431, 0), 6, "capped by the size decimals: 6 - 0");
  assert.equal(priceDecimals(0.0431, 3), 3);
  assert.equal(priceDecimals(21.5, 4), 2, "capped at 6 - 4");
  assert.equal(priceDecimals(0, 1), 2);
});

test("the pointer's candle is read in UTC", () => {
  assert.equal(candleText({ time: 0, open: 10, high: 12, low: 9, close: 11 }), "1970-01-01 00:00 UTC · O 10 H 12 L 9 C 11");
  assert.equal(candleText(undefined), "");
});

test("the chart's file is the library's own, unchanged, with its licence and notice beside it, and the page credits TradingView", () => {
  const dir = "../vendor/lightweight-charts/";
  const lib = readFileSync(new URL(`${dir}lightweight-charts.standalone.production.js`, import.meta.url));
  // The file of lightweight-charts@5.3.0 (dist/lightweight-charts.standalone.production.js) as the npm registry served it.
  assert.equal(createHash("sha256").update(lib).digest("hex"), "338a625ece52c5b85fb026e222dad5d0f365f745060afbfae96bda91636142e9");
  assert.match(lib.subarray(0, 300).toString("utf8"), /TradingView Lightweight Charts™ v5\.3\.0[\s\S]*Apache License 2\.0/);
  assert.match(readFileSync(new URL(`${dir}LICENSE`, import.meta.url), "utf8"), /Apache License\s+Version 2\.0/);
  assert.match(readFileSync(new URL(`${dir}NOTICE`, import.meta.url), "utf8"), /Copyright \(c\) 2026 TradingView, Inc\.\nhttps:\/\/www\.tradingview\.com\//);
  const view = readFileSync(new URL("../views/examchart.js", import.meta.url), "utf8");
  assert.match(view, /TradingView Lightweight Charts™ Copyright \(c\) 2026 TradingView, Inc\./);
  assert.match(view, /const TRADINGVIEW = "https:\/\/www\.tradingview\.com\/"/);
});
