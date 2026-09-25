// A pool's stage: the number src/Pool.sol stores, the name a page shows, and what the pool is doing.
//
// "Passed, waiting for a key" was added at the END, as 4, not between Challenge and Funded where it
// happens. The app reads pools of two factories, the first deployment and the one beside it, and a
// number that meant one thing on one and another on the other would need a branch on the deployment
// everywhere a stage is shown. Appended, 0 to 3 mean the same on both, and a pool of the older factory
// simply never reports 4. The price is that the order of the list is no longer the order of a life.

/** The contract's own names, in its order. app/tests/stages.test.mjs holds them to src/Pool.sol. */
export const STAGE_IDS = ["Idle", "Challenge", "Funded", "Closing", "PassedAwaitingKey"];

/** The name a page shows beside a pool, one for each of STAGE_IDS. */
export const STAGE = ["Idle", "Challenge", "Funded", "Closing", "Passed, waiting for a key"];

/** A stage this app does not know is shown as its number: a new one must never render as "undefined". */
export function stageName(stage) {
  return STAGE[Number(stage)] ?? `Stage ${Number(stage)}`;
}

const WORDS = [
  "Idle: it can sell a challenge.",
  "A challenge is running on it.",
  "A funded stage is running: the trader who passed trades the pool's own capital.",
  "Closing: the funded stage has ended and is settling; the pool is idle again once it has.",
  "Passed, waiting for a key: the trader passed the challenge, and the funded stage opens as soon as a "
    + "trading key is free. Until then nothing trades on this pool's account.",
];

/** One sentence on what the pool is doing, for the pool page and the check-it-yourself page. */
export function stageWords(stage) {
  return WORDS[Number(stage)] ?? `Stage ${Number(stage)}, which this page does not know yet.`;
}

/** A funded trader is on the pool: the stage is running (2) or settling (3). By name, not by order:
 *  "stage 2 or later" would take 4, where the trader has passed and nothing is funded yet. */
export function isFundedStage(stage) {
  return Number(stage) === 2 || Number(stage) === 3;
}
