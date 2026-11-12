// app/config.js is written by scripts/app_config.py from spike/hlspike/network.py and deployments/LIVE.json. These
// tests hold the committed file to the script's testnet output, and show what it writes for mainnet: an example
// mainnet deployment is laid out in a directory of its own, beside testnet's real files, never in deployments/.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const SCRIPT = join(ROOT, "scripts/app_config.py");
const run = (...args) => spawnSync("python3", [SCRIPT, ...args], { cwd: ROOT, encoding: "utf8" });
const LIVE = JSON.parse(readFileSync(join(ROOT, "deployments/LIVE.json"), "utf8")).deployments;

const FACTORY = "0x00000000000000000000000000000000000000F1";
const MAIN_RECORD = {
  label: "v3-main", network: "mainnet", chain_id: 999, status: "complete", block: 1234567,
  PoolFactory: FACTORY, KeyRegistry: "0x00000000000000000000000000000000000000A1",
  PaperExamImpl: "0x00000000000000000000000000000000000000B1",
  deployer: "0x00000000000000000000000000000000000000D1", platform_assets: { BTC: 0, ETH: 1, SOL: 5, HYPE: 159 },
};
const MAIN = {
  label: "v3-main", network: "mainnet", chainId: 999, role: "live", record: "deployments/mainnet-v3-main.json",
  factory: FACTORY, pool: null, gateway: "https://pools-api.usenami.io/main",
  deployer: MAIN_RECORD.deployer, keeper: "0x00000000000000000000000000000000000000E1",
};
const MAIN_SHARED = {
  ...MAIN, label: "shared-main", role: "shared", record: "deployments/mainnet-shared-main.json",
  pool: "0x00000000000000000000000000000000000000C1", gateway: null,
};

/** A directory laid out like the repository: testnet's real files, and these mainnet entries with their records. */
function example(entries, records) {
  const dir = mkdtempSync(join(tmpdir(), "app-config-"));
  cpSync(join(ROOT, "spike/hlspike/network.py"), join(dir, "spike/hlspike/network.py"));
  cpSync(join(ROOT, "deployments"), join(dir, "deployments"), { recursive: true });
  writeFileSync(join(dir, "deployments/LIVE.json"), JSON.stringify({ deployments: [...LIVE, ...entries] }, null, 2));
  for (const [path, record] of Object.entries(records)) writeFileSync(join(dir, path), JSON.stringify(record, null, 2));
  return dir;
}

const records = {
  "deployments/mainnet-v3-main.json": MAIN_RECORD,
  "deployments/mainnet-shared-main.json": { ...MAIN_RECORD, label: "shared-main", SharedPool: MAIN_SHARED.pool },
};

test("app/config.js is what scripts/app_config.py writes for testnet", () => {
  const written = execFileSync("python3", [SCRIPT, "--network", "testnet"], { cwd: ROOT, encoding: "utf8" });
  assert.equal(readFileSync(join(ROOT, "app/config.js"), "utf8"), written,
    "run: python3 scripts/app_config.py --out app/config.js");
});

test("a mainnet site's settings hold mainnet's values and mainnet's deployments only", async () => {
  const dir = example([MAIN, MAIN_SHARED], records);
  try {
    const answer = run("--network", "mainnet", "--root", dir, "--out", join(dir, "config.mjs"));
    assert.equal(answer.status, 0, answer.stderr);
    const text = readFileSync(join(dir, "config.mjs"), "utf8");
    const { CONFIG } = await import(pathToFileURL(join(dir, "config.mjs")).href);
    assert.equal(CONFIG.chainId, 999);
    assert.equal(CONFIG.chainHex, "0x3e7");
    assert.equal(CONFIG.chainName, "HyperEVM");
    assert.equal(CONFIG.rpc, "https://rpc.hyperliquid.xyz/evm");
    assert.equal(CONFIG.hlInfo, "https://api.hyperliquid.xyz/info");
    assert.equal(CONFIG.hlExchange, "https://api.hyperliquid.xyz/exchange");
    assert.equal(CONFIG.hlApp, "https://app.hyperliquid.xyz");
    assert.equal(CONFIG.hyperliquidChain, "Mainnet");
    assert.equal(CONFIG.usdc, "0xb88339CB7199b77E23DB6E890353E22632Ba630f");
    assert.equal(CONFIG.gateway, MAIN.gateway);
    assert.deepEqual(CONFIG.deployments, [{
      label: "v3-main", role: "live", factory: FACTORY, registry: MAIN_RECORD.KeyRegistry, deployBlock: 1234567,
      deployer: MAIN_RECORD.deployer, platformAssets: [0, 1, 5, 159], sharedPool: MAIN_SHARED.pool,
      sharedPoolRecord: "shared-main", assetLeverage: true,
    }]);
    // Nothing of testnet's is left in it: not the word, not its USDC, not one of its deployments' addresses.
    assert.doesNotMatch(text, /testnet/i);
    for (const address of ["0x2B3370eE501B4a559b57D449569354196457D8Ab",
      ...LIVE.flatMap((e) => [e.factory, e.pool, e.deployer, e.keeper]).filter(Boolean)]) {
      assert.ok(!text.toLowerCase().includes(address.toLowerCase()), `testnet's ${address} is in mainnet's settings`);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("no settings for a network without exactly one live deployment, or with a record of another chain", () => {
  const none = run("--network", "mainnet");
  assert.notEqual(none.status, 0);
  assert.match(none.stderr, /0 live deployments on mainnet/);
  for (const [entries, recs, why] of [
    [[MAIN, { ...MAIN, label: "v3-main2", record: "deployments/mainnet-v3-main2.json" }],
      { ...records, "deployments/mainnet-v3-main2.json": { ...MAIN_RECORD, label: "v3-main2" } }, /2 live deployments on mainnet/],
    [[MAIN], { ...records, "deployments/mainnet-v3-main.json": { ...MAIN_RECORD, chain_id: 998 } }, /chain_id 998, not mainnet's 999/],
    [[MAIN], records, null],
  ]) {
    const dir = example(entries, recs);
    try {
      const answer = run("--network", "mainnet", "--root", dir);
      if (why) {
        assert.notEqual(answer.status, 0);
        assert.match(answer.stderr, why);
      } else {
        assert.equal(answer.status, 0, answer.stderr);
        assert.doesNotMatch(answer.stdout, /sharedPool: "/); // no shared entry on mainnet: the site opens none
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("a record that names a KeyBinder gives the site its binder, marked; one with both or neither gives no settings", async () => {
  const BINDER = "0x00000000000000000000000000000000000000A2";
  const { KeyRegistry, ...noRegistry } = MAIN_RECORD;
  const cases = [
    [{ ...noRegistry, KeyBinder: BINDER }, null],
    [{ ...MAIN_RECORD, KeyBinder: BINDER }, /v3-main: the record must name a KeyRegistry or a KeyBinder, one of them/],
    [noRegistry, /v3-main: the record must name a KeyRegistry or a KeyBinder, one of them/],
    // Named by its entry, not by a field the record might lack.
    [{ ...MAIN_RECORD, KeyBinder: BINDER, label: undefined }, /v3-main: the record must name a KeyRegistry or a KeyBinder/],
  ];
  assert.ok(KeyRegistry);
  for (const [record, why] of cases) {
    const dir = example([MAIN], { "deployments/mainnet-v3-main.json": record });
    try {
      const answer = run("--network", "mainnet", "--root", dir, "--out", join(dir, "config.mjs"));
      if (why) {
        assert.notEqual(answer.status, 0);
        assert.match(answer.stderr, why);
        continue;
      }
      assert.equal(answer.status, 0, answer.stderr);
      const { CONFIG } = await import(pathToFileURL(join(dir, "config.mjs")).href);
      const [live] = CONFIG.deployments;
      assert.equal(live.registry, BINDER);
      assert.equal(live.keyBinder, true);
      assert.deepEqual(Object.keys(live).slice(0, 5), ["label", "role", "factory", "registry", "keyBinder"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("--gateway puts a gateway on this machine in, and nothing else changes", () => {
  const plain = run().stdout;
  const local = run("--gateway", "http://127.0.0.1:8787").stdout;
  const live = LIVE.find((e) => e.network === "testnet" && e.role === "live");
  assert.equal(local, plain.replace(`gateway: "${live.gateway}"`, 'gateway: "http://127.0.0.1:8787"'));
  assert.notEqual(local, plain);
  for (const bad of ["", "127.0.0.1:8787"]) {
    const refused = run("--gateway", bad);
    assert.notEqual(refused.status, 0, `--gateway ${JSON.stringify(bad)} was taken`);
    assert.match(refused.stderr, /--gateway takes a URL/);
  }
});
