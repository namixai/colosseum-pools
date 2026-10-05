// node --test app/tests/shared.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SHARED_ABI, BLOCKER, TICKET_STATE, MAX_PER_POINT, blockerText, amount, usd, plain, spot1e8, shares, worth, price,
  depositPlan, ticketsToName, lockedUntil, paymentsLine, explain, openedTicket, paidSummary, depositState, sealLine,
  SHARED_POOL, isCurrentPool, feeLine,
} from "../lib/shared.js";
import { DEPLOYMENTS, liveDeployment, deploymentNamed } from "../lib/deployments.js";

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
  assert.match(explain(revert("SeatsNotSealed", [])), /hasn't sealed the book of seats/);
  assert.equal(explain(revert("SeatBusy", ["0x0"])), "");
  assert.equal(explain(new Error("user rejected")), "");
  assert.equal(explain(undefined), "");
});

// ── which pool the page opens, and whether it takes a deposit ─────────────────────────

const VIEW = readFileSync(join(ROOT, "app", "views", "shared.js"), "utf8");
const record = (label) => JSON.parse(readFileSync(join(ROOT, "deployments", `testnet-${label}.json`), "utf8"));

test("the shared page with no address opens the live deployment's shared pool, as its record names it", () => {
  const live = liveDeployment();
  // The record the config names: a deployment has held more than one shared pool since 5 Oct 2026.
  assert.equal(live.sharedPoolRecord, "shared-demo2b");
  const r = record(live.sharedPoolRecord);
  assert.equal(r.label, live.sharedPoolRecord);
  assert.equal(r.status, "complete");
  assert.equal(r.factory_from, live.label);
  assert.equal(r.PoolFactory, live.factory, "the pool's seats come from the live factory");
  assert.equal(live.sharedPool, r.SharedPool);
  assert.equal(SHARED_POOL, r.SharedPool);
  // Only the live deployment has one: the archive's shared pools open by address.
  assert.deepEqual(DEPLOYMENTS.filter((d) => "sharedPool" in d).map((d) => d.label), [live.label]);
  assert.deepEqual(DEPLOYMENTS.filter((d) => "sharedPoolRecord" in d).map((d) => d.label), [live.label]);
  // The pool before it on the same factory is another record, and is not the default any more.
  const earlier = record("shared-demo2");
  assert.equal(earlier.PoolFactory, live.factory);
  assert.notEqual(earlier.SharedPool, SHARED_POOL);
  assert.match(VIEW, /const at = ethers\.getAddress\(address \|\| SHARED_POOL\);/);
});

test("the page takes a deposit only into the current pool: started, sealed, on the live factory", () => {
  const [live, archive] = DEPLOYMENTS;
  const current = true;
  assert.deepEqual(depositState({ deployment: live, started: true, sealed: true, current }), { open: true, note: "" });
  // A pool older than the seal has no such reading; where the page would open it at all, it deposits as before.
  assert.equal(depositState({ deployment: live, started: true, sealed: null, current }).open, true);

  const unsealed = depositState({ deployment: live, started: true, sealed: false, current });
  assert.equal(unsealed.open, false);
  assert.match(unsealed.note, /hasn't sealed the book of seats/);
  const unstarted = depositState({ deployment: live, started: false, sealed: false, current });
  assert.equal(unstarted.open, false);
  assert.match(unstarted.note, /hasn't started yet/);
  const archived = depositState({ deployment: archive, started: true, sealed: null, current: false });
  assert.equal(archived.open, false);
  assert.match(archived.note, /first deployment, kept as an archive/);
  assert.match(archived.note, /can still ask to withdraw/);
  const own = depositState({ deployment: null, started: true, sealed: null, current: false });
  assert.equal(own.open, false);
  assert.match(own.note, /a factory of its own/);

  // An earlier pool of the live deployment: started, sealed, on the live factory -- and still closed, because the
  // site names another. Until 5 Oct 2026 this was open: the pool of 3 October would have kept its deposit form.
  const earlier = depositState({ deployment: live, started: true, sealed: true, current: false });
  assert.equal(earlier.open, false);
  assert.match(earlier.note, /an earlier shared pool of the live deployment, kept as a record/);
  assert.match(earlier.note, /A holder here can still ask to withdraw\./);
  // Left out, `current` closes the form: a caller that forgot to say is not taken for the current pool.
  assert.equal(depositState({ deployment: live, started: true, sealed: true }).open, false);
  // Which pool is current is the config's address, whatever the case of the link.
  assert.equal(isCurrentPool(SHARED_POOL), true);
  assert.equal(isCurrentPool(SHARED_POOL.toLowerCase()), true);
  assert.equal(isCurrentPool(record("shared-demo2").SharedPool), false);
  assert.equal(isCurrentPool(undefined), false);
  assert.equal(isCurrentPool("0x1", ""), false);
  // A config that names no pool makes no pool current, not every empty address.
  assert.equal(isCurrentPool("", ""), false);
  assert.equal(isCurrentPool(undefined, undefined), false);

  // The three pools of the earlier rounds, by the factory each record names: none of them is the live one's.
  for (const label of ["shared-demo", "shared-trade", "shared-run"]) {
    const d = deploymentNamed(record(label).PoolFactory);
    assert.equal(depositState({ deployment: d, started: true, sealed: null, current: false }).open, false, label);
  }
});

test("the platform's fee is said as a share of a holder's profit, and as none where the pool takes none", () => {
  assert.equal(feeLine(1000), "10% of your own profit, taken when you withdraw");
  assert.equal(feeLine(1000n), "10% of your own profit, taken when you withdraw");
  assert.equal(feeLine(250), "2.5% of your own profit, taken when you withdraw");
  // A pool deployed with 0, as the current one is: nothing is taken, and the line does not read as if it were.
  assert.equal(feeLine(0), "none: the platform takes nothing from a holder's profit");
  assert.equal(feeLine(0n), "none: the platform takes nothing from a holder's profit");
  assert.equal(record(liveDeployment().sharedPoolRecord).fee_bps, 0);
  assert.match(VIEW, /row\("The platform's fee", esc\(feeLine\(feeBps\)\)\)/);
});

test("the book of seats is said as the contract holds it, and not at all for a pool older than the seal", () => {
  assert.match(sealLine(true), /^Sealed: nobody can add a seat/);
  assert.match(sealLine(false), /^Open: .*takes no deposit until the book is sealed/);
  assert.equal(sealLine(null), "");
});

test("the page reads the seal and the factory from the pool, and offers no way to send money where deposits are closed", () => {
  // The deployment is the one whose factory the pool itself names, not one the link or the config says.
  assert.match(VIEW, /sp\.factory\(\),/);
  assert.match(VIEW, /const deployment = deploymentNamed\(seatFactory\);/);
  assert.match(VIEW, /const current = isCurrentPool\(at\);\s+const deposit = depositState\(\{ deployment, started, sealed, current \}\);/);
  // An earlier pool of the live deployment is named as one on its badge.
  assert.match(VIEW, /: current \? "live deployment" : "live deployment, an earlier pool";/);
  // Each seat's card says whether an agent could trade it through the repository's client, from the seat's own terms.
  assert.match(VIEW, /const agent = agentCardLine\(agentTradability\(\{ capital: Number\(terms\.capital\) \/ 1e6,\s+fundedCapital: Number\(terms\.fundedCapital\) \/ 1e6, leverageX100: rules\.maxLeverageX100 \}\)\);/);
  assert.match(VIEW, /\$\{agent \? `<p class="small">\$\{esc\(agent\)\}<\/p>` : ""\}/);
  // Only a revert means "older than the seal"; a failed read is not taken for it.
  assert.match(VIEW, /sp\.seatsSealed\(\)\.catch\(\(err\) => \{\s+if \(err\?\.code === "CALL_EXCEPTION"\) return null;\s+throw err;/);
  // The form, its button's handler and the "Send to it" button of an open ticket all hang on the same answer.
  assert.match(VIEW, /\$\{pool\.deposit\.open \? `<p>Each deposit goes to an address of its own/);
  assert.match(VIEW, /if \(pool\.deposit\.open\) wire\(\$\("#dep-btn", box\)/);
  assert.match(VIEW, /const waitingToSend = pool\.deposit\.open && state === "Open" && spots\[i\] === 0n;/);
  // Asking to withdraw is not behind it.
  assert.match(VIEW, /\n  wire\(\$\("#wd-btn", box\), async/);
  // Whether seats can still be added is said from seatsSealed() alone. A pool older than the seal may have closed
  // its seats another way, so the page claims nothing about it.
  assert.doesNotMatch(VIEW, /operator can (still )?add seats/i);
  // An unstarted pool can't run a settlement point, and the page doesn't offer one.
  assert.match(VIEW, /<button id="point"\$\{reason \|\| !started \? " disabled" : ""\}>/);
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
