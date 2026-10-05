// An investor's withdrawal of a pool's capital on HyperCore: what may be asked for, and what can be said after.
//
// `withdrawOnCore(amount)` is a HyperEVM transaction that asks HyperCore to move the money. The transaction
// succeeds whatever HyperCore then does, and HyperCore drops an amount above the balance without a word. On
// 4 Oct 2026 an investor typed 771.56 against a balance of 771.556173: two transactions went through, nothing
// moved, and the page said "Sent to your HyperCore account." both times. So the amount is checked against the
// exact balance before anything is signed, and "arrived" is said only from the balance read back afterwards.
// No browser globals here, so node's test runner loads it (app/tests/withdraw.test.mjs).
import { spot1e8, usd, plain } from "./shared.js";

/** How long the page waits for HyperCore to carry a withdrawal out, and how often it looks. */
export const WITHDRAW_WAIT_MS = 30_000;
export const WITHDRAW_POLL_MS = 2_000;

/** A pool's spot USDC, exactly, in 1e8 units, out of Hyperliquid's `spotClearinghouseState` answer. */
export function usdcTotal1e8(state) {
  const usdc = (state?.balances || []).find((b) => b.coin === "USDC");
  return usdc ? spot1e8(usdc.total) : 0n;
}

/** The whole balance the way the field takes it: every decimal the pool holds, nothing rounded up. */
export function allOf(balance1e8) {
  return plain(balance1e8, 8);
}

/**
 * The amount typed into the field, in 1e8 units, or a refusal in words before the wallet is asked. More than the
 * pool holds is refused here, because HyperCore would not refuse it: it would do nothing.
 */
export function withdrawPlan(text, balance1e8) {
  const t = String(text ?? "").trim();
  if (!/^\d+(\.\d{1,8})?$/.test(t)) throw new Error("Type the amount in USDC, with at most eight decimals.");
  const amount = spot1e8(t);
  const balance = BigInt(balance1e8);
  if (amount === 0n) throw new Error("Type an amount above zero.");
  if (amount > balance) {
    // Every decimal of both: the two can differ in the eighth place, where a rounded figure would show them equal.
    throw new Error(`The pool holds ${plain(balance, 8)} USDC on HyperCore and you asked for ${plain(amount, 8)}. `
      + "HyperCore drops a transfer above the balance without a word, so nothing was sent. Use All for the whole "
      + "balance.");
  }
  return amount;
}

/**
 * What happened, from the pool's balance before and after: `ok` only when the balance fell by what was asked.
 * A balance that did not move is said as that, with the transaction having gone through all the same.
 */
export function withdrawVerdict({ before, after, amount }) {
  const b = BigInt(before);
  const a = BigInt(after);
  const fell = b > a ? b - a : 0n;
  if (fell >= BigInt(amount)) {
    return { ok: true, text: `Arrived: the pool's HyperCore balance went from ${usd(b, 8)} to ${usd(a, 8)} USDC.` };
  }
  if (fell === 0n) {
    return { ok: false, text: "The transaction went through, but HyperCore has not moved the money: the pool still "
      + `holds ${usd(a, 8)} USDC. Reload in a minute; if it is still there, ask again.` };
  }
  return { ok: false, text: `The pool's HyperCore balance fell by ${usd(fell, 8)} USDC, not the ${usd(amount, 8)} `
    + `asked for: it holds ${usd(a, 8)} now. Reload to see where it settles.` };
}
