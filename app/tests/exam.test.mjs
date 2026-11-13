// The trader's screen of a paper exam (app/lib/exam.js): the calls it builds, byte for byte as Foundry's
// `cast calldata` writes them; the account as PaperExam counts it; an order worked out before it is sent; the
// contract's refusals in plain words; and the names, each held to src/exam/PaperExam.sol.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  PAPER_EXAM_ABI, STATUS, BREACH, FALLBACK_BLOCKS, MIN_TRADE_NOTIONAL, ORACLE_PX_PRECOMPILE, BBO_PRECOMPILE, SIGNS,
  ESTIMATE_NOTE, REFUSALS, placeCall, graduateCall, setAgentCall, pxIndex, oraclePxCall, bboCall, bboOf, sizeUnits,
  pxUnits, sizeText, pxText, usdText, pendingOrder, examAccount, fillPrice, orderPreview, refusalText, refusalOf,
  candleRows, bookSides, dataSource,
} from "../lib/exam.js";
import { keccak256 } from "./keccak.mjs";

const SRC = readFileSync(new URL("../../src/exam/PaperExam.sol", import.meta.url), "utf8");
const TYPES = readFileSync(new URL("../../src/Types.sol", import.meta.url), "utf8");
const FOLDED = SRC.replace(/\s+/g, " ").replace(/ (calldata|memory)(?= )/g, "");

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
  assert.match(SRC, /function setAgent\(address agent_\) external \{\s+if \(msg\.sender != trader\) revert NotTrader\(\);/);
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
  assert.match(SRC, /if \(!ok \|\| r\.length < 64\) return \(oracle, SOURCE_ORACLE_FALLBACK\);/);
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
  assert.match(SRC, /if \(eq \* bps < base \* \(bps - int256\(uint256\(_rules\.maxDrawdownBps\)\)\)\) return Breach\.Drawdown;/);
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
    blocksToFill: 1, fallbackFrom: 1_071 });
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
  assert.match(SRC, /px = isBuy \? \(b\.ask > oracle \? b\.ask : oracle\) : \(b\.bid < oracle \? b\.bid : oracle\);/);
  assert.match(SRC, /if \(worse \* Units\.BPS > uint256\(oracle\) \* maxWorseBps\) revert TooFarFromOracle\(px, oracle\);/);
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
  assert.match(SRC, /uint256 cost = \(ntl \* _terms\.costPpm \+ PPM - 1\) \/ PPM;/);
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
  assert.equal(first, Object.keys(REFUSALS).length);
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
