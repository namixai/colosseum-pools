// node --test app/tests
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { decode, hex } from "../lib/cbor.js";
import { orderUrl } from "../lib/gateway.js";
import { createNavigator, needsGate } from "../lib/nav.js";
import { settle } from "../lib/ui.js";
import { canonical, roundPrice, roundSize } from "../lib/hl.js";

const h = (s) => Uint8Array.from(s.match(/../g).map((b) => parseInt(b, 16)));

// RFC 8949, Appendix A.
test("cbor: RFC 8949 examples", () => {
  assert.equal(decode(h("00")), 0);
  assert.equal(decode(h("17")), 23);
  assert.equal(decode(h("1818")), 24);
  assert.equal(decode(h("1903e8")), 1000);
  assert.equal(decode(h("1a000f4240")), 1000000);
  assert.equal(decode(h("20")), -1);
  assert.equal(decode(h("3863")), -100);
  assert.equal(hex(decode(h("40"))), "");
  assert.equal(hex(decode(h("4401020304"))), "01020304");
  assert.equal(decode(h("6161")), "a");
  assert.equal(decode(h("6449455446")), "IETF");
  assert.deepEqual(decode(h("80")), []);
  assert.deepEqual(decode(h("83010203")), [1, 2, 3]);
  assert.deepEqual(decode(h("8301820203820405")), [1, [2, 3], [4, 5]]);
  const m = decode(h("a201020304"));
  assert.equal(m.get(1), 2);
  assert.equal(m.get(3), 4);
  assert.equal(decode(h("f4")), false);
  assert.equal(decode(h("f5")), true);
  assert.equal(decode(h("f6")), null);
  assert.equal(decode(h("c11a514b67b0")), 1363896240);
  // indefinite-length items (the Nitro attestation payload is an indefinite map)
  assert.equal(hex(decode(h("5f42010243030405ff"))), "0102030405");
  assert.equal(decode(h("7f657374726561646d696e67ff")), "streaming");
  assert.deepEqual(decode(h("9f018202039f0405ffff")), [1, [2, 3], [4, 5]]);
  const im = decode(h("bf61610161629f0203ffff"));
  assert.equal(im.get("a"), 1);
  assert.deepEqual(im.get("b"), [2, 3]);
  assert.throws(() => decode(h("0000")), /trailing/);
});

// A decoder that loops forever has to fail its test, not hang the run, so each hostile input
// is decoded in a child process with a time limit.
function decodeInChild(input) {
  const cbor = new URL("../lib/cbor.js", import.meta.url).href;
  const code = `import { decode } from ${JSON.stringify(cbor)};
    const bytes = Uint8Array.from(${JSON.stringify(input)}.match(/../g).map((b) => parseInt(b, 16)));
    try { decode(bytes); console.log("decoded"); } catch (e) { console.log("error: " + e.message); }`;
  const run = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8", timeout: 5000 });
  return run.error ? `no answer: ${run.error.code}` : run.stdout.trim();
}

test("cbor: hostile input fails fast", () => {
  const early = "error: CBOR input ends early";
  assert.equal(decodeInChild("9f01"), early); // indefinite array with no break
  assert.equal(decodeInChild("bf6161"), early); // indefinite map cut after a key
  assert.equal(decodeInChild("7f6161"), early); // indefinite text with no break
  assert.equal(decodeInChild("9bffffffffffffffff"), early); // an array of 2^64 items
  assert.equal(decodeInChild("5a0000ffff00"), early); // bytes declared longer than the input
  assert.equal(decodeInChild("58"), early); // a one-byte length that isn't there
  assert.equal(decodeInChild("19"), early); // a two-byte number that isn't there
  assert.equal(decodeInChild("0000"), "error: trailing bytes after the CBOR item");
  assert.equal(decodeInChild("81".repeat(17) + "00"), "error: CBOR nesting too deep");
  assert.equal(decodeInChild("81".repeat(15) + "00"), "decoded");
});

test("the gateway is reached over https unless it runs on this machine", () => {
  assert.equal(orderUrl("http://127.0.0.1:8787"), "http://127.0.0.1:8787/v1/order");
  assert.equal(orderUrl("http://localhost:8787/"), "http://localhost:8787/v1/order");
  assert.equal(orderUrl("https://gateway.example.org/pools"), "https://gateway.example.org/pools/v1/order");
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
