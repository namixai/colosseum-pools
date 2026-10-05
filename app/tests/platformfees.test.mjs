// node --test app/tests/platformfees.test.mjs
//
// "How the platform earns" on the Economics page (app/lib/platformfees.js, app/views/economics.js): the charges
// planned for mainnet, what the model says they come to, and a calculator that shows the pool before them. The
// calculator used to carry a field "Platform's cut of the price, %", 20 by default and charged on top of the price,
// and a row "Platform's fee, a year": a tariff that was dropped, shown next to the one that replaced it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { RATES, MODELLED, platformIncome, investorCost, builderShare, earnsText } from "../lib/platformfees.js";
import { specFrom, results, lendingDemand, SHOWN_SCENARIOS } from "../views/economics.js";
import { evaluate } from "../lib/calc.js";
import { feeLine } from "../lib/shared.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const tables = JSON.parse(text("../data/calc_tables.json"));
const words = (html) => html.replace(/<[^>]+>/g, " ").replace(/&#39;/g, "'").replace(/\s+/g, " ").trim();
const defaults = (pool) => specFrom({ mode: "default", pool: String(pool), chmode: "real", assetList: "default" });
const base = (pool) => evaluate(tables, { ...defaults(pool), scenario: "base" });

test("the charges are the four agreed ones, and the text counts as many as it lists", () => {
  assert.deepEqual(RATES, { challenge: 0.005, withdrawal: 0.005, builder: 0.0005, poolHype: 1 });
  const [, charges] = earnsText();
  assert.match(charges, /^Four charges are planned for mainnet\./);
  // One sentence for each, in the order of RATES.
  assert.match(charges, /A challenge costs its price and nothing on top: 0\.5% of that price goes to the platform, and the pool receives the rest\./);
  assert.match(charges, /When an investor withdraws, 0\.5% of the value withdrawn is charged\./);
  assert.match(charges, /carry a builder fee of 0\.05% of the notional\./);
  assert.match(charges, /Creating a pool costs 1 HYPE\./);
  assert.match(charges, /These are first rates, meant to go down, not up\.$/);
  // No share of anyone's profit, said first.
  assert.equal(earnsText()[0], "The platform charges for operations. It takes no share of anyone's profit, neither an investor's nor a trader's.");
  assert.doesNotMatch(earnsText().join(" "), /Three fees|Three charges/);
});

test("two of the three modelled parts follow from the calculator on the same page", () => {
  for (const m of MODELLED) {
    const r = base(m.pool);
    // The share of the price: every challenge sold in a year, at its price, times the rate.
    const paid = r.groups.reduce((a, g) => a + g.sold_per_year * g.price, 0);
    assert.ok(Math.abs(paid * RATES.challenge - m.challenge) < 1, `${m.pool}: ${paid * RATES.challenge} against ${m.challenge}`);
    // The charge on a withdrawal: the whole pool withdrawn once, after a year, at what it is worth by then.
    const worth = m.pool * (1 + r.annual_return) - m.challenge;
    assert.ok(Math.abs(worth * RATES.withdrawal - m.withdrawal) / m.withdrawal < 0.01, `${m.pool}: ${worth * RATES.withdrawal} against ${m.withdrawal}`);
  }
  // The return the text names is the one the calculator shows for the pool it opens with.
  assert.equal(base(100_000).annual_return.toFixed(3), MODELLED[0].investorReturn.toFixed(3));
  // The builder fee is the part the calculator cannot give: its table carries no trading volume.
  assert.equal(JSON.stringify(tables).includes("notional_per_year"), false);
});

test("what the text says the charges come to is what the numbers give", () => {
  const [small, large] = MODELLED;
  assert.equal(platformIncome(small), 1_600);
  assert.equal(platformIncome(large), 18_842);
  // The investor's cost is the two charges that come out of the pool, over its capital: 0.71 and 0.76 points.
  assert.equal((investorCost(small) * 100).toFixed(2), "0.71");
  assert.equal((investorCost(large) * 100).toFixed(2), "0.76");
  // "More than half" holds for both pools.
  assert.equal(Math.round(builderShare(small) * 100), 56);
  assert.equal(Math.round(builderShare(large) * 100), 60);
  const model = earnsText()[2];
  // Three charges make the platform's income; two of them make the investor's cost, and the words keep the two apart.
  assert.equal(model, "In the model, at the base scenario, the first three bring the platform about $1,600 a year from a "
    + "$100,000 pool. The first two fall on its investor, and cost about 0.7 points of a 16.6% return. For a "
    + "$1,000,000 pool it is about $18,800 and 0.8 points. Those points leave the builder fee out. The model books "
    + "that fee to the traders and does not split it, but it is taken from the trading account, which holds the "
    + "pool's capital, so part of it can fall on the pool. It is more than half of the platform's income here, and it "
    + "rests on a trading volume nobody has measured yet.");
  assert.doesNotMatch(model, /first three[^.]*cost its investor/);
  // The builder fee is not said to be nobody's but the traders': a trader trades the pool's capital, and the fee is
  // taken from that account. The page says the estimate leaves it out, and the code says its division is not computed.
  assert.doesNotMatch(earnsText().join(" "), /not the investor's/);
  assert.match(text("../lib/platformfees.js"), /The builder fee is left out\.[^/]*so part of it can fall on the\s+\* pool\. How much is not computed here\./);
  // And none of it is charged on testnet, which the last paragraph says.
  assert.equal(earnsText()[3], "This is the design for mainnet. The testnet contracts charge nothing for a withdrawal, "
    + "for an order or for creating a pool, and the challenge fee in the factory is a flat placeholder, paid on top of "
    + "the price. The shared pool's contract can still take a share of a holder's own profit: the current shared pool "
    + "was deployed with that share at zero, and the earlier ones carry 10%.");
  assert.equal(earnsText().length, 4);
  // That last sentence is the shared pool page's own row: "none" for the current pool, 10% for a pool deployed with it.
  assert.equal(feeLine(0), "none: the platform takes nothing from a holder's profit");
  assert.equal(feeLine(1000), "10% of your own profit, taken when you withdraw");
});

test("the calculator shows the pool before the platform's charges, and has no field for the dropped tariff", () => {
  const source = text("../views/economics.js");
  assert.doesNotMatch(source, /feePct|Platform's cut of the price|Platform's fee, a year|platform_fee_year/);
  assert.match(source, /fee_pct_price: 0,/);
  // Whatever a caller passes, the spec carries no fee on top of the price.
  assert.equal(defaults(100_000).fee_pct_price, 0);
  assert.equal(specFrom({ mode: "default", pool: "100000", chmode: "real", feePct: "20" }).fee_pct_price, 0);
  assert.equal(specFrom({ mode: "custom", seats: "20000x2", reserve: "5000", share: "50", pricePct: "2", dd: "5",
    daily: "2", target: "12", chmode: "demo", feePct: "20" }).fee_pct_price, 0);
  for (const pool of [100_000, 500_000, 1_000_000]) {
    const spec = defaults(pool);
    const runs = Object.fromEntries(SHOWN_SCENARIOS.map((s) => [s, evaluate(tables, { ...spec, scenario: s })]));
    for (const r of Object.values(runs)) {
      assert.equal(r.platform_fee_year, 0);
      for (const g of r.groups) assert.equal(g.trader_pays, g.price, "the trader pays the price and nothing on top");
    }
    const page = words(results(tables, runs, lendingDemand(tables, spec)));
    assert.match(page, /Investor's return, a year, before the platform's charges base /);
    assert.match(page, /Every figure here is before the platform's charges, which are set out under "How the platform earns" below\./);
    assert.doesNotMatch(page, /Platform's fee/);
  }
  // The investor's return did not move with the field: it never depended on a fee charged on top of the price.
  const was = { 100000: ["16.6", "35.2"], 500000: ["20.5", "48.7"], 1000000: ["20.9", "52.5"] };
  for (const [pool, [b, g]] of Object.entries(was)) {
    const spec = defaults(Number(pool));
    assert.equal((evaluate(tables, { ...spec, scenario: "base" }).annual_return * 100).toFixed(1), b);
    assert.equal((evaluate(tables, { ...spec, scenario: "good" }).annual_return * 100).toFixed(1), g);
    const charged = evaluate(tables, { ...spec, scenario: "base", fee_pct_price: 0.2 });
    assert.equal(charged.annual_return, evaluate(tables, { ...spec, scenario: "base" }).annual_return);
  }
});

test("the section stands on the Economics page, after what the pool comes to", () => {
  const source = text("../views/economics.js");
  assert.match(source, /import \{ earnsText \} from "\.\.\/lib\/platformfees\.js";/);
  assert.match(source, /<section class="card" id="earns">\s+<h3>How the platform earns<\/h3>\s+\$\{earnsText\(\)\.map\(\(p\) => `<p>\$\{esc\(p\)\}<\/p>`\)\.join\("\\n      "\)\}\s+<\/section>/);
  assert.ok(source.indexOf("<h3>What it comes to</h3>") < source.indexOf("<h3>How the platform earns</h3>"));
  assert.ok(source.indexOf("<h3>How the platform earns</h3>") < source.indexOf("<h3>Who takes the loss</h3>"));
});
