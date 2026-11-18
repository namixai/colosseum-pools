// The lines of the open position on the chart (app/lib/positionlines.js): the entry worked out of the fills, the stop
// and the take as the page reads them; no browser.
import test from "node:test";
import assert from "node:assert/strict";
import { entryPrice, positionLines } from "../lib/positionlines.js";

const BTC = 3;
const ETH = 4;
const PERPS = new Map([[BTC, { index: BTC, name: "BTC", szDecimals: 5 }], [ETH, { index: ETH, name: "ETH", szDecimals: 4 }]]);
// BTC at 5 size decimals: one price decimal, so 600000 units are 60000.0.
const fill = (asset, isBuy, sz, px, source = 0) => ({ name: "Filled", args: { asset: BigInt(asset), isBuy, sz: BigInt(sz), px: BigInt(px), cost: 5n, late: 0n, source: BigInt(source) } });
const carried = (asset, isTake, sz, px, triggerPx, source = 0) => ({ name: "TriggerFilled", args: { asset: BigInt(asset), isTake, sz: BigInt(sz), px: BigInt(px), cost: 5n, triggerPx: BigInt(triggerPx), source: BigInt(source) } });
const account = (positions) => ({ positions });
const pos = (asset, size, extra = {}) => ({ asset, size: BigInt(size), side: size > 0 ? "long" : "short", stop: null, take: null, ...extra });
const trig = (px) => ({ px: BigInt(px), sz: 0n, state: "armed" });

test("one fill is the entry", () => {
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000)], BTC, 10_000n), 600_000);
  assert.equal(entryPrice([fill(BTC, false, 10_000, 600_000)], BTC, -10_000n), 600_000, "a short too");
});

test("more bought on the same side moves the average by size", () => {
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000), fill(BTC, true, 10_000, 620_000)], BTC, 20_000n), 610_000);
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000), fill(BTC, true, 30_000, 640_000)], BTC, 40_000n), 630_000);
});

test("part of the position closed leaves the average where it was", () => {
  const events = [fill(BTC, true, 20_000, 600_000), fill(BTC, false, 10_000, 650_000)];
  assert.equal(entryPrice(events, BTC, 10_000n), 600_000);
});

test("a position closed and opened again has the entry of the new one", () => {
  const events = [fill(BTC, true, 10_000, 600_000), fill(BTC, false, 10_000, 610_000), fill(BTC, false, 5_000, 620_000)];
  assert.equal(entryPrice(events, BTC, -5_000n), 620_000);
});

test("a fill that carries the position through zero opens the other side at its own price", () => {
  const events = [fill(BTC, true, 10_000, 600_000), fill(BTC, false, 15_000, 610_000)];
  assert.equal(entryPrice(events, BTC, -5_000n), 610_000);
});

test("a stop or a take that was carried out is one trade, not two", () => {
  const events = [fill(BTC, true, 20_000, 600_000), fill(BTC, false, 5_000, 590_000, 1), carried(BTC, false, 5_000, 590_000, 595_000, 1)];
  assert.equal(entryPrice(events, BTC, 15_000n), 600_000);
});

test("fills of another asset are not counted", () => {
  const events = [fill(ETH, true, 1_000, 300_000), fill(BTC, true, 10_000, 600_000), fill(ETH, false, 1_000, 310_000)];
  assert.equal(entryPrice(events, BTC, 10_000n), 600_000);
});

test("no proof, no price: fills that do not add up to the position, or none at all", () => {
  assert.equal(entryPrice([fill(BTC, true, 4_000, 600_000)], BTC, 10_000n), null, "the journal has not read back to the start of the position");
  assert.equal(entryPrice([], BTC, 10_000n), null);
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000)], BTC, 0n), null, "no position");
  assert.equal(entryPrice([carried(BTC, true, 1, 1, 1)], BTC, 10_000n), null, "a record that is not what the contract writes");
});

test("lines of a long position with a stop and a take, in the prices the page writes", () => {
  const lines = positionLines({ account: account([pos(BTC, 10_000, { stop: trig(550_000), take: trig(700_000) })]), asset: BTC, perps: PERPS,
    events: [fill(BTC, true, 10_000, 600_000)] });
  assert.deepEqual(lines, { entry: 60_000, stop: 55_000, take: 70_000 });
});

test("a short without a stop has the lines it has", () => {
  const lines = positionLines({ account: account([pos(BTC, -10_000, { take: trig(500_000) })]), asset: BTC, perps: PERPS, events: [fill(BTC, false, 10_000, 612_345)] });
  assert.deepEqual(lines, { entry: 61_234.5, stop: null, take: 50_000 });
});

test("without fills the entry is missing but the stop and the take are drawn", () => {
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

test("the page gives the chart the lines on every repaint of the head and again when the journal is read", async () => {
  const { readFileSync } = await import("node:fs");
  const page = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  assert.match(page, /st\.chart\?\.setPositionLines\(positionLines\(\{ account: st\.snap\?\.account, asset: st\.asset, perps: st\.perps, events: st\.journal \}\)\)/);
  assert.match(page, /paintJournal\(page, st\);\n {4}paintHead\(page, st\);/);
  const chart = readFileSync(new URL("../views/examchart.js", import.meta.url), "utf8");
  assert.match(chart, /setPositionLines\(lines\)/);
  assert.ok(!/localStorage\.(get|set)Item/.test(chart.replace(/function keep[\s\S]*?\n}\n/g, "")), "the browser's storage is touched only inside try/catch helpers");
});
