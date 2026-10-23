// node --test app/tests/multi.test.mjs
//
// The pages of a pool of many traders, for its traders (app/lib/multi.js, app/views/multi.js). The contracts are
// drafts at 378fae1f; their ABI as `forge inspect` gave it is app/tests/multi-abi.json, and every fragment
// the pages use is held to it word for word. What a page shows as open is held here case by case: a call anyone
// may make with no keeper to make it is a button, and the reason it is or is not open is said.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { SOURCE, MULTI_ABI, EXAM_ABI, EXAM_STATUS, SEAT_STATE, EXAM_STATE, AFTER_TARGET, planView, boundsView, examActions,
  seatActions, parseInvite, sizeUnits, coinAmount, myExams, rememberExam, span, CHECKPOINT_WINDOW, DAY } from "../lib/multi.js";
import { CONFIG } from "../config.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const FIXTURE = JSON.parse(text("./multi-abi.json"));
const POOL = "0x00000000000000000000000000000000000000A1";
const ME = "0x00000000000000000000000000000000000000D1";
const OTHER = "0x00000000000000000000000000000000000000D2";
const ZERO = "0x0000000000000000000000000000000000000000";
const open = (list) => Object.fromEntries(list.map((a) => [a.id, a.enabled]));
const why = (list, id) => list.find((a) => a.id === id).why;

test("every fragment the pages use is the drafts' own, word for word", () => {
  assert.equal(FIXTURE.source, SOURCE);
  assert.equal(SOURCE, "378fae1fd8fbd0485528fa97350baae19149159c");
  for (const f of MULTI_ABI) assert.ok(FIXTURE.MultiPool.includes(f), f);
  for (const f of EXAM_ABI) assert.ok(FIXTURE.PaperExam.includes(f), f);
  // Every call the pages make is in what they hand ethers: a name missing there would fail only in a browser.
  const named = (abi) => new Set(abi.map((f) => f.split("(")[0].split(" ")[1]));
  const multi = named(MULTI_ABI);
  const exam = named(EXAM_ABI);
  const view = text("../views/multi.js");
  for (const m of view.matchAll(/(?<![\w.])pool\.(\w+)\(/g)) assert.ok(multi.has(m[1]), `MultiPool.${m[1]}`);
  for (const m of view.matchAll(/(?<![\w.])exam\.(\w+)\(/g)) assert.ok(exam.has(m[1]), `PaperExam.${m[1]}`);
  for (const m of view.matchAll(/chain\.write\("multi", [^,]+, (?:"(\w+)"|id)/g)) if (m[1]) assert.ok(multi.has(m[1]), m[1]);
  for (const id of ["open", "rearm", "reopen", "finish", "sweep", "noteActivity", "hitTarget", "closeIdle", "endSeat", "buy", "takeFree", "claim", "release"]) {
    assert.ok(multi.has(id), id);
  }
  for (const id of ["place", "fill", "checkpoint", "breach", "expire", "graduate"]) assert.ok(exam.has(id), id);
  // The events the pages read their answers from, and the errors the contract's refusals are named by.
  for (const f of ["ExamSold", "FreeExamTaken", "Claimed"]) assert.ok(MULTI_ABI.some((x) => x.startsWith(`event ${f}(`)), f);
  assert.equal(MULTI_ABI.filter((x) => x.startsWith("error ")).length, FIXTURE.MultiPool.filter((x) => x.startsWith("error ")).length);
  assert.equal(EXAM_ABI.filter((x) => x.startsWith("error ")).length, FIXTURE.PaperExam.filter((x) => x.startsWith("error ")).length);
  // The states, in the drafts' order.
  assert.deepEqual(EXAM_STATUS, ["None", "Active", "Passed", "Breached", "Expired"]);
  assert.deepEqual(SEAT_STATE, ["None", "Free", "Armed", "Funded", "Waiting"]);
  assert.deepEqual(EXAM_STATE, ["None", "Sold", "Claimed", "Released"]);
  assert.equal(AFTER_TARGET.length, 3);
});

const PLAN = {
  menu: [{ size: 5_000_000n, price: 50_000n }, { size: 25_000_000n, price: 250_000n }],
  examRules: { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 500, assets: [3, 4, 0] },
  exam: { capital: 0n, targetsBps: [1000, 500], stepDuration: 30 * DAY, maxIdle: 14 * DAY, minTradingDays: 3, costPpm: 950, fillDelay: 2, fillWindow: 300 },
  fundedRules: { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 300, assets: [3] },
  fundedTargetBps: 1000, traderShareBps: 8000, afterTarget: 2, inWorkBps: 8000, maxSeats: 16, claimWindow: 7 * DAY,
  term: 180 * DAY, maxRetakes: 0, fundedIdle: 30 * DAY, freePerMonth: 3, inviter: ME,
};

test("the plan is read as its creator set it, and test bounds are said to be test bounds", () => {
  const v = planView(PLAN);
  assert.deepEqual(v.menu, [{ line: 0, size: 5, price: 0.05 }, { line: 1, size: 25, price: 0.25 }]);
  assert.deepEqual(v.examRules, { dailyLoss: "3%", drawdown: "6%", leverage: "5×", assets: [3, 4, 0] });
  assert.deepEqual(v.exam, { targets: ["10%", "5%"], step: "30 days", idle: "14 days", tradingDays: 3, cost: "0.095%", fillDelay: 2, fillWindow: 300 });
  assert.equal(v.fundedRules.leverage, "3×");
  assert.equal(v.fundedTarget, "10%");
  assert.equal(v.afterTarget, "The account stays after its target, and its trader may take it again");
  assert.equal(v.retakes, "no limit");
  assert.deepEqual([v.traderShare, v.inWork, v.seats, v.claimWindow, v.term, v.fundedIdle, v.freePerMonth], ["80%", "80%", 16, "7 days", "180 days", "30 days", 3]);
  assert.equal(planView({ ...PLAN, afterTarget: 0, fundedTargetBps: 0 }).fundedTarget, "none");
  assert.equal(planView({ ...PLAN, afterTarget: 1 }).retakes, "—");
  assert.equal(planView({ ...PLAN, maxRetakes: 2 }).retakes, "2");
  const product = boundsView({ productBounds: true, minTerm: 180 * DAY, maxTerm: 730 * DAY, notice: 14 * DAY });
  assert.deepEqual(product, { test: false, words: "The product's bounds: a term of 180 days to 730 days, 14 days with nothing sold at the end." });
  const testing = boundsView({ productBounds: false, minTerm: 10_800, maxTerm: DAY, notice: 900 });
  assert.equal(testing.test, true);
  assert.equal(testing.words, "Test bounds, not the product's: a term of 3 hours to 1 day, 15 minutes with nothing sold at the end. "
    + "A pool deployed to watch a whole term quickly.");
  assert.deepEqual([span(0), span(DAY), span(2 * DAY), span(3600), span(5400), span(600)], ["none", "1 day", "2 days", "1 hour", "1.5 hours", "10 minutes"]);
});

const NOW = 1_800_000_000;   // 08:00 UTC
const BLOCK = 1000;
const exam = (over = {}) => ({
  status: 1, step: 0, deadline: NOW + DAY, maxIdle: 0, lastFillAt: NOW - 60, trader: ME, agent: ZERO,
  pending: { asset: 3, isBuy: true, sz: 0n, placedBlock: 0 }, fillDelay: 2, fillWindow: 300, capital: 5_000_000n,
  targetsBps: [1000], equity: 5_000_000n, notional: 0n, violation: 0, tradingDays: 0, minTradingDays: 0,
  day: Math.floor(NOW / DAY) - 1, passedAt: 0, claimWindow: 3600, poolState: 1, ...over,
});

test("an order waits its blocks, fills in its window by anyone, and only the trader or their agent places one", () => {
  let a = examActions(exam(), ME, NOW, BLOCK);
  assert.equal(open(a).place, true);
  assert.equal(open(a).fill, false);
  assert.equal(why(a, "fill"), "no order is waiting");
  assert.equal(open(examActions(exam(), OTHER, NOW, BLOCK)).place, false);
  assert.equal(open(examActions(exam(), null, NOW, BLOCK)).place, false);
  assert.equal(open(examActions(exam({ agent: OTHER }), OTHER, NOW, BLOCK)).place, true, "the trader's agent places too");
  const placed = { asset: 3, isBuy: true, sz: 500n, placedBlock: BLOCK };   // fillable from BLOCK + 2 to BLOCK + 301
  for (const [block, fills] of [[BLOCK + 1, false], [BLOCK + 2, true], [BLOCK + 301, true], [BLOCK + 302, false]]) {
    a = examActions(exam({ pending: placed }), OTHER, NOW, block);
    assert.equal(open(a).fill, fills, String(block));
  }
  assert.equal(why(examActions(exam({ pending: placed }), OTHER, NOW, BLOCK + 1), "fill"), `it can be filled from block ${BLOCK + 2}`);
  assert.equal(why(examActions(exam({ pending: placed }), OTHER, NOW, BLOCK + 302), "fill"), `its window closed at block ${BLOCK + 301}`);
  // One order waits at a time: a new one only once the waiting one's window has closed.
  assert.equal(open(examActions(exam({ pending: placed }), ME, NOW, BLOCK + 301)).place, false);
  assert.equal(open(examActions(exam({ pending: placed }), ME, NOW, BLOCK + 302)).place, true);
  // Past its time nothing is placed or filled, and anyone may end it.
  a = examActions(exam({ pending: placed, deadline: NOW - 1 }), ME, NOW, BLOCK + 5);
  assert.deepEqual([open(a).place, open(a).fill, open(a).expire], [false, false, true]);
  a = examActions(exam({ maxIdle: 3600, lastFillAt: NOW - 3601 }), OTHER, NOW, BLOCK);
  assert.equal(open(a).expire, true, "a step that went too long without a fill");
  assert.equal(open(examActions(exam({ deadline: 0 }), OTHER, NOW, BLOCK)).expire, false, "no deadline, no idle limit");
});

test("the day's snapshot, a breach and the pass open exactly when the contract would take them", () => {
  const midnight = Math.floor(NOW / DAY) * DAY + DAY;
  assert.equal(open(examActions(exam(), OTHER, midnight + 60, BLOCK)).checkpoint, true);
  assert.equal(open(examActions(exam(), OTHER, midnight + CHECKPOINT_WINDOW, BLOCK)).checkpoint, false);
  assert.equal(open(examActions(exam({ day: Math.floor(midnight / DAY) }), OTHER, midnight + 60, BLOCK)).checkpoint, false, "taken already");
  assert.equal(open(examActions(exam({ violation: 3 }), OTHER, NOW, BLOCK)).breach, true);
  assert.equal(why(examActions(exam({ violation: 3 }), OTHER, NOW, BLOCK), "breach"), "Leverage is broken at this moment");
  assert.equal(open(examActions(exam(), OTHER, NOW, BLOCK)).breach, false);
  // The pass: the trader or their agent, nothing open, no rule broken, the target met, the days traded.
  const ready = exam({ equity: 5_500_000n });
  assert.equal(open(examActions(ready, ME, NOW, BLOCK)).graduate, true);
  assert.equal(open(examActions(exam({ equity: 5_500_000n, agent: OTHER }), OTHER, NOW, BLOCK)).graduate, true);
  for (const [over, words] of [[{ equity: 5_499_999n }, "equity has to reach 5.5 USDC"], [{ equity: 5_500_000n, notional: 1n }, "everything has to be closed first"],
    [{ equity: 5_500_000n, minTradingDays: 2, tradingDays: 1 }, "2 trading days are needed"]]) {
    const a = examActions(exam(over), ME, NOW, BLOCK);
    assert.equal(open(a).graduate, false, words);
    assert.equal(why(a, "graduate"), words);
  }
  assert.equal(open(examActions(exam({ equity: 5_500_000n, violation: 1 }), ME, NOW, BLOCK)).graduate, false);
  assert.equal(open(examActions(ready, OTHER, NOW, BLOCK)).graduate, false, "not somebody else");
  // The second step's target is its own.
  assert.equal(why(examActions(exam({ step: 1, targetsBps: [1000, 500], equity: 5_000_000n }), ME, NOW, BLOCK), "graduate"), "equity has to reach 5.25 USDC");
});

test("a pass is taken by its trader in its window, and a size nobody will use is given back by anyone", () => {
  const passed = exam({ status: 2, passedAt: NOW - 100 });
  assert.equal(open(examActions(passed, ME, NOW, BLOCK)).claim, true);
  assert.equal(open(examActions(exam({ status: 2, passedAt: NOW - 100, agent: OTHER }), OTHER, NOW, BLOCK)).claim, false, "the trader, not their agent");
  assert.equal(open(examActions(passed, ME, NOW - 100 + 3601, BLOCK)).claim, false, "past the window");
  assert.equal(open(examActions(exam({ status: 2, passedAt: NOW - 100, poolState: 2 }), ME, NOW, BLOCK)).claim, false, "taken already");
  assert.equal(open(examActions(passed, ME, NOW, BLOCK)).release, false, "a pass in its window keeps its size");
  assert.equal(open(examActions(passed, OTHER, NOW - 100 + 3601, BLOCK)).release, true);
  for (const status of [3, 4]) assert.equal(open(examActions(exam({ status }), OTHER, NOW, BLOCK)).release, true, String(status));
  assert.equal(open(examActions(exam({ status: 3, poolState: 3 }), OTHER, NOW, BLOCK)).release, false, "given back already");
  assert.equal(open(examActions(exam(), OTHER, NOW, BLOCK)).release, false, "a running exam keeps its size");
  // Nothing that moves an exam is open once it has ended.
  const ended = open(examActions(exam({ status: 3, violation: 1, pending: { asset: 3, isBuy: true, sz: 5n, placedBlock: BLOCK } }), ME, NOW, BLOCK + 5));
  assert.deepEqual([ended.place, ended.fill, ended.checkpoint, ended.breach, ended.expire, ended.graduate], [false, false, false, false, false, false]);
});

test("a seat's calls are anyone's where the contract lets anyone, and its trader's where only they may", () => {
  const seat = (over) => ({ state: 2, trader: ME, armedAt: NOW - 30, restUntil: NOW - 1, waitUntil: 0, targetHit: false, ...over });
  const ctx = (over) => ({ me: OTHER, now: NOW, armWait: 60, afterTarget: 1, ended: false, stage: 2, ...over });
  let a = open(seatActions(seat(), ctx()));
  assert.deepEqual([a.open, a.rearm], [true, false]);
  assert.equal(open(seatActions(seat({ armedAt: NOW - 60 }), ctx())).rearm, true);
  assert.equal(open(seatActions(seat({ restUntil: NOW + 30 }), ctx())).open, false, "a freed seat rests");
  a = open(seatActions(seat({ state: 3 }), ctx()));
  assert.deepEqual([a.hitTarget, a.closeIdle, a.noteActivity, a.endSeat, a.finish, a.open], [true, true, true, false, false, false]);
  assert.equal(open(seatActions(seat({ state: 3 }), ctx({ afterTarget: 0 }))).hitTarget, false, "no target in this plan");
  assert.equal(open(seatActions(seat({ state: 3 }), ctx({ ended: true }))).endSeat, true);
  assert.equal(open(seatActions(seat({ state: 3 }), ctx({ stage: 0 }))).finish, true, "a settled stage");
  assert.equal(open(seatActions(seat({ state: 4, waitUntil: NOW + 10 }), ctx({ me: ME }))).reopen, true);
  assert.equal(open(seatActions(seat({ state: 4, waitUntil: NOW + 10 }), ctx())).reopen, false, "only its trader");
  assert.equal(open(seatActions(seat({ state: 4, waitUntil: NOW - 1 }), ctx({ me: ME }))).reopen, false);
  assert.equal(open(seatActions(seat({ state: 1 }), ctx())).sweep, true);
  assert.equal(open(seatActions(seat({ state: 3 }), ctx())).sweep, false);
});

test("an invitation is checked before it is sent: this pool, this wallet, in time, a line of the menu, a whole signature", () => {
  const inv = { pool: POOL, trader: ME, line: 1, nonce: 7, expiresAt: NOW + 3600, v: 27, r: `0x${"ab".repeat(32)}`, s: `0x${"cd".repeat(32)}` };
  const check = (over, ctx = {}) => parseInvite(JSON.stringify({ ...inv, ...over }), { pool: POOL, me: ME, now: NOW, lines: 3, ...ctx });
  assert.deepEqual(check({}), { invite: { line: 1, nonce: 7, expiresAt: NOW + 3600, v: 27, r: inv.r, s: inv.s } });
  assert.deepEqual(check({ pool: POOL.toLowerCase(), trader: ME.toLowerCase() }).invite.line, 1);
  for (const [over, ctx, error] of [
    [{ pool: OTHER }, {}, "This invitation is for another pool."],
    [{}, { me: OTHER }, "This invitation names another wallet: only its trader can take it."],
    [{}, { me: null }, "Connect the wallet the invitation names."],
    [{ expiresAt: NOW - 1 }, {}, "This invitation has lapsed."],
    [{ line: 3 }, {}, "This invitation names a size the menu does not have."],
    [{ line: -1 }, {}, "This invitation names a size the menu does not have."],
    [{ nonce: "7" }, {}, "This invitation has no number."],
    [{ v: 1 }, {}, "This invitation's signature is not whole."],
    [{ r: "0x12" }, {}, "This invitation's signature is not whole."],
  ]) assert.equal(check(over, ctx).error, error, JSON.stringify(over));
  assert.equal(parseInvite("not json", { pool: POOL, me: ME, now: NOW, lines: 3 }).error, "This is not an invitation: paste the line of JSON it was given as.");
  assert.equal(parseInvite("null", { pool: POOL, me: ME, now: NOW, lines: 3 }).error, "This is not an invitation.");
});

test("an order's size is the asset's own units, and comes back as the same amount", () => {
  assert.equal(sizeUnits("0.005", 5), 500n);
  assert.equal(sizeUnits("1", 2), 100n);
  assert.equal(sizeUnits(" 12.5 ", 1), 125n);
  for (const [t, d, m] of [["0.0000001", 5, /at most 5 decimals/], ["0", 5, /above zero/], ["-1", 5, /plain number/], ["1e3", 5, /plain number/], ["abc", 2, /plain number/]]) {
    assert.throws(() => sizeUnits(t, d), m, t);
  }
  assert.equal(coinAmount(500n, 5), "0.005");
  assert.equal(coinAmount(-1_234_567n, 5), "-12.34567");
  assert.equal(coinAmount(0n, 4), "0");
  assert.equal(coinAmount(100n, 2), "1");
  assert.equal(coinAmount(7n, 0), "7");
});

test("the exams bought from a browser are kept by pool and wallet, and a broken store loses nothing else", () => {
  const box = new Map();
  const store = { getItem: (k) => box.get(k) ?? null, setItem: (k, v) => box.set(k, v) };
  const exam1 = "0x00000000000000000000000000000000000000E1";
  const exam2 = "0x00000000000000000000000000000000000000E2";
  assert.deepEqual(myExams(store, POOL, ME), []);
  rememberExam(store, POOL, ME, exam1);
  rememberExam(store, POOL, ME, exam2);
  rememberExam(store, POOL, ME, exam1.toLowerCase());
  assert.deepEqual(myExams(store, POOL, ME), [exam2, exam1], "newest first, once each");
  assert.deepEqual(myExams(store, POOL, OTHER), []);
  assert.deepEqual(myExams(store, OTHER, ME), []);
  box.set([...box.keys()][0], "{broken");
  assert.deepEqual(myExams(store, POOL, ME), []);
  const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.deepEqual(myExams(throwing, POOL, ME), []);
  assert.deepEqual(rememberExam(throwing, POOL, ME, exam1), [exam1]);
  assert.deepEqual(myExams(null, POOL, ME), []);
});

test("a pool is listed from its deployment's record, and a record with none listed here turns this red", () => {
  const records = readdirSync(new URL("../../deployments/", import.meta.url)).filter((f) => /^testnet-.*\.json$/.test(f))
    .map((f) => JSON.parse(text(`../../deployments/${f}`))).filter((r) => r.MultiPool);
  assert.ok(Array.isArray(CONFIG.multi));
  assert.equal(CONFIG.multi.length, records.length, "every deployment of a pool of many traders is listed, and nothing else");
  for (const d of CONFIG.multi) {
    const r = records.find((x) => x.label === d.label);
    assert.ok(r, d.label);
    assert.equal(d.pool, r.MultiPool);
    assert.equal(d.factory, r.PoolFactory);
  }
  // Linked from no page while it is a draft: reached by its address only.
  const app = text("../app.js");
  assert.match(app, /\[\/\^#\\\/multi\$\/, \(m, page\) => multiListView\(page\)\],/);
  assert.match(app, /\[\/\^#\\\/exam\\\/\(0x\[0-9a-fA-F\]\{40\}\)\$\/, \(m, page\) => examView\(m\[1\], page\)\],/);
  assert.doesNotMatch(text("../index.html"), /#\/multi|#\/exam/);
  const view = text("../views/multi.js");
  assert.match(view, /const DRAFT = "A draft: the contracts behind these pages are not on main and not reviewed by an audit\.";/);
  assert.match(view, /\$\{productBounds \? "" : badge\("Test bounds", "warn"\)\}/);
  assert.match(view, /\$\{bounds\.test \? badge\("Test bounds", "warn"\) : ""\}/);
  assert.equal((view.match(/esc\(DRAFT\)/g) || []).length, 4, "said on every page");
  // A purchase pays the pool its price and the platform its fee, both taken by the pool: one approval for both.
  assert.match(view, /await chain\.approveIfNeeded\(d\.pool, BigInt\(plan\.menu\[m\.line\]\.price\) \+ BigInt\(fee\)\);/);
});
