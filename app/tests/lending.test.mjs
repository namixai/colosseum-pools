// node --test app/tests/lending.test.mjs
//
// The Economics page shows two scenarios, base and good, and in place of the third a line: how many buyers a month
// the pool needs to earn what lending stablecoins pays. The table keeps the third scenario's cells; only the page
// leaves them out. The number on the line is the model's own answer, worked out on the page, never written in.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { evaluate, defaultLayout } from "../lib/calc.js";
import { SHOWN_SCENARIOS, LENDING, lendingDemand, results, specFrom } from "../views/economics.js";

const tables = JSON.parse(readFileSync(new URL("../data/calc_tables.json", import.meta.url), "utf8"));
const words = (html) => html.replace(/<[^>]+>/g, " ").replace(/&#39;/g, "'").replace(/\s+/g, " ");
const spec = (pool) => specFrom({ mode: "default", pool: String(pool), chmode: "real", assetList: "default" });
const page = (pool) => {
  const s = spec(pool);
  const runs = Object.fromEntries(SHOWN_SCENARIOS.map((scenario) => [scenario, evaluate(tables, { ...s, scenario })]));
  return words(results(tables, runs, lendingDemand(tables, s)));
};

test("the page shows base and good; the table still holds bad, cell for cell", () => {
  assert.deepEqual(SHOWN_SCENARIOS, ["base", "good"]);
  assert.ok(tables.scenarios.bad, "the scenario stays in the table");
  const cells = (name) => Object.keys(tables.cells).filter((k) => k.startsWith(`${name}|`)).length;
  assert.ok(cells("bad") > 0);
  assert.equal(cells("bad"), cells("base"));
  const text = page(100_000);
  assert.match(text, /Investor's return, a year, before the platform's charges base 16\.6% · good 35\.2%/);
  // "bad" as a word nowhere in what the page says (the warning badges use it only as a class, which words() drops).
  assert.doesNotMatch(text, /\bbad\b/);
  assert.doesNotMatch(text, /4\.4%/);
  assert.match(text, /at the two scenarios named above/);
  const source = readFileSync(new URL("../views/economics.js", import.meta.url), "utf8");
  assert.match(source, /runs = SHOWN_SCENARIOS\.map\(\(scenario\) => \[scenario, evaluate\(tables, \{ \.\.\.spec, scenario \}\)\]\);/);
});

test("the demand line: the buyers a month at which the pool earns what lending stablecoins pays", () => {
  assert.equal(LENDING.rate, 0.05);
  assert.match(LENDING.source, /^DeFiLlama, 30 Sep 2026, 30-day yields: Maple 5\.1%, Fluid 5\.1%, Aave 4% to 5\.6%$/);
  const s = spec(100_000);
  const d = lendingDemand(tables, s);
  // Held to the model from outside the search: at that demand the base scenario earns 5%, a little less earns less
  // and a little more earns more.
  const at = (demand) => evaluate(tables, { ...s, scenario: "base", demand_per_100k: demand }).annual_return;
  assert.ok(Math.abs(at(d) - 0.05) < 1e-9, `${at(d)}`);
  assert.ok(at(d - 0.1) < 0.05 && at(d + 0.1) > 0.05);
  const text = page(100_000);
  assert.match(text, /To earn what lending stablecoins pays, 5% a year 3\.8 buyers a month for every \$90,000 of seats; the base scenario assumes 15/);
  assert.doesNotMatch(text, /for this pool's/, "a $100,000 pool has exactly $90,000 of seats");
  assert.match(text, /\(DeFiLlama, 30 Sep 2026, 30-day yields: Maple 5\.1%, Fluid 5\.1%, Aave 4% to 5\.6%\)/);
  // A larger pool: the same scale, and what it comes to for the pool's own seats.
  assert.match(page(500_000), /3\.6 buyers a month for every \$90,000 of seats \(17\.9 for this pool's \$450,000\)/);
});

test("the demand line says so when no demand gets there, and moves with the table", () => {
  const s = spec(100_000);
  const runs = Object.fromEntries(SHOWN_SCENARIOS.map((scenario) => [scenario, evaluate(tables, { ...s, scenario })]));
  assert.match(words(results(tables, runs, null)), /no demand the model takes brings this pool to 5% a year/);
  const t = JSON.parse(JSON.stringify(tables));
  t.scenarios.base.demand_per_100k = 20;
  assert.match(words(results(t, runs, lendingDemand(t, s))), /the base scenario assumes 20/);
  assert.deepEqual(defaultLayout(100_000).seats.reduce((a, g) => a + g.F * g.count, 0), tables.demand.reference_seat_capital);
});
