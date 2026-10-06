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
import { STAGE_NAMES, STAGE_IDS, stageName, stageWords, isFundedStage, spanWords, awaitingKeyWords } from "../lib/stages.js";
import { saleBlocker } from "../lib/funding.js";
import { pastFundedStage, ruleVerdict, liveReadingIsMoot, notActiveYet, NOT_ACTIVE_YET, WHY_NOT_JUDGED } from "../lib/verdict.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(ROOT, "src", "Pool.sol"), "utf8");
// Comments first, then commas: a comment inside the enum may have commas of its own.
const CONTRACT = SOURCE.match(/enum Stage\s*\{([^}]*)\}/)[1].replace(/\/\/[^\n]*/g, "").split(",").map((s) => s.trim()).filter(Boolean);
// chain.js keeps the contract's names as one literal line, the one scripts/abi-check.py holds to the contract.
const CHAIN_STAGE = JSON.parse(readFileSync(join(ROOT, "app", "lib", "chain.js"), "utf8")
  .match(/^export const STAGE = (\[[^\]\n]*\]);$/m)[1]);

test("the app's stages are the contract's, in its order, and anything more comes after them", () => {
  assert.ok(CONTRACT.length >= 4, `src/Pool.sol has ${CONTRACT.length} stages`);
  assert.deepEqual(STAGE_IDS.slice(0, CONTRACT.length), CONTRACT);
  assert.deepEqual(CHAIN_STAGE, STAGE_IDS, "chain.js names the same stages as stages.js");
  assert.equal(STAGE_NAMES.length, STAGE_IDS.length, "one name on the page for each stage");
});

test("stage 4 is the trader who passed and waits for a key, and no stage is shown as undefined", () => {
  assert.equal(STAGE_IDS[4], "PassedAwaitingKey");
  assert.equal(stageName(4), "Passed, waiting for a key");
  assert.equal(stageName("4"), "Passed, waiting for a key");
  assert.equal(stageName(0), "Idle");
  assert.equal(stageName(7), "Stage 7");
  for (let s = 0; s < STAGE_NAMES.length; s++) assert.ok(stageWords(s).length > 10, `stage ${s} has words`);
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
    + "a trading key is free. 6 days and 12 hours left before the pool may step back: after 2026-10-03 05:20:00 UTC, if "
    + "there is still no key to be had, anyone may release it to Idle, and the trader keeps the pass and the challenge share.");
  // The release time to the second: 45 seconds past the minute, and the page says so, not just the minute.
  assert.match(awaitingKeyWords({ passedAt: passedAt + 45, window, now: passedAt }), /after 2026-10-03 05:20:45 UTC,/);
  // abandonFundedStage reverts TooEarly up to and including passedAt + window.
  assert.match(awaitingKeyWords({ passedAt, window, now: passedAt + window }), /less than an hour left before the pool may step back/);
  assert.match(awaitingKeyWords({ passedAt, window, now: passedAt + window + 1 }),
    /The wait is over: if there is still no key to be had, anyone may now release the pool to Idle/);
});

test("where the contract has stage 4, it has the clock the pool page reads", () => {
  if (!CONTRACT.includes("PassedAwaitingKey")) return;   // a checkout whose Pool.sol predates the stage
  assert.match(SOURCE, /uint64 public passedAt;/);
  assert.match(SOURCE, /uint64 public constant AWAIT_KEY_WINDOW = 7 days;/);
  assert.match(SOURCE, /function abandonFundedStage\(\) external inStage\(Stage\.PassedAwaitingKey\)/);
  assert.match(SOURCE, /if \(block\.timestamp <= passedAt \+ AWAIT_KEY_WINDOW\) revert TooEarly\(\);/);
  // Where the release also waits for there to be no key, the page says so on both sides of the window.
  if (/revert KeyAvailable\(\);/.test(SOURCE)) {
    const passedAt = 1790400000, window = 7 * 86400;
    for (const now of [passedAt, passedAt + window + 1]) {
      assert.match(awaitingKeyWords({ passedAt, window, now }), /if there is still no key to be had, anyone may/);
    }
  }
});

test("a challenge that is bought and not started is shown as not judged, never as a drawdown", () => {
  const view = (name) => readFileSync(join(ROOT, "app", "views", name), "utf8");
  // Status 1 is Created in the contract's own list, and the only status `breach` takes is Active.
  const account = readFileSync(join(ROOT, "src", "ChallengeAccount.sol"), "utf8");
  const statuses = account.match(/enum Status\s*\{([^}]*)\}/)[1].replace(/\/\/[^\n]*/g, "").split(",").map((x) => x.trim()).filter(Boolean);
  assert.equal(statuses[1], "Created");
  assert.equal(statuses[2], "Active");
  assert.match(account, /function breach\([^)]*\)\s+external\s+inStatus\(Status\.Active\)/);
  // What the contract answers for such an account until its capital arrives: equity 0 against a line that is
  // already the capital, so `violation()` says Drawdown (1). That is the reading the page must not show.
  assert.match(account, /function drawdownBase\(\) public view override returns \(int64\) \{\s+return int64\(_terms\.capital\);/);
  const ruled = readFileSync(join(ROOT, "src", "RuledAccount.sol"), "utf8");
  assert.match(ruled, /if \(eq \* bps < base \* \(bps - int256\(uint256\(_rules\.maxDrawdownBps\)\)\)\) return Breach\.Drawdown;/);

  assert.equal(notActiveYet({ kind: "challenge", status: 1 }), true);
  assert.equal(notActiveYet({ kind: "challenge", status: 1n }), true);
  for (const status of [0, 2, 3, 4, 5, 6, 7, 8]) assert.equal(notActiveYet({ kind: "challenge", status }), false, `status ${status}`);
  // A pool's stage 1 is a pool with a challenge on it, not an account waiting to start.
  assert.equal(notActiveYet({ kind: "pool", status: 1 }), false);
  assert.equal(notActiveYet(), false);

  // Bought, capital not there yet: the live reading is Drawdown, and the verdict is "not active yet".
  const waiting = ruleVerdict({ recorded: 0, live: 1, stopped: false, finished: false, waiting: true });
  assert.deepEqual(waiting, { kind: "not-active-yet" });
  assert.equal(liveReadingIsMoot(waiting), true, "a reading of an account that is not judged is not shown as a verdict");
  // The same once the capital has arrived and the reading turns to None: still not judged, not "inside the rules".
  assert.deepEqual(ruleVerdict({ live: 0, waiting: true }), { kind: "not-active-yet" });
  // Without the flag an Active account with the same reading is a live verdict, as before.
  assert.deepEqual(ruleVerdict({ recorded: 0, live: 1 }), { kind: "live", reason: 1 });
  // Anything the contract wrote down, or an account that has ended, still comes first.
  assert.deepEqual(ruleVerdict({ recorded: 3, live: 1, waiting: true }), { kind: "recorded", reason: 3 });
  assert.deepEqual(ruleVerdict({ live: 1, stopped: true, finished: true, waiting: true }), { kind: "finished-with-no-rule-broken" });
  assert.deepEqual(ruleVerdict({ live: 1, stopped: true, waiting: true }), { kind: "stopped-without-a-recorded-reason" });

  // The words are the client's own for the same state.
  assert.equal(NOT_ACTIVE_YET, "not active yet: nothing is judged before activation");
  assert.match(WHY_NOT_JUDGED, /bought and not started/);
  assert.match(WHY_NOT_JUDGED, /stops an Active challenge only/);
  assert.match(WHY_NOT_JUDGED, /not a stop, so this page does not show it\.$/);

  // The check-it-yourself page: the one page that asks `violation()` whatever the status.
  const verify = view("verify.js");
  assert.match(verify, /const verdict = ruleVerdict\(\{ recorded, live, stopped, finished, waiting: notActiveYet\(\{ kind, status: state \}\) \}\);/);
  assert.match(verify, /verdict\.kind === "not-active-yet"\s+\? row\("The contract's verdict", badge\(NOT_ACTIVE_YET, ""\)\)/);
  assert.match(verify, /\$\{verdict\.kind === "not-active-yet" \? `<p class="small muted">\$\{esc\(WHY_NOT_JUDGED\)\}<\/p>`\s+: liveReadingIsMoot\(verdict\) \?/);
  // The challenge page reads no equity and no verdict before the start, and gives the trade panel to Active alone.
  const challenge = view("challenge.js");
  assert.match(challenge, /if \(s >= 2\) settle\(equityPanel\(/);
  assert.match(challenge, /if \(s === 2 && isTrader && !archived\) settle\(tradePanel\(/);
  assert.match(challenge, /\$\{s >= 2 \? row\("Deadline", esc\(when\(deadline\)\)\) : ""\}/);
  assert.match(challenge, /const tone = s === 2 \? "ok" : s === 6 \|\| s === 8 \? "ok" : stopped \? "bad" : "";/);
  // A shared pool's seat names the challenge's status as the contract has it, and a deadline only once there is one.
  const shared = view("shared.js");
  assert.match(shared, /status = row\("Challenge", `\$\{name\}, \$\{esc\(chain\.STATUS\[Number\(s\)\]\)\}\$\{\s+Number\(deadline\) \? `, until \$\{esc\(when\(deadline\)\)\}` : ""\}`\);/);
  // A pool with a challenge on it does not say the challenge is running: it may not have started.
  assert.equal(stageWords(1), "A challenge is on it: its own page says whether it has started.");
  assert.doesNotMatch(stageWords(1), /running|active/i);
});
