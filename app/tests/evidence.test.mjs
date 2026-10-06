// node --test app/tests/evidence.test.mjs
//
// The page "What happened on chain" (#/evidence) shows app/data/evidence.json. It is held to the two documents
// it comes from the way our submission texts are held to them: each source line of a row is found in its
// section of the document as written, and everything the row shows (account, transaction, block, sender, fills,
// record) is inside its own source lines. A row can then neither drift from the documents nor pair one account
// with another's transaction. The documents are read from this checkout, so on main they are main's.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { TEXT, EVENTS, groups, quoted, appLink, howToCheck, receiptVerdict, fillsVerdict, eventText } from "../lib/evidence.js";
import { DEPLOYMENTS } from "../lib/deployments.js";

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

// The documents' own lists of what the records do not show: the bullets under these two headings. The page's
// "What none of this shows" is taken from them, and LIMIT_LISTS is where that is written down.
const LIMIT_LISTS = [
  ["docs/EVIDENCE.md", "Not demonstrated yet"],
  ["docs/EVIDENCE-SHARED-POOL.md", "What this does not show"],
];
/** A heading that announces such a list, by its words. */
const SAYS_NOT_SHOWN = /\b(not|never|nothing) (shown?|demonstrated|recorded|proved|proven)\b|\bdoes(n't| not) (show|prove|demonstrate)\b/i;
/** The headings of a document, `##` and deeper, as written. */
const headings = (path) => [...readFileSync(join(ROOT, path), "utf8").matchAll(/^#{2,} +(.+?) *$/gm)].map((m) => m[1]);
/** The bullets of one `##` section, each as the rows quote the documents: one line, no markdown. */
function bullets(path, name) {
  const md = readFileSync(join(ROOT, path), "utf8");
  const part = md.split(/^## /m).slice(1).find((x) => x.slice(0, x.indexOf("\n")).trim() === name);
  if (part === undefined) return undefined;
  return part.slice(part.indexOf("\n") + 1).split(/^- /m).slice(1).map((b) => plain(b.split(/^#{2,} /m)[0]));
}
/** The sentence a bullet opens with: what the page shows of it. */
const opening = (text) => (text.match(/^.*?[.!?](?=\s|$)/) || [text])[0];

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
  // The chain shows WHICH address sent a transaction; it cannot show that nobody told it to.
  // A document may claim it where its own section carries the keeper's journal — 1 October 2026
  // the deployment 2 record claimed it from the chain alone, and the review bot, not a guard,
  // caught that. A phrase guard over the documents cannot tell the claim from its denial, so the
  // rule lives here, where it is exact: the page may not say it in its own voice.
  /\bunprompted\b/i, /nobody asked\b/i, /on its own initiative/i,
  // "Arm's length" means an independent party, and there is none here: the deployment 2 trader is
  // another AI agent of this team on a wallet we funded. The documents' own limit says so; the page
  // claimed the opposite in a row title until 1 October 2026.
  /arm.s[- ]length/i, /independent trader/i,
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
  assert.equal(DATA.rows.length, 42);
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
  assert.equal(cut.length, 3);
  for (const r of cut) assert.equal(Number(r.record.match(/cut at block (\d+)/)[1]), r.block, r.what);
});

test("every deployment record that names a registry lists the keys published into it", () => {
  // The document's whole "check it yourself" route starts at the record's `published_keys`: with
  // no list there is nothing to call `bindingOf` on, and a reader cannot rebuild the set at all.
  // Scanning the registry's events instead is not a way out -- the node rate limits
  // `eth_getLogs` over a range this wide, which is finding A-14 from the other side.
  //
  // `deployments/testnet-demo2.json` had no list, because the keys were published by hand after
  // the deploy script was refused and nothing wrote them back. The field is for a READER to
  // enumerate; the deploy's own key checks still read the chain and must not start trusting it
  // (ops/deploy_testnet.py, and the cross-registry trap its tests pin).
  // A record may borrow another's registry instead of standing up its own -- the shared
  // deployments do, which is why the rule is per REGISTRY and not per file.
  const dir = join(ROOT, "deployments");
  const records = readdirSync(dir).filter((f) => f.endsWith(".json"));
  assert.ok(records.length >= 3, "there are deployment records to check");
  const listed = new Map();
  const named = new Map();
  for (const file of records) {
    const rec = JSON.parse(readFileSync(join(dir, file), "utf8"));
    if (!rec.KeyRegistry) continue;
    named.set(rec.KeyRegistry, file);
    const keys = rec.published_keys;
    if (keys === undefined) continue;
    assert.ok(Array.isArray(keys) && keys.length > 0, `${file}: published_keys is a non-empty list or absent`);
    assert.equal(new Set(keys).size, keys.length, `${file}: no key is listed twice`);
    for (const k of keys) assert.match(k, /^0x[0-9a-fA-F]{40}$/, `${file}: ${k} is an address`);
    assert.ok(!listed.has(rec.KeyRegistry), `${rec.KeyRegistry}: listed by one record, not by ${file} and ${listed.get(rec.KeyRegistry)}`);
    listed.set(rec.KeyRegistry, file);
  }
  for (const [registry, file] of named) {
    assert.ok(listed.has(registry), `${registry}, named by ${file}, has its published keys in some record`);
  }
});

test("the check-it-yourself route warns that a pool's retired key may be a reservation", () => {
  // The route misled until 1 October 2026: it said a retired key was cut when its account
  // stopped, which is false for a pool. Buying a challenge binds TWO keys in one transaction, the
  // pool's being the funded-stage reservation, and a released reservation reads exactly like a
  // funded stage that ended with nothing broken. Only the event separates them, so the route has
  // to name it -- a reader who does not know this double-counts every challenge that did not pass.
  const how = section("docs/EVIDENCE.md", "Checking it yourself");
  assert.match(how, /retired key on a POOL is not by itself a funded stage/);
  assert.match(how, /TraderFunded/);
  assert.match(how, /published_keys/);
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
  assert.equal(leverage.length, 4);
  // The fourth was arranged with a separate AI agent of the team rather than staged by us, so it says so
  // in its own words. What may not happen is a leverage stop that claims nobody arranged it.
  for (const r of leverage) assert.ok(r.notes.some((n) => /were staged|deliberately, by us|arranged on purpose/.test(n)), `${r.what}: says it was arranged`);
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
  // The deployments the site reads, by label, from the config the deployments test holds to deployments/.
  const READ = DEPLOYMENTS.map((d) => d.label);
  assert.deepEqual(READ, ["demo2", "demo"]);
  assert.ok(DATA.rows.some((r) => r.factory === "demo2" && appLink(r) === `#/verify/${r.account}`),
    "deployment 2's rows open on the check-it-yourself page");
  for (const r of DATA.rows) {
    assert.ok([null, "challenge", "pool", "seat", "shared pool"].includes(r.kind), `${r.what}: kind ${r.kind}`);
    // Each section's rows belong to one deployment. The site reads two (app/config.js, since 1 Oct 2026): demo2,
    // live, and demo, the archive -- a row of either gets its link, a row of any other (the rehearsal) none.
    const expected = { "The fourth ending, on the rehearsal deployment": "rehearsal",
                       "Deployment 2, and the first run where the trader was not us": "demo2",
                       "Deployment 2: a pass, and a funded-stage position closed at a loss by our own take": "demo2",
                       "The pool on the second deployment (3 October)": "demo2",
                       "A challenge sold on the seat, and started by the host's keeper (4 October)": "demo2",
                       "The seat's challenge runs out, the host settles it, and both depositors leave (5 October)": "demo2",
                       "A second pool on the second deployment, with three seats (5 October)": "demo2" };
    assert.equal(r.factory, expected[r.section] ?? "demo", r.what);
    const link = appLink(r);
    if (!READ.includes(r.factory) || !r.account) assert.equal(link, null, `${r.what}: the app does not read it`);
    else if (r.kind === "shared pool") {
      assert.ok(pools.includes(r.account), `${r.what}: one of the shared pools in the table`);
      assert.equal(link, `#/shared/${r.account}`);
    } else assert.equal(link, `#/verify/${r.account}`, r.what);
  }
});

test("the shared pools are the document's table, cell for cell", () => {
  const body = section(DATA.pools.doc, DATA.pools.section);
  assert.equal(DATA.pools.rows.length, 5);
  for (const p of DATA.pools.rows) {
    const line = `| ${p.pool} | ${p.factory} | ${p.open} | ${p.showed} |`;
    assert.ok(body.includes(line), line);
    // Each opens by its own address: `#/shared` alone is the live deployment's pool, which is none of these.
    assert.equal(p.open, `#/shared/${p.pool}`);
  }
});

test("what was staged, and what none of this shows, are quoted from their sections", () => {
  const staged = section(DATA.staged.doc, DATA.staged.section);
  for (const q of DATA.staged.quotes) assert.ok(staged.includes(q), q);
  for (const must of [/placed by a person/, /benches with soft targets/, /not a bench/, /Every wallet here is ours/, /keeper's two addresses/, /testnet money/, /deliberate/]) {
    assert.ok(DATA.staged.quotes.some((q) => must.test(q)), `staged: ${must}`);
  }
  for (const l of DATA.limits) assert.ok(section(l.doc, l.section)?.includes(l.quote), l.quote);
  for (const must of [/^Drawdown/, /ForbiddenAsset/, /A stop on a seat/, /A passed challenge or a funded stage on a seat/, /A holder leaving with a gain/, /A trader we do not control on a seat/, /Nobody has reviewed it/]) {
    assert.ok(DATA.limits.some((l) => must.test(l.quote)), `limits: ${must}`);
  }
});

test("what none of this shows is every bullet of the documents' own lists, in their order", () => {
  for (const [doc, name] of LIMIT_LISTS) {
    const listed = bullets(doc, name);
    assert.ok(listed, `${doc} has no section "${name}": the page's limits are taken from it`);
    assert.ok(listed.length > 0, `${doc}, "${name}": the section holds no bullets`);
    const shown = DATA.limits.filter((l) => l.doc === doc && l.section === name).map((l) => l.quote);
    // One line on the page for each bullet, in the document's order, each the bullet's opening sentence. A bullet
    // added to the document and not to the page is a limit the page would keep quiet about; a line left on the
    // page after its bullet has gone is a limit the documents no longer state.
    assert.deepEqual(shown, listed.map(opening), `${doc}, "${name}"`);
    for (const q of shown) assert.match(q, /[.!?]$/, `«${q}» is a whole sentence`);
  }
  // Today's lists, so that a parser that read nothing cannot pass: one bullet in the first, four in the second.
  assert.deepEqual(bullets("docs/EVIDENCE.md", "Not demonstrated yet").map(opening),
    ["Drawdown, the last of the three rules the pools enforce."]);
  assert.deepEqual(bullets("docs/EVIDENCE-SHARED-POOL.md", "What this does not show").map(opening), [
    "A passed challenge or a funded stage on a seat, and the funded term running out.",
    "A holder leaving with a gain.",
    "A trader we do not control on a seat.",
    "A stop on a seat.",
  ]);
  // A bullet's second sentence does not leak into its line: "A stop on a seat. The rules above would stop…".
  assert.equal(opening("A stop on a seat. The rules above would stop the challenge at 2.70 USDC of equity."), "A stop on a seat.");
  assert.equal(opening("No full stop here"), "No full stop here");
  // A new list of the same kind in either document has to be named here, or its bullets reach no page.
  const named = new Set(LIMIT_LISTS.map(([doc, name]) => `${doc}#${name}`));
  for (const doc of DOCS.keys()) {
    for (const h of headings(doc)) {
      if (SAYS_NOT_SHOWN.test(h)) assert.ok(named.has(`${doc}#${h}`), `${doc}: the list "${h}" is not on the page`);
    }
  }
  assert.equal(SAYS_NOT_SHOWN.test("What this does not show"), true);
  assert.equal(SAYS_NOT_SHOWN.test("Not demonstrated yet"), true);
  assert.equal(SAYS_NOT_SHOWN.test("What is staged, and what is not"), false);
  // The lines that are not bullets of a list stay tied to their own sections by the test above.
  const outside = DATA.limits.filter((l) => !named.has(`${l.doc}#${l.section}`)).map((l) => l.section);
  assert.deepEqual(outside, ["ForbiddenAsset, and why it is not in the list above", ""]);
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
  // A row whose only evidence is fills says just that. Picked by "fills and no transaction" rather
  // than "the first row with fills": a later row gained both, and `find` would have taken it.
  const trades = DATA.rows.find((r) => r.fills && !r.tx);
  assert.deepEqual(howToCheck(trades), [`userFills for ${trades.account} on Hyperliquid's testnet info API`]);
  const both = DATA.rows.find((r) => r.fills && r.tx);
  assert.deepEqual(howToCheck(both), ["eth_getTransactionReceipt for the transaction", ...both.check,
                                      `userFills for ${both.account} on Hyperliquid's testnet info API`]);
  for (const r of DATA.rows) assert.ok(howToCheck(r).length > 0, `${r.what}: says how to check it`);
});

test("the node's receipt is held against the row: success, block, sender and account", () => {
  const row = DATA.rows.find((r) => /DailyLoss/.test(r.record));
  const good = { status: 1, blockNumber: 65178469, from: row.from.toLowerCase(), to: row.account.toLowerCase() };
  assert.equal(receiptVerdict(row, good).ok, true);
  assert.match(receiptVerdict(row, good).text, /block 65178469/);
  // Only what was compared is called agreed: the record (a call's answer here) is not in the receipt.
  assert.match(receiptVerdict(row, good).text, /Block, sender and account are as the row says\. This button does not read the record itself\.$/);
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

// The contracts' own declarations, an enum written as the uint8 it is in the ABI.
const SOURCES = ["src/Pool.sol", "src/ChallengeAccount.sol"].map((p) => readFileSync(join(ROOT, p), "utf8")).join("\n");
const declared = (name) => {
  const m = SOURCES.match(new RegExp(`event ${name}\\(([^)]*)\\);`));
  return m && `event ${name}(${m[1].replace(/\b(Status|Breach)\b/g, "uint8").replace(/\s+/g, " ").trim()})`;
};
const params = (line) => line.slice(line.indexOf("(") + 1, -1).split(",").map((p) => p.trim().split(" ").pop());

test("the events the page reads are the contracts' own, field for field", () => {
  for (const line of EVENTS) {
    const name = line.match(/^event (\w+)\(/)[1];
    assert.equal(line, declared(name), name);
  }
});

test("a row that names an event carries it as the document writes it", () => {
  const named = DATA.rows.filter((r) => r.check.some((c) => /\b(FundedResult|FundedPayoutSent|Stopped)\(/.test(c)));
  assert.equal(named.length, 6);
  for (const r of named) {
    assert.ok(r.event, `${r.what}: carries the event it names`);
    assert.ok(r.sources.some((s) => s.includes(r.event.text)), `${r.what}: ${r.event.text} is in its sources`);
    assert.ok(r.event.text.startsWith(`${r.event.name}(`), r.what);
    const line = EVENTS.find((l) => l.startsWith(`event ${r.event.name}(`));
    assert.ok(line, `${r.what}: the page can decode ${r.event.name}`);
    for (const [k, v] of Object.entries(r.event.values)) {
      assert.ok(params(line).includes(k), `${r.what}: ${k} is a field of ${r.event.name}`);
      assert.ok(hasNumber(r.event.text, v), `${r.what}: ${k} ${v} is what the document writes`);
    }
  }
});

test("the node's receipt bears out a named event only with its values, from the row's account", () => {
  // The stage that paid, of 25 September: two rows name a FundedResult now, and the other one paid nothing.
  const row = DATA.rows.find((r) => r.event?.name === "FundedResult" && r.event.values.payout === "1017840");
  const receipt = { status: 1, blockNumber: 65199083, from: "0xdc87191c63ab838434806d6dc4752904efab59b0", to: row.account };
  const event = { address: row.account.toUpperCase().replace("0X", "0x"), name: "FundedResult",
    args: { trader: "0xdc87191c63ab838434806d6dc4752904efab59b0", realized: 10012723n, payout: 1017840n } };
  const ok = receiptVerdict(row, receipt, [event]);
  assert.equal(ok.ok, true);
  assert.equal(ok.text, `The node says: block 65199083, from ${receipt.from}, to ${receipt.to}, succeeded. `
    + `Account and the event ${eventText(row.event)} are as the row says.`);
  for (const [bad, said] of [
    [[], /carry no FundedResult\(realized 10012723, payout 1017840\)/],
    [[{ ...event, args: { ...event.args, payout: 1017841n } }], /carry no FundedResult/],
    [[{ ...event, address: "0xf4d98de2e668a8376319f54fc9592e948bd08655" }], /carry no FundedResult/],
    [[{ ...event, name: "FundedPayoutSent" }], /carry no FundedResult/],
  ]) {
    const v = receiptVerdict(row, receipt, bad);
    assert.equal(v.ok, false, said.source);
    assert.match(v.text, said);
  }
});
