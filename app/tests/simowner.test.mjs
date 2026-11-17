// node --test app/tests/simowner.test.mjs
//
// The owner's two sections of the control panel (app/lib/simowner.js, views/simowner-codes.js, views/simowner-withdraw.js),
// built to the approved mockup 02: a code accepted or refused (the share, the author, a code a sale has named), the
// standing request to withdraw in each state, the buttons that close and finish the pool by their dates, and what each
// call says it will do. On made-up reads shaped as SimPool answers them; no browser and no chain.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SIM_OWNER_CALLS_ABI, MIN_SHARE_BPS, MAX_SHARE_BPS, parsePercent, parseUsdc, codeRead, codeRow, codePlan, ownerRefusal,
  codeLink, checkWords, scanNote, codesHtml, requestState, requestPlan, addPlan, approverPlan, approverNow, closeState,
  finishState, receiptFacts, afterWithdrawWords, withdrawHtml, shortAddress,
} from "../lib/simowner.js";
import { WITHDRAW_DELAY, WITHDRAW_WINDOW, MIN_TERM } from "../lib/simpool.js";
import { MANAGE_NAV } from "../lib/simpanel.js";
import { keccak256 } from "./keccak.mjs";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (...p) => readFileSync(join(ROOT, ...p), "utf8");
const POOL_SOL = read("src", "sim", "SimPool.sol");
const FACTORY_SOL = read("src", "sim", "SimPoolFactory.sol");
const noComments = (s) => s.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
const U = 1_000_000n;
const POOL = "0x0000000000000000000000000000000000009001";
const PARTNER = "0x34aa00000000000000000000000000000000cdcd";
const AUTHOR = "0x56bb00000000000000000000000000000000efef";
const ZERO = "0x0000000000000000000000000000000000000000";
const NOW = 1_800_000_000;
const DAY = 86_400;

const plan = (over = {}) => codePlan({ text: "alpha-group", partner: PARTNER, share: "50", author: "", authorShare: "", keccak256, ...over });
const existing = (over = {}) => codeRead("0x" + "ab".repeat(32), { partner: PARTNER, bps: 5000, used: false, authorBps: 0, author: ZERO, brought: 0n, partnerGot: 0n, authorGot: 0n, ...over });

// ── what the sections send and read is the contract's own ───────────────────────────────────────────────────────

test("every call, getter, event and error of the owner's sections is the pool's own, as the contract declares it", () => {
  for (const l of SIM_OWNER_CALLS_ABI) {
    const [, kind, name] = l.match(/^(function|event|error) (\w+)/);
    const re = kind === "function"
      ? new RegExp(`function ${name}\\(|public\\s+${name}\\s*;`)
      : new RegExp(`${kind} ${name}\\(`);
    assert.match(POOL_SOL, re, `${l} is not the contract's`);
  }
  assert.match(POOL_SOL, /function setCode\(bytes32 code, address partner, uint16 bps\) external onlyCreator/);
  assert.match(POOL_SOL, /function setSplitCode\(bytes32 code, address partner, uint16 bps, address author, uint16 authorBps\)\s+external\s+onlyCreator/);
  assert.match(POOL_SOL, /function addToPot\(uint256 amount\) external onlyCreator/);
  assert.match(POOL_SOL, /function requestWithdraw\(uint256 amount\) external onlyCreator/);
  assert.match(POOL_SOL, /function withdraw\(\) external onlyCreator/);
  assert.match(POOL_SOL, /function setApprover\(address approver_\) external onlyCreator/);
});

test("the platform's bounds of a share and the waits are the factory's", () => {
  assert.match(FACTORY_SOL, new RegExp(`MIN_PARTNER_BPS = ${MIN_SHARE_BPS};`));
  assert.match(FACTORY_SOL, new RegExp(`MAX_PARTNER_BPS = ${MAX_SHARE_BPS};`));
  assert.match(FACTORY_SOL, new RegExp(`WITHDRAW_DELAY = ${WITHDRAW_DELAY / DAY} days;`));
  assert.match(FACTORY_SOL, new RegExp(`WITHDRAW_WINDOW = ${WITHDRAW_WINDOW / DAY} days;`));
  assert.match(FACTORY_SOL, new RegExp(`MIN_TERM = ${MIN_TERM / DAY} days;`));
});

test("the section's two menu entries are built and jump to their boxes", () => {
  assert.deepEqual(MANAGE_NAV.filter((s) => s.key === "codes" || s.key === "withdraw").map((s) => [s.built, s.jump]), [[true, "codes"], [true, "withdraw"]]);
});

// ── typed amounts and shares ────────────────────────────────────────────────────────────────────────────────────

test("a share and an amount are read as typed, and nothing else is taken for one", () => {
  assert.equal(parsePercent("50"), 5000);
  assert.equal(parsePercent("52.5"), 5250);
  assert.equal(parsePercent("40%"), 4000);
  assert.equal(parsePercent("0"), 0);
  for (const bad of ["", "abc", "-5", "5.555", "1e2", "50,5"]) assert.equal(parsePercent(bad), null, bad);
  assert.equal(parseUsdc("300"), 300n * U);
  assert.equal(parseUsdc("300.5"), 300_500_000n);
  assert.equal(parseUsdc("$1,200.25"), 1_200_250_000n);
  assert.equal(parseUsdc("0.000001"), 1n);
  for (const bad of ["", "abc", "-1", "1.1234567", "1e3", "0x10"]) assert.equal(parseUsdc(bad), null, bad);
});

// ── a code accepted or refused ──────────────────────────────────────────────────────────────────────────────────

test("a code for one is accepted: the call, the hash of its text, and what it will do in words", () => {
  const p = plan();
  assert.equal(p.ok, true);
  assert.equal(p.mode, "new");
  assert.equal(p.call.fn, "setCode");
  assert.equal(p.call.args.length, 3);
  assert.equal(p.call.args[0], keccak256("0x" + Buffer.from("alpha-group").toString("hex")));
  assert.deepEqual(p.call.args.slice(1), [PARTNER, 5000]);
  assert.match(p.words, /Writes the code into the pool/);
  assert.match(p.words, /gets 50% of the pool's profit/);
  assert.match(p.words, /Nothing moves now/);
  assert.match(p.words, /fixed and the share can only rise/);
});

test("a code for two is accepted: setSplitCode with the author and the author's part of the share", () => {
  const p = plan({ author: AUTHOR, authorShare: "30" });
  assert.equal(p.ok, true);
  assert.equal(p.call.fn, "setSplitCode");
  assert.deepEqual(p.call.args.slice(1), [PARTNER, 5000, AUTHOR, 3000]);
  assert.match(p.words, /gets 30% of that share and the partner the rest/);
});

test("a share of 0 is accepted; 39 and 61 are refused in words, as are every share outside 0 and 40 to 60", () => {
  assert.equal(plan({ share: "0" }).ok, true);
  assert.match(plan({ share: "0" }).words, /gets no share of the profit/);
  for (const [share, shown] of [["39", "39%"], ["61", "61%"], ["100", "100%"], ["10", "10%"], ["39.99", "39.99%"]]) {
    const p = plan({ share });
    assert.equal(p.ok, false, share);
    assert.match(p.errors.join(" "), /A share is 0, or between 40% and 60% of the profit/);
    assert.ok(p.errors.join(" ").includes(shown), `names ${shown}`);
  }
  for (const share of ["40", "60", "52.5"]) assert.equal(plan({ share }).ok, true, share);
  assert.equal(plan({ share: "" }).ok, false);
  assert.match(plan({ share: "lots" }).errors.join(" "), /Write the share as a percent/);
});

test("an author equal to the partner is refused, in any letter case; so is an author with no part, and a part with no author", () => {
  const same = plan({ author: PARTNER.toUpperCase().replace("0X", "0x"), authorShare: "30" });
  assert.equal(same.ok, false);
  assert.match(same.errors.join(" "), /The author must be another address than the partner/);
  assert.match(plan({ author: AUTHOR, authorShare: "" }).errors.join(" "), /Write the author's part/);
  assert.match(plan({ author: "", authorShare: "30" }).errors.join(" "), /Name the author/);
  for (const part of ["0", "100", "150", "x"]) {
    assert.match(plan({ author: AUTHOR, authorShare: part }).errors.join(" "), /more than 0 and less than 100/, part);
  }
  assert.match(plan({ author: "0x12", authorShare: "30" }).errors.join(" "), /0x and 40 characters/);
});

test("a code needs its text, as it is: no space at either end, which the trader's page would cut off", () => {
  assert.match(plan({ text: "" }).errors.join(" "), /Write the code's text/);
  assert.match(plan({ text: " alpha" }).errors.join(" "), /cannot start or end with a space/);
  assert.match(plan({ text: "alpha " }).errors.join(" "), /cannot start or end with a space/);
  assert.equal(plan({ text: "alpha group" }).ok, true, "a space inside is part of the text");
  assert.match(plan({ partner: "0x12" }).errors.join(" "), /group owner's address/);
  assert.match(plan({ partner: ZERO }).errors.join(" "), /group owner's address/);
});

test("a code no sale has named can be changed whole; one a sale has named takes only a higher share with the same people", () => {
  const free = existing({ used: false });
  const change = plan({ existing: free, partner: AUTHOR, share: "45" });
  assert.equal(change.ok, true);
  assert.equal(change.mode, "change");
  assert.match(change.words, /Changes the code \(no sale has named it yet\)/);

  const used = existing({ used: true, bps: 4000 });
  const raise = plan({ existing: used, share: "50" });
  assert.equal(raise.ok, true);
  assert.equal(raise.mode, "raise");
  assert.match(raise.words, /Raises this code's share from 40% to 50%/);
  assert.match(raise.words, /counts from your next withdrawal/);

  for (const share of ["40", "0"]) {
    const low = plan({ existing: used, share });
    assert.equal(low.ok, false, share);
    assert.match(low.errors.join(" "), /its share only rises: it is 40% now/);
  }
  const other = plan({ existing: used, partner: AUTHOR, share: "50" });
  assert.equal(other.ok, false);
  assert.match(other.errors.join(" "), /partner, author and split are fixed/);
  const addedAuthor = plan({ existing: used, share: "50", author: AUTHOR, authorShare: "30" });
  assert.equal(addedAuthor.ok, false);
  const split = existing({ used: true, bps: 4000, author: AUTHOR, authorBps: 3000 });
  assert.equal(plan({ existing: split, share: "50", author: AUTHOR, authorShare: "30" }).ok, true);
  assert.equal(plan({ existing: split, share: "50", author: AUTHOR, authorShare: "20" }).ok, false);
});

test("what the pool holds under a typed text is said before anything is sent", () => {
  assert.match(checkWords(null, "x"), /No code "x" in this pool yet/);
  assert.match(checkWords(existing({ used: false })), /no sale has named it yet: all of it can still be changed/);
  assert.match(checkWords(existing({ used: true })), /sales have named it: only a higher share/);
  assert.match(checkWords(existing({ used: true, author: AUTHOR, authorBps: 3000 })), /author 0x56bb…efef with 30% of the share/);
});

test("the contract's refusals of a code and of the owner's calls are said in words", () => {
  assert.match(ownerRefusal("BadShare"), /0, or between 40% and 60%.*another address than the partner/);
  assert.match(ownerRefusal("CodeUsed"), /fixed. Only a higher share/);
  assert.match(ownerRefusal("TooMuch"), /more than the pool lets you take/);
  assert.match(ownerRefusal("NotReady"), /7 days/);
  assert.match(ownerRefusal("RequestLapsed"), /3 days/);
  assert.match(ownerRefusal("Locked"), /first 184 days/);
  assert.match(ownerRefusal("NotOver"), /cannot finish yet/);
  assert.match(ownerRefusal("NotCreator"), /Only the pool's creator/, "the pool's own words for the rest");
  assert.equal(ownerRefusal("Nonsense"), "");
});

test("the link with a code is text for the clipboard: the trader's route with the code in it, spaces and all", () => {
  assert.equal(codeLink(POOL, "alpha-group", "https://pools.example/"), `https://pools.example/#/sim/${POOL}?code=alpha-group`);
  const link = codeLink(POOL, "my group & co", "");
  assert.equal(new URLSearchParams(link.split("?")[1]).get("code"), "my group & co");
});

// ── the table of codes ──────────────────────────────────────────────────────────────────────────────────────────

const rowOf = (over = {}, due = [84n * U, 0n], text = "") => codeRow(existing(over), due, text);

test("each code's row: partner, author, share, brought, what it has accrued and what waits (shareDue)", () => {
  const split = rowOf({ used: true, bps: 5000, author: AUTHOR, authorBps: 3000, brought: 1_240n * U, partnerGot: 150n * U, authorGot: 60n * U }, [84n * U, 36n * U], "alpha-group");
  assert.equal(split.waiting, 120n * U);
  assert.equal(split.sent, 210n * U);
  assert.equal(split.accrued, 330n * U);
  const html = codesHtml({ rows: [split], sharesDue: 120n * U, scan: "Codes found." });
  assert.match(html, /<td data-l="Share">50%<\/td>/);
  assert.match(html, /0x56bb…efef<\/span><small>30% of the share/);
  assert.match(html, /<td data-l="Brought">\$1,240<\/td>/);
  assert.match(html, /\$330<small>sent \$210<\/small>/);
  assert.match(html, /<b>\$120<\/b><small>partner \$84 · author \$36<\/small>/);
  assert.match(html, /Owed to partners in all \(sharesDue\)<\/span><b>\$120<\/b>/);
  assert.match(html, /Raise share/);
  assert.match(html, /data-act="copy"/);
});

test("a row without its text has no copy button; one no sale has named says Edit; none read says so", () => {
  const html = codesHtml({ rows: [rowOf({ used: false })], sharesDue: 0n, scan: "x" });
  assert.match(html, />Edit<\/button>/);
  assert.ok(!/data-act="copy"/.test(html), "the pool keeps the hash only: no text, no link");
  assert.match(html, /no author/);
  const unread = codesHtml({ rows: [codeRow(existing(), null)], sharesDue: null, scan: "x" });
  assert.match(unread, /not read/);
  assert.ok(!/\$NaN|undefined|null/.test(unread));
});

test("no codes yet: the table says how a code is made, and the form is closed until asked", () => {
  const html = codesHtml({ rows: [], sharesDue: 0n, scan: "x" });
  assert.match(html, /No codes in the blocks read yet/);
  assert.match(html, /<form id="so-code-form"[^>]* hidden>/);
  assert.match(html, /Set a new code/);
  assert.match(codesHtml({ rows: [], sharesDue: 0n, scan: "x", open: true }), /<form id="so-code-form" class="so-form" autocomplete="off">/);
});

test("the list says where the codes were found: the pool lists none, and older blocks can be read", () => {
  assert.match(scanNote({ from: 1000, to: 2000, latest: 2000, known: 0 }), /blocks 1000 to 2000\. Older ones are not listed/);
  assert.match(scanNote({ from: 1000, to: 1900, latest: 2000, known: 2 }), /the chain is at 2000.*2 typed or set from this browser/);
  assert.match(scanNote({ from: 0, to: 50, latest: 50 }), /start of the chain/);
});

// ── the standing request to withdraw, in each state ─────────────────────────────────────────────────────────────

const standing = (requested, readyAt, extra = {}) => ({ requested, readyAt, closed: false, closedAt: 0, ...extra });

test("no request, a request waiting, ready, lapsed, or not read: what each says and which buttons it allows", () => {
  const none = requestState(standing(0n, 0), NOW);
  assert.deepEqual([none.state, none.canWithdraw, none.canCancel], ["none", false, false]);
  assert.match(none.words, /No request is standing/);

  const waiting = requestState(standing(300n * U, NOW + 3 * DAY), NOW, 500n * U);
  assert.deepEqual([waiting.state, waiting.canWithdraw, waiting.canCancel], ["waiting", false, true]);
  assert.match(waiting.words, /Requested \$300\. It can be carried out no sooner than .*UTC, and no later than .*UTC/);
  assert.match(waiting.words, new RegExp(new Date((NOW + 3 * DAY) * 1000).toISOString().slice(0, 16).replace("T", " ")));

  const ready = requestState(standing(300n * U, NOW - 1 * DAY), NOW, 120n * U);
  assert.deepEqual([ready.state, ready.canWithdraw, ready.canCancel], ["ready", true, true]);
  assert.match(ready.words, /can be carried out now, until .*UTC/);
  assert.match(ready.words, /the lesser of \$300 and what it can spare at that moment \(\$120 now\)/);

  const lapsed = requestState(standing(300n * U, NOW - (WITHDRAW_WINDOW / DAY + 1) * DAY), NOW);
  assert.deepEqual([lapsed.state, lapsed.canWithdraw, lapsed.canCancel], ["lapsed", false, true]);
  assert.match(lapsed.words, /lapsed on .*UTC/);

  const edge = requestState(standing(300n * U, NOW - WITHDRAW_WINDOW), NOW);
  assert.equal(edge.state, "lapsed", "at the last second of the window it has lapsed, as the contract has it");
  assert.equal(requestState(standing(300n * U, NOW), NOW).state, "ready", "from its second, it is ready");

  const unread = requestState({ closed: false }, NOW);
  assert.deepEqual([unread.state, unread.canWithdraw, unread.canCancel], ["unread", false, false]);
});

test("a request: the amount against withdrawable(), in words, and what it replaces", () => {
  const ok = requestPlan({ text: "300", withdrawable: 500n * U, now: NOW });
  assert.equal(ok.ok, true);
  assert.equal(ok.amount, 300n * U);
  assert.match(ok.words, /Asks the pool to release \$300\. Nothing moves now\./);
  assert.ok(ok.words.includes(new Date((NOW + WITHDRAW_DELAY) * 1000).toISOString().slice(0, 16).replace("T", " ")), "from a week on");
  assert.match(ok.words, /within 3 days after that/);
  assert.match(ok.words, /less 0\.5% to the platform/);
  assert.equal(requestPlan({ text: "500", withdrawable: 500n * U, now: NOW }).ok, true, "all of it is not too much");
  const over = requestPlan({ text: "500.01", withdrawable: 500n * U, now: NOW });
  assert.equal(over.ok, false);
  assert.match(over.error, /at most \$500 now/);
  assert.match(requestPlan({ text: "0", withdrawable: 1n, now: NOW }).error, /above 0/);
  assert.match(requestPlan({ text: "abc", withdrawable: 1n, now: NOW }).error, /Write an amount/);
  assert.equal(requestPlan({ text: "5", withdrawable: 0n, now: NOW }).ok, false, "nothing to withdraw: nothing to ask");
  const replace = requestPlan({ text: "100", withdrawable: 500n * U, now: NOW, standing: requestState(standing(300n * U, NOW + DAY), NOW) });
  assert.match(replace.words, /replaces your standing request of \$300 and the wait starts again/);
});

test("adding money and naming the approver: what each will do, before it is signed", () => {
  const add = addPlan({ text: "5000", principal: 1_600n * U });
  assert.equal(add.ok, true);
  assert.match(add.words, /Puts \$5,000 of your own money into the pool as principal/);
  assert.match(add.words, /never shared with partners/);
  assert.match(add.words, /one signature.*second/);
  assert.match(add.words, /put in \$1,600 so far/);
  assert.match(addPlan({ text: "0" }).error, /above 0/);

  const named = approverPlan({ text: AUTHOR, current: ZERO });
  assert.equal(named.ok, true);
  assert.match(named.words, /Names 0x56bb…efef as the pool's approver/);
  assert.match(named.words, /72 hours after its claim/);
  assert.match(named.words, /7 days after/);
  const none = approverPlan({ text: ZERO, current: AUTHOR });
  assert.equal(none.ok, true);
  assert.equal(none.none, true);
  assert.match(none.words, /Removes the approver/);
  assert.match(approverPlan({ text: AUTHOR, current: AUTHOR.toUpperCase().replace("0X", "0x") }).error, /approver already/);
  assert.match(approverPlan({ text: "0x12", current: ZERO }).error, /0x and 40 characters/);
  assert.match(approverNow(AUTHOR), /Approver: 0x56bb…efef/);
  assert.match(approverNow(ZERO), /No approver named/);
});

// ── closing and finishing, by their dates ───────────────────────────────────────────────────────────────────────

const OPENED = NOW - 10 * DAY;
const pool = (over = {}) => ({ lockedUntil: OPENED + MIN_TERM, closed: false, closedAt: 0, closedEarly: false, finished: false, live: 0, owed: 0n, reserved: 0n, ...over });
const utcOf = (s) => new Date(s * 1000).toISOString().slice(0, 16).replace("T", " ");

test("closing is held until day 184, with the date, and opens at the second the term ends", () => {
  const locked = closeState(pool(), NOW);
  assert.equal(locked.enabled, false);
  assert.equal(locked.kind, "locked");
  assert.equal(locked.label, "Close pool · locked until day 184");
  assert.ok(locked.why.includes(utcOf(OPENED + MIN_TERM)), "the date it opens");
  assert.ok(locked.day184.includes(`Day 184 is ${utcOf(OPENED + MIN_TERM)}`));
  assert.ok(locked.day184.includes(`opened on ${utcOf(OPENED)}`), "the opening date is the term's end less 184 days");
  const last = closeState(pool(), OPENED + MIN_TERM - 1);
  assert.equal(last.enabled, false, "the last second of the term");
  const open = closeState(pool(), OPENED + MIN_TERM);
  assert.equal(open.enabled, true);
  assert.equal(open.kind, "open");
  assert.match(open.words, /Stops selling exams for good/);
});

test("a closed pool, one the platform closed early, and a finished one: the close button says so and stays held", () => {
  const closed = closeState(pool({ closed: true, closedAt: NOW - DAY, lockedUntil: OPENED + MIN_TERM }), NOW);
  assert.equal(closed.enabled, false);
  assert.equal(closed.kind, "closed");
  assert.match(closed.why, new RegExp(`Closed on ${utcOf(NOW - DAY)} UTC\\.`));
  const early = closeState(pool({ closed: true, closedEarly: true, closedAt: NOW - DAY }), NOW);
  assert.match(early.why, /by the platform, before its term ended/);
  const done = closeState(pool({ closed: true, finished: true }), NOW);
  assert.equal(done.kind, "finished");
  assert.equal(done.enabled, false);
});

test("finishing: held while the pool is open, a week after the close, and while anything runs, is owed or is kept for a stop", () => {
  const open = finishState(pool(), NOW);
  assert.equal(open.enabled, false);
  assert.equal(open.label, "Finish pool · close it first");
  assert.match(open.why[0], /Close it to new sales first/);

  const closedAt = NOW - 2 * DAY;
  const early = finishState(pool({ closed: true, closedAt }), NOW);
  assert.equal(early.enabled, false);
  assert.equal(early.kind, "waiting");
  assert.ok(early.why.some((w) => w.includes(`no sooner than ${utcOf(closedAt + WITHDRAW_DELAY)}`)));

  const busy = finishState(pool({ closed: true, closedAt: NOW - 8 * DAY, live: 3, owed: 800n * U, reserved: 400n * U }), NOW);
  assert.equal(busy.enabled, false);
  assert.equal(busy.why.length, 3);
  assert.ok(busy.why.some((w) => /3 exams or stages still running/.test(w)));
  assert.ok(busy.why.some((w) => /\$800 of payouts still owed/.test(w)));
  assert.ok(busy.why.some((w) => /\$400 kept for stops that can still be lifted/.test(w)));
  assert.match(finishState(pool({ closed: true, closedAt: NOW - 8 * DAY, live: 1 }), NOW).why[0], /1 exam or stage still running/);

  const week = finishState(pool({ closed: true, closedAt: NOW - WITHDRAW_DELAY }), NOW);
  assert.equal(week.enabled, true, "at the second the week is over");
  assert.equal(finishState(pool({ closed: true, closedAt: NOW - WITHDRAW_DELAY + 1 }), NOW).enabled, false);
  assert.match(week.words, /sends you everything left in it: your principal first, then the profit/);
  assert.match(week.words, /less 0\.5% to the platform/);
  assert.match(week.words, /cannot be undone/);

  const done = finishState(pool({ closed: true, finished: true }), NOW);
  assert.equal(done.kind, "finished");
  assert.equal(done.enabled, false);
});

// ── the section's markup ────────────────────────────────────────────────────────────────────────────────────────

const section = ({ request = standing(0n, 0), reads = pool(), now = NOW, wd = 300n * U, principal = 1_600n * U, approver = ZERO } = {}) =>
  withdrawHtml({ money: { withdrawable: wd, principal }, request: requestState(request, now, wd), approver,
    close: closeState(reads, now), finish: finishState(reads, now), now });
const disabled = (html, id) => new RegExp(`id="${id}"[^>]*disabled`).test(html);

test("withdraw & finish before day 184: what can be requested, the held close button with its reason, no standing request", () => {
  const html = section();
  assert.match(html, /Can request now: <b>\$300<\/b> \(read from withdrawable\(\)\)/);
  assert.match(html, /Before day 184 only money above the \$1,600 you put in can leave/);
  assert.match(html, /Close pool · locked until day 184/);
  assert.ok(disabled(html, "so-close"));
  assert.ok(disabled(html, "so-finish"));
  assert.match(html, /Day 184 is/);
  assert.ok(!/id="so-withdraw"/.test(html) && !/id="so-cancel"/.test(html), "no request, nothing to carry out or cancel");
  assert.ok(disabled(html, "so-request") && disabled(html, "so-add") && disabled(html, "so-approver"), "each waits for its field");
});

test("the request section in each state: Withdraw only while ready, Cancel while there is a request", () => {
  const waiting = section({ request: standing(300n * U, NOW + 2 * DAY) });
  assert.match(waiting, /data-request="waiting"/);
  assert.ok(disabled(waiting, "so-withdraw"));
  assert.ok(!disabled(waiting, "so-cancel"));
  assert.match(waiting, /no sooner than/);
  assert.match(waiting, /Withdrawing opens when the wait is over/);

  const ready = section({ request: standing(300n * U, NOW - DAY) });
  assert.match(ready, /data-request="ready"/);
  assert.ok(!disabled(ready, "so-withdraw"));
  assert.ok(!disabled(ready, "so-cancel"));
  assert.match(ready, /Carries out the request: the pool pays you the lesser/);

  const lapsed = section({ request: standing(300n * U, NOW - 5 * DAY) });
  assert.match(lapsed, /data-request="lapsed"/);
  assert.ok(disabled(lapsed, "so-withdraw"));
  assert.ok(!disabled(lapsed, "so-cancel"));
  assert.match(lapsed, /cancel it, then ask again/);

  const unread = section({ request: { closed: false } });
  assert.match(unread, /data-request="unread"/);
  assert.ok(!/id="so-withdraw"/.test(unread));
});

test("the close and finish buttons by their dates: held, open, and held again once the pool is closed", () => {
  const term = OPENED + MIN_TERM;
  const before = section();
  assert.ok(disabled(before, "so-close") && disabled(before, "so-finish"));
  const after = section({ reads: pool(), now: term });
  assert.ok(!disabled(after, "so-close"));
  assert.ok(disabled(after, "so-finish"));
  assert.match(after, /Stops selling exams for good/);
  const closed = section({ reads: pool({ closed: true, closedAt: term + 100 }), now: term + 200 });
  assert.ok(disabled(closed, "so-close"));
  assert.ok(disabled(closed, "so-finish"));
  assert.match(closed, /A week must pass after the close: no sooner than/);
  const ready = section({ reads: pool({ closed: true, closedAt: term + 100 }), now: term + 100 + WITHDRAW_DELAY });
  assert.ok(disabled(ready, "so-close"));
  assert.ok(!disabled(ready, "so-finish"));
  assert.match(ready, /Ends the pool and sends you everything left in it/);
});

test("the approver now, and a figure not read is said, never shown as a number", () => {
  assert.match(section({ approver: AUTHOR }), /Approver: 0x56bb…efef/);
  assert.match(section(), /No approver named/);
  const html = withdrawHtml({ money: { withdrawable: null, principal: null }, request: requestState({ closed: false }, NOW), approver: ZERO,
    close: closeState(pool(), NOW), finish: finishState(pool(), NOW), now: NOW });
  assert.match(html, /Can request now: <b>not read<\/b>/);
  assert.match(html, /above the not read you put in/);
  assert.ok(!/\$NaN|undefined/.test(html));
});

// ── after a withdrawal ──────────────────────────────────────────────────────────────────────────────────────────

test("after a withdrawal: what went to the platform and what accrued to the partners, from the receipt's events", () => {
  const events = [
    { name: "Withdrawn", args: { to: AUTHOR, amount: 300n * U, fee: 1_500_000n, partnerAmount: 120n * U, sent: true } },
    { name: "ShareAccrued", args: { code: "0x" + "cd".repeat(32), toPartner: 84n * U, toAuthor: 36n * U } },
  ];
  const facts = receiptFacts(events);
  assert.deepEqual([facts.withdrawn.amount, facts.withdrawn.fee, facts.withdrawn.partners, facts.withdrawn.sent], [300n * U, 1_500_000n, 120n * U, true]);
  assert.equal(facts.accrued.length, 1);
  const words = afterWithdrawWords(facts, "0xabc");
  assert.match(words, /Withdrawn \$300: \$1\.50 to the platform, and \$120 accrued to the partners, which stays in the pool for them to claim\./);
  assert.match(words, /The rest went to your wallet\./);
  assert.match(words, /Code 0xcdcd…cdcd: partner \$84, author \$36\./);
  assert.match(words, /0xabc/);
});

test("a withdrawal the token refused is set aside for the owner, and the words say so; a receipt without the event says only Done", () => {
  const refused = afterWithdrawWords(receiptFacts([{ name: "Withdrawn", args: { to: AUTHOR, amount: 10n * U, fee: 50_000n, partnerAmount: 0n, sent: false } }]), "0x1");
  assert.match(refused, /set aside for you: the token refused it/);
  assert.match(refused, /nothing accrued to the partners/);
  assert.equal(afterWithdrawWords(receiptFacts([]), "0x2"), "Done (transaction 0x2).");
});

// ── the panel stays a panel of its own ──────────────────────────────────────────────────────────────────────────

test("the new sections carry no word the owner struck, and no purchase and no link to the trader's pages", () => {
  const files = ["app/lib/simowner.js", "app/views/simowner-codes.js", "app/views/simowner-withdraw.js", "app/views/simowner-run.js"];
  for (const f of files) {
    const src = noComments(read(...f.split("/")));
    assert.ok(!/simulat|paper|fixed payout|fixed 8/i.test(src), `${f} has a struck word`);
    assert.ok(!/\.buy\(|menuHtml|sp-buy|href=/.test(src), `${f} buys or links`);
  }
  const html = [codesHtml({ rows: [rowOf({ used: true }, [1n, 1n], "x")], sharesDue: 1n, scan: "s" }), section()].join("\n");
  assert.ok(!/href=|<a /.test(html), "no link of any kind in the sections");
  assert.ok(!/simulat|paper/i.test(html));
  assert.ok(!/#\/sim\/|\/pool\//.test(html), "the route of the trader's page is text for the clipboard only, built on a press");
});

test("the sections open only to the pool's creator, and a pool of other hands is never asked for its codes or its cash", () => {
  const admin = read("app", "views", "simadmin.js");
  const view = admin.slice(admin.indexOf("export async function simAdminView"));
  assert.ok(view.indexOf('role !== "owner"') < view.indexOf("readCodes(at)"));
  assert.ok(view.indexOf('role !== "owner"') < view.indexOf("readWithdraw(at)"));
  assert.match(admin, /wireCodes\(page, \{ at \}, codes\)/);
  assert.match(admin, /wireWithdraw\(page, \{ at, refresh:/);
  const index = read("app", "index.html");
  assert.match(index, /<link rel="stylesheet" href="simowner\.css">/);
});

test("calls go one at a time through the wallet, and close and finish take a second press", () => {
  const run = read("app", "views", "simowner-run.js");
  assert.match(run, /let busy = false;/);
  assert.match(run, /if \(busy\) return;/);
  assert.match(run, /if \(!chain\.currentSigner\(\)\) await chain\.connect\(\);/);
  const wd = read("app", "views", "simowner-withdraw.js");
  assert.match(wd, /twoPresses\(close,/);
  assert.match(wd, /twoPresses\(finish,/);
  assert.match(wd, /Step 1 of 2: allow the pool to take the money/);
  assert.match(wd, /Step 2 of 2: the pool takes the money/);
});

test("short forms of an address", () => {
  assert.equal(shortAddress(PARTNER), "0x34aa…cdcd");
  assert.equal(shortAddress(ZERO), "none");
});
