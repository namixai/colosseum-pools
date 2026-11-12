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
import { SIM_POOL_ABI, SIM_POOL_STOP_RESERVE_ABI, PAYOUT_DELAY, STOP_RESERVE, simPoolPage } from "../lib/simpool.js";

const CHAIN = JSON.parse(readFileSync(new URL("./fixtures/sim-page-chain.json", import.meta.url), "utf8"));
const LARGE_PAYOUT = 800_000_000n;

/** The event lines of the library's ABI, by name: each parameter's type, name and whether it is a topic. */
const EVENTS = new Map([...SIM_POOL_ABI, ...SIM_POOL_STOP_RESERVE_ABI].filter((l) => l.startsWith("event ")).map((l) => {
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
  assert.ok(proof.paidAt - proof.requestedAt >= PAYOUT_DELAY, "paid no sooner than 48 hours after it was asked for");
});

test("the stopped payout and the held one are apart from the paid, each with what it needs", () => {
  assert.deepEqual(Object.fromEntries(Object.entries(page.byStatus).map(([k, v]) => [k, v.length])),
    { waiting: 0, queued: 0, paid: 1, stopped: 1, held: 1, refused: 0 });
  const [stopped] = page.byStatus.stopped;
  assert.equal(stopped.stop.reason, 1);
  assert.equal(stopped.stop.trades.length, 2);
  assert.ok(stopped.objection, "the trader's objection is on the page");
  assert.equal(stopped.stopNote.state, "window");
  assert.equal(stopped.reserveEnds, stopped.stoppedAt + STOP_RESERVE);
  const [held] = page.byStatus.held;
  assert.ok(held.heldAt);
  assert.equal(page.held, LARGE_PAYOUT);
  assert.equal(page.reserved, LARGE_PAYOUT, "the stopped payout's money, set aside for its 14 days");
  assert.equal(page.counters.stopped, 1n);
  assert.deepEqual(page.gaps, []);
});

test("the exams and the stages are all there, and the page says how far the chain was read", () => {
  assert.equal(page.exams.length, 13);
  assert.equal(page.stages.length, 3);
  assert.equal(page.stages.filter((s) => s.payoutId !== null).length, 3);
  assert.equal(page.scanText, `Read from block ${CHAIN.scan.from} to block ${CHAIN.scan.to}: every event of this pool `
    + "in that range is listed.");
  assert.ok(!JSON.stringify(Object.keys(page)).includes("locked"));
});
