// node --test app/tests/withdraw.test.mjs
//
// An investor's withdrawal of a pool's capital on HyperCore (app/lib/withdraw.js, app/views/pool.js). The call is
// a HyperEVM transaction that asks HyperCore to move the money, and it succeeds whatever HyperCore then does. On
// 4 Oct 2026 an investor asked for 771.56 against 771.556173: two transactions went through, nothing moved, and
// the page said "Sent" twice. The numbers and the ledger entry below are that day's.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  usdcTotal1e8, allOf, withdrawPlan, hashesOf, transferOf, withdrawOutcome, ledgerCutoff, UNREAD, WITHDRAW_WAIT_MS,
  WITHDRAW_POLL_MS, BLOCK_SECOND_MS,
} from "../lib/withdraw.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const HELD = 77_155_617_300n; // 771.556173 USDC
const POOL = "0x2839D3C872CE82151a16aFE0315756915D8A9b79";
const OWNER = "0x21538eBF6598e5866BA496A954dE8E39097bFB59";
// What Hyperliquid's userNonFundingLedgerUpdates answered for that pool: the third attempt, the one that moved.
const ENTRY = { time: 1791102688365, hash: "0xc23d29d321cb954ec3b6042adf6e4400007841b8bcceb4206605d525e0cf6f39",
  delta: { type: "spotTransfer", token: "USDC", amount: "771.55", usdcValue: "771.55", user: POOL.toLowerCase(),
    destination: OWNER.toLowerCase(), fee: "0.0", nativeTokenFee: "0.0", nonce: 559966, feeToken: "" } };
const AMOUNT = 77_155_000_000n;
const CLICK = ENTRY.time - 4_000;
const who = (over = {}) => ({ pool: POOL, owner: OWNER, amount: AMOUNT, known: new Set(), since: CLICK, ...over });

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
  assert.equal(withdrawPlan("771.55", HELD), AMOUNT);
  assert.equal(withdrawPlan(" 0.00000001 ", HELD), 1n);
  // Nothing, zero, and anything that is not a plain decimal of at most eight places.
  assert.throws(() => withdrawPlan("0", HELD), /above zero/);
  assert.throws(() => withdrawPlan("0.00000000", HELD), /above zero/);
  for (const bad of ["", "abc", "1e3", "-5", "1,5", "771.556173123", ".5"]) {
    assert.throws(() => withdrawPlan(bad, HELD), /at most eight decimals/, bad);
  }
});

test("the withdrawal is one ledger entry: USDC, from the pool, to the owner, of the amount, made after the click", () => {
  assert.equal(transferOf([ENTRY], who()), ENTRY);
  // The addresses as a wallet or a link spells them: case does not decide.
  assert.equal(transferOf([ENTRY], who({ pool: POOL.toLowerCase(), owner: OWNER.toUpperCase().replace("0X", "0x") })), ENTRY);
  // Each thing that makes it another transfer.
  assert.equal(transferOf([ENTRY], who({ amount: 77_156_000_000n })), null, "another amount");
  assert.equal(transferOf([ENTRY], who({ amount: AMOUNT + 1n })), null, "one unit off");
  // A larger transfer to the owner is not this one either: an earlier, bigger withdrawal would pass for a smaller.
  assert.equal(transferOf([ENTRY], who({ amount: AMOUNT - 1n })), null, "one unit under");
  assert.equal(transferOf([ENTRY], who({ amount: 100_000_000n })), null, "a much smaller amount");
  assert.equal(transferOf([ENTRY], who({ owner: "0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4" })), null, "to someone else");
  assert.equal(transferOf([ENTRY], who({ pool: OWNER })), null, "from another account");
  assert.equal(transferOf([{ ...ENTRY, delta: { ...ENTRY.delta, token: "HYPE" } }], who()), null, "another token");
  assert.equal(transferOf([{ ...ENTRY, delta: { ...ENTRY.delta, type: "deposit" } }], who()), null, "not a transfer");
  // A challenge's capital leaving the pool in the same seconds is a transfer too, to the challenge's account.
  const bought = { ...ENTRY, hash: "0x" + "ab".repeat(32),
    delta: { ...ENTRY.delta, amount: "771.55", destination: "0xa571193573d068414a524dcb3d134f685f1e022f" } };
  assert.equal(transferOf([bought], who()), null, "the same amount to another account is not this withdrawal");
  assert.equal(transferOf([bought, ENTRY], who()), ENTRY);
  // What was in the ledger before the click is not the answer, however well it fits: an identical withdrawal made
  // a minute earlier would otherwise pass for this one.
  assert.equal(transferOf([ENTRY], who({ known: hashesOf([ENTRY]) })), null);
  assert.equal(hashesOf([ENTRY]).has(ENTRY.hash), true);
  assert.equal(hashesOf(undefined).size, 0);
  // Nor is an entry older than the cut: at the cut it is this withdrawal, a millisecond before it another one.
  assert.equal(transferOf([ENTRY], who({ since: ENTRY.time })), ENTRY);
  assert.equal(transferOf([ENTRY], who({ since: ENTRY.time + 1 })), null);
  // A ledger that is empty, missing, or holds something unreadable answers null and does not throw.
  assert.equal(transferOf([], who()), null);
  assert.equal(transferOf(undefined, who()), null);
  assert.equal(transferOf([{ time: ENTRY.time, hash: "0x1" }, { ...ENTRY, delta: { ...ENTRY.delta, amount: "n/a" } }], who()), null);
});

// Transfers HyperCore carried out for this repository's shared pools on 5 October: the HyperEVM block that asked,
// that block's time in seconds, and the time of the ledger entry in ms.
const ON_RECORD = [
  [66066670, 1791178623, 1791178623431],
  [66066691, 1791178644, 1791178644385],
  [66066711, 1791178664, 1791178664439],
  [66075727, 1791187532, 1791187532401],
];

test("the cut between before and after is on the chain's clock, never the browser's", () => {
  // One second under the block's time, for a block's whole seconds against the ledger's milliseconds.
  assert.equal(BLOCK_SECOND_MS, 1_000);
  assert.equal(ledgerCutoff(1791178623), 1791178622000);
  assert.equal(ledgerCutoff(1791178623n), 1791178622000);
  // Every transfer on record is stamped after its block's time and inside the same second, so a cut taken at a
  // block read before the click keeps the transfer, with nothing borrowed from the browser.
  for (const [block, seconds, entryMs] of ON_RECORD) {
    assert.ok(entryMs >= seconds * 1000 && entryMs - seconds * 1000 < 1_000, `block ${block}`);
    const entry = { ...ENTRY, time: entryMs };
    assert.equal(transferOf([entry], who({ since: ledgerCutoff(seconds) })), entry, `block ${block}`);
    // A block read some seconds before the transaction cuts earlier still, and loses nothing.
    assert.equal(transferOf([entry], who({ since: ledgerCutoff(seconds - 30) })), entry);
    // A transfer of the same amount made before that block's second began is on the other side of the cut.
    assert.equal(transferOf([{ ...entry, time: seconds * 1000 - BLOCK_SECOND_MS - 1 }], who({ since: ledgerCutoff(seconds) })), null);
  }
  // A clock that could not be read is an error before anything is sent, not a cut at zero that lets everything in.
  for (const bad of [undefined, null, NaN, 0, -5, "soon"]) assert.throws(() => ledgerCutoff(bad), /chain's clock could not be read/);
  // The panel asks the chain for the time and never the browser: a browser clock a minute ahead would put the
  // transfer before the click, and the page would report no transfer of a withdrawal that arrived.
  const pool = text("../views/pool.js");
  const click = pool.slice(pool.indexOf('wire($("#wd-btn", page)'), pool.indexOf("const outcome = withdrawOutcome("));
  assert.ok(click.length > 500);
  assert.doesNotMatch(click, /Date\.now|new Date|performance\.now/);
  assert.match(click, /const \[before, since\] = await Promise\.all\(\[readPoolSpot\(address\), chain\.blockTime\(\)\.then\(ledgerCutoff\)\]\);/);
  // The same cut goes to the reading taken before, to every reading after, and to the matcher.
  assert.match(click, /const known = hashesOf\(await hl\.ledger\(address, since - LEDGER_LOOKBACK_MS\)\);/);
  assert.match(click, /hl\.ledger\(address, since\)\.catch\(\(\) => null\)/);
  assert.match(click, /const who = \{ pool: address, owner, amount, known, since \};/);
  assert.match(text("../lib/withdraw.js"), /if \(Number\(entry\.time\) < Number\(since\)\) continue;/);
  // The chain's clock is its latest block's time.
  assert.match(text("../lib/chain.js"), /export async function blockTime\(\) \{\s+const block = await readProvider\.getBlock\("latest"\);\s+if \(!block\) throw new Error\("The chain's latest block could not be read\."\);\s+return block\.timestamp;\s+\}/);
});

test("arrived is said from the ledger alone, and never from the transaction or the balance", () => {
  // The third attempt of 4 October: the transfer is in the ledger.
  const arrived = withdrawOutcome({ before: HELD, ...who(), ledgers: [[], [ENTRY]], reads: [HELD, 617_300n] });
  assert.equal(arrived.ok, true);
  assert.equal(arrived.text, "Arrived: HyperCore's record shows 771.55 USDC sent from the pool to your account.");
  assert.equal(arrived.hash, ENTRY.hash);
  assert.equal(arrived.after, 617_300n);
  // The first two: ledgers read to the end, no transfer in them, the balance where it was.
  const dropped = withdrawOutcome({ before: HELD, ...who({ amount: 77_156_000_000n }), ledgers: [[], [], []], reads: [HELD, HELD, HELD] });
  assert.equal(dropped.ok, false);
  assert.equal(dropped.unread, undefined);
  assert.match(dropped.text, /^The transaction is in a block, but HyperCore's record shows no transfer of 771\.56 USDC from the pool to you after it\./);
  assert.match(dropped.text, /Its balance read 771\.556173 USDC before and 771\.556173 now\./);
  assert.doesNotMatch(dropped.text, /Arrived|Sent/);
  for (const v of [arrived, dropped]) assert.ok(v.text.length <= 300, v.text);
});

test("a fall in the balance as large as the withdrawal is not the withdrawal", () => {
  // A challenge bought in the same seconds takes 771.55 out of the pool for its own account, and the withdrawal is
  // dropped. The balance is down by exactly the amount asked for. Until 5 Oct 2026 the page said "Arrived" here.
  const bought = { ...ENTRY, hash: "0x" + "cd".repeat(32),
    delta: { ...ENTRY.delta, destination: "0xa571193573d068414a524dcb3d134f685f1e022f" } };
  const taken = withdrawOutcome({ before: HELD, ...who(), ledgers: [[bought], [bought]], reads: [617_300n, 617_300n] });
  assert.equal(taken.ok, false);
  assert.doesNotMatch(taken.text, /Arrived/);
  // It says what it read, both balances, and gives no reason for the difference.
  assert.match(taken.text, /shows no transfer of 771\.55 USDC from the pool to you after it\. Its balance read 771\.556173 USDC before and 0\.006173 now\./);
  assert.doesNotMatch(taken.text, /because|dropped|challenge|bought|fell short/i);
  // The other way round: the purchase took less, the balance is down by less than the amount, and the withdrawal did
  // go through later. The transfer in the ledger settles it whatever the balance was doing.
  const late = withdrawOutcome({ before: HELD, ...who({ amount: 50_000_000_000n }),
    ledgers: [[bought], [bought, { ...ENTRY, delta: { ...ENTRY.delta, amount: "500.0" } }]], reads: [HELD - 7_100_000_000n, 20_055_617_300n] });
  assert.equal(late.ok, true);
  assert.match(late.text, /^Arrived: HyperCore's record shows 500 USDC sent/);
});

test("a ledger that could not be read is said as unread, never as nothing sent and never as no transfer", () => {
  // Every reading failed: the transaction is in a block and the page knows nothing more.
  const blind = withdrawOutcome({ before: HELD, ...who(), ledgers: [null, null, null], reads: [null, null, null] });
  assert.equal(blind.ok, false);
  assert.equal(blind.unread, true);
  assert.equal(blind.text, UNREAD);
  assert.equal(blind.after, undefined);
  assert.match(UNREAD, /^The withdrawal's transaction is in a block, but HyperCore's record of the pool's transfers could not be read afterwards/);
  assert.match(UNREAD, /Don't ask again yet/);
  assert.doesNotMatch(UNREAD, /no transfer|Arrived|Sent/);
  assert.ok(UNREAD.length <= 300, String(UNREAD.length));
  // No reading at all is the same.
  assert.equal(withdrawOutcome({ before: HELD, ...who(), ledgers: [] }).unread, true);
  // An earlier "no transfer" followed by a failed reading is not news: the last word has to be a fresh one.
  const stale = withdrawOutcome({ before: HELD, ...who(), ledgers: [[], [], null], reads: [HELD, HELD, null] });
  assert.equal(stale.unread, true);
  assert.equal(stale.after, HELD, "the last balance that did come back is still shown");
  // A ledger that shows the transfer settles it, whatever failed before or after.
  for (const ledgers of [[[ENTRY]], [null, [ENTRY]], [[], null, [ENTRY]], [[ENTRY], null]]) {
    assert.equal(withdrawOutcome({ before: HELD, ...who(), ledgers }).ok, true);
  }
  // The ledger read, the balance not: the page still answers from the ledger and says the balance is unknown.
  const half = withdrawOutcome({ before: HELD, ...who(), ledgers: [[]], reads: [null] });
  assert.equal(half.unread, undefined);
  assert.match(half.text, /shows no transfer of 771\.55 USDC from the pool to you after it\. Its balance could not be read\./);
});

test("the investor's panel refuses above the balance, waits for the transfer, and reports from the ledger", () => {
  const pool = text("../views/pool.js");
  // The exact balance stands by the field, with a button that fills it in.
  assert.match(pool, /The pool holds <span id="wd-balance">\$\{esc\(usd\(poolSpot, 8\)\)\}<\/span> USDC on HyperCore\./);
  assert.match(pool, /<button id="wd-all" class="secondary" type="button"\$\{off\}>All<\/button>/);
  // The click reads the balance and the ledger before anything is sent, checks the amount, and only then asks the wallet.
  assert.match(pool, /const \[before, since\] = await Promise\.all\(\[readPoolSpot\(address\), chain\.blockTime\(\)\.then\(ledgerCutoff\)\]\);\s+const known = hashesOf\(await hl\.ledger\(address, since - LEDGER_LOOKBACK_MS\)\);\s+showBalance\(before\);\s+const amount = withdrawPlan\(\$\("#wd", page\)\.value, before\);\s+await chain\.write\("pool", address, "withdrawOnCore", \[amount\]\);/);
  // The transfer it looks for is the pool's, to the pool's owner as the chain names it, not to whoever is connected.
  assert.match(pool, /const who = \{ pool: address, owner, amount, known, since \};/);
  // After the transaction a reading that fails is kept as a failed reading, not thrown: the transaction is in a
  // block by then, and a bare error would read as if nothing had been sent.
  assert.match(pool, /hl\.ledger\(address, since\)\.catch\(\(\) => null\), readPoolSpot\(address\)\.catch\(\(\) => null\),/);
  // Only the transfer itself ends the waiting; a fall in the balance does not.
  assert.match(pool, /if \(ledger !== null && transferOf\(ledger, who\)\) break;/);
  assert.doesNotMatch(pool, /now < before\) break|arrivedBy/);
  // What it says comes from all the readings together.
  assert.match(pool, /const outcome = withdrawOutcome\(\{ before, \.\.\.who, ledgers, reads \}\);\s+if \(outcome\.after !== undefined\) showBalance\(outcome\.after\);\s+if \(!outcome\.ok\) throw new Error\(outcome\.text\);\s+return outcome\.text;/);
  assert.doesNotMatch(pool, /Sent to your HyperCore account/);
  assert.match(text("../lib/hl.js"), /export const ledger = \(user, startTime\) => info\(\{ type: "userNonFundingLedgerUpdates", user, startTime \}\);/);
  // Long enough for HyperCore, short enough to answer while the investor is still there.
  assert.ok(WITHDRAW_WAIT_MS >= 20_000 && WITHDRAW_WAIT_MS <= 60_000);
  assert.ok(WITHDRAW_POLL_MS >= 1_000 && WITHDRAW_POLL_MS < WITHDRAW_WAIT_MS);
});

test("a balance that cannot be read costs the two buttons that need it, not the page", () => {
  const pool = text("../views/pool.js");
  // At load a failed reading is caught: the router would otherwise draw "Could not load this page" over a pool
  // whose every other part was read.
  assert.match(pool, /const poolSpot = await readPoolSpot\(address\)\.catch\(\(\) => null\);\s+const off = poolSpot === null \? " disabled" : "";/);
  assert.match(pool, /\$\{poolSpot === null\s+\? `<p class="notice">The pool's balance on HyperCore could not be read just now, so withdrawing on HyperCore is\s+off until it can be/);
  // The field and both buttons are off together, and nothing is wired to them.
  assert.match(pool, /<input id="wd" type="number" step="any" min="0" placeholder="USDC"\$\{off\}>/);
  assert.match(pool, /<button id="wd-btn" class="secondary"\$\{off\}>Withdraw on HyperCore<\/button>/);
  assert.match(pool, /if \(poolSpot === null\) return;\s+const showBalance/);
  // The income held on HyperEVM does not depend on that reading and stays wired above it.
  assert.ok(pool.indexOf('wire($("#earned", page)') < pool.indexOf("if (poolSpot === null) return;"));
  // "All" goes through wire like every other button, so a reading that fails there is said next to it.
  assert.match(pool, /wire\(\$\("#wd-all", page\), async \(\) => \{\s+const now = await readPoolSpot\(address\);/);
  assert.doesNotMatch(pool, /\$\("#wd-all", page\)\.addEventListener/);
});
