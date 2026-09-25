// node --test app/tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { refusalText } from "../lib/gateway.js";

// Answers the live gateway actually returns, copied from its source (gateway/server.py) and
// from replies it gave on 25 September 2026.
const BUSY = { http: 429, status: "busy", code: "upstream_busy",
               detail: "the chain node is refusing reads right now; try again in a moment" };
const NOT_YOURS = { http: 403, status: "refused_by_gateway", code: "not_your_account",
                    detail: "the key on this account is not bound to the signer" };
const UNCERTAIN = { http: 502, status: "uncertain", code: "send_failed",
                    detail: "the order may or may not have reached Hyperliquid; check the account" };
const VENUE = { http: 422, status: "refused_by_venue", reason: "Order price cannot be more than 80% away from the reference price" };

test("the sentence written for a person beats the code written for a machine", () => {
  assert.equal(refusalText(BUSY), BUSY.detail);
  assert.notEqual(refusalText(BUSY), "upstream_busy");
  assert.equal(refusalText(NOT_YOURS), NOT_YOURS.detail);
});

test("an answer that is not a refusal still reaches the trader", () => {
  // This is the one that costs money if it is hidden: told only "send_failed", a trader sends
  // the order again.
  assert.equal(refusalText(UNCERTAIN), UNCERTAIN.detail);
  assert.match(refusalText(UNCERTAIN), /check the account/);
});

test("the venue's own words come first", () => {
  assert.equal(refusalText(VENUE), VENUE.reason);
  // Even when we have something of our own to say about it.
  assert.equal(refusalText({ ...VENUE, detail: "ours", code: "refused" }), VENUE.reason);
});

test("it falls back only as far as it has to", () => {
  assert.equal(refusalText({ status: "bad_response", code: "boom" }), "boom");
  assert.equal(refusalText({ status: "bad_response" }), "bad_response");
  assert.equal(refusalText({}), "the gateway said nothing");
  assert.equal(refusalText(), "the gateway said nothing");
});

test("the trading panel actually goes through it", () => {
  // Testing the function alone would stay green if the view went back to printing res.code, which
  // is exactly the state this change is fixing. So the guard reads the view's source.
  const src = readFileSync(new URL("../views/trading.js", import.meta.url), "utf8");
  const notPlaced = src.match(/Not (?:placed|cancelled): \$\{([^}]*)\}/g) || [];
  assert.equal(notPlaced.length, 2, "both the order and the cancel report a refusal");
  for (const line of notPlaced) {
    assert.match(line, /gateway\.refusalText\(res\)/, `still formatting a refusal by hand: ${line}`);
  }
  assert.doesNotMatch(src, /res\.reason \|\| res\.code/, "the old hand-rolled chain is back");
});
