// node --test app/tests/simsite.test.mjs
//
// A site of simulation pools (app/config.js, `sim`; app/lib/simsite.js): the pools of one SimPoolFactory and no other
// pool. The first page lists them; a pool's page, its panels and an exam's screen open only for a pool the factory's
// own list names; and the pages of a PoolFactory's pools are not part of the site. Testnet's site names no such
// factory and opens a simulation pool by its address, as it did. A mainnet site is built in a directory of its own
// (app/tests/mainnet-site.mjs).
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CONFIG } from "../config.js";
import { simSite, isSitePool, noContract, simListHtml, NOT_FOUND } from "../lib/simsite.js";
import { indexUrl, askIndex, sourceNote, indexBase } from "../lib/simindex.js";
import { reviewsUrl, askReviews } from "../lib/simoperator.js";
import { needsGate } from "../lib/nav.js";
import { mainnetSite, SIM_FACTORY } from "./mainnet-site.mjs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const POOL = "0x7A4999c02E749933af456d0Ac241a10584315812";
const OTHER = "0x00000000000000000000000000000000000000b1";
const short = (a) => `${a.slice(0, 6)}…${a.slice(-4)}`;
const built = mainnetSite();
after(() => built.remove());

test("testnet's site names no factory of simulation pools; a mainnet site names one, and no other deployment", async () => {
  assert.equal(simSite(), null);
  assert.equal(simSite(CONFIG), null);
  assert.equal(simSite(null), null);
  const { CONFIG: main } = await import(new URL(`file://${built.site}/config.js`).href);
  assert.deepEqual(simSite(main), { label: "sim-main", factory: SIM_FACTORY });
  const lib = await built.lib("simsite.js");
  assert.deepEqual(lib.simSite(), { label: "sim-main", factory: SIM_FACTORY });
  // None of the pools a PoolFactory makes: nothing live, nothing archived, nothing read-only, no shared pool.
  const deployments = await built.lib("deployments.js");
  assert.deepEqual(deployments.DEPLOYMENTS, []);
  assert.deepEqual(deployments.READ_ONLY_DEPLOYMENTS, []);
  assert.deepEqual(deployments.everyDeployment(), []);
  assert.equal(deployments.liveDeployment(), null);
});

test("a pool is the site's when the factory's own list names it, and no other address is", () => {
  const pools = [POOL, "0x00000000000000000000000000000000000000A2"];
  assert.equal(isSitePool(pools, POOL), true);
  assert.equal(isSitePool(pools, POOL.toLowerCase()), true, "case aside");
  assert.equal(isSitePool(pools, "0x00000000000000000000000000000000000000a2"), true);
  assert.equal(isSitePool(pools, OTHER), false);
  assert.equal(isSitePool([], POOL), false, "a factory with no pools has none");
  assert.equal(isSitePool(null, POOL), false, "no factory at the address: no pool is the site's");
  assert.equal(isSitePool(pools, null), false, "an exam of no pool");
  assert.equal(isSitePool(pools, undefined), false);
  assert.equal(isSitePool(pools, ""), false);
});

test("no contract at the factory's address is told from a read that failed", () => {
  // ethers 6.17.0, a view answered with no bytes.
  assert.equal(noContract({ code: "BAD_DATA", value: "0x", shortMessage: "could not decode result data" }), true);
  // The node's rate limit inside a batch is BAD_DATA too, with the node's answer as its value: a failed read.
  assert.equal(noContract({ code: "BAD_DATA", value: { error: { code: -32005, message: "rate limited" } } }), false);
  assert.equal(noContract({ code: "BAD_DATA", value: [{ error: { code: -32005 } }] }), false);
  assert.equal(noContract({ code: "CALL_EXCEPTION", data: "0x" }), false);
  assert.equal(noContract(new TypeError("Failed to fetch")), false);
  assert.equal(noContract(null), false);
  const view = read("../views/simsite.js");
  assert.match(view, /catch \(err\) \{\s+if \(noContract\(err\)\) return null;\s+throw err;\s+\}/, "any other failure is the page's failure, not an empty list");
});

test("the first page lists the factory's pools, a link each; with none it says so, and with no factory yet that", () => {
  const html = simListHtml([POOL, OTHER], { network: "Hyperliquid", short });
  assert.match(html, /<h2>Pools<\/h2>/);
  assert.deepEqual([...html.matchAll(/<a href="([^"]+)">([^<]+)<\/a>/g)].map((m) => [m[1], m[2]]),
    [[`#/sim/${POOL}`, `Pool ${short(POOL)}`], [`#/sim/${OTHER}`, `Pool ${short(OTHER)}`]]);
  assert.equal(simListHtml([], { network: "Hyperliquid", short }), `<section class="card"><h2>Pools</h2><p>No pools yet.</p></section>`);
  const none = simListHtml(null, { network: "Hyperliquid", short });
  assert.match(none, /<h2>Not deployed yet<\/h2>\s*<p>The contracts are not on Hyperliquid yet, so there is nothing to list\.<\/p>/);
  assert.doesNotMatch(none, /href=/);
  // The network is the settings' name for it, and what is not the page's own text is escaped.
  assert.match(simListHtml(null, { network: "Hyperliquid testnet", short }), /not on Hyperliquid testnet yet/);
  assert.match(simListHtml(null, { network: "<b>", short }), /not on &lt;b&gt; yet/);
  assert.doesNotMatch(simListHtml(['"><img>'], { network: "x", short: (a) => a }), /<img>/);
  // No page of a PoolFactory's pools is linked from it, and nothing is said of a network in the page's own words.
  for (const page of [html, none]) assert.doesNotMatch(page, /#\/(new|pool|shared|economics|verify|evidence|terms)|testnet|mock/i);
  const view = read("../views/simsite.js");
  assert.match(view, /render\(page, simListHtml\(await sitePools\(\), \{ network: networkName\(\), short: chain\.short \}\)\);/);
  assert.match(view, /new ethers\.Contract\(simSite\(\)\.factory, SIM_FACTORY_ABI, chain\.readProvider\)\.pools\(\)/, "the list is the factory's own, read from the chain");
});

test("a site of simulation pools routes its first page to the list and has none of the other pools' pages", () => {
  const app = read("../app.js");
  assert.match(app, /const routes = simSite\(\) \? \[\[\/\^#\?\\\/\?\$\/, \(m, page\) => simListView\(page\)\], \.\.\.simRoutes\] : \[\.\.\.poolRoutes, \.\.\.simRoutes\];/);
  const [pools, sims] = [app.slice(app.indexOf("const poolRoutes = ["), app.indexOf("const simRoutes = [")),
    app.slice(app.indexOf("const simRoutes = ["), app.indexOf("const routes = simSite()"))];
  // Every page of a PoolFactory's pools, and the terms of their demo, is in the one list a site of simulation pools leaves out.
  for (const view of ["listView", "newPoolView", "poolView", "challengeView", "verifyView", "economicsView", "sharedView", "evidenceView", "termsView"]) {
    assert.match(pools, new RegExp(`=> ${view}\\(`), `${view} is not a route of the pools' site`);
    assert.doesNotMatch(sims, new RegExp(`=> ${view}\\(`), `${view} is a route of a site of simulation pools`);
  }
  for (const view of ["simPartnerView", "simPoolView", "simOperatorView", "simTradeView", "simExamsView", "simPayoutsView", "simAdminView", "examView"]) {
    assert.match(sims, new RegExp(`=> ${view}\\(`), `${view} is not a route of the simulation pools`);
    assert.doesNotMatch(pools, new RegExp(`=> ${view}\\(`));
  }
  // An address the site has no page for is "Not found", in the words the site always had.
  assert.match(app, /return \(page\) => render\(page, NOT_FOUND\);/);
  assert.equal(NOT_FOUND, `<section class="card"><h2>Not found</h2><p><a href="#/">Back to the pools</a></p></section>`);
  // No gateway is named on such a site, so the footer asks none for its commit.
  assert.match(app, /if \(!link \|\| !CONFIG\.gateway\) return;\s+fetch\(`\$\{CONFIG\.gateway\}\/v1\/health`/);
});

test("a page of a pool is drawn only for a pool of the site's factory; on a site that names none, for any address", () => {
  const app = read("../app.js");
  // Every route of the panels carries the pool's address first, and the router asks before the page's own view runs.
  assert.match(app, /const PANEL_POOL = \/\^#\\\/\(\?:sim\|manage\)\\\/\(0x\[0-9a-fA-F\]\{40\}\)\/;/);
  assert.match(app, /const pool = PANEL_POOL\.exec\(hash\)\?\.\[1\];\s+return pool \? async \(page\) => \(\(await sitePool\(pool, page\)\) \? handler\(m, page\) : undefined\) : \(page\) => handler\(m, page\);/);
  const PANEL_POOL = /^#\/(?:sim|manage)\/(0x[0-9a-fA-F]{40})/;
  for (const tail of ["", "?code=abc", "/partner", "/trade", "/exams", "/payouts"]) assert.equal(PANEL_POOL.exec(`#/sim/${POOL}${tail}`)?.[1], POOL);
  for (const tail of ["", "/operator"]) assert.equal(PANEL_POOL.exec(`#/manage/${POOL}${tail}`)?.[1], POOL);
  for (const hash of ["#/", `#/exam/${POOL}`, "#/exam", `#/pool/${POOL}`, "#/sim", "#/manage"]) assert.equal(PANEL_POOL.exec(hash), null);
  const view = read("../views/simsite.js");
  const check = view.slice(view.indexOf("export async function sitePool("));
  assert.match(check, /^export async function sitePool\(pool, page\) \{\s+if \(!simSite\(\)\) return true;\s+if \(isSitePool\(named, pool\) \|\| isSitePool\(await sitePools\(\), pool\)\) return true;/);
  // A pool the factory named once is not asked for again (its list only grows); the list kept is the chain's own, as last
  // read, and an address it does not name is read for afresh, never taken from anything but the factory.
  assert.match(view, /^let named = \[\];$/m);
  assert.match(view, /named = \[\.\.\.\(await new ethers\.Contract\(simSite\(\)\.factory, SIM_FACTORY_ABI, chain\.readProvider\)\.pools\(\)\)\];\s+return named;/);
  assert.equal([...view.matchAll(/\bnamed = /g)].length, 2, "the kept list is written where it is declared and where the factory is read, nowhere else");
  assert.match(read("../../src/sim/SimPoolFactory.sol"), /_pools\.push\(pool\);/);
  assert.equal([...read("../../src/sim/SimPoolFactory.sol").matchAll(/_pools\b/g)].length, 3, "the factory's list is declared, pushed to and returned: nothing takes a pool out of it");
  assert.match(check, /if \(page\.isConnected\) delete document\.body\.dataset\.panel;\s+render\(page, NOT_FOUND\);\s+return false;/, "the page is the site's own, not a panel's");
  // An exam's screen: its pool is asked of the exam and held to the factory's list before anything of the exam is read.
  const exam = read("../views/exam.js");
  assert.match(exam, /const ofSite = simSite\(\) \? await simPoolOf\(address\) : null;\s+if \(simSite\(\) && !\(await sitePool\(ofSite, page\)\)\) return;\s+const exam = new ethers\.Contract\(address, PAPER_EXAM_ABI, chain\.readProvider\);/);
  assert.match(exam, /simSite\(\) \? ofSite : simPoolOf\(address\)\]\);/);
});

test("the entry question is asked again on mainnet: testnet's answer is kept under testnet's name only", () => {
  const app = read("../app.js");
  assert.match(app, /const GATE_KEY = isMainnet\(\) \? "pools-gate-v1-mainnet" : "pools-gate-v1";/);
  // The terms of the demo are not a page of a site of simulation pools, so every page of it asks.
  for (const hash of ["#/", `#/sim/${POOL}`, `#/manage/${POOL}`, `#/exam/${POOL}`]) assert.equal(needsGate(hash, false), true);
  assert.match(built.index, /<dialog id="gate">[\s\S]*id="gate-agree"[\s\S]*id="gate-ok" disabled/);
});

test("a site that names no index says so, and its panels read the chain themselves", async () => {
  assert.equal(indexBase({}), undefined);
  assert.equal(indexBase({ simIndex: "https://a.example/sim" }), "https://a.example/sim");
  assert.equal(indexBase({ simIndex: "https://a.example/sim", gateway: "https://b.example" }), "https://a.example/sim");
  assert.equal(indexBase(CONFIG), CONFIG.gateway, "testnet's index is on its gateway's host, as it was");
  assert.throws(() => indexUrl(undefined, POOL), /this site names no index/);
  assert.throws(() => reviewsUrl(undefined, POOL, [1]), /this site names no index/);
  let asked = 0;
  const fetch = async () => { asked += 1; throw new Error("not asked"); };
  const answer = await askIndex({ base: undefined, pool: POOL, chainId: 999, fetch });
  assert.deepEqual(answer, { ok: false, why: "this site names no index" });
  assert.match(sourceNote(answer), /^The pool's index on the host is not available \(this site names no index\), so this page reads the pool's events from the chain itself/);
  const reviews = await askReviews({ base: undefined, pool: POOL, ids: [3], chainId: 999, fetch });
  assert.equal(reviews.failed, "this site names no index");
  assert.deepEqual(reviews.docs, {});
  assert.equal(asked, 0, "nothing is fetched from an address that was never named");
  // A mainnet site whose entry names where its index answers asks there.
  const indexed = mainnetSite({ gateway: "https://pools-api.usenami.io/sim" });
  try {
    const { CONFIG: main } = await import(new URL(`file://${indexed.site}/config.js`).href);
    assert.equal(indexBase(main), "https://pools-api.usenami.io/sim");
    assert.equal(indexUrl(indexBase(main), POOL), `https://pools-api.usenami.io/sim/v1/simindex/pool?pool=${POOL}`);
  } finally {
    indexed.remove();
  }
});
