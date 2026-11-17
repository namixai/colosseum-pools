// node --test app/tests/simlines.test.mjs
//
// The two panels of a pool of exams (app/lib/simlines.js, app/lib/simpanel.js, views/simpool.js, views/simadmin.js),
// built to the approved mockups 01 and 02: each state of a line for the trader, each state of the owner's switch, the
// money the owner reads, who sees what, the words Alex struck (a simulation, a paper exam, a fixed payout sum on the
// trader's page), and that neither panel carries the other's controls or a link to it. On a made-up pool's reads
// (fixtures/simlines.mjs), shaped as SimPool answers them.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  SIM_LINES_ABI, SIM_OWNER_ABI, TERMS_BOOK_ABI, COVER_PAYOUTS, dollars, tierOf, menuRows, tradeState, buyControl,
  switchState, sizeGroups, sizeNotes, sizeStatus, roleOf, ownerMoney, poolRefusal, termsText, isSet, menuHtml,
  moneyHtml, switchesHtml,
} from "../lib/simlines.js";
import {
  panelOf, USER_NAV, MANAGE_NAV, userHeaderHtml, manageHeaderHtml, footerHtml, notOwnerHtml, noWalletHtml,
} from "../lib/simpanel.js";
import { menu, lineStates, FIRST_ON, SIZES, PRICES, TIERS } from "./fixtures/simlines.mjs";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (...p) => readFileSync(join(ROOT, ...p), "utf8");
const POOL_SOL = read("src", "sim", "SimPool.sol");
const U = 1_000_000n;

const pool = ({ on = FIRST_ON, free = 3_600n * U, closed = false } = {}) => {
  const { menu: m, tiers } = menu();
  return menuRows({ menu: m, tiers, states: lineStates({ on, free, closed }), closed });
};
const line = (rows, size, loss) => rows.find((r) => r.size === BigInt(size) * U && r.tier.short.startsWith(`${loss}%`));
const cellsOf = (html, state) => html.split('<div class="sp-cell ').filter((c) => c.startsWith(`${state}"`) || c.includes(`data-state="${state}"`));
const noComments = (s) => s.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");

test("the fixture is the first pool's plan: sizes, tiers (loss, target), prices", () => {
  const plan = JSON.parse(read("deployments", "plans", "sim-first-pool.json")).menu;
  const { menu: m, tiers } = menu();
  assert.equal(m.length, plan.length);
  plan.forEach((p, i) => {
    assert.equal(m[i][0], BigInt(p.size_usdc) * U);
    assert.equal(m[i][1], BigInt(p.price_usdc) * U);
    assert.deepEqual(tiers[i], [p.max_drawdown_bps, p.target_bps, p.drawdown_kind]);
  });
  assert.equal(PRICES.length, 24);
  assert.equal(SIZES.length * TIERS.length, 24);
});

test("a tier is the pool's own numbers, loss limit first, as the mockup says it", () => {
  const t = tierOf([300, 900, 1]);
  assert.equal(t.label, "loss 3% · target 9%");
  assert.equal(t.short, "3% / 9%");
  assert.equal(t.words, "3% loss limit, 9% target, trailing to start");
  assert.equal(tierOf({ maxDrawdownBps: 450, targetBps: 1000, drawdownKind: 0 }).short, "4.5% / 10%");
  assert.match(tierOf([600, 1000, 0]).words, /from start/);
});

test("the menu has 24 lines in six sizes by four tiers, each need two payouts of 8 % of its size", () => {
  assert.equal(COVER_PAYOUTS, 2n);
  assert.match(POOL_SOL, /uint256 constant COVER_PAYOUTS = 2;/);
  const rows = pool();
  assert.equal(rows.length, 24);
  const r = line(rows, 5_000, 3);
  assert.equal(r.price, 20n * U);
  assert.equal(r.payout, 400n * U);
  assert.equal(r.need, 800n * U);
  const { tiers, groups } = sizeGroups(rows);
  assert.deepEqual(tiers.map((t) => t.label), ["loss 3% · target 9%", "loss 4% · target 10%", "loss 5% · target 10%", "loss 6% · target 10%"]);
  assert.equal(groups.length, 6);
  assert.deepEqual(groups.map((g) => g.size), SIZES);
  assert.ok(groups.every((g) => g.lines.length === 4 && g.lines.every(Boolean)));
  // Whatever order the pool lists its lines in.
  const back = sizeGroups([...rows].reverse());
  assert.deepEqual(back.groups.map((g) => g.size), SIZES);
  assert.deepEqual(back.tiers.map((t) => t.label), tiers.map((t) => t.label));
});

test("a menu whose tiers or states do not match its lines is refused, not guessed", () => {
  const { menu: m, tiers } = menu();
  assert.throws(() => menuRows({ menu: m, tiers: tiers.slice(1), states: lineStates({ on: [], free: 0n }) }), /24 lines/);
  assert.throws(() => menuRows({ menu: m, tiers, states: lineStates({ on: [], free: 0n }).slice(1) }), /24 lines/);
});

test("dollars are written as the mockups write them", () => {
  assert.equal(dollars(20n * U), "$20");
  assert.equal(dollars(3_600n * U), "$3,600");
  assert.equal(dollars(2_375n * U + 500_000n), "$2,375.50");
});

// ── the trader's states of a line ─────────────────────────────────────────────────────────────────────────────

test("trader, on sale: 'Buy $20', active once a code is given; without one it waits and says why", () => {
  const r = line(pool(), 5_000, 3);
  assert.equal(r.sells, true);
  assert.equal(tradeState(r).kind, "sells");
  assert.deepEqual(buyControl(r, "GROUP-ONE"), { shown: true, enabled: true, label: "Buy $20", why: "" });
  for (const code of ["", "   ", undefined]) {
    const c = buyControl(r, code);
    assert.deepEqual([c.shown, c.enabled, c.label], [true, false, "Buy $20"]);
    assert.match(c.why, /code/);
  }
});

test("trader, on but the pool is short: 'Short $400', disabled whatever the code, with the sum", () => {
  const r = line(pool(), 25_000, 3);
  assert.deepEqual([r.on, r.sells, r.short], [true, false, 400n * U]);
  assert.equal(tradeState(r).kind, "short");
  const c = buyControl(r, "GROUP-ONE");
  assert.deepEqual([c.shown, c.enabled, c.label], [true, false, "Short $400"]);
});

test("trader, on in a closed pool: disabled and says closed", () => {
  const r = line(pool({ closed: true }), 5_000, 3);
  assert.deepEqual([r.on, r.sells, r.short], [true, false, 0n]);
  assert.equal(tradeState(r).kind, "closed");
  const c = buyControl(r, "GROUP-ONE");
  assert.deepEqual([c.shown, c.enabled, c.label], [true, false, "Closed"]);
});

test("trader, switched off by the owner: no button at all, whatever the pool holds", () => {
  const r = line(pool({ free: 10_000_000n * U }), 250_000, 6);
  assert.equal(r.on, false);
  assert.equal(r.canSwitchOn, true, "the pool could cover it; a line never switches itself on");
  assert.equal(tradeState(r).kind, "off");
  assert.equal(buyControl(r, "GROUP-ONE").shown, false);
});

test("trader's menu, the mockup's state: 5K and 10K on sale, 25K short $400, 50K, 100K and 250K off", () => {
  const html = menuHtml(pool(), 3_600n * U, "GROUP-ONE");
  const sections = html.split('<section class="sp-size ').slice(1);
  assert.deepEqual(sections.map((s) => s.split('"')[0]), ["sale", "sale", "short", "off", "off", "off"]);
  assert.equal(cellsOf(html, "sells").length, 8);
  assert.equal(cellsOf(html, "short").length, 4);
  assert.equal(cellsOf(html, "off").length, 12);
  assert.ok(cellsOf(html, "sells").every((c) => /class="sp-buy"[^>]*data-line="\d+"(?![^>]*disabled)/.test(c)));
  assert.ok(cellsOf(html, "short").every((c) => /class="sp-buy"[^>]* disabled[^>]*>Short \$400</.test(c)));
  assert.ok(cellsOf(html, "off").every((c) => !c.includes("<button") && c.includes(">Off<")));
  assert.equal((html.match(/<button/g) || []).length, 12, "no button for the twelve lines off");
  // The mockup's own words.
  assert.match(html, /On sale\.<\/b> The pool has \$3,600 free; this size needs \$800 \(two payouts\)\./);
  assert.match(html, /On sale\.<\/b> The pool has \$3,600 free; this size needs \$1,600 \(two payouts\)\./);
  assert.match(html, /On, but the pool is short of money\.<\/b> It has \$3,600 free and needs \$4,000 for two payouts, \$400 short\. Buying comes back by itself when money returns\./);
  assert.match(html, /Switched off by the pool owner\.<\/b> Not for sale\./);
  assert.match(html, />Buy \$20</);
  assert.match(html, />Buy \$95</);
  assert.match(html, /Menu · 6 sizes × 4 tiers · 24 lines/);
  // Without a code every Buy waits; the rest does not change.
  assert.ok(!/class="sp-buy"[^>]*data-line="\d+"(?![^>]*disabled)/.test(menuHtml(pool(), 3_600n * U, "")));
});

test("trader's menu: no payout column, no payout sum, no gauge, no facts strip, no management control", () => {
  const html = menuHtml(pool(), 3_600n * U, "GROUP-ONE");
  assert.ok(!/payout[^s]/i.test(html.replace(/two payouts/g, "")), "no payout sum or column");
  assert.ok(!/\$400[^<]*<\/div><button|pays|8%|8 %/.test(html), "no fixed payout");
  assert.ok(!/gauge|Money free for payouts|locked at opening|payouts a day/i.test(html));
  assert.ok(!/sp-tg|role="switch"|setLine|sp-swc/.test(html));
  for (const [, tag] of html.matchAll(/data-line="(\d+)"/g)) assert.ok(Number(tag) < 24);
});

test("a pool that is closed says so under each size that is on", () => {
  const html = menuHtml(pool({ closed: true }), 3_600n * U, "GROUP-ONE");
  // The 25K lines are short as well as closed: lineState answers the sum it lacks whatever the pool's state, and
  // money does not reopen a closed pool, so they say closed, not short (review of this change).
  assert.equal(cellsOf(html, "closed").length, 12);
  assert.equal(cellsOf(html, "short").length, 0);
  assert.match(html, /On, but the pool is closed to new sales\./);
  assert.ok(!/short of money|Short \$/.test(html));
  const g = sizeGroups(pool({ closed: true })).groups[2];
  assert.deepEqual(sizeNotes(g, 3_600n * U).map((n) => n.kind), ["closed"]);
});

// ── the owner's states of a switch ────────────────────────────────────────────────────────────────────────────

test("owner, a line on and selling: the switch is on and can be switched off", () => {
  assert.deepEqual(switchState(line(pool(), 5_000, 3)), { kind: "on", checked: true, enabled: true, next: false, note: "On sale" });
});

test("owner, a line on that the pool cannot cover now: still on, off-able, with the sum it lacks", () => {
  const s = switchState(line(pool(), 25_000, 3));
  assert.deepEqual([s.kind, s.checked, s.enabled, s.next], ["on-idle", true, true, false]);
  assert.match(s.note, /short by \$400/);
});

test("owner, a line on in a closed pool: still on and can be switched off", () => {
  const s = switchState(line(pool({ closed: true }), 5_000, 3));
  assert.deepEqual([s.kind, s.checked, s.enabled, s.next], ["on-idle", true, true, false]);
  assert.match(s.note, /closed/);
});

test("owner, a line off that the pool can cover: the switch can be switched on", () => {
  const s = switchState(line(pool({ free: 1_000_000n * U }), 250_000, 3));
  assert.deepEqual([s.kind, s.checked, s.enabled, s.next], ["off-ready", false, true, true]);
});

test("owner, a line off that the pool cannot cover: disabled, the sum it lacks beside it", () => {
  const s = switchState(line(pool(), 50_000, 3));
  assert.deepEqual([s.kind, s.checked, s.enabled], ["off-short", false, false]);
  assert.equal(s.note, "Off. Cannot be switched on: short by $4,400");
});

test("owner, a line off in a closed pool: disabled, the pool is the reason", () => {
  const s = switchState(line(pool({ closed: true, free: 1_000_000n * U }), 250_000, 3));
  assert.deepEqual([s.kind, s.enabled, s.checked], ["off-closed", false, false]);
  assert.match(s.note, /pool is closed/);
});

test("owner's switches, the mockup's state: shortfalls $4,400, $12,400 and $36,400 beside the switches that cannot turn on", () => {
  const html = switchesHtml(pool(), 3_600n * U);
  assert.equal((html.match(/role="switch"/g) || []).length, 24);
  const cells = (kind) => html.split('<div class="sp-swc ').filter((c) => c.startsWith(`${kind}"`));
  assert.equal(cells("on").length, 8);
  assert.equal(cells("on-idle").length, 4);
  assert.equal(cells("off-short").length, 12);
  assert.ok(cells("on").concat(cells("on-idle")).every((c) => !/<button[^>]* disabled/.test(c) && /aria-checked="true"/.test(c)), "a line on can always be switched off");
  assert.ok(cells("off-short").every((c) => /<button[^>]* disabled/.test(c) && /aria-checked="false"/.test(c)));
  assert.match(html, /needs \$8,000 free for two payouts\. Short \$4,400\./);
  assert.match(html, /needs \$16,000 free for two payouts\. Short \$12,400\./);
  assert.match(html, /needs \$40,000 free for two payouts\. Short \$36,400\./);
  assert.match(html, /pool short \$400\. Sells again by itself when money returns\. You can switch it off\./);
  assert.match(html, /free \$3,600 ≥ needs \$800/);
  assert.match(html, /payout \$400<br>needs \$800/);
  assert.ok(!/sp-buy|Buy \$/.test(html), "no purchase control on the owner's panel");
  // Money enough: every line that is off can be switched on.
  const ready = switchesHtml(pool({ free: 1_000_000n * U }), 1_000_000n * U);
  const off = ready.split('<div class="sp-swc ').filter((c) => c.startsWith('off-ready"'));
  assert.equal(off.length, 12);
  assert.ok(off.every((c) => !/<button[^>]* disabled/.test(c) && /data-next="true"/.test(c)));
});

test("a size with lines in different states says each, with a count", () => {
  const rows = pool({ on: [0, 1, 16] });
  const g = sizeGroups(rows).groups[0];
  assert.deepEqual(sizeStatus(g, 3_600n * U).badges.map((b) => b.label), ["On · selling ×2", "Off · can switch on ×2"]);
  assert.deepEqual(sizeNotes(g, 3_600n * U).map((n) => n.kind), ["sells", "off"]);
});

// ── role, money, words ────────────────────────────────────────────────────────────────────────────────────────

const OWNER = "0x00000000000000000000000000000000000000Aa";
const POOL_ADDR = "0x0000000000000000000000000000000000009001";

test("the role comes from the pool's creator: the owner, someone else, no wallet", () => {
  assert.equal(roleOf(OWNER.toLowerCase(), OWNER), "owner");
  assert.equal(roleOf("0x0000000000000000000000000000000000000bb2", OWNER), "not-owner");
  assert.equal(roleOf(null, OWNER), "no-wallet");
  assert.equal(roleOf(undefined, OWNER), "no-wallet");
});

test("the owner's money in four parts: free, locked (owed, stops, held, staged), reserved for partners, floor", () => {
  const m = ownerMoney({ free: 3_600n * U, owed: 800n * U, reserved: 400n * U, held: 0n, staged: 0n, sharesDue: 120n * U, floor: 2_400n * U });
  assert.deepEqual([m.free, m.locked, m.shares, m.floor, m.total], [3_600n * U, 1_200n * U, 120n * U, 2_400n * U, 4_920n * U]);
  assert.deepEqual(m.parts, { locked: 24.39, shares: 2.43, free: 73.17, floor: 75.6 });
  const html = moneyHtml(m);
  assert.match(html, /Pool money · \$4,920 in the pool/);
  for (const k of ["free", "locked", "shares", "floor"]) assert.match(html, new RegExp(`data-money="${k}"`));
  assert.match(html, /<dd>\$3,600<\/dd>/);
  assert.match(html, /<dd>\$1,200<\/dd>/);
  assert.match(html, /<dd>\$120<\/dd>/);
  assert.match(html, /<dd>\$2,400<\/dd>/);
  assert.match(html, /Payouts owed \$800, kept for a stop \$400, held for traders \$0, passed exams and running stages \$0\./);
  assert.match(html, /floor \$2,400 · stays in while open/);
  assert.match(html, /left:75\.6%/, "the marker is where a withdrawal stops: what the pool keeps and the floor");
  // A pool with nothing in it draws a bar of nothing.
  assert.deepEqual(ownerMoney({ free: 0n, owed: 0n, reserved: 0n, held: 0n, staged: 0n, sharesDue: 0n, floor: 0n }).parts, { locked: 0, shares: 0, free: 0, floor: 0 });
});

test("the pool's refusals are said in words, the sum from the error", () => {
  assert.match(poolRefusal("LineOff"), /switched off/);
  assert.match(poolRefusal("NotCovered", [12_500_000n]), /short by \$12\.50/);
  assert.match(poolRefusal("NoSuchCode"), /not one of this pool's/);
  assert.match(poolRefusal("Closing"), /closed/);
  assert.match(poolRefusal("NotCreator"), /Only the pool's creator/);
  assert.equal(poolRefusal("Anything else"), "");
});

test("the terms are the book's text under the pool's hash; none, or a book without it, says nothing made up", () => {
  const h = `0x${"ab".repeat(32)}`;
  assert.equal(termsText(h, "Terms."), "Terms.");
  assert.equal(termsText(h, ""), null);
  assert.equal(termsText(`0x${"0".repeat(64)}`, "Terms."), null);
  assert.equal(isSet("0x0000000000000000000000000000000000000000"), false);
  assert.equal(isSet(OWNER), true);
});

// ── the two panels are two pages ──────────────────────────────────────────────────────────────────────────────

test("each panel is its own route, and the panel is read from the hash", () => {
  assert.equal(panelOf(`#/sim/${POOL_ADDR}`), "user");
  assert.equal(panelOf(`#/sim/${POOL_ADDR}?code=GROUP-ONE`), "user");
  assert.equal(panelOf(`#/manage/${POOL_ADDR}`), "manage");
  for (const h of ["#/", "#/pool/" + POOL_ADDR, "#/economics", "#/manage", "#/sim"]) assert.equal(panelOf(h), null);
  const app = read("app", "app.js");
  assert.match(app, /#\\\/sim\\\/\(0x/);
  assert.match(app, /#\\\/manage\\\/\(0x/);
  assert.match(app, /panelOf\(hash\)/);
});

test("the user panel's header: its badge, Testnet app, the menu with the next sections held, no link to the control panel", () => {
  const html = userHeaderHtml({ address: POOL_ADDR, account: "0x7a…3f" });
  assert.match(html, /User panel/);
  assert.match(html, /Testnet app/);
  assert.deepEqual(USER_NAV.map((s) => s.label), ["Pool", "Trade", "My exams", "My payouts"]);
  assert.equal((html.match(/<a /g) || []).length, 1, "only the pool page is built");
  assert.equal((html.match(/aria-disabled="true"/g) || []).length, 3);
  assert.ok(!/manage/i.test(html.replace(/Management/g, "")), "no link to the control panel");
  assert.match(html, /0x7a…3f/);
  assert.match(userHeaderHtml({ address: POOL_ADDR, account: "" }), /Connect wallet/);
});

test("the control panel's header: its own band and tag, the role from the contract, its menu, no link to the trader's pages", () => {
  const owner = manageHeaderHtml({ address: POOL_ADDR, account: "0x12…ab", role: "owner", poolName: "Pool 0x0000…9001" });
  assert.match(owner, /Management panel/);
  assert.match(owner, /sp-mtag">Management/);
  assert.match(owner, /role read from contract/);
  assert.match(owner, /Pool creator · Pool 0x0000…9001/);
  assert.deepEqual(MANAGE_NAV.map((s) => s.label), ["Line switches", "Pool money", "Codes &amp; shares".replace("&amp;", "&"), "Withdraw & finish"]);
  assert.equal((owner.match(/aria-disabled="true"/g) || []).length, 2, "codes and withdrawing are held for the next sections");
  assert.match(owner, /Separate route\. Not linked from the trader's pages\. No buy buttons here\./);
  assert.ok(!/#\/sim\/|href="#\/"/.test(owner), "no link to the user panel");
  assert.match(manageHeaderHtml({ address: POOL_ADDR, account: "0x9…", role: "not-owner", poolName: "P" }), /No role in this pool/);
  assert.match(manageHeaderHtml({ address: POOL_ADDR, account: "", role: "no-wallet", poolName: "P" }), /Not connected/);
});

test("an address that is not the owner sees one sentence and no control; no wallet sees the way to connect", () => {
  assert.match(notOwnerHtml(), /This is the pool owner's panel\. Your address is not this pool's owner\./);
  assert.ok(!/<button|role="switch"|<table|\$\d/.test(notOwnerHtml()), "nothing of the pool, and no control");
  assert.match(noWalletHtml(), /Connect the wallet that owns the pool/);
  assert.ok(!/role="switch"|\$\d/.test(noWalletHtml()));
});

test("the footer keeps the testnet badge, and no screen calls the product a simulation or a paper exam", () => {
  assert.match(footerHtml(), /Testnet\. Mock USDC\. Unaudited\./);
  const rows = pool();
  const screens = [
    menuHtml(rows, 3_600n * U, "x"), switchesHtml(rows, 3_600n * U), footerHtml(), notOwnerHtml(), noWalletHtml(),
    userHeaderHtml({ address: POOL_ADDR, account: "" }), manageHeaderHtml({ address: POOL_ADDR, account: "", role: "owner", poolName: "P" }),
    moneyHtml(ownerMoney({ free: 1n, owed: 0n, reserved: 0n, held: 0n, staged: 0n, sharesDue: 0n, floor: 0n })),
    noComments(read("app", "views", "simpool.js")), noComments(read("app", "views", "simadmin.js")),
  ].join("\n");
  assert.ok(!/simulat|paper|fixed payout|fixed 8/i.test(screens));
});

test("the site's navigation names neither panel; neither view carries the other's controls or links", () => {
  const index = read("app", "index.html");
  const nav = index.match(/<nav[\s\S]*?<\/nav>/)[0];
  assert.ok(!/manage/.test(nav) && !/#\/sim/.test(nav));
  const trader = noComments(read("app", "views", "simpool.js"));
  assert.ok(!/manage|setLine|simadmin|reserved|sharesDue|owed/.test(trader), "no management and no owner's reads in the trader's view");
  const admin = noComments(read("app", "views", "simadmin.js"));
  assert.ok(!/\.buy\(|menuHtml|sp-buy|#\/sim\//.test(admin), "no purchase and no link to the trader's page in the owner's view");
});

test("the owner's panel reads the pool's money only after the wallet is the pool's creator", () => {
  const admin = read("app", "views", "simadmin.js");
  const view = admin.slice(admin.indexOf("export async function simAdminView"));
  assert.ok(view.indexOf("creator()") < view.indexOf("readOwnerPool(at)"));
  assert.ok(view.indexOf('role !== "owner"') < view.indexOf("readOwnerPool(at)"));
});

test("the user panel takes one purchase at a time and connects the wallet before it buys; the owner's page survives a failed re-read", () => {
  const trader = read("app", "views", "simpool.js");
  assert.match(trader, /let busy = false;/);
  assert.match(trader, /busy\)\s*return;|\|\| busy\) return;/);
  assert.ok(trader.indexOf("await chain.connect()") < trader.indexOf("busy = true;"), "connecting comes first and alone");
  assert.ok(!/if \(!chain\.currentSigner\(\)\) await chain\.connect\(\);\n  const signer/.test(trader), "the purchase itself does not connect");
  const admin = read("app", "views", "simadmin.js");
  assert.match(admin, /try \{\n      await simAdminView\(at, page\);\n    \} catch/);
});

test("the site's own header and footer are hidden on the two panels, and the testnet strip is in each page's footer", () => {
  const css = read("app", "style.css");
  assert.match(css, /body\[data-panel\] > header/);
  assert.match(css, /body\[data-panel\] > footer/);
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
