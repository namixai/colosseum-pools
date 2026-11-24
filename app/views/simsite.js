// A site of simulation pools, read (app/lib/simsite.js has what it is and the words): the factory's own list of its
// pools, the first page drawn from it, and the check a page of a pool makes before it draws anything.
import * as chain from "../lib/chain.js";
import { render } from "../lib/ui.js";
import { SIM_FACTORY_ABI } from "../lib/simpool.js";
import { simSite, isSitePool, noContract, simListHtml, NOT_FOUND } from "../lib/simsite.js";
import { networkName } from "../lib/network.js";

const { ethers } = window;

/** The pools of the site's factory as the chain has them now, oldest first; null when no contract stands at the
 *  factory's address. Any other failure of the read is thrown: a list that was not read is not an empty one. */
export async function sitePools() {
  try {
    return [...(await new ethers.Contract(simSite().factory, SIM_FACTORY_ABI, chain.readProvider).pools())];
  } catch (err) {
    if (noContract(err)) return null;
    throw err;
  }
}

/** The first page of a site of simulation pools. */
export async function simListView(page) {
  render(page, simListHtml(await sitePools(), { network: networkName(), short: chain.short }));
}

/**
 * Whether a page of `pool` may be drawn. On a site whose settings name no factory, always. On a site of simulation
 * pools, only for a pool its factory made: for any other address "Not found" is drawn here, under the site's own
 * header, and the caller draws nothing.
 */
export async function sitePool(pool, page) {
  if (!simSite()) return true;
  if (isSitePool(await sitePools(), pool)) return true;
  // The router set the panel's marker for the address: this is no panel's page.
  if (page.isConnected) delete document.body.dataset.panel;
  render(page, NOT_FOUND);
  return false;
}
