// Deployment settings for the app. Testnet only.
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
  usdc: "0x2B3370eE501B4a559b57D449569354196457D8Ab",
  // The deployments the site reads, live first. Every field is copied from deployments/testnet-<label>.json, and
  // app/tests/deployments.test.mjs holds each one to its record, so the site and the host cannot part silently.
  //   factory, registry  the deployment's PoolFactory and KeyRegistry;
  //   deployBlock        its first block: where the check-it-yourself page says its history starts; nothing scans
  //                      back towards it (app/lib/keys.js);
  //   deployer           the deploy key: the pools it owns are our benches, which the list names (app/lib/listing.js);
  //   platformAssets     the perp indices the factory lists. The chain stays the authority -- the new-pool form asks
  //                      isPlatformAsset for each -- this only keeps the form from probing every perp at once.
  // The live one is what the pool gateway and the keepers serve since 1 Oct 2026: pools are bought, traded and opened
  // there. The archive is the first deployment, kept so its records can still be read and checked: it sells nothing.
  deployments: [
    {
      label: "demo2",
      role: "live",
      factory: "0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222",
      registry: "0x53AF27F65Dd7473c890f633aC0025b261307779e",
      deployBlock: 65736733,
      deployer: "0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4",
      platformAssets: [3, 4, 0],
    },
    {
      label: "demo",
      role: "archive",
      factory: "0xf2707FCf99eD546BBA4612783761e7906FA1958e",
      registry: "0x6b256B983b849934e0AA500cF2e3Ca176B0d35BA",
      deployBlock: 65021402,
      deployer: "0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4",
      platformAssets: [3, 4, 0],
    },
  ],
};
