// The trader's screen of a paper exam (src/exam/PaperExam.sol): the account as the contract counts it, the lines it
// is judged by and how far each is, the order waiting and its window, the calls behind the buttons, an estimate of
// what an order would do before it is sent, and the contract's refusals in plain words. No browser globals, no reads
// and no keys here, so node's test runner loads it (app/tests/exam.test.mjs): the page reads the getters named in
// PAPER_EXAM_ABI and the chain's prices, hands them in, and gives the calls built here to the wallet that signs.
//
// What an exam is, for whoever draws the screen. It is traded on no exchange and on nobody's money: an order is a
// transaction to the trader's own exam contract (`place`), the position is a number there, and the platform's
// filler fills it a few blocks later at that block's price: the chain's oracle price, or, where the exam fills at
// the book, the trader's side of the best bid and offer, never better for the trader than the oracle. Orders are at
// the market and one waits at a time; there are no limits. A position is closed by an order of the trader's own, or
// by a stop or a take the trader set on it: once the oracle price has reached it, whoever carries it out closes the
// position (or a size of it) at the price a fill takes in that block, and that close is a trade like any fill. The
// drawdown line is the starting balance less the allowed drawdown, or, in an exam of the trailing kind, the highest
// day's snapshot of the step less that same amount, never above the starting balance. Every rule is judged on the
// oracle price of the block someone asks in.
//
// Units, as the contract holds them. Money is USDC with 6 decimals (1_000_000 is 1 USDC). A size is in the asset's
// own units, `szDecimals` decimals of Hyperliquid's list (0.001 BTC is 1 at 3 decimals... as an integer: 1). A price
// carries `6 - szDecimals` decimals, so size times price is already USDC units. Every number here that the contract
// compares is a BigInt and is compared as the contract compares it.

import { encodeCall } from "./operator.js";

/** What the screen reads and sends, as PaperExam declares them (a test holds each line to the source). */
export const PAPER_EXAM_ABI = [
  "function status() view returns (uint8)",
  "function step() view returns (uint8)",
  "function trader() view returns (address)",
  "function agent() view returns (address)",
  "function filler() view returns (address)",
  "function fromBook() view returns (bool)",
  "function maxWorseBps() view returns (uint16)",
  "function cash() view returns (int256)",
  "function positionOf(uint32 asset) view returns (int256)",
  "function equity() view returns (int256)",
  "function notional() view returns (uint256)",
  "function violation() view returns (uint8)",
  "function pending() view returns (uint32 asset, bool isBuy, uint64 sz, uint64 placedBlock)",
  "function stepStartedAt() view returns (uint64)",
  "function deadline() view returns (uint64)",
  "function lastFillAt() view returns (uint64)",
  "function day() view returns (uint32)",
  "function dayStartEquity() view returns (int256)",
  "function tradingDays() view returns (uint16)",
  "function drawdownKind() view returns (uint8)",
  "function peakSnapshot() view returns (int256)",
  "function drawdownLine() view returns (int256)",
  "function stopOf(uint32 asset) view returns (uint64 px, uint64 sz, uint64 armedAt, uint64 seenAt)",
  "function takeOf(uint32 asset) view returns (uint64 px, uint64 sz, uint64 armedAt, uint64 seenAt)",
  "function rules() view returns ((uint16 dailyLossBps, uint16 maxDrawdownBps, uint32 maxLeverageX100, uint32[] assets, uint32[] assetLeverageX100))",
  "function terms() view returns ((uint64 capital, uint16[] targetsBps, uint32 stepDuration, uint32 maxIdle, uint16 minTradingDays, uint32 costPpm, uint16 fillDelay, uint16 fillWindow))",
  "function place(uint32 asset, bool isBuy, uint64 sz)",
  "function setTrigger(uint32 asset, bool isTake, uint64 px, uint64 sz)",
  "function clearTrigger(uint32 asset, bool isTake)",
  "function graduate()",
  "function setAgent(address agent_)",
  "event AgentSet(address indexed agent)",
  "event Placed(uint32 indexed asset, bool isBuy, uint64 sz, uint256 fillFrom, uint256 fillUntil)",
  "event Lapsed(uint32 indexed asset, bool isBuy, uint64 sz)",
  "event Filled(uint32 indexed asset, bool isBuy, uint64 sz, uint64 px, uint256 cost, uint256 late, uint8 source)",
  "event TriggerSet(uint32 indexed asset, bool isTake, uint64 px, uint64 sz, uint256 armedAt)",
  "event TriggerCleared(uint32 indexed asset, bool isTake)",
  "event TriggerSeen(uint32 indexed asset, bool isTake, uint64 oracle, uint256 seenAt)",
  "event TriggerFilled(uint32 indexed asset, bool isTake, uint64 sz, uint64 px, uint256 cost, uint64 triggerPx, uint8 source)",
  "event DaySnapshot(uint32 indexed day, int256 equity)",
];

/** PaperExam.Status and Breach (src/Types.sol), by their numbers. */
export const STATUS = Object.freeze(["none", "active", "passed", "breached", "expired"]);
export const BREACH = Object.freeze(["none", "drawdown", "daily-loss", "leverage", "forbidden-asset"]);
/** PaperExam.FROM_START and TRAILING_TO_START: how the drawdown line is drawn (`drawdownKind`). */
export const FROM_START = 0;
export const TRAILING_TO_START = 1;
export const DRAWDOWN_KINDS = Object.freeze(["from-start", "trailing-to-start"]);
/** Blocks after an order's window from which anyone may fill one that only makes a position smaller. */
export const FALLBACK_BLOCKS = 60;
/** The least notional of a fill that counts as a trade: a trading day, and the end of a stretch without one. */
export const MIN_TRADE_NOTIONAL = 10_000_000n;
const BPS = 10_000n;
const PPM = 1_000_000n;

/** The chain's own prices, read with `eth_call`: the oracle price of a perp, and its best bid and offer. */
export const ORACLE_PX_PRECOMPILE = "0x0000000000000000000000000000000000000807";
export const BBO_PRECOMPILE = "0x000000000000000000000000000000000000080e";
const HIP3_BASE = 100000;

/** Who signs an order, as the page says it beside the button. */
export const SIGNS = Object.freeze({
  trader: "the trader's wallet: one signature and gas for each order",
  agent: "the second key the trader named: it signs orders with no prompt; it cannot change who trades",
  traderOnly: "the trader's wallet only",
});

/** What the page says beside any number worked out before an order is sent. */
export const ESTIMATE_NOTE = "An estimate at the prices of this moment. The order is filled a few blocks later, at "
  + "the price of the block that fills it.";
/** What the screen says once, in plain sight. */
export const WHAT_THIS_IS = "A paper exam: your orders are not sent to an exchange and no money is traded. Prices "
  + "and the book are Hyperliquid's. You close a position with an order of your own, or with a stop or a take you set "
  + "on it.";

const ADDRESS = /^0x[0-9a-f]{40}$/i;
const ZERO = /^0x0{40}$/i;
const abs = (x) => (x < 0n ? -x : x);
const ceilDiv = (a, b) => (a + b - 1n) / b;

// ── the calls ──────────────────────────────────────────────────────

function call(exam, signature, args, signer, keccak256) {
  if (!ADDRESS.test(String(exam))) throw new Error(`${exam} is not an exam's address`);
  return { to: exam, method: signature.split("(")[0], signature, args, data: encodeCall(signature, args, keccak256),
    signer, signs: SIGNS[signer] };
}

/** An asset's number, or an error: a missing one (null, undefined, an empty string) is no asset, where `Number` would
 *  make it asset 0; an explicit 0 is an asset (review of #294). */
function assetId(asset) {
  const missing = asset === null || asset === undefined || (typeof asset === "string" && asset.trim() === "");
  const a = missing ? NaN : Number(asset);
  if (!Number.isInteger(a) || a < 0 || a > 0xffffffff) throw new Error(`${asset} is not an asset`);
  return a;
}

/** `place(asset, isBuy, sz)`: an order at the market, `size` in the asset's size units (see `sizeUnits`). */
export function placeCall({ exam, asset, isBuy, size, by = "trader", keccak256 }) {
  const a = assetId(asset);
  const sz = BigInt(size);
  if (sz <= 0n) throw new Error("an order needs a size above zero");
  if (sz >= 1n << 64n) throw new Error("the size does not fit an order");
  if (by !== "trader" && by !== "agent") throw new Error("only the trader or the second key places an order");
  return call(exam, "place(uint32,bool,uint64)", [a, Boolean(isBuy), sz], by, keccak256);
}

/** `graduate()`: passes the step. The contract takes it only flat, at the target, in time, with no rule broken. */
export function graduateCall({ exam, by = "trader", keccak256 }) {
  if (by !== "trader" && by !== "agent") throw new Error("only the trader or the second key passes the step");
  return call(exam, "graduate()", [], by, keccak256);
}

/**
 * `setTrigger(asset, isTake, px, sz)`: a stop (`isTake` false) or a take on the position in `asset`, replacing the one
 * there: close `size` of it (0: all of it) once the oracle price reaches `px`, given as the contract holds prices
 * (`pxUnits`). It acts `fillDelay` blocks after it is set. The trader or the second key.
 */
export function setTriggerCall({ exam, asset, isTake, px, size = 0n, by = "trader", keccak256 }) {
  const a = assetId(asset);
  const p = BigInt(px);
  const sz = BigInt(size);
  if (p <= 0n) throw new Error("a stop or a take needs a price above zero");
  if (p >= 1n << 64n) throw new Error("the price does not fit a stop");
  if (sz < 0n) throw new Error("a stop or a take closes a size of zero (all of it) or more");
  if (sz >= 1n << 64n) throw new Error("the size does not fit a stop");
  if (by !== "trader" && by !== "agent") throw new Error("only the trader or the second key sets a stop");
  return call(exam, "setTrigger(uint32,bool,uint64,uint64)", [a, Boolean(isTake), p, sz], by, keccak256);
}

/** `clearTrigger(asset, isTake)`: takes a stop or a take off. The trader or the second key. */
export function clearTriggerCall({ exam, asset, isTake, by = "trader", keccak256 }) {
  const a = assetId(asset);
  if (by !== "trader" && by !== "agent") throw new Error("only the trader or the second key takes a stop off");
  return call(exam, "clearTrigger(uint32,bool)", [a, Boolean(isTake)], by, keccak256);
}

/**
 * `setAgent(agent)`: names a second key that may place orders and pass the step as well as the trader; the zero
 * address takes it back. Only the trader, and never the exam's filler: the contract refuses it (`BadTerms`).
 */
export function setAgentCall({ exam, agent, filler, keccak256 }) {
  if (!ADDRESS.test(String(agent))) throw new Error(`${agent} is not an address`);
  if (filler != null && !ZERO.test(String(filler)) && String(agent).toLowerCase() === String(filler).toLowerCase()) {
    throw new Error("the exam's filler cannot be the second key");
  }
  return call(exam, "setAgent(address)", [agent], "traderOnly", keccak256);
}

/** The index the chain's precompiles take for an asset (CoreOps.pxIndex). */
export function pxIndex(asset) {
  const a = assetId(asset);
  return a >= HIP3_BASE ? a - HIP3_BASE : a;
}

/** The `eth_call` that reads an asset's oracle price: a uint64 in the contract's price units. */
export function oraclePxCall(asset) {
  return { to: ORACLE_PX_PRECOMPILE, data: `0x${pxIndex(asset).toString(16).padStart(64, "0")}` };
}

/** The `eth_call` that reads an asset's best bid and offer; `bboOf` reads its answer. */
export function bboCall(asset) {
  return { to: BBO_PRECOMPILE, data: `0x${pxIndex(asset).toString(16).padStart(64, "0")}` };
}

/** The best bid and offer out of the precompile's answer, or null where it is short (the contract then fills at the oracle). */
export function bboOf(hex) {
  const h = String(hex ?? "").replace(/^0x/, "");
  if (h.length < 128) return null;
  return { bid: BigInt(`0x${h.slice(0, 64)}`), ask: BigInt(`0x${h.slice(64, 128)}`) };
}

// ── units ──────────────────────────────────────────────────────────

function decimalUnits(text, decimals, what) {
  // An asset of more than 6 size decimals has no price decimals left in the contract's units: refused, not sliced.
  if (!Number.isInteger(Number(decimals)) || Number(decimals) < 0) throw new Error(`${what} is not supported for this asset`);
  const t = String(text).trim();
  const m = /^(\d+)(?:\.(\d+))?$/.exec(t);
  if (!m) throw new Error(`${what} is not a plain positive number: ${text}`);
  const frac = m[2] ?? "";
  if (frac.length > decimals && /[1-9]/.test(frac.slice(decimals))) {
    throw new Error(`${what} has at most ${decimals} decimals here`);
  }
  return BigInt(m[1]) * 10n ** BigInt(decimals) + BigInt((frac.slice(0, decimals) || "0").padEnd(decimals, "0") || "0");
}

/** A size typed as a number ("0.015") in the asset's size units, exactly; more decimals than the asset has are refused. */
export function sizeUnits(text, szDecimals) {
  const v = decimalUnits(text, szDecimalsOf(szDecimals), "the size");
  if (v <= 0n) throw new Error("the size must be above zero");
  return v;
}

/** A price as Hyperliquid writes it ("69422.0") in the contract's price units: `6 - szDecimals` decimals. */
export function pxUnits(text, szDecimals) {
  return decimalUnits(text, 6 - szDecimalsOf(szDecimals), "the price");
}

/** An asset's size decimals, or an error where they were not read (null, undefined, empty): `Number(null)` is 0,
 *  which would give a size or a price at the wrong scale (review of #294). */
function szDecimalsOf(szDecimals) {
  if (szDecimals === null || szDecimals === undefined || (typeof szDecimals === "string" && szDecimals.trim() === "")) {
    throw new Error("the asset's size decimals were not read");
  }
  return Number(szDecimals);
}

/** A whole number of units as a decimal string with `decimals` decimals, trailing zeros dropped. */
export function unitsText(units, decimals) {
  const v = BigInt(units);
  const d = Number(decimals);
  const s = abs(v).toString().padStart(d + 1, "0");
  const whole = s.slice(0, s.length - d);
  const frac = d ? s.slice(s.length - d).replace(/0+$/, "") : "";
  return `${v < 0n ? "-" : ""}${whole}${frac ? `.${frac}` : ""}`;
}

export const sizeText = (units, szDecimals) => unitsText(units, szDecimalsOf(szDecimals));
export const pxText = (px, szDecimals) => unitsText(px, 6 - szDecimalsOf(szDecimals));
export const usdText = (units) => unitsText(units, 6);

// ── the account, as the contract counts it ─────────────────────────

function big(v, fallback = 0n) {
  return v === undefined || v === null ? fallback : BigInt(v);
}

function rulesOf(reads) {
  const r = reads.rules ?? {};
  return {
    dailyLossBps: big(r.dailyLossBps ?? r[0]), maxDrawdownBps: big(r.maxDrawdownBps ?? r[1]),
    maxLeverageX100: big(r.maxLeverageX100 ?? r[2]),
    assets: [...(r.assets ?? r[3] ?? [])].map(Number),
    assetLeverageX100: [...(r.assetLeverageX100 ?? r[4] ?? [])].map((x) => BigInt(x)),
  };
}

function termsOf(reads) {
  const t = reads.terms ?? {};
  return {
    capital: big(t.capital ?? t[0]), targetsBps: [...(t.targetsBps ?? t[1] ?? [])].map((x) => BigInt(x)),
    stepDuration: Number(t.stepDuration ?? t[2] ?? 0), maxIdle: Number(t.maxIdle ?? t[3] ?? 0),
    minTradingDays: Number(t.minTradingDays ?? t[4] ?? 0), costPpm: big(t.costPpm ?? t[5]),
    fillDelay: Number(t.fillDelay ?? t[6] ?? 0), fillWindow: Number(t.fillWindow ?? t[7] ?? 0),
  };
}

/** A drawdown kind as the contract holds it, or an error where it was not read (null, undefined, empty) or is none. */
function drawdownKindOf(kind) {
  if (kind === null || kind === undefined || (typeof kind === "string" && kind.trim() === "")) {
    throw new Error("the exam's drawdown kind was not read");
  }
  const k = Number(kind);
  if (!Number.isInteger(k) || k < FROM_START || k > TRAILING_TO_START) throw new Error(`${kind} is not a drawdown kind`);
  return k;
}

/**
 * The drawdown line times `BPS`, as `_drawdownLineBps` keeps it, exact: the starting balance less `maxDrawdownBps` of
 * it (`FROM_START`); or the highest daily snapshot of the step less that same amount, never above the starting
 * balance (`TRAILING_TO_START`). `peak` is `peakSnapshot()`: only a day's snapshot raises it, never a rise within a day.
 */
export function drawdownLineBps({ kind, capital, maxDrawdownBps, peak }) {
  const base = BigInt(capital);
  const dd = BigInt(maxDrawdownBps);
  if (drawdownKindOf(kind) === FROM_START) return base * (BPS - dd);
  if (peak === null || peak === undefined) throw new Error("the step's highest snapshot was not read");
  const trailing = BigInt(peak) * BPS - base * dd;
  return trailing < base * BPS ? trailing : base * BPS;
}

/** `drawdownLine()`: the lowest equity that keeps the drawdown rule, in whole units, rounded up as the contract rounds it. */
export function drawdownLine(args) {
  return (drawdownLineBps(args) + BPS - 1n) / BPS;
}

/** The drawdown line of these reads: the one the contract answered, or worked out from the kind and the snapshot's peak. */
function drawdownOf(reads, rules, capital) {
  const peak = reads.peakSnapshot === undefined || reads.peakSnapshot === null ? null : BigInt(reads.peakSnapshot);
  if (reads.drawdownLine != null) {
    return { at: BigInt(reads.drawdownLine), kind: reads.drawdownKind == null ? null : drawdownKindOf(reads.drawdownKind), peak };
  }
  const kind = drawdownKindOf(reads.drawdownKind);
  return { at: drawdownLine({ kind, capital, maxDrawdownBps: rules.maxDrawdownBps, peak }), kind, peak };
}

/**
 * Positions by asset: {size, mark, stop, take}; a mark is the oracle price read for that asset, null where it was not
 * read. `stop` and `take` are what `stopOf(asset)` and `takeOf(asset)` answered ({px, sz, armedAt, seenAt} or the
 * tuple), null where there is none (a price of zero).
 */
function positionsOf(reads) {
  const out = new Map();
  for (const p of reads.positions ?? []) {
    // A mark of zero is no price: the contract's `_mark` reverts (NoPrice) rather than count a position at nothing,
    // which would leave a short's proceeds in equity (review of #294).
    const mark = p.mark === undefined || p.mark === null ? null : BigInt(p.mark);
    out.set(Number(p.asset), { size: big(p.size), mark: mark === null || mark <= 0n ? null : mark,
      stop: triggerOf(p.stop), take: triggerOf(p.take) });
  }
  return out;
}

/** A stop or a take as `stopOf`/`takeOf` answer it, or null where there is none (`px` zero, or not read). */
function triggerOf(raw) {
  if (raw === undefined || raw === null) return null;
  const px = big(raw.px ?? raw[0]);
  if (px === 0n) return null;
  const seenAt = Number(raw.seenAt ?? raw[3] ?? 0);
  return { px, sz: big(raw.sz ?? raw[1]), armedAt: Number(raw.armedAt ?? raw[2] ?? 0), seenAt: seenAt === 0 ? null : seenAt };
}

/** Equity and open notional at the marks given, as `_book` adds them; null where an open position has no mark. */
function book(cash, positions) {
  let equity = cash;
  let notional = 0n;
  for (const { size, mark } of positions.values()) {
    if (size === 0n) continue;
    if (mark === null) return { equity: null, notional: null };
    equity += size * mark;
    notional += abs(size) * mark;
  }
  return { equity, notional };
}

/**
 * Which rule these numbers break, in the contract's own order (`_judge`), by its name in BREACH. `drawdownAt` is the
 * drawdown line in whole units: the contract's `equity * BPS < line * BPS` and `equity < ceil(line)` are the same for
 * a whole equity, so the line the contract answers (`drawdownLine()`) is judged as it is.
 */
function judge({ equity, notional, positions, rules, drawdownAt, dayStartEquity }) {
  if (equity < drawdownAt) return "drawdown";
  if (dayStartEquity > 0n && equity * BPS < dayStartEquity * (BPS - rules.dailyLossBps)) return "daily-loss";
  if (notional * 100n > equity * rules.maxLeverageX100) return "leverage";
  if (overAssetCap(equity, positions, rules)) return "leverage";
  return "none";
}

function overAssetCap(equity, positions, rules) {
  return rules.assetLeverageX100.some((cap, i) => {
    const p = positions.get(rules.assets[i]);
    return p && p.size !== 0n && abs(p.size) * p.mark * 100n > equity * cap;
  });
}

function over(reads, terms, now) {
  const deadline = Number(reads.deadline ?? 0);
  if (deadline !== 0 && now > deadline) return true;
  return terms.maxIdle !== 0 && now > Number(reads.lastFillAt ?? 0) + terms.maxIdle;
}

/**
 * The order waiting, if any, with its window in blocks: the first block that may fill it (`fillFrom`), the last
 * of its window (`fillUntil`), and where `block` stands. `waiting` before the window, `filling` inside it, `lapsed`
 * past it: a lapsed order costs nothing and is replaced by the next one placed. Where the exam has a filler, an
 * order that only makes a position smaller can be filled by anyone from `fallbackFrom` on.
 */
export function pendingOrder(reads, block) {
  const o = reads.pending ?? {};
  const sz = big(o.sz ?? o[2]);
  if (sz === 0n) return null;
  const terms = termsOf(reads);
  const fillFrom = Number(o.placedBlock ?? o[3]) + terms.fillDelay;
  const fillUntil = fillFrom + terms.fillWindow - 1;
  const b = Number(block);
  const state = b < fillFrom ? "waiting" : b <= fillUntil ? "filling" : "lapsed";
  const hasFiller = reads.filler != null && !ZERO.test(String(reads.filler));
  return { asset: Number(o.asset ?? o[0]), isBuy: Boolean(o.isBuy ?? o[1]), size: sz, fillFrom, fillUntil, state,
    blocksToFill: Math.max(0, fillFrom - b), fallbackFrom: hasFiller ? fillUntil + 1 + FALLBACK_BLOCKS : null };
}

/**
 * The account as the exam counts it, from its getters and the oracle prices of its open positions.
 *
 * `reads`: status, step, cash, dayStartEquity, tradingDays, deadline, lastFillAt, pending, filler, rules, terms, and
 * `positions`: [{asset, size, mark}] for every asset of the rules, `size` from `positionOf(asset)` and `mark` the
 * oracle price read for it (`oraclePxCall`). `now` is the chain's clock, `block` its latest block.
 *
 * `lines` are what the exam is judged by, each with the equity under which it breaks (`at`) and how far the account
 * stands from it (`room`, negative once broken): the drawdown, and the day's loss from the snapshot of the UTC day.
 * `floor` is the nearer of them. The drawdown line is drawn by the exam's `drawdownKind` (`reads.drawdownKind`, with
 * `reads.peakSnapshot`): from the starting balance, or trailing the step's highest day's snapshot to the starting
 * balance (`drawdownLine`); an exam whose kind was not read (and whose line the contract was not asked for) is an
 * error, not a line from the start, which would be the wrong one for a trailing exam: `examAccount` and
 * `orderPreview` throw "the exam's drawdown kind was not read", as a missing asset or size decimals do. A line the contract itself answers is taken as read (`reads.drawdownLine`,
 * `reads.dailyLine`). Each position carries its `stop` and its `take` (`triggerView`).
 */
export function examAccount(reads, { now, block }) {
  const rules = rulesOf(reads);
  const terms = termsOf(reads);
  const status = STATUS[Number(reads.status ?? 0)] ?? "none";
  const step = Number(reads.step ?? 0);
  const cash = big(reads.cash);
  const positions = positionsOf(reads);
  const { equity, notional } = book(cash, positions);
  const priced = equity !== null;
  const dayStartEquity = big(reads.dayStartEquity);
  const capital = terms.capital;

  const lines = [];
  const drawdown = drawdownOf(reads, rules, capital);
  const drawdownAt = drawdown.at;
  lines.push({ kind: "drawdown", at: drawdownAt, room: priced ? equity - drawdownAt : null,
    drawdownKind: drawdown.kind === null ? null : DRAWDOWN_KINDS[drawdown.kind], peak: drawdown.peak });
  if (reads.dailyLine != null || dayStartEquity > 0n) {
    const dailyAt = reads.dailyLine != null ? BigInt(reads.dailyLine) : ceilDiv(dayStartEquity * (BPS - rules.dailyLossBps), BPS);
    lines.push({ kind: "daily-loss", at: dailyAt, room: priced ? equity - dailyAt : null, from: dayStartEquity });
  }
  const floor = lines.reduce((a, b) => (b.at > a.at ? b : a));

  const targetBps = terms.targetsBps[step] ?? null;
  const targetAt = targetBps === null ? null : capital + (capital * targetBps) / BPS;
  const target = targetAt === null ? null : { at: targetAt, toGo: priced ? targetAt - equity : null, step, steps: terms.targetsBps.length };

  const breach = priced ? judge({ equity, notional, positions, rules, drawdownAt, dayStartEquity }) : null;
  const deadline = Number(reads.deadline ?? 0) || null;
  const idleUntil = terms.maxIdle ? Number(reads.lastFillAt ?? 0) + terms.maxIdle : null;
  const endsAt = deadline !== null && idleUntil !== null ? Math.min(deadline, idleUntil) : deadline ?? idleUntil;
  const isOver = over(reads, terms, Number(now));
  const pending = pendingOrder(reads, block);
  const active = status === "active";

  // `graduate`, checked in the contract's own order; the first thing in its way is said.
  let cannot = null;
  if (!active) cannot = "BadStatus";
  else if (isOver) cannot = "TooLate";
  else if (!priced) cannot = "NoPrice";
  else if (breach !== "none") cannot = "RuleBroken";
  else if (notional !== 0n) cannot = "NotFlat";
  else if (targetAt !== null && equity < targetAt) cannot = "TargetNotMet";
  else if (Number(reads.tradingDays ?? 0) < terms.minTradingDays) cannot = "NotEnoughDays";

  return {
    status, step, capital, cash, equity, notional, priced,
    positions: [...positions.entries()].filter(([, p]) => p.size !== 0n).map(([asset, p]) => ({
      asset, size: p.size, side: p.size > 0n ? "long" : "short", mark: p.mark,
      stop: triggerView(p.stop, false, p, reads, block), take: triggerView(p.take, true, p, reads, block),
      notional: p.mark === null ? null : abs(p.size) * p.mark,
      leverageX100: p.mark === null || !priced || equity <= 0n ? null : (abs(p.size) * p.mark * 100n) / equity,
      capX100: rules.assetLeverageX100[rules.assets.indexOf(asset)] ?? rules.maxLeverageX100,
    })),
    leverageX100: priced && equity > 0n ? (notional * 100n) / equity : null,
    maxLeverageX100: rules.maxLeverageX100,
    lines, floor, target, breach, drawdownKind: drawdown.kind === null ? null : DRAWDOWN_KINDS[drawdown.kind],
    peakSnapshot: drawdown.peak,
    days: { traded: Number(reads.tradingDays ?? 0), needed: terms.minTradingDays },
    time: { deadline, idleUntil, endsAt, over: isOver },
    pending,
    canPlace: active && !isOver && (pending === null || pending.state === "lapsed"),
    graduate: { can: cannot === null, refusal: cannot, text: cannot === null ? null : refusalText(cannot) },
    assets: rules.assets, costPpm: terms.costPpm, fillDelay: terms.fillDelay, fillWindow: terms.fillWindow,
  };
}

// ── an order, before it is sent ────────────────────────────────────

/**
 * The price the contract would fill at with these prices (`_fillPrice`): the oracle's; or, where the exam fills at
 * the book, the trader's side of the best bid and offer, never better for the trader than the oracle. `waits` is
 * true where the book stands more than `maxWorseBps` from the oracle: the fill is refused in such a block and
 * waits for a later one of its window. A book not read, empty on a side or crossed leaves the oracle's.
 */
export function fillPrice({ isBuy, oracle, bid, ask, fromBook, maxWorseBps }) {
  const o = BigInt(oracle);
  if (!fromBook) return { px: o, source: "oracle", waits: false };
  if (bid == null || ask == null) return { px: o, source: "oracle-fallback", waits: false };
  const b = BigInt(bid);
  const a = BigInt(ask);
  if (b === 0n || a < b) return { px: o, source: "oracle-fallback", waits: false };
  const px = isBuy ? (a > o ? a : o) : (b < o ? b : o);
  const worse = isBuy ? px - o : o - px;
  return { px, source: "book", waits: worse * BPS > o * BigInt(maxWorseBps ?? 0) };
}

/**
 * What an order would do, worked out before it is sent, as `place` and `fill` would: an estimate, said as one
 * (ESTIMATE_NOTE), since the fill takes the price of a later block.
 *
 * `refusal` names what `place` would refuse now (the status, the time, the asset, the size, an order still
 * waiting); with one, nothing else is worked out. Otherwise: the price and its source, the notional, the cost, the
 * position and the account after the fill, and `atFill`, what the fill itself would refuse at these prices:
 * `OverLeverage` for an order that takes the account over its leverage (one that only makes a position smaller
 * never is), or `TooFarFromOracle` while the book stands off the oracle. `oracle`, `bid` and `ask` are the order's
 * asset's, in the contract's price units.
 */
export function orderPreview(reads, { asset, isBuy, size, oracle, bid, ask, now, block }) {
  const rules = rulesOf(reads);
  const terms = termsOf(reads);
  const a = asset === null || asset === undefined || asset === "" ? NaN : Number(asset);  // a missing asset is none
  const no = (name) => ({ refusal: name, text: refusalText(name), note: ESTIMATE_NOTE });
  if ((STATUS[Number(reads.status ?? 0)] ?? "none") !== "active") return no("BadStatus");
  if (over(reads, terms, Number(now))) return no("TooLate");
  if (!rules.assets.includes(a)) return no("AssetNotAllowed");
  const sz = big(size);
  if (sz <= 0n) return no("ZeroSize");
  const waiting = pendingOrder(reads, block);
  if (waiting !== null && waiting.state !== "lapsed") return no("OrderPending");
  if (oracle == null || BigInt(oracle) === 0n) return no("NoPrice");

  const { px, source, waits } = fillPrice({ isBuy, oracle, bid, ask, fromBook: Boolean(reads.fromBook), maxWorseBps: reads.maxWorseBps });
  const notional = sz * px;
  const cost = ceilDiv(notional * terms.costPpm, PPM);
  const positions = positionsOf(reads);
  const held = positions.get(a);
  const before = held?.size ?? 0n;
  const delta = isBuy ? sz : -sz;
  const after = before + delta;
  // A position closed or turned round loses its stop and its take (`_apply`).
  const closesPosition = before !== 0n && (after === 0n || (after > 0n) !== (before > 0n));
  positions.set(a, { size: after, mark: BigInt(oracle) });
  const cash = big(reads.cash) - delta * px - cost;
  const { equity, notional: open } = book(cash, positions);
  const reduces = (before > 0n) !== (delta > 0n) && abs(delta) <= abs(before);

  // `fill` takes the price first (TooFarFromOracle ends it there), and reads the book only for an order that makes the
  // exposure larger: a reduction is filled whatever another asset's price is, or the book's (review of #294).
  let atFill = waits ? "TooFarFromOracle" : null;
  if (abs(after) > abs(before)) {
    if (equity === null) atFill = atFill ?? "NoPrice";
    else if (equity <= 0n || open * 100n > equity * rules.maxLeverageX100 || overAssetCap(equity, positions, rules)) {
      atFill = atFill ?? "OverLeverage";
    }
  }
  const capital = terms.capital;
  const breach = equity === null ? null : judge({ equity, notional: open, positions, rules, drawdownAt: drawdownOf(reads, rules, capital).at,
    dayStartEquity: big(reads.dayStartEquity) });
  return {
    refusal: null, text: null, px, source, notional, cost, reduces, countsAsTrade: notional >= MIN_TRADE_NOTIONAL,
    closesPosition, dropsTriggers: closesPosition ? [held.stop && "stop", held.take && "take"].filter(Boolean) : [],
    after: { position: after, cash, equity, notional: open,
      leverageX100: equity !== null && equity > 0n ? (open * 100n) / equity : null, breach },
    atFill, atFillText: atFill === null ? null : refusalText(atFill),
    fillFrom: Number(block) + terms.fillDelay, fillUntil: Number(block) + terms.fillDelay + terms.fillWindow - 1,
    note: ESTIMATE_NOTE,
  };
}

// ── stops and takes ────────────────────────────────────────────────

/**
 * How far the oracle price stands from a trigger, in price units: positive while it has not been reached, zero or
 * negative once it has. A stop on a long and a take on a short are reached at or under the price, a take on a long
 * and a stop on a short at or over it (`_triggered`).
 */
function triggerRoom(t, isTake, size, mark) {
  if (mark === null) return null;
  return isTake === (size > 0n) ? t.px - mark : mark - t.px;
}

/** What `_triggered` would refuse a stop or a take with, in the contract's own order; null where it has triggered. */
function triggerRefusal(t, size, mark, block) {
  if (t === null) return { name: "NoTrigger" };
  if (Number(block) < t.armedAt) return { name: "TooEarly", armedAt: t.armedAt };
  if (size === 0n) return { name: "NoPosition" };
  if (mark === null) return { name: "NoPrice" };
  const room = triggerRoom(t, t.isTake, size, mark);
  if (room > 0n) return { name: "NotTriggered", oracle: mark, triggerPx: t.px };
  return null;
}

/**
 * A position's stop or take as the screen shows it: its price, the size it closes (`closes`, all of the position where
 * its size is zero or more than is open), when it acts (`armedAt`, `fillDelay` after it was set), how far the oracle is
 * from it (`room`), and where it stands: `waiting` before its block, `armed`, `triggered` (the oracle has reached it,
 * nobody has shown it yet), `seen` (shown at `seenAt`), or `unpriced` where the oracle price was not read. `carriedBy`
 * is who may carry it out now: the exam's filler, anyone from `fallbackFrom` on once it was seen, or, with no filler,
 * anyone but the trader and the second key. Null where there is none.
 */
function triggerView(t, isTake, p, reads, block) {
  if (t === null || p.size === 0n) return null;
  const b = Number(block);
  const open = abs(p.size);
  const room = triggerRoom(t, isTake, p.size, p.mark);
  const hasFiller = reads.filler != null && !ZERO.test(String(reads.filler));
  const fallbackFrom = hasFiller && t.seenAt !== null ? t.seenAt + FALLBACK_BLOCKS : null;
  const state = b < t.armedAt ? "waiting" : room === null ? "unpriced" : room > 0n ? "armed" : t.seenAt !== null ? "seen" : "triggered";
  return {
    kind: isTake ? "take" : "stop", px: t.px, sz: t.sz, closes: t.sz === 0n || t.sz > open ? open : t.sz,
    whole: t.sz === 0n || t.sz >= open, armedAt: t.armedAt, blocksToArm: Math.max(0, t.armedAt - b), seenAt: t.seenAt,
    fallbackFrom, room, state,
    carriedBy: !hasFiller ? "anyone-but-trader" : fallbackFrom !== null && b >= fallbackFrom ? "anyone" : "filler",
  };
}

/** The position and the stop or take of an asset; a missing asset (null, undefined, empty) is no position, not asset 0. */
function triggerFor(reads, asset, isTake) {
  const noAsset = asset === null || asset === undefined || (typeof asset === "string" && asset.trim() === "");
  const p = positionsOf(reads).get(noAsset ? NaN : Number(asset));
  const t = p === undefined ? null : isTake ? p.take : p.stop;
  return { p: p ?? { size: 0n, mark: null, stop: null, take: null }, t: t === null ? null : { ...t, isTake: Boolean(isTake) } };
}

/**
 * What setting a stop or a take would do, as `setTrigger` would refuse it, in its order: the exam's status, the step's
 * time, no position in that asset, a price of zero; with none, when it would act (`armedAt`), the size it would close,
 * what it replaces, and `reached`: whether the oracle price is already past it (it would then close the position as
 * soon as it acts: a stop set past the price is no way round the delay).
 */
export function triggerSetPreview(reads, { asset, isTake, px, size = 0n, oracle, now, block }) {
  const terms = termsOf(reads);
  const no = (name) => ({ refusal: name, text: refusalText(name) });
  if ((STATUS[Number(reads.status ?? 0)] ?? "none") !== "active") return no("BadStatus");
  if (over(reads, terms, Number(now))) return no("TooLate");
  const { p, t: existing } = triggerFor(reads, asset, isTake);
  if (p.size === 0n) return no("NoPosition");
  const price = big(px);
  if (price <= 0n) return no("ZeroPrice");
  const mark = oracle == null || BigInt(oracle) <= 0n ? (p.mark ?? null) : BigInt(oracle);
  const open = abs(p.size);
  const sz = big(size);
  const room = triggerRoom({ px: price }, Boolean(isTake), p.size, mark);
  return { refusal: null, text: null, kind: isTake ? "take" : "stop", px: price, sz, closes: sz === 0n || sz > open ? open : sz,
    whole: sz === 0n || sz >= open, armedAt: Number(block) + terms.fillDelay, replaces: existing === null ? null : { px: existing.px, sz: existing.sz },
    reached: room === null ? null : room <= 0n };
}

/**
 * What carrying out a stop or a take would do, worked out as `fillTrigger` works it out: an estimate at the prices
 * given (`oracle`, `bid`, `ask` are the asset's), said as one. `refusal` names what it would be refused with, in the
 * contract's order: the status, the step's time, then no stop or take there, its block not come (`TooEarly`), no
 * position, no price, the oracle not at it yet (`NotTriggered`), and the price of the fill too far from the oracle
 * (`TooFarFromOracle`, where it waits for a later block). Who carries it out is the keeper's concern, not worked out here.
 *
 * Without a refusal: the close is a trade like any fill (`trade`, as `Filled` records it, `late` the blocks since `armedAt`), in the
 * direction that makes the position smaller and never larger than what is open, at the price a fill takes, with its
 * cost; it needs no leverage room. `after` is the account then. The trigger that was carried out is removed, and the
 * other one with it where the position is closed (`clears`); an order waiting on that asset lapses then
 * (`lapsesOrder`). `events` are the contract's, in its order.
 */
export function triggerFill(reads, { asset, isTake, oracle, bid, ask, now, block }) {
  const rules = rulesOf(reads);
  const terms = termsOf(reads);
  const no = (name, extra = {}) => ({ refusal: name, text: refusalText(name), note: ESTIMATE_NOTE, ...extra });
  if ((STATUS[Number(reads.status ?? 0)] ?? "none") !== "active") return no("BadStatus");
  if (over(reads, terms, Number(now))) return no("TooLate");
  const { p, t } = triggerFor(reads, asset, isTake);
  const mark = oracle == null || BigInt(oracle) <= 0n ? null : BigInt(oracle);
  const refused = triggerRefusal(t, p.size, mark, block);
  if (refused !== null) {
    const { name, ...detail } = refused;
    return no(name, detail);
  }
  const a = Number(asset);
  const before = p.size;
  const isBuy = before < 0n;
  const { px, source, waits } = fillPrice({ isBuy, oracle: mark, bid, ask, fromBook: Boolean(reads.fromBook), maxWorseBps: reads.maxWorseBps });
  if (waits) return no("TooFarFromOracle", { oracle: mark, px });
  const open = abs(before);
  const sz = t.sz === 0n || t.sz > open ? open : t.sz;
  const notional = sz * px;
  const cost = ceilDiv(notional * terms.costPpm, PPM);
  const delta = isBuy ? sz : -sz;
  const after = before + delta;
  const positions = positionsOf(reads);
  positions.set(a, { ...p, size: after, mark });
  const cash = big(reads.cash) - delta * px - cost;
  const { equity, notional: openAfter } = book(cash, positions);
  const capital = terms.capital;
  const other = isTake ? p.stop : p.take;
  const closed = after === 0n;
  const waiting = pendingOrder(reads, block);
  const lapsesOrder = closed && waiting !== null && waiting.asset === a;
  const breach = equity === null ? null : judge({ equity, notional: openAfter, positions, rules,
    drawdownAt: drawdownOf(reads, rules, capital).at, dayStartEquity: big(reads.dayStartEquity) });
  return {
    refusal: null, text: null, isTake: Boolean(isTake), triggerPx: t.px,
    trade: { asset: a, isBuy, size: sz, px, source, notional, cost, countsAsTrade: notional >= MIN_TRADE_NOTIONAL, late: Number(block) - Number(t.armedAt) },
    after: { position: after, cash, equity, notional: openAfter,
      leverageX100: equity !== null && equity > 0n ? (openAfter * 100n) / equity : null, breach },
    clears: { fired: true, other: closed && other !== null ? (isTake ? "stop" : "take") : null },
    lapsesOrder,
    events: ["TriggerCleared", ...(closed && other !== null ? ["TriggerCleared"] : []), ...(lapsesOrder ? ["Lapsed"] : []),
      "Filled", "TriggerFilled"],
    note: ESTIMATE_NOTE,
  };
}

const EVENT_FIELDS = Object.freeze({
  Placed: ["asset", "isBuy", "sz", "fillFrom", "fillUntil"],
  Lapsed: ["asset", "isBuy", "sz"],
  Filled: ["asset", "isBuy", "sz", "px", "cost", "late", "source"],
  TriggerSet: ["asset", "isTake", "px", "sz", "armedAt"],
  TriggerCleared: ["asset", "isTake"],
  TriggerSeen: ["asset", "isTake", "oracle", "seenAt"],
  TriggerFilled: ["asset", "isTake", "sz", "px", "cost", "triggerPx", "source"],
});

/** An event's arguments by name, from a decoded log: an object by the names, or a list in the event's order. */
function argsOf(event) {
  const fields = EVENT_FIELDS[event.name];
  const args = event.args ?? {};
  return Object.fromEntries(fields.map((f, i) => [f, args[f] ?? args[i]]));
}

/**
 * The stops, the takes and the trades of an exam, folded out of its recorded events in the order the chain wrote them
 * (`events`: [{name, args}], the logs of `TriggerSet`, `TriggerCleared`, `TriggerSeen`, `TriggerFilled` and `Filled`;
 * others are left out). A stop or a take is set, then removed (by the trader, by a close, or by its own fill) or seen;
 * a carried-out one is a trade like any fill: `TriggerFilled` follows the `Filled` it comes with, and that trade is
 * said to have been a stop or a take (`via`), with the trigger's price. A `TriggerFilled` with no such `Filled` before
 * it is an error: the record is not what the contract writes.
 */
export function triggerLedger(events) {
  const stops = new Map();
  const takes = new Map();
  const trades = [];
  for (const event of events) {
    if (!EVENT_FIELDS[event.name] || event.name === "Placed" || event.name === "Lapsed") continue;
    const a = argsOf(event);
    const asset = Number(a.asset);
    const side = a.isTake ? takes : stops;
    if (event.name === "TriggerSet") {
      side.set(asset, { asset, px: BigInt(a.px), sz: BigInt(a.sz), armedAt: Number(a.armedAt), seenAt: null });
    } else if (event.name === "TriggerCleared") {
      side.delete(asset);
    } else if (event.name === "TriggerSeen") {
      const t = side.get(asset);
      if (t && t.seenAt === null) t.seenAt = Number(a.seenAt);
    } else if (event.name === "Filled") {
      trades.push({ asset, isBuy: Boolean(a.isBuy), size: BigInt(a.sz), px: BigInt(a.px), cost: BigInt(a.cost), late: Number(a.late),
        source: Number(a.source), via: "order", triggerPx: null });
    } else {
      const last = trades[trades.length - 1];
      if (!last || last.via !== "order" || last.asset !== asset || last.size !== BigInt(a.sz) || last.px !== BigInt(a.px)
        || last.cost !== BigInt(a.cost) || last.source !== Number(a.source)) {
        throw new Error("a TriggerFilled with no Filled before it that matches it");
      }
      last.via = a.isTake ? "take" : "stop";
      last.triggerPx = BigInt(a.triggerPx);
    }
  }
  return { stops: [...stops.values()], takes: [...takes.values()], trades };
}

// ── the contract's refusals, in plain words ────────────────────────

/** Every error of PaperExam a trader's call can meet, with its arguments and what it means for the trader. */
export const REFUSALS = Object.freeze({
  "BadStatus(uint8)": "The exam is over: it takes no more orders.",
  "NotTrader()": "Only the trader of this exam, or the second key they named, can do this.",
  "BadTerms()": "The exam's filler cannot be named as the second key.",
  "TooLate()": "The step's time is over, or it went too long without a trade: nothing more is placed or passed.",
  "AssetNotAllowed(uint32)": "This exam does not trade that asset.",
  "ZeroSize()": "An order needs a size above zero.",
  "OrderPending()": "An order is already waiting. One waits at a time: this one can be placed once it is filled or its window is over.",
  "OverLeverage()": "The order would take the account over its leverage, so it was not filled. An order that only makes a position smaller is always filled.",
  "TooFarFromOracle(uint64,uint64)": "The book stands too far from the oracle price in this block: the order waits for a later block of its window.",
  "NoPrice(uint32)": "The chain gives no price for that asset right now.",
  "RuleBroken(uint8)": "A rule of the exam is broken at this moment's prices: the step cannot be passed.",
  "NotFlat()": "A position is still open. Close every position with orders of your own, then pass the step.",
  "TargetNotMet(int256,int256)": "The account is under the step's target.",
  "NotEnoughDays(uint16,uint16)": "The step needs more days with a trade before it can be passed.",
  "NoOrder()": "No order is waiting.",
  "TooEarly(uint256)": "The order's first block has not come yet.",
  "OrderLapsed()": "The order's window is over: it was not filled and costs nothing. Place it again.",
  "OwnOrder()": "Whoever placed an order does not fill it.",
  "NotFiller()": "Inside its window an order is filled by the exam's filler only.",
  "OutsideCheckpointWindow()": "The day's snapshot is taken in the first minutes of the UTC day only.",
  "AlreadyCheckpointed()": "The day's snapshot is already taken.",
  "NoBreach()": "No rule is broken right now.",
  "NotOver()": "The step's time is not over.",
  "BadRules()": "These rules cannot start an exam.",
  "UnsupportedSizeDecimals(uint32,uint8)": "That asset's size units are not supported.",
  // The stop and the take of a position (setTrigger, clearTrigger, fillTrigger), as `triggerSetPreview` and
  // `triggerFill` refuse them.
  "NoPosition()": "There is no position in that asset to set a stop or a take on.",
  "ZeroPrice()": "A stop or a take needs a price above zero.",
  "NoTrigger()": "There is no stop or take there to remove.",
  "NotTriggered(uint64,uint64)": "The oracle price has not reached the stop or the take yet.",
});

const REFUSAL_BY_NAME = Object.freeze(Object.fromEntries(Object.entries(REFUSALS).map(([sig, text]) => [sig.split("(")[0], text])));

/** The plain words for a refusal by its name ("OverLeverage"); the name itself where it is none of PaperExam's. */
export function refusalText(name) {
  return REFUSAL_BY_NAME[name] ?? name;
}

/**
 * The refusal behind a call's revert data: its name and its plain words, by the error's selector; null where the
 * data is none of PaperExam's errors. `keccak256` takes 0x-hex and returns 0x-hex (ethers.keccak256).
 */
export function refusalOf(data, keccak256) {
  const d = String(data ?? "").toLowerCase();
  if (!/^0x[0-9a-f]{8}/.test(d)) return null;
  return selectorsOf(keccak256).get(d.slice(0, 10)) ?? null;
}

// The error selectors, worked out once for each hash function handed in: 25 hashes are not worth one lookup.
const SELECTORS = new WeakMap();

function selectorsOf(keccak256) {
  if (!SELECTORS.has(keccak256)) {
    const encoder = new TextEncoder();
    SELECTORS.set(keccak256, new Map(Object.entries(REFUSALS).map(([sig, text]) => {
      const hex = `0x${[...encoder.encode(sig)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
      return [keccak256(hex).slice(0, 10).toLowerCase(), { name: sig.split("(")[0], signature: sig, text }];
    })));
  }
  return SELECTORS.get(keccak256);
}

// ── Hyperliquid's open data, for the chart and the book ────────────

/**
 * Which network a deployment is on and where its chart and book come from. Nothing here names a network or an
 * address: the deployment's record says its chain (`chainId`, as deployments/LIVE.json and the records carry it),
 * and the app's settings (`config`, app/config.js, written from spike/hlspike/network.py for the network the app is
 * built for) give that network's info API and name. The data is the deployment's own network or nothing: a page
 * built for one network never draws another's candles beside an exam of this one, since the exam fills and is
 * judged on the prices of its own chain. `rehearsal` is true on a network that is not mainnet: the exam is a run
 * of the deployment, and traders do not trade there.
 */
export function dataSource(deployment, config) {
  const chainId = Number(deployment?.chainId ?? deployment?.chain_id);
  if (!Number.isInteger(chainId)) {
    return { ok: false, reason: "no-chain", text: "The deployment's record names no chain: no chart or book is shown." };
  }
  if (!config?.hlInfo || Number(config.chainId) !== chainId) {
    return { ok: false, reason: "other-network", chainId, text: `This exam is on chain ${chainId}; this page's data is for chain `
      + `${config?.chainId ?? "none"}. No chart or book is shown rather than another network's.` };
  }
  return { ok: true, chainId, info: config.hlInfo, network: config.hyperliquidChain ?? null,
    rehearsal: config.hyperliquidChain !== "Mainnet" };
}

/**
 * Candles out of Hyperliquid's `candleSnapshot` (info API): each with its opening time in seconds and its prices as
 * numbers, oldest first. The prices are the venue's, for the chart; the exam fills and is judged on the chain's
 * oracle price, which the page reads from the chain (`oraclePxCall`).
 */
export function candleRows(raw) {
  if (!Array.isArray(raw)) return [];  // an error answer ({"error": ...}) is no candles
  return raw
    .map((c) => ({ time: Math.floor(Number(c.t) / 1000), open: Number(c.o), high: Number(c.h), low: Number(c.l),
      close: Number(c.c), volume: Number(c.v) }))
    .filter((c) => Number.isFinite(c.time) && [c.open, c.high, c.low, c.close].every((x) => Number.isFinite(x) && x > 0))
    .sort((a, b) => a.time - b.time);
}

/**
 * The book out of Hyperliquid's `l2Book`: `depth` levels a side, bids from the best down and asks from the best up,
 * each with its price and size as written and the size added up from the best; the best of each side, the middle
 * and the spread in bps of the middle. An empty side leaves its best null, and no middle.
 */
export function bookSides(raw, depth = 10) {
  const side = (levels) => {
    let total = 0;
    return (Array.isArray(levels) ? levels : []).slice(0, depth).map((l) => {
      total += Number(l.sz);
      return { px: String(l.px), sz: String(l.sz), total };
    });
  };
  const bids = side(raw?.levels?.[0]);
  const asks = side(raw?.levels?.[1]);
  const bestBid = bids.length ? Number(bids[0].px) : null;
  const bestAsk = asks.length ? Number(asks[0].px) : null;
  const mid = bestBid !== null && bestAsk !== null ? (bestBid + bestAsk) / 2 : null;
  return { time: raw?.time ?? null, bids, asks, bestBid, bestAsk, mid,
    spreadBps: mid ? ((bestAsk - bestBid) / mid) * 10_000 : null };
}
