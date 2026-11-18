// The entry price of an open position, worked out of the fills (app/lib/entry.js), without a browser.
import test from "node:test";
import assert from "node:assert/strict";
import { entryPrice, entryFromFills, tradesOf, foldFill, entryOfState, stateOf, FLAT } from "../lib/entry.js";

const BTC = 3;
const ETH = 4;
// BTC at 5 size decimals: one price decimal, so 600000 units are 60000.0.
const fill = (asset, isBuy, sz, px, source = 0) => ({ name: "Filled", args: { asset: BigInt(asset), isBuy, sz: BigInt(sz), px: BigInt(px), cost: 5n, late: 0n, source: BigInt(source) } });
const carried = (asset, isTake, sz, px, triggerPx, source = 0) => ({ name: "TriggerFilled", args: { asset: BigInt(asset), isTake, sz: BigInt(sz), px: BigInt(px), cost: 5n, triggerPx: BigInt(triggerPx), source: BigInt(source) } });

test("one fill is the entry, long or short", () => {
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000)], BTC, 10_000n), 600_000);
  assert.equal(entryPrice([fill(BTC, false, 10_000, 600_000)], BTC, -10_000n), 600_000);
});

test("more on the same side moves the average by size", () => {
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000), fill(BTC, true, 10_000, 620_000)], BTC, 20_000n), 610_000);
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000), fill(BTC, true, 30_000, 640_000)], BTC, 40_000n), 630_000);
});

test("part of the position closed leaves the average where it was", () => {
  const events = [fill(BTC, true, 20_000, 600_000), fill(BTC, false, 10_000, 650_000)];
  assert.equal(entryPrice(events, BTC, 10_000n), 600_000);
  const more = [fill(BTC, true, 10_000, 600_000), fill(BTC, true, 10_000, 620_000), fill(BTC, false, 5_000, 700_000), fill(BTC, true, 5_000, 640_000)];
  // 20 000 held at 610 000, 5 000 closed (still 610 000 on 15 000), then 5 000 more at 640 000
  assert.equal(entryPrice(more, BTC, 20_000n), (610_000 * 15_000 + 640_000 * 5_000) / 20_000);
});

test("a position closed to zero and opened again has the entry of the new one", () => {
  const events = [fill(BTC, true, 10_000, 600_000), fill(BTC, false, 10_000, 610_000), fill(BTC, false, 5_000, 620_000)];
  assert.equal(entryPrice(events, BTC, -5_000n), 620_000);
});

test("a fill that carries the position through zero opens the other side at its own price, for what is left", () => {
  const events = [fill(BTC, true, 10_000, 600_000), fill(BTC, false, 15_000, 610_000)];
  assert.equal(entryPrice(events, BTC, -5_000n), 610_000);
  const then = [...events, fill(BTC, false, 5_000, 630_000)];
  assert.equal(entryPrice(then, BTC, -10_000n), 620_000, "5 000 at 610 000 and 5 000 at 630 000");
});

test("a flip is proven even when the journal begins after the trade before it", () => {
  // nothing before the flip is known; the position before the flipping trade is known exactly (long 3 000) from the size now
  assert.equal(entryPrice([fill(BTC, false, 8_000, 610_000)], BTC, -5_000n), 610_000);
});

test("no position is no entry", () => {
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000), fill(BTC, false, 10_000, 610_000)], BTC, 0n), null);
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000)], BTC, 0n), null);
});

test("a stop or a take that was carried out is one trade, not two", () => {
  const events = [fill(BTC, true, 20_000, 600_000), fill(BTC, false, 5_000, 590_000, 1), carried(BTC, false, 5_000, 590_000, 595_000, 1)];
  assert.equal(entryPrice(events, BTC, 15_000n), 600_000);
  assert.equal(tradesOf(events, BTC).length, 2);
});

test("fills of another asset are not counted", () => {
  const events = [fill(ETH, true, 1_000, 300_000), fill(BTC, true, 10_000, 600_000), fill(ETH, false, 1_000, 310_000)];
  assert.equal(entryPrice(events, BTC, 10_000n), 600_000);
});

test("no proof, no price: the fills run out before the position is accounted for", () => {
  assert.equal(entryPrice([fill(BTC, true, 4_000, 600_000)], BTC, 10_000n), null, "only part of the position is in the fills");
  assert.equal(entryPrice([], BTC, 10_000n), null);
  assert.equal(entryPrice([fill(BTC, true, 10_000, 600_000), fill(BTC, true, 5_000, 640_000)], BTC, 10_000n), 620_000,
    "more than the position: it was short 5 000 before, and the first fill carried it through zero for what is left, 5 000 at 600 000, then 5 000 at 640 000");
  assert.equal(entryPrice([carried(BTC, true, 1, 1, 1)], BTC, 10_000n), null, "a record that is not what the contract writes");
  assert.equal(entryFromFills(null, 5n), null);
});

test("a part closed leaves the average exactly where it was, whatever the sizes", () => {
  // 95 at 550000 and 5 at 550010, then 42 sold: the average must not move by a rounding
  const events = [fill(BTC, true, 95, 550_000), fill(BTC, true, 5, 550_010), fill(BTC, false, 42, 560_000)];
  const before = entryPrice(events.slice(0, 2), BTC, 100n);
  assert.equal(entryPrice(events, BTC, 58n), before);
  assert.equal(Math.round(before), 550_001);
});

test("the running state folds one fill at a time", () => {
  let s = foldFill(FLAT, { isBuy: true, size: 10n, px: 100n });
  assert.deepEqual([s.held, entryOfState(s)], [10n, 100]);
  s = foldFill(s, { isBuy: true, size: 10n, px: 120n });
  assert.equal(entryOfState(s), 110);
  s = foldFill(s, { isBuy: false, size: 5n, px: 500n });
  assert.deepEqual([s.held, entryOfState(s)], [15n, 110]);
  s = foldFill(s, { isBuy: false, size: 15n, px: 90n });
  assert.deepEqual([s.held, entryOfState(s)], [0n, null]);
  assert.equal(entryOfState(stateOf(-4n, 250)), 250);
  assert.equal(stateOf(-4n, 250).avg, 250);
});
