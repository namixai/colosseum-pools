// Fan-out reads, bounded.
//
// ethers sends everything a Promise.all hands it as ONE JSON-RPC batch. The public HyperEVM RPC
// refuses a batch over twenty calls outright -- it answers the whole batch with
// -32010 "Exceeded max limit of 20", and ethers surfaces that as "missing response for request".
// Measured on 20 Sep 2026. The new-pool page asked about 64 perps that way and never loaded.
export const MAX_BATCH = 20;

/**
 * The options of the app's read provider. readAll keeps one fan-out under the node's limit, but ethers batches
 * whatever reads start together -- two renders of a page, or a page's independent Promise.alls -- and only the
 * provider itself can hold all of them to it. batchMaxCount does: ethers splits a larger batch.
 */
export const PROVIDER_OPTIONS = Object.freeze({ staticNetwork: true, batchMaxCount: MAX_BATCH });

/** Runs `make(item, i)` for every item, in rounds of at most `size`, keeping the input's order. */
export async function readAll(items, make, size = MAX_BATCH) {
  if (!(size >= 1)) throw new Error("a round has to hold at least one call");
  const out = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(...(await Promise.all(items.slice(i, i + size).map((item, j) => make(item, i + j)))));
  }
  return out;
}
