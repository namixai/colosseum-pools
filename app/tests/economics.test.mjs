// node --test app/tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseSeats, specFrom, gridValues, onGrid } from "../views/economics.js";
import { minPrice, ratioOff, gridText, CHALLENGE_RATIO } from "../lib/floor.js";
import { readAll, MAX_BATCH } from "../lib/batch.js";
import { evaluate } from "../lib/calc.js";

const tables = JSON.parse(readFileSync(new URL("../data/calc_tables.json", import.meta.url), "utf8"));
const RULES = { dd: 0.06, daily: 0.03, target: 0.10, share: 0.80 };

test("seats are read as size x count, and a line that is not one is refused", () => {
  assert.deepEqual(parseSeats("50000x1, 25000x1, 5000x3"),
    [{ F: 50000, count: 1 }, { F: 25000, count: 1 }, { F: 5000, count: 3 }]);
  assert.deepEqual(parseSeats(" 10000 × 2 "), [{ F: 10000, count: 2 }]);
  assert.throws(() => parseSeats("50000"), /not a seat/);
  assert.throws(() => parseSeats("0x3"), /above zero/);
  assert.throws(() => parseSeats("50000x1, oops"), /not a seat/);
});

test("the default mode is the decision's pool, and nothing below the minimum is one", () => {
  const spec = specFrom({ mode: "default", pool: "100000", chmode: "real", feePct: "20" });
  assert.deepEqual(spec.seats, [{ F: 50000, count: 1 }, { F: 25000, count: 1 }, { F: 5000, count: 3 }]);
  assert.equal(spec.reserve, 10000);
  assert.equal(spec.price_pct, 0.01);
  assert.equal(spec.trader_share, 0.80);
  assert.equal(spec.max_drawdown, 0.06);
  assert.equal(spec.daily_loss, 0.03);

  const million = specFrom({ mode: "default", pool: "1000000", chmode: "real", feePct: "20" });
  assert.equal(million.seats.reduce((a, s) => a + s.count, 0), 50, "ten copies of the template");

  // A pool of its own starts at $100k; below that the page offers the shared pool instead.
  assert.throws(() => specFrom({ mode: "default", pool: "50000", chmode: "real", feePct: "20" }),
    (e) => e.tooSmall === true);
});

test("the custom mode passes the investor's own terms through", () => {
  const spec = specFrom({
    mode: "custom", seats: "20000x2", reserve: "5000", share: "50", pricePct: "2",
    dd: "5", daily: "2", target: "12", chmode: "demo", feePct: "0",
  });
  assert.deepEqual(spec.seats, [{ F: 20000, count: 2 }]);
  assert.equal(spec.reserve, 5000);
  assert.equal(spec.trader_share, 0.5);
  assert.equal(spec.price_pct, 0.02);
  assert.equal(spec.max_drawdown, 0.05);
  assert.equal(spec.daily_loss, 0.02);
  assert.equal(spec.target, 0.12);
  assert.equal(spec.mode, "demo");
  assert.equal(spec.fee_pct_price, 0);
});

test("the form offers exactly the rules the table has cells for", () => {
  const grid = gridValues(tables);
  assert.deepEqual(grid.dd, [4, 5, 6, 8, 10]);
  assert.deepEqual(grid.daily, [2, 3, 4, 5]);
  assert.deepEqual(grid.target, [8, 10, 12]);
  assert.equal(onGrid(tables, { scenario: "base", mode: "real", dd: 0.06, daily: 0.03, target: 0.10 }), true);
  assert.equal(onGrid(tables, { scenario: "base", mode: "real", dd: 0.065, daily: 0.03, target: 0.10 }), false);
});

test("the price floor is the floor of the investor's own rules, or nothing", () => {
  const seat = minPrice(tables, { fundedCapital: 10000, capital: 1000, ...RULES, target: 0.08 });
  assert.ok(seat.price > 0 && seat.price < 10000);
  // Off the grid the model says nothing rather than pricing the nearest rules it happens to hold.
  const off = minPrice(tables, { fundedCapital: 10000, capital: 1000, ...RULES, target: 0.09 });
  assert.equal(off.offGrid, true);
  assert.equal(off.price, undefined);
  assert.equal(minPrice(tables, { fundedCapital: 0, capital: 0, ...RULES }).offGrid, true);

  // A bigger seat costs the pool more per challenge: the figure scales with what is at risk.
  const small = minPrice(tables, { fundedCapital: 1000, capital: 100, ...RULES }).price;
  const big = minPrice(tables, { fundedCapital: 10000, capital: 1000, ...RULES }).price;
  assert.ok(big > small * 5, `${big} vs ${small}`);
});

test("a challenge account away from a tenth of the seat is called out", () => {
  assert.equal(ratioOff({ capital: 1000, fundedCapital: 10000 }), null, "a tenth is what the model prices");
  assert.equal(ratioOff({ capital: 100, fundedCapital: 200 }), 0.5);
  assert.equal(ratioOff({ capital: 0, fundedCapital: 0 }), null);
  assert.equal(CHALLENGE_RATIO, tables.constants.challenge_ratio, "the note and the table agree");
  assert.match(gridText(tables), /drawdown 4%, 5%, 6%, 8%, 10%/);
});

test("the new-pool form takes one number from the model and no more", () => {
  // The decision of 20 Sep: the calculator lives on its own page, because it describes a pool of
  // several seats with a shared reserve while this form makes a single-seat pool. Exactly one
  // figure travels to the form -- the floor under the price of the pool being created.
  const source = readFileSync(new URL("../views/pools.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /\bevaluate\b/, "the whole calculator moved into the new-pool form");
  assert.doesNotMatch(source, /defaultLayout|demandForReturn|annual_return/,
    "the form is showing the pool-at-scale model next to a button that does not make one");
  assert.match(source, /minPrice\(/, "the one figure that does belong there is gone");
});

test("a fan-out read never hands the node a batch it refuses", async () => {
  // ethers sends one Promise.all as one JSON-RPC batch, and this RPC refuses a batch over twenty
  // calls with -32010. The new-pool page asked about 64 perps that way and never loaded at all.
  assert.equal(MAX_BATCH, 20);
  let inFlight = 0, worst = 0;
  const rounds = [];
  const make = async (item) => {
    inFlight++; worst = Math.max(worst, inFlight);
    await new Promise((r) => setTimeout(r, 0));
    rounds.push(item);
    inFlight--;
    return item * 2;
  };
  const items = Array.from({ length: 64 }, (_, i) => i);
  const out = await readAll(items, make);
  assert.ok(worst <= MAX_BATCH, `${worst} calls at once, the node takes ${MAX_BATCH}`);
  assert.deepEqual(out, items.map((i) => i * 2), "the order of the answers is the order of the questions");
  assert.deepEqual(rounds.slice().sort((a, b) => a - b), items, "every item was asked about once");
  assert.deepEqual(await readAll([], make), []);
  await assert.rejects(() => readAll(items, make, 0), /at least one/);
});

test("the new-pool form asks about the deployment's assets, not about every perp on the venue", () => {
  const source = readFileSync(new URL("../views/pools.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /slice\(0,\s*64\)/, "the form is probing a fixed slice of the venue's perps again");
  assert.match(source, /chain\.readAll\(/, "the fan-out is not going through the bounded helper");
});

test("the asset list reaches the calculator, and the page cannot show a tail without naming it", () => {
  // The measured tail is two to three times larger on a wide list than on BTC/ETH/SOL. A page that
  // shows one number without the list it belongs to is wrong for one of the two pools.
  const spec = specFrom({ mode: "default", pool: "100000", chmode: "real", feePct: "20", assetList: "wide" });
  assert.equal(spec.asset_list, "wide");
  assert.equal(specFrom({ mode: "default", pool: "100000", chmode: "real", feePct: "20" }).asset_list, "default",
    "no list named means the list the demo deploys with");

  const dflt = evaluate(tables, { ...specFrom({ mode: "default", pool: "100000", chmode: "real", feePct: "20" }), scenario: "base" });
  const wide = evaluate(tables, { ...spec, scenario: "base" });
  assert.deepEqual(dflt.cascade.coins, ["BTC", "ETH", "SOL"]);
  assert.ok(wide.cascade.coins.length > dflt.cascade.coins.length);

  const worst = (r) => Math.max(...Object.values(r.cascade.cases).map((c) => c.loss_share_of_seats.high));
  assert.ok(worst(wide) > worst(dflt), "the wide list has the bigger tail, as measured");
  // The figure the decision quotes -- more than four fifths -- belongs to the wide list. On the
  // default list the smallest case is nowhere near it, which is why the page prints the list.
  const calm = dflt.cascade.cases.spread_calm.loss_share_of_seats;
  assert.ok(calm.high < 0.5, `${calm.high} on BTC/ETH/SOL with the big seats on the calm coins`);
  assert.ok(wide.cascade.cases.reference_mix.loss_share_of_seats.low > 0.8);

  // Both ends of every range, never one: low is the next open, high the worst price of the window.
  for (const c of Object.values(dflt.cascade.cases)) {
    assert.ok(c.loss_share_of_seats.high >= c.loss_share_of_seats.low);
  }
  assert.ok(dflt.cascade.note.length > 80, "the mandatory caption travels with the numbers");
  assert.equal(dflt.cascade.rules_match, true, "the default rules are the ones it was measured on");
});

test("the Economics page shows the coins and the caption, not just a percentage", () => {
  const source = readFileSync(new URL("../views/economics.js", import.meta.url), "utf8");
  assert.match(source, /c\.coins/, "the page prints a tail without naming the assets it was measured on");
  assert.match(source, /c\.note/, "the page drops the mandatory caption");
  assert.match(source, /loss_share_of_seats/, "the page stopped showing the measured loss");
  // No hand-typed cascade percentage: the page prints what the measurement says for the list chosen.
  assert.doesNotMatch(source, /8[37]\.[17]\s*%|four fifths/,
    "a cascade figure is hard-coded in the page and will not follow the asset list");
});
