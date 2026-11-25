// node --test app/tests/terms.test.mjs
//
// The Terms of Use of a mainnet site (Marketing's second text, 25 November 2026, with no company named) and its picture
// of a shared link (24 November): the text drawn whole by app/lib/terms.js, its menu the first pool's plan, its numbers
// the contracts' and the plan's, and one place still open, the date it was last updated, which is set on the day the
// site goes up; and a mainnet build's page naming the picture it carries. scripts/test-pack-site.sh holds that a
// mainnet site is not packed while any place is open.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { TERMS_FILE, termsHtml } from "../lib/terms.js";
import { mainnetSite } from "./mainnet-site.mjs";

const read = (...parts) => readFileSync(new URL(`../../${parts.join("/")}`, import.meta.url), "utf8");
const TEXT = read("app", TERMS_FILE);
const HTML = termsHtml(TEXT);
/** What a reader sees of the page: its text without the tags, the entities read back. */
const seen = (html) => html.replace(/<[^>]+>/g, " ").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", '"')
  .replaceAll("&#39;", "'").replaceAll("&amp;", "&").replace(/\s+/g, " ").trim();
const built = mainnetSite();
after(() => built.remove());

test("the Terms of Use are Marketing's text, with the one place left open, the date, until the day the site goes up", () => {
  assert.equal(TERMS_FILE, "data/terms-mainnet.md");
  assert.ok(TEXT.startsWith("# Terms of Use\n\nUpools at https://pools.usenami.io/\n\nLast updated: "));
  // The date is set on the day the site goes up (the owner, 24 November); until then it is a place the packer refuses.
  const updated = TEXT.split("\n")[4].slice("Last updated: ".length);
  if (updated !== "[TO CONFIRM: effective date]") {
    assert.match(updated, /^(January|February|March|April|May|June|July|August|September|October|November|December) [1-9]\d?, 20\d\d$/,
      "a date, as November 25, 2026");
    assert.ok(!Number.isNaN(Date.parse(updated)), updated);
  }
  assert.deepEqual([...TEXT.matchAll(/\[TO CONFIRM: ([^\]]+)\]/g)].map((m) => m[1]), updated.startsWith("[") ? ["effective date"] : []);
  assert.equal(TEXT.split("[TO CONFIRM").length - 1, updated.startsWith("[") ? 1 : 0, "no place marked any other way");
  // The contact address is put together here, so that the hygiene gate sees it only where the Terms publish it.
  const contact = ["business", "usenami.io"].join("@");
  assert.ok(TEXT.endsWith(`## 16. Contact\n\n${contact}\n\nhttps://pools.usenami.io/\n`));
  // No company (the owner, 24 November): the operator is Upools, with no name, address, number, law or venue.
  assert.ok(TEXT.includes('In these Terms, "Upools", "we", "us" and "the platform" mean the operator of Upools.'));
  assert.doesNotMatch(TEXT, /legal entity|registered address|registration number|governing law|arbitration|jurisdiction of|courts of/i);
  // An objection to a stop is written to the contact within the 14 days, as on Paradex.
  assert.ok(TEXT.includes(`**How to object.** Write to ${contact} within 14 days after the stop.`));
  // The owner's word of 24 November, and no word that sells a gain.
  assert.doesNotMatch(TEXT, /testnet|mock|unaudited|paper|simulat|invest|profit|guarantee|returns/i);
  assert.match(TEXT, /^[\x0a\x20-\x7e]*$/, "plain ASCII, as written");
});

test("the menu of the Terms is the first pool's plan: each tier's price for each size, and the lines it opens with", () => {
  const plan = JSON.parse(read("deployments", "plans", "sim-first-pool.json")).menu;
  const rows = TEXT.split("\n").filter((line) => /^\| \d% \/ \d+% \|/.test(line));
  assert.equal(rows.length, 4);
  const header = TEXT.split("\n").find((line) => line.startsWith("| Loss limit / target |"));
  const sizes = header.split("|").slice(2, -1).map((c) => Number(c.trim().replaceAll(",", "")));
  assert.deepEqual(sizes, [5000, 10000, 25000, 50000, 100000, 250000]);
  for (const row of rows) {
    const [tier, ...prices] = row.split("|").slice(1, -1).map((c) => c.trim());
    const [dd, target] = tier.split(" / ").map((p) => Number(p.replace("%", "")) * 100);
    prices.forEach((price, k) => {
      const line = plan.find((m) => m.size_usdc === sizes[k] && m.max_drawdown_bps === dd && m.target_bps === target);
      assert.ok(line, `${tier} at ${sizes[k]}`);
      assert.equal(price, `${line.price_usdc.toLocaleString("en-US")} USDC`, `${tier} at ${sizes[k]}`);
    });
  }
  // The first pool opens with 5,000 and 10,000 on and the rest listed and off (25 November). Every line the Terms say
  // is on is on in the plan; the plan of main still has 25,000 on until the first pool's own change lands.
  assert.ok(TEXT.includes("The pool opens with the lines of 5,000 and 10,000 switched on. The lines of 25,000, 50,000, 100,000 and 250,000 are listed and switched off."));
  const on = [5000, 10000];
  for (const size of on) assert.ok(plan.filter((m) => m.size_usdc === size).every((m) => m.on), `${size} is on in the plan`);
  // A line is switched on only with the pool's free money for COVER_PAYOUTS of its payouts, each PAYOUT_BPS of its size.
  const cover = Number(read("src", "sim", "SimPool.sol").match(/^uint256 constant COVER_PAYOUTS = (\d+);$/m)[1]);
  const payoutBps = Number(read("src", "sim", "SimPoolFactory.sol").match(/ PAYOUT_BPS = (\d+);/)[1]);
  const needs = TEXT.match(/free money for two payouts of that line: (.+?)\. Until/)[1].replace(" and ", ", ").split(", ");
  assert.equal(cover, 2, "two payouts");
  assert.deepEqual(needs.map((n) => Number(n.match(/for ([\d,]+)$/)[1].replaceAll(",", ""))), sizes.filter((s) => !on.includes(s)));
  for (const n of needs) {
    const [, need, size] = n.match(/^([\d,]+) USDC for ([\d,]+)$/).map((v) => Number(v.replaceAll(",", "")));
    assert.equal(need, (cover * size * payoutBps) / 10000, n);
  }
  assert.ok(TEXT.includes(`you are paid a fixed ${payoutBps / 100}% of the account size, once, in USDC.`));
});

test("every number of the Terms' rules is the contracts' or the first pool's plan's", () => {
  const plan = JSON.parse(read("deployments", "plans", "sim-first-pool.json"));
  const exam = read("src", "exam", "PaperExam.sol");
  const factory = read("src", "sim", "SimPoolFactory.sol");
  const constant = (src, name) => src.match(new RegExp(` ${name} = ([^;]+);`))[1];
  const days = (v) => Number(v.replace(/ days$/, ""));
  assert.equal(constant(exam, "CHECKPOINT_WINDOW"), "15 minutes");
  assert.ok(TEXT.includes("(in the day's first 15 minutes)") && TEXT.includes("taken within the first 15 minutes of the UTC day"));
  for (const terms of [plan.exam, plan.funded]) {
    assert.equal(terms.fill_delay, 4);
    assert.equal(terms.fill_window, 3);
    assert.equal(terms.cost_ppm, 450);
    assert.equal(terms.step_duration_s, 30 * 86400);
    assert.equal(terms.min_trading_days, 0);
  }
  for (const rules of [plan.exam_rules, plan.funded_rules]) {
    assert.equal(rules.daily_loss_bps, 300);
    assert.deepEqual(rules.assets, ["BTC", "ETH", "SOL"]);
    assert.deepEqual(rules.asset_leverage_x100, [1000, 1000, 500]);
  }
  assert.ok(TEXT.includes("Equity may fall no more than 3% below the day's first snapshot."));
  assert.ok(TEXT.includes("At most 10x on BTC and ETH, and at most 5x on SOL."));
  assert.ok(TEXT.includes("Thirty days at most per stage. There is no minimum number of trading days."));
  assert.ok(TEXT.includes("no sooner than the fourth block after the block it is placed in, at the price of the block that fills it. If it is not filled within 3 blocks from then, it lapses at no cost."));
  assert.equal(plan.max_worse_bps, 150);
  assert.equal(plan.from_book, true);
  assert.ok(TEXT.includes("A fill takes the order book's price, never better than the oracle's price and at most 1.5% worse. Each fill pays 0.045% of its notional"));
  // A stop or a target acts fillDelay blocks after it is set; with the filler silent, anyone may show it triggered and,
  // FALLBACK_BLOCKS later, carry it out.
  assert.equal(constant(exam, "FALLBACK_BLOCKS"), "60");
  assert.ok(TEXT.includes("They take effect no sooner than 4 blocks after they are set."));
  assert.ok(TEXT.includes("The platform's filler executes triggered orders. If the filler does not, anyone may record that the order has triggered and execute it 60 blocks later."));
  assert.equal(plan.fund_window_s, 7 * 86400);
  assert.ok(TEXT.includes("you start the second stage within seven days."));
  assert.equal(constant(factory, "PAYOUT_DELAY"), "72 hours");
  assert.equal(days(constant(factory, "PAYOUT_HARD")), 7);
  assert.ok(TEXT.includes("no sooner than 72 hours after it is claimed, and then once the pool's approver has approved it, or 7 days after it is claimed without approval."));
  assert.equal(plan.payouts_per_day, 2);
  assert.equal(constant(factory, "PAYOUT_DAY"), "24 hours");
  assert.ok(TEXT.includes("Each pool pays at most two payouts a day, counted over any 24 hours."));
  assert.equal(days(constant(factory, "STOP_RESERVE")), 14);
  assert.ok(TEXT.includes("A stopped payout stays in the pool for at least 14 days"));
  assert.deepEqual([constant(factory, "MIN_PARTNER_BPS"), constant(factory, "MAX_PARTNER_BPS")], ["4000", "6000"]);
  assert.ok(TEXT.includes("none, or 40% to 60%."));
  assert.equal(days(constant(factory, "MIN_TERM")), 184);
  assert.ok(TEXT.includes("A pool runs at least 6 months (184 days) from its opening."));
  assert.deepEqual([days(constant(factory, "WITHDRAW_DELAY")), days(constant(factory, "WITHDRAW_WINDOW"))], [7, 3]);
  assert.ok(TEXT.includes("The money leaves no sooner than 7 days after the request and within 3 days after that"));
  assert.equal(constant(factory, "FLOOR_PAYOUTS"), "3");
  assert.ok(TEXT.includes("and above three payouts of the largest line"));
  assert.equal(constant(factory, "FEE_BPS"), "50");
  assert.ok(TEXT.includes("- 0.5% of each exam price.\n- 0.5% of each withdrawal from a pool.\n- 1 HYPE to open a pool.\n"));
  assert.match(read("ops", "deploy_sim.py"), /^CREATION_FEES = \{"mainnet": HYPE, /m, "1 HYPE on mainnet");
});

test("the page draws every line of the text and nothing else: the headings, the lists, the table and the bold words", () => {
  assert.match(HTML, /^<h2>Terms of Use<\/h2>\n<p>Upools at https:\/\/pools\.usenami\.io\/<\/p>\n/);
  assert.deepEqual([...HTML.matchAll(/<h3>(.*?)<\/h3>/g)].map((m) => m[1].replace(/^\d+\. /, "")), ["Who we are and what these Terms cover",
    "What an exam is", "The menu", "Exam rules", "Orders and fills", "Passing, the second stage and the payout", "Stopped payouts",
    "Trading group codes", "Pool creators", "Platform fees", "Risks you accept", "No warranties", "Limitation of liability",
    "Who may use Upools", "Changes to these Terms", "Contact"]);
  assert.equal([...HTML.matchAll(/<table>/g)].length, 1);
  assert.match(HTML, /<div class="scroll"><table><thead><tr><th>Loss limit \/ target<\/th><th>5,000<\/th><th>10,000<\/th><th>25,000<\/th><th>50,000<\/th><th>100,000<\/th><th>250,000<\/th><\/tr><\/thead><tbody><tr><td>3% \/ 9%<\/td><td>20 USDC<\/td>/);
  assert.equal([...HTML.matchAll(/<tr>/g)].length, 5, "a header and the four tiers; the line under the header only marks it");
  assert.doesNotMatch(HTML, /\|---/);
  assert.equal([...HTML.matchAll(/<ol>/g)].length, 1);
  assert.match(HTML, /<ol><li>Opposite positions with another account on the platform\.<\/li><li>Reason 1, and the two wallets are linked\.<\/li>/);
  assert.equal([...HTML.matchAll(/<ul>/g)].length, 8);
  assert.match(HTML, /<li><strong>Loss limit\.<\/strong> Each stage has a loss limit/);
  assert.doesNotMatch(HTML, /\*\*|^#|\n#/m);
  // Every line of the text is on the page, as written once its marks are read, and the page has no word more.
  const lines = TEXT.split("\n").filter((l) => l.trim() && !/^\|---/.test(l)).map((line) => (/^#+ /.test(line)
    ? line.replace(/^#+ /, "") : line.replace(/^- /, "").replace(/^\d+\. /, "")).replaceAll("**", "")
    .split("|").map((c) => c.trim()).filter(Boolean).join(" "));
  for (const words of lines) assert.ok(seen(HTML).includes(words), words);
  assert.equal(seen(HTML), lines.join(" "), "no word added, none lost");
});

test("the text is never taken as markup: anything but its own forms is escaped", () => {
  assert.equal(termsHtml("<script>alert(1)</script> & \"x\""), "<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;</p>");
  assert.equal(termsHtml("## <b>x</b>\n- **<i>y</i>** z"), "<h3>&lt;b&gt;x&lt;/b&gt;</h3>\n<ul><li><strong>&lt;i&gt;y&lt;/i&gt;</strong> z</li></ul>");
  assert.equal(termsHtml("| a | <b> |\n|---|---|\n| 1 | 2 |"), '<div class="scroll"><table><thead><tr><th>a</th><th>&lt;b&gt;</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>');
  assert.equal(termsHtml("one\ntwo\n\nthree"), "<p>one two</p>\n<p>three</p>");
  // A line with leading spaces is a paragraph's like any other, and the page is drawn: no line is left untaken
  // (review of #346, where it looped for ever).
  assert.equal(termsHtml("  leading spaces\n  more spaces"), "<p>leading spaces more spaces</p>");
  assert.equal(termsHtml("one\n   \n\t two\n  - not a list\n  ## not a heading"), "<p>one</p>\n<p>two - not a list ## not a heading</p>");
  assert.equal(termsHtml("## A\n  indented\n- b"), "<h3>A</h3>\n<p>indented</p>\n<ul><li>b</li></ul>");
  assert.equal(termsHtml(""), "");
});

test("a mainnet build names its own picture of a shared link, which it carries, and the title and description it shows", () => {
  const head = built.index.slice(0, built.index.indexOf("</head>"));
  const meta = (re) => head.match(re)?.[1];
  assert.equal(meta(/<title>([^<]+)<\/title>/), "Upools: trading exams on Hyperliquid");
  assert.equal(meta(/<meta property="og:title" content="([^"]+)">/), meta(/<title>([^<]+)<\/title>/));
  assert.equal(meta(/<meta name="description" content="([^"]+)">/),
    "Trading exams on BTC, ETH and SOL on Hyperliquid mainnet. The price is paid in USDC. No real money is traded in the exam.");
  assert.equal(meta(/<meta property="og:description" content="([^"]+)">/), meta(/<meta name="description" content="([^"]+)">/));
  assert.ok(meta(/<meta name="description" content="([^"]+)">/).length <= 160);
  assert.equal(meta(/<meta property="og:image:alt" content="([^"]+)">/), "Upools on Hyperliquid mainnet. BTC, ETH, SOL. USDC.");
  const image = meta(/<meta property="og:image" content="https:\/\/pools\.usenami\.io\/([^"]+)">/);
  assert.equal(image, "brand/og-mainnet-1200x630.png");
  assert.ok(existsSync(join(built.site, image)), "the picture is in the site the page is packed with");
  const bytes = readFileSync(join(built.site, image));
  assert.equal(createHash("sha256").update(bytes).digest("hex"), "370eca1884597718bfc189c102dabe1ee9a99dabfcb502dfa167d451fd4b2f70", "Marketing's picture");
  assert.equal(bytes.subarray(1, 4).toString("latin1"), "PNG");
  assert.deepEqual([bytes.readUInt32BE(16), bytes.readUInt32BE(20), bytes[24], bytes[25]], [1200, 630, 8, 2], "1200×630, 8-bit RGB, no alpha");
  assert.ok(existsSync(join(built.site, TERMS_FILE)), "and the Terms of Use it links");
  assert.match(built.index, /<a href="#\/terms">Terms of Use<\/a>/);
});

test("testnet's page is as it was: its own picture and title, and no word of mainnet's Terms of Use", () => {
  const page = read("app", "index.html");
  assert.match(page, /<title>Pools on Hyperliquid · testnet<\/title>/);
  assert.match(page, /<meta property="og:image" content="https:\/\/pools\.usenami\.io\/brand\/og-image-1200x630\.png">/);
  assert.doesNotMatch(page, /og-mainnet|Terms of Use|Upools: trading exams/);
  assert.match(page, /<a href="#\/terms">Terms<\/a>/, "the demo's terms, as before");
});
