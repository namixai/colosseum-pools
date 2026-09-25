// node --test app/tests/evidence.test.mjs
//
// The page "What happened on chain" (#/evidence) shows app/data/evidence.json. It is held to the two documents
// it comes from the way our submission texts are held to them: each source line of a row is found in its
// section of the document as written, and everything the row shows (account, transaction, block, sender, fills,
// record) is inside its own source lines. A row can then neither drift from the documents nor pair one account
// with another's transaction. The documents are read from this checkout, so on main they are main's.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { TEXT, groups, quoted, appLink, howToCheck, receiptVerdict, fillsVerdict } from "../lib/evidence.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const DATA = JSON.parse(readFileSync(join(ROOT, "app", "data", "evidence.json"), "utf8"));

// A document as the rows quote it: no emphasis, code marks or link targets, one space for any run of spaces.
const plain = (md) => md.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\*\*/g, "").replace(/`/g, "").replace(/\s+/g, " ").trim();

function read(path) {
  const md = readFileSync(join(ROOT, path), "utf8");
  const parts = md.split(/^## /m);
  const sections = new Map([["", plain(parts[0])]]);
  const order = [""];
  for (const part of parts.slice(1)) {
    const nl = part.indexOf("\n");
    const head = part.slice(0, nl).trim();
    sections.set(head, plain(part.slice(nl + 1)));
    order.push(head);
  }
  return { all: plain(md), sections, order };
}
const DOCS = new Map(["docs/EVIDENCE.md", "docs/EVIDENCE-SHARED-POOL.md"].map((p) => [p, read(p)]));
const section = (doc, name) => DOCS.get(doc)?.sections.get(name);

/** The text names the address in full or as the documents shorten it, "0x547067e2…26a6". */
function mentions(text, address) {
  if (text.includes(address)) return true;
  const a = address.toLowerCase();
  return [...text.matchAll(/0x([0-9a-fA-F]{6,})…([0-9a-fA-F]*)/g)]
    .some((m) => a.startsWith(`0x${m[1].toLowerCase()}`) && a.endsWith(m[2].toLowerCase()));
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const numbers = (text) => text.replace(/0x[0-9a-fA-F]+/g, "").match(/\d+(?:[.,]\d+)*/g) || [];
const hasNumber = (text, n) => new RegExp(`(^|[^\\d.,])${escapeRe(n)}($|[^\\d])`).test(text);

// Claims the page may not make in its own words: they are not what the documents record (EVIDENCE.md, "What is
// staged, and what is not"). A record or a note may carry such words only as the documents' own.
const NOT_CLAIMED = [
  /automatic/i, /autonomous/i, /\bAI\b/, /\bbots?\b/i, /\b(exchange|Hyperliquid|venue)\b.{0,30}\b(refus|reject)/i,
  /\bmainnet\b/i, /guarantee/i, /trustless/i, /\bsafe\b/i, /\bsecure/i, /\baudited\b/i, /\byield/i, /\bearn/i,
  /\bprofit/i, /\bevery (breach|stop)\b/i, /\bnobody arranged\b/i,
];
const OUR_WORDS = [...Object.values(TEXT).flat(), ...DATA.rows.map((r) => r.what)];

test("every row's source lines are in its section of the document", () => {
  const lost = [];
  for (const r of DATA.rows) {
    const body = section(r.doc, r.section);
    if (body === undefined) lost.push(`${r.what}: ${r.doc} has no section "${r.section}"`);
    else for (const s of r.sources) if (!body.includes(s)) lost.push(`${r.what}: «${s}»`);
  }
  assert.deepEqual(lost, []);
  assert.equal(DATA.rows.length, 25);
});

test("a row shows nothing its own source lines do not say", () => {
  for (const r of DATA.rows) {
    const src = r.sources.join(" ");
    const tag = r.what;
    assert.ok(r.sources.some((s) => s.includes(r.record)), `${tag}: the record is quoted from a source line`);
    if (r.account) {
      assert.ok(mentions(src, r.account), `${tag}: its sources name the account`);
      assert.ok(DOCS.get(r.doc).all.includes(r.account), `${tag}: the account is written out in full in ${r.doc}`);
    }
    if (r.tx) assert.ok(src.includes(r.tx), `${tag}: its sources name the transaction`);
    if (r.block) assert.ok(hasNumber(src, `block ${r.block}`), `${tag}: block ${r.block} is the one its sources name`);
    if (r.from) assert.ok(src.includes(r.from), `${tag}: its sources name the sender`);
    if (r.sentBy) assert.ok(src.includes(r.sentBy), `${tag}: who sent it is said in its sources`);
    for (const h of r.fills || []) assert.ok(src.includes(h), `${tag}: its sources name the fill ${h}`);
    for (const n of numbers(r.what)) assert.ok(hasNumber(src, n), `${tag}: ${n} is in its sources`);
  }
});

test("a key is cut in the block of the transaction that stopped its account", () => {
  const cut = DATA.rows.filter((r) => r.tx && /cut at block \d+/.test(r.record));
  assert.equal(cut.length, 2);
  for (const r of cut) assert.equal(Number(r.record.match(/cut at block (\d+)/)[1]), r.block, r.what);
});

test("the calls and events a row names are the documents' own", () => {
  for (const r of DATA.rows) {
    const all = DOCS.get(r.doc).all;
    for (const c of r.check) {
      for (const name of c.match(/[A-Za-z.]+\(/g) || []) assert.ok(all.includes(name), `${r.what}: ${name} is in ${r.doc}`);
      for (const hex of c.match(/0x[0-9a-fA-F]{8,}/g) || []) assert.ok(all.includes(hex), `${r.what}: ${hex} is in ${r.doc}`);
      for (const n of numbers(c)) assert.ok(hasNumber(all, n), `${r.what}: ${n} is in ${r.doc}`);
    }
    for (const n of r.notes) assert.ok(all.includes(n), `${r.what}: the note «${n}» is in ${r.doc}`);
  }
});

test("every leverage stop is marked as staged, and the daily-loss stop as the one nobody arranged", () => {
  const end = "The end states recorded so far";
  const table = section("docs/EVIDENCE.md", end);
  const inTable = DATA.rows.filter((r) => r.section === end && /\bLeverage\b/.test(r.record));
  assert.equal(inTable.length, (table.match(/(breachReason|fundedEndReason) Leverage/g) || []).length);
  assert.equal(inTable.length, 2);
  const two = DATA.rows.filter((r) => r.notes.some((n) => n.startsWith("The two leverage stops were staged")));
  assert.deepEqual(two, inTable);
  const leverage = DATA.rows.filter((r) => /\bLeverage\b/.test(r.record));
  assert.equal(leverage.length, 3);
  for (const r of leverage) assert.ok(r.notes.some((n) => /were staged|deliberately, by us/.test(n)), `${r.what}: says it was staged`);
  const daily = DATA.rows.filter((r) => /DailyLoss/.test(r.record));
  assert.equal(daily.length, 1);
  assert.ok(daily[0].notes.some((n) => /nobody arranged/.test(n)), "the daily-loss stop says nobody arranged it");
  assert.ok(daily[0].notes.some((n) => /run from a laptop/.test(n)), "and that the keeper ran from a laptop");
});

test("the leverage stop that ran on the demo pool says so, with the pool's terms", () => {
  const staged = section("docs/EVIDENCE.md", "What is staged, and what is not");
  const m = staged.match(/challenge (0x[0-9a-fA-F]+)…, stopped for leverage — ran on the demo pool (0x[0-9a-fA-F]{40})/);
  assert.ok(m, "the document names the challenge and the demo pool");
  const row = DATA.rows.find((r) => r.account?.toLowerCase().startsWith(m[1].toLowerCase()));
  assert.ok(row, "that challenge is a row of the page");
  assert.ok(row.notes.some((n) => n.includes(m[2]) && n.includes("the terms an investor would actually set")), row.what);
});

test("the page links an account only where the app reads it", () => {
  const rehearsal = section("docs/EVIDENCE.md", "The fourth ending, on the rehearsal deployment");
  assert.match(rehearsal, /not on the demo's factory/);
  const pools = DATA.pools.rows.map((p) => p.pool);
  for (const r of DATA.rows) {
    assert.ok([null, "challenge", "pool", "seat", "shared pool"].includes(r.kind), `${r.what}: kind ${r.kind}`);
    assert.equal(r.factory, r.section === "The fourth ending, on the rehearsal deployment" ? "rehearsal" : "demo", r.what);
    const link = appLink(r);
    if (r.factory !== "demo" || !r.account) assert.equal(link, null, `${r.what}: the app does not read it`);
    else if (r.kind === "shared pool") {
      assert.ok(pools.includes(r.account), `${r.what}: one of the three shared pools`);
      assert.equal(link, `#/shared/${r.account}`);
    } else assert.equal(link, `#/verify/${r.account}`, r.what);
  }
});

test("the three shared pools are the document's table, cell for cell", () => {
  const body = section(DATA.pools.doc, DATA.pools.section);
  assert.equal(DATA.pools.rows.length, 3);
  for (const p of DATA.pools.rows) {
    const line = `| ${p.pool} | ${p.factory} | ${p.open} | ${p.showed} |`;
    assert.ok(body.includes(line), line);
    assert.match(p.open, /^#\/shared(\/0x[0-9a-fA-F]{40})?( |$)/);
  }
});

test("what was staged, and what none of this shows, are quoted from their sections", () => {
  const staged = section(DATA.staged.doc, DATA.staged.section);
  for (const q of DATA.staged.quotes) assert.ok(staged.includes(q), q);
  for (const must of [/placed by a person/, /benches with soft targets/, /not a bench/, /Every wallet here is ours/, /keeper's two addresses/, /testnet money/, /deliberate/]) {
    assert.ok(DATA.staged.quotes.some((q) => must.test(q)), `staged: ${must}`);
  }
  for (const l of DATA.limits) assert.ok(section(l.doc, l.section)?.includes(l.quote), l.quote);
  for (const must of [/^Drawdown/, /ForbiddenAsset/, /A stop on a seat/, /A passed challenge or a funded stage on a seat/, /Nobody has reviewed it/]) {
    assert.ok(DATA.limits.some((l) => must.test(l.quote)), `limits: ${must}`);
  }
});

test("the page's own words claim nothing the documents do not", () => {
  for (const text of OUR_WORDS) {
    const hits = NOT_CLAIMED.filter((re) => re.test(text)).map(String);
    assert.deepEqual(hits, [], text);
  }
  const intro = TEXT.intro.join(" ");
  for (const must of [/testnet/, /mock USDC/, /Every wallet in these records is ours/, /docs\/EVIDENCE\.md/, /docs\/EVIDENCE-SHARED-POOL\.md/]) {
    assert.match(intro, must);
  }
});

test("rows come in the documents' order, one group per section", () => {
  const all = groups(DATA.rows);
  const pairs = new Set(DATA.rows.map((r) => `${r.doc}#${r.section}`));
  assert.equal(all.length, pairs.size);
  for (const g of all) assert.ok(g.rows.every((r) => r.doc === g.doc && r.section === g.section), g.section);
  for (const [doc, d] of DOCS) {
    const seen = all.filter((g) => g.doc === doc).map((g) => d.order.indexOf(g.section));
    assert.deepEqual(seen, [...seen].sort((a, b) => a - b), doc);
  }
});

test("a quote cut at either end is marked, so a fragment does not pass for a sentence", () => {
  assert.equal(quoted("Every wallet here is ours."), "“Every wallet here is ours.”");
  assert.equal(quoted("it was run from a laptop"), "“…it was run from a laptop…”");
  assert.equal(quoted("0x914E4bf9… asks +1% on 11 USDC."), "“…0x914E4bf9… asks +1% on 11 USDC.”");
});

test("how to check: the receipt for a transaction, userFills for fills, then the row's own calls", () => {
  const stop = DATA.rows.find((r) => /DailyLoss/.test(r.record));
  assert.deepEqual(howToCheck(stop), ["eth_getTransactionReceipt for the transaction", "status() and breachReason() on the account"]);
  const trades = DATA.rows.find((r) => r.fills);
  assert.deepEqual(howToCheck(trades), [`userFills for ${trades.account} on Hyperliquid's testnet info API`]);
  for (const r of DATA.rows) assert.ok(howToCheck(r).length > 0, `${r.what}: says how to check it`);
});

test("the node's receipt is held against the row: success, block, sender and account", () => {
  const row = DATA.rows.find((r) => /DailyLoss/.test(r.record));
  const good = { status: 1, blockNumber: 65178469, from: row.from.toLowerCase(), to: row.account.toLowerCase() };
  assert.equal(receiptVerdict(row, good).ok, true);
  assert.match(receiptVerdict(row, good).text, /block 65178469/);
  for (const [bad, said] of [
    [{ ...good, status: 0 }, /reverted/],
    [{ ...good, blockNumber: 65178470 }, /names block 65178469/],
    [{ ...good, from: "0x00d014df2b4ffdb0654ea079e4792fd15a350fd4" }, /names the sender/],
    [{ ...good, to: "0xf4d98de2e668a8376319f54fc9592e948bd08655" }, /another account/],
  ]) {
    const v = receiptVerdict(row, bad);
    assert.equal(v.ok, false, said.source);
    assert.match(v.text, said);
  }
  assert.equal(receiptVerdict(row, null).ok, false);
  // A row that names no account is not held to where the transaction went.
  for (const unnamed of DATA.rows.filter((r) => r.tx && !r.account)) {
    const receipt = { status: 1, blockNumber: unnamed.block ?? 1, from: unnamed.from ?? "0x01", to: "0x02" };
    assert.equal(receiptVerdict(unnamed, receipt).ok, true, unnamed.what);
  }
});

test("Hyperliquid's fills are held against the hashes the row names", () => {
  const row = DATA.rows.find((r) => r.fills);
  const fill = (hash, side, px) => ({ hash, side, px, sz: "0.00013", coin: "BTC" });
  const both = [fill(row.fills[0], "B", "84619"), fill(row.fills[1], "A", "84618"), fill("0x" + "1".repeat(64), "B", "1")];
  const v = fillsVerdict(row, both);
  assert.equal(v.ok, true);
  assert.equal(v.text, "Hyperliquid lists both fills: bought 0.00013 BTC at 84619; sold 0.00013 BTC at 84618.");
  assert.equal(fillsVerdict(row, both.slice(1)).ok, false);
  assert.equal(fillsVerdict(row, []).ok, false);
});
