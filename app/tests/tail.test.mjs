// node --test app/tests/tail.test.mjs
//
// The Economics page's measured tail (app/data/calc_tables.json, the block `tail`) is the published cascade package's
// results (stress/results), read the way the table's own tool reads them: for each way of executing the stop, the pool's
// worse side on the cascade day. The default list is measured at the leverage rule v1.2, each seat at its coin's
// (stress/make_cascade.py); the wide list, which the rule does not allow, stays at the stress test's own 5x. Every figure the page shows from that block is derived again here from the package, so a
// new version of the package that moves a number turns this red until the table is rebuilt.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (...p) => JSON.parse(readFileSync(join(ROOT, ...p), "utf8"));
const T = read("app", "data", "calc_tables.json").tail;
const RUN = { open: read("stress", "results", "pool-stress-2026-09-25-lag1.json"), worst: read("stress", "results", "pool-stress-2026-09-25-lag1-worst.json") };
const CASCADE = read("stress", "results", "cascade-v1.2-2026-10-29.json");
const MODES = ["open", "worst"];
const DAY = "2025-10-10";
const SEAT_CAPITAL = 90_000;                                       // five seats: 50k, 25k and three of 5k
const coin = (s) => s.replace(/USDT$/, "");
const close = (a, b, what) => assert.ok(Math.abs(a - b) < 1e-12, `${what}: ${a} in the table, ${b} in the package`);

/** A pool of five seats on the cascade day: the worse of its two sides, as a share of the seats' capital. */
function poolCase(rowsOf) {
  const out = {};
  for (const mode of MODES) {
    const rows = rowsOf(mode).filter((r) => r["сутки"] === DAY);
    assert.equal(rows.length, 2, `${mode}: one row per side on ${DAY}`);
    const worse = rows.reduce((a, b) => (b["худший_убыток_usd"] > a["худший_убыток_usd"] ? b : a));
    out[mode] = { share: worse["худший_убыток_usd"] / SEAT_CAPITAL, liquidated: worse["мест_ликвидировано_в_худшем"] };
  }
  return out;
}

/** Every seat on one coin: the worst per-coin loss of the list on the cascade day. */
function singleCoin(coins) {
  return Object.fromEntries(MODES.map((mode) => [mode, Math.max(...RUN[mode]["строки"]
    .filter((r) => r["сутки"] === DAY && coins.includes(coin(r["монета"]))).map((r) => r["убыток_макс_%"] / 100))]));
}

test("the default list's cascade is the cascade file's layouts at the rule's leverage, and its worst coin", () => {
  const list = T.cascade.lists.default;
  assert.equal(list.source, "stress/results/cascade-v1.2-2026-10-29.json");
  assert.deepEqual(list.coins, ["BTC", "ETH", "SOL"]);
  assert.deepEqual(list.cases, CASCADE.cases, "the table carries the file's cases, cell for cell");
  // Each seat at the leverage the rule allows its coin, in every run of the file.
  const RULE = { BTCUSDT: 10, ETHUSDT: 10, SOLUSDT: 5 };
  for (const run of CASCADE.runs) assert.deepEqual(run.seat_leverage, run.seat_coins.map((c) => RULE[c]), run.layout);
  // Derived again from the tool's rows: the worse side of the worse layout, a share of the seats' capital.
  const seats = CASCADE.pool.usd * CASCADE.pool.in_work_bps / 10000;
  const worse = (ids, mode) => CASCADE.runs.filter((r) => ids.includes(r.layout) && r.exec === mode).flatMap((r) => r.rows)
    .reduce((a, b) => (b["худший_убыток_usd"] > a["худший_убыток_usd"] ? b : a));
  for (const [name, ids] of [["spread_calm", ["calm-seats-big"]], ["spread_risky", ["risky-seat-big"]],
    ["single_coin", ["all-on-btc", "all-on-eth", "all-on-sol"]]]) {
    for (const mode of MODES) {
      const row = worse(ids, mode);
      close(list.cases[name][mode], row["худший_убыток_usd"] / seats, `${name} ${mode}`);
      assert.equal(list.cases[name].seats_liquidated[mode], row["мест_ликвидировано_в_худшем"], `${name} ${mode} liquidated`);
    }
  }
  // The big seats on the calm coins are the levers' cell at the rule's leverage: there the worse side is the long one.
  const levers = read("stress", "results", "levers-2026-10-29.json");
  for (const mode of MODES) {
    const cell = levers.cells.find((c) => c.stop === "keeper" && c.leverage === "v1.2" && c.list === "default" && c.exec === mode);
    assert.equal(Math.round(list.cases.spread_calm[mode] * 1000) / 10, cell.worst_pct, mode);
    assert.equal(list.cases.spread_calm[`${mode}_at`], "long");
  }
  // What the page shows, pinned: each case's range and the leverage of each end.
  const pct = (name) => MODES.map((m) => Math.round(list.cases[name][m] * 1000) / 10);
  assert.deepEqual(pct("spread_calm"), [40.2, 67.2]);
  assert.deepEqual(pct("spread_risky"), [42.5, 84.4]);
  assert.deepEqual(pct("single_coin"), [68.2, 100]);
  assert.deepEqual(list.cases.single_coin.leverage, { open: "ETH 10x", worst: "SOL 5x" });
  for (const name of ["spread_calm", "spread_risky"]) assert.deepEqual(list.cases[name].leverage, { open: "BTC and ETH 10x, SOL 5x", worst: "BTC and ETH 10x, SOL 5x" });
});

test("the wide list's cascade is the stress test's own pool and its worst coin", () => {
  const cases = T.cascade.lists.wide.cases;
  const coins = [...new Set(RUN.open["строки"].map((r) => coin(r["монета"])))].sort();
  assert.deepEqual(T.cascade.lists.wide.coins, coins);
  const p = poolCase((mode) => RUN[mode]["пул"]);
  for (const mode of MODES) close(cases.reference_mix[mode], p[mode].share, `reference_mix ${mode}`);
  assert.deepEqual(cases.reference_mix.seats_liquidated, { open: p.open.liquidated, worst: p.worst.liquidated });
  const one = singleCoin(coins);
  for (const mode of MODES) close(cases.single_coin[mode], one[mode], `wide single_coin ${mode}`);
  assert.equal(T.cascade.day, DAY);
});

test("a stop's overshoot is the package's summaries, side by side and execution by execution", () => {
  const FIELDS = { mean: "перелёт_средн_пп", median: "перелёт_медиана_пп", p95: "перелёт_p95_пп", p99: "перелёт_p99_пп", max: "перелёт_макс_пп" };
  for (const [side, ru] of [["long", "лонг"], ["short", "шорт"]]) {
    for (const mode of MODES) {
      const row = RUN[mode]["сводки"].find((s) => s["сводка"].startsWith(ru));
      const t = T.overshoot[side][mode];
      for (const [f, key] of Object.entries(FIELDS)) close(t[f], row[key] / 100, `${side} ${mode} ${f}`);
      close(t.share_liquidated, row["ликвидаций_%"] / 100, `${side} ${mode} liquidated`);
      assert.equal(t.episodes, row["эпизодов"]);
    }
  }
  close(T.o99.low, Math.min(T.overshoot.long.open.p99, T.overshoot.long.worst.p99), "o99 low");
  close(T.o99.high, Math.max(T.overshoot.long.open.p99, T.overshoot.long.worst.p99), "o99 high");
});

test("the default list's note names the rule's leverage; the overshoot's note is the stress test's own pool at 5x", () => {
  const note = T.cascade.lists.default.note;
  assert.doesNotMatch(note, /[А-Яа-яЁё]/);
  assert.doesNotMatch(note, /pf#|cp#|#\d{3,}/);
  assert.match(note, /each seat at the leverage the leverage rule v1\.2 allows its coin \(BTC and ETH 10x, SOL 5x\)/);
  assert.match(note, /the worse of the two sides/);
  assert.match(note, /possible, not an expectation/);
  assert.doesNotMatch(note, /from the start, 5x/, "the stress test's one leverage for every seat");
  assert.match(T.note, /5x/, "the stress test's pool, which the overshoot is measured on, names its leverage");
});

test("the rules measured are the package's, and the note the page prints is English and names what was measured", () => {
  const r = RUN.open["правила"];
  assert.deepEqual(T.rules_measured, { max_drawdown: r["просадка"], daily_loss: r["дневной_убыток"], leverage: r["плечо"] });
  assert.doesNotMatch(T.note, /[А-Яа-яЁё]/, "no Russian on the page");
  assert.doesNotMatch(T.note, /pf#|#\d{3,}/, "no internal pull request on the page");
  assert.match(T.note, /one-minute bars from Bybit/);
  assert.match(T.note, /Hyperliquid itself is not checked/);
  assert.match(T.note, /possible, not an expectation/);
});
