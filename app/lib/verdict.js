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
 * `live` is violation() computed now; `stopped` is isStopped().
 */
export function ruleVerdict({ recorded = 0, live = 0, stopped = false } = {}) {
  if (Number(recorded)) return { kind: "recorded", reason: Number(recorded) };
  if (stopped) return { kind: "stopped-without-a-recorded-reason" };
  return { kind: "live", reason: Number(live) };
}

/** True when a live reading would be about an account that no longer holds what it was judged on. */
export function liveReadingIsMoot(verdict) {
  return verdict.kind !== "live";
}
