// node --test app/tests/deployments.test.mjs
//
// The site reads two deployments (Alex's word, 1 Oct 2026): demo2, live, which the gateway and the keepers serve,
// and demo, the first, kept as an archive whose records can still be read and checked. app/config.js holds both, each
// equal to its record in deployments/; a page finds which deployment an address belongs to; the archive sells nothing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CONFIG } from "../config.js";
import { DEPLOYMENTS, liveDeployment, isArchive, pickDeployment, deploymentNamed, ARCHIVE } from "../lib/deployments.js";
import { poolStatus } from "../lib/stages.js";
import { tradeTarget } from "../lib/listing.js";
import { appLink, factoryQuestion, withDeployment } from "../lib/evidence.js";

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
    assert.equal(d.registry, r.KeyRegistry, `${d.label} registry`);
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
  assert.match(chain, /await Promise\.all\(DEPLOYMENTS\.map\(\(d\) => factory\(undefined, d\)\[ask\]\(address\)\)\)/);
  assert.match(text("../views/pool.js"), /const deployment = await chain\.deploymentOf\(address, "pool"\);/);
  assert.match(text("../views/challenge.js"), /const deployment = await chain\.deploymentOf\(address, "challenge"\);/);
  const verify = text("../views/verify.js");
  assert.match(verify, /chain\.deploymentOf\(address, "pool"\), chain\.deploymentOf\(address, "challenge"\)/);
  assert.match(verify, /const reg = chain\.registry\(undefined, deployment\);/);
  // The new-pool form creates on the live factory only: chain.factory() is the live one unless told otherwise.
  assert.match(chain, /export function factory\(runner, deployment = liveDeployment\(\)\)/);
  assert.match(text("../views/pools.js"), /chain\.write\("factory", chain\.factory\(\)\.target, "createPool"/);
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
  assert.match(list, /const blocker = archived \? \{ kind: "archived" \}/);
  assert.match(list, /for \(const item of archived\) \$\("#archive", page\)\.append\(poolCard\(item, fee\)\);/);
  assert.match(ARCHIVE.heading, /^First deployment, kept as an archive$/);
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
