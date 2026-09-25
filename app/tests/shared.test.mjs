// node --test app/tests/shared.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SHARED_ABI, BLOCKER, TICKET_STATE, MAX_PER_POINT, blockerText, amount, usd, plain, spot1e8, shares, worth, price,
  depositPlan, ticketsToName, lockedUntil, paymentsLine, explain, openedTicket, paidSummary,
} from "../lib/shared.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(ROOT, "src", "shared", "SharedPool.sol"), "utf8");

// The testnet run after its second point (spike/results/2026-09-24.jsonl): value 10.000001 USDC on
// 10.76923154 shares.
const VALUE = 1_000_000_100n;
const TOTAL = 1_076_923_154n;

test("amounts are shown from the integers, not through a float", () => {
  assert.equal(amount(1_807_142_800n, 8), "18.07");
  assert.equal(amount(500_000n, 6), "0.50");
  assert.equal(amount(123_456_789_000_000n, 8), "1,234,567.89");
  assert.equal(amount(-150_000_000n, 8), "-1.50");
  assert.equal(amount(1_807_142_800n, 8, 6), "18.071428");
  assert.equal(shares(TOTAL), "10.76");
});

test("USDC is shown to the millionth a payment carries, down to cents", () => {
  assert.equal(usd(92_857_200n, 8), "0.928572");
  assert.equal(usd(2_000_000_000n, 8), "20.00");
  assert.equal(usd(500_000n, 6), "0.50");
  assert.equal(usd(123_450_000n, 8), "1.2345");
  assert.equal(usd(1_807_142_899n, 8), "18.071428", "below the millionth is cut, not rounded up");
});

test("an input gets the number back without separators or padding", () => {
  assert.equal(plain(123_456_789_000_000n, 8), "1234567.89");
  assert.equal(plain(2_000_000_000n, 8), "20");
  assert.equal(plain(976_923_154n, 8), "9.76923154");
  assert.equal(plain(0n, 8), "0");
});

test("Hyperliquid's balance strings become spot units exactly, or not at all", () => {
  assert.equal(spot1e8("20.0"), 2_000_000_000n);
  assert.equal(spot1e8("18.071428"), 1_807_142_800n);
  assert.equal(spot1e8("0.00000001"), 1n);
  assert.equal(spot1e8("7"), 700_000_000n);
  for (const bad of ["", "1e-8", "-1", "1.123456789", "0x10", "1,000.5", null]) {
    assert.throws(() => spot1e8(bad), /not a spot USDC amount/, String(bad));
  }
});

test("a share's price and what shares are worth, at the run's numbers", () => {
  assert.equal(price(VALUE, TOTAL), "0.928571");
  // The platform's starting 1e8 shares after the run's third point: value 0.928572 on 1e8 shares.
  assert.equal(price(92_857_200n, 100_000_000n), "0.928572");
  // The depositor's queued shares: the third point paid them 9.071429 USDC, this rounded down to the
  // millionth a payment carries.
  assert.equal(worth(976_923_154n, VALUE, TOTAL), 907_142_954n);
  assert.equal(worth(TOTAL, VALUE, TOTAL), VALUE, "all the shares are worth the whole value");
  assert.equal(worth(5n, VALUE, 0n), 0n);
  assert.equal(price(VALUE, 0n), "—");
});

test("a deposit below the pool's minimum is refused before the wallet is asked", () => {
  assert.throws(() => depositPlan("19.99", 2_000_000_000n), /smallest deposit this pool takes is 20.00 USDC/);
  assert.deepEqual(depositPlan("20", 2_000_000_000n), { deposit: 2_000_000_000n, cost: 2_100_000_000n });
  assert.deepEqual(depositPlan("25.5", 2_000_000_000n), { deposit: 2_550_000_000n, cost: 2_650_000_000n });
});

test("a point names only tickets holding a deposit it can take, in the order given, eight at most", () => {
  const min = 2_000_000_000n;
  const t = (n, spot) => ({ address: `0x${String(n).padStart(40, "0")}`, spot });
  assert.deepEqual(ticketsToName([t(1, min - 1n), t(2, min), t(3, 0n), t(4, 5n * min)], min),
    [t(2, 0n).address, t(4, 0n).address]);
  const many = Array.from({ length: 11 }, (_, i) => t(i + 1, min));
  assert.equal(ticketsToName(many, min).length, MAX_PER_POINT);
  assert.deepEqual(ticketsToName(many, min), many.slice(0, 8).map((x) => x.address), "the first ones, the viewer's own");
});

test("a deposit goes to the ticket its own transaction opened", () => {
  const me = "0xbD97438655835138daBeE38f3B7d96275eDc315a";
  const other = "0x278AbBC5B78F34F77829dDd7887566E182beBD83";
  const opened = (depositor, ticket) => ({ name: "TicketOpened", args: { depositor, ticket, index: 0n } });
  const mine = "0x617bCc231586d33ab3984BFE07aa8cAdf9AEe27d";
  assert.equal(openedTicket([null, opened(other, "0x" + "11".repeat(20)), opened(me.toLowerCase(), mine)], me), mine);
  assert.throws(() => openedTicket([opened(other, mine)], me), /names no ticket opened for this wallet/);
  assert.throws(() => openedTicket([], me), /nothing was sent/);
});

test("the lock runs from the latest deposit", () => {
  assert.equal(lockedUntil(1_790_289_725n, 600n), 1_790_290_325);
});

test("a payment's two parts are said apart, and the total with them", () => {
  assert.equal(paymentsLine({ evm: 500_000n, core: 1_807_142_800n }),
    "18.571428 USDC: 0.50 on HyperEVM, in your wallet, and 18.071428 on HyperCore, in your spot balance");
  assert.equal(paymentsLine({ evm: 0n, core: 1_900_000_000n }),
    "19.00 USDC: 0.00 on HyperEVM, in your wallet, and 19.00 on HyperCore, in your spot balance");
});

test("what the pool has paid is read by position, so the time of the last payment is the field, not a method", () => {
  // ethers answers with an array, and an array's `at` is Array.prototype.at: the second round's page
  // showed "—" for the time until this read the answer by position.
  const when = (s) => `T${s}`;
  const answer = [1_790_310_795n, 0n, 1_951_219_500n];
  assert.equal(paidSummary(answer, when),
    "19.512195 USDC: 0.00 on HyperEVM, in your wallet, and 19.512195 on HyperCore, in your spot balance. "
    + "The latest payment: T1790310795.");
  assert.equal(paidSummary([0n, 0n, 0n], when), "Nothing yet.");
  assert.match(paidSummary(null, when), /deployed before the contract kept a record of payments/);
});

test("every reason blocker() can give has its own words", () => {
  assert.equal(BLOCKER.length, 6);
  assert.equal(new Set(BLOCKER).size, BLOCKER.length);
  assert.match(blockerText(0), /Nothing is holding it up/);
  assert.match(blockerText(3), /still has open positions/);
  assert.match(blockerText(5n), /five minutes/);
  assert.equal(blockerText(9), "Unknown reason 9.");
});

test("the pool's reverts are said in words; anything else is left to the page", () => {
  const revert = (name, args) => ({ revert: { name, args } });
  assert.equal(explain(revert("Locked", [1_790_291_578n])),
    "Your shares are locked until 2026-09-24 23:12 UTC, a fixed time after your latest deposit.");
  assert.equal(explain(revert("NotFree", [976_923_154n])), "You can ask for at most 9.76 shares now.");
  assert.match(explain(revert("NotQuiet", [5n, "0x6cAA4Ce577728F8386FF15fcFaA8F224E261486A"])), /five minutes/);
  assert.equal(explain(revert("SeatBusy", ["0x0"])), "");
  assert.equal(explain(new Error("user rejected")), "");
  assert.equal(explain(undefined), "");
});

// ── the ABI against the contract's source ─────────────────────────────────────────────
//
// The app reads the pool through the signatures above. A field renamed or two fields of the same
// width swapped in the contract would decode into the wrong words here without any error, so the
// source is read back: every name the app calls, and the order and types of the structs and enums
// whose values it shows.

function body(kind, name) {
  const m = SOURCE.match(new RegExp(`${kind}\\s+${name}\\s*\\{([^}]*)\\}`));
  assert.ok(m, `${kind} ${name} is missing from SharedPool.sol`);
  return m[1].replace(/\/\/[^\n]*/g, "");
}

function fields(name) {
  return body("struct", name).split(";").map((f) => f.trim()).filter(Boolean).map((f) => f.split(/\s+/));
}

function members(name) {
  return body("enum", name).split(",").map((m) => m.trim()).filter(Boolean);
}

function abiOutputs(fn) {
  const line = SHARED_ABI.find((l) => l.startsWith(`function ${fn}(`));
  assert.ok(line, `${fn} is not in the app's ABI`);
  const m = line.match(/returns \((.*)\)$/);
  return m[1].split(",").map((o) => o.trim().split(/\s+/));
}

test("every function the app calls is in the contract", () => {
  for (const line of SHARED_ABI.filter((l) => l.startsWith("function "))) {
    const name = line.match(/^function (\w+)\(/)[1];
    const declared = new RegExp(`function ${name}\\(`).test(SOURCE)
      || new RegExp(`\\bpublic\\s+(?:constant\\s+|immutable\\s+)?${name};`).test(SOURCE);
    assert.ok(declared, `SharedPool.sol has no ${name}`);
  }
  for (const line of SHARED_ABI.filter((l) => l.startsWith("error "))) {
    const name = line.match(/^error (\w+)\(/)[1];
    assert.match(SOURCE, new RegExp(`error ${name}\\(`), `SharedPool.sol has no error ${name}`);
  }
  for (const line of SHARED_ABI.filter((l) => l.startsWith("event "))) {
    const [, name, params] = line.match(/^event (\w+)\((.*)\)$/);
    const declared = SOURCE.match(new RegExp(`event ${name}\\(([^)]*)\\);`));
    assert.ok(declared, `SharedPool.sol has no event ${name}`);
    const squash = (s) => s.replace(/\s+/g, " ").trim();
    assert.equal(squash(declared[1]), squash(params), `${name} is declared otherwise in SharedPool.sol`);
  }
});

test("payments() decodes in the contract's order: when, HyperEVM, HyperCore", () => {
  assert.deepEqual(fields("Payments"), [["uint64", "at"], ["uint96", "evm"], ["uint96", "core"]]);
  assert.deepEqual(abiOutputs("payments"), [["uint64", "at"], ["uint96", "evm"], ["uint96", "core"]]);
});

test("tickets() and the ticket states are the contract's", () => {
  assert.deepEqual(fields("Ticket"), [["address", "depositor"], ["TicketState", "state"]]);
  assert.deepEqual(abiOutputs("tickets"), [["address", "depositor"], ["uint8", "state"]]);
  assert.deepEqual(members("TicketState"), TICKET_STATE);
});

test("the blocker's words follow the contract's reasons, one for one", () => {
  assert.deepEqual(members("Blocker"),
    ["None", "RuleBroken", "SeatClosing", "PositionsOpen", "PayoutInFlight", "PaymentInFlight"]);
  assert.equal(BLOCKER.length, members("Blocker").length);
  assert.deepEqual(abiOutputs("blocker"), [["uint8", "reason"], ["address", "account"]]);
});

test("the constants the app copies are the contract's", () => {
  assert.match(SOURCE, new RegExp(`MAX_PER_POINT = ${MAX_PER_POINT};`));
});
