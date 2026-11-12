// The app's settings for Hyperliquid testnet, written by scripts/app_config.py from spike/hlspike/network.py and
// deployments/LIVE.json. Edit those and run `python3 scripts/app_config.py --out app/config.js`
// rather than this file: app/tests/config.test.mjs holds the committed copy to the script's output.
// `gateway` is the pools host behind Cloudflare (docs/HOSTING.md); for a gateway on this machine, run the script
// with --gateway http://127.0.0.1:8787.
export const CONFIG = {
  chainId: 998,
  chainHex: "0x3e6",
  chainName: "HyperEVM Testnet",
  nativeCurrency: { name: "HYPE", symbol: "HYPE", decimals: 18 },
  rpc: "https://rpc.hyperliquid-testnet.xyz/evm",
  hlInfo: "https://api.hyperliquid-testnet.xyz/info",
  hlExchange: "https://api.hyperliquid-testnet.xyz/exchange",
  hlApp: "https://app.hyperliquid-testnet.xyz",
  hyperliquidChain: "Testnet",
  gateway: "https://pools-api.usenami.io",
  usdc: "0x2B3370eE501B4a559b57D449569354196457D8Ab",
  // The deployments the site reads: the network's live entry in deployments/LIVE.json, then its archive ones. Every
  // field comes from the entry or its record, deployments/<network>-<label>.json, and app/tests/deployments.test.mjs
  // holds each one to them, so the site and the host cannot part silently.
  //   factory, registry  the deployment's PoolFactory, and where its keys are bound: its KeyRegistry, or its
  //                      KeyBinder on a deployment after it (docs/KEY-BINDING.md);
  //   keyBinder          true when `registry` is a KeyBinder: chain.js then reads it with the binder's ABI, which
  //                      answers bindingOf, isBound and keyOf as the registry does and has no freeCount. Absent for a
  //                      KeyRegistry.
  //   deployBlock        its first block: where the check-it-yourself page says its history starts; nothing scans
  //                      back towards it (app/lib/keys.js);
  //   deployer           the deploy key: the pools it owns are our benches, which the list names (app/lib/listing.js);
  //   platformAssets     the perp indices the factory lists. The chain stays the authority -- the new-pool form asks
  //                      isPlatformAsset for each -- this only keeps the form from probing every perp at once.
  //   sharedPool         the shared pool #/shared opens when the link names no address, and the only one the page
  //                      takes a deposit into: the network's first shared entry on the live factory. Its seats are
  //                      made by this deployment's factory.
  //   sharedPoolRecord   the label of that pool's record: the page's test holds the address to it. The archive has
  //                      none: its shared pools open by address and take no deposit.
  //   liveRunPool        a pool the team opened on the deployment after it was deployed, for a live run. The deploy
  //                      key owns it as it owns the benches, and its terms are not a bench's, so the list names it
  //                      apart (app/lib/listing.js). No record carries it; scripts/app_config.py keeps it.
  //   assetLeverage      true for a factory of v3 (its record names a PaperExamImpl), whose Rules carry each asset's
  //                      own leverage cap (assetLeverageX100): createPool then sends five fields
  //                      (app/lib/deployments.js, poolRules) and the pages show those caps. Absent for a factory
  //                      before v3, which takes four.
  // The live one is what the pool gateway and the keepers serve: pools are bought, traded and opened there. An
  // archive one is kept so its records can still be read and checked: it sells nothing.
  deployments: [
    {
      label: "demo2",
      role: "live",
      factory: "0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222",
      registry: "0x53AF27F65Dd7473c890f633aC0025b261307779e",
      deployBlock: 65736733,
      deployer: "0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4",
      platformAssets: [3, 4, 0],
      sharedPool: "0x348F9814061B0d26c2aBbdd2FE19ca807ceDa8a4",
      sharedPoolRecord: "shared-demo2c",
      liveRunPool: "0x70066669eC7Eb992055c82B615eD822CFF14e44A",
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
  // Deployments the site reads and offers nothing on: no purchase, no order, no new pool (app/lib/deployments.js,
  // READ_ONLY_DEPLOYMENTS). They are the network's `testers` entries in deployments/LIVE.json, kept out of
  // `deployments` above, which the pages buy, trade and open pools on. app/tests/readonly.test.mjs holds each to its
  // record. The fields of `deployments` above, and:
  //   record         the entry's label in LIVE.json, whose record is deployments/<network>-<record>.json;
  //   usdc           the token its pools take a challenge's price in, where the record names its own;
  //   gateway        its own gateway instance, which serves its accounts' orders;
  //   builtAt        the commit its contracts were built from. app/data/abi-v3-demo3.json is the ABI of demo3's at
  //                  that commit, and the test holds every call the app makes to them to it.
  readOnlyDeployments: [
    {
      label: "demo3",
      record: "v3-demo3",
      role: "read-only",
      factory: "0x070D0c7dd79D41e4329B2d231b308BA88089d81a",
      registry: "0x8e2E527aed19a0969aEa49e16aE1Abbc7B84Cc85",
      deployBlock: 67915896,
      deployer: "0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4",
      platformAssets: [3, 4, 0, 750003, 750000],
      assetLeverage: true,
      usdc: "0xa73D107dC507851CE4dD4A426e163e2F3368300D",
      gateway: "https://pools-api.usenami.io/demo3",
      builtAt: "23772e2dbf9f4913dc265efa68768030ab7d4d2a",
    },
  ],
};
