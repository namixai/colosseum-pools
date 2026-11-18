// Chain reads that go easy on a public node.
//
// Every read of the app goes to one public HyperEVM RPC, from the visitor's own address. Measured on 12 Oct 2026:
// the home page asked it 158 calls in 41 HTTP requests, 50 of them the same call asked twice, and a visitor with
// two tabs and a wallet open got "Could not load this page" on it. The node does not answer 429. It answers
// HTTP 200 with one object for the whole batch, `{"id":null,"error":{"code":-32005,"message":"rate limited"}}`,
// with no Retry-After, and after a burst it kept refusing a single call for about 45 seconds.
//
// So the reads are made gentle in one place, under the provider, where every page's reads pass:
//   - the same call asked twice is asked once: while it is in flight, and for a short time after;
//   - what a contract never changes (its rules, its terms, its owner) is kept for the session;
//   - calls are gathered into batches of at most twenty, and at most two batches are out at a time;
//   - a refusal for the rate is not an error yet: the batch waits and asks again, longer each time, and every
//     other batch waits with it. Only when the last wait is spent do the calls fail, with the node's own refusal.
//
// 13 Oct 2026, measured again: 91 calls in 12 requests one after another, 6.3 seconds for the home page, and a home
// page the visitor had already left went on asking for its 44. So two things more:
//   - reads of the latest state ride together in one call of Multicall3 (app/lib/multicall.js), once the code at
//     its address has been checked. The node counts that call as one. A read that fails inside a pack, and every
//     read of a pack the node would not take, is asked again the plain way, and after such a pack no more are made;
//   - what a page still has waiting when the visitor leaves it is not sent, and an answer that comes back for it
//     is kept but not handed to it, so a view that reads in steps stops there.
// No browser globals here, so node's test runner loads it (app/tests/gentle.test.mjs).
import { MAX_BATCH } from "./batch.js";
import { packable, encodeAggregate3, decodeAggregate3 } from "./multicall.js";

export const GENTLE = Object.freeze({
  maxBatch: MAX_BATCH,
  /** Reads in one pack. Fifty-six of a pool's views came back in one answer of 12 KB (13 Oct 2026). */
  maxPack: 50,
  /** Batches out at one time. */
  maxInFlight: 2,
  /** How long calls are gathered before a batch goes: one render's reads start within a few milliseconds. */
  gatherMs: 15,
  /** How long an answer that can change is kept. Short on purpose: a verdict or a stage read here is shown as now. */
  liveMs: 3_000,
  /** How long an answer that cannot change is kept. */
  staticMs: 30 * 60_000,
  /** The waits after a refusal for the rate, in turn: a minute in all, a little over what the node held a refusal. */
  retryDelaysMs: Object.freeze([1_000, 2_000, 4_000, 8_000, 15_000, 30_000]),
  /**
   * How long a call that is out is joined by a later asking of the same call. A call whose request never comes back would
   * otherwise be joined for ever: every later reading of it would wait on the one that hangs. Past this, the same call is
   * asked again (and the new asking is the one later ones join). Under the screens' own limit for a reading that is out.
   */
  joinMs: 10_000,
  /** A request that did not reach the node at all, or met a 5xx, is sent again this many times. */
  networkRetries: 2,
  networkDelayMs: 800,
});

/** The methods whose answers may be shared and kept. Anything else goes to the node every time it is asked. */
export const READS = new Set([
  "eth_call", "eth_getCode", "eth_chainId", "eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_blockNumber",
  "eth_getBalance",
]);

/** What a read is answered with when the page it was for has been left before it was sent. */
export const PAGE_LEFT = "PAGE_LEFT";
const left = () => Object.assign(new Error("This read was for a page that is no longer open."), { code: PAGE_LEFT });

/** The node's code for "rate limited", and what it says. */
export const RATE_LIMITED = Object.freeze({ code: -32005, message: "rate limited" });

/** One call as the cache knows it: the method and its parameters, as sent. */
export function keyOf(call) {
  return `${call.method}:${JSON.stringify(call.params ?? [])}`;
}

const says = (error) => Number(error?.code) === RATE_LIMITED.code || Number(error?.code) === 429
  || /rate limit|too many requests/i.test(String(error?.message || ""));

/**
 * Whether an answer is the node refusing for the rate: HTTP 429, the one object it sends in place of a batch, or a
 * batch in which a call was refused that way.
 */
export function refusedForRate(status, body) {
  if (Number(status) === 429) return true;
  if (Array.isArray(body)) return body.some((item) => says(item?.error));
  return Boolean(body && typeof body === "object" && says(body.error));
}

/**
 * Whether an answer can never change, so that it may be kept for the session. `selectors` are the first four bytes
 * of the views a contract fixes when it is set up. An empty answer is never kept: "no code here" and "0x" can both
 * become something.
 */
export function neverChanges(call, result, selectors = new Set()) {
  const params = call.params ?? [];
  if (call.method === "eth_chainId") return typeof result === "string";
  if (call.method === "eth_getTransactionReceipt") return Boolean(result && result.blockNumber);
  if (call.method === "eth_getCode") return typeof result === "string" && result.length > 2;
  if (call.method === "eth_call") {
    const tx = params[0] || {};
    const data = String(tx.data ?? tx.input ?? "");
    const latest = params[1] === undefined || params[1] === "latest";
    return latest && typeof result === "string" && result.length > 2 && selectors.has(data.slice(0, 10).toLowerCase());
  }
  return false;
}

const copy = (answer) => (answer.thrown ? answer : JSON.parse(JSON.stringify(answer)));
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * The gentle reader. `post(batch)` sends one JSON-RPC batch and resolves to `{ status, body }`, the HTTP status and
 * the parsed answer (null when it was not JSON); it rejects when nothing answered.
 *
 * `request(call)` resolves, never rejects, to one of: `{ result }`, `{ error }` as the node gave it, or `{ thrown }`,
 * an Error for a request that never got an answer. `onWait` is told each wait, `{ attempt, of, delayMs, calls }`, and
 * `null` when the reads go again. `store` is a sessionStorage-like place for what never changes, or null.
 *
 * `pack` is `{ address, check }` or null. `check(ask)` is asked once, with a `request` of this reader, whether the
 * contract at the address is the one to pack reads through; until it has said yes, and after a pack that failed,
 * every read goes the plain way.
 */
export function createGentle({
  post, now = Date.now, sleep = pause, schedule = (fn, ms) => setTimeout(fn, ms), onWait = () => {},
  staticSelectors = new Set(), store = null, storePrefix = "pools-rpc-v1:", options = {}, pack = null,
} = {}) {
  const o = { ...GENTLE, ...options };
  const cache = new Map();     // key -> { at, ttl, answer }
  const flying = new Map();    // key -> { asked, item } of a call that is out or queued
  const queue = [];            // { call, key, kept, resolve, generation, visit, direct }
  const stats = { posts: 0, calls: 0, cached: 0, shared: 0, waits: 0, gaveUp: 0, packs: 0, packed: 0, packFailures: 0, dropped: 0 };
  // "unasked", "asking", "on" or "off": whether reads may ride in packs.
  let packing = pack ? "unasked" : "off";
  // Which page of the visitor's a read was asked for: it counts up each time they leave one.
  let visit = 0;
  let inFlight = 0;
  let scheduled = false;
  let coolUntil = 0;
  let waiting = 0;
  // Which side of the visitor's last transaction a call was asked on. A read asked before it may come back after
  // it, with what the chain held before: that answer goes to who asked, and is neither kept nor shared.
  let generation = 0;

  function remembered(key) {
    const hit = cache.get(key);
    if (hit && now() - hit.at < hit.ttl) return hit.answer;
    if (hit) cache.delete(key);
    if (!store) return null;
    try {
      const kept = JSON.parse(store.getItem(storePrefix + key) || "null");
      if (!kept || !(now() - kept.at < o.staticMs)) return null;
      cache.set(key, { at: kept.at, ttl: o.staticMs, answer: { result: kept.result } });
      return { result: kept.result };
    } catch {
      return null;   // storage blocked or full of something else: the node is asked, as if nothing were kept
    }
  }

  function remember(item, answer) {
    if (!item.kept || answer.result === undefined) return;
    const fixed = neverChanges(item.call, answer.result, staticSelectors);
    if (!fixed && item.generation !== generation) return;
    const at = now();
    cache.set(item.key, { at, ttl: fixed ? o.staticMs : o.liveMs, answer });
    if (fixed && store) {
      try {
        store.setItem(storePrefix + item.key, JSON.stringify({ at, result: answer.result }));
      } catch {
        // kept in memory only
      }
    }
  }

  function askAboutPack() {
    packing = "asking";
    Promise.resolve()
      .then(() => pack.check((call) => request(call, { lasting: true })))
      .then((yes) => {
        packing = yes === true ? "on" : "off";
      }, () => {
        packing = "off";
      });
  }

  /** `lasting`: a read that belongs to no page, so leaving a page does not drop it. */
  function request(call, { lasting = false } = {}) {
    if (packing === "unasked") askAboutPack();
    const key = keyOf(call);
    const kept = READS.has(call.method);
    if (kept) {
      const answer = remembered(key);
      if (answer) {
        stats.cached++;
        return Promise.resolve(copy(answer));
      }
      const same = flying.get(key);
      if (same && now() - same.at < o.joinMs) {
        stats.shared++;
        // The page that is open now wants it too: it is no longer only a read of a page that was left.
        if (same.item.visit < visit) same.item.visit = visit;
        return same.asked.then(copy);
      }
    }
    const item = { call, key, kept, generation, visit: lasting ? Infinity : visit, direct: false, resolve: null };
    const asked = new Promise((resolve) => {
      item.resolve = resolve;
    });
    queue.push(item);
    if (kept) {
      const entry = { asked, item, at: now() };
      flying.set(key, entry);
      // Only its own entry: by now a later asking of the same call may be the one that is out.
      asked.then(() => {
        if (flying.get(key) === entry) flying.delete(key);
      });
    }
    if (!scheduled) {
      scheduled = true;
      schedule(() => {
        scheduled = false;
        pump();
      }, o.gatherMs);
    }
    return asked.then(copy);
  }

  /**
   * The next request to the node: at most `maxBatch` calls, a pack of reads counting as the one call it is. A read
   * still waiting for a page the visitor has left is not sent: whoever asked is told so.
   */
  function take() {
    const entries = [];
    let open = null;
    while (queue.length) {
      const item = queue[0];
      if (item.kept && item.visit < visit) {
        queue.shift();
        stats.dropped++;
        item.resolve({ thrown: left() });
        continue;
      }
      const rides = packing === "on" && !item.direct && packable(item.call, pack.address);
      if (rides && open && open.items.length < o.maxPack) {
        open.items.push(queue.shift());
        continue;
      }
      if (entries.length >= o.maxBatch) break;
      const entry = { items: [queue.shift()], packed: rides };
      entries.push(entry);
      if (rides) open = entry;
    }
    // A pack of one is the call itself.
    for (const entry of entries) if (entry.items.length === 1) entry.packed = false;
    return entries;
  }

  function pump() {
    while (inFlight < o.maxInFlight && queue.length) {
      const entries = take();
      if (entries.length) send(entries);
    }
  }

  /** One entry as the node is asked it: the call itself, or one call of the pack contract carrying its reads. */
  function wire(entry, id) {
    if (!entry.packed) {
      const { call } = entry.items[0];
      return { jsonrpc: "2.0", id, method: call.method, params: call.params ?? [] };
    }
    const data = encodeAggregate3(entry.items.map(({ call }) => {
      const tx = call.params[0];
      return { to: tx.to, data: tx.data ?? tx.input };
    }));
    return { jsonrpc: "2.0", id, method: "eth_call", params: [{ to: pack.address, data }, "latest"] };
  }

  const reads = (entries) => entries.reduce((n, entry) => n + entry.items.length, 0);

  /** One batch to the node, once. What came back, sorted into what to do with it. */
  async function ask(entries) {
    const body = entries.map((entry, i) => wire(entry, i + 1));
    for (let n = 0; ; n++) {
      let reply = null;
      let failure = null;
      try {
        reply = await post(body);
      } catch (err) {
        failure = err instanceof Error ? err : new Error(String(err));
      }
      if (reply) {
        stats.posts++;
        stats.calls += entries.length;
        for (const entry of entries) {
          if (!entry.packed) continue;
          stats.packs++;
          stats.packed += entry.items.length;
        }
        if (refusedForRate(reply.status, reply.body)) return { kind: "limited" };
        if (Array.isArray(reply.body)) return { kind: "answers", list: reply.body };
        if (reply.body && typeof reply.body === "object" && reply.body.error) return { kind: "refused", error: reply.body.error };
        // No JSON-RPC answer at all: a gateway's error page, or a body cut short.
        failure = Object.assign(new Error(`The node answered HTTP ${reply.status} with nothing this app can read.`), { status: reply.status });
        if (Number(reply.status) < 500) return { kind: "thrown", thrown: failure };
      }
      if (n >= o.networkRetries) return { kind: "thrown", thrown: failure };
      await sleep(o.networkDelayMs * (n + 1));
    }
  }

  async function send(entries) {
    inFlight++;
    let told = false;
    try {
      let outcome;
      for (let attempt = 0; ; attempt++) {
        // A refusal met by one batch holds every batch: asking through it only earns another refusal.
        const hold = coolUntil - now();
        if (hold > 0) await sleep(hold);
        outcome = await ask(entries);
        if (outcome.kind !== "limited" || attempt >= o.retryDelaysMs.length) break;
        const delayMs = o.retryDelaysMs[attempt];
        coolUntil = Math.max(coolUntil, now() + delayMs);
        stats.waits++;
        if (!told) waiting++;
        told = true;
        onWait({ attempt: attempt + 1, of: o.retryDelaysMs.length, delayMs, calls: reads(entries) });
        await sleep(delayMs);
      }
      settle(entries, outcome);
    } finally {
      if (told && --waiting === 0) onWait(null);
      inFlight--;
      pump();
    }
  }

  /**
   * An answer goes to whoever asked, and into memory. When the page it was asked for has been left meanwhile, the
   * answer is still kept, but that page is told it was left: a view that reads in steps stops at this one, and does
   * not go on to ask the next.
   */
  function finish(item, answer) {
    remember(item, answer);
    if (item.kept && item.visit < visit) {
      stats.dropped++;
      item.resolve({ thrown: left() });
    } else {
      item.resolve(answer);
    }
  }

  function settle(entries, outcome) {
    const byId = new Map();
    if (outcome.kind === "answers") for (const a of outcome.list) byId.set(Number(a?.id), a);
    if (outcome.kind === "limited") stats.gaveUp += reads(entries);
    const again = [];
    entries.forEach((entry, i) => {
      // No answer at all, or the last refusal for the rate: every read of the batch gets it, packed or not.
      if (outcome.kind === "thrown") return entry.items.forEach((item) => finish(item, { thrown: outcome.thrown }));
      if (outcome.kind === "limited") return entry.items.forEach((item) => finish(item, { error: { ...RATE_LIMITED } }));
      const a = outcome.kind === "answers" ? byId.get(i + 1) : null;
      if (!entry.packed) {
        const item = entry.items[0];
        if (outcome.kind === "refused") return finish(item, { error: outcome.error });
        if (!a) return finish(item, { error: { code: -32000, message: "the node left this call out of its answer" } });
        return finish(item, a.error ? { error: a.error } : { result: a.result });
      }
      // A pack. What it brought is each read's own answer. A pack the node would not take, or whose answer is not
      // a pack's, is the last one made: its reads are asked again the plain way, which is how they were asked before
      // there were packs. So is a read that failed inside a pack, so that its error is the node's own for that call.
      const parts = a && typeof a.result === "string" ? decodeAggregate3(a.result, entry.items.length) : null;
      if (!parts) {
        packing = "off";
        stats.packFailures++;
        again.push(...entry.items);
        return undefined;
      }
      return entry.items.forEach((item, j) => (parts[j].success ? finish(item, { result: parts[j].data }) : again.push(item)));
    });
    for (const item of again) item.direct = true;
    queue.unshift(...again);
  }

  /**
   * Forget what can change: after a transaction of the visitor's own, the page must read what it did. That takes in
   * the reads still out or waiting, which were asked before it: none of them is joined by a later asking, and what
   * they bring back is not kept.
   */
  function forgetLive() {
    generation++;
    flying.clear();
    for (const [key, hit] of cache) if (hit.ttl !== o.staticMs) cache.delete(key);
  }

  /**
   * The visitor went to another page. What the page they left still has waiting is not sent. What is already out
   * comes back and is kept as any answer is, but the page that was left is not handed it, so it asks for nothing
   * more. A read the new page asks for too is the new page's.
   */
  function leave() {
    visit++;
  }

  return { request: (call) => request(call), forgetLive, leave, stats: () => ({ ...stats, queued: queue.length, inFlight, packing }) };
}

/** What the page says while the reads wait for the node. */
export function waitWords(wait) {
  if (!wait) return "";
  const seconds = Math.max(1, Math.round(wait.delayMs / 1000));
  return `The public HyperEVM RPC is limiting requests from this browser. Waiting ${seconds} s before asking again `
    + `(try ${wait.attempt} of ${wait.of}).`;
}
