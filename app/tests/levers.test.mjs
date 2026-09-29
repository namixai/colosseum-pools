// node --test app/tests/levers.test.mjs
//
// The levers an investor sets, on the Economics page: where the stop sits, the leverage, the coins. The figures are the
// cascade package's own lever runs (stress/results/levers-2026-09-29.json, made by stress/make_levers.py), copied into
// the table's block `levers`; the card reads every one of them from there. The stop on the exchange runs on this site
// since 29 Sep 2026, and the card says so; the worst case comes last, and it is named as the case without that stop.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { leverCell, exchangeBracket, leversCard } from "../views/economics.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const text = (...p) => readFileSync(join(ROOT, ...p), "utf8");
const TABLES = JSON.parse(text("app", "data", "calc_tables.json"));
const L = TABLES.levers;
const PACKAGE = JSON.parse(text(...L.source.split("/")));
const words = (html) => html.replace(/<[^>]+>/g, " ").replace(/&#39;/g, "'").replace(/\s+/g, " ");
const copy = () => JSON.parse(JSON.stringify(TABLES));

test("the lever cells are the cascade package's own result, cell for cell", () => {
  assert.equal(L.source, "stress/results/levers-2026-09-29.json");
  assert.ok(existsSync(join(ROOT, "stress", "make_levers.py")), "the script that makes the file ships with it");
  assert.deepEqual(L.cells, PACKAGE.cells);
  assert.deepEqual(L.days, PACKAGE.days);
  assert.deepEqual(L.lists, PACKAGE.lists);
  assert.deepEqual(L.book, PACKAGE.book);
  // Two ways the stop can sit, two leverages, two lists; the keeper's minute has two ends.
  assert.equal(L.cells.length, 12);
  for (const stop of ["exchange", "keeper"]) for (const lev of [3, 5]) for (const list of ["default", "wide"]) {
    for (const exec of stop === "exchange" ? ["open"] : ["open", "worst"]) leverCell(TABLES, stop, lev, list, exec);
  }
});

test("the lever runs are the package's other runs where they overlap", () => {
  // Same days as the published runs, and the keeper at 5x on the wide list is the README's headline, long side.
  const days = (file) => [...text("stress", file).match(/^DAYS = \(([^)]*)\)/m)[1].matchAll(/"(20\d\d-\d\d-\d\d)"/g)]
    .map((m) => m[1]);
  assert.deepEqual(L.days, days("make_results.py"));
  assert.deepEqual(L.days, days("make_levers.py"));
  const headline = (file) => JSON.parse(text("stress", "results", file))["пул"]
    .find((r) => r["сутки"] === "2025-10-10" && r["сторона"] === "лонг");
  // Both the table and the lever file itself, so neither can drift from the published runs on its own.
  for (const source of [TABLES, { levers: PACKAGE }]) {
    for (const [exec, file] of [["open", "pool-stress-2026-09-25-lag1.json"], ["worst", "pool-stress-2026-09-25-lag1-worst.json"]]) {
      const cell = leverCell(source, "keeper", 5, "wide", exec);
      assert.equal(cell.worst_pct, headline(file)["худший_убыток_%_капитала_мест"], exec);
      assert.equal(cell.seats_liquidated_on_worst, headline(file)["мест_ликвидировано_в_худшем"], exec);
    }
    const lag0 = headline("pool-stress-2026-09-25-lag0.json");
    assert.equal(leverCell(source, "exchange", 5, "wide").worst_pct, lag0["худший_убыток_%_капитала_мест"]);
  }
});

test("the stop on the exchange: the line, and the book on top at both ends", () => {
  // Low end: half the spread of the calmest coin. High end: the 99th percentile of the walk, the highest over every
  // seat and coin, each seat priced at the nearest measured size at or above its notional. Basis points of the
  // notional, so times the leverage against the seat's capital. On the measurement, that high end is SOL's $1M walk.
  const b3 = exchangeBracket(TABLES, 3);
  const b5 = exchangeBracket(TABLES, 5);
  const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} vs ${b}`);
  near(b3.line, 3.1); near(b3.low, 3.1 + 0.059 * 3 / 100); near(b3.high, 3.1 + 7.235 * 3 / 100);
  near(b5.line, 3.2); near(b5.low, 3.2 + 0.059 * 5 / 100); near(b5.high, 3.2 + 7.235 * 5 / 100);
  // $50k at 3x is $150k of notional: the $1M measurement is the nearest one at or above it, not the $100k one.
  const t = copy();
  for (const coin of ["BTC", "ETH", "SOL"]) for (const size of ["10000", "100000", "1000000"]) t.levers.book.walk_bps[coin][size].p99 = 0.5;
  t.levers.book.walk_bps.SOL["1000000"].p99 = 20;
  near(exchangeBracket(t, 3).high, 3.1 + 20 * 3 / 100);
  // And no seat is priced below its notional: with the $1M walk the cheapest, the $100k one for the small seats counts.
  t.levers.book.walk_bps.SOL["1000000"].p99 = 0.1;
  t.levers.book.walk_bps.ETH["100000"].p99 = 9;
  near(exchangeBracket(t, 3).high, 3.1 + 9 * 3 / 100);
});

test("the card reads every figure from the table, and moves with it", () => {
  const card = words(leversCard(TABLES));
  for (const lev of [3, 5]) {
    for (const list of ["default", "wide"]) {
      assert.ok(card.includes(`${leverCell(TABLES, "exchange", lev, list).worst_pct.toFixed(1)}%`), `exchange ${lev}x ${list}`);
      const [open, worst] = ["open", "worst"].map((e) => leverCell(TABLES, "keeper", lev, list, e));
      assert.ok(card.includes(`${open.worst_pct.toFixed(0)}% to ${worst.worst_pct.toFixed(0)}%`), `keeper ${lev}x ${list}`);
    }
    assert.ok(card.includes(`${exchangeBracket(TABLES, lev).high.toFixed(1)}% at ${lev}x`), `book ${lev}x`);
  }
  // Worked out here, not asked of the function: the line plus SOL's $1M walk (7.235 bps) times the leverage.
  assert.match(card, /the worst day is at most 3\.3% at 3x and 3\.6% at 5x/);
  const medians = L.cells.map((c) => c.median_of_days_pct);
  assert.ok(card.includes(`${Math.min(...medians).toFixed(2)}% to ${Math.max(...medians).toFixed(2)}%`));
  assert.match(card, /65% to 92%, up to 3 of 5 seats liquidated/);
  assert.match(card, /84% to 94%, 3 of 5 seats liquidated/);
  assert.match(card, /between 2025-10-10 and 2026-06-05/);

  const t = copy();
  const cell = (stop, lev, list, exec = "open") => t.levers.cells.find((c) => c.stop === stop && c.leverage === lev
    && c.list === list && c.exec === exec);
  cell("keeper", 5, "wide", "worst").worst_pct = 97.2;
  cell("keeper", 5, "wide", "open").seats_liquidated_on_worst = 4;
  cell("keeper", 5, "wide", "worst").seats_liquidated_on_worst = 4;
  cell("exchange", 3, "default").worst_pct = 2.9;
  t.levers.cells[0].median_of_days_pct = 2.5;
  const moved = words(leversCard(t));
  assert.match(moved, /84% to 97%, 4 of 5 seats liquidated/);
  assert.match(moved, /took 84% to 97% of the seats' capital and liquidated 4 of the five seats/);
  assert.match(moved, /2\.9%/);
  assert.match(moved, /2\.50% to 3\.91%/);
});

test("the stop on the exchange is labelled with the day it went live here, the keeper alone as before it", () => {
  const card = words(leversCard(TABLES));
  assert.match(card, /Stop on the exchange, at the line \(this site, since 29 Sep 2026\)/);
  assert.match(card, /Stop with the keeper alone, a minute late \(before 29 Sep 2026\)/);
  assert.match(card, /On this site since 29 Sep 2026, after a live check on testnet through a gateway running the same code\./);
  assert.doesNotMatch(card, /[Bb]eing built|today/);
  // The card claims no more of the gateway than its three layers say.
  assert.doesNotMatch(card, /refuses to open/);
});

test("the three layers come first, then the levers, and the worst case last, as the case without that stop", () => {
  const card = words(leversCard(TABLES));
  const at = (s) => { const i = card.indexOf(s); assert.ok(i >= 0, `missing: ${s}`); return i; };
  assert.ok(at("Before signing, the gateway") < at("On the exchange, a stop at the rule line"));
  assert.ok(at("On the exchange, a stop at the rule line") < at("After the fact, the contract"));
  assert.ok(at("After the fact, the contract") < at("Where the stop sits is the investor's biggest lever"));
  assert.ok(at("Where the stop sits is the investor's biggest lever") < at("Without a stop on the exchange"));
  const last = card.slice(at("Without a stop on the exchange"));
  assert.match(last, /How often such a day comes is not measured, and the venue replayed is Bybit, not Hyperliquid\.\s*$/);
  // A measurement of a calm book is not a claim about a cascade.
  assert.match(card, /The book in a cascade has never been measured/);
  assert.match(card, /the investor's own money/);
});

test("the page puts the card where the loss is told, and the cascade block says whose stop it shows", () => {
  const source = text("app", "views", "economics.js");
  assert.ok(source.indexOf("Who takes the loss") < source.indexOf("What the rules do not protect against"));
  assert.match(source, /\$\("#who", page\)\.innerHTML = leversCard\(tables\);/);
  assert.match(source, /The capital in a pool is the investor's/);
  const block = source.slice(source.indexOf("What the rules do not protect against")).replace(/\s+/g, " ");
  assert.match(block, /with the keeper alone, a minute late<\/strong>, as pools ran before 29 Sep 2026: with the stop on the exchange, the same day stays at the line/);
  assert.doesNotMatch(block, /runs today/);
  assert.match(source.replace(/\s+/g, " "), /The exceptions on this page are the levers an investor sets and the cascade further down/);
});

test("the levers name their side, and the book names its source", () => {
  const card = words(leversCard(TABLES));
  const at = (s) => { const i = card.indexOf(s); assert.ok(i >= 0, `missing: ${s}`); return i; };
  // One line above the levers: they are the long side; the cascade block below is the worse side. Not merged.
  assert.match(card, /The levers below are measured on the long side, as in the headline of the cascade package's README; the cascade further down shows the worse of the two sides on the same days\./);
  assert.ok(at("measured on the long side") < at("Where the stop sits is the investor's biggest lever"));
  // The book: whose collection, which venue and market, when, and what is published of it.
  assert.equal(L.book.source, "stress/results/hl-book-2026-09-27.json");
  assert.ok(existsSync(join(ROOT, ...L.book.source.split("/"))), "the analyzer's output ships with the package");
  for (const f of ["collect_l2.py", "book.py", "analyze_l2.py"]) assert.ok(existsSync(join(ROOT, "stress", "book", f)), f);
  assert.match(card, /the book taken from our own collection of Hyperliquid's public order book \(l2Book, mainnet\), 34 hourly files of snapshots from 2026-09-25 to 2026-09-27, a calm market; the repository holds the derived bounds and the scripts that derive them, not the raw snapshots\./);
  // Each cell is the worse of the two sides of the analyzer's output, never the side that flatters.
  const out = JSON.parse(text(...L.book.source.split("/")));
  assert.equal(L.book.walk_bps.SOL["1000000"].p99, 7.235);
  assert.equal(out.coins.SOL.cost_bps.sell["1000000"].p99, 6.959);
  assert.equal(L.book.walk_bps.BTC["10000"].p99, 1.077);
  assert.equal(L.book.half_spread_bps.BTC, 0.059);
});

test("the table's lever notes are English and name what was measured", () => {
  for (const note of [L.note, L.book.note, L.book.measured, PACKAGE.note]) {
    assert.doesNotMatch(note, /[А-Яа-яЁё]/);
    assert.doesNotMatch(note, /(pf|cp)#\d+/);
  }
  assert.match(L.note, /The book in a cascade has not been measured/);
  assert.match(L.book.measured, /calm market/);
});
