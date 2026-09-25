// Whether a pool can sell its next challenge, and what is missing when it can't.
//
// A pool that has finished a cycle is usually short, and the reason is not a loss. HyperCore
// charges 1 USDC for creating the challenge's account and never returns it, while the price the
// trader paid lands on the other side of the bridge, in `earned` on HyperEVM. So a pool's
// HyperCore balance falls by at least that fee every challenge it sells, and the money meant to
// replace it sits on HyperEVM until the owner moves it across by hand. Nothing on chain does
// that move. Measured on testnet on 25 Sep 2026: a stand pool that sold one challenge, saw it
// pass, funded the trader and took the funded stage back came home with 40.78 USDC on HyperCore
// against the 42.00 it needs to sell the next one -- 1.00 of the gap was the account fee.
//
// Before this the pool page offered "Pay and start" whenever the pool was idle, so a trader paid
// the approval and then watched buyChallenge revert with NotEnoughCapital.

export const NO_CHALLENGE = "0x0000000000000000000000000000000000000000";

/**
 * Why the pool cannot sell a challenge right now, or null when it can.
 * `short` is in the same units as `spot` and `needed` -- the caller's, not the chain's.
 */
export function saleBlocker({ stage = 0, ready = false, challenge = NO_CHALLENGE, spot = 0, needed = 0 } = {}) {
  if (Number(stage) !== 0) return { kind: "taken" };
  if (!ready) return { kind: "not-prepared" };
  if (challenge !== NO_CHALLENGE) return { kind: "settling" };
  const short = Number(needed) - Number(spot);
  if (short > 0) return { kind: "underfunded", short };
  return null;
}

/**
 * What the owner has to do to close a gap: how much of it the income already held in the
 * contract covers, and how much has to come from somewhere else. Returns null when there is
 * no gap, so a funded pool says nothing at all.
 */
export function topUpAdvice({ short = 0, earned = 0 } = {}) {
  if (Number(short) <= 0) return null;
  const fromEarned = Math.min(Number(earned), Number(short));
  return { short: Number(short), fromEarned, stillNeeded: Number(short) - fromEarned };
}
