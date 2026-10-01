// The list of pools: what the page says above it, which pools it puts first, and what a card says about a pool
// that cannot sell a challenge. No chain reads here; views/pools.js reads and hands the facts in.

/** The line above the list: the site's own description (index.html), and the two ways in. */
export const LANDING = {
  line: "Investors open pools with rules; traders and AI agents take challenges without ever holding a key.",
  invest: { label: "I want to invest", href: "#/shared" },
  trade: { label: "I want to trade", none: "No investor's pool on the live deployment can sell a challenge right now: "
    + "the list is below." },
};

/**
 * What kind of pool this is, by who owns it. Every pool in the demo's list but one is ours: benches owned by the
 * deploy key, with test parameters, and seats of the shared pools, which a contract owns. They stay listed --
 * the On chain page links to them -- but under their own heading, named for what they are. A pool anyone else
 * opens is listed as it is.
 */
export function poolKind({ owner, ownerIsContract, deployer }) {
  if (deployer && String(owner).toLowerCase() === String(deployer).toLowerCase()) return "bench";
  if (ownerIsContract) return "seat";
  return "pool";
}

/**
 * Whether an address holds code, for poolKind. A label is not worth a page: a refused read (a node that rate-limits,
 * say) answers false, so the pool is listed as anyone's, and the list and the pool page still load.
 */
export async function holdsCode(getCode, address) {
  try {
    return (await getCode(address)) !== "0x";
  } catch {
    return false;
  }
}

export const KIND_NOTE = {
  bench: "Rehearsal pool: ours, with test parameters.",
  seat: "A seat of a shared pool: many investors hold shares in it.",
  pool: "",
};

export const OTHERS_HEADING = "Rehearsal pools and shared-pool seats";

/** The line a card carries when the pool cannot sell a challenge; empty when it can. */
export function cardBlockerLine(blocker) {
  if (!blocker || blocker.kind === "taken") return "";
  if (blocker.kind === "underfunded") {
    return `Can't sell a challenge: ${Number(blocker.short).toFixed(2)} USDC short on HyperCore until its investor tops it up.`;
  }
  if (blocker.kind === "not-prepared") return "Can't sell a challenge: its investor has not prepared the account.";
  return "Can't sell a challenge: the last one is still settling.";
}

/** The pools in the order the page shows them: those anyone could buy from first, then the rest, newest first
 *  inside each group. Items are { kind, blocker, index } with index the order the factory lists them. */
export function listOrder(items) {
  const group = (i) => (i.kind === "pool" ? 0 : 1);
  return [...items].sort((a, b) => group(a) - group(b) || b.index - a.index);
}

/** Where "I want to trade" goes: the first investor's pool of the live deployment that can sell a challenge, or
 *  null -- then the button takes the visitor to the list, never to one of our benches or to the archive. */
export function tradeTarget(ordered) {
  const open = ordered.find((i) => i.kind === "pool" && !i.blocker && !i.archived);
  return open ? open.address : null;
}
