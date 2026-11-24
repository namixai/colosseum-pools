// node --test app/tests/terms.test.mjs
//
// The Terms of Use of a mainnet site and its picture of a shared link (Marketing, 24 November 2026): the text as it
// was written, drawn whole by app/lib/terms.js, its menu the first pool's plan, its ten places to fill in still open;
// and a mainnet build's page naming the picture it carries. scripts/test-pack-site.sh holds that a mainnet site is not
// packed while any of those places is open.
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

test("the Terms of Use are Marketing's text, with its ten places to fill in still marked as they were written", () => {
  assert.equal(TERMS_FILE, "data/terms-mainnet.md");
  assert.ok(TEXT.startsWith("# Terms of Use\n\nUpools at https://pools.usenami.io/\n\nLast updated: [TO CONFIRM: effective date]\n"));
  // The contact address is put together here, so that the hygiene gate sees it only where the Terms publish it.
  assert.ok(TEXT.endsWith(`## 15. Contact\n\n${["business", "usenami.io"].join("@")}\n\nhttps://pools.usenami.io/\n`));
  assert.deepEqual([...TEXT.matchAll(/\[TO CONFIRM: ([^\]]+)\]/g)].map((m) => m[1]), [
    "effective date", "legal entity name", "registered address", "company registration number",
    "objection procedure, what evidence to send, and who decides",
    "disclaimer of warranties and limitation of liability wording, reviewed by counsel",
    "minimum age, restricted countries and persons, sanctions screening, whether identity checks apply",
    "how and when we may change these Terms and how users are told", "governing law",
    "dispute venue or arbitration body, and any class action waiver",
  ]);
  assert.equal(TEXT.split("[TO CONFIRM").length - 1, 10, "no place marked any other way");
  // The owner's word of 24 November, and no word that sells a gain.
  assert.doesNotMatch(TEXT, /unaudited|invest|profit|guarantee|returns/i);
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
  const on = plan.filter((m) => m.on).map((m) => m.size_usdc);
  assert.deepEqual([...new Set(on)], [5000, 10000, 25000]);
  assert.ok(TEXT.includes("The pool opens with the lines of 5,000, 10,000 and 25,000 switched on. The lines of 50,000, 100,000 and 250,000 are listed and switched off."));
});

test("the page draws every line of the text and nothing else: the headings, the lists, the table and the bold words", () => {
  assert.match(HTML, /^<h2>Terms of Use<\/h2>\n<p>Upools at https:\/\/pools\.usenami\.io\/<\/p>\n/);
  assert.deepEqual([...HTML.matchAll(/<h3>(.*?)<\/h3>/g)].map((m) => m[1].replace(/^\d+\. /, "")), ["Who we are and what these Terms cover",
    "What an exam is", "The menu", "Exam rules", "Orders and fills", "Passing, the second stage and the payout", "Stopped payouts",
    "Trading group codes", "Pool creators", "Platform fees", "Risks you accept", "Who may use Upools", "Changes to these Terms",
    "Governing law and disputes", "Contact"]);
  assert.equal([...HTML.matchAll(/<table>/g)].length, 1);
  assert.match(HTML, /<div class="scroll"><table><thead><tr><th>Loss limit \/ target<\/th><th>5,000<\/th><th>10,000<\/th><th>25,000<\/th><th>50,000<\/th><th>100,000<\/th><th>250,000<\/th><\/tr><\/thead><tbody><tr><td>3% \/ 9%<\/td><td>20 USDC<\/td>/);
  assert.equal([...HTML.matchAll(/<tr>/g)].length, 5, "a header and the four tiers; the line under the header only marks it");
  assert.doesNotMatch(HTML, /\|---/);
  assert.equal([...HTML.matchAll(/<ol>/g)].length, 1);
  assert.match(HTML, /<ol><li>Opposite positions with another account on the platform\.<\/li><li>Reason 1, and the two wallets are linked\.<\/li>/);
  assert.equal([...HTML.matchAll(/<ul>/g)].length, 5);
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
