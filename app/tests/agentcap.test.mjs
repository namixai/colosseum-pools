// node --test app/tests/agentcap.test.mjs
//
// The page says when an agent could not trade a pool through this repository's trader client (app/lib/agentcap.js).
// The two numbers behind it are the client's, so they are read back from agents/ here: a cap or a minimum changed
// there and not here turns this file red, not the page wrong.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ORDER_SHARE_OF_RULE, MIN_ORDER_USDC, agentOrderCap, minCapitalForAgent, agentTradability, agentNote, agentCardLine,
  agentFormWarning,
} from "../lib/agentcap.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const DESK = text("../../agents/desk.py");
const CLIENT = text("../../agents/client.py");
const number = (source, pattern, what) => {
  const m = source.match(pattern);
  assert.ok(m, `${what} is where this test looks for it`);
  return Number(m[1]);
};

test("the share of the rule and the smallest order are the trader client's own numbers", () => {
  // The default the desk carries, and the number the command-line client hands it: one figure, written twice there.
  assert.equal(number(DESK, /^ {4}max_order_share_of_rule: float = ([\d.]+)$/m, "the desk's share"), ORDER_SHARE_OF_RULE);
  assert.equal(number(CLIENT, /^WINDOW_MAX_ORDER_SHARE_OF_RULE = ([\d.]+)$/m, "the client's share"), ORDER_SHARE_OF_RULE);
  assert.equal(number(DESK, /^MIN_ORDER_USDC = ([\d.]+)$/m, "the smallest order"), MIN_ORDER_USDC);
  // The formula, as the desk writes it: equity times the leverage rule times the share.
  assert.match(DESK, /by_rule = max\(equity, 0\.0\) \* leverage_x100 \/ 100 \* self\.max_order_share_of_rule/);
  // And the two refusals an opening order meets: under the minimum, or over the cap.
  assert.match(DESK, /if notional < MIN_ORDER_USDC:/);
  assert.match(DESK, /if notional > cap:/);
});

test("the cap is the client's arithmetic, and a pool is an agent's to trade only from the minimum up", () => {
  // The challenge sold on 4 Oct 2026: 3 USDC at 5x. The client said "over this session's cap of 6.00 per order".
  assert.equal(agentOrderCap(3, 500).toFixed(2), "6.00");
  assert.equal(agentOrderCap(-1, 500), 0);
  const small = agentTradability({ capital: 3, fundedCapital: 30, leverageX100: 500 });
  assert.equal(small.challenge, false);
  assert.equal(small.funded, true);
  assert.equal(small.fundedCap, 60);
  // The edge at 5x is 5 USDC, and at 3x it is 8.34: a cent under either and no order fits.
  assert.equal(agentTradability({ capital: 5, fundedCapital: 50, leverageX100: 500 }).challenge, true);
  assert.equal(agentTradability({ capital: 4.99, fundedCapital: 50, leverageX100: 500 }).challenge, false);
  assert.equal(agentTradability({ capital: 8.34, fundedCapital: 84, leverageX100: 300 }).challenge, true);
  assert.equal(agentTradability({ capital: 8.33, fundedCapital: 84, leverageX100: 300 }).challenge, false);
  assert.equal(minCapitalForAgent(500), 5);
  assert.equal(minCapitalForAgent(300), 8.34);
  assert.equal(minCapitalForAgent(100), 25);
  assert.equal(minCapitalForAgent(0), Infinity);
  // The pools the site lists as live are not caught by it: 70 at 3x, and the live run's 7 at 5x.
  assert.equal(agentTradability({ capital: 70, fundedCapital: 70, leverageX100: 300 }).challenge, true);
  assert.equal(agentTradability({ capital: 7, fundedCapital: 70, leverageX100: 500 }).challenge, true);
  // A funded stage smaller than the minimum is said too, by its own capital.
  const thin = agentTradability({ capital: 20, fundedCapital: 4, leverageX100: 500 });
  assert.deepEqual([thin.challenge, thin.funded], [true, false]);
});

test("the words say whose refusal it is, with the number, and nothing where an agent can trade", () => {
  const ok = agentTradability({ capital: 70, fundedCapital: 70, leverageX100: 300 });
  assert.equal(agentNote(ok), "");
  assert.equal(agentCardLine(ok), "");
  assert.equal(agentFormWarning(ok, 300), "");

  const small = agentTradability({ capital: 3, fundedCapital: 30, leverageX100: 500 });
  const note = agentNote(small);
  assert.match(note, /^An agent using this repository's trader client can't open a position in this pool's challenge:/);
  assert.match(note, /caps one order at 6\.00 USDC \(0\.4 of the leverage rule on the stage's capital\)/);
  assert.match(note, /Hyperliquid takes no order under 10 USDC\./);
  // Not the contract's rule and not the gateway's: the site's own trade panel is not held to it, and the page says so
  // rather than call the pool untradable.
  assert.match(note, /the client's own limit, not the contract's or the gateway's: the trade panel on this site is not held to it\.$/);
  assert.doesNotMatch(note, /can't be traded|untradable|nobody|the gateway refuses/i);
  assert.equal(agentCardLine(small),
    "An agent using this repository's client can't trade its challenge: one order is capped at 6.00 USDC, under Hyperliquid's minimum of 10.");

  const thin = agentTradability({ capital: 20, fundedCapital: 4, leverageX100: 500 });
  assert.match(agentNote(thin), /in this pool's funded stage: the client caps one order at 8\.00 USDC/);

  // The form adds what would make it tradable, for the leverage typed in.
  assert.match(agentFormWarning(small, 500), /With 5× leverage the challenge capital has to be at least 5\.00 USDC for such an agent to trade it\.$/);
  assert.match(agentFormWarning(thin, 500), /With 5× leverage the funded capital has to be at least 5\.00 USDC/);
  const both = agentTradability({ capital: 9, fundedCapital: 9, leverageX100: 250 });
  assert.match(agentFormWarning(both, 250), /With 2\.5× leverage the challenge capital and the funded capital have to be at least 10\.00 USDC/);
});

test("the list, the pool page and the form all say it, from the pool's own terms and rules", () => {
  const pools = text("../views/pools.js");
  const pool = text("../views/pool.js");
  const terms = /agentTradability\(\{ capital: Number\(terms\.capital\) \/ 1e6,\s+fundedCapital: Number\(terms\.fundedCapital\) \/ 1e6, leverageX100: rules\.maxLeverageX100 \}\)/;
  // The card and the page read the same three numbers of the pool, and say nothing about an archived pool.
  assert.match(pools, new RegExp(`const agent = archived \\? "" : agentCardLine\\(${terms.source}\\);`));
  assert.match(pools, /\$\{agent \? `<p class="small">\$\{esc\(agent\)\}<\/p>` : ""\}/);
  assert.match(pool, new RegExp(`const agent = archived \\? "" : agentNote\\(${terms.source}\\);`));
  // On the pool page twice: under the pool's name, and in the buy card above the outcomes and the button that takes
  // the money.
  assert.equal(pool.match(/\$\{agent \? `<p class="notice">\$\{esc\(agent\)\}<\/p>` : ""\}/g)?.length, 2);
  assert.match(pool, /\$\{agent \? `<p class="notice">\$\{esc\(agent\)\}<\/p>` : ""\}\s+\$\{outcomesHtml\(terms, fee\)\}\s+<button id="buy-btn">Pay and start<\/button>/);
  // The form: the warning sits above the button, is drawn before anything is sent and again on every change.
  assert.match(pools, /<p class="small" id="agent-warning"><\/p>\s+<button type="button" id="create">Create the pool<\/button>/);
  assert.match(pools, /agentWarning\(page\);\s+wire\(\$\("#create", page\)/);
  assert.match(pools, /const leverageX100 = Math\.round\(Number\(f\.get\("lev"\)\) \* 100\);\s+const words = agentFormWarning\(agentTradability\(\{ capital: Number\(f\.get\("capital"\)\),\s+fundedCapital: Number\(f\.get\("funded"\)\), leverageX100 \}\), leverageX100\);/);
  assert.match(pools, /form\.addEventListener\("input", show\);\s+show\(\);\s+\}\s+\/\*\*\s+\* The floor/);
  // A warning, not a refusal: the contract takes such a pool and a person can trade it from the site.
  assert.doesNotMatch(pools, /agentFormWarning[\s\S]{0,400}throw new Error/);
});
