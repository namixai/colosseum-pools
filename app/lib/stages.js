// A pool's stage: the number src/Pool.sol stores, the name a page shows, and what the pool is doing.
//
// "Passed, waiting for a key" was added at the END, as 4, not between Challenge and Funded where it
// happens. The app reads pools of two factories, the first deployment and the one beside it, and a
// number that meant one thing on one and another on the other would need a branch on the deployment
// everywhere a stage is shown. Appended, 0 to 3 mean the same on both, and a pool of the older factory
// simply never reports 4. The price is that the order of the list is no longer the order of a life.

/** The contract's own names, in its order. app/tests/stages.test.mjs holds them to src/Pool.sol and to the
 *  same list in app/lib/chain.js (STAGE), which scripts/abi-check.py reads. */
export const STAGE_IDS = ["Idle", "Challenge", "Funded", "Closing", "PassedAwaitingKey"];

/** The name a page shows beside a pool, one for each of STAGE_IDS: a page never shows the contract's identifier. */
export const STAGE_NAMES = ["Idle", "Challenge", "Funded", "Closing", "Passed, waiting for a key"];

/** A stage this app does not know is shown as its number: a new one must never render as "undefined". */
export function stageName(stage) {
  return STAGE_NAMES[Number(stage)] ?? `Stage ${Number(stage)}`;
}

const WORDS = [
  "Idle: it can sell a challenge.",
  "A challenge is on it: its own page says whether it has started.",
  "A funded stage is running: the trader who passed trades the pool's own capital.",
  "Closing: the funded stage has ended and is settling; the pool is idle again once it has.",
  "Passed, waiting for a key: the trader passed the challenge, and the funded stage opens as soon as a "
    + "trading key is free. Until then nothing trades on this pool's account.",
];

/** One sentence on what the pool is doing, for the pool page and the check-it-yourself page. */
export function stageWords(stage) {
  return WORDS[Number(stage)] ?? `Stage ${Number(stage)}, which this page does not know yet.`;
}

// To the second: the contract releases the pool strictly after passedAt + window, and a page that said
// only the minute would name a moment at which the release still reverts.
const utc = (seconds) => new Date(Number(seconds) * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC";

/** Seconds as the page says them: "6 days and 9 hours", "9 hours", "less than an hour". */
export function spanWords(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds)));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  if (d > 0) return h > 0 ? `${plural(d, "day")} and ${plural(h, "hour")}` : plural(d, "day");
  return h > 0 ? plural(h, "hour") : "less than an hour";
}

/** Stage 4 with its clock: the pool may be released to Idle only strictly after passedAt + window
 *  (Pool.abandonFundedStage reverts TooEarly up to and including that second), and only while there is no
 *  key to be had: a live reserved key or a free one in the registry makes it revert KeyAvailable, because
 *  then the right call is to open the funded stage, which anyone may make. */
export function awaitingKeyWords({ passedAt, window, now }) {
  const release = Number(passedAt) + Number(window);
  const head = "Passed, waiting for a key: the trader passed the challenge, and the funded stage opens as soon "
    + "as a trading key is free.";
  const keeps = "and the trader keeps the pass and the challenge share.";
  const noKey = "if there is still no key to be had";
  return Number(now) > release
    ? `${head} The wait is over: ${noKey}, anyone may now release the pool to Idle, ${keeps}`
    : `${head} ${spanWords(release - Number(now))} left before the pool may step back: after ${utc(release)}, ${noKey}, `
      + `anyone may release it to Idle, ${keeps}`;
}

/** A funded trader is on the pool: the stage is running (2) or settling (3). By name, not by order:
 *  "stage 2 or later" would take 4, where the trader has passed and nothing is funded yet. */
export function isFundedStage(stage) {
  return Number(stage) === 2 || Number(stage) === 3;
}

/**
 * What a page shows for a pool: the name on its badge, the badge's tone, and one sentence on what it is doing.
 * The stage alone said "Idle: it can sell a challenge" for a pool that could not -- short on HyperCore, not
 * prepared, or still settling the last challenge -- while the row above named the gap. `blocker` is
 * funding.js saleBlocker's answer for the same pool, so the badge and the buy button decide from one reading.
 */
export function poolStatus(stage, blocker, archived = false) {
  // A pool of the archived deployment sells nothing whatever it holds: the gateway and the keepers serve the live one.
  if (archived && Number(stage) === 0) {
    return { name: "Archive", tone: "", words: "Archive: this pool belongs to the first deployment, which sells no "
      + "challenges." };
  }
  if (Number(stage) !== 0 || !blocker || blocker.kind === "taken") {
    return { name: stageName(stage), tone: Number(stage) === 0 ? "ok" : "", words: stageWords(stage) };
  }
  if (blocker.kind === "underfunded") {
    return {
      name: "Awaiting top-up", tone: "",
      words: `Awaiting top-up: idle, but ${Number(blocker.short).toFixed(2)} USDC short on HyperCore, so it cannot `
        + "sell a challenge until its investor moves USDC across.",
    };
  }
  if (blocker.kind === "not-prepared") {
    return { name: "Not prepared", tone: "", words: "Not prepared: idle, but its investor has not prepared the account, "
      + "so it cannot sell a challenge yet." };
  }
  return { name: "Settling", tone: "", words: "Settling: idle, but the last challenge is still settling, so it cannot "
    + "sell the next one until that ends." };
}
