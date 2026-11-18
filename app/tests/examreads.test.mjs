// node --test app/tests/examreads.test.mjs
//
// What one pass of the trading screen reads (app/lib/examreads.js) and what that costs the node, with the app's own
// gentle reader (app/lib/gentle.js, which carries reads in Multicall3 packs) in between and a node of the test's own
// at the other end. The "before" pass below is the one the screen made at f0ebccc: five reads of every market,
// every pass. It is kept here only to be measured against.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createGentle, GENTLE } from "../lib/gentle.js";
import { MULTICALL3, encodeAggregate3, decodeAggregate3 } from "../lib/multicall.js";
import { ORACLE_PX_PRECOMPILE, BBO_PRECOMPILE, bboOf } from "../lib/exam.js";
import { readPass, readList, fullyRead, withPrices, oracleOf, LIST_MS } from "../lib/examreads.js";

const EXAM = "0x70066669eC7Eb992055c82B615eD822CFF14e44A";
const word = (n) => BigInt.asUintN(256, BigInt(n)).toString(16).padStart(64, "0");
const SEL = { position: "aaaa0001", stop: "aaaa0002", take: "aaaa0003" };
const ACCOUNT_SEL = Array.from({ length: 16 }, (_, i) => `bbbb${String(i).padStart(4, "0")}`);
const STATIC = new Set(ACCOUNT_SEL.slice(14).map((s) => `0x${s}`));   // rules() and terms()

/** What the node's chain holds: the markets with a position (asset -> size), and a price for each asset. */
const chainOf = (held) => ({
  held,
  oracle: (asset) => 1_000_000n + BigInt(asset),
  bbo: (asset) => [1_000_000n + BigInt(asset) - 5n, 1_000_000n + BigInt(asset) + 5n],
});

/** The answer of the node to one call, as raw data. */
function answer(chain, to, data) {
  const bare = data.replace(/^0x/, "");
  const lower = to.toLowerCase();
  const arg = bare.length >= 72 ? Number(BigInt(`0x${bare.slice(8, 72)}`)) : 0;
  if (lower === ORACLE_PX_PRECOMPILE) return `0x${word(chain.oracle(Number(BigInt(`0x${bare}`))))}`;
  if (lower === BBO_PRECOMPILE) {
    const [bid, ask] = chain.bbo(Number(BigInt(`0x${bare}`)));
    return `0x${word(bid)}${word(ask)}`;
  }
  const sel = bare.slice(0, 8);
  if (sel === SEL.position) return `0x${word(chain.held.get(arg) ?? 0n)}`;
  if (sel === SEL.stop || sel === SEL.take) return `0x${word(chain.held.has(arg) ? 900_000n : 0n)}${word(0)}${word(0)}${word(0)}`;
  return `0x${word(1)}`;
}

/** The inner calls of an aggregate3 call's data: [{to, data}]. */
function inner(data) {
  const h = data.replace(/^0x/, "").slice(8);
  const at = (byte) => Number(BigInt(`0x${h.slice(byte * 2, byte * 2 + 64)}`));
  const count = at(32);
  const items = 64;
  return Array.from({ length: count }, (_, i) => {
    const tuple = items + at(items + i * 32);
    const to = `0x${h.slice((tuple + 12) * 2, (tuple + 32) * 2)}`;
    const length = at(tuple + 96);
    return { to, data: `0x${h.slice((tuple + 128) * 2, (tuple + 128 + length) * 2)}` };
  });
}

/** aggregate3's answer for `results` ([{success, data}]). */
function packed(results) {
  const tuples = results.map(({ success, data }) => {
    const d = data.replace(/^0x/, "");
    const bytes = d.length / 2;
    return word(success ? 1 : 0) + word(0x40) + word(bytes) + d.padEnd(Math.ceil(bytes / 32) * 64, "0");
  });
  let offset = results.length * 32;
  const heads = tuples.map((t) => {
    const head = word(offset);
    offset += t.length / 2;
    return head;
  });
  return `0x${word(0x20)}${word(results.length)}${heads.join("")}${tuples.join("")}`;
}

/**
 * A reader over a node of the test's own. `node.reads` counts the reads the node ran (inside a pack too), `node.bytes`
 * the bytes of the answers, `node.packs` the packs. `fail(call)` makes a read fail, in a pack or plain; `failInner(call)`
 * only inside a pack (the same read asked plain then answers); `refusePacks` makes the node revert a pack as a whole.
 */
function world({ held = new Map(), failInner = () => false, refusePacks = false, fail = () => false } = {}) {
  const chain = chainOf(held);
  const node = { reads: 0, bytes: 0, packs: 0, requests: 0, calls: 0 };
  const w = { clock: 0, node, chain, asked: 0 };
  w.reader = createGentle({
    now: () => w.clock, sleep: async () => {}, schedule: (fn) => setTimeout(fn, 2), staticSelectors: STATIC,
    options: { gatherMs: 0 }, pack: { address: MULTICALL3.address, check: async () => true },
    post: async (batch) => {
      node.requests++;
      const body = batch.map((c) => {
        node.calls++;
        if (c.method === "eth_getBlockByNumber") {
          node.reads++;
          return { jsonrpc: "2.0", id: c.id, result: { number: "0x64", timestamp: "0x6553f100" } };
        }
        const [tx] = c.params;
        if (tx.to.toLowerCase() === MULTICALL3.address.toLowerCase()) {
          node.packs++;
          if (refusePacks) return { jsonrpc: "2.0", id: c.id, error: { code: -32000, message: "execution reverted" } };
          const calls = inner(tx.data);
          const results = calls.map((x) => (failInner(x) || fail(x) ? { success: false, data: "0x" } : { success: true, data: answer(chain, x.to, x.data) }));
          node.reads += calls.length;
          const result = packed(results);
          node.bytes += result.length / 2 - 1;
          return { jsonrpc: "2.0", id: c.id, result };
        }
        node.reads++;
        if (fail(tx)) return { jsonrpc: "2.0", id: c.id, error: { code: -32000, message: "execution reverted" } };
        const result = answer(chain, tx.to, tx.data);
        node.bytes += result.length / 2 - 1;
        return { jsonrpc: "2.0", id: c.id, result };
      });
      return { status: 200, body };
    },
  });
  const ask = async (method, params) => {
    w.asked++;
    const a = await w.reader.request({ method, params });
    if (a.error) throw new Error(a.error.message);
    return a.result;
  };
  const call = (to, data) => ask("eth_call", [{ to, data }, "latest"]);
  w.io = {
    block: async () => {
      const b = await ask("eth_getBlockByNumber", ["latest", false]);
      return { number: Number(b.number), timestamp: Number(b.timestamp) };
    },
    account: () => Promise.all(ACCOUNT_SEL.map((s) => call(EXAM, `0x${s}`))),
    position: async (asset) => BigInt(await call(EXAM, `0x${SEL.position}${word(asset)}`)),
    stop: (asset) => call(EXAM, `0x${SEL.stop}${word(asset)}`),
    take: (asset) => call(EXAM, `0x${SEL.take}${word(asset)}`),
    oracle: (asset) => call(ORACLE_PX_PRECOMPILE, `0x${word(asset)}`),
    bbo: (asset) => call(BBO_PRECOMPILE, `0x${word(asset)}`),
  };
  return w;
}

/** The pass of f0ebccc: the account and the block, then five reads of every market. Same shape as readPass's answer. */
async function passBefore(io, { assets }) {
  const attempt = (fn) => Promise.resolve().then(fn).catch(() => null);
  const [block, got] = await Promise.all([io.block(), io.account()]);
  const perAsset = await Promise.all(assets.map(async (asset) => {
    const [size, stop, take, oracleHex, bboHex] = await Promise.all([
      io.position(asset), attempt(() => io.stop(asset)), attempt(() => io.take(asset)),
      attempt(() => io.oracle(asset)), attempt(() => io.bbo(asset)),
    ]);
    const bbo = bboOf(bboHex);
    return { asset, size, stop, take, oracle: oracleOf(oracleHex), bid: bbo?.bid ?? null, ask: bbo?.ask ?? null };
  }));
  return { block, got, perAsset };
}

/** Lets the reader ask whether packs may be used (the first read asks), so that the pass that follows rides in packs. */
async function warm(w) {
  await w.io.block();
  await new Promise((done) => setImmediate(done));
}

const assetsOf = (n) => Array.from({ length: n }, (_, i) => i + 1);
const strip = ({ read, ...row }) => row;

/** One pass in steady state: a first pass warms what is kept for good, then five seconds pass, then this one is measured. */
async function measure(make, n, held) {
  const w = world({ held });
  const assets = assetsOf(n);
  await make(w.io, { assets, selected: assets[0] });
  w.clock += 5_000;
  const before = { ...w.node, asked: w.asked, ...w.reader.stats() };
  const out = await make(w.io, { assets, selected: assets[0] });
  const after = { ...w.node, asked: w.asked, ...w.reader.stats() };
  const diff = (k) => after[k] - before[k];
  return {
    out, asked: diff("asked"), requests: diff("requests"), calls: diff("calls"), packs: diff("packs"), reads: diff("reads"),
    bytes: diff("bytes"),
  };
}

test("a pass reads the market on screen and the markets with a position, and the size of every market", async () => {
  const w = world({ held: new Map([[7, 3_000n], [9, -2_000n]]) });
  const assets = assetsOf(10);
  const seen = [];
  const io = Object.fromEntries(Object.entries(w.io).map(([k, f]) => [k, (...a) => { seen.push([k, a[0]]); return f(...a); }]));
  const out = await readPass(io, { assets, selected: 2 });
  const of = (k) => seen.filter(([x]) => x === k).map(([, a]) => a).sort((a, b) => a - b);
  assert.deepEqual(of("position"), assets, "the size of every market, so that no position is missed");
  for (const k of ["stop", "take", "oracle", "bbo"]) assert.deepEqual(of(k), [2, 7, 9], `${k}: the market on screen and the two held`);
  assert.deepEqual(out.perAsset.filter((a) => a.read).map((a) => a.asset), [2, 7, 9]);
  assert.deepEqual([...fullyRead(out.perAsset)], [2, 7, 9]);
  assert.equal(out.perAsset.length, 10, "a row for every market");
  assert.equal(out.perAsset[0].stop, null, "no stop is read where there is no position and the market is not on screen");
  assert.equal(out.block.number, 100);
});

test("the numbers of the markets read are the numbers the old pass read", async () => {
  const held = new Map([[7, 3_000n], [9, -2_000n]]);
  const assets = assetsOf(10);
  const before = await passBefore(world({ held }).io, { assets });
  const after = await readPass(world({ held }).io, { assets, selected: 2 });
  assert.deepEqual(after.got, before.got);
  assert.deepEqual(after.block, before.block);
  for (const row of after.perAsset.filter((a) => a.read)) assert.deepEqual(strip(row), before.perAsset.find((a) => a.asset === row.asset));
  // The markets not read in full keep their size, as before.
  for (const row of after.perAsset.filter((a) => !a.read)) assert.equal(row.size, before.perAsset.find((a) => a.asset === row.asset).size);
});

test("the market on screen is read even when it has no position, and a pass of an exam of no market reads only the account", async () => {
  const none = await readPass(world().io, { assets: [], selected: 1 });
  assert.deepEqual(none.perAsset, []);
  assert.equal(none.got.length, 16);
  const w = world();
  const out = await readPass(w.io, { assets: [5, 6], selected: 6 });
  assert.deepEqual(out.perAsset.filter((a) => a.read).map((a) => a.asset), [6]);
  assert.equal(out.perAsset[1].oracle, 1_000_006n);
  assert.deepEqual([...out.fresh], [[6, 1_000_006n]]);
  const kept = await readPass(w.io, { assets: [5, 6], selected: 6, prices: new Map([[5, 77n]]) });
  assert.equal(kept.perAsset[0].oracle, 77n, "a market not read carries the price kept for it");
});

test("a price, a stop or a take that cannot be read is null, as before; a size that cannot be read fails the pass", async () => {
  const bad = world({ held: new Map([[3, 100n]]), fail: (c) => c.to.toLowerCase() === ORACLE_PX_PRECOMPILE && BigInt(c.data) === 3n });
  const out = await readPass(bad.io, { assets: [1, 2, 3], selected: 1 });
  const row = out.perAsset.find((a) => a.asset === 3);
  assert.equal(row.oracle, null);
  assert.equal(row.size, 100n);
  assert.equal(row.read, true);
  assert.ok(out.perAsset.find((a) => a.asset === 1).oracle > 0n, "the other market is read all the same");
  const noSize = world({ fail: (c) => (c.data ?? "").startsWith(`0x${SEL.position}`) && BigInt(`0x${c.data.slice(10)}`) === 2n });
  await assert.rejects(() => readPass(noSize.io, { assets: [1, 2], selected: 1 }), /execution reverted/);
});

test("the prices of the list are read for the markets a pass does not read, and put over the ones kept", async () => {
  const w = world();
  const kept = new Map([[1, 5n], [2, 6n]]);
  const fresh = await readList(w.io, { assets: [1, 2, 3, 4], skip: new Set([1]) });
  assert.deepEqual([...fresh], [[2, 1_000_002n], [3, 1_000_003n], [4, 1_000_004n]], "only the markets asked");
  const out = withPrices(kept, fresh);
  assert.deepEqual([...out], [[1, 5n], [2, 1_000_002n], [3, 1_000_003n], [4, 1_000_004n]]);
  assert.deepEqual([...kept], [[1, 5n], [2, 6n]], "what was kept is not changed in place");
  const failing = world({ fail: (c) => c.to.toLowerCase() === ORACLE_PX_PRECOMPILE && BigInt(c.data) === 3n });
  const part = await readList(failing.io, { assets: [2, 3] });
  assert.equal(part.get(3), null, "a price that was not read is no price, not an old one");
  assert.equal(part.get(2), 1_000_002n);
  assert.equal(LIST_MS, 15_000);
});

test("a pack the node refuses is asked again read by read, and the pass brings the same", async () => {
  const held = new Map([[2, 5n]]);
  const assets = assetsOf(6);
  const good = await readPass(world({ held }).io, { assets, selected: 2 });
  const refused = world({ held, refusePacks: true });
  await warm(refused);
  const out = await readPass(refused.io, { assets, selected: 2 });
  assert.deepEqual(out, good);
  assert.ok(refused.node.packs >= 1 && refused.reader.stats().packFailures >= 1, "a pack was tried and failed");
  assert.equal(refused.reader.stats().packing, "off", "and no more are made after it");
  const alone = world({ held, failInner: (c) => c.data.startsWith(`0x${SEL.stop}`) });
  await warm(alone);
  const part = await readPass(alone.io, { assets, selected: 2 });
  // The failed read inside the pack is asked again plain, and the plain call answers: the same pass.
  assert.deepEqual(part, good);
});

test("what the screen asks of the node per pass, before and after, at 3, 10 and 100 markets", async () => {
  const table = [];
  for (const n of [3, 10, 100]) {
    // One position open, the screen on the first market; and (for the worst case) a position in every tenth market.
    for (const held of [new Map([[2, 4_000n]]), new Map(assetsOf(n).filter((a) => a % 10 === 0).map((a) => [a, 4_000n]))]) {
      const b = await measure(passBefore, n, held);
      const a = await measure(readPass, n, held);
      table.push({ n, held: held.size, before: { asked: b.asked, requests: b.requests, calls: b.calls, reads: b.reads, bytes: b.bytes },
        after: { asked: a.asked, requests: a.requests, calls: a.calls, reads: a.reads, bytes: a.bytes } });
      assert.ok(a.asked < b.asked || n === 3, `${n} markets: fewer reads asked`);
      assert.ok(a.requests <= b.requests && a.calls <= b.calls && a.reads <= b.reads && a.bytes <= b.bytes);
      assert.deepEqual(a.out.got, b.out.got);
    }
  }
  if (process.env.EXAMREADS_TABLE) console.log(table.map((r) => `${r.n} markets, ${r.held} held: before ${JSON.stringify(r.before)} | after ${JSON.stringify(r.after)}`).join("\n"));
  const at100 = table.find((r) => r.n === 100 && r.held === 1);
  // The block, the account's 14 getters that change, the 100 sizes, and the four reads of each of two markets (the one on
  // screen and the one held).
  assert.equal(at100.after.reads, 1 + 14 + 100 + 8, "100 markets, one position: no read of a market that is neither on screen nor held");
  assert.equal(at100.before.reads, 1 + 14 + 500, "and before it was five reads of every market");
  assert.ok(at100.after.calls <= GENTLE.maxBatch, "under the node's limit of a batch");
  assert.ok(at100.after.bytes * 4 < at100.before.bytes, "and under a quarter of the bytes");
});

test("the page reads through this module and asks the list's prices only while the list is open", () => {
  const page = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  assert.match(page, /readPass\(readerOf\(st\)/);
  assert.match(page, /readList\(readerOf\(st\)/);
  assert.doesNotMatch(page, /st\.assets\.map\(async \(asset\)/, "no pass reads five things of every market");
  assert.match(page, /if \(!st\.market\.open \|\| st\.listReading === session\) return;/);
  assert.match(page, /refreshList\(page, st\)\.catch/);
  assert.match(page, /selected !== st\.asset\) return tick\(page, st\)/, "a market chosen during a reading is read again");
  assert.match(page, /if \(seq !== st\.tickSeq\) return undefined;/, "only the latest pass draws");
  assert.match(page, /if \(st\.market\.open && session === st\.listSession\) \{\s*st\.prices = withPrices\(st\.prices, fresh\);/, "a list closed, or closed and opened again, during its reading keeps nothing of it");
  assert.match(page, /st\.listSession \+= 1;/, "every opening and closing is a new session");
  assert.match(page, /st\.prices = st\.market\.open \? withPrices\(st\.prices, snap\.fresh\) : snap\.fresh;/, "with the list closed only what the pass read is kept");
});

test("encode and decode of a pack still agree with the test's node", () => {
  const calls = [{ to: EXAM, data: `0x${SEL.position}${word(3)}` }, { to: ORACLE_PX_PRECOMPILE, data: `0x${word(4)}` }];
  assert.deepEqual(inner(encodeAggregate3(calls)).map((c) => c.data), calls.map((c) => c.data));
  assert.deepEqual(decodeAggregate3(packed([{ success: true, data: "0x01" }, { success: false, data: "0x" }]), 2),
    [{ success: true, data: "0x01" }, { success: false, data: "0x" }]);
});
