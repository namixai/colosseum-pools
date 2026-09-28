// node --test app/tests/tail.test.mjs
//
// The Economics page's measured tail (app/data/calc_tables.json, the block `tail`) is the published cascade package's
// results (stress/results), read the way the table's own tool reads them: for each way of executing the stop, the pool's
// worse side on the cascade day. Every figure the page shows from that block is derived again here from the package, so a
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
const LAYOUTS = read("stress", "results", "layouts-default-list-2026-09-25.json");
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

function layout(id) {
  return (mode) => LAYOUTS.runs.find((r) => r.layout === id && r.exec === mode).rows;
}

test("the default list's cascade is the package's layouts and its worst coin", () => {
  const cases = T.cascade.lists.default.cases;
  assert.deepEqual(T.cascade.lists.default.coins, ["BTC", "ETH", "SOL"]);
  for (const [name, id] of [["spread_calm", "calm-seats-big"], ["spread_risky", "risky-seat-big"]]) {
    const p = poolCase(layout(id));
    for (const mode of MODES) close(cases[name][mode], p[mode].share, `${name} ${mode}`);
    assert.deepEqual(cases[name].seats_liquidated, { open: p.open.liquidated, worst: p.worst.liquidated }, name);
  }
  const one = singleCoin(["BTC", "ETH", "SOL"]);
  for (const mode of MODES) close(cases.single_coin[mode], one[mode], `single_coin ${mode}`);
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

test("the rules measured are the package's, and the note the page prints is English and names what was measured", () => {
  const r = RUN.open["правила"];
  assert.deepEqual(T.rules_measured, { max_drawdown: r["просадка"], daily_loss: r["дневной_убыток"], leverage: r["плечо"] });
  assert.doesNotMatch(T.note, /[А-Яа-яЁё]/, "no Russian on the page");
  assert.doesNotMatch(T.note, /pf#|#\d{3,}/, "no internal pull request on the page");
  assert.match(T.note, /one-minute bars from Bybit/);
  assert.match(T.note, /Hyperliquid itself is not checked/);
  assert.match(T.note, /possible, not an expectation/);
});
