// node --test app/tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { saleBlocker, topUpAdvice, NO_CHALLENGE } from "../lib/funding.js";

// The stand pool on testnet, 25 Sep 2026, the moment after a whole cycle came home: one
// challenge sold, passed, the trader funded, the funded stage stopped and settled back. It
// holds less than it needs, and the difference is mostly the account fee HyperCore kept.
const AFTER_A_CYCLE = { stage: 0, ready: true, challenge: NO_CHALLENGE, spot: 40.781793, needed: 42 };

test("a pool that can sell says nothing", () => {
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, spot: 42 }), null);
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, spot: 99 }), null);
});

test("a pool short on HyperCore does not offer the sale", () => {
  const blocker = saleBlocker(AFTER_A_CYCLE);
  assert.notEqual(blocker, null, "the button was offered on a pool that cannot pay the capital");
  assert.equal(blocker.kind, "underfunded");
  // The gap is read off the two figures, never a number written into the page.
  assert.ok(Math.abs(blocker.short - (AFTER_A_CYCLE.needed - AFTER_A_CYCLE.spot)) < 1e-9);
});

test("exactly enough is enough", () => {
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, spot: 42, needed: 42 }), null);
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, spot: 41.999999, needed: 42 }).kind, "underfunded");
});

test("the other blockers keep their own answers, and come first", () => {
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, stage: 2 }).kind, "taken");
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, ready: false }).kind, "not-prepared");
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, challenge: "0x110fb6b8985f89b4e651cbeb536925c5fd1950b0" }).kind, "settling");
  // A pool that is both unprepared and short is unprepared: the money is the later question.
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, ready: false, spot: 0 }).kind, "not-prepared");
  // A taken pool is taken whatever it holds -- its capital is out working, not missing.
  assert.equal(saleBlocker({ ...AFTER_A_CYCLE, stage: 1, spot: 0 }).kind, "taken");
});

test("no gap, no advice", () => {
  assert.equal(topUpAdvice({ short: 0, earned: 5 }), null);
  assert.equal(topUpAdvice({ short: -3, earned: 5 }), null);
});

test("the advice splits the gap between the income held here and the investor", () => {
  const partly = topUpAdvice({ short: 1.218207, earned: 1 });
  assert.equal(partly.fromEarned, 1);
  assert.ok(Math.abs(partly.stillNeeded - 0.218207) < 1e-9);

  const covered = topUpAdvice({ short: 1.218207, earned: 5 });
  assert.equal(covered.fromEarned, 1.218207, "it would withdraw more than the gap");
  assert.equal(covered.stillNeeded, 0);

  const nothingHere = topUpAdvice({ short: 1.218207, earned: 0 });
  assert.equal(nothingHere.fromEarned, 0);
  assert.equal(nothingHere.stillNeeded, 1.218207);
});
