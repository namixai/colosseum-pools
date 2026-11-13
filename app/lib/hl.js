// Hyperliquid testnet info API, and the number formats Hyperliquid accepts.
import { CONFIG } from "../config.js";

export async function info(body) {
  const res = await fetch(CONFIG.hlInfo, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // The status goes on the error, not only into its message: the failure page tells a refusal
  // (429) from a failure by the status (app/lib/ui.js) rather than by parsing the words.
  if (!res.ok) throw Object.assign(new Error(`Hyperliquid info ${body.type}: HTTP ${res.status}`), { status: res.status });
  return res.json();
}

// The list of perps is asked once: whoever asks while it is on its way waits for that answer. Kept as the answer
// alone, every pool's card that started before it arrived asked again: the home page sent it 36 times at once
// (13 Oct 2026). A failure is not kept, so the next asking asks again.
let metaAsked = null;

export function perps() {
  if (!metaAsked) {
    metaAsked = info({ type: "meta" })
      .then((meta) => meta.universe.map((a, index) => ({ index, name: a.name, szDecimals: a.szDecimals })))
      .catch((err) => {
        metaAsked = null;
        throw err;
      });
  }
  return metaAsked;
}

// A perp of another dex (HIP-3) is numbered 100000 + dex × 10000 + its index in that dex's list: 750000 is
// xyz:XYZ100 on testnet (gateway/assets.py). Its name is in its own dex's meta, asked of that dex by name; the dex
// list and each dex's meta are asked once, as Hyperliquid's own list is above.
const HIP3_BASE = 100000;
const DEX_STRIDE = 10000;
let dexesAsked = null;
const dexMetaAsked = new Map();

function perpDexs() {
  if (!dexesAsked) {
    dexesAsked = info({ type: "perpDexs" });
    dexesAsked.catch(() => {
      dexesAsked = null;
    });
  }
  return dexesAsked;
}

function dexMeta(dex) {
  if (!dexMetaAsked.has(dex)) {
    const asked = info({ type: "meta", dex });
    asked.catch(() => dexMetaAsked.delete(dex));
    dexMetaAsked.set(dex, asked);
  }
  return dexMetaAsked.get(dex);
}

export async function perp(index) {
  const i = Number(index);
  const unknown = { index: i, name: `#${i}`, szDecimals: 0 };
  if (i < HIP3_BASE) return (await perps()).find((p) => p.index === i) || unknown;
  const dex = (await perpDexs())[Math.floor((i - HIP3_BASE) / DEX_STRIDE)];
  if (!dex?.name) return unknown;
  const asset = (await dexMeta(dex.name)).universe?.[(i - HIP3_BASE) % DEX_STRIDE];
  return asset ? { index: i, name: asset.name, szDecimals: asset.szDecimals } : unknown;
}

export const account = (user) => info({ type: "clearinghouseState", user });
export const spot = (user) => info({ type: "spotClearinghouseState", user });
export const openOrders = (user) => info({ type: "openOrders", user });
export const fills = (user) => info({ type: "userFills", user });
export const mids = () => info({ type: "allMids" });
/** A perp's candles from `startTime` to `endTime` (ms), by its name ("BTC") and an interval ("1m", "15m", "1h", "1d"):
 *  open data, no key. `candleRows` (app/lib/exam.js) reads the answer. */
export const candles = (coin, interval, startTime, endTime) =>
  info({ type: "candleSnapshot", req: { coin, interval, startTime, endTime } });
/** A perp's book, both sides from the best: open data, no key. `bookSides` (app/lib/exam.js) reads the answer. */
export const book = (coin) => info({ type: "l2Book", coin });
/** An account's deposits, withdrawals and transfers since `startTime` (ms), as Hyperliquid's own ledger has them. */
export const ledger = (user, startTime) => info({ type: "userNonFundingLedgerUpdates", user, startTime });

export async function spotUsdc(user) {
  const s = await spot(user);
  const b = (s.balances || []).find((x) => x.coin === "USDC");
  return b ? Number(b.total) : 0;
}

/** Positions as {index, name, szi}. Coin names are mapped back to perp indices. */
export async function positions(user) {
  const [state, list] = await Promise.all([account(user), perps()]);
  return (state.assetPositions || []).map((ap) => {
    const p = list.find((x) => x.name === ap.position.coin);
    return { index: p ? p.index : -1, name: ap.position.coin, szi: Number(ap.position.szi) };
  });
}

/** The orders to name in a stop or a settle call, as [asset, oid] pairs. */
export async function cancelsFor(user) {
  const [orders, list] = await Promise.all([openOrders(user), perps()]);
  return orders
    .map((o) => {
      const p = list.find((x) => x.name === o.coin);
      return p ? [p.index, BigInt(o.oid)] : null;
    })
    .filter(Boolean);
}

/**
 * A decimal string the way Hyperliquid normalizes it: no exponent, no trailing zeros, no
 * trailing dot, no leading zeros. The gateway accepts nothing else, because Hyperliquid checks
 * signatures against this form.
 */
export function canonical(text) {
  const t = String(text).trim();
  if (!/^\d+(\.\d+)?$/.test(t)) throw new Error(`not a plain positive number: ${text}`);
  let [whole, frac = ""] = t.split(".");
  whole = whole.replace(/^0+(?=\d)/, "");
  frac = frac.replace(/0+$/, "");
  const out = frac ? `${whole}.${frac}` : whole;
  if (!/[1-9]/.test(out)) throw new Error("must be above zero");
  return out;
}

/** Price rounded to Hyperliquid's perp rule: five significant figures (an integer price is
 *  always allowed) and at most 6 - szDecimals decimals. */
export function roundPrice(px, szDecimals) {
  if (!(px > 0)) throw new Error("price must be above zero");
  const maxDecimals = 6 - szDecimals;
  const intDigits = Math.floor(Math.log10(px)) + 1;
  let rounded;
  if (intDigits >= 5) {
    rounded = Math.round(px).toString();
  } else {
    const sig = Number(px.toPrecision(5));
    rounded = sig.toFixed(Math.max(0, Math.min(maxDecimals, 5 - intDigits)));
  }
  return canonical(rounded);
}

/** Size rounded down to the asset's size decimals. */
export function roundSize(sz, szDecimals) {
  const f = 10 ** szDecimals;
  const v = Math.floor(sz * f + 1e-9) / f;
  if (!(v > 0)) throw new Error(`size rounds to zero at ${szDecimals} decimals`);
  return canonical(v.toFixed(szDecimals));
}
