// node --test app/tests/levers.test.mjs
//
// The levers an investor sets, on the Economics page: where the stop sits, the leverage, the coins. The figures are the
// cascade package's own lever runs (stress/results/levers-2026-10-29.json, made by stress/make_levers.py), copied into
// the table's block `levers`; the card reads every one of them from there. The leverage on the page is the one the
// leverage rule v1.2 allows each coin, and 10x on every seat beside it; 3x, 5x and the list with alts stay in the table.
// The stop on the exchange runs on this site since 29 Sep 2026, and the card says so; the worst case comes last, and it
// is named as the case without that stop.
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
  assert.equal(L.source, "stress/results/levers-2026-10-29.json");
  assert.ok(existsSync(join(ROOT, "stress", "make_levers.py")), "the script that makes the file ships with it");
  assert.deepEqual(L.cells, PACKAGE.cells);
  assert.deepEqual(L.days, PACKAGE.days);
  assert.deepEqual(L.lists, PACKAGE.lists);
  assert.deepEqual(L.book, PACKAGE.book);
  assert.deepEqual(L.pool, PACKAGE.pool);
  assert.deepEqual(L.rule, PACKAGE.rule);
  // Two ways the stop can sit, three leverages for every seat on two lists and the rule's on BTC, ETH and SOL alone;
  // the keeper's minute has two ends.
  assert.equal(L.cells.length, 21);
  for (const stop of ["exchange", "keeper"]) for (const lev of [3, 5, 10, "v1.2"]) for (const list of ["default", "wide"]) {
    if (lev === "v1.2" && list === "wide") {
      assert.throws(() => leverCell(TABLES, stop, lev, list), /no lever cell/, "the rule allows none of the alts");
      continue;
    }
    for (const exec of stop === "exchange" ? ["open"] : ["open", "worst"]) leverCell(TABLES, stop, lev, list, exec);
  }
});

test("the rule's leverage, seat by seat, and the pool's share beside the seats'", () => {
  // The leverage rule v1.2: BTC and ETH 10x, SOL 5x; the seats in their order, BTC, ETH and three on SOL.
  assert.deepEqual(L.rule.seat_leverage, { BTC: 10, ETH: 10, SOL: 5 });
  // And the script that made the file says the same: the rule's leverage and the seats' share of the pool.
  const script = text("stress", "make_levers.py");
  const said = JSON.parse(script.match(/^RULE, RULE_LEVERAGE = "v1\.2", (\{[^}]*\})$/m)[1]);
  assert.deepEqual(Object.fromEntries(Object.entries(said).map(([c, v]) => [c.replace(/USDT$/, ""), v])), L.rule.seat_leverage);
  assert.equal(Number(script.match(/^IN_WORK_BPS = (\d+)/m)[1]), L.pool.in_work_bps);
  for (const c of L.cells.filter((x) => x.leverage === "v1.2")) assert.deepEqual(c.seat_leverage, [10, 10, 5, 5, 5]);
  for (const c of L.cells.filter((x) => x.leverage !== "v1.2")) assert.deepEqual(c.seat_leverage, Array(5).fill(c.leverage));
  // The seats hold 80% of a $100k pool, in the proportions 50 : 25 : 5 : 5 : 5; the rest is its reserve.
  assert.equal(L.pool.usd, 100000);
  assert.equal(L.pool.in_work_bps, 8000);
  assert.ok(Math.abs(L.pool.seats_usd.reduce((a, b) => a + b, 0) - 80000) < 0.05);
  assert.deepEqual(L.book.seats_usd, L.pool.seats_usd);
  // A share of the pool is the tool's loss in dollars over the whole pool, and four fifths of the seats' share.
  for (const c of L.cells) {
    assert.ok(Math.abs(c.worst_pct_of_pool - c.worst_usd / 1000) <= 0.051, `${c.stop} ${c.leverage} ${c.list} ${c.exec}`);
    assert.ok(Math.abs(c.worst_pct_of_pool - c.worst_pct * 0.8) <= 0.1, `${c.stop} ${c.leverage} ${c.list} ${c.exec}`);
  }
  const rule = [leverCell(TABLES, "exchange", "v1.2", "default"), ...["open", "worst"].map((e) => leverCell(TABLES, "keeper", "v1.2", "default", e))];
  assert.deepEqual(rule.map((c) => c.worst_pct), [3.4, 40.2, 67.2]);
  assert.deepEqual(rule.map((c) => c.worst_pct_of_pool), [2.7, 32.2, 53.8]);
  assert.deepEqual(rule.map((c) => c.seats_liquidated_on_worst), [0, 0, 0]);
});

test("the cells at 3x and 5x are the 29 Sep 2026 file's, with the seats at 80% of the pool instead of 90%", () => {
  // A share of the seats' capital does not move with the seats' size; their dollars move with it, by eight ninths.
  const sep = JSON.parse(text("stress", "results", "levers-2026-09-29.json"));
  assert.equal(sep.cells.length, 12);
  for (const old of sep.cells) {
    const now = leverCell(TABLES, old.stop, old.leverage, old.list, old.exec);
    for (const k of ["worst_day", "worst_entry", "worst_pct", "seats_hit_on_worst", "seats_liquidated_on_worst", "days_with_liquidations"]) {
      assert.equal(now[k], old[k], `${old.stop} ${old.leverage} ${old.list} ${old.exec} ${k}`);
    }
    assert.ok(Math.abs(now.worst_usd - old.worst_usd * 8 / 9) <= 1, `${old.stop} ${old.leverage} ${old.list} ${old.exec} usd`);
    assert.ok(Math.abs(now.median_of_days_pct - old.median_of_days_pct) <= 0.011, `${old.stop} ${old.leverage} ${old.list} ${old.exec} median`);
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
  // Low end: half the spread of the calmest coin, at the lowest seat's leverage. High end: the 99th percentile of the
  // walk, the highest over every seat and coin, each seat at its own leverage and priced at the nearest measured size
  // at or above its notional. Basis points of the notional, so times the seat's leverage against its capital. On the
  // measurement, that high end is SOL's $1M walk for the BTC seat at 10x.
  const rule = exchangeBracket(TABLES, "v1.2");
  const ten = exchangeBracket(TABLES, 10);
  const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} vs ${b}`);
  near(rule.line, 3.4); near(rule.low, 3.4 + 0.059 * 5 / 100); near(rule.high, 3.4 + 7.235 * 10 / 100);
  near(ten.line, 3.5); near(ten.low, 3.5 + 0.059 * 10 / 100); near(ten.high, 3.5 + 7.235 * 10 / 100);
  // The BTC seat, $44.4k at 10x, is $444k of notional: the $1M measurement is the nearest one at or above it.
  const t = copy();
  for (const coin of ["BTC", "ETH", "SOL"]) for (const size of ["10000", "100000", "1000000"]) t.levers.book.walk_bps[coin][size].p99 = 0.5;
  t.levers.book.walk_bps.SOL["1000000"].p99 = 20;
  near(exchangeBracket(t, "v1.2").high, 3.4 + 20 * 10 / 100);
  // Each seat at its own leverage: a SOL seat, $4.4k at 5x, is $22k of notional, so its $100k walk counts five times,
  // not ten; at 10x on every seat the same walk counts ten times.
  t.levers.book.walk_bps.SOL["1000000"].p99 = 0.1;
  t.levers.book.walk_bps.ETH["100000"].p99 = 9;
  near(exchangeBracket(t, "v1.2").high, 3.4 + 9 * 5 / 100);
  near(exchangeBracket(t, 10).high, 3.5 + 9 * 10 / 100);
});

test("the card reads every figure from the table, and moves with it", () => {
  const card = words(leversCard(TABLES));
  // Two rows, both on BTC, ETH and SOL: the rule's leverage, and 10x on every seat. No 3x, no 5x for all, no alts.
  assert.match(card, /Leverage Stop on the exchange at the line \(model; the live stop sits 6 bps before it\) Stop with the keeper alone, a minute late \(before 29 Sep 2026\) BTC and ETH 10x, SOL 5x \(rule v1\.2\) 3\.4% 40% to 67%, no seat liquidated 10x on every seat 3\.5% 46% to 73%, 3 of 5 seats liquidated The seats hold/);
  assert.doesNotMatch(card, /\b3x\b|5x on every seat|[Ww]ith alts|alts at/);
  for (const lev of ["v1.2", 10]) {
    assert.ok(card.includes(`${leverCell(TABLES, "exchange", lev, "default").worst_pct.toFixed(1)}%`), `exchange ${lev}`);
    const [open, worst] = ["open", "worst"].map((e) => leverCell(TABLES, "keeper", lev, "default", e));
    assert.ok(card.includes(`${open.worst_pct.toFixed(0)}% to ${worst.worst_pct.toFixed(0)}%`), `keeper ${lev}`);
  }
  // The pool's share: the seats hold 80% of it, and the rest is its reserve.
  assert.match(card, /The seats hold 80% of the pool and the rest is its reserve, so at the rule's leverage the worst day cost the whole pool 2\.7% with the stop on the exchange, and 32% to 54% with the keeper alone\./);
  // Worked out here, not asked of the function: the line plus SOL's $1M walk (7.235 bps) times 10, for the BTC seat.
  assert.match(card, /the worst day is at most 4\.1% at the rule's leverage and 4\.2% at 10x/);
  assert.match(card, /it cost 3\.37% to 4\.61% in every combination above/);
  assert.match(card, /took 40% to 67% of the seats' capital and liquidated none of the five seats\./);
  assert.match(card, /between 2025-10-10 and 2026-06-05/);

  const t = copy();
  const cell = (stop, lev, list, exec = "open") => t.levers.cells.find((c) => c.stop === stop && c.leverage === lev
    && c.list === list && c.exec === exec);
  cell("keeper", "v1.2", "default", "worst").worst_pct = 70.1;
  cell("keeper", "v1.2", "default", "worst").worst_pct_of_pool = 56.1;
  cell("keeper", "v1.2", "default", "worst").seats_liquidated_on_worst = 2;
  cell("exchange", "v1.2", "default").worst_pct = 2.9;
  cell("exchange", "v1.2", "default").worst_pct_of_pool = 2.3;
  cell("keeper", 10, "default", "worst").median_of_days_pct = 4.95;
  cell("keeper", 3, "default", "worst").median_of_days_pct = 9.99;   // not on the page, so not in its range
  t.levers.pool.in_work_bps = 7500;
  t.levers.rule.seat_leverage = { BTC: 10, ETH: 5, SOL: 5 };
  const moved = words(leversCard(t));
  assert.match(moved, /BTC 10x, ETH and SOL 5x \(rule v1\.2\) 2\.9% 40% to 70%, up to 2 of 5 seats liquidated/);
  assert.match(moved, /The seats hold 75% of the pool and the rest is its reserve, so at the rule's leverage the worst day cost the whole pool 2\.3% with the stop on the exchange, and 32% to 56% with the keeper alone\./);
  assert.match(moved, /took 40% to 70% of the seats' capital and liquidated 2 of the five seats/);
  assert.match(moved, /3\.37% to 4\.95%/);
});

test("the stop on the exchange is labelled with the day it went live here, the keeper alone as before it", () => {
  const card = words(leversCard(TABLES));
  // The table is a model of a stop at the line; the column says so, and where the live stop sits.
  assert.match(card, /Stop on the exchange at the line \(model; the live stop sits 6 bps before it\)/);
  assert.match(card, /Stop with the keeper alone, a minute late \(before 29 Sep 2026\)/);
  assert.match(card, /On this site since 29 Sep 2026, after a live check on testnet through a gateway running the same code\./);
  assert.doesNotMatch(card, /[Bb]eing built|today/);
  // The card claims no more of the gateway than its three layers say.
  assert.doesNotMatch(card, /refuses to open/);
});

test("the three layers come first, then the levers, and the worst case last, as the case without that stop", () => {
  const card = words(leversCard(TABLES));
  const at = (s) => { const i = card.indexOf(s); assert.ok(i >= 0, `missing: ${s}`); return i; };
  assert.ok(at("Before signing, the gateway") < at("On the exchange, a stop 6 basis points before the daily-loss and drawdown line"));
  assert.ok(at("On the exchange, a stop 6 basis points before the daily-loss and drawdown line") < at("After the fact, the contract"));
  // The stop goes on before an order that opens or grows a position, 6 bps before the line (docs/SAFETY.md, section 1);
  // its fill is not promised before the line.
  assert.match(card, /before any order that may open or grow a position/);
  assert.match(card, /The stop fires 6 basis points of the price before the rule's line/);
  assert.match(card, /A fill more than 1\.5 basis points past the trigger lands beyond the line/);
  assert.match(card, /a worse fill, or a gap, lands beyond the line/);
  // Leverage is held after the fact only, and the card says so where the layers are.
  assert.ok(at("It does not check leverage") < at("Leverage is not on that line: only the contract and the keeper"));
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

test("the design and the gateway's docs quote the rule's cells, at the rule's leverage", () => {
  const [line, open, worst] = [leverCell(TABLES, "exchange", "v1.2", "default"),
    ...["open", "worst"].map((e) => leverCell(TABLES, "keeper", "v1.2", "default", e))];
  for (const doc of ["DESIGN.md", "GATEWAY.md"]) {
    const said = text("docs", doc).replace(/\s+/g, " ");
    assert.match(said, /at the leverage the leverage rule v1\.2 allows each coin \(BTC and ETH 10x, SOL 5x\)/, doc);
    assert.ok(said.includes(`lost ${open.worst_pct}–${worst.worst_pct} % of the seats' capital`), doc);
    assert.ok(said.includes(`against ${line.worst_pct} % with the stop at the line (\`${L.source}\`)`), doc);
    assert.doesNotMatch(said, /83\.7–94\.1|stress test's pool/, doc);
  }
});

test("the table's lever notes are English and name what was measured", () => {
  for (const note of [L.note, L.book.note, L.book.measured, PACKAGE.note]) {
    assert.doesNotMatch(note, /[А-Яа-яЁё]/);
    assert.doesNotMatch(note, /(pf|cp)#\d+/);
  }
  assert.match(L.note, /The book in a cascade has not been measured/);
  assert.match(L.book.measured, /calm market/);
});
