// The pool calculator: closed formulas, no simulation. What the Economics page runs in the browser.
//
//   import { evaluate, defaultLayout } from "./calc.js";
//   const tables = await (await fetch("../data/calc_tables.json")).json();
//   const r = evaluate(tables, { ...defaultLayout(100000), scenario: "base", mode: "real", asset_list: "default" });
//   r.annual_return, r.groups[0].min_price, r.cascade, r.warnings ...
//
// `tables` is app/data/calc_tables.json: the heavy part of the answer, already reduced to a grid.
// Nothing in this file draws a random number, reads a file or touches the network.
//
// Two different kinds of number live here, and the page has to keep them apart:
//
//   * the returns, the prices and the loads are a MODEL. They were derived from a Monte Carlo
//     model of the contracts in this repository, and their inputs about people -- who buys, how
//     often, how strong the traders are -- are assumptions nobody has checked against real ones.
//   * the cascade block is a MEASUREMENT: one-minute bars of real crash days, replayed through
//     these rules. It is what did happen on one day, not what is expected to happen. It carries
//     no frequency, so it is never subtracted from the returns above.
//
// The model itself is our own tooling and is not published here. What is published is this twin
// of it and the grid it produced, plus the golden cases in app/tests/fixtures/calc_cases.json,
// which app/tests/calc.test.mjs holds this file to at a relative 1e-9.
export const WORST = -Math.log(0.01);       // the 1-in-100 overshoot of a stop, in units of the mean (exponential)
export const FAILS_WARNING = 10;            // a reserve that takes fewer failures in a row than this is flagged
export const MODES = ["real", "demo"];
export const SCENARIOS = ["bad", "base", "good"];

/** Share of buyers lost by `servers` seats with no queue when `offered` = arrival rate x mean holding time. */
export function erlangB(servers, offered) {
  let b = 1.0;
  for (let k = 1; k <= servers; k++) b = (offered * b) / (k + offered * b);
  return b;
}

/** The measured tail of the table (calc_tables.py). A table without it is refused, not guessed: the exponential's thin tail
 *  would pass for the pool's worst case. */
export function tailBlock(tables) {
  if (tables.tail === undefined) throw new Error("the table has no measured tail: run calc_tables.py --tail-only");
  return tables.tail;
}

/** The 1-in-100 overshoot of a stop, in shares of the seat's capital: the measured upper value, or 4.6 x the scenario's mean
 *  where that is larger (a scenario whose mean is beyond 1.7 %). */
export function worstOvershoot(tables, overshootMean) {
  return Math.max(tailBlock(tables).o99.high, WORST * overshootMean);
}

/** The expected annual return once cascades that cost `lossShareOfPool` of the pool come `perYear` times a year. The frequency is
 *  the reader's assumption: the data has one cascade and no rate. */
export function afterCascades(annualReturn, perYear, lossShareOfPool) {
  return annualReturn - perYear * lossShareOfPool;
}

export function cellKey(scenario, mode, maxDrawdown, dailyLoss, target) {
  return `${scenario}|${mode}|${Math.round(maxDrawdown * 1e4)}|${Math.round(dailyLoss * 1e4)}|${Math.round(target * 1e4)}`;
}

/** One cell of the table; a rule off the grid is refused, not guessed (offer the grid in the form). */
export function cell(tables, scenario, mode, maxDrawdown, dailyLoss, target) {
  const k = cellKey(scenario, mode, maxDrawdown, dailyLoss, target);
  const c = tables.cells[k];
  if (c === undefined) throw new Error(`rules off the grid of the table: ${k}`);
  return c;
}

export function bracket(tables, F) {
  const b = tables.demand.brackets;
  return F >= b.L_min ? "L" : F >= b.M_min ? "M" : "S";
}

/** Buyers a month for each group of seats: demand_per_100k for every $90 000 of seats, over the brackets present as the
 *  mix says (renormalised), and inside a bracket by the number of seats. */
export function arrivalsPerMonth(tables, groups, demandPer100k) {
  const seatCapital = groups.reduce((a, g) => a + g.F * g.count, 0);
  const total = (demandPer100k * seatCapital) / tables.demand.reference_seat_capital;
  const mix = tables.demand.mix;
  const names = groups.map((g) => bracket(tables, g.F));
  const weight = {};
  for (const n of names) weight[n] = mix[n];
  const norm = Object.values(weight).reduce((a, b) => a + b, 0);
  const seatsIn = {};
  groups.forEach((g, i) => { seatsIn[names[i]] = (seatsIn[names[i]] ?? 0) + g.count; });
  return groups.map((g, i) => ((total * weight[names[i]]) / norm) * g.count / seatsIn[names[i]]);
}

/** One seat of size F: what the trader pays and gets, and what a sold challenge does to the pool. */
export function seatEconomics(tables, c, F, o) {
  const k = tables.constants;
  const rho = o.mode === "real" ? k.challenge_ratio : 0.0;
  const C = rho * F;
  const nAcct = o.mode === "real" ? 1.0 * k.new_account_fee : c.p_pass * k.new_account_fee;
  const price = o.price_pct * F;
  const fee = o.fee_flat + o.fee_pct * price;
  const poolResult = rho * F * c.g_c + F * c.g_f - o.share * F * c.h_f + c.p_sent * k.new_account_fee - nAcct;
  const cost = -poolResult;
  const o99 = worstOvershoot(tables, o.overshoot_mean);
  const lossC = C * (o.dd + o99) + (o.mode === "real" ? k.new_account_fee : 0.0) - price;
  const lossF = F * (o.dd + o99);
  return {
    F, C, price, fee, trader_pays: price + fee, cost, cost_pct_F: cost / F, margin: price - cost, min_price: cost,
    min_price_pct_F: cost / F, trader_ceiling_at_take_profit: o.share * o.take_profit * F, commit: F + C + k.capital_reserve,
    loss_failed_challenge: lossC, loss_failed_funded: lossF, price_below_cost: price < cost,
  };
}

/** null means no limit: a loss of zero or less does not eat the reserve. */
export function failsInARow(reserve, loss) {
  return loss <= 0 ? null : Math.floor(reserve / loss);
}

/** What a cascade like 10.10.2025 does to this pool, from the stress test's measurements (not a model, not a probability). A case
 *  is a way the seats are spread over the coins of the list. Losses are shares of the seats' capital; the pool's reserve is on top of
 *  the seats, so the share of the pool is smaller. The measurement is at the rules of the stress test (3 % a day, 6 % from the start,
 *  5x): for other rules `rules_match` is false and the numbers are only a guide. `low` is the stop executed at the next open, `high`
 *  at the worst price of the delay window. */
export function cascadeBlock(tables, spec, seatCapital, capital, dd, daily, annualReturn) {
  const t = tailBlock(tables);
  const name = spec.asset_list ?? "default";
  const lists = t.cascade.lists;
  if (lists[name] === undefined) throw new Error(`asset_list '${name}' is not measured: ${Object.keys(lists).sort()}`);
  const reserve = spec.reserve;
  const cases = {};
  for (const [cname, c] of Object.entries(lists[name].cases)) {
    const share = { low: c.open, high: c.worst };
    const each = (fn) => ({ low: fn(share.low), high: fn(share.high) });
    cases[cname] = {
      label: c.label,
      loss_share_of_seats: { ...share },
      loss_usd: each((v) => v * seatCapital),
      loss_share_of_pool: each((v) => (v * seatCapital) / capital),
      multiple_of_rules_cap: each((v) => v / dd),
      loss_beyond_reserve_usd: each((v) => Math.max(v * seatCapital - reserve, 0.0)),
      years_of_base_return: each((v) => (annualReturn > 0 ? (v * seatCapital) / capital / annualReturn : null)),
    };
  }
  const mr = t.rules_measured;
  return {
    asset_list: name, coins: [...lists[name].coins], day: t.cascade.day, cases, rules_cap_usd: dd * seatCapital, reserve, measured_rules: { ...mr },
    rules_match: Math.abs(dd - mr.max_drawdown) < 1e-12 && Math.abs(daily - mr.daily_loss) < 1e-12, note: t.note,
  };
}

/** spec: seats [{F, count}], reserve, scenario, mode, max_drawdown, daily_loss, target, trader_share, price_pct,
 *  fee_pct_price, fee_flat, demand_per_100k (optional: the scenario's own), asset_list ("default" or "wide"). Returns what the
 *  calculator shows. */
export function evaluate(tables, spec) {
  const sc = tables.scenarios[spec.scenario];
  const mode = spec.mode ?? "real";
  const dd = spec.max_drawdown ?? 0.06;
  const daily = spec.daily_loss ?? 0.03;
  const target = spec.target ?? 0.10;
  const share = spec.trader_share ?? 0.80;
  const c = cell(tables, spec.scenario, mode, dd, daily, target);
  const groups = spec.seats.map((g) => ({ ...g }));
  const d = spec.demand_per_100k == null ? sc.demand_per_100k : spec.demand_per_100k;
  const lam = arrivalsPerMonth(tables, groups, d);
  const seatCapital = groups.reduce((a, g) => a + g.F * g.count, 0);
  const capital = seatCapital + spec.reserve;
  const dpm = tables.constants.days_per_month;
  let income = 0, feeIncome = 0, soldTotal = 0, busySeats = 0, committed = 0;
  const nSeats = groups.reduce((a, g) => a + g.count, 0);
  const outGroups = groups.map((g, i) => {
    const l = lam[i];
    const e = seatEconomics(tables, c, g.F, {
      mode, share, price_pct: spec.price_pct ?? 0.01, fee_pct: spec.fee_pct_price ?? 0.0, fee_flat: spec.fee_flat ?? 0.0,
      take_profit: sc.take_profit, dd, overshoot_mean: sc.overshoot_mean,
    });
    const a = (l * c.days) / dpm;
    const blocking = erlangB(g.count, a);
    const sold = 12.0 * l * (1.0 - blocking);
    const carried = a * (1.0 - blocking);
    income += sold * e.margin;
    feeIncome += sold * e.fee;
    soldTotal += sold;
    busySeats += carried;
    committed += carried * e.commit;
    return {
      ...e, count: g.count, arrivals_per_month: l, offered_load: a, blocking, sold_per_year: sold, busy_share: carried / g.count,
      fails_challenge: failsInARow(spec.reserve, e.loss_failed_challenge), fails_funded: failsInARow(spec.reserve, e.loss_failed_funded),
    };
  });
  const o99 = worstOvershoot(tables, sc.overshoot_mean);
  const shock = spec.reserve / seatCapital - dd;      // the overshoot at which every funded seat stopping at once eats the reserve
  const big = outGroups.reduce((a, b) => (b.F > a.F ? b : a));
  const annualReturn = income / capital;
  const cascade = cascadeBlock(tables, spec, seatCapital, capital, dd, daily, annualReturn);
  const warnings = [];
  if (outGroups.some((x) => x.price_below_cost)) warnings.push("price_below_cost");
  if (big.fails_challenge !== null && big.fails_challenge < FAILS_WARNING) warnings.push("reserve_takes_few_failures");
  if (shock < o99) warnings.push("reserve_short_of_a_shock");
  const worstShare = Math.max(...Object.values(cascade.cases).map((c) => c.loss_share_of_seats.high));
  if (worstShare > dd) warnings.push("cascade_beyond_rules");
  if (worstShare * seatCapital > spec.reserve) warnings.push("cascade_beyond_reserve");
  if (!cascade.rules_match) warnings.push("cascade_measured_on_other_rules");
  const o = tailBlock(tables).o99;
  return {
    capital, seat_capital: seatCapital, n_seats: nSeats, groups: outGroups, annual_return: annualReturn, annual_income: income,
    platform_fee_year: feeIncome, sold_per_year: soldTotal, seat_busy_share: busySeats / nSeats,
    capital_committed_share: committed / capital, idle_share: 1.0 - committed / capital, shock_overshoot_covered: shock,
    worst_overshoot: o99, worst_overshoot_range: { low: o.low, high: o.high }, cascade, warnings,
  };
}

/** Buyers a month per $90 000 of seats at which the pool's return reaches `target` (bisection); null if it never does
 *  below `hi`. The return does not fall as demand grows, so bisection is enough. */
export function demandForReturn(tables, spec, target, hi = 400.0) {
  const f = (d) => evaluate(tables, { ...spec, demand_per_100k: d }).annual_return;
  if (f(hi) < target) return null;
  let lo = 0.0, up = hi;
  for (let i = 0; i < 60; i++) {
    const mid = 0.5 * (lo + up);
    if (f(mid) < target) lo = mid; else up = mid;
  }
  return 0.5 * (lo + up);
}

/** The seats and the reserve of the default layout (the rule of sizes.py), for a pool of `pool` dollars. */
export function defaultLayout(pool) {
  const template = 100000.0;
  const k = Math.max(1, Math.floor(pool / template + 1e-9));
  const shrink = Math.min(pool / template, 1.0);
  const seats = [
    { F: 50000.0 * shrink, count: 1 * k },
    { F: 25000.0 * shrink, count: 1 * k },
    { F: 5000.0 * shrink, count: 3 * k },
  ];
  return { seats, reserve: pool - seats.reduce((a, s) => a + s.F * s.count, 0) };
}
