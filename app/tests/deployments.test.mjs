// node --test app/tests/deployments.test.mjs
//
// The site reads two deployments (Alex's word, 1 Oct 2026): demo2, live, which the gateway and the keepers serve,
// and demo, the first, kept as an archive whose records can still be read and checked. app/config.js holds both, each
// equal to its record in deployments/; a page finds which deployment an address belongs to; the archive sells nothing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { CONFIG } from "../config.js";
import { DEPLOYMENTS, liveDeployment, isArchive, pickDeployment, deploymentNamed, ARCHIVE, ASSET_LEVERAGE_X100, poolRules,
  defaultCapsText, dexOf, rulesFields, capsByAsset } from "../lib/deployments.js";
import { poolStatus } from "../lib/stages.js";
import { tradeTarget } from "../lib/listing.js";
import { TEXT, appLink, factoryQuestion, withDeployment, deploymentBadge, accountCell } from "../lib/evidence.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const record = (label) => JSON.parse(text(`../../deployments/testnet-${label}.json`));
const slice = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));

test("each deployment in app/config.js is its record in deployments/, field for field", () => {
  assert.deepEqual(DEPLOYMENTS.map((d) => [d.label, d.role]), [["demo2", "live"], ["demo", "archive"]]);
  // The perp universe is one per chain: demo2's record names each asset with its index, the first one by name only.
  const index = record("demo2").platform_assets;
  for (const d of DEPLOYMENTS) {
    const r = record(d.label);
    assert.equal(r.label, d.label);
    assert.equal(r.chain_id, CONFIG.chainId);
    assert.equal(d.factory, r.PoolFactory, `${d.label} factory`);
    assert.equal(d.registry, r.KeyRegistry ?? r.KeyBinder, `${d.label} registry`);
    assert.equal(d.keyBinder, r.KeyBinder ? true : undefined, `${d.label} keyBinder`);
    assert.equal(d.deployBlock, r.block, `${d.label} deployBlock`);
    assert.equal(d.deployer, r.deployer, `${d.label} deployer`);
    const assets = Array.isArray(r.platform_assets) ? r.platform_assets.map((name) => index[name]) : Object.values(r.platform_assets);
    assert.deepEqual([...d.platformAssets].sort(), [...assets].sort(), `${d.label} assets`);
  }
  // Nothing outside the list says where a deployment is: the old single-deployment fields are gone.
  for (const field of ["factory", "registry", "deployBlock", "deployer", "platformAssets"]) {
    assert.equal(field in CONFIG, false, field);
  }
  assert.equal(liveDeployment().label, "demo2");
});

test("an address belongs to the deployment whose factory made it, and every page asks", () => {
  const [live, archive] = DEPLOYMENTS;
  assert.equal(pickDeployment(DEPLOYMENTS, [true, false]), live);
  assert.equal(pickDeployment(DEPLOYMENTS, [false, true]), archive);
  assert.equal(pickDeployment(DEPLOYMENTS, [false, false]), null);
  assert.equal(deploymentNamed("demo2"), live);
  assert.equal(deploymentNamed(archive.factory.toLowerCase()), archive);
  assert.equal(deploymentNamed("rehearsal"), null);
  assert.equal(isArchive(archive), true);
  assert.equal(isArchive(live), false);
  const chain = text("../lib/chain.js");
  assert.match(chain, /const ask = kind === "challenge" \? "isChallenge" : "isPool";/);
  assert.match(chain, /await Promise\.all\(DEPLOYMENTS\.map\(\(d\) => factory\(runner, d\)\[ask\]\(address\)\)\)/);
  assert.match(text("../views/pool.js"), /const deployment = await chain\.deploymentOf\(address, "pool"\);/);
  assert.match(text("../views/challenge.js"), /const deployment = await chain\.deploymentOf\(address, "challenge"\);/);
  const verify = text("../views/verify.js");
  assert.match(verify, /chain\.deploymentOf\(address, "pool", chain\.checkProvider\),\s+chain\.deploymentOf\(address, "challenge", chain\.checkProvider\)/);
  assert.match(verify, /const reg = chain\.registry\(chain\.checkProvider, deployment\);/);
  // The new-pool form creates on the live factory only: chain.factory() is the live one unless told otherwise.
  assert.match(chain, /export function factory\(runner, deployment = liveDeployment\(\)\)/);
  assert.match(text("../views/pools.js"), /chain\.write\(chain\.factoryKind\(\), chain\.factory\(\)\.target, "createPool"/);
});

test("the archive sells nothing: no buy button, no way to reach the purchase, no new start", () => {
  assert.deepEqual(poolStatus(0, null, true), { name: "Archive", tone: "",
    words: "Archive: this pool belongs to the first deployment, which sells no challenges." });
  assert.equal(poolStatus(2, null, true).name, "Funded", "a running stage keeps its own name");
  assert.equal(tradeTarget([{ address: "0xold", kind: "pool", blocker: null, archived: true }]), null);
  const pool = text("../views/pool.js");
  assert.match(pool, /const blocker = archived \? \{ kind: "archived" \} : saleBlocker\(/);
  // The purchase exists only inside the branch a blocker closes, and an archived pool always has one.
  const open = slice(pool, "  if (!blocker) {", "  } else {");
  assert.match(open, /chain\.write\("pool", address, "buyChallenge"\)/);
  assert.equal(pool.split('"buyChallenge"').length, 2, "buyChallenge is called from one place");
  assert.match(pool, /blocker\.kind === "archived" \? "No challenges are sold here: this pool belongs to the archived deployment\."/);
  assert.match(pool, /\$\{archived \? `<p class="notice">\$\{esc\(ARCHIVE\.poolNote\)\}<\/p>` : ""\}/);
  // No price to pay where nothing is sold.
  assert.match(pool, /termsHtml\(terms, archived \? null : fee\)/);
  // The investor of an archived pool keeps the ways out, not the ways in.
  assert.match(pool, /if \(!archived\) wireFunding\(page, address\);/);
  assert.match(pool, /\$\{archived \? "" : `<div class="inline"><input id="dep"/);
  assert.match(pool, /chain\.write\("pool", address, "withdrawEarned"\)/);
  assert.match(pool, /chain\.write\("pool", address, "withdrawOnCore", \[amount\]\)/);
  assert.match(pool, /if \(Number\(stage\) === 2 && isFunded && !archived\) settle\(tradePanel/);
  const challenge = text("../views/challenge.js");
  assert.match(challenge, /if \(!spoiled && !archived\) buttons\.push\(`<button id="activate">Start<\/button>`\);/);
  assert.match(challenge, /if \(s === 2 && isTrader && !archived\) settle\(tradePanel/);
  const list = text("../views/pools.js");
  // Before its balance is in and after: an archived pool's blocker is the archive, never a reading.
  assert.match(list, /spotUsdc: null, blocker: archived \? \{ kind: "archived" \} : undefined,/);
  assert.match(list, /const blocker = item\.archived \? \{ kind: "archived" \}/);
  assert.match(list, /const archived = listOrder\(items\.filter\(\(i\) => i\.archived\)\);\s+\$\("#archive", page\)\.replaceChildren\(\.\.\.archived\.map\(\(i\) => poolCard\(i, fee\)\)\);/);
  assert.match(ARCHIVE.heading, /^First deployment, kept as an archive$/);
  // Check-it-yourself agrees: an archived pool is never "Idle: it can sell", before or after a funded cycle.
  const verify = text("../views/verify.js");
  assert.match(verify, /settle\(fillsPanel\(account, address, page, kind, deployment\), \$\("#fills", page\)\);/);
  assert.match(slice(verify, "async function fillsPanel(", "$(\"#fills\", page).className"), /const archived = isArchive\(deployment\);/);
  assert.match(verify, /row\("What the pool is doing", esc\(poolStatus\(state, null, archived\)\.words\)\)/);
  assert.match(verify, /\? ", and as part of the archived deployment it sells no further challenge"/);
  // With no investor's pool open on the live deployment, "I want to trade" goes to the list heading, never empty space.
  assert.match(list, /\$\("#count", page\)\.scrollIntoView\(\{ behavior: "smooth" \}\);/);
});

test("the records page holds each row to the deployment it names", () => {
  const row = (factory, kind = "pool") => ({ account: "0x237afA2D58", factory, kind, tx: "0x1" });
  assert.equal(appLink(row("demo")), "#/verify/0x237afA2D58");
  assert.equal(appLink(row("demo2", "challenge")), "#/verify/0x237afA2D58");
  assert.equal(appLink(row("rehearsal")), null);
  assert.equal(factoryQuestion(row("demo2", "challenge")), "challenge");
  assert.equal(factoryQuestion(row("demo", "seat")), "pool");
  assert.equal(factoryQuestion(row("rehearsal")), null);
  const [live, archive] = DEPLOYMENTS;
  const green = { ok: true, text: "The node says: block 1." };
  assert.deepEqual(withDeployment(green, row("demo2"), live),
    { ok: true, text: "The node says: block 1. The account was made by the demo2 factory, as the row says." });
  assert.deepEqual(withDeployment(green, row("demo2"), archive),
    { ok: false, text: "The node says: block 1. The row names the demo2 deployment, but the demo factory made it." });
  assert.deepEqual(withDeployment(green, row("demo"), null),
    { ok: false, text: "The node says: block 1. The row names the demo deployment, but neither factory this site reads made it." });
  assert.equal(withDeployment(green, row("rehearsal"), undefined), green);
  assert.match(text("../views/evidence.js"), /return withDeployment\(receiptVerdict\(row, receipt, decode\(receipt\)\), row, madeBy\);/);
});

test("the records page says on each row which deployment it is about", () => {
  const row = (factory, kind = "pool") => ({ account: "0x237afA2D58", factory, kind });
  assert.equal(deploymentBadge(row("demo")), "first deployment, archive");
  assert.equal(deploymentBadge(row("demo", "challenge")), "first deployment, archive");
  assert.equal(deploymentBadge(row("demo2")), "live deployment");
  assert.equal(deploymentBadge(row("rehearsal", "challenge")), "rehearsal deployment");
  // A shared pool is not a factory's account: the table of pools names where its seats come from.
  assert.equal(deploymentBadge(row("demo", "shared pool")), "");
  assert.equal(deploymentBadge(row("somewhere else")), "");
  // Every pool, challenge and seat in the records carries one, and both deployments the site reads appear.
  const data = JSON.parse(text("../data/evidence.json"));
  const marked = data.rows.filter((r) => ["pool", "challenge", "seat"].includes(r.kind)).map(deploymentBadge);
  assert.ok(marked.every(Boolean));
  assert.ok(marked.includes("first deployment, archive") && marked.includes("live deployment"));
  // What the page prints, for a row with an account and for one without: the mark comes before either.
  assert.equal(accountCell({ account: "0xabc", kind: "pool", factory: "demo" }),
    '<span class="badge">pool</span> <span class="badge">first deployment, archive</span><br><code>0xabc</code>'
    + '<br><a href="#/verify/0xabc">Open it in the app</a>');
  assert.equal(accountCell({ account: "0xabc", kind: "challenge", factory: "demo2" }),
    '<span class="badge">challenge</span> <span class="badge">live deployment</span><br><code>0xabc</code>'
    + '<br><a href="#/verify/0xabc">Open it in the app</a>');
  assert.equal(accountCell({ factory: "rehearsal" }),
    '<span class="badge">rehearsal deployment</span><br><span class="muted">not named in the document</span>');
  assert.equal(accountCell({ account: "0xabc", kind: "shared pool", factory: "demo" }),
    '<span class="badge">shared pool</span> <code>0xabc</code><br><a href="#/shared/0xabc">Open it in the app</a>');
  assert.match(text("../views/evidence.js"), /const acct = accountCell\(row\);/);
  assert.match(TEXT.intro.join(" "), /The first deployment is kept as an archive: its records stand and can be checked, and it sells nothing\. The second is the live one/);
  // The rows that carry no mark are named too, so a reader does not take them for rows the page forgot.
  assert.match(TEXT.intro.join(" "), /A shared pool carries no such mark: it is not a factory's account, and the table of the shared pools names the factory its seats come from\./);
});

test("createPool takes four fields before v3 and each asset's own cap after them on a v3 factory", () => {
  const rules = { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 500, assets: [3, 4, 0, 135, 9] };
  const names = ["BTC", "ETH", "SOL", "HYPE", "NEW"];
  // demo2 and the archive are factories before v3: the four fields, and nothing else, or createPool would not encode.
  for (const d of DEPLOYMENTS) assert.deepEqual(Object.keys(poolRules(d, rules, names)), ["dailyLossBps", "maxDrawdownBps", "maxLeverageX100", "assets"]);
  // A v3 factory: in the order of the assets, the platform's default by coin, never over the account's 5x, and the
  // account's cap for a coin with no default.
  const v3 = poolRules({ assetLeverage: true }, rules, names);
  assert.deepEqual(v3.assetLeverageX100, [500, 500, 500, 500, 500]);
  assert.deepEqual(poolRules({ assetLeverage: true }, { ...rules, maxLeverageX100: 1000 }, names).assetLeverageX100, [1000, 1000, 500, 1000, 1000]);
  assert.deepEqual(ASSET_LEVERAGE_X100, { BTC: 1000, ETH: 1000, SOL: 500, HYPE: 1000, SP500: 1000, XYZ100: 800, CL: 300, BRENTOIL: null,
    GOLD: 2000, SILVER: 1500 });
  // The leverage rule v1.2: HYPE 10x, the most Hyperliquid itself lends on it (its meta's maxLeverage, mainnet and testnet).
  assert.equal(defaultCapsText(["HYPE", "SOL"]), "HYPE 10×, SOL 5×");
  assert.equal(defaultCapsText(["BTC", "ETH", "xyz:XYZ100", "xyz:CL", "NEW"]), "BTC 10×, ETH 10×, xyz:XYZ100 8×, xyz:CL 3×");
  // One dex an account on v3: the factory refuses rules that mix them, so the form says so before anything is sent.
  assert.deepEqual([3, 750000, 750029, 110052].map(dexOf), [0, 65, 65, 1]);
  assert.throws(() => poolRules({ assetLeverage: true }, { ...rules, assets: [3, 750000] }, ["BTC", "xyz:XYZ100"]), /all trade on one dex/);
  assert.equal(poolRules(DEPLOYMENTS[0], { ...rules, assets: [3, 750000] }, ["BTC", "xyz:XYZ100"]).assetLeverageX100, undefined);
  const xyz = poolRules({ assetLeverage: true }, { ...rules, maxLeverageX100: 1000, assets: [750000, 750029] }, ["xyz:XYZ100", "xyz:CL"]);
  assert.deepEqual(xyz.assetLeverageX100, [800, 300]);
  assert.match(text("../views/pools.js"), /<input name="lev" type="number" step="0.1" min="1" max="10"/);
  // Each factory's createPool sends its own copy of the rules; scripts/abi-check.py holds them to the contracts.
  const chain = text("../lib/chain.js");
  assert.match(chain, /`function createPool\(\$\{RULES_WRITE\} rules, \$\{TERMS\} terms\) returns \(address\)`/);
  assert.match(chain, /`function createPool\(\$\{RULES_WRITE_V3\} rules, \$\{TERMS\} terms\) returns \(address\)`/);
  assert.match(chain, /return deployment\?\.assetLeverage \? "factoryV3" : "factory";/);
});

test("a market whose cap is still to come is refused, not given the account's cap", () => {
  const rules = { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 1000, assets: [750000, 750099] };
  ASSET_LEVERAGE_X100.NEXT = null;
  try {
    assert.throws(() => poolRules({ assetLeverage: true }, rules, ["xyz:XYZ100", "xyz:NEXT"]), /xyz:NEXT: the platform gives it no leverage cap/);
    assert.equal(defaultCapsText(["xyz:XYZ100", "xyz:NEXT"]), "xyz:XYZ100 8×");
  } finally {
    delete ASSET_LEVERAGE_X100.NEXT;
  }
});

test("xyz:BRENTOIL is withdrawn: a pool that names it is refused and the form shows no cap for it", () => {
  // The leverage rule v1.3 (2 November 2026) withdrew xyz:BRENTOIL: null, no cap from the platform.
  assert.equal(ASSET_LEVERAGE_X100.BRENTOIL, null);
  const rules = { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 1000, assets: [750029, 750049] };
  assert.throws(() => poolRules({ assetLeverage: true }, rules, ["xyz:CL", "xyz:BRENTOIL"]),
    /^Error: xyz:BRENTOIL: the platform gives it no leverage cap, so no pool may list it\.$/);
  assert.equal(defaultCapsText(["xyz:CL", "xyz:BRENTOIL"]), "xyz:CL 3×");
});

test("xyz:GOLD is 20×, the leverage rule v1.3", () => {
  // The leverage rule v1.3 (2 November 2026) for xyz:GOLD, under the 25x Hyperliquid lends on it (its meta's
  // maxLeverage, mainnet and testnet alike, read on 10 November 2026); never over the account's own cap.
  assert.equal(ASSET_LEVERAGE_X100.GOLD, 2000);
  const rules = { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 2500, assets: [750003] };
  assert.deepEqual(poolRules({ assetLeverage: true }, rules, ["xyz:GOLD"]).assetLeverageX100, [2000]);
  assert.deepEqual(poolRules({ assetLeverage: true }, { ...rules, maxLeverageX100: 1000 }, ["xyz:GOLD"]).assetLeverageX100, [1000]);
  assert.equal(defaultCapsText(["xyz:GOLD"]), "xyz:GOLD 20×");
});

test("xyz:SILVER is 15×, the leverage rule v1.3", () => {
  // The leverage rule v1.3 (2 November 2026) for xyz:SILVER, under the 25x Hyperliquid lends on it (its meta's
  // maxLeverage, mainnet and testnet alike, read on 11 November 2026). Before it was in the table a pool that named
  // it took the account's cap: 20x on a 20x account. Never over the account's own cap.
  assert.equal(ASSET_LEVERAGE_X100.SILVER, 1500);
  const rules = { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 2000, assets: [750026] };
  assert.deepEqual(poolRules({ assetLeverage: true }, rules, ["xyz:SILVER"]).assetLeverageX100, [1500]);
  assert.deepEqual(poolRules({ assetLeverage: true }, { ...rules, maxLeverageX100: 1000 }, ["xyz:SILVER"]).assetLeverageX100, [1000]);
  assert.equal(defaultCapsText(["xyz:GOLD", "xyz:SILVER"]), "xyz:GOLD 20×, xyz:SILVER 15×");
});

test("the site's table of caps is ops/leverage.py's", () => {
  // Both create pools: the site's form and the ops scripts. A cap in one and not in the other gives one pool's
  // rules a cap another's would not have.
  const py = text("../../ops/leverage.py");
  const table = py.match(/^ASSET_LEVERAGE_X100 = \{([^}]*)\}/m);
  assert.ok(table, "ASSET_LEVERAGE_X100 in ops/leverage.py");
  const fromPy = {};
  for (const [, coin, value] of table[1].matchAll(/"(\w+)": (\d+|None)/g)) fromPy[coin] = value === "None" ? null : Number(value);
  assert.deepEqual(fromPy, ASSET_LEVERAGE_X100);
});

test("a HIP-3 perp is named from its own dex's meta, so the form gives it the platform's default cap", async () => {
  const hl = await import("../lib/hl.js");
  const asked = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (_url, { body }) => {
    const b = JSON.parse(body);
    asked.push(b);
    const answer = b.type === "perpDexs" ? [null, ...Array.from({ length: 64 }, (_, i) => ({ name: `d${i + 1}` })), { name: "xyz" }]
      : b.type === "meta" && b.dex === "xyz" ? { universe: [{ name: "xyz:XYZ100", szDecimals: 4 }, { name: "xyz:TSLA", szDecimals: 3 }] }
        : { universe: [{ name: "SOL", szDecimals: 2 }, { name: "APT", szDecimals: 2 }, { name: "ATOM", szDecimals: 2 }, { name: "BTC", szDecimals: 5 }] };
    return { ok: true, json: async () => answer };
  };
  try {
    assert.deepEqual(await hl.perp(750000), { index: 750000, name: "xyz:XYZ100", szDecimals: 4 });
    assert.equal((await hl.perp(750001)).name, "xyz:TSLA");
    assert.equal((await hl.perp(3)).name, "BTC");
    assert.equal((await hl.perp(850000)).name, "#850000", "a dex Hyperliquid does not list");
    // The dex list and the dex's meta asked once each.
    assert.equal(asked.filter((b) => b.type === "perpDexs").length, 1);
    assert.equal(asked.filter((b) => b.type === "meta" && b.dex === "xyz").length, 1);
  } finally {
    globalThis.fetch = real;
  }
  const names = ["xyz:XYZ100", "xyz:CL"];
  assert.deepEqual(poolRules({ assetLeverage: true }, { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 1000,
    assets: [750000, 750029] }, names).assetLeverageX100, [800, 300]);
  assert.match(text("../views/pools.js"), /\.map\(\(index\) => hl\.perp\(Number\(index\)\)\)/);
  // The checkbox says the name the cap is given by, the dex's own (CodeRabbit, cp#165).
  assert.match(text("../views/pools.js"), /checked>\n\s+\$\{esc\(nameOf\(index\)\)\}<\/label>/);
});

test("a v3 account's own cap for each asset is read from rules() of either shape and shown by asset", () => {
  // rules() answered as one tuple with arrays in it: the tuple's offset, then its head, then the arrays. The fourth
  // word of the head is where assets start inside the tuple: 0x80 after four words, 0xa0 after five.
  const w = (n) => BigInt(n).toString(16).padStart(64, "0");
  const four = `0x${[0x20, 300, 600, 500, 0x80, 2, 3, 4].map(w).join("")}`;
  const five = `0x${[0x20, 300, 600, 1000, 0xa0, 0x100, 2, 3, 4, 2, 500, 1000].map(w).join("")}`;
  assert.equal(rulesFields(four), 4);
  assert.equal(rulesFields(five), 5);
  assert.equal(rulesFields(`0x${[0x20, 300, 600, 500, 0x60].map(w).join("")}`), null);
  assert.equal(capsByAsset("BTC, ETH", [500, 1000]), "BTC 5×, ETH 10×");
  // Asked only of an account of a deployment marked v3: demo2 and the archive spend no read on it.
  const chain = text("../lib/chain.js");
  assert.match(chain, /if \(!deployment\?\.assetLeverage\) return \[\];\n  const raw = await readProvider\.call/);
  assert.match(chain, /if \(rulesFields\(raw\) !== 5\) return \[\];/);
  assert.match(text("../views/pools.js"), /\.\.\.\(caps\.length \? \[row\("Max leverage by asset", esc\(capsByAsset\(assets, caps\)\)\)\] : \[\]\),/);
});

// deployments/LIVE.json is the one list of the deployments: every record in deployments/, what it is for today and
// where it is served (docs/DEPLOYMENTS.md). The tests below hold it to the records, to the networks
// spike/hlspike/network.py defines, and to app/config.js: a deployment added, retired or moved in one of them and not
// in the others is red here. Both networks go through the same checks; a mainnet example shows it.
const LIVE = JSON.parse(text("../../deployments/LIVE.json")).deployments;
const ROLES = ["live", "testers", "shared", "staged", "archive", "retired"];
const FIELDS = ["label", "network", "chainId", "role", "record", "factory", "pool", "gateway", "deployer", "keeper"];
// v3-demo3's record was written by hand with the label "demo3"; the tools, the host and LIVE.json call it by its file.
const RECORD_LABEL = { "v3-demo3": "demo3" };
// Each network's name and HyperEVM chain id, read from network.py itself rather than copied here.
const NETWORKS = Object.fromEntries([...text("../../spike/hlspike/network.py")
  .matchAll(/= Network\(\s*name="(\w+)",\s*chain_id=(\d+),/g)].map(([, name, id]) => [name, Number(id)]));

// The record files in deployments/, LIVE.json aside, and one record read. registries.json is no record either: it is
// the deploy scripts' journal of the key registries (ops/deploy_testnet.py, REGISTRY_JOURNAL).
const NOT_RECORDS = ["LIVE.json", "registries.json"];
const RECORDS = readdirSync(new URL("../../deployments/", import.meta.url))
  .filter((f) => f.endsWith(".json") && !NOT_RECORDS.includes(f)).map((f) => `deployments/${f}`);
const readRecord = (path) => JSON.parse(text(`../../${path}`));

// What a list of entries must say about the record files beside it: run on LIVE.json as it is, and on LIVE.json with
// a mainnet deployment added.
function checkLive(live, records, recordOf) {
  assert.deepEqual(live.map((e) => e.record).sort(), [...records].sort(), "one entry for each record, none without one");
  assert.equal(new Set(live.map((e) => e.label)).size, live.length, "no label twice");
  for (const network of new Set(live.map((e) => e.network))) {
    const lives = live.filter((e) => e.network === network && e.role === "live").length;
    assert.ok(lives <= 1, `one live deployment on ${network} at most, not ${lives}`);
  }
  const gateways = live.map((e) => e.gateway).filter(Boolean);
  assert.equal(new Set(gateways).size, gateways.length, "no gateway serves two deployments");
  for (const e of live) {
    assert.deepEqual(Object.keys(e), FIELDS, `${e.label}: the fields, in order`);
    assert.ok(ROLES.includes(e.role), `${e.label}: role ${e.role}`);
    assert.ok(Object.hasOwn(NETWORKS, e.network), `${e.label}: network ${e.network}`);
    assert.equal(e.chainId, NETWORKS[e.network], `${e.label}: chainId is ${e.network}'s`);
    assert.equal(e.record, `deployments/${e.network}-${e.label}.json`, `${e.label}: record`);
    const r = recordOf(e.record);
    assert.equal(r.label, RECORD_LABEL[e.label] ?? e.label, `${e.label}: the record's label`);
    assert.equal(r.status, "complete", `${e.label}: the record's status`);
    assert.equal(r.chain_id, e.chainId, `${e.label}: the record's chain_id`);
    if ("network" in r) assert.equal(r.network, e.network, `${e.label}: the record's network`);
    assert.equal(e.factory, r.PoolFactory, `${e.label}: factory`);
    assert.equal(e.pool, r.SharedPool ?? null, `${e.label}: pool`);
    // A registry handed over at the deploy (--operator): from the deployer, to the address the owner gave, pending
    // until that address accepts the role on chain.
    if (r.registry_operator) {
      const { from, to, state } = r.registry_operator;
      assert.equal(from, r.deployer, `${e.label}: the handover is the deployer's`);
      assert.match(to, /^0x[0-9a-fA-F]{40}$/, `${e.label}: the handover names an address`);
      assert.notEqual(to.toLowerCase(), from.toLowerCase(), `${e.label}: a handover to the deployer is none`);
      assert.ok(["pending", "accepted"].includes(state), `${e.label}: handover ${state}`);
    }
    assert.equal(e.deployer, r.deployer, `${e.label}: deployer`);
    // What serves it: its own gateway and keeper for live and testers; the shared pools' keeper for a shared pool,
    // whose seats trade through the gateway of the deployment that made them; nothing for the staged, the archive and
    // the retired.
    const served = e.role === "live" || e.role === "testers";
    if (served) assert.match(e.gateway, /^https:\/\/[a-z0-9.-]+(\/[a-z0-9]+)?$/, `${e.label}: gateway`);
    else assert.equal(e.gateway, null, `${e.label}: no gateway of its own`);
    if (served || e.role === "shared") assert.match(e.keeper, /^0x[0-9a-fA-F]{40}$/, `${e.label}: keeper`);
    else assert.equal(e.keeper, null, `${e.label}: no keeper`);
    if (e.role === "shared") assert.notEqual(e.pool, null, `${e.label}: a shared deployment is its pool`);
  }
}

// What app/config.js must say, for a list of entries: the deployments the site reads, of the app's own network.
function checkSite(live) {
  const network = Object.keys(NETWORKS).find((n) => NETWORKS[n] === CONFIG.chainId);
  assert.ok(network, `CONFIG.chainId ${CONFIG.chainId} is the chain id of a network network.py defines`);
  const mine = live.filter((e) => e.network === network);
  // The site reads the live deployment and the archive of its network, live first, and no other.
  const site = [...mine.filter((e) => e.role === "live"), ...mine.filter((e) => e.role === "archive")];
  assert.deepEqual(DEPLOYMENTS.map((d) => [d.label, d.role]), site.map((e) => [e.label, e.role]));
  DEPLOYMENTS.forEach((d, i) => {
    const e = site[i];
    assert.equal(CONFIG.chainId, e.chainId, `${d.label}: chainId`);
    assert.equal(d.factory, e.factory, `${d.label}: factory`);
    assert.equal(d.deployer, e.deployer, `${d.label}: deployer`);
    // The shared pool #/shared opens is a shared deployment whose seats this factory makes.
    assert.equal("sharedPool" in d, "sharedPoolRecord" in d, `${d.label}: sharedPool and sharedPoolRecord go together`);
    if (!("sharedPoolRecord" in d)) return;
    const shared = mine.find((x) => x.label === d.sharedPoolRecord);
    assert.ok(shared, `${d.label}: sharedPoolRecord ${d.sharedPoolRecord} is not in LIVE.json`);
    assert.equal(shared.role, "shared", `${d.label}: ${d.sharedPoolRecord} is a shared deployment in LIVE.json`);
    assert.equal(d.sharedPool, shared.pool, `${d.label}: sharedPool`);
    assert.equal(shared.factory, d.factory, `${d.label}: the shared pool's seats come from this factory`);
  });
  assert.equal(CONFIG.gateway, site[0].gateway, "orders go to the live deployment's gateway");
}

test("deployments/LIVE.json lists every record in deployments/, and each entry says what its record says", () => {
  assert.deepEqual(Object.keys(NETWORKS).sort(), ["mainnet", "testnet"], "network.py is read: both networks and their chain ids");
  checkLive(LIVE, RECORDS, readRecord);
});

test("app/config.js reads the deployments LIVE.json gives the site, at the addresses LIVE.json gives them", () => {
  checkSite(LIVE);
});

// A mainnet deployment as it will be listed. It is an example and lives here, not in deployments/, which holds what
// is deployed. Its record carries the fields the checks read; the addresses are placeholders.
const MAINNET_RECORD = { network: "mainnet", chain_id: 999, status: "complete",
  PoolFactory: "0x00000000000000000000000000000000000000F1", deployer: "0x00000000000000000000000000000000000000D1" };
const MAINNET = { label: "v3-main", network: "mainnet", chainId: 999, role: "live", record: "deployments/mainnet-v3-main.json",
  factory: MAINNET_RECORD.PoolFactory, pool: null, gateway: "https://pools-api.usenami.io/main",
  deployer: MAINNET_RECORD.deployer, keeper: "0x00000000000000000000000000000000000000E1" };

test("a registry handed over at the deploy is the deployer's handover, to another address, pending or accepted", () => {
  const examples = ["deployments/mainnet-v3-main.json"];
  const handover = (h) => (path) => (examples.includes(path)
    ? { label: "v3-main", ...MAINNET_RECORD, registry_operator: h } : readRecord(path));
  const testnet = LIVE.filter((e) => e.network === "testnet");
  const list = [[...testnet, MAINNET], [...testnet.map((e) => e.record), MAINNET.record]];
  const given = "0x00000000000000000000000000000000000000Cc";
  checkLive(...list, handover({ from: MAINNET_RECORD.deployer, to: given, state: "pending" }));
  checkLive(...list, handover({ from: MAINNET_RECORD.deployer, to: given, state: "accepted" }));
  assert.throws(() => checkLive(...list, handover({ from: given, to: MAINNET_RECORD.deployer, state: "pending" })), /deployer's/);
  assert.throws(() => checkLive(...list, handover({ from: MAINNET_RECORD.deployer, to: MAINNET_RECORD.deployer, state: "pending" })), /is none/);
  assert.throws(() => checkLive(...list, handover({ from: MAINNET_RECORD.deployer, to: given, state: "sent" })), /handover sent/);
});

test("a mainnet deployment goes through the same checks beside testnet's, and app/config.js answers for its own network only", () => {
  // The example stands beside testnet's entries only, and the placeholder record answers for the example's own files
  // only: a real mainnet deployment in LIVE.json is checked by the first test, and changes nothing here.
  const examples = ["v3-main", "v3-main2"].map((label) => `deployments/mainnet-${label}.json`);
  const recordOf = (path) => (examples.includes(path)
    ? { label: path.slice("deployments/mainnet-".length, -".json".length), ...MAINNET_RECORD } : readRecord(path));
  const testnet = LIVE.filter((e) => e.network === "testnet");
  const plus = (...entries) => [[...testnet, ...entries], [...testnet.map((e) => e.record), ...entries.map((e) => e.record)]];
  // Live, and before that staged: deployed, with nothing serving it yet.
  checkLive(...plus(MAINNET), recordOf);
  checkLive(...plus({ ...MAINNET, role: "staged", gateway: null, keeper: null }), recordOf);
  checkSite(plus(MAINNET)[0]);
  // The checks hold on mainnet as on testnet: its own chain id, its own file, one live deployment, and nothing serving
  // a staged one.
  assert.throws(() => checkLive(...plus({ ...MAINNET, chainId: 998 }), recordOf), /v3-main: chainId is mainnet's/);
  assert.throws(() => checkLive(...plus({ ...MAINNET, record: "deployments/testnet-v3-main.json" }), recordOf), /v3-main: record/);
  assert.throws(() => checkLive(...plus(MAINNET, { ...MAINNET, label: "v3-main2", record: "deployments/mainnet-v3-main2.json",
    gateway: "https://pools-api.usenami.io/main2" }), recordOf), /one live deployment on mainnet at most, not 2/);
  assert.throws(() => checkLive(...plus({ ...MAINNET, role: "staged" }), recordOf), /v3-main: no gateway of its own/);
});
