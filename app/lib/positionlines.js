// The lines the chart draws for the open position of the market on screen: where it was entered, its stop and its take.
// No browser and no reads here: the position, its stop and take are what the page already reads for its table, the fills
// are the events the journal already holds. The chart that draws them is app/views/examchart.js.
import { pxText, triggerLedger } from "./exam.js";
import { isKnown } from "./examscreen.js";

/**
 * The average price a position was entered at, in the contract's price units (a number: an average is not a whole
 * number of units), or null where the fills at hand do not prove it.
 *
 * The contract keeps no entry price, only the size of the position, so the price is worked out of the fills (`Filled`,
 * with `TriggerFilled` for those a stop or a take carried out; `events` as the journal holds them, oldest first). The
 * latest fills of the asset are added up from the newest back: where they come to exactly the position the chain says
 * is open now, the position was flat before the oldest of them, and the fills since are its whole history. A journal
 * that has not read back that far proves nothing, and then there is no price rather than a guess. Within that history
 * more of the position bought or sold on the same side moves the average, part of it closed leaves it where it was, and
 * a fill that carries it through zero opens the other side at its own price.
 */
export function entryPrice(events, asset, size) {
  const open = BigInt(size);
  if (open === 0n) return null;
  let trades;
  try {
    trades = triggerLedger(events.filter((e) => e.name === "Filled" || e.name === "TriggerFilled")).trades.filter((t) => t.asset === Number(asset));
  } catch {
    return null;
  }
  const signed = (t) => (t.isBuy ? t.size : -t.size);
  let sum = 0n;
  let from = -1;
  for (let i = trades.length - 1; i >= 0; i -= 1) {
    sum += signed(trades[i]);
    if (sum === open) {
      from = i;
      break;
    }
  }
  if (from < 0) return null;
  let held = 0n;
  let cost = 0;  // the price paid for what is held, in price units times size units
  for (const t of trades.slice(from)) {
    const delta = signed(t);
    const px = Number(t.px);
    if (held === 0n || (held > 0n) === (delta > 0n)) {
      cost += px * Number(t.size);
      held += delta;
    } else if (t.size <= (held < 0n ? -held : held)) {
      const was = held < 0n ? -held : held;
      cost *= Number(was - t.size) / Number(was);
      held += delta;
    } else {
      held += delta;
      cost = px * Number(held < 0n ? -held : held);
    }
  }
  if (held !== open || held === 0n) return null;
  return cost / Number(held < 0n ? -held : held);
}

/**
 * The lines for `asset`: {entry, stop, take}, each a price as a number or null, or null where the asset has no open
 * position (or its size decimals were not read, so no price can be written). `account` is `examAccount`'s answer,
 * `perps` the page's Map of assets by number, `events` the journal's events.
 */
export function positionLines({ account, asset, perps, events = [] }) {
  const p = account?.positions.find((x) => x.asset === asset);
  const perp = perps.get(asset);
  if (!p || p.size === 0n || !isKnown(perp)) return null;
  const dec = perp.szDecimals;
  const price = (units) => Number(pxText(units, dec));
  const entry = entryPrice(events, asset, p.size);
  return {
    entry: entry === null ? null : price(BigInt(Math.round(entry))),
    stop: p.stop ? price(p.stop.px) : null,
    take: p.take ? price(p.take.px) : null,
  };
}
