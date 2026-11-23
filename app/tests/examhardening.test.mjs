// node --test app/tests/examhardening.test.mjs
//
// Review 9 of #336, the trading screen: the header with a touched stop waiting for its settle (M5), the read of the
// exam's stop book (M6), and what the page says after a settle (L5).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { examAccount } from "../lib/exam.js";
import { accountFigures } from "../lib/examscreen.js";
import { stopBookOf, settledText, BOOK_TRIES } from "../lib/examreads.js";

const USD = 1_000_000n, DAY = 86_400, BTC = 3;
const base = (touches, size = 100n) => ({
  status: 1, step: 0, cash: 10_000n * USD, dayStartEquity: 10_000n * USD, tradingDays: 0, deadline: 30 * DAY,
  drawdownKind: 0, peakSnapshot: 10_000n * USD, lastFillAt: 0, pending: { asset: 0, isBuy: false, sz: 0n, placedBlock: 0n },
  maxWorseBps: 150,
  rules: { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 1000, assets: [BTC], assetLeverageX100: [1000] },
  terms: { capital: 10_000n * USD, targetsBps: [1000], stepDuration: 30 * DAY, maxIdle: 0, minTradingDays: 0, costPpm: 450, fillDelay: 4, fillWindow: 10 },
  positions: [{ asset: BTC, size, mark: 1_000_000n, stop: { px: 950_000n, sz: 0n, armedAt: 400 } }], touches,
});
const touch = (px = 949_000n) => [{ asset: BTC, isTake: false, level: 950_000n, px, book: 0xffffffff, blk: 498, time: DAY - 2, pos: 7 }];

test("with a touched stop waiting, the header counts the account after its settle, not the position open at the mark", () => {
  const a = examAccount(base(touch()), { now: DAY, block: 500 });
  const b = examAccount(base([]), { now: DAY, block: 500 });
  assert.equal(a.positions[0].stop.state, "touched", "the positions table keeps what stands now");
  assert.equal(a.positions[0].size, 100n);
  // Sold 100 at the touch's 949 000 with a cost of 450 ppm: flat, the loss of the fall in the equity.
  const cost = (100n * 949_000n * 450n + 999_999n) / 1_000_000n;
  assert.equal(a.equity, 10_000n * USD + 100n * 949_000n - cost);
  assert.notEqual(a.equity, b.equity);
  assert.equal(a.notional, 0n, "the stop closes the position");
  assert.equal(a.graduate.refusal, "TargetNotMet", "flat after the settle: what stands in the way is the target, not NotFlat");
  assert.equal(b.graduate.refusal, "NotFlat");
  assert.deepEqual({ touches: a.afterSettle.touches, filled: a.afterSettle.filled }, { touches: 1, filled: 1 });
  assert.equal(a.afterSettle.asMarked.equity, b.equity, "the account at the marks is still said, beside");
  assert.equal(b.afterSettle, null);
  const f = accountFigures(a);
  assert.match(f.afterSettle, /^After the settle of 1 touched stop or take: .* Before it, at the marks: equity /);
  assert.equal(accountFigures(b).afterSettle, null);
});

test("a touch that breaks a rule shows the breach before the settle comes", () => {
  // A long of 10 000 at the mark; the stop is touched at 880 000: the fall breaks the 6 % loss limit after the settle.
  // The cash is what is left of the capital after the buy, so the account at the mark is the capital.
  const reads = { ...base([{ ...touch(880_000n)[0] }], 10_000n), cash: 0n };
  const a = examAccount(reads, { now: DAY, block: 500 });
  const b = examAccount({ ...base([], 10_000n), cash: 0n }, { now: DAY, block: 500 });
  assert.equal(b.equity, 10_000n * USD);
  assert.equal(b.breach, "none");
  assert.equal(a.breach, "drawdown");
  assert.equal(a.graduate.refusal, "RuleBroken");
});

test("a touch the settle would not fill (the exam over at its time) changes nothing", () => {
  const reads = base([{ ...touch()[0], time: 31 * DAY }]);
  const a = examAccount(reads, { now: DAY, block: 500 });
  const b = examAccount(base([]), { now: DAY, block: 500 });
  assert.equal(a.equity, b.equity);
  assert.equal(a.afterSettle.filled, 0);
});

const reverted = () => Object.assign(new Error("missing revert data"), { code: "CALL_EXCEPTION", data: null,
  info: { error: { code: 3, message: "execution reverted" } } });
const busy = () => Object.assign(new Error("missing revert data"), { code: "CALL_EXCEPTION", data: null,
  info: { error: { code: -32005, message: "rate limited" } } });
const down = () => Object.assign(new Error("network error"), { code: "NETWORK_ERROR" });

test("only the exam's refusal of stopBook() means no book; a node that fails is asked again, then said", async () => {
  const waits = [];
  const wait = async (ms) => { waits.push(ms); };
  const book = "0x" + "b0".repeat(20);
  assert.equal(await stopBookOf(async () => book, { wait }), book);
  assert.equal(await stopBookOf(async () => { throw reverted(); }, { wait }), null, "an exam made before the book");
  assert.deepEqual(waits, []);
  // Failed twice, then answered: the book, not a session without one.
  let n = 0;
  assert.equal(await stopBookOf(async () => { if (n++ < 2) throw (n === 1 ? busy() : down()); return book; }, { wait }), book);
  assert.equal(waits.length, 2);
  // Failed every time: an error the page says (and offers to try again), never "no book".
  n = 0;
  await assert.rejects(stopBookOf(async () => { n++; throw busy(); }, { wait }), /did not answer whether this exam has a stop book/);
  assert.equal(n, BOOK_TRIES);
});

test("the screen reads the stop book through stopBookOf, with no catch that turns a failure into no book", () => {
  const src = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  assert.match(src, /stopBookOf\(\(\) => exam\.stopBook\(\)\)/);
  assert.doesNotMatch(src, /stopBook\(\)\.catch\(/);
});

test("Settled. only when no touch is left; a settle done in part says how many are left", () => {
  assert.equal(settledText([]), "Settled. The account now shows the stop or take filled at the price of its touch.");
  assert.equal(settledText([{}, {}]), "Settled in part: 2 touched stops or takes still to carry out. Press Settle again to carry out the rest (anyone may).");
  assert.match(settledText([{}]), /^Settled in part: 1 touched stop or take still/);
  assert.doesNotMatch(settledText(null), /^Settled\./, "the touches left not read: not said as settled");
  const src = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  assert.match(src, /settledText\(left\)/);
});

test("the target line says reached once the equity is at the target or past it, never a negative amount to go", () => {
  const at = (cash) => accountFigures(examAccount({ ...base([]), cash, positions: [] }, { now: DAY, block: 500 })).target;
  const below = at(10_500n * USD), equal = at(11_000n * USD), past = at(11_250n * USD);
  assert.equal(below.reached, false);
  assert.match(below.progress, /^to go /);
  assert.doesNotMatch(below.progress, /-/);
  for (const t of [equal, past]) {
    assert.equal(t.reached, true);
    assert.equal(t.progress, "reached");
    assert.doesNotMatch(t.toGo, /-/);
  }
  const src = readFileSync(new URL("../views/exam.js", import.meta.url), "utf8");
  assert.match(src, /esc\(f\.target\.progress\)/);
  assert.doesNotMatch(src, /to go \$\{esc\(f\.target\.toGo\)\}/);
});
