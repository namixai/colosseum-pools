// A pool of many traders, from the trader's side: what its pages read of a MultiPool and of the paper exams it
// sells, which of the calls anyone may make are open now, and why the others are not.
//
// The contracts are drafts (src/multi/MultiPool.sol, src/exam/PaperExam.sol at commit 378fae1f, pull requests
// #136 to #143), not deployed and not in this branch. Their ABI is taken word for word from `forge inspect` at that
// commit (app/tests/multi-abi.json), and the test holds every fragment here to it. The addresses come from
// the deployment's record (CONFIG.multi, app/config.js) once one is made.
//
// What anyone may call and no keeper calls yet -- a fill, the day's snapshot, a breach, the opening of a funded
// account -- is a button on the page with the reason it is or is not open. The contract decides in the end: a
// button that is open here and refused there shows the contract's refusal.
//
// No browser globals here, so node's test runner loads it (app/tests/multi.test.mjs).

export const SOURCE = "378fae1fd8fbd0485528fa97350baae19149159c";

export const MULTI_ABI = Object.freeze([
  "function plan() view returns (((uint64 size, uint64 price)[] menu, (uint16 dailyLossBps, uint16 maxDrawdownBps, uint32 maxLeverageX100, uint32[] assets) examRules, (uint64 capital, uint16[] targetsBps, uint32 stepDuration, uint32 maxIdle, uint16 minTradingDays, uint32 costPpm, uint16 fillDelay, uint16 fillWindow) exam, (uint16 dailyLossBps, uint16 maxDrawdownBps, uint32 maxLeverageX100, uint32[] assets) fundedRules, uint16 fundedTargetBps, uint16 traderShareBps, uint8 afterTarget, uint16 inWorkBps, uint16 maxSeats, uint32 claimWindow, uint32 term, uint16 maxRetakes, uint32 fundedIdle, uint16 freePerMonth, address inviter))",
  "function owner() view returns (address)",
  "function factory() view returns (address)",
  "function usdc() view returns (address)",
  "function value() view returns (uint256)",
  "function inWork() view returns (uint256)",
  "function held() view returns (uint256)",
  "function room(uint256 line) view returns (uint256)",
  "function endsAt() view returns (uint64)",
  "function startedAt() view returns (uint64)",
  "function closing() view returns (bool)",
  "function saleLead() view returns (uint64)",
  "function productBounds() view returns (bool)",
  "function minTerm() view returns (uint32)",
  "function maxTerm() view returns (uint32)",
  "function notice() view returns (uint64)",
  "function seats() view returns (address[])",
  "function seatInfo(address seat) view returns (uint8 line, uint8 state, bool targetHit, address trader, uint64 armedAt, uint64 restUntil, uint64 waitUntil, uint16 retakes)",
  "function examInfo(address exam) view returns (uint8 line, uint8 state, address seat)",
  "function freeMonth() view returns (uint32)",
  "function freeTaken() view returns (uint16)",
  "function FREE_MONTH() view returns (uint32)",
  "function ARM_WAIT() view returns (uint64)",
  "function inviteSpent(uint64 nonce) view returns (bool)",
  "function buy(uint256 line) returns (address exam)",
  "function takeFree(uint8 line, uint64 nonce, uint64 expiresAt, uint8 v, bytes32 r, bytes32 s) returns (address exam)",
  "function claim(address exam) returns (address seat)",
  "function open(address seat)",
  "function rearm(address seat)",
  "function reopen(address seat)",
  "function release(address exam)",
  "function finish(address seat)",
  "function sweep(address seat)",
  "function noteActivity(address seat)",
  "function hitTarget(address seat, (uint32 asset, uint64 oid)[] cancels, uint32[] extraAssets, bytes32 salt)",
  "function closeIdle(address seat, (uint32 asset, uint64 oid)[] cancels, uint32[] extraAssets, bytes32 salt)",
  "function endSeat(address seat, (uint32 asset, uint64 oid)[] cancels, uint32[] extraAssets, bytes32 salt)",
  "event ExamSold(address indexed exam, address indexed trader, uint256 line, uint64 size, uint64 price)",
  "event FreeExamTaken(address indexed exam, address indexed trader, uint64 nonce)",
  "event Claimed(address indexed exam, address indexed trader, address indexed seat)",
  "error AlreadyArmed()",
  "error BadPlan()",
  "error BadSeat()",
  "error BufferNeeded()",
  "error CoreWriterLib__CannotSelfTransfer()",
  "error Ended()",
  "error Ending()",
  "error FailedDeployment()",
  "error Full()",
  "error InsufficientBalance(uint256 balance, uint256 needed)",
  "error InviteLapsed()",
  "error InviteSpent()",
  "error Locked()",
  "error NoFreeLeft()",
  "error NoRoom()",
  "error NoSuchLine()",
  "error NoTarget()",
  "error NotEnded()",
  "error NotEnoughFree(uint64 spot, uint64 inFlight, uint64 cost)",
  "error NotFlat()",
  "error NotInvited()",
  "error NotOwner()",
  "error NotPassed()",
  "error NotSold()",
  "error NotTrader()",
  "error PrecompileLib__AccountMarginSummaryPrecompileFailed()",
  "error PrecompileLib__CoreUserExistsPrecompileFailed()",
  "error PrecompileLib__Position2PrecompileFailed()",
  "error PrecompileLib__SpotBalancePrecompileFailed()",
  "error RuleBroken(uint8 reason)",
  "error SafeERC20FailedOperation(address token)",
  "error StillRunning()",
  "error StillTrading(uint256 until)",
  "error TargetNotMet(int256 equity, int256 target)",
  "error TooLate()",
  "error TooSoon()",
]);

export const EXAM_ABI = Object.freeze([
  "function pool() view returns (address)",
  "function trader() view returns (address)",
  "function agent() view returns (address)",
  "function status() view returns (uint8)",
  "function step() view returns (uint8)",
  "function stepStartedAt() view returns (uint64)",
  "function deadline() view returns (uint64)",
  "function lastFillAt() view returns (uint64)",
  "function passedAt() view returns (uint64)",
  "function endReason() view returns (uint8)",
  "function endEquity() view returns (int256)",
  "function day() view returns (uint32)",
  "function dayStartEquity() view returns (int256)",
  "function tradingDays() view returns (uint16)",
  "function rules() view returns ((uint16 dailyLossBps, uint16 maxDrawdownBps, uint32 maxLeverageX100, uint32[] assets))",
  "function terms() view returns ((uint64 capital, uint16[] targetsBps, uint32 stepDuration, uint32 maxIdle, uint16 minTradingDays, uint32 costPpm, uint16 fillDelay, uint16 fillWindow))",
  "function pending() view returns (uint32 asset, bool isBuy, uint64 sz, uint64 placedBlock)",
  "function equity() view returns (int256 eq)",
  "function notional() view returns (uint256 ntl)",
  "function cash() view returns (int256)",
  "function violation() view returns (uint8)",
  "function positionOf(uint32 asset) view returns (int256)",
  "function result() view returns (uint8, address, uint64, uint64)",
  "function place(uint32 asset, bool isBuy, uint64 sz)",
  "function fill()",
  "function checkpoint()",
  "function breach()",
  "function expire()",
  "function graduate()",
  "function setAgent(address agent_)",
  "event Placed(uint32 indexed asset, bool isBuy, uint64 sz, uint256 fillFrom, uint256 fillUntil)",
  "event Filled(uint32 indexed asset, bool isBuy, uint64 sz, uint64 px, uint256 cost)",
  "event Passed(address indexed trader, int256 equity)",
  "event Stopped(uint8 indexed status, uint8 indexed reason, int256 equity)",
  "event StepPassed(uint8 indexed step, int256 equity)",
  "error AlreadyCheckpointed()",
  "error AssetNotAllowed(uint32 asset)",
  "error BadRules()",
  "error BadStatus(uint8 status)",
  "error BadTerms()",
  "error InvalidInitialization()",
  "error NoBreach()",
  "error NoOrder()",
  "error NoPrice(uint32 asset)",
  "error NotEnoughDays(uint16 traded, uint16 needed)",
  "error NotFlat()",
  "error NotInitializing()",
  "error NotOver()",
  "error NotTrader()",
  "error OrderLapsed()",
  "error OrderPending()",
  "error OutsideCheckpointWindow()",
  "error OverLeverage()",
  "error PrecompileLib__MarkPxPrecompileFailed()",
  "error PrecompileLib__PerpAssetInfoPrecompileFailed()",
  "error RuleBroken(uint8 reason)",
  "error TargetNotMet(int256 equity, int256 target)",
  "error TooEarly(uint256 fillFrom)",
  "error TooLate()",
  "error UnsupportedSizeDecimals(uint32 asset, uint8 szDecimals)",
  "error ZeroSize()",
]);


/** MultiPool's record of an exam it sold (`examInfo`). */
export const EXAM_STATE = Object.freeze(["None", "Sold", "Claimed", "Released"]);
/** A paper exam's own status. */
export const EXAM_STATUS = Object.freeze(["None", "Active", "Passed", "Breached", "Expired"]);
/** A seat: a funded account the pool owns and runs. */
export const SEAT_STATE = Object.freeze(["None", "Free", "Armed", "Funded", "Waiting"]);
/** What the plan says happens at a funded account's target. */
export const AFTER_TARGET = Object.freeze(["The trader stops it; there is no target", "The account closes at its target",
  "The account stays after its target, and its trader may take it again"]);
export const BREACH = Object.freeze(["None", "Drawdown", "Daily loss", "Leverage", "Forbidden asset"]);
/** The paper exam takes the day's snapshot in the first fifteen minutes of the UTC day. */
export const CHECKPOINT_WINDOW = 15 * 60;
export const DAY = 86_400;
const BPS = 10_000n;

const n = (x) => Number(x);
const big = (x) => BigInt(x);
const lower = (a) => String(a || "").toLowerCase();

/** 1e6 = 1 USDC, as a number of USDC. */
export const usdc6 = (x) => n(x) / 1e6;

/** A length of time in words: days where it is days, else hours, else minutes. */
export function span(seconds) {
  const s = n(seconds);
  if (s === 0) return "none";
  if (s % DAY === 0) return `${s / DAY} day${s === DAY ? "" : "s"}`;
  if (s % 3600 === 0) return `${s / 3600} hour${s === 3600 ? "" : "s"}`;
  if (s >= 3600) return `${(s / 3600).toFixed(1)} hours`;
  return `${Math.round(s / 60)} minutes`;
}

const pct = (bps) => `${(n(bps) / 100).toFixed(2).replace(/\.?0+$/, "")}%`;

/** The plan as the page shows it. `plan` is what `plan()` returns, field by field. */
export function planView(plan) {
  const rules = (r) => ({ dailyLoss: pct(r.dailyLossBps), drawdown: pct(r.maxDrawdownBps),
    leverage: `${n(r.maxLeverageX100) / 100}×`, assets: [...r.assets].map(n) });
  return {
    menu: [...plan.menu].map((m, line) => ({ line, size: usdc6(m.size), price: usdc6(m.price) })),
    examRules: rules(plan.examRules),
    exam: { targets: [...plan.exam.targetsBps].map(pct), step: span(plan.exam.stepDuration), idle: span(plan.exam.maxIdle),
      tradingDays: n(plan.exam.minTradingDays), cost: `${n(plan.exam.costPpm) / 10_000}%`,
      fillDelay: n(plan.exam.fillDelay), fillWindow: n(plan.exam.fillWindow) },
    fundedRules: rules(plan.fundedRules),
    fundedTarget: n(plan.afterTarget) === 0 ? "none" : pct(plan.fundedTargetBps),
    traderShare: pct(plan.traderShareBps),
    afterTarget: AFTER_TARGET[n(plan.afterTarget)] ?? `unknown (${n(plan.afterTarget)})`,
    inWork: pct(plan.inWorkBps),
    seats: n(plan.maxSeats),
    claimWindow: span(plan.claimWindow),
    term: span(plan.term),
    retakes: n(plan.afterTarget) === 2 ? (n(plan.maxRetakes) === 0 ? "no limit" : String(n(plan.maxRetakes))) : "—",
    fundedIdle: span(plan.fundedIdle),
    freePerMonth: n(plan.freePerMonth),
    inviter: plan.inviter,
  };
}

/** Whether a pool is one the product offers or one deployed with shorter bounds to watch a whole term. */
export function boundsView({ productBounds, minTerm, maxTerm, notice }) {
  const words = `a term of ${span(minTerm)} to ${span(maxTerm)}, ${span(notice)} with nothing sold at the end`;
  return productBounds
    ? { test: false, words: `The product's bounds: ${words}.` }
    : { test: true, words: `Test bounds, not the product's: ${words}. A pool deployed to watch a whole term quickly.` };
}

const action = (id, label, who, enabled, why) => ({ id, label, who, enabled: Boolean(enabled), why });

/**
 * What can be done on an exam now, and by whom. `e` is what the exam and the pool say of it; `me` the connected
 * wallet, or null; `now` the chain's time in seconds; `block` its latest block.
 */
export function examActions(e, me, now, block) {
  const active = n(e.status) === 1;
  const over = (n(e.deadline) !== 0 && now > n(e.deadline)) || (n(e.maxIdle) !== 0 && now > n(e.lastFillAt) + n(e.maxIdle));
  const trader = me !== null && lower(me) === lower(e.trader);
  const placer = trader || (me !== null && lower(e.agent) !== lower("0x0000000000000000000000000000000000000000") && lower(me) === lower(e.agent));
  const waiting = big(e.pending.sz) !== 0n;
  const from = n(e.pending.placedBlock) + n(e.fillDelay);
  const until = from + n(e.fillWindow) - 1;
  const capital = big(e.capital);
  const target = capital + (capital * big(e.targetsBps[n(e.step)] ?? 0)) / BPS;
  const passedAt = n(e.passedAt);
  const sold = n(e.poolState) === 1;
  const ended = n(e.status) === 3 || n(e.status) === 4;
  const windowShut = n(e.status) === 2 && now > passedAt + n(e.claimWindow);
  return [
    action("place", "Place an order", "the trader or their agent",
      active && !over && placer && !(waiting && block <= until),
      !active ? "the exam is not running" : over ? "the step is past its time" : !placer ? "only the exam's trader or their agent places orders"
        : waiting && block <= until ? `an order is waiting until block ${until}` : "one order waits at a time; it fills at the mark of a later block"),
    action("fill", "Fill the waiting order", "anyone",
      active && !over && waiting && block >= from && block <= until,
      !waiting ? "no order is waiting" : block < from ? `it can be filled from block ${from}` : block > until ? `its window closed at block ${until}`
        : `anyone may fill it, from block ${from} to block ${until}, at the mark of the block that fills it`),
    action("checkpoint", "Take the day's snapshot", "anyone",
      active && now % DAY < CHECKPOINT_WINDOW && Math.floor(now / DAY) > n(e.day),
      Math.floor(now / DAY) <= n(e.day) ? "today's snapshot is taken" : now % DAY >= CHECKPOINT_WINDOW
        ? "only in the first fifteen minutes of the UTC day" : "the day's loss is measured from it"),
    action("breach", "Stop it: a rule is broken", "anyone", active && n(e.violation) !== 0,
      n(e.violation) !== 0 ? `${BREACH[n(e.violation)]} is broken at this moment` : "no rule is broken at this moment"),
    action("expire", "End it: time is up", "anyone", active && over, over ? "the step is past its deadline or its idle limit" : "the step is in time"),
    action("graduate", "Pass the step", "the trader or their agent",
      active && !over && placer && n(e.violation) === 0 && big(e.notional) === 0n && big(e.equity) >= target && n(e.tradingDays) >= n(e.minTradingDays),
      !placer ? "only the trader or their agent passes a step" : big(e.notional) !== 0n ? "everything has to be closed first"
        : big(e.equity) < target ? `equity has to reach ${usdc6(target)} USDC` : n(e.tradingDays) < n(e.minTradingDays)
          ? `${n(e.minTradingDays)} trading days are needed` : "no rule broken, nothing open, the target met"),
    action("claim", "Take the funded account", "the trader",
      n(e.status) === 2 && sold && trader && !windowShut,
      n(e.status) !== 2 ? "the exam has not passed" : !sold ? "its account was taken or its size given back" : !trader ? "only the exam's trader takes the account"
        : windowShut ? "the window to take it has closed" : `until ${new Date((passedAt + n(e.claimWindow)) * 1000).toISOString().slice(0, 16)} UTC`),
    action("release", "Give the size back to the pool", "anyone", sold && (ended || windowShut),
      ended ? "the exam ended without a pass" : windowShut ? "nobody took the account in its window" : "only an exam that ended without a pass, or a pass nobody took in time"),
  ];
}

/** What can be done on a seat now. `s` is `seatInfo(seat)`; `stage` is the seat's own `Pool.stage()`. */
export function seatActions(s, { me, now, armWait, afterTarget, ended, stage }) {
  const state = n(s.state);
  const mine = me !== null && lower(me) === lower(s.trader);
  return [
    action("open", "Open the funded account", "anyone", state === 2 && now >= n(s.restUntil),
      state !== 2 ? "no account is waiting to open" : now < n(s.restUntil) ? "the seat rests a minute after it was freed"
        : "once the money has arrived, a few seconds after the claim; the contract refuses it before"),
    action("rearm", "Send the money again", "anyone", state === 2 && now >= n(s.armedAt) + n(armWait),
      state !== 2 ? "no money is on its way" : "if the money did not arrive within a minute"),
    action("reopen", "Take the account again", "its trader", state === 4 && mine && now <= n(s.waitUntil),
      state !== 4 ? "the seat is not waiting for its trader" : !mine ? "only its trader" : now > n(s.waitUntil) ? "the wait is over" : "without another exam"),
    action("hitTarget", "Close it at its target", "anyone", state === 3 && n(afterTarget) !== 0 && !ended,
      n(afterTarget) === 0 ? "this plan has no target" : state !== 3 ? "no account is trading" : "once equity has reached the target, with nothing open"),
    action("closeIdle", "Close it: nobody trades it", "anyone", state === 3, state !== 3 ? "no account is trading" : "after the plan's time without a trade"),
    action("noteActivity", "Note a trade", "anyone", state === 3, state !== 3 ? "no account is trading" : "so a trader who traded is not taken for one who did not"),
    action("endSeat", "End it: the pool's term is over", "anyone", state === 3 && ended, ended ? "the pool has stopped" : "only after the pool's end"),
    action("finish", "Close the books", "anyone", state === 3 && n(stage) === 0,
      state !== 3 ? "no account to close" : n(stage) !== 0 ? "the stage has not been settled" : "the money goes back to the pool"),
    action("sweep", "Take back what reached a free seat", "anyone", state === 1, state === 1 ? "a free seat only" : "the seat is not free"),
  ];
}

/**
 * An invitation as `ops/multi_run.py invite` prints it, checked before it is sent: for this pool, for the wallet
 * that sends it, not lapsed, on a line of the menu, signed (v, r, s in their forms).
 */
export function parseInvite(text, { pool, me, now, lines }) {
  let inv;
  try {
    inv = JSON.parse(String(text));
  } catch {
    return { error: "This is not an invitation: paste the line of JSON it was given as." };
  }
  if (!inv || typeof inv !== "object") return { error: "This is not an invitation." };
  if (lower(inv.pool) !== lower(pool)) return { error: "This invitation is for another pool." };
  if (me === null) return { error: "Connect the wallet the invitation names." };
  if (lower(inv.trader) !== lower(me)) return { error: "This invitation names another wallet: only its trader can take it." };
  if (!Number.isInteger(inv.line) || inv.line < 0 || inv.line >= lines) return { error: "This invitation names a size the menu does not have." };
  if (!Number.isInteger(inv.nonce) || inv.nonce < 0) return { error: "This invitation has no number." };
  if (!Number.isInteger(inv.expiresAt) || inv.expiresAt < now) return { error: "This invitation has lapsed." };
  if (![27, 28].includes(inv.v) || !/^0x[0-9a-f]{64}$/i.test(String(inv.r)) || !/^0x[0-9a-f]{64}$/i.test(String(inv.s))) {
    return { error: "This invitation's signature is not whole." };
  }
  return { invite: { line: inv.line, nonce: inv.nonce, expiresAt: inv.expiresAt, v: inv.v, r: inv.r, s: inv.s } };
}

/** An order's size as the exam takes it: whole units of the asset's size decimals. */
export function sizeUnits(text, szDecimals) {
  const t = String(text).trim();
  if (!/^\d+(\.\d+)?$/.test(t)) throw new Error("A size is a plain number, like 0.005.");
  const [whole, frac = ""] = t.split(".");
  if (frac.length > szDecimals) throw new Error(`This asset takes at most ${szDecimals} decimals.`);
  const units = BigInt(whole + frac.padEnd(szDecimals, "0"));
  if (units === 0n) throw new Error("A size above zero.");
  return units;
}

/** A size in whole units, back as an amount of the asset. */
export function coinAmount(units, szDecimals) {
  const neg = big(units) < 0n;
  const s = (neg ? -big(units) : big(units)).toString().padStart(szDecimals + 1, "0");
  const out = szDecimals ? `${s.slice(0, -szDecimals)}.${s.slice(-szDecimals)}`.replace(/\.?0+$/, "") : s;
  return `${neg ? "-" : ""}${out}`;
}

const myExamsKey = (pool, me) => `pools-my-exams-v1:${lower(pool)}:${lower(me)}`;
/** The exams this browser bought on a pool for a wallet. The chain lists no exams by trader, so the page keeps them. */
export function myExams(store, pool, me) {
  try {
    const list = JSON.parse(store?.getItem(myExamsKey(pool, me)) || "[]");
    return Array.isArray(list) ? list.filter((a) => /^0x[0-9a-fA-F]{40}$/.test(a)) : [];
  } catch {
    return [];
  }
}
export function rememberExam(store, pool, me, exam) {
  const list = myExams(store, pool, me);
  if (!list.some((a) => lower(a) === lower(exam))) list.unshift(exam);
  try {
    store?.setItem(myExamsKey(pool, me), JSON.stringify(list.slice(0, 50)));
  } catch {
    // kept nowhere: the exam is still the chain's, and its address is in the receipt
  }
  return list;
}
