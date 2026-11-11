// The deployments the site reads (app/config.js): which one is live, which is the archive, and which one an address
// belongs to. No chain reads here; chain.js asks the factories and hands the answers in.
import { CONFIG } from "../config.js";

export const DEPLOYMENTS = CONFIG.deployments;

/** The deployment the gateway and the keepers serve: where pools are bought, traded and opened. */
export function liveDeployment(list = DEPLOYMENTS) {
  return list.find((d) => d.role === "live") ?? null;
}

/**
 * How many fields rules() answered, from the answer itself (hex): the tuple's fourth word is where `assets` starts
 * inside it, 0x80 after a head of four words and 0xa0 after five (gateway/chain.py, decode_rules). Null for neither.
 */
export function rulesFields(hex) {
  const word = (i) => BigInt(`0x${hex.slice(2 + 64 * i, 2 + 64 * (i + 1)) || "0"}`);
  const assetsAt = word(Number(word(0)) / 32 + 3);
  return assetsAt === 0x80n ? 4 : assetsAt === 0xa0n ? 5 : null;
}

/** The dex an order number's perp trades on: 0 for Hyperliquid's own, else a builder's (100000 + dex × 10000 + index). */
export function dexOf(asset) {
  return Number(asset) < 100000 ? 0 : Math.floor((Number(asset) - 100000) / 10000);
}

/**
 * Each asset's own leverage cap, times 100, by its coin: the platform's defaults (the CTO, 21 Oct 2026), the ones
 * ops/leverage.py gives a v3 pool's rules. A coin not named here takes the account's cap. A coin named with null has no
 * cap from the platform (a market withdrawn, or one whose listing waits for its number): poolRules refuses it rather
 * than give it the account's cap. GOLD: 20x, the leverage rule v1.3 (2 November 2026) for xyz:GOLD. SILVER: 15x, the same
 * rule's number for xyz:SILVER. BRENTOIL: withdrawn by the same rule; a pool that names it is refused.
 */
export const ASSET_LEVERAGE_X100 = { BTC: 1000, ETH: 1000, SOL: 500, HYPE: 1000, SP500: 1000, XYZ100: 800, CL: 300, BRENTOIL: null,
  GOLD: 2000, SILVER: 1500 };

/**
 * The rules createPool takes on `deployment`'s factory. Before v3, the four fields. On a v3 factory (`assetLeverage`
 * in app/config.js) each asset's own cap after them, in the order of `rules.assets`, never over the account's cap:
 * the factory refuses a cap over it, or under 1x. `names` are the assets' names in the same order ("BTC",
 * "xyz:XYZ100"); a builder dex's market goes by its coin.
 */
export function poolRules(deployment, rules, names) {
  const { dailyLossBps, maxDrawdownBps, maxLeverageX100, assets } = rules;
  const out = { dailyLossBps, maxDrawdownBps, maxLeverageX100, assets };
  if (!deployment?.assetLeverage) return out;
  // A v3 account trades on one dex, where its capital is: the factory refuses rules that mix them (BadRules).
  if (new Set(assets.map(dexOf)).size > 1) {
    throw new Error("A pool's assets all trade on one dex: Hyperliquid's own, or one builder's such as xyz. Pick them from one.");
  }
  const coin = (name) => String(name ?? "").split(":").pop();
  const waiting = names.filter((n) => ASSET_LEVERAGE_X100[coin(n)] === null);
  if (waiting.length) throw new Error(`${waiting.join(", ")}: the platform gives it no leverage cap, so no pool may list it.`);
  out.assetLeverageX100 = assets.map((_, i) => Math.min(ASSET_LEVERAGE_X100[coin(names[i])] ?? maxLeverageX100, maxLeverageX100));
  return out;
}

/** "BTC 10×, ETH 10×, SOL 5×": the platform's default cap of each of `names` that has one, as the new-pool form says it. */
export function defaultCapsText(names) {
  const coin = (name) => String(name ?? "").split(":").pop();
  return names.filter((n) => Number.isInteger(ASSET_LEVERAGE_X100[coin(n)])).map((n) => `${n} ${ASSET_LEVERAGE_X100[coin(n)] / 100}×`).join(", ");
}

/** "BTC 5×, ETH 10×": each asset of `assets` ("BTC, ETH") with its cap, times 100, in the same order. */
export function capsByAsset(assets, caps) {
  return assets.split(", ").map((name, i) => `${name} ${Number(caps[i]) / 100}×`).join(", ");
}

/** The first deployment, kept so its records can be read and checked. It sells nothing. */
export function isArchive(deployment) {
  return deployment?.role === "archive";
}

/** Given one answer per deployment in `list`'s order (isPool, isChallenge), the deployment that said yes. */
export function pickDeployment(list, answers) {
  const i = answers.findIndex(Boolean);
  return i < 0 ? null : list[i];
}

/** A deployment named by its factory's address or by its label, as the records page names it. */
export function deploymentNamed(name, list = DEPLOYMENTS) {
  const n = String(name ?? "").toLowerCase();
  return list.find((d) => d.factory.toLowerCase() === n || d.label.toLowerCase() === n) ?? null;
}

/**
 * The deployments the site only reads (app/config.js, readOnlyDeployments): it shows their pools and accounts as the
 * chain has them and offers nothing on them, no purchase, no order, no new pool. Never part of DEPLOYMENTS, which
 * the pages buy, trade and open pools on.
 */
export const READ_ONLY_DEPLOYMENTS = CONFIG.readOnlyDeployments ?? [];

/** Whether the site only reads `deployment`. */
export function isReadOnly(deployment) {
  return deployment?.role === "read-only";
}

/** Every deployment an address may belong to: the ones the site serves, live first, then the ones it only reads. */
export function everyDeployment() {
  return [...DEPLOYMENTS, ...READ_ONLY_DEPLOYMENTS];
}

/** The token `deployment`'s pools take a challenge's price in: its own when it names one, else the exchange's USDC. */
export function usdcOf(deployment) {
  return deployment?.usdc ?? CONFIG.usdc;
}

/** The gateway instance that serves `deployment`'s accounts: its own when it names one, else the site's. */
export function gatewayOf(deployment) {
  return deployment?.gateway ?? CONFIG.gateway;
}

export const READ_ONLY = {
  heading: "HIP-3 deployment (v3), read only",
  note: "These pools belong to the v3 deployment, whose accounts trade on a builder's dex (HIP-3). The site shows "
    + "them as the chain has them; it sells no challenges here and takes no orders for them.",
  poolNote: "This pool belongs to the v3 deployment on a builder's dex (HIP-3), which this site only reads: it sells "
    + "no challenge here. What it shows is read from the chain.",
  challengeNote: "This challenge belongs to the v3 deployment on a builder's dex (HIP-3), which this site only reads: "
    + "orders for this account are not taken here. What it shows is read from the chain.",
};

export const ARCHIVE = {
  heading: "First deployment, kept as an archive",
  note: "These pools belong to the first deployment. It is kept so its records can be read and checked; the gateway "
    + "and the keepers serve the live deployment, so these pools sell no challenges.",
  poolNote: "This pool belongs to the first deployment, kept as an archive: it sells no challenges, because the "
    + "gateway and the keepers serve the live deployment. Its records can still be read and checked, and its investor "
    + "can still withdraw.",
  challengeNote: "This challenge belongs to the first deployment, kept as an archive: the gateway serves the live "
    + "deployment, so orders for this account are not taken. Its records can still be read and checked.",
};
