// node --test app/tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { orderUrl } from "../lib/gateway.js";
import { createNavigator, logWindows, needsGate } from "../lib/nav.js";
import { settle } from "../lib/ui.js";
import { canonical, roundPrice, roundSize } from "../lib/hl.js";
import { refusal, splitSignature, spotSend } from "../lib/hlsend.js";

test("the gateway is reached over https unless it runs on this machine", () => {
  assert.equal(orderUrl("http://127.0.0.1:8787"), "http://127.0.0.1:8787/v1/order");
  assert.equal(orderUrl("http://localhost:8787/"), "http://localhost:8787/v1/order");
  assert.equal(orderUrl("https://gateway.example.org/pools"), "https://gateway.example.org/pools/v1/order");
  assert.equal(orderUrl("https://gateway.example.org/pools/?tenant=a#top"), "https://gateway.example.org/pools/v1/order");
  assert.throws(() => orderUrl("http://gateway.example.org"), /https/);
  assert.throws(() => orderUrl("http://127.0.0.1.example.org"), /https/);
  assert.throws(() => orderUrl("ftp://127.0.0.1"), /https/);
});

test("a page that finishes loading after the user moved on stays off screen", async () => {
  const mounted = [];
  const navigate = createNavigator(() => {
    const page = { html: "" };
    mounted.push(page);
    return page;
  });
  let release;
  const slow = navigate(async (page) => {
    await new Promise((resolve) => (release = resolve));
    page.html = "the pool the user left";
  });
  const fast = navigate(async (page) => {
    page.html = "the pool the user opened";
  });
  assert.equal(await fast, true);
  release();
  assert.equal(await slow, false);
  assert.equal(mounted.length, 2);
  assert.equal(mounted.at(-1).html, "the pool the user opened");
});

test("only the page on screen reports its failure", async () => {
  const errors = [];
  const report = (page, err) => errors.push(err.message);
  const navigate = createNavigator(() => ({}));
  let fail;
  const stale = navigate(() => new Promise((_, reject) => (fail = reject)), report);
  await navigate(async () => {
    throw new Error("current");
  }, report);
  fail(new Error("stale"));
  await stale;
  assert.deepEqual(errors, ["current"]);
});

test("event history is read newest first, 50 blocks a call at most", () => {
  assert.deepEqual(logWindows(130, 0, 50, 10), {
    windows: [{ start: 81, end: 130 }, { start: 31, end: 80 }, { start: 0, end: 30 }],
    complete: true,
  });
  assert.deepEqual(logWindows(130, 0, 50, 2), {
    windows: [{ start: 81, end: 130 }, { start: 31, end: 80 }],
    complete: false,
  });
  assert.deepEqual(logWindows(130, 100, 50, 10), { windows: [{ start: 100, end: 130 }], complete: true });
  for (const { start, end } of logWindows(10_000, 0, 50, 500).windows) assert.ok(end - start + 1 <= 50);
});

test("every page but the terms asks the entry question until it is answered", () => {
  assert.equal(needsGate("#/terms", false), false);
  assert.equal(needsGate("#/", false), true);
  assert.equal(needsGate("#/pool/0x0000000000000000000000000000000000000001", false), true);
  assert.equal(needsGate("#/verify", true), false);
});

test("a panel that fails to load says so", async () => {
  const box = { textContent: "Loading…" };
  await settle(Promise.reject(new Error("rpc down")), box);
  assert.match(box.textContent, /rpc down/);
  const fine = { textContent: "Loading…" };
  await settle(Promise.resolve(), fine);
  assert.equal(fine.textContent, "Loading…");
});

test("canonical numbers, the form Hyperliquid verifies against", () => {
  assert.equal(canonical("60000.0"), "60000");
  assert.equal(canonical("0.00020"), "0.0002");
  assert.equal(canonical("007.50"), "7.5");
  assert.equal(canonical("0.1"), "0.1");
  assert.throws(() => canonical("0"), /above zero/);
  assert.throws(() => canonical("1e5"));
  assert.throws(() => canonical("-1"));
  assert.throws(() => canonical(".5"));
});

test("prices: five significant figures, integers always allowed, 6 - szDecimals decimals", () => {
  assert.equal(roundPrice(76412.37, 5), "76412"); // BTC
  assert.equal(roundPrice(123456.7, 5), "123457"); // integer part over five digits
  assert.equal(roundPrice(76412.5, 5), "76413"); // a half rounds up
  assert.equal(roundPrice(2413.71, 4), "2413.7"); // ETH
  assert.equal(roundPrice(98.307, 2), "98.307"); // SOL
  assert.equal(roundPrice(26.0, 2), "26");
  assert.equal(roundPrice(0.123456, 0), "0.12346");
  assert.equal(roundPrice(0.012345678, 0), "0.012346");
  assert.equal(roundPrice(1.23456789, 5), "1.2"); // capped at 6 - 5 = 1 decimal
  assert.throws(() => roundPrice(0, 2));
});

test("sizes: rounded down to size decimals", () => {
  assert.equal(roundSize(0.000259, 5), "0.00025");
  assert.equal(roundSize(1.999, 2), "1.99");
  assert.equal(roundSize(3, 0), "3");
  assert.equal(roundSize(0.3, 1), "0.3"); // float noise must not drop a unit
  assert.throws(() => roundSize(0.000001, 5), /zero/);
});

// The same input through the Hyperliquid Python SDK's user_signed_payload gives this domain,
// type list and message; eth_account and ethers 5 both hash it to 0x5b3135…ef60.
const SPOT_VECTOR = {
  destination: "0x5fb436b6b806541ade43cac6088810b3a4e9e25e",
  token: "USDC:0xeb62eee3685fc4c43992febcd9e75443",
  amount: "1",
  time: 1789640000000,
  chainHex: "0x3e6",
};

test("spot transfer: the data the wallet signs is Hyperliquid's", () => {
  const out = spotSend(SPOT_VECTOR);
  assert.deepEqual(out.domain, {
    name: "HyperliquidSignTransaction", version: "1", chainId: 998,
    verifyingContract: "0x0000000000000000000000000000000000000000",
  });
  assert.deepEqual(out.types, {
    "HyperliquidTransaction:SpotSend": [
      { name: "hyperliquidChain", type: "string" },
      { name: "destination", type: "string" },
      { name: "token", type: "string" },
      { name: "amount", type: "string" },
      { name: "time", type: "uint64" },
    ],
  });
  assert.deepEqual(out.message, {
    hyperliquidChain: "Testnet", destination: SPOT_VECTOR.destination, token: SPOT_VECTOR.token,
    amount: "1", time: 1789640000000,
  });
  assert.deepEqual(out.action, { type: "spotSend", signatureChainId: "0x3e6", ...out.message });
});

test("spot transfer: amounts are canonical, and a bad field is refused before signing", () => {
  assert.equal(spotSend({ ...SPOT_VECTOR, amount: "0250.50" }).message.amount, "250.5");
  assert.throws(() => spotSend({ ...SPOT_VECTOR, destination: "0x" + "00".repeat(20) }), /destination/);
  assert.throws(() => spotSend({ ...SPOT_VECTOR, destination: "0x5fb4" }), /destination/);
  assert.throws(() => spotSend({ ...SPOT_VECTOR, token: "USDC" }), /token/);
  assert.throws(() => spotSend({ ...SPOT_VECTOR, time: 0 }), /time/);
  assert.throws(() => spotSend({ ...SPOT_VECTOR, chainHex: "998" }), /chain/);
  assert.throws(() => spotSend({ ...SPOT_VECTOR, amount: "0" }), /above zero/);
});

test("spot transfer: a 65-byte signature becomes r, s and v", () => {
  const r = "11".repeat(32);
  const s = "22".repeat(32);
  assert.deepEqual(splitSignature(`0x${r}${s}1b`), { r: `0x${r}`, s: `0x${s}`, v: 27 });
  assert.deepEqual(splitSignature(`0x${r}${s}01`), { r: `0x${r}`, s: `0x${s}`, v: 28 });
  assert.throws(() => splitSignature(`0x${r}${s}1d`), /v/);
  assert.throws(() => splitSignature(`0x${r}`), /65-byte/);
});

test("spot transfer: only an ok answer counts as sent", () => {
  assert.equal(refusal({ status: "ok", response: { type: "default" } }), null);
  assert.equal(refusal({ status: "err", response: "Insufficient balance for token transfer" }),
    "Insufficient balance for token transfer");
  assert.match(refusal({ status: "unknown" }), /confirms nothing/);
  assert.match(refusal(null), /confirms nothing/);
});
