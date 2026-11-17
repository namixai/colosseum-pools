// The partner's and the author's cabinet in a pool's user panel, as mockup 05 draws it: for each code that names the
// connected wallet, its role, its share, what it has accrued and what waits to be sent, what it has received, and the
// button that sends it (`claimShares`, which anyone may send and which pays only the code's partner and author). No
// browser globals and no reads here, so node's test runner loads it (app/tests/simpartner.test.mjs): the view reads the
// pool and hands the answers in; these functions say which codes are the wallet's and what each shows.
//
// The pool has no list of its codes: every one is in a `CodeSet` event (indexed by its partner and its author), and the
// page finds the wallet's codes there. The chain's own `codeOf` and `shareDue` then say what each is and is owed, so a
// code that was reset by a later event, or taken off sale, is what the chain says now. What the page adds is a sum of
// the contract's own figures over the wallet's codes, and the words.
import { esc } from "./ui.js";
import { usd } from "./shared.js";
import { utc } from "./simpool.js";
import { encodeCall } from "./operator.js";

const ZERO = /^0x0{40}$/i;
const BPS = 10_000;
const same = (a, b) => Boolean(a && b) && String(a).toLowerCase() === String(b).toLowerCase();
const isSet = (address) => Boolean(address) && !ZERO.test(String(address));

/** The CodeSet events of `names` the wallet as partner or as author, one entry for each code (the latest event of it
 *  wins), in the order the codes were first set. Events: `{ name, args, block, index }`. A code taken off sale (its
 *  partner zero) is not listed. The chain's `codeOf` has the last word (`cabinetRows`). */
export function codesNamingAccount(events, account) {
  const latest = new Map();
  const ordered = [...events].filter((e) => (e.name ?? e.eventName) === "CodeSet")
    .sort((a, b) => (a.block ?? a.blockNumber) - (b.block ?? b.blockNumber) || (a.index ?? 0) - (b.index ?? 0));
  for (const e of ordered) latest.set(String(e.args.code).toLowerCase(), e);
  const out = [];
  for (const e of latest.values()) {
    if (!isSet(e.args.partner)) continue;
    if (same(e.args.partner, account) || (isSet(e.args.author) && same(e.args.author, account))) out.push(String(e.args.code));
  }
  return out;
}

/** A share in bps as a percent: 4000 → "40%", 2500 → "25%", 3333 → "33.33%". */
export const percent = (bps) => `${(Number(bps) / 100).toFixed(2).replace(/\.?0+$/, "")}%`;

const money = (v) => `$${usd(v, 6)}`;
export const claimHint = "Sends this code's waiting parts: yours to you, the partner's to the partner, the author's to the author. Whoever sends it pays the gas.";

/**
 * Each of the wallet's codes as the cabinet shows it. `codes`: `[{ code, c (codeOf), due (shareDue) }]`. A code whose
 * partner and author are not the wallet, by the chain, is left out. `role`: "partner" or "author". `waiting` and
 * `received` are the wallet's own part, from `shareDue` and from the code's `partnerGot` and `authorGot`.
 */
export function cabinetRows({ account, codes }) {
  const rows = [];
  for (const { code, c, due } of codes) {
    const partner = c.partner ?? c[0];
    const author = c.author ?? c[4];
    const authorBps = Number(c.authorBps ?? c[3]);
    const bps = Number(c.bps ?? c[1]);
    const role = same(partner, account) ? "partner" : isSet(author) && same(author, account) ? "author" : null;
    if (!role) continue;
    const toPartner = BigInt(due.toPartner ?? due[0]);
    const toAuthor = BigInt(due.toAuthor ?? due[1]);
    const waiting = role === "partner" ? toPartner : toAuthor;
    const got = BigInt(role === "partner" ? (c.partnerGot ?? c[9]) : (c.authorGot ?? c[10]));
    const hasAuthor = isSet(author) && authorBps > 0;
    const part = role === "partner" ? BPS - (hasAuthor ? authorBps : 0) : authorBps;
    rows.push({
      code, role, bps, authorBps: hasAuthor ? authorBps : 0, author: hasAuthor ? author : null, partner,
      brought: BigInt(c.brought ?? c[5]), waiting, received: got, hasWaiting: waiting > 0n,
      // The code's whole waiting sum, both parts: what a claim of this code sends.
      sendsAll: toPartner + toAuthor,
      partWords: role === "partner"
        ? (hasAuthor ? `author ${short(author)} gets ${percent(authorBps)} of it, you ${percent(part)}` : "you get all of it")
        : `your part: ${percent(authorBps)} of it`,
    });
  }
  return rows;
}

const short = (a) => `${String(a).slice(0, 6)}…${String(a).slice(-4)}`;
export const codeName = (code) => `${String(code).slice(0, 6)}…${String(code).slice(-2)}`;

/** The four figures over the cabinet: the contract's own amounts of the wallet's codes, added, and how many are of each
 *  role. */
export function totals(rows) {
  return {
    waiting: rows.reduce((s, r) => s + r.waiting, 0n),
    received: rows.reduce((s, r) => s + r.received, 0n),
    codes: rows.length,
    asPartner: rows.filter((r) => r.role === "partner").length,
    asAuthor: rows.filter((r) => r.role === "author").length,
    claimable: rows.filter((r) => r.hasWaiting).map((r) => r.code),
  };
}

/** `claimShares(codes)` for the codes given: the call, and what it will do in words. Nothing to claim is no call. */
export function claimCall({ pool, rows, keccak256 }) {
  const list = rows.filter((r) => r.hasWaiting);
  if (list.length === 0) throw new Error("nothing is waiting for these codes");
  const codes = list.map((r) => r.code);
  return {
    to: pool, method: "claimShares", signature: "claimShares(bytes32[])", args: [codes], signer: "anyone",
    data: encodeCall("claimShares(bytes32[])", [codes], keccak256),
    words: `Sends what ${list.length === 1 ? "this code has" : `these ${list.length} codes have`} accrued: ${list.map((r) => `${codeName(r.code)} ${money(r.waiting)} to you`).join(", ")}`
      + `${list.some((r) => r.sendsAll > r.waiting) ? ", and the other part of the code to whom it belongs" : ""}. The money leaves the pool's reserve for partners; you pay the gas.`,
  };
}

/** The sends to the wallet, from the `SharePaid` events: when, which code, how much, the transaction; a part the token
 *  refused is set aside for the wallet and says so. Newest first. `times` maps a block to its time in seconds. */
export function sendsTo(events, account, times = new Map()) {
  return [...events].filter((e) => (e.name ?? e.eventName) === "SharePaid" && same(e.args.to, account))
    .sort((a, b) => (b.block ?? b.blockNumber) - (a.block ?? a.blockNumber) || (b.index ?? 0) - (a.index ?? 0))
    .map((e) => {
      const block = e.block ?? e.blockNumber;
      return { code: String(e.args.code), amount: BigInt(e.args.amount), sent: Boolean(e.args.sent), block,
        tx: e.tx ?? e.transactionHash, time: times.get(block) ?? null };
    });
}

/** A day as the mockup writes it: "10 Nov 2026". */
export function dayText(seconds) {
  return new Date(Number(seconds) * 1000).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}

export const HOW_SHARES_WORK = Object.freeze([
  "A code is credited only when the pool owner takes money out above what was put in, after the platform's 0.5%. Its part follows the share of sales the code brought.",
  "What is credited is never taken back. It stays in the pool, out of the owner's reach and out of payouts.",
  "Anyone may send it at any time. The money goes only to the code's partner and author.",
  "After the first sale, the partner, the author and the split are fixed. The share can rise, never fall.",
]);

// ── markup (mockup 05; classes prefixed sq-, app/simops.css; the panel's own are sp-) ─────────────────────────────

/** The user panel's own menu item, once the wallet is in a code: the cabinet's route. */
export const partnerHref = (pool) => `#/sim/${pool}/partner`;

export function partnerNoteHtml(account, poolName) {
  return `<div class="sq-note"><span class="dot"></span><span>This section is visible only because <b>${esc(account)}</b> is written in a code of ${esc(poolName)}. Other wallets don't see the Partner tab.</span></div>`;
}

/** What a wallet that is in no code sees at the cabinet's route, and the whole of it. */
export const noCodeHtml = (connected) => `<section class="sp-msg"><h2>Partner cabinet</h2><p>${connected
  ? "Your address is not written in any code of this pool, so there is nothing to show."
  : "Connect the wallet that is written in a code of this pool to open the partner cabinet."}</p></section>`;

function bar(row) {
  const total = row.received + row.waiting;
  const got = total === 0n ? 0 : Number((row.received * 10_000n) / total) / 100;
  return `<div class="sq-bar"><span class="got" style="width:${got}%">received</span><span class="wait" style="width:${100 - got}%">waiting</span></div>`;
}

export function codeCardHtml(r) {
  const role = r.role === "partner" ? "partner" : "author";
  const shareCell = `<div class="sq-cell"><dt>Code's share</dt><dd>${esc(percent(r.bps))}</dd><p>${esc(r.partWords)}</p></div>`;
  const waiting = `<div class="sq-cell hot"><dt>Accrued and waiting</dt><dd>${esc(money(r.waiting))}</dd></div>`;
  const received = `<div class="sq-cell"><dt>Received</dt><dd>${esc(money(r.received))}</dd></div>`;
  const brought = `<div class="sq-cell"><dt>Sales it brought</dt><dd>${esc(money(r.brought))}</dd></div>`;
  const nothing = r.received === 0n && r.waiting === 0n ? `<div class="sq-none">Nothing credited yet</div>` : bar(r);
  const button = r.hasWaiting
    ? `<button type="button" class="sq-claim" data-claim="${esc(r.code)}">Claim ${esc(money(r.waiting))}</button>`
    : `<button type="button" class="sq-claim" data-claim="${esc(r.code)}" disabled>Claim · nothing waiting</button>`;
  return `<section class="sp-box sq-card" data-code="${esc(r.code)}" data-role="${role}"><div class="sp-bh"><b>Code ${esc(codeName(r.code))}</b>`
    + `<span>you are the <i class="sq-role ${role}">${role}</i></span></div>`
    + `<dl class="sq-cells">${shareCell}${brought}${waiting}${received}</dl>${nothing}`
    + `<div class="sq-act">${button}<span class="sq-hint">${esc(claimHint)}</span></div></section>`;
}

export function sendsHtml(sends, scanNote = "") {
  const rows = sends.length
    ? sends.map((s) => `<div class="sq-srow"><span data-l="Date">${s.time == null ? `block ${esc(s.block)}` : esc(dayText(s.time))}</span>`
      + `<span data-l="Code">${esc(codeName(s.code))}</span><span data-l="Sent to you">${esc(money(s.amount))}${s.sent ? "" : " · set aside"}</span>`
      + `<span data-l="Transaction" class="tx">${esc(`${String(s.tx).slice(0, 6)}…${String(s.tx).slice(-2)}`)}</span></div>`).join("")
    : `<p class="so-empty">No send to you in the blocks read.</p>`;
  return `<section class="sp-box" id="sq-sends"><div class="sp-bh"><b>Sent to you</b><span>every send is a transaction on chain</span></div>`
    + `<div class="sq-shead"><span>Date</span><span>Code</span><span>Sent to you</span><span>Transaction</span></div>${rows}`
    + `${scanNote ? `<p class="so-why sq-scan" id="sq-scan">${esc(scanNote)}</p>` : ""}</section>`;
}

export function howHtml() {
  return `<section class="sp-box"><div class="sp-bh"><b>How shares work</b><span>from the pool's terms</span></div>`
    + `<ul class="sq-how">${HOW_SHARES_WORK.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></section>`;
}

/** The page under the header and the note: the four tiles, a card for each code, the sends and how shares work. */
export function partnerHtml({ rows, sends, scanNote, poolName }) {
  const t = totals(rows);
  const claimAll = t.claimable.length > 1
    ? `<div class="sq-all"><button type="button" class="sq-claim" id="sq-claim-all">Claim all · ${esc(money(t.waiting))}</button><span class="sq-hint">One transaction for ${t.claimable.length} codes.</span></div>`
    : "";
  return `<section class="sp-hero"><div class="sp-kick">Partner &amp; author cabinet · ${esc(poolName)}</div>`
    + `<h1>Shares <em>waiting for you</em></h1>`
    + `<p class="sp-lede">When the pool owner takes money out, each code is credited its share. The share stays in the pool, set aside for you, until someone sends it. Press Claim to take it now.</p></section>`
    + `<dl class="so-stats sq-stats"><div class="so-tile hot"><dt>Accrued and waiting, all codes</dt><dd>${esc(money(t.waiting))}</dd></div>`
    + `<div class="so-tile"><dt>Received so far</dt><dd>${esc(money(t.received))}</dd></div>`
    + `<div class="so-tile"><dt>Your codes</dt><dd>${t.codes}</dd><p>${t.asPartner} as partner · ${t.asAuthor} as author</p></div>`
    + `<div class="so-tile"><dt>Next weekly send</dt><dd>Mon–Wed</dd><p>platform keeper, once a week</p></div></dl>`
    + claimAll
    + `<div class="sq-cards">${rows.map(codeCardHtml).join("")}</div>`
    + `<div class="sq-two">${sendsHtml(sends, scanNote)}${howHtml()}</div>`
    + `<p id="sim-status" class="sp-status" role="status"></p>`;
}
