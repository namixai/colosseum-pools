// node --test app/tests/simoperator.test.mjs
//
// The operator's section of the control panel (app/lib/simoperator.js), built to mockup 04: who is shown what by the
// role the chain gives the wallet (the pool's approver, the factory's checker, closer and operator), the queue in claim order with
// its times, the stop form's four reasons, the stops with their 14 days, the owner's request, the words before each
// call, and that the page carries no word Alex struck and no link to the user panel. On a made-up pool's reads.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SIM_OPERATOR_ABI, SIM_OPERATOR_FACTORY_ABI, STOP_FORM, mergeAbis, operatorRoles, roleWords, can, stopFromForm, stopWords,
  approveWords, liftWords, finalizeWords, cancelWords, operatorRefusal, lineLabel, operatorModel, selectionCall,
  operatorHeaderHtml, messageHtml, operatorHtml, NO_ROLE_WORDS, NO_WALLET_WORDS, idText,
} from "../lib/simoperator.js";
import { SIM_POOL_APPROVAL_ABI, SIM_POOL_ABI, STOP_RESERVE } from "../lib/simpool.js";
import { OPERATOR_ABI, PAYOUT_DELAY, PAYOUT_HARD } from "../lib/operator.js";
import { panelOf } from "../lib/simpanel.js";
import { keccak256 } from "./keccak.mjs";
import { payoutRange, PAYOUT_WINDOW, STOP_LOOKBACK, stopIdsFromEvents, stopScanNote, stopsHtml } from "../lib/simoperator.js";
import { createConfirm, createPending } from "../lib/simconfirm.js";
import { newScan, scanStops, planRanges, isDone, serial, addLogs, BATCH, WINDOW } from "../lib/stopscan.js";
import { queueHtml, unreadHtml } from "../lib/simoperator.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (...p) => readFileSync(join(ROOT, ...p), "utf8");
const POOL = "0x00000000000000000000000000000000000000a1";
const APPROVER = "0x00000000000000000000000000000000000000b1";
const CHECKER = "0x00000000000000000000000000000000000000c1";
const CLOSER = "0x00000000000000000000000000000000000000d1";
const OPERATOR = "0x00000000000000000000000000000000000000f1";
const STRANGER = "0x00000000000000000000000000000000000000e1";
const TRADER = "0x0000000000000000000000000000000000000101";
const OTHER = "0x0000000000000000000000000000000000000299";
const ZERO = "0x0000000000000000000000000000000000000000";
const U = 1_000_000n;
const H = 3600;
const D = 24 * H;
const NOW = 1_800_000_000;
const A = `0x${"a".repeat(64)}`;
const B = `0x${"b".repeat(64)}`;
const C = `0x${"c".repeat(64)}`;

const payout = (o) => ({ account: `0x${String(o.n).padStart(40, "0")}`, trader: TRADER, amount: 800n * U, dueAt: NOW + 10 * H, state: 1,
  wasStopped: false, stoppedAt: 0, approved: false, ...o });
const MENU = [{ size: 10_000_000_000n, price: 1_000_000_000n }, { size: 5_000_000_000n, price: 500_000_000n }];
const TIERS = [{ maxDrawdownBps: 600, targetBps: 1000, drawdownKind: 0 }, { maxDrawdownBps: 300, targetBps: 900, drawdownKind: 1 }];

/** Three waiting payouts (claimed 60 h ago, 100 h ago, 5 h ago), one stopped 1 day ago, one stopped 15 days ago. */
function reads(over = {}) {
  const payouts = [
    payout({ n: 1, dueAt: NOW - 28 * H, approved: true }),            // 0: claimed 100 h ago, approved, past its 72 h
    payout({ n: 2, dueAt: NOW + 12 * H }),                             // 1: claimed 60 h ago, 72 h running
    payout({ n: 3, dueAt: NOW - 5 * H }),                              // 2: claimed 77 h ago, past 72 h, waiting for the approver
    payout({ n: 4, dueAt: NOW + 67 * H }),                             // 3: claimed 5 h ago
    payout({ n: 5, state: 2, wasStopped: true, stoppedAt: NOW - D, amount: 400n * U }),
    payout({ n: 6, state: 5, wasStopped: true, stoppedAt: NOW - 15 * D, amount: 400n * U }),
    payout({ n: 7, state: 3 }),
  ];
  const r = { now: NOW, latest: LATEST, chainId: CHAIN, approver: APPROVER, payouts, firstId: 0, menu: MENU, tiers: TIERS,
    accountLines: new Map(payouts.map((p, i) => [p.account.toLowerCase(), i % 2])),
    reasons: new Map([[4, 1], [5, 3]]), payoutsPerDay: 2, payRoom: 1, nextPayAt: 0, requested: 300n * U, readyAt: NOW + 2 * D, ...over };
  // Each payout waiting has a fresh clean review, as the review service writes it, unless the test hands its own.
  if (!("reviews" in over)) r.reviews = Object.fromEntries(r.payouts.map((p, i) => [r.firstId + i, p]).filter(([, p]) => p && p.state === 1)
    .map(([id, p]) => [id, cleanReview(id, p)]));
  return r;
}
const CHAIN = 998;
const LATEST = 5_000_000;
function cleanReview(id, p, over = {}) {
  return { schema: "payout-review/3", chainId: CHAIN, pool: POOL, id, trader: p.trader, amount: String(p.amount),
    claimedAt: Number(p.dueAt) - 72 * H, dueAt: Number(p.dueAt), verdict: "clean", action: "approve", reasons: [],
    call: { kind: "approve" }, readAt: { block: LATEST - 10, time: NOW - 60 }, ...over };
}
const model = (roles, over) => operatorModel({ pool: POOL, roles, reads: reads(over), keccak256 });
const page = (roles, over) => operatorHtml(model(roles, over), { name: "Pool 0xa1…", role: roleWords(roles) });

// ── who is looking ─────────────────────────────────────────────────

test("the roles are the chain's: the pool's approver, the factory's checker, closer and operator", () => {
  const roles = (account) => operatorRoles({ account, approver: APPROVER, checker: CHECKER, closer: CLOSER, operator: OPERATOR });
  assert.deepEqual(roles(null), { kind: "no-wallet", roles: [] });
  assert.deepEqual(roles(STRANGER), { kind: "no-role", roles: [] });
  assert.deepEqual(roles(APPROVER.toUpperCase().replace("0X", "0x")), { kind: "operator", roles: ["approver"] });
  assert.deepEqual(roles(CHECKER).roles, ["checker"]);
  assert.deepEqual(roles(CLOSER).roles, ["closer"]);
  assert.deepEqual(roles(OPERATOR), { kind: "operator", roles: ["operator"] });
  // one key may hold two roles
  assert.deepEqual(operatorRoles({ account: CHECKER, approver: CHECKER, checker: CHECKER, closer: CLOSER }).roles, ["approver", "checker"]);
  // a zero address names nobody: the wallet that is the zero address is not the approver of a pool with none
  assert.equal(operatorRoles({ account: ZERO, approver: ZERO, checker: CHECKER, closer: CLOSER }).kind, "no-role");
  assert.equal(roleWords(["approver", "checker"]), "Approver · Checker");
  assert.equal(roleWords(["operator"]), "Factory operator");
});

test("each role can do what its contract call allows, and no more", () => {
  assert.deepEqual(can(["approver"]), { seeQueue: true, approve: true, stop: false, lift: false, seeStops: false, seeRequests: false, cancelRequest: false });
  assert.deepEqual(can(["checker"]), { seeQueue: true, approve: false, stop: true, lift: false, seeStops: true, seeRequests: true, cancelRequest: false });
  // lifting a stop is the factory's operator's (SimPool.liftStop), and the checker's no more
  assert.deepEqual(can(["operator"]), { seeQueue: false, approve: false, stop: false, lift: true, seeStops: true, seeRequests: false, cancelRequest: false });
  assert.deepEqual(can(["closer"]), { seeQueue: false, approve: false, stop: false, lift: false, seeStops: false, seeRequests: true, cancelRequest: true });
  assert.deepEqual(can([]), { seeQueue: false, approve: false, stop: false, lift: false, seeStops: false, seeRequests: false, cancelRequest: false });
});

test("a wallet with no role sees one sentence and nothing else; a wallet not connected is asked to connect", () => {
  const body = messageHtml("no-role");
  assert.match(body, new RegExp(NO_ROLE_WORDS));
  assert.equal((body.match(/<p>/g) || []).length, 1);
  assert.doesNotMatch(body, /<button|<input|<select|<table|so-row/);
  assert.match(messageHtml("no-wallet"), new RegExp(NO_WALLET_WORDS));
  const head = operatorHeaderHtml({ address: POOL, account: "0xe1…", state: "no-role", roles: [] });
  assert.doesNotMatch(head, /<nav/, "no menu for a wallet with no role");
  assert.match(head, /No role in this pool/);
});

// ── the queue ──────────────────────────────────────────────────────

test("the queue is in the order the payouts were claimed, each with its times in 'not before' words", () => {
  const m = model(["approver"]);
  assert.deepEqual(m.queue.map((r) => r.id), [0, 1, 2, 3], "only the waiting ones, by id");
  const words = Object.fromEntries(m.queue.map((r) => [r.id, r.words]));
  // approved: one time
  assert.match(words[0].lines[0], /^approved: not before 20\d\d-\d\d-\d\d \d\d:\d\d UTC$/);
  assert.equal(words[0].text, "Approved · can be paid now");
  // not approved: both times, 72 hours with approval and 7 days without
  assert.match(words[1].lines[0], /^with approval: not before /);
  assert.match(words[1].lines[1], /^without it: not before .* \(7 days\)$/);
  assert.equal(words[1].text, "Waiting · 72 h running");
  assert.equal(words[2].text, "Past 72 h · waiting for the approver");
  assert.equal(m.queue[1].hardAt - m.queue[1].dueAt, PAYOUT_HARD - PAYOUT_DELAY);
  assert.equal(m.queue[1].claimedAt, m.queue[1].dueAt - PAYOUT_DELAY);
});

test("the line is the size and the tier of the trader's account", () => {
  assert.equal(lineLabel(MENU, TIERS, 0), "10K · loss 6% · target 10%");
  assert.equal(lineLabel(MENU, TIERS, 1), "5K · loss 3% · target 9%");
  assert.equal(lineLabel(MENU, TIERS, 9), "line 9");
  const m = model(["approver"]);
  assert.equal(m.queue[0].line, "10K · loss 6% · target 10%");
  assert.equal(m.queue[1].line, "5K · loss 3% · target 9%");
});

test("a pool with no approver says so: every payout goes out at its 7 days", () => {
  const m = model(["checker"], { approver: ZERO });
  assert.equal(m.nobody, true);
  const past = m.queue.find((r) => r.id === 2);
  assert.equal(past.status, "no-approver");
  assert.equal(past.words.text, "No approver set · goes out at the 7 days");
  assert.ok(!past.words.lines.some((l) => l.startsWith("with approval")), "no approval to wait for");
  assert.equal(past.approve, null, "nothing to approve");
});

test("the five figures: the queue, the day's payouts, the next slot, the stops open, the requests", () => {
  const s = model(["checker"]).summary;
  assert.deepEqual(s, { inQueue: 4, paidDay: 1, perDay: 2, nextSlot: "now", stopsOpen: 1, requests: 1 });
  const full = model(["checker"], { payRoom: 0, nextPayAt: NOW + 5 * H }).summary;
  assert.match(full.nextSlot, /UTC$/);
  assert.equal(full.paidDay, 2);
});

// ── what each role is drawn ────────────────────────────────────────

test("the approver is drawn the queue and its approve buttons, no stop form and no stops", () => {
  const html = page(["approver"]);
  assert.match(html, /id="queue"/);
  assert.match(html, /data-approve="1"/);
  assert.match(html, /id="so-approve-sel"/);
  assert.match(html, /class="so-pick"/);
  assert.doesNotMatch(html, /data-approve="0"/, "an approved payout has nothing to approve");
  assert.doesNotMatch(html, /id="stop-form"|data-stop-for|id="stops"|id="requests"|data-lift|data-cancel/);
});

test("the checker is drawn the stop form, the stops without a lift and the request to look at; not the approve buttons", () => {
  const html = page(["checker"]);
  assert.match(html, /id="stop-form"/);
  assert.match(html, /data-stop-for="1"/);
  assert.doesNotMatch(html, /data-stop-for="4"|data-stop-for="0"\b.*data-stop-for="0"/, "a payout stopped already is not offered a stop");
  assert.match(html, /id="stops"/);
  assert.doesNotMatch(html, /data-lift/, "the checker stops; lifting is the factory's operator's");
  assert.match(html, /data-final="5"[^>]*disabled/, "a stop already final has nothing left to send");
  assert.match(html, /data-final="4"[^>]*disabled/, "and one inside its 14 days is not final yet");
  const late = page(["checker"], { payouts: reads().payouts.map((p, i) => (i === 4 ? { ...p, stoppedAt: NOW - 15 * D } : p)) });
  assert.match(late, /data-final="4"(?![^>]*disabled)/, "a stop whose 14 days ended can be made final");
  assert.match(html, /id="requests"/);
  assert.doesNotMatch(html, /data-cancel/, "the contract gives the checker no cancel");
  assert.match(html, /View only: the contract lets the closer cancel, not the checker/);
  assert.doesNotMatch(html, /data-approve|so-pick|so-approve-sel/);
});

test("the factory's operator is drawn the stops with their lift, and nothing of the queue, the stop form or the requests", () => {
  const html = page(["operator"]);
  assert.match(html, /Stops <em>&amp; lifts<\/em>/);
  assert.match(html, /id="stops"/);
  assert.match(html, /Stops open/);
  assert.match(html, /data-lift="4"(?![^>]*disabled)/, "a stop inside its 14 days can be lifted");
  assert.match(html, /data-lift="5"[^>]*disabled/, "a stop past its 14 days cannot");
  assert.match(html, /data-final="4"[^>]*disabled/, "one inside its 14 days is not final yet");
  const late = page(["operator"], { payouts: reads().payouts.map((p, i) => (i === 4 ? { ...p, stoppedAt: NOW - 15 * D } : p)) });
  assert.match(late, /data-lift="4"[^>]*disabled/, "nor one whose 14 days ended");
  assert.match(late, /data-final="4"(?![^>]*disabled)/, "that one anyone makes final");
  assert.doesNotMatch(html, /id="queue"|id="stop-form"|data-stop-for|id="requests"|data-approve|data-cancel|Payouts in queue/);
});

test("a wallet that is only the factory's operator is told of payouts not read, in the stops; the checker once, in the queue", () => {
  const holes = reads().payouts.map((p, i) => (i === 2 ? null : p));
  const op = page(["operator"], { payouts: holes });
  assert.match(op, /id="stops"[\s\S]*id="so-unread-payouts"/);
  assert.match(op, /1 payout could not be read \(#0002\); a stop among them is not shown/);
  assert.equal(page(["checker"], { payouts: holes }).match(/id="so-unread-payouts"/g).length, 1, "in the queue, not twice");
  assert.doesNotMatch(page(["operator"]), /so-unread-payouts/, "every payout read: nothing to say");
});

test("the closer is drawn the request and its cancel, nothing of the queue or the stops", () => {
  const html = page(["closer"]);
  assert.match(html, /id="requests"/);
  assert.match(html, /data-cancel="1"/);
  assert.doesNotMatch(html, /id="queue"|id="stop-form"|id="stops"|data-approve|Payouts in queue/);
  assert.match(html, /Requests to check/);
});

test("a lapsed request has nothing to cancel; no request says so", () => {
  assert.match(page(["closer"], { readyAt: NOW - 5 * D }), /Nothing to cancel/);
  assert.doesNotMatch(page(["closer"], { readyAt: NOW - 5 * D }), /data-cancel/);
  assert.match(page(["closer"], { requested: 0n, readyAt: 0 }), /No request to withdraw/);
});

test("a wallet with two roles is drawn both", () => {
  const html = page(["approver", "checker"]);
  assert.match(html, /data-approve="1"/);
  assert.match(html, /id="stop-form"/);
});

// ── the stops ──────────────────────────────────────────────────────

test("a stop shows its reason, its 14 days and what can be done with it", () => {
  const m = model(["operator"]);
  assert.deepEqual(m.stops.map((s) => [s.id, s.state, s.reason]), [[4, "window", 1], [5, "final", 3]].map((x) => x),
    "stopped 1 day ago: inside its 14 days; refused: final");
  assert.equal(m.stops[0].reserveEnds, NOW - D + STOP_RESERVE);
  assert.equal(m.stops[0].canLift, true);
  assert.equal(m.stops[0].canFinalize, false);
  assert.match(m.stops[0].reasonText, /Opposite positions/);
  const late = model(["operator"], { payouts: reads().payouts.map((p, i) => (i === 4 ? { ...p, stoppedAt: NOW - 15 * D } : p)) });
  assert.equal(late.stops[0].state, "due-final");
  assert.equal(late.stops[0].canFinalize, true);
  assert.equal(late.stops[0].canLift, false);
  // the checker sees the same stops and makes one final, and lifts none
  const checker = model(["checker"], { payouts: reads().payouts.map((p, i) => (i === 4 ? { ...p, stoppedAt: NOW - 15 * D } : p)) });
  assert.deepEqual(checker.stops.map((s) => [s.id, s.canLift, s.canFinalize]), [[4, false, true], [5, false, false]]);
  assert.ok(model(["checker"]).stops.every((s) => !s.canLift));
});

test("an approver does not lift, nor does the checker", () => {
  const m = model(["approver"]);
  assert.ok(m.stops.every((s) => !s.canLift && !s.canFinalize));
  assert.ok(model(["checker"]).stops.every((s) => !s.canLift));
});

// ── the stop form ──────────────────────────────────────────────────

test("the four reasons are the contract's, each with the evidence it needs", () => {
  assert.deepEqual(STOP_FORM.map((f) => f.reason), [1, 2, 3, 4]);
  assert.match(STOP_FORM[0].needs, /the other account/);
  assert.match(STOP_FORM[1].needs, /link the wallets/);
  assert.match(STOP_FORM[2].needs, /hash of the published dispute text/);
  assert.match(STOP_FORM[3].needs, /hash of the published analysis/);
});

test("the form builds the call the pool would take, or says what is missing", () => {
  const ok = (fields) => stopFromForm({ pool: POOL, fields: { id: 2, ...fields }, keccak256 });
  assert.equal(ok({ reason: 1, paired: OTHER, trades: `${A}, ${B}`, links: "" }).ok, true);
  assert.equal(ok({ reason: 1, paired: OTHER, trades: `${A} ${B}`, links: "" }).call.method, "stopPayout");
  assert.match(ok({ reason: 1, paired: "", trades: `${A},${B}`, links: "" }).problem, /needs the other account/);
  assert.match(ok({ reason: 1, paired: OTHER, trades: A, links: "" }).problem, /both trades/);
  assert.match(ok({ reason: 2, paired: OTHER, trades: `${A},${B}`, links: "" }).problem, /needs the links/);
  assert.equal(ok({ reason: 2, paired: OTHER, trades: `${A},${B}`, links: C }).ok, true);
  assert.equal(ok({ reason: 3, paired: "", trades: A, links: C }).ok, true);
  assert.match(ok({ reason: 3, paired: OTHER, trades: A, links: C }).problem, /names no other account/);
  assert.match(ok({ reason: 3, paired: "", trades: A, links: `${B},${C}` }).problem, /exactly one link/);
  assert.equal(ok({ reason: 4, paired: "", trades: `${A},${B}`, links: C }).ok, true, "prohibited play may name no other account");
  assert.equal(ok({ reason: 4, paired: OTHER, trades: `${A},${B}`, links: C }).ok, true);
  assert.match(ok({ reason: 4, paired: "", trades: A, links: C }).problem, /at least two/);
  assert.match(ok({ reason: 9, paired: "", trades: A, links: "" }).problem, /four reasons/);
  assert.match(stopFromForm({ pool: POOL, fields: { id: "", reason: 1 }, keccak256 }).problem, /Choose the payout/);
  assert.match(ok({ reason: 1, paired: OTHER, trades: "not-a-hash,also-not", links: "" }).problem, /not 32 bytes/);
});

test("a stop is signed by the checker's key; the words say what it will do", () => {
  const got = stopFromForm({ pool: POOL, fields: { id: 2, reason: 3, paired: "", trades: A, links: C }, keccak256 });
  assert.equal(got.call.signer, "checker");
  const row = model(["checker"]).queue.find((r) => r.id === 2);
  const words = stopWords(row, 3, NOW);
  assert.match(words, /Writes a stop on payout #0002 \(\$800\) with reason 3 on chain/);
  assert.match(words, /stays in the pool, set aside, for 14 days/);
  assert.match(words, /may object/);
});

// ── the words before each call ─────────────────────────────────────

test("every call is described in words before it is sent", () => {
  const m = model(["approver", "checker", "closer"]);
  const rows = m.queue.filter((r) => r.approvable);
  const sentence = approveWords(rows.slice(0, 2));
  assert.match(sentence, /^Approves 2 payouts #0001 \(\$800\), #0002 \(\$800\)\./);
  assert.match(sentence, /instead of 7 days after its claim/);
  assert.match(sentence, /Approving sets neither the amount nor the trader/);
  assert.match(approveWords([rows[0]]), /^Approves payout #0001/);
  assert.match(liftWords(m.stops[0]), /Lifts the stop of payout #0004 \(\$400\)/);
  assert.match(finalizeWords(m.stops[1]), /final.*Anyone may send it/);
  assert.match(cancelWords(m.request), /Cancels the creator's request to withdraw \$300\..*never moves the money/);
  assert.equal(idText(7), "#0007");
});

test("approving several payouts is one call; a red pool is approved one by one", () => {
  const m = model(["approver"]);
  const rows = m.queue.filter((r) => r.approvable);
  const got = selectionCall({ pool: POOL, rows, keccak256 });
  assert.equal(got.ok, true);
  assert.equal(got.call.method, "approvePayouts");
  assert.deepEqual(got.call.args[0], rows.map((r) => BigInt(r.id)));
  assert.equal(got.call.signer, "approver");
  const red = selectionCall({ pool: POOL, rows: rows.map((r) => ({ ...r, poolRed: true })), keccak256 });
  assert.equal(red.ok, false);
  assert.match(red.problem, /approve each payout on its own/);
  assert.equal(selectionCall({ pool: POOL, rows: [], keccak256 }).ok, false);
});

test("the pool's refusals are said in words", () => {
  for (const name of ["NotApprover", "NotChecker", "NotOperator", "NotCreator", "BadPayout", "BadReason", "BadPaired", "ReserveOver", "ReserveRunning", "NoRequest", "Done"]) {
    assert.match(operatorRefusal(name), /^Refused: /, name);
  }
  assert.equal(operatorRefusal("Nothing"), "");
});

// ── the page and the rules Alex set ────────────────────────────────

test("no word Alex struck, no link to the user panel, no buy button", () => {
  const html = `${operatorHeaderHtml({ address: POOL, account: "0xc1…", state: "operator", roles: ["checker"] })}${page(["approver", "checker", "closer"])}`;
  assert.doesNotMatch(html, /simulat|paper/i);
  assert.doesNotMatch(html, /href="#\/sim\//, "the management panel never links to the user panel");
  assert.doesNotMatch(html, /class="sp-buy"|>Buy</);
  assert.match(html, /Management panel/);
});

test("the header names the sections of the role and reads the role from the contract", () => {
  const head = (roles) => operatorHeaderHtml({ address: POOL, account: "0x…", state: "operator", roles });
  assert.match(head(["checker"]), /data-jump="queue"[\s\S]*data-jump="stops"[\s\S]*data-jump="requests"/);
  assert.doesNotMatch(head(["closer"]), /data-jump="queue"|data-jump="stops"/);
  assert.match(head(["approver"]), /data-jump="queue"/);
  assert.doesNotMatch(head(["approver"]), /data-jump="stops"|data-jump="requests"/);
  assert.match(head(["approver", "closer"]), /Approver · Closer/);
  assert.match(head(["checker"]), /role read from contract/);
});

test("the operator's route is in the management panel's family", () => {
  assert.equal(panelOf(`#/manage/${POOL}/operator`), "manage");
  assert.equal(panelOf(`#/manage/${POOL}`), "manage");
  assert.equal(panelOf(`#/manage/${POOL}/other`), null);
});

// ── the ABI and the app ────────────────────────────────────────────

test("the ABI this page reads is the contract's: each function and event is in the source", () => {
  const pool = read("src", "sim", "SimPool.sol");
  const factory = read("src", "sim", "SimPoolFactory.sol");
  const names = (abi, kind) => abi.map((s) => new RegExp(`^${kind}\\s+(\\w+)`).exec(s)?.[1]).filter(Boolean);
  for (const n of names(SIM_OPERATOR_ABI, "function")) assert.match(pool, new RegExp(`function ${n}\\(|\\bpublic\\s+${n}\\b`), `SimPool has ${n}`);
  // an error the pool declares, or the factory's that the pool reverts with (NotOperator, of liftStop)
  for (const n of names(SIM_OPERATOR_ABI, "error")) {
    assert.ok(new RegExp(`error ${n}\\(`).test(pool) || (new RegExp(`revert SimPoolFactory\\.${n}\\(\\)`).test(pool)
      && new RegExp(`error ${n}\\(`).test(factory)), `SimPool declares ${n}, or reverts with the factory's`);
  }
  for (const n of names(SIM_OPERATOR_FACTORY_ABI, "function")) assert.match(factory, new RegExp(`\\b${n}\\b`), `the factory has ${n}`);
  // the two functions the page sends that no other library names
  assert.match(pool, /function liftStop\(uint256 id\) external \{\s+if \(msg\.sender != factory\.operator\(\)\) revert SimPoolFactory\.NotOperator\(\);/);
  assert.match(pool, /function finalizeStop\(uint256 id\) external/);
  assert.match(pool, /function approvePayouts\(uint256\[\] calldata ids\) external/);
  assert.match(pool, /function stopPayout\(uint256 id, uint8 reason, address paired, bytes32\[\] calldata trades, bytes32\[\] calldata links\)\s+external\s+onlyChecker/);
});

test("a merged ABI names each function once and keeps the widest payout()", () => {
  const abi = mergeAbis(SIM_POOL_APPROVAL_ABI, SIM_OPERATOR_ABI, OPERATOR_ABI, SIM_POOL_ABI);
  assert.equal(abi.filter((s) => s.startsWith("function payout(")).length, 1);
  assert.match(abi.find((s) => s.startsWith("function payout(")), /bool approved/);
  assert.equal(new Set(abi).size, abi.length);
  assert.equal(abi.filter((s) => /^function approver\(/.test(s)).length, 1);
});

test("the page is wired into the app's routes", () => {
  const app = read("app", "app.js");
  assert.match(app, /\^#\\\/manage\\\/\(0x\[0-9a-fA-F\]\{40\}\)\\\/operator\$/);
  assert.match(app, /simOperatorView\(m\[1\], page\)/);
  const view = read("app", "views", "simoperator.js");
  assert.doesNotMatch(view, /window\.ethereum|localStorage|privateKey/);
  assert.match(read("app", "index.html"), /href="simops\.css"/);
});

// ── review of the first version ────────────────────────────────────

test("the queue is read from the first payout not settled, however many the pool has had", () => {
  // 5 000 payouts, the oldest not settled is #1200: the queue starts there (less the stops' look-back), not at the last 200
  const r = payoutRange({ count: 5000, head: 1200 });
  assert.equal(r.first, 1200 - STOP_LOOKBACK);
  assert.equal(r.ids[0], 900);
  assert.ok(r.ids.includes(1200), "the head is in it");
  assert.equal(r.ids.length, PAYOUT_WINDOW);
  assert.equal(r.unread, 5000 - (900 + PAYOUT_WINDOW), "the later ones are said to be unread, not dropped");
  // a payout 0 still waiting is read, whatever the count
  assert.equal(payoutRange({ count: 5000, head: 0 }).ids[0], 0);
  assert.equal(payoutRange({ count: 5000, head: 100 }).ids[0], 0);
  // nothing waiting: the head is the count; the last of the look-back is read, and nothing is unread
  const idle = payoutRange({ count: 1000, head: 1000 });
  assert.equal(idle.ids[0], 700);
  assert.equal(idle.unread, 0);
  assert.equal(idle.ids.at(-1), 999);
  // more asked for: the window grows from the same start
  const more = payoutRange({ count: 5000, head: 1200, limit: 2 * PAYOUT_WINDOW });
  assert.equal(more.first, r.first);
  assert.equal(more.ids.length, 2 * PAYOUT_WINDOW);
  assert.deepEqual(payoutRange({ count: 0, head: 0 }), { first: 0, end: 0, ids: [], unread: 0 });
  // a head past the count (not read at the same moment) cannot read past the end
  assert.equal(payoutRange({ count: 10, head: 50 }).ids.at(-1), 9);
});

test("an old payout is offered its approval and its stop, and an old stop its lift", () => {
  const old = [payout({ n: 1, dueAt: NOW - 2 * H }), payout({ n: 2, state: 2, wasStopped: true, stoppedAt: NOW - D, amount: 400n * U })];
  const m = operatorModel({ pool: POOL, roles: ["approver", "checker", "operator"], reads: { ...reads({ payouts: old, firstId: 1200, reasons: new Map([[1201, 3]]), unread: 4000 }) }, keccak256 });
  assert.deepEqual(m.queue.map((r) => r.id), [1200], "ids are the pool's, not the page's index");
  assert.ok(m.queue[0].approve && m.queue[0].stoppable);
  assert.deepEqual(m.stops.map((s) => [s.id, s.reason, s.canLift]), [[1201, 3, true]]);
  assert.equal(m.unread, 4000);
  const html = operatorHtml(m, { name: "Pool", role: "" });
  assert.match(html, /id="so-more">Read later payouts/);
  assert.match(html, /4000 later payouts are not read yet/);
  assert.doesNotMatch(page(["approver"]), /so-more/, "nothing unread, no button");
});

test("a second press belongs to what was on the screen at the first", () => {
  const c = createConfirm();
  const sent = [];
  const call = (ids) => `approve:${ids.join(",")}`;
  // select payout 1 and press: armed, nothing sent
  assert.equal(c.press(call([1]), () => "send 1").action, "arm");
  // the selection changes to payout 2 and the button is pressed again: not a confirmation, nothing of payout 1 is sent
  const second = c.press(call([2]), () => "send 2");
  assert.equal(second.action, "arm");
  // pressed again with payout 2 still selected: that is the confirmation, and it sends the payout the first press named
  const third = c.press(call([2]), () => "send other");
  assert.equal(third.action, "send");
  sent.push(third.run());
  assert.deepEqual(sent, ["send 2"]);
  // a confirmation is used once
  assert.equal(c.press(call([2]), () => "again").action, "arm");
  // a change on the screen forgets it
  c.reset();
  assert.equal(c.key, null);
  assert.equal(c.press(call([2]), () => "x").action, "arm");
  // the same holds for the stop form: the call's own data is the key
  const stop = (reason) => stopFromForm({ pool: POOL, fields: { id: 2, reason, paired: OTHER, trades: `${A},${B}`, links: "" }, keccak256 }).call.data;
  const f = createConfirm();
  assert.equal(f.press(stop(1), () => 1).action, "arm");
  assert.equal(f.press(stop(1), () => 1).action, "send");
  const g = createConfirm();
  assert.equal(g.press(stop(1), () => 1).action, "arm");
  const edited = stopFromForm({ pool: POOL, fields: { id: 2, reason: 1, paired: OTHER, trades: `${A},${C}`, links: "" }, keccak256 }).call.data;
  assert.equal(g.press(edited, () => 2).action, "arm", "a changed form is not a confirmation");
});

test("one submission at a time, whatever the buttons say", async () => {
  const p = createPending();
  let release;
  const first = p.run(() => new Promise((r) => { release = r; }));
  assert.equal(p.active, true);
  assert.deepEqual(await p.run(async () => "second"), { refused: true });
  release("done");
  assert.deepEqual(await first, { refused: false, value: "done" });
  assert.equal(p.active, false);
  assert.deepEqual(await p.run(async () => "third"), { refused: false, value: "third" });
  // a failed job clears the guard
  await assert.rejects(p.run(async () => { throw new Error("x"); }), /x/);
  assert.equal(p.active, false);
});

test("the operator page uses these guards", () => {
  const view = read("app", "views", "simoperator.js");
  assert.match(view, /const pending = createPending\(\);/);
  assert.match(view, /const confirm = createConfirm\(\);/);
  assert.match(view, /confirm\.press\(call\.data, run\)/);
  assert.match(view, /disarm\(\);\s*paintSelection\(\)/, "a changed selection forgets the confirmation");
  assert.match(view, /const changed = \(\) => \{ disarm\(\); paint\(\); \};/, "so does a changed stop form");
  assert.match(view, /if \(pending\.active\) button\.disabled = true;/, "a redraw of the selection does not re-enable a pending button");
  assert.match(view, /go\.disabled = !got\.ok \|\| pending\.active;/);
  assert.match(view, /payoutRange\(\{ count, head, limit \}\)/);
  assert.doesNotMatch(view, /Math\.max\(0, Number\(count\) - PAYOUT_WINDOW\)/);
  assert.match(view, /twoStep\(button, "stop"/, "the stop form is a two-step too");
});

// ── stops from the events ──────────────────────────────────────────

test("a stop 400 payouts back is in the list and can be lifted, whatever its number", () => {
  // payout #3 was stopped a day ago; the pool has since settled 450 payouts, so its head is 450 and the window of ids
  // (from 150) does not reach #3 any more
  const range = payoutRange({ count: 850, head: 450 });
  assert.ok(!range.ids.includes(3), "the number window does not hold it");
  const stopped = payout({ n: 3, state: 2, wasStopped: true, stoppedAt: NOW - D, amount: 400n * U });
  const settled = range.ids.map((id) => payout({ n: 10_000 + id, state: 3 }));
  const base = { ...reads(), payouts: settled, firstId: range.first, reasons: new Map(), accountLines: new Map() };
  const without = operatorModel({ pool: POOL, roles: ["operator"], reads: base, keccak256 });
  assert.deepEqual(without.stops, [], "without the events the stop is not found");
  // the events name #3; it is read by its id
  const found = operatorModel({ pool: POOL, roles: ["operator"], keccak256,
    reads: { ...base, extra: [{ id: 3, payout: stopped }], reasons: new Map([[3, 2]]), stopScan: { done: true, note: "x" } } });
  assert.deepEqual(found.stops.map((s) => [s.id, s.state, s.reason, s.canLift]), [[3, "window", 2, true]]);
  assert.equal(found.summary.stopsOpen, 1);
  const html = operatorHtml(found, { name: "Pool", role: "" });
  assert.match(html, /data-lift="3"(?![^>]*disabled)/, "its lift is offered");
  // the lift is the contract's call on that id, signed by the factory's operator: the page's handler sends liftStop(stop.id)
  assert.match(read("app", "views", "simoperator.js"), /\(c\) => c\.liftStop\(id\)/);
  // an extra that the window holds too is not counted twice
  const twice = operatorModel({ pool: POOL, roles: ["checker"], keccak256,
    reads: { ...base, payouts: [stopped, ...settled], firstId: 3, extra: [{ id: 3, payout: stopped }] } });
  assert.equal(twice.stops.filter((s) => s.id === 3).length, 1);
  // a stop past its 14 days found the same way can be made final; a final one has nothing left
  const old = operatorModel({ pool: POOL, roles: ["checker"], keccak256,
    reads: { ...base, extra: [{ id: 3, payout: { ...stopped, stoppedAt: NOW - 15 * D } }, { id: 4, payout: { ...stopped, state: 5 } }] } });
  assert.deepEqual(old.stops.map((s) => [s.id, s.state, s.canFinalize]), [[3, "due-final", true], [4, "final", false]]);
});

test("the stops' ids are the events' ids, each once, oldest first", () => {
  const ev = (name, id) => ({ name, args: { id } });
  assert.deepEqual(stopIdsFromEvents([ev("PayoutStopped", 9), ev("StopLifted", 4), ev("PayoutStopped", 2), ev("PayoutStopped", 9)]), [2, 9]);
  assert.deepEqual(stopIdsFromEvents([]), []);
});

test("the stops list says how far the events were read, and offers more until the 14 days are covered", () => {
  assert.match(stopScanNote({ floor: 100, back: 100, latest: 500, head: 500, done: true }), /Every stop of the last 14 days/);
  const partial = stopScanNote({ floor: 100, back: 400, latest: 500, head: 500, done: false });
  assert.match(partial, /blocks 401–500; the 14 days reach back to block 100/);
  assert.match(stopScanNote({ floor: 100, back: 400, latest: 480, head: 500, done: false }), /Blocks after 480 are not read/);
  const m = model(["checker"], { stopScan: { done: false, note: partial } });
  assert.match(stopsHtml(m), /id="so-stops-more">Read earlier stops/);
  assert.match(stopsHtml(model(["checker"], { stopScan: { done: true, note: "done" } })), /^((?!so-stops-more)[\s\S])*$/);
  assert.doesNotMatch(stopsHtml(model(["checker"])), /so-stop-scan/, "no scan, no note");
});

test("the events are read within the node's limits, the last 14 days only, and a failed search keeps the page", () => {
  const scan = read("app", "views", "simstopscan.js");
  assert.match(scan, /iface\.getEvent\("PayoutStopped"\)\.topicHash/);
  assert.match(scan, /Number\(now\) - STOP_RESERVE - MARGIN/);
  const view = read("app", "views", "simoperator.js");
  assert.match(view, /lookForStops\(address, now, stopCalls\)/);
  assert.match(view, /Stops could not be looked for in the events/);
  assert.match(view, /moreStops\.set\(at, MORE_CALLS\)/);
});

// ── a payout the node did not answer for ───────────────────────────

test("a stop whose payout was not read stays in the list, marked not read, with no control", () => {
  const stopped = payout({ n: 3, state: 2, wasStopped: true, stoppedAt: NOW - D, amount: 400n * U });
  const base = { ...reads(), payouts: [], firstId: 450, accountLines: new Map(), reasons: new Map([[3, 1]]) };
  const m = operatorModel({ pool: POOL, roles: ["operator"], keccak256,
    reads: { ...base, extra: [{ id: 3, payout: stopped }, { id: 8, payout: null }, { id: 9, payout: undefined }] } });
  assert.deepEqual(m.stops.map((s) => [s.id, s.state, s.canLift]), [[3, "window", true], [8, "unread", false], [9, "unread", false]]);
  assert.equal(m.summary.stopsOpen, 3, "an unread stop is counted open, not dropped");
  const html = stopsHtml(m);
  assert.match(html, /data-stop="8" data-state="unread"/);
  assert.match(html, /Not read/);
  assert.doesNotMatch(html, /data-lift="8"|data-final="8"/);
  assert.match(html, /data-lift="3"/);
});

test("a hole in the window of payouts is named, never a crash and never a silent drop", () => {
  const rows = reads().payouts;
  const m = operatorModel({ pool: POOL, roles: ["approver", "checker"], keccak256,
    reads: { ...reads(), payouts: [rows[0], null, rows[2], undefined], firstId: 10, reasons: new Map(), accountLines: new Map() } });
  assert.deepEqual(m.unreadPayouts, [11, 13]);
  assert.deepEqual(m.queue.map((r) => r.id), [10, 12], "the ones read are in the queue under their own numbers");
  // the checker's queue says so, and that a stop among them is not shown
  assert.match(queueHtml(m), /id="so-unread-payouts">2 payouts could not be read \(#0011, #0013\); a stop among them is not shown\. Reload the page/);
  assert.doesNotMatch(queueHtml(model(["checker"])), /so-unread-payouts/);
  // the approver sees it too, in the queue, and not the words about stops
  const approver = operatorModel({ pool: POOL, roles: ["approver"], keccak256,
    reads: { ...reads(), payouts: [rows[0], null], firstId: 10, reasons: new Map(), accountLines: new Map() } });
  assert.match(queueHtml(approver), /1 payout could not be read \(#0011\)\. Reload the page to read it again\./);
  assert.doesNotMatch(unreadHtml(approver), /stop/);
  // and a queue that is empty because of it does not say that nothing is waiting
  const empty = operatorModel({ pool: POOL, roles: ["approver"], keccak256, reads: { ...reads(), payouts: [null], firstId: 0, reasons: new Map(), accountLines: new Map() } });
  assert.match(queueHtml(empty), /No payout is waiting among those read\./);
  assert.doesNotMatch(queueHtml(model(["approver"], { payouts: [] })), /among those read/);
});

test("an event without its id names no stop", () => {
  assert.deepEqual(stopIdsFromEvents([{ name: "PayoutStopped" }, { name: "PayoutStopped", args: {} }, { name: "PayoutStopped", args: { id: null } },
    { name: "PayoutStopped", args: { id: 0 } }]), [0]);
});

test("the events are asked for a few windows at a time, and the view hands them to the scanner", () => {
  const scan = read("app", "views", "simstopscan.js");
  assert.match(scan, /scanStops\(st, \{ read,/);
  assert.match(scan, /serial\(locks, address\.toLowerCase\(\)/, "one reading of a pool at a time");
  assert.match(scan, /Number\(now\) - STOP_RESERVE - MARGIN/);
  assert.match(read("app", "lib", "stopscan.js"), /export const BATCH = 3;/);
  assert.match(read("app", "lib", "stopscan.js"), /export const WINDOW = 1000;/);
  const view = read("app", "views", "simoperator.js");
  assert.match(view, /pool\.payout\(id\)\.catch\(\(\) => null\)/);
  assert.match(view, /payouts\[i\] && \[2, 5\]\.includes/);
  assert.match(view, /more\.disabled = true;/, "the button is off while a search runs");
});

// ── the scanner: batches, failures, one at a time ──────────────────

const log = (block, id, index = 0) => ({ blockNumber: block, index, topics: ["0xtopic", `0x${id.toString(16).padStart(64, "0")}`] });

test("windows are planned from the cursors, at most three asked at once", async () => {
  const st = newScan({ floor: 0, latest: 10_000 });
  st.head = 12_500;
  const plan = planRanges(st, { forward: 8, backward: 4 });
  assert.deepEqual(plan.ahead, [[10_001, 11_000], [11_001, 12_000], [12_001, 12_500]]);
  assert.deepEqual(plan.behind, [[9_001, 10_000], [8_001, 9_000], [7_001, 8_000], [6_001, 7_000]]);
  let live = 0;
  let most = 0;
  const read = async () => { live += 1; most = Math.max(most, live); await new Promise((r) => setTimeout(r, 1)); live -= 1; return []; };
  await scanStops(st, { read, forward: 8, backward: 9 });
  assert.equal(most, BATCH, "never more than three at once");
  assert.equal(st.latest, 12_500);
  assert.equal(st.back, 10_000 - 9 * WINDOW);
});

test("a batch that fails keeps the stops found before it, and its blocks are asked for again", async () => {
  const st = newScan({ floor: 0, latest: 20_000 });
  // the first press finds stop #3 in the window just below the newest block
  const first = await scanStops(st, { read: async (from) => (from === 19_001 ? [log(19_500, 3)] : []), forward: 0, backward: 3 });
  assert.equal(first.failure, null);
  assert.deepEqual(stopIdsFromEvents([...st.events.values()]), [3]);
  const backAfterFirst = st.back;
  // the second press: its batch fails
  const boom = new Error("node refused");
  const second = await scanStops(st, { read: async (from) => { if (from === backAfterFirst - 2 * WINDOW + 1) throw boom; return [log(backAfterFirst - 1, 4)]; }, forward: 0, backward: 3 });
  assert.equal(second.failure, boom);
  assert.deepEqual(stopIdsFromEvents([...st.events.values()]), [3], "#3 is still found, and nothing of the failed batch is kept");
  assert.equal(st.back, backAfterFirst, "the cursor did not pass the failed batch");
  assert.equal(isDone(st), false);
  // the third press asks for the same blocks again and goes on
  const asked = [];
  const third = await scanStops(st, { read: async (from, to) => { asked.push([from, to]); return []; }, forward: 0, backward: 3 });
  assert.equal(third.failure, null);
  assert.deepEqual(asked[0], [backAfterFirst - WINDOW + 1, backAfterFirst]);
  assert.ok(st.back < backAfterFirst);
});

test("a log without the stop's id is skipped, and the end of the 14 days is the end", async () => {
  const st = newScan({ floor: 18_500, latest: 20_000 });
  addLogs(st, [{ blockNumber: 1, index: 0 }, { blockNumber: 2, index: 0, topics: ["0xt"] }, null, log(19_900, 7)]);
  assert.deepEqual(stopIdsFromEvents([...st.events.values()]), [7]);
  await scanStops(st, { read: async () => [], forward: 0, backward: 5 });
  assert.equal(st.back, 18_499);
  assert.equal(isDone(st), true);
  assert.equal(isDone({ ...st, head: 20_050 }), false, "blocks not yet read are not 'done'");
});

test("readings of one pool go one at a time", async () => {
  const locks = new Map();
  const order = [];
  const job = (name, ms, fail = false) => async () => { order.push(`${name} start`); await new Promise((r) => setTimeout(r, ms)); order.push(`${name} end`); if (fail) throw new Error(name); return name; };
  const a = serial(locks, "pool", job("a", 20, true));
  const b = serial(locks, "pool", job("b", 1));
  const other = serial(locks, "other", job("c", 1));
  await assert.rejects(a, /a/);
  assert.equal(await b, "b");
  await other;
  assert.deepEqual(order.filter((x) => /^[ab]/.test(x)), ["a start", "a end", "b start", "b end"], "b waits for a, even when a failed");
  assert.ok(order.indexOf("c start") < order.indexOf("a end"), "another pool is not held up");
});
