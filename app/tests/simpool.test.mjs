// node --test app/tests/simpool.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SIM_POOL_ABI, SIM_FACTORY_ABI, SIM_POOL_STOP_RESERVE_ABI, FEE_BPS, PAYOUT_BPS, PAYOUT_DELAY, STOP_RESERVE, MIN_TERM,
  PAYOUT_STATE, STATUS, TOTAL_INCLUDES, hasStopReserve, stopNote, utc, termNote, activityCounts, accountsToRead,
  EXAM_END,
  stopReasonText, normalizeEvent, blocksToDate, payoutOf, menuLines, soldExams, stages, payouts, paidTotal,
  stopCounters, proofOf, gaps, simPoolPage, factoryPools, readLogs, firstBlockAt, claimedAt, scanText, codeShares,
  codeHash, saleNonce, purchase, signedPurchase, RECEIVE_WITH_AUTHORIZATION_TYPES,
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
  ev("ExamSold", { exam: EXAM1, trader: T1, line: 1n, size: 10_000_000_000n, price: 100_000_000n, fee: 500_000n, bySignature: false, code: H(0xc1), partner: A(0x99) }, 100),
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
/** The reading the events came from: blocks 100 to 500, read to the end. */
const SCAN = { from: 100, to: 500, readTo: 500, complete: true };
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
  codes: [{ code: H(0xc1), partner: A(0x99), bps: 5000, used: true, brought: 159_200_000n }],
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
  assert.equal(PAYOUT_DELAY, 72 * 3600);
  assert.match(FACTORY_SOL, /PAYOUT_DELAY = 72 hours;/);
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
  assert.throws(() => simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: SCAN, reads: noOwed, events: EVENTS, now: 3_000 }),
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
  assert.equal(exams[0].code, H(0xc1));
  assert.equal(exams[0].partner, A(0x99), "the sale's code names its partner");
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
  const page = simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: SCAN, reads: { ...READS, principal: 5n, locked: 7n },
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
  assert.equal(simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: SCAN, reads: { ...READS, reserved: 400_000_000n },
    events: EVENTS, times: TIMES, now: 3_000 }).reserved, 400_000_000n);
  const all = keys(page);
  for (const k of ["locked", "principal"]) assert.ok(!all.has(k), `${k} is on the page`);
  assert.ok(!SIM_POOL_ABI.some((l) => /principal|withdrawable|AddedToPot/.test(l)), "the library reads none of it");
});

test("the factory's pools come without what each locked", () => {
  const list = factoryPools([
    ev("PoolCreated", { pool: POOL, creator: T1, locked: 1_600_000_000n }, 7),
  ], TIMES);
  assert.deepEqual(list, [{ pool: POOL, creator: T1, tx: H(700), block: 7, time: null }]);
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
  assert.deepEqual(whole, { logs: asked.map(([from, to]) => ({ from, to })), from: 100, to: 260, readTo: 260, complete: true,
    calls: 4 });
  asked.length = 0;
  const cut = await readLogs({ getLogs, from: 100, to: 260, window: 50, maxCalls: 2 });
  assert.deepEqual([cut.readTo, cut.complete, cut.calls], [199, false, 2]);
  const one = await readLogs({ getLogs, from: 5, to: 5 });
  assert.deepEqual([one.readTo, one.complete, one.calls], [5, true, 1]);
  await assert.rejects(readLogs({ getLogs, from: 0, to: 1, window: 0 }), /at least one block/);
});

test("the first block at a time is found by halving, and the claim's time is the due time less 72 hours", async () => {
  const timeOf = async (b) => 1_000 + b * 2;
  assert.equal(await firstBlockAt(1_101, { low: 0, high: 1_000, timeOf }), 51);
  assert.equal(await firstBlockAt(1_100, { low: 0, high: 1_000, timeOf }), 50);
  assert.equal(await firstBlockAt(0, { low: 0, high: 1_000, timeOf }), 0);
  assert.equal(await firstBlockAt(0, { low: 10, high: 5, timeOf }), null, "an empty range has no block");
  assert.equal(await firstBlockAt(1_000 + 2_001 * 2, { low: 0, high: 1_000, timeOf }), null, "not yet on chain");
  assert.equal(claimedAt(2_000n + BigInt(PAYOUT_DELAY)), 2_000);
});

test("the page says how far the chain was read, and is whole only if the reading was", async () => {
  const page = simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: SCAN, reads: READS, events: EVENTS, times: TIMES,
    now: 3_000 });
  assert.deepEqual(page.scan, SCAN);
  assert.equal(page.complete, true);
  assert.equal(page.scanText, "Read from block 100 to block 500: every event of this pool in that range is listed.");
  // The same events from a reading the node's budget cut short: the payments still add up, the lists may not.
  const cut = await readLogs({ getLogs: async () => [], from: 100, to: 500, window: 50, maxCalls: 2 });
  const partial = simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: cut, reads: READS, events: EVENTS,
    times: TIMES, now: 3_000 });
  assert.equal(partial.total.complete, true);
  assert.equal(partial.complete, false);
  assert.deepEqual(partial.scan, { from: 100, to: 500, readTo: 199, complete: false });
  assert.match(partial.scanText, /to block 199 of 500: the lists below stop there/);
  assert.equal(scanText(cut), partial.scanText);
  // Payments that do not add up, or a payout missing its events, also make the page not whole.
  assert.equal(simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: SCAN, reads: { ...READS, paidCount: 3n },
    events: EVENTS, times: TIMES, now: 3_000 }).complete, false);
  assert.equal(simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: SCAN, reads: READS,
    events: EVENTS.filter((e) => e.name !== "PayoutDue"), times: TIMES, now: 3_000 }).complete, false);
  for (const bad of [undefined, { ...SCAN, readTo: undefined }, { ...SCAN, complete: "yes" }]) {
    assert.throws(() => simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: bad, reads: READS, events: EVENTS,
      now: 3_000 }), /needs the scan its events came from/);
  }
});

test("a stop on a pool with the reserve says where it stands: the 14 days, the objection, the final", () => {
  const at = 1_800_000_000;
  const until = at + STOP_RESERVE;
  const stopped = { ...STRUCT(STAGE1, T1, 1n, 2_000n, 2, true), stoppedAt: BigInt(at) };
  const objection = [ev("StopObjected", { id: 0n, trader: T1, text: H(0x0b) }, 700)];
  const times = new Map([[700, at + 3_600]]);
  const [w] = payouts([stopped], objection, at + 60, times);
  assert.deepEqual([w.stopNote.state, w.stopNote.until], ["window", until]);
  assert.equal(w.stopNote.text, `Stopped. Until ${utc(until)} the trader may object and the checker may lift the stop; `
    + `the money stays in the pool, set aside. The trader objected on ${utc(at + 3_600)}; the objection is on chain.`);
  assert.equal(utc(until), "2027-01-29 08:00 UTC");
  const [d] = payouts([stopped], [], until, times);
  assert.equal(d.stopNote.state, "due-final", "the 14 days are over at their last second");
  assert.match(d.stopNote.text, /the refusal becomes final when anyone sends it \(finalizeStop\)\.$/);
  const [f] = payouts([[STAGE1, T1, 1n, 2_000n, 5, true, BigInt(at)]], objection, until + 10, times);
  assert.equal(f.stopNote.state, "final");
  assert.match(f.stopNote.text, /^Refused: the stop was not lifted within 14 days, and the refusal is final\. The trader objected/);
  const [old] = payouts([STRUCT(STAGE1, T1, 1n, 2_000n, 2, true)], [], 3_000);
  assert.deepEqual(old.stopNote, { state: "stopped", until: null, text: "Stopped by the checker." }, "a pool without the reserve");
  for (const state of [1, 3, 4]) {
    assert.equal(payouts([{ ...STRUCT(STAGE1, T1, 1n, 2_000n, state, true), stoppedAt: BigInt(at) }], [], at)[0].stopNote,
      null, `state ${state}: not stopped now, so no note`);
  }
  assert.equal(stopNote({ status: "paid" }, 0), null);
  // An objection whose block was not dated still says when, by its block.
  assert.match(payouts([stopped], objection, at + 60)[0].stopNote.text, /objected on block 700;/);
});

test("each code's partner, share, part of what the pool took, and what it was sent or had set aside", () => {
  const codes = [
    { code: H(0xc1), partner: A(0x91), bps: 5000, used: true, brought: 300n },
    { code: H(0xc2), partner: A(0x92), bps: 4000, used: true, brought: 100n },
    { code: H(0xc3), partner: A(0x93), bps: 0, used: false, brought: 0n },
  ];
  const list = codeShares(codes, [
    ev("PartnerPaid", { code: H(0xc1), partner: A(0x91), amount: 70n, sent: true }, 200),
    ev("PartnerPaid", { code: H(0xc2), partner: A(0x92), amount: 20n, sent: false }, 200, 1),
    ev("PartnerPaid", { code: H(0xc1), partner: A(0x91), amount: 5n, sent: true }, 300),
  ]);
  assert.deepEqual(list.map((k) => [k.partOfAll, k.paid, k.setAside, k.used]),
    [[7500, 75n, 0n, true], [2500, 0n, 20n, true], [0, 0n, 0n, false]]);
  assert.deepEqual(codeShares([], []), []);
});

// ── buying with a code ───────────────────────────────────────────

/** Keccak-256 over 0x-hex bytes, for the tests alone (the page has ethers.keccak256): the sponge of FIPS 202 with
 *  Keccak's own padding, 1600-bit state in 64-bit lanes. Held to vectors from `cast keccak` below. */
const RC = [0x1n, 0x8082n, 0x800000000000808an, 0x8000000080008000n, 0x808bn, 0x80000001n, 0x8000000080008081n,
  0x8000000000008009n, 0x8an, 0x88n, 0x80008009n, 0x8000000an, 0x8000808bn, 0x800000000000008bn, 0x8000000000008089n,
  0x8000000000008003n, 0x8000000000008002n, 0x8000000000000080n, 0x800an, 0x800000008000000an, 0x8000000080008081n,
  0x8000000000008080n, 0x80000001n, 0x8000000080008008n];
const ROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14];
const M64 = (1n << 64n) - 1n;
const rotl = (x, n) => (n === 0 ? x : ((x << BigInt(n)) | (x >> BigInt(64 - n))) & M64);
function keccak256(hex) {
  const bytes = Buffer.from(hex.slice(2), "hex");
  const rate = 136;
  const padded = Buffer.alloc(Math.ceil((bytes.length + 1) / rate) * rate);
  bytes.copy(padded);
  padded[bytes.length] ^= 0x01;
  padded[padded.length - 1] ^= 0x80;
  const a = new Array(25).fill(0n);
  for (let off = 0; off < padded.length; off += rate) {
    for (let i = 0; i < rate / 8; ++i) a[i] ^= padded.readBigUInt64LE(off + 8 * i);
    for (let round = 0; round < 24; ++round) {
      const c = [0, 1, 2, 3, 4].map((x) => a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20]);
      for (let x = 0; x < 5; ++x) {
        const d = c[(x + 4) % 5] ^ rotl(c[(x + 1) % 5], 1);
        for (let y = 0; y < 25; y += 5) a[x + y] ^= d;
      }
      const b = new Array(25);
      for (let x = 0; x < 5; ++x) {
        for (let y = 0; y < 5; ++y) b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(a[x + 5 * y], ROT[x + 5 * y]);
      }
      for (let y = 0; y < 25; y += 5) {
        for (let x = 0; x < 5; ++x) a[x + y] = b[x + y] ^ (~b[((x + 1) % 5) + y] & M64 & b[((x + 2) % 5) + y]);
      }
      a[0] ^= RC[round];
    }
  }
  const out = Buffer.alloc(32);
  for (let i = 0; i < 4; ++i) out.writeBigUInt64LE(a[i], 8 * i);
  return `0x${out.toString("hex")}`;
}

// Vectors from Foundry's `cast keccak` (and `cast abi-encode "f(bytes32,bytes32)"` for the nonce).
const GROUP_ONE = "0x224fae07b464aab1b3a7320c4a369ed467f616401af5107c645f2636bc520c46";
const SALT7 = `0x${"7".padStart(64, "0")}`;
const NONCE_GROUP_ONE_7 = "0x009fc5d0eb2aa96c517c432aa30979862ca89d6e5d55d72ae906e4913a7cbdec";

test("the tests' keccak256 is Keccak-256: the empty input's hash", () => {
  assert.equal(keccak256("0x"), "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470");
});

test("a code's hash is the keccak256 of its text exactly as given, as ops/deploy_sim.py keys it", () => {
  assert.equal(codeHash("GROUP-ONE", keccak256), GROUP_ONE);
  assert.equal(codeHash("Grüße, group ✓", keccak256),
    "0x82cb0817338a7f131782c03bdad240a53050cb73f855d7bf3aa76434f29b3f03", "UTF-8, not one byte per character");
  assert.equal(codeHash("GROUP-ONE ", keccak256),
    "0x181a4653259b3939acd076117359d4827bbe743c5157da6850e87b64d2041f79", "nothing trimmed: a space is another code");
  assert.throws(() => codeHash("", keccak256), /not empty/);
  assert.match(readFileSync(join(ROOT, "ops", "deploy_sim.py"), "utf8"), /return keccak\(text=text\)/);
});

test("the signed nonce is keccak256(abi.encode(code, salt)), as the pool and the relayer take it", () => {
  assert.equal(saleNonce(GROUP_ONE, SALT7, keccak256), NONCE_GROUP_ONE_7);
  assert.equal(saleNonce(GROUP_ONE.toUpperCase().replace("0X", "0x"), SALT7, keccak256), NONCE_GROUP_ONE_7);
  assert.throws(() => saleNonce("0x1234", SALT7, keccak256), /code is not 32 bytes/);
  assert.throws(() => saleNonce(GROUP_ONE, "salt", keccak256), /salt is not 32 bytes/);
  assert.match(POOL_SOL, /keccak256\(abi\.encode\(code, salt\)\)/);
  assert.match(readFileSync(join(ROOT, "ops", "sim_relay.py"), "utf8"), /keccak\(encode\(\["bytes32", "bytes32"\], \[code, salt\]\)\)/);
});

/** A function's parameters in the source, as `type name` pairs. */
function sourceParams(source, name) {
  const m = source.match(new RegExp(`function ${name}\\(([\\s\\S]*?)\\)\\s*(?:external|public)`));
  return m[1].split(",").map((p) => p.trim().split(/\s+/)).map((w) => `${w[0]} ${w[w.length - 1]}`);
}

test("the two ways to buy are the contract's own, parameter for parameter", () => {
  for (const name of ["buy", "buyWithAuthorization"]) {
    const line = SIM_POOL_ABI.find((l) => l.startsWith(`function ${name}(`));
    const abi = line.match(/^function \w+\((.*?)\)/)[1].split(", ").map((p) => p.trim());
    assert.deepEqual(abi, sourceParams(POOL_SOL, name), name);
  }
});

const CODES = [
  { code: GROUP_ONE, partner: A(0x99), bps: 5000, used: true, brought: 0n },
  { code: H(0xc2), partner: A(0), bps: 0, used: false, brought: 0n },
];

test("a purchase is checked as the pool checks it: the line on the menu, the code on sale", () => {
  const p = purchase({ menu: READS.menu, line: 1, code: GROUP_ONE, codes: CODES });
  assert.deepEqual(p, { method: "buy", args: [1n, GROUP_ONE], price: BigInt(READS.menu[1].price) });
  assert.deepEqual(purchase({ menu: READS.menu, line: 0, code: GROUP_ONE.toUpperCase().replace("0X", "0x") }).args,
    [0n, GROUP_ONE], "the code as the pool keys it; without the pool's codes read, no check of them");
  assert.throws(() => purchase({ menu: READS.menu, line: READS.menu.length, code: GROUP_ONE }), /no line/);
  assert.throws(() => purchase({ menu: READS.menu, line: -1, code: GROUP_ONE }), /no line/);
  assert.throws(() => purchase({ menu: READS.menu, line: 0, code: H(0xc3), codes: CODES }), /not on sale/);
  assert.throws(() => purchase({ menu: READS.menu, line: 0, code: H(0xc2), codes: CODES }), /not on sale/,
    "a code taken off sale (no partner)");
});

test("a signed purchase: the trader signs the line's price to the pool under the code's nonce, and a relayer sends it", () => {
  const signed = signedPurchase({
    pool: POOL, trader: T1, menu: READS.menu, line: 1, code: GROUP_ONE, codes: CODES, salt: SALT7,
    validAfter: 0, validBefore: 1_800_000_000, keccak256,
  });
  assert.equal(signed.nonce, NONCE_GROUP_ONE_7);
  assert.deepEqual(signed.message, {
    from: T1, to: POOL, value: BigInt(READS.menu[1].price), validAfter: 0n, validBefore: 1_800_000_000n, nonce: NONCE_GROUP_ONE_7,
  });
  assert.equal(signed.types, RECEIVE_WITH_AUTHORIZATION_TYPES);
  // The message's fields are the token's, in its order (Usdc3009 in test/sim/SimPool.t.sol stands for Circle's).
  const typehash = readFileSync(join(ROOT, "test", "sim", "SimPool.t.sol"), "utf8")
    .match(/"ReceiveWithAuthorization\((.*?)\)"/)[1];
  assert.equal(RECEIVE_WITH_AUTHORIZATION_TYPES.ReceiveWithAuthorization.map((f) => `${f.type} ${f.name}`).join(","), typehash);
  const sig = { v: 27, r: H(0xaa), s: H(0xbb) };
  const args = signed.args(sig);
  assert.deepEqual(sourceParams(POOL_SOL, "buyWithAuthorization").map((p) => p.split(" ")[1]),
    ["trader", "line", "code", "salt", "validAfter", "validBefore", "v", "r", "s"]);
  assert.deepEqual(args, [T1, 1n, GROUP_ONE, SALT7, 0n, 1_800_000_000n, 27, H(0xaa), H(0xbb)]);
  const body = signed.relayBody(sig);
  const relay = readFileSync(join(ROOT, "ops", "sim_relay.py"), "utf8");
  const wanted = [...relay.matchAll(/raw\["(\w+)"\]/g)].map((m) => m[1]);
  for (const k of ["code", "salt", "r", "s"]) wanted.push(k);
  assert.deepEqual(Object.keys(body).sort(), [...new Set(wanted)].sort(), "what the relayer reads, and nothing else");
  assert.deepEqual(body, { pool: POOL, trader: T1, line: 1, code: GROUP_ONE, salt: SALT7, valid_after: 0,
    valid_before: 1_800_000_000, v: 27, r: H(0xaa), s: H(0xbb) });
  assert.throws(() => signedPurchase({ pool: POOL, trader: T1, menu: READS.menu, line: 1, code: GROUP_ONE, salt: SALT7,
    validAfter: 10, validBefore: 10, keccak256 }), /end after it starts/);
});


test("the pool's term: the time before which its creator cannot close it, and whether the platform closed it sooner", () => {
  assert.equal(MIN_TERM, 180 * 24 * 3600);
  assert.match(FACTORY_SOL, /MIN_TERM = 180 days;/);
  const read = (extra, now) => simPoolPage({ pool: POOL, usdc: USDC, chainId: 998, scan: SCAN, reads: { ...READS, ...extra },
    events: EVENTS, times: TIMES, now });
  const locked = read({ lockedUntil: 5_000n, closedEarly: false }, 4_999);
  assert.deepEqual([locked.lockedUntil, locked.creatorMayClose, locked.closedEarly], [5_000, false, false]);
  assert.equal(read({ lockedUntil: 5_000n, closedEarly: false }, 5_000).creatorMayClose, true, "from its time on");
  assert.equal(read({ lockedUntil: 5_000n, closedEarly: true }, 10).closedEarly, true);
  assert.deepEqual([read({}, 10).lockedUntil, read({}, 10).creatorMayClose], [null, null], "not read: not guessed");
});

test("the pool's term in the library's own words: locked, open, closed, closed early by the platform", () => {
  const until = 1_743_595_200; // 2025-04-02 12:00 UTC
  assert.equal(termNote({}, 0), null, "a pool without a term: no words");
  assert.deepEqual(termNote({ lockedUntil: BigInt(until) }, until - 1), { state: "locked", until,
    text: "Runs at least until 2025-04-02 12:00 UTC: before then its creator can neither close it nor take back what "
      + "it put in." });
  assert.deepEqual(termNote({ lockedUntil: until }, until), { state: "open", until,
    text: "Its term ended on 2025-04-02 12:00 UTC: its creator may close it now." });
  assert.deepEqual(termNote({ lockedUntil: until, closed: true }, until + 1), { state: "closed", until,
    text: "Closed after its term ended on 2025-04-02 12:00 UTC." });
  const early = termNote({ lockedUntil: until, closed: true, closedEarly: true }, 10);
  assert.equal(early.state, "closed-early");
  assert.match(early.text, /^Closed early by the platform, before its term ended on 2025-04-02 12:00 UTC\. /);
  assert.match(early.text, /the codes' owners, and the rest to the creator\.$/);
});

test("counts per trader and per code: breached and expired apart, an unread account said, a fill day not guessed", () => {
  const ev = (name, block, args) => ({ name, block, index: 0, tx: `0x${block}`, args });
  const C1 = `0x${"c1".repeat(32)}`;
  const [E1, E2, E3, S1, P1, X] = [A(0xe1), A(0xe2), A(0xe3), A(0x51), A(0xa1), A(0x99)];
  const pool = [
    ev("ExamSold", 1, { exam: E1, trader: T1, line: 1n, size: 10_000_000_000n, price: 85_000_000n, fee: 425_000n,
      bySignature: false, code: C1, partner: P1 }),
    ev("ExamSold", 2, { exam: E2, trader: T1, line: 0n, size: 5_000_000_000n, price: 45_000_000n, fee: 225_000n,
      bySignature: true, code: C1, partner: P1 }),
    ev("ExamSold", 3, { exam: E3, trader: X, line: 0n, size: 5_000_000_000n, price: 45_000_000n, fee: 225_000n,
      bySignature: false, code: C1, partner: P1 }),
    ev("Funded", 4, { stage: S1, exam: E1, trader: T1 }),
    ev("PayoutDue", 6, { id: 0n, account: S1, trader: T1, amount: 800_000_000n, dueAt: 100n }),
    ev("PayoutStopped", 7, { id: 0n, trader: T1, amount: 800_000_000n, reason: 1, paired: E3, trades: [], links: [] }),
    ev("StopFinal", 9, { id: 0n, amount: 800_000_000n, reserved: 0n }),
  ];
  assert.deepEqual(accountsToRead(pool).map((a) => [a.account, a.kind]), [[E1, "exam"], [E2, "exam"], [E3, "exam"], [S1, "stage"]]);
  const logs = new Map([
    [E1.toLowerCase(), [ev("Filled", 1, {}), ev("Filled", 2, {}), ev("Passed", 3, { trader: T1, equity: 1n })]],
    [E2.toLowerCase(), [ev("Stopped", 5, { status: 3n, reason: 2n, equity: -1n })]],
    [S1.toLowerCase(), [ev("Filled", 5, {}), ev("StepPassed", 5, { step: 0n, equity: 1n }), ev("Stopped", 8, { status: 4n, reason: 0n, equity: 0n })]],
  ]);
  const day = 86_400;
  const times = new Map([[1, 10 * day], [2, 10 * day + 5], [5, 11 * day]]);
  const c = activityCounts(pool, logs, times);
  const t = c.traders.find((x) => x.trader === T1.toLowerCase());
  assert.deepEqual([t.purchases, t.spent, t.brought], [2, 130_000_000n, 130_000_000n - 650_000n]);
  assert.deepEqual([t.examsPassed, t.examsBreached, t.examsExpired, t.stagesStarted, t.stagesPassed, t.stagesExpired,
    t.stepsPassed, t.fills, t.fillDays], [1, 1, 0, 1, 0, 1, 1, 3, 2]);
  assert.deepEqual([t.payouts.due, t.payouts.stopped, t.payouts.refused, t.payouts.refusedAmount, t.payouts.paid],
    [1, 1, 1, 800_000_000n, 0]);
  assert.equal(t.complete, true);
  assert.deepEqual(c.unread, [E3.toLowerCase()], "the exam whose logs were not given");
  const x = c.traders.find((k) => k.trader === X.toLowerCase());
  assert.equal(x.complete, false, "its trader's counts from exam logs are too low, and say so");
  const code = c.codes[0];
  assert.deepEqual([code.traders, code.purchases, code.examsPassed, code.examsBreached, code.stagesExpired, code.complete],
    [2, 3, 1, 1, 1, false]);
  assert.deepEqual(EXAM_END, { 3: "breached", 4: "expired" });
  // A fill whose block time was not read: the days are not guessed.
  assert.equal(activityCounts(pool, logs, new Map([[1, 0]])).traders.find((k) => k.trader === T1.toLowerCase()).fillDays, null);
});
