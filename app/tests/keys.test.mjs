// node --test app/tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { keyFacts, isZero, sameAddress, MAX_CHAIN_READS_ON_LOAD, ZERO } from "../lib/keys.js";
import { friendly, rateLimited } from "../lib/ui.js";

// Hex letters on purpose: an all-digit address would make every case test pass by accident.
const KEY = "0xaBcDeF1111111111111111111111111111111111";
const CUT = "0xFeDcBa2222222222222222222222222222222222";
const ACCOUNT = "0x3333333333333333333333333333333333333333";

/** Counts the chain reads and refuses any call the panel is not allowed to make on load. */
function reader({ agentKey = ZERO, keyOf = ZERO, cutKey = ZERO, cutBlock = 0 } = {}) {
  const seen = [];
  const chainRead = (name, value) => (...args) => { seen.push(name); return Promise.resolve(value(...args)); };
  const reads = new Proxy({
    agentKey: chainRead("agentKey", () => agentKey),
    keyOf: chainRead("keyOf", () => keyOf),
    cutKey: chainRead("cutKey", () => cutKey),
    cutBlock: chainRead("cutBlock", () => cutBlock),
    bindingOf: chainRead("bindingOf", (key) => ({ state: 2, trader: ACCOUNT, key })),
    // Hyperliquid's API, not the RPC: counted apart.
    role: (key) => { seen.push("role*"); return Promise.resolve({ role: "agent", data: { user: ACCOUNT } }); },
  }, {
    get(target, name) {
      if (!(name in target)) throw new Error(`the panel may not call ${String(name)} while it loads`);
      return target[name];
    },
  });
  return { reads, seen };
}

const rpcReads = (seen) => seen.filter((n) => !n.endsWith("*")).length;

test("the key panel draws from contract state, in a handful of reads", async () => {
  const { reads, seen } = reader({ agentKey: KEY, keyOf: KEY, cutKey: CUT, cutBlock: 64_600_000 });
  const facts = await keyFacts(reads);

  assert.ok(rpcReads(seen) <= MAX_CHAIN_READS_ON_LOAD,
    `${rpcReads(seen)} chain reads on load, the panel is allowed ${MAX_CHAIN_READS_ON_LOAD}`);
  assert.equal(facts.current, KEY);
  assert.equal(facts.cutKey, CUT);
  assert.equal(facts.cutBlock, 64_600_000);
  // The key approved now and the key a stop cut, named without touching the event log.
  assert.deepEqual(facts.keys.map((k) => k.address), [KEY, CUT]);
  assert.deepEqual(facts.keys.map((k) => k.isCurrent), [true, false]);
  assert.deepEqual(facts.keys.map((k) => k.wasCut), [false, true]);
});

test("an account that never held a key reads nothing about keys", async () => {
  const { reads, seen } = reader();
  const facts = await keyFacts(reads);
  assert.deepEqual(facts.keys, []);
  assert.equal(facts.current, null);
  assert.equal(facts.cutKey, null);
  assert.equal(facts.cutBlock, 0);
  assert.equal(rpcReads(seen), 4, "the four state reads and nothing per key");
});

test("the same address bound and approved is named once", async () => {
  const { reads } = reader({ agentKey: KEY, keyOf: KEY.toUpperCase().replace("0X", "0x") });
  const facts = await keyFacts(reads);
  assert.equal(facts.keys.length, 1);
});

test("the panel may not read the event log while it loads", () => {
  // The burst that broke this section came from two log scans on the load path. keyFacts is
  // held to its reader above; this holds the view that calls it.
  const source = readFileSync(new URL("../views/verify.js", import.meta.url), "utf8");
  const start = source.indexOf("async function keysPanel(");
  assert.ok(start > 0, "keysPanel is still the panel");
  const after = source.indexOf("\nasync function ", start + 1);
  const body = source.slice(start, after > 0 ? after : undefined);
  assert.doesNotMatch(body, /chain\.history\(/, "keysPanel reads the event log on load again");
  assert.doesNotMatch(body, /queryFilter\(/, "keysPanel reads the event log on load again");
});

test("zero addresses and case differences", () => {
  assert.equal(isZero(ZERO), true);
  assert.equal(isZero(null), true);
  assert.equal(isZero(KEY), false);
  assert.equal(sameAddress(KEY, KEY.toUpperCase().replace("0X", "0x")), true);
  assert.equal(sameAddress(KEY, CUT), false);
  assert.equal(sameAddress(null, null), false);
});

test("a rate-limited RPC is said in words, not pasted as a payload", () => {
  const wrapped = Object.assign(new Error('could not coalesce error (error={ "code": -32005 }, payload={ "id": 58 })'),
    { error: { code: -32005, message: "rate limited" } });
  assert.equal(rateLimited(wrapped), true);
  assert.match(friendly(wrapped), /rate limited/);
  assert.doesNotMatch(friendly(wrapped), /payload|jsonrpc|coalesce/);

  assert.equal(rateLimited(new Error("execution reverted")), false);
  assert.match(friendly(new Error("execution reverted")), /execution reverted/);
  assert.equal(rateLimited({ info: { error: { code: -32005 } } }), true);
});
