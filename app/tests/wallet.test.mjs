// node --test app/tests/wallet.test.mjs
//
// Connecting a wallet on a pool page failed on the live site (1 Oct 2026): the page drew itself twice, ethers sent
// both renders' reads as one batch of 22, and the public RPC refused it with -32010. The page then blamed a mismatch
// with the deployment and said reloading would not help. One render per connection; the read provider never sends
// more than the node takes; and a refused batch is named for what it is.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { wireWallet } from "../lib/walletbutton.js";
import { MAX_BATCH, PROVIDER_OPTIONS } from "../lib/batch.js";
import { friendly, failureHint, batchRefused } from "../lib/ui.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

/** chain.js's contract, faked: connect() tells every onAccount listener about the address, then resolves. */
function harness({ fail = false } = {}) {
  const listeners = [];
  const calls = { route: 0, paint: [], report: [] };
  let click = null;
  wireWallet({
    button: { addEventListener: (event, fn) => { if (event === "click") click = fn; } },
    connect: async () => {
      if (fail) throw new Error("Cancelled");
      listeners.forEach((fn) => fn("0xabc"));
      return "0xabc";
    },
    onAccount: (fn) => listeners.push(fn),
    paint: (addr) => calls.paint.push(addr),
    route: () => { calls.route += 1; },
    report: (err) => calls.report.push(err.message),
  });
  return { calls, click: () => click(), change: (addr) => listeners.forEach((fn) => fn(addr)) };
}

test("connecting a wallet draws the page once", async () => {
  const h = harness();
  await h.click();
  assert.equal(h.calls.route, 1, "two renders start their reads together and ethers sends them as one batch");
  assert.deepEqual(h.calls.paint, ["0xabc"]);
  // A change of account in the wallet: one render too.
  h.change(null);
  assert.equal(h.calls.route, 2);
  // A refused connection draws nothing and says why.
  const bad = harness({ fail: true });
  await bad.click();
  assert.equal(bad.calls.route, 0);
  assert.deepEqual(bad.calls.paint, [null]);
  assert.deepEqual(bad.calls.report, ["Cancelled"]);
  // Held to the real pieces: chain.connect() tells the listeners, and app.js wires the button through wireWallet.
  assert.match(text("../lib/chain.js"), /signer = await provider\.getSigner\(\);\s*listeners\.forEach\(\(fn\) => fn\(signer\.address\)\);/);
  const app = text("../app.js");
  assert.match(app, /wireWallet\(\{/);
  const button = app.slice(app.indexOf("function walletButton()"), app.indexOf("// Answered in this tab"));
  assert.doesNotMatch(button, /route\(\)/, "the button redraws the page itself again");
});

test("the read provider never sends a batch larger than the node takes", () => {
  assert.equal(MAX_BATCH, 20);
  assert.ok(PROVIDER_OPTIONS.batchMaxCount <= MAX_BATCH);
  assert.equal(PROVIDER_OPTIONS.staticNetwork, true);
  const chain = text("../lib/chain.js");
  assert.match(chain, /export const readProvider = new ethers\.JsonRpcProvider\(CONFIG\.rpc, CONFIG\.chainId, PROVIDER_OPTIONS\);/);
  // No other provider of the app reads the chain around it.
  for (const file of ["../lib/chain.js", "../lib/hl.js", "../lib/keys.js", "../views/pool.js", "../views/pools.js",
    "../views/challenge.js", "../views/shared.js", "../views/verify.js", "../views/evidence.js"]) {
    const source = text(file);
    const providers = source.match(/new ethers\.JsonRpcProvider\([^)]*\)/g) || [];
    for (const p of providers) assert.match(p, /PROVIDER_OPTIONS/, `${file}: ${p}`);
  }
});

// What ethers 6.17.0 threw for a batch of 22 reads on 1 Oct 2026, cut to the fields the app reads.
const REFUSED = {
  code: "BAD_DATA",
  shortMessage: "missing response for request",
  message: "missing response for request (value=[ { \"error\": { \"code\": -32010, \"data\": \"Exceeded max limit of 20\", "
    + "\"message\": \"The batch request was too large\" }, \"id\": null, \"jsonrpc\": \"2.0\" } ], info={...}, code=BAD_DATA, "
    + "version=6.17.0)",
  value: [{ jsonrpc: "2.0", id: null, error: { code: -32010, message: "The batch request was too large", data: "Exceeded max limit of 20" } }],
  info: { payload: { method: "eth_call", id: 1, jsonrpc: "2.0" } },
};

test("a refused batch is named for what it is, not as a mismatch with the deployment", () => {
  assert.equal(batchRefused(REFUSED), true);
  // Read from the node's answer alone, as a wrapper with a shorter message would carry it.
  assert.equal(batchRefused({ code: "BAD_DATA", shortMessage: "missing response for request",
    message: "missing response for request", value: [{ error: { code: -32010 } }] }), true);
  // The code alone, as a node that words its refusal differently would send it.
  assert.equal(batchRefused({ error: { code: -32010, message: "refused" } }), true);
  assert.equal(friendly(REFUSED),
    "The public HyperEVM RPC refused this page's reads: it takes at most 20 in one batch, and the page sent more at once.");
  const hint = failureHint(REFUSED);
  assert.match(hint, /a fault in this app asking too much at once, not in the contracts or the deployment/);
  assert.match(hint, /Reloading the page usually gets past it\./);
  assert.doesNotMatch(hint, /do not match|will not help/);
  // A real decode error still says what it says.
  const decode = { code: "BAD_DATA", shortMessage: "could not decode result data", message: "could not decode result data", value: "0x" };
  assert.equal(batchRefused(decode), false);
  assert.match(failureHint(decode), /the app and the deployment it points at do not match\. Reloading will not help\./);
});
