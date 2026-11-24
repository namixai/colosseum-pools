// node --test app/tests/mainnet-load.test.mjs
//
// A mainnet site of simulation pools has no live deployment, no archive and no shared pool (app/config.js,
// `deployments: []`), and app.js still imports every page: the pages of a PoolFactory's pools are no routes of such a
// site, but their modules load with it. So no module may need a deployment to stand while it loads: one that did
// (app/lib/shared.js read the live deployment's shared pool as it loaded) left the whole site on "Loading…". Every
// module of the site is loaded here beside mainnet's settings, with a stand-in for the browser's `window.ethers`: what
// a module does as it loads is all that runs. This file has the stand-in to itself: node runs each test file apart.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { mainnetSite } from "./mainnet-site.mjs";

// Anything a module asks of ethers as it loads: a class to extend, an interface to build, a constant to read.
const anything = new Proxy(function () {}, {
  get: (target, key) => (key === Symbol.toPrimitive ? () => "" : key === "then" ? undefined : anything),
  apply: () => anything,
  construct: () => anything,
});
globalThis.window = { ethers: anything };
globalThis.ethers = anything;

const APP = fileURLToPath(new URL("../", import.meta.url));
const built = mainnetSite();
after(() => built.remove());

/** Loads every module under `site`'s lib/ and views/, and answers the ones that threw as they loaded. */
async function load(site) {
  const failed = [];
  let loaded = 0;
  for (const dir of ["lib", "views"]) {
    for (const file of readdirSync(join(site, dir)).filter((name) => name.endsWith(".js")).sort()) {
      try {
        await import(pathToFileURL(join(site, dir, file)).href);
        loaded += 1;
      } catch (err) {
        failed.push(`${dir}/${file}: ${err.name}: ${err.message}`);
      }
    }
  }
  return { failed, loaded };
}

test("every module of a mainnet site of simulation pools loads: none needs a live deployment to stand", async () => {
  const { failed, loaded } = await load(built.site);
  assert.deepEqual(failed, []);
  assert.ok(loaded >= 70, `${loaded} modules`);
  // What the pools' pages would read of a live deployment is absent, not a crash.
  const shared = await import(pathToFileURL(join(built.site, "lib/shared.js")).href);
  assert.equal(shared.SHARED_POOL, undefined, "a site with no live deployment has no shared pool");
  const chain = await import(pathToFileURL(join(built.site, "lib/chain.js")).href);
  assert.equal(chain.deployed(), false);
  // app.js imports them all, whichever site it is: the routes decide what is shown, not what is loaded.
  const app = readFileSync(join(built.site, "app.js"), "utf8");
  for (const view of ["pools", "pool", "challenge", "verify", "economics", "shared", "evidence", "simpool", "simadmin", "simoperator", "simpartner", "simtrader", "exam", "simsite"]) {
    assert.match(app, new RegExp(`^import \\{[^}]+\\} from "\\./views/${view}\\.js";$`, "m"), `app.js does not import views/${view}.js`);
  }
});

test("the same modules load beside testnet's settings, as they did", async () => {
  const { failed, loaded } = await load(APP);
  assert.deepEqual(failed, []);
  assert.ok(loaded >= 70, `${loaded} modules`);
  const shared = await import(pathToFileURL(join(APP, "lib/shared.js")).href);
  assert.match(shared.SHARED_POOL, /^0x[0-9a-fA-F]{40}$/);
});
