// An investor's withdrawal of a pool's capital on HyperCore: what may be asked for, and what can be said after.
//
// `withdrawOnCore(amount)` is a HyperEVM transaction that asks HyperCore to move the money. The transaction
// succeeds whatever HyperCore then does, and HyperCore drops an amount above the balance without a word. On
// 4 Oct 2026 an investor typed 771.56 against a balance of 771.556173: two transactions went through, nothing
// moved, and the page said "Sent to your HyperCore account." both times.
//
// So two things hold here. The amount is checked against the exact balance before anything is signed. And
// "arrived" is said from one witness only: the exchange's own ledger for the pool's account showing a transfer of
// that amount to the owner, made after the click. The pool's balance is not that witness. A challenge bought in the
// same seconds takes its capital out of the same balance, and a fall as large as the withdrawal would pass for it.
// Where the ledger shows no such transfer, the page says what it read and names no cause.
//
// "After the click" is measured on the chain's clock, the one HyperCore stamps its ledger by, and never on the
// browser's. A browser clock a minute ahead would put the transfer before the click, and the page would say it saw
// no transfer of a withdrawal that arrived.
// No browser globals here, so node's test runner loads it (app/tests/withdraw.test.mjs).
import { spot1e8, usd, plain } from "./shared.js";

/** How long the page waits for HyperCore to carry a withdrawal out, and how often it looks. */
export const WITHDRAW_WAIT_MS = 30_000;
export const WITHDRAW_POLL_MS = 2_000;
/** A block's time is in whole seconds and a ledger entry's in milliseconds: one second covers the rounding. */
export const BLOCK_SECOND_MS = 1_000;

/**
 * Where the ledger is cut between "before this withdrawal" and "after it", in ms: the time of the chain's latest
 * block, read before anything is sent, less the rounding. HyperCore carries a transfer out in the block that asked
 * for it or a later one, and stamps it by the same clock: on the transfers this repository has on record the entry
 * is 0.37 to 0.55 s after its block's time. The same cut goes to the ledger's query and to `transferOf`.
 */
export function ledgerCutoff(blockSeconds) {
  const seconds = Number(blockSeconds);
  if (!Number.isFinite(seconds) || seconds <= 0) throw new Error("The chain's clock could not be read.");
  return seconds * 1000 - BLOCK_SECOND_MS;
}

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

const same = (a, b) => String(a ?? "").toLowerCase() === String(b ?? "").toLowerCase() && Boolean(a);

/** The hashes of a ledger's entries: what was already there before the click, so it is not taken for the answer. */
export function hashesOf(ledger) {
  return new Set((ledger || []).map((e) => String(e.hash).toLowerCase()));
}

/**
 * The ledger entry that is this withdrawal, or null: a spot transfer of USDC, from the pool, to the owner, of
 * exactly the amount, not among the entries read before the click (`known`) and not older than `since`, the cut
 * `ledgerCutoff` gives. `ledger` is Hyperliquid's `userNonFundingLedgerUpdates` for the pool's account.
 */
export function transferOf(ledger, { pool, owner, amount, known, since }) {
  for (const entry of ledger || []) {
    const d = entry?.delta;
    if (!d || d.type !== "spotTransfer" || d.token !== "USDC") continue;
    if (!same(d.user, pool) || !same(d.destination, owner)) continue;
    if (known.has(String(entry.hash).toLowerCase())) continue;
    if (Number(entry.time) < Number(since)) continue;
    let sent;
    try {
      sent = spot1e8(d.amount);
    } catch {
      continue;
    }
    if (sent === BigInt(amount)) return entry;
  }
  return null;
}

/** Said when the transaction is in a block and the ledger could not be read after it. */
export const UNREAD = "The withdrawal's transaction is in a block, but HyperCore's record of the pool's transfers "
  + "could not be read afterwards, so the page cannot say whether the money moved. Don't ask again yet: reload and "
  + "look at the balance first.";

/**
 * The outcome, from what was read after the transaction. `ledgers` holds every reading of the pool's ledger in
 * order, `null` for one that failed; `reads` the readings of its balance, the same way.
 *
 * "Arrived" needs the transfer itself in a ledger. With ledgers read to the end and no such transfer, the page says
 * that, with the balance before and now if it has one -- and does not say why the balance is what it is: something
 * else may have moved it. With no ledger read at all, or the last one failed, it is UNREAD.
 */
export function withdrawOutcome({ before, amount, pool, owner, known, since, ledgers, reads = [] }) {
  const balance = [...reads].reverse().find((r) => r !== null && r !== undefined);
  const after = balance === undefined ? undefined : BigInt(balance);
  for (const ledger of ledgers) {
    if (ledger === null) continue;
    const entry = transferOf(ledger, { pool, owner, amount, known, since });
    if (entry) {
      return { ok: true, after, hash: entry.hash,
        text: `Arrived: HyperCore's record shows ${plain(amount, 8)} USDC sent from the pool to your account.` };
    }
  }
  const last = ledgers.length ? ledgers[ledgers.length - 1] : null;
  if (last === null) return { ok: false, unread: true, after, text: UNREAD };
  const seen = after === undefined ? "Its balance could not be read."
    : `Its balance read ${usd(before, 8)} USDC before and ${usd(after, 8)} now.`;
  return { ok: false, after, text: `The transaction is in a block, but HyperCore's record shows no transfer of `
    + `${plain(amount, 8)} USDC from the pool to you after it. ${seen} Reload in a minute before asking again.` };
}
