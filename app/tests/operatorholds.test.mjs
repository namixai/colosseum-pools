// Review 12 of #336, H-1 and L-2: the operator's page offers a payout's one-signature approval only on a fresh review of
// this very payout that says approve, checked by the rules `deploy_sim approve` checks it with
// (ops/payout_review.py, card_problem); every other payout gets a separate step that says why, and asks twice. The
// reviews come from the index's route (gateway/simindex.py), and their reasons are said in words.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  REVIEW_SCHEMA, REVIEW_MAX_BLOCKS, REVIEW_MAX_S, PAYOUT_DELAY, HOLD_WORDS, awaitingApproval, approveCall, approveSelectedCall,
  holdWarning, reasonsText, reviewFor,
} from "../lib/operator.js";
import { askReviews, reviewsUrl, reviewHtml, queueHtml, operatorModel } from "../lib/simoperator.js";
import { keccak256 } from "./keccak.mjs";

const POOL = "0x00000000000000000000000000000000000000a1";
const T1 = "0x0000000000000000000000000000000000000101";
const S1 = "0x0000000000000000000000000000000000000301";
const NOW = 2_000_000_000;
const LATEST = 9_000_000;
const CHAIN = 999;
const D = 86400;
const CLAIM = NOW - D;
const payout = () => ({ account: S1, trader: T1, amount: 800_000_000n, dueAt: BigInt(CLAIM + PAYOUT_DELAY), state: 1, wasStopped: false });
const clean = (over = {}) => ({
  schema: REVIEW_SCHEMA, chainId: CHAIN, pool: POOL, id: 0, trader: T1, stage: S1, exam: null, amount: "800000000",
  claimedAt: CLAIM, dueAt: CLAIM + PAYOUT_DELAY, verdict: "clean", action: "approve", reasons: [], evidence: {}, stopDraft: null,
  call: { kind: "approve", data: approveCall({ pool: POOL, ids: [0], keccak256 }).data }, readAt: { block: LATEST - 5, time: NOW - 60 },
  ...over });
const hold = (over = {}) => clean({ verdict: "suspicious", action: "hold", call: null,
  reasons: [{ kind: "stop-unchecked", text: "stops not checked on the stage: x" }], stopDraft: { kind: "stop-draft", reason: 3 }, ...over });
const row = (reviews, extra = {}) => awaitingApproval({ pool: POOL, structs: [payout()], now: NOW, reviews, keccak256, chainId: CHAIN,
  latest: LATEST, ...extra })[0];
const offeredNone = (r, why) => {
  assert.equal(r.approve, null, "no one-signature approval");
  assert.equal(r.approveWithout, undefined, "no approval without a review");
  assert.ok(r.noApproval, "the page says why none");
  if (why) assert.match(r.noApproval.why, why);
};

test("R12-P1 no review loaded: no approval, the page says none was loaded", () => {
  offeredNone(row({}), /No review of payout 0 was loaded\./);
  offeredNone(row({}, { reviewNotes: { 0: "the index could not be reached" } }), /was loaded \(the index could not be reached\)/);
});

test("R12-P2 a hold document that names a call is thrown out, and no approval is offered", () => {
  const r = row({ 0: hold({ call: { kind: "approve" } }) });
  assert.match(r.reviewProblem, /holds a payout yet names a call/);
  offeredNone(r, /cannot be used: a review that holds a payout yet names a call/);
});

test("R12-P3 another schema, a read time missing, another chain, another claim: thrown out, no approval", () => {
  for (const [over, why] of [[{ schema: "payout-review/3" }, /not a payout-review\/4/], [{ readAt: {} }, /not read whole/],
    [{ readAt: { block: LATEST } }, /not read whole/], [{ chainId: 998 }, /another chain/], [{ chainId: undefined }, /another chain/],
    [{ dueAt: CLAIM + PAYOUT_DELAY + 1 }, /another claim/], [{ claimedAt: undefined }, /before the claim/],
    [{ amount: 800_000_001 }, /another amount/], [{ trader: S1 }, /another trader/], [{ id: 1 }, /another payout/]]) {
    const r = row({ 0: clean(over) });
    assert.match(r.reviewProblem, why, JSON.stringify(over));
    offeredNone(r, /cannot be used/);
  }
  offeredNone(row({ 0: clean() }, { chainId: null }), /another chain/);
});

test("R12-P4 a review whose action is none holds the approval back too", () => {
  const r = row({ 0: clean({ action: "none", verdict: "suspicious", call: { kind: "none", note: "not read" } }) });
  assert.equal(r.review.action, "none");
  offeredNone(r, /says none \(suspicious\)/);
});

test("R12-P5 a review whose action is stop (or decide) leaves no approval beside the stop", () => {
  const A = `0x${"a".repeat(64)}`, B = `0x${"b".repeat(64)}`;
  const s = row({ 0: clean({ verdict: "suspicious", action: "stop", call: { kind: "stop", args: [0, 1, "0x0000000000000000000000000000000000000299", [A, B], []] } }) });
  assert.equal(s.review.action, "stop");
  offeredNone(s, /says stop/);
  offeredNone(row({ 0: clean({ verdict: "suspicious", action: "decide" }) }), /says decide/);
  offeredNone(row({ 0: clean({ action: "decide" }) }), /says decide \(clean\)/);
  offeredNone(row({ 0: clean({ verdict: "suspicious" }) }), /says approve \(suspicious\)/);
});

test("R12-P6 a clean review older than an hour, in blocks or in seconds, or from the future, carries no approval", () => {
  const fresh = row({ 0: clean() });
  assert.equal(fresh.reviewProblem, null);
  assert.equal(fresh.approve.method, "approvePayouts");
  assert.equal(fresh.noApproval, null);
  for (const readAt of [{ block: LATEST - REVIEW_MAX_BLOCKS - 1, time: NOW - 60 }, { block: LATEST - 5, time: NOW - REVIEW_MAX_S - 1 },
    { block: LATEST + 1, time: NOW - 60 }, { block: LATEST - 5, time: NOW + 61 }, { block: LATEST - 5, time: CLAIM + 5 }]) {
    const r = row({ 0: clean({ readAt }) });
    assert.equal(r.reviewStale, true, JSON.stringify(readAt));
    assert.ok(r.review, "what it said is still shown");
    offeredNone(r, /too old to rest an approval on: a review read at block/);
  }
  const edge = row({ 0: clean({ readAt: { block: LATEST - REVIEW_MAX_BLOCKS, time: NOW - REVIEW_MAX_S } }) });
  assert.equal(edge.approve.method, "approvePayouts", "an hour exactly is still fresh, as card_problem has it");
  offeredNone(row({ 0: clean() }, { latest: null }), /age is unknown/);
});

test("R12-P7 held: the words are by the kind of the reasons, with the ceiling's date and the stop of reason 3", () => {
  const unchecked = row({ 0: hold() });
  offeredNone(unchecked);
  assert.match(unchecked.noApproval.why, /stops of this account were not checked/);
  assert.doesNotMatch(unchecked.noApproval.why, /did not count up/);
  assert.match(unchecked.noApproval.why, /goes out by itself at its ceiling, .* UTC; only a stop of reason 3 signed before then stops it\./);
  const missed = row({ 0: hold({ reasons: [{ kind: "missed-stop", text: "a stop missed on the exam: BTC" }] }) });
  assert.match(missed.noApproval.why, /A stop the oracle reached was not carried out/);
  const recount = row({ 0: hold({ reasons: [{ kind: "recount", text: "recount on the stage: result" }] }) });
  assert.match(recount.noApproval.why, /did not count up/);
  const late = row({ 0: hold({ reasons: [{ kind: "stop-late", text: "late" }] }) });
  assert.match(late.noApproval.why, /long after the oracle reached it/);
  assert.equal(holdWarning([{ kind: "recount" }, { kind: "missed-stop" }]), `${HOLD_WORDS["missed-stop"]} ${HOLD_WORDS.recount}`);
  assert.throws(() => approveSelectedCall({ pool: POOL, rows: [unchecked], keccak256 }), /no fresh review that says approve/);
});

test("L-2 the reasons are said by their text, never as [object Object]", () => {
  assert.equal(reasonsText([{ kind: "recount", text: "recount on the stage" }, "plain", { kind: "late" }]), "recount on the stage; plain; late");
  const r = row({ 0: hold() });
  const html = reviewHtml(r);
  assert.match(html, /stops not checked on the stage: x/);
  assert.doesNotMatch(html, /object Object/);
});

test("the page draws Approve only on a clean fresh review, else no button and its words", () => {
  const reads = (reviews) => ({ now: NOW, latest: LATEST, chainId: CHAIN, approver: "0x00000000000000000000000000000000000000b1",
    payouts: [payout()], firstId: 0, menu: [], tiers: [], reasons: new Map(), payoutsPerDay: 1, payRoom: 1, nextPayAt: 0, reviews });
  const draw = (reviews, extra = {}) => queueHtml(operatorModel({ pool: POOL, roles: ["approver"], reads: { ...reads(reviews), ...extra }, keccak256 }));
  const ok = draw({ 0: clean() });
  assert.match(ok, /data-approve="0">Approve</);
  assert.match(ok, /class="so-pick"/);
  assert.doesNotMatch(ok, /data-no-approval/);
  for (const [reviews, words] of [[{}, /No review of payout 0 was loaded/], [{ 0: hold() }, /says hold/],
    [{ 0: clean({ schema: "x" }) }, /cannot be used/], [{ 0: clean({ readAt: { block: 1, time: NOW - 60 } }) }, /too old/]]) {
    const html = draw(reviews);
    assert.doesNotMatch(html, /without a review|data-without|data-approve=/);
    assert.match(html, /data-no-approval="0"/);
    assert.doesNotMatch(html, /class="so-pick"/, "never in a batch");
    assert.match(html, words);
  }
  assert.match(draw({}, { reviewsFailed: "the index could not be reached" }), /id="so-reviews-failed">The payout reviews could not be loaded/);
});

test("the reviews are asked of the index's route by a checked pool and whole numbers, and checked on the way in", async () => {
  assert.equal(reviewsUrl("https://api.example/", POOL, [3, 4, 3]), `https://api.example/v1/simindex/reviews?pool=${POOL}&ids=3,4`);
  assert.throws(() => reviewsUrl("http://api.example", POOL, [1]), /https/);
  assert.throws(() => reviewsUrl("https://api.example", "../x", [1]), /not a pool/);
  assert.throws(() => reviewsUrl("https://api.example", POOL, [-1]), /whole number/);
  const answer = (status, body) => async () => ({ status, ok: status < 300, json: async () => body });
  const got = await askReviews({ base: "https://api.example", pool: POOL, ids: [0, 1, 2], chainId: CHAIN,
    fetch: answer(200, { status: "ok", chain_id: CHAIN, pool: POOL, reviews: { 0: clean() }, missing: [1], unreadable: { 2: "not JSON" } }) });
  assert.deepEqual(Object.keys(got.docs), ["0"]);
  assert.match(got.notes[1], /written none/);
  assert.match(got.notes[2], /could not be read: not JSON/);
  assert.equal(got.failed, null);
  for (const [fetch, why] of [[answer(200, { status: "ok", chain_id: 998, pool: POOL, reviews: {} }), /chain 998/],
    [answer(200, { status: "ok", chain_id: CHAIN, pool: S1, reviews: {} }), /another pool/],
    [answer(404, { code: "reviews_off" }), /no folder of reviews/], [answer(500, null), /HTTP 500/],
    [async () => { throw new TypeError("Failed to fetch"); }, /could not be reached/]]) {
    const bad = await askReviews({ base: "https://api.example", pool: POOL, ids: [0], chainId: CHAIN, fetch });
    assert.match(bad.failed, why);
    assert.deepEqual(bad.docs, {});
    assert.match(bad.notes[0], /could not be loaded/);
  }
});

test("the page's hour is the review's hour, and its rules are card_problem's", () => {
  const src = readFileSync(new URL("../../ops/payout_review.py", import.meta.url), "utf8");
  assert.match(src, new RegExp(`REVIEW_MAX_BLOCKS = ${REVIEW_MAX_BLOCKS.toLocaleString("en").replace(",", "_")}`));
  assert.match(src, new RegExp(`REVIEW_MAX_S = ${REVIEW_MAX_S.toLocaleString("en").replace(",", "_")}`));
  for (const kind of Object.keys(HOLD_WORDS)) assert.match(src, new RegExp(`"${kind}"`), kind);
});

test("M4 a document as the reviews route hands it out (no evidence, a stop's call without its arguments) is read", () => {
  const out = clean({ verdict: "suspicious", action: "stop", evidence: undefined, stopDraft: undefined,
    reasons: [{ kind: "pair", text: "paired with another account" }],
    call: { kind: "stop", to: POOL, method: "stopPayout", signature: "stopPayout(uint256,uint8,address,bytes32[],bytes32[])" } });
  delete out.evidence;
  delete out.stopDraft;
  const got = reviewFor(out, { id: 0, trader: T1, amount: 800_000_000n, dueAt: CLAIM + PAYOUT_DELAY, claimedAt: CLAIM },
    { pool: POOL, keccak256, chainId: CHAIN, latest: LATEST, now: NOW });
  assert.equal(got.problem, null);
  assert.deepEqual([got.review.action, got.review.call, got.review.stopDraft], ["stop", null, null]);
});
