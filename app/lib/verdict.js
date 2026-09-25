// What a page should say about the rules for one account.
//
// A stopped account is read twice over: the contract recorded a reason when it stopped it, and
// anyone can also ask the contract what it makes of the account right now. Those two answers
// drift apart the moment the account is settled and its money leaves: an empty account is a
// hundred per cent below where it started, so the live reading says "drawdown" whatever the
// stop was actually for. On 24 Sep 2026 that is what the demo showed a visitor -- "Drawdown" on
// a challenge the contract had stopped for leverage.
//
// So: if a reason was recorded, that is the answer, and the live reading is not shown next to
// it as an equal. The live reading is for accounts that are still trading, where it is the only
// thing that can be asked.

/**
 * `recorded` is breachReason() where the account keeps one (a challenge) and 0 otherwise;
 * `live` is violation() computed now; `stopped` is isStopped(); `finished` says the account has
 * stopped trading for good -- a challenge past Active, a pool in Closing.
 *
 * A challenge that passed is as empty as one that was stopped: it hands its capital back and
 * ends at Settled with no reason recorded, so without `finished` the live reading would call a
 * pass a drawdown. Review caught that before the first graduate could show it to anyone.
 */
export function ruleVerdict({ recorded = 0, live = 0, stopped = false, finished = false } = {}) {
  if (Number(recorded)) return { kind: "recorded", reason: Number(recorded) };
  if (finished) return { kind: "finished-with-no-rule-broken" };
  if (stopped) return { kind: "stopped-without-a-recorded-reason" };
  return { kind: "live", reason: Number(live) };
}

/** True when a live reading would be about an account that no longer holds what it was judged on. */
export function liveReadingIsMoot(verdict) {
  return verdict.kind !== "live";
}

/**
 * Whether what this account has to say is about a funded stage that is OVER.
 *
 * A pool keeps `cutBlock` — the block where it cut the funded trader's key — and keeps it after
 * the stage has ended and the capital has come home. That is the only thing on the pool that
 * tells the two quiet cases apart: a funded stage that ended CLEANLY records `fundedEndReason`
 * None, which is byte for byte what a pool that has never funded anyone records. Before this, a
 * judge arriving after a completed cycle saw the second and could not know it was the first.
 *
 * Stage 0 is the condition, not just a non-zero block: a stage still running (2) or still
 * settling (3) is present tense, and the page already has words for those.
 */
export function pastFundedStage({ kind, stage = 0, cutBlock = 0 } = {}) {
  return kind === "pool" && Number(cutBlock) > 0 && Number(stage) === 0;
}
