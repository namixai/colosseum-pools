// node --test app/tests/ux.test.mjs
//
// Four points of the UX audit of 30 Sep 2026: a pool that cannot sell a challenge is not shown as "Idle: it can
// sell a challenge"; the list says what the site is and offers two ways in; our benches and the shared pools' seats
// are named and listed apart; and a buyer sees the whole price and every way a challenge ends before paying.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { poolStatus } from "../lib/stages.js";
import { saleBlocker } from "../lib/funding.js";
import { LANDING, poolKind, holdsCode, KIND_NOTE, OTHERS_HEADING, cardBlockerLine, listOrder, tradeTarget } from "../lib/listing.js";
import { totalPrice, totalPriceWords, outcomes, outcomesHtml } from "../lib/outcomes.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const DEMO_TERMS = { price: 7_000_000n, capital: 70_000_000n, targetBps: 800, duration: 604800,
  traderShareChallengeBps: 0, traderShareFundedBps: 8000, fundedCapital: 700_000_000n };
const FEE = 700_000n;

test("a pool that cannot sell a challenge says so on its badge and in its sentence", () => {
  assert.deepEqual(poolStatus(0, null), { name: "Idle", tone: "ok", words: "Idle: it can sell a challenge." });
  // What the audit saw on 0x2b10…e80C: 15.03 USDC against the 16.00 it needs.
  const short = saleBlocker({ stage: 0, ready: true, spot: 15.03, needed: 16 });
  assert.deepEqual(poolStatus(0, short), {
    name: "Awaiting top-up", tone: "",
    words: "Awaiting top-up: idle, but 0.97 USDC short on HyperCore, so it cannot sell a challenge until its investor "
      + "moves USDC across.",
  });
  assert.equal(poolStatus(0, saleBlocker({ stage: 0, ready: false })).name, "Not prepared");
  assert.equal(poolStatus(0, saleBlocker({ stage: 0, ready: true, challenge: "0x" + "11".repeat(20) })).name, "Settling");
  for (const b of [short, { kind: "not-prepared" }, { kind: "settling" }]) {
    assert.doesNotMatch(poolStatus(0, b).words, /it can sell a challenge/);
    assert.notEqual(poolStatus(0, b).tone, "ok");
  }
  // A stage that is not idle keeps its own name and words.
  assert.deepEqual(poolStatus(1, { kind: "taken" }), { name: "Challenge", tone: "", words: "A challenge is running on it." });
  assert.equal(cardBlockerLine(short), "Can't sell a challenge: 0.97 USDC short on HyperCore until its investor tops it up.");
  assert.equal(cardBlockerLine(null), "");
  assert.equal(cardBlockerLine({ kind: "taken" }), "");
});

test("both pages decide the badge, the card and the buy button from one reading", () => {
  const pool = text("../views/pool.js");
  assert.match(pool, /const status = poolStatus\(stage, blocker\);/);
  assert.match(pool, /badge\(status\.name, status\.tone\)/);
  assert.match(pool, /: status\.words;/);
  const list = text("../views/pools.js");
  assert.match(list, /const blocker = saleBlocker\(\{ stage, ready, challenge, spot: spotUsdc, needed: Number\(neededSpot\) \/ 1e8 \}\);/);
  assert.match(list, /const status = poolStatus\(stage, blocker\);/);
  assert.match(list, /\$\{blocker \? "See the pool" : "Open the pool"\}/);
  assert.doesNotMatch(list, /badge\(stageName\(stage\)/);
});

test("the list says what the site is, and offers a way in for each role", () => {
  const description = text("../index.html").match(/<meta name="description" content="([^"]+)"/)[1];
  assert.equal(LANDING.line, description.replace(/ Hyperliquid testnet\.$/, ""));
  assert.deepEqual(LANDING.invest, { label: "I want to invest", href: "#/shared" });
  assert.equal(LANDING.trade.label, "I want to trade");
  const items = [
    { index: 0, address: "0xbench", kind: "bench", blocker: null },
    { index: 1, address: "0xshort", kind: "pool", blocker: { kind: "underfunded", short: 1 } },
    { index: 2, address: "0xopen", kind: "pool", blocker: null },
    { index: 3, address: "0xseat", kind: "seat", blocker: { kind: "underfunded", short: 1 } },
  ];
  const ordered = listOrder(items);
  assert.deepEqual(ordered.map((i) => i.address), ["0xopen", "0xshort", "0xseat", "0xbench"]);
  assert.equal(tradeTarget(ordered), "0xopen", "a pool anyone opened, that can sell, first");
  assert.equal(tradeTarget(listOrder(items.filter((i) => i.address !== "0xopen"))), "0xbench", "then any that can sell");
  assert.equal(tradeTarget(listOrder(items.filter((i) => !["0xopen", "0xbench"].includes(i.address)))), null);
  const list = text("../views/pools.js");
  assert.match(list, /LANDING\.trade\.none/);
  assert.match(list, /tradeTarget\(ordered\)/);
});

test("our benches and the shared pools' seats are named and listed apart, by who owns them", () => {
  const deployer = JSON.parse(text("../../deployments/testnet-demo.json")).deployer;
  assert.match(text("../config.js"), new RegExp(`deployer: "${deployer}"`));
  assert.equal(poolKind({ owner: deployer.toLowerCase(), ownerIsContract: false, deployer }), "bench");
  assert.equal(poolKind({ owner: "0x547067e2D6c5627C5463c4cf62086eeD1B2C26a6", ownerIsContract: true, deployer }), "seat");
  assert.equal(poolKind({ owner: "0x21538eBF6598e5866BA496A954dE8E39097bFB59", ownerIsContract: false, deployer }), "pool");
  assert.equal(poolKind({ owner: "0x21538eBF6598e5866BA496A954dE8E39097bFB59", ownerIsContract: false }), "pool");
  assert.equal(KIND_NOTE.bench, "Rehearsal pool: ours, with test parameters.");
  assert.equal(KIND_NOTE.seat, "A seat of a shared pool: many investors hold shares in it.");
  assert.equal(KIND_NOTE.pool, "");
  assert.equal(OTHERS_HEADING, "Rehearsal pools and shared-pool seats");
  const list = text("../views/pools.js");
  assert.match(list, /poolKind\(\{ owner, ownerIsContract, deployer: CONFIG\.deployer \}\)/);
  assert.match(list, /\(kind === "pool" \? \$\("#pools", page\) : \$\("#others", page\)\)\.append\(card\);/);
  assert.match(text("../views/pool.js"), /KIND_NOTE\[kind\]/);
});

test("the whole price, and every way a challenge ends, before Pay and start", () => {
  assert.deepEqual(totalPrice(7_000_000n, FEE), { price: 7_000_000n, fee: 700_000n, total: 7_700_000n });
  assert.equal(totalPriceWords(DEMO_TERMS.price, FEE),
    "7.70 USDC — the price 7.00 and the platform's fee 0.70, which isn't refunded");
  assert.equal(totalPriceWords(1_000_000n, 0n), "1.00 USDC");
  const rows = outcomes(DEMO_TERMS, FEE);
  assert.equal(rows.length, 5);
  assert.deepEqual(rows[0], {
    what: "You pass: within the time limit the account reaches the 8% target with every position closed and no rule broken.",
    you: "None of the challenge's profit; then the pool funds you with 700.00 USDC and you keep 80% of what that stage earns.",
    paid: "The price and the fee are not refunded.",
  });
  for (const r of rows.slice(1, 4)) {
    assert.equal(r.you, "Nothing.");
    assert.equal(r.paid, "The price and the fee are not refunded.");
  }
  assert.match(rows[1].what, /anyone stops the challenge/);
  assert.equal(rows[2].what, "The time limit runs out.");
  assert.equal(rows[3].what, "You walk away.");
  // The one refund the contracts make: ChallengeAccount.abort -> Pool.onChallengeAborted sends the price back.
  assert.match(rows[4].what, /within an hour/);
  assert.equal(rows[4].paid, "The price comes back to your wallet; the fee does not.");
  assert.match(text("../../src/ChallengeAccount.sol"), /uint64 public constant START_WINDOW = 1 hours;/);
  assert.match(text("../../src/Pool.sol"), /safeTransfer\(challengeTrader, price\);\s*emit ChallengeRefunded/);
  const shares = outcomes({ ...DEMO_TERMS, traderShareChallengeBps: 1000 }, 0n);
  assert.match(shares[0].you, /^10% of the challenge's profit/);
  assert.equal(shares[1].paid, "The price is not refunded.");
  const pool = text("../views/pool.js");
  assert.match(pool, /\$\{outcomesHtml\(terms, fee\)\}\s*<button id="buy-btn">Pay and start<\/button>/);
  assert.match(pool, /termsHtml\(terms, fee\)/);
  assert.match(text("../views/pools.js"), /termsHtml\(terms, fee\)/);
  assert.match(outcomesHtml(DEMO_TERMS, FEE), /<th>How it ends<\/th><th>What you get<\/th><th>What you paid<\/th>/);
});

test("the Economics page opens with the risk: who can lose what, what was checked where, and that it is a model", async () => {
  const { riskSummary } = await import("../views/economics.js");
  const tables = JSON.parse(text("../data/calc_tables.json"));
  const words = (html) => html.replace(/<[^>]+>/g, " ").replace(/&#39;/g, "'").replace(/\s+/g, " ").trim();
  assert.equal(words(riskSummary(tables)),
    "A pool can lose its investor's capital; a trader can lose only what a challenge costs. "
    + "Checked on Hyperliquid testnet, and readable on chain: the mechanism — the gateway puts a stop and a take on the "
    + "exchange before any order that may open a position, and the contract enforces the rules and records every stop. "
    + "Replayed on Bybit's one-minute data, not measured on Hyperliquid: the losses — on the worst of the 8 crash days, "
    + "long side, at 5x on a list with alts, a stop on the exchange cost 3.2% at the line and about 3.6% with a calm "
    + "market's order book on top, though a crash's book was never measured and could cost more; a stop a minute late "
    + "cost 84% to 94% of the seats' capital. No pool has run with real traders or real money, so every return on this "
    + "page is a model.");
  // Read from the table, not written in: the lever first, then the keeper.
  const t = JSON.parse(JSON.stringify(tables));
  t.levers.cells.find((c) => c.stop === "keeper" && c.leverage === 5 && c.list === "wide" && c.exec === "worst").worst_pct = 97.2;
  t.levers.days = t.levers.days.slice(0, 7);
  const moved = words(riskSummary(t));
  assert.match(moved, /the worst of the 7 crash days/);
  assert.match(moved, /a stop on the exchange cost 3\.2% at the line and about 3\.6% with a calm market's order book on top, though a crash's book was never measured and could cost more; a stop a minute late cost 84% to 97%/);
  assert.doesNotMatch(moved, /at most/, "the book on top is a calm market's estimate, not a bound");
  // At the top of the page, before the calculator.
  const source = text("../views/economics.js");
  assert.match(source, /<h2>Economics<\/h2>\s*<div id="risk" class="muted">…<\/div>/);
  assert.match(source, /\$\("#risk", page\)\.innerHTML = riskSummary\(tables\);/);
});

test("a refused read of the owner's code costs the label, never the page", async () => {
  assert.equal(await holdsCode(async () => "0x6080", "0xowner"), true);
  assert.equal(await holdsCode(async () => "0x", "0xowner"), false);
  assert.equal(await holdsCode(async () => { throw new Error("429 Too Many Requests"); }, "0xowner"), false);
  for (const page of ["../views/pools.js", "../views/pool.js"]) {
    const source = text(page);
    assert.match(source, /await holdsCode\(\(a\) => chain\.readProvider\.getCode\(a\), owner\)/, page);
    assert.doesNotMatch(source, /\(await chain\.readProvider\.getCode\(owner\)\)/, page);
  }
});
