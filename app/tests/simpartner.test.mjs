// node --test app/tests/simpartner.test.mjs
//
// The partner's and author's cabinet (app/lib/simpartner.js), built to mockup 05: which codes are a wallet's (found in the
// `CodeSet` events, then held to the chain's `codeOf`), the role, the share, what waits and what was received for each,
// the Claim button and when it is off, the sends to the wallet, and that the page is the user panel's and carries no word
// Alex struck and no link to the management panel. On a made-up pool's reads.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  codesNamingAccount, cabinetRows, totals, claimCall, sendsTo, dayText, percent, codeName, HOW_SHARES_WORK, partnerHref,
  partnerHtml, partnerNoteHtml, noCodeHtml, codeCardHtml,
} from "../lib/simpartner.js";
import { SIM_POOL_ABI } from "../lib/simpool.js";
import { panelOf } from "../lib/simpanel.js";
import { keccak256 } from "./keccak.mjs";
import { roleIn, confirmedCodes } from "../lib/simpartner.js";
import { sameAddress, ZERO_ADDRESS } from "../lib/ui.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (...p) => readFileSync(join(ROOT, ...p), "utf8");
const POOL = "0x00000000000000000000000000000000000000a1";
const PARTNER = "0x0000000000000000000000000000000000000034";
const AUTHOR = "0x0000000000000000000000000000000000000056";
const OTHER = "0x0000000000000000000000000000000000000077";
const ZERO = "0x0000000000000000000000000000000000000000";
const U = 1_000_000n;
const word = (n) => `0x${n.toString(16).padStart(64, "0")}`;
const CODE_A = word(0x9f21);
const CODE_B = word(0x2ef4);
const CODE_C = word(0x7777);

const set = (code, partner, bps, author = ZERO, authorBps = 0, block = 100, index = 0) => ({ name: "CodeSet", block, index,
  args: { code, partner, bps, author, authorBps } });
const paid = (code, to, amount, sent = true, block = 200, index = 0) => ({ name: "SharePaid", block, index, tx: `0x${block.toString(16).padStart(64, "0")}`,
  args: { code, to, amount, sent } });
const codeOf = (o) => ({ partner: PARTNER, bps: 5000, used: true, authorBps: 3000, author: AUTHOR, brought: 1240n * U, mark: 0n, partnerDue: 0n,
  authorDue: 0n, partnerGot: 210n * U, authorGot: 90n * U, frac: 0n, ...o });
const due = (toPartner, toAuthor) => ({ toPartner, toAuthor });

// ── finding the wallet's codes ─────────────────────────────────────

test("a wallet's codes are the CodeSet events that name it as partner or as author", () => {
  const events = [set(CODE_A, PARTNER, 5000, AUTHOR, 3000), set(CODE_B, OTHER, 4000, PARTNER, 2500, 101), set(CODE_C, OTHER, 6000, ZERO, 0, 102)];
  assert.deepEqual(codesNamingAccount(events, PARTNER), [CODE_A, CODE_B]);
  assert.deepEqual(codesNamingAccount(events, AUTHOR), [CODE_A]);
  assert.deepEqual(codesNamingAccount(events, "0x0000000000000000000000000000000000000099"), []);
  assert.deepEqual(codesNamingAccount([], PARTNER), []);
});

test("a code set again is what its last event says; a code taken off sale is not listed", () => {
  const events = [set(CODE_A, OTHER, 4000, ZERO, 0, 100), set(CODE_A, PARTNER, 5000, ZERO, 0, 150), set(CODE_B, PARTNER, 4000, ZERO, 0, 100),
    set(CODE_B, ZERO, 0, ZERO, 0, 160)];
  assert.deepEqual(codesNamingAccount(events, PARTNER), [CODE_A]);
  assert.deepEqual(codesNamingAccount(events, OTHER), []);
  // events in any order: the later block wins
  assert.deepEqual(codesNamingAccount([...events].reverse(), PARTNER), [CODE_A]);
  // an event of another kind is not a code
  assert.deepEqual(codesNamingAccount([paid(CODE_A, PARTNER, 1n)], PARTNER), []);
});

test("an address in no code gets the one sentence of the cabinet and nothing else", () => {
  assert.deepEqual(cabinetRows({ account: OTHER, codes: [{ code: CODE_A, c: codeOf(), due: due(0n, 0n) }] }), [], "held to the chain's codeOf");
  const connected = noCodeHtml(true);
  assert.match(connected, /not written in any code of this pool/);
  assert.equal((connected.match(/<p>/g) || []).length, 1);
  assert.doesNotMatch(connected, /<button|<table|sq-card/);
  assert.match(noCodeHtml(false), /Connect the wallet/);
});

// ── a code, as partner and as author ───────────────────────────────

test("as the partner: the part of the code's share after the author's, what waits and what was received", () => {
  const [row] = cabinetRows({ account: PARTNER, codes: [{ code: CODE_A, c: codeOf(), due: due(84n * U, 36n * U) }] });
  assert.equal(row.role, "partner");
  assert.equal(row.bps, 5000);
  assert.equal(row.waiting, 84n * U, "the partner's own part of shareDue");
  assert.equal(row.received, 210n * U, "partnerGot");
  assert.equal(row.brought, 1240n * U);
  assert.equal(row.hasWaiting, true);
  assert.equal(row.sendsAll, 120n * U);
  assert.match(row.partWords, /^author 0x0000…0056 gets 30% of it, you 70%$/);
});

test("as the author: the author's part", () => {
  const [row] = cabinetRows({ account: AUTHOR, codes: [{ code: CODE_A, c: codeOf({ authorBps: 2500 }), due: due(84n * U, 0n) }] });
  assert.equal(row.role, "author");
  assert.equal(row.waiting, 0n);
  assert.equal(row.received, 90n * U, "authorGot");
  assert.equal(row.hasWaiting, false);
  assert.equal(row.partWords, "your part: 25% of it");
});

test("a code for one gives the partner all of it", () => {
  const [row] = cabinetRows({ account: PARTNER, codes: [{ code: CODE_A, c: codeOf({ author: ZERO, authorBps: 0, authorGot: 0n }), due: due(10n * U, 0n) }] });
  assert.equal(row.partWords, "you get all of it");
  assert.equal(row.author, null);
});

test("two codes for one wallet: the totals add the contract's own figures, and name the codes there is something to claim", () => {
  const rows = cabinetRows({ account: PARTNER, codes: [
    { code: CODE_A, c: codeOf(), due: due(84n * U, 36n * U) },
    { code: CODE_B, c: codeOf({ partner: OTHER, author: PARTNER, authorBps: 2500, authorGot: 5n * U }), due: due(40n * U, 0n) },
  ] });
  assert.deepEqual(rows.map((r) => r.role), ["partner", "author"]);
  const t = totals(rows);
  assert.equal(t.waiting, 84n * U);
  assert.equal(t.received, 215n * U);
  assert.equal(t.codes, 2);
  assert.equal(t.asPartner, 1);
  assert.equal(t.asAuthor, 1);
  assert.deepEqual(t.claimable, [CODE_A]);
});

// ── the Claim button ───────────────────────────────────────────────

test("Claim is on, with the amount, when something waits for the wallet, and off when nothing does", () => {
  const [waiting] = cabinetRows({ account: PARTNER, codes: [{ code: CODE_A, c: codeOf(), due: due(84n * U, 0n) }] });
  const on = codeCardHtml(waiting);
  assert.match(on, /<button type="button" class="sq-claim" data-claim="0x[0-9a-f]{64}">Claim \$84\.00<\/button>/);
  assert.doesNotMatch(on, /disabled/);
  const [none] = cabinetRows({ account: PARTNER, codes: [{ code: CODE_A, c: codeOf({ partnerGot: 0n, authorGot: 0n }), due: due(0n, 0n) }] });
  const off = codeCardHtml(none);
  assert.match(off, /class="sq-claim"[^>]*disabled>Claim · nothing waiting</);
  assert.match(off, /Nothing credited yet/, "nothing accrued and nothing received");
  // received but nothing waiting: the bar shows the received part
  const [got] = cabinetRows({ account: PARTNER, codes: [{ code: CODE_A, c: codeOf(), due: due(0n, 0n) }] });
  assert.match(codeCardHtml(got), /sq-bar/);
  assert.match(codeCardHtml(got), /disabled>Claim · nothing waiting/);
});

test("the claim is `claimShares` of the wallet's own codes that have something waiting; nothing waiting is no call", () => {
  const rows = cabinetRows({ account: PARTNER, codes: [
    { code: CODE_A, c: codeOf(), due: due(84n * U, 36n * U) },
    { code: CODE_B, c: codeOf({ author: ZERO, authorBps: 0 }), due: due(0n, 0n) },
  ] });
  const call = claimCall({ pool: POOL, rows, keccak256 });
  assert.equal(call.method, "claimShares");
  assert.deepEqual(call.args, [[CODE_A]], "the code with nothing waiting is left out");
  assert.equal(call.signer, "anyone");
  const selector = keccak256(`0x${Buffer.from("claimShares(bytes32[])").toString("hex")}`).slice(0, 10);
  assert.equal(call.data, `${selector}${word(0x20).slice(2)}${word(1).slice(2)}${CODE_A.slice(2)}`);
  assert.match(call.words, /^Sends what this code has accrued: /)
  assert.match(call.words, /\$84\.00 to you/);
  assert.match(call.words, /you pay the gas/);
  assert.throws(() => claimCall({ pool: POOL, rows: [rows[1]], keccak256 }), /nothing is waiting/);
  assert.throws(() => claimCall({ pool: POOL, rows: [], keccak256 }), /nothing is waiting/);
});

test("the claim's calldata is for two codes in one call", () => {
  const rows = cabinetRows({ account: PARTNER, codes: [
    { code: CODE_A, c: codeOf(), due: due(1n * U, 0n) },
    { code: CODE_B, c: codeOf(), due: due(2n * U, 0n) },
  ] });
  const call = claimCall({ pool: POOL, rows, keccak256 });
  assert.deepEqual(call.args[0], [CODE_A, CODE_B]);
  assert.match(call.data, new RegExp(`${word(2).slice(2)}${CODE_A.slice(2)}${CODE_B.slice(2)}$`));
  assert.match(call.words, /^Sends what these 2 codes have accrued/);
});

// ── the history ────────────────────────────────────────────────────

test("the sends to the wallet, newest first, with the time of their block and a set-aside part said so", () => {
  const events = [paid(CODE_A, PARTNER, 90n * U, true, 200), paid(CODE_A, PARTNER, 120n * U, true, 300), paid(CODE_A, AUTHOR, 36n * U, true, 300, 1),
    paid(CODE_B, PARTNER, 5n * U, false, 250), set(CODE_A, PARTNER, 5000)];
  const times = new Map([[200, 1_800_000_000], [300, 1_800_100_000]]);
  const list = sendsTo(events, PARTNER, times);
  assert.deepEqual(list.map((s) => s.block), [300, 250, 200]);
  assert.deepEqual(list.map((s) => s.amount), [120n * U, 5n * U, 90n * U]);
  assert.equal(list[0].time, 1_800_100_000);
  assert.equal(list[1].time, null, "a block whose time was not read says its block");
  assert.equal(list[1].sent, false);
  assert.equal(dayText(1_794_312_000), "10 Nov 2026");
});

// ── the page ───────────────────────────────────────────────────────

const rowsFor = (account) => cabinetRows({ account, codes: [{ code: CODE_A, c: codeOf(), due: due(84n * U, 0n) },
  { code: CODE_B, c: codeOf({ partner: OTHER, author: PARTNER, authorBps: 4000, bps: 4000, partnerGot: 0n, authorGot: 0n }), due: due(0n, 0n) }] });

test("the page: four figures, a card for each code, the sends and how shares work", () => {
  const sends = sendsTo([paid(CODE_A, PARTNER, 120n * U, true, 300)], PARTNER, new Map([[300, 1_794_312_000]]));
  const html = partnerHtml({ rows: rowsFor(PARTNER), sends, scanNote: "All of the pool's events were read.", poolName: "POOL 0xa1…" });
  assert.match(html, /Accrued and waiting, all codes<\/dt><dd>\$84\.00/);
  assert.match(html, /Received so far<\/dt><dd>\$210\.00/);
  assert.match(html, /Your codes<\/dt><dd>2<\/dd><p>1 as partner · 1 as author/);
  assert.match(html, /Next weekly send<\/dt><dd>Mon–Wed/);
  assert.equal((html.match(/class="sp-box sq-card"/g) || []).length, 2);
  assert.match(html, /you are the <i class="sq-role partner">partner/);
  assert.match(html, /you are the <i class="sq-role author">author/);
  assert.match(html, /Sent to you/);
  assert.match(html, /10 Nov 2026/);
  assert.match(html, /How shares work/);
  for (const line of HOW_SHARES_WORK) assert.ok(html.includes(line.replace(/'/g, "&#39;")), line);
  assert.match(html, /All of the pool&#39;s events were read/);
  assert.ok(!/Claim all/.test(html), "one code has something waiting: no claim-all button");
});

test("two codes with something waiting get a button for all of them", () => {
  const rows = cabinetRows({ account: PARTNER, codes: [{ code: CODE_A, c: codeOf(), due: due(84n * U, 0n) }, { code: CODE_B, c: codeOf(), due: due(6n * U, 0n) }] });
  const html = partnerHtml({ rows, sends: [], scanNote: "", poolName: "POOL" });
  assert.match(html, /id="sq-claim-all">Claim all · \$90\.00<\/button>/);
  assert.match(html, /No send to you in the blocks read/);
});

test("the page names the wallet and why it sees the tab; no word Alex struck; no link to the management panel", () => {
  const note = partnerNoteHtml("0x0000…0034", "Pool 0xa1…");
  assert.match(note, /visible only because <b>0x0000…0034<\/b> is written in a code of Pool 0xa1…/);
  const all = `${note}${partnerHtml({ rows: rowsFor(PARTNER), sends: [], scanNote: "", poolName: "POOL" })}${noCodeHtml(true)}`;
  assert.doesNotMatch(all, /simulat|paper/i);
  assert.doesNotMatch(all, /#\/manage|operator|approver/i, "the user panel never links to the management panel");
  assert.doesNotMatch(all, /class="sp-buy"/);
});

test("the cabinet's route is in the user panel's family, and the tab's address is the pool's", () => {
  assert.equal(partnerHref(POOL), `#/sim/${POOL}/partner`);
  assert.equal(panelOf(`#/sim/${POOL}/partner`), "user");
  assert.equal(panelOf(`#/sim/${POOL}`), "user");
  assert.equal(panelOf(`#/sim/${POOL}/exams`), "user", "the trader's own pages are the same panel");
  assert.equal(panelOf(`#/sim/${POOL}/other`), null);
});

test("small things: a share in percent, a code's short name", () => {
  assert.equal(percent(4000), "40%");
  assert.equal(percent(2500), "25%");
  assert.equal(percent(3333), "33.33%");
  assert.equal(percent(6000), "60%");
  assert.equal(codeName(CODE_A), "0x0000…21");
});

// ── the contract and the app ───────────────────────────────────────

test("what the cabinet reads and sends is in the pool's ABI and in the contract", () => {
  const pool = read("src", "sim", "SimPool.sol");
  for (const f of ["codeOf(bytes32 code)", "shareDue(bytes32 code)", "claimShares(bytes32[])"]) {
    assert.ok(SIM_POOL_ABI.some((s) => s.includes(f.split("(")[0] + "(")), f);
  }
  assert.match(pool, /function shareDue\(bytes32 code\) external view returns \(uint256 toPartner, uint256 toAuthor\)/);
  assert.match(pool, /function claimShares\(bytes32\[\] calldata codes\) external/);
  assert.match(pool, /event CodeSet\(bytes32 indexed code, address indexed partner, uint16 bps, address indexed author, uint16 authorBps\)/);
  assert.match(pool, /event SharePaid\(bytes32 indexed code, address indexed to, uint256 amount, bool sent\)/);
  assert.ok(SIM_POOL_ABI.includes("event CodeSet(bytes32 indexed code, address indexed partner, uint16 bps, address indexed author, uint16 authorBps)"));
  assert.ok(SIM_POOL_ABI.includes("event SharePaid(bytes32 indexed code, address indexed to, uint256 amount, bool sent)"));
});

test("the tab is shown by the user panel's shell only to a wallet in a code, and the route is wired", () => {
  const app = read("app", "app.js");
  assert.match(app, /\\\/partner\$\/, \(m, page\) => simPartnerView\(m\[1\], page\)/);
  const shell = read("app", "views", "simshell.js");
  assert.match(shell, /revealPartnerTab\(page, user\[1\]/);
  assert.match(shell, /panelOf\(location\.hash\) === "user"/);
  const codes = read("app", "views", "simcodes.js");
  assert.match(codes, /\(await currentCodes\(at, account, st\)\)\.length > 0/, "no code, no tab");
  assert.match(codes, /catch \{\s*found = false;/, "a failed look adds nothing");
});

test("the cabinet's reads stay inside the node's limits", () => {
  const codes = read("app", "views", "simcodes.js");
  assert.match(codes, /const WINDOW = 1000;/);
  assert.match(codes, /topics: \[st\.topics\]/);
  const view = read("app", "views", "simpartner.js");
  assert.doesNotMatch(view, /window\.ethereum|localStorage/);
  assert.doesNotMatch(view, /page\.onclick/, "no overwriting of the page's other handlers");
  assert.match(view, /page\.__partnerClicks\?\.abort\(\)/, "one click handler, so a redraw never answers twice");
  assert.match(view, /addEventListener\("click"/);
});

// ── review of the first version ────────────────────────────────────

test("the chain has the last word on a code: one set again to another address no longer names the wallet", () => {
  const was = codeOf({ partner: OTHER, author: ZERO, authorBps: 0 });
  assert.equal(roleIn(codeOf(), PARTNER), "partner");
  assert.equal(roleIn(codeOf(), AUTHOR), "author");
  assert.equal(roleIn(was, PARTNER), null, "reset to another partner");
  assert.equal(roleIn(codeOf({ partner: ZERO, author: ZERO }), ZERO), null, "the zero address is nobody's");
  // an old CodeSet names the wallet, the chain's codeOf does not: the code is not the wallet's, and no tab is shown for it
  const found = codesNamingAccount([set(CODE_A, PARTNER, 5000)], PARTNER);
  assert.deepEqual(found, [CODE_A]);
  assert.deepEqual(confirmedCodes([{ code: CODE_A, c: was }], PARTNER), []);
  assert.deepEqual(confirmedCodes([{ code: CODE_A, c: codeOf() }, { code: CODE_B, c: was }], PARTNER), [CODE_A]);
});

test("one answer for the zero address and for 'the same address'", () => {
  assert.equal(ZERO_ADDRESS, "0x0000000000000000000000000000000000000000");
  assert.equal(sameAddress(PARTNER, PARTNER.toUpperCase().replace("0X", "0x")), true);
  assert.equal(sameAddress(PARTNER, AUTHOR), false);
  assert.equal(sameAddress(null, PARTNER), false);
  for (const f of ["simpartner.js", "simoperator.js"]) {
    const src = read("app", "lib", f);
    assert.doesNotMatch(src, /const ZERO(_ADDRESS)? = /, `${f} has no zero address of its own`);
    assert.doesNotMatch(src, /\(a, b\) => Boolean\(a && b\)/, `${f} has no comparison of its own`);
  }
});

test("the cabinet's page keeps a failed read on the page and offers more reading while any block is unread", () => {
  const view = read("app", "views", "simpartner.js");
  assert.match(view, /id="sq-retry">Try again/, "a failed read says so, with a retry, on the current page");
  assert.match(view, /Could not read the pool: /);
  assert.match(view, /const left = finished\(st\) \? "" :/, "the empty cabinet offers more reading until the whole is read");
  assert.match(view, /if \(!finished\(st\) \|\| leftOut > 0\) \$\("#sq-scan", page\)/, "so does the cabinet with codes, and while a send is unchecked");
  assert.match(view, /simPartnerView\(at, page, \{ more: 0, notice \}\)/, "the claim's answer is drawn after the redraw, even a failed one");
  const codes = read("app", "views", "simcodes.js");
  assert.match(codes, /export const finished = \(st\) => complete\(st\) && st\.latest >= st\.head;/);
  assert.doesNotMatch(codes, /known\./, "an unfinished or failed look is not remembered as 'not in a code'");
  assert.match(codes, /pool\.codeOf\(code\)/, "the tab waits for the chain's own answer");
});

test("a claim is one at a time, and its second press is the first press's claim", () => {
  const view = read("app", "views", "simpartner.js");
  assert.match(view, /const pending = createPending\(\);/);
  assert.match(view, /if \(pending\.active\) return;/);
  assert.match(view, /confirm\.press\(call\.data, \(\) => args\)/);
});
