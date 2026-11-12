// node --test app/tests/keybinder.test.mjs
//
// A deployment whose record names a KeyBinder (src/KeyBinder.sol) instead of a KeyRegistry: the site reads its keys'
// bindings from the binder, with the binder's own ABI, from the first such record on. scripts/app_config.py marks the
// row `keyBinder` (app/tests/config.test.mjs), deployments.js says which ABI, and chain.js reads with it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { keysKind } from "../lib/deployments.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8").replace(/\r\n/g, "\n");
const CHAIN = text("../lib/chain.js");
const BINDER_SOL = text("../../src/KeyBinder.sol");
const REGISTRY_SOL = text("../../src/KeyRegistry.sol");

/** The lines of one of chain.js's ABI lists, by its key. */
function abiList(key) {
  const m = CHAIN.match(new RegExp(`\\n  ${key}: \\[\\n([\\s\\S]*?)\\n  \\],`));
  assert.ok(m, `chain.js has no ABI list ${key}`);
  return [...m[1].matchAll(/^\s+"(.*)",$/gm)].map((l) => l[1]);
}

/** `type indexed? name, ...` as types with their indexed flag. */
function shape(params) {
  return params.split(",").map((p) => p.trim()).filter(Boolean).map((p) => {
    const words = p.split(/\s+/);
    return `${words[0]}${words.includes("indexed") ? " indexed" : ""}`;
  }).join(",");
}

const BINDER = abiList("binder");
const REGISTRY = abiList("registry");

test("a deployment marked keyBinder is read as a binder, any other as a registry", () => {
  assert.equal(keysKind({ keyBinder: true }), "binder");
  assert.equal(keysKind({ registry: "0x01" }), "registry");
  assert.equal(keysKind(null), "registry");
  assert.equal(keysKind(undefined), "registry");
  assert.match(CHAIN, /export function registry\(runner, deployment = liveDeployment\(\)\) \{\n {2}return contract\(keysKind\(deployment\), deployment\.registry, runner\);/);
});

test("every line of the binder's ABI is the binder's own: its views and its events, types and topics alike", () => {
  // An enum is a uint8 in the ABI.
  const enums = [...BINDER_SOL.matchAll(/enum (\w+) \{/g)].map((m) => m[1]);
  const abiTypes = (params) => params.replace(new RegExp(`\\b(${enums.join("|")})\\b`, "g"), "uint8");
  const events = new Map([...BINDER_SOL.matchAll(/event\s+(\w+)\(([^)]*)\);/g)].map((m) => [m[1], shape(abiTypes(m[2]))]));
  for (const line of BINDER) {
    const e = line.match(/^event (\w+)\((.*)\)$/);
    if (e) {
      assert.equal(events.get(e[1]), shape(e[2]), `${e[1]} differs from src/KeyBinder.sol`);
      continue;
    }
    const name = line.match(/^function (\w+)\(/)[1];
    assert.match(BINDER_SOL, new RegExp(`function ${name}\\(|public\\s+${name}\\s*;|\\)\\s+public\\s+${name}\\s*;`),
      `${name} is not a view of the binder`);
  }
  // bindingOf answers its Binding: the state, the account, the trader, as the registry's does.
  assert.match(BINDER_SOL, /struct Binding \{\s+State state;\s+address account;\s+address trader;\s+\}/);
  assert.ok(BINDER.includes("function bindingOf(address key) view returns ((uint8 state, address account, address trader))"));
});

test("the binder's ABI has no freeCount, and its KeyBound is not the registry's", () => {
  assert.ok(!BINDER.some((l) => l.includes("freeCount")), "nothing waits in a binder");
  assert.ok(!/function freeCount\(/.test(BINDER_SOL));
  assert.match(REGISTRY_SOL, /function freeCount\(/, "the registry keeps its own");
  const bound = (list) => list.find((l) => l.startsWith("event KeyBound("));
  assert.notEqual(bound(BINDER), bound(REGISTRY), "a binder's KeyBound read with the registry's line would not decode");
});

test("the key states are the same in both, so one badge reads either", () => {
  const states = (source) => source.match(/enum State \{([\s\S]*?)\}/)[1].split(",").map((s) => s.trim()).filter(Boolean);
  assert.deepEqual(states(BINDER_SOL), states(REGISTRY_SOL));
  assert.match(CHAIN, /export const KEY_STATE = \["Unknown", "Free", "Bound", "Retired"\];/);
  assert.deepEqual(states(BINDER_SOL), ["Unknown", "Free", "Bound", "Retired"]);
});

test("every read the check-it-yourself page makes of a deployment's keys is in both ABIs", () => {
  const calls = [...text("../views/verify.js").matchAll(/\breg\.(\w+)\(/g)].map((m) => m[1]);
  assert.ok(calls.length >= 2, "the page reads the keys contract");
  for (const name of calls) {
    for (const [list, which] of [[BINDER, "binder"], [REGISTRY, "registry"]]) {
      assert.ok(list.some((l) => l.startsWith(`function ${name}(`)), `${name} is not in the ${which}'s ABI`);
    }
  }
});
