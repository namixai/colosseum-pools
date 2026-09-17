// Deployment settings for the app. Testnet only.
// `factory` and `registry` are filled in from deployments/testnet-<label>.json after a deploy.
export const CONFIG = {
  chainId: 998,
  chainHex: "0x3e6",
  chainName: "HyperEVM Testnet",
  nativeCurrency: { name: "HYPE", symbol: "HYPE", decimals: 18 },
  rpc: "https://rpc.hyperliquid-testnet.xyz/evm",
  hlInfo: "https://api.hyperliquid-testnet.xyz/info",
  hlExchange: "https://api.hyperliquid-testnet.xyz/exchange",
  hlApp: "https://app.hyperliquid-testnet.xyz",
  gateway: "http://127.0.0.1:8787",
  factory: "",
  registry: "",
  // First block of the deployment. Event history is read back towards it, newest first, in
  // windows of 50 blocks: HyperEVM answers no more per eth_getLogs call.
  deployBlock: 0,
  logWindow: 50,
  usdc: "0x2B3370eE501B4a559b57D449569354196457D8Ab",
};
