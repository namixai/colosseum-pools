// node --test app/tests/simlines.test.mjs
//
// The two panels of a simulated pool (app/lib/simlines.js, app/lib/simpanel.js, views/simpool.js, views/simadmin.js):
// each state of a line for the trader, each state of the owner's switch, the money each panel shows, who sees what,
// and that neither panel carries the other's controls. On a made-up pool's reads (fixtures/simlines.mjs), shaped as
// SimPool answers them.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SIM_LINES_ABI, SIM_OWNER_ABI, TERMS_BOOK_ABI, tierOf, menuRows, tradeState, buyControl, switchState, menuGrid, roleOf,
  traderMoney, ownerMoney, poolRefusal, termsText, isSet, moneyHtml, menuHtml, switchesHtml,
} from "../lib/simlines.js";
import { PANEL_SECTIONS, panelNavHtml, notOwnerHtml, noWalletHtml } from "../lib/simpanel.js";
import { menu, lineStates, FIRST_ON, SIZES } from "./fixtures/simlines.mjs";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (...p) => readFileSync(join(ROOT, ...p), "utf8");
const POOL_SOL = read("src", "sim", "SimPool.sol");
const USDC = 1_000_000n;

const pool = ({ on = FIRST_ON, free = 100_000n * USDC, closed = false } = {}) => {
  const { menu: m, tiers } = menu();
  return menuRows({ menu: m, tiers, states: lineStates({ on, free, closed }) });
};
const line = (rows, size, tier) => rows.find((r) => r.size === BigInt(size) * USDC && r.tier.short.startsWith(`${tier}%`));

test("a tier is read from the pool's own numbers, and said as target / loss limit", () => {
  const t = tierOf([900, 300, 1]);
  assert.equal(t.short, "3% / 9%");
  assert.equal(t.words, "3% target, 9% loss limit, trailing to start");
  assert.equal(tierOf({ maxDrawdownBps: 1000, targetBps: 450, drawdownKind: 0 }).short, "4.5% / 10%");
  assert.match(tierOf([1000, 600, 0]).words, /from start/);
});

test("the menu has 24 lines: six sizes by four tiers, each with its price and the 8 % payout from the chain's numbers", () => {
  const rows = pool();
  assert.equal(rows.length, 24);
  const r = line(rows, 5_000, 3);
  assert.equal(r.price, 45n * USDC);
  assert.equal(r.payout, 400n * USDC);
  const grid = menuGrid(rows);
  assert.equal(grid.sizes.length, 6);
  assert.equal(grid.tiers.length, 4);
  assert.deepEqual(grid.tiers.map((t) => t.short), ["3% / 9%", "4% / 10%", "5% / 10%", "6% / 10%"]);
  assert.ok(grid.cells.every((row) => row.length === 4 && row.every(Boolean)), "every cell has its line");
  // Whatever order the pool lists its lines in, the grid reads size by size, tier by tier.
  const shuffled = [...rows].reverse();
  assert.deepEqual(menuGrid(shuffled).sizes, SIZES);
  assert.deepEqual(menuGrid(shuffled).tiers.map((t) => t.short), grid.tiers.map((t) => t.short));
});

test("a menu whose tiers or states do not match its lines is refused, not guessed", () => {
  const { menu: m, tiers } = menu();
  assert.throws(() => menuRows({ menu: m, tiers: tiers.slice(1), states: lineStates({ on: [], free: 0n }) }), /24 lines/);
  assert.throws(() => menuRows({ menu: m, tiers, states: lineStates({ on: [], free: 0n }).slice(1) }), /24 lines/);
});

// ── the trader's four states of a line ────────────────────────────────────────────────────────────────────────

test("trader, a line on and covered is on sale: its button is active once a code is given", () => {
  const r = line(pool(), 5_000, 3);
  assert.equal(r.sells, true);
  assert.deepEqual(tradeState(r), { kind: "sells", words: "On sale" });
  assert.deepEqual(buyControl(r, "GROUP-ONE"), { shown: true, enabled: true, why: "" });
});

test("trader, a line on and sold with no code asked: the button is disabled and says a code is needed", () => {
  const r = line(pool(), 5_000, 3);
  for (const code of ["", "   ", undefined]) {
    const c = buyControl(r, code);
    assert.equal(c.shown, true);
    assert.equal(c.enabled, false);
    assert.match(c.why, /code/);
  }
});

test("trader, a line on but the pool lacks the money for two payouts: disabled, and why, with the sum", () => {
  // 10 000 USDC free is 20 000 payouts' worth short of... the 250 000 line needs 2 x 20 000 = 40 000.
  const rows = pool({ on: [...FIRST_ON, 20], free: 30_000n * USDC });
  const r = rows[20];
  assert.equal(r.size, 250_000n * USDC);
  assert.equal(r.on, true);
  assert.equal(r.sells, false);
  assert.equal(r.short, 10_000n * USDC);
  const s = tradeState(r);
  assert.equal(s.kind, "short");
  assert.match(s.words, /short by 10,000\.00 USDC/);
  const c = buyControl(r, "GROUP-ONE");
  assert.equal(c.shown, true);
  assert.equal(c.enabled, false, "a code does not open it");
  assert.match(c.why, /short by 10,000\.00 USDC/);
});

test("trader, a line on in a pool that is closed to new sales: disabled, and why", () => {
  const rows = pool({ closed: true });
  const r = line(rows, 5_000, 3);
  assert.equal(r.on, true);
  assert.equal(r.sells, false);
  assert.equal(r.short, 0n);
  assert.equal(tradeState(r).kind, "closed");
  const c = buyControl(r, "GROUP-ONE");
  assert.deepEqual([c.shown, c.enabled], [true, false]);
  assert.match(c.why, /closed to new sales/);
});

test("trader, a line the owner switched off is shown grey with no button, whatever the pool holds", () => {
  const rows = pool({ free: 10_000_000n * USDC });
  const r = line(rows, 250_000, 6);
  assert.equal(r.on, false);
  assert.equal(r.canSwitchOn, true, "the pool could cover it, and it is still off: a line never switches itself on");
  assert.equal(tradeState(r).kind, "off");
  assert.deepEqual(buyControl(r, "GROUP-ONE"), { shown: false, enabled: false, why: "Switched off by the pool's owner" });
});

test("trader's page: only the lines on sale have an active button; a line off has none at all; the owner's controls are absent", () => {
  const rows = pool({ on: [...FIRST_ON, 20], free: 30_000n * USDC });
  const html = menuHtml(rows, "GROUP-ONE");
  const cells = (state) => html.split("<td ").filter((c) => c.includes(`data-state="${state}"`));
  assert.equal(cells("sells").length, 12);
  assert.ok(cells("sells").every((c) => /<button[^>]*class="sim-buy"[^>]*data-line="\d+"(?![^>]*disabled)/.test(c)), "each line on sale has an active button");
  assert.equal(cells("short").length, 1);
  assert.ok(cells("short").every((c) => /class="sim-buy"[^>]* disabled/.test(c) && /short by 10,000\.00 USDC/.test(c)));
  assert.equal(cells("off").length, 11);
  assert.ok(cells("off").every((c) => !c.includes("<button") && /Switched off by the pool/.test(c)));
  assert.equal((html.match(/<button/g) || []).length, 13, "twelve on sale and one short; none for the eleven off");
  assert.ok(!/sim-switch|setLine|role="switch"/.test(html), "no control of the pool's management on the trader's page");
  // Without a code every button waits.
  assert.ok(!/class="sim-buy"[^>]*data-line="\d+"(?![^>]*disabled)/.test(menuHtml(rows, "")));
});

test("the menu's prices and payouts are the chain's, in the cells", () => {
  const html = menuHtml(pool(), "x");
  assert.match(html, /45\.00 USDC/, "5 000 on the 3 % tier costs 45.00 in this fixture");
  assert.match(html, /pays 400\.00 USDC per stage passed/);
  assert.match(html, /pays 20,000\.00 USDC per stage passed/, "250 000 pays 20 000");
});

// ── the owner's states of a switch ────────────────────────────────────────────────────────────────────────────

test("owner, a line on and selling: the switch is on and can be switched off", () => {
  const r = line(pool(), 5_000, 3);
  assert.deepEqual(switchState(r), { kind: "on", checked: true, enabled: true, next: false, note: "On sale" });
});

test("owner, a line on that the pool cannot cover now: still on, still off-able, with the sum it lacks", () => {
  const r = pool({ on: [...FIRST_ON, 20], free: 30_000n * USDC })[20];
  const s = switchState(r);
  assert.equal(s.kind, "on-idle");
  assert.equal(s.checked, true);
  assert.equal(s.enabled, true, "off can always be done");
  assert.equal(s.next, false);
  assert.match(s.note, /short by 10,000\.00 USDC/);
});

test("owner, a line on in a closed pool: still on and can be switched off", () => {
  const s = switchState(line(pool({ closed: true }), 5_000, 3));
  assert.deepEqual([s.kind, s.checked, s.enabled, s.next], ["on-idle", true, true, false]);
  assert.match(s.note, /closed/);
});

test("owner, a line off that the pool can cover: the switch can be switched on", () => {
  const r = line(pool({ free: 1_000_000n * USDC }), 250_000, 3);
  assert.deepEqual(switchState(r), {
    kind: "off-ready", checked: false, enabled: true, next: true,
    note: "Off. The pool has the money for two payouts of this line: it can be switched on",
  });
});

test("owner, a line off that the pool cannot cover: the switch is disabled and the note names the sum it lacks", () => {
  const r = line(pool({ free: 30_000n * USDC }), 250_000, 3);
  const s = switchState(r);
  assert.equal(s.kind, "off-short");
  assert.equal(s.enabled, false);
  assert.equal(s.checked, false);
  assert.equal(s.note, "Off. Cannot be switched on: short by 10,000.00 USDC");
});

test("owner, a line off in a closed pool: the switch is disabled, the pool is the reason", () => {
  const s = switchState(line(pool({ closed: true, free: 1_000_000n * USDC }), 250_000, 3));
  assert.deepEqual([s.kind, s.enabled, s.checked], ["off-closed", false, false]);
  assert.match(s.note, /pool is closed/);
});

test("owner's panel: 24 switches, each as its state allows, no purchase control; the sum is beside a disabled one", () => {
  const rows = pool({ on: [...FIRST_ON, 20], free: 30_000n * USDC });
  const html = switchesHtml(rows);
  assert.equal((html.match(/role="switch"/g) || []).length, 24);
  const cells = (kind) => html.split("<td ").filter((c) => c.includes(`data-switch="${kind}"`));
  assert.equal(cells("on").length, 12);
  assert.equal(cells("on-idle").length, 1);
  assert.equal(cells("off-ready").length, 8, "the 50 000 and 100 000 lines are covered by 30 000 free");
  assert.equal(cells("off-short").length, 3, "the other three 250 000 lines are not");
  assert.ok(cells("off-ready").every((c) => !/<button[^>]* disabled/.test(c) && /aria-checked="false"/.test(c)));
  assert.ok(cells("on").every((c) => !/<button[^>]* disabled/.test(c) && /aria-checked="true"/.test(c)));
  assert.ok(cells("on-idle").every((c) => !/<button[^>]* disabled/.test(c)), "a line on can always be switched off");
  assert.ok(cells("off-short").every((c) => /<button[^>]* disabled/.test(c) && /short by/.test(c)));
  assert.ok(!/sim-buy|Buy exam/.test(html), "the owner's panel has no purchase button");
  // Money enough for the lines off: every one that is off becomes switchable.
  const ready = switchesHtml(pool({ free: 1_000_000n * USDC }));
  assert.equal(ready.split("<td ").filter((c) => c.includes('data-switch="off-ready"')).length, 12);
  assert.ok(ready.split("<td ").filter((c) => c.includes('data-switch="off-ready"')).every((c) => !/<button[^>]* disabled/.test(c)));
});

// ── role, money, words ────────────────────────────────────────────────────────────────────────────────────────

const OWNER = "0x00000000000000000000000000000000000000Aa";

test("the role comes from the pool's creator: the owner, someone else, no wallet", () => {
  assert.equal(roleOf(OWNER.toLowerCase(), OWNER), "owner");
  assert.equal(roleOf("0x0000000000000000000000000000000000000bb2", OWNER), "not-owner");
  assert.equal(roleOf(null, OWNER), "no-wallet");
  assert.equal(roleOf(undefined, OWNER), "no-wallet");
});

test("an address that is not the owner sees one sentence and no control; no wallet sees only the way to connect", () => {
  const html = notOwnerHtml();
  assert.match(html, /This is the pool owner's panel\. Your address is not this pool's owner\./);
  assert.ok(!/<button|role="switch"|<table|USDC/.test(html), "nothing of the pool, and no control");
  const none = noWalletHtml();
  assert.match(none, /Connect/);
  assert.ok(!/role="switch"|<table|USDC/.test(none));
});

test("the panel has its own navigation, with the next sections held in place and no link to the trader's pages", () => {
  const html = panelNavHtml(OWNER, "lines");
  assert.deepEqual(PANEL_SECTIONS.map((s) => s.key), ["lines", "withdraw", "codes", "operator"]);
  assert.equal((html.match(/<a /g) || []).length, 1, "only the section that is built is a link");
  assert.equal((html.match(/aria-disabled="true"/g) || []).length, 3);
  assert.ok(!html.includes("#/sim/") && !html.includes('href="#/"'), "no link to the trader's panel");
});

test("the trader sees free, floor and what is set aside for traders: not the creator's reserve, not the partners' shares", () => {
  const rows = traderMoney({ free: 1_500_000_000n, floor: 120_000_000n, held: 7_000_000n });
  assert.deepEqual(rows.map((r) => r.key), ["free", "floor", "held"]);
  const html = moneyHtml(rows);
  assert.match(html, /1,500\.00 USDC/);
  assert.match(html, /120\.00 USDC/);
  assert.match(html, /7\.00 USDC/);
  assert.ok(!/Locked|principal|partners/i.test(html));
});

test("the owner sees free, locked, set aside for partners by their codes, and the floor", () => {
  const rows = ownerMoney({ free: 1_500_000_000n, principal: 1_600_000_000n, sharesDue: 42_500_000n, floor: 120_000_000n });
  assert.deepEqual(rows.map((r) => r.key), ["free", "locked", "shares", "floor"]);
  assert.deepEqual(rows.map((r) => r.value), [1_500_000_000n, 1_600_000_000n, 42_500_000n, 120_000_000n]);
  const html = moneyHtml(rows);
  assert.match(html, /Set aside for partners by their codes/);
  assert.match(html, /1,600\.00 USDC/);
  assert.match(html, /42\.50 USDC/);
});

test("the pool's refusals are said in words, the sum from the error", () => {
  assert.match(poolRefusal("LineOff"), /switched off/);
  assert.match(poolRefusal("NotCovered", [12_500_000n]), /short by 12\.50 USDC/);
  assert.match(poolRefusal("NoSuchCode"), /not one of this pool's/);
  assert.match(poolRefusal("Closing"), /closed/);
  assert.match(poolRefusal("NotCreator"), /Only the pool's creator/);
  assert.equal(poolRefusal("Anything else"), "");
});

test("the terms are the book's text under the pool's hash; a pool with none, or a book without it, says nothing made up", () => {
  const h = `0x${"ab".repeat(32)}`;
  assert.equal(termsText(h, "Terms."), "Terms.");
  assert.equal(termsText(h, ""), null);
  assert.equal(termsText(`0x${"0".repeat(64)}`, "Terms."), null);
  assert.equal(isSet("0x0000000000000000000000000000000000000000"), false);
  assert.equal(isSet(OWNER), true);
});

// ── the two panels are two pages ──────────────────────────────────────────────────────────────────────────────

test("the site's navigation names neither panel's pool pages: the control panel has no link from the trader's side", () => {
  const index = read("app", "index.html");
  const nav = index.match(/<nav[\s\S]*?<\/nav>/)[0];
  assert.ok(!/manage/.test(nav) && !/#\/sim/.test(nav));
  const trader = read("app", "views", "simpool.js");
  assert.ok(!/manage|setLine|principal|simadmin/.test(trader.replace(/\/\/.*$/gm, "")), "no management in the trader's view");
  const admin = read("app", "views", "simadmin.js");
  assert.ok(!/\.buy\(|menuHtml|sim-buy|#\/sim\//.test(admin.replace(/\/\/.*$/gm, "")), "no purchase and no link to the trader's page in the owner's view");
  const app = read("app", "app.js");
  assert.match(app, /#\\\/sim\\\/\(0x/);
  assert.match(app, /#\\\/manage\\\/\(0x/);
});

test("the owner's panel reads the reserve and the shares only after the wallet is the pool's creator", () => {
  const admin = read("app", "views", "simadmin.js");
  const view = admin.slice(admin.indexOf("export async function simAdminView"));
  assert.ok(view.indexOf("creator()") < view.indexOf("readOwnerPool(at)"));
  assert.ok(view.indexOf('role !== "owner"') < view.indexOf("readOwnerPool(at)"));
});

test("everything the two panels read or send is the pool's own, as the contract declares it", () => {
  for (const abi of [SIM_LINES_ABI, SIM_OWNER_ABI]) {
    for (const l of abi) {
      const [, kind, name] = l.match(/^(function|event|error) (\w+)/);
      const re = kind === "function"
        ? new RegExp(`function ${name}\\(|public\\s+${name}\\s*;`)
        : new RegExp(`${kind} ${name}\\(`);
      assert.match(POOL_SOL, re, `${l} is not the contract's`);
    }
  }
  assert.match(read("src", "multi", "TermsBook.sol"), /function text\(bytes32/);
  assert.equal(TERMS_BOOK_ABI.length, 1);
  assert.match(POOL_SOL, /function setLine\(uint256 line, bool on\) external onlyCreator/);
});
