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
import { STAGE, STAGE_IDS, stageName, stageWords, isFundedStage } from "../lib/stages.js";
import { saleBlocker } from "../lib/funding.js";
import { pastFundedStage } from "../lib/verdict.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(ROOT, "src", "Pool.sol"), "utf8");
const CONTRACT = SOURCE.match(/enum Stage\s*\{([^}]*)\}/)[1].split(",").map((s) => s.replace(/\/\/.*$/gm, "").trim()).filter(Boolean);

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
