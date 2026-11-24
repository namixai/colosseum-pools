// app/config.js is written by scripts/app_config.py from spike/hlspike/network.py and deployments/LIVE.json. These
// tests hold the committed file to the script's testnet output, and show what it writes for mainnet: an example
// mainnet deployment is laid out in a directory of its own, beside testnet's real files, never in deployments/.
// The same for index.html (--index): the committed page is testnet's, and mainnet's is that page with its own words.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const SCRIPT = join(ROOT, "scripts/app_config.py");
const run = (...args) => spawnSync("python3", [SCRIPT, ...args], { cwd: ROOT, encoding: "utf8" });
const LIVE = JSON.parse(readFileSync(join(ROOT, "deployments/LIVE.json"), "utf8")).deployments;
// The examples stand beside testnet's entries only: a mainnet deployment that is really listed must not make an
// example a second one of its network, nor an example of "no mainnet deployment" false.
const TESTNET = LIVE.filter((e) => e.network === "testnet");

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
  cpSync(join(ROOT, "app/index.html"), join(dir, "app/index.html"));
  cpSync(join(ROOT, "deployments"), join(dir, "deployments"), { recursive: true });
  writeFileSync(join(dir, "deployments/LIVE.json"), JSON.stringify({ deployments: [...TESTNET, ...entries] }, null, 2));
  for (const [path, record] of Object.entries(records)) writeFileSync(join(dir, path), JSON.stringify(record, null, 2));
  return dir;
}

const records = {
  "deployments/mainnet-v3-main.json": MAIN_RECORD,
  "deployments/mainnet-shared-main.json": { ...MAIN_RECORD, label: "shared-main", SharedPool: MAIN_SHARED.pool },
};

// A live PoolFactory deployment that is not the repository's, for what the script does with one: on testnet, since a
// mainnet site has none of those pools and the script writes it no settings from one. It stands in a list of its own
// (`only`), where it is the network's one live deployment.
const T_RECORD = {
  label: "v3-example", network: "testnet", chain_id: 998, status: "complete", block: 1234567,
  PoolFactory: FACTORY, KeyRegistry: "0x00000000000000000000000000000000000000A1",
  PaperExamImpl: "0x00000000000000000000000000000000000000B1",
  deployer: "0x00000000000000000000000000000000000000D1", platform_assets: { BTC: 3, ETH: 4, SOL: 0, HYPE: 135 },
};
const T_LIVE = {
  label: "v3-example", network: "testnet", chainId: 998, role: "live", record: "deployments/testnet-v3-example.json",
  factory: FACTORY, pool: null, gateway: "https://pools-api.usenami.io/example",
  deployer: T_RECORD.deployer, keeper: "0x00000000000000000000000000000000000000E1",
};
const T_SHARED = {
  ...T_LIVE, label: "shared-example", role: "shared", record: "deployments/testnet-shared-example.json",
  pool: "0x00000000000000000000000000000000000000C1", gateway: null,
};
const tRecords = {
  "deployments/testnet-v3-example.json": T_RECORD,
  "deployments/testnet-shared-example.json": { ...T_RECORD, label: "shared-example", SharedPool: T_SHARED.pool },
};

// A simulation-pool deployment on mainnet as it will be listed: staged, by its SimPoolFactory, with mainnet's USDC. An
// example in a directory of its own, shaped like deployments/testnet-rehearsal998.json; the addresses are placeholders.
const SIM_FACTORY = "0x00000000000000000000000000000000000000F5";
const MAIN_USDC = "0xb88339CB7199b77E23DB6E890353E22632Ba630f";
const SIM_RECORD = {
  label: "sim-main", network: "mainnet", chain_id: 999, status: "complete", block: 2345678,
  SimPoolFactory: SIM_FACTORY, SimPoolImpl: "0x00000000000000000000000000000000000000B5",
  TermsBook: "0x00000000000000000000000000000000000000B6", usdc: MAIN_USDC,
  deployer: "0x00000000000000000000000000000000000000D1", platform_assets: { BTC: 0, ETH: 1, SOL: 5 },
};
const SIM = {
  label: "sim-main", network: "mainnet", chainId: 999, role: "staged", record: "deployments/mainnet-sim-main.json",
  factory: SIM_FACTORY, pool: null, gateway: null, deployer: SIM_RECORD.deployer, keeper: null,
};
const simRecords = { "deployments/mainnet-sim-main.json": SIM_RECORD };

const index = (...args) => run("--index", ...args);
const PAGE = readFileSync(join(ROOT, "app/index.html"), "utf8");

/** A directory laid out like the repository whose list holds these entries and no others. */
function only(entries, records) {
  const dir = mkdtempSync(join(tmpdir(), "app-config-"));
  cpSync(join(ROOT, "spike/hlspike/network.py"), join(dir, "spike/hlspike/network.py"));
  cpSync(join(ROOT, "app/index.html"), join(dir, "app/index.html"));
  mkdirSync(join(dir, "deployments"));
  writeFileSync(join(dir, "deployments/LIVE.json"), JSON.stringify({ deployments: entries }, null, 2));
  for (const [path, record] of Object.entries(records)) writeFileSync(join(dir, path), JSON.stringify(record, null, 2));
  return dir;
}

test("app/config.js is what scripts/app_config.py writes for testnet", () => {
  const written = execFileSync("python3", [SCRIPT, "--network", "testnet"], { cwd: ROOT, encoding: "utf8" });
  assert.equal(readFileSync(join(ROOT, "app/config.js"), "utf8"), written,
    "run: python3 scripts/app_config.py --out app/config.js");
});

test("a site with a live deployment holds its own network's values and its own network's deployments only", async () => {
  // A testnet list of its own, with a mainnet simulation-pool deployment beside it: nothing of mainnet's is read for it.
  const dir = only([T_LIVE, T_SHARED, SIM], { ...tRecords, ...simRecords });
  try {
    const answer = run("--network", "testnet", "--root", dir, "--out", join(dir, "config.mjs"));
    assert.equal(answer.status, 0, answer.stderr);
    const text = readFileSync(join(dir, "config.mjs"), "utf8");
    const { CONFIG } = await import(pathToFileURL(join(dir, "config.mjs")).href);
    assert.equal(CONFIG.chainId, 998);
    assert.equal(CONFIG.chainHex, "0x3e6");
    assert.equal(CONFIG.chainName, "HyperEVM Testnet");
    assert.equal(CONFIG.networkName, "Hyperliquid testnet");
    assert.equal(CONFIG.rpc, "https://rpc.hyperliquid-testnet.xyz/evm");
    assert.equal(CONFIG.hlInfo, "https://api.hyperliquid-testnet.xyz/info");
    assert.equal(CONFIG.hlExchange, "https://api.hyperliquid-testnet.xyz/exchange");
    assert.equal(CONFIG.hlApp, "https://app.hyperliquid-testnet.xyz");
    assert.equal(CONFIG.hyperliquidChain, "Testnet");
    assert.equal(CONFIG.usdc, "0x2B3370eE501B4a559b57D449569354196457D8Ab");
    assert.equal(CONFIG.gateway, T_LIVE.gateway);
    assert.deepEqual(CONFIG.deployments, [{
      label: "v3-example", role: "live", factory: FACTORY, registry: T_RECORD.KeyRegistry, deployBlock: 1234567,
      deployer: T_RECORD.deployer, platformAssets: [3, 4, 0, 135], sharedPool: T_SHARED.pool,
      sharedPoolRecord: "shared-example", assetLeverage: true,
    }]);
    // Nothing of mainnet's is in it: not its factory of simulation pools, not its USDC, and no `sim`.
    assert.equal("sim" in CONFIG, false);
    for (const address of [SIM_FACTORY, MAIN_USDC]) {
      assert.ok(!text.toLowerCase().includes(address.toLowerCase()), `mainnet's ${address} is in testnet's settings`);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a live deployment on mainnet gets no settings and no page: a mainnet site is its simulation pools' alone", () => {
  // Settings written from a live deployment name no factory of simulation pools, and a site without one would open
  // a pool's page for any address in a link. Alone, with its shared pool, or beside the staged factory: nothing is written.
  for (const entries of [[MAIN], [MAIN, MAIN_SHARED], [MAIN, SIM], [SIM, MAIN]]) {
    const dir = example(entries, { ...records, ...simRecords });
    try {
      for (const answer of [run("--network", "mainnet", "--root", dir), index("--network", "mainnet", "--root", dir)]) {
        assert.notEqual(answer.status, 0, `written for ${entries.map((e) => e.label)}`);
        assert.match(answer.stderr, /a live deployment on mainnet in deployments\/LIVE\.json: a mainnet site is written from its staged simulation-pool deployment alone/);
        assert.equal(answer.stdout, "");
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("no settings for a network without exactly one live deployment, or with a record of another chain", () => {
  const empty = example([], {});
  try {
    const none = run("--network", "mainnet", "--root", empty);
    assert.notEqual(none.status, 0);
    assert.match(none.stderr, /0 live deployments on mainnet/);
  } finally {
    rmSync(empty, { recursive: true, force: true });
  }
  const second = { ...T_LIVE, label: "v3-example2", record: "deployments/testnet-v3-example2.json", gateway: "https://pools-api.usenami.io/example2" };
  for (const [network, dir, why] of [
    ["testnet", only([T_LIVE, second], { ...tRecords, "deployments/testnet-v3-example2.json": { ...T_RECORD, label: "v3-example2" } }), /2 live deployments on testnet/],
    ["mainnet", example([MAIN, { ...MAIN, label: "v3-main2", record: "deployments/mainnet-v3-main2.json" }],
      { ...records, "deployments/mainnet-v3-main2.json": { ...MAIN_RECORD, label: "v3-main2" } }), /2 live deployments on mainnet/],
    ["testnet", only([T_LIVE], { ...tRecords, "deployments/testnet-v3-example.json": { ...T_RECORD, chain_id: 999 } }), /chain_id 999, not testnet's 998/],
    ["mainnet", example([SIM], { "deployments/mainnet-sim-main.json": { ...SIM_RECORD, chain_id: 998 } }), /chain_id 998, not mainnet's 999/],
    ["testnet", only([T_LIVE], tRecords), null],
  ]) {
    try {
      const answer = run("--network", network, "--root", dir);
      if (why) {
        assert.notEqual(answer.status, 0);
        assert.match(answer.stderr, why);
      } else {
        assert.equal(answer.status, 0, answer.stderr);
        assert.doesNotMatch(answer.stdout, /sharedPool: "/); // no shared entry on the network: the site opens none
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("a record that names a KeyBinder gives the site its binder, marked; one with both or neither gives no settings", async () => {
  const BINDER = "0x00000000000000000000000000000000000000A2";
  const { KeyRegistry, ...noRegistry } = T_RECORD;
  const cases = [
    [{ ...noRegistry, KeyBinder: BINDER }, null],
    [{ ...T_RECORD, KeyBinder: BINDER }, /v3-example: the record must name a KeyRegistry or a KeyBinder, one of them/],
    [noRegistry, /v3-example: the record must name a KeyRegistry or a KeyBinder, one of them/],
    // Named by its entry, not by a field the record might lack.
    [{ ...T_RECORD, KeyBinder: BINDER, label: undefined }, /v3-example: the record must name a KeyRegistry or a KeyBinder/],
  ];
  assert.ok(KeyRegistry);
  for (const [record, why] of cases) {
    const dir = only([T_LIVE], { "deployments/testnet-v3-example.json": record });
    try {
      const answer = run("--network", "testnet", "--root", dir, "--out", join(dir, "config.mjs"));
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

test("a network with no live deployment gets a site of simulation pools: its one staged factory, and no other deployment", async () => {
  const dir = example([SIM], simRecords);
  try {
    const answer = run("--network", "mainnet", "--root", dir, "--out", join(dir, "config.mjs"));
    assert.equal(answer.status, 0, answer.stderr);
    const text = readFileSync(join(dir, "config.mjs"), "utf8");
    const { CONFIG } = await import(pathToFileURL(join(dir, "config.mjs")).href);
    // The network is mainnet's, from network.py: the chain a wallet is offered, the public node, the name the pages write.
    assert.equal(CONFIG.chainId, 999);
    assert.equal(CONFIG.chainHex, "0x3e7");
    assert.equal(CONFIG.chainName, "HyperEVM");
    assert.equal(CONFIG.networkName, "Hyperliquid");
    assert.deepEqual(CONFIG.nativeCurrency, { name: "HYPE", symbol: "HYPE", decimals: 18 });
    assert.equal(CONFIG.rpc, "https://rpc.hyperliquid.xyz/evm");
    assert.equal(CONFIG.hlInfo, "https://api.hyperliquid.xyz/info");
    assert.equal(CONFIG.hyperliquidChain, "Mainnet");
    assert.equal(CONFIG.usdc, MAIN_USDC);
    // One factory of simulation pools, the entry's and its record's; no pools of a PoolFactory, read-only or not.
    assert.deepEqual(CONFIG.sim, { label: "sim-main", factory: SIM_FACTORY });
    assert.deepEqual(CONFIG.deployments, []);
    assert.equal("readOnlyDeployments" in CONFIG, false);
    // No gateway takes orders for it, and no index is named until the entry names where one answers.
    assert.equal("gateway" in CONFIG, false);
    assert.equal("simIndex" in CONFIG, false);
    // Nothing of testnet's is left in it: not the word, not its USDC, not one of its deployments' addresses.
    assert.doesNotMatch(text, /testnet/i);
    for (const address of ["0x2B3370eE501B4a559b57D449569354196457D8Ab",
      ...TESTNET.flatMap((e) => [e.factory, e.pool, e.deployer, e.keeper]).filter(Boolean)]) {
      assert.ok(!text.toLowerCase().includes(address.toLowerCase()), `testnet's ${address} is in mainnet's settings`);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the index of a site of simulation pools is where its entry says, or where --gateway says, and nowhere by default", async () => {
  const at = "https://pools-api.usenami.io/sim";
  const dir = example([{ ...SIM, gateway: at }], simRecords);
  try {
    for (const [args, want] of [[[], at], [["--gateway", "http://127.0.0.1:8789"], "http://127.0.0.1:8789"]]) {
      const out = join(dir, `config-${args.length}.mjs`);
      const answer = run("--network", "mainnet", "--root", dir, ...args, "--out", out);
      assert.equal(answer.status, 0, answer.stderr);
      const { CONFIG } = await import(pathToFileURL(out).href);
      assert.equal(CONFIG.simIndex, want);
      assert.equal("gateway" in CONFIG, false, "a site of simulation pools sends no orders: it has no gateway");
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a site of simulation pools shows one factory, on the network's own USDC, and names no other deployment", async () => {
  const second = { ...SIM, label: "sim-main2", record: "deployments/mainnet-sim-main2.json", factory: "0x00000000000000000000000000000000000000F6" };
  const cases = [
    // Two staged factories: the site shows the pools of one.
    [[SIM, second], { ...simRecords, "deployments/mainnet-sim-main2.json": { ...SIM_RECORD, label: "sim-main2", SimPoolFactory: second.factory } },
      /2 staged simulation-pool deployments on mainnet/],
    // A retired factory beside the staged one is not shown, and does not stand in the way.
    [[SIM, { ...second, role: "retired" }], { ...simRecords, "deployments/mainnet-sim-main2.json": { ...SIM_RECORD, label: "sim-main2", SimPoolFactory: second.factory } }, null],
    // A retired factory alone is nothing to show.
    [[{ ...SIM, role: "retired" }], simRecords, /0 live deployments on mainnet in deployments\/LIVE\.json, and no staged simulation-pool deployment/],
    // A staged PoolFactory deployment is not a simulation pool's: nothing serves it yet, and the site does not read it.
    [[{ ...MAIN, role: "staged", gateway: null, keeper: null }], records, /0 live deployments on mainnet in deployments\/LIVE\.json, and no staged simulation-pool deployment/],
    // The pages call the token USDC, and mainnet's plaque calls it real: a factory on another token gets no site.
    [[SIM], { "deployments/mainnet-sim-main.json": { ...SIM_RECORD, usdc: "0x2B3370eE501B4a559b57D449569354196457D8Ab" } }, /usdc 0x2B3370eE501B4a559b57D449569354196457D8Ab, not mainnet's/],
    [[SIM], { "deployments/mainnet-sim-main.json": { ...SIM_RECORD, usdc: undefined } }, /usdc None, not mainnet's/],
    [[{ ...SIM, factory: second.factory }], simRecords, /sim-main: the entry's factory is not the record's SimPoolFactory/],
    [[SIM], { "deployments/mainnet-sim-main.json": { ...SIM_RECORD, chain_id: 998 } }, /chain_id 998, not mainnet's 999/],
  ];
  for (const [entries, recs, why] of cases) {
    const dir = example(entries, recs);
    try {
      const answer = run("--network", "mainnet", "--root", dir);
      if (why) {
        assert.notEqual(answer.status, 0, `settings were written: ${why}`);
        assert.match(answer.stderr, why);
      } else {
        assert.equal(answer.status, 0, answer.stderr);
        assert.match(answer.stdout, new RegExp(`factory: "${SIM_FACTORY}"`));
        assert.ok(!answer.stdout.includes(second.factory), "the retired factory is in the settings");
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
  // A testers deployment beside the staged factory, with no live one: the site is still the one factory's, and the
  // settings name no deployment the site would only read.
  const TESTERS = { ...MAIN, role: "testers" };
  const beside = example([SIM, TESTERS], { ...records, ...simRecords });
  try {
    const out = join(beside, "config.mjs");
    const answer = run("--network", "mainnet", "--root", beside, "--out", out);
    assert.equal(answer.status, 0, answer.stderr);
    const { CONFIG } = await import(pathToFileURL(out).href);
    assert.deepEqual(CONFIG.sim, { label: "sim-main", factory: SIM_FACTORY });
    assert.deepEqual(CONFIG.deployments, []);
    assert.equal("readOnlyDeployments" in CONFIG, false);
    assert.ok(!readFileSync(out, "utf8").includes(FACTORY), "the testers deployment's factory is in the settings of a site of simulation pools");
  } finally {
    rmSync(beside, { recursive: true, force: true });
  }
});

test("the entry a site of simulation pools is written from is held to the list's shape: its factory an address, its chain the network's, its gateway named", () => {
  const ZERO = "0x0000000000000000000000000000000000000000";
  const withFactory = (factory) => [[{ ...SIM, factory }], { "deployments/mainnet-sim-main.json": { ...SIM_RECORD, SimPoolFactory: factory } }];
  const { gateway, ...noGateway } = SIM;
  const { chainId, ...noChain } = SIM;
  const { factory: named, ...noFactory } = SIM;
  assert.deepEqual([gateway, chainId, named], [null, 999, SIM_FACTORY]);
  const cases = [
    // A factory that is no address a contract stands at: in the entry and in the record alike, so nothing else catches it.
    [...withFactory("nonsense"), /sim-main: factory 'nonsense' is not an address a factory stands at: 0x and 40 hexadecimal characters, not all zeros/],
    [...withFactory(ZERO), /sim-main: factory '0x0{40}' is not an address a factory stands at/],
    [...withFactory(SIM_FACTORY.slice(0, 41)), /is not an address a factory stands at/],
    [...withFactory(`${SIM_FACTORY}00`), /is not an address a factory stands at/],
    [...withFactory(` ${SIM_FACTORY}`), /is not an address a factory stands at/],
    [...withFactory(SIM_FACTORY.slice(2)), /is not an address a factory stands at/],
    // The entry's own chain id: the record's is held to the network already, the entry's was not read at all.
    [[{ ...SIM, chainId: 998 }], simRecords, /sim-main: chainId 998, not mainnet's 999/],
    [[{ ...SIM, chainId: "999" }], simRecords, /sim-main: chainId '999', not mainnet's 999/],
    [[{ ...SIM, chainId: null }], simRecords, /sim-main: chainId None, not mainnet's 999/],
    // A field the entry lacks is said by name, not met as a missing key.
    [[noGateway], simRecords, /sim-main: the entry has no `gateway`: null, or the https address its pools' index answers at/],
    [[noChain], simRecords, /sim-main: the entry has no `chainId`: the network's chain id/],
    [[noFactory], simRecords, /sim-main: the entry has no `factory`: its SimPoolFactory/],
    // An address in lower case is the same address: the settings carry it as the entry writes it.
    [...withFactory(SIM_FACTORY.toLowerCase()), null],
    [[SIM], simRecords, null],
  ];
  for (const [entries, recs, why] of cases) {
    const dir = example(entries, recs);
    try {
      for (const answer of [run("--network", "mainnet", "--root", dir), index("--network", "mainnet", "--root", dir)]) {
        if (why) {
          assert.notEqual(answer.status, 0, `written: ${why}`);
          assert.match(answer.stderr, why);
          assert.doesNotMatch(answer.stderr, /Traceback|KeyError/, "a refusal in words, not a crash");
          assert.equal(answer.stdout, "");
        } else {
          assert.equal(answer.status, 0, answer.stderr);
        }
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("testnet's settings name no simulation pool: its live deployment is its site, and the pages write its name", async () => {
  const { CONFIG } = await import(pathToFileURL(join(ROOT, "app/config.js")).href);
  assert.equal("sim" in CONFIG, false);
  assert.equal("simIndex" in CONFIG, false);
  assert.equal(CONFIG.networkName, "Hyperliquid testnet");
  assert.equal(CONFIG.chainName, "HyperEVM Testnet");
  assert.equal(CONFIG.chainHex, "0x3e6");
  assert.equal(CONFIG.rpc, "https://rpc.hyperliquid-testnet.xyz/evm");
  assert.ok(LIVE.some((e) => e.network === "testnet" && e.role === "staged" && e.label === "rehearsal998"),
    "the rehearsal's simulation pools are staged on testnet, and still not in its settings");
});

test("app/index.html is what the script writes for testnet: the committed page is testnet's, word for word", () => {
  const written = index("--network", "testnet");
  assert.equal(written.status, 0, written.stderr);
  assert.equal(written.stdout, PAGE);
  assert.equal(index().stdout, PAGE, "testnet is the default");
});

test("mainnet's index.html says mainnet in every place the committed one says testnet, and has none of the other pools' lines", () => {
  const dir = example([SIM], simRecords);
  try {
    const written = index("--network", "mainnet", "--root", dir);
    assert.equal(written.status, 0, written.stderr);
    const page = written.stdout;
    assert.doesNotMatch(page, /testnet|mock|hackathon|demo|no value/i);
    assert.match(page, /<title>Pools on Hyperliquid<\/title>/);
    assert.match(page, /<meta name="description" content="Pools on Hyperliquid\.">/);
    assert.match(page, /<meta property="og:title" content="Pools on Hyperliquid">/);
    assert.match(page, /<meta property="og:description" content="Pools on Hyperliquid\.">/);
    // The plaque stands where testnet's notice and the entry question stood, and the question is still asked.
    assert.match(page, /<div class="notice">Mainnet\. Real USDC\. Unaudited\.<\/div>/);
    assert.match(page, /<dialog id="gate">\s*<h2>Before you continue<\/h2>\s*<p>Mainnet\. Real USDC\. Unaudited\.<\/p>\s*<label class="check"><input type="checkbox" id="gate-agree"> I understand\.<\/label>/);
    assert.equal(page.match(/Mainnet\. Real USDC\. Unaudited\./g).length, 2);
    // The picture of a shared link has testnet's plaque drawn into it: mainnet's page names no picture.
    assert.doesNotMatch(page, /og:image|twitter:card/);
    // The menu is the pools alone; no page of a PoolFactory's pools, no sentence about their keys, no terms of their demo.
    assert.deepEqual([...page.matchAll(/<a href="(#[^"]*)"/g)].map((m) => m[1]), ["#/"]);
    assert.doesNotMatch(page, /holds a key|pool gateway|#\/terms|#\/new|#\/shared|#\/economics|#\/verify|#\/evidence/);
    // Everything else is the committed page: the same lines in the same order, less the ones that went.
    const kept = new Set(page.split("\n"));
    const changed = PAGE.split("\n").filter((line) => !kept.has(line));
    assert.equal(changed.length, 19, changed.join("\n"));
    assert.ok(changed.every((line) => /testnet|og:image|twitter:card|href="#\/(new|shared|economics|verify|evidence|terms)"|holds a key/.test(line)), changed.join("\n"));
    for (const id of ["wallet", "rpc-wait", "view", "build", "gateway-commit", "gate", "gate-agree", "gate-ok"]) {
      assert.ok(page.includes(`id="${id}"`), `the page lost #${id}, which app.js reads`);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("no index.html for a network with no site, or when the page's words have moved from where the script replaces them", () => {
  const empty = example([], {});
  try {
    const none = index("--network", "mainnet", "--root", empty);
    assert.notEqual(none.status, 0);
    assert.match(none.stderr, /0 live deployments on mainnet/);
    assert.equal(none.stdout, "");
  } finally {
    rmSync(empty, { recursive: true, force: true });
  }
  for (const [from, to, why] of [
    ["Hyperliquid <strong>testnet</strong> and mock USDC.", "Hyperliquid <strong>testnet</strong>, mock USDC.", /0 places read '<div class="notice">Hyperliquid <strong>testnet<\/strong> and mock USDC\.<\/div>', not 1/],
    ["<title>Pools on Hyperliquid · testnet</title>", "<title>Pools on Hyperliquid · testnet</title><title>Pools on Hyperliquid · testnet</title>", /2 places read '<title>Pools on Hyperliquid · testnet<\/title>', not 1/],
    ['      <a href="#/evidence">On chain</a>\n', '      <a href="#/evidence">On chain</a>\n      <a href="#/more">More</a>\n', null],
    ['      <a href="#/economics">Economics</a>\n', "", /0 places read '      <a href="#\/new">New pool<\/a>/],
  ]) {
    const dir = example([SIM], simRecords);
    try {
      assert.ok(PAGE.includes(from));
      writeFileSync(join(dir, "app/index.html"), PAGE.replace(from, to));
      const written = index("--network", "mainnet", "--root", dir);
      if (why) {
        assert.notEqual(written.status, 0, `a page was written: ${why}`);
        assert.match(written.stderr, why);
        assert.match(written.stderr, /the page changed, and its words for mainnet in scripts\/app_config\.py have to follow it/);
        assert.equal(written.stdout, "");
      } else {
        // A line the script does not know stays as it is: it adds nothing and takes nothing else away.
        assert.equal(written.status, 0, written.stderr);
        assert.match(written.stdout, /<a href="#\/more">More<\/a>/);
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
  const refused = index("--gateway", "http://127.0.0.1:8787");
  assert.notEqual(refused.status, 0);
  assert.match(refused.stderr, /--gateway is a setting: index\.html has none/);
});

// The repository's own mainnet entries, whatever they are on the day: none, and the script stops; or a staged
// simulation-pool deployment, and the settings and the page are that deployment's, on chain 999.
test("the repository's own mainnet site is what deployments/LIVE.json lists for mainnet today", async () => {
  const listed = LIVE.filter((e) => e.network === "mainnet" && ["live", "staged"].includes(e.role));
  const settings = run("--network", "mainnet");
  const page = index("--network", "mainnet");
  if (listed.length === 0) {
    for (const answer of [settings, page]) {
      assert.notEqual(answer.status, 0);
      assert.match(answer.stderr, /0 live deployments on mainnet/);
      assert.equal(answer.stdout, "");
    }
    return;
  }
  assert.equal(settings.status, 0, settings.stderr);
  assert.equal(page.status, 0, page.stderr);
  assert.match(settings.stdout, /^  chainId: 999,$/m);
  assert.match(settings.stdout, /^  rpc: "https:\/\/rpc\.hyperliquid\.xyz\/evm",$/m);
  assert.doesNotMatch(settings.stdout, /testnet/i);
  assert.doesNotMatch(page.stdout, /testnet|mock/i);
  const live = listed.find((e) => e.role === "live");
  if (!live) {
    // A site of simulation pools: the one staged factory, held to its entry.
    const sims = listed.filter((e) => "SimPoolFactory" in JSON.parse(readFileSync(join(ROOT, e.record), "utf8")));
    assert.equal(sims.length, 1, "the mainnet site shows the pools of one staged simulation-pool deployment");
    assert.match(settings.stdout, new RegExp(`^  sim: \\{\\n    label: "${sims[0].label}",\\n    factory: "${sims[0].factory}",\\n  \\},$`, "m"));
    assert.match(settings.stdout, /^  deployments: \[\n  \],$/m);
  }
});
