// Hyperliquid testnet info API, and the number formats Hyperliquid accepts.
import { CONFIG } from "../config.js";

export async function info(body) {
  const res = await fetch(CONFIG.hlInfo, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Hyperliquid info ${body.type}: HTTP ${res.status}`);
  return res.json();
}

let metaCache = null;

export async function perps() {
  if (!metaCache) {
    const meta = await info({ type: "meta" });
    metaCache = meta.universe.map((a, index) => ({ index, name: a.name, szDecimals: a.szDecimals }));
  }
  return metaCache;
}

export async function perp(index) {
  return (await perps()).find((p) => p.index === index) || { index, name: `#${index}`, szDecimals: 0 };
}

export const account = (user) => info({ type: "clearinghouseState", user });
export const spot = (user) => info({ type: "spotClearinghouseState", user });
export const openOrders = (user) => info({ type: "openOrders", user });
export const fills = (user) => info({ type: "userFills", user });
export const mids = () => info({ type: "allMids" });

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
