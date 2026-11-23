// node --test app/tests/simindex.test.mjs
//
// The panels' use of the sim pools' event index on the host (app/lib/simindex.js): the index's answer is checked and its
// events read as candidates, an index that does not answer is said in words and the page reads the chain itself, a node's
// odd refusal is said as what it is, and a send the index names is shown only when its receipt has it. No browser and no
// chain: `fetch` and the receipts are made up.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { indexUrl, eventOf, checkAnswer, askIndex, sourceNote, readShare, readFailure, sendInReceipt, indexBase, receiptsToAsk, sortSends }
  from "../lib/simindex.js";
import { scanNote, codesHtml } from "../lib/simowner.js";
import { stopScanNote } from "../lib/simoperator.js";

const here = dirname(fileURLToPath(import.meta.url));
const POOL = "0x00000000000000000000000000000000000000A1";
const PARTNER = "0x00000000000000000000000000000000000000C3";
const AUTHOR = "0x00000000000000000000000000000000000000D4";
const CODE = `0x${"ab".repeat(32)}`;
const TX = `0x${"01".repeat(32)}`;
const SHARE_PAID = `0x${"5a".repeat(32)}`;

const codeSet = { name: "CodeSet", block: 107, index: 1, tx: TX, code: CODE, partner: PARTNER, author: AUTHOR, bps: 4000, authorBps: 1000 };
const paid = { name: "SharePaid", block: 2500, index: 0, tx: TX, code: CODE, to: PARTNER, amount: "123456", sent: true };
const stopped = { name: "PayoutStopped", block: 2600, index: 0, tx: TX, id: 7, trader: PARTNER };
const answer = (over = {}) => ({ status: "ok", pool: POOL, chain_id: 998, created: 105, scanned_to: 3000,
  events: [codeSet, paid, stopped, { name: "StopLifted", block: 2700, index: 0, tx: TX, id: 7 }], ...over });

test("the index is asked on the gateway's host, over https or on this machine", () => {
  assert.equal(indexUrl("https://api.example/", POOL), `https://api.example/v1/simindex/pool?pool=${POOL}`);
  assert.equal(indexUrl("http://127.0.0.1:8789", POOL), `http://127.0.0.1:8789/v1/simindex/pool?pool=${POOL}`);
  assert.throws(() => indexUrl("http://api.example", POOL), /https/);
  assert.equal(indexBase({ gateway: "https://g" }), "https://g");
  assert.equal(indexBase({ gateway: "https://g", simIndex: "https://i" }), "https://i");
});

test("the index's events are read as the panels read events, and an entry of another shape is left out", () => {
  const c = eventOf(codeSet);
  assert.deepEqual(c.args, { code: CODE, partner: PARTNER, bps: 4000, author: AUTHOR, authorBps: 1000 });
  assert.equal(c.fromIndex, true);
  assert.equal(eventOf(paid).args.amount, 123456n);
  assert.equal(eventOf(stopped).args.id, 7);
  assert.equal(eventOf({ ...codeSet, partner: "0x12" }), null);
  assert.equal(eventOf({ ...codeSet, bps: 10001 }), null);
  assert.equal(eventOf({ ...paid, amount: "-1" }), null);
  assert.equal(eventOf({ ...stopped, id: -1 }), null);
  assert.equal(eventOf({ ...codeSet, name: "Withdrawn" }), null);
  assert.equal(eventOf(null), null);
});

test("an answer for another pool or chain, or of another shape, is not taken", () => {
  const ok = checkAnswer(answer(), { pool: POOL.toLowerCase(), chainId: 998 });
  assert.equal(ok.ok, true);
  assert.equal(ok.events.length, 4);
  assert.equal(ok.scannedTo, 3000);
  assert.equal(checkAnswer(answer({ pool: PARTNER }), { pool: POOL, chainId: 998 }).ok, false);
  assert.match(checkAnswer(answer({ chain_id: 999 }), { pool: POOL, chainId: 998 }).why, /chain 999/);
  assert.equal(checkAnswer(answer({ events: "x" }), { pool: POOL, chainId: 998 }).ok, false);
  assert.equal(checkAnswer(null, { pool: POOL, chainId: 998 }).ok, false);
  // An event past the blocks the index says it read, or not readable, is dropped and counted.
  const some = checkAnswer(answer({ events: [codeSet, { ...paid, block: 3001 }, { junk: 1 }] }), { pool: POOL, chainId: 998 });
  assert.equal(some.events.length, 1);
  assert.equal(some.dropped, 2);
});

const reply = (status, body) => async () => ({ ok: status >= 200 && status < 300, status, json: async () => body });

test("asking the index never throws: each failure is words for the page", async () => {
  const base = "https://api.example";
  assert.equal((await askIndex({ base, pool: POOL, chainId: 998, fetch: reply(200, answer()) })).ok, true);
  assert.match((await askIndex({ base, pool: POOL, chainId: 998, fetch: reply(404, { code: "unknown_pool" }) })).why, /does not know this pool/);
  assert.match((await askIndex({ base, pool: POOL, chainId: 998, fetch: reply(502, null) })).why, /HTTP 502/);
  assert.match((await askIndex({ base, pool: POOL, chainId: 998, fetch: async () => { throw new TypeError("Failed to fetch"); } })).why, /could not be reached/);
  assert.match((await askIndex({ base: "http://x", pool: POOL, chainId: 998, fetch: reply(200, answer()) })).why, /https/);
  const hang = (url, { signal }) => new Promise((_, reject) => signal.addEventListener("abort", () => {
    const e = new Error("aborted");
    e.name = "AbortError";
    reject(e);
  }));
  assert.match((await askIndex({ base, pool: POOL, chainId: 998, fetch: hang, timeoutMs: 20 })).why, /did not answer in 0 seconds/);
});

test("the page says where its list comes from, and the slow reading's share", () => {
  assert.match(sourceNote({ ok: true, scannedTo: 3000 }), /index on the host up to block 3000.*read from the pool itself/);
  assert.match(sourceNote({ ok: false, why: "it could not be reached" }), /not available \(it could not be reached\).*reads the pool's events from the chain itself.*button reads further/);
  assert.equal(sourceNote(null), "");
  assert.equal(readShare({ from: 100, to: 199, read: 50 }), 50);
  assert.equal(readShare({ from: 100, to: 99, read: 0 }), 100);
  assert.equal(readShare({ from: 0, to: 9, read: 20 }), 100);
});

test("a node's refusal ethers cannot name is said in words, never as the raw error", () => {
  const raw = Object.assign(new Error("could not coalesce error (error={ \"code\": -32603 })"), { code: "UNKNOWN_ERROR", shortMessage: "could not coalesce error" });
  const words = readFailure(raw);
  assert.doesNotMatch(words, /coalesce/);
  assert.match(words, /did not answer this read/);
  // Said for every caller alike: a section with no button must not be told to press one.
  assert.doesNotMatch(words, /button|nothing found so far is lost/);
  assert.match(readFailure({ shortMessage: "missing response" }), /did not answer/);
  assert.equal(readFailure({ code: "ACTION_REJECTED" }), "Cancelled in the wallet.");
});

test("a send the index names is shown only when its receipt has it as the pool wrote it", () => {
  const ev = eventOf(paid);
  const word = (hex) => `0x${hex.slice(2).toLowerCase().padStart(64, "0")}`;
  const log = { address: POOL, topics: [SHARE_PAID, CODE, word(PARTNER)],
    data: `0x${(123456).toString(16).padStart(64, "0")}${"1".padStart(64, "0")}` };
  const ctx = { pool: POOL, topic: SHARE_PAID };
  assert.equal(sendInReceipt({ status: 1, logs: [log] }, ev, ctx), true);
  assert.equal(sendInReceipt({ status: 1, logs: [{ ...log, address: PARTNER }] }, ev, ctx), false);
  assert.equal(sendInReceipt({ status: 1, logs: [{ ...log, data: log.data.replace(/1$/, "0") }] }, ev, ctx), false);
  assert.equal(sendInReceipt({ status: 1, logs: [{ ...log, topics: [SHARE_PAID, CODE, word(AUTHOR)] }] }, ev, ctx), false);
  assert.equal(sendInReceipt({ status: 0, logs: [log] }, ev, ctx), false);
  assert.equal(sendInReceipt(null, ev, ctx), false);
});

test("the codes' note names the index, a reading that failed, and no blocks read", () => {
  const viaIndex = scanNote({ from: 105, to: 3000, latest: 3000, source: { ok: true, scannedTo: 2990 } });
  assert.match(viaIndex, /index on the host up to block 2990.*blocks 105 to 3000\. That is the pool's first block\./);
  const slow = scanNote({ from: 2000, to: 3000, latest: 3000, source: { ok: false, why: "it could not be reached" }, failed: "the chain node did not answer" });
  assert.match(slow, /not available.*blocks 2000 to 3000\. Older ones are not listed.*could not be read: the chain node did not answer\./);
  assert.match(scanNote({ from: 3001, to: 3000, latest: 3000, source: { ok: false, why: "x" } }), /No blocks could be read yet/);
  assert.match(codesHtml({ rows: [], sharesDue: 0n, scan: "", more: false }), /Read the newest blocks/);
  assert.match(codesHtml({ rows: [], sharesDue: 0n, scan: "" }), /Load earlier blocks/);
});

test("the stops' note says every stop since the pool was made when the index named them", () => {
  assert.match(stopScanNote({ floor: 105, back: 104, latest: 3000, head: 3000, done: true, sinceCreation: true }), /since the pool was made/);
  assert.match(stopScanNote({ floor: 105, back: 104, latest: 3000, head: 3000, done: true }), /last 14 days/);
});

test("the panels ask the index, and keep reading the chain without it", () => {
  for (const view of ["simowner-codes.js", "simstopscan.js", "simcodes.js"]) {
    const src = readFileSync(join(here, "../views", view), "utf8");
    assert.match(src, /askIndex\(/, view);
    assert.match(src, /getLogs\(/, view);
  }
  // Each candidate is read from the chain: codes by codeOf, stops by payout(id), sends by their receipt.
  assert.match(readFileSync(join(here, "../views/simcodes.js"), "utf8"), /getTransactionReceipt/);
  assert.match(readFileSync(join(here, "../views/simoperator.js"), "utf8"), /pool\.payout\(id\)/);
  assert.match(readFileSync(join(here, "../views/simowner-codes.js"), "utf8"), /pool\.codeOf\(h\)/);
});

test("a node's rate limit gets the advice to wait, not to ask again at once", () => {
  const limited = { code: "UNKNOWN_ERROR", shortMessage: "could not coalesce error",
    message: 'could not coalesce error (error={ "code": -32005, "message": "rate limited" })' };
  assert.match(readFailure(limited), /rate limited.*wait a minute/i);
  assert.doesNotMatch(readFailure(limited), /did not answer this read/);
});

test("the sends the index named are checked newest first, a few at a time, and the rest wait for the next pass", () => {
  const ev = (block, tx, to = PARTNER) => ({ fromIndex: true, name: "SharePaid", block, index: 0, tx, args: { to } });
  const events = [ev(1, "0x01"), ev(2, "0x02"), ev(3, "0x03"), ev(3, "0x03"), ev(4, "0x04", AUTHOR),
    { ...ev(5, "0x05"), fromIndex: false }];
  assert.deepEqual(receiptsToAsk(events, PARTNER.toLowerCase(), new Map(), 2), ["0x03", "0x02"]);
  assert.deepEqual(receiptsToAsk(events, PARTNER, new Map([["0x03", {}], ["0x02", {}]]), 2), ["0x01"]);
});

test("the cabinet offers to go on while a send is left unchecked, even with every block read", () => {
  const src = readFileSync(join(here, "../views/simpartner.js"), "utf8");
  assert.match(src, /if \(!finished\(st\) \|\| leftOut > 0\)/);
});

test("a receipt that fails again and again goes to the back: the older sends are checked all the same", () => {
  const ev = (block, tx) => ({ fromIndex: true, name: "SharePaid", block, index: 0, tx, args: { to: PARTNER } });
  const events = [ev(1, "0x01"), ev(2, "0x02"), ev(3, "0x03")];
  // The two newest failed once: the oldest, never asked, comes first; then the failed ones, fewest failures first.
  const tries = new Map([["0x03", 1], ["0x02", 2]]);
  assert.deepEqual(receiptsToAsk(events, PARTNER, new Map(), 2, tries), ["0x01", "0x03"]);
});

test("a send the index named is confirmed, rejected or not checked, and only the last is offered again", () => {
  const word = (hex) => `0x${hex.slice(2).toLowerCase().padStart(64, "0")}`;
  const ev = (block, tx, amount) => ({ fromIndex: true, name: "SharePaid", block, index: 0, tx,
    args: { code: CODE, to: PARTNER, amount: BigInt(amount), sent: true } });
  const log = (amount) => ({ address: POOL, topics: [SHARE_PAID, CODE, word(PARTNER)],
    data: `0x${amount.toString(16).padStart(64, "0")}${"1".padStart(64, "0")}` });
  const chainSend = { name: "SharePaid", block: 9, index: 0, tx: "0x09", args: { code: CODE, to: PARTNER, amount: 1n, sent: true } };
  const events = [ev(1, "0x01", 5), ev(2, "0x02", 6), ev(3, "0x03", 7), chainSend];
  const checked = new Map([["0x01", { status: 1, logs: [log(5)] }], ["0x02", { status: 1, logs: [log(999)] }]]);
  const out = sortSends(events, PARTNER, checked, { pool: POOL, topic: SHARE_PAID });
  assert.deepEqual(out.events.map((e) => e.tx), ["0x01", "0x09"]);
  assert.equal(out.rejected, 1);
  assert.equal(out.unchecked, 1);
  // A rejected send is not asked for again: its receipt was read.
  assert.deepEqual(receiptsToAsk(events, PARTNER, checked, 40, new Map()), ["0x03"]);
});

test("the cabinet keeps a count of failed receipts and offers more only for sends not checked", () => {
  const src = readFileSync(join(here, "../views/simcodes.js"), "utf8");
  assert.match(src, /st\.tries\.set\(tx, \(st\.tries\.get\(tx\) \?\? 0\) \+ 1\)/);
  assert.match(src, /left: sorted\.unchecked/);
});
