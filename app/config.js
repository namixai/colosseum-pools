// Deployment settings for the app. Testnet only.
// `factory` and `registry` are filled in from deployments/testnet-<label>.json after a deploy.
// `gateway` is the pools host behind Cloudflare (docs/HOSTING.md); against a gateway on this
// machine, use http://127.0.0.1:8787 instead.
export const CONFIG = {
  chainId: 998,
  chainHex: "0x3e6",
  chainName: "HyperEVM Testnet",
  nativeCurrency: { name: "HYPE", symbol: "HYPE", decimals: 18 },
  rpc: "https://rpc.hyperliquid-testnet.xyz/evm",
  hlInfo: "https://api.hyperliquid-testnet.xyz/info",
  hlExchange: "https://api.hyperliquid-testnet.xyz/exchange",
  hlApp: "https://app.hyperliquid-testnet.xyz",
  gateway: "https://pools-api.usenami.io",
  factory: "0x52A141515570eA66053D29bBCb61042e5693970D",
  registry: "0xAcB4af764984CD624098662A4057d7bA85E1176E",
  // First block of the deployment: what the check-it-yourself page names as the start of this
  // deployment's history. Nothing scans back towards it — see app/lib/keys.js.
  deployBlock: 64583921,
  usdc: "0x2B3370eE501B4a559b57D449569354196457D8Ab",
};
