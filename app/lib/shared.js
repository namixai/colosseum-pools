// The shared pool: its address, the ABI this app reads it with, and the arithmetic of a share.
// No browser globals here, so node's test runner loads it (app/tests/shared.test.mjs).
//
// Testnet only, and not part of the core: SharedPool (src/shared/SharedPool.sol) is a layer over
// Pool, PoolFactory, ChallengeAccount and KeyRegistry, which it uses unchanged. docs/SHARED-POOL.md
// has the design and the testnet run.

import { liveDeployment, isArchive } from "./deployments.js";

/** The pool `#/shared` opens when the link names no address: the live deployment's
 *  (app/config.js, deployments/testnet-shared-demo2.json). The three pools of the earlier rounds are
 *  still there and open by address, the way docs/EVIDENCE-SHARED-POOL.md lists them. */
export const SHARED_POOL = liveDeployment().sharedPool;

export const SHARED_ABI = [
  "function value() view returns (uint256)",
  "function totalShares() view returns (uint256)",
  "function seedShares() view returns (uint256)",
  "function queuedShares() view returns (uint256)",
  "function minDeposit() view returns (uint64)",
  "function lock() view returns (uint32)",
  "function feeBps() view returns (uint16)",
  "function platform() view returns (address)",
  "function factory() view returns (address)",
  "function seatsSealed() view returns (bool)",
  "function blocker() view returns (uint8 reason, address account)",
  "function seats() view returns (address[])",
  "function fundedTerm(address seat) view returns (uint32)",
  "function points() view returns (uint256)",
  "function lastPoint() view returns (uint64)",
  "function openTicketCount() view returns (uint256)",
  "function openTickets(uint256 from, uint256 count) view returns (address[])",
  "function sharesOf(address holder) view returns (uint256)",
  "function queuedOf(address holder) view returns (uint256)",
  "function basis(address holder) view returns (uint256)",
  "function lastDeposit(address holder) view returns (uint64)",
  "function ticketCount(address depositor) view returns (uint256)",
  "function ticketAddress(address depositor, uint256 n) view returns (address)",
  "function tickets(address ticket) view returns (address depositor, uint8 state)",
  "function payments(address holder) view returns (uint64 at, uint96 evm, uint96 core)",
  "function openTicket() returns (address)",
  "event TicketOpened(address indexed depositor, address indexed ticket, uint256 index)",
  "function requestRedeem(uint256 shares)",
  "function settle(address[] recognize)",
  "error NotStarted()",
  "error SeatsNotSealed()",
  "error TooMany()",
  "error NotQuiet(uint8 reason, address account)",
  "error NoValue()",
  "error NothingRequested()",
  "error NotFree(uint256 free)",
  "error Locked(uint64 until)",
];

/** A settlement point names at most this many tickets (SharedPool.MAX_PER_POINT). */
export const MAX_PER_POINT = 8;
/** What HyperCore charges the sender for creating a new address's account: every ticket is new. */
export const NEW_ACCOUNT_FEE = 100_000_000n;
export const TICKET_STATE = ["None", "Open", "Closed"];

/**
 * Whether this page takes a deposit into the pool, and if not, why, in words. Three things close it:
 *   - the pool's seats come from a factory the gateway and the keepers don't serve (the archive's, or
 *     one of the pool's own): money put there would sit in seats nobody can buy;
 *   - the pool hasn't started: there is no share to price a deposit by;
 *   - the operator hasn't sealed the book of seats: the contract itself refuses a ticket until then.
 * `sealed` is null for a pool deployed before the seal existed, which takes deposits as it always did.
 * Withdrawals are never closed by any of this.
 */
export function depositState({ deployment, started, sealed }) {
  if (!deployment) {
    return { open: false, note: "This pool's seats come from a factory of its own, which the gateway and the "
      + "keepers don't serve. Its record can be read here; the page takes no deposit into it." };
  }
  if (isArchive(deployment)) {
    return { open: false, note: "This pool's seats belong to the first deployment, kept as an archive: nobody can "
      + "buy a challenge on them, so the page takes no deposit into it. Its record can still be read and checked, "
      + "and a holder can still ask to withdraw." };
  }
  if (!started) return { open: false, note: "The pool hasn't started yet: the platform's starting shares come first." };
  if (sealed === false) {
    return { open: false, note: "The operator hasn't sealed the book of seats yet. Until then the contract opens "
      + "no deposit ticket; after it, nobody can add a seat." };
  }
  return { open: true, note: "" };
}

/** What the page says about the book of seats, by `seatsSealed()`; "" for a pool older than the seal. */
export function sealLine(sealed) {
  if (sealed === null) return "";
  return sealed ? "Sealed: nobody can add a seat any more."
    : "Open: the operator can still add seats, and the pool takes no deposit until the book is sealed.";
}

/** Why a settlement point can't run now, in words, by `blocker()`'s reason. */
export const BLOCKER = [
  "Nothing is holding it up.",
  "A seat or a challenge breaks one of its rules right now. Someone has to call breach on it first.",
  "A seat is closing its funded stage.",
  "A stopped challenge still has open positions, so its loss can still move.",
  "A payout to a trader has been sent and hasn't landed yet.",
  "The pool paid someone on HyperCore less than five minutes ago and gives the payment time to land.",
];

export function blockerText(reason) {
  const n = Number(reason);
  return BLOCKER[n] || `Unknown reason ${n}.`;
}

/** A fixed-point bigint as a decimal string: `amount(1850000000n, 8)` is "18.50". */
export function amount(value, decimals, shown = 2) {
  const v = BigInt(value);
  const neg = v < 0n;
  const abs = neg ? -v : v;
  const base = 10n ** BigInt(decimals);
  const whole = (abs / base).toLocaleString("en-US");
  const frac = (abs % base).toString().padStart(decimals, "0").slice(0, shown);
  return `${neg ? "-" : ""}${whole}${shown ? `.${frac}` : ""}`;
}

/** USDC as this page shows it: to the millionth, the smallest amount a payment carries, with
 *  trailing zeros dropped down to cents. `usd(92857200n, 8)` is "0.928572", `usd(2e9, 8)` "20.00". */
export function usd(value, decimals) {
  return amount(value, decimals, 6).replace(/(\.\d\d\d*?)0+$/, "$1");
}

/** The same number the way an input field takes it back: no thousands separators, no padding. */
export function plain(value, decimals) {
  const v = BigInt(value);
  const base = 10n ** BigInt(decimals);
  const frac = (v % base).toString().padStart(decimals, "0").replace(/0+$/, "");
  return `${v / base}${frac ? `.${frac}` : ""}`;
}

/** HyperCore spot units (1e8 = 1 USDC) out of the decimal string Hyperliquid's API answers with,
 *  exactly: "20.0" is 2000000000n. More than eight decimals is refused rather than rounded. */
export function spot1e8(text) {
  const t = String(text ?? "").trim();
  if (!/^\d+(\.\d{1,8})?$/.test(t)) throw new Error(`not a spot USDC amount: ${text}`);
  const [whole, frac = ""] = t.split(".");
  return BigInt(whole) * 100_000_000n + BigInt(frac.padEnd(8, "0"));
}

/** Shares are counted in the pool's own units, 1e8 of them to a USDC at the start. */
export function shares(value, shown = 2) {
  return amount(value, 8, shown);
}

/** What `held` shares are worth at the pool's value now (1e8 = 1 USDC), rounded down. */
export function worth(held, value, totalShares) {
  const t = BigInt(totalShares);
  return t === 0n ? 0n : (BigInt(held) * BigInt(value)) / t;
}

/** The price of 1e8 shares in USDC, as a decimal string with six decimals. */
export function price(value, totalShares) {
  const t = BigInt(totalShares);
  if (t === 0n) return "—";
  return amount((BigInt(value) * 1_000_000n) / t, 6, 6);
}

/**
 * A deposit typed into the form, checked the way the pool will check it: at least `minDeposit`, at
 * most eight decimals. `cost` is what leaves the depositor's HyperCore account: the deposit and the
 * 1 USDC HyperCore charges for creating the ticket's account.
 */
export function depositPlan(text, minDeposit) {
  const deposit = spot1e8(text);
  const min = BigInt(minDeposit);
  if (deposit < min) throw new Error(`The smallest deposit this pool takes is ${usd(min, 8)} USDC.`);
  return { deposit, cost: deposit + NEW_ACCOUNT_FEE };
}

/** The open tickets a settlement point should name: those holding at least `minDeposit`, in the
 *  order given, at most MAX_PER_POINT. A ticket below the minimum waits; naming it does nothing. */
export function ticketsToName(open, minDeposit) {
  const min = BigInt(minDeposit);
  return open
    .filter((t) => BigInt(t.spot) >= min)
    .slice(0, MAX_PER_POINT)
    .map((t) => t.address);
}

/**
 * The ticket an `openTicket` transaction opened, from its receipt's logs as the pool's interface
 * parsed them. Not the ticket count read before sending: two pages open on the same wallet can read
 * the same count, and the second would then send its deposit to the first one's ticket.
 */
export function openedTicket(parsedLogs, depositor) {
  const who = String(depositor).toLowerCase();
  const log = parsedLogs.find((l) => l && l.name === "TicketOpened" && String(l.args.depositor).toLowerCase() === who);
  if (!log) throw new Error("The pool's answer names no ticket opened for this wallet; nothing was sent.");
  return log.args.ticket;
}

/** When a holder may ask to withdraw: their latest deposit plus the pool's lock (seconds). */
export function lockedUntil(lastDeposit, lock) {
  return Number(lastDeposit) + Number(lock);
}

/** What the pool has paid a holder so far, each part where it was paid. */
export function paymentsLine({ evm, core }) {
  const e = BigInt(evm);
  const c = BigInt(core);
  const total = e * 100n + c; // HyperEVM USDC has 6 decimals, HyperCore spot 8
  return `${usd(total, 8)} USDC: ${usd(e, 6)} on HyperEVM, in your wallet, and ${usd(c, 8)} `
    + "on HyperCore, in your spot balance";
}

/**
 * "Paid to you so far" from what `payments(holder)` answered, or null for a pool older than that
 * record. The answer is read by position: ethers hands it back as an array, and on an array `at`
 * is Array.prototype.at, so `paid.at` would be a function, not the time of the last payment.
 */
export function paidSummary(paid, when) {
  if (paid === null) {
    return "This pool was deployed before the contract kept a record of payments. Your wallet and your "
      + "HyperCore spot balance show what arrived.";
  }
  const [at, evm, core] = paid;
  if (Number(at) === 0) return "Nothing yet.";
  return `${paymentsLine({ evm, core })}. The latest payment: ${when(at)}.`;
}

/** A revert of the pool's, said so the person knows what to do; "" when it isn't one of these. */
export function explain(err) {
  const name = err?.revert?.name;
  const args = err?.revert?.args || [];
  if (name === "Locked") {
    return `Your shares are locked until ${new Date(Number(args[0]) * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC, `
      + "a fixed time after your latest deposit.";
  }
  if (name === "NotFree") return `You can ask for at most ${shares(args[0])} shares now.`;
  if (name === "NotQuiet") return `A settlement point can't run now. ${blockerText(args[0])}`;
  if (name === "NothingRequested") return "Ask for more than zero shares.";
  if (name === "TooMany") return "Too many at once. A point takes at most eight tickets, and at most 16 people can wait to be paid.";
  if (name === "NotStarted") return "The pool hasn't started yet.";
  if (name === "SeatsNotSealed") return "The operator hasn't sealed the book of seats yet, so the pool opens no deposit ticket.";
  if (name === "NoValue") return "The pool holds nothing to price a share by.";
  return "";
}
