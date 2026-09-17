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
