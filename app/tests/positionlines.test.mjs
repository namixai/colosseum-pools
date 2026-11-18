// The lines of the open position on the chart (app/lib/positionlines.js): the entry worked out of the fills, the stop
// and the take as the page reads them; no browser.
import test from "node:test";
import assert from "node:assert/strict";
import { positionLines } from "../lib/positionlines.js";

const BTC = 3;
const ETH = 4;
const PERPS = new Map([[BTC, { index: BTC, name: "BTC", szDecimals: 5 }], [ETH, { index: ETH, name: "ETH", szDecimals: 4 }]]);
// BTC at 5 size decimals: one price decimal, so 600000 units are 60000.0.
const account = (positions) => ({ positions });
const pos = (asset, size, extra = {}) => ({ asset, size: BigInt(size), side: size > 0 ? "long" : "short", stop: null, take: null, ...extra });
const trig = (px) => ({ px: BigInt(px), sz: 0n, state: "armed" });

test("lines of a long position with a stop and a take, in the prices the page writes", () => {
  const lines = positionLines({ account: account([pos(BTC, 10_000, { stop: trig(550_000), take: trig(700_000) })]), asset: BTC, perps: PERPS,
    entryOf: (asset, size) => (asset === BTC && size === 10_000n ? 600_000 : null) });
  assert.deepEqual(lines, { entry: 60_000, stop: 55_000, take: 70_000 });
});

test("a short without a stop has the lines it has", () => {
  const lines = positionLines({ account: account([pos(BTC, -10_000, { take: trig(500_000) })]), asset: BTC, perps: PERPS, entryOf: () => 612_345 });
  assert.deepEqual(lines, { entry: 61_234.5, stop: null, take: 50_000 });
});

test("a proof that is for another size is not drawn: the entry is asked for the size the position has now", () => {
  const asked = [];
  positionLines({ account: account([pos(BTC, 10_000)]), asset: BTC, perps: PERPS, entryOf: (a, size) => { asked.push([a, size]); return null; } });
  assert.deepEqual(asked, [[BTC, 10_000n]]);
});

test("without a proven entry the entry is missing but the stop and the take are drawn", () => {
  const lines = positionLines({ account: account([pos(BTC, 10_000, { stop: trig(550_000) })]), asset: BTC, perps: PERPS });
  assert.deepEqual(lines, { entry: null, stop: 55_000, take: null });
});

test("no position on this market: no lines, whatever another market holds", () => {
  const acc = account([pos(ETH, 1_000, { stop: trig(250_000) })]);
  assert.equal(positionLines({ account: acc, asset: BTC, perps: PERPS }), null);
  assert.deepEqual(positionLines({ account: acc, asset: ETH, perps: PERPS }), { entry: null, stop: 2500, take: null });
  assert.equal(positionLines({ account: account([]), asset: BTC, perps: PERPS }), null);
  assert.equal(positionLines({ account: null, asset: BTC, perps: PERPS }), null, "the first reading has not come yet");
});

test("an asset whose size decimals were not read has no lines: no price can be written", () => {
  const unknown = new Map([[BTC, { index: BTC, name: "#3", szDecimals: 0, unknown: true }]]);
  assert.equal(positionLines({ account: account([pos(BTC, 10_000, { stop: trig(550_000) })]), asset: BTC, perps: unknown }), null);
});

test("the page gives the chart the lines on every repaint of the head", async () => {
  const { readFileSync } = await import("node:fs");
  const page = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  assert.match(page, /st\.chart\?\.setPositionLines\(positionLines\(\{ account: st\.snap\?\.account, asset: st\.asset, perps: st\.perps, entryOf:/);
  const chart = readFileSync(new URL("../views/examchart.js", import.meta.url), "utf8");
  assert.match(chart, /setPositionLines\(lines\)/);
  assert.ok(!/localStorage\.(get|set)Item/.test(chart.replace(/function keep[\s\S]*?\n}\n/g, "")), "the browser's storage is touched only inside try/catch helpers");
});
