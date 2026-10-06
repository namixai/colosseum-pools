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
 * stopped trading for good -- a challenge past Active, a pool in Closing; `waiting` says it has not
 * begun -- a challenge that is bought and not started (`notActiveYet`).
 *
 * A challenge that passed is as empty as one that was stopped: it hands its capital back and
 * ends at Settled with no reason recorded, so without `finished` the live reading would call a
 * pass a drawdown. Review caught that before the first graduate could show it to anyone.
 */
export function ruleVerdict({ recorded = 0, live = 0, stopped = false, finished = false, waiting = false } = {}) {
  if (Number(recorded)) return { kind: "recorded", reason: Number(recorded) };
  if (finished) return { kind: "finished-with-no-rule-broken" };
  if (stopped) return { kind: "stopped-without-a-recorded-reason" };
  if (waiting) return { kind: "not-active-yet" };
  return { kind: "live", reason: Number(live) };
}

/**
 * A challenge that is bought and not started: status Created (1).
 *
 * The contract judges nothing there. `breach` takes an Active challenge and reverts on any other, so no rule
 * can stop this account yet. A live `violation()` still answers, and until the capital arrives it answers
 * Drawdown: the account's equity reads 0 against a drawdown line that is already the challenge's capital. A page
 * that showed that reading would show a buyer a stop that cannot happen, on the challenge they have just paid for.
 * The repository's client says the same of this state, in the same words (agents/desk.py).
 */
export function notActiveYet({ kind, status } = {}) {
  return kind === "challenge" && Number(status) === 1;
}

/** What a page says in place of a verdict for such a challenge, and why no reading is shown beside it. */
export const NOT_ACTIVE_YET = "not active yet: nothing is judged before activation";
export const WHY_NOT_JUDGED = "This challenge is bought and not started. The contract stops an Active challenge "
  + "only, so no rule applies to it yet. Its violation() still answers when asked, and until the capital arrives "
  + "it answers Drawdown: the account's equity reads 0 against a line already set from the capital. That is a "
  + "reading of an empty account, not a stop, so this page does not show it.";

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
 * settling (3) is present tense, and the page already has words for those. So is 4, a trader who
 * passed and waits for a key: nothing funded has happened yet, whatever an earlier stage left in
 * `cutBlock`.
 */
export function pastFundedStage({ kind, stage = 0, cutBlock = 0 } = {}) {
  return kind === "pool" && Number(cutBlock) > 0 && Number(stage) === 0;
}
