// What is read for the entry price, what is kept and when nothing is read (app/lib/entrytrack.js), with a node and a
// storage made up here.
import test from "node:test";
import assert from "node:assert/strict";
import { createEntryTracker, browserStore, prove, READ_BLOCKS } from "../lib/entrytrack.js";

const BTC = 3;
const ETH = 4;
const EXAM = "0xAbC0000000000000000000000000000000000001";
const fill = (asset, isBuy, sz, px, block) => ({ name: "Filled", block, index: 0, args: { asset: BigInt(asset), isBuy, sz: BigInt(sz), px: BigInt(px), cost: 5n, late: 0n, source: 0n } });
const AT = { fillAt: 1_000n, cash: 5n };   // the exam's lastFillAt and cash as read
const KEY = (asset) => `exam-entry-v1:${EXAM.toLowerCase()}:${asset}`;

function memory(seed = {}) {
  const m = new Map(Object.entries(seed));
  return { map: m, get: (k) => (m.has(k) ? m.get(k) : null), set: (k, v) => { m.set(k, v); }, remove: (k) => { m.delete(k); } };
}
/** A node that holds `events` and counts the readings made of it. */
function node(events, { fail = false } = {}) {
  const calls = [];
  return { calls, read: async (from, to) => {
    calls.push([from, to]);
    if (fail) throw new Error("the node refused");
    return events.filter((e) => e.block >= from && e.block <= to);
  } };
}
const settled = () => new Promise((r) => setTimeout(r, 5));
const make = (n, store, changes = []) => createEntryTracker({ address: EXAM, read: n.read, store, onChange: () => changes.push(1) });

test("a position with no price known is read once, from the block it was read at back over one journal reading", async () => {
  const n = node([fill(BTC, true, 6_000, 598_000, 4_900), fill(BTC, true, 4_000, 602_000, 4_950)]);
  const store = memory();
  const changes = [];
  const t = make(n, store, changes);
  t.observe({ asset: BTC, size: 10_000n, block: 5_000, ...AT });
  await settled();
  assert.deepEqual(n.calls, [[5_000 - READ_BLOCKS + 1, 5_000]]);
  assert.equal(t.entry(BTC, 10_000n), 599_600);
  assert.equal(changes.length, 1);
  assert.deepEqual(JSON.parse(store.get(KEY(BTC))), { size: "10000", entry: 599_600, block: 5_000, fillAt: "1000", cash: "5" });
});

test("the price is given only for the size it was proven for", async () => {
  const t = make(node([fill(BTC, true, 10_000, 600_000, 4_990)]), memory());
  t.observe({ asset: BTC, size: 10_000n, block: 5_000, ...AT });
  await settled();
  assert.equal(t.entry(BTC, 10_000n), 600_000);
  assert.equal(t.entry(BTC, 12_000n), null);
});

test("a kept proof for the same size is drawn without a reading", async () => {
  const n = node([]);
  const store = memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_000, fillAt: "1000", cash: "5" }) });
  const t = make(n, store);
  t.observe({ asset: BTC, size: 10_000n, block: 9_000, ...AT });
  await settled();
  assert.deepEqual(n.calls, []);
  assert.equal(t.entry(BTC, 10_000n), 600_000, "an hour later, a day later: the same");
});

test("a size that changed is read from the block after the last one the proof knows, and added to it", async () => {
  const n = node([fill(BTC, true, 10_000, 600_000, 4_000), fill(BTC, true, 10_000, 620_000, 4_500)]);
  const store = memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_100, fillAt: "1000", cash: "5" }) });
  const t = make(n, store);
  t.observe({ asset: BTC, size: 20_000n, block: 4_600, ...AT });
  await settled();
  assert.deepEqual(n.calls, [[4_101, 4_600]], "only the blocks after the proof are read");
  assert.equal(t.entry(BTC, 20_000n), 610_000, "the kept 10 000 at 600 000 and the new 10 000 at 620 000, the fill before the proof not counted twice");
  assert.deepEqual(JSON.parse(store.get(KEY(BTC))), { size: "20000", entry: 610_000, block: 4_600, fillAt: "1000", cash: "5" });
});

test("a proof older than one reading is not read up to: only a reading back, and no price if that does not prove it", async () => {
  const n = node([]);
  const t = make(n, memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 100, fillAt: "1000", cash: "5" }) }));
  t.observe({ asset: BTC, size: 20_000n, block: 100_000, ...AT });
  await settled();
  assert.deepEqual(n.calls, [[100_000 - READ_BLOCKS + 1, 100_000]]);
  assert.equal(t.entry(BTC, 20_000n), null);
});

test("a proof too old to reach is not built on, and fills that do not reach back to the opening prove nothing", async () => {
  const n = node([fill(BTC, false, 5_000, 610_000, 9_900)]);
  const store = memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 100, fillAt: "1000", cash: "5" }) });
  const t = make(n, store);
  t.observe({ asset: BTC, size: 5_000n, block: 10_000, ...AT });
  await settled();
  assert.equal(t.entry(BTC, 5_000n), null, "a day of fills between the proof and the reading is missing: no price rather than a guess");
  assert.equal(store.get(KEY(BTC)), null);
});

test("fills that do not come to the size drop the record: no price, nothing said", async () => {
  const store = memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_950, fillAt: "1000", cash: "5" }) });
  const t = make(node([fill(BTC, true, 1_000, 650_000, 4_990)]), store);
  t.observe({ asset: BTC, size: 20_000n, block: 5_000, ...AT });
  await settled();
  assert.equal(t.entry(BTC, 20_000n), null);
  assert.equal(store.get(KEY(BTC)), null, "the record is thrown out");
});

test("a node that refuses leaves no price and no record, and the reading is not repeated for the same size", async () => {
  const n = node([], { fail: true });
  const store = memory();
  const t = make(n, store);
  t.observe({ asset: BTC, size: 10_000n, block: 5_000, ...AT });
  await settled();
  assert.equal(t.entry(BTC, 10_000n), null);
  assert.equal(n.calls.length, 1);
  for (let i = 0; i < 5; i += 1) t.observe({ asset: BTC, size: 10_000n, block: 5_005 + i, ...AT });
  await settled();
  assert.equal(n.calls.length, 1, "nothing is read on a timer: the same size is not asked again");
  t.observe({ asset: BTC, size: 12_000n, block: 5_100, ...AT });
  await settled();
  assert.equal(n.calls.length, 2, "a new size is");
});

test("no position: nothing is read and any record is thrown out", async () => {
  const n = node([]);
  const store = memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_000, fillAt: "1000", cash: "5" }) });
  const changes = [];
  const t = make(n, store, changes);
  t.observe({ asset: BTC, size: 0n, block: 5_000, ...AT });
  await settled();
  assert.deepEqual(n.calls, []);
  assert.equal(store.get(KEY(BTC)), null);
  assert.equal(changes.length, 1);
});

test("a reading made while another is out waits for it and only the latest asking is made", async () => {
  const n = node([fill(BTC, true, 10_000, 600_000, 4_990), fill(ETH, false, 2_000, 300_000, 4_995)]);
  const t = make(n, memory());
  t.observe({ asset: BTC, size: 10_000n, block: 5_000, ...AT });
  t.observe({ asset: ETH, size: -1_000n, block: 5_001, ...AT });
  t.observe({ asset: ETH, size: -2_000n, block: 5_002, ...AT });
  await settled();
  assert.equal(n.calls.length, 2, "one at a time: BTC, then the latest asking, ETH");
  assert.equal(t.entry(BTC, 10_000n), 600_000);
  assert.equal(t.entry(ETH, -2_000n), 300_000);
});

test("events already read (the journal's) prove the price without a reading of its own", () => {
  const n = node([]);
  const store = memory();
  const t = make(n, store);
  t.feed({ events: [fill(BTC, true, 10_000, 600_000, 4_990)], from: 4_000, to: 5_000 }, { asset: BTC, size: 10_000n, block: 5_000, ...AT });
  assert.equal(t.entry(BTC, 10_000n), 600_000);
  assert.deepEqual(n.calls, []);
  assert.ok(store.get(KEY(BTC)));
  t.feed({ events: [], from: 4_000, to: 4_999 }, { asset: ETH, size: 5n, block: 5_000, ...AT });
  assert.equal(t.entry(ETH, 5n), null, "events that stop before the block the size was read at prove nothing");
});

test("a record that is not one is ignored; a browser without storage keeps nothing and does not throw", async () => {
  for (const junk of ["", "x", "{}", '{"size":"0","entry":1,"block":1}', '{"size":"5","entry":-1,"block":1}', '{"size":5,"entry":1,"block":1}', '{"size":"5","entry":1,"block":-1}']) {
    const n = node([fill(BTC, true, 5, 100, 4_990)]);
    const t = make(n, memory({ [KEY(BTC)]: junk }));
    t.observe({ asset: BTC, size: 5n, block: 5_000, ...AT });
    await settled();
    assert.equal(n.calls.length, 1, `read for ${JSON.stringify(junk)}`);
    assert.equal(t.entry(BTC, 5n), 100);
  }
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
  const store = browserStore(broken);
  assert.equal(store.get("k"), null);
  store.set("k", "v");
  store.remove("k");
  const t = make(node([fill(BTC, true, 5, 100, 4_990)]), store);
  t.observe({ asset: BTC, size: 5n, block: 5_000, ...AT });
  await settled();
  assert.equal(t.entry(BTC, 5n), 100, "it works for this visit");
});

test("prove: a record inside what was read is added to, one outside it is not", () => {
  const events = [fill(BTC, true, 5_000, 620_000, 150)];
  const record = { size: 5_000n, entry: 600_000, block: 100 };
  assert.equal(prove({ events, from: 101, asset: BTC, size: 10_000n, record }), 610_000);
  assert.equal(prove({ events, from: 200, asset: BTC, size: 10_000n, record }), null, "the record ends before the reading starts: a gap");
});

test("a stopped tracker reads nothing more", async () => {
  const n = node([]);
  const t = make(n, memory());
  t.stop();
  t.observe({ asset: BTC, size: 10_000n, block: 5_000, ...AT });
  await settled();
  assert.deepEqual(n.calls, []);
});

test("the screen asks every few seconds while a slow reading is out: the same size is read once, and not again after it fails", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const calls = [];
  const t = createEntryTracker({ address: EXAM, store: memory(), read: async (from, to) => { calls.push([from, to]); await gate; throw new Error("late failure"); } });
  t.observe({ asset: BTC, size: 10_000n, block: 5_000, ...AT });
  t.observe({ asset: BTC, size: 10_000n, block: 5_005, ...AT });
  t.observe({ asset: BTC, size: 10_000n, block: 5_010, ...AT });
  release();
  await settled();
  await settled();
  assert.equal(calls.length, 1);
});

test("one reading is the journal's: 20 calls of 50 blocks", async () => {
  const { readFileSync } = await import("node:fs");
  const page = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  const window = Number(page.match(/const JOURNAL_WINDOW = (\d+);/)[1]);
  const calls = Number(page.match(/const JOURNAL_CALLS = (\d+);/)[1]);
  assert.equal(window * calls, READ_BLOCKS);
  assert.match(page, /readLogs\(\{ from, to, window: JOURNAL_WINDOW, maxCalls: JOURNAL_CALLS,/);
  assert.match(page, /st\.entries\?\.observe\(/);
  assert.match(page, /fillAt: st\.snap\.reads\.lastFillAt, cash: st\.snap\.reads\.cash/, "the exam's own reading of lastFillAt and cash, no call of its own");
  assert.equal((page.match(/exam\.lastFillAt\(\)/g) ?? []).length, 1);
  assert.ok(!/setInterval\([^)]*trackEntry/.test(page), "the entry price is not read on a timer of its own");
});

test("the same size and the same last fill and cash: the kept proof is used and no log is read", async () => {
  const n = node([]);
  const t = make(n, memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_000, fillAt: "1700000000", cash: "9000000000" }) }));
  t.observe({ asset: BTC, size: 10_000n, block: 90_000, fillAt: 1_700_000_000n, cash: 9_000_000_000n });
  await settled();
  assert.deepEqual(n.calls, []);
  assert.equal(t.entry(BTC, 10_000n), 600_000);
});

test("the same size but another last fill: the kept price is not used, the fills since are read and added", async () => {
  // closed and opened again to the same size while the page was away: the old record says 600 000, the fills say 640 000
  const events = [fill(BTC, false, 10_000, 610_000, 4_300), fill(BTC, true, 10_000, 640_000, 4_400)];
  const n = node(events);
  const store = memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_000, fillAt: "1700000000", cash: "9000000000" }) });
  const t = make(n, store);
  t.observe({ asset: BTC, size: 10_000n, block: 4_500, fillAt: 1_700_000_500n, cash: 9_000_100_000n });
  assert.equal(t.entry(BTC, 10_000n), null, "not drawn while the reading is out");
  await settled();
  assert.deepEqual(n.calls, [[4_001, 4_500]], "only the blocks after the record");
  assert.equal(t.entry(BTC, 10_000n), 640_000, "the new price, not the old one");
  assert.deepEqual(JSON.parse(store.get(KEY(BTC))), { size: "10000", entry: 640_000, block: 4_500, fillAt: "1700000500", cash: "9000100000" });
});

test("the same size and last fill but another cash (a fill under 10 dollars moves cash and not lastFillAt): read, not used", async () => {
  const n = node([fill(BTC, false, 10_000, 610_000, 4_300), fill(BTC, true, 10_000, 640_000, 4_400)]);
  const t = make(n, memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_000, fillAt: "1700000000", cash: "9000000000" }) }));
  t.observe({ asset: BTC, size: 10_000n, block: 4_500, fillAt: 1_700_000_000n, cash: 8_999_999_990n });
  await settled();
  assert.equal(n.calls.length, 1);
  assert.equal(t.entry(BTC, 10_000n), 640_000);
});

test("a fill in another market moves lastFillAt: the kept price is read again and stays if no fill of this market came", async () => {
  const n = node([fill(ETH, true, 1_000, 300_000, 4_300)]);
  const t = make(n, memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 4_000, fillAt: "1700000000", cash: "9000000000" }) }));
  t.observe({ asset: BTC, size: 10_000n, block: 4_500, fillAt: 1_700_000_300n, cash: 8_999_000_000n });
  await settled();
  assert.equal(n.calls.length, 1);
  assert.equal(t.entry(BTC, 10_000n), 600_000);
});

test("the same size and another last fill, and the fills cannot be read back far enough: the record is thrown out and no price is drawn", async () => {
  const store = memory({ [KEY(BTC)]: JSON.stringify({ size: "10000", entry: 600_000, block: 100, fillAt: "1700000000", cash: "9000000000" }) });
  const t = make(node([]), store);
  t.observe({ asset: BTC, size: 10_000n, block: 90_000, fillAt: 1_700_050_000n, cash: 8_000_000_000n });
  await settled();
  assert.equal(t.entry(BTC, 10_000n), null);
  assert.equal(store.get(KEY(BTC)), null);
});

test("a record made before lastFillAt and cash were kept is not one", async () => {
  const n = node([fill(BTC, true, 5, 100, 4_990)]);
  const t = make(n, memory({ [KEY(BTC)]: JSON.stringify({ size: "5", entry: 100, block: 4_000 }) }));
  t.observe({ asset: BTC, size: 5n, block: 5_000, ...AT });
  await settled();
  assert.equal(n.calls.length, 1, "it is read, not trusted");
});
