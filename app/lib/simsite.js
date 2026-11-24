// A site of simulation pools (app/config.js, `sim`): one SimPoolFactory, whose pools are the only pools the site shows.
// scripts/app_config.py writes `sim` for a network with no live deployment (docs/DEPLOYMENTS.md): the first page then
// lists the factory's pools, and a pool's page, its panels and an exam's screen open only for a pool the factory's own
// `pools()` names. A link to any other address draws "Not found": a contract that answers like a pool is not one of
// the site's because it says so. On a site whose settings name no factory (testnet's), the simulation pages open by a
// pool's address, as they did. No browser globals and no reads here, so node's test runner loads it
// (app/tests/simsite.test.mjs): the view reads the factory (app/views/simsite.js) and hands the answer in.
import { CONFIG } from "../config.js";
import { esc, sameAddress, rateLimited } from "./ui.js";
import { isMainnet } from "./network.js";

/** The factory whose pools the site shows (`{ label, factory }`), or null on a site whose settings name none. */
export const simSite = (config = CONFIG) => config?.sim ?? null;

/**
 * Which pool a page of the panels, or an exam's screen, may be drawn for, by the settings. "listed": a site of
 * simulation pools, a pool its factory's list names. "any": a site that names no factory and is not mainnet's
 * (testnet's pages open by a pool's address, as they did). "none": mainnet settings that name no factory. The script
 * writes no such settings (scripts/app_config.py stops on them); were some written by hand or by an older script,
 * the pages would otherwise draw whatever contract a link names, with real USDC behind its Buy button.
 */
export function poolPages(config = CONFIG) {
  if (simSite(config)) return "listed";
  return isMainnet(config) ? "none" : "any";
}

/** Whether `address` is one of `pools`, the factory's own list, case aside. A list that was not read (null: no
 *  contract stands at the factory's address) names no pool. */
export function isSitePool(pools, address) {
  return Array.isArray(pools) && pools.some((p) => sameAddress(p, address));
}

/** Whether a failed read says that no contract stands at the address: the node answered a view with no bytes, which
 *  ethers reports as BAD_DATA with the value "0x". A rate limit met inside a batch is BAD_DATA too, with the node's
 *  own answer as its value (app/lib/ui.js, rateLimitedBy): that one is a failed read, never "no contract". */
export const noContract = (err) => err?.code === "BAD_DATA" && err?.value === "0x";

/** Whether a read failed on its way to an answer (the node's rate limit, the network, anything that is not the
 *  contract's own word), as against the contract answering: a revert, or bytes that are not the getter's. Only the
 *  second says what stands at an address. A rate limit met inside a batch reaches a page as BAD_DATA, so it is asked
 *  first. An exam's screen on a site of simulation pools draws "Not found" on the contract's answer alone: a read
 *  that failed is the page's failure, with its "Try again". */
export const readFailed = (err) => rateLimited(err) || !["BAD_DATA", "CALL_EXCEPTION"].includes(err?.code);

/** What a page that is not one of the site's draws: the words of the site's own "no such page". */
export const NOT_FOUND = `<section class="card"><h2>Not found</h2><p><a href="#/">Back to the pools</a></p></section>`;

/**
 * The first page of a site of simulation pools: the factory's pools, oldest first, each a link to its page. `pools` is
 * the factory's `pools()`, or null when no contract stands at the factory's address yet: the page then says so, in
 * the name the settings give the network. `short` shortens an address for the eye; the link carries it whole.
 */
export function simListHtml(pools, { network, short }) {
  if (pools === null) {
    return `<section class="card"><h2>Not deployed yet</h2>
    <p>The contracts are not on ${esc(network)} yet, so there is nothing to list.</p></section>`;
  }
  if (pools.length === 0) return `<section class="card"><h2>Pools</h2><p>No pools yet.</p></section>`;
  return `<section class="card"><h2>Pools</h2><ul class="sim-list">${pools.map((pool) =>
    `<li><a href="#/sim/${esc(pool)}">Pool ${esc(short(pool))}</a></li>`).join("")}</ul></section>`;
}
