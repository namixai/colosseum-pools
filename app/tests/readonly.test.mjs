// node --test app/tests/readonly.test.mjs
//
// The deployment the site only reads: demo3, the v3 factory whose accounts trade on a builder's dex (HIP-3). app/config.js
// holds it apart from the deployments the pages buy, trade and open pools on, equal to its record in deployments/;
// every call the app would make to its contracts is held to their ABI as deployed (app/data/abi-v3-demo3.json); and an
// account's trading key is said as its contract says it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CONFIG } from "../config.js";
import { DEPLOYMENTS, READ_ONLY_DEPLOYMENTS, READ_ONLY, isReadOnly, everyDeployment, usdcOf, gatewayOf, liveDeployment,
  deploymentNamed, isArchive } from "../lib/deployments.js";
import { keyTerm, keyTermWords } from "../lib/agentterm.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8").replace(/\r\n/g, "\n");
const record = (name) => JSON.parse(text(`../../deployments/testnet-${name}.json`));
const DEPLOYED = JSON.parse(text("../data/abi-v3-demo3.json"));
const CHAIN = text("../lib/chain.js");

test("the deployment the site only reads is its record in deployments/, field for field", () => {
  assert.deepEqual(READ_ONLY_DEPLOYMENTS.map((d) => [d.label, d.role]), [["demo3", "read-only"]]);
  for (const d of READ_ONLY_DEPLOYMENTS) {
    const r = record(d.record);
    assert.equal(r.label, d.label);
    assert.equal(r.chain_id, CONFIG.chainId);
    assert.equal(d.factory, r.PoolFactory, `${d.label} factory`);
    assert.equal(d.registry, r.KeyRegistry ?? r.KeyBinder, `${d.label} registry`);
    assert.equal(d.keyBinder, r.KeyBinder ? true : undefined, `${d.label} keyBinder`);
    assert.equal(d.deployBlock, r.block, `${d.label} deployBlock`);
    assert.equal(d.deployer, r.deployer, `${d.label} deployer`);
    assert.equal(d.usdc, r.usdc, `${d.label} usdc`);
    assert.equal(d.usdc, r.DemoUsdc, `${d.label} takes its own test dollar`);
    assert.equal(d.builtAt, r.commit, `${d.label} builtAt`);
    assert.deepEqual([...d.platformAssets].sort(), Object.values(r.platform_assets).sort(), `${d.label} assets`);
    assert.equal(d.assetLeverage, true, "a v3 factory");
    assert.equal(d.gateway, `${CONFIG.gateway}/${d.label}`, "its own gateway instance");
  }
  // The ABI the test holds the app to is the one built at the deployed commit.
  assert.equal(DEPLOYED.builtAt, READ_ONLY_DEPLOYMENTS[0].builtAt);
  assert.equal(DEPLOYED.record, `deployments/testnet-${READ_ONLY_DEPLOYMENTS[0].record}.json`);
});

test("the deployments the site only reads are the testers entries of its network in deployments/LIVE.json", () => {
  const live = JSON.parse(text("../../deployments/LIVE.json")).deployments;
  const network = CONFIG.chainId === 999 ? "mainnet" : "testnet";
  const testers = live.filter((e) => e.network === network && e.role === "testers");
  assert.deepEqual(READ_ONLY_DEPLOYMENTS.map((d) => [d.record, d.factory, d.deployer, d.gateway]),
    testers.map((e) => [e.label, e.factory, e.deployer, e.gateway]));
  for (const d of READ_ONLY_DEPLOYMENTS) assert.equal(`deployments/${network}-${d.record}.json`, testers.find((e) => e.label === d.record).record);
});

test("a deployment the site only reads is never one it sells, trades or opens pools on", () => {
  const factories = new Set(DEPLOYMENTS.map((d) => d.factory.toLowerCase()));
  for (const d of READ_ONLY_DEPLOYMENTS) assert.equal(factories.has(d.factory.toLowerCase()), false, d.label);
  const [demo3] = READ_ONLY_DEPLOYMENTS;
  assert.equal(isReadOnly(demo3), true);
  for (const d of DEPLOYMENTS) assert.equal(isReadOnly(d), false, d.label);
  assert.equal(isArchive(demo3), false, "not the archive: its words are not the archive's");
  assert.equal(liveDeployment().label, "demo2");
  assert.equal(deploymentNamed("demo3"), null, "the pages' own lookup does not find it");
  assert.deepEqual(everyDeployment().map((d) => d.label), ["demo2", "demo", "demo3"]);
  // The pages ask deploymentOf, which asks the served deployments only; the read-only ones are asked apart.
  const of = (name) => CHAIN.slice(CHAIN.indexOf(`export async function ${name}(`), CHAIN.indexOf("\n}\n", CHAIN.indexOf(`export async function ${name}(`)));
  assert.match(of("deploymentOf"), /await Promise\.all\(DEPLOYMENTS\.map\(\(d\) => factory\(runner, d\)\[ask\]\(address\)\)\);\n  return pickDeployment\(DEPLOYMENTS, answers\);/);
  assert.match(of("readOnlyDeploymentOf"), /const question = kind === "challenge" \? "isChallenge" : "isPool";/);
  assert.match(of("readOnlyDeploymentOf"),
    /await Promise\.all\(READ_ONLY_DEPLOYMENTS\.map\(\(d\) => factory\(runner, d\)\[question\]\(address\)\)\);\n  return pickDeployment\(READ_ONLY_DEPLOYMENTS, answers\);/);
  for (const words of Object.values(READ_ONLY)) assert.match(words, /HIP-3/);
  assert.match(READ_ONLY.poolNote, /sells no challenge here/);
  assert.match(READ_ONLY.challengeNote, /orders for this account are not taken here/);
});

test("a read-only deployment's price token and gateway are its own; the served ones keep the site's", () => {
  const [demo3] = READ_ONLY_DEPLOYMENTS;
  assert.equal(usdcOf(demo3), "0xa73D107dC507851CE4dD4A426e163e2F3368300D");
  assert.equal(gatewayOf(demo3), "https://pools-api.usenami.io/demo3");
  for (const d of DEPLOYMENTS) {
    assert.equal(usdcOf(d), CONFIG.usdc, d.label);
    assert.equal(gatewayOf(d), CONFIG.gateway, d.label);
  }
  assert.equal(usdcOf(null), CONFIG.usdc);
  assert.equal(gatewayOf(undefined), CONFIG.gateway);
});

// The app's ABI as chain.js writes it, lifted from the source: chain.js reads window.ethers when it loads.
function liftAbi() {
  const from = CHAIN.indexOf("const RULES =");
  const to = CHAIN.indexOf("export const STATUS");
  const v3Factory = CHAIN.match(/ABI\.factoryV3 = [\s\S]+?;\n/);
  assert.ok(from !== -1 && to > from && v3Factory, "chain.js has its ABI where this test lifts it from");
  return new Function(`${CHAIN.slice(from, to).replace("export const ABI", "const ABI")}\n${v3Factory[0]}\nreturn ABI;`)();
}

// One line of a human-readable ABI, as the data file writes it: types only, names left out.
function split(s) {
  const parts = [];
  let depth = 0;
  let cur = "";
  for (const ch of s) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === "," && depth === 0) {
      parts.push(cur);
      cur = "";
    } else cur += ch;
  }
  if (cur.trim()) parts.push(cur);
  return parts;
}
function closing(s, open) {
  let depth = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === "(") depth++;
    if (s[i] === ")" && --depth === 0) return i;
  }
  return -1;
}
function type(p) {
  const s = p.trim();
  if (!s.startsWith("(")) return s.split(/\s+/)[0];
  const end = closing(s, 0);
  const suffix = s.slice(end + 1).trim().match(/^(\[\d*\])*/)[0];
  return `(${split(s.slice(1, end)).map(type).join(",")})${suffix}`;
}
function normal(line) {
  const head = line.match(/^(function|event|error) (\w+)\(/);
  const open = head[0].length - 1;
  const close = closing(line, open);
  const ins = split(line.slice(open + 1, close)).map(type).join(",");
  if (head[1] !== "function") return `${head[1]} ${head[2]}(${ins})`;
  const out = line.slice(close + 1).match(/returns \((.*)\)\s*$/);
  return `function ${head[2]}(${ins})->(${out ? split(out[1]).map(type).join(",") : ""})`;
}

test("every call the app makes to demo3's contracts is in their ABI as deployed", () => {
  const ABI = liftAbi();
  const contractOf = { factoryV3: "PoolFactory", poolV3: "Pool", challengeV3: "ChallengeAccount", registry: "KeyRegistry" };
  const off = [];
  for (const [kind, name] of Object.entries(contractOf)) {
    const deployed = new Set(DEPLOYED.contracts[name]);
    // Functions and events: what the app calls and the logs it asks for. An error line only lets a refusal be named
    // (lib/ui.js, refusalWords); one a contract never raises costs nothing, so errors are not held here.
    for (const line of ABI[kind].filter((l) => !l.startsWith("error "))) {
      const n = normal(line);
      if (!deployed.has(n)) off.push(`${kind}: ${n}`);
    }
  }
  // Two lines the app reads past on purpose, and only these. rules(): four of the five fields, the fifth read raw
  // where it is wanted (chain.assetCaps, deployments.rulesFields). startSurplus(): added to the contracts after the
  // deployed commit; an account without it refuses, and lib/verdict.js reads that refusal as 0.
  const RULES5 = "function rules()->((uint16,uint16,uint32,uint32[],uint32[]))";
  assert.deepEqual(off.sort(), [
    "challengeV3: function rules()->((uint16,uint16,uint32,uint32[]))",
    "challengeV3: function startSurplus()->(uint64)",
    "poolV3: function rules()->((uint16,uint16,uint32,uint32[]))",
    "poolV3: function startSurplus()->(uint64)",
  ]);
  assert.ok(DEPLOYED.contracts.Pool.includes(RULES5) && DEPLOYED.contracts.ChallengeAccount.includes(RULES5));
  assert.equal(DEPLOYED.contracts.Pool.some((l) => l.startsWith("function startSurplus(")), false);
  assert.match(text("../lib/verdict.js"), /if \(reverted\(e\)\) return 0;/);
  // The v3 factory is the one createPool goes to with the rules' fifth field: that line is in the deployed ABI too.
  assert.ok(DEPLOYED.contracts.PoolFactory.includes(normal(ABI.factoryV3.find((l) => l.startsWith("function createPool(")))));
});

test("v3Facts reads a v3 account only, through the v3 ABI of a pool or a challenge", () => {
  const body = CHAIN.slice(CHAIN.indexOf("export async function v3Facts("), CHAIN.indexOf("\n}\n", CHAIN.indexOf("export async function v3Facts(")));
  assert.match(body, /if \(!deployment\?\.assetLeverage\) return null;/);
  assert.match(body, /contract\(kind === "challenge" \? "challengeV3" : "poolV3", address, runner\)/);
  for (const call of ["dex", "agentKey", "agentApprovedAt", "agentValidUntil", "sentToDex", "accountEquity"]) {
    assert.match(body, new RegExp(`c\\.${call}\\(\\)`), call);
  }
  assert.match(body, /sentToDex: Number\(sent\) \/ 1e6, equity: Number\(equity\) \/ 1e6/);
});

test("a trading key's approval: none, valid to the second before it ends, ended from that second", () => {
  // demo3's live challenge and its pool, as the chain answered on 5 Nov 2026 at 1793863993.
  const now = 1793863993;
  const challenge = { approvedAt: 1793617339, validUntilMs: 1809082939000 };
  assert.deepEqual(keyTerm(challenge, now), { state: "valid", until: 1809082939, days: 176 });
  assert.equal(keyTermWords(keyTerm(challenge, now)), "Its trading key is approved until 2027-04-30 11:02 UTC, 176 days from now.");
  // A pool whose challenge is running has no key of its own: approvedAt 0, and validUntil is then the bare term.
  const pool = { approvedAt: 0, validUntilMs: 15465600000 };
  assert.deepEqual(keyTerm(pool, now), { state: "none" });
  assert.equal(keyTermWords(keyTerm(pool, now)), "No trading key is approved on this account.");
  assert.deepEqual(keyTerm(challenge, 1809082938), { state: "valid", until: 1809082939, days: 0 });
  assert.equal(keyTermWords(keyTerm(challenge, 1809082938 - 86400)).endsWith("1 day from now."), true);
  assert.deepEqual(keyTerm(challenge, 1809082939), { state: "ended", until: 1809082939 });
  assert.equal(keyTermWords(keyTerm(challenge, 1809082939)),
    "Its trading key's approval ended at 2027-04-30 11:02 UTC; Hyperliquid takes no order signed with it.");
  // Without the time now there is nothing to measure against: it says so rather than answering NaN days.
  for (const now of [undefined, null, "", "soon", NaN]) {
    assert.throws(() => keyTerm({ approvedAt: 1, validUntilMs: 2_000_000_000_000 }, now), /keyTerm needs the time now/);
  }
});
