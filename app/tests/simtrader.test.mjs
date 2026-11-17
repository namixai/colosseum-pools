// node --test app/tests/simtrader.test.mjs
//
// The trader's lists of a pool of exams (app/lib/simtrader.js, views/simtrader.js): what the wallet's exams and stages
// are and may do, the payouts the pool holds for it by state, how far the chain was looked through, and the words and
// sums the lists may carry. On made-up reads shaped as SimPool and PaperExam answer them (fixtures/simlines.mjs); no
// browser, no node.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ACCOUNT_ABI, SIM_TRADER_ABI, KIND, SCAN_BLOCKS, scanPlan, canLookBack, scanNote, candidatesOf, span, accountState,
  accountRows, lineInfo, tradeTarget, payoutLine, payoutRows, requestable, traderRefusal, remembered, remember, examsHtml,
  payoutsHtml, noteHtml,
} from "../lib/simtrader.js";
import { panelOf } from "../lib/simpanel.js";
import { menu } from "./fixtures/simlines.mjs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const ROOT = (p) => read(`../../${p}`);
const noComments = (s) => s.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
const U = 1_000_000n;
const DAY = 86_400;
const NOW = 1_800_000_000;
const WALLET = "0x00000000000000000000000000000000000000a1";
const OTHER = "0x00000000000000000000000000000000000000b2";
const EXAM = "0x00000000000000000000000000000000000000e1";
const EXAM2 = "0x00000000000000000000000000000000000000e2";
const STAGE = "0x00000000000000000000000000000000000000f1";
const FOREIGN = "0x00000000000000000000000000000000000000f9";
const FUND_WINDOW = 3 * DAY;

const { menu: m, tiers } = menu();
const lines = lineInfo(m, tiers);
const sold = (exam, trader, block, line = 1) => ({ name: "ExamSold", block, index: 0, args: { exam, trader, line, size: lines[line].size, price: 30n * U, fee: 0n, bySignature: false, code: "0x" + "11".repeat(32), partner: OTHER } });
const funded = (stage, exam, trader, block) => ({ name: "Funded", block, index: 0, args: { stage, exam, trader } });
const acct = (o) => ({ kind: KIND.exam, line: 1, done: false, status: 1, trader: WALLET, passedAt: 0, ...o });

// ── the contract's own words ─────────────────────────────────────────────────────────────────────────────────

test("every call, getter and refusal the trader's lists use is the contract's own", () => {
  const pool = ROOT("src/sim/SimPool.sol");
  for (const l of SIM_TRADER_ABI) {
    const [, kind, name] = l.match(/^(function|error) (\w+)/);
    assert.match(pool, new RegExp(kind === "function" ? `function ${name}\\(` : `error ${name}\\(`), `${l} is not the contract's`);
  }
  const exam = ROOT("src/exam/PaperExam.sol");
  assert.match(exam, /address public pool;/);
  assert.match(exam, /function result\(\) external view returns \(Status, address, uint64, uint64\)/);
  assert.equal(ACCOUNT_ABI.length, 2);
  assert.match(pool, /function claimPayout\(address stage\) external returns \(uint256 id\)/);
  assert.match(pool, /function fund\(address exam\) external returns \(address stage\)/);
  // the kinds are SimPool.Kind's order
  assert.match(pool, /enum Kind \{\s*None,\s*Exam,\s*Stage\s*\}/);
  assert.deepEqual(KIND, { none: 0, exam: 1, stage: 2 });
});

// ── how far the chain was looked through ────────────────────────────────────────────────────────────────────

test("the newest stretch is looked through first, then the ones before it, down to the pool's first block and no further", () => {
  assert.equal(SCAN_BLOCKS, 1000);
  assert.deepEqual(scanPlan({ latest: 50_000, floor: 100, loaded: null }), { from: 49_001, to: 50_000 });
  assert.deepEqual(scanPlan({ latest: 50_000, floor: 100, loaded: { from: 49_001, top: 50_000 } }), { from: 48_001, to: 49_000 });
  assert.deepEqual(scanPlan({ latest: 50_000, floor: 48_500, loaded: { from: 49_001, top: 50_000 } }), { from: 48_500, to: 49_000 }, "not before the floor");
  assert.equal(scanPlan({ latest: 50_000, floor: 48_500, loaded: { from: 48_500, top: 50_000 } }), null);
  assert.deepEqual(scanPlan({ latest: 600, floor: 0, loaded: null }), { from: 0, to: 600 }, "a young chain starts at block 0");
});

test("looking further back is open until the floor is reached, and the note says only what was looked through", () => {
  assert.equal(canLookBack(null, 100), true);
  assert.equal(canLookBack({ from: 101, top: 200 }, 100), true);
  assert.equal(canLookBack({ from: 100, top: 200 }, 100), false);
  assert.equal(scanNote(null), "");
  assert.match(scanNote({ from: 49_001, top: 50_000 }, 100), /Looked through blocks 49001 to 50000\. An exam bought earlier is not listed/);
  assert.match(scanNote({ from: 100, top: 50_000 }, 100), /Looked through every block of the pool, from block 100 to block 50000\./);
});

// ── the wallet's accounts ───────────────────────────────────────────────────────────────────────────────────

test("candidates are the wallet's own exams and stages from the events, oldest first, with the addresses the browser kept after them", () => {
  const events = [sold(EXAM2, OTHER, 12), funded(STAGE, EXAM, WALLET, 30), sold(EXAM, WALLET, 10)];
  const list = candidatesOf(events, WALLET, [FOREIGN, EXAM, EXAM.toUpperCase().replace("0X", "0x")]);
  assert.deepEqual(list.map((c) => [c.address, c.kind, c.block]), [[EXAM, "exam", 10], [STAGE, "stage", 30], [FOREIGN.toLowerCase(), null, null]]);
  assert.equal(list[1].exam, EXAM);
  assert.deepEqual(candidatesOf([], WALLET, []), []);
});

test("an exam is running, passed (its stage to start within the window, then too late), not passed or expired; a stage is requested once", () => {
  const at = { now: NOW, fundWindow: FUND_WINDOW };
  assert.deepEqual(accountState(acct({ status: 1 }), at), { key: "running", label: "Running", note: "", action: null });
  assert.equal(accountState(acct({ status: 3 }), at).label, "Not passed");
  assert.equal(accountState(acct({ status: 4 }), at).label, "Expired");
  const fresh = accountState(acct({ status: 2, passedAt: NOW - DAY }), at);
  assert.deepEqual([fresh.label, fresh.action], ["Passed", "fund"]);
  assert.match(fresh.note, /within 3 days of passing/);
  const late = accountState(acct({ status: 2, passedAt: NOW - 4 * DAY }), at);
  assert.deepEqual([late.key, late.action], ["lapsed", null]);
  assert.equal(accountState(acct({ status: 2, passedAt: NOW - DAY, done: true }), at).action, null);
  const stage = (o) => accountState(acct({ kind: KIND.stage, status: 2, ...o }), at);
  assert.equal(stage({}).action, "claim");
  assert.equal(stage({ done: true }).action, null, "a requested payout is not requested twice");
  assert.match(stage({ done: true }).note, /My payouts/);
});

test("the rows are the wallet's accounts the chain knows, newest first; another wallet's address and a stranger's are not rows", () => {
  const events = [sold(EXAM, WALLET, 10), sold(EXAM2, WALLET, 20, 5), funded(STAGE, EXAM, WALLET, 30)];
  const candidates = candidatesOf(events, WALLET, [FOREIGN]);
  const reads = new Map([
    [EXAM, acct({ line: 1, status: 2, passedAt: NOW - 2 * DAY, done: true })],
    [EXAM2, acct({ line: 5, status: 1 })],
    [STAGE, acct({ kind: KIND.stage, line: 1, status: 2, passedAt: NOW - DAY })],
    [FOREIGN.toLowerCase(), acct({ trader: OTHER })],
  ]);
  const rows = accountRows({ candidates, reads, wallet: WALLET, lines, now: NOW, fundWindow: FUND_WINDOW });
  assert.deepEqual(rows.map((r) => [r.address, r.step]), [[STAGE, "Stage"], [EXAM2, "Exam"], [EXAM, "Exam"]]);
  assert.equal(rows[0].size, 5_000n * U);
  assert.equal(rows[1].size, 10_000n * U);
  assert.equal(rows[1].tier.short, "4% / 10%");
  assert.deepEqual(rows.map((r) => r.href), [`#/exam/${STAGE}`, `#/exam/${EXAM2}`, `#/exam/${EXAM}`]);
  assert.equal(rows[2].state.note, "The stage on the same size was started.", "its Funded event was read");
  assert.equal(rows.find((r) => r.address === FOREIGN), undefined);
  // an address the pool does not know as an account is no row
  const none = new Map([[EXAM, acct({ kind: KIND.none })]]);
  assert.deepEqual(accountRows({ candidates: [{ address: EXAM, kind: "exam", block: 1 }], reads: none, wallet: WALLET, lines, now: NOW, fundWindow: FUND_WINDOW }), []);
});

test("Trade opens the exam that is running, else the newest account; a wallet with none has nowhere to go", () => {
  const rows = [
    { address: STAGE, state: { key: "passed" } }, { address: EXAM2, state: { key: "running" } }, { address: EXAM, state: { key: "running" } },
  ];
  assert.equal(tradeTarget(rows), EXAM2, "the newest of the running ones");
  assert.equal(tradeTarget(rows.filter((r) => r.state.key !== "running")), STAGE);
  assert.equal(tradeTarget([]), null);
});

test("the menu's lines are read as a size and a tier each, and a length that does not agree is an error", () => {
  assert.equal(lines.length, 24);
  assert.equal(lines[0].size, 5_000n * U);
  assert.throws(() => lineInfo(m, tiers.slice(1)), /24 lines and its tiers 23/);
});

// ── the payouts the pool holds ──────────────────────────────────────────────────────────────────────────────

const struct = (o) => ({ account: STAGE, trader: WALLET, amount: 800n * U, dueAt: NOW + DAY, state: 1, wasStopped: false, stoppedAt: 0n, approved: false, ...o });
const one = (o, reason = null) => {
  const [p] = payoutRows({ structs: [struct(o)], wallet: WALLET, now: NOW, reasons: reason === null ? new Map() : new Map([[0, reason]]) });
  return p;
};

test("a payout waits, is approved, is due in the queue, is paid, is stopped (with its reason, in the contract's words), refused, or set aside", () => {
  const waiting = one({});
  assert.deepEqual([waiting.key, waiting.label], ["waiting", "Waiting"]);
  assert.match(waiting.when, /^Not before \d{4}-\d\d-\d\d \d\d:\d\d UTC$/);
  assert.match(waiting.note, /3 days from its request, then for the approver; one not approved goes out by itself 7 days after/);
  const approved = one({ approved: true });
  assert.deepEqual([approved.key, approved.label], ["approved", "Approved"]);
  assert.match(approved.when, /^Not before /);
  const hard = one({ dueAt: NOW - 1 });
  assert.equal(hard.label, "Waiting for approval");
  assert.match(hard.when, /^Not before /);
  const queued = one({ dueAt: NOW - 8 * DAY });
  assert.deepEqual([queued.key, queued.label, queued.when], ["approved", "Due", null]);
  assert.match(queued.note, /in the order of the requests, within the pool's daily limit/);
  assert.equal(one({ state: 3 }).label, "Paid");
  const stopped = one({ state: 2, wasStopped: true, stoppedAt: BigInt(NOW - DAY) }, 3);
  assert.equal(stopped.label, "Stopped");
  assert.match(stopped.note, /The result is disputed: a flaw of the exam, the oracle or the filler/);
  assert.match(stopped.note, /For 14 days after the stop you may object, and the checker may lift it\./);
  assert.match(stopped.when, /^Final not before /);
  assert.match(one({ state: 2, wasStopped: true, stoppedAt: BigInt(NOW - DAY) }).note, /The reason is recorded on chain with the stop\./, "no reason is made up where the event was not read");
  const refused = one({ state: 5, wasStopped: true, stoppedAt: BigInt(NOW - 20 * DAY) }, 1);
  assert.equal(refused.label, "Refused");
  assert.match(refused.note, /Opposite positions with another account of this platform's pools/);
  assert.equal(one({ state: 4 }).label, "Set aside");
});

test("only the wallet's payouts are listed, newest first, each with the sum the pool wrote", () => {
  const structs = [struct({ state: 3 }), struct({ trader: OTHER, amount: 3n * U }), struct({ amount: 400n * U })];
  const rows = payoutRows({ structs, wallet: WALLET, now: NOW });
  assert.deepEqual(rows.map((r) => [r.id, r.amountText]), [[2, "$400"], [0, "$800"]]);
});

test("a time is only ever 'not before': no hour is promised for a payout, and none is read as a time of day", () => {
  const all = [one({}), one({ approved: true }), one({ dueAt: NOW - 1 }), one({ dueAt: NOW - 8 * DAY }), one({ state: 3 }),
    one({ state: 2, stoppedAt: BigInt(NOW - DAY) }), one({ state: 4 })];
  for (const p of all) assert.ok(!/\bat \d|\bby \d|\bon \d|\bwithin \d+ (hour|min)/.test(`${p.when ?? ""} ${p.note}`), `${p.label}: ${p.when} ${p.note}`);
  for (const p of all.filter((x) => x.when)) assert.match(p.when, /^(Not before|Final not before) /);
});

test("a passed stage that has no payout yet may be requested, and it carries no sum", () => {
  const rows = accountRows({
    candidates: [{ address: STAGE, kind: "stage", block: 5 }, { address: EXAM, kind: "exam", block: 4 }],
    reads: new Map([[STAGE, acct({ kind: KIND.stage, status: 2, passedAt: NOW - DAY })], [EXAM, acct({ status: 2, passedAt: NOW - DAY, done: true })]]),
    wallet: WALLET, lines, now: NOW, fundWindow: FUND_WINDOW,
  });
  const ready = requestable(rows);
  assert.deepEqual(ready.map((r) => r.address), [STAGE]);
  const html = payoutsHtml({ ready, rows: [] });
  assert.match(html, new RegExp(`data-claim="${STAGE}"`));
  assert.match(html, /Request payout/);
  assert.match(html, /<div class="sp-c" data-l="Amount">—<\/div>/);
  assert.ok(!/\$800|\$400/.test(html), "the sum is the pool's, written when the payout is requested");
});

test("a refusal of the trader's calls is said in words, and an unknown one is left to the wallet's own", () => {
  for (const name of ["NotTrader", "NotAnExam", "NotAStage", "Done", "NotPassed", "TooLate", "StillRunning"]) assert.ok(traderRefusal(name), name);
  assert.equal(traderRefusal("Anything"), "");
  assert.equal(span(3 * DAY), "3 days");
  assert.equal(span(DAY), "1 day");
  assert.equal(span(7 * 24 * 3600), "7 days");
  assert.equal(span(2 * 3600), "2 hours");
});

// ── what the browser keeps ──────────────────────────────────────────────────────────────────────────────────

test("the browser keeps the addresses of the wallet's accounts, each once, the newest last, and nothing else it is handed", () => {
  assert.deepEqual(remembered(null), []);
  assert.deepEqual(remembered("not json"), []);
  assert.deepEqual(remembered('{"a":1}'), []);
  assert.deepEqual(remembered(JSON.stringify([EXAM, "javascript:alert(1)", 5, STAGE])), [EXAM, STAGE]);
  let raw = null;
  raw = remember(raw, EXAM);
  raw = remember(raw, STAGE);
  raw = remember(raw, EXAM);
  assert.deepEqual(remembered(raw), [STAGE, EXAM]);
  const many = Array.from({ length: 60 }, (_, i) => `0x${String(i).padStart(40, "0")}`);
  assert.equal(remembered(many.reduce((r, a) => remember(r, a), null)).length, 50);
});

// ── the pages ───────────────────────────────────────────────────────────────────────────────────────────────

const sampleRows = () => accountRows({
  candidates: [{ address: EXAM, kind: "exam", block: 1 }, { address: STAGE, kind: "stage", block: 2 }],
  reads: new Map([[EXAM, acct({ status: 2, passedAt: NOW - DAY })], [STAGE, acct({ kind: KIND.stage, status: 2, passedAt: NOW - DAY })]]),
  wallet: WALLET, lines, now: NOW, fundWindow: FUND_WINDOW,
});

test("My exams: a line each with size, tier, step, state and Trade to its screen, the stage's start and the payout's request where the contract allows them", () => {
  const html = examsHtml(sampleRows());
  assert.match(html, /My exams · 2/);
  assert.match(html, new RegExp(`<a class="sp-trade" href="#/exam/${EXAM}">Trade</a>`));
  assert.match(html, new RegExp(`<a class="sp-trade" href="#/exam/${STAGE}">Trade</a>`));
  assert.match(html, new RegExp(`data-fund="${EXAM}">Start the stage`));
  assert.match(html, new RegExp(`data-claim="${STAGE}">Request payout`));
  assert.match(html, /data-l="Size">\$5,000</);
  assert.match(html, /loss 4% · target 10%/);
  assert.match(examsHtml([]), /No exam of this wallet was found in the blocks looked through\./);
  assert.ok(!/\b8%|payout of \$|\$800/.test(html), "no fixed payout sum on the list of exams");
});

test("the lists draw what the chain gave through the escape, and the markup has no hand-written address of a stranger", () => {
  const evil = [{ ...sampleRows()[0], address: `${EXAM}"><script>alert(1)</script>`, href: `#/exam/${EXAM}"><script>` }];
  assert.ok(!/<script>/.test(examsHtml(evil)));
  assert.ok(!/<script>/.test(noteHtml("a<script>", "<b>")));
});

test("neither list nor view calls the product a simulation or a paper exam, shows a fixed payout, or touches the owner's panel", () => {
  const mine = [examsHtml(sampleRows()), payoutsHtml({ ready: requestable(sampleRows()), rows: [one({}), one({ state: 3 })] }),
    noComments(read("../lib/simtrader.js")), noComments(read("../views/simtrader.js"))].join("\n");
  assert.ok(!/simulat|paper|fixed payout|fixed 8/i.test(mine));
  const view = noComments(read("../views/simtrader.js"));
  assert.ok(!/manage|setLine|simadmin|reserved\(|sharesDue|owed/.test(view), "nothing of the owner's panel in the trader's pages");
});

test("the three pages and the trading screen are routes of the user panel; the router sends each to its view", () => {
  const app = read("../app.js");
  for (const tail of ["trade", "exams", "payouts"]) assert.match(app, new RegExp(`#\\\\/sim\\\\/\\(0x\\[0-9a-fA-F\\]\\{40\\}\\)\\\\/${tail}\\$`));
  assert.match(app, /simTradeView\(m\[1\], page\)/);
  assert.match(app, /simExamsView\(m\[1\], page\)/);
  assert.match(app, /simPayoutsView\(m\[1\], page\)/);
  assert.equal(panelOf(`#/sim/${WALLET.replace("a1", "00")}/exams`), "user");
});

test("a page asks for the wallet before it reads anything of the wallet; sending connects first and alone, one call at a time", () => {
  const view = read("../views/simtrader.js");
  for (const fn of ["simTradeView", "simExamsView", "simPayoutsView"]) {
    const body = view.slice(view.indexOf(`export async function ${fn}`));
    assert.ok(body.indexOf("chain.currentAddress()") < body.indexOf("await open("), `${fn} reads the wallet's things only for a wallet`);
  }
  assert.match(view, /if \(!button \|\| button\.disabled \|\| st\.flight\) return;/);
  assert.ok(view.indexOf("await chain.connect()") < view.indexOf("st.flight = true;"), "connecting comes first and alone");
  assert.match(view, /location\.replace\(/, "Trade does not leave itself in the history");
});

test("the purchase keeps the exam it bought for the wallet, and a stage started keeps the stage", () => {
  const pool = read("../views/simpool.js");
  assert.match(pool, /keepFromReceipt\(address, signer\.address, receipt\)/);
  const view = read("../views/simtrader.js");
  assert.match(view, /parsed\?\.name === "ExamSold"\) keepAddress\(pool, wallet, parsed\.args\.exam\)/);
  assert.match(view, /parsed\?\.name === "Funded"\) keepAddress\(pool, wallet, parsed\.args\.stage\)/);
});

test("the stylesheet's braces balance: an unclosed block swallows every rule after it into its media query", () => {
  const css = read("../style.css").replace(/\/\*[\s\S]*?\*\//g, "");
  assert.equal(css.split("{").length, css.split("}").length);
  // each of the two panels' phone rules closes before the trading screen's rules begin
  assert.ok(css.indexOf("main:has(> .ex-page)") > css.lastIndexOf(".sp-c.sz:before"));
  const depth = css.slice(0, css.indexOf("main:has(> .ex-page)")).split("{").length - css.slice(0, css.indexOf("main:has(> .ex-page)")).split("}").length;
  assert.equal(depth, 0, "the trading screen's rules are at the top level, for every width");
});
