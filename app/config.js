// Deployment settings for the app. Testnet only.
// `factory` and `registry` are filled in from deployments/testnet-<label>.json after a deploy.
export const CONFIG = {
  chainId: 998,
  chainHex: "0x3e6",
  chainName: "HyperEVM Testnet",
  nativeCurrency: { name: "HYPE", symbol: "HYPE", decimals: 18 },
  rpc: "https://rpc.hyperliquid-testnet.xyz/evm",
  hlInfo: "https://api.hyperliquid-testnet.xyz/info",
  hlApp: "https://app.hyperliquid-testnet.xyz",
  gateway: "http://127.0.0.1:8787",
  factory: "",
  registry: "",
  // First block of the deployment: event history is read from here, in small windows,
  // because the public RPC limits log queries.
  deployBlock: 0,
  logWindow: 1000,
  usdc: "0x2B3370eE501B4a559b57D449569354196457D8Ab",
  signerAttestation: "https://signer-demo.usenami.io:8443/attestation",
  baseRpc: "https://mainnet.base.org",
  pcr0Registry: "0x38b42eED740b0fDeb211bBDf773F2238cAEec240",
};
