// node --test app/tests/simpool.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SIM_POOL_ABI, SIM_FACTORY_ABI, SIM_POOL_STOP_RESERVE_ABI, FEE_BPS, PAYOUT_BPS, PAYOUT_DELAY, STOP_RESERVE,
  PAYOUT_STATE, STATUS, TOTAL_INCLUDES, hasStopReserve,
  stopReasonText, normalizeEvent, blocksToDate, payoutOf, menuLines, soldExams, stages, payouts, paidTotal,
  stopCounters, proofOf, gaps, simPoolPage, factoryPools, readLogs, firstBlockAt, claimedAt,
} from "../lib/simpool.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const POOL_SOL = readFileSync(join(ROOT, "src", "sim", "SimPool.sol"), "utf8");
const FACTORY_SOL = readFileSync(join(ROOT, "src", "sim", "SimPoolFactory.sol"), "utf8");

// Addresses of the made-up pool below: none is a real wallet.
const A = (n) => `0x${n.toString(16).padStart(40, "0")}`;
const POOL = A(0x9001);
const USDC = A(0x9002);
const T1 = A(0x11);
const T2 = A(0x12);
const T3 = A(0x13);
const EXAM1 = A(0xe1);
const EXAM2 = A(0xe2);
const EXAM3 = A(0xe3);
const STAGE1 = A(0x51);
const STAGE2 = A(0x52);
const STAGE3 = A(0x53);
const PAIRED = A(0x77);
const H = (n) => `0x${n.toString(16).padStart(64, "0")}`;

const ev = (name, args, block, index = 0) => ({ name, args, block, tx: H(block * 100 + index), index });

/** A pool's life in events: three exams, three stages, four payouts (paid, stopped, lifted then paid, held), one
 *  queue for want of money, and a release. Given out of order, as the windows of a reading may bring them. */
const EVENTS = [
  ev("PayoutPaid", { id: 0n, trader: T1, amount: 800_000_000n, paidCount: 1n, paidTotal: 800_000_000n }, 400),
  ev("ExamSold", { exam: EXAM1, trader: T1, line: 1n, size: 10_000_000_000n, price: 100_000_000n, fee: 500_000n, bySignature: false }, 100),
  ev("ExamSold", { exam: EXAM3, trader: T3, line: 0n, size: 5_000_000_000n, price: 60_000_000n, fee: 300_000n, bySignature: false }, 101, 1),
  ev("ExamSold", { exam: EXAM2, trader: T2, line: 0n, size: 5_000_000_000n, price: 60_000_000n, fee: 300_000n, bySignature: true }, 101),
  ev("Funded", { stage: STAGE1, exam: EXAM1, trader: T1 }, 200),
  ev("Funded", { stage: STAGE2, exam: EXAM2, trader: T2 }, 201),
  ev("Funded", { stage: STAGE3, exam: EXAM3, trader: T3 }, 202),
  ev("PayoutDue", { id: 0n, account: STAGE1, trader: T1, amount: 800_000_000n, dueAt: 2_000n }, 300),
  ev("PayoutDue", { id: 1n, account: STAGE2, trader: T2, amount: 400_000_000n, dueAt: 2_100n }, 301),
  ev("PayoutDue", { id: 2n, account: STAGE3, trader: T3, amount: 400_000_000n, dueAt: 2_200n }, 302),
  ev("PayoutStopped", {
    id: 1n, trader: T2, amount: 400_000_000n, reason: 2, paired: PAIRED, trades: [H(0xa1), H(0xa2)], links: [H(0xb1)],
    stoppedCount: 1n, liftedCount: 0n, paidCount: 0n,
  }, 350),
  ev("PayoutStopped", {
    id: 2n, trader: T3, amount: 400_000_000n, reason: 1, paired: PAIRED, trades: [H(0xc1), H(0xc2)], links: [],
    stoppedCount: 2n, liftedCount: 0n, paidCount: 0n,
  }, 351),
  ev("StopLifted", { id: 2n, stoppedCount: 2n, liftedCount: 1n, paidCount: 1n }, 410),
  ev("PayoutQueued", { id: 2n, balance: 100_000_000n }, 420),
  ev("PayoutPaid", { id: 2n, trader: T3, amount: 400_000_000n, paidCount: 2n, paidTotal: 1_200_000_000n }, 430),
  ev("Released", { account: EXAM1 }, 150),
];

const STRUCT = (account, trader, amount, dueAt, state, wasStopped) => ({ account, trader, amount, dueAt, state, wasStopped });
const PAYOUTS = [
  STRUCT(STAGE1, T1, 800_000_000n, 2_000n, 3, false),
  STRUCT(STAGE2, T2, 400_000_000n, 2_100n, 2, true),
  STRUCT(STAGE3, T3, 400_000_000n, 2_200n, 3, true),
];
const TIMES = new Map(EVENTS.map((e) => [e.block, 1_000 + e.block]));

const READS = {
  menu: [{ size: 5_000_000_000n, price: 60_000_000n }, { size: 10_000_000_000n, price: 100_000_000n }],
  payouts: PAYOUTS,
  paidCount: 2n,
  paidTotal: 1_200_000_000n,
  stoppedCount: 2n,
  liftedCount: 1n,
  owed: 0n,
  held: 0n,
  live: 0n,
  closed: false,
  finished: false,
  partner: A(0x99),
  partnerBps: 5000,
  terms: H(0x7e),
  fundWindow: 86_400,
};

/** `event Name(type indexed? name, ...)`, as types with their indexed flag. */
function shape(params) {
  return params.split(",").map((p) => p.trim()).filter(Boolean).map((p) => {
    const words = p.split(/\s+/);
    return `${words[0]}${words.includes("indexed") ? " indexed" : ""}`;
  }).join(",");
}

function sourceEvents(source) {
  const out = new Map();
  for (const m of source.matchAll(/event\s+(\w+)\(([\s\S]*?)\);/g)) out.set(m[1], shape(m[2].replace(/\/\/.*$/gm, "")));
  return out;
}

test("every event the library reads is the contract's own, types and indexed topics alike", () => {
  const pool = sourceEvents(POOL_SOL);
  const factory = sourceEvents(FACTORY_SOL);
  const check = (abi, declared) => {
    for (const line of abi.filter((l) => l.startsWith("event "))) {
      const m = line.match(/^event (\w+)\((.*)\)$/);
      assert.ok(declared.has(m[1]), `${m[1]} is not declared`);
      assert.equal(shape(m[2]), declared.get(m[1]), `${m[1]} differs from the source`);
    }
  };
  check(SIM_POOL_ABI, pool);
  check(SIM_FACTORY_ABI, factory);
});

test("every getter the library reads is the contract's own", () => {
  for (const [abi, source] of [[SIM_POOL_ABI, POOL_SOL], [SIM_FACTORY_ABI, FACTORY_SOL]]) {
    for (const line of abi.filter((l) => l.startsWith("function "))) {
      const name = line.match(/^function (\w+)\(/)[1];
      const declared = new RegExp(`function ${name}\\(|public\\s+(?:immutable\\s+)?${name}\\s*;|\\)\\s+public\\s+${name}\\s*;`);
      assert.match(source, declared, `${name} is not a getter of the contract`);
    }
  }
});

test("the platform's numbers are the factory's constants", () => {
  assert.match(FACTORY_SOL, new RegExp(`FEE_BPS = ${FEE_BPS};`));
  assert.match(FACTORY_SOL, new RegExp(`PAYOUT_BPS = ${PAYOUT_BPS};`));
  assert.equal(PAYOUT_DELAY, 48 * 3600);
  assert.match(FACTORY_SOL, /PAYOUT_DELAY = 48 hours;/);
});

test("the payout states are the contract's, in its order, and Refused next on a pool with the stop reserve", () => {
  const body = POOL_SOL.match(/enum PayoutState \{([\s\S]*?)\}/)[1].replace(/\/\/\/.*$/gm, "");
  const declared = body.split(",").map((s) => s.trim()).filter(Boolean);
  assert.deepEqual(PAYOUT_STATE.slice(0, declared.length), declared);
  assert.ok(declared.length === 5 || declared.length === 6, declared.join());
  assert.equal(PAYOUT_STATE[5], "Refused");
});

/** Whether the source has the stop reserve: before it is merged the test holds its lines absent, after it to the source. */
const RESERVE = /uint256 public reserved;/.test(POOL_SOL);

test("the stop reserve's lines are the contract's own once it has them, and absent from it before", () => {
  const events = sourceEvents(POOL_SOL);
  const struct = POOL_SOL.match(/struct Payout \{([\s\S]*?)\}/)[1].replace(/\/\/\/.*$/gm, "");
  const fields = [...struct.matchAll(/^\s*(\w+)\s+(\w+);/gm)].map((m) => m[2]);
  const line = SIM_POOL_STOP_RESERVE_ABI.find((l) => l.startsWith("function payout("));
  const abiFields = [...line.matchAll(/(?:address|uint64|uint8|bool) (\w+)/g)].map((m) => m[1]);
  const v1 = SIM_POOL_ABI.find((l) => l.startsWith("function payout("));
  assert.deepEqual([...v1.matchAll(/(?:address|uint64|uint8|bool) (\w+)/g)].map((m) => m[1]), abiFields.slice(0, 6),
    "the six fields lead both, so the first line decodes either pool's answer");
  if (RESERVE) {
    assert.deepEqual(fields, abiFields);
    for (const l of SIM_POOL_STOP_RESERVE_ABI.filter((x) => x.startsWith("event "))) {
      const m = l.match(/^event (\w+)\((.*)\)$/);
      assert.equal(shape(m[2]), events.get(m[1]), m[1]);
    }
    assert.match(FACTORY_SOL, /STOP_RESERVE = 14 days;/);
  } else {
    assert.deepEqual(fields, abiFields.slice(0, 6));
    assert.ok(!events.has("StopObjected") && !events.has("StopFinal"));
  }
  assert.equal(STOP_RESERVE, 14 * 24 * 3600);
});

test("a stopped payout on a pool with the stop reserve says until when, the objection, and the final", () => {
  const stoppedAt = 1_500;
  const structs = [
    { ...STRUCT(STAGE1, T1, 1n, 2_000n, 2, true), stoppedAt: BigInt(stoppedAt) },
    [STAGE2, T2, 2n, 2_100n, 5, true, 1_600n],
    STRUCT(STAGE3, T3, 3n, 2_200n, 3, false),
  ];
  const events = [
    ev("StopObjected", { id: 0n, trader: T1, text: H(0x0b) }, 500),
    ev("StopFinal", { id: 1n, amount: 2n, reserved: 0n }, 600),
  ];
  const [a, b, c] = payouts(structs, events, 3_000, TIMES);
  assert.equal(a.status, "stopped");
  assert.equal(a.stoppedAt, stoppedAt);
  assert.equal(a.reserveEnds, stoppedAt + STOP_RESERVE);
  assert.deepEqual([a.objection.text, a.objection.tx], [H(0x0b), H(500 * 100)]);
  assert.equal(b.status, "refused");
  assert.equal(b.stoppedAt, 1_600, "read from the array as ethers hands it");
  assert.equal(b.final.tx, H(600 * 100));
  assert.ok(STATUS.refused);
  assert.deepEqual([c.stoppedAt, c.reserveEnds, c.objection, c.final], [null, null, null, null],
    "a pool without the reserve, or a payout never stopped");
  assert.deepEqual(gaps(payouts([[STAGE2, T2, 2n, 2_100n, 5, true, 1_600n]], [], 3_000)),
    [{ id: 0, missing: "PayoutDue" }, { id: 0, missing: "PayoutStopped" }, { id: 0, missing: "StopFinal" }]);
});

test("whether a pool has the stop reserve: a revert of reserved() says no, a node's failure is not taken for it", async () => {
  const revert = (err) => err.code === "CALL_EXCEPTION";
  assert.equal(await hasStopReserve(async () => 0n, revert), true);
  assert.equal(await hasStopReserve(async () => { throw Object.assign(new Error("x"), { code: "CALL_EXCEPTION" }); }, revert), false);
  await assert.rejects(hasStopReserve(async () => { throw Object.assign(new Error("rate"), { code: -32005 }); }, revert), /rate/);
});

test("a missing read is named, never taken for zero; an event without its block is refused", () => {
  assert.throws(() => paidTotal(EVENTS, { paidCount: 2n }), /lack paidTotal/);
  assert.throws(() => stopCounters({}), /lack stoppedCount, liftedCount, paidCount/);
  const { owed, ...noOwed } = READS;
  assert.equal(owed, 0n);
  assert.throws(() => simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, reads: noOwed, events: EVENTS, now: 3_000 }),
    /lack owed/);
  assert.throws(() => normalizeEvent({ eventName: "PayoutPaid", args: {}, transactionHash: H(1) }), /without its block/);
});

test("a menu line pays 8% of its size and gives the platform 0.5% of its price", () => {
  const lines = menuLines(READS.menu);
  assert.deepEqual(lines[0], { line: 0, size: 5_000_000_000n, price: 60_000_000n, payout: 400_000_000n, fee: 300_000n });
  assert.equal(lines[1].payout, 800_000_000n);
  assert.equal(payoutOf(12_345_678n), 987_654n, "rounded down, as the contract does");
  // ethers hands a struct as an array as well as by name.
  assert.equal(menuLines([[5_000_000_000n, 60_000_000n]])[0].payout, 400_000_000n);
});

test("exams sold come oldest first, with the stage each earned and whether it was released", () => {
  const exams = soldExams(EVENTS, TIMES);
  assert.deepEqual(exams.map((e) => e.exam), [EXAM1, EXAM2, EXAM3]);
  assert.equal(exams[0].stage, STAGE1);
  assert.equal(exams[0].released, true);
  assert.equal(exams[1].released, false);
  assert.equal(exams[1].bySignature, true);
  assert.equal(exams[0].price, 100_000_000n);
  assert.equal(exams[0].tx, H(100 * 100));
  assert.equal(exams[0].time, 1_100);
});

test("stages name their exam and the payout they wrote", () => {
  const list = stages(EVENTS, TIMES);
  assert.deepEqual(list.map((s) => s.stage), [STAGE1, STAGE2, STAGE3]);
  assert.deepEqual(list.map((s) => s.payoutId), [0, 1, 2]);
  assert.equal(list[0].exam, EXAM1);
  assert.equal(stages([ev("Funded", { stage: STAGE1, exam: EXAM1, trader: T1 }, 1)])[0].payoutId, null);
});

test("each payout's status comes from the contract's state and the chain's clock", () => {
  const list = payouts(PAYOUTS, EVENTS, 3_000, TIMES);
  assert.deepEqual(list.map((p) => p.status), ["paid", "stopped", "paid"]);
  const due = STRUCT(STAGE1, T1, 1n, 2_000n, 1, false);
  assert.equal(payouts([due], [], 1_999)[0].status, "waiting");
  assert.equal(payouts([due], [], 2_000)[0].status, "queued", "the contract pays once the time has come, not after");
  assert.equal(payouts([STRUCT(STAGE1, T1, 1n, 2_000n, 4, false)], [], 0)[0].status, "held");
  assert.equal(payouts([STRUCT(STAGE1, T1, 1n, 2_000n, 0, false)], [], 0)[0].status, "unknown");
  for (const k of ["waiting", "queued", "paid", "stopped", "held"]) assert.ok(STATUS[k]);
});

test("a stopped payout carries the reason, the other account, the trades and links, and the stop's transaction", () => {
  const p = payouts(PAYOUTS, EVENTS, 3_000, TIMES)[1];
  assert.equal(p.stop.reason, 2);
  assert.equal(p.stop.reasonText, stopReasonText(2));
  assert.equal(p.stop.paired, PAIRED);
  assert.deepEqual(p.stop.trades, [H(0xa1), H(0xa2)]);
  assert.deepEqual(p.stop.links, [H(0xb1)]);
  assert.equal(p.stop.tx, H(350 * 100));
  assert.equal(p.lift, null);
  assert.match(stopReasonText(1), /Opposite positions/);
  assert.match(stopReasonText(9), /Unknown reason 9/);
});

test("a lifted stop keeps its stop and adds the lift; a payment found short is noted only while queued", () => {
  const p = payouts(PAYOUTS, EVENTS, 3_000, TIMES)[2];
  assert.equal(p.status, "paid");
  assert.equal(p.stop.reason, 1);
  assert.equal(p.lift.tx, H(410 * 100));
  assert.equal(p.short, null, "paid since: the queue is history");
  const queued = payouts([...PAYOUTS.slice(0, 2), STRUCT(STAGE3, T3, 400_000_000n, 2_200n, 1, true)], EVENTS, 3_000, TIMES)[2];
  assert.equal(queued.status, "queued");
  assert.equal(queued.short.balance, 100_000_000n);
  assert.equal(queued.short.tx, H(420 * 100));
});

test("paid in total is the sum of the payment events, each with its transaction, checked against the contract", () => {
  const t = paidTotal(EVENTS, READS, TIMES);
  assert.equal(t.total, 1_200_000_000n);
  assert.equal(t.listed, 1_200_000_000n);
  assert.equal(t.complete, true);
  assert.deepEqual(t.payments.map((p) => [p.id, p.tx]), [[0, H(400 * 100)], [2, H(430 * 100)]]);
  assert.equal(t.includes, TOTAL_INCLUDES);
});

test("a reading that missed a payment says so and keeps the contract's total", () => {
  const cut = EVENTS.filter((e) => !(e.name === "PayoutPaid" && e.args.id === 2n));
  const t = paidTotal(cut, READS);
  assert.equal(t.complete, false);
  assert.equal(t.total, 1_200_000_000n, "the total is the contract's, never the smaller sum of what was read");
  assert.equal(t.listed, 800_000_000n);
  // Same count, wrong sum: still not complete.
  assert.equal(paidTotal(EVENTS, { ...READS, paidTotal: 1_200_000_001n }).complete, false);
  assert.equal(paidTotal(EVENTS, { ...READS, paidCount: 3n }).complete, false);
});

test("the line under the total says what is in it and what is not", () => {
  for (const words of ["8%", "stopped", "waiting", "refused", "0.5%", "withdrew"]) assert.ok(TOTAL_INCLUDES.includes(words), words);
});

test("the counters of stops: every stop, every lift, and the stops that stand", () => {
  assert.deepEqual(stopCounters(READS), { stopped: 2n, lifted: 1n, standing: 1n, paid: 2n });
});

test("a paid payout's proof has its five fields; an unpaid one has none", () => {
  const list = payouts(PAYOUTS, EVENTS, 3_000, TIMES);
  const proof = proofOf(list[0], { pool: POOL, usdc: USDC, chainId: 998 });
  assert.deepEqual(proof, {
    id: 0, chainId: 998, tx: H(400 * 100), token: USDC, amount: 800_000_000n,
    requestedAt: 1_300, requestTx: H(300 * 100), paidAt: 1_400, from: POOL, to: T1,
  });
  assert.equal(proofOf(list[1], { pool: POOL, usdc: USDC, chainId: 998 }), null);
  const unread = payouts(PAYOUTS, EVENTS.filter((e) => e.name !== "PayoutPaid"), 3_000)[0];
  assert.equal(proofOf(unread, { pool: POOL, usdc: USDC, chainId: 998 }), null, "no payment event, no proof");
});

test("gaps name each payout whose events were not all read", () => {
  assert.deepEqual(gaps(payouts(PAYOUTS, EVENTS, 3_000)), []);
  const none = gaps(payouts(PAYOUTS, [], 3_000));
  assert.deepEqual(none, [
    { id: 0, missing: "PayoutDue" }, { id: 0, missing: "PayoutPaid" },
    { id: 1, missing: "PayoutDue" }, { id: 1, missing: "PayoutStopped" },
    { id: 2, missing: "PayoutDue" }, { id: 2, missing: "PayoutPaid" }, { id: 2, missing: "PayoutStopped" },
    { id: 2, missing: "StopLifted" },
  ]);
  assert.deepEqual(gaps(payouts([STRUCT(STAGE1, T1, 1n, 2_000n, 4, false)], [], 0)),
    [{ id: 0, missing: "PayoutDue" }, { id: 0, missing: "PayoutHeld" }]);
});

/** Every key anywhere in `value`. */
function keys(value, out = new Set()) {
  if (value && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) {
      out.add(k);
      keys(v, out);
    }
  }
  return out;
}

test("the page of a pool has everything it shows and nothing of what the creator locked or put in", () => {
  const page = simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, reads: { ...READS, principal: 5n, locked: 7n },
    events: [...EVENTS, ev("AddedToPot", { amount: 7n, principal: 7n }, 1)], times: TIMES, now: 3_000 });
  assert.equal(page.menu.length, 2);
  assert.equal(page.exams.length, 3);
  assert.equal(page.stages.length, 3);
  assert.equal(page.byStatus.paid.length, 2);
  assert.equal(page.byStatus.stopped.length, 1);
  assert.equal(page.proofs.length, 2);
  assert.equal(page.total.complete, true);
  assert.deepEqual(page.gaps, []);
  assert.equal(page.counters.standing, 1n);
  assert.equal(page.reserved, null, "a pool without the stop reserve");
  assert.equal(simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, reads: { ...READS, reserved: 400_000_000n },
    events: EVENTS, times: TIMES, now: 3_000 }).reserved, 400_000_000n);
  const all = keys(page);
  for (const k of ["locked", "principal"]) assert.ok(!all.has(k), `${k} is on the page`);
  assert.ok(!SIM_POOL_ABI.some((l) => /principal|withdrawable|AddedToPot/.test(l)), "the library reads none of it");
});

test("the factory's pools come without what each locked", () => {
  const list = factoryPools([
    ev("PoolCreated", { pool: POOL, creator: T1, partner: A(0x99), partnerBps: 5000n, locked: 1_600_000_000n }, 7),
  ], TIMES);
  assert.deepEqual(list, [{ pool: POOL, creator: T1, partner: A(0x99), partnerBps: 5000, tx: H(700), block: 7, time: null }]);
  assert.ok(!keys(list).has("locked"));
});

test("an ethers event log reads as the same event", () => {
  const log = {
    eventName: "PayoutPaid", blockNumber: 12, transactionHash: H(5), index: 3,
    args: { toObject: () => ({ id: 4n, trader: T1, amount: 9n, paidCount: 1n, paidTotal: 9n }) },
  };
  assert.deepEqual(normalizeEvent(log), {
    name: "PayoutPaid", args: { id: 4n, trader: T1, amount: 9n, paidCount: 1n, paidTotal: 9n }, block: 12, tx: H(5), index: 3,
  });
  assert.equal(normalizeEvent({ fragment: { name: "Closed" }, args: {}, blockNumber: 1, transactionHash: H(1), logIndex: 2 }).index, 2);
  assert.deepEqual(blocksToDate([log, ev("Closed", {}, 3), ev("Closed", {}, 12, 1)]), [3, 12]);
});

test("logs are read in windows, within a budget of calls, and a reading cut short says where it stopped", async () => {
  const asked = [];
  const getLogs = async (from, to) => {
    asked.push([from, to]);
    return [{ from, to }];
  };
  const whole = await readLogs({ getLogs, from: 100, to: 260, window: 50 });
  assert.deepEqual(asked, [[100, 149], [150, 199], [200, 249], [250, 260]]);
  assert.deepEqual(whole, { logs: asked.map(([from, to]) => ({ from, to })), readTo: 260, complete: true, calls: 4 });
  asked.length = 0;
  const cut = await readLogs({ getLogs, from: 100, to: 260, window: 50, maxCalls: 2 });
  assert.deepEqual([cut.readTo, cut.complete, cut.calls], [199, false, 2]);
  const one = await readLogs({ getLogs, from: 5, to: 5 });
  assert.deepEqual([one.readTo, one.complete, one.calls], [5, true, 1]);
  await assert.rejects(readLogs({ getLogs, from: 0, to: 1, window: 0 }), /at least one block/);
});

test("the first block at a time is found by halving, and the claim's time is the due time less 48 hours", async () => {
  const timeOf = async (b) => 1_000 + b * 2;
  assert.equal(await firstBlockAt(1_101, { low: 0, high: 1_000, timeOf }), 51);
  assert.equal(await firstBlockAt(1_100, { low: 0, high: 1_000, timeOf }), 50);
  assert.equal(await firstBlockAt(0, { low: 0, high: 1_000, timeOf }), 0);
  assert.equal(await firstBlockAt(0, { low: 10, high: 5, timeOf }), null, "an empty range has no block");
  assert.equal(await firstBlockAt(1_000 + 2_001 * 2, { low: 0, high: 1_000, timeOf }), null, "not yet on chain");
  assert.equal(claimedAt(2_000n + BigInt(PAYOUT_DELAY)), 2_000);
});
