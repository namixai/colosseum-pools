// node --test app/tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { keyFacts, isZero, sameAddress, MAX_CHAIN_READS_ON_LOAD, ZERO } from "../lib/keys.js";
import { friendly, rateLimited, failureHint } from "../lib/ui.js";
import { ruleVerdict, liveReadingIsMoot, pastFundedStage } from "../lib/verdict.js";
import * as hl from "../lib/hl.js";

// Hex letters on purpose: an all-digit address would make every case test pass by accident.
const KEY = "0xaBcDeF1111111111111111111111111111111111";
const CUT = "0xFeDcBa2222222222222222222222222222222222";
const ACCOUNT = "0x3333333333333333333333333333333333333333";

/** Counts the chain reads and refuses any call the panel is not allowed to make on load. */
function reader({ agentKey = ZERO, keyOf = ZERO, cutKey = ZERO, cutBlock = 0, roleFails = null } = {}) {
  const seen = [];
  const chainRead = (name, value) => (...args) => { seen.push(name); return Promise.resolve(value(...args)); };
  const reads = new Proxy({
    agentKey: chainRead("agentKey", () => agentKey),
    keyOf: chainRead("keyOf", () => keyOf),
    cutKey: chainRead("cutKey", () => cutKey),
    cutBlock: chainRead("cutBlock", () => cutBlock),
    bindingOf: chainRead("bindingOf", (key) => ({ state: 2, trader: ACCOUNT, key })),
    // Hyperliquid's API, not the RPC: counted apart.
    role: (key) => {
      seen.push("role*");
      if (roleFails === "reject") return Promise.reject(new Error("hyperliquid info api is down"));
      if (roleFails === "throw") throw new Error("hyperliquid info api is down");
      return Promise.resolve({ role: "agent", data: { user: ACCOUNT } });
    },
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
  assert.equal(facts.chainReads, rpcReads(seen), "the number the page prints about itself is the number it made");
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
  assert.equal(facts.chainReads, 4);
});

test("the same address bound and approved is named once", async () => {
  const { reads } = reader({ agentKey: KEY, keyOf: KEY.toUpperCase().replace("0X", "0x") });
  const facts = await keyFacts(reads);
  assert.equal(facts.keys.length, 1);
});

test("a stop shows the reason the contract recorded, not one computed on an empty account", () => {
  // 24 Sep 2026 on the live demo: a challenge the contract stopped for leverage (3) read
  // "Drawdown" (1) on its own page, because after settling the account holds nothing and a
  // reading of nothing is a hundred per cent below where it started.
  const settled = ruleVerdict({ recorded: 3, live: 1, stopped: true });
  assert.deepEqual(settled, { kind: "recorded", reason: 3 });
  assert.equal(liveReadingIsMoot(settled), true, "the live reading would be shown as an equal");

  // Still trading: the live reading is the only thing that can be asked, and it is shown.
  const trading = ruleVerdict({ recorded: 0, live: 0, stopped: false });
  assert.deepEqual(trading, { kind: "live", reason: 0 });
  assert.equal(liveReadingIsMoot(trading), false);
  assert.deepEqual(ruleVerdict({ recorded: 0, live: 2, stopped: false }), { kind: "live", reason: 2 });

  // A pool records no reason of its own: say it is stopped rather than invent one.
  assert.deepEqual(ruleVerdict({ recorded: 0, live: 1, stopped: true }), { kind: "stopped-without-a-recorded-reason" });

  // A challenge that PASSED is as empty as one that was stopped: it hands the capital back and
  // ends at Settled with no reason recorded. Without this the first graduate would be shown as
  // a drawdown -- the same defect, on the other branch, and review caught it before it ran.
  const passed = ruleVerdict({ recorded: 0, live: 1, stopped: false, finished: true });
  assert.deepEqual(passed, { kind: "finished-with-no-rule-broken" });
  assert.equal(liveReadingIsMoot(passed), true);
  // A stopped account still carries its recorded reason ahead of anything else.
  assert.deepEqual(ruleVerdict({ recorded: 3, live: 1, stopped: true, finished: true }), { kind: "recorded", reason: 3 });

  // The pages that render it know when an account has finished.
  const livePanel = readFileSync(new URL("../views/trading.js", import.meta.url), "utf8");
  assert.match(livePanel, /finished/, "the live panel cannot tell a finished account from a trading one");
  const poolView = readFileSync(new URL("../views/pool.js", import.meta.url), "utf8");
  assert.match(poolView, /stopped: Number\(stage\) === 3/, "a pool in Closing tells the panel nothing");
  // A pool keeps its own reason when it stops a funded trader; throwing it away would leave the
  // page saying "stopped" where the contract can say what for.
  assert.match(poolView, /pool\.fundedEndReason\(\)/, "the pool page drops its own recorded reason");
  assert.doesNotMatch(livePanel, /capital is back with/, "the panel claims settlement has finished");

  // The pages ask for the recorded reason, and the challenge page no longer hides it once the
  // challenge is settled -- which is where a judge reads it.
  const verify = readFileSync(new URL("../views/verify.js", import.meta.url), "utf8");
  assert.match(verify, /breachReason\(\)/, "the verify page does not read the recorded reason");
  assert.match(verify, /finished = kind === "challenge"/, "the verify page cannot tell a finished account");
  assert.match(verify, /account\.fundedEndReason\(\)/, "the verify page drops a pool's recorded reason");
  assert.match(verify, /ruleVerdict\(/);
  const challenge = readFileSync(new URL("../views/challenge.js", import.meta.url), "utf8");
  assert.match(challenge, /ruleVerdict\(/);
  assert.doesNotMatch(challenge, /s === 3 \? row\("Stopped for"/, "the reason is tied to one status again");
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

test("a page that fails to load names a cause only when the error shows one", async () => {
  // What the demo hit: an app that reads a Terms of seven fields, against contracts that return
  // six. ethers 6.17 raises this. No wait fixes it, so the page must not blame a busy RPC.
  const decode = Object.assign(new Error('could not decode result data (value="0x00", code=BAD_DATA, version=6.17.0)'),
    { code: "BAD_DATA", shortMessage: "could not decode result data", info: { method: "terms", signature: "terms()" } });
  assert.match(failureHint(decode), /do not match/);
  assert.doesNotMatch(failureHint(decode), /rate limit|reload in a moment/i);

  // Nothing answered, or a server answered with an error: as the browser and ethers say it.
  for (const down of [new TypeError("Failed to fetch"), new TypeError("NetworkError when attempting to fetch resource."),
    new TypeError("Load failed"), Object.assign(new Error("server response 503"), { code: "SERVER_ERROR", info: { responseStatus: 503 } })]) {
    assert.match(failureHint(down), /could not be reached/, down.message);
  }
  // Hyperliquid's own answers, as app/lib/hl.js raises them: a failure that may pass, a refusal,
  // and one that says the request itself was wrong.
  const saved = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ ok: false, status: 502 });
    await assert.rejects(hl.info({ type: "meta" }), (err) => /could not be reached/.test(failureHint(err)));
    globalThis.fetch = async () => ({ ok: false, status: 429 });
    await assert.rejects(hl.info({ type: "meta" }), (err) => rateLimited(err)
      // Said in the name of whoever refused: this one is Hyperliquid, not the RPC.
      && /Hyperliquid's testnet API is refusing/.test(friendly(err))
      && !/HyperEVM RPC/.test(friendly(err))
      && failureHint(err) === "");
    // 404 and 400 answer the same way however often the page is reloaded, so it is not told to.
    for (const status of [404, 400]) {
      globalThis.fetch = async () => ({ ok: false, status });
      await assert.rejects(hl.info({ type: "meta" }), (err) => failureHint(err) === "");
    }
  } finally {
    globalThis.fetch = saved;
  }
  // The same, from ethers: a status it carries in info decides, whatever its code says.
  assert.equal(failureHint(Object.assign(new Error("server response 404"),
    { code: "SERVER_ERROR", info: { responseStatus: 404 } })), "");

  // Rate limited: friendly() says so and when to try again, and the hint doesn't say it twice --
  // nor call a server's 429 "could not be reached".
  const throttled = Object.assign(new Error("server response 429"), { code: "SERVER_ERROR", info: { responseStatus: 429 } });
  assert.equal(rateLimited(throttled), true);
  assert.match(friendly(throttled), /The public HyperEVM RPC is refusing/);
  assert.equal(failureHint(throttled), "");
  assert.equal(failureHint({ info: { error: { code: -32005 } } }), "");
  // Anything else: the message, and no cause the error didn't show.
  assert.equal(failureHint(new Error("execution reverted")), "");
  assert.equal(failureHint(new TypeError("x is not a function")), "");

  // And the page that shows it adds no cause of its own.
  const app = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.doesNotMatch(app, /rate limited/i, "the failure page names a cause whatever the error");
});

test("Hyperliquid failing does not take the contract's own answer down with it", async () => {
  // userRole is a second opinion on facts the contract has already given. Before this, one
  // failed call there turned the whole section back into an error -- the fragility this panel
  // was rebuilt to remove, returning through a different door.
  for (const how of ["reject", "throw"]) {
    const { reads } = reader({ agentKey: KEY, keyOf: KEY, cutKey: CUT, cutBlock: 64_600_000, roleFails: how });
    const facts = await keyFacts(reads);
    assert.equal(facts.current, KEY, `${how}: the approved key is still named`);
    assert.equal(facts.cutKey, CUT, `${how}: the cut key is still named`);
    assert.equal(facts.keys.length, 2);
    for (const k of facts.keys) {
      assert.equal(k.role, null, `${how}: no answer is shown as no answer, not as a guess`);
      assert.equal(k.binding.state, 2, `${how}: the registry's binding still reads`);
    }
  }
});

test("the page prints the read count it made, not a number typed into it", () => {
  const source = readFileSync(new URL("../views/verify.js", import.meta.url), "utf8");
  assert.match(source, /facts\.chainReads/, "the page no longer prints the count it measured");
  assert.doesNotMatch(source, /\bin (four|five|six|\d+) reads\b/i, "a read count is typed into the page again");
  assert.match(source, /!k\.role/, "a key with no answer from Hyperliquid would break the render");
});

// The three pools on the demo factory on 25 Sep 2026, read from the chain. Two of them look
// identical on every field a page used to show; cutBlock is the one that separates them.
const CLEAN_CLOSE = { kind: "pool", stage: 0, cutBlock: 65199047 };   // funded stage ended, no rule broken
const LEVERAGE_STOP = { kind: "pool", stage: 0, cutBlock: 65176406 }; // funded stage stopped for leverage
const NEVER_FUNDED = { kind: "pool", stage: 0, cutBlock: 0 };         // an investor's pool that has not funded anyone

test("a pool that has funded someone says so after the stage is over", () => {
  assert.equal(pastFundedStage(CLEAN_CLOSE), true);
  assert.equal(pastFundedStage(LEVERAGE_STOP), true);
  assert.equal(pastFundedStage(NEVER_FUNDED), false, "a pool nobody has funded must not claim a past stage");
});

test("a stage that is not over yet is present tense", () => {
  assert.equal(pastFundedStage({ ...CLEAN_CLOSE, stage: 2 }), false, "funded and trading now");
  assert.equal(pastFundedStage({ ...CLEAN_CLOSE, stage: 3 }), false, "stopped and still settling");
  assert.equal(pastFundedStage({ ...CLEAN_CLOSE, stage: 1 }), false, "a challenge is running");
});

test("challenges are judged by their own status, not by this", () => {
  assert.equal(pastFundedStage({ kind: "challenge", stage: 0, cutBlock: 65180551 }), false);
});

test("the clean close stops reading like a pool nobody ever funded", () => {
  // Both record fundedEndReason None and neither is stopped. Before cutBlock was read, both
  // produced the same live verdict and the page said "inside the rules" over a finished cycle.
  const asVerdict = (p) => ruleVerdict({ recorded: 0, live: 0, stopped: false, finished: pastFundedStage(p) }).kind;
  assert.equal(asVerdict(CLEAN_CLOSE), "finished-with-no-rule-broken");
  assert.equal(asVerdict(NEVER_FUNDED), "live");
  assert.notEqual(asVerdict(CLEAN_CLOSE), asVerdict(NEVER_FUNDED));
});

test("the verify page actually asks it", () => {
  // Testing pastFundedStage alone would stay green if the view went back to deciding "finished"
  // from the stage alone, which is the state this fixes.
  const src = readFileSync(new URL("../views/verify.js", import.meta.url), "utf8");
  assert.match(src, /pastFundedStage\(\{ kind, stage: state, cutBlock \}\)/, "the view must ask the function");
  assert.match(src, /account\.cutBlock\(\)/, "and must read cutBlock to ask it with");
  assert.doesNotMatch(src, /const finished = kind === "challenge" && Number\(state\) > 2;/,
    "the old pool-blind rule is back");
});
