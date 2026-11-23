// The trader's screen of a paper exam (app/lib/exam.js): the calls it builds, byte for byte as Foundry's
// `cast calldata` writes them; the account as PaperExam counts it; an order worked out before it is sent; the
// contract's refusals in plain words; and the names, each held to src/exam/PaperExam.sol.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  PAPER_EXAM_ABI, STATUS, BREACH, FROM_START, TRAILING_TO_START, DRAWDOWN_KINDS, FALLBACK_BLOCKS, MIN_TRADE_NOTIONAL,
  ORACLE_PX_PRECOMPILE, BBO_PRECOMPILE, SIGNS, ESTIMATE_NOTE, REFUSALS, placeCall, graduateCall, setAgentCall,
  setTriggerCall, clearTriggerCall, sizeFromUsdc, pxIndex, oraclePxCall, bboCall, bboOf, sizeUnits, pxUnits, sizeText, pxText, usdText,
  pendingOrder, examAccount, fillPrice, orderPreview, refusalText, refusalOf, candleRows, bookSides, dataSource,
  drawdownLine, drawdownLineBps, triggerSetPreview, triggerFill, triggerLedger, settleCall, SETTLE_GAS, STOP_BOOK_ABI,
  STOP_BOOK_REFUSALS, inForce, touchFillPrice,
} from "../lib/exam.js";
import { keccak256 } from "./keccak.mjs";

const SRC = readFileSync(new URL("../../src/exam/PaperExam.sol", import.meta.url), "utf8");
const TYPES = readFileSync(new URL("../../src/Types.sol", import.meta.url), "utf8");
// One line, the way the ABI lines are written: no space inside a bracket that a long declaration broke.
const FOLDED = SRC.replace(/\s+/g, " ").replace(/ (calldata|memory)(?= )/g, "").replace(/\( /g, "(").replace(/ \)/g, ")");

const EXAM = "0x00000000000000000000000000000000000000e1";
const FILLER = "0x00000000000000000000000000000000000000f1";
const ZERO = "0x0000000000000000000000000000000000000000";
const W = (hex) => hex.padStart(64, "0");
const USD = 1_000_000n;
const DAY = 86_400;
const BTC = 3;
const ETH = 4;
const SOL = 5;
// BTC at 5 size decimals: a price carries one decimal (60 000.0 is 600 000), 0.1 BTC is 10 000 size units.
const PX = 600_000n;

/** An exam of 10 000 as the first pool sells it: 3 % a day, 6 % in all, 10x (SOL 5x), target 10 %, 30 days. */
function reads(over = {}) {
  return {
    status: 1, step: 0, cash: 10_000n * USD, dayStartEquity: 10_000n * USD, tradingDays: 0, deadline: 30 * DAY,
    drawdownKind: FROM_START, peakSnapshot: 10_000n * USD,
    lastFillAt: 0, pending: { asset: 0, isBuy: false, sz: 0n, placedBlock: 0n }, filler: FILLER, fromBook: true,
    maxWorseBps: 150,
    rules: { dailyLossBps: 300, maxDrawdownBps: 600, maxLeverageX100: 1000, assets: [BTC, ETH, SOL], assetLeverageX100: [1000, 1000, 500] },
    terms: { capital: 10_000n * USD, targetsBps: [1000], stepDuration: 30 * DAY, maxIdle: 0, minTradingDays: 0, costPpm: 450, fillDelay: 1, fillWindow: 10 },
    positions: [{ asset: BTC, size: 0n, mark: PX }, { asset: ETH, size: 0n, mark: 30_000n }, { asset: SOL, size: 0n, mark: 1_500n }],
    ...over,
  };
}
const AT = { now: DAY, block: 1_000 };

// ── the calls, against `cast calldata` ─────────────────────────────

test("the calls are encoded as cast calldata encodes them", () => {
  // cast calldata "place(uint32,bool,uint64)" 3 true 1500
  assert.equal(placeCall({ exam: EXAM, asset: 3, isBuy: true, size: 1500n, keccak256 }).data, `0x5ec3f2d9${W("3")}${W("1")}${W("5dc")}`);
  // cast calldata "place(uint32,bool,uint64)" 110005 false 18446744073709551615
  assert.equal(placeCall({ exam: EXAM, asset: 110005, isBuy: false, size: (1n << 64n) - 1n, keccak256 }).data,
    `0x5ec3f2d9${W("1adb5")}${W("0")}${W("ffffffffffffffff")}`);
  // cast calldata "graduate()"; cast calldata "setAgent(address)" 0x…Ab
  assert.equal(graduateCall({ exam: EXAM, keccak256 }).data, "0xd3618cca");
  assert.equal(setAgentCall({ exam: EXAM, agent: "0x00000000000000000000000000000000000000Ab", keccak256 }).data, `0xbcf685ed${W("ab")}`);
});

test("an order is the trader's or the second key's, with a size the contract takes", () => {
  const c = placeCall({ exam: EXAM, asset: BTC, isBuy: true, size: 10_000n, keccak256 });
  assert.deepEqual([c.to, c.method, c.signer, c.signs], [EXAM, "place", "trader", SIGNS.trader]);
  assert.equal(placeCall({ exam: EXAM, asset: BTC, isBuy: true, size: 1n, by: "agent", keccak256 }).signs, SIGNS.agent);
  assert.throws(() => placeCall({ exam: EXAM, asset: BTC, isBuy: true, size: 0n, keccak256 }), /above zero/);
  assert.throws(() => placeCall({ exam: EXAM, asset: BTC, isBuy: true, size: 1n << 64n, keccak256 }), /does not fit/);
  assert.throws(() => placeCall({ exam: EXAM, asset: -1, isBuy: true, size: 1n, keccak256 }), /not an asset/);
  assert.throws(() => placeCall({ exam: "0x12", asset: BTC, isBuy: true, size: 1n, keccak256 }), /not an exam/);
  assert.throws(() => placeCall({ exam: EXAM, asset: BTC, isBuy: true, size: 1n, by: "filler", keccak256 }), /only the trader/);
});

test("the second key is named by the trader only, taken back with the zero address, and is never the filler", () => {
  const c = setAgentCall({ exam: EXAM, agent: ZERO, filler: FILLER, keccak256 });
  assert.deepEqual([c.method, c.signer, c.data], ["setAgent", "traderOnly", `0xbcf685ed${W("0")}`]);
  assert.throws(() => setAgentCall({ exam: EXAM, agent: FILLER.toUpperCase().replace("0X", "0x"), filler: FILLER, keccak256 }), /filler cannot/);
  assert.throws(() => setAgentCall({ exam: EXAM, agent: "0xab", keccak256 }), /not an address/);
  assert.equal(setAgentCall({ exam: EXAM, agent: ZERO, filler: ZERO, keccak256 }).method, "setAgent", "an exam without a filler");
  assert.match(SRC, /if \(filler != address\(0\) && agent_ == filler\) revert BadTerms\(\);/);
  assert.match(SRC, /function setAgent\(address agent_\) external settled \{\s+if \(msg\.sender != trader\) revert NotTrader\(\);/);
});

// ── the names, by the source ───────────────────────────────────────

test("every line of the ABI is the exam's own, by its source", () => {
  const enums = { Status: "uint8", Breach: "uint8" };
  for (const line of PAPER_EXAM_ABI) {
    if (line.startsWith("event ")) {
      assert.ok(FOLDED.includes(`${line};`), line);
      continue;
    }
    const m = /^function (\w+)\(([^)]*)\)(?: view returns \((.+)\))?$/.exec(line);
    assert.ok(m, `an ABI line this test can read: ${line}`);
    const [, name, params, returns] = m;
    if (FOLDED.includes(`function ${name}(${params})`)) continue;
    if (name === "pending") {
      // The getter of a public struct answers its fields in order.
      assert.ok(FOLDED.includes("Order public pending;"), line);
      const fields = /struct Order \{(.*?)\}/.exec(FOLDED)[1].replace(/\/\/\/[^;]*?(?=uint|bool)/g, "").trim();
      assert.equal(returns, fields.split(";").map((f) => f.trim()).filter(Boolean).join(", "));
      continue;
    }
    if (name === "stopOf" || name === "takeOf") {
      assert.ok(FOLDED.includes(`mapping(uint32 asset => Trigger) public ${name};`), line);
      const fields = /struct Trigger \{(.*?)\}/.exec(FOLDED)[1].replace(/\/\/\/[^;]*?(?=uint)/g, "").trim();
      assert.deepEqual([params, returns], ["uint32 asset", fields.split(";").map((f) => f.trim()).filter(Boolean).join(", ")]);
      continue;
    }
    if (name === "changeOf") {
      // The change asked of a stop or a take: a Trigger, by asset and side.
      assert.ok(FOLDED.includes("mapping(uint32 asset => mapping(bool isTake => Trigger)) public changeOf;"), line);
      const fields = /struct Trigger \{(.*?)\}/.exec(FOLDED)[1].replace(/\/\/\/[^;]*?(?=uint)/g, "").trim();
      assert.deepEqual([params, returns], ["uint32 asset, bool isTake", fields.split(";").map((f) => f.trim()).filter(Boolean).join(", ")]);
      continue;
    }
    if (name === "positionOf") {
      assert.ok(FOLDED.includes("mapping(uint32 asset => int256) public positionOf;"), line);
      assert.deepEqual([params, returns], ["uint32 asset", "int256"]);
      continue;
    }
    // A public variable's getter: of that type, an enum read as its number.
    const declared = new RegExp(`(\\w+) public ${name};`).exec(FOLDED);
    assert.ok(declared, line);
    assert.equal(enums[declared[1]] ?? declared[1], returns, line);
  }
  // `rules()` and `terms()` answer structs: their fields in the contract's order.
  const fieldsOf = (src, name) => [...new RegExp(`struct ${name} \\{(.*?)\\n\\}`, "s").exec(src)[1].matchAll(/^\s*(\w+(?:\[\])?) (\w+);/gm)]
    .map((f) => `${f[1]} ${f[2]}`).join(", ");
  assert.ok(PAPER_EXAM_ABI.includes(`function rules() view returns ((${fieldsOf(TYPES, "Rules")}))`));
  assert.ok(PAPER_EXAM_ABI.includes(`function terms() view returns ((${fieldsOf(SRC, "ExamTerms")}))`));
});

test("the statuses, the breaches and the constants are the contract's", () => {
  const names = (src, name) => new RegExp(`enum ${name} \\{(.*?)\\}`, "s").exec(src)[1].replace(/\/\/\/[^\n]*/g, "").split(",").map((s) => s.trim());
  const kebab = (s) => s.replace(/([a-z])([A-Z])/g, "$1-$2").toLowerCase();
  assert.deepEqual(names(SRC, "Status").map(kebab), [...STATUS]);
  assert.deepEqual(names(TYPES, "Breach").map(kebab), [...BREACH]);
  assert.match(SRC, new RegExp(`uint16 public constant FALLBACK_BLOCKS = ${FALLBACK_BLOCKS};`));
  assert.match(SRC, /uint256 public constant MIN_TRADE_NOTIONAL = 10e6;/);
  assert.equal(MIN_TRADE_NOTIONAL, 10n * USD);
  // The oracle and the book where the contract's own tests put them.
  const t = readFileSync(new URL("../../test/exam/PaperExam.t.sol", import.meta.url), "utf8");
  assert.ok(t.includes(`vm.etch(${ORACLE_PX_PRECOMPILE}, type(OracleFollowsMark).runtimeCode);`));
  assert.ok(t.includes(`BboStub(${BBO_PRECOMPILE}).set(BTC, bid, ask);`), "the bid first, then the ask");
});

test("the chain's prices are asked by the index the precompiles take", () => {
  assert.deepEqual([pxIndex(3), pxIndex(110005)], [3, 10005]);
  assert.deepEqual(oraclePxCall(3), { to: ORACLE_PX_PRECOMPILE, data: `0x${W("3")}` });
  assert.deepEqual(bboCall(110005), { to: BBO_PRECOMPILE, data: `0x${W("2715")}` });
  assert.deepEqual(bboOf(`0x${W("927c0")}${W("927ca")}`), { bid: 600_000n, ask: 600_010n });
  assert.equal(bboOf("0x"), null, "a book that answers short: the contract fills at the oracle");
  assert.match(SRC, /if \(!ok \|\| r\.length < 64\) return \(false, 0\);/);
});

// ── units ──────────────────────────────────────────────────────────

test("a size and a price are whole units, exactly, and a size finer than the asset has is refused", () => {
  assert.equal(sizeUnits("0.015", 5), 1_500n);
  assert.equal(sizeUnits("2", 5), 200_000n);
  assert.equal(sizeUnits("0.10000000", 5), 10_000n, "trailing zeros are no finer");
  assert.throws(() => sizeUnits("0.000001", 5), /at most 5 decimals/);
  assert.throws(() => sizeUnits("0", 5), /above zero/);
  assert.throws(() => sizeUnits("-1", 5), /plain positive/);
  assert.throws(() => sizeUnits("1e3", 5), /plain positive/);
  assert.equal(pxUnits("69422.0", 5), 694_220n);
  assert.equal(pxUnits("151.25", 2), 1_512_500n, "an asset of 2 size decimals carries 4 price decimals");
  assert.deepEqual([sizeText(1_500n, 5), pxText(694_220n, 5), usdText(-2_700_000n), usdText(10_000n * USD)], ["0.015", "69422", "-2.7", "10000"]);
  // 0.1 BTC at 60 000.0 is 6 000 USDC in the contract's units: size times price, nothing else.
  assert.equal(sizeUnits("0.1", 5) * pxUnits("60000", 5), 6_000n * USD);
});

// ── the account ────────────────────────────────────────────────────

test("a flat account: its lines, its target, its time, and why it cannot pass yet", () => {
  const a = examAccount(reads(), AT);
  assert.deepEqual([a.status, a.equity, a.notional, a.leverageX100, a.positions.length], ["active", 10_000n * USD, 0n, 0n, 0]);
  assert.deepEqual(a.lines.map((l) => [l.kind, l.at, l.room]),
    [["drawdown", 9_400n * USD, 600n * USD], ["daily-loss", 9_700n * USD, 300n * USD]]);
  assert.equal(a.floor.kind, "daily-loss", "the nearer line");
  assert.deepEqual([a.target.at, a.target.toGo, a.target.steps], [11_000n * USD, 1_000n * USD, 1]);
  assert.deepEqual(a.time, { deadline: 30 * DAY, idleUntil: null, endsAt: 30 * DAY, over: false });
  assert.deepEqual([a.breach, a.canPlace, a.graduate.can, a.graduate.refusal], ["none", true, false, "TargetNotMet"]);
  assert.equal(a.graduate.text, "The account is under the step's target.");
});

test("a position is counted at the oracle price, with its leverage against its asset's cap", () => {
  // Long 0.1 BTC bought at 60 000 for a cost of 2.70; the oracle now at 60 600.
  const cash = 10_000n * USD - 10_000n * PX - 2_700_000n;
  const a = examAccount(reads({ cash, positions: [{ asset: BTC, size: 10_000n, mark: 606_000n }, { asset: SOL, size: 0n, mark: 1_500n }] }), AT);
  assert.equal(a.equity, 10_000n * USD - 2_700_000n + 60n * USD);
  assert.equal(a.notional, 6_060n * USD);
  assert.deepEqual(a.positions.map((p) => [p.asset, p.side, p.notional, p.leverageX100, p.capX100]), [[BTC, "long", 6_060n * USD, 60n, 1000n]]);
  assert.equal(a.leverageX100, 60n);
  assert.equal(a.graduate.refusal, "NotFlat");
  const short = examAccount(reads({ cash: 10_000n * USD + 100_000n * 1_500n, positions: [{ asset: SOL, size: -100_000n, mark: 1_500n }] }), AT);
  assert.deepEqual(short.positions.map((p) => [p.side, p.capX100]), [["short", 500n]], "SOL at its own cap");
});

test("each line breaks one unit under it, as the contract judges, and the day's line follows the day's snapshot", () => {
  const at = (cash, more = {}) => examAccount(reads({ cash, ...more }), AT);
  assert.equal(at(9_700n * USD).breach, "none", "on the line is not under it");
  assert.equal(at(9_700n * USD - 1n).breach, "daily-loss");
  assert.equal(at(9_700n * USD - 1n).lines[1].room, -1n);
  // The next day's snapshot is taken from the smaller equity: the day's line is now 3 % under 9 700.
  const next = at(9_500n * USD, { dayStartEquity: 9_700n * USD });
  assert.deepEqual([next.lines[1].at, next.lines[1].from, next.floor.kind, next.breach], [9_409n * USD, 9_700n * USD, "daily-loss", "none"]);
  assert.equal(at(9_400n * USD, { dayStartEquity: 9_600n * USD }).breach, "none");
  assert.equal(at(9_400n * USD - 1n, { dayStartEquity: 9_600n * USD }).breach, "drawdown");
  assert.equal(at(9_400n * USD - 1n, { dayStartEquity: 0n }).lines.length, 1, "no snapshot, no day's line");
  // The drawdown line is `_drawdownLineBps`: a fixed line from the starting balance (`FROM_START`) or one that trails the
  // best day's snapshot (`TRAILING_TO_START`); the trailing kind is the next tests'.
  assert.match(SRC, /if \(eq \* bps < _drawdownLineBps\(\)\) return Breach\.Drawdown;/);
  assert.match(SRC, /if \(drawdownKind == FROM_START\) return base \* \(bps - dd\);/);
  assert.match(SRC, /if \(eq \* bps < dayStartEquity \* \(bps - int256\(uint256\(_rules\.dailyLossBps\)\)\)\) return Breach\.DailyLoss;/);
});

test("a line the contract itself answers is shown as read, not worked out from a constant", () => {
  const a = examAccount(reads({ cash: 10_300n * USD, drawdownLine: 9_900n * USD, dailyLine: 9_800n * USD }), AT);
  assert.deepEqual(a.lines.map((l) => [l.kind, l.at, l.room]), [["drawdown", 9_900n * USD, 400n * USD], ["daily-loss", 9_800n * USD, 500n * USD]]);
  assert.equal(a.floor.kind, "drawdown");
});

test("leverage over the account's cap or an asset's own is a breach", () => {
  const big = (asset, size, mark) => examAccount(reads({ cash: 10_000n * USD - size * mark, positions: [{ asset, size, mark }] }), AT);
  assert.equal(big(BTC, 166_666n, PX).breach, "none", "99 999.6 of notional on 10 000");
  assert.equal(big(BTC, 166_667n, PX).breach, "leverage");
  assert.equal(big(SOL, 33_333_333n, 1_500n).breach, "none", "SOL at 5x: 49 999.9995 of notional");
  assert.equal(big(SOL, 33_333_334n, 1_500n).breach, "leverage");
  const two = examAccount(reads({ cash: 10_000n * USD - 100_000n * PX - 2_000_000n * 30_000n,
    positions: [{ asset: BTC, size: 100_000n, mark: PX }, { asset: ETH, size: 2_000_000n, mark: 30_000n }] }), AT);
  assert.equal(two.breach, "leverage", "6x in each of two assets is 12x on the account");
});

test("a position whose price was not read leaves the account unpriced, and nothing is guessed", () => {
  const a = examAccount(reads({ positions: [{ asset: BTC, size: 10_000n, mark: null }] }), AT);
  assert.deepEqual([a.priced, a.equity, a.leverageX100, a.breach, a.lines[0].room, a.target.toGo], [false, null, null, null, null, null]);
  assert.equal(a.graduate.refusal, "NoPrice");
});

test("the step's time: its deadline, the longest stretch without a trade, and what is over", () => {
  const idle = examAccount(reads({ terms: { ...reads().terms, maxIdle: 7 * DAY }, lastFillAt: 2 * DAY }), { now: 9 * DAY, block: 1 });
  assert.deepEqual(idle.time, { deadline: 30 * DAY, idleUntil: 9 * DAY, endsAt: 9 * DAY, over: false });
  const late = examAccount(reads({ terms: { ...reads().terms, maxIdle: 7 * DAY }, lastFillAt: 2 * DAY }), { now: 9 * DAY + 1, block: 1 });
  assert.deepEqual([late.time.over, late.canPlace, late.graduate.refusal], [true, false, "TooLate"]);
  assert.equal(examAccount(reads(), { now: 30 * DAY + 1, block: 1 }).time.over, true);
  assert.equal(examAccount(reads({ deadline: 0 }), { now: 99 * DAY, block: 1 }).time.endsAt, null, "no deadline and no idle limit");
});

test("the order waiting: its window in blocks, and when another can be placed", () => {
  const r = reads({ pending: { asset: BTC, isBuy: true, sz: 10_000n, placedBlock: 1_000n } });
  assert.equal(pendingOrder(reads(), 1_000), null);
  assert.deepEqual(pendingOrder(r, 1_000), { asset: BTC, isBuy: true, size: 10_000n, fillFrom: 1_001, fillUntil: 1_010, state: "waiting",
    blocksToFill: 1, fallbackFrom: 1_071, fallbackUntil: 1_130 });
  assert.equal(pendingOrder(r, 1_001).state, "filling");
  assert.equal(pendingOrder(r, 1_010).state, "filling");
  assert.equal(pendingOrder(r, 1_011).state, "lapsed");
  assert.equal(pendingOrder({ ...r, filler: ZERO }, 1_011).fallbackFrom, null, "no filler, no fallback");
  assert.deepEqual([examAccount(r, { now: DAY, block: 1_010 }).canPlace, examAccount(r, { now: DAY, block: 1_011 }).canPlace], [false, true]);
  // The same order read by position, as a tuple.
  assert.equal(pendingOrder({ ...r, pending: [BTC, true, 10_000n, 1_000n] }, 1_005).state, "filling");
  assert.match(SRC, /if \(block\.number < _fillFrom\(old\) \+ _terms\.fillWindow\) revert OrderPending\(\);/);
  assert.match(SRC, /emit Placed\(asset, isBuy, sz, from, from \+ _terms\.fillWindow - 1\);/);
});

test("the step can be passed flat, at the target, with its days traded and no rule broken", () => {
  const ok = examAccount(reads({ cash: 11_000n * USD }), AT);
  assert.deepEqual([ok.graduate.can, ok.graduate.refusal, ok.target.toGo], [true, null, 0n]);
  assert.equal(examAccount(reads({ cash: 11_000n * USD - 1n }), AT).graduate.refusal, "TargetNotMet");
  const days = examAccount(reads({ cash: 11_000n * USD, tradingDays: 2, terms: { ...reads().terms, minTradingDays: 3 } }), AT);
  assert.deepEqual([days.graduate.refusal, days.days], ["NotEnoughDays", { traded: 2, needed: 3 }]);
  assert.equal(examAccount(reads({ cash: 11_000n * USD, status: 2 }), AT).graduate.refusal, "BadStatus");
  assert.equal(examAccount(reads({ cash: 9_000n * USD }), AT).graduate.refusal, "RuleBroken");
  // The target of the second step of two, on the same capital.
  const two = examAccount(reads({ step: 1, terms: { ...reads().terms, targetsBps: [800, 500] } }), AT);
  assert.deepEqual([two.target.at, two.target.step, two.target.steps], [10_500n * USD, 1, 2]);
});

// ── an order, before it is sent ────────────────────────────────────

test("the price a fill takes: the oracle's, or the trader's side of the book, never better than the oracle", () => {
  const p = (over) => fillPrice({ isBuy: true, oracle: PX, bid: 599_900n, ask: 600_100n, fromBook: true, maxWorseBps: 150, ...over });
  assert.deepEqual(p({ fromBook: false }), { px: PX, source: "oracle", waits: false });
  assert.deepEqual(p({}), { px: 600_100n, source: "book", waits: false });
  assert.deepEqual(p({ ask: 599_950n, bid: 599_900n }), { px: PX, source: "book", waits: false }, "an ask under the oracle buys at the oracle");
  assert.deepEqual(p({ isBuy: false }), { px: 599_900n, source: "book", waits: false });
  assert.deepEqual(p({ isBuy: false, bid: 600_050n, ask: 600_100n }), { px: PX, source: "book", waits: false }, "a bid over the oracle sells at the oracle");
  assert.equal(p({ bid: 0n }).source, "oracle-fallback", "an empty side");
  assert.equal(p({ bid: 600_100n, ask: 600_000n }).source, "oracle-fallback", "a crossed book");
  assert.equal(p({ bid: null, ask: null }).source, "oracle-fallback", "a book not read");
  assert.equal(p({ ask: 609_000n }).waits, false, "150 bps worse exactly is still filled");
  assert.equal(p({ ask: 609_001n }).waits, true);
  assert.match(SRC, /return \(true, isBuy \? \(ask > oracle \? ask - oracle : 0\) : \(bid < oracle \? oracle - bid : 0\)\);/);
  assert.match(SRC, /if \(gap \* Units\.BPS > uint256\(oracle\) \* maxWorseBps\) \{\s+if \(book == ORDER\) revert TooFarFromOracle\(px, oracle\);/);
});

test("an order worked out before it is sent: its price, its cost, and the account after it", () => {
  const got = orderPreview(reads(), { asset: BTC, isBuy: true, size: 10_000n, oracle: PX, bid: 599_900n, ask: 600_100n, ...AT });
  assert.deepEqual([got.refusal, got.px, got.source, got.notional, got.cost], [null, 600_100n, "book", 6_001n * USD, 2_700_450n]);
  assert.deepEqual(got.after, { position: 10_000n, cash: 10_000n * USD - 6_001n * USD - 2_700_450n, equity: 10_000n * USD - USD - 2_700_450n,
    notional: 6_000n * USD, leverageX100: 60n, breach: "none" });
  assert.deepEqual([got.atFill, got.reduces, got.countsAsTrade, got.fillFrom, got.fillUntil, got.note], [null, false, true, 1_001, 1_010, ESTIMATE_NOTE]);
  // The cost is rounded up, as the contract rounds it: 1.800003 USDC of notional at 450 ppm is 810.00135 units.
  const dust = orderPreview(reads({ fromBook: false }), { asset: BTC, isBuy: true, size: 3n, oracle: PX + 1n, ...AT });
  assert.deepEqual([dust.cost, dust.countsAsTrade], [811n, false], "under 10 USDC of notional is no trading day");
  assert.match(SRC, /cost = \(ntl \* _terms\.costPpm \+ PPM - 1\) \/ PPM;/);
});

test("an order that would take the account over its leverage is said before it is sent; one that reduces never is", () => {
  const at = (size, more = {}) => orderPreview(reads({ fromBook: false, ...more }), { asset: BTC, isBuy: true, size, oracle: PX, ...AT });
  // 99 000 of notional costs 44.55, which leaves 9 955.45: ten times that is 99 554.5.
  assert.equal(at(165_000n).atFill, null);
  assert.equal(at(166_000n).atFill, "OverLeverage", "99 600 of notional: over, once its own cost is off the equity");
  const overIt = at(167_000n);
  assert.deepEqual([overIt.atFill, overIt.refusal], ["OverLeverage", null], "placed, then refused at its fill");
  assert.match(overIt.atFillText, /over its leverage/);
  const sol = (size) => orderPreview(reads({ fromBook: false }), { asset: SOL, isBuy: false, size, oracle: 1_500n, ...AT }).atFill;
  assert.deepEqual([sol(33_000_000n), sol(34_000_000n)], [null, "OverLeverage"], "SOL at its own cap of 5x");
  // An account already over (the price moved): closing part of it is never refused, adding to it is.
  const heavy = { cash: 10_000n * USD - 300_000n * PX, positions: [{ asset: BTC, size: 300_000n, mark: PX }] };
  const close = orderPreview(reads({ fromBook: false, ...heavy }), { asset: BTC, isBuy: false, size: 50_000n, oracle: PX, ...AT });
  assert.deepEqual([close.atFill, close.reduces, close.after.position, close.after.breach], [null, true, 250_000n, "leverage"],
    "still over at 15x, and filled all the same");
  assert.equal(orderPreview(reads({ fromBook: false, ...heavy }), { asset: BTC, isBuy: true, size: 1n, oracle: PX, ...AT }).atFill, "OverLeverage");
  // Through flat to the other side is not "only smaller".
  const flip = orderPreview(reads({ fromBook: false, ...heavy }), { asset: BTC, isBuy: false, size: 600_001n, oracle: PX, ...AT });
  assert.deepEqual([flip.reduces, flip.atFill], [false, "OverLeverage"]);
  assert.match(SRC, /if \(eq <= 0 \|\| open \* 100 > uint256\(eq\) \* _rules\.maxLeverageX100 \|\| _overAssetCap\(eq\)\) \{/);
});

test("a book standing off the oracle is said as a wait, not as a fill", () => {
  const got = orderPreview(reads(), { asset: BTC, isBuy: true, size: 10_000n, oracle: PX, bid: 599_000n, ask: 620_000n, ...AT });
  assert.deepEqual([got.atFill, got.px], ["TooFarFromOracle", 620_000n]);
  assert.match(got.atFillText, /waits for a later block/);
});

test("what place itself would refuse is said first, in the contract's order", () => {
  const p = (r, o = {}) => orderPreview(r, { asset: BTC, isBuy: true, size: 10_000n, oracle: PX, ...AT, ...o });
  assert.equal(p(reads({ status: 3 })).refusal, "BadStatus");
  assert.equal(p(reads(), { now: 30 * DAY + 1 }).refusal, "TooLate");
  assert.equal(p(reads(), { asset: 9 }).refusal, "AssetNotAllowed");
  assert.equal(p(reads(), { size: 0n }).refusal, "ZeroSize");
  const waiting = reads({ pending: { asset: ETH, isBuy: false, sz: 5n, placedBlock: 995n } });
  assert.equal(p(waiting, { block: 1_005 }).refusal, "OrderPending");
  assert.equal(p(waiting, { block: 1_006 }).refusal, null, "its window over, the next order replaces it");
  assert.equal(p(reads(), { oracle: null }).refusal, "NoPrice");
  assert.equal(p(reads({ status: 3 })).text, REFUSALS["BadStatus(uint8)"]);
  // `place` checks in this order: who, the time, the asset, the size, the order waiting.
  const body = SRC.slice(SRC.indexOf("function place("), SRC.indexOf("// ── anyone"));
  const order = ["revert NotTrader()", "revert TooLate()", "revert AssetNotAllowed(asset)", "revert ZeroSize()", "revert OrderPending()"];
  assert.deepEqual([...order].sort((a, b) => body.indexOf(a) - body.indexOf(b)), order);
});

// ── the refusals ───────────────────────────────────────────────────

test("every error of the contract has its plain words, by its own signature", () => {
  const enums = { Status: "uint8", Breach: "uint8" };
  const declared = [...SRC.matchAll(/^\s*error (\w+)\(([^)]*)\);/gm)].map(([, name, params]) =>
    `${name}(${params.split(",").map((p) => p.trim().split(" ")[0]).filter(Boolean).map((t) => enums[t] ?? t).join(",")})`);
  assert.deepEqual(Object.keys(REFUSALS).sort(), declared.sort());
  for (const text of Object.values(REFUSALS)) assert.match(text, /^[A-Z].*\.$/, "a sentence a trader reads");
});

test("a revert is read back to its refusal by the error's selector", () => {
  // cast sig "OverLeverage()" and "TargetNotMet(int256,int256)"
  assert.deepEqual(refusalOf("0xc1b46258", keccak256), { name: "OverLeverage", signature: "OverLeverage()", text: REFUSALS["OverLeverage()"] });
  assert.equal(refusalOf(`0x61379ba7${W("1")}${W("2")}`, keccak256).name, "TargetNotMet");
  assert.equal(refusalOf("0xdeadbeef", keccak256), null);
  assert.equal(refusalOf("0x", keccak256), null);
  assert.equal(refusalOf(undefined, keccak256), null);
  assert.equal(refusalText("NotFlat"), REFUSALS["NotFlat()"]);
  assert.equal(refusalText("SomethingElse"), "SomethingElse", "not PaperExam's: the name as it is");
});

// ── Hyperliquid's open data ────────────────────────────────────────

test("candles and the book are read as Hyperliquid's info API answers them", () => {
  // As `candleSnapshot` and `l2Book` answered on 13 November 2026 (the fields used; prices are strings).
  const candles = candleRows([
    { t: 1794578400000, T: 1794579299999, s: "BTC", i: "15m", o: "69414.0", c: "69500.0", h: "69550.0", l: "69400.0", v: "0.1", n: 12 },
    { t: 1794577500000, T: 1794578399999, s: "BTC", i: "15m", o: "69631.0", c: "69414.0", h: "69723.0", l: "69020.0", v: "0.22174", n: 162 },
    { t: 1794576600000, o: "0", c: "1", h: "1", l: "1", v: "0" },
  ]);
  assert.deepEqual(candles, [
    { time: 1794577500, open: 69631, high: 69723, low: 69020, close: 69414, volume: 0.22174 },
    { time: 1794578400, open: 69414, high: 69550, low: 69400, close: 69500, volume: 0.1 },
  ], "oldest first, a candle without a price left out");
  assert.deepEqual(candleRows(undefined), []);
  const book = bookSides({ coin: "BTC", time: 1794578000000, levels: [
    [{ px: "69422.0", sz: "0.00283", n: 1 }, { px: "69420.0", sz: "0.00288", n: 1 }, { px: "69400.0", sz: "1", n: 3 }],
    [{ px: "69443.0", sz: "0.00023", n: 2 }, { px: "69487.0", sz: "0.00036", n: 1 }],
  ] }, 2);
  assert.deepEqual(book.bids, [{ px: "69422.0", sz: "0.00283", total: 0.00283 }, { px: "69420.0", sz: "0.00288", total: 0.00283 + 0.00288 }]);
  assert.deepEqual([book.asks.length, book.bestBid, book.bestAsk, book.mid, book.time], [2, 69422, 69443, 69432.5, 1794578000000]);
  assert.ok(Math.abs(book.spreadBps - (21 / 69432.5) * 10_000) < 1e-9);
  const empty = bookSides({ levels: [[], [{ px: "1", sz: "1" }]] });
  assert.deepEqual([empty.bestBid, empty.bestAsk, empty.mid, empty.spreadBps], [null, 1, null, null]);
  // The book the exam fills at is the chain's own best bid and offer, in the contract's units.
  assert.equal(pxUnits(book.bids[0].px, 5), 694_220n);
});

// ── review of #294 ─────────────────────────────────────────────────

test("an asset that is no asset is refused before it is made a call for the chain, and a size of no decimals is not sliced", () => {
  for (const bad of [-5, 1.5, "x", 2 ** 32]) {
    assert.throws(() => pxIndex(bad), /is not an asset/, String(bad));
    assert.throws(() => oraclePxCall(bad), /is not an asset/);
    assert.throws(() => bboCall(bad), /is not an asset/);
  }
  assert.equal(pxIndex("3"), 3);
  // An asset of 8 size decimals has none left for a price in the contract's units: refused, not a RangeError.
  assert.throws(() => pxUnits("1", 8), /not supported for this asset/);
  assert.throws(() => sizeUnits("1", -1), /not supported for this asset/);
  assert.equal(pxUnits("5", 6), 5n, "6 size decimals: a price of no decimals");
});

test("the error selectors are worked out once for a hash function, and each hash function has its own", () => {
  let calls = 0;
  const counting = (hex) => { calls += 1; return keccak256(hex); };
  assert.equal(refusalOf("0xc1b46258", counting).name, "OverLeverage");
  const first = calls;
  assert.equal(first, new Set([...Object.keys(REFUSALS), ...Object.keys(STOP_BOOK_REFUSALS)]).size);
  assert.equal(refusalOf("0x61379ba7", counting).name, "TargetNotMet");
  assert.equal(refusalOf("0xdeadbeef", counting), null);
  assert.equal(calls, first, "no more hashing for the next reverts");
  assert.equal(refusalOf("0xc1b46258", keccak256).name, "OverLeverage", "another function, its own table");
});

test("an error answer from Hyperliquid is no candles and no book", () => {
  for (const raw of [{ error: "rate limited" }, "Too many requests", null, 5]) {
    assert.deepEqual(candleRows(raw), []);
    const book = bookSides(raw);
    assert.deepEqual([book.bids, book.asks, book.mid], [[], [], null]);
  }
  assert.deepEqual(bookSides({ levels: [{ error: 1 }, "x"] }).bids, [], "levels that are no lists");
});

// ── the network of the data: the deployment's, from the settings, never a constant here ──

const NETWORK_PY = readFileSync(new URL("../../spike/hlspike/network.py", import.meta.url), "utf8");

/** The app's settings for a network, as scripts/app_config.py writes them from spike/hlspike/network.py. */
function settingsOf(name) {
  const block = new RegExp(`^${name.toUpperCase()} = Network\\((.*?)^\\)`, "ms").exec(NETWORK_PY)[1];
  const field = (key) => new RegExp(`${key}=("?)([^",\\n]+)\\1,`).exec(block)[2];
  return { chainId: Number(field("chain_id")), hlInfo: `${field("api_url")}/info`, hyperliquidChain: field("hyperliquid_chain") };
}

test("the data is the deployment's own network: mainnet or the rehearsal's, from the settings the page was built with", () => {
  const testnet = settingsOf("testnet");
  const mainnet = settingsOf("mainnet");
  assert.deepEqual([testnet.chainId, mainnet.chainId], [998, 999]);
  assert.notEqual(testnet.hlInfo, mainnet.hlInfo);
  // A deployment on mainnet, a page built for mainnet: its API, and not a rehearsal.
  assert.deepEqual(dataSource({ chainId: 999 }, mainnet), { ok: true, chainId: 999, info: mainnet.hlInfo, network: "Mainnet", rehearsal: false });
  // The rehearsal on testnet: its API, said to be a rehearsal.
  assert.deepEqual(dataSource({ chainId: 998 }, testnet), { ok: true, chainId: 998, info: testnet.hlInfo, network: "Testnet", rehearsal: true });
  // A record as the deploy writes it (snake case) reads the same.
  assert.equal(dataSource({ chain_id: 999 }, mainnet).ok, true);
  // Never the other network's data beside an exam of this one, in either direction.
  for (const [deployment, config] of [[{ chainId: 999 }, testnet], [{ chainId: 998 }, mainnet]]) {
    const got = dataSource(deployment, config);
    assert.deepEqual([got.ok, got.reason, got.info], [false, "other-network", undefined]);
    assert.match(got.text, /No chart or book is shown rather than another network's/);
  }
  assert.equal(dataSource({}, mainnet).reason, "no-chain", "a record without its chain");
  assert.equal(dataSource({ chainId: 999 }, {}).ok, false, "settings without an API");
  assert.equal(dataSource({ chainId: 999 }, undefined).ok, false);
});

test("no Hyperliquid address is written in the screen's library or in its reads: they come from the settings", () => {
  for (const file of ["../lib/exam.js", "../lib/hl.js"]) {
    const src = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.ok(!/https?:\/\/[^\s"'`]*hyperliquid/i.test(src), `${file} names a Hyperliquid host`);
    assert.ok(!/(?<![\w.])(998|999)(?![\w.])/.test(src.replace(/\/\/.*$/gm, "")), `${file} names a chain id`);
  }
  assert.match(readFileSync(new URL("../lib/hl.js", import.meta.url), "utf8"), /fetch\(CONFIG\.hlInfo,/);
  // The precompiles are the same on both networks (spike/hlspike/network.py), so one address serves both.
  assert.match(NETWORK_PY, /the precompiles are the same on both networks/);
});

// ── review of #294, second round ───────────────────────────────────

test("a missing asset is no asset, where Number would make it asset 0; an explicit 0 is one", () => {
  for (const missing of [null, undefined, "", "  "]) {
    assert.throws(() => placeCall({ exam: EXAM, asset: missing, isBuy: true, size: 1n, keccak256 }), /is not an asset/, String(missing));
    assert.throws(() => pxIndex(missing), /is not an asset/);
    assert.throws(() => oraclePxCall(missing), /is not an asset/);
    assert.equal(orderPreview(reads(), { asset: missing, isBuy: true, size: 1n, oracle: PX, ...AT }).refusal, "AssetNotAllowed");
  }
  assert.equal(placeCall({ exam: EXAM, asset: 0, isBuy: true, size: 1n, keccak256 }).args[0], 0);
  assert.equal(placeCall({ exam: EXAM, asset: "0", isBuy: true, size: 1n, keccak256 }).args[0], 0);
  assert.equal(pxIndex(0), 0);
  const zero = reads({ rules: { ...reads().rules, assets: [0, BTC] } });
  assert.equal(orderPreview(zero, { asset: 0, isBuy: true, size: 1n, oracle: PX, ...AT }).refusal, null, "asset 0 named, and allowed");
  for (const missing of [null, undefined, ""]) {
    assert.equal(orderPreview(zero, { asset: missing, isBuy: true, size: 1n, oracle: PX, ...AT }).refusal, "AssetNotAllowed",
      "an exam that trades asset 0 is not ordered in by a missing one");
  }
});

test("size decimals that were not read are refused, not taken for none", () => {
  for (const missing of [null, undefined, ""]) {
    assert.throws(() => sizeUnits("1", missing), /size decimals were not read/);
    assert.throws(() => pxUnits("60000", missing), /size decimals were not read/);
    assert.throws(() => sizeText(100_000n, missing), /size decimals were not read/);
    assert.throws(() => pxText(600_000n, missing), /size decimals were not read/);
  }
  assert.deepEqual([sizeUnits("1", 0), pxUnits("60000", 0), sizeUnits("1", 5), pxUnits("60000", 5)], [1n, 60_000_000_000n, 100_000n, 600_000n],
    "an asset of no size decimals is a number that was read");
  assert.equal(sizeUnits("1", "5"), 100_000n, "read as text from a list");
});

test("a mark of zero is no price: a flat account is not made of a short counted at nothing", () => {
  const short = (mark) => examAccount(reads({ cash: 10_000n * USD + 100_000n * PX, positions: [{ asset: BTC, size: -100_000n, mark }] }), AT);
  const zero = short(0n);
  assert.deepEqual([zero.priced, zero.equity, zero.graduate.refusal, zero.breach], [false, null, "NoPrice", null]);
  assert.equal(short(PX).priced, true);
  assert.equal(short(-1n).priced, false, "nor a price under zero");
  // Not eligible to pass on proceeds alone: at zero the short's sale would stay in the equity.
  assert.notEqual(zero.graduate.can, true);
});

test("a reduction is not refused for another asset's missing price; an order that adds exposure is", () => {
  const heldBtc = { cash: 10_000n * USD - 100_000n * PX, positions: [{ asset: BTC, size: 100_000n, mark: PX }, { asset: ETH, size: 1_000n, mark: null }] };
  const at = (isBuy, size, asset = BTC) => orderPreview(reads({ fromBook: false, ...heldBtc }), { asset, isBuy, size, oracle: asset === BTC ? PX : 30_000n, ...AT });
  const reduce = at(false, 40_000n);
  assert.deepEqual([reduce.refusal, reduce.atFill, reduce.reduces, reduce.after.equity], [null, null, true, null], "filled; the equity stays unknown");
  const add = at(true, 1n);
  assert.deepEqual([add.refusal, add.atFill], [null, "NoPrice"], "the contract reads the book for it, and the price is not there");
  assert.equal(at(false, 1n, ETH).atFill, null, "closing the unpriced position itself is a reduction too");
  assert.match(SRC, /if \(_abs\(afterFill\) > _abs\(before\)\) \{/);
});

test("when the book is too far and the leverage too large, the price refusal comes first, as in fill", () => {
  // Over 10x on the account's own cap, and an ask 3 % above the oracle with 150 bps allowed.
  const got = orderPreview(reads(), { asset: BTC, isBuy: true, size: 200_000n, oracle: PX, bid: 599_000n, ask: 618_000n, ...AT });
  assert.equal(got.atFill, "TooFarFromOracle");
  const over = orderPreview(reads(), { asset: BTC, isBuy: true, size: 200_000n, oracle: PX, bid: 599_900n, ask: 600_100n, ...AT });
  assert.equal(over.atFill, "OverLeverage", "with the book in range the leverage is what refuses");
  // In the contract the price is read before the exposure check: the function that holds the leverage revert calls
  // `_fillPrice` above it.
  const revert = SRC.indexOf("revert OverLeverage();", SRC.indexOf("_fillPrice("));
  const fn = SRC.slice(SRC.lastIndexOf("function ", revert), revert);
  assert.ok(fn.includes("_fillPrice("), "the price is taken in the function that refuses the leverage, above the refusal");
});

// ── the drawdown line trailing to the start, and stops and takes (the contract's own cases) ──
//
// The numbers are test/exam/PaperExam.t.sol's: a capital of 100, 10 % drawdown, the day's loss out of the way, a
// fill delay of 2 blocks; 100 units bought at a mark of 1 000 000 is 1x long, so the equity is 100 times the mark's
// move. A library that counts these differently from the contract turns one of these tests red.

const C100 = 100n * USD;
const CONTRACT_BLOCK = 500;

/** The contract's `_exam()` after `_fillerTrade(e, isBuy, 100)`: a position of 100 at 1 000 000, cash what it cost. */
function held({ size = 100n, mark = 1_000_000n, stop = null, take = null, filler = FILLER, ...more } = {}) {
  return {
    status: 1, step: 0, cash: C100 - size * 1_000_000n, dayStartEquity: C100, tradingDays: 0, deadline: 7 * DAY, lastFillAt: 0,
    pending: { asset: 0, isBuy: false, sz: 0n, placedBlock: 0n }, filler, fromBook: false, maxWorseBps: 0,
    drawdownKind: FROM_START, peakSnapshot: C100,
    rules: { dailyLossBps: 9999, maxDrawdownBps: 1000, maxLeverageX100: 500, assets: [BTC, ETH], assetLeverageX100: [] },
    terms: { capital: C100, targetsBps: [1000], stepDuration: 7 * DAY, maxIdle: 0, minTradingDays: 0, costPpm: 0, fillDelay: 2, fillWindow: 3 },
    positions: [{ asset: BTC, size, mark, stop, take }, { asset: ETH, size: 0n, mark: 250_000n }],
    ...more,
  };
}
const NOW = { now: DAY, block: CONTRACT_BLOCK };
/** `stopOf`/`takeOf` as the contract answers: px, sz, armedAt. */
const trig = (px, sz = 0n, armedAt = CONTRACT_BLOCK) => ({ px, sz, armedAt });
const BOOK = "0x00000000000000000000000000000000000000b0";

test("the drawdown line trails the highest day's snapshot to the starting balance, and never above it", () => {
  const at = (peak, kind = TRAILING_TO_START) => drawdownLine({ kind, capital: C100, maxDrawdownBps: 1000, peak });
  // test_trailingLine_risesWithTheDaysSnapshots_andStopsAtTheStart: 90 until a higher snapshot, then 94, 97, 100, 100.
  assert.deepEqual([100n, 104n, 107n, 112n, 115n].map((p) => at(p * USD)), [90n, 94n, 97n, 100n, 100n].map((v) => v * USD));
  assert.equal(at(104n * USD, FROM_START), 90n * USD, "the line from the start does not move (test_trailingLine_breaksWhereTheLineFromTheStartWouldNot)");
  assert.equal(at(107n * USD, "0"), 90n * USD, "the kind read as text from a list");
  // The line is kept exact: a fraction of a unit rounds up, as `drawdownLine()` rounds it.
  assert.equal(drawdownLineBps({ kind: 1, capital: C100, maxDrawdownBps: 1000, peak: 104n * USD + 1n }), (104n * USD + 1n) * 10_000n - C100 * 1000n);
  assert.equal(drawdownLine({ kind: 1, capital: C100, maxDrawdownBps: 1000, peak: 104n * USD + 1n }), 94n * USD + 1n);
  assert.equal(drawdownLine({ kind: 1, capital: 10_001n, maxDrawdownBps: 3333, peak: 10_001n }), 6_668n, "10 001 less 3 333 bps of it, rounded up");
  // An exam whose kind or whose snapshot was not read has no line worked out, and one of no kind is no exam.
  for (const missing of [null, undefined, ""]) {
    assert.throws(() => drawdownLine({ kind: missing, capital: C100, maxDrawdownBps: 1000, peak: C100 }), /drawdown kind was not read/);
  }
  assert.throws(() => at(100n * USD, 2), /is not a drawdown kind/);
  assert.throws(() => at(100n * USD, 1.5), /is not a drawdown kind/);
  assert.throws(() => drawdownLine({ kind: 1, capital: C100, maxDrawdownBps: 1000, peak: null }), /highest snapshot was not read/);
  assert.equal(drawdownLine({ kind: 0, capital: C100, maxDrawdownBps: 1000, peak: null }), 90n * USD, "from the start needs no peak");
  assert.deepEqual([FROM_START, TRAILING_TO_START, [...DRAWDOWN_KINDS]], [0, 1, ["from-start", "trailing-to-start"]]);
});

test("the account breaks a trailing line where the line from the start would not, and one unit under the line, not on it", () => {
  const acct = (kind, peak, mark) => examAccount(held({ drawdownKind: kind, peakSnapshot: peak * USD, mark: BigInt(mark) }), NOW);
  // test_trailingLine_breaksWhereTheLineFromTheStartWouldNot: a snapshot of 104, then the mark at 939 000 (equity 93.9).
  const trailing = acct(TRAILING_TO_START, 104n, 939_000);
  const fixed = acct(FROM_START, 104n, 939_000);
  assert.deepEqual([trailing.breach, fixed.breach], ["drawdown", "none"], "under 94, over 90");
  assert.deepEqual([trailing.lines[0].at, trailing.lines[0].drawdownKind, trailing.lines[0].peak, trailing.drawdownKind, trailing.peakSnapshot],
    [94n * USD, "trailing-to-start", 104n * USD, "trailing-to-start", 104n * USD]);
  assert.deepEqual([fixed.lines[0].at, fixed.lines[0].drawdownKind, fixed.floor.kind], [90n * USD, "from-start", "drawdown"]);
  assert.equal(trailing.lines[0].room, 93_900_000n - 94n * USD, "negative once broken");
  // test_trailingLine_risesWithTheDaysSnapshots: a snapshot of 112 puts the line at the start's 100; 100 holds, a unit under breaks.
  assert.equal(acct(TRAILING_TO_START, 112n, 1_000_000).breach, "none");
  assert.equal(acct(TRAILING_TO_START, 112n, 999_999).breach, "drawdown");
  assert.equal(acct(TRAILING_TO_START, 115n, 999_999).lines[0].at, 100n * USD, "and it stays there");
  // The step's first snapshot is its starting balance: the line is the start's until a higher one.
  assert.equal(acct(TRAILING_TO_START, 100n, 900_000).breach, "none");
  assert.equal(acct(TRAILING_TO_START, 100n, 899_999).breach, "drawdown");
  // A rise within the day moves nothing: the peak is the snapshot's, not the equity now (test_trailingLine_aRiseWithinTheDayMovesNothing).
  const noon = acct(TRAILING_TO_START, 100n, 1_080_000);
  assert.deepEqual([noon.equity, noon.lines[0].at], [108n * USD, 90n * USD]);
  // An exam of the trailing kind with no snapshot read, or of no kind read, is not given a line from the start.
  assert.throws(() => examAccount(held({ drawdownKind: TRAILING_TO_START, peakSnapshot: undefined }), NOW), /highest snapshot was not read/);
  assert.throws(() => examAccount(held({ drawdownKind: undefined }), NOW), /drawdown kind was not read/);
  assert.equal(examAccount(held({ drawdownKind: undefined, drawdownLine: 94n * USD }), NOW).lines[0].drawdownKind, null,
    "a line the contract answered needs no kind to be shown");
});

test("the drawdown is judged on the line the contract keeps: equity under it, as `equity * BPS < line` is, at every unit", () => {
  const BPS = 10_000n;
  for (const [kind, peak, dd] of [[1, 104_000_000n, 1000n], [1, 100_000_001n, 333n], [1, 9_999_999n, 1n], [0, 100_000_000n, 1000n], [1, 250_000_000n, 9_999n]]) {
    const exact = drawdownLineBps({ kind, capital: C100, maxDrawdownBps: dd, peak });
    const line = drawdownLine({ kind, capital: C100, maxDrawdownBps: dd, peak });
    for (const equity of [line - 2n, line - 1n, line, line + 1n, line + 2n, 0n, -5n]) {
      const a = examAccount(held({ drawdownKind: kind, peakSnapshot: peak, cash: equity, positions: [], rules: { ...held().rules, maxDrawdownBps: dd } }), NOW);
      assert.equal(a.breach === "drawdown", equity * BPS < exact, `${kind} ${peak} ${dd} ${equity}`);
    }
  }
  assert.match(SRC, /if \(eq \* bps < _drawdownLineBps\(\)\) return Breach\.Drawdown;/);
  assert.match(SRC, /return \(_drawdownLineBps\(\) \+ bps - 1\) \/ bps;/);
  assert.match(SRC, /if \(drawdownKind == FROM_START\) return base \* \(bps - dd\);/);
  assert.match(SRC, /int256 trailing = peakSnapshot \* bps - base \* dd;/);
  assert.match(SRC, /return trailing < base \* bps \? trailing : base \* bps;/);
  assert.match(SRC, /if \(eq > peakSnapshot\) peakSnapshot = eq;/, "only `checkpoint` raises the peak");
  assert.match(SRC, /uint8 public constant FROM_START = 0;\s+uint8 public constant TRAILING_TO_START = 1;/);
  assert.match(SRC, /if \(drawdownKind_ > TRAILING_TO_START\) revert BadTerms\(\);/);
  assert.match(SRC, /peakSnapshot = cash;/, "a new step starts again from its balance");
});

test("an order that closes or turns a position round takes its stop and its take with it; a smaller one does not", () => {
  // test_trigger_goesWithAPositionAnOrderClosesOrTurnsRound: 100 long with a stop and a take.
  const r = held({ stop: trig(950_000n), take: trig(1_100_000n) });
  const sell = (size) => orderPreview(r, { asset: BTC, isBuy: false, size, oracle: 1_000_000n, ...NOW });
  assert.deepEqual([sell(40n).closesPosition, sell(40n).dropsTriggers], [false, []], "smaller: both stay");
  assert.deepEqual([sell(100n).closesPosition, sell(100n).dropsTriggers], [true, ["stop", "take"]], "closed");
  assert.deepEqual([sell(160n).closesPosition, sell(160n).dropsTriggers, sell(160n).after.position], [true, ["stop", "take"], -60n], "turned round");
  const stopOnly = orderPreview(held({ stop: trig(950_000n) }), { asset: BTC, isBuy: false, size: 100n, oracle: 1_000_000n, ...NOW });
  assert.deepEqual(stopOnly.dropsTriggers, ["stop"]);
  assert.deepEqual(orderPreview(held({ size: 0n }), { asset: BTC, isBuy: true, size: 5n, oracle: 1_000_000n, ...NOW }).dropsTriggers, [], "a new position has none");
  assert.match(SRC, /if \(before != 0 && \(afterFill == 0 \|\| \(afterFill > 0\) != \(before > 0\)\)\) \{[^}]*?if \(stopOf\[asset\]\.px != 0\) _clear\(asset, false, book == ORDER\);\s+if \(takeOf\[asset\]\.px != 0\) _clear\(asset, true, book == ORDER\);/);
});

test("a stop and a take as the screen shows them: when they act, how far the oracle is, a touch waiting for its settle, a change asked", () => {
  const view = (more = {}, at = NOW) => examAccount(held(more), at).positions.find((p) => p.asset === BTC);
  const s = view({ stop: trig(950_000n, 0n, 502), take: trig(1_100_000n, 40n, 400) }).stop;
  assert.deepEqual([s.kind, s.px, s.closes, s.whole, s.armedAt, s.blocksToArm, s.state, s.room, s.touch, s.change],
    ["stop", 950_000n, 100n, true, 502, 2, "waiting", 50_000n, null, null]);
  const t = view({ stop: trig(950_000n, 0n, 502), take: trig(1_100_000n, 40n, 400) }).take;
  assert.deepEqual([t.kind, t.closes, t.whole, t.state, t.room], ["take", 40n, false, "armed", 100_000n]);
  assert.equal(view({ stop: trig(950_000n, 0n, 400), mark: 950_000n }).stop.state, "reached", "at the stop is reached");
  assert.equal(view({ stop: trig(950_000n, 0n, 400), mark: null }).stop.state, "unpriced");
  // A touch the book logged and nobody settled yet: filled at its price, waiting for the settle.
  const touches = [{ asset: BTC, isTake: false, level: 950_000n, px: 949_000n, book: 0xffffffff, blk: 498, time: DAY - 2, pos: 7 }];
  const touched = view({ stop: trig(950_000n, 0n, 400), touches }).stop;
  assert.deepEqual([touched.state, touched.touch], ["touched", { px: 949_000n, blk: 498, level: 950_000n }]);
  assert.equal(view({ take: trig(1_100_000n, 0n, 400), touches }).take.state, "armed", "a stop's touch is not the take's");
  // A change asked: until its block the one there acts; from it, the change does, or none.
  const stopChange = { px: 0n, sz: 0n, armedAt: 503 };
  const pending = view({ stop: trig(950_000n, 0n, 400), positions: [{ asset: BTC, size: 100n, mark: 1_000_000n, stop: trig(950_000n, 0n, 400), stopChange }] }).stop;
  assert.deepEqual([pending.px, pending.change], [950_000n, { px: 0n, removes: true, armedAt: 503, blocksToChange: 3 }]);
  const gone = examAccount(held({ positions: [{ asset: BTC, size: 100n, mark: 1_000_000n, stop: trig(950_000n, 0n, 400), stopChange }] }), { now: DAY, block: 503 });
  assert.equal(gone.positions.find((p) => p.asset === BTC).stop, null, "taken off from its block");
  const moveTo = { px: 960_000n, sz: 10n, armedAt: 503 };
  const moved = examAccount(held({ positions: [{ asset: BTC, size: 100n, mark: 1_000_000n, stop: trig(950_000n, 0n, 400), stopChange: moveTo }] }), { now: DAY, block: 503 });
  assert.deepEqual([moved.positions.find((p) => p.asset === BTC).stop.px, moved.positions.find((p) => p.asset === BTC).stop.closes], [960_000n, 10n]);
  assert.equal(inForce(trig(950_000n), moveTo, 502).t.px, 950_000n);
  assert.match(SRC, /if \(c\.armedAt == 0 \|\| c\.armedAt > block\.number\) return t;/);
  assert.match(SRC, /sz = t\.sz == 0 \|\| t\.sz > open \? uint64\(open\) : t\.sz;/);
});

test("a settle carries out a touch as a trade: at the touch's price and book, closing what is open, in fireTrigger's order", () => {
  const touch = { px: 949_000n, book: 0xffffffff, blk: 501, time: DAY - 1 };
  const fill = (more = {}, args = {}) => triggerFill(held({ stop: trig(950_000n, 0n, 500), ...more }), { asset: BTC, isTake: false, touch, ...NOW, ...args });
  const got = fill();
  assert.deepEqual([got.refusal, got.triggerPx, got.touchBlock, got.touchPx, got.trade.isBuy, got.trade.size, got.trade.px, got.trade.source, got.trade.late],
    [null, 950_000n, 501, 949_000n, false, 100n, 949_000n, "oracle", 1]);
  assert.deepEqual([got.after.position, got.clears.other, got.events], [0n, null, ["TriggerCleared", "Filled", "TriggerFilled"]]);
  assert.equal(fill({ take: trig(1_100_000n) }).clears.other, "take", "a close takes the other one with it");
  assert.equal(fill({}, { touch: { ...touch, px: 951_000n } }).refusal, "NotTriggered");
  assert.equal(fill({}, { touch: { ...touch, blk: 499 } }).refusal, "TooEarly", "before its armedAt");
  assert.equal(fill({}, { touch: { ...touch, time: 7 * DAY + 1 } }).refusal, "TooLate", "the exam was over at the touch");
  assert.equal(fill({ size: 0n }).refusal, "NoPosition");
  assert.equal(fill({ stop: null }).refusal, "NoTrigger");
  assert.equal(fill({}, { touch: null }).refusal, "NotTriggered");
  assert.equal(fill({ status: 3 }).refusal, "BadStatus");
  // A change in force at the touch is what the settle carries out.
  const moved = triggerFill(held({ positions: [{ asset: BTC, size: 100n, mark: 1_000_000n, stop: trig(950_000n, 0n, 400),
    stopChange: { px: 960_000n, sz: 40n, armedAt: 501 } }] }), { asset: BTC, isTake: false, touch: { ...touch, px: 959_000n }, ...NOW });
  assert.deepEqual([moved.refusal, moved.triggerPx, moved.trade.size], [null, 960_000n, 40n]);
  const body = SRC.slice(SRC.indexOf("function fireTrigger("), SRC.indexOf("/// @notice Takes the day's snapshot"));
  const order = ["if (msg.sender != stopBook) revert NotBook();", "if (c.px == level && c.armedAt == armedAt) t = c;",
    "if (status != Status.Active || _overAt(touchTime)) {", "_fire(asset, isTake, t, uint256(book) << 64 | px, touchBlock, touchTime);"];
  assert.deepEqual([...order].sort((a, b) => body.indexOf(a) - body.indexOf(b)), order);
});

test("a stop's close takes the book of its touch's block: the trader's side, and past the bound at the bound, never refused", () => {
  const p = (over) => touchFillPrice({ isBuy: false, oracle: 1_000_000n, book: (300 << 16) | 200, fromBook: true, maxWorseBps: 150, ...over });
  assert.deepEqual(p({ fromBook: false }), { px: 1_000_000n, source: "oracle" });
  assert.deepEqual(p({}), { px: 999_800n, source: "book" }, "a sell at the bid, 200 millionths under");
  assert.deepEqual(p({ isBuy: true }), { px: 1_000_300n, source: "book" }, "a buy at the ask");
  assert.deepEqual(p({ book: 0xffffffff }), { px: 1_000_000n, source: "oracle-fallback" });
  assert.deepEqual(p({ book: 60_000 }), { px: 985_000n, source: "book" }, "6 % off: at the bound of 150 bps");
  assert.match(SRC, /gap = uint256\(oracle\) \* \(isBuy \? book >> 16 : uint16\(book\)\) \/ 1e6;/);
  assert.match(SRC, /px = isBuy \? oracle \+ bound : oracle - bound;/);
  const r = held({ stop: trig(950_000n, 0n, 500), fromBook: true, maxWorseBps: 150, terms: { ...held().terms, costPpm: 450 } });
  const got = triggerFill(r, { asset: BTC, isTake: false, touch: { px: 949_000n, book: 200, blk: 501, time: DAY - 1 }, ...NOW });
  assert.deepEqual([got.trade.px, got.trade.cost], [948_811n, (100n * 948_811n * 450n + 999_999n) / 1_000_000n]);
});

test("a close by a stop lapses an order waiting on that asset, and only where the position is closed", () => {
  const touch = { px: 949_000n, book: 0xffffffff, blk: 501, time: DAY - 1 };
  const waiting = { asset: BTC, isBuy: true, sz: 5n, placedBlock: 499n };
  const got = triggerFill(held({ stop: trig(950_000n, 0n, 500), pending: waiting }), { asset: BTC, isTake: false, touch, ...NOW });
  assert.deepEqual([got.lapsesOrder, got.events], [true, ["TriggerCleared", "Lapsed", "Filled", "TriggerFilled"]]);
  const part = triggerFill(held({ stop: trig(950_000n, 40n, 500), pending: waiting }), { asset: BTC, isTake: false, touch, ...NOW });
  assert.equal(part.lapsesOrder, false, "the position is still open");
});

test("what setting a stop would do: refused in setTrigger's order, acting a fill delay later, replacing the one there", () => {
  const set = (more = {}, args = {}) => triggerSetPreview(held(more), { asset: BTC, isTake: false, px: 950_000n, oracle: 1_000_000n, ...NOW, ...args });
  const ok = set();
  assert.deepEqual([ok.refusal, ok.kind, ok.px, ok.armedAt, ok.closes, ok.whole, ok.replaces, ok.reached], [null, "stop", 950_000n, 502, 100n, true, null, false],
    "test_trigger_onlyTheTraderSetsIt_onAnOpenPosition_actingAfterTheDelay: block + fill delay");
  assert.deepEqual([set({}, { isTake: true, px: 1_100_000n, size: 40n }).closes, set({}, { isTake: true, px: 1_100_000n, size: 40n }).whole], [40n, false]);
  assert.equal(set({}, { size: 500n }).closes, 100n);
  assert.deepEqual(set({ stop: trig(900_000n, 10n, 400) }).replaces, { px: 900_000n, sz: 10n });
  assert.equal(set({ take: trig(900_000n, 10n, 400) }).replaces, null, "a stop replaces a stop only");
  assert.equal(set({}, { px: 1_200_000n }).reached, true, "set past the price: it closes as soon as it acts");
  assert.equal(set({}, { px: 1_000_000n }).reached, true);
  assert.equal(set({}, { px: 1_100_000n, isTake: true }).reached, false);
  assert.equal(set({}, { oracle: null }).reached, false, "the position's own mark when the oracle is not given");
  assert.equal(set({ positions: [{ asset: BTC, size: 100n, mark: null }] }, { oracle: null }).reached, null, "no price, not guessed");
  for (const [refusal, more, args] of [
    ["BadStatus", { status: 2 }, {}], ["TooLate", {}, { now: 8 * DAY }], ["NoPosition", { size: 0n }, {}],
    ["NoPosition", {}, { asset: ETH }], ["NoPosition", {}, { asset: null }], ["NoPosition", {}, { asset: "" }], ["ZeroPrice", {}, { px: 0n }],
  ]) {
    assert.equal(set(more, args).refusal, refusal, refusal);
    assert.equal(set(more, args).text, REFUSALS[`${refusal}(${refusal === "BadStatus" ? "uint8" : ""})`]);
  }
  assert.equal(set({ status: 2, size: 0n }, { px: 0n, now: 8 * DAY }).refusal, "BadStatus", "in the contract's order");
  assert.equal(set({ size: 0n }, { px: 0n, now: 8 * DAY }).refusal, "TooLate");
  assert.equal(set({ size: 0n }, { px: 0n }).refusal, "NoPosition");
  // With a stop book: the oracle not read or over the book's highest price, a level already reached, a level over it.
  const B = { stopBook: "0x00000000000000000000000000000000000000b0" };
  assert.equal(set(B).refusal, null);
  assert.equal(set(B, { px: 1_000_000n }).refusal, "WouldTrigger", "set on the price's way, not behind it");
  assert.equal(set(B, { px: 1_100_000n, isTake: true }).refusal, null);
  assert.equal(set(B, { px: 1_000_000n, isTake: true }).refusal, "WouldTrigger");
  assert.equal(set({ ...B, positions: [{ asset: BTC, size: 100n, mark: null }] }, { oracle: null }).refusal, "NoPrice");
  assert.equal(set(B, { oracle: 4_294_967_295n }).refusal, "NoPrice", "over the book's highest price");
  assert.equal(set(B, { oracle: 5_000_000_000n, px: 4_294_967_295n }).refusal, "NoPrice");
  assert.equal(set({ ...B, positions: [{ asset: BTC, size: -100n, mark: 1_000_000n }] }, { px: 4_294_967_295n }).refusal, "LevelTooHigh");
  const moved = set({ ...B, stop: trig(900_000n, 0n, 400) });
  assert.deepEqual([moved.isChange, moved.replaces, moved.armedAt], [true, { px: 900_000n, sz: 0n }, 502], "a change, a fill delay on");
  assert.equal(set({ stop: trig(900_000n, 0n, 400) }).isChange, false, "without a book it replaces at once");
  const body = SRC.slice(SRC.indexOf("function setTrigger("), SRC.indexOf("/// @notice Removes the stop"));
  const order = ["inStatus(Status.Active)", "revert NotTrader()", "revert TooLate()", "revert NoPosition()", "revert ZeroPrice()",
    "uint64 armedAt = uint64(block.number) + _terms.fillDelay;", "if (oracle > MAX_PRICE) revert NoPrice(asset);",
    "revert WouldTrigger(oracle, px);", "IStopBook(b).add(asset, isTake, below, px, armedAt);"];
  assert.deepEqual([...order].sort((a, b) => body.indexOf(a) - body.indexOf(b)), order);
});

test("the calls for a stop and a take are encoded as cast calldata encodes them", () => {
  // cast calldata "setTrigger(uint32,bool,uint64,uint64)" 3 false 950000 0
  const stop = setTriggerCall({ exam: EXAM, asset: BTC, isTake: false, px: 950_000n, keccak256 });
  assert.equal(stop.data, `0x2c1c69ce${W("3")}${W("0")}${W("e7ef0")}${W("0")}`);
  assert.deepEqual([stop.to, stop.method, stop.signer, stop.signs, stop.args], [EXAM, "setTrigger", "trader", SIGNS.trader, [3, false, 950_000n, 0n]]);
  // cast calldata "setTrigger(uint32,bool,uint64,uint64)" 110005 true 1050000 40
  assert.equal(setTriggerCall({ exam: EXAM, asset: 110005, isTake: true, px: 1_050_000n, size: 40n, by: "agent", keccak256 }).data,
    `0x2c1c69ce${W("1adb5")}${W("1")}${W("100590")}${W("28")}`);
  assert.equal(setTriggerCall({ exam: EXAM, asset: 3, isTake: true, px: 1n, by: "agent", keccak256 }).signs, SIGNS.agent);
  // cast calldata "clearTrigger(uint32,bool)" 3 true
  const clear = clearTriggerCall({ exam: EXAM, asset: BTC, isTake: true, keccak256 });
  assert.equal(clear.data, `0x308311df${W("3")}${W("1")}`);
  assert.deepEqual([clear.method, clear.signer], ["clearTrigger", "trader"]);
  assert.throws(() => setTriggerCall({ exam: EXAM, asset: BTC, isTake: false, px: 0n, keccak256 }), /price above zero/);
  assert.throws(() => setTriggerCall({ exam: EXAM, asset: BTC, isTake: false, px: -1n, keccak256 }), /price above zero/);
  assert.throws(() => setTriggerCall({ exam: EXAM, asset: BTC, isTake: false, px: 1n << 64n, keccak256 }), /does not fit/);
  assert.throws(() => setTriggerCall({ exam: EXAM, asset: BTC, isTake: false, px: 1n, size: -1n, keccak256 }), /zero \(all of it\) or more/);
  assert.throws(() => setTriggerCall({ exam: EXAM, asset: BTC, isTake: false, px: 1n, size: 1n << 64n, keccak256 }), /does not fit/);
  assert.throws(() => setTriggerCall({ exam: EXAM, asset: BTC, isTake: false, px: 1n, by: "filler", keccak256 }), /only the trader/);
  assert.throws(() => setTriggerCall({ exam: EXAM, asset: null, isTake: false, px: 1n, keccak256 }), /not an asset/);
  assert.throws(() => clearTriggerCall({ exam: EXAM, asset: -1, isTake: true, keccak256 }), /not an asset/);
  assert.throws(() => clearTriggerCall({ exam: EXAM, asset: 3, isTake: true, by: "x", keccak256 }), /only the trader/);
  assert.match(SRC, /function clearTrigger\(uint32 asset, bool isTake\) external settled \{\s+if \(msg\.sender != trader && msg\.sender != agent\) revert NotTrader\(\);/);
  // cast calldata "settle(address)" 0x…e1, to the stop book, with its fixed gas
  const settle = settleCall({ book: BOOK, exam: EXAM, keccak256 });
  assert.deepEqual([settle.to, settle.method, settle.gas, settle.data], [BOOK, "settle", SETTLE_GAS, `0x6a256b29${W("e1")}`]);
  assert.ok(SETTLE_GAS <= 2_000_000n, "inside a small block");
  assert.throws(() => settleCall({ book: ZERO, exam: EXAM, keccak256 }), /not a stop book/);
  const book = readFileSync(new URL("../../src/sim/StopBook.sol", import.meta.url), "utf8").replace(/\s+/g, " ");
  for (const line of STOP_BOOK_ABI) {
    const name = /^function (\w+)\(/.exec(line)[1];
    assert.ok(book.includes(`function ${name}(`) || book.includes(` public ${name};`), line);
  }
  assert.ok(book.includes("struct Touch { uint32 asset; bool isTake; uint64 level; uint64 px; uint32 book; uint256 blk; uint256 time; uint256 pos; }"));
});

test("the stops, the takes and the trades are folded out of the recorded events; with a book a change waits its block", () => {
  const ev = (name, args, blockNumber = 1) => ({ name, args, blockNumber });
  const plain = triggerLedger([
    ev("TriggerSet", { asset: BTC, isTake: false, px: 950_000n, sz: 0n, armedAt: 502n }),
    ev("TriggerSet", { asset: BTC, isTake: true, px: 1_100_000n, sz: 40n, armedAt: 503n }),
    ev("TriggerSet", { asset: BTC, isTake: false, px: 940_000n, sz: 0n, armedAt: 505n }, 3),
  ]);
  assert.deepEqual(plain.stops.map((t) => t.px), [940_000n], "without a book a stop replaces the one there");
  const book = (more) => triggerLedger([
    ev("TriggerSet", { asset: BTC, isTake: false, px: 950_000n, sz: 0n, armedAt: 502n }, 500),
    ev("TriggerSet", { asset: BTC, isTake: false, px: 0n, sz: 0n, armedAt: 510n }, 508),
    ...more,
  ], { book: true });
  const asked = book([]);
  assert.deepEqual([asked.stops[0].px, asked.stops[0].change], [950_000n, { px: 0n, sz: 0n, armedAt: 510 }], "taken off from 510: until then it acts");
  assert.deepEqual(book([ev("TriggerSet", { asset: BTC, isTake: true, px: 1_100_000n, sz: 0n, armedAt: 513n }, 511)]).stops, [],
    "a later event finds the change in force");
  const fillTx = [
    ev("TriggerCleared", { asset: BTC, isTake: false }, 520),
    ev("Filled", { asset: BTC, isBuy: false, sz: 100n, px: 949_000n, cost: 0n, late: 7n, source: 0 }, 520),
    ev("TriggerFilled", { asset: BTC, isTake: false, sz: 100n, px: 949_000n, cost: 0n, triggerPx: 950_000n, source: 0, touchBlock: 509n, touchPx: 949_000n }, 520),
  ];
  const filled = book(fillTx);
  assert.deepEqual(filled.trades.map((t) => [t.via, t.triggerPx, t.touchBlock, t.touchPx]), [["stop", 950_000n, 509, 949_000n]]);
  assert.deepEqual(filled.stops, []);
  assert.throws(() => triggerLedger([fillTx[2]]), /no Filled before it/);
});

test("an amount in USDC buys the size it divides into at the price, rounded down to the asset's step", () => {
  // BTC at 5 size decimals: a price carries one decimal, 60 000.0 is 600 000; 1 size unit is 0.00001 BTC
  const px = 600_000n;
  assert.equal(sizeFromUsdc(600n * USD, px), 1_000n, "an exact division: $600 at 60 000.0 is 0.01 BTC");
  assert.equal(sizeText(sizeFromUsdc(600n * USD, px), 5), "0.01");
  // 1 000 USDC at 60 000.0 is 0.016666... BTC: the 0.01666 below it, never the 0.01667 above
  assert.equal(sizeFromUsdc(1_000n * USD, px), 1_666n);
  assert.equal(sizeText(sizeFromUsdc(1_000n * USD, px), 5), "0.01666");
  // the boundary: one unit of USDC short of a whole step is the step below, the step's own cost is the step
  assert.equal(sizeFromUsdc(2n * px - 1n, px), 1n);
  assert.equal(sizeFromUsdc(2n * px, px), 2n);
  // the order never costs more than the amount typed
  for (const usdc of [1n * USD, 1_035_030_000n, 123_456_789n, 10_000n * USD]) {
    const size = sizeFromUsdc(usdc, px);
    assert.ok(size * px <= usdc && (size + 1n) * px > usdc, `${usdc}`);
  }
});

test("an asset of no size decimals and one of five both divide the same way: price units follow the size decimals", () => {
  // 0 size decimals: the price carries 6 decimals, 2.5 USDC a unit is 2 500 000; $10 buys 4 units
  assert.equal(sizeFromUsdc(10n * USD, 2_500_000n), 4n);
  assert.equal(sizeText(4n, 0), "4");
  // 2 size decimals (SOL at 148.2500 has 4 price decimals: 1 482 500): $100 buys 0.67
  assert.equal(sizeFromUsdc(100n * USD, 1_482_500n), 67n);
  assert.equal(sizeText(67n, 2), "0.67");
  // 5 size decimals: one step is 0.00001 BTC and costs $0.6 at 60 000.0, so $1 buys exactly one
  assert.equal(sizeFromUsdc(USD, 600_000n), 1n);
});

test("an amount below one step, a zero, and a price that was not read are refused in words", () => {
  assert.throws(() => sizeFromUsdc(599_999n, 600_000n), /less than one step of this asset's size: one step costs \$0\.6 at this price/);
  assert.throws(() => sizeFromUsdc(1n, 2_500_000n), /less than one step/);
  assert.throws(() => sizeFromUsdc(0n, 600_000n), /must be above zero/);
  assert.throws(() => sizeFromUsdc(-5n, 600_000n), /must be above zero/);
  assert.throws(() => sizeFromUsdc(null, 600_000n), /must be above zero/);
  for (const px of [null, undefined, 0n, -1n]) assert.throws(() => sizeFromUsdc(100n * USD, px), /The price was not read/);
  // the price is checked first: no price, no talk of the amount
  assert.throws(() => sizeFromUsdc(0n, null), /The price was not read/);
});
