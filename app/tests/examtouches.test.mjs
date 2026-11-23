// Review 12 of #336, M-4 and L-3: the exam's page with the stop book's touches not read, and the read of `stopBook()`.
// A failed read of the touches is not an empty list: the page says the account may be another and holds the trade
// buttons until a pass reads them. A refusal of `stopBook()` is "no book" only where a read every exam answers goes
// through on the same node; else it is the node's failure, asked again and then said with "Try again".
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readPass, stopBookOf } from "../lib/examreads.js";
import { readsFrom, tradeHold, SCREEN_TEXT } from "../lib/examscreen.js";

const BTC = 3;
const io = (touches) => ({ block: async () => ({ number: 1, timestamp: 1 }), account: async () => [], position: async () => 0n,
  touches, stop: async () => null, take: async () => null, oracle: async () => null, bbo: async () => null });

test("R12-C6 the read of the touches fails: the pass says they were not read, and holds no list for them", async () => {
  const got = await readPass(io(async () => { throw new Error("429"); }), { assets: [BTC], selected: BTC });
  assert.deepEqual(got.touches, []);
  assert.equal(got.touchesUnread, true);
  const fine = await readPass(io(async () => [{ asset: BTC }]), { assets: [BTC], selected: BTC });
  assert.deepEqual([fine.touches.length, fine.touchesUnread], [1, false]);
  const none = await readPass(io(undefined), { assets: [BTC], selected: BTC });
  assert.equal(none.touchesUnread, false, "an exam without a book has no touches to read");
  const odd = await readPass(io(async () => null), { assets: [BTC], selected: BTC });
  assert.equal(odd.touchesUnread, true, "an answer that is not a list is not none");
});

test("R12-C7 with the touches not read the page says the account may be another, and holds trading", () => {
  const got = { rules: { assets: [BTC] }, terms: {}, pending: {} };
  const unread = readsFrom(got, [], [], true);
  assert.equal(unread.touchesUnread, true);
  assert.equal(tradeHold(unread), SCREEN_TEXT.touchesUnread);
  assert.match(SCREEN_TEXT.touchesUnread, /could not be read.*account may be different.*held until they are read/s);
  assert.equal(tradeHold(readsFrom(got, [], [])), null);
  const src = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  assert.match(src, /readsFrom\([^;]*touches, touchesUnread\)/s);
  assert.match(src, /send\.disabled = Boolean\(hold\) \|\|/);
  assert.match(src, /\$\(`#ex-\$\{kind\}-set`, page\)\.disabled = Boolean\(hold\) \|\|/);
  assert.match(src, /id="ex-touches-retry">Try again</);
  assert.match(src, /if \(trades && tradeHold\(st\.snap\?\.reads\)\)/);
});

const refusal = () => Object.assign(new Error("missing revert data"), { code: "CALL_EXCEPTION", data: null,
  info: { error: { code: 3, message: "execution reverted" } } });

test("R12-S1 a refusal is no book only where the control read answers on the same node", async () => {
  const wait = async () => {};
  assert.equal(await stopBookOf(async () => { throw refusal(); }, { wait, control: async () => 1n }), null);
  let n = 0;
  await assert.rejects(stopBookOf(async () => { n++; throw new Error("timeout"); }, { wait, control: async () => 1n }),
    /did not answer whether this exam has a stop book/);
  assert.equal(n, 3);
  n = 0;
  assert.equal(await stopBookOf(async () => { if (++n < 3) throw new Error("x"); return "0xabc"; }, { wait }), "0xabc");
});

test("R12-S3 a refusal whose control read fails too is the node's failure: asked again, then Try again", async () => {
  const wait = async () => {};
  const e = Object.assign(new Error("call failed"), { code: "CALL_EXCEPTION", info: { error: { message: "upstream: execution reverted by gateway timeout" } } });
  let controls = 0;
  await assert.rejects(stopBookOf(async () => { throw e; }, { wait, control: async () => { controls++; throw e; } }),
    /did not answer whether this exam has a stop book/);
  assert.equal(controls, 3);
  // Failed, then the node came back: the second pass's control answers, and the refusal is taken for no book.
  let k = 0;
  assert.equal(await stopBookOf(async () => { throw refusal(); }, { wait, control: async () => { if (++k < 2) throw e; return 1n; } }), null);
  // Without a control no refusal is taken for no book.
  await assert.rejects(stopBookOf(async () => { throw refusal(); }, { wait }), /did not answer/);
});
