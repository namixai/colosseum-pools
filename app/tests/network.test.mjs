// node --test app/tests/network.test.mjs
//
// No page names a network in its own text: the site's settings say which network it is built for and what the pages
// call it (app/config.js, scripts/app_config.py), and the panels, the failure words and the plaque follow them. The
// committed settings are testnet's, so the app's own libraries are testnet's pages; a mainnet site of simulation pools
// is built in a directory of its own (app/tests/mainnet-site.mjs) and its libraries are loaded from there.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { CONFIG } from "../config.js";
import { isMainnet, networkName, networkWord } from "../lib/network.js";
import { networkMarks, SCREEN_TEXT } from "../lib/examscreen.js";
import { userHeaderHtml, manageHeaderHtml, footerHtml } from "../lib/simpanel.js";
import { friendly, failureHint, rateLimitedBy } from "../lib/ui.js";
import { mainnetSite } from "./mainnet-site.mjs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const POOL = "0x7A4999c02E749933af456d0Ac241a10584315812";
const MAINNET = { hyperliquidChain: "Mainnet", networkName: "Hyperliquid" };
const built = mainnetSite();
after(() => built.remove());

test("the name the pages write and the word before a service come from the settings", () => {
  assert.equal(networkName(), "Hyperliquid testnet");
  assert.equal(networkName(CONFIG), CONFIG.networkName);
  assert.equal(networkWord(), "testnet ");
  assert.equal(isMainnet(), false);
  assert.equal(networkName(MAINNET), "Hyperliquid");
  assert.equal(networkWord(MAINNET), "");
  assert.equal(isMainnet(MAINNET), true);
  // By Hyperliquid's own name for the chain, the one the settings sign under: nothing else decides it.
  assert.equal(isMainnet({ hyperliquidChain: "Testnet", chainId: 999, networkName: "Hyperliquid" }), false);
  assert.equal(isMainnet({}), false);
  assert.equal(isMainnet(null), false);
});

test("testnet's pages are as they were: the tag, the plaque, and the services named for testnet", () => {
  assert.match(userHeaderHtml({ address: POOL, account: "" }), /<span class="sp-tag">Testnet app<\/span>/);
  assert.match(footerHtml(), /<span class="sp-disc">Testnet\. Mock USDC\. Unaudited\.<\/span>/);
  assert.equal(rateLimitedBy({ status: 429 }), "Hyperliquid's testnet API");
  const down = Object.assign(new Error("x"), { status: 503 });
  assert.equal(failureHint(down), "A service this page reads could not be reached or answered with an error: the HyperEVM "
    + "testnet RPC, Hyperliquid's testnet API or the pool gateway. Reload in a moment.");
});

test("a mainnet site's settings are mainnet's, and its panels carry mainnet's plaque, no tag and no word of testnet", async () => {
  const { CONFIG: main } = await import(pathToFileURL(join(built.site, "config.js")).href);
  assert.equal(main.chainId, 999);
  assert.equal(main.networkName, "Hyperliquid");
  const network = await built.lib("network.js");
  assert.equal(network.networkName(), "Hyperliquid");
  assert.equal(network.networkWord(), "");
  assert.equal(network.isMainnet(), true);
  const panel = await built.lib("simpanel.js");
  const header = panel.userHeaderHtml({ address: POOL, account: "" });
  const footer = panel.footerHtml();
  const manage = panel.manageHeaderHtml({ address: POOL, account: "", role: "no-wallet", poolName: "Pool" });
  assert.doesNotMatch(header, /sp-tag/);
  assert.match(footer, /<span class="sp-disc">Mainnet\. Real USDC\. Unaudited\.<\/span>/);
  for (const html of [header, footer, manage, panel.notOwnerHtml(), panel.noWalletHtml()]) assert.doesNotMatch(html, /testnet|mock/i);
  // The trading screen's footer in the panel says the same plaque, with its own source line.
  assert.match(panel.footerHtml({ source: "x" }), /Mainnet\. Real USDC\. Unaudited\./);
});

test("a mainnet site names its services without testnet: Hyperliquid's API, the HyperEVM RPC", async () => {
  const ui = await built.lib("ui.js");
  assert.equal(ui.rateLimitedBy({ status: 429 }), "Hyperliquid's API");
  assert.match(ui.friendly(Object.assign(new Error("x"), { status: 429 })), /^Hyperliquid's API is refusing/);
  assert.match(friendly(Object.assign(new Error("x"), { status: 429 })), /^Hyperliquid's testnet API is refusing/);
  const hint = ui.failureHint(Object.assign(new Error("x"), { status: 503 }));
  assert.equal(hint, "A service this page reads could not be reached or answered with an error: the HyperEVM "
    + "RPC, Hyperliquid's API or the pool gateway. Reload in a moment.");
});

test("the plaque of a mainnet page is one text: the panels' and index.html's are the same", () => {
  assert.equal(SCREEN_TEXT.mainnetPlaque, "Mainnet. Real USDC. Unaudited.");
  assert.deepEqual(networkMarks(MAINNET), { plaque: SCREEN_TEXT.mainnetPlaque, tag: null });
  assert.ok(built.index.includes(`<div class="notice">${SCREEN_TEXT.mainnetPlaque}</div>`));
  assert.ok(built.index.includes(`<p>${SCREEN_TEXT.mainnetPlaque}</p>`));
  // And testnet's plaque is nowhere in a mainnet site's two written files.
  for (const text of [built.index, built.config]) assert.ok(!text.includes(SCREEN_TEXT.testnetPlaque) && !text.includes(SCREEN_TEXT.testnetTag));
});

// The pages a site of simulation pools draws, and the libraries under them: a pool's page and panels, an exam's screen,
// the site's first page, and what every page shares.
const DRAWN = [
  ...readdirSync(new URL("../views/", import.meta.url)).filter((f) => /^(sim|exam)/.test(f)).map((f) => `../views/${f}`),
  ...readdirSync(new URL("../lib/", import.meta.url)).filter((f) => /^(sim|exam|operator|stopscan|network|wallet|walletbutton|ui|nav|build|gentle|batch|multicall|positionlines|entrytrack|chartdata|indicators|hl\.|chain\.)/.test(f)).map((f) => `../lib/${f}`),
];
/** A file's code without its comments: what the page can say. Strings that hold `//` (an address of a site) stay. */
const code = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "").split("\n").map((line) => line.replace(/(^|[\s;{}(,])\/\/.*$/, "$1")).join("\n");

test("no page of the simulation pools names a network in its own words: each takes the name and the plaque from the settings", () => {
  assert.ok(DRAWN.length > 30, `${DRAWN.length} files`);
  // The words for each of the two networks stand in one place each, picked by the settings: the plaques and the tag
  // (examscreen.js) and the word before a service (network.js).
  const allowed = {
    "../lib/examscreen.js": [/^  testnetPlaque: "Testnet\. Mock USDC\. Unaudited\.",$/, /^  testnetTag: "Testnet app",$/,
      /^  mainnetPlaque: "Mainnet\. Real USDC\. Unaudited\.",$/, /^  if \(config\?\.hyperliquidChain === "Mainnet"\) return \{ plaque: SCREEN_TEXT\.mainnetPlaque, tag: null \};$/,
      /^  return \{ plaque: SCREEN_TEXT\.testnetPlaque, tag: SCREEN_TEXT\.testnetTag \};$/],
    "../lib/network.js": [/^export const isMainnet = \(config = CONFIG\) => config\?\.hyperliquidChain === "Mainnet";$/,
      /^export const networkWord = \(config = CONFIG\) => \(isMainnet\(config\) \? "" : "testnet "\);$/],
    // Which network's data an exam is shown beside, by the settings' own chain name.
    "../lib/exam.js": [/^    rehearsal: config\.hyperliquidChain !== "Mainnet" \};$/],
    // What mainnet's settings must carry for a pool's page to open at all: asked of the settings, not said by a page.
    "../lib/simsite.js": [/^import \{ isMainnet \} from "\.\/network\.js";$/, /^  return isMainnet\(config\) \? "none" : "any";$/,
      /^  if \(isMainnet\(config\) && !sameAddress\(token, config\.usdc\)\) \{$/],
  };
  for (const path of DRAWN) {
    const named = code(read(path)).split("\n").filter((line) => /testnet|mainnet|mock/i.test(line))
      .filter((line) => !(allowed[path] ?? []).some((ok) => ok.test(line)));
    assert.deepEqual(named, [], `${path} names a network itself`);
  }
  // The kick over each page's title is the settings' name for the network.
  for (const [path, line] of [
    ["../views/simpool.js", /<div class="sp-kick">Pool \$\{esc\(chain\.short\(at\)\)\} · \$\{esc\(networkName\(\)\)\}<\/div>/],
    ["../views/simtrader.js", /· Pool \$\{esc\(chain\.short\(at\)\)\} · \$\{esc\(networkName\(\)\)\}<\/div>`/],
    ["../views/simadmin.js", /<div class="sp-kick">Owner panel · \$\{esc\(name\)\} · \$\{esc\(networkName\(\)\)\}<\/div>/],
    ["../lib/simoperator.js", /<div class="sp-kick">Operator panel · \$\{esc\(name\)\} · \$\{esc\(network\)\}<\/div>`/],
  ]) assert.match(read(path), line, path);
  assert.match(read("../lib/simoperator.js"), /export function operatorHtml\(model, \{ name, role, network = networkName\(\) \}\) \{/);
  // Every panel's header and footer take the marks of the site's network unless a page hands its own.
  assert.match(read("../lib/simpanel.js"), /const SITE_MARKS = Object\.freeze\(networkMarks\(CONFIG\)\);/);
  assert.match(read("../lib/simpanel.js"), /marks = SITE_MARKS \}\) \{/);
  assert.match(read("../lib/simpanel.js"), /export function footerHtml\(\{ marks = SITE_MARKS, /);
});

test("no page of the simulation pools has a faucet, test money or a button for it, on either network", () => {
  for (const path of DRAWN) assert.doesNotMatch(code(read(path)), /faucet|test dollar|test money|\bmint\(/i, path);
  assert.doesNotMatch(built.index, /faucet/i);
  assert.doesNotMatch(built.config, /faucet/i);
});

test("the wallet is offered the chain the settings name: its id, its name, its currency and its public node", async () => {
  const { ensureChain } = await built.lib("wallet.js");
  const { CONFIG: main } = await import(pathToFileURL(join(built.site, "config.js")).href);
  const asked = [];
  let on = "0x1";
  const eth = { request: async ({ method, params }) => {
    asked.push({ method, params });
    if (method === "eth_chainId") return on;
    if (method === "wallet_switchEthereumChain") throw Object.assign(new Error("Unrecognized chain ID"), { code: 4902 });
    if (method === "wallet_addEthereumChain") on = params[0].chainId;
    return null;
  } };
  assert.equal(await ensureChain(eth, main), "added");
  assert.deepEqual(asked.find((a) => a.method === "wallet_switchEthereumChain").params, [{ chainId: "0x3e7" }]);
  assert.deepEqual(asked.find((a) => a.method === "wallet_addEthereumChain").params, [{
    chainId: "0x3e7", chainName: "HyperEVM", nativeCurrency: { name: "HYPE", symbol: "HYPE", decimals: 18 },
    rpcUrls: ["https://rpc.hyperliquid.xyz/evm"],
  }]);
  // The read provider and the wallet take the node and the chain from the same settings, and from nowhere else.
  const chain = read("../lib/chain.js");
  assert.match(chain, /super\(CONFIG\.rpc, CONFIG\.chainId, PROVIDER_OPTIONS\);/);
  assert.match(chain, /await ensureChain\(window\.ethereum, CONFIG\);/);
  assert.match(chain, /const response = await fetch\(CONFIG\.rpc, \{/);
  assert.doesNotMatch(code(chain), /https?:\/\//, "an address of a node or a site written into chain.js");
});
