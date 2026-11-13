// The operator's page of a simulated pool: the payouts waiting for a decision, the creator's request to withdraw, and
// the pool's state, with the call behind every button. No browser globals, no reads and no keys here, so node's test
// runner loads it (app/tests/operator.test.mjs): the page reads the getters and the events, hands them in, and gives
// the calls these functions build to the wallet that signs them.
//
// Who signs what. A payout's approval is one signature of the pool's approver, a Ledger key: it can only bring a
// payout forward from its ceiling (7 days from the claim) to its 72 hours, never change the amount or the trader.
// The creator's calls (a request to withdraw, the withdrawal, closing after the term) are the creator's Safe: two
// signatures, in the Safe's own app, which takes the call's `to` and `data` from here. A stop, the cancelling of a
// request and an early close are the platform's keys (the factory's checker and closer).
//
// The review of a payout (ops/payout_review.py, schema "payout-review/1") is read from a file the page is given. It
// is advice, never the call: the page builds every call itself, and shows a review only where it is of this very
// payout (`reviewFor`). The names of the approval and of the withdrawal are the contract's (a test holds each line
// of OPERATOR_ABI to src/sim/SimPool.sol); a pool from before them answers none of their reads.

import { termNote, withdrawalOf, PAYOUT_DELAY, PAYOUT_HARD, WITHDRAW_DELAY, WITHDRAW_WINDOW } from "./simpool.js";

// The 72 hours and the ceiling (7 days from the claim, past which a payout is paid unapproved unless it was stopped)
// are the pool page's own constants, held to the factory's source there: one number in the app, not two.
export { PAYOUT_DELAY, PAYOUT_HARD, WITHDRAW_DELAY, WITHDRAW_WINDOW };

/** What the page reads and sends beyond SIM_POOL_ABI (app/lib/simpool.js), as the contracts declare them. */
export const OPERATOR_ABI = [
  "function approver() view returns (address)",
  "function approvePayouts(uint256[] ids)",
  "function stopPayout(uint256 id, uint8 reason, address paired, bytes32[] trades, bytes32[] links)",
  "function principal() view returns (uint256)",
  "function withdrawable() view returns (uint256)",
  "function requested() view returns (uint256)",
  "function readyAt() view returns (uint64)",
  "function closedAt() view returns (uint64)",
  "function requestWithdraw(uint256 amount)",
  "function withdraw()",
  "function cancelWithdraw()",
  "function close()",
  "event ApproverSet(address indexed approver)",
  "event PayoutApproved(uint256 indexed id, address indexed approver)",
  "event WithdrawRequested(uint256 amount, uint64 readyAt, uint64 lapsesAt)",
  "event WithdrawCancelled(address indexed by, uint256 amount)",
];

export const REVIEW_SCHEMA = "payout-review/1";

/** Who signs each call, as the page says it beside the button. */
export const SIGNERS = Object.freeze({
  approver: "the pool's approver (Ledger): one signature",
  creator: "the pool's creator (Safe): two signatures, in the Safe app",
  checker: "the platform's checker key",
  closer: "the platform's closer key",
});

// ── calldata ───────────────────────────────────────────────────────

const ZERO = /^0x0{40}$/i;
const ADDRESS = /^0x[0-9a-f]{40}$/i;
const WORD = /^0x[0-9a-f]{64}$/i;

function hexWord(n) {
  const v = BigInt(n);
  if (v < 0n || v >= 1n << 256n) throw new Error(`${n} is not a uint256`);
  return v.toString(16).padStart(64, "0");
}

function staticWord(type, value) {
  if (/^uint\d+$/.test(type)) return hexWord(value);
  if (type === "bool") return hexWord(value ? 1 : 0);
  if (type === "address") {
    if (!ADDRESS.test(String(value))) throw new Error(`${value} is not an address`);
    return String(value).slice(2).toLowerCase().padStart(64, "0");
  }
  if (type === "bytes32") {
    if (!WORD.test(String(value))) throw new Error(`${value} is not 32 bytes`);
    return String(value).slice(2).toLowerCase();
  }
  throw new Error(`no encoding here for ${type}`);
}

function utf8Hex(text) {
  return `0x${[...new TextEncoder().encode(text)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * A call's data: the selector of `signature` and its arguments, ABI-encoded. Covers what the operator sends:
 * uintN, bool, address, bytes32 and arrays of them. `keccak256` takes 0x-hex and returns 0x-hex (ethers.keccak256).
 */
export function encodeCall(signature, args, keccak256) {
  const m = /^(\w+)\(([^()]*)\)$/.exec(signature);
  if (!m) throw new Error(`not a signature: ${signature}`);
  const types = m[2] ? m[2].split(",") : [];
  if (types.length !== args.length) throw new Error(`${signature} takes ${types.length} arguments, not ${args.length}`);
  const heads = [];
  const tails = [];
  let tailBytes = 32 * types.length;
  types.forEach((type, i) => {
    if (type.endsWith("[]")) {
      const inner = type.slice(0, -2);
      const list = [...args[i]];
      heads.push(hexWord(tailBytes));
      const tail = hexWord(list.length) + list.map((v) => staticWord(inner, v)).join("");
      tails.push(tail);
      tailBytes += tail.length / 2;
    } else {
      heads.push(staticWord(type, args[i]));
    }
  });
  return `${keccak256(utf8Hex(signature)).slice(0, 10)}${heads.join("")}${tails.join("")}`;
}

function call(pool, signature, args, signer, keccak256) {
  if (!ADDRESS.test(String(pool))) throw new Error(`${pool} is not a pool's address`);
  return { to: pool, method: signature.split("(")[0], signature, args, data: encodeCall(signature, args, keccak256),
    signer, signs: SIGNERS[signer] };
}

/** `approvePayouts(ids)`: one signature of the approver lets these payouts through once their 72 hours are over. */
export function approveCall({ pool, ids, keccak256 }) {
  const list = [...ids].map((id) => BigInt(id));
  if (list.length === 0) throw new Error("no payout to approve");
  if (new Set(list.map(String)).size !== list.length) throw new Error("a payout is named twice");
  return call(pool, "approvePayouts(uint256[])", [list], "approver", keccak256);
}

/**
 * `stopPayout`, checked as the pool checks it: reason 1, a pair (another account, at least two trades, no links);
 * reason 2, a pair and the wallets linked (at least one link); reason 3, a result disputed (no other account, at
 * least one trade, and exactly one link: the keccak256 of the published analysis).
 */
export function stopCall({ pool, id, reason, paired, trades, links, keccak256 }) {
  const r = Number(reason);
  const t = [...trades];
  const l = [...links];
  if (r === 1 || r === 2) {
    if (ZERO.test(String(paired))) throw new Error("a pair needs the other account");
    if (t.length < 2) throw new Error("a pair needs both trades");
    if (r === 1 && l.length !== 0) throw new Error("reason 1 has no links; with links it is reason 2");
    if (r === 2 && l.length === 0) throw new Error("reason 2 needs the links between the wallets");
  } else if (r === 3) {
    if (!ZERO.test(String(paired))) throw new Error("a disputed result names no other account");
    if (t.length < 1) throw new Error("a disputed result needs the transaction that shows it");
    if (l.length !== 1) throw new Error("a disputed result needs exactly one link: the hash of the analysis");
  } else {
    throw new Error(`no reason ${reason}`);
  }
  return call(pool, "stopPayout(uint256,uint8,address,bytes32[],bytes32[])", [BigInt(id), r, paired, t, l], "checker",
    keccak256);
}

/** The creator's request to withdraw `amount` (USDC units), carried out a week later at the earliest. */
export function requestWithdrawCall({ pool, amount, withdrawable, keccak256 }) {
  const a = BigInt(amount);
  if (a <= 0n) throw new Error("nothing to withdraw");
  if (withdrawable !== undefined && a > BigInt(withdrawable)) throw new Error("more than the pool lets the creator take now");
  return call(pool, "requestWithdraw(uint256)", [a], "creator", keccak256);
}

export function withdrawCall({ pool, keccak256 }) {
  return call(pool, "withdraw()", [], "creator", keccak256);
}

/** Cancels the standing request; the creator may, and so may the platform's closer. */
export function cancelWithdrawCall({ pool, by = "closer", keccak256 }) {
  if (by !== "closer" && by !== "creator") throw new Error("only the creator or the closer cancels a request");
  return call(pool, "cancelWithdraw()", [], by, keccak256);
}

/** Closes the pool: the closer at any time (before the term that is an early close), the creator from the term on. */
export function closeCall({ pool, by, lockedUntil, now, keccak256 }) {
  if (by === "creator") {
    // Not read is not "the term is over": a missing time would compare as NaN, and NaN is never less.
    if (now == null || lockedUntil == null) throw new Error("the term and the time are needed before the creator closes");
    if (Number(now) < Number(lockedUntil)) throw new Error("the creator closes only from the end of the term");
  }
  if (by !== "creator" && by !== "closer") throw new Error("only the creator or the closer closes a pool");
  return call(pool, "close()", [], by, keccak256);
}

// ── what the page shows ────────────────────────────────────────────

function eventsNamed(events, name) {
  return events.filter((e) => (e.name ?? e.eventName) === name);
}

/**
 * The payouts waiting (Due), each with its three times (the claim, 72 hours after it, the ceiling), whether it is
 * approved, and when it will be paid as things stand. `structs` are `payout(id)` in order of id; `approved` is read
 * from the struct (its eighth field), else from the PayoutApproved events. `approver` is the pool's `approver()`:
 * the zero address means nobody approves, and every payout goes out at its ceiling. `approvable` says whether the
 * pool would take an approval of this payout now: not one approved already, and not where nobody approves. `reviews`
 * maps an id to its review document, shown only where it is of this payout. The most urgent first: the earliest of
 * their payment times.
 */
export function awaitingApproval({ pool, structs, events = [], now, reviews = {}, keccak256, approvals = true, approver }) {
  const nobody = approver != null && ZERO.test(String(approver));
  const approvedEv = new Set(eventsNamed(events, "PayoutApproved").map((e) => Number(e.args.id)));
  const out = [];
  [...structs].forEach((p, index) => {
    // A hole in the list (a payout not read) is skipped, not taken for a payout; its id is its own where given.
    if (!p) return;
    const id = p.id !== undefined ? Number(p.id) : index;
    if (Number(p.state ?? p[4]) !== 1) return;
    const dueAt = Number(p.dueAt ?? p[3]);
    const claimedAt = dueAt - PAYOUT_DELAY;
    // A pool without approvals (it does not answer `approver()`) pays each payout at its 72 hours by itself: no
    // ceiling, nothing to approve (review of #283).
    const hardAt = approvals ? claimedAt + PAYOUT_HARD : null;
    const rawApproved = p.approved ?? p[7];
    const approved = rawApproved === undefined ? approvedEv.has(id) : Boolean(rawApproved);
    const payAt = approved || !approvals ? dueAt : hardAt;
    let status;
    if (!approvals) status = now >= dueAt ? "payable" : "in-72-hours";
    else if (now >= hardAt) status = "past-ceiling";
    else if (approved) status = now >= dueAt ? "approved-payable" : "approved";
    else if (now < dueAt) status = "in-72-hours";
    else status = nobody ? "no-approver" : "awaiting-approval";
    const row = {
      id, account: p.account ?? p[0], trader: p.trader ?? p[1], amount: BigInt(p.amount ?? p[2]),
      claimedAt, dueAt, hardAt, approved, payAt, status, approvable: Boolean(approvals) && !approved && !nobody,
    };
    const doc = reviews[id] ?? reviews[String(id)];
    const got = doc === undefined ? { review: null, problem: "no review" } : reviewFor(doc, row, { pool, keccak256 });
    row.review = got.review;
    row.reviewProblem = got.problem;
    out.push(row);
  });
  return out.sort((a, b) => a.payAt - b.payAt || a.id - b.id);
}

/**
 * A review document, if it is of this payout and its call is the one the page would build: otherwise the problem,
 * and no review. A review read before the claim, of another pool, id, trader or amount, or naming another call, is
 * not shown as if it were this payout's.
 */
export function reviewFor(doc, payout, { pool, keccak256 }) {
  const no = (problem) => ({ review: null, problem });
  if (!doc || doc.schema !== REVIEW_SCHEMA) return no(`not a ${REVIEW_SCHEMA} document`);
  if (String(doc.pool).toLowerCase() !== String(pool).toLowerCase()) return no("a review of another pool");
  if (Number(doc.id) !== Number(payout.id)) return no("a review of another payout");
  if (String(doc.trader).toLowerCase() !== String(payout.trader).toLowerCase()) return no("a review naming another trader");
  if (BigInt(doc.amount) !== BigInt(payout.amount)) return no("a review of another amount");
  if (!doc.readAt || doc.readAt.time == null || !(Number(doc.readAt.time) >= Number(payout.claimedAt))) {
    return no("a review read before the claim");
  }
  const c = doc.call ?? {};
  let built;
  try {
    // Nothing to sign: a pool without approvals or without an approver, a payout approved already, a stop the pool
    // would refuse, or something not read.
    if (c.kind === "none") built = null;
    else if (c.kind === "approve") built = approveCall({ pool, ids: [payout.id], keccak256 });
    else if (c.kind === "stop") {
      const [, reason, paired, trades, links] = c.args;
      built = stopCall({ pool, id: payout.id, reason, paired, trades, links, keccak256 });
    } else return no("a review with no call");
  } catch (err) {
    return no(`a review whose call the pool would refuse: ${err.message}`);
  }
  if (built && c.data && String(c.data).toLowerCase() !== built.data.toLowerCase()) return no("a review whose call data is not its call");
  return { review: { verdict: doc.verdict, reasons: doc.reasons, evidence: doc.evidence, call: built,
    readAt: doc.readAt }, problem: null };
}

/**
 * The creator's standing request to withdraw, if any: how much, when it was made, from when it can be carried out
 * and until when, and the calls a page offers for it now.
 */
export function withdrawRequest({ pool, requested, readyAt, now, keccak256 }) {
  // The pool page's reading of the request (simpool.js), with the calls the operator's page offers for it.
  const w = withdrawalOf({ requested: requested ?? 0n, readyAt: readyAt ?? 0n }, now);
  if (w.state === "none") return null;
  const calls = {};
  if (w.state !== "lapsed") calls.cancel = cancelWithdrawCall({ pool, by: "closer", keccak256 });
  if (w.state === "ready") calls.withdraw = withdrawCall({ pool, keccak256 });
  return { amount: w.amount, requestedAt: w.readyAt - WITHDRAW_DELAY, readyAt: w.readyAt, lapsesAt: w.lapsesAt,
    status: w.state, calls };
}

/**
 * The pool's state as the operator reads it: its USDC and what of it is owed, held back for refused transfers and
 * kept for recent stops; what is free of those; what the creator put in and may take now; how many exams and stages
 * are live; the 180-day term in the library's own words (termNote); when `finish` can come, if closed.
 */
export function poolState(reads = {}, now) {
  const big = (k) => BigInt(reads[k] ?? 0);
  const balance = big("balance");
  const owed = big("owed");
  const held = big("held");
  const reserved = big("reserved");
  const closedAt = Number(reads.closedAt ?? 0);
  return {
    balance, owed, held, reserved,
    free: balance - owed - held - reserved,
    principal: big("principal"),
    withdrawable: big("withdrawable"),
    live: Number(reads.live ?? 0),
    approver: reads.approver ?? null,
    term: termNote(reads, now),
    closed: Boolean(reads.closed),
    finished: Boolean(reads.finished),
    finishFrom: closedAt ? closedAt + WITHDRAW_DELAY : null,
  };
}
