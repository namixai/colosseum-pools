// What "1. Who can trade this account" reads, kept away from the browser so a test can count
// the reads.
//
// Why it is a module of its own: the panel used to answer that question out of the event log.
// Reading the log meant `eth_getLogs` in windows of 50 blocks, up to 40 windows a call, twice
// per load. Measured against the public RPC on 20 Sep 2026: one load sent 25 HTTP requests
// carrying 58 JSON-RPC calls and was refused with -32005 partway through, and the refusal took
// down the four cheap reads that actually carry the answer. The scan could not have found
// anything either: 40 windows cover 2 000 blocks of the 199 832 since the deployment, about
// 1%, and the gap grows by some 88 000 blocks a day.
//
// So the panel now answers from what the contracts state. That is exact, it is four reads, and
// it does not decay: `agentKey`, the registry's binding, and `cutKey` / `cutBlock`, which the
// account writes down itself when a stop replaces its agent.

/** Reads the panel is allowed to make before it draws. The test holds this number. */
export const MAX_CHAIN_READS_ON_LOAD = 6;

export const ZERO = "0x0000000000000000000000000000000000000000";

export function isZero(address) {
  return !address || String(address).toLowerCase() === ZERO;
}

/**
 * The facts of the panel, from contract state only. `reads` is every call it may make:
 *   agentKey(), keyOf(), cutKey(), cutBlock(), bindingOf(key), role(key)
 * `role` is Hyperliquid's own answer and does not touch the RPC; the rest do.
 *
 * Returns the keys this account has held that can be named without the log: the one approved
 * now and the one a stop cut. Both may be absent, and they may be the same address only if a
 * cut key was later re-approved, which the registry does not allow.
 */
export async function keyFacts(reads) {
  const [current, boundNow, cutKey, cutBlock] = await Promise.all([
    reads.agentKey(), reads.keyOf(), reads.cutKey(), reads.cutBlock(),
  ]);
  const named = [];
  for (const address of [current, boundNow, cutKey]) {
    if (!isZero(address) && !named.some((k) => sameAddress(k, address))) named.push(address);
  }
  const keys = await Promise.all(named.map(async (address) => ({
    address,
    binding: await reads.bindingOf(address),
    // Hyperliquid's answer is a second opinion on facts the contract has already given. If it
    // fails, the key is shown without it -- a failure there must not take the contract's own
    // state down with it, which is the fragility this module exists to remove.
    role: await Promise.resolve().then(() => reads.role(address)).catch(() => null),
    isCurrent: sameAddress(address, current),
    wasCut: sameAddress(address, cutKey),
  })));
  return {
    current: isZero(current) ? null : current,
    boundNow: isZero(boundNow) ? null : boundNow,
    cutKey: isZero(cutKey) ? null : cutKey,
    cutBlock: Number(cutBlock || 0),
    keys,
    // What the panel says about itself has to be true: four state reads, then one binding per
    // key it could name. The page prints this number rather than a constant.
    chainReads: 4 + named.length,
  };
}

export function sameAddress(a, b) {
  return Boolean(a && b && String(a).toLowerCase() === String(b).toLowerCase());
}
