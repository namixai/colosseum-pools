// The chart's moving averages and the choice of indicators (app/lib/indicators.js), without a browser.
import test from "node:test";
import assert from "node:assert/strict";
import { ema, emaPoints, INDICATORS, DEFAULT_CHOICE, choiceText, readChoice } from "../lib/indicators.js";

const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} is not ${b}`);
const rows = (n) => Array.from({ length: n }, (_, i) => ({ time: 1000 + i * 60, close: 100 + i }));

test("a known series gives the known averages: the first is the plain mean, the rest are smoothed by 2 / (period + 1)", () => {
  assert.deepEqual(ema([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  const out = ema([10, 20, 30, 40], 2);  // k = 2/3: seed 15, then 30 * 2/3 + 15 / 3 = 25, then 40 * 2/3 + 25 / 3
  assert.equal(out[0], null);
  close(out[1], 15);
  close(out[2], 25);
  close(out[3], 40 * 2 / 3 + 25 / 3);
});

test("the line starts where the data suffices: index period - 1", () => {
  const out = ema(rows(30).map((r) => r.close), 20);
  assert.equal(out.length, 30);
  assert.ok(out.slice(0, 19).every((v) => v === null));
  close(out[19], 109.5);  // the mean of 100 ... 119
  assert.ok(out.slice(19).every((v) => Number.isFinite(v)));
});

test("a series shorter than the period has no average, an empty one has nothing", () => {
  assert.deepEqual(ema([1, 2, 3], 5), [null, null, null]);
  assert.deepEqual(emaPoints(rows(199), 200), []);
  assert.deepEqual(ema([], 20), []);
  assert.deepEqual(emaPoints([], 20), []);
  assert.equal(emaPoints(rows(200), 200).length, 1, "exactly the period: one point, the mean");
});

test("a period of 1 is the series itself, and a period that is not a whole number is refused", () => {
  assert.deepEqual(ema([3, 1, 4], 1), [3, 1, 4]);
  for (const bad of [0, -1, 2.5, NaN, "20"]) assert.throws(() => ema([1, 2, 3], bad));
});

test("a value that is not a number ends the line there instead of spreading NaN", () => {
  assert.deepEqual(ema([1, 2, 3, NaN, 5], 2), [null, 1.5, 2.5, null, null]);
  assert.deepEqual(ema([NaN, 2, 3], 2), [null, null, null]);
});

test("the points carry the candles' times, and only where the average exists", () => {
  const pts = emaPoints(rows(5), 3);
  assert.deepEqual(pts.map((p) => p.time), [1120, 1180, 1240]);
  assert.deepEqual(pts.map((p) => p.value), [101, 102, 103], "a straight climb of 1 a candle: the average trails by 1");
});

test("a new candle changes only the averages from its time on", () => {
  const all = rows(60);
  const before = emaPoints(all.slice(0, 59), 20);
  const after = emaPoints(all, 20);
  assert.deepEqual(after.slice(0, before.length), before);
  assert.equal(after.length, before.length + 1);
});

test("three averages and the volume, the volume alone on by default", () => {
  assert.deepEqual(INDICATORS.map((i) => i.label), ["Volume", "EMA 20", "EMA 50", "EMA 200"]);
  assert.deepEqual(INDICATORS.filter((i) => i.period).map((i) => i.period), [20, 50, 200]);
  assert.equal(new Set(INDICATORS.filter((i) => i.color).map((i) => i.color)).size, 3, "three colours");
  assert.deepEqual(DEFAULT_CHOICE, { volume: true, ema20: false, ema50: false, ema200: false });
  assert.deepEqual(readChoice(null), DEFAULT_CHOICE);
  assert.deepEqual(readChoice(undefined), DEFAULT_CHOICE);
});

test("the choice is kept as the keys that are on and read back the same", () => {
  for (const choice of [DEFAULT_CHOICE, { volume: false, ema20: true, ema50: false, ema200: true },
    { volume: false, ema20: false, ema50: false, ema200: false }, { volume: true, ema20: true, ema50: true, ema200: true }]) {
    assert.deepEqual(readChoice(choiceText(choice)), choice);
  }
  assert.equal(choiceText({ volume: false, ema20: false, ema50: false, ema200: false }), "[]");
});

test("kept text that is not a list of known keys is the default, never a broken chart", () => {
  for (const junk of ["", "x", "{}", "null", "[1]", '["ema7"]', '["volume","ema7"]', '"volume"', "[[]]"]) {
    assert.deepEqual(readChoice(junk), DEFAULT_CHOICE, junk);
  }
});
