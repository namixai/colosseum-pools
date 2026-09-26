// node --test app/tests/stages.test.mjs
//
// A pool's stage 4, "Passed, waiting for a key", added at the end of the contract's list. The pages
// read pools of both factories, so the list here has to agree with src/Pool.sol wherever the contract
// has a stage, and a stage the contract has not got yet can only come after all of its own.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { STAGE, STAGE_IDS, stageName, stageWords, isFundedStage, spanWords, awaitingKeyWords } from "../lib/stages.js";
import { saleBlocker } from "../lib/funding.js";
import { pastFundedStage } from "../lib/verdict.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(ROOT, "src", "Pool.sol"), "utf8");
// Comments first, then commas: a comment inside the enum may have commas of its own.
const CONTRACT = SOURCE.match(/enum Stage\s*\{([^}]*)\}/)[1].replace(/\/\/[^\n]*/g, "").split(",").map((s) => s.trim()).filter(Boolean);

test("the app's stages are the contract's, in its order, and anything more comes after them", () => {
  assert.ok(CONTRACT.length >= 4, `src/Pool.sol has ${CONTRACT.length} stages`);
  assert.deepEqual(STAGE_IDS.slice(0, CONTRACT.length), CONTRACT);
  assert.equal(STAGE.length, STAGE_IDS.length, "one name on the page for each stage");
});

test("stage 4 is the trader who passed and waits for a key, and no stage is shown as undefined", () => {
  assert.equal(STAGE_IDS[4], "PassedAwaitingKey");
  assert.equal(stageName(4), "Passed, waiting for a key");
  assert.equal(stageName("4"), "Passed, waiting for a key");
  assert.equal(stageName(0), "Idle");
  assert.equal(stageName(7), "Stage 7");
  for (let s = 0; s < STAGE.length; s++) assert.ok(stageWords(s).length > 10, `stage ${s} has words`);
  assert.match(stageWords(4), /^Passed, waiting for a key: the trader passed the challenge/);
  assert.match(stageWords(4), /nothing trades on this pool's account/);
  assert.equal(stageWords(9), "Stage 9, which this page does not know yet.");
});

test("a funded trader is shown for a running or settling stage, not for one still waiting for its key", () => {
  assert.deepEqual([0, 1, 2, 3, 4].map(isFundedStage), [false, false, true, true, false]);
  assert.equal(isFundedStage("2"), true);
});

test("a pool waiting for a key is taken: it sells no challenge", () => {
  const ready = { ready: true, challenge: "0x0000000000000000000000000000000000000000", spot: 50, needed: 34 };
  assert.equal(saleBlocker({ ...ready, stage: 0 }), null);
  assert.deepEqual(saleBlocker({ ...ready, stage: 4 }), { kind: "taken" });
});

test("a pool waiting for a key is not read as a funded stage that is over", () => {
  assert.equal(pastFundedStage({ kind: "pool", stage: 4, cutBlock: 65206003 }), false);
  assert.equal(pastFundedStage({ kind: "pool", stage: 0, cutBlock: 65206003 }), true);
});

test("the time left is said in days and hours, and never as a negative", () => {
  assert.equal(spanWords(0), "less than an hour");
  assert.equal(spanWords(3599), "less than an hour");
  assert.equal(spanWords(3600), "1 hour");
  assert.equal(spanWords(7 * 3600), "7 hours");
  assert.equal(spanWords(86400), "1 day");
  assert.equal(spanWords(86400 + 3600), "1 day and 1 hour");
  assert.equal(spanWords(6 * 86400 + 12 * 3600 + 59), "6 days and 12 hours");
  assert.equal(spanWords(-5), "less than an hour");
});

test("a pool waiting for a key says how long before it may step back, and the second it may", () => {
  const passedAt = 1790400000, window = 7 * 86400;
  const early = awaitingKeyWords({ passedAt, window, now: passedAt + 12 * 3600 });
  assert.equal(early, "Passed, waiting for a key: the trader passed the challenge, and the funded stage opens as soon as "
    + "a trading key is free. 6 days and 12 hours left before the pool may step back: after 2026-10-03 05:20 UTC anyone "
    + "may release it to Idle, and the trader keeps the pass and the challenge share.");
  // abandonFundedStage reverts TooEarly up to and including passedAt + window.
  assert.match(awaitingKeyWords({ passedAt, window, now: passedAt + window }), /less than an hour left before the pool may step back/);
  assert.match(awaitingKeyWords({ passedAt, window, now: passedAt + window + 1 }), /The wait is over: anyone may now release the pool to Idle/);
});

test("where the contract has stage 4, it has the clock the pool page reads", () => {
  if (!CONTRACT.includes("PassedAwaitingKey")) return;   // a checkout whose Pool.sol predates the stage
  assert.match(SOURCE, /uint64 public passedAt;/);
  assert.match(SOURCE, /uint64 public constant AWAIT_KEY_WINDOW = 7 days;/);
  assert.match(SOURCE, /function abandonFundedStage\(\) external inStage\(Stage\.PassedAwaitingKey\)/);
  assert.match(SOURCE, /if \(block\.timestamp <= passedAt \+ AWAIT_KEY_WINDOW\) revert TooEarly\(\);/);
});
