// node --test app/tests/mainnet-routes.test.mjs
//
// The router of a mainnet site, run: the real app.js of a mainnet build (app/tests/mainnet-site.mjs) is loaded with
// stand-ins for the browser and for ethers, and every address change goes through its own `route()`. The stand-in
// for ethers records each contract a page builds and each read it makes, and answers only what a test set up: the
// factory's `pools()`, an exam's `pool()` and a pool's `accountOf`. So a test can say what was drawn for an address
// and that nothing of that address was read. What the other tests hold by the lines of app.js is held here by what
// the router does. This file has the stand-ins to itself: node runs each test file apart.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { mainnetSite, SIM_FACTORY } from "./mainnet-site.mjs";

const LISTED = "0x1111111111111111111111111111111111111111";
const OTHER = "0x6666666666666666666666666666666666666666";
const EXAM = "0x7777777777777777777777777777777777777777";
const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();

// What the chain answers: the factory's pools, a failure of that read, and the exams: `exams` maps a contract to the
// pool its `pool()` names, and `disowned` holds those the named pool's `accountOf` does not know (a contract that only
// says it is a pool's exam).
const chain = { pools: [LISTED], fail: null, exams: new Map(), disowned: new Set(), built: [], reads: [] };

class Contract {
  constructor(address) {
    chain.built.push(String(address));
    return new Proxy({}, { get: (_, key) => (key === "then" ? undefined : async (...args) => {
      chain.reads.push(`${address}.${String(key)}`);
      if (same(address, SIM_FACTORY) && key === "pools") {
        if (chain.fail) throw chain.fail;
        return chain.pools;
      }
      if (key === "pool" && chain.exams.has(String(address).toLowerCase())) return chain.exams.get(String(address).toLowerCase());
      if (key === "accountOf") {
        const asked = String(args[0]).toLowerCase();
        return { kind: same(chain.exams.get(asked), address) && !chain.disowned.has(asked) ? 1n : 0n };
      }
      // Anything else is a read no test set up: the node not answering, as a page sees it.
      throw Object.assign(new Error("no answer"), { code: "NETWORK_ERROR" });
    }) });
  }
}
// ethers as the modules ask for it while they load, with the contracts above.
const ethers = new Proxy(function () {}, {
  get: (target, key) => (key === Symbol.toPrimitive ? () => "" : key === "then" ? undefined
    : key === "Contract" ? Contract : key === "getAddress" ? (a) => a : ethers),
  apply: () => ethers,
  construct: () => ethers,
});
const element = () => {
  const e = { innerHTML: "", textContent: "", dataset: {}, isConnected: true, className: "", hidden: false, style: {}, open: false,
    addEventListener() {}, removeEventListener() {}, querySelector: () => element(), querySelectorAll: () => [],
    replaceChildren(child) { e.child = child; }, after() {}, showModal() {}, close() {} };
  return e;
};
const listeners = {};
const view = element();
const body = element();
globalThis.document = { createElement: element, getElementById: () => view, querySelector: element, addEventListener() {}, body };
globalThis.window = { ethers, ethereum: undefined, sessionStorage: { getItem: () => null, setItem() {} },
  addEventListener: (name, fn) => { listeners[name] = fn; }, removeEventListener() {}, scrollTo() {}, scrollY: 0, confirm: () => false };
globalThis.ethers = ethers;
globalThis.location = { hash: "#/" };
globalThis.history = { state: null, replaceState() {} };
globalThis.localStorage = { getItem: () => "ok", setItem() {} };  // the entry question is answered
globalThis.alert = () => {};
// The node says which chain it is (the exam's screen asks it directly), and the site serves its Terms of Use from its
// own folder unless a test takes them away (`terms.down`); nothing else on the network answers.
const terms = { asked: [], down: false };
globalThis.fetch = async (url, init) => {
  if (/eth_chainId/.test(init?.body ?? "")) return { ok: true, status: 200, json: async () => ({ jsonrpc: "2.0", id: 1, result: "0x3e7" }) };
  if (String(url) === "./data/terms-mainnet.md") {
    terms.asked.push(String(url));
    if (!terms.down) return { ok: true, status: 200, text: async () => readFileSync(join(built.site, "data/terms-mainnet.md"), "utf8") };
  }
  return { ok: false, status: 500, json: async () => null };
};
// A wait of the reader (a retry, a poll) is not sat out: only the few milliseconds reads are gathered in.
const timeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => (ms > 100 ? 0 : timeout(fn, ms, ...args));
globalThis.setInterval = () => 0;

const built = mainnetSite();
after(() => built.remove());
const { CONFIG } = await import(pathToFileURL(join(built.site, "config.js")).href);
await import(pathToFileURL(join(built.site, "app.js")).href);

/** Opens `hash` the way the browser does and answers what was drawn, the contracts built other than the factory, and
 *  whether the page stands in a panel. */
async function open(hash) {
  chain.built = [];
  chain.reads = [];
  globalThis.location.hash = hash;
  await listeners.hashchange();
  return { html: view.child.innerHTML, built: chain.built.filter((a) => !same(a, SIM_FACTORY)), reads: [...chain.reads], panel: body.dataset.panel ?? null };
}
const NOT_FOUND = /<h2>Not found<\/h2><p><a href="#\/">Back to the pools<\/a><\/p>/;
const refused = (page) => NOT_FOUND.test(page.html) && page.built.length === 0;
const PANELS = (pool) => [`#/sim/${pool}`, `#/sim/${pool}?code=x`, `#/sim/${pool}/trade`, `#/sim/${pool}/exams`, `#/sim/${pool}/payouts`,
  `#/sim/${pool}/partner`, `#/manage/${pool}`, `#/manage/${pool}/operator`];

test("the site under test is a mainnet build of one factory, and its first page lists that factory's pools", async () => {
  assert.equal(CONFIG.chainId, 999);
  assert.equal(CONFIG.sim.factory, SIM_FACTORY);
  const page = await open("#/");
  assert.match(page.html, new RegExp(`<a href="#/sim/${LISTED}">Pool 0x1111…1111</a>`));
  assert.deepEqual(page.reads, [`${SIM_FACTORY}.pools`]);
  assert.equal(page.panel, null);
});

test("a pool the factory's list does not name: every page of the panels is Not found, and nothing of it is read", async () => {
  for (const hash of PANELS(OTHER)) {
    const page = await open(hash);
    assert.ok(refused(page), `${hash}: drew ${page.html.slice(0, 80)}, built ${page.built}`);
    assert.deepEqual(page.reads, [`${SIM_FACTORY}.pools`], `${hash}: only the factory's list is asked`);
    assert.equal(page.panel, null, `${hash}: the site's own header, not a panel's`);
  }
});

test("a pool the list names opens, in whichever case its address is written, and is not asked of the factory twice", async () => {
  for (const address of [LISTED, LISTED.replace("0x", "0X").toUpperCase().replace("0X", "0x")]) {
    const page = await open(`#/sim/${address}`);
    assert.ok(page.built.some((a) => same(a, LISTED)), `${address}: the pool's own reads did not start`);
    assert.doesNotMatch(page.html, NOT_FOUND);
    assert.ok(!page.reads.includes(`${SIM_FACTORY}.pools`), "a pool the list named once is the site's for good");
  }
});

test("an address with a space, a character more, or in a route of the other pools is Not found", async () => {
  for (const hash of [`#/sim/${LISTED}%20`, `#/sim/${LISTED} `, `#/sim/ ${LISTED}`, `#/sim/${LISTED}0`, `#/sim/${LISTED}/trade/`,
    `#/sim/${LISTED}\n`, `#/sim/${LISTED}//partner`, `#/manage/${LISTED}/exams`, `#/pool/${LISTED}`, `#/challenge/${LISTED}`,
    `#/shared/${LISTED}`, "#/shared", "#/new", "#/verify", "#/economics", "#/evidence"]) {
    const page = await open(hash);
    assert.ok(refused(page), `${JSON.stringify(hash)}: drew ${page.html.slice(0, 60)}, built ${page.built}`);
    assert.deepEqual(page.reads, [], `${JSON.stringify(hash)}: no page of the site, so nothing is read`);
  }
});

test("the terms page of a mainnet site is its own Terms of Use, read from its folder and drawn whole; a text that did not arrive is a failure", async () => {
  terms.asked = [];
  const page = await open("#/terms");
  assert.deepEqual(terms.asked, ["./data/terms-mainnet.md"]);
  assert.match(page.html, /^<section class="card narrow terms"><h2>Terms of Use<\/h2>/);
  assert.equal([...page.html.matchAll(/<h3>/g)].length, 15, "the fifteen sections");
  assert.match(page.html, /<h3>15\. Contact<\/h3>\n<p>business@usenami\.io<\/p>\n<p>https:\/\/pools\.usenami\.io\/<\/p><\/section>$/);
  assert.equal([...page.html.matchAll(/\[TO CONFIRM/g)].length, 10, "the ten places to fill in are shown as they are");
  assert.deepEqual(page.reads, [], "nothing of the chain is read for the terms");
  assert.equal(page.panel, null);
  terms.down = true;
  try {
    const down = await open("#/terms");
    assert.match(down.html, /<h2>Could not load this page<\/h2>/);
    assert.match(down.html, /the Terms of Use could not be read \(500\)/);
    assert.match(down.html, /<button id="try-again">Try again<\/button>/);
  } finally {
    terms.down = false;
  }
});

test("when the factory's list cannot be read, a page fails with its cause and a way to try again, and reads nothing of the address", async () => {
  chain.fail = Object.assign(new Error("no answer"), { code: "NETWORK_ERROR" });
  try {
    for (const hash of [`#/sim/${OTHER}`, `#/manage/${OTHER}`, `#/manage/${OTHER}/operator`]) {
      const page = await open(hash);
      assert.match(page.html, /<h2>Could not load this page<\/h2>/, hash);
      assert.match(page.html, /<button id="try-again">Try again<\/button>/, hash);
      assert.doesNotMatch(page.html, NOT_FOUND, "a list that was not read says nothing of the address");
      assert.deepEqual(page.built, [], hash);
    }
    // The first page says the same: a list that was not read is not an empty one.
    assert.match((await open("#/")).html, /<h2>Could not load this page<\/h2>/);
  } finally {
    chain.fail = null;
  }
});

test("with no contract at the factory's address the first page says so, and no address is a pool of the site", async () => {
  chain.fail = Object.assign(new Error("could not decode result data"), { code: "BAD_DATA", value: "0x" });
  try {
    const first = await open("#/");
    assert.match(first.html, /<h2>Not deployed yet<\/h2>\s*<p>The contracts are not on Hyperliquid yet, so there is nothing to list\.<\/p>/);
    assert.ok(refused(await open(`#/sim/${OTHER}`)));
    assert.ok(refused(await open(`#/manage/${OTHER}/operator`)));
  } finally {
    chain.fail = null;
  }
});

test("an exam's screen: Not found for an exam whose pool the list does not name, or that the pool does not own", async () => {
  const ownReads = (page) => page.reads.filter((r) => /\.(rules|terms|trader)$/.test(r));
  try {
    // Its `pool()` names a pool the factory never made.
    chain.exams = new Map([[EXAM.toLowerCase(), OTHER]]);
    let page = await open(`#/exam/${EXAM}`);
    assert.match(page.html, NOT_FOUND);
    assert.equal(page.panel, null);
    assert.deepEqual(ownReads(page), [], "nothing of the exam itself is read");
    // Its `pool()` names a listed pool, and that pool's `accountOf` does not know it: a contract that only says so.
    chain.exams = new Map([[EXAM.toLowerCase(), LISTED]]);
    chain.disowned = new Set([EXAM.toLowerCase()]);
    page = await open(`#/exam/${EXAM}`);
    assert.match(page.html, NOT_FOUND);
    assert.deepEqual(page.reads, [`${EXAM}.pool`, `${LISTED}.accountOf`], "the pool answers that the address is none of its accounts, and nothing more is read");
    assert.equal(page.panel, null);
    // A contract with no `pool()` at all: the read fails on its way in this stand-in, so the page fails; it is not drawn.
    page = await open(`#/exam/${OTHER}`);
    assert.match(page.html, /<h2>Could not load this page<\/h2>/);
    assert.deepEqual(ownReads(page), []);
  } finally {
    chain.exams = new Map();
    chain.disowned = new Set();
  }
});

test("an exam's screen opens for an exam of a listed pool that the pool owns, and for that alone", async () => {
  chain.exams = new Map([[EXAM.toLowerCase(), LISTED]]);
  const page = await open(`#/exam/${EXAM}`);
  assert.deepEqual(page.reads.slice(0, 2), [`${EXAM}.pool`, `${LISTED}.accountOf`], "the pool is asked of the exam, and the exam of the pool, first");
  assert.ok(page.reads.some((r) => r === `${EXAM}.rules`), `the screen went on to the exam's own reads: ${page.reads}`);
  assert.doesNotMatch(page.html, NOT_FOUND);
});

// Last, since it loads a second site over the first: mainnet settings with no factory in them. scripts/app_config.py
// writes none such; here they are the build's own with `sim` taken out, as a hand or an older script might leave them.
test("mainnet settings that name no factory: no page of a pool, no panel and no exam's screen opens, and nothing is read", async () => {
  const bare = mainnetSite();
  try {
    const file = join(bare.site, "config.js");
    const settings = readFileSync(file, "utf8");
    const stripped = settings.replace(/^  sim: \{\n(?:    .*\n)*  \},\n/m, "");
    assert.notEqual(stripped, settings, "the build's settings name the factory under `sim`");
    writeFileSync(file, stripped);
    const { CONFIG: none } = await import(pathToFileURL(file).href);
    assert.equal(none.chainId, 999);
    assert.equal(none.hyperliquidChain, "Mainnet");
    assert.equal("sim" in none, false);
    await import(pathToFileURL(join(bare.site, "app.js")).href);
    chain.pools = [LISTED];
    chain.exams = new Map([[EXAM.toLowerCase(), LISTED]]);
    // Even the pool the factory would list, and its own exam: with no list in the settings there is nothing to hold them to.
    for (const hash of [...PANELS(LISTED), ...PANELS(OTHER), `#/exam/${EXAM}`]) {
      const page = await open(hash);
      assert.ok(refused(page), `${hash}: drew ${page.html.slice(0, 80)}, built ${page.built}`);
      assert.deepEqual(page.reads, [], `${hash}: nothing is read, of the address or of any factory`);
      assert.equal(page.panel, null, hash);
    }
  } finally {
    chain.exams = new Map();
    bare.remove();
  }
});
