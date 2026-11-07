// node --test app/tests/poolquote.test.mjs
//
// The estimate a pool's creator sees beside the creation form, and the form's own rules
// (app/lib/poolquote.js, app/lib/poolform.js, app/views/poolform.js).
//
// The arithmetic is our finance model's, written in Python and carried to app/lib line by line. The nineteen cases
// in pool-quote-vectors-2026-10-13.json are that model's own inputs and outputs, written by the Python: the port is
// held to every number in them, to the sixth decimal. The form may show a number only where the model has one;
// everywhere else it says "No estimate" and why.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { quote, unit, atLeverage, settle, gambler, kaufmanRoberts, NO_ESTIMATE, LABEL, SCENARIOS, EXAMS, DRAWDOWNS, TARGETS, MONTHS,
  MIX, DEMAND, LEVERAGES } from "../lib/poolquote.js";
import { SCALE, SIZES, LEVERAGE, LEVERAGE_RISK, MODEL, FIELDS, INVESTORS, AFTER_TARGET, AT_END, TEXT, NOTICE_DAYS, defaults, outOfBounds,
  modelArgs, estimate, termWords, scaleWords } from "../lib/poolform.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const TABLE = JSON.parse(text("../data/pool-quote-units-2026-10-13.json"));
const VECTORS = JSON.parse(text("./pool-quote-vectors-2026-10-13.json"));
const sha = (s) => createHash("sha256").update(s).digest("hex");
// Keys in order at every depth, no spaces: the same bytes whoever serializes it.
const canon = (v) => (Array.isArray(v) ? `[${v.map(canon).join(",")}]`
  : v && typeof v === "object" ? `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(",")}}`
    : JSON.stringify(v));

/** The form as it opens, with the creator's three first choices made: one investor, 100 USDC, 12 months. */
const chosen = (over = {}) => ({ ...defaults(), investors: "one", amount: 100, months: 12, ...over });

test("the port gives the model's own numbers for its nineteen cases, to the sixth decimal", () => {
  assert.equal(VECTORS.cases.length, 19);
  let numbers = 0;
  const inputs = VECTORS.cases.map((c) => c.in);
  for (const { in: input, out } of VECTORS.cases) {
    const got = quote(TABLE, input);
    assert.deepEqual(Object.keys(got).sort(), Object.keys(out).sort(), JSON.stringify(input));
    for (const part of [...SCENARIOS, "gambler"]) {
      assert.deepEqual(Object.keys(got[part]).sort(), Object.keys(out[part]).sort());
      for (const [name, want] of Object.entries(out[part])) {
        numbers++;
        if (typeof want === "boolean") assert.equal(got[part][name], want, `${part}.${name} ${JSON.stringify(input)}`);
        else assert.ok(Math.abs(got[part][name] - want) <= 5.5e-7, `${part}.${name}: ${got[part][name]} against ${want} for ${JSON.stringify(input)}`);
      }
    }
  }
  assert.equal(numbers, 646);
  // Seven of the cases are the leverage's: on each node, between nodes, and above the last node.
  const levers = inputs.map((i) => i.leverage).filter((x) => x !== undefined).sort((a, b) => a - b);
  assert.deepEqual(levers, [1, 1.2, 1.5, 2, 2.3, 3, 10]);
  // The cases reach every exam, every way the account can end, both ways out of a term, and a menu of one size.
  for (const exam of Object.keys(EXAMS)) assert.ok(inputs.some((i) => (i.exam ?? "1x10") === exam), exam);
  for (const mode of ["closed", "stays", "self"]) assert.ok(inputs.some((i) => i.mode === mode), mode);
  assert.ok(inputs.some((i) => i.withdraw === "profit") && inputs.some((i) => i.menu?.length === 1));
  assert.equal(LABEL, "a model, not a promise");
});

test("the table is the model's, number for number, and says what it was simulated with", () => {
  // The scenarios and the turnover of the model's table, in canonical JSON: any number changed here is seen.
  assert.equal(sha(canon({ scenarios: TABLE.scenarios, turnover: TABLE.meta.turnover })),
    "fb4a8e833651f62d491577493fa3c7aa0ec59dc4716abf9050c03b977a9e2616");
  assert.deepEqual(Object.keys(TABLE.scenarios), ["bad", "base", "good"]);
  assert.equal(TABLE.meta.paths, 30000);
  assert.equal(TABLE.meta.seed, 20261013);
  // What the simulation held fixed is what the form asks for before it shows a number.
  // The leverage: the scenarios are every leverage from 2.5x up, and the table has stages of their own below it.
  assert.equal(TABLE.meta.leverage, 5);
  assert.deepEqual(TABLE.meta.leverage_nodes, LEVERAGES);
  assert.deepEqual(Object.keys(TABLE.leverage), ["1.00", "1.50", "2.00"]);
  assert.equal(sha(canon(TABLE.leverage)), "6e16519e251ccba85a84bea2c033e76f6b9824b4b45cceb109330d71000ce2ab");
  assert.equal(100 * TABLE.meta.exam_drawdown, MODEL.examDrawdown);
  assert.equal(100 * TABLE.meta.daily_loss, MODEL.daily);
  assert.equal(TABLE.meta.exam_days, MODEL.stepDays);
  assert.equal(MODEL.price, 1);
  for (const scenario of SCENARIOS) {
    const sc = TABLE.scenarios[scenario];
    assert.ok(Math.abs(sc.weights.reduce((a, b) => a + b, 0) - 1) < 1e-9, "the kinds of trader are the whole of the buyers");
    assert.deepEqual(Object.keys(sc.exam).sort(), ["0.05", "0.08", "0.10"]);
    assert.equal(Object.keys(sc.funded).length, DRAWDOWNS.length * (TARGETS.length + 1));
  }
  assert.deepEqual(DEMAND, { bad: 5, base: 15, good: 40 });
  assert.deepEqual(MIX, { 5000: 0.55, 10000: 0.15, 25000: 0.30 });
});

test("outside what the model covers there is no estimate: it refuses, it does not compute", () => {
  const base = { pool_usd: 100000, months: 12, mode: "closed" };
  const refused = (over) => assert.throws(() => quote(TABLE, { ...base, ...over }), (err) => err.code === NO_ESTIMATE, JSON.stringify(over));
  assert.ok(quote(TABLE, base).base.pct_year > 0);
  for (const over of [
    { mode: "open" }, { exam: "1x12" }, { exam: "toString" }, { drawdown: 0.07 }, { target: 0.12 },
    { months: 5 }, { months: 25 }, { share: 0.49 }, { share: 0.91 }, { work: 0.49 }, { work: 0.91 }, { withdraw: "some" },
    { menu: [] }, { menu: [5000, 7000] }, { menu: ["toString"] }, { pool_usd: 99900 }, { pool_usd: 100050 },
  ]) refused(over);
  // The edges it does cover.
  for (const over of [{ leverage: 0.99 }, { leverage: 10.01 }, { leverage: 0 }]) refused(over);
  for (const over of [{ months: 6 }, { months: 24 }, { share: 0.5 }, { share: 0.9 }, { work: 0.5 }, { work: 0.9 }, { menu: [25000] },
    { leverage: 1 }, { leverage: 10 }]) {
    assert.ok(Number.isFinite(quote(TABLE, { ...base, ...over }).base.pct_term), JSON.stringify(over));
  }
  // With no target, the target is not asked: the trader stops the account themselves.
  assert.ok(Number.isFinite(quote(TABLE, { ...base, mode: "self", target: 0.12 }).base.pct_term));
});

test("the parts of the arithmetic, each on a case small enough to do by hand", () => {
  // One size of one unit, load 1, room for 1: half of the buyers find no room. Room for none: all of them.
  assert.deepEqual(kaufmanRoberts(1, [[1, 1.0]]), [0.5]);
  assert.deepEqual(kaufmanRoberts(0, [[1, 1.0]]), [1]);
  assert.deepEqual(kaufmanRoberts(2, [[1, 1.0]]), [0.2]);
  // A size of two units in room for two, with a size of one beside it.
  const [small, large] = kaufmanRoberts(2, [[1, 1.0], [2, 0.5]]);
  assert.ok(Math.abs(small - 1 / 3) < 1e-12 && Math.abs(large - 2 / 3) < 1e-12);
  // The buyer with no skill: one step of 10% against a 6% drawdown is passed 6 times in 16.
  const g = gambler({ exam: "1x10", drawdown: 0.06, target: 0.10, mode: "closed", share: 0.8 });
  assert.ok(Math.abs(g.pass_max_pct - 37.5) < 1e-9);
  assert.ok(Math.abs(g.need_price_pct - 1.125) < 1e-9);
  assert.equal(g.ok, false);
  assert.equal(gambler({ exam: "2x10+5", drawdown: 0.03, target: 0.05, mode: "closed", share: 0.5 }).ok, true);
  // One trader who always passes in 10 days and is always paid 4% of the seat in 20: the pool pays his share and
  // keeps the rest of what the account made.
  const tables = { meta: { turnover: [2.0] }, scenarios: { base: { weights: [1.0], exam: { "0.10": [[1.0, 10.0]] },
    funded: { "0.06|0.10": [[0.05, 0.04, 1.0, 1.0, 20.0]], "0.06|self": [[0.05, 0.04, 0.0, 1.0, 20.0]] } } } };
  const u = unit(tables, "base", { exam: "1x10", drawdown: 0.06, target: 0.10, mode: "closed", share: 0.8 });
  assert.deepEqual(u, { p_pass: 1, p_paid: 1, cost: 0.8 * 0.04 - 0.05, payout: 0.8 * 0.04, days_exam: 10, days_funded: 20, notional: 40 });
  assert.equal(unit(tables, "base", { exam: "1x10", drawdown: 0.06, target: 0.10, mode: "self", share: 0.8 }).days_funded, 20);
  // A term twice as long sells twice as many seats, and its yearly rate is the result of the term over the years.
  const one = settle(u, "base", { pool_usd: 100000, months: 12, work: 0.8, menu: [5000] });
  const two = settle(u, "base", { pool_usd: 100000, months: 24, work: 0.8, menu: [5000] });
  assert.ok(Math.abs(two.sold - 2 * one.sold) < 1e-9);
  assert.ok(Math.abs(two.pct_year - two.pct_term / 2) < 1e-12);
  assert.equal(one.pct_year, one.pct_term);
  // Between two nodes of leverage a purchase is the straight line between them; from 2.5x up it is the last node.
  const settings = { exam: "1x10", drawdown: 0.06, target: 0.10, mode: "closed", share: 0.8 };
  const [lo, hi, mid] = [1.0, 1.5, 1.25].map((leverage) => atLeverage(TABLE, "base", leverage, settings));
  for (const k of Object.keys(mid)) assert.ok(Math.abs(mid[k] - (lo[k] + hi[k]) / 2) < 1e-12, k);
  assert.deepEqual(atLeverage(TABLE, "base", 1.0, settings), unit(TABLE, "base", { ...settings, node: 1.0 }));
  for (const leverage of [2.5, 3, 7.5, 10]) assert.deepEqual(atLeverage(TABLE, "base", leverage, settings), unit(TABLE, "base", settings));
  assert.notDeepEqual(unit(TABLE, "base", { ...settings, node: 2.0 }), unit(TABLE, "base", settings));
  // A pool that loses: each purchase costs it 2% of the seat against a price of 1%. Withdrawing everything, the
  // fee is on what is left of the pool; withdrawing only the profit, there is none and no fee on a withdrawal.
  const losing = { p_pass: 0, p_paid: 0, cost: 0.02, payout: 0, days_exam: 10, days_funded: 0, notional: 0 };
  const all = settle(losing, "base", { pool_usd: 100000, months: 12, work: 0.8, menu: [5000] });
  const price = all.traders_pay;
  assert.ok(price > 0 && all.usd_term < 0);
  assert.ok(Math.abs(all.platform - (0.005 * price + 0.005 * (100000 - price - 0.005 * price))) < 1e-9);
  const kept = settle(losing, "base", { pool_usd: 100000, months: 12, work: 0.8, menu: [5000], withdraw: "profit" });
  assert.ok(Math.abs(kept.platform - 0.005 * price) < 1e-9);
});

test("the form opens filled: only who invests, the amount and the term are the creator's to give first", () => {
  const d = defaults();
  assert.deepEqual(d, { investors: null, sizes: [5, 10, 25], atEnd: "stop", steps: [10], assets: [], afterTarget: "closed",
    amount: null, months: null, work: 80, price: 1, examDrawdown: 6, examDaily: 3, examLeverage: 5, stepDays: 30, idleDays: 14,
    tradingDays: 0, fillCost: 950, fillDelay: null, fillWindow: null, target: 10, fundedDrawdown: 6, fundedDaily: 3,
    fundedLeverage: 5, share: 80 });
  // The creator is to see the percentage at once: with those three given, nothing else stands between the form as it
  // opens and an estimate. The two block counts of a fill are unset too, and the model takes no account of them.
  assert.deepEqual(Object.keys(d).filter((id) => d[id] === null), ["investors", "amount", "months", "fillDelay", "fillWindow"]);
  assert.deepEqual(modelArgs(d), { invalid: [], missing: ["who invests", "the amount", "the term"], outside: [] });
  assert.equal(estimate(TABLE, { ...d, investors: "one", amount: 100, months: 12 }).ok, true);
  // Where the design gives no default, the form starts where the model was run.
  assert.deepEqual([d.price, d.fundedDrawdown, d.fundedDaily, d.fundedLeverage], [MODEL.price, MODEL.examDrawdown, MODEL.daily, LEVERAGE.start]);
  assert.equal(NOTICE_DAYS, 14);
  // The leverage is the creator's, from 1x to 10x, and starts at 5x.
  assert.deepEqual(LEVERAGE, { min: 1, max: 10, start: 5 });
  for (const id of ["examLeverage", "fundedLeverage"]) {
    const f = FIELDS.find((x) => x.id === id);
    assert.deepEqual([f.min, f.max], [1, 10], id);
  }
  const by = Object.fromEntries(FIELDS.map((f) => [f.id, f]));
  assert.deepEqual([by.months.min, by.months.max], MONTHS);
  assert.deepEqual([by.amount.min, by.amount.step], [100, 100]);
  assert.deepEqual([by.fillCost.min, by.fillCost.max], [0, 100000]);
  assert.deepEqual([by.fillDelay.min, by.fillDelay.max, by.fillWindow.min, by.fillWindow.max], [1, 600, 1, 600]);
  assert.equal(new Set(FIELDS.map((f) => f.id)).size, FIELDS.length);
  // Who invests is the first choice and has no default. The shared pool is shown and cannot be chosen yet.
  assert.deepEqual(INVESTORS.map((o) => [o.id, o.label, o.built]), [["one", "One investor", true], ["several", "Shared pool", false]]);
  assert.equal(INVESTORS[1].note, "Being built: the contract for a pool of several investors is being written, so this cannot be chosen yet.");
  assert.deepEqual(AFTER_TARGET.map((o) => o.id), ["closed", "stays", "self"]);
  assert.deepEqual(AT_END.map((o) => [o.id, o.withdraw]), [["stop", "all"], ["extend", "profit"]]);
  assert.deepEqual(outOfBounds(d), []);
  assert.deepEqual(outOfBounds({ ...d, examLeverage: 12, months: 30, fillDelay: 0, amount: 50 }).map((b) => b.id),
    ["amount", "months", "examLeverage", "fillDelay"]);
});

test("the estimate is asked of the model for a thousand times the amount, with the settings as the model names them", () => {
  assert.equal(SCALE, 1000);
  assert.deepEqual(SIZES, [5, 10, 25]);
  assert.deepEqual(modelArgs(chosen()), { args: { pool_usd: 100000, months: 12, mode: "closed", exam: "1x10", drawdown: 0.06, target: 0.10,
    share: 0.8, work: 0.8, menu: [5000, 10000, 25000], leverage: 5, withdraw: "all" } });
  assert.deepEqual(modelArgs(chosen({ amount: 2000, months: 24, afterTarget: "stays", steps: [10, 5], fundedDrawdown: 5, target: 15, share: 70,
    work: 60, sizes: [10, 25], atEnd: "extend" })).args,
  { pool_usd: 2000000, months: 24, mode: "stays", exam: "2x10+5", drawdown: 0.05, target: 0.15, share: 0.7, work: 0.6,
    menu: [10000, 25000], leverage: 5, withdraw: "profit" });
  for (const [steps, exam] of [[[10], "1x10"], [[8], "1x8"], [[10, 5], "2x10+5"], [[8, 5], "2x8+5"]]) {
    assert.equal(modelArgs(chosen({ steps })).args.exam, exam);
  }
  // The form's result for the model's first case is that case's own numbers.
  const first = VECTORS.cases[0];
  assert.deepEqual(first.in, { pool_usd: 100000, months: 12, mode: "closed" });
  const got = estimate(TABLE, chosen());
  assert.equal(got.ok, true);
  assert.deepEqual(got.rows.map((r) => [r.scenario, r.name, r.term, r.year]), SCENARIOS.map((s) =>
    [s, s[0].toUpperCase() + s.slice(1), `${first.out[s].pct_term.toFixed(1)}%`, `${first.out[s].pct_year.toFixed(1)}%`]));
  assert.equal(got.usd, 100000);
  assert.equal(got.label, LABEL);
});

test("the two numbers of an estimate cannot be read as one: the whole term, and the same per year", () => {
  const got = estimate(TABLE, chosen({ months: 24 }));
  assert.deepEqual(got.headers, ["For the whole term, 24 months", "The same, per year"]);
  // 29.0% for the 24 months is 14.5% a year: read as a yearly rate, the first number would be twice the truth.
  assert.deepEqual(got.rows.map((r) => [r.name, r.term, r.year]), [["Bad", "7.5%", "3.8%"], ["Base", "29.0%", "14.5%"], ["Good", "65.8%", "32.9%"]]);
  assert.equal(termWords(24), "The first number is for all 24 months together. The second is the same result as a rate per year: "
    + "it is not a second return and is not added to the first.");
  assert.equal(scaleWords(100000), "Computed for a pool of $100,000: on this testnet 100 USDC stand for $100,000, so the amount is "
    + "taken 1,000 times over.");
  assert.equal(TEXT.label, "Model estimate — not a promise");
  const view = text("../views/poolform.js");
  assert.match(view, /const list = \(header, pick\) => `<h4>\$\{esc\(header\)\}<\/h4>\$\{result\.rows\.map\(\(r\) => row\(r\.name, esc\(pick\(r\)\)\)\)\.join\(""\)\}`;/);
  assert.match(view, /\$\{list\(result\.headers\[0\], \(r\) => r\.term\)\}\$\{list\(result\.headers\[1\], \(r\) => r\.year\)\}\s+<p class="small">\$\{esc\(termWords\(result\.months\)\)\}<\/p>\s+<p class="small muted">\$\{esc\(scaleWords\(result\.usd\)\)\}<\/p>/);
  assert.match(view, /<p class="muted">\$\{esc\(TEXT\.label\)\}<\/p>/);
});

test("the form shows a number only where the model has one, and otherwise says what is missing or outside", () => {
  // As the form opens: what the creator still has to give, who invests first.
  const empty = estimate(TABLE, defaults());
  assert.equal(empty.ok, false);
  assert.deepEqual(empty.missing, ["who invests", "the amount", "the term"]);
  assert.deepEqual(empty.outside, []);
  assert.deepEqual(empty.invalid, []);
  // An amount that is not in whole hundreds is told the rule, not that an estimate is missing.
  for (const amount of [250, 150, 50, 100.5]) {
    const got = estimate(TABLE, chosen({ amount }));
    assert.deepEqual([got.ok, got.invalid, got.missing, got.outside, got.rows], [false, [TEXT.amountRule], [], [], undefined], String(amount));
  }
  assert.equal(TEXT.amountRule, "The amount must be a multiple of 100 USDC, from 100.");
  for (const amount of [100, 300, 2500]) assert.equal(estimate(TABLE, chosen({ amount })).ok, true, String(amount));
  assert.match(text("../views/poolform.js"), /<p><strong>\$\{esc\(result\.invalid\.length \? result\.invalid\.join\(" "\) : TEXT\.none\)\}<\/strong><\/p>/);
  const outside = (over, words) => {
    const got = estimate(TABLE, chosen(over));
    assert.equal(got.ok, false, JSON.stringify(over));
    assert.equal(got.rows, undefined);
    assert.deepEqual(got.outside, [words], JSON.stringify(over));
    assert.deepEqual(got.missing, []);
    assert.deepEqual(got.invalid, []);
  };
  // The model has one leverage for the exam and the funded account after it: two different ones have no estimate.
  outside({ examLeverage: 4 }, "the same leverage in the exam and on the funded account");
  outside({ fundedLeverage: 3 }, "the same leverage in the exam and on the funded account");
  outside({ examLeverage: 12, fundedLeverage: 12 }, "a leverage from 1× to 10×");
  outside({ examLeverage: 0.5, fundedLeverage: 0.5 }, "a leverage from 1× to 10×");
  // Any one leverage from 1x to 10x has its estimate, and it is the model's for that leverage.
  for (const leverage of [1, 1.2, 2.3, 4, 10]) {
    const got = estimate(TABLE, chosen({ examLeverage: leverage, fundedLeverage: leverage }));
    const want = quote(TABLE, { pool_usd: 100000, months: 12, mode: "closed", leverage });
    assert.deepEqual(got.rows.map((r) => r.year), SCENARIOS.map((s) => `${want[s].pct_year.toFixed(1)}%`), String(leverage));
  }
  assert.notDeepEqual(estimate(TABLE, chosen({ examLeverage: 1, fundedLeverage: 1 })).rows, estimate(TABLE, chosen()).rows);
  outside({ examDrawdown: 8 }, "an exam drawdown of 6%");
  outside({ examDaily: 4 }, "a daily loss of 3% in the exam");
  outside({ fundedDaily: 2 }, "a daily loss of 3% on the funded account");
  outside({ stepDays: 45 }, "30 days for a step");
  outside({ price: 2 }, "an exam priced at 1% of the seat");
  outside({ steps: [12] }, "an exam of one step of 10% or 8%, or of two steps, 10% then 5% or 8% then 5%");
  outside({ steps: [10, 5, 5] }, "an exam of one step of 10% or 8%, or of two steps, 10% then 5% or 8% then 5%");
  outside({ fundedDrawdown: 7 }, "a funded drawdown of 3, 4, 5, 6, 8 or 10%");
  outside({ target: 12 }, "a funded target of 5, 8, 10, 15 or 20%");
  outside({ share: 95 }, "a trader's share from 50% to 90%");
  outside({ work: 95 }, "a share in work from 50% to 90%");
  outside({ months: 5 }, "a term of 6 to 24 whole months");
  outside({ months: 25 }, "a term of 6 to 24 whole months");
  outside({ months: 12.5 }, "a term of 6 to 24 whole months");
  outside({ investors: "several" }, "one investor: a pool of several is not built");
  // The smallest pool with the smallest share in work still holds the largest seat.
  assert.equal(estimate(TABLE, chosen({ work: 50, amount: 100, sizes: [25] })).ok, true);
  // What is not chosen is missing, never taken as something.
  for (const [over, words] of [[{ afterTarget: null }, "what follows the target"], [{ share: null }, "the trader's share"],
    [{ sizes: [] }, "at least one size on sale"], [{ steps: [10, null] }, "the exam's steps"], [{ price: null }, "the price of an exam"],
    [{ fundedLeverage: null }, "the funded account's leverage"]]) {
    const got = estimate(TABLE, chosen(over));
    assert.equal(got.ok, false);
    assert.deepEqual(got.missing, [words]);
  }
  // With no target the target field is not asked about: the trader stops the account.
  assert.equal(estimate(TABLE, chosen({ afterTarget: "self", target: 33 })).ok, true);
  assert.equal(estimate(TABLE, chosen({ afterTarget: "self", target: null })).ok, true);
  // What the model takes no account of does not take the estimate away, and does not change it.
  const plain = estimate(TABLE, chosen());
  assert.deepEqual(estimate(TABLE, chosen({ assets: [0, 1], idleDays: 3, tradingDays: 5, fillCost: 0, fillDelay: 2, fillWindow: 50 })), plain);
  assert.match(TEXT.fixed, /^The model holds fixed: an exam drawdown of 6%, 3% a day, 30 days a step, an exam priced at 1% of the seat\. It takes no account of the assets,/);
});

test("the draft sends nothing, says what the numbers are and are not, and shows the creator no flag about buyers", () => {
  const view = text("../views/poolform.js");
  // The button is there and cannot be pressed, and nothing is wired to it.
  assert.match(view, /<button type="button" id="send" disabled>\$\{esc\(TEXT\.send\)\}<\/button>/);
  assert.match(view, /<p class="small muted">\$\{esc\(TEXT\.unsent\)\}<\/p>/);
  assert.doesNotMatch(view, /#send"[^\n]*addEventListener|chain\.write|createPool|connect\(/);
  assert.doesNotMatch(view, /import \* as chain/);
  assert.equal(TEXT.unsent, "Not connected: the contracts are drafts and are not deployed, so there is nothing to send to.");
  assert.equal(TEXT.draft, "A draft. The contracts this form is for are designs and drafts, none is deployed, and the form sends nothing.");
  // The words beside the numbers, as approved on 14 October: the label, the footnote under the numbers, what the
  // result depends on, the line at every leverage, what the creator confirms, and the last line.
  assert.equal(TEXT.label, "Model estimate — not a promise");
  assert.equal(TEXT.footnote, "This is a model estimate, not a promised or guaranteed return. It is not based on live trading with real "
    + "money yet. Your actual result can be below the lower number and can be a loss.");
  assert.deepEqual(TEXT.factors, ["how many traders buy challenges in your pool", "how they trade and how many pass",
    "market moves: on a sharp day a stop can fill beyond the drawdown line",
    "the terms you set: stricter terms may attract fewer traders than the estimate assumes",
    "the term and the size of the pool: the shorter and the smaller, the more chance decides",
    "the exchange and the network working as expected"]);
  assert.equal(LEVERAGE_RISK, "Higher leverage carries the risk of larger losses.");
  assert.deepEqual(FIELDS.filter((f) => f.note).map((f) => [f.id, f.note]), [["examLeverage", LEVERAGE_RISK], ["fundedLeverage", LEVERAGE_RISK]]);
  assert.equal(TEXT.confirmHeading, "By creating the pool you confirm:");
  assert.deepEqual(TEXT.confirm, [
    "The percentage is an estimate, not a guarantee; you may get back less than you put in.",
    "Your deposit is locked until the end of the term you chose; there is no early withdrawal; deposits are in stablecoin only.",
    "Funded traders trade with the pool's money: their profit is shared with them at the share you set, their loss stays with the pool.",
    "You chose the pool's terms yourself.",
    "At the end of the term positions are closed and profit is split; 14 days before the end you can extend the pool or request a withdrawal.",
    "Platform fees — 0.5 % of each challenge price and 0.5 % of a withdrawal — are already in the estimate."]);
  assert.equal(TEXT.last, "Remember: you alone are responsible for your money.");
  // Where they stand: the label above the numbers, the footnote and the factors under them, the line under each
  // leverage, the confirmation with its checkbox before the button, and the last line after it, large.
  assert.match(view, /<h3>\$\{esc\(TEXT\.estimate\)\}<\/h3>\s+<p class="muted">\$\{esc\(TEXT\.label\)\}<\/p>\s+<div id="estimate">/);
  assert.match(view, /<div id="estimate"><p class="muted">Loading…<\/p><\/div>\s+<p class="small" id="footnote">\$\{esc\(TEXT\.footnote\)\}<\/p>\s+<p class="small">\$\{esc\(TEXT\.factorsHeading\)\}<\/p>\s+<div class="small">\$\{list\(TEXT\.factors\)\}<\/div>/);
  assert.match(view, /\$\{f\.note\s+\? `<p class="small">\$\{esc\(f\.note\)\}<\/p>` : ""\}/);
  assert.match(view, /<label class="check"><input type="checkbox" id="confirm"> \$\{esc\(TEXT\.confirmHeading\)\}<\/label>\s+\$\{list\(TEXT\.confirm\)\}\s+<\/fieldset>\s+<p><button type="button" id="send" disabled>/);
  assert.match(view, /<p class="small muted">\$\{esc\(TEXT\.unsent\)\}<\/p>\s+<p class="lead"><strong>\$\{esc\(TEXT\.last\)\}<\/strong><\/p>\s+<\/form>/);
  assert.doesNotMatch(view, /id="risks"/);
  // The worst day at a leverage is not given as a number: nothing the form says names it, and the line at the
  // leverage has no number in it.
  const said = [...Object.values(TEXT).flat(), ...FIELDS.map((f) => f.note)];
  assert.ok(!said.some((s) => /worst/i.test(String(s))), "no worst day");
  assert.doesNotMatch(LEVERAGE_RISK, /\d/);
  // The form opens filled, so there is no button that fills it.
  assert.doesNotMatch(view, /id="example"|withExample/);
  // The model also says what a buyer with no skill costs the pool. The creator is not shown it.
  assert.doesNotMatch(view, /gambler/);
  assert.doesNotMatch(text("../lib/poolform.js"), /gambler/);
  // Who invests comes first, and the rest opens once it is chosen; several investors cannot be chosen.
  assert.match(view, /<fieldset><legend>1\. Who invests<\/legend>/);
  assert.match(view, /name="investors" value="\$\{esc\(o\.id\)\}"\$\{o\.built \? "" : " disabled"\}>/);
  assert.match(view, /for \(const later of form\.querySelectorAll\("fieldset\.later"\)\) later\.disabled = values\.investors === null;/);
  assert.equal((view.match(/<fieldset class="later" disabled>/g) || []).length, 3);
  // No page links to the draft; it is reached by its address.
  assert.match(text("../app.js"), /\[\/\^#\\\/new\\\/many\$\/, \(m, page\) => poolFormView\(page\)\],/);
  assert.doesNotMatch(text("../index.html"), /new\/many/);
  for (const page of ["pools.js", "pool.js", "economics.js", "shared.js"]) assert.doesNotMatch(text(`../views/${page}`), /new\/many/, page);
});
