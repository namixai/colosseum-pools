// Spot transfers on HyperCore, signed by the connected wallet. This is how an investor's
// capital reaches a pool: the USDC bridge from HyperEVM credits nothing to a contract.
import { CONFIG } from "../config.js";
import { canonical, info } from "./hl.js";

export const SPOT_SEND_TYPES = {
  "HyperliquidTransaction:SpotSend": [
    { name: "hyperliquidChain", type: "string" },
    { name: "destination", type: "string" },
    { name: "token", type: "string" },
    { name: "amount", type: "string" },
    { name: "time", type: "uint64" },
  ],
};

const ZERO = "0x0000000000000000000000000000000000000000";

/**
 * A `spotSend` action and the EIP-712 data the wallet signs for it. `chainHex` is the
 * wallet's current chain: Hyperliquid accepts any chain for this kind of action, and
 * wallets refuse to sign for a chain they aren't on. `hyperliquidChain` is what keeps the
 * action on testnet.
 */
export function spotSend({ destination, token, amount, time, chainHex }) {
  if (!/^0x[0-9a-fA-F]{40}$/.test(destination) || destination.toLowerCase() === ZERO) {
    throw new Error(`not a destination address: ${destination}`);
  }
  if (!/^[A-Z0-9]+:0x[0-9a-f]{32}$/.test(token)) throw new Error(`not a token id: ${token}`);
  if (!Number.isSafeInteger(time) || time <= 0) throw new Error("time must be a timestamp in ms");
  if (!/^0x[0-9a-f]+$/.test(chainHex)) throw new Error(`not a hex chain id: ${chainHex}`);
  const message = { hyperliquidChain: "Testnet", destination, token, amount: canonical(amount), time };
  return {
    action: { type: "spotSend", signatureChainId: chainHex, ...message },
    domain: { name: "HyperliquidSignTransaction", version: "1", chainId: Number(BigInt(chainHex)), verifyingContract: ZERO },
    types: SPOT_SEND_TYPES,
    message,
  };
}

/** A 65-byte signature as Hyperliquid takes it. */
export function splitSignature(hex) {
  if (!/^0x[0-9a-fA-F]{130}$/.test(hex)) throw new Error("not a 65-byte signature");
  let v = parseInt(hex.slice(130, 132), 16);
  if (v < 27) v += 27;
  if (v !== 27 && v !== 28) throw new Error(`bad signature v: ${v}`);
  return { r: `0x${hex.slice(2, 66)}`, s: `0x${hex.slice(66, 130)}`, v };
}

/** What Hyperliquid answered, as the reason for a refusal or null for success. */
export function refusal(answer) {
  if (answer && answer.status === "ok") return null;
  if (answer && answer.status === "err") return String(answer.response || "refused");
  return "Hyperliquid's answer confirms nothing; check your balances";
}

/**
 * What the person can do about a refusal, or "" when the reason already says it.
 *
 * Hyperliquid switches this action off for an account in unified mode and answers "Action
 * disabled when unified account is active" -- which is true and useless to read. It stopped the
 * demo's own investor on 24 Sep 2026, and the same transfer made in Hyperliquid's app went
 * through, so that is what the page says now. The recognised case names the way out; anything
 * else keeps the venue's own words rather than a guess at what they mean.
 */
export function whatToDo(why, { destination, amount, app = CONFIG.hlApp } = {}) {
  if (/unified account/i.test(String(why || ""))) {
    return `Your Hyperliquid account has unified mode on, and that switches this transfer off. `
      + `Open ${app} with this wallet and send ${amount} USDC to ${destination} there instead.`;
  }
  return "";
}

let usdcToken = null;

async function usdcTokenId() {
  if (!usdcToken) {
    const meta = await info({ type: "spotMeta" });
    const t = (meta.tokens || []).find((x) => x.name === "USDC");
    if (!t) throw new Error("USDC is missing from Hyperliquid's spot metadata");
    usdcToken = `USDC:${t.tokenId}`;
  }
  return usdcToken;
}

/** Signs a USDC spot transfer with the wallet and sends it to Hyperliquid testnet. */
export async function sendUsdc(signer, destination, amount) {
  const chainHex = `0x${(await signer.provider.getNetwork()).chainId.toString(16)}`;
  const time = Date.now();
  const { action, domain, types, message } = spotSend({
    destination, token: await usdcTokenId(), amount, time, chainHex,
  });
  const signature = splitSignature(await signer.signTypedData(domain, types, message));
  const res = await fetch(CONFIG.hlExchange, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, nonce: time, signature, vaultAddress: null }),
  });
  const answer = await res.json().catch(() => null);
  const why = refusal(answer);
  if (why) {
    const advice = whatToDo(why, { destination, amount: canonical(amount) });
    throw new Error(`Hyperliquid refused the transfer: ${why}${advice ? `. ${advice}` : ""}`);
  }
}
