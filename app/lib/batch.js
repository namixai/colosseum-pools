// Fan-out reads, bounded.
//
// ethers sends everything a Promise.all hands it as ONE JSON-RPC batch. The public HyperEVM RPC
// refuses a batch over twenty calls outright -- it answers the whole batch with
// -32010 "Exceeded max limit of 20", and ethers surfaces that as "missing response for request".
// Measured on 20 Sep 2026. The new-pool page asked about 64 perps that way and never loaded.
export const MAX_BATCH = 20;

/** Runs `make(item, i)` for every item, in rounds of at most `size`, keeping the input's order. */
export async function readAll(items, make, size = MAX_BATCH) {
  if (!(size >= 1)) throw new Error("a round has to hold at least one call");
  const out = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(...(await Promise.all(items.slice(i, i + size).map((item, j) => make(item, i + j)))));
  }
  return out;
}
