// node --test app/tests/gentle.test.mjs
//
// The gentle reader under the app's read provider (app/lib/gentle.js, app/lib/chain.js, app/app.js).
//
// 12 Oct 2026: a visitor with two tabs of the site and a wallet open got "Could not load this page" on the home page.
// Measured that day against the public HyperEVM RPC: the home page asked 158 calls in 41 HTTP requests, 50 of them
// repeats; the node refuses for the rate with HTTP 200 and ONE object for the whole batch,
// {"jsonrpc":"2.0","id":null,"error":{"code":-32005,"message":"rate limited"}}, no Retry-After; after a burst it
// went on refusing a single call for about 45 seconds. The replies below are that node's, as it gave them.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  GENTLE, READS, RATE_LIMITED, keyOf, refusedForRate, neverChanges, createGentle, waitWords,
} from "../lib/gentle.js";
import { MAX_BATCH } from "../lib/batch.js";
import { PAGE_LEFT } from "../lib/gentle.js";
import { MULTICALL3, packable, encodeAggregate3, decodeAggregate3 } from "../lib/multicall.js";
import { createNavigator, scrollTarget, LOADING } from "../lib/nav.js";
import { LIST_KEPT, readAgo } from "../lib/listing.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

/** What the node sent in place of a batch it refused for the rate. */
const REFUSAL = { status: 200, body: { jsonrpc: "2.0", id: null, error: { code: -32005, message: "rate limited" } } };
const RULES = "0x52f6747a";     // rules(), fixed when a pool is set up
const STAGE = "0xc040e6b8";     // stage(), which changes
const POOL = "0x70066669eC7Eb992055c82B615eD822CFF14e44A";
const call = (data, to = POOL) => ({ method: "eth_call", params: [{ to, data }, "latest"] });

/** A node that answers every call with its own data, unless `reply(n, batch)` says otherwise. */
const echo = (batch) => ({ status: 200, body: batch.map((c) => ({ jsonrpc: "2.0", id: c.id, result: `0x${String(c.params[0]?.data ?? c.method).replace(/^0x/, "")}aa` })) });

/**
 * A reader on a clock of the test's own, with every post and every wait written down. A sleep ends only when the
 * clock is moved to it, and the clock moves to the next sleeper by itself whenever nothing else can run: so what
 * happens during a wait happens at the time it would, not after it, and a wait nobody expected cannot hang a test.
 */
function world({ reply = () => null, options = {}, store = null, staticSelectors = new Set([RULES]), pack = null } = {}) {
  const w = { t: 0, posts: [], waits: [], sleeps: [], sleepers: [], out: 0, most: 0, armed: false };
  const arm = () => {
    if (w.armed || !w.sleepers.length) return;
    w.armed = true;
    // After every microtask that is due: the posts and their answers of this moment are through.
    setImmediate(() => {
      w.armed = false;
      w.sleepers.sort((a, b) => a.at - b.at);
      const next = w.sleepers.shift();
      w.t = Math.max(w.t, next.at);
      next.wake();
      arm();
    });
  };
  w.go = (promise) => promise;
  w.reader = createGentle({
    now: () => w.t,
    sleep: (ms) => new Promise((wake) => {
      w.sleeps.push(ms);
      w.sleepers.push({ at: w.t + ms, wake });
      arm();
    }),
    schedule: (fn) => queueMicrotask(fn),
    onWait: (wait) => w.waits.push(wait),
    staticSelectors, store, options, pack,
    post: async (batch) => {
      w.out++;
      w.most = Math.max(w.most, w.out);
      w.posts.push({ at: w.t, batch });
      // This post's own number, taken before anything else is sent: the nth request the node saw.
      const n = w.posts.length;
      await Promise.resolve();
      const said = await reply(n, batch);
      w.out--;
      if (said instanceof Error) throw said;
      return said || echo(batch);
    },
  });
  return w;
}

/** A sessionStorage-like thing. */
function memoryStore() {
  const kept = new Map();
  return { kept, getItem: (k) => (kept.has(k) ? kept.get(k) : null), setItem: (k, v) => kept.set(k, String(v)) };
}

test("the same call asked twice is asked once, while it is out and for a short time after", async () => {
  const w = world();
  const [a, b, other] = await Promise.all([w.reader.request(call(STAGE)), w.reader.request(call(STAGE)), w.reader.request(call("0xd2ef7398"))]);
  assert.deepEqual(a, { result: "0xc040e6b8aa" });
  assert.deepEqual(b, a);
  assert.deepEqual(other, { result: "0xd2ef7398aa" });
  assert.equal(w.posts.length, 1);
  assert.equal(w.posts[0].batch.length, 2, "two different calls, the repeated one sent once");
  // Each asker has an answer of its own: one that changes its copy does not change another's.
  a.result = "changed";
  assert.equal(b.result, "0xc040e6b8aa");
  // Inside the short time it is not asked again; at its end it is.
  w.t = GENTLE.liveMs - 1;
  assert.deepEqual(await w.reader.request(call(STAGE)), { result: "0xc040e6b8aa" });
  assert.equal(w.posts.length, 1);
  w.t = GENTLE.liveMs;
  await w.reader.request(call(STAGE));
  assert.equal(w.posts.length, 2);
  // The same view of another contract is another call.
  await w.reader.request(call(STAGE, "0xf40da2862f1e3f20ab7d1ed7b5e67a83d333cb3b"));
  assert.equal(w.posts.length, 3);
  assert.equal(keyOf(call(STAGE)), `eth_call:[{"to":"${POOL}","data":"${STAGE}"},"latest"]`);
  const s = w.reader.stats();
  assert.equal(s.shared, 1);
  assert.equal(s.cached, 1);
  assert.equal(s.calls, 4);
});

test("what can change is kept a few seconds, and what a contract never changes is kept for the session", async () => {
  // A verdict or a stage read through this is shown to a visitor as now: a few seconds and no more.
  assert.ok(GENTLE.liveMs >= 1_000 && GENTLE.liveMs <= 5_000);
  assert.ok(GENTLE.staticMs >= 10 * 60_000);
  const store = memoryStore();
  const w = world({ store });
  await w.reader.request(call(RULES));
  await w.reader.request(call(STAGE));
  assert.equal(w.posts.length, 2);
  w.t = GENTLE.liveMs + 1;
  await w.reader.request(call(RULES));
  assert.equal(w.posts.length, 2, "the rules are not asked again");
  await w.reader.request(call(STAGE));
  assert.equal(w.posts.length, 3, "the stage is");
  w.t = GENTLE.staticMs - 1;
  await w.reader.request(call(RULES));
  assert.equal(w.posts.length, 3);
  w.t = GENTLE.staticMs;
  await w.reader.request(call(RULES));
  assert.equal(w.posts.length, 4, "kept for the session, not for ever");

  // Only what never changes goes to the store, and another page of the same tab reads it from there.
  assert.equal(store.kept.size, 1);
  assert.ok([...store.kept.keys()][0].startsWith("pools-rpc-v1:eth_call:"));
  const later = world({ store });
  later.t = GENTLE.staticMs + 5;
  assert.deepEqual(await later.reader.request(call(RULES)), { result: "0x52f6747aaa" });
  assert.equal(later.posts.length, 0);
  later.t = 2 * GENTLE.staticMs + 5;
  await later.reader.request(call(RULES));
  assert.equal(later.posts.length, 1, "what the store holds grows old too");
  // A store that throws is a store that is not there.
  const broken = world({ store: { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("full"); } } });
  assert.deepEqual(await broken.reader.request(call(RULES)), { result: "0x52f6747aaa" });
  assert.equal(broken.posts.length, 1);

  const fixed = new Set([RULES]);
  assert.equal(neverChanges(call(RULES), "0x01", fixed), true);
  assert.equal(neverChanges(call(STAGE), "0x01", fixed), false);
  assert.equal(neverChanges(call(RULES.toUpperCase().replace("0X", "0x")), "0x01", fixed), true);
  // An empty answer may yet become something, and a reading of an older block is not the page's "now".
  assert.equal(neverChanges(call(RULES), "0x", fixed), false);
  assert.equal(neverChanges({ method: "eth_call", params: [{ to: POOL, data: RULES }, "0x10"] }, "0x01", fixed), false);
  assert.equal(neverChanges({ method: "eth_getCode", params: [POOL, "latest"] }, "0x6080"), true);
  assert.equal(neverChanges({ method: "eth_getCode", params: [POOL, "latest"] }, "0x"), false);
  assert.equal(neverChanges({ method: "eth_getTransactionReceipt", params: ["0x01"] }, { blockNumber: "0x5" }), true);
  assert.equal(neverChanges({ method: "eth_getTransactionReceipt", params: ["0x01"] }, null), false);
  assert.equal(neverChanges({ method: "eth_getBlockByNumber", params: ["latest", false] }, { timestamp: "0x1" }), false);
  assert.equal(neverChanges({ method: "eth_chainId", params: [] }, "0x3e6"), true);
});

test("calls are gathered into batches of at most twenty, and at most two batches are out at a time", async () => {
  assert.equal(GENTLE.maxBatch, MAX_BATCH);
  assert.ok(GENTLE.maxInFlight >= 1 && GENTLE.maxInFlight <= 2);
  // The node may answer a batch in any order; each asker gets its own answer by the id.
  const w = world({ reply: (n, batch) => ({ status: 200, body: [...echo(batch).body].reverse() }) });
  const selectors = Array.from({ length: 50 }, (_, i) => `0x${(0x1000 + i).toString(16).padStart(8, "0")}`);
  const answers = await Promise.all(selectors.map((s) => w.reader.request(call(s))));
  assert.deepEqual(w.posts.map((p) => p.batch.length), [20, 20, 10]);
  assert.equal(w.most, 2);
  answers.forEach((a, i) => assert.deepEqual(a, { result: `${selectors[i]}aa` }));
  for (const p of w.posts) assert.deepEqual(p.batch.map((c) => c.id), p.batch.map((_, i) => i + 1));
  assert.ok(w.posts[0].batch.every((c) => c.jsonrpc === "2.0"));
  // With one batch out at a time they go one after another.
  const single = world({ options: { maxInFlight: 1 } });
  await Promise.all(selectors.map((s) => single.reader.request(call(s))));
  assert.equal(single.most, 1);
  assert.equal(single.posts.length, 3);
  // A call the node leaves out of its answer fails alone, and says so.
  const short = world({ reply: (n, batch) => ({ status: 200, body: echo(batch).body.slice(1) }) });
  const [lost, found] = await Promise.all([short.reader.request(call(STAGE)), short.reader.request(call(RULES))]);
  assert.match(lost.error.message, /left this call out/);
  assert.deepEqual(found, { result: "0x52f6747aaa" });
});

test("a refusal for the rate is waited out and asked again, and is not an error", async () => {
  // The node's own refusal: HTTP 200, one object, no id.
  assert.equal(refusedForRate(200, REFUSAL.body), true);
  assert.equal(refusedForRate(429, null), true);
  assert.equal(refusedForRate(200, [{ id: 1, result: "0x" }, { id: 2, error: { code: -32005, message: "rate limited" } }]), true);
  assert.equal(refusedForRate(200, { error: { code: -32000, message: "Too Many Requests" } }), true);
  // Not a refusal for the rate: a batch too large, a revert, an answer.
  assert.equal(refusedForRate(200, { id: null, error: { code: -32010, message: "Exceeded max limit of 20" } }), false);
  assert.equal(refusedForRate(200, [{ id: 1, error: { code: 3, message: "execution reverted", data: "0x" } }]), false);
  assert.equal(refusedForRate(200, [{ id: 1, result: "0x01" }]), false);
  assert.equal(refusedForRate(200, null), false);
  assert.deepEqual(RATE_LIMITED, { code: -32005, message: "rate limited" });

  for (const refusal of [REFUSAL, { status: 429, body: null }]) {
    const w = world({ reply: (n) => (n <= 2 ? refusal : null) });
    const answer = await w.go(w.reader.request(call(STAGE)));
    assert.deepEqual(answer, { result: "0xc040e6b8aa" });
    assert.equal(w.posts.length, 3);
    assert.deepEqual(w.posts.map((p) => p.at), [0, 1_000, 3_000]);
    assert.deepEqual(w.sleeps, [1_000, 2_000]);
    assert.deepEqual(w.waits, [
      { attempt: 1, of: 6, delayMs: 1_000, calls: 1 },
      { attempt: 2, of: 6, delayMs: 2_000, calls: 1 },
      null,
    ]);
    assert.equal(w.reader.stats().waits, 2);
    assert.equal(w.reader.stats().gaveUp, 0);
  }
  // Each wait is longer than the one before, and together they outlast the 45 seconds the node held a refusal.
  const delays = [...GENTLE.retryDelaysMs];
  assert.deepEqual(delays, [...delays].sort((a, b) => a - b));
  assert.equal(new Set(delays).size, delays.length);
  const sum = delays.reduce((a, b) => a + b, 0);
  assert.ok(sum >= 45_000 && sum <= 90_000, `${sum} ms in all`);
  assert.ok(delays[0] >= 500);
});

test("a refusal met by one batch holds every batch", async () => {
  // Thirty calls: two batches out together. The first is refused once; the second is refused too, and then both
  // wait. No batch is sent inside a wait: asking through it only earns another refusal.
  const w = world({ reply: (n) => (n <= 2 ? REFUSAL : null) });
  const selectors = Array.from({ length: 30 }, (_, i) => `0x${(0x2000 + i).toString(16).padStart(8, "0")}`);
  const answers = await w.go(Promise.all(selectors.map((s) => w.reader.request(call(s)))));
  assert.ok(answers.every((a) => a.result));
  assert.equal(w.posts.length, 4);
  assert.deepEqual(w.posts.slice(0, 2).map((p) => p.at), [0, 0]);
  for (const p of w.posts.slice(2)) assert.ok(p.at >= 1_000, `sent again at ${p.at} ms`);
  // The page is told once that the reads go again, when the last waiting batch is through.
  assert.equal(w.waits.filter((x) => x === null).length, 1);
  assert.equal(w.waits[w.waits.length - 1], null);

  // A batch that was still in the queue waits too. Fifty calls: two batches go out, the first is refused and the
  // second answered; the third leaves the queue the moment the second is back, and must not be sent inside the wait.
  const q = world({ reply: (n) => (n === 1 ? REFUSAL : null) });
  const fifty = Array.from({ length: 50 }, (_, i) => `0x${(0x3000 + i).toString(16).padStart(8, "0")}`);
  await q.go(Promise.all(fifty.map((s) => q.reader.request(call(s)))));
  assert.equal(q.posts.length, 4);
  assert.deepEqual(q.posts.slice(0, 2).map((p) => p.at), [0, 0]);
  assert.deepEqual(q.posts.slice(2).map((p) => p.at), [1_000, 1_000], "the queued batch and the refused one both go after the wait");
  assert.deepEqual(q.posts.map((p) => p.batch.length).sort((a, b) => a - b), [10, 20, 20, 20]);
});

test("only when the last wait is spent do the calls fail, with the node's own refusal", async () => {
  const w = world({ reply: () => REFUSAL });
  const [a, b] = await w.go(Promise.all([w.reader.request(call(STAGE)), w.reader.request(call(RULES))]));
  assert.deepEqual(a, { error: { code: -32005, message: "rate limited" } });
  assert.deepEqual(b, a);
  // Asked once and then once after each wait: seven times, a minute apart from first to last.
  assert.equal(w.posts.length, GENTLE.retryDelaysMs.length + 1);
  assert.deepEqual(w.sleeps, [...GENTLE.retryDelaysMs]);
  assert.equal(w.t, 60_000);
  assert.equal(w.waits.filter(Boolean).length, 6);
  assert.equal(w.waits[w.waits.length - 1], null);
  assert.equal(w.reader.stats().gaveUp, 2);
  // A refusal is not an answer: nothing is kept, and the next asking goes to the node.
  const before = w.posts.length;
  w.reader.request(call(STAGE));
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(w.posts.length > before);
});

test("an error is the call's own, is not waited on and is not kept", async () => {
  // A revert inside a batch: that call fails, the others are answered.
  const revert = { code: 3, message: "execution reverted", data: "0x12345678" };
  const w = world({ reply: (n, batch) => ({ status: 200, body: batch.map((c) => (c.params[0].data === STAGE ? { id: c.id, error: revert } : { id: c.id, result: "0x01" })) }) });
  const [bad, good] = await Promise.all([w.reader.request(call(STAGE)), w.reader.request(call(RULES))]);
  assert.deepEqual(bad, { error: revert });
  assert.deepEqual(good, { result: "0x01" });
  assert.deepEqual(w.sleeps, []);
  await w.reader.request(call(STAGE));
  assert.equal(w.posts.length, 2, "the revert was not kept");
  // The node refusing the batch as a whole for another reason: every call gets that reason, at once.
  const tooLarge = { code: -32010, message: "Exceeded max limit of 20" };
  const refused = world({ reply: () => ({ status: 200, body: { jsonrpc: "2.0", id: null, error: tooLarge } }) });
  assert.deepEqual(await refused.reader.request(call(STAGE)), { error: tooLarge });
  assert.equal(refused.posts.length, 1);
  assert.deepEqual(refused.sleeps, []);
});

test("a request that never got an answer is sent again, then fails the way fetch fails", async () => {
  const down = new TypeError("Failed to fetch");
  const once = world({ reply: (n) => (n <= 2 ? down : null) });
  assert.deepEqual(await once.go(once.reader.request(call(STAGE))), { result: "0xc040e6b8aa" });
  assert.equal(once.posts.length, 3);
  assert.deepEqual(once.sleeps, [GENTLE.networkDelayMs, 2 * GENTLE.networkDelayMs]);
  assert.deepEqual(once.waits, [], "not a rate limit: the page is not told to wait for one");

  const never = world({ reply: () => down });
  const lost = await never.go(never.reader.request(call(STAGE)));
  assert.equal(lost.thrown, down);
  assert.equal(never.posts.length, GENTLE.networkRetries + 1);
  // A gateway in front of the node answering 502 is the same; a 4xx that is not JSON is not worth asking again.
  const gateway = world({ reply: () => ({ status: 502, body: null }) });
  const bad = await gateway.go(gateway.reader.request(call(STAGE)));
  assert.equal(bad.thrown.status, 502);
  assert.equal(gateway.posts.length, GENTLE.networkRetries + 1);
  const page = world({ reply: () => ({ status: 404, body: null }) });
  assert.equal((await page.reader.request(call(STAGE))).thrown.status, 404);
  assert.equal(page.posts.length, 1);
});

test("after a transaction of the visitor's own, what can change is read again", async () => {
  const w = world();
  await Promise.all([w.reader.request(call(STAGE)), w.reader.request(call(RULES))]);
  w.reader.forgetLive();
  await Promise.all([w.reader.request(call(STAGE)), w.reader.request(call(RULES))]);
  assert.equal(w.posts.length, 2);
  assert.deepEqual(w.posts[1].batch.map((c) => c.params[0].data), [STAGE], "the stage is read again, the rules are not");
  // What is not a read is never shared and never kept.
  assert.equal(READS.has("eth_sendRawTransaction"), false);
  assert.equal(READS.has("eth_call"), true);
  const send = { method: "eth_sendRawTransaction", params: ["0xf86b"] };
  await Promise.all([w.reader.request(send), w.reader.request(send)]);
  assert.equal(w.posts[w.posts.length - 1].batch.filter((c) => c.method === "eth_sendRawTransaction").length, 2);
});

test("a read that was out when the visitor's transaction landed is not joined and not kept", async () => {
  // The first reading of the stage is still out when the transaction is mined. It answers with the stage from
  // before; the page, drawn again after the transaction, must not be given that.
  const turn = () => new Promise((resolve) => setImmediate(resolve));
  // The node holds the first two requests until the test lets each go, and answers any later one at once: a request
  // the reader should not have made then shows as a count that is wrong, not as a test that never ends.
  const held = (gates, upTo) => async (n, batch) => {
    if (n <= upTo) await new Promise((open) => gates.push(open));
    return { status: 200, body: batch.map((c) => ({ jsonrpc: "2.0", id: c.id, result: `0x0${n}` })) };
  };
  const gates = [];
  const w = world({ reply: held(gates, 2) });
  const before = w.reader.request(call(STAGE));
  await turn();
  assert.equal(w.posts.length, 1);
  w.reader.forgetLive();
  const after = w.reader.request(call(STAGE));
  await turn();
  assert.equal(w.posts.length, 2, "asked again, not joined to the reading from before the transaction");
  // The old reading comes back first. It goes to who asked for it, and takes nothing of the new one's with it:
  // a third asking, while the second is still out, joins the second.
  gates[0]();
  assert.deepEqual(await before, { result: "0x01" });
  const third = w.reader.request(call(STAGE));
  await turn();
  assert.equal(w.posts.length, 2, "the old answer's return did not unseat the reading that is out");
  gates[1]();
  assert.deepEqual(await after, { result: "0x02" });
  assert.deepEqual(await third, { result: "0x02" });
  // What is kept is the reading made after the transaction, whichever came back last.
  assert.deepEqual(await w.reader.request(call(STAGE)), { result: "0x02" });
  assert.equal(w.posts.length, 2);

  // The same order the other way round: the new reading back first, the old one last. The old one is not kept.
  const late = [];
  const v = world({ reply: held(late, 2) });
  const old = v.reader.request(call(STAGE));
  await turn();
  v.reader.forgetLive();
  const fresh = v.reader.request(call(STAGE));
  await turn();
  late[1]();
  assert.deepEqual(await fresh, { result: "0x02" });
  late[0]();
  assert.deepEqual(await old, { result: "0x01" });
  assert.deepEqual(await v.reader.request(call(STAGE)), { result: "0x02" });
  assert.equal(v.posts.length, 2);

  // What a contract never changes is the same on both sides of any transaction: out or not, it is kept.
  const fixed = [];
  const r = world({ reply: held(fixed, 1) });
  const rules = r.reader.request(call(RULES));
  await turn();
  r.reader.forgetLive();
  fixed[0]();
  assert.deepEqual(await rules, { result: "0x01" });
  assert.deepEqual(await r.reader.request(call(RULES)), { result: "0x01" });
  assert.equal(r.posts.length, 1);
});

test("the read provider is the gentle one, and nothing a rule is judged by is kept for the session", () => {
  const chain = text("../lib/chain.js");
  assert.match(chain, /class GentleProvider extends ethers\.JsonRpcProvider \{\s+constructor\(reader\) \{\s+super\(CONFIG\.rpc, CONFIG\.chainId, PROVIDER_OPTIONS\);\s+this\.reader = reader;\s+\}\s+async _send\(payload\) \{/);
  assert.match(chain, /const answers = await Promise\.all\(list\.map\(\(p\) => this\.reader\.request\(\{ method: p\.method, params: p\.params \}\)\)\);/);
  assert.match(chain, /const lost = answers\.find\(\(a\) => a\.thrown\);\s+if \(lost\) throw lost\.thrown;/);
  // Two providers, both gentle: the pages' own, whose reads may ride in packs, and the checking pages', whose
  // reads never do.
  assert.match(chain, /export const readProvider = new GentleProvider\(gentle\);/);
  assert.match(chain, /export const checkProvider = new GentleProvider\(plain\);/);
  assert.equal((chain.match(/new GentleProvider\(/g) || []).length, 2);
  assert.doesNotMatch(chain, /new ethers\.JsonRpcProvider\(/);
  // What is kept for the session is named, and is only what the contracts fix when they are set up.
  const fixed = JSON.parse(chain.match(/export const FIXED_VIEWS = Object\.freeze\((\[[^\]]*\])\);/)[1].replace(/,\s*\]/, "]"));
  assert.deepEqual(fixed, ["rules()", "terms()", "owner()", "trader()", "pool()", "createdAt()", "factory()", "feeBps()", "minDeposit()", "lock()"]);
  for (const live of ["stage", "status", "violation", "breachReason", "isStopped", "challenge", "accountReady", "capitalNeeded",
    "deadline", "drawdownBase", "dayStartEquity", "balanceOf", "isPool", "isChallenge", "value", "seatsSealed", "freeCount"]) {
    assert.ok(!fixed.some((f) => f.startsWith(`${live}(`)), `${live}() is read again every time`);
  }
  // Each is set once in the contracts: assigned in an initializer or a constructor, or declared immutable.
  const pool = text("../../src/Pool.sol");
  const account = text("../../src/ChallengeAccount.sol");
  const shared = text("../../src/shared/SharedPool.sol");
  assert.match(pool, /owner = owner_;/);
  assert.equal((pool.match(/\bowner = /g) || []).length, 1, "a pool's owner is assigned once");
  assert.equal((pool.match(/_terms = /g) || []).length, 1);
  assert.equal((account.match(/_terms = /g) || []).length, 1);
  assert.equal((account.match(/\btrader = /g) || []).length, 1);
  assert.equal((account.match(/createdAt = /g) || []).length, 1);
  for (const name of ["factory", "minDeposit", "lock", "feeBps"]) assert.match(shared, new RegExp(`public immutable ${name};`), name);
  // A write through the wallet forgets what can change, on both paths the app writes by.
  assert.match(chain, /const receipt = await tx\.wait\(\);\s+forgetLiveReads\(\);/);
  assert.match(text("../views/shared.js"), /const receipt = await tx\.wait\(\);\s+chain\.forgetLiveReads\(\);/);
  assert.match(chain, /store\.getItem\("pools-rpc-v1:probe"\);/);
});

test("the home page reads a few pools at a time, and asks an archived pool nothing about a sale", () => {
  const pools = text("../views/pools.js");
  const atOnce = Number(pools.match(/export const POOLS_AT_ONCE = (\d+);/)[1]);
  // Seven reads a pool, riding in packs of fifty: a round has to fit the calls of one request, and be more than one
  // pool. Before packs it had to fit two batches of twenty reads, which was five pools.
  const packsARound = Math.ceil((atOnce * 7) / GENTLE.maxPack);
  assert.ok(atOnce >= 2 && packsARound <= MAX_BATCH, `${atOnce} pools at a time are ${packsARound} packs`);
  assert.ok(atOnce * 7 > GENTLE.maxInFlight * MAX_BATCH, "more pools at a time than fitted without packs");
  assert.match(pools, /const fromChain = await chain\.readAll\(\[\.\.\.addresses\.entries\(\)\],\s+\(\[index, address\]\) => readPoolChain\(address, index, deployment\), POOLS_AT_ONCE\);/);
  assert.doesNotMatch(pools, /await readPoolChain\(address/, "a pool at a time is a request for every pool");
  // Both deployments are read at the same time, and the list is in their order, live first.
  assert.match(pools, /const parts = await Promise\.all\(DEPLOYMENTS\.filter\(\(d\) => chain\.deployed\(d\)\)\.map\(async \(deployment\) => \{/);
  assert.match(pools, /return \{ items: parts\.flatMap\(\(part\) => part\.items\),/);
  // The three readings a sale is decided by are asked of a pool that can sell, and of no other.
  assert.match(pools, /const sale = archived \? \[null, null, 0n\] : \[pool\.accountReady\(\), pool\.challenge\(\), pool\.capitalNeeded\(\)\];/);
  assert.match(pools, /rulesAndTerms\(pool\), pool\.stage\(\), owned,\s+owned\.then\(\(address\) => holdsCode\(\(a\) => chain\.readProvider\.getCode\(a\), address\)\), \.\.\.sale,/);
  assert.match(pools, /const blocker = item\.archived \? \{ kind: "archived" \}\s+: saleBlocker\(\{ stage: item\.stage, ready: item\.ready, challenge: item\.challenge, spot: spotUsdc,\s+needed: Number\(item\.neededSpot\) \/ 1e8 \}\);/);
  assert.ok(pools.indexOf("const archived = isArchive(deployment);") < pools.indexOf("const sale = archived ?"));
});

test("while the reads wait the page says so, and a page that still fails offers to try again", () => {
  assert.equal(waitWords(null), "");
  assert.equal(waitWords({ attempt: 3, of: 6, delayMs: 4_000, calls: 20 }),
    "The public HyperEVM RPC is limiting requests from this browser. Waiting 4 s before asking again (try 3 of 6).");
  assert.match(waitWords({ attempt: 1, of: 6, delayMs: 400, calls: 1 }), /Waiting 1 s/);
  const app = text("../app.js");
  assert.match(app, /const box = \$\("#rpc-wait"\);\s+if \(!box\) return;\s+chain\.onRpcWait\(\(wait\) => \{\s+box\.textContent = waitWords\(wait\);\s+box\.hidden = !wait;\s+\}\);/);
  assert.match(app, /walletButton\(\);\s+rpcWaitNotice\(\);/);
  assert.match(app, /<p><button id="try-again">Try again<\/button><\/p><\/section>`\);\s+\$\("#try-again", page\)\.addEventListener\("click", route\);/);
  assert.match(text("../index.html"), /<div class="notice" id="rpc-wait" role="status" hidden><\/div>/);
});

// ---- 13 Oct 2026: reads in packs, pages that are left, and the way a page opens ----
//
// Measured that day: the home page asked the node 91 calls in 12 requests one after another and took 6.3 seconds;
// after a click the screen was empty for up to 2.0 seconds; a page opened where the one before it had been
// scrolled to; and a home page left after 0.4 seconds still asked for its 44 calls. Twelve calls of Multicall3
// with fifty reads each were answered with no refusal, so the node counts the call and not what it carries.

// aggregate3 for three calls, and an answer for three, as eth_abi 5 encodes them (not this app's encoder).
const POOL2 = "0xa2eEe2CF75d5f740E436a08007345dA5789a4499";
const BALANCE = "0x70a08231000000000000000000000000e3fef7523607aef26b3bfc06d414d59525b10dbb";
const VECTOR_IN = "0x82ad56cb000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000030000000000000000000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000001a000000000000000000000000070066669ec7eb992055c82b615ed822cff14e44a000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000600000000000000000000000000000000000000000000000000000000000000004c040e6b800000000000000000000000000000000000000000000000000000000000000000000000000000000a2eee2cf75d5f740e436a08007345da5789a449900000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000000000000000452f6747a0000000000000000000000000000000000000000000000000000000000000000000000000000000070066669ec7eb992055c82b615ed822cff14e44a00000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000000000000002470a08231000000000000000000000000e3fef7523607aef26b3bfc06d414d59525b10dbb00000000000000000000000000000000000000000000000000000000";
const VECTOR_OUT = "0x00000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000003000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000e00000000000000000000000000000000000000000000000000000000000000160000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000400000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000040000000000000000000000000000000000000000000000000000000000000000708c379a001020300000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000400000000000000000000000000000000000000000000000000000000000000000";

// The test's own reading and writing of a pack, so the node below is not built from the code it tests.
const word = (n) => n.toString(16).padStart(64, "0");
const wordAt = (h, byte) => parseInt(h.slice(byte * 2 + 40, byte * 2 + 64), 16);
function unpack(data) {
  const h = data.slice(10);
  const base = wordAt(h, 0) + 32;
  return Array.from({ length: wordAt(h, base - 32) }, (_, i) => {
    const tuple = base + wordAt(h, base + 32 * i);
    const bytes = tuple + wordAt(h, tuple + 64);
    return { to: `0x${h.slice(tuple * 2 + 24, tuple * 2 + 64)}`, data: `0x${h.slice((bytes + 32) * 2, (bytes + 32 + wordAt(h, bytes)) * 2)}` };
  });
}
function packed(results) {
  const tuples = results.map(({ ok, data }) => {
    const d = data.slice(2);
    return word(ok ? 1 : 0) + word(0x40) + word(d.length / 2) + d.padEnd(Math.ceil(d.length / 64) * 64, "0");
  });
  let offset = results.length * 32;
  const heads = tuples.map((t) => {
    const head = word(offset);
    offset += t.length / 2;
    return head;
  });
  return `0x${word(0x20)}${word(results.length)}${heads.join("")}${tuples.join("")}`;
}
const isPack = (c) => c.method === "eth_call" && String(c.params[0]?.to).toLowerCase() === MULTICALL3.address.toLowerCase();
const said = (data) => `0x${String(data).replace(/^0x/, "")}aa`;   // what the node says to a read, alone or in a pack

/**
 * A node with the pack contract on it. `failing` reads revert, in a pack and alone. `packs(c, n)` may answer a pack
 * its own way. It ends a test that keeps asking: after `most` requests it stops answering.
 */
const chainNode = ({ failing = new Set(), packs = () => null, most = 12 } = {}) => (n, batch) => {
  if (n > most) return new Error("the test's node was asked too many times");
  return { status: 200, body: batch.map((c) => {
    if (isPack(c)) {
      return packs(c, n) || { jsonrpc: "2.0", id: c.id, result: packed(unpack(c.params[0].data).map((sub) => (failing.has(sub.data)
        ? { ok: false, data: "0x08c379a0" } : { ok: true, data: said(sub.data) }))) };
    }
    if (failing.has(c.params[0]?.data)) return { jsonrpc: "2.0", id: c.id, error: { code: 3, message: "execution reverted" } };
    return { jsonrpc: "2.0", id: c.id, result: said(c.params[0]?.data ?? c.method) };
  }) };
};
const YES = { address: MULTICALL3.address, check: async () => true };
const sel = (i) => `0x${(0xa0000000 + i).toString(16)}`;
/** Whether packs may be made is asked with the first read: this is that read, answered, and the check with it. */
async function opened(w) {
  await w.reader.request(call("0x00000001"));
  await new Promise((r) => setImmediate(r));
  return w.posts.length;
}

test("a pack is written and read as Multicall3's aggregate3, and nothing is guessed from a broken answer", () => {
  assert.equal(MULTICALL3.address, "0xcA11bde05977b3631167028862bE2a173976CA11");
  assert.equal(MULTICALL3.codeHash, "0xd5c15df687b16f2ff992fc8d767b4216323184a2bbc6ee2f9c398c318e770891");
  const calls = [{ to: POOL, data: STAGE }, { to: POOL2, data: RULES }, { to: POOL, data: BALANCE }];
  assert.equal(encodeAggregate3(calls), VECTOR_IN);
  assert.deepEqual(unpack(VECTOR_IN), calls.map((c) => ({ to: c.to.toLowerCase(), data: c.data })));
  const out = [{ success: true, data: `0x${word(1)}` }, { success: false, data: "0x08c379a0010203" }, { success: true, data: "0x" }];
  assert.deepEqual(decodeAggregate3(VECTOR_OUT, 3), out);
  assert.equal(packed(out.map((o) => ({ ok: o.success, data: o.data }))), VECTOR_OUT);
  // An answer for another number of calls, one cut short, one that points outside itself, and no answer.
  assert.equal(decodeAggregate3(VECTOR_OUT, 2), null);
  assert.equal(decodeAggregate3(VECTOR_OUT, 4), null);
  assert.equal(decodeAggregate3(VECTOR_OUT.slice(0, -64), 3), null);
  assert.equal(decodeAggregate3(`0x${word(0x20)}${word(1)}${word(0x9999)}`, 1), null);
  assert.equal(decodeAggregate3("0x", 0), null);
  // One answer whose data says it is 64 bytes long and brings 32.
  assert.equal(decodeAggregate3(`0x${word(0x20)}${word(1)}${word(0x20)}${word(1)}${word(0x40)}${word(64)}${word(7)}`, 1), null);
  assert.deepEqual(decodeAggregate3(`0x${word(0x20)}${word(1)}${word(0x20)}${word(1)}${word(0x40)}${word(32)}${word(7)}`, 1),
    [{ success: true, data: `0x${word(7)}` }]);
  assert.equal(decodeAggregate3("0xzz", 1), null);
  // What may ride: a plain read of the latest state. Not a call that names a sender, a value or a block, not
  // another method, and not a call to the pack contract itself.
  assert.equal(packable(call(STAGE)), true);
  assert.equal(packable({ method: "eth_call", params: [{ to: POOL, input: STAGE }] }), true);
  assert.equal(packable({ method: "eth_call", params: [{ to: POOL, data: STAGE, from: POOL2 }, "latest"] }), false);
  assert.equal(packable({ method: "eth_call", params: [{ to: POOL, data: STAGE, value: "0x1" }, "latest"] }), false);
  assert.equal(packable({ method: "eth_call", params: [{ to: POOL, data: STAGE }, "0x10"] }), false);
  assert.equal(packable({ method: "eth_call", params: [{ to: POOL, data: STAGE }, "latest", {}] }), false);
  assert.equal(packable({ method: "eth_getCode", params: [POOL, "latest"] }), false);
  assert.equal(packable(call(STAGE, MULTICALL3.address)), false);
  assert.equal(packable(call(STAGE, MULTICALL3.address.toLowerCase())), false);
  assert.equal(packable(call("0x", POOL)), false);
});

test("reads asked together ride in one call of the pack contract, and each gets its own answer", async () => {
  const w = world({ reply: chainNode(), pack: YES });
  const before = await opened(w);
  const reads = [call(sel(1)), call(sel(2), POOL2), call(sel(3)), call(sel(4), POOL2), call(sel(5)), call(sel(6)), call(sel(7))];
  const own = [{ method: "eth_getCode", params: [POOL, "latest"] },
    { method: "eth_call", params: [{ to: POOL, data: sel(8), from: POOL2 }, "latest"] }];
  const answers = await Promise.all([...reads, ...own].map((c) => w.reader.request(c)));
  assert.equal(w.posts.length, before + 1, "one request to the node");
  const sent = w.posts.at(-1).batch;
  assert.equal(sent.length, 3, "the pack, and the two calls that go as themselves");
  assert.equal(sent[0].params[0].to, MULTICALL3.address);
  assert.equal(sent[0].params[1], "latest");
  assert.deepEqual(unpack(sent[0].params[0].data), reads.map((c) => ({ to: c.params[0].to.toLowerCase(), data: c.params[0].data })));
  assert.deepEqual(sent.slice(1).map((c) => ({ method: c.method, params: c.params })), own);
  assert.deepEqual(answers.slice(0, 7), reads.map((c) => ({ result: said(c.params[0].data) })));
  assert.deepEqual(answers.slice(7), [{ result: said("eth_getCode") }, { result: said(sel(8)) }]);
  const stats = w.reader.stats();
  assert.equal(stats.packs, 1);
  assert.equal(stats.packed, 7);
  assert.equal(stats.calls, 1 + 3, "the node was asked four calls for ten reads");
  // An answer from a pack is kept and shared like any other.
  assert.deepEqual(await w.reader.request(reads[0]), { result: said(sel(1)) });
  assert.equal(w.posts.length, before + 1);
  // One read alone is the call itself: a pack of one would only hide it.
  w.t += GENTLE.liveMs + 1;
  await w.reader.request(call(sel(9)));
  assert.equal(isPack(w.posts.at(-1).batch[0]), false);
});

test("a pack holds fifty reads and a request twenty calls, so a pool with many seats is still a few calls", async () => {
  const w = world({ reply: chainNode(), pack: YES });
  const before = await opened(w);
  const reads = Array.from({ length: 120 }, (_, i) => call(sel(i)));
  const answers = await Promise.all(reads.map((c) => w.reader.request(c)));
  assert.equal(w.posts.length, before + 1);
  assert.deepEqual(w.posts.at(-1).batch.map((c) => unpack(c.params[0].data).length), [GENTLE.maxPack, GENTLE.maxPack, 20]);
  assert.equal(GENTLE.maxPack, 50);
  assert.deepEqual(answers.map((a) => a.result), reads.map((c) => said(c.params[0].data)));
  // A thousand and one reads: twenty packs in the first request, the last read in the next.
  const w2 = world({ reply: chainNode(), pack: YES });
  await opened(w2);
  await Promise.all(Array.from({ length: 1001 }, (_, i) => w2.reader.request(call(sel(i)))));
  assert.deepEqual(w2.posts.slice(-2).map((p) => p.batch.length), [MAX_BATCH, 1]);
  assert.ok(w2.most <= GENTLE.maxInFlight);
});

test("until the contract has been checked, and when it is not the one, every read goes the plain way", async () => {
  // The check asks the node for the code and has not been answered: nothing rides yet.
  let answer;
  const pending = world({ reply: chainNode(), pack: { address: MULTICALL3.address, check: () => new Promise((r) => { answer = r; }) } });
  await opened(pending);
  await Promise.all([call(sel(1)), call(sel(2)), call(sel(3))].map((c) => pending.reader.request(c)));
  assert.equal(pending.posts.at(-1).batch.length, 3);
  assert.ok(pending.posts.every((p) => !p.batch.some(isPack)));
  assert.equal(pending.reader.stats().packing, "asking");
  answer(true);
  await new Promise((r) => setImmediate(r));
  assert.equal(pending.reader.stats().packing, "on");
  // It said no, it said something that is not yes, or it failed: no packs.
  for (const check of [async () => false, async () => "yes", async () => { throw new Error("no answer"); }]) {
    const w = world({ reply: chainNode(), pack: { address: MULTICALL3.address, check } });
    await opened(w);
    await Promise.all([call(sel(1)), call(sel(2))].map((c) => w.reader.request(c)));
    assert.ok(w.posts.every((p) => !p.batch.some(isPack)));
    assert.equal(w.reader.stats().packing, "off");
  }
  // The check's own read is a read of this reader, belongs to no page, and survives the visitor leaving one.
  // The visitor leaves while it is out: the page's own read is told so, the check is answered.
  let release;
  const gate = new Promise((r) => { release = r; });
  const node = chainNode();
  const w = world({ reply: async (n, batch) => (n === 1 ? (await gate, node(n, batch)) : node(n, batch)), pack: { address: MULTICALL3.address,
    check: async (ask) => (await ask({ method: "eth_getCode", params: [MULTICALL3.address, "latest"] })).result === said("eth_getCode") } });
  const first = w.reader.request(call("0x00000001"));
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(w.posts[0].batch.map((c) => c.method), ["eth_call", "eth_getCode"], "the check's read is out with the page's");
  w.reader.leave();
  release();
  assert.equal((await first).thrown?.code, PAGE_LEFT);
  await new Promise((r) => setImmediate(r));
  assert.equal(w.reader.stats().packing, "on");
  assert.ok(w.posts.some((p) => p.batch.some((c) => c.method === "eth_getCode" && c.params[0] === MULTICALL3.address)));
  // With no pack contract named there is nothing to ask and nothing rides.
  const none = world({ reply: chainNode() });
  await Promise.all([call(sel(1)), call(sel(2))].map((c) => none.reader.request(c)));
  assert.equal(none.reader.stats().packing, "off");
  assert.equal(none.posts[0].batch.length, 2);
});

test("a read that fails inside a pack is asked again as itself, and its error is the node's own", async () => {
  const w = world({ reply: chainNode({ failing: new Set([sel(2), sel(4)]) }), pack: YES });
  const before = await opened(w);
  const answers = await Promise.all([1, 2, 3, 4].map((i) => w.reader.request(call(sel(i)))));
  assert.deepEqual(answers, [{ result: said(sel(1)) }, { error: { code: 3, message: "execution reverted" } },
    { result: said(sel(3)) }, { error: { code: 3, message: "execution reverted" } }]);
  assert.equal(w.posts.length, before + 2);
  assert.deepEqual(w.posts.at(-1).batch.map((c) => [c.params[0].to, c.params[0].data]), [[POOL, sel(2)], [POOL, sel(4)]]);
  // Packs go on: one read that reverts says nothing about the contract.
  assert.equal(w.reader.stats().packFailures, 0);
  w.t += GENTLE.liveMs + 1;
  await Promise.all([5, 6].map((i) => w.reader.request(call(sel(i)))));
  assert.equal(isPack(w.posts.at(-1).batch[0]), true);
});

test("a pack the node will not take is the last one made, and its reads are asked again the plain way", async () => {
  for (const refuse of [
    (c) => ({ jsonrpc: "2.0", id: c.id, error: { code: -32000, message: "out of gas" } }),
    (c) => ({ jsonrpc: "2.0", id: c.id, result: "0x1234" }),
    (c) => ({ jsonrpc: "2.0", id: c.id, result: packed([{ ok: true, data: "0xaa" }]) }),
    () => ({ jsonrpc: "2.0", id: 999, result: "0x" }),
  ]) {
    const w = world({ reply: chainNode({ packs: refuse }), pack: YES });
    const before = await opened(w);
    const answers = await Promise.all([1, 2, 3].map((i) => w.reader.request(call(sel(i)))));
    assert.deepEqual(answers, [1, 2, 3].map((i) => ({ result: said(sel(i)) })), "each read is answered all the same");
    assert.equal(w.posts.length, before + 2);
    assert.equal(w.posts.at(-1).batch.length, 3);
    assert.ok(!w.posts.at(-1).batch.some(isPack));
    assert.equal(w.reader.stats().packFailures, 1);
    assert.equal(w.reader.stats().packing, "off");
    w.t += GENTLE.liveMs + 1;
    await Promise.all([4, 5].map((i) => w.reader.request(call(sel(i)))));
    assert.ok(!w.posts.at(-1).batch.some(isPack), "after it, reads go as they went before there were packs");
    assert.equal(w.posts.at(-1).batch.length, 2);
  }
});

test("a refusal for the rate holds a pack like any batch: it waits, and is asked again as the same pack", async () => {
  const node = chainNode();
  let refused = 0;
  const w = world({ pack: YES, reply: (n, batch) => (batch.some(isPack) && refused++ === 0 ? REFUSAL : node(n, batch)) });
  const before = await opened(w);
  const answers = await Promise.all([1, 2, 3].map((i) => w.reader.request(call(sel(i)))));
  assert.deepEqual(answers, [1, 2, 3].map((i) => ({ result: said(sel(i)) })));
  assert.equal(w.posts.length, before + 2);
  assert.deepEqual(w.posts.at(-1).batch, w.posts.at(-2).batch);
  assert.equal(w.posts.at(-1).at - w.posts.at(-2).at, GENTLE.retryDelaysMs[0]);
  assert.deepEqual(w.waits.filter(Boolean).map((x) => x.calls), [3], "the wait speaks of the reads, not of the one call");
  assert.equal(w.reader.stats().packFailures, 0);
  assert.equal(w.reader.stats().packing, "on");
});

test("what a page still has waiting when it is left is not sent, and an answer that comes back for it is kept but not handed to it", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const w = world({ options: { maxInFlight: 1, maxBatch: 2 }, reply: async (n) => (n === 1 ? (await gate, null) : null) });
  const asked = [1, 2, 3, 4, 5].map((i) => w.reader.request(call(sel(i))));
  const sent = { method: "eth_sendRawTransaction", params: ["0xabc"] };
  const write = w.reader.request(sent);
  await new Promise((r) => setImmediate(r));
  assert.equal(w.posts.length, 1, "two reads are out, the rest wait");
  w.reader.leave();
  release();
  const answers = await Promise.all(asked);
  for (const a of answers) assert.equal(a.thrown?.code, PAGE_LEFT);
  assert.equal(PAGE_LEFT, "PAGE_LEFT");
  // What is not a read is never dropped: it is sent, and answered.
  assert.deepEqual(await write, { result: said("eth_sendRawTransaction") });
  assert.deepEqual(w.posts.map((p) => p.batch.map((c) => c.params[0]?.data ?? c.method)), [[sel(1), sel(2)], ["eth_sendRawTransaction"]]);
  assert.equal(w.reader.stats().dropped, 5);
  // The two that came back were kept: the page that is open now has them without asking.
  assert.deepEqual(await w.reader.request(call(sel(1))), { result: said(sel(1)) });
  assert.equal(w.posts.length, 2);
  // The three that were never sent are asked when a page asks for them.
  assert.deepEqual(await w.reader.request(call(sel(4))), { result: said(sel(4)) });
  assert.equal(w.posts.length, 3);
});

test("a read the page that is open asks for too is that page's, though the page before it asked first", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const w = world({ options: { maxInFlight: 1, maxBatch: 1 }, reply: async (n) => (n === 1 ? (await gate, null) : null) });
  const out = w.reader.request(call(sel(1)));        // sent
  const waiting = w.reader.request(call(sel(2)));    // waits behind it
  await new Promise((r) => setImmediate(r));
  w.reader.leave();
  const again = [w.reader.request(call(sel(1))), w.reader.request(call(sel(2)))];
  release();
  assert.deepEqual(await Promise.all([out, waiting, ...again]), [1, 2, 1, 2].map((i) => ({ result: said(sel(i)) })));
  assert.equal(w.reader.stats().dropped, 0);
  assert.deepEqual(w.posts.map((p) => p.batch[0].params[0].data), [sel(1), sel(2)]);
});

test("a page is told when it is left, says it is loading until its view writes, and opens at its top or where it was left", async () => {
  const order = [];
  const navigate = createNavigator(() => (order.push("mount"), {}), { leave: () => order.push("leave") });
  await navigate(async () => order.push("view"));
  await navigate(async () => order.push("view"));
  assert.deepEqual(order, ["leave", "mount", "view", "leave", "mount", "view"]);
  assert.equal(await createNavigator(() => ({}))(async () => {}), true, "with nothing to tell, it navigates as before");
  // An entry the visitor comes back to carries its place; a page they go on to has none.
  assert.equal(scrollTarget(null), 0);
  assert.equal(scrollTarget(undefined), 0);
  assert.equal(scrollTarget({}), 0);
  assert.equal(scrollTarget({ y: 1500 }), 1500);
  assert.equal(scrollTarget({ y: 1500.4 }), 1500);
  assert.equal(scrollTarget({ y: "abc" }), 0);
  assert.equal(scrollTarget({ y: -5 }), 0);
  assert.equal(scrollTarget({ y: Infinity }), 0);
  // The words are the ones the site opens with.
  assert.equal(LOADING, '<p class="muted">Loading…</p>');
  assert.ok(text("../index.html").includes(`<main id="view">${LOADING}</main>`));
  const app = text("../app.js");
  assert.match(app, /const page = document\.createElement\("div"\);\s+page\.innerHTML = LOADING;\s+view\(\)\.replaceChildren\(page\);\s+return page;\s+\}, \{ leave: chain\.leavePage \}\);/);
  assert.match(app, /history\.scrollRestoration = "manual";/);
  assert.match(app, /history\.replaceState\(\{ \.\.\.\(history\.state \|\| \{\}\), y: Math\.round\(window\.scrollY\) \}, ""\);/);
  assert.match(app, /document\.addEventListener\("click", write, true\);/);
  assert.match(app, /clearTimeout\(scrollNote\);\s+const y = scrollTarget\(history\.state\);\s+const shown = navigate\(viewFor\(hash\), failed\);\s+place\(y, shown\);/);
  assert.match(app, /function place\(y, shown\) \{\s+window\.scrollTo\(0, y\);\s+if \(!y\) return;/);
  assert.match(app, /if \(current && !moved\) window\.scrollTo\(0, y\);/);
  assert.match(text("../lib/chain.js"), /export const leavePage = \(\) => \{\s+gentle\.leave\(\);\s+plain\.leave\(\);\s+\};/);
});

test("packs are made only through the code that is Multicall3's, and the pages that check never use them", () => {
  const chain = text("../lib/chain.js");
  assert.match(chain, /const answer = await ask\(\{ method: "eth_getCode", params: \[MULTICALL3\.address, "latest"\] \}\);\s+return typeof answer\.result === "string" && answer\.result\.length > 2\s+&& ethers\.keccak256\(answer\.result\) === MULTICALL3\.codeHash;/);
  assert.match(chain, /pack: \{ address: MULTICALL3\.address, check: isMulticall3 \},/);
  // The reader of the checking pages has no pack contract and a memory of its own.
  const plain = chain.match(/const plain = createGentle\(\{([^}]*)\}\);/)[1];
  assert.doesNotMatch(plain, /pack/);
  assert.match(plain, /storePrefix: "pools-rpc-check-v1:"/);
  assert.equal((chain.match(/createGentle\(\{/g) || []).length, 2);
  const verify = text("../views/verify.js");
  assert.match(verify, /chain\.deploymentOf\(address, "pool", chain\.checkProvider\),\s+chain\.deploymentOf\(address, "challenge", chain\.checkProvider\)/);
  assert.match(verify, /const account = chain\.contract\(kind, address, chain\.checkProvider\);/);
  assert.match(verify, /const reg = chain\.registry\(chain\.checkProvider, deployment\);/);
  assert.doesNotMatch(verify, /readProvider|chain\.contract\([^)]*\)(?<!checkProvider\))|chain\.registry\(undefined|chain\.factory\(/);
  const records = text("../views/evidence.js");
  assert.match(records, /checkProvider\.getTransactionReceipt\(row\.tx\),\s+question \? deploymentOf\(row\.account, question, checkProvider\) : undefined,/);
  assert.doesNotMatch(records, /readProvider/);
  assert.match(chain, /export async function deploymentOf\(address, kind = "pool", runner = undefined\) \{/);
});

test("coming back to the list shows what was read before, says how old it is, and reads again", async () => {
  assert.equal(readAgo(0), "0 s ago");
  assert.equal(readAgo(7400), "7 s ago");
  assert.equal(readAgo(59_400), "59 s ago");
  assert.equal(readAgo(60_000), "1 min ago");
  assert.equal(readAgo(45 * 60_000), "45 min ago");
  assert.equal(readAgo(3 * 3600_000), "3 h ago");
  assert.equal(readAgo(-5), "0 s ago");
  assert.equal(`${LIST_KEPT.shown(readAgo(7000))} ${LIST_KEPT.again}`, "Shown as it was read 7 s ago. Reading the chain again…");
  assert.equal(LIST_KEPT.failed, "Reading it again failed:");
  const pools = text("../views/pools.js");
  assert.match(pools, /const kept = lastList;/);
  assert.match(pools, /if \(kept\) \{\s+\/\/[^\n]*\n\s+paint\(page, kept, true\);\s+asRead\.textContent = `\$\{LIST_KEPT\.shown\(readAgo\(Date\.now\(\) - kept\.at\)\)\} \$\{LIST_KEPT\.again\}`;\s+asRead\.hidden = false;/);
  assert.match(pools, /lastList = \{ \.\.\.read, at: Date\.now\(\) \};\s+paint\(page, lastList, true\);\s+asRead\.hidden = true;/);
  // A reading that fails does not take the list that is on screen with it; with nothing on screen, the page fails.
  assert.match(pools, /\} catch \(err\) \{\s+if \(!kept\) throw err;/);
  assert.match(pools, /asRead\.textContent = `\$\{LIST_KEPT\.shown\(readAgo\(Date\.now\(\) - kept\.at\)\)\} \$\{LIST_KEPT\.failed\} \$\{friendly\(err\)\}`;/);
  // The pools are shown when the chain has said what they are; "I want to trade" leads somewhere only once every
  // balance is in, because a balance decides whether a pool can sell.
  assert.match(pools, /const read = await readList\(live, kept \? undefined : \(items, fee\) => paint\(page, \{ items, fee \}, false\)\);/);
  // Nothing is painted before the live deployment has answered: the archive alone would make the line above the
  // list say there are no pools on the live deployment.
  assert.match(pools, /if \(sofar\.has\(live\)\) seen\(DEPLOYMENTS\.flatMap\(\(d\) => sofar\.get\(d\)\?\.items \?\? \[\]\), sofar\.get\(live\)\.fee\);/);
  assert.match(pools, /if \(!complete\) return;\s+\/\/[^\n]*\n\s+const target = tradeTarget\(ordered\.filter\(\(i\) => i\.spotUsdc !== null\)\);/);
  assert.match(pools, /const held = known \? `\$\{esc\(spotUsdc\.toFixed\(2\)\)\} USDC on HyperCore spot`\s+: esc\(item\.unread \? LIST_KEPT\.unread : LIST_KEPT\.balance\);/);
  // One balance Hyperliquid does not give costs that card its balance and its verdict, never the list on screen.
  assert.match(pools, /let spotUsdc;\s+try \{\s+spotUsdc = await item\.spot;\s+\} catch \{\s+return \{ \.\.\.item, spotUsdc: null, unread: true \};\s+\}/);
  assert.equal(LIST_KEPT.unread, "its HyperCore balance could not be read");
  assert.equal(LIST_KEPT.balance, "reading its HyperCore balance…");
  // Hyperliquid's list of perps is asked once, by however many cards: 36 askings at once were measured.
  const hl = await import("../lib/hl.js");
  const real = globalThis.fetch;
  let asked = 0;
  let fail = true;
  globalThis.fetch = async () => {
    asked++;
    if (fail) return { ok: false, status: 500, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => ({ universe: [{ name: "BTC", szDecimals: 5 }, { name: "ETH", szDecimals: 4 }] }) };
  };
  try {
    await assert.rejects(() => Promise.all([hl.perps(), hl.perps()]), /HTTP 500/);
    assert.equal(asked, 1);
    fail = false;                     // a failure is not kept: the next asking asks again
    const [a, b, c] = await Promise.all([hl.perps(), hl.perp(1), hl.perps()]);
    assert.equal(asked, 2);
    assert.deepEqual(a, [{ index: 0, name: "BTC", szDecimals: 5 }, { index: 1, name: "ETH", szDecimals: 4 }]);
    assert.deepEqual(b, { index: 1, name: "ETH", szDecimals: 4 });
    assert.equal(c, a);
    await hl.perps();
    assert.equal(asked, 2);
  } finally {
    globalThis.fetch = real;
  }
});
