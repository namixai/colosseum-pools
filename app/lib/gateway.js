// Orders go to the pool gateway signed by the trader's wallet (EIP-712, version 2).
// The wallet shows the fields; the gateway builds the Hyperliquid action from them.
import { CONFIG } from "../config.js";

const DOMAIN = { name: "colosseum-pools gateway", version: "2", chainId: CONFIG.chainId };

const TYPES = {
  Order: [
    { name: "account", type: "address" },
    { name: "asset", type: "uint32" },
    { name: "isBuy", type: "bool" },
    { name: "limitPx", type: "string" },
    { name: "size", type: "string" },
    { name: "reduceOnly", type: "bool" },
    { name: "tif", type: "string" },
    { name: "nonce", type: "uint64" },
    { name: "expiresAt", type: "uint64" },
  ],
  Cancel: [
    { name: "account", type: "address" },
    { name: "asset", type: "uint32" },
    { name: "oid", type: "uint64" },
    { name: "nonce", type: "uint64" },
    { name: "expiresAt", type: "uint64" },
  ],
};

function window45s() {
  const nonce = Date.now();
  return { nonce, expiresAt: nonce + 45_000 };
}

// Orders and the gateway's answers carry account data, so anything but a gateway on this
// machine has to be reached over https.
export function orderUrl(base) {
  const url = new URL(base);
  const local = url.hostname === "127.0.0.1" || url.hostname === "localhost" || url.hostname === "[::1]";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new Error(`the gateway must be reached over https, not ${url.protocol}//${url.host}`);
  }
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/v1/order`;
  url.search = "";
  url.hash = "";
  return url.toString();
}

async function post(body) {
  const res = await fetch(orderUrl(CONFIG.gateway), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await res.json().catch(() => ({ status: "bad_response" }));
  return { http: res.status, ...payload };
}

export async function placeOrder(signer, { account, asset, isBuy, limitPx, size, reduceOnly = false, tif = "Gtc" }) {
  const order = { account, asset, isBuy, limitPx, size, reduceOnly, tif, ...window45s() };
  const signature = await signer.signTypedData(DOMAIN, { Order: TYPES.Order }, order);
  return post({ kind: "order", order, signature });
}

export async function cancelOrder(signer, { account, asset, oid }) {
  const cancel = { account, asset, oid: Number(oid), ...window45s() };
  const signature = await signer.signTypedData(DOMAIN, { Cancel: TYPES.Cancel }, cancel);
  return post({ kind: "cancel", cancel, signature });
}

/**
 * What to show a trader when an order was not placed.
 *
 * The gateway answers in two registers at once: a machine `code` and a `detail` written for a
 * person. The page showed the code and dropped the sentence, so a refusal read `upstream_busy`
 * when what the gateway actually said was "the chain node is refusing reads right now; try again
 * in a moment" — the difference between knowing to wait and not knowing what happened.
 *
 * It matters most on the two answers that are not refusals at all: when the gateway cannot tell
 * whether the order reached Hyperliquid, the detail says "the order may or may not have reached
 * Hyperliquid; check the account". No code can say that, and a trader who isn't told it may send
 * the order twice.
 *
 * A venue refusal carries Hyperliquid's own words in `reason`, and those come first: they are
 * what the exchange actually answered, not our description of it.
 */
export function refusalText(res = {}) {
  if (res.reason) return res.reason;
  if (res.detail) return res.detail;
  return res.code || res.status || "the gateway said nothing";
}
