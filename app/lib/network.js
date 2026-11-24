// What a page says of the network it is built for. The site's settings (app/config.js, written by scripts/app_config.py
// for one network) say which network that is and what the pages call it; no page names a network in its own text. No
// browser globals, so node's test runner loads it (app/tests/network.test.mjs).
import { CONFIG } from "../config.js";

/** Whether the settings are mainnet's, by Hyperliquid's own name for the chain: the one its signatures carry. */
export const isMainnet = (config = CONFIG) => config?.hyperliquidChain === "Mainnet";

/** The network as a page writes it beside a pool: "Hyperliquid testnet", and "Hyperliquid" on mainnet. */
export const networkName = (config = CONFIG) => String(config.networkName);

/** The word a sentence puts before a service of the network ("Hyperliquid's testnet API"): none on mainnet. */
export const networkWord = (config = CONFIG) => (isMainnet(config) ? "" : "testnet ");
