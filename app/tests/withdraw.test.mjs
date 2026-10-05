// node --test app/tests/withdraw.test.mjs
//
// An investor's withdrawal of a pool's capital on HyperCore (app/lib/withdraw.js, app/views/pool.js). The call is
// a HyperEVM transaction that asks HyperCore to move the money, and it succeeds whatever HyperCore then does. On
// 4 Oct 2026 an investor asked for 771.56 against 771.556173: two transactions went through, nothing moved, and
// the page said "Sent" twice. The numbers below are that day's.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  usdcTotal1e8, allOf, withdrawPlan, withdrawVerdict, withdrawOutcome, arrivedBy, UNREAD, WITHDRAW_WAIT_MS,
  WITHDRAW_POLL_MS,
} from "../lib/withdraw.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const HELD = 77_155_617_300n; // 771.556173 USDC

test("the pool's balance is read exactly, and All fills in every decimal of it", () => {
  assert.equal(usdcTotal1e8({ balances: [{ coin: "HYPE", total: "1.0" }, { coin: "USDC", total: "771.556173" }] }), HELD);
  assert.equal(usdcTotal1e8({ balances: [] }), 0n);
  assert.equal(usdcTotal1e8(undefined), 0n);
  // Not 771.56: the field gets what the pool holds, and nothing is rounded up.
  assert.equal(allOf(HELD), "771.556173");
  assert.equal(allOf(617_300n), "0.006173");
  assert.equal(allOf(0n), "0");
  assert.equal(withdrawPlan(allOf(HELD), HELD), HELD);
});

test("an amount above the balance is refused before the wallet is asked, in words that say why", () => {
  // The amount of 4 October: 0.003827 above what the pool held.
  assert.throws(() => withdrawPlan("771.56", HELD), (err) => {
    assert.match(err.message, /^The pool holds 771\.556173 USDC on HyperCore and you asked for 771\.56\./);
    assert.match(err.message, /HyperCore drops a transfer above the balance without a word, so nothing was sent\./);
    assert.match(err.message, /Use All for the whole balance\.$/);
    // The page prints at most 300 characters of an error.
    assert.ok(err.message.length <= 300, String(err.message.length));
    return true;
  });
  // One unit above is above, and the refusal shows the digit that differs.
  assert.throws(() => withdrawPlan("771.55617301", HELD), /holds 771\.556173 USDC on HyperCore and you asked for 771\.55617301\./);
  // At the balance and under it, the amount is the number typed, exactly.
  assert.equal(withdrawPlan("771.556173", HELD), HELD);
  assert.equal(withdrawPlan("771.55", HELD), 77_155_000_000n);
  assert.equal(withdrawPlan(" 0.00000001 ", HELD), 1n);
  // Nothing, zero, and anything that is not a plain decimal of at most eight places.
  assert.throws(() => withdrawPlan("0", HELD), /above zero/);
  assert.throws(() => withdrawPlan("0.00000000", HELD), /above zero/);
  for (const bad of ["", "abc", "1e3", "-5", "1,5", "771.556173123", ".5"]) {
    assert.throws(() => withdrawPlan(bad, HELD), /at most eight decimals/, bad);
  }
});

test("arrived is said only from the balance read back, never from the transaction", () => {
  // What the third attempt of 4 October did: 771.55 left, 0.006173 stayed.
  const arrived = withdrawVerdict({ before: HELD, after: 617_300n, amount: 77_155_000_000n });
  assert.deepEqual(arrived, { ok: true,
    text: "Arrived: the pool's HyperCore balance went from 771.556173 to 0.006173 USDC." });
  // What the first two did: the transaction went through and the balance did not move.
  const dropped = withdrawVerdict({ before: HELD, after: HELD, amount: 77_156_000_000n });
  assert.equal(dropped.ok, false);
  assert.match(dropped.text, /^The transaction went through, but HyperCore has not moved the money: the pool still holds 771\.556173 USDC\./);
  assert.doesNotMatch(dropped.text, /Sent|Arrived/);
  // Money that came in meanwhile is not a withdrawal.
  assert.equal(withdrawVerdict({ before: HELD, after: HELD + 1n, amount: 1n }).ok, false);
  // Less than asked is said as less.
  const part = withdrawVerdict({ before: 10_000_000_000n, after: 6_000_000_000n, amount: 5_000_000_000n });
  assert.equal(part.ok, false);
  assert.match(part.text, /fell by 40\.00 USDC, not the 50\.00 asked for: it holds 60\.00 now/);
  for (const v of [arrived, dropped, part]) assert.ok(v.text.length <= 300, v.text);
});

test("a reading that fails after the transaction is said as unread, never as nothing sent and never as not moved", () => {
  const amount = 77_155_000_000n;
  const left = 617_300n;
  // Every reading failed: the transaction is in a block and the page knows nothing more.
  const blind = withdrawOutcome({ before: HELD, amount, reads: [null, null, null] });
  assert.deepEqual(blind, { ok: false, unread: true, text: UNREAD });
  assert.match(UNREAD, /^The withdrawal's transaction is in a block, but the pool's balance could not be read afterwards/);
  assert.match(UNREAD, /Don't ask again yet/);
  assert.doesNotMatch(UNREAD, /has not moved|Arrived|Sent/);
  assert.ok(UNREAD.length <= 300, String(UNREAD.length));
  assert.equal(blind.after, undefined);
  // No reading at all is the same.
  assert.equal(withdrawOutcome({ before: HELD, amount, reads: [] }).unread, true);
  // An earlier "not moved" followed by a failed reading is not news: the last word has to be a fresh one.
  const stale = withdrawOutcome({ before: HELD, amount, reads: [HELD, HELD, null] });
  assert.equal(stale.unread, true);
  assert.equal(stale.text, UNREAD);
  // A reading that shows the money gone settles it, whatever failed before or after.
  for (const reads of [[left], [null, left], [HELD, null, left], [left, null]]) {
    const done = withdrawOutcome({ before: HELD, amount, reads });
    assert.equal(done.ok, true, JSON.stringify(reads, (k, v) => (typeof v === "bigint" ? String(v) : v)));
    assert.equal(done.after, left);
    assert.match(done.text, /^Arrived: the pool's HyperCore balance went from 771\.556173 to 0\.006173 USDC\./);
  }
  // Fresh readings to the end and no change: that, and only that, is "not moved".
  const dropped = withdrawOutcome({ before: HELD, amount: 77_156_000_000n, reads: [null, HELD, HELD] });
  assert.equal(dropped.ok, false);
  assert.equal(dropped.unread, undefined);
  assert.equal(dropped.after, HELD);
  assert.match(dropped.text, /HyperCore has not moved the money: the pool still holds 771\.556173 USDC/);
});

test("a challenge bought while a withdrawal is on its way is not taken for the withdrawal", () => {
  // A pool holding 771.556173, a withdrawal of 500 asked for, and a challenge sold in the next block: 71 USDC leave
  // for the challenge's account before HyperCore carries the withdrawal out.
  const amount = 50_000_000_000n;
  const bought = HELD - 7_100_000_000n;
  const both = bought - amount;
  // The page waits for a fall of at least the amount, so the purchase alone does not end the waiting.
  assert.equal(arrivedBy(HELD, bought, amount), false);
  assert.equal(arrivedBy(HELD, both, amount), true);
  assert.equal(arrivedBy(HELD, HELD - amount, amount), true, "exactly the amount is the amount");
  assert.equal(arrivedBy(HELD, HELD - amount + 1n, amount), false);
  assert.equal(arrivedBy(HELD, HELD, amount), false);
  assert.equal(arrivedBy(HELD, HELD + 1n, amount), false, "money that came in is not a withdrawal");
  assert.equal(arrivedBy(HELD, null, amount), false);
  assert.equal(arrivedBy(HELD, undefined, amount), false);
  // The withdrawal then lands: the outcome is "arrived", read from the reading that shows it.
  const landed = withdrawOutcome({ before: HELD, amount, reads: [bought, bought, both] });
  assert.equal(landed.ok, true);
  assert.equal(landed.after, both);
  // The withdrawal was dropped and only the purchase moved the balance: after the whole wait the page says the
  // balance fell by less than was asked, which is what happened, and does not say "arrived".
  const dropped = withdrawOutcome({ before: HELD, amount, reads: [bought, bought, bought] });
  assert.equal(dropped.ok, false);
  assert.equal(dropped.after, bought);
  assert.match(dropped.text, /fell by 71\.00 USDC, not the 500\.00 asked for: it holds 700\.556173 now/);
  assert.doesNotMatch(dropped.text, /Arrived/);
  // The loop on the page ends on that same test and on no smaller fall.
  const pool = text("../views/pool.js");
  assert.match(pool, /reads\.push\(now\);[\s\S]{0,260}if \(arrivedBy\(before, now, amount\)\) break;/);
  assert.doesNotMatch(pool, /now < before\) break/);
});

test("the investor's panel shows the balance, refuses above it, and reports from the balance afterwards", () => {
  const pool = text("../views/pool.js");
  // The exact balance stands by the field, with a button that fills it in.
  assert.match(pool, /The pool holds <span id="wd-balance">\$\{esc\(usd\(poolSpot, 8\)\)\}<\/span> USDC on HyperCore\./);
  assert.match(pool, /<button id="wd-all" class="secondary" type="button">All<\/button>/);
  assert.match(pool, /\$\("#wd", page\)\.value = allOf\(now\);/);
  // The click reads the balance again, checks the amount against it, and only then asks the wallet.
  assert.match(pool, /const before = await readPoolSpot\(address\);\s+\$\("#wd-balance", page\)\.textContent = usd\(before, 8\);\s+const amount = withdrawPlan\(\$\("#wd", page\)\.value, before\);\s+await chain\.write\("pool", address, "withdrawOnCore", \[amount\]\);/);
  // After the transaction it waits for the balance to fall. A reading that fails is kept as a failed reading, not
  // thrown: the transaction is in a block by then, and a bare error would read as if nothing had been sent.
  assert.match(pool, /for \(let waited = 0; waited < WITHDRAW_WAIT_MS; waited \+= WITHDRAW_POLL_MS\)/);
  assert.match(pool, /const now = await readPoolSpot\(address\)\.catch\(\(\) => null\);\s+reads\.push\(now\);/);
  // What it says comes from all the readings together, and the balance on the page only from a reading that came back.
  assert.match(pool, /const outcome = withdrawOutcome\(\{ before, amount, reads \}\);\s+if \(outcome\.after !== undefined\) \$\("#wd-balance", page\)\.textContent = usd\(outcome\.after, 8\);\s+if \(!outcome\.ok\) throw new Error\(outcome\.text\);\s+return outcome\.text;/);
  assert.doesNotMatch(pool, /Sent to your HyperCore account/);
  assert.match(pool, /return usdcTotal1e8\(await hl\.spot\(address\)\);/);
  // Long enough for HyperCore, short enough to answer while the investor is still there.
  assert.ok(WITHDRAW_WAIT_MS >= 20_000 && WITHDRAW_WAIT_MS <= 60_000);
  assert.ok(WITHDRAW_POLL_MS >= 1_000 && WITHDRAW_POLL_MS < WITHDRAW_WAIT_MS);
});
