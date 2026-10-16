// An indicative percentage for the investor of a pool of many traders: the pool's settings, its amount and its
// term of 6 to 24 months give three numbers, for a bad, a base and a good run of buyers. A model, not a promise.
//
// The arithmetic is our finance model's, carried here line by line from its Python source and held to it by nineteen
// cases of inputs and outputs, to the sixth decimal (app/tests/poolquote.test.mjs). The table it reads,
// app/data/pool-quote-units-2026-10-13.json, is a Monte Carlo of how five kinds of trader do in an exam and on a
// funded account: 30 000 paths a cell, seed 20261013.
//
// What the model holds fixed, and so what an estimate exists for: an exam drawdown of 6%, 3% a day, 30 days a step,
// an exam priced at 1% of the seat, the platform's fees (0.5% of the price, 0.5% of a withdrawal, the builder fee
// of 0.05%), and a seat that is held from the moment its exam is bought. For anything else there is no estimate,
// and quote() says so instead of returning a number.
//
// The leverage may be anything from 1x to 10x. In the model it only caps how far an account can swing in a day,
// and that cap binds below about 2.4x: the table has stages for 1x, 1.5x and 2x, every leverage from 2.5x up is
// one and the same stage, and between those nodes the estimate is a straight line.
//
// No browser globals and no library here, so node's test runner loads it.

export const SCENARIOS = Object.freeze(["bad", "base", "good"]);
/** Buyers a month for every $100 000 of the pool. */
export const DEMAND = Object.freeze({ bad: 5.0, base: 15.0, good: 40.0 });
/** Who wants which size; a size that is not on the menu loses its buyers. */
export const MIX = Object.freeze({ 5000: 0.55, 10000: 0.15, 25000: 0.30 });
export const STEP = 5000;
export const PRICE_PCT = 0.01;
export const RATE_CHALLENGE = 0.005;
export const RATE_WITHDRAWAL = 0.005;
export const RATE_BUILDER = 0.0005;
/** USDC the exchange takes for a new account. */
export const ACCOUNT_FEE = 1.0;
export const DAYS_PER_MONTH = 365.0 / 12.0;
export const EXAM_DRAWDOWN = 0.06;
export const EXAMS = Object.freeze({ "1x10": [0.10], "1x8": [0.08], "2x10+5": [0.10, 0.05], "2x8+5": [0.08, 0.05] });
export const MODES = Object.freeze(["closed", "stays", "self"]);
export const DRAWDOWNS = Object.freeze([0.03, 0.04, 0.05, 0.06, 0.08, 0.10]);
export const TARGETS = Object.freeze([0.05, 0.08, 0.10, 0.15, 0.20]);
export const MONTHS = Object.freeze([6, 24]);
/** The leverages the table holds stages for. 2.5 stands for every leverage up to 10x. */
export const LEVERAGES = Object.freeze([1.0, 1.5, 2.0, 2.5]);
export const LEVERAGE = Object.freeze([1.0, 10.0]);
export const LABEL = "a model, not a promise";

/** What quote() throws for settings the model has no estimate for. */
export const NO_ESTIMATE = "NO_ESTIMATE";
const refuse = (message) => Object.assign(new Error(message), { code: NO_ESTIMATE });

const sum = (list) => list.reduce((total, x) => total + x, 0);

/** Share of the buyers of each size that find no room. classes: [units of STEP, offered load in seats]. */
export function kaufmanRoberts(capacity, classes) {
  const q = [1.0, ...new Array(capacity).fill(0.0)];
  for (let n = 1; n <= capacity; n++) {
    q[n] = sum(classes.filter(([b]) => n >= b).map(([b, a]) => a * b * q[n - b])) / n;
  }
  const total = sum(q);
  return classes.map(([b]) => sum(q.slice(capacity - b + 1)) / total);
}

/**
 * One purchase by the model's traders, as shares of the seat: what it costs the pool, what the trader is paid, how
 * long the seat is held, how much is really traded. `node` is a leverage of the table.
 */
export function unit(tables, scenario, { exam, drawdown, target, mode, share, node = LEVERAGES[LEVERAGES.length - 1] }) {
  const sc = tables.scenarios[scenario];
  const at = node >= LEVERAGES[LEVERAGES.length - 1] ? sc : tables.leverage[node.toFixed(2)][scenario];
  const turnover = tables.meta.turnover;
  const stage = at.funded[`${drawdown.toFixed(2)}|${mode === "self" ? "self" : target.toFixed(2)}`];
  const u = { p_pass: 0.0, p_paid: 0.0, cost: 0.0, payout: 0.0, days_exam: 0.0, days_funded: 0.0, notional: 0.0 };
  sc.weights.forEach((w, i) => {
    let p = 1.0;
    let d = 0.0;
    for (const step of EXAMS[exam]) {
      const [hit, days] = at.exam[step.toFixed(2)][i];
      d += p * days;
      p *= hit;
    }
    const [gross, gain, hit, paid, days] = stage[i];
    const k = mode === "stays" ? 1.0 / (1.0 - hit) : 1.0;      // the stage again after every payout
    u.p_pass += w * p;
    u.p_paid += w * p * paid;
    u.cost += w * p * k * (share * gain - gross);
    u.payout += w * p * k * share * gain;
    u.days_exam += w * d;
    u.days_funded += w * p * k * days;
    u.notional += w * p * k * days * turnover[i];
  });
  return u;
}

/** One purchase at any leverage from 1x to 10x: a node of the table, or a straight line between two nodes. */
export function atLeverage(tables, scenario, leverage, settings) {
  const top = LEVERAGES[LEVERAGES.length - 1];
  if (leverage >= top) return unit(tables, scenario, { ...settings, node: top });
  const low = Math.max(...LEVERAGES.filter((x) => x <= leverage));
  const a = unit(tables, scenario, { ...settings, node: low });
  if (low === leverage) return a;
  const high = Math.min(...LEVERAGES.filter((x) => x > leverage));
  const b = unit(tables, scenario, { ...settings, node: high });
  const w = (leverage - low) / (high - low);
  return Object.fromEntries(Object.keys(a).map((k) => [k, (1.0 - w) * a[k] + w * b[k]]));
}

/**
 * The buyer with no skill: how often he can pass at most, what price of the exam covers him, what the pool keeps
 * of a 1% price. Per cent of the seat.
 */
export function gambler({ exam, drawdown, target, mode, share }) {
  let p = 1.0;
  for (const step of EXAMS[exam]) p *= EXAM_DRAWDOWN / (step + EXAM_DRAWDOWN);
  const paid = share * (mode === "closed" ? (target * drawdown) / (target + drawdown) : drawdown);
  return { pass_max_pct: 100 * p, need_price_pct: 100 * p * paid, keeps_pct: 100 * (PRICE_PCT - p * paid), ok: p * paid <= PRICE_PCT };
}

/** A pool of `pool_usd` over `months` when one purchase is `u`. */
export function settle(u, scenario, { pool_usd, months, work, menu, withdraw = "all" }) {
  const capacity = Math.trunc((work * pool_usd) / STEP + 1e-9);
  const buyers = (DEMAND[scenario] * pool_usd) / 100000.0;
  const sizes = [...menu].sort((a, b) => a - b);
  const held = u.days_exam + u.days_funded;
  const block = kaufmanRoberts(capacity, sizes.map((s) => [Math.floor(s / STEP), (buyers * MIX[s] * held) / DAYS_PER_MONTH]));
  const t = months / 12.0;
  const sold = sizes.map((s, i) => 12.0 * t * buyers * MIX[s] * (1.0 - block[i]));
  const seats = sum(sold.map((n, i) => n * sizes[i]));
  const price = PRICE_PCT * seats;
  const gross = price - u.cost * seats - sum(sold) * (u.p_pass - u.p_paid) * ACCOUNT_FEE;
  const fee_price = RATE_CHALLENGE * price;
  const fee_withdrawal = RATE_WITHDRAWAL * ((withdraw === "all" ? pool_usd : 0.0)
    + Math.max(gross - fee_price, withdraw === "profit" ? 0.0 : -pool_usd));
  const net = gross - fee_price - fee_withdrawal;
  const builder = RATE_BUILDER * u.notional * seats;
  const wanted = 12.0 * t * buyers * sum(sizes.map((s) => MIX[s]));
  return { pct_term: (100 * net) / pool_usd, pct_year: (100 * net) / pool_usd / t, usd_term: net, pass_pct: 100 * u.p_pass,
    sold: sum(sold), no_seat_pct: 100 * (1.0 - sum(sold) / wanted), traders_pay: price, traders_get: u.payout * seats,
    platform: fee_price + fee_withdrawal + builder, builder };
}

/**
 * The indicative percentage for the investor: `{ bad, base, good, gambler, label }`. Throws an error whose `code` is
 * NO_ESTIMATE for settings the model does not cover: it refuses, it does not compute.
 */
export function quote(tables, { pool_usd, months, mode, exam = "1x10", drawdown = 0.06, target = 0.10, share = 0.80, work = 0.80,
  menu = [5000, 10000, 25000], leverage = 5.0, withdraw = "all" }) {
  if (!MODES.includes(mode) || !Object.hasOwn(EXAMS, exam) || !DRAWDOWNS.includes(drawdown) || (mode !== "self" && !TARGETS.includes(target))) {
    throw refuse("a setting the table does not hold");
  }
  if (!(MONTHS[0] <= months && months <= MONTHS[1]) || !(0.5 <= share && share <= 0.9) || !(0.5 <= work && work <= 0.9)
    || !["all", "profit"].includes(withdraw)) {
    throw refuse("a term of 6 to 24 months, a trader's share and a share in work of 50 to 90%");
  }
  if (!(LEVERAGE[0] <= leverage && leverage <= LEVERAGE[1])) throw refuse("a leverage from 1x to 10x");
  if (!menu.length || menu.some((s) => !Object.hasOwn(MIX, s)) || pool_usd < 100000 || pool_usd % 100 || Math.max(...menu) > work * pool_usd) {
    throw refuse("a pool from $100 000 in whole hundreds; a menu out of 5, 10 and 25 thousand");
  }
  const out = {};
  for (const s of SCENARIOS) {
    out[s] = settle(atLeverage(tables, s, leverage, { exam, drawdown, target, mode, share }), s,
      { pool_usd, months, work, menu: [...menu], withdraw });
  }
  out.gambler = gambler({ exam, drawdown, target, mode, share });
  out.label = LABEL;
  return out;
}
