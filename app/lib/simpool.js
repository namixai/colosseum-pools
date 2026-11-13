// A simulated pool (src/sim/SimPool.sol) and its factory (src/sim/SimPoolFactory.sol), as a page shows them: the
// menu, the exams sold, the stages, every payout by its state and, for a payout that was paid, the five fields that
// let anyone check it without trusting the page. No browser globals and no reads here, so node's test runner loads
// it (app/tests/simpool.test.mjs): the page reads the getters and the events and hands them in; these functions put
// them together and say where the events do not account for what the getters count.
//
// Why both. The getters are exact and cheap: how many payouts, each one's state, how many were paid and how much,
// how many were stopped and lifted. Only the events carry what a reader checks a payout by: the transaction of each
// payment, and the reason, the other account and the trades of each stop. Events are read in windows of blocks
// (readLogs), and a reading that ran out of its budget is not the whole history: every total here says whether the
// events it was summed from add up to the contract's own counters.
//
// What is left out on purpose: what the creator locked when the pool opened (PoolCreated.locked, noteLocked) and
// what the creator has put in since (principal). A page of payouts shows what was paid, not the creator's money.

/** The pool's getters and events a page reads, as the contract declares them (app/tests/simpool.test.mjs holds the
 *  events to the source). An enum is a uint8 in the ABI. */
export const SIM_POOL_ABI = [
  "function factory() view returns (address)",
  "function usdc() view returns (address)",
  "function creator() view returns (address)",
  "function codes() view returns (bytes32[])",
  "function buy(uint256 line, bytes32 code) returns (address exam)",
  "function buyWithAuthorization(address trader, uint256 line, bytes32 code, bytes32 salt, uint256 validAfter, uint256 validBefore, uint8 v, bytes32 r, bytes32 s) returns (address exam)",
  "function codeOf(bytes32 code) view returns (address partner, uint16 bps, bool used, bool listed, uint256 brought)",
  "function brought() view returns (uint256)",
  "function terms() view returns (bytes32)",
  "function largestPayout() view returns (uint64)",
  "function owed() view returns (uint256)",
  "function live() view returns (uint256)",
  "function closed() view returns (bool)",
  "function finished() view returns (bool)",
  "function lockedUntil() view returns (uint64)",
  "function closedEarly() view returns (bool)",
  "function closedAt() view returns (uint64)",
  "function requested() view returns (uint256)",
  "function readyAt() view returns (uint64)",
  "function head() view returns (uint256)",
  "function stoppedCount() view returns (uint256)",
  "function liftedCount() view returns (uint256)",
  "function paidCount() view returns (uint256)",
  "function paidTotal() view returns (uint256)",
  "function held() view returns (uint256)",
  "function heldOf(address holder) view returns (uint256)",
  "function fundWindow() view returns (uint32)",
  "function fromBook() view returns (bool)",
  "function maxWorseBps() view returns (uint16)",
  "function menu() view returns ((uint64 size, uint64 price)[])",
  "function payout(uint256 id) view returns ((address account, address trader, uint64 amount, uint64 dueAt, uint8 state, bool wasStopped))",
  "function payoutCount() view returns (uint256)",
  "function accountOf(address account) view returns (uint8 kind, uint8 line, bool done)",
  "event ExamSold(address indexed exam, address indexed trader, uint256 line, uint64 size, uint64 price, uint256 fee, bool bySignature, bytes32 code, address indexed partner)",
  "event CodeSet(bytes32 indexed code, address indexed partner, uint16 bps)",
  "event PartnerPaid(bytes32 indexed code, address indexed partner, uint256 amount, bool sent)",
  "event Funded(address indexed stage, address indexed exam, address indexed trader)",
  "event Released(address indexed account)",
  "event PayoutDue(uint256 indexed id, address indexed account, address indexed trader, uint64 amount, uint64 dueAt)",
  "event PayoutQueued(uint256 indexed id, uint256 balance)",
  "event PayoutPaid(uint256 indexed id, address indexed trader, uint64 amount, uint256 paidCount, uint256 paidTotal)",
  "event PayoutStopped(uint256 indexed id, address indexed trader, uint64 amount, uint8 reason, address paired, bytes32[] trades, bytes32[] links, uint256 stoppedCount, uint256 liftedCount, uint256 paidCount)",
  "event PayoutHeld(uint256 indexed id, address indexed trader, uint64 amount)",
  "event HeldClaimed(address indexed trader, address indexed to, uint256 amount)",
  "event StopLifted(uint256 indexed id, uint256 stoppedCount, uint256 liftedCount, uint256 paidCount)",
  "event Closed()",
  "event ClosedEarly(address indexed closer)",
  "event Withdrawn(address indexed to, uint256 amount, uint256 fee, uint256 partnerAmount)",
  "event WithdrawRequested(uint256 amount, uint64 readyAt, uint64 lapsesAt)",
  "event WithdrawCancelled(address indexed by, uint256 amount)",
];

/**
 * What a pool with a stop reserve adds (the stop held for STOP_RESERVE, the trader's objection, the stop made final):
 * the payout's `stoppedAt`, the `reserved` view and two events. A pool without it answers none of them, so a page
 * reads these only once `reserved()` has answered (`hasStopReserve`). `payout(id)` in SIM_POOL_ABI decodes the
 * answer of either pool: its six fields lead both, and the seventh, here, is read only where it is there.
 */
export const SIM_POOL_STOP_RESERVE_ABI = [
  "function reserved() view returns (uint256)",
  "function payout(uint256 id) view returns ((address account, address trader, uint64 amount, uint64 dueAt, uint8 state, bool wasStopped, uint64 stoppedAt))",
  "event StopObjected(uint256 indexed id, address indexed trader, bytes32 text)",
  "event StopFinal(uint256 indexed id, uint64 amount, uint256 reserved)",
];

/** Whether a pool has the stop reserve, from its answer to `reserved()`: a value, or a revert (a pool without it).
 *  `read` is that call; a failure that is not a revert (the node, the rate) is thrown, never taken for "without". */
export async function hasStopReserve(read, isRevert) {
  try {
    await read();
    return true;
  } catch (err) {
    if (isRevert(err)) return false;
    throw err;
  }
}

export const SIM_FACTORY_ABI = [
  "function pools() view returns (address[])",
  "function poolOf(address account) view returns (address)",
  "function checker() view returns (address)",
  "function filler() view returns (address)",
  "function feeRecipient() view returns (address)",
  "function termsBook() view returns (address)",
  "function usdc() view returns (address)",
  "event PoolCreated(address indexed pool, address indexed creator, uint256 locked)",
  "event CheckerSet(address indexed checker)",
];

/** The factory's constants, the same for every pool (SimPoolFactory.sol); the test holds them to the source. */
export const FEE_BPS = 50n;
export const PAYOUT_BPS = 800n;
export const PAYOUT_DELAY = 72 * 3600;
/** How long a stop holds its money and can be lifted or objected to, on a pool with the stop reserve. */
export const STOP_RESERVE = 14 * 24 * 3600;
/** How long a pool runs at least: before it the creator neither closes it nor takes back what it put in. */
export const MIN_TERM = 180 * 24 * 3600;
/** How long a creator's request to withdraw waits, and `finish` after the close; how long a ready request stays good. */
export const WITHDRAW_DELAY = 7 * 24 * 3600;
export const WITHDRAW_WINDOW = 3 * 24 * 3600;
const BPS = 10_000n;

/** SimPool.PayoutState, by its number. */
export const PAYOUT_STATE = ["None", "Due", "Stopped", "Paid", "Held", "Refused"];

/** What a page calls each state of a payout. "waiting" and "queued" are both Due on chain: before its time, and
 *  past it but not yet sent (nobody has called `pay`, or the pool's USDC did not cover it: then `short` says so). */
export const STATUS = Object.freeze({
  waiting: "Waiting out the 72 hours",
  queued: "Due, in the queue to be sent",
  paid: "Paid",
  stopped: "Stopped by the checker",
  held: "Held: the token refused the transfer, the money is set aside for the trader",
  refused: "Refused: the stop stood 14 days without a lift and is final",
});

/** Why the checker stopped a payout: SimPool.stopPayout's `reason`. */
export const STOP_REASON = Object.freeze({
  1: "Opposite positions with another account of this platform's pools",
  2: "Opposite positions with another account, and the two wallets linked",
});

export function stopReasonText(reason) {
  return STOP_REASON[Number(reason)] || `Unknown reason ${Number(reason)}`;
}

/** The line a page shows next to "Paid in total": what the sum is and what it is not. */
export const TOTAL_INCLUDES = "The sum of every payment this pool's contract sent to a trader who passed both "
  + "stages: 8% of the size the trader passed on, once per stage, in USDC, each with its own transaction below. "
  + "Not in it: payouts stopped by the checker, payouts still waiting or queued, payouts the token refused (held for "
  + "the trader), the platform's 0.5% of each exam's price, and anything the pool's creator withdrew.";

/**
 * One event as these functions read it: `{ name, args, block, tx, index }`. Takes an ethers v6 EventLog (eventName,
 * args as a Result, blockNumber, transactionHash, index) or that shape already.
 */
export function normalizeEvent(e) {
  const args = e.args && typeof e.args.toObject === "function" ? e.args.toObject() : { ...e.args };
  const block = Number(e.block ?? e.blockNumber);
  // An event without its block cannot be ordered or dated: a reading that lost it is said, not sorted as NaN.
  if (!Number.isInteger(block)) throw new Error(`an event ${e.name ?? e.eventName ?? ""} without its block number`);
  return {
    name: e.name ?? e.eventName ?? e.fragment?.name,
    args,
    block,
    tx: e.tx ?? e.transactionHash,
    index: Number(e.index ?? e.logIndex ?? 0),
  };
}

/** The getters a function needs, all there, or an error that names the missing ones: a total of zero on a page of
 *  payouts would be a number nobody can check, where "could not read paidTotal" is the truth. */
function need(reads, names) {
  const missing = names.filter((n) => reads?.[n] === undefined || reads?.[n] === null);
  if (missing.length) throw new Error(`the pool's reads lack ${missing.join(", ")}`);
}

function ordered(events) {
  return events.map(normalizeEvent).sort((a, b) => a.block - b.block || a.index - b.index);
}

function named(events, name) {
  return events.filter((e) => e.name === name);
}

function at(e, times) {
  return { tx: e.tx, block: e.block, time: times?.get?.(e.block) ?? null };
}

/** The blocks whose time a page needs to read to date every event: one `eth_getBlockByNumber` each. */
export function blocksToDate(events) {
  return [...new Set(events.map((e) => normalizeEvent(e).block))].sort((a, b) => a - b);
}

/** The payout a stage of `size` earns: 8% of it (SimPool._payoutOf). */
export function payoutOf(size) {
  return BigInt(size) * PAYOUT_BPS / BPS;
}

/** The menu, line by line: the size, the price, what passing both stages pays, and the platform's part of the price. */
export function menuLines(menu) {
  return [...menu].map((m, line) => {
    const size = BigInt(m.size ?? m[0]);
    const price = BigInt(m.price ?? m[1]);
    return { line, size, price, payout: payoutOf(size), fee: price * FEE_BPS / BPS };
  });
}

/** Every exam sold, oldest first: who bought it, off which line, at what price, whether by signature, with which code
 *  and so for which partner, the transaction, and what became of it here: the stage it earned, or released. */
export function soldExams(events, times) {
  const all = ordered(events);
  const released = new Set(named(all, "Released").map((e) => String(e.args.account).toLowerCase()));
  const funded = new Map(named(all, "Funded").map((e) => [String(e.args.exam).toLowerCase(), e.args.stage]));
  return named(all, "ExamSold").map((e) => {
    const exam = e.args.exam;
    return {
      exam,
      trader: e.args.trader,
      line: Number(e.args.line),
      size: BigInt(e.args.size),
      price: BigInt(e.args.price),
      fee: BigInt(e.args.fee),
      bySignature: Boolean(e.args.bySignature),
      code: e.args.code ?? null,
      partner: e.args.partner ?? null,
      stage: funded.get(String(exam).toLowerCase()) ?? null,
      released: released.has(String(exam).toLowerCase()),
      ...at(e, times),
    };
  });
}

/** Every stage started, oldest first, with the exam it came from, whether it was released and the payout it wrote. */
export function stages(events, times) {
  const all = ordered(events);
  const released = new Set(named(all, "Released").map((e) => String(e.args.account).toLowerCase()));
  const payoutBy = new Map(named(all, "PayoutDue").map((e) => [String(e.args.account).toLowerCase(), Number(e.args.id)]));
  return named(all, "Funded").map((e) => ({
    stage: e.args.stage,
    exam: e.args.exam,
    trader: e.args.trader,
    released: released.has(String(e.args.stage).toLowerCase()),
    payoutId: payoutBy.get(String(e.args.stage).toLowerCase()) ?? null,
    ...at(e, times),
  }));
}

/**
 * Every payout, by id, from the getters (`payout(id)` for each id below `payoutCount()`) and the events that name it.
 * `status` is one of STATUS's keys, from the getter's state and `now` (seconds, the chain's clock). The events add
 * the transactions: `due` (the claim), `paid`, `stop` (with its reason, the other account and the trades and links
 * the checker named), `lift`, `heldAt`, and `short`, the pool's balance when a payment found it short.
 */
export function payouts(structs, events, now, times) {
  const all = ordered(events);
  const byId = (name) => {
    const m = new Map();
    for (const e of named(all, name)) m.set(Number(e.args.id), e);
    return m;
  };
  const due = byId("PayoutDue");
  const paid = byId("PayoutPaid");
  const stopped = byId("PayoutStopped");
  const lifted = byId("StopLifted");
  const queued = byId("PayoutQueued");
  const heldEv = byId("PayoutHeld");
  const objected = byId("StopObjected");
  const final = byId("StopFinal");
  return [...structs].map((p, id) => {
    const state = Number(p.state ?? p[4]);
    const dueAt = Number(p.dueAt ?? p[3]);
    // Only a pool with the stop reserve has it (SIM_POOL_STOP_RESERVE_ABI); 0 there for a payout never stopped.
    const rawStoppedAt = p.stoppedAt ?? p[6];
    const stoppedAt = rawStoppedAt === undefined ? null : Number(rawStoppedAt);
    let status;
    if (state === 1) status = now < dueAt ? "waiting" : "queued";
    else if (state === 2) status = "stopped";
    else if (state === 3) status = "paid";
    else if (state === 4) status = "held";
    else if (state === 5) status = "refused";
    else status = "unknown";
    const s = stopped.get(id);
    const q = queued.get(id);
    const o = objected.get(id);
    const out = {
      id,
      account: p.account ?? p[0],
      trader: p.trader ?? p[1],
      amount: BigInt(p.amount ?? p[2]),
      dueAt,
      status,
      wasStopped: Boolean(p.wasStopped ?? p[5]),
      due: due.has(id) ? at(due.get(id), times) : null,
      paid: paid.has(id) ? at(paid.get(id), times) : null,
      stop: s ? {
        reason: Number(s.args.reason),
        reasonText: stopReasonText(s.args.reason),
        paired: s.args.paired,
        trades: [...s.args.trades],
        links: [...s.args.links],
        ...at(s, times),
      } : null,
      // The stop reserve: until when a stop can still be lifted or objected to, the trader's objection, the final.
      stoppedAt: stoppedAt || null,
      reserveEnds: stoppedAt ? stoppedAt + STOP_RESERVE : null,
      objection: o ? { text: o.args.text, ...at(o, times) } : null,
      final: final.has(id) ? at(final.get(id), times) : null,
      lift: lifted.has(id) ? at(lifted.get(id), times) : null,
      heldAt: heldEv.has(id) ? at(heldEv.get(id), times) : null,
      short: q && status === "queued" ? { balance: BigInt(q.args.balance), ...at(q, times) } : null,
    };
    out.stopNote = stopNote(out, now);
    return out;
  });
}

/** A time on the chain's clock (seconds) as a page writes it: "2026-11-26 14:05 UTC". */
export function utc(seconds) {
  return `${new Date(Number(seconds) * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/**
 * Where a stopped or refused payout stands, in words, on a pool with the stop reserve: the 14 days in which the
 * trader may object and the checker may lift the stop, then the refusal, final once anyone sends `finalizeStop`.
 * `state` is "window" (the 14 days run), "due-final" (they are over, the stop not yet made final), "final", or
 * "stopped" (a pool without the reserve: no window to speak of). Null for a payout never stopped or since lifted.
 */
export function stopNote(p, now) {
  const objected = p.objection
    ? ` The trader objected on ${p.objection.time == null ? `block ${p.objection.block}` : utc(p.objection.time)}; `
      + "the objection is on chain."
    : "";
  if (p.status === "refused") {
    return { state: "final", until: p.reserveEnds,
      text: `Refused: the stop was not lifted within 14 days, and the refusal is final.${objected}` };
  }
  if (p.status !== "stopped") return null;
  // `== null`: a payout put together without the field (undefined) is a pool without the reserve too (review of #272).
  if (p.reserveEnds == null) return { state: "stopped", until: null, text: "Stopped by the checker." };
  if (now < p.reserveEnds) {
    return { state: "window", until: p.reserveEnds,
      text: `Stopped. Until ${utc(p.reserveEnds)} the trader may object and the checker may lift the stop; the money `
        + `stays in the pool, set aside.${objected}` };
  }
  return { state: "due-final", until: p.reserveEnds,
    text: `Stopped, and the 14 days ended ${utc(p.reserveEnds)} without a lift: the refusal becomes final when anyone `
      + `sends it (finalizeStop).${objected}` };
}

/**
 * "Paid in total": the sum of the PayoutPaid events, each payment with its transaction, set against the contract's
 * own `paidCount()` and `paidTotal()`. `complete` is false when the events read do not add up to them (a reading cut
 * short): the page then shows the contract's total and says the list is partial, never a smaller sum as the total.
 */
export function paidTotal(events, reads, times) {
  need(reads, ["paidCount", "paidTotal"]);
  const list = named(ordered(events), "PayoutPaid").map((e) => ({
    id: Number(e.args.id), trader: e.args.trader, amount: BigInt(e.args.amount), ...at(e, times),
  }));
  const sum = list.reduce((s, p) => s + p.amount, 0n);
  const count = BigInt(reads.paidCount);
  const total = BigInt(reads.paidTotal);
  const complete = BigInt(list.length) === count && sum === total;
  return { total, count, listed: sum, payments: list, complete, includes: TOTAL_INCLUDES };
}

/** The pool's counters of stops: every stop, every lift, the stops that stand, and the payouts paid. */
export function stopCounters(reads) {
  need(reads, ["stoppedCount", "liftedCount", "paidCount"]);
  const stopped = BigInt(reads.stoppedCount);
  const lifted = BigInt(reads.liftedCount);
  return { stopped, lifted, standing: stopped - lifted, paid: BigInt(reads.paidCount) };
}

/**
 * The five fields that make one paid payout checkable by anyone, the ones a public payout record needs before a
 * stranger can match it to the chain without trusting whoever published it:
 *   1. the payout's id, which is also a topic of the payment's own log, so the record and the transaction meet;
 *   2. the chain and the full transaction hash, and the token;
 *   3. the amount as the transfer carried it (one amount: nobody's share is taken out of a payout);
 *   4. when it was asked for (the claim) and when it was paid;
 *   5. the address it was paid from: the pool's contract, which also holds the pool's money.
 * Null for a payout that is not paid, or whose payment event was not read.
 */
export function proofOf(payout, { pool, usdc, chainId }) {
  if (payout.status !== "paid" || !payout.paid) return null;
  return {
    id: payout.id,
    chainId,
    tx: payout.paid.tx,
    token: usdc,
    amount: payout.amount,
    requestedAt: payout.due?.time ?? null,
    requestTx: payout.due?.tx ?? null,
    paidAt: payout.paid.time,
    from: pool,
    to: payout.trader,
  };
}

/**
 * Where the events the getters count were not all read: a paid payout without its payment, a stop without its
 * reasons, a lifted stop without its lift, a payout without its claim. Each gap names the payout and what is missing.
 */
export function gaps(list) {
  const out = [];
  for (const p of list) {
    if (!p.due) out.push({ id: p.id, missing: "PayoutDue" });
    if (p.status === "paid" && !p.paid) out.push({ id: p.id, missing: "PayoutPaid" });
    if ((p.status === "stopped" || p.wasStopped) && !p.stop) out.push({ id: p.id, missing: "PayoutStopped" });
    // Stopped once and no longer stopped: lifted, unless the stop was made final (refused).
    if (p.wasStopped && p.status !== "stopped" && p.status !== "refused" && !p.lift) {
      out.push({ id: p.id, missing: "StopLifted" });
    }
    if (p.status === "held" && !p.heldAt) out.push({ id: p.id, missing: "PayoutHeld" });
    if (p.status === "refused" && !p.final) out.push({ id: p.id, missing: "StopFinal" });
  }
  return out;
}

/**
 * The creator's standing request to withdraw, as a page shows it: how much, from when it can be carried out and until
 * when, and where it stands now ("waiting", "ready", "lapsed"); null with none, or for a pool that does not answer
 * `requested()`. `finishFrom`: from when a closed pool may finish, a week after its close.
 */
export function withdrawalOf(reads, now) {
  // Both read, or neither shown: a missing readyAt would make every comparison false and the request "lapsed"
  // (review of #282).
  if (reads.requested === undefined || reads.requested === null || reads.readyAt === undefined || reads.readyAt === null) {
    return null;
  }
  const finishFrom = reads.closed && reads.closedAt ? Number(reads.closedAt) + WITHDRAW_DELAY : null;
  const amount = BigInt(reads.requested);
  if (amount === 0n) return { amount: 0n, readyAt: null, lapsesAt: null, state: "none", finishFrom };
  const readyAt = Number(reads.readyAt);
  const lapsesAt = readyAt + WITHDRAW_WINDOW;
  const state = now < readyAt ? "waiting" : now < lapsesAt ? "ready" : "lapsed";
  return { amount, readyAt, lapsesAt, state, finishFrom };
}

/**
 * The whole page of one simulated pool. `reads` are the pool's getters (menu, payoutCount's payouts as `payouts`,
 * paidCount, paidTotal, stoppedCount, liftedCount, owed, held, live, closed, finished, codes (each code's codeOf
 * with its key: { code, partner, bps, used, brought }), terms, fundWindow); `events` the pool's events from its creation on; `times` a Map of block → seconds; `now` the chain's
 * clock. No field of it carries what the creator locked or put in.
 */
export function simPoolPage({ pool, usdc, chainId, reads, events, times, now, scan }) {
  // The reading the events came from (readLogs): without it the page could not say how far the chain was read, and
  // lists cut short by the node's budget would look whole.
  if (!scan || ![scan.from, scan.to, scan.readTo].every(Number.isInteger) || typeof scan.complete !== "boolean") {
    throw new Error("simPoolPage needs the scan its events came from: { from, to, readTo, complete } (readLogs)");
  }
  need(reads, ["menu", "payouts", "paidCount", "paidTotal", "stoppedCount", "liftedCount", "owed", "held", "live",
    "closed", "finished", "codes", "terms", "fundWindow"]);
  const list = payouts(reads.payouts, events, now, times);
  const total = paidTotal(events, reads, times);
  const missing = gaps(list);
  const byStatus = {};
  for (const k of Object.keys(STATUS)) byStatus[k] = list.filter((p) => p.status === k);
  return {
    pool,
    chainId,
    token: usdc,
    menu: menuLines(reads.menu),
    exams: soldExams(events, times),
    stages: stages(events, times),
    payouts: list,
    byStatus,
    total,
    proofs: list.map((p) => proofOf(p, { pool, usdc, chainId })).filter(Boolean),
    counters: stopCounters(reads),
    owed: BigInt(reads.owed),
    held: BigInt(reads.held),
    // Stopped payouts' money held for the stop reserve; null for a pool without it.
    reserved: reads.reserved === undefined || reads.reserved === null ? null : BigInt(reads.reserved),
    live: BigInt(reads.live),
    closed: Boolean(reads.closed),
    finished: Boolean(reads.finished),
    // The time before which the creator can neither close the pool nor take back what it put in (null if not read),
    // and whether the platform's closer closed it sooner.
    lockedUntil: reads.lockedUntil === undefined || reads.lockedUntil === null ? null : Number(reads.lockedUntil),
    closedEarly: Boolean(reads.closedEarly),
    creatorMayClose: reads.lockedUntil === undefined || reads.lockedUntil === null ? null : now >= Number(reads.lockedUntil),
    termNote: termNote(reads, now),
    withdrawal: withdrawalOf(reads, now),
    codes: codeShares(reads.codes, events),
    terms: reads.terms,
    fundWindow: Number(reads.fundWindow),
    gaps: missing,
    scan: { from: scan.from, to: scan.to, readTo: scan.readTo, complete: scan.complete },
    scanText: scanText(scan),
    // Everything on the page is whole only if the chain was read to the end, the payments add up to the contract's
    // counters, and no payout lacks an event. Otherwise the page says which, and shows the contract's own total.
    complete: scan.complete && total.complete && missing.length === 0,
  };
}

/**
 * The pool's term in words, for the page to show as it is: the page keeps no words of its own about closing.
 * `state` is "locked" (before the term: its creator can neither close it nor take back what it put in), "open" (the
 * term is over, the pool still sells), "closed" (closed at or after its term) or "closed-early" (the platform's closer
 * closed it before its term). Null for a pool without a term (`lockedUntil` not read).
 */
export function termNote(reads, now) {
  if (reads.lockedUntil === undefined || reads.lockedUntil === null) return null;
  const until = Number(reads.lockedUntil);
  if (reads.closedEarly) {
    return { state: "closed-early", until,
      text: `Closed early by the platform, before its term ended on ${utc(until)}. The money goes where it would `
        + "anyway: to the traders, to the codes' owners, and the rest to the creator." };
  }
  if (reads.closed) return { state: "closed", until, text: `Closed after its term ended on ${utc(until)}.` };
  if (now < until) {
    return { state: "locked", until,
      text: `Runs at least until ${utc(until)}: before then its creator can neither close it nor take back what it `
        + "put in." };
  }
  return { state: "open", until, text: `Its term ended on ${utc(until)}: its creator may close it now.` };
}

/** The line a page shows over its lists: how far the chain was read, and whether the lists are whole. */
export function scanText(scan) {
  if (scan.complete) {
    return `Read from block ${scan.from} to block ${scan.to}: every event of this pool in that range is listed.`;
  }
  return `Read from block ${scan.from} to block ${scan.readTo} of ${scan.to}: the lists below stop there and may miss `
    + "what happened after. The total paid is the contract's own.";
}

/** The factory's pools, oldest first, each with its creator and the transaction that opened it. The amount locked
 *  at the opening is not carried, and a pool's partners are its codes (codeShares). */
export function factoryPools(events, times) {
  return named(ordered(events), "PoolCreated").map((e) => ({
    pool: e.args.pool,
    creator: e.args.creator,
    ...at(e, times),
  }));
}

/**
 * Each code of a pool: its partner, its share (bps of the profit a withdrawal shares), whether a sale has fixed it,
 * what its sales brought and that part of all the pool took (bps), and what its partner was sent (`paid`) or had set
 * aside because the token refused (`setAside`), from the PartnerPaid events read. `codes` are the codeOf reads.
 */
export function codeShares(codes, events) {
  const all = codes.reduce((t, k) => t + BigInt(k.brought), 0n);
  const paid = new Map();
  for (const e of named(ordered(events), "PartnerPaid")) {
    const key = String(e.args.code).toLowerCase();
    const was = paid.get(key) ?? { paid: 0n, setAside: 0n };
    if (e.args.sent) was.paid += BigInt(e.args.amount);
    else was.setAside += BigInt(e.args.amount);
    paid.set(key, was);
  }
  return codes.map((k) => {
    const brought = BigInt(k.brought);
    const got = paid.get(String(k.code).toLowerCase()) ?? { paid: 0n, setAside: 0n };
    return {
      code: k.code,
      partner: k.partner,
      bps: Number(k.bps),
      used: Boolean(k.used),
      brought,
      partOfAll: all === 0n ? 0 : Number(brought * BPS / all),
      paid: got.paid,
      setAside: got.setAside,
    };
  });
}

/**
 * Reads logs from `from` to `to` in windows of `window` blocks, at most `maxCalls` calls, through `getLogs(from, to)`.
 * Returns the logs, the last block read, and whether the range was read to its end. A public node limits both the
 * window and the calls (app/lib/keys.js on the 50-block window); a page that stops short says so with `readTo`.
 */
export async function readLogs({ getLogs, from, to, window = 50, maxCalls = 40 }) {
  if (window < 1) throw new Error("readLogs: a window is at least one block");
  const logs = [];
  let next = from;
  let calls = 0;
  while (next <= to && calls < maxCalls) {
    const end = Math.min(next + window - 1, to);
    logs.push(...await getLogs(next, end));
    calls += 1;
    next = end + 1;
  }
  return { logs, from, to, readTo: next - 1, complete: next > to, calls };
}

/**
 * The first block at or after `time` (seconds), by halving between `low` and `high` with `timeOf(block)`. A payout's
 * claim is in the block whose time is its `dueAt` minus 72 hours, and its payment at or after `dueAt`: so a page can
 * read the few blocks around them instead of the pool's whole history.
 */
export async function firstBlockAt(time, { low, high, timeOf }) {
  let lo = low;
  let hi = high;
  if (lo > hi) return null;
  if (await timeOf(hi) < time) return null;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (await timeOf(mid) < time) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** The time a payout's claim was written: its `dueAt` less the 72 hours (SimPool.claimPayout). */
export function claimedAt(dueAt) {
  return Number(dueAt) - PAYOUT_DELAY;
}

// ── buying with a code ───────────────────────────────────────────

const WORD = /^0x[0-9a-fA-F]{64}$/;
const ZERO_ADDRESS = /^0x0{40}$/i;

function word(value, name) {
  if (typeof value !== "string" || !WORD.test(value)) throw new Error(`${name} is not 32 bytes in 0x-hex: ${value}`);
  return value.toLowerCase();
}

/** The UTF-8 bytes of `text`, as 0x-hex. */
function utf8Hex(text) {
  return `0x${Array.from(new TextEncoder().encode(text), (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * What a pool keys a trading group's code by, and what a sale names: the keccak256 of the code's text, exactly as
 * given (ops/deploy_sim.py, code_hash). `keccak256` is the page's own (ethers.keccak256), over 0x-hex bytes. Nothing
 * is trimmed or folded here, so the hash is the one the creator set: a page that forgives a stray space trims first.
 */
export function codeHash(text, keccak256) {
  if (typeof text !== "string" || text.length === 0) throw new Error("a code is a text, not empty");
  return word(keccak256(utf8Hex(text)), "keccak256");
}

/** The nonce a trader signs to buy by signature: keccak256(abi.encode(code, salt)), two 32-byte words, so the code is
 *  in the signature and whoever sends it cannot name another (SimPool.buyWithAuthorization, ops/sim_relay.py). */
export function saleNonce(code, salt, keccak256) {
  return word(keccak256(`0x${word(code, "code").slice(2)}${word(salt, "salt").slice(2)}`), "keccak256");
}

/** EIP-3009's message, as Circle's FiatToken signs it (the token's own name, version, chain and address are its domain). */
export const RECEIVE_WITH_AUTHORIZATION_TYPES = Object.freeze({
  ReceiveWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
});

/**
 * A purchase off line `line` with `code` (a code's keccak256), checked as the pool would check it before anything is
 * signed or sent: the line is on the menu, and, when the page has read the pool's codes (`codes`, as simPoolPage
 * takes them), the code is one of them with a partner, that is, on sale. Returns `buy`'s arguments and the price the
 * trader approves.
 */
export function purchase({ menu, line, code, codes }) {
  const n = Number(line);
  if (!Number.isInteger(n) || n < 0 || n >= menu.length) throw new Error(`no line ${line} on this pool's menu`);
  const key = word(code, "code");
  if (codes) {
    const set = codes.find((k) => String(k.code).toLowerCase() === key);
    if (!set || ZERO_ADDRESS.test(String(set.partner))) throw new Error("this code is not on sale at this pool");
  }
  return { method: "buy", args: [BigInt(n), key], price: BigInt(menu[n].price) };
}

/**
 * A purchase by the trader's signature, which a relayer sends (ops/sim_relay.py): what the trader signs, the EIP-3009
 * transfer of the line's price to the pool with the nonce of the code and `salt` (any 32 bytes, new for each
 * purchase); then, given the signature, `buyWithAuthorization`'s arguments and the relayer's JSON body.
 */
export function signedPurchase({ pool, trader, menu, line, code, codes, salt, validAfter = 0, validBefore, keccak256 }) {
  const { price, args } = purchase({ menu, line, code, codes });
  const key = args[1];
  const s = word(salt, "salt");
  if (!(BigInt(validBefore) > BigInt(validAfter))) throw new Error("the signature must end after it starts");
  const nonce = saleNonce(key, s, keccak256);
  const message = { from: trader, to: pool, value: price, validAfter: BigInt(validAfter), validBefore: BigInt(validBefore), nonce };
  return {
    nonce,
    message,
    types: RECEIVE_WITH_AUTHORIZATION_TYPES,
    args: ({ v, r, s: sig }) => [trader, args[0], key, s, message.validAfter, message.validBefore, v, r, sig],
    relayBody: ({ v, r, s: sig }) => ({
      pool, trader, line: Number(args[0]), code: key, salt: s,
      // Decimal strings, not numbers: a JSON number past 2^53 would reach the relayer rounded, and the signed
      // fields would no longer be the trader's (review of #279). The relayer reads them with int().
      valid_after: BigInt(validAfter).toString(), valid_before: BigInt(validBefore).toString(), v, r, s: sig,
    }),
  };
}
