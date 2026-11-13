// node --test app/tests/simpool-chain.test.mjs
//
// The page of a simulated pool drawn from that contract's own events, not from made-up ones: test/sim/SimPool.t.sol
// (test_page_recordedChain) runs a pool on the simulator and records every event of it the page reads, as the node
// would give it (topics and data untouched), with the getters' answers after. That test fails if the file is not
// what the contract emits today. This one decodes those logs by the library's own ABI lines and checks the page
// the way a stranger would check a page of payouts: the total paid is the sum of the payment events, each with its
// transaction, and it is the contract's own paidTotal.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  SIM_POOL_ABI, SIM_POOL_STOP_RESERVE_ABI, PAPER_EXAM_EVENTS_ABI, PAYOUT_DELAY, STOP_RESERVE, MIN_TERM, simPoolPage,
  activityCounts, accountsToRead,
} from "../lib/simpool.js";

const CHAIN = JSON.parse(readFileSync(new URL("./fixtures/sim-page-chain.json", import.meta.url), "utf8"));
const LARGE_PAYOUT = 800_000_000n;

/** The event lines of the library's ABI, by name: each parameter's type, name and whether it is a topic. */
const EVENTS = new Map([...SIM_POOL_ABI, ...SIM_POOL_STOP_RESERVE_ABI, ...PAPER_EXAM_EVENTS_ABI].filter((l) => l.startsWith("event ")).map((l) => {
  const [, name, body] = l.match(/^event (\w+)\((.*)\)$/);
  const params = body ? body.split(", ").map((p) => {
    const words = p.split(" ");
    return { type: words[0], indexed: words.includes("indexed"), name: words[words.length - 1] };
  }) : [];
  return [name, params];
}));

function word(type, hex) {
  if (type === "address") return `0x${hex.slice(24)}`;
  if (type === "bool") return BigInt(`0x${hex}`) !== 0n;
  if (type === "bytes32") return `0x${hex}`;
  if (/^uint\d+$/.test(type)) return BigInt(`0x${hex}`);
  if (type === "int256") return BigInt.asIntN(256, BigInt(`0x${hex}`));
  throw new Error(`no decoding for ${type}`);
}

/** One raw log decoded by the ABI line of its name: topics for the indexed parameters, the data's words for the rest,
 *  a bytes32[] by its offset. Every word of the data must be accounted for. */
function decode(log) {
  const params = EVENTS.get(log.name);
  assert.ok(params, `${log.name} is not an event of the library's ABI`);
  const topics = params.filter((p) => p.indexed);
  assert.equal(log.topics.length, 1 + topics.length, `${log.name}: its topics`);
  const data = log.data.slice(2);
  const words = data.match(/.{64}/g) ?? [];
  assert.equal(words.length * 64, data.length, `${log.name}: whole words`);
  const args = {};
  let head = 0;
  let used = 0;
  topics.forEach((p, i) => { args[p.name] = word(p.type, log.topics[i + 1].slice(2)); });
  for (const p of params.filter((x) => !x.indexed)) {
    if (p.type === "bytes32[]") {
      const at = Number(BigInt(`0x${words[head]}`)) / 32;
      const n = Number(BigInt(`0x${words[at]}`));
      args[p.name] = words.slice(at + 1, at + 1 + n).map((w) => `0x${w}`);
      used += 1 + n;
    } else {
      args[p.name] = word(p.type, words[head]);
    }
    head += 1;
    used += 1;
  }
  assert.equal(used, words.length, `${log.name}: every word of its data read`);
  return { name: log.name, args, block: log.block, tx: log.tx, index: log.index };
}

const LOGS = CHAIN.events.map(decode);
const TIMES = new Map(CHAIN.events.map((e) => [e.block, e.time]));
const page = simPoolPage({
  pool: CHAIN.pool, usdc: CHAIN.usdc, chainId: CHAIN.chainId, reads: CHAIN.reads, events: LOGS, times: TIMES,
  now: CHAIN.now, scan: CHAIN.scan,
});

test("every recorded event decodes by the library's ABI lines, word for word", () => {
  assert.ok(LOGS.length >= 20, `${LOGS.length} events`);
  const names = new Set(LOGS.map((e) => e.name));
  for (const n of ["ExamSold", "Funded", "PayoutDue", "PayoutStopped", "StopObjected", "PayoutPaid", "PayoutHeld"]) {
    assert.ok(names.has(n), n);
  }
});

test("paid in total is the sum of the payment events, and the contract's own paidTotal", () => {
  const events = LOGS.filter((e) => e.name === "PayoutPaid");
  const sum = events.reduce((s, e) => s + e.args.amount, 0n);
  assert.equal(page.total.total, sum);
  assert.equal(page.total.total, BigInt(CHAIN.reads.paidTotal));
  assert.equal(page.total.total, LARGE_PAYOUT);
  assert.equal(page.total.complete, true);
  assert.equal(page.complete, true);
  assert.deepEqual(page.total.payments.map((p) => [p.id, p.amount, p.tx]), events.map((e) => [Number(e.args.id), e.args.amount, e.tx]));
  // The event's own running total agrees too: what a reader of one transaction sees.
  assert.equal(events.at(-1).args.paidTotal, sum);
});

test("the paid payout carries its proof: its id, its transaction, its amount, when asked and when paid, from the pool", () => {
  assert.equal(page.proofs.length, 1);
  const [proof] = page.proofs;
  const paid = LOGS.find((e) => e.name === "PayoutPaid");
  const due = LOGS.find((e) => e.name === "PayoutDue" && e.args.id === paid.args.id);
  assert.equal(proof.id, Number(paid.args.id));
  assert.equal(proof.tx, paid.tx);
  assert.equal(proof.amount, paid.args.amount);
  assert.equal(proof.from, CHAIN.pool);
  assert.equal(proof.token, CHAIN.usdc);
  assert.equal(proof.to.toLowerCase(), paid.args.trader);
  assert.equal(proof.requestTx, due.tx);
  assert.ok(proof.paidAt - proof.requestedAt >= PAYOUT_DELAY, "paid no sooner than the payout delay (72 hours) after it was asked for");
});

test("the stopped payout and the held one are apart from the paid, each with what it needs", () => {
  assert.deepEqual(Object.fromEntries(Object.entries(page.byStatus).map(([k, v]) => [k, v.length])),
    { waiting: 0, queued: 0, approval: 0, paid: 1, stopped: 1, held: 1, refused: 0 });
  const [stopped] = page.byStatus.stopped;
  assert.equal(stopped.stop.reason, 1);
  assert.equal(stopped.stop.trades.length, 2);
  assert.ok(stopped.objection, "the trader's objection is on the page");
  assert.equal(stopped.stopNote.state, "window");
  assert.equal(stopped.reserveEnds, stopped.stoppedAt + STOP_RESERVE);
  const [held] = page.byStatus.held;
  assert.ok(held.heldAt);
  const setAside = LOGS.filter((e) => e.name === "PartnerPaid" && !e.args.sent).reduce((t, e) => t + e.args.amount, 0n);
  assert.equal(page.held, LARGE_PAYOUT + setAside, "the refused payout and the refused owner's part");
  assert.equal(page.reserved, LARGE_PAYOUT, "the stopped payout's money, set aside for its 14 days");
  assert.equal(page.counters.stopped, 1n);
  assert.deepEqual(page.gaps, []);
});

test("the exams and the stages are all there, and the page says how far the chain was read", () => {
  assert.equal(page.exams.length, 43);
  assert.equal(page.stages.length, 3);
  assert.equal(page.stages.filter((s) => s.payoutId !== null).length, 3);
  assert.equal(page.scanText, `Read from block ${CHAIN.scan.from} to block ${CHAIN.scan.to}: every event of this pool `
    + "in that range is listed.");
  // No locked amount on the page; the term's date (lockedUntil) is not an amount.
  assert.ok(!JSON.stringify(Object.keys(page).filter((k) => k !== "lockedUntil")).includes("locked"));
});

test("each code with what its sales brought and what its owner was sent or had set aside, as the events say", () => {
  const sold = LOGS.filter((e) => e.name === "ExamSold");
  const parts = LOGS.filter((e) => e.name === "PartnerPaid");
  assert.equal(page.codes.length, 2);
  // Foundry's keccak256("GROUP-ONE"), the code the page computes from the text (simpool.test.mjs holds codeHash to it).
  assert.equal(page.codes[0].code, "0x224fae07b464aab1b3a7320c4a369ed467f616401af5107c645f2636bc520c46");
  const all = sold.reduce((t, e) => t + e.args.price - e.args.fee, 0n);
  for (const c of page.codes) {
    const mine = sold.filter((e) => e.args.code === c.code);
    assert.ok(mine.length > 0, "every code here sold");
    assert.equal(c.brought, mine.reduce((t, e) => t + e.args.price - e.args.fee, 0n), "brought: its sales' prices less the fee");
    for (const e of mine) assert.equal(e.args.partner, c.partner.toLowerCase(), "each sale names the code's owner");
    const got = parts.filter((e) => e.args.code === c.code);
    assert.equal(c.paid, got.filter((e) => e.args.sent).reduce((t, e) => t + e.args.amount, 0n));
    assert.equal(c.setAside, got.filter((e) => !e.args.sent).reduce((t, e) => t + e.args.amount, 0n));
    assert.equal(c.partOfAll, Number(c.brought * 10_000n / all));
    // The withdrawal of 400 took profit only, so its owner's part is the code's share of 400 less the platform's
    // 0.5 %, in the part the code brought (docs/SIM-POOL.md): worked out here, apart from the pool.
    const net = 400_000_000n - 400_000_000n * 50n / 10_000n;
    assert.equal(c.paid + c.setAside, net * c.brought * BigInt(c.bps) / (all * 10_000n));
  }
  assert.deepEqual(page.codes.map((c) => [c.bps, c.paid > 0n, c.setAside > 0n]), [[5000, true, false], [4000, false, true]],
    "the first owner was sent its part, the second's was set aside: the token refuses it");
  assert.equal(page.codes.reduce((t, c) => t + c.brought, 0n), BigInt(CHAIN.reads.codes.reduce((t, k) => t + BigInt(k.brought), 0n)));
});


test("the pool's term as the contract keeps it: 6 months, 184 days, from its creation, and the platform's early close", () => {
  // The pool was created in the block of its first event (the codes set at once), so its term runs from that time.
  assert.equal(page.lockedUntil, CHAIN.events[0].time + MIN_TERM, "lockedUntil: the creation plus 184 days");
  assert.ok(CHAIN.now < page.lockedUntil, "the chain ends long before the term");
  assert.equal(page.creatorMayClose, false, "before its term the creator may not close it");
  assert.equal(page.closed, true);
  assert.equal(page.closedEarly, true, "the platform's closer closed it early");
  assert.equal(page.termNote.state, "closed-early");
  assert.match(page.termNote.text, /^Closed early by the platform, before its term ended on /, "the library's words");
});

// ── the counts a points formula would be built from (activityCounts), on the same chain ──

/** Each exam's and stage's own logs, decoded, by account in lower case: the second pass over the logs. */
const EXAM_LOGS = new Map(Object.entries(CHAIN.examEvents).map(([a, logs]) => [a.toLowerCase(), logs.map(decode)]));
const ALL_TIMES = new Map([...TIMES, ...Object.values(CHAIN.examEvents).flat().map((e) => [e.block, e.time])]);
const counts = activityCounts(LOGS, EXAM_LOGS, ALL_TIMES);

test("each fill keeps the block it was emitted in: a pass's two fills are two blocks", () => {
  // The recording collects the logs after each fill, before the next block (review of #281): a fill recorded at a
  // later block would date its trading day, and its order against the others, wrongly.
  const passed = Object.values(CHAIN.examEvents).filter((evs) => evs.some((e) => e.name === "Passed"));
  assert.ok(passed.length >= 6, "three exams and three stages passed");
  for (const evs of passed) {
    const fills = evs.filter((e) => e.name === "Filled").map((e) => e.block);
    assert.equal(fills.length, 2);
    assert.ok(fills[0] < fills[1], `fills at ${fills.join(" and ")}`);
  }
});

test("the second pass reads exactly the pool's exams and stages, and every one of them was read", () => {
  const accounts = accountsToRead(LOGS);
  assert.deepEqual([accounts.filter((a) => a.kind === "exam").length, accounts.filter((a) => a.kind === "stage").length], [43, 3]);
  assert.deepEqual(accounts.map((a) => a.account.toLowerCase()).sort(), [...EXAM_LOGS.keys()].sort(),
    "the recorded accounts are those the pool sold and started, no more");
  assert.deepEqual(counts.unread, []);
  assert.ok(counts.traders.every((t) => t.complete) && counts.codes.every((c) => c.complete));
});

test("per trader: purchases, exams and stages passed, fills, fill days and payouts, as the events say", () => {
  const by = new Map(counts.traders.map((t) => [t.trader, t]));
  assert.equal(counts.traders.length, 43, "three who passed, ten and thirty who bought");
  const [paid, stopped, held] = CHAIN.reads.payouts.map((p) => by.get(p.trader.toLowerCase()));
  for (const t of [paid, stopped, held]) {
    assert.deepEqual([t.purchases, t.examsPassed, t.stagesStarted, t.stagesPassed, t.payouts.due, t.fills, t.fillDays],
      [1, 1, 1, 1, 1, 4, 1], "one exam bought, passed, its stage passed and claimed, two trades a step, in one day");
    assert.equal(t.payouts.dueAmount, LARGE_PAYOUT);
  }
  assert.deepEqual([paid.payouts.paid, paid.payouts.paidAmount, paid.payouts.stopped, paid.payouts.held], [1, LARGE_PAYOUT, 0, 0]);
  assert.deepEqual([stopped.payouts.stopped, stopped.payouts.stoppedAmount, stopped.payouts.objected, stopped.payouts.paid],
    [1, LARGE_PAYOUT, 1, 0]);
  assert.deepEqual([held.payouts.held, held.payouts.heldAmount, held.payouts.paid], [1, LARGE_PAYOUT, 0]);
  const buyers = counts.traders.filter((t) => ![paid, stopped, held].includes(t));
  assert.ok(buyers.every((t) => t.purchases === 1 && t.examsPassed === 0 && t.fills === 0 && t.payouts.due === 0));
  // Every trader and every number against the events and the getters, apart from the counts.
  const sold = LOGS.filter((e) => e.name === "ExamSold");
  assert.equal(counts.traders.reduce((n, t) => n + t.purchases, 0), sold.length);
  assert.equal(counts.traders.reduce((n, t) => n + t.spent, 0n), sold.reduce((n, e) => n + e.args.price, 0n));
  assert.equal(counts.traders.reduce((n, t) => n + t.payouts.paidAmount, 0n), BigInt(CHAIN.reads.paidTotal));
  assert.equal(counts.traders.reduce((n, t) => n + t.fills, 0),
    Object.values(CHAIN.examEvents).flat().filter((e) => e.name === "Filled").length);
});

test("per code: its traders, purchases, what it brought, and what its traders passed and were paid", () => {
  const [one, two] = page.codes.map((c) => counts.codes.find((k) => k.code === c.code.toLowerCase()));
  assert.equal(counts.codes.length, 2);
  for (const [k, c] of [[one, page.codes[0]], [two, page.codes[1]]]) {
    assert.equal(k.brought, c.brought, "brought: as the page's codes have it, from the same sales");
    assert.equal(k.partner, c.partner.toLowerCase());
  }
  assert.deepEqual([one.traders, one.purchases, one.examsPassed, one.stagesPassed, one.fills], [13, 13, 3, 3, 12]);
  assert.deepEqual([one.payouts.due, one.payouts.paid, one.payouts.held, one.payouts.stopped, one.payouts.objected],
    [3, 1, 1, 1, 1]);
  assert.deepEqual([two.traders, two.purchases, two.examsPassed, two.payouts.due], [30, 30, 0, 0]);
  assert.ok(counts.traders.every((t) => t.codes.length === 1), "each trader bought with one code");
  assert.ok(!JSON.stringify(counts, (k, v) => (typeof v === "bigint" ? String(v) : v)).match(/points|score|weight/i),
    "counts only: no points, scores or weights");
});
