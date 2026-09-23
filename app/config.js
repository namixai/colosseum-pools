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
  factory: "0xf2707FCf99eD546BBA4612783761e7906FA1958e",
  registry: "0x6b256B983b849934e0AA500cF2e3Ca176B0d35BA",
  // First block of the deployment: what the check-it-yourself page names as the start of this
  // deployment's history. Nothing scans back towards it — see app/lib/keys.js.
  deployBlock: 65021402,
  usdc: "0x2B3370eE501B4a559b57D449569354196457D8Ab",
  // Perp indices the factory lists, copied from deployments/testnet-<label>.json after a deploy.
  // The chain stays the authority -- the new-pool form asks isPlatformAsset for each of these and
  // drops any the factory does not confirm. The list is here only to keep the form from probing
  // every perp on the venue, which the public RPC refuses as one batch.
  platformAssets: [3, 4, 0],
};
