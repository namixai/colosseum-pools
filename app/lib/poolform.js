// The form that would create a pool of many traders: what its creator sets, and what the model says such a pool
// may return. A draft: the contracts it is for are designs and drafts, none is deployed, and the form sends nothing.
//
// The settings, their bounds and their defaults are the table of the design "what the creator of a pool sets"
// (docs/POOL-PARAMETERS.md in the draft that carries the exam contract). A default is what the form starts with.
//
// The creator is to see the percentage at once, so the form opens filled. Three things have no value to start
// from and are the creator's first choices: who invests, the amount and the term. For six settings the design's
// table gives no default either, and the form starts them where the model was run, so that an estimate stands
// beside the form as soon as the amount and the term are in: an exam priced at 1% of the seat; a funded drawdown
// of 6%, a daily loss of 3% and 5x leverage, which are the exam's; a trader's share of 80%; and an account that
// closes at its target. Each is the creator's to change.
//
// The estimate is app/lib/poolquote.js. It exists for some values and no others, and for the rest this says
// "no estimate" and why, never a number. No browser globals here, so node's test runner loads it.
import { quote, EXAMS, DRAWDOWNS, TARGETS, MONTHS, SCENARIOS, LABEL, NO_ESTIMATE, PRICE_PCT, EXAM_DRAWDOWN, LEVERAGE as MODEL_LEVERAGE }
  from "./poolquote.js";

/**
 * On this testnet 100 USDC of a pool stand for $100 000 of one: the model's buyers, sizes and fees are in dollars
 * of that scale, so the estimate is computed for the amount taken a thousand times over.
 */
export const SCALE = 1000;
/** The sizes of seat a pool may put on sale, in USDC of this testnet. */
export const SIZES = Object.freeze([5, 10, 25]);
export const NOTICE_DAYS = 14;
/** The creator sets the leverage from 1x to 10x; the form starts at 5x. */
export const LEVERAGE = Object.freeze({ min: 1, max: 10, start: 5 });
/** What the model holds fixed: an estimate exists only with these. The leverage is not among them. */
export const MODEL = Object.freeze({ examDrawdown: 100 * EXAM_DRAWDOWN, daily: 3, stepDays: 30, price: 100 * PRICE_PCT });

const field = (id, section, label, start, min, max, step = 1, note = "") => Object.freeze({ id, section, label, start, min, max, step, note });
/** Said beside each choice of leverage. The worst day is not given as a number: the line says what leverage risks. */
export const LEVERAGE_RISK = "Higher leverage carries the risk of larger losses.";

/** The numbers the creator types. `start: null` is a field with nothing to start from: the creator's to fill. */
export const FIELDS = Object.freeze([
  field("amount", "pool", "Amount, USDC", null, 100, null, 100),
  field("months", "pool", "Term, months", null, MONTHS[0], MONTHS[1]),
  field("work", "pool", "Share of the pool in work, %", 80, 1, 100),
  field("price", "exam", "Price of an exam, % of the seat", MODEL.price, 0.01, 100, 0.01),
  field("examDrawdown", "exam", "Drawdown, %", 6, 0.01, 99.99, 0.01),
  field("examDaily", "exam", "Daily loss, %", 3, 0.01, 99.99, 0.01),
  field("examLeverage", "exam", "Leverage, ×", LEVERAGE.start, LEVERAGE.min, LEVERAGE.max, 0.1, LEVERAGE_RISK),
  field("stepDays", "exam", "Time for a step, days (0 is no deadline)", 30, 0, null),
  field("idleDays", "exam", "Time a step may go without a fill, days (0 is no limit)", 14, 0, null),
  field("tradingDays", "exam", "Trading days a step needs", 0, 0, null),
  field("fillCost", "exam", "Cost of a fill, millionths of the notional", 950, 0, 100000),
  field("fillDelay", "exam", "Blocks before a fill", null, 1, 600),
  field("fillWindow", "exam", "Blocks a fill has", null, 1, 600),
  field("target", "funded", "Target, %", 10, 0.01, 100, 0.01),
  field("fundedDrawdown", "funded", "Drawdown, %", MODEL.examDrawdown, 0.01, 99.99, 0.01),
  field("fundedDaily", "funded", "Daily loss, %", MODEL.daily, 0.01, 99.99, 0.01),
  field("fundedLeverage", "funded", "Leverage, ×", LEVERAGE.start, LEVERAGE.min, LEVERAGE.max, 0.1, LEVERAGE_RISK),
  field("share", "funded", "Trader's share of the profit, %", 80, 0, 100),
]);

/** Who invests. The shared pool, of several investors, has no contract yet: it is shown and cannot be chosen. */
export const INVESTORS = Object.freeze([
  Object.freeze({ id: "one", label: "One investor", built: true, note: "" }),
  Object.freeze({ id: "several", label: "Shared pool", built: false,
    note: "Being built: the contract for a pool of several investors is being written, so this cannot be chosen yet." }),
]);
/** What follows the target of a funded account. The form starts with the first: the account closes. */
export const AFTER_TARGET = Object.freeze([
  Object.freeze({ id: "closed", label: "The account closes" }),
  Object.freeze({ id: "stays", label: "The account stays" }),
  Object.freeze({ id: "self", label: "The trader stops it" }),
]);
/** The end of the term. Not extended is where the form starts. */
export const AT_END = Object.freeze([
  Object.freeze({ id: "stop", label: "Not extended", withdraw: "all" }),
  Object.freeze({ id: "extend", label: "Extended", withdraw: "profit" }),
]);
export const MAX_STEPS = 4;

/** What the form starts with. */
export function defaults() {
  const values = { investors: null, sizes: [...SIZES], atEnd: "stop", steps: [10], assets: [], afterTarget: AFTER_TARGET[0].id };
  for (const f of FIELDS) values[f.id] = f.start;
  return values;
}

const isNumber = (x) => typeof x === "number" && Number.isFinite(x);
const near = (a, b) => Math.abs(a - b) < 1e-9;
const node = (list, percent) => list.find((x) => near(100 * x, percent));
const examOf = (steps) => Object.keys(EXAMS).find((key) => EXAMS[key].length === steps.length
  && EXAMS[key].every((x, i) => near(100 * x, steps[i])));

/** The fields whose value is outside the bounds the design gives the contracts: `[{ id, label }]`. */
export function outOfBounds(values) {
  return FIELDS.filter((f) => {
    const v = values[f.id];
    if (v === null || v === undefined) return false;
    return !isNumber(v) || v < f.min || (f.max !== null && v > f.max);
  }).map(({ id, label }) => ({ id, label }));
}

/**
 * What the model is asked for these values: `{ args }` for quote(), or `{ invalid, missing, outside }`. `invalid` is
 * what the form itself does not take, said as the rule it breaks; `missing` is what the creator has not chosen
 * yet; `outside` is what they chose that the model has no estimate for, each in words.
 */
export function modelArgs(values) {
  const invalid = [];
  const missing = [];
  const outside = [];
  const need = (ok, words) => {
    if (!ok) missing.push(words);
    return ok;
  };
  const within = (ok, words) => {
    if (!ok) outside.push(words);
    return ok;
  };
  const v = values;
  if (need(v.investors !== null, "who invests")) within(v.investors === "one", "one investor: a pool of several is not built");
  // An amount that is not in whole hundreds is not a setting without an estimate: the form does not take it.
  if (need(isNumber(v.amount), "the amount") && !(v.amount >= 100 && near(v.amount % 100, 0))) invalid.push(TEXT.amountRule);
  if (need(isNumber(v.months), "the term")) {
    within(Number.isInteger(v.months) && v.months >= MONTHS[0] && v.months <= MONTHS[1], `a term of ${MONTHS[0]} to ${MONTHS[1]} whole months`);
  }
  const sizes = (v.sizes || []).filter((s) => SIZES.includes(s));
  need(sizes.length > 0 && sizes.length === (v.sizes || []).length, "at least one size on sale");
  if (need(isNumber(v.work), "the share of the pool in work")) within(v.work >= 50 && v.work <= 90, "a share in work from 50% to 90%");
  if (need(isNumber(v.price), "the price of an exam")) within(near(v.price, MODEL.price), `an exam priced at ${MODEL.price}% of the seat`);
  const steps = v.steps || [];
  const exam = steps.every(isNumber) ? examOf(steps) : undefined;
  if (need(steps.length > 0 && steps.every(isNumber), "the exam's steps")) {
    within(Boolean(exam), "an exam of one step of 10% or 8%, or of two steps, 10% then 5% or 8% then 5%");
  }
  if (need(isNumber(v.examDrawdown), "the exam's drawdown")) within(near(v.examDrawdown, MODEL.examDrawdown), `an exam drawdown of ${MODEL.examDrawdown}%`);
  if (need(isNumber(v.examDaily), "the exam's daily loss")) within(near(v.examDaily, MODEL.daily), `a daily loss of ${MODEL.daily}% in the exam`);
  // The model has one leverage for an exam and the funded account after it: an estimate exists when the two are one.
  const leverage = need(isNumber(v.examLeverage), "the exam's leverage")
    && within(v.examLeverage >= MODEL_LEVERAGE[0] && v.examLeverage <= MODEL_LEVERAGE[1], "a leverage from 1× to 10×")
    ? v.examLeverage : undefined;
  if (need(isNumber(v.stepDays), "the time for a step")) within(near(v.stepDays, MODEL.stepDays), `${MODEL.stepDays} days for a step`);
  need(v.afterTarget !== null && AFTER_TARGET.some((a) => a.id === v.afterTarget), "what follows the target");
  const drawdown = isNumber(v.fundedDrawdown) ? node(DRAWDOWNS, v.fundedDrawdown) : undefined;
  if (need(isNumber(v.fundedDrawdown), "the funded account's drawdown")) {
    within(drawdown !== undefined, "a funded drawdown of 3, 4, 5, 6, 8 or 10%");
  }
  if (need(isNumber(v.fundedDaily), "the funded account's daily loss")) within(near(v.fundedDaily, MODEL.daily), `a daily loss of ${MODEL.daily}% on the funded account`);
  if (need(isNumber(v.fundedLeverage), "the funded account's leverage") && leverage !== undefined) {
    within(near(v.fundedLeverage, leverage), "the same leverage in the exam and on the funded account");
  }
  let target;
  if (v.afterTarget !== "self" && need(isNumber(v.target), "the funded account's target")) {
    target = node(TARGETS, v.target);
    within(target !== undefined, "a funded target of 5, 8, 10, 15 or 20%");
  }
  if (need(isNumber(v.share), "the trader's share")) within(v.share >= 50 && v.share <= 90, "a trader's share from 50% to 90%");
  const end = AT_END.find((a) => a.id === v.atEnd);
  need(Boolean(end), "what happens at the end of the term");
  if (invalid.length || missing.length || outside.length) return { invalid, missing, outside };
  return { args: { pool_usd: v.amount * SCALE, months: v.months, mode: v.afterTarget, exam, drawdown, target: target ?? TARGETS[2],
    share: v.share / 100, work: v.work / 100, menu: sizes.map((s) => s * SCALE), leverage, withdraw: end.withdraw } };
}

const NAMES = Object.freeze({ bad: "Bad", base: "Base", good: "Good" });
const percent = (x) => `${x.toFixed(1)}%`;

/**
 * The estimate for these values: `{ ok: true, months, usd, rows, headers, label }`, each row
 * `{ scenario, name, term, year }`, or `{ ok: false, invalid, missing, outside }`. The two numbers of a row are never one
 * sum: `term` is for all the months together, `year` is that brought to one year.
 */
export function estimate(tables, values) {
  const asked = modelArgs(values);
  if (!asked.args) return { ok: false, invalid: asked.invalid, missing: asked.missing, outside: asked.outside };
  let out;
  try {
    out = quote(tables, asked.args);
  } catch (err) {
    if (err?.code !== NO_ESTIMATE) throw err;
    return { ok: false, invalid: [], missing: [], outside: [err.message] };
  }
  const months = asked.args.months;
  return {
    ok: true, months, usd: asked.args.pool_usd, label: LABEL,
    headers: [`For the whole term, ${months} months`, "The same, per year"],
    rows: SCENARIOS.map((scenario) => ({ scenario, name: NAMES[scenario], term: percent(out[scenario].pct_term), year: percent(out[scenario].pct_year) })),
  };
}

/** The words under the estimate that keep its two numbers apart. */
export function termWords(months) {
  return `The first number is for all ${months} months together. The second is the same result as a rate per year: `
    + "it is not a second return and is not added to the first.";
}

/** The words that say what amount the estimate was computed for. */
export function scaleWords(usd) {
  return `Computed for a pool of $${usd.toLocaleString("en-US")}: on this testnet 100 USDC stand for $100,000, so the amount is `
    + `taken ${SCALE.toLocaleString("en-US")} times over.`;
}

export const TEXT = Object.freeze({
  title: "A pool of many traders",
  draft: "A draft. The contracts this form is for are designs and drafts, none is deployed, and the form sends nothing.",
  lead: "You set the pool's conditions once, when you create it. They are public before anyone deposits or buys, "
    + "and they do not change afterwards.",
  estimate: "What the pool may return to its investor",
  label: "Model estimate — not a promise",
  footnote: "This is a model estimate, not a promised or guaranteed return. It is not based on live trading with real money yet. "
    + "Your actual result can be below the lower number and can be a loss.",
  factorsHeading: "What the result depends on:",
  factors: Object.freeze([
    "how many traders buy challenges in your pool",
    "how they trade and how many pass",
    "market moves: on a sharp day a stop can fill beyond the drawdown line",
    "the terms you set: stricter terms may attract fewer traders than the estimate assumes",
    "the term and the size of the pool: the shorter and the smaller, the more chance decides",
    "the exchange and the network working as expected",
  ]),
  confirmHeading: "By creating the pool you confirm:",
  confirm: Object.freeze([
    "The percentage is an estimate, not a guarantee; you may get back less than you put in.",
    "Your deposit is locked until the end of the term you chose; there is no early withdrawal; deposits are in stablecoin only.",
    "Funded traders trade with the pool's money: their profit is shared with them at the share you set, their loss stays with the pool.",
    "You chose the pool's terms yourself.",
    "At the end of the term positions are closed and profit is split; 14 days before the end you can extend the pool or request a withdrawal.",
    "Platform fees — 0.5 % of each challenge price and 0.5 % of a withdrawal — are already in the estimate.",
  ]),
  last: "Remember: you alone are responsible for your money.",
  none: "No estimate.",
  amountRule: "The amount must be a multiple of 100 USDC, from 100.",
  missing: "Still to choose:",
  outside: "The model has an estimate only for:",
  fixed: `The model holds fixed: an exam drawdown of ${MODEL.examDrawdown}%, ${MODEL.daily}% a day, ${MODEL.stepDays} days a step, `
    + `an exam priced at ${MODEL.price}% of the seat. It takes no account of the assets, of the time a step `
    + "may go without a fill, of the trading days a step needs, of the cost of a fill or of the blocks before a fill.",
  send: "Create the pool",
  unsent: "Not connected: the contracts are drafts and are not deployed, so there is nothing to send to.",
  notice: `Notice before the term ends: ${NOTICE_DAYS} days, fixed.`,
  atEnd: "Not extended: every position is closed, the profit is shared and the pool stops. Extended: the pool goes on with the "
    + "same amount and the profit is withdrawn.",
  afterTarget: "Stays: the account goes for the target again after each payout. The trader stops it: there is no target, and the "
    + "target above is not used.",
  size: "Size of a funded account: the size of its exam.",
  assets: "Assets, for the exam and for the funded account",
  noAssets: "No assets are chosen. The estimate does not depend on them.",
});
