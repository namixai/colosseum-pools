// Holds app/lib/calc.js to the model it was derived from: every golden case of calc_cases.json, to
// a relative 1e-9. The cases were produced by that model, not by this file, so a formula that
// drifts here goes red.
//
//   node --test app/tests/*.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { evaluate, demandForReturn, defaultLayout, erlangB, cellKey, cell, arrivalsPerMonth, worstOvershoot, afterCascades, WORST } from "../lib/calc.js";

const here = dirname(fileURLToPath(import.meta.url));
const tables = JSON.parse(readFileSync(join(here, "..", "data", "calc_tables.json"), "utf8"));
const golden = JSON.parse(readFileSync(join(here, "fixtures", "calc_cases.json"), "utf8"));

function close(actual, expected, path) {
  if (expected === null || typeof expected === "string" || typeof expected === "boolean") {
    assert.equal(actual, expected, path);
  } else if (typeof expected === "number") {
    assert.equal(typeof actual, "number", `${path}: ${actual} is not a number`);
    const tol = 1e-9 * Math.max(1, Math.abs(expected));
    assert.ok(Math.abs(actual - expected) <= tol, `${path}: ${actual} vs ${expected}`);
  } else if (Array.isArray(expected)) {
    assert.ok(Array.isArray(actual), `${path}: not an array`);
    assert.equal(actual.length, expected.length, `${path}: length`);
    expected.forEach((e, i) => close(actual[i], e, `${path}[${i}]`));
  } else {
    assert.deepEqual(Object.keys(actual).sort(), Object.keys(expected).sort(), `${path}: keys`);
    for (const k of Object.keys(expected)) close(actual[k], expected[k], `${path}.${k}`);
  }
}

test("evaluate gives the numbers of closed.py, case by case", () => {
  assert.ok(golden.evaluate.length >= 20);
  for (const c of golden.evaluate) close(evaluate(tables, c.spec), c.expected, c.name);
});

test("demandForReturn finds the same demand, or none", () => {
  for (const c of golden.demand_for_return) {
    const got = demandForReturn(tables, c.spec, c.target);
    if (c.expected === null) assert.equal(got, null, c.name); else close(got, c.expected, c.name);
  }
});

test("defaultLayout is the layout rule", () => {
  for (const c of golden.default_layout) close(defaultLayout(c.pool), c.expected, `layout ${c.pool}`);
});

test("erlangB is the Erlang loss", () => {
  for (const c of golden.erlang_b) close(erlangB(c.servers, c.offered), c.expected, `erlangB(${c.servers}, ${c.offered})`);
  assert.equal(erlangB(0, 3), 1);
  assert.equal(erlangB(5, 0), 0);
});

test("a rule off the grid is refused, not guessed", () => {
  assert.throws(() => cell(tables, "base", "real", 0.07, 0.03, 0.10), /off the grid/);
  assert.equal(cellKey("base", "real", 0.06, 0.03, 0.10), "base|real|600|300|1000");
});

test("afterCascades is the return less a rate times a cost", () => {
  assert.ok(golden.after_cascades.length >= 4);
  for (const c of golden.after_cascades) close(afterCascades(c.annual_return, c.per_year, c.loss_share_of_pool), c.expected, "afterCascades");
});

test("the worst overshoot is the measured one, and the exponential only where it is larger", () => {
  assert.equal(worstOvershoot(tables, 0.005), tables.tail.o99.high);
  assert.ok(Math.abs(worstOvershoot(tables, 0.03) - WORST * 0.03) < 1e-15);
  assert.ok(-Math.log(0.01) * 0.005 < tables.tail.o99.low);           // the exponential of the mean is under even the mildest measured value
  const r = evaluate(tables, { ...defaultLayout(100000), scenario: "base", mode: "real" });
  assert.equal(r.worst_overshoot, tables.tail.o99.high);
  assert.deepEqual(r.worst_overshoot_range, { low: tables.tail.o99.low, high: tables.tail.o99.high });
});

test("the cascade of the stress test's own pool, in dollars", () => {
  const r = evaluate(tables, { ...defaultLayout(100000), scenario: "base", mode: "real", asset_list: "wide" });
  const ref = r.cascade.cases.reference_mix;
  assert.ok(Math.abs(ref.loss_usd.low - 75299) < 1);                   // the backtester: $75 299 of $90 000, three seats liquidated
  assert.ok(Math.abs(ref.loss_usd.high - 78362) < 1);                  // with the stop at the worst price of its window
  assert.ok(Math.abs(ref.multiple_of_rules_cap.low - 0.8367 / 0.06) < 0.01);
  assert.ok(Math.abs(r.cascade.rules_cap_usd - 5400) < 1e-9);
  assert.equal(r.cascade.asset_list, "wide");
  assert.equal(r.cascade.coins.length, 10);
});

test("the default list is the default, an unknown list is refused, a table without the tail is refused", () => {
  const spec = { ...defaultLayout(100000), scenario: "base", mode: "real" };
  const r = evaluate(tables, spec);
  assert.deepEqual(r.cascade.coins, ["BTC", "ETH", "SOL"]);
  assert.equal(r.cascade.asset_list, "default");
  assert.throws(() => evaluate(tables, { ...spec, asset_list: "everything" }), /not measured/);
  const { tail, ...bare } = tables;
  assert.throws(() => evaluate(bare, spec), /no measured tail/);
  assert.throws(() => worstOvershoot(bare, 0.005), /no measured tail/);
});

test("the reserve sits on top of the seats, and the cascade comes in years of the base return", () => {
  const spec = { ...defaultLayout(100000), scenario: "base", mode: "real" };
  const r = evaluate(tables, spec);
  const c = r.cascade.cases.single_coin;
  assert.ok(Math.abs(c.loss_share_of_pool.high - c.loss_share_of_seats.high * 0.9) < 1e-12);
  assert.ok(Math.abs(c.loss_beyond_reserve_usd.high - (c.loss_usd.high - 10000)) < 1e-9);
  const y = r.cascade.cases.spread_calm;
  assert.ok(Math.abs(y.years_of_base_return.low - y.loss_share_of_pool.low / r.annual_return) < 1e-12);
  const thick = evaluate(tables, { seats: spec.seats, reserve: 500000, scenario: "base", mode: "real" });
  for (const cs of Object.values(thick.cascade.cases)) assert.deepEqual(cs.loss_beyond_reserve_usd, { low: 0, high: 0 });
  assert.ok(!thick.warnings.includes("cascade_beyond_reserve"));
  const under = evaluate(tables, { ...spec, price_pct: 0.001 });
  assert.ok(under.annual_return < 0);
  for (const cs of Object.values(under.cascade.cases)) assert.deepEqual(cs.years_of_base_return, { low: null, high: null });
});

test("rules off the measured ones are flagged, and the default pool is warned about its tail", () => {
  const spec = { ...defaultLayout(100000), scenario: "base", mode: "real" };
  const w = evaluate(tables, spec);
  assert.equal(w.cascade.rules_match, true);
  for (const code of ["reserve_short_of_a_shock", "cascade_beyond_rules", "cascade_beyond_reserve"]) assert.ok(w.warnings.includes(code), code);
  assert.ok(!w.warnings.includes("cascade_measured_on_other_rules"));
  const other = evaluate(tables, { ...spec, max_drawdown: 0.08, daily_loss: 0.04 });
  assert.equal(other.cascade.rules_match, false);
  assert.ok(other.warnings.includes("cascade_measured_on_other_rules"));
  assert.equal(evaluate(tables, { ...spec, daily_loss: 0.04 }).cascade.rules_match, false);     // either line off is enough
});

test("the default pool of $100 000 gives the report's numbers", () => {
  const r = evaluate(tables, { ...defaultLayout(100000), scenario: "base", mode: "real" });
  assert.ok(Math.abs(r.annual_return - 0.168) < 0.006);
  assert.equal(r.n_seats, 5);
  const lam = arrivalsPerMonth(tables, defaultLayout(100000).seats, 15);
  assert.ok(Math.abs(lam.reduce((a, b) => a + b, 0) - 15) < 1e-12);
});
