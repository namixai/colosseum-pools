// The operator's page of a simulated pool (app/lib/operator.js): the calls it builds, byte for byte as Foundry's
// `cast calldata` writes them; the payouts waiting, with their times and their reviews; the request to withdraw; the
// pool's state.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  OPERATOR_ABI, PAYOUT_DELAY, PAYOUT_HARD, WITHDRAW_DELAY, WITHDRAW_WINDOW, REVIEW_SCHEMA, SIGNERS,
  encodeCall, approveCall, stopCall, requestWithdrawCall, withdrawCall, cancelWithdrawCall, closeCall,
  awaitingApproval, reviewFor, withdrawRequest, poolState, approveAnywayCall, HOLD_WARNING,
} from "../lib/operator.js";
import { keccak256 } from "./keccak.mjs";

const POOL = "0x00000000000000000000000000000000000000a1";
const T1 = "0x0000000000000000000000000000000000000101";
const S1 = "0x0000000000000000000000000000000000000301";
const OTHER = "0x0000000000000000000000000000000000000299";
const ZERO = "0x0000000000000000000000000000000000000000";
const A = `0x${"a".repeat(64)}`;
const B = `0x${"b".repeat(64)}`;
const C = `0x${"c".repeat(64)}`;
const W = (hex) => hex.padStart(64, "0");
const H = 3600;
const D = 24 * H;

// ── the calls, against `cast calldata` ─────────────────────────────

const CAST_APPROVE_3_7 = `0xd6532c4d${W("20")}${W("2")}${W("3")}${W("7")}`;
const CAST_STOP = `0xf30c3d00${W("5")}${W("2")}${W("299")}${W("a0")}${W("100")}${W("2")}${"a".repeat(64)}${"b".repeat(64)}`
  + `${W("1")}${"c".repeat(64)}`;

test("the calls are encoded as cast calldata encodes them", () => {
  assert.equal(encodeCall("approvePayouts(uint256[])", [[3n, 7n]], keccak256), CAST_APPROVE_3_7);
  assert.equal(encodeCall("approvePayouts(uint256[])", [[]], keccak256), `0xd6532c4d${W("20")}${W("0")}`);
  assert.equal(encodeCall("stopPayout(uint256,uint8,address,bytes32[],bytes32[])", [5n, 2, OTHER, [A, B], [C]], keccak256),
    CAST_STOP);
  assert.equal(encodeCall("requestWithdraw(uint256)", [1_000_000n], keccak256), `0x745400c9${W("f4240")}`);
  assert.equal(encodeCall("cancelWithdraw()", [], keccak256), "0x84b76824");
  assert.equal(encodeCall("withdraw()", [], keccak256), "0x3ccfd60b");
  assert.equal(encodeCall("close()", [], keccak256), "0x43d726d6");
  assert.throws(() => encodeCall("f(uint256)", [], keccak256), /takes 1 arguments/);
  assert.throws(() => encodeCall("f(address)", ["0x12"], keccak256), /not an address/);
  assert.throws(() => encodeCall("f(uint256)", [-1n], keccak256), /not a uint256/);
  assert.throws(() => encodeCall("f(bytes32)", ["0x12"], keccak256), /not 32 bytes/);
});

test("an approval is the approver's one signature over the payouts named, each once", () => {
  const c = approveCall({ pool: POOL, ids: [3, 7], keccak256 });
  assert.deepEqual({ to: c.to, method: c.method, args: c.args, data: c.data, signer: c.signer },
    { to: POOL, method: "approvePayouts", args: [[3n, 7n]], data: CAST_APPROVE_3_7, signer: "approver" });
  assert.equal(c.signs, SIGNERS.approver);
  assert.throws(() => approveCall({ pool: POOL, ids: [], keccak256 }), /no payout/);
  assert.throws(() => approveCall({ pool: POOL, ids: [3, 3], keccak256 }), /named twice/);
  assert.throws(() => approveCall({ pool: "0x12", ids: [3], keccak256 }), /pool's address/);
});

test("a stop is checked as the pool checks it, reason by reason", () => {
  const ok = stopCall({ pool: POOL, id: 5, reason: 2, paired: OTHER, trades: [A, B], links: [C], keccak256 });
  assert.equal(ok.data, CAST_STOP);
  assert.equal(ok.signer, "checker");
  stopCall({ pool: POOL, id: 5, reason: 1, paired: OTHER, trades: [A, B], links: [], keccak256 });
  stopCall({ pool: POOL, id: 5, reason: 3, paired: ZERO, trades: [A], links: [C], keccak256 });
  const bad = [
    [{ reason: 1, paired: OTHER, trades: [A, B], links: [C] }, /reason 1 has no links/],
    [{ reason: 2, paired: OTHER, trades: [A, B], links: [] }, /reason 2 needs the links/],
    [{ reason: 1, paired: ZERO, trades: [A, B], links: [] }, /the other account/],
    [{ reason: 1, paired: OTHER, trades: [A], links: [] }, /both trades/],
    [{ reason: 3, paired: OTHER, trades: [A], links: [C] }, /names no other account/],
    [{ reason: 3, paired: ZERO, trades: [], links: [C] }, /the transaction that shows it/],
    [{ reason: 3, paired: ZERO, trades: [A], links: [C, B] }, /exactly one link/],
    [{ reason: 4, paired: OTHER, trades: [A, B], links: [] }, /no reason 4/],
  ];
  for (const [args, msg] of bad) assert.throws(() => stopCall({ pool: POOL, id: 5, keccak256, ...args }), msg);
});

test("the creator's calls are the Safe's, a cancel is the closer's or the creator's, an early close the closer's", () => {
  const r = requestWithdrawCall({ pool: POOL, amount: 1_000_000n, withdrawable: 2_000_000n, keccak256 });
  assert.deepEqual([r.data, r.signer, r.signs], [`0x745400c9${W("f4240")}`, "creator", SIGNERS.creator]);
  assert.throws(() => requestWithdrawCall({ pool: POOL, amount: 3n, withdrawable: 2n, keccak256 }), /more than/);
  assert.throws(() => requestWithdrawCall({ pool: POOL, amount: 0n, keccak256 }), /nothing/);
  assert.equal(withdrawCall({ pool: POOL, keccak256 }).signer, "creator");
  assert.equal(cancelWithdrawCall({ pool: POOL, keccak256 }).signer, "closer");
  assert.equal(cancelWithdrawCall({ pool: POOL, by: "creator", keccak256 }).signer, "creator");
  assert.throws(() => cancelWithdrawCall({ pool: POOL, by: "anyone", keccak256 }), /only the creator or the closer/);
  assert.equal(closeCall({ pool: POOL, by: "closer", lockedUntil: 100, now: 50, keccak256 }).signer, "closer");
  assert.equal(closeCall({ pool: POOL, by: "creator", lockedUntil: 100, now: 100, keccak256 }).data, "0x43d726d6");
  assert.throws(() => closeCall({ pool: POOL, by: "creator", lockedUntil: 100, now: 99, keccak256 }), /end of the term/);
  assert.throws(() => closeCall({ pool: POOL, by: "creator", now: 100, keccak256 }), /term and the time are needed/);
  assert.throws(() => closeCall({ pool: POOL, by: "creator", lockedUntil: 100, keccak256 }), /term and the time are needed/);
});

// ── the payouts waiting ────────────────────────────────────────────

const NOW = 10 * D;
function payout(id, { claimedAt, state = 1, approved } = {}) {
  const p = { account: S1, trader: T1, amount: 800_000_000n, dueAt: BigInt(claimedAt + PAYOUT_DELAY), state, wasStopped: false };
  if (approved !== undefined) p.approved = approved;
  return p;
}

function reviewDoc(id, over = {}) {
  return {
    schema: REVIEW_SCHEMA, pool: POOL, id, trader: T1, stage: S1, exam: OTHER, amount: "800000000",
    claimedAt: 0, dueAt: 0, hardAt: 0, verdict: "clean", reasons: [], evidence: { pairs: [] },
    call: { kind: "approve", to: POOL, method: "approvePayouts", signature: "approvePayouts(uint256[])", args: [[id]],
      data: approveCall({ pool: POOL, ids: [id], keccak256 }).data },
    readAt: { block: 1, time: NOW },
    ...over,
  };
}

test("each payout waiting has its claim, its 72 hours and its ceiling, and the most urgent comes first", () => {
  const structs = [
    payout(0, { claimedAt: NOW - 8 * D }),                    // past its ceiling: paid unapproved
    payout(1, { claimedAt: NOW - 1 * D }),                    // in its 72 hours
    payout(2, { claimedAt: NOW - 4 * D }),                    // past 72 hours, not approved
    payout(3, { claimedAt: NOW - 4 * D, approved: true }),    // approved and payable
    payout(4, { claimedAt: NOW - 2 * D, approved: true }),    // approved, its 72 hours not over
    payout(5, { claimedAt: NOW - 5 * D, state: 3 }),          // paid: not waiting
  ];
  const rows = awaitingApproval({ pool: POOL, structs, now: NOW, keccak256 });
  assert.deepEqual(rows.map((r) => [r.id, r.status]), [
    [0, "past-ceiling"], [3, "approved-payable"], [4, "approved"], [2, "awaiting-approval"], [1, "in-72-hours"],
  ]);
  const r2 = rows.find((r) => r.id === 2);
  assert.deepEqual([r2.claimedAt, r2.dueAt, r2.hardAt, r2.payAt, r2.approved],
    [NOW - 4 * D, NOW - D, NOW + 3 * D, NOW + 3 * D, false]);
  assert.equal(rows.find((r) => r.id === 4).payAt, NOW + D, "an approved payout is paid at its 72 hours");
  assert.equal(r2.amount, 800_000_000n);
  assert.equal(PAYOUT_HARD, 7 * D);
});

test("a hole in the list of payouts is skipped, and a payout's own id is kept", () => {
  const structs = [];
  structs[2] = { ...payout(2, { claimedAt: NOW - D }) };
  structs[5] = { ...payout(5, { claimedAt: NOW - D }), id: 9n };
  const rows = awaitingApproval({ pool: POOL, structs, now: NOW, keccak256 });
  assert.deepEqual(rows.map((r) => r.id), [2, 9]);
});

test("a pool without approvals pays at 72 hours by itself: no ceiling, nothing to approve", () => {
  const structs = [payout(0, { claimedAt: NOW - D }), payout(1, { claimedAt: NOW - 4 * D })];
  const none = { ...reviewDoc(0), call: { kind: "none", to: POOL, method: null, signature: null, args: [], data: null,
    note: "in this pool a payout goes by itself 72 hours after its claim" } };
  const rows = awaitingApproval({ pool: POOL, structs, now: NOW, keccak256, approvals: false, reviews: { 0: none } });
  assert.deepEqual(rows.map((r) => [r.id, r.status, r.hardAt, r.payAt, r.approvable]),
    [[1, "payable", null, NOW - D, false], [0, "in-72-hours", null, NOW + 2 * D, false]]);
  const r0 = rows.find((r) => r.id === 0);
  assert.equal(r0.reviewProblem, null);
  assert.equal(r0.review.call, null, "nothing to sign");
  assert.equal(r0.approve, null, "no Approve button on a pool without approvals");
});

test("a pool without the field says approved by its events", () => {
  const structs = [payout(0, { claimedAt: NOW - D }), payout(1, { claimedAt: NOW - D })];
  const rows = awaitingApproval({ pool: POOL, structs, events: [{ name: "PayoutApproved", args: { id: 1n, approver: OTHER } }],
    now: NOW, keccak256 });
  assert.deepEqual(rows.map((r) => [r.id, r.approved]), [[1, true], [0, false]]);
});

test("a payout read by position says approved by its eighth field", () => {
  const due = BigInt(NOW - 4 * D + PAYOUT_DELAY);
  const structs = [[S1, T1, 800_000_000n, due, 1, false, 0n, true], [S1, T1, 800_000_000n, due, 1, false, 0n, false]];
  const rows = awaitingApproval({ pool: POOL, structs, now: NOW, keccak256 });
  assert.deepEqual(rows.map((r) => [r.id, r.approved, r.status]), [[0, true, "approved-payable"], [1, false, "awaiting-approval"]]);
});

test("only a payout the pool would take an approval of is approvable: not one approved, not where nobody approves", () => {
  const structs = [
    payout(0, { claimedAt: NOW - 4 * D, approved: false }),   // past its 72 hours, waiting for the approver
    payout(1, { claimedAt: NOW - 4 * D, approved: true }),    // approved already: a second approval reverts
    payout(2, { claimedAt: NOW - 1 * D, approved: false }),   // in its 72 hours: can be approved ahead
  ];
  const named = awaitingApproval({ pool: POOL, structs, now: NOW, keccak256, approver: OTHER });
  assert.deepEqual(named.map((r) => [r.id, r.status, r.approvable]),
    [[1, "approved-payable", false], [0, "awaiting-approval", true], [2, "in-72-hours", true]]);
  const nobody = awaitingApproval({ pool: POOL, structs, now: NOW, keccak256, approver: ZERO });
  assert.deepEqual(nobody.map((r) => [r.id, r.status, r.approvable, r.payAt]),
    [[1, "approved-payable", false, NOW - D], [0, "no-approver", false, NOW + 3 * D], [2, "in-72-hours", false, NOW + 6 * D]]);
  assert.equal(awaitingApproval({ pool: POOL, structs, now: NOW, keccak256 })[2].approvable, true, "no approver handed in: as named");
  assert.deepEqual(named.map((r) => r.approve?.method ?? null), [null, "approvePayouts", "approvePayouts"],
    "no Approve button on a payout approved already");
  assert.deepEqual(nobody.map((r) => r.approve), [null, null, null], "nor where nobody approves");
});

test("a review is shown only where it is of this very payout and names the call the page would build", () => {
  const structs = [payout(0, { claimedAt: NOW - D }), payout(1, { claimedAt: NOW - D })];
  const rows = awaitingApproval({ pool: POOL, structs, now: NOW, reviews: { 0: reviewDoc(0) }, keccak256 });
  const r0 = rows.find((r) => r.id === 0);
  assert.equal(r0.reviewProblem, null);
  assert.equal(r0.review.verdict, "clean");
  assert.equal(r0.review.call.data, CAST_APPROVE_3_7.slice(0, 10) + W("20") + W("1") + W("0"));
  assert.equal(rows.find((r) => r.id === 1).reviewProblem, "no review");
  const row = rows.find((r) => r.id === 0);
  const cases = [
    [{ schema: "payout-review/1" }, /not a payout-review\/2/],
    [{ pool: OTHER }, /another pool/],
    [{ id: 1 }, /another payout/],
    [{ trader: OTHER }, /another trader/],
    [{ amount: "1" }, /another amount/],
    [{ readAt: { block: 1, time: NOW - 2 * D } }, /before the claim/],
    [{ readAt: { block: 1 } }, /before the claim/],
    [{ call: { kind: "approve", data: "0xd6532c4d00" } }, /call data is not its call/],
    [{ call: { kind: "stop", args: [0, 1, OTHER, [A], []] } }, /would refuse: a pair needs both trades/],
    [{ call: {} }, /no call/],
    [{ action: "stop" }, /action \(stop\) is not its call \(approve\)/],
    [{ action: "hold" }, /holds a payout yet names a call/],
  ];
  for (const [over, msg] of cases) {
    const got = reviewFor({ ...reviewDoc(0), ...over }, row, { pool: POOL, keccak256 });
    assert.equal(got.review, null);
    assert.match(got.problem, msg);
  }
  const stop = reviewFor({ ...reviewDoc(0), verdict: "suspicious", action: "stop",
    call: { kind: "stop", args: [0, 1, OTHER, [A, B], []], data: null } }, row, { pool: POOL, keccak256 });
  assert.equal(stop.review.call.method, "stopPayout");
  assert.equal(stop.review.call.signer, "checker");
});

test("a payout whose result did not count up is offered no approval, only a deliberate second step", () => {
  const structs = [payout(0, { claimedAt: NOW - D }), payout(1, { claimedAt: NOW - D })];
  const held = { ...reviewDoc(0), verdict: "suspicious", action: "hold", call: null,
    reasons: [{ kind: "recount", text: "recount on the stage: result" }],
    stopDraft: { kind: "stop-draft", reason: 3, paired: ZERO, trades: [A], links: ["<keccak256 of the published analysis>"], data: null } };
  const decide = { ...reviewDoc(1), verdict: "suspicious", action: "decide", reasons: [{ kind: "late", text: "fills later" }] };
  const rows = awaitingApproval({ pool: POOL, structs, now: NOW, reviews: { 0: held, 1: decide }, keccak256 });
  const r0 = rows.find((r) => r.id === 0);
  assert.equal(r0.reviewProblem, null);
  assert.deepEqual([r0.review.action, r0.review.call, r0.approve], ["hold", null, null], "no Approve button");
  assert.equal(r0.review.stopDraft.reason, 3);
  assert.deepEqual(r0.approveAnyway, { warning: HOLD_WARNING });
  assert.throws(() => approveAnywayCall({ pool: POOL, row: r0, confirm: "yes", keccak256 }), /warning confirmed/);
  assert.equal(approveAnywayCall({ pool: POOL, row: r0, confirm: HOLD_WARNING, keccak256 }).method, "approvePayouts");
  const r1 = rows.find((r) => r.id === 1);
  assert.equal(r1.review.action, "decide");
  assert.equal(r1.approve.method, "approvePayouts", "suspicious but counted up: the approval is there, the choice the signer's");
  assert.equal(r1.approveAnyway, null);
  assert.throws(() => approveAnywayCall({ pool: POOL, row: r1, confirm: HOLD_WARNING, keccak256 }), /not held/);
});

test("the review the script writes is read as it is written", () => {
  // The document's fields, as ops/payout_review.py's card() names them: the two hold each other to one schema.
  const src = readFileSync(new URL("../../ops/payout_review.py", import.meta.url), "utf8");
  assert.match(src, new RegExp(`SCHEMA = "${REVIEW_SCHEMA}"`));
  for (const key of ["schema", "pool", "id", "trader", "amount", "claimedAt", "dueAt", "hardAt", "verdict", "reasons",
    "evidence", "call", "stopDraft", "readAt", "mirrors", "recount"]) {
    assert.match(src, new RegExp(`"${key}":`), key);
  }
});

// ── the request to withdraw and the pool's state ───────────────────

test("a request waits a week, can be carried out for three days, then lapses", () => {
  assert.equal(withdrawRequest({ pool: POOL, requested: 0n, readyAt: 0, now: NOW, keccak256 }), null);
  const readyAt = NOW + 2 * D;
  const w = withdrawRequest({ pool: POOL, requested: 5_000_000n, readyAt, now: NOW, keccak256 });
  assert.deepEqual([w.amount, w.requestedAt, w.readyAt, w.lapsesAt, w.status],
    [5_000_000n, readyAt - WITHDRAW_DELAY, readyAt, readyAt + WITHDRAW_WINDOW, "waiting"]);
  assert.deepEqual(Object.keys(w.calls), ["cancel"]);
  assert.equal(w.calls.cancel.signer, "closer");
  const ready = withdrawRequest({ pool: POOL, requested: 5_000_000n, readyAt, now: readyAt, keccak256 });
  assert.equal(ready.status, "ready");
  assert.deepEqual(Object.keys(ready.calls).sort(), ["cancel", "withdraw"]);
  assert.equal(ready.calls.withdraw.signer, "creator");
  const lapsed = withdrawRequest({ pool: POOL, requested: 5_000_000n, readyAt, now: readyAt + WITHDRAW_WINDOW, keccak256 });
  assert.deepEqual([lapsed.status, lapsed.calls], ["lapsed", {}]);
});

test("the pool's state: what is free of what it owes, holds and keeps for stops, the term and the finish", () => {
  const reads = { balance: 10_000n, owed: 800n, held: 100n, reserved: 200n, principal: 5_000n, withdrawable: 1_000n,
    live: 7n, approver: T1, lockedUntil: NOW + D, closed: false, closedEarly: false, closedAt: 0n };
  const s = poolState(reads, NOW);
  assert.deepEqual([s.balance, s.free, s.principal, s.withdrawable, s.live, s.approver, s.finishFrom],
    [10_000n, 8_900n, 5_000n, 1_000n, 7, T1, null]);
  assert.equal(s.term.state, "locked");
  const closed = poolState({ ...reads, closed: true, closedEarly: true, closedAt: BigInt(NOW) }, NOW);
  assert.deepEqual([closed.term.state, closed.finishFrom], ["closed-early", NOW + WITHDRAW_DELAY]);
  assert.equal(poolState({}, NOW).term, null, "a pool without the term says nothing of it");
  assert.equal(poolState(undefined, NOW).balance, 0n, "nothing read: zeros, not a throw");
});

test("the ABI names the calls and events the functions build and read", () => {
  for (const s of ["function approvePayouts(uint256[] ids)", "function requestWithdraw(uint256 amount)",
    "function cancelWithdraw()", "function withdraw()", "function close()",
    "event PayoutApproved(uint256 indexed id, address indexed approver)",
    "event WithdrawRequested(uint256 amount, uint64 readyAt, uint64 lapsesAt)"]) {
    assert.ok(OPERATOR_ABI.includes(s), s);
  }
});

test("every line of the ABI is the pool's own, by its source", () => {
  // The contract's text with its whitespace folded and the data locations dropped, as an ABI line writes a call.
  const src = readFileSync(new URL("../../src/sim/SimPool.sol", import.meta.url), "utf8")
    .replace(/\s+/g, " ").replace(/ (calldata|memory)(?= )/g, "");
  for (const line of OPERATOR_ABI) {
    if (line.startsWith("event ")) {
      assert.ok(src.includes(`${line};`), line);
      continue;
    }
    const m = /^function (\w+)\(([^)]*)\)(?: view returns \((\w+)\))?$/.exec(line);
    assert.ok(m, `an ABI line this test can read: ${line}`);
    const [, name, params, returns] = m;
    // A function of the pool by its name and parameters, or the getter of a public variable of that type.
    const declared = src.includes(`function ${name}(${params})`)
      || (params === "" && returns !== undefined && src.includes(` ${returns} public ${name};`));
    assert.ok(declared, line);
  }
});
