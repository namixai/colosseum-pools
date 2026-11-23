// The owner's two sections of the control panel, "Codes & shares" and "Withdraw & finish" (mockup 02, 17 November): what
// each control may do now and what it will do, in words, before the owner signs. No browser globals and no reads here,
// so node's test runner loads it (app/tests/simowner.test.mjs): the views (simowner-codes.js, simowner-withdraw.js) read
// the pool and hand the answers in. The pool's contract is the only authority: every check below only says early what
// the contract would refuse (`BadShare`, `CodeUsed`, `TooMuch`, `NotReady`, `Locked`, `NotOver`), and the contract
// refuses it all the same if a page lets it through. No money is worked out here: sums are the pool's own reads
// (`withdrawable`, `shareDue`, `sharesDue`, the figures of `codeOf`), shown as they are.
import { esc } from "./ui.js";
import { sourceNote } from "./simindex.js";
import { dollars, isSet, poolRefusal } from "./simlines.js";
import {
  utc, withdrawalOf, WITHDRAW_DELAY, WITHDRAW_WINDOW, MIN_TERM, FEE_BPS, PAYOUT_DELAY, PAYOUT_HARD,
} from "./simpool.js";

/** What the two sections send and read beyond SIM_POOL_ABI and SIM_LINES_ABI, as SimPool declares them. */
export const SIM_OWNER_CALLS_ABI = [
  "function setCode(bytes32 code, address partner, uint16 bps)",
  "function setSplitCode(bytes32 code, address partner, uint16 bps, address author, uint16 authorBps)",
  "function addToPot(uint256 amount)",
  "function requestWithdraw(uint256 amount)",
  "function withdraw()",
  "function cancelWithdraw()",
  "function setApprover(address approver_)",
  "function approver() view returns (address)",
  "function close()",
  "function finish()",
  "function withdrawable() view returns (uint256)",
  "function principal() view returns (uint256)",
  "event AddedToPot(uint256 amount, uint256 principal)",
  "event ApproverSet(address indexed approver)",
  "event Finished(uint256 amount)",
  "error BadShare()",
  "error CodeUsed()",
  "error TooMuch()",
  "error NoRequest()",
  "error NotReady()",
  "error RequestLapsed()",
  "error Locked()",
  "error Done()",
  "error NotOver()",
];

/** The platform's bounds of a partner's share, in bps (SimPoolFactory.MIN_PARTNER_BPS and MAX_PARTNER_BPS): the same for
 *  every pool; the test holds them to the source. */
export const MIN_SHARE_BPS = 4000;
export const MAX_SHARE_BPS = 6000;

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const feeWords = () => `${(Number(FEE_BPS) / 100).toString()}%`;
const days = (seconds) => `${Math.round(Number(seconds) / 86_400)} days`;

/** A share typed as a percent ("0", "40", "52.5") in basis points; null for anything else (at most two decimals). */
export function parsePercent(text) {
  const t = String(text ?? "").trim().replace(/%$/, "").trim();
  const m = /^(\d{1,3})(?:\.(\d{1,2}))?$/.exec(t);
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0") || 0);
}

/** An amount of USDC typed as "300", "300.5" or "$1,200.25", in the token's units (6 decimals); null for anything else. */
export function parseUsdc(text) {
  const t = String(text ?? "").trim().replace(/^\$/, "").replace(/,/g, "");
  const m = /^(\d+)(?:\.(\d{1,6}))?$/.exec(t);
  if (!m) return null;
  return BigInt(m[1]) * 1_000_000n + BigInt((m[2] ?? "").padEnd(6, "0") || "0");
}

const percent = (bps) => `${(bps / 100).toFixed(2).replace(/\.?0+$/, "")}%`;
/** An address shortened to its ends, as the panel shows one. */
export const shortAddress = (a) => (isSet(a) ? `${String(a).slice(0, 6)}…${String(a).slice(-4)}` : "none");
/** A code's hash shortened the same way. */
export const shortHash = (h) => `${String(h).slice(0, 6)}…${String(h).slice(-4)}`;

// ── codes ────────────────────────────────────────────────────────────────────────────────────────────────────────

/** What `codeOf` answered for a code, as a plain object (ethers gives a Result; a test may give either). */
export function codeRead(code, r) {
  const g = (name, i) => r[name] ?? r[i];
  const author = g("author", 4);
  return {
    code, partner: g("partner", 0), bps: Number(g("bps", 1)), used: Boolean(g("used", 2)), authorBps: Number(g("authorBps", 3)),
    author: isSet(author) ? author : null, brought: BigInt(g("brought", 5)),
    partnerGot: BigInt(g("partnerGot", 9)), authorGot: BigInt(g("authorGot", 10)),
  };
}

/**
 * The plan of one form of "Set a code": the call it makes (`setCode`, or `setSplitCode` for a code for two), its
 * arguments, what it will do in words, or what is wrong with it, in words. `existing` is `codeRead` of the typed
 * text's hash (null when not read); `keccak256` is the page's own. The contract's checks, said before the signature:
 * a share of 0 or 40-60 %, an author who is another address than the partner, and, once a sale named the code, the
 * same partner, author and split and a higher share only.
 */
export function codePlan({ text, partner, share, author, authorShare, existing = null, keccak256, hash }) {
  const errors = [];
  const t = String(text ?? "");
  if (!t) errors.push("Write the code's text: the word or phrase your group's traders type or follow a link with.");
  else if (t !== t.trim()) errors.push("A code cannot start or end with a space: the trader's page cuts spaces off the ends, so it would never match.");
  const p = String(partner ?? "").trim();
  if (!ADDRESS.test(p) || !isSet(p)) errors.push("The partner is the group owner's address: 0x and 40 characters.");
  const bps = parsePercent(share);
  if (bps === null) errors.push("Write the share as a percent: 0, or from 40 to 60.");
  else if (bps !== 0 && (bps < MIN_SHARE_BPS || bps > MAX_SHARE_BPS)) {
    errors.push(`A share is 0, or between 40% and 60% of the profit: ${percent(bps)} is outside that.`);
  }
  const a = String(author ?? "").trim();
  const as = String(authorShare ?? "").trim();
  let authorBps = 0;
  let split = false;
  if (a || as) {
    split = true;
    if (!a) errors.push("Name the author, or leave the author's part empty.");
    else if (!ADDRESS.test(a) || !isSet(a)) errors.push("The author's address is 0x and 40 characters.");
    else if (ADDRESS.test(p) && a.toLowerCase() === p.toLowerCase()) errors.push("The author must be another address than the partner: a code for two names two people.");
    const ab = parsePercent(as);
    if (!as) errors.push("Write the author's part, a percent of the code's share (more than 0 and less than 100).");
    else if (ab === null || ab <= 0 || ab >= 10_000) errors.push("The author's part is a percent of the code's share: more than 0 and less than 100.");
    else authorBps = ab;
  }
  const exists = Boolean(existing && isSet(existing.partner));
  const mode = !exists ? "new" : existing.used ? "raise" : "change";
  if (mode === "raise" && !errors.length) {
    const same = p.toLowerCase() === String(existing.partner).toLowerCase()
      && (existing.author ? a.toLowerCase() === String(existing.author).toLowerCase() : !a)
      && authorBps === existing.authorBps;
    if (!same) errors.push("A sale has named this code, so its partner, author and split are fixed. Only its share can be raised, with the same partner, author and split.");
    else if (bps <= existing.bps) errors.push(`A sale has named this code, and its share only rises: it is ${percent(existing.bps)} now, so ${percent(bps)} is not higher.`);
  }
  if (errors.length) return { ok: false, errors, mode, words: "" };
  const who = `${shortAddress(p)}`;
  let words;
  if (mode === "raise") {
    words = `Raises this code's share from ${percent(existing.bps)} to ${percent(bps)}. It counts from your next withdrawal on; what the old share accrued stays its own. A share never falls back.`;
  } else {
    const head = mode === "change" ? "Changes the code (no sale has named it yet)" : "Writes the code into the pool";
    const take = bps === 0 ? `${who} gets no share of the profit` : `${who} gets ${percent(bps)} of the pool's profit on what this code's sales bring`;
    const cut = split ? `, ${shortAddress(a)} gets ${percent(authorBps)} of that share and the partner the rest` : "";
    words = `${head}: ${take}${cut}. Nothing moves now: shares accrue when you withdraw, and are sent by a claim. `
      + "From the first sale on, the partner, author and split are fixed and the share can only rise.";
  }
  const code = hash ?? (keccak256 ? codeHashOf(t, keccak256) : null);
  const call = split ? { fn: "setSplitCode", args: [code, p, bps, a, authorBps] } : { fn: "setCode", args: [code, p, bps] };
  return { ok: true, errors: [], mode, words, call, code };
}

function codeHashOf(text, keccak256) {
  const hex = `0x${Array.from(new TextEncoder().encode(text), (b) => b.toString(16).padStart(2, "0")).join("")}`;
  return keccak256(hex).toLowerCase();
}

/** The contract's refusal of a code or an owner's call, in words; the pool's own words (simlines.poolRefusal) for the
 *  rest; "" for a refusal this page has no words for. */
export function ownerRefusal(name, args = []) {
  switch (name) {
    case "BadShare": return "The pool refused the share: it is 0, or between 40% and 60%, and a code for two names an author who is another address than the partner, with a part of the share more than 0 and less than 100%.";
    case "CodeUsed": return "A sale has named this code: its partner, author and split are fixed. Only a higher share is accepted, with the same partner, author and split.";
    case "TooMuch": return "That is more than the pool lets you take now. Read the amount again: it can be less than a minute ago.";
    case "NoRequest": return "There is no standing request to withdraw.";
    case "NotReady": return "Not yet: a request waits 7 days, and a pool can finish 7 days after it is closed.";
    case "RequestLapsed": return "The request lapsed: it could be carried out for 3 days after its week. Make a new one.";
    case "Locked": return "The pool is in its first 184 days: only the platform can close it before then.";
    case "Done": return "The pool is finished already.";
    case "NotOver": return "The pool cannot finish yet: something is still running, owed or held for a stop that can be lifted.";
    default: return poolRefusal(name, args);
  }
}

/** What a trader opens to buy with a code: the route of the user panel with the code in it. This is text for the
 *  clipboard, handed to the owner to give out; the control panel carries no link to it. `base` is the page's address
 *  up to the hash. */
export function codeLink(pool, text, base = "") {
  return `${base}#/sim/${pool}?code=${encodeURIComponent(text)}`;
}

/** The row of one code on the table: its read, what it has waiting (`shareDue`) and the text, when this browser knows it. */
export function codeRow(read, due, text = "") {
  const d = due ? { toPartner: BigInt(due.toPartner ?? due[0]), toAuthor: BigInt(due.toAuthor ?? due[1]) } : null;
  const sent = read.partnerGot + read.authorGot;
  const waiting = d ? d.toPartner + d.toAuthor : null;
  return { ...read, text, due: d, sent, waiting, accrued: waiting === null ? null : waiting + sent };
}

const dash = '<span class="so-mut">not read</span>';

function rowHtml(r) {
  const author = r.author ? `<span class="mono">${esc(shortAddress(r.author))}</span><small>${esc(percent(r.authorBps))} of the share</small>` : '<span class="so-mut">no author</span>';
  const waiting = r.waiting === null ? dash
    : `<b>${esc(dollars(r.waiting))}</b>${r.author ? `<small>partner ${esc(dollars(r.due.toPartner))} · author ${esc(dollars(r.due.toAuthor))}</small>` : ""}`;
  const accrued = r.accrued === null ? dash : `${esc(dollars(r.accrued))}<small>sent ${esc(dollars(r.sent))}</small>`;
  const edit = r.used ? "Raise share" : "Edit";
  return `<tr data-code="${esc(r.code)}"${r.used ? ' data-used="true"' : ""}>`
    + `<td data-l="Code" class="mono"><span title="${esc(r.code)}">${esc(shortHash(r.code))}</span>${r.text ? `<small class="so-text">${esc(r.text)}</small>` : ""}</td>`
    + `<td data-l="Partner" class="mono">${esc(shortAddress(r.partner))}</td>`
    + `<td data-l="Share">${esc(percent(r.bps))}</td>`
    + `<td data-l="Author">${author}</td>`
    + `<td data-l="Brought">${esc(dollars(r.brought))}</td>`
    + `<td data-l="Accrued">${accrued}</td>`
    + `<td data-l="Waiting">${waiting}</td>`
    + `<td data-l="" class="so-rowact"><button type="button" class="so-btn ghost sm" data-act="edit" data-code="${esc(r.code)}">${edit}</button>`
    + `${r.text ? `<button type="button" class="so-btn ghost sm" data-act="copy" data-code="${esc(r.code)}">Copy link</button>` : ""}</td></tr>`;
}

/** Where the list of codes was read from, in words: the pool lists no codes, they are found in its `CodeSet` events. */
export function scanNote({ from, to, latest, known = 0, source = null, failed = "" }) {
  const lead = source ? `${sourceNote(source)} ` : "";
  const failure = failed ? ` The last blocks asked for could not be read: ${failed}.` : "";
  const typed = known ? `, and ${known} typed or set from this browser` : "";
  if (from > to) return `${lead}No blocks could be read yet${typed}.${failure}`;
  const range = `Codes found in the events of blocks ${from} to ${to}${to < latest ? ` (the chain is at ${latest})` : ""}`;
  // The index read the pool's events from the block it was made in: there is nothing older to load.
  const older = source?.ok ? " That is the pool's first block."
    : from > 0 ? " Older ones are not listed: load earlier blocks, or type a code's text in the form to look it up." : " That is the start of the chain.";
  return `${lead}${range}${typed}.${older}${failure}`;
}

/** The section "Codes & shares": the table of codes, the form, what the partners are owed in all. */
export function codesHtml({ rows, sharesDue, scan, open = false, more = true }) {
  const body = rows.length ? rows.map(rowHtml).join("")
    : '<tr class="so-empty"><td colspan="8">No codes in the blocks read yet. Set one below: a code is what your group\'s traders type, or follow a link with, to buy.</td></tr>';
  return `<section class="sp-box so-codes" id="codes"><div class="sp-bh"><b>Codes &amp; shares</b><span>share 0 or 40–60% · fixed after the first sale, can only rise</span></div>`
    + `<table class="so-t"><thead><tr><th>Code (hash)</th><th>Partner</th><th>Share</th><th>Author</th><th>Brought</th><th>Accrued</th><th>Waiting</th><th></th></tr></thead><tbody>${body}</tbody></table>`
    + `<div class="so-total" data-money="partners"><span>Owed to partners in all (sharesDue)</span><b>${esc(sharesDue === null ? "not read" : dollars(sharesDue))}</b>`
    + `<span class="sp-why">Accrued when you withdraw, sent by anyone's claim, only to each code's partner and author. Not yours and not for payouts.</span></div>`
    + `<div class="so-act"><button type="button" class="so-btn" id="so-new-code">+ Set a new code</button>`
    + `<button type="button" class="so-btn ghost" id="so-more-codes">${more ? "Load earlier blocks" : "Read the newest blocks"}</button>`
    + `<span class="sp-why" id="so-scan">${esc(scan)}</span></div>`
    + codeFormHtml(open)
    + `<p id="so-codes-status" class="sp-status" role="status"></p></section>`;
}

/** The form of a code. Empty: the view fills `#so-check` (what the pool holds under the typed text) and `#so-plan`
 *  (what the call will do, or what is wrong) as the owner types. */
export function codeFormHtml(open = false) {
  return `<form id="so-code-form" class="so-form" autocomplete="off"${open ? "" : " hidden"}>`
    + `<label>Code text<input name="text" type="text" spellcheck="false" placeholder="the word your group's traders type"></label>`
    + `<label>Partner (the group's owner)<input name="partner" type="text" spellcheck="false" placeholder="0x…"></label>`
    + `<label>Share of the profit, %<input name="share" type="text" inputmode="decimal" placeholder="0, or 40 to 60"></label>`
    + `<label>Author, if the code is for two (optional)<input name="author" type="text" spellcheck="false" placeholder="0x…, another address than the partner"></label>`
    + `<label>Author's part of the share, %<input name="authorShare" type="text" inputmode="decimal" placeholder="more than 0, less than 100"></label>`
    + `<p class="sp-why" id="so-check" role="status"></p>`
    + `<p class="so-plan" id="so-plan" role="status"></p>`
    + `<div class="so-row"><button type="submit" class="so-btn" id="so-code-send" disabled>Set the code</button>`
    + `<button type="button" class="so-btn ghost" id="so-code-cancel">Close the form</button></div></form>`;
}

/** What the pool holds under a typed code, in words: nothing, a code no sale has named (can be changed), or one a sale
 *  has named (only a raise). */
export function checkWords(existing, text = "") {
  if (!existing || !isSet(existing.partner)) return `No code "${text}" in this pool yet: setting it writes a new one.`;
  const who = `partner ${shortAddress(existing.partner)}, share ${percent(existing.bps)}${existing.author ? `, author ${shortAddress(existing.author)} with ${percent(existing.authorBps)} of the share` : ""}`;
  return existing.used
    ? `This code is in the pool (${who}) and sales have named it: only a higher share can be set, with the same partner, author and split.`
    : `This code is in the pool (${who}), no sale has named it yet: all of it can still be changed.`;
}

// ── withdrawing, the money in, the approver, closing and finishing ──────────────────────────────────────────────────

/** The standing request to withdraw, as the section shows it, in words with dates ("no sooner than"), and the calls
 *  it offers now: carry it out only while ready, cancel while there is one. `reads` are `requested`, `readyAt`,
 *  `closed`, `closedAt`; `withdrawable` is the pool's own answer for the carrying out (it pays the lesser). */
export function requestState(reads, now, withdrawable = null) {
  const w = withdrawalOf(reads, now);
  if (w === null) return { state: "unread", words: "The request could not be read.", canWithdraw: false, canCancel: false };
  if (w.state === "none") return { state: "none", words: "No request is standing.", canWithdraw: false, canCancel: false };
  const amount = dollars(w.amount);
  const base = { amount: w.amount, readyAt: w.readyAt, lapsesAt: w.lapsesAt, canCancel: true };
  if (w.state === "waiting") {
    return { ...base, state: "waiting", canWithdraw: false,
      words: `Requested ${amount}. It can be carried out no sooner than ${utc(w.readyAt)}, and no later than ${utc(w.lapsesAt)}.` };
  }
  if (w.state === "ready") {
    return { ...base, state: "ready", canWithdraw: true,
      words: `Requested ${amount}. It can be carried out now, until ${utc(w.lapsesAt)}. The pool pays the lesser of ${amount} and what it can spare at that moment${withdrawable === null ? "" : ` (${dollars(withdrawable)} now)`}.` };
  }
  return { ...base, state: "lapsed", canWithdraw: false,
    words: `The request for ${amount} lapsed on ${utc(w.lapsesAt)}: it could be carried out for 3 days after its week. Cancel it and make a new one.` };
}

/** The call of a request to withdraw: the amount typed, checked against `withdrawable()` (the pool answers the same). */
export function requestPlan({ text, withdrawable, standing = null, now }) {
  const amount = parseUsdc(text);
  if (amount === null) return { ok: false, error: "Write an amount in USDC, like 300 or 300.50.", words: "" };
  if (amount === 0n) return { ok: false, error: "Write an amount above 0.", words: "" };
  if (withdrawable !== null && amount > BigInt(withdrawable)) {
    return { ok: false, error: `The pool lets you request at most ${dollars(withdrawable)} now.`, words: "" };
  }
  const replaces = standing && standing.state !== "none" && standing.state !== "unread" ? ` It replaces your standing request of ${dollars(standing.amount)} and the wait starts again.` : "";
  return { ok: true, amount, words: `Asks the pool to release ${dollars(amount)}. Nothing moves now. From ${utc(Number(now) + WITHDRAW_DELAY)} at the earliest, and within ${days(WITHDRAW_WINDOW)} after that, you carry it out: the pool then pays the lesser of this and what it can spare that day, less ${feeWords()} to the platform. The partners' share of the profit in it stays in the pool for them.${replaces}` };
}

/** Putting the owner's own money in: `addToPot`, after the wallet allows the pool to take it. */
export function addPlan({ text, principal = null }) {
  const amount = parseUsdc(text);
  if (amount === null) return { ok: false, error: "Write an amount in USDC, like 5000 or 5000.50.", words: "" };
  if (amount === 0n) return { ok: false, error: "Write an amount above 0.", words: "" };
  const had = principal === null ? "" : ` You have put in ${dollars(principal)} so far.`;
  return { ok: true, amount, words: `Puts ${dollars(amount)} of your own money into the pool as principal: your wallet first allows the pool to take it (one signature), then the pool takes it (a second). It is never shared with partners. More money free lets more lines sell. Before day 184 only profit can leave; later a withdrawal takes profit first, and the principal comes back with what is above it, or when you finish.${had}` };
}

/** Naming the approver: an address, or the zero address for none. */
export function approverPlan({ text, current }) {
  const t = String(text ?? "").trim();
  if (!ADDRESS.test(t)) return { ok: false, error: "The approver is an address: 0x and 40 characters.", words: "" };
  if (t.toLowerCase() === String(current ?? "").toLowerCase()) return { ok: false, error: "That address is the approver already.", words: "" };
  const none = !isSet(t);
  return { ok: true, address: t, none, words: none
    ? `Removes the approver: every payout then goes out ${days(PAYOUT_HARD)} after its claim.`
    : `Names ${shortAddress(t)} as the pool's approver. A payout it approves goes out ${PAYOUT_DELAY / 3600} hours after its claim; one it does not approve goes out ${days(PAYOUT_HARD)} after. The checker can still stop a payout before it is paid.` };
}

/** The approver now, in words. */
export function approverNow(approver) {
  return isSet(approver) ? `Approver: ${shortAddress(approver)}. A payout it approves goes out ${PAYOUT_DELAY / 3600} hours after its claim.`
    : `No approver named: every payout goes out ${days(PAYOUT_HARD)} after its claim.`;
}

/**
 * Closing the pool to new sales (`close`): the creator may from the end of the term on, and the platform's closer at
 * any time. `reads`: `lockedUntil`, `closed`, `closedAt`, `closedEarly`, `finished`. The control is disabled with the
 * reason and the date until the contract lets it through.
 */
export function closeState(reads, now) {
  const until = Number(reads.lockedUntil);
  const opened = until - MIN_TERM;
  const day184 = `Day 184 is ${utc(until)} (the pool opened on ${utc(opened)}).`;
  if (reads.finished) return { kind: "finished", enabled: false, label: "Pool finished", why: "The pool is finished: nothing more to close.", day184 };
  if (reads.closed) {
    return { kind: "closed", enabled: false, label: "Pool closed",
      why: `Closed on ${utc(reads.closedAt)}${reads.closedEarly ? " by the platform, before its term ended" : ""}. It sells no more; what runs, runs to its end.`, day184 };
  }
  if (Number(now) < until) {
    return { kind: "locked", enabled: false, label: "Close pool · locked until day 184",
      why: `You can close the pool from ${utc(until)}. Before then only the platform can.`, day184 };
  }
  return { kind: "open", enabled: true, label: "Close the pool to new sales",
    why: "The pool's term is over: you can close it now.", day184,
    words: "Stops selling exams for good. Exams and stages already sold run to their end. A week after closing, once nothing runs, nothing is owed and no stop can be lifted, you can finish the pool." };
}

/** Finishing the pool (`finish`): closed, a week after its close, nothing running, owed or held for a stop. */
export function finishState(reads, now) {
  if (reads.finished) return { kind: "finished", enabled: false, label: "Pool finished", why: ["The pool is finished: everything left was sent to you."] };
  if (!reads.closed) return { kind: "open", enabled: false, label: "Finish pool · close it first", why: ["The pool is open. Close it to new sales first; it can finish a week after that."] };
  const why = [];
  const from = Number(reads.closedAt) + WITHDRAW_DELAY;
  if (Number(now) < from) why.push(`A week must pass after the close: no sooner than ${utc(from)}.`);
  if (Number(reads.live) !== 0) why.push(`${reads.live} exam${Number(reads.live) === 1 ? "" : "s"} or stage${Number(reads.live) === 1 ? "" : "s"} still running or able to pay.`);
  if (BigInt(reads.owed) !== 0n) why.push(`${dollars(reads.owed)} of payouts still owed.`);
  if (BigInt(reads.reserved) !== 0n) why.push(`${dollars(reads.reserved)} kept for stops that can still be lifted.`);
  if (why.length) return { kind: "waiting", enabled: false, label: "Finish pool · not yet", why };
  return { kind: "ready", enabled: true, label: "Finish the pool", why: [],
    words: `Ends the pool and sends you everything left in it: your principal first, then the profit, less ${feeWords()} to the platform. What is owed to partners (their waiting shares) and money set aside for others stays in the pool for them. It cannot be undone.` };
}

/** What a withdrawal or a finish did, from the events of its receipt (`name`, `args`): what went to the platform and
 *  what accrued to the partners (`Withdrawn`), and each code's share counted in it (`ShareAccrued`, when the receipt has
 *  any). The rest went to the owner's wallet: no figure is worked out for it. */
export function receiptFacts(events) {
  const w = events.find((e) => e.name === "Withdrawn");
  const accrued = events.filter((e) => e.name === "ShareAccrued").map((e) => ({ code: e.args.code, toPartner: BigInt(e.args.toPartner), toAuthor: BigInt(e.args.toAuthor) }));
  if (!w) return { withdrawn: null, accrued };
  return { withdrawn: { amount: BigInt(w.args.amount), fee: BigInt(w.args.fee), partners: BigInt(w.args.partnerAmount), sent: Boolean(w.args.sent) }, accrued };
}

/** The words after a withdrawal or a finish: how much came out, what the platform took, what accrued to the partners. */
export function afterWithdrawWords(facts, hash) {
  const w = facts.withdrawn;
  if (!w) return `Done (transaction ${hash}).`;
  const to = w.sent ? "The rest went to your wallet." : "The rest is set aside for you: the token refused it. Take it with the pool's claimHeld.";
  const counted = facts.accrued.map((a) => ` Code ${shortHash(a.code)}: partner ${dollars(a.toPartner)}, author ${dollars(a.toAuthor)}.`).join("");
  const part = w.partners === 0n ? "nothing accrued to the partners" : `${dollars(w.partners)} accrued to the partners, which stays in the pool for them to claim`;
  return `Withdrawn ${dollars(w.amount)}: ${dollars(w.fee)} to the platform, and ${part}. ${to}${counted} (Transaction ${hash}.)`;
}

/** The section "Withdraw & finish", the right-hand box of mockup 02. */
export function withdrawHtml({ money, request, approver, close, finish, now }) {
  const wd = money.withdrawable;
  const standing = request.state === "none" || request.state === "unread" ? `<p class="sp-why" data-request="${esc(request.state)}">${esc(request.words)}</p>`
    : `<div class="so-standing" data-request="${esc(request.state)}"><span class="sp-state ${request.state === "ready" ? "on" : request.state === "lapsed" ? "off-short" : "on-idle"}">${esc(request.state === "waiting" ? "Waiting" : request.state === "ready" ? "Ready" : "Lapsed")}</span>`
      + `<p>${esc(request.words)}</p><div class="so-row">`
      + `<button type="button" class="so-btn" id="so-withdraw"${request.canWithdraw ? "" : " disabled"}>Withdraw</button>`
      + `<button type="button" class="so-btn ghost" id="so-cancel"${request.canCancel ? "" : " disabled"}>Cancel request</button></div>`
      + `<p class="so-plan" id="so-withdraw-words">${request.canWithdraw ? esc("Carries out the request: the pool pays you the lesser of the amount and what it can spare now, less " + feeWords() + " to the platform. The partners' share of the profit in it stays in the pool for them. You see their part as soon as it is done.") : esc(request.state === "waiting" ? "Withdrawing opens when the wait is over." : "A lapsed request cannot be carried out: cancel it, then ask again.")}</p></div>`;
  const finishWhy = finish.why.map((w) => `<li>${esc(w)}</li>`).join("");
  return `<section class="sp-box so-wd" id="withdraw"><div class="sp-bh"><b>Withdraw &amp; finish</b></div><div class="so-wdb">`
    + `<form id="so-request-form" autocomplete="off"><label>Request a withdrawal<span class="so-in"><input name="amount" type="text" inputmode="decimal" placeholder="amount, USDC"><button type="submit" class="so-btn" id="so-request" disabled>Request</button></span></label>`
    + `<p class="sp-why" data-money="withdrawable">Can request now: <b>${esc(wd === null ? "not read" : dollars(wd))}</b> (read from withdrawable()). The money leaves no sooner than 7 days after the request, within 3 days after that, and only what the pool can spare then. ${esc(feeWords())} of each withdrawal goes to the platform.</p>`
    + `<p class="so-plan" id="so-request-words" role="status"></p></form>`
    + standing
    + `<p class="sp-why" data-money="principal">Before day 184 only money above the ${esc(money.principal === null ? "not read" : dollars(money.principal))} you put in can leave.</p>`
    + `<form id="so-add-form" autocomplete="off"><label>Add money to the pool<span class="so-in"><input name="amount" type="text" inputmode="decimal" placeholder="amount, USDC"><button type="submit" class="so-btn ghost" id="so-add" disabled>+ Add money</button></span></label>`
    + `<p class="so-plan" id="so-add-words" role="status"></p></form>`
    + `<form id="so-approver-form" autocomplete="off"><label>Approver<span class="so-in"><input name="approver" type="text" spellcheck="false" placeholder="0x… (the zero address for none)"><button type="submit" class="so-btn ghost" id="so-approver" disabled>Set approver</button></span></label>`
    + `<p class="sp-why" data-approver="${esc(approver)}">${esc(approverNow(approver))}</p><p class="so-plan" id="so-approver-words" role="status"></p></form>`
    + `<div class="so-end"><button type="button" class="so-btn so-hatch" id="so-close" data-state="${esc(close.kind)}"${close.enabled ? "" : " disabled"}>${esc(close.label)}</button>`
    + `<p class="sp-why" data-close="${esc(close.kind)}">${esc(close.why)}</p><p class="sp-why">${esc(close.day184)}</p>${close.words ? `<p class="so-plan">${esc(close.words)}</p>` : ""}`
    + `<button type="button" class="so-btn so-hatch" id="so-finish" data-state="${esc(finish.kind)}"${finish.enabled ? "" : " disabled"}>${esc(finish.label)}</button>`
    + `${finishWhy ? `<ul class="so-why" data-finish="${esc(finish.kind)}">${finishWhy}</ul>` : ""}${finish.words ? `<p class="so-plan">${esc(finish.words)}</p>` : ""}</div>`
    + `<p id="so-wd-status" class="sp-status" role="status"></p></div></section>`;
}

/** The two sections side by side, as the mockup lays them out (stacked on a phone, by the stylesheet). */
export const ownerPairHtml = (codes, withdraw) => `<div class="so-two">${codes}${withdraw}</div>`;
