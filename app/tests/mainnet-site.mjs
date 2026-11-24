// A mainnet site of simulation pools, built for the tests alone the way scripts/pack-site.sh --network mainnet packs
// one: app/ as it stands, with the config.js and the index.html scripts/app_config.py writes for mainnet in place of the
// committed ones, which are testnet's. The mainnet deployment is an example in a directory of its own, shaped like
// deployments/testnet-rehearsal998.json, never in deployments/: the addresses are placeholders. Not a test file: the
// tests that draw a mainnet site's pages import their libraries from the copy this makes.
import { spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const SCRIPT = join(ROOT, "scripts/app_config.py");

export const SIM_FACTORY = "0x00000000000000000000000000000000000000F5";
export const MAINNET_USDC = "0xb88339CB7199b77E23DB6E890353E22632Ba630f";
export const SIM_RECORD = {
  label: "sim-main", network: "mainnet", chain_id: 999, status: "complete", block: 2345678,
  SimPoolFactory: SIM_FACTORY, SimPoolImpl: "0x00000000000000000000000000000000000000B5",
  TermsBook: "0x00000000000000000000000000000000000000B6", usdc: MAINNET_USDC,
  deployer: "0x00000000000000000000000000000000000000D1", platform_assets: { BTC: 0, ETH: 1, SOL: 5 },
};
export const SIM_ENTRY = {
  label: "sim-main", network: "mainnet", chainId: 999, role: "staged", record: "deployments/mainnet-sim-main.json",
  factory: SIM_FACTORY, pool: null, gateway: null, deployer: SIM_RECORD.deployer, keeper: null,
};

/**
 * Builds the site in a directory of its own and answers `{ dir, site, config, index, lib(name), remove() }`: `site` is
 * the folder a server would serve, `config` and `index` the texts of its two written files, `lib(name)` the module
 * site/lib/<name> loaded beside mainnet's settings. `entry` changes the example's entry (its `gateway`, say).
 */
export function mainnetSite(entry = {}) {
  const dir = mkdtempSync(join(tmpdir(), "app-mainnet-"));
  // Beside testnet's entries only: a mainnet deployment that is really listed must not make the example a second one.
  const live = JSON.parse(readFileSync(join(ROOT, "deployments/LIVE.json"), "utf8")).deployments.filter((e) => e.network === "testnet");
  cpSync(join(ROOT, "spike/hlspike/network.py"), join(dir, "spike/hlspike/network.py"));
  cpSync(join(ROOT, "deployments"), join(dir, "deployments"), { recursive: true });
  writeFileSync(join(dir, "deployments/LIVE.json"), JSON.stringify({ deployments: [...live, { ...SIM_ENTRY, ...entry }] }, null, 2));
  writeFileSync(join(dir, SIM_ENTRY.record), JSON.stringify(SIM_RECORD, null, 2));
  // The site: app/ without its tests, then the two files written for mainnet over testnet's.
  cpSync(join(ROOT, "app"), join(dir, "app"), { recursive: true, filter: (from) => !from.startsWith(join(ROOT, "app/tests")) });
  const site = join(dir, "app");
  const written = {};
  for (const [name, args] of [["config.js", []], ["index.html", ["--index"]]]) {
    const run = spawnSync("python3", [SCRIPT, "--network", "mainnet", "--root", dir, ...args], { cwd: ROOT, encoding: "utf8" });
    if (run.status !== 0) {
      rmSync(dir, { recursive: true, force: true });
      throw new Error(`scripts/app_config.py wrote no mainnet ${name}: ${run.stderr}`);
    }
    written[name] = run.stdout;
  }
  // index.html is read from the example's own app/, so both are written only once both are read.
  for (const [name, text] of Object.entries(written)) writeFileSync(join(site, name), text);
  return {
    dir, site, config: written["config.js"], index: written["index.html"],
    lib: (name) => import(pathToFileURL(join(site, "lib", name)).href),
    remove: () => rmSync(dir, { recursive: true, force: true }),
  };
}
