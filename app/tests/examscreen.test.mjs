// What the paper-exam trading page says and works out (app/lib/examscreen.js), without a browser: the reads it hands
// the library, the words beside each number, the order form, the scale, the book, the chart's geometry and the
// journal; and what the page's own files must and must not contain (app/views/exam.js, app/app.js).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  WHAT_THIS_IS, ESTIMATE_NOTE, FROM_START, TRAILING_TO_START, REFUSALS, examAccount, refusalText, dataSource,
} from "../lib/exam.js";
import {
  SCREEN_TEXT, readsFrom, accountFigures, positionRow, triggerText, pendingText, barShare,
  accountScale, orderForm, previewView, triggerForm, triggerPreviewView, bookView,
  journalRows, timeLeft, sendFailure, leverageText, usdShown, networkMarks, lifeSteps, timeText, markUnknown, isKnown, singleFlight, journalPlan, journalRange, canLoadEarlier,
  assetFromQuery, queryWithAsset, marketRows, stepIndex, sizeUnitOf, usdcUnits, sizeEntry,
} from "../lib/examscreen.js";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const USD = 1_000_000n;
const DAY = 86_400;
const BTC = 3;
const ETH = 4;
const SOL = 5;
const PX = 600_000n;  // BTC at 5 size decimals: one price decimal, 60 000.0
const PERPS = new Map([[BTC, { index: BTC, name: "BTC", szDecimals: 5 }], [ETH, { index: ETH, name: "ETH", szDecimals: 4 }],
  [SOL, { index: SOL, name: "SOL", szDecimals: 2 }]]);
const AT = { now: DAY, block: 1_000 };

/** What the chain's getters answer, the way ethers hands them (bigint numbers, a tuple as a list with names). */
function got(over = {}) {
  const tuple = (names, values) => Object.assign([...values], Object.fromEntries(names.map((n, i) => [n, values[i]])));
  return {
    status: 1n, step: 0n, cash: 10_000n * USD, dayStartEquity: 10_000n * USD, tradingDays: 0n, deadline: BigInt(30 * DAY), lastFillAt: 0n,
    pending: tuple(["asset", "isBuy", "sz", "placedBlock"], [0n, false, 0n, 0n]),
    filler: "0x00000000000000000000000000000000000000f1", fromBook: true, maxWorseBps: 150n,
    drawdownKind: BigInt(FROM_START), peakSnapshot: 10_000n * USD,
    rules: tuple(["dailyLossBps", "maxDrawdownBps", "maxLeverageX100", "assets", "assetLeverageX100"],
      [300n, 600n, 1000n, [3n, 4n, 5n], [1000n, 1000n, 500n]]),
    terms: tuple(["capital", "targetsBps", "stepDuration", "maxIdle", "minTradingDays", "costPpm", "fillDelay", "fillWindow"],
      [10_000n * USD, [1000n], BigInt(30 * DAY), 0n, 0n, 450n, 1n, 10n]),
    ...over,
  };
}
const flat = () => [{ asset: BTC, size: 0n, oracle: PX }, { asset: ETH, size: 0n, oracle: 30_000n }, { asset: SOL, size: 0n, oracle: 1_500n }];
const long = (extra = {}) => [{ asset: BTC, size: 10_000n, oracle: PX, ...extra }, { asset: ETH, size: 0n, oracle: 30_000n }, { asset: SOL, size: 0n, oracle: 1_500n }];
const reads = (over = {}, per = flat) => readsFrom(got(over), per());

// ── what is read ───────────────────────────────────────────────────

test("an ethers answer (bigint numbers, tuples with names) is handed to the library as the account it counts", () => {
  const r = reads();
  const a = examAccount(r, AT);
  assert.equal(a.status, "active");
  assert.equal(a.priced, true);
  assert.equal(a.capital, 10_000n * USD);
  assert.deepEqual(a.assets, [BTC, ETH, SOL]);
  assert.equal(a.canPlace, true);
  assert.equal(a.lines[0].at, 9_400n * USD, "the line from the start: 6 % under 10 000");
  assert.equal(a.target.at, 11_000n * USD);
});

test("a price that was not read leaves the account unpriced, and the page says so instead of a figure", () => {
  const a = examAccount(reads({}, () => long({ oracle: null })), AT);
  assert.equal(a.priced, false);
  const f = accountFigures(a);
  assert.equal(f.equity, SCREEN_TEXT.noPrice);
  assert.equal(f.leverage, SCREEN_TEXT.noPrice);
  assert.equal(f.target.toGo, SCREEN_TEXT.noPrice);
  assert.ok(f.lines.every((l) => l.room === SCREEN_TEXT.noPrice));
  assert.equal(accountScale(a), null, "no bar without an equity");
  assert.equal(positionRow(a.positions[0], PERPS).mark, SCREEN_TEXT.noPrice);
});

test("the figures are the library's, said with its formatters; the loss line is named by its kind", () => {
  const a = examAccount(reads(), AT);
  const f = accountFigures(a);
  assert.deepEqual([f.size, f.step, f.equity, f.balance, f.leverage], ["$10,000.00", "1 of 1", "$10,000.00", "$10,000.00", "0.00×"]);
  assert.equal(f.lines[0].label, "Total loss limit");
  assert.deepEqual(f.lines.map((l) => l.near), [false, true], "the daily line (9 700) is nearer than the total one (9 400): it is marked");
  assert.equal(f.lines[1].label, "Daily loss limit");
  assert.equal(f.days, null, "no minimum of trading days: nothing to count");
  const trailing = accountFigures(examAccount(reads({ drawdownKind: BigInt(TRAILING_TO_START), peakSnapshot: 10_200n * USD }), AT));
  assert.equal(trailing.lines[0].label, "Loss limit (follows your best day's balance up to the starting balance)");
  assert.equal(trailing.lines[0].at, "$9,600.00", "the highest snapshot less the same allowance");
  const days = accountFigures(examAccount(reads({ terms: Object.assign([...got().terms], { ...got().terms, minTradingDays: 3n }) }), AT));
  assert.equal(days.days, "0 of 3");
});

// ── words ──────────────────────────────────────────────────────────

test("a position's row names the asset, side and size, and says its stop and take as the brief does", () => {
  const stop = { px: 550_000n, sz: 0n, armedAt: 900, seenAt: 0 };
  const take = { px: 700_000n, sz: 4_000n, armedAt: 1_003, seenAt: 0 };
  const a = examAccount(reads({ cash: 4_000n * USD }, () => long({ stop, take })), AT);
  const row = positionRow(a.positions[0], PERPS);
  assert.deepEqual([row.name, row.side, row.size, row.mark, row.notional], ["BTC", "Long", "0.1 BTC", "60000", "$6,000.00"]);
  assert.equal(row.leverage, "0.60× / 10.00×");
  assert.equal(row.stop, "Stop 55000 · closes all · armed");
  assert.equal(row.take, "Take 70000 · closes 0.04 · armed in 3 blocks");
  assert.deepEqual([row.closeIsBuy, row.closeSize], [false, 10_000n], "Close is an order the other way for the whole size");
  const short = examAccount(reads({ cash: 16_000n * USD }, () => [{ asset: BTC, size: -10_000n, oracle: PX }, ...flat().slice(1)]), AT);
  const s = positionRow(short.positions[0], PERPS);
  assert.deepEqual([s.side, s.size, s.closeIsBuy], ["Short", "0.1 BTC", true]);
  assert.equal(triggerText({ kind: "stop", px: 1n, whole: true, blocksToArm: 0, state: "seen", closes: 1n }, 5), "Stop 0.1 · closes all · reached, shown on chain");
});

test("the waiting order is said in the brief's three ways", () => {
  const order = (placedBlock) => got({ pending: Object.assign([BTC, true, 1_500n, placedBlock], { asset: BTC, isBuy: true, sz: 1_500n, placedBlock }) });
  const say = (block) => pendingText(examAccount(readsFrom(order(1_000n), flat()), { now: DAY, block }).pending, PERPS);
  assert.equal(say(1_000).text, "Order sent: Buy 0.015 BTC · fills from block 1001");
  assert.equal(say(1_001).text, "Being filled · window until block 1010");
  assert.equal(say(1_011).text, "Not filled. The order lapsed at no cost — place it again.");
  assert.equal(pendingText(null, PERPS), null);
});

test("a number is placed on the bar from the figures the library gave, kept between its ends", () => {
  assert.equal(barShare(50n, 0n, 100n), 5_000);
  assert.equal(barShare(-5n, 0n, 100n), 0);
  assert.equal(barShare(500n, 0n, 100n), 10_000);
  assert.equal(barShare(1n, 5n, 5n), 0, "a bar of no length");
  const a = examAccount(reads(), AT);
  const scale = accountScale(a);
  assert.deepEqual([scale.equity, scale.target], [3_750, 10_000], "10 000 between the 9 400 line and the 11 000 target");
  assert.equal(scale.lines.find((l) => l.near).kind, "daily-loss");
  assert.equal(scale.broken, false);
  const high = accountScale(examAccount(reads({ cash: 12_000n * USD }), AT));
  assert.equal(high.equity, 10_000, "an equity past the target ends the bar");
  assert.equal(high.target, 6_153, "the target is then short of the end");
});

test("a time left, and a refusal of a sent call in the contract's words", () => {
  assert.equal(timeLeft(DAY * 3 + 4 * 3600, 0), "3 d 4 h");
  assert.equal(timeLeft(5 * 3600 + 12 * 60, 0), "5 h 12 min");
  assert.equal(timeLeft(90, 0), "1 min");
  assert.equal(timeLeft(100, 100), "Over");
  assert.equal(timeLeft(null, 5), "—");
  assert.equal(leverageText(250n), "2.50×");
  const refusalOfData = (d) => (d === "0xaa" ? { text: refusalText("OrderPending") } : null);
  assert.match(sendFailure({ data: "0xaa" }, refusalOfData), /already waiting/);
  assert.equal(sendFailure({ code: "ACTION_REJECTED" }, refusalOfData), "Cancelled in the wallet.");
  assert.equal(sendFailure({ shortMessage: "nope" }, refusalOfData), "nope");
});

// ── the order form ─────────────────────────────────────────────────

const PRICES = { oracle: PX, bid: 599_900n, ask: 600_100n };
const BTCP = PERPS.get(BTC);
const form = (r, sizeInput, extra = {}) => orderForm(r, { asset: BTC, perp: BTCP, isBuy: true, sizeInput, prices: PRICES, ...AT, ...extra });

test("a size is typed as a number and turned into units by the library; what it refuses is said in its words", () => {
  assert.deepEqual(form(reads(), ""), { size: null, error: null, preview: null });
  assert.equal(form(reads(), "0.015").size, 1_500n);
  assert.match(form(reads(), "0.000001").error, /at most 5 decimals/);
  assert.match(form(reads(), "abc").error, /not a plain positive number/);
  assert.match(form(reads(), "0").error, /above zero/);
  assert.equal(form(reads(), "0.015").error, null);
});

test("the estimate shows the price and its source, the cost, the position and the leverage after, and the library's note", () => {
  const f = form(reads(), "0.1");
  const v = previewView(f.preview, BTCP);
  assert.equal(v.blocked, false);
  assert.deepEqual(v.rows, [
    ["Est. fill price", "60010 (book)"], ["Cost", "$2.70045"], ["Position after", "0.1 BTC"], ["Leverage after", "0.60×"]]);
  assert.equal(v.note, ESTIMATE_NOTE);
  assert.deepEqual(v.warnings, []);
  assert.equal(f.preview.px, 600_100n, "the figure is the library's, not worked out here");
});

test("an order the contract would refuse is blocked, in the contract's words", () => {
  const waiting = reads({ pending: Object.assign([BTC, true, 1_000n, 1_000n], { asset: BTC, isBuy: true, sz: 1_000n, placedBlock: 1_000n }) });
  const v = previewView(form(waiting, "0.1").preview, BTCP);
  assert.equal(v.blocked, true);
  assert.equal(v.warnings[0].text, refusalText("OrderPending"));
  assert.equal(previewView(form(reads({ status: 3n }), "0.1").preview, BTCP).warnings[0].text, refusalText("BadStatus"));
  assert.equal(previewView(null, BTCP), null);
});

test("an order over the leverage is accepted but not filled, and one that ends under a loss line says so, red", () => {
  const over = previewView(form(reads(), "2").preview, BTCP);
  assert.equal(over.blocked, false);
  assert.deepEqual(over.warnings.map((w) => [w.tone, w.text]).slice(0, 1), [["bad", refusalText("OverLeverage")]]);
  // BTC is held at a price where the account already stands under its daily line (9 700): any order ends there
  const hurt = reads({ cash: 4_000n * USD }, () => long({ oracle: 540_000n }));
  const v = previewView(orderForm(hurt, { asset: ETH, perp: PERPS.get(ETH), isBuy: true, sizeInput: "0.01", prices: { oracle: 30_000n, bid: 29_990n, ask: 30_010n }, ...AT }).preview, PERPS.get(ETH));
  assert.ok(v.warnings.some((w) => w.tone === "bad" && w.text === SCREEN_TEXT.breach), "the account would stand under its line");
});

test("an order that closes a position says it takes its stop and take off", () => {
  const stop = { px: 550_000n, sz: 0n, armedAt: 900, seenAt: 0 };
  const r = readsFrom(got({ cash: 4_000n * USD }), long({ stop }));
  const v = previewView(orderForm(r, { asset: BTC, perp: BTCP, isBuy: false, sizeInput: "0.1", prices: PRICES, ...AT }).preview, BTCP);
  assert.match(v.warnings.at(-1).text, /closes or turns a position round takes its stop and take off\. This one takes off the stop\./);
});

test("a stop is typed as a price and worked out by the library; one past the oracle price says it closes at once", () => {
  const stop = { px: 550_000n, sz: 0n, armedAt: 900, seenAt: 0 };
  const r = readsFrom(got({ cash: 4_000n * USD }), long({ stop }));
  const t = triggerForm(r, { asset: BTC, perp: BTCP, isTake: false, pxInput: "59000", sizeInput: "", oracle: PX, ...AT });
  assert.equal(t.px, 590_000n);
  const v = triggerPreviewView(t.preview, BTCP);
  assert.deepEqual(v.lines.map((l) => l.text), ["Stop 59000 · closes all · acts from block 1001", "Replaces the stop at 55000."]);
  const past = triggerPreviewView(triggerForm(r, { asset: BTC, perp: BTCP, isTake: false, pxInput: "61000", sizeInput: "0.05", oracle: PX, ...AT }).preview, BTCP);
  assert.equal(past.lines.at(-1).text, SCREEN_TEXT.triggerReached);
  assert.equal(past.lines[0].text, "Stop 61000 · closes 0.05 · acts from block 1001");
  assert.match(triggerForm(r, { asset: BTC, perp: BTCP, isTake: false, pxInput: "61000.55", sizeInput: "", oracle: PX, ...AT }).error, /at most 1 decimals/);
  assert.equal(triggerForm(r, { asset: BTC, perp: BTCP, isTake: false, pxInput: "", sizeInput: "", oracle: PX, ...AT }).preview, null);
  const none = triggerPreviewView(triggerForm(reads(), { asset: BTC, perp: BTCP, isTake: true, pxInput: "70000", sizeInput: "", oracle: PX, ...AT }).preview, BTCP);
  assert.deepEqual([none.blocked, none.lines[0].text], [true, refusalText("NoPosition")]);
});

// ── the book, the chart, the journal ───────────────────────────────

test("the book shows the asks above the bids, ten a side at most, with each level's depth against the deeper side", () => {
  const side = (px0, dir) => Array.from({ length: 12 }, (_, i) => ({ px: String(px0 + dir * i), sz: "1", n: 1 }));
  const b = bookView({ time: 5, levels: [side(100, -1), side(101, 1)] });
  assert.equal(b.asks.length, 10);
  assert.equal(b.bids.length, 10);
  assert.equal(b.asks[0].px, "110", "the highest ask first, the best one last");
  assert.equal(b.asks.at(-1).px, "101");
  assert.equal(b.bids[0].px, "100");
  assert.equal(b.bids.at(-1).share, 100);
  assert.equal(b.asks[0].share, 100);
  assert.equal(b.asks.at(-1).share, 10);
  assert.equal(b.spread, "99.5 bps");
  assert.equal(bookView({ levels: [[], []] }).empty, true);
  assert.equal(bookView({ error: "x" }).empty, true, "an error answer is no book");
});

test("the journal reads the events in order, and a stop that was carried out is one row, said as a stop filled", () => {
  const ev = (name, args, block) => ({ name, args, block });
  const rows = journalRows([
    ev("Placed", { asset: BTC, isBuy: true, sz: 1_500n, fillFrom: 11n, fillUntil: 20n }, 10),
    ev("Filled", { asset: BTC, isBuy: true, sz: 1_500n, px: 600_100n, cost: 405_000n, late: 2n, source: 1n }, 13),
    ev("TriggerSet", { asset: BTC, isTake: false, px: 550_000n, sz: 0n, armedAt: 16n }, 14),
    ev("Filled", { asset: BTC, isBuy: false, sz: 1_500n, px: 549_900n, cost: 400_000n, late: 0n, source: 0n }, 40),
    ev("TriggerFilled", { asset: BTC, isTake: false, sz: 1_500n, px: 549_900n, cost: 400_000n, triggerPx: 550_000n, source: 0n }, 40),
    ev("Lapsed", { asset: ETH, isBuy: true, sz: 50n }, 55),
    ev("DaySnapshot", { day: 20_400n, equity: 10_100n * USD }, 60),
  ], PERPS);
  assert.deepEqual(rows.map((r) => r.text), [
    "Order placed: Buy 0.015 BTC · fills from block 11 to 20",
    "Filled: Buy 0.015 BTC at 60010 · cost $0.405 · price from the book · 2 blocks after the first",
    "Stop set on BTC at 55000 · closes all · acts from block 16",
    "Stop filled: Sell 0.015 BTC at 54990 · cost $0.40 · price from the oracle · it was set at 55000",
    "Order lapsed at no cost: Buy 0.005 ETH",
    "Day 20400 snapshot: equity $10,100.00",
  ]);
  assert.deepEqual(rows[1].cells, { event: "Filled", asset: "BTC", side: "Buy", size: "0.015", price: "60010", cost: "$0.405", from: "book", note: "2 blocks" });
  assert.deepEqual([rows[3].cells.event, rows[3].cells.note, rows[3].cells.price], ["Stop filled", "set at 55000", "54990"]);
  assert.deepEqual([rows[5].cells.event, rows[5].cells.note, rows[5].cells.asset], ["DaySnapshot", "equity $10,100.00", "—"]);
});

// ── the page's own files ───────────────────────────────────────────

test("the screen says what the brief wants said, and the strip is the library's own words", () => {
  assert.equal(WHAT_THIS_IS, "Prices and the book are Hyperliquid's. You close a position with an order of your own, or with a stop or a take you set on it.");
  assert.equal(SCREEN_TEXT.strip, "Exam");
  assert.equal(SCREEN_TEXT.testnetPlaque, "Testnet. Mock USDC. Unaudited.");
  assert.equal(SCREEN_TEXT.testnetTag, "Testnet app");
  assert.equal(SCREEN_TEXT.fillsLater, "Orders fill a few seconds after you send them, at the price of that moment.");
  assert.equal(SCREEN_TEXT.exit, "To exit a position, send an order yourself, or set a stop or a take on it.");
  assert.equal(SCREEN_TEXT.gas, "Each order is a transaction: a signature and gas. Or name a second key and trade without prompts.");
  assert.match(SCREEN_TEXT.secondKey, /^A second key can place orders and pass the step on this exam\. It cannot move money: an exam holds none\./);
  const view = read("../views/exam.js");
  assert.match(view, /esc\(WHAT_THIS_IS\)/, "the strip takes the library's words");
  assert.match(view, /esc\(ESTIMATE_NOTE\)|view\.note/, "the estimate's note comes from the library");
});

test("the page names no network, chain, address or perp number: the deployment and the settings do", () => {
  const view = read("../views/exam.js");
  const lib = read("../lib/examscreen.js");
  for (const [name, text] of [["views/exam.js", view], ["lib/examscreen.js", lib]]) {
    assert.doesNotMatch(text, /hyperliquid(-testnet)?\.xyz|api\.hyperliquid|\b998\b|\b999\b|0x3e[67]/i, `${name} names a network`);
    assert.doesNotMatch(text.replaceAll("0x0000000000000000000000000000000000000000", ""), /0x[0-9a-fA-F]{40}/, `${name} holds an address (the zero address aside)`);
  }
  assert.match(view, /dataSource\(\{ chainId \}, CONFIG\)/, "the data source is the library's, from the chain the exam is on and the settings");
  assert.match(view, /fetch\(CONFIG\.rpc[\s\S]{0,160}eth_chainId/, "the chain id is asked of the node itself");
  assert.doesNotMatch(view, /readProvider\.send\("eth_chainId"/, "the read provider answers with the app's own chain (staticNetwork): no check");
  assert.match(view, /exam\.rules\(\)/, "the assets are the exam's rules");
  assert.doesNotMatch(view, /perps\.set\(\d|assets = \[\d/, "no asset list in the page");
});

test("the page sends nothing to an exchange, shows no exchange's address and does no money arithmetic of its own", () => {
  const view = read("../views/exam.js");
  assert.doesNotMatch(view, /hlExchange|\/exchange|gateway\.[a-z]|hlsend|hlSend/, "no exchange, no gateway");
  assert.doesNotMatch(view, /real account|real money|deposit|withdraw/i);
  assert.doesNotMatch(view, /\bBigInt\([^)]*\)\s*[*+\-/]|\* *10n|\/ *10n/, "no money sums in the view");
});

test("the page is routed at #/exam/<address>, and the other trading page is left as it is", () => {
  const app = read("../app.js");
  assert.match(app, /\^#\\\/exam\(\?:\\\/\(0x\[0-9a-fA-F\]\{40\}\)\)\?\(\?:\\\?\(\.\*\)\)\?\$\//);
  assert.match(app, /examView\(m\[1\], page, m\[2\]\)/, "the query reaches the page: it names the market");
  assert.match(app, /import \{ examView \} from "\.\/views\/exam\.js"/);
  assert.doesNotMatch(read("../views/trading.js"), /examscreen|PAPER_EXAM/);
});

test("on a network the page is not built for, nothing is drawn but the library's words", () => {
  const off = dataSource({ chainId: 999 }, { chainId: 998, hlInfo: "x", hyperliquidChain: "Testnet" });
  assert.equal(off.ok, false);
  assert.match(off.text, /No chart or book is shown/);
  const on = dataSource({ chainId: 998 }, { chainId: 998, hlInfo: "x", hyperliquidChain: "Testnet" });
  assert.deepEqual([on.ok, on.rehearsal], [true, true]);
  assert.equal(dataSource({ chainId: 999 }, { chainId: 999, hlInfo: "x", hyperliquidChain: "Mainnet" }).rehearsal, false);
  assert.match(read("../views/exam.js"), /networkMarks\(CONFIG\)/, "the plaque and the tag follow the settings of the network the page is built for");
  assert.match(read("../views/exam.js"), /<h3>Chart and book not shown<\/h3><p>\$\{esc\(ds\.text\)\}/, "a refused source shows its text");
});

// ── the approved layout ─────────────────────────────────────────────

test("a dollar amount keeps the library's digits: thousands grouped, two decimals at least, nothing rounded", () => {
  assert.equal(usdShown(10_240n * USD), "$10,240.00");
  assert.equal(usdShown(9_999_965_000n), "$9,999.965");
  assert.equal(usdShown(500n), "$0.0005");
  assert.equal(usdShown(-5_000n), "-$0.005");
  assert.equal(usdShown(1_234_567n * USD), "$1,234,567.00");
});

test("a testnet page carries the plaque and the tag, a mainnet page neither", () => {
  assert.deepEqual(networkMarks({ hyperliquidChain: "Testnet" }), { plaque: "Testnet. Mock USDC. Unaudited.", tag: "Testnet app" });
  assert.equal(networkMarks({ hyperliquidChain: "Mainnet" }), null);
  assert.equal(networkMarks({ hyperliquidChain: "Mainnet", chainId: 998 }), null, "by the network the page is built for, nothing else");
  assert.match(read("../config.js"), /hyperliquidChain: "Testnet"/);
});

test("the strip's four steps carry no amounts, and the exam is the one in hand while it is active", () => {
  const steps = (status) => lifeSteps({ status }).map((x) => `${x.n} ${x.label}:${x.state}`);
  assert.deepEqual(steps("active"), ["1 Bought:done", "2 Exam:now", "3 Stage:", "4 Payout:"]);
  assert.deepEqual(steps("passed"), ["1 Bought:done", "2 Exam:done", "3 Stage:", "4 Payout:"]);
  assert.equal(steps("breached")[1], "2 Exam:");
});

test("the book's spread is said in price and in bps; a journal time is UTC", () => {
  const b = bookView({ levels: [[{ px: "91248", sz: "1" }], [{ px: "91252", sz: "1" }]] });
  assert.equal(b.spreadAbs, "4");
  assert.equal(bookView({ levels: [[], []] }).spreadAbs, "—");
  assert.equal(timeText(Date.UTC(2026, 10, 17, 13, 2, 41) / 1000), "17 Nov 13:02:41");
});

test("no word of a simulation on the screen, no amount of a payout, no sample marks, no chart maker's credit", () => {
  const strings = Object.values(SCREEN_TEXT).join("\n");
  const view = read("../views/exam.js").split("\n").filter((l) => !/^\s*(\/\/|\/\*|\*)/.test(l)).join("\n");
  const lib = read("../lib/examscreen.js").split("\n").filter((l) => !/^\s*(\/\/|\/\*|\*)/.test(l)).join("\n");
  const exam = read("../lib/exam.js");
  const said = [strings, WHAT_THIS_IS, ESTIMATE_NOTE, ...Object.values(REFUSALS), view, lib].join("\n").replaceAll("PAPER_EXAM_ABI", "");
  assert.doesNotMatch(said, /simulat|paper|rehears/i);
  assert.doesNotMatch(said, /mockup|TradingView|\$800|8 ?%|fixed payout/i);
  assert.match(exam, /export const WHAT_THIS_IS = "Prices and the book are Hyperliquid's\./);
});

test("the page's styles are the approved layout's: three columns, the tabs under the chart and the book, the phone's own order", () => {
  const css = read("../style.css");
  assert.match(css, /\.ex-term \{ display: grid; grid-template-columns: minmax\(0, 1fr\) 300px 340px;/);
  assert.match(css, /--up: #2F7FC9; --dn: #C0622A;/);
  assert.match(css, /\.ex-bottom \{ grid-column: 1 \/ 3; grid-row: 2; \}/);
  assert.match(css, /\.ex-term\[data-phone="chart"\] \.ex-book \{ display: none; \}/);
  assert.doesNotMatch(css, /\.ex-rehearsal/);
});

// ── review of #314 ─────────────────────────────────────────────────

test("an asset the exchange's list does not name is unknown: no size or price is taken in its units", () => {
  const stand = { index: ETH, name: `#${ETH}`, szDecimals: 0 };
  const unknown = markUnknown(stand, ETH);
  assert.deepEqual([unknown.unknown, isKnown(unknown), isKnown(markUnknown(PERPS.get(ETH), ETH))], [true, false, true]);
  assert.equal(markUnknown({ index: ETH, name: "ETH", szDecimals: 4 }, ETH).unknown, undefined);
  assert.equal(markUnknown(undefined, ETH).unknown, true);
  // a size of 1 would be one unit at no decimals, 10 000 times too small for an asset of four
  const o = orderForm(reads(), { asset: ETH, perp: unknown, isBuy: true, sizeInput: "1", prices: { oracle: 30_000n, bid: 29_990n, ask: 30_010n }, ...AT });
  assert.deepEqual([o.size, o.preview, o.error], [null, null, SCREEN_TEXT.noAsset]);
  const t = triggerForm(reads(), { asset: ETH, perp: unknown, isTake: false, pxInput: "3000", sizeInput: "", oracle: 30_000n, ...AT });
  assert.deepEqual([t.px, t.preview, t.error], [null, null, SCREEN_TEXT.noAsset]);
  assert.equal(previewView(o.preview, unknown), null, "nothing to show but the message");
  const none = orderForm(reads(), { asset: ETH, perp: undefined, isBuy: true, sizeInput: "1", prices: PRICES, ...AT });
  assert.equal(none.error, SCREEN_TEXT.noAsset, "a missing asset is as unknown");
});

test("an unknown asset's position, order and journal rows show no size or price, only what is in the contract's own units", () => {
  const perps = new Map([[BTC, BTCP], [ETH, markUnknown({ index: ETH, name: "#4", szDecimals: 0 }, ETH)]]);
  const stop = { px: 30_000n, sz: 0n, armedAt: 900, seenAt: 0 };
  const a = examAccount(reads({ cash: 9_000n * USD }, () => [{ asset: BTC, size: 0n, oracle: PX }, { asset: ETH, size: 10_000n, oracle: 30_000n, stop }, { asset: SOL, size: 0n, oracle: 1_500n }]), AT);
  const row = positionRow(a.positions[0], perps);
  assert.deepEqual([row.name, row.size, row.mark, row.stop, row.closeSize], ["#4", "—", "—", "Stop set", 10_000n]);
  assert.equal(row.notional, "$300.00", "the notional is the library's, in USDC");
  const order = got({ pending: Object.assign([ETH, true, 1_500n, 1_000n], { asset: ETH, isBuy: true, sz: 1_500n, placedBlock: 1_000n }) });
  assert.equal(pendingText(examAccount(readsFrom(order, flat()), { now: DAY, block: 1_000 }).pending, perps).text, "Order sent: Buy — #4 · fills from block 1001");
  const rows = journalRows([{ name: "Filled", args: { asset: ETH, isBuy: true, sz: 1_500n, px: 30_000n, cost: 1n, late: 0n, source: 0n }, block: 5 }], perps);
  assert.deepEqual([rows[0].cells.size, rows[0].cells.price], ["—", "—"]);
});

test("one call is in flight at a time: a second press does nothing, and only the call that started clears the flag", async () => {
  const f = singleFlight();
  let release;
  const first = f.run(() => new Promise((r) => { release = r; }));
  assert.equal(f.busy, true);
  let ran = false;
  assert.deepEqual(await f.run(async () => { ran = true; }), { skipped: true });
  assert.equal(ran, false);
  assert.equal(f.busy, true, "the refused press did not clear it");
  release("done");
  assert.deepEqual(await first, { skipped: false, value: "done" });
  assert.equal(f.busy, false);
  await assert.rejects(f.run(async () => { throw new Error("x"); }), /x/);
  assert.equal(f.busy, false, "a failed call clears it too");
  assert.deepEqual(await f.run(async () => 1), { skipped: false, value: 1 });
});

test("the page reads the flag in its handlers and paints the sending buttons off while a call is out", () => {
  const view = read("../views/exam.js");
  assert.match(view, /st\.flight\.run\(async/);
  assert.doesNotMatch(view, /st\.busy/);
  assert.match(view, /data-close="\$\{r\.asset\}"\$\{st\.flight\.busy \? " disabled" : ""\}/);
  assert.match(view, /data-clear="\$\{r\.asset\}:\$\{k\}"\$\{st\.flight\.busy/);
  assert.match(view, /g\.disabled = !account\.graduate\.can \|\| st\.flight\.busy/);
  const poll = view.slice(view.indexOf("function paintPositions"), view.indexOf("function paintTicket"));
  assert.doesNotMatch(poll, /paintJournal/, "the poll does not repaint the journal (its scroll is the reader's)");
});

test("the journal's range joins what is loaded: the newest blocks, then older, then the ones since the top", () => {
  assert.deepEqual(journalPlan({ latest: 5_000, loaded: null, direction: "earlier" }), { from: 4_001, to: 5_000 });
  assert.deepEqual(journalPlan({ latest: 500, loaded: null, direction: "earlier" }), { from: 0, to: 500 });
  const loaded = { from: 4_001, top: 5_000 };
  assert.deepEqual(journalPlan({ latest: 5_000, loaded, direction: "newer" }), null, "nothing newer");
  assert.deepEqual(journalPlan({ latest: 5_060, loaded, direction: "newer" }), { from: 5_001, to: 5_060 });
  assert.deepEqual(journalPlan({ latest: 9_000, loaded, direction: "newer" }), { from: 5_001, to: 6_000 }, "a long gap is read a stretch at a time, from the top");
  assert.deepEqual(journalPlan({ latest: 5_000, loaded, direction: "earlier" }), { from: 3_001, to: 4_000 });
  assert.equal(journalPlan({ latest: 5_000, loaded: { from: 0, top: 5_000 }, direction: "earlier" }), null);
  assert.equal(journalRange(null), "");
  assert.equal(journalRange({ from: 3_001, top: 5_060 }), "Events from block 3001 to block 5060.", "the loaded run, not the latest block");
  const view = read("../views/exam.js");
  assert.match(view, /id="ex-journal-new"/);
  assert.doesNotMatch(view, /to \$\{latest\}/);
});

test("the journal's button that loads earlier events is off once the range starts at block 0, and open before that", () => {
  assert.equal(canLoadEarlier(null), true, "nothing loaded: the first load is open");
  assert.equal(canLoadEarlier({ from: 1, top: 500 }), true);
  assert.equal(canLoadEarlier({ from: 0, top: 500 }), false);
  // a plan from block 0 has nothing earlier, and the page switches the button off, not only says so
  assert.equal(journalPlan({ latest: 500, loaded: { from: 0, top: 500 }, direction: "earlier" }), null);
  const view = read("../views/exam.js");
  assert.match(view, /buttons\[0\]\.disabled = !canLoadEarlier\(st\.loaded\);/);
  assert.ok(view.indexOf("buttons.forEach((x) => { x.disabled = false; });") < view.indexOf("buttons[0].disabled = !canLoadEarlier(st.loaded);"), "the switch-off comes after the buttons are opened again");
});

test("the trading screen stands in the user panel's header for an exam of a pool of exams, and in the site's for any other", () => {
  const view = read("../views/exam.js");
  assert.match(view, /userHeaderHtml\(\{ address: panelPool, account: accountLabel\(\), current: "trade"/);
  assert.match(view, /if \(!panelPool\) leavePanel\(page\);/);
  assert.match(view, /const leavePanel = \(page\) => \{ if \(page\.isConnected\) delete document\.body\.dataset\.panel; \};/, "a left-behind view does not clear the marker of the page now on screen");
  assert.ok(view.indexOf("if (!page.isConnected) return;") < view.indexOf("if (!panelPool) leavePanel(page);"));
  assert.match(read("../app.js"), /function failed\(page, err\) \{[^}]*delete document\.body\.dataset\.panel;/, "a page that did not load gets the site's navigation back");
  assert.match(view, /accountOf\(address\)/);
  // the panel's header and footer carry the testnet tag and badge, so the screen's own are drawn only outside the panel
  assert.match(view, /marks && !st\.panelPool \? `<span class="ex-tag">/);
  assert.match(view, /marks && !st\.panelPool \? `<div class="ex-foot">/);
});

// ── the market list ────────────────────────────────────────────────

const RULES10 = { maxLeverageX100: 1000n, assetLeverageX100: [1000n, 1000n, 500n] };
const perAsset = (held = []) => [BTC, ETH, SOL].map((asset) => ({ asset, size: held.includes(asset) ? 5_000n : 0n,
  oracle: { [BTC]: 642_500n, [ETH]: 312_050n, [SOL]: 1_482_500n }[asset] }));

test("the market list holds the exam's assets only, each with its price, its own leverage cap and a mark where a position is open", () => {
  const rows = marketRows({ assets: [BTC, ETH, SOL], perps: PERPS, perAsset: perAsset([ETH]), rules: RULES10 });
  assert.deepEqual(rows.map((r) => [r.name, r.price, r.leverage, r.held]), [
    ["BTC", "64250", "10×", false], ["ETH", "3120.5", "10×", true], ["SOL", "148.25", "5×", false],
  ]);
  // an asset the list of perps does not name is still a row, with no price made up
  const odd = marketRows({ assets: [9], perps: PERPS, perAsset: [{ asset: 9, size: 0n, oracle: 5n }], rules: { maxLeverageX100: 2000n, assetLeverageX100: [] } });
  assert.deepEqual([odd[0].name, odd[0].price, odd[0].leverage], ["#9", "—", "20×"]);
  // a price not read is a dash
  assert.equal(marketRows({ assets: [BTC], perps: PERPS, perAsset: [{ asset: BTC, size: 0n, oracle: null }], rules: RULES10 })[0].price, "—");
});

test("the search keeps the names that contain it, in any case, and a hundred assets are a list like three", () => {
  const rows = marketRows({ assets: [BTC, ETH, SOL], perps: PERPS, perAsset: perAsset(), rules: RULES10, query: "  e " });
  assert.deepEqual(rows.map((r) => r.name), ["ETH"]);
  assert.deepEqual(marketRows({ assets: [BTC, ETH, SOL], perps: PERPS, perAsset: perAsset(), rules: RULES10, query: "so" }).map((r) => r.name), ["SOL"]);
  assert.deepEqual(marketRows({ assets: [BTC], perps: PERPS, perAsset: perAsset(), rules: RULES10, query: "zzz" }), []);
  const many = Array.from({ length: 100 }, (_, i) => 1000 + i);
  const perps = new Map(many.map((a) => [a, { index: a, name: `A${a}`, szDecimals: 2 }]));
  const rules = { maxLeverageX100: 1000n, assetLeverageX100: many.map(() => 300n) };
  const all = marketRows({ assets: many, perps, perAsset: many.map((asset) => ({ asset, size: 0n, oracle: 100_000n })), rules });
  assert.equal(all.length, 100);
  assert.equal(marketRows({ assets: many, perps, perAsset: [], rules, query: "A1055" }).length, 1);
});

test("the arrows step through the list and wrap, Home and End jump, an empty list highlights nothing", () => {
  assert.equal(stepIndex(3, -1, "ArrowDown"), 0);
  assert.equal(stepIndex(3, 2, "ArrowDown"), 0);
  assert.equal(stepIndex(3, 0, "ArrowUp"), 2);
  assert.equal(stepIndex(3, -1, "ArrowUp"), 2);
  assert.equal(stepIndex(100, 5, "End"), 99);
  assert.equal(stepIndex(100, 5, "Home"), 0);
  assert.equal(stepIndex(0, 0, "ArrowDown"), -1);
  assert.equal(stepIndex(3, 7, "x"), 2, "an index past the end is brought back");
});

test("the address names the market: a link opens the same one, and an asset the exam does not trade is not taken", () => {
  assert.equal(assetFromQuery("asset=4", [BTC, ETH, SOL]), ETH);
  assert.equal(assetFromQuery("code=x&asset=5", [BTC, ETH, SOL]), SOL);
  for (const q of ["asset=9", "asset=", "asset=-1", "asset=4x", "asset=0x4", "", undefined]) assert.equal(assetFromQuery(q, [BTC, ETH, SOL]), BTC, String(q));
  assert.equal(queryWithAsset("", ETH), "asset=4");
  assert.equal(queryWithAsset("asset=3&x=1", SOL), "asset=5&x=1");
  const view = read("../views/exam.js");
  assert.match(view, /history\.replaceState\(history\.state, "", `\$\{base\}\?\$\{queryWithAsset\(st\.query, st\.asset\)\}`\)/, "the address is rewritten without an entry of history");
  assert.ok(!/ex-assets|id="ex-asset"/.test(view.replace(/\/\/.*$/gm, "")), "no tabs of assets in the order column, no second selector");
  assert.match(view, /role="listbox"/);
  assert.match(view, /e\.key === "Escape"/);
  assert.match(view, /e\.key === "Enter"/);
});

// ── the unit of the size ───────────────────────────────────────────

const READS = { fromBook: false, maxWorseBps: 150 };
const prices = { oracle: 642_500n, bid: null, ask: null };

test("the unit is the asset's or USDC, and what the browser holds is one of the two words", () => {
  assert.equal(sizeUnitOf("usdc"), "usdc");
  assert.equal(sizeUnitOf("asset"), "asset");
  for (const v of [null, undefined, "", "USD", "<b>"]) assert.equal(sizeUnitOf(v), "asset");
});

test("an amount of USDC is read exactly, to six decimals", () => {
  assert.equal(usdcUnits("1035.03"), 1_035_030_000n);
  assert.equal(usdcUnits("0.000001"), 1n);
  assert.equal(usdcUnits("100"), 100_000_000n);
  assert.throws(() => usdcUnits("0.0000011"), /at most 6 decimals/);
  for (const bad of ["", "-5", "1e3", "1,000", "abc"]) assert.throws(() => usdcUnits(bad), /plain positive number/);
});

test("typed in USDC, the size is the amount at the estimate's price rounded down, and both numbers are shown", () => {
  // $1,035.03 at 64 250.0 is 0.016108... BTC: 0.01610, never 0.01611
  const e = sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "usdc", input: "1035.03", prices });
  assert.equal(e.sizeInput, "0.0161");
  assert.equal(e.conv, "≈ 0.01610 BTC", "the size at the asset's own decimals");
  assert.equal(e.error, null);
  assert.equal(e.disabled, null);
  // on the book's side: a buy at a dearer ask is a smaller size for the same amount
  const book = { fromBook: true, maxWorseBps: 150 };
  const dear = sizeEntry(book, { perp: BTCP, isBuy: true, unit: "usdc", input: "1035.03", prices: { oracle: 642_500n, bid: 642_000n, ask: 645_000n } });
  assert.equal(dear.sizeInput, "0.01604");
  const sell = sizeEntry(book, { perp: BTCP, isBuy: false, unit: "usdc", input: "1035.03", prices: { oracle: 642_500n, bid: 642_000n, ask: 645_000n } });
  assert.equal(sell.sizeInput, "0.01612");
});

test("typed in the asset's units, the amount is shown under the field at the same price", () => {
  const e = sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "asset", input: "0.0161", prices });
  assert.equal(e.sizeInput, "0.0161");
  assert.equal(e.conv, "≈ $1,034.425");
  assert.equal(sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "asset", input: "", prices }).conv, "");
  // a size the asset cannot carry says nothing of its amount: the order form refuses it in the library's words
  assert.equal(sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "asset", input: "0.000001", prices }).conv, "");
  assert.equal(sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "asset", input: "0.0161", prices: { oracle: null } }).conv, "");
});

test("in USDC the field is off, with the reason, while the price is not read; an asset with no data is off as before", () => {
  const off = sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "usdc", input: "100", prices: { oracle: null, bid: null, ask: null } });
  assert.equal(off.disabled, SCREEN_TEXT.usdcNoPrice);
  assert.equal(off.sizeInput, "");
  assert.equal(sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "usdc", input: "100", prices: { oracle: 0n } }).disabled, SCREEN_TEXT.usdcNoPrice);
  const unknown = sizeEntry(READS, { perp: { index: 9, name: "#9", szDecimals: 0, unknown: true }, isBuy: true, unit: "usdc", input: "100", prices });
  assert.deepEqual([unknown.sizeInput, unknown.disabled, unknown.error], ["", null, null]);
  assert.equal(orderForm(reads(), { asset: 9, perp: { unknown: true, name: "#9" }, isBuy: true, sizeInput: "1", prices, ...AT }).error, SCREEN_TEXT.noAsset);
});

test("an amount below one step, or a zero, is the library's refusal, shown under the field", () => {
  const less = sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "usdc", input: "0.5", prices });
  assert.match(less.error, /less than one step of this asset's size/);
  assert.equal(less.sizeInput, "");
  assert.match(sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "usdc", input: "0", prices }).error, /above zero/);
  assert.match(sizeEntry(READS, { perp: BTCP, isBuy: true, unit: "usdc", input: "x", prices }).error, /plain positive number/);
});

test("the order is sent in the asset's units, the unit is remembered in the browser, and stops and takes keep their own size field", () => {
  const view = read("../views/exam.js");
  assert.match(view, /sizeEntry\(reads, \{ perp, isBuy: st\.isBuy, unit: st\.unit/);
  assert.match(view, /store\(UNIT_KEY, st\.unit\)/);
  assert.match(view, /orderForm\(reads, \{[^}]*sizeInput: entry\.sizeInput/);
  assert.match(view, /placeCall\(\{ exam: st\.address, asset: st\.asset, isBuy: st\.isBuy, size: st\.order\.size/);
  assert.match(view, /id="ex-\$\{k\}-sz" inputmode="decimal" autocomplete="off" placeholder="all"/, "the stop and the take size stays in the asset or 'all'");
});
