// The lines the chart draws for the open position of the market on screen: where it was entered, its stop and its take.
// No browser and no reads here: the position, its stop and take are what the page already reads for its table; the entry
// price is proven from the fills elsewhere (app/lib/entry.js, entrytrack.js). The chart that draws them is app/views/examchart.js.
import { pxText } from "./exam.js";
import { isKnown } from "./examscreen.js";

/**
 * The lines for `asset`: {entry, stop, take}, each a price as a number or null, or null where the asset has no open
 * position (or its size decimals were not read, so no price can be written). `account` is `examAccount`'s answer,
 * `perps` the page's Map of assets by number, `entryOf(asset, size)` the proven entry price in price units or null.
 */
export function positionLines({ account, asset, perps, entryOf = () => null }) {
  const p = account?.positions.find((x) => x.asset === asset);
  const perp = perps.get(asset);
  if (!p || p.size === 0n || !isKnown(perp)) return null;
  const dec = perp.szDecimals;
  const price = (units) => Number(pxText(units, dec));
  const entry = entryOf(asset, p.size);
  return {
    entry: entry === null ? null : price(BigInt(Math.round(entry))),
    stop: p.stop ? price(p.stop.px) : null,
    take: p.take ? price(p.take.px) : null,
  };
}
